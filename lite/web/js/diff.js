// diff.js — 行级差异 + 逐块合并(文档内容冲突弹窗用;零依赖,不引 diff 库)
//   var d = Diff.build(mine, theirs);
//   d.count                 // 变更块数(= git 眼里的冲突块数)
//   d.render(picks)         // HTML:上下文行 + 每块一张带三选的卡片
//   d.merge(picks)          // 按选择拼出的合并结果文本
//   d.applyPicks(el, picks) // 把选择反映到已渲染的 DOM(只改类名,不重建 → 不丢滚动位置)
//
//   picks: { 块序号: 'mine' | 'theirs' | 'both' },缺省 'mine'
//
//   语义:mine = 你手里的正文,theirs = 服务端当前正文。按"采纳服务端版本会怎样"着色:
//   你的版本独有的行标 df-del,服务端独有的行标 df-add,两侧相同的行标 df-ctx。
//
//   为什么是行级:正文是 Markdown 源码,行是它天然的编辑单位;字符级差异在"保留哪边"
//   这个决策上不提供额外信息,却要多一个数量级的代码与调参。
//
//   **数据与视图分离**:行表是完整的(含所有未变行),渲染时才把长上下文折成 ⋯。
//   合并必须基于完整行表 —— 渲染层的折叠会把内容丢掉,那样拼出来的结果就是错的。
//
//   规模是有界的:先剪公共前后缀(实际改动通常只占几行)再对中段做 LCS;中段仍超限时
//   退化成"整段一块"(粗粒度但可合并);渲染行数另有上限,且只影响观感、不影响合并。
window.Diff = (function () {
  'use strict';

  var CTX = 3;         // 变更块上下保留的上下文行数
  var MAX_MID = 1500;  // 剪掉公共前后缀后,允许做 LCS 的单侧行数上限
  var MAX_ROWS = 600;  // 渲染行数上限

  /** 切成行数组;结尾换行单独记标志 —— 合并时要靠它原样还原两侧的字节,
   *  否则"全部用服务端"拼出来的结果会比服务端少一个换行,白白多写一次库。 */
  function split(s) {
    var t = (s == null ? '' : String(s)).replace(/\r\n/g, '\n');
    var endsNl = /\n$/.test(t);
    if (endsNl) t = t.slice(0, -1);
    return { lines: t.split('\n'), endsNl: endsNl };
  }

  /** 公共前后缀长度:先把没变的部分剪掉,LCS 的规模就只剩真正改动的部分 */
  function trimCommon(a, b) {
    var p = 0;
    while (p < a.length && p < b.length && a[p] === b[p]) p++;
    var s = 0;
    while (s < a.length - p && s < b.length - p &&
           a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
    return { p: p, s: s };
  }

  /** 行级 LCS 编辑脚本 → [{t:'ctx'|'del'|'add', a:i|null, b:j|null}] */
  function script(a, b) {
    var n = a.length, m = b.length, i, j;
    // dp[i][j] = a[i:] 与 b[j:] 的最长公共子序列长度
    var dp = [];
    for (i = 0; i <= n; i++) dp.push(new Int32Array(m + 1));
    for (i = n - 1; i >= 0; i--) {
      for (j = m - 1; j >= 0; j--) {
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1
                                 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    var out = [];
    i = 0; j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) { out.push({ t: 'ctx', a: i, b: j }); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: 'del', a: i, b: null }); i++; }
      else { out.push({ t: 'add', a: null, b: j }); j++; }
    }
    while (i < n) { out.push({ t: 'del', a: i, b: null }); i++; }
    while (j < m) { out.push({ t: 'add', a: null, b: j }); j++; }
    return out;
  }

  /**
   * 编辑脚本 → 行表。每项是:
   *   {t:'ctx', a:i, b:j}                     两侧相同,不参与选择
   *   {t:'hunk', id:n, del:[i..], add:[j..]}  一块变更 = git 的一个冲突块
   */
  function toRows(a, b) {
    var t = trimCommon(a, b);
    var midA = a.slice(t.p, a.length - t.s), midB = b.slice(t.p, b.length - t.s);
    var rows = [], hunks = [], cur = null, i;

    function flush() {
      if (!cur) return;
      cur.id = hunks.length;
      hunks.push(cur);
      rows.push({ t: 'hunk', id: cur.id, del: cur.del, add: cur.add });
      cur = null;
    }

    for (i = 0; i < t.p; i++) rows.push({ t: 'ctx', a: i, b: i });
    if (midA.length > MAX_MID || midB.length > MAX_MID) {
      // 粗粒度退化:差异段太大,逐行 LCS 既慢也没有可读性 → 整段作为一块
      cur = { del: [], add: [] };
      for (i = 0; i < midA.length; i++) cur.del.push(t.p + i);
      for (i = 0; i < midB.length; i++) cur.add.push(t.p + i);
      flush();
    } else {
      var mid = script(midA, midB);
      for (i = 0; i < mid.length; i++) {
        var r = mid[i];
        if (r.t === 'ctx') {
          flush();
          rows.push({ t: 'ctx', a: r.a + t.p, b: r.b + t.p });
        } else {
          if (!cur) cur = { del: [], add: [] };
          if (r.t === 'del') cur.del.push(r.a + t.p); else cur.add.push(r.b + t.p);
        }
      }
      flush();
    }
    for (i = t.s; i > 0; i--) rows.push({ t: 'ctx', a: a.length - i, b: b.length - i });
    return { rows: rows, hunks: hunks };
  }

  function pickOf(picks, id) {
    var p = picks && picks[id];
    return p === 'theirs' || p === 'both' ? p : 'mine';
  }

  function rowHtml(kind, text, noA, noB) {
    var mark = kind === 'del' ? '-' : (kind === 'add' ? '+' : ' ');
    return '<div class="df-row df-' + kind + '">' +
      '<span class="df-no">' + (noA == null ? '' : noA + 1) + '</span>' +
      '<span class="df-no">' + (noB == null ? '' : noB + 1) + '</span>' +
      '<span class="df-txt"><span class="df-mark">' + mark + '</span>' +
      UI.esc(text == null ? '' : text) + '</span>' +
      '</div>';
  }

  function hunkHtml(row, picks, a, b) {
    var pick = pickOf(picks, row.id);
    var html = '<div class="df-hunk df-pick-' + pick + '" data-hunk="' + row.id + '">' +
      '<div class="df-hunk-head"><span class="df-hunk-no">第 ' + (row.id + 1) + ' 处</span>' +
      UI.seg({
        id: 'df-seg-' + row.id,
        auto: true,
        active: pick,
        items: [
          { key: 'mine', label: '我的' },
          { key: 'theirs', label: '服务端' },
          { key: 'both', label: '两者保留' },
        ],
      }) + '</div>';
    var i;
    for (i = 0; i < row.del.length; i++) html += rowHtml('del', a[row.del[i]], row.del[i], null);
    for (i = 0; i < row.add.length; i++) html += rowHtml('add', b[row.add[i]], null, row.add[i]);
    return html + '</div>';
  }

  /** @returns {{count:number, rows:Array, render:Function, merge:Function, applyPicks:Function}} */
  function build(mine, theirs) {
    var sa = split(mine), sb = split(theirs);
    var a = sa.lines, b = sb.lines;
    var model = toRows(a, b);
    var rows = model.rows;

    function merge(picks) {
      var out = [], lastFrom = null, i, k;
      for (i = 0; i < rows.length; i++) {
        var row = rows[i];
        if (row.t === 'ctx') { out.push(a[row.a]); lastFrom = 'a'; continue; }
        var pick = pickOf(picks, row.id);
        if (pick === 'mine' || pick === 'both') {
          for (k = 0; k < row.del.length; k++) { out.push(a[row.del[k]]); lastFrom = 'a'; }
        }
        if (pick === 'theirs' || pick === 'both') {
          for (k = 0; k < row.add.length; k++) { out.push(b[row.add[k]]); lastFrom = 'b'; }
        }
      }
      var text = out.join('\n');
      // 结尾换行跟随"最后一行来自哪一侧",这样全选一边时结果与那一侧逐字节相同
      var src = lastFrom === 'b' ? sb : sa;
      return text + (src.endsNl ? '\n' : '');
    }

    function render(picks) {
      // 先算哪些上下文行要保留(每块上下各 CTX 行),被折掉的位置渲染成一行 ⋯
      var keep = [], i, k, gap = false, n = 0;
      for (i = 0; i < rows.length; i++) keep.push(rows[i].t === 'hunk');
      for (i = 0; i < rows.length; i++) {
        if (rows[i].t !== 'hunk') continue;
        for (k = Math.max(0, i - CTX); k <= Math.min(rows.length - 1, i + CTX); k++) keep[k] = true;
      }
      var html = '<div class="df-wrap">';
      for (i = 0; i < rows.length; i++) {
        if (n >= MAX_ROWS) { html += '<div class="df-row df-gap">差异过多,仅显示前面的部分(合并仍按完整内容进行)</div>'; break; }
        var row = rows[i];
        if (!keep[i]) {
          if (!gap) { html += '<div class="df-row df-gap">⋯</div>'; n++; gap = true; }
          continue;
        }
        gap = false;
        if (row.t === 'ctx') { html += rowHtml('ctx', a[row.a], row.a, row.b); n++; }
        else { html += hunkHtml(row, picks, a, b); n += row.del.length + row.add.length + 1; }
      }
      return html + '</div>';
    }

    /** 把选择反映到已渲染的 DOM。**不重建** —— 重建会丢滚动位置,而"我在看第几处"
     *  正是这个界面唯一要保持的状态。 */
    function applyPicks(container, picks) {
      var els = container.querySelectorAll('.df-hunk');
      for (var i = 0; i < els.length; i++) {
        var el = els[i], pick = pickOf(picks, el.dataset.hunk);
        el.className = 'df-hunk df-pick-' + pick;
        var segEl = el.querySelector('.seg');
        if (segEl) UI.segSet(segEl, pick);
      }
    }

    return { count: model.hunks.length, rows: rows, render: render, merge: merge,
             applyPicks: applyPicks };
  }

  return { build: build };
})();
