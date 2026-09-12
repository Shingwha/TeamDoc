"""管理后台:存储统计、孤儿文件清理、备份导出(仅全局管理员)。

为什么单独一个模块:存储与备份是运维视角的功能,与 auth 的认证/用户管理不属于
同一关注点;files.py 是面向普通成员的项目云空间。这里只放 is_admin 才能碰的东西。

备份/恢复的实现细节都在 backup.py —— 这里只保留 HTTP 入口,免得"存储统计"与
"备份运维"两件事在一个文件里互相干扰。
"""
import hashlib
import os
import shutil
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

import backup
import throttle
import watchdog
import ws as ws_mod
from auth import (LOGIN_ACCOUNT_POLICY, ONLINE_WINDOW_SECONDS, AuthContext,
                  avatar_color, describe_ua, err, pat_write_guard, require_admin)
from files import (MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, STORAGE_RESERVE_MB,
                   chunked_file, save_request_body)
from models import (DB_PATH, FILES_DIR, INFLIGHT_STORAGE, AuthSession, Doc,
                    DocVersion, File, Folder, LoginEvent, Project, ProjectMember,
                    User, engine, get_db, release_db, utcnow)
from projects import batch_stats

router = APIRouter(dependencies=[Depends(pat_write_guard)])


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


def _scan_orphans(db: DbSession) -> dict:
    """比对磁盘物理文件与 DB 记录(存储总览与孤儿清理共用同一份结果,不再各扫一遍)。

    - orphans:磁盘上有、DB 里没有引用(且不在传)的文件 —— 上传中途失败或删项目时
      留下的,占着空间却在界面上永远看不见,故提供清理入口。
    - missing:DB 有记录但磁盘文件不见了(被手动删过/磁盘故障),只报告不处理。
    - inflightSkipped:正在上传的文件(先落盘后登记,窗口期内必然不在 DB 里)。
    """
    known = {Path(p).name for (p,) in db.query(File.storage_path).all()}
    victims = []          # (绝对路径, 大小):真正的孤儿
    on_disk_names = set()
    inflight = 0
    for entry in os.scandir(FILES_DIR):
        if not entry.is_file(follow_symlinks=False):
            continue
        on_disk_names.add(entry.name)
        if entry.name in known:
            continue
        # 在传文件:先落盘后登记,此刻它必然不在 known 里。删了就是"上传成功但
        # 文件没了"(永久 404),所以必须跳过(见 models.INFLIGHT_STORAGE)
        if entry.name in INFLIGHT_STORAGE:
            inflight += 1
            continue
        try:
            victims.append((entry.path, entry.stat().st_size))
        except OSError:
            pass
    # 熔断判定:磁盘上明明有文件,却几乎全被判为孤儿 —— 更像是库不对,而不是垃圾多
    tripped = len(on_disk_names) > 0 and len(victims) > len(on_disk_names) * 2 / 3
    return {"victims": victims,
            "onDisk": len(on_disk_names),
            "onDiskBytes": sum(s for _, s in victims),  # 仅孤儿部分,总览另有 _dir_size
            "missing": len(known - on_disk_names),
            "inflight": inflight,
            "breakerTripped": tripped}


@router.get("/api/admin/storage")
def storage_overview(ctx: AuthContext = Depends(require_admin),
                     db: DbSession = Depends(get_db)):
    """存储总览:实例占用总量(活跃/回收站分列)/ 磁盘余量 / 孤儿文件 / 生效中的限制。

    分项目占用在管理后台「项目」区(每行 storageBytes,仅协作项目);
    个人空间占用不设管理员视图(§4.2:个人空间对管理员保密,占用亦然)。
    回收站占用单列:回收站里的文件仍占物理磁盘,混在一起会出现
    "删了文件占用没变"的困惑。
    """
    # 总量口径:活跃与回收站分列(同一张表按 deleted_at 拆两段)
    active = db.query(func.sum(File.size), func.count(File.id)) \
        .filter(File.deleted_at.is_(None)).one()
    trash = db.query(func.sum(File.size), func.count(File.id)) \
        .filter(File.deleted_at.isnot(None)).one()
    active_bytes = int(active[0] or 0)
    active_files = int(active[1] or 0)
    trash_bytes = int(trash[0] or 0)
    trash_files = int(trash[1] or 0)

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
        "orphans": _orphan_report(_scan_orphans(db)),
    }


def _orphan_report(scan: dict) -> dict:
    """_scan_orphans 的总览口径(计数与字节数)。"""
    return {"orphans": len(scan["victims"]),
            "orphanBytes": sum(s for _, s in scan["victims"]),
            "missing": scan["missing"],
            "filesOnDisk": scan["onDisk"],
            "filesOnDiskBytes": _dir_size(FILES_DIR),
            "inflightSkipped": scan["inflight"]}


@router.get("/api/admin/diagnostics")
def diagnostics(ctx: AuthContext = Depends(require_admin),
                db: DbSession = Depends(get_db)):
    """只读诊断快照:进程 / 连接池 / 线程 / WS / 库文件尺寸。

    故障时先看这里,再翻 logs/ 里的 stall-*.txt(事件循环停滞时 watchdog 转储的
    线程栈)。不做任何磁盘扫描。
    """

    def _size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return -1

    return {
        "uptimeSeconds": round(watchdog.uptime_seconds(), 1),
        "pid": os.getpid(),
        "pool": engine.pool.status(),
        "dbFiles": {"db": _size(DB_PATH),
                    "wal": _size(Path(str(DB_PATH) + "-wal")),
                    "shm": _size(Path(str(DB_PATH) + "-shm"))},
        "threads": {"count": threading.active_count(),
                    "names": sorted(t.name for t in threading.enumerate())},
        "inflightUploads": len(INFLIGHT_STORAGE),
        # WS 数量反映"标签页堆积 / 重连风暴"
        "ws": {"perDoc": {str(k): len(v) for k, v in ws_mod.POOL.items()},
               "total": sum(len(v) for v in ws_mod.POOL.values())},
        # 会话过期行没有后台清理,长期只增不减(量级很小,但值得看)
        "sessions": db.query(AuthSession).count(),
        # 登录侧:还在冷却中的节流键、审计表行数(有保留期,但攻击会把它刷大)
        "loginLocks": throttle.active_count(db),
        "loginEvents": db.query(LoginEvent).count(),
    }


# ---------- 登录状态与审计(管理后台:用户表列 / 登录详情抽屉 / 登录动态区) ----------

def _session_json(sess: AuthSession, *, current_token: str | None = None) -> dict:
    """一条会话的管理视图。

    **不返回真实 token**:对外标识是 ref = sha256(token) —— 能指认某条会话(从而强制
    下线),但不能反推成凭据。管理页面会被截图、进浏览器历史、写进工单,凭据不该出现在那里。
    在线判定用与用户列表同一个窗口(auth.ONLINE_WINDOW_SECONDS)。
    """
    label, kind = describe_ua(sess.user_agent or "")
    seen = sess.last_seen_at or sess.created_at
    online = (utcnow() - seen).total_seconds() <= ONLINE_WINDOW_SECONDS
    return {
        "ref": hashlib.sha256(sess.token.encode("utf-8")).hexdigest(),
        "ip": sess.ip,
        "device": label,
        "deviceKind": kind,
        "userAgent": sess.user_agent,
        "createdAt": sess.created_at.isoformat(),
        "lastSeenAt": sess.last_seen_at.isoformat() if sess.last_seen_at else None,
        "expiresAt": sess.expires_at.isoformat(),
        "online": online,
        "current": bool(current_token and sess.token == current_token),
    }


def _event_json(e: LoginEvent, user_name: str | None = None) -> dict:
    """一条登录记录。字段面固定(userName 未知时为 null):缺键会让前端与脚本
    在"账号不存在"的行上踩 KeyError —— 而那恰恰是撞库最该被看见的行。"""
    label, kind = describe_ua(e.user_agent or "")
    return {"id": e.id, "createdAt": e.created_at.isoformat(), "result": e.result,
            "email": e.email, "ip": e.ip, "device": label, "deviceKind": kind,
            "userAgent": e.user_agent, "userName": user_name}


@router.get("/api/admin/users/{user_id}/access")
def user_access(user_id: int, ctx: AuthContext = Depends(require_admin),
                db: DbSession = Depends(get_db)):
    """某个账号的登录状态:当前登录限制 + 活跃会话 + 最近登录记录(登录详情抽屉一次拉齐)。

    口径与用户列表一致(同一张 login_events、同一个在线窗口),不另立一套判据。
    """
    user = db.get(User, user_id)
    if not user:
        err(404, "NOT_FOUND", "用户不存在")
    now = utcnow()
    sessions = (db.query(AuthSession)
                .filter(AuthSession.user_id == user.id, AuthSession.expires_at > now)
                .order_by(AuthSession.created_at.desc()).all())
    events = (db.query(LoginEvent).filter(LoginEvent.user_id == user.id)
              .order_by(LoginEvent.created_at.desc()).limit(50).all())
    return {
        "user": {"id": user.id, "email": user.email, "name": user.name,
                 "isAdmin": bool(user.is_admin), "isDisabled": bool(user.is_disabled)},
        "lock": throttle.state(db, LOGIN_ACCOUNT_POLICY, user.email),
        # 上限一并给出:界面才说得出"再失败 2 次将锁定"这类话,而不是让管理员去翻文档
        "maxFails": LOGIN_ACCOUNT_POLICY.max_fails,
        "sessions": [_session_json(s, current_token=ctx.session_token) for s in sessions],
        "events": [_event_json(e) for e in events],
    }


@router.delete("/api/admin/sessions/{ref}")
def revoke_session(ref: str, ctx: AuthContext = Depends(require_admin),
                   db: DbSession = Depends(get_db)):
    """按 ref(sha256(token))强制下线一条会话。找不到 → 404。

    按哈希比对而不是给会话加一列非秘密 id:会话表只有几十~几百行,遍历一次是主键扫描;
    而 token 就是会话的键(models.py「秘密与标识分离」),为展示再造一个标识列,
    等于给一张全量变动的表改结构。
    """
    for sess in db.query(AuthSession).all():
        if hashlib.sha256(sess.token.encode("utf-8")).hexdigest() == ref:
            db.delete(sess)
            db.commit()
            return {"ok": True, "revoked": 1}
    err(404, "NOT_FOUND", "会话不存在或已失效")


@router.delete("/api/admin/users/{user_id}/sessions")
def revoke_user_sessions(user_id: int, ctx: AuthContext = Depends(require_admin),
                         db: DbSession = Depends(get_db)):
    """把某个账号的所有会话踢下线(账号疑似被盗时的第一动作),返回踢掉几条。

    与"重置密码吊销全部会话"同一语义,区别是不改口令 —— 适用于"怀疑会话被窃取但
    口令没泄露"的场景。
    """
    user = db.get(User, user_id)
    if not user:
        err(404, "NOT_FOUND", "用户不存在")
    n = (db.query(AuthSession).filter(AuthSession.user_id == user.id)
         .delete(synchronize_session=False))
    db.commit()
    return {"ok": True, "revoked": int(n)}


@router.get("/api/admin/login-events")
def login_events(limit: int = 100, ctx: AuthContext = Depends(require_admin),
                 db: DbSession = Depends(get_db)):
    """全站最近登录动态 —— 含**账号不存在**的失败,那是撞库/密码喷洒最直接的痕迹。

    刻意不做筛选参数:几十人内网规模下,要看某个人就进他的登录详情抽屉。
    """
    limit = max(1, min(int(limit), 500))
    rows = db.query(LoginEvent).order_by(LoginEvent.created_at.desc()).limit(limit).all()
    ids = {r.user_id for r in rows if r.user_id}
    names = ({u.id: u.name for u in db.query(User).filter(User.id.in_(ids)).all()}
             if ids else {})
    return [_event_json(r, names.get(r.user_id)) for r in rows]


@router.post("/api/admin/storage/cleanup")
def cleanup_orphans(dryRun: bool = False, force: bool = False,
                    ctx: AuthContext = Depends(require_admin),
                    db: DbSession = Depends(get_db)):
    """清理无主物理文件(磁盘上有、DB 里无引用)。

    只删 orphans,不碰 missing —— 后者是 DB 记录还在但文件丢了,
    需要的是从备份恢复内容,删记录只会把问题藏起来。

    三道防护(前两道此前都没有):
    - `dryRun=1`:只报告将删除什么,一个文件都不碰。界面上先预览再确认。
    - **熔断**:孤儿数超过磁盘文件总数的 2/3 时拒绝执行,除非显式 `force=1`。
      防的是这个真实场景:库被换成空的/旧的、或指向了错的数据目录 ——
      此时磁盘上每个文件都会被判为孤儿,一键下去数据全没了。
      正常情况孤儿只是少数残留,出现"绝大多数都是孤儿"本身就是危险信号。
    - **跳过在传文件**(`inflightSkipped` 计数):上传先落盘、后提交记录,这期间它
      必然不在 known 里。熔断拦不住单个在传文件,漏跳就是"上传成功但文件没了"。
    """
    scan = _scan_orphans(db)
    victims, on_disk = scan["victims"], scan["onDisk"]
    blocked = scan["breakerTripped"] and not force
    preview = {
        "dryRun": True,
        "orphans": len(victims),
        "orphanBytes": sum(s for _, s in victims),
        "filesOnDisk": on_disk,
        "inflightSkipped": scan["inflight"],
        "breakerTripped": scan["breakerTripped"],
        "blocked": blocked,
    }
    if dryRun:
        return preview
    if blocked:
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
            "orphans": len(victims), "filesOnDisk": on_disk,
            "inflightSkipped": scan["inflight"]}


@router.get("/api/admin/projects")
def admin_projects(ctx: AuthContext = Depends(require_admin),
                   db: DbSession = Depends(get_db)):
    """管理后台「项目」总览:全部协作项目,**不含个人空间**。
    个人空间按设计不可管理(不可删/不可管成员/不可公开,§4.2),放进"接管与
    生命周期管理"列表是纯噪声,且与"个人空间对管理员保密"的立场矛盾(Notion/
    GitHub 派,而非 GitLab/Google 的合规派);其占用亦不设管理员视图,存储总览
    只给实例总量。契约保留 isPersonal 字段(恒 false),为将来 includePersonal
    审计开关预留。
    管理员视角要的是"谁拥有、占多少、活跃吗",与 projects.project_json 的成员
    视角(myRole/isMember)消费者不同,故独立序列化,但字段名与其保持一致。
    计数/占用经 projects.batch_stats 批量聚合(4 条 GROUP BY),不做每项目扫表;
    storageBytes 只含活跃云空间文件(回收站占用在存储总览单列,不在此重复)。"""
    ps = (db.query(Project).filter(Project.is_personal.is_(False))
          .order_by(Project.name.asc()).all())
    stats = batch_stats(db, [p.id for p in ps])
    owners: dict = {}
    for pid, u in (db.query(ProjectMember.project_id, User)
                   .join(User, User.id == ProjectMember.user_id)
                   .filter(ProjectMember.project_id.in_([p.id for p in ps]),
                           ProjectMember.role == "OWNER").all()):
        owners.setdefault(pid, []).append({
            "id": u.id, "name": u.name, "email": u.email,
            "isDisabled": bool(u.is_disabled), "avatarColor": avatar_color(u.id)})
    out = []
    for p in ps:
        s = stats.get(p.id, {})
        out.append({
            "id": p.id, "name": p.name, "description": p.description,
            "isPersonal": bool(p.is_personal), "isPublic": p.visibility == "public",
            "createdAt": p.created_at.isoformat(),
            "lastUpdatedAt": s["lastUpdatedAt"].isoformat() if s.get("lastUpdatedAt") else None,
            "memberCount": s.get("memberCount", 0),
            "docCount": s.get("docCount", 0),
            "storageBytes": s.get("storageBytes", 0),
            "owners": owners.get(p.id, []),
        })
    return out


@router.get("/api/admin/backup")
def download_backup(ctx: AuthContext = Depends(require_admin),
                    db: DbSession = Depends(get_db)):
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
    # 生成归档与回吐期间不需要数据库,先结束事务(归档可能几 GB,见 models.release_db)
    release_db(db)
    if not backup.BACKUP_LOCK.acquire(timeout=300):
        err(409, "CONFLICT", "另一轮备份正在进行,请稍后再试")
    try:
        archive, size, _stats = backup.build_archive()
    finally:
        backup.BACKUP_LOCK.release()

    def gen():
        try:
            with open(archive, "rb") as fh:
                yield from chunked_file(fh)
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
def backup_run_now(ctx: AuthContext = Depends(require_admin)):
    """立即执行一轮备份并返回各目标结果(不等定时)。"""
    return backup.run_backup(trigger="manual")


# ---------- 恢复 ----------
# 上传 → 校验 → "重启后生效"。不在请求里直接换库:替换运行中的 SQLite 库
# (及其 -wal/-shm)是危险动作。替换发生在下次启动、任何连接建立之前。

@router.get("/api/admin/restore/status")
def restore_status(ctx: AuthContext = Depends(require_admin)):
    return backup.restore_status()


@router.post("/api/admin/restore/upload")
async def restore_upload(request: Request, ctx: AuthContext = Depends(require_admin),
                         db: DbSession = Depends(get_db)):
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

    # 收 body 前结束事务(备份包可能上 GB,理由同 files.upload_file)
    release_db(db)
    backup.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    part = backup.STAGING_UPLOAD.with_name("restore-upload.zip.part")
    status, _total = await save_request_body(request, part, MAX_UPLOAD_BYTES)
    if status == "too_large":
        err(400, "VALIDATION", f"备份文件过大(上限 {MAX_UPLOAD_MB}MB)")
    if status == "interrupted":
        err(400, "VALIDATION", "上传中断,请重试")
    if status == "empty":
        err(400, "VALIDATION", "请求体为空,未收到备份内容")

    # 校验(白名单 + 版本一致性 + 可读性)在 stage_upload 内完成,失败即拒绝并删除
    info, msg = backup.stage_upload(part)
    if not info:
        err(400, "VALIDATION", f"备份校验未通过:{msg}")
    return {"ok": True, "info": info}


@router.post("/api/admin/restore/arm")
def restore_arm(ctx: AuthContext = Depends(require_admin)):
    """标记暂存的备份在下次重启时生效。"""
    ok, msg = backup.arm_restore()
    if not ok:
        err(400, "VALIDATION", msg)
    return {"ok": True, "armed": True}


@router.delete("/api/admin/restore")
def restore_cancel(ctx: AuthContext = Depends(require_admin)):
    """丢弃暂存的备份 / 取消待生效的恢复。"""
    backup.clear_restore()
    return {"ok": True}
