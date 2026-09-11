// views/drive.js — 云空间视图(飞书式统一列表:文件夹在前、文件在后,单个列表)
//   统一以项目上下文工作(scope 已随"个人空间并入个人项目"从契约中移除);
//   个人云空间 = 个人项目(isPersonal)的 files tab,与项目云空间复用同一视图。
//   上传:XHR 进度 + 并发 3 队列 + 右下角上传面板;支持文件夹(选择/拖拽,保留目录结构)。
//   多选:行首 checkbox,批量打包下载(zip)/ 删除;删除 = 进项目回收站。
//   视图:列表 / 网格(图片墙),分页「加载更多」,文本与 Markdown 站内预览。
//   Views.driveBody — 渲染体,由 project.js 的 files tab 调用
window.Views = window.Views || {};
(function () {
  'use strict';

  // 文件类型图标与配色已提到组件库(搜索页/项目页等都要用,见 ui.js 的 UI.fileIcon)
  const fileIcon = UI.fileIcon;

  // 预览能力由服务端判定(行内带 canInline / isText,见 files.py 的 _INLINE_MIME)。
  // 前端不再本地按 mime 前缀猜 —— 否则白名单一收紧就会出现"预览按钮在、点了却下载"。
  // 注:菜单/搜索结果等没有服务端标志位的场景,用 canInlineMime 做粗略兜底。
  const canInlineMime = (mime) => !!mime && (mime.startsWith('image/') || mime === 'application/pdf');

  function downloadUrl(id, inline) {
    return '/api/files/' + encodeURIComponent(id) + '/download' + (inline ? '?inline=1' : '');
  }

  window.Views.driveBody = async function (container, { projectId, myRole, isMember, folderId: initialFolder }) {
    // 移动需要"成员且 EDITOR+"(项目内)/ 源项目 ADMIN(跨项目,服务端再判)。
    // 必须看 isMember:公开项目的访客也拿到 VIEWER,更不该看到任何写操作
    const canMove = !!isMember && UI.roleRank(myRole) >= 1;
    const canWrite = !!isMember && UI.roleRank(myRole) >= 1;
    let folderId = initialFolder || null;
    // 面包屑路径栈。刷新/深链时由 resolveStack 从服务端文件夹树重建
    // (以前父链只在会话内,刷新就回根目录)
    let stack = [{ id: null, name: '全部文件' }];
    // 排序与视图形态都记忆在 localStorage:云空间是高频重复访问的页面,
    // 每次进来都回到默认值会让人反复重设。
    // 网格(图片墙)态的默认排序是"最新在前" —— 图片墙的用途就是"看最近传了什么图",
    // 按名称排会把图片散在大目录各处。仅在用户没主动选过排序时套用这个默认。
    const savedSort = localStorage.getItem('td:drive-sort');
    let viewMode = localStorage.getItem('td:drive-view') === 'grid' ? 'grid' : 'list';
    let sortKey = savedSort || (viewMode === 'grid' ? 'time' : 'name'); // name | time | size
    let sortDir = localStorage.getItem('td:drive-dir')
      ? (localStorage.getItem('td:drive-dir') === 'desc' ? -1 : 1)
      : (viewMode === 'grid' ? -1 : 1);
    const PAGE = 100;      // 单页文件数,与「加载更多」配合
    let loadedFiles = 0;   // 已加载的文件条数(分页游标 = offset)
    let loadedFilesList = []; // 已加载的文件对象(追加渲染用;不靠读 DOM 反推,免得丢字段)
    let hasMore = false;

    container.innerHTML =
      UI.toolbar({
        left: '<nav class="crumb" id="drive-crumb"></nav>',
        right:
          UI.seg({
            id: 'drive-view-seg', cls: 'drive-view-seg',
            active: viewMode,
            items: [
              { key: 'list', label: '列表', icon: 'list-check-2' },
              { key: 'grid', label: '网格', icon: 'grid-fill' },
            ],
          }) +
          (canWrite
            ? UI.btn({ id: 'btn-mkdir', label: '新建文件夹', icon: 'folder-add-line', kind: 'tonal', size: 'sm' }) +
              UI.btn({ id: 'btn-upload-dir', label: '上传文件夹', icon: 'folder-upload-line', kind: 'tonal', size: 'sm' }) +
              UI.btn({ id: 'btn-upload', label: '上传', icon: 'upload-2-line', kind: 'filled', size: 'sm' })
            : '') +
          '<input type="file" id="upload-input" multiple hidden>' +
          '<input type="file" id="upload-dir-input" webkitdirectory hidden>',
      }) +
      '<div id="drive-storage" class="card-note"></div>' +
      UI.tableHead(
        [
          { html: '<button class="dh-sort" data-sort="name" type="button">名称 <i class="ri-arrow-up-line"></i></button>' },
          { html: '<button class="dh-sort" data-sort="size" type="button">大小 <i class="ri-subtract-line"></i></button>' },
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
            (canWrite ? UI.btn({ id: 'batch-del', label: '删除', icon: 'delete-bin-line', kind: 'danger-outline', size: 'sm' }) : '') +
            UI.btn({ id: 'batch-cancel', label: '取消', kind: 'text', size: 'sm' }),
        }
      ) +
      '<div id="drive-rows">' + UI.loadingRow() + '</div>' +
      '<div id="drive-more" class="mt-3" hidden></div>' +
      '<div id="drive-trunc" hidden></div>' +
      '<div class="up-panel" id="up-panel" hidden>' +
      '<div class="up-head"><span id="up-title">上传</span>' +
      UI.iconBtn({ id: 'up-close', icon: 'close-line', title: '收起', size: 'sm' }) + '</div>' +
      '<div class="up-list" id="up-list"></div>' +
      '</div>';

    const crumbEl = container.querySelector('#drive-crumb');
    const tableEl = container.querySelector('#drive-table');
    const rowsEl = container.querySelector('#drive-rows');
    const moreEl = container.querySelector('#drive-more');
    const storageEl = container.querySelector('#drive-storage');
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
    const viewSeg = container.querySelector('#drive-view-seg');
    const selected = new Set(); // 多选状态:"{kind}:{id}"
    let activeUploads = 0;      // 进行中上传数(>0 时上传面板不可收起)
    let lastFolders = [];       // 最近一次加载的文件夹(网格视图重绘用)

    function renderCrumb() {
      crumbEl.innerHTML = stack.map((s, i) =>
        (i > 0 ? '<i class="ri-arrow-right-s-line crumb-sep"></i>' : '') +
        (i === stack.length - 1
          ? '<span class="crumb-item current">' + UI.esc(s.name) + '</span>'
          : '<button class="crumb-item" type="button" data-crumb="' + i + '">' + UI.esc(s.name) + '</button>')
      ).join('');
    }

    /** 把当前目录同步进 URL(?folder=),使刷新与"从别处跳进某目录"都能定位。
     *  用 replaceState 而非改 hash:改 hash 会触发 app.js 重新路由并重建整个视图,
     *  在一次普通的下钻操作里那是多余的整页重绘。 */
    function syncUrl() {
      const base = '#/p/' + projectId + '/files' + (folderId ? '?folder=' + encodeURIComponent(folderId) : '');
      if (location.hash !== base) history.replaceState(null, '', base);
    }

    /** 进入某目录(下钻/回跳共用),同时同步 URL */
    async function gotoFolder(id, name) {
      folderId = id;
      if (id && name != null) {
        // 下钻:压栈;回跳由调用方先裁剪 stack
        if (stack[stack.length - 1].id !== id) stack.push({ id, name });
      }
      syncUrl();
      await load();
    }

    function sortIconHtml(key) {
      if (sortKey !== key) return UI.icon('subtract-line');
      return UI.icon(sortDir === 1 ? 'arrow-up-line' : 'arrow-down-line');
    }

    function renderHeadSorts() {
      const label = { name: '名称', size: '大小', time: '时间' };
      tableEl.querySelectorAll('.dh-sort').forEach((b) => {
        b.innerHTML = (label[b.dataset.sort] || b.dataset.sort) + ' ' + sortIconHtml(b.dataset.sort);
      });
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
          ? UI.iconBtn({ icon: 'download-2-line', title: '打包下载', size: 'sm', cls: 'act-download-dir' }) +
            (canWrite ? UI.iconBtn({ icon: 'edit-line', title: '重命名', size: 'sm', cls: 'act-rename' }) : '') +
            (canMove ? UI.iconBtn({ icon: 'share-forward-line', title: '移动', size: 'sm', cls: 'act-move' }) : '') +
            (canWrite ? UI.iconBtn({ icon: 'delete-bin-line', title: '删除(含内容)', danger: true, size: 'sm', cls: 'act-del' }) : '')
          : (canPreview
              ? UI.iconBtn({ icon: 'eye-line', title: '预览', size: 'sm', cls: 'act-preview' })
              : '') +
            UI.iconBtn({ icon: 'download-2-line', title: '下载', size: 'sm', cls: 'act-download' }) +
            (canWrite ? UI.iconBtn({ icon: 'edit-line', title: '重命名', size: 'sm', cls: 'act-rename' }) : '') +
            (canWrite ? UI.iconBtn({
              icon: f.isPublic ? 'global-line' : 'lock-line',
              title: f.isPublic ? '取消公开(当前任何登录用户可下载)'
                : '公开此文件(让不在项目中的人也能下载)',
              cls: 'act-publish',
            }) : '') +
            (canMove ? UI.iconBtn({ icon: 'share-forward-line', title: '移动', size: 'sm', cls: 'act-move' }) : '') +
            (canWrite ? UI.iconBtn({ icon: 'delete-bin-line', title: '删除', danger: true, size: 'sm', cls: 'act-del' }) : ''));
      return UI.tableRow(
        [
          {
            html: UI.cellName({
              icon: fi.icon, iconCls: fi.cls,
              link: '<button class="row-link" type="button" title="' + UI.esc(f.name) + '">' + UI.esc(f.name) + '</button>',
              badges: (referenced
                ? '<span class="badge" title="有文档引用了此文件,删除后引用将失效">被引用</span>' : '') +
                (!isFolder && f.isPublic
                  ? '<span class="badge primary" title="任何登录用户都能下载此文件">公开</span>' : ''),
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
            ' data-public="' + (!isFolder && f.isPublic ? '1' : '') + '"' +
            ' data-referenced="' + (referenced ? '1' : '') + '"',
        }
      );
    }

    /** 网格/图片墙瓦片。图片用原图 + CSS 缩放 + lazy loading —— 不引 Pillow 生成缩略图:
     *  内网带宽够,零后端依赖更值。多选在网格下同样可用(左上角选择框)。 */
    function tileHtml(f, isFolder) {
      const fi = isFolder ? { icon: 'folder-fill', cls: 'folder' } : fileIcon(f.mime);
      const key = (isFolder ? 'folder:' : 'file:') + f.id;
      const isImg = !isFolder && f.canInline && String(f.mime || '').startsWith('image/');
      const thumb = isImg
        ? '<img src="' + downloadUrl(f.id, true) + '" alt="" loading="lazy">'
        : UI.icon(fi.icon, fi.cls);
      return '<div class="tile' + (isFolder ? ' is-folder' : '') + (selected.has(key) ? ' selected' : '') + '"' +
        ' data-kind="' + (isFolder ? 'folder' : 'file') + '"' +
        ' data-id="' + UI.esc(f.id) + '" data-name="' + UI.esc(f.name) + '"' +
        ' data-mime="' + UI.esc(f.mime || '') + '"' +
        ' data-caninline="' + (!isFolder && f.canInline ? '1' : '') + '"' +
        ' data-public="' + (!isFolder && f.isPublic ? '1' : '') + '"' +
        ' data-referenced="' + (!isFolder && f.referenced ? '1' : '') + '"' +
        ' title="' + UI.esc(f.name) + '">' +
        '<input type="checkbox" class="sel-box tile-check"' + (selected.has(key) ? ' checked' : '') + '>' +
        '<div class="tile-thumb">' + thumb +
        (!isFolder && f.isPublic ? '<span class="badge xs primary tile-pub" title="任何登录用户都能下载">公开</span>' : '') +
        '</div>' +
        '<div class="tile-name">' + UI.esc(f.name) + '</div>' +
        '<div class="tile-meta">' + (isFolder ? '文件夹' : UI.esc(UI.fmtSize(f.size))) + '</div>' +
        '</div>';
    }

    function renderGrid(folders, files, appendFiles) {
      // 加载更多时只追加新到的一页:原来是每次全量重建 DOM,
      // 翻到第 N 页的总工作量是 O(N²),上千文件的目录会明显卡顿
      if (appendFiles && rowsEl.className === 'file-grid') {
        rowsEl.insertAdjacentHTML('beforeend', appendFiles.map((f) => tileHtml(f, false)).join(''));
        return;
      }
      rowsEl.className = 'file-grid';
      // 列头是给列表用的(名称/大小/时间),网格下没有对应语义,隐藏掉
      tableEl.classList.add('drive-grid-mode');
      rowsEl.innerHTML = folders.map((f) => tileHtml(f, true)).join('') +
        files.map((f) => tileHtml(f, false)).join('');
    }

    function renderList(folders, files, appendFiles) {
      // 同上:追加新页而非重建全部行
      if (appendFiles && !rowsEl.className) {
        rowsEl.insertAdjacentHTML('beforeend', appendFiles.map((f) => rowHtml(f, false)).join(''));
        return;
      }
      rowsEl.className = '';
      tableEl.classList.remove('drive-grid-mode');
      // 统一列表:文件夹在前、文件在后(排序已由服务端完成,前端不再重排)
      rowsEl.innerHTML = folders.map((f) => rowHtml(f, true)).join('') +
        files.map((f) => rowHtml(f, false)).join('');
    }

    /** 「加载更多」按钮:文件分页游标推进(文件夹一次取全,不参与分页) */
    function renderMore() {
      if (!hasMore) {
        moreEl.hidden = true;
        moreEl.innerHTML = '';
        return;
      }
      moreEl.hidden = false;
      moreEl.innerHTML = UI.btn({ id: 'btn-more', label: '加载更多', kind: 'tonal', size: 'sm' });
      const b = moreEl.querySelector('#btn-more');
      b.disabled = false;
      b.onclick = async () => {
        b.disabled = true;
        await load({ append: true });
      };
    }

    /** 目录占用一行提示(活跃 + 回收站分列,见 files.py project_storage 的说明) */
    async function loadStorage() {
      try {
        const s = await api('/api/projects/' + encodeURIComponent(projectId) + '/storage');
        storageEl.innerHTML = UI.icon('hard-drive-3-line') + ' 本项目占用 ' +
          UI.esc(UI.fmtSize(s.active.bytes)) + '(' + s.active.fileCount + ' 个文件 · ' +
          s.active.folderCount + ' 个文件夹)' +
          (s.trash.fileCount
            ? ' · 回收站另有 ' + UI.esc(UI.fmtSize(s.trash.bytes)) + '(' + s.trash.fileCount + ' 个,仍占磁盘)'
            : '') +
          ' · 磁盘剩余 ' + UI.esc(UI.fmtSize(s.disk.free));
      } catch (e) {
        storageEl.textContent = ''; // 占用是辅助信息,拿不到就不显示,不打断主流程
      }
    }

    async function load(opts) {
      const append = opts && opts.append;
      if (!append) { loadedFiles = 0; selected.clear(); }
      let qs = '?project_id=' + encodeURIComponent(projectId) +
        '&offset=' + loadedFiles + '&limit=' + PAGE +
        '&sort=' + encodeURIComponent(sortKey) +
        '&dir=' + (sortDir === 1 ? 'asc' : 'desc');
      if (folderId) qs += '&folder_id=' + encodeURIComponent(folderId);
      try {
        const data = await api('/api/files' + qs);
        const files = data.files || [];
        // 文件夹只在首屏取一次(不参与分页);文件按页追加到已加载列表
        if (!append) { lastFolders = data.folders || []; loadedFilesList = []; }
        loadedFilesList = loadedFilesList.concat(files);
        loadedFiles = loadedFilesList.length;
        hasMore = !!data.hasMore;
        renderHeadSorts();
        // 文件夹数受服务端 2000 上限:真的超出才提示(文件走分页,不再是问题)
        const folderExtra = Math.max(0, (data.total.folders || 0) - lastFolders.length);
        if (folderExtra > 0) {
          truncEl.hidden = false;
          truncEl.className = 'banner warn sm mt-3';
          truncEl.innerHTML = UI.icon('alert-line') +
            '<span>当前目录有 ' + data.total.folders + ' 个文件夹,仅显示前 ' + lastFolders.length +
            ' 个。建议拆分到子文件夹。</span>';
        } else {
          truncEl.hidden = true;
          truncEl.innerHTML = '';
        }
        if (!lastFolders.length && !loadedFilesList.length) {
          rowsEl.className = '';
          rowsEl.innerHTML = '';
          rowsEl.appendChild(UI.emptyState({
            icon: 'cloud-line',
            title: '这里还是空的',
            desc: '上传文件或新建文件夹,也可以直接把文件拖进来',
            action: { label: '上传文件', onClick: () => uploadInput.click() },
          }));
          renderMore();
          return;
        }
        if (viewMode === 'grid') renderGrid(lastFolders, loadedFilesList, append ? files : null);
        else renderList(lastFolders, loadedFilesList, append ? files : null);
        renderMore();
        updateBatchBar();
      } catch (e) {
        rowsEl.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message });
      }
    }

    /** 由文件夹树重建路径栈(刷新/深链用)。
     *  以前父链只存在会话内,刷新就回"全部文件" —— 现在从服务端的树里查出祖先链,
     *  并顺带把 URL 里的 ?folder= 深链(搜索结果跳进来时带)解析成栈。 */
    async function resolveStack(targetFolderId) {
      if (!targetFolderId) return;
      try {
        const tree = await api('/api/projects/' + encodeURIComponent(projectId) + '/folders/tree') || [];
        const path = [];
        let found = false;
        (function walk(nodes, trail) {
          for (const n of nodes) {
            if (found) return;
            const next = trail.concat([{ id: n.id, name: n.name }]);
            if (n.id === targetFolderId) { path.push(...next); found = true; return; }
            walk(n.children || [], next);
          }
        })(tree, []);
        // 目标文件夹可能已被删掉/不属于本项目:那就留在根目录,不要造出半截面包屑
        if (found) stack = [{ id: null, name: '全部文件' }].concat(path);
      } catch (e) { /* 拿不到树就保持根目录,列表本身仍可用 */ }
    }

    // 行/瓦片共用一个"元素选择器":列表用 .data-table-row,网格用 .tile
    const ITEM_SEL = '.data-table-row, .tile';

    function itemOf(el) {
      const row = el.closest(ITEM_SEL);
      if (!row) return null;
      return { id: row.dataset.id, name: row.dataset.name, mime: row.dataset.mime, kind: row.dataset.kind,
               canInline: row.dataset.caninline === '1', isPublic: row.dataset.public === '1',
               referenced: row.dataset.referenced === '1' };
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
      return [...rowsEl.querySelectorAll(ITEM_SEL)]
        .filter((r) => selected.has(r.dataset.kind + ':' + r.dataset.id))
        .map((r) => ({ id: r.dataset.id, name: r.dataset.name, mime: r.dataset.mime, kind: r.dataset.kind,
                       canInline: r.dataset.caninline === '1',
                       isPublic: r.dataset.public === '1',
                       referenced: r.dataset.referenced === '1' }));
    }

    function updateBatchBar() {
      const rows = selectedRows();
      // 表头变身:有选择时同一行原地切换为批量操作(Gmail 式),列表不位移
      headEl.classList.toggle('selecting', rows.length > 0);
      const fileCount = rows.filter((r) => r.kind === 'file').length;
      const folderCount = rows.length - fileCount;
      batchInfo.textContent = '已选 ' + rows.length + ' 项' +
        (folderCount ? '(含 ' + folderCount + ' 个文件夹,将保留目录结构)' : '');
      // 文件夹也能打包(服务端递归展开),所以只要有选择按钮就可用
      container.querySelector('#batch-dl').disabled = rows.length === 0;
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
      rowsEl.querySelectorAll(ITEM_SEL).forEach((r) => setRowSelected(r, checkAll.checked));
      updateBatchBar();
    });

    container.querySelector('#batch-cancel').onclick = () => {
      selected.clear();
      rowsEl.querySelectorAll(ITEM_SEL).forEach((r) => setRowSelected(r, false));
      updateBatchBar();
    };

    container.querySelector('#batch-dl').onclick = () => {
      const rows = selectedRows();
      if (!rows.length) return;
      const files = rows.filter((r) => r.kind === 'file');
      const folders = rows.filter((r) => r.kind === 'folder');
      if (rows.length === 1 && files.length === 1) { triggerDownload(files[0]); return; }
      // 与单文件下载同一套锚点机制:window.open 对附件流可能被拦/开空白页。
      // 文件夹交给服务端递归展开并保留目录结构(见 files.py 的 _collect_zip_targets)
      const a = document.createElement('a');
      a.href = '/api/files/zip?ids=' + files.map((f) => encodeURIComponent(f.id)).join(',') +
        '&folderIds=' + folders.map((f) => encodeURIComponent(f.id)).join(',');
      a.download = '文件打包.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();
    };

    const batchDelBtn = container.querySelector('#batch-del');
    if (batchDelBtn) batchDelBtn.onclick = async () => {
      const rows = selectedRows();
      if (!rows.length) return;
      const folders = rows.filter((r) => r.kind === 'folder');
      const tip = '将删除 ' + rows.length + ' 项(可在项目「回收站」恢复)。' +
        (folders.length ? '文件夹会连同其中的内容一起删除(整棵可恢复)。' : '') +
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
      UI.toast('已删除 ' + okCount + ' 项' + (failCount ? ',' + failCount + ' 项失败' : ''),
        failCount ? 'warning' : 'success');
      await load();
    };

    // ---------- 预览 ----------
    /** 判断预览方式:图片/PDF 开新标签(浏览器原生渲染器最好用);
     *  文本与 Markdown 走**站内模态框** —— 否则看一个 .md 要先下载到本地,
     *  而全站已有 Markdown 渲染栈,预览成本几乎为零。 */
    function previewKind(f) {
      if (!f.canInline) return null;
      if (String(f.mime || '').startsWith('image/') || f.mime === 'application/pdf') return 'native';
      return 'inline';
    }

    async function openPreview(f) {
      const kind = previewKind(f);
      if (kind === 'native') { window.open(downloadUrl(f.id, true), '_blank'); return; }
      const isMd = /^text\/(markdown|x-markdown)$/.test(f.mime) || /\.(md|markdown)$/i.test(f.name);
      // 先开模态框显示加载态,再拉内容(大文件也不会让界面看起来没反应)
      const m = UI.modal({
        title: f.name,
        wide: true,
        body: '<div class="preview-meta">' + UI.icon('file-text-line') +
          '<span>' + UI.esc(f.mime || '') + ' · ' + UI.esc(UI.fmtSize(f.size)) + '</span>' +
          UI.btn({ id: 'pv-download', label: '下载', icon: 'download-2-line', kind: 'tonal', size: 'sm' }) +
          UI.btn({ id: 'pv-to-doc', label: '存为文档', icon: 'file-add-line', kind: 'tonal', size: 'sm' }) +
          '</div>' +
          '<div class="preview-body" id="pv-body">' + UI.loadingRow() + '</div>',
      });
      m.body.querySelector('#pv-download').onclick = () => triggerDownload(f);
      // "存为文档":把上传的 .md/.txt 变成项目文档(否则只能下载→改→再传,还是新建不是新版本)
      m.body.querySelector('#pv-to-doc').onclick = () => convertToDoc(f, m);
      const box = m.body.querySelector('#pv-body');
      try {
        const r = await fetch(downloadUrl(f.id, true), { credentials: 'same-origin' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const text = await r.text();
        if (isMd && window.MdRender) {
          box.innerHTML = '<div class="markdown-body"></div>';
          await window.MdRender.mount(box.firstElementChild, text);
        } else {
          box.innerHTML = '';
          const pre = document.createElement('pre');
          pre.textContent = text; // textContent 赋值即转义,不解析 HTML
          box.appendChild(pre);
          if (window.Prism && Prism.highlightElement) {
            // 给 pre 里的代码块上色(Prism 会自行识别语言或按 plain 处理)
            const code = document.createElement('code');
            code.className = 'language-plaintext';
            code.textContent = text;
            pre.textContent = '';
            pre.appendChild(code);
            try { Prism.highlightElement(code); } catch (e) { /* 高亮失败不影响内容 */ }
          }
        }
      } catch (e) {
        box.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: '无法预览:' + e.message });
      }
    }

    /** 把文本类文件转为项目文档(内容直接落进新文档,原文件保留) */
    async function convertToDoc(f, modalRef) {
      const title = f.name.replace(/\.[^.]+$/, '') || '未命名文档';
      const ok = await UI.confirmDialog(
        '将「' + f.name + '」的内容创建为项目文档「' + title + '」?原文件保留不动。', { okText: '创建' });
      if (!ok) return;
      try {
        const r = await fetch(downloadUrl(f.id, true), { credentials: 'same-origin' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const text = await r.text();
        const created = await api('/api/projects/' + encodeURIComponent(projectId) + '/docs',
          { method: 'POST', body: { title } });
        await api('/api/docs/' + created.id + '/content', { method: 'PUT', body: { content: text } });
        if (modalRef) modalRef.close(true);
        UI.toast('已创建文档,可在「文档」中编辑', 'success');
        location.hash = '#/p/' + projectId + '/docs/' + created.id;
      } catch (e) { UI.err(e); }
    }

    // 列表交互(事件委托)
    rowsEl.addEventListener('click', async (e) => {
      const f = itemOf(e.target);
      if (!f) return;

      if (e.target.classList.contains('sel-box')) {
        e.stopPropagation(); // 网格瓦片整块可点:勾选不应触发"打开"
        setRowSelected(e.target.closest(ITEM_SEL), e.target.checked);
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
          ? '删除会连同文件夹里的全部内容一起进回收站(整棵可恢复)。确定删除「' + f.name + '」?'
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
      if (e.target.closest('.act-preview')) { await openPreview(f); return; }
      if (e.target.closest('.act-download')) { triggerDownload(f); return; }
      // 文件夹的下载图标 = 打包下载整棵子树(保留目录结构)
      if (e.target.closest('.act-download-dir')) {
        const a = document.createElement('a');
        a.href = '/api/files/zip?folderIds=' + encodeURIComponent(f.id);
        a.download = (f.name || '文件夹') + '.zip';
        document.body.appendChild(a);
        a.click();
        a.remove();
        return;
      }
      if (e.target.closest('.act-publish')) {
        const want = !f.isPublic;
        if (want) {
          const ok = await UI.confirmDialog(
            '公开后,本实例任何登录用户都能下载「' + f.name + '」。' +
            '适合"把这一份发给不在本项目里的同事",但请注意它不再受项目权限保护。确定公开?',
            { okText: '公开' });
          if (!ok) return;
        }
        try {
          await api('/api/files/' + f.id, { method: 'PATCH', body: { isPublic: want } });
          UI.toast(want ? '已公开' : '已取消公开', 'success');
          await load();
        } catch (err) { UI.err(err); }
        return;
      }
      if (e.target.closest('.act-move')) {
        openMoveModal(f, projectId, folderId, load);
        return;
      }

      // 点击名称 / 网格瓦片:文件夹进入;可预览的打开站内预览,其余下载
      if (e.target.closest('.row-link') || e.target.closest('.tile')) {
        if (f.kind === 'folder') {
          await gotoFolder(f.id, f.name);
        } else if (previewKind(f)) {
          await openPreview(f);
        } else {
          triggerDownload(f);
        }
      }
    });

    // 列头排序(升序再点切降序)。排序走服务端并记忆 —— 分页与排序必须在一起,
    // 客户端只排当前页会得到"每页内部有序"这种看似正确实则错误的结果
    tableEl.querySelectorAll('.dh-sort').forEach((b) => {
      b.addEventListener('click', async () => {
        if (sortKey === b.dataset.sort) sortDir = -sortDir;
        else { sortKey = b.dataset.sort; sortDir = 1; }
        try {
          localStorage.setItem('td:drive-sort', sortKey);
          localStorage.setItem('td:drive-dir', sortDir === 1 ? 'asc' : 'desc');
        } catch (e) { /* 隐私模式下不可写,忽略 */ }
        await load();
      });
    });

    // 视图切换:列表 / 网格(记忆)
    if (viewSeg) {
      viewSeg.addEventListener('click', async (e) => {
        const b = e.target.closest('button[data-key]');
        if (!b) return;
        viewMode = b.dataset.key;
        try { localStorage.setItem('td:drive-view', viewMode); } catch (err) { /* 忽略 */ }
        viewSeg.querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
        // 切到网格而用户从未主动选过排序时,用"最新在前"(图片墙的用途);
        // 一旦用户点过列头排序(savedSort 有值),就尊重他的选择
        if (viewMode === 'grid' && !localStorage.getItem('td:drive-sort')) {
          sortKey = 'time';
          sortDir = -1;
        }
        await load();
      });
    }

    // 面包屑回跳
    crumbEl.addEventListener('click', async (e) => {
      const a = e.target.closest('[data-crumb]');
      if (!a) return;
      const idx = Number(a.dataset.crumb);
      stack = stack.slice(0, idx + 1);
      folderId = stack[stack.length - 1].id;
      syncUrl();
      await load();
    });

    // 新建文件夹:{name, projectId, parentId?}
    const mkdirBtn = container.querySelector('#btn-mkdir');
    if (mkdirBtn) mkdirBtn.onclick = async () => {
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

    // XHR 上传(fetch 拿不到上传进度);凭据走同源 Cookie,与 api() 一致。
    // 请求体 = 文件本身(raw body),元数据走 query string —— 见 files.py upload_file
    // 的注释:multipart 会让 >1MB 的文件在服务端落两次盘(先 spool 再拷),
    // 传大文件要两倍空间与 IO。
    function xhrUpload(file, targetFolderId, onProgress) {
      return new Promise((resolve, reject) => {
        const x = new XMLHttpRequest();
        let qs = '?projectId=' + encodeURIComponent(projectId) +
          '&name=' + encodeURIComponent(file.name);
        if (targetFolderId) qs += '&folderId=' + encodeURIComponent(targetFolderId);
        x.open('POST', '/api/files/upload' + qs);
        // 不设 Content-Type:让浏览器按 File 自动带上并计算 Content-Length
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
        x.send(file);
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

    if (uploadBtn) uploadBtn.onclick = () => uploadInput.click();
    const upDirBtn = container.querySelector('#btn-upload-dir');
    if (upDirBtn) upDirBtn.onclick = () => uploadDirInput.click();
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

    // 深链/刷新:把目标文件夹解析成完整路径栈,面包屑才显示得出来
    await resolveStack(folderId);
    renderCrumb();
    await load();
    loadStorage(); // 占用是辅助信息,不阻塞主列表渲染
  };

  // 移动文件 / 文件夹:项目内整理(EDITOR 即可)或跨项目转移(源项目需 ADMIN)
  //   POST /api/files/{id}/move  {projectId, folderId?}
  //   POST /api/files/folders/{id}/move  {projectId, parentId?}
  //   curFolderId:当前所在目录 —— 移动文件到"本项目"时,默认选中它,便于原地整理
  async function openMoveModal(item, currentProjectId, curFolderId, onDone) {
    let projects = [];
    try { projects = await api('/api/projects') || []; }
    catch (e) { UI.err(e); return; }
    const isFolder = item.kind === 'folder';
    const m = UI.modal({
      title: '移动到',
      body:
        '<p class="modal-text muted mb-4">将「' + UI.esc(item.name) +
        '」移动位置(仅改归属,物理文件不复制不移动)。</p>' +
        '<div class="field flush"><label>目标项目</label>' +
        '<select class="select" id="mv-proj"></select></div>' +
        '<div class="field flush mt-3"><label>目标文件夹</label>' +
        '<select class="select" id="mv-dir"></select></div>',
      actions: [
        { label: '取消', kind: 'text', value: null },
        {
          label: '移动', kind: 'filled',
          handler: async ({ close, body, btn }) => {
            const projId = body.querySelector('#mv-proj').value;
            const dirId = body.querySelector('#mv-dir').value;
            btn.disabled = true;
            try {
              if (isFolder) {
                await api('/api/files/folders/' + item.id + '/move', {
                  method: 'POST',
                  body: { projectId: projId, parentId: dirId || null },
                });
              } else {
                await api('/api/files/' + item.id + '/move', {
                  method: 'POST',
                  body: { projectId: projId, folderId: dirId || null },
                });
              }
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
    const projSel = m.body.querySelector('#mv-proj');
    const dirSel = m.body.querySelector('#mv-dir');
    // 目标项目:全部项目(含当前项目 —— 项目内整理是高频需求);个人项目由服务端置顶
    projSel.innerHTML = projects.map((p) =>
      '<option value="' + UI.esc(p.id) + '"' + (p.id === currentProjectId ? ' selected' : '') + '>' +
      UI.esc(p.name) + (p.isPersonal ? '(个人)' : '') + '</option>'
    ).join('');

    // 目标文件夹:按所选项目拉文件夹树铺平成缩进选项
    // (excludeId:移动文件夹时排除自己与自己的后代,否则会形成环)
    async function loadDirs() {
      const projId = projSel.value;
      dirSel.innerHTML = '<option value="">(项目根目录)</option>';
      let tree = [];
      try { tree = await api('/api/projects/' + encodeURIComponent(projId) + '/folders/tree') || []; }
      catch (e) { return; } // 无权限的项目(如他人个人空间)拿不到树,只留根目录
      const opts = [];
      (function walk(nodes, depth) {
        nodes.forEach((n) => {
          if (isFolder && n.id === item.id) return; // 排除自己(连同其子树一起跳过)
          // option 里 HTML 实体不渲染,层级缩进用全角空格(视觉上稳定,不依赖字体等宽)
          opts.push('<option value="' + UI.esc(n.id) + '">' +
            '\u3000'.repeat(depth) + UI.esc(n.name) + '</option>');
          walk(n.children || [], depth + 1);
        });
      })(tree, 0);
      dirSel.innerHTML += opts.join('');
      // 同项目内移动文件时默认落在当前所在目录
      if (!isFolder && projId === currentProjectId && curFolderId) dirSel.value = curFolderId;
    }
    projSel.addEventListener('change', loadDirs);
    await loadDirs();
  }
})();
