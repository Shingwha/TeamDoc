"""文档域(构建文档 §7.4):文档树 / 内容读写 / 版本历史 / 反向链接。

项目与成员在 projects.py;回收站(删除/恢复/彻底删除 + 列表)在 trash.py;
文件夹树在 files.py。此前四类混在同一个文件里(728 行),回收站流程还与
files.py 各写一份 —— 按 domain 拆开后各文件可独立演进。
"""
import os

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, bad_request, err, get_project_or_404, int_field,
                  opt_int, pat_write_guard, require_doc_role, require_project_role,
                  str_field)
from models import (Doc, DocVersion, Project, User, ancestor_names, build_tree,
                    collect_subtree, get_db, utcnow)

router = APIRouter(dependencies=[Depends(pat_write_guard)])

# 版本合并窗口(分钟):同一人在窗口内的连续保存不再新增还原点,
# 于是窗口起点的快照被保留 —— 即"这次编辑开始前的状态",一次编辑会话 = 一个还原点。
#
# 默认 0 = 不合并:保存已改为**手动**(编辑器只在你点保存时写),每次保存都是刻意动作,
# 就该各留一个可回退的点。窗口机制原本是为了压自动保存的噪音(800ms 一次能产几十个版本),
# 那个前提已经不存在了。需要稀疏历史时把它调大即可(例如回到 5)。
VERSION_MERGE_MINUTES = int(os.environ.get("VERSION_MERGE_MINUTES", "0"))

# 快照的成因(models.DocVersion.kind)。**只有两档**,因为历史里只有两种东西:
#   save    —— 保存前那一版(默认;WS 与 REST 都算同一种,传输方式不是语义)
#   restore —— 一次回退之前的现场(界面标它,因为时间线里出现异常要能解释)
# 展示用词属于界面,不落库。
VERSION_KINDS = ("save", "restore")

_DAY = 86400
_HOUR = 3600


class ContentConflict(Exception):
    """保存携带的基线版本与服务端当前版本不一致 —— 有人在这期间改过正文。

    与 ws._WsClose 同一路数:共享写入层只做判定并抛出,由两个传输层各自映射成自己的
    形状(REST → 409 + 现场数据;WS → conflict 消息)。判定、快照、保留策略都只写一遍,
    避免出现"网页端拦住了、CLI 没拦住"这种漂移。
    """

    def __init__(self, version: int, content: str, by_name: str):
        super().__init__(f"content conflict at version {version}")
        self.version = version
        self.content = content
        self.by_name = by_name


def _user_name(db: DbSession, user_id: int | None) -> str:
    """冲突现场"最后写入者"的姓名,只为把提示写成人话("张三 修改了这篇文档")。
    查不到返回空串 —— 一个装饰性字段不该让冲突判定本身失败。"""
    if not user_id:
        return ""
    u = db.get(User, user_id)
    return u.name if u else ""


def doc_brief(doc: Doc) -> dict:
    """文档简要信息(创建/更新端点共用的返回形状,此前两处手拼)"""
    return {"id": doc.id, "projectId": doc.project_id, "parentId": doc.parent_id,
            "title": doc.title, "version": doc.version,
            "createdAt": doc.created_at.isoformat(), "updatedAt": doc.updated_at.isoformat()}


@router.get("/api/projects/{project_id}/docs/tree")
def doc_tree(project_id: int, ctx: AuthContext = Depends(require_project_role("VIEWER")),
             db: DbSession = Depends(get_db)):
    docs = (db.query(Doc).filter_by(project_id=project_id).filter(Doc.deleted_at.is_(None))
            .order_by(Doc.sort.asc(), Doc.created_at.asc()).all())
    return build_tree(docs, lambda d: {"id": d.id, "title": d.title, "parentId": d.parent_id,
                                       "updatedAt": d.updated_at.isoformat(), "children": []})


@router.post("/api/projects/{project_id}/docs")
def create_doc(project_id: int, payload: dict,
               ctx: AuthContext = Depends(require_project_role("EDITOR")),
               db: DbSession = Depends(get_db)):
    get_project_or_404(db, project_id)
    title = str_field(payload, "title", 200) or "无标题文档"
    parent_id = opt_int(payload.get("parentId"))
    if parent_id is not None:
        parent = db.get(Doc, parent_id)
        if not parent or parent.project_id != project_id or parent.deleted_at is not None:
            err(404, "NOT_FOUND", "父文档不存在")
    max_sort = (db.query(func.max(Doc.sort))
                .filter_by(project_id=project_id)
                .filter(Doc.deleted_at.is_(None))
                .filter(Doc.parent_id.is_(None) if parent_id is None else Doc.parent_id == parent_id)
                .scalar())
    doc = Doc(project_id=project_id, parent_id=parent_id, title=title,
              sort=(max_sort or 0) + 1, created_by=ctx.user.id, updated_by=ctx.user.id)
    db.add(doc)
    db.commit()
    return doc_brief(doc)


@router.get("/api/docs/{doc_id}")
def get_doc(dep=Depends(require_doc_role("VIEWER")), db: DbSession = Depends(get_db)):
    _, doc = dep
    # 位置上下文:location 契约与 files.file_meta 完全同形(projectId/projectName/path),
    # 引用浮层、CLI --meta 等消费方一份代码即可展示"在哪"
    proj = db.get(Project, doc.project_id)
    return {"id": doc.id, "projectId": doc.project_id, "parentId": doc.parent_id,
            "title": doc.title, "content": doc.content, "version": doc.version,
            "location": {"projectId": doc.project_id,
                         "projectName": proj.name if proj else "",
                         "path": ancestor_names(db, Doc, doc.parent_id, "title")},
            "contentChars": len(doc.content or ""),
            "updatedAt": doc.updated_at.isoformat(), "createdAt": doc.created_at.isoformat()}


@router.get("/api/docs/{doc_id}/backlinks")
def list_backlinks(doc_id: int, dep=Depends(require_doc_role("VIEWER")),
                   db: DbSession = Depends(get_db)):
    """反向链接:同项目内正文中引用了本文档(teamdoc://doc/{pid}/{doc_id})的未删除文档"""
    _, doc = dep
    rows = (db.query(Doc).filter_by(project_id=doc.project_id)
            .filter(Doc.deleted_at.is_(None), Doc.id != doc.id,
                    Doc.content.like(f"%teamdoc://doc/%/{doc_id})%"))
            .order_by(Doc.updated_at.desc()).limit(100).all())
    return [{"id": d.id, "title": d.title, "updatedAt": d.updated_at.isoformat()} for d in rows]


@router.patch("/api/docs/{doc_id}")
def patch_doc(doc_id: int, payload: dict, dep=Depends(require_doc_role("EDITOR")),
              db: DbSession = Depends(get_db)):
    ctx, doc = dep
    if "title" in payload:
        doc.title = str_field(payload, "title", 200, required=True)
    if "parentId" in payload:
        parent_id = opt_int(payload["parentId"])
        if parent_id is not None:
            parent = db.get(Doc, parent_id)
            if not parent or parent.project_id != doc.project_id or parent.deleted_at is not None:
                err(404, "NOT_FOUND", "父文档不存在")
            if parent_id in collect_subtree(db, Doc, doc):
                err(409, "CONFLICT", "不能移动到自己的子文档下")
        doc.parent_id = parent_id
    doc.updated_by = ctx.user.id
    doc.updated_at = utcnow()
    db.commit()
    return doc_brief(doc)


def _prune_versions(db: DbSession, doc_id: int) -> int:
    """按年龄分层保留历史版本,返回删除条数。

    "只留最近 N 条"是按条数限制,而条数对应的时间跨度不可预测 —— 被频繁编辑的文档
    几周就把历史整段丢光(且删除是静默的),几乎没人动的文档却永远留着。改为按年龄分层:
    近期密、远期疏,历史不会整段消失,只是越老粒度越粗。

        1 小时内   → 全部保留
        1 天内     → 每小时留最新 1 条
        30 天内    → 每天留最新 1 条
        1 年内     → 每周留最新 1 条
        更早       → 每月留最新 1 条

    同一档内只留最新一条:从新到旧扫描,首次占用该档桶者保留。稳态上限约 124 条/文档。

    只取 (id, created_at):这个函数在**每次保存**时都会跑(含 WS 那条保存路径),
    而 content 是 Text —— 把全部版本正文读进内存只为算几个时间戳,
    在粘过大表格的高频文档上会造成明显的内存峰值。
    """
    rows = (db.query(DocVersion.id, DocVersion.created_at)
            .filter_by(doc_id=doc_id)
            .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).all())
    now = utcnow()
    seen: set = set()
    drop: list = []
    for vid, created in rows:
        age = (now - created).total_seconds()
        if age < _HOUR:
            continue  # 最近一小时全留:覆盖"刚改坏了要回滚"这一最主要场景
        if age < _DAY:
            bucket = ("h", created.strftime("%Y-%m-%d %H"))
        elif age < 30 * _DAY:
            bucket = ("d", created.strftime("%Y-%m-%d"))
        elif age < 365 * _DAY:
            bucket = ("w", created.strftime("%Y-%W"))
        else:
            bucket = ("m", created.strftime("%Y-%m"))
        if bucket in seen:
            drop.append(vid)
        else:
            seen.add(bucket)
    if drop:
        # 分批删除:一次 IN 的元素过多会撞上 SQLite 的变量数上限(SQLITE_MAX_VARIABLE_NUMBER)。
        # 稳态约 124 条不会触发,但"先高频编辑、之后长期不动再触发剪枝"时 drop 可以很大。
        for i in range(0, len(drop), 500):
            db.query(DocVersion).filter(DocVersion.id.in_(drop[i:i + 500])) \
                .delete(synchronize_session=False)
    return len(drop)


def save_doc_content(db: DbSession, doc: Doc, content: str, user_id: int,
                     kind: str = "save", *, base_version: int | None = None) -> tuple:
    """内容有变化时先把旧内容存为 DocVersion;version+=1(该字段是"保存次数",不是版本数)。

    base_version:调用方声称自己基于哪一版改的,与 doc.version 不符即抛 ContentConflict。
    **整篇覆盖的两条写入路径(REST PUT / WS content 消息)必须传** —— 这正是"两人同时
    编辑""陈旧标签页整篇盖回去"这类静默丢字的唯一出口:后写者赢之前先被拦下来问人。
    None 表示"服务端自身在本次事务内读取并写入"(append 的读-改-写、restore 的主动还原),
    它们结构上不覆盖别人的正文,或本身就是一次有"restore"快照兜底的显式覆盖;这不是
    "强制覆盖"的后门。

    kind:快照的成因(见 VERSION_KINDS),默认"save"。只有回退才需要显式传,因为它是
    唯一需要被区分的成因 —— 界面据此标出「回退前」,合并窗口也据此不把它并掉。

    还原点两条规则:
      * **空内容不留还原点**(见下方判断处):空是所有状态的缺省,不是值得回退的状态;
      * 合并窗口(VERSION_MERGE_MINUTES>0 时才生效):同一人、同一成因、窗口内的连续保存
        不再新增还原点,窗口起点的快照即为本次编辑会话的还原点。

    返回 (是否发生变化, 当前版本号)。REST 与 WebSocket 两条写入路径共用,
    避免版本策略在两处漂移。**不提交事务**,由调用方决定提交时机。
    """
    if doc.content == content:
        return False, doc.version
    if base_version is not None and base_version != doc.version:
        # 判定在任何状态改动之前:快照、version+1、剪枝都不做,服务端保持原样
        raise ContentConflict(doc.version, doc.content, _user_name(db, doc.updated_by))
    now = utcnow()
    last = (db.query(DocVersion).filter_by(doc_id=doc.id)
            .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).first())
    within_window = (VERSION_MERGE_MINUTES > 0
                     and last is not None
                     and last.kind == kind
                     and last.created_by == user_id
                     and (now - last.created_at).total_seconds() < VERSION_MERGE_MINUTES * 60)
    if not within_window and doc.content:
        # 旧内容为空则**不留下还原点**:空不是一种状态,而是所有状态的缺省(想得到
        # 空文档全选删掉即可)。留着它只会让每篇文档的历史首条点进去是一片空白 ——
        # 而真正要救的那份"清空之前的内容",由清空那次保存照常快照下来,不受影响。
        db.add(DocVersion(doc_id=doc.id, content=doc.content, kind=kind,
                          created_by=user_id, created_at=now))
    doc.content = content
    doc.version += 1
    doc.updated_by = user_id
    doc.updated_at = now
    db.flush()
    _prune_versions(db, doc.id)
    return True, doc.version


def _save_content(db: DbSession, doc: Doc, content: str, user_id: int,
                  kind: str = "save", *, base_version: int | None = None) -> dict:
    """REST 写入:调用共享快照逻辑并提交"""
    _, version = save_doc_content(db, doc, content, user_id, kind,
                                  base_version=base_version)
    db.commit()
    return {"version": version}


@router.put("/api/docs/{doc_id}/content")
def put_content(doc_id: int, payload: dict, dep=Depends(require_doc_role("EDITOR")),
                db: DbSession = Depends(get_db)):
    ctx, doc = dep
    content = payload.get("content")
    if not isinstance(content, str):
        bad_request("content 必须为字符串")
    # 基线必填只对 **web 会话**这一档:浏览器手里有加载过的缓冲,必须声明自己基于哪一版,
    # 否则陈旧标签页会整篇盖回去 —— 这是本契约唯一的防护目标。
    # PAT(CLI / 脚本 / td api)是"把文档定稿成这份内容"的程序化写入,不持有缓冲,
    # 缺省即覆盖:与 HTTP 的 If-Match 可选、S3 PUT 默认无条件同一模型。
    base_version = int_field(payload, "baseVersion")
    if base_version is None and ctx.via == "web":
        bad_request("baseVersion 不能为空")
    try:
        return _save_content(db, doc, content, ctx.user.id, base_version=base_version)
    except ContentConflict as c:
        # 冲突现场随 409 一起回:客户端要立刻拿服务端当前正文做差异对比,
        # 让它再发一次 GET 会拿到一个可能又变了的第三态
        err(409, "CONFLICT", "文档已被他人修改,本次保存未写入",
            extra={"currentVersion": c.version, "currentContent": c.content,
                   "by": c.by_name})


@router.post("/api/docs/{doc_id}/append")
def append_content(doc_id: int, payload: dict, dep=Depends(require_doc_role("EDITOR")),
                   db: DbSession = Depends(get_db)):
    ctx, doc = dep
    content = payload.get("content")
    if not isinstance(content, str):
        bad_request("content 必须为字符串")
    # 空内容分隔 \n\n(§7.4);append 是否快照旧内容文档未定义,
    # 按最简实现与 PUT 一致(先存快照,保证可回滚)
    new_content = (doc.content + "\n\n" + content) if doc.content else content
    # 不校验基线:追加的读-改-写全在服务端本次事务里完成,拼的是"当下的正文",
    # 不存在"拿着旧副本整篇盖回去"的形状 —— 并发追加也不会丢掉别人的正文
    return _save_content(db, doc, new_content, ctx.user.id, base_version=None)


@router.get("/api/docs/{doc_id}/versions")
def list_versions(doc_id: int, dep=Depends(require_doc_role("VIEWER")),
                  db: DbSession = Depends(get_db)):
    """版本列表(不含正文)。

    只 SELECT 需要的列,长度在 SQL 里算:正文是 Text,而列表要的是"哪一版"的线索
    (什么时候、谁、多大),把 100 条正文读进内存只为算字符数没有道理。
    作者名在这里批量解析,形状与文件列表一致 —— 前端是静态页,不做跨接口 join。
    """
    _, doc = dep
    rows = (db.query(DocVersion.id, DocVersion.kind, DocVersion.created_at,
                     DocVersion.created_by, func.length(DocVersion.content))
            .filter_by(doc_id=doc.id)
            .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).limit(100).all())
    author_ids = {r[3] for r in rows if r[3]}
    authors = ({u.id: u.name for u in db.query(User).filter(User.id.in_(author_ids)).all()}
               if author_ids else {})
    return [{"id": r[0], "kind": r[1], "createdAt": r[2].isoformat(),
             "contentChars": r[4] or 0,
             "createdBy": ({"id": r[3], "name": authors[r[3]]} if r[3] in authors else None)}
            for r in rows]


@router.get("/api/docs/{doc_id}/versions/{vid}")
def get_version(doc_id: int, vid: int, dep=Depends(require_doc_role("VIEWER")),
                db: DbSession = Depends(get_db)):
    _, doc = dep
    v = db.query(DocVersion).filter_by(id=vid, doc_id=doc.id).first()
    if not v:
        err(404, "NOT_FOUND", "版本不存在")
    return {"content": v.content}


@router.post("/api/docs/{doc_id}/versions/{vid}/restore")
def restore_version(doc_id: int, vid: int, dep=Depends(require_doc_role("EDITOR")),
                    db: DbSession = Depends(get_db)):
    ctx, doc = dep
    v = db.query(DocVersion).filter_by(id=vid, doc_id=doc.id).first()
    if not v:
        err(404, "NOT_FOUND", "版本不存在")
    # 等价于对该版本内容执行 PUT content(§7.4)。
    # kind 用"restore":它标记的是"回退之前的现场",是唯一需要被区分的成因 ——
    # 合并窗口不会把它并掉,界面据此标出「回退前」。
    # 不校验基线:还原是用户看着历史版本做出的显式覆盖决定,且现场已被存成快照 ——
    # 拦下来问人反而使"我想回到那一版"这个明确意图没法完成
    return _save_content(db, doc, v.content, ctx.user.id, kind="restore",
                         base_version=None)
