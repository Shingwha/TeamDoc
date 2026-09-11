"""云空间:上传 / 下载 / 文件夹 / 项目间移动(构建文档 §7.5,归属制)。

需求变更:取消独立"个人云空间"概念,一切文件/文件夹都归属项目
(个人空间 = 本人的 is_personal 项目),权限统一走项目角色(读 VIEWER / 写 EDITOR)。
删除 = 软删除进项目回收站(可恢复);彻底删除才清记录与物理文件。
"""
import mimetypes
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, bad_request, current_user, ensure_project_role, err,
                  get_project_or_404, project_role, require_project_role, require_write,
                  require_write_ctx, str_field)
from models import (FILES_DIR, Doc, File, Folder, Project, User, file_abspath, get_db,
                    new_id, unlink_quiet, utcnow)

router = APIRouter()

# 单文件上传上限:默认 20GB。定得高是有意的 —— 内网常见设计源文件与素材压缩包
# 动辄十几 GB,默认值定小了会变成"每次遇到更大的包就来改一次配置"。
# 真正兜底的不是这个数字,而是两道磁盘守卫:上传前的余量预检 + 写盘时每 16MB 复查
# (见下方 _check_reserve 与 upload_file),所以调大上限不会让磁盘失去保护。
# 环境变量是唯一真相源,管理后台只做只读展示 —— 见 admin.py 返回的 limits 字段。
# 注意:反代(client_max_body_size)必须 ≥ 本值,否则大包在反代层就被拦成 413,
# 应用收不到请求、也就给不出"上限 X MB"这句明确提示(见 DEPLOY.md §1.5)。
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20480"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
CHUNK = 1024 * 1024  # 流式写盘,逐 1MB 块(§7.5)
# 上传前要求保留的最小磁盘余量:磁盘写满的表现是 500 与半截文件,提前拒绝体验更好
STORAGE_RESERVE_MB = int(os.environ.get("STORAGE_RESERVE_MB", "1024"))

# ---------- 类型判定与 inline 白名单(安全边界,勿放宽) ----------
# mime 由**服务端**按扩展名判定,不采信上传方(set_*_mime 对多媒体类型有修正)
_EXTRA_MIME = {
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".yaml": "text/yaml", ".yml": "text/yaml",
    ".toml": "text/plain", ".ini": "text/plain", ".conf": "text/plain",
    ".log": "text/plain", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".ts": "text/plain", ".tsx": "text/plain", ".jsx": "text/plain",
    ".py": "text/x-python", ".rb": "text/x-ruby", ".go": "text/x-go",
    ".rs": "text/x-rust", ".java": "text/x-java", ".c": "text/x-c",
    ".h": "text/x-c", ".cpp": "text/x-c++", ".hpp": "text/x-c++",
    ".sh": "text/x-sh", ".bash": "text/x-sh", ".zsh": "text/x-sh",
    ".ps1": "text/plain", ".sql": "text/x-sql", ".xml": "text/xml",
    ".css": "text/css", ".scss": "text/plain", ".less": "text/plain",
    ".vue": "text/plain", ".svelte": "text/plain", ".diff": "text/plain", ".patch": "text/plain",
}

# 可 inline 显示的**显式白名单**。危险点:同源渲染的可执行格式会让上传者拿到 XSS
# (svg 内可含 <script>、html 直接是文档),故 svg/html/xhtml 与一切未知类型
# 一律强制 attachment 下载。
_INLINE_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "image/avif", "image/x-icon", "image/vnd.microsoft.icon",
    "application/pdf",
} | {v for v in _EXTRA_MIME.values()} | {"text/plain"}

# 文本类:前端据此走站内模态框预览(而非图片/PDF 的新标签页)
TEXT_MIME_PREFIXES = ("text/",)
TEXT_MIME_EXACT = {"application/json", "application/xml", "application/x-yaml"}

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".ico")


def guess_mime(name: str) -> str:
    """按文件名判定 mime(服务端唯一真相,不采信客户端声明)"""
    ext = Path(name).suffix.lower()
    if ext in _EXTRA_MIME:
        return _EXTRA_MIME[ext]
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def can_inline(mime: str) -> bool:
    return mime in _INLINE_MIME


def is_text(mime: str) -> bool:
    return mime.startswith(TEXT_MIME_PREFIXES) or mime in TEXT_MIME_EXACT


def _check_folder(db: DbSession, folder_id: str, project_id: str) -> Folder:
    """父文件夹必须属于同项目"""
    folder = db.get(Folder, folder_id)
    if not folder or folder.deleted_at is not None:
        err(404, "NOT_FOUND", "文件夹不存在")
    if folder.project_id != project_id:
        bad_request("父文件夹不属于该项目")
    return folder


def _get_file_or_404(db: DbSession, file_id: str) -> File:
    f = db.get(File, file_id)
    if not f or f.deleted_at is not None:
        err(404, "NOT_FOUND", "文件不存在")
    return f


def ensure_file_access(db: DbSession, ctx: AuthContext, f: File):
    """下载鉴权:项目角色(含公开项目的 VIEWER)→ 单文件公开 → 403。

    公开文件让"把这一份发给不在项目里的同事"成立,而不必把整个项目公开
    (在 NAS 上这是随手的事,此前唯一的办法是把对方加成项目成员,代价是
    让他看到整个项目)。

    用 role 判定而非捕获 ensure_project_role 的 403:后者把状态码语义塞进
    控制流,一旦将来错误码变化就会静默失效。
    """
    if project_role(db, f.project_id, ctx.user) is not None:
        return
    if f.is_public:
        return
    err(403, "FORBIDDEN", "需要 VIEWER 及以上权限")


def _get_folder_or_404(db: DbSession, folder_id: str) -> Folder:
    f = db.get(Folder, folder_id)
    if not f or f.deleted_at is not None:
        err(404, "NOT_FOUND", "文件夹不存在")
    return f


# 排序字段白名单:值直接映射到列,避免把用户输入拼进 order_by
_FILE_SORTS = {"name": File.name, "time": File.created_at, "size": File.size}
_FOLDER_SORTS = {"name": Folder.name, "time": Folder.created_at}


@router.get("/api/files")
def list_files(project_id: str = "", folder_id: str | None = None,
               offset: int = 0, limit: int = 100,
               sort: str = "name", dir: str = "asc",
               ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    """列目录:文件夹 + 文件(分页)。

    分页替代原先的"单次 500 条 + 提示拆目录":一旦当 NAS 用,扁平大目录很常见,
    500 条上限会真的碰到,而"建议拆分到子文件夹"是把系统限制转嫁给用户。

    排序在**服务端**做:分页与排序必须在一起 —— 客户端只排当前页,得到的是
    "每页内部有序"这种看似有序实则错误的结果。文件夹一次取全(单目录的文件夹
    数量远小于文件,且上层要按树渲染),文件才分页。
    """
    if not project_id:
        bad_request("project_id 不能为空")
    get_project_or_404(db, project_id)
    ensure_project_role(db, ctx, project_id, "VIEWER")
    if folder_id:
        _check_folder(db, folder_id, project_id)
    # 参数校验:非法值回落到默认,而不是报错(前端旧版本可能传别的 key)
    limit = max(1, min(int(limit or 100), 500))
    # offset 必须**双向**限幅:只限下界的话,传一个超过 int64 的值(SQLite 的
    # INTEGER 上限)会在绑定参数时抛 OverflowError,冒泡成 500。
    # 上界取 int32 级别:远超任何真实数据量,又不至于碰到 SQLite 的整数边界。
    offset = max(0, min(int(offset or 0), 2 ** 31 - 1))
    descending = (dir or "").lower() == "desc"
    fcol = _FOLDER_SORTS.get(sort or "", Folder.name)
    icol = _FILE_SORTS.get(sort or "", File.name)

    fq = db.query(Folder).filter_by(project_id=project_id)
    iq = db.query(File).filter_by(project_id=project_id)
    if folder_id:
        fq = fq.filter(Folder.parent_id == folder_id)
        iq = iq.filter(File.folder_id == folder_id)
    else:
        fq = fq.filter(Folder.parent_id.is_(None))
        iq = iq.filter(File.folder_id.is_(None))
    fq_live = fq.filter(Folder.deleted_at.is_(None))
    iq_live = iq.filter(File.deleted_at.is_(None))
    folder_total = fq_live.count()
    file_total = iq_live.count()
    folders = (fq_live.order_by(fcol.desc() if descending else fcol.asc(),
                                Folder.id.asc())  # 同名时按 id 定序,保证翻页稳定
               .limit(2000).all())
    files = (iq_live.order_by(icol.desc() if descending else icol.asc(),
                              File.id.asc())
             .offset(offset).limit(limit).all())
    # 弱提示:正文引用了 /api/files/{id}/download 的文档(单查询 + 正则提取,30 人规模足够)。
    # 只取当前页的 id 做匹配 —— 分页后扫描量不再随目录增大而线性增长
    page_ids = {f.id for f in files}
    referenced_ids: set[str] = set()
    if page_ids:
        for (content,) in db.query(Doc.content).filter(
                Doc.project_id == project_id, Doc.deleted_at.is_(None),
                Doc.content.like("%/api/files/%")).all():
            for fid in re.findall(r"/api/files/([0-9a-f]+)/download", content or ""):
                if fid in page_ids:
                    referenced_ids.add(fid)
    creator_ids = {f.created_by for f in files if f.created_by}
    creators = {u.id: u for u in db.query(User).filter(User.id.in_(creator_ids)).all()} \
        if creator_ids else {}
    return {
        "folders": [{"id": f.id, "name": f.name, "createdAt": f.created_at.isoformat()}
                    for f in folders],
        "files": [{
            "id": f.id, "name": f.name, "mime": f.mime, "size": f.size,
            "referenced": f.id in referenced_ids,
            # 前端据此决定"眼睛"预览图标与站内模态框;判据在服务端,不各写一套
            "canInline": can_inline(f.mime), "isText": is_text(f.mime),
            "isPublic": bool(f.is_public),
            "createdAt": f.created_at.isoformat(),
            "createdBy": ({"id": creators[f.created_by].id, "name": creators[f.created_by].name}
                          if f.created_by in creators else None),
        } for f in files],
        # total 是截断前的计数(文件夹受 2000 上限,文件为真实总数)
        "total": {"folders": folder_total, "files": file_total},
        "offset": offset, "limit": limit,
        "hasMore": offset + len(files) < file_total,
    }


@router.get("/api/projects/{project_id}/storage")
def project_storage(project_id: str, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                    db: DbSession = Depends(get_db)):
    """项目占用统计。

    **活跃与回收站必须分列**:回收站里的文件仍占物理磁盘(只有彻底删除才 unlink),
    混成一个数会出现"我删了文件,占用怎么没变"的困惑。前端在云空间工具栏与项目
    设置页展示这两个数。
    """
    active_bytes, active_count = db.query(
        func.coalesce(func.sum(File.size), 0), func.count(File.id)
    ).filter_by(project_id=project_id).filter(File.deleted_at.is_(None)).one()
    trash_bytes, trash_count = db.query(
        func.coalesce(func.sum(File.size), 0), func.count(File.id)
    ).filter_by(project_id=project_id).filter(File.deleted_at.isnot(None)).one()
    folder_count = (db.query(Folder).filter_by(project_id=project_id)
                    .filter(Folder.deleted_at.is_(None)).count())
    doc_bytes = db.query(func.coalesce(func.sum(func.length(Doc.content)), 0)) \
        .filter_by(project_id=project_id).filter(Doc.deleted_at.is_(None)).scalar() or 0
    disk = shutil.disk_usage(str(FILES_DIR))
    return {
        "active": {"bytes": active_bytes or 0, "fileCount": active_count,
                   "folderCount": folder_count},
        "trash": {"bytes": trash_bytes or 0, "fileCount": trash_count},
        "docs": {"bytes": doc_bytes},
        # 磁盘余量是给用户的"还能传多少"信号,比任何配额都直观
        "disk": {"total": disk.total, "free": disk.free},
    }


def _dedupe_name(existing: set[str], name: str) -> str:
    """在同目录已有名字集合里取一个不冲突的名字:foo.png → foo(2).png → foo(3).png"""
    if name not in existing:
        return name
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, "." + ext) if dot else (name, "")
    n = 2
    while f"{base}({n}){suffix}" in existing:
        n += 1
    return f"{base}({n}){suffix}"


def _names_in_folder(db: DbSession, project_id: str, folder_id: str | None,
                     exclude_file: str | None = None,
                     exclude_folder: str | None = None) -> tuple[set, set]:
    """同目录下的未删除文件名与文件夹名(重名判定用)"""
    fq = db.query(File.name).filter_by(project_id=project_id).filter(File.deleted_at.is_(None))
    oq = db.query(Folder.name).filter_by(project_id=project_id).filter(Folder.deleted_at.is_(None))
    fq = fq.filter(File.folder_id == folder_id) if folder_id else fq.filter(File.folder_id.is_(None))
    oq = oq.filter(Folder.parent_id == folder_id) if folder_id else oq.filter(Folder.parent_id.is_(None))
    if exclude_file:
        fq = fq.filter(File.id != exclude_file)
    if exclude_folder:
        oq = oq.filter(Folder.id != exclude_folder)
    return ({n for (n,) in fq.all()}, {n for (n,) in oq.all()})


def _user_name_conflict(existing: set, existing_dirs: set, name: str, kind: str):
    """用户显式操作(新建/重命名)遇重名 → 409 明确报错。

    不静默改名:用户输入的名字被悄悄改掉,比报错更让人困惑(他会以为系统没生效)。
    上传走另一条路 —— 那里自动加后缀,因为用户没有"为一个文件起名"的动作。
    """
    if name in existing:
        err(409, "CONFLICT", f"同目录下已有同名{kind}「{name}」,请换一个名称")
    if name in existing_dirs:
        err(409, "CONFLICT", f"同目录下已有同名文件夹「{name}」,请换一个名称")


@router.post("/api/files/folders")
def create_folder(payload: dict, ctx: AuthContext = Depends(require_write),
                  db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    name = str_field(payload, "name", 100, required=True)
    project_id = str_field(payload, "projectId", 20, required=True)
    parent_id = payload.get("parentId") or None
    get_project_or_404(db, project_id)
    ensure_project_role(db, ctx, project_id, "EDITOR")
    if parent_id:
        if not isinstance(parent_id, str):
            bad_request("parentId 必须为字符串")
        _check_folder(db, parent_id, project_id)
    file_names, dir_names = _names_in_folder(db, project_id, parent_id)
    _user_name_conflict(file_names, dir_names, name, "文件夹")
    folder = Folder(name=name, project_id=project_id, parent_id=parent_id)
    db.add(folder)
    db.commit()
    return {"id": folder.id, "name": folder.name, "createdAt": folder.created_at.isoformat()}


@router.patch("/api/files/folders/{folder_id}")
def rename_folder(folder_id: str, payload: dict, ctx: AuthContext = Depends(require_write),
                  db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    folder = _get_folder_or_404(db, folder_id)
    ensure_project_role(db, ctx, folder.project_id, "EDITOR")
    name = str_field(payload, "name", 100, required=True)
    if name != folder.name:
        file_names, dir_names = _names_in_folder(db, folder.project_id, folder.parent_id,
                                                 exclude_folder=folder.id)
        _user_name_conflict(file_names, dir_names, name, "文件夹")
    folder.name = name
    db.commit()
    return {"id": folder.id, "name": folder.name, "createdAt": folder.created_at.isoformat()}


@router.delete("/api/files/folders/{folder_id}")
def delete_folder(folder_id: str, ctx: AuthContext = Depends(require_write),
                  db: DbSession = Depends(get_db)):
    """删除文件夹 = **递归软删除整棵子树**(文件夹 + 后代文件夹 + 文件)。

    历史语义是"非空拒删",后果是用户必须自底向上手工清空才能删掉一个目录:
    层级越深步骤越多,中途失败还会留下删了一半的状态。现改为一次性整棵软删除,
    与文档删除的语义对齐(docs.py 的 delete_doc 也是整棵子树),且因为是软删除
    所以完全可恢复。

    回收站只列**子树根**(见 docs.py 的 project_trash),不会因为删一个目录
    而多出几十条子项。
    """
    folder = db.get(Folder, folder_id)
    if not folder:
        err(404, "NOT_FOUND", "文件夹不存在")
    # 先校权限再暴露删除状态:授权判定先于资源状态,非成员无法据状态码差异探测
    ensure_project_role(db, ctx, folder.project_id, "EDITOR")
    if folder.deleted_at is not None:
        err(404, "NOT_FOUND", "文件夹不存在")
    ids = _folder_subtree_ids(db, folder)
    now = utcnow()
    removed_files = (db.query(File)
                     .filter(File.folder_id.in_(ids), File.deleted_at.is_(None))
                     .update({"deleted_at": now}, synchronize_session=False))
    removed_folders = (db.query(Folder)
                       .filter(Folder.id.in_(ids), Folder.deleted_at.is_(None))
                       .update({"deleted_at": now}, synchronize_session=False))
    db.commit()
    return {"ok": True, "removedFolders": removed_folders, "removedFiles": removed_files}


def _folder_subtree_ids(db: DbSession, folder: Folder) -> list[str]:
    """该文件夹及其全部后代文件夹 id(不看删除状态 —— 供删除/恢复/彻底删除/移动统一使用)"""
    rows = db.query(Folder.id, Folder.parent_id).filter_by(project_id=folder.project_id).all()
    children: dict = {}
    for fid, pid in rows:
        children.setdefault(pid, []).append(fid)
    result, stack = [], [folder.id]
    while stack:
        cur = stack.pop()
        result.append(cur)
        stack.extend(children.get(cur, []))
    return result


@router.post("/api/files/folders/{folder_id}/restore")
def restore_folder(folder_id: str, ctx: AuthContext = Depends(require_write),
                   db: DbSession = Depends(get_db)):
    """从回收站恢复文件夹 = **整棵子树**(与删除对称)。

    副作用须知:子树里**先前单独删掉**的东西会一起回来。这与文档的恢复语义一致
    (文档子树也是整体恢复);要精确区分"哪些是这次删的"需要给删除操作记批次 id,
    本轮不做,已记入 HANDOFF §4.8。

    父文件夹仍在回收站时,本文件夹回落到项目根目录 —— 否则恢复出来的东西仍然
    看不见(对齐文档恢复语义)。
    """
    folder = db.get(Folder, folder_id)
    if not folder:
        err(404, "NOT_FOUND", "文件夹不存在")
    ensure_project_role(db, ctx, folder.project_id, "EDITOR")
    if folder.deleted_at is None:
        err(409, "CONFLICT", "文件夹不在回收站")
    ids = _folder_subtree_ids(db, folder)
    restored_folders = (db.query(Folder)
                        .filter(Folder.id.in_(ids), Folder.deleted_at.isnot(None))
                        .update({"deleted_at": None}, synchronize_session=False))
    restored_files = (db.query(File)
                      .filter(File.folder_id.in_(ids), File.deleted_at.isnot(None))
                      .update({"deleted_at": None}, synchronize_session=False))
    # 父级仍在回收站 → 回落到项目根。用 update 而非改 ORM 属性:本对象的状态
    # 已被上面的批量语句改过,直接赋属性会把陈旧的 deleted_at 一起写回去
    parent_id = folder.parent_id
    if parent_id and parent_id not in set(ids):
        parent = db.get(Folder, parent_id)
        if parent and parent.deleted_at is not None:
            db.query(Folder).filter(Folder.id == folder.id) \
                .update({"parent_id": None}, synchronize_session=False)
    db.commit()
    return {"ok": True, "restoredFolders": restored_folders, "restoredFiles": restored_files}


@router.post("/api/files/folders/{folder_id}/move")
def move_folder(folder_id: str, payload: dict, ctx: AuthContext = Depends(require_write),
                db: DbSession = Depends(get_db)):
    """移动文件夹(项目内整理 / 跨项目转移)。

    权限:项目内移动只需 EDITOR(整理自己项目的目录不该要求管理员);
    跨项目需源项目 ADMIN + 目标项目 EDITOR(与文件移动一致,防止把内容搬出不受控的项目)。

    跨项目时会一并改写**整棵子树**的 project_id —— 文件夹与文件都按 project_id
    归属,只改顶层会让子项留在原项目,形成跨项目的悬挂结构。
    """
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    folder = _get_folder_or_404(db, folder_id)
    target_id = str_field(payload, "projectId", 20, required=True)
    parent_id = payload.get("parentId") or None
    src_id = folder.project_id
    ensure_project_role(db, ctx, src_id, "ADMIN" if target_id != src_id else "EDITOR")
    get_project_or_404(db, target_id)
    ensure_project_role(db, ctx, target_id, "EDITOR")
    ids = _folder_subtree_ids(db, folder)
    if parent_id:
        if not isinstance(parent_id, str):
            bad_request("parentId 必须为字符串")
        if parent_id in set(ids):
            err(409, "CONFLICT", "不能移动到自己的子文件夹下")
        _check_folder(db, parent_id, target_id)
    folder.parent_id = parent_id
    if target_id != src_id:
        folder.project_id = target_id
        db.query(Folder).filter(Folder.id.in_(ids)) \
            .update({"project_id": target_id}, synchronize_session=False)
        db.query(File).filter(File.folder_id.in_(ids)) \
            .update({"project_id": target_id}, synchronize_session=False)
    db.commit()
    return {"id": folder.id, "projectId": folder.project_id, "parentId": folder.parent_id,
            "movedFolders": len(ids)}


@router.delete("/api/files/folders/{folder_id}/permanent")
def permanent_delete_folder(folder_id: str, ctx: AuthContext = Depends(require_write),
                            db: DbSession = Depends(get_db)):
    """彻底删除文件夹:仅回收站中的可删;连同子树内所有文件夹与文件一起清除(含物理文件)。

    由于删除即整棵软删除(见 delete_folder),回收站里一个文件夹的子树必然整体处于
    已删除状态;这里仍按"全部后代"清除,不区分删除状态 —— 用户要彻底删掉这个目录,
    目录里的东西自然一并消失。
    """
    folder = db.get(Folder, folder_id)
    if not folder:
        err(404, "NOT_FOUND", "文件夹不存在")
    ensure_project_role(db, ctx, folder.project_id, "EDITOR")
    if folder.deleted_at is None:
        err(409, "CONFLICT", "文件夹不在回收站")
    ids = _folder_subtree_ids(db, folder)
    files = db.query(File).filter(File.folder_id.in_(ids)).all()
    paths = [file_abspath(f.storage_path) for f in files]
    for f in files:
        db.delete(f)
    db.query(Folder).filter(Folder.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    for p in paths:
        unlink_quiet(p)  # 记录已删;清理失败不回滚,残留由管理后台孤儿清理兜底
    return {"ok": True, "removedFolders": len(ids), "removedFiles": len(files)}


class _TooLarge(Exception):
    pass


class _NoSpace(Exception):
    pass


class _Empty(Exception):
    pass


def _free_bytes() -> int:
    return shutil.disk_usage(str(FILES_DIR)).free


def _check_reserve(extra_needed: int = 0):
    """磁盘余量守卫:余量将低于 STORAGE_RESERVE_MB 时拒绝上传。

    在写盘前与写盘循环中各调用一次 —— 只检查开头的话,一个超大文件仍能把盘写满
    (表现为半截文件 + 500,且失败路径的清理本身也可能因为没空间而失败)。
    """
    reserve = STORAGE_RESERVE_MB * 1024 * 1024
    if _free_bytes() - extra_needed < reserve:
        err(400, "VALIDATION", f"服务器存储空间不足(需保留 {STORAGE_RESERVE_MB}MB 余量),请联系管理员清理")


@router.post("/api/files/upload")
async def upload_file(request: Request,
                      projectId: str = "", folderId: str | None = None, name: str = "",
                      ctx: AuthContext = Depends(require_write),
                      db: DbSession = Depends(get_db)):
    """上传文件:请求体就是文件本身(raw body 流式写盘)。

    为什么不用 multipart:Starlette 的 multipart 解析器会把 >1MB 的 part 先落到
    **系统临时目录**(spool_max_size=1MB),handler 再拷进 data/files —— 传 2GB
    要 4GB 空闲且 IO 翻倍。raw body 没有这个双写,`xhr.upload.onprogress` 与
    Content-Length 照常可用,CLI 直传也更顺。

    代价:端点不再声明 Form/UploadFile,Swagger 里不好点(参数在 query string 上)。
    这是全项目唯一一处非标准上传写法,取舍已记入 HANDOFF §5。
    """
    if not projectId:
        bad_request("projectId 不能为空")
    display_name = (name or "").strip()[:255] or "未命名文件"
    get_project_or_404(db, projectId)
    ensure_project_role(db, ctx, projectId, "EDITOR")
    if folderId:
        _check_folder(db, folderId, projectId)
    # 声明了长度就做一次快速预检,避免收完大文件才发现空间不足
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared:
        if declared > MAX_UPLOAD_BYTES:
            err(400, "VALIDATION", f"文件过大(上限 {MAX_UPLOAD_MB}MB)")
        if declared > _free_bytes():
            err(400, "VALIDATION", "服务器存储空间不足,请联系管理员清理")
    _check_reserve()
    # 同目录重名自动加后缀(foo.png → foo(2).png):上传是"把文件拖进来",
    # 用户没有"为它起名"的动作,为此弹一个报错再让他改名的体验更差
    file_names, _dirs = _names_in_folder(db, projectId, folderId)
    display_name = _dedupe_name(file_names, display_name)
    file_id = new_id()  # 主键需先生成(默认 default 仅在 INSERT 时触发)
    rec = File(id=file_id, name=display_name,
               project_id=projectId, folder_id=folderId,
               # mime 由服务端按文件名判定,不采信上传方传来的值
               mime=guess_mime(display_name),
               created_by=ctx.user.id,
               # 只存 basename:绝对路径与机器绑定,换机/换数据目录恢复会让全部文件失效。
               # 解析统一走 models.file_abspath()。
               storage_path=file_id)
    path = FILES_DIR / file_id
    total = 0
    try:
        with open(path, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise _TooLarge
                if total % (16 * CHUNK) < CHUNK:  # 每 16MB 复查一次余量,避免逐块 stat 的开销
                    if _free_bytes() < STORAGE_RESERVE_MB * 1024 * 1024:
                        raise _NoSpace
                out.write(chunk)
        if total == 0:
            raise _Empty
    except _TooLarge:
        path.unlink(missing_ok=True)
        err(400, "VALIDATION", f"文件过大(上限 {MAX_UPLOAD_MB}MB)")
    except _NoSpace:
        path.unlink(missing_ok=True)
        err(400, "VALIDATION", f"服务器存储空间不足(需保留 {STORAGE_RESERVE_MB}MB 余量),请联系管理员清理")
    except _Empty:
        # 空文件是合法的,但"一个字节都没收到"通常意味着请求写错了(比如忘了带 body),
        # 静默存成 0 字节文件会让人以为文件内容丢了
        path.unlink(missing_ok=True)
        err(400, "VALIDATION", "请求体为空,未收到文件内容")
    except Exception:
        # 客户端中断、磁盘错误等:半成品必须清掉,否则成为永久孤儿
        # (DB 记录尚未提交,而文件已占空间,界面里永远看不到它)
        path.unlink(missing_ok=True)
        raise
    rec.size = total
    db.add(rec)
    try:
        db.commit()
    except Exception:
        # 文件已完整落盘,但记录没提交(库锁超时、磁盘/句柄错误等)。
        # 不清理就是一个永久孤儿:占着磁盘、界面上永远看不见,只能等管理员跑孤儿清理。
        # 顺序仍是"先写完文件、再提交记录",所以不会出现反向的"记录在、文件没了"。
        path.unlink(missing_ok=True)
        raise
    return file_json(rec)


def file_json(rec: File) -> dict:
    """文件序列化(上传/改名共用;列表另见 list_files,含引用与创建者)"""
    return {"id": rec.id, "name": rec.name, "mime": rec.mime, "size": rec.size,
            "canInline": can_inline(rec.mime), "isText": is_text(rec.mime),
            "isPublic": bool(rec.is_public),
            "createdAt": rec.created_at.isoformat()}


@router.post("/api/files/{file_id}/move")
def move_file(file_id: str, payload: dict, ctx: AuthContext = Depends(require_write),
              db: DbSession = Depends(get_db)):
    """移动文件:项目内整理只需 EDITOR;跨项目需源项目 ADMIN + 目标项目 EDITOR。

    只改记录字段,物理文件不动。
    """
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    f = _get_file_or_404(db, file_id)
    target_id = str_field(payload, "projectId", 20, required=True)
    folder_id = payload.get("folderId") or None
    src_id = f.project_id
    ensure_project_role(db, ctx, src_id, "ADMIN" if target_id != src_id else "EDITOR")
    get_project_or_404(db, target_id)
    ensure_project_role(db, ctx, target_id, "EDITOR")
    if folder_id:
        if not isinstance(folder_id, str):
            bad_request("folderId 必须为字符串")
        _check_folder(db, folder_id, target_id)
    f.project_id = target_id
    f.folder_id = folder_id
    db.commit()
    return {"id": f.id, "projectId": f.project_id, "folderId": f.folder_id}


@router.get("/api/files/{file_id}/download")
def download_file(file_id: str, inline: str = "",
                  ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    """下载权限:项目成员(或全局管理员、公开项目访客),或该文件被单独设为公开"""
    f = _get_file_or_404(db, file_id)
    ensure_file_access(db, ctx, f)
    path = file_abspath(f.storage_path)
    if not path.is_file():
        err(404, "NOT_FOUND", "文件内容不存在")
    # inline 只对白名单类型生效:svg/html 等可执行格式强制 attachment,
    # 否则上传者能借"预览"在服务端同源下执行脚本(存储型 XSS)
    mime = f.mime or "application/octet-stream"
    want_inline = inline == "1" and can_inline(mime)
    if want_inline:
        disposition = "inline"
        media = mime
    else:
        disposition = "attachment"
        # 未知/危险类型回吐为二进制流,避免浏览器按扩展名嗅探后当页面渲染
        media = mime if can_inline(mime) else "application/octet-stream"
    cd = f"{disposition}; filename*=UTF-8''{quote(f.name)}"
    return FileResponse(str(path), media_type=media,
                        headers={"Content-Disposition": cd,
                                 "X-Content-Type-Options": "nosniff"})


@router.get("/api/files/{file_id}/meta")
def file_meta(file_id: str,
              ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    """单文件元数据:文档里 @文件 引用点击后,前端定位项目/文件夹并渲染预览浮层。

    可见性与 download 完全同口径(_get_file_or_404 + ensure_file_access):
    真成员 / 公开项目访客 / 单文件公开 均可读;回收站中的文件 404 ——
    引用一个已删除的文件就当它不存在,与 download 行为一致。
    """
    f = _get_file_or_404(db, file_id)
    ensure_file_access(db, ctx, f)
    # 位置上下文:location 契约与 docs.get_doc 完全同形(projectId/projectName/path),
    # 引用浮层、CLI --meta 等消费方一份代码即可展示"在哪"
    proj = db.get(Project, f.project_id)
    folder_path, cur, depth = [], (db.get(Folder, f.folder_id) if f.folder_id else None), 0
    while cur and depth < 64:  # 深度上限防数据异常成环
        folder_path.insert(0, cur.name)
        cur = db.get(Folder, cur.parent_id) if cur.parent_id else None
        depth += 1
    return {**file_json(f), "projectId": f.project_id, "folderId": f.folder_id,
            "location": {"projectId": f.project_id,
                         "projectName": proj.name if proj else "",
                         "path": folder_path}}


@router.patch("/api/files/{file_id}")
def rename_file(file_id: str, payload: dict, ctx: AuthContext = Depends(require_write),
                db: DbSession = Depends(get_db)):
    """改名 / 改公开状态(名称与 isPublic 可分别提交)"""
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    f = _get_file_or_404(db, file_id)
    ensure_project_role(db, ctx, f.project_id, "EDITOR")
    if "name" in payload:
        name = str_field(payload, "name", 255, required=True)
        if name != f.name:
            # 用户显式改名:重名直接报错(不静默加后缀,否则名字会被悄悄改掉)
            file_names, dir_names = _names_in_folder(db, f.project_id, f.folder_id, exclude_file=f.id)
            _user_name_conflict(file_names, dir_names, name, "文件")
        f.name = name
        # 改名可能改掉扩展名,mime 必须跟着重算 —— 否则把 evil.svg 改成 photo.png 之后,
        # 服务端仍按 svg 对待(或反之)会让预览/下载行为与看到的文件名不符
        f.mime = guess_mime(f.name)
    if "isPublic" in payload:
        f.is_public = bool(payload["isPublic"])
    db.commit()
    return {**file_json(f), "isPublic": bool(f.is_public)}


@router.delete("/api/files/{file_id}")
def delete_file(file_id: str, ctx: AuthContext = Depends(require_write),
                db: DbSession = Depends(get_db)):
    f = db.get(File, file_id)
    if not f:
        err(404, "NOT_FOUND", "文件不存在")
    # 先校权限再暴露删除状态(与文件夹删除一致)
    ensure_project_role(db, ctx, f.project_id, "EDITOR")
    if f.deleted_at is not None:
        err(404, "NOT_FOUND", "文件不存在")
    f.deleted_at = utcnow()
    db.commit()
    return {"ok": True}


@router.post("/api/files/{file_id}/restore")
def restore_file(file_id: str, ctx: AuthContext = Depends(require_write),
                 db: DbSession = Depends(get_db)):
    """从回收站恢复;所在文件夹也被删时回落到项目根目录(对齐文档恢复语义 §7.4)"""
    f = db.get(File, file_id)
    if not f:
        err(404, "NOT_FOUND", "文件不存在")
    # 先校权限再暴露回收站状态:否则非成员可凭 409/403 差异探测他人回收站内容
    ensure_project_role(db, ctx, f.project_id, "EDITOR")
    if f.deleted_at is None:
        err(409, "CONFLICT", "文件不在回收站")
    if f.folder_id:
        folder = db.get(Folder, f.folder_id)
        if not folder or folder.deleted_at is not None:
            f.folder_id = None
    f.deleted_at = None
    db.commit()
    return {"ok": True}


@router.delete("/api/files/{file_id}/permanent")
def permanent_delete_file(file_id: str, ctx: AuthContext = Depends(require_write),
                          db: DbSession = Depends(get_db)):
    """彻底删除:仅回收站中的文件可删;清记录并删除物理文件"""
    f = db.get(File, file_id)
    if not f:
        err(404, "NOT_FOUND", "文件不存在")
    ensure_project_role(db, ctx, f.project_id, "EDITOR")
    if f.deleted_at is None:
        err(409, "CONFLICT", "文件不在回收站")
    path = file_abspath(f.storage_path)
    db.delete(f)
    db.commit()
    unlink_quiet(path)  # 记录已删;清理失败不回滚,残留由管理后台孤儿清理兜底
    return {"ok": True}


def _zip_arcname(name: str, used: dict) -> str:
    """zip 内同名去重:foo.png → foo(2).png"""
    if name not in used:
        used[name] = 1
        return name
    used[name] += 1
    stem, dot, ext = name.rpartition(".")
    return f"{stem}({used[name]}).{ext}" if dot else f"{name}({used[name]})"


ZIP_MAX_FILES = 1000  # 一次打包的文件数上限(含展开的文件夹内容)
# 一次打包的**总字节**上限:只限文件数挡不住"1000 个超大文件"。取 4GB ——
# 远超内网日常批量下载,又不足以在压缩过程中把数据盘/系统盘写满。
ZIP_MAX_BYTES = int(os.environ.get("ZIP_MAX_BYTES_MB", "4096")) * 1024 * 1024


def _safe_seg(name: str) -> str:
    """zip 路径段清理:去掉路径分隔符与上跳,避免压缩包内的路径穿越"""
    return name.replace("/", "_").replace("\\", "_").replace("..", "_").strip() or "未命名"


def _collect_zip_targets(db: DbSession, ctx: AuthContext, file_ids: list[str],
                         folder_ids: list[str]) -> list[tuple[str, File]]:
    """把"勾选的文件 + 勾选的文件夹(递归)"展开成 (zip 内相对路径, File) 列表。

    文件夹会带出**相对所选根目录的目录结构** —— 这是它的意义所在:原来只能勾选
    平铺的文件,下载一个子目录得手动全选、下回来还全堆在一起。
    """
    wanted: list[tuple[str, File]] = []  # (前缀路径, 文件)
    # 1) 散选的文件:落在 zip 根
    for f in db.query(File).filter(File.id.in_(file_ids), File.deleted_at.is_(None)).all():
        ensure_project_role(db, ctx, f.project_id, "VIEWER")
        wanted.append(("", f))
    # 2) 勾选的文件夹:各自递归取子树,前缀 = 该文件夹名
    for fid in folder_ids:
        folder = _get_folder_or_404(db, fid)
        ensure_project_role(db, ctx, folder.project_id, "VIEWER")
        ids = _folder_subtree_ids(db, folder)
        folders = {x.id: x for x in db.query(Folder).filter(Folder.id.in_(ids)).all()}
        # 建"文件夹 id → 相对该根目录的路径"
        rel: dict[str, str] = {folder.id: _safe_seg(folder.name)}
        # 自顶向下推路径:按层级展开,父在前保证父路径已就绪
        # 自顶向下推路径:按层级展开,父在前保证父路径已就绪。
        # 用 "父 id → 子列表" 索引而不是每层扫全部文件夹(原写法是 O(n²),
        # 宽目录/深目录下明显变慢)
        children_of: dict[str, list] = {}
        for x in folders.values():
            children_of.setdefault(x.parent_id, []).append(x)
        pending = [folder.id]
        while pending:
            cur = pending.pop(0)
            for child in children_of.get(cur, ()):
                if child.id == folder.id:
                    continue  # 自环:根不再入队
                rel[child.id] = rel[cur] + "/" + _safe_seg(child.name)
                pending.append(child.id)
        files = (db.query(File).filter(File.folder_id.in_(ids), File.deleted_at.is_(None))
                 .order_by(File.created_at.asc()).all())
        for f in files:
            wanted.append((rel.get(f.folder_id, _safe_seg(folder.name)), f))
        # 展开后立即检查上限:原实现是全部收集完才判,于是传入少量文件夹 id
        # 就能让服务端展开并查询巨量行(拥有大目录的成员可据此造成资源消耗)
        if len(wanted) > ZIP_MAX_FILES:
            err(400, "VALIDATION",
                f"一次最多打包 {ZIP_MAX_FILES} 个文件(含文件夹展开),请减少选择")
    return wanted


@router.get("/api/files/zip")
def zip_files(ids: str = "", folderIds: str = "", ctx: AuthContext = Depends(current_user),
              db: DbSession = Depends(get_db)):
    """打包下载:GET /api/files/zip?ids=a,b&folderIds=c,d

    文件夹会递归展开并保留目录结构(见 _collect_zip_targets)。文件总数上限
    ZIP_MAX_FILES,超限直接拒绝 —— 比默默截断好:用户拿到一个不完整的包却以为
    是全部,这在"下载备份"场景下是危险的。
    """
    file_ids = [i for i in ids.split(",") if i]
    folder_ids = [i for i in folderIds.split(",") if i]
    if not file_ids and not folder_ids:
        bad_request("ids 与 folderIds 不能同时为空")
    targets = _collect_zip_targets(db, ctx, file_ids[:ZIP_MAX_FILES], folder_ids[:200])
    if not targets:
        err(404, "NOT_FOUND", "没有可下载的文件")
    if len(targets) > ZIP_MAX_FILES:
        err(400, "VALIDATION", f"一次最多打包 {ZIP_MAX_FILES} 个文件(含文件夹展开),请减少选择")
    # 压缩前先按**声明大小**预检总量:文件数上限挡不住"1000 个 20GB 的文件"。
    # zip 是边压边写 spool(超 64MB 落系统盘),没有这道预检就可能把磁盘写满,
    # 且失败发生在压缩中途、用户只看到一个中断的下载。
    declared = sum(f.size or 0 for _prefix, f in targets)
    if declared > ZIP_MAX_BYTES:
        err(400, "VALIDATION",
            f"所选文件合计 {declared // (1024 * 1024)}MB,超过单次打包上限 "
            f"{ZIP_MAX_BYTES // (1024 * 1024)}MB,请分批下载")
    # 先入内存缓冲(64MB),超出自动落临时盘;生成完再流式回吐,避免边下边压的连接占用
    spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    used: dict = {}
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as z:
        for prefix, f in targets:
            path = file_abspath(f.storage_path)
            if not path.is_file():
                continue  # 记录在但物理文件丢了:跳过,不让整包失败
            base = _safe_seg(f.name)
            # 对"含目录的完整路径"去重:a/b.png → a/b(2).png(rpartition 只切最后一段)
            arc = _zip_arcname((prefix + "/" + base) if prefix else base, used)
            z.write(str(path), arcname=arc)
    spool.seek(0, 2)
    total = spool.tell()  # 带上 Content-Length:否则 chunked 下载浏览器无法显示进度/及时完成
    spool.seek(0)

    def gen():
        try:
            while True:
                chunk = spool.read(CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            spool.close()

    cd = "attachment; filename*=UTF-8''" + quote("文件打包.zip")
    return StreamingResponse(gen(), media_type="application/zip",
                             headers={"Content-Disposition": cd, "Content-Length": str(total)})
