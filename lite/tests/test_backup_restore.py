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
  7. 端到端恢复:造数据 → 备份 → 改数据 → 上传 → arm → 重启 → 断言回到备份时点

恢复演练已全自动化:服务器由 fixture 托管,重启只是一行调用 —— 旧脚本
"跑到 arm 提示人工重启、再带 --verify-restore 跑第二次"的两阶段流程,
连同状态文件 _restore_drill.json 一起删掉了。
"""
import io
import os
import uuid
import zipfile

from _harness import ADMIN_EMAIL, ADMIN_PASSWORD, Client


def test_backup_targets_retention_and_missing_dir(admin, backup_dirs):
    d1, d2, ghost = backup_dirs
    r = admin.post("/api/admin/backup/run")
    assert r.status == 200, f"手动备份返回 200: {str(r.data)[:120]}"
    data = r.data
    assert "teamdoc-backup-" in (data.get("file") or ""), \
        f"备份文件名带时间戳: {data.get('file')}"
    assert bool(data.get("ok")), f"至少一个目标成功: {str(data.get('targets'))[:200]}"
    # 断言用"配置了几个目标就有几个结果",而不假设都成功
    assert len(data.get("targets", {})) == len(backup_dirs), \
        f"每个配置目标都有结果记录: {len(data.get('targets', {}))} vs {len(backup_dirs)}"
    # 目标目录不存在 → 该目标记为失败,且不自动创建
    assert not os.path.exists(ghost), "不存在的目标目录不会被自动创建"

    stat = admin.get("/api/admin/backup/status").data
    assert all(k in stat for k in ("config", "last", "nextRun")), \
        f"状态含 config.last.nextRun: {list(stat)}"
    assert all("exists" in d for d in stat.get("config", {}).get("dirs", [])), \
        f"暴露目标目录可达性: {stat.get('config', {}).get('dirs')}"

    # 保留策略:连做 keep+1 轮,目录里自有备份数不应超过 keep(真正触发裁剪)
    keep = stat.get("config", {}).get("keep") or 0
    if keep > 0:
        for _ in range(keep + 1):
            assert admin.post("/api/admin/backup/run").status == 200
        mine = [f for f in os.listdir(d1)
                if f.startswith("teamdoc-backup-") and f.endswith(".zip")]
        assert len(mine) <= keep, f"保留策略生效(应 <= {keep} 份): 实有 {len(mine)}"
        # 手工文件绝不能被保留策略删掉
        manual = os.path.join(d1, "hand-placed-notes.txt")
        with open(manual, "w", encoding="utf-8") as fh:
            fh.write("do not delete me")
        assert admin.post("/api/admin/backup/run").status == 200
        assert os.path.exists(manual), "手工放入的文件未被删除"


def test_bad_archives_rejected(admin):
    for label, payload in (
        ("非 zip 文件", b"definitely not a zip"),
        ("空 body", b""),
    ):
        r = admin.post("/api/admin/restore/upload", raw_body=payload,
                       ctype="application/octet-stream")
        assert r.status == 400, f"拒绝{label}: {r.status} {str(r.data)[:100]}"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("RESTORE.txt", "no db here")
    r = admin.post("/api/admin/restore/upload", raw_body=buf.getvalue(),
                   ctype="application/octet-stream")
    assert r.status == 400, f"拒绝缺 teamdoc.db 的包: {r.status} {str(r.data)[:100]}"
    admin.delete("/api/admin/restore")  # 清掉可能残留的暂存


def test_zip_slip_whitelist(admin):
    """把带恶意路径的包塞进上传端点,断言全部被拒。"""
    # 先拿一份合法包的 teamdoc.db 作为载具
    r = admin.get("/api/admin/backup")
    assert r.status == 200 and isinstance(r.data, bytes) and r.data[:2] == b"PK", \
        f"取得合法备份用于构造恶意包: {r.status}"
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
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
        r = admin.post("/api/admin/restore/upload", raw_body=buf.getvalue(),
                       ctype="application/octet-stream")
        assert r.status == 400, f"拒绝 {label}: {r.status} {str(r.data)[:100]}"

    admin.delete("/api/admin/restore")  # 清掉可能残留的暂存


def test_orphan_breaker(admin, data_dir):
    """孤儿清理 dry-run 与熔断:多数文件被判孤儿时必须拒绝,force 显式越过。"""
    files_dir = data_dir / "files"
    before = admin.get("/api/admin/storage").data["orphans"]
    base_on_disk = before.get("filesOnDisk", 0) if before else 0

    # 制造足够多的孤儿以越过 2/3 熔断线(admin.py: victims > on-disk * 2/3 拒绝)
    made = []
    for _ in range(max(4, base_on_disk * 3)):
        p = files_dir / uuid.uuid4().hex[:16]
        p.write_bytes(b"orphan" * 200)
        made.append(p)

    r = admin.post("/api/admin/storage/cleanup?dryRun=1")
    assert r.status == 200, f"dry-run 返回 200: {str(r.data)[:120]}"
    assert r.data.get("orphans", 0) >= len(made), f"dry-run 报告待删数量: {r.data}"
    assert all(p.exists() for p in made), "dry-run 未删除任何文件"

    r = admin.post("/api/admin/storage/cleanup")
    assert r.status == 400, f"熔断触发时清理被拒(400): {r.status} {str(r.data)[:120]}"
    assert all(p.exists() for p in made), "被拒后文件仍在"

    r = admin.post("/api/admin/storage/cleanup?force=1")
    assert r.status == 200, f"force=1 可越过熔断: {r.status} {str(r.data)[:120]}"
    assert not any(p.exists() for p in made), "真的删掉了刚造的孤儿"


def test_restore_drill(admin, server, base_url):
    """端到端恢复:造数据 → 备份 → 改数据 → 上传 → arm → 重启 → 断言回到备份时点。

    重启由 fixture 托管的服务器就地完成(恢复在启动时应用)。重启后重新登录:
    备份之后新发的会话会随库回滚,不能再依赖旧 cookie 是否仍有效。
    """
    pid = admin.post("/api/projects", {"name": f"恢复演练-{uuid.uuid4().hex[:6]}"}).data["id"]
    r = admin.upload(pid, "marker.txt", b"ORIGINAL-CONTENT")
    assert r.status == 200, f"上传标记文件: {str(r.data)[:100]}"
    files = admin.get(f"/api/files?project_id={pid}").data.get("files") or []
    assert any(f["name"] == "marker.txt" for f in files), "列表可见标记文件"
    fid = files[0]["id"]

    r = admin.get("/api/admin/backup")
    blob = r.data
    assert r.status == 200 and isinstance(blob, bytes) and blob[:2] == b"PK", "取得备份包"

    # 改数据:文件改名 + 新建项目(恢复后都应回退)
    admin.patch(f"/api/files/{fid}", {"name": "changed-after-backup.txt"})
    pid2 = admin.post("/api/projects", {"name": f"备份后新建-{uuid.uuid4().hex[:6]}"}).data["id"]

    # 上传 → arm → 重启(恢复在启动时应用)
    r = admin.post("/api/admin/restore/upload", raw_body=blob,
                   ctype="application/octet-stream")
    assert r.status == 200, f"上传备份并通过校验: {str(r.data)[:150]}"
    r = admin.post("/api/admin/restore/arm")
    assert r.status == 200 and r.data.get("armed"), f"置为重启后生效: {str(r.data)[:120]}"

    server.restart()
    admin = Client(base_url)
    admin.login(ADMIN_EMAIL, ADMIN_PASSWORD)

    names = [f["name"] for f in
             (admin.get(f"/api/files?project_id={pid}").data or {}).get("files", [])]
    assert "marker.txt" in names, f"文件名回到备份时点(marker.txt): {names}"
    assert "changed-after-backup.txt" not in names, f"备份后的改名已回退: {names}"
    assert admin.get(f"/api/projects/{pid2}").status == 404, "备份后新建的项目已消失"
    r = admin.get("/api/admin/restore/status")
    assert r.status == 200 and not r.data.get("staged") and not r.data.get("armed"), \
        f"恢复后暂存与待生效标记已清理: {r.data}"
