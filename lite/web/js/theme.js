// theme.js — 主题管理:浅色 / 深色 / 系统 + 种子色(Material You 令牌,见 css/tokens.css)
// 持久化 localStorage;切换即时生效、无刷新;通过 onChange 通知(编辑器据此重建)
window.Theme = (function () {
  'use strict';

  var MODE_KEY = 'td:theme-mode';
  var COLOR_KEY = 'td:seed-color';
  var MODES = [
    { id: 'light', label: '浅色', icon: 'sun-line' },
    { id: 'dark', label: '深色', icon: 'moon-line' },
    { id: 'system', label: '系统', icon: 'computer-line' },
  ];
  // 种子色:value 为浅色主色,仅用于选择器圆点展示
  var COLORS = [
    { id: 'blue', label: '蓝', value: '#3370ff' },
    { id: 'purple', label: '紫', value: '#7558f0' },
    { id: 'green', label: '绿', value: '#1d8a4e' },
    { id: 'orange', label: '橙', value: '#b85a19' },
    { id: 'pink', label: '粉', value: '#bf2f63' },
    { id: 'teal', label: '青', value: '#007888' },
  ];

  var mql = window.matchMedia('(prefers-color-scheme: dark)');
  var listeners = [];

  function getMode() {
    var m = localStorage.getItem(MODE_KEY);
    return MODES.some(function (x) { return x.id === m; }) ? m : 'system';
  }
  function getColor() {
    var c = localStorage.getItem(COLOR_KEY);
    return COLORS.some(function (x) { return x.id === c; }) ? c : 'blue';
  }
  /** 当前生效的主题(把 system 解析为 light/dark) */
  function resolved() {
    var m = getMode();
    return m === 'system' ? (mql.matches ? 'dark' : 'light') : m;
  }
  function isDark() { return resolved() === 'dark'; }

  function apply() {
    var html = document.documentElement;
    html.dataset.theme = resolved();
    html.dataset.color = getColor();
    applyFavicon();
    listeners.slice().forEach(function (fn) { try { fn(resolved()); } catch (e) { /* 忽略 */ } });
  }

  /** favicon 跟随种子色(原设计:圆角方块 + 三条文档线,只把底色从写死的紫色
      换成当前主题色)。用**饱和的种子色**而不是 --md-primary:后者在深色模式下是
      浅色变体(如 #adc6ff),衬白线条会看不清;浏览器标签页也只在明暗间保留一个图标,
      固定用饱和色最易辨认。 */
  function faviconSvg(color) {
    var svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>" +
      "<rect width='32' height='32' rx='8' fill='" + color + "'/>" +
      "<path d='M9 9h14v3H9zm0 5.5h14v3H9zm0 5.5h9v3H9z' fill='white'/></svg>";
    return 'data:image/svg+xml,' + encodeURIComponent(svg);
  }
  function applyFavicon() {
    var id = getColor();
    var entry = null;
    for (var i = 0; i < COLORS.length; i++) {
      if (COLORS[i].id === id) { entry = COLORS[i]; break; }
    }
    var link = document.querySelector("link[rel='icon']");
    if (!link) {
      link = document.createElement('link');
      link.rel = 'icon';
      document.head.appendChild(link);
    }
    link.href = faviconSvg(entry ? entry.value : '#3370ff');
  }

  function setMode(m) { localStorage.setItem(MODE_KEY, m); apply(); }
  function setColor(c) { localStorage.setItem(COLOR_KEY, c); apply(); }

  /** 主题变化订阅,返回取消函数 */
  function onChange(fn) {
    listeners.push(fn);
    return function () {
      var i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  // 监听走 UI.onMediaChange(媒体查询监听的唯一入口)。UI 在 theme 之后加载,
  // 故延迟到 DOMContentLoaded 再绑定。系统模式之外的切换不依赖本监听。
  document.addEventListener('DOMContentLoaded', function () {
    UI.onMediaChange(mql, function () { if (getMode() === 'system') apply(); });
  });
  apply();

  return { MODES, COLORS, getMode, getColor, isDark, setMode, setColor, onChange };
})();
