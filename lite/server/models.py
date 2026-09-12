"""数据模型与数据库连接(构建文档 §5)。

- SQLite(WAL 模式,busy_timeout=5000,foreign_keys=ON)
- **资源表主键:Integer 自增(AUTOINCREMENT,永不复用),起点 10000(五位数)**
  (users/pats/projects/project_members/docs/doc_versions/folders/files,外键同为 Integer)。
  删掉末尾的行也不会让新数据拿到已删 id —— 正文里指向已删资源的死链不会"复活";
  起点种子由 schema.seed_id_start 幂等写入。id 是纯标识、可公开显示,不承载秘密。
- **秘密与标识分离**:会话的键是随机 token(sessions.token,即 Cookie 值),
  PAT 的秘密是 token_hash —— 两者不随"标识数字化"变得可猜。
- 时间戳:UTC 无时区 naive
"""
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, Text, String,
    UniqueConstraint, create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

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

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def file_abspath(storage_path: str) -> Path:
    """把 files.storage_path 解析成物理文件的绝对路径。

    **新数据只存 basename**(如 `a1b2c3...`),不存机器相关的绝对路径 ——
    否则把备份恢复到另一台机器、或改了 TEAMDOC_DATA_DIR,库里所有路径都会失效,
    表现为"文件全 404",而孤儿扫描还发现不了(它比的是 basename,磁盘上仍在)。

    历史数据可能是绝对路径:按原样使用,保证升级后立即可用;
    启动时会由 schema.normalize_storage_paths() 回填为 basename。
    """
    if not storage_path:
        return FILES_DIR / ""
    p = Path(storage_path)
    if p.is_absolute():
        return p
    return FILES_DIR / p.name


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


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)  # 个人空间(每用户一个,不可删/不可管成员)
    # 可见性:"private"(默认,仅成员可见)/ "public"(本实例所有登录用户可**只读**访问)。
    # 公开不产生成员关系 —— 鉴权上表现为"非成员拿到 VIEWER"(见 auth.project_role)。
    # 个人空间永远保持 private,服务端硬拒改动(个人空间是私有草稿区,
    # 一旦可公开用户就不敢往里放东西,而那正是它的价值)。
    visibility: Mapped[str] = mapped_column(String(10), default="private")
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
