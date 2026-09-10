// views/drive.js — 云空间视图(飞书式统一列表:文件夹在前、文件在后,单个列表)
//   统一以项目上下文工作(scope 已随"个人空间并入个人项目"从契约中移除);
//   个人云空间 = 个人项目(isPersonal)的 files tab,与项目云空间复用同一视图。
//   上传:XHR 进度 + 并发 3 队列 + 右下角上传面板;支持文件夹(选择/拖拽,保留目录结构)。
//   多选:行首 checkbox,批量打包下载(zip)/ 删除;删除 = 进项目回收站。
//   Views.driveBody — 渲染体,由 project.js 的 files tab 调用
window.Views = window.Views || {};
(function () {
  'use strict';

  /** 按 mime 给出图标与配色 */
  function fileIcon(mime) {
    mime = mime || '';
    if (mime.startsWith('image/')) return { icon: 'image-line', cls: 'fi-image' };
    if (mime === 'application/pdf') return { icon: 'file-pdf-2-line', cls: 'fi-pdf' };
    if (mime.indexOf('word') >= 0) return { icon: 'file-word-2-line', cls: 'fi-word' };
    if (mime.indexOf('excel') >= 0 || mime.indexOf('spreadsheet') >= 0) return { icon: 'file-excel-2-line', cls: 'fi-excel' };
    if (mime.indexOf('presentation') >= 0 || mime.indexOf('powerpoint') >= 0) return { icon: 'file-ppt-2-line', cls: 'fi-ppt' };
    if (mime.indexOf('zip') >= 0 || mime.indexOf('compressed') >= 0 || mime.indexOf('tar') >= 0) return { icon: 'file-zip-line', cls: 'fi-zip' };
    if (mime.startsWith('text/') || mime.indexOf('json') >= 0 || mime.indexOf('markdown') >= 0) return { icon: 'file-text-line', cls: 'fi-text' };
    if (mime.startsWith('audio/') || mime.startsWith('video/')) return { icon: 'file-music-line', cls: 'fi-media' };
    return { icon: 'file-line', cls: 'fi-default' };
  }

  // 预览能力由服务端判定(行内带 canInline / isText,见 files.py 的 _INLINE_MIME)。
  // 前端不再本地按 mime 前缀猜 —— 否则白名单一收紧就会出现"预览按钮在、点了却下载"。
  // 注:菜单/搜索结果等没有服务端标志位的场景,用 canInlineMime 做粗略兜底。
  const canInlineMime = (mime) => !!mime && (mime.startsWith('image/') || mime === 'application/pdf');

  function downloadUrl(id, inline) {
    return '/api/files/' + encodeURIComponent(id) + '/download' + (inline ? '?inline=1' : '');
  }

  window.Views.driveBody = async function (container, { projectId, myRole }) {
    // 归属转移(项目间移动)发起方需源项目 ADMIN 及以上
    const canMoveOut = UI.roleRank(myRole) >= 2;
    let folderId = null;
    // 注:API 没有「查询文件夹父链」的端点,面包屑路径由前端在会话内维护(刷新后回到根目录)
    let stack = [{ id: null, name: '全部文件' }];
    let sortKey = 'name'; // name | time
    let sortDir = 1;      // 1 升序 / -1 降序

    container.innerHTML =
      UI.toolbar({
        left: '<nav class="crumb" id="drive-crumb"></nav>',
        right:
          UI.btn({ id: 'btn-mkdir', label: '新建文件夹', icon: 'folder-add-line', kind: 'tonal', size: 'sm' }) +
          UI.btn({ id: 'btn-upload-dir', label: '上传文件夹', icon: 'folder-upload-line', kind: 'tonal', size: 'sm' }) +
          UI.btn({ id: 'btn-upload', label: '上传', icon: 'upload-2-line', kind: 'filled', size: 'sm' }) +
          '<input type="file" id="upload-input" multiple hidden>' +
          '<input type="file" id="upload-dir-input" webkitdirectory hidden>',
      }) +
      UI.tableHead(
        [
          { html: '<button class="dh-sort" data-sort="name" type="button">名称 <i class="ri-arrow-up-line"></i></button>' },
          { html: '大小' },
          { html: '<button class="dh-sort" data-sort="time" type="button">时间 <i class="ri-subtract-line"></i></button>', cls: 'dh-time' },
          { html: '' },
        ],
        {
          id: 'drive-table', headId: 'drive-head',
          cls: 'drive-table',
          checkAll: '<input type="checkbox" id="drive-check-all" class="sel-box" title="全选">',
          batch:
            '<span class="batch-info" id="drive-batch-info"></span>' +
            UI.btn({ id: 'batch-dl', label: '打包下载', icon: 'download-2-line', kind: 'tonal', size: 'sm' }) +
            UI.btn({ id: 'batch-del', label: '删除', icon: 'delete-bin-line', kind: 'danger-outline', size: 'sm' }) +
            UI.btn({ id: 'batch-cancel', label: '取消', kind: 'text', size: 'sm' }),
        }
      ) +
      '<div id="drive-rows">' + UI.loadingRow() + '</div>' +
      '<div id="drive-trunc" hidden></div>' +
      '</div>' +
      '<div class="up-panel" id="up-panel" hidden>' +
      '<div class="up-head"><span id="up-title">上传</span>' +
      UI.iconBtn({ id: 'up-close', icon: 'close-line', title: '收起', size: 'sm' }) + '</div>' +
      '<div class="up-list" id="up-list"></div>' +
      '</div>';

    const crumbEl = container.querySelector('#drive-crumb');
    const tableEl = container.querySelector('#drive-table');
    const rowsEl = container.querySelector('#drive-rows');
    const uploadInput = container.querySelector('#upload-input');
    const uploadDirInput = container.querySelector('#upload-dir-input');
    const uploadBtn = container.querySelector('#btn-upload');
    const headEl = container.querySelector('#drive-head');
    const batchInfo = container.querySelector('#drive-batch-info');
    const checkAll = container.querySelector('#drive-check-all');
    const upPanel = container.querySelector('#up-panel');
    const upList = container.querySelector('#up-list');
    const upTitle = container.querySelector('#up-title');
    const truncEl = container.querySelector('#drive-trunc');
    const selected = new Set(); // 多选状态:"{kind}:{id}"
    let activeUploads = 0;      // 进行中上传数(>0 时上传面板不可收起)

    function renderCrumb() {
      crumbEl.innerHTML = stack.map((s, i) =>
        (i > 0 ? '<i class="ri-arrow-right-s-line crumb-sep"></i>' : '') +
        (i === stack.length - 1
          ? '<span class="crumb-item current">' + UI.esc(s.name) + '</span>'
          : '<button class="crumb-item" type="button" data-crumb="' + i + '">' + UI.esc(s.name) + '</button>')
      ).join('');
    }

    function sortIconHtml(key) {
      if (sortKey !== key) return UI.icon('subtract-line');
      return UI.icon(sortDir === 1 ? 'arrow-up-line' : 'arrow-down-line');
    }

    function renderHeadSorts() {
      tableEl.querySelectorAll('.dh-sort').forEach((b) => {
        b.innerHTML = (b.dataset.sort === 'name' ? '名称 ' : '时间 ') + sortIconHtml(b.dataset.sort);
      });
    }

    function bySort(a, b) {
      let r = 0;
      if (sortKey === 'time') r = String(a.createdAt || '').localeCompare(String(b.createdAt || ''));
      else r = String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN');
      return r * sortDir;
    }

    function rowHtml(f, isFolder) {
      const fi = isFolder ? { icon: 'folder-fill', cls: 'folder' } : fileIcon(f.mime);
      const referenced = !isFolder && f.referenced;
      // 服务端已判定可否 inline(白名单);缺字段的旧响应回落到本地粗略判断
      const canPreview = !isFolder && (f.canInline != null ? f.canInline : canInlineMime(f.mime));
      const key = (isFolder ? 'folder:' : 'file:') + f.id;
      const check = '<input type="checkbox" class="sel-box"' + (selected.has(key) ? ' checked' : '') + '>';
      const acts =
        (isFolder
          ? UI.iconBtn({ icon: 'edit-line', title: '重命名', size: 'sm', cls: 'act-rename' }) +
            UI.iconBtn({ icon: 'delete-bin-line', title: '删除', danger: true, size: 'sm', cls: 'act-del' })
          : (canPreview
              ? UI.iconBtn({ icon: 'eye-line', title: '预览', size: 'sm', cls: 'act-preview' })
              : '') +
            UI.iconBtn({ icon: 'download-2-line', title: '下载', size: 'sm', cls: 'act-download' }) +
            UI.iconBtn({ icon: 'edit-line', title: '重命名', size: 'sm', cls: 'act-rename' }) +
            (canMoveOut ? UI.iconBtn({ icon: 'share-forward-line', title: '移动到其他项目', size: 'sm', cls: 'act-move' }) : '') +
            UI.iconBtn({ icon: 'delete-bin-line', title: '删除', danger: true, size: 'sm', cls: 'act-del' }));
      return UI.tableRow(
        [
          {
            html: UI.cellName({
              icon: fi.icon, iconCls: fi.cls,
              link: '<button class="row-link" type="button" title="' + UI.esc(f.name) + '">' + UI.esc(f.name) + '</button>',
              badges: referenced
                ? '<span class="row-ref-badge" title="有文档引用了此文件,删除后引用将失效">被引用</span>' : '',
            }),
          },
          { html: UI.cellMeta(isFolder ? '—' : UI.esc(UI.fmtSize(f.size)), 'row-size') },
          { html: UI.cellMeta(UI.esc(UI.fmtDate(f.createdAt)), 'row-time') },
        ],
        {
          check: check,
          acts: acts,
          selected: selected.has(key),
          attrs: 'data-kind="' + (isFolder ? 'folder' : 'file') + '"' +
            ' data-id="' + UI.esc(f.id) + '" data-name="' + UI.esc(f.name) + '"' +
            ' data-mime="' + UI.esc(f.mime || '') + '"' +
            ' data-caninline="' + (canPreview ? '1' : '') + '"' +
            ' data-referenced="' + (referenced ? '1' : '') + '"',
        }
      );
    }

    function renderList(data) {
      const folders = (data.folders || []).slice().sort(bySort);
      const files = (data.files || []).slice().sort(bySort);
      renderHeadSorts();
      // 服务端单次最多返回 500 项:超出时提示,避免"文件不见了"的误解
      const total = data.total || {};
      const extra = Math.max(0, (total.folders || 0) - folders.length)
        + Math.max(0, (total.files || 0) - files.length);
      if (extra > 0) {
        truncEl.hidden = false;
        truncEl.className = 'banner warn sm mt-3';
        truncEl.innerHTML = UI.icon('alert-line') +
          '<span>当前目录共 ' + ((total.folders || 0) + (total.files || 0)) + ' 项,仅显示前 500 项' +
          '(文件夹 ' + folders.length + ' / ' + (total.folders || 0) +
          ',文件 ' + files.length + ' / ' + (total.files || 0) + ')。建议拆分到子文件夹。</span>';
      } else {
        truncEl.hidden = true;
        truncEl.innerHTML = '';
      }
      if (!folders.length && !files.length) {
        rowsEl.innerHTML = '';
        rowsEl.appendChild(UI.emptyState({
          icon: 'cloud-line',
          title: '这里还是空的',
          desc: '上传文件或新建文件夹,也可以直接把文件拖进来',
          action: { label: '上传文件', onClick: () => uploadInput.click() },
        }));
        return;
      }
      // 统一列表:文件夹在前、文件在后
      rowsEl.innerHTML = folders.map((f) => rowHtml(f, true)).join('') + files.map((f) => rowHtml(f, false)).join('');
    }

    async function load() {
      // project_id 必填;folder_id 进入文件夹时携带
      let qs = '?project_id=' + encodeURIComponent(projectId);
      if (folderId) qs += '&folder_id=' + encodeURIComponent(folderId);
      try {
        const data = await api('/api/files' + qs);
        selected.clear(); // 切换目录后选择集失效(行元素重建,旧 key 可能已不在当前目录)
        renderCrumb();
        renderList(data);
        updateBatchBar();
      } catch (e) {
        rowsEl.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message });
      }
    }

    function rowOf(el) {
      const row = el.closest('.data-table-row');
      if (!row) return null;
      return { id: row.dataset.id, name: row.dataset.name, mime: row.dataset.mime, kind: row.dataset.kind,
               canInline: row.dataset.caninline === '1', referenced: row.dataset.referenced === '1' };
    }

    function triggerDownload(f) {
      const a = document.createElement('a');
      a.href = downloadUrl(f.id, false);
      a.download = f.name || '';
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    // ---------- 多选与批量操作 ----------
    function selectedRows() {
      return [...rowsEl.querySelectorAll('.data-table-row')]
        .filter((r) => selected.has(r.dataset.kind + ':' + r.dataset.id))
        .map((r) => ({ id: r.dataset.id, name: r.dataset.name, mime: r.dataset.mime, kind: r.dataset.kind,
                       canInline: r.dataset.caninline === '1',
                       referenced: r.dataset.referenced === '1' }));
    }

    function updateBatchBar() {
      const rows = selectedRows();
      // 表头变身:有选择时同一行原地切换为批量操作(Gmail 式),列表不位移
      headEl.classList.toggle('selecting', rows.length > 0);
      const fileCount = rows.filter((r) => r.kind === 'file').length;
      batchInfo.textContent = '已选 ' + rows.length + ' 项' +
        (fileCount !== rows.length ? '(文件夹不参与打包)' : '');
      container.querySelector('#batch-dl').disabled = !fileCount;
      const boxes = [...rowsEl.querySelectorAll('.sel-box')];
      checkAll.checked = boxes.length > 0 && boxes.every((b) => b.checked);
      checkAll.indeterminate = !checkAll.checked && boxes.some((b) => b.checked);
    }

    function setRowSelected(row, on) {
      const key = row.dataset.kind + ':' + row.dataset.id;
      if (on) selected.add(key); else selected.delete(key);
      row.classList.toggle('selected', on);
      row.querySelector('.sel-box').checked = on;
    }

    checkAll.addEventListener('change', () => {
      rowsEl.querySelectorAll('.data-table-row').forEach((r) => setRowSelected(r, checkAll.checked));
      updateBatchBar();
    });

    container.querySelector('#batch-cancel').onclick = () => {
      selected.clear();
      rowsEl.querySelectorAll('.data-table-row').forEach((r) => setRowSelected(r, false));
      updateBatchBar();
    };

    container.querySelector('#batch-dl').onclick = () => {
      const files = selectedRows().filter((r) => r.kind === 'file');
      if (!files.length) return;
      if (files.length === 1) { triggerDownload(files[0]); return; }
      // 与单文件下载同一套锚点机制:window.open 对附件流可能被拦/开空白页
      const a = document.createElement('a');
      a.href = '/api/files/zip?ids=' + files.map((f) => encodeURIComponent(f.id)).join(',');
      a.download = '文件打包.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();
    };

    container.querySelector('#batch-del').onclick = async () => {
      const rows = selectedRows();
      if (!rows.length) return;
      const folders = rows.filter((r) => r.kind === 'folder');
      const tip = '将删除 ' + rows.length + ' 项(可在项目「回收站」恢复)。' +
        (folders.length ? '文件夹仅可删除空的,非空会跳过。' : '') +
        '确定删除?';
      if (!(await UI.confirmDialog(tip))) return;
      let okCount = 0, failCount = 0;
      for (const r of rows) {
        try {
          const url = r.kind === 'folder' ? '/api/files/folders/' + r.id : '/api/files/' + r.id;
          await api(url, { method: 'DELETE' });
          okCount++;
        } catch { failCount++; }
      }
      selected.clear();
      UI.toast('已删除 ' + okCount + ' 项' + (failCount ? ',' + failCount + ' 项失败(文件夹非空?)' : ''),
        failCount ? 'warning' : 'success');
      await load();
    };

    // 列表交互(事件委托)
    rowsEl.addEventListener('click', async (e) => {
      const f = rowOf(e.target);
      if (!f) return;

      if (e.target.classList.contains('sel-box')) {
        setRowSelected(e.target.closest('.data-table-row'), e.target.checked);
        updateBatchBar();
        return;
      }
      if (e.target.closest('.act-rename')) {
        const name = await UI.inputDialog({
          title: f.kind === 'folder' ? '重命名文件夹' : '重命名文件',
          label: '名称', value: f.name,
        });
        if (!name) return;
        try {
          const url = f.kind === 'folder' ? '/api/files/folders/' + f.id : '/api/files/' + f.id;
          await api(url, { method: 'PATCH', body: { name } });
          UI.toast('已重命名', 'success');
          await load();
        } catch (err) { UI.err(err); }
        return;
      }
      if (e.target.closest('.act-del')) {
        const tip = f.kind === 'folder'
          ? '仅可删除空文件夹。删除后进入项目回收站,可随时恢复。确定删除「' + f.name + '」?'
          : f.referenced
            ? '「' + f.name + '」正被文档引用,删除后引用将失效(恢复前)。仍要删除?'
            : '删除后进入项目回收站,可随时恢复。确定删除「' + f.name + '」?';
        if (!(await UI.confirmDialog(tip))) return;
        try {
          const url = f.kind === 'folder' ? '/api/files/folders/' + f.id : '/api/files/' + f.id;
          await api(url, { method: 'DELETE' });
          UI.toast('已删除(可在回收站恢复)', 'success');
          selected.delete(f.kind + ':' + f.id);
          await load();
        } catch (err) { UI.err(err); }
        return;
      }
      if (e.target.closest('.act-preview')) { window.open(downloadUrl(f.id, true), '_blank'); return; }
      if (e.target.closest('.act-download')) { triggerDownload(f); return; }
      if (e.target.closest('.act-move')) { openMoveModal(f, projectId, load); return; }

      // 点击名称:文件夹进入;可预览文件打开预览,其余下载
      if (e.target.closest('.row-link')) {
        if (f.kind === 'folder') {
          stack.push({ id: f.id, name: f.name });
          folderId = f.id;
          await load();
        } else if (f.canInline) {
          window.open(downloadUrl(f.id, true), '_blank');
        } else {
          triggerDownload(f);
        }
      }
    });

    // 列头排序(名称 / 时间,升序再点切降序)
    tableEl.querySelectorAll('.dh-sort').forEach((b) => {
      b.addEventListener('click', () => {
        if (sortKey === b.dataset.sort) sortDir = -sortDir;
        else { sortKey = b.dataset.sort; sortDir = 1; }
        load();
      });
    });

    // 面包屑回跳
    crumbEl.addEventListener('click', async (e) => {
      const a = e.target.closest('[data-crumb]');
      if (!a) return;
      const idx = Number(a.dataset.crumb);
      stack = stack.slice(0, idx + 1);
      folderId = stack[stack.length - 1].id;
      await load();
    });

    // 新建文件夹:{name, projectId, parentId?}
    container.querySelector('#btn-mkdir').onclick = async () => {
      const name = await UI.inputDialog({ title: '新建文件夹', label: '文件夹名称' });
      if (!name) return;
      try {
        const body = { name, projectId };
        if (folderId) body.parentId = folderId;
        await api('/api/files/folders', { method: 'POST', body });
        UI.toast('已创建文件夹', 'success');
        await load();
      } catch (err) { UI.err(err); }
    };

    // ---------- 上传:队列(并发 3)+ 进度面板 + 文件夹结构保留 ----------
    const CONCURRENCY = 3;
    const upQueue = [];
    let upRunning = 0, upDone = 0, upTotal = 0;

    function updateUpTitle() {
      upTitle.textContent = upRunning || upQueue.length
        ? '上传中 ' + upDone + '/' + upTotal : '上传完成';
    }

    function addUpRow(name) {
      const row = document.createElement('div');
      row.className = 'up-row';
      row.innerHTML = '<div class="up-info"><span class="up-name" title="' + UI.esc(name) + '">' + UI.esc(name) +
        '</span><span class="up-state">等待中</span></div>' +
        '<div class="up-bar"><i style="width:0%"></i></div>';
      upList.appendChild(row);
      return row;
    }

    function setUpState(row, pct, text, cls) {
      row.querySelector('.up-bar i').style.width = Math.max(0, Math.min(100, pct)) + '%';
      const st = row.querySelector('.up-state');
      st.textContent = text;
      st.className = 'up-state' + (cls ? ' ' + cls : '');
    }

    // XHR 上传(fetch 拿不到上传进度);凭据走同源 Cookie,与 api() 一致
    function xhrUpload(file, targetFolderId, onProgress) {
      return new Promise((resolve, reject) => {
        const x = new XMLHttpRequest();
        x.open('POST', '/api/files/upload');
        x.upload.onprogress = (e) => {
          if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
        };
        x.onload = () => {
          if (x.status >= 200 && x.status < 300) resolve();
          else {
            let msg = 'HTTP ' + x.status;
            try { msg = (JSON.parse(x.responseText).detail || {}).message || msg; } catch { /* 忽略解析失败 */ }
            reject(new Error(msg));
          }
        };
        x.onerror = () => reject(new Error('网络错误'));
        const fd = new FormData();
        fd.append('file', file);
        fd.append('projectId', projectId);
        if (targetFolderId) fd.append('folderId', targetFolderId);
        if (file.type) fd.append('mime', file.type);
        x.send(fd);
      });
    }

    function pumpQueue() {
      while (upRunning < CONCURRENCY && upQueue.length) {
        const task = upQueue.shift();
        upRunning++;
        updateUpTitle();
        xhrUpload(task.file, task.folderId, (pct) => setUpState(task.row, pct, pct + '%'))
          .then(() => setUpState(task.row, 100, '完成', 'ok'))
          .catch((err) => setUpState(task.row, 100, err.message, 'fail'))
          .finally(() => {
            upRunning--;
            upDone++;
            activeUploads = upRunning + upQueue.length;
            updateUpTitle();
            if (!activeUploads) load(); // 队列清空后统一刷新列表
            pumpQueue();
          });
      }
    }

    // 入队一组任务:{file, folderId}
    function enqueue(tasks) {
      if (!tasks.length) return;
      upPanel.hidden = false;
      tasks.forEach((t) => {
        t.row = addUpRow(t.file.name);
        upQueue.push(t);
      });
      upTotal += tasks.length;
      activeUploads = upRunning + upQueue.length;
      updateUpTitle();
      pumpQueue();
    }

    container.querySelector('#up-close').onclick = () => {
      if (activeUploads) return; // 上传中不收起,避免误解为取消
      upPanel.hidden = true;
      upList.innerHTML = '';
      upDone = 0; upTotal = 0;
    };

    // 目录上传:按相对路径逐级建文件夹(路径→folderId 会话内缓存,整批先串行建好再传,避免并发建重名)
    const dirCache = new Map(); // "{parentId|''}/{路径}" → folderId
    async function ensureDirPath(parts) {
      let parentId = folderId; // 起点 = 当前目录
      let prefix = (parentId || '') + '/';
      for (const part of parts) {
        prefix += part + '/';
        if (dirCache.has(prefix)) { parentId = dirCache.get(prefix); continue; }
        const body = { name: part, projectId };
        if (parentId) body.parentId = parentId;
        const created = await api('/api/files/folders', { method: 'POST', body });
        dirCache.set(prefix, created.id);
        parentId = created.id;
      }
      return parentId;
    }

    // 入队带相对路径的文件(来自 webkitdirectory / 拖拽目录)
    async function enqueueWithPaths(items) {
      const tasks = [];
      try {
        for (const it of items) {
          const parts = (it.relPath || '').split('/').filter(Boolean);
          const dir = parts.length ? await ensureDirPath(parts.slice(0, -1)) : folderId;
          tasks.push({ file: it.file, folderId: dir });
        }
      } catch (err) { UI.err(err); return; }
      enqueue(tasks);
    }

    uploadBtn.onclick = () => uploadInput.click();
    container.querySelector('#btn-upload-dir').onclick = () => uploadDirInput.click();
    uploadInput.addEventListener('change', () => {
      const files = Array.from(uploadInput.files || []);
      uploadInput.value = '';
      enqueue(files.map((f) => ({ file: f, folderId })));
    });
    uploadDirInput.addEventListener('change', () => {
      const files = Array.from(uploadDirInput.files || []);
      uploadDirInput.value = '';
      enqueueWithPaths(files.map((f) => ({ file: f, relPath: f.webkitRelativePath || f.name })));
    });

    // 拖拽:优先走 webkitGetAsEntry 递归目录(保留结构),降级为纯文件列表
    function readEntry(entry, prefix) {
      return new Promise((resolve) => {
        if (entry.isFile) {
          entry.file((file) => resolve([{ file, relPath: prefix + file.name }]), () => resolve([]));
        } else if (entry.isDirectory) {
          const reader = entry.createReader();
          const entries = [];
          const readBatch = () => {
            // readEntries 每次最多 100 项,必须循环读到空为止
            reader.readEntries((batch) => {
              if (!batch.length) {
                Promise.all(entries.map((en) => readEntry(en, prefix + entry.name + '/')))
                  .then((groups) => resolve(groups.flat()));
              } else {
                entries.push(...batch);
                readBatch();
              }
            }, () => resolve([]));
          };
          readBatch();
        } else resolve([]);
      });
    }

    ['dragenter', 'dragover'].forEach((ev) => {
      tableEl.addEventListener(ev, (e) => {
        e.preventDefault();
        tableEl.classList.add('dragover');
      });
    });
    tableEl.addEventListener('dragleave', (e) => {
      if (!tableEl.contains(e.relatedTarget)) tableEl.classList.remove('dragover');
    });
    tableEl.addEventListener('drop', async (e) => {
      e.preventDefault();
      tableEl.classList.remove('dragover');
      const items = Array.from((e.dataTransfer && e.dataTransfer.items) || []);
      const entries = items.map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null)).filter(Boolean);
      if (entries.length) {
        const groups = await Promise.all(entries.map((en) => readEntry(en, '')));
        await enqueueWithPaths(groups.flat());
      } else {
        const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
        if (files.length) enqueue(files.map((f) => ({ file: f, folderId })));
      }
    });

    await load();
  };

  // 项目间移动(源项目 ADMIN+ 可发起;目标项目需 EDITOR+):POST /api/files/{id}/move {projectId}
  async function openMoveModal(file, currentProjectId, onDone) {
    let projects = [];
    try { projects = await api('/api/projects') || []; }
    catch (e) { UI.err(e); return; }
    // 目标列表:排除当前项目;个人项目已由服务端置顶,标注「个人」
    const targets = projects.filter((p) => p.id !== currentProjectId);
    if (!targets.length) { UI.toast('没有可移动到的其他项目', 'warning'); return; }
    const m = UI.modal({
      title: '移动到其他项目',
      body:
        '<p class="modal-text muted mb-4">将「' + UI.esc(file.name) +
        '」移动到其他项目(仅修改归属,物理文件不复制不移动)。</p>' +
        '<div class="field flush"><label>目标项目</label>' +
        '<select class="select" id="mv-proj"></select></div>',
      // 注:接口支持可选 folderId(目标文件夹),此处最简实现固定移动到目标项目根目录
      actions: [
        { label: '取消', kind: 'text', value: null },
        {
          label: '移动', kind: 'filled',
          handler: async ({ close, body, btn }) => {
            btn.disabled = true;
            try {
              await api('/api/files/' + file.id + '/move', {
                method: 'POST',
                body: { projectId: body.querySelector('#mv-proj').value },
              });
              close(true);
              UI.toast('已移动', 'success');
              if (onDone) onDone();
            } catch (e) {
              btn.disabled = false;
              UI.err(e);
            }
          },
        },
      ],
    });
    const sel = m.body.querySelector('#mv-proj');
    sel.innerHTML = targets.map((p) =>
      '<option value="' + UI.esc(p.id) + '">' + UI.esc(p.name) + (p.isPersonal ? '(个人)' : '') + '</option>'
    ).join('');
  }
})();
