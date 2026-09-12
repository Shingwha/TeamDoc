"""逐页巡检:真实 app.js 驱动每个路由,收集 JS 错误并报告渲染情况;末尾做一次交互断言。

用法:
    TD_BASE=http://127.0.0.1:8137 python tests/visual_sweep.py
    TD_BASE=... TD_PID=10000 python tests/visual_sweep.py     # 额外覆盖项目内页面与文档树折叠

它用一个临时校验页(注入到 web/ 下,跑完自动删除)完成:
  同步 XHR 登录 → 等 app.js 的路由跑完 → 切到目标 hash → 收集 window.onerror
  与 console.error → 把结果写进 DOM 供本脚本读取。

交互断言为什么必须有:静态渲染正常 ≠ 交互正常。"点侧栏项目行没反应""折叠箭头点不动"
这类缺陷既不报错也不让页面崩,只有真实点击 + 断言状态变化才抓得到(资源 id 整数化后
侧栏与文档树各坏过一次,都是这么漏过去的)。

注意两个坑(都踩过):
  * 必须在 window load 之后再改 hash:app.js 的 DOMContentLoaded 处理器会按
    当时的 hash 路由一次,提前改会被覆盖回 #/。
  - URL 里的 # 必须 percent-encode,否则它会被当成 URL 的 fragment 截断。

另一个坑(Windows):**不能**把打开的文件句柄交给 Chrome 当 stdout —— 句柄会被 Chrome
子进程继承,临时目录清理时 unlink 报 WinError 32(实测连崩三次)。故统一 capture_output。
"""
import os
import re
import subprocess
import sys
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


# 交互断言页:点侧栏项目行(展开 → 再点收起);进项目文档页点折叠箭头(子层 hidden 应切换)
INJECT_INTERACT = '''  <script>
    (function () {
      var x = new XMLHttpRequest();
      x.open('POST', '/api/auth/login', false);
      x.setRequestHeader('Content-Type', 'application/json');
      x.send(JSON.stringify({ email: 'admin@teamdoc.local', password: 'admin12345' }));
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
  (function () {
    var OUT = {};
    var PID = (new URLSearchParams(location.search)).get('pid') || '';
    function kids() { return document.querySelectorAll('.tree-children:not([hidden]) .side-tree-children').length; }
    function report() {
      var l = ['hash=' + location.hash, 'errors=' + (window.__errors.join(' | ') || 'none')];
      Object.keys(OUT).forEach(function (k) { l.push(k + '=' + OUT[k]); });
      var el = document.createElement('pre');
      el.id = 'sweep-out';
      el.textContent = l.join(String.fromCharCode(10));
      document.body.appendChild(el);
    }
    function step2() {
      if (!PID) { OUT.docTreeChecked = 0; report(); return; }
      location.hash = '#/p/' + PID + '/docs';
      setTimeout(function () {
        var caret = document.querySelector('#doc-tree .tree-caret:not(.leaf)');
        if (!caret) { OUT.docTreeChecked = 0; report(); return; }
        var row = caret.closest('.doc-row');
        var id = row.dataset.id;
        var before = row.parentElement.querySelector('.tree-children');
        if (!before) { OUT.docTreeChecked = 0; report(); return; }
        OUT.docTreeChecked = 1;
        OUT.docChildrenHiddenBefore = before.hasAttribute('hidden') ? 1 : 0;
        caret.click();
        setTimeout(function () {
          // 点击后整棵树会重绘,旧节点已脱离文档 —— 必须按 data-id 重新查询
          var row2 = document.querySelector('#doc-tree .doc-row[data-id="' + id + '"]');
          var after = row2 ? row2.parentElement.querySelector('.tree-children') : null;
          OUT.docChildrenHiddenAfterClick = (after && after.hasAttribute('hidden')) ? 1 : 0;
          report();
        }, 500);
      }, 2600);
    }
    window.addEventListener('load', function () {
      setTimeout(function () {
        // 侧栏:点项目行 = 展开,再点 = 收起(不导航);每次点击前重新查询(重绘会换节点)
        var rows = document.querySelectorAll('.side-proj');
        OUT.sidebarRows = rows.length;
        if (!rows.length) { step2(); return; }
        rows[0].click();
        setTimeout(function () {
          OUT.sidebarChildrenAfterExpand = kids();
          document.querySelectorAll('.side-proj')[0].click();
          setTimeout(function () {
            OUT.sidebarChildrenAfterCollapse = kids();
            step2();
          }, 400);
        }, 400);
      }, 2200);
    });
  })();
  </script>
'''


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def dump_page(chrome, page, query="", budget=12000):
    """跑一次无头 Chrome 并取回 DOM 文本(capture_output:句柄不进 Chrome,见文件头说明)"""
    url = f"{BASE}/{os.path.basename(page)}?{query}" if query else f"{BASE}/{os.path.basename(page)}"
    proc = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox",
         f"--virtual-time-budget={budget}", "--dump-dom", url],
        capture_output=True, timeout=300)
    # 显式按 utf-8 解码:text=True 会按系统 locale(Windows 是 cp936)解,中文标题会变乱码
    return proc.stdout.decode("utf-8", "replace")


def read_report(content):
    """从 dump 出来的 DOM 里取回注入页写下的 `key=value` 报告(无则 None)"""
    i = content.find('<pre id="sweep-out"')
    if i < 0:
        return None
    text = re.sub(r"<[^>]+>", "", content[i:content.find("</pre>", i)]).strip()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def check_interactions(chrome, page, src, marker, pid):
    """真实点击后断言状态变化(返回 (ok, 说明))。见文件头"交互断言为什么必须有"."""
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(src.replace(marker, INJECT_INTERACT))
    info = read_report(dump_page(chrome, page, f"pid={urllib.parse.quote(pid, safe='')}", budget=30000))
    if info is None:
        return False, "未产出交互结果(页面可能整块崩了)"
    problems = []
    if info.get("errors", "?") != "none":
        problems.append("JS 错误: " + info.get("errors", "?"))
    notes = []
    if info.get("sidebarRows", "0") == "0":
        notes.append("无项目,侧栏断言跳过")
    else:
        expanded = info.get("sidebarChildrenAfterExpand", "0")
        if not expanded.isdigit() or int(expanded) < 1:
            problems.append("侧栏项目行点不开(点击后没有子项)")
        if info.get("sidebarChildrenAfterCollapse", "?") != "0":
            problems.append("侧栏项目行收不起(再点后子项仍在)")
        else:
            notes.append("侧栏展开/收起正常")
    if info.get("docTreeChecked", "0") == "1":
        if info.get("docChildrenHiddenBefore") == info.get("docChildrenHiddenAfterClick"):
            problems.append("文档树折叠箭头无效(子层 hidden 未切换)")
        else:
            notes.append("文档树折叠正常")
    else:
        notes.append("文档树折叠跳过(项目无子文档)")
    if problems:
        return False, "; ".join(problems)
    return True, ";".join(notes)


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
            content = dump_page(chrome, page, f"h={enc}")
            info = read_report(content)
            if info is None:
                print(f"  FAIL  {name:8s} 未产出巡检结果(页面可能整块崩了)")
                failures.append(name)
                continue
            errs = info.get("errors", "?")
            ok = errs == "none" and info.get("hash") == hash_
            mark = "OK  " if ok else "FAIL"
            print(f"  {mark}  {name:8s} {info.get('title', '?'):10s} "
                  f"hash={info.get('hash')} bytes={info.get('bytes')}"
                  + ("" if ok else f"  errors={errs}"))
            if not ok:
                failures.append(name)
        # 交互断言(静态渲染正常 ≠ 交互正常)
        ok, note = check_interactions(chrome, page, src, marker, pid)
        print(f"  {'OK  ' if ok else 'FAIL'}  交互      {note}")
        if not ok:
            failures.append("交互")
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
