"""管理后台:存储统计、孤儿文件清理、备份导出(仅全局管理员)。

为什么单独一个模块:存储与备份是运维视角的功能,与 auth 的认证/用户管理不属于
同一关注点;files.py 是面向普通成员的项目云空间。这里只放 is_admin 才能碰的东西。
"""
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from auth import AuthContext, require_admin
from files import MAX_UPLOAD_MB, STORAGE_RESERVE_MB
from models import DB_PATH, FILES_DIR, Doc, DocVersion, File, Folder, Project, get_db

router = APIRouter()

RESTORE_README = """TeamDoc Lite 备份
==================

本压缩包包含:
  teamdoc.db   数据库(用 SQLite VACUUM INTO 生成的一致性快照)
  files/       云空间的全部物理文件(文件名即数据库 files 表的 storage_path 基名)
  RESTORE.txt  本说明

恢复步骤
--------

1. 停掉正在运行的 TeamDoc 服务(务必先停,否则数据库可能被写坏)。

2. 清空/改名原数据目录(默认 lite/server/data/,可用环境变量 TEAMDOC_DATA_DIR 指定)。
   建议先把它改名保留,确认恢复成功后再删除:
       mv data data.old

3. 新建数据目录,把本压缩包解开到其中,使目录结构为:
       data/teamdoc.db
       data/files/<一堆无扩展名的文件>

4. 启动服务。服务启动时会自动执行数据库迁移,把结构补齐到当前版本。

注意
----

* 直接复制 teamdoc.db 是**不可靠**的:数据库以 WAL 模式运行,尚未 checkpoint 的
  写入还在 teamdoc.db-wal 里,单独复制 .db 会拿到旧快照。本备份用 VACUUM INTO
  生成,内容完整,不需要 -wal / -shm 文件。
* 备份期间服务可以继续运行(生成的是某个时刻的一致快照),但恢复必须停机。
* 备份不含会话与 PAT 的明文(PAT 只存 sha256),恢复后用户需重新登录。
"""


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _orphan_scan(db: DbSession) -> dict:
    """比对磁盘上的物理文件与 DB 记录。

    - orphans:磁盘上有、DB 里没有引用的文件 —— 上传中途失败或删项目时留下的,
      占着空间却在界面上永远看不见,故提供清理入口。
    - missing:DB 有记录但磁盘文件不见了(被手动删过/磁盘故障),只报告不处理。
    """
    known = {Path(p).name for (p,) in db.query(File.storage_path).all()}
    on_disk = {}
    for entry in os.scandir(FILES_DIR):
        if entry.is_file(follow_symlinks=False):
            on_disk[entry.name] = entry.stat().st_size
    orphans = {n: s for n, s in on_disk.items() if n not in known}
    missing = known - set(on_disk)
    return {
        "orphans": len(orphans), "orphanBytes": sum(orphans.values()),
        "missing": len(missing),
        "filesOnDisk": len(on_disk), "filesOnDiskBytes": sum(on_disk.values()),
    }


@router.get("/api/admin/storage")
def storage_overview(ctx: AuthContext = Depends(require_admin),
                     db: DbSession = Depends(get_db)):
    """存储总览:实例占用 / 分项目占用 / 磁盘余量 / 孤儿文件。

    占用按**项目**统计而非按人:文件可在项目间移动且不改 created_by,
    按人统计会随移动漂移,无法作为配额依据(本项目也不设硬配额)。
    回收站占用单列:回收站里的文件仍占物理磁盘,混在一起会出现
    "删了文件占用没变"的困惑。
    """
    # 活跃与回收站分列(同一张表按 deleted_at 拆两段)
    proj_rows = db.query(
        File.project_id,
        func.sum(File.size),
        func.count(File.id),
    ).filter(File.deleted_at.is_(None)).group_by(File.project_id).all()
    trash_rows = db.query(
        File.project_id,
        func.sum(File.size),
        func.count(File.id),
    ).filter(File.deleted_at.isnot(None)).group_by(File.project_id).all()
    names = {p.id: p.name for p in db.query(Project.id, Project.name).all()}
    trash_by = {pid: (size or 0, cnt) for pid, size, cnt in trash_rows}

    projects = []
    for pid, size, cnt in proj_rows:
        t_size, t_cnt = trash_by.get(pid, (0, 0))
        projects.append({
            "projectId": pid, "name": names.get(pid, "(已删除项目)"),
            "bytes": size or 0, "fileCount": cnt,
            "trashBytes": t_size, "trashFileCount": t_cnt,
        })
    # 只有回收站内容的项目也要出现(否则那部分占用无处可查)
    for pid, (t_size, t_cnt) in trash_by.items():
        if not any(p["projectId"] == pid for p in projects):
            projects.append({
                "projectId": pid, "name": names.get(pid, "(已删除项目)"),
                "bytes": 0, "fileCount": 0,
                "trashBytes": t_size, "trashFileCount": t_cnt,
            })
    projects.sort(key=lambda p: p["bytes"] + p["trashBytes"], reverse=True)

    active_bytes = sum(p["bytes"] for p in projects)
    active_files = sum(p["fileCount"] for p in projects)
    trash_bytes = sum(p["trashBytes"] for p in projects)
    trash_files = sum(p["trashFileCount"] for p in projects)

    # 文档与历史版本(版本按年龄分层保留,稳态约 124 条/文档,粘过大表格的文档可观)
    doc_bytes = db.query(func.sum(func.length(Doc.content))) \
        .filter(Doc.deleted_at.is_(None)).scalar() or 0
    ver_bytes = db.query(func.sum(func.length(DocVersion.content))).scalar() or 0
    doc_count = db.query(Doc).filter(Doc.deleted_at.is_(None)).count()
    ver_count = db.query(DocVersion).count()
    folder_count = db.query(Folder).filter(Folder.deleted_at.is_(None)).count()

    disk = shutil.disk_usage(str(FILES_DIR))
    return {
        "files": {"activeBytes": active_bytes, "activeCount": active_files,
                  "trashBytes": trash_bytes, "trashCount": trash_files,
                  "folderCount": folder_count},
        "docs": {"bytes": doc_bytes, "count": doc_count,
                 "versionBytes": ver_bytes, "versionCount": ver_count},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free,
                 "dataDirBytes": _dir_size(FILES_DIR)},
        # 生效中的限制,只读暴露出**服务端实际用的值**。
        # 为什么要暴露:这些值原先只存在于环境变量,用户要传个 20GB 的包被拒了
        # 才知道有上限,管理员也无处可查。界面不该替用户记住部署参数。
        "limits": {"maxUploadMb": MAX_UPLOAD_MB, "storageReserveMb": STORAGE_RESERVE_MB},
        "projects": projects,
        "orphans": _orphan_scan(db),
    }


@router.post("/api/admin/storage/cleanup")
def cleanup_orphans(ctx: AuthContext = Depends(require_admin),
                    db: DbSession = Depends(get_db)):
    """清理无主物理文件(磁盘上有、DB 里无引用)。

    只删 orphans,不碰 missing —— 后者是 DB 记录还在但文件丢了,
    需要的是从备份恢复内容,删记录只会把问题藏起来。
    """
    known = {Path(p).name for (p,) in db.query(File.storage_path).all()}
    removed, freed, failed = 0, 0, 0
    for entry in os.scandir(FILES_DIR):
        if not entry.is_file(follow_symlinks=False) or entry.name in known:
            continue
        size = entry.stat().st_size
        try:
            os.unlink(entry.path)
            removed += 1
            freed += size
        except OSError:
            failed += 1
    return {"ok": True, "removed": removed, "freedBytes": freed, "failed": failed}


def _make_db_snapshot(target: Path):
    """用 VACUUM INTO 生成一致性快照。

    不能直接复制 teamdoc.db:WAL 模式下未 checkpoint 的写入还在 -wal 文件里,
    单独复制 .db 会拿到旧快照(恢复后表现为"最近的数据不见了")。
    VACUUM INTO 产出的是单文件、已整理、内容完整的副本,且不需要停机。
    """
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


@router.get("/api/admin/backup")
def download_backup(ctx: AuthContext = Depends(require_admin)):
    """整站备份:数据库快照 + 全部物理文件,打包为 zip 流式下载。

    与云空间的打包下载同构:先落到 SpooledTemporaryFile,再流式回吐并带
    Content-Length —— 否则浏览器无法显示下载进度,大包期间像是卡死。
    """
    spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "teamdoc.db"
        _make_db_snapshot(snap)
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(str(snap), arcname="teamdoc.db")
            z.writestr("RESTORE.txt", RESTORE_README)
            for entry in os.scandir(FILES_DIR):
                if entry.is_file(follow_symlinks=False):
                    z.write(entry.path, arcname=f"files/{entry.name}")
    spool.seek(0, 2)
    total = spool.tell()
    spool.seek(0)

    def gen():
        try:
            while True:
                chunk = spool.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            spool.close()

    cd = "attachment; filename*=UTF-8''" + quote("teamdoc-backup.zip")
    return StreamingResponse(gen(), media_type="application/zip",
                             headers={"Content-Disposition": cd,
                                      "Content-Length": str(total)})
