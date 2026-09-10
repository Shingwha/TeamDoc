// views/settings.js — 个人设置(#/settings)
// 修改密码 / 两步验证(TOTP)绑定 / PAT 管理
window.Views = window.Views || {};
(function () {
  'use strict';

  const PAT_API = '/api/auth/pats';

  window.Views.settings = async function (container) {
    let me;
    try { me = await api('/api/auth/me'); }
    catch (e) { UI.err(e); return; }
    const u = me.user;

    container.innerHTML =
      '<div class="view-narrow">' +
      UI.pageHead({ title: '个人设置' }) +
      '<div class="card-grid wide">' +
      '<div class="stack">' +
      // 修改密码
      UI.card({
        title: '修改密码', icon: 'lock-2-line',
        body:
          '<form id="pwd-form">' +
          '<div class="field"><label>原密码</label>' +
          '<input type="password" class="input" name="oldPassword" required autocomplete="current-password"></div>' +
          '<div class="field"><label>新密码(至少 8 位)</label>' +
          '<input type="password" class="input" name="newPassword" required minlength="8" autocomplete="new-password"></div>' +
          UI.btn({ label: '保存', kind: 'filled', size: 'sm', type: 'submit' }) +
          '</form>',
      }) +
      // 两步验证
      UI.card({
        title: '两步验证(TOTP)', icon: 'shield-keyhole-line',
        actions: u.totpEnabled ? UI.badge({ text: '已开启', kind: 'success' }) : UI.badge({ text: '未开启' }),
        body: '<div id="totp-area"></div>',
      }) +
      '</div>' +
      // PAT 管理
      '<div class="stack">' +
      UI.card({
        title: '访问令牌(PAT)', icon: 'key-line', between: true,
        actions: UI.btn({ id: 'pat-new', label: '新建令牌', icon: 'add-line', kind: 'filled', size: 'sm' }),
        note: '用于 CLI(td)等工具以 Bearer 方式访问 API。明文只在创建时显示一次。',
        body: '<div id="pat-list">' + UI.loadingRow() + '</div>',
      }) +
      '</div>' +
      '</div>' +
      '</div>';

    // ---------- 修改密码 ----------
    container.querySelector('#pwd-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        await api('/api/users/me/password', {
          method: 'POST',
          body: { oldPassword: String(fd.get('oldPassword') || ''), newPassword: String(fd.get('newPassword') || '') },
        });
        UI.toast('密码已修改', 'success');
        e.target.reset();
      } catch (err) { UI.err(err); }
    });

    // ---------- 两步验证 ----------
    const totpArea = container.querySelector('#totp-area');
    function renderTotpArea() {
      if (u.totpEnabled) {
        totpArea.innerHTML =
          '<p class="card-text">登录时需额外输入身份验证器中的动态验证码。</p>' +
          UI.btn({ id: 'totp-disable', label: '解绑两步验证', kind: 'danger-outline', size: 'sm' });
        totpArea.querySelector('#totp-disable').onclick = async () => {
          const password = await UI.inputDialog({ title: '解绑两步验证', label: '请输入当前密码确认', type: 'password' });
          if (!password) return;
          try {
            await api('/api/auth/totp/disable', { method: 'POST', body: { password } });
            UI.toast('已解绑两步验证', 'success');
            App.user.totpEnabled = false;
            App.refresh();
          } catch (err) { UI.err(err); }
        };
      } else {
        totpArea.innerHTML =
          '<p class="card-text">绑定身份验证器(如 Google Authenticator)后,登录需要动态验证码。</p>' +
          UI.btn({ id: 'totp-start', label: '开始绑定', kind: 'filled', size: 'sm' });
        totpArea.querySelector('#totp-start').onclick = async () => {
          const password = await UI.inputDialog({ title: '绑定两步验证', label: '请输入当前密码确认', type: 'password' });
          if (!password) return;
          try {
            const r = await api('/api/auth/totp/setup', { method: 'POST', body: { password } });
            // 展示 secret 与 otpauth uri 文本,由用户手动添加到验证器
            totpArea.innerHTML =
              '<div class="field"><label>密钥(secret)</label>' +
              '<div class="secret-box">' + UI.esc(r.secret) + '</div></div>' +
              '<div class="field"><label>绑定链接(otpauth uri)</label>' +
              '<div class="secret-box sm">' + UI.esc(r.uri) + '</div></div>' +
              '<p class="card-text">请将上方密钥或链接添加到身份验证器,然后输入生成的 6 位动态验证码完成绑定。</p>' +
              '<div class="control-row narrow">' +
              '<input class="input" id="totp-code" inputmode="numeric" maxlength="8" placeholder="6 位动态验证码">' +
              UI.btn({ id: 'totp-enable', label: '完成绑定', kind: 'filled' }) + '</div>' +
              UI.btn({ id: 'totp-cancel', label: '取消', kind: 'text', size: 'sm', cls: 'mt-2' });
            totpArea.querySelector('#totp-enable').onclick = async () => {
              const code = totpArea.querySelector('#totp-code').value.trim();
              if (!code) { UI.toast('请输入动态验证码', 'warning'); return; }
              try {
                await api('/api/auth/totp/enable', { method: 'POST', body: { code } });
                UI.toast('两步验证已开启', 'success');
                App.user.totpEnabled = true;
                App.refresh();
              } catch (err) { UI.err(err); }
            };
            totpArea.querySelector('#totp-cancel').onclick = () => renderTotpArea();
          } catch (err) { UI.err(err); }
        };
      }
    }
    renderTotpArea();

    // ---------- PAT 管理 ----------
    const patList = container.querySelector('#pat-list');
    async function loadPats() {
      try {
        const list = await api(PAT_API) || [];
        const active = list.filter((p) => !p.revokedAt);
        if (!active.length) {
          patList.innerHTML = '';
          patList.appendChild(UI.emptyState({ icon: 'key-line', title: '暂无令牌', desc: '创建一个令牌供 CLI 使用' }));
          return;
        }
        patList.innerHTML = active.map((p) =>
          UI.listRow({
            attrs: 'data-pid="' + UI.esc(p.id) + '"',
            title: UI.esc(p.name),
            badges: UI.badge({ text: p.scopes || 'read' }),
            sub: '最近使用:' + UI.esc(p.lastUsedAt ? UI.fmtDate(p.lastUsedAt) : '从未使用') +
              ' · 创建于 ' + UI.esc(UI.fmtDate(p.createdAt)),
            actions: UI.iconBtn({ icon: 'delete-bin-line', title: '吊销', danger: true, cls: 'pat-revoke' }),
          })
        ).join('');
      } catch (e) {
        patList.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message, sm: true });
      }
    }
    await loadPats();

    patList.addEventListener('click', async (e) => {
      const btn = e.target.closest('.pat-revoke');
      if (!btn) return;
      if (!(await UI.confirmDialog('吊销后使用该令牌的 CLI / 脚本将立即失效。确定吊销?'))) return;
      try {
        await api(PAT_API + '/' + btn.closest('.list-row').dataset.pid, { method: 'DELETE' });
        UI.toast('已吊销', 'success');
        await loadPats();
      } catch (err) { UI.err(err); }
    });

    container.querySelector('#pat-new').onclick = () => {
      UI.formModal({
        title: '新建访问令牌',
        okText: '创建',
        fields: [
          { name: 'name', label: '名称', required: true, maxlength: 50, placeholder: '例如:本机 CLI' },
          {
            name: 'scopes', label: '权限范围', type: 'select', value: 'read',
            options: [
              { value: 'read', label: 'read(只读)' },
              { value: 'read,write', label: 'read,write(读写)' },
            ],
          },
        ],
        submit: async (v, { close }) => {
          const r = await api(PAT_API, { method: 'POST', body: { name: v.name.trim(), scopes: v.scopes } });
          close(true);
          showTokenOnce(r && r.token);
          await loadPats();
        },
      });
    };

    // 明文只显示一次,醒目提示复制
    function showTokenOnce(token) {
      const m = UI.modal({
        title: '令牌创建成功',
        body:
          UI.banner({
            kind: 'warn', icon: 'error-warning-line',
            html: '<span>明文令牌仅此一次显示,关闭后无法再次查看,请立即复制并妥善保存。</span>',
          }) +
          '<div class="secret-box mt-4">' +
          UI.esc(token || '(响应未包含 token 字段,请检查后端创建接口返回值)') + '</div>',
        actions: [
          {
            label: '复制令牌', kind: 'tonal',
            handler: () => { if (token) UI.copyText(token); },
          },
          { label: '我已保存', kind: 'filled', value: true },
        ],
      });
      void m;
    }
  };
})();
