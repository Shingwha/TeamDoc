"""文档正文写入的基线校验与版本规则(见 docs.save_doc_content / ws.py)。

背景:此前保存是纯 LWW —— 后写者整篇覆盖,先写者那几分钟的字静默消失,
而双方界面都显示"已保存 ✓"。这里把"必须声明基于哪一版改"变成受测契约:

  * PUT:基线过期 409(且服务端内容一字未动)、缺基线 400(仅 web 会话)、同内容短路不判冲突
  * WS:冲突只回发送者本人(不回 saved、不向其他人广播 remote)
  * append / restore 是显式豁免路径,文档前进后仍能写入
  * 空内容不留还原点(新建文档的第一次写入不产生历史)

进程内并发(两个请求同时到)测不出这类问题 —— 冲突的判定对象是"版本号",
所以用"先写一次把版本推上去,再拿旧基线写"来构造,等价且可重复。
"""
import json
import time

import pytest

from _harness import Client


def _new_project(admin, name):
    r = admin.post("/api/projects", {"name": name})
    assert r.status == 200, f"建项目失败: {r.status} {r.data}"
    return r.data["id"]


def _new_doc(admin, pid, title="并发文档"):
    r = admin.post(f"/api/projects/{pid}/docs", {"title": title})
    assert r.status == 200, f"建文档失败: {r.status} {r.data}"
    return r.data


def _put(admin, did, content, base_version, expect=200):
    """写正文;base_version 传 None 表示**不带**该字段(用于测缺失被拒)"""
    body = {"content": content}
    if base_version is not None:
        body["baseVersion"] = base_version
    r = admin.put(f"/api/docs/{did}/content", body)
    assert r.status == expect, f"写正文应返回 {expect}: {r.status} {r.data}"
    return r


def _content(admin, did):
    return admin.get(f"/api/docs/{did}").data


def _ws_uri(base_url, did):
    return base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/docs/{did}"


def _wait(conn, mtype, timeout=10):
    """等指定类型的消息:连接建立后服务端会先推 presence,不能假定首条就是目标"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(conn.recv(timeout=timeout))
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"未收到 {mtype} 消息")


def _expect_none(conn, mtype, secs=1.5):
    """secs 内不应收到该类型消息;其它类型(presence 等)照吃,不算违规"""
    deadline = time.time() + secs
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            return
        try:
            msg = json.loads(conn.recv(timeout=remain))
        except TimeoutError:
            return
        assert msg.get("type") != mtype, f"不该收到 {mtype} 消息: {msg}"


def test_put_stale_base_conflicts_and_keeps_server_content(admin):
    """基线过期 → 409,且 409 里带回现场;服务端内容一字未动(不静默覆盖)。"""
    pid = _new_project(admin, "基线冲突")
    doc = _new_doc(admin, pid)
    did = doc["id"]
    assert doc["version"] == 0, f"新文档版本从 0 起: {doc}"

    r1 = _put(admin, did, "# 先写的\n", 0)
    assert r1.data["version"] == 1, f"保存成功版本 +1: {r1.data}"

    # 另一个人(或另一个标签页)手上还是 v0,此刻才点保存
    r2 = _put(admin, did, "# 后写的(基于旧版)\n", 0, expect=409)
    detail = r2.data["detail"]
    assert detail["code"] == "CONFLICT", f"409 错误码: {detail}"
    assert detail["currentVersion"] == 1, f"带回服务端当前版本: {detail}"
    assert detail["currentContent"] == "# 先写的\n", \
        f"带回服务端当前正文(客户端要拿它做差异对比): {detail}"

    cur = _content(admin, did)
    assert cur["content"] == "# 先写的\n", f"服务端内容未被覆盖: {cur}"
    assert cur["version"] == 1, f"版本未被推进: {cur}"

    # 拿到现场后按新基线重发 = 用户在弹窗里选了"保留我的" → 必须成功
    r3 = _put(admin, did, "# 后写的(基于旧版)\n", detail["currentVersion"])
    assert r3.data["version"] == 2, f"按新基线重发应成功: {r3.data}"
    # 被覆盖的那份仍能从历史取回(服务端在落库前存了"覆盖前"快照)
    vers = admin.get(f"/api/docs/{did}/versions").data
    assert vers, "覆盖后应有历史版本可回退"


def test_put_base_version_required_only_for_web(base_url, admin):
    """基线的必填只对浏览器这一档;PAT(CLI/脚本)缺省即覆盖。

    分档的意义:挡住"陈旧标签页整篇盖回去",同时不让程序化写入被迫假装自己持有缓冲。
    显式带了基线就一律照查 —— 分档放宽的是"缺省",不是"声明了不生效"。
    """
    pid = _new_project(admin, "基线分档")
    did = _new_doc(admin, pid)["id"]

    # Web 会话(带 Cookie):缺基线 400、非整数 400
    r = _put(admin, did, "# 无基线\n", None, expect=400)
    assert r.data["detail"]["code"] == "VALIDATION", f"Web 缺基线错误码: {r.data}"
    bad = admin.put(f"/api/docs/{did}/content", {"content": "x", "baseVersion": "abc"})
    assert bad.status == 400, f"非整数基线应 400: {bad.status} {bad.data}"
    assert _content(admin, did)["content"] == "", "两次都没写进去"

    # PAT(CLI 同款):不带基线 = 无条件覆盖,零改动可用
    pat = admin.post("/api/auth/pats", {"name": "分档测试", "scopes": "read,write"})
    assert pat.status == 200, f"建 PAT 失败: {pat.status} {pat.data}"
    cli = Client(base_url, bearer=pat.data["token"])
    ok = cli.put(f"/api/docs/{did}/content", {"content": "# PAT 写的\n"})
    assert ok.status == 200, f"PAT 不带基线应放行: {ok.status} {ok.data}"
    assert _content(admin, did)["content"] == "# PAT 写的\n", "PAT 的覆盖生效"

    # PAT 显式带了过期基线 → 照样 409,且内容不动
    stale = cli.put(f"/api/docs/{did}/content",
                    {"content": "# 过期基线\n", "baseVersion": 0})
    assert stale.status == 409, f"PAT 带过期基线应被拦: {stale.status} {stale.data}"
    assert stale.data["detail"]["code"] == "CONFLICT", f"错误码: {stale.data}"
    assert _content(admin, did)["content"] == "# PAT 写的\n", "被拦下时内容不动"


def test_put_same_content_is_not_a_conflict(admin):
    """内容一字不差时短路放行:没有可丢的东西,不该弹冲突窗打扰人。"""
    pid = _new_project(admin, "同内容短路")
    did = _new_doc(admin, pid)["id"]
    _put(admin, did, "# 同样\n", 0)

    r = _put(admin, did, "# 同样\n", 0)   # 基线已过期,但内容完全相同
    assert r.status == 200 and r.data["version"] == 1, f"同内容应短路放行: {r.status} {r.data}"


def test_append_ignores_base(admin):
    """append 是豁免路径:读-改-写在服务端同一次事务里完成,不覆盖别人的正文。"""
    pid = _new_project(admin, "追加豁免")
    did = _new_doc(admin, pid)["id"]
    _put(admin, did, "# 第一段", 0)

    r = admin.post(f"/api/docs/{did}/append", {"content": "追加段"})
    assert r.status == 200, f"追加不校验基线: {r.status} {r.data}"
    assert _content(admin, did)["content"] == "# 第一段\n\n追加段", "追加拼在当下正文之后"


def test_restore_version_ignores_base(admin):
    """restore 是豁免路径:用户看着历史主动覆盖,现场另有回退点快照兜底。"""
    pid = _new_project(admin, "还原豁免")
    did = _new_doc(admin, pid)["id"]
    _put(admin, did, "# 甲\n", 0)
    _put(admin, did, "# 乙\n", 1)

    vers = admin.get(f"/api/docs/{did}/versions").data
    assert vers, "至少有一条覆盖前的快照"
    vid = vers[-1]["id"]                       # 最早那条 = 最早可回退的还原点
    old = admin.get(f"/api/docs/{did}/versions/{vid}").data["content"]

    r = admin.post(f"/api/docs/{did}/versions/{vid}/restore", {})
    assert r.status == 200, f"还原不校验基线: {r.status} {r.data}"
    assert _content(admin, did)["content"] == old, "内容回到该版本"


def test_version_record_shape(admin):
    """版本记录的契约:kind 描述成因(界面据它决定怎么称呼),内容大小与作者名由服务端给。

    列表要的是"哪一版"的线索 —— 什么时候、谁、多大。作者名在这里解析(与文件列表同一
    形状),前端就不必为一个列表再拉一次同事目录做 join。
    """
    pid = _new_project(admin, "版本记录形状")
    me = admin.get("/api/auth/me").data["user"]
    did = _new_doc(admin, pid)["id"]
    _put(admin, did, "# 第一版\n", 0)
    _put(admin, did, "# 第二版\n", 1)

    rows = admin.get(f"/api/docs/{did}/versions").data
    assert len(rows) == 1, f"只有一条还原点: {rows}"
    r = rows[0]
    assert r["kind"] == "save", f"普通保存的成因是 save: {r}"
    assert r["contentChars"] == len("# 第一版\n"), f"带内容大小(挑版本用): {r}"
    assert r["createdBy"] == {"id": me["id"], "name": me["name"]}, \
        f"作者已解析成 {{id, name}}(与文件列表同形): {r}"


def test_restore_marks_a_restore_point(admin):
    """回退会在历史里留下一条 kind=restore:「回退前的现场」是唯一需要被标出的成因。"""
    pid = _new_project(admin, "回退点标记")
    did = _new_doc(admin, pid)["id"]
    _put(admin, did, "# 甲\n", 0)
    _put(admin, did, "# 乙\n", 1)
    vid = admin.get(f"/api/docs/{did}/versions").data[0]["id"]

    assert admin.post(f"/api/docs/{did}/versions/{vid}/restore", {}).status == 200
    kinds = [r["kind"] for r in admin.get(f"/api/docs/{did}/versions").data]
    assert kinds.count("restore") == 1, f"回退点恰好一条,且成因是 restore: {kinds}"
    assert set(kinds) <= {"save", "restore"}, f"成因只有这两档: {kinds}"


def test_no_restore_point_for_empty_content(admin):
    """空内容不留还原点。

    空不是一种状态,而是所有状态的缺省(想得到空文档,全选删掉即可)。留下它只会让
    每篇文档的历史首条点进去是一片空白;而真正要救的"清空之前的内容",由清空那次
    保存照常快照下来 —— 那条才是用户要找的东西。
    """
    pid = _new_project(admin, "空还原点")
    did = _new_doc(admin, pid)["id"]

    _put(admin, did, "# 第一版\n", 0)
    assert admin.get(f"/api/docs/{did}/versions").data == [], \
        "新建文档的第一次写入不产生历史(旧内容为空)"

    _put(admin, did, "# 第二版\n", 1)
    vers = admin.get(f"/api/docs/{did}/versions").data
    assert len(vers) == 1, f"第二次写入才产生第一个还原点(实际 {len(vers)} 条)"
    first = admin.get(f"/api/docs/{did}/versions/{vers[0]['id']}").data["content"]
    assert first == "# 第一版\n", f"还原点是上一版内容: {first!r}"

    # 清空后再写:空状态同样不成为还原点(与"新建即空"是同一条规则)
    _put(admin, did, "", 2)
    _put(admin, did, "# 又一版\n", 3)
    vers2 = admin.get(f"/api/docs/{did}/versions").data
    contents = [admin.get(f"/api/docs/{did}/versions/{v['id']}").data["content"] for v in vers2]
    assert contents, "仍有可回退的还原点"
    assert "" not in contents, f"不该有内容为空的还原点: {[c[:10] for c in contents]}"
    # 清空之前的正文必须还在,否则就是"去掉了噪音也去掉了数据"
    assert "# 第二版\n" in contents, f"清空前的那份要能找回: {[c[:10] for c in contents]}"


def test_ws_conflict_goes_only_to_sender(base_url, admin):
    """WS:过期基线的保存只回发送者 conflict;其他人不该收到 remote(服务端没写库)。"""
    ws_sync = pytest.importorskip("websockets.sync.client")
    pid = _new_project(admin, "WS 冲突")
    did = _new_doc(admin, pid)["id"]
    uri = _ws_uri(base_url, did)
    hdr = {"Cookie": "td_sid=" + admin.sid}

    a = ws_sync.connect(uri, additional_headers=hdr, open_timeout=10, max_queue=None, legacy=True)
    b = ws_sync.connect(uri, additional_headers=hdr, open_timeout=10, max_queue=None, legacy=True)
    try:
        # connect() 返回只代表 accept 完成;B 真正入池的信号是随后服务端推的 presence
        # (ws.py 注册完立即广播)。不等它就保存,remote 广播可能跑在 B 入池之前 —— 偶发超时。
        _wait(b, "presence")
        # A 以基线 0 保存 → saved v1,并广播给 B
        a.send(json.dumps({"type": "content", "content": "# A 的\n", "baseVersion": 0}))
        saved = _wait(a, "saved")
        assert saved["version"] == 1, f"A 保存成功: {saved}"
        assert _wait(b, "remote")["content"] == "# A 的\n", "B 收到 A 的改动"

        # B 手上还是 v0,这时保存 → 只回 conflict
        b.send(json.dumps({"type": "content", "content": "# B 的\n", "baseVersion": 0}))
        cf = _wait(b, "conflict")
        assert cf["version"] == 1, f"conflict 带服务端版本: {cf}"
        assert cf["content"] == "# A 的\n", f"conflict 带服务端正文: {cf}"
        _expect_none(b, "saved", 1.0)
        _expect_none(a, "remote", 1.5)          # 没写库,就不该有人被通知

        assert _content(admin, did)["content"] == "# A 的\n", "服务端仍是 A 的内容"

        # 采纳现场版本号后重发 → 正常保存(A 收到 B 的改动)
        b.send(json.dumps({"type": "content", "content": "# B 的\n",
                           "baseVersion": cf["version"]}))
        assert _wait(b, "saved")["version"] == 2, "按冲突现场版本号重发应成功"
        assert _wait(a, "remote")["content"] == "# B 的\n", "A 收到 B 的改动"
    finally:
        a.close()
        b.close()


def test_ws_save_without_base_is_ignored(base_url, admin):
    """没带基线的整篇覆盖一律不写:旧页面应当明确失败(刷新即可),而不是继续静默盖掉别人。"""
    ws_sync = pytest.importorskip("websockets.sync.client")
    pid = _new_project(admin, "WS 缺基线")
    did = _new_doc(admin, pid)["id"]
    uri = _ws_uri(base_url, did)

    with ws_sync.connect(uri, additional_headers={"Cookie": "td_sid=" + admin.sid},
                         open_timeout=10, max_queue=None, legacy=True) as ws:
        ws.send(json.dumps({"type": "content", "content": "# 无基线\n"}))
        _expect_none(ws, "saved", 1.5)
    assert _content(admin, did)["content"] == "", "没写进去"
