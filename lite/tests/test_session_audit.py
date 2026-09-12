"""管理后台的登录状态与审计:用户列表字段、登录详情抽屉、强制下线、解锁、登录动态。

走会话级共享实例(默认阈值):这里的断言都是"看得到什么",与阈值无关 ——
节流语义(锁定/退避/持久性)在 test_login_throttle.py 的专用实例里测。
"""
import json

from _harness import Client, make_user, rand_email


def _row(admin, email):
    return [x for x in admin.get("/api/users").data if x["email"] == email][0]


def test_users_row_exposes_login_status(admin, base_url):
    """用户表带的登录状态:最近登录时间/IP、在线、活跃会话数、锁定截止。"""
    email, _ = make_user(admin, "审计甲")
    c = Client(base_url)
    c.login(email, "pw12345678")
    row = _row(admin, email)
    assert row["lastLoginAt"] and row["lastLoginIp"] == "127.0.0.1"
    assert row["online"] is True and row["sessionCount"] == 1
    assert row["lockedUntil"] is None


def test_access_drawer_shows_sessions_and_events(admin, base_url):
    """登录详情:会话列表(设备/IP/时间)+ 登录记录;真实 token 绝不出现在响应里。"""
    email, u = make_user(admin, "审计乙")
    c = Client(base_url)
    c.login(email, "pw12345678")
    d = admin.get(f"/api/admin/users/{u['id']}/access").data
    assert d["user"]["email"] == email
    s = d["sessions"][0]
    assert s["ip"] == "127.0.0.1"
    assert s["device"] and s["deviceKind"] in ("desktop", "mobile")
    # ref 是 sha256(token):能指认会话,但不能反推成凭据
    assert len(s["ref"]) == 64 and s["ref"] != c.sid
    assert c.sid not in json.dumps(d, ensure_ascii=False)
    assert d["events"][0]["result"] == "ok"
    assert d["maxFails"] >= 1


def test_failed_login_is_audited_and_unlock_works(admin, base_url):
    """失败落审计、触发锁定后列表可见、解锁后立即恢复登录。"""
    email, u = make_user(admin, "审计丙")
    c = Client(base_url)
    for _ in range(3):
        assert c.post("/api/auth/login", {"email": email, "password": "bad"}).status == 401
    d = admin.get(f"/api/admin/users/{u['id']}/access").data
    assert d["events"][0]["result"] == "bad_password"
    assert d["lock"]["failCount"] == 3
    assert d["lock"]["lockedUntil"] is None       # 默认上限 5 次,3 次还没锁
    assert _row(admin, email)["lockedUntil"] is None

    r4 = c.post("/api/auth/login", {"email": email, "password": "bad"})
    assert r4.status == 401                       # 第 4 次仍给机会(提示只剩 1 次)
    assert "还可尝试 1 次" in r4.data["detail"]["message"]
    assert c.post("/api/auth/login", {"email": email, "password": "bad"}).status == 429
    assert _row(admin, email)["lockedUntil"]
    assert admin.get(f"/api/admin/users/{u['id']}/access").data["events"][0]["result"] == "locked"

    assert admin.patch(f"/api/users/{u['id']}", {"unlock": True}).data["lockedUntil"] is None
    assert _row(admin, email)["lockedUntil"] is None
    assert c.post("/api/auth/login", {"email": email, "password": "pw12345678"}).status == 200


def test_disabled_account_login_is_audited(admin, base_url):
    """口令正确但账号被禁用:结果记为 disabled —— "离职后仍有人尝试登录"要看得见。"""
    email, u = make_user(admin, "审计己")
    assert admin.patch(f"/api/users/{u['id']}", {"isDisabled": True}).status == 200
    r = Client(base_url).post("/api/auth/login", {"email": email, "password": "pw12345678"})
    assert r.status == 401 and "禁用" in r.data["detail"]["message"]
    assert admin.get(f"/api/admin/users/{u['id']}/access").data["events"][0]["result"] == "disabled"


def test_revoke_session_by_ref(admin, base_url):
    """按 ref 强制下线:那条 Cookie 立刻 401,同一用户的其它会话不受影响;可全部下线。"""
    email, u = make_user(admin, "审计丁")
    a, b = Client(base_url), Client(base_url)
    a.login(email, "pw12345678")
    b.login(email, "pw12345678")
    sessions = admin.get(f"/api/admin/users/{u['id']}/access").data["sessions"]
    assert len(sessions) == 2
    ref = sessions[0]["ref"]
    assert admin.delete(f"/api/admin/sessions/{ref}").data["revoked"] == 1
    assert admin.delete(f"/api/admin/sessions/{ref}").status == 404   # 已失效
    # 恰好一条死了、一条还活着(不假设列表顺序与客户端的对应关系)
    assert sorted([a.get("/api/auth/me").status, b.get("/api/auth/me").status]) == [200, 401]
    assert admin.delete(f"/api/admin/users/{u['id']}/sessions").data["revoked"] == 1
    assert a.get("/api/auth/me").status == 401
    assert b.get("/api/auth/me").status == 401


def test_login_events_feed_and_permissions(admin, base_url):
    """全站登录动态:不存在的账号也留痕(撞库痕迹)、不带口令等秘密、仅管理员可见。"""
    ghost = rand_email("ghost")
    secret = "Sup3rSecretPw!"
    Client(base_url).post("/api/auth/login", {"email": ghost, "password": secret})
    feed = admin.get("/api/admin/login-events?limit=200").data
    assert any(e["email"] == ghost and e["result"] == "bad_password" for e in feed)
    ghost_rows = [e for e in feed if e["email"] == ghost]
    assert ghost_rows and ghost_rows[0]["userName"] is None   # 账号不存在:字段在,值为空
    assert any(e["userName"] for e in feed)                   # 已知账号带上姓名
    body = json.dumps(feed, ensure_ascii=False)
    assert secret not in body
    assert not any("pass" in k.lower() for k in feed[0].keys())

    anon = Client(base_url)
    assert anon.get("/api/admin/login-events").status == 401
    pe, _ = make_user(admin, "审计戊")
    pc = Client(base_url)
    pc.login(pe, "pw12345678")
    assert pc.get("/api/admin/login-events").status == 403
    assert pc.get(f"/api/admin/users/{admin.get('/api/auth/me').data['user']['id']}/access").status == 403
    assert pc.delete("/api/admin/sessions/" + "0" * 64).status == 403
    assert pc.delete("/api/admin/users/10000/sessions").status == 403

    # 同事目录(全员可见)不含来源与时间 —— IP 只在管理端暴露
    assert set(admin.get("/api/users/directory").data[0].keys()) == {"id", "name", "email", "avatarColor"}
