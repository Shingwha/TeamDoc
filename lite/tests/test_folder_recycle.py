"""端到端验证:文件夹回收站 + 列表 total 字段 + 权限顺序。

用法:先启动服务(隔离数据目录 + 非常用端口),再运行本脚本。
"""
import json
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8123"
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
    boundary = "----td" + uuid.uuid4().hex
    parts = []
    for k, v in {"projectId": pid, "folderId": folder_id, "mime": "text/plain"}.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/plain\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + "/api/files/upload", data=b"".join(parts), method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("Cookie", "td_sid=" + SESSIONS[who])
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    print(("  OK  " if cond else "  FAIL") + "  " + name + ("" if cond else f"   <- {detail}"))
    if not cond:
        FAIL.append(name)


ADMIN = "admin@teamdoc.local"
VIEWER = "viewer@teamdoc.local"
OUTSIDER = "outsider@teamdoc.local"

print("=== 登录 ===")
check("管理员登录", login(ADMIN, "admin12345") == 200)

print("\n=== 准备:项目 + 两层文件夹 + 文件 ===")
st, proj = call("POST", "/api/projects", {"name": "回收站测试项目"})
check("建项目", st == 200, f"{st} {proj}")
pid = proj["id"]
st, root = call("POST", "/api/files/folders", {"name": "根文件夹", "projectId": pid})
check("建根文件夹", st == 200, f"{st} {root}")
fid = root["id"]
st, sub = call("POST", "/api/files/folders",
               {"name": "子文件夹", "projectId": pid, "parentId": fid})
check("建子文件夹", st == 200, f"{st} {sub}")
sub_id = sub["id"]
up = upload(pid, fid, "note.txt", b"hello recycle bin")
check("上传文件进根文件夹", "id" in up, str(up))
file_id = up["id"]

print("\n=== 场景1:非空文件夹不可删 ===")
st, r = call("DELETE", f"/api/files/folders/{fid}")
check("非空文件夹删除被拒 403", st == 403, f"{st} {r}")

print("\n=== 场景2:删文件 -> 删文件夹 -> 回收站可见 ===")
st, r = call("DELETE", f"/api/files/{file_id}")
check("删文件", st == 200, f"{st} {r}")
st, r = call("DELETE", f"/api/files/{file_id}")
check("重复删文件 -> 404", st == 404, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{fid}")
check("仍有子文件夹,根文件夹删除被拒 403", st == 403, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{sub_id}")
check("删子文件夹", st == 200, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{fid}")
check("删根文件夹", st == 200, f"{st} {r}")

st, trash = call("GET", f"/api/projects/{pid}/trash")
check("回收站返回 folders 字段", st == 200 and "folders" in trash, f"{st} {trash}")
fids = [f["id"] for f in trash.get("folders", [])]
check("回收站含 2 个文件夹", len(fids) == 2, str(fids))
check("按删除时间倒序(根在前)", bool(fids) and fids[0] == fid, str(fids))
check("回收站含 1 个文件", len(trash.get("files", [])) == 1, str(trash.get("files")))

print("\n=== 场景3:恢复子文件夹(父仍在回收站 -> 回落根目录) ===")
st, r = call("POST", f"/api/files/folders/{sub_id}/restore")
check("恢复子文件夹", st == 200, f"{st} {r}")
st, listing = call("GET", f"/api/files?project_id={pid}")
names = [f["name"] for f in listing.get("folders", [])]
check("子文件夹出现在项目根目录(已回落)", "子文件夹" in names, str(names))

print("\n=== 场景4:恢复文件 ===")
st, r = call("POST", f"/api/files/{file_id}/restore")
check("恢复文件", st == 200, f"{st} {r}")
st, root_list = call("GET", f"/api/files?project_id={pid}")
check("恢复的文件回到根目录(原父文件夹已删)",
      [f["name"] for f in root_list["files"]] == ["note.txt"], str(root_list["files"]))

print("\n=== 场景5:边界(状态不符 -> 409) ===")
st, r = call("POST", f"/api/files/folders/{sub_id}/restore")
check("恢复不在回收站的文件夹 -> 409", st == 409, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{sub_id}/permanent")
check("彻底删不在回收站的文件夹 -> 409", st == 409, f"{st} {r}")
st, r = call("DELETE", f"/api/files/{file_id}/permanent")
check("彻底删不在回收站的文件 -> 409", st == 409, f"{st} {r}")

print("\n=== 场景6:彻底删除文件夹(级联子文件夹 + 文件) ===")
st, fid2 = call("POST", "/api/files/folders", {"name": "级联根", "projectId": pid})
st, sub2 = call("POST", "/api/files/folders",
                {"name": "级联子", "projectId": pid, "parentId": fid2["id"]})
deep = upload(pid, sub2["id"], "deep.txt", b"deep")
st, r = call("DELETE", f"/api/files/{deep['id']}")
check("删深层文件", st == 200, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{sub2['id']}")
check("删级联子文件夹", st == 200, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{fid2['id']}")
check("删级联根文件夹", st == 200, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{fid2['id']}/permanent")
check("彻底删除级联根文件夹", st == 200, f"{st} {r}")
check("级联 2 个文件夹", r.get("removedFolders") == 2, str(r))
check("级联 1 个文件(深层文件)", r.get("removedFiles") == 1, str(r))
st, trash4 = call("GET", f"/api/projects/{pid}/trash")
cascade = {fid2["id"], sub2["id"]}
check("级联文件夹已从回收站清除",
      not (cascade & {f["id"] for f in trash4.get("folders", [])}), str(trash4.get("folders")))
check("级联文件已从回收站清除",
      deep["id"] not in {f["id"] for f in trash4.get("files", [])}, str(trash4.get("files")))

print("\n=== 场景7:恢复文件夹后其内文件可回到原文件夹 ===")
st, f2 = call("POST", "/api/files/folders", {"name": "保留夹", "projectId": pid})
keep = upload(pid, f2["id"], "keep.txt", b"keep")
call("DELETE", f"/api/files/{keep['id']}")
call("DELETE", f"/api/files/folders/{f2['id']}")
call("POST", f"/api/files/folders/{f2['id']}/restore")
st, r = call("POST", f"/api/files/{keep['id']}/restore")
check("恢复文件成功", st == 200, f"{st} {r}")
st, inner = call("GET", f"/api/files?project_id={pid}&folder_id={f2['id']}")
innames = [f["name"] for f in inner.get("files", [])]
check("文件回到原文件夹内(未回落根)", innames == ["keep.txt"], str(inner))

print("\n=== 场景8:列表 total 字段(截断提示依据) ===")
# 项目根此刻:根文件夹(已删)、子文件夹(已恢复)、级联根(已彻底删)、保留夹(已恢复)
#              根目录文件:note.txt
st, listing = call("GET", f"/api/files?project_id={pid}")
check("返回 total 字段", "total" in listing, str(listing.keys()))
check("total.folders 准确(=2:子文件夹+保留夹)",
      listing["total"]["folders"] == 2, str(listing.get("total")))
check("total.files 准确(=1:note.txt)",
      listing["total"]["files"] == 1, str(listing.get("total")))
check("total 与返回条数一致(未截断)",
      listing["total"]["folders"] == len(listing["folders"])
      and listing["total"]["files"] == len(listing["files"]), str(listing.get("total")))

print("\n=== 场景9:权限(VIEWER 不可写,可读回收站;非成员一律 403) ===")
st, u2 = call("POST", "/api/users",
              {"email": VIEWER, "name": "只读", "password": "viewer12345"})
check("建 VIEWER 用户", st in (200, 409), f"{st} {u2}")
call("POST", f"/api/projects/{pid}/members", {"email": VIEWER, "role": "VIEWER"})
# 建空文件夹并删除,使其真正在回收站:若权限判定晚于状态判定会得到 409 而非 403
st, pf = call("POST", "/api/files/folders", {"name": "权限夹", "projectId": pid})
call("DELETE", f"/api/files/folders/{pf['id']}")
st, tp = call("GET", f"/api/projects/{pid}/trash")
check("权限夹已在回收站(前置条件)",
      pf["id"] in {f["id"] for f in tp.get("folders", [])}, str(tp.get("folders")))

check("VIEWER 登录", login(VIEWER, "viewer12345") == 200)
st, r = call("DELETE", f"/api/files/folders/{pf['id']}", who=VIEWER)
check("VIEWER 删文件夹 -> 403(权限先于状态判定)", st == 403, f"{st} {r}")
st, r = call("POST", f"/api/files/folders/{pf['id']}/restore", who=VIEWER)
check("VIEWER 恢复回收站文件夹 -> 403(非 409)", st == 403, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{pf['id']}/permanent", who=VIEWER)
check("VIEWER 彻底删回收站文件夹 -> 403(非 409)", st == 403, f"{st} {r}")
st, r = call("GET", f"/api/projects/{pid}/trash", who=VIEWER)
check("VIEWER 可读回收站", st == 200, f"{st}")

st, o = call("POST", "/api/users",
             {"email": OUTSIDER, "name": "外部", "password": "outer12345"})
check("建非成员用户", st in (200, 409), f"{st} {o}")
check("非成员登录", login(OUTSIDER, "outer12345") == 200)
st, r = call("POST", f"/api/files/folders/{pf['id']}/restore", who=OUTSIDER)
check("非成员恢复回收站文件夹 -> 403(不泄露状态)", st == 403, f"{st} {r}")
st, r = call("DELETE", f"/api/files/folders/{pf['id']}/permanent", who=OUTSIDER)
check("非成员彻底删回收站文件夹 -> 403(不泄露状态)", st == 403, f"{st} {r}")
st, r = call("GET", f"/api/projects/{pid}/trash", who=OUTSIDER)
check("非成员读回收站 -> 403", st == 403, f"{st} {r}")

print("\n=== 场景10:文档回收站同样先校权限 ===")
st, doc = call("POST", f"/api/projects/{pid}/docs", {"title": "待删文档"})
st, r = call("DELETE", f"/api/docs/{doc['id']}")
check("删文档", st == 200, f"{st} {r}")
st, r = call("POST", f"/api/docs/{doc['id']}/restore", who=OUTSIDER)
check("非成员恢复回收站文档 -> 403(非 409)", st == 403, f"{st} {r}")
st, r = call("POST", f"/api/docs/{doc['id']}/restore", who=VIEWER)
check("VIEWER 恢复回收站文档 -> 403(非 409)", st == 403, f"{st} {r}")

print("\n=== 清理 ===")
st, r = call("DELETE", f"/api/projects/{pid}")
check("测试项目已删除", st == 200, f"{st} {r}")

print("\n" + "=" * 52)
if FAIL:
    print(f"!! {len(FAIL)} 项失败:")
    for f in FAIL:
        print("   - " + f)
    raise SystemExit(1)
print("全部通过")
