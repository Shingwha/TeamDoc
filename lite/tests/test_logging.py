"""日志:事件循环只入队,写出在独立线程 —— 挂起的输出端不能拖住服务。

2026-09-12 事故:控制台窗口被鼠标选中(Windows 快速编辑)后 conhost 挂起所有输出,
而 uvicorn 的访问日志是在**事件循环线程**上同步写控制台的 —— 整个服务随之冻结:
进程活着、没有任何报错、所有客户端连不上(主线程栈停在 logging 的 stream.write)。
设计见 logsetup.py。

本文件守三条:
1. 写出端卡住(队列满)时,入队方一次都不阻塞 —— 纯单元断言,默认组就跑;
2. 输出端真被挂起时(用一条从不读取的管道复现)服务照常应答 —— 端到端;
3. 控制台外观没被"统一格式"改掉:uvicorn 的行仍是它自己的渲染(配色/紧凑格式)。
"""
import logging
import queue
import re
import sys
import time

import pytest

from _harness import SERVER_DIR, Server

sys.path.insert(0, str(SERVER_DIR))  # 直接用服务端模块断言(与 README 的运行方式一致)
import logsetup  # noqa: E402


def test_queue_full_never_blocks_caller():
    """写出端卡住(队列满)时记录被丢弃,调用线程一次都不阻塞。"""
    full = queue.Queue(maxsize=1)
    full.put_nowait(object())
    handler = logsetup._Handler(full)
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    t0 = time.monotonic()
    for _ in range(2000):
        handler.handle(record)
    assert time.monotonic() - t0 < 0.5


@pytest.fixture(scope="module")
def stalled_sink_server():
    """stdout 接一条从不读取的管道(与"控制台被选中"同类:输出端不消费)。"""
    s = Server(stdout_pipe=True)
    s.start()
    yield s
    s.stop()
    s.cleanup()


@pytest.mark.slow
def test_service_survives_stalled_log_sink(stalled_sink_server):
    c = stalled_sink_server.admin_client()
    long_path = "/" + "x" * 4000  # 单条访问日志足够大:几十条就能写满任何管道缓冲
    for _ in range(40):
        c.get(long_path)  # 404,但照样进访问日志

    t0 = time.monotonic()
    r = c.get("/api/auth/status", timeout=10)
    elapsed = time.monotonic() - t0
    assert r.status == 200, f"输出端挂起时服务失去响应: {r.status} {r.data}"
    assert elapsed < 3.0, f"响应被日志写出拖慢:{elapsed:.1f}s"


@pytest.mark.slow
def test_log_file_still_written(stalled_sink_server):
    """旁路归旁路,落盘不能因此失效。"""
    log = stalled_sink_server.data_dir / "logs" / "teamdoc.log"
    deadline = time.time() + 5
    text = ""
    while time.time() < deadline:
        text = log.read_text("utf-8", "replace") if log.exists() else ""
        if "/api/auth/status" in text:
            break
        time.sleep(0.2)
    assert log.exists(), f"日志文件未生成: {log}"
    assert "/api/auth/status" in text, "访问日志未落盘"


def test_console_keeps_uvicorn_rendering(server):
    """控制台里 uvicorn 的行仍是它自己的渲染(紧凑前缀 + 状态短语),没被统一格式取代。"""
    server.admin_client().get("/api/auth/status")
    out = server.data_dir / "server.log"  # 托管实例的 stdout/stderr 捕获
    deadline = time.time() + 5
    text = ""
    while time.time() < deadline:
        text = out.read_text("utf-8", "replace") if out.exists() else ""
        if "GET /api/auth/status" in text:
            break
        time.sleep(0.2)
    assert re.search(r"^INFO:\s+Started server process", text, re.M), text[-1500:]
    assert '- "GET /api/auth/status HTTP/1.1" 200 OK' in text, text[-1500:]
    assert "[uvicorn.access]" not in text, "uvicorn 的行被我们的统一格式接管了"
