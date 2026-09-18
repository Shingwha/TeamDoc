// views/project.js — 项目作用域 + 项目内各模块作用域
//   Views.projectLevel          — 项目对象只在这里取一次;模块层由它挂出来
//   Views.membersLevel          — 成员
//   Views.trashLevel            — 回收站
//   Views.projectSettingsLevel  — 项目设置
//   Views.filesLevel            — 云空间(主体复用 views/drive.js 的 driveBody)
//   文档模块在 views/docs.js(自带一层,切文档时它原样留着)
//
// 作用域契约见 ARCHITECTURE.md「渲染契约」:render 同步画骨架并返回 Fragment;
// 数据自己异步填;子层用 ctx.mount 交出挂载点;清理用 ctx.dispose 注册。
window.Views = window.Views || {};
(function () {
  'use strict';

  /**
   * 项目作用域。它自己只画一个 #proj-body(项目页无大页头,页头归各模块),
   * 拿到项目对象后才把模块子层挂出来 —— 项目内切模块、切文档都不会再重取项目对象。
   * 打不开项目时(403/JOIN_REQUIRED/不存在)错误态由本层画,子层根本不存在:
   * 这份错误路径只有这一处,不必每个模块各写一遍。
   */
  window.Views.projectLevel = function (r) {
    const pid = r.pid;
    let proj = null;

    return {
      key: 'p/' + pid,
      render(ctx) {
        // 等待期的骨架按**当前模块的形状**给:文档页是"树列 + 编辑区"(满高布局,内边距
        // 归列自己),其余模块是单列。形状一致,数据到达时就地替换,不用重新找位置
        const cur = Route.current() || {};
        const shape = cur.tab === 'docs' ? Views.docsSkeleton() : UI.skeleton('rows', 6);
        const frag = Scope.html('<div id="proj-body">' + shape + '</div>');
        const body = frag.querySelector('#proj-body');
        load(body, ctx);
        return frag;
      },
      // 模块描述在挂载那一刻产出:proj 已经到手,而且读的是**当前**路由(不是本层
      // 构造时的快照)—— 切模块时本层被复用,只有这里会重新执行
      child() {
        const cur = Route.current();
        const nav = Route.navOf(cur.tab) || Route.navOf('docs');
        return nav.level(Object.assign({}, cur, { proj: proj }));
      },
    };

    async function load(body, ctx) {
      try {
        proj = await api(Endpoints.project(pid));
      } catch (e) {
        console.error(e);
        // 公开项目的 403(JOIN_REQUIRED)是唯一说得清出路的失败:直链打开、同事发来的
        // 链接都落在这里,给一个加入按钮,而不是让人对着"需要 VIEWER 及以上权限"发懵
        const joinable = e.code === 'JOIN_REQUIRED';
        body.innerHTML =
          UI.banner({ kind: 'danger', icon: 'error-warning-line',
                      text: joinable ? '你还不是这个项目的成员,加入后才能查看。'
                                     : '无法打开该项目:' + (e.message || '未知错误') }) +
          '<div class="form-inline mt-4">' +
          UI.btn({ id: 'proj-back', label: '返回项目列表', kind: 'tonal' }) +
          (joinable ? UI.btn({ id: 'proj-join', label: '加入项目', kind: 'filled' }) : '') +
          '</div>';
        const back = body.querySelector('#proj-back');
        if (back) back.onclick = () => { location.hash = '#/'; };
        const joinBtn = body.querySelector('#proj-join');
        if (joinBtn) joinBtn.onclick = () => ProjectsAPI.joinPrompt(pid);
        return;   // 不交槽位 = 没有子层
      }
      if (!ctx.alive()) return;
      body.innerHTML = '';
      ctx.mount(body);
    }
  };

  // ==================== 成员模块(#/p/{id}/members;个人项目不可见,入口已被导航隐藏) ====================
  window.Views.membersLevel = function (r) {
    const proj = r.proj;
    return {
      key: 'p/' + r.pid + '/members',
      render(ctx) {
        // 直链打开个人项目的成员页:没有子层可挂,直接改地址
        if (proj.isPersonal) { location.replace(App.route.project(r.pid, 'docs')); return Scope.html(''); }
        // 管理入口一律走 UI.canAdmin(服务端 project_role 的镜像):达到 ADMIN 的只有
        // 真成员里的管理员与全局管理员 —— 非成员没有角色,不可能误看到管理入口
        const canAdmin = UI.canAdmin(proj);
        // OWNER 选项只对有 OWNER 级管辖权的人展示(UI.canOwn = 真所有者或全局管理员):
        // 项目 ADMIN 选了也会被服务端 403,与其让人点了再报错,不如不渲染
        const canOwn = UI.canOwn(proj);
        const ROLE_CHOICES = canOwn ? ['OWNER', 'ADMIN', 'EDITOR', 'VIEWER'] : ['ADMIN', 'EDITOR', 'VIEWER'];

        // 页头右上角放「添加成员」(与其他页面的头按钮同位);副标题省略 —— 项目名在侧栏,人数看列表
        const frag = Scope.html(
          UI.pageHead({
            title: '成员',
            actions: (canAdmin ? UI.btn({ id: 'mb-pick', label: '添加成员', icon: 'user-add-line', kind: 'filled', size: 'sm' }) : ''),
          }) +
          UI.card({
            body: '<div id="mb-list">' + UI.skeleton('rows', 4) + '</div>',
          }));

        const listEl = frag.querySelector('#mb-list');
        function load() {
          return UI.loadInto(listEl, () => api(Endpoints.projectMembers(proj.id)), {
            empty: (members) => !(members || []).length,
            emptyNode: () => UI.emptyState({ icon: 'team-line', title: '暂无成员' }),
            render: (members) => members.map((mb) => {
              const u = mb.user || {};
              return UI.listRow({
                attrs: 'data-uid="' + UI.esc(mb.userId) + '"',
                avatar: UI.avatar({ name: u.name || u.email, seed: mb.userId, color: u.avatarColor }),
                title: UI.esc(u.name || '-'),
                badges: u.isDisabled ? UI.badge({ text: '已禁用', kind: 'danger' }) : '',
                sub: UI.esc(u.email || '-'),
                actions: canAdmin
                  ? '<select class="select mb-role-sel">' +
                    ROLE_CHOICES.map((rr) =>
                      '<option value="' + rr + '"' + (rr === mb.role ? ' selected' : '') + '>' + UI.esc(UI.roleLabel(rr)) + '</option>'
                    ).join('') + '</select>' +
                    UI.iconBtn({ icon: 'user-unfollow-line', title: '移除成员', danger: true, cls: 'mb-remove' })
                  : UI.badge({ text: UI.roleLabel(mb.role) }),
              });
            }).join(''),
          });
        }
        load();

        if (!canAdmin) return frag;

        /** 已在项目中的用户 id:选人浮层按 id 排除,免得选了才报 409 */
        function existingIds() {
          return [...listEl.querySelectorAll('.list-row')]
            .map((row) => row.dataset.uid).filter(Boolean);
        }
        async function addMember(userId, role) {
          try {
            await api(Endpoints.projectMembers(proj.id), { method: 'POST', body: { userId, role } });
            await load();
            return true;
          } catch (e) { UI.err(e); return false; }
        }

        // 添加成员:批量圈选(候选态,可反复勾选),在浮层里选好加入角色,确认后统一加入
        frag.querySelector('#mb-pick').onclick = async () => {
          const picked = await UI.personPicker({
            title: '从同事目录选择',
            excludeIds: existingIds(),
            multi: true,
            roleSelect: {
              label: '加入角色',
              options: ROLE_CHOICES.map((rr) => ({ value: rr, label: UI.roleLabel(rr) })),
              value: 'EDITOR',
            },
          });
          if (!picked || !picked.length) return;
          const res = await UI.eachOk(picked, (p) =>
            addMember(p.id, p.role).then((okFlag) => {
              if (!okFlag) throw new Error('add member failed'); // 失败计数靠抛错,名单从 failed 取
            }));
          if (res.failed.length) UI.toast('添加失败:' + res.failed.map((p) => p.name || p.email).join('、'), 'danger');
          else UI.toast(res.ok === 1 ? '已添加 ' + (picked[0].name || picked[0].email) : '已添加 ' + res.ok + ' 名成员', 'success');
        };
        listEl.addEventListener('change', async (e) => {
          const sel = e.target.closest('.mb-role-sel');
          if (!sel) return;
          const uid = sel.closest('.list-row').dataset.uid;
          try {
            await api(Endpoints.projectMember(proj.id, uid), { method: 'PATCH', body: { role: sel.value } });
            UI.toast('已更新角色', 'success');
          } catch (err) { UI.err(err); await load(); }
        });
        listEl.addEventListener('click', async (e) => {
          const btn = e.target.closest('.mb-remove');
          if (!btn) return;
          const uid = btn.closest('.list-row').dataset.uid;
          if (!(await UI.confirmDialog('确定移除该成员?'))) return;
          try {
            await api(Endpoints.projectMember(proj.id, uid), { method: 'DELETE' });
            UI.toast('已移除', 'success');
            await load();
          } catch (err) { UI.err(err); }
        });
        return frag;
      },
    };
  };

  // ==================== 回收站模块(#/p/{id}/trash) ====================
  // 三类回收项(文档 / 文件 / 文件夹)用 .seg 分段切换分类,避免长列表里翻找;
  // 三类数据一次取回后缓存在内存,切 tab 只重渲染、不重新请求
  window.Views.trashLevel = function (r) {
    const proj = r.proj;
    return {
      key: 'p/' + r.pid + '/trash',
      render(ctx) {
        // 副标题由 load() 填充(含项目名与各项计数),故先占位
        const frag = Scope.html(
          UI.pageHead({ title: '回收站', sub: '<span id="trash-sub"></span>' }) +
          UI.card({
            body: '<div id="trash-seg"></div>' +
              '<div id="trash-list">' + UI.skeleton('rows', 4) + '</div>',
          }));

        const listEl = frag.querySelector('#trash-list');
        const segEl = frag.querySelector('#trash-seg');
        const subEl = frag.querySelector('#trash-sub');
        const canWrite = UI.canEdit(proj); // 成员且 EDITOR 及以上可恢复/彻底删除

        // 三类回收项的资源地址都取自 Endpoints(路径字面量不在这里出现)
        const KIND_URL = { doc: Endpoints.doc, file: Endpoints.file, folder: Endpoints.folder };
        const KIND_ICON = { doc: 'file-text-line', file: 'file-line', folder: 'folder-line' };
        const CATS = [
          { key: 'docs', kind: 'doc', label: '文档', icon: 'file-text-line' },
          { key: 'files', kind: 'file', label: '文件', icon: 'file-line' },
          { key: 'folders', kind: 'folder', label: '文件夹', icon: 'folder-line' },
        ];

        let data = { docs: [], files: [], folders: [] };
        let cat = 'docs';

        function items(key) { return data[key] || []; }

        /** 段标签渲染:非空分类才在标签上带数量,便于一眼看出哪类有待处理项 */
        function renderSeg() {
          segEl.innerHTML = UI.seg({
            auto: true, role: 'tablist', active: cat,
            items: CATS.map((c) => ({
              key: c.key, label: c.label, icon: c.icon,
              count: items(c.key).length, disabled: !items(c.key).length,
            })),
          });
        }

        function rowHtml(item, kind) {
          const isDoc = kind === 'doc';
          const label = isDoc ? item.title : item.name;
          // 分类已由上方 tab 表达,副标题不再重复"文档/文件"字样,只留元信息
          const meta = kind === 'file' && item.size != null ? UI.fmtSize(item.size) + ' · ' : '';
          return UI.listRow({
            attrs: 'data-kind="' + kind + '" data-id="' + UI.esc(item.id) + '"',
            icon: KIND_ICON[kind] || 'file-line',
            title: UI.esc(label),
            sub: meta + '删除于 ' + UI.esc(UI.fmtDate(item.deletedAt)),
            actions: canWrite
              ? UI.btn({ label: '恢复', kind: 'outline', size: 'sm', cls: 'tr-restore' }) +
                UI.iconBtn({ icon: 'delete-bin-line', title: '彻底删除', danger: true, size: 'sm', cls: 'tr-purge' })
              : '',
          });
        }

        function renderList() {
          const rows = items(cat);
          if (!rows.length) {
            listEl.innerHTML = '';
            // 全空 → 回收站整体为空;仅当前分类空 → 提示切到别的分类
            const total = items('docs').length + items('files').length + items('folders').length;
            listEl.appendChild(total
              ? UI.emptyState({ icon: 'inbox-line', title: '该分类下没有内容' })
              : UI.emptyState({ icon: 'delete-bin-line', title: '回收站为空' }));
            return;
          }
          const kind = CATS.find((c) => c.key === cat).kind;
          listEl.innerHTML = rows.map((it) => rowHtml(it, kind)).join('');
        }

        async function load() {
          try {
            data = await api(Endpoints.projectTrash(proj.id)) || {};
            const total = items('docs').length + items('files').length + items('folders').length;
            // 默认落在第一个非空分类:只删过文件时不会先看到空的"文档"页
            if (!items(cat).length) {
              const first = CATS.find((c) => items(c.key).length);
              if (first) cat = first.key;
            }
            subEl.textContent = proj.name + ' · 共 ' + total + ' 项' +
              (total ? ' · 已删除的项可在此恢复;彻底删除不可恢复' : '');
            renderSeg();
            renderList();
          } catch (e) {
            listEl.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message });
          }
        }
        load();

        UI.segWire(segEl, (key) => {
          if (key === cat) return;
          cat = key;
          renderSeg();
          renderList();
        });

        listEl.addEventListener('click', async (e) => {
          const row = e.target.closest('.list-row');
          if (!row) return;
          const id = row.dataset.id, kind = row.dataset.kind;
          const url = (KIND_URL[kind] || Endpoints.file)(id);
          if (e.target.closest('.tr-restore')) {
            try {
              const res = await api(url + '/restore', { method: 'POST' });
              UI.toast('已恢复 ' + ((res && res.count) || 1) + ' 项', 'success');
              await load();
            } catch (err) { UI.err(err); }
            return;
          }
          if (e.target.closest('.tr-purge')) {
            const name = row.querySelector('.list-row-title').textContent;
            const tail = kind === 'doc' ? '将连同子文档与历史版本一起清除,'
              : kind === 'folder' ? '将连同其中的子文件夹与文件一起彻底清除(物理文件会被删除),'
                : '物理文件将被删除,';
            const ok = await UI.confirmDialog('彻底删除「' + name + '」?' + tail + '此操作不可恢复。');
            if (!ok) return;
            try {
              const res = await api(url + '/permanent', { method: 'DELETE' });
              UI.toast('已彻底删除 ' + ((res && res.count) || 1) + ' 项', 'success');
              await load();
            } catch (err) { UI.err(err); }
          }
        });
        return frag;
      },
    };
  };

  // ==================== 项目设置模块(#/p/{id}/settings) ====================
  window.Views.projectSettingsLevel = function (r) {
    const proj = r.proj;
    return {
      key: 'p/' + r.pid + '/settings',
      render(ctx) {
        const canEdit = UI.canAdmin(proj);    // 成员且 ADMIN 及以上可改
        const canDelete = UI.canOwn(proj); // OWNER 级管辖权;个人空间已在 canOwn 内排除
        const dis = canEdit ? '' : ' disabled';

        const frag = Scope.html(
          UI.pageHead({
            title: '设置',
            sub: '我的角色:' + UI.esc(UI.roleLabel(proj.myRole)) +
              ' · ' + (proj.memberCount != null ? proj.memberCount : '-') + ' 成员' +
              ' · ' + (proj.docCount != null ? proj.docCount : '-') + ' 文档' +
              (proj.isPersonal ? ' · 个人空间' : ''),
          }) +
          '<div class="stack">' +
          UI.card({
            body:
              '<div class="field"><label>项目名</label>' +
              '<input class="input" id="ps-name" maxlength="100" value="' + UI.esc(proj.name) + '"' + dis + '></div>' +
              '<div class="field"><label>描述</label>' +
              '<textarea class="textarea" id="ps-desc" rows="3"' + dis + '>' + UI.esc(proj.description || '') + '</textarea></div>' +
              (canEdit ? UI.btn({ id: 'ps-save', label: '保存', kind: 'filled' }) : ''),
          }) +
          (proj.isPersonal
            ? ''
            : UI.card({
              title: '可见性', icon: proj.isPublic ? 'global-line' : 'lock-line',
              note: proj.isPublic
                ? '公开:任何登录用户都能在「发现」里看到本项目并自助加入;加入前他们看不到项目里的任何内容,' +
                  '加入后按下面的角色获得权限。'
                : '私有:只有项目成员能看到,也无法被自助加入。',
              body: canEdit
                ? '<label class="check-row"><input type="checkbox" id="ps-public"' +
                  (proj.isPublic ? ' checked' : '') + '>公开到「发现」广场(任何同事都能发现并自助加入)</label>' +
                  // 加入角色只在公开时有意义,跟着公开开关一起即时提交
                  (proj.isPublic
                    ? '<div class="field mt-3"><label>同事自助加入后的角色</label>' +
                      '<select class="select" id="ps-join-role">' +
                      [['VIEWER', '只读成员(能看,不能改)'], ['EDITOR', '编辑者(可直接改文档、传文件)']]
                        .map((o) => '<option value="' + o[0] + '"' +
                          (proj.joinRole === o[0] ? ' selected' : '') + '>' + UI.esc(o[1]) + '</option>')
                        .join('') + '</select></div>'
                    : '')
                : (proj.isPublic ? UI.badge({ text: '公开', kind: 'primary' }) : UI.badge({ text: '私有' })),
            })) +
          (proj.isMember && !proj.isPersonal
            ? UI.card({
              danger: true,
              title: '退出项目', icon: 'logout-box-r-line',
              note: '退出后将无法再访问本项目的文档、云空间与回收站,最新动态也不再显示;重新加入需要项目管理员邀请。',
              body: UI.btn({ id: 'ps-leave', label: '退出项目', icon: 'logout-box-r-line', kind: 'danger-outline' }),
            })
            : '') +
          (canDelete
            ? UI.card({
              danger: true,
              title: '危险操作', icon: 'error-warning-line',
              note: '删除项目将同时删除其全部文档与成员关系,且不可恢复。',
              body: UI.btn({ id: 'ps-delete', label: '删除项目', icon: 'delete-bin-line', kind: 'danger-outline' }),
            })
            : '') +
          '</div>');

        const nameEl = frag.querySelector('#ps-name');
        const descEl = frag.querySelector('#ps-desc');
        const saveBtn = frag.querySelector('#ps-save');
        if (saveBtn) saveBtn.onclick = async () => {
          const name = nameEl.value.trim();
          if (!name) { UI.toast('项目名不能为空', 'warning'); return; }
          saveBtn.disabled = true;
          try {
            await api(Endpoints.project(proj.id), {
              method: 'PATCH',
              body: { name, description: descEl.value },
            });
            UI.toast('已保存', 'success');
            App.refreshSidebar();
            App.refresh();   // 项目对象的副本在项目层,新名字要经它传下来
          } catch (e) {
            saveBtn.disabled = false;
            UI.err(e);
          }
        };

        // 可见性开关:即时提交(勾选/取消都是明确意图,不需要再来一次"保存")
        const pubBox = frag.querySelector('#ps-public');
        if (pubBox) pubBox.onchange = async () => {
          const want = pubBox.checked;
          if (want) {
            const ok = await UI.confirmDialog(
              '公开后,任何登录用户都能在「发现」里看到本项目并自助加入。他们加入前看不到项目里的任何内容,' +
              '加入后按你设定的角色获得权限。确定公开?',
              { okText: '公开' });
            if (!ok) { pubBox.checked = false; return; }
          }
          pubBox.disabled = true;
          try {
            await api(Endpoints.project(proj.id), { method: 'PATCH', body: { isPublic: want } });
            UI.toast(want ? '已公开到广场' : '已设为私有', 'success');
            App.refresh(); // 提示文案与徽标跟着走
          } catch (e) {
            pubBox.checked = !want;
            pubBox.disabled = false;
            UI.err(e);
          }
        };

        // 自助加入后的角色:同一张卡上的第二档设置,语义与公开开关一致(选了就是明确意图)
        const roleSel = frag.querySelector('#ps-join-role');
        if (roleSel) roleSel.onchange = async () => {
          roleSel.disabled = true;
          try {
            await api(Endpoints.project(proj.id), { method: 'PATCH', body: { joinRole: roleSel.value } });
            UI.toast('已更新加入角色', 'success');
            App.refresh();
          } catch (e) {
            roleSel.value = proj.joinRole;
            roleSel.disabled = false;
            UI.err(e);
          }
        };

        const delBtn = frag.querySelector('#ps-delete');
        if (delBtn) delBtn.onclick = async () => {
          const ok = await UI.confirmDialog('删除项目将同时删除其全部文档与成员关系,且不可恢复。确定删除「' + proj.name + '」?');
          if (!ok) return;
          try {
            await api(Endpoints.project(proj.id), { method: 'DELETE' });
            UI.toast('项目已删除', 'success');
            App.refreshSidebar();
            location.hash = '#/';
          } catch (e) { UI.err(e); }
        };

        const leaveBtn = frag.querySelector('#ps-leave');
        if (leaveBtn) leaveBtn.onclick = async () => {
          const ownerHint = proj.myRole === 'OWNER'
            ? '你是项目所有者:若你是唯一所有者,需先在成员页把所有权转让给他人才能退出。'
            : '';
          const ok = await UI.confirmDialog(
            '退出「' + proj.name + '」后,你将不再能访问它的文档、云空间与回收站,最新动态也不再显示。' +
            ownerHint + '确定退出?',
            { okText: '退出' });
          if (!ok) return;
          leaveBtn.disabled = true;
          try {
            await api(Endpoints.projectLeave(proj.id), { method: 'POST' });
            UI.toast('已退出「' + proj.name + '」', 'success');
            App.refreshSidebar();
            location.hash = '#/';
          } catch (e) {
            leaveBtn.disabled = false;
            UI.err(e);
          }
        };
        return frag;
      },
    };
  };

  // ==================== 云空间模块(#/p/{id}/files;主体复用 drive.js) ====================
  // query.folder 是深链目标目录(搜索结果"进入所在目录"、刷新保持位置都用它)
  // highlight:从文档 @文件 引用跳转过来,定位并闪烁该文件行(driveBody 内处理)
  window.Views.filesLevel = function (r) {
    return {
      // 键里带 query:换目录是一次真正的换页(?folder=A → 无 query 也要重建,
      // 否则点侧栏「云空间」会留在旧目录里)
      key: 'p/' + r.pid + '/files' + (r.qs ? '?' + r.qs : ''),
      render(ctx) {
        return Views.driveBody({
          projectId: r.pid,
          proj: r.proj,
          folderId: r.query.get('folder') || null,
          highlight: r.query.get('highlight') || null,
        });
      },
    };
  };
})();
