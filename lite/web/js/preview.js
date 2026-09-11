// preview.js — 站内预览浮层(单一实现):云空间文件预览、文档里 @文档/@文件 引用点击共用。
//   交互约定(全站统一):点击引用/预览按钮 → 先出浮层就地给内容与上下文,跳转/下载是
//   浮层里的显式次要动作。之前"每个入口各自手拼一个浮层"的实现已全部收拢到这里。
//
//   Preview.open({ title, wide?, metaHtml?, actions?, preview, note? })
//     metaHtml: Preview.meta([...]) 生成的元数据行
//     actions:  [{ id?, label, icon?, kind?, onClick({close, body}) }](不自动关闭,由回调决定)
//     preview:  { kind:'image'|'pdf'|'markdown'|'text'|'none',
//                 url?(image/pdf 的内联地址), text?(Promise<string>,markdown/text 正文),
//                 note?(kind=none 时的说明文案) }
//     note:     预览区下方的补充说明(如"仅显示前 6000 字")
//   markdown 走 MdRender(与文档预览同栈);text 走等宽 pre + Prism 明文高亮(与云空间原行为一致)。
window.Preview = (function () {
  'use strict';

  /** 元数据行:items 为 {iconName?, iconCls?, text} 或纯字符串,以 · 相连 */
  function meta(items) {
    var html = (items || []).map(function (it) {
      if (typeof it === 'string') return '<span>' + UI.esc(it) + '</span>';
      var icon = it.iconName
        ? '<i class="row-icon ' + UI.esc(it.iconCls || '') + ' ri-' +
          String(it.iconName).replace(/^ri-/, '') + '"></i>'
        : '';
      return icon + '<span>' + UI.esc(it.text) + '</span>';
    }).join('<span>·</span>');
    return '<div class="pv-meta">' + html + '</div>';
  }

  function previewNode(spec) {
    if (spec.kind === 'image') return '<img class="pv-img" src="' + spec.url + '" alt="">';
    if (spec.kind === 'pdf') return '<iframe class="pv-frame" src="' + spec.url + '"></iframe>';
    if (spec.kind === 'markdown') return '<div class="pv-md markdown-body" id="pv-md">加载中…</div>';
    if (spec.kind === 'text') return '<pre class="pv-pre md-fallback" id="pv-pre">加载中…</pre>';
    return UI.banner({ kind: 'info', icon: 'information-line',
                       text: spec.note || '该类型不支持站内预览,可下载后查看。' });
  }

  function open(opts) {
    var actionsHtml = (opts.actions || []).map(function (a) {
      return UI.btn({ id: a.id, label: a.label, icon: a.icon, kind: a.kind || 'tonal', size: 'sm' });
    }).join('');
    var metaHtml = opts.metaHtml || opts.meta || ''; // 两种名字都收:meta 为常用简称
    var body =
      metaHtml +
      (actionsHtml ? '<div class="pv-actions">' + actionsHtml + '</div>' : '') +
      '<div class="pv-body" id="pv-body">' + previewNode(opts.preview) + '</div>' +
      (opts.note ? '<p class="pv-note">' + UI.esc(opts.note) + '</p>' : '');
    var m = UI.modal({ title: opts.title, wide: opts.wide !== false, body: body });

    (opts.actions || []).forEach(function (a) {
      if (!a.onClick) return;
      var el = m.body.querySelector('#' + a.id);
      if (el) el.onclick = function () { a.onClick({ close: m.close.bind(m), body: m.body }); };
    });

    var spec = opts.preview;
    if ((spec.kind === 'markdown' || spec.kind === 'text') && spec.text) {
      spec.text.then(function (t) {
        var box = m.body.querySelector('#pv-md') || m.body.querySelector('#pv-pre');
        if (!box) return;
        if (spec.kind === 'markdown' && window.MdRender) {
          window.MdRender.mount(box, t);
        } else {
          box.textContent = '';
          var code = document.createElement('code');
          code.className = 'language-plaintext';
          code.textContent = t; // textContent 赋值即转义,不解析 HTML
          box.appendChild(code);
          if (window.Prism && Prism.highlightElement) {
            try { Prism.highlightElement(code); } catch (e) { /* 高亮失败不影响内容 */ }
          }
        }
      }).catch(function (e) {
        var area = m.body.querySelector('#pv-body');
        if (area) area.innerHTML =
          UI.banner({ kind: 'danger', icon: 'error-warning-line', text: '无法预览:' + (e.message || '未知错误') });
      });
    }
    return m;
  }

  return { open: open, meta: meta };
})();
