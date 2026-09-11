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

async function api(path, { method = 'GET', body, raw = false } = {}) {
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
  const resp = await fetch(path, opts);
  if (resp.status === 204) return raw ? resp : null; // 204 无 body
  const text = await resp.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!resp.ok) {
    const detail = data && typeof data === 'object' ? data.detail : null;
    const code = (detail && detail.code) || ('HTTP_' + resp.status);
    const message = (detail && detail.message)
      || (typeof data === 'string' ? data : '')
      || ('请求失败(' + resp.status + ')');
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
