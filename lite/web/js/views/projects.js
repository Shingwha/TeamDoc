// views/projects.js — 项目首页(#/):项目卡片网格 + 新建项目
window.Views = window.Views || {};
(function () {
  'use strict';

  window.Views.projects = async function (container) {
    container.innerHTML =
      '<div class="page-head">' +
      '<div><h1 class="page-title">项目</h1><div class="page-sub">我参与的全部项目</div></div>' +
      '<button class="btn btn-filled" id="btn-new-proj" type="button"><i class="ri-add-line"></i>新建项目</button>' +
      '</div>' +
      '<div class="proj-grid" id="proj-grid">' + UI.loadingRow() + '</div>';

    const grid = container.querySelector('#proj-grid');

    async function load() {
      try {
        const list = await api('/api/projects');
        if (!(list || []).length) {
          grid.innerHTML = '';
          grid.appendChild(UI.emptyState({
            icon: 'ri-folder-open-line',
            title: '暂无项目',
            desc: '创建第一个项目,开始团队协作',
            action: { label: '新建项目', onClick: openCreate },
          }));
          return;
        }
        grid.innerHTML = list.map((p) =>
          '<a class="proj-card" href="#/p/' + UI.esc(p.id) + '">' +
          '<div class="proj-name-row">' +
          '<span class="proj-name">' + UI.esc(p.name) + '</span>' +
          (p.isPersonal ? '<span class="badge">个人</span>' : '') +
          '</div>' +
          '<div class="proj-desc">' + UI.esc(p.description || (p.isPersonal ? '我的私有文档与文件' : '暂无描述')) + '</div>' +
          '<div class="proj-meta">' +
          (p.isPersonal
            ? '<span>' + (p.docCount != null ? p.docCount : '-') + ' 文档</span>'
            : '<span>' + UI.icon('team-line') + '</span>' +
              '<span>' + (p.memberCount != null ? p.memberCount : '-') + ' 成员</span>' +
              '<span>·</span><span>' + (p.docCount != null ? p.docCount : '-') + ' 文档</span>') +
          (p.myRole ? '<span class="badge primary">' + UI.esc(UI.roleLabel(p.myRole)) + '</span>' : '') +
          '</div></a>'
        ).join('');
      } catch (e) {
        grid.innerHTML = '<div class="text-danger">' + UI.esc(e.message) + '</div>';
      }
    }

    function openCreate() {
      UI.modal({
        title: '新建项目',
        body:
          '<div class="field"><label>项目名 *</label>' +
          '<input class="input" id="np-name" maxlength="100" placeholder="例如:产品设计"></div>' +
          '<div class="field" style="margin-bottom:0"><label>描述</label>' +
          '<textarea class="textarea" id="np-desc" rows="2" placeholder="这个项目是做什么的?"></textarea></div>',
        actions: [
          { label: '取消', kind: 'text', value: null },
          {
            label: '创建', kind: 'filled',
            handler: async ({ close, body, btn }) => {
              const name = body.querySelector('#np-name').value.trim();
              if (!name) { UI.toast('请填写项目名', 'warning'); return; }
              btn.disabled = true;
              try {
                const p = await api('/api/projects', {
                  method: 'POST',
                  body: { name, description: body.querySelector('#np-desc').value },
                });
                close(true);
                UI.toast('项目已创建', 'success');
                if (p && p.id && App.expandProject) App.expandProject(p.id); // 落地页子项立即可见
                App.refreshSidebar();
                if (p && p.id) location.hash = '#/p/' + p.id;
                else load();
              } catch (e) {
                btn.disabled = false;
                UI.err(e);
              }
            },
          },
        ],
      });
    }

    container.querySelector('#btn-new-proj').onclick = openCreate;
    await load();
  };
})();
