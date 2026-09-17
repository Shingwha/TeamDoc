"""Markdown 语法的快速单测:在 node 里加载真实的 js/markdown.js,对一张"语法 → 期望结果"的表逐条断言。

为什么不只靠浏览器演练:
  * 快 —— 毫秒级,不启服务、不开浏览器,所以边界可以列得很密;
  * 语法规则是**成组**变化的(比如公式的定界规则一改,价格文本、转义、代码块会一起受影响),
    一条条列出来才看得出哪些是刻意为之、哪些是回归;
  * 浏览器演练(test_markdown_diagram.py)验证的是"链路通不通"(库真的被拉下来、DOM 真的画出来、
    不发跨源请求),规则本身在这里钉死。两者互补,缺一条都会漏。

两趟跑:
  A 趟把三个懒加载库 stub 成**已就绪**,断言"这段源码被认成了什么";
  B 趟让库**缺席**(加载器的 script 请求被记录、onload 立刻回调),断言"什么时候去拉库" ——
  懒加载门槛是这个项目的硬约束(内网 + 3MB 的图表库),认错的代价是"没有图的文档也下载 2.6MB"。
"""
import json
import shutil
import subprocess

import pytest

from _harness import WEB_DIR

NODE = shutil.which("node")
WEB_JS = str(WEB_DIR).replace("\\", "/") + "/"

HARNESS = r"""
const fs = require('fs');
const WEB = __WEB__;
const MODE = '__MODE__';
const CASES = __CASES__;

global.window = globalThis;
global.UI = {
  esc: (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  btn: () => '',            // 代码块角标是 DOM 层的事,由浏览器演练覆盖
  copyText: () => {},
};
global.getComputedStyle = () => ({ fontFamily: 'sans-serif' });
global.window.marked = require(WEB + 'vendor/marked/marked.min.js');

const REQUESTS = [];
// markdown.js 顶层会往 document 上挂"文内锚点"的委托点击(脚注角标/回跳不走 location.hash),
// 所以桩里的 document 必须有 addEventListener —— 真实浏览器一定有,这里不能省。
// 监听器要**收下来**:文内锚点的分流规则(拦文内锚点、放行 #/ 路由)在这里做单元验证,
// 浏览器演练只覆盖"点了之后真的滚动、且没被踢出文档"。
const DOC_BASE = {
  body: null,
  listeners: {},
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  getElementById() { return null; },   // 锚点目标不存在:拦下点击但不滚动(jumpToAnchor 打头就返回)
};
if (MODE === 'ready') {
  // 库已就绪:断言"被认成了什么"。stub 的 class 刻意不叫 katex* —— 显示公式外面还包着
  // .katex-display-wrapper,名字太像会把两层一起数进去
  global.window.katex = {
    renderToString: (t, o) => '<span class="kxstub" data-display="' + (o.displayMode ? 1 : 0) + '">' + t + '</span>',
  };
  global.window.mermaid = { initialize() {}, render() { return Promise.resolve({ svg: '' }); } };
  global.document = Object.assign({ createElement: () => ({}), head: { appendChild() {} } }, DOC_BASE);
} else {
  // 库缺席:记录懒加载请求,并让 onload 立刻回调(否则预热的 Promise 永不落地、render 挂死)
  global.document = Object.assign({
    createElement: () => {
      const el = {};
      Object.defineProperty(el, 'src', { set(v) { REQUESTS.push(v); }, get() { return ''; } });
      return el;
    },
    head: { appendChild: (el) => { if (el && typeof el.onload === 'function') setTimeout(el.onload, 0); } },
  }, DOC_BASE);
}
eval(fs.readFileSync(WEB + 'js/markdown.js', 'utf8'));

(async () => {
  const out = [];
  for (const c of CASES) {
    const before = REQUESTS.length;
    let html = '';
    try { html = await window.MdRender.render(c.md); }
    catch (e) { html = '!!THROW!! ' + e.message; }
    out.push({ name: c.name, html: html, requests: REQUESTS.slice(before) });
  }
  out.push({ name: '__anchors__', anchors: anchorProbe() });
  console.log(JSON.stringify(out));
})();

// 文内锚点的分流:内容里的 #fn-1 必须被拦下(交给浏览器走就是"跳出文档"),
// 而 #/discover 这种站内路由必须原样放行(吞掉它等于把站内链接写死)
function anchorProbe() {
  const res = { registered: 0, anchorPrevented: null, routePrevented: null, threw: '' };
  const fns = (global.document.listeners || {}).click || [];
  res.registered = fns.length;
  if (!fns.length) return res;
  const fire = (href) => {
    let prevented = false;
    const a = {
      getAttribute: () => href,
      closest: () => a,                       // 模拟"点击落在一个匹配的链接上"
      hasAttribute: () => false,
      classList: { add() {}, remove() {} },
      focus() {}, scrollIntoView() {}, offsetWidth: 0,
    };
    const ev = { target: a, preventDefault: () => { prevented = true; } };
    for (const fn of fns) fn(ev);
    return prevented;
  };
  try {
    res.anchorPrevented = fire('#fn-1');
    res.routePrevented = fire('#/discover');
  } catch (e) { res.threw = e.message; }
  return res;
}
"""

# 语法 → 期望。键位:
#   math    行内公式数(KaTeX stub 出现次数)
#   display 显示公式数(.katex-display-wrapper)
#   has     必须出现的片段
#   hasnt   必须不出现的片段
#   loads   本轮应当去请求的库(子串,只 B 趟有效)
#   noloads 本轮**不该**请求的库
CASES = [
    # ---- 公式:行内 $…$ 的三条定界要求 ----
    {"name": "价格文本不是公式", "md": "价格 $5 和 $6 之间。", "math": 0, "has": ["$5 和 $6"], "noloads": ["katex"]},
    {"name": "三个价格", "md": "共 $3、$5 与 $7 三项。", "math": 0, "noloads": ["katex"]},
    {"name": "US$ 写法", "md": "定价 US$5 与 US$6 两档。", "math": 0},
    {"name": "内侧贴空白不是公式", "md": "写成 $ x + y $ 不行。", "math": 0, "has": ["$ x + y $"], "noloads": ["katex"]},
    {"name": "标准行内公式", "md": "公式 $E=mc^2$ 结束。", "math": 1, "loads": ["katex"]},
    {"name": "行内公式里的转义美元", "md": "价格 $\\$5$ 与 $x$ 两个。", "math": 2},
    {"name": "转义美元符号是字面量", "md": "价格 \\$100 不是公式。", "math": 0, "has": ["$100"], "noloads": ["katex"]},
    {"name": "单行块级公式", "md": "$$a^2+b^2$$", "math": 1, "display": 1, "has": ["<span class=\"katex-display-wrapper\">"]},
    {"name": "多行块级公式", "md": "$$\n\\int_0^1 x\\,dx\n$$", "math": 1, "display": 1, "has": ["int_0^1"]},
    {"name": "段落中间的 $$", "md": "前文 $$a^2$$ 后文。", "math": 1, "display": 1, "has": ["前文 ", " 后文。"]},
    {"name": "段落中间的 $$ 内侧贴空白不算", "md": "成本 $$ 和 $$ 之间。", "math": 0, "has": ["$$ 和 $$"], "noloads": ["katex"]},
    {"name": "两段各自的块级公式", "md": "$$\na\n$$\n\n正文\n\n$$\nb\n$$", "math": 2, "display": 2},
    # ---- 代码里的 $ 一律不是公式 ----
    {"name": "围栏代码块里的 $", "md": "```\n$x$ 与 $$y$$\n```", "math": 0, "has": ["<pre><code>"], "noloads": ["katex"]},
    {"name": "行内代码里的 $", "md": "`$x$` 不是公式。", "math": 0, "has": ["<code>$x$</code>"]},
    {"name": "围栏里只是提到 mermaid", "md": "```\nmermaid 图表写法:\n```", "has": ["<pre><code>"], "noloads": ["mermaid", "katex"]},
    {"name": "真的 mermaid 围栏才拉图库", "md": "```mermaid\nflowchart LR\n  A-->B\n```", "has": ["md-diagram"], "loads": ["mermaid"]},
    # ---- 其它扩展语法 ----
    {"name": "脚注", "md": "正文[^1]。\n\n[^1]: 说明。",
     "has": ["md-fn-ref", "<section class=\"md-footnotes\">", "<li id=\"fn-1\">", "md-fn-back"]},
    {"name": "悬空脚注保留字面量", "md": "没有定义的[^zz]。", "has": ["[^zz]"], "hasnt": ["md-fn-ref"]},
    {"name": "高亮", "md": "a ==重要== b", "has": ["<mark>重要</mark>"]},
    {"name": "上下标", "md": "H~2~O 与 x^2^", "has": ["H<sub>2</sub>O", "x<sup>2</sup>"]},
    {"name": "删除线仍走 GFM", "md": "~~删掉~~", "has": ["<del>删掉</del>"]},
    {"name": "图表占位(源码留在 DOM 里)", "md": "```mermaid\nflowchart LR\n  A[开始] --> B[结束]\n```",
     "has": ["<div class=\"md-diagram\">", "md-diagram-src", "A[开始] --&gt; B[结束]"]},
    {"name": "代码块保住 language class", "md": "```python\nx = 1\n```", "has": ["<pre><code class=\"language-python\">"]},
    {"name": "表格对齐标记透传", "md": "| a | b |\n|:--|--:|\n| 1 | 2 |", "has": ["align=\"left\"", "align=\"right\""]},
    {"name": "raw HTML 被转义", "md": "<img src=x onerror=alert(1)>", "hasnt": ["<img"], "has": ["&lt;img"]},
]


def _run_node(tmp_path, mode):
    script = (HARNESS
              .replace("__WEB__", json.dumps(WEB_JS))
              .replace("__MODE__", mode)
              .replace("__CASES__", json.dumps(CASES, ensure_ascii=False)))
    path = tmp_path / f"md_harness_{mode}.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, timeout=180)
    out = proc.stdout.decode("utf-8", "replace").strip()
    assert out, f"node 没有输出(stderr: {proc.stderr.decode('utf-8', 'replace')[:800]})"
    return json.loads(out)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """两趟(node 启动一次几十毫秒,两趟比按需反复起更省事)"""
    if not NODE:
        pytest.skip("未找到 node,跳过 Markdown 语法单测")
    tmp = tmp_path_factory.mktemp("md_syntax")
    return {mode: {r["name"]: r for r in _run_node(tmp, mode)} for mode in ("ready", "absent")}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_markdown_syntax(case, runs):
    """ready 趟看"被认成了什么",absent 趟看"什么时候去拉库" —— 两趟的断言职责刻意分开:
    absent 趟里 KaTeX 缺席,公式本来就该退回源码显示,拿公式数去要求它只会自欺。"""
    ready = runs["ready"].get(case["name"])
    assert ready is not None, f"ready 趟没有产出 {case['name']}"
    html = ready["html"]
    assert not html.startswith("!!THROW!!"), f"渲染抛错: {html[:200]}"
    if "math" in case:
        n = html.count('class="kxstub"')
        assert n == case["math"], f"公式数应为 {case['math']},实际 {n}: {html[:300]}"
    if "display" in case:
        n = html.count("katex-display-wrapper")
        assert n == case["display"], f"显示公式数应为 {case['display']},实际 {n}: {html[:300]}"
    for frag in case.get("has", []):
        assert frag in html, f"期望出现 {frag!r}: {html[:300]}"
    for frag in case.get("hasnt", []):
        assert frag not in html, f"不该出现 {frag!r}: {html[:300]}"

    absent = runs["absent"].get(case["name"])
    assert absent is not None, f"absent 趟没有产出 {case['name']}"
    assert not absent["html"].startswith("!!THROW!!"), f"库缺席时渲染抛错: {absent['html'][:200]}"
    joined = " ".join(absent["requests"])
    for lib in case.get("loads", []):
        assert lib in joined, f"该拉 {lib} 却没拉(请求: {absent['requests']})"
    for lib in case.get("noloads", []):
        assert lib not in joined, f"不该拉 {lib} 却拉了(请求: {absent['requests']})"


def test_anchor_click_routing(runs):
    """文内锚点的分流规则(单元级):拦文内锚点、放行站内路由。

    "拦下之后有没有真的跳到、有没有被踢出文档"由浏览器演练
    (`test_markdown_diagram.py`)覆盖;这里锁的是这条判断本身 ——
    写反了就是"点脚注跳出文档"或"站内路由链接失效"两种极端。
    """
    probe = (runs["ready"].get("__anchors__") or {}).get("anchors")
    assert probe, "node 桩没有产出锚点探针结果"
    assert probe["threw"] == "", f"派发点击时抛错: {probe['threw']}"
    assert int(probe["registered"]) >= 1, "markdown.js 没有往 document 上挂锚点委托"
    assert probe["anchorPrevented"] is True, \
        "文内锚点没被拦下 —— 会走 location.hash,被哈希路由当成路由跳走"
    assert probe["routePrevented"] is False, \
        "#/… 站内路由被吞掉了(文档里的站内链接会变成死链)"
