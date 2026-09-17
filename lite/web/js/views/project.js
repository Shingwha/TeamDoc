// views/project.js — 项目页各模块视图(模块导航 = 侧栏项目树的子项,主区无大页头)
//   Views.projectDocs     — 文档模块编排:文档树(渲染/交互在 doc-tree.js)+ 编辑器/历史版本
//                          (协同逻辑在 doc-editor.js),本系统最重页面(整页 flex 撑满主区)
//   Views.projectFiles    — 云空间模块:复用 views/drive.js 的 driveBody(项目上下文)
//   Views.projectMembers / projectTrash / projectSettings — 成员 / 回收站 / 项目设置
window.Views = window.Views || {};
(function () {
  'use strict';

  // ---------- 项目页公共壳:仅获取项目对象(权限判断)+ 提供 #proj-body 容器 ----------
  async function projectShell(container, projectId) {
    container.innerHTML = UI.loadingRow();
    let proj;
    try {
      proj = await api('/api/projects/' + encodeURIComponent(projectId));
    } catch (e) {
      // 失败时必须自己渲染错误态并终止:否则 loading 行会永远留在页面上,
      // 用户既看不到原因也没有返回入口(项目/成员/回收站/设置/云空间共用这条路径)。
      // 公开项目的 403(JOIN_REQUIRED)是唯一说得清出路的失败:直链打开、同事发来的
      // 链接都落在这里,给一个加入按钮,而不是让人对着"需要 VIEWER 及以上权限"发懵
      const joinable = e.code === 'JOIN_REQUIRED';
      container.innerHTML =
        UI.banner({ kind: 'danger', icon: 'error-warning-line',
                    text: joinable ? '你还不是这个项目的成员,加入后才能查看。'
                                   : '无法打开该项目:' + (e.message || '未知错误') }) +
        '<div class="form-inline mt-4">' +
        UI.btn({ id: 'proj-back', label: '返回项目列表', kind: 'tonal' }) +
        (joinable ? UI.btn({ id: 'proj-join', label: '加入项目', kind: 'filled' }) : '') +
        '</div>';
      const back = container.querySelector('#proj-back');
      if (back) back.onclick = () => { location.hash = '#/'; };
      const joinBtn = container.querySelector('#proj-join');
      if (joinBtn) joinBtn.onclick = () => ProjectsAPI.joinPrompt(projectId);
      throw e;  // 仍向上抛,让路由层记录(但不影响已渲染的错误态)
    }
    container.innerHTML = '<div id="proj-body"></div>';
    return proj;
  }

  // ==================== 成员视图(#/p/{id}/members;个人项目不可见,入口已被导航隐藏) ====================
  window.Views.projectMembers = async function (container, { projectId }) {
    const proj = await projectShell(container, projectId);
    if (proj.isPersonal) { location.replace(App.route.project(projectId, 'docs')); return; }
    container.classList.add('page-narrow'); // 成员列表是单列,用窄页
    // 管理入口一律走 UI.canAdmin(服务端 project_role 的镜像):达到 ADMIN 的只有
    // 真成员里的管理员与全局管理员 —— 非成员没有角色,不可能误看到管理入口
    const canAdmin = UI.canAdmin(proj);
    // OWNER 选项只对有 OWNER 级管辖权的人展示(UI.canOwn = 真所有者或全局管理员):
    // 项目 ADMIN 选了也会被服务端 403,与其让人点了再报错,不如不渲染
    const canOwn = UI.canOwn(proj);
    const ROLE_CHOICES = canOwn ? ['OWNER', 'ADMIN', 'EDITOR', 'VIEWER'] : ['ADMIN', 'EDITOR', 'VIEWER'];
    const body = container.querySelector('#proj-body');
    // 页头右上角放「添加成员」(与其他页面的头按钮同位);副标题省略 —— 项目名在侧栏,人数看列表
    body.innerHTML =
      UI.pageHead({
        title: '成员',
        actions: (canAdmin ? UI.btn({ id: 'mb-pick', label: '添加成员', icon: 'user-add-line', kind: 'filled', size: 'sm' }) : ''),
      }) +
      UI.card({
        body: '<div id="mb-list">' + UI.loadingRow() + '</div>',
      });

    const listEl = body.querySelector('#mb-list');
    function load() {
      return UI.loadInto(listEl, () => api('/api/projects/' + proj.id + '/members'), {
        empty: (members) => !(members || []).length,
        emptyNode: () => UI.emptyState({ icon: 'team-line', title: '暂无成员' }),
        render: (members) => members.map((mb) => {
          const u = mb.user || {};
          return UI.listRow({
            attrs: 'data-uid="' + UI.esc(mb.userId) + '"',
            avatar: UI.avatar({ name: u.name || u.email, seed: mb.userId, color: u.avatarColor }),
            title: UI.esc(u.name || '-'),
            badges: u.isDisabled ? UI.badge({ text: '已禁用', kind: 'danger' }) : '',
            sub: UI.esc(u.email || '-'),
            actions: canAdmin
              ? '<select class="select mb-role-sel">' +
                ROLE_CHOICES.map((r) =>
                  '<option value="' + r + '"' + (r === mb.role ? ' selected' : '') + '>' + UI.esc(UI.roleLabel(r)) + '</option>'
                ).join('') + '</select>' +
                UI.iconBtn({ icon: 'user-unfollow-line', title: '移除成员', danger: true, cls: 'mb-remove' })
              : UI.badge({ text: UI.roleLabel(mb.role) }),
          });
        }).join(''),
      });
    }
    await load();

    if (!canAdmin) return;

    /** 已在项目中的用户 id:选人浮层按 id 排除,免得选了才报 409 */
    function existingIds() {
      return [...listEl.querySelectorAll('.list-row')]
        .map((r) => r.dataset.uid).filter(Boolean);
    }
    async function addMember(userId, displayName, role) {
      try {
        await api('/api/projects/' + proj.id + '/members', { method: 'POST', body: { userId, role } });
        await load();
        return true;
      } catch (e) { UI.err(e); return false; }
    }

    // 添加成员:批量圈选(候选态,可反复勾选),在浮层里选好加入角色,确认后统一加入
    body.querySelector('#mb-pick').onclick = async () => {
      const picked = await UI.personPicker({
        title: '从同事目录选择',
        excludeIds: existingIds(),
        multi: true,
        roleSelect: {
          label: '加入角色',
          options: ROLE_CHOICES.map((r) => ({ value: r, label: UI.roleLabel(r) })),
          value: 'EDITOR',
        },
      });
      if (!picked || !picked.length) return;
      const r = await UI.eachOk(picked, (p) =>
        addMember(p.id, p.name || p.email, p.role).then((okFlag) => {
          if (!okFlag) throw new Error('add member failed'); // 失败计数靠抛错,名单从 failed 取
        }));
      if (r.failed.length) UI.toast('添加失败:' + r.failed.map((p) => p.name || p.email).join('、'), 'danger');
      else UI.toast(r.ok === 1 ? '已添加 ' + (picked[0].name || picked[0].email) : '已添加 ' + r.ok + ' 名成员', 'success');
    };
    listEl.addEventListener('change', async (e) => {
      const sel = e.target.closest('.mb-role-sel');
      if (!sel) return;
      const uid = sel.closest('.list-row').dataset.uid;
      try {
        await api('/api/projects/' + proj.id + '/members/' + uid, { method: 'PATCH', body: { role: sel.value } });
        UI.toast('已更新角色', 'success');
      } catch (err) { UI.err(err); await load(); }
    });
    listEl.addEventListener('click', async (e) => {
      const btn = e.target.closest('.mb-remove');
      if (!btn) return;
      const uid = btn.closest('.list-row').dataset.uid;
      if (!(await UI.confirmDialog('确定移除该成员?'))) return;
      try {
        await api('/api/projects/' + proj.id + '/members/' + uid, { method: 'DELETE' });
        UI.toast('已移除', 'success');
        await load();
      } catch (err) { UI.err(err); }
    });
  };

  // ==================== 回收站视图(#/p/{id}/trash) ====================
  // 三类回收项(文档 / 文件 / 文件夹)用 .seg 分段切换分类,避免长列表里翻找;
  // 三类数据一次取回后缓存在内存,切 tab 只重渲染、不重新请求
  window.Views.projectTrash = async function (container, { projectId }) {
    const proj = await projectShell(container, projectId);
    container.classList.add('page-narrow'); // 回收站是单列列表,用窄页
    const body = container.querySelector('#proj-body');
    // 副标题由 load() 填充(含项目名与各项计数),故先占位
    body.innerHTML =
      UI.pageHead({ title: '回收站', sub: '<span id="trash-sub"></span>' }) +
      UI.card({
        body: '<div id="trash-seg"></div>' +
          '<div id="trash-list">' + UI.loadingRow() + '</div>',
      });

    const listEl = body.querySelector('#trash-list');
    const segEl = body.querySelector('#trash-seg');
    const subEl = body.querySelector('#trash-sub');
    const canWrite = UI.canEdit(proj); // 成员且 EDITOR 及以上可恢复/彻底删除

    // 三类回收项:doc / file / folder;file/folder 的 URL 前缀统一从 FilesAPI 拼
    // (drive.js 的唯一映射,勿再手写 —— doc 不在其列,保留文档自己的前缀)
    const KIND_API = {
      doc: '/api/docs/',
      file: FilesAPI.url('file', ''),
      folder: FilesAPI.url('folder', ''),
    };
    const KIND_ICON = { doc: 'file-text-line', file: 'file-line', folder: 'folder-line' };
    const CATS = [
      { key: 'docs', kind: 'doc', label: '文档', icon: 'file-text-line' },
      { key: 'files', kind: 'file', label: '文件', icon: 'file-line' },
      { key: 'folders', kind: 'folder', label: '文件夹', icon: 'folder-line' },
    ];

    let data = { docs: [], files: [], folders: [] };
    let cat = 'docs';

    function items(key) { return data[key] || []; }

    /** 段标签渲染:非空分类才在标签上带数量,便于一眼看出哪类有待处理项 */
    function renderSeg() {
      segEl.innerHTML = UI.seg({
        auto: true, role: 'tablist', active: cat,
        items: CATS.map((c) => ({
          key: c.key, label: c.label, icon: c.icon,
          count: items(c.key).length, disabled: !items(c.key).length,
        })),
      });
    }

    function rowHtml(item, kind) {
      const isDoc = kind === 'doc';
      const label = isDoc ? item.title : item.name;
      // 分类已由上方 tab 表达,副标题不再重复"文档/文件"字样,只留元信息
      const meta = kind === 'file' && item.size != null ? UI.fmtSize(item.size) + ' · ' : '';
      return UI.listRow({
        attrs: 'data-kind="' + kind + '" data-id="' + UI.esc(item.id) + '"',
        icon: KIND_ICON[kind] || 'file-line',
        title: UI.esc(label),
        sub: meta + '删除于 ' + UI.esc(UI.fmtDate(item.deletedAt)),
        actions: canWrite
          ? UI.btn({ label: '恢复', kind: 'outline', size: 'sm', cls: 'tr-restore' }) +
            UI.iconBtn({ icon: 'delete-bin-line', title: '彻底删除', danger: true, size: 'sm', cls: 'tr-purge' })
          : '',
      });
    }

    function renderList() {
      const rows = items(cat);
      if (!rows.length) {
        listEl.innerHTML = '';
        // 全空 → 回收站整体为空;仅当前分类空 → 提示切到别的分类
        const total = items('docs').length + items('files').length + items('folders').length;
        listEl.appendChild(total
          ? UI.emptyState({ icon: 'inbox-line', title: '该分类下没有内容' })
          : UI.emptyState({ icon: 'delete-bin-line', title: '回收站为空' }));
        return;
      }
      const kind = CATS.find((c) => c.key === cat).kind;
      listEl.innerHTML = rows.map((it) => rowHtml(it, kind)).join('');
    }

    async function load() {
      try {
        data = await api('/api/projects/' + proj.id + '/trash') || {};
        const total = items('docs').length + items('files').length + items('folders').length;
        // 默认落在第一个非空分类:只删过文件时不会先看到空的"文档"页
        if (!items(cat).length) {
          const first = CATS.find((c) => items(c.key).length);
          if (first) cat = first.key;
        }
        subEl.textContent = proj.name + ' · 共 ' + total + ' 项' +
          (total ? ' · 已删除的项可在此恢复;彻底删除不可恢复' : '');
        renderSeg();
        renderList();
      } catch (e) {
        listEl.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message });
      }
    }
    await load();

    UI.segWire(segEl, (key) => {
      if (key === cat) return;
      cat = key;
      renderSeg();
      renderList();
    });

    listEl.addEventListener('click', async (e) => {
      const row = e.target.closest('.list-row');
      if (!row) return;
      const id = row.dataset.id, kind = row.dataset.kind;
      const base = KIND_API[kind] || '/api/files/';
      if (e.target.closest('.tr-restore')) {
        try {
          const r = await api(base + id + '/restore', { method: 'POST' });
          const n = (r && (r.restored || r.removedFolders)) || 1;
          UI.toast('已恢复 ' + n + ' 项', 'success');
          await load();
        } catch (err) { UI.err(err); }
        return;
      }
      if (e.target.closest('.tr-purge')) {
        const name = row.querySelector('.list-row-title').textContent;
        const tail = kind === 'doc' ? '将连同子文档与历史版本一起清除,'
          : kind === 'folder' ? '将连同其中的子文件夹与文件一起彻底清除(物理文件会被删除),'
            : '物理文件将被删除,';
        const ok = await UI.confirmDialog('彻底删除「' + name + '」?' + tail + '此操作不可恢复。');
        if (!ok) return;
        try {
          const r = await api(base + id + '/permanent', { method: 'DELETE' });
          const extra = r && r.removedFiles ? ',含 ' + r.removedFiles + ' 个文件' : '';
          UI.toast('已彻底删除' + extra, 'success');
          await load();
        } catch (err) { UI.err(err); }
      }
    });
  };

  // ==================== 设置视图(#/p/{id}/settings) ====================
  window.Views.projectSettings = async function (container, { projectId }) {
    const proj = await projectShell(container, projectId);
    container.classList.add('page-narrow'); // 设置是单列表单,用窄页
    const canEdit = UI.canAdmin(proj);    // 成员且 ADMIN 及以上可改
    const canDelete = UI.canOwn(proj); // OWNER 级管辖权;个人空间已在 canOwn 内排除
    const dis = canEdit ? '' : ' disabled';
    const body = container.querySelector('#proj-body');
    body.innerHTML =
      UI.pageHead({
        title: '设置',
        sub: '我的角色:' + UI.esc(UI.roleLabel(proj.myRole)) +
          ' · ' + (proj.memberCount != null ? proj.memberCount : '-') + ' 成员' +
          ' · ' + (proj.docCount != null ? proj.docCount : '-') + ' 文档' +
          (proj.isPersonal ? ' · 个人空间' : ''),
      }) +
      '<div class="stack">' +
      UI.card({
        body:
          '<div class="field"><label>项目名</label>' +
          '<input class="input" id="ps-name" maxlength="100" value="' + UI.esc(proj.name) + '"' + dis + '></div>' +
          '<div class="field"><label>描述</label>' +
          '<textarea class="textarea" id="ps-desc" rows="3"' + dis + '>' + UI.esc(proj.description || '') + '</textarea></div>' +
          (canEdit ? UI.btn({ id: 'ps-save', label: '保存', kind: 'filled' }) : ''),
      }) +
      (proj.isPersonal
        ? ''
        : UI.card({
          title: '可见性', icon: proj.isPublic ? 'global-line' : 'lock-line',
          note: proj.isPublic
            ? '公开:任何登录用户都能在「发现」里看到本项目并自助加入;加入前他们看不到项目里的任何内容,' +
              '加入后按下面的角色获得权限。'
            : '私有:只有项目成员能看到,也无法被自助加入。',
          body: canEdit
            ? '<label class="check-row"><input type="checkbox" id="ps-public"' +
              (proj.isPublic ? ' checked' : '') + '>公开到「发现」广场(任何同事都能发现并自助加入)</label>' +
              // 加入角色只在公开时有意义,跟着公开开关一起即时提交
              (proj.isPublic
                ? '<div class="field mt-3"><label>同事自助加入后的角色</label>' +
                  '<select class="select" id="ps-join-role">' +
                  [['VIEWER', '只读成员(能看,不能改)'], ['EDITOR', '编辑者(可直接改文档、传文件)']]
                    .map((o) => '<option value="' + o[0] + '"' +
                      (proj.joinRole === o[0] ? ' selected' : '') + '>' + UI.esc(o[1]) + '</option>')
                    .join('') + '</select></div>'
                : '')
            : (proj.isPublic ? UI.badge({ text: '公开', kind: 'primary' }) : UI.badge({ text: '私有' })),
        })) +
      (proj.isMember && !proj.isPersonal
        ? UI.card({
          danger: true,
          title: '退出项目', icon: 'logout-box-r-line',
          note: '退出后将无法再访问本项目的文档、云空间与回收站,最新动态也不再显示;重新加入需要项目管理员邀请。',
          body: UI.btn({ id: 'ps-leave', label: '退出项目', icon: 'logout-box-r-line', kind: 'danger-outline' }),
        })
        : '') +
      (canDelete
        ? UI.card({
          danger: true,
          title: '危险操作', icon: 'error-warning-line',
          note: '删除项目将同时删除其全部文档与成员关系,且不可恢复。',
          body: UI.btn({ id: 'ps-delete', label: '删除项目', icon: 'delete-bin-line', kind: 'danger-outline' }),
        })
        : '') +
      '</div>';

    const saveBtn = body.querySelector('#ps-save');
    if (saveBtn) saveBtn.onclick = async () => {
      const name = body.querySelector('#ps-name').value.trim();
      if (!name) { UI.toast('项目名不能为空', 'warning'); return; }
      saveBtn.disabled = true;
      try {
        await api('/api/projects/' + proj.id, {
          method: 'PATCH',
          body: { name, description: body.querySelector('#ps-desc').value },
        });
        UI.toast('已保存', 'success');
        App.refreshSidebar();
        App.refresh();
      } catch (e) {
        saveBtn.disabled = false;
        UI.err(e);
      }
    };

    // 可见性开关:即时提交(勾选/取消都是明确意图,不需要再来一次"保存")
    const pubBox = body.querySelector('#ps-public');
    if (pubBox) pubBox.onchange = async () => {
      const want = pubBox.checked;
      if (want) {
        const ok = await UI.confirmDialog(
          '公开后,任何登录用户都能在「发现」里看到本项目并自助加入。他们加入前看不到项目里的任何内容,' +
          '加入后按你设定的角色获得权限。确定公开?',
          { okText: '公开' });
        if (!ok) { pubBox.checked = false; return; }
      }
      pubBox.disabled = true;
      try {
        await api('/api/projects/' + proj.id, { method: 'PATCH', body: { isPublic: want } });
        UI.toast(want ? '已公开到广场' : '已设为私有', 'success');
        App.refresh(); // 重渲染,让提示文案与徽标跟上
      } catch (e) {
        pubBox.checked = !want;
        pubBox.disabled = false;
        UI.err(e);
      }
    };

    // 自助加入后的角色:同一张卡上的第二档设置,语义与公开开关一致(选了就是明确意图)
    const roleSel = body.querySelector('#ps-join-role');
    if (roleSel) roleSel.onchange = async () => {
      roleSel.disabled = true;
      try {
        await api('/api/projects/' + proj.id, { method: 'PATCH', body: { joinRole: roleSel.value } });
        UI.toast('已更新加入角色', 'success');
        App.refresh();
      } catch (e) {
        roleSel.value = proj.joinRole;
        roleSel.disabled = false;
        UI.err(e);
      }
    };

    const delBtn = body.querySelector('#ps-delete');    if (delBtn) delBtn.onclick = async () => {
      const ok = await UI.confirmDialog('删除项目将同时删除其全部文档与成员关系,且不可恢复。确定删除「' + proj.name + '」?');
      if (!ok) return;
      try {
        await api('/api/projects/' + proj.id, { method: 'DELETE' });
        UI.toast('项目已删除', 'success');
        App.refreshSidebar();
        location.hash = '#/';
      } catch (e) { UI.err(e); }
    };

    const leaveBtn = body.querySelector('#ps-leave');
    if (leaveBtn) leaveBtn.onclick = async () => {
      const ownerHint = proj.myRole === 'OWNER'
        ? '你是项目所有者:若你是唯一所有者,需先在成员页把所有权转让给他人才能退出。'
        : '';
      const ok = await UI.confirmDialog(
        '退出「' + proj.name + '」后,你将不再能访问它的文档、云空间与回收站,最新动态也不再显示。' +
        ownerHint + '确定退出?',
        { okText: '退出' });
      if (!ok) return;
      leaveBtn.disabled = true;
      try {
        await api('/api/projects/' + proj.id + '/leave', { method: 'POST' });
        UI.toast('已退出「' + proj.name + '」', 'success');
        App.refreshSidebar();
        location.hash = '#/';
      } catch (e) {
        leaveBtn.disabled = false;
        UI.err(e);
      }
    };
  };

  // ==================== 文档模块 ====================
  window.Views.projectDocs = async function (container, { projectId, docId }) {
    container.classList.add('view-fill'); // 文档页整链 flex 撑满主区(见 app.css)
    const proj = await projectShell(container, projectId);
    const canEdit = UI.canEdit(proj); // EDITOR 及以上可写;VIEWER 只读,未加入者在 projectShell 就被拦下
    const body = container.querySelector('#proj-body');
    body.innerHTML =
      '<div class="docs-wrap">' +
      '<div class="doc-tree-col">' +
      UI.toolbar({
        cls: 'doc-tree-head',
        left: '<span class="toolbar-title">文档</span>',
        right: canEdit ? UI.btn({ id: 'btn-new-doc', label: '新文档', icon: 'add-line', kind: 'tonal', size: 'sm' }) : '',
      }) +
      '<div id="doc-tree" class="doc-tree"></div></div>' +
      '<div class="doc-editor-col" id="doc-editor-col"></div>' +
      '</div>';

    const treeEl = body.querySelector('#doc-tree');
    const editorCol = body.querySelector('#doc-editor-col');
    let treeData = [];
    const collapsed = new Set(); // 折叠 id 集合,键为 id 字符串(DocTree 内部以 UI.idOf 过形状)

    /** 按文档 id 找节点(「更多」菜单要拿标题回显) */
    function findNode(id) {
      let hit = null;
      UI.walkTree(treeData, (n) => {
        if (UI.sameId(n.id, id)) { hit = n; return false; }
      });
      return hit;
    }

    // 渲染与交互(caret 折叠 / 行导航 / 行内按钮)都在 doc-tree.js 的 DocTree 里
    function renderTree() {
      DocTree.renderTreeInto(treeEl, {
        treeData, activeId: docId, collapsed, canEdit,
        callbacks: {
          onNav: (id) => { location.hash = App.route.project(projectId, 'docs', id); },
          onAdd: (parentId) => createDoc(parentId),
          onMenu: (btn, id) => openDocMenu(btn, id),
        },
      });
    }

    async function loadTree() {
      try {
        treeData = await api('/api/projects/' + projectId + '/docs/tree') || [];
        renderTree();
      } catch (e) {
        treeEl.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message, sm: true, cls: 'm-2' });
      }
    }

    async function createDoc(parentId) {
      try {
        const body2 = { title: '无标题文档' };
        if (parentId) body2.parentId = parentId;
        const d = await api('/api/projects/' + projectId + '/docs', { method: 'POST', body: body2 });
        await loadTree();
        location.hash = App.route.project(projectId, 'docs', d.id);
      } catch (e) { UI.err(e); }
    }

    /** 「更多」下拉:重命名 / 移动到… / 删除;id 已由 DocTree 用 UI.idOf 过形状 */
    function openDocMenu(btn, id) {
      const node = findNode(id);
      UI.dropdownMenu(btn, [
        {
          icon: 'ri-edit-line', label: '重命名', onClick: async () => {
            const title = await UI.inputDialog({ title: '重命名文档', label: '标题', value: node ? node.title : '' });
            if (!title) return;
            try {
              await api('/api/docs/' + id, { method: 'PATCH', body: { title } });
              await loadTree();
              if (id === docId) {
                const t = editorCol.querySelector('#doc-title');
                if (t) t.value = title;
              }
            } catch (err) { UI.err(err); }
          },
        },
        {
          icon: 'ri-folder-transfer-line', label: '移动到…', onClick: () => {
            // 选择器与云空间共用(MoveTarget):项目内改父级、跨项目转移走同一个端点。
            // 跨项目会跳转路由 —— 文档的 projectId 变了,留在旧路由上会立刻 404
            MoveTarget.open({
              item: { id, name: node ? node.title : '' }, kind: 'doc', projectId,
              parentId: (node && node.parentId) || null,
              onDone: async (res) => {
                if (!res || res.projectId === projectId) { await loadTree(); return; }
                window.location.hash = App.route.project(res.projectId, 'docs', res.id);
                App.refreshSidebar();
              },
            });
          },
        },
        {
          icon: 'ri-delete-bin-line', label: '删除', danger: true, onClick: async () => {
            const ok = await UI.confirmDialog('删除后进入回收站,可在项目「回收站」中恢复。确定删除「' + (node ? node.title : '') + '」?');
            if (!ok) return;
            try {
              await api('/api/docs/' + id, { method: 'DELETE' });
              UI.toast('已删除(可在回收站恢复)', 'success');
              await loadTree();
              if (id === docId) location.hash = App.route.project(projectId, 'docs');
            } catch (err) { UI.err(err); }
          },
        },
      ], { align: 'end' });
      btn.click();
    }

    const newDocBtn = body.querySelector('#btn-new-doc');
    if (newDocBtn) newDocBtn.onclick = () => createDoc(null);

    await loadTree();
    if (docId) DocEditorView.open(docId, { projectId, canEdit, editorCol, onTreeChanged: loadTree });
    else {
      editorCol.innerHTML = '';
      editorCol.appendChild(UI.emptyState({
        icon: 'ri-edit-box-line',
        title: '从左侧选择一篇文档',
        desc: canEdit ? '或者创建一篇新文档' : '',
      }));
    }
  };

  // ==================== 云空间模块(复用 drive.js,个人项目同视图) ====================
  // query.folder 是深链目标目录(搜索结果"进入所在目录"、刷新保持位置都用它)
  window.Views.projectFiles = async function (container, { projectId, query }) {
    const proj = await projectShell(container, projectId);
    const folderId = (query && query.get('folder')) || null;
    // highlight:从文档 @文件 引用跳转过来,定位并闪烁该文件行(driveBody 内处理)
    const highlight = (query && query.get('highlight')) || null;
    await window.Views.driveBody(container.querySelector('#proj-body'),
      { projectId, proj, folderId, highlight });
  };
})();
