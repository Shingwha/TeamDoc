// ui.js — 自研组件库与通用工具(全部视图复用,禁止重复造轮子)
// 工具:esc / debounce / fmtSize / fmtDate / copyText / roleRank / roleLabel / icon
// 浮层:toast / err / modal / confirmDialog / inputDialog / formModal / dropdownMenu
// 展示:avatar / spinner / loadingRow / emptyState / badge / banner / card
// 布局:pageHead / toolbar / seg / listRow / tableHead / tableRow / btn / iconBtn
//
// 约定:标记类工厂一律返回 HTML 字符串(与视图的字符串拼接风格一致,不改调用方写法);
//       需要事件接线的才返回 DOM 元素(emptyState)。
//       尺寸档位来自 tokens.css,工厂里只做"档位 → class"的映射,不写裸像素。
window.UI = (function () {
  'use strict';

  /** HTML 转义(所有动态文本拼接前必须过一遍) */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /** 防抖,带 flush()/cancel() */
  function debounce(fn, ms) {
    var t = null, args = null, ctx = null;
    function d() {
      args = arguments; ctx = this;
      clearTimeout(t);
      t = setTimeout(function () { t = null; fn.apply(ctx, args); }, ms);
    }
    d.flush = function () { if (t) { clearTimeout(t); t = null; fn.apply(ctx, args); } };
    d.cancel = function () { clearTimeout(t); t = null; };
    return d;
  }

  /**
   * Remix Icon:<i class="ri-xxx"></i>
   * name 传裸名('file-text-line')或带前缀('ri-file-text-line')均可 —— 内部统一归一化。
   * 历史上两种写法混用,直接拼接会产出 ri-ri-* 死类名(图标不显示),故在此兜住。
   */
  function icon(name, cls) {
    var n = String(name || '').replace(/^ri-/, '');
    return '<i class="ri-' + esc(n) + (cls ? ' ' + esc(cls) : '') + '"></i>';
  }


  /** 按 mime 给出文件类型图标与配色类(全站唯一来源:云空间/搜索/最近文件共用)。
   *  配色类 .fi-* 定义在 components.css,取 --file-* 固定色(主题无关,深色下不变) */
  function fileIcon(mime) {
    mime = mime || '';
    if (mime.indexOf('image/') === 0) return { icon: 'image-line', cls: 'fi-image' };
    if (mime === 'application/pdf') return { icon: 'file-pdf-2-line', cls: 'fi-pdf' };
    if (mime.indexOf('word') >= 0) return { icon: 'file-word-2-line', cls: 'fi-word' };
    if (mime.indexOf('excel') >= 0 || mime.indexOf('spreadsheet') >= 0) return { icon: 'file-excel-2-line', cls: 'fi-excel' };
    if (mime.indexOf('presentation') >= 0 || mime.indexOf('powerpoint') >= 0) return { icon: 'file-ppt-2-line', cls: 'fi-ppt' };
    if (mime.indexOf('zip') >= 0 || mime.indexOf('compressed') >= 0 || mime.indexOf('tar') >= 0) return { icon: 'file-zip-line', cls: 'fi-zip' };
    if (mime.indexOf('text/') === 0 || mime.indexOf('json') >= 0 || mime.indexOf('markdown') >= 0) return { icon: 'file-text-line', cls: 'fi-text' };
    if (mime.indexOf('audio/') === 0 || mime.indexOf('video/') === 0) return { icon: 'file-music-line', cls: 'fi-media' };
    return { icon: 'file-line', cls: 'fi-default' };
  }

  /** 文件大小人性化 */
  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  /** 服务端时间戳为 UTC naive:无时区后缀时按 UTC 解析再转本地。解析失败返回 null */
  function parseDate(s) {
    if (!s) return null;
    var str = String(s);
    var hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(str);
    var d = new Date(hasTz ? str : str.replace(' ', 'T') + 'Z');
    return isNaN(d.getTime()) ? null : d;
  }

  /** 完整时间(本地时区,24 小时制)。无法解析时原样返回,便于排查 */
  function fmtDate(s) {
    if (!s) return '-';
    var d = parseDate(s);
    return d ? d.toLocaleString('zh-CN', { hour12: false }) : String(s);
  }

  /** 短日期 M/D HH:MM(去年份与秒),用于列表徽标这类空间紧张处。
      从 Date 直接格式化,而不是对 fmtDate 的结果做 slice —— 后者依赖月/日的位数:
      "2026/9/11 15:12:34".slice(5,16) 会得到 "9/11 15:12:"(多一个冒号),
      而 "2026/12/25 15:12:34" 恰好正常。同一个函数在不同日期下表现不一致。 */
  function fmtDateShort(s) {
    if (!s) return '-';
    var d = parseDate(s);
    if (!d) return String(s);
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return (d.getMonth() + 1) + '/' + d.getDate() + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes());
  }

  /** 复制文本(非安全上下文降级 execCommand) */
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (_) { /* 忽略 */ }
      ta.remove();
    }
    toast('已复制到剪贴板', 'success');
  }

  /* ---------- 角色 ---------- */
  var ROLE_RANK = { VIEWER: 0, EDITOR: 1, ADMIN: 2, OWNER: 3 };
  var ROLE_LABEL = { VIEWER: '只读成员', EDITOR: '编辑者', ADMIN: '管理员', OWNER: '所有者' };
  function roleRank(r) { return ROLE_RANK[r] != null ? ROLE_RANK[r] : -1; }
  function roleLabel(r) { return ROLE_LABEL[r] || r || '-'; }

  /* ---------- 项目权限判定 ----------
     公开项目引入后,**不要**再用 roleRank(myRole) 直接判:公开项目的访客也会
     拿到 myRole=VIEWER,但他不是成员 —— 仅凭 myRole 渲染出写按钮,点了就是 403。
     一律用下面的 helper,它们同时看 isMember。 */
  /** 真成员 + 角色达到 required */
  function hasRole(proj, required) {
    if (!proj || !proj.isMember) return false;
    return roleRank(proj.myRole) >= roleRank(required);
  }
  /** 可写(EDITOR+):上传/新建/编辑/删除内容、回收站的恢复与彻底删除 */
  function canEdit(proj) { return hasRole(proj, 'EDITOR'); }
  /** 可管项目(ADMIN+):改项目信息、管理成员、公开开关、跨项目移动的源项目 */
  function canAdmin(proj) { return hasRole(proj, 'ADMIN'); }
  /** 可危险操作(OWNER):删除项目 */
  function canOwn(proj) { return hasRole(proj, 'OWNER'); }

  /* ---------- 头像 ---------- */
  // 调色板与 server/auth.py 的 _PALETTE 保持一致(顺序即映射,勿单独改动其中一侧)
  var AVATAR_COLORS = ['#3370ff', '#7c3aed', '#db2777', '#ea580c', '#16a34a', '#0891b2', '#ca8a04', '#1f6feb'];
  function hashIndex(seed, mod) {
    var h = 0, s = String(seed == null ? '' : seed);
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return mod ? h % mod : h;
  }
  /**
   * 本地哈希取色:仅在调用方拿不到服务端 avatarColor 时兜底(如仅知 userId 的场景)。
   * 正常路径应传 opts.color(即各接口返回的 avatarColor),由服务端统一取色,
   * 避免同一用户在不同视图呈现不同颜色。
   */
  function avatarColor(seed) { return AVATAR_COLORS[hashIndex(seed, AVATAR_COLORS.length)]; }

  /**
   * 头像 HTML:首字圆形。优先 opts.color(服务端 avatarColor),否则按 seed(id)哈希取色。
   * 尺寸走类(--avatar-sm/lg),底色是数据(服务端下发)故仍内联注入 —— 那是"内容",不是"样式决策"。
   * @param {{name:string, seed?:string, size?:number|'sm'|'lg', color?:string, editing?:boolean, title?:string}} opts
   *   size 传数字时向上取最近的档位(28→sm、36及以上→lg),传档位名则直接用
   */
  function avatar(opts) {
    opts = opts || {};
    var size = opts.size;
    var sizeCls = '';
    if (size === 'sm' || size === 'lg') sizeCls = ' avatar-' + size;
    else if (typeof size === 'number' && size <= 28) sizeCls = ' avatar-sm';
    else if (typeof size === 'number' && size >= 36) sizeCls = ' avatar-lg';
    var color = opts.color || avatarColor(opts.seed || opts.name || '?');
    return '<span class="avatar' + sizeCls + (opts.editing ? ' editing' : '') + '"' +
      ' style="background:' + esc(color) + '" title="' + esc(opts.title || opts.name || '') + '">' +
      esc(String(opts.name || '?').slice(0, 1)) + '</span>';
  }

  /** 加载圈 HTML;size 传 'sm' 得 16px 小圈,否则默认 22px */
  function spinner(size) {
    return '<span class="spinner' + (size === 'sm' ? ' spinner-sm' : '') + '"></span>';
  }


  /** 居中加载行 HTML */
  function loadingRow(text) {
    return '<div class="loading-row">' + spinner() + '<span>' + esc(text || '加载中…') + '</span></div>';
  }

  /**
   * 空状态,返回 DOM 元素。action: {label, onClick} 可选。
   * @param {{icon:string, title:string, desc?:string, sm?:boolean,
   *          action?:{label:string, onClick:Function}}} opts
   *   sm:小尺寸档(面板/侧栏内),不显示图标
   */
  function emptyState(opts) {
    var el = document.createElement('div');
    el.className = 'empty-state' + (opts.sm ? ' sm' : '');
    el.innerHTML =
      '<div class="es-icon">' + icon(opts.icon || 'inbox-line') + '</div>' +
      '<div class="es-title">' + esc(opts.title || '') + '</div>' +
      (opts.desc ? '<div class="es-desc">' + esc(opts.desc) + '</div>' : '');
    if (opts.action && opts.action.label) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-filled btn-sm';
      btn.textContent = opts.action.label;
      if (opts.action.onClick) btn.addEventListener('click', opts.action.onClick);
      el.appendChild(btn);
    }
    return el;
  }

  /* ---------- 按钮 / 徽标 / 提示条(HTML 字符串工厂) ---------- */

  var BTN_KIND = {
    filled: 'btn-filled', tonal: 'btn-tonal', outline: 'btn-outline',
    text: 'btn-text', danger: 'btn-danger', 'danger-outline': 'btn-danger-outline',
  };

  /**
   * 按钮 HTML。
   * @param {{label?:string, icon?:string, kind?:string, size?:'sm'|'lg'|'block',
   *          id?:string, title?:string, type?:string, disabled?:boolean,
   *          attrs?:string, cls?:string}} o
   *   kind: filled | tonal | outline | text | danger | danger-outline
   *   size: 省略为页面级(40px);'sm' 行内级(32px);'lg' 登录页主按钮
   */
  function btn(o) {
    o = o || {};
    var cls = ['btn', BTN_KIND[o.kind], o.size === 'sm' ? 'btn-sm' : '', o.size === 'lg' ? 'btn-lg' : '',
      o.size === 'block' ? 'btn-block' : '', o.cls || ''].filter(Boolean).join(' ');
    return '<button class="' + cls + '" type="' + esc(o.type || 'button') + '"' +
      (o.id ? ' id="' + esc(o.id) + '"' : '') +
      (o.title ? ' title="' + esc(o.title) + '"' : '') +
      (o.disabled ? ' disabled' : '') +
      (o.attrs ? ' ' + o.attrs : '') + '>' +
      (o.icon ? icon(o.icon) : '') + (o.label ? esc(o.label) : '') + '</button>';
  }

  /**
   * 图标按钮 HTML(纯图标圆形;title 必给,否则无文字标签不可访问)。
   * @param {{icon:string, title:string, danger?:boolean, size?:'sm', id?:string,
   *          cls?:string, attrs?:string}} o
   */
  function iconBtn(o) {
    o = o || {};
    return '<button class="btn-icon' + (o.size === 'sm' ? ' btn-sm' : '') +
      (o.danger ? ' danger' : '') + (o.cls ? ' ' + o.cls : '') + '" type="button"' +
      (o.id ? ' id="' + esc(o.id) + '"' : '') +
      ' title="' + esc(o.title || '') + '"' +
      (o.attrs ? ' ' + o.attrs : '') + '>' + icon(o.icon) + '</button>';
  }

  /**
   * 徽标 HTML(全站唯一的胶囊小标签)。
   * @param {{text:string, kind?:'primary'|'success'|'danger'|'warning', count?:boolean,
   *          cls?:string}} o
   *   count:数字计数徽标(等宽居中,用于分段/侧栏)
   *   cls:附加类(xs 极紧凑档 / tint 跟随文字色的淡底)
   */
  function badge(o) {
    o = o || {};
    var cls = ['badge', o.kind || '', o.count ? 'count' : '', o.cls || ''].filter(Boolean).join(' ');
    return '<span class="' + cls + '">' + esc(o.text == null ? '' : o.text) + '</span>';
  }

  /**
   * 提示条 HTML(list-warn / remote-bar / login-err 三套同构实现的统一替代)。
   * @param {{text:string, kind?:'warn'|'danger'|'info'|'primary', icon?:string,
   *          sm?:boolean, id?:string, cls?:string,
   *          action?:{label:string, kind?:string, id?:string}}} o
   *   text 为纯文本(需富文本时调用方自行拼接后传入 html)
   */
  function banner(o) {
    o = o || {};
    var cls = ['banner', o.kind || 'info', o.sm ? 'sm' : '', o.cls || ''].filter(Boolean).join(' ');
    return '<div class="' + cls + '"' + (o.id ? ' id="' + esc(o.id) + '"' : '') +
      (o.hidden ? ' hidden' : '') + '>' +
      (o.icon ? icon(o.icon) : '') +
      (o.html != null ? o.html : '<span>' + esc(o.text || '') + '</span>') +
      (o.action ? btn({ label: o.action.label, kind: o.action.kind || 'tonal', size: 'sm', id: o.action.id }) : '') +
      '</div>';
  }

  /**
   * 卡片 HTML(带标题/说明/操作的通用容器)。
   * @param {{title?:string, icon?:string, note?:string, body:string,
   *          actions?:string, between?:boolean, danger?:boolean,
   *          cls?:string, id?:string}} o
   *   actions:操作区 HTML(通常由 btn()/iconBtn() 拼);between:标题与操作分居两端
   *   body:卡片正文 HTML
   */
  function card(o) {
    o = o || {};
    var head = '';
    if (o.title != null) {
      // between:标题与操作分居两端;否则操作紧跟标题文字之后
      var label = (o.icon ? icon(o.icon) + ' ' : '') + esc(o.title);
      head = '<div class="card-title' + (o.between ? ' between' : '') + (o.danger ? ' danger' : '') + '">' +
        (o.between ? '<span>' + label + '</span>' : label) +
        (o.actions || '') +
        '</div>';
    }
    return '<div class="card' + (o.cls ? ' ' + o.cls : '') + '"' + (o.id ? ' id="' + esc(o.id) + '"' : '') + '>' +
      head + (o.note ? '<div class="card-note">' + esc(o.note) + '</div>' : '') + o.body + '</div>';
  }

  /**
   * 统计格子组:一排"标签 / 数值 / 副文本"的等宽单元格(管理后台的存储、备份区共用)。
   * @param {Array<{label:string, value:string, sub?:string, icon?:string,
   *                muted?:boolean, danger?:boolean}>} items
   *   value 为纯文本(调用方传 fmtSize 等结果即可);muted:次要数值;danger:告警数值
   *
   * 为什么收进工厂:这段结构原先在管理后台手写了多份,新增备份卡时又抄了一遍 ——
   * 于是"数值该用哪个类、告警怎么标红"变成每个调用点各自记忆的约定,
   * 改一处样式要翻遍所有副本。
   */
  function statGrid(items) {
    return '<div class="stat-grid">' + (items || []).map(function (s) {
      var cls = ['stat-value', s.muted ? 'muted' : '', s.danger ? 'danger' : '']
        .filter(Boolean).join(' ');
      return '<div class="stat">' +
        '<div class="stat-label">' + (s.icon ? icon(s.icon) : '') + esc(s.label) + '</div>' +
        '<div class="' + cls + '">' + esc(s.value) + '</div>' +
        (s.sub ? '<div class="stat-sub">' + esc(s.sub) + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
  }

  /* ---------- 布局工厂 ---------- */

  /**
   * 页头 HTML:标题 + 副标题(左)与页面级操作(右)。
   * @param {{title:string, sub?:string, actions?:string, id?:string}} o
   */
  function pageHead(o) {
    o = o || {};
    return '<div class="page-head"' + (o.id ? ' id="' + esc(o.id) + '"' : '') + '>' +
      '<div><h1 class="page-title">' + esc(o.title) + '</h1>' +
      (o.sub ? '<div class="page-sub">' + o.sub + '</div>' : '') + '</div>' +
      (o.actions ? '<div class="page-actions">' + o.actions + '</div>' : '') + '</div>';
  }

  /**
   * 工具栏 HTML:上下文信息(左)+ 操作组(右),用于内容区顶部。
   * @param {{left:string, right?:string, id?:string, cls?:string}} o
   */
  function toolbar(o) {
    o = o || {};
    return '<div class="toolbar' + (o.cls ? ' ' + o.cls : '') + '"' + (o.id ? ' id="' + esc(o.id) + '"' : '') + '>' +
      o.left + (o.right ? '<div class="toolbar-actions">' + o.right + '</div>' : '') + '</div>';
  }

  /**
   * 区块小标题(页面内分段,如管理后台的"存储"/"用户")。
   * @param {{title:string, icon?:string, actions?:string}} o
   *   actions:该分区的操作(如"新建用户")—— 放在标题行右侧,
   *   使按钮与它作用的列表同处一个容器(而非挂在页面级页头)
   */
  function sectionTitle(o) {
    o = o || {};
    return '<div class="section-title">' +
      (o.icon ? icon(o.icon) + ' ' : '') + esc(o.title) +
      (o.actions ? '<div class="section-actions">' + o.actions + '</div>' : '') +
      '</div>';
  }

  /**
   * 分段控件 HTML。点击由各视图自行委托(工厂不接管业务)。
   * @param {{items:Array<{key:string,label:string,icon?:string,count?:number,disabled?:boolean}>,
   *          active:string, auto?:boolean, id?:string, role?:string, cls?:string}} o
   *   auto:按内容排布不拉伸(段数少的场景);role 传 'tablist' 可加 aria 语义
   */
  function seg(o) {
    o = o || {};
    return '<div class="seg' + (o.auto ? ' auto' : '') + (o.cls ? ' ' + o.cls : '') + '"' +
      (o.id ? ' id="' + esc(o.id) + '"' : '') +
      (o.role ? ' role="' + esc(o.role) + '"' : '') + '>' +
      (o.items || []).map(function (it) {
        var on = it.key === o.active;
        return '<button type="button" data-key="' + esc(it.key) + '"' +
          (o.role === 'tablist' ? ' role="tab" aria-selected="' + on + '"' : '') +
          ' class="' + (on ? 'active' : '') + '"' + (it.disabled ? ' disabled' : '') + '>' +
          (it.icon ? icon(it.icon) : '') + '<span>' + esc(it.label) + '</span>' +
          (it.count ? badge({ text: it.count, count: true, cls: 'tint' }) : '') + '</button>';
      }).join('') + '</div>';
  }

  /**
   * 列表行 HTML(弹性型:图标/头像 + 标题 + 副文本 + 徽标 + 操作)。
   * 需要"列与列对齐"的场景请改用 tableHead/tableRow。
   * @param {{icon?:string, iconCls?:string, avatar?:string, title:string, sub?:string,
   *          badges?:string, meta?:string, actions?:string, raised?:boolean,
   *          hoverable?:boolean, selected?:boolean, attrs?:string, cls?:string,
   *          tag?:string, href?:string, subClamp?:boolean}} o
   *   iconCls:图标配色类(fi-pdf 等);avatar:UI.avatar() 的产物,与 icon 二选一
   *   attrs:附加属性串(如 data-id="x");sub/badges/meta/actions 均为已拼好的 HTML
   *   tag/href:整行作为链接时传 tag:'a' + href(搜索结果卡片)
   *   subClamp:副文本最多两行(用于展示较长的片段)
   */
  function listRow(o) {
    o = o || {};
    var cls = ['list-row', o.raised ? 'raised' : '', o.hoverable ? 'hoverable' : '',
      o.selected ? 'selected' : '', o.cls || ''].filter(Boolean).join(' ');
    var tag = o.tag || 'div';
    return '<' + tag + ' class="' + cls + '"' +
      (tag === 'a' && o.href ? ' href="' + esc(o.href) + '"' : '') +
      (o.attrs ? ' ' + o.attrs : '') + '>' +
      (o.avatar || '') +
      (o.icon ? '<i class="row-icon ' + esc(o.iconCls || '') + ' ' + ('ri-' + String(o.icon).replace(/^ri-/, '')) + '"></i>' : '') +
      '<div class="list-row-main">' +
      '<div class="list-row-title">' + o.title + (o.badges ? ' ' + o.badges : '') + '</div>' +
      (o.sub ? '<div class="list-row-sub' + (o.subClamp ? ' clamp-2' : '') + '">' + o.sub + '</div>' : '') +
      '</div>' +
      (o.meta || '') +
      (o.actions ? '<div class="list-row-acts">' + o.actions + '</div>' : '') +
      '</' + tag + '>';
  }

  /* ---------- 数据表格工厂 ---------- */

  /**
   * 表头 HTML。列宽通过 style 变量 --tpl 注入到 .data-table 上(表头与数据行共用)。
   * @param {Array<{html:string, cls?:string}>} cols 各列内容(已拼好的 HTML)
   * @param {{tpl?:string, batch?:string, id?:string, cls?:string, checkAll?:string}} [o]
   *   batch:批量操作区 HTML(选中时同一行原地变身,列表不位移)
   *   checkAll:全选 checkbox 的 HTML(省略则无首列复选框)
   */
  function tableHead(cols, o) {
    o = o || {};
    var style = o.tpl ? ' style="--tpl:' + o.tpl + '"' : '';
    return '<div class="data-table' + (o.cls ? ' ' + o.cls : '') + '"' +
      (o.id ? ' id="' + esc(o.id) + '"' : '') + style + '>' +
      '<div class="data-table-head"' + (o.headId ? ' id="' + esc(o.headId) + '"' : '') + '>' +
      (o.checkAll ? '<div class="cell-check">' + o.checkAll + '</div>' : '') +
      cols.map(function (c) { return '<div class="head-col' + (c.cls ? ' ' + c.cls : '') + '">' + c.html + '</div>'; }).join('') +
      (o.batch ? '<div class="batch">' + o.batch + '</div>' : '') +
      '</div>';
  }

  /**
   * 数据行 HTML。
   * @param {Array<{html:string, cls?:string}>} cells
   * @param {{check?:string, acts?:string, attrs?:string, selected?:boolean}} [o]
   *   check:复选框 HTML;acts:操作按钮 HTML(自动包进 .row-acts)
   */
  function tableRow(cells, o) {
    o = o || {};
    return '<div class="data-table-row' + (o.selected ? ' selected' : '') + '"' + (o.attrs ? ' ' + o.attrs : '') + '>' +
      (o.check ? '<div class="cell-check">' + o.check + '</div>' : '') +
      cells.map(function (c) { return '<div class="' + (c.cls || '') + '">' + c.html + '</div>'; }).join('') +
      (o.acts ? '<div class="row-acts">' + o.acts + '</div>' : '') +
      '</div>';
  }

  /** 单元格:名称(图标/头像 + 文本按钮 + 徽标) */
  function cellName(o) {
    o = o || {};
    return '<div class="cell-name">' +
      (o.avatar || '') +
      (o.icon ? '<i class="row-icon ' + esc(o.iconCls || '') + ' ' + ('ri-' + String(o.icon).replace(/^ri-/, '')) + '"></i>' : '') +
      (o.link !== undefined ? o.link : '<span class="list-row-title">' + (o.text || '') + '</span>') +
      (o.badges || '') + '</div>';
  }

  /** 单元格:次要信息(大小/时间/日期) */
  function cellMeta(html, cls) {
    return '<div class="cell-meta ' + (cls || '') + '">' + html + '</div>';
  }

  /** 单元格:身份(头像 + 主文本 + 副文本),用于用户/成员表 */
  function cellId(o) {
    o = o || {};
    return '<div class="cell-id">' + (o.avatar || '') +
      '<div class="cell-id-main">' +
      '<div class="cell-id-name">' + (o.title || '') + '</div>' +
      (o.sub ? '<div class="cell-id-sub">' + o.sub + '</div>' : '') +
      '</div></div>';
  }

  /**
   * 可点卡片 HTML(整卡为一个链接,如项目卡)。
   * @param {{href:string, name:string, badges?:string, desc?:string,
   *          meta?:Array<{icon?:string, text:string}>, metaHtml?:string, attrs?:string}} o
   *   meta:自动拼为"图标 + 文本"序列(空项跳过);metaHtml 用于需要徽标等富内容的场合,
   *   两者可同时给(meta 在前、metaHtml 在后)
   */
  function cardLink(o) {
    o = o || {};
    var meta = '';
    if (o.meta) {
      meta = o.meta.filter(function (m) { return m && (m.text || m.icon); }).map(function (m) {
        return '<span>' + (m.icon ? icon(m.icon) : '') + esc(m.text || '') + '</span>';
      }).join('');
    }
    meta += o.metaHtml || '';
    return '<a class="card-link" href="' + esc(o.href) + '"' + (o.attrs ? ' ' + o.attrs : '') + '>' +
      '<div class="card-link-name-row"><span class="card-link-name">' + esc(o.name) + '</span>' +
      (o.badges || '') + '</div>' +
      '<div class="card-link-desc">' + esc(o.desc || '') + '</div>' +
      (meta ? '<div class="card-link-meta">' + meta + '</div>' : '') +
      '</a>';
  }

  /* ---------- Toast ---------- */
  var TOAST_ICON = {
    success: 'checkbox-circle-line',
    danger: 'error-warning-line',
    error: 'error-warning-line',
    warning: 'alert-line',
    info: 'information-line',
  };
  /** 底部居中 snackbar;type: info/success/warning/danger */
  function toast(msg, type) {
    type = type || 'info';
    var root = document.getElementById('toast-root');
    if (!root) return;
    var el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.innerHTML = icon(TOAST_ICON[type] || TOAST_ICON.info);
    var span = document.createElement('span');
    span.textContent = msg;
    el.appendChild(span);
    root.appendChild(el);
    setTimeout(function () {
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 250);
    }, 3200);
  }
  /** 统一错误提示(ApiError.message) */
  function err(e) { toast((e && e.message) || '操作失败', 'danger'); }

  /* ---------- 模态框 ---------- */
  var ACTION_CLASS = {
    filled: 'btn btn-filled',
    tonal: 'btn btn-tonal',
    outline: 'btn btn-outline',
    text: 'btn btn-text',
    danger: 'btn btn-danger',
    'danger-outline': 'btn btn-danger-outline',
  };

  // 模态框栈:Esc 只关闭最上层
  var modalStack = [];
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modalStack.length) {
      e.stopPropagation();
      modalStack[modalStack.length - 1].close(null);
    }
  }, true);

  /**
   * 通用模态框:esc 与遮罩点击均可关闭。
   * @param {{title:string, body:string|HTMLElement, wide?:boolean,
   *          actions?:Array<{label:string, kind?:string, value?:any, handler?:Function}>,
   *          onClose?:Function}} opts
   * @returns {{el:HTMLElement, box:HTMLElement, body:HTMLElement, close:Function, result:Promise}}
   *   actions 无 handler 时点击即 close(value);result 在关闭时兑现(遮罩/esc 关闭兑现 null)。
   */
  function modal(opts) {
    opts = opts || {};
    var mask = document.createElement('div');
    mask.className = 'modal-mask';
    var box = document.createElement('div');
    box.className = 'modal-box' + (opts.wide ? ' wide' : '');
    box.setAttribute('role', 'dialog');
    box.tabIndex = -1;
    mask.appendChild(box);

    var resolveResult;
    var result = new Promise(function (res) { resolveResult = res; });
    var closed = false;
    var handle = { el: mask, box: box, body: null, close: close, result: result };

    function close(value) {
      if (closed) return;
      closed = true;
      var idx = modalStack.indexOf(handle);
      if (idx >= 0) modalStack.splice(idx, 1);
      mask.classList.add('closing');
      setTimeout(function () { mask.remove(); }, 140);
      resolveResult(value === undefined ? null : value);
      if (opts.onClose) { try { opts.onClose(value === undefined ? null : value); } catch (e) { /* 忽略 */ } }
    }
    mask.addEventListener('pointerdown', function (e) { if (e.target === mask) close(null); });

    var head = document.createElement('div');
    head.className = 'modal-head';
    head.innerHTML = '<h3>' + esc(opts.title || '') + '</h3>';
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn-icon';
    closeBtn.title = '关闭';
    closeBtn.innerHTML = icon('close-line');
    closeBtn.addEventListener('click', function () { close(null); });
    head.appendChild(closeBtn);
    box.appendChild(head);

    var body = document.createElement('div');
    body.className = 'modal-body';
    if (typeof opts.body === 'string') body.innerHTML = opts.body;
    else if (opts.body) body.appendChild(opts.body);
    box.appendChild(body);

    if (opts.actions && opts.actions.length) {
      var foot = document.createElement('div');
      foot.className = 'modal-foot';
      opts.actions.forEach(function (a) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = ACTION_CLASS[a.kind] || ACTION_CLASS.text;
        btn.textContent = a.label;
        btn.addEventListener('click', function () {
          if (a.handler) a.handler({ close: close, el: mask, body: body, btn: btn });
          else close(a.value);
        });
        foot.appendChild(btn);
      });
      box.appendChild(foot);
    }

    handle.body = body;
    document.body.appendChild(mask);
    modalStack.push(handle);
    var focusable = body.querySelector('input, select, textarea');
    if (focusable) { focusable.focus(); if (focusable.select) focusable.select(); }
    else box.focus();
    return handle;
  }

  /**
   * 确认对话框,返回 Promise<boolean>。
   * @param {string} message
   * @param {{title?:string, okText?:string, danger?:boolean}} [opts]
   */
  function confirmDialog(message, opts) {
    opts = opts || {};
    return modal({
      title: opts.title || '确认操作',
      body: '<p class="modal-text">' + esc(message) + '</p>',
      actions: [
        { label: '取消', kind: 'text', value: false },
        { label: opts.okText || '确定', kind: opts.danger === false ? 'filled' : 'danger', value: true },
      ],
    }).result.then(function (v) { return v === true; });
  }

  /**
   * 输入对话框,返回 Promise<string|null>(确定 → 输入值 trim 后;取消/关闭 → null)。
   * @param {{title:string, label?:string, value?:string, type?:string,
   *          placeholder?:string, help?:string, okText?:string}} opts
   */
  function inputDialog(opts) {
    opts = opts || {};
    var bodyHtml =
      (opts.label ? '<div class="field flush"><label>' + esc(opts.label) + '</label>' : '<div>') +
      '<input class="input" id="idlg-input" type="' + esc(opts.type || 'text') + '"' +
      ' value="' + esc(opts.value || '') + '" placeholder="' + esc(opts.placeholder || '') + '" autocomplete="off">' +
      (opts.help ? '<div class="help">' + esc(opts.help) + '</div>' : '') +
      '</div>';
    var m = modal({
      title: opts.title,
      body: bodyHtml,
      actions: [
        { label: '取消', kind: 'text', value: null },
        { label: opts.okText || '确定', kind: 'filled', value: '__ok__' },
      ],
    });
    var input = m.body.querySelector('#idlg-input');
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); m.close(input.value.trim()); }
    });
    return m.result.then(function (v) {
      return v === '__ok__' ? input.value.trim() : (typeof v === 'string' ? v : null);
    });
  }

  /**
   * 表单模态框:统一"拼字段 → 取值 → 必填校验 → 提交 → 错误提示"样板。
   * @param {{title:string, fields:Array, okText?:string, okKind?:string, submit:Function}} opts
   *   fields 元素:{name, label?, type?, value?, placeholder?, required?, maxlength?,
   *              rows?, options?, disabled?, help?, checkbox?, autocomplete?}
   *     type: text(默认) | email | password | textarea | select | checkbox
   *     select 用 options:[{value,label}];checkbox 用 checkbox:'行内文字'
   *   submit(values, {close, btn, body}) 为 async;抛错自动 UI.err 并恢复按钮
   *   提交成功后自行调用 close(true)
   * @returns 同 modal()(result 在取消/关闭时兑现 null)
   */
  function formModal(opts) {
    opts = opts || {};
    var fields = opts.fields || [];

    function fieldHtml(f) {
      var id = 'fm-' + f.name;
      var fid = ' data-field="' + esc(f.name) + '"';
      // checkbox 同样包进 .field:否则它会直接贴住上一个字段(缺一个字段间距)
      if (f.type === 'checkbox') {
        return '<div class="field"><label class="check-row"><input type="checkbox" id="' + id + '"' + fid +
          (f.value ? ' checked' : '') + '>' + esc(f.checkbox || f.label || '') + '</label></div>';
      }
      var label = f.label
        ? '<label for="' + id + '">' + esc(f.label) + (f.required ? ' *' : '') + '</label>' : '';
      var body;
      if (f.type === 'textarea') {
        body = '<textarea class="textarea" id="' + id + '"' + fid + ' rows="' + (f.rows || 2) + '"' +
          (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : '') +
          (f.maxlength ? ' maxlength="' + f.maxlength + '"' : '') +
          (f.disabled ? ' disabled' : '') + '>' + esc(f.value || '') + '</textarea>';
      } else if (f.type === 'select') {
        body = '<select class="select" id="' + id + '"' + fid + (f.disabled ? ' disabled' : '') + '>' +
          (f.options || []).map(function (o) {
            return '<option value="' + esc(o.value) + '"' +
              (o.value === f.value ? ' selected' : '') + '>' + esc(o.label) + '</option>';
          }).join('') + '</select>';
      } else {
        body = '<input class="input" id="' + id + '"' + fid + ' type="' + esc(f.type || 'text') + '"' +
          ' value="' + esc(f.value == null ? '' : f.value) + '"' +
          (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : '') +
          (f.maxlength ? ' maxlength="' + f.maxlength + '"' : '') +
          ' autocomplete="' + esc(f.autocomplete || 'off') + '"' +
          (f.disabled ? ' disabled' : '') + '>';
      }
      return '<div class="field">' + label + body +
        (f.help ? '<div class="help">' + esc(f.help) + '</div>' : '') + '</div>';
    }

    function readValues(bodyEl) {
      var values = {};
      fields.forEach(function (f) {
        var el = bodyEl.querySelector('[data-field="' + f.name + '"]');
        if (!el) return;
        values[f.name] = f.type === 'checkbox' ? el.checked : String(el.value);
      });
      return values;
    }

    return modal({
      title: opts.title,
      body: '<div class="form-modal">' + fields.map(fieldHtml).join('') + '</div>',
      actions: [
        { label: '取消', kind: 'text', value: null },
        {
          label: opts.okText || '确定', kind: opts.okKind || 'filled',
          handler: function (ctx) {
            var values = readValues(ctx.body);
            for (var i = 0; i < fields.length; i++) {
              var f = fields[i];
              if (!f.required || f.type === 'checkbox') continue;
              var v = values[f.name];
              if (v === '' || v == null) {
                toast((f.label || f.name) + '不能为空', 'warning');
                return;
              }
            }
            ctx.btn.disabled = true;
            Promise.resolve()
              .then(function () { return opts.submit(values, ctx); })
              .catch(function (e) { ctx.btn.disabled = false; err(e); });
          },
        },
      ],
    });
  }

  /* ---------- 选人器 ---------- */

  /**
   * 同事选择器:弹窗 + 搜索框 + 头像列表,返回选中的用户对象(Promise)。
   * 取代"让用户手工输入同事邮箱"——没人记得住别人的邮箱,而拼错只会得到 404。
   * @param {{title?:string, exclude?:string[], excludeIds?:string[]}} o
   *   exclude:要排除的邮箱(如已在项目中的成员);excludeIds:要排除的用户 id
   */
  function personPicker(o) {
    o = o || {};
    var exclude = {};
    (o.exclude || []).forEach(function (e) { exclude[String(e).toLowerCase()] = 1; });
    var excludeIds = {};
    (o.excludeIds || []).forEach(function (i) { excludeIds[i] = 1; });
    var m = modal({
      title: o.title || '选择成员',
      body:
        '<div class="field flush"><input class="input" id="pp-q"' +
        ' type="text" placeholder="搜索姓名或邮箱" autocomplete="off"></div>' +
        '<div id="pp-list" class="pp-list">' + loadingRow() + '</div>',
    });
    var qEl = m.body.querySelector('#pp-q');
    var listEl = m.body.querySelector('#pp-list');
    var all = [];

    function render() {
      var q = (qEl.value || '').trim().toLowerCase();
      var rows = all.filter(function (u) {
        if (excludeIds[u.id]) return false;
        if (exclude[String(u.email || '').toLowerCase()]) return false;
        if (!q) return true;
        return (u.name || '').toLowerCase().indexOf(q) >= 0 ||
          (u.email || '').toLowerCase().indexOf(q) >= 0;
      });
      if (!rows.length) {
        listEl.innerHTML = '<div class="pp-empty">' + (all.length ? '没有匹配的同事' : '暂无可选同事') + '</div>';
        return;
      }
      listEl.innerHTML = rows.map(function (u) {
        return '<button type="button" class="pp-item" data-uid="' + esc(u.id) + '">' +
          avatar({ name: u.name || u.email, seed: u.id, color: u.avatarColor, size: 'lg' }) +
          '<span class="pp-main"><span class="pp-name">' + esc(u.name || '') + '</span>' +
          '<span class="pp-mail">' + esc(u.email || '') + '</span></span>' +
          '</button>';
      }).join('');
    }

    listEl.addEventListener('click', function (e) {
      var b = e.target.closest('.pp-item');
      if (!b) return;
      var u = all.filter(function (x) { return x.id === b.dataset.uid; })[0];
      if (u) m.close(u);
    });
    qEl.addEventListener('input', render);
    qEl.addEventListener('keydown', function (e) {
      // 回车选中第一条可见项,减少"打字 + 找鼠标"的往返
      if (e.key === 'Enter') {
        e.preventDefault();
        var first = listEl.querySelector('.pp-item');
        if (first) first.click();
      }
    });

    api('/api/users/directory').then(function (list) {
      all = list || [];
      render();
      qEl.focus();
    }).catch(function (e) {
      listEl.innerHTML = '<div class="pp-empty">' + esc(e.message || '加载失败') + '</div>';
    });
    return m.result;
  }

  /* ---------- 下拉菜单 ---------- */
  var openMenuCleanup = null;
  function closeOpenMenu() {
    if (openMenuCleanup) { openMenuCleanup(); openMenuCleanup = null; }
  }

  /** 关闭所有模态框。路由切换时调用 —— 模态框挂在 document.body 上,
   *  不会被视图的 innerHTML 替换清掉,不显式关就会出现"导航后旧弹窗盖在新页面上"。 */
  function closeAllModals() {
    // 从后往前关:close() 会改 modalStack
    while (modalStack.length) {
      try { modalStack[modalStack.length - 1].close(null); }
      catch (e) { modalStack.pop(); }
    }
  }

  document.addEventListener('pointerdown', function (e) {
    // 点击菜单外部时关闭(菜单本体与触发器除外)
    if (openMenuCleanup && !e.target.closest('.menu')) closeOpenMenu();
  }, true);

  function positionMenu(menu, trigger, align, direction) {
    var r = trigger.getBoundingClientRect();
    menu.style.visibility = 'hidden';
    var mw = menu.offsetWidth, mh = menu.offsetHeight;
    var left = align === 'end' ? r.right - mw : r.left;
    left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
    var top;
    if (direction === 'up') {
      top = r.top - mh - 6;
      if (top < 8) top = Math.min(r.bottom + 6, window.innerHeight - mh - 8); // 上方放不下则回落到下方
    } else {
      top = r.bottom + 6;
      if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
    }
    menu.style.left = left + 'px';
    menu.style.top = Math.max(8, top) + 'px';
    menu.style.visibility = '';
  }

  /**
   * 下拉菜单:绑定到 trigger 的点击;自动定位、点击外部 / Esc 关闭。
   * items 为数组或返回数组的函数(每次打开时求值,适合内容随状态变化的菜单)。
   * 数组元素:{icon?, label, danger?, disabled?, onClick, keepOpen?} | {divider:true} | {custom:HTMLElement}
   * @returns {Function} close 手动关闭函数
   */
  function dropdownMenu(trigger, items, opts) {
    opts = opts || {};
    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      if (openMenuCleanup && openMenuCleanup._trigger === trigger) { closeOpenMenu(); return; }
      closeOpenMenu();
      show();
    });

    function show() {
      var menu = document.createElement('div');
      menu.className = 'menu';

      var list = typeof items === 'function' ? items() : items;
      (list || []).forEach(function (it) {
        if (!it) return;
        if (it.divider) {
          var dv = document.createElement('div');
          dv.className = 'menu-divider';
          menu.appendChild(dv);
          return;
        }
        if (it.custom) {
          menu.appendChild(it.custom);
          return;
        }
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'menu-item' + (it.danger ? ' danger' : '');
        btn.disabled = !!it.disabled;
        btn.innerHTML = (it.icon ? icon(it.icon) : '') + '<span>' + esc(it.label || '') + '</span>';
        btn.addEventListener('click', function () {
          if (!it.keepOpen) close();
          if (it.onClick) it.onClick();
        });
        menu.appendChild(btn);
      });

      document.body.appendChild(menu);
      positionMenu(menu, trigger, opts.align, opts.direction);

      function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } }
      function onScrollOrResize() { close(); }
      document.addEventListener('keydown', onKey, true);
      window.addEventListener('resize', onScrollOrResize);
      window.addEventListener('scroll', onScrollOrResize, true);

      function close() {
        document.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', onScrollOrResize);
        window.removeEventListener('scroll', onScrollOrResize, true);
        menu.remove();
        if (openMenuCleanup === close) openMenuCleanup = null;
      }
      close._trigger = trigger;
      openMenuCleanup = close;
    }
    return { close: closeOpenMenu };
  }

  return {
    esc: esc,
    debounce: debounce,
    icon: icon,
    fileIcon: fileIcon,
    fmtSize: fmtSize,
    fmtDate: fmtDate,
    fmtDateShort: fmtDateShort,
    copyText: copyText,
    roleRank: roleRank,
    roleLabel: roleLabel,
    hasRole: hasRole,
    canEdit: canEdit,
    canAdmin: canAdmin,
    canOwn: canOwn,
    avatar: avatar,
    spinner: spinner,
    loadingRow: loadingRow,
    emptyState: emptyState,
    toast: toast,
    err: err,
    modal: modal,
    confirmDialog: confirmDialog,
    inputDialog: inputDialog,
    formModal: formModal,
    personPicker: personPicker,
    dropdownMenu: dropdownMenu,
    closeOpenMenu: closeOpenMenu,
    closeAllModals: closeAllModals,
    /* 标记类工厂(HTML 字符串) */
    btn: btn,
    iconBtn: iconBtn,
    badge: badge,
    banner: banner,
    card: card,
    statGrid: statGrid,
    pageHead: pageHead,
    toolbar: toolbar,
    sectionTitle: sectionTitle,
    seg: seg,
    listRow: listRow,
    tableHead: tableHead,
    tableRow: tableRow,
    cellName: cellName,
    cellMeta: cellMeta,
    cellId: cellId,
    cardLink: cardLink,
  };
})();
