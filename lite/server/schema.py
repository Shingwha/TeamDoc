"""数据库结构:唯一来源是 models.py,启动时建表并自检是否与模型一致。

## 只有一份结构定义

`models.py` 是结构的唯一来源,没有"迁移历史"这第二个来源 —— 那个设计会让
"模型改了什么"与"库里实际是什么"分头演进,而漂移是静默的:等它表现出来时
(某条 INSERT 报 NOT NULL constraint failed、某个字段怎么也对不上)已经离病灶很远了。

启动时:

1. `create_all` —— 建缺失的表(全新库一次建全;已有表不动)
2. `check_drift` —— 逐表逐列比对,三类问题都报:

   * **库里有、模型没有** → 残留列。NOT NULL 且无默认值时,任何不带它的 INSERT
     都会失败;可空/有默认值的虽无害,也一并报出来提醒清理。
   * **模型有、库里没有** → create_all 不会给已有表加列,运行时会报 no such column。
   * **两边都有、类型对不上** → 名字还在,读能过,直到按值匹配或写入时才炸。

对不上就**启动失败并说清楚怎么处理**,而不是带着隐患运行。处理方式只有重建
(停服 → 删数据目录 → 按 models.py 一次生成);需要保住的内容先用旧版本跑起来导出。
"认出结构不符"与"兼容两种结构"是两件事:前者是几行判定换一句能照做的提示,
后者才是要长期背着的负担。
"""
import os

import models
from media import guess_mime
from sqlalchemy import Integer, String, Text, text


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

    为什么必须做:全站信任库里的 mime(搜索、预览、内嵌判定都读它),而判定规则
    在 media.py 一处演进 —— 规则变了(新增扩展名、收紧白名单)存量行不会自己跟上。
    每次启动对齐一次,读时就不必再重算。幂等:值已一致的行不动。
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
            elif p["kind"] == "type":
                lines.append(f"  · {p['table']}.{p['column']} 库里是 {p['type']},"
                             f"模型要求 {p['want']} —— 两边的值对不上")
            else:
                lines.append(f"  · {p['table']}.{p['column']} 模型有但库里缺此列 —— 读写会报 no such column")
        raise RuntimeError(
            "数据库结构与 models.py 不一致:\n" + "\n".join(lines) +
            "\n\n唯一的结构来源是 models.py,这里不提供结构迁移。处理方式:停服后删掉"
            "数据目录重新初始化(库会按 models.py 一次生成;需要保住的内容先用旧版本"
            "跑起来导出)。\n**不要**带着不匹配的结构启动:名字相同而类型不同最容易漏过 ——"
            "读能过,写与按值匹配要到运行时才炸。")
    # 结构确认无误后再归一数据:storage_path 回归 basename、mime 与服务端判定对齐
    normalize_storage_paths(engine)
    normalize_mimes(engine)


def check_drift(engine) -> list[dict]:
    """逐表比对模型与库,返回差异列表(启动自检与备份校验共用这一个结构化判定)。

    返回项:{table, column, type, want, kind: 'extra'|'missing'|'type', blocking}
    - extra   库里多出的列。blocking 表示它 NOT NULL 且无默认值,会阻断插入。
    - missing 模型有、库里没有的列 —— 读写会报 no such column。
    - type    两边都有但**类型对不上**。比缺列更隐蔽:名字还在,读能过,
              直到按值匹配或写入时才表现出"怎么也对不上"。
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
                    "table": table.name, "column": name, "type": info["type"], "want": "",
                    "kind": "extra",
                    "blocking": info["notnull"] and info["default"] is None,
                })
            for name in sorted(model_cols - set(actual)):
                problems.append({
                    "table": table.name, "column": name, "type": "", "want": "",
                    "kind": "missing", "blocking": False,
                })
            for name in sorted(model_cols & set(actual)):
                want = _declared_type(table.columns[name])
                got = _type_word(actual[name]["type"])
                if not want or not got or want == got:
                    continue
                problems.append({"table": table.name, "column": name,
                                 "type": actual[name]["type"], "want": want,
                                 "kind": "type", "blocking": False})
    return problems


def _type_word(decl: str) -> str:
    """库里的类型声明归一成首词:VARCHAR(26) / varchar 都算 VARCHAR。"""
    return (decl or "").upper().split("(")[0].strip()


def _declared_type(col) -> str:
    """模型侧的类型,归一到"整数 / 字符串"两档(Text 单独认,它声明的就是 TEXT)。

    SQLite 的类型是自由文本(VARCHAR(26) 的大小写与空格写法都能存),逐字符比对
    只会得到噪声。而本项目真正会踩的错列型就是"id 该是字符串却是整数"这一类 ——
    判这条分界足够,其余类型不参与判定(交给缺列/多列两条)。
    """
    if isinstance(col.type, Text):      # Text 是 String 的子类,必须先判
        return "TEXT"
    if isinstance(col.type, String):
        return "VARCHAR"
    if isinstance(col.type, Integer):
        return "INTEGER"
    return ""
