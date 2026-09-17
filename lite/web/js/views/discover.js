// views/discover.js — 发现页(#/discover):公开项目广场 + 最近动态
//   公开项目是"可发现 + 可自助加入":加入前读不到任何内容,所以这里的卡片只给
//   项目本身(描述/计数),点一下的下一步是**加入**(ProjectsAPI.joinPrompt)。
//   广场只发现**项目**(个人空间永不出现);"团队"这个概念在本系统里就等于项目。
//   30 人的团队项目不多,静态目录浏览起来比直接问同事还慢 —— 所以两段都按"最近
//   有人在动"排序,并把最近动态放在一起:打开就能看到同事在干什么,不用去逛。
window.Views = window.Views || {};
(function () {
  'use strict';

  window.Views.discover = async function (container) {
    // 卡片栅格需要横向铺开,用默认页宽(项目/发现/后台/云空间同档)
    container.innerHTML =
      UI.pageHead({
        title: '发现',
        sub: '可加入的公开项目,与你参与项目的最近动态',
      }) +
      '<div id="disc-body">' + UI.loadingRow() + '</div>';

    const body = container.querySelector('#disc-body');
    // 卡片的点击语义:已加入的直接进,未加入的先问一句要不要加入 —— 判据只有
    // isMember(与卡片「已加入/可加入」徽标同源),对所有人一致:管理员的管辖身份
    // 不是成员身份,从广场参与一个项目同样要先加入。委托只挂一次:load() 会反复
    // 重绘 body 的内容
    let byId = new Map();
    body.addEventListener('click', (e) => {
      const a = e.target.closest('a.card-link');
      if (!a) return;
      const p = byId.get(UI.idOf(a.dataset.pid));
      if (!p || p.isMember) return;   // 已加入:照常进入
      e.preventDefault();
      ProjectsAPI.joinPrompt(p.id, p.name, p.joinRole);
    });

    async function load() {
      let projects = [];
      try {
        projects = await api('/api/discover/projects') || [];
      } catch (e) {
        body.innerHTML = UI.errorBanner(e);
        return;
      }
      byId = new Map(projects.map((p) => [p.id, p]));

      let html = '';

      // 第一段:公开项目(未加入的点了会问"要不要加入")
      html += UI.sectionTitle({ title: '公开项目(' + projects.length + ')', icon: 'apps-2-line' });
      if (!projects.length) {
        html += UI.banner({
          kind: 'info', icon: 'information-line',
          text: '还没有公开的项目。项目管理员可以在「项目设置」里把项目公开到广场,同事就能发现并自助加入。',
        });
      } else {
        html += '<div class="card-grid">' + projects.map((p) =>
          UI.cardLink({
            href: App.route.project(p.id),
            attrs: 'data-pid="' + UI.esc(p.id) + '"',
            name: p.name,
            badges: p.isMember
              ? UI.badge({ text: '已加入', kind: 'success' })
              : UI.badge({ text: '可加入', kind: 'primary' }),
            desc: p.description || '暂无描述',
            meta: UI.projectCountMeta(p),
            metaHtml: p.lastUpdatedAt
              ? UI.badge({ text: '更新于 ' + UI.fmtDateShort(p.lastUpdatedAt), kind: 'primary' })
              : (p.isMember && p.myRole ? UI.badge({ text: UI.roleLabel(p.myRole), kind: 'primary' }) : ''),
          })
        ).join('') + '</div>';
      }

      // 第二段:最近动态(仅我参与的项目,个人项目计入;/api/recent 已按成员过滤)。
      // 取数与行构造走 Recent(与搜索页空态共用);分组与空态文案是本页的呈现。
      html += UI.sectionTitle({ title: '最近动态', icon: 'time-line' });
      let docs = [], files = [];
      // 最近动态是辅助信息:拉失败不该让整个页面空白
      try { ({ docs, files } = await Recent.fetch(12)); } catch (e) { /* 忽略 */ }
      if (docs.length || files.length) {
        html += docs.slice(0, 6).map((d) => Recent.docRow(d)).join('') +
          files.slice(0, 6).map((f) => Recent.fileRow(f)).join('');
      } else {
        // 未参加任何项目时动态必然为空,给个指引而不是整段消失
        html += UI.banner({
          kind: 'info', icon: 'information-line',
          text: '加入项目后,这里会展示你参与项目的最新动态。',
        });
      }

      body.innerHTML = html;
    }

    await load();
  };
})();
