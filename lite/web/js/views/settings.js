// views/settings.js — 个人设置(#/settings)
// 修改密码 / 访问令牌(PAT)管理
window.Views = window.Views || {};
(function () {
  'use strict';

  window.Views.settings = async function (container) {
    let me;
    try { me = await api(Endpoints.authMe()); }
    catch (e) { UI.err(e); return; }
    const u = me.user;

    // 纯表单卡,用窄页
    container.classList.add('page-narrow');
    container.innerHTML =
      UI.pageHead({ title: '个人设置' }) +
      '<div class="card-grid wide">' +
      '<div class="stack">' +
      // 账号身份:id 是唯一不变的标识(邮箱是可改的登录名)
      UI.card({
        title: '账号', icon: 'user-3-line',
        body:
          '<div class="field"><label>邮箱</label>' +
          // 只读框标 username:改密表单就在同页,浏览器靠它把新密码关联到本账号
          // (关联不上就另存一条重复条目);readonly 字段不会被回填,标了是安全的
          '<input class="input" value="' + UI.esc(u.email) + '" readonly autocomplete="username"></div>' +
          '<div class="field"><label>姓名</label>' +
          '<input class="input" value="' + UI.esc(u.name) + '" readonly></div>' +
          '<div class="field"><label>ID</label>' +
          '<input class="input" id="my-id" value="' + u.id + '" readonly></div>',
      }) +
      // 修改密码
      UI.card({
        title: '修改密码', icon: 'lock-2-line',
        body:
          '<form id="pwd-form" class="form">' +
          '<div class="field"><label>原密码</label>' +
          '<input type="password" class="input" name="oldPassword" required autocomplete="current-password"></div>' +
          '<div class="field"><label>新密码(至少 8 位)</label>' +
          '<input type="password" class="input" name="newPassword" required minlength="8" autocomplete="new-password"></div>' +
          // 密码不可见,故须二次输入:防的是打错后自己也不知道,而非防攻击
          '<div class="field"><label>确认新密码</label>' +
          '<input type="password" class="input" name="confirmPassword" required minlength="8" autocomplete="new-password"></div>' +
          '<div>' + UI.btn({ label: '保存', kind: 'filled', type: 'submit' }) + '</div>' +
          '</form>',
      }) +
      '</div>' +
      // PAT 管理
      '<div class="stack">' +
      UI.card({
        title: '访问令牌', icon: 'key-line', between: true,
        actions: UI.btn({ id: 'pat-new', label: '新建令牌', icon: 'add-line', kind: 'filled' }),
        body: '<div id="pat-list">' + UI.loadingRow() + '</div>',
      }) +
      '</div>' +
      '</div>';

    // ---------- 修改密码 ----------
    container.querySelector('#pwd-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const newPassword = String(fd.get('newPassword') || '');
      if (newPassword !== String(fd.get('confirmPassword') || '')) {
        UI.toast('两次输入的新密码不一致', 'warning');
        return;
      }
      try {
        const r = await api(Endpoints.myPassword(), {
          method: 'POST',
          body: { oldPassword: String(fd.get('oldPassword') || ''), newPassword },
        });
        // 服务端会轮换掉其它设备的会话:说清楚"别的设备要重新登录",免得被当成故障
        const n = (r && r.revokedSessions) || 0;
        UI.toast(n ? '密码已修改,' + n + ' 个其它会话已失效' : '密码已修改', 'success');
        e.target.reset();
      } catch (err) { UI.err(err); }
    });


    // ---------- PAT 管理 ----------
    const patList = container.querySelector('#pat-list');
    function loadPats() {
      return UI.loadInto(patList, () => api(Endpoints.pats()), {
        empty: (list) => !(list || []).filter((p) => !p.revokedAt).length,
        emptyHtml: UI.emptyHtml({ icon: 'key-line', title: '暂无令牌' }),
        errorOpts: { sm: true },
        // 副文本只留"最近使用":它能回答"这个令牌还有人在用吗",
        // 而创建时间在判断令牌是否该清理时并不提供额外信息(两个时间戳并列反而更长)
        render: (list) => (list || []).filter((p) => !p.revokedAt).map((p) =>
          UI.listRow({
            attrs: 'data-pid="' + UI.esc(p.id) + '"',
            title: UI.esc(p.name),
            badges: UI.badge({ text: p.scopes || 'read' }),
            sub: p.lastUsedAt ? '最近使用 ' + UI.esc(UI.fmtDate(p.lastUsedAt)) : '从未使用',
            actions: UI.iconBtn({ icon: 'delete-bin-line', title: '吊销', danger: true, cls: 'pat-revoke' }),
          })
        ).join(''),
      });
    }
    await loadPats();

    patList.addEventListener('click', async (e) => {
      const btn = e.target.closest('.pat-revoke');
      if (!btn) return;
      await UI.confirmAction('使用该令牌的工具将立即失效。确定吊销?', { okMsg: '已吊销' }, async () => {
        await api(Endpoints.pat(btn.closest('.list-row').dataset.pid), { method: 'DELETE' });
        await loadPats();
      });
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
          const r = await api(Endpoints.pats(), { method: 'POST', body: { name: v.name.trim(), scopes: v.scopes } });
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
            onClick: () => { if (token) UI.copyText(token); },
          },
          { label: '我已保存', kind: 'filled', value: true },
        ],
      });
      void m;
    }
  };
})();
