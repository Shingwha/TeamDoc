// views/admin/backup.js — 管理后台·备份与恢复区(目标目录状态 / 立即备份 /
// 下载备份 / 上传-校验-重启生效的恢复流水线)。与备份共用一张卡:它们本是同一条
// 流水线的两端,拆成两个 section 只会让页面变长、标题重复。
window.AdminSections = window.AdminSections || {};
(function () {
  'use strict';

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

  window.AdminSections.backup = function (ctx) {
    const el = ctx.el;

    function load() {
      // 两个状态源一次渲染(同一张卡);loadInto 负责失败横幅
      return UI.loadInto(el, () => Promise.all([
        api('/api/admin/backup/status'),
        api('/api/admin/restore/status'),
      ]), { render: (pair) => backupHtml(pair[0], pair[1]) });
    }

    el.addEventListener('click', async (e) => {
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
          await load();
        } catch (err) { UI.err(err); } finally { btn.disabled = false; }
      } else if (e.target.closest('#restore-pick')) {
        const input = el.querySelector('#restore-file');
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
          await load();
        } catch (err) { UI.err(err); }
      } else if (e.target.closest('#restore-cancel')) {
        const ok = await UI.confirmDialog('放弃这次恢复?暂存的备份会被删除,当前数据不受影响。',
          { okText: '放弃' });
        if (!ok) return;
        try {
          await api('/api/admin/restore', { method: 'DELETE' });
          UI.toast('已取消恢复', 'info');
          await load();
        } catch (err) { UI.err(err); }
      }
    });

    // 文件选择:选中即上传(不额外加"上传"按钮 —— 选文件本身就是明确的动作)
    el.addEventListener('change', async (e) => {
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
        await load();
      }
    });

    return { key: 'backup', load };
  };
})();
