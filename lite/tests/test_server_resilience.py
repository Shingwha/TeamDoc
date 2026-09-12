"""服务端抗压回归:卡住的长传输 / 大量长连接不应拖垮其他请求。

背景:线上出现"server 跑一段时间后前端完全无响应、刷新也连不上、控制台没有任何
报错"。根因之一是 SQLite 连接被长事务掏空 —— 下载/打包/上传在**整个传输期间**
持有数据库连接(FastAPI 的 yield 依赖要等响应体发完才清理),默认队列池 15 条被
卡住的传输占满后,所有请求(含 WS 握手,它同步跑在事件循环上)一起排队 30 秒后
超时。修复:传输前提前归还连接(models.release_db)+ 连接池换 NullPool + WS 的
数据库操作挪进 worker 线程。

本文件制造"卡住的长传输"与"大量长连接",断言普通请求仍然毫秒级可用。
修复前:场景1/2 会等到池超时(30s)或直接失败;场景3 对应 WS 占满池的历史修复。

标记 slow:分钟级耗时,默认跳过(pytest.ini),`-m slow` 单跑。
"""
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from _harness import Client

pytestmark = pytest.mark.slow

FILE_MB = 16                       # 下载场景的文件必须大于内核发送缓冲,否则服务端不会被卡住
PROBE_TIMEOUT = 6.0                # 探针请求超时:修复后应远小于它
PROBE_LIMIT = 3.0                  # 断言阈值(留足慢机器余量)


def _split_base(base_url):
    p = urllib.parse.urlsplit(base_url)
    return (p.hostname or "127.0.0.1"), (p.port or 80)


def probe(base_url, sid, path="/api/auth/status"):
    """带 Cookie 发一个普通 GET,返回 (状态码|None, 秒)。超时返回 None —— 修复前
    池被占满时这里会等到 30 秒池超时,正是本次故障的形态。"""
    req = urllib.request.Request(base_url + path)
    req.add_header("Cookie", "td_sid=" + (sid or ""))
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            r.read()
            return r.status, time.monotonic() - t0
    except Exception:
        return None, time.monotonic() - t0


def fetch_bytes(base_url, sid, path):
    """完整读一个二进制响应,返回 (状态码, 字节数) —— 用于断言传输内容完整。"""
    req = urllib.request.Request(base_url + path)
    req.add_header("Cookie", "td_sid=" + (sid or ""))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, len(e.read())


def stalled_upload(base_url, sid, pid, name, declared=1 * 1024 * 1024, send=4 * 1024):
    """裸 socket 发一个 raw body 上传:声明 1MB 却只发 4KB,然后**保持连接不关**。

    服务端此时正卡在 request.stream() 里等剩余字节 —— 修复前它会一直握着一条
    数据库连接(预检已查过库),直到客户端消失。返回 socket 供调用方最后关闭。
    """
    host, port = _split_base(base_url)
    path = (f"/api/files/upload?projectId={urllib.parse.quote(str(pid))}"
            f"&name={urllib.parse.quote(name)}")
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Cookie: td_sid={sid}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {declared}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        s = socket.create_connection((host, port), timeout=15)
        s.sendall(head + b"x" * send)
        return s
    except OSError:
        return None


def held_download(base_url, sid, fid):
    """裸 socket 请求下载:只读响应头,**不读 body**,并调小接收缓冲。

    服务端写满内核缓冲后会阻塞在响应的 send 上 —— 修复前这条请求从头到尾占着
    一条数据库连接(响应体发完才 close)。返回 socket 供调用方最后关闭。
    """
    host, port = _split_base(base_url)
    head = (
        f"GET /api/files/{fid}/download HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Cookie: td_sid={sid}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        s = socket.create_connection((host, port), timeout=15)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
        s.sendall(head)
        s.settimeout(15)
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        if b" 200 " not in buf.split(b"\r\n", 1)[0]:
            s.close()
            return None
        return s
    except OSError:
        return None


def _close_all(socks):
    for s in socks:
        try:
            s.close()
        except OSError:
            pass


def test_long_transfers_and_ws_do_not_starve_server(base_url, admin):
    sid = admin.sid
    st, base_secs = probe(base_url, sid)
    assert st == 200 and base_secs < PROBE_LIMIT, \
        f"基线 /api/auth/status 正常({base_secs * 1000:.0f}ms)"

    pid = admin.post("/api/projects", {"name": "抗压测试项目", "description": "r"}).data["id"]
    did = admin.post(f"/api/projects/{pid}/docs", {"title": "抗压文档"}).data["id"]

    payload = b"z" * (FILE_MB * 1024 * 1024)
    r = admin.upload(pid, "big.bin", payload)
    assert r.status == 200 and r.data and r.data.get("size") == len(payload), \
        f"上传 {FILE_MB}MB 文件成功: {r.status} {r.data}"
    fid = r.data["id"]

    # ---------- 场景1:一批"只发头不发 body"的上传卡住 ----------
    socks = [s for s in (stalled_upload(base_url, sid, pid, f"stall-{i}.bin")
                         for i in range(20)) if s]
    assert len(socks) == 20, \
        f"已挂起 20 个上传(声明 1MB,只发 4KB 后不关连接): 实际 {len(socks)}"
    time.sleep(0.5)
    for path, label in (("/api/auth/status", "auth/status(查库)"),
                        ("/api/projects", "项目列表(需登录)"),
                        (f"/api/docs/{did}", "读文档")):
        st, secs = probe(base_url, sid, path)
        assert st == 200 and secs < PROBE_LIMIT, \
            f"挂起期间 {label} 仍可用({secs * 1000:.0f}ms): status={st} 耗时 {secs:.1f}s"
    st, _ = probe(base_url, sid, f"/api/files/{fid}/download")
    assert st == 200, f"挂起期间仍能发起下载(收到响应头): status={st}"
    # 再完整下载一次,确认传输本身没被影响
    st, n = fetch_bytes(base_url, sid, f"/api/files/{fid}/download")
    assert st == 200 and n == len(payload), \
        f"挂起期间 {FILE_MB}MB 下载内容完整: {st} {n}B"
    _close_all(socks)
    time.sleep(1.0)

    # ---------- 场景2:一批"只读响应头"的下载卡住 ----------
    dl = [s for s in (held_download(base_url, sid, fid) for _ in range(20)) if s]
    assert len(dl) == 20, f"已挂起 20 个下载(不读 body): 实际 {len(dl)}"
    time.sleep(1.5)  # 等服务端把内核缓冲写满、真正阻塞在发送上
    for path, label in (("/api/auth/status", "auth/status(查库)"),
                        ("/api/projects", "项目列表"),
                        (f"/api/docs/{did}", "读文档")):
        st, secs = probe(base_url, sid, path)
        assert st == 200 and secs < PROBE_LIMIT, \
            f"挂起期间 {label} 仍可用({secs * 1000:.0f}ms): status={st} 耗时 {secs:.1f}s"
    _close_all(dl)
    time.sleep(1.0)

    # ---------- 场景3:20 条 WS 长连接(WS 占满池的历史修复回归) ----------
    ws_sync = pytest.importorskip("websockets.sync.client")
    uri = base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/docs/{did}"
    conns = []
    try:
        for _ in range(20):
            # 不用 with 块,直接拿连接对象,才能一次性持有一批;
            # legacy=True 是"直接连接"的官方写法(否则每条连接告一次警)
            conns.append(ws_sync.connect(uri, additional_headers={"Cookie": "td_sid=" + sid},
                                         open_timeout=10, max_queue=None, legacy=True))
    except Exception as exc:
        conns = conns or []
        raise AssertionError(f"建立 20 条 WS 连接失败: {exc!r}") from exc
    assert len(conns) == 20, f"已建立 20 条 WS 连接: 实际 {len(conns)}"
    st, secs = probe(base_url, sid)
    assert st == 200 and secs < PROBE_LIMIT, \
        f"20 条 WS 在线时 auth/status 仍可用({secs * 1000:.0f}ms): 耗时 {secs:.1f}s"
    st, secs = probe(base_url, sid, "/api/projects")
    assert st == 200 and secs < PROBE_LIMIT, \
        f"20 条 WS 在线时项目列表仍可用({secs * 1000:.0f}ms): 耗时 {secs:.1f}s"
    # WS 的数据库操作挪进 worker 线程后,保存链路必须照常工作
    conns[0].send(json.dumps({"type": "content", "content": "# 抗压测试\n\n正文\n"}))
    saved = None
    deadline = time.time() + 10
    while time.time() < deadline and saved is None:
        msg = json.loads(conns[0].recv(timeout=10))
        if msg.get("type") == "saved":
            saved = msg
    assert saved is not None, f"WS 内容保存返回 saved(数据库改动未破坏): {saved}"
    for c in conns:
        try:
            c.close()
        except Exception:
            pass

    # 负载卸掉后,写路径依然健康
    assert admin.delete(f"/api/projects/{pid}").status == 200, "负载后项目删除正常"
