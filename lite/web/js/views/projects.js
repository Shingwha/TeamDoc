// views/projects.js — 项目首页(#/):项目卡片网格 + 新建项目
window.Views = window.Views || {};
// 公开项目的加入入口**只有这一份**(发现页卡片询问与项目页的 403 错误态共用),
// 服务端对应也只有 POST /join 一条路。加一次项目要同时做三件事:落库、让侧栏
// 长出这个项目(模块导航由此而来)、进入它 —— 散在各调用方就必然漏第三步。
window.ProjectsAPI = (function () {
  'use strict';

  /** 执行加入;返回是否成功。落地模块交给路由决定(它会按 tab 记忆/默认 docs 重定向) */
  async function join(pid, name) {
    try {
      await api('/api/projects/' + encodeURIComponent(pid) + '/join', { method: 'POST' });
    } catch (e) {
      UI.err(e);
      return false;
    }
    UI.toast('已加入' + (name ? '「' + name + '」' : '项目'), 'success');
    App.refreshSidebar();
    location.hash = App.route.project(pid);
    return true;
  }

  /** 问一句要不要加入:joinRole 决定文案(EDITOR 加入即可改内容,VIEWER 只能看) */
  async function joinPrompt(pid, name, joinRole) {
    const ok = await UI.confirmDialog(
      '加入' + (name ? '「' + name + '」' : '这个项目') + '?加入后你可以查看本项目的文档与云空间,' +
      (joinRole === 'EDITOR' ? '并可编辑文档、上传文件' : '编辑权限需项目管理员另行授予') + '。',
      { title: '加入项目', okText: '加入', danger: false });
    if (ok) await join(pid, name);
  }

  return { join: join, joinPrompt: joinPrompt };
})();

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
          href: App.route.project(p.id),
          name: p.name,
          badges: p.isPersonal ? UI.badge({ text: '个人' }) : '',
          desc: p.description || (p.isPersonal ? '我的私有文档与文件' : '暂无描述'),
          meta: p.isPersonal
            ? [{ text: (p.docCount != null ? p.docCount : '-') + ' 文档' }]
            : UI.projectCountMeta(p),
          metaHtml: p.myRole ? UI.badge({ text: UI.roleLabel(p.myRole), kind: 'primary' }) : '',
        })).join('');
      } catch (e) {
        grid.innerHTML = UI.errorBanner(e);
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
          if (p && p.id) location.hash = App.route.project(p.id);
          else load();
        },
      });
    }

    container.querySelector('#btn-new-proj').onclick = openCreate;
    await load();
  };
})();
