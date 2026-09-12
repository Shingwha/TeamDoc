"""无头 Chrome 校验底座:临时校验页注入 + --dump-dom 取回结果。

机制:把注入脚本写进 web/ 下的临时页(只有站内绝对路径才能引到真实样式与 app.js),
跑完必删;--virtual-time-budget 下 CSS 过渡不推进,注入页需先整体关掉过渡。

Windows 坑(都实测踩过,别丢):
- 不能把打开的文件句柄交给 Chrome 当 stdout —— 句柄会被 Chrome 子进程继承,
  临时目录清理时 unlink 报 WinError 32(连崩三次)。故统一 capture_output。
- subprocess 文本模式按系统 locale(cp936)解码,中文标题会乱码 —— 统一拿原始
  字节自己按 utf-8 解。
"""
import os
import re
import subprocess
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from _harness import ADMIN_EMAIL, ADMIN_PASSWORD, WEB_DIR

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def login_js(email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
    """同步 XHR 登录块:注入页在 app.js 跑起来之前就要有会话。

    默认管理员;自助加入这类"必须换个身份才看得见问题"的演练传自己的账号。
    """
    return (
        "(function () {\n"
        "      var x = new XMLHttpRequest();\n"
        "      x.open('POST', '/api/auth/login', false);\n"
        "      x.setRequestHeader('Content-Type', 'application/json');\n"
        f"      x.send(JSON.stringify({{ email: '{email}', password: '{password}' }}));\n"
        "    })();"
    )


def error_trap_js():
    """window.onerror + console.error 收集块:巡检的眼睛。"""
    return (
        "window.__errors = [];\n"
        "    window.onerror = function (m, s, l) { window.__errors.push(m + ' @line' + l); };\n"
        "    var __ce = console.error;\n"
        "    console.error = function () {\n"
        "      window.__errors.push('console.error: ' + [].join.call(arguments, ' '));\n"
        "      __ce.apply(console, arguments);\n"
        "    };"
    )


@contextmanager
def temp_page(name, inject):
    """把 index.html 复制为临时校验页并注入脚本;用完必删(放在 web/ 下才能引真实资源)。"""
    src = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    marker = '  <script src="/js/app.js"></script>\n'
    assert marker in src, "index.html 结构变了:找不到 app.js 引用(注入点)"
    page = WEB_DIR / name
    page.write_text(src.replace(marker, inject), encoding="utf-8")
    try:
        yield page.name
    finally:
        if page.exists():
            page.unlink()


def dump_page(chrome, base_url, page_name, query="", budget=12000):
    """跑一次无头 Chrome 并取回 DOM 文本。"""
    url = f"{base_url}/{page_name}" + (f"?{query}" if query else "")
    proc = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox",
         f"--virtual-time-budget={budget}", "--dump-dom", url],
        capture_output=True, timeout=300)
    return proc.stdout.decode("utf-8", "replace")


def read_report(dom_text, out_id="sweep-out"):
    """从 dump 出来的 DOM 里取回注入页写下的 `key=value` 报告(无则 None)。"""
    i = dom_text.find(f'<pre id="{out_id}"')
    if i < 0:
        return None
    text = re.sub(r"<[^>]+>", "", dom_text[i:dom_text.find("</pre>", i)]).strip()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
