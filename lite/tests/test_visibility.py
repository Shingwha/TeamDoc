"""公开项目(可发现 + 可自助加入)与单文件公开。

公开项目的语义:**本实例所有登录用户可发现(发现页广场)+ 可自助加入**,
但**加入前完全不可读**。鉴权上没有 public 分支 —— auth.project_role 对非成员一律
返回 None,文档树/正文/历史/云空间/搜索/WS 因此全部自动拒绝,不需要逐端点判公开;
唯一的例外是拒绝文案:公开项目的 403 带 JOIN_REQUIRED,前端据此渲染「加入」入口
(同事把项目/文档链接发过来时,不会撞上一句无从下手的"需要 VIEWER 及以上权限")。

加入后的角色由项目设置 join_role 决定,只可能是 VIEWER / EDITOR(projects.JOIN_ROLES):
自助加入是任何登录用户都能走的路径,拿不到管理权。

覆盖:
  1. 未加入:私有与公开一视同仁,读入口全部 403(公开项目带 JOIN_REQUIRED)
  2. 自助加入:200 → 立即可读、进入「我的项目」;重复加入 409
  3. joinRole 决定加入后的能力:VIEWER 只读 / EDITOR 可写
  4. 加入 → 退出 → 再加入的闭环(退出后立即失去访问)
  5. 私有项目与个人空间不可加入;公开开关与 joinRole 需 ADMIN,值域受校验
  6. 广场只列公开项目、按最近活跃倒序,卡片带 joinRole/isMember 且不含内容
  7. 搜索不含未加入的公开项目(加入后才搜得到);最近动态只含已参加项目
  8. 关闭公开:拦住新加入,但已在册成员不受影响
  9. 单文件公开:与项目可见性无关的独立通道

各测试自建项目与账号,互不依赖。
"""
import urllib.parse

from _harness import ABSENT_ID, Client, make_user


def _outsider(base_url, admin, name="vis-out"):
    """新账号:不参加任何项目(只有个人空间)。"""
    email, _ = make_user(admin, name, "pass12345")
    c = Client(base_url)
    c.login(email, "pass12345")
    return c


def _denied_code(resp):
    return ((resp.data or {}).get("detail") or {}).get("code")


def _read_paths(pid, did=None, fid=None):
    """未加入者能碰到的全部"读"入口:每一处都不得泄露内容。

    did/fid 不给就只查项目级入口(文档/文件端点会因资源不存在先 404,
    那是另一条判定,不该混进权限断言)。
    """
    paths = [
        ("项目详情", f"/api/projects/{pid}"),
        ("文档树", f"/api/projects/{pid}/docs/tree"),
        ("文件列表", f"/api/files?project_id={pid}"),
        ("成员列表", f"/api/projects/{pid}/members"),
        ("回收站", f"/api/projects/{pid}/trash"),
    ]
    if did is not None:
        paths += [("文档正文", f"/api/docs/{did}"), ("历史版本", f"/api/docs/{did}/versions")]
    if fid is not None:
        paths += [("文件下载", f"/api/files/{fid}/download"),
                  ("文件元数据", f"/api/files/{fid}/meta")]
    return paths


def _assert_read_locked(client, pid, did=None, fid=None, code="FORBIDDEN"):
    for label, path in _read_paths(pid, did, fid):
        r = client.get(path)
        assert r.status == 403, f"{label}:未加入 → 403: {r.status} {r.data}"
        assert _denied_code(r) == code, f"{label}:错误码应为 {code}: {_denied_code(r)}"


def _public_project(admin, name, **patch):
    pid = admin.post("/api/projects", {"name": name, "description": "d"}).data["id"]
    r = admin.patch(f"/api/projects/{pid}", {"isPublic": True, **patch})
    assert r.status == 200, f"公开项目 → 200: {r.status} {r.data}"
    return pid


def test_unjoined_cannot_read_public_project(base_url, admin):
    """场景 1:公开 ≠ 可读。未加入时公开项目与私有项目在鉴权上没有区别,
    唯一的差别是拒绝文案(JOIN_REQUIRED 说得清出路)。"""
    pid = admin.post("/api/projects", {"name": "公开待加入", "description": "d"}).data["id"]
    d1 = admin.post(f"/api/projects/{pid}/docs", {"title": "加入前的文档"}).data
    did = d1["id"]
    admin.put(f"/api/docs/{did}/content",
              {"content": "# 内容\n\n机密内容在这里。", "baseVersion": d1["version"]})
    fid = admin.upload(pid, "机密文件.txt", b"secret").data["id"]
    out = _outsider(base_url, admin)

    _assert_read_locked(out, pid, did, fid)                   # 私有(默认):通用拒绝
    admin.patch(f"/api/projects/{pid}", {"isPublic": True})
    _assert_read_locked(out, pid, did, fid, "JOIN_REQUIRED")  # 公开但未加入:说得清出路

    # 广场是唯一能看到它的地方,且只给项目本身(描述/计数),不下一分内容
    card = next((x for x in (out.get("/api/discover/projects").data or [])
                 if x["id"] == pid), None)
    assert card is not None, "公开项目出现在广场"
    assert card["isMember"] is False and card["myRole"] is None, f"未加入者的卡片: {card}"
    assert card["joinRole"] == "VIEWER", f"卡片带 joinRole(默认只读): {card}"


def test_self_join_grants_access_and_lists_project(base_url, admin):
    """场景 2:自助加入 → 立即能读,项目进入「我的项目」(侧栏/项目列表的数据源)。"""
    pid = _public_project(admin, "自助加入项目")
    did = admin.post(f"/api/projects/{pid}/docs", {"title": "加入后可见"}).data["id"]
    admin.upload(pid, "加入后可见.txt", b"hello")
    out = _outsider(base_url, admin)
    assert pid not in [p["id"] for p in out.get("/api/projects").data], "加入前不在我的项目里"

    r = out.post(f"/api/projects/{pid}/join")
    assert r.status == 200, f"自助加入 → 200: {r.status} {r.data}"
    assert r.data.get("isMember") is True and r.data.get("myRole") == "VIEWER", \
        f"加入后按 join_role 成为真成员(默认 VIEWER): {r.data}"
    assert out.get(f"/api/projects/{pid}").status == 200, "加入后项目详情可读"
    assert out.get(f"/api/projects/{pid}/docs/tree").status == 200, "加入后文档树可读"
    assert out.get(f"/api/docs/{did}").status == 200, "加入后文档正文可读"
    assert out.get(f"/api/files?project_id={pid}").status == 200, "加入后文件列表可读"
    assert out.get(f"/api/projects/{pid}/members").status == 200, "加入后成员列表可读"
    assert pid in [p["id"] for p in out.get("/api/projects").data], "加入后进入我的项目列表"
    assert out.post(f"/api/projects/{pid}/join").status == 409, "重复加入 → 409"


def test_join_role_decides_write_ability(base_url, admin):
    """场景 3:joinRole 是"加入后能做什么"的唯一来源:VIEWER 只读,EDITOR 可写。"""
    p_view = _public_project(admin, "只读加入项目")
    p_edit = _public_project(admin, "可编辑加入项目", joinRole="EDITOR")
    assert admin.get(f"/api/projects/{p_edit}").data.get("joinRole") == "EDITOR", "joinRole 读得回"

    out_v = _outsider(base_url, admin, "vis-v")
    out_e = _outsider(base_url, admin, "vis-e")
    out_v.post(f"/api/projects/{p_view}/join")
    out_e.post(f"/api/projects/{p_edit}/join")

    # 只读成员:能读,不能写
    assert out_v.get(f"/api/projects/{p_view}/docs/tree").status == 200
    assert out_v.post(f"/api/projects/{p_view}/docs", {"title": "只读成员建文档"}).status == 403, \
        "VIEWER 加入后建文档 → 403"
    assert out_v.upload(p_view, "只读成员上传.txt", b"x").status == 403, \
        "VIEWER 加入后上传 → 403"
    # 可编辑成员:能建文档、能上传
    assert out_e.post(f"/api/projects/{p_edit}/docs", {"title": "可编辑成员建文档"}).status == 200, \
        "EDITOR 加入后建文档 → 200"
    assert out_e.upload(p_edit, "可编辑成员上传.txt", b"x").status == 200, \
        "EDITOR 加入后上传 → 200"


def test_join_leave_roundtrip(base_url, admin):
    """场景 4:退出后立即失去访问,且可以再次自助加入(进出闭环)。"""
    pid = _public_project(admin, "进出闭环项目")
    did = admin.post(f"/api/projects/{pid}/docs", {"title": "闭环文档"}).data["id"]
    fid = admin.upload(pid, "闭环文件.txt", b"x").data["id"]
    out = _outsider(base_url, admin)
    assert out.post(f"/api/projects/{pid}/join").status == 200, "加入 → 200"
    assert out.get(f"/api/projects/{pid}/docs/tree").status == 200, "加入后可读"

    assert out.post(f"/api/projects/{pid}/leave").status == 200, "退出 → 200"
    _assert_read_locked(out, pid, did, fid, "JOIN_REQUIRED")
    assert out.post(f"/api/projects/{pid}/join").status == 200, "退出后可再次加入"


def test_join_rejected_for_private_and_personal(base_url, admin):
    """场景 5:只有公开项目能自助加入;个人空间与私有项目一律 403。"""
    pid_priv = admin.post("/api/projects", {"name": "私有不可加入", "description": "d"}).data["id"]
    personal = next(x for x in admin.get("/api/projects").data if x["isPersonal"])
    out = _outsider(base_url, admin)

    assert out.post(f"/api/projects/{pid_priv}/join").status == 403, "私有项目不可加入"
    assert out.post(f"/api/projects/{personal['id']}/join").status == 403, "个人空间不可加入"
    assert out.post(f"/api/projects/{ABSENT_ID}/join").status == 404, "不存在的项目 → 404"
    # id 形状非法是另一回事:它在路由边界就被拦下(400),与"不存在"(404)分开 ——
    # 前者是坏链接,后者是"这个项目没了"
    assert out.post("/api/projects/999999/join").status == 400, "非法 id 形状 → 400"

    # 已被管理员拉进项目的人再自助加入 → 409(成员关系只有一行)。
    # 判定顺序是"不存在 404 → 未公开 403 → 已是成员 409"(权限先于状态,§4.7),
    # 所以这里必须用公开项目,否则先撞上 403
    pid_pub = _public_project(admin, "公开且已加入")
    email, u = make_user(admin, "已加入者", "pass12345")
    c = Client(base_url)
    c.login(email, "pass12345")
    admin.post(f"/api/projects/{pid_pub}/members", {"userId": u["id"], "role": "VIEWER"})
    assert c.post(f"/api/projects/{pid_pub}/join").status == 409, "已是成员 → 409"


def test_join_settings_need_admin_and_valid_role(base_url, admin):
    """场景 5(续):公开开关与加入角色都是项目设置,需 ADMIN;角色值域只有 VIEWER/EDITOR。"""
    pid = admin.post("/api/projects", {"name": "加入设置权限", "description": "d"}).data["id"]
    email, viewer = make_user(admin, "设置只读", "pass12345")
    admin.post(f"/api/projects/{pid}/members", {"userId": viewer["id"], "role": "VIEWER"})
    c = Client(base_url)
    c.login(email, "pass12345")

    assert c.patch(f"/api/projects/{pid}", {"isPublic": True}).status == 403, "VIEWER 改公开 → 403"
    assert c.patch(f"/api/projects/{pid}", {"joinRole": "EDITOR"}).status == 403, \
        "VIEWER 改加入角色 → 403"
    for bad in ("ADMIN", "OWNER", "EDITORX"):
        assert admin.patch(f"/api/projects/{pid}", {"joinRole": bad}).status == 400, \
            f"joinRole={bad} → 400(自助加入不得授予管理权)"
    assert admin.patch(f"/api/projects/{pid}", {"joinRole": "EDITOR"}).status == 200, \
        "ADMIN 改加入角色 → 200"


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


def test_discover_plaza_ordering_and_fields(base_url, admin):
    """场景 6:广场 = 公开项目目录(唯一入口),按最近活跃倒序,卡片带加入所需字段。"""
    out = _outsider(base_url, admin)
    pid = _public_project(admin, "广场旧公开项目")
    pid_priv = admin.post("/api/projects", {"name": "广场私有项目", "description": "d"}).data["id"]

    disc = out.get("/api/discover/projects")
    assert disc.status == 200, f"广场可读: {disc.status}"
    ids = [x["id"] for x in (disc.data or [])]
    assert pid in ids, f"含刚公开的项目: {ids[:5]}"
    assert pid_priv not in ids, f"不含私有项目: {ids[:5]}"
    assert all(x["isPublic"] for x in (disc.data or [])), "每项都标 isPublic"
    assert all("joinRole" in x and x["isMember"] is False for x in (disc.data or [])), \
        "每项都带 joinRole,且对外部人 isMember=false"

    # 活跃排序:再公开一个并制造更新的活跃,它应排更前
    pid3 = _public_project(admin, "更新的公开项目")
    admin.post(f"/api/projects/{pid3}/docs", {"title": "刚建的文档"})
    ids2 = [x["id"] for x in out.get("/api/discover/projects").data]
    assert ids2 and ids2[0] == pid3, f"最近活跃的排在前面: {ids2[:4]}"


def test_search_and_recent_follow_membership(base_url, admin):
    """场景 7:未加入搜不到公开项目(加入前不可读);加入后进入搜索范围。
    最近动态始终只含已参加项目 —— 它和搜索的差别只在管理员那一档。"""
    pid_pub = _public_project(admin, "公开可搜项目")
    doc = admin.post(f"/api/projects/{pid_pub}/docs", {"title": "加入后才可见的文档"}).data
    admin.put(f"/api/docs/{doc['id']}/content",
              {"content": "# 内容\n\n机密内容在这里。", "baseVersion": doc["version"]})
    pid_priv = admin.post("/api/projects", {"name": "私有不可搜项目", "description": "d"}).data["id"]
    admin.upload(pid_priv, "绝密文件.txt", b"top secret")

    out = _outsider(base_url, admin)
    s = out.get("/api/search?q=" + urllib.parse.quote("机密内容")).data
    assert not s.get("docs"), f"未加入 → 搜不到公开项目内容: {str(s.get('docs'))[:120]}"
    s2 = out.get("/api/search?q=" + urllib.parse.quote("绝密文件")).data
    assert all(f["name"] != "绝密文件.txt" for f in s2.get("files", [])), \
        f"私有项目内容搜不到: {str(s2.get('files'))[:120]}"
    r = out.get("/api/search")
    assert r.status == 200, f"最近动态(q 为空的搜索)200: {r.status}"
    assert not r.data.get("files") and not r.data.get("docs"), \
        f"未参加任何项目 → 最近动态为空: {str(r.data)[:160]}"

    out.post(f"/api/projects/{pid_pub}/join")
    s3 = out.get("/api/search?q=" + urllib.parse.quote("机密内容")).data
    assert s3.get("docs"), f"加入后能搜到该项目文档: {str(s3)[:160]}"
    rec = out.get("/api/search").data
    assert "加入后才可见的文档" in [d["title"] for d in rec.get("docs", [])], \
        f"加入后动态含该项目: {str(rec)[:160]}"


def test_close_public_blocks_join_but_keeps_members(base_url, admin):
    """场景 8:关闭公开只拦"新加入",已在册成员不受影响。"""
    pid = _public_project(admin, "关闭公开测试")
    did = admin.post(f"/api/projects/{pid}/docs", {"title": "待关闭文档"}).data["id"]
    joined = _outsider(base_url, admin, "vis-in")
    later = _outsider(base_url, admin, "vis-late")
    assert joined.post(f"/api/projects/{pid}/join").status == 200, "公开期间可加入"

    assert admin.patch(f"/api/projects/{pid}", {"isPublic": False}).status == 200, "关闭公开 → 200"
    assert later.post(f"/api/projects/{pid}/join").status == 403, "关闭后不能再加入"
    assert later.get(f"/api/projects/{pid}").status == 403, "关闭后未加入者读 → 403"
    assert later.get(f"/api/docs/{did}").status == 403, "关闭后未加入者读文档 → 403"
    assert joined.get(f"/api/projects/{pid}").status == 200, "已在册成员不受影响"
    ids = [x["id"] for x in (later.get("/api/discover/projects").data or [])]
    assert pid not in ids, "关闭后从广场消失"


def test_file_share_link(base_url, admin, data_dir):
    """场景 9:分享链接是与项目可见性无关的独立通道(发给不在项目里的同事)。

    与"公开开关"的区别在于**能收回**:链接里是一个随机 token(不是文件 id),
    吊销 = 清空 token,已发出的那条链接立刻失效。
    """
    import sqlite3
    out = _outsider(base_url, admin)
    anon = Client(base_url)          # 完全不登录:分享链接不该要求会话
    pid = admin.post("/api/projects", {"name": "分享链接测试", "description": "d"}).data["id"]
    fp1 = admin.upload(pid, "共享给同事.txt", b"shared").data
    fp2 = admin.upload(pid, "不共享.txt", b"secret").data
    assert out.get(f"/api/files/{fp1['id']}/download").status == 403, \
        "项目私有时非成员下载 → 403"

    r = admin.post(f"/api/files/{fp1['id']}/share", {})
    assert r.status == 200 and r.data.get("url"), f"建立分享链接: {r.status} {r.data}"
    share_url = r.data["url"]
    assert r.data.get("expiresAt") is None, "缺省不过期"
    assert fp1["id"] not in share_url, "链接里不该出现文件 id(它只需要自己那个 token)"

    got = anon.get(share_url)
    assert got.status == 200 and got.data == b"shared", f"匿名下载: {got.status} {got.data}"
    assert anon.get(f"/api/files/{fp1['id']}/download").status in (401, 403), \
        "文件 id 本身不因分享而开放"
    assert anon.get(f"/api/share/{'x' * 43}").status == 404, "编造的 token → 404"
    assert anon.get(f"/api/files?project_id={pid}").status in (401, 403), \
        "项目本身仍私有(分享只放开这一个文件)"
    assert anon.get(f"/api/share/{fp2['id']}").status == 404, "没分享的文件拿不到"

    # 列表标记:只带 shared 布尔,token 不随列表下发
    rows = {f["id"]: f for f in admin.get(f"/api/files?project_id={pid}").data["files"]}
    assert rows[fp1["id"]]["shared"] is True and rows[fp2["id"]]["shared"] is False
    assert "shareUrl" not in rows[fp1["id"]], "列表不下发 token"
    meta = admin.get(f"/api/files/{fp1['id']}/meta").data
    assert meta["shareUrl"] == share_url, "详情里给链接(分享面板要用)"

    # 重新分享 = 换新 token,旧链接立刻失效(而不是在旧链接上延期)
    again = admin.post(f"/api/files/{fp1['id']}/share", {})
    assert again.data["token"] != r.data["token"]
    assert anon.get(share_url).status == 404, "旧链接已被换掉"
    assert anon.get(again.data["url"]).status == 200

    # 过期:把到期时间直接改到过去(唯一的办法 —— 接口不接受负数天,见下面的 400)
    db_path = data_dir / "teamdoc.db"
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.execute("UPDATE files SET share_expires_at='2000-01-01 00:00:00' WHERE id=?",
                     (fp1["id"],))
        conn.commit()
    finally:
        conn.close()
    assert anon.get(again.data["url"]).status == 404, "过期链接 → 404"
    assert admin.post(f"/api/files/{fp1['id']}/share",
                      {"expireDays": -1}).status == 400, "负的天数 → 400"

    # 吊销:立刻断链,文件本身不受影响
    fresh = admin.post(f"/api/files/{fp1['id']}/share", {"expireDays": 7}).data
    assert fresh["expiresAt"], "带有效期的分享返回到期时间"
    assert anon.get(fresh["url"]).status == 200
    assert admin.delete(f"/api/files/{fp1['id']}/share").status == 200
    assert anon.get(fresh["url"]).status == 404, "吊销后立刻失效"
    assert admin.get(f"/api/files/{fp1['id']}/meta").data["shareUrl"] is None
    assert out.get(f"/api/files/{fp1['id']}/download").status == 403, '回到「只有成员能下」'

    # 权限:建立/吊销分享需要 EDITOR(VIEWER 只读)
    viewer_email, viewer = make_user(admin, "只读分享", "viewer12345")
    viewer_c = Client(base_url)
    viewer_c.login(viewer_email, "viewer12345")
    admin.post(f"/api/projects/{pid}/members", {"userId": viewer["id"], "role": "VIEWER"})
    assert viewer_c.post(f"/api/files/{fp1['id']}/share", {}).status == 403, "VIEWER 分享 → 403"
    assert viewer_c.delete(f"/api/files/{fp1['id']}/share").status == 403, "VIEWER 吊销 → 403"
