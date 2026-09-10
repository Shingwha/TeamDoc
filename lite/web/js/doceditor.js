// doceditor.js — 文档编辑器增强层(纯 textarea 源码编辑,视觉统一走 editor.css):
//   1) teamdoc:// 引用 chip 的全局点击路由(注册一次,预览区/历史预览通用)
//   2) @ 或 [[ 触发引用搜索浮层(调 /api/search;文档插 chip 链接,图片文件插原生 ![]())
//   3) / 行首触发"插入组件"菜单(标题/列表/代码块/引用/分割线/上传),继续输入即时过滤
//   4) 选区浮动工具栏(纯文字格式:加粗/斜体/删除线/行内代码/链接)
//   5) 图片/附件上传:粘贴、拖拽、/ 菜单,插入 ![](url) / [name](url)
// 引用存储格式(唯一真相源为 Markdown 纯文本,源码即存储,零序列化):
//   文档:[@标题](teamdoc://doc/{projectId}/{docId})  文件:[@名称](teamdoc://file/{fileId})
//   chip 视觉由 editor.css 的 a[href^="teamdoc://"] 规则负责。
window.DocEditor = (function () {
  'use strict';

  // ---------- 1) chip 全局点击路由 ----------
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href^="teamdoc://"]');
    if (!a) return;
    e.preventDefault();
    var m = (a.getAttribute('href') || '').match(/^teamdoc:\/\/(doc|file)\/(.+)$/);
    if (!m) return;
    if (m[1] === 'doc') {
      var seg = m[2].split('/');
      // 引用默认新标签页打开(不打断当前编辑/阅读上下文)
      if (seg.length === 2) window.open(location.origin + '/#/p/' + seg[0] + '/docs/' + seg[1], '_blank');
    } else {
      window.open('/api/files/' + encodeURIComponent(m[2]) + '/download?inline=1', '_blank');
    }
  }, true);

  // Markdown 链接文本/URL 清洗(防止方括号/括号/空白破坏链接语法)
  function mdText(s) { return String(s || '').replace(/[[\]()\r\n]/g, ' ').trim() || '未命名'; }
  function mdUrl(s) { return String(s || '').replace(/[\s()]/g, ''); }

  // 云空间文件 → 浮层候选:图片走原生 Markdown 图片语法(预览直接显示),其余插引用 chip
  function fileItem(f) {
    var isImg = (f.mime || '').indexOf('image/') === 0;
    return {
      kind: '文件', kindLabel: isImg ? '图片' : '文件',
      icon: isImg ? 'image-line' : 'attachment-2', name: f.name,
      md: isImg
        ? '![' + mdText(f.name) + '](/api/files/' + f.id + '/download?inline=1)'
        : '[@' + mdText(f.name) + '](teamdoc://file/' + f.id + ')',
    };
  }

  // ---------- 浮层基础设施 ----------
  var openPanel = null; // 单例:{close()}
  function closePanel() { if (openPanel) { openPanel.close(); openPanel = null; } }

  function placeAt(el, rect) {
    el.style.visibility = 'hidden';
    document.body.appendChild(el);
    var mw = el.offsetWidth;
    var left = Math.max(8, Math.min(rect.left, window.innerWidth - mw - 8));
    // 上下两侧取空间更大的一侧,并按可用空间收缩高度,保证整体不出屏幕
    var spaceBelow = window.innerHeight - rect.bottom - 14;
    var spaceAbove = rect.top - 14;
    var below = spaceBelow >= spaceAbove;
    el.style.maxHeight = Math.min(320, Math.max(120, below ? spaceBelow : spaceAbove)) + 'px';
    var mh = el.offsetHeight;
    var top = below ? rect.bottom + 6 : rect.top - mh - 6;
    el.style.left = left + 'px';
    el.style.top = Math.max(8, Math.min(top, window.innerHeight - mh - 8)) + 'px';
    el.style.visibility = '';
  }

  // 只滚动面板自己的列表,不用 scrollIntoView(它会连带滚动所有祖先容器,把整页顶跑)
  function scrollItemIntoView(list, btn) {
    if (!btn) return;
    var lr = list.getBoundingClientRect(), br = btn.getBoundingClientRect();
    if (br.top < lr.top) list.scrollTop -= lr.top - br.top;
    else if (br.bottom > lr.bottom) list.scrollTop += br.bottom - lr.bottom;
  }

  // 面板打开期间的全局关闭器:点外部 / Esc / 滚动 / 缩放
  // 注意:面板自身列表的滚动(键盘导航 scrollItemIntoView)不算,否则会把自己关掉
  function bindPanelClosers(el, onClose) {
    function onDown(e) { if (!e.target.closest('.menu')) onClose(); }
    function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); onClose(); } }
    function onMove() { onClose(); }
    function onScroll(e) { if (e.target && el.contains(e.target)) return; onClose(); }
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onMove);
    window.addEventListener('scroll', onScroll, true);
    return function () {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', onMove);
      window.removeEventListener('scroll', onScroll, true);
    };
  }

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
    var onPick = opts.onPick, onCancel = opts.onCancel, loadDefaults = opts.loadDefaults;
    closePanel();
    var el = document.createElement('div');
    el.className = 'menu panel';
    el.innerHTML =
      '<input class="input menu-search" type="text" placeholder="搜索文档 / 文件…" autocomplete="off">' +
      '<div class="menu-scroll">' + UI.emptyState({ sm: true, title: '输入关键词,引用项目文档或云空间文件' }) + '</div>';
    placeAt(el, rect);
    var input = el.querySelector('.menu-search');
    var list = el.querySelector('.menu-scroll');
    var items = [], active = -1, closed = false;

    function close() {
      if (closed) return;
      closed = true;
      unbind();
      el.remove();
      if (openPanel && openPanel.el === el) openPanel = null;
    }
    var unbind = bindPanelClosers(el, close);

    function setActive(i) {
      active = i;
      var btns = list.querySelectorAll('.menu-item');
      btns.forEach(function (b, j) { b.classList.toggle('active', j === i); });
      scrollItemIntoView(list, btns[i]);
    }

    function render(results) {
      items = results;
      active = results.length ? 0 : -1;
      if (!results.length) {
        list.innerHTML = UI.emptyState({ sm: true, title: '无匹配结果' }).outerHTML;
        placeAt(el, rect);
        return;
      }
      var html = '', lastKind = '';
      results.forEach(function (r, i) {
        if (r.kind !== lastKind) {
          lastKind = r.kind;
          html += '<div class="menu-group">' + UI.esc(r.kind) + '</div>';
        }
        html += '<button type="button" class="menu-item' + (i === active ? ' active' : '') + '" data-i="' + i + '">' +
          UI.icon(r.icon) + '<span class="menu-name">' + UI.esc(r.name) + '</span>' +
          '<span class="menu-kind">' + UI.esc(r.kindLabel) + '</span></button>';
      });
      list.innerHTML = html;
      placeAt(el, rect); // 结果是异步到达的,内容填充后重新定位,避免按空面板算出的高度翻出屏幕
    }

    list.addEventListener('mousedown', function (e) { e.preventDefault(); }); // 保持输入框焦点
    list.addEventListener('click', function (e) {
      var btn = e.target.closest('.menu-item');
      if (!btn) return;
      var it = items[Number(btn.dataset.i)];
      close();
      if (it) onPick(it);
    });

    var doSearch = UI.debounce(function () {
      var q = input.value.trim();
      if (!q) { showDefaults(); return; }
      api('/api/search?q=' + encodeURIComponent(q) + '&type=all').then(function (r) {
        if (closed) return;
        var docs = (r && r.docs || []).slice(0, 6).map(function (d) {
          return {
            kind: '文档', kindLabel: '文档', icon: 'file-text-line', name: d.title || '无标题文档',
            md: '[@' + mdText(d.title) + '](teamdoc://doc/' + d.projectId + '/' + d.id + ')',
          };
        });
        var files = (r && r.files || []).slice(0, 4).map(fileItem);
        render(docs.concat(files));
      }).catch(function () { if (!closed) render([]); });
    }, 250);

    // 空查询:默认展示当前项目最近文档/文件(loadDefaults 注入,失败退回提示)
    var defaultsCache = null;
    function showDefaults() {
      if (defaultsCache) { render(defaultsCache); return; }
      if (!loadDefaults) {
        list.innerHTML = UI.emptyState({ sm: true, title: '输入关键词,引用项目文档或云空间文件' }).outerHTML;
        items = []; active = -1;
        return;
      }
      loadDefaults().then(function (r) {
        if (closed) return;
        defaultsCache = r;
        render(r);
      }).catch(function () {
        if (!closed) list.innerHTML = UI.emptyState({ sm: true, title: '输入关键词,引用项目文档或云空间文件' }).outerHTML;
      });
    }
    input.addEventListener('input', doSearch);
    showDefaults();

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) setActive((active + 1) % items.length); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) setActive((active - 1 + items.length) % items.length); }
      else if (e.key === 'Backspace' && !input.value) {
        e.preventDefault();
        close();
        if (onCancel) onCancel();
      }
      else if (e.key === 'Enter') {
        e.preventDefault();
        var it = items[active];
        close();
        if (it) onPick(it);
      }
    });

    input.focus();
    openPanel = { el: el, close: close };
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
    { key: 'hr', icon: 'separator', label: '分割线', snippet: '\n---\n' },
    { key: 'upload', icon: 'image-add-line', label: '上传图片 / 附件', upload: true },
  ];

  // rect 处弹出组件菜单;textarea 保持焦点,继续输入的字符做 label 过滤
  // opts: { ta, start(/ 的位置), onPick(item, query), onClose }
  function openSlashPanel(rect, opts) {
    var ta = opts.ta;
    closePanel();
    var el = document.createElement('div');
    el.className = 'menu panel';
    el.innerHTML = '<div class="menu-scroll"></div>';
    placeAt(el, rect);
    var list = el.querySelector('.menu-scroll');
    var items = [], active = -1, closed = false;

    function close() {
      if (closed) return;
      closed = true;
      unbind();
      ta.removeEventListener('keydown', onKey, true);
      el.remove();
      if (openPanel && openPanel.el === el) openPanel = null;
      if (opts.onClose) opts.onClose();
    }
    var unbind = bindPanelClosers(el, close);

    function setActive(i) {
      active = i;
      var btns = list.querySelectorAll('.menu-item');
      btns.forEach(function (b, j) { b.classList.toggle('active', j === i); });
      scrollItemIntoView(list, btns[i]);
    }

    function render(query) {
      var q = String(query || '').toLowerCase();
      items = SLASH_ITEMS.filter(function (it) {
        return !q || it.label.toLowerCase().indexOf(q) >= 0 || it.key.indexOf(q) === 0;
      });
      active = items.length ? 0 : -1;
      if (!items.length) { list.innerHTML = UI.emptyState({ sm: true, title: '无匹配组件' }).outerHTML; return; }
      list.innerHTML = items.map(function (it, i) {
        return '<button type="button" class="menu-item' + (i === active ? ' active' : '') + '" data-i="' + i + '">' +
          UI.icon(it.icon) + '<span class="menu-name">' + UI.esc(it.label) + '</span></button>';
      }).join('');
      placeAt(el, rect); // 内容变化后重新定位(过滤会收缩高度)
    }
    render('');

    function pick(i) {
      var it = items[i];
      close();
      if (it) opts.onPick(it, opts.start);
    }

    list.addEventListener('mousedown', function (e) { e.preventDefault(); }); // 保持 textarea 焦点
    list.addEventListener('click', function (e) {
      var btn = e.target.closest('.menu-item');
      if (btn) pick(Number(btn.dataset.i));
    });

    // 键盘导航挂在 textarea 上(capture,防止移动光标)
    function onKey(e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) setActive((active + 1) % items.length); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) setActive((active - 1 + items.length) % items.length); }
      else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pick(active); }
    }
    ta.addEventListener('keydown', onKey, true);

    openPanel = { el: el, close: close, render: render };
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
        api('/api/projects/' + opts.projectId + '/docs/tree'),
        api('/api/files?project_id=' + encodeURIComponent(opts.projectId)),
      ]).then(function (rs) {
        var flat = [];
        (function walk(ns) { (ns || []).forEach(function (n) { flat.push(n); walk(n.children); }); })(rs[0]);
        var docs = flat.sort(function (a, b) { return a.updatedAt < b.updatedAt ? 1 : -1; }).slice(0, 6)
          .map(function (d) {
            return {
              kind: '文档', kindLabel: '文档', icon: 'file-text-line', name: d.title || '无标题文档',
              md: '[@' + mdText(d.title) + '](teamdoc://doc/' + opts.projectId + '/' + d.id + ')',
            };
          });
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
          var img = (f.type || '').indexOf('image/') === 0;
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
