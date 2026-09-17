"""云空间:上传 / 下载 / 文件夹 / 项目间移动(构建文档 §7.5,归属制)。

一切文件/文件夹都归属项目(个人空间 = 本人的 is_personal 项目),权限统一走项目角色
(读 VIEWER / 写 EDITOR)。删除 = 软删除进项目回收站(可恢复);彻底删除才清记录与
物理文件。
"""
import secrets
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

import config
import refs

from auth import (AuthContext, clamp_int, current_user, ensure_move_allowed,
                  ensure_project_role, get_project_or_404, id_csv_field, id_field,
                  load_live, opt_id, opt_int, pat_write_guard, query_id,
                  require_project_role, require_role, str_field, user_map,
                  validate_parent)
from errors import CONFLICT, NOT_FOUND, VALIDATION, bad_request, err
from ids import IdPath
from media import can_inline, guess_mime
from models import (FILES_DIR, INFLIGHT_STORAGE, Doc, File, Folder, build_tree,
                    collect_subtree, disk_free_bytes, disk_total_bytes, file_abspath,
                    get_db,
                    location_json, release_db, unlink_quiet, utcnow)
from serialize import file_json, folder_json, iso

router = APIRouter(dependencies=[Depends(pat_write_guard)])

# 单文件上传上限(默认 20GB,见 config.py)。定得高是有意的 —— 内网常见设计源文件与
# 素材压缩包动辄十几 GB。真正兜底的不是这个数字,而是两道磁盘守卫:上传前的余量预检
# + 写盘时每 16MB 复查(见 _check_reserve 与 upload_file),所以调大上限不会让磁盘
# 失去保护。注意:反代(client_max_body_size)必须 ≥ 本值,否则大包在反代层就被拦成
# 413,应用收不到请求、也就给不出"上限 X MB"这句明确提示(见 DEPLOY.md §1.5)。
MAX_UPLOAD_MB = config.MAX_UPLOAD_MB
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
CHUNK = 1024 * 1024  # 流式写盘,逐 1MB 块(§7.5)
STORAGE_RESERVE_MB = config.STORAGE_RESERVE_MB

# 编辑器上传的附件统一落在这个目录(项目根下)。目录名是**服务端的约定**:
# 客户端只说"我这是某篇文档的附件",定位与创建都由服务端做(见 upload_file 的 docId 分支)。
ATTACH_FOLDER_NAME = "文档附件"


def _attach_folder(db: DbSession, project_id: str) -> str:
    """取(必要时建)项目根下的「文档附件」目录,返回其 id。

    只在文档附件上传时使用。同名目录在回收站里不算数(那是一个已删除的目录)。
    """
    folder = (db.query(Folder)
              .filter_by(project_id=project_id, parent_id=None, name=ATTACH_FOLDER_NAME)
              .filter(Folder.deleted_at.is_(None)).first())
    if folder is None:
        folder = Folder(name=ATTACH_FOLDER_NAME, project_id=project_id, parent_id=None)
        db.add(folder)
        db.flush()   # 拿 id(上传随后要用它作为 folder_id)
    return folder.id


def ensure_file_access(db: DbSession, ctx: AuthContext, f: File):
    """登录态下载 / 读元数据的鉴权:项目角色(成员或全局管理员),不足则 403。

    "发给不在项目里的人"由**分享链接**(/api/share/{token})承担,与这里无关:
    那条通道不带会话,凭的是链接里的秘密。拒绝的文案与错误码只走
    ensure_project_role 一处(FORBIDDEN / JOIN_REQUIRED)。
    """
    ensure_project_role(db, ctx, f.project_id, "VIEWER")


# 排序字段白名单:值直接映射到列,避免把用户输入拼进 order_by。
# 取并集做校验(文件夹没有 size,落到它时按 name 排),所以 sort 的合法取值只有这三个。
_FILE_SORTS = {"name": File.name, "time": File.created_at, "size": File.size}
_FOLDER_SORTS = {"name": Folder.name, "time": Folder.created_at}
_SORTS = tuple(sorted(set(_FILE_SORTS) | set(_FOLDER_SORTS)))
_DIRECTIONS = ("asc", "desc")


@router.get("/api/files")
def list_files(project_id: str = "", folder_id: str = "",
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
    project_id = query_id(project_id, "project_id", required=True)
    folder_id = query_id(folder_id, "folderId")
    # 项目 id 来自查询串(不是路径参数),所以存在性与角色在这里显式判一次
    get_project_or_404(db, project_id)
    ensure_project_role(db, ctx, project_id, "VIEWER")
    if folder_id:
        validate_parent(db, Folder, folder_id, project_id, label="父文件夹")
    if sort not in _SORTS:
        bad_request(f"sort 必须为 {'/'.join(_SORTS)}")
    if dir not in _DIRECTIONS:
        bad_request(f"dir 必须为 {'/'.join(_DIRECTIONS)}")
    limit = clamp_int(limit, 1, 500, "limit")
    # offset 上界取 int32 级别:远超任何真实数据量,又不至于碰到 SQLite 的整型边界
    # (再大就会在绑定参数时抛 OverflowError,冒泡成 500)
    offset = clamp_int(offset, 0, 2 ** 31 - 1, "offset")
    descending = dir == "desc"
    fcol = _FOLDER_SORTS.get(sort, Folder.name)
    icol = _FILE_SORTS.get(sort, File.name)

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
    # 弱提示:正文引用了这个文件的文档(单查询 + 正则提取,30 人规模足够)。
    # 两种契约形态都算,判定在 refs.py(与反链、移动前检查同源)。
    # **跨项目**:移动文件不会改写正文里的引用,所以"谁在引用它"必须全库问,
    # 只看本项目会漏掉搬迁来源——那正是最该提醒的场景。
    page_ids = {f.id for f in files}
    referenced_ids: set[str] = set()
    if page_ids:
        for (content,) in db.query(Doc.content).filter(
                Doc.deleted_at.is_(None), refs.sql_prefilter(Doc.content)).all():
            referenced_ids |= refs.file_ids_in(content) & page_ids
    creators = user_map(db, (f.created_by for f in files))
    return {
        "folders": [folder_json(f) for f in folders],
        "files": [file_json(f, face="list", referenced=f.id in referenced_ids,
                            created_by=creators.get(f.created_by))
                  for f in files],
        # total 是截断前的计数(文件夹受 2000 上限,文件为真实总数)
        "total": {"folders": folder_total, "files": file_total},
        "offset": offset, "limit": limit,
        "hasMore": offset + len(files) < file_total,
    }


@router.get("/api/projects/{project_id}/storage")
def project_storage(project_id: IdPath, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                    db: DbSession = Depends(get_db)):
    """项目占用统计。

    **活跃与回收站必须分列**:回收站里的文件仍占物理磁盘(只有彻底删除才 unlink),
    混成一个数会出现"我删了文件,占用怎么没变"的困惑。前端在云空间工具栏与项目
    设置页展示这两个数。
    """
    totals = file_storage_totals(db, project_id)
    active_bytes, active_count = totals["active"]
    trash_bytes, trash_count = totals["trash"]
    folder_count = (db.query(Folder).filter_by(project_id=project_id)
                    .filter(Folder.deleted_at.is_(None)).count())
    doc_bytes = db.query(func.coalesce(func.sum(func.length(Doc.content)), 0)) \
        .filter_by(project_id=project_id).filter(Doc.deleted_at.is_(None)).scalar() or 0
    return {
        "active": {"bytes": active_bytes or 0, "fileCount": active_count,
                   "folderCount": folder_count},
        "trash": {"bytes": trash_bytes or 0, "fileCount": trash_count},
        "docs": {"bytes": doc_bytes},
        # 磁盘余量是给用户的"还能传多少"信号,比任何配额都直观
        "disk": {"total": disk_total_bytes(), "free": disk_free_bytes()},
    }


def dedupe_name(existing: set, name: str) -> str:
    """在已占用名集合里取一个不冲突的名字:foo.png → foo(2).png → foo(3).png。
    上传自动加后缀与 zip 内去重共用;返回值不自动加入集合,调用方自行 add。"""
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
        err(409, CONFLICT, f"同目录下已有同名{kind}「{name}」,请换一个名称")
    if name in existing_dirs:
        err(409, CONFLICT, f"同目录下已有同名文件夹「{name}」,请换一个名称")


@router.post("/api/files/folders")
def create_folder(payload: dict, ctx: AuthContext = Depends(current_user),
                  db: DbSession = Depends(get_db)):
    name = str_field(payload, "name", 100, required=True)
    project_id = id_field(payload, "projectId", required=True)
    parent_id = opt_id(payload.get("parentId"))
    get_project_or_404(db, project_id)
    ensure_project_role(db, ctx, project_id, "EDITOR")
    if parent_id:
        validate_parent(db, Folder, parent_id, project_id, label="父文件夹")
    file_names, dir_names = _names_in_folder(db, project_id, parent_id)
    _user_name_conflict(file_names, dir_names, name, "文件夹")
    folder = Folder(name=name, project_id=project_id, parent_id=parent_id)
    db.add(folder)
    db.commit()
    return folder_json(folder)


@router.patch("/api/files/folders/{folder_id}")
def rename_folder(folder_id: IdPath, payload: dict,
                  dep=Depends(require_role(Folder, "文件夹", "EDITOR", path_param="folder_id")),
                  db: DbSession = Depends(get_db)):
    _, folder = dep
    name = str_field(payload, "name", 100, required=True)
    if name != folder.name:
        file_names, dir_names = _names_in_folder(db, folder.project_id, folder.parent_id,
                                                 exclude_folder=folder.id)
        _user_name_conflict(file_names, dir_names, name, "文件夹")
    folder.name = name
    db.commit()
    return folder_json(folder)



@router.post("/api/files/folders/{folder_id}/move")
def move_folder(folder_id: IdPath, payload: dict,
                dep=Depends(require_role(Folder, "文件夹", "EDITOR", path_param="folder_id")),
                db: DbSession = Depends(get_db)):
    """移动文件夹(项目内整理 / 跨项目转移)。

    权限与父级判定都在 auth(ensure_move_allowed / validate_parent)—— 文档、文件夹、
    文件三种资源共用同一份,差别只在"改哪些表"。

    跨项目时会一并改写**整棵子树**的 project_id —— 文件夹与文件都按 project_id
    归属,只改顶层会让子项留在原项目,形成跨项目的悬挂结构。
    """
    ctx, folder = dep
    target_id = id_field(payload, "projectId", required=True)
    src_id = folder.project_id
    ensure_move_allowed(db, ctx, src_id, target_id)
    ids = collect_subtree(db, Folder, folder)
    parent_id = opt_id(payload.get("parentId"))
    validate_parent(db, Folder, parent_id, target_id, moving=folder, label="父文件夹")
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



def _reserve_breached(extra_needed: int = 0) -> bool:
    """余量是否已低于保留线(纯判定,报错文案由调用点决定)。"""
    return disk_free_bytes() - extra_needed < STORAGE_RESERVE_MB * 1024 * 1024


def _check_reserve(extra_needed: int = 0):
    """磁盘余量守卫的报错出口:余量将低于 STORAGE_RESERVE_MB 时拒绝上传。

    在写盘前与写盘循环中各调用一次 —— 只检查开头的话,一个超大文件仍能把盘写满
    (表现为半截文件 + 500,且失败路径的清理本身也可能因为没空间而失败)。
    """
    if _reserve_breached(extra_needed):
        err(400, VALIDATION, f"服务器存储空间不足(需保留 {STORAGE_RESERVE_MB}MB 余量),请联系管理员清理")


async def save_request_body(request: Request, path, max_bytes: int, *,
                             on_progress=None) -> tuple:
    """请求体流式落盘(raw body)。返回 (状态, 已写字节数);状态:
    ok / too_large(超上限,半成品已删)/ empty(空体,半成品已删)/
    aborted(on_progress 主动中止,如磁盘余量不足,半成品已删)/
    interrupted(客户端断开或写盘错误,半成品已删)。

    文件上传与恢复包上传共用;错误文案由调用方决定(面向的场景不同)。
    on_progress(total) 每写一块后调用,返回 False 表示中止(磁盘余量不足等),
    同样清理半成品。
    """
    total = 0
    try:
        with open(path, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    unlink_quiet(path)
                    return "too_large", total
                out.write(chunk)
                if on_progress is not None and not on_progress(total):
                    unlink_quiet(path)
                    return "aborted", total
    except Exception:
        unlink_quiet(path)
        return "interrupted", total
    if total == 0:
        # 空文件是合法的,但"一个字节都没收到"通常意味着请求写错了(比如忘了带 body),
        # 静默存成 0 字节文件会让人以为文件内容丢了
        unlink_quiet(path)
        return "empty", 0
    return "ok", total


def raise_for_body_status(status: str, *, kind: str = "文件") -> None:
    """save_request_body 的状态 → HTTP 错误唯一映射(文件上传与备份恢复共用)。

    状态面在 save_request_body docstring 单点声明;aborted 只可能由上传产生
    (恢复不传 on_progress)。kind 只进文案('文件'/'备份'),两场景措辞随它走。"""
    if status == "too_large":
        err(400, VALIDATION, f"{kind}过大(上限 {MAX_UPLOAD_MB}MB)")
    if status == "aborted":
        err(400, VALIDATION,
            f"服务器存储空间不足(需保留 {STORAGE_RESERVE_MB}MB 余量),请联系管理员清理")
    if status == "interrupted":
        err(400, VALIDATION, "上传中断,请重试")
    if status == "empty":
        err(400, VALIDATION, f"请求体为空,未收到{kind}内容")


@router.post("/api/files/upload")
async def upload_file(request: Request,
                      projectId: str = "", folderId: str = "", docId: str = "",
                      name: str = "",
                      ctx: AuthContext = Depends(current_user),
                      db: DbSession = Depends(get_db)):
    """上传文件:请求体就是文件本身(raw body 流式写盘)。

    为什么不用 multipart:Starlette 的 multipart 解析器会把 >1MB 的 part 先落到
    **系统临时目录**(spool_max_size=1MB),handler 再拷进 data/files —— 传 2GB
    要 4GB 空闲且 IO 翻倍。raw body 没有这个双写,`xhr.upload.onprogress` 与
    Content-Length 照常可用,CLI 直传也更顺。

    代价:端点不再声明 Form/UploadFile,Swagger 里不好点(参数在 query string 上)。
    这是全项目唯一一处非标准上传写法:元数据走 query、请求体即文件字节。

    两种入口,二者取一:
    - `projectId`(+可选 `folderId`)—— 云空间里"传到当前目录";
    - `docId` —— 编辑器的附件上传。**项目与目录都由服务端按文档当前归属推导**
      (文档可能刚被移到别的项目,前端手里的 projectId 已经过期;而"文档附件"这个
      目录名是服务端的约定,不该让每个客户端各自按名字去猜。
    """
    if docId:
        doc = db.get(Doc, docId)
        if not doc or doc.deleted_at is not None:
            err(404, NOT_FOUND, "文档不存在")
        projectId, folderId = doc.project_id, _attach_folder(db, doc.project_id)
        get_project_or_404(db, projectId)
        ensure_project_role(db, ctx, projectId, "EDITOR")
    else:
        projectId = query_id(projectId, "projectId", required=True)
        folderId = query_id(folderId, "folderId")
        get_project_or_404(db, projectId)
        ensure_project_role(db, ctx, projectId, "EDITOR")
        if folderId:
            validate_parent(db, Folder, folderId, projectId, label="父文件夹")
    display_name = (name or "").strip()[:255] or "未命名文件"
    # 声明了长度就做一次快速预检,避免收完大文件才发现空间不足
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared:
        if declared > MAX_UPLOAD_BYTES:
            err(400, VALIDATION, f"文件过大(上限 {MAX_UPLOAD_MB}MB)")
        if declared > disk_free_bytes():
            err(400, VALIDATION, "服务器存储空间不足,请联系管理员清理")
    _check_reserve()
    # 同目录重名自动加后缀(foo.png → foo(2).png):上传是"把文件拖进来",
    # 用户没有"为它起名"的动作,为此弹一个报错再让他改名的体验更差
    file_names, _dirs = _names_in_folder(db, projectId, folderId)
    display_name = dedupe_name(file_names, display_name)
    # 物理文件名与行 id 解耦:行 id 是标识,物理名是不可猜的随机串(也是
    # storage_path 的值;解析统一走 models.file_abspath)。类型不存库 ——
    # media.guess_mime 是唯一判定,读时按名字算(见 serialize.file_json)。
    storage_name = secrets.token_hex(16)
    rec = File(name=display_name,
               project_id=projectId, folder_id=folderId,
               created_by=ctx.user.id,
               storage_path=storage_name)
    path = FILES_DIR / storage_name
    # 预检已完成,收 body 前结束事务:整个上传期间(可能几小时)不需要数据库。
    # 末尾 db.add/commit 会自动重新开一条连接,那才是真正需要库的一瞬间。
    release_db(db)
    # 全程登记在传文件名:这期间它在库里还没有记录,孤儿清理必须跳过(见 models.INFLIGHT_STORAGE)
    INFLIGHT_STORAGE.add(storage_name)

    def _reserve_ok(total):
        # 每 16MB 复查一次余量,避免逐块 stat 的开销
        return not (total % (16 * CHUNK) < CHUNK and _reserve_breached())

    try:
        status, total = await save_request_body(request, path, MAX_UPLOAD_BYTES,
                                                on_progress=_reserve_ok)
        raise_for_body_status(status)
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
    finally:
        INFLIGHT_STORAGE.discard(storage_name)
    return file_json(rec)


@router.get("/api/projects/{project_id}/folders/tree")
def folder_tree(project_id: IdPath, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                db: DbSession = Depends(get_db)):
    """项目文件夹树(未删除)。

    一次调用解决三处需要"文件夹层级"的地方:移动目标选择器、面包屑、上传目录时的
    路径缓存 —— 父链由服务端给出,前端不必自己在会话里维护(刷新就会丢)。
    返回嵌套结构,便于直接渲染树。
    """
    folders = (db.query(Folder).filter_by(project_id=project_id)
               .filter(Folder.deleted_at.is_(None))
               .order_by(Folder.name.asc()).limit(2000).all())
    return build_tree(folders, lambda f: folder_json(f, face="tree"))


@router.post("/api/files/{file_id}/move")
def move_file(file_id: IdPath, payload: dict,
              dep=Depends(require_role(File, "文件", "EDITOR", path_param="file_id")),
              db: DbSession = Depends(get_db)):
    """移动文件:项目内整理只需 EDITOR;跨项目需源项目 ADMIN + 目标项目 EDITOR。

    只改记录字段,物理文件不动。
    """
    ctx, f = dep
    target_id = id_field(payload, "projectId", required=True)
    folder_id = opt_id(payload.get("folderId"))
    src_id = f.project_id
    ensure_move_allowed(db, ctx, src_id, target_id)
    validate_parent(db, Folder, folder_id, target_id, label="目标文件夹")
    f.project_id = target_id
    f.folder_id = folder_id
    db.commit()
    return {"id": f.id, "projectId": f.project_id, "folderId": f.folder_id}


@router.get("/api/files/{file_id}/download")
def download_file(file_id: IdPath, inline: str = "",
                  ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    """下载权限:项目成员或全局管理员(匿名分享走 /api/share/{token})"""
    f = load_live(db, File, file_id, "文件")
    ensure_file_access(db, ctx, f)
    return _file_response(db, f, inline)


def _file_response(db: DbSession, f: File, inline: str):
    """把一份文件回吐成响应(登录下载与匿名分享下载共用)。

    inline 只对白名单类型生效:svg/html 等可执行格式强制 attachment,否则上传者能借
    "预览"在服务端同源下执行脚本(存储型 XSS)。分享链接走的是同一段判定 ——
    两条下载通道的差别只在"怎么获得权限",不在"怎么回吐"。
    """
    path = file_abspath(f.storage_path)
    if not path.is_file():
        err(404, NOT_FOUND, "文件内容不存在")
    mime = guess_mime(f.name)
    if inline == "1" and can_inline(mime):
        disposition, media = "inline", mime
    else:
        disposition = "attachment"
        # 未知/危险类型回吐为二进制流,避免浏览器按扩展名嗅探后当页面渲染
        media = mime if can_inline(mime) else "application/octet-stream"
    cd = f"{disposition}; filename*=UTF-8''{quote(f.name)}"
    # 需要的数据已取成局部变量,结束事务:这条请求后面只剩文件传输,连接不参与
    release_db(db)
    return FileResponse(str(path), media_type=media,
                        headers={"Content-Disposition": cd,
                                 "X-Content-Type-Options": "nosniff"})


@router.get("/api/share/{token}")
def shared_download(token: str, inline: str = "", db: DbSession = Depends(get_db)):
    """匿名下载分享文件:token 即凭据,不需要登录。

    - token 是**秘密**:不对它做形状校验之外的任何"提示"(不存在/已吊销/已过期一律 404,
      不区分 —— 区分等于告诉猜的人"这个 token 存在过");
    - 文件被删除(进回收站)或彻底删除 → 同样 404;
    - 分享链接不暴露文件 id:链接里只有 token,清空 token 就是真的断链。
    """
    f = db.query(File).filter(File.share_token == token).first() if token else None
    if not f or f.deleted_at is not None:
        err(404, NOT_FOUND, "分享链接无效")
    if f.share_expires_at is not None and f.share_expires_at < utcnow():
        err(404, NOT_FOUND, "分享链接已过期")
    return _file_response(db, f, inline)


@router.post("/api/files/{file_id}/share")
def share_file(file_id: IdPath, payload: dict,
               dep=Depends(require_role(File, "文件", "EDITOR", path_param="file_id")),
               db: DbSession = Depends(get_db)):
    """建立分享链接(payload.expireDays 可选,缺省/0 = 不过期)。

    已分享的文件再次调用 = **换一个新 token**,旧链接立刻失效(重新分享的语义是
    "只让新的这一条有效",而不是"在旧链接上延期" —— 后者没法收回已经扩散出去的链接)。
    """
    _, f = dep
    days = opt_int(payload.get("expireDays"))
    if days is not None and days < 0:
        bad_request("expireDays 不能为负")
    f.share_token = secrets.token_urlsafe(32)
    f.share_expires_at = utcnow() + timedelta(days=days) if days else None
    db.commit()
    return {"token": f.share_token, "url": f"/api/share/{f.share_token}",
            "expiresAt": iso(f.share_expires_at)}


@router.delete("/api/files/{file_id}/share")
def unshare_file(file_id: IdPath, dep=Depends(require_role(File, "文件", "EDITOR", path_param="file_id")),
                 db: DbSession = Depends(get_db)):
    """吊销分享链接:清掉 token,已发出的链接立刻失效(幂等:没分享过也返回成功)。"""
    _, f = dep
    f.share_token = None
    f.share_expires_at = None
    db.commit()
    return {"ok": True}


@router.get("/api/files/{file_id}/meta")
def file_meta(file_id: IdPath,
              ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    """单文件元数据:文档里 @文件 引用点击后,前端定位项目/文件夹并渲染预览浮层。

    可见性与 download 完全同口径(get_file + ensure_file_access);
    回收站中的文件 404 —— 引用一个已删除的文件就当它不存在,与 download 一致。

    **只有这里带 shareUrl**:分享 token 是凭据,列表/搜索那种批量返回不该捎带它
    (file_json 只给一个 shared 布尔,徽标够用)。
    """
    f = load_live(db, File, file_id, "文件")
    ensure_file_access(db, ctx, f)
    # 位置上下文:location 契约由 models.location_json 单点构造(与 get_doc 同形)
    return file_json(f, face="meta",
                     location=location_json(db, f.project_id, Folder, f.folder_id, "name"))


@router.patch("/api/files/{file_id}")
def rename_file(file_id: IdPath, payload: dict,
                dep=Depends(require_role(File, "文件", "EDITOR", path_param="file_id")),
                db: DbSession = Depends(get_db)):
    """改名(分享状态走 /api/files/{id}/share,不在这里改)"""
    _, f = dep
    if "name" in payload:
        name = str_field(payload, "name", 255, required=True)
        if name != f.name:
            # 用户显式改名:重名直接报错(不静默加后缀,否则名字会被悄悄改掉)
            file_names, dir_names = _names_in_folder(db, f.project_id, f.folder_id, exclude_file=f.id)
            _user_name_conflict(file_names, dir_names, name, "文件")
        f.name = name
        # 类型不落库:改名改了扩展名,类型自然跟着变(读时按名字推导)
    db.commit()
    return file_json(f)



def chunked_file(fh, chunk: int = CHUNK):
    """按块读取已打开的文件对象,供 StreamingResponse 回吐。
    zip 打包与备份下载共用;finally 语义(关 spool/删暂存)由各自的生成器包裹。"""
    while True:
        data = fh.read(chunk)
        if not data:
            return
        yield data


ZIP_MAX_FILES = 1000  # 一次打包的文件数上限(含展开的文件夹内容)
ZIP_MAX_FOLDERS = 200  # 一次打包的**选择项**里文件夹数上限(一个文件夹可能展开成很多文件)
# 一次打包的**总字节**上限:只限文件数挡不住"1000 个超大文件"。取 4GB ——
# 远超内网日常批量下载,又不足以在压缩过程中把数据盘/系统盘写满。
ZIP_MAX_BYTES = config.ZIP_MAX_MB * 1024 * 1024


def _safe_seg(name: str) -> str:
    """zip 路径段清理:去掉路径分隔符与上跳,避免压缩包内的路径穿越"""
    return name.replace("/", "_").replace("\\", "_").replace("..", "_").strip() or "未命名"


def _ensure_zip_count(n: int) -> None:
    """打包文件数上限的唯一判定(展开中与展开后共用同一阈值与文案)。"""
    if n > ZIP_MAX_FILES:
        err(400, VALIDATION, f"一次最多打包 {ZIP_MAX_FILES} 个文件(含文件夹展开),请减少选择")


def file_storage_totals(db: DbSession, project_id: str | None = None) -> dict:
    """文件存储按 活跃/回收站 拆段统计(项目占用与全站总览共用同一口径,
    回收站里的文件仍占物理磁盘,必须分列)。返回
    {"active": (bytes, count), "trash": (bytes, count)};project_id 为 None 时统计全站。"""
    base = db.query(func.coalesce(func.sum(File.size), 0), func.count(File.id))
    if project_id is not None:
        base = base.filter(File.project_id == project_id)
    active = base.filter(File.deleted_at.is_(None)).one()
    trash = base.filter(File.deleted_at.isnot(None)).one()
    return {"active": (active[0] or 0, active[1]), "trash": (trash[0] or 0, trash[1])}


def _collect_zip_targets(db: DbSession, ctx: AuthContext, file_ids: list[str],
                         folder_ids: list[str]) -> list[tuple[str, File]]:
    """把"勾选的文件 + 勾选的文件夹(递归)"展开成 (zip 内相对路径, File) 列表。

    文件夹会带出**相对所选根目录的目录结构** —— 勾选一个子目录就能连同它的层级
    一起打包,不必手动全选文件。
    """
    wanted: list[tuple[str, File]] = []  # (前缀路径, 文件)
    # 1) 散选的文件:落在 zip 根
    for f in db.query(File).filter(File.id.in_(file_ids), File.deleted_at.is_(None)).all():
        ensure_project_role(db, ctx, f.project_id, "VIEWER")
        wanted.append(("", f))
    # 2) 勾选的文件夹:各自递归取子树,前缀 = 该文件夹名
    for fid in folder_ids:
        folder = load_live(db, Folder, fid, "文件夹")
        ensure_project_role(db, ctx, folder.project_id, "VIEWER")
        ids = collect_subtree(db, Folder, folder)
        folders = {x.id: x for x in db.query(Folder).filter(Folder.id.in_(ids)).all()}
        # 建"文件夹 id → 相对该根目录的路径"
        rel: dict[str, str] = {folder.id: _safe_seg(folder.name)}
        # 自顶向下推路径:按层级展开,父在前保证父路径已就绪。
        # 用"父 id → 子列表"索引而不是每层扫全部文件夹(后者是 O(n²),
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
        # 展开后立即检查上限:若等全部收集完才判,传入少量文件夹 id 就能让服务端
        # 展开并查询巨量行(拥有大目录的成员可据此造成资源消耗)
        _ensure_zip_count(len(wanted))
    return wanted


@router.get("/api/files/zip")
def zip_files(ids: str = "", folderIds: str = "", ctx: AuthContext = Depends(current_user),
              db: DbSession = Depends(get_db)):
    """打包下载:GET /api/files/zip?ids=a,b&folderIds=c,d

    文件夹会递归展开并保留目录结构(见 _collect_zip_targets)。文件总数上限
    ZIP_MAX_FILES,超限直接拒绝 —— 比默默截断好:用户拿到一个不完整的包却以为
    是全部,这在"下载备份"场景下是危险的。
    """
    file_ids = id_csv_field(ids, "ids")
    folder_ids = id_csv_field(folderIds, "folderIds")
    if not file_ids and not folder_ids:
        bad_request("ids 与 folderIds 不能同时为空")
    # 超限在**收集之前**就拒绝:先截断再校验等于静默给一个残缺的包(与本函数约定矛盾)
    if len(file_ids) > ZIP_MAX_FILES or len(folder_ids) > ZIP_MAX_FOLDERS:
        err(400, VALIDATION,
            f"一次最多打包 {ZIP_MAX_FILES} 个文件 / {ZIP_MAX_FOLDERS} 个文件夹,请减少选择")
    targets = _collect_zip_targets(db, ctx, file_ids, folder_ids)
    if not targets:
        err(404, NOT_FOUND, "没有可下载的文件")
    _ensure_zip_count(len(targets))
    # 压缩前先按**声明大小**预检总量:文件数上限挡不住"1000 个 20GB 的文件"。
    # zip 是边压边写 spool(超 64MB 落系统盘),没有这道预检就可能把磁盘写满,
    # 且失败发生在压缩中途、用户只看到一个中断的下载。
    declared = sum(f.size or 0 for _prefix, f in targets)
    if declared > ZIP_MAX_BYTES:
        err(400, VALIDATION,
            f"所选文件合计 {declared // (1024 * 1024)}MB,超过单次打包上限 "
            f"{ZIP_MAX_BYTES // (1024 * 1024)}MB,请分批下载")
    # 物化成纯数据(前缀/路径/文件名)后结束事务:压缩与回吐可能持续几十分钟,
    # 期间不需要数据库,更不能把记录对象带出会话边界
    entries = [(prefix, str(file_abspath(f.storage_path)), _safe_seg(f.name))
               for prefix, f in targets]
    release_db(db)
    # 先入内存缓冲(64MB),超出自动落临时盘;生成完再流式回吐,避免边下边压的连接占用
    spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    used: set = set()
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as z:
        for prefix, stored, base in entries:
            path = Path(stored)
            if not path.is_file():
                continue  # 记录在但物理文件丢了:跳过,不让整包失败
            # 对"含目录的完整路径"去重:a/b.png → a/b(2).png
            arc = dedupe_name(used, (prefix + "/" + base) if prefix else base)
            used.add(arc)
            z.write(stored, arcname=arc)
    spool.seek(0, 2)
    total = spool.tell()  # 带上 Content-Length:否则 chunked 下载浏览器无法显示进度/及时完成
    spool.seek(0)

    def gen():
        try:
            yield from chunked_file(spool)
        finally:
            spool.close()

    cd = "attachment; filename*=UTF-8''" + quote("文件打包.zip")
    return StreamingResponse(gen(), media_type="application/zip",
                             headers={"Content-Disposition": cd, "Content-Length": str(total)})
