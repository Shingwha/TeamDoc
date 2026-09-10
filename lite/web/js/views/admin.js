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
      '<div class="page-head">' +
      '<div><h1 class="page-title">管理后台</h1><div class="page-sub">用户管理</div></div>' +
      '<button class="btn btn-filled" id="btn-new-user" type="button"><i class="ri-user-add-line"></i>新建用户</button>' +
      '</div>' +
      '<div class="card user-list" id="admin-body">' + UI.loadingRow() + '</div>' +
      '</div>';

    const body = container.querySelector('#admin-body');
    let users = [];

    async function load() {
      try {
        users = await api('/api/users') || [];
        if (!users.length) {
          body.innerHTML = '';
          body.appendChild(UI.emptyState({ icon: 'ri-user-line', title: '暂无用户' }));
          return;
        }
        body.innerHTML = users.map((u) =>
          '<div class="user-row" data-uid="' + UI.esc(u.id) + '">' +
          '<div class="user-id">' +
          UI.avatar({ name: u.name || u.email, seed: u.id, size: 36 }) +
          '<div style="min-width:0"><div class="u-name">' + UI.esc(u.name) + '</div>' +
          '<div class="u-mail">' + UI.esc(u.email) + '</div></div></div>' +
          '<div class="small muted">' + UI.esc(UI.fmtDate(u.createdAt)) + '</div>' +
          '<div>' + (u.isAdmin ? '<span class="badge primary">管理员</span>' : '<span class="badge">成员</span>') + '</div>' +
          '<div>' + (u.isDisabled ? '<span class="badge danger">已禁用</span>' : '<span class="badge success">正常</span>') + '</div>' +
          '<div class="user-acts">' +
          '<button class="btn-icon u-edit" type="button" title="编辑">' + UI.icon('edit-line') + '</button>' +
          '<button class="btn-icon u-reset" type="button" title="重置密码">' + UI.icon('key-2-line') + '</button>' +
          '<button class="btn-icon ' + (u.isDisabled ? '' : 'danger') + ' u-toggle" type="button" title="' +
          (u.isDisabled ? '启用' : '禁用') + '">' + UI.icon(u.isDisabled ? 'play-circle-line' : 'forbid-circle-line') + '</button>' +
          '</div></div>'
        ).join('');
      } catch (e) {
        body.innerHTML = '<div class="text-danger">' + UI.esc(e.message) + '</div>';
      }
    }

    function findUser(uid) { return users.find((u) => u.id === uid); }

    // 新建用户(邮箱重复 409 由服务端透出)
    container.querySelector('#btn-new-user').onclick = () => {
      UI.modal({
        title: '新建用户',
        body:
          '<div class="field"><label>邮箱 *</label>' +
          '<input type="email" class="input" id="nu-email" autocomplete="off"></div>' +
          '<div class="field"><label>姓名 *</label>' +
          '<input class="input" id="nu-name" maxlength="50"></div>' +
          '<div class="field"><label>初始密码(至少 8 位)*</label>' +
          '<input type="password" class="input" id="nu-password" autocomplete="new-password"></div>' +
          '<label class="check-row"><input type="checkbox" id="nu-admin">设为管理员</label>',
        actions: [
          { label: '取消', kind: 'text', value: null },
          {
            label: '创建', kind: 'filled',
            handler: async ({ close, body: b, btn }) => {
              const email = b.querySelector('#nu-email').value.trim();
              const name = b.querySelector('#nu-name').value.trim();
              const password = b.querySelector('#nu-password').value;
              if (!email || !name || password.length < 8) { UI.toast('请填写邮箱、姓名,密码至少 8 位', 'warning'); return; }
              btn.disabled = true;
              try {
                await api('/api/users', {
                  method: 'POST',
                  body: { email, name, password, isAdmin: b.querySelector('#nu-admin').checked },
                });
                close(true);
                UI.toast('用户已创建', 'success');
                await load();
              } catch (e) {
                btn.disabled = false;
                UI.err(e);
              }
            },
          },
        ],
      });
    };

    body.addEventListener('click', async (e) => {
      const row = e.target.closest('.user-row[data-uid]');
      if (!row) return;
      const u = findUser(row.dataset.uid);
      if (!u) return;

      if (e.target.closest('.u-edit')) {
        UI.modal({
          title: '编辑用户',
          body:
            '<div class="field"><label>邮箱</label>' +
            '<input class="input" value="' + UI.esc(u.email) + '" disabled></div>' +
            '<div class="field"><label>姓名</label>' +
            '<input class="input" id="eu-name" maxlength="50" value="' + UI.esc(u.name) + '"></div>' +
            '<label class="check-row"><input type="checkbox" id="eu-admin"' + (u.isAdmin ? ' checked' : '') + '>管理员</label>',
          actions: [
            { label: '取消', kind: 'text', value: null },
            {
              label: '保存', kind: 'filled',
              handler: async ({ close, body: b, btn }) => {
                btn.disabled = true;
                try {
                  await api('/api/users/' + u.id, {
                    method: 'PATCH',
                    body: { name: b.querySelector('#eu-name').value.trim(), isAdmin: b.querySelector('#eu-admin').checked },
                  });
                  close(true);
                  UI.toast('已保存', 'success');
                  await load();
                } catch (err) {
                  // 最后一名可用管理员保护由服务端 409 透出
                  btn.disabled = false;
                  UI.err(err);
                }
              },
            },
          ],
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
