"""服务端抗压回归:卡住的长传输 / 大量长连接不应拖垮其他请求。

背景:线上出现"server 跑一段时间后前端完全无响应、刷新也连不上、控制台没有任何
报错"。根因之一是 SQLite 连接被长事务掏空 —— 下载/打包/上传在**整个传输期间**
持有数据库连接(FastAPI 的 yield 依赖要等响应体发完才清理),默认队列池 15 条被
卡住的传输占满后,所有请求(含 WS 握手,它同步跑在事件循环上)一起排队 30 秒后
超时。修复:传输前提前归还连接(models.release_db)+ 连接池换 NullPool + WS 的
数据库操作挪进 worker 线程。

本脚本制造"卡住的长传输"与"大量长连接",断言普通请求仍然毫秒级可用。
修复前:场景1/2 会等到池超时(30s)或直接失败;场景3 对应 WS 占满池的历史修复。

用法(隔离实例,勿对真实数据目录运行):
    cd lite/server
    TEAMDOC_DATA_DIR=/tmp/td_resilience PORT=8123 .venv/Scripts/python.exe main.py
    TD_BASE=http://127.0.0.1:8123 python tests/test_server_resilience.py
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
FAIL, PASS = [], []
SID = None
FILE_MB = 16                       # 下载场景的文件必须大于内核发送缓冲,否则服务端不会被卡住
PROBE_TIMEOUT = 6.0                # 探针请求超时:修复后应远小于它
PROBE_LIMIT = 3.0                  # 断言阈值(留足慢机器余量)


def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}" + (f"   <- {extra}" if extra and not cond else ""))


def call(method, path, body=None, raw_body=None, ctype="application/json"):
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None and ctype:
        req.add_header("Content-Type", ctype)
    if SID:
        req.add_header("Cookie", "td_sid=" + SID)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            try:
                return r.status, json.loads(r.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return r.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return e.code, None


def fetch_bytes(path):
    """完整读一个二进制响应,返回 (状态码, 字节数) —— 用于断言传输内容完整。"""
    req = urllib.request.Request(BASE + path)
    if SID:
        req.add_header("Cookie", "td_sid=" + SID)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, len(e.read())


def probe(path="/api/auth/status"):
    """带 Cookie 发一个普通 GET,返回 (状态码|None, 秒)。超时返回 None —— 修复前
    池被占满时这里会等到 30 秒池超时,正是本次故障的形态。"""
    req = urllib.request.Request(BASE + path)
    if SID:
        req.add_header("Cookie", "td_sid=" + SID)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            r.read()
            return r.status, time.monotonic() - t0
    except Exception:
        return None, time.monotonic() - t0


def _split_base():
    p = urllib.parse.urlsplit(BASE)
    return (p.hostname or "127.0.0.1"), (p.port or 80)


def stalled_upload(pid: int, name: str, declared=1 * 1024 * 1024, send=4 * 1024):
    """裸 socket 发一个 raw body 上传:声明 1MB 却只发 4KB,然后**保持连接不关**。

    服务端此时正卡在 request.stream() 里等剩余字节 —— 修复前它会一直握着一条
    数据库连接(预检已查过库),直到客户端消失。返回 socket 供调用方最后关闭。
    """
    host, port = _split_base()
    path = ("/api/files/upload?projectId=" + urllib.parse.quote(str(pid))
            + "&name=" + urllib.parse.quote(name))
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Cookie: td_sid={SID}\r\n"
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


def held_download(fid: int):
    """裸 socket 请求下载:只读响应头,**不读 body**,并调小接收缓冲。

    服务端写满内核缓冲后会阻塞在响应的 send 上 —— 修复前这条请求从头到尾占着
    一条数据库连接(响应体发完才 close)。返回 socket 供调用方最后关闭。
    """
    host, port = _split_base()
    head = (
        f"GET /api/files/{fid}/download HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Cookie: td_sid={SID}\r\n"
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


def login():
    global SID
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"email": "admin@teamdoc.local",
                                                  "password": "admin12345"}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            sc = r.headers.get("Set-Cookie", "")
    except Exception:
        return False
    SID = sc.split("td_sid=")[1].split(";")[0] if "td_sid=" in sc else None
    return bool(SID)


def main():
    print("=" * 60)
    print("服务端抗压:长传输 / 长连接不应拖垮其他请求")
    print("=" * 60)
    if not login():
        print("登录失败,请先跑 tests/_bootstrap.py")
        return 1

    # 基线:没有任何负载时普通请求的耗时
    st, base_secs = probe()
    check(f"基线 /api/auth/status 正常({base_secs * 1000:.0f}ms)", st == 200 and base_secs < PROBE_LIMIT)

    st, proj = call("POST", "/api/projects", {"name": "抗压测试项目", "description": "r"})
    if st != 200:
        print("建项目失败:", st, proj)
        return 1
    pid = proj["id"]
    st, doc = call("POST", f"/api/projects/{pid}/docs", {"title": "抗压文档"})
    did = doc["id"]

    print(f"\n=== 准备:{FILE_MB}MB 文件(用于下载场景) ===")
    payload = b"z" * (FILE_MB * 1024 * 1024)
    qs = "?projectId=" + urllib.parse.quote(str(pid)) + "&name=big.bin"
    st, f = call("POST", "/api/files/upload" + qs, raw_body=payload,
                 ctype="application/octet-stream")
    check(f"上传 {FILE_MB}MB 文件成功", st == 200 and f and f.get("size") == len(payload),
          f"{st} {f}")
    fid = (f or {}).get("id")

    # ---------- 场景1:一批"只发头不发 body"的上传卡住 ----------
    print("\n=== 场景1:20 个卡住的上传(修复前每条占一条池连接) ===")
    socks = [s for s in (stalled_upload(pid, f"stall-{i}.bin") for i in range(20)) if s]
    check("已挂起 20 个上传(声明 1MB,只发 4KB 后不关连接)", len(socks) == 20, f"实际 {len(socks)}")
    time.sleep(0.5)
    for path, label in (("/api/auth/status", "auth/status(查库)"),
                        ("/api/projects", "项目列表(需登录)"),
                        (f"/api/docs/{did}", "读文档")):
        st, secs = probe(path)
        check(f"挂起期间 {label} 仍可用({secs * 1000:.0f}ms)",
              st == 200 and secs < PROBE_LIMIT, f"status={st} 耗时 {secs:.1f}s")
    if fid:
        st, secs = probe(f"/api/files/{fid}/download")
        check(f"挂起期间仍能发起下载({secs:.2f}s 内收到响应头)", st == 200, f"status={st}")
    # 再完整下载一次,确认传输本身没被影响
    if fid:
        st, n = fetch_bytes(f"/api/files/{fid}/download")
        check(f"挂起期间 {FILE_MB}MB 下载内容完整", st == 200 and n == len(payload), f"{st} {n}B")
    for s in socks:
        try:
            s.close()
        except OSError:
            pass
    time.sleep(1.0)

    # ---------- 场景2:一批"只读响应头"的下载卡住 ----------
    print("\n=== 场景2:20 个卡住的下载(响应体发完前不放连接) ===")
    if fid:
        dl = [s for s in (held_download(fid) for _ in range(20)) if s]
        check("已挂起 20 个下载(不读 body)", len(dl) == 20, f"实际 {len(dl)}")
        time.sleep(1.5)  # 等服务端把内核缓冲写满、真正阻塞在发送上
        for path, label in (("/api/auth/status", "auth/status(查库)"),
                            ("/api/projects", "项目列表"),
                            (f"/api/docs/{did}", "读文档")):
            st, secs = probe(path)
            check(f"挂起期间 {label} 仍可用({secs * 1000:.0f}ms)",
                  st == 200 and secs < PROBE_LIMIT, f"status={st} 耗时 {secs:.1f}s")
        for s in dl:
            try:
                s.close()
            except OSError:
                pass
        time.sleep(1.0)

    # ---------- 场景3:20 条 WS 长连接(WS 占满池的历史修复回归) ----------
    print("\n=== 场景3:20 条 WS 长连接同时在线 ===")
    try:
        from websockets.sync.client import connect as ws_connect  # type: ignore
    except ImportError:
        print("  SKIP  websockets 库不可用,跳过 WS 场景")
        ws_connect = None
    if ws_connect:
        uri = BASE.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/docs/{did}"
        conns = []
        try:
            for _ in range(20):
                # legacy=True:直接拿到连接对象(不用 with 块),这样才能一次性持有一批
                conns.append(ws_connect(uri, additional_headers={"Cookie": "td_sid=" + SID},
                                        open_timeout=10, max_queue=None, legacy=True))
        except Exception as exc:
            check("建立 20 条 WS 连接", False, repr(exc))
        check("已建立 20 条 WS 连接", len(conns) == 20, f"实际 {len(conns)}")
        st, secs = probe("/api/auth/status")
        check(f"20 条 WS 在线时 auth/status 仍可用({secs * 1000:.0f}ms)",
              st == 200 and secs < PROBE_LIMIT, f"status={st} 耗时 {secs:.1f}s")
        st, secs = probe("/api/projects")
        check(f"20 条 WS 在线时项目列表仍可用({secs * 1000:.0f}ms)",
              st == 200 and secs < PROBE_LIMIT, f"status={st} 耗时 {secs:.1f}s")
        if conns:
            # WS 的数据库操作挪进 worker 线程后,保存链路必须照常工作
            conns[0].send(json.dumps({"type": "content", "content": "# 抗压测试\n\n正文\n"}))
            saved = None
            deadline = time.time() + 10
            while time.time() < deadline and saved is None:
                msg = json.loads(conns[0].recv(timeout=10))
                if msg.get("type") == "saved":
                    saved = msg
            check("WS 内容保存返回 saved(数据库改动未破坏)", saved is not None, str(saved))
        for c in conns:
            try:
                c.close()
            except Exception:
                pass

    print("\n=== 清理 ===")
    st, _ = call("DELETE", f"/api/projects/{pid}")
    check("测试项目已删除", st == 200, str(st))

    print("\n" + "=" * 60)
    if FAIL:
        print(f"失败 {len(FAIL)} 项:")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"全部通过({len(PASS)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
