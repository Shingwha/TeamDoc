// app.js — hash 路由 + 壳(侧栏 240px:搜索 / 项目树 / 用户卡片)+ 登录视图
// 路由表:#/login、#/、#/p/{id}(重定向)、#/p/{id}/docs/{docId?}、#/p/{id}/files、
//         #/drive、#/search?q=、#/discover、#/admin、#/settings
(function () {
  'use strict';

  window.Views = window.Views || {};

  // ---------- 全局状态 ----------
  const App = {
    user: null,       // 当前登录用户 {id,email,name,isAdmin,avatarColor}
    auth: null,       // /api/auth/me 的 auth 段 {via,scopes}
    cleanups: [],     // 视图注册的清理函数(关闭 WS、移除编辑器增强监听等)
    onCleanup(fn) { this.cleanups.push(fn); },
    runCleanups() { this.cleanups.splice(0).forEach((fn) => { try { fn(); } catch (e) { /* 忽略 */ } }); },
    // 离开守卫:视图在"有未保存内容"时拦下路由切换。**机制在此,策略由视图给** ——
    // 注册的 fn(target) 返回 Promise<boolean>,false = 不许离开。同时只有一个
    // (同一时刻只有一个编辑器视图活着),所以用单槽而不是数组。
    leaveGuard: null,
    onLeaveGuard(fn) { this.leaveGuard = fn; },
    clearLeaveGuard() { this.leaveGuard = null; },
    // 路由生成点:项目内 '#/p/…' 的唯一构造处(此前 20+ 处手拼,esc/encode 口径不一;
    // 路由方案要改时只动这里)。query 为对象,键序即串序,值做 encodeURIComponent。
    route: {
      project(pid, tab, docId, query) {
        let h = '#/p/' + encodeURIComponent(pid);
        if (tab) h += '/' + encodeURIComponent(tab);
        if (docId != null) h += '/' + encodeURIComponent(docId);
        if (query) {
          const qs = Object.keys(query)
            .filter((k) => query[k] != null)
            .map((k) => k + '=' + encodeURIComponent(query[k])).join('&');
          if (qs) h += '?' + qs;
        }
        return h;
      },
    },
  };
  window.App = App;

  // ---------- 项目内导航配置(数据驱动:侧栏渲染 / 路由 / tab 记忆白名单共用) ----------
  // 新增模块只需在此加一项
  const PROJECT_NAV = [
    { key: 'docs', icon: 'ri-file-text-line', label: '文档', view: 'projectDocs', visible: () => true },
    { key: 'files', icon: 'ri-folder-line', label: '云空间', view: 'projectFiles', visible: () => true },
    { key: 'members', icon: 'ri-team-line', label: '成员', view: 'projectMembers', visible: (p) => !p || !p.isPersonal },
    // 回收站含"删了什么"这类项目内部信息,只对真成员与全局管理员显示
    // (两者之外的人服务端会 403,这里提前隐藏,不留一个点了报错的 tab)
    { key: 'trash', icon: 'ri-delete-bin-line', label: '回收站', view: 'projectTrash',
      visible: (p) => !p || p.isMember || !!(App.user && App.user.isAdmin) },
    { key: 'settings', icon: 'ri-settings-4-line', label: '设置', view: 'projectSettings', visible: () => true },
  ];
  const PROJECT_NAV_KEYS = PROJECT_NAV.map((n) => n.key);

  // ---------- 壳 ----------
  function showShell() {
    document.getElementById('shell').hidden = false;
    document.getElementById('login-root').hidden = true;
  }
  function hideShell() {
    document.getElementById('shell').hidden = true;
    document.getElementById('login-root').hidden = false;
  }

  function setupShell() {
    const u = App.user;
    if (!u) return;
    const name = u.name || u.email || '?';
    const sideUser = document.getElementById('side-user');
    sideUser.innerHTML =
      UI.avatar({ name: name, seed: u.id, color: u.avatarColor, size: 32 }) +
      '<span class="side-user-name side-label">' + UI.esc(name) + '</span>' +
      // 菜单向上弹出,故用上向 chevron 作指示(原 more-unfold-line 在 Remix Icon 4.5 中不存在,一直不显示)
      '<i class="ri-arrow-up-s-line"></i>';
    sideUser.title = name + '(' + (u.email || '') + ')';
    document.getElementById('side-admin').hidden = !u.isAdmin;
    loadSidebarProjects();
  }

  // ---------- 侧栏:项目树(可展开节点;子项数据来自 PROJECT_NAV) ----------
  let sidebarProjects = null; // null=未加载;否则为 /api/projects 的最近结果(个人项目已在服务端置顶)
  // 展开状态的唯一真相:仅由用户点击行、一次性种子(刷新/深链)、新建项目三处写入;
  // 路由变化只影响高亮,绝不动展开态——这是"离开项目页不收回"的根本保证
  const treeExpanded = new Map(); // projectId → bool
  let treeSeeded = false;

  async function loadSidebarProjects() {
    const box = document.getElementById('side-projects');
    try {
      sidebarProjects = (await api('/api/projects')) || [];
      renderProjectTree();
    } catch (e) {
      box.innerHTML = '<span class="side-empty">加载失败</span>';
    }
  }
  App.refreshSidebar = loadSidebarProjects;

  /** 重绘项目树:展开态纯读 treeExpanded;路由只决定节点弱化高亮与子项 active。
      结构骨架由 UI.tree 产出(.tree-node/.tree-row/.tree-caret/.tree-children),
      侧栏变体类(.side-item/.side-proj/.side-tree-children)挂在行与子项上(样式见 app.css) */
  function renderProjectTree() {
    if (!sidebarProjects) return;
    const box = document.getElementById('side-projects');
    if (!sidebarProjects.length) {
      box.innerHTML = '<span class="side-empty">暂无项目</span>';
      return;
    }
    const segs = currentSegs();
    // segs 是 URL 切出来的字符串,而展开表的键是 p.id(JSON 数字):不归一化就会"点了没反应"
    // (历史缺陷:资源 id 整数化后,侧栏项目行点击/深链展开全失效,见 HANDOFF §4.3)
    const curPid = segs[0] === 'p' ? Number(segs[1]) : null;
    const curTab = segs[0] === 'p' ? (segs[2] || null) : null;
    // 一次性种子:刷新/深链直接进入项目页时默认展开该项目;此后展开态完全由用户接管
    if (!treeSeeded) {
      treeSeeded = true;
      if (curPid) treeExpanded.set(curPid, true);
    }
    box.innerHTML = UI.tree({
      nodes: sidebarProjects,
      // 子项是模块导航链接而非树行,交给 childrenHtml 渲染;kids 恒非空 → caret 恒可展开
      children: (p) => PROJECT_NAV.filter((n) => n.visible(p)),
      expanded: (p) => treeExpanded.get(p.id) === true,
      rowCls: (p) => 'side-item side-proj' + (p.id === curPid ? ' current' : ''),
      rowAttrs: (p) => 'data-pid="' + UI.esc(p.id) + '" title="' + UI.esc(p.name) + '"',
      // caret 之外的内容:折叠图标栏态替身头像 + 项目名 + 个人徽章
      rowInner: (p) =>
        // 折叠图标栏态的替身:首字圆形头像(宽屏展开时隐藏;项目色已下线,统一 primary 固定色)
        '<span class="side-proj-avatar">' + UI.avatar({
          name: p.name, seed: p.id, size: 'sm', color: 'var(--md-primary)',
        }) + '</span>' +
        '<span class="side-label side-proj-name">' + UI.esc(p.name) + '</span>' +
        (p.isPersonal ? '<span class="badge xs side-label">个人</span>' : ''),
      childrenHtml: (p) =>
        '<div class="side-tree-children">' + PROJECT_NAV
          .filter((n) => n.visible(p))
          .map((n) =>
            '<a class="side-item side-subitem' + (p.id === curPid && curTab === n.key ? ' active' : '') + '"' +
            ' href="' + App.route.project(p.id, n.key) + '" title="' + UI.esc(n.label) + '">' +
            UI.icon(n.icon) + '<span class="side-label">' + UI.esc(n.label) + '</span></a>'
          ).join('') + '</div>',
    });
    if (curPid) {
      const escaped = window.CSS && CSS.escape ? CSS.escape(curPid) : curPid;
      const node = box.querySelector('[data-pid="' + escaped + '"]');
      if (node) node.scrollIntoView({ block: 'nearest' });
    }
  }

  // 节点行点击 = 纯展开/收起(整行,不导航;进入项目只能点子项);这是展开态的日常唯一写入点
  document.getElementById('side-projects').addEventListener('click', (e) => {
    const row = e.target.closest('.side-proj');
    if (!row) return;
    // dataset 读出来是字符串,而展开表以 p.id(数字)为键 —— 必须转数字,否则写入的键读不到
    const pid = UI.numId(row.dataset.pid);
    treeExpanded.set(pid, treeExpanded.get(pid) !== true);
    renderProjectTree();
  });

  // 新建项目后跳转前显式展开,保证落地页子项可见(见 projects.js)
  App.expandProject = (pid) => { treeExpanded.set(UI.numId(pid), true); renderProjectTree(); };

  /** 全局项(项目列表 / 发现 / 管理后台 / 个人设置)高亮 + 项目树随路由重绘 */
  function markSidebarActive(segs) {
    document.querySelectorAll('#sidebar .side-item.active').forEach((a) => a.classList.remove('active'));
    let sel = null;
    // #/ 即项目列表页(segs 为空),它是"项目"这一项的归属路由
    if (segs.length === 0) sel = '[data-route="projects"]';
    else if (segs[0] === 'discover') sel = '[data-route="discover"]';
    else if (segs[0] === 'admin') sel = '[data-route="admin"]';
    else if (segs[0] === 'settings') sel = '[data-route="settings"]';
    if (sel) {
      const el = document.querySelector('#sidebar ' + sel);
      if (el) el.classList.add('active');
    }
    renderProjectTree();
  }

  // ---------- 用户头像下拉菜单(个人设置 / 外观 / 退出登录) ----------
  // ---------- 外观面板:主题三档 + 种子色圆点(挂在用户菜单里) ----------
  function buildAppearancePanel() {
    const wrap = document.createElement('div');
    wrap.className = 'menu-header';
    wrap.innerHTML = '<div class="small muted mb-2 fw-semi">外观</div>';

    // 浅色 / 深色 / 跟随系统
    const segEl = document.createElement('div');
    segEl.innerHTML = UI.seg({
      active: Theme.getMode(),
      items: Theme.MODES.map((m) => ({ key: m.id, label: m.label, icon: m.icon })),
    });
    const seg = segEl.firstElementChild;
    UI.segWire(seg, (key) => Theme.setMode(key));
    wrap.appendChild(seg);

    // 种子色圆点选择器(底色是数据 —— 主题色值来自 Theme.COLORS,故仍内联注入)
    const dots = document.createElement('div');
    dots.className = 'color-dots mt-3';
    Theme.COLORS.forEach((c) => {
      const d = document.createElement('button');
      d.type = 'button';
      d.className = 'color-dot' + (Theme.getColor() === c.id ? ' active' : '');
      d.style.background = c.value;
      d.title = c.label;
      d.addEventListener('click', () => {
        Theme.setColor(c.id);
        dots.querySelectorAll('.color-dot').forEach((x) => x.classList.remove('active'));
        d.classList.add('active');
      });
      dots.appendChild(d);
    });
    wrap.appendChild(dots);
    return wrap;
  }

  function setupUserMenu() {
    const trigger = document.getElementById('side-user');
    UI.dropdownMenu(trigger, () => [
      {
        custom: (() => {
          const u = App.user || {};
          const el = document.createElement('div');
          el.className = 'menu-header';
          el.innerHTML =
            '<div class="cell-id">' +
            UI.avatar({ name: u.name || u.email, seed: u.id || u.email, size: 'lg', color: u.avatarColor }) +
            '<div class="cell-id-main"><div class="cell-id-name truncate">' + UI.esc(u.name || '') + '</div>' +
            '<div class="small muted truncate">' + UI.esc(u.email || '') + '</div></div></div>';
          return el;
        })(),
      },
      { divider: true },
      { icon: 'settings-3-line', label: '个人设置', onClick: () => { location.hash = '#/settings'; } },
      { custom: buildAppearancePanel() },
      { divider: true },
      { icon: 'logout-box-line', label: '退出登录', danger: true, onClick: doLogout },
    ], { direction: 'up' });
  }

  async function doLogout() {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* 忽略 */ }
    App.user = null;
    App.auth = null;
    if (location.hash === '#/login') route();
    else location.hash = '#/login';
  }

  // ---------- 路由 ----------
  function parseHash() {
    const h = location.hash.replace(/^#/, '') || '/';
    const qIdx = h.indexOf('?');
    const pathPart = qIdx >= 0 ? h.slice(0, qIdx) : h;
    const queryPart = qIdx >= 0 ? h.slice(qIdx + 1) : '';
    return {
      path: pathPart,
      segs: pathPart.split('/').filter(Boolean),
      query: new URLSearchParams(queryPart),
    };
  }
  function currentSegs() { return parseHash().segs; }

  function route() {
    Promise.resolve(_route()).catch((e) => {
      console.error(e);
      UI.err(e);
    });
  }
  App.refresh = () => route(); // 视图内数据变更后重渲染当前路由

  function hashOf(url) {
    if (!url) return '';
    const i = url.indexOf('#');
    return i < 0 ? '' : url.slice(i);
  }

  /**
   * 带守卫的 hashchange 入口(取代直接 route)。
   *
   * 守卫拒绝时必须把 hash 还原回去 —— 浏览器已经把地址改了,不还原的话地址栏与画面
   * 会对不上。还原动作自己又会触发一次 hashchange,**那一次什么都不做**:我们从没渲染
   * 新路由,旧视图(连同用户没保存的正文)还好端端留在 DOM 里,再跑一遍 route() 会把
   * 编辑器整个重建、未保存内容就没了。
   *
   * 用"还原目标 hash"而不是布尔标志来识别那一次:布尔标志一旦因为浏览器没派发事件而
   * 卡住,会静默吞掉下一次真实导航(点了没反应);比对 hash 则会自愈。
   */
  let restoredTo = null;
  async function guardedRoute(ev) {
    if (restoredTo !== null && location.hash === restoredTo) { restoredTo = null; return; }
    const fromHash = hashOf(ev && ev.oldURL);
    // 去登录页不拦:登出与 401 失效有自己的语义,拦住会把用户卡在一个已失效的页面上
    if (!App.leaveGuard || location.hash.startsWith('#/login')) { route(); return; }
    let allow = true;
    try { allow = await App.leaveGuard(location.hash); }
    catch (e) { allow = true; }   // 守卫自身出错不该把用户困死
    if (allow) { route(); return; }
    restoredTo = fromHash || '#/';
    location.replace(restoredTo);
  }

  async function _route() {
    App.runCleanups();
    UI.closeOpenMenu();
    // 模态框挂在 body 上,不属于任何视图,路由切换不会自动清掉它们 ——
    // 不关的话旧弹窗会盖在新页面上(且 Esc 只能关它)
    UI.closeAllModals();
    if (App.closeNavDrawer) App.closeNavDrawer();
    const { segs, query } = parseHash();
    const view = document.getElementById('view');

    if (segs[0] === 'login') { await renderLogin(); return; }

    // 路由守卫:未登录一律跳 #/login
    if (!App.user) {
      try {
        const me = await api('/api/auth/me');
        App.user = me.user;
        App.auth = me.auth || null;
        setupShell();
      } catch (e) {
        if (location.hash !== '#/login') location.replace('#/login');
        else await renderLogin();
        return;
      }
    }

    showShell();
    markSidebarActive(segs);
    view.innerHTML = '';
    // 上一路由的页面模式残留清掉:填充模式(文档页)与窄页(表单/设置页)
    // 都由各视图自行声明,路由切换时先回到默认页宽
    view.classList.remove('view-fill', 'page-narrow');

    if (segs.length === 0) return Views.projects(view, { query });

    if (segs[0] === 'p' && segs[1]) {
      // id 段在路由边界统一转数字:资源 id 现为自增整数,视图内的 === 比较才能对上
      const pid = Number(segs[1]);
      if (!Number.isInteger(pid) || pid <= 0) { location.replace('#/'); return; }
      // 再次进入项目默认落在上次访问的模块(localStorage 记忆,白名单校验)
      if (segs.length === 2) {
        let tab = UI.pref.get('td:lastTab:' + pid, 'docs');
        if (PROJECT_NAV_KEYS.indexOf(tab) < 0) tab = 'docs';
        location.replace(App.route.project(pid, tab));
        return;
      }
      const nav = PROJECT_NAV.find((n) => n.key === segs[2]);
      if (nav) {
        UI.pref.set('td:lastTab:' + pid, nav.key);
        const docId = segs[3] ? Number(segs[3]) : null;
        return Views[nav.view](view, { projectId: pid, docId: docId && Number.isInteger(docId) ? docId : null, query });
      }
      location.replace(App.route.project(pid));
      return;
    }

    if (segs[0] === 'drive') {
      // 个人云空间已并入"个人项目":重定向到其云空间 tab
      try {
        const list = await api('/api/projects');
        const personal = (list || []).find((p) => p.isPersonal);
        location.replace(personal ? App.route.project(personal.id, 'files') : '#/');
      } catch (e) {
        UI.err(e);
        location.replace('#/');
      }
      return;
    }
    if (segs[0] === 'search') {
      document.getElementById('global-search').value = query.get('q') || '';
      return Views.search(view, { query });
    }
    if (segs[0] === 'discover') return Views.discover(view, { query });
    if (segs[0] === 'admin') return Views.admin(view, { query });
    if (segs[0] === 'settings') return Views.settings(view, { query });

    location.replace('#/');
  }

  // ---------- 登录视图(#/login,无壳;含初始化向导,§7.1) ----------
  async function renderLogin() {
    hideShell();
    App.runCleanups();
    const root = document.getElementById('login-root');
    root.innerHTML = '<div class="login-page"><div class="login-card">' + UI.loadingRow() + '</div></div>';

    // 已登录直接进入系统
    try {
      const me = await api('/api/auth/me');
      if (me && me.user) {
        App.user = me.user;
        App.auth = me.auth || null;
        setupShell();
        location.replace('#/');
        return;
      }
    } catch (e) { /* 未登录,继续渲染登录页 */ }

    // 未初始化 → 初始化向导;已初始化 → 登录表单(§7.1 status)
    let status = { bootstrapped: true, dbReady: true };
    try { status = await api('/api/auth/status'); }
    catch (e) { status = { bootstrapped: true, dbReady: false }; }

    if (!status.bootstrapped) renderAuthForm(root, { status, mode: 'bootstrap' });
    else renderAuthForm(root, { status, mode: 'login', email: rememberedEmail() });
  }

  // 记住邮箱:只在登录成功后才写入(见 renderLoginForm),避免把打错的邮箱也记住
  const REMEMBER_EMAIL_KEY = 'td:login-email';
  function rememberedEmail() { return UI.pref.get(REMEMBER_EMAIL_KEY, ''); }

  function dbWarning(status) {
    return status && status.dbReady === false
      ? UI.banner({ kind: 'info', icon: 'database-2-line', cls: 'mb-4', text: '数据库未就绪,请稍后刷新重试' }) : '';
  }

  /**
   * 登录 / 初始化向导共用一张表单(两份骨架 ~90% 相同,差异全部走配置):
   * mode: 'login'(记住邮箱 + remember 勾选)| 'bootstrap'(多一个姓名字段,
   * 成功后多一句 toast;autocomplete 令牌按"创建新账号"语义给)。
   */
  function renderAuthForm(root, { status, mode, email = '' }) {
    const isBootstrap = mode === 'bootstrap';
    // 刻意用 type="text":type="email" 会启用浏览器的原生格式校验,而它比服务端严
    // (如 11@.com 会被拦下),而后端只要求含 "@"。两边强度不一致会造出"管理后台能建、
    // 登录页却登不进去"的账号。格式判定统一交给服务端,前端只管必填。
    const emailField =
      '<div class="field"><label>邮箱</label>' +
      '<input type="text" class="input" name="email" required autocomplete="username" value="' + UI.esc(email) + '"></div>';
    const passwordField = '<div class="field"><label>密码' + (isBootstrap ? '(至少 8 位)' : '') + '</label>' +
      '<input type="password" class="input" name="password" required' + (isBootstrap ? ' minlength="8"' : '') +
      ' autocomplete="' + (isBootstrap ? 'new-password' : 'current-password') + '"></div>';
    const nameField = isBootstrap
      ? '<div class="field"><label>姓名</label>' +
        '<input type="text" class="input" name="name" required maxlength="50" autocomplete="name"></div>'
      : '';
    // 文案是"延长登录有效期"而非"记住密码":本地不存密码,靠更长的会话 cookie 实现。
    // 写成"记住密码"会让用户以为密码落盘了,是误导
    const rememberField = isBootstrap
      ? ''
      : '<div class="field"><label class="check-row"><input type="checkbox" name="remember">' +
        '记住我(延长登录有效期)</label></div>';

    root.innerHTML =
      '<div class="login-page"><div class="login-card">' +
      '<div class="login-brand">TeamDoc</div>' +
      '<div class="login-sub">' + (isBootstrap ? '初始化:创建管理员账号' : '小团队自部署知识库') + '</div>' +
      dbWarning(status) +
      UI.banner({ id: 'login-err', kind: 'danger', cls: 'mb-4', hidden: true, html: '<span></span>' }) +
      // 字段顺序即机制,新增字段别插进邮箱和密码之间:密码管理器把「密码框上方
      // 最近的文本框」当账号,所以邮箱必须紧贴密码框,姓名殿后并标 autocomplete="name"
      // 以人员语义退出凭据配对 —— 单靠令牌拦不住(实测 Chrome 仍会把紧贴密码框的
      // 姓名当成账号,把「姓名+密码」存成一组凭据),顺序才是根治。
      '<form id="auth-form">' + emailField + passwordField + nameField + rememberField +
      '<button type="submit" class="btn btn-filled btn-lg btn-block" id="auth-btn">' +
      (isBootstrap ? '创建并登录' : '登 录') + '</button>' +
      '</form></div></div>';

    const form = root.querySelector('#auth-form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const body = {
        email: String(fd.get('email') || '').trim(),
        password: String(fd.get('password') || ''),
      };
      if (isBootstrap) body.name = String(fd.get('name') || '').trim();
      else body.remember = fd.get('remember') != null;
      const btn = root.querySelector('#auth-btn');
      btn.disabled = true;
      try {
        const r = await api(isBootstrap ? '/api/auth/bootstrap' : '/api/auth/login', { method: 'POST', body });
        if (!isBootstrap) UI.pref.set(REMEMBER_EMAIL_KEY, body.email); // 只在成功后记,避免记住打错的
        App.user = r.user;
        App.auth = null;
        if (isBootstrap) UI.toast('初始化完成,已登录', 'success');
        setupShell();
        location.replace('#/');
      } catch (err2) {
        btn.disabled = false;
        const errBox = root.querySelector('#login-err');
        errBox.querySelector('span').textContent = err2.message || (isBootstrap ? '初始化失败' : '登录失败');
        errBox.hidden = false;
      }
    });
  }

  // ---------- 启动 ----------
  document.addEventListener('DOMContentLoaded', () => {
    const shell = document.getElementById('shell');
    const gs = document.getElementById('global-search');
    const narrowMq = window.matchMedia('(max-width: 960px)');

    // 侧栏两种状态:折叠(72px 图标栏)/ 展开(240px)。
    // 宽屏展开是内联的(挤压主区);窄屏放不下 240px 内联,展开必须浮层化 —— 即 .nav-open 抽屉。
    // 两者共用同一套 CSS(#shell.side-collapsed:not(.nav-open)),JS 只负责"谁来置位"。
    let userCollapsed = UI.pref.get('td:side-collapsed') === '1'; // 只存宽屏偏好
    const collapseBtn = document.getElementById('side-collapse');

    /** 窄屏恒折叠(否则没有导航入口);宽屏按用户偏好。窄屏不写 localStorage,回到宽屏即恢复原选择 */
    function applyNavMode() {
      shell.classList.toggle('side-collapsed', narrowMq.matches || userCollapsed);
      syncCollapseBtn();
    }

    /** 改折叠偏好并持久化(宽屏语义);窄屏调用方请用抽屉开关代替 */
    function setUserCollapsed(collapsed) {
      userCollapsed = collapsed;
      UI.pref.set('td:side-collapsed', collapsed ? '1' : '');
      applyNavMode();
    }

    /** 按钮语义随屏宽变化,标题始终如实描述"点了会怎样" */
    function syncCollapseBtn() {
      const expanded = narrowMq.matches
        ? shell.classList.contains('nav-open')
        : !shell.classList.contains('side-collapsed');
      collapseBtn.title = expanded ? '收起侧栏' : '展开侧栏';
    }

    applyNavMode();
    // 兼容分支在 UI.onMediaChange 里(theme.js 同用):旧内核没有 addEventListener,
    // 直接调用会抛 TypeError —— 而 DOMContentLoaded 回调里后续的 setupUserMenu()
    // 与 route() 都不会执行,页面停在隐藏的 #shell 上 = 白屏。
    UI.onMediaChange(narrowMq, applyNavMode);
    collapseBtn.addEventListener('click', () => {
      // 窄屏:侧栏已是 72px 图标栏,"展开"只能浮层化,故该按钮即抽屉开关
      if (narrowMq.matches) {
        if (shell.classList.contains('nav-open')) closeNavDrawer(); else openNavDrawer(false);
        return;
      }
      setUserCollapsed(!userCollapsed);
    });

    // 抽屉:#shell.nav-open 由 CSS 负责定位与过渡,JS 只切 class
    function openNavDrawer(focusSearch) {
      shell.classList.add('nav-open');
      syncCollapseBtn();
      // 搜索框刚从 display:none 恢复,同帧 focus 会失效,延后一帧再聚焦
      if (focusSearch) requestAnimationFrame(() => { gs.focus(); gs.select(); });
    }
    function closeNavDrawer() {
      shell.classList.remove('nav-open');
      syncCollapseBtn();
    }
    App.closeNavDrawer = closeNavDrawer;
    // 折叠态的搜索按钮:窄屏打开抽屉并聚焦输入框;宽屏先展开侧栏再聚焦
    document.getElementById('side-search-btn').addEventListener('click', () => {
      if (narrowMq.matches) openNavDrawer(true);
      else { setUserCollapsed(false); gs.focus(); gs.select(); }
    });
    document.getElementById('side-scrim').addEventListener('click', closeNavDrawer);
    // 选中导航项后收起(项目节点行是纯展开/收起,不关抽屉)
    document.getElementById('sidebar').addEventListener('click', (e) => {
      const item = e.target.closest('.side-item');
      if (item && !item.classList.contains('side-proj')) closeNavDrawer();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeNavDrawer();
    });

    // 侧栏全局搜索:回车跳转 #/search?q=;Ctrl+K 聚焦(窄屏先展开抽屉,宽屏折叠态先展开图标栏)
    gs.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const q = gs.value.trim();
        const target = '#/search' + (q ? '?q=' + encodeURIComponent(q) : '');
        if (location.hash === target) route(); // hash 未变化时手动触发重新搜索
        else location.hash = target;
      }
    });
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        if (narrowMq.matches) openNavDrawer(true);
        else {
          if (shell.classList.contains('side-collapsed')) setUserCollapsed(false);
          gs.focus(); gs.select();
        }
      }
    });
    setupUserMenu();
    window.addEventListener('hashchange', guardedRoute);
    route();
  });
})();
