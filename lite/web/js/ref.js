// ref.js — 引用契约的**唯一真相源**(前端半边;服务端半边是 lite/server/refs.py)。
//
// 正文里指向站内资源的引用有两种形态,都是普通 Markdown 链接(可读性优先,源码即存储):
//
//   chip    [@标题](teamdoc://doc/{docId})      文档引用(编辑器 @ 菜单给出的形态)
//           [@名称](teamdoc://file/{fileId})     文件引用
//   附件/内嵌 [名称](/api/files/{fileId}/download)
//           ![名称](/api/files/{fileId}/download?inline=1)
//
// 两条不变量,违反任何一条都会让"被引用"标记、反链、移动前检查同时失效:
//   1. **id 是 ULID 字符串**,形状判定只走 UI.idOf(与服务端 ids.py 同形);
//   2. **引用里只出现资源自己的 id** —— 不带 projectId:项目归属会变(文档可跨项目
//      移动),引用必须跟着资源走。
//
// 格式与解析都在这个文件:别在视图里另写正则拼引用。服务端解析在 refs.py,
// 两边的语法逐字对齐(有测试分别盯着两侧)。
// DL_RE 认的是下面 downloadUrl 产出的形状(路径本身归 Endpoints.fileDownload):
// 改下载地址的形状要同时改这三处 —— 本正则、Endpoints、refs.py。
window.Ref = (function () {
  'use strict';

  var DOC_PREFIX = 'teamdoc://doc/';
  var FILE_PREFIX = 'teamdoc://file/';
  var DL_RE = /^\/api\/files\/([^/?#]+)\/download(\?[^#]*)?$/;

  // ---------- 生成 ----------

  /** 文档引用 chip:[@标题](teamdoc://doc/{id}) */
  function docChip(title, docId) {
    return '[@' + text(title, '无标题文档') + '](' + DOC_PREFIX + mustId(docId) + ')';
  }

  /** 文件引用 chip:[@名称](teamdoc://file/{id}) */
  function fileChip(name, fileId) {
    return '[@' + text(name, '未命名文件') + '](' + FILE_PREFIX + mustId(fileId) + ')';
  }

  /** 下载地址(inline=1 为站内内嵌预览地址) */
  function downloadUrl(fileId, inline) {
    return Endpoints.fileDownload(mustId(fileId), inline);
  }

  /** 内嵌图片:只能用于"能嵌图"的类型(见 UI.isEmbedImage),否则插出来是裂图 */
  function image(name, fileId) {
    return '![' + text(name, '未命名文件') + '](' + downloadUrl(fileId, true) + ')';
  }

  /** 附件链接:[名称](/api/files/{id}/download) */
  function attachment(name, fileId) {
    return '[' + text(name, '未命名文件') + '](' + downloadUrl(fileId, false) + ')';
  }

  /** 文件引用条目:能嵌图的走原生图片语法(预览直接显示),其余插 chip。
   *  判定用 UI.isEmbedImage(图片类型 + 服务端白名单):md/txt/pdf 等 canInline
   *  同样为 true,但并不"能嵌图" —— 插 ![](...) 只会得到裂图。 */
  function fileMarkdown(f) {
    return UI.isEmbedImage(f.mime, f.canInline)
      ? image(f.name, f.id) : fileChip(f.name, f.id);
  }

  // ---------- 解析 ----------

  /** 解析 chip 引用:返回 {kind:'doc'|'file', id} 或 null(非引用/形状不对一律 null)。
   *  严格 id-only:引用里只有资源 id,没有项目 id,也没有别的形态。 */
  function parse(href) {
    var s = String(href || '');
    var kind = null;
    if (s.indexOf(DOC_PREFIX) === 0) kind = 'doc';
    else if (s.indexOf(FILE_PREFIX) === 0) kind = 'file';
    if (!kind) return null;
    var id = UI.idOf(s.slice(kind === 'doc' ? DOC_PREFIX.length : FILE_PREFIX.length));
    return id ? { kind: kind, id: id } : null;
  }

  // ---------- 点击路由 ----------
  // 引用是"看一眼"的动作:点 chip 出预览浮层(preview.js,与云空间预览同源复用),
  // 跳转/下载是浮层里的显式次要动作(飞书/Notion 的 peek 模式)—— 不打断当前上下文。
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href^="teamdoc://"]');
    if (!a) return;
    e.preventDefault();
    var href = a.getAttribute('href') || '';
    var r = parse(href);
    if (!r) {
      // 形状不对(旧书签、手改半截、scheme 拼错):明确报错,不要静默吞掉让用户以为"点了没反应"
      UI.toast('引用格式不正确:' + href + '(应为 teamdoc://doc/{id} 或 teamdoc://file/{id})', 'warning');
      return;
    }
    if (r.kind === 'doc') openDoc(r.id); else openFile(r.id);
  }, true);

  function openDoc(docId) {
    // 项目归属**只信服务端**:引用里没有 projectId(移动后正文不改写),
    // 路由必须按 GET /api/docs/{id} 返回的 projectId 拼,否则跨项目引用会跳错项目
    api(Endpoints.doc(docId)).then(function (d) {
      var full = d.content || '';
      var truncated = full.length > 6000;
      Preview.open({
        title: d.title || '未命名文档',
        meta: Preview.meta([
          { iconName: 'folder-2-line',
            text: '位置:' + [d.location.projectName || d.projectId].concat(d.location.path || []).join(' / ') },
        ]) + Preview.meta([
          { iconName: 'time-line', text: '更新 ' + UI.fmtDate(d.updatedAt) },
          { text: '保存 ' + (d.version != null ? d.version : '-') + ' 次' },
          { text: (d.contentChars != null ? d.contentChars : 0) + ' 字' },
        ]),
        actions: [
          { id: 'doc-ref-open', label: '打开全文', icon: 'external-link-line', kind: 'filled',
            onClick: function () {
              window.open(location.origin + '/' + App.route.project(d.projectId, 'docs', d.id), '_blank');
            } },
        ],
        preview: { kind: 'markdown', text: Promise.resolve(truncated ? full.slice(0, 6000) : full) },
        note: truncated ? '预览仅显示前 6000 字,全文请「打开全文」。' : null,
      });
    }).catch(function (e) {
      UI.err(e); // 引用的文档已删除/无权限:给可理解的错误
    });
  }

  function openFile(fileId) {
    api(Endpoints.fileMeta(fileId)).then(function (meta) {
      var dl = downloadUrl(meta.id, false);
      var inline = downloadUrl(meta.id, true);
      var fi = UI.fileIcon(meta.mime);
      var isImage = UI.isEmbedImage(meta.mime, meta.canInline);
      var isMd = meta.isText && UI.isMarkdown(meta.mime, meta.name);
      Preview.open({
        title: meta.name,
        meta: Preview.meta([
          { iconName: fi.icon, iconCls: fi.cls,
            text: (meta.mime || '未知类型') + ' · ' + UI.fmtSize(meta.size) },
        ]) + Preview.meta([
          { iconName: 'folder-2-line',
            text: '位置:' + [meta.location.projectName || meta.projectId]
                    .concat(meta.location.path || []).join(' / ') },
        ]) + (meta.shared ? Preview.meta([{ text: '已建立分享链接,持有链接的人无需登录即可下载' }]) : ''),
        actions: [
          { id: 'pv-locate', label: '在云空间中查看', icon: 'folder-open-line',
            onClick: function () {
              // 新标签页打开,与 @文档 引用同策略:不打断当前阅读上下文
              var loc = meta.location;
              window.open(location.origin + '/' + App.route.project(loc.projectId, 'files', null,
                loc.path.length ? { folder: meta.folderId, highlight: meta.id } : { highlight: meta.id }),
                '_blank');
            } },
          { id: 'pv-download', label: '下载', icon: 'download-2-line',
            onClick: function () { UI.download(dl, { filename: meta.name }); } },
        ],
        preview: {
          kind: Preview.kindOf(meta),
          url: inline,
          text: meta.isText ? apiText(inline) : null,
          note: '该类型不支持站内预览,可下载后查看。',
        },
      });
    }).catch(function (e) {
      // 引用一个已删除(404)/已无权限(403)的文件:给可理解的错误,不静默
      UI.err(e);
    });
  }

  // ---------- 文本清洗 ----------

  /** Markdown 链接文本清洗:方括号/圆括号/空白会破坏链接语法,一律换成空格 */
  function text(s, fallback) {
    return String(s || '').replace(/[[\]()\r\n]/g, ' ').trim() || fallback;
  }

  /** 外部链接地址清洗(引用生成不走这里 —— id 由 mustId 兜底) */
  function url(s) { return String(s || '').replace(/[\s()]/g, ''); }

  /** 生成引用时 id 形状不对 = 调用方传了脏数据,当场抛错比写出坏引用好 */
  function mustId(v) {
    var id = UI.idOf(v);
    if (!id) throw new Error('不是合法的 id: ' + v);
    return id;
  }

  return {
    doc: DOC_PREFIX, file: FILE_PREFIX,
    docChip: docChip, fileChip: fileChip, fileMarkdown: fileMarkdown,
    downloadUrl: downloadUrl, image: image, attachment: attachment,
    parse: parse,
    openDoc: openDoc, openFile: openFile,
    text: text, url: url, mustId: mustId,
  };
})();
