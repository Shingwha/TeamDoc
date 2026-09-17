// doceditor.js — 文档编辑器增强层(纯 textarea 源码编辑,视觉统一走 editor.css):
//   1) @ 或 [[ 触发引用搜索浮层(调 /api/search;文档/文件都插引用 chip)
//   2) / 行首触发"插入组件"菜单(标题/列表/代码块/引用/分割线/上传),继续输入即时过滤
//   3) 选区浮动工具栏(纯文字格式:加粗/斜体/删除线/行内代码/链接)
//   4) 图片/附件上传:粘贴、拖拽、/ 菜单,插入 ![](url) / [name](url)
// 引用格式与解析**不在这里**:生成走 Ref.*(js/ref.js),点击路由也在那儿注册。
// 本文件只负责"编辑器里怎么插入引用",不持有格式知识。
window.DocEditor = (function () {
  'use strict';

  // Markdown 链接文本清洗:方括号/括号/空白破坏链接语法 —— 由 Ref.text 提供,
  // 这里只做本地别名,免得格式清洗出现第二份实现
  var mdText = Ref.text;
  var mdUrl = Ref.url;

  // 文档 → 浮层候选(引用搜索与默认候选共用的一份条目构造)
  function docItem(d) {
    return {
      kind: '文档', kindLabel: '文档', icon: 'file-text-line', name: d.title || '无标题文档',
      md: Ref.docChip(d.title, d.id),
    };
  }

  // 云空间文件 → 浮层候选:能嵌图的走原生 Markdown 图片语法(预览直接显示),其余插引用 chip
  function fileItem(f) {
    var isImg = UI.isEmbedImage(f.mime, f.canInline);
    return {
      kind: '文件', kindLabel: isImg ? '图片' : '文件',
      icon: isImg ? 'image-line' : 'attachment-2', name: f.name,
      md: Ref.fileMarkdown(f),
    };
  }

  // ---------- 浮层基础设施 ----------
  // 定位、视口钳位、关闭器、单例互斥都在 ui.floatingLayer / ui.menuList;
  // 这里只保留"当前面板"句柄(enhance 清理与互斥用)与 textarea 光标定位。
  var openPanel = null; // {close(), render?(query)}
  function closePanel() { if (openPanel) { openPanel.close(); openPanel = null; } }

  // ---------- textarea 光标像素定位:mirror-div 标准做法 ----------
  var MIRROR_PROPS = ['boxSizing', 'width', 'fontFamily', 'fontSize', 'fontWeight', 'fontStyle',
    'letterSpacing', 'textIndent', 'lineHeight', 'tabSize',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth'];

  function caretRect(ta, pos) {
    var cs = getComputedStyle(ta);
    var mirror = document.createElement('div');
    mirror.style.position = 'absolute';
    mirror.style.visibility = 'hidden';
    mirror.style.whiteSpace = 'pre-wrap';
    mirror.style.wordWrap = 'break-word';
    MIRROR_PROPS.forEach(function (p) { mirror.style[p] = cs[p]; });
    var r = ta.getBoundingClientRect();
    // mirror 以文档坐标对齐 textarea(扣除其内部滚动),marker 的视口坐标即光标视口坐标
    mirror.style.left = (r.left + window.scrollX - ta.scrollLeft) + 'px';
    mirror.style.top = (r.top + window.scrollY - ta.scrollTop) + 'px';
    mirror.textContent = ta.value.slice(0, pos);
    var marker = document.createElement('span');
    marker.textContent = '​';
    mirror.appendChild(marker);
    document.body.appendChild(mirror);
    var m = marker.getBoundingClientRect();
    var lh = parseFloat(cs.lineHeight) || (parseFloat(cs.fontSize) || 16) * 1.6;
    mirror.remove();
    return { left: m.left, top: m.top, bottom: m.top + lh, height: lh };
  }

  // ---------- 2) 引用搜索浮层 ----------
  // opts: { onPick, onCancel, loadDefaults }
  //   onCancel:搜索框为空时按 Backspace 退出(删除触发符并回到编辑器),与 Esc 的区别是 Esc 保留字面量
  //   loadDefaults:空查询时的默认候选(当前项目最近文档/文件),返回 Promise<items[]>
  function openRefPanel(rect, opts) {
    closePanel();
    var EMPTY = UI.emptyHtml({ sm: true, title: '输入关键词,引用项目文档或云空间文件' });
    var defaultsCache = null;

    var panel = UI.menuList({
      rect: rect,
      search: { placeholder: '搜索文档 / 文件…' },
      items: [],
      groupOf: function (it) { return it.kind; },
      itemHtml: function (r) {
        return UI.icon(r.icon) + '<span class="menu-name">' + UI.esc(r.name) + '</span>' +
          '<span class="menu-kind">' + UI.esc(r.kindLabel) + '</span>';
      },
      emptyHtml: function () { return EMPTY; },
      onPick: opts.onPick,
      onBackspaceEmpty: opts.onCancel,
      onInput: function (q) { doSearch(q); },
      onClose: function () { if (openPanel && openPanel._panel === panel) openPanel = null; },
    });

    // 空查询:默认展示当前项目最近文档/文件(loadDefaults 注入,失败退回提示)
    function showDefaults() {
      if (defaultsCache) { panel.render(defaultsCache); return; }
      if (!opts.loadDefaults) { panel.render([]); return; }
      opts.loadDefaults().then(function (r) {
        if (panel.closed) return;
        defaultsCache = r;
        panel.render(r);
      }).catch(function () { if (!panel.closed) panel.render([]); });
    }

    var doSearch = UI.debounce(function (q) {
      if (!q) { showDefaults(); return; }
      api(Endpoints.search({ q: q, type: 'all' })).then(function (r) {
        if (panel.closed) return;
        var docs = (r && r.docs || []).slice(0, 6).map(function (d) { return docItem(d); });
        var files = (r && r.files || []).slice(0, 4).map(fileItem);
        panel.render(docs.concat(files));
      }).catch(function () { if (!panel.closed) panel.render([]); });
    }, 250);

    showDefaults();
    openPanel = { _panel: panel, close: panel.close };
    return openPanel;
  }

  // ---------- 3) 选区浮动工具栏 ----------
  var FLOAT_CMDS = [
    { icon: 'bold', title: '加粗', before: '**', after: '**', ph: '加粗文字' },
    { icon: 'italic', title: '斜体', before: '*', after: '*', ph: '斜体文字' },
    { icon: 'strikethrough', title: '删除线', before: '~~', after: '~~', ph: '删除线' },
    { icon: 'code-line', title: '行内代码', before: '`', after: '`', ph: '代码' },
    { icon: 'link', title: '链接', link: true },
  ];

  // ---------- / 行首插入组件菜单 ----------
  // 与选区浮动工具栏语义分开:浮动工具栏只改文字格式,/ 菜单负责插入块级组件与上传
  var SLASH_ITEMS = [
    { key: 'h1', icon: 'h-1', label: '一级标题', snippet: '# ' },
    { key: 'h2', icon: 'h-2', label: '二级标题', snippet: '## ' },
    { key: 'h3', icon: 'h-3', label: '三级标题', snippet: '### ' },
    { key: 'ul', icon: 'list-unordered', label: '无序列表', snippet: '- ' },
    { key: 'ol', icon: 'list-ordered', label: '有序列表', snippet: '1. ' },
    { key: 'todo', icon: 'list-check-3', label: '待办事项', snippet: '- [ ] ' },
    { key: 'quote', icon: 'double-quotes-l', label: '引用', snippet: '> ' },
    { key: 'code', icon: 'code-box-line', label: '代码块', snippet: '```\n\n```\n', cursorBack: 5 },
    // 图表:光标停在示例行尾(改内容比从空块开始敲省事);语法见 markdown.js 的 Mermaid 段
    { key: 'mermaid', icon: 'flow-chart', label: '图表(Mermaid)',
      snippet: '```mermaid\nflowchart LR\n  A[开始] --> B[结束]\n```\n', cursorBack: 5 },
    { key: 'hr', icon: 'separator', label: '分割线', snippet: '\n---\n' },
    { key: 'upload', icon: 'image-add-line', label: '上传图片 / 附件', upload: true },
  ];

  // rect 处弹出组件菜单;textarea 保持焦点,继续输入的字符做 label 过滤
  // opts: { ta, start(/ 的位置), onPick(item, query), onClose }
  function openSlashPanel(rect, opts) {
    var ta = opts.ta;
    closePanel();
    var panel = UI.menuList({
      rect: rect,
      keyTarget: ta,   // 键盘导航挂在 textarea 上(capture,防止移动光标)
      tabPicks: true,
      items: [],
      itemHtml: function (it) {
        return UI.icon(it.icon) + '<span class="menu-name">' + UI.esc(it.label) + '</span>';
      },
      emptyHtml: function () { return UI.emptyHtml({ sm: true, title: '无匹配组件' }); },
      onPick: function (it) { opts.onPick(it, opts.start); },
      onClose: function () {
        if (openPanel && openPanel._panel === panel) openPanel = null;
        if (opts.onClose) opts.onClose();
      },
    });

    function renderQuery(query) {
      var q = String(query || '').toLowerCase();
      panel.render(SLASH_ITEMS.filter(function (it) {
        return !q || it.label.toLowerCase().indexOf(q) >= 0 || it.key.indexOf(q) === 0;
      }));
    }
    renderQuery('');
    openPanel = { _panel: panel, close: panel.close, render: renderQuery };
    return openPanel;
  }

  /**
   * 增强一个 Markdown 源码 textarea:@ 与 [[ 引用 / 选区浮动工具栏 / 粘贴拖拽上传。
   * @param ta 源码 textarea
   * @param opts { projectId, upload: async (File) => {name, url} }
   * @returns {Function} cleanup(路由离开时调用)
   */
  function enhance(ta, opts) {
    var floatbar = null;

    // 空查询默认候选:当前项目最近更新的文档(树拉平按 updatedAt 倒序)+ 云空间根目录最近文件
    function loadDefaults() {
      return Promise.all([
        api(Endpoints.docTree(opts.projectId)),
        api(Endpoints.files({ project_id: opts.projectId })),
      ]).then(function (rs) {
        var flat = [];
        (function walk(ns) { (ns || []).forEach(function (n) { flat.push(n); walk(n.children); }); })(rs[0]);
        var docs = flat.sort(function (a, b) { return a.updatedAt < b.updatedAt ? 1 : -1; }).slice(0, 6)
          .map(docItem);
        var files = ((rs[1] && rs[1].files) || []).slice()
          .sort(function (a, b) { return a.createdAt < b.createdAt ? 1 : -1; }).slice(0, 4)
          .map(fileItem);
        return docs.concat(files);
      });
    }

    function emitInput() { ta.dispatchEvent(new Event('input', { bubbles: true })); }

    function insertAtCursor(text) {
      ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
      ta.focus();
      emitInput();
    }

    // --- @ / [[ 触发:输入事件里看光标前的字符 ---
    // triggerStart 记录触发符位置,选中后整体替换为 Markdown 链接;Esc 关闭浮层则触发符原样保留
    var slash = null; // {start, panel} / 菜单打开期间的状态

    function closeSlash() { if (slash) { var p = slash.panel; slash = null; p.close(); } }

    function slashQuery() {
      return ta.value.slice(slash.start + 1, ta.selectionStart);
    }

    function pickSlash(it, s) {
      var pos = ta.selectionStart;
      ta.focus();
      if (it.upload) {
        ta.setRangeText('', s, pos, 'end');
        emitInput();
        pickFile();
        return;
      }
      ta.setRangeText(it.snippet, s, pos, 'end');
      var cur = s + it.snippet.length - (it.cursorBack || 0);
      ta.setSelectionRange(cur, cur);
      emitInput();
    }

    function onInput() {
      var pos = ta.selectionStart, v = ta.value;
      // / 菜单已打开:更新过滤;/ 被删掉或光标跑出触发区则关闭
      if (slash) {
        if (pos < slash.start + 1 || v.charAt(slash.start) !== '/') { closeSlash(); }
        else { slash.panel.render(slashQuery()); return; }
      }
      var start = -1;
      if (v.charAt(pos - 1) === '@') start = pos - 1;
      else if (v.slice(pos - 2, pos) === '[[') start = pos - 2;
      else if (v.charAt(pos - 1) === '/') {
        // 仅在行首触发(前面只有空白):/ 是"插入组件",不是普通字符
        var lineHead = v.slice(v.lastIndexOf('\n', pos - 2) + 1, pos - 1);
        if (/^\s*$/.test(lineHead)) {
          slash = { start: pos - 1 };
          slash.panel = openSlashPanel(caretRect(ta, pos), {
            ta: ta, start: slash.start,
            onPick: function (it, s) { pickSlash(it, s); },
            onClose: function () { slash = null; },
          });
          return;
        }
      }
      if (start < 0) return;
      openRefPanel(caretRect(ta, pos), {
        onPick: function (it) {
          ta.focus();
          ta.setRangeText(it.md + ' ', start, pos, 'end');
          emitInput();
        },
        onCancel: function () {
          // 空查询 Backspace 退出:删掉触发符(@ 或 [[),回到编辑器
          ta.focus();
          ta.setRangeText('', start, pos, 'end');
          emitInput();
        },
        loadDefaults: loadDefaults,
      });
    }

    // --- 浮动工具栏 ---
    function buildFloatbar() {
      var bar = document.createElement('div');
      bar.className = 'td-floatbar';
      bar.hidden = true;
      FLOAT_CMDS.forEach(function (c) {
        // 反色表面上的图标按钮:复用 .btn-icon 的尺寸与圆角,只覆盖配色
        var wrap = document.createElement('span');
        wrap.innerHTML = UI.iconBtn({ icon: c.icon, title: c.title, cls: 'float-btn' });
        var b = wrap.firstElementChild;
        b.addEventListener('mousedown', function (e) { e.preventDefault(); }); // 保持选区
        b.addEventListener('click', function () { execCmd(c); });
        bar.appendChild(b);
      });
      document.body.appendChild(bar);
      return bar;
    }

    function execCmd(c) {
      var s = ta.selectionStart, e = ta.selectionEnd;
      var sel = ta.value.slice(s, e);
      if (c.link) return insertLink(sel, s, e);
      var text = sel || c.ph;
      ta.setRangeText(c.before + text + c.after, s, e, 'end');
      // 选中内层文字,方便继续输入替换占位文本
      ta.setSelectionRange(s + c.before.length, s + c.before.length + text.length);
      ta.focus();
      emitInput();
      hideBar();
    }

    function insertLink(sel, s, e) {
      hideBar();
      UI.inputDialog({ title: '插入链接', label: '链接地址', placeholder: 'https://…' }).then(function (url) {
        url = mdUrl(url);
        if (!url) return;
        ta.focus();
        ta.setRangeText('[' + mdText(sel || '链接') + '](' + url + ')', s, e, 'end');
        emitInput();
      });
    }

    // --- 上传:粘贴 / 拖拽 / 按钮 ---
    function uploadFiles(files) {
      (Array.prototype.slice.call(files)).forEach(function (f) {
        opts.upload(f).then(function (r) {
          // r.isImage 由调用方判定(见 views/doc-editor.js uploadFile,UI.isEmbedImage = 图片类型 + 服务端白名单);
          // 不做 f.type 本地猜测 —— 与服务端白名单不一致时只会插出坏图(见 ui.js isEmbedImage)
          var img = !!r.isImage;
          insertAtCursor(img ? '![' + mdText(r.name) + '](' + r.url + ')' : '[' + mdText(r.name) + '](' + r.url + ')');
        }).catch(function (err) { UI.toast('上传失败:' + f.name + '(' + (err.message || '') + ')', 'danger'); });
      });
    }

    function pickFile() {
      hideBar();
      var inp = document.createElement('input');
      inp.type = 'file';
      inp.multiple = true;
      inp.onchange = function () { if (inp.files.length) uploadFiles(inp.files); };
      inp.click();
    }

    function onPaste(e) {
      var fs = e.clipboardData && e.clipboardData.files;
      if (fs && fs.length) { e.preventDefault(); uploadFiles(fs); }
    }
    function onDrop(e) {
      var fs = e.dataTransfer && e.dataTransfer.files;
      if (fs && fs.length) { e.preventDefault(); uploadFiles(fs); }
    }
    function onDragOver(e) {
      if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') >= 0) e.preventDefault();
    }

    function showBar() {
      if (!floatbar) floatbar = buildFloatbar();
      var r = caretRect(ta, ta.selectionStart);
      floatbar.hidden = false;
      var bw = floatbar.offsetWidth, bh = floatbar.offsetHeight;
      var left = Math.max(8, Math.min(r.left, window.innerWidth - bw - 8));
      var top = r.top - bh - 8;
      if (top < 8) top = Math.min(r.bottom + 8, window.innerHeight - bh - 8);
      floatbar.style.left = left + 'px';
      floatbar.style.top = Math.max(8, top) + 'px';
    }
    function hideBar() { if (floatbar) floatbar.hidden = true; }

    var maybeShowBar = UI.debounce(function () {
      if (document.activeElement !== ta || ta.selectionStart === ta.selectionEnd) { hideBar(); return; }
      showBar();
    }, 80);

    function onMouseUp() { maybeShowBar(); }
    function onKeyUp(e) {
      if (e.key === 'Escape') { hideBar(); return; }
      if (e.shiftKey || e.key.indexOf('Arrow') === 0) maybeShowBar();
    }
    function onDocDown(e) {
      if (e.target !== ta && !(floatbar && floatbar.contains(e.target))) hideBar();
    }
    function onScroll(e) { if (!floatbar || !floatbar.contains(e.target)) hideBar(); }

    ta.addEventListener('input', onInput);
    ta.addEventListener('mouseup', onMouseUp);
    ta.addEventListener('keyup', onKeyUp);
    ta.addEventListener('paste', onPaste);
    ta.addEventListener('drop', onDrop);
    ta.addEventListener('dragover', onDragOver);
    document.addEventListener('pointerdown', onDocDown, true);
    window.addEventListener('resize', onScroll);
    ta.addEventListener('scroll', onScroll);

    return function cleanup() {
      closePanel();
      ta.removeEventListener('input', onInput);
      ta.removeEventListener('mouseup', onMouseUp);
      ta.removeEventListener('keyup', onKeyUp);
      ta.removeEventListener('paste', onPaste);
      ta.removeEventListener('drop', onDrop);
      ta.removeEventListener('dragover', onDragOver);
      document.removeEventListener('pointerdown', onDocDown, true);
      window.removeEventListener('resize', onScroll);
      ta.removeEventListener('scroll', onScroll);
      maybeShowBar.cancel();
      if (floatbar) { floatbar.remove(); floatbar = null; }
    };
  }

  return { enhance: enhance };
})();
