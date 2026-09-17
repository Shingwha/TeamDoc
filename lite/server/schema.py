"""数据库结构:唯一来源是 models.py,启动时建表并自检是否与模型一致。

## 只有一份结构定义

`models.py` 是结构的唯一来源,没有"迁移历史"这第二个来源 —— 那会让"模型改了什么"
与"库里实际是什么"分头演进,而漂移是静默的:等它表现出来时(某条 INSERT 报
NOT NULL constraint failed、某个字段怎么也对不上)已经离病灶很远了。

启动时:

1. `create_all` —— 建缺失的表(全新库一次建全;已有表不动)
2. `check_drift` —— 逐表逐列比对,三类问题都报:

   * **库里有、模型没有** → 残留列。NOT NULL 且无默认值时,任何不带它的 INSERT
     都会失败;可空/有默认值的虽无害,也一并报出来提醒清理。
   * **模型有、库里没有** → create_all 不会给已有表加列,运行时会报 no such column。
   * **两边都有、类型对不上** → 名字还在,读能过,直到按值匹配或写入时才炸。

对不上就**拒绝启动**,并给出那一条可执行的操作。启动路径上不做任何数据改写:
库里只存无法重算的事实,所以没有"启动时回填一次"这种东西需要存在。
"""
import models
from sqlalchemy import Integer, String, Text, text


def init(engine) -> None:
    """建表 + 自检。结构不一致直接抛 RuntimeError(附带那一条可执行的操作)。"""
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
            f"\n\n处理:停服,删掉 {models.DB_PATH},重启(会按 models.py 一次建全)。")


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


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
    ).first() is not None


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
