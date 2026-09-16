// views/recent.js — 「最近动态」的取数与行构造(发现广场与搜索页空态共用)。
//   此前两处各抄一份 ~50 行的 fetch + listRow 标记(连深链拼接都一字不差);
//   分组标题/副文案/空态是两页各自的呈现,留在视图里 —— 这里只收敛不变的部分。
window.Recent = (function () {
  'use strict';

  /** 拉取最近动态;limit 作用于文档/文件各自(服务端两类各取 limit 条) */
  async function fetch(limit) {
    const r = await api('/api/recent?limit=' + (limit || 12)) || {};
    return { docs: r.docs || [], files: r.files || [] };
  }

  /** 文档行(raised/hoverable 锚点行,sub 由调用方给 —— 两页的副文案口径不同) */
  function docRow(d, sub) {
    return UI.listRow({
      raised: true, hoverable: true, tag: 'a',
      href: App.route.project(d.projectId, 'docs', d.id),
      icon: 'file-text-line', iconCls: 'fi-doc',
      title: UI.esc(d.title),
      sub: sub != null ? sub : UI.esc((d.projectName || '') + ' 的文档 · ' + UI.fmtDate(d.updatedAt)),
    });
  }

  /** 文件行(icon 按 mime 取色;folderId 有值时深链直达所在目录) */
  function fileRow(f, sub) {
    const fi = UI.fileIcon(f.mime);
    return UI.listRow({
      raised: true, hoverable: true, tag: 'a',
      href: App.route.project(f.projectId, 'files', null,
        f.folderId != null ? { folder: f.folderId } : null),
      icon: fi.icon, iconCls: fi.cls,
      title: UI.esc(f.name),
      sub: sub != null ? sub : UI.esc((f.projectName || '') + ' 的文件 · ' + UI.fmtSize(f.size) +
        ' · ' + UI.fmtDate(f.createdAt)),
    });
  }

  return { fetch: fetch, docRow: docRow, fileRow: fileRow };
})();
