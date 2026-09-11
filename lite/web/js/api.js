// api.js — fetch 封装(构建文档 §10.6 约定,契约不可变)
//  - 自动 credentials:'same-origin';body 为对象时 JSON 序列化 + content-type
//  - 204 → null(必须先判断 204 再解析 JSON)
//  - 错误响应 {detail:{code,message}} → throw ApiError(code, message, status)
//  - 401 且非登录页 → location.hash = '#/login'

class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.name = 'ApiError';
    this.code = code || 'UNKNOWN';
    this.status = status || 0;
  }
}

async function api(path, { method = 'GET', body, raw = false, timeoutMs = 30000 } = {}) {
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined && body !== null) {
    if (body instanceof FormData) {
      // multipart:浏览器自动带 boundary,禁止手动设置 Content-Type
      opts.body = body;
    } else if (body instanceof Blob) {
      // 原始二进制上传(raw body):不设 Content-Type,浏览器按 Blob 的 type 带,
      // 并自动算出 Content-Length(上传进度依赖它)。File 是 Blob 的子类,一并命中
      opts.body = body;
    } else if (typeof body === 'object') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    } else {
      opts.body = body;
    }
  }
  // 超时:后端假死(库锁死、磁盘卡住)时 fetch 会一直挂着,视图的 loading 永远转下去,
  // 用户没有任何失败出口。AbortController 让它在 30 秒后失败并给出可读提示。
  // 大文件上传/下载不走这个函数(用 XHR 或锚点),不受此限。
  let timer = null;
  if (timeoutMs > 0 && typeof AbortController !== 'undefined') {
    const ctrl = new AbortController();
    opts.signal = ctrl.signal;
    timer = setTimeout(() => ctrl.abort(), timeoutMs);
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    // 网络层失败抛的是原生 TypeError(信息是英文 "Failed to fetch"),
    // 各视图直接展示 e.message 会把英文甩给用户;这里统一转成可读的中文。
    const aborted = e && e.name === 'AbortError';
    throw new ApiError(aborted ? 'TIMEOUT' : 'NETWORK',
      aborted ? '请求超时(服务可能正忙或已停止响应),请稍后重试'
              : '无法连接服务器,请检查网络或服务是否在运行', 0);
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (resp.status === 204) return raw ? resp : null; // 204 无 body
  const text = await resp.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!resp.ok) {
    const detail = data && typeof data === 'object' ? data.detail : null;
    const code = (detail && detail.code) || ('HTTP_' + resp.status);
    // 反代/网关返回的 HTML 错误页别原样甩给用户(一段标签没人看得懂)
    const rawText = typeof data === 'string' && !/[<>]/.test(data) ? data : '';
    const message = (detail && detail.message) || rawText || ('请求失败(' + resp.status + ')');
    // 401 且非登录页 → 跳登录(会话过期或未登录)
    if (resp.status === 401 && !location.hash.startsWith('#/login')) {
      location.hash = '#/login';
    }
    throw new ApiError(code, message, resp.status);
  }
  return raw ? resp : data;
}

window.api = api;
window.ApiError = ApiError;
