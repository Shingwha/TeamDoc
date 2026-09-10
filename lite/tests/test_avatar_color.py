"""验证头像取色统一(同一用户在所有接口返回同一 avatarColor)。

用法:先启动服务(隔离数据目录 + 非常用端口),再运行本脚本。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8123"
SID = None
FAIL = []


def call(method, path, body=None):
    global SID
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if SID:
        req.add_header("Cookie", "td_sid=" + SID)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            sc = r.headers.get("Set-Cookie")
            if sc and "td_sid=" in sc:
                SID = sc.split("td_sid=")[1].split(";")[0]
            b = r.read()
            return r.status, (json.loads(b.decode()) if b else None)
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, b[:200]


def check(name, cond, detail=""):
    print(("  OK  " if cond else "  FAIL") + "  " + name + ("" if cond else f"   <- {detail}"))
    if not cond:
        FAIL.append(name)


print("=== 准备 ===")
st, me = call("POST", "/api/auth/login",
              {"email": "admin@teamdoc.local", "password": "admin12345"})
check("管理员登录", st == 200, f"{st} {me}")
admin_uid = me["user"]["id"]
admin_color = me["user"]["avatarColor"]
check("登录响应含 avatarColor", bool(admin_color), str(me["user"]))

st, proj = call("POST", "/api/projects", {"name": "取色测试项目"})
pid = proj["id"]

# 建第二个用户并加入项目
call("POST", "/api/users", {"email": "c1@teamdoc.local", "name": "甲", "password": "color12345"})
call("POST", "/api/users", {"email": "c2@teamdoc.local", "name": "乙", "password": "color12345"})
call("POST", f"/api/projects/{pid}/members", {"email": "c1@teamdoc.local", "role": "EDITOR"})
call("POST", f"/api/projects/{pid}/members", {"email": "c2@teamdoc.local", "role": "EDITOR"})

print("\n=== 1) 项目成员列表返回 avatarColor ===")
st, members = call("GET", f"/api/projects/{pid}/members")
check("成员列表 200", st == 200, str(members))
colors = {}
for m in members:
    u = m["user"]
    colors[u["id"]] = u.get("avatarColor")
    check(f'  成员 {u["name"]} 含 avatarColor', bool(u.get("avatarColor")), str(u))

print("\n=== 2) 管理员用户列表返回同一 avatarColor ===")
st, users = call("GET", "/api/users")
by_id = {u["id"]: u for u in users}
for uid, c in colors.items():
    got = by_id.get(uid, {}).get("avatarColor")
    check(f"  用户 {by_id.get(uid, {}).get('name')} 的色值一致", got == c, f"成员列表={c} 用户列表={got}")

print("\n=== 3) /api/auth/me 与成员列表一致 ===")
st, me2 = call("GET", "/api/auth/me")
check("me.avatarColor == 成员列表中的自己",
      me2["user"]["avatarColor"] == colors.get(admin_uid),
      f'{me2["user"]["avatarColor"]} vs {colors.get(admin_uid)}')

print("\n=== 4) 同一用户多次请求色值稳定 ===")
st, m_again = call("GET", f"/api/projects/{pid}/members")
again = {m["user"]["id"]: m["user"]["avatarColor"] for m in m_again}
check("重复请求色值不变", again == colors, f"{again} vs {colors}")

print("\n=== 5) 不同用户色值分布(非全部相同) ===")
check("多用户色值不全相同(哈希生效)", len(set(colors.values())) > 1, str(colors))

print("\n=== 6) WS presence 带 avatarColor(源码级确认,连不上 WS 则跳过) ===")
try:
    import asyncio

    import websockets  # type: ignore

    async def ws_check():
        uri = "ws://127.0.0.1:8123/ws/docs/%s"
        st, doc = call("POST", f"/api/projects/{pid}/docs", {"title": "WS 测试"})
        did = doc["id"]
        async with websockets.connect(uri % did,
                                      additional_headers={"Cookie": "td_sid=" + SID}) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            return msg

    msg = asyncio.run(ws_check())
    users_in = msg.get("users", [])
    check("presence 消息含 users", bool(users_in), str(msg))
    if users_in:
        check("presence 用户含 avatarColor", bool(users_in[0].get("avatarColor")), str(users_in))
        check("presence avatarColor 与 REST 一致",
              users_in[0]["avatarColor"] == colors.get(users_in[0]["userId"]),
              f'{users_in[0]} vs {colors}')
except ImportError:
    print("  ·  websockets 库不可用,跳过 WS 环节")
except Exception as e:
    check("WS presence 检查", False, repr(e))

print("\n=== 清理 ===")
st, r = call("DELETE", f"/api/projects/{pid}")
check("删测试项目", st == 200, f"{st} {r}")

print("\n" + "=" * 52)
if FAIL:
    print(f"!! {len(FAIL)} 项失败:")
    for f in FAIL:
        print("   - " + f)
    sys.exit(1)
print("全部通过")
