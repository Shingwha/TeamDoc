/* markdown.js — 全站唯一 Markdown 渲染路径(编辑预览 / 历史版本预览 / 云空间 md 预览 / @引用浮层共用)。
   栈:marked 11.1.1 + Prism 1.29.0(autoloader 按需拉语言包)+ KaTeX 0.16.9 + Mermaid 11.12.0。
   后两者都是**检测到才懒加载**(公式看 $,图表看 ```mermaid 围栏),全部本地 vendor,纯内网部署无外网依赖。
   自定义语法:$$..$$ / $..$ 公式、[^脚注]、==高亮==、~下标~ 与 ^上标^、```mermaid 图表;代码块带语言角标与复制按钮。
   **语法规格与逐条用例在 `lite/MARKDOWN.md` 与 `lite/tests/test_markdown_render.py`**:这里改规则,
   那两处要一起改(它们是对外承诺)。
   安全:raw HTML 一律转义(多人协作防 XSS),只支持纯 Markdown 语义;teamdoc:// 引用链接输出为普通 <a>,
   由 editor.css 的 chip 样式与 doceditor.js 的全局点击路由接管。 */
(function () {
  'use strict';

  var KATEX_BASE = '/vendor/katex';
  var MERMAID_BASE = '/vendor/mermaid';
  var PRISM_COMPONENTS = '/vendor/prism/components/';
  var katexPromise = null;
  var mermaidPromise = null;

  // marked 未加载(资源缺失)时降级为纯文本预览,不让预览区整块失效
  if (!window.marked) {
    console.error('marked 未加载:预览将以纯文本显示(/vendor/marked/marked.min.js 是否可访问?)');
    window.MdRender = {
      render: function (md) { return Promise.resolve('<pre class="md-fallback">' + UI.esc(md || '') + '</pre>'); },
      mount: function (el, md) { el.innerHTML = '<pre class="md-fallback">' + UI.esc(md || '') + '</pre>'; return Promise.resolve(); },
    };
    return;
  }

  function loadKatex() {
    if (window.katex) return Promise.resolve();
    if (!katexPromise) {
      katexPromise = new Promise(function (resolve, reject) {
        var css = document.createElement('link');
        css.rel = 'stylesheet';
        css.href = KATEX_BASE + '/katex.min.css';
        document.head.appendChild(css);
        var s = document.createElement('script');
        s.src = KATEX_BASE + '/katex.min.js';
        s.onload = function () { resolve(); };
        s.onerror = function () { katexPromise = null; reject(new Error('katex load failed')); };
        document.head.appendChild(s);
      });
    }
    return katexPromise;
  }

  function loadMermaid() {
    if (window.mermaid) return Promise.resolve();
    if (!mermaidPromise) {
      mermaidPromise = new Promise(function (resolve, reject) {
        // 图表样式由 mermaid 在每张 SVG 内部自带(没有独立的 css 文件);单文件 UMD,不依赖别的 chunk
        var s = document.createElement('script');
        s.src = MERMAID_BASE + '/mermaid.min.js';
        s.onload = function () { resolve(); };
        s.onerror = function () { mermaidPromise = null; reject(new Error('mermaid load failed')); };
        document.head.appendChild(s);
      });
    }
    return mermaidPromise;
  }

  function renderMath(tex, displayMode) {
    if (!window.katex) return UI.esc(displayMode ? '$$' + tex + '$$' : '$' + tex + '$'); // 懒加载失败兜底:显示源码
    var html = katex.renderToString(tex, { displayMode: displayMode, throwOnError: false });
    // 包裹层是 span 而不是 div:行内公式、以及"段落中间的 $$…$$"都可能落在 <p> 里,
    // 塞 div 进去 HTML 解析器会把段落提前闭合,后面那半句文字被甩到段落外面(block 由 CSS 给)
    return displayMode ? '<span class="katex-display-wrapper">' + html + '</span>' : html;
  }

  /* ---------- 源码探测:三处"扫一遍源码找特定写法"共用同一套围栏规则 ----------
     visit(line, i, offset, ctx) 返回非 undefined 即结束扫描,并成为 scanSource 的结果。
     ctx: { fenceLine, opener, closer, inside } —— opener 是行尾的语言标记(非围栏行是 null,
     空标记的空围栏是 '')。围栏规则只此一份:块级公式起点、有没有公式、有没有 mermaid 围栏
     都从它出发,不再各写一套(以前三处各有一份,三份的判断迟早不一致)。 */
  function scanSource(src, visit) {
    var fence = null;
    var offset = 0;
    var lines = String(src == null ? '' : src).split('\n');
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var ctx = { fenceLine: false, opener: null, closer: false, inside: false };
      var m = /^\s*(`{3,}|~{3,})[ \t]*([^\s`~]*)/.exec(line);
      if (m) {
        ctx.fenceLine = true;
        var ch = m[1].charAt(0);
        if (!fence) { fence = ch; ctx.opener = m[2] || ''; }
        else if (ch === fence && !m[2]) { fence = null; ctx.closer = true; }  // 闭合行:同种字符且不带语言标记
        else ctx.inside = true;   // 围栏里出现"别种字符的围栏行":算内容
      } else if (fence) {
        ctx.inside = true;
      }
      var r = visit(line, i, offset, ctx);
      if (r !== undefined) return r;
      offset += line.length + 1;
    }
    return undefined;
  }

  // 块级公式的候选起点:行首的 $$(跳过围栏代码块内部),返回它在源码里的字符下标(-1 = 没有)。
  // 不能用 src.indexOf('$$'):行内代码里的 `$$`(如文档中举例说明的写法)会被当成候选起点,
  // 导致 marked 从代码 span 中间切断段落,并让后续所有 $$ 配对整体错位。
  function firstBlockMathIndex(src) {
    var hit = scanSource(src, function (line, i, offset, ctx) {
      if (!ctx.fenceLine && !ctx.inside && /^\$\$/.test(line)) return offset;
    });
    return hit === undefined ? -1 : hit;
  }

  /* ---------- 公式 ----------
     行内 `$…$` 三条要求(唯一判定在 inlineMath 的 tokenizer 里):
       ① 开 $ 后面不紧贴空白(也不是 $)
       ② 闭 $ 前面不紧贴空白(也不是 $)
       ③ 闭 $ 后面不是数字
     ①② 挡住中文里最常见的美元金额 —— "价格 $5 和 $6 之间"、"共 $3 两项"(老实现是
     /^\$([^$]+?)\$/,会把 "5 和 " 当公式,是挂了很久的误判);③ 挡住 US$5 … $6 这种
     前一个 $ 贴着字母、后一个 $ 后面紧跟数字的写法。内容不跨行、不能为空。
     显示公式 $$…$$ 走另一套:独占一行时(块级)内容可缩进跨行、不设限制;写在段落中间的
     (inlineDisplayMath)同样要求内侧不贴空白 —— 它要和散文抢地盘,"成本 $$ 和 $$ 之间"不该被当公式。 */
  var blockMath = {
    name: 'blockMath',
    level: 'block',
    start: function (src) { return firstBlockMathIndex(src); },
    tokenizer: function (src) {
      // 开闭 $$ 各自独占一行,内容不跨空行 —— 即使起点判断有偏差也不会吞掉后续段落,
      // 失败模式从"整体错位"降级为"退回普通文本"
      var cap = /^\$\$[ \t]*\n([\s\S]+?)\n[ \t]*\$\$[ \t]*(?:\n+|$)/.exec(src);
      if (cap) return { type: 'blockMath', raw: cap[0], text: cap[1].trim() };
      // 兼容单行写法 $$x$$(内容不含换行,不可能跨界吞噬)
      cap = /^\$\$([^\n$]+?)\$\$/.exec(src);
      if (cap) return { type: 'blockMath', raw: cap[0], text: cap[1].trim() };
    },
    renderer: function (token) { return renderMath(token.text, true); },
  };
  var inlineMath = {
    name: 'inlineMath',
    level: 'inline',
    start: function (src) { return src.indexOf('$'); },
    tokenizer: function (src) {
      var cap = /^\$(?![\s$])((?:\\\$|[^$\n])*?[^\s$])\$(?!\d)/.exec(src);
      if (cap) return { type: 'inlineMath', raw: cap[0], text: cap[1] };   // 两侧不贴空白 ⇒ 无需 trim
    },
    renderer: function (token) { return renderMath(token.text, false); },
  };
  var inlineDisplayMath = {
    name: 'inlineDisplayMath',
    level: 'inline',
    start: function (src) { var i = src.indexOf('$$'); return i < 0 ? undefined : i; },
    tokenizer: function (src) {
      // 段落中间的 $$…$$:与行内公式同一套"内侧不贴空白"要求 —— 它要和散文抢地盘,
      // 松一寸就有"成本 $$ 和 $$ 之间"这种误判。独占一行的块级写法不要求(那种写法本来就带缩进/换行)
      var cap = /^\$\$(?![\s$])([\s\S]*?[^\s$])\$\$/.exec(src);
      if (cap) return { type: 'inlineDisplayMath', raw: cap[0], text: cap[1] };
    },
    renderer: function (token) { return renderMath(token.text, true); },
  };

  /* ---------- 图表(Mermaid):```mermaid 围栏 → <div class="md-diagram"> ----------
     源码**留在 DOM 里**(渲染成功只是被 CSS 隐藏),于是三件事都不需要额外的登记表:
     主题切换后重画、渲染失败退回源码、用户想看原始写法时它就在那里。 */
  var mermaidSeq = 0;        // mermaid.render 的 id 必须唯一:同页多图(或多处预览同时挂载)并发渲染时同 id 会互相污染
  var mermaidSig = null;     // 最近一次 initialize 生效的"主题+主色"签名,用来判断配置是否真的变了

  /** 主题签名:明暗 + 主色值。图表配色由二者共同决定,任一变化都得重画 */
  function diagramSignature() {
    var primary = '';
    try {
      primary = (getComputedStyle(document.documentElement).getPropertyValue('--md-primary') || '').trim();
    } catch (e) { /* 取不到就只按明暗区分 */ }
    return ((window.Theme && Theme.isDark()) ? 'dark' : 'light') + '|' + primary;
  }

  /** 图表配色走站点令牌 —— 与 Prism 高亮同一立场:不引第三方主题包,颜色只有一个来源。
      (mermaid 自带的 default/dark 主题是它自己的淡紫色板,放进 6 个种子色的界面里格格不入) */
  function mermaidVars() {
    var cs = getComputedStyle(document.documentElement);
    function v(name, fallback) {
      var x = (cs.getPropertyValue(name) || '').trim();
      return x || fallback;   // 令牌被改名/读不到时退回 mermaid 的默认色,不至于整块崩
    }
    return {
      primaryColor: v('--md-primary-container', '#dbe4ff'),
      mainBkg: v('--md-primary-container', '#dbe4ff'),
      primaryTextColor: v('--md-on-primary-container', '#10297e'),
      primaryBorderColor: v('--md-primary', '#3370ff'),
      lineColor: v('--md-outline', '#8f959e'),
      textColor: v('--md-on-surface', '#1f2329'),
      secondaryColor: v('--md-surface-container-high', '#e9eaee'),
      tertiaryColor: v('--md-surface-container-low', '#f6f7f9'),
      edgeLabelBackground: v('--md-surface-container-high', '#e9eaee'),
      clusterBkg: v('--md-surface-container-low', '#f6f7f9'),
      clusterBorder: v('--md-outline-variant', '#dee0e3'),
      fontFamily: document.body ? getComputedStyle(document.body).fontFamily : 'sans-serif',
    };
  }

  /** 按当前主题配置 mermaid;返回是否重新配置过(配置变了就意味着已画好的图要重画) */
  function initMermaid() {
    var sig = diagramSignature();
    if (sig === mermaidSig) return false;
    mermaidSig = sig;
    window.mermaid.initialize({
      startOnLoad: false,           // 只渲染显式交给它的节点,不让它扫全页(页面里同时可能有多个预览)
      securityLevel: 'strict',      // 标签经 DOMPurify 清洗、click 交互禁用 —— 与"raw HTML 一律转义"同一立场
      theme: 'base',                // 色板下面自己给(见 mermaidVars)
      themeVariables: mermaidVars(),
      suppressErrorRendering: true, // 语法错误时不往页面塞半成品 SVG,由下面统一显示源码 + 原因
    });
    return true;
  }

  /** 渲染一个图表占位:成功 → svg 进 view 层;失败 → 保留源码并写明原因 */
  function renderOneDiagram(box) {
    var srcEl = box.querySelector('.md-diagram-src');
    var view = box.querySelector('.md-diagram-view');
    if (!srcEl || !view) return Promise.resolve();
    var code = srcEl.textContent || '';
    var old = box.querySelector('.md-diagram-note');
    if (old) old.remove();
    box.classList.remove('failed');
    if (!code.trim()) { box.classList.remove('rendered'); view.innerHTML = ''; return Promise.resolve(); }
    return window.mermaid.render('td-mmd-' + (++mermaidSeq), code).then(function (r) {
      view.innerHTML = r.svg;
      box.classList.add('rendered');
      if (r.bindFunctions) r.bindFunctions(view);
    }).catch(function (e) {
      view.innerHTML = '';           // 不留半成品
      box.classList.remove('rendered');
      box.classList.add('failed');
      var note = document.createElement('p');
      note.className = 'md-diagram-note';
      note.textContent = '图表未渲染:' + ((e && e.message) || '语法错误') + '。下面是源码,修正后再看预览。';
      box.insertBefore(note, srcEl);
    });
  }

  /** 渲染 root 内的全部图表占位(首次挂载与主题变化后重画走同一条路径) */
  function renderDiagrams(root) {
    if (!root) return Promise.resolve();
    var boxes = root.querySelectorAll('.md-diagram');
    if (!boxes.length) return Promise.resolve();
    // 库可能还没加载:提前量靠 render() 的围栏检测,**正确性由这里兜底**(漏检也不会永远显示源码)
    var ready = window.mermaid ? Promise.resolve() : loadMermaid().catch(function () { /* 失败则整段退回源码 */ });
    return ready.then(function () {
      if (!window.mermaid) return;
      var reinit = initMermaid();
      var jobs = [];
      for (var i = 0; i < boxes.length; i++) {
        // 画过且配置没变就跳过:mount 会被反复调用(预览态随远端更新重渲染)
        if (!reinit && boxes[i].classList.contains('rendered')) continue;
        jobs.push(renderOneDiagram(boxes[i]));
      }
      return Promise.all(jobs);
    });
  }

  // 主题/种子色切换:SVG 的配色在渲染时就烘焙进文件了(不像 CSS 那样自动跟随),签名变了就整批重画。
  // 延后一帧,让主题先落到界面上再干活。
  if (window.Theme && Theme.onChange) {
    Theme.onChange(function () {
      if (!window.mermaid || diagramSignature() === mermaidSig) return;
      setTimeout(function () { renderDiagrams(document); }, 0);
    });
  }

  /* ---------- 脚注 [^1] ----------
     定义与引用天然分两趟:marked 先把块级 token 全部走完(定义收集齐),再走行内 —— 所以 renderer 里
     可以直接判"引用了不存在的脚注"并退回字面文本。解析全程同步,模块级的收集器不会串趟。 */
  var fnDefs = {};    // label → 定义正文
  var fnOrder = [];   // 首次引用顺序(编号按引用先后定,定义写在正文哪个位置都不影响)
  var fnNo = {};      // label → 编号
  var fnSeen = {};    // label → 已出现的引用次数(同一脚注被引多次时,锚点 id 不能重复)

  function footnoteReset() { fnDefs = {}; fnOrder = []; fnNo = {}; fnSeen = {}; }

  var footnoteDef = {
    name: 'footnoteDef',
    level: 'block',
    // 刻意**不给** start:[^1] 引用可能出现在段落中间,而 start 是"下一个块级候选起点"的提示 ——
    // 把段中的 [^ 报上去,marked 会把这一段从那里切开再拼回,并在拼接处塞一个多余的换行
    // (实测表现为脚注角标被挤到下一行)。没有 start 只是少一次快路径优化,行为完全一致。
    tokenizer: function (src) {
      // [^label]: 正文;续行需缩进 ≥2 空格。行尾空行一并吃掉,不留下空段落
      var cap = /^\[\^([^\]\s]+)\]:[ \t]*((?:[^\n]*(?:\n[ \t]{2,}[^\n]*)*)?)(?:\n+|$)/.exec(src);
      if (!cap) return;
      fnDefs[cap[1]] = cap[2].replace(/\n[ \t]{2,}/g, '\n').trim();
      return { type: 'footnoteDef', raw: cap[0] };
    },
    renderer: function () { return ''; },   // 定义只在文末脚注区出现
  };

  var footnoteRef = {
    name: 'footnoteRef',
    level: 'inline',
    start: function (src) { var i = src.indexOf('[^'); return i < 0 ? undefined : i; },
    tokenizer: function (src) {
      var cap = /^\[\^([^\]\s]+)\]/.exec(src);
      if (!cap) return;
      var label = cap[1];
      if (!fnDefs[label]) return;                                // 没有对应定义:当普通文本,不留悬空角标
      if (!fnNo[label]) fnNo[label] = fnOrder.push(label);        // 编号按首次引用顺序(push 返回新长度)
      fnSeen[label] = (fnSeen[label] || 0) + 1;
      return { type: 'footnoteRef', raw: cap[0], label: label, seq: fnSeen[label] };
    },
    renderer: function (token) {
      var n = fnNo[token.label];
      return '<sup class="md-fn-ref"><a id="fnref-' + n + '-' + token.seq + '" href="#fn-' + n +
        '" title="跳转到脚注 ' + n + '">' + n + '</a></sup>';
    },
  };

  /** 文末脚注区。定义里可能又引用了别的脚注,故遍历时实时取 fnOrder.length(新编号顺带补上) */
  function footnoteSection() {
    var items = '';
    var done = {};
    for (var i = 0; i < fnOrder.length; i++) {
      var label = fnOrder[i];
      if (done[label]) continue;
      done[label] = 1;
      var n = fnNo[label];
      var refs = fnSeen[label] || 1;
      // 同一脚注被引多次时,回跳要能分别回到每一处(只给一个 ↩ 的话,后几处引用回不去)
      var backs = '';
      for (var k = 1; k <= refs; k++) {
        backs += '<a class="md-fn-back" href="#fnref-' + n + '-' + k + '" title="回到正文第 ' + k +
          ' 处引用">↩' + (refs > 1 ? '<sup>' + k + '</sup>' : '') + '</a>';
      }
      items += '<li id="fn-' + n + '">' + marked.parseInline(fnDefs[label] || '') + backs + '</li>';
    }
    return items ? '<section class="md-footnotes"><hr><ol>' + items + '</ol></section>' : '';
  }

  /* ---------- ==高亮== / ~下标~ / ^上标^ ----------
     三者都要求"分隔符内侧不紧贴空白或同类符号",把 a == b、3 ~ 5 这类正常文本挡在外面;
     ~~删除线~~ 不受影响:它的首个 ~ 后面还是 ~,这里的 sub 规则直接不匹配,落回 GFM 处理。 */
  var markExt = {
    name: 'mark',
    level: 'inline',
    start: function (src) { var i = src.indexOf('=='); return i < 0 ? undefined : i; },
    tokenizer: function (src) {
      var cap = /^==(?![=\s])([^\n]*?[^\s=])==(?!=)/.exec(src);
      if (cap) return { type: 'mark', raw: cap[0], tokens: this.lexer.inlineTokens(cap[1]) };
    },
    renderer: function (token) { return '<mark>' + this.parser.parseInline(token.tokens) + '</mark>'; },
  };
  var subExt = {
    name: 'subscript',
    level: 'inline',
    start: function (src) { var i = src.indexOf('~'); return i < 0 ? undefined : i; },
    tokenizer: function (src) {
      var cap = /^~(?![~\s])([^\s\n~]+)~(?!~)/.exec(src);
      if (cap) return { type: 'subscript', raw: cap[0], tokens: this.lexer.inlineTokens(cap[1]) };
    },
    renderer: function (token) { return '<sub>' + this.parser.parseInline(token.tokens) + '</sub>'; },
  };
  var supExt = {
    name: 'superscript',
    level: 'inline',
    start: function (src) { var i = src.indexOf('^'); return i < 0 ? undefined : i; },
    tokenizer: function (src) {
      var cap = /^\^(?![\s^])([^\s\n^]+)\^(?!\^)/.exec(src);
      if (cap) return { type: 'superscript', raw: cap[0], tokens: this.lexer.inlineTokens(cap[1]) };
    },
    renderer: function (token) { return '<sup>' + this.parser.parseInline(token.tokens) + '</sup>'; },
  };

  var renderer = new marked.Renderer();
  // raw HTML 全部转义。注意 marked v11 的 renderer.html 收的是**原始字符串**
  // (不是 token 对象)—— 签名变了会让正文里的 HTML 整段消失,test_markdown_render
  // 的"raw HTML 被转义"用例盯着这一点。
  renderer.html = function (html) { return UI.esc(html || ''); };
  // 所有链接新标签页打开(阅读型应用惯例;teamdoc:// 由 ref.js 的全局点击路由接管,target 不影响)。
  // 协议白名单:marked 只对 href 做 encodeURI,不过滤 javascript:/data: ——
  // 于是 [x](javascript:...) 会渲染出可点的脚本链接。现代浏览器配合 target=_blank
  // 通常不执行,但老内核/国产浏览器不保证,属纵深防御缺口,故设白名单。
  var SAFE_URL = /^(https?:|mailto:|teamdoc:|\/|#)/i;
  renderer.link = function (href, title, text) {
    if (!SAFE_URL.test(String(href == null ? '' : href))) return text;  // 不给链接,保留文字
    return '<a href="' + UI.esc(href) + '"' + (title ? ' title="' + UI.esc(title) + '"' : '') +
      ' target="_blank" rel="noopener noreferrer">' + text + '</a>';
  };
  // 代码块:与 marked 11 默认实现逐句对齐(renderer.code 是位置参数 (code, lang, escaped)),
  // 只把 mermaid 围栏换成图表占位。不照抄默认实现就会丢掉 language-xxx class —— Prism 高亮与它的
  // autoloader(按 class 里的语言按需拉包)全靠它,表现为"所有代码块都不高亮"。
  renderer.code = function (code, lang, escaped) {
    var name = String(lang == null ? '' : lang).trim().split(/\s+/)[0] || '';
    if (/^mermaid$/i.test(name)) {
      return '<div class="md-diagram"><pre class="md-diagram-src">' + UI.esc(code) +
        '</pre><div class="md-diagram-view"></div></div>\n';
    }
    var text = String(code == null ? '' : code).replace(/\n$/, '') + '\n';
    var body = escaped ? text : UI.esc(text);   // escaped:上游已转义过(本站在 html 渲染器里统一转义)
    return name
      ? '<pre><code class="language-' + UI.esc(name) + '">' + body + '</code></pre>\n'
      : '<pre><code>' + body + '</code></pre>\n';
  };
  marked.use({
    renderer: renderer,
    extensions: [blockMath, inlineMath, inlineDisplayMath, footnoteDef, footnoteRef, markExt, subExt, supExt],
  });
  marked.setOptions({ breaks: true, gfm: true });

  if (window.Prism && Prism.plugins && Prism.plugins.autoloader) {
    Prism.plugins.autoloader.languages_path = PRISM_COMPONENTS;
  }

  /* 首个 ```mermaid / ~~~mermaid 开围栏行(大小写不敏感),返回行下标;没有则 undefined。
     这只是提前量:命中就先拉库,预览挂上时能立刻画。真正的判定在 renderDiagrams(按 DOM 里的占位)。
     围栏内部的 "mermaid" 字样不算 —— 文档里讲 mermaid 写法时不该顺带拉 2.6MB 的库。 */
  function mermaidFenceLine(src) {
    return scanSource(src, function (line, i, offset, ctx) {
      if (ctx.opener !== null && /^mermaid$/i.test(ctx.opener)) return i;
    });
  }

  /* 正文里有没有公式 —— 决定要不要先把 KaTeX 拉下来。判定**直接问 tokenizer 本身**,
     而不是另写一套"像不像公式"的正则:两套判定必然漂移,漂移的后果是"这篇文档第一次
     打开时公式显示成源码,切一下预览才正常"这种只在特定正文上出现的偶发。 */
  function hasMath(src) {
    if (firstBlockMathIndex(src) >= 0) return true;
    var found = false;
    scanSource(src, function (line, i, offset, ctx) {
      if (ctx.fenceLine || ctx.inside) return;        // 围栏行自身与围栏内部都不是正文
      var at = line.indexOf('$');
      while (at >= 0) {
        var rest = line.slice(at);
        if (inlineMath.tokenizer(rest) || inlineDisplayMath.tokenizer(rest)) { found = true; return true; }
        at = line.indexOf('$', at + 1);
      }
    });
    return found;
  }

  /* 渲染 Markdown 为 HTML 字符串;含公式/图表时先确保对应库就绪 */
  function render(md) {
    var src = String(md || '');
    var warm = [];
    if (hasMath(src) && !window.katex) warm.push(loadKatex().catch(function () { /* 失败则公式按源码显示 */ }));
    if (mermaidFenceLine(src) !== undefined && !window.mermaid) warm.push(loadMermaid().catch(function () { /* 失败则图表按源码显示 */ }));
    return Promise.all(warm).then(function () {
      footnoteReset();
      return marked.parse(src) + footnoteSection();   // 脚注区固定在文末
    });
  }

  /** 代码块的语言角标与复制按钮(预览态专属;源码态是 textarea,不经过这里) */
  function decorateCodeBlocks(root) {
    var codes = root.querySelectorAll('pre > code');
    for (var i = 0; i < codes.length; i++) {
      var code = codes[i];
      var pre = code.parentElement;
      if (!pre || pre.classList.contains('md-diagram-src')) continue;                      // 图表源码不是普通代码块
      if (pre.parentElement && pre.parentElement.classList.contains('md-code')) continue;  // 已装饰过(重复 mount)
      var lang = (code.className.match(/language-([^\s]+)/) || [])[1] || '';
      var wrap = document.createElement('div');
      wrap.className = 'md-code';
      wrap.innerHTML = '<div class="md-code-bar">' +
        (lang ? '<span class="md-code-lang">' + UI.esc(lang) + '</span>' : '') +
        UI.btn({ icon: 'file-copy-line', label: '复制', kind: 'text', size: 'sm', cls: 'md-code-copy', title: '复制代码' }) +
        '</div>';
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
      // 复制的是 textContent(高亮后是 span 树,textContent 仍是原文);UI.copyText 自带降级与 toast
      wrap.querySelector('.md-code-copy').addEventListener('click', function (el) {
        return function () { UI.copyText(el.textContent || ''); };
      }(code));
    }
  }

  /* ---------- 文内锚点(#...) ----------
     全站是哈希路由(#/p/{项目}/docs/{文档} 这类),所以**不能**让浏览器去走 location.hash:
     正文里一个 href="#fn-1" 会被路由当成一条路由解析,点下去 hash 变成 #/,用户直接被踢回
     项目列表、正在读的文档整个消失(这不是"没跳到位",是跳出了文档)。脚注的引用与回跳、
     作者手写的 [文字](#某处) 都会踩到同一个坑,所以在这里统一接管:拦下点击 → 自己滚动定位。
     一并说明为什么不改用非哈希的锚点写法:锚点还要在引用浮层、历史版本预览里可用,
     那些场景没有路由可挂,只有 JS 定位这一条路对四处渲染容器都成立。
     id 由渲染产物提供(脚注的 fn-N / fnref-N-k);作者写的 #某处 找不到目标就静默不动。
     例外:#/… 是站内路由本身(侧边栏那些链接同款),放给路由器走,别在这里吞掉。 */
  var ANCHOR_SCOPE = '.markdown-body a[href^="#"]';

  function jumpToAnchor(id) {
    var target = id && document.getElementById(id);
    if (!target) return;
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');  // 让定位同时把键盘焦点带过去
    try { target.focus({ preventScroll: true }); } catch (e) { /* 老浏览器忽略参数:退回无参调用 */ target.focus(); }
    target.scrollIntoView({ block: 'center' });   // 不用 smooth:长文档里平滑滚动会让"跳到了哪"更迟才看见
    target.classList.remove('md-anchor-hit');
    void target.offsetWidth;                      // 强制重排:连点同一处时动画能重新播
    target.classList.add('md-anchor-hit');
    setTimeout(function () { target.classList.remove('md-anchor-hit'); }, 1600);
  }

  // 捕获阶段接管:路由也在 document 上听点击,先到这层就不给它机会
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest(ANCHOR_SCOPE);
    if (!a) return;
    var frag = decodeURIComponent((a.getAttribute('href') || '').slice(1));
    if (frag.charAt(0) === '/') return;   // #/… = 站内路由,交给路由器
    e.preventDefault();
    jumpToAnchor(frag);
  }, true);

  /* 渲染并挂载到容器:代码高亮(autoloader 未加载的语言会异步拉取后自动重刷)→ 角标 → 图表 */
  function mount(el, md) {
    return render(md).then(function (html) {
      el.innerHTML = html;
      if (window.Prism && Prism.highlightAllUnder) {
        try { Prism.highlightAllUnder(el); } catch (e) { /* 高亮失败不影响内容 */ }
      }
      decorateCodeBlocks(el);
      return renderDiagrams(el);
    });
  }

  window.MdRender = { render: render, mount: mount };
})();
