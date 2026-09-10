"""项目 / 成员 / 文档树 / 内容 / 版本 / 回收站(构建文档 §7.3、§7.4)。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, avatar_color, bad_request, current_user, ensure_project_role,
                  err, get_project_or_404, project_role, require_doc_role, require_project_role,
                  require_write, require_write_ctx, str_field)
from models import Doc, DocVersion, File, Folder, Project, ProjectMember, User, get_db, utcnow

router = APIRouter()

ROLES = ("OWNER", "ADMIN", "EDITOR", "VIEWER")
VERSION_KEEP = 50  # 版本只保留最近 50 条(§7.4、§8)


# ---------- 序列化 ----------

def _project_json(db: DbSession, p: Project, user: User) -> dict:
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "isPersonal": bool(p.is_personal),
        "createdAt": p.created_at.isoformat(),
        "myRole": project_role(db, p.id, user),
        "memberCount": db.query(ProjectMember).filter_by(project_id=p.id).count(),
        "docCount": db.query(Doc).filter_by(project_id=p.id).filter(Doc.deleted_at.is_(None)).count(),
    }


# ---------- 7.3 项目 ----------

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
    # 文档未定义项目文件的处置;按最简实现:一并删除项目空间 files/folders 记录
    # (物理文件保留在磁盘,§15 明确不做物理清理)
    db.query(File).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(Folder).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.query(ProjectMember).filter_by(project_id=project_id).delete(synchronize_session=False)
    db.delete(p)
    db.commit()
    return {"ok": True}


# ---------- 7.3 成员 ----------

@router.get("/api/projects/{project_id}/members")
def list_members(project_id: str, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                 db: DbSession = Depends(get_db)):
    rows = (db.query(ProjectMember, User).join(User, User.id == ProjectMember.user_id)
            .filter(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.created_at.asc()).all())
    return [{
        "userId": m.user_id, "role": m.role,
        "user": {"id": u.id, "email": u.email, "name": u.name,
                 "isDisabled": bool(u.is_disabled), "avatarColor": avatar_color(u.id)},
    } for m, u in rows]


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
    """项目回收站:文档 + 云空间文件 + 文件夹统一返回(均按删除时间倒序)"""
    docs = (db.query(Doc).filter_by(project_id=project_id).filter(Doc.deleted_at.isnot(None))
            .order_by(Doc.deleted_at.desc()).limit(500).all())
    files = (db.query(File).filter_by(project_id=project_id).filter(File.deleted_at.isnot(None))
             .order_by(File.deleted_at.desc()).limit(500).all())
    folders = (db.query(Folder).filter_by(project_id=project_id).filter(Folder.deleted_at.isnot(None))
               .order_by(Folder.deleted_at.desc()).limit(500).all())
    return {
        "docs": [{"id": d.id, "title": d.title, "deletedAt": d.deleted_at.isoformat()}
                 for d in docs],
        "files": [{"id": f.id, "name": f.name, "mime": f.mime, "size": f.size,
                   "deletedAt": f.deleted_at.isoformat()} for f in files],
        "folders": [{"id": f.id, "name": f.name, "deletedAt": f.deleted_at.isoformat()}
                    for f in folders],
    }


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
def get_doc(dep=Depends(require_doc_role("VIEWER"))):
    _, doc = dep
    return {"id": doc.id, "projectId": doc.project_id, "parentId": doc.parent_id,
            "title": doc.title, "content": doc.content, "version": doc.version,
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


def save_doc_content(db: DbSession, doc: Doc, content: str, user_id: str,
                     label: str = "覆盖前") -> tuple[bool, int]:
    """内容有变化时先把旧内容存为 DocVersion,版本只保留最近 VERSION_KEEP 条;version+=1。

    返回 (是否发生变化, 当前版本号)。REST 与 WebSocket 两条写入路径共用,
    避免版本保留策略在两处漂移。**不提交事务**,由调用方决定提交时机。
    """
    if doc.content == content:
        return False, doc.version
    db.add(DocVersion(doc_id=doc.id, content=doc.content, label=label, created_by=user_id))
    doc.content = content
    doc.version += 1
    doc.updated_by = user_id
    doc.updated_at = utcnow()
    db.flush()
    ids = [v.id for v in db.query(DocVersion.id).filter_by(doc_id=doc.id)
           .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).all()]
    if len(ids) > VERSION_KEEP:
        db.query(DocVersion).filter(DocVersion.id.in_(ids[VERSION_KEEP:])) \
            .delete(synchronize_session=False)
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
    # 等价于对该版本内容执行 PUT content(§7.4)
    return _save_content(db, doc, v.content, ctx.user.id, label="覆盖前")
