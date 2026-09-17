// move-target.js — 「移动到…」的共享实现(文件 / 文件夹 / 文档共用一套选择器)。
//
// 三种资源的移动端点各有一个(它们的 payload 字段名不同:folderId / parentId),
// 但**用户看到的东西完全一样**:选目标项目 → 选目标位置 → 确认。三种资源共用这一套
// 选择器:各写一份就会在两处各自漂移(权限提示、目录排除、默认值)。
//
// 三个资源类型的差异全部收在 KINDS 表里:
//   tree      目标位置树的取数函数(Endpoints 里的文件夹树 / 文档树)
//   fieldName 移动端点里"目标位置"的字段名
//   exclude   需要在目标树里排除的子树根(移动文件夹/文档时排除自己,防环)
window.MoveTarget = (function () {
  'use strict';

  var KINDS = {
    file: { label: '文件', tree: Endpoints.folderTree, fieldName: 'folderId',
            path: Endpoints.fileMove },
    folder: { label: '文件夹', tree: Endpoints.folderTree, fieldName: 'parentId',
              path: Endpoints.folderMove },
    doc: { label: '文档', tree: Endpoints.docTree, fieldName: 'parentId',
           path: Endpoints.docMove },
  };

  /**
   * 打开移动弹窗。
   * @param opts.item     {id, name} 被移动的资源
   * @param opts.kind     'file' | 'folder' | 'doc'
   * @param opts.projectId 当前所在项目(默认选中)
   * @param opts.parentId  当前位置(同项目内移动时默认选中,便于原地整理)
   * @param opts.onDone   移动成功后的回调(参数为服务端返回体)
   */
  async function open(opts) {
    var kind = KINDS[opts.kind];
    if (!kind) throw new Error('未知的移动类型: ' + opts.kind);
    var projects = [];
    try { projects = await api(Endpoints.projects()) || []; }
    catch (e) { UI.err(e); return; }

    var hint = document.createElement('p');
    var m = UI.modal({
      title: '移动到',
      body:
        '<p class="modal-text muted mb-4">将「' + UI.esc(opts.item.name) +
        '」移动位置(仅改归属,内容不复制不移动)。</p>' +
        '<div class="field flush"><label>目标项目</label>' +
        '<select class="select" id="mv-proj"></select></div>' +
        '<div class="field flush mt-3"><label>目标位置</label>' +
        '<select class="select" id="mv-dir"></select></div>',
      actions: [
        { label: '取消', kind: 'text', value: null },
        {
          label: '移动', kind: 'filled',
          onClick: async function (ctx) {
            var projId = ctx.body.querySelector('#mv-proj').value;
            var dirId = ctx.body.querySelector('#mv-dir').value;
            if (projId === opts.projectId && (dirId || null) === (opts.parentId || null)) {
              ctx.close(false);
              return;   // 目标与现状相同:不打扰服务端
            }
            ctx.btn.disabled = true;
            try {
              var payload = { projectId: projId };
              payload[kind.fieldName] = dirId || null;
              var res = await api(kind.path(opts.item.id), { method: 'POST', body: payload });
              ctx.close(true);
              UI.toast('已移动', 'success');
              if (opts.onDone) opts.onDone(res);
            } catch (e) {
              ctx.btn.disabled = false;
              UI.err(e);
            }
          },
        },
      ],
    });
    m.body.appendChild(hint);
    hint.className = 'modal-text muted mt-3';
    hint.hidden = true;

    var projSel = m.body.querySelector('#mv-proj');
    var dirSel = m.body.querySelector('#mv-dir');
    // 目标项目:全部项目(含当前项目 —— 项目内整理是高频需求);个人项目由服务端置顶
    projSel.innerHTML = projects.map(function (p) {
      return '<option value="' + UI.esc(p.id) + '"' +
        (p.id === opts.projectId ? ' selected' : '') + '>' +
        UI.esc(p.name) + (p.isPersonal ? '(个人)' : '') + '</option>';
    }).join('');

    /** 目标位置:按所选项目拉树铺平成缩进选项(excludeId 排除自己与后代,防环) */
    async function loadDirs() {
      var projId = projSel.value;
      var sameProject = projId === opts.projectId;
      dirSel.innerHTML = '<option value="">(项目根)</option>';
      var tree = [];
      try {
        tree = await api(kind.tree(projId)) || [];
      } catch (e) {
        return;   // 无权限的项目(如他人个人空间)拿不到树,只留根
      }
      var opts_html = [];
      (function walk(nodes, depth) {
        (nodes || []).forEach(function (n) {
          // 排除自己(return 会连同子树一起跳过):移到自己的后代里会成环
          if (n.id === opts.item.id) return;
          // option 里 HTML 实体不渲染,层级缩进用全角空格(视觉上稳定,不依赖字体等宽)
          opts_html.push('<option value="' + UI.esc(n.id) + '">' +
            '\u3000'.repeat(depth) + UI.esc(n.name || n.title || '') + '</option>');
          walk(n.children, depth + 1);
        });
      })(tree, 0);
      dirSel.innerHTML += opts_html.join('');
      if (sameProject && opts.parentId) dirSel.value = opts.parentId;
    }

    /** 跨项目时问一次服务端:哪些东西不会跟着走(文档的附件留在源项目) */
    async function checkHint() {
      if (opts.kind !== 'doc' || projSel.value === opts.projectId) { hint.hidden = true; return; }
      try {
        var r = await api(Endpoints.docMoveCheck(opts.item.id, {
          projectId: projSel.value,
          parentId: dirSel.value || '',
        }));
        var lines = ['将移动 ' + r.docs + ' 篇文档(含子文档)。'];
        if (r.foreignFiles && r.foreignFiles.length) {
          lines.push('正文引用的 ' + r.foreignFiles.length +
            ' 个文件仍留在原项目:' + r.foreignFiles.slice(0, 3).map(function (f) {
              return '「' + f.name + '」';
            }).join('') + (r.foreignFiles.length > 3 ? ' 等' : '') + '。');
        }
        hint.textContent = lines.join('');
        hint.hidden = false;
      } catch (e) {
        hint.hidden = true;   // 提示拿不到不该挡住移动本身
      }
    }

    projSel.addEventListener('change', async function () { await loadDirs(); checkHint(); });
    dirSel.addEventListener('change', checkHint);
    await loadDirs();
    checkHint();
  }

  return { open: open };
})();
