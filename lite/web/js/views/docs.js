// views/docs.js — 文档模块作用域(#/p/{id}/docs[/{docId}]):树列 + 文档位
//   两层作用域各管一件事:
//     p/{pid}/docs        文档模块层 —— 切文档时它**原样留着**:树不重取、不重绘,
//                         折叠态与滚动位置都在,树面板开关也归它
//     p/{pid}/docs/{id|-} 文档位层 —— 唯一随文档切换重建的那一层:编辑器(views/doc-editor.js)
//                         或"未选文档"空态。槽位永远有层,所以不需要"子层走了要恢复什么"
//
//   作用域契约见 ARCHITECTURE.md「渲染契约」。
window.Views = window.Views || {};
(function () {
  'use strict';

  // 折叠态按项目记(与侧栏 treeExpanded 同一口径):离开文档模块再回来不收回。
  // 它是用户的选择,不是可以被重新渲染出来的派生值
  const collapsedByProject = new Map();

  /**
   * 文档模块的骨架:冷进项目、项目对象还没到手时铺它 —— 形状与真布局逐块对应
   * (左树列、右编辑区),数据到达时就地替换,眼睛不用重新找位置。
   * 文档页的列结构知识只在本模块里,别处不另抄一份(抄的那份一定会漂)。
   * 树列的开关态取 UI.panelOpenByDefault(与状态机同一判定):少了它,树列按默认的
   * "关"渲染成 0 宽,骨架就会先满幅、等真面板挂上再跳一下
   */
  window.Views.docsSkeleton = function () {
    const open = UI.panelOpenByDefault('td:tree-open');
    return '<div class="docs-wrap' + (open ? ' tree-open' : '') + '">' +
      '<div class="doc-tree-col"><div class="doc-tree-inner">' + UI.skeleton('rows', 6) + '</div></div>' +
      '<div class="doc-editor-col"><div class="editor-canvas"><div class="doc-content">' +
      UI.skeleton('lines', 8) +
      '</div></div></div>' +
      '</div>';
  };

  window.Views.docsLevel = function (r) {
    const pid = r.pid;
    const proj = r.proj;
    const canEdit = UI.canEdit(proj);   // EDITOR 及以上可写;VIEWER 只读,未加入者在项目层就被拦下
    const collapsed = collapsedByProject.get(pid) || new Set();
    collapsedByProject.set(pid, collapsed);

    let treeData = [];
    let wrap = null;
    let treeEl = null;
    let panel = null;   // 树面板双态(宽屏内联 / 窄屏抽屉)

    /**
     * 当前打开的文档 id:**现读当前路由**,不闭包快照 —— 本层在切文档时不重建,
     * 闭包里的 id 会停在第一次的值(见「渲染契约」第 2 条)。
     */
    function docId() {
      const cur = Route.current();
      return cur && cur.pid === pid ? cur.docId : null;
    }

    /** 按文档 id 找节点(「更多」菜单要拿标题回显) */
    function findNode(id) {
      let hit = null;
      UI.walkTree(treeData, (n) => {
        if (n.id === id) { hit = n; return false; }
      });
      return hit;
    }

    // 渲染与交互(caret 折叠 / 行导航 / 行内按钮)都在 doc-tree.js 的 DocTree 里
    function renderTree() {
      DocTree.renderTreeInto(treeEl, {
        treeData, activeId: docId(), collapsed, canEdit,
        callbacks: {
          onNav: (id) => {
            // 点的就是当前文档:hash 不会变、页面不会重渲;抽屉形态下要自己收(宽屏内联不受影响)
            if (id === docId()) {
              if (panel.isNarrow()) panel.close();
              return;
            }
            location.hash = App.route.project(pid, 'docs', id);
          },
          onAdd: (parentId) => createDoc(parentId),
          onMenu: (btn, id) => openDocMenu(btn, id),
        },
      });
    }

    async function loadTree() {
      try {
        treeData = await api(Endpoints.docTree(pid)) || [];
        renderTree();
      } catch (e) {
        treeEl.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message, sm: true, cls: 'm-2' });
      }
    }

    async function createDoc(parentId) {
      try {
        const body = { title: '无标题文档' };
        if (parentId) body.parentId = parentId;
        const d = await api(Endpoints.docsOf(pid), { method: 'POST', body: body });
        await loadTree();
        location.hash = App.route.project(pid, 'docs', d.id);
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
              await api(Endpoints.doc(id), { method: 'PATCH', body: { title } });
              await loadTree();
              // 当前打开的那篇:编辑头的标题跟着改(它归文档位层,这里只改它已渲染的输入框)
              if (id === docId()) {
                const t = wrap.querySelector('#doc-title');
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
              item: { id, name: node ? node.title : '' }, kind: 'doc', projectId: pid,
              parentId: (node && node.parentId) || null,
              onDone: async (res) => {
                if (!res || res.projectId === pid) { await loadTree(); return; }
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
              await api(Endpoints.doc(id), { method: 'DELETE' });
              UI.toast('已删除(可在回收站恢复)', 'success');
              await loadTree();
              if (id === docId()) location.hash = App.route.project(pid, 'docs');
            } catch (err) { UI.err(err); }
          },
        },
      ], { align: 'end' });
      btn.click();
    }

    /** 开关按钮在编辑头里(随文档位重建),标题随状态变化(状态机每次置位后回调) */
    function syncTreeToggle(p) {
      const b = wrap && wrap.querySelector('#btn-doc-tree');
      if (!b) return;
      b.title = p.isOpen() ? '收起文档列表' : '展开文档列表';
    }

    return {
      key: 'p/' + pid + '/docs',
      render(ctx) {
        const frag = Scope.html(
          '<div class="docs-wrap">' +
          // 遮罩只服务窄屏抽屉(显示条件在 CSS 的 .docs-wrap.tree-open 规则里);视觉走 .scrim 基类
          '<div class="scrim doc-tree-scrim"></div>' +
          // 列框(.doc-tree-col)只是裁剪框,宽度与水平内边距归内容层 .doc-tree-inner(app.css)
          '<div class="doc-tree-col">' +
          '<div class="doc-tree-inner">' +
          UI.toolbar({
            cls: 'doc-tree-head',
            left: '<span class="toolbar-title">文档</span>',
            right: canEdit ? UI.btn({ id: 'btn-new-doc', label: '新文档', icon: 'add-line', kind: 'tonal', size: 'sm' }) : '',
          }) +
          '<div id="doc-tree" class="doc-tree">' + UI.skeleton('rows', 6) + '</div></div></div>' +
          '<div class="doc-editor-col" id="doc-editor-col"></div>' +
          '</div>');

        wrap = frag.querySelector('.docs-wrap');
        treeEl = frag.querySelector('#doc-tree');
        const editorCol = frag.querySelector('#doc-editor-col');

        // 面板监听挂在 document/UI.NARROW 上,本层被换掉时必须销毁,否则闭包会操纵旧节点
        panel = UI.dualPanel({
          el: wrap,
          openCls: 'tree-open',
          prefKey: 'td:tree-open',
          onChange: syncTreeToggle,
        });
        ctx.dispose(panel.destroy);
        wrap.querySelector('.doc-tree-scrim').addEventListener('click', panel.close);

        const newDocBtn = frag.querySelector('#btn-new-doc');
        if (newDocBtn) newDocBtn.onclick = () => createDoc(null);

        loadTree();
        // 文档位立刻挂上:编辑器骨架(或空态)这一帧就位,正文到了就地填
        ctx.mount(editorCol);
        // 编辑头由子层同步建好,晚于状态机首次置位 —— 补一次回调,让开关按钮语义就位
        panel.refresh();
        return frag;
      },
      // 文档位描述在挂载那一刻产出:读当前路由拿 docId,并带上本层的运行时(回调、面板)
      child() {
        const id = docId();
        const shared = {
          pid, canEdit, createDoc, onTreeChanged: loadTree,
          onTreeToggle: panel.toggle,
          // 标题的即取值:文档树里已经有它,编辑器第一帧就能显示正确的标题
          docTitle: () => (findNode(id) || {}).title,
        };
        return id
          ? DocEditorView.level(Object.assign({ docId: id }, shared))
          : unopenedSlot(Object.assign({}, shared, { docId: null, panel: panel }));
      },
      // 子层换人:原地移动高亮行(重绘整棵树会丢滚动位置)
      onChild(props) { DocTree.setActive(treeEl, props.docId); },
    };
  };

  /**
   * 未选文档时的文档位。它是"文档位"的另一半而不是特例分支 —— 槽位永远有层,
   * 于是"子层被换掉之后父层要恢复什么"这个问题根本不存在。
   */
  function unopenedSlot(o) {
    return {
      key: 'p/' + o.pid + '/docs/-',
      props: { docId: null },
      render() {
        // 空态主行动随状态走,保证任何状态下都有明确出路:
        //   树关着(窄屏首载/用户收起)→「打开文档列表」直达开关;
        //   树开着且可写 →「新建文档」兑现 desc 的预告(列表已在左侧,不缺入口);
        //   树开着只读 → 无行动,从左侧列表选择即可
        const frag = Scope.html('');
        frag.appendChild(UI.emptyState({
          icon: 'edit-box-line',
          title: '选择一篇文档',
          desc: o.panel.isOpen() && o.canEdit ? '或者创建一篇新文档' : '',
          action: !o.panel.isOpen()
            ? { label: '打开文档列表', onClick: o.panel.toggle }
            : (o.canEdit ? { label: '新建文档', onClick: () => o.createDoc(null) } : null),
        }));
        return frag;
      },
    };
  }
})();
