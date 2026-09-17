"""Markdown 扩展语法的浏览器演练:图表(Mermaid)的懒加载 / 渲染 / 离线,以及脚注、高亮、代码角标。

为什么必须真跑浏览器:这条链路每一环都只在浏览器里成立 —— 围栏检测、marked 扩展、懒注入 <script>、
mermaid 把配色烘焙进 SVG、以及"有没有偷偷请求外网"。接口测试和 node 冒烟都覆盖不到
"库真的被拉下来了、图真的画出来了、源码真的被藏起来了"。

一个页面跑两站(单次 Chrome 启动,省一半时间):
  纯文本文档(无围栏)→ 图表库**不该**被请求;顺带断言脚注 / 高亮 / 上下标 / 代码角标已生效
  含 ```mermaid 的文档 → 库被拉下来、<svg> 出现在 .md-diagram.rendered 里、源码被 CSS 隐藏
两站都断言:页面零跨源请求 —— 这是"内网离线可用"在浏览器侧的实证(前面几条只证明了资源可达)。
"""
import urllib.parse

import pytest

from _chrome import dump_page, error_trap_js, find_chrome, login_js, read_report, temp_page

pytestmark = pytest.mark.browser

PLAIN_MD = """纯文本预览演练:==高亮== 与 H~2~O 和 x^2^ 都该按扩展语法渲染。

价格 $5 和 $6 之间不是公式,公式 $E=mc^2$ 才是。

$$
\\int_0^1 x\\,dx
$$

```python
def f():
    return 1
```

脚注引用[^1]。作者手写的锚点 [跳到脚注](#fn-1) 也要留在本文档里。

[^1]: 脚注定义,带 *强调*。
""" + "\n\n".join(
    # 占位段落:把脚注顶到首屏之外,"跳转真的滚动了"才有可断言的前提(短文档里
    # 目标本来就在视口内,滚不滚动都看不出来)
    f"占位段落 {i}:把脚注推到首屏之外,好断言跳转确实滚动了。" for i in range(1, 31)
)

DIAGRAM_MD = """```mermaid
flowchart LR
  A[开始] --> B[结束]
```
"""

DIAGRAM_DRILL_INJECT = '''  <script>
    __LOGIN_JS__
    __ERRTRAP_JS__
    // 编辑器记住的模式:直接进预览态,省掉"点分段按钮"这一步(VIEWER 本来就是预览)
    try { localStorage.setItem('td:doc-mode', 'preview'); } catch (e) {}
  </script>
  <script src="/js/app.js"></script>
  <script>
  (function () {
    var OUT = {};
    var QS = new URLSearchParams(location.search);
    var PID = QS.get('pid') || '';
    var PLAIN = QS.get('plain') || '';
    var DIAGRAM = QS.get('diagram') || '';

    function preview() { return document.getElementById('md-preview'); }
    function resources() {
      try { return performance.getEntriesByType('resource') || []; } catch (e) { return []; }
    }
    function mermaidRequests() {
      return resources().filter(function (r) { return r.name.indexOf('mermaid') >= 0; }).length;
    }
    function crossOrigin() {
      return resources().filter(function (r) { return r.name.indexOf(location.origin) !== 0; })
        .map(function (r) { return r.name.slice(0, 60); });
    }
    function report() {
      var l = ['hash=' + location.hash, 'errors=' + (window.__errors.join(' | ') || 'none')];
      Object.keys(OUT).forEach(function (k) { l.push(k + '=' + OUT[k]); });
      var el = document.createElement('pre');
      el.id = 'sweep-out';
      el.textContent = l.join(String.fromCharCode(10));
      document.body.appendChild(el);
    }

    // 第一站:纯文本文档。图表库此时**不该**被请求(懒加载的门槛就是这里)
    function step1() {
      var box = preview();
      OUT.plainPreview = box ? 1 : 0;
      OUT.plainDiagram = document.querySelectorAll('#md-preview .md-diagram').length;
      OUT.plainMermaidRequests = mermaidRequests();
      OUT.plainCodeCopy = document.querySelectorAll('#md-preview .md-code-copy').length;
      OUT.plainCodeLang = document.querySelectorAll('#md-preview .md-code-lang').length;
      OUT.plainFootnotes = document.querySelectorAll('#md-preview .md-footnotes li').length;
      OUT.plainFnRef = document.querySelectorAll('#md-preview .md-fn-ref').length;
      OUT.plainMark = document.querySelectorAll('#md-preview mark').length;
      OUT.plainSub = document.querySelectorAll('#md-preview sub').length;
      // 上标要排掉脚注角标(它也是 <sup>)—— 只数 x^2^ 那一个
      OUT.plainSup = document.querySelectorAll('#md-preview sup:not(.md-fn-ref)').length;
      // 公式:KaTeX 的显示公式是 .katex-display > .katex 两层,所以要分开数
      // (行内只该有一个 $E=mc^2$;显示公式一个;价格文本 $5 和 $6 必须保持字面)
      var allMath = document.querySelectorAll('#md-preview .katex').length;
      var dispMath = document.querySelectorAll('#md-preview .katex-display-wrapper .katex').length;
      OUT.plainMathInline = allMath - dispMath;
      OUT.plainMathDisplay = dispMath;
      OUT.plainMathText = (preview() || { textContent: '' }).textContent.indexOf('价格 $5 和 $6 之间') >= 0 ? 1 : 0;
      var disp = document.querySelector('#md-preview .katex-display-wrapper');
      OUT.plainDisplayMath = document.querySelectorAll('#md-preview .katex-display-wrapper').length;
      // 包裹层必须是 span:段落中间的 $$…$$ 若用 div 包,HTML 解析器会把段落提前闭合、后半句被甩出去
      OUT.displayWrapperTag = disp ? disp.tagName : '(无)';
      stepAnchors();
    }
    // 文内锚点(脚注角标/回跳、作者写的 [文字](#锚点))必须由 markdown.js 接管滚动:
    // 全站是哈希路由,交给浏览器走 location.hash 会被解析成"一条路由" —— 症状不是"没跳到"
    // 而是"跳出了文档"(hash 变 #/,正在读的正文整个消失),所以断言分两层:hash 不变 + 真的滚到位。
    // 走一遍真实顺序:先点正文角标(该往下滚到脚注),再点脚注的 ↩(该滚回正文引用处)。
    function stepAnchors() {
      var ref = document.querySelector('#md-preview .md-fn-ref a');
      var back = document.querySelector('#md-preview .md-fn-back');
      var authored = null;
      document.querySelectorAll('#md-preview a[href="#fn-1"]').forEach(function (a) {
        if (!a.closest('.md-fn-ref')) authored = a;
      });
      var fnLi = document.getElementById('fn-1');
      var fnRef = document.querySelector('#md-preview .md-fn-ref');
      // 滚动容器不写死:从目标往上找第一个真正溢出的祖先(实测是 #editor-scroll,
      // 不是 window —— 量 window.scrollY 恒为 0,会得到"没滚动"的假象)
      function scrollerOf(el) {
        var n = el;
        while (n && n !== document.documentElement) {
          if (n.scrollHeight > n.clientHeight && n.clientHeight > 0) return n;
          n = n.parentElement;
        }
        return null;
      }
      function scrollTopOf(el) { var s = scrollerOf(el); return s ? Math.round(s.scrollTop) : -1; }
      function inView(el) {
        if (!el) return 0;
        var r = el.getBoundingClientRect();
        return r.top >= 0 && r.bottom <= window.innerHeight ? 1 : 0;
      }
      function clearHits() {
        document.querySelectorAll('.md-anchor-hit').forEach(function (e) { e.classList.remove('md-anchor-hit'); });
      }
      OUT.anchorHashBefore = location.hash;
      OUT.anchorBackHref = back ? back.getAttribute('href') : '(无回跳链接)';
      OUT.anchorAuthoredHref = authored ? authored.getAttribute('href') : '(无作者锚点链接)';
      OUT.innerHeight = window.innerHeight;
      OUT.refTopBefore = fnRef ? Math.round(fnRef.getBoundingClientRect().top) : '(无角标)';
      OUT.fnTopBefore = fnLi ? Math.round(fnLi.getBoundingClientRect().top) : '(无脚注)';
      OUT.scrollTopBefore = fnLi ? scrollTopOf(fnLi) : -1;
      if (!ref || !back) { OUT.stop = '缺少脚注链接'; out(OUT); return; }

      clearHits();
      ref.click();
      setTimeout(function () {
        OUT.anchorHashAfterRef = location.hash;
        OUT.previewAfterRef = preview() ? 1 : 0;
        OUT.hitAfterRef = document.querySelectorAll('#md-preview .md-anchor-hit').length;
        OUT.fnInViewAfterRef = inView(fnLi);
        OUT.scrollTopAfterRef = scrollTopOf(fnLi);
        clearHits();
        back.click();
        setTimeout(function () {
          OUT.anchorHashAfterBack = location.hash;
          OUT.previewAfterBack = preview() ? 1 : 0;
          OUT.hitAfterBack = document.querySelectorAll('#md-preview .md-anchor-hit').length;
          OUT.refInViewAfterBack = inView(fnRef);
          OUT.scrollTopAfterBack = scrollTopOf(fnLi);
          if (authored) authored.click();
          setTimeout(function () {
            OUT.anchorHashAfterAuthored = location.hash;
            OUT.previewAfterAuthored = preview() ? 1 : 0;
            step2();
          }, 400);
        }, 400);
      }, 400);
    }
    function step2() {
      location.hash = '#/p/' + PID + '/docs/' + DIAGRAM;
      window.dispatchEvent(new HashChangeEvent('hashchange'));
      setTimeout(step3, 6000);
    }
    function step3() {
      OUT.diagramPreview = preview() ? 1 : 0;
      OUT.diagramCount = document.querySelectorAll('#md-preview .md-diagram').length;
      OUT.diagramRendered = document.querySelectorAll('#md-preview .md-diagram.rendered').length;
      OUT.diagramSvg = document.querySelectorAll('#md-preview .md-diagram-view svg').length;
      OUT.diagramFailed = document.querySelectorAll('#md-preview .md-diagram.failed').length;
      // 源码是不是真的被藏起来了(靠的是 CSS 规则,不是从 DOM 里删掉 —— 主题切换重画要用它)
      var src = document.querySelector('#md-preview .md-diagram.rendered .md-diagram-src');
      OUT.diagramSrcDisplay = src ? getComputedStyle(src).display : '(未找到源码)';
      OUT.diagramCodeCopy = document.querySelectorAll('#md-preview .md-code-copy').length;
      OUT.mermaidRequests = mermaidRequests();
      // 切主题:SVG 的配色是渲染时烘焙进去的,明暗变了必须重画。
      // 判据用 svg 的 id —— 每次渲染都取新的自增 id,id 变了就说明真的重画过
      var svg = document.querySelector('#md-preview .md-diagram-view svg');
      OUT.svgIdBefore = svg ? svg.id : '(无 svg)';
      Theme.setMode('dark');
      setTimeout(step4, 4000);
    }
    function step4() {
      OUT.themeAfter = document.documentElement.dataset.theme;
      var svg = document.querySelector('#md-preview .md-diagram-view svg');
      OUT.svgIdAfter = svg ? svg.id : '(无 svg)';
      OUT.diagramSvgAfterTheme = document.querySelectorAll('#md-preview .md-diagram-view svg').length;
      OUT.diagramFailedAfterTheme = document.querySelectorAll('#md-preview .md-diagram.failed').length;
      // 图表配色必须跟随站点令牌(mermaid 自带色板早就是淡紫,与 6 个种子色都格格不入)。
      // 令牌值转成 rgb 再比对:两边都由浏览器算,省掉十六进制/色彩空间的换算
      var probe = document.createElement('div');
      probe.style.backgroundColor =
        getComputedStyle(document.documentElement).getPropertyValue('--md-primary-container');
      document.body.appendChild(probe);
      OUT.tokenContainerRgb = getComputedStyle(probe).backgroundColor;
      var node = document.querySelector('#md-preview .md-diagram-view .node rect, #md-preview .md-diagram-view rect.basic');
      OUT.nodeFillRgb = node ? getComputedStyle(node).fill : '(未找到节点)';
      probe.remove();
      OUT.crossOrigin = crossOrigin().join(',') || 'none';
      report();
    }
    window.addEventListener('load', function () {
      setTimeout(function () {
        location.hash = '#/p/' + PID + '/docs/' + PLAIN;
        window.dispatchEvent(new HashChangeEvent('hashchange'));
        setTimeout(step1, 3000);
      }, 150);
    });
  })();
  </script>
'''


def _make_doc(admin, pid, title, content):
    d = admin.post(f"/api/projects/{pid}/docs", {"title": title})
    assert d.status == 200, f"建文档失败: {d.data}"
    did = d.data["id"]
    r = admin.put(f"/api/docs/{did}/content", {"content": content, "baseVersion": 0})
    assert r.status == 200, f"写正文失败: {r.data}"
    return did


def test_markdown_extensions_in_browser(base_url, admin):
    """纯文本 → 含图文档:懒加载门槛、图表渲染、零跨源请求,一次跑完。"""
    chrome = find_chrome()
    if not chrome:
        pytest.skip("未找到 Chrome/Edge,跳过 Markdown 浏览器演练")

    pid = admin.post("/api/projects", {"name": "Markdown 演练项目", "description": "d"}).data["id"]
    try:
        plain = _make_doc(admin, pid, "纯文本文档", PLAIN_MD)
        diagram = _make_doc(admin, pid, "含图表文档", DIAGRAM_MD)
        inject = DIAGRAM_DRILL_INJECT.replace("__LOGIN_JS__", login_js()) \
                                      .replace("__ERRTRAP_JS__", error_trap_js())
        qs = "pid={}&plain={}&diagram={}".format(
            urllib.parse.quote(str(pid), safe=""), plain, diagram)
        with temp_page("_md_diagram.html", inject) as page_name:
            info = read_report(dump_page(chrome, base_url, page_name, qs, budget=40000))
    finally:
        admin.delete(f"/api/projects/{pid}")

    assert info is not None, "未产出演练结果(页面可能整块崩了)"
    assert info.get("errors", "?") == "none", f"演练页 JS 错误: {info.get('errors')}"

    # --- 纯文本文档:扩展语法生效,而图表库一次都没被请求 ---
    assert info.get("plainPreview") == "1", f"纯文本文档没有进到预览态: {info}"
    assert info.get("plainDiagram") == "0", "无 mermaid 围栏的文档不该出现图表占位"
    assert info.get("plainMermaidRequests") == "0", \
        f"没有围栏却拉取了 mermaid({info.get('plainMermaidRequests')} 次请求)—— 懒加载失效"
    assert info.get("plainFootnotes") == "1", "脚注定义没有渲染成文末脚注区"
    assert info.get("plainFnRef") == "1", "脚注引用没有渲染成角标"
    assert info.get("plainMark") == "1", "==高亮== 没有生效"
    assert info.get("plainSub") == "1", "~下标~ 没有生效"
    assert info.get("plainSup") == "1", "x^2^ 的 ^上标^ 没有生效"
    assert info.get("plainMathInline") == "1", \
        f"行内公式渲染数不对(应只有 $E=mc^2$ 一个,价格文本不该被当公式): {info.get('plainMathInline')}"
    assert info.get("plainMathDisplay") == "1", \
        f"显示公式渲染数不对: {info.get('plainMathDisplay')}"
    assert info.get("plainMathText") == "1", \
        f"价格文本被公式规则吃掉了,预览里应原样保留 '价格 $5 和 $6 之间': " \
        f"{info.get('plainMathText')}"
    assert info.get("plainDisplayMath") == "1", "块级 $$…$$ 没有渲染成显示公式"
    assert info.get("displayWrapperTag") == "SPAN", \
        f"显示公式包裹层应为 span(div 会把段落提前闭合): {info.get('displayWrapperTag')}"
    assert info.get("plainCodeCopy") == "1", "代码块缺少复制按钮"
    assert info.get("plainCodeLang") == "1", "代码块缺少语言角标"

    # --- 文内锚点:三层断言 = 链接没被改写 + hash 没变(没被路由带走) + 真的滚到了位 ---
    assert info.get("anchorBackHref") == "#fnref-1-1", \
        f"回跳链接的指向变了: {info.get('anchorBackHref')}"
    assert info.get("anchorAuthoredHref") == "#fn-1", \
        f"作者手写的锚点链接被改写了(应原样保留): {info.get('anchorAuthoredHref')}"
    # 夹具自检:角标在首屏内、脚注在首屏外,滚动才有可观察的位移(整页 ~2400px,视口 ~485px)
    assert 0 <= int(info.get("refTopBefore") or -1) <= int(info.get("innerHeight") or 0), \
        f"夹具失效:正文角标一开始不在首屏里(top={info.get('refTopBefore')})"
    assert int(info.get("fnTopBefore") or 0) > int(info.get("innerHeight") or 0), \
        f"夹具失效:脚注一开始就在首屏里,滚动断言形同虚设(top={info.get('fnTopBefore')})"
    # ① 点角标 → 下滚到脚注;② 点 ↩ → 滚回正文引用处。两处都必须留在本文档
    assert info.get("anchorHashAfterRef") == info.get("anchorHashBefore"), \
        f"点脚注角标后路由被改写(用户会被踢出文档): " \
        f"{info.get('anchorHashBefore')} -> {info.get('anchorHashAfterRef')}"
    assert info.get("previewAfterRef") == "1", "点脚注角标后正文消失了"
    assert int(info.get("scrollTopAfterRef") or 0) > int(info.get("scrollTopBefore") or 0), \
        f"点角标后滚动容器没动 —— 没被踢出去,但也没跳到脚注: " \
        f"{info.get('scrollTopBefore')} -> {info.get('scrollTopAfterRef')}"
    assert info.get("fnInViewAfterRef") == "1", "点角标后脚注没有进入视口"
    assert info.get("hitAfterRef") == "1", "角标落点没有高亮(.md-anchor-hit 没加上)"
    assert info.get("anchorHashAfterBack") == info.get("anchorHashBefore"), \
        f"点回跳后路由被改写: -> {info.get('anchorHashAfterBack')}"
    assert info.get("previewAfterBack") == "1", "点回跳后正文整个消失了(被哈希路由带走)"
    assert int(info.get("scrollTopAfterBack") or 0) < int(info.get("scrollTopAfterRef") or 0), \
        f"点回跳没有往回滚(引用在正文,位置应在脚注之上): " \
        f"{info.get('scrollTopAfterRef')} -> {info.get('scrollTopAfterBack')}"
    assert info.get("refInViewAfterBack") == "1", "点回跳后正文引用处没有回到视口"
    assert info.get("hitAfterBack") == "1", "回跳落点没有高亮"
    assert info.get("anchorHashAfterAuthored") == info.get("anchorHashBefore"), \
        f"作者写的 #锚点把路由改写了: -> {info.get('anchorHashAfterAuthored')}"
    assert info.get("previewAfterAuthored") == "1", "作者手写的锚点点完正文消失了"

    # --- 含图文档:库被拉下来、SVG 画出来、源码被藏起来 ---
    assert info.get("diagramPreview") == "1", "含图文档没有进到预览态"
    assert int(info.get("mermaidRequests") or 0) >= 1, "到了含 mermaid 围栏的文档却没拉取图表库"
    assert info.get("diagramCount") == "1", f"图表占位数量异常: {info.get('diagramCount')}"
    assert info.get("diagramFailed") == "0", \
        f"图表渲染失败(会退回源码显示): {info.get('diagramFailed')} 处"
    assert info.get("diagramSvg") == "1", "图表没有渲染出 <svg>"
    assert info.get("diagramSrcDisplay") == "none", \
        f"渲染成功后源码应被 CSS 隐藏(实际 display={info.get('diagramSrcDisplay')})"
    assert info.get("diagramCodeCopy") == "0", "图表源码不该被当成普通代码块加角标"

    # --- 离线:两站都不许有跨源请求(资源可达 ≠ 真的没往外跑) ---
    assert info.get("crossOrigin") == "none", \
        f"页面出现了跨源请求(内网离线部署会失效): {info.get('crossOrigin')}"

    # --- 切深色:图表必须重画(配色烘焙在 SVG 里,不会自己跟随主题) ---
    assert info.get("themeAfter") == "dark", f"主题没切成深色: {info.get('themeAfter')}"
    assert info.get("diagramSvgAfterTheme") == "1", "切主题后图表没了"
    assert info.get("diagramFailedAfterTheme") == "0", "切主题后图表渲染失败"
    assert info.get("svgIdBefore") != info.get("svgIdAfter"), \
        f"切主题后图表没有重画(SVG 未变): {info.get('svgIdBefore')} -> {info.get('svgIdAfter')}"
    # 先确认真的量到了值(否则下面那条"相等"可能是两边都取空的假绿)
    assert (info.get("nodeFillRgb") or "").startswith("rgb("), \
        f"没取到图表节点的填充色: {info.get('nodeFillRgb')}"
    assert (info.get("tokenContainerRgb") or "").startswith("rgb("), \
        f"没取到 --md-primary-container 的计算值: {info.get('tokenContainerRgb')}"
    assert info.get("nodeFillRgb") == info.get("tokenContainerRgb"), \
        f"图表节点没跟上站点令牌(应为 --md-primary-container): " \
        f"节点 {info.get('nodeFillRgb')} vs 令牌 {info.get('tokenContainerRgb')}"
