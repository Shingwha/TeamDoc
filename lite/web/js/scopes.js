// scopes.js — 渲染作用域链:键不变即复用,键变了才重建那一层
//
// 为什么需要它:路由切换原本是"整块销毁重建"(清空宿主 → 视图自己重新请求 → 重新拼
// HTML),于是切一篇文档也会连带重建侧栏、文档树与项目对象,用户看到"旧页消失 → 转圈
// → 空两栏 → 内容"。这里把渲染单元从"整页"缩到"一层作用域":壳/项目/文档模块各是一层,
// 每层有自己的键,键没变的那几层的 DOM、状态、监听、WebSocket 全部原样留着。
//
// 契约(ARCHITECTURE.md「渲染契约」,改动前读那一段):
//   1. 一个界面 = 一串作用域;键相同即复用,键变了才重建那一层
//   2. 作用域只交节点、不套壳:render 返回 DocumentFragment,节点直接落进上层槽位 ——
//      DOM 层级与"视图直接写容器"时逐字相同(布局契约里的直接子选择器继续成立)
//   3. 挂载路径上没有 await:render 同步画出骨架(含最终几何)并返回,数据由本层自己
//      的异步填充负责 —— 等待期只能出现在新层自己的区域内,永不把已有内容换成加载态
//   4. 父层决定子层:ctx.mount(slot) 交出子层挂载点(可延迟到数据到手;不调用就没有
//      子层)。子层描述由 spec.child() 在挂载那一刻产出,拿到的自然是父层当前状态
//   5. 清理归本层:ctx.dispose 注册的清理随本层被换掉时执行 —— 没有全局清理注册表
window.Scope = (function () {
  'use strict';

  let chain = [];        // 已挂载的层,下标 = 深度;0 层挂在宿主上
  let host = null;       // 宿主元素(#view)
  let hostClass = null;  // 当前设在宿主上的页面模式类

  /** HTML 字符串 → DocumentFragment:视图骨架的唯一出口(不经中间容器)。
   *  **片段在挂载时会被搬空**(appendChild 把子节点移走),所以渲染后还要用的元素
   *  必须在 render 的同步段里抓好引用,或者用 ctx.query(sel) 现查。 */
  function html(str) {
    const t = document.createElement('template');
    t.innerHTML = str;
    return t.content;
  }

  /** 卸载第 depth 层及其后的所有层(深层先走,释放顺序与挂载相反) */
  function unmountFrom(depth) {
    for (let i = chain.length - 1; i >= depth; i--) {
      const lvl = chain[i];
      // 先摘链再清理:清理过程中问 alive() 会立刻得 false,晚到的响应不会再写 DOM
      chain.length = i;
      for (let j = lvl.disposers.length - 1; j >= 0; j--) {
        try { lvl.disposers[j](); } catch (e) { console.error(e); }
      }
      lvl.nodes.forEach(function (n) { n.remove(); });
    }
  }

  /**
   * 子层对齐:本层就绪后把它下面那一层挂到槽位上。
   * 键没变 → 子层自己会复用(它的子层再往下递归),这里只负责把描述递下去;
   * 键变了 → 父层需要知道换人了(文档树要移动高亮行之类),用 onChild 通知。
   */
  function alignChild(lvl, depth, force) {
    if (!lvl.slot || !lvl.spec.child) return;
    const child = lvl.spec.child();
    if (!child) return;
    const changed = child.key !== lvl.childKey;
    lvl.childKey = child.key;
    mountAt(lvl.slot, child, depth + 1, force);
    if ((changed || force) && lvl.spec.onChild) lvl.spec.onChild(child.props || null);
  }

  /** 挂载或复用第 depth 层 */
  function mountAt(parent, spec, depth, force) {
    const cur = chain[depth];
    if (!force && cur && cur.key === spec.key) {
      alignChild(cur, depth, force);   // 复用:DOM/状态/监听全留着,只向下对齐
      return;
    }
    unmountFrom(depth);

    const lvl = {
      key: spec.key,
      spec: spec,
      nodes: [],        // 本层交给上层的节点(卸载时逐个移除)
      slot: null,       // 子层挂载点
      disposers: [],
      childKey: null,   // 上一次对齐的子层键(判断"换人了")
    };
    chain[depth] = lvl;

    const ctx = {
      /** 交出子层挂载点。可以晚到(数据到手再调);不调用就没有子层。
       *  一个层只声明一次挂载点(本层自己创建、自己拥有) —— 第二次调用是写错了 */
      mount: function (slot) {
        if (chain[depth] !== lvl) return;    // 本层已被换掉,忽略
        if (lvl.slot) { console.error('作用域 ' + spec.key + ' 重复声明了子层挂载点'); return; }
        lvl.slot = slot;
        alignChild(lvl, depth, force);
      },
      /** 本层还挂着吗。晚到的响应写进的是已脱离文档的节点,问一句就知道该不该继续。
       *  空节点层(整层没有内容,例如无权限直接跳走)只要还在链上就算活着 */
      alive: function () {
        if (chain[depth] !== lvl) return false;
        return lvl.nodes.length === 0 || lvl.nodes.some(function (n) { return n.isConnected; });
      },
      /** 在本层自己的范围内查元素。挂载前在片段里查、挂载后在宿主里查,同一个调用
       *  两种时机都对 —— 片段挂载后被搬空,直接 frag.querySelector 会悄悄拿到 null */
      query: function (sel) {
        const scope = lvl.nodes.length ? lvl.nodes[0].parentNode : null;
        return scope ? scope.querySelector(sel) : null;
      },
      /** 注册清理(WS、全局监听、离开守卫…),换层时按注册逆序执行 */
      dispose: function (fn) { if (fn) lvl.disposers.push(fn); },
    };

    let frag = null;
    try {
      frag = spec.render(ctx, spec.props);
    } catch (e) {
      console.error('作用域 ' + spec.key + ' 渲染失败', e);
    }
    // 运行时形状检查:漏返回片段 = 挂载路径上混进了 await 或忘了 return,
    // 这类错误只会表现为"页面空白",在这里直接指出原因并拆掉半条链(下次导航重来)
    if (!frag || frag.nodeType !== 11) {
      if (frag) console.error('作用域 ' + spec.key + ' 的 render 必须同步返回 DocumentFragment' +
        '(用 Scope.html(html) 构造)', frag);
      unmountFrom(depth);
      return;
    }
    lvl.nodes = Array.prototype.slice.call(frag.childNodes);
    parent.appendChild(frag);
    // 注意:不在这里 alignChild。子层由 ctx.mount 交出(可能发生在 render 内,也可能
    // 晚到)。render 里没调用 ctx.mount 的层就是叶子层。
  }

  /** 把页面模式类设到宿主上(模式来自路由表;视图不得自己加类) */
  function setHostClass(cls) {
    if (hostClass === cls) return;
    if (host && hostClass) host.classList.remove(hostClass);
    hostClass = cls;
    if (host && cls) host.classList.add(cls);
  }

  /**
   * 走一遍作用域链。
   * @param {HTMLElement} hostEl  宿主(路由页面的容器)
   * @param {Object} spec         最外层作用域描述
   * @param {{hostClass?:string, force?:boolean}} [opts] force = 丢掉现有链整条重建
   */
  function apply(hostEl, spec, opts) {
    opts = opts || {};
    if (host !== hostEl) {
      unmountFrom(0);           // 换宿主:旧链连清理一起丢掉,不能留下没人管的 WS/监听
      host = hostEl;
      hostClass = null;
      chain = [];
    }
    setHostClass(opts.hostClass || null);
    mountAt(host, spec, 0, !!opts.force);
  }

  /** 清空整条链(登出/进登录页) */
  function clear() {
    unmountFrom(0);
    setHostClass(null);
  }

  return { html: html, apply: apply, clear: clear };
})();
