"""项目与成员(构建文档 §7.3):项目 CRUD / 发现广场 / 成员管理 / 自助退出。

统计与序列化的单一来源也在这里:project_json(成员视角)、batch_stats(批量聚合,
列表页 N 个项目只花常数条查询)、visible_project_ids(可见项目集合)。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, avatar_color, bad_request, current_user, err,
                  get_project_or_404, int_field, is_project_member,
                  is_project_owner_or_admin, pat_write_guard, project_role,
                  require_project_role, str_field)
from models import (Doc, DocVersion, File, Folder, Project, ProjectMember,
                    User, file_abspath, get_db)
from trash import commit_and_unlink

router = APIRouter(dependencies=[Depends(pat_write_guard)])

ROLES = ("OWNER", "ADMIN", "EDITOR", "VIEWER")


# ---------- 统计与序列化(单一来源) ----------

def batch_stats(db: DbSession, ids) -> dict:
    """项目统计批量聚合:memberCount / docCount / lastUpdatedAt / storageBytes。

    项目列表页与列表内每个项目的 _project_json 旧实现是 N+1(每项目 6 条查询);
    管理后台项目总览又另写了一套批量版 —— 同一组指标两套实现、两套正确性维护。
    现在只有这一份,4 条 GROUP BY,不论项目数。"""
    ids = list(ids)
    if not ids:
        return {}
    member_counts = dict(db.query(ProjectMember.project_id, func.count())
                         .filter(ProjectMember.project_id.in_(ids))
                         .group_by(ProjectMember.project_id).all())
    doc_agg = {pid: (cnt, last) for pid, cnt, last in
               (db.query(Doc.project_id, func.count(), func.max(Doc.updated_at))
                .filter(Doc.project_id.in_(ids), Doc.deleted_at.is_(None))
                .group_by(Doc.project_id).all())}
    file_agg = {pid: (sz, last) for pid, sz, last in
                (db.query(File.project_id, func.coalesce(func.sum(File.size), 0),
                          func.max(File.created_at))
                 .filter(File.project_id.in_(ids), File.deleted_at.is_(None))
                 .group_by(File.project_id).all())}
    out = {}
    for pid in ids:
        d_cnt, d_last = doc_agg.get(pid, (0, None))
        f_sz, f_last = file_agg.get(pid, (0, None))
        lasts = [x for x in (d_last, f_last) if x is not None]
        out[pid] = {
            "memberCount": member_counts.get(pid, 0),
            "docCount": d_cnt,
            "lastUpdatedAt": max(lasts) if lasts else None,
            "storageBytes": int(f_sz),
        }
    return out


def project_json(db: DbSession, p: Project, user: User, stats: dict | None = None) -> dict:
    """项目序列化(成员视角)。stats 传 batch_stats 的对应项可避免列表页 N+1;
    权限(myRole/isMember)仍逐项目经 project_role —— 它是全站权限的咽喉,
    不能为了批量而在第二处复制其语义。"""
    s = stats if stats is not None else batch_stats(db, [p.id]).get(p.id, {})
    last = s.get("lastUpdatedAt")
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
        "memberCount": s.get("memberCount", 0),
        "docCount": s.get("docCount", 0),
    }


def visible_project_ids(db: DbSession, user: User, *, site_wide: bool = False) -> set:
    """我可见的项目集合(单一来源,防各端点各拼一份后语义漂移)。

    基础 = 我参加的项目(个人空间天然在内)。site_wide=True 时并入公开项目、
    管理员再覆盖全部非个人项目 —— 即搜索的"全站能见";False 是最近动态的
    "我的工作台"。两者的差异是有意设计,勿对齐。"""
    ids = {m.project_id for m in
           db.query(ProjectMember).filter_by(user_id=user.id).all()}
    if site_wide:
        ids |= {p for (p,) in db.query(Project.id)
                .filter(Project.visibility == "public", Project.is_personal.is_(False)).all()}
        if user.is_admin:
            ids |= {p for (p,) in db.query(Project.id)
                    .filter(Project.is_personal.is_(False)).all()}
    return ids


def _member_json(user: User, role: str) -> dict:
    return {"userId": user.id, "role": role,
            "user": {"id": user.id, "email": user.email, "name": user.name,
                     "isDisabled": bool(user.is_disabled), "avatarColor": avatar_color(user.id)}}


# ---------- 项目 ----------

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
    stats = batch_stats(db, [p.id for p in rows])
    items = [project_json(db, p, ctx.user, stats.get(p.id)) for p in rows]
    # 无活动时间的排最后(用空串比较,避免 None 参与排序)
    items.sort(key=lambda x: x.get("lastUpdatedAt") or "", reverse=True)
    return items


@router.post("/api/projects")
def create_project(payload: dict, ctx: AuthContext = Depends(current_user),
                   db: DbSession = Depends(get_db)):
    name = str_field(payload, "name", 100, required=True)
    description = str_field(payload, "description", 5000)
    p = Project(name=name, description=description, created_by=ctx.user.id)
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=ctx.user.id, role="OWNER"))
    db.commit()
    return project_json(db, p, ctx.user)


@router.get("/api/projects")
def list_projects(all: str = "", ctx: AuthContext = Depends(current_user),
                  db: DbSession = Depends(get_db)):
    ids = visible_project_ids(db, ctx.user, site_wide=all == "1" and ctx.user.is_admin)
    rows = (db.query(Project).filter(Project.id.in_(ids))
            .order_by(Project.is_personal.desc(), Project.created_at.asc()).all()
            if ids else [])
    stats = batch_stats(db, [p.id for p in rows])
    return [project_json(db, p, ctx.user, stats.get(p.id)) for p in rows]


@router.get("/api/projects/{project_id}")
def get_project(project_id: int, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                db: DbSession = Depends(get_db)):
    return project_json(db, get_project_or_404(db, project_id), ctx.user)


@router.patch("/api/projects/{project_id}")
def patch_project(project_id: int, payload: dict,
                  ctx: AuthContext = Depends(require_project_role("ADMIN")),
                  db: DbSession = Depends(get_db)):
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
    return project_json(db, p, ctx.user)


@router.delete("/api/projects/{project_id}")
def delete_project(project_id: int, ctx: AuthContext = Depends(require_project_role("ADMIN")),
                   db: DbSession = Depends(get_db)):
    p = get_project_or_404(db, project_id)
    if p.is_personal:
        err(403, "FORBIDDEN", "个人空间不可删除")
    # 删除需要 OWNER 级管辖权(§auth.is_project_owner_or_admin):真所有者,或全局管理员。
    # 依赖只能取到 ADMIN(全局管理员在项目上映射为 ADMIN),所以这里显式判一次;
    # 否则唯一所有者失联的项目既不能转移所有权也不能删除,永久死锁。
    if not is_project_owner_or_admin(db, project_id, ctx.user):
        err(403, "FORBIDDEN", "仅项目所有者或全局管理员可删除项目")
    # 先删 doc_versions(docs in project)、docs、members,再删项目(手动处理 FK)
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
    commit_and_unlink(db, paths)
    return {"ok": True, "removedFiles": len(paths)}


# ---------- 成员 ----------

@router.get("/api/projects/{project_id}/members")
def list_members(project_id: int, ctx: AuthContext = Depends(require_project_role("VIEWER")),
                 db: DbSession = Depends(get_db)):
    rows = (db.query(ProjectMember, User).join(User, User.id == ProjectMember.user_id)
            .filter(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.created_at.asc()).all())
    # 公开项目的"访客"(非成员)不返回邮箱与禁用状态:VIEWER 的含义是"能读内容",
    # 不该连带把全组成员邮箱和账号状态暴露给本实例任意登录用户。
    # 真成员照旧;全局管理员是信任根(用户管理里本就看得到全部邮箱),
    # 且接管失联项目时需要核对人选,同样放开。
    member = is_project_member(db, project_id, ctx.user) or ctx.user.is_admin
    out = []
    for m, u in rows:
        d = {"id": u.id, "name": u.name, "avatarColor": avatar_color(u.id)}
        if member:
            d["email"] = u.email
            d["isDisabled"] = bool(u.is_disabled)
        out.append({"userId": m.user_id, "role": m.role, "user": d})
    return out


@router.post("/api/projects/{project_id}/members")
def add_member(project_id: int, payload: dict,
               ctx: AuthContext = Depends(require_project_role("ADMIN")),
               db: DbSession = Depends(get_db)):
    p = get_project_or_404(db, project_id)
    if p.is_personal:
        err(403, "FORBIDDEN", "个人空间不可管理成员")
    user_id = int_field(payload, "userId", required=True)
    role = str_field(payload, "role", 10, default="VIEWER") or "VIEWER"
    if role not in ROLES:
        bad_request("role 必须为 OWNER/ADMIN/EDITOR/VIEWER")
    # 授予 OWNER 需要 OWNER 级管辖权(is_project_owner_or_admin):项目 ADMIN 不得
    # 自我提权——升成 OWNER 后就能删项目;全局管理员显式豁免(信任根),否则唯一
    # 所有者失联的项目无人能接管。
    if role == "OWNER" and not is_project_owner_or_admin(db, project_id, ctx.user):
        err(403, "FORBIDDEN", "只有项目所有者才能授予所有者")
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        err(404, "NOT_FOUND", "用户不存在")
    if db.query(ProjectMember).filter_by(project_id=project_id, user_id=user.id).first():
        err(409, "CONFLICT", "该用户已是项目成员")
    m = ProjectMember(project_id=project_id, user_id=user.id, role=role)
    db.add(m)
    db.commit()
    return _member_json(user, role)


def _owner_count(db: DbSession, project_id: int) -> int:
    return db.query(ProjectMember).filter_by(project_id=project_id, role="OWNER").count()


@router.patch("/api/projects/{project_id}/members/{user_id}")
def patch_member(project_id: int, user_id: int, payload: dict,
                 ctx: AuthContext = Depends(require_project_role("ADMIN")),
                 db: DbSession = Depends(get_db)):
    p = get_project_or_404(db, project_id)
    if p.is_personal:
        err(403, "FORBIDDEN", "个人空间不可管理成员")
    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).first()
    if not m:
        err(404, "NOT_FOUND", "成员不存在")
    role = str_field(payload, "role", 10, required=True)
    if role not in ROLES:
        bad_request("role 必须为 OWNER/ADMIN/EDITOR/VIEWER")
    # 同 add_member:授予 OWNER 需要 OWNER 级管辖权(项目 ADMIN 不得自我提权,
    # 全局管理员豁免——见 auth.is_project_owner_or_admin)
    if role == "OWNER" and m.role != "OWNER" \
            and not is_project_owner_or_admin(db, project_id, ctx.user):
        err(403, "FORBIDDEN", "只有项目所有者才能授予所有者")
    # 最后一个 OWNER 降级保护(§7.3)
    if m.role == "OWNER" and role != "OWNER" and _owner_count(db, project_id) <= 1:
        err(409, "CONFLICT", "项目至少需要一名所有者")
    m.role = role
    db.commit()
    return {"userId": user_id, "role": role}


@router.delete("/api/projects/{project_id}/members/{user_id}")
def remove_member(project_id: int, user_id: int,
                  ctx: AuthContext = Depends(require_project_role("ADMIN")),
                  db: DbSession = Depends(get_db)):
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


@router.post("/api/projects/{project_id}/leave")
def leave_project(project_id: int,
                  ctx: AuthContext = Depends(require_project_role("VIEWER")),
                  db: DbSession = Depends(get_db)):
    """成员自助退出。判定链与 remove_member 对齐:个人空间 403 → 非成员 404 → 末代 OWNER 409。"""
    p = get_project_or_404(db, project_id)
    if p.is_personal:
        err(403, "FORBIDDEN", "个人空间不可退出")
    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=ctx.user.id).first()
    if not m:
        err(404, "NOT_FOUND", "你不是该项目成员")
    if m.role == "OWNER" and _owner_count(db, project_id) <= 1:
        err(409, "CONFLICT", "项目至少需要一名所有者,请先转让所有权")
    db.delete(m)
    db.commit()
    return {"ok": True}
