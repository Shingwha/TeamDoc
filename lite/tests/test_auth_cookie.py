"""会话与凭据边界的回归测试。

盯的是两类"看起来像登录坏了"的故障,它们都不是认证逻辑本身错了,而是**失败的形状**
不对 —— 用户看到的现象是"所有功能都在报错",真正的病因却藏在这些边界里:

1. 会话失效后浏览器仍重放那条死 Cookie:401 必须顺手清掉它,否则请求一个接一个被拒,
   页面刷出一屏 401 而不会自我恢复。
2. 链路上多出一个 Authorization 头:不能因此把合法的 Cookie 会话判成未登录。
   本应用的令牌一律 `tdp_` 前缀,前缀本身就是判据;把"任意 Bearer"当成"PAT 认证失败"
   会让反代/浏览器扩展/同源其它客户端把所有人挡在门外。
3. 非法 id(如整数时代残留的 `project_id=10004`)必须得到一个明确的 400,而不是
   因为依赖顺序被 401 盖住 —— 401 只意味着"凭据有问题",不能让别的错误伪装成它。
"""
import urllib.error
import urllib.request


def _raw_get(base_url, path, *, cookie=None, bearer=None):
    """裸请求:harness 的 Client 只会在 Cookie 与 Bearer 之间二选一,这里要的是两者同时带。"""
    req = urllib.request.Request(base_url + path)
    if cookie:
        req.add_header("Cookie", "td_sid=" + cookie)
    if bearer:
        req.add_header("Authorization", "Bearer " + bearer)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}


def test_401_clears_the_session_cookie(base_url):
    """失效会话的 401 必须带清 Cookie 头(服务端在 main.py 的 401 处理里统一加)。"""
    status, headers = _raw_get(base_url, "/api/auth/me", cookie="deadbeef" * 8)
    assert status == 401, f"死会话应为 401: {status}"
    sc = headers.get("set-cookie") or ""
    assert "td_sid=" in sc and "Max-Age=0" in sc, f"401 应带清 Cookie 头: {sc!r}"


def test_unknown_bearer_does_not_kill_cookie_auth(base_url, admin):
    """不是我们的 Bearer(非 tdp_ 前缀)不应让合法的 Cookie 会话失效。"""
    status, _ = _raw_get(base_url, "/api/auth/me", cookie=admin.sid,
                         bearer="some-proxy-token")
    assert status == 200, f"合法会话 + 陌生 Bearer 应照常认证: {status}"


def test_invalid_id_is_rejected_by_shape(base_url, admin):
    """id 形状不对 → 400(带会话时不会被 401 盖住),且不影响正常 id。"""
    assert admin.get("/api/files?project_id=10004").status == 400, "整数时代的旧 id → 400"
    assert admin.get("/api/projects/10004/storage").status == 400, "路径里的旧 id → 400"
    pid = admin.post("/api/projects", {"name": "id 形状", "description": "d"}).data["id"]
    assert admin.get(f"/api/files?project_id={pid}").status == 200, "合法 ULID 照常 200"
