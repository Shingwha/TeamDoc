"""事件循环停滞看门狗:心跳 + 全线程栈转储。

"进程活着、控制台没有任何输出、所有客户端都连不上"这类故障事后无法从日志推断,
所以在停滞超过阈值时直接把**所有线程当前的 Python 栈**写进 logs/stall-*.txt
(faulthandler 抓的是真实帧,且不需要目标线程配合),下次一眼就能看到卡在哪个调用。

局限:若某线程在 C 调用中一直持着 GIL,本线程也无法运行 —— 用外部
`py-spy dump --pid <pid>` 兜底(见 DEPLOY.md 排障小节)。
"""
import asyncio
import faulthandler
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from logsetup import LOG_DIR  # 与日志同目录(单一来源,见 logsetup)

logger = logging.getLogger("teamdoc.watchdog")

# 停滞阈值:正常请求都是毫秒级,连"重量级同步端点"也跑在 anyio 工作线程里、
# 不影响事件循环心跳,所以 10 秒只可能是真卡住。
STALL_SECONDS = float(os.environ.get("WATCHDOG_STALL_SECONDS", "10"))
# 转储冷却:卡住往往持续很久,不冷却会每秒写一个文件把盘写满
COOLDOWN_SECONDS = 60.0

PROCESS_START = time.monotonic()
_last_beat = time.monotonic()
_started = False
_lock = threading.Lock()
_heartbeat_task: asyncio.Task | None = None


def uptime_seconds() -> float:
    return time.monotonic() - PROCESS_START


async def _heartbeat():
    global _last_beat
    while True:
        _last_beat = time.monotonic()
        await asyncio.sleep(1.0)


def _dump() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"stall-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"# 事件循环停滞转储 {datetime.now().isoformat()}\n"
                 f"# 停滞时长 {time.monotonic() - _last_beat:.1f}s\n")
        faulthandler.dump_traceback(file=fh, all_threads=True)
    return path


def _watch():
    last_dump = 0.0
    while True:
        time.sleep(1.0)
        stalled = time.monotonic() - _last_beat
        if stalled < STALL_SECONDS:
            continue
        now = time.monotonic()
        if now - last_dump < COOLDOWN_SECONDS:
            continue
        last_dump = now
        try:
            path = _dump()
            logger.warning("事件循环停滞 %.1fs,已转储全部线程栈到 %s", stalled, path)
        except Exception:
            logger.exception("停滞转储失败")


def start():
    """应用启动时调用一次(幂等):起心跳协程与守护线程。"""
    global _started, _heartbeat_task
    with _lock:
        if _started:
            return
        _started = True
    _heartbeat_task = asyncio.get_running_loop().create_task(_heartbeat())
    threading.Thread(target=_watch, name="teamdoc-watchdog", daemon=True).start()
