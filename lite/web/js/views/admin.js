// views/admin.js — 管理后台(#/admin,仅 is_admin)
//   四个区:存储(占用总览 / 孤儿清理)+ 备份与恢复 + 项目(全站总览 / 接管)+ 用户管理
//   存储区回答的是"盘还剩多少、都被谁占了、有没有看不见的垃圾",这是内网自部署
//   最容易出事又最看不到的一块;项目区回答的是"谁拥有什么、人走了谁来接管"
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
        sub: '存储、备份、项目、用户与登录安全',
      }) +
      '<div id="admin-store"></div>' +
      '<div id="admin-backup"></div>' +
      '<div id="admin-projects"></div>' +
      '<div id="admin-body"></div>' +
      '<div id="admin-security"></div>';

    const storeEl = container.querySelector('#admin-store');
    const backupEl = container.querySelector('#admin-backup');
    const projEl = container.querySelector('#admin-projects');
    const body = container.querySelector('#admin-body');
    const secEl = container.querySelector('#admin-security');
    let users = [];
    let projects = [];

    // 列宽:身份(弹性) / ID / 加入时间 / 最近登录 / 角色徽标 / 状态徽标 / 操作。
    // 徽标列必须固定宽 —— 文案长短不一(「管理员」/「成员」、「正常」/「锁定至 9/12 09:23」),
    // 用 auto 会让各行列错位;操作列用 components.css 派生的 --col-acts(--acts-n × 行内档),
    // 不写死像素,免得多/少一个按钮就和列宽脱钩
    const TPL = 'minmax(0, 1.5fr) 56px minmax(0, 0.8fr) minmax(0, 1.1fr) 64px 148px var(--col-acts)';

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
      return UI.sectionTitle({
        title: '存储', icon: 'database-2-line',
      }) +
        UI.card({
          body:
          UI.statGrid([
            {
              // 告警语义在这里:整块磁盘剩余不足 10% 或不足 5GB 时标红
              label: '磁盘剩余', icon: 'hard-drive-3-line', danger: lowFree,
              value: UI.fmtSize(disk.free),
              sub: '整块磁盘 ' + UI.fmtSize(disk.total) + ',已用 ' +
                Math.round((disk.used / disk.total) * 100) + '%',
            },
            // 生效中的限制(只读):这些值来自部署时的环境变量,界面上原先看不到 ——
            // 用户传大文件被拒才知道有上限。放在这里让"上限是多少"一眼可查。
            {
              label: '单文件上限', icon: 'upload-2-line',
              value: UI.fmtSize((s.limits ? s.limits.maxUploadMb : 0) * 1024 * 1024),
              sub: '磁盘保留 ' +
                UI.fmtSize((s.limits ? s.limits.storageReserveMb : 0) * 1024 * 1024) +
                ',低于它拒绝上传',
            },
            {
              label: '云空间文件', value: UI.fmtSize(s.files.activeBytes),
              sub: s.files.activeCount + ' 个 · ' + s.files.folderCount + ' 个文件夹',
            },
            {
              label: '回收站占用', muted: true, value: UI.fmtSize(s.files.trashBytes),
              sub: s.files.trashCount + ' 个,仍占磁盘',
            },
            {
              label: '文档与历史版本', value: UI.fmtSize(s.docs.bytes + s.docs.versionBytes),
              sub: s.docs.count + ' 篇 · 版本 ' + s.docs.versionCount + ' 条',
            },
          ]) +
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
            : ''),
      });
    }

    async function loadStore() {
      try {
        const s = await api('/api/admin/storage');
        storeEl.innerHTML = storeHtml(s);
      } catch (e) {
        storeEl.innerHTML = UI.errorBanner(e);
      }
    }

    storeEl.addEventListener('click', async (e) => {
      if (e.target.closest('#btn-cleanup')) {
        // 先 dry-run 预览:告诉管理员"将删掉什么、多少字节",再让他确认。
        // 原来是一句话弹窗直接删 —— 破坏性操作不该让人在看不到范围的情况下点确定。
        let pv;
        try {
          pv = await api('/api/admin/storage/cleanup?dryRun=1', { method: 'POST' });
        } catch (err) { UI.err(err); return; }
        if (!pv.orphans) {
          UI.toast('没有可清理的无主文件', 'info');
          await loadStore();
          return;
        }
        if (pv.breakerTripped) {
          // 熔断:磁盘上绝大多数文件都被判为孤儿,多半是库不对而不是垃圾多。
          // 用 modal 而非 confirmDialog —— 这里只需要"知道了",不给"继续"的口子。
          UI.modal({
            title: '已阻止清理',
            body: UI.banner({
              kind: 'danger', icon: 'error-warning-line',
              html: '<span>检测到 <b>' + pv.orphans + '/' + pv.filesOnDisk +
                '</b> 个文件都被判为无主(超过 2/3)。</span>',
            }) +
              '<p class="modal-text mt-4">这通常意味着数据库为空、指向了错的数据目录,或恢复了一份' +
              '旧备份 —— 此时清理会删光磁盘上的文件,因此已阻止。</p>' +
              '<p class="modal-text mt-2">请先确认数据目录与数据库是否匹配。确实要清理时,' +
              '可直接调用接口并显式带上 force=1。</p>',
            actions: [{ label: '我知道了', kind: 'filled', value: true }],
          });
          return;
        }
        const ok = await UI.confirmDialog(
          '将删除 ' + pv.orphans + ' 个无主文件,释放 ' + UI.fmtSize(pv.orphanBytes) +
          '。这些文件在界面上看不到(数据库无引用),删除不影响任何文档与云空间内容。确定清理?',
          { okText: '清理' });
        if (!ok) return;
        try {
          const r = await api('/api/admin/storage/cleanup', { method: 'POST' });
          UI.toast('已清理 ' + r.removed + ' 个文件,释放 ' + UI.fmtSize(r.freedBytes), 'success');
          await loadStore();
        } catch (err) { UI.err(err); }
      }
    });

    // ---------- 备份与恢复 ----------

    /** 一行目标目录状态:可达 / 不可达 + 上次写入结果。 */
    function targetRows(st) {
      const dirs = (st.config && st.config.dirs) || [];
      const last = st.last || {};
      const per = last.targets || {};
      if (!dirs.length) {
        return UI.banner({
          kind: 'info', icon: 'information-line', sm: true,
          text: '未配置备份目标目录。设置环境变量 BACKUP_DIRS(逗号分隔,可填多个盘/网络共享)后,' +
            '服务会按周期自动备份;在配置之前,"下载备份"仍可随时手动导出。',
        });
      }
      return dirs.map((d) => {
        const r = per[d.path];
        let sub, badge;
        if (!d.exists) {
          // 目录不存在是最需要提前说清的一种:多半是移动盘没插 / 共享没挂载
          badge = UI.badge({ text: '目录不存在', kind: 'danger' });
          sub = '备份会跳过它(不会自动创建目录)';
        } else if (!r) {
          badge = UI.badge({ text: '尚未写入', kind: 'warning' });
          sub = '等待首次备份';
        } else if (r.ok) {
          badge = UI.badge({ text: '正常', kind: 'success' });
          sub = '上次成功' + (r.file ? ' · ' + UI.esc(r.file) : '') +
            (r.pruned ? ' · 清理旧包 ' + r.pruned + ' 份' : '');
        } else {
          badge = UI.badge({ text: '失败', kind: 'danger' });
          sub = UI.esc(r.error || '未知原因');
        }
        return UI.listRow({
          icon: 'folder-line', iconCls: 'fi-default', title: UI.esc(d.path),
          badges: badge, sub: sub,
          meta: r && r.bytes ? UI.esc(UI.fmtSize(r.bytes)) : '',
        });
      }).join('');
    }

    function backupHtml(st, rs) {
      const cfg = st.config || {};
      const last = st.last || {};
      // 上次结果:成功 / 部分成功 / 失败 / 从未
      let verdict;
      if (last.runAt == null) {
        verdict = { label: '上次备份', muted: true, value: '从未备份',
          sub: '配置目标目录后会自动开始,也可点"立即备份"' };
      } else if (last.ok && last.partial) {
        verdict = { label: '上次备份', danger: true, value: '部分成功',
          sub: UI.fmtDate(last.runAt) + ' · 有目标未写入' };
      } else if (last.ok) {
        verdict = { label: '上次备份', value: UI.fmtDate(last.runAt),
          sub: '最近一次成功' + (last.trigger === 'scheduled' ? '(自动)' : '(手动)') };
      } else {
        verdict = { label: '上次备份', danger: true, value: '失败',
          sub: UI.fmtDate(last.runAt) + ' · ' + (last.error || '有目标写入失败') };
      }

      const nextTxt = cfg.enabled
        ? (st.nextRun ? UI.fmtDate(st.nextRun) : '待安排')
        : '未启用';
      const nextSub = cfg.enabled
        ? '每 ' + cfg.intervalHours + ' 小时一次'
        : (cfg.dirs && cfg.dirs.length ? '间隔为 0,仅手动' : '未配置目标目录');

      return UI.sectionTitle({
        title: '备份与恢复', icon: 'save-3-line',
        actions: UI.btn({ id: 'btn-backup-now', label: '立即备份', icon: 'refresh-line', kind: 'tonal' }) +
          UI.btn({ id: 'btn-backup-dl', label: '下载备份', icon: 'download-2-line', kind: 'tonal' }),
      }) +
        UI.card({
          // 同一张卡:上半是备份现状,下半是恢复入口(同一条流水线的两端)
          body: UI.statGrid([
            verdict,
            { label: '下次自动备份', value: nextTxt, sub: nextSub },
            {
              label: '保留份数', value: cfg.keep > 0 ? cfg.keep + ' 份' : '不限',
              sub: '每个目标目录,自动清理更旧的备份',
            },
          ]) + targetRows(st) +
            restoreSection(rs),
          danger: !!(rs && rs.armed),
        });
    }

    /** 恢复区:上传备份 → 校验暂存 → 重启后生效。
     *  与备份共用一张卡(标题行下方),不再单独起一个 section —— 备份与恢复本就是
     *  同一件事的两端,拆成两个 section 只是让页面变长、标题重复。
     *  "重启后才生效"写在最显眼处:不说清的话,管理员点完会以为立刻就换了。 */
    function restoreSection(rs) {
      const head = '<div class="card-subtitle">' + UI.icon('history-line') + '从备份恢复</div>';
      if (!rs || !rs.staged) {
        // 文件选择沿用云空间的做法:UI.btn + 隐藏的 file input。
        // 不用裸 <input type="file"> —— 各浏览器长相不一,也无法跟站内按钮对齐尺寸档。
        return head +
          '<div class="form-inline">' +
          UI.btn({ id: 'restore-pick', label: '选择备份文件', icon: 'upload-2-line', kind: 'tonal' }) +
          UI.btn({ id: 'restore-arm', label: '重启后生效', kind: 'filled', disabled: true }) +
          '</div>' +
          '<input type="file" id="restore-file" accept=".zip,application/zip" hidden>' +
          '<div class="help mt-2">恢复会替换当前全部数据;上传校验通过后点"重启后生效",' +
          '再重启 TeamDoc 服务才会真正替换(替换前自动把现有数据留一份)。</div>';
      }
      const info = '用户 ' + (rs.users == null ? '?' : rs.users) +
        ' · 项目 ' + (rs.projects == null ? '?' : rs.projects) +
        ' · 文档 ' + (rs.docs == null ? '?' : rs.docs) +
        ' · 文件 ' + (rs.files == null ? '?' : rs.files);
      return head +
        UI.banner({
          kind: rs.armed ? 'warn' : 'info',
          icon: rs.armed ? 'restart-line' : 'file-zip-line',
          html: '<span><b>' + (rs.armed ? '已就绪:重启 TeamDoc 服务后生效' : '已暂存,待确认生效') +
            '</b> · 上传于 ' + UI.esc(UI.fmtDate(rs.stagedAt)) + ' · ' + UI.esc(UI.fmtSize(rs.bytes)) +
            '<br>内含:' + UI.esc(info) + '</span>',
        }) +
        '<div class="form-inline">' +
        (rs.armed
          ? UI.btn({ id: 'restore-cancel', label: '取消(放弃这次恢复)', kind: 'text' })
          : UI.btn({ id: 'restore-arm', label: '重启后生效', icon: 'restart-line', kind: 'filled' }) +
            UI.btn({ id: 'restore-cancel', label: '取消', kind: 'text' })) +
        '</div>';
    }

    async function loadBackup() {
      try {
        const [st, rs] = await Promise.all([
          api('/api/admin/backup/status'),
          api('/api/admin/restore/status'),
        ]);
        backupEl.innerHTML = backupHtml(st, rs);
      } catch (e) {
        backupEl.innerHTML = UI.errorBanner(e);
      }
    }

    backupEl.addEventListener('click', async (e) => {
      if (e.target.closest('#btn-backup-dl')) {
        // 刻意不给 UI.download 传 filename —— 让服务端 Content-Disposition 的文件名(带时间戳)
        // 生效,否则每次下载都会覆盖成同一个 teamdoc-backup.zip,事后分不清哪份是新的。
        UI.download('/api/admin/backup');
        UI.toast('备份下载已开始(含数据库快照与全部文件)', 'info');
      } else if (e.target.closest('#btn-backup-now')) {
        const btn = e.target.closest('#btn-backup-now');
        btn.disabled = true;
        try {
          const r = await api('/api/admin/backup/run', { method: 'POST' });
          const oks = Object.values(r.targets || {}).filter((t) => t.ok).length;
          const total = Object.keys(r.targets || {}).length;
          if (r.ok && oks === total) {
            UI.toast('备份完成:' + r.file + '(' + UI.fmtSize(r.bytes) + ',写入 ' + total + ' 个目标)',
              'success');
          } else if (r.ok) {
            // 部分成功必须说清是哪个目标没成,否则"备份成功"会给人虚假安心
            UI.toast('仅写入 ' + oks + '/' + total + ' 个目标,请查看失败原因', 'warning');
          } else {
            UI.toast(r.error || '备份失败,请查看各目标状态', 'danger');
          }
          await loadBackup();
        } catch (err) { UI.err(err); } finally { btn.disabled = false; }
      } else if (e.target.closest('#restore-pick')) {
        const input = backupEl.querySelector('#restore-file');
        if (input) input.click();
      } else if (e.target.closest('#restore-arm')) {
        // confirmDialog 的正文走 esc(),不支持换行与 markdown —— 一句说完
        const ok = await UI.confirmDialog(
          '当前全部数据将被这份备份替换(替换前会自动把现有数据留一份)。' +
          '确认后需重启 TeamDoc 服务才生效。确定?',
          { okText: '重启后生效' });
        if (!ok) return;
        try {
          await api('/api/admin/restore/arm', { method: 'POST' });
          UI.toast('已就绪:重启 TeamDoc 服务后数据将被替换', 'warning');
          await loadBackup();
        } catch (err) { UI.err(err); }
      } else if (e.target.closest('#restore-cancel')) {
        const ok = await UI.confirmDialog('放弃这次恢复?暂存的备份会被删除,当前数据不受影响。',
          { okText: '放弃' });
        if (!ok) return;
        try {
          await api('/api/admin/restore', { method: 'DELETE' });
          UI.toast('已取消恢复', 'info');
          await loadBackup();
        } catch (err) { UI.err(err); }
      }
    });

    // 文件选择:选中即上传(不额外加"上传"按钮 —— 选文件本身就是明确的动作)
    backupEl.addEventListener('change', async (e) => {
      const input = e.target.closest('#restore-file');
      if (!input || !input.files || !input.files[0]) return;
      const file = input.files[0];
      if (!(await UI.confirmDialog(
        '将上传并校验备份「' + file.name + '」(' + UI.fmtSize(file.size) + ')。' +
        '上传后还需点"重启后生效"并重启服务才会真正替换数据。继续?', { okText: '上传' }))) {
        input.value = '';
        return;
      }
      input.disabled = true;
      try {
        // 用 apiUpload(XHR raw body):备份可能上 GB,raw body 不落系统临时目录;
        // 也不能用 api() —— 它的 30 秒总超时会把大备份直接掐断。apiUpload 只做
        // 空闲超时,并实时回报进度(否则大备份上传期间界面看起来像卡死)
        let lastPct = -10;
        const r = await apiUpload('/api/admin/restore/upload', file, {
          onProgress: (pct) => {
            if (pct >= lastPct + 10) { lastPct = pct; UI.toast('备份上传中 ' + pct + '%', 'info'); }
          },
        });
        UI.toast('备份已上传并通过校验' + (r && r.info ? '(用户 ' + r.info.users +
          ' · 项目 ' + r.info.projects + ')' : ''), 'success');
      } catch (err) {
        UI.err(err);
      } finally {
        input.disabled = false;
        input.value = '';
        await loadBackup();
      }
    });

    // ---------- 项目区 ----------

    // 列宽:项目(弹性) / 所有者(弹性) / 成员 / 文档 / 最近活跃 / 占用 / 操作(2 颗)。
    // 数字与日期列固定宽,文案列给弹性 —— 与用户表同一套列宽思路
    const PROJ_TPL = 'minmax(0, 1.4fr) minmax(0, 1fr) 56px 56px minmax(0, 0.9fr) 80px var(--col-acts)';

    async function loadProjects() {
      try {
        projects = await api('/api/admin/projects') || [];
        if (!projects.length) { projEl.innerHTML = ''; return; }
        projEl.innerHTML =
          UI.sectionTitle({ title: '项目', icon: 'folder-3-line' }) +
          UI.tableHead(
            [{ html: '项目' }, { html: '所有者' }, { html: '成员' }, { html: '文档' },
             { html: '最近活跃' }, { html: '占用' }, { html: '' }],
            { tpl: PROJ_TPL, cls: 'acts-static acts-2' }
          ) + projects.map((p) => {
            // 所有者已禁用必须标出来:那是"唯一所有者失联 → 项目无人可接管"的信号,
            // 也是这个分区存在的理由(禁用后在这里把所有权交给接手人)
            const ownerHtml = p.owners.length
              ? p.owners.map((o) => UI.esc(o.name || o.email) +
                  (o.isDisabled ? ' ' + UI.badge({ text: '已禁用', kind: 'danger' }) : '')
                ).join('、')
              : '<span class="muted">—</span>';
            return UI.tableRow([
              {
                html: UI.cellId({
                  title: UI.esc(p.name) +
                    (p.isPersonal ? ' ' + UI.badge({ text: '个人空间' }) : '') +
                    (p.isPublic ? ' ' + UI.badge({ text: '公开', kind: 'primary' }) : ''),
                  sub: p.description ? UI.esc(p.description) : '',
                }),
              },
              { html: ownerHtml },
              { html: UI.cellMeta(String(p.memberCount)) },
              { html: UI.cellMeta(String(p.docCount)) },
              { html: UI.cellMeta(p.lastUpdatedAt ? UI.fmtDate(p.lastUpdatedAt) : '—') },
              { html: UI.cellMeta(UI.fmtSize(p.storageBytes)) },
            ], {
              attrs: 'data-pid="' + UI.esc(p.id) + '"',
              acts: p.isPersonal ? '' :
                UI.iconBtn({ icon: 'team-line', title: '管理成员', cls: 'p-members' }) +
                UI.iconBtn({ icon: 'delete-bin-line', title: '删除项目', danger: true, cls: 'p-delete' }),
            });
          }).join('');
      } catch (e) {
        projEl.innerHTML = UI.errorBanner(e);
      }
    }

    // ---------- 用户管理 ----------

    async function load() {
      try {
        users = await api('/api/users') || [];
        if (!users.length) {
          body.innerHTML = UI.emptyHtml({ icon: 'user-line', title: '暂无用户' });
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
            [{ html: '用户' }, { html: 'ID' }, { html: '加入时间' }, { html: '最近登录' },
             { html: '角色' }, { html: '状态' }, { html: '' }],
            { tpl: TPL, cls: 'acts-static acts-5' }
          ) + users.map((u) =>
            UI.tableRow([
              {
                html: UI.cellId({
                  avatar: UI.avatar({ name: u.name || u.email, seed: u.id, size: 'lg', color: u.avatarColor }),
                  title: UI.esc(u.name),
                  sub: UI.esc(u.email),
                }),
              },
              { html: UI.cellMeta(String(u.id)) },
              { html: UI.cellMeta(UI.esc(UI.fmtDate(u.createdAt))) },
              {
                // 最近登录:时间 + 在线标记 + 来源 IP。来源信息只在管理端出现
                // (同事目录/成员列表都没有它);"在线"是服务端按最近活跃窗口算的近似值
                html: UI.cellId({
                  title: u.lastLoginAt
                    ? UI.esc(UI.fmtDateShort(u.lastLoginAt)) + ' ' +
                      (u.online ? UI.badge({ text: '在线', kind: 'success' })
                                : UI.badge({ text: '离线', cls: 'muted' }))
                    : '<span class="muted">从未登录</span>',
                  sub: u.lastLoginIp ? UI.esc(u.lastLoginIp) : '',
                }),
              },
              { html: u.isAdmin ? UI.badge({ text: '管理员', kind: 'primary' }) : UI.badge({ text: '成员' }) },
              {
                html: (u.isDisabled ? UI.badge({ text: '已禁用', kind: 'danger' })
                                    : UI.badge({ text: '正常', kind: 'success' })) +
                  (u.lockedUntil
                    ? ' ' + UI.badge({ text: '锁定至 ' + UI.fmtDateShort(u.lockedUntil),
                                       kind: 'danger', title: u.lockedUntil })
                    : ''),
              },
            ], {
              attrs: 'data-uid="' + UI.esc(u.id) + '"',
              acts:
                UI.iconBtn({ icon: 'edit-line', title: '编辑', cls: 'u-edit' }) +
                UI.iconBtn({ icon: 'key-2-line', title: '重置密码', cls: 'u-reset' }) +
                UI.iconBtn({ icon: 'shield-keyhole-line', title: '登录详情(会话 / 来源 / 记录)',
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
      } catch (e) {
        body.innerHTML = UI.errorBanner(e);
      }
    }

    function findUser(uid) { return users.find((u) => UI.sameId(u.id, uid)); }

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

    // ---------- 登录详情(登录限制 / 活跃会话 / 登录记录) ----------

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

    function wireAccess(root, u, reload) {
      const unlock = root.querySelector('#acc-unlock');
      if (unlock) unlock.addEventListener('click', async () => {
        try {
          await api('/api/users/' + u.id, { method: 'PATCH', body: { unlock: true } });
          UI.toast('已解除登录锁定', 'success');
          await Promise.all([reload(), load(), loadSecurity()]);
        } catch (e) { UI.err(e); }
      });
      root.querySelectorAll('.acc-kick').forEach((b) => b.addEventListener('click', async () => {
        try {
          await api('/api/admin/sessions/' + b.dataset.ref, { method: 'DELETE' });
          UI.toast('该会话已下线', 'success');
          await Promise.all([reload(), load(), loadSecurity()]);
        } catch (e) { UI.err(e); }
      }));
      const all = root.querySelector('#acc-kick-all');
      if (all) all.addEventListener('click', () => UI.confirmAction(
        '强制下线「' + (u.name || u.email) + '」的全部会话?该用户在所有设备上都需要重新登录。',
        { okText: '全部下线', okMsg: '已全部下线', danger: true },
        async () => {
          await api('/api/admin/users/' + u.id + '/sessions', { method: 'DELETE' });
          await Promise.all([reload(), load(), loadSecurity()]);
        }));
    }

    async function openAccess(u) {
      const m = UI.modal({
        title: '登录详情 · ' + (u.name || u.email), wide: true,
        body: UI.loadingRow(),
      });
      const paint = async () => {
        let d;
        try { d = await api('/api/admin/users/' + u.id + '/access'); }
        catch (e) { m.body.innerHTML = UI.errorBanner(e); return; }
        m.body.innerHTML = lockCard(d) + sessionsCard(d) + eventsCard(d);
        wireAccess(m.body, u, paint);
      };
      await paint();
    }

    // ---------- 登录动态(全站最近登录事件,含不存在账号的失败 = 撞库痕迹) ----------

    const SEC_TPL = 'minmax(0, 1fr) minmax(0, 1.2fr) 88px minmax(0, 1.4fr)';
    let secFilter = 'all';
    let secEvents = [];

    function secTable() {
      const rows = secFilter === 'fail' ? secEvents.filter((e) => e.result !== 'ok') : secEvents;
      if (!rows.length) return UI.emptyHtml({ icon: 'shield-keyhole-line', title: '暂无登录记录' });
      return UI.tableHead(
        [{ html: '时间' }, { html: '账号' }, { html: '结果' }, { html: '来源' }],
        { tpl: SEC_TPL }
      ) + rows.map((e) => UI.tableRow([
        { html: UI.cellMeta(UI.esc(UI.fmtDate(e.createdAt))) },
        // 账号不存在时把邮箱显示在标题上并标注出来 —— 那正是"有人在拿不存在的邮箱扫"的信号
        { html: e.userName
            ? UI.cellId({ title: UI.esc(e.userName), sub: UI.esc(e.email) })
            : UI.cellId({ title: UI.esc(e.email),
                          sub: '<span class="muted">账号不存在</span>' }) },
        { html: resultBadge(e.result) },
        { html: UI.cellId({ title: UI.esc(e.ip || '未知来源'),
                            sub: UI.esc(e.device || '未知设备') }) },
      ])).join('');
    }

    async function loadSecurity() {
      try {
        secEvents = await api('/api/admin/login-events?limit=100') || [];
      } catch (e) { secEl.innerHTML = UI.errorBanner(e); return; }
      secEl.innerHTML = UI.sectionTitle({
        title: '登录动态', icon: 'shield-keyhole-line',
        actions: UI.seg({ id: 'sec-filter', auto: true, active: secFilter,
                          items: [{ key: 'all', label: '全部' }, { key: 'fail', label: '仅失败' }] }),
      }) + '<div id="sec-body">' + secTable() + '</div>';
      UI.segWire(secEl.querySelector('#sec-filter'), (key) => {
        secFilter = key;
        secEl.querySelector('#sec-body').innerHTML = secTable();
      });
    }

    // 事件委托挂视图根容器:项目区在 #admin-projects、用户区在 #admin-body,
    // 挂 body 会漏掉项目区的点击。
    // #view 是持久节点(路由重绘只清子节点,清不掉监听器),故必须 App.onCleanup 注销 ——
    // 否则离开再回来,一次点击会执行两次(弹两个窗、发两次请求)
    const onAdminClick = async (e) => {
      if (e.target.closest('#btn-new-user')) { openNewUser(); return; }

      // 项目区行:接管与删除的入口。成员管理不在这里重做一套 —— 成员页对全局管理员
      // 完全可用(含授予 OWNER),管理后台只负责"发现 + 跳转"
      const prow = e.target.closest('.data-table-row[data-pid]');
      if (prow) {
        const p = projects.find((x) => UI.sameId(x.id, prow.dataset.pid));
        if (!p) return;
        if (e.target.closest('.p-members')) {
          location.hash = '#/p/' + p.id + '/members';
        } else if (e.target.closest('.p-delete')) {
          await UI.confirmAction(
            '删除「' + p.name + '」将同时删除其全部文档、文件与成员关系,且不可恢复。确定删除?',
            { okText: '删除', okMsg: '项目已删除' },
            async () => {
              await api('/api/projects/' + p.id, { method: 'DELETE' });
              await Promise.all([loadProjects(), loadStore()]); // 占用总览同步回落
            });
        }
        return;
      }

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
            await api('/api/users/' + u.id, {
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
          await api('/api/users/' + u.id, { method: 'PATCH', body: { password: pwd } });
          UI.toast('密码已重置', 'success');
        } catch (err) { UI.err(err); }
      } else if (e.target.closest('.u-delete')) {
        // 失败由 confirmAction 统一 UI.err 透出:服务端对"有数据的账号"会回 409 并说明原因,
        // 让管理员知道改用禁用
        await UI.confirmAction(
          '永久删除「' + u.email + '」?该账号从未产生任何文档、文件或项目,删除后不可恢复。',
          { okText: '删除', okMsg: '用户已删除' },
          async () => {
            await api('/api/users/' + u.id, { method: 'DELETE' });
            await load();
          });
      } else if (e.target.closest('.u-toggle')) {
        const disabling = !u.isDisabled;
        // 禁用前点名其唯一拥有的项目,防止制造"所有者失联"的死锁项目。
        // projects 由项目区加载,加载失败时为空 —— 只是少了提醒,不阻断禁用
        const soleOwned = disabling
          ? projects.filter((p) => !p.isPersonal && p.owners.length === 1 && p.owners[0].id === u.id)
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
          await api('/api/users/' + u.id, { method: 'PATCH', body: { isDisabled: disabling } });
          UI.toast(disabling ? '已禁用' : '已启用', 'success');
          await Promise.all([load(), loadProjects()]); // 项目区所有者的"已禁用"徽标要跟上
        } catch (err) { UI.err(err); }
      }
    };
    container.addEventListener('click', onAdminClick);
    App.onCleanup(() => container.removeEventListener('click', onAdminClick));

    await Promise.all([loadStore(), loadBackup(), loadProjects(), load(), loadSecurity()]);
  };
})();
