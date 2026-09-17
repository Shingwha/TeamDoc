// views/admin.js — 管理后台协调器(#/admin,仅 is_admin)
//   五个区(存储 / 备份与恢复 / 项目 / 用户 / 登录动态)各自拆进 views/admin/*.js,
//   本文件只做装配:容器骨架、跨区共享状态、refresh 注册表。
//   "操作后刷哪些区"由调用点显式声明(ctx.refresh('users','security')):各调用点自己
//   挑 Promise.all 组合的话,新增一个区时没人能保证所有调用点都补上新依赖。
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

    // 跨区共享状态:用户区"禁用"前的 soleOwned 提醒要读项目区所有者,是典型消费方;
    // 各区 load() 时写入,事件发生时读取
    const state = {};
    const sections = {};
    const ctx = {
      state,
      // 刷新注册表:按区名显式刷新,替代散点的 Promise.all([loadA(), loadB()]) 组合
      refresh: (...keys) => Promise.all(keys.map((k) => sections[k] && sections[k].load())),
    };
    [['storage', '#admin-store'],
     ['backup', '#admin-backup'],
     ['projects', '#admin-projects'],
     ['users', '#admin-body'],
     ['security', '#admin-security']].forEach(([key, sel]) => {
      sections[key] = window.AdminSections[key](
        Object.assign({}, ctx, { el: container.querySelector(sel) }));
    });

    // 各区事件委托挂在自己的容器上:容器由本视图创建、随路由重绘销毁,监听器随之消失,
    // 不需要在 App.onCleanup 里逐个注销(挂在持久节点上的委托才需要那一步)
    await Promise.all(Object.values(sections).map((s) => s.load()));
  };
})();
