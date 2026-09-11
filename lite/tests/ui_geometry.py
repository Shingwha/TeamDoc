"""设计一致性几何断言:把"宽度/高度不统一"从观感变成可回归的数字。

用法(需先启动服务并 bootstrap 管理员):
    TD_BASE=http://127.0.0.1:8137 python tests/ui_geometry.py
    TD_PID=<项目id> TD_BASE=... python tests/ui_geometry.py   # 额外覆盖项目内页面

校验四类问题(每类都由一次真实缺陷驱动):
  1. 页宽:ss、内容块必须同宽(历史 bug:#view 不限宽,卡片各自限 720/560)
  2. 控件高度:输入框/下拉/按钮落在令牌档位,且同一容器内高度唯一
     (历史 bug:.input 无 height,靠 line-height 拼出 40.4px,与 40px 的 .btn 混排)
  3. 字段可见:输入框底色 != 模态框底色,且常驻 1px 描边
     (历史 bug:两者同用 --md-surface-container-high,未聚焦即隐形)
  4. 操作列:row-acts 实际内容宽 <= 声明的 --col-acts
     (历史 bug:文件行 6 个按钮 176px,列声明 168px,溢出 8px)

沿用 visual_sweep.py 的机制:注入临时校验页 + 无头 Chrome + 结果写进 DOM。
坑:临时页放在 web/ 下(才能用站内绝对路径引真实样式),用完必删;
    --virtual-time-budget 下 CSS 过渡不推进,故先注入 transition/animation: none。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
TMP_PAGE = "_ui_geometry.html"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# 期望的控件高度档(tokens.css 的 --ctl-l)
EXPECT_INPUT_H = 36
# 窗口必须宽于任何页宽上限,否则"页头与内容不同宽"根本不会显现(会被窄窗口掩盖)
WINDOW_SIZE = "1680,1000"

INJECT = r'''  <script>
    (function () {
      var x = new XMLHttpRequest();
      x.open('POST', '/api/auth/login', false);
      x.setRequestHeader('Content-Type', 'application/json');
      x.send(JSON.stringify({ email: 'admin@teamdoc.local', password: 'admin12345' }));
      window.__loginStatus = x.status;
    })();
    // 过渡在 --virtual-time-budget 下不推进,会把测量停在起始值;先整体关掉
    var __s = document.createElement('style');
    __s.textContent = '*,*::before,*::after{transition:none !important;animation:none !important}';
    document.head.appendChild(__s);
    window.__geo = { routes: {}, dialogs: {}, meta: {} };
    // 量一个 14px 中文字符的**自然行盒**高度,作为 select 垂直裁剪的判据基线
    // (与 --fs-body / .select 的 font-size 一致;不能写死 18)
    (function () {
      var p = document.createElement('span');
      p.textContent = '所有者';
      p.style.cssText = 'position:absolute;visibility:hidden;font-size:14px;line-height:normal;white-space:nowrap';
      document.body.appendChild(p);
      window.__geo.meta.naturalTextH = Math.round(p.getBoundingClientRect().height * 100) / 100;
      document.body.removeChild(p);
    })();
  </script>
  <script src="/js/app.js"></script>
  <script>
  (function () {
    var PARAMS = new URLSearchParams(location.search);
    var ROUTES = JSON.parse(PARAMS.get('routes') || '[]');
    var GEO = window.__geo;

    function rect(el) { return el.getBoundingClientRect(); }
    function w(el) { return Math.round(rect(el).width * 100) / 100; }
    function h(el) { return Math.round(rect(el).height * 100) / 100; }
    function cs(el, k) { return getComputedStyle(el)[k]; }
    function cv(el, k) { return getComputedStyle(el).getPropertyValue(k).trim(); }
    // 高度:过滤掉隐藏元素(display:none 实测为 0,会把"容器内唯一"误判成混排)
    function visHeights(els) {
      return els.map(function (e) { return h(e); }).filter(function (x) { return x > 1; });
    }
    function uniq(a) { return Array.from(new Set(a)).sort(function (x, y) { return x - y; }); }
    function all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

    function measureRoute() {
      var view = document.getElementById('view');
      var head = view.querySelector('.page-head');
      // 主体内容块 = #view 下除页头/包裹层外最宽的元素(取直接子元素的实测最大宽)
      var kids = all(':scope > *', view).filter(function (el) { return el !== head && !el.hidden; });
      var bodyW = kids.length ? Math.max.apply(null, kids.map(w)) : null;
      return {
        viewMaxW: cs(view, 'maxWidth'),
        viewW: w(view),
        headW: head ? w(head) : null,
        bodyW: bodyW,
        // 输入框只有两档:表单档 --ctl-l(36) 与行内档 --ctl-m(32)。
        // 行内档只允许出现在 .row-acts / .list-row-acts 里(行高 52 装不下 36)
        inputs: all('.input, .select', view).map(function (el) {
          return { h: h(el), inRow: !!el.closest('.row-acts, .list-row-acts') };
        }).filter(function (x) { return x.h > 1; }),
        // select 的垂直裁剪:内容盒高度必须 >= 文字的自然行盒。
        // 历史 bug:.select 沿用了 .input 的 8px 上下内边距,行内档 32px 减去它们只剩
        // 16px,而 14px 中文字符的自然行盒是 18px —— 文字上下各被裁 1px,
        // 表现为"成员行的角色下拉文字显示不全",而表单档(36px)侥幸没暴露。
        // 用 canvas 量真实字宽/行高,不能靠目测。
        selects: all('select', view).map(function (el) {
          var pt = parseFloat(cs(el, 'paddingTop')) || 0;
          var pb = parseFloat(cs(el, 'paddingBottom')) || 0;
          return {
            h: h(el),
            contentH: Math.round((h(el) - pt - pb) * 100) / 100,
            padT: pt, padB: pb,
            cls: el.className || el.id || 'select'
          };
        }).filter(function (x) { return x.h > 1; }),
        containers: {
          formInline: uniq(visHeights(all('.form-inline .input, .form-inline .select, .form-inline .btn', view))),
          rowActs: uniq(visHeights(all('.row-acts .btn, .row-acts .btn-icon', view))),
          listRowActs: uniq(visHeights(all('.list-row-acts .btn, .list-row-acts .btn-icon, .list-row-acts .select', view))),
          pageActions: uniq(visHeights(all('.page-actions .btn', view)))
        },
        // 操作列:实际按钮总宽 vs 该列**实测**宽度。
        // 不解析 CSS 变量(calc 未求值),直接量 .row-acts 自身 —— 它就是 grid 的末列单元,
        // 其宽度即布局解析后的真实列宽。
        // 也不能用 scrollWidth:块级子元素被 grid 列拉满,scrollWidth 恒等于列宽,
        // 溢出反而测不出来。故累加子元素宽度 + 间隙,得到真实"需要多宽"。
        acts: all('.row-acts, .list-row-acts', view).map(function (el) {
          var kids = Array.prototype.slice.call(el.children);
          var sumW = kids.reduce(function (s, k) { return s + k.getBoundingClientRect().width; }, 0);
          var gap = parseFloat(cs(el, 'columnGap')) || 0;
          var need = Math.ceil(sumW + gap * Math.max(0, kids.length - 1));
          return {
            n: kids.length,
            need: need,
            colW: Math.round(el.getBoundingClientRect().width * 100) / 100,
            childWs: uniq(kids.map(function (k) { return Math.round(k.getBoundingClientRect().width); }))
          };
        })
      };
    }

    function probeDialog(name, openFn, done) {
      openFn();
      setTimeout(function () {
        var box = document.querySelector('.modal-box');
        if (!box) { GEO.dialogs[name] = { error: 'modal not opened' }; done(); return; }
        // 模态框打开时会自动聚焦首个输入框;而"未聚焦是否可见"才是要测的缺陷。
        // 先 blur,再逐个量所有字段的底色。
        if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
        // textarea 天然是多行(2×--ctl-l = 72),不参与单行控件的高度断言,只量 input/select
        var single = all('.input, .select', box);
        var allFields = all('.input, .select, .textarea', box);
        var bgs = uniq(allFields.map(function (el) { return cs(el, 'backgroundColor'); }));
        var boxBg = cs(box, 'backgroundColor');
        var fields = all('.form-modal > .field', box);
        var lastField = fields[fields.length - 1];
        var inp = single[0] || allFields[0];
        GEO.dialogs[name] = {
          theme: document.documentElement.dataset.theme,
          boxBg: boxBg,
          inputBgs: bgs,
          // 任一字段与模态框同色 => 未聚焦时隐形
          sameBg: bgs.some(function (b) { return b === boxBg; }),
          inputBorderW: inp ? cs(inp, 'borderTopWidth') : null,
          inputH: inp ? h(inp) : null,
          inputHeights: uniq(visHeights(single)),
          fieldCount: fields.length,
          // checkbox 字段是否也包在 .field 里(历史 bug:裸 .check-row 直接贴上一个字段)
          bareCheckRows: all('.form-modal > .check-row', box).length,
          footBtnH: uniq(visHeights(all('.modal-foot .btn', box))),
          // 最后一个字段底缘到模态框底缘的距离
          lastFieldGap: lastField ? Math.round((rect(box).bottom - rect(lastField).bottom) * 100) / 100 : null
        };
        var closer = box.querySelector('.modal-head .btn-icon');
        if (closer) closer.click();
        setTimeout(done, 60);
      }, 420);
    }

    function runRoutes(i, done) {
      if (i >= ROUTES.length) { done(); return; }
      var r = ROUTES[i];
      location.hash = r.hash;
      window.dispatchEvent(new HashChangeEvent('hashchange'));
      setTimeout(function () {
        GEO.routes[r.name] = measureRoute();
        runRoutes(i + 1, done);
      }, 1300);
    }

    function setTheme(t, cb) {
      document.documentElement.dataset.theme = t;
      setTimeout(cb, 80);
    }

    function finish() {
      var el = document.createElement('pre');
      el.id = 'geo-out';
      el.textContent = JSON.stringify(GEO);
      document.body.appendChild(el);
    }

    window.addEventListener('load', function () {
      setTimeout(function () {
        runRoutes(0, function () {
          location.hash = '#/';
          window.dispatchEvent(new HashChangeEvent('hashchange'));
          setTimeout(function () {
            setTheme('light', function () {
              probeDialog('light', function () {
                var b = document.querySelector('#btn-new-proj');
                if (b) b.click();
              }, function () {
                setTheme('dark', function () {
                  probeDialog('dark', function () {
                    var b = document.querySelector('#btn-new-proj');
                    if (b) b.click();
                  }, function () {
                    setTheme('light', function () {
                      location.hash = '#/admin';
                      window.dispatchEvent(new HashChangeEvent('hashchange'));
                      setTimeout(function () {
                        // 分区级按钮应在其作用的分区容器内(#admin-body 的用户表),不在页面级页头
                        var nb = document.querySelector('#btn-new-user');
                        GEO.meta.newUserBtnInBody = !!(nb && nb.closest('#admin-body'));
                        GEO.meta.newUserBtnInPageHead = !!(nb && nb.closest('.page-head'));
                        GEO.meta.adminSectionTitles = document.querySelectorAll('#view .section-title').length;
                        probeDialog('newUser', function () {
                          if (nb) nb.click();
                        }, finish);
                      }, 1300);
                    });
                  });
                });
              });
            });
          }, 300);
        });
      }, 200);
    });
  })();
  </script>
'''


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def collect(chrome):
    src = open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read()
    marker = '  <script src="/js/app.js"></script>\n'
    assert marker in src, "index.html 结构变了:找不到 app.js 引用"
    page = os.path.join(WEB_DIR, TMP_PAGE)

    pid = os.environ.get("TD_PID", "")
    doc_id = os.environ.get("TD_DOC", "")
    routes = [
        {"name": "项目", "hash": "#/"},
        {"name": "发现", "hash": "#/discover"},
        {"name": "搜索", "hash": "#/search"},
        {"name": "个人设置", "hash": "#/settings"},
        {"name": "管理后台", "hash": "#/admin"},
    ]
    if pid:
        routes += [
            {"name": "成员", "hash": f"#/p/{pid}/members"},
            {"name": "回收站", "hash": f"#/p/{pid}/trash"},
            {"name": "项目设置", "hash": f"#/p/{pid}/settings"},
            {"name": "云空间", "hash": f"#/p/{pid}/files"},
            {"name": "文档", "hash": f"#/p/{pid}/docs"},
        ]

    with open(page, "w", encoding="utf-8") as fh:
        fh.write(src.replace(marker, INJECT))
    try:
        enc = urllib.parse.quote(json.dumps(routes, ensure_ascii=False), safe="")
        url = f"{BASE}/{TMP_PAGE}?routes={enc}"
        with tempfile.TemporaryDirectory() as td:
            dom = os.path.join(td, "dom.html")
            with open(dom, "w", encoding="utf-8") as out:
                subprocess.run(
                    [chrome, "--headless", "--disable-gpu", "--no-sandbox",
                     f"--window-size={WINDOW_SIZE}",
                     "--virtual-time-budget=90000", "--dump-dom", url],
                    stdout=out, stderr=subprocess.DEVNULL, timeout=300)
            content = open(dom, encoding="utf-8", errors="replace").read()
    finally:
        if os.path.exists(page):
            os.remove(page)

    i = content.find('<pre id="geo-out"')
    assert i >= 0, "未产出几何结果(页面可能整块崩了)"
    raw = content[i:content.find("</pre>", i)]
    raw = raw[raw.find(">") + 1:]
    import html as _html
    return json.loads(_html.unescape(raw))


def main():
    chrome = find_chrome()
    if not chrome:
        print("未找到 Chrome/Edge,跳过几何断言")
        return 0
    geo = collect(chrome)

    fails = []
    print("=== 1) 页宽:每页声明页宽,且只有两档;页头与内容同宽 ===")
    tiers = {}
    for name, r in geo["routes"].items():
        if r["headW"] is None:
            print(f"  SKIP  {name}(无页头)")
            continue
        # 每个页面都必须由页面层声明宽度上限(历史 bug:maxW=none 意味着铺满全屏)
        maxw = r["viewMaxW"]
        if maxw == "none":
            print(f"  FAIL  {name:6s} 页面未声明宽度上限(max-width: none),实测 {r['viewW']}")
            fails.append(f"页面未限宽:{name}")
        else:
            tiers.setdefault(maxw, []).append(name)
            print(f"  OK    {name:6s} 页宽上限={maxw} 实测={r['viewW']}")
        # 页头与同一页的内容块必须同宽(取内容块最大宽比对)
        if r["bodyW"] is not None and abs(r["headW"] - r["bodyW"]) >= 1:
            print(f"  FAIL  {name:6s} 页头 {r['headW']} != 内容 {r['bodyW']}")
            fails.append(f"页头内容不同宽:{name} {r['headW']}!={r['bodyW']}")
    if len(tiers) > 2:
        print(f"  FAIL  页宽档位有 {len(tiers)} 档(应 ≤2):{ {k: v for k, v in tiers.items()} }")
        fails.append(f"页宽档位过多:{list(tiers)}")
    else:
        print(f"  页宽档位 {len(tiers)} 档:" +
              "; ".join(f"{k} → {','.join(v)}" for k, v in tiers.items()))

    print("\n=== 2) 控件高度:输入框/按钮落在令牌档位,容器内唯一 ===")
    for name, r in geo["routes"].items():
        for key, label in (("formInline", "行内表单"), ("listRowActs", "列表行操作"),
                           ("rowActs", "表格行操作"), ("pageActions", "页头操作")):
            hs = r["containers"][key]
            if len(hs) <= 1:
                continue
            print(f"  FAIL  {name:6s} {label} 高度混排:{hs}")
            fails.append(f"高度混排:{name} {label} {hs}")
        for x in r["inputs"]:
            # 表单档 36;行内档 32 只允许在行内操作容器里(那里行高 52)
            ok = abs(x["h"] - EXPECT_INPUT_H) <= 0.6 or (x["inRow"] and abs(x["h"] - 32) <= 0.6)
            if not ok:
                where = "行内" if x["inRow"] else "表单"
                print(f"  FAIL  {name:6s} {where}输入框/下拉高 {x['h']} 不在档位上(应 36,行内应 32)")
                fails.append(f"输入框高:{name} {where} {x['h']}")
        # select 内容盒必须装得下文字的自然行盒,否则上下被裁(历史 bug:成员行
        # 角色下拉沿用 input 的 8px 上下内边距,32px 档下只剩 16px < 18px)
        for s in r.get("selects", []):
            if s["contentH"] + 0.5 < geo["meta"].get("naturalTextH", 18):
                print(f"  FAIL  {name:6s} 下拉文字被裁:{s['cls']} 盒高 {s['h']} "
                      f"上下内边距 {s['padT']}/{s['padB']} → 内容盒仅 {s['contentH']}"
                      f"(文字需 {geo['meta'].get('naturalTextH')})")
                fails.append(f"下拉文字被裁:{name} {s['cls']}")
            # select 的文字由浏览器在固定高度内垂直居中,上下内边距永远不该有
            elif s['padT'] != 0 or s['padB'] != 0:
                print(f"  FAIL  {name:6s} select 不该有上下内边距:{s['cls']} "
                      f"padT={s['padT']} padB={s['padB']}")
                fails.append(f"select 上下内边距:{name} {s['cls']}")
    print("  (无输出即全部通过)")

    print("\n=== 3) 字段可见:输入框底色 != 模态框底色,且常有 1px 描边 ===")
    for name, d in geo["dialogs"].items():
        if d.get("error"):
            print(f"  FAIL  {name}:{d['error']}")
            fails.append(f"模态框未打开:{name}")
            continue
        ok_bg = not d["sameBg"]
        ok_bw = d["inputBorderW"] == "1px"
        ok_h = all(abs(x - EXPECT_INPUT_H) <= 0.6 for x in d["inputHeights"])
        mark = "OK  " if (ok_bg and ok_bw and ok_h) else "FAIL"
        print(f"  {mark}  {name:8s} theme={d['theme']:5s} boxBg={d['boxBg']} "
              f"inputBgs={d['inputBgs']} border={d['inputBorderW']} inputH={d['inputHeights']}")
        if not ok_bg:
            fails.append(f"输入框与模态框同色:{name}")
        if not ok_bw:
            fails.append(f"输入框无描边:{name} border={d['inputBorderW']}")
        if not ok_h:
            fails.append(f"模态框内输入框高不对:{name} {d['inputHeights']}")
        if d["bareCheckRows"]:
            print(f"  FAIL  {name:8s} 有 {d['bareCheckRows']} 个 checkbox 字段未包 .field")
            fails.append(f"checkbox 字段未成组:{name}")

    print("\n=== 4) 操作列:按钮总宽 <= 该列实测宽度(不溢出) ===")
    checked = 0
    for name, r in geo["routes"].items():
        for a in r["acts"]:
            if a["colW"] <= 0:
                continue
            checked += 1
            ok = a["need"] <= a["colW"] + 0.5
            print(f"  {'OK  ' if ok else 'FAIL'}  {name:6s} n={a['n']} 需要={a['need']} "
                  f"列宽={a['colW']} 各按钮={a['childWs']}")
            if not ok:
                fails.append(f"操作列溢出:{name} n={a['n']} 需要{a['need']}>{a['colW']} 按钮{a['childWs']}")
    if not checked:
        print("  SKIP  (未取到表格行操作;可传 TD_PID 覆盖项目页)")

    print("\n=== 5) 分区级操作的位置:按钮应在其作用的容器内 ===")
    meta = geo["meta"]
    if meta.get("newUserBtnInPageHead"):
        print("  FAIL  新建用户仍在页面级页头(它的作用范围只是用户表)")
        fails.append("新建用户按钮位置:仍在页头")
    elif meta.get("newUserBtnInBody"):
        print("  OK    新建用户 位于 #admin-body(用户表所在的分区容器)")
    else:
        print("  FAIL  新建用户 按钮未找到")
        fails.append("新建用户按钮丢失")
    if meta.get("adminSectionTitles", 0) >= 2:
        print(f"  OK    管理后台有两个分区标题(存储 / 用户)")
    else:
        print(f"  FAIL  管理后台分区标题数={meta.get('adminSectionTitles')}(应 ≥2)")
        fails.append("管理后台缺少分区标题")

    print()
    if fails:
        print(f"共 {len(fails)} 项不一致:")
        for f in fails:
            print("  - " + f)
        return 1
    print("设计一致性断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
