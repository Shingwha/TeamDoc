"""文档域(构建文档 §7.4):文档树 / 内容读写 / 版本历史 / 反向链接。

项目与成员在 projects.py;回收站(删除/恢复/彻底删除 + 列表)在 trash.py;
文件夹树在 files.py。此前四类混在同一个文件里(728 行),回收站流程还与
files.py 各写一份 —— 按 domain 拆开后各文件可独立演进。
"""
import os

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, bad_request, err, get_project_or_404, opt_int,
                  pat_write_guard, require_doc_role, require_project_role, str_field)
from models import (Doc, DocVersion, Project, ancestor_names, build_tree,
                    collect_subtree, get_db, utcnow)

router = APIRouter(dependencies=[Depends(pat_write_guard)])

# 版本合并窗口(分钟):同一人在窗口内的连续保存不再新增还原点,
# 于是窗口起点的快照被保留 —— 即"这次编辑开始前的状态",一次编辑会话 = 一个还原点。
# 编辑器自动保存是 800ms 防抖,不加合并窗口的话一小时能产生几十个版本。
VERSION_MERGE_MINUTES = int(os.environ.get("VERSION_MERGE_MINUTES", "5"))

_DAY = 86400
_HOUR = 3600


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

    只取 (id, created_at):这个函数在**每次保存**时都会跑(含 WS 自动保存),
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
                     label: str = "覆盖前") -> tuple:
    """内容有变化时先把旧内容存为 DocVersion;version+=1(该字段是"保存次数",不是版本数)。

    合并窗口:同一人、同类来源(label 相同)、窗口内的连续保存不再新增还原点 ——
    窗口起点的快照即为本次编辑会话的还原点。label 不同的操作(如还原历史)不会与
    自动保存合并,因此还原点天然被保留。

    返回 (是否发生变化, 当前版本号)。REST 与 WebSocket 两条写入路径共用,
    避免版本策略在两处漂移。**不提交事务**,由调用方决定提交时机。
    """
    if doc.content == content:
        return False, doc.version
    now = utcnow()
    last = (db.query(DocVersion).filter_by(doc_id=doc.id)
            .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).first())
    within_window = (last is not None
                     and last.label == label
                     and last.created_by == user_id
                     and (now - last.created_at).total_seconds() < VERSION_MERGE_MINUTES * 60)
    if not within_window:
        db.add(DocVersion(doc_id=doc.id, content=doc.content, label=label,
                          created_by=user_id, created_at=now))
    doc.content = content
    doc.version += 1
    doc.updated_by = user_id
    doc.updated_at = now
    db.flush()
    _prune_versions(db, doc.id)
    return True, doc.version


def _save_content(db: DbSession, doc: Doc, content: str, user_id: int,
                  label: str = "覆盖前") -> dict:
    """REST 写入:调用共享快照逻辑并提交"""
    _, version = save_doc_content(db, doc, content, user_id, label)
    db.commit()
    return {"version": version}


@router.put("/api/docs/{doc_id}/content")
def put_content(doc_id: int, payload: dict, dep=Depends(require_doc_role("EDITOR")),
                db: DbSession = Depends(get_db)):
    ctx, doc = dep
    content = payload.get("content")
    if not isinstance(content, str):
        bad_request("content 必须为字符串")
    return _save_content(db, doc, content, ctx.user.id, label="覆盖前")


@router.post("/api/docs/{doc_id}/append")
def append_content(doc_id: int, payload: dict, dep=Depends(require_doc_role("EDITOR")),
                   db: DbSession = Depends(get_db)):
    ctx, doc = dep
    content = payload.get("content")
    if not isinstance(content, str):
        bad_request("content 必须为字符串")
    # 空内容分隔 \n\n(§7.4);append 是否快照旧内容文档未定义,
    # 按最简实现与 PUT 一致(先存"覆盖前"快照,保证可回滚)
    new_content = (doc.content + "\n\n" + content) if doc.content else content
    return _save_content(db, doc, new_content, ctx.user.id, label="覆盖前")


@router.get("/api/docs/{doc_id}/versions")
def list_versions(doc_id: int, dep=Depends(require_doc_role("VIEWER")),
                  db: DbSession = Depends(get_db)):
    _, doc = dep
    rows = (db.query(DocVersion).filter_by(doc_id=doc.id)
            .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).limit(100).all())
    return [{"id": v.id, "label": v.label, "createdAt": v.created_at.isoformat(),
             "createdBy": v.created_by} for v in rows]


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
    # label 用"还原前":与自动保存的 label 不同 → 不会被合并窗口并掉,
    # 于是"还原操作之前的现场"始终是一个可回退的里程碑。
    return _save_content(db, doc, v.content, ctx.user.id, label="还原前")
