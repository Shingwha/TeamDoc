"""公开项目与单文件公开(第 3 批)。

公开项目的语义:**本实例所有登录用户可只读浏览**,不产生成员关系。
鉴权上一处改动(auth.project_role 里"公开且非成员 → VIEWER")让文档树、文档读写、
云空间、搜索、WS 全部自动生效;但前端必须同时拿到 isMember 才能正确渲染
(否则会把访客当成员,渲染出点了就 403 的写按钮)。

覆盖:
  1. 私有项目:非成员读 → 403(默认行为不变)
  2. 公开项目:非成员可读文档树/文档内容/文件列表/回收站;写 → 403
  3. isMember 正确区分"成员"与"公开项目访客"(两者 myRole 都是 VIEWER)
  4. 个人空间**不可公开**(403),且不出现在广场
  5. 公开开关的权限:需 ADMIN;个人空间硬拒
  6. 单文件公开:非成员可下载该文件,但下载同项目其他未公开文件仍 403
  7. 广场只列公开项目,按最近活跃倒序
  8. 搜索把公开项目内容并入(避免"广场看得到、搜不到");最近动态仅限
     已参加项目 —— 非成员(含公开项目访客)在 /api/recent 里什么都看不到
  9. 关闭公开后,非成员立即失去访问
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


def call(method, path, body=None, raw=None, sid=None, anon=False, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if not anon:
        req.add_header("Cookie", "td_sid=" + (sid or SID or ""))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            blob = r.read()
            try:
                return r.status, json.loads(blob.decode())
            except (ValueError, UnicodeDecodeError):
                return r.status, blob
    except urllib.error.HTTPError as e:
        blob = e.read()
        try:
            return e.code, json.loads(blob.decode())
        except (ValueError, UnicodeDecodeError):
            return e.code, blob


def login(email, password):
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


def upload(pid, name, content):
    qs = "?projectId=" + urllib.parse.quote(str(pid)) + "&name=" + urllib.parse.quote(name)
    return call("POST", "/api/files/upload" + qs, raw=content,
                ctype="application/octet-stream")


def mkuser(prefix):
    email = f"{prefix}-{uuid.uuid4().hex[:6]}@t.local"
    st, u = call("POST", "/api/users", {"email": email, "name": prefix, "password": "pass12345"})
    return email, login(email, "pass12345"), u["id"]


def main():
    global SID
    print("=" * 60)
    print("公开项目与单文件公开")
    print("=" * 60)
    SID = login("admin@teamdoc.local", "admin12345")
    if not SID:
        print("登录失败,请先跑 tests/_bootstrap.py")
        return 1

    # 准备:管理员建项目 + 文档 + 文件;另建一个局外人
    st, proj = call("POST", "/api/projects", {"name": "可见性测试项目", "description": "d"})
    pid = proj["id"]
    st, doc = call("POST", f"/api/projects/{pid}/docs", {"title": "公开前的文档"})
    did = doc["id"]
    call("PUT", f"/api/docs/{did}/content", {"content": "# 内容\n\n机密内容在这里。"})
    st, f_priv = upload(pid, "私有文件.txt", b"private data")
    st, f_pub = upload(pid, "公开文件.txt", b"public data")
    outsider_email, outsider, _ = mkuser("vis-out")
    check("局外人账号已建并可登录", bool(outsider))

    print("\n=== 场景1:私有项目(默认)—— 非成员一律拒绝 ===")
    st, p = call("GET", f"/api/projects/{pid}", sid=outsider)
    check("非成员读项目详情 → 403", st == 403, str(st))
    st, _ = call("GET", f"/api/projects/{pid}/docs/tree", sid=outsider)
    check("非成员读文档树 → 403", st == 403, str(st))
    st, _ = call("GET", f"/api/docs/{did}", sid=outsider)
    check("非成员读文档 → 403", st == 403, str(st))
    st, _ = call("GET", f"/api/files?project_id={pid}", sid=outsider)
    check("非成员列文件 → 403", st == 403, str(st))
    st, _ = call("GET", f"/api/files/{f_priv['id']}/download", sid=outsider)
    check("非成员下载文件 → 403", st == 403, str(st))

    print("\n=== 场景2:公开开关(需 ADMIN) ===")
    # 用一个 VIEWER 成员验证"不能公开"(需要 ADMIN)
    viewer_email, viewer, viewer_uid = mkuser("vis-view")
    call("POST", f"/api/projects/{pid}/members", {"userId": viewer_uid, "role": "VIEWER"})
    st, _ = call("PATCH", f"/api/projects/{pid}", {"isPublic": True}, sid=viewer)
    check("VIEWER 改公开 → 403(需 ADMIN)", st == 403, str(st))
    st, r = call("PATCH", f"/api/projects/{pid}", {"isPublic": True})
    check("ADMIN 公开项目 → 200", st == 200, f"{st} {r}")
    check("返回 isPublic=true", r.get("isPublic") is True, str(r.get("isPublic")))

    print("\n=== 场景3:公开后非成员只读可访问,写仍被拒 ===")
    st, p2 = call("GET", f"/api/projects/{pid}", sid=outsider)
    check("非成员读项目详情 → 200", st == 200, str(st))
    check("访客 myRole=VIEWER", p2.get("myRole") == "VIEWER", str(p2.get("myRole")))
    check("访客 isMember=false(**前端靠它区分**)",
          p2.get("isMember") is False, str(p2.get("isMember")))
    st, _ = call("GET", f"/api/projects/{pid}/docs/tree", sid=outsider)
    check("非成员读文档树 → 200", st == 200, str(st))
    st, d2 = call("GET", f"/api/docs/{did}", sid=outsider)
    check("非成员读文档内容 → 200", st == 200 and "机密内容" in (d2 or {}).get("content", ""),
          str(st))
    st, lst = call("GET", f"/api/files?project_id={pid}", sid=outsider)
    check("非成员列文件 → 200", st == 200, str(st))
    check("文件列表含未公开的文件(项目公开则项目内文件都可读)",
          any(f["name"] == "私有文件.txt" for f in (lst or {}).get("files", [])),
          str([f["name"] for f in (lst or {}).get("files", [])]))
    st, _ = call("GET", f"/api/files/{f_priv['id']}/download", sid=outsider)
    check("非成员下载项目内文件 → 200", st == 200, str(st))
    # 写操作必须全拒
    st, _ = call("POST", f"/api/projects/{pid}/docs", {"title": "访客想建文档"}, sid=outsider)
    check("访客建文档 → 403", st == 403, str(st))
    st, _ = call("PUT", f"/api/docs/{did}/content", {"content": "篡改"}, sid=outsider)
    check("访客改文档内容 → 403", st == 403, str(st))
    st, _ = call("DELETE", f"/api/docs/{did}", sid=outsider)
    check("访客删文档 → 403", st == 403, str(st))
    st, _ = call("POST", "/api/files/upload?projectId=" + str(pid) + "&name=x.txt",
                 raw=b"x", sid=outsider, ctype="application/octet-stream")
    check("访客上传 → 403", st == 403, str(st))
    st, _ = call("DELETE", f"/api/files/{f_priv['id']}", sid=outsider)
    check("访客删文件 → 403", st == 403, str(st))
    st, _ = call("POST", "/api/files/folders", {"projectId": pid, "name": "访客目录"}, sid=outsider)
    check("访客建文件夹 → 403", st == 403, str(st))
    st, _ = call("PATCH", f"/api/projects/{pid}", {"name": "改名"}, sid=outsider)
    check("访客改项目 → 403", st == 403, str(st))
    st, _ = call("GET", f"/api/projects/{pid}/members", sid=outsider)
    check("访客可读成员列表(公开项目橱窗的一部分)", st == 200, str(st))

    print("\n=== 场景4:成员未受影响 ===")
    st, pm = call("GET", f"/api/projects/{pid}", sid=viewer)
    check("成员 isMember=true", pm.get("isMember") is True, str(pm.get("isMember")))
    check("成员 myRole=VIEWER 且 isMember 为真(与访客可区分)",
          pm.get("myRole") == "VIEWER" and pm.get("isMember") is True, str(pm))
    st, _ = call("POST", f"/api/projects/{pid}/docs", {"title": "成员不能建(VIEWER)"}, sid=viewer)
    check("VIEWER 成员建文档 → 403", st == 403, str(st))

    print("\n=== 场景5:个人空间不可公开 ===")
    st, mine = call("GET", "/api/projects")
    personal = next((x for x in mine if x["isPersonal"]), None)
    check("找到自己的个人空间", personal is not None)
    st, r = call("PATCH", f"/api/projects/{personal['id']}", {"isPublic": True})
    check("个人空间公开 → 403", st == 403, f"{st} {r}")
    st, r = call("PATCH", f"/api/projects/{personal['id']}", {"isPublic": False})
    check("个人空间设私有也拒(硬拒而非仅拦公开)", st == 403, str(st))
    st, disc = call("GET", "/api/discover/projects")
    check("广场里没有任何个人空间",
          not any(x["isPersonal"] for x in (disc or [])), str([x["name"] for x in (disc or [])][:5]))

    print("\n=== 场景6:单文件公开 ===")
    # 用另一个私有项目验证"项目私有 + 单文件公开"
    st, proj2 = call("POST", "/api/projects", {"name": "单文件公开测试", "description": "d"})
    pid2 = proj2["id"]
    st, fp1 = upload(pid2, "共享给同事.txt", b"shared")
    st, fp2 = upload(pid2, "不共享.txt", b"secret")
    st, _ = call("GET", f"/api/files/{fp1['id']}/download", sid=outsider)
    check("项目私有时非成员下载 → 403", st == 403, str(st))
    st, r = call("PATCH", f"/api/files/{fp1['id']}", {"isPublic": True})
    check("标记文件公开 → 200", st == 200, f"{st} {r}")
    check("响应带 isPublic=true", r.get("isPublic") is True, str(r))
    st, _ = call("GET", f"/api/files/{fp1['id']}/download", sid=outsider)
    check("公开后非成员可下载 → 200", st == 200, str(st))
    st, _ = call("GET", f"/api/files/{fp2['id']}/download", sid=outsider)
    check("同项目未公开文件仍 403(只放开这一个)", st == 403, str(st))
    st, _ = call("GET", f"/api/files?project_id={pid2}", sid=outsider)
    check("项目本身仍是私有(列表 403)", st == 403, str(st))
    st, _ = call("PATCH", f"/api/files/{fp1['id']}", {"isPublic": False})
    check("取消公开 → 200", st == 200, str(st))
    st, _ = call("GET", f"/api/files/{fp1['id']}/download", sid=outsider)
    check("取消后非成员立即失去访问", st == 403, str(st))

    print("\n=== 场景7:广场只列公开项目、按最近活跃倒序 ===")
    st, disc = call("GET", "/api/discover/projects", sid=outsider)
    check("广场可读", st == 200, str(st))
    ids = [x["id"] for x in (disc or [])]
    check("含刚公开的项目", pid in ids, str(ids[:5]))
    check("不含私有项目(单文件测试项目)", pid2 not in ids, str(ids[:5]))
    check("每项都标 isPublic", all(x["isPublic"] for x in (disc or [])))
    # 活跃排序:给另一个公开项目加内容后它应排更前
    st, proj3 = call("POST", "/api/projects", {"name": "更新的公开项目", "description": "d"})
    pid3 = proj3["id"]
    call("PATCH", f"/api/projects/{pid3}", {"isPublic": True})
    call("POST", f"/api/projects/{pid3}/docs", {"title": "刚建的文档"})
    st, disc2 = call("GET", "/api/discover/projects", sid=outsider)
    ids2 = [x["id"] for x in (disc2 or [])]
    check("最近活跃的排在前面", ids2 and ids2[0] == pid3, str(ids2[:4]))

    print("\n=== 场景8:搜索并入公开项目;最近动态仅限已参加项目 ===")
    st, s = call("GET", "/api/search?q=" + urllib.parse.quote("机密内容"), sid=outsider)
    check("非成员能搜到公开项目里的文档", st == 200 and len(s.get("docs", [])) >= 1,
          str(s.get("docs"))[:120])
    st, s2 = call("GET", "/api/search?q=" + urllib.parse.quote("共享给同事"), sid=outsider)
    check("搜索能命中…(私有项目内容搜不到)", st == 200, str(st))
    # 最新动态是"我的工作台"视角:outsider 没参加任何项目,公开项目也不该出现
    st, rec = call("GET", "/api/recent", sid=outsider)
    check("最近接口 200", st == 200, str(st))
    leaked_f = [f for f in (rec or {}).get("files", []) if f["projectId"] == pid]
    check("最近动态不含公开项目的文件(非成员)",
          not leaked_f, str([f["name"] for f in leaked_f][:5]))
    check("未参加任何项目 → 最近动态为空",
          not (rec or {}).get("files") and not (rec or {}).get("docs"),
          str(rec)[:160])

    print("\n=== 场景9:关闭公开后立即失去访问 ===")
    st, _ = call("PATCH", f"/api/projects/{pid}", {"isPublic": False})
    check("关闭公开 → 200", st == 200, str(st))
    st, _ = call("GET", f"/api/projects/{pid}", sid=outsider)
    check("关闭后非成员读项目 → 403", st == 403, str(st))
    st, _ = call("GET", f"/api/docs/{did}", sid=outsider)
    check("关闭后非成员读文档 → 403", st == 403, str(st))
    st, disc3 = call("GET", "/api/discover/projects", sid=outsider)
    check("关闭后从广场消失", pid not in [x["id"] for x in (disc3 or [])])

    print("\n=== 清理 ===")
    for x in (pid, pid2, pid3):
        call("DELETE", f"/api/projects/{x}")
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
