// views/discover.js — 发现页(#/discover):公开项目广场 + 最近动态
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
        sub: '团队里公开的项目与最近动态',
      }) +
      '<div id="disc-body">' + UI.loadingRow() + '</div>';

    const body = container.querySelector('#disc-body');

    async function load() {
      let projects = [];
      try {
        projects = await api('/api/discover/projects') || [];
      } catch (e) {
        body.innerHTML = UI.errorBanner(e);
        return;
      }

      let html = '';

      // 第一段:公开项目
      html += UI.sectionTitle({ title: '公开项目(' + projects.length + ')', icon: 'apps-2-line' });
      if (!projects.length) {
        html += UI.banner({
          kind: 'info', icon: 'information-line',
          text: '还没有公开的项目。项目管理员可以在「项目设置」里把项目公开到广场。',
        });
      } else {
        html += '<div class="card-grid">' + projects.map((p) =>
          UI.cardLink({
            href: '#/p/' + UI.esc(p.id),
            name: p.name,
            badges: UI.badge({ text: '公开', kind: 'primary' }) +
              (p.isMember ? ' ' + UI.badge({ text: '已加入', kind: 'success' }) : ''),
            desc: p.description || '暂无描述',
            meta: [
              { icon: 'team-line', text: '' },
              { text: (p.memberCount != null ? p.memberCount : '-') + ' 成员' },
              { text: '·' },
              { text: (p.docCount != null ? p.docCount : '-') + ' 文档' },
            ],
            metaHtml: p.lastUpdatedAt
              ? UI.badge({ text: '更新于 ' + UI.fmtDateShort(p.lastUpdatedAt), kind: 'primary' })
              : (p.myRole ? UI.badge({ text: UI.roleLabel(p.myRole), kind: 'primary' }) : ''),
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
