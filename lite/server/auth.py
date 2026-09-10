"""认证与权限(构建文档 §6、§7.1、§7.2)。

- 密码:hashlib.scrypt(纯标准库)
- 会话:Cookie `td_sid`(HttpOnly + SameSite=Lax)
- PAT:`tdp_` 前缀,库存 sha256
- TOTP:pyotp
- 权限依赖:current_user / require_write / require_admin / require_project_role / require_doc_role
"""
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session as DbSession

import models
from models import AuthSession, Doc, Pat, Project, ProjectMember, User, get_db, utcnow

router = APIRouter()

SESSION_COOKIE = "td_sid"
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))
SESSION_TTL = SESSION_TTL_DAYS * 86400

ROLE_RANK = {"OWNER": 3, "ADMIN": 2, "EDITOR": 1, "VIEWER": 0}

# TOTP 门禁白名单(§6.3):开启 TOTP 且会话未验证时仅放行这些路径
_TOTP_WHITELIST = ("/api/auth/totp/", "/api/auth/logout", "/api/auth/me")

# 头像颜色:文档 §5 未建 avatar_color 字段,但 §7.1 登录响应要求 avatarColor;
# 按最简实现:由 user id 哈希在固定调色板中取色(确定性,不落库)
_PALETTE = ["#3370ff", "#7c3aed", "#db2777", "#ea580c", "#16a34a", "#0891b2", "#ca8a04", "#1f6feb"]


# ---------- 错误与校验工具 ----------

def err(status: int, code: str, message: str):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def bad_request(message: str):
    err(400, "VALIDATION", message)


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

def avatar_color(user_id: str) -> str:
    h = int(hashlib.md5(user_id.encode("utf-8")).hexdigest(), 16)
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
        "totpEnabled": bool(u.totp_enabled),
    }


# ---------- 认证上下文(§6.3) ----------

@dataclass
class AuthContext:
    user: User
    via: str  # "web" / "pat"
    scopes: list = field(default_factory=list)
    totp_verified: bool = True
    session_token: str | None = None
    pat_id: str | None = None


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
            # PAT 不受 TOTP 门禁限制(§16.2),视为已验证
            return AuthContext(user=user, via="pat", scopes=pat.scopes.split(","),
                               totp_verified=True, pat_id=pat.id)
        return None

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sess = db.get(AuthSession, token)
        if sess and sess.expires_at > utcnow():
            user = db.get(User, sess.user_id)
            if user and not user.is_disabled:
                # 剩余寿命 < 1/2 时滑动续期(§6.2)
                if (sess.expires_at - utcnow()).total_seconds() < SESSION_TTL / 2:
                    sess.expires_at = utcnow() + timedelta(seconds=SESSION_TTL)
                    db.commit()
                return AuthContext(user=user, via="web", scopes=["read", "write"],
                                   totp_verified=bool(sess.totp_verified), session_token=token)
    return None


def current_user(request: Request, db: DbSession = Depends(get_db)) -> AuthContext:
    ctx = authenticate(request, db)
    if ctx is None:
        err(401, "UNAUTHORIZED", "未登录")
    # TOTP 门禁(§6.3):仅会话登录且未验证时拦截白名单外接口
    if ctx.via == "web" and ctx.user.totp_enabled and not ctx.totp_verified:
        path = request.url.path
        if not any(path.startswith(p) for p in _TOTP_WHITELIST):
            raise HTTPException(status_code=401,
                                detail={"code": "TOTP_REQUIRED", "message": "请先完成两步验证"})
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


def require_write_ctx(ctx: AuthContext, db: DbSession | None = None) -> AuthContext:
    """PAT 缺 write scope → 403。

    供"已带角色依赖、无法再叠加 require_write"的路由使用(角色校验通过后仍需
    校验 PAT scope,否则只读令牌可执行写操作)。
    """
    if ctx.via == "pat" and "write" not in ctx.scopes:
        err(403, "FORBIDDEN", "令牌缺少 write 权限")
    return ctx


def project_role(db: DbSession, project_id: str, user: User) -> str | None:
    """成员角色优先;否则 is_admin → ADMIN;否则 None(§6.3)"""
    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user.id).first()
    if m:
        return m.role
    if user.is_admin:
        return "ADMIN"
    return None


def ensure_project_role(db: DbSession, ctx: AuthContext, project_id: str,
                        required: str) -> str | None:
    """按项目角色鉴权:不足则 403,返回实际角色(供需要区分的调用方使用)"""
    role = project_role(db, project_id, ctx.user)
    if role is None or ROLE_RANK.get(role, -1) < ROLE_RANK[required]:
        err(403, "FORBIDDEN", f"需要 {required} 及以上权限")
    return role


def get_project_or_404(db: DbSession, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if not p:
        err(404, "NOT_FOUND", "项目不存在")
    return p


def require_project_role(required: str):
    def dep(project_id: str, ctx: AuthContext = Depends(current_user),
            db: DbSession = Depends(get_db)) -> AuthContext:
        ensure_project_role(db, ctx, project_id, required)
        return ctx
    return dep


def require_doc_role(required: str):
    """按文档路径参数鉴权:文档不存在/已删 → 404;再校验项目角色"""
    def dep(doc_id: str, ctx: AuthContext = Depends(current_user),
            db: DbSession = Depends(get_db)) -> tuple:
        doc = db.get(Doc, doc_id)
        if not doc or doc.deleted_at is not None:
            err(404, "NOT_FOUND", "文档不存在")
        ensure_project_role(db, ctx, doc.project_id, required)
        return ctx, doc
    return dep


# ---------- 会话辅助 ----------

def _create_session(db: DbSession, user_id: str, totp_verified: bool) -> str:
    token = secrets.token_hex(32)
    db.add(AuthSession(token=token, user_id=user_id, totp_verified=totp_verified,
                       expires_at=utcnow() + timedelta(seconds=SESSION_TTL)))
    db.commit()
    return token


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL,
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
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
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
    token = _create_session(db, user.id, totp_verified=True)
    _set_session_cookie(response, token)
    return {"user": user_json(user)}


@router.post("/api/auth/login")
def login(payload: dict, response: Response, db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    email = str_field(payload, "email", 255, required=True).lower()
    password = payload.get("password")
    if not isinstance(password, str):
        bad_request("password 不能为空")
    user = db.query(User).filter_by(email=email).first()
    if not user or not verify_password(password, user.password_hash):
        err(401, "UNAUTHORIZED", "邮箱或密码错误")
    if user.is_disabled:
        err(401, "UNAUTHORIZED", "账号已被禁用")
    if user.totp_enabled:
        code = str_field(payload, "totpCode", 10)
        if not code:
            raise HTTPException(status_code=401, detail={
                "code": "TOTP_REQUIRED", "message": "该账号已开启两步验证,请提供动态验证码"})
        totp = pyotp.TOTP(user.totp_secret)
        if not totp.verify(code, valid_window=1):
            err(401, "UNAUTHORIZED", "动态验证码错误")
    token = _create_session(db, user.id, totp_verified=True)
    _set_session_cookie(response, token)
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
def auth_me(ctx: AuthContext = Depends(current_user)):
    return {
        "user": user_json(ctx.user),
        "auth": {"via": ctx.via, "scopes": ctx.scopes, "totpVerified": ctx.totp_verified},
    }


@router.post("/api/users/me/password")
def change_my_password(payload: dict, ctx: AuthContext = Depends(require_write),
                       db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    old = payload.get("oldPassword")
    new = payload.get("newPassword")
    if not isinstance(old, str) or not isinstance(new, str):
        bad_request("oldPassword/newPassword 必须为字符串")
    if not verify_password(old, ctx.user.password_hash):
        err(403, "FORBIDDEN", "原密码错误")
    check_password_strength(new)
    user = db.get(User, ctx.user.id)
    user.password_hash = hash_password(new)
    db.commit()
    return {"ok": True}


# ---------- 6.4 TOTP ----------

@router.post("/api/auth/totp/setup")
def totp_setup(payload: dict, ctx: AuthContext = Depends(require_write),
               db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    password = payload.get("password")
    if not isinstance(password, str) or not verify_password(password, ctx.user.password_hash):
        err(403, "FORBIDDEN", "密码错误")
    user = db.get(User, ctx.user.id)
    secret = pyotp.random_base32()
    user.totp_secret = secret  # 生成 secret 存 user(不启用)
    db.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="TeamDoc")
    return {"secret": secret, "uri": uri}


@router.post("/api/auth/totp/enable")
def totp_enable(payload: dict, ctx: AuthContext = Depends(require_write),
                db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    code = str_field(payload, "code", 10, required=True)
    user = db.get(User, ctx.user.id)
    if not user.totp_secret:
        bad_request("请先调用 totp/setup 生成密钥")
    if not pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
        err(400, "VALIDATION", "动态验证码错误")
    user.totp_enabled = True
    if ctx.session_token:
        sess = db.get(AuthSession, ctx.session_token)
        if sess:
            sess.totp_verified = True
    db.commit()
    return {"ok": True}


@router.post("/api/auth/totp/disable")
def totp_disable(payload: dict, ctx: AuthContext = Depends(require_write),
                 db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
    password = payload.get("password")
    if not isinstance(password, str) or not verify_password(password, ctx.user.password_hash):
        err(403, "FORBIDDEN", "密码错误")
    user = db.get(User, ctx.user.id)
    user.totp_secret = None
    user.totp_enabled = False
    db.commit()
    return {"ok": True}


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
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
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
def revoke_pat(pat_id: str, ctx: AuthContext = Depends(require_write),
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


@router.get("/api/users")
def list_users(ctx: AuthContext = Depends(require_admin), db: DbSession = Depends(get_db)):
    rows = db.query(User).order_by(User.created_at.asc()).all()
    return [_admin_list_json(u) for u in rows]


@router.post("/api/users")
def create_user(payload: dict, ctx: AuthContext = Depends(require_admin),
                db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
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
def patch_user(user_id: str, payload: dict, ctx: AuthContext = Depends(require_admin),
               db: DbSession = Depends(get_db)):
    if not isinstance(payload, dict):
        bad_request("请求体必须为 JSON 对象")
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
    db.commit()
    return _admin_list_json(user)
