// views/projects.js — 项目首页(#/):项目卡片网格 + 新建项目
window.Views = window.Views || {};
(function () {
  'use strict';

  window.Views.projects = async function (container) {
    container.innerHTML =
      UI.pageHead({
        title: '项目',
        sub: '我参与的全部项目',
        actions: UI.btn({ id: 'btn-new-proj', label: '新建项目', icon: 'add-line', kind: 'filled' }),
      }) +
      '<div class="card-grid" id="proj-grid">' + UI.loadingRow() + '</div>';

    const grid = container.querySelector('#proj-grid');

    async function load() {
      try {
        const list = await api('/api/projects');
        if (!(list || []).length) {
          grid.innerHTML = '';
          grid.appendChild(UI.emptyState({
            icon: 'folder-open-line',
            title: '暂无项目',
            desc: '创建第一个项目,开始团队协作',
            action: { label: '新建项目', onClick: openCreate },
          }));
          return;
        }
        grid.innerHTML = list.map((p) => UI.cardLink({
          href: '#/p/' + UI.esc(p.id),
          name: p.name,
          badges: p.isPersonal ? UI.badge({ text: '个人' }) : '',
          desc: p.description || (p.isPersonal ? '我的私有文档与文件' : '暂无描述'),
          meta: p.isPersonal
            ? [{ text: (p.docCount != null ? p.docCount : '-') + ' 文档' }]
            : [
                // 图标独占一个 span(与原文一致:图标与计数之间保留 meta 行的 8px 间距)
                { icon: 'team-line', text: '' },
                { text: (p.memberCount != null ? p.memberCount : '-') + ' 成员' },
                { text: '·' },
                { text: (p.docCount != null ? p.docCount : '-') + ' 文档' },
              ],
          metaHtml: p.myRole ? UI.badge({ text: UI.roleLabel(p.myRole), kind: 'primary' }) : '',
        })).join('');
      } catch (e) {
        grid.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message });
      }
    }

    function openCreate() {
      UI.formModal({
        title: '新建项目',
        okText: '创建',
        fields: [
          { name: 'name', label: '项目名', required: true, maxlength: 100, placeholder: '例如:产品设计' },
          { name: 'description', label: '描述', type: 'textarea', rows: 2, placeholder: '这个项目是做什么的?' },
        ],
        submit: async (v, { close }) => {
          const p = await api('/api/projects', { method: 'POST', body: { name: v.name.trim(), description: v.description } });
          close(true);
          UI.toast('项目已创建', 'success');
          if (p && p.id && App.expandProject) App.expandProject(p.id); // 落地页子项立即可见
          App.refreshSidebar();
          if (p && p.id) location.hash = '#/p/' + p.id;
          else load();
        },
      });
    }

    container.querySelector('#btn-new-proj').onclick = openCreate;
    await load();
  };
})();
