// views/search.js — 搜索结果页(#/search?q=)
// 类型筛选 chip(全部/文档/文件);结果卡片;snippet 为纯文本,转义后包 <mark> 高亮
window.Views = window.Views || {};
(function () {
  'use strict';

  // snippet 是后端截取的纯文本;先转义再包 <mark>,不做任何 HTML 注入
  function highlight(text, q) {
    const t = String(text == null ? '' : text);
    const needle = (q || '').trim();
    if (!needle) return UI.esc(t);
    const lower = t.toLowerCase();
    const n = needle.toLowerCase();
    let out = '', i = 0, idx;
    while ((idx = lower.indexOf(n, i)) !== -1) {
      out += UI.esc(t.slice(i, idx)) + '<mark>' + UI.esc(t.slice(idx, idx + n.length)) + '</mark>';
      i = idx + n.length;
    }
    return out + UI.esc(t.slice(i));
  }

  const TYPES = [
    { id: 'all', label: '全部' },
    { id: 'docs', label: '文档' },
    { id: 'files', label: '文件' },
  ];

  window.Views.search = async function (container, { query }) {
    const q = (query.get('q') || '').trim();
    let type = 'all';

    container.innerHTML =
      '<div class="view-narrow">' +
      '<div class="page-head">' +
      '<div><h1 class="page-title">搜索</h1>' +
      (q ? '<div class="page-sub">「' + UI.esc(q) + '」的搜索结果</div>' : '') +
      '</div></div>' +
      '<div class="chip-row" id="search-chips"></div>' +
      '<div id="search-result"></div>' +
      '</div>';

    const box = container.querySelector('#search-result');
    const chipsEl = container.querySelector('#search-chips');

    if (!q) {
      box.innerHTML = '';
      box.appendChild(UI.emptyState({
        icon: 'ri-search-line',
        title: '输入关键词开始搜索',
        desc: '使用侧栏搜索框,或按 Ctrl+K 快速聚焦',
      }));
    }

    function renderChips() {
      chipsEl.innerHTML = TYPES.map((t) =>
        '<button class="chip' + (t.id === type ? ' selected' : '') + '" type="button" data-type="' + t.id + '">' +
        (t.id === type ? UI.icon('check-line') : '') + UI.esc(t.label) + '</button>'
      ).join('');
    }
    renderChips();

    chipsEl.addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip || chip.dataset.type === type) return;
      type = chip.dataset.type;
      renderChips();
      if (q) load();
    });

    async function load() {
      box.innerHTML = UI.loadingRow('搜索中…');
      try {
        const data = await api('/api/search?q=' + encodeURIComponent(q) + '&type=' + encodeURIComponent(type));
        const docs = data.docs || [];
        const files = data.files || [];
        if (!docs.length && !files.length) {
          box.innerHTML = '';
          box.appendChild(UI.emptyState({
            icon: 'ri-file-search-line',
            title: '未找到相关结果',
            desc: '换个关键词试试',
          }));
          return;
        }
        let html = '';
        if (docs.length) {
          html += '<div class="section-title">' + UI.icon('file-text-line') + ' 文档(' + docs.length + ')</div>' +
            '<div class="result-list">' + docs.map((d) =>
              '<a class="result-item" href="#/p/' + UI.esc(d.projectId) + '/docs/' + UI.esc(d.id) + '">' +
              '<i class="row-icon fi-word ri-file-text-line"></i>' +
              '<div style="min-width:0">' +
              '<div class="result-title">' + highlight(d.title, q) + '</div>' +
              (d.snippet ? '<div class="result-snippet">' + highlight(d.snippet, q) + '</div>' : '') +
              '</div></a>'
            ).join('') + '</div>';
        }
        if (files.length) {
          html += '<div class="section-title">' + UI.icon('folder-line') + ' 文件(' + files.length + ')</div>' +
            '<div class="result-list">' + files.map((f) =>
              '<a class="result-item" href="/api/files/' + UI.esc(f.id) + '/download" download>' +
              '<i class="row-icon fi-default ri-file-line"></i>' +
              '<div style="min-width:0">' +
              '<div class="result-title">' + highlight(f.name, q) + '</div>' +
              '<div class="result-tag">项目文件 · 点击下载</div>' +
              '</div></a>'
            ).join('') + '</div>';
        }
        box.innerHTML = html;
      } catch (e) {
        box.innerHTML = '<div class="text-danger">' + UI.esc(e.message) + '</div>';
      }
    }

    if (q) await load();
  };
})();
