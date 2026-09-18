// route.js — 路由文法:hash ↔ 路由事实,以及"路由 → 作用域"的唯一分派处
//
// 三件事,各只有一处:
//   Route.resolve(hash)  纯解析:返回作用域描述(或重定向)。重定向/坏 id 在**碰 DOM
//                        之前**判定完 —— 先清空页面再跳转,用户就会看到一帧空白
//   Route.NAV            项目内模块导航(侧栏子项 / 模块分派 / tab 记忆白名单共用)
//   Route.project(...)   '#/p/…' 的唯一构造点(视图不手拼路由串)
//
// Route.current() 是"当前路由事实"的唯一来源。作用域**不要**把路由快照进闭包:
// 复用的层不会重建,闭包里的 docId 会停留在第一次的值。规矩是 —— 闭包只留常量
// (pid、tab 这类在自己 key 里的东西),更深的路由片段在用到的那一刻现读。
window.Route = (function () {
  'use strict';

  // 页面模式:宿主类名只在映射表里出现一次(视图不得自己加类)
  const MODE_CLASS = { fill: 'view-fill', narrow: 'page-narrow' };

  /**
   * 项目内模块导航。新增模块只需在此加一项:侧栏子项、路由分派、tab 记忆校验共用它。
   *   level(r)    模块的作用域工厂;r = 当前路由事实(+ 由项目层补上的 proj)
   *   mode        页面模式:'fill' 满高填充 / 'narrow' 窄页 / 缺省默认页宽
   *   visible(p)  该项目下是否显示(输入可能为 null = 项目对象还没到手)
   */
  const NAV = [
    { key: 'docs', icon: 'file-text-line', label: '文档', mode: 'fill',
      level: function (r) { return Views.docsLevel(r); }, visible: function () { return true; } },
    { key: 'files', icon: 'folder-line', label: '云空间',
      level: function (r) { return Views.filesLevel(r); }, visible: function () { return true; } },
    { key: 'members', icon: 'team-line', label: '成员', mode: 'narrow',
      level: function (r) { return Views.membersLevel(r); },
      visible: function (p) { return !p || !p.isPersonal; } },
    // 回收站含"删了什么"这类项目内部信息,只对真成员与全局管理员显示
    // (两者之外的人服务端会 403,这里提前隐藏,不留一个点了报错的 tab)
    { key: 'trash', icon: 'delete-bin-line', label: '回收站', mode: 'narrow',
      level: function (r) { return Views.trashLevel(r); },
      visible: function (p) { return !p || UI.canRead(p); } },
    { key: 'settings', icon: 'settings-4-line', label: '设置', mode: 'narrow',
      level: function (r) { return Views.projectSettingsLevel(r); },
      visible: function () { return true; } },
  ];
  const NAV_KEYS = NAV.map(function (n) { return n.key; });
  const TAB_KEY = 'td:lastTab:';

  /** 解析 hash → 路由事实。query 只在这里构造一次,消费方读 r.query */
  function parse(hash) {
    const h = String(hash || '').replace(/^#/, '') || '/';
    const qi = h.indexOf('?');
    const path = qi >= 0 ? h.slice(0, qi) : h;
    return {
      hash: h,
      segs: path.split('/').filter(Boolean),
      qs: qi >= 0 ? h.slice(qi + 1) : '',
      query: new URLSearchParams(qi >= 0 ? h.slice(qi + 1) : ''),
    };
  }

  /** '#/p/…' 的唯一构造点(视图/视图外都从这里取串,免得 esc 口径各写各的) */
  function project(pid, tab, docId, query) {
    let h = '#/p/' + encodeURIComponent(pid);
    if (tab) h += '/' + encodeURIComponent(tab);
    if (docId != null) h += '/' + encodeURIComponent(docId);
    if (query) {
      const qs = Object.keys(query)
        .filter(function (k) { return query[k] != null; })
        .map(function (k) { return k + '=' + encodeURIComponent(query[k]); }).join('&');
      if (qs) h += '?' + qs;
    }
    return h;
  }

  /** 项目路失效时落回哪一页:上次访问的模块(白名单校验,缺省文档) */
  function lastTab(pid) {
    const tab = UI.pref.get(TAB_KEY + pid, 'docs');
    return NAV_KEYS.indexOf(tab) < 0 ? 'docs' : tab;
  }

  function navOf(key) {
    return NAV.find(function (n) { return n.key === key; }) || null;
  }

  /**
   * 路由 → 作用域描述。返回 {spec, hostClass, route} 或 {redirect, notice} 或 {login:true}。
   * 纯函数:不碰 DOM、不写存储(记住 tab 由调用方按 route 写)。
   */
  function resolve(hash) {
    const r = parse(hash);
    const segs = r.segs;

    if (segs[0] === 'login') return { login: true };
    if (!segs.length) return { spec: Views.projectsLevel(r), route: r };
    if (segs[0] === 'discover') return { spec: Views.discoverLevel(r), route: r };
    if (segs[0] === 'search') {
      return { spec: Views.searchLevel(r), hostClass: MODE_CLASS.narrow, route: r };
    }
    if (segs[0] === 'settings') {
      return { spec: Views.settingsLevel(r), hostClass: MODE_CLASS.narrow, route: r };
    }
    if (segs[0] === 'admin') return { spec: Views.adminLevel(r), route: r };

    if (segs[0] !== 'p' || !segs[1]) return { redirect: '#/' };
    // 路由边界只做形状判定:形状不对 = 这条链接坏了(旧书签、手工改短的地址),
    // 给一句可见的提示再回列表页 —— 静默回退会让人以为"点了没反应"
    const pid = UI.idOf(segs[1]);
    if (!pid) return { redirect: '#/', notice: '链接里的项目 id 无效,已回到项目列表' };
    r.pid = pid;
    // 再次进入项目默认落在上次访问的模块(localStorage 记忆,白名单校验)
    if (segs.length === 2) return { redirect: project(pid, lastTab(pid)) };
    const nav = navOf(segs[2]);
    if (!nav) return { redirect: project(pid) };
    r.tab = nav.key;
    r.docId = segs[3] ? UI.idOf(segs[3]) : null;
    // 项目页只有一层"项目作用域",模块层由它挂出来(child() 在拿到 proj 后才产出)
    return {
      spec: Views.projectLevel(r),
      hostClass: nav.mode ? MODE_CLASS[nav.mode] : null,
      route: r,
    };
  }

  // 当前路由事实:由 app.js 在每次路由时设入,作用域在需要更深的路由片段时现读
  let current = null;
  function setCurrent(r) { current = r; }
  function currentRoute() { return current; }

  return {
    NAV: NAV,
    resolve: resolve,
    parse: parse,
    project: project,
    navOf: navOf,
    lastTab: lastTab,
    current: currentRoute,
    setCurrent: setCurrent,
  };
})();
