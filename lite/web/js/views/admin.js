// views/admin.js — 管理后台(#/admin,仅 is_admin)
// 用户列表 + 新建 / 编辑(姓名、设管理员)/ 禁用 / 重置密码
window.Views = window.Views || {};
(function () {
  'use strict';

  window.Views.admin = async function (container) {
    if (!App.user || !App.user.isAdmin) {
      UI.toast('仅管理员可访问管理后台', 'warning');
      location.replace('#/');
      return;
    }

    container.innerHTML =
      '<div class="view-narrow">' +
      UI.pageHead({
        title: '管理后台',
        sub: '用户管理',
        actions: UI.btn({ id: 'btn-new-user', label: '新建用户', icon: 'user-add-line', kind: 'filled' }),
      }) +
      '<div id="admin-body">' + UI.loadingRow() + '</div>' +
      '</div>';

    const body = container.querySelector('#admin-body');
    let users = [];

    // 列宽:身份(弹性) / 加入时间 / 角色徽标 / 状态徽标 / 操作。
    // 徽标列必须固定宽 —— 文案长短不一(「管理员」/「成员」),用 auto 会让各行列错位;
    // 操作 = 3 × 36px 图标按钮 + 2 × 2px 间距
    const TPL = 'minmax(0, 1.6fr) minmax(0, 1fr) 64px 64px 112px';

    async function load() {
      try {
        users = await api('/api/users') || [];
        if (!users.length) {
          body.innerHTML = '';
          body.appendChild(UI.emptyState({ icon: 'user-line', title: '暂无用户' }));
          return;
        }
        body.innerHTML = UI.tableHead(
          [{ html: '用户' }, { html: '加入时间' }, { html: '角色' }, { html: '状态' }, { html: '' }],
          { tpl: TPL, cls: 'acts-static' }
        ) + users.map((u) =>
          UI.tableRow([
            {
              html: UI.cellId({
                avatar: UI.avatar({ name: u.name || u.email, seed: u.id, size: 'lg', color: u.avatarColor }),
                title: UI.esc(u.name),
                sub: UI.esc(u.email),
              }),
            },
            { html: UI.cellMeta(UI.esc(UI.fmtDate(u.createdAt))) },
            { html: u.isAdmin ? UI.badge({ text: '管理员', kind: 'primary' }) : UI.badge({ text: '成员' }) },
            { html: u.isDisabled ? UI.badge({ text: '已禁用', kind: 'danger' }) : UI.badge({ text: '正常', kind: 'success' }) },
          ], {
            attrs: 'data-uid="' + UI.esc(u.id) + '"',
            acts:
              UI.iconBtn({ icon: 'edit-line', title: '编辑', cls: 'u-edit' }) +
              UI.iconBtn({ icon: 'key-2-line', title: '重置密码', cls: 'u-reset' }) +
              UI.iconBtn({
                icon: u.isDisabled ? 'play-circle-line' : 'forbid-circle-line',
                title: u.isDisabled ? '启用' : '禁用',
                danger: !u.isDisabled, cls: 'u-toggle',
              }),
          })
        ).join('');
      } catch (e) {
        body.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message });
      }
    }

    function findUser(uid) { return users.find((u) => u.id === uid); }

    // 新建用户(邮箱重复 409 由服务端透出)
    container.querySelector('#btn-new-user').onclick = () => {
      UI.formModal({
        title: '新建用户',
        okText: '创建',
        fields: [
          { name: 'email', label: '邮箱', type: 'email', required: true },
          { name: 'name', label: '姓名', required: true, maxlength: 50 },
          { name: 'password', label: '初始密码(至少 8 位)', type: 'password', required: true, autocomplete: 'new-password' },
          { name: 'isAdmin', type: 'checkbox', checkbox: '设为管理员' },
        ],
        submit: async (v, { close }) => {
          if (v.password.length < 8) { UI.toast('密码至少 8 位', 'warning'); return; }
          await api('/api/users', {
            method: 'POST',
            body: { email: v.email.trim(), name: v.name.trim(), password: v.password, isAdmin: v.isAdmin },
          });
          close(true);
          UI.toast('用户已创建', 'success');
          await load();
        },
      });
    };

    body.addEventListener('click', async (e) => {
      const row = e.target.closest('.data-table-row[data-uid]');
      if (!row) return;
      const u = findUser(row.dataset.uid);
      if (!u) return;

      if (e.target.closest('.u-edit')) {
        UI.formModal({
          title: '编辑用户',
          okText: '保存',
          fields: [
            { name: 'email', label: '邮箱', value: u.email, disabled: true },
            { name: 'name', label: '姓名', value: u.name, maxlength: 50 },
            { name: 'isAdmin', type: 'checkbox', checkbox: '管理员', value: u.isAdmin },
          ],
          // 最后一名可用管理员保护由服务端 409 透出
          submit: async (v, { close }) => {
            await api('/api/users/' + u.id, {
              method: 'PATCH',
              body: { name: v.name.trim(), isAdmin: v.isAdmin },
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
          help: '为「' + u.email + '」设置新密码',
        });
        if (!pwd) return;
        if (pwd.length < 8) { UI.toast('密码至少 8 位', 'warning'); return; }
        try {
          await api('/api/users/' + u.id, { method: 'PATCH', body: { password: pwd } });
          UI.toast('密码已重置', 'success');
        } catch (err) { UI.err(err); }
      } else if (e.target.closest('.u-toggle')) {
        const disabling = !u.isDisabled;
        const ok = await UI.confirmDialog(
          (disabling ? '禁用后该用户将立即无法访问系统。' : '') + '确定' + (disabling ? '禁用' : '启用') + '「' + u.email + '」?',
          { danger: disabling, okText: disabling ? '禁用' : '启用' });
        if (!ok) return;
        try {
          await api('/api/users/' + u.id, { method: 'PATCH', body: { isDisabled: disabling } });
          UI.toast(disabling ? '已禁用' : '已启用', 'success');
          await load();
        } catch (err) { UI.err(err); }
      }
    });

    await load();
  };
})();
