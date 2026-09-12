"""云空间:分页 / 服务端排序 / 重名 / 占用统计 / 最近文件。

覆盖:
  1. 分页:offset/limit 生效,total 为真实总数,hasMore 正确
  2. 服务端排序:name / time / size 升序降序,翻页不重不漏(按 id 定序)
  3. 重名:上传自动加后缀(foo(2).png);新建/重命名遇重名 409
  4. 改名会改掉扩展名时 mime 跟着重算
  5. 项目占用统计:活跃与回收站分列,删文件后活跃减少、回收站增加
  6. 最近文件:跨项目返回,带项目名;不可见项目不出现

各测试自建项目,互不依赖。
"""
from _harness import Client, make_user


def _new_project(admin, name):
    return admin.post("/api/projects", {"name": name, "description": "d"}).data["id"]


def test_paging_and_server_sorting(admin):
    pid = _new_project(admin, "云空间分页测试")
    # 建 12 个文件,名字与大小都可控以便验证排序
    expected = []
    for i in range(12):
        n = f"f{i:02d}.txt"
        r = admin.upload(pid, n, b"x" * (100 * (i + 1)))
        expected.append(n)
        assert r.status == 200, f"上传 {n}: {r.data}"

    # === 分页 ===
    r = admin.get(f"/api/files?project_id={pid}&limit=5&offset=0&sort=name&dir=asc")
    page1 = r.data
    assert r.status == 200, f"首页 200: {str(page1)[:120]}"
    assert len(page1["files"]) == 5, f"首页返回 5 条: {len(page1['files'])}"
    assert page1["total"]["files"] == 12, f"total.files = 12: {page1['total']}"
    assert page1["hasMore"] is True, f"hasMore = true: {page1.get('hasMore')}"
    names1 = [f["name"] for f in page1["files"]]
    assert names1 == expected[:5], f"首页按名称升序且是前 5 个: {names1}"
    names2 = [f["name"] for f in admin.get(
        f"/api/files?project_id={pid}&limit=5&offset=5&sort=name&dir=asc").data["files"]]
    assert names2 == expected[5:10], f"第二页是接下来 5 个: {names2}"
    page3 = admin.get(
        f"/api/files?project_id={pid}&limit=5&offset=10&sort=name&dir=asc").data
    assert len(page3["files"]) == 2, f"末页只剩 2 条: {len(page3['files'])}"
    assert page3["hasMore"] is False, f"末页 hasMore = false: {page3.get('hasMore')}"
    all_names = names1 + names2 + [f["name"] for f in page3["files"]]
    assert all_names == expected, f"翻页不重不漏: {all_names}"

    # === 服务端排序 ===
    got = [f["name"] for f in admin.get(
        f"/api/files?project_id={pid}&sort=name&dir=desc&limit=500").data["files"]]
    assert got == list(reversed(expected)), f"名称降序: {got[:4]}"
    sizes = [f["size"] for f in admin.get(
        f"/api/files?project_id={pid}&sort=size&dir=desc&limit=500").data["files"]]
    assert sizes == sorted(sizes, reverse=True), f"按大小降序: {sizes[:5]}"
    sizes_a = [f["size"] for f in admin.get(
        f"/api/files?project_id={pid}&sort=size&dir=asc&limit=500").data["files"]]
    assert sizes_a == sorted(sizes_a), f"按大小升序: {sizes_a[:5]}"
    times = [f["createdAt"] for f in admin.get(
        f"/api/files?project_id={pid}&sort=time&dir=desc&limit=500").data["files"]]
    assert times == sorted(times, reverse=True), f"按时间降序: {times[:3]}"
    r = admin.get(f"/api/files?project_id={pid}&sort=bogus&dir=bogus&limit=1")
    assert r.status == 200, f"非法排序参数回落到名称升序(不报错): {str(r.data)[:80]}"


def test_duplicate_names_and_rename_mime(admin):
    pid = _new_project(admin, "云空间重名测试")

    # === 重名处理 ===
    first = admin.upload(pid, "同名.txt", b"first").data
    assert first["name"] == "同名.txt", f"首次上传名字保持不变: {first['name']}"
    second = admin.upload(pid, "同名.txt", b"second").data
    assert second["name"] == "同名(2).txt", f"重名上传自动加后缀: {second['name']}"
    third = admin.upload(pid, "同名.txt", b"third").data
    assert third["name"] == "同名(3).txt", f"第三次上传 (3): {third['name']}"
    # 三个都在
    lst = admin.get(f"/api/files?project_id={pid}&limit=500").data
    same = sorted(f["name"] for f in lst["files"] if f["name"].startswith("同名"))
    assert same == ["同名(2).txt", "同名(3).txt", "同名.txt"], f"三个同名文件都存在: {same}"
    # 显式新建/重命名重名 → 409
    assert admin.post("/api/files/folders",
                      {"projectId": pid, "name": "重名目录"}).status == 200, "新建文件夹 200"
    r = admin.post("/api/files/folders", {"projectId": pid, "name": "重名目录"})
    assert r.status == 409, f"重名文件夹 → 409: {r.status} {r.data}"
    r = admin.post("/api/files/folders", {"projectId": pid, "name": "同名.txt"})
    assert r.status == 409, f"文件夹与文件重名 → 409: {r.status} {r.data}"
    r = admin.patch(f"/api/files/{first['id']}", {"name": second["name"]})
    assert r.status == 409, f"重命名撞已有文件 → 409: {r.status} {r.data}"
    r = admin.patch(f"/api/files/{first['id']}", {"name": "重名目录"})
    assert r.status == 409, f"重命名撞已有文件夹 → 409: {r.status} {r.data}"
    r = admin.patch(f"/api/files/{first['id']}", {"name": "同名.txt"})
    assert r.status == 200, f"改名成自己原名 → 200(不算冲突): {r.status} {r.data}"

    # === 改名后 mime 跟着重算 ===
    svg = admin.upload(pid, "图形.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>').data
    assert svg["canInline"] is False, f"svg 上传后 canInline=false: {svg}"
    renamed = admin.patch(f"/api/files/{svg['id']}", {"name": "图形.png"}).data
    assert renamed["mime"] == "image/png", f"改名为 .png 后 mime 变为 image/png: {renamed}"
    assert renamed["canInline"] is True, f"canInline 随之变 true: {renamed}"
    back = admin.patch(f"/api/files/{svg['id']}", {"name": "图形.html"}).data
    assert back["canInline"] is False, f"改成 .html 后 canInline 回落 false: {back}"


def test_project_storage_stats(admin):
    pid = _new_project(admin, "项目占用统计测试")
    # 先放一个种子文件:占用统计各段断言需要项目非空
    admin.upload(pid, "seed.txt", b"s" * 100)
    r = admin.get(f"/api/projects/{pid}/storage")
    s0 = r.data
    assert r.status == 200, f"占用接口 200: {str(s0)[:120]}"
    assert s0["active"]["fileCount"] > 0, f"活跃文件数 > 0: {s0['active']}"
    base_active = s0["active"]["bytes"]
    base_trash = s0["trash"]["bytes"]
    victim = admin.upload(pid, "待删除.bin", b"v" * 5000).data
    s1 = admin.get(f"/api/projects/{pid}/storage").data
    assert s1["active"]["bytes"] == base_active + 5000, \
        f"上传后活跃字节增加: {base_active} -> {s1['active']['bytes']}"
    admin.delete(f"/api/files/{victim['id']}")
    s2 = admin.get(f"/api/projects/{pid}/storage").data
    assert s2["active"]["bytes"] == base_active, \
        f"删除后活跃字节回落: {s2['active']['bytes']}"
    assert s2["trash"]["bytes"] == base_trash + 5000, \
        f"回收站字节增加(仍占磁盘,故单列): {base_trash} -> {s2['trash']['bytes']}"
    assert s2["trash"]["fileCount"] == s0["trash"]["fileCount"] + 1, \
        f"回收站文件数 +1: {s2['trash']}"
    assert s2["disk"]["free"] > 0, f"含磁盘余量: {s2['disk']}"
    admin.post(f"/api/files/{victim['id']}/restore")
    admin.delete(f"/api/files/{victim['id']}/permanent")
    s3 = admin.get(f"/api/projects/{pid}/storage").data
    assert s3["trash"]["bytes"] == base_trash, \
        f"彻底删除后回收站归零: {s3['trash']['bytes']}"


def test_recent_files_cross_project(base_url, admin):
    pid_a = _new_project(admin, "最近文件A")
    pid_b = _new_project(admin, "最近文件B")
    assert admin.upload(pid_a, "A项目文件.txt", b"aaa").status == 200
    assert admin.upload(pid_b, "B项目文件.txt", b"bbb").status == 200
    doc = admin.post(f"/api/projects/{pid_a}/docs", {"title": "最近文档"}).data
    admin.put(f"/api/docs/{doc['id']}/content", {"content": "# 内容"})

    rec = admin.get("/api/recent?limit=30").data
    fnames = [f["name"] for f in rec["files"]]
    assert "A项目文件.txt" in fnames and "B项目文件.txt" in fnames, \
        f"两个项目的文件都出现在最近(跨项目): {fnames[:6]}"
    assert all(f.get("projectName") for f in rec["files"]), "文件带项目名"
    assert "最近文档" in [d["title"] for d in rec["docs"]], "含最近文档"
    assert all(d.get("projectName") for d in rec["docs"]), "文档带项目名"

    # 非成员什么都不会看到:最新动态只含**已参加**的项目(/api/recent 按成员过滤,
    # 见 search.py),公开项目也进不来(公开只意味着可发现 + 可自助加入,
    # 未加入者在鉴权上拿不到任何角色)。新建账号只"参加"了个人空间(无文档无文件),
    # 所以列表应为空。
    other_email, _ = make_user(admin, "外部", "outer12345")
    other = Client(base_url)
    other.login(other_email, "outer12345")
    rec2 = other.get("/api/recent").data
    assert not rec2["files"] and not rec2["docs"], \
        f"非成员最近动态为空(私有与公开项目均不出现): {str(rec2)[:200]}"
