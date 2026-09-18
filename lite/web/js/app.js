// app.js — 壳(侧栏 240px:搜索 / 项目树 / 用户卡片)+ 登录视图 + 路由入口
//   路由的文法(解析/串构造/模块表)在 route.js,界面怎么被挂出来在 scopes.js ——
//   本文件只做三件事:拿当前 hash、把路由事实交给作用域链、处理"不渲染任何东西"的分支
//   (登录页 / 重定向)。
(function () {
  'use strict';

  window.Views = window.Views || {};

  // ---------- 全局状态 ----------
  const App = {
    user: null,       // 当前登录用户 {id,email,name,isAdmin,avatarColor}
    auth: null,       // /api/auth/me 的 auth 段 {via,scopes}
    // 离开守卫:视图在"有未保存内容"时拦下路由切换。**机制在此,策略由视图给** ——
    // 注册的 fn(target) 返回 Promise<boolean>,false = 不许离开。同时只有一个
    // (同一时刻只有一个编辑器视图活着),所以用单槽而不是数组。
    leaveGuard: null,
    onLeaveGuard(fn) { this.leaveGuard = fn; },
    clearLeaveGuard() { this.leaveGuard = null; },
    // 路由串生成点(实现在 route.js:'#/p/…' 的唯一构造处,视图不手拼路由串)
    route: { project: Route.project },
  };
  window.App = App;

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

  // ---------- 侧栏:项目树(可展开节点;子项数据来自 Route.NAV) ----------
  let sidebarProjects = null; // null=未加载;否则为 /api/projects 的最近结果(个人项目已在服务端置顶)
  // 展开状态的唯一真相:仅由用户点击行、一次性种子(刷新/深链)、新建项目三处写入;
  // 路由变化只影响高亮,绝不动展开态——这是"离开项目页不收回"的根本保证
  const treeExpanded = new Map(); // projectId → bool
  let treeSeeded = false;
  let treeScrolledTo = null;      // 上次因路由滚动过的项目(避免每次导航都滚动侧栏)

  async function loadSidebarProjects() {
    const box = document.getElementById('side-projects');
    try {
      sidebarProjects = (await api(Endpoints.projects())) || [];
      renderProjectTree();
    } catch (e) {
      box.innerHTML = '<span class="side-empty">加载失败</span>';
    }
  }
  App.refreshSidebar = loadSidebarProjects;

  /** 重绘项目树:展开态纯读 treeExpanded;路由只决定节点弱化高亮与子项 active。
      结构骨架由 UI.tree 产出(.tree-node/.tree-row/.tree-caret/.tree-children),
      侧栏变体类(.side-item/.side-proj/.side-tree-children)挂在行与子项上(样式见 app.css)。
      **只在数据/展开态变化时调用** —— 路由切换只动 class(见 markSidebarActive) */
  function renderProjectTree() {
    if (!sidebarProjects) return;
    const box = document.getElementById('side-projects');
    if (!sidebarProjects.length) {
      box.innerHTML = '<span class="side-empty">暂无项目</span>';
      return;
    }
    const { curPid, curTab } = sidebarActive();
    // 一次性种子:刷新/深链直接进入项目页时默认展开该项目;此后展开态完全由用户接管
    if (!treeSeeded) {
      treeSeeded = true;
      if (curPid) treeExpanded.set(curPid, true);
    }
    box.innerHTML = UI.tree({
      nodes: sidebarProjects,
      // 子项是模块导航链接而非树行,交给 childrenHtml 渲染;kids 恒非空 → caret 恒可展开
      children: (p) => Route.NAV.filter((n) => n.visible(p)),
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
      // data-pid/data-tab 是原地高亮(不重绘)的命中依据
      childrenHtml: (p) =>
        '<div class="side-tree-children">' + Route.NAV
          .filter((n) => n.visible(p))
          .map((n) =>
            '<a class="side-item side-subitem' + (p.id === curPid && curTab === n.key ? ' active' : '') + '"' +
            ' data-pid="' + UI.esc(p.id) + '" data-tab="' + UI.esc(n.key) + '"' +
            ' href="' + App.route.project(p.id, n.key) + '" title="' + UI.esc(n.label) + '">' +
            UI.icon(n.icon) + '<span class="side-label">' + UI.esc(n.label) + '</span></a>'
          ).join('') + '</div>',
    });
    scrollToCurrentProject(curPid, box);
  }

  /** 当前路由落在哪个项目/模块(路由里的 id 过一遍形状:形状不对 → null,坏链接不高亮) */
  function sidebarActive() {
    const segs = currentSegs();
    return {
      curPid: segs[0] === 'p' ? UI.idOf(segs[1]) : null,
      curTab: segs[0] === 'p' ? (segs[2] || null) : null,
    };
  }

  /** 侧栏滚动只在"当前项目变了"时发生:每次路由都滚一下,侧栏会自己动起来 */
  function scrollToCurrentProject(curPid, box) {
    if (!curPid || treeScrolledTo === curPid) return;
    treeScrolledTo = curPid;
    const escaped = window.CSS && CSS.escape ? CSS.escape(curPid) : curPid;
    const node = box.querySelector('.side-proj[data-pid="' + escaped + '"]');
    if (node) node.scrollIntoView({ block: 'nearest' });
  }

  // 节点行点击 = 纯展开/收起(整行,不导航;进入项目只能点子项);这是展开态的日常唯一写入点
  document.getElementById('side-projects').addEventListener('click', (e) => {
    const row = e.target.closest('.side-proj');
    if (!row) return;
    const pid = UI.idOf(row.dataset.pid);
    const open = treeExpanded.get(pid) !== true;
    treeExpanded.set(pid, open);
    setTreeNodeOpen(row, open);
  });

  /** 原地展开/收起一个树节点:结构与 UI.tree 产出的一致(.tree-caret + .tree-children)。
      整树重绘会丢 hover/焦点,树越大越明显 —— 折叠只是隐藏子层,不该重建节点 */
  function setTreeNodeOpen(row, open) {
    const node = row.closest('.tree-node') || row.parentElement;
    const caret = node.querySelector('.tree-caret');
    const kids = node.querySelector('.tree-children');
    if (caret && !caret.classList.contains('leaf')) caret.classList.toggle('open', open);
    if (kids) kids.hidden = !open;
  }

  // 新建项目后跳转前显式展开,保证落地页子项可见(见 projects.js)
  App.expandProject = (pid) => { treeExpanded.set(UI.idOf(pid), true); renderProjectTree(); };

  /** 全局项(项目列表 / 发现 / 管理后台 / 个人设置)与项目子项的高亮。
      **只动 class**:路由切换重绘侧栏会让整个项目树重建一次(旧代码就是),那是"整页闪"的一部分 */
  function markSidebarActive() {
    const { curPid, curTab } = sidebarActive();
    let sel = null;
    // #/ 即项目列表页(segs 为空),它是"项目"这一项的归属路由
    const segs = currentSegs();
    if (segs.length === 0) sel = '[data-route="projects"]';
    else if (segs[0] === 'discover') sel = '[data-route="discover"]';
    else if (segs[0] === 'admin') sel = '[data-route="admin"]';
    else if (segs[0] === 'settings') sel = '[data-route="settings"]';
    document.querySelectorAll('#sidebar .side-item.active').forEach((a) => {
      if (!sel || !a.matches(sel)) a.classList.remove('active');
    });
    if (sel) {
      const el = document.querySelector('#sidebar ' + sel);
      if (el) el.classList.add('active');
    }
    const box = document.getElementById('side-projects');
    box.querySelectorAll('.side-proj').forEach((row) => {
      row.classList.toggle('current', row.dataset.pid === curPid);
    });
    box.querySelectorAll('.side-subitem').forEach((a) => {
      a.classList.toggle('active', a.dataset.pid === curPid && a.dataset.tab === curTab);
    });
    scrollToCurrentProject(curPid, box);
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
    try { await api(Endpoints.authLogout(), { method: 'POST' }); } catch (e) { /* 忽略 */ }
    App.user = null;
    App.auth = null;
    if (location.hash === '#/login') route();
    else location.hash = '#/login';
  }

  // ---------- 路由 ----------
  // 解析归 route.js(Route.parse/resolve),这里只做三件事:拿路由事实、把事实交给作用域
  // 链、处理"不渲染任何东西"的分支(登录页 / 重定向)。
  function currentSegs() { return Route.parse(location.hash).segs; }

  /** force = 丢掉现有作用域链整条重建(页面身份变了;只是数据变了该让那一层自己重取) */
  let forceNext = false;   // 待兑现的强制重建:重定向会跨一次 _route,标记必须活到真正挂载那一次
  function route(force) {
    forceNext = forceNext || !!force;
    Promise.resolve(_route()).catch((e) => {
      console.error(e);
      UI.err(e);
    });
  }
  /**
   * 页面数据变了、而链上某一层的**存在与否**跟着变了时用它。典型:刚加入一个项目 ——
   * 项目层的错误态还在,而它的键(p/{pid})没变,不强制重建就永远停在"你还不是成员"。
   * 只让那一层自己重取是不够的:错误态/权限位是**上游层**的数据。
   */
  App.refresh = () => route(true);

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
   * 新路由,旧作用域(连同用户没保存的正文)还好端端留在 DOM 里,再跑一遍 route() 会把
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
    UI.closeOpenMenu();
    // 模态框挂在 body 上,不属于任何作用域,路由切换不会自动清掉它们 ——
    // 不关的话旧弹窗会盖在新页面上(且 Esc 只能关它)
    UI.closeAllModals();
    if (App.closeNavDrawer) App.closeNavDrawer();
    const view = document.getElementById('view');
    const out = Route.resolve(location.hash);

    if (out.login) { Scope.clear(); await renderLogin(); return; }

    // 路由守卫:未登录一律跳 #/login
    if (!App.user) {
      try {
        const me = await api(Endpoints.authMe());
        App.user = me.user;
        App.auth = me.auth || null;
        setupShell();
      } catch (e) {
        if (location.hash !== '#/login') location.replace('#/login');
        else { Scope.clear(); await renderLogin(); }
        return;
      }
    }

    // 重定向在碰 DOM 之前返回:先清空页面再跳转,用户会看到一帧空白
    if (out.redirect) {
      if (out.notice) UI.err(out.notice);
      location.replace(out.redirect);
      return;
    }

    Route.setCurrent(out.route);
    // 记住项目内模块,再次进入项目时落在同一处(见 Route.resolve 的裸项目链接分支)
    if (out.route.tab) UI.pref.set('td:lastTab:' + out.route.pid, out.route.tab);
    showShell();
    markSidebarActive();
    if (out.route.segs[0] === 'search') {
      const gs = document.getElementById('global-search');
      if (gs) gs.value = out.route.query.get('q') || '';
    }
    // 作用域链:键没变的那几层原样留着(壳/项目/文档树),只有键变了的那一层重建。
    // 强制重建标记在这里兑现 —— 重定向会跨一次 _route,提前清掉的话那次强制就丢了
    Scope.apply(view, out.spec, { hostClass: out.hostClass, force: forceNext });
    forceNext = false;
  }

  // ---------- 登录视图(#/login,无壳;含初始化向导,§7.1) ----------
  async function renderLogin() {
    hideShell();
    const root = document.getElementById('login-root');
    root.innerHTML = '<div class="login-page"><div class="login-card">' + UI.loadingRow() + '</div></div>';

    // 已登录直接进入系统
    try {
      const me = await api(Endpoints.authMe());
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
    try { status = await api(Endpoints.authStatus()); }
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
        const r = await api(isBootstrap ? Endpoints.authBootstrap() : Endpoints.authLogin(),
          { method: 'POST', body });
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
    const collapseBtn = document.getElementById('side-collapse');

    // 侧栏双态(宽屏内联 / 窄屏抽屉)状态机统一在 UI.dualPanel:一个布尔、一个类,
    // 「开」的宽屏形态是 240px 内联、「开」的窄屏形态是抽屉,CSS 见 app.css 的
    // #shell.side-open 两条规则。拖窗口不会改变开/关,只会改变形态
    const sidePanel = UI.dualPanel({
      el: shell,
      openCls: 'side-open',
      prefKey: 'td:side-open',
      onChange: syncCollapseBtn,
    });

    /** 按钮标题如实描述"点了会怎样"(状态机每次置位后回调;宽窄同语义) */
    function syncCollapseBtn(p) {
      collapseBtn.title = p.isOpen() ? '收起侧栏' : '展开侧栏';
    }

    collapseBtn.addEventListener('click', () => sidePanel.toggle());

    // 抽屉聚焦辅助:搜索框刚从 display:none 恢复,同帧 focus 会失效,延后一帧再聚焦
    function openNavDrawer(focusSearch) {
      sidePanel.open();
      if (focusSearch) requestAnimationFrame(() => { gs.focus(); gs.select(); });
    }
    // 路由切换时的"关抽屉"是窄屏礼仪(抽屉盖着新页面),宽屏内联侧栏不受路由影响 —
    // 若不分宽度地 close(),宽屏每次导航都会把侧栏收起并持久化,等于替用户开关
    App.closeNavDrawer = function () { if (sidePanel.isNarrow()) sidePanel.close(); };
    // rail 态的搜索按钮:窄屏打开抽屉并聚焦输入框;宽屏先展开侧栏再聚焦
    document.getElementById('side-search-btn').addEventListener('click', () => {
      if (sidePanel.isNarrow()) openNavDrawer(true);
      else { sidePanel.setOpen(true); gs.focus(); gs.select(); }
    });
    document.getElementById('side-scrim').addEventListener('click', sidePanel.close);
    // 选中导航项后收起(仅抽屉形态;项目节点行是纯展开/收起,不关抽屉;Esc 归 UI.dualPanel)
    document.getElementById('sidebar').addEventListener('click', (e) => {
      const item = e.target.closest('.side-item');
      if (sidePanel.isNarrow() && item && !item.classList.contains('side-proj')) sidePanel.close();
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
        if (sidePanel.isNarrow()) openNavDrawer(true);
        else {
          sidePanel.setOpen(true);
          gs.focus(); gs.select();
        }
      }
    });
    setupUserMenu();
    window.addEventListener('hashchange', guardedRoute);
    route();
  });
})();
