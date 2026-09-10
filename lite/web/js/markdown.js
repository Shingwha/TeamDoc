/* markdown.js — 全站唯一 Markdown 渲染路径(编辑预览 / 历史版本预览共用)。
   栈:marked 11.1.1(jsdelivr 固定)+ Prism 1.29.0(autoloader 按需拉语言包)+ KaTeX 0.16.9(检测到公式才懒加载)。
   安全:raw HTML 一律转义(多人协作防 XSS),只支持纯 Markdown 语义;teamdoc:// 引用链接输出为普通 <a>,
   由 editor.css 的 chip 样式与 doceditor.js 的全局点击路由接管。 */
(function () {
  'use strict';

  var KATEX_CDN = 'https://cdn.jsdelivr.net/npm/katex@0.16.9';
  var PRISM_COMPONENTS = 'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/';
  var katexPromise = null;

  function loadKatex() {
    if (window.katex) return Promise.resolve();
    if (!katexPromise) {
      katexPromise = new Promise(function (resolve, reject) {
        var css = document.createElement('link');
        css.rel = 'stylesheet';
        css.href = KATEX_CDN + '/dist/katex.min.css';
        document.head.appendChild(css);
        var s = document.createElement('script');
        s.src = KATEX_CDN + '/dist/katex.min.js';
        s.onload = function () { resolve(); };
        s.onerror = function () { katexPromise = null; reject(new Error('katex load failed')); };
        document.head.appendChild(s);
      });
    }
    return katexPromise;
  }

  function renderMath(tex, displayMode) {
    if (!window.katex) return UI.esc(displayMode ? '$$' + tex + '$$' : '$' + tex + '$'); // 懒加载失败兜底:显示源码
    var html = katex.renderToString(tex, { displayMode: displayMode, throwOnError: false });
    return displayMode ? '<div class="katex-display-wrapper">' + html + '</div>' : html;
  }

  var blockMath = {
    name: 'blockMath',
    level: 'block',
    start: function (src) { return src.indexOf('$$'); },
    tokenizer: function (src) {
      var cap = /^\$\$([\s\S]+?)\$\$/.exec(src);
      if (cap) return { type: 'blockMath', raw: cap[0], text: cap[1].trim() };
    },
    renderer: function (token) { return renderMath(token.text, true); },
  };
  var inlineMath = {
    name: 'inlineMath',
    level: 'inline',
    start: function (src) { return src.indexOf('$'); },
    tokenizer: function (src) {
      var cap = /^\$((?:[^$]|\\\$)+?)\$/.exec(src);
      if (cap) return { type: 'inlineMath', raw: cap[0], text: cap[1].trim() };
    },
    renderer: function (token) { return renderMath(token.text, false); },
  };

  var renderer = new marked.Renderer();
  // raw HTML 全部转义(marked v11 传 token 对象,旧签名传字符串,两种都兜住)
  renderer.html = function (t) { return UI.esc(typeof t === 'string' ? t : (t && t.text) || ''); };
  // 所有链接新标签页打开(阅读型应用惯例;teamdoc:// 由 doceditor.js 全局路由接管,target 不影响)
  renderer.link = function (href, title, text) {
    return '<a href="' + UI.esc(href) + '"' + (title ? ' title="' + UI.esc(title) + '"' : '') +
      ' target="_blank" rel="noopener noreferrer">' + text + '</a>';
  };
  marked.use({ renderer: renderer, extensions: [blockMath, inlineMath] });
  marked.setOptions({ breaks: true, gfm: true });

  if (window.Prism && Prism.plugins && Prism.plugins.autoloader) {
    Prism.plugins.autoloader.languages_path = PRISM_COMPONENTS;
  }

  var MATH_RE = /\$\$[\s\S]+?\$\$|\$[^$\n]+?\$/;

  /* 渲染 Markdown 为 HTML 字符串;含公式时先确保 KaTeX 就绪 */
  function render(md) {
    var src = String(md || '');
    var needMath = MATH_RE.test(src) && !window.katex;
    return (needMath ? loadKatex().catch(function () { /* 失败则公式按源码显示 */ }) : Promise.resolve())
      .then(function () { return marked.parse(src); });
  }

  /* 渲染并挂载到容器,随后做代码高亮(autoloader 未加载的语言会异步拉取后自动重刷) */
  function mount(el, md) {
    return render(md).then(function (html) {
      el.innerHTML = html;
      if (window.Prism && Prism.highlightAllUnder) {
        try { Prism.highlightAllUnder(el); } catch (e) { /* 高亮失败不影响内容 */ }
      }
    });
  }

  window.MdRender = { render: render, mount: mount };
})();
