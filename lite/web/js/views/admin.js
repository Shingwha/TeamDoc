// views/admin.js — 管理后台(#/admin,仅 is_admin)
//   三个区:存储(占用总览 / 孤儿清理 / 备份下载)+ 用户管理 + 危险操作提示
//   存储区回答的是"盘还剩多少、都被谁占了、有没有看不见的垃圾",这是内网自部署
//   最容易出事又最看不到的一块
window.Views = window.Views || {};
(function () {
  'use strict';

  window.Views.admin = async function (container) {
    if (!App.user || !App.user.isAdmin) {
      UI.toast('仅管理员可访问管理后台', 'warning');
      location.replace('#/');
      return;
    }

    // 存储统计 + 用户表都要横向铺开,用默认页宽(与项目/发现/云空间同档)
    container.innerHTML =
      UI.pageHead({
        title: '管理后台',
        sub: '存储与用户',
      }) +
      '<div id="admin-store"></div>' +
      '<div id="admin-body"></div>';

    const storeEl = container.querySelector('#admin-store');
    const body = container.querySelector('#admin-body');
    let users = [];

    // 列宽:身份(弹性) / 加入时间 / 角色徽标 / 状态徽标 / 操作。
    // 徽标列必须固定宽 —— 文案长短不一(「管理员」/「成员」),用 auto 会让各行列错位;
    // 操作列用 components.css 派生的 --col-acts(--acts-n × 行内档),不写死像素,
    // 免得多/少一个按钮就和列宽脱钩
    const TPL = 'minmax(0, 1.6fr) minmax(0, 1fr) 64px 64px var(--col-acts)';

    // ---------- 存储区 ----------

    /** 占比条 + 图例:TeamDoc 自身占用的构成(文件 / 回收站 / 文档+版本)。
     *  分母只用本应用占的空间,不含系统其它文件 —— 否则小团队的数据量下
     *  整条几乎全是空白,看不出构成。 */
    function occupancyBar(s) {
      const docsBytes = s.docs.bytes + s.docs.versionBytes;
      const total = s.files.activeBytes + s.files.trashBytes + docsBytes;
      if (total <= 0) return '';
      const pct = (n) => Math.max(n > 0 ? 0.6 : 0, (n / total) * 100); // 极小值留一丝可见
      return '<div class="seg-bar">' +
        '<i class="active" style="width:' + pct(s.files.activeBytes) + '%"></i>' +
        '<i class="trash" style="width:' + pct(s.files.trashBytes) + '%"></i>' +
        '<i class="other" style="width:' + pct(docsBytes) + '%"></i>' +
        '</div>' +
        '<div class="legend">' +
        '<span class="legend-item"><i class="legend-dot active"></i>文件 ' + UI.esc(UI.fmtSize(s.files.activeBytes)) + '</span>' +
        '<span class="legend-item"><i class="legend-dot trash"></i>回收站 ' + UI.esc(UI.fmtSize(s.files.trashBytes)) + '</span>' +
        '<span class="legend-item"><i class="legend-dot other"></i>文档与历史版本 ' + UI.esc(UI.fmtSize(docsBytes)) + '</span>' +
        '<span class="legend-item">TeamDoc 共占 ' + UI.esc(UI.fmtSize(total)) + '</span>' +
        '</div>';
    }

    function storeHtml(s) {
      const disk = s.disk, o = s.orphans;
      // 磁盘剩余不足 10% 或不足 5GB 时标红:这是"该清理了"的可执行信号。
      // 注意这是**整块磁盘**的余量,不是 TeamDoc 的占用 —— 副标题写明,免得误读
      const lowFree = disk.free < disk.total * 0.1 || disk.free < 5 * 1024 * 1024 * 1024;
      const projRows = (s.projects || []).slice(0, 8);
      const more = (s.projects || []).length - projRows.length;
      return UI.sectionTitle({
        title: '存储', icon: 'database-2-line',
        actions: UI.btn({ id: 'btn-backup', label: '下载备份', icon: 'download-2-line', kind: 'tonal' }),
      }) +
        UI.card({
          body:
          '<div class="stat-grid">' +
          '<div class="stat"><div class="stat-label">' + UI.icon('hard-drive-3-line') + '磁盘剩余</div>' +
          '<div class="stat-value' + (lowFree ? ' danger' : '') + '">' + UI.esc(UI.fmtSize(disk.free)) + '</div>' +
          '<div class="stat-sub">整块磁盘 ' + UI.esc(UI.fmtSize(disk.total)) + ',已用 ' +
          Math.round((disk.used / disk.total) * 100) + '%</div></div>' +
          // 生效中的限制(只读):这些值来自部署时的环境变量,界面上原先看不到 ——
          // 用户传大文件被拒才知道有上限。放在这里让"上限是多少"一眼可查。
          '<div class="stat"><div class="stat-label">' + UI.icon('upload-2-line') + '单文件上限</div>' +
          '<div class="stat-value">' + UI.esc(UI.fmtSize((s.limits ? s.limits.maxUploadMb : 0) * 1024 * 1024)) + '</div>' +
          '<div class="stat-sub">磁盘保留 ' +
          UI.esc(UI.fmtSize((s.limits ? s.limits.storageReserveMb : 0) * 1024 * 1024)) +
          ',低于它拒绝上传</div></div>' +
          '<div class="stat"><div class="stat-label">云空间文件</div>' +
          '<div class="stat-value">' + UI.esc(UI.fmtSize(s.files.activeBytes)) + '</div>' +
          '<div class="stat-sub">' + s.files.activeCount + ' 个 · ' + s.files.folderCount + ' 个文件夹</div></div>' +
          '<div class="stat"><div class="stat-label">回收站占用</div>' +
          '<div class="stat-value muted">' + UI.esc(UI.fmtSize(s.files.trashBytes)) + '</div>' +
          '<div class="stat-sub">' + s.files.trashCount + ' 个,仍占磁盘</div></div>' +
          '<div class="stat"><div class="stat-label">文档与历史版本</div>' +
          '<div class="stat-value">' + UI.esc(UI.fmtSize(s.docs.bytes + s.docs.versionBytes)) + '</div>' +
          '<div class="stat-sub">' + s.docs.count + ' 篇 · 版本 ' + s.docs.versionCount + ' 条</div></div>' +
          '</div>' +
          occupancyBar(s) +
          (o.orphans > 0
            ? UI.banner({
                kind: 'warn', icon: 'alert-line', sm: true,
                text: '发现 ' + o.orphans + ' 个无主文件(' + UI.fmtSize(o.orphanBytes) +
                  '):磁盘上有、数据库里没有引用,通常是上传中断或删项目留下的,占空间但界面上看不见。',
                action: { label: '清理', id: 'btn-cleanup' },
              })
            : '') +
          (o.missing > 0
            ? UI.banner({
                kind: 'danger', icon: 'error-warning-line', sm: true,
                text: '有 ' + o.missing + ' 条文件记录在磁盘上找不到内容(可能被手动删除或磁盘故障)。' +
                  '建议从备份恢复,不建议直接删记录。',
              })
            : '') +
          (projRows.length
            ? '<div class="section-title">' + UI.icon('bar-chart-2-line') + ' 各项目占用</div>' +
              projRows.map((p) =>
                UI.listRow({
                  icon: 'folder-line', iconCls: 'fi-default',
                  title: UI.esc(p.name),
                  sub: p.fileCount + ' 个文件' +
                    (p.trashFileCount ? ' · 回收站 ' + p.trashFileCount + ' 个' : ''),
                  meta: UI.esc(UI.fmtSize(p.bytes + p.trashBytes)),
                })
              ).join('') +
              (more > 0 ? '<div class="card-note mt-2">另有 ' + more + ' 个项目,未显示</div>' : '')
            : ''),
      });
    }

    async function loadStore() {
      try {
        const s = await api('/api/admin/storage');
        storeEl.innerHTML = storeHtml(s);
      } catch (e) {
        storeEl.innerHTML = UI.banner({ kind: 'danger', icon: 'error-warning-line', text: e.message });
      }
    }

    storeEl.addEventListener('click', async (e) => {
      if (e.target.closest('#btn-cleanup')) {
        const ok = await UI.confirmDialog(
          '将删除磁盘上所有"数据库无引用"的文件。这些文件在界面里看不到,删除不影响任何文档与云空间内容。确定清理?',
          { okText: '清理' });
        if (!ok) return;
        try {
          const r = await api('/api/admin/storage/cleanup', { method: 'POST' });
          UI.toast('已清理 ' + r.removed + ' 个文件,释放 ' + UI.fmtSize(r.freedBytes), 'success');
          await loadStore();
        } catch (err) { UI.err(err); }
      } else if (e.target.closest('#btn-backup')) {
        // 锚点下载:window.open 对附件流不可靠(可能被拦或开空白页)
        const a = document.createElement('a');
        a.href = '/api/admin/backup';
        a.download = 'teamdoc-backup.zip';
        document.body.appendChild(a);
        a.click();
        a.remove();
        UI.toast('备份下载已开始(含数据库快照与全部文件)', 'info');
      }
    });

    // ---------- 用户管理 ----------

    async function load() {
      try {
        users = await api('/api/users') || [];
        if (!users.length) {
          body.innerHTML = '';
          body.appendChild(UI.emptyState({ icon: 'user-line', title: '暂无用户' }));
          return;
        }
        body.innerHTML =
          UI.sectionTitle({
            title: '用户', icon: 'team-line',
            // 分区级操作放在分区标题栏:它的作用范围就是这个用户表,
            // 放在页面级页头会与上半部的存储区产生"按钮管哪块"的歧义
            actions: UI.btn({ id: 'btn-new-user', label: '新建用户', icon: 'user-add-line', kind: 'filled' }),
          }) +
          UI.tableHead(
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
                  icon: u.isDisabled ? 'play-circle-line' : 'forbid-2-line',
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

    /** 新建用户。按钮在"用户"分区标题栏里,而该标题栏随用户表一起重渲染,
     *  故走 body 的事件委托(见下方 click 监听),不在此处直接绑 onclick */
    function openNewUser() {
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
    }

    body.addEventListener('click', async (e) => {
      if (e.target.closest('#btn-new-user')) { openNewUser(); return; }
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

    await Promise.all([loadStore(), load()]);
  };
})();
