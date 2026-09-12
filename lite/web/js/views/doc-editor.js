// views/doc-editor.js — 文档编辑器与历史版本模态(由 project.js 原样搬移)
//   DocEditorView.open(docId, { projectId, canEdit, editorCol, onTreeChanged })
//     编辑器:标题/正文保存、编辑/预览切换、WS 实时协同、反链栏、粘贴拖拽上传
//   DocEditorView.openHistoryModal(docId, canEdit, onRestored) — 历史版本模态
//   逻辑未动;仅做三处工具化清理:renderPreview 的错误条改 UI.errorBanner、历史模态的
//   id 比较改 UI.sameId/UI.numId、编辑模式偏好改 UI.pref(seg 的 active 切换改 UI.segSet)。
//   本模块只被 project.js 调用;App/api/MdRender/Preview/DocEditor 均为运行时引用。
window.DocEditorView = (function () {
  'use strict';

  // ---------- 编辑器 + 实时协同 ----------
  function openEditor(docId, { projectId, canEdit, editorCol, onTreeChanged }) {
    editorCol.innerHTML =
      '<div class="editor-head">' +
      '<input id="doc-title" class="doc-title-input" placeholder="无标题文档" maxlength="200"' + (canEdit ? '' : ' disabled') + '>' +
      '<span class="save-status" id="save-status"></span>' +
      '<span id="presence-inline" class="presence-inline"></span>' +
      (canEdit
        ? UI.seg({
          id: 'doc-mode-seg',
          active: null, // 初始态由 JS 按偏好存储设置,见下方 setMode
          items: [
            { key: 'edit', label: '编辑', icon: 'edit-line' },
            { key: 'preview', label: '预览', icon: 'eye-line' },
          ],
        })
        : '') +
      UI.btn({ id: 'btn-history', label: '历史', icon: 'history-line', kind: 'text', size: 'sm' }) +
      '</div>' +
      UI.banner({
        kind: 'primary', icon: 'information-line', id: 'remote-bar', cls: 'remote-bar', hidden: true,
        html: '<span id="remote-msg"></span>',
        action: { id: 'btn-load-latest', label: '加载最新', kind: 'tonal' },
      }) +
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
    let scheduleSave = null;      // 输入防抖(见下方赋值):声明提前,清理函数才够早注册
    let scheduleTitle = null;
    let wsRetryTimer = null;      // WS 重连定时器

    // 清理必须在这里注册,不能放在 init() 末尾:init() 里有 await,快速切文档/刷新时
    // 新路由的 runCleanups() 会先跑,前一个编辑器随后才注册 —— 它的 WS 与全局监听
    // 就会残留到下一次路由(照常处理每条 presence 广播)。
    App.onCleanup(() => {
      destroyed = true;
      if (scheduleSave) scheduleSave.cancel();
      if (scheduleTitle) scheduleTitle.cancel();
      if (wsRetryTimer) { clearTimeout(wsRetryTimer); wsRetryTimer = null; }
      if (editorCleanup) { editorCleanup(); editorCleanup = null; }
      try { if (ws) ws.close(); } catch (e) { /* 忽略 */ }
    });
    // 编辑/预览模式:有编辑权默认编辑态并记住上次选择;VIEWER 恒为预览
    let mode = canEdit ? (UI.pref.get('td:doc-mode') || 'edit') : 'preview';

    const setStatus = (t) => { statusEl.textContent = t; };
    /**
     * 保存成功的时间戳。顶栏不再显示 v{N} —— 那个数字是"保存次数"(每次内容变化 +1),
     * 不是版本数,显示它会把"改得很勤"误读成"版本很多"。用户真正关心的是"我改的东西存下了没有、什么时候存的"。
     */
    const markSaved = () => setStatus('已保存 ✓ ' +
      new Date().toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' }));
    const hasPending = () => ta.value !== lastSaved;

    function renderPreview() {
      // MdRender.mount 返回 Promise(内部 marked 是异步懒加载的)。
      // 不接住它的话,解析异常会变成未处理的 rejection —— 预览区停在旧内容,
      // 只在控制台报错,用户不知道发生了什么。
      return Promise.resolve(MdRender.mount(previewEl, ta.value)).catch((e) => {
        previewEl.innerHTML = UI.errorBanner({ message: '预览渲染失败:' + (e.message || '未知错误') });
      });
    }

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
      if (canEdit) UI.pref.set('td:doc-mode', m);
      ta.hidden = m !== 'edit';
      previewEl.hidden = m !== 'preview';
      const seg = editorCol.querySelector('#doc-mode-seg');
      if (seg) UI.segSet(seg, m);
      if (m === 'preview') renderPreview();
      else { autosize(); ta.focus(); }
    }

    const segEl = editorCol.querySelector('#doc-mode-seg');
    if (segEl) segEl.addEventListener('click', (e) => {
      // 段标识是 data-key(UI.seg 的统一约定),勿再写回 data-mode
      const b = e.target.closest('button[data-key]');
      if (b && b.dataset.key !== mode) setMode(b.dataset.key);
    });

    function applyRemote(content, version) {
      const c = content || '';
      // 远端覆盖是全文替换,尽量保留光标位置(程序赋值不触发 input,无需抑制回环)
      const s = ta.selectionStart, epos = ta.selectionEnd;
      ta.value = c;
      if (document.activeElement === ta) ta.setSelectionRange(Math.min(s, c.length), Math.min(epos, c.length));
      if (mode === 'preview') renderPreview(); else autosize();
      lastSaved = c;
      if (canEdit) markSaved(); else setStatus('只读');
    }

    // 自动保存:输入防抖 800ms;WS 已连走 content 消息,未连上降级 PUT /content(§8)
    scheduleSave = UI.debounce(() => saveContent(), 800);
    function saveContent() {
      if (!canEdit || destroyed) return;
      const content = ta.value;
      if (content === lastSaved) return;
      setStatus('保存中…');
      if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
        lastSaved = content; // 服务端回 saved 后即为已保存
        ws.send(JSON.stringify({ type: 'content', content }));
      } else {
        api('/api/docs/' + docId + '/content', { method: 'PUT', body: { content } })
          .then(() => {
            lastSaved = content;
            markSaved();
          })
          .catch((e) => { setStatus('保存失败'); UI.err(e); });
      }
    }

    // 标题:失焦 / 防抖 600ms PATCH
    scheduleTitle = UI.debounce(() => saveTitle(), 600);
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
          name: u.name, seed: u.userId, size: 26, editing: u.editing, color: u.avatarColor,
          title: (u.name || '') + (u.editing ? '(编辑中)' : ''),
        })
      ).join('');
      editorCol.querySelector('#presence-inline').innerHTML = html;
    }

    // 服务端会主动关闭连接的"不可恢复"关闭码:身份/文档状态不会因为重连而改变,
    // 继续重连只是每 3 秒打一次服务端
    const WS_FATAL = {
      4401: { text: '登录已失效,请重新登录', kind: 'danger', toLogin: true },
      4403: { text: '你已不在该项目中或权限已变更,实时协作已断开', kind: 'warning' },
      4404: { text: '文档不存在或已被删除', kind: 'warning' },
    };

    function connectWs() {
      if (destroyed) return;  // 视图已销毁:失败重试的定时器/在途 init 都可能走到这里
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      // 同源连接,浏览器自动带 Cookie(§8)
      ws = new WebSocket(proto + '://' + location.host + '/ws/docs/' + docId);
      ws.onopen = () => { wsReady = true; };
      ws.onmessage = (ev) => {
        // 收到任何消息 = 这条连接真的被服务端接纳了(服务端在注册后立刻广播
        // presence),此时才清零重试计数。不能写在 onopen:服务端是"先 accept 再
        // 鉴权/出错关闭",被踢的连接也会触发 onopen —— 那样"最多重连 3 次"永不
        // 生效,变成每 3 秒无限重连,每次重连又去查库,反过来把服务端拖住。
        wsRetries = 0;
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'presence') {
          updatePresence(msg.users || []);
        } else if (msg.type === 'saved') {
          markSaved();
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
        const fatal = WS_FATAL[ev.code];
        if (fatal) {
          UI.toast(fatal.text, fatal.kind);
          if (fatal.toLogin) location.hash = '#/login';
          return;
        }
        // 异常断开:最多重连 3 次,指数退避 + 抖动(多标签同时断线时别一起打服务端)。
        // 断开期间保存自动降级为 PUT /content,功能不受影响;超过 3 次就停,不再空转。
        if (wsRetries < 3) {
          wsRetries++;
          const delay = Math.min(30000, 3000 * Math.pow(2, wsRetries - 1)) + Math.random() * 1000;
          if (wsRetryTimer) clearTimeout(wsRetryTimer);
          wsRetryTimer = setTimeout(() => { wsRetryTimer = null; if (!destroyed) connectWs(); }, delay);
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
      // raw body 上传:元数据走 query string(见 drive.js xhrUpload 的说明)
      let qs = '?projectId=' + encodeURIComponent(projectId) +
        '&name=' + encodeURIComponent(f.name);
      if (folderId) qs += '&folderId=' + encodeURIComponent(folderId);
      // 走 apiUpload 而不是 api():api() 的 30 秒总超时对上传是错的,稍大的附件
      // 必然在传完之前被 abort;apiUpload 只做"空闲超时"(见 api.js)
      const rec = await apiUpload('/api/files/upload' + qs, f);
      const fid = rec && (rec.id || (rec.file && rec.file.id));
      if (!fid) throw new Error('上传响应缺少文件 id');
      // 是否插成原生图片由服务端判定(canInline):svg 等不在白名单的类型
      // 加 ?inline=1 只会下载,插成 ![]() 就是一张坏图
      let url = '/api/files/' + fid + '/download';
      if (rec && rec.canInline) url += '?inline=1';
      return { name: f.name, url, isImage: !!(rec && rec.canInline) };
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
    // 清理在函数开头就已注册(见那里的注释):这里再注册会晚于 await,
    // 快速切文档时新路由的 runCleanups() 会先跑,这一份就漏掉了。
  }

  // ---------- 历史版本模态框(左列表右预览;预览区复用 preview.js,与引用浮层同源) ----------
  async function openHistoryModal(docId, canEdit, onRestored) {
    const m = UI.modal({
      title: '历史版本',
      wide: true,
      body:
        '<div class="hv-layout">' +
        '<div class="hv-list" id="hv-list">' + UI.loadingRow() + '</div>' +
        '<div class="hv-preview" id="hv-preview"></div>' +
        '</div>',
    });
    const listEl = m.body.querySelector('#hv-list');
    const previewEl = m.body.querySelector('#hv-preview');
    let versions = null;

    try {
      // 操作人名称:目录接口所有登录用户可读;拿不到就退回显示 ID 前缀
      const [vers, dir] = await Promise.all([
        api('/api/docs/' + docId + '/versions'),
        api('/api/users/directory').catch(() => []),
      ]);
      versions = vers || [];
      const nameOf = (id) => {
        const u = (dir || []).find((x) => x.id === id);
        return u ? u.name : (id ? String(id).slice(0, 8) + '…' : '-');
      };
      if (!versions.length) {
        listEl.innerHTML = '<div class="muted small p-2">暂无历史版本</div>';
        previewEl.innerHTML = '<div class="muted small p-4">该文档还没有历史版本。</div>';
        return;
      }
      listEl.innerHTML = versions.map((v, i) =>
        UI.listRow({
          attrs: 'data-vid="' + UI.esc(v.id) + '"',
          title: UI.esc((i === 0 ? '最新 · ' : '') + (v.label || '未命名版本')),
          sub: UI.esc(UI.fmtDate(v.createdAt)) + ' · ' + UI.esc(nameOf(v.createdBy)),
        })
      ).join('');

      async function showVersion(vid) {
        // vid 可能来自 dataset(字符串),也可能来自 versions[i].id(数字),统一按 id 判定
        listEl.querySelectorAll('.list-row').forEach((x) =>
          x.classList.toggle('selected', UI.sameId(x.dataset.vid, vid)));
        const v = versions.find((x) => x.id === UI.numId(vid)) || {};
        previewEl.innerHTML =
          '<div class="hv-actions">' +
            Preview.meta([
              { iconName: 'history-line', text: (v.label || '未命名版本') + ' · ' + UI.fmtDate(v.createdAt) },
              { iconName: 'user-line', text: nameOf(v.createdBy) },
            ]) +
            (canEdit ? UI.btn({ id: 'hv-restore', label: '恢复到此版本', icon: 'restart-line', kind: 'filled', size: 'sm' }) : '') +
          '</div>' +
          '<div class="pv-body hv-body"></div>';
        // 预览与正文预览同一条渲染路径(preview.js:raw HTML 已转义防 XSS)
        Preview.fill(previewEl.querySelector('.hv-body'), {
          kind: 'markdown',
          text: api('/api/docs/' + docId + '/versions/' + vid).then((r) => r.content || ''),
        });
        const btn = previewEl.querySelector('#hv-restore');
        if (btn) btn.addEventListener('click', async () => {
          const ok = await UI.confirmDialog('恢复到此版本?当前内容会先自动存为历史版本。', { danger: false, okText: '恢复' });
          if (!ok) return;
          try {
            await api('/api/docs/' + docId + '/versions/' + vid + '/restore', { method: 'POST' });
            m.close(true);
            UI.toast('已恢复到该版本', 'success');
            onRestored && onRestored();
          } catch (err) { UI.err(err); }
        });
      }

      listEl.addEventListener('click', (e) => {
        const item = e.target.closest('.list-row');
        if (item && item.dataset.vid) showVersion(item.dataset.vid);
      });
      // 打开即预览最新一个版本,不用再手点一下
      showVersion(versions[0].id);
    } catch (e) {
      listEl.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message, sm: true });
    }
  }

  return { open: openEditor, openHistoryModal: openHistoryModal };
})();
