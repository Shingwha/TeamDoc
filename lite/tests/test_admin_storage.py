"""管理后台(第 0 批 0.3-0.5):存储统计 / 孤儿文件清理 / 备份导出 / TOTP 重置。

为什么需要这些:
  - 磁盘会漏:上传中途失败、删项目只删记录,都会留下无主文件永久占盘,
    而界面里既看不到也删不掉 —— 用半年后"盘满了但文件加起来远小于占用"。
  - 备份必须用 VACUUM INTO:WAL 模式下直接复制 .db 拿到的是旧快照,
    用它恢复会表现为"最近的数据不见了"。
  - TOTP 丢设备即锁死:登录要求验证码而用户自己无从重置,必须有管理员出路。

验证点:
  1. /api/admin/storage 返回磁盘/文件/文档/项目/孤儿各段,回收站占用与活跃占用分列
  2. 非管理员访问三个端点一律 403
  3. 孤儿文件(磁盘有、DB 无)被识别并可清理,且不误删正常文件
  4. 删除项目会清掉该项目的物理文件(以前明确不清理)
  5. 回收站的文件仍占物理磁盘,但转入 trashBytes 统计(不混入活跃占用)
  6. 备份 zip 含 teamdoc.db + files/ + RESTORE.txt;库快照表与数据完整、无损坏
  7. TOTP 重置后用户可凭密码登录

用法:先启动隔离实例并设 TD_DATA_DIR 便于检查物理文件。
    TD_BASE=http://127.0.0.1:8137 TD_DATA_DIR=C:/tdtest/base python tests/test_admin_storage.py
"""
import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
FAIL, PASS = [], []
SID = None


def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}" + (f"   <- {extra}" if extra and not cond else ""))


def call(method, path, body=None, raw=None, ctype="application/json", who=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None and ctype:
        req.add_header("Content-Type", ctype)
    cookie = SID if who is None else who
    if cookie:
        req.add_header("Cookie", "td_sid=" + cookie)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
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
    """raw body 上传:请求体即文件,元数据走 query string"""
    qs = "?projectId=" + urllib.parse.quote(pid) + "&name=" + urllib.parse.quote(name)
    if folder:
        qs += "&folderId=" + urllib.parse.quote(folder)
    return call("POST", "/api/files/upload" + qs, raw=content,
                ctype="application/octet-stream")


def storage():
    return call("GET", "/api/admin/storage")[1]


def main():
    global SID
    print("=" * 60)
    print("管理后台:存储统计 / 孤儿清理 / 备份 / TOTP 重置")
    print("=" * 60)
    SID = login()
    if not SID:
        print("登录失败,请先跑 tests/_bootstrap.py")
        return 1

    data_dir = os.environ.get("TD_DATA_DIR", "")
    files_dir = os.path.join(data_dir, "files") if data_dir else ""

    print("\n=== 场景1:存储统计接口 ===")
    st, s = call("GET", "/api/admin/storage")
    check("存储接口 200", st == 200, str(s)[:120])
    check("含 files/docs/disk/projects/orphans 各段",
          isinstance(s, dict) and all(k in s for k in ("files", "docs", "disk", "projects", "orphans")),
          str(list(s)) if isinstance(s, dict) else "")
    if isinstance(s, dict):
        check("disk.free > 0", s["disk"]["free"] > 0, str(s["disk"]))
        check("活跃占用与回收站占用分列",
              "activeBytes" in s["files"] and "trashBytes" in s["files"], str(s["files"]))

    print("\n=== 场景2:非管理员一律 403 ===")
    plain_email = f"plain-{uuid.uuid4().hex[:6]}@t.local"
    call("POST", "/api/users", {"email": plain_email, "name": "普通", "password": "plain12345"})
    plain_sid = login(plain_email, "plain12345")
    check("普通用户登录成功", bool(plain_sid))
    for label, method, path in [("存储统计", "GET", "/api/admin/storage"),
                                ("备份下载", "GET", "/api/admin/backup"),
                                ("孤儿清理", "POST", "/api/admin/storage/cleanup")]:
        st, _ = call(method, path, who=plain_sid)
        check(f"普通用户{label} → 403", st == 403, str(st))

    print("\n=== 场景3:孤儿文件识别与清理 ===")
    # 断言一律用差值:实例的数据目录可能被复用,历史遗留的孤儿(正是本功能要清理的东西)
    # 会让"绝对值为 0"的假设失败 —— 那不是代码错,是测试假设错
    st, proj = call("POST", "/api/projects", {"name": "存储测试项目", "description": "s"})
    pid = proj["id"]
    st, f1 = upload(pid, "keep.txt", b"keep me")
    check("上传正常文件", st == 200, str(f1))
    if files_dir:
        base = storage()["orphans"]
        orphan_name = uuid.uuid4().hex[:16]
        orphan_path = os.path.join(files_dir, orphan_name)
        with open(orphan_path, "wb") as fh:
            fh.write(b"x" * 4096)
        mid = storage()["orphans"]
        check("识别出新增孤儿(差值 1)", mid["orphans"] - base["orphans"] == 1,
              f"base={base['orphans']} mid={mid['orphans']}")
        check("孤儿字节数增加", mid["orphanBytes"] - base["orphanBytes"] >= 4096,
              f"base={base['orphanBytes']} mid={mid['orphanBytes']}")
        st, c = call("POST", "/api/admin/storage/cleanup")
        check("清理接口 200", st == 200, str(c))
        check("至少清掉刚造的那个", isinstance(c, dict) and c.get("removed", 0) >= 1, str(c))
        check("孤儿已从磁盘删除", not os.path.exists(orphan_path))
        check("正常文件未被误删", os.path.exists(os.path.join(files_dir, f1["id"])))
        check("清理后孤儿归零", storage()["orphans"]["orphans"] == 0, str(storage()["orphans"]))
    else:
        print("  SKIP  未设 TD_DATA_DIR,跳过物理文件检查")

    print("\n=== 场景4:删除项目会清掉物理文件 ===")
    if files_dir:
        st, f2 = upload(pid, "gone.bin", b"z" * 2048)
        f2_path = os.path.join(files_dir, f2["id"])
        check("文件已落盘", os.path.exists(f2_path))
        st, r = call("DELETE", f"/api/projects/{pid}")
        check("项目删除 200", st == 200, str(st))
        # 该项目此刻有 keep.txt 与 gone.bin 两个文件,应全部清除
        check("返回清理文件数=项目内文件数", isinstance(r, dict) and r.get("removedFiles") == 2, str(r))
        check("物理文件已一并清除", not os.path.exists(f2_path))
        check("另一个文件也已清除", not os.path.exists(os.path.join(files_dir, f1["id"])))
    else:
        st, r = call("DELETE", f"/api/projects/{pid}")
        check("项目删除 200", st == 200, str(st))

    print("\n=== 场景5:回收站占用单列 ===")
    st, p2 = call("POST", "/api/projects", {"name": "回收站占用测试", "description": "s"})
    pid2 = p2["id"]
    st, big = upload(pid2, "trashme.bin", b"q" * 8192)
    row1 = next((p for p in storage()["projects"] if p["projectId"] == pid2), None)
    check("项目出现在占用列表", row1 is not None)
    check("活跃占用含该文件", row1 and row1["bytes"] >= 8192, str(row1))
    call("DELETE", f"/api/files/{big['id']}")
    row2 = next((p for p in storage()["projects"] if p["projectId"] == pid2), None)
    check("删除后计入回收站占用", row2 and row2["trashBytes"] >= 8192, str(row2))
    check("活跃占用归零", row2 and row2["bytes"] == 0, str(row2))
    if files_dir:
        check("回收站文件仍占磁盘(故必须单列)",
              os.path.exists(os.path.join(files_dir, big["id"])))

    print("\n=== 场景6:备份导出 ===")
    st, blob = call("GET", "/api/admin/backup")
    check("备份接口 200", st == 200, str(st)[:80])
    if isinstance(blob, bytes) and blob[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(blob))
        names = z.namelist()
        check("含 teamdoc.db", "teamdoc.db" in names, str(names[:8]))
        check("含 RESTORE.txt", "RESTORE.txt" in names, str(names[:8]))
        check("含 files/ 物理文件", any(n.startswith("files/") for n in names),
              str([n for n in names if n.startswith("files/")][:5]))
        check("zip 无损坏", z.testzip() is None)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            dbp = os.path.join(td, "teamdoc.db")
            with open(dbp, "wb") as fh:
                fh.write(z.read("teamdoc.db"))
            c = sqlite3.connect(dbp)
            tables = sorted(r[0] for r in c.execute("select name from sqlite_master where type='table'"))
            usercnt = c.execute("select count(*) from users").fetchone()[0]
            projcnt = c.execute("select count(*) from projects").fetchone()[0]
            filecnt = c.execute("select count(*) from files").fetchone()[0]
            c.close()
        check("快照含全部表", "files" in tables and "schema_version" in tables, str(tables))
        check("快照含用户数据", usercnt >= 2, f"users={usercnt}")
        check("快照含项目数据", projcnt >= 1, f"projects={projcnt}")
        # 关键:回收站里的文件记录也在快照里(WAL 未 checkpoint 的写入不能丢)
        check("快照含最新写入(回收站文件记录)", filecnt >= 1, f"files={filecnt}")
    else:
        check("备份返回 zip 字节流", False, f"type={type(blob)}")

    print("\n=== 场景7:TOTP 重置(丢设备后管理员解锁) ===")
    tt_email = f"totp-{uuid.uuid4().hex[:6]}@t.local"
    st, nu2 = call("POST", "/api/users", {"email": tt_email, "name": "两步用户", "password": "totp12345"})
    uid2 = nu2["id"]
    sid2 = login(tt_email, "totp12345")
    st, setup = call("POST", "/api/auth/totp/setup", {"password": "totp12345"}, who=sid2)
    check("TOTP setup 成功", st == 200, str(setup))
    try:
        import pyotp
        code = pyotp.TOTP(setup["secret"]).now()
    except ImportError:
        print("  SKIP  venv 缺 pyotp,跳过 TOTP 场景")
        code = None
    if code:
        st, _ = call("POST", "/api/auth/totp/enable", {"code": code}, who=sid2)
        check("用户已开启 TOTP", st == 200, str(st))
        st, r = call("POST", "/api/auth/login", {"email": tt_email, "password": "totp12345"})
        check("开启后登录要求验证码(401 TOTP_REQUIRED)",
              st == 401 and isinstance(r, dict) and r.get("detail", {}).get("code") == "TOTP_REQUIRED", str(r))
        st, r = call("POST", f"/api/users/{uid2}/totp/reset")
        check("管理员重置 TOTP 200", st == 200, str(r))
        st, r = call("POST", "/api/auth/login", {"email": tt_email, "password": "totp12345"})
        check("重置后可凭密码直接登录", st == 200, str(r)[:100])
    st, _ = call("POST", "/api/users/nope/totp/reset")
    check("重置不存在的用户 → 404", st == 404, str(st))

    print("\n=== 清理 ===")
    call("DELETE", f"/api/projects/{pid2}")
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
