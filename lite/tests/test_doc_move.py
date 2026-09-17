"""文档移动:项目内改位置 / 跨项目转移 / 移动前检查。

文档此前**完全不能移动**(没有端点,树上的「更多」也只有重命名与删除)—— 这条测试
盯的是"补上之后语义与文件夹、文件一致":同一套权限(项目内 EDITOR、跨项目源 ADMIN +
目标 EDITOR)、同一套父级校验(存在/未删/同项目/防环)、整棵子树一起走。

与文件/文件夹唯一的差异写在用例里:回收站里的子文档**不随迁**(文件按 folder_id
归属,而文档按 project_id,二者不是一回事),恢复时回落到项目根。
"""
import pytest

from _harness import ABSENT_ID, Client, make_user


def _mkdoc(c, pid, title, parent=None):
    body = {"title": title}
    if parent:
        body["parentId"] = parent
    r = c.post(f"/api/projects/{pid}/docs", body)
    assert r.status == 200, f"建文档失败 {r.status}: {r.data}"
    return r.data


def _tree(c, pid):
    r = c.get(f"/api/projects/{pid}/docs/tree")
    assert r.status == 200, f"读文档树失败: {r.status} {r.data}"
    return r.data or []


def _flatten(nodes, out=None):
    out = [] if out is None else out
    for n in nodes:
        out.append(n)
        _flatten(n.get("children") or [], out)
    return out


def _titles(c, pid):
    return [n["title"] for n in _flatten(_tree(c, pid))]


def test_doc_move_semantics_and_permissions(base_url, admin):
    src = admin.post("/api/projects", {"name": "文档移动源", "description": "d"}).data["id"]
    dst = admin.post("/api/projects", {"name": "文档移动目标", "description": "d"}).data["id"]

    # === 项目内改父级(EDITOR 即可) ===
    parent = _mkdoc(admin, src, "父文档")
    kid = _mkdoc(admin, src, "子文档")
    other = _mkdoc(admin, src, "同级文档")
    r = admin.post(f"/api/docs/{kid['id']}/move",
                   {"projectId": src, "parentId": parent["id"]})
    assert r.status == 200, f"项目内移动 200: {r.status} {r.data}"
    assert r.data["parentId"] == parent["id"] and r.data["projectId"] == src
    tree = _tree(admin, src)
    p_node = next(n for n in tree if n["title"] == "父文档")
    assert [c["title"] for c in p_node["children"]] == ["子文档"], p_node

    # 位置:移到新层后落在该层末尾(显式维护排序,不靠原来的 sort 恰好合适)
    r = admin.post(f"/api/docs/{other['id']}/move",
                   {"projectId": src, "parentId": parent["id"]})
    assert r.status == 200
    kids = [c["title"] for c in
            next(n for n in _tree(admin, src) if n["title"] == "父文档")["children"]]
    assert kids == ["子文档", "同级文档"], f"新移动进来的排在末尾: {kids}"

    # 防环:移到自己的后代下 → 409
    r = admin.post(f"/api/docs/{parent['id']}/move",
                   {"projectId": src, "parentId": other["id"]})
    assert r.status == 409, f"移入自己的子树 → 409: {r.status} {r.data}"
    # 不存在的父级 / 别的项目的父级 → 404(父级不可见就不该被指出来)
    assert admin.post(f"/api/docs/{kid['id']}/move",
                      {"projectId": src, "parentId": ABSENT_ID}).status == 404
    assert admin.post(f"/api/docs/{kid['id']}/move",
                      {"projectId": dst, "parentId": parent["id"]}).status == 404, \
        "父级在另一个项目 → 404"

    # === 跨项目:整棵子树一起走 ===
    sub = _mkdoc(admin, src, "孙文档", parent=kid["id"])
    r = admin.post(f"/api/docs/{parent['id']}/move", {"projectId": dst})
    assert r.status == 200, f"跨项目移动 200: {r.status} {r.data}"
    assert r.data["movedDocs"] == 4, f"整棵子树一起走(父+子+孙+先前移入的同级): {r.data}"
    assert _titles(admin, src) == [], f"源项目已空: {_titles(admin, src)}"
    assert sorted(_titles(admin, dst)) == sorted(["父文档", "子文档", "孙文档", "同级文档"]), \
        _titles(admin, dst)
    assert admin.get(f"/api/docs/{sub['id']}").data["projectId"] == dst, "孙文档也跟着换了归属"

    # === 回收站里的子文档不随迁 ===
    trash_doc = _mkdoc(admin, dst, "将被删的子文档", parent=parent["id"])
    assert admin.delete(f"/api/docs/{trash_doc['id']}").status == 200
    back = admin.post("/api/projects", {"name": "第三项目", "description": "d"}).data["id"]
    r = admin.post(f"/api/docs/{parent['id']}/move", {"projectId": back})
    assert r.status == 200, f"{r.status} {r.data}"
    # 它留在 dst 的回收站里,没有跟着去 back(回收站内容不随迁)
    assert [d["title"] for d in admin.get(f"/api/projects/{dst}/trash").data["docs"]] \
        == ["将被删的子文档"]
    assert admin.get(f"/api/projects/{back}/trash").data["docs"] == []
    # 恢复时父级已不在同一项目 → 回落到项目根(而不是挂成跨项目的父子链)
    assert admin.post(f"/api/docs/{trash_doc['id']}/restore").status == 200
    got = admin.get(f"/api/docs/{trash_doc['id']}").data
    assert got["projectId"] == dst and got["parentId"] is None, f"恢复后回落到项目根: {got}"
    assert "将被删的子文档" in _titles(admin, dst)

    # === 权限矩阵 ===
    viewer_email, viewer = make_user(admin, "只读", "viewer12345")
    editor_email, editor = make_user(admin, "编辑", "editor12345")
    outsider_email, _ = make_user(admin, "外部", "outer12345")
    viewer_c, editor_c, outsider_c = Client(base_url), Client(base_url), Client(base_url)
    viewer_c.login(viewer_email, "viewer12345")
    editor_c.login(editor_email, "editor12345")
    outsider_c.login(outsider_email, "outer12345")
    admin.post(f"/api/projects/{back}/members", {"userId": viewer["id"], "role": "VIEWER"})
    admin.post(f"/api/projects/{back}/members", {"userId": editor["id"], "role": "EDITOR"})
    admin.post(f"/api/projects/{dst}/members", {"userId": editor["id"], "role": "EDITOR"})
    node = _mkdoc(admin, back, "权限测试文档")
    assert viewer_c.post(f"/api/docs/{node['id']}/move",
                         {"projectId": back}).status == 403, "VIEWER 移动 → 403"
    assert editor_c.post(f"/api/docs/{node['id']}/move",
                         {"projectId": back}).status == 200, "EDITOR 项目内移动 → 200"
    assert editor_c.post(f"/api/docs/{node['id']}/move",
                         {"projectId": dst}).status == 403, "EDITOR 跨项目 → 403(需源项目 ADMIN)"
    assert outsider_c.post(f"/api/docs/{node['id']}/move",
                           {"projectId": dst}).status == 403, "非成员移动 → 403"
    # 目标项目无角色:源项目 ADMIN 也进不去(不能往别人项目里塞东西)
    admin.post(f"/api/projects/{dst}/members", {"userId": viewer["id"], "role": "VIEWER"})
    r = viewer_c.post(f"/api/docs/{node['id']}/move", {"projectId": dst})
    assert r.status == 403, f"没有目标项目 EDITOR → 403: {r.status} {r.data}"

    # === 移动前检查(move-check):只读、给出"什么不会跟着走" ===
    f = admin.upload(back, "留在源项目的图.png", b"png")
    assert f.status == 200, f"上传失败: {f.data}"
    fid = f.data["id"]
    admin.put(f"/api/docs/{node['id']}/content",
              {"content": f"![图](/api/files/{fid}/download?inline=1)", "baseVersion": 0})
    chk = admin.get(f"/api/docs/{node['id']}/move-check?projectId={dst}")
    assert chk.status == 200, f"move-check 200: {chk.status} {chk.data}"
    assert chk.data["docs"] == 1 and chk.data["targetProject"]["id"] == dst
    assert [x["id"] for x in chk.data["foreignFiles"]] == [fid], \
        f"正文引用的文件会留在源项目: {chk.data}"
    assert "留在源项目" in chk.data["note"]
    # 同项目移动没有"留下来的文件"这回事
    chk2 = admin.get(f"/api/docs/{node['id']}/move-check?projectId={back}")
    assert chk2.data["foreignFiles"] == [] and chk2.data["docs"] == 1
    # 只读接口也要权限:VIEWER 想检查一个自己不能移动的目标 → 403
    admin.post(f"/api/projects/{back}/members", {"userId": viewer["id"], "role": "VIEWER"})
    assert viewer_c.get(f"/api/docs/{node['id']}/move-check?projectId={dst}").status == 403

    admin.delete(f"/api/projects/{back}")
    admin.delete(f"/api/projects/{dst}")
    admin.delete(f"/api/projects/{src}")


def test_backlinks_and_references_are_cross_project(base_url, admin):
    """引用按 id 全站成立:文档搬到别的项目后,反链与「被引用」都还认得它。

    这条是"引用不带项目归属"的直接后果:搬家不再需要改写任何正文,
    而关系查询必须**跨项目**问,否则搬完就成了看不见的悬空引用。
    """
    a = admin.post("/api/projects", {"name": "引用源", "description": "d"}).data["id"]
    b = admin.post("/api/projects", {"name": "引用目标", "description": "d"}).data["id"]
    target = _mkdoc(admin, a, "被引用的文档")
    referer = _mkdoc(admin, a, "引用它的文档")
    f = admin.upload(a, "共享附件.txt", b"hello").data
    admin.put(f"/api/docs/{referer['id']}/content",
              {"content": f"见 [@{target['title']}](teamdoc://doc/{target['id']}) "
                          f"与 [@附件](teamdoc://file/{f['id']})",
               "baseVersion": 0})

    # 同项目:反链可见
    assert [d["id"] for d in admin.get(f"/api/docs/{target['id']}/backlinks").data] == [referer["id"]]
    refs = {x["id"]: x for x in admin.get(f"/api/files?project_id={a}&limit=50").data["files"]}
    assert refs[f["id"]]["referenced"] is True, "chip 形态的引用要被认出来"

    # 目标文档搬到 B:引用它的文档还在 A —— 反链必须跨项目出现,并带来源项目名
    assert admin.post(f"/api/docs/{target['id']}/move", {"projectId": b}).status == 200
    rows = admin.get(f"/api/docs/{target['id']}/backlinks").data
    assert [d["id"] for d in rows] == [referer["id"]], "跨项目反链仍在"
    assert rows[0]["projectId"] == a and rows[0]["projectName"] == "引用源"

    # 文件搬到 B 后,正文里的引用仍然认得它(「被引用」按 id 跨项目比对)
    assert admin.post(f"/api/files/{f['id']}/move", {"projectId": b}).status == 200
    refs_b = {x["id"]: x for x in admin.get(f"/api/files?project_id={b}&limit=50").data["files"]}
    assert refs_b[f["id"]]["referenced"] is True, "文件换项目后仍被 A 的文档引用着"

    admin.delete(f"/api/projects/{a}")
    admin.delete(f"/api/projects/{b}")


def test_doc_move_rejects_bad_payload(base_url, admin):
    """移动端点的输入契约:非法形状 400、缺 projectId 400、不存在 404。"""
    pid = admin.post("/api/projects", {"name": "输入校验", "description": "d"}).data["id"]
    doc = _mkdoc(admin, pid, "文档")
    assert admin.post(f"/api/docs/{doc['id']}/move", {}).status == 400, "缺 projectId → 400"
    assert admin.post(f"/api/docs/{doc['id']}/move",
                      {"projectId": "999999"}).status == 400, "旧式数字 id → 400(形状非法)"
    assert admin.post(f"/api/docs/{doc['id']}/move",
                      {"projectId": ABSENT_ID}).status == 404, "形状合法但不存在 → 404"
    assert admin.post(f"/api/docs/{ABSENT_ID}/move", {"projectId": pid}).status == 404, "文档不存在 → 404"
    assert admin.get(f"/api/docs/{ABSENT_ID}/move-check?projectId={pid}").status == 404
    assert admin.get(f"/api/docs/{doc['id']}/move-check?projectId=999999").status == 400, \
        "查询串里的非法 id 也是 400"
    admin.delete(f"/api/projects/{pid}")
