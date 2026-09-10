"""全端点冒烟测试:遍历所有 API 路由,断言无 5xx(NameError/TypeError 类回归的护栏)。

用法:先启动服务(隔离数据目录 + 非常用端口),再运行本脚本。
输出每个端点的状态码;任何 5xx 视为失败。

可重复运行:测试用户邮箱带每次运行唯一的随机后缀。此前用固定邮箱,而系统没有
"删除用户"接口(只能禁用),于是第二次运行必然撞 409 并让后续断言全用 None 作 id
连带失败 —— 看起来像服务端 bug,其实是脚本不可重入。
"""
import os
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
SID = None
FAIL = []
# 每次运行唯一,保证脚本可重复运行(系统无删除用户接口)
SMOKE_EMAIL = f"smoke-{uuid.uuid4().hex[:8]}@teamdoc.local"


def raw(method, path, body=None, ctype=None, raw_body=None):
    global SID
    data = raw_body
    if data is None and body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None and data is not None and ctype is None:
        req.add_header("Content-Type", "application/json")
    if ctype:
        req.add_header("Content-Type", ctype)
    if SID:
        req.add_header("Cookie", "td_sid=" + SID)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            sc = r.headers.get("Set-Cookie")
            if sc and "td_sid=" in sc:
                SID = sc.split("td_sid=")[1].split(";")[0]
            b = r.read()
            ctype_resp = r.headers.get("Content-Type", "")
            # 二进制响应(下载/打包)不解析 JSON,只看状态码与字节数
            if "json" not in ctype_resp:
                return r.status, {"_binary": len(b), "_ctype": ctype_resp}
            return r.status, (json.loads(b.decode("utf-8")) if b else None)
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, b[:200]


def hit(label, method, path, body=None, expect=None, ctype=None, raw_body=None):
    st, r = raw(method, path, body, ctype, raw_body)
    bad = st >= 500
    unexpected = expect is not None and st != expect
    mark = "FAIL" if (bad or unexpected) else " OK "
    if bad or unexpected:
        FAIL.append(f"{label} [{method} {path}] -> {st} {str(r)[:160]}")
    print(f"  {mark} {st:3d}  {label:38s} {method:6s} {path}"
          + (f"  (期望 {expect})" if unexpected else ""))
    return st, r


def upload_file(pid, folder_id, name, content):
    """raw body 上传:请求体即文件,元数据走 query string(见 files.py upload_file)"""
    qs = "?projectId=" + urllib.parse.quote(pid) + "&name=" + urllib.parse.quote(name)
    if folder_id:
        qs += "&folderId=" + urllib.parse.quote(folder_id)
    st, r = raw("POST", "/api/files/upload" + qs, raw_body=content)
    return r


print("=== 认证 ===")
hit("认证状态", "GET", "/api/auth/status", expect=200)
hit("初始化(已初始化)", "POST", "/api/auth/bootstrap",
    {"email": "x@y.z", "name": "x", "password": "12345678"}, expect=403)
hit("登录", "POST", "/api/auth/login",
    {"email": "admin@teamdoc.local", "password": "admin12345"}, expect=200)
hit("当前用户", "GET", "/api/auth/me", expect=200)
hit("登录失败", "POST", "/api/auth/login",
    {"email": "admin@teamdoc.local", "password": "wrong"}, expect=401)
hit("参数校验(空 body)", "POST", "/api/auth/login", {}, expect=400)
hit("TOTP setup(密码错)", "POST", "/api/auth/totp/setup", {"password": "bad"}, expect=403)
hit("TOTP enable(未 setup)", "POST", "/api/auth/totp/enable", {"code": "123456"}, expect=400)
hit("TOTP disable", "POST", "/api/auth/totp/disable", {"password": "admin12345"}, expect=200)
hit("PAT 列表", "GET", "/api/auth/pats", expect=200)

print("\n=== 用户管理 ===")
hit("用户列表", "GET", "/api/users", expect=200)
st, nu = hit("建用户", "POST", "/api/users",
             {"email": SMOKE_EMAIL, "name": "冒烟", "password": "smoke12345"}, expect=200)
uid = nu["id"] if isinstance(nu, dict) and "id" in nu else None
hit("建用户(重复邮箱)", "POST", "/api/users",
    {"email": SMOKE_EMAIL, "name": "冒烟", "password": "smoke12345"}, expect=409)
hit("建用户(弱密码)", "POST", "/api/users",
    {"email": "w@teamdoc.local", "name": "弱", "password": "123"}, expect=400)
hit("改用户", "PATCH", f"/api/users/{uid}", {"name": "冒烟2"}, expect=200)
hit("改用户(不存在)", "PATCH", "/api/users/nope", {"name": "x"}, expect=404)

print("\n=== 项目 ===")
st, proj = hit("建项目", "POST", "/api/projects", {"name": "冒烟项目", "description": "d"}, expect=200)
pid = proj["id"]
hit("项目列表", "GET", "/api/projects", expect=200)
hit("项目列表(all=1)", "GET", "/api/projects?all=1", expect=200)
hit("项目详情", "GET", f"/api/projects/{pid}", expect=200)
hit("项目详情(不存在)", "GET", "/api/projects/nope", expect=404)
hit("改项目", "PATCH", f"/api/projects/{pid}", {"name": "冒烟项目2"}, expect=200)
hit("建项目(空名)", "POST", "/api/projects", {"name": ""}, expect=400)

print("\n=== 成员 ===")
hit("成员列表", "GET", f"/api/projects/{pid}/members", expect=200)
hit("加成员", "POST", f"/api/projects/{pid}/members",
    {"email": SMOKE_EMAIL, "role": "EDITOR"}, expect=200)
hit("加成员(重复)", "POST", f"/api/projects/{pid}/members",
    {"email": SMOKE_EMAIL, "role": "EDITOR"}, expect=409)
hit("加成员(用户不存在)", "POST", f"/api/projects/{pid}/members",
    {"email": "ghost@teamdoc.local", "role": "VIEWER"}, expect=404)
hit("加成员(非法角色)", "POST", f"/api/projects/{pid}/members",
    {"email": SMOKE_EMAIL, "role": "BOSS"}, expect=400)
hit("改成员角色", "PATCH", f"/api/projects/{pid}/members/{uid}", {"role": "VIEWER"}, expect=200)
hit("改成员(不存在)", "PATCH", f"/api/projects/{pid}/members/nope", {"role": "VIEWER"}, expect=404)
# 唯一 OWNER 降级保护:当前项目 OWNER 是管理员本人,尝试把自己降为 ADMIN 应 409
st, me = raw("GET", "/api/auth/me")
my_uid = me["user"]["id"]
hit("降级唯一 OWNER(保护)", "PATCH", f"/api/projects/{pid}/members/{my_uid}",
    {"role": "ADMIN"}, expect=409)
hit("移除唯一 OWNER(保护)", "DELETE", f"/api/projects/{pid}/members/{my_uid}", expect=409)
hit("移除成员", "DELETE", f"/api/projects/{pid}/members/{uid}", expect=200)
hit("移除成员(不存在)", "DELETE", f"/api/projects/{pid}/members/nope", expect=404)

print("\n=== 文档 ===")
st, doc = hit("建文档", "POST", f"/api/projects/{pid}/docs", {"title": "冒烟文档"}, expect=200)
did = doc["id"]
hit("文档树", "GET", f"/api/projects/{pid}/docs/tree", expect=200)
hit("回收站", "GET", f"/api/projects/{pid}/trash", expect=200)
hit("读文档", "GET", f"/api/docs/{did}", expect=200)
hit("读文档(不存在)", "GET", "/api/docs/nope", expect=404)
hit("改文档", "PATCH", f"/api/docs/{did}", {"title": "冒烟文档2"}, expect=200)
hit("写内容", "PUT", f"/api/docs/{did}/content", {"content": "# 标题\n\n正文 `code`\n"}, expect=200)
hit("写内容(同内容)", "PUT", f"/api/docs/{did}/content", {"content": "# 标题\n\n正文 `code`\n"}, expect=200)
hit("追加内容", "POST", f"/api/docs/{did}/append", {"content": "追加段落"}, expect=200)
hit("反链", "GET", f"/api/docs/{did}/backlinks", expect=200)
st, vers = hit("版本列表", "GET", f"/api/docs/{did}/versions", expect=200)
if vers:
    vid = vers[0]["id"]
    hit("读版本", "GET", f"/api/docs/{did}/versions/{vid}", expect=200)
    hit("恢复版本", "POST", f"/api/docs/{did}/versions/{vid}/restore", expect=200)
hit("读版本(不存在)", "GET", f"/api/docs/{did}/versions/nope", expect=404)
hit("写内容(非字符串)", "PUT", f"/api/docs/{did}/content", {"content": 123}, expect=400)
hit("建子文档", "POST", f"/api/projects/{pid}/docs", {"title": "子", "parentId": did}, expect=200)

print("\n=== 云空间 ===")
hit("文件列表", "GET", f"/api/files?project_id={pid}", expect=200)
st, fold = hit("建文件夹", "POST", "/api/files/folders", {"name": "夹", "projectId": pid}, expect=200)
fid = fold["id"]
st, fold_nested = hit("建文件夹(子夹,带 parentId)", "POST", "/api/files/folders",
                      {"name": "夹2", "projectId": pid, "parentId": fid}, expect=200)
fid_nested = fold_nested["id"]
hit("建文件夹(空名)", "POST", "/api/files/folders", {"name": "", "projectId": pid}, expect=400)
st, fold2 = hit("建文件夹(用于上传)", "POST", "/api/files/folders",
                {"name": "上传夹", "projectId": pid}, expect=200)
fid2 = fold2["id"]
up = upload_file(pid, fid2, "smoke.txt", b"hello smoke")
print(f"   OK  {200 if up and 'id' in up else 'ERR'}  上传文件                                POST   /api/files/upload")
if not (up and "id" in up):
    FAIL.append("上传文件失败: " + str(up))
f_id = up["id"] if up and "id" in up else None
hit("文件列表(目录内)", "GET", f"/api/files?project_id={pid}&folder_id={fid2}", expect=200)
hit("重命名文件", "PATCH", f"/api/files/{f_id}", {"name": "smoke2.txt"}, expect=200)
hit("重命名文件夹", "PATCH", f"/api/files/folders/{fid2}", {"name": "上传夹改"}, expect=200)
hit("下载文件", "GET", f"/api/files/{f_id}/download", expect=200)
hit("下载文件(inline)", "GET", f"/api/files/{f_id}/download?inline=1", expect=200)
hit("下载(不存在)", "GET", "/api/files/nope/download", expect=404)
hit("打包下载", "GET", f"/api/files/zip?ids={f_id}", expect=200)
hit("打包(空 ids)", "GET", "/api/files/zip", expect=400)
hit("移动文件(目标=自身)", "POST", f"/api/files/{f_id}/move", {"projectId": pid}, expect=200)
hit("删文件", "DELETE", f"/api/files/{f_id}", expect=200)
hit("恢复文件", "POST", f"/api/files/{f_id}/restore", expect=200)
hit("删文件(再)", "DELETE", f"/api/files/{f_id}", expect=200)
hit("彻底删文件", "DELETE", f"/api/files/{f_id}/permanent", expect=200)
hit("彻底删文件(再)", "DELETE", f"/api/files/{f_id}/permanent", expect=404)
# fid 含子夹 fid_nested:删除文件夹 = 递归软删整棵子树(历史语义是"非空拒删")
hit("删非空父夹 -> 200(递归)", "DELETE", f"/api/files/folders/{fid}", expect=200)
hit("恢复父夹(整棵子树)", "POST", f"/api/files/folders/{fid}/restore", expect=200)
hit("删子夹", "DELETE", f"/api/files/folders/{fid_nested}", expect=200)
hit("恢复子夹", "POST", f"/api/files/folders/{fid_nested}/restore", expect=200)
hit("删父夹(再,递归)", "DELETE", f"/api/files/folders/{fid}", expect=200)
hit("彻底删父夹(级联)", "DELETE", f"/api/files/folders/{fid}/permanent", expect=200)
hit("彻底删父夹(再) -> 404", "DELETE", f"/api/files/folders/{fid}/permanent", expect=404)
# 文件夹移动:同项目 EDITOR 即可;移入自己的后代拒绝
hit("文件夹树", "GET", f"/api/projects/{pid}/folders/tree", expect=200)
st, mv_a = hit("建移动源夹", "POST", "/api/files/folders", {"projectId": pid, "name": "移动源"}, expect=200)
st, mv_b = hit("建移动目标夹", "POST", "/api/files/folders", {"projectId": pid, "name": "移动目标"}, expect=200)
hit("项目内移动夹", "POST", f"/api/files/folders/{mv_a['id']}/move",
    {"projectId": pid, "parentId": mv_b["id"]}, expect=200)
hit("移入自己后代 -> 409", "POST", f"/api/files/folders/{mv_b['id']}/move",
    {"projectId": pid, "parentId": mv_a["id"]}, expect=409)
hit("移动夹(不存在)", "POST", "/api/files/folders/nope/move", {"projectId": pid}, expect=404)
hit("文件夹树(不存在项目)", "GET", "/api/projects/nope/folders/tree", expect=404)
hit("删文件夹(不存在)", "DELETE", "/api/files/folders/nope", expect=404)

print("\n=== 搜索 ===")
# 中文查询必须 percent-encode(urllib 不接受非 ASCII URL)
Q = urllib.parse.quote("冒烟")
hit("搜索(全部)", "GET", f"/api/search?q={Q}", expect=200)
hit("搜索(文档)", "GET", f"/api/search?q={Q}&type=docs", expect=200)
hit("搜索(文件)", "GET", "/api/search?q=smoke&type=files", expect=200)
hit("搜索(空)", "GET", "/api/search", expect=200)
hit("搜索(非法 type 回落 all)", "GET", "/api/search?q=smoke&type=bogus", expect=200)

print("\n=== 清理 ===")
hit("删测试项目", "DELETE", f"/api/projects/{pid}", expect=200)
if uid:
    hit("禁用测试用户", "PATCH", f"/api/users/{uid}", {"isDisabled": True}, expect=200)

print("\n" + "=" * 60)
if FAIL:
    print(f"!! {len(FAIL)} 项失败:")
    for f in FAIL:
        print("   - " + f)
    sys.exit(1)
print("全部端点无 5xx,冒烟通过")
