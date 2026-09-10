"""数据模型与数据库连接(构建文档 §5)。

- SQLite(WAL 模式,busy_timeout=5000,foreign_keys=ON)
- 所有主键:secrets.token_hex(8)(16 字符十六进制)
- 时间戳:UTC 无时区 naive
"""
import os
import secrets
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


def new_id() -> str:
    return secrets.token_hex(8)


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
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(50))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuthSession(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    totp_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)


class Pat(Base):
    __tablename__ = "pats"
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(50))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[str] = mapped_column(String(20), default="read")  # "read" 或 "read,write"
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)  # 个人空间(每用户一个,不可删/不可管成员)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(10), default="VIEWER")  # OWNER/ADMIN/EDITOR/VIEWER
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Doc(Base):
    __tablename__ = "docs"
    __table_args__ = (Index("ix_docs_project_deleted", "project_id", "deleted_at"),)
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="无标题文档")
    content: Mapped[str] = mapped_column(Text, default="")
    sort: Mapped[float] = mapped_column(Float, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class DocVersion(Base):
    __tablename__ = "doc_versions"
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=new_id)
    doc_id: Mapped[str] = mapped_column(ForeignKey("docs.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Folder(Base):
    __tablename__ = "folders"
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)  # 一切归属项目
    parent_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class File(Base):
    __tablename__ = "files"
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)  # 一切归属项目
    folder_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(300), unique=True)
    created_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
