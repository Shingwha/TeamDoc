"""认证与权限(构建文档 §6、§7.1、§7.2)。

- 密码:hashlib.scrypt(纯标准库)
- 会话:Cookie `td_sid`(HttpOnly + SameSite=Lax)
- PAT:`tdp_` 前缀,库存 sha256
- 权限依赖:current_user / require_write / require_admin / require_project_role / require_doc_role

不做两步验证(TOTP):内网自部署 + 30 人规模下,它防的"密码泄露后的二次验证"
价值低(内部人有自己的账号),而账号管控靠"禁用用户"与可吊销的 PAT 覆盖。
见 HANDOFF §4.16。
"""
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session as DbSession

from models import (AuthSession, Doc, DocVersion, File, Folder, Pat, Project,
                    ProjectMember, User, get_db, utcnow)

router = APIRouter()

SESSION_COOKIE = "td_sid"
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))
SESSION_TTL = SESSION_TTL_DAYS * 86400
# 勾选"记住我"时的会话有效期。**注意:不勾选仍是上面这档** ——
# 默认值必须与旧行为一致,否则已习惯"关浏览器不用重登"的人会被莫名登出。
REMEMBER_TTL_DAYS = int(os.environ.get("REMEMBER_TTL_DAYS", "30"))
REMEMBER_TTL = REMEMBER_TTL_DAYS * 86400

ROLE_RANK = {"OWNER": 3, "ADMIN": 2, "EDITOR": 1, "VIEWER": 0}

# 头像颜色:文档 §5 未建 avatar_color 字段,但 §7.1 登录响应要求 avatarColor;
# 按最简实现:由 user id 哈希在固定调色板中取色(确定性,不落库)
_PALETTE = ["#3370ff", "#7c3aed", "#db2777", "#ea580c", "#16a34a", "#0891b2", "#ca8a04", "#1f6feb"]


# ---------- 错误与校验工具 ----------

def err(status: int, code: str, message: str):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def bad_request(message: str):
    err(400, "VALIDATION", message)


def int_field(payload: dict, key: str, required: bool = False, default: int | None = None) -> int | None:
    """取整数字段:接受 JSON 数字或数字字符串(前端从 dataset 取出的值是字符串)"""
    v = payload.get(key)
    if v is None or v == "":
        if required:
            bad_request(f"{key} 不能为空")
        return default
    if isinstance(v, bool):
        bad_request(f"{key} 必须为整数")
    try:
        return int(v)
    except (TypeError, ValueError):
        bad_request(f"{key} 必须为整数")


def opt_int(value, key: str = "id") -> int | None:
    """可选整数:None/空 → None;数字或数字字符串 → int;其余 400。
    用于 payload 里的可选 id 字段(parentId/folderId 等)。"""
    if value is None or value == "":
        return None
    return int_field({key: value}, key)


def str_field(payload: dict, key: str, max_len: int, required: bool = False, default: str = "") -> str:
    """取字符串字段并做类型/空值/长度校验(§14.10)"""
    v = payload.get(key)
    if v is None:
        if required:
            bad_request(f"{key} 不能为空")
        return default
    if not isinstance(v, str):
        bad_request(f"{key} 必须为字符串")
    v = v.strip()
    if required and not v:
        bad_request(f"{key} 不能为空")
    return v[:max_len]


def check_password_strength(password: str):
    if len(password) < 8:
        bad_request("密码长度至少 8 位")


def check_email(email: str):
    if not email or "@" not in email or len(email) > 255:
        bad_request("邮箱格式不正确")


# ---------- 密码(§6.1) ----------

def hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk.hex(), digest_hex)


# ---------- 序列化 ----------

def avatar_color(user_id: int) -> str:
    h = int(hashlib.md5(str(user_id).encode("utf-8")).hexdigest(), 16)
    return _PALETTE[h % len(_PALETTE)]


def create_personal_project(db: DbSession, user: User):
    """每个用户创建时自动拥有的"个人空间"项目(is_personal=True,本人 OWNER)。

    需求变更:取消独立个人云空间,统一为个人项目。调用前 user 需已 flush(有 id)。
    """
    p = Project(name="个人空间", is_personal=True, created_by=user.id)
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="OWNER"))


def user_json(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "isAdmin": bool(u.is_admin),
        "avatarColor": avatar_color(u.id),
    }


# ---------- 认证上下文(§6.3) ----------

@dataclass
class AuthContext:
    user: User
    via: str  # "web" / "pat"
    scopes: list = field(default_factory=list)
    session_token: str | None = None
    pat_id: int | None = None
    # 本次会话的总生命周期(秒)。续期与 cookie max_age 都按它走,而不是按全局 SESSION_TTL ——
    # 否则 30 天的"记住我"会话会在滑动续期时被砍回 7 天。PAT 认证为 None。
    session_lifetime: float | None = None


def _pat_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(request: Request, db: DbSession) -> AuthContext | None:
    """解析顺序:PAT(Bearer tdp_)→ Cookie 会话"""
    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer "):
        token = authz[7:].strip()
        if token.startswith("tdp_"):
            pat = db.query(Pat).filter(Pat.token_hash == _pat_hash(token), Pat.revoked_at.is_(None)).first()
            if not pat:
                return None
            user = db.get(User, pat.user_id)
            if not user or user.is_disabled:
                return None
            # 每次认证刷新 last_used_at(距上次 >60s 才写,§6.5)
            now = utcnow()
            if pat.last_used_at is None or (now - pat.last_used_at).total_seconds() > 60:
                pat.last_used_at = now
                db.commit()
            return AuthContext(user=user, via="pat", scopes=pat.scopes.split(","), pat_id=pat.id)
        return None

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sess = db.get(AuthSession, token)
        if sess and sess.expires_at > utcnow():
            user = db.get(User, sess.user_id)
            if user and not user.is_disabled:
                # 会话自身的生命周期(记住我 30 天 / 普通 7 天)。用 expires_at - created_at 推导,
                # 而不是读全局 SESSION_TTL:后者会把长会话续短,且无需为此新增数据库列。
                lifetime = (sess.expires_at - sess.created_at).total_seconds()
                # 剩余寿命 < 1/2 时滑动续期(§6.2)
                if (sess.expires_at - utcnow()).total_seconds() < lifetime / 2:
                    sess.expires_at = utcnow() + timedelta(seconds=lifetime)
                    db.commit()
                return AuthContext(user=user, via="web", scopes=["read", "write"],
                                   session_token=token, session_lifetime=lifetime)
    return None


def current_user(request: Request, db: DbSession = Depends(get_db)) -> AuthContext:
    ctx = authenticate(request, db)
    if ctx is None:
        err(401, "UNAUTHORIZED", "未登录")
    request.state.auth = ctx
    return ctx


def require_write(request: Request, ctx: AuthContext = Depends(current_user)) -> AuthContext:
    """非 GET 方法且 PAT scopes 无 write → 403(§6.3)"""
    if request.method != "GET" and ctx.via == "pat" and "write" not in ctx.scopes:
        err(403, "FORBIDDEN", "令牌缺少 write 权限")
    return ctx


def require_admin(ctx: AuthContext = Depends(current_user)) -> AuthContext:
    if not ctx.user.is_admin:
        err(403, "FORBIDDEN", "需要管理员权限")
    return ctx


def require_admin_write(request: Request,
                        ctx: AuthContext = Depends(require_admin)) -> AuthContext:
    """管理员 + 写 scope。管理端的写操作必须用它,而不是裸 require_admin。

    为什么单独一个依赖:require_admin 只看 is_admin,**完全不看 PAT scope**。
    于是"管理员账号签发的只读令牌"能调用全部管理端写接口 —— 实测可用它建出
    新的管理员账号再登录,只读令牌直接提权成完整管理员。项目内其它写端点
    (docs/files)都过了 require_write/require_write_ctx,只有用户管理与 admin.py
    漏了,这是实现不一致而非设计取舍。
    """
    if request.method != "GET" and ctx.via == "pat" and "write" not in ctx.scopes:
        err(403, "FORBIDDEN", "令牌缺少 write 权限")
    return ctx


def require_write_ctx(ctx: AuthContext, db: DbSession | None = None) -> AuthContext:
    """PAT 缺 write scope → 403。

    供"已带角色依赖、无法再叠加 require_write"的路由使用(角色校验通过后仍需
    校验 PAT scope,否则只读令牌可执行写操作)。
    """
    if ctx.via == "pat" and "write" not in ctx.scopes:
        err(403, "FORBIDDEN", "令牌缺少 write 权限")
    return ctx


def project_role(db: DbSession, project_id: int, user: User) -> str | None:
    """成员角色优先;否则 is_admin → ADMIN;否则公开项目 → VIEWER;否则 None(§6.3)

    **这是全站权限的咽喉**:文档树/文档读写/云空间/搜索/WS 都经这里判定。
    公开项目在此返回 VIEWER 一处,即可让非成员获得正确的只读能力 ——
    WS 的 readonly 也自动正确(它在 ROLE_RANK 上比 EDITOR 低)。

    注意"公开"**不产生成员关系**:调用方若需要区分"真成员"与"公开可见的访客",
    必须另查 is_member()(前端据此决定是否渲染写操作)。
    """
    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user.id).first()
    if m:
        return m.role
    p = db.get(Project, project_id)
    # 个人空间对管理员也**不开放**。产品上写死了"个人空间永远私有"(见 models.Project
    # 的注释),而列表与搜索都已排除他人的个人空间;若这里给管理员返回 ADMIN,
    # 按 id 直连就能读到别人的私有草稿,与承诺矛盾。管理员自己的个人空间在上面的
    # 成员查询里已作为 OWNER 命中,不受影响。
    if p is not None and p.is_personal and p.created_by != user.id:
        return None
    if user.is_admin:
        return "ADMIN"
    if p is not None and not p.is_personal and p.visibility == "public":
        return "VIEWER"
    return None


def is_project_member(db: DbSession, project_id: int, user: User) -> bool:
    """真实成员关系(不含"管理员"与"公开项目的访客")。
    前端需要它来区分"能写"与"只是看得到":仅凭 project_role 非空会让公开项目的
    访客拿到 VIEWER,若前端据此渲染出写按钮,点了就是 403。"""
    return db.query(ProjectMember).filter_by(project_id=project_id,
                                             user_id=user.id).first() is not None


def is_project_owner_or_admin(db: DbSession, project_id: int, user: User) -> bool:
    """OWNER 级管辖权:项目真实所有者,或全局管理员。
    全局管理员是信任根(本就能删用户、看全量数据、下载备份),在授 OWNER、删项目
    这类"接管"语义上不受项目内角色约束——否则唯一所有者失联/被禁用的项目会永久
    死锁(所有权移不动、项目删不掉)。这不重新引入"ADMIN 自我提权"风险:该风险
    的主体是项目内角色(他们仍被 project_role 判定拦住);个人空间的保护(§4.2)
    在端点里先于本判定。前端对应 UI.canOwn。"""
    if user.is_admin:
        return True
    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user.id,
                                          role="OWNER").first()
    return m is not None


def ensure_project_role(db: DbSession, ctx: AuthContext, project_id: int,
                        required: str) -> str | None:
    """按项目角色鉴权:不足则 403,返回实际角色(供需要区分的调用方使用)"""
    role = project_role(db, project_id, ctx.user)
    if role is None or ROLE_RANK.get(role, -1) < ROLE_RANK[required]:
        err(403, "FORBIDDEN", f"需要 {required} 及以上权限")
    return role


def get_project_or_404(db: DbSession, project_id: int) -> Project:
    p = db.get(Project, project_id)
    if not p:
        err(404, "NOT_FOUND", "项目不存在")
    return p


def require_project_role(required: str):
    def dep(project_id: int, ctx: AuthContext = Depends(current_user),
            db: DbSession = Depends(get_db)) -> AuthContext:
        # 先确认项目存在:project_role 对全局管理员一律返回 ADMIN,若不在这里
        # 兜住,管理员访问不存在的项目会拿到 200 空结果(而不是 404),
        # 与其它端点的语义不一致,也让"项目是否存在"变得可探测
        get_project_or_404(db, project_id)
        ensure_project_role(db, ctx, project_id, required)
        return ctx
    return dep


def require_doc_role(required: str):
    """按文档路径参数鉴权:文档不存在/已删 → 404;再校验项目角色"""
    def dep(doc_id: int, ctx: AuthContext = Depends(current_user),
            db: DbSession = Depends(get_db)) -> tuple:
        doc = db.get(Doc, doc_id)
        if not doc or doc.deleted_at is not None:
            err(404, "NOT_FOUND", "文档不存在")
        ensure_project_role(db, ctx, doc.project_id, required)
        return ctx, doc
    return dep


# ---------- 会话辅助 ----------

def _create_session(db: DbSession, user_id: int, ttl: int = SESSION_TTL) -> str:
    token = secrets.token_hex(32)
    db.add(AuthSession(token=token, user_id=user_id,
                       expires_at=utcnow() + timedelta(seconds=ttl)))
    db.commit()
    return token


def _set_session_cookie(response: Response, token: str, max_age: int = SESSION_TTL):
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age,
                        httponly=True, samesite="lax", path="/")


# ---------- 7.1 认证路由 ----------

@router.get("/api/auth/status")
def auth_status(db: DbSession = Depends(get_db)):
    try:
        bootstrapped = db.query(User).count() > 0
        return {"bootstrapped": bootstrapped, "dbReady": True}
    except Exception:
        return {"bootstrapped": False, "dbReady": False}


@router.post("/api/auth/bootstrap")
def bootstrap(payload: dict, response: Response, db: DbSession = Depends(get_db)):
    if db.query(User).count() > 0:
        err(403, "FORBIDDEN", "系统已初始化")
    email = str_field(payload, "email", 255, required=True).lower()
    check_email(email)
    name = str_field(payload, "name", 50, required=True)
    password = payload.get("password")
    if not isinstance(password, str):
        bad_request("password 必须为字符串")
    check_password_strength(password)
    user = User(email=email, name=name, password_hash=hash_password(password), is_admin=True)
    db.add(user)
    db.flush()
    create_personal_project(db, user)  # 同一事务创建个人空间项目
    db.commit()
    token = _create_session(db, user.id)
    _set_session_cookie(response, token)
    return {"user": user_json(user)}


@router.post("/api/auth/login")
def login(payload: dict, response: Response, db: DbSession = Depends(get_db)):
    email = str_field(payload, "email", 255, required=True).lower()
    password = payload.get("password")
    if not isinstance(password, str):
        bad_request("password 不能为空")
    user = db.query(User).filter_by(email=email).first()
    if not user or not verify_password(password, user.password_hash):
        err(401, "UNAUTHORIZED", "邮箱或密码错误")
    if user.is_disabled:
        err(401, "UNAUTHORIZED", "账号已被禁用")
    # 记住我 → 更长会话;严格 is True,避免客户端传 "false"/1 这类真值被误判
    ttl = REMEMBER_TTL if payload.get("remember") is True else SESSION_TTL
    token = _create_session(db, user.id, ttl)
    _set_session_cookie(response, token, ttl)
    return {"user": user_json(user)}


@router.post("/api/auth/logout")
def logout(response: Response, ctx: AuthContext = Depends(current_user),
           db: DbSession = Depends(get_db)):
    if ctx.session_token:
        db.query(AuthSession).filter_by(token=ctx.session_token).delete()
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
def auth_me(response: Response, ctx: AuthContext = Depends(current_user)):
    # web 会话:让浏览器 cookie 与服务端滑动会话同步续期。
    # 服务端在 authenticate 里已按生命周期滑动,若不回刷 cookie,浏览器会在登录满 30 天时
    # 先丢掉 cookie —— 表现为"服务端会话还有效,用户却被登出"。
    if ctx.via == "web" and ctx.session_token and ctx.session_lifetime:
        _set_session_cookie(response, ctx.session_token, int(ctx.session_lifetime))
    return {
        "user": user_json(ctx.user),
        "auth": {"via": ctx.via, "scopes": ctx.scopes},
    }


@router.post("/api/users/me/password")
def change_my_password(payload: dict, ctx: AuthContext = Depends(require_write),
                       db: DbSession = Depends(get_db)):
    old = payload.get("oldPassword")
    new = payload.get("newPassword")
    if not isinstance(old, str) or not isinstance(new, str):
        bad_request("oldPassword/newPassword 必须为字符串")
    if not verify_password(old, ctx.user.password_hash):
        err(403, "FORBIDDEN", "原密码错误")
    check_password_strength(new)
    user = db.get(User, ctx.user.id)
    user.password_hash = hash_password(new)
    # 改密即轮换:吊销该用户**其它**会话(保留当前这条,否则用户会被自己踢下线)。
    # PAT 不动 —— 用户自愿改密,CLI/自动化不该被牵连;若请求由 PAT 发起,则没有
    # "当前会话"可留,全部会话失效正是期望行为。
    q = db.query(AuthSession).filter(AuthSession.user_id == user.id)
    if ctx.session_token:
        q = q.filter(AuthSession.token != ctx.session_token)
    revoked = q.delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "revokedSessions": revoked}


# ---------- 6.5 PAT ----------

@router.get("/api/auth/pats")
def list_pats(ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    rows = (db.query(Pat).filter_by(user_id=ctx.user.id)
            .filter(Pat.revoked_at.is_(None)).order_by(Pat.created_at.desc()).all())
    return [{
        "id": p.id, "name": p.name, "scopes": p.scopes,
        "lastUsedAt": p.last_used_at.isoformat() if p.last_used_at else None,
        "createdAt": p.created_at.isoformat(),
    } for p in rows]


@router.post("/api/auth/pats")
def create_pat(payload: dict, ctx: AuthContext = Depends(require_write),
               db: DbSession = Depends(get_db)):
    # 仅 Web 会话可创建(§6.5),拒绝 PAT 调用
    if ctx.via != "web":
        err(403, "FORBIDDEN", "仅 Web 会话可创建访问令牌")
    name = str_field(payload, "name", 50, required=True)
    scopes = str_field(payload, "scopes", 20, default="read")
    if scopes not in ("read", "read,write"):
        bad_request('scopes 仅支持 "read" 或 "read,write"')
    token = "tdp_" + secrets.token_hex(24)
    pat = Pat(user_id=ctx.user.id, name=name, token_hash=_pat_hash(token), scopes=scopes)
    db.add(pat)
    db.commit()
    # 明文只在创建响应返回一次
    return {"id": pat.id, "name": pat.name, "scopes": pat.scopes, "token": token,
            "createdAt": pat.created_at.isoformat()}


@router.delete("/api/auth/pats/{pat_id}")
def revoke_pat(pat_id: int, ctx: AuthContext = Depends(require_write),
               db: DbSession = Depends(get_db)):
    if ctx.via != "web":
        err(403, "FORBIDDEN", "仅 Web 会话可吊销访问令牌")
    pat = db.query(Pat).filter_by(id=pat_id, user_id=ctx.user.id).first()
    if not pat or pat.revoked_at is not None:
        err(404, "NOT_FOUND", "令牌不存在")
    pat.revoked_at = utcnow()
    db.commit()
    return {"ok": True}


# ---------- 7.2 用户管理(仅 is_admin) ----------

def _admin_list_json(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name,
            "isAdmin": bool(u.is_admin), "isDisabled": bool(u.is_disabled),
            "avatarColor": avatar_color(u.id),
            "createdAt": u.created_at.isoformat()}


def _enabled_admin_count(db: DbSession) -> int:
    return db.query(User).filter_by(is_admin=True, is_disabled=False).count()


def _busy_user_ids(db: DbSession) -> set[str]:
    """返回"产生过内容、因而只能禁用不能删除"的用户 id 集合。

    判定标准刻意取严(宁可多判为"有内容"):删除账号的真正风险不是删掉登录能力,
    而是把他名下的数据变成孤儿 —— 文档的 created_by/updated_by、历史版本、云空间文件、
    项目归属,这些列是 String 而非外键,删了用户不会级联,只会留下指向不存在用户的
    悬空引用。所以只要沾过一样,就归为"有内容"。

    个人空间不算"内容":它是建号时自动创建的容器。但个人空间里若有东西,那算。
    """
    busy: set[str] = set()
    # 文档:创建过或编辑过都算(updated_by 同样是悬空引用的来源)
    for col in (Doc.created_by, Doc.updated_by, DocVersion.created_by, File.created_by):
        for (uid,) in db.query(col).filter(col.isnot(None)).distinct():
            busy.add(uid)
    # 创建过非个人项目
    for (uid,) in (db.query(Project.created_by)
                   .filter(Project.created_by.isnot(None), Project.is_personal.is_(False))
                   .distinct()):
        busy.add(uid)
    # 是非个人项目的 OWNER(兜住 created_by 为空的早期项目)
    for (uid,) in (db.query(ProjectMember.user_id)
                   .join(Project, Project.id == ProjectMember.project_id)
                   .filter(ProjectMember.role == "OWNER", Project.is_personal.is_(False))
                   .distinct()):
        busy.add(uid)
    # 个人空间非空
    owner_of = {p.id: p.created_by for p in db.query(Project).filter(Project.is_personal.is_(True))}
    if owner_of:
        ids = list(owner_of)
        for model in (Doc, File, Folder):
            for (pid,) in db.query(model.project_id).filter(model.project_id.in_(ids)).distinct():
                busy.add(owner_of[pid])
    return busy


def _deletion_blockers(db: DbSession, user: User) -> list[str]:
    """该账号为什么不能删除;空列表表示可以删。

    删除只对"干净账号"开放 —— 典型场景就是管理员建号时打错了邮箱,想清理掉重来。
    这个限制是有意的:凡产生过数据的账号,行业惯例都是停用而非删除,
    否则"他建的文档归谁"就成了必须回答却答不好的问题。
    """
    if user.id not in _busy_user_ids(db):
        return []
    counts = {
        "文档": db.query(Doc).filter((Doc.created_by == user.id) | (Doc.updated_by == user.id)).count(),
        "文档历史版本": db.query(DocVersion).filter_by(created_by=user.id).count(),
        "云空间文件": db.query(File).filter_by(created_by=user.id).count(),
        "项目": db.query(Project).filter(Project.created_by == user.id,
                                      Project.is_personal.is_(False)).count(),
    }
    parts = [f"{n} 个{d}" for d, n in counts.items() if n]
    if not parts:
        parts = ["其个人空间内已有内容"]
    return parts


@router.delete("/api/users/{user_id}")
def delete_user(user_id: int, ctx: AuthContext = Depends(require_admin_write),
                db: DbSession = Depends(get_db)):
    """删除用户(仅限从未产生数据的"干净"账号)。

    为什么不做无条件删除:文档/历史版本/文件的 created_by 是 String 而非外键,
    删用户不会级联清理,只会留下悬空引用。所以有数据的账号一律引导去"禁用"
    (保留数据、立即失去访问),这也是协作类系统的通行做法。
    """
    user = db.get(User, user_id)
    if not user:
        err(404, "NOT_FOUND", "用户不存在")
    if user.id == ctx.user.id:
        bad_request("不能删除自己的账号")
    if user.is_admin and not user.is_disabled and _enabled_admin_count(db) <= 1:
        err(409, "CONFLICT", "系统至少需要一名可用管理员")
    blockers = _deletion_blockers(db, user)
    if blockers:
        err(409, "CONFLICT",
            "该账号已产生数据(" + "、".join(blockers) + "),删除会留下无主的文档与文件。"
            "请改用「禁用」:账号立即失去访问,数据保留。")

    # 干净账号:清理硬引用后删除。个人空间是建号时自动创建的,一并回收。
    db.query(AuthSession).filter_by(user_id=user.id).delete()
    db.query(Pat).filter_by(user_id=user.id).delete()
    personal_ids = [p.id for p in db.query(Project)
                    .filter_by(created_by=user.id, is_personal=True)]
    if personal_ids:
        db.query(ProjectMember).filter(ProjectMember.project_id.in_(personal_ids)).delete(
            synchronize_session=False)
        db.query(Project).filter(Project.id.in_(personal_ids)).delete(synchronize_session=False)
    db.query(ProjectMember).filter_by(user_id=user.id).delete()
    db.delete(user)
    db.commit()
    return {"ok": True}


@router.get("/api/users")
def list_users(ctx: AuthContext = Depends(require_admin), db: DbSession = Depends(get_db)):
    rows = db.query(User).order_by(User.created_at.asc()).all()
    # canDelete 供管理后台决定是否渲染删除按钮 —— 让按钮只在真能删时出现,
    # 比"点了再报错"友好。一次聚合算出全部"有内容"的用户,不做 N+1 查询。
    busy = _busy_user_ids(db)
    last_admin = _enabled_admin_count(db) <= 1
    out = []
    for u in rows:
        d = _admin_list_json(u)
        d["canDelete"] = (u.id not in busy
                          and not (u.is_admin and not u.is_disabled and last_admin))
        out.append(d)
    return out


@router.get("/api/users/directory")
def user_directory(ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    """同事目录(任意登录用户可读):id / 姓名 / 邮箱 / 头像色。

    存在的理由:此前全站唯一的用户列表是管理后台(仅 is_admin),普通用户能看到的
    用户只有"我已加入项目的成员",而添加成员只能**精确输入邮箱** —— 没人告诉你同事
    邮箱,你就无法与任何人协作,新人入职后除管理员谁也找不到。这是缺失的协作入口,
    不是锦上添花。

    刻意**不含** isAdmin / isDisabled / createdAt:那些是管理后台的字段面,
    没必要暴露给全员。
    """
    rows = (db.query(User).filter_by(is_disabled=False)
            .order_by(User.name.asc(), User.created_at.asc()).all())
    return [{"id": u.id, "name": u.name, "email": u.email,
             "avatarColor": avatar_color(u.id)} for u in rows]


@router.post("/api/users")
def create_user(payload: dict, ctx: AuthContext = Depends(require_admin_write),
                db: DbSession = Depends(get_db)):
    email = str_field(payload, "email", 255, required=True).lower()
    check_email(email)
    name = str_field(payload, "name", 50, required=True)
    password = payload.get("password")
    if not isinstance(password, str):
        bad_request("password 必须为字符串")
    check_password_strength(password)
    is_admin = bool(payload.get("isAdmin", False))
    if db.query(User).filter_by(email=email).first():
        err(409, "CONFLICT", "该邮箱已被注册")
    user = User(email=email, name=name, password_hash=hash_password(password), is_admin=is_admin)
    db.add(user)
    db.flush()
    create_personal_project(db, user)  # 同一事务创建个人空间项目
    db.commit()
    return _admin_list_json(user)


@router.patch("/api/users/{user_id}")
def patch_user(user_id: int, payload: dict, ctx: AuthContext = Depends(require_admin_write),
               db: DbSession = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        err(404, "NOT_FOUND", "用户不存在")
    # 最后一名可用管理员保护(§6.6)
    if user.is_admin and not user.is_disabled:
        losing_admin = ("isAdmin" in payload and not payload["isAdmin"]) or \
                       ("isDisabled" in payload and payload["isDisabled"])
        if losing_admin and _enabled_admin_count(db) <= 1:
            err(409, "CONFLICT", "系统至少需要一名可用管理员")
    if "name" in payload:
        user.name = str_field(payload, "name", 50, required=True)
    if "email" in payload:
        # 邮箱可改:它既是登录名也是唯一约束,建错一个就无法登录、又删不掉这个坑,
        # 必须给管理员一个修的方法(行业惯例:停用之外一定配"编辑")。
        email = str_field(payload, "email", 255, required=True).lower()
        check_email(email)
        if email != user.email:
            taken = db.query(User).filter(User.email == email, User.id != user.id).first()
            if taken:
                err(409, "CONFLICT", "该邮箱已被注册")
            user.email = email
    if "isAdmin" in payload:
        user.is_admin = bool(payload["isAdmin"])
    if "isDisabled" in payload:
        user.is_disabled = bool(payload["isDisabled"])
    if "password" in payload:
        password = payload["password"]
        if not isinstance(password, str):
            bad_request("password 必须为字符串")
        check_password_strength(password)
        user.password_hash = hash_password(password)
        # 重置密码 = 把持有旧凭据的一方踢出去,故该用户的**全部会话与 PAT** 一并失效。
        # 不吊销等于没重置:旧会话(可能正被占用的那个)照旧能读写全站。
        db.query(AuthSession).filter(AuthSession.user_id == user.id).delete(synchronize_session=False)
        now = utcnow()
        for pat in db.query(Pat).filter(Pat.user_id == user.id, Pat.revoked_at.is_(None)).all():
            pat.revoked_at = now
    db.commit()
    return _admin_list_json(user)
