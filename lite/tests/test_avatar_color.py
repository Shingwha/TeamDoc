"""验证头像取色统一(同一用户在所有接口返回同一 avatarColor)。

历史教训:本测试曾用固定邮箱 c1@/c2@teamdoc.local 建用户,依赖"忽略建号 409"——
系统没有删除用户接口,这些账号永久留在数据目录里,是全套脚本里唯一的脏数据源。
现在统一走 make_user 的随机后缀,数据目录又是一次性的,两层保险。
"""
import asyncio
import json

import pytest

from _harness import Client, make_user


def test_avatar_color_consistency(base_url, admin):
    me = admin.get("/api/auth/me").data["user"]
    admin_uid, admin_color = me["id"], me["avatarColor"]
    assert bool(admin_color), f"登录响应含 avatarColor: {me}"

    pid = admin.post("/api/projects", {"name": "取色测试项目"}).data["id"]
    u1_email, _ = make_user(admin, "甲", "color12345")
    u2_email, _ = make_user(admin, "乙", "color12345")
    directory = admin.get("/api/users/directory").data
    uid_by_email = {u["email"]: u["id"] for u in directory}
    admin.post(f"/api/projects/{pid}/members",
               {"userId": uid_by_email[u1_email], "role": "EDITOR"})
    admin.post(f"/api/projects/{pid}/members",
               {"userId": uid_by_email[u2_email], "role": "EDITOR"})

    # 1) 项目成员列表返回 avatarColor
    r = admin.get(f"/api/projects/{pid}/members")
    assert r.status == 200, f"成员列表 200: {r.status} {r.data}"
    colors = {}
    for m in r.data:
        u = m["user"]
        colors[u["id"]] = u.get("avatarColor")
        assert bool(u.get("avatarColor")), f"成员 {u['name']} 含 avatarColor: {u}"

    # 2) 管理员用户列表返回同一 avatarColor
    by_id = {u["id"]: u for u in admin.get("/api/users").data}
    for uid, c in colors.items():
        got = by_id.get(uid, {}).get("avatarColor")
        assert got == c, \
            f"用户 {by_id.get(uid, {}).get('name')} 的色值一致: 成员列表={c} 用户列表={got}"

    # 3) /api/auth/me 与成员列表一致
    me2 = admin.get("/api/auth/me").data["user"]
    assert me2["avatarColor"] == colors.get(admin_uid), \
        f"me.avatarColor == 成员列表中的自己: {me2['avatarColor']} vs {colors.get(admin_uid)}"

    # 4) 同一用户多次请求色值稳定
    again = {m["user"]["id"]: m["user"]["avatarColor"]
             for m in admin.get(f"/api/projects/{pid}/members").data}
    assert again == colors, f"重复请求色值不变: {again} vs {colors}"

    # 5) 不同用户色值分布(哈希生效,非全部相同)
    assert len(set(colors.values())) > 1, f"多用户色值不全相同: {colors}"


def test_avatar_color_ws_presence(base_url, admin):
    """WS presence 带 avatarColor,且与 REST 一致。"""
    websockets = pytest.importorskip("websockets")
    me = admin.get("/api/auth/me").data["user"]
    pid = admin.post("/api/projects", {"name": "WS 取色项目"}).data["id"]
    did = admin.post(f"/api/projects/{pid}/docs", {"title": "WS 测试"}).data["id"]
    # 从 base_url 推导 WS 地址,勿硬编码端口(硬编码会在多实例环境悄悄连错服务)
    uri = base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/docs/{did}"

    async def ws_check():
        async with websockets.connect(uri, additional_headers={
                "Cookie": "td_sid=" + admin.sid}) as ws:
            return json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

    msg = asyncio.run(ws_check())
    users_in = msg.get("users", [])
    assert bool(users_in), f"presence 消息含 users: {msg}"
    assert bool(users_in[0].get("avatarColor")), f"presence 用户含 avatarColor: {users_in}"
    assert users_in[0]["avatarColor"] == me["avatarColor"], \
        f"presence avatarColor 与 REST 一致: {users_in[0]} vs {me}"
