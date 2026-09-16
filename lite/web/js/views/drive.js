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

  // 预览能力由服务端判定(行内带 canInline / isText,见 files.py 的 _INLINE_MIME 与 ui.js 的判定家族):
  // 前端不本地按 mime 前缀猜 —— 白名单一收紧就会出现"预览按钮在、点了却下载"。
  // canInline/isText 由服务端 file_json 单出口恒带(列表/搜索/上传/meta 全有),直接信标志位。

  // 文件资源 URL 的唯一映射(doc 在 project.js 的回收站里另有一组;file/folder 都从这里取)。
  // 下载与上传 query 的构造也收在这里:服务端契约(raw body + query 元数据,见
  // files.py upload_file)改参数名时,只动这一处而不是每个拼接点。
  window.FilesAPI = {
    url(kind, id) { return kind === 'folder' ? '/api/files/folders/' + encodeURIComponent(id) : '/api/files/' + encodeURIComponent(id); },
    download(id, inline) {
      return '/api/files/' + encodeURIComponent(id) + '/download' + (inline ? '?inline=1' : '');
    },
    upload(projectId, name, folderId) {
      let qs = '?projectId=' + encodeURIComponent(projectId) + '&name=' + encodeURIComponent(name);
      if (folderId) qs += '&folderId=' + encodeURIComponent(folderId);
      return '/api/files/upload' + qs;
    },
  };

  window.Views.driveBody = async function (container, { projectId, proj, folderId: initialFolder, highlight }) {
    // 写/移动一律 UI.canEdit(EDITOR+):与文档 tab 的判定同口径 —— 此前这里手写
    // roleRank(myRole) >= 1 且额外卡 isMember,导致"非成员全局管理员在云空间没有
    // 写按钮、在文档 tab 却能建文档"的不一致。VIEWER 天然过不了 EDITOR。
    const canMove = UI.canEdit(proj);
    const canWrite = UI.canEdit(proj);
    // 路径栈 = "当前目录"的唯一来源:栈顶即当前目录(根目录的 id 为 null)。
    // 刷新/深链时由 resolveStack 从服务端文件夹树重建(以前父链只在会话内,刷新就回根目录)。
    // 不再另存一份 folderId:那是同一事实的第二份副本,下钻/回跳/深链三处各要手写同步。
    let stack = [{ id: null, name: '全部文件' }];
    const curFolderId = () => stack[stack.length - 1].id;
    // 排序与视图形态都记忆在 localStorage:云空间是高频重复访问的页面,
    // 每次进来都回到默认值会让人反复重设。
    // 网格(图片墙)态的默认排序是"最新在前" —— 图片墙的用途就是"看最近传了什么图",
    // 按名称排会把图片散在大目录各处。仅在用户没主动选过排序时套用这个默认。
    const savedSort = UI.pref.get('td:drive-sort', null);
    let viewMode = UI.pref.get('td:drive-view') === 'grid' ? 'grid' : 'list';
    let sortKey = savedSort || (viewMode === 'grid' ? 'time' : 'name'); // name | time | size
    let sortDir = UI.pref.get('td:drive-dir', null)
      ? (UI.pref.get('td:drive-dir') === 'desc' ? -1 : 1)
      : (viewMode === 'grid' ? -1 : 1);
    const PAGE = 100;      // 单页文件数,与「加载更多」配合
    let loadedFiles = 0;   // 已加载的文件条数(分页游标 = offset)
    let loadedRows = [];   // 已加载的文件行(追加渲染用;不靠读 DOM 反推,免得丢字段)
    let hasMore = false;
    // 行对象的唯一构造点:服务端的 folders/files 是两个数组,只有"拍平成统一列表"
    // 这一刻才知道类型 —— 所以类型由 rowOf 打进行对象自身,key 也从行派生(keyOf)。
    // 消费端(点击/多选/批量/移动)一律读 row.kind:此前类型被 isFolder 布尔参数、
    // data-kind、key 前缀各写一遍,而消费端读的那份没人写,于是文件夹被当成文件
    // 下载 / 改名 / 删除(id 撞车时命中的是另一个真实文件)。
    const rowOf = (kind, dto) => ({ ...dto, kind });
    const keyOf = (row) => row.kind + ':' + row.id;
    // "kind:id" → 行 的索引:点击/多选据此取回完整对象,dataset 只留 kind/id 做命中测试。
    // 此前把 7 个字段序列化进 dataset 再读回来重建对象,正是 id 归一化补丁的根源。
    const itemIndex = new Map();

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
          { html: UI.sortHead('名称', 'name', { key: sortKey, dir: sortDir }) },
          { html: UI.sortHead('大小', 'size', { key: sortKey, dir: sortDir }) },
          { html: UI.sortHead('时间', 'time', { key: sortKey, dir: sortDir }), cls: 'dh-time' },
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
    let lastFolders = [];       // 最近一次加载的文件夹行(网格视图重绘用)

    /** 面包屑是 stack(路径栈)的投影,**唯一绘制点在 load()** —— 改了 stack 的每一步
     *  (gotoFolder 下钻 / 面包屑回跳 / resolveStack 深链解析)都必经 load()。
     *  别在这里的调用方补第二次渲染:以前它只在视图初始化时画一次,点进文件夹后
     *  面包屑停在根路径(整条路径只剩一个不可点的 current),等于没有返回上一级的入口。 */
    function renderCrumb() {
      // UI.crumbs 只返回条目:容器就是这个 nav.crumb 本身(#drive-crumb),别再套一层
      crumbEl.innerHTML = UI.crumbs(stack.map((s) => ({ label: s.name })));
    }

    /** 把当前目录同步进 URL(?folder=),使刷新与"从别处跳进某目录"都能定位。
     *  用 replaceState 而非改 hash:改 hash 会触发 app.js 重新路由并重建整个视图,
     *  在一次普通的下钻操作里那是多余的整页重绘。 */
    function syncUrl() {
      const id = curFolderId();
      const base = App.route.project(projectId, 'files', null, id ? { folder: id } : null);
      if (location.hash !== base) history.replaceState(null, '', base);
    }

    /** 进入某目录(下钻/回跳共用),同时同步 URL */
    async function gotoFolder(id, name) {
      // 下钻 = 压栈;当前目录随之变成栈顶(没有第二份 folderId 要同步)
      if (stack[stack.length - 1].id !== id) stack.push({ id, name });
      syncUrl();
      await load();
    }

    function renderHeadSorts() {
      UI.sortHeadSet(tableEl, { key: sortKey, dir: sortDir });
    }

    // 行/瓦片共用的命中测试属性:只留 kind 与 id(行本体在 itemIndex 里)
    function attrsFor(row) {
      return 'data-kind="' + row.kind + '" data-id="' + UI.esc(row.id) + '"';
    }

    // 删除确认文案:文件夹(整棵可恢复)/被引用文件/普通文件三档
    function deleteTip(f) {
      return f.kind === 'folder'
        ? '删除会连同文件夹里的全部内容一起进回收站(整棵可恢复)。确定删除「' + f.name + '」?'
        : f.referenced
          ? '「' + f.name + '」正被文档引用,删除后引用将失效(恢复前)。仍要删除?'
          : '删除后进入项目回收站,可随时恢复。确定删除「' + f.name + '」?';
    }

    function rowHtml(row) {
      const isFolder = row.kind === 'folder'; // 局部派生:类型只来自行对象,不由调用方传参
      const fi = isFolder ? { icon: 'folder-fill', cls: 'folder' } : fileIcon(row.mime);
      const referenced = !isFolder && row.referenced;
      // 服务端已判定可否 inline(白名单,file_json 恒带 canInline)
      const canPreview = !isFolder && !!row.canInline;
      const key = keyOf(row);
      const check = '<input type="checkbox" class="sel-box"' + (selected.has(key) ? ' checked' : '') + '>';
      // 操作列:声明式列表生成 —— folder/file 的差异只在条目取舍与文案,
      // 不再两套三元表达式互抄(此前加一个新操作要改两处,必漏一处)
      const acts = [
        isFolder && { icon: 'download-2-line', title: '打包下载', cls: 'act-download-dir' },
        !isFolder && canPreview && { icon: 'eye-line', title: '预览', cls: 'act-preview' },
        !isFolder && { icon: 'download-2-line', title: '下载', cls: 'act-download' },
        canWrite && { icon: 'edit-line', title: '重命名', cls: 'act-rename' },
        !isFolder && canWrite && {
          icon: row.isPublic ? 'global-line' : 'lock-line',
          title: row.isPublic ? '已公开:复制链接给同事 / 取消公开'
            : '公开此文件(让不在项目中的人也能下载)',
          cls: 'act-publish',
        },
        canMove && { icon: 'share-forward-line', title: '移动', cls: 'act-move' },
        canWrite && { icon: 'delete-bin-line', title: isFolder ? '删除(含内容)' : '删除',
                      danger: true, cls: 'act-del' },
      ].filter(Boolean)
        .map((a) => UI.iconBtn({ icon: a.icon, title: a.title, danger: a.danger, size: 'sm', cls: a.cls }))
        .join('');
      return UI.tableRow(
        [
          {
            html: UI.cellName({
              icon: fi.icon, iconCls: fi.cls,
              link: '<button class="row-link" type="button" title="' + UI.esc(row.name) + '">' + UI.esc(row.name) + '</button>',
              badges: (referenced
                ? '<span class="badge" title="有文档引用了此文件,删除后引用将失效">被引用</span>' : '') +
                (!isFolder && row.isPublic
                  ? '<span class="badge primary" title="任何登录用户都能下载此文件">公开</span>' : ''),
            }),
          },
          { html: UI.cellMeta(isFolder ? '—' : UI.esc(UI.fmtSize(row.size)), 'row-size') },
          { html: UI.cellMeta(UI.esc(UI.fmtDate(row.createdAt)), 'row-time') },
        ],
        {
          check: check,
          acts: acts,
          selected: selected.has(key),
          attrs: attrsFor(row),
        }
      );
    }

    /** 网格/图片墙瓦片。图片用原图 + CSS 缩放 + lazy loading —— 不引 Pillow 生成缩略图:
     *  内网带宽够,零后端依赖更值。多选在网格下同样可用(左上角选择框)。 */
    function tileHtml(row) {
      const isFolder = row.kind === 'folder'; // 同上:类型来自行对象
      const fi = isFolder ? { icon: 'folder-fill', cls: 'folder' } : fileIcon(row.mime);
      const key = keyOf(row);
      const isImg = !isFolder && UI.isEmbedImage(row.mime, row.canInline);
      const thumb = isImg
        ? '<img src="' + FilesAPI.download(row.id, true) + '" alt="" loading="lazy">'
        : UI.icon(fi.icon, fi.cls);
      return '<div class="tile' + (isFolder ? ' is-folder' : '') + (selected.has(key) ? ' selected' : '') + '"' +
        ' ' + attrsFor(row) + ' title="' + UI.esc(row.name) + '">' +
        '<input type="checkbox" class="sel-box tile-check"' + (selected.has(key) ? ' checked' : '') + '>' +
        '<div class="tile-thumb">' + thumb +
        (!isFolder && row.isPublic ? '<span class="badge xs primary tile-pub" title="任何登录用户都能下载">公开</span>' : '') +
        '</div>' +
        '<div class="tile-name">' + UI.esc(row.name) + '</div>' +
        '<div class="tile-meta">' + (isFolder ? '文件夹' : UI.esc(UI.fmtSize(row.size))) + '</div>' +
        '</div>';
    }

    function renderGrid(folders, files, appendRows) {
      // 加载更多时只追加新到的一页:原来是每次全量重建 DOM,
      // 翻到第 N 页的总工作量是 O(N²),上千文件的目录会明显卡顿
      if (appendRows && rowsEl.className === 'file-grid') {
        rowsEl.insertAdjacentHTML('beforeend', appendRows.map((r) => tileHtml(r)).join(''));
        return;
      }
      rowsEl.className = 'file-grid';
      // 列头是给列表用的(名称/大小/时间),网格下没有对应语义,隐藏掉
      tableEl.classList.add('drive-grid-mode');
      rowsEl.innerHTML = folders.map((r) => tileHtml(r)).join('') +
        files.map((r) => tileHtml(r)).join('');
    }

    function renderList(folders, files, appendRows) {
      // 同上:追加新页而非重建全部行
      if (appendRows && !rowsEl.className) {
        rowsEl.insertAdjacentHTML('beforeend', appendRows.map((r) => rowHtml(r)).join(''));
        return;
      }
      rowsEl.className = '';
      tableEl.classList.remove('drive-grid-mode');
      // 统一列表:文件夹在前、文件在后(排序已由服务端完成,前端不再重排)
      rowsEl.innerHTML = folders.map((r) => rowHtml(r)).join('') +
        files.map((r) => rowHtml(r)).join('');
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
      // 导航状态 → DOM 的唯一投影点:面包屑只依赖 stack(调用方在进来之前已更新),
      // 与请求结果无关,所以放在取数之前 —— 请求失败也不会把导航停在过期路径上。
      renderCrumb();
      if (!append) { loadedFiles = 0; selected.clear(); }
      let qs = '?project_id=' + encodeURIComponent(projectId) +
        '&offset=' + loadedFiles + '&limit=' + PAGE +
        '&sort=' + encodeURIComponent(sortKey) +
        '&dir=' + (sortDir === 1 ? 'asc' : 'desc');
      const dir = curFolderId();
      if (dir) qs += '&folder_id=' + encodeURIComponent(dir);
      try {
        const data = await api('/api/files' + qs);
        // 两条数据路径(首屏 / 加载更多)都在这里成行:类型标签只有这一处出生点
        const pageRows = (data.files || []).map((f) => rowOf('file', f));
        // 文件夹只在首屏取一次(不参与分页);文件按页追加到已加载列表
        if (!append) {
          lastFolders = (data.folders || []).map((f) => rowOf('folder', f));
          loadedRows = [];
          itemIndex.clear();
        }
        loadedRows = loadedRows.concat(pageRows);
        loadedFiles = loadedRows.length;
        hasMore = !!data.hasMore;
        lastFolders.forEach((r) => itemIndex.set(keyOf(r), r));
        loadedRows.forEach((r) => itemIndex.set(keyOf(r), r));
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
        if (!lastFolders.length && !loadedRows.length) {
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
        if (viewMode === 'grid') renderGrid(lastFolders, loadedRows, append ? pageRows : null);
        else renderList(lastFolders, loadedRows, append ? pageRows : null);
        renderMore();
        updateBatchBar();
      } catch (e) {
        rowsEl.innerHTML = UI.errorBanner(e);
      }
    }

    /** 由文件夹树重建路径栈(刷新/深链用)。
     *  以前父链只存在会话内,刷新就回"全部文件" —— 现在从服务端的树里查出祖先链,
     *  并顺带把 URL 里的 ?folder= 深链(搜索结果跳进来时带)解析成栈。 */
    async function resolveStack(targetFolderId) {
      if (!targetFolderId) return;
      targetFolderId = Number(targetFolderId); // URL 深链进来是字符串
      try {
        const tree = await api('/api/projects/' + encodeURIComponent(projectId) + '/folders/tree') || [];
        let path = null;
        UI.walkTree(tree, (n, trail) => {
          if (n.id === targetFolderId) { path = trail.map((x) => ({ id: x.id, name: x.name })); return false; }
          return true;
        });
        // 目标文件夹可能已被删掉/不属于本项目:那就留在根目录,不要造出半截面包屑
        if (path) stack = [{ id: null, name: '全部文件' }].concat(path);
      } catch (e) { /* 拿不到树就保持根目录,列表本身仍可用 */ }
    }

    // 行/瓦片共用一个"元素选择器":列表用 .data-table-row,网格用 .tile。
    // 加新视图形态必须同步它,否则多选静默失效。
    const ITEM_SEL = '.data-table-row, .tile';

    /** 命中测试 → 完整对象(来自 itemIndex,不读 DOM 反推字段) */
    function itemOf(el) {
      const row = el.closest(ITEM_SEL);
      return row ? itemIndex.get(row.dataset.kind + ':' + row.dataset.id) || null : null;
    }

    function triggerDownload(f) { UI.download(FilesAPI.download(f.id, false), { filename: f.name || '' }); }

    // ---------- 多选与批量操作 ----------
    function selectedRows() {
      // 选中集以 "kind:id" 为键,对象本体在 itemIndex —— 与 DOM 无关
      return [...selected].map((k) => itemIndex.get(k)).filter(Boolean);
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
      UI.download('/api/files/zip?ids=' + files.map((f) => encodeURIComponent(f.id)).join(',') +
        '&folderIds=' + folders.map((f) => encodeURIComponent(f.id)).join(','),
        { filename: '文件打包.zip' });
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
      const r = await UI.eachOk(rows, (row) => api(FilesAPI.url(row.kind, row.id), { method: 'DELETE' }));
      selected.clear();
      UI.toast('已删除 ' + r.ok + ' 项' + (r.failed.length ? ',' + r.failed.length + ' 项失败' : ''),
        r.failed.length ? 'warning' : 'success');
      await load();
    };

    // ---------- 预览 ----------
    /** 判断预览方式:图片/PDF 开新标签(浏览器原生渲染器最好用);
     *  文本与 Markdown 走**站内模态框** —— 否则看一个 .md 要先下载到本地,
     *  而全站已有 Markdown 渲染栈,预览成本几乎为零。 */
    function previewKind(f) {
      if (!f.canInline) return null;
      return UI.canInlineMime(f.mime) ? 'native' : 'inline';
    }

    async function openPreview(f) {
      const kind = previewKind(f);
      if (kind === 'native') { window.open(FilesAPI.download(f.id, true), '_blank'); return; }
      const isMd = UI.isMarkdown(f.mime, f.name);
      // 文本/Markdown 的站内模态框已收拢到共享组件 preview.js(@文档/@文件 引用浮层同源复用)
      Preview.open({
        title: f.name,
        meta: Preview.meta([{ iconName: 'file-text-line',
                              text: (f.mime || '') + ' · ' + UI.fmtSize(f.size) }]),
        actions: [
          { id: 'pv-download', label: '下载', icon: 'download-2-line', onClick: () => triggerDownload(f) },
          // "存为文档":把上传的 .md/.txt 变成项目文档(否则只能下载→改→再传,还是新建不是新版本)
          { id: 'pv-to-doc', label: '存为文档', icon: 'file-add-line', onClick: ({ close }) => convertToDoc(f, { close }) },
        ],
        preview: {
          kind: isMd ? 'markdown' : 'text',
          url: FilesAPI.download(f.id, true),
          text: apiText(FilesAPI.download(f.id, true)),
        },
      });
    }

    /** 把文本类文件转为项目文档(内容直接落进新文档,原文件保留) */
    async function convertToDoc(f, modalRef) {
      const title = f.name.replace(/\.[^.]+$/, '') || '未命名文档';
      const ok = await UI.confirmDialog(
        '将「' + f.name + '」的内容创建为项目文档「' + title + '」?原文件保留不动。', { okText: '创建' });
      if (!ok) return;
      try {
        const text = await apiText(FilesAPI.download(f.id, true));
        const created = await api('/api/projects/' + encodeURIComponent(projectId) + '/docs',
          { method: 'POST', body: { title } });
        // 基线取建文档时返回的 version:新文档没有并发写入者,但保存契约要求带基线
        await api('/api/docs/' + created.id + '/content',
          { method: 'PUT', body: { content: text, baseVersion: created.version || 0 } });
        if (modalRef) modalRef.close(true);
        UI.toast('已创建文档,可在「文档」中编辑', 'success');
        location.hash = App.route.project(projectId, 'docs', created.id);
      } catch (e) { UI.err(e); }
    }

    /** 单文件公开的链接面板:公开成功后自动弹出,已公开文件的公开图标也指向这里。
     *  紧凑分享卡:文件名一行省略,链接框单行省略 + 整框即复制热区,底部只留取消公开。 */
    function showPublicDialog(f) {
      // 绝对链接:同事拿到的是完整 URL,从聊天工具里点开就能用
      const url = location.origin + FilesAPI.download(f.id);
      const fi = UI.fileIcon(f.mime);
      const unpublish = ({ close }) => {
        close(true);
        UI.confirmAction('取消后,项目外的同事将无法再通过链接下载「' + f.name + '」。确定取消公开?',
          { okText: '取消公开', okMsg: '已取消公开' },
          () => api('/api/files/' + f.id, { method: 'PATCH', body: { isPublic: false } }).then(load));
      };
      const m = UI.modal({
        title: '公开链接',
        body:
          '<div class="pub-file">' +
            UI.icon(fi.icon, 'row-icon ' + fi.cls) +
            '<span class="pub-file-name" title="' + UI.esc(f.name) + '">' + UI.esc(f.name) + '</span>' +
          '</div>' +
          UI.banner({
            kind: 'info', icon: 'information-line',
            text: '任何登录用户可下载;取消公开或删除文件后链接失效。',
          }) +
          '<button type="button" class="pub-link" id="pub-link" title="点击复制链接">' +
            '<code>' + UI.esc(url) + '</code>' + UI.icon('file-copy-line') +
          '</button>',
        actions: [
          { label: '取消公开', kind: 'danger-outline', onClick: unpublish },
        ],
      });
      m.body.querySelector('#pub-link').onclick = () => UI.copyText(url);
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
          await api(FilesAPI.url(f.kind, f.id), { method: 'PATCH', body: { name } });
          UI.toast('已重命名', 'success');
          await load();
        } catch (err) { UI.err(err); }
        return;
      }
      if (e.target.closest('.act-del')) {
        const ok = await UI.confirmAction(deleteTip(f), { okMsg: '已删除(可在回收站恢复)' }, async () => {
          await api(FilesAPI.url(f.kind, f.id), { method: 'DELETE' });
          selected.delete(f.kind + ':' + f.id);
          await load();
        });
        return;
      }
      if (e.target.closest('.act-preview')) { await openPreview(f); return; }
      if (e.target.closest('.act-download')) { triggerDownload(f); return; }
      // 文件夹的下载图标 = 打包下载整棵子树(保留目录结构)
      if (e.target.closest('.act-download-dir')) {
        UI.download('/api/files/zip?folderIds=' + encodeURIComponent(f.id),
          { filename: (f.name || '文件夹') + '.zip' });
        return;
      }
      if (e.target.closest('.act-publish')) {
        // 已公开:图标是链接面板入口(复制链接 / 取消公开都在面板里)
        if (f.isPublic) { showPublicDialog(f); return; }
        await UI.confirmAction(
          '公开后,本实例任何登录用户都能下载「' + f.name + '」。' +
          '适合"把这一份发给不在本项目里的同事",但请注意它不再受项目权限保护。确定公开?',
          { okText: '公开', okMsg: '已公开' },
          async () => {
            await api('/api/files/' + f.id, { method: 'PATCH', body: { isPublic: true } });
            await load();
            showPublicDialog(Object.assign({}, f, { isPublic: true }));
          });
        return;
      }
      if (e.target.closest('.act-move')) {
        openMoveModal(f, projectId, curFolderId(), load);
        return;
      }

      // 点击名称 / 网格瓦片:按类型穷举 —— 文件夹下钻、可预览的打开预览、其余下载。
      // 未知类型什么都不做:绝不能让"类型不认识"静默退化成一次下载(那会打到同 id 的文件)
      if (e.target.closest('.row-link') || e.target.closest('.tile')) {
        if (f.kind === 'folder') {
          await gotoFolder(f.id, f.name);
        } else if (f.kind !== 'file') {
          console.error('云空间:未知行类型,已忽略点击', f);
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
        UI.pref.set('td:drive-sort', sortKey);
        UI.pref.set('td:drive-dir', sortDir === 1 ? 'asc' : 'desc');
        await load();
      });
    });

    // 视图切换:列表 / 网格(记忆)
    if (viewSeg) UI.segWire(viewSeg, async (key) => {
      viewMode = key;
      UI.pref.set('td:drive-view', viewMode);
      // 切到网格而用户从未主动选过排序时,用"最新在前"(图片墙的用途);
      // 一旦用户点过列头排序(savedSort 有值),就尊重他的选择
      if (viewMode === 'grid' && !UI.pref.get('td:drive-sort', null)) {
        sortKey = 'time';
        sortDir = -1;
      }
      await load();
    });

    // 面包屑回跳:裁剪栈,当前目录随栈顶一起回退
    UI.crumbsWire(crumbEl, async (idx) => {
      stack = stack.slice(0, idx + 1);
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
        const parent = curFolderId();
        if (parent) body.parentId = parent;
        await api('/api/files/folders', { method: 'POST', body });
        UI.toast('已创建文件夹', 'success');
        await load();
      } catch (err) { UI.err(err); }
    };

    // ---------- 上传:队列(并发 3)+ 进度面板 + 文件夹结构保留 ----------
    const CONCURRENCY = 3;
    // 不做"离开视图即 abort":切标签/切页面时在传的大文件应当继续传完,
    // 中途取消等于让用户白传;真正要兜底的"永远挂着不动"由 apiUpload 的
    // 空闲超时覆盖(见 api.js 的说明)。
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
      return apiUpload(FilesAPI.upload(projectId, file.name, targetFolderId), file, { onProgress });
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
      let parentId = curFolderId(); // 起点 = 当前目录
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
          const dir = parts.length ? await ensureDirPath(parts.slice(0, -1)) : curFolderId();
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
      enqueue(files.map((f) => ({ file: f, folderId: curFolderId() })));
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
        if (files.length) enqueue(files.map((f) => ({ file: f, folderId: curFolderId() })));
      }
    });

    // 深链/刷新:把目标文件夹解析成完整路径栈(面包屑随 load() 一起重绘)
    await resolveStack(initialFolder);
    await load();
    loadStorage(); // 占用是辅助信息,不阻塞主列表渲染
    // 深链高亮:从文档里 @文件 引用跳转过来(?highlight=文件ID),滚动定位并闪烁提示
    if (highlight) {
      const row = rowsEl.querySelector('[data-id="' + CSS.escape(highlight) + '"]');
      if (row) {
        row.scrollIntoView({ block: 'center' });
        row.classList.add('row-flash');
        setTimeout(() => row.classList.remove('row-flash'), 2500);
      }
    }
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
          onClick: async ({ close, body, btn }) => {
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
          // 排除自己(return 会连同子树一起跳过,否则把文件夹移进自己的后代里会成环)。
          // n.id 是接口给的数字,item.id 取自 dataset 是字符串 —— 两侧都要归一
          if (isFolder && Number(n.id) === Number(item.id)) return;
          // option 里 HTML 实体不渲染,层级缩进用全角空格(视觉上稳定,不依赖字体等宽)
          opts.push('<option value="' + UI.esc(n.id) + '">' +
            '\u3000'.repeat(depth) + UI.esc(n.name) + '</option>');
          walk(n.children || [], depth + 1);
        });
      })(tree, 0);
      dirSel.innerHTML += opts.join('');
      // 同项目内移动文件时默认落在当前所在目录(projSel.value 是字符串,路由的 currentProjectId 是数字)
      if (!isFolder && Number(projId) === Number(currentProjectId) && curFolderId) dirSel.value = curFolderId;
    }
    projSel.addEventListener('change', loadDirs);
    await loadDirs();
  }
})();
