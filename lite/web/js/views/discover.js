// views/discover.js — 发现页(#/discover):公开项目广场 + 最近动态
//   广场只发现**项目**(个人空间永不出现);"团队"这个概念在本系统里就等于项目。
//   30 人的团队项目不多,静态目录浏览起来比直接问同事还慢 —— 所以两段都按"最近
//   有人在动"排序,并把最近动态放在一起:打开就能看到同事在干什么,不用去逛。
window.Views = window.Views || {};
(function () {
  'use strict';

  window.Views.discover = async function (container) {
    container.innerHTML =
      '<div class="view-narrow">' +
      UI.pageHead({
        title: '发现',
        sub: '团队里公开的项目与最近动态',
      }) +
      '<div id="disc-body">' + UI.loadingRow() + '</div>' +
      '</div>';

    const body = container.querySelector('#disc-body');

    async function load() {
      let projects = [], recent = { files: [], docs: [] };
      try {
        projects = await api('/api/discover/projects') || [];
      } catch (e) {
        body.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message });
        return;
      }
      // 最近动态是辅助信息:拉失败不该让整个页面空白
      try { recent = await api('/api/recent?limit=12'); } catch (e) { /* 忽略 */ }

      const files = recent.files || [];
      const docs = recent.docs || [];
      let html = '';

      // 第一段:公开项目
      html += '<div class="section-title">' + UI.icon('apps-2-line') +
        ' 公开项目(' + projects.length + ')</div>';
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
              ? UI.badge({ text: '更新于 ' + UI.fmtDate(p.lastUpdatedAt).slice(5, 16), kind: 'primary' })
              : (p.myRole ? UI.badge({ text: UI.roleLabel(p.myRole), kind: 'primary' }) : ''),
          })
        ).join('') + '</div>';
      }

      // 第二段:最近动态(跨项目,零新表)
      if (docs.length || files.length) {
        html += '<div class="section-title">' + UI.icon('time-line') + ' 最近动态</div>';
        if (docs.length) {
          html += docs.slice(0, 6).map((d) =>
            UI.listRow({
              raised: true, hoverable: true, tag: 'a',
              href: '#/p/' + UI.esc(d.projectId) + '/docs/' + UI.esc(d.id),
              icon: 'file-text-line', iconCls: 'fi-doc',
              title: UI.esc(d.title),
              sub: UI.esc(d.projectName || '') + ' 的文档 · ' + UI.esc(UI.fmtDate(d.updatedAt)),
            })
          ).join('');
        }
        if (files.length) {
          html += files.slice(0, 6).map((f) => {
            const fi = UI.fileIcon(f.mime);
            return UI.listRow({
              raised: true, hoverable: true, tag: 'a',
              href: '#/p/' + UI.esc(f.projectId) + '/files' +
                (f.folderId ? '?folder=' + UI.esc(f.folderId) : ''),
              icon: fi.icon, iconCls: fi.cls,
              title: UI.esc(f.name),
              sub: UI.esc(f.projectName || '') + ' 的文件 · ' + UI.esc(UI.fmtSize(f.size)) +
                ' · ' + UI.esc(UI.fmtDate(f.createdAt)),
            });
          }).join('');
        }
      }

      body.innerHTML = html;
    }

    await load();
  };
})();
