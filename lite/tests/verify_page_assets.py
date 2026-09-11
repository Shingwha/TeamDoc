"""模拟浏览器加载 index.html:解析全部引用资源并逐一请求,确认无 404/无外链。

用法:先启动服务,再运行本脚本。退出码非 0 表示存在坏引用。
"""
import os
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")


def fetch(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "TeamDoc verifier"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            # uvicorn 发送小写头名,统一转小写再取
            return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}


problems = []
print("=== 1) 抓取 index.html ===")
st, html_bytes, headers = fetch("/index.html")
html = html_bytes.decode("utf-8")
print(f"  /index.html -> {st}, {len(html_bytes)} 字节, Cache-Control={headers.get('cache-control')}")
if st != 200:
    problems.append("/index.html 不可访问")

print("\n=== 2) 外链检查(纯内网部署要求零外链) ===")
ext = [u for u in re.findall(r'(?:src|href)="(https?://[^"]+)"', html)]
# 内联 SVG 的 xmlns 命名空间不是网络请求
ext = [u for u in ext if "www.w3.org" not in u]
if ext:
    for u in ext:
        problems.append(f"仍存在外链: {u}")
    print(f"  ✗ 发现 {len(ext)} 个外链")
else:
    print("  OK  零外链")

print("\n=== 3) 逐资源请求(HTML 直接引用) ===")
refs = re.findall(r'(?:src|href)="(/[^"]+)"', html)
for ref in sorted(set(refs)):
    st, body, hd = fetch(ref)
    cc = hd.get("cache-control", "")
    ok = st == 200 and cc == "no-cache"
    print(f"  {'OK ' if ok else 'FAIL'} {st:3d}  {cc:10s}  {len(body):7d}B  {ref}")
    if st != 200:
        problems.append(f"{ref} -> HTTP {st}")
    elif cc != "no-cache":
        problems.append(f"{ref} 缺少 no-cache")

print("\n=== 4) 递归跟随 CSS 内引用的资源(字体等) ===")
css_refs = [r for r in sorted(set(refs)) if r.endswith(".css")]
seen = set()
for css_ref in css_refs:
    st, body, _ = fetch(css_ref)
    if st != 200:
        continue
    text = body.decode("utf-8", "replace")
    for m in re.finditer(r'url\((["\']?)([^)"\']+)\1\)', text):
        u = m.group(2)
        if u.startswith(("data:", "http:", "https:", "#")):
            continue
        full = urllib.parse.urljoin(css_ref, u)
        if full in seen:
            continue
        seen.add(full)
        st2, b2, _ = fetch(full)
        if st2 != 200:
            problems.append(f"{css_ref} 引用的 {u} -> HTTP {st2}")
            print(f"  FAIL {st2:3d}  {full}  (来自 {css_ref})")
print(f"  CSS 内引用资源 {len(seen)} 个,坏引用 {len([p for p in problems if '引用的' in p])} 个")

print("\n=== 5) JS 中的动态资源路径(KaTeX / Prism components) ===")
st, md, _ = fetch("/js/markdown.js")
mdjs = md.decode("utf-8")
katex_base = re.search(r"KATEX_BASE\s*=\s*'([^']+)'", mdjs)
prism_path = re.search(r"PRISM_COMPONENTS\s*=\s*'([^']+)'", mdjs)
checks = []
if katex_base:
    b = katex_base.group(1)
    checks += [f"{b}/katex.min.js", f"{b}/katex.min.css"]

# KaTeX 字体全量校验:katex.min.css 是**运行时**注入的,不在 index.html 的引用里,
# 所以第 4 步的递归跟随覆盖不到它。它内部引用了 40 个 woff2/woff —— 漏一个,
# 对应的数学符号在离线内网就永远加载不出来(而且只在那类公式出现时才暴露)。
if katex_base:
    b = katex_base.group(1)
    st, css_body, _ = fetch(f"{b}/katex.min.css")
    if st == 200:
        css_txt = css_body.decode("utf-8", "replace")
        fonts = sorted({m.group(2) for m in
                        re.finditer(r'url\((["\']?)([^)"\']+)\1\)', css_txt)
                        if not m.group(2).startswith(("data:", "http", "#"))})
        bad = 0
        for f in fonts:
            full = urllib.parse.urljoin(f"{b}/katex.min.css", f)
            stf, _b, _ = fetch(full)
            if stf != 200:
                problems.append(f"KaTeX 字体 {full} -> HTTP {stf}")
                bad += 1
                print(f"  FAIL {stf:3d}  {full}")
        print(f"  KaTeX 字体 {len(fonts)} 个,坏引用 {bad} 个")
    else:
        problems.append(f"katex.min.css -> HTTP {st}")
        print(f"  FAIL {st:3d}  {b}/katex.min.css")

# Prism 语言包:autoloader 按文档里的围栏语言**按需**拉取,仓库随附有限的常见语言。
# 抽查 3 个只能证明"目录在";这里改为全量校验随附的 components 目录,
# 并额外断言一批常见语言确实存在(缺了只会"不高亮",不会报错,所以最容易被忽略)。
if prism_path:
    p = prism_path.group(1)
    common = ["python", "go", "sql", "javascript", "typescript", "java", "csharp", "cpp",
              "bash", "json", "yaml", "markdown", "rust", "php", "ruby", "kotlin", "swift",
              "docker", "nginx", "powershell"]
    for lang in common:
        c = f"{p}prism-{lang}.min.js"
        st, body, _ = fetch(c)
        if st != 200:
            problems.append(f"Prism 语言包 {lang} -> HTTP {st}")
            print(f"  FAIL {st:3d}  prism-{lang}")
    print(f"  Prism 常见语言包 {len(common)} 个已校验")
    # 目录清单里的文件也逐个确认可达
    st, idx, _ = fetch(f"{p}")
    if st == 200:
        listed = sorted(set(re.findall(r'href="([^"]+\.min\.js)"', idx.decode("utf-8", "replace"))))
        bad = 0
        for f in listed:
            full = urllib.parse.urljoin(f"{p}", f)
            stf, _b, _ = fetch(full)
            if stf != 200:
                problems.append(f"Prism 组件 {full} -> HTTP {stf}")
                bad += 1
        if listed:
            print(f"  Prism 组件目录 {len(listed)} 个文件,坏引用 {bad} 个")

for c in checks:
    st, body, _ = fetch(c)
    ok = st == 200
    print(f"  {'OK ' if ok else 'FAIL'} {st:3d}  {len(body):7d}B  {c}")
    if not ok:
        problems.append(f"动态资源 {c} -> HTTP {st}")

print("\n=== 6) index.html 中的内联脚本与 DOM 骨架 ===")
for probe, desc in [
    ("td:theme-mode", "首屏主题防闪烁脚本"),
    ("td:seed-color", "种子色恢复"),
    ('id="shell"', "SPA 壳节点"),
    ('id="login-root"', "登录视图节点"),
    ('id="toast-root"', "Toast 容器"),
    ('id="side-projects"', "侧栏项目树容器"),
]:
    ok = probe in html
    print(f"  {'OK ' if ok else 'FAIL'}  {desc}")
    if not ok:
        problems.append(f"index.html 缺少 {desc}")

print("\n" + "=" * 60)
if problems:
    print(f"!! {len(problems)} 项问题:")
    for p in problems:
        print("   - " + p)
    sys.exit(1)
print("页面资源完整,无外链,全部 no-cache ✓")
