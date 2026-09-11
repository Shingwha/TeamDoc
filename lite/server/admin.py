"""管理后台:存储统计、孤儿文件清理、备份导出(仅全局管理员)。

为什么单独一个模块:存储与备份是运维视角的功能,与 auth 的认证/用户管理不属于
同一关注点;files.py 是面向普通成员的项目云空间。这里只放 is_admin 才能碰的东西。

备份/恢复的实现细节都在 backup.py —— 这里只保留 HTTP 入口,免得"存储统计"与
"备份运维"两件事在一个文件里互相干扰。
"""
import os
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

import backup
from auth import AuthContext, err, require_admin, require_admin_write
from files import MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, STORAGE_RESERVE_MB
from models import FILES_DIR, Doc, DocVersion, File, Folder, Project, get_db

router = APIRouter()


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
def cleanup_orphans(dryRun: bool = False, force: bool = False,
                    ctx: AuthContext = Depends(require_admin_write),
                    db: DbSession = Depends(get_db)):
    """清理无主物理文件(磁盘上有、DB 里无引用)。

    只删 orphans,不碰 missing —— 后者是 DB 记录还在但文件丢了,
    需要的是从备份恢复内容,删记录只会把问题藏起来。

    两道防护(此前都没有):
    - `dryRun=1`:只报告将删除什么,一个文件都不碰。界面上先预览再确认。
    - **熔断**:孤儿数超过磁盘文件总数的 2/3 时拒绝执行,除非显式 `force=1`。
      防的是这个真实场景:库被换成空的/旧的、或指向了错的数据目录 ——
      此时磁盘上每个文件都会被判为孤儿,一键下去数据全没了。
      正常情况孤儿只是少数残留,出现"绝大多数都是孤儿"本身就是危险信号。
    """
    known = {Path(p).name for (p,) in db.query(File.storage_path).all()}
    victims = []
    on_disk = 0
    for entry in os.scandir(FILES_DIR):
        if not entry.is_file(follow_symlinks=False):
            continue
        on_disk += 1
        if entry.name in known:
            continue
        try:
            victims.append((entry.path, entry.stat().st_size))
        except OSError:
            pass

    # 熔断判定:磁盘上明明有文件,却几乎全被判为孤儿 —— 更像是库不对,而不是垃圾多
    tripped = on_disk > 0 and len(victims) > on_disk * 2 / 3
    preview = {
        "dryRun": True,
        "orphans": len(victims),
        "orphanBytes": sum(s for _, s in victims),
        "filesOnDisk": on_disk,
        "breakerTripped": tripped,
        "blocked": tripped and not force,
    }
    if dryRun:
        return preview
    if preview["blocked"]:
        err(400, "VALIDATION",
            f"检测到 {len(victims)}/{on_disk} 个文件都被判为无主(超过 2/3),已拒绝清理。"
            "这通常意味着数据库为空、指向了错的数据目录,或恢复了一份旧备份 —— "
            "此时清理会删光磁盘上的文件。请先确认数据目录与数据库是否匹配;"
            "确实要清理请显式传 force=1。")

    removed, freed, failed = 0, 0, 0
    for path, size in victims:
        try:
            os.unlink(path)
            removed += 1
            freed += size
        except OSError:
            failed += 1
    return {"ok": True, "removed": removed, "freedBytes": freed, "failed": failed,
            "orphans": len(victims), "filesOnDisk": on_disk}


@router.get("/api/admin/backup")
def download_backup(ctx: AuthContext = Depends(require_admin)):
    """整站备份:数据库快照 + 全部物理文件,打包为 zip 流式下载。

    与云空间的打包下载同构:先落到数据目录内的暂存文件,再流式回吐并带
    Content-Length —— 否则浏览器无法显示下载进度,大包期间像是卡死。

    归档用 backup.build_archive() 生成,因此下载到的包与定时备份、手动备份
    是**同一套产物**:带时间戳、同样保证一致性。这也顺带修掉了"文件名写死
    teamdoc-backup.zip,多次下载互相覆盖"的老问题 —— 命名由服务端决定,
    前端不再用 a.download 覆盖它。

    持 BACKUP_LOCK:build_archive 写固定命名的暂存文件,与定时备份并发会互相踩。
    锁只覆盖"生成"这一段;流式回吐在锁外进行,否则一个大包的下载会长时间卡住定时备份。
    """
    if not backup.BACKUP_LOCK.acquire(timeout=300):
        err(409, "CONFLICT", "另一轮备份正在进行,请稍后再试")
    try:
        archive, size, _stats = backup.build_archive()
    finally:
        backup.BACKUP_LOCK.release()

    def gen():
        try:
            with open(archive, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            # 暂存副本用完即删:它只是一次下载的载体,不是备份目标
            archive.unlink(missing_ok=True)

    cd = "attachment; filename*=UTF-8''" + quote(archive.name)
    return StreamingResponse(gen(), media_type="application/zip",
                             headers={"Content-Disposition": cd,
                                      "Content-Length": str(size)})


@router.get("/api/admin/backup/status")
def backup_status(ctx: AuthContext = Depends(require_admin)):
    """备份现状:配置的目标目录、上次结果、下次计划时间、各目标是否可达。"""
    return backup.status()


@router.post("/api/admin/backup/run")
def backup_run_now(ctx: AuthContext = Depends(require_admin_write)):
    """立即执行一轮备份并返回各目标结果(不等定时)。"""
    return backup.run_backup(trigger="manual")


# ---------- 恢复 ----------
# 上传 → 校验 → "重启后生效"。不在请求里直接换库:替换运行中的 SQLite 库
# (及其 -wal/-shm)是危险动作。替换发生在下次启动、任何连接建立之前。

@router.get("/api/admin/restore/status")
def restore_status(ctx: AuthContext = Depends(require_admin)):
    return backup.restore_status()


@router.post("/api/admin/restore/upload")
async def restore_upload(request: Request, ctx: AuthContext = Depends(require_admin_write)):
    """上传一份备份 zip(raw body 流式落盘),校验后暂存。

    流式而非 request.body():备份可能上 GB,整体读进内存会直接吃掉服务端内存。
    落盘到数据目录内的暂存区,不占系统盘。
    """
    st = backup.restore_status()
    if st.get("armed"):
        # 已有"待生效"的恢复未被应用就再传一份,会让管理员搞不清重启后到底哪个生效
        err(409, "CONFLICT", "已有一份待重启生效的备份。请先取消它,或重启服务使其生效后再上传")

    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared and declared > MAX_UPLOAD_BYTES:
        err(400, "VALIDATION", f"备份文件过大(上限 {MAX_UPLOAD_MB}MB)")

    backup.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    part = backup.STAGING_UPLOAD.with_name("restore-upload.zip.part")
    total, too_large = 0, False
    try:
        with open(part, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    too_large = True
                    break
                out.write(chunk)
    except Exception:
        part.unlink(missing_ok=True)
        err(400, "VALIDATION", "上传中断,请重试")
    if too_large:
        part.unlink(missing_ok=True)
        err(400, "VALIDATION", f"备份文件过大(上限 {MAX_UPLOAD_MB}MB)")
    if total == 0:
        part.unlink(missing_ok=True)
        err(400, "VALIDATION", "请求体为空,未收到备份内容")

    # 校验(白名单 + 版本一致性 + 可读性)在 stage_upload 内完成,失败即拒绝并删除
    info, msg = backup.stage_upload(part)
    if not info:
        err(400, "VALIDATION", f"备份校验未通过:{msg}")
    return {"ok": True, "info": info}


@router.post("/api/admin/restore/arm")
def restore_arm(ctx: AuthContext = Depends(require_admin_write)):
    """标记暂存的备份在下次重启时生效。"""
    ok, msg = backup.arm_restore()
    if not ok:
        err(400, "VALIDATION", msg)
    return {"ok": True, "armed": True}


@router.delete("/api/admin/restore")
def restore_cancel(ctx: AuthContext = Depends(require_admin_write)):
    """丢弃暂存的备份 / 取消待生效的恢复。"""
    backup.clear_restore()
    return {"ok": True}
