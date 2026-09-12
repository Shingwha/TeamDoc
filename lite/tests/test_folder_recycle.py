"""端到端验证:文件夹操作闭环(递归删除 / 递归恢复 / 移动 / 回收站子树根 / 权限)。

用法:先启动服务(隔离数据目录 + 非常用端口),再运行本脚本。
要看物理文件是否清除,额外传 TD_DATA_DIR。

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
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
FAIL = []
SESSIONS = {}   # email -> sid


def raw_call(method, path, body=None, sid=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if sid:
        req.add_header("Cookie", "td_sid=" + sid)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            new_sid = None
            sc = r.headers.get("Set-Cookie")
            if sc and "td_sid=" in sc:
                new_sid = sc.split("td_sid=")[1].split(";")[0]
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else None), new_sid
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, (json.loads(raw) if raw else None), None


def login(email, password):
    st, r, sid = raw_call("POST", "/api/auth/login", {"email": email, "password": password})
    if sid:
        SESSIONS[email] = sid
    return st


def call(method, path, body=None, who="admin@teamdoc.local"):
    st, r, sid = raw_call(method, path, body, SESSIONS.get(who))
    if sid:
        SESSIONS[who] = sid
    return st, r


def upload(pid, folder_id, filename, content, who="admin@teamdoc.local"):
    """raw body 上传:请求体即文件,元数据走 query string(见 files.py upload_file)"""
    from urllib.parse import quote
    qs = "?projectId=" + quote(str(pid)) + "&name=" + quote(filename)
    if folder_id:
        qs += "&folderId=" + quote(str(folder_id))
    req = urllib.request.Request(BASE + "/api/files/upload" + qs, data=content, method="POST")
    if SESSIONS.get(who):
        req.add_header("Cookie", "td_sid=" + SESSIONS[who])
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "null")


def ok(label, cond, extra=""):
    if cond:
        print(f"  OK    {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f"   <- {extra}" if extra else ""))


def mkdir(pid, name, parent=None):
    body = {"projectId": pid, "name": name}
    if parent:
        body["parentId"] = parent
    st, r = call("POST", "/api/files/folders", body)
    assert st == 200, f"建文件夹失败 {st}: {r}"
    return r["id"]


def trash(pid):
    st, r = call("GET", f"/api/projects/{pid}/trash")
    return r if st == 200 else {"docs": [], "files": [], "folders": []}


def tree(pid, who="admin@teamdoc.local"):
    st, r = call("GET", f"/api/projects/{pid}/folders/tree", who=who)
    return st, (r or [])


def flatten(nodes, out=None):
    out = [] if out is None else out
    for n in nodes:
        out.append(n["name"])
        flatten(n.get("children", []), out)
    return out


def main():
    if login("admin@teamdoc.local", "admin12345") != 200:
        print("管理员登录失败,请先跑 tests/_bootstrap.py")
        return 1

    st, proj = call("POST", "/api/projects", {"name": "文件夹语义测试", "description": "d"})
    pid = proj["id"]
    st, proj_b = call("POST", "/api/projects", {"name": "文件夹语义测试B", "description": "d"})
    pid_b = proj_b["id"]
    data_dir = os.environ.get("TD_DATA_DIR", "")
    files_dir = os.path.join(data_dir, "files") if data_dir else ""

    print("\n=== 场景1:非空文件夹可直接删除(历史语义是 403 非空拒删) ===")
    a = mkdir(pid, "父目录")
    sub = mkdir(pid, "子目录", a)
    sub2 = mkdir(pid, "孙目录", sub)
    _phys_before = set(os.listdir(files_dir)) if files_dir else set()
    f_root = upload(pid, a, "父层文件.txt", b"in parent")[1]
    f_deep = upload(pid, sub2, "深层文件.txt", b"deep content")[1]
    _phys_new = (set(os.listdir(files_dir)) - _phys_before) if files_dir else set()
    st, r = call("DELETE", f"/api/files/folders/{a}")
    ok("删非空文件夹 200(不再 403)", st == 200, f"{st} {r}")
    ok("返回删除文件夹数=3(父+子+孙)", r.get("removedFolders") == 3, str(r))
    ok("返回删除文件数=2", r.get("removedFiles") == 2, str(r))
    st, lst = call("GET", f"/api/files?project_id={pid}")
    ok("根目录已看不到该文件夹", not any(f["name"] == "父目录" for f in lst["folders"]), str(lst["folders"]))

    print("\n=== 场景2:回收站只列子树根(不列被一起删掉的子项) ===")
    t = trash(pid)
    folder_names = [f["name"] for f in t["folders"]]
    ok("回收站文件夹只 1 条", len(t["folders"]) == 1, str(folder_names))
    ok("该条是父目录(子树根)", folder_names == ["父目录"], str(folder_names))
    file_names = [f["name"] for f in t["files"]]
    ok("回收站文件为空(子文件随根隐藏)", file_names == [], str(file_names))

    print("\n=== 场景3:恢复 = 整棵子树一起回来 ===")
    st, r = call("POST", f"/api/files/folders/{a}/restore")
    ok("恢复 200", st == 200, f"{st} {r}")
    ok("返回恢复文件夹数=3", r.get("restoredFolders") == 3, str(r))
    ok("返回恢复文件数=2", r.get("restoredFiles") == 2, str(r))
    st, tree_a = tree(pid)
    ok("文件夹树含父目录", any(n["name"] == "父目录" for n in tree_a), str(tree_a))
    s1 = next((n for n in tree_a if n["name"] == "父目录"), {})
    ok("嵌套:父目录含子目录", any(c["name"] == "子目录" for c in s1.get("children", [])), str(s1))
    ok("嵌套:子目录含孙目录",
       any(c["name"] == "孙目录" for c in next(
           (c for c in s1.get("children", []) if c["name"] == "子目录"), {}).get("children", [])),
       str(s1))
    st, lst = call("GET", f"/api/files?project_id={pid}&folder_id={sub2}")
    ok("孙目录里文件已回来", any(f["name"] == "深层文件.txt" for f in lst["files"]), str(lst["files"]))
    ok("回收站已空", not trash(pid)["folders"] and not trash(pid)["files"])

    print("\n=== 场景4:恢复时父文件夹仍在回收站 → 回落到项目根 ===")
    st, _ = call("DELETE", f"/api/files/folders/{a}")
    ok("重新删父目录 200", st == 200, str(st))
    st, _ = call("POST", f"/api/files/folders/{sub}/restore")
    ok("恢复子目录 200", st == 200, str(st))
    st, lst = call("GET", f"/api/files?project_id={pid}")
    ok("子目录回落到项目根(父仍在回收站)", any(f["name"] == "子目录" for f in lst["folders"]),
       str(lst["folders"]))
    ok("父目录仍在回收站", any(f["name"] == "父目录" for f in trash(pid)["folders"]),
       str(trash(pid)["folders"]))
    st, lst = call("GET", f"/api/files?project_id={pid}&folder_id={sub2}")
    ok("孙目录随子目录一起恢复", sub2 is not None and st == 200, str(st))
    if files_dir:
        # 断链后这两个文件仍应活着(恢复是递归的,它们只是换了父目录)
        _now = set(os.listdir(files_dir))
        ok("两个物理文件随恢复存活", _phys_new <= _now, str(_phys_new - _now))

    print("\n=== 场景5:彻底删除级联清子文件夹与文件 ===")
    # 用一棵独立的新树验证级联:场景4 把子目录恢复到根后父链已断,
    # 此时"父目录"的子树里只剩它自己 —— 拿它做级联断言会误判(不该误删已断链的内容)
    c = mkdir(pid, "级联根")
    c_sub = mkdir(pid, "级联子", c)
    c_deep = mkdir(pid, "级联孙", c_sub)
    _phys_before = set(os.listdir(files_dir)) if files_dir else set()
    fc1 = upload(pid, c, "级联文件1.txt", b"c1")[1]
    fc2 = upload(pid, c_deep, "级联文件2.txt", b"c2")[1]
    _phys_new = (set(os.listdir(files_dir)) - _phys_before) if files_dir else set()
    st, _ = call("DELETE", f"/api/files/folders/{c}")
    ok("删级联根 200", st == 200, str(st))
    st, r = call("DELETE", f"/api/files/folders/{c}/permanent")
    ok("彻底删级联根 200", st == 200, f"{st} {r}")
    ok("级联文件夹数=3", r.get("removedFolders") == 3, str(r))
    ok("级联文件数=2", r.get("removedFiles") == 2, str(r))
    st, tree_a = tree(pid)
    names = flatten(tree_a)
    ok("三个级联目录已从树中消失",
       not any(n.startswith("级联") for n in names), str(names))
    if files_dir:
        _now = set(os.listdir(files_dir))
        ok("两个级联物理文件已清除", not (_phys_new & _now), str(_phys_new & _now))
    else:
        print("  SKIP  未设 TD_DATA_DIR,跳过物理文件检查")
    # 顺带确认:已断链的"子目录"没有被打扰(场景4 恢复到根的那支)
    ok("已回落到根的子目录未被级联误删", "子目录" in names, str(names))

    print("\n=== 场景6:文件夹移动 ===")
    st, tree_a = tree(pid)
    sub_id = next((n["id"] for n in tree_a if n["name"] == "子目录"), None)
    ok("子目录在根(场景4 回落出来的)", sub_id is not None, str(names))
    dest = mkdir(pid, "目标目录")
    st, r = call("POST", f"/api/files/folders/{sub_id}/move", {"projectId": pid, "parentId": dest})
    ok("项目内移动 200", st == 200, f"{st} {r}")
    st, tree_a = tree(pid)
    dest_node = next((n for n in tree_a if n["name"] == "目标目录"), {})
    ok("子目录已挂到目标目录下", any(c["name"] == "子目录" for c in dest_node.get("children", [])),
       str(dest_node))
    st, r = call("POST", f"/api/files/folders/{dest}/move", {"projectId": pid, "parentId": sub_id})
    ok("移入自己的后代 → 409", st == 409, f"{st} {r}")
    st, r = call("POST", f"/api/files/folders/{dest}/move", {"projectId": pid_b})
    ok("跨项目移动 200", st == 200, f"{st} {r}")
    st, lst_b = call("GET", f"/api/files?project_id={pid_b}")
    ok("目标项目根可见该目录", any(f["name"] == "目标目录" for f in lst_b["folders"]), str(lst_b["folders"]))
    st, lst_a = call("GET", f"/api/files?project_id={pid}")
    ok("源项目已看不到该目录", not any(f["name"] == "目标目录" for f in lst_a["folders"]),
       str(lst_a["folders"]))
    st, tb = tree(pid_b)
    d = next((n for n in tb if n["name"] == "目标目录"), {})
    ok("子树随根一起换项目(子目录也在新项目)",
       any(c["name"] == "子目录" for c in d.get("children", [])), str(tb))

    print("\n=== 场景7:权限语义(授权先于状态判定) ===")
    viewer_email = f"fr-viewer-{uuid.uuid4().hex[:6]}@t.local"
    outsider_email = f"fr-out-{uuid.uuid4().hex[:6]}@t.local"
    _, viewer = call("POST", "/api/users", {"email": viewer_email, "name": "只读", "password": "viewer12345"})
    call("POST", "/api/users", {"email": outsider_email, "name": "外部", "password": "outer12345"})
    login(viewer_email, "viewer12345")
    login(outsider_email, "outer12345")
    call("POST", f"/api/projects/{pid}/members", {"userId": viewer["id"], "role": "VIEWER"})
    ok("VIEWER 可读回收站", call("GET", f"/api/projects/{pid}/trash", who=viewer_email)[0] == 200)
    st, _ = call("DELETE", f"/api/files/folders/{dest}", who=viewer_email)
    ok("VIEWER 删文件夹 → 403", st == 403, str(st))
    st, _ = call("POST", f"/api/files/folders/{dest}/restore", who=viewer_email)
    ok("VIEWER 恢复 → 403(非 409)", st == 403, str(st))
    st, _ = call("POST", f"/api/files/folders/{dest}/move", {"projectId": pid}, who=viewer_email)
    ok("VIEWER 移动 → 403", st == 403, str(st))
    st, _ = call("DELETE", f"/api/files/folders/{dest}", who=outsider_email)
    ok("非成员删 → 403(不泄露存在性)", st == 403, str(st))
    st, _ = call("POST", f"/api/files/folders/{dest}/move", {"projectId": pid}, who=outsider_email)
    ok("非成员移动 → 403", st == 403, str(st))
    st, _ = tree(pid, who=outsider_email)
    ok("非成员读文件夹树 → 403", st == 403, str(st))
    st, _ = call("DELETE", "/api/files/folders/999999", who=viewer_email)
    ok("不存在的文件夹 → 404(存在性与否先判)", st == 404, str(st))

    editor_email = f"fr-editor-{uuid.uuid4().hex[:6]}@t.local"
    _, editor = call("POST", "/api/users", {"email": editor_email, "name": "编辑", "password": "editor12345"})
    login(editor_email, "editor12345")
    call("POST", f"/api/projects/{pid}/members", {"userId": editor["id"], "role": "EDITOR"})
    call("POST", f"/api/projects/{pid_b}/members", {"userId": editor["id"], "role": "EDITOR"})
    mk = mkdir(pid, "编辑测试目录")
    st, _ = call("POST", f"/api/files/folders/{mk}/move", {"projectId": pid}, who=editor_email)
    ok("EDITOR 项目内移动 → 200", st == 200, str(st))
    st, _ = call("POST", f"/api/files/folders/{mk}/move", {"projectId": pid_b}, who=editor_email)
    ok("EDITOR 跨项目移动 → 403(需源项目 ADMIN)", st == 403, str(st))

    print("\n=== 清理 ===")
    call("DELETE", f"/api/projects/{pid}")
    call("DELETE", f"/api/projects/{pid_b}")
    print("  OK    测试项目已删除")

    print("\n" + "=" * 52)
    if FAIL:
        print(f"失败 {len(FAIL)} 项:")
        for f in FAIL:
            print("   -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
