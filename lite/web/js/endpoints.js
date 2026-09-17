// endpoints.js — 后端路径的**唯一构造处**。
//
// 为什么要有这一层:URL 是前后端契约的一部分,和字段名同等重要。散在各视图里手拼的话,
// 改一个路径要全仓 grep,漏一处的表现是"某个按钮点了没反应" —— 而它不会被任何服务端
// 测试发现(测试打的是服务端,不是页面里的字符串)。
//
// 约定:
//   * 这里只拼路径,不发请求(api/apiUpload/UI.download 各自负责传输);
//   * 参数一律经 encodeURIComponent,调用方不需要自己转义;
//   * 查询串用对象传,值为 null/undefined/'' 的键自动略去 —— 条件参数不必拼字符串。
window.Endpoints = (function () {
  'use strict';

  function enc(v) { return encodeURIComponent(v); }

  function qs(params) {
    if (!params) return '';
    var parts = Object.keys(params)
      .filter(function (k) { return params[k] != null && params[k] !== ''; })
      .map(function (k) { return k + '=' + enc(params[k]); });
    return parts.length ? '?' + parts.join('&') : '';
  }

  function projectSub(pid, sub) { return '/api/projects/' + enc(pid) + sub; }

  return {
    // ---------- 认证 / 用户 ----------
    authStatus() { return '/api/auth/status'; },
    authMe() { return '/api/auth/me'; },
    authLogin() { return '/api/auth/login'; },
    authBootstrap() { return '/api/auth/bootstrap'; },
    authLogout() { return '/api/auth/logout'; },
    myPassword() { return '/api/users/me/password'; },
    pats() { return '/api/auth/pats'; },
    pat(id) { return '/api/auth/pats/' + enc(id); },
    users() { return '/api/users'; },
    user(id) { return '/api/users/' + enc(id); },
    directory() { return '/api/users/directory'; },

    // ---------- 项目 / 成员 ----------
    projects(params) { return '/api/projects' + qs(params); },
    project(pid) { return '/api/projects/' + enc(pid); },
    projectMembers(pid) { return projectSub(pid, '/members'); },
    projectMember(pid, uid) { return projectSub(pid, '/members/' + enc(uid)); },
    projectJoin(pid) { return projectSub(pid, '/join'); },
    projectLeave(pid) { return projectSub(pid, '/leave'); },
    projectStorage(pid) { return projectSub(pid, '/storage'); },
    projectTrash(pid) { return projectSub(pid, '/trash'); },
    discover() { return '/api/discover/projects'; },

    // ---------- 文档 ----------
    docTree(pid) { return projectSub(pid, '/docs/tree'); },
    docsOf(pid) { return projectSub(pid, '/docs'); },
    doc(id) { return '/api/docs/' + enc(id); },
    docContent(id) { return '/api/docs/' + enc(id) + '/content'; },
    docAppend(id) { return '/api/docs/' + enc(id) + '/append'; },
    docBacklinks(id) { return '/api/docs/' + enc(id) + '/backlinks'; },
    docMove(id) { return '/api/docs/' + enc(id) + '/move'; },
    docMoveCheck(id, params) { return '/api/docs/' + enc(id) + '/move-check' + qs(params); },
    docVersions(id) { return '/api/docs/' + enc(id) + '/versions'; },
    docVersion(id, vid) { return '/api/docs/' + enc(id) + '/versions/' + enc(vid); },
    docRestore(id, vid) { return docVersion(id, vid) + '/restore'; },

    // ---------- 文件 / 文件夹 ----------
    files(params) { return '/api/files' + qs(params); },
    fileUpload(params) { return '/api/files/upload' + qs(params); },
    file(id) { return '/api/files/' + enc(id); },
    fileDownload(id, inline) { return '/api/files/' + enc(id) + '/download' + (inline ? '?inline=1' : ''); },
    fileMeta(id) { return '/api/files/' + enc(id) + '/meta'; },
    fileMove(id) { return '/api/files/' + enc(id) + '/move'; },
    fileShare(id) { return '/api/files/' + enc(id) + '/share'; },
    // 匿名分享下载:凭链接里的 token,不带会话
    share(token) { return '/api/share/' + enc(token); },
    // 打包下载:ids/folderIds 都是逗号分隔的 id 串,值本身不再逐个转义
    filesZip(params) { return '/api/files/zip' + qs(params); },
    folders() { return '/api/files/folders'; },
    folder(id) { return '/api/files/folders/' + enc(id); },
    folderMove(id) { return '/api/files/folders/' + enc(id) + '/move'; },
    folderTree(pid) { return projectSub(pid, '/folders/tree'); },

    // ---------- 搜索 ----------
    // q 为空即"最近动态",type 取 all/docs/files(见服务端 search.py)
    search(params) { return '/api/search' + qs(params); },

    // ---------- 管理后台 ----------
    adminStorage() { return '/api/admin/storage'; },
    adminDiagnostics() { return '/api/admin/diagnostics'; },
    adminCleanup(params) { return '/api/admin/storage/cleanup' + qs(params); },
    adminProjects() { return '/api/admin/projects'; },
    adminAccess(uid) { return '/api/admin/users/' + enc(uid) + '/access'; },
    adminRevokeSession(ref) { return '/api/admin/sessions/' + enc(ref); },
    adminRevokeUserSessions(uid) { return '/api/admin/users/' + enc(uid) + '/sessions'; },
    adminLoginEvents(params) { return '/api/admin/login-events' + qs(params); },
    adminBackup() { return '/api/admin/backup'; },
    adminBackupStatus() { return '/api/admin/backup/status'; },
    adminBackupRun() { return '/api/admin/backup/run'; },
    adminRestoreStatus() { return '/api/admin/restore/status'; },
    adminRestoreUpload() { return '/api/admin/restore/upload'; },
    adminRestoreArm() { return '/api/admin/restore/arm'; },
    adminRestore() { return '/api/admin/restore'; },
  };
})();
