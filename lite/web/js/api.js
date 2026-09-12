// api.js — fetch 封装(构建文档 §10.6 约定,契约不可变)
//  - 自动 credentials:'same-origin';body 为对象时 JSON 序列化 + content-type
//  - 204 → null(必须先判断 204 再解析 JSON)
//  - 错误响应 {detail:{code,message}} → throw ApiError(code, message, status)
//  - 401 且非登录页 → location.hash = '#/login'

class ApiError extends Error {
  constructor(code, message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.code = code || 'UNKNOWN';
    this.status = status || 0;
    // 完整的 detail 原样保留:契约错误除了 code/message 还可能带现场数据
    // (文档内容冲突的 409 带 currentVersion/currentContent,前端要拿它做差异对比)
    this.detail = detail || null;
  }
}

async function api(path, { method = 'GET', body, raw = false, timeoutMs = 30000 } = {}) {
  // raw: 返回原始文本(不做 JSON 解析;apiText 的基础)。body 在下面已读成 text,
  // 直接交付 —— 绝不能把 Response 原样交出去,那会带着已消费的 body(raw 调用方
  // 再读就抛 "body stream already read");其余调用方拿解析后的 JSON/文本
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
  // 计时覆盖**含 body 读取的整个过程**:只保护到响应头是不够的 —— 服务端可能
  // 回完头就卡住,这个请求会永远 pending,而它一直占着浏览器对同源的 6 个并发
  // 名额,攒够 6 条整页就"点什么都没反应"(服务端却完全正常)。
  // 大文件上传/下载不走这个函数(用 XHR 或锚点),不受此限。
  let timer = null;
  if (timeoutMs > 0 && typeof AbortController !== 'undefined') {
    const ctrl = new AbortController();
    opts.signal = ctrl.signal;
    timer = setTimeout(() => ctrl.abort(), timeoutMs);
  }
  let resp;
  let text;
  try {
    resp = await fetch(path, opts);
    if (resp.status === 204) return raw ? '' : null; // 204 无 body,raw 模式交付空文本
    text = await resp.text();
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
    throw new ApiError(code, message, resp.status, detail);
  }
  return raw ? text : data;
}

/**
 * 原始二进制上传(XHR,raw body),供云空间 / 编辑器附件 / 备份恢复共用。
 *
 * 为什么不用 api():fetch 拿不到上传进度,而且**上传不能设总时长超时** —— 大文件
 * 传几十分钟是正常的。这里只设"空闲超时"(idleMs 内没有任何进度就中止),既不会
 * 误杀正常的大文件,又能让死掉的链路及时退出:挂着的请求会一直占住浏览器对同源的
 * 6 个并发名额,占满 6 条整页就"点什么都没反应"(服务端却完全正常)。
 *
 * 其余约定与 api() 一致:同源 Cookie、{detail:{code,message}} → ApiError。
 * 空闲超时固定 120 秒(没有任何进度即中止)。
 */
function apiUpload(path, file, { onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open('POST', path);
    x.timeout = 120000;
    // 不设 Content-Type:让浏览器按 File 自动带上并计算 Content-Length
    if (onProgress && x.upload) {
      x.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
    }
    const parse = () => { try { return JSON.parse(x.responseText); } catch { return null; } };
    x.onload = () => {
      if (x.status >= 200 && x.status < 300) {
        resolve(x.responseText ? parse() : null);
        return;
      }
      const detail = (parse() || {}).detail;
      reject(new ApiError((detail && detail.code) || 'HTTP_' + x.status,
        (detail && detail.message) || ('HTTP ' + x.status), x.status, detail));
    };
    x.onerror = () => reject(new ApiError('NETWORK', '无法连接服务器,请检查网络或服务是否在运行', 0));
    x.ontimeout = () => reject(new ApiError('TIMEOUT', '上传超时(长时间无进度),请检查网络后重试', 0));
    x.send(file);
  });
}

/**
 * 拉取纯文本响应(带同源凭据;非 2xx 按 api() 同一套错误语义抛 ApiError)。
 * 站内文本预览(云空间 / @文件引用 / 存为文档)共用 —— 此前三处各写一份 fetch 样板。
 */
async function apiText(path, { timeoutMs = 60000 } = {}) {
  return api(path, { raw: true, timeoutMs });
}

window.api = api;
window.apiUpload = apiUpload;
window.apiText = apiText;
window.ApiError = ApiError;
