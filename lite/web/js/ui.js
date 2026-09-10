// ui.js — 自研组件库与通用工具(全部视图复用,禁止重复造轮子)
// 组件:toast / modal / confirmDialog / inputDialog / dropdownMenu / avatar / emptyState /
//      spinner / icon;工具:esc / debounce / fmtSize / fmtDate / copyText / roleRank / roleLabel
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

  /** Remix Icon:<i class="ri-xxx"></i> */
  function icon(name, cls) {
    return '<i class="ri-' + esc(name) + (cls ? ' ' + esc(cls) : '') + '"></i>';
  }

  /** 文件大小人性化 */
  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  /** 服务端时间戳为 UTC naive:无时区后缀时按 UTC 解析再转本地 */
  function fmtDate(s) {
    if (!s) return '-';
    var str = String(s);
    var hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(str);
    var d = new Date(hasTz ? str : str.replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return str;
    return d.toLocaleString('zh-CN', { hour12: false });
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
   * @param {{name:string, seed?:string, size?:number, color?:string, editing?:boolean, title?:string}} opts
   */
  function avatar(opts) {
    opts = opts || {};
    var size = opts.size || 32;
    var color = opts.color || avatarColor(opts.seed || opts.name || '?');
    return '<span class="avatar' + (opts.editing ? ' editing' : '') + '"' +
      ' style="width:' + size + 'px;height:' + size + 'px;font-size:' + Math.round(size * 0.42) +
      'px;background:' + esc(color) + '" title="' + esc(opts.title || opts.name || '') + '">' +
      esc(String(opts.name || '?').slice(0, 1)) + '</span>';
  }

  /** 加载圈 HTML */
  function spinner(size) {
    size = size || 22;
    return '<span class="spinner" style="width:' + size + 'px;height:' + size + 'px"></span>';
  }

  /** 居中加载行 HTML */
  function loadingRow(text) {
    return '<div class="loading-row">' + spinner() + '<span>' + esc(text || '加载中…') + '</span></div>';
  }

  /**
   * 空状态,返回 DOM 元素。action: {label, onClick} 可选。
   * @param {{icon:string, title:string, desc?:string, action?:{label:string, onClick:Function}}} opts
   */
  function emptyState(opts) {
    var el = document.createElement('div');
    el.className = 'empty-state';
    el.innerHTML =
      '<div class="es-icon">' + icon(opts.icon || 'ri-inbox-line') + '</div>' +
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
      (opts.label ? '<div class="field" style="margin-bottom:0"><label>' + esc(opts.label) + '</label>' : '<div>') +
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
      if (f.type === 'checkbox') {
        return '<label class="check-row"><input type="checkbox" id="' + id + '"' + fid +
          (f.value ? ' checked' : '') + '>' + esc(f.checkbox || f.label || '') + '</label>';
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

  /* ---------- 下拉菜单 ---------- */
  var openMenuCleanup = null;
  function closeOpenMenu() {
    if (openMenuCleanup) { openMenuCleanup(); openMenuCleanup = null; }
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
    fmtSize: fmtSize,
    fmtDate: fmtDate,
    copyText: copyText,
    roleRank: roleRank,
    roleLabel: roleLabel,
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
    dropdownMenu: dropdownMenu,
    closeOpenMenu: closeOpenMenu,
  };
})();
