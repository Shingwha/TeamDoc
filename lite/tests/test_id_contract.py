"""id 契约与引用契约的单元层(不经过 HTTP,直接导入服务端模块)。

放在这里的原因:这两条契约的实现各自只有一两行,而消费者遍布全站(id 出现在路由、
payload、dataset、正文、备份)。靠"记得去看一眼"维持一致是不可靠的,所以把形状与
语法钉成断言。

选型是确定的,不再讨论:ULID(不可枚举、一种形态、字典序 = 时间序)、
引用只认 id-only(项目归属会变,引用必须跨项目成立)。
"""
import pytest
from sqlalchemy import Integer, MetaData, String, create_engine, text

import ids
import refs
import schema


# ---------- id 形状 ----------

def test_new_id_shape_and_alphabet():
    seen = {ids.new_id() for _ in range(200)}
    assert len(seen) == 200, "id 必须唯一(80 位随机)"
    for v in seen:
        assert len(v) == 26, v
        assert ids.is_id(v), v
        # Crockford Base32 的字母表排除 I/L/O/U —— 手抄和口头传达时不会认错
        assert not set(v) & set("ILOU"), f"出现了易混字符:{v}"
        assert v == v.upper(), "库里只存大写形态"


def test_id_order_is_time_order():
    """字典序 = 创建顺序(列表与树的排序依赖这条)。"""
    import time
    a = ids.new_id()
    time.sleep(0.003)   # 跨毫秒:同一毫秒内是随机序,所以必须真的等一小会儿
    b = ids.new_id()
    time.sleep(0.003)
    c = ids.new_id()
    assert a < b < c
    assert sorted([c, a, b]) == [a, b, c]


def test_is_id_rejects_bad_shapes():
    good = ids.new_id()
    bad = ["", "abc", good[:-1], good + "a", "9" * 27, "0" * 25,
           good.replace(good[0], "I"), good.replace(good[0], "O"),
           good.replace(good[0], "L"), good.replace(good[0], "U"),
           good.lower()]
    for v in bad:
        assert not ids.is_id(v), f"应判非法:{v!r}"
    assert not ids.is_id(None) and not ids.is_id(12345) and not ids.is_id(["x"])


def test_normalize_id_only_normalizes_case():
    v = ids.new_id()
    assert ids.normalize_id(v.lower()) == v, "链接被人手工改小写也要认得"
    assert ids.normalize_id("  " + v) is None, "不 trim,不做别的猜测"
    assert ids.normalize_id("999999") is None, "纯数字不是 id"
    assert ids.normalize_id(None) is None


def test_numeric_shape_is_invalid_not_absent():
    """数字不是"另一种 id",而是**形状非法**。这条决定了 API 行为:

    路径/参数里带旧式数字 → 400 VALIDATION(坏链接);形状合法但不存在的 ULID
    → 404(资源没了)。两者语义不同,测试里也别混用(见 _harness.ABSENT_ID)。
    """
    assert not ids.is_id("999999")
    assert ids.is_id("0" * 26), "形状合法(毫秒时间戳 0),但 new_id() 永远生成不出来"


def test_idpath_validation_is_the_same_judgement():
    """路由用的 IdPath 与 is_id 必须是同一判定(否则"路由放行、查询落空")。"""
    import pydantic
    v = ids.new_id()
    ta = pydantic.TypeAdapter(ids.IdPath)
    assert ta.validate_python(v.lower()) == v, "IdPath 也做大小写归一"
    for bad in ("999999", "", "!" * 26, "i" * 26):   # i/I 不在字母表里
        with pytest.raises(pydantic.ValidationError):
            ta.validate_python(bad)


# ---------- 引用语法 ----------

def test_refs_recognizes_both_forms():
    fid, did = ids.new_id(), ids.new_id()
    content = (
        f"chip 文档 [@标题](teamdoc://doc/{did}) "
        f"chip 文件 [@名称](teamdoc://file/{fid}) "
        f"附件 [名称](/api/files/{fid}/download) "
        f"内嵌 ![名称](/api/files/{fid}/download?inline=1)"
    )
    assert refs.doc_ids_in(content) == {did}
    assert refs.file_ids_in(content) == {fid}


def test_refs_ignores_non_id_candidates():
    """只有 id-only 的规范形态算引用 —— 没有兼容分支。"""
    fid = ids.new_id()
    assert refs.file_ids_in("[a](/api/files/999999/download)") == set()
    assert refs.file_ids_in("[a](teamdoc://file/999999)") == set()
    assert refs.file_ids_in(f"[a](teamdoc://file/{fid}x)") == set(), "26 位之后的尾巴不算"
    assert refs.doc_ids_in(f"[@x](teamdoc://doc/{ids.new_id()}/{ids.new_id()})") == set(), \
        "带项目 id 的两段式不是本契约的形态"


def test_refs_generation_round_trips():
    fid, did = ids.new_id(), ids.new_id()
    assert refs.doc_ids_in(f"[@x]({refs.doc_ref(did)})") == {did}
    assert refs.file_ids_in(f"[@x]({refs.file_ref(fid)})") == {fid}
    assert refs.download_url(fid) == f"/api/files/{fid}/download"
    assert refs.download_url(fid, True) == f"/api/files/{fid}/download?inline=1"
    assert refs.file_ids_in(f"[@x]({refs.download_url(fid, True)})") == {fid}


def test_sql_prefilter_is_not_stricter_than_parser():
    """粗筛不能比精确判定更严:否则"正文里明明有引用"的行在 SQL 阶段就被滤掉。"""
    fid, did = ids.new_id(), ids.new_id()
    for sample in (f"[a](/api/files/{fid}/download)", f"[@x](teamdoc://file/{fid})",
                   f"[@x](teamdoc://doc/{did})"):
        assert any(p in sample for p in ("/api/files/", "teamdoc://doc/", "teamdoc://file/")), sample
    assert refs.file_ids_in("") == set() and refs.doc_ids_in("") == set()
    assert refs.file_ids_in(None) == set()


# ---------- 结构与备份的一致性 ----------

def _reverse_types(md: MetaData) -> MetaData:
    """派生一份"id 是整数"的旧结构:用来验证结构自检能认出类型不符。"""
    old = MetaData()
    for t in md.sorted_tables:
        t.to_metadata(old)
    for t in old.sorted_tables:
        for c in t.columns:
            if isinstance(c.type, String) and (c.name == "id" or c.name.endswith("_id")):
                c.type = Integer()
    return old


def test_check_drift_reports_type_mismatch(tmp_path):
    """名字还在、类型变了:必须报出来(这类漂移读能过,写与匹配才炸)。"""
    import models
    path = tmp_path / "old.db"
    eng = create_engine(f"sqlite:///{path}")
    _reverse_types(models.Base.metadata).create_all(eng)
    try:
        problems = schema.check_drift(eng)
    finally:
        eng.dispose()
    kinds = {p["kind"] for p in problems}
    assert "type" in kinds, f"应报出类型不符:{problems[:5]}"
    assert {"users", "docs"} <= {p["table"] for p in problems if p["kind"] == "type"}
    # 结构不符 → 启动自检直接拒绝(附重建指引)
    eng = create_engine(f"sqlite:///{path}")
    try:
        with pytest.raises(RuntimeError) as e:
            schema.init(eng)
        assert "结构与 models.py 不一致" in str(e.value)
    finally:
        eng.dispose()


def test_check_drift_on_current_schema_is_clean(tmp_path):
    """按 models.py 建出来的库必须零差异 —— 这条保证上面那类断言不会误报。"""
    import models
    path = tmp_path / "fresh.db"
    eng = create_engine(f"sqlite:///{path}")
    try:
        models.Base.metadata.create_all(eng)
        assert schema.check_drift(eng) == []
        schema.init(eng)   # 不抛错
    finally:
        eng.dispose()
