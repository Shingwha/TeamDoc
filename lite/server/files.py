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

from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, bad_request, current_user, ensure_project_role, err,
                  get_project_or_404, require_write, require_write_ctx, str_field)
from models import FILES_DIR, Doc, File, Folder, User, get_db, new_id, unlink_quiet, utcnow

router = APIRouter()

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
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


def _get_folder_or_404(db: DbSession, folder_id: str) -> Folder:
    f = db.get(Folder, folder_id)
    if not f or f.deleted_at is not None:
        err(404, "NOT_FOUND", "文件夹不存在")
    return f


@router.get("/api/files")
def list_files(project_id: str = "", folder_id: str | None = None,
               ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    if not project_id:
        bad_request("project_id 不能为空")
    get_project_or_404(db, project_id)
    ensure_project_role(db, ctx, project_id, "VIEWER")
    if folder_id:
        _check_folder(db, folder_id, project_id)
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
    # 截断提示:总数在截断前取,前端据此提示"仅显示前 N 项"(避免静默丢项)
    folder_total = fq_live.count()
    file_total = iq_live.count()
    folders = fq_live.order_by(Folder.created_at.asc()).limit(500).all()
    files = iq_live.order_by(File.created_at.asc()).limit(500).all()
    # 弱提示:正文引用了 /api/files/{id}/download 的文档(单查询 + 正则提取,30 人规模足够)
    referenced_ids: set[str] = set()
    for (content,) in db.query(Doc.content).filter(
            Doc.project_id == project_id, Doc.deleted_at.is_(None),
            Doc.content.like("%/api/files/%")).all():
        referenced_ids.update(re.findall(r"/api/files/([0-9a-f]+)/download", content or ""))
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
            "createdAt": f.created_at.isoformat(),
            "createdBy": ({"id": creators[f.created_by].id, "name": creators[f.created_by].name}
                          if f.created_by in creators else None),
        } for f in files],
        "total": {"folders": folder_total, "files": file_total},
    }


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
    folder.name = str_field(payload, "name", 100, required=True)
    db.commit()
    return {"id": folder.id, "name": folder.name, "createdAt": folder.created_at.isoformat()}


@router.delete("/api/files/folders/{folder_id}")
def delete_folder(folder_id: str, ctx: AuthContext = Depends(require_write),
                  db: DbSession = Depends(get_db)):
    """删除空文件夹 → 进项目回收站(可恢复)。

    "空" 只统计未删除内容:若子项已各自进回收站,则本文件夹可直接删除
    (与"含已删文件的文件夹可删"一致);这些子项留在回收站,直至本文件夹
    被彻底删除时级联清除。
    """
    folder = db.get(Folder, folder_id)
    if not folder:
        err(404, "NOT_FOUND", "文件夹不存在")
    # 先校权限再暴露删除状态:授权判定先于资源状态,非成员无法据状态码差异探测
    ensure_project_role(db, ctx, folder.project_id, "EDITOR")
    if folder.deleted_at is not None:
        err(404, "NOT_FOUND", "文件夹不存在")
    has_child_folder = (db.query(Folder).filter_by(parent_id=folder.id)
                        .filter(Folder.deleted_at.is_(None)).count() > 0)
    has_child_file = (db.query(File).filter_by(folder_id=folder.id)
                      .filter(File.deleted_at.is_(None)).count() > 0)
    if has_child_folder or has_child_file:
        err(403, "FORBIDDEN", "文件夹非空,请先清空内容")
    folder.deleted_at = utcnow()
    db.commit()
    return {"ok": True}


def _folder_subtree_ids(db: DbSession, folder: Folder) -> list[str]:
    """该文件夹及其全部后代文件夹 id(不看删除状态,供彻底删除级联)"""
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
    """从回收站恢复文件夹;父文件夹仍在回收站时回落到项目根目录(对齐文档恢复语义 §7.4)"""
    folder = db.get(Folder, folder_id)
    if not folder:
        err(404, "NOT_FOUND", "文件夹不存在")
    ensure_project_role(db, ctx, folder.project_id, "EDITOR")
    if folder.deleted_at is None:
        err(409, "CONFLICT", "文件夹不在回收站")
    folder.deleted_at = None
    if folder.parent_id:
        parent = db.get(Folder, folder.parent_id)
        if parent and parent.deleted_at is not None:
            folder.parent_id = None
    db.commit()
    return {"ok": True}


@router.delete("/api/files/folders/{folder_id}/permanent")
def permanent_delete_folder(folder_id: str, ctx: AuthContext = Depends(require_write),
                            db: DbSession = Depends(get_db)):
    """彻底删除文件夹:仅回收站中的可删;连同其下已删除的子文件夹与文件一起清除(含物理文件)

    文件夹删除时要求为空,故其树内不可能存在未删除的项(移动/上传均拒绝已删文件夹),
    级联范围只覆盖回收站内容。
    """
    folder = db.get(Folder, folder_id)
    if not folder:
        err(404, "NOT_FOUND", "文件夹不存在")
    ensure_project_role(db, ctx, folder.project_id, "EDITOR")
    if folder.deleted_at is None:
        err(409, "CONFLICT", "文件夹不在回收站")
    ids = _folder_subtree_ids(db, folder)
    files = db.query(File).filter(File.folder_id.in_(ids)).all()
    paths = [f.storage_path for f in files]
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
async def upload_file(file: UploadFile,
                      projectId: str = Form(...), folderId: str | None = Form(None),
                      mime: str | None = Form(None),
                      ctx: AuthContext = Depends(require_write),
                      db: DbSession = Depends(get_db)):
    get_project_or_404(db, projectId)
    ensure_project_role(db, ctx, projectId, "EDITOR")
    if folderId:
        _check_folder(db, folderId, projectId)
    _check_reserve()
    file_id = new_id()  # 主键需先生成(默认 default 仅在 INSERT 时触发)
    display_name = (file.filename or "未命名文件")[:255]
    rec = File(id=file_id, name=display_name,
               project_id=projectId, folder_id=folderId,
               # mime 由服务端按文件名判定,不采信上传方传来的值
               mime=guess_mime(display_name),
               created_by=ctx.user.id, storage_path=str(FILES_DIR / file_id))
    path = FILES_DIR / file_id
    total = 0
    try:
        with open(path, "wb") as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise _TooLarge
                if total % (16 * CHUNK) < CHUNK:  # 每 16MB 复查一次余量,避免逐块 stat 的开销
                    if _free_bytes() < STORAGE_RESERVE_MB * 1024 * 1024:
                        raise _NoSpace
                out.write(chunk)
    except _TooLarge:
        path.unlink(missing_ok=True)
        err(400, "VALIDATION", "文件过大")
    except _NoSpace:
        path.unlink(missing_ok=True)
        err(400, "VALIDATION", f"服务器存储空间不足(需保留 {STORAGE_RESERVE_MB}MB 余量),请联系管理员清理")
    except Exception:
        # 客户端中断、磁盘错误等:半成品必须清掉,否则成为永久孤儿
        # (DB 记录尚未提交,而文件已占空间,界面里永远看不到它)
        path.unlink(missing_ok=True)
        raise
    rec.size = total
    db.add(rec)
    db.commit()
    return {"id": rec.id, "name": rec.name, "mime": rec.mime, "size": rec.size,
            "canInline": can_inline(rec.mime), "isText": is_text(rec.mime),
            "createdAt": rec.created_at.isoformat()}


@router.post("/api/files/{file_id}/move")
def move_file(file_id: str, payload: dict, ctx: AuthContext = Depends(require_write),
              db: DbSession = Depends(get_db)):
    """项目间移动(原"归属转移"):只改记录字段,物理文件不动。

    发起方需源项目 ADMIN;目标项目需 EDITOR。
    """
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    f = _get_file_or_404(db, file_id)
    target_id = str_field(payload, "projectId", 20, required=True)
    folder_id = payload.get("folderId") or None
    ensure_project_role(db, ctx, f.project_id, "ADMIN")  # 源项目 ADMIN
    get_project_or_404(db, target_id)
    ensure_project_role(db, ctx, target_id, "EDITOR")  # 目标项目 EDITOR
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
    """下载权限 = 一条规则:是否该项目成员(或全局管理员)"""
    f = _get_file_or_404(db, file_id)
    ensure_project_role(db, ctx, f.project_id, "VIEWER")
    path = Path(f.storage_path)
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


@router.patch("/api/files/{file_id}")
def rename_file(file_id: str, payload: dict, ctx: AuthContext = Depends(require_write),
                db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    f = _get_file_or_404(db, file_id)
    ensure_project_role(db, ctx, f.project_id, "EDITOR")
    f.name = str_field(payload, "name", 255, required=True)
    db.commit()
    return {"id": f.id, "name": f.name, "mime": f.mime, "size": f.size,
            "createdAt": f.created_at.isoformat()}


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
    path = f.storage_path
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


@router.get("/api/files/zip")
def zip_files(ids: str = "", ctx: AuthContext = Depends(current_user),
              db: DbSession = Depends(get_db)):
    """多文件打包下载:GET /api/files/zip?ids=a,b,c(≤200 个,单文件流式读入 zip)"""
    id_list = [i for i in ids.split(",") if i][:200]
    if not id_list:
        bad_request("ids 不能为空")
    rows = (db.query(File).filter(File.id.in_(id_list), File.deleted_at.is_(None))
            .order_by(File.created_at.asc()).all())
    if not rows:
        err(404, "NOT_FOUND", "没有可下载的文件")
    for f in rows:
        ensure_project_role(db, ctx, f.project_id, "VIEWER")
    # 先入内存缓冲(64MB),超出自动落临时盘;生成完再流式回吐,避免边下边压的连接占用
    spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    used: dict = {}
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as z:
        for f in rows:
            path = Path(f.storage_path)
            if path.is_file():
                z.write(str(path), arcname=_zip_arcname(f.name, used))
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
