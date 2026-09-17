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
   * name 是**裸名**('file-text-line'),前缀由这里加 —— 名字只有这一种写法,
   * 传带前缀的名字会拼出 ri-ri-* 死类名(图标不显示)。
   */
  function icon(name, cls) {
    return '<i class="ri-' + esc(name || '') + (cls ? ' ' + esc(cls) : '') + '"></i>';
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

  /** 自适应时间:今天 10:09 / 昨天 18:03 / 9/11 14:20 / 2025-12-03 09:15。
      给"按时间找东西"的列表用(版本历史):近期的记录只关心几点几分,远期才需要完整日期。
      精度到分钟 —— 同一分钟内的多条靠副文本(作者、字数)区分,需要秒级精确时
      那类列表应另给完整时间的 title 提示。 */
  function fmtWhen(s) {
    if (!s) return '-';
    var d = parseDate(s);
    if (!d) return String(s);
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    var hm = p(d.getHours()) + ':' + p(d.getMinutes());
    var now = new Date();
    // 按"本地零点"算天数差:直接拿时间戳相除会在夏令时那两天算出一个不满一天的小数
    var days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate()) -
                           new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
    if (days === 0) return '今天 ' + hm;
    if (days === 1) return '昨天 ' + hm;
    if (d.getFullYear() === now.getFullYear()) {
      return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
    }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + hm;
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
     myRole 是服务端 project_role 的综合结果:成员 → 成员角色(全局管理员加入后角色
     低于 ADMIN 时兜底 ADMIN —— 管辖不因加入而降级);非成员的全局管理员 → ADMIN;
     其余(含未加入的公开项目)→ null。直接比级即可 —— 非成员没有角色,不可能误过
     EDITOR 及以上的判定;而非成员管理员必须能拿到管理入口(否则"接管失联项目"在
     界面上无从下手,后台项目区的模块直达按钮也依赖它)。 */
  /** 有效角色达到 required(服务端 project_role 的镜像) */
  function hasRole(proj, required) {
    if (!proj) return false;
    return roleRank(proj.myRole) >= roleRank(required);
  }
  /** 可写(EDITOR+):上传/新建/编辑/删除内容、回收站的恢复与彻底删除 */
  function canEdit(proj) { return hasRole(proj, 'EDITOR'); }
  /** 能进入并读取(VIEWER+):真成员与全局管理员;公开项目**加入前** myRole 为
   *  null 过不了这一档(镜像服务端 require_project_role("VIEWER"))。发现页的
   *  "先问要不要加入"不用它 —— 那里看 isMember(参与语义:加入才需要问;
   *  管辖直达走管理后台的模块直达按钮,不经广场)。 */
  function canRead(proj) { return hasRole(proj, 'VIEWER'); }
  /** 可管项目(ADMIN+):改项目信息、管理成员、公开开关与加入角色、跨项目移动的源项目 */
  function canAdmin(proj) { return hasRole(proj, 'ADMIN'); }
  /** OWNER 级管辖权(授 OWNER / 删除项目):镜像 auth.is_project_owner_or_admin
   *  + 端点依赖(ADMIN 起步)——真所有者;全局管理员按 myRole 判(非个人项目里
   *  myRole 恒 ≥ ADMIN:max 语义下加入不降级、非成员兜底 ADMIN)。个人空间对
   *  管理员也关闭,与个人空间保护一致。 */
  function canOwn(proj) {
    if (!proj || proj.isPersonal) return false;
    if (App.user && App.user.isAdmin) return roleRank(proj.myRole) >= roleRank('ADMIN');
    return roleRank(proj.myRole) >= roleRank('OWNER');
  }

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
    return '<span class="' + cls + '"' + (o.title ? ' title="' + esc(o.title) + '"' : '') + '>' +
      esc(o.text == null ? '' : o.text) + '</span>';
  }

  /**
   * 提示条 HTML(list-warn / remote-bar / login-err 三套同构实现的统一替代)。
   * @param {{text:string, kind?:'warn'|'danger'|'info'|'primary', icon?:string,
   *          sm?:boolean, id?:string, cls?:string,
   *          action?:{label:string, kind?:string, id?:string}}} o
   *   text 为纯文本(需富文本时调用方自行拼接后传入 html)
   */
  var BANNER_ICON = { danger: 'error-warning-line', warn: 'alert-line', info: 'information-line' };
  function banner(o) {
    o = o || {};
    var cls = ['banner', o.kind || 'info', o.sm ? 'sm' : '', o.cls || ''].filter(Boolean).join(' ');
    // 图标缺省按 kind 取默认:danger/warn 的图标不再靠每个调用点记忆
    var ic = o.icon || BANNER_ICON[o.kind || 'info'] || '';
    return '<div class="' + cls + '"' + (o.id ? ' id="' + esc(o.id) + '"' : '') +
      (o.hidden ? ' hidden' : '') + '>' +
      (ic ? icon(ic) : '') +
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
      (o.icon ? icon(o.icon, 'row-icon' + (o.iconCls ? ' ' + o.iconCls : '')) : '') +
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
      (o.icon ? icon(o.icon, 'row-icon' + (o.iconCls ? ' ' + o.iconCls : '')) : '') +
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

  /** 项目卡的 "N 成员 · N 文档" 计数行(发现页与项目列表页同款;
   * 图标独占一格是为了图标与计数之间保留 meta 行的间距) */
  function projectCountMeta(p) {
    return [
      { icon: 'team-line', text: '' },
      { text: (p.memberCount != null ? p.memberCount : '-') + ' 成员' },
      { text: '·' },
      { text: (p.docCount != null ? p.docCount : '-') + ' 文档' },
    ];
  }

  /* ---------- Toast ---------- */
  var TOAST_ICON = {
    success: 'checkbox-circle-line',
    danger: 'error-warning-line',
    warning: 'alert-line',
    info: 'information-line',
  };
  /** 底部居中 snackbar;type: info/success/warning/danger(CSS 只认这四档,勿造第五种) */
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
        if (a.id) btn.id = a.id;
        btn.className = ACTION_CLASS[a.kind] || ACTION_CLASS.text;
        btn.textContent = a.label;
        btn.addEventListener('click', function () {
          // 有 onClick 则由回调决定何时关闭;无则点击即 close(value)
          if (a.onClick) a.onClick({ close: close, el: mask, body: body, btn: btn });
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
   * @param {{title:string, label?:string, value?:string, type?:string, autocomplete?:string,
   *          placeholder?:string, help?:string, okText?:string}} opts
   *   autocomplete:密码类输入必须显式给令牌(如 new-password)—— 默认的 off 会被浏览器忽略,
   *                反而可能把操作者自己的密码回填进来
   */
  function inputDialog(opts) {
    opts = opts || {};
    var bodyHtml =
      (opts.label ? '<div class="field flush"><label>' + esc(opts.label) + '</label>' : '<div>') +
      '<input class="input" id="idlg-input" type="' + esc(opts.type || 'text') + '"' +
      ' value="' + esc(opts.value || '') + '" placeholder="' + esc(opts.placeholder || '') + '"' +
      ' autocomplete="' + esc(opts.autocomplete || 'off') + '">' +
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
          onClick: function (ctx) {
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
  /** 同事选人浮层。
   *  单选(默认):点人即关闭,m.result 解析为该用户;
   *  multi:true:点人切换候选态(可反复勾选),底部「确认」按钮带计数,
   *  m.result 解析为用户数组。excludeIds:要排除的用户 id(如已在项目中的成员);
   *  身份键只有 id,与后端一致。搜索框按 姓名 / id / 邮箱 匹配。
   *  roleSelect:{options:[{value,label}], value}:multi 下每个候选人有独立角色下拉
   *  (默认取 roleSelect.value),确认时各项带各自 .role —— 批量授权各自可选。 */
  function personPicker(o) {
    o = o || {};
    var multi = !!o.multi;
    var roleSel = o.roleSelect || null;
    var excludeIds = {};
    (o.excludeIds || []).forEach(function (i) { excludeIds[i] = 1; });
    var picked = {}; // multi 模式的候选区:uid -> user(带各自的 role)

    var confirmBtn = multi
      ? { id: 'pp-confirm', label: (o.confirmLabel || '添加所选') + '(0)', kind: 'filled',
          onClick: function (ctx) {
            var list = Object.keys(picked).map(function (k) { return picked[k]; });
            if (!list.length) return; // 空选不关闭,给用户继续勾选的机会
            ctx.close(list); // 各候选人的 .role 已在勾选/调整时就绪
          } }
      : null;

    var m = modal({
      title: o.title || '选择成员',
      body:
        '<div class="field flush"><input class="input" id="pp-q"' +
        ' type="text" placeholder="搜索姓名或邮箱" autocomplete="off"></div>' +
        '<div id="pp-list" class="pp-list">' + loadingRow() + '</div>' +
        '<div id="pp-picked"></div>',
      actions: confirmBtn ? [confirmBtn] : [],
    });
    var qEl = m.body.querySelector('#pp-q');
    var listEl = m.body.querySelector('#pp-list');
    var all = [];
    var confirmEl = confirmBtn ? m.box.querySelector('#pp-confirm') : null;
    updateConfirm();

    function updateConfirm() {
      if (!confirmEl) return;
      var n = Object.keys(picked).length;
      confirmEl.textContent = (o.confirmLabel || '添加所选') + '(' + n + ')';
      confirmEl.disabled = !n;
    }

    function render() {
      var q = (qEl.value || '').trim().toLowerCase();
      var rows = all.filter(function (u) {
        if (excludeIds[u.id]) return false;
        if (!q) return true;
        return (u.name || '').toLowerCase().indexOf(q) >= 0 ||
          (u.email || '').toLowerCase().indexOf(q) >= 0 ||
          String(u.id).indexOf(q) >= 0;
      });
      if (!rows.length) {
        listEl.innerHTML = '<div class="pp-empty">' + (all.length ? '没有匹配的同事' : '暂无可选同事') + '</div>';
        return;
      }
      listEl.innerHTML = rows.map(function (u) {
        var sel = !!picked[u.id];
        // multi+roleSelect:勾选的人在行内直接配角色(与成员列表"每人一个角色框"一致)
        var roleHtml = (multi && roleSel && sel)
          ? '<select class="select pp-role-per" data-uid="' + esc(u.id) + '">' +
            roleSel.options.map(function (op) {
              return '<option value="' + esc(op.value) + '"' +
                (op.value === picked[u.id].role ? ' selected' : '') + '>' + esc(op.label) + '</option>';
            }).join('') +
            '</select>'
          : '';
        var check = multi
          ? '<i class="pp-check ri-' + (sel ? 'checkbox-circle-fill' : 'checkbox-blank-circle-line') + '"></i>'
          : '';
        // select 不能嵌在 button 里:multi 行用 div,单选行保持 button
        var openTag = multi
          ? '<div class="pp-item' + (sel ? ' selected' : '') + '" data-uid="' + esc(u.id) + '">'
          : '<button type="button" class="pp-item" data-uid="' + esc(u.id) + '">';
        return openTag +
          avatar({ name: u.name || u.email, seed: u.id, color: u.avatarColor, size: 'lg' }) +
          '<span class="pp-main"><span class="pp-name">' + esc(u.name || '') + '</span>' +
          '<span class="pp-mail">' + esc(u.email || '') + '</span></span>' +
          roleHtml + check + (multi ? '</div>' : '</button>');
      }).join('');
    }

    /** 行内角色下拉变更:更新候选人的角色(select 的 change 不触发行切换) */
    listEl.addEventListener('change', function (e) {
      var sel = e.target.closest('.pp-role-per');
      if (!sel) return;
      var uid = sel.dataset.uid;
      if (picked[uid]) picked[uid].role = sel.value;
    });

    listEl.addEventListener('click', function (e) {
      // 行内角色框的交互不触发行切换
      if (e.target.closest('.pp-role-per')) return;
      var b = e.target.closest('.pp-item');
      if (!b) return;
      var u = all.filter(function (x) { return String(x.id) === String(b.dataset.uid); })[0];
      if (!u) return;
      if (multi) {
        // 批量模式:点击切换候选态,不关闭;确认按钮统一提交
        if (picked[u.id]) delete picked[u.id];
        else picked[u.id] = Object.assign({}, u, { role: roleSel ? roleSel.value : undefined });
        render();
        updateConfirm();
        return;
      }
      m.close(u);
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

    api(Endpoints.directory()).then(function (list) {
      all = list || [];
      render();
      qEl.focus();
    }).catch(function (e) {
      listEl.innerHTML = '<div class="pp-empty">' + esc(e.message || '加载失败') + '</div>';
    });
    return m.result;
  }

  /* ---------- 浮层基座(下拉菜单 / 编辑器面板共用) ---------- */
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

  /**
   * 浮层定位:视口钳位(左右不出屏;上下按 direction 取位,放不下换一侧)。
   * rect 锚定时(编辑器面板传光标矩形)还会按可用空间收缩 maxHeight,
   * 保证长列表整体不出屏 —— 钳位逻辑只有这一份,引用浮层与编辑器面板共用。
   */
  function positionLayer(el, anchor, opts) {
    opts = opts || {};
    el.style.visibility = 'hidden';
    var mw = el.offsetWidth, mh = el.offsetHeight;
    var left = opts.align === 'end' ? anchor.right - mw : anchor.left;
    left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
    var top;
    if (opts.direction === 'up') {
      top = anchor.top - mh - 6;
      if (top < 8) top = Math.min(anchor.bottom + 6, window.innerHeight - mh - 8); // 上方放不下则回落到下方
    } else if (opts.rect) {
      // 光标锚定:两侧取空间更大的一侧,并按可用空间收缩高度
      var spaceBelow = window.innerHeight - anchor.bottom - 14;
      var spaceAbove = anchor.top - 14;
      var below = spaceBelow >= spaceAbove;
      el.style.maxHeight = Math.min(320, Math.max(120, below ? spaceBelow : spaceAbove)) + 'px';
      mh = el.offsetHeight;
      top = below ? anchor.bottom + 6 : anchor.top - mh - 6;
    } else {
      top = anchor.bottom + 6;
      if (top + mh > window.innerHeight - 8) top = Math.max(8, anchor.top - mh - 6);
    }
    el.style.left = left + 'px';
    el.style.top = Math.max(8, Math.min(top, window.innerHeight - mh - 8)) + 'px';
    el.style.visibility = '';
  }

  /**
   * 浮层生命周期:点外部 / Esc / 滚动 / 缩放关闭,并纳入全站单例互斥。
   * 「浮层自身列表的滚动」不算 —— 键盘导航滚动列表时不能把自己关掉。
   * @returns {Function} close(移除元素并解绑)
   */
  function bindLayerClosers(el, onClose) {
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

  /**
   * 打开一个浮层:定位 + 生命周期 + 单例互斥一把抓。
   * @param {HTMLElement} el 已填好内容的元素(样式类需含 menu)
   * @param {{rect?:DOMRect, trigger?:HTMLElement, align?:string, direction?:string}} o
   *   rect 与 trigger 二选一:前者锚定任意矩形(编辑器光标),后者锚定元素(下拉菜单)
   * @returns {{close:Function, reposition:Function}}
   */
  function floatingLayer(el, o) {
    o = o || {};
    document.body.appendChild(el);
    var anchor = o.rect || (o.trigger && o.trigger.getBoundingClientRect());
    var closed = false;
    function reposition() { if (!closed && anchor) positionLayer(el, anchor, o); }
    reposition();
    function close() {
      if (closed) return;
      closed = true;
      unbind();
      el.remove();
      if (openMenuCleanup === close) openMenuCleanup = null;
    }
    var unbind = bindLayerClosers(el, close);
    close._trigger = o.trigger || null;
    openMenuCleanup = close;
    return { close: close, reposition: reposition };
  }

  /** 只滚动列表自身,不用 scrollIntoView(它会连带滚动所有祖先容器,把整页顶跑) */
  function scrollItemIntoView(list, btn) {
    if (!btn) return;
    var lr = list.getBoundingClientRect(), br = btn.getBoundingClientRect();
    if (br.top < lr.top) list.scrollTop -= lr.top - br.top;
    else if (br.bottom > lr.bottom) list.scrollTop += br.bottom - lr.bottom;
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

      var layer = floatingLayer(menu, { trigger: trigger, align: opts.align, direction: opts.direction });
      var close = layer.close;
    }
    return { close: closeOpenMenu };
  }


  /* ---------- 列表浮层(引用搜索 / 斜杠菜单等"键盘可导航的候选列表") ---------- */

  /**
   * 在浮层基座之上的候选列表:渲染 items、上下键环取、回车/点击选中、active 滚动跟随。
   * 下拉菜单(dropdownMenu)是静态动作列表,这里是"可过滤/可异步搜索的候选"。
   * @param {{rect:DOMRect, cls?:string, search?:{placeholder:string},
   *          items:Array|Function, itemHtml:(it,i)=>string, groupOf?:(it)=>string,
   *          emptyHtml?:(q)=>string, onPick:Function, keyTarget?:HTMLElement,
   *          tabPicks?:boolean, onBackspaceEmpty?:Function, onClose?:Function}} o
   *   search 给出时面板内建输入框;输入事件经 o.onInput(q, handle) 驱动查询
   *   (自行 fetch 后调 handle.render(items))。静态列表直接传 items。
   *   keyTarget:键盘事件源(斜杠菜单是 textarea;默认为面板内输入框)。
   * @returns {{render:Function, close:Function}}
   */
  function menuList(o) {
    var el = document.createElement('div');
    el.className = 'menu panel' + (o.cls ? ' ' + o.cls : '');
    el.innerHTML =
      (o.search ? '<input class="input menu-search" type="text" placeholder="' + esc(o.search.placeholder || '搜索…') + '" autocomplete="off">' : '') +
      '<div class="menu-scroll"></div>';
    var list = el.querySelector('.menu-scroll');
    var input = el.querySelector('.menu-search');
    var keyTarget = o.keyTarget || input;
    var items = [], active = -1;

    var layer = floatingLayer(el, { rect: o.rect });

    function setActive(i) {
      active = i;
      var btns = list.querySelectorAll('.menu-item');
      btns.forEach(function (b, j) { b.classList.toggle('active', j === i); });
      scrollItemIntoView(list, btns[i]);
    }

    /** 重渲列表并复位选中;异步结果到达后再调本函数即可(会重新定位视口) */
    function render(rows) {
      items = rows || [];
      active = items.length ? 0 : -1;
      if (!items.length) {
        list.innerHTML = o.emptyHtml ? o.emptyHtml(input ? input.value : '')
          : emptyHtml({ sm: true, title: '无匹配结果' });
        layer.reposition();
        return;
      }
      var html = '', lastGroup = '';
      items.forEach(function (r, i) {
        var g = o.groupOf ? o.groupOf(r) : '';
        if (g && g !== lastGroup) {
          lastGroup = g;
          html += '<div class="menu-group">' + esc(g) + '</div>';
        }
        html += '<button type="button" class="menu-item' + (i === active ? ' active' : '') + '" data-i="' + i + '">' +
          o.itemHtml(r, i) + '</button>';
      });
      list.innerHTML = html;
      layer.reposition(); // 结果异步到达/过滤收缩后重新定位,避免按空面板算出的高度翻出屏幕
    }

    function pick(i) {
      var it = items[i];
      layer.close();
      if (it != null && o.onPick) o.onPick(it);
    }

    // 列表交互:mousedown 阻止抢焦点(输入框/textarea 保持焦点),click 选中
    list.addEventListener('mousedown', function (e) { e.preventDefault(); });
    list.addEventListener('click', function (e) {
      var btn = e.target.closest('.menu-item');
      if (btn) pick(Number(btn.dataset.i));
    });

    function onKey(e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) setActive((active + 1) % items.length); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) setActive((active - 1 + items.length) % items.length); }
      else if (e.key === 'Enter' || (o.tabPicks && e.key === 'Tab')) { e.preventDefault(); pick(active); }
      else if (e.key === 'Backspace' && input && !input.value && o.onBackspaceEmpty) {
        e.preventDefault();
        layer.close();
        o.onBackspaceEmpty();
      }
    }
    if (keyTarget) keyTarget.addEventListener('keydown', onKey, true);
    if (input && o.onInput) input.addEventListener('input', function () { o.onInput(input.value.trim(), handle); });
    if (input) input.focus();

    var handle = {
      render: render,
      close: layer.close,
      /** 异步查询回调据此放弃过期渲染(面板已关) */
      get closed() { return !document.body.contains(el); },
    };
    // 关闭时的两件收尾:通知调用方(斜杠菜单要复位状态)、解绑挂在外部元素的键盘监听
    var baseClose = layer.close;
    layer.close = function () {
      if (keyTarget && keyTarget !== input) keyTarget.removeEventListener('keydown', onKey, true);
      baseClose();
      if (o.onClose) o.onClose();
    };
    handle.close = layer.close;
    return handle;
  }

  /* ---------- 视图样板收敛(各视图手写多份的流程,统一到这里) ---------- */

  /** 错误横幅:视图 catch 的标准输出(el.innerHTML = UI.errorBanner(e));
   *  opts 透传 banner 的 sm/cls 等(如小面板内的紧凑档) */
  function errorBanner(e, opts) {
    return banner(Object.assign({ kind: 'danger', text: (e && e.message) || '操作失败' }, opts));
  }

  /**
   * 区块加载三段式(loading → 成功渲染/空态 → 失败横幅)的收敛件。
   * 视图不要再手写 `X.innerHTML = loadingRow(); try { data = await f(); ... } catch`:
   * 各写一份的结果是"失败画在哪"全靠自觉,有的视图出错后页面就一直是骨架。
   * @param el 容器(骨架里的初始 loading 由调用方写,或传 opts.loadingHtml)
   * @param fetcher () => Promise<data>
   * @param opts { render(data) → HTML 字符串;
   *               empty(data) → bool,为真时空态:emptyHtml(字符串)或 emptyNode()(DOM,带按钮的空态);
   *               errorOpts 传给 errorBanner(如 {sm:true}) }
   * @returns Promise<data|undefined>(失败时已画横幅,值为 undefined)
   */
  function loadInto(el, fetcher, opts) {
    opts = opts || {};
    if (opts.loadingHtml != null) el.innerHTML = opts.loadingHtml;
    return Promise.resolve()
      .then(fetcher)
      .then(function (data) {
        el.innerHTML = '';
        if (opts.empty && opts.empty(data)) {
          if (opts.emptyNode) el.appendChild(opts.emptyNode(data));
          else el.innerHTML = opts.emptyHtml || '';
          return data;
        }
        var html = opts.render(data);
        if (html != null) el.innerHTML = html;
        return data;
      })
      .catch(function (e) {
        el.innerHTML = errorBanner(e, opts.errorOpts);
      });
  }

  /** 空态 HTML 字符串(emptyState 的标记类版本;innerHTML 一次赋值,不再两步操作) */
  function emptyHtml(opts) { return emptyState(opts).outerHTML; }

  /**
   * 「确认 → 执行 → toast/err」四段式(全站约 20 份手写样板的收敛)。
   * @param {string} message 确认文案
   * @param {{okText?:string, danger?:boolean, okMsg?:string|false}} [opts] danger 默认 true;
   *   okMsg 传 false 时不 toast(调用方自行提示)
   * @param {Function} fn async 操作本体
   * @returns {Promise<boolean>} 是否成功执行
   */
  async function confirmAction(message, opts, fn) {
    if (typeof opts === 'function') { fn = opts; opts = {}; }
    opts = opts || {};
    if (!(await confirmDialog(message, opts))) return false;
    try {
      await fn();
      if (opts.okMsg !== false) toast(opts.okMsg || '操作成功', 'success');
      return true;
    } catch (e) {
      err(e);
      return false;
    }
  }

  /** seg 点击接线(委托 + active 同步的唯一实现) */
  function segWire(el, onChange) {
    el.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-key]');
      if (!b || b.disabled) return;
      segSet(el, b.dataset.key);
      onChange(b.dataset.key, b);
    });
  }

  /** 把 seg 的 active 态切到指定 key(渲染后补设/外部状态变化时用) */
  function segSet(el, key) {
    el.querySelectorAll('button[data-key]').forEach(function (b) {
      b.classList.toggle('active', b.dataset.key === key);
    });
  }

  /** 批量逐个执行 + 成功/失败计数(批量删除、批量加成员同款流程):
   *  逐项 await fn(item),抛错计为失败并继续,绝不因一项失败而中断整批;
   *  返回 {ok, failed[]} —— failed 收集失败项本身,汇总文案由调用方决定。 */
  function eachOk(items, fn) {
    var ok = 0, failed = [];
    var chain = Promise.resolve();
    (items || []).forEach(function (it) {
      chain = chain.then(function () {
        return Promise.resolve(fn(it)).then(function () { ok++; },
          function () { failed.push(it); });
      });
    });
    return chain.then(function () { return { ok: ok, failed: failed }; });
  }

  /**
   * 锚点下载。不传 filename 时不设 a.download —— 让服务端 Content-Disposition
   * 的文件名生效(备份下载等场景的命名决策在服务端,客户端名会覆盖它)。
   * 同源附件流别用 window.open:可能被拦或开空白页。
   */
  function download(href, opts) {
    opts = opts || {};
    var a = document.createElement('a');
    a.href = href;
    if (opts.filename) a.download = opts.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  /** localStorage 偏好读写(统一 try/catch:隐私模式/超配额下会直接抛) */
  var pref = {
    get: function (key, def) {
      try { var v = localStorage.getItem(key); return v == null ? def : v; } catch (e) { return def; }
    },
    set: function (key, v) {
      try { localStorage.setItem(key, v); return true; } catch (e) { return false; }
    },
    remove: function (key) { try { localStorage.removeItem(key); } catch (e) { /* 忽略 */ } },
  };

  /** id 形状:ULID(26 字符 Crockford Base32,字母表排除 I/L/O/U),与服务端 ids.py
      同一套判定 —— 前端这一层的用处是"坏链接给可见提示",而不是拿着半截字符串去请求。 */
  var ID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

  /** id 归一:形状不对 → null(调用方据此判"这条链接坏了");合法返回大写形态,
      与库里的写法一致(链接被人手工改成小写也认得)。
      **形状判定只有这一处**,别在各个视图里各写一遍正则。 */
  function idOf(v) {
    var s = String(v == null ? '' : v).toUpperCase();
    return ID_RE.test(s) ? s : null;
  }

  /* ---------- mime 判定(正式判定以服务端下发的 canInline/isText 为准;
     这里是"没有服务端标志位的场景"(菜单、编辑器插入)的本地镜像) ---------- */

  function isImage(mime) { return String(mime || '').indexOf('image/') === 0; }
  function isMarkdown(mime, name) {
    return /^text\/(markdown|x-markdown)$/.test(mime || '') || /\.(md|markdown)$/i.test(name || '');
  }
  function canInlineMime(mime) {
    return !!mime && (isImage(mime) || mime === 'application/pdf');
  }
  /* 能否以原生 ![](...) 嵌入正文:图片类型 且 服务端 inline 白名单放行,缺一不可。
     注意 canInline 为 true ≠ 能嵌图 —— 白名单还覆盖 PDF 与全部文本类(md/txt/json/代码…),
     那些类型插 ![](...) 只会得到裂图(浏览器渲染不了 text/markdown),只能插 [@名称](teamdoc://…) chip。
     判定必须带服务端下发的 canInline(svg 是 image/ 但恒强制下载);缺省不嵌,不做本地 mime 猜测。 */
  function isEmbedImage(mime, canInline) {
    return isImage(mime) && !!canInline;
  }

  /* ---------- 面包屑 ---------- */

  /** 面包屑条目:除最后一段(current)外均为按钮,点击经 data-ci 委托。
   *  只返回条目片段 —— `.crumb` 容器由调用方给(它承担 flex 布局与 min-width:0),
   *  本函数不再自带一层 nav.crumb,否则容器会套容器(见 drive.js 的 #drive-crumb)。 */
  function crumbs(items) {
    items = items || [];
    return items.map(function (c, i) {
      var last = i === items.length - 1;
      return last
        ? '<span class="crumb-item current">' + esc(c.label) + '</span>'
        : '<button type="button" class="crumb-item" data-ci="' + i + '">' + esc(c.label) + '</button>';
    }).join('<i class="crumb-sep ri-arrow-right-s-line"></i>');
  }

  /** 面包屑点击接线:onChange(index) */
  function crumbsWire(el, onChange) {
    el.addEventListener('click', function (e) {
      var b = e.target.closest('[data-ci]');
      if (b) onChange(Number(b.dataset.ci));
    });
  }

  /* ---------- 可排序表头 ---------- */

  /**
   * 可排序表头按钮(cur:{key,dir} 为当前排序;非当前列显示中立图标)。
   * 状态重绘用 sortHeadSet(el, cur),两者共用 data-sort/data-label 约定。
   */
  function sortHead(label, key, cur) {
    var ic = cur && cur.key === key ? (cur.dir === 1 ? 'arrow-up-line' : 'arrow-down-line') : 'subtract-line';
    return '<button class="dh-sort" data-sort="' + esc(key) + '" data-label="' + esc(label) +
      '" type="button">' + esc(label) + ' ' + icon(ic) + '</button>';
  }

  function sortHeadSet(el, cur) {
    el.querySelectorAll('.dh-sort').forEach(function (b) {
      var key = b.dataset.sort, label = b.dataset.label || key;
      var ic = cur && cur.key === key ? (cur.dir === 1 ? 'arrow-up-line' : 'arrow-down-line') : 'subtract-line';
      b.innerHTML = esc(label) + ' ' + icon(ic);
    });
  }

  /* ---------- 树(侧栏项目树与文档树共用) ---------- */

  /**
   * 树 HTML 工厂:侧栏项目树(.side-tree-node)与文档树(.doc-ul)共用这一份
   * 「caret 旋转 + 子容器 hidden + dataset id」的渲染与交互。
   * @param {{nodes:Array, children?:(n)=>Array, expanded:(n)=>boolean,
   *          rowCls?:(n)=>string, rowAttrs?:(n)=>string, rowInner:(n)=>string,
   *          rowTag?:string, childrenHtml?:(n)=>string}} o
   *   节点须含 id;children 缺省取 n.children。childrenHtml 覆盖默认的递归子树
   *   (侧栏的子项是导航链接而非树行,用它注入)。行点击/caret 点击由调用方委托。
   * @returns string HTML;结构为 .tree-node > .tree-row[.tree-caret + rowInner]
   *   + .tree-children;无子节点的 caret 带 .leaf。
   */
  function tree(o) {
    function branch(nodes) {
      return (nodes || []).map(function (n) {
        var kids = o.children ? o.children(n) : (n.children || []);
        var open = kids.length > 0 && o.expanded(n);
        var caret = '<span class="tree-caret' + (kids.length ? (open ? ' open' : '') : ' leaf') + '">' +
          icon('arrow-right-s-line') + '</span>';
        var tag = o.rowTag || 'button';
        // rowTag 用 div(行内还要放按钮)时不输出 type 属性
        var typeAttr = tag === 'button' ? ' type="button"' : '';
        return '<div class="tree-node">' +
          '<' + tag + typeAttr + ' class="tree-row' + (o.rowCls ? ' ' + o.rowCls(n) : '') + '"' +
          ' data-id="' + esc(n.id) + '"' + (o.rowAttrs ? ' ' + o.rowAttrs(n) : '') + '>' +
          caret + (o.rowInner ? o.rowInner(n) : '') +
          '</' + tag + '>' +
          (kids.length
            ? '<div class="tree-children"' + (open ? '' : ' hidden') + '>' +
              (o.childrenHtml ? o.childrenHtml(n) : branch(kids)) + '</div>'
            : '') +
          '</div>';
      }).join('');
    }
    return branch(o.nodes);
  }

  /** 深度遍历树(元素须含 children 数组);fn(node, trail) 的 trail 为根到当前节点的
   *  路径(含自身),返回 false 跳过该子树。找路径/铺平/找节点三类遍历共用。 */
  function walkTree(nodes, fn, trail) {
    (nodes || []).forEach(function (n) {
      var t = (trail || []).concat([n]);
      if (fn(n, t) !== false) walkTree(n.children || [], fn, t);
    });
  }

  /** MediaQueryList 监听:窄屏判定与"跟随系统深色"共用的唯一入口。 */
  function onMediaChange(mql, fn) {
    mql.addEventListener('change', fn);
  }

  return {
    esc: esc,
    debounce: debounce,
    icon: icon,
    fileIcon: fileIcon,
    fmtSize: fmtSize,
    fmtDate: fmtDate,
    fmtDateShort: fmtDateShort,
    fmtWhen: fmtWhen,
    copyText: copyText,
    roleRank: roleRank,
    roleLabel: roleLabel,
    canEdit: canEdit,
    canRead: canRead,
    canAdmin: canAdmin,
    canOwn: canOwn,
    avatar: avatar,
    loadingRow: loadingRow,
    emptyState: emptyState,
    emptyHtml: emptyHtml,
    errorBanner: errorBanner,
    loadInto: loadInto,
    confirmAction: confirmAction,
    toast: toast,
    err: err,
    modal: modal,
    confirmDialog: confirmDialog,
    inputDialog: inputDialog,
    formModal: formModal,
    personPicker: personPicker,
    dropdownMenu: dropdownMenu,
    floatingLayer: floatingLayer,
    menuList: menuList,
    closeOpenMenu: closeOpenMenu,
    closeAllModals: closeAllModals,
    segWire: segWire,
    segSet: segSet,
    eachOk: eachOk,
    download: download,
    pref: pref,
    idOf: idOf,
    isImage: isImage,
    isMarkdown: isMarkdown,
    canInlineMime: canInlineMime,
    isEmbedImage: isEmbedImage,
    crumbs: crumbs,
    crumbsWire: crumbsWire,
    walkTree: walkTree,
    sortHead: sortHead,
    sortHeadSet: sortHeadSet,
    tree: tree,
    onMediaChange: onMediaChange,
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
    projectCountMeta: projectCountMeta,
  };
})();
