// views/admin/users.js — 管理后台·用户区(用户表 / 新建 / 编辑 / 重置密码 /
// 禁用 / 删除)+ 用户详情(账号标识 / 登录限制 / 活跃会话 / 登录记录)。
// resultBadge/RESULT_META 也服务 security 区(登录动态表),故挂在 AdminSections 上共享。
window.AdminSections = window.AdminSections || {};
(function () {
  'use strict';

  // 结果码 → 徽标。服务端给稳定代码,文案只在前端映射(与 roleLabel 同一分工)。
  const RESULT_META = {
    ok: { label: '成功', kind: 'success', icon: 'check-line' },
    bad_password: { label: '密码错误', kind: 'danger', icon: 'close-line' },
    disabled: { label: '已禁用账号', kind: 'warn', icon: 'forbid-2-line' },
    locked: { label: '触发锁定', kind: 'danger', icon: 'lock-line' },
  };
  function resultBadge(r) {
    return UI.badge({ text: (RESULT_META[r] || {}).label || r || '未知',
                      kind: (RESULT_META[r] || {}).kind || '' });
  }
  function deviceIcon(kind) { return kind === 'mobile' ? 'smartphone-line' : 'computer-line'; }
  window.AdminSections.RESULT_META = RESULT_META;
  window.AdminSections.resultBadge = resultBadge;

  /** 锁定徽标:文案只写「已锁定」,完整时间进 title —— 列表里那列曾被
   *  「锁定至 9/12 09:23」撑到 148px 宽;精确到分的时间是排查信息,归详情 */
  function lockBadge(u) {
    return u.lockedUntil
      ? UI.badge({ text: '已锁定', kind: 'danger', title: '锁定至 ' + UI.fmtDate(u.lockedUntil) })
      : '';
  }

  /** 列表只标"不是默认态"的:成员是默认角色,每行挂一个「成员」徽标只会把
   *  真正要看的三种状态(管理员 / 已禁用 / 已锁定)淹掉 */
  function stateBadges(u) {
    return (u.isAdmin ? UI.badge({ text: '管理员', kind: 'primary' }) : '') +
      (u.isDisabled ? UI.badge({ text: '已禁用', kind: 'danger' }) : '') +
      lockBadge(u);
  }

  // 列:身份(弹性) / 最近登录(弹性) / 操作(派生固定宽)。宽度与表头写在**同一处** ——
  // 上一版把 --tpl 与单元格分开写,id 从数字换成 26 位 ULID 时只改了单元格,列宽
  // 仍停在 56px,那串 id 于是溢到邻列上、整张表糊成一片。
  // 可用的轨道只有两种:弹性 fr(按比例吃满剩余空间)与派生固定宽(--col-acts)——
  // 每行是各自独立的 grid,内容宽度(auto)的列会各行各算,行与行错位。
  // 不定长的内容(姓名/邮箱)集中在弹性列里、徽标并进身份格(不占列宽):
  // 列越少,弹性列吃得越满,也就越"自适应"。
  const COLS = [
    { head: '用户', w: 'minmax(0, 1fr)' },
    { head: '最近登录', w: 'minmax(0, 0.75fr)' },
    { head: '', w: 'var(--col-acts)' },
  ];
  const TPL = COLS.map((c) => c.w).join(' ');
  const HEADS = COLS.map((c) => ({ html: c.head }));

  window.AdminSections.users = function (ctx) {
    const el = ctx.el;
    let users = [];

    function load() {
      // fetcher 里先落模块状态(空列表也要覆盖旧值,见 projects 区同注释)
      return UI.loadInto(el, () => api(Endpoints.users()).then((list) => {
        users = list || [];
        return users;
      }), {
        empty: (list) => !list.length,
        emptyHtml: UI.emptyHtml({ icon: 'user-line', title: '暂无用户' }),
        render: () => {
          return UI.sectionTitle({
            title: '用户', icon: 'team-line',
            // 分区级操作放在分区标题栏:它的作用范围就是这个用户表,
            // 放在页面级页头会与上半部的存储区产生"按钮管哪块"的歧义
            actions: UI.btn({ id: 'btn-new-user', label: '新建用户', icon: 'user-add-line', kind: 'filled' }),
          }) +
            UI.tableHead(HEADS, { tpl: TPL, cls: 'acts-static acts-5' }) + users.map((u) =>
              UI.tableRow([
                {
                  html: UI.cellId({
                    avatar: UI.avatar({ name: u.name || u.email, seed: u.id, size: 'lg', color: u.avatarColor }),
                    title: UI.esc(u.name),
                    sub: UI.esc(u.email),
                    badges: stateBadges(u),
                  }),
                },
                {
                  // 最近登录:时间 + 在线标记。它留在列表是因为"谁最近还在用"是
                  // 列表级的扫描任务;来源 IP 只在排查时看,归用户详情
                  html: UI.cellId({
                    title: u.lastLoginAt ? UI.esc(UI.fmtDateShort(u.lastLoginAt))
                                         : '<span class="muted">从未登录</span>',
                    badges: u.lastLoginAt
                      ? (u.online ? UI.badge({ text: '在线', kind: 'success' })
                                  : UI.badge({ text: '离线', cls: 'muted' }))
                      : '',
                  }),
                },
              ], {
                attrs: 'data-uid="' + UI.esc(u.id) + '"',
                acts:
                  UI.iconBtn({ icon: 'edit-line', title: '编辑', cls: 'u-edit' }) +
                  UI.iconBtn({ icon: 'key-2-line', title: '重置密码', cls: 'u-reset' }) +
                  UI.iconBtn({ icon: 'shield-keyhole-line', title: '用户详情(标识 / 会话 / 登录记录)',
                               cls: 'u-access' }) +
                  UI.iconBtn({
                    icon: u.isDisabled ? 'play-circle-line' : 'forbid-2-line',
                    title: u.isDisabled ? '启用' : '禁用',
                    danger: !u.isDisabled, cls: 'u-toggle',
                  }) +
                  // 只对"从未产生数据"的账号渲染删除:有内容的账号服务端会拒绝,
                  // 与其让人点了再报错,不如只让能删的显示出来
                  (u.canDelete
                    ? UI.iconBtn({ icon: 'delete-bin-line', title: '删除(仅限无数据的账号)',
                                   danger: true, cls: 'u-delete' })
                    : ''),
              })
            ).join('');
        },
      });
    }

    function findUser(uid) { return users.find((u) => u.id === uid); }

    /** 新建用户。按钮在"用户"分区标题栏里,而该标题栏随用户表一起重渲染,
     *  故走 el 的事件委托(见下方 click 监听),不在此处直接绑 onclick */
    function openNewUser() {
      UI.formModal({
        title: '新建用户',
        okText: '创建',
        // 字段顺序遵循 app.js renderAuthForm 的凭据规则:邮箱紧贴密码框,姓名殿后
        fields: [
          { name: 'email', label: '邮箱', type: 'email', required: true, autocomplete: 'username' },
          { name: 'password', label: '初始密码(至少 8 位)', type: 'password', required: true, autocomplete: 'new-password' },
          { name: 'name', label: '姓名', required: true, maxlength: 50, autocomplete: 'name' },
          { name: 'isAdmin', type: 'checkbox', checkbox: '设为管理员' },
        ],
        submit: async (v, { close }) => {
          if (v.password.length < 8) { UI.toast('密码至少 8 位', 'warning'); return; }
          await api(Endpoints.users(), {
            method: 'POST',
            body: { email: v.email.trim(), name: v.name.trim(), password: v.password, isAdmin: v.isAdmin },
          });
          close(true);
          UI.toast('用户已创建', 'success');
          await load();
        },
      });
    }

    // ---------- 用户详情 ----------

    /** 账号标识:列表里放不下的字段(ID / 加入时间 / 来源 IP)都收在这里。
     *  要加字段就往 detailList 的数组里加一项 —— 布局不用动。 */
    function identityCard(u) {
      return UI.card({
        title: '账号标识', icon: 'fingerprint-line',
        body: UI.detailList([
          { label: '用户 ID', html: UI.idText({ value: u.id, copy: true, full: true }) },
          { label: '邮箱', html: UI.esc(u.email) },
          { label: '加入时间', html: UI.esc(UI.fmtDate(u.createdAt)) },
          {
            label: '角色',
            html: u.isAdmin ? UI.badge({ text: '管理员', kind: 'primary' }) : UI.badge({ text: '成员' }),
          },
          {
            label: '状态',
            html: (u.isDisabled ? UI.badge({ text: '已禁用', kind: 'danger' })
                                : UI.badge({ text: '正常', kind: 'success' })) +
              (u.lockedUntil ? ' <span class="muted">锁定至 ' + UI.esc(UI.fmtDate(u.lockedUntil)) + '</span>' : ''),
          },
          {
            // 精确到分的完整时间与来源 IP:排查"这个账号从哪儿登的"要看它们,
            // 但它们是逐用户的信息,不该占列表的列宽
            label: '最近登录',
            html: u.lastLoginAt
              ? UI.esc(UI.fmtDate(u.lastLoginAt)) +
                (u.lastLoginIp ? ' <span class="muted">来自 ' + UI.esc(u.lastLoginIp) + '</span>' : '')
              : '<span class="muted">从未登录</span>',
          },
        ]),
      });
    }

    /** 登录限制:锁着就给"解除锁定"入口,没锁但有失败计数就说明还差几次被锁。 */
    function lockCard(d) {
      const lock = d.lock || {};
      const max = d.maxFails || 5;
      if (lock.lockedUntil) {
        return UI.card({
          title: '登录限制', icon: 'lock-line',
          body: '<div class="muted">该账号连续登录失败,已进入冷却(第 ' + (lock.strikes || 1) +
                ' 次触发)。冷却会自行过期,也可以立即解除。</div>',
          actions: UI.btn({ label: '解除锁定', kind: 'tonal', size: 'sm', id: 'acc-unlock' }),
        });
      }
      if (lock.failCount > 0) {
        return UI.card({
          title: '登录限制', icon: 'shield-keyhole-line',
          body: '<div class="muted">窗口内已失败 ' + lock.failCount + ' 次,再失败 ' +
                Math.max(0, max - lock.failCount) + ' 次将自动锁定(上限 ' + max + ' 次)。</div>',
        });
      }
      return UI.card({
        title: '登录限制', icon: 'shield-check-line',
        body: '<div class="muted">无限制:窗口内没有失败记录。</div>',
      });
    }

    function sessionsCard(d) {
      const list = d.sessions || [];
      const rows = list.map((s) => UI.listRow({
        icon: deviceIcon(s.deviceKind),
        title: UI.esc(s.device) +
          (s.online ? ' ' + UI.badge({ text: '在线', kind: 'success' }) : '') +
          (s.current ? ' ' + UI.badge({ text: '当前会话', cls: 'tint' }) : ''),
        // 登录时间与最近活跃都给:前者回答"这是谁什么时候登的",
        // 后者回答"它现在还在动吗"(会话有效期 7/30 天,只看创建时间会误判)
        sub: UI.esc([s.ip || '未知来源',
                     '登录 ' + UI.fmtDateShort(s.createdAt),
                     '最近活跃 ' + UI.fmtDateShort(s.lastSeenAt || s.createdAt)].join(' · ')),
        actions: UI.btn({ label: '强制下线', kind: 'danger-outline', size: 'sm',
                          cls: 'acc-kick', attrs: 'data-ref="' + UI.esc(s.ref) + '"' }),
      })).join('');
      return UI.card({
        title: '活跃会话(' + list.length + ')', icon: 'computer-line',
        body: rows || '<div class="muted">没有未过期的会话。</div>',
        actions: list.length
          ? UI.btn({ label: '全部下线', kind: 'danger-outline', size: 'sm', id: 'acc-kick-all' })
          : '',
      });
    }

    function eventsCard(d) {
      const list = d.events || [];
      const rows = list.map((e) => UI.listRow({
        icon: (RESULT_META[e.result] || {}).icon || 'question-line',
        title: resultBadge(e.result) + ' <span class="muted">' +
               UI.esc(UI.fmtDate(e.createdAt)) + '</span>',
        sub: UI.esc([e.ip || '未知来源', e.device || '未知设备'].join(' · ')),
      })).join('');
      return UI.card({
        title: '登录记录(最近 ' + list.length + ' 条)', icon: 'history-line',
        note: '含密码错误与触发锁定;冷却期内被拦下的请求不落记录(它们在"登录限制"里体现)',
        body: rows || '<div class="muted">暂无登录记录。</div>',
      });
    }

    function wireAccess(root, u, repaint) {
      const unlock = root.querySelector('#acc-unlock');
      if (unlock) unlock.addEventListener('click', async () => {
        try {
          await api(Endpoints.user(u.id), { method: 'PATCH', body: { unlock: true } });
          UI.toast('已解除登录锁定', 'success');
          // 抽屉自身重画 + 用户表/登录动态的状态徽标也要跟上
          await Promise.all([repaint(), ctx.refresh('users', 'security')]);
        } catch (e) { UI.err(e); }
      });
      root.querySelectorAll('.acc-kick').forEach((b) => b.addEventListener('click', async () => {
        try {
          await api(Endpoints.adminRevokeSession(b.dataset.ref), { method: 'DELETE' });
          UI.toast('该会话已下线', 'success');
          await Promise.all([repaint(), ctx.refresh('users', 'security')]);
        } catch (e) { UI.err(e); }
      }));
      const all = root.querySelector('#acc-kick-all');
      if (all) all.addEventListener('click', () => UI.confirmAction(
        '强制下线「' + (u.name || u.email) + '」的全部会话?该用户在所有设备上都需要重新登录。',
        { okText: '全部下线', okMsg: '已全部下线', danger: true },
        async () => {
          await api(Endpoints.adminRevokeUserSessions(u.id), { method: 'DELETE' });
          await Promise.all([repaint(), ctx.refresh('users', 'security')]);
        }));
    }

    async function openAccess(u) {
      const m = UI.modal({
        title: '用户详情 · ' + (u.name || u.email), wide: true,
        body: UI.loadingRow(),
      });
      const paint = async () => {
        let d;
        try { d = await api(Endpoints.adminAccess(u.id)); }
        catch (e) { m.body.innerHTML = UI.errorBanner(e); return; }
        m.body.innerHTML = identityCard(u) + lockCard(d) + sessionsCard(d) + eventsCard(d);
        wireAccess(m.body, u, paint);
      };
      await paint();
    }

    // 行内操作:事件委托挂在本区容器上
    el.addEventListener('click', async (e) => {
      if (e.target.closest('#btn-new-user')) { openNewUser(); return; }

      const row = e.target.closest('.data-table-row[data-uid]');
      if (!row) return;
      const u = findUser(row.dataset.uid);
      if (!u) return;

      if (e.target.closest('.u-access')) {
        await openAccess(u);
      } else if (e.target.closest('.u-edit')) {
        UI.formModal({
          title: '编辑用户',
          okText: '保存',
          fields: [
            // 邮箱可改:它既是登录名也是唯一约束。建错一个(比如打错域名)就既登不进
            // 也无法重名重建,必须有地方修。服务端会查重,冲突时以 409 透出。
            { name: 'email', label: '邮箱', type: 'text', value: u.email, required: true },
            { name: 'name', label: '姓名', value: u.name, maxlength: 50 },
            { name: 'isAdmin', type: 'checkbox', checkbox: '管理员', value: u.isAdmin },
          ],
          // 最后一名可用管理员保护由服务端 409 透出
          submit: async (v, { close }) => {
            await api(Endpoints.user(u.id), {
              method: 'PATCH',
              body: { email: v.email.trim(), name: v.name.trim(), isAdmin: v.isAdmin },
            });
            close(true);
            UI.toast('已保存', 'success');
            await load();
          },
        });
      } else if (e.target.closest('.u-reset')) {
        const pwd = await UI.inputDialog({
          title: '重置密码',
          label: '新密码(至少 8 位)',
          type: 'password',
          // 密码框不能靠默认的 autocomplete="off":浏览器会忽略密码框上的 off,可能把管理员
          // 自己的密码回填进"给他人设密码"的框。new-password 令牌才挡得住回填
          autocomplete: 'new-password',
          help: '为「' + u.email + '」设置新密码;该用户的登录会话与访问令牌会一并失效',
        });
        if (!pwd) return;
        if (pwd.length < 8) { UI.toast('密码至少 8 位', 'warning'); return; }
        try {
          await api(Endpoints.user(u.id), { method: 'PATCH', body: { password: pwd } });
          UI.toast('密码已重置', 'success');
        } catch (err) { UI.err(err); }
      } else if (e.target.closest('.u-delete')) {
        // 失败由 confirmAction 统一 UI.err 透出:服务端对"有数据的账号"会回 409 并说明原因,
        // 让管理员知道改用禁用
        await UI.confirmAction(
          '永久删除「' + u.email + '」?该账号从未产生任何文档、文件或项目,删除后不可恢复。',
          { okText: '删除', okMsg: '用户已删除' },
          async () => {
            await api(Endpoints.user(u.id), { method: 'DELETE' });
            await load();
          });
      } else if (e.target.closest('.u-toggle')) {
        const disabling = !u.isDisabled;
        // 禁用前点名其唯一拥有的项目,防止制造"所有者失联"的死锁项目。
        // projects 由项目区加载写进 ctx.state,加载失败时为空 —— 只是少了提醒,不阻断禁用
        const soleOwned = disabling
          ? (ctx.state.projects || []).filter((p) => !p.isPersonal && p.owners.length === 1 && p.owners[0].id === u.id)
          : [];
        const lockHint = soleOwned.length
          ? '注意:「' + soleOwned.map((p) => p.name).join('、') +
            '」的唯一所有者是该用户,禁用后请到「项目」区把所有权交给接手人,否则项目将无人可管理。'
          : '';
        const ok = await UI.confirmDialog(
          (disabling ? '禁用后该用户将立即无法访问系统。' : '') + lockHint +
          '确定' + (disabling ? '禁用' : '启用') + '「' + u.email + '」?',
          { danger: disabling, okText: disabling ? '禁用' : '启用' });
        if (!ok) return;
        try {
          await api(Endpoints.user(u.id), { method: 'PATCH', body: { isDisabled: disabling } });
          UI.toast(disabling ? '已禁用' : '已启用', 'success');
          // 项目区所有者的"已禁用"徽标要跟上
          await ctx.refresh('users', 'projects');
        } catch (err) { UI.err(err); }
      }
    });

    return { key: 'users', load };
  };
})();
