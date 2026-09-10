"""逐页巡检:真实 app.js 驱动每个路由,收集 JS 错误并报告渲染情况。

用法:
    python tests/visual_sweep.py            # 需要 TD_BASE/TD_SHOT_DIR 环境变量
    TD_BASE=http://127.0.0.1:8137 python tests/visual_sweep.py

它用一个临时校验页(注入到 web/ 下,跑完自动删除)完成:
  同步 XHR 登录 → 等 app.js 的路由跑完 → 切到目标 hash → 收集 window.onerror
  与 console.error → 把结果写进 DOM 供本脚本读取。

注意两个坑(都踩过):
  * 必须在 window load 之后再改 hash:app.js 的 DOMContentLoaded 处理器会按
    当时的 hash 路由一次,提前改会被覆盖回 #/。
  - URL 里的 # 必须 percent-encode,否则它会被当成 URL 的 fragment 截断。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
TMP_PAGE = "_visual_sweep.html"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

INJECT = '''  <script>
    (function () {
      var x = new XMLHttpRequest();
      x.open('POST', '/api/auth/login', false);
      x.setRequestHeader('Content-Type', 'application/json');
      x.send(JSON.stringify({ email: 'admin@teamdoc.local', password: 'admin12345' }));
      window.__loginStatus = x.status;
    })();
    window.__errors = [];
    window.onerror = function (m, s, l) { window.__errors.push(m + ' @line' + l); };
    var __ce = console.error;
    console.error = function () {
      window.__errors.push('console.error: ' + [].join.call(arguments, ' '));
      __ce.apply(console, arguments);
    };
  </script>
  <script src="/js/app.js"></script>
  <script>
    var HASH = (new URLSearchParams(location.search)).get('h') || '#/';
    function report() {
      var l = ['hash=' + location.hash, 'errors=' + (window.__errors.join(' | ') || 'none')];
      var t = document.querySelector('.page-title');
      l.push('title=' + (t ? t.textContent : '(无)'));
      l.push('bytes=' + document.getElementById('view').innerHTML.length);
      var el = document.createElement('pre');
      el.id = 'sweep-out';
      el.textContent = l.join(String.fromCharCode(10));
      document.body.appendChild(el);
    }
    window.addEventListener('load', function () {
      setTimeout(function () {
        location.hash = HASH;
        window.dispatchEvent(new HashChangeEvent('hashchange'));
        setTimeout(report, 2600);
      }, 150);
    });
  </script>
'''


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def main():
    chrome = find_chrome()
    if not chrome:
        print("未找到 Chrome/Edge,跳过逐页巡检")
        return 0

    src = open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read()
    marker = '  <script src="/js/app.js"></script>\n'
    assert marker in src, "index.html 结构变了:找不到 app.js 引用"
    page = os.path.join(WEB_DIR, TMP_PAGE)
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(src.replace(marker, INJECT))

    # 取一个有文档的项目,用于项目内页面
    pid = os.environ.get("TD_PID", "")
    doc_id = os.environ.get("TD_DOC", "")
    pages = [
        ("首页", "#/"),
        ("发现", "#/discover"),
        ("搜索", "#/search"),
        ("个人设置", "#/settings"),
        ("管理后台", "#/admin"),
    ]
    if pid:
        pages += [
            ("文档", f"#/p/{pid}/docs"),
            ("云空间", f"#/p/{pid}/files"),
            ("成员", f"#/p/{pid}/members"),
            ("回收站", f"#/p/{pid}/trash"),
            ("项目设置", f"#/p/{pid}/settings"),
        ]
    if pid and doc_id:
        # 编辑器是最重的页面(WS、自动保存、浮动工具栏),权限改动最容易在这里出问题
        pages.append(("文档详情", f"#/p/{pid}/docs/{doc_id}"))

    failures = []
    try:
        for name, hash_ in pages:
            enc = urllib.parse.quote(hash_, safe="")
            url = f"{BASE}/{TMP_PAGE}?h={enc}"
            with tempfile.TemporaryDirectory() as td:
                dom = os.path.join(td, "dom.html")
                with open(dom, "w", encoding="utf-8") as out:
                    subprocess.run(
                        [chrome, "--headless", "--disable-gpu", "--no-sandbox",
                         "--virtual-time-budget=12000", "--dump-dom", url],
                        stdout=out, stderr=subprocess.DEVNULL, timeout=120)
                content = open(dom, encoding="utf-8", errors="replace").read()
            i = content.find('<pre id="sweep-out"')
            if i < 0:
                print(f"  FAIL  {name:8s} 未产出巡检结果(页面可能整块崩了)")
                failures.append(name)
                continue
            text = re.sub(r"<[^>]+>", "", content[i:content.find("</pre>", i)]).strip()
            info = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            errs = info.get("errors", "?")
            ok = errs == "none" and info.get("hash") == hash_
            mark = "OK  " if ok else "FAIL"
            print(f"  {mark}  {name:8s} {info.get('title', '?'):10s} "
                  f"hash={info.get('hash')} bytes={info.get('bytes')}"
                  + ("" if ok else f"  errors={errs}"))
            if not ok:
                failures.append(name)
    finally:
        if os.path.exists(page):
            os.remove(page)

    print()
    if failures:
        print(f"有 {len(failures)} 个页面异常: {', '.join(failures)}")
        return 1
    print(f"全部 {len(pages)} 个页面渲染正常,无 JS 错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
