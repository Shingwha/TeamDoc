"""登录节流:账号维度与来源维度的拦截、退避、清零与持久性。

为什么自带实例而不是用会话级共享实例:节流阈值决定了整套测试的登录节奏,而共享实例上
所有客户端都来自 127.0.0.1 —— 在那里把来源桶打满,后续测试会集体收到 429,
看起来像服务端 bug。所以这里按维度各起一台,并把另一维度关掉,断言才是确定性的:

- 账号维度实例:LOGIN_IP_MAX_FAILS=0(关掉来源维度)
- 来源维度实例:LOGIN_MAX_FAILS=999(账号维度实际不触发)

另外,"不存在的邮箱也一样锁定"这条是刻意设计的:否则攻击者能靠"锁没锁"反推账号存在性。
"""
import time

import pytest

from _harness import Client, Server, make_user, rand_email


@pytest.fixture(scope="module")
def acct_server():
    """账号维度:3 次失败即 60 秒冷却,重复触发退避到 120 秒封顶。"""
    s = Server(extra_env={"LOGIN_MAX_FAILS": "3", "LOGIN_FAIL_WINDOW": "900",
                          "LOGIN_LOCKOUT": "60", "LOGIN_LOCKOUT_MAX": "120",
                          "LOGIN_IP_MAX_FAILS": "0"})
    s.start()
    yield s
    s.stop()
    s.cleanup()


@pytest.fixture(scope="module")
def ip_server():
    """来源维度:4 次失败即 2 秒冷却;账号维度放开(999 次)。"""
    s = Server(extra_env={"LOGIN_IP_MAX_FAILS": "4", "LOGIN_IP_LOCKOUT": "2",
                          "LOGIN_FAIL_WINDOW": "900", "LOGIN_MAX_FAILS": "999"})
    s.start()
    yield s
    s.stop()
    s.cleanup()


def test_account_lockout_and_unlock(acct_server):
    """失败到上限 → 429 + Retry-After;锁定期内正确密码也进不来;管理员可解锁。"""
    admin = acct_server.admin_client()
    email, u = make_user(admin, "节流甲")
    c = Client(acct_server.base_url)
    c.login(email, "pw12345678")

    r1 = c.post("/api/auth/login", {"email": email, "password": "bad"})
    r2 = c.post("/api/auth/login", {"email": email, "password": "bad"})
    assert [r1.status, r2.status] == [401, 401]
    # 快被锁时给出剩余次数提示(上限 3 → 失败后分别剩 2、1 次)
    assert "还可尝试 2 次" in r1.data["detail"]["message"]
    assert "还可尝试 1 次" in r2.data["detail"]["message"]

    r3 = c.post("/api/auth/login", {"email": email, "password": "bad"})
    assert r3.status == 429, r3.data
    assert r3.data["detail"]["code"] == "TOO_MANY_ATTEMPTS"
    assert int(r3.headers["retry-after"]) >= 1

    # 锁定先于校验:冷却期内即使密码正确也拒绝(否则"锁定期"形同虚设)
    assert c.post("/api/auth/login", {"email": email, "password": "pw12345678"}).status == 429

    row = [x for x in admin.get("/api/users").data if x["email"] == email][0]
    assert row["lockedUntil"], row
    assert admin.patch(f"/api/users/{u['id']}", {"unlock": True}).data["lockedUntil"] is None
    assert c.post("/api/auth/login", {"email": email, "password": "pw12345678"}).status == 200


def test_unknown_email_locks_identically(acct_server):
    """不存在的邮箱与存在的账号表现一致(状态码/错误码/是否锁定)—— 不泄露账号存在性。"""
    c = Client(acct_server.base_url)
    ghost = rand_email("ghost")
    codes = [c.post("/api/auth/login", {"email": ghost, "password": "bad"}).status
             for _ in range(3)]
    assert codes == [401, 401, 429]
    r = c.post("/api/auth/login", {"email": ghost, "password": "bad"})
    assert r.status == 429 and r.data["detail"]["code"] == "TOO_MANY_ATTEMPTS"


def test_success_clears_account_counter(acct_server):
    """成功即清零:失败 2 次 → 登录成功 → 再失败 2 次仍是 401(没有被累加成冷却)。"""
    admin = acct_server.admin_client()
    email, _ = make_user(admin, "节流乙")
    c = Client(acct_server.base_url)
    for _ in range(2):
        assert c.post("/api/auth/login", {"email": email, "password": "bad"}).status == 401
    c.login(email, "pw12345678")
    for _ in range(2):
        assert c.post("/api/auth/login", {"email": email, "password": "bad"}).status == 401


def test_change_password_shares_account_throttle(acct_server):
    """自助改密走同一咽喉:旧密码连错到上限一样被冷却(会话被盗后猜旧密码也被挡)。"""
    admin = acct_server.admin_client()
    email, _ = make_user(admin, "节流丁")
    c = Client(acct_server.base_url)
    c.login(email, "pw12345678")
    codes = [c.post("/api/users/me/password",
                    {"oldPassword": "bad", "newPassword": "newpw12345"}).status
             for _ in range(3)]
    assert codes == [403, 403, 429]
    # 账号维度是共用的:改密路径上的失败同样会把登录一起锁住
    assert c.post("/api/auth/login", {"email": email, "password": "pw12345678"}).status == 429


def test_lockout_survives_restart(acct_server):
    """落库而非进程内计数:重启后仍然锁着 —— 否则"重启即重置"就是一条绕过路径。"""
    admin = acct_server.admin_client()
    email, _ = make_user(admin, "节流丙")
    c = Client(acct_server.base_url)
    for _ in range(3):
        c.post("/api/auth/login", {"email": email, "password": "bad"})
    assert c.post("/api/auth/login", {"email": email, "password": "pw12345678"}).status == 429
    acct_server.restart()
    assert c.post("/api/auth/login", {"email": email, "password": "pw12345678"}).status == 429


def test_source_ip_spraying_is_throttled(ip_server):
    """一个来源轮着试多个账号(密码喷洒):换全新账号也被拦;成功登录不清来源桶;
    节流只作用于凭据端点,已登录用户的正常读写不受影响。"""
    admin = ip_server.admin_client()
    c = Client(ip_server.base_url)
    for i in range(3):
        email, _ = make_user(admin, f"喷洒{i}")
        assert c.post("/api/auth/login", {"email": email, "password": "bad"}).status == 401
    # 成功登录**不**清来源计数(否则攻击者只要有一个有效账号就能不断重置这个桶)
    ok_email, _ = make_user(admin, "喷洒-真")
    assert c.post("/api/auth/login", {"email": ok_email, "password": "pw12345678"}).status == 200
    # 于是第 4 次失败即触发来源冷却(若被成功登录清零,这里仍会是 401)
    r = c.post("/api/auth/login", {"email": rand_email("spray"), "password": "bad"})
    assert r.status == 429, r.data
    # 换一个全新账号照样被拦(喷洒被打断)—— 而它自己的账号维度并没有失败记录
    fresh, _ = make_user(admin, "喷洒-新")
    assert c.post("/api/auth/login", {"email": fresh, "password": "pw12345678"}).status == 429
    # 绝不是"全站封 IP":已登录会话照常读写
    assert admin.get("/api/projects").status == 200
    # 冷却按时间自行过期
    time.sleep(2.3)
    assert c.post("/api/auth/login", {"email": fresh, "password": "pw12345678"}).status == 200
