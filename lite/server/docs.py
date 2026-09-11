"""项目 / 成员 / 文档树 / 内容 / 版本 / 回收站(构建文档 §7.3、§7.4)。"""
import os
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, avatar_color, bad_request, current_user, ensure_project_role,
                  err, get_project_or_404, is_project_member, project_role, require_doc_role,
                  require_project_role, require_write, require_write_ctx, str_field)
# 类型判定与白名单的唯一真相在 files.py(搜索模块也这样复用);files 不反向依赖 docs,无环
from files import can_inline
from models import (FILES_DIR, Doc, DocVersion, File, Folder, Project, ProjectMember, User,
                    file_abspath, get_db, unlink_quiet, utcnow)

router = APIRouter()

ROLES = ("OWNER", "ADMIN", "EDITOR", "VIEWER")

# 版本合并窗口(分钟):同一人在窗口内的连续保存不再新增还原点,
# 于是窗口起点的快照被保留 —— 即"这次编辑开始前的状态",一次编辑会话 = 一个还原点。
# 编辑器自动保存是 800ms 防抖,不加合并窗口的话一小时能产生几十个版本。
VERSION_MERGE_MINUTES = int(os.environ.get("VERSION_MERGE_MINUTES", "5"))

_DAY = 86400
_HOUR = 3600


# ---------- 序列化 ----------

def _project_json(db: DbSession, p: Project, user: User) -> dict:
    # lastUpdatedAt 取"项目内最近一次文档更新或文件上传",供发现页按活跃度排序。
    # 单条查询取两个 max 再比大小,避免为每个项目扫表。
    last_doc = db.query(func.max(Doc.updated_at)).filter_by(project_id=p.id) \
        .filter(Doc.deleted_at.is_(None)).scalar()
    last_file = db.query(func.max(File.created_at)).filter_by(project_id=p.id) \
        .filter(File.deleted_at.is_(None)).scalar()
    last = max([x for x in (last_doc, last_file) if x is not None], default=None)
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "isPersonal": bool(p.is_personal),
        "isPublic": p.visibility == "public",
        # isMember 与 myRole 必须同时给:公开项目的访客也会拿到 myRole=VIEWER,
        # 前端若只看 myRole 会以为自己是成员并渲染出写按钮(点了 403)
        "isMember": is_project_member(db, p.id, user),
        "createdAt": p.created_at.isoformat(),
        "lastUpdatedAt": last.isoformat() if last else None,
        "myRole": project_role(db, p.id, user),
        "memberCount": db.query(ProjectMember).filter_by(project_id=p.id).count(),
        "docCount": db.query(Doc).filter_by(project_id=p.id).filter(Doc.deleted_at.is_(None)).count(),
    }


# ---------- 7.3 项目 ----------

@router.get("/api/discover/projects")
def discover_projects(ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    """公开项目广场:本实例全部公开项目(按最近活跃倒序)。

    30 人的团队项目数量不多,静态目录浏览起来比直接问同事还慢 —— 所以
    按 lastUpdatedAt 倒序,把"最近有人在动"的排在最前(`/api/recent` 提供动态,
    前端与它合成一页)。

    个人空间永不出现:即便数据异常导致它被标成 public,这里也排除(双保险)。
    """
    rows = (db.query(Project)
            .filter(Project.visibility == "public", Project.is_personal.is_(False))
            .all())
    items = [_project_json(db, p, ctx.user) for p in rows]
    # 无活动时间的排最后(用空串比较,避免 None 参与排序)
    items.sort(key=lambda x: x.get("lastUpdatedAt") or "", reverse=True)
    return items


@router.post("/api/projects")
def create_project(payload: dict, ctx: AuthContext = Depends(require_write),
                   db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    name = str_field(payload, "name", 100, required=True)
    description = str_field(payload, "description", 5000)
    p = Project(name=name, description=description, created_by=ctx.user.id)
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=ctx.user.id, role="OWNER"))
    db.commit()
    return _project_json(db, p, ctx.user)


@router.get("/api/projects")
def list_projects(all: str = "", ctx: AuthContext = Depends(current_user),
                  db: DbSession = Depends(get_db)):
    if all == "1" and ctx.user.is_admin:
        # 管理员:全部非个人项目 + 我自己的个人项目(不含他人个人项目)
        rows = (db.query(Project)
                .filter((Project.is_personal.is_(False)) | (Project.created_by == ctx.user.id))
                .order_by(Project.is_personal.desc(), Project.created_at.asc()).all())
    else:
        # 我参与的,个人项目排最前
        ids = [m.project_id for m in db.query(ProjectMember).filter_by(user_id=ctx.user.id).all()]
        rows = (db.query(Project).filter(Project.id.in_(ids))
                .order_by(Project.is_personal.desc(), Project.created_at.asc()).all()
                if ids else [])
    return [_project_json(db, p, ctx.user) for p in rows]


@router.get("/api/projects/{project_id}")
def get_project(project_id: str, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                db: DbSession = Depends(get_db)):
    return _project_json(db, get_project_or_404(db, project_id), ctx.user)


@router.patch("/api/projects/{project_id}")
def patch_project(project_id: str, payload: dict,
                  ctx: AuthContext = Depends(require_project_role("ADMIN")),
                  db: DbSession = Depends(get_db)):
    _ = require_write_ctx(ctx, db)  # PAT write 校验
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    p = get_project_or_404(db, project_id)
    if "name" in payload:
        p.name = str_field(payload, "name", 100, required=True)
    if "description" in payload:
        p.description = str_field(payload, "description", 5000)
    if "isPublic" in payload:
        if p.is_personal:
            # 个人空间永远私有:它是私有草稿区,一旦能公开用户就不敢往里放东西,
            # 而那正是它的价值。要公开内容就建一个普通项目。
            err(403, "FORBIDDEN", "个人空间不可公开")
        p.visibility = "public" if payload["isPublic"] else "private"
    db.commit()
    return _project_json(db, p, ctx.user)


@router.delete("/api/projects/{project_id}")
def delete_project(project_id: str, ctx: AuthContext = Depends(require_project_role("OWNER")),
                   db: DbSession = Depends(get_db)):
    _ = require_write_ctx(ctx, db)
    p = get_project_or_404(db, project_id)
    if p.is_personal:
        err(403, "FORBIDDEN", "个人空间不可删除")
    # §7.3:先删 doc_versions(docs in project)、docs、members,再删项目(手动处理 FK)
    doc_ids = [d.id for d in db.query(Doc.id).filter_by(project_id=project_id).all()]
    if doc_ids:
        db.query(DocVersion).filter(DocVersion.doc_id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(Doc).filter(Doc.id.in_(doc_ids)).delete(synchronize_session=False)
    # 物理文件必须一并清除:只删记录会留下一堆无主文件永久占盘,
    # 而删除项目的人以为空间已经释放了(管理后台的孤儿文件清理可兜底回收)
    paths = [file_abspath(p) for (p,) in
             db.query(File.storage_path).filter_by(project_id=project_id).all()]
    db.query(File).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(Folder).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(ProjectMember).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.delete(p)
    db.commit()
    for path in paths:
        unlink_quiet(path)
    return {"ok": True, "removedFiles": len(paths)}


# ---------- 7.3 成员 ----------

@router.get("/api/projects/{project_id}/members")
def list_members(project_id: str, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                 db: DbSession = Depends(get_db)):
    rows = (db.query(ProjectMember, User).join(User, User.id == ProjectMember.user_id)
            .filter(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.created_at.asc()).all())
    # 公开项目的"访客"(非成员)不返回邮箱与禁用状态:VIEWER 的含义是"能读内容",
    # 不该连带把全组成员邮箱和账号状态暴露给本实例任意登录用户。
    # 真成员照旧(他们本来就在成员页管理这些人)。
    member = is_project_member(db, project_id, ctx.user)
    out = []
    for m, u in rows:
        d = {"id": u.id, "name": u.name, "avatarColor": avatar_color(u.id)}
        if member:
            d["email"] = u.email
            d["isDisabled"] = bool(u.is_disabled)
        out.append({"userId": m.user_id, "role": m.role, "user": d})
    return out


@router.post("/api/projects/{project_id}/members")
def add_member(project_id: str, payload: dict,
               ctx: AuthContext = Depends(require_project_role("ADMIN")),
               db: DbSession = Depends(get_db)):
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    p = get_project_or_404(db, project_id)
    if p.is_personal:
        err(403, "FORBIDDEN", "个人空间不可管理成员")
    email = str_field(payload, "email", 255, required=True).lower()
    role = str_field(payload, "role", 10, default="VIEWER") or "VIEWER"
    if role not in ROLES:
        bad_request("role 必须为 OWNER/ADMIN/EDITOR/VIEWER")
    # 授予 OWNER 必须已是 OWNER。否则项目 ADMIN 可把自己升成 OWNER,
    # 而删除项目要 OWNER —— 等于 ADMIN 能绕过这层门槛删项目;
    # 全局管理员(在项目上只映射到 ADMIN)也能借此删掉他人项目。
    if role == "OWNER" and project_role(db, project_id, ctx.user) != "OWNER":
        err(403, "FORBIDDEN", "只有项目所有者才能授予所有者")
    user = db.query(User).filter_by(email=email).first()
    if not user:
        err(404, "NOT_FOUND", "用户不存在(需先用管理员账号创建)")
    if db.query(ProjectMember).filter_by(project_id=project_id, user_id=user.id).first():
        err(409, "CONFLICT", "该用户已是项目成员")
    m = ProjectMember(project_id=project_id, user_id=user.id, role=role)
    db.add(m)
    db.commit()
    return _member_json(user, role)


def _owner_count(db: DbSession, project_id: str) -> int:
    return db.query(ProjectMember).filter_by(project_id=project_id, role="OWNER").count()


def _member_json(user: User, role: str) -> dict:
    return {"userId": user.id, "role": role,
            "user": {"id": user.id, "email": user.email, "name": user.name,
                     "isDisabled": bool(user.is_disabled), "avatarColor": avatar_color(user.id)}}


@router.patch("/api/projects/{project_id}/members/{user_id}")
def patch_member(project_id: str, user_id: str, payload: dict,
                 ctx: AuthContext = Depends(require_project_role("ADMIN")),
                 db: DbSession = Depends(get_db)):
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    p = get_project_or_404(db, project_id)
    if p.is_personal:
        err(403, "FORBIDDEN", "个人空间不可管理成员")
    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).first()
    if not m:
        err(404, "NOT_FOUND", "成员不存在")
    role = str_field(payload, "role", 10, required=True)
    if role not in ROLES:
        bad_request("role 必须为 OWNER/ADMIN/EDITOR/VIEWER")
    # 同 add_member:ADMIN 不得授予 OWNER(否则可自我提权后删项目)
    if role == "OWNER" and m.role != "OWNER" \
            and project_role(db, project_id, ctx.user) != "OWNER":
        err(403, "FORBIDDEN", "只有项目所有者才能授予所有者")
    # 最后一个 OWNER 降级保护(§7.3)
    if m.role == "OWNER" and role != "OWNER" and _owner_count(db, project_id) <= 1:
        err(409, "CONFLICT", "项目至少需要一名所有者")
    m.role = role
    db.commit()
    return {"userId": user_id, "role": role}


@router.delete("/api/projects/{project_id}/members/{user_id}")
def remove_member(project_id: str, user_id: str,
                  ctx: AuthContext = Depends(require_project_role("ADMIN")),
                  db: DbSession = Depends(get_db)):
    require_write_ctx(ctx, db)
    p = get_project_or_404(db, project_id)
    if p.is_personal:
        err(403, "FORBIDDEN", "个人空间不可管理成员")
    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).first()
    if not m:
        err(404, "NOT_FOUND", "成员不存在")
    if m.role == "OWNER" and _owner_count(db, project_id) <= 1:
        err(409, "CONFLICT", "项目至少需要一名所有者")
    db.delete(m)
    db.commit()
    return {"ok": True}


# ---------- 7.4 文档 ----------

@router.get("/api/projects/{project_id}/docs/tree")
def doc_tree(project_id: str, ctx: AuthContext = Depends(require_project_role("VIEWER")),
             db: DbSession = Depends(get_db)):
    docs = (db.query(Doc).filter_by(project_id=project_id).filter(Doc.deleted_at.is_(None))
            .order_by(Doc.sort.asc(), Doc.created_at.asc()).all())
    nodes = {d.id: {"id": d.id, "title": d.title, "parentId": d.parent_id,
                    "updatedAt": d.updated_at.isoformat(), "children": []} for d in docs}
    roots = []
    for d in docs:
        node = nodes[d.id]
        if d.parent_id and d.parent_id in nodes:
            nodes[d.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/api/projects/{project_id}/trash")
def project_trash(project_id: str, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                  db: DbSession = Depends(get_db)):
    """项目回收站:文档 + 云空间文件 + 文件夹统一返回(均按删除时间倒序)。

    **只列"子树根"**:删除文件夹/文档时整棵子树都被软删除,若把子项也列出来,
    删一个目录会让回收站一次多出几十条,而它们本就该随根一起恢复(恢复是递归的)。
    判据:父级未删除、或父级不存在(父级被彻底删掉后子项已在同一事务中清除)。

    注意 500 条上限作用在**过滤前**的原始集合上:极端情况下(单项目回收站里
    超过 500 个已删项)可能少列一些根。要彻底解决需引入分页,见 HANDOFF §4.9。
    """
    # 回收站只对**真成员与全局管理员**开放:公开项目的访客能读正式内容,
    # 但不该看到别人删掉了什么(删除历史属于项目内部信息)。
    # 管理员放行是因为他们本就承担运维职责(且不因此获得个人空间访问权,见 §4.14)。
    if not (is_project_member(db, project_id, ctx.user) or ctx.user.is_admin):
        err(403, "FORBIDDEN", "回收站仅项目成员可见")
    docs = (db.query(Doc).filter_by(project_id=project_id).filter(Doc.deleted_at.isnot(None))
            .order_by(Doc.deleted_at.desc()).limit(500).all())
    files = (db.query(File).filter_by(project_id=project_id).filter(File.deleted_at.isnot(None))
             .order_by(File.deleted_at.desc()).limit(500).all())
    folders = (db.query(Folder).filter_by(project_id=project_id).filter(Folder.deleted_at.isnot(None))
               .order_by(Folder.deleted_at.desc()).limit(500).all())
    # 已删文档 id(判断某文档的父级是否也在回收站)
    deleted_doc_ids = {d.id for d in docs}
    docs = [d for d in docs if not d.parent_id or d.parent_id not in deleted_doc_ids]
    # 已删文件夹 id:一次取全(不带上限),否则父级不在前 500 条时会被误判成根
    deleted_folder_ids = {fid for (fid,) in db.query(Folder.id)
                          .filter_by(project_id=project_id)
                          .filter(Folder.deleted_at.isnot(None)).all()}
    folders = [f for f in folders if not f.parent_id or f.parent_id not in deleted_folder_ids]
    files = [f for f in files if not f.folder_id or f.folder_id not in deleted_folder_ids]
    return {
        "docs": [{"id": d.id, "title": d.title, "deletedAt": d.deleted_at.isoformat()}
                 for d in docs],
        "files": [{"id": f.id, "name": f.name, "mime": f.mime, "size": f.size,
                   "canInline": can_inline(f.mime),
                   "deletedAt": f.deleted_at.isoformat()} for f in files],
        "folders": [{"id": f.id, "name": f.name, "deletedAt": f.deleted_at.isoformat()}
                    for f in folders],
    }


@router.get("/api/projects/{project_id}/folders/tree")
def folder_tree(project_id: str, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                db: DbSession = Depends(get_db)):
    """项目文件夹树(未删除)。

    一次调用解决三处需要"文件夹层级"的地方:移动目标选择器、面包屑(此前父链只存在
    前端会话内,刷新就回根目录)、上传目录时的路径缓存。返回嵌套结构,便于直接渲染树。
    """
    folders = (db.query(Folder).filter_by(project_id=project_id)
               .filter(Folder.deleted_at.is_(None))
               .order_by(Folder.name.asc()).limit(2000).all())
    nodes = {f.id: {"id": f.id, "name": f.name, "parentId": f.parent_id, "children": []}
             for f in folders}
    roots = []
    for f in folders:
        node = nodes[f.id]
        if f.parent_id and f.parent_id in nodes:
            nodes[f.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.post("/api/projects/{project_id}/docs")
def create_doc(project_id: str, payload: dict,
               ctx: AuthContext = Depends(require_project_role("EDITOR")),
               db: DbSession = Depends(get_db)):
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    get_project_or_404(db, project_id)
    title = str_field(payload, "title", 200) or "无标题文档"
    parent_id = payload.get("parentId") or None
    if parent_id is not None:
        if not isinstance(parent_id, str):
            bad_request("parentId 必须为字符串")
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
    return {"id": doc.id, "projectId": doc.project_id, "parentId": doc.parent_id,
            "title": doc.title, "version": doc.version,
            "createdAt": doc.created_at.isoformat(), "updatedAt": doc.updated_at.isoformat()}


@router.get("/api/docs/{doc_id}")
def get_doc(dep=Depends(require_doc_role("VIEWER")), db: DbSession = Depends(get_db)):
    _, doc = dep
    # 位置上下文:location 契约与 files.file_meta 完全同形(projectId/projectName/path),
    # 引用浮层、CLI --meta 等消费方一份代码即可展示"在哪"
    proj = db.get(Project, doc.project_id)
    parent_path, cur, depth = [], doc, 0
    while cur.parent_id and depth < 64:  # 深度上限防数据异常成环
        cur = db.get(Doc, cur.parent_id)
        if not cur:
            break
        parent_path.insert(0, cur.title)
        depth += 1
    return {"id": doc.id, "projectId": doc.project_id, "parentId": doc.parent_id,
            "title": doc.title, "content": doc.content, "version": doc.version,
            "location": {"projectId": doc.project_id,
                         "projectName": proj.name if proj else "",
                         "path": parent_path},
            "contentChars": len(doc.content or ""),
            "updatedAt": doc.updated_at.isoformat(), "createdAt": doc.created_at.isoformat()}


@router.get("/api/docs/{doc_id}/backlinks")
def list_backlinks(doc_id: str, dep=Depends(require_doc_role("VIEWER")),
                   db: DbSession = Depends(get_db)):
    """反向链接:同项目内正文中引用了本文档(teamdoc://doc/{pid}/{doc_id})的未删除文档"""
    _, doc = dep
    rows = (db.query(Doc).filter_by(project_id=doc.project_id)
            .filter(Doc.deleted_at.is_(None), Doc.id != doc.id,
                    Doc.content.like(f"%teamdoc://doc/%/{doc_id})%"))
            .order_by(Doc.updated_at.desc()).limit(100).all())
    return [{"id": d.id, "title": d.title, "updatedAt": d.updated_at.isoformat()} for d in rows]


def _subtree_ids(db: DbSession, doc: Doc) -> list[str]:
    """收集自身 + 全部后代 id"""
    all_docs = db.query(Doc.id, Doc.parent_id).filter_by(project_id=doc.project_id).all()
    children = {}
    for did, pid in all_docs:
        children.setdefault(pid, []).append(did)
    result, stack = [], [doc.id]
    while stack:
        cur = stack.pop()
        result.append(cur)
        stack.extend(children.get(cur, []))
    return result


@router.patch("/api/docs/{doc_id}")
def patch_doc(doc_id: str, payload: dict, dep=Depends(require_doc_role("EDITOR")),
              db: DbSession = Depends(get_db)):
    ctx, doc = dep
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    if "title" in payload:
        doc.title = str_field(payload, "title", 200, required=True)
    if "parentId" in payload:
        parent_id = payload["parentId"] or None
        if parent_id is not None:
            if not isinstance(parent_id, str):
                bad_request("parentId 必须为字符串")
            parent = db.get(Doc, parent_id)
            if not parent or parent.project_id != doc.project_id or parent.deleted_at is not None:
                err(404, "NOT_FOUND", "父文档不存在")
            if parent_id in _subtree_ids(db, doc):
                err(409, "CONFLICT", "不能移动到自己的子文档下")
        doc.parent_id = parent_id
    doc.updated_by = ctx.user.id
    doc.updated_at = utcnow()
    db.commit()
    return {"id": doc.id, "projectId": doc.project_id, "parentId": doc.parent_id,
            "title": doc.title, "version": doc.version,
            "updatedAt": doc.updated_at.isoformat(), "createdAt": doc.created_at.isoformat()}


@router.delete("/api/docs/{doc_id}")
def delete_doc(doc_id: str, dep=Depends(require_doc_role("EDITOR")),
               db: DbSession = Depends(get_db)):
    ctx, doc = dep
    require_write_ctx(ctx, db)
    ids = _subtree_ids(db, doc)
    now = utcnow()
    removed = (db.query(Doc).filter(Doc.id.in_(ids), Doc.deleted_at.is_(None))
               .update({"deleted_at": now}, synchronize_session=False))
    db.commit()
    return {"removed": removed}


@router.post("/api/docs/{doc_id}/restore")
def restore_doc(doc_id: str, ctx: AuthContext = Depends(current_user),
                db: DbSession = Depends(get_db)):
    require_write_ctx(ctx, db)
    doc = db.get(Doc, doc_id)
    if not doc:
        err(404, "NOT_FOUND", "文档不存在")
    # 先校权限再暴露回收站状态:否则非成员可凭 409/403 差异探测他人回收站内容
    ensure_project_role(db, ctx, doc.project_id, "EDITOR")
    if doc.deleted_at is None:
        err(409, "CONFLICT", "文档不在回收站")
    ids = _subtree_ids(db, doc)
    restored = (db.query(Doc).filter(Doc.id.in_(ids), Doc.deleted_at.isnot(None))
                .update({"deleted_at": None}, synchronize_session=False))
    # 父级仍在回收站时移到根(§7.4)
    if doc.parent_id:
        parent = db.get(Doc, doc.parent_id)
        if parent and parent.deleted_at is not None:
            doc.parent_id = None
    db.commit()
    return {"restored": restored}


@router.delete("/api/docs/{doc_id}/permanent")
def permanent_delete_doc(doc_id: str, ctx: AuthContext = Depends(current_user),
                         db: DbSession = Depends(get_db)):
    """彻底删除:仅回收站中的文档可删;连同子树与历史版本一起清除"""
    require_write_ctx(ctx, db)
    doc = db.get(Doc, doc_id)
    if not doc:
        err(404, "NOT_FOUND", "文档不存在")
    ensure_project_role(db, ctx, doc.project_id, "EDITOR")
    if doc.deleted_at is None:
        err(409, "CONFLICT", "文档不在回收站")
    ids = _subtree_ids(db, doc)
    db.query(DocVersion).filter(DocVersion.doc_id.in_(ids)).delete(synchronize_session=False)
    db.query(Doc).filter(Doc.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return {"deleted": len(ids)}


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

    只取 (id, created_at):这个函数在**每次保存**时都会跑(含 WS 自动保存),
    而 content 是 Text —— 把全部版本正文读进内存只为算几个时间戳,
    在粘过大表格的高频文档上会造成明显的内存峰值。
    """
    rows = (db.query(DocVersion.id, DocVersion.created_at)
            .filter_by(doc_id=doc_id)
            .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).all())
    now = utcnow()
    seen: set = set()
    drop: list[str] = []
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
                     label: str = "覆盖前") -> tuple[bool, int]:
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


def _save_content(db: DbSession, doc: Doc, content: str, user_id: str,
                  label: str = "覆盖前") -> dict:
    """REST 写入:调用共享快照逻辑并提交"""
    _, version = save_doc_content(db, doc, content, user_id, label)
    db.commit()
    return {"version": version}


@router.put("/api/docs/{doc_id}/content")
def put_content(doc_id: str, payload: dict, dep=Depends(require_doc_role("EDITOR")),
                db: DbSession = Depends(get_db)):
    ctx, doc = dep
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    content = payload.get("content")
    if not isinstance(content, str):
        bad_request("content 必须为字符串")
    return _save_content(db, doc, content, ctx.user.id, label="覆盖前")


@router.post("/api/docs/{doc_id}/append")
def append_content(doc_id: str, payload: dict, dep=Depends(require_doc_role("EDITOR")),
                   db: DbSession = Depends(get_db)):
    ctx, doc = dep
    require_write_ctx(ctx, db)
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    content = payload.get("content")
    if not isinstance(content, str):
        bad_request("content 必须为字符串")
    # 空内容分隔 \n\n(§7.4);append 是否快照旧内容文档未定义,
    # 按最简实现与 PUT 一致(先存"覆盖前"快照,保证可回滚)
    new_content = (doc.content + "\n\n" + content) if doc.content else content
    return _save_content(db, doc, new_content, ctx.user.id, label="覆盖前")


@router.get("/api/docs/{doc_id}/versions")
def list_versions(doc_id: str, dep=Depends(require_doc_role("VIEWER")),
                  db: DbSession = Depends(get_db)):
    _, doc = dep
    rows = (db.query(DocVersion).filter_by(doc_id=doc.id)
            .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).limit(100).all())
    return [{"id": v.id, "label": v.label, "createdAt": v.created_at.isoformat(),
             "createdBy": v.created_by} for v in rows]


@router.get("/api/docs/{doc_id}/versions/{vid}")
def get_version(doc_id: str, vid: str, dep=Depends(require_doc_role("VIEWER")),
                db: DbSession = Depends(get_db)):
    _, doc = dep
    v = db.query(DocVersion).filter_by(id=vid, doc_id=doc.id).first()
    if not v:
        err(404, "NOT_FOUND", "版本不存在")
    return {"content": v.content}


@router.post("/api/docs/{doc_id}/versions/{vid}/restore")
def restore_version(doc_id: str, vid: str, dep=Depends(require_doc_role("EDITOR")),
                    db: DbSession = Depends(get_db)):
    ctx, doc = dep
    require_write_ctx(ctx, db)
    v = db.query(DocVersion).filter_by(id=vid, doc_id=doc.id).first()
    if not v:
        err(404, "NOT_FOUND", "版本不存在")
    # 等价于对该版本内容执行 PUT content(§7.4)。
    # label 用"还原前":与自动保存的 label 不同 → 不会被合并窗口并掉,
    # 于是"还原操作之前的现场"始终是一个可回退的里程碑。
    return _save_content(db, doc, v.content, ctx.user.id, label="还原前")
