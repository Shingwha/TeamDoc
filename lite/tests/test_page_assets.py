"""模拟浏览器加载 index.html:解析全部引用资源并逐一请求,确认无 404/无外链。

纯内网部署前后的必跑项;本文件是只读的,可安全对准任何实例(含生产)运行。
KaTeX 字体与 Prism 语言包不在 index.html 的静态引用里(运行时按需加载),
故单独从 markdown.js 的配置常量解析出路径后全量校验。
问题先收集再统一断言:一次看到全部坏引用,而不是改一个跑一次。
"""
import re
import urllib.parse

from _harness import Client

PRISM_COMMON_LANGS = [
    "python", "go", "sql", "javascript", "typescript", "java", "csharp", "cpp",
    "bash", "json", "yaml", "markdown", "rust", "php", "ruby", "kotlin", "swift",
    "docker", "nginx", "powershell",
]


def test_page_assets_complete(base_url):
    c = Client(base_url)
    problems = []

    # === 1) 抓取 index.html ===
    r = c.get("/index.html")
    assert r.status == 200, "/index.html 不可访问"
    html = r.data.decode("utf-8")
    assert r.headers.get("cache-control") == "no-cache", \
        f"index.html 应 no-cache: {r.headers.get('cache-control')}"

    # === 2) 外链检查(纯内网部署要求零外链;内联 SVG 的 xmlns 不是网络请求) ===
    ext = [u for u in re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
           if "www.w3.org" not in u]
    problems += [f"仍存在外链: {u}" for u in ext]

    # === 3) 逐资源请求(HTML 直接引用) ===
    refs = sorted(set(re.findall(r'(?:src|href)="(/[^"]+)"', html)))
    for ref in refs:
        rr = c.get(ref)
        cc = rr.headers.get("cache-control", "")
        if rr.status != 200:
            problems.append(f"{ref} -> HTTP {rr.status}")
        elif cc != "no-cache":
            problems.append(f"{ref} 缺少 no-cache(实际 {cc!r})")

    # === 4) 递归跟随 CSS 内引用的资源(字体等) ===
    seen = set()
    for css_ref in [x for x in refs if x.endswith(".css")]:
        body = c.get(css_ref).data
        if not isinstance(body, bytes):
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
            st = c.get(full).status
            if st != 200:
                problems.append(f"{css_ref} 引用的 {u} -> HTTP {st}")

    # === 5) JS 中的动态资源路径(KaTeX / Prism components) ===
    mdjs = c.get("/js/markdown.js").data.decode("utf-8")
    katex_base = re.search(r"KATEX_BASE\s*=\s*'([^']+)'", mdjs)
    prism_path = re.search(r"PRISM_COMPONENTS\s*=\s*'([^']+)'", mdjs)

    # KaTeX 字体全量校验:katex.min.css 是**运行时**注入的,不在 index.html 的引用里,
    # 所以上一步的递归跟随覆盖不到它。它内部引用了 40 个 woff2/woff —— 漏一个,
    # 对应的数学符号在离线内网就永远加载不出来(而且只在那类公式出现时才暴露)。
    if katex_base:
        b = katex_base.group(1)
        rr = c.get(f"{b}/katex.min.css")
        if rr.status != 200:
            problems.append(f"katex.min.css -> HTTP {rr.status}")
        else:
            css_txt = rr.data.decode("utf-8", "replace")
            fonts = sorted({m.group(2) for m in
                            re.finditer(r'url\((["\']?)([^)"\']+)\1\)', css_txt)
                            if not m.group(2).startswith(("data:", "http", "#"))})
            for f in fonts:
                full = urllib.parse.urljoin(f"{b}/katex.min.css", f)
                stf = c.get(full).status
                if stf != 200:
                    problems.append(f"KaTeX 字体 {full} -> HTTP {stf}")
        for sub in ("katex.min.js", "katex.min.css"):
            if c.get(f"{b}/{sub}").status != 200:
                problems.append(f"动态资源 {b}/{sub} 不可达")

    # Prism 语言包:autoloader 按文档里的围栏语言**按需**拉取。缺了只会"不高亮",
    # 不会报错,所以最容易被忽略 —— 常见语言逐个查,目录清单也逐个确认可达。
    if prism_path:
        p = prism_path.group(1)
        for lang in PRISM_COMMON_LANGS:
            if c.get(f"{p}prism-{lang}.min.js").status != 200:
                problems.append(f"Prism 语言包 {lang} -> HTTP 404")
        idx = c.get(f"{p}")
        if idx.status == 200:
            listed = sorted(set(re.findall(r'href="([^"]+\.min\.js)"',
                                           idx.data.decode("utf-8", "replace"))))
            for f in listed:
                full = urllib.parse.urljoin(f"{p}", f)
                if c.get(full).status != 200:
                    problems.append(f"Prism 组件 {full} -> HTTP 404")

    # === 6) index.html 中的内联脚本与 DOM 骨架 ===
    for probe, desc in [
        ("td:theme-mode", "首屏主题防闪烁脚本"),
        ("td:seed-color", "种子色恢复"),
        ('id="shell"', "SPA 壳节点"),
        ('id="login-root"', "登录视图节点"),
        ('id="toast-root"', "Toast 容器"),
        ('id="side-projects"', "侧栏项目树容器"),
    ]:
        if probe not in html:
            problems.append(f"index.html 缺少{desc}")

    assert not problems, f"{len(problems)} 项问题:\n" + "\n".join("  - " + p for p in problems)
