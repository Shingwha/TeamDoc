// views/admin/projects.js — 管理后台·项目区(全站协作项目总览 / 跳转 / 删除)
// 只列协作项目:个人空间对管理员保密(HANDOFF §7.1),占用统计也不含它们。
// 成员管理不在这里重做一套 —— 成员页对全局管理员完全可用(含授予 OWNER),
// 管理后台只负责"发现 + 跳转"。
window.AdminSections = window.AdminSections || {};
(function () {
  'use strict';

  // 列宽:项目(弹性) / 所有者(弹性) / 成员 / 文档 / 最近活跃 / 占用 / 操作(2 颗)。
  // 数字与日期列固定宽,文案列给弹性 —— 与用户表同一套列宽思路
  const PROJ_TPL = 'minmax(0, 1.4fr) minmax(0, 1fr) 56px 56px minmax(0, 0.9fr) 80px var(--col-acts)';

  window.AdminSections.projects = function (ctx) {
    const el = ctx.el;

    function load() {
      // fetcher 里先落共享状态(空列表也要覆盖旧值,不能让事件处理器读到上一次的数据)
      return UI.loadInto(el, () => api('/api/admin/projects').then((list) => {
        ctx.state.projects = list || [];
        return ctx.state.projects;
      }), {
        empty: (list) => !list.length,
        emptyHtml: '',
        render: (list) => {
          return UI.sectionTitle({ title: '项目', icon: 'folder-3-line' }) +
            UI.tableHead(
              [{ html: '项目' }, { html: '所有者' }, { html: '成员' }, { html: '文档' },
               { html: '最近活跃' }, { html: '占用' }, { html: '' }],
              { tpl: PROJ_TPL, cls: 'acts-static acts-2' }
            ) + (list || []).map((p) => {
              // 所有者已禁用必须标出来:那是"唯一所有者失联 → 项目无人可接管"的信号,
              // 也是这个分区存在的理由(禁用后在这里把所有权交给接手人)
              const ownerHtml = p.owners.length
                ? p.owners.map((o) => UI.esc(o.name || o.email) +
                    (o.isDisabled ? ' ' + UI.badge({ text: '已禁用', kind: 'danger' }) : '')
                  ).join('、')
                : '<span class="muted">—</span>';
              return UI.tableRow([
                {
                  html: UI.cellId({
                    title: UI.esc(p.name) +
                      (p.isPersonal ? ' ' + UI.badge({ text: '个人空间' }) : '') +
                      (p.isPublic ? ' ' + UI.badge({ text: '公开', kind: 'primary' }) : ''),
                    sub: p.description ? UI.esc(p.description) : '',
                  }),
                },
                { html: ownerHtml },
                { html: UI.cellMeta(String(p.memberCount)) },
                { html: UI.cellMeta(String(p.docCount)) },
                { html: UI.cellMeta(p.lastUpdatedAt ? UI.fmtDate(p.lastUpdatedAt) : '—') },
                { html: UI.cellMeta(UI.fmtSize(p.storageBytes)) },
              ], {
                attrs: 'data-pid="' + UI.esc(p.id) + '"',
                acts: p.isPersonal ? '' :
                  UI.iconBtn({ icon: 'team-line', title: '管理成员', cls: 'p-members' }) +
                  UI.iconBtn({ icon: 'delete-bin-line', title: '删除项目', danger: true, cls: 'p-delete' }),
              });
            }).join('');
        },
      });
    }

    el.addEventListener('click', async (e) => {
      const prow = e.target.closest('.data-table-row[data-pid]');
      if (!prow) return;
      const p = (ctx.state.projects || []).find((x) => UI.sameId(x.id, prow.dataset.pid));
      if (!p) return;
      if (e.target.closest('.p-members')) {
        location.hash = App.route.project(p.id, 'members');
      } else if (e.target.closest('.p-delete')) {
        await UI.confirmAction(
          '删除「' + p.name + '」将同时删除其全部文档、文件与成员关系,且不可恢复。确定删除?',
          { okText: '删除', okMsg: '项目已删除' },
          async () => {
            await api('/api/projects/' + p.id, { method: 'DELETE' });
            // 占用总览同步回落
            await ctx.refresh('projects', 'storage');
          });
      }
    });

    return { key: 'projects', load };
  };
})();
