"""数据库迁移:按版本号顺序补齐存量库的结构改动。

为什么需要:`Base.metadata.create_all()` 只建缺失的**表**,不会给已有的表加**列**。
一旦要新增字段(项目可见性、文件公开等),存量库就必须手动补列,否则运行时才暴露
"no such column"。这里用一张 schema_version 表 + 有序迁移列表把它自动化:
启动时比对版本号,只执行未执行的迁移。

用法(新增字段时):
    1. 在 models.py 给模型加上字段(create_all 负责新库)
    2. 在 MIGRATIONS 末尾追加 (版本号, 说明, 执行函数),函数内用 _add_column 补存量库
    3. 不要修改已发布的迁移条目 —— 它们的版本号已被写入过库

SQLite 的 ALTER TABLE ADD COLUMN 有两条硬约束:不能加 NOT NULL 且无默认值的列,
默认值必须是常量。所以新增列一律"可空 + 默认值由应用层写入",见 _add_column 的注释。
"""
from sqlalchemy import text

import models


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
    ).first()
    return row is not None


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).all()}


def _drop_column(conn, table: str, column: str) -> bool:
    """删列;不存在则跳过(使迁移可重复执行)。返回是否真的执行了。

    依赖 SQLite 3.35+ 的原生 DROP COLUMN(本项目实测 3.47)。限制:不能删主键、
    UNIQUE 约束列、有索引的列 —— 本项目的用法都避开这些。
    早期 SQLite 需要"建新表 + 拷数据 + 换名"三步,那种写法风险高得多,故不在此兼容。
    """
    if column not in _columns(conn, table):
        return False
    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    return True


def _add_column(conn, table: str, column: str, decl: str) -> bool:
    """加列;已存在则跳过(使迁移可重复执行)。返回是否真的执行了。

    decl 需自带 SQLite 允许的常量默认值,例如 "VARCHAR(10) DEFAULT 'private'"。
    不给 NOT NULL:SQLite 只能为常量默认值加 NOT NULL 列,而布尔/时间类字段
    由应用层在写入时保证,模型侧用 default= 兜底。
    """
    if column in _columns(conn, table):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
    return True


# ---------- 迁移条目 ----------

def _m001_baseline(conn):
    """基线:建全部表(models.py 的唯一真相)。

    对所有版本的库都安全:已有表不动,缺的表补建。因此首次引入迁移机制时,
    存量库执行本步是空操作,新库则一次建全 9 张表。

    必须复用传入的 conn 而非 models.engine:外层事务已持有写锁,另开连接建表
    会在 SQLite 下撞 SQLITE_BUSY(create_all 接受 Connection,同样走这条路)。
    """
    models.Base.metadata.create_all(conn)


def _m002_visibility(conn):
    """项目可见性 + 单文件公开。

    可见性让"团队广场"成立:公开项目对本实例所有登录用户**只读**可见
    (鉴权上表现为非成员拿到 VIEWER,见 auth.project_role),不产生成员关系。
    单文件公开补"只想共享一个文件"的缺口。

    默认值 'private' / 0 保证存量数据行为完全不变 —— 迁移不改变任何既有的可见性。
    """
    _add_column(conn, "projects", "visibility", "VARCHAR(10) DEFAULT 'private'")
    _add_column(conn, "files", "is_public", "BOOLEAN DEFAULT 0")


def _m003_drop_totp(conn):
    """移除两步验证(TOTP)。

    原因:本项目是内网自部署、30 人规模、无公网暴露。TOTP 防的是"密码泄露后的
    第二次验证",而在这种场景下内部人有自己的账号,不需要破解他人密码;真正有效的
    账号管控手段是"禁用用户"与 PAT(可只读、可吊销)。保留一套没人用的安全机制
    只会增加代码面与运维负担(如"用户换手机后管理员必须重置"这类支持成本)。

    代价已知并接受:内网若有人拿到同事密码(共享密码、离职账号未禁用),不再有
    第二道防线。要恢复此能力需重做端点、前端面板与门禁,见 git 历史。

    AuthSession.totp_verified 一并删除 —— 它是"本次会话是否通过两步验证"的标记,
    没有 TOTP 就永远为真,留着只会误导。
    """
    _drop_column(conn, "users", "totp_secret")
    _drop_column(conn, "users", "totp_enabled")
    _drop_column(conn, "sessions", "totp_verified")


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "基线:建初始 9 张表", _m001_baseline),
    (2, "项目可见性 + 单文件公开", _m002_visibility),
    (3, "移除两步验证(TOTP)", _m003_drop_totp),
]


def _current_version(conn) -> int:
    if not _table_exists(conn, "schema_version"):
        return 0
    row = conn.execute(text("SELECT MAX(version) FROM schema_version")).first()
    return int(row[0]) if row and row[0] is not None else 0


def run(engine) -> int:
    """执行所有未应用的迁移,返回最终版本号。每个迁移一个事务。"""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY,"
            " description VARCHAR(200) NOT NULL,"
            " applied_at VARCHAR(32) NOT NULL)"))
    for version, description, fn in sorted(MIGRATIONS, key=lambda m: m[0]):
        with engine.begin() as conn:
            if version <= _current_version(conn):
                continue
            fn(conn)
            conn.execute(text(
                "INSERT INTO schema_version (version, description, applied_at)"
                " VALUES (:v, :d, datetime('now'))"), {"v": version, "d": description})
    with engine.begin() as conn:
        return _current_version(conn)
