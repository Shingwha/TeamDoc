"""「移动到…」的浏览器演练(真实点击,纯黑盒)。

为什么值得单开一个文件:文档移动是这轮补上的能力,而它的前端链路最长 ——
文档树「更多」→ 下拉菜单 → 共享选择器(move-target.js)→ 选项目/位置 → 服务端移动
→ 前端跳路由。任何一段断了,服务端测试(test_doc_move.py)都还是全绿,用户却点不动。

**点击用 dispatchEvent 派发真实 MouseEvent,不用 element.click()**:「更多」按钮的
处理器内部会再调一次 btn.click() 去展开菜单,而 HTMLElement.click() 带"click in
progress"重入保护 —— 用 click() 模拟时那次内层调用被浏览器忽略,菜单永远打不开,
演练会误报成功能坏了(真实鼠标点击不设这个标志,不受影响)。
"""
import urllib.parse

import pytest

from _chrome import dump_page, error_trap_js, find_chrome, login_js, read_report, temp_page

pytestmark = pytest.mark.browser

MOVE_INJECT = '''  <script>
    __LOGIN_JS__
    __ERRTRAP_JS__
  </script>
  <script src="/js/app.js"></script>
  <script>
  (function () {
    var OUT = {};
    var Q = new URLSearchParams(location.search);
    var FROM = Q.get('from'), TO = Q.get('to'), DOC = Q.get('doc');
    function fire(el) {
      el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    }
    function report() {
      var l = ['hash=' + location.hash, 'errors=' + (window.__errors.join(' | ') || 'none')];
      Object.keys(OUT).forEach(function (k) { l.push(k + '=' + OUT[k]); });
      var el = document.createElement('pre');
      el.id = 'sweep-out';
      el.textContent = l.join(String.fromCharCode(10));
      document.body.appendChild(el);
    }
    function menuItem(label) {
      var items = document.querySelectorAll('.menu .menu-item');
      for (var i = 0; i < items.length; i++) {
        if (items[i].textContent.indexOf(label) >= 0) return items[i];
      }
      return null;
    }
    function stepMove() {
      var more = document.querySelector('#doc-tree .doc-row[data-id="' + DOC + '"] .act-more');
      if (!more) { OUT.menuOpened = 'no-more-btn'; report(); return; }
      fire(more);
      setTimeout(function () {
        var item = menuItem('移动到');
        OUT.menuOpened = item ? 1 : 0;
        OUT.menuHasRename = menuItem('重命名') ? 1 : 0;
        if (!item) { report(); return; }
        fire(item);
        setTimeout(function () {
          var sel = document.querySelector('.modal-box #mv-proj');
          OUT.modalOpened = sel ? 1 : 0;
          OUT.dirOptions = document.querySelectorAll('.modal-box #mv-dir option').length;
          if (!sel) { report(); return; }
          // 换目标项目:目录选项随项目重载
          sel.value = TO;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          setTimeout(function () {
            OUT.dirOptionsAfterSwitch = document.querySelectorAll('.modal-box #mv-dir option').length;
            var btns = document.querySelectorAll('.modal-box .modal-foot button');
            var move = null;
            for (var i = 0; i < btns.length; i++) { if (btns[i].textContent.indexOf('移动') >= 0) move = btns[i]; }
            if (!move) { OUT.moveBtn = 0; report(); return; }
            OUT.moveBtn = 1;
            fire(move);
            setTimeout(function () {
              OUT.modalClosed = document.querySelector('.modal-box') ? 0 : 1;
              // 跨项目后前端应自动跳到目标项目:等路由与树都落定再断言
              setTimeout(function () {
                OUT.hashAfter = location.hash;
                OUT.rowInNewProject = document.querySelector(
                  '#doc-tree .doc-row[data-id="' + DOC + '"]') ? 1 : 0;
                report();
              }, 2400);
            }, 1200);
          }, 1400);
        }, 800);
      }, 600);
    }
    window.addEventListener('load', function () {
      setTimeout(function () {
        location.hash = '#/p/' + FROM + '/docs/' + DOC;
        window.dispatchEvent(new HashChangeEvent('hashchange'));
        setTimeout(stepMove, 2600);
      }, 150);
    });
  })();
  </script>
'''


def test_doc_move_through_ui(base_url, admin):
    """真实点击:文档树 → 移动到… → 换项目 → 移动,断言路由与树都跟着换。"""
    chrome = find_chrome()
    if not chrome:
        pytest.skip("未找到 Chrome/Edge,跳过浏览器演练")

    src = admin.post("/api/projects", {"name": "移动演练源", "description": "d"}).data["id"]
    dst = admin.post("/api/projects", {"name": "移动演练目标", "description": "d"}).data["id"]
    doc = admin.post(f"/api/projects/{src}/docs", {"title": "要搬的文档"}).data
    admin.put(f"/api/docs/{doc['id']}/content",
              {"content": "# 正文\n\n搬走之后附件留在源项目。", "baseVersion": doc["version"]})

    inject = MOVE_INJECT.replace("__LOGIN_JS__", login_js()).replace("__ERRTRAP_JS__", error_trap_js())
    params = f"from={src}&to={dst}&doc={urllib.parse.quote(doc['id'], safe='')}"
    with temp_page("_doc_move.html", inject) as page_name:
        info = read_report(dump_page(chrome, base_url, page_name, params, budget=40000))

    assert info is not None, "未产出演练结果(页面可能整块崩了)"
    assert info.get("errors", "?") == "none", f"演练页 JS 错误: {info.get('errors')}"
    assert info.get("menuOpened") == "1", f"文档树「更多」没弹出菜单: {info}"
    assert info.get("menuHasRename") == "1", f"菜单项不完整: {info}"
    assert info.get("modalOpened") == "1", f"「移动到…」没打开选择器: {info}"
    assert info.get("moveBtn") == "1", f"弹窗里没有「移动」按钮: {info}"
    assert info.get("modalClosed") == "1", f"移动后弹窗没关(可能报了错): {info}"
    # 跨项目移动后必须跳到新项目 —— 留在旧路由上会立刻 404
    assert info.get("hashAfter", "").startswith(f"#/p/{dst}/docs/{doc['id']}"), \
        f"移动后没有跳到目标项目: {info.get('hashAfter')}"
    assert info.get("rowInNewProject") == "1", "目标项目的文档树里没有这篇文档"

    # 服务端真相:归属已改,源项目树里不再有它
    assert admin.get(f"/api/docs/{doc['id']}").data["projectId"] == dst
    src_titles = [n["title"] for n in (admin.get(f"/api/projects/{src}/docs/tree").data or [])]
    assert "要搬的文档" not in src_titles, src_titles
    dst_titles = [n["title"] for n in (admin.get(f"/api/projects/{dst}/docs/tree").data or [])]
    assert "要搬的文档" in dst_titles, dst_titles
    admin.delete(f"/api/projects/{src}")
    admin.delete(f"/api/projects/{dst}")
