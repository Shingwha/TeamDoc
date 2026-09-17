"""端到端验证:文件夹操作闭环(递归删除 / 递归恢复 / 移动 / 回收站子树根 / 权限)。

覆盖:
  1. 非空文件夹可直接删除(递归软删除整棵子树)—— 历史语义是"非空拒删"
  2. 递归删除后回收站只列**子树根**,不列出被一起删掉的子项
  3. 恢复文件夹 = 整棵子树一起回来
  4. 恢复时父文件夹仍在回收站 → 本文件夹回落到项目根目录
  5. 彻底删除 = 级联清子文件夹与文件(含物理文件)
  6. 文件夹移动:项目内 EDITOR 即可;拒绝移入自己的后代;跨项目需源 ADMIN;
     跨项目时整棵子树换项目(不留下跨项目悬挂)
  7. 权限语义:VIEWER / 非成员 403 且不泄露资源状态(授权先于状态判定)
  8. 文件夹树 API 返回嵌套结构

场景 1-4、6、7 在同一项目状态链上,必须按序执行,故合在一个测试函数里;
场景 5 自带独立子树。数据目录是一次性的,不再需要自清理。
"""
from _harness import ABSENT_ID, Client, make_user


def test_folder_recycle_semantics(base_url, admin, data_dir):
    files_dir = data_dir / "files"

    # --- 本文件专用小工具(闭包绑定客户端,别的文件不要抄) ---
    def mkdir(pid, name, parent=None):
        body = {"projectId": pid, "name": name}
        if parent:
            body["parentId"] = parent
        r = admin.post("/api/files/folders", body)
        assert r.status == 200, f"建文件夹失败 {r.status}: {r.data}"
        return r.data["id"]

    def trash(pid):
        r = admin.get(f"/api/projects/{pid}/trash")
        return r.data if r.status == 200 else {"docs": [], "files": [], "folders": []}

    def tree(pid, c=admin):
        r = c.get(f"/api/projects/{pid}/folders/tree")
        return r.status, (r.data or [])

    def flatten(nodes, out=None):
        out = [] if out is None else out
        for n in nodes:
            out.append(n["name"])
            flatten(n.get("children", []), out)
        return out

    pid = admin.post("/api/projects", {"name": "文件夹语义测试", "description": "d"}).data["id"]
    pid_b = admin.post("/api/projects",
                       {"name": "文件夹语义测试B", "description": "d"}).data["id"]

    # === 场景1:非空文件夹可直接删除(历史语义是 403 非空拒删) ===
    a = mkdir(pid, "父目录")
    sub = mkdir(pid, "子目录", a)
    sub2 = mkdir(pid, "孙目录", sub)
    phys_before = set(files_dir.iterdir()) if files_dir.exists() else set()
    f_root = admin.upload(pid, "父层文件.txt", b"in parent", folder_id=a).data
    f_deep = admin.upload(pid, "深层文件.txt", b"deep content", folder_id=sub2).data
    phys_new = (set(files_dir.iterdir()) - phys_before) if files_dir.exists() else set()
    r = admin.delete(f"/api/files/folders/{a}")
    assert r.status == 200, f"删非空文件夹应 200(不再 403): {r.status} {r.data}"
    assert r.data.get("removedFolders") == 3, f"返回删除文件夹数=3(父+子+孙): {r.data}"
    assert r.data.get("removedFiles") == 2, f"返回删除文件数=2: {r.data}"
    r = admin.get(f"/api/files?project_id={pid}")
    assert not any(f["name"] == "父目录" for f in r.data["folders"]), \
        f"根目录已看不到该文件夹: {r.data['folders']}"

    # === 场景2:回收站只列子树根(不列被一起删掉的子项) ===
    t = trash(pid)
    folder_names = [f["name"] for f in t["folders"]]
    assert len(t["folders"]) == 1, f"回收站文件夹只 1 条: {folder_names}"
    assert folder_names == ["父目录"], f"该条是父目录(子树根): {folder_names}"
    assert [f["name"] for f in t["files"]] == [], \
        f"回收站文件为空(子文件随根隐藏): {t['files']}"

    # === 场景3:恢复 = 整棵子树一起回来 ===
    r = admin.post(f"/api/files/folders/{a}/restore")
    assert r.status == 200, f"恢复 200: {r.status} {r.data}"
    assert r.data.get("restoredFolders") == 3, f"返回恢复文件夹数=3: {r.data}"
    assert r.data.get("restoredFiles") == 2, f"返回恢复文件数=2: {r.data}"
    st, tree_a = tree(pid)
    assert any(n["name"] == "父目录" for n in tree_a), f"文件夹树含父目录: {tree_a}"
    s1 = next((n for n in tree_a if n["name"] == "父目录"), {})
    assert any(c["name"] == "子目录" for c in s1.get("children", [])), \
        f"嵌套:父目录含子目录: {s1}"
    sub_node = next((c for c in s1.get("children", []) if c["name"] == "子目录"), {})
    assert any(c["name"] == "孙目录" for c in sub_node.get("children", [])), \
        f"嵌套:子目录含孙目录: {s1}"
    r = admin.get(f"/api/files?project_id={pid}&folder_id={sub2}")
    assert any(f["name"] == "深层文件.txt" for f in r.data["files"]), \
        f"孙目录里文件已回来: {r.data['files']}"
    assert not trash(pid)["folders"] and not trash(pid)["files"], "回收站已空"

    # === 场景4:恢复时父文件夹仍在回收站 → 回落到项目根 ===
    assert admin.delete(f"/api/files/folders/{a}").status == 200, "重新删父目录 200"
    assert admin.post(f"/api/files/folders/{sub}/restore").status == 200, "恢复子目录 200"
    r = admin.get(f"/api/files?project_id={pid}")
    assert any(f["name"] == "子目录" for f in r.data["folders"]), \
        f"子目录回落到项目根(父仍在回收站): {r.data['folders']}"
    assert any(f["name"] == "父目录" for f in trash(pid)["folders"]), \
        f"父目录仍在回收站: {trash(pid)['folders']}"
    r = admin.get(f"/api/files?project_id={pid}&folder_id={sub2}")
    assert r.status == 200, f"孙目录随子目录一起恢复: {r.status}"
    if files_dir.exists():
        # 断链后这两个文件仍应活着(恢复是递归的,它们只是换了父目录)
        now = set(files_dir.iterdir())
        assert phys_new <= now, f"两个物理文件随恢复存活: {phys_new - now}"

    # === 场景5:彻底删除级联清子文件夹与文件 ===
    # 用一棵独立的新树验证级联:场景4 把子目录恢复到根后父链已断,
    # 此时"父目录"的子树里只剩它自己 —— 拿它做级联断言会误判(不该误删已断链的内容)
    c_root = mkdir(pid, "级联根")
    c_sub = mkdir(pid, "级联子", c_root)
    c_deep = mkdir(pid, "级联孙", c_sub)
    phys_before = set(files_dir.iterdir()) if files_dir.exists() else set()
    admin.upload(pid, "级联文件1.txt", b"c1", folder_id=c_root)
    admin.upload(pid, "级联文件2.txt", b"c2", folder_id=c_deep)
    phys_new = (set(files_dir.iterdir()) - phys_before) if files_dir.exists() else set()
    assert admin.delete(f"/api/files/folders/{c_root}").status == 200, "删级联根 200"
    r = admin.delete(f"/api/files/folders/{c_root}/permanent")
    assert r.status == 200, f"彻底删级联根 200: {r.status} {r.data}"
    assert r.data.get("removedFolders") == 3, f"级联文件夹数=3: {r.data}"
    assert r.data.get("removedFiles") == 2, f"级联文件数=2: {r.data}"
    st, tree_a = tree(pid)
    names = flatten(tree_a)
    assert not any(n.startswith("级联") for n in names), \
        f"三个级联目录已从树中消失: {names}"
    if files_dir.exists():
        now = set(files_dir.iterdir())
        assert not (phys_new & now), f"两个级联物理文件已清除: {phys_new & now}"
    # 顺带确认:已断链的"子目录"没有被打扰(场景4 恢复到根的那支)
    assert "子目录" in names, f"已回落到根的子目录未被级联误删: {names}"

    # === 场景6:文件夹移动 ===
    st, tree_a = tree(pid)
    sub_id = next((n["id"] for n in tree_a if n["name"] == "子目录"), None)
    assert sub_id is not None, f"子目录在根(场景4 回落出来的): {names}"
    dest = mkdir(pid, "目标目录")
    r = admin.post(f"/api/files/folders/{sub_id}/move", {"projectId": pid, "parentId": dest})
    assert r.status == 200, f"项目内移动 200: {r.status} {r.data}"
    st, tree_a = tree(pid)
    dest_node = next((n for n in tree_a if n["name"] == "目标目录"), {})
    assert any(c["name"] == "子目录" for c in dest_node.get("children", [])), \
        f"子目录已挂到目标目录下: {dest_node}"
    r = admin.post(f"/api/files/folders/{dest}/move", {"projectId": pid, "parentId": sub_id})
    assert r.status == 409, f"移入自己的后代 → 409: {r.status} {r.data}"
    r = admin.post(f"/api/files/folders/{dest}/move", {"projectId": pid_b})
    assert r.status == 200, f"跨项目移动 200: {r.status} {r.data}"
    r = admin.get(f"/api/files?project_id={pid_b}")
    assert any(f["name"] == "目标目录" for f in r.data["folders"]), \
        f"目标项目根可见该目录: {r.data['folders']}"
    r = admin.get(f"/api/files?project_id={pid}")
    assert not any(f["name"] == "目标目录" for f in r.data["folders"]), \
        f"源项目已看不到该目录: {r.data['folders']}"
    st, tb = tree(pid_b)
    d = next((n for n in tb if n["name"] == "目标目录"), {})
    assert any(c["name"] == "子目录" for c in d.get("children", [])), \
        f"子树随根一起换项目(子目录也在新项目): {tb}"

    # === 场景7:权限语义(授权先于状态判定) ===
    viewer_email, viewer = make_user(admin, "只读", "viewer12345")
    outsider_email, _ = make_user(admin, "外部", "outer12345")
    viewer_c = Client(base_url)
    viewer_c.login(viewer_email, "viewer12345")
    outsider_c = Client(base_url)
    outsider_c.login(outsider_email, "outer12345")
    admin.post(f"/api/projects/{pid}/members", {"userId": viewer["id"], "role": "VIEWER"})
    assert viewer_c.get(f"/api/projects/{pid}/trash").status == 200, "VIEWER 可读回收站"
    assert viewer_c.delete(f"/api/files/folders/{dest}").status == 403, "VIEWER 删文件夹 → 403"
    assert viewer_c.post(f"/api/files/folders/{dest}/restore").status == 403, \
        "VIEWER 恢复 → 403(非 409)"
    assert viewer_c.post(f"/api/files/folders/{dest}/move",
                         {"projectId": pid}).status == 403, "VIEWER 移动 → 403"
    assert outsider_c.delete(f"/api/files/folders/{dest}").status == 403, \
        "非成员删 → 403(不泄露存在性)"
    assert outsider_c.post(f"/api/files/folders/{dest}/move",
                           {"projectId": pid}).status == 403, "非成员移动 → 403"
    assert outsider_c.get(f"/api/projects/{pid}/folders/tree").status == 403, \
        "非成员读文件夹树 → 403"
    r = viewer_c.delete(f"/api/files/folders/{ABSENT_ID}")
    assert r.status == 404, f"不存在的文件夹 → 404(存在性与否先判): {r.status}"

    editor_email, editor = make_user(admin, "编辑", "editor12345")
    editor_c = Client(base_url)
    editor_c.login(editor_email, "editor12345")
    admin.post(f"/api/projects/{pid}/members", {"userId": editor["id"], "role": "EDITOR"})
    admin.post(f"/api/projects/{pid_b}/members", {"userId": editor["id"], "role": "EDITOR"})
    mk = mkdir(pid, "编辑测试目录")
    assert editor_c.post(f"/api/files/folders/{mk}/move",
                         {"projectId": pid}).status == 200, "EDITOR 项目内移动 → 200"
    r = editor_c.post(f"/api/files/folders/{mk}/move", {"projectId": pid_b})
    assert r.status == 403, f"EDITOR 跨项目移动 → 403(需源项目 ADMIN): {r.status}"
