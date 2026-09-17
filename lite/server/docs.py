"""文档域(构建文档 §7.4):文档树 / 内容读写 / 版本历史 / 反向链接。

项目与成员在 projects.py;回收站(删除/恢复/彻底删除 + 列表)在 trash.py;
文件夹树在 files.py。此前四类混在同一个文件里(728 行),回收站流程还与
files.py 各写一份 —— 按 domain 拆开后各文件可独立演进。
"""
import os

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

import refs

from auth import (AuthContext, bad_request, ensure_move_allowed, err,
                  get_project_or_404, id_field, int_field, opt_id, pat_write_guard,
                  require_doc_role, require_project_role, str_field, user_names,
                  validate_parent)
from ids import IdPath, normalize_id
from models import (Doc, DocVersion, File, Project, User, build_tree,
                    collect_subtree, get_db, location_json, next_sort, utcnow)
from projects import visible_project_ids

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


def _user_name(db: DbSession, user_id: str | None) -> str:
    """冲突现场"最后写入者"的姓名,只为把提示写成人话("张三 修改了这篇文档")。
    查不到返回空串 —— 一个装饰性字段不该让冲突判定本身失败。"""
    if not user_id:
        return ""
    u = db.get(User, user_id)
    return u.name if u else ""


def str_content(payload: dict) -> str | None:
    """正文字段校验的共用谓词(REST PUT/append 与 WS content 消息同判):
    非字符串返回 None —— 失败语义各归调用点(REST 400,WS 静默忽略并记 WARNING)。"""
    content = payload.get("content")
    return content if isinstance(content, str) else None


def doc_brief(doc: Doc) -> dict:
    """文档简要信息(创建/更新端点共用的返回形状,此前两处手拼)"""
    return {"id": doc.id, "projectId": doc.project_id, "parentId": doc.parent_id,
            "title": doc.title, "version": doc.version,
            "createdAt": doc.created_at.isoformat(), "updatedAt": doc.updated_at.isoformat()}


@router.get("/api/projects/{project_id}/docs/tree")
def doc_tree(project_id: IdPath, ctx: AuthContext = Depends(require_project_role("VIEWER")),
             db: DbSession = Depends(get_db)):
    docs = (db.query(Doc).filter_by(project_id=project_id).filter(Doc.deleted_at.is_(None))
            .order_by(Doc.sort.asc(), Doc.id.asc()).all())
    return build_tree(docs, lambda d: {"id": d.id, "title": d.title, "parentId": d.parent_id,
                                       "updatedAt": d.updated_at.isoformat(), "children": []})


@router.post("/api/projects/{project_id}/docs")
def create_doc(project_id: IdPath, payload: dict,
               ctx: AuthContext = Depends(require_project_role("EDITOR")),
               db: DbSession = Depends(get_db)):
    get_project_or_404(db, project_id)
    title = str_field(payload, "title", 200) or "无标题文档"
    parent_id = opt_id(payload.get("parentId"))
    validate_parent(db, Doc, parent_id, project_id)
    doc = Doc(project_id=project_id, parent_id=parent_id, title=title,
              sort=next_sort(db, Doc, project_id, parent_id),
              created_by=ctx.user.id, updated_by=ctx.user.id)
    db.add(doc)
    db.commit()
    return doc_brief(doc)


@router.get("/api/docs/{doc_id}")
def get_doc(dep=Depends(require_doc_role("VIEWER")), db: DbSession = Depends(get_db)):
    _, doc = dep
    # 位置上下文:location 契约由 models.location_json 单点构造(与 file_meta 同形),
    # 引用浮层、CLI --meta 等消费方一份代码即可展示"在哪"
    return {"id": doc.id, "projectId": doc.project_id, "parentId": doc.parent_id,
            "title": doc.title, "content": doc.content, "version": doc.version,
            "location": location_json(db, doc.project_id, Doc, doc.parent_id, "title"),
            "contentChars": len(doc.content or ""),
            "updatedAt": doc.updated_at.isoformat(), "createdAt": doc.created_at.isoformat()}


@router.get("/api/docs/{doc_id}/backlinks")
def list_backlinks(doc_id: IdPath, dep=Depends(require_doc_role("VIEWER")),
                   db: DbSession = Depends(get_db)):
    """反向链接:正文里引用了本文档的**未删除**文档。

    **跨项目**,并按可见性过滤:引用是 `teamdoc://doc/{id}`,不含项目归属,
    所以引你的文档可能在别的项目里(文档被移走后尤其如此)。只看同项目会把
    跨项目引用变成看不见的悬空引用。

    可见性:只列调用者能看见的项目(自己参加的 + 管理员的),别人的个人空间/私有项目
    不出现在这里 —— 反链是"引用关系",不该变成项目内容的探测器。
    """
    ctx, doc = dep
    visible = visible_project_ids(db, ctx.user)
    rows = (db.query(Doc).filter(Doc.deleted_at.is_(None), Doc.id != doc.id,
                                 Doc.project_id.in_(visible),
                                 refs.sql_prefilter(Doc.content))
            .order_by(Doc.updated_at.desc()).limit(200).all())
    hits = [d for d in rows if doc.id in refs.doc_ids_in(d.content)]
    projects = {p.id: p.name for p in
                db.query(Project).filter(Project.id.in_({d.project_id for d in hits})).all()}
    return [{"id": d.id, "title": d.title, "updatedAt": d.updated_at.isoformat(),
             "projectId": d.project_id, "projectName": projects.get(d.project_id, "")}
            for d in hits[:100]]


@router.patch("/api/docs/{doc_id}")
def patch_doc(doc_id: IdPath, payload: dict, dep=Depends(require_doc_role("EDITOR")),
              db: DbSession = Depends(get_db)):
    """改标题 / 改父级(项目内)。

    **跨项目移动不在这里** —— 它要改整棵子树的归属、要与项目权限打交道,是
    POST /api/docs/{id}/move 的事(见下)。这里只整理项目内的位置。
    """
    ctx, doc = dep
    if "title" in payload:
        doc.title = str_field(payload, "title", 200, required=True)
    if "parentId" in payload:
        parent_id = opt_id(payload["parentId"])
        validate_parent(db, Doc, parent_id, doc.project_id, moving=doc, label="父文档")
        if parent_id != doc.parent_id:
            # 换层后落在目标层末尾:位置由一个显式值决定,不靠"原来的 sort 恰好还合适"
            doc.sort = next_sort(db, Doc, doc.project_id, parent_id)
        doc.parent_id = parent_id
    doc.updated_by = ctx.user.id
    doc.updated_at = utcnow()
    db.commit()
    return doc_brief(doc)


@router.post("/api/docs/{doc_id}/move")
def move_doc(doc_id: IdPath, payload: dict, dep=Depends(require_doc_role("EDITOR")),
             db: DbSession = Depends(get_db)):
    """移动文档(项目内改位置 / 跨项目转移)。

    权限与父级判定复用 auth 的两个共用函数(与文件、文件夹移动同一套语义):
    项目内整理只需 EDITOR;跨项目需源项目 ADMIN + 目标项目 EDITOR。

    **整棵子树一起走,回收站里的内容除外**:子文档必须跟着父文档(否则在新项目里
    挂着一串指向旧项目的子节点)。回收站里的文档是"已删除"状态,恢复时由
    trash._parent_gone 兜住(父级不在同一项目就回落到项目根)—— 让它们悄悄跟着搬走
    与用户看到的回收站内容不符。
    """
    ctx, doc = dep
    target_id = id_field(payload, "projectId", required=True)
    parent_id = opt_id(payload.get("parentId"))
    src_id = doc.project_id
    ensure_move_allowed(db, ctx, src_id, target_id)
    validate_parent(db, Doc, parent_id, target_id, moving=doc, label="父文档")
    subtree = collect_subtree(db, Doc, doc)
    doc.parent_id = parent_id
    doc.sort = next_sort(db, Doc, target_id, parent_id)
    moved = subtree
    if target_id != src_id:
        # 只改未删除的行:回收站里的子文档不随迁(恢复时由 trash._parent_gone 兜住)
        moved = [d.id for d in db.query(Doc.id)
                 .filter(Doc.id.in_(subtree), Doc.deleted_at.is_(None)).all()]
        doc.project_id = target_id
        db.query(Doc).filter(Doc.id.in_(moved)) \
            .update({"project_id": target_id}, synchronize_session=False)
    db.commit()
    return {**doc_brief(doc), "fromProjectId": src_id, "movedDocs": len(moved)}


@router.get("/api/docs/{doc_id}/move-check")
def move_check(doc_id: IdPath, projectId: str = "", parentId: str = "",
               dep=Depends(require_doc_role("VIEWER")), db: DbSession = Depends(get_db)):
    """移动前提示(只读、不改任何东西)。

    移动文档**不会带走附件**:文件按自己的 project_id 归属,跨项目搬一篇文档时,
    它正文里引用的文件仍留在原项目。这不是缺陷而是选择(文件可能被多篇文档引用,
    跟着搬会让别处的引用指向另一个项目),但用户必须**在动手前知道** —— 否则
    表现成"搬过去附件全打不开了"。

    返回随迁文档数与"会留在源项目的被引用文件",供确认弹窗直接展示。
    """
    ctx, doc = dep
    # 目标项目缺省 = 当前项目(前端"只改位置"时不必传);非法形状当场 400
    target = normalize_id(projectId) if projectId else doc.project_id
    if target is None:
        err(400, "VALIDATION", "projectId 不是合法的 id")
    parent = opt_id(parentId)
    ensure_move_allowed(db, ctx, doc.project_id, target)
    validate_parent(db, Doc, parent, target, moving=doc, label="父文档")
    subtree = [d.id for d in db.query(Doc.id).filter(Doc.id.in_(collect_subtree(db, Doc, doc)),
                                                     Doc.deleted_at.is_(None)).all()]
    files = []
    if target != doc.project_id:
        # 子树里所有文档正文引用的、且仍留在源项目的文件
        contents = db.query(Doc.content).filter(Doc.id.in_(subtree)).all()
        fids = set()
        for (c,) in contents:
            fids |= refs.file_ids_in(c)
        if fids:
            rows = db.query(File).filter(File.id.in_(fids), File.deleted_at.is_(None),
                                         File.project_id == doc.project_id).all()
            files = [{"id": f.id, "name": f.name} for f in rows]
    proj = db.get(Project, target)
    return {"docId": doc.id, "fromProjectId": doc.project_id,
            "targetProject": {"id": target, "name": proj.name if proj else ""},
            "docs": len(subtree), "foreignFiles": files,
            "note": ("跨项目移动只搬文档,正文引用的文件仍留在源项目"
                     if files else "")}


def _prune_versions(db: DbSession, doc_id: str) -> int:
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


def save_doc_content(db: DbSession, doc: Doc, content: str, user_id: str,
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


def _save_content(db: DbSession, doc: Doc, content: str, user_id: str,
                  kind: str = "save", *, base_version: int | None = None) -> dict:
    """REST 写入:调用共享快照逻辑并提交"""
    _, version = save_doc_content(db, doc, content, user_id, kind,
                                  base_version=base_version)
    db.commit()
    return {"version": version}


@router.put("/api/docs/{doc_id}/content")
def put_content(doc_id: IdPath, payload: dict, dep=Depends(require_doc_role("EDITOR")),
                db: DbSession = Depends(get_db)):
    ctx, doc = dep
    content = str_content(payload)
    if content is None:
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
def append_content(doc_id: IdPath, payload: dict, dep=Depends(require_doc_role("EDITOR")),
                   db: DbSession = Depends(get_db)):
    ctx, doc = dep
    content = str_content(payload)
    if content is None:
        bad_request("content 必须为字符串")
    # 空内容分隔 \n\n(§7.4);append 是否快照旧内容文档未定义,
    # 按最简实现与 PUT 一致(先存快照,保证可回滚)
    new_content = (doc.content + "\n\n" + content) if doc.content else content
    # 不校验基线:追加的读-改-写全在服务端本次事务里完成,拼的是"当下的正文",
    # 不存在"拿着旧副本整篇盖回去"的形状 —— 并发追加也不会丢掉别人的正文
    return _save_content(db, doc, new_content, ctx.user.id, base_version=None)


@router.get("/api/docs/{doc_id}/versions")
def list_versions(doc_id: IdPath, dep=Depends(require_doc_role("VIEWER")),
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
    authors = user_names(db, (r[3] for r in rows))
    return [{"id": r[0], "kind": r[1], "createdAt": r[2].isoformat(),
             "contentChars": r[4] or 0,
             "createdBy": ({"id": r[3], "name": authors[r[3]]} if r[3] in authors else None)}
            for r in rows]


@router.get("/api/docs/{doc_id}/versions/{vid}")
def get_version(doc_id: IdPath, vid: IdPath, dep=Depends(require_doc_role("VIEWER")),
                db: DbSession = Depends(get_db)):
    _, doc = dep
    v = db.query(DocVersion).filter_by(id=vid, doc_id=doc.id).first()
    if not v:
        err(404, "NOT_FOUND", "版本不存在")
    return {"content": v.content}


@router.post("/api/docs/{doc_id}/versions/{vid}/restore")
def restore_version(doc_id: IdPath, vid: IdPath, dep=Depends(require_doc_role("EDITOR")),
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
