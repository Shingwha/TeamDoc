// views/project.js — 项目页各模块视图(模块导航 = 侧栏项目树的子项,主区无大页头)
//   Views.projectDocs     — 文档模块:文档树 + Markdown 源码/预览编辑 + 实时协同,本系统最重页面(整页 flex 撑满主区)
//   Views.projectFiles    — 云空间模块:复用 views/drive.js 的 driveBody(项目上下文)
//   Views.projectMembers / projectTrash / projectSettings — 成员 / 回收站 / 项目设置
window.Views = window.Views || {};
(function () {
  'use strict';

  // ---------- 项目页公共壳:仅获取项目对象(权限判断)+ 提供 #proj-body 容器 ----------
  async function projectShell(container, projectId) {
    container.innerHTML = UI.loadingRow();
    const proj = await api('/api/projects/' + encodeURIComponent(projectId));
    container.innerHTML = '<div id="proj-body"></div>';
    return proj;
  }

  // ==================== 成员视图(#/p/{id}/members;个人项目不可见,入口已被导航隐藏) ====================
  window.Views.projectMembers = async function (container, { projectId }) {
    const proj = await projectShell(container, projectId);
    if (proj.isPersonal) { location.replace('#/p/' + projectId + '/docs'); return; }
    const canAdmin = UI.roleRank(proj.myRole) >= 2;
    const body = container.querySelector('#proj-body');
    body.innerHTML =
      '<div class="page-head"><div><h1 class="page-title">成员</h1>' +
      '<div class="page-sub">' + UI.esc(proj.name) + ' · ' + (proj.memberCount != null ? proj.memberCount : '-') + ' 人</div></div></div>' +
      '<div class="card w-list">' +
      '<div id="mb-list">' + UI.loadingRow() + '</div>' +
      (canAdmin
        ? '<div style="display:flex;gap:8px;align-items:flex-end;margin-top:16px;flex-wrap:wrap">' +
          '<div class="field" style="flex:1;min-width:200px;margin-bottom:0"><label>按邮箱添加成员</label>' +
          '<input class="input" id="mb-email" type="email" placeholder="user@example.com"></div>' +
          '<div class="field" style="margin-bottom:0"><label>角色</label>' +
          '<select class="select" id="mb-role" style="width:auto">' +
          '<option value="VIEWER">只读成员</option><option value="EDITOR" selected>编辑者</option>' +
          '<option value="ADMIN">管理员</option><option value="OWNER">所有者</option></select></div>' +
          '<button class="btn btn-filled" id="mb-add" type="button">添加</button></div>'
        : '') +
      '</div>';

    const listEl = body.querySelector('#mb-list');
    async function load() {
      try {
        const members = await api('/api/projects/' + proj.id + '/members');
        if (!(members || []).length) {
          listEl.innerHTML = '';
          listEl.appendChild(UI.emptyState({ icon: 'ri-team-line', title: '暂无成员' }));
          return;
        }
        listEl.innerHTML = members.map((mb) => {
          const u = mb.user || {};
          return '<div class="mrow" data-uid="' + UI.esc(mb.userId) + '">' +
            UI.avatar({ name: u.name || u.email, seed: mb.userId, size: 32 }) +
            '<div class="mrow-main">' +
            '<div class="mrow-title">' + UI.esc(u.name || '-') +
            (u.isDisabled ? ' <span class="badge danger">已禁用</span>' : '') + '</div>' +
            '<div class="mrow-sub">' + UI.esc(u.email || '-') + '</div></div>' +
            (canAdmin
              ? '<select class="select mb-role-sel">' +
                ['OWNER', 'ADMIN', 'EDITOR', 'VIEWER'].map((r) =>
                  '<option value="' + r + '"' + (r === mb.role ? ' selected' : '') + '>' + UI.esc(UI.roleLabel(r)) + '</option>'
                ).join('') + '</select>' +
                '<button class="btn-icon danger mb-remove" type="button" title="移除成员">' + UI.icon('user-unfollow-line') + '</button>'
              : '<span class="badge">' + UI.esc(UI.roleLabel(mb.role)) + '</span>') +
            '</div>';
        }).join('');
      } catch (e) {
        listEl.innerHTML = '<div class="text-danger">' + UI.esc(e.message) + '</div>';
      }
    }
    await load();

    if (!canAdmin) return;
    body.querySelector('#mb-add').onclick = async () => {
      const email = body.querySelector('#mb-email').value.trim();
      const role = body.querySelector('#mb-role').value;
      if (!email) { UI.toast('请填写邮箱', 'warning'); return; }
      try {
        await api('/api/projects/' + proj.id + '/members', { method: 'POST', body: { email, role } });
        UI.toast('已添加成员', 'success');
        body.querySelector('#mb-email').value = '';
        await load();
      } catch (e) { UI.err(e); }
    };
    listEl.addEventListener('change', async (e) => {
      const sel = e.target.closest('.mb-role-sel');
      if (!sel) return;
      const uid = sel.closest('.mrow').dataset.uid;
      try {
        await api('/api/projects/' + proj.id + '/members/' + uid, { method: 'PATCH', body: { role: sel.value } });
        UI.toast('已更新角色', 'success');
      } catch (err) { UI.err(err); await load(); }
    });
    listEl.addEventListener('click', async (e) => {
      const btn = e.target.closest('.mb-remove');
      if (!btn) return;
      const uid = btn.closest('.mrow').dataset.uid;
      if (!(await UI.confirmDialog('确定移除该成员?'))) return;
      try {
        await api('/api/projects/' + proj.id + '/members/' + uid, { method: 'DELETE' });
        UI.toast('已移除', 'success');
        await load();
      } catch (err) { UI.err(err); }
    });
  };

  // ==================== 回收站视图(#/p/{id}/trash) ====================
  // 文档与云空间文件统一在此恢复/彻底删除;彻底删除会清物理文件,不可恢复
  window.Views.projectTrash = async function (container, { projectId }) {
    const proj = await projectShell(container, projectId);
    const body = container.querySelector('#proj-body');
    body.innerHTML =
      '<div class="page-head"><div><h1 class="page-title">回收站</h1>' +
      '<div class="page-sub">' + UI.esc(proj.name) + ' · 已删除的文档和文件可在此恢复;彻底删除不可恢复</div></div></div>' +
      '<div class="card w-list"><div id="trash-list">' + UI.loadingRow() + '</div></div>';

    const listEl = body.querySelector('#trash-list');
    const canWrite = UI.roleRank(proj.myRole) >= 1; // EDITOR 及以上可恢复/彻底删除

    function rowHtml(item, kind) {
      const isDoc = kind === 'doc';
      return '<div class="mrow" data-kind="' + kind + '" data-id="' + UI.esc(item.id) + '">' +
        UI.icon(isDoc ? 'file-text-line' : 'file-line') +
        '<div class="mrow-main"><div class="mrow-title">' + UI.esc(isDoc ? item.title : item.name) + '</div>' +
        '<div class="mrow-sub">' + (isDoc ? '文档' : '文件' + (item.size != null ? ' · ' + UI.fmtSize(item.size) : '')) +
        ' · 删除于 ' + UI.esc(UI.fmtDate(item.deletedAt)) + '</div></div>' +
        (canWrite
          ? '<button class="btn btn-outline btn-sm tr-restore" type="button">恢复</button>' +
            '<button class="btn-icon btn-sm danger tr-purge" type="button" title="彻底删除"><i class="ri-delete-bin-line"></i></button>'
          : '') +
        '</div>';
    }

    async function load() {
      try {
        const data = await api('/api/projects/' + proj.id + '/trash');
        const docs = data.docs || [], files = data.files || [];
        if (!docs.length && !files.length) {
          listEl.innerHTML = '';
          listEl.appendChild(UI.emptyState({ icon: 'ri-delete-bin-line', title: '回收站为空' }));
          return;
        }
        listEl.innerHTML =
          (docs.length ? '<div class="tr-group">文档(' + docs.length + ')</div>' +
            docs.map((d) => rowHtml(d, 'doc')).join('') : '') +
          (files.length ? '<div class="tr-group">文件(' + files.length + ')</div>' +
            files.map((f) => rowHtml(f, 'file')).join('') : '');
      } catch (e) {
        listEl.innerHTML = '<div class="text-danger">' + UI.esc(e.message) + '</div>';
      }
    }
    await load();

    listEl.addEventListener('click', async (e) => {
      const row = e.target.closest('.mrow');
      if (!row) return;
      const id = row.dataset.id, kind = row.dataset.kind;
      const base = kind === 'doc' ? '/api/docs/' : '/api/files/';
      if (e.target.closest('.tr-restore')) {
        try {
          const r = await api(base + id + '/restore', { method: 'POST' });
          UI.toast('已恢复 ' + ((r && r.restored) || 1) + ' 项', 'success');
          await load();
        } catch (err) { UI.err(err); }
        return;
      }
      if (e.target.closest('.tr-purge')) {
        const name = row.querySelector('.mrow-title').textContent;
        const ok = await UI.confirmDialog('彻底删除「' + name + '」?' +
          (kind === 'doc' ? '将连同子文档与历史版本一起清除,' : '物理文件将被删除,') + '此操作不可恢复。');
        if (!ok) return;
        try {
          await api(base + id + '/permanent', { method: 'DELETE' });
          UI.toast('已彻底删除', 'success');
          await load();
        } catch (err) { UI.err(err); }
      }
    });
  };

  // ==================== 设置视图(#/p/{id}/settings) ====================
  window.Views.projectSettings = async function (container, { projectId }) {
    const proj = await projectShell(container, projectId);
    const rank = UI.roleRank(proj.myRole);
    const canEdit = rank >= 2;              // ADMIN 及以上可改;VIEWER 只读
    const canDelete = rank >= 3 && !proj.isPersonal; // 仅 OWNER;个人项目永不显示
    const dis = canEdit ? '' : ' disabled';
    const body = container.querySelector('#proj-body');
    body.innerHTML =
      '<div class="page-head"><div><h1 class="page-title">设置</h1>' +
      '<div class="page-sub">我的角色:' + UI.esc(UI.roleLabel(proj.myRole)) +
      ' · ' + (proj.memberCount != null ? proj.memberCount : '-') + ' 成员' +
      ' · ' + (proj.docCount != null ? proj.docCount : '-') + ' 文档' +
      (proj.isPersonal ? ' · 个人空间' : '') + '</div></div></div>' +
      '<div class="card w-form">' +
      '<div class="field"><label>项目名</label>' +
      '<input class="input" id="ps-name" maxlength="100" value="' + UI.esc(proj.name) + '"' + dis + '></div>' +
      '<div class="field"><label>描述</label>' +
      '<textarea class="textarea" id="ps-desc" rows="3"' + dis + '>' + UI.esc(proj.description || '') + '</textarea></div>' +
      (canEdit ? '<button class="btn btn-filled" id="ps-save" type="button">保存</button>' : '') +
      '</div>' +
      (canDelete
        ? '<div class="card w-form" style="margin-top:16px">' +
          '<div class="card-title" style="color:var(--md-error)">' + UI.icon('error-warning-line') + ' 危险操作</div>' +
          '<p class="small muted" style="margin:0 0 12px">删除项目将同时删除其全部文档与成员关系,且不可恢复。</p>' +
          '<button class="btn btn-danger-outline btn-sm" id="ps-delete" type="button">' +
          UI.icon('delete-bin-line') + ' 删除项目</button></div>'
        : '');

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

    const delBtn = body.querySelector('#ps-delete');
    if (delBtn) delBtn.onclick = async () => {
      const ok = await UI.confirmDialog('删除项目将同时删除其全部文档与成员关系,且不可恢复。确定删除「' + proj.name + '」?');
      if (!ok) return;
      try {
        await api('/api/projects/' + proj.id, { method: 'DELETE' });
        UI.toast('项目已删除', 'success');
        App.refreshSidebar();
        location.hash = '#/';
      } catch (e) { UI.err(e); }
    };
  };

  // ==================== 文档模块 ====================
  window.Views.projectDocs = async function (container, { projectId, docId }) {
    container.classList.add('view-fill'); // 文档页整链 flex 撑满主区(见 app.css)
    const proj = await projectShell(container, projectId);
    const canEdit = UI.roleRank(proj.myRole) >= 1; // EDITOR 及以上可写(VIEWER 只读)
    const body = container.querySelector('#proj-body');
    body.innerHTML =
      '<div class="docs-wrap">' +
      '<div class="doc-tree-col">' +
      '<div class="doc-tree-head"><span>文档</span>' +
      (canEdit ? '<button id="btn-new-doc" class="btn btn-tonal btn-sm" type="button"><i class="ri-add-line"></i>新文档</button>' : '') +
      '</div><div id="doc-tree" class="doc-tree"></div></div>' +
      '<div class="doc-editor-col" id="doc-editor-col"></div>' +
      '</div>';

    const treeEl = body.querySelector('#doc-tree');
    const editorCol = body.querySelector('#doc-editor-col');
    let treeData = [];
    const collapsed = new Set();

    function findNode(nodes, id) {
      for (const n of nodes) {
        if (n.id === id) return n;
        const hit = findNode(n.children || [], id);
        if (hit) return hit;
      }
      return null;
    }

    function treeHtml(nodes) {
      return '<ul class="doc-ul">' + nodes.map((n) => {
        const kids = n.children || [];
        const isCollapsed = collapsed.has(n.id);
        return '<li>' +
          '<div class="doc-row' + (n.id === docId ? ' active' : '') + '" data-id="' + UI.esc(n.id) + '">' +
          '<span class="doc-caret' + (kids.length ? (isCollapsed ? '' : ' open') : ' leaf') + '"><i class="ri-arrow-right-s-line"></i></span>' +
          UI.icon('file-text-line') +
          '<span class="doc-name" title="' + UI.esc(n.title) + '">' + UI.esc(n.title) + '</span>' +
          (canEdit
            ? '<span class="doc-acts">' +
              '<button class="btn-icon btn-sm act-add" type="button" title="新建子文档"><i class="ri-add-line"></i></button>' +
              '<button class="btn-icon btn-sm act-more" type="button" title="更多"><i class="ri-more-fill"></i></button>' +
              '</span>'
            : '') +
          '</div>' +
          (kids.length ? '<div class="doc-children"' + (isCollapsed ? ' hidden' : '') + '>' + treeHtml(kids) + '</div>' : '') +
          '</li>';
      }).join('') + '</ul>';
    }

    function renderTree() {
      treeEl.innerHTML = '';
      if (!treeData.length) {
        treeEl.appendChild(UI.emptyState({
          icon: 'ri-article-line',
          title: '还没有文档',
          desc: canEdit ? '创建第一篇文档开始写作' : '等待项目成员创建文档',
          action: canEdit ? { label: '创建第一篇文档', onClick: () => createDoc(null) } : null,
        }));
        return;
      }
      treeEl.innerHTML = treeHtml(treeData);
    }

    async function loadTree() {
      try {
        treeData = await api('/api/projects/' + projectId + '/docs/tree') || [];
        renderTree();
      } catch (e) {
        treeEl.innerHTML = '<div class="text-danger small" style="padding:8px">' + UI.esc(e.message) + '</div>';
      }
    }

    async function createDoc(parentId) {
      try {
        const body2 = { title: '无标题文档' };
        if (parentId) body2.parentId = parentId;
        const d = await api('/api/projects/' + projectId + '/docs', { method: 'POST', body: body2 });
        await loadTree();
        location.hash = '#/p/' + projectId + '/docs/' + d.id;
      } catch (e) { UI.err(e); }
    }

    function openDocMenu(btn) {
      const row = btn.closest('.doc-row');
      const id = row.dataset.id;
      const node = findNode(treeData, id);
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
          icon: 'ri-delete-bin-line', label: '删除', danger: true, onClick: async () => {
            const ok = await UI.confirmDialog('删除后进入回收站,可在项目「回收站」中恢复。确定删除「' + (node ? node.title : '') + '」?');
            if (!ok) return;
            try {
              await api('/api/docs/' + id, { method: 'DELETE' });
              UI.toast('已删除(可在回收站恢复)', 'success');
              await loadTree();
              if (id === docId) location.hash = '#/p/' + projectId + '/docs';
            } catch (err) { UI.err(err); }
          },
        },
      ], { align: 'end' });
      btn.click();
    }

    // 树交互(事件委托)
    treeEl.addEventListener('click', async (e) => {
      const caret = e.target.closest('.doc-caret');
      if (caret && !caret.classList.contains('leaf')) {
        const row = caret.closest('.doc-row');
        const id = row.dataset.id;
        if (collapsed.has(id)) collapsed.delete(id); else collapsed.add(id);
        renderTree();
        e.stopPropagation();
        return;
      }
      const add = e.target.closest('.act-add');
      if (add) { e.stopPropagation(); createDoc(add.closest('.doc-row').dataset.id); return; }
      const more = e.target.closest('.act-more');
      if (more) { e.stopPropagation(); openDocMenu(more); return; }
      const row = e.target.closest('.doc-row');
      if (row) location.hash = '#/p/' + projectId + '/docs/' + row.dataset.id;
    });

    const newDocBtn = body.querySelector('#btn-new-doc');
    if (newDocBtn) newDocBtn.onclick = () => createDoc(null);

    await loadTree();
    if (docId) openEditor(docId, { projectId, canEdit, editorCol, onTreeChanged: loadTree });
    else {
      editorCol.innerHTML = '';
      editorCol.appendChild(UI.emptyState({
        icon: 'ri-edit-box-line',
        title: '从左侧选择一篇文档',
        desc: canEdit ? '或者创建一篇新文档' : '',
      }));
    }
  };

  // ---------- 编辑器 + 实时协同 ----------
  function openEditor(docId, { projectId, canEdit, editorCol, onTreeChanged }) {
    editorCol.innerHTML =
      '<div class="editor-head">' +
      '<input id="doc-title" class="doc-title-input" placeholder="无标题文档" maxlength="200"' + (canEdit ? '' : ' disabled') + '>' +
      '<span class="doc-ver" id="doc-ver"></span>' +
      '<span class="save-status" id="save-status"></span>' +
      '<span id="presence-inline" class="presence-inline"></span>' +
      (canEdit
        ? '<div class="seg" id="doc-mode-seg">' +
          '<button type="button" data-mode="edit"><i class="ri-edit-line"></i>编辑</button>' +
          '<button type="button" data-mode="preview"><i class="ri-eye-line"></i>预览</button>' +
          '</div>'
        : '') +
      '<button id="btn-history" class="btn btn-text btn-sm" type="button"><i class="ri-history-line"></i>历史</button>' +
      '</div>' +
      '<div id="remote-bar" class="remote-bar" hidden>' +
      UI.icon('information-line') + '<span id="remote-msg"></span>' +
      '<button id="btn-load-latest" class="btn btn-tonal btn-sm" type="button">加载最新</button>' +
      '</div>' +
      // 单一滚动容器:滚动条在页面右缘;正文居中
      '<div class="editor-scroll" id="editor-scroll">' +
      '<div class="editor-canvas">' +
      '<div class="doc-content">' +
      '<textarea id="md-source" class="md-source" placeholder="开始编写 Markdown 文档…输入 @ 或 [[ 引用文档与文件" spellcheck="false"' + (canEdit ? '' : ' hidden') + '></textarea>' +
      '<div id="md-preview" class="markdown-body doc-preview"' + (canEdit ? ' hidden' : '') + '></div>' +
      // 反链栏在文档内容流末尾(跟随滚动),不占编辑器底部的固定空间
      '<div class="doc-backlinks" id="doc-backlinks" hidden></div>' +
      '</div>' +
      '</div></div>';

    const titleEl = editorCol.querySelector('#doc-title');
    const verEl = editorCol.querySelector('#doc-ver');
    const statusEl = editorCol.querySelector('#save-status');
    const remoteBar = editorCol.querySelector('#remote-bar');
    const remoteMsg = editorCol.querySelector('#remote-msg');
    const ta = editorCol.querySelector('#md-source');
    const previewEl = editorCol.querySelector('#md-preview');
    const scrollEl = editorCol.querySelector('#editor-scroll');

    let ws = null;
    let wsReady = false;
    let destroyed = false;
    let lastSaved = '';           // 最近一次已保存内容
    let lastTitle = '';
    let editorFocused = false;
    let pendingRemote = null;
    let wsRetries = 0;
    let editorCleanup = null;     // DocEditor.enhance 的清理函数(@/[[ 引用、浮动工具栏、上传)
    // 编辑/预览模式:有编辑权默认编辑态并记住上次选择;VIEWER 恒为预览
    let mode = canEdit ? (localStorage.getItem('td:doc-mode') || 'edit') : 'preview';

    const setStatus = (t) => { statusEl.textContent = t; };
    const setVersion = (v) => { if (v != null) verEl.textContent = 'v' + v; };
    const hasPending = () => ta.value !== lastSaved;

    function renderPreview() { return MdRender.mount(previewEl, ta.value); }

    // 编辑态 textarea 自动撑高(自身不滚动,滚动统一由 .editor-scroll 承担)。
    // 注意:height='auto' 的瞬间 textarea 塌缩会把 scrollTop 钳到 0(焦点移走时表现为"跳回顶部"),
    // 必须保存并恢复滚动位置
    function autosize() {
      const st = scrollEl.scrollTop;
      ta.style.height = 'auto';
      ta.style.height = ta.scrollHeight + 'px';
      scrollEl.scrollTop = st;
    }

    function setMode(m) {
      mode = m;
      if (canEdit) localStorage.setItem('td:doc-mode', m);
      ta.hidden = m !== 'edit';
      previewEl.hidden = m !== 'preview';
      const seg = editorCol.querySelector('#doc-mode-seg');
      if (seg) seg.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.mode === m));
      if (m === 'preview') renderPreview();
      else { autosize(); ta.focus(); }
    }

    const segEl = editorCol.querySelector('#doc-mode-seg');
    if (segEl) segEl.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-mode]');
      if (b && b.dataset.mode !== mode) setMode(b.dataset.mode);
    });

    function applyRemote(content, version) {
      const c = content || '';
      // 远端覆盖是全文替换,尽量保留光标位置(程序赋值不触发 input,无需抑制回环)
      const s = ta.selectionStart, epos = ta.selectionEnd;
      ta.value = c;
      if (document.activeElement === ta) ta.setSelectionRange(Math.min(s, c.length), Math.min(epos, c.length));
      if (mode === 'preview') renderPreview(); else autosize();
      lastSaved = c;
      setVersion(version);
      setStatus(canEdit ? '已保存 ✓' : '只读');
    }

    // 自动保存:输入防抖 800ms;WS 已连走 content 消息,未连上降级 PUT /content(§8)
    const scheduleSave = UI.debounce(() => saveContent(), 800);
    function saveContent() {
      if (!canEdit || destroyed) return;
      const content = ta.value;
      if (content === lastSaved) return;
      setStatus('保存中…');
      if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
        lastSaved = content; // 服务端回 saved 后更新版本号
        ws.send(JSON.stringify({ type: 'content', content }));
      } else {
        api('/api/docs/' + docId + '/content', { method: 'PUT', body: { content } })
          .then((r) => {
            lastSaved = content;
            setStatus('已保存 ✓');
            if (r && r.version != null) setVersion(r.version);
          })
          .catch((e) => { setStatus('保存失败'); UI.err(e); });
      }
    }

    // 标题:失焦 / 防抖 600ms PATCH
    const scheduleTitle = UI.debounce(() => saveTitle(), 600);
    async function saveTitle() {
      const t = titleEl.value.trim();
      if (!canEdit || !t || t === lastTitle) return;
      try {
        await api('/api/docs/' + docId, { method: 'PATCH', body: { title: t } });
        lastTitle = t;
        onTreeChanged && onTreeChanged();
      } catch (e) { UI.err(e); }
    }
    titleEl.addEventListener('input', scheduleTitle);
    titleEl.addEventListener('blur', () => scheduleTitle.flush());

    // 正文:input 防抖保存 + presence;失焦立即保存
    ta.addEventListener('input', () => {
      autosize();
      setStatus('');
      scheduleSave();
      sendEditingTrue();
    });
    ta.addEventListener('focus', () => { editorFocused = true; sendEditing(true); });
    ta.addEventListener('blur', () => { editorFocused = false; sendEditing(false); saveContent(); });

    // presence:编辑中标记(§8)
    function sendEditing(editing) {
      if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: 'presence', editing })); } catch (e) { /* 忽略 */ }
      }
    }
    const sendEditingTrue = UI.debounce(() => sendEditing(true), 200);

    function updatePresence(users) {
      const html = (users || []).map((u) =>
        UI.avatar({
          name: u.name, seed: u.userId, size: 26, editing: u.editing,
          title: (u.name || '') + (u.editing ? '(编辑中)' : ''),
        })
      ).join('');
      editorCol.querySelector('#presence-inline').innerHTML = html;
    }

    function connectWs() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      // 同源连接,浏览器自动带 Cookie(§8)
      ws = new WebSocket(proto + '://' + location.host + '/ws/docs/' + docId);
      ws.onopen = () => { wsReady = true; wsRetries = 0; };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'presence') {
          updatePresence(msg.users || []);
        } else if (msg.type === 'saved') {
          setStatus('已保存 ✓');
          setVersion(msg.version);
        } else if (msg.type === 'remote') {
          // §8 客户端行为:编辑器无焦点且无待保存本地改动 → 直接 setValue;否则提示条
          if (!canEdit || (!editorFocused && !hasPending())) {
            pendingRemote = null;
            remoteBar.hidden = true;
            applyRemote(msg.content, msg.version);
          } else {
            pendingRemote = msg;
            remoteMsg.textContent = (msg.by || '其他成员') + ' 更新了文档';
            remoteBar.hidden = false;
          }
        }
      };
      ws.onclose = (ev) => {
        wsReady = false;
        if (destroyed) return;
        if (ev.code === 4401) { UI.toast('登录已失效,请重新登录', 'danger'); location.hash = '#/login'; return; }
        if (ev.code === 4404) { UI.toast('文档不存在或已被删除', 'warning'); return; }
        // 异常断开:最多重连 3 次、间隔 3s;断开期间保存自动降级 PUT
        if (wsRetries < 3) {
          wsRetries++;
          setTimeout(() => { if (!destroyed) connectWs(); }, 3000);
        }
      };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* 忽略 */ } };
    }

    editorCol.querySelector('#btn-load-latest').onclick = () => {
      if (pendingRemote) applyRemote(pendingRemote.content, pendingRemote.version);
      pendingRemote = null;
      remoteBar.hidden = true;
    };

    editorCol.querySelector('#btn-history').onclick = () =>
      openHistoryModal(docId, canEdit, () => reloadDoc());

    async function reloadDoc() {
      try {
        const doc = await api('/api/docs/' + docId);
        applyRemote(doc.content, doc.version);
        titleEl.value = doc.title || '';
        lastTitle = doc.title || '';
        loadBacklinks();
        if (onTreeChanged) onTreeChanged();
      } catch (e) { UI.err(e); }
    }

    // ---------- 反链栏:同项目内引用了本文档的文档(teamdoc://doc 链接) ----------
    const backlinksEl = editorCol.querySelector('#doc-backlinks');
    async function loadBacklinks() {
      try {
        const rows = await api('/api/docs/' + docId + '/backlinks');
        if (destroyed) return;
        if (!rows || !rows.length) { backlinksEl.hidden = true; return; }
        backlinksEl.hidden = false;
        backlinksEl.innerHTML = '<span>被引用</span>' + rows.map((d) =>
          '<button type="button" class="bl-chip" data-id="' + UI.esc(d.id) + '" title="更新于 ' +
          UI.esc(UI.fmtDate(d.updatedAt)) + '">' + UI.esc(d.title) + '</button>'
        ).join('');
      } catch (e) { if (!destroyed) backlinksEl.hidden = true; }
    }
    backlinksEl.addEventListener('click', (e) => {
      const b = e.target.closest('.bl-chip');
      if (b) location.hash = '#/p/' + projectId + '/docs/' + b.dataset.id;
    });

    // 上传:POST /api/files/upload(projectId 必填),图片带 inline=1 便于预览直显。
    // 编辑器上传统一归入项目根目录的「文档附件」文件夹(没有则自动创建),避免弄乱云空间根列表。
    let attachFolderPromise = null;
    function ensureAttachFolder() {
      if (!attachFolderPromise) {
        attachFolderPromise = (async () => {
          const r = await api('/api/files?project_id=' + encodeURIComponent(projectId));
          const found = (r.folders || []).find((f) => f.name === '文档附件');
          if (found) return found.id;
          const f = await api('/api/files/folders', { method: 'POST', body: { projectId, name: '文档附件' } });
          return f.id;
        })().catch((e) => { attachFolderPromise = null; throw e; });
      }
      return attachFolderPromise;
    }
    async function uploadFile(f) {
      const folderId = await ensureAttachFolder();
      const fd = new FormData();
      fd.append('file', f);
      fd.append('projectId', projectId);
      fd.append('folderId', folderId);
      if (f.type) fd.append('mime', f.type);
      const rec = await api('/api/files/upload', { method: 'POST', body: fd });
      const fid = rec && (rec.id || (rec.file && rec.file.id));
      if (!fid) throw new Error('上传响应缺少文件 id');
      let url = '/api/files/' + fid + '/download';
      if ((f.type || '').startsWith('image/')) url += '?inline=1';
      return { name: f.name, url };
    }

    async function init() {
      // 编辑器增强:@ 与 [[ 引用浮层 / 选区浮动工具栏 / 粘贴拖拽上传(VIEWER 只读不启用)
      if (canEdit && window.DocEditor) editorCleanup = DocEditor.enhance(ta, { projectId: projectId, upload: uploadFile });
      setMode(mode);
      try {
        const doc = await api('/api/docs/' + docId);
        if (destroyed) return;
        applyRemote(doc.content, doc.version);
        titleEl.value = doc.title || '';
        lastTitle = doc.title || '';
        loadBacklinks();
      } catch (e) {
        setStatus('');
        UI.err(e);
      }
      connectWs();
    }
    init();

    // 路由离开时清理,防止泄漏(纯 CSS 变量主题,编辑器无需随主题重建)
    App.onCleanup(() => {
      destroyed = true;
      scheduleSave.cancel();
      scheduleTitle.cancel();
      if (editorCleanup) { editorCleanup(); editorCleanup = null; }
      try { if (ws) ws.close(); } catch (e) { /* 忽略 */ }
    });
  }

  // ---------- 历史版本模态框(列表 → 预览 → 恢复) ----------
  async function openHistoryModal(docId, canEdit, onRestored) {
    const m = UI.modal({
      title: '历史版本',
      wide: true,
      body:
        '<div class="hv-layout">' +
        '<div class="hv-list" id="hv-list">' + UI.loadingRow() + '</div>' +
        '<div class="hv-preview" id="hv-preview">' +
        '<div class="muted small" style="padding:16px">选择左侧版本进行预览</div></div>' +
        '</div>',
      actions: [{ label: '关闭', kind: 'text', value: null }],
    });
    const listEl = m.body.querySelector('#hv-list');
    const previewEl = m.body.querySelector('#hv-preview');
    let selectedVid = null;
    let canRestore = canEdit;

    try {
      const versions = await api('/api/docs/' + docId + '/versions');
      if (!(versions || []).length) {
        listEl.innerHTML = '<div class="muted small" style="padding:8px">暂无历史版本</div>';
      } else {
        listEl.innerHTML = versions.map((v) =>
          '<div class="hv-item" data-vid="' + UI.esc(v.id) + '">' +
          '<div class="hv-label">' + UI.esc(v.label || '未命名版本') + '</div>' +
          '<div class="hv-meta">' + UI.esc(UI.fmtDate(v.createdAt)) +
          (v.createdBy ? ' · ' + UI.esc(v.createdBy) : '') + '</div>' +
          '</div>'
        ).join('');
      }
    } catch (e) {
      listEl.innerHTML = '<div class="text-danger small">' + UI.esc(e.message) + '</div>';
    }

    listEl.addEventListener('click', async (e) => {
      const item = e.target.closest('.hv-item');
      if (!item) return;
      const vid = item.dataset.vid;
      selectedVid = vid;
      listEl.querySelectorAll('.hv-item').forEach((x) => x.classList.toggle('selected', x === item));
      try {
        const v = await api('/api/docs/' + docId + '/versions/' + vid);
        previewEl.innerHTML = '';
        // 预览与正文预览同一条渲染路径(js/markdown.js,raw HTML 已转义防 XSS)
        const md = document.createElement('div');
        md.className = 'markdown-body hv-md';
        previewEl.appendChild(md);
        MdRender.mount(md, v.content || '');
        if (canRestore) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'btn btn-filled btn-sm';
          btn.style.margin = '8px';
          btn.textContent = '恢复到此版本';
          btn.addEventListener('click', async () => {
            const ok = await UI.confirmDialog('恢复到此版本?当前内容会先自动存为历史版本。', { danger: false, okText: '恢复' });
            if (!ok) return;
            try {
              await api('/api/docs/' + docId + '/versions/' + selectedVid + '/restore', { method: 'POST' });
              m.close(true);
              UI.toast('已恢复到该版本', 'success');
              onRestored && onRestored();
            } catch (err) { UI.err(err); }
          });
          previewEl.appendChild(btn);
        }
      } catch (err) { UI.err(err); }
    });
  }

  // ==================== 云空间模块(复用 drive.js,个人项目同视图) ====================
  window.Views.projectFiles = async function (container, { projectId }) {
    const proj = await projectShell(container, projectId);
    await window.Views.driveBody(container.querySelector('#proj-body'), { projectId, myRole: proj.myRole });
  };
})();
