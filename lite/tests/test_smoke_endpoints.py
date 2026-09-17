"""全端点冒烟:遍历所有 API 路由,断言无 5xx 且状态码符合契约(NameError/TypeError 类回归的护栏)。

职责边界:只保证"每个路由都活着、返回契约状态码"。深度语义场景(文件夹递归删除/级联/
移动、孤儿清理、备份恢复)已下沉到各自的专项测试,这里不重复。

可重复运行:测试用户邮箱带随机后缀。此前用固定邮箱,而系统没有"删除用户"接口
(只能禁用),第二次运行必然撞 409 并让后续断言全用 None 作 id 连带失败 ——
看起来像服务端 bug,其实是脚本不可重入。
"""
import urllib.parse

from _harness import ABSENT_ID, Client, rand_email


def test_smoke_all_endpoints(base_url, admin):
    """失败先收集再汇总:一次跑完看到全部问题(这是冒烟,不是单点断言)。"""
    bad = []

    def hit(label, method, path, body=None, expect=None, who=None, **kw):
        c = who or admin
        r = c.request(method, path, body=body, **kw)
        problems = []
        if r.status >= 500:
            problems.append("5xx")
        if expect is not None and r.status != expect:
            problems.append(f"期望 {expect}")
        if problems:
            bad.append(f"{label} [{method} {path}] -> {r.status} "
                       f"{str(r.data)[:160]}({'、'.join(problems)})")
        return r

    # === 认证 ===
    hit("认证状态", "GET", "/api/auth/status", expect=200)
    hit("初始化(已初始化)", "POST", "/api/auth/bootstrap",
        {"email": "x@y.z", "name": "x", "password": "12345678"}, expect=403)
    hit("登录", "POST", "/api/auth/login",
        {"email": "admin@teamdoc.local", "password": "admin12345"}, expect=200)
    hit("当前用户", "GET", "/api/auth/me", expect=200)
    hit("登录失败", "POST", "/api/auth/login",
        {"email": "admin@teamdoc.local", "password": "wrong"}, expect=401)
    hit("参数校验(空 body)", "POST", "/api/auth/login", {}, expect=400)
    hit("PAT 列表", "GET", "/api/auth/pats", expect=200)

    # === 用户管理 ===
    hit("用户列表", "GET", "/api/users", expect=200)
    smoke_email = rand_email("smoke")
    r = hit("建用户", "POST", "/api/users",
            {"email": smoke_email, "name": "冒烟", "password": "smoke12345"}, expect=200)
    uid = r.data["id"] if isinstance(r.data, dict) and "id" in r.data else None
    hit("建用户(重复邮箱)", "POST", "/api/users",
        {"email": smoke_email, "name": "冒烟", "password": "smoke12345"}, expect=409)
    hit("建用户(弱密码)", "POST", "/api/users",
        {"email": "w@teamdoc.local", "name": "弱", "password": "123"}, expect=400)
    hit("改用户", "PATCH", f"/api/users/{uid}", {"name": "冒烟2"}, expect=200)
    hit("改用户(不存在)", "PATCH", f"/api/users/{ABSENT_ID}", {"name": "x"}, expect=404)
    hit("改用户(非法 id)", "PATCH", "/api/users/abc", {"name": "x"}, expect=400)

    # === 项目 ===
    pid = hit("建项目", "POST", "/api/projects", {"name": "冒烟项目", "description": "d"},
              expect=200).data["id"]
    hit("项目列表", "GET", "/api/projects", expect=200)
    hit("项目列表(all=1)", "GET", "/api/projects?all=1", expect=200)
    hit("项目详情", "GET", f"/api/projects/{pid}", expect=200)
    hit("项目详情(不存在)", "GET", f"/api/projects/{ABSENT_ID}", expect=404)
    hit("改项目", "PATCH", f"/api/projects/{pid}", {"name": "冒烟项目2"}, expect=200)
    hit("建项目(空名)", "POST", "/api/projects", {"name": ""}, expect=400)

    # === 成员 ===
    hit("成员列表", "GET", f"/api/projects/{pid}/members", expect=200)
    hit("加成员", "POST", f"/api/projects/{pid}/members",
        {"userId": uid, "role": "EDITOR"}, expect=200)
    hit("加成员(重复)", "POST", f"/api/projects/{pid}/members",
        {"userId": uid, "role": "EDITOR"}, expect=409)
    hit("加成员(用户不存在)", "POST", f"/api/projects/{pid}/members",
        {"userId": ABSENT_ID, "role": "VIEWER"}, expect=404)
    hit("加成员(非法角色)", "POST", f"/api/projects/{pid}/members",
        {"userId": uid, "role": "BOSS"}, expect=400)
    hit("改成员角色", "PATCH", f"/api/projects/{pid}/members/{uid}", {"role": "VIEWER"},
        expect=200)
    hit("改成员(不存在)", "PATCH", f"/api/projects/{pid}/members/{ABSENT_ID}", {"role": "VIEWER"},
        expect=404)
    hit("改成员(非法 id)", "PATCH", f"/api/projects/{pid}/members/abc", {"role": "VIEWER"},
        expect=400)
    # 唯一 OWNER 降级保护:当前项目 OWNER 是管理员本人,尝试把自己降为 ADMIN 应 409
    my_uid = admin.get("/api/auth/me").data["user"]["id"]
    hit("降级唯一 OWNER(保护)", "PATCH", f"/api/projects/{pid}/members/{my_uid}",
        {"role": "ADMIN"}, expect=409)
    hit("移除唯一 OWNER(保护)", "DELETE", f"/api/projects/{pid}/members/{my_uid}", expect=409)
    hit("移除成员", "DELETE", f"/api/projects/{pid}/members/{uid}", expect=200)
    hit("移除成员(不存在)", "DELETE", f"/api/projects/{pid}/members/{ABSENT_ID}", expect=404)

    # === 自助退出(冒烟用户自己的会话,与管理员会话分开) ===
    hit("再加成员(自助退出用)", "POST", f"/api/projects/{pid}/members",
        {"userId": uid, "role": "EDITOR"}, expect=200)
    smoke = Client(base_url)
    smoke.login(smoke_email, "smoke12345")
    plist = smoke.get("/api/projects")
    personal_pid = next((p["id"] for p in (plist.data or []) if p.get("isPersonal")), None)
    if personal_pid:
        hit("自助退出(个人空间)", "POST", f"/api/projects/{personal_pid}/leave", expect=403)
    hit("自助退出", "POST", f"/api/projects/{pid}/leave", expect=200, who=smoke)
    # 退出后已非成员:私有项目在权限层就被拦(403,不暴露项目存在性)
    hit("自助退出(私有项目已非成员)", "POST", f"/api/projects/{pid}/leave", expect=403, who=smoke)
    hit("临时公开项目", "PATCH", f"/api/projects/{pid}", {"isPublic": True}, expect=200)
    # 公开项目对非成员就是"可加入但不可读":权限层直接拦(403 JOIN_REQUIRED),
    # 端点内那条"非成员"分支(404)已不可达 —— join 才是自助入口
    hit("自助退出(公开项目未加入)", "POST", f"/api/projects/{pid}/leave", expect=403, who=smoke)
    hit("自助加入(公开项目)", "POST", f"/api/projects/{pid}/join", expect=200, who=smoke)
    hit("自助加入(已是成员)", "POST", f"/api/projects/{pid}/join", expect=409, who=smoke)
    hit("自助退出(加入后)", "POST", f"/api/projects/{pid}/leave", expect=200, who=smoke)
    hit("恢复私有", "PATCH", f"/api/projects/{pid}", {"isPublic": False}, expect=200)
    hit("自助退出(唯一 OWNER 保护)", "POST", f"/api/projects/{pid}/leave", expect=409)

    # === 管理后台项目 ===
    # 全局管理员是信任根,拥有 OWNER 级管辖权(auth.is_project_owner_or_admin):
    # 可看全站项目总览、可授 OWNER、可删他人项目(接管语义);个人空间保护不变。
    sproj_id = hit("冒烟用户建项目", "POST", "/api/projects", {"name": "冒烟接管项目"},
                   expect=200, who=smoke).data["id"]
    hit("管理项目总览", "GET", "/api/admin/projects", expect=200)
    hit("诊断快照", "GET", "/api/admin/diagnostics", expect=200)
    # 个人空间不进管理列表(不可管理 + 对管理员保密);占用走存储总览
    r = admin.get("/api/admin/projects")
    bad_personal = [p["name"] for p in (r.data or []) if p.get("isPersonal")]
    if bad_personal:
        bad.append(f"管理项目总览混入个人空间: {bad_personal[:3]}")
    hit("管理项目总览(非管理员)", "GET", "/api/admin/projects", expect=403, who=smoke)
    hit("诊断快照(非管理员)", "GET", "/api/admin/diagnostics", expect=403, who=smoke)
    hit("管理员授 OWNER(豁免)", "POST", f"/api/projects/{sproj_id}/members",
        {"userId": my_uid, "role": "OWNER"}, expect=200)
    sproj2_id = hit("冒烟用户再建项目", "POST", "/api/projects", {"name": "冒烟直删项目"},
                    expect=200, who=smoke).data["id"]
    # 管理员对该项目无任何成员关系,删除走的是 is_admin 豁免而非 OWNER 身份
    hit("管理员直删他人项目(豁免)", "DELETE", f"/api/projects/{sproj2_id}", expect=200)
    hit("管理员删接管项目", "DELETE", f"/api/projects/{sproj_id}", expect=200)
    if personal_pid:
        hit("管理员删个人空间(仍拒绝)", "DELETE", f"/api/projects/{personal_pid}", expect=403)
    # 项目内 ADMIN(非全局)授 OWNER 仍被拒:自我提权风险的主体是项目内角色
    hit("加冒烟用户为项目 ADMIN", "POST", f"/api/projects/{pid}/members",
        {"userId": uid, "role": "ADMIN"}, expect=200)
    hit("项目 ADMIN 授 OWNER(仍拒绝)", "POST", f"/api/projects/{pid}/members",
        {"userId": my_uid, "role": "OWNER"}, expect=403, who=smoke)

    # === 文档 ===
    did = hit("建文档", "POST", f"/api/projects/{pid}/docs", {"title": "冒烟文档"},
              expect=200).data["id"]
    hit("文档树", "GET", f"/api/projects/{pid}/docs/tree", expect=200)
    hit("回收站", "GET", f"/api/projects/{pid}/trash", expect=200)
    ver = hit("读文档", "GET", f"/api/docs/{did}", expect=200).data["version"]
    hit("读文档(不存在)", "GET", f"/api/docs/{ABSENT_ID}", expect=404)
    hit("改文档", "PATCH", f"/api/docs/{did}", {"title": "冒烟文档2"}, expect=200)
    # 正文写入必须声明基线版本(不匹配 409、缺失 400),详见 test_doc_conflict
    hit("写内容", "PUT", f"/api/docs/{did}/content",
        {"content": "# 标题\n\n正文 `code`\n", "baseVersion": ver}, expect=200)
    hit("写内容(同内容)", "PUT", f"/api/docs/{did}/content",
        {"content": "# 标题\n\n正文 `code`\n", "baseVersion": ver}, expect=200)
    hit("写内容(基线过期)", "PUT", f"/api/docs/{did}/content",
        {"content": "覆盖尝试", "baseVersion": ver}, expect=409)
    hit("写内容(web 缺基线)", "PUT", f"/api/docs/{did}/content", {"content": "覆盖尝试"},
        expect=400)
    hit("追加内容", "POST", f"/api/docs/{did}/append", {"content": "追加段落"}, expect=200)
    hit("反链", "GET", f"/api/docs/{did}/backlinks", expect=200)
    vers = hit("版本列表", "GET", f"/api/docs/{did}/versions", expect=200)
    if vers.data:
        vid = vers.data[0]["id"]
        hit("读版本", "GET", f"/api/docs/{did}/versions/{vid}", expect=200)
        hit("恢复版本", "POST", f"/api/docs/{did}/versions/{vid}/restore", expect=200)
    hit("读版本(不存在)", "GET", f"/api/docs/{did}/versions/{ABSENT_ID}", expect=404)
    hit("写内容(非字符串)", "PUT", f"/api/docs/{did}/content",
        {"content": 123, "baseVersion": ver}, expect=400)
    hit("建子文档", "POST", f"/api/projects/{pid}/docs", {"title": "子", "parentId": did},
        expect=200)

    # === 云空间(递归删除/级联/移动语义在 test_folder_recycle,这里只保路由契约) ===
    hit("文件列表", "GET", f"/api/files?project_id={pid}", expect=200)
    fid = hit("建文件夹", "POST", "/api/files/folders", {"name": "夹", "projectId": pid},
              expect=200).data["id"]
    hit("建文件夹(子夹,带 parentId)", "POST", "/api/files/folders",
        {"name": "夹2", "projectId": pid, "parentId": fid}, expect=200)
    hit("建文件夹(空名)", "POST", "/api/files/folders", {"name": "", "projectId": pid},
        expect=400)
    fold2_id = hit("建文件夹(用于上传)", "POST", "/api/files/folders",
                   {"name": "上传夹", "projectId": pid}, expect=200).data["id"]
    up = admin.upload(pid, "smoke.txt", b"hello smoke", folder_id=fold2_id)
    if up.status != 200 or "id" not in (up.data or {}):
        bad.append(f"上传文件失败: {up}")
        f_id = None
    else:
        f_id = up.data["id"]
    hit("文件列表(目录内)", "GET", f"/api/files?project_id={pid}&folder_id={fold2_id}",
        expect=200)
    hit("重命名文件", "PATCH", f"/api/files/{f_id}", {"name": "smoke2.txt"}, expect=200)
    hit("重命名文件夹", "PATCH", f"/api/files/folders/{fold2_id}", {"name": "上传夹改"},
        expect=200)
    hit("下载文件", "GET", f"/api/files/{f_id}/download", expect=200)
    hit("下载文件(inline)", "GET", f"/api/files/{f_id}/download?inline=1", expect=200)
    hit("下载(不存在)", "GET", f"/api/files/{ABSENT_ID}/download", expect=404)
    hit("文件元数据", "GET", f"/api/files/{f_id}/meta", expect=200)
    hit("元数据(不存在)", "GET", f"/api/files/{ABSENT_ID}/meta", expect=404)
    hit("打包下载", "GET", f"/api/files/zip?ids={f_id}", expect=200)
    hit("打包(空 ids)", "GET", "/api/files/zip", expect=400)
    hit("移动文件(目标=自身)", "POST", f"/api/files/{f_id}/move", {"projectId": pid},
        expect=200)
    hit("删文件", "DELETE", f"/api/files/{f_id}", expect=200)
    hit("恢复文件", "POST", f"/api/files/{f_id}/restore", expect=200)
    hit("删文件(再)", "DELETE", f"/api/files/{f_id}", expect=200)
    hit("彻底删文件", "DELETE", f"/api/files/{f_id}/permanent", expect=200)
    hit("彻底删文件(再)", "DELETE", f"/api/files/{f_id}/permanent", expect=404)
    hit("文件夹树", "GET", f"/api/projects/{pid}/folders/tree", expect=200)
    hit("移动夹(不存在)", "POST", f"/api/files/folders/{ABSENT_ID}/move", {"projectId": pid},
        expect=404)
    hit("文件夹树(不存在项目)", "GET", f"/api/projects/{ABSENT_ID}/folders/tree", expect=404)
    hit("删文件夹(不存在)", "DELETE", f"/api/files/folders/{ABSENT_ID}", expect=404)

    # === 搜索(中文查询必须 percent-encode,urllib 不接受非 ASCII URL) ===
    q = urllib.parse.quote("冒烟")
    hit("搜索(全部)", "GET", f"/api/search?q={q}", expect=200)
    hit("搜索(文档)", "GET", f"/api/search?q={q}&type=docs", expect=200)
    hit("搜索(文件)", "GET", "/api/search?q=smoke&type=files", expect=200)
    hit("搜索(空)", "GET", "/api/search", expect=200)
    hit("搜索(非法 type)", "GET", "/api/search?q=smoke&type=bogus", expect=400)

    # === 登录安全(管理端:登录状态 / 会话 / 解锁 / 审计) ===
    hit("登录详情(会话与来源)", "GET", f"/api/admin/users/{uid}/access", expect=200)
    hit("登录动态", "GET", "/api/admin/login-events?limit=5", expect=200)
    hit("解锁(未锁定账号也幂等)", "PATCH", f"/api/users/{uid}", {"unlock": True}, expect=200)
    hit("下线会话(ref 不存在)", "DELETE", "/api/admin/sessions/" + "0" * 64, expect=404)
    hit("登录详情(非管理员)", "GET", f"/api/admin/users/{uid}/access", expect=403, who=smoke)
    hit("登录动态(非管理员)", "GET", "/api/admin/login-events", expect=403, who=smoke)
    hit("下线会话(非管理员)", "DELETE", f"/api/admin/users/{uid}/sessions",
        expect=403, who=smoke)
    # 放在最后:它踢掉该用户全部会话,之后再拿 smoke 客户端断言 403 只会得到 401
    hit("下线用户全部会话", "DELETE", f"/api/admin/users/{uid}/sessions", expect=200)

    # === 节流契约(语义深测在 test_login_throttle,这里只钉住契约) ===
    ghost = rand_email("smoke-ghost")
    for _ in range(4):   # 默认上限 5 次:前 4 次仍给机会(最后 2 次带剩余次数提示)
        hit("登录失败(未达上限)", "POST", "/api/auth/login",
            {"email": ghost, "password": "wrong"}, expect=401)
    r = hit("登录失败(触发节流)", "POST", "/api/auth/login",
            {"email": ghost, "password": "wrong"}, expect=429)
    if not (r.headers.get("retry-after") or "").isdigit():
        bad.append("限流响应缺少 Retry-After 头或不是秒数: "
                   + repr(r.headers.get("retry-after")))

    # === 清理 ===
    hit("删测试项目", "DELETE", f"/api/projects/{pid}", expect=200)
    if uid:
        hit("禁用测试用户", "PATCH", f"/api/users/{uid}", {"isDisabled": True}, expect=200)

    assert not bad, f"{len(bad)} 项失败:\n" + "\n".join("  - " + b for b in bad)
