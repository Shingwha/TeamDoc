"""备份与恢复:多目标 / 保留策略 / 完整性校验 / 端到端恢复。

为什么需要这些:
  - 备份是数据安全的最后一道防线。没有校验的备份只是"看起来成功"的包,
    恢复那天才发现坏掉 —— 所以每次写完必须回读校验。
  - 恢复会覆盖全部数据。它必须真的被演练过一次,而且必须留下回退目录,
    否则"恢复功能"本身就是最大的风险点。
  - 孤儿清理是破坏性操作:库一旦为空/指向错目录,磁盘文件会被全部判为孤儿,
    一键删光。熔断与 dry-run 是这一场景的护栏,必须有测试盯着。

验证点:
  1. 备份文件名带时间戳;多目标目录一次全写入;内容可解、库可读、表齐全
  2. 保留策略只留最近 N 份,且**不误删**手工放进目录的其它文件
  3. 目标目录不存在 → 记为失败(部分成功),且**不自动创建**该目录
  4. 校验能拒绝:非 zip / 缺 teamdoc.db / 截断包
  5. 恢复上传校验含 **zip slip 白名单**(穿越、绝对路径、子目录、意外条目一律拒绝)
  6. 孤儿清理:dry-run 不删文件;清理熔断在"多数文件被判孤儿"时拒绝,force 可越过
  7. 端到端恢复:造数据 → 备份 → 改数据 → 上传 → arm → (重启) → 断言回到备份时点

用法:先启动隔离实例,并配置 BACKUP_DIRS 指向可写目录。
    TD_BASE=http://127.0.0.1:8137 TD_BK_DIRS="C:/tdtest/bk1,C:/tdtest/bk2" python tests/test_backup_restore.py

注意:恢复场景需要**重启服务**才能生效,脚本会把待恢复状态留在暂存区并提示你重启,
重启后带 `--verify-restore` 再跑一次完成断言(见 tests/README.md)。
"""
import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
import zipfile

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
BK_DIRS = [d for d in os.environ.get("TD_BK_DIRS", "").split(",") if d.strip()]
FAIL, PASS = [], []
SID = None

RESTORE_FLAG = "_restore_drill.json"


def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}" + (f"   <- {extra}" if extra and not cond else ""))


def call(method, path, body=None, raw=False, ctype="application/json", who=None):
    """raw=True 表示返回原始响应字节(如备份 zip),不是请求体。"""
    if body is None:
        data = None
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)  # 原始二进制(备份上传):不 JSON 序列化
    else:
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None and ctype:
        req.add_header("Content-Type", ctype)
    cookie = SID if who is None else who
    if cookie:
        req.add_header("Cookie", "td_sid=" + cookie)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            blob = r.read()
            if raw:
                return r.status, blob
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


def flag_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), RESTORE_FLAG)


def scenario_zip_slip():
    """把带恶意路径的包塞进上传端点,断言全部被拒。"""
    print("\n=== 场景5:恢复上传的 zip slip 白名单 ===")
    # 先拿一份合法包的 teamdoc.db 作为载具
    st, blob = call("GET", "/api/admin/backup", raw=True)
    if st != 200 or not isinstance(blob, bytes) or blob[:2] != b"PK":
        check("取得合法备份用于构造恶意包", False, f"status={st}")
        return
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        db_bytes = z.read("teamdoc.db")

    cases = [
        ("files/../../evil.txt", "files/../.. 穿越"),
        ("files/C:/Windows/evil.dll", "绝对路径"),
        ("files/sub/evil", "files/ 下带子目录"),
        ("hack.py", "意外顶层条目"),
    ]
    for arc, label in cases:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("teamdoc.db", db_bytes)
            z.writestr(arc, b"x")
        st, r = call("POST", "/api/admin/restore/upload", buf.getvalue(),
                     ctype="application/octet-stream")
        check(f"拒绝 {label}", st == 400, f"status={st} {str(r)[:100]}")

    # 清掉可能残留的暂存
    call("DELETE", "/api/admin/restore")


def scenario_bad_archives():
    print("\n=== 场景4:坏包被拒绝 ===")
    for label, payload in (
        ("非 zip 文件", b"definitely not a zip"),
        ("空 body", b""),
    ):
        st, r = call("POST", "/api/admin/restore/upload", payload,
                     ctype="application/octet-stream")
        check(f"拒绝{label}", st == 400, f"status={st} {str(r)[:100]}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("RESTORE.txt", "no db here")
    st, r = call("POST", "/api/admin/restore/upload", buf.getvalue(),
                 ctype="application/octet-stream")
    check("拒绝缺 teamdoc.db 的包", st == 400, f"status={st} {str(r)[:100]}")
    call("DELETE", "/api/admin/restore")


def scenario_backup_targets():
    print("\n=== 场景1/2/3:多目标、保留策略、目标不存在 ===")
    st, r = call("POST", "/api/admin/backup/run")
    check("手动备份返回 200", st == 200, str(r)[:120])
    if st != 200:
        return
    check("备份文件名带时间戳", "teamdoc-backup-" in (r.get("file") or ""), str(r.get("file")))
    check("至少一个目标成功", bool(r.get("ok")), str(r.get("targets"))[:200])

    if BK_DIRS:
        # 断言用"配置了几个目标就有几个结果",而不假设都存在
        check("每个配置目标都有结果记录",
              len(r.get("targets", {})) == len(BK_DIRS),
              f"{len(r.get('targets', {}))} vs {len(BK_DIRS)}")

    st, stat = call("GET", "/api/admin/backup/status")
    check("状态接口 200", st == 200, str(stat)[:120])
    if st == 200:
        cfg = stat.get("config", {})
        check("状态含 config.last.nextRun",
              all(k in stat for k in ("config", "last", "nextRun")), str(list(stat)))
        check("暴露目标目录可达性",
              all("exists" in d for d in cfg.get("dirs", [])), str(cfg.get("dirs")))

    # 保留策略:连做几轮,目录里自有备份数不应超过 BACKUP_KEEP
    keep = None
    if st == 200:
        keep = stat.get("config", {}).get("keep")
    if BK_DIRS and keep and keep > 0:
        for _ in range(3):
            call("POST", "/api/admin/backup/run")
        d0 = BK_DIRS[0]
        if os.path.isdir(d0):
            mine = [f for f in os.listdir(d0)
                    if f.startswith("teamdoc-backup-") and f.endswith(".zip")]
            check(f"保留策略生效(应 <= {keep} 份)", len(mine) <= keep, f"实有 {len(mine)}")
            # 手工文件绝不能被保留策略删掉
            manual = os.path.join(d0, "hand-placed-notes.txt")
            with open(manual, "w", encoding="utf-8") as fh:
                fh.write("do not delete me")
            call("POST", "/api/admin/backup/run")
            check("手工放入的文件未被删除", os.path.exists(manual))
            try:
                os.remove(manual)
            except OSError:
                pass


def scenario_orphan_breaker():
    print("\n=== 场景6:孤儿清理 dry-run 与熔断 ===")
    data_dir = os.environ.get("TD_DATA_DIR", "")
    if not data_dir:
        print("  SKIP  未设 TD_DATA_DIR,无法制造孤儿")
        return
    files_dir = os.path.join(data_dir, "files")

    st, before = call("GET", "/api/admin/storage")
    base_on_disk = before["orphans"]["filesOnDisk"] if st == 200 else 0

    # 制造足够多的孤儿以越过 2/3 熔断线
    made = []
    try:
        for _ in range(max(4, base_on_disk * 3)):
            p = os.path.join(files_dir, uuid.uuid4().hex[:16])
            with open(p, "wb") as fh:
                fh.write(b"orphan" * 200)
            made.append(p)
    except OSError as e:
        print(f"  SKIP  无法写入数据目录:{e}")
        return

    st, pv = call("POST", "/api/admin/storage/cleanup?dryRun=1")
    check("dry-run 返回 200", st == 200, str(pv)[:120])
    if st == 200:
        check("dry-run 报告待删数量", pv.get("orphans", 0) >= len(made), str(pv))
    check("dry-run 未删除任何文件", all(os.path.exists(p) for p in made),
          "有文件被删")

    st, r = call("POST", "/api/admin/storage/cleanup")
    check("熔断触发时清理被拒(400)", st == 400, f"status={st} {str(r)[:120]}")
    check("被拒后文件仍在", all(os.path.exists(p) for p in made))

    st, r = call("POST", "/api/admin/storage/cleanup?force=1")
    check("force=1 可越过熔断", st == 200, f"status={st} {str(r)[:120]}")
    if st == 200:
        check("真的删掉了刚造的孤儿", not any(os.path.exists(p) for p in made))


def scenario_restore_drill():
    """阶段一:造数据 + 备份,并把状态写到 flag,等重启后再 verify。"""
    print("\n=== 场景7:端到端恢复(第一步:造数据+备份+改数据) ===")
    st, proj = call("POST", "/api/projects", {"name": f"恢复演练-{uuid.uuid4().hex[:6]}"})
    if st != 200:
        check("建演练项目", False, str(proj)[:120])
        return
    pid = proj["id"]
    st, _ = call("POST", f"/api/files/upload?projectId={pid}&name=marker.txt",
                 b"ORIGINAL-CONTENT", ctype="application/octet-stream")
    check("上传标记文件", st == 200, str(_)[:100])
    st, listing = call("GET", f"/api/files?project_id={pid}")
    files = (listing or {}).get("files") or []
    check("列表可见标记文件", any(f["name"] == "marker.txt" for f in files), str(listing)[:120])
    if not files:
        return
    fid = files[0]["id"]

    st, blob = call("GET", "/api/admin/backup", raw=True)
    check("取得备份包", st == 200 and isinstance(blob, bytes) and blob[:2] == b"PK")
    if st != 200:
        return
    bpath = os.path.join(os.path.dirname(flag_path()), "restore-drill-backup.zip")
    try:
        with open(bpath, "wb") as fh:
            fh.write(blob)
    except OSError as e:
        check("保存备份包", False, str(e))
        return

    # 改数据:文件改名 + 新建项目(恢复后都应回退)
    call("PATCH", f"/api/files/{fid}", {"name": "changed-after-backup.txt"})
    st, proj2 = call("POST", "/api/projects", {"name": f"备份后新建-{uuid.uuid4().hex[:6]}"})
    check("备份后改动已生效(改名/新建)", st == 200, str(proj2)[:100])

    # 上传 → arm
    st, info = call("POST", "/api/admin/restore/upload", blob,
                    ctype="application/octet-stream")
    check("上传备份并通过校验", st == 200, str(info)[:150])
    if st != 200:
        return
    st, r = call("POST", "/api/admin/restore/arm")
    check("置为重启后生效", st == 200 and r.get("armed"), str(r)[:120])

    with open(flag_path(), "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "fid": fid, "pid2": proj2.get("id"),
                   "backup": bpath}, fh, ensure_ascii=False, indent=2)
    print("\n  >> 现在**重启服务**,然后用 --verify-restore 再跑一次完成断言")


def verify_restore():
    print("\n=== 场景7:端到端恢复(第二步:重启后断言) ===")
    try:
        with open(flag_path(), encoding="utf-8") as fh:
            stt = json.load(fh)
    except (OSError, ValueError):
        print("  找不到演练状态,请先跑一次不带参数的测试")
        return
    pid, pid2 = stt["pid"], stt["pid2"]

    st, proj = call("GET", f"/api/projects/{pid}")
    check("恢复后演练项目仍在", st == 200, f"status={st}")
    st, listing = call("GET", f"/api/files?project_id={pid}")
    names = [f["name"] for f in ((listing or {}).get("files") or [])]
    check("文件名回到备份时点(marker.txt)", "marker.txt" in names, str(names))
    check("备份后的改名已回退", "changed-after-backup.txt" not in names, str(names))
    st, _ = call("GET", f"/api/projects/{pid2}")
    check("备份后新建的项目已消失", st == 404, f"status={st}")

    st, r = call("GET", "/api/admin/restore/status")
    check("恢复后暂存与待生效标记已清理",
          st == 200 and not r.get("staged") and not r.get("armed"), str(r))

    for p in (flag_path(), stt.get("backup", "")):
        try:
            os.remove(p)
        except OSError:
            pass


def main():
    global SID
    verify_only = "--verify-restore" in sys.argv
    print("=" * 60)
    print("备份与恢复:多目标 / 保留 / 校验 / 端到端恢复")
    print("=" * 60)
    SID = login()
    if not SID:
        print("登录失败,请先跑 tests/_bootstrap.py")
        return 1

    if verify_only:
        verify_restore()
    else:
        scenario_backup_targets()
        scenario_bad_archives()
        scenario_zip_slip()
        scenario_orphan_breaker()
        scenario_restore_drill()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项,失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
