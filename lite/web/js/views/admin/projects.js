// views/admin/projects.js — 管理后台·项目区(全站协作项目总览 / 模块直达 / 删除)
// 只列协作项目:个人空间对管理员保密,占用统计也不含它们。
// 管理动作不在这里重做 —— 每行给五个模块的直达按钮(文档/云空间/成员/回收站/设置,
// 与侧栏 PROJECT_NAV 同一套 tab),点进去就是项目内页面;全局管理员对任何协作
// 项目的有效角色至少是 ADMIN(auth.project_role 的 max 语义),页面上的管理能力
// 不随"是否加入"变化 —— 后台就是管理员的管理驾驶舱。这里只负责"发现 + 直达 + 删除"。
window.AdminSections = window.AdminSections || {};
(function () {
  'use strict';

  // 五个模块直达按钮:tab 键、图标、文案与 app.js 的 PROJECT_NAV 逐项对齐 ——
  // 同一功能在项目里和后台里必须是同一个图标(回收站曾是这里的特例,画成 history-line,
  // 结果同一个"回收站"在两处长得不一样)。与同一行末尾「删除项目」同形是有意为之:
  // 那颗粒是 danger 红且在最右,靠颜色区分,不靠换图标。
  const TABS = [
    { tab: 'docs', icon: 'file-text-line', title: '文档' },
    { tab: 'files', icon: 'folder-line', title: '云空间' },
    { tab: 'members', icon: 'team-line', title: '成员' },
    { tab: 'trash', icon: 'delete-bin-line', title: '回收站' },
    { tab: 'settings', icon: 'settings-4-line', title: '设置' },
  ];

  // 列宽:项目(弹性) / 所有者(弹性) / 成员 / 文档 / 最近活跃 / 占用 / 操作(6 颗:五模块直达 + 删除)。
  // 数字与日期列固定宽,文案列给弹性 —— 与用户表同一套列宽思路
  const PROJ_TPL = 'minmax(0, 1.4fr) minmax(0, 1fr) 56px 56px minmax(0, 0.9fr) 80px var(--col-acts)';

  window.AdminSections.projects = function (ctx) {
    const el = ctx.el;

    function load() {
      // fetcher 里先落共享状态(空列表也要覆盖旧值,不能让事件处理器读到上一次的数据)
      return UI.loadInto(el, () => api(Endpoints.adminProjects()).then((list) => {
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
              { tpl: PROJ_TPL, cls: 'acts-6' }
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
                    title: UI.esc(p.name),
                    sub: p.description ? UI.esc(p.description) : '',
                    badges: (p.isPersonal ? UI.badge({ text: '个人空间' }) : '') +
                      (p.isPublic ? UI.badge({ text: '公开', kind: 'primary' }) : ''),
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
                  TABS.map((t) =>
                    UI.iconBtn({ icon: t.icon, title: t.title, cls: 'p-tab', attrs: 'data-ptab="' + t.tab + '"' })
                  ).join('') +
                  UI.iconBtn({ icon: 'delete-bin-line', title: '删除项目', danger: true, cls: 'p-delete' }),
              });
            }).join('');
        },
      });
    }

    el.addEventListener('click', async (e) => {
      const prow = e.target.closest('.data-table-row[data-pid]');
      if (!prow) return;
      const p = (ctx.state.projects || []).find((x) => x.id === prow.dataset.pid);
      if (!p) return;
      const tabBtn = e.target.closest('.p-tab');
      if (tabBtn) {
        location.hash = App.route.project(p.id, tabBtn.dataset.ptab);
      } else if (e.target.closest('.p-delete')) {
        await UI.confirmAction(
          '删除「' + p.name + '」将同时删除其全部文档、文件与成员关系,且不可恢复。确定删除?',
          { okText: '删除', okMsg: '项目已删除' },
          async () => {
            await api(Endpoints.project(p.id), { method: 'DELETE' });
            // 占用总览同步回落
            await ctx.refresh('projects', 'storage');
          });
      }
    });

    return { key: 'projects', load };
  };
})();
