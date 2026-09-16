"""认证与权限(构建文档 §6、§7.1、§7.2)。

- 密码:hashlib.scrypt(纯标准库)。**校验只有一条路:`verify_credentials()`** ——
  它一处做完"冷却检查 + 恒定耗时校验 + 登录审计 + 成功清零",新增凭据端点只要走它
  就不可能漏掉节流与反枚举(`hash_password` 仍对外:离线重置密码脚本要用,见 DEPLOY §5)。
- 会话:Cookie `td_sid`(HttpOnly + SameSite=Lax),带来源 IP / UA / 最近活跃(仅管理员可见)
- 登录审计:login_events 表(成功/密码错/已禁用/触发锁定),有保留期
- PAT:`tdp_` 前缀,库存 sha256
- 权限依赖:current_user / require_admin / require_project_role / require_doc_role /
  require_file_role / require_folder_role;PAT write scope 由挂在每个 router 上的
  pat_write_guard 统一校验

不做两步验证(TOTP):内网自部署 + 30 人规模下,它防的"密码泄露后的二次验证"
价值低(内部人有自己的账号),而账号管控靠"禁用用户"与可吊销的 PAT 覆盖。
见 HANDOFF §4.16。
"""
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

import throttle
from models import (AuthSession, Doc, DocVersion, File, Folder, LoginEvent, Pat,
                    Project, ProjectMember, User, get_db, utcnow)

logger = logging.getLogger("teamdoc.security")

SESSION_COOKIE = "td_sid"
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))
SESSION_TTL = SESSION_TTL_DAYS * 86400
# 勾选"记住我"时的会话有效期。**注意:不勾选仍是上面这档** ——
# 默认值必须与旧行为一致,否则已习惯"关浏览器不用重登"的人会被莫名登出。
REMEMBER_TTL_DAYS = int(os.environ.get("REMEMBER_TTL_DAYS", "30"))
REMEMBER_TTL = REMEMBER_TTL_DAYS * 86400

# ---------- 登录节流策略(§4.9;机制在 throttle.py) ----------
# 两个维度各管一类攻击:账号维度挡"盯着一个人猛试",来源维度挡"一个来源轮着试很多账号"
# (密码喷洒/撞库)。阈值都可用环境变量调,详见 DEPLOY §1.3。
LOGIN_ACCOUNT_POLICY = throttle.Policy(
    name="account",
    max_fails=int(os.environ.get("LOGIN_MAX_FAILS", "5")),
    window=int(os.environ.get("LOGIN_FAIL_WINDOW", "900")),
    lockout=int(os.environ.get("LOGIN_LOCKOUT", "60")),
    max_lockout=int(os.environ.get("LOGIN_LOCKOUT_MAX", "3600")),
    clear_on_success=True,
    # 必须大于 max_lockout:否则连续攻击每过一个冷却周期就被判成"静默",永远回到最短冷却
    idle_reset=7200,
)
LOGIN_SOURCE_IP_POLICY = throttle.Policy(
    name="source_ip",
    subject="client_ip",
    max_fails=int(os.environ.get("LOGIN_IP_MAX_FAILS", "20")),
    window=int(os.environ.get("LOGIN_FAIL_WINDOW", "900")),
    lockout=int(os.environ.get("LOGIN_IP_LOCKOUT", "300")),
)
LOGIN_POLICIES = (LOGIN_ACCOUNT_POLICY, LOGIN_SOURCE_IP_POLICY)
LOGIN_EVENT_KEEP_DAYS = int(os.environ.get("LOGIN_EVENT_KEEP_DAYS", "30"))
# "在线" = 存在未过期会话且最近活跃在这个窗口内(auth_me / 任何 REST 请求 / WS 消息都刷新)
ONLINE_WINDOW_SECONDS = 300


def is_online(seen_at, created_at, now=None) -> bool:
    """"在线"判定唯一口径:最近活跃(无活跃记录则回退创建时间)在窗口内。
    用户列表(_login_status)与登录详情(_session_json)共用此函数 ——
    窗口口径升级(如改时长、改"活跃"定义)只动这里,两处不可能再漂移。"""
    return (seen_at or created_at) >= (now or utcnow()) - timedelta(seconds=ONLINE_WINDOW_SECONDS)


def user_map(db: DbSession, ids) -> dict[int, User]:
    """id → User 批量解析(自动滤空、空集短路)。文件上传者/版本作者/登录事件等
    "列表里顺带解析人"的场景共用,替代各处复制的 in_(ids) 查询 + 空集判断样板。"""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u for u in db.query(User).filter(User.id.in_(ids)).all()}


def user_names(db: DbSession, ids) -> dict[int, str]:
    """id → 姓名的批量解析;查不到的人自然缺席,调用方用 in/.get 兜底。"""
    return {uid: u.name for uid, u in user_map(db, ids).items()}
# 最近活跃的落库频率:与 pat.last_used_at 同一惯例(>60s 才写一次),不给每个请求加一次写
SEEN_WRITE_INTERVAL_SECONDS = 60
_EVENT_PRUNE_INTERVAL_SECONDS = 3600

ROLE_RANK = {"OWNER": 3, "ADMIN": 2, "EDITOR": 1, "VIEWER": 0}

# 头像颜色:文档 §5 未建 avatar_color 字段,但 §7.1 登录响应要求 avatarColor;
# 按最简实现:由 user id 哈希在固定调色板中取色(确定性,不落库)
_PALETTE = ["#3370ff", "#7c3aed", "#db2777", "#ea580c", "#16a34a", "#0891b2", "#ca8a04", "#1f6feb"]


# ---------- 错误与校验工具 ----------

def err(status: int, code: str, message: str, *, headers: dict | None = None,
        extra: dict | None = None):
    """抛一个契约错误:{code,message};extra 用于 409 之类需要附带现场数据的场景
    (目前只有文档内容冲突:客户端要拿服务端当前正文做差异对比,不能只给一句错误文案)。"""
    detail = {"code": code, "message": message}
    if extra:
        detail.update(extra)
    raise HTTPException(status_code=status, detail=detail, headers=headers)


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


def bool_field(payload: dict, key: str, default: bool = False) -> bool:
    """严格布尔字段:仅接受 JSON true/false,其余真值("false"/1)一律按 default。

    真值强转是隐性坑:客户端把 "false" 字符串发上来,bool() 会判成 True,
    表现成"勾了个没勾的框"。所有布尔语义的 payload 字段都应走这里。"""
    v = payload.get(key)
    return v if isinstance(v, bool) else default


def check_password_strength(password: str):
    if len(password) < 8:
        bad_request("密码长度至少 8 位")


def check_email(email: str):
    if not email or "@" not in email or len(email) > 255:
        bad_request("邮箱格式不正确")


# ---------- 登录来源(IP / UA) ----------

def client_ip(request: Request) -> str:
    """来源地址,用于"来源 IP"节流维度与登录审计。

    uvicorn 的 ProxyHeadersMiddleware 默认开启,且默认只信任 127.0.0.1 发来的
    X-Forwarded-For —— 同机 Nginx 反代下这里拿到的已经是真实客户端地址。
    **反代在另一台机器上**时必须给 uvicorn 传 `--proxy-headers --forwarded-allow-ips=<反代IP>`,
    否则所有请求都来自反代地址,来源维度会退化成"全站共用一个桶"(那时请把
    LOGIN_IP_MAX_FAILS 设为 0 关掉该维度,见 DEPLOY §1.5)。
    """
    return request.client.host if request.client and request.client.host else ""


def client_ua(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:300]


_BROWSER_TOKENS = (("Edg", "Edge"), ("OPR", "Opera"), ("Firefox", "Firefox"),
                   ("Chrome", "Chrome"), ("Safari", "Safari"))
_OS_TOKENS = (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"),
              ("iPad", "iPad"), ("Mac OS X", "macOS"), ("Linux", "Linux"))
_MOBILE_TOKENS = ("Android", "iPhone", "iPad", "Mobile")


def describe_ua(ua: str) -> tuple[str, str]:
    """把 UA 归纳为 (可读标签, 设备档位 desktop|mobile)。认不出回落"未知设备"。

    刻意只做粗归纳(要在内网离线环境零依赖运行),且**只在服务端做一次**:
    UA 解析放前端就变成"同一份知识两处实现"(与 mime 判定同一条原则)。原始 UA
    一并返回给前端做 tooltip,排查时以它为准。
    """
    if not ua:
        return "未知设备", "desktop"
    browser = next((label for tok, label in _BROWSER_TOKENS if tok in ua), "")
    os_name = next((label for tok, label in _OS_TOKENS if tok in ua), "")
    label = " · ".join(p for p in (browser, os_name) if p) or "未知设备"
    kind = "mobile" if any(tok in ua for tok in _MOBILE_TOKENS) else "desktop"
    return label, kind


# ---------- 密码(§6.1) ----------

def hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"{salt.hex()}${dk.hex()}"


def _verify_password(plain: str, stored: str) -> bool:
    """口令散列比对(**模块内部**)。外部一律走 verify_credentials():那里才有节流与审计。"""
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk.hex(), digest_hex)


@cache
def _dummy_hash() -> str:
    """给"账号不存在"路径用的固定散列(进程内算一次)。

    没有它时,scrypt 只在邮箱存在时才跑 —— 响应耗时成了"该邮箱是否注册过"的信号:
    撞库前先按耗时筛出有效账号,防御方最贵的一步反而被省掉了。用固定散列把这条路径
    补齐,让两种情况的耗时与结果都无从区分(枚举账号再无捷径)。
    """
    return hash_password(secrets.token_hex(16))


# ---------- 凭据校验唯一入口(节流 + 审计) ----------

def record_login_event(db: DbSession, *, user: User | None, email: str, ip: str,
                       ua: str, result: str) -> None:
    """落一行登录审计。result: ok / bad_password / disabled / locked。**绝不记录口令**。

    冷却期内被拦下的请求不落行:它们没有跑过校验,又可以被无限刷 —— 落库只会让审计表
    变成攻击者的写入放大器(当前锁状态由 throttle_state 承载,管理端直接展示)。
    """
    db.add(LoginEvent(user_id=user.id if user else None, email=email, ip=ip or None,
                      user_agent=ua or None, result=result))
    _prune_login_events(db)
    db.commit()


_event_pruned_at = 0.0


def _prune_login_events(db: DbSession, batch: int = 500) -> int:
    """按保留期分批清理审计(与 docs._prune_versions 同一做法:一次只删一批,
    避免一个大事务把 SQLite 写锁攥住)。带进程内节流阀 —— 只在下一次记录时才会再扫。"""
    global _event_pruned_at
    if LOGIN_EVENT_KEEP_DAYS <= 0 or time.monotonic() - _event_pruned_at < _EVENT_PRUNE_INTERVAL_SECONDS:
        return 0
    _event_pruned_at = time.monotonic()
    cutoff = utcnow() - timedelta(days=LOGIN_EVENT_KEEP_DAYS)
    ids = [i for (i,) in db.query(LoginEvent.id).filter(LoginEvent.created_at < cutoff).limit(batch)]
    if not ids:
        return 0
    n = db.query(LoginEvent).filter(LoginEvent.id.in_(ids)).delete(synchronize_session=False)
    return int(n)


def _throttle_subject(policy: throttle.Policy, ident: str, ip: str) -> str:
    return ip if policy.subject == "client_ip" else ident


def verify_credentials(request: Request, db: DbSession, *, ident: str, password: str,
                       user: User | None,
                       policies: tuple[throttle.Policy, ...] = LOGIN_POLICIES) -> bool:
    """账号口令校验的唯一入口(登录 / 自助改密共用)。返回是否通过;命中节流抛 429。

    顺序即语义:
    1. **冷却检查**(账号 + 来源):命中即 429 + Retry-After,且不跑 scrypt、不累加计数 ——
       "锁定期内正确密码也进不来"与"一个人连点不会把来源桶刷爆"都由这一处保证
    2. 校验:用户不存在也走一次 scrypt(_dummy_hash),消除"账号是否存在"的时序信号
    3. 失败:两个维度各记一次;本次触发冷却 → 429,否则返回 False 由调用方按自己的
       契约回 401/403
    4. 通过:清除声明了 clear_on_success 的维度(账号);来源维度**不清**(否则攻击者
       只要有一个有效账号就能不断重置来源桶继续喷洒)

    节流与审计都在这里,调用方只负责"凭据不符"的文案。
    """
    ip = client_ip(request)
    ua = client_ua(request)
    for p in policies:
        lock = throttle.locked(db, p, _throttle_subject(p, ident, ip))
        if lock:
            logger.warning("登录节流拦截:ident=%s ip=%s 策略=%s 剩余=%ds",
                           ident, ip or "-", p.name, lock.retry_after)
            _too_many(lock.retry_after)

    ok = _verify_password(password, user.password_hash if user is not None else _dummy_hash())
    if not ok:
        locked = None
        for p in policies:
            lk = throttle.count_failure(db, p, _throttle_subject(p, ident, ip))
            if lk and (locked is None or lk.retry_after > locked.retry_after):
                locked = lk
        record_login_event(db, user=user, email=ident, ip=ip, ua=ua,
                           result="locked" if locked else "bad_password")
        logger.warning("登录失败:ident=%s ip=%s 触发冷却=%s", ident, ip or "-",
                       f"{locked.retry_after}s/{locked.strikes}次" if locked else "否")
        throttle.sweep(db, max(p.window for p in policies))
        if locked:
            _too_many(locked.retry_after)
        return False

    for p in policies:
        if p.clear_on_success:
            throttle.clear(db, p, _throttle_subject(p, ident, ip))
    # 口令正确但账号被禁用:结果记为 disabled —— "拿着正确密码却进不来"正是管理员
    # 需要看见的信号(离职后仍有人尝试登录)。
    record_login_event(db, user=user, email=ident, ip=ip, ua=ua,
                       result="disabled" if user.is_disabled else "ok")
    return True


def _too_many(retry_after: int):
    err(429, "TOO_MANY_ATTEMPTS", f"登录失败次数过多,请 {retry_after} 秒后重试",
        headers={"Retry-After": str(retry_after)})


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


def authenticate(request: Request, db: DbSession = Depends(get_db)) -> AuthContext | None:
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
    return session_context(db, request.cookies.get(SESSION_COOKIE))


def session_context(db: DbSession, token: str | None) -> AuthContext | None:
    """Cookie 会话判定(含滑动续期与"最近活跃"刷新);无效返回 None。

    HTTP 的 authenticate 与 WS 的逐条权限复核共用同一套
    "token 存在 + 未过期 + 用户未禁用" 规则 —— 改会话策略只改这里,
    不会出现"REST 改了、长连接还是旧规则"的漂移。
    """
    if not token:
        return None
    sess = db.get(AuthSession, token)
    if not sess or sess.expires_at <= utcnow():
        return None
    user = db.get(User, sess.user_id)
    if not user or user.is_disabled:
        return None
    now = utcnow()
    dirty = False
    # 最近活跃(管理后台"在线/活跃会话"的判据):>60 秒才写一次,与 pat.last_used_at 同一
    # 惯例 —— 每个请求都写会让每次读页面都变成一次数据库写入。
    if (now - (sess.last_seen_at or sess.created_at)).total_seconds() > SEEN_WRITE_INTERVAL_SECONDS:
        sess.last_seen_at = now
        dirty = True
    # 会话自身的生命周期(记住我 30 天 / 普通 7 天)。用 expires_at - created_at 推导,
    # 而不是读全局 SESSION_TTL:后者会把长会话续短,且无需为此新增数据库列。
    lifetime = (sess.expires_at - sess.created_at).total_seconds()
    # 剩余寿命 < 1/2 时滑动续期(§6.2)。与最近活跃合并为一次提交
    if (sess.expires_at - now).total_seconds() < lifetime / 2:
        sess.expires_at = now + timedelta(seconds=lifetime)
        dirty = True
    if dirty:
        db.commit()
    return AuthContext(user=user, via="web", scopes=["read", "write"],
                       session_token=token, session_lifetime=lifetime)


def current_user(request: Request, ctx: AuthContext | None = Depends(authenticate)) -> AuthContext:
    if ctx is None:
        err(401, "UNAUTHORIZED", "未登录")
    request.state.auth = ctx
    return ctx


def pat_write_guard(request: Request, ctx: AuthContext | None = Depends(authenticate)) -> None:
    """PAT write scope 全站守卫:非 GET 且凭据是只读 PAT → 403。

    挂在每个 APIRouter 上统一生效(WS 除外 —— 它只认 Web 会话,PAT 连不上)。
    历史教训:此前靠每个写端点手工叠一层 scope 校验,漏一个就是一个提权洞
    (管理端写接口曾整体漏掉,只读令牌可用它建出新的管理员账号);收敛到
    router 级守卫后,新增写端点不可能再漏挂。未认证请求在此放行,
    由各端点的 current_user 负责 401。
    """
    if request.method != "GET" and ctx is not None and ctx.via == "pat" \
            and "write" not in ctx.scopes:
        err(403, "FORBIDDEN", "令牌缺少 write 权限")


def require_admin(ctx: AuthContext = Depends(current_user)) -> AuthContext:
    if not ctx.user.is_admin:
        err(403, "FORBIDDEN", "需要管理员权限")
    return ctx


router = APIRouter(dependencies=[Depends(pat_write_guard)])


def project_role(db: DbSession, project_id: int, user: User) -> str | None:
    """成员角色优先;否则 is_admin → ADMIN;否则 None(§6.3)

    **这是全站权限的咽喉**:文档树/文档读写/云空间/搜索/WS 都经这里判定。
    非成员一律无角色 —— 公开项目也不例外,公开只意味着"可发现 + 可自助加入"
    (见 projects.join_project),**不产生任何读权限**,所以这里没有 public 分支。
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
    return None


def is_project_member(db: DbSession, project_id: int, user: User) -> bool:
    """真实成员关系(不含"全局管理员":管理员对任何非个人项目都是 ADMIN,但不是成员)。

    区分"我加入了这个项目"与"我是管理员所以能进":前端据此渲染「退出项目」
    这类只对真成员成立的入口(管理员在项目里没有成员身份可退)。
    """
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


def has_role(role: str | None, required: str) -> bool:
    """角色比较唯一入口:role 达到 required 级别(含)与否。

    ensure_project_role 与 WS 的逐条权限复核共用,避免比较逻辑散落多处。"""
    return role is not None and ROLE_RANK.get(role, -1) >= ROLE_RANK[required]


def ensure_project_role(db: DbSession, ctx: AuthContext, project_id: int,
                        required: str) -> str | None:
    """按项目角色鉴权:不足则 403,返回实际角色(供需要区分的调用方使用)。

    拒绝时分两种文案:公开项目回 JOIN_REQUIRED —— 它是**唯一能用一句话说清出路**
    的拒绝(去发现页点加入即可),同事把项目/文档链接发过来时不会撞上一句
    "需要 VIEWER 及以上权限"而无从下手。其余项目维持通用文案(不泄露存在性以外的信息)。
    """
    role = project_role(db, project_id, ctx.user)
    if not has_role(role, required):
        p = db.get(Project, project_id)
        if p is not None and p.visibility == "public":
            err(403, "JOIN_REQUIRED", "本项目需先加入才能查看")
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


def require_doc_role(required: str, *, for_trash: bool = False):
    """按文档路径参数鉴权,返回 (ctx, doc)。顺序约定同 require_file_role。"""
    def dep(doc_id: int, ctx: AuthContext = Depends(current_user),
            db: DbSession = Depends(get_db)) -> tuple:
        doc = db.get(Doc, doc_id)
        if not doc:
            err(404, "NOT_FOUND", "文档不存在")
        ensure_project_role(db, ctx, doc.project_id, required)
        if doc.deleted_at is not None and not for_trash:
            err(404, "NOT_FOUND", "文档不存在")
        return ctx, doc
    return dep


def require_file_role(required: str, *, for_trash: bool = False):
    """按文件路径参数鉴权,返回 (ctx, file)。

    判定顺序:不存在 → 404,权限 → 403,状态 → 404(授权先于状态,
    非成员无法凭差异探测他人资源)。for_trash=True 供回收站端点(恢复/彻底删除):
    "已删"是前置条件而非异常,由端点自行回 409。
    """
    def dep(file_id: int, ctx: AuthContext = Depends(current_user),
            db: DbSession = Depends(get_db)) -> tuple:
        f = db.get(File, file_id)
        if not f:
            err(404, "NOT_FOUND", "文件不存在")
        ensure_project_role(db, ctx, f.project_id, required)
        if f.deleted_at is not None and not for_trash:
            err(404, "NOT_FOUND", "文件不存在")
        return ctx, f
    return dep


def require_folder_role(required: str, *, for_trash: bool = False):
    """按文件夹路径参数鉴权,返回 (ctx, folder)。顺序约定同 require_file_role。"""
    def dep(folder_id: int, ctx: AuthContext = Depends(current_user),
            db: DbSession = Depends(get_db)) -> tuple:
        folder = db.get(Folder, folder_id)
        if not folder:
            err(404, "NOT_FOUND", "文件夹不存在")
        ensure_project_role(db, ctx, folder.project_id, required)
        if folder.deleted_at is not None and not for_trash:
            err(404, "NOT_FOUND", "文件夹不存在")
        return ctx, folder
    return dep


# ---------- 会话辅助 ----------

def _create_session(db: DbSession, user_id: int, ttl: int = SESSION_TTL, *,
                    ip: str = "", ua: str = "") -> str:
    """建一条会话。ip/ua 只进管理端展示,不进任何面向普通用户的接口。"""
    token = secrets.token_hex(32)
    now = utcnow()
    # created_at 显式给值:默认值在 flush 时才求值,会比上面的 now 晚几毫秒 ——
    # 于是"最近活跃"看起来早于"登录时间"。同一时刻写入,读起来才自洽。
    db.add(AuthSession(token=token, user_id=user_id, created_at=now,
                       expires_at=now + timedelta(seconds=ttl),
                       ip=ip or None, user_agent=ua or None, last_seen_at=now))
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


def _validated_user_fields(payload: dict) -> tuple[str, str, str]:
    """建号字段校验的共用序列(初始化向导与管理员建号同一规则):
    邮箱规范化+校验 → 姓名 → 口令类型+强度。邮箱查重由调用方决定时机与文案
    (初始化时库必空,管理员建号撞重要给 409)。"""
    email = str_field(payload, "email", 255, required=True).lower()
    check_email(email)
    name = str_field(payload, "name", 50, required=True)
    password = payload.get("password")
    if not isinstance(password, str):
        bad_request("password 必须为字符串")
    check_password_strength(password)
    return email, name, password


def _create_user_with_personal_space(db: DbSession, email: str, name: str,
                                     password: str, is_admin: bool) -> User:
    """建用户 + 同事务建个人空间(§7.1)——建号落库的唯一路径,bootstrap 与
    管理员建号共用;会话签发、登录审计等差异由调用方在提交后各自追加。"""
    user = User(email=email, name=name, password_hash=hash_password(password),
                is_admin=is_admin)
    db.add(user)
    db.flush()
    create_personal_project(db, user)  # 同一事务创建个人空间项目
    db.commit()
    return user


@router.post("/api/auth/bootstrap")
def bootstrap(payload: dict, request: Request, response: Response,
              db: DbSession = Depends(get_db)):
    if db.query(User).count() > 0:
        err(403, "FORBIDDEN", "系统已初始化")
    email, name, password = _validated_user_fields(payload)
    user = _create_user_with_personal_space(db, email, name, password, is_admin=True)
    token = _create_session(db, user.id, ip=client_ip(request), ua=client_ua(request))
    _set_session_cookie(response, token)
    # 首个管理员诞生也算一次登录:初始化是安全上最该留痕的动作之一
    # (DEPLOY §1.2 的"初始化窗口"讲的就是它)
    record_login_event(db, user=user, email=email, ip=client_ip(request),
                       ua=client_ua(request), result="ok")
    return {"user": user_json(user)}


def _mismatch_message(db: DbSession, email: str) -> str:
    """凭据不符的文案。仅剩 1~2 次机会时给出提示 —— 用户不知道"再错就要被锁"只会
    以为系统坏了;而计数与账号是否存在无关(不存在的邮箱同样计),不构成枚举信号。"""
    msg = "邮箱或密码错误"
    left = throttle.remaining(db, LOGIN_ACCOUNT_POLICY, email)
    if left is not None and 0 < left <= 2:
        msg += f"(还可尝试 {left} 次)"
    return msg


@router.post("/api/auth/login")
def login(payload: dict, request: Request, response: Response,
          db: DbSession = Depends(get_db)):
    email = str_field(payload, "email", 255, required=True).lower()
    password = payload.get("password")
    if not isinstance(password, str):
        bad_request("password 不能为空")
    user = db.query(User).filter_by(email=email).first()
    # 节流 + 恒定耗时校验 + 审计都在这里(命中冷却直接 429)
    if not verify_credentials(request, db, ident=email, password=password, user=user):
        err(401, "UNAUTHORIZED", _mismatch_message(db, email))
    if user.is_disabled:
        err(401, "UNAUTHORIZED", "账号已被禁用")
    # 记住我 → 更长会话;严格布尔判定,避免客户端传 "false"/1 这类真值被误判
    ttl = REMEMBER_TTL if bool_field(payload, "remember") else SESSION_TTL
    token = _create_session(db, user.id, ttl, ip=client_ip(request), ua=client_ua(request))
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
def change_my_password(payload: dict, request: Request, ctx: AuthContext = Depends(current_user),
                       db: DbSession = Depends(get_db)):
    old = payload.get("oldPassword")
    new = payload.get("newPassword")
    if not isinstance(old, str) or not isinstance(new, str):
        bad_request("oldPassword/newPassword 必须为字符串")
    # 走同一咽喉(会话被盗后拿旧密码反复试也被计数与冷却挡住)。
    # 这里**只挂账号维度**:调用方已经是登录态,来源 IP 不是这条路径的威胁轴,
    # 而共享出口 IP 的办公室里一个人打错几次不该把整个来源桶算进去。
    if not verify_credentials(request, db, ident=ctx.user.email, password=old,
                             user=ctx.user, policies=(LOGIN_ACCOUNT_POLICY,)):
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
def create_pat(payload: dict, ctx: AuthContext = Depends(current_user),
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
def revoke_pat(pat_id: int, ctx: AuthContext = Depends(current_user),
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


_EMPTY_LOGIN_STATUS = {"lastLoginAt": None, "lastLoginIp": None, "online": False,
                       "sessionCount": 0, "lockedUntil": None}


def _login_status(db: DbSession, users: list[User]) -> dict[int, dict]:
    """批量算"登录状态":最近一次成功登录(时间/IP)、活跃会话数、是否在线、是否被锁。

    为什么从 login_events 聚合而不在 users 上加列:最后登录时间是**审计表的事实**,
    在 users 上再存一份就是同一事实两份副本(改邮箱、手工改库、恢复旧备份都会让它们漂移)。
    批量取(窗口函数 + 一次会话扫描)而不是每用户三次查询,免得随规模线性放大。
    """
    now = utcnow()
    ids = [u.id for u in users]
    out = {uid: dict(_EMPTY_LOGIN_STATUS) for uid in ids}
    if not ids:
        return out
    # 每个用户最近一次成功登录:窗口函数取每组第一行(SQLite 3.25+,本项目实测 3.47)
    rn = func.row_number().over(partition_by=LoginEvent.user_id,
                                order_by=LoginEvent.created_at.desc()).label("rn")
    ranked = (select(LoginEvent.user_id.label("uid"), LoginEvent.created_at,
                     LoginEvent.ip, rn)
              .where(LoginEvent.result == "ok", LoginEvent.user_id.isnot(None),
                     LoginEvent.user_id.in_(ids)).subquery())
    for uid, created, ip in db.execute(select(ranked.c.uid, ranked.c.created_at, ranked.c.ip)
                                       .where(ranked.c.rn == 1)):
        st = out.get(uid)
        if st:
            st["lastLoginAt"] = created.isoformat()
            st["lastLoginIp"] = ip
    # 活跃会话:未过期即计入;"在线"再看最近活跃窗口(is_online 唯一口径)
    for uid, seen, created in (db.query(AuthSession.user_id, AuthSession.last_seen_at,
                                        AuthSession.created_at)
                               .filter(AuthSession.expires_at > now,
                                       AuthSession.user_id.in_(ids))):
        st = out.get(uid)
        if not st:
            continue
        st["sessionCount"] += 1
        if is_online(seen, created, now):
            st["online"] = True
    locks = throttle.locks_for(db, LOGIN_ACCOUNT_POLICY, [u.email for u in users])
    for u in users:
        if u.email in locks:
            out[u.id]["lockedUntil"] = locks[u.email]
    return out


def _admin_user_json(db: DbSession, u: User) -> dict:
    """单个用户的完整管理视图(基础字段 + 登录状态)。"""
    d = _admin_list_json(u)
    d.update(_login_status(db, [u]).get(u.id, dict(_EMPTY_LOGIN_STATUS)))
    return d


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
def delete_user(user_id: int, ctx: AuthContext = Depends(require_admin),
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
    status = _login_status(db, rows)  # 登录状态(最近登录/IP、在线、会话数、锁定)同样批量算
    out = []
    for u in rows:
        d = _admin_list_json(u)
        d.update(status.get(u.id, dict(_EMPTY_LOGIN_STATUS)))
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
def create_user(payload: dict, ctx: AuthContext = Depends(require_admin),
                db: DbSession = Depends(get_db)):
    email, name, password = _validated_user_fields(payload)
    is_admin = bool_field(payload, "isAdmin")
    if db.query(User).filter_by(email=email).first():
        err(409, "CONFLICT", "该邮箱已被注册")
    user = _create_user_with_personal_space(db, email, name, password, is_admin=is_admin)
    return _admin_user_json(db, user)


@router.patch("/api/users/{user_id}")
def patch_user(user_id: int, payload: dict, ctx: AuthContext = Depends(require_admin),
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
        user.is_admin = bool_field(payload, "isAdmin")
    if "isDisabled" in payload:
        user.is_disabled = bool_field(payload, "isDisabled")
    if "unlock" in payload and bool_field(payload, "unlock"):
        # 解锁:冷却本来会自己过期,但被锁在门外的人需要立刻放行,否则运维的唯一手段
        # 就成了改数据库(DEPLOY §5)。只清账号维度 —— 来源 IP 的桶不属于某个账号,
        # 且它短(默认 5 分钟),误伤面小。
        throttle.clear(db, LOGIN_ACCOUNT_POLICY, user.email)
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
    return _admin_user_json(db, user)
