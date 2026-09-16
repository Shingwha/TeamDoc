// views/doc-editor.js — 文档编辑器与历史版本模态(由 project.js 原样搬移)
//   DocEditorView.open(docId, { projectId, canEdit, editorCol, onTreeChanged })
//     编辑器:**手动保存**(按钮 / Ctrl+S,不再自动保存)、编辑/预览切换、WS 实时协同、
//     反链栏、粘贴拖拽上传、离开前未保存提示、冲突逐块合并
//   DocEditorView.openHistoryModal(docId, canEdit, onRestored, hasUnsaved) — 历史版本模态
//   本模块只被 project.js 调用;App/api/MdRender/Preview/DocEditor 均为运行时引用。
window.DocEditorView = (function () {
  'use strict';

  // WS 保存的等待上限:超过就按失败处理(状态栏说"保存失败",不放行"保存并离开")。
  // 宁可让用户重试,也不要给出"已保存"的假象
  const SAVE_TIMEOUT_MS = 5000;

  // ---------- 编辑器 + 实时协同 ----------
  function openEditor(docId, { projectId, canEdit, editorCol, onTreeChanged }) {
    editorCol.innerHTML =
      '<div class="editor-head">' +
      '<input id="doc-title" class="doc-title-input" placeholder="无标题文档" maxlength="200"' + (canEdit ? '' : ' disabled') + '>' +
      '<span class="save-status" id="save-status"></span>' +
      // 保存是显式动作(不再自动保存):按钮只在脏时可用,Ctrl/Cmd+S 同效
      (canEdit ? UI.btn({ id: 'btn-save', label: '保存', icon: 'save-line', kind: 'filled', size: 'sm', disabled: true }) : '') +
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
        // 不叫"加载最新":你在脏的时候点它会把没保存的正文直接盖掉。
        // 改成打开逐块合并,由人决定哪几处用服务端的
        action: { id: 'btn-load-latest', label: '比较并合并', kind: 'tonal' },
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
    const saveBtn = editorCol.querySelector('#btn-save');
    const remoteBar = editorCol.querySelector('#remote-bar');
    const remoteMsg = editorCol.querySelector('#remote-msg');
    const ta = editorCol.querySelector('#md-source');
    const previewEl = editorCol.querySelector('#md-preview');
    const scrollEl = editorCol.querySelector('#editor-scroll');

    let ws = null;
    let wsReady = false;
    let destroyed = false;
    let lastSaved = '';           // 最近一次已保存的正文
    let lastTitle = '';           // 最近一次已保存的标题
    let editorFocused = false;
    let pendingRemote = null;
    let wsRetries = 0;
    let editorCleanup = null;     // DocEditor.enhance 的清理函数(@/[[ 引用、浮动工具栏、上传)
    let wsRetryTimer = null;      // WS 重连定时器
    let leavePromptOpen = false;  // "未保存就离开"三选弹窗是否已开着(防重入)
    let onBeforeUnload = null;    // 关标签/刷新的拦截器(路由清理时移除)
    // 基线版本:保存时把它一起送给服务端,声明"我这份是基于哪一版改的"。
    // 只在**真正与服务端对齐**时推进(初始加载、saved 回包、采纳远端、REST 保存成功);
    // 收到 remote 但没采纳时**不能**推进 —— 那等于谎报基线
    let baseVersion = 0;
    let conflict = null;          // {content, version, by} 服务端现场;非空 = 保存全部暂停
    let conflictModal = null;     // 冲突弹窗开着时的引用

    // 清理必须在这里注册,不能放在 init() 末尾:init() 里有 await,快速切文档/刷新时
    // 新路由的 runCleanups() 会先跑,前一个编辑器随后才注册 —— 它的 WS 与全局监听
    // 就会残留到下一次路由(照常处理每条 presence 广播)。
    App.onCleanup(() => {
      destroyed = true;
      App.clearLeaveGuard();
      if (onBeforeUnload) { window.removeEventListener('beforeunload', onBeforeUnload); onBeforeUnload = null; }
      if (wsRetryTimer) { clearTimeout(wsRetryTimer); wsRetryTimer = null; }
      if (editorCleanup) { editorCleanup(); editorCleanup = null; }
      try { if (ws) ws.close(); } catch (e) { /* 忽略 */ }
    });
    // 编辑/预览模式:有编辑权默认编辑态并记住上次选择;VIEWER 恒为预览
    let mode = canEdit ? (UI.pref.get('td:doc-mode') || 'edit') : 'preview';

    let saving = false;      // 写入在途
    let saveError = false;   // 上一次写入失败(下次输入或重试时清掉)
    let savedAt = '';        // 最近一次保存成功的 HH:MM

    const setStatus = (t) => { statusEl.textContent = t; };
    /** 脏 = 正文或标题与"已保存值"有任何不同。手动保存下它是按钮可用性的唯一依据,
     *  也是离开拦截的判定 —— 所以它必须同时覆盖标题(否则改了标题点保存会被当成无事发生)。 */
    const dirty = () => ta.value !== lastSaved || (titleEl.value.trim() || '') !== lastTitle;

    /**
     * 状态栏是这些状态的**唯一出口**:冲突 > 写入中 > 失败 > 未保存 > 已保存(时间) > 只读。
     * 各处只改状态量再调它,不要各自拼文案 —— 拼法一定会漂。
     * 顶栏不显示 v{N}:那是"保存次数",显示它会把"改得很勤"误读成"版本很多"。
     */
    function renderStatus() {
      if (conflict) setStatus('有冲突未解决');
      else if (saving) setStatus('保存中…');
      else if (saveError) setStatus('保存失败');
      else if (dirty()) setStatus('未保存');
      else if (!canEdit) setStatus('只读');
      else if (savedAt) setStatus('已保存 ✓ ' + savedAt);
      else setStatus('');
      statusEl.classList.toggle('conflict', !!conflict);
      statusEl.title = conflict ? '点击重新打开冲突处理' : '';
      if (saveBtn) {
        // 冲突态下按钮改成"重开冲突处理"的入口:此刻发起写入只会被再拒一次
        saveBtn.disabled = conflict ? false : !dirty();
        saveBtn.title = conflict ? '有冲突未解决,点击处理' : '保存(Ctrl+S)';
      }
    }

    const markSaved = () => {
      savedAt = new Date().toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
      renderStatus();
    };

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
      if (version != null) baseVersion = version;
      saveError = false;
      markSaved();   // 此时正文与服务端一致;标题若仍是脏的,renderStatus 会如实显示"未保存"
    }

    // ---------- 内容冲突:服务端拒绝时不静默覆盖,逐块交给用户合并 ----------
    let mergePlan = null;   // Diff.build(...) 的产物(冲突期间才有)
    let picks = {};         // {块序号: 'mine' | 'theirs' | 'both'},缺省 mine

    /** 进入冲突态:保存暂停、状态栏常驻提示、弹出逐块合并弹窗。
     *  payload 兼容三种来源:WS 的 conflict 消息(content/version)、REST 的 409 detail
     *  (currentContent/currentVersion)、以及脏的时候点"比较并合并"(远端消息本身)。 */
    function enterConflict(p) {
      const serverContent = (p.currentContent != null ? p.currentContent : p.content) || '';
      const serverVersion = (p.currentVersion != null ? p.currentVersion : p.version);
      conflict = { content: serverContent, version: serverVersion, by: p.by || '' };
      // 服务端那份才是"已保存"的事实:把 lastSaved 拉回它 —— 界面不再显示假成功,
      // dirty() 也恢复为真(本地确实有没写进去的内容)
      lastSaved = serverContent;
      pendingRemote = null;
      remoteBar.hidden = true;
      renderStatus();
      openConflictModal();
    }

    /** 冲突未解决期间又有人保存:把"服务端那一侧"刷新到最新并重画,不打断用户。
     *  已做的选择按**块序号**保留:序号是稳定标识(行号会变),而且重画后每个块的
     *  seg 仍显示着当前选择,用户看得见自己选了什么,不存在"悄悄沿用"的情况。 */
    function refreshConflict(p) {
      if (!conflict) return;
      conflict.content = (p.currentContent != null ? p.currentContent : p.content) || '';
      conflict.version = (p.currentVersion != null ? p.currentVersion : p.version);
      if (p.by) conflict.by = p.by;
      lastSaved = conflict.content;
      renderStatus();
      if (conflictModal) paintConflict();
    }

    function clearConflictState() {
      conflict = null;
      conflictModal = null;
      mergePlan = null;
      picks = {};
      renderStatus();
    }

    /** 画出弹窗内容(首次与"现场又变了"共用):说明一句 + 图例一行 + 逐块差异 */
    function paintConflict() {
      const plan = Diff.build(ta.value, conflict.content);
      mergePlan = plan;
      // 现场变了之后块数可能变少,丢掉越界的选择;其余按序号保留
      Object.keys(picks).forEach((k) => { if (Number(k) >= plan.count) delete picks[k]; });

      const who = conflict.by ? UI.esc(conflict.by) + ' ' : '其他人';
      const body = conflictModal.body;
      body.innerHTML = '';
      const note = document.createElement('p');
      note.className = 'pv-note conflict-note';
      note.innerHTML = who + '在你编辑期间也保存了这篇文档。保存已暂停,逐处选择要保留的内容。';
      const legend = document.createElement('div');
      legend.className = 'df-legend';
      legend.innerHTML = '共 ' + plan.count + ' 处差异 · <b class="cn-del">−</b> 你的版本 · ' +
        '<b class="cn-add">+</b> 服务端版本';
      const diffEl = document.createElement('div');
      diffEl.innerHTML = plan.render(picks);
      // 一次事件委托管所有块 —— 逐块绑监听会在重画时漏掉新块
      diffEl.addEventListener('click', (e) => {
        const b = e.target.closest('.df-hunk .seg button[data-key]');
        if (!b) return;
        picks[b.closest('.df-hunk').dataset.hunk] = b.dataset.key;
        plan.applyPicks(diffEl, picks);
      });
      body.appendChild(note);
      body.appendChild(legend);
      body.appendChild(diffEl);
      conflictModal.diffEl = diffEl;
    }

    function setAllPicks(kind) {
      if (!mergePlan) return;
      for (let i = 0; i < mergePlan.count; i++) picks[i] = kind;
      mergePlan.applyPicks(conflictModal.diffEl, picks);
    }

    function openConflictModal() {
      if (!conflict || destroyed) return;
      if (conflictModal) { paintConflict(); return; }
      conflictModal = { body: null, diffEl: null, handle: null };
      const modal = UI.modal({
        title: '文档已被他人修改',
        wide: true,
        body: '<div class="conflict-body"></div>',
        actions: [
          { id: 'cf-all-mine', kind: 'text', label: '全部用我的',
            onClick: () => setAllPicks('mine') },
          { id: 'cf-all-theirs', kind: 'text', label: '全部用服务端',
            onClick: () => setAllPicks('theirs') },
          { id: 'cf-apply', kind: 'filled', label: '保存合并结果',
            onClick: ({ close }) => { close(null); applyMerge(); } },
        ],
        // Esc / 遮罩 / 右上角关闭 = 暂不处理:弹窗关掉但保存仍然暂停,
        // 状态栏留着可点入口。绝不静默继续 —— 再写一次就是又一次整篇覆盖
        onClose: () => { conflictModal = null; renderStatus(); },
      });
      conflictModal.handle = modal;
      conflictModal.body = modal.body.querySelector('.conflict-body');
      paintConflict();
    }

    /** 应用合并结果。结果与服务端现场完全一致时直接本地采用、不写库
     *  (省掉一次无意义的写入与版本 +1;全选"服务端"会走到这条路)。 */
    function applyMerge() {
      if (!conflict || !mergePlan) return;
      const merged = mergePlan.merge(picks);
      const serverContent = conflict.content;
      const version = conflict.version;
      clearConflictState();
      if (merged === serverContent) {
        applyRemote(serverContent, version);
        UI.toast('已采用服务端版本', 'success');
        return;
      }
      ta.value = merged;
      autosize();
      baseVersion = version;   // 我们就是基于现场这一版合并的,写回去不会再撞
      // null 而不是 '':内容被清空时 '' 会让 saveContent 误判成"没有变化"而不发请求
      lastSaved = null;
      saveContent();
    }

    // ---------- 手动保存 ----------
    // 保存只在这个入口发生(保存按钮 / Ctrl+S / 保存并离开 / 应用合并结果后)。
    // 自动保存已整体移除:写一次就少一次撞冲突的机会,而每次写入都是用户的明确决定。
    // 传输不变:WS 已连走 content 消息,未连上降级 PUT /content(§8);两条都带 baseVersion。
    let pendingSave = null;   // WS 在途的那次保存(由回包或超时兑现)

    /**
     * 保存正文 + 标题。返回 Promise<boolean>:true = 都落库了,false = 有失败
     * (状态栏已说明原因)。「保存并离开」必须等它真成功才能放行,所以 WS 那条路
     * 也要等回包 —— 早先是"发出去就算已保存",那样放行时可能根本没落库。
     */
    function saveContent() {
      if (!canEdit || destroyed) return Promise.resolve(false);
      if (conflict) { openConflictModal(); return Promise.resolve(false); }
      const title = titleEl.value.trim();
      const needTitle = title !== lastTitle && !!title;
      const content = ta.value;
      const needContent = content !== lastSaved;
      if (!needTitle && !needContent) { renderStatus(); return Promise.resolve(true); }
      saving = true;
      saveError = false;
      renderStatus();
      const jobs = [];
      if (needTitle) jobs.push(saveTitle(title));
      if (needContent) jobs.push(sendContent(content));
      return Promise.all(jobs).then((rs) => {
        saving = false;
        renderStatus();
        return rs.every((ok) => ok !== false);
      });
    }

    function saveTitle(title) {
      return api('/api/docs/' + docId, { method: 'PATCH', body: { title } })
        .then(() => {
          lastTitle = title;
          if (onTreeChanged) onTreeChanged();   // 树上的标题要跟着变
          return true;
        })
        .catch((e) => { saveError = true; UI.err(e); return false; });
    }

    function sendContent(content) {
      if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
        return new Promise((resolve) => {
          const piece = { content, resolve, timer: null };
          // 回包可能永远不来(服务端卡住、链路半死)。等不到就按失败处理:不推进
          // lastSaved、不放行"保存并离开" —— 宁可让用户看到"保存失败"再试一次,
          // 也不要给出一个"已保存"的假象
          piece.timer = setTimeout(() => {
            if (pendingSave !== piece) return;
            pendingSave = null;
            saveError = true;
            renderStatus();
            resolve(false);
          }, SAVE_TIMEOUT_MS);
          pendingSave = piece;
          try { ws.send(JSON.stringify({ type: 'content', content, baseVersion })); }
          catch (e) { clearTimeout(piece.timer); pendingSave = null; saveError = true; UI.err(e); resolve(false); }
        });
      }
      return api('/api/docs/' + docId + '/content', { method: 'PUT', body: { content, baseVersion } })
        .then((r) => {
          lastSaved = content;
          if (r && r.version != null) baseVersion = r.version;
          return true;
        })
        .catch((e) => {
          if (e.status === 409 && e.detail) { enterConflict(e.detail); return false; }
          saveError = true;
          UI.err(e);
          return false;
        });
    }

    // 标题与正文共用同一个保存动作(只有正文手动、标题自动会造成"标题悄悄存了、
    // 正文没存"的错位)。输入只更新脏态,不发起写入。
    titleEl.addEventListener('input', () => { saveError = false; renderStatus(); });

    // 正文:输入只更新脏态与 presence,保存交给按钮 / Ctrl+S
    ta.addEventListener('input', () => {
      autosize();
      saveError = false;
      renderStatus();
      sendEditingTrue();
    });
    ta.addEventListener('focus', () => { editorFocused = true; sendEditing(true); });
    ta.addEventListener('blur', () => { editorFocused = false; sendEditing(false); });

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
          // 兑现这次在途的保存:确认落库后才推进 lastSaved / 基线
          if (pendingSave) {
            const piece = pendingSave;
            pendingSave = null;
            clearTimeout(piece.timer);
            lastSaved = piece.content;
            markSaved();
            piece.resolve(true);
          }
          if (msg.version != null) baseVersion = msg.version;
        } else if (msg.type === 'conflict') {
          // 服务端拒绝了这次保存(基线过期)。已有冲突时只刷新"服务端那一侧"
          if (pendingSave) {
            const piece = pendingSave;
            pendingSave = null;
            clearTimeout(piece.timer);
            piece.resolve(false);
          }
          if (conflict) refreshConflict(msg); else enterConflict(msg);
        } else if (msg.type === 'remote') {
          // 冲突未解决期间:别人再保存只更新现场,不打扰
          if (conflict) { refreshConflict(msg); return; }
          // §8 客户端行为:编辑器无焦点且没有未保存改动 → 直接 setValue;否则提示条
          if (!canEdit || (!editorFocused && !dirty())) {
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
      const r = pendingRemote;
      pendingRemote = null;
      remoteBar.hidden = true;
      if (!r) return;
      // 有未保存改动时不是"覆盖",而是打开逐块合并(基线用最新的远端版本,写完不会再撞)。
      // 旧文案「加载最新」是个静默丢改动的按钮:点一下自己的字就没了
      if (dirty()) { enterConflict({ content: r.content, version: r.version, by: r.by }); return; }
      applyRemote(r.content, r.version);
    };

    editorCol.querySelector('#btn-history').onclick = () =>
      openHistoryModal(docId, canEdit, () => reloadDoc(), () => dirty());

    // 状态栏在冲突态下是重开弹窗的入口(弹窗用 Esc/遮罩关掉后从这里回来)
    statusEl.addEventListener('click', () => { if (conflict) openConflictModal(); });

    async function reloadDoc() {
      try {
        const doc = await api('/api/docs/' + docId);
        // 走到这里 = 用户刚做了一次显式的服务端动作(如恢复历史版本),
        // 本地那份已被放弃,冲突随之消解(否则保存会一直卡在暂停态)
        clearConflictState();
        applyRemote(doc.content, doc.version);
        titleEl.value = doc.title || '';
        lastTitle = doc.title || '';
        loadBacklinks();
        if (onTreeChanged) onTreeChanged();
      } catch (e) { UI.err(e); }
    }

    // ---------- 手动保存与"离开前提示"的入口 ----------
    if (saveBtn) saveBtn.addEventListener('click', () => saveContent());

    // Ctrl/Cmd+S:必须 preventDefault,否则会触发浏览器的"保存网页"
    function onKeydown(e) {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        saveContent();
      }
    }
    document.addEventListener('keydown', onKeydown);

    /** 没保存就别走。机制在 app.js(hashchange 先问守卫),这里只给策略。
     *  同一套拦截覆盖:点文档树切文档、点侧栏切路由,以及关闭标签/刷新(beforeunload)。 */
    App.onLeaveGuard(async () => {
      if (destroyed || !canEdit || !dirty()) return true;
      if (leavePromptOpen) return false;   // 已经问过一次,别叠弹窗
      leavePromptOpen = true;
      try {
        const choice = await UI.modal({
          title: '有未保存的改动',
          body: '<p class="pv-note">这篇文档的改动还没有保存到服务器。</p>',
          actions: [
            { kind: 'text', label: '取消', value: 'cancel' },
            { kind: 'text', label: '放弃改动并离开', value: 'discard' },
            { kind: 'filled', label: '保存并离开', value: 'save' },
          ],
        }).result;
        if (choice === 'discard') {
          // 先把脏态清掉再放行,否则下一次守卫还会再问一遍
          lastSaved = ta.value;
          lastTitle = titleEl.value.trim() || '';
          return true;
        }
        if (choice !== 'save') return false;    // 取消 / Esc / 遮罩
        return !!(await saveContent());         // 存成功才放行:失败与冲突都不放
      } finally { leavePromptOpen = false; }
    });

    // 关闭标签 / 刷新:浏览器只认这一种拦截方式(提示文案由浏览器给,改不了)
    onBeforeUnload = (e) => {
      if (destroyed || !canEdit || !dirty()) return;
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    App.onCleanup(() => { document.removeEventListener('keydown', onKeydown); });

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
      const meta = rec && (rec.file || rec);
      const fid = meta && meta.id;
      if (!fid) throw new Error('上传响应缺少文件 id');
      // 是否插成原生 ![](...) = 图片类型 且 服务端 inline 白名单(UI.isEmbedImage):
      // canInline 单独不够 —— 白名单还覆盖 PDF 与全部文本类,插 ![](...) 只会得到坏图;
      // 不加 inline=1 的类型走附件链接(强制下载),语义也一致
      const embedImage = UI.isEmbedImage(meta.mime, meta.canInline);
      let url = '/api/files/' + fid + '/download';
      if (embedImage) url += '?inline=1';
      return { name: f.name, url, isImage: embedImage };
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
        renderStatus();
        UI.err(e);
      }
      connectWs();
    }
    init();
    // 清理在函数开头就已注册(见那里的注释):这里再注册会晚于 await,
    // 快速切文档时新路由的 runCleanups() 会先跑,这一份就漏掉了。
  }

  // ---------- 历史版本模态框(左列表右预览;预览区复用 preview.js,与引用浮层同源) ----------

  /** 版本记录的**展示用词**。kind 是机制层的事实(models.DocVersion.kind),怎么称呼归界面:
   *  普通保存(默认档)不给注解 —— 整张列表通篇都是它,逐行标注只是噪音;
   *  只有回退点值得标出来,因为时间线里出现异常时用户得知道为什么。 */
  const VERSION_TAGS = {
    restore: { text: '回退前', title: '这里发生过一次版本回退,这一条是回退前的现场' },
  };

  /** 一版的展示事实,**唯一来源**:列表行与预览头部都只用它,不许各拼一套(会漂)。
   *  字数在挑版本时比时间更好认 —— "我删掉那一段之前是多大"一眼就能对上。 */
  function versionFacts(v) {
    const tag = VERSION_TAGS[v.kind];
    return {
      when: UI.fmtWhen(v.createdAt),
      size: Number(v.contentChars || 0).toLocaleString('zh-CN') + ' 字',
      who: (v.createdBy && v.createdBy.name) || '—',
      badge: tag ? UI.badge({ text: tag.text, title: tag.title, kind: 'primary', cls: 'xs' }) : '',
    };
  }

  /**
   * @param onRestored  恢复成功后的回调(编辑器侧传 reloadDoc)
   * @param hasUnsaved  可选:返回"当前是否有未保存改动"。恢复会覆盖本地内容,
   *                    有脏改动时确认框必须点明这一点(服务端存的是"还原前"的**已保存**内容)
   */
  async function openHistoryModal(docId, canEdit, onRestored, hasUnsaved) {
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
      // 作者名由服务端解析(与文件列表同一形状):静态页不做跨接口 join
      versions = (await api('/api/docs/' + docId + '/versions')) || [];
      if (!versions.length) {
        listEl.innerHTML = '<div class="muted small p-2">暂无历史版本</div>';
        previewEl.innerHTML = '<div class="muted small p-4">该文档还没有历史版本。</div>';
        return;
      }
      listEl.innerHTML = versions.map((v) => {
        const f = versionFacts(v);
        return UI.listRow({
          // 悬停给秒级完整时间:列表按分钟显示(便于扫读),同一分钟内那几条靠副文本区分
          attrs: 'data-vid="' + UI.esc(v.id) + '" title="' + UI.esc(UI.fmtDate(v.createdAt)) + '"',
          title: UI.esc(f.when),
          badges: f.badge,
          sub: UI.esc(f.who) + ' · ' + f.size,
        });
      }).join('');

      async function showVersion(vid) {
        // vid 可能来自 dataset(字符串),也可能来自 versions[i].id(数字),统一按 id 判定
        listEl.querySelectorAll('.list-row').forEach((x) =>
          x.classList.toggle('selected', UI.sameId(x.dataset.vid, vid)));
        const f = versionFacts(versions.find((x) => x.id === UI.numId(vid)) || {});
        // 事实只在一处说(选中的那一行),这里只放"做了什么"与"能做什么":
        // 同一句话在列表与预览各写一遍,迟早会漂,而且窄栏里必然折行
        previewEl.innerHTML =
          '<div class="hv-actions">' +
            f.badge +
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
          // 服务端会把"还原前"的现场存成历史版本,所以你手上的**已保存**内容不会丢;
          // 但本地未保存的改动会被 reloadDoc 覆盖掉,这一点必须说清
          const extra = hasUnsaved && hasUnsaved() ? '\n你还有没保存的改动,恢复后会丢失。' : '';
          const ok = await UI.confirmDialog('恢复到此版本?当前内容会先自动存为历史版本。' + extra,
            { danger: false, okText: '恢复' });
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
