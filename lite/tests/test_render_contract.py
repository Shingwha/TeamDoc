"""渲染契约的机器化(ARCHITECTURE.md「渲染契约」节的对应测试)。

为什么要单独有这一份:切文档时"整页重渲染"是**没有报错、页面也照常能用**的那类缺陷 ——
既不会让巡检变红,也不会让谁点不动。只有盯住"节点还是不是原来那个""有没有重新发请求"
才抓得到。前两个断言是静态的(永远会跑),后面的用真实浏览器点一遍。

反过来说,这也是这次重构的验收口径:切换文档只该重建编辑区那一层,其余(DOM、折叠态、
项目对象、文档树)必须原样留着。
"""
import re
from pathlib import Path

import pytest

from _chrome import dump_page, error_trap_js, find_chrome, login_js, read_report, temp_page
from _harness import WEB_DIR

JS_DIR = WEB_DIR / "js"

# 巡检页:登录 → 记录所有 /api 请求(补丁必须在 app.js 之前生效) → 真实点击切文档 →
# 报告"节点是否还是原来那个""期间有没有重新取项目/文档树"。
PROBE_INJECT = '''  <script>
    __LOGIN_JS__
    __ERRTRAP_JS__
    window.__reqs = [];
    (function () {
      var fetch0 = window.fetch;
      window.fetch = function (u) {
        var url = String(u && u.url ? u.url : u);
        if (url.indexOf('/api/') >= 0) window.__reqs.push(url.replace(/^[^/]*\\/\\/[^/]+/, ''));
        return fetch0.apply(this, arguments);
      };
    })();
  </script>
  <script src="/js/app.js"></script>
  <script>
  (function () {
    var OUT = {};
    var Q = new URLSearchParams(location.search);
    var PID = Q.get('pid'), D1 = Q.get('d1'), D2 = Q.get('d2');
    function report() {
      var l = ['hash=' + location.hash, 'errors=' + (window.__errors.join(' | ') || 'none')];
      Object.keys(OUT).forEach(function (k) { l.push(k + '=' + OUT[k]); });
      var el = document.createElement('pre');
      el.id = 'sweep-out';
      el.textContent = l.join(String.fromCharCode(10));
      document.body.appendChild(el);
    }
    // 给节点打探针:重渲染会换掉节点,探针随之消失 —— 这就是"有没有重建"的证据
    function probe(sel) {
      var el = document.querySelector(sel);
      if (el) el.setAttribute('data-probe', '1');
      return !!el;
    }
    function fetchSince(n) {
      return window.__reqs.slice(n).filter(function (u) { return u.indexOf('/api/projects/') >= 0; });
    }
    // 项目对象:切模块时不该重新取(项目层在这一跳里被复用)。
    // 文档树不在此列 —— 离开文档模块再回来是一次真正的重建(数据生命期 = 区域生存期)
    var FACT = ['/api/projects/' + PID];
    function factsSince(n) {
      return window.__reqs.slice(n).filter(function (u) { return FACT.indexOf(u) >= 0; });
    }
    window.addEventListener('load', function () {
      setTimeout(function () {
        location.hash = '#/p/' + PID + '/docs/' + D1;
        setTimeout(function () {
          OUT.sideProbed = probe('#side-projects') ? 1 : 0;
          OUT.treeProbed = probe('#doc-tree') ? 1 : 0;
          // 先折叠一个节点:切文档后这个折叠态必须还在(否则等于整树重建过)
          var caret = document.querySelector('#doc-tree .tree-caret:not(.leaf)');
          if (caret) caret.click();
          OUT.collapsedBefore = document.querySelector('#doc-tree .tree-children[hidden]') ? 1 : 0;
          // 记下基线,然后点另一篇文档(真实用户动作:点树行)
          var base = window.__reqs.length;
          var row = document.querySelector('#doc-tree .doc-row[data-id="' + D2 + '"]');
          OUT.targetFound = row ? 1 : 0;
          if (!row) { report(); return; }
          row.click();
          setTimeout(function () {
            OUT.treeSame = document.querySelector('#doc-tree[data-probe]') ? 1 : 0;
            OUT.sideSame = document.querySelector('#side-projects[data-probe]') ? 1 : 0;
            OUT.refetched = fetchSince(base).join(',') || 'none';
            OUT.collapsedKept = document.querySelector('#doc-tree .tree-children[hidden]') ? 1 : 0;
            // 复用的父层要跟着子层换人:树上高亮行必须移到刚点的那一篇
            var act = document.querySelector('#doc-tree .doc-row.active');
            OUT.activeRow = act ? act.dataset.id : '(无)';
            var t = document.querySelector('#doc-title');
            OUT.editorTitle = t ? t.value : '(无标题框)';
            OUT.editorBody = document.querySelector('#md-source, #md-preview') ? 1 : 0;
            // 再切一次功能(文档 → 云空间 → 文档):侧栏与文档树节点都不该被换掉,
            // 项目对象与文档树(也)不该重新取 —— 项目层在这一跳里被复用
            var base2 = window.__reqs.length;
            location.hash = '#/p/' + PID + '/files';
            setTimeout(function () {
              location.hash = '#/p/' + PID + '/docs/' + D1;
              setTimeout(function () {
                OUT.sideSameAfterTab = document.querySelector('#side-projects[data-probe]') ? 1 : 0;
                // 反过来的一面:文档模块离开过就重建(不做跨模块 keep-alive),所以
                // 探针不该还在 —— 这一条钉的是"作用域即生命期",不是"越省越好"
                OUT.docsTreeRecreatedAfterTab = document.querySelector('#doc-tree[data-probe]') ? 0 : 1;
                OUT.tabRefetch = factsSince(base2).join(',') || 'none';
                report();
              }, 1500);
            }, 1500);
          }, 1500);
        }, 2600);
      }, 150);
    });
  })();
  </script>
'''


def test_view_container_is_owned_by_the_router():
    """#view 只许路由层碰:谁都能清空它,就又回到"整页销毁重建" """
    offenders = []
    for path in sorted(JS_DIR.rglob("*.js")):
        if path.name in ("app.js", "scopes.js"):
            continue
        if re.search(r"getElementById\(\s*['\"]view['\"]", path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(WEB_DIR).as_posix())
    assert not offenders, "#view 只能在 app.js / scopes.js 里取:\n" + "\n".join("  - " + o for o in offenders)


def test_view_dispatch_only_in_route_table():
    """视图分派只在 route.js 的路由表里:app.js 不得出现 Views."""
    app_text = (JS_DIR / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"\bViews\.", app_text), "视图分派应集中在 route.js 的路由表,app.js 里不许出现 Views."


def test_page_mode_is_a_route_fact():
    """页面模式(满高 / 窄页)是路由表的事实:视图不得自己往容器上加类"""
    offenders = []
    for path in sorted((JS_DIR / "views").rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        for cls in ("view-fill", "page-narrow"):
            if cls in text:
                offenders.append(f"{path.relative_to(WEB_DIR).as_posix()}: {cls}")
    assert not offenders, "页面模式类只许出现在 route.js 的模式映射里:\n" + \
        "\n".join("  - " + o for o in offenders)


@pytest.mark.browser
def test_doc_switch_reuses_everything_but_the_editor(base_url, admin):
    """切文档:只有编辑区该重建 —— 侧栏与文档树的节点、折叠态必须原样留着,
    而且不许重新取项目对象与文档树(那是"整页重渲染"的指纹)。"""
    chrome = find_chrome()
    if not chrome:
        pytest.skip("未找到 Chrome/Edge,跳过渲染契约巡检")

    r = admin.post("/api/projects", {"name": "渲染契约项目", "description": "切文档复用断言"})
    assert r.status == 200, f"建项目失败: {r.data}"
    pid = r.data["id"]
    d1 = admin.post(f"/api/projects/{pid}/docs", {"title": "契约文档甲"})
    d2 = admin.post(f"/api/projects/{pid}/docs",
                    {"title": "契约文档乙", "parentId": d1.data["id"]})
    assert d1.status == 200 and d2.status == 200, f"建文档失败: {d1.data} {d2.data}"

    try:
        query = f"pid={pid}&d1={d1.data['id']}&d2={d2.data['id']}"
        inject = PROBE_INJECT.replace("__LOGIN_JS__", login_js()).replace("__ERRTRAP_JS__", error_trap_js())
        with temp_page("_render_contract.html", inject) as page_name:
            info = read_report(dump_page(chrome, base_url, page_name, query, budget=30000))
    finally:
        admin.delete(f"/api/projects/{pid}")

    assert info is not None, "未产出巡检结果(页面可能整块崩了)"
    assert info.get("errors", "?") == "none", f"JS 错误: {info.get('errors')}"
    assert info.get("sideProbed") == "1" and info.get("treeProbed") == "1", \
        f"探针没打上(侧栏或文档树没渲染): {info}"
    assert info.get("targetFound") == "1", f"没找到要切的第二篇文档: {info}"

    problems = []
    if info.get("treeSame") != "1":
        problems.append("切换文档后文档树被重建了(节点不是原来那个)")
    if info.get("sideSame") != "1":
        problems.append("切换文档后侧栏被重建了(节点不是原来那个)")
    if info.get("sideSameAfterTab") != "1":
        problems.append("切模块往返后侧栏被重建了(节点不是原来那个)")
    if info.get("docsTreeRecreatedAfterTab") != "1":
        problems.append("文档模块离开过却没重建(多出了跨模块的残留状态)")
    if info.get("tabRefetch") != "none":
        problems.append(f"切模块时重新取了项目对象: {info.get('tabRefetch')}")
    if info.get("refetched") != "none":
        problems.append(f"切换文档时重新取了项目对象/文档树: {info.get('refetched')}")
    if info.get("collapsedBefore") == "1" and info.get("collapsedKept") != "1":
        problems.append("切换文档后文档树的折叠态丢了(说明整树重绘过)")
    if info.get("activeRow") != str(d2.data["id"]):
        problems.append(f"文档树的高亮行没跟着切: {info.get('activeRow')}")
    if info.get("editorTitle") != "契约文档乙":
        problems.append(f"编辑区没有换成第二篇文档: title={info.get('editorTitle')!r}")
    if info.get("editorBody") != "1":
        problems.append("编辑区正文区没渲染出来")
    assert not problems, "渲染契约失败:\n" + "\n".join("  - " + p for p in problems)
