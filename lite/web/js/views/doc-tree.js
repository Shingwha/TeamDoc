// views/doc-tree.js — 文档树(渲染 + 交互),由文档模块作用域(views/docs.js)调用
//   DocTree.renderTreeInto(el, opts):把树渲染进 el 并接管 el 上的点击委托 ——
//     caret 点击原地切折叠(stopPropagation)、行点击导航、行内「新建子文档 / 更多」按钮
//   DocTree.setActive(el, id):原地移动高亮行(切文档只动 class,不重绘树)
//   骨架由 UI.tree 产出(.tree-node/.tree-row/.tree-caret/.tree-children),
//   文档树变体 = 容器保留 .doc-tree 类 + 行类 .tree-row.doc-row(样式见 app.css)。
//   本模块不引用调用方的任何运行时产物。
window.DocTree = (function () {
  'use strict';

  /**
   * @param {HTMLElement} el    树容器(#doc-tree,保留 .doc-tree 类)
   * @param {Object} opts
   *   treeData   树节点数组(id / title / children)
   *   activeId   当前文档 id(JSON 数字;高亮行,null 表示未选)
   *   collapsed  折叠 id 集合,键为 id 字符串(dataset 回读处用 UI.idOf 过形状)
   *   canEdit    成员且 EDITOR 及以上:显示行内「新建子文档 / 更多」
   *   callbacks  onNav(id) 行点击导航;onAdd(parentId) 新建子文档(空树的动作传 null);
   *              onMenu(btn, id) 「更多」下拉(id 已归一为数字)
   */
  function renderTreeInto(el, opts) {
    // 点击委托只绑一次:renderTreeInto 会被反复调用(加载/切折叠后重渲),
    // 每次都 addEventListener 会在同一个 el 上叠出多份监听
    if (!el._docTreeWired) {
      el._docTreeWired = true;
      el.addEventListener('click', (e) => {
        const o = el._docTreeOpts;
        if (!o) return;
        const cb = o.callbacks || {};
        const caret = e.target.closest('.tree-caret');
        if (caret && !caret.classList.contains('leaf')) {
          const row = caret.closest('.tree-row');
          // dataset 是字符串,而 collapsed 以节点 id(JSON 数字)为键 —— 不归一化就永远命中不了
          const id = UI.idOf(row.dataset.id);
          if (id != null) {
            const open = o.collapsed.has(id);
            if (open) o.collapsed.delete(id); else o.collapsed.add(id);
            setNodeOpen(row, open);   // 原地切:重绘整棵树会丢滚动位置
          }
          e.stopPropagation();
          return;
        }
        const add = e.target.closest('.act-add');
        if (add) { e.stopPropagation(); if (cb.onAdd) cb.onAdd(add.closest('.tree-row').dataset.id); return; }
        const more = e.target.closest('.act-more');
        if (more) {
          e.stopPropagation();
          if (cb.onMenu) cb.onMenu(more, UI.idOf(more.closest('.tree-row').dataset.id));
          return;
        }
        const row = e.target.closest('.tree-row');
        if (row && cb.onNav) cb.onNav(row.dataset.id);
      });
    }
    el._docTreeOpts = opts;

    el.innerHTML = '';
    if (!(opts.treeData || []).length) {
      el.appendChild(UI.emptyState({
        icon: 'article-line',
        title: '还没有文档',
        desc: opts.canEdit ? '创建第一篇文档开始写作' : '等待项目成员创建文档',
        action: opts.canEdit ? { label: '创建第一篇文档', onClick: () => opts.callbacks.onAdd(null) } : null,
      }));
      return;
    }
    el.innerHTML = UI.tree({
      nodes: opts.treeData,
      expanded: (n) => !opts.collapsed.has(n.id),
      rowCls: (n) => 'doc-row' + (n.id === opts.activeId ? ' active' : ''),
      // 行内含 .act-add/.act-more 按钮,行本体不能也是 button(button 嵌 button 会被解析器拆开)
      rowTag: 'div',
      rowInner: (n) =>
        UI.icon('file-text-line', 'doc-icon') +
        '<span class="doc-name" title="' + UI.esc(n.title) + '">' + UI.esc(n.title) + '</span>' +
        (opts.canEdit
          ? '<span class="doc-acts">' +
            UI.iconBtn({ icon: 'add-line', title: '新建子文档', size: 'sm', cls: 'act-add' }) +
            UI.iconBtn({ icon: 'more-fill', title: '更多', size: 'sm', cls: 'act-more' }) +
            '</span>'
          : ''),
    });
  }

  /**
   * 原地展开/收起一个节点:结构与 UI.tree 产出的一致(.tree-caret + .tree-children)。
   * 折叠只是隐藏子层,不该重建节点 —— 重建会丢滚动位置与 hover,树越大越明显。
   */
  function setNodeOpen(row, open) {
    const node = row.closest('.tree-node') || row.parentElement;
    const caret = node.querySelector('.tree-caret');
    const kids = node.querySelector('.tree-children');
    if (caret && !caret.classList.contains('leaf')) caret.classList.toggle('open', open);
    if (kids) kids.hidden = !open;
  }

  /** 原地移动高亮行:切换文档只动 class 就够了(整树重绘连滚动位置都会丢) */
  function setActive(el, id) {
    el.querySelectorAll('.doc-row').forEach((row) => {
      row.classList.toggle('active', UI.idOf(row.dataset.id) === id);
    });
  }

  return { renderTreeInto: renderTreeInto, setActive: setActive };
})();
