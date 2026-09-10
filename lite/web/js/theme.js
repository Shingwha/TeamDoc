// theme.js — 主题管理:浅色 / 深色 / 跟随系统 + 种子色(Material You 令牌,见 css/tokens.css)
// 持久化 localStorage;切换即时生效、无刷新;通过 onChange 通知(编辑器据此重建)
window.Theme = (function () {
  'use strict';

  var MODE_KEY = 'td:theme-mode';
  var COLOR_KEY = 'td:seed-color';
  var MODES = [
    { id: 'light', label: '浅色', icon: 'ri-sun-line' },
    { id: 'dark', label: '深色', icon: 'ri-moon-line' },
    { id: 'system', label: '跟随系统', icon: 'ri-computer-line' },
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
    listeners.slice().forEach(function (fn) { try { fn(resolved()); } catch (e) { /* 忽略 */ } });
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

  if (mql.addEventListener) {
    mql.addEventListener('change', function () { if (getMode() === 'system') apply(); });
  }
  apply();

  return { MODES, COLORS, getMode, getColor, isDark, setMode, setColor, onChange };
})();
