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

  /** 搜索页作用域(#/search?q=)。键里带 query —— 换关键词就是换了一个区域 */
  window.Views.searchLevel = function (r) {
    return {
      key: 'search' + (r.qs ? '?' + r.qs : ''),
      render(ctx) {
        const q = (r.query.get('q') || '').trim();
        let type = 'all';

        const frag = Scope.html(
          UI.pageHead({
            title: '搜索',
            sub: q ? '「' + UI.esc(q) + '」的搜索结果' : '',
          }) +
          '<div class="chip-row" id="search-chips"></div>' +
          '<div id="search-result">' + UI.skeleton('rows', 5) + '</div>');

        const box = frag.querySelector('#search-result');
        const chipsEl = frag.querySelector('#search-chips');

        /** 无关键词时展示"最近上传 / 最近更新"。
         *  放这里而不是新增导航项:搜索框是"找东西"的默认入口,而空关键词时的空态
         *  原本只是一句无用的提示 —— 换成跨项目的最近动态正好补齐"我昨天传的东西
         *  在哪"这个最常见的找文件场景(用户往往不记得它在哪个项目)。 */
        async function renderRecent() {
          let docs, files;
          try {
            ({ docs, files } = await Recent.fetch(12));
          } catch (e) {
            box.innerHTML = UI.errorBanner(e);
            return;
          }
          if (!docs.length && !files.length) {
            box.innerHTML = UI.emptyHtml({
              icon: 'search-line',
              title: '输入关键词开始搜索',
              desc: '使用侧栏搜索框,或按 Ctrl+K 快速聚焦',
            });
            return;
          }
          let html = '';
          if (docs.length) {
            html += UI.sectionTitle({ title: '最近更新的文档', icon: 'time-line' }) +
              '<div class="result-list">' + docs.map((d) =>
                Recent.docRow(d, UI.esc(d.projectName || '') + ' · ' + UI.esc(UI.fmtDate(d.updatedAt)))
              ).join('') + '</div>';
          }
          if (files.length) {
            html += UI.sectionTitle({ title: '最近上传的文件', icon: 'time-line' }) +
              '<div class="result-list">' + files.map((f) =>
                Recent.fileRow(f, UI.esc(f.projectName || '') + ' · ' + UI.esc(UI.fmtSize(f.size)) +
                  ' · 点击进入所在目录')
              ).join('') + '</div>';
          }
          box.innerHTML = html;
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
          box.innerHTML = UI.skeleton('rows', 5);
          try {
            const data = await api(Endpoints.search({ q: q, type: type }));
            const docs = data.docs || [];
            const files = data.files || [];
            if (!docs.length && !files.length) {
              box.innerHTML = UI.emptyHtml({
                icon: 'file-search-line',
                title: '未找到相关结果',
                desc: '换个关键词试试',
              });
              return;
            }
            let html = '';
            if (docs.length) {
              html += UI.sectionTitle({ title: '文档(' + docs.length + ')', icon: 'file-text-line' }) +
                '<div class="result-list">' + docs.map((d) =>
                  UI.listRow({
                    raised: true, hoverable: true, tag: 'a',
                    href: App.route.project(d.projectId, 'docs', d.id),
                    icon: 'file-text-line', iconCls: 'fi-doc',
                    title: highlight(d.title, q),
                    sub: d.snippet ? highlight(d.snippet, q) : '',
                    subClamp: true,
                  })
                ).join('') + '</div>';
            }
            if (files.length) {
              html += UI.sectionTitle({ title: '文件(' + files.length + ')', icon: 'folder-line' }) +
                '<div class="result-list">' + files.map((f) => {
                  const fi = UI.fileIcon(f.mime);
                  // 点击进入文件所在目录(而非直接下载):直接下载不告诉用户在哪个项目、
                  // 也跳不到上下文,而"这文件在哪"通常和"我要它"一样重要
                  return UI.listRow({
                    raised: true, hoverable: true, tag: 'a',
                    href: App.route.project(f.projectId, 'files', null,
                      f.folderId ? { folder: f.folderId } : null),
                    icon: fi.icon, iconCls: fi.cls,
                    title: highlight(f.name, q),
                    sub: UI.esc(f.projectName || '') + ' · ' + UI.esc(UI.fmtSize(f.size)) +
                      ' · 点击进入所在目录',
                  });
                }).join('') + '</div>';
            }
            box.innerHTML = html;
          } catch (e) {
            box.innerHTML = UI.errorBanner(e);
          }
        }

        if (q) load(); else renderRecent();
        return frag;
      },
    };
  };
})();
