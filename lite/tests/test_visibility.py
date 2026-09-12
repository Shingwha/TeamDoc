"""公开项目与单文件公开。

公开项目的语义:**本实例所有登录用户可只读浏览**,不产生成员关系。
鉴权上一处改动(auth.project_role 里"公开且非成员 → VIEWER")让文档树、文档读写、
云空间、搜索、WS 全部自动生效;但前端必须同时拿到 isMember 才能正确渲染
(否则会把访客当成员,渲染出点了就 403 的写按钮)。

覆盖:
  1. 私有项目:非成员读 → 403(默认行为不变)
  2. 公开开关的权限:需 ADMIN
  3. 公开项目:非成员可读文档树/文档内容/文件列表;写全拒
  4. isMember 正确区分"成员"与"公开项目访客"(两者 myRole 都是 VIEWER)
  5. 个人空间**不可公开**(硬拒),且不出现在广场
  6. 单文件公开:非成员可下载该文件,但同项目其他未公开文件仍 403
  7. 广场只列公开项目,按最近活跃倒序
  8. 搜索把公开项目内容并入(避免"广场看得到、搜不到");最近动态仅限已参加项目
  9. 关闭公开后,非成员立即失去访问

各测试自建项目与账号,互不依赖。
"""
import urllib.parse

from _harness import Client, make_user


def _outsider(base_url, admin):
    email, _ = make_user(admin, "vis-out", "pass12345")
    c = Client(base_url)
    c.login(email, "pass12345")
    return c


def test_public_project_readonly_semantics(base_url, admin):
    """场景 1-4:私有拒 → 公开可读 → 写全拒 → 成员不受影响(同一公开开关生命周期)。"""
    pid = admin.post("/api/projects", {"name": "可见性测试项目", "description": "d"}).data["id"]
    did = admin.post(f"/api/projects/{pid}/docs", {"title": "公开前的文档"}).data["id"]
    admin.put(f"/api/docs/{did}/content", {"content": "# 内容\n\n机密内容在这里。"})
    f_priv = admin.upload(pid, "私有文件.txt", b"private data").data
    admin.upload(pid, "公开文件.txt", b"public data")
    out = _outsider(base_url, admin)

    # === 私有项目(默认)—— 非成员一律拒绝 ===
    assert out.get(f"/api/projects/{pid}").status == 403, "非成员读项目详情 → 403"
    assert out.get(f"/api/projects/{pid}/docs/tree").status == 403, "非成员读文档树 → 403"
    assert out.get(f"/api/docs/{did}").status == 403, "非成员读文档 → 403"
    assert out.get(f"/api/files?project_id={pid}").status == 403, "非成员列文件 → 403"
    assert out.get(f"/api/files/{f_priv['id']}/download").status == 403, "非成员下载文件 → 403"

    # === 公开开关(需 ADMIN) ===
    viewer_email, viewer = make_user(admin, "vis-view", "pass12345")
    admin.post(f"/api/projects/{pid}/members", {"userId": viewer["id"], "role": "VIEWER"})
    viewer_c = Client(base_url)
    viewer_c.login(viewer_email, "pass12345")
    assert viewer_c.patch(f"/api/projects/{pid}", {"isPublic": True}).status == 403, \
        "VIEWER 改公开 → 403(需 ADMIN)"
    r = admin.patch(f"/api/projects/{pid}", {"isPublic": True})
    assert r.status == 200 and r.data.get("isPublic") is True, f"ADMIN 公开项目 → 200: {r.data}"

    # === 公开后非成员只读可访问,写仍被拒 ===
    r = out.get(f"/api/projects/{pid}")
    p2 = r.data
    assert r.status == 200, f"非成员读项目详情 → 200: {r.status}"
    assert p2.get("myRole") == "VIEWER", f"访客 myRole=VIEWER: {p2.get('myRole')}"
    assert p2.get("isMember") is False, \
        f"访客 isMember=false(**前端靠它区分**): {p2.get('isMember')}"
    assert out.get(f"/api/projects/{pid}/docs/tree").status == 200, "非成员读文档树 → 200"
    r = out.get(f"/api/docs/{did}")
    assert r.status == 200 and "机密内容" in (r.data or {}).get("content", ""), \
        f"非成员读文档内容 → 200: {r.status}"
    r = out.get(f"/api/files?project_id={pid}")
    assert r.status == 200, f"非成员列文件 → 200: {r.status}"
    assert any(f["name"] == "私有文件.txt" for f in (r.data or {}).get("files", [])), \
        "文件列表含未公开的文件(项目公开则项目内文件都可读)"
    assert out.get(f"/api/files/{f_priv['id']}/download").status == 200, \
        "非成员下载项目内文件 → 200"
    # 写操作必须全拒
    assert out.post(f"/api/projects/{pid}/docs", {"title": "访客想建文档"}).status == 403, \
        "访客建文档 → 403"
    assert out.put(f"/api/docs/{did}/content", {"content": "篡改"}).status == 403, \
        "访客改文档内容 → 403"
    assert out.delete(f"/api/docs/{did}").status == 403, "访客删文档 → 403"
    assert out.upload(pid, "x.txt", b"x").status == 403, "访客上传 → 403"
    assert out.delete(f"/api/files/{f_priv['id']}").status == 403, "访客删文件 → 403"
    assert out.post("/api/files/folders", {"projectId": pid, "name": "访客目录"}).status == 403, \
        "访客建文件夹 → 403"
    assert out.patch(f"/api/projects/{pid}", {"name": "改名"}).status == 403, "访客改项目 → 403"
    assert out.get(f"/api/projects/{pid}/members").status == 200, \
        "访客可读成员列表(公开项目橱窗的一部分)"

    # === 成员未受影响 ===
    pm = viewer_c.get(f"/api/projects/{pid}").data
    assert pm.get("isMember") is True, f"成员 isMember=true: {pm.get('isMember')}"
    assert pm.get("myRole") == "VIEWER" and pm.get("isMember") is True, \
        f"成员 myRole=VIEWER 且 isMember 为真(与访客可区分): {pm}"
    assert viewer_c.post(f"/api/projects/{pid}/docs",
                         {"title": "成员不能建(VIEWER)"}).status == 403, \
        "VIEWER 成员建文档 → 403"


def test_personal_space_cannot_be_public(admin):
    personal = next((x for x in admin.get("/api/projects").data if x["isPersonal"]), None)
    assert personal is not None, "找到自己的个人空间"
    r = admin.patch(f"/api/projects/{personal['id']}", {"isPublic": True})
    assert r.status == 403, f"个人空间公开 → 403: {r.status} {r.data}"
    r = admin.patch(f"/api/projects/{personal['id']}", {"isPublic": False})
    assert r.status == 403, f"个人空间设私有也拒(硬拒而非仅拦公开): {r.status}"
    disc = admin.get("/api/discover/projects").data
    assert not any(x["isPersonal"] for x in (disc or [])), \
        f"广场里没有任何个人空间: {[x['name'] for x in disc[:5]]}"


def test_single_file_public(base_url, admin):
    out = _outsider(base_url, admin)
    pid = admin.post("/api/projects", {"name": "单文件公开测试", "description": "d"}).data["id"]
    fp1 = admin.upload(pid, "共享给同事.txt", b"shared").data
    fp2 = admin.upload(pid, "不共享.txt", b"secret").data
    assert out.get(f"/api/files/{fp1['id']}/download").status == 403, \
        "项目私有时非成员下载 → 403"
    r = admin.patch(f"/api/files/{fp1['id']}", {"isPublic": True})
    assert r.status == 200 and r.data.get("isPublic") is True, \
        f"标记文件公开 → 200: {r.status} {r.data}"
    assert out.get(f"/api/files/{fp1['id']}/download").status == 200, "公开后非成员可下载 → 200"
    assert out.get(f"/api/files/{fp2['id']}/download").status == 403, \
        "同项目未公开文件仍 403(只放开这一个)"
    assert out.get(f"/api/files?project_id={pid}").status == 403, \
        "项目本身仍是私有(列表 403)"
    assert admin.patch(f"/api/files/{fp1['id']}", {"isPublic": False}).status == 200, \
        "取消公开 → 200"
    assert out.get(f"/api/files/{fp1['id']}/download").status == 403, \
        "取消后非成员立即失去访问"


def test_discover_plaza_ordering(base_url, admin):
    out = _outsider(base_url, admin)
    pid = admin.post("/api/projects", {"name": "广场旧公开项目", "description": "d"}).data["id"]
    admin.patch(f"/api/projects/{pid}", {"isPublic": True})
    pid_priv = admin.post("/api/projects", {"name": "广场私有项目", "description": "d"}).data["id"]

    disc = out.get("/api/discover/projects")
    assert disc.status == 200, f"广场可读: {disc.status}"
    ids = [x["id"] for x in (disc.data or [])]
    assert pid in ids, f"含刚公开的项目: {ids[:5]}"
    assert pid_priv not in ids, f"不含私有项目: {ids[:5]}"
    assert all(x["isPublic"] for x in (disc.data or [])), "每项都标 isPublic"

    # 活跃排序:再公开一个并制造更新的活跃,它应排更前
    pid3 = admin.post("/api/projects", {"name": "更新的公开项目", "description": "d"}).data["id"]
    admin.patch(f"/api/projects/{pid3}", {"isPublic": True})
    admin.post(f"/api/projects/{pid3}/docs", {"title": "刚建的文档"})
    ids2 = [x["id"] for x in out.get("/api/discover/projects").data]
    assert ids2 and ids2[0] == pid3, f"最近活跃的排在前面: {ids2[:4]}"


def test_search_merges_public_recent_does_not(base_url, admin):
    """搜索并入公开项目(避免"广场看得到、搜不到");最近动态仅限已参加项目。"""
    out = _outsider(base_url, admin)
    pid_pub = admin.post("/api/projects", {"name": "公开可搜项目", "description": "d"}).data["id"]
    admin.patch(f"/api/projects/{pid_pub}", {"isPublic": True})
    doc = admin.post(f"/api/projects/{pid_pub}/docs", {"title": "公开文档"}).data
    admin.put(f"/api/docs/{doc['id']}/content", {"content": "# 内容\n\n机密内容在这里。"})
    pid_priv = admin.post("/api/projects", {"name": "私有不可搜项目", "description": "d"}).data["id"]
    admin.upload(pid_priv, "绝密文件.txt", b"top secret")

    s = out.get("/api/search?q=" + urllib.parse.quote("机密内容")).data
    assert len(s.get("docs", [])) >= 1, f"非成员能搜到公开项目里的文档: {str(s.get('docs'))[:120]}"
    s2 = out.get("/api/search?q=" + urllib.parse.quote("绝密文件")).data
    assert all(f["name"] != "绝密文件.txt" for f in s2.get("files", [])), \
        f"私有项目内容搜不到: {str(s2.get('files'))[:120]}"
    # 最新动态是"我的工作台"视角:outsider 没参加任何项目,公开项目也不该出现
    r = out.get("/api/recent")
    assert r.status == 200, f"最近接口 200: {r.status}"
    assert not r.data.get("files") and not r.data.get("docs"), \
        f"未参加任何项目 → 最近动态为空(公开项目也不并入): {str(r.data)[:160]}"


def test_close_public_revokes_access(base_url, admin):
    out = _outsider(base_url, admin)
    pid = admin.post("/api/projects", {"name": "关闭公开测试", "description": "d"}).data["id"]
    did = admin.post(f"/api/projects/{pid}/docs", {"title": "待关闭文档"}).data["id"]
    admin.patch(f"/api/projects/{pid}", {"isPublic": True})
    assert out.get(f"/api/projects/{pid}").status == 200, "公开期间访客可读"

    assert admin.patch(f"/api/projects/{pid}", {"isPublic": False}).status == 200, "关闭公开 → 200"
    assert out.get(f"/api/projects/{pid}").status == 403, "关闭后非成员读项目 → 403"
    assert out.get(f"/api/docs/{did}").status == 403, "关闭后非成员读文档 → 403"
    ids = [x["id"] for x in (out.get("/api/discover/projects").data or [])]
    assert pid not in ids, "关闭后从广场消失"
