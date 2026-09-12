"""数据库结构:唯一来源是 models.py,启动时建表并自检是否与模型一致。

## 为什么没有"迁移"

开发期曾有一套迁移系统(schema_version 表 + 有序迁移列表),本意是让存量库自动追平
模型。实践下来它引入了一个更糟的问题:**schema 有了两个来源**(models.py 与迁移历史),
两者会漂移,而漂移是静默的 —— `projects.color` 就是这样埋了很久:模型里删了字段、
迁移里没删列,INSERT 时不带它,SQLite 报 NOT NULL constraint failed,要到用户
"新建项目"时才炸出来。修复它的方式又是再写一条迁移(打补丁),而不是消除病根。

现在只有一个来源:`models.py`。启动时:

1. `create_all` —— 建缺失的表(对全新库一次建全;已有表不动)
2. `check_drift` —— 逐表比对列,双向报告:

   * **库里有、模型没有** → 残留列。若是 NOT NULL 无默认值,任何不带它的 INSERT
     都会失败(就是上面那个 bug);可空/有默认值的虽无害,也一并报出来提醒清理。
   * **模型有、库里没有** → create_all 不会给已有表加列,这会让运行时报
     "no such column"。必须报出来。

有漂移就**启动失败并说清楚怎么处理**,而不是带着隐患运行。开发期处理方式:删掉数据
目录重建(库会按 models.py 生成),或写一条重建语句。数据可丢,所以不做自动迁移。

## 以后真的要上生产、且数据不能丢时

那时再见招拆招:引入迁移是为"保数据",不是为"省事"。到了那一步也不要退回
"迁移历史与模型并存"的老路,而是让迁移只做 models.py 表达不了的事(数据搬迁、
回填),结构本身始终由 models.py 定义。
"""
import os

import models
from media import guess_mime
from sqlalchemy import text


def normalize_storage_paths(engine) -> int:
    """把 files.storage_path 里的历史绝对路径回填为 basename。返回改动行数。

    为什么必须做:绝对路径与机器绑定,一旦恢复数据到另一台机器或改了
    TEAMDOC_DATA_DIR,库里所有路径失效、文件全部 404,而孤儿扫描按 basename
    比对发现不了(磁盘上文件还在)。统一成 basename 后,物理位置只由
    FILES_DIR 决定,数据目录才真正可搬迁。

    幂等:已经是 basename 的行不动;只处理绝对路径。
    """
    changed = 0
    with engine.begin() as conn:
        if not _table_exists(conn, "files"):
            return 0
        rows = conn.execute(text("SELECT id, storage_path FROM files")).all()
        for fid, sp in rows:
            if not sp:
                continue
            if os.path.isabs(sp):
                name = os.path.basename(sp)
                if name and name != sp:
                    conn.execute(text("UPDATE files SET storage_path=:n WHERE id=:i"),
                                 {"n": name, "i": fid})
                    changed += 1
    return changed


def normalize_mimes(engine) -> int:
    """把 files.mime 回填为服务端按文件名判定的值。返回改动行数。

    为什么必须做:上传早期的存量记录采信过客户端声明的 mime,搜索与最近动态
    因此一直在读时重算 guess_mime —— 同一份数据两种口径,必然漂移。回填后
    全站统一信任库值,读时重算全部删除(与 normalize_storage_paths 同一模式)。
    幂等:值已一致的行不动。
    """
    changed = 0
    with engine.begin() as conn:
        if not _table_exists(conn, "files"):
            return 0
        rows = conn.execute(text("SELECT id, name, mime FROM files")).all()
        for fid, name, mime in rows:
            want = guess_mime(name or "")
            if mime != want:
                conn.execute(text("UPDATE files SET mime=:m WHERE id=:i"),
                             {"m": want, "i": fid})
                changed += 1
    return changed


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
    ).first() is not None


def init(engine) -> None:
    """建表 + 自检 + 数据归一。结构不一致直接抛 RuntimeError(附带可执行的修复指引)。"""
    models.Base.metadata.create_all(engine)
    problems = check_drift(engine)
    if problems:
        lines = []
        for p in problems:
            if p["kind"] == "extra":
                risk = ("NOT NULL 且无默认值,会阻断插入" if p["blocking"]
                        else "可空/有默认值,无害但应清理")
                lines.append(f"  · {p['table']}.{p['column']} 库里多出此列({p['type']})—— {risk}")
            else:
                lines.append(f"  · {p['table']}.{p['column']} 模型有但库里缺此列 —— 读写会报 no such column")
        raise RuntimeError(
            "数据库结构与 models.py 不一致:\n" + "\n".join(lines) +
            "\n\n这是 schema 漂移,不是数据问题。开发期最省事的处理:停服后删掉数据目录"
            "重新初始化(库会按 models.py 一次生成;备份目录里的旧数据可另行取用)。"
            "\n若这次不能丢数据,就在重建前把有用内容导出,或手工执行对应的 ALTER TABLE。")
    # 结构确认无误后再归一数据:把历史绝对 storage_path 回填为 basename,
    # 否则换机/改数据目录恢复后所有文件会 404(见 normalize_storage_paths)
    normalize_storage_paths(engine)
    normalize_mimes(engine)
    seed_id_start(engine)


# 资源 id 起点 = 10000(五位数):首条数据拿到 10000。行业惯例是"垫高起点的自增"
# (Discuz 系论坛的 uid 起跳、QQ 号同理)——整齐、也不显得像测试账号。种子只对
# "从未插入过数据"的表生效(没有 sequence 行才补),幂等;存量表由迁移工具重编号。
_ID_START = 10000
_ID_TABLES = ("users", "pats", "projects", "project_members",
              "docs", "doc_versions", "folders", "files", "login_events")


def seed_id_start(engine) -> None:
    """给空的资源表写入自增种子(sqlite_sequence = 起点-1,首条数据 = 起点)。幂等。"""
    with engine.begin() as conn:
        if not _table_exists(conn, "sqlite_sequence"):
            return  # 还没有任何 AUTOINCREMENT 插入,序列表尚未创建;首次插入后自然生效
        for t in _ID_TABLES:
            row = conn.execute(
                text("SELECT seq FROM sqlite_sequence WHERE name = :n"), {"n": t}).first()
            if row is None:
                conn.execute(
                    text("INSERT INTO sqlite_sequence(name, seq) VALUES (:n, :s)"),
                    {"n": t, "s": _ID_START - 1})


def check_drift(engine) -> list[dict]:
    """逐表比对模型与库的列,返回差异列表。

    返回项:{table, column, type, kind: 'extra'|'missing', blocking: bool}
    blocking 只对 extra 有意义:NOT NULL 且无默认值的残留列会让 INSERT 失败。
    """
    problems: list[dict] = []
    with engine.begin() as conn:
        for table in models.Base.metadata.sorted_tables:
            if not _table_exists(conn, table.name):
                continue  # 还没建的表由 create_all 负责
            model_cols = {c.name for c in table.columns}
            # PRAGMA table_info 的列:cid, name, type, notnull, dflt_value, pk
            rows = conn.execute(text(f"PRAGMA table_info({table.name})")).all()
            actual = {r[1]: {"type": r[2], "notnull": bool(r[3]), "default": r[4]} for r in rows}
            for name, info in actual.items():
                if name in model_cols:
                    continue
                problems.append({
                    "table": table.name, "column": name, "type": info["type"],
                    "kind": "extra",
                    "blocking": info["notnull"] and info["default"] is None,
                })
            for name in sorted(model_cols - set(actual)):
                problems.append({
                    "table": table.name, "column": name, "type": "",
                    "kind": "missing", "blocking": False,
                })
    return problems
