"""管理后台:存储统计 / 孤儿文件清理 / 备份导出。

为什么需要这些:
  - 磁盘会漏:上传中途失败、删项目只删记录,都会留下无主文件永久占盘,
    而界面里既看不到也删不掉 —— 用半年后"盘满了但文件加起来远小于占用"。
  - 备份必须用 VACUUM INTO:WAL 模式下直接复制 .db 拿到的是旧快照,
    用它恢复会表现为"最近的数据不见了"。

验证点:
  1. /api/admin/storage 返回磁盘/文件/文档/孤儿各段,回收站占用与活跃占用分列
  2. 非管理员访问三个端点一律 403
  3. 孤儿文件(磁盘有、DB 无)被识别并可清理,且不误删正常文件
  4. 删除项目会清掉该项目的物理文件(以前明确不清理)
  5. 回收站的文件仍占物理磁盘,但转入 trashBytes 统计(不混入活跃占用)
  6. 备份 zip 含 teamdoc.db + files/ + RESTORE.txt;库快照表与数据完整、无损坏
"""
import io
import sqlite3
import tempfile
import uuid
import zipfile

from _harness import Client, rand_email


def test_storage_overview_and_403(admin, base_url):
    # === 存储统计接口 ===
    r = admin.get("/api/admin/storage")
    assert r.status == 200, f"存储接口 200: {str(r.data)[:120]}"
    s = r.data
    assert isinstance(s, dict) and all(k in s for k in ("files", "docs", "disk", "orphans")), \
        f"含 files/docs/disk/orphans 各段: {list(s)}"
    assert s["disk"]["free"] > 0, f"disk.free > 0: {s['disk']}"
    assert "activeBytes" in s["files"] and "trashBytes" in s["files"], \
        f"活跃占用与回收站占用分列: {s['files']}"

    # === 非管理员一律 403 ===
    plain_email = rand_email("plain")
    admin.post("/api/users", {"email": plain_email, "name": "普通", "password": "plain12345"})
    plain = Client(base_url)
    plain.login(plain_email, "plain12345")
    for label, method, path in [("存储统计", "GET", "/api/admin/storage"),
                                ("备份下载", "GET", "/api/admin/backup"),
                                ("孤儿清理", "POST", "/api/admin/storage/cleanup")]:
        r = plain.request(method, path)
        assert r.status == 403, f"普通用户{label} → 403: {r.status}"


def test_orphan_cleanup_and_project_delete(admin, data_dir):
    files_dir = data_dir / "files"

    # === 孤儿文件识别与清理 ===
    # 数据目录由 fixture 一次性创建,清理后孤儿数应归零 —— 这个绝对断言现在是安全的
    # (旧脚本跑在可复用的数据目录上,只能断言差值;根因已随一次性目录消除)
    pid = admin.post("/api/projects", {"name": "存储测试项目", "description": "s"}).data["id"]
    phys_before = set(files_dir.iterdir())
    r = admin.upload(pid, "keep.txt", b"keep me")
    assert r.status == 200, f"上传正常文件: {r.data}"
    keep_phys = set(files_dir.iterdir()) - phys_before
    assert len(keep_phys) == 1, f"恰好落盘一个物理文件: {keep_phys}"
    base = admin.get("/api/admin/storage").data["orphans"]
    orphan_path = files_dir / uuid.uuid4().hex[:16]
    orphan_path.write_bytes(b"x" * 4096)
    mid = admin.get("/api/admin/storage").data["orphans"]
    assert mid["orphans"] - base["orphans"] == 1, \
        f"识别出新增孤儿(差值 1): base={base['orphans']} mid={mid['orphans']}"
    assert mid["orphanBytes"] - base["orphanBytes"] >= 4096, \
        f"孤儿字节数增加: base={base['orphanBytes']} mid={mid['orphanBytes']}"
    r = admin.post("/api/admin/storage/cleanup")
    assert r.status == 200, f"清理接口 200: {r.data}"
    assert isinstance(r.data, dict) and r.data.get("removed", 0) >= 1, \
        f"至少清掉刚造的那个: {r.data}"
    assert not orphan_path.exists(), "孤儿已从磁盘删除"
    assert bool(keep_phys & set(files_dir.iterdir())), "正常文件未被误删"
    assert admin.get("/api/admin/storage").data["orphans"]["orphans"] == 0, "清理后孤儿归零"

    # === 删除项目会清掉物理文件 ===
    gone_before = set(files_dir.iterdir())
    assert admin.upload(pid, "gone.bin", b"z" * 2048).status == 200
    gone_phys = set(files_dir.iterdir()) - gone_before
    assert len(gone_phys) == 1, f"文件已落盘: {gone_phys}"
    r = admin.delete(f"/api/projects/{pid}")
    assert r.status == 200, f"项目删除 200: {r.status}"
    # 该项目此刻有 keep.txt 与 gone.bin 两个文件,应全部清除
    assert isinstance(r.data, dict) and r.data.get("removedFiles") == 2, \
        f"返回清理文件数=项目内文件数: {r.data}"
    now = set(files_dir.iterdir())
    assert not (gone_phys & now), "物理文件已一并清除"
    assert not (keep_phys & now), "另一个文件(keep.txt)也已清除"


def test_trash_bytes_admin_totals(admin, data_dir):
    """回收站占用单列(管理端总量口径)。项目级 /storage 契约在 test_files_paging。"""
    files_dir = data_dir / "files"
    base_files = admin.get("/api/admin/storage").data["files"]
    pid2 = admin.post("/api/projects",
                      {"name": "回收站占用测试-" + uuid.uuid4().hex[:6],
                       "description": "s"}).data["id"]
    big_before = set(files_dir.iterdir())
    big = admin.upload(pid2, "trashme.bin", b"q" * 8192).data
    big_phys = set(files_dir.iterdir()) - big_before
    after_up = admin.get("/api/admin/storage").data["files"]
    assert after_up["activeBytes"] - base_files["activeBytes"] >= 8192, \
        f"活跃占用随上传增加: base={base_files['activeBytes']} up={after_up['activeBytes']}"
    admin.delete(f"/api/files/{big['id']}")
    after_del = admin.get("/api/admin/storage").data["files"]
    assert after_del["trashBytes"] - base_files["trashBytes"] >= 8192, \
        f"删除后计入回收站占用: base={base_files['trashBytes']} del={after_del['trashBytes']}"
    assert after_up["activeBytes"] - after_del["activeBytes"] >= 8192, \
        f"活跃占用同步回落: up={after_up['activeBytes']} del={after_del['activeBytes']}"
    assert bool(big_phys & set(files_dir.iterdir())), "回收站文件仍占磁盘(故必须单列)"


def test_backup_zip_integrity(admin):
    r = admin.get("/api/admin/backup")
    assert r.status == 200, f"备份接口 200: {r.status}"
    blob = r.data
    assert isinstance(blob, bytes) and blob[:2] == b"PK", f"备份返回 zip 字节流: type={type(blob)}"
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = z.namelist()
    assert "teamdoc.db" in names, f"含 teamdoc.db: {names[:8]}"
    assert "RESTORE.txt" in names, f"含 RESTORE.txt: {names[:8]}"
    assert any(n.startswith("files/") for n in names), \
        f"含 files/ 物理文件: {[n for n in names if n.startswith('files/')][:5]}"
    assert z.testzip() is None, "zip 无损坏"
    with tempfile.TemporaryDirectory() as td:
        dbp = f"{td}/teamdoc.db"
        with open(dbp, "wb") as fh:
            fh.write(z.read("teamdoc.db"))
        c = sqlite3.connect(dbp)
        tables = sorted(r[0] for r in c.execute(
            "select name from sqlite_master where type='table'"))
        usercnt = c.execute("select count(*) from users").fetchone()[0]
        projcnt = c.execute("select count(*) from projects").fetchone()[0]
        filecnt = c.execute("select count(*) from files").fetchone()[0]
        c.close()
    # 备份端点内部用 VACUUM INTO,快照必须含全部业务表(缺表意味着备份不可用)
    assert set(tables) >= {"users", "projects", "docs", "files", "folders",
                           "doc_versions", "project_members", "pats", "sessions"}, str(tables)
    assert usercnt >= 2, f"快照含用户数据: users={usercnt}"
    assert projcnt >= 1, f"快照含项目数据: projects={projcnt}"
    # 关键:回收站里的文件记录也在快照里(WAL 未 checkpoint 的写入不能丢)
    assert filecnt >= 1, f"快照含最新写入(回收站文件记录): files={filecnt}"
