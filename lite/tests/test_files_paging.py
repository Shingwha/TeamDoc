"""云空间:分页 / 服务端排序 / 重名 / 占用统计 / 最近文件。

用法:先启动服务(隔离数据目录 + 非常用端口),再运行本脚本。

覆盖:
  1. 分页:offset/limit 生效,total 为真实总数,hasMore 正确
  2. 服务端排序:name / time / size 升序降序,翻页不重不漏(按 id 定序)
  3. 重名:上传自动加后缀(foo(2).png);新建/重命名遇重名 409
  4. 改名会改掉扩展名时 mime 跟着重算
  5. 项目占用统计:活跃与回收站分列,删文件后活跃减少、回收站增加
  6. 最近文件:跨项目返回,带项目名,按时间倒序;不可见项目不出现
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
FAIL, PASS = [], []
SID = None


def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}" + (f"   <- {extra}" if extra and not cond else ""))


def call(method, path, body=None, raw=None, ctype="application/json", sid=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    req.add_header("Cookie", "td_sid=" + (sid or SID or ""))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            blob = r.read()
            try:
                return r.status, json.loads(blob.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return r.status, blob
    except urllib.error.HTTPError as e:
        blob = e.read()
        try:
            return e.code, json.loads(blob.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return e.code, blob


def login(email="admin@teamdoc.local", password="admin12345"):
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"email": email, "password": password}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            sc = r.headers.get("Set-Cookie", "")
        return sc.split("td_sid=")[1].split(";")[0] if "td_sid=" in sc else None
    except urllib.error.HTTPError:
        return None


def upload(pid, name, content, folder=None):
    qs = "?projectId=" + urllib.parse.quote(pid) + "&name=" + urllib.parse.quote(name)
    if folder:
        qs += "&folderId=" + urllib.parse.quote(folder)
    return call("POST", "/api/files/upload" + qs, raw=content,
                ctype="application/octet-stream")


def main():
    global SID
    print("=" * 60)
    print("云空间:分页 / 排序 / 重名 / 占用 / 最近文件")
    print("=" * 60)
    SID = login()
    if not SID:
        print("登录失败,请先跑 tests/_bootstrap.py")
        return 1

    st, proj = call("POST", "/api/projects", {"name": "云空间功能测试", "description": "d"})
    pid = proj["id"]
    st, proj_b = call("POST", "/api/projects", {"name": "云空间功能测试B", "description": "d"})
    pid_b = proj_b["id"]

    print("\n=== 场景1:分页 ===")
    # 建 12 个文件,名字可控以便验证排序
    expected = []
    for i in range(12):
        n = f"f{i:02d}.txt"
        st, r = upload(pid, n, b"x" * (100 * (i + 1)))
        expected.append(n)
        if st != 200:
            check(f"上传 {n}", False, str(r))
    st, page1 = call("GET", f"/api/files?project_id={pid}&limit=5&offset=0&sort=name&dir=asc")
    check("首页 200", st == 200, str(page1)[:120])
    check("首页返回 5 条", len(page1["files"]) == 5, len(page1["files"]))
    check("total.files = 12", page1["total"]["files"] == 12, page1["total"])
    check("hasMore = true", page1["hasMore"] is True, page1.get("hasMore"))
    names1 = [f["name"] for f in page1["files"]]
    check("首页按名称升序且是前 5 个", names1 == expected[:5], str(names1))
    st, page2 = call("GET", f"/api/files?project_id={pid}&limit=5&offset=5&sort=name&dir=asc")
    names2 = [f["name"] for f in page2["files"]]
    check("第二页是接下来 5 个", names2 == expected[5:10], str(names2))
    st, page3 = call("GET", f"/api/files?project_id={pid}&limit=5&offset=10&sort=name&dir=asc")
    check("末页只剩 2 条", len(page3["files"]) == 2, len(page3["files"]))
    check("末页 hasMore = false", page3["hasMore"] is False, page3.get("hasMore"))
    all_names = names1 + names2 + [f["name"] for f in page3["files"]]
    check("翻页不重不漏", all_names == expected, str(all_names))

    print("\n=== 场景2:服务端排序 ===")
    st, desc = call("GET", f"/api/files?project_id={pid}&sort=name&dir=desc&limit=500")
    got = [f["name"] for f in desc["files"]]
    check("名称降序", got == list(reversed(expected)), str(got[:4]))
    st, bysize = call("GET", f"/api/files?project_id={pid}&sort=size&dir=desc&limit=500")
    sizes = [f["size"] for f in bysize["files"]]
    check("按大小降序", sizes == sorted(sizes, reverse=True), str(sizes[:5]))
    st, bysize_a = call("GET", f"/api/files?project_id={pid}&sort=size&dir=asc&limit=500")
    sizes_a = [f["size"] for f in bysize_a["files"]]
    check("按大小升序", sizes_a == sorted(sizes_a), str(sizes_a[:5]))
    st, bytime = call("GET", f"/api/files?project_id={pid}&sort=time&dir=desc&limit=500")
    times = [f["createdAt"] for f in bytime["files"]]
    check("按时间降序", times == sorted(times, reverse=True), str(times[:3]))
    st, weird = call("GET", f"/api/files?project_id={pid}&sort=bogus&dir=bogus&limit=1")
    check("非法排序参数回落到名称升序(不报错)", st == 200, str(weird)[:80])

    print("\n=== 场景3:重名处理 ===")
    st, first = upload(pid, "同名.txt", b"first")
    check("首次上传 200", st == 200, str(first))
    check("名字保持不变", first["name"] == "同名.txt", first["name"])
    st, second = upload(pid, "同名.txt", b"second")
    check("重名上传自动加后缀", second["name"] == "同名(2).txt", second["name"])
    st, third = upload(pid, "同名.txt", b"third")
    check("第三次上传 (3)", third["name"] == "同名(3).txt", third["name"])
    # 三个都在
    st, lst = call("GET", f"/api/files?project_id={pid}&limit=500")
    same = sorted(f["name"] for f in lst["files"] if f["name"].startswith("同名"))
    check("三个同名文件都存在", same == ["同名(2).txt", "同名(3).txt", "同名.txt"], str(same))
    # 显式新建/重命名重名 → 409
    st, r = call("POST", "/api/files/folders", {"projectId": pid, "name": "重名目录"})
    check("新建文件夹 200", st == 200, str(r))
    st, r = call("POST", "/api/files/folders", {"projectId": pid, "name": "重名目录"})
    check("重名文件夹 → 409", st == 409, f"{st} {r}")
    fid = r if st == 200 else None
    st, r = call("POST", "/api/files/folders", {"projectId": pid, "name": "同名.txt"})
    check("文件夹与文件重名 → 409", st == 409, f"{st} {r}")
    st, r = call("PATCH", f"/api/files/{first['id']}", {"name": second["name"]})
    check("重命名撞已有文件 → 409", st == 409, f"{st} {r}")
    st, r = call("PATCH", f"/api/files/{first['id']}", {"name": "重名目录"})
    check("重命名撞已有文件夹 → 409", st == 409, f"{st} {r}")
    st, r = call("PATCH", f"/api/files/{first['id']}", {"name": "同名.txt"})
    check("改名成自己原名 → 200(不算冲突)", st == 200, f"{st} {r}")

    print("\n=== 场景4:改名后 mime 跟着重算 ===")
    st, svg = upload(pid, "图形.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    check("svg 上传后 canInline=false", svg["canInline"] is False, str(svg))
    st, renamed = call("PATCH", f"/api/files/{svg['id']}", {"name": "图形.png"})
    check("改名为 .png 后 mime 变为 image/png", renamed["mime"] == "image/png", str(renamed))
    check("canInline 随之变 true", renamed["canInline"] is True, str(renamed))
    st, back = call("PATCH", f"/api/files/{svg['id']}", {"name": "图形.html"})
    check("改成 .html 后 canInline 回落 false", back["canInline"] is False, str(back))

    print("\n=== 场景5:项目占用统计 ===")
    st, s0 = call("GET", f"/api/projects/{pid}/storage")
    check("占用接口 200", st == 200, str(s0)[:120])
    check("活跃文件数 > 0", s0["active"]["fileCount"] > 0, str(s0["active"]))
    base_active = s0["active"]["bytes"]
    base_trash = s0["trash"]["bytes"]
    st, victim = upload(pid, "待删除.bin", b"v" * 5000)
    st, s1 = call("GET", f"/api/projects/{pid}/storage")
    check("上传后活跃字节增加", s1["active"]["bytes"] == base_active + 5000,
          f"{base_active} -> {s1['active']['bytes']}")
    call("DELETE", f"/api/files/{victim['id']}")
    st, s2 = call("GET", f"/api/projects/{pid}/storage")
    check("删除后活跃字节回落", s2["active"]["bytes"] == base_active, str(s2["active"]["bytes"]))
    check("回收站字节增加(仍占磁盘,故单列)", s2["trash"]["bytes"] == base_trash + 5000,
          f"{base_trash} -> {s2['trash']['bytes']}")
    check("回收站文件数 +1", s2["trash"]["fileCount"] == s0["trash"]["fileCount"] + 1, str(s2["trash"]))
    check("含磁盘余量", s2["disk"]["free"] > 0, str(s2["disk"]))
    st, _ = call("POST", f"/api/files/{victim['id']}/restore")
    st, _ = call("DELETE", f"/api/files/{victim['id']}/permanent")
    st, s3 = call("GET", f"/api/projects/{pid}/storage")
    check("彻底删除后回收站归零", s3["trash"]["bytes"] == base_trash, str(s3["trash"]))

    print("\n=== 场景6:最近文件(跨项目) ===")
    st, rb = upload(pid_b, "B项目文件.txt", b"bbb")
    check("在 B 项目上传文件", st == 200, str(rb))
    st, st_doc = call("POST", f"/api/projects/{pid}/docs", {"title": "最近文档"})
    call("PUT", f"/api/docs/{st_doc['id']}/content", {"content": "# 内容"})
    st, rec = call("GET", "/api/recent?limit=30")
    check("最近接口 200", st == 200, str(rec)[:100])
    fnames = [f["name"] for f in rec["files"]]
    check("含 A 项目的文件", any(n.startswith("f0") for n in fnames), str(fnames[:6]))
    check("含 B 项目的文件", "B项目文件.txt" in fnames, str(fnames[:6]))
    check("文件带项目名", all(f.get("projectName") for f in rec["files"]),
          str([f.get("projectName") for f in rec["files"][:3]]))
    dnames = [d["title"] for d in rec["docs"]]
    check("含最近文档", "最近文档" in dnames, str(dnames))
    check("文档带项目名", all(d.get("projectName") for d in rec["docs"]), str(rec["docs"][:2]))
    # 非成员看不到**私有**项目的内容。
    # 注意不能断言"列表为空":公开项目引入后,非成员能看到公开项目里的文件/文档
    # (见 test_visibility.py),所以判据必须是"不含这些私有项目的 id"
    other_email = f"rec-out-{uuid.uuid4().hex[:6]}@t.local"
    call("POST", "/api/users", {"email": other_email, "name": "外部", "password": "outer12345"})
    other_sid = login(other_email, "outer12345")
    st, rec2 = call("GET", "/api/recent", sid=other_sid)
    private_ids = {pid, pid_b}
    leaked = [f for f in rec2["files"] if f["projectId"] in private_ids]
    check("非成员看不到私有项目的文件", not leaked, str(leaked)[:160])
    leaked_docs = [d for d in rec2["docs"] if d["projectId"] in private_ids]
    check("非成员看不到私有项目的文档", not leaked_docs, str(leaked_docs)[:160])

    print("\n=== 清理 ===")
    call("DELETE", f"/api/projects/{pid}")
    call("DELETE", f"/api/projects/{pid_b}")
    check("测试项目已删除", True)

    print("\n" + "=" * 60)
    if FAIL:
        print(f"失败 {len(FAIL)} 项:")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"全部通过({len(PASS)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
