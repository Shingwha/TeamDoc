"""数据模型与数据库连接(构建文档 §5)。

- SQLite(WAL 模式,busy_timeout=5000,foreign_keys=ON)
- **资源表主键:Integer 自增(AUTOINCREMENT,永不复用),起点 10000(五位数)**
  (users/pats/projects/project_members/docs/doc_versions/folders/files/login_events,
  外键同为 Integer)。
  删掉末尾的行也不会让新数据拿到已删 id —— 正文里指向已删资源的死链不会"复活";
  起点种子由 schema.seed_id_start 幂等写入。id 是纯标识、可公开显示,不承载秘密。
- **秘密与标识分离**:会话的键是随机 token(sessions.token,即 Cookie 值),
  PAT 的秘密是 token_hash —— 两者不随"标识数字化"变得可猜。
- 登录相关(pats/sessions/login_events/throttle_state):会话带来源与最近活跃(仅管理员
  可见);login_events 是登录审计(有保留期);throttle_state 是凭据尝试的节流计数。
  后两张标了 info.disposable —— **可丢弃状态**,备份/恢复不要求它们存在(见 backup.py)。
- 时间戳:UTC 无时区 naive
"""
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import Request
from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, Text, String,
    UniqueConstraint, create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

logger = logging.getLogger("teamdoc.db")

BASE_DIR = Path(__file__).resolve().parent
# 数据目录默认 server/data;支持环境变量覆盖(文档未定义,供测试/多实例隔离用)
DATA_DIR = Path(os.environ["TEAMDOC_DATA_DIR"]).resolve() if os.environ.get("TEAMDOC_DATA_DIR") \
    else BASE_DIR / "data"
FILES_DIR = DATA_DIR / "files"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "teamdoc.db"

# 正在上传的物理文件名(进程内登记)。上传是"先落盘、后提交 DB",这段时间它在库里还
# 没有记录;孤儿清理若只按 DB 比对,就会把在传文件当垃圾删掉 —— 上传随后提交成功,
# 记录指向已删文件,永久 404 且无任何提示。清理侧必须跳过这里登记的名字。
# 单 worker 部署下该集合才是精确的(见 HANDOFF §4.10)。
INFLIGHT_STORAGE: set[str] = set()

# "连接不参与长 I/O"不变量的可观测阈值:正常请求毫秒级,超过即记 WARNING
DB_WARN_SECONDS = 2.0

# 不用连接池:SQLite 是单文件、单写者,连接廉价,而队列池的"15 条上限 + 借不到等
# 30 秒"会把任何一条慢连接放大成全站排队 —— 历史上 WS 握手与传输端点都踩过。
# NullPool 下每条会话按需开、用完关,慢查询只影响它自己的那个请求。
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)


@event.listens_for(engine, "connect")
def _set_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    # busy_timeout 必须先设:它决定后面这些语句在库被别处写入时愿意等多久
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def get_db(request: Request):
    """请求级会话。**只有一两句查询的端点用它就对了**;凡是随后要搬运大量字节的
    端点(下载/上传/打包/备份),必须在此之前调用 release_db()。"""
    db = SessionLocal()
    started = time.monotonic()
    try:
        yield db
    finally:
        db.close()
        held = time.monotonic() - started
        if held > DB_WARN_SECONDS:
            # 把"连接不参与长 I/O"这条不变量变成可观测的:正常请求都是毫秒级
            logger.warning("数据库会话持有 %.1fs:%s %s", held,
                           request.method, request.url.path)


def release_db(db) -> None:
    """提前归还连接:事务的边界是"用库的那一段",不是"整个请求"。

    FastAPI 的 yield 依赖要等**响应体发完**才执行清理(fastapi/routing.py 的
    request_response),所以文件响应与流式响应会把连接占满全程 —— 几 GB 的下载、
    几小时的上传都算;客户端中途消失(睡眠/断网/暂停下载)时这条连接再也回不来。
    Session.close() 可重复调用,提前关了不影响依赖自己的 close()。
    """
    db.close()


def file_abspath(storage_path: str) -> Path:
    """把 files.storage_path(basename)解析成物理文件的绝对路径。

    只存 basename、不存机器相关的绝对路径 —— 否则把备份恢复到另一台机器、
    或改了 TEAMDOC_DATA_DIR,库里所有路径都会失效,表现为"文件全 404",
    而孤儿扫描还发现不了(它比的是 basename,磁盘上仍在)。
    历史绝对路径由 schema.normalize_storage_paths() 在启动时回填,
    这里统一按 basename 解析,不再兼容第二种口径。"""
    return FILES_DIR / Path(storage_path).name


def utcnow() -> datetime:
    return datetime.utcnow()


def unlink_quiet(path) -> bool:
    """删除物理文件,失败不抛错。返回是否真的删掉了。

    语义:DB 记录已删/已提交时,物理清理失败**不应**让整个请求失败 ——
    用户看到的是"删除成功",而残留文件由管理后台的孤儿清理兜底。
    反过来则不可以:先删文件再删记录,一旦提交失败就变成"文件没了但记录还在"。
    """
    if not path:
        return False
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def build_tree(rows, node_fn) -> list:
    """把(已排序的)model 行组装成嵌套树,返回根节点列表。

    行须有 id / parent_id(Doc 与 Folder 同构);node_fn(row) 产出的节点字典
    须含 "id" 并预置 "children": []。父不在集合内(父已删)的行视为根。
    文档树与文件夹树共用,勿再各写一份遍历。"""
    nodes = {r.id: node_fn(r) for r in rows}
    roots = []
    for r in rows:
        node = nodes[r.id]
        parent = nodes.get(r.parent_id) if r.parent_id else None
        (parent["children"] if parent else roots).append(node)
    return roots


def collect_subtree(db, model, root) -> list:
    """root 自身 + 全部后代 id(不看删除状态 —— 删除/恢复/彻底删除/防环移动统一使用)。
    Doc 与 Folder 同构(id/parent_id/project_id),一份实现两处用。"""
    rows = db.query(model.id, model.parent_id).filter_by(project_id=root.project_id).all()
    children: dict = {}
    for nid, pid in rows:
        children.setdefault(pid, []).append(nid)
    result, stack = [], [root.id]
    while stack:
        cur = stack.pop()
        result.append(cur)
        stack.extend(children.get(cur, []))
    return result


def ancestor_names(db, model, start_id, attr: str) -> list[str]:
    """祖先链名称列表(根在前),用于位置上下文(location.path)。

    文档的标题链与文件的文件夹链共用;深度上限 64 防数据异常成环,
    父不存在则止步(该层之后的路径已不可信)。"""
    names: list[str] = []
    cur_id, depth = start_id, 0
    while cur_id and depth < 64:
        node = db.get(model, cur_id)
        if not node:
            break
        names.insert(0, getattr(node, attr))
        cur_id = node.parent_id
        depth += 1
    return names


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(50))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuthSession(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)  # 随机秘密,即 Cookie 值
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    # 来源信息:管理后台"登录状态 / 活跃会话"用,**只在管理员接口暴露**(同事目录
    # 与项目成员列表都没有它)。旧库手工 ALTER 补列后为 NULL,读取端一律容忍。
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 最近活跃:session_context 以 >60 秒的频率刷新(与 pat.last_used_at 同一惯例,
    # 不按请求写)。"在线" = 未过期且最近活跃在 ONLINE_WINDOW_SECONDS 内(见 auth.py)。
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Pat(Base):
    __tablename__ = "pats"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(50))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # 秘密只在 hash 里
    scopes: Mapped[str] = mapped_column(String(20), default="read")  # "read" 或 "read,write"
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LoginEvent(Base):
    """登录审计:一次**真正跑过口令校验**的尝试落一行(冷却期内的 429 不落 —— 那可以被
    无限刷,而当前锁状态由 throttle_state 承载)。

    - email 存提交原文的规范化值:账号不存在时也要有,否则"有人在拿不存在的邮箱撞库"
      这件事就看不见了;user_id 仅在账号存在时填。
    - 保留期由 auth.LOGIN_EVENT_KEEP_DAYS 控制,超期分批删除。
    """
    __tablename__ = "login_events"
    __table_args__ = (Index("ix_login_events_user_created", "user_id", "created_at"),
                      {"sqlite_autoincrement": True, "info": {"disposable": True}})
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    email: Mapped[str] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    result: Mapped[str] = mapped_column(String(20))  # ok / bad_password / disabled / locked
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class ThrottleState(Base):
    """凭据尝试的节流状态(throttle.py 的机制表):一行一个键,窗口内计数 + 冷却。

    key = "<策略名>:<主体>"(如 account:someone@x.com / source_ip:192.168.1.9)。
    纯派生状态(info.disposable):删掉只会让所有人重新获得尝试机会,故备份校验不要求它。
    """
    __tablename__ = "throttle_state"
    __table_args__ = {"info": {"disposable": True}}
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(default=utcnow)  # 本轮计数的起点
    strikes: Mapped[int] = mapped_column(Integer, default=0)  # 触发冷却的次数(退避倍增用)
    locked_until: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)  # 个人空间(每用户一个,不可删/不可管成员)
    # 可见性:"private"(默认,仅成员可见)/ "public"。
    # public = 本实例所有登录用户**可发现 + 可自助加入**;加入前**完全不可读**
    # (非成员在 auth.project_role 里拿不到任何角色 → 全站 403)。
    # 个人空间永远保持 private,服务端硬拒改动(个人空间是私有草稿区,
    # 一旦可公开用户就不敢往里放东西,而那正是它的价值)。
    visibility: Mapped[str] = mapped_column(String(10), default="private")
    # 自助加入后拿到的角色,仅 public 时有意义。只有 VIEWER / EDITOR 两档:
    # 自助加入是一条**任何人**都能走的路径,绝不能拿到管理权(ADMIN/OWNER 只能由
    # 成员管理页授予)。改这一档需要项目 ADMIN(见 projects.patch_project)。
    join_role: Mapped[str] = mapped_column(String(10), default="VIEWER")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"), {"sqlite_autoincrement": True})
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(10), default="VIEWER")  # OWNER/ADMIN/EDITOR/VIEWER
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Doc(Base):
    __tablename__ = "docs"
    __table_args__ = (Index("ix_docs_project_deleted", "project_id", "deleted_at"),
                      {"sqlite_autoincrement": True})
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="无标题文档")
    content: Mapped[str] = mapped_column(Text, default="")
    sort: Mapped[float] = mapped_column(Float, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class DocVersion(Base):
    __tablename__ = "doc_versions"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("docs.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)  # 一切归属项目
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class File(Base):
    __tablename__ = "files"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)  # 一切归属项目
    folder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(300), unique=True)
    # 单文件公开:让不在本项目里的登录用户也能下载。
    # 存在的意义是补"只想共享一个文件,又不想把整个项目公开"这个缺口 ——
    # 否则唯一的办法是把文件挪进一个公开项目,代价是暴露整个项目。
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
