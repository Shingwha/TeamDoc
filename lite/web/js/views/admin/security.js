// views/admin/security.js — 管理后台·登录动态区(全站最近登录事件,
// 含不存在账号的失败 = 撞库痕迹)。结果徽标/结果码映射复用 users 区暴露的共享件。
window.AdminSections = window.AdminSections || {};
(function () {
  'use strict';

  const SEC_TPL = 'minmax(0, 1fr) minmax(0, 1.2fr) 88px minmax(0, 1.4fr)';

  window.AdminSections.security = function (ctx) {
    const el = ctx.el;
    let secFilter = 'all';
    let secEvents = [];

    function secTable() {
      const RESULT_META = window.AdminSections.RESULT_META;
      const rows = secFilter === 'fail' ? secEvents.filter((e) => e.result !== 'ok') : secEvents;
      if (!rows.length) return UI.emptyHtml({ icon: 'shield-keyhole-line', title: '暂无登录记录' });
      return UI.tableHead(
        [{ html: '时间' }, { html: '账号' }, { html: '结果' }, { html: '来源' }],
        { tpl: SEC_TPL }
      ) + rows.map((e) => UI.tableRow([
        { html: UI.cellMeta(UI.esc(UI.fmtDate(e.createdAt))) },
        // 账号不存在时把邮箱显示在标题上并标注出来 —— 那正是"有人在拿不存在的邮箱扫"的信号
        { html: e.userName
            ? UI.cellId({ title: UI.esc(e.userName), sub: UI.esc(e.email) })
            : UI.cellId({ title: UI.esc(e.email),
                          sub: '<span class="muted">账号不存在</span>' }) },
        { html: window.AdminSections.resultBadge(e.result) },
        { html: UI.cellId({ title: UI.esc(e.ip || '未知来源'),
                            sub: UI.esc(e.device || '未知设备') }) },
      ])).join('');
    }

    function load() {
      return UI.loadInto(el, () => api('/api/admin/login-events?limit=100'), {
        render: (list) => {
          secEvents = list || [];
          return UI.sectionTitle({
            title: '登录动态', icon: 'shield-keyhole-line',
            actions: UI.seg({ id: 'sec-filter', auto: true, active: secFilter,
                              items: [{ key: 'all', label: '全部' }, { key: 'fail', label: '仅失败' }] }),
          }) + '<div id="sec-body">' + secTable() + '</div>';
        },
      }).then(() => {
        const seg = el.querySelector('#sec-filter');
        if (seg) UI.segWire(seg, (key) => {
          secFilter = key;
          el.querySelector('#sec-body').innerHTML = secTable();
        });
      });
    }

    return { key: 'security', load };
  };
})();
