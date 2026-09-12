"""整站备份与恢复(仅管理员)。

为什么单独一个模块:备份是一条完整生命周期 —— 一致性快照 → 落盘到多个目标 →
完整性校验 → 保留策略清旧 → 记录结果 → (恢复侧)暂存/校验/开机应用。
塞进 admin.py 会让那个文件同时承担"存储统计"和"备份运维"两件事,也让备份的
测试入口变得含混。admin.py 只保留 HTTP 入口,实现都在这里。

## 三条不能退让的设计

1. **快照必须用 VACUUM INTO**。WAL 模式下未 checkpoint 的写入还在 -wal 里,
   直接复制 teamdoc.db 会拿到旧快照(实测复制出的库一张表都没有)。

2. **目标目录必须已存在,绝不自动创建**。若自动 mkdir,移动硬盘没插、
   NAS 没挂载时会在本机静默建出同名目录,备份写到假路径上,而管理员以为成功了 ——
   这比直接失败危险得多。不存在就是失败,明确报出来。

3. **禁止把整包落进系统临时目录**。原实现用 SpooledTemporaryFile,超过 64MB 会
   溢写到系统盘(C:),系统盘小的时候备份直接失败,且失败原因难以看懂。
   现在快照与 zip 都在数据目录的暂存区生成,再逐个投递到目标。

## 定时任务:这是本项目唯一的后台线程

项目其余部分没有任何调度器(VERSION_MERGE_MINUTES 是保存时的时间戳比较,不是定时器)。
定时备份若靠"请求驱动"就永远不触发,所以这里必须起线程。线程整体包 try/except ——
定时任务最怕的不是某次失败,而是静默死掉后再也不备份。
"""
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import models
from models import DB_PATH, FILES_DIR, DATA_DIR, utcnow

log = logging.getLogger("teamdoc.backup")

# 文件名常量:保留策略只认这个前缀/后缀,绝不误删管理员手工放进目录的其它文件
BACKUP_PREFIX = "teamdoc-backup-"
BACKUP_SUFFIX = ".zip"
STAMP_FMT = "%Y%m%d-%H%M%S"

# 逗号分隔而不是 os.pathsep:Windows 用 ";" 分隔,而盘符本身带冒号(D:\),
# 一旦有人写成 "D:\a;E:\b" 用 pathsep 切会把盘符切坏。逗号在路径里几乎不会出现。
BACKUP_DIRS = [d.strip() for d in os.environ.get("BACKUP_DIRS", "").split(",") if d.strip()]


def _env_float(name: str, default: float) -> float:
    """容错的浮点环境变量读取:写错了退回默认值,不让服务起不来。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("环境变量 %s=%r 不是数字,退回默认值 %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


# 间隔为 0 表示关闭定时备份(仍需手动触发/下载)
BACKUP_INTERVAL_HOURS = _env_float("BACKUP_INTERVAL_HOURS", 24.0)
BACKUP_KEEP = _env_int("BACKUP_KEEP", 7)

# 备份生成/投递互斥。单 worker 下这是一个进程内锁,足以覆盖"定时线程与手动触发
# 同时到达"以及"管理员重复点按钮"。它保护的是暂存区的固定命名文件。
BACKUP_LOCK = threading.Lock()

# 状态落盘用 JSON 而非建表:避免为了运维元数据动 models.py(结构单一来源,漂移会启动失败)。
# 见 DEPLOY.md §2.0。
STATE_PATH = DATA_DIR / "backup-state.json"
STAGING_DIR = DATA_DIR / ".backup-staging"

RESTORE_README = """TeamDoc Lite 备份
==================

本压缩包包含:
  teamdoc.db   数据库(用 SQLite VACUUM INTO 生成的一致性快照)
  files/       云空间的全部物理文件(文件名即数据库 files 表的 storage_path 基名)
  RESTORE.txt  本说明

恢复步骤(推荐:在管理后台「备份与恢复」里直接上传本压缩包)
----------------------------------------------------------------

1. 管理后台 → 备份与恢复 → 上传本 zip,服务端会校验(结构与当前版本一致、
   库可打开、表齐全)并暂存。
2. 点「重启后生效」。服务重启时会自动完成替换。
3. 重启前请确认暂存信息无误 —— 替换会覆盖当前全部数据。

手工恢复(无法进入管理后台时)
----------------------------

1. **停掉正在运行的 TeamDoc 服务**(务必先停,否则数据库可能被写坏)。
2. 把原数据目录改名保留(默认 lite/server/data/,可用 TEAMDOC_DATA_DIR 指定):
       mv data data.old
3. 新建数据目录,把本压缩包解开到其中,使结构为:
       data/teamdoc.db
       data/files/<一堆无扩展名的文件>
4. 启动服务。

注意
----

* **只能恢复到相同版本的 TeamDoc**。本项目没有数据库迁移机制:启动时只建缺失的
  表,已有表不加列,结构与 models.py 不一致会直接启动失败。备份里的库若来自
  不同版本,恢复后会起不来 —— 上传时服务端会预先校验并拒绝这类备份。
* 直接复制 teamdoc.db 是**不可靠**的:数据库以 WAL 模式运行,尚未 checkpoint 的
  写入还在 teamdoc.db-wal 里,单独复制 .db 会拿到旧快照。本备份用 VACUUM INTO
  生成,内容完整,不需要 -wal / -shm 文件。
* 备份期间服务可以继续运行(生成的是某个时刻的一致快照),但恢复必须停机(或走
  上面的"重启后生效")。
* 备份不含会话与 PAT 的明文(PAT 只存 sha256),恢复后用户需重新登录。
"""


# ---------- 目标目录 ----------

def configured_dirs() -> list[Path]:
    return [Path(d) for d in BACKUP_DIRS]


def target_report() -> list[dict]:
    """给界面用:每个配置目标当前是否可达(不存在时提前警示,而不是等备份失败)。"""
    out = []
    for d in configured_dirs():
        out.append({"path": str(d), "exists": d.is_dir()})
    return out


# ---------- 状态 ----------

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    """原子写:临时文件 + replace,避免读取方看到半截 JSON。"""
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _parse_ts(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


# ---------- 快照与打包 ----------

def make_db_snapshot(target: Path) -> None:
    """用 VACUUM INTO 生成一致性快照(不停机)。"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


def _expected_tables() -> set[str]:
    return {t.name for t in models.Base.metadata.sorted_tables}


def _snapshot_file_names(snap: Path) -> set[str]:
    """读取快照里 files 表引用的物理文件名集合(只取 basename)。

    为什么按**快照里的清单**打包,而不是直接扫 FILES_DIR:扫盘会掺进"快照之后
    才出现或消失"的文件 —— 正在上传的半截文件(有文件无记录)、刚被硬删的物理
    文件(有记录无文件),于是包内的库与 files/ 互相对不上,恢复时才暴露。
    以快照为基准,两边必然一致:快照里没有的记录不会进来,快照里有的我们尽力凑齐。

    取 basename 而非原样路径:storage_path 可能是绝对路径(历史数据),也可能是
    相对名(见 files.py 的说明),basename 两种都能对上,换机恢复的包也才可移植。
    """
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT storage_path FROM files").fetchall()
    finally:
        conn.close()
    return {Path(r[0]).name for r in rows if r[0]}


def build_archive() -> tuple[Path, int, dict]:
    """在暂存区生成一份备份 zip,返回 (路径, 字节数, 统计)。

    调用方负责用完 unlink —— 下载端点要流式回吐,定时任务要投递到多个目标。

    **必须持 BACKUP_LOCK 调用**:生成过程会写固定命名的暂存文件,并发进入会
    互相踩(VACUUM INTO 目标重名报 "table already exists"、两个 ZipFile 同写
    一个 .part 导致包损坏)。run_backup 与下载端点都已在锁内调用。
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    # 毫秒精度:即便锁失效(或将来多进程),同一秒内的两次备份也不会同名互相覆盖。
    # 定长格式,prune 的字典序排序不受影响。
    now = datetime.now()
    stamp = now.strftime(STAMP_FMT) + f"-{now.microsecond // 1000:03d}"
    final = STAGING_DIR / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    part = final.with_name(final.name + ".part")
    snap = STAGING_DIR / f".snapshot-{stamp}.db"
    missing: list[str] = []
    included = 0
    try:
        make_db_snapshot(snap)
        names = _snapshot_file_names(snap)
        # 先写 .part 再改名:半截包若直接叫 .zip,日后会被当成一份"好备份"
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.write(str(snap), arcname="teamdoc.db")
            z.writestr("RESTORE.txt", RESTORE_README)
            for name in sorted(names):
                p = FILES_DIR / name
                if p.is_file():
                    z.write(str(p), arcname=f"files/{name}")
                    included += 1
                else:
                    # 库里有记录、盘上没文件(missing):收不进包,但要如实计数上报,
                    # 不能让一份"缺文件"的备份看起来完全成功
                    missing.append(name)
        os.replace(part, final)
    finally:
        snap.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return final, final.stat().st_size, {"filesIncluded": included,
                                        "filesMissing": len(missing)}


def verify_archive(path: Path) -> tuple[bool, str]:
    """校验一份备份是否可用:zip 可解、库可打开、表齐全。

    这是"备份有没有真的写成"的唯一判据 —— 不校验就只会留下一个看似成功、
    恢复那天才发现坏掉的包。校验失败的文件会被调用方删掉,不留误导性的残骸。

    覆盖范围按"能恢复"这个目标定:zip 结构与 db 完整性。不做全量 testzip ——
    那要解压每一个文件,大库下代价与收益不成比例;落盘用 shutil.copyfile,
    它要么写完要么抛错,再叠加下面的大小比对即可挡住截断。
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "teamdoc.db" not in names:
                return False, "备份内缺少 teamdoc.db"
            with tempfile.TemporaryDirectory(prefix=".verify-",
                                              dir=_staging_dir()) as tmp:
                z.extract("teamdoc.db", tmp)
                dbp = Path(tmp) / "teamdoc.db"
                if not dbp.is_file():
                    return False, "teamdoc.db 解压失败"
                try:
                    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
                    try:
                        rows = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    finally:
                        conn.close()
                except sqlite3.Error as e:
                    return False, f"备份内的数据库无法打开:{e}"
    except zipfile.BadZipFile:
        return False, "不是有效的 zip 文件(可能传输中断或文件损坏)"
    except OSError as e:
        return False, f"读取备份失败:{e}"

    have = {r[0] for r in rows}
    missing = _expected_tables() - have
    if missing:
        return False, ("备份结构与当前版本不一致,缺少表:" + ", ".join(sorted(missing)) +
                       "。本项目无数据库迁移,只能恢复到相同版本。")
    return True, ""


def _staging_dir() -> str:
    """证明暂存目录存在并返回其字符串路径 —— 校验/恢复的临时目录都开在这里。

    刻意不开在系统临时目录:系统盘可能很小,而备份的中间产物与数据同量级,
    放系统盘会让"备份"在一台健康的数据盘上因 C: 满而失败。
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    return str(STAGING_DIR)


# ---------- 保留策略 ----------

def list_archives(dest: Path) -> list[Path]:
    """目标目录里由本程序生成的备份,按时间倒序(新→旧)。

    只认 BACKUP_PREFIX/SUFFIX:管理员往这个目录里放别的文件是常见做法
    (比如手工复制的旧备份、说明文件),保留策略绝不能碰它们。
    时间戳是零填充定长,字典序即时间序 —— 比 mtime 可靠(复制会改 mtime)。
    """
    try:
        entries = [e for e in os.scandir(dest) if e.is_file(follow_symlinks=False)]
    except OSError:
        return []
    mine = [Path(e.path) for e in entries
            if e.name.startswith(BACKUP_PREFIX) and e.name.endswith(BACKUP_SUFFIX)]
    return sorted(mine, key=lambda p: p.name, reverse=True)


def prune(dest: Path, keep: int) -> list[str]:
    """保留最近 keep 份,返回被删除的文件名。keep<=0 表示不限(不清)。"""
    if keep <= 0:
        return []
    removed = []
    for p in list_archives(dest)[keep:]:
        try:
            p.unlink()
            removed.append(p.name)
        except OSError as e:
            log.warning("清理旧备份失败 %s: %s", p, e)
    return removed


# ---------- 一次备份 ----------

def _deliver(archive: Path, dest: Path, size: int) -> dict:
    """把已生成好的备份投递到一个目标目录,并校验落盘结果。"""
    if not dest.is_dir():
        return {"ok": False, "error": "目标目录不存在(不会自动创建:移动盘未插或共享未挂载时,"
                                      "自动建目录会把备份写到本机的假路径上)"}
    final = dest / archive.name
    part = dest / (archive.name + ".part")
    try:
        shutil.copyfile(archive, part)
        got = part.stat().st_size
        if got != size:
            raise OSError(f"写入大小不符(期望 {size} 字节,实得 {got})")
        os.replace(part, final)
    except OSError as e:
        part.unlink(missing_ok=True)
        return {"ok": False, "error": f"写入失败:{e}"}

    ok, err = verify_archive(final)
    if not ok:
        final.unlink(missing_ok=True)  # 坏包必须删掉,不能留在目录里冒充好备份
        return {"ok": False, "error": err}

    return {"ok": True, "file": archive.name, "bytes": size,
            "pruned": len(prune(dest, BACKUP_KEEP))}


def run_backup(trigger: str = "manual") -> dict:
    """执行一轮备份:生成一次,投递到全部配置目标。

    生成一次再逐个复制(而不是每个目标各打一遍包):压缩只做一次,
    代价是暂存盘要放得下一份包 —— 暂存就在数据目录里,它本来就得放得下数据。

    整个流程持 BACKUP_LOCK:定时线程与手动触发可能同时到达,而生成阶段写的是
    固定命名暂存文件,并发进入会直接失败(实测 8 个并发请求全军覆没)。
    拿不到锁就立刻返回"已有备份在执行",不排队 —— 排队没有意义(重复备份)。
    """
    dirs = configured_dirs()
    started = utcnow()
    result = {"trigger": trigger, "startedAt": started.isoformat(sep=" ", timespec="seconds"),
              "targets": {}, "file": None, "bytes": 0}

    if not dirs:
        result["ok"] = False
        result["error"] = "未配置备份目标目录(环境变量 BACKUP_DIRS 为空)"
        _record(result, started)
        return result

    if not BACKUP_LOCK.acquire(blocking=False):
        result["ok"] = False
        result["busy"] = True
        result["error"] = "已有一轮备份正在进行,本次跳过"
        _record(result, started)
        return result

    archive = None
    try:
        archive, size, stats = build_archive()
        result["file"] = archive.name
        result["bytes"] = size
        result.update(stats)
        # 生成后先自检一次:若生成阶段就坏了(磁盘错误等),没必要再往每个目标写一遍。
        # 目标侧的校验(_deliver 里)仍保留 —— 它验的是"这个目标盘上的那份"。
        ok, err = verify_archive(archive)
        if not ok:
            result["targets"] = {str(d): {"ok": False, "error": f"生成的备份未通过自检:{err}"}
                                 for d in dirs}
        else:
            for d in dirs:
                result["targets"][str(d)] = _deliver(archive, d, size)
    except Exception as e:  # 生成阶段失败(磁盘满/库锁等):所有目标都算失败
        log.exception("备份生成失败")
        result["targets"] = {str(d): {"ok": False, "error": f"备份生成失败:{e}"} for d in dirs}
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
        BACKUP_LOCK.release()

    oks = [t for t in result["targets"].values() if t.get("ok")]
    result["ok"] = len(oks) > 0
    if result["ok"] and len(oks) < len(result["targets"]):
        result["partial"] = True
    _record(result, started)
    return result


def _record(result: dict, started: datetime) -> None:
    """把这一轮结果与下次计划时间写进状态文件。"""
    state = load_state()
    finished = utcnow()
    state.update({
        "lastRun": result["startedAt"],
        "lastOk": bool(result.get("ok")),
        "lastPartial": bool(result.get("partial")),
        "lastError": result.get("error", ""),
        "lastFile": result.get("file"),
        "lastBytes": result.get("bytes", 0),
        "lastTrigger": result.get("trigger"),
        "lastBusy": bool(result.get("busy")),
        # 快照里有记录、盘上却没文件的条数。>0 时这份备份是**不完整**的,
        # 界面必须显示出来 —— 一份缺文件的备份看起来完全正常,只在恢复时才露馅
        "lastFilesMissing": result.get("filesMissing", 0),
        "lastFilesIncluded": result.get("filesIncluded", 0),
        "lastDurationSec": round((finished - started).total_seconds(), 1),
        "targets": result.get("targets", {}),
        "nextRun": (finished + timedelta(hours=BACKUP_INTERVAL_HOURS)
                    ).isoformat(sep=" ", timespec="seconds") if BACKUP_INTERVAL_HOURS > 0 else None,
    })
    try:
        save_state(state)
    except OSError as e:
        log.error("备份状态写入失败: %s", e)


def status() -> dict:
    """给界面的完整现状:配置 + 上次结果 + 下次计划 + 目标可达性。"""
    st = load_state()
    return {
        "config": {
            "dirs": target_report(),
            "intervalHours": BACKUP_INTERVAL_HOURS,
            "keep": BACKUP_KEEP,
            "enabled": bool(configured_dirs()) and BACKUP_INTERVAL_HOURS > 0,
        },
        "last": {
            "runAt": st.get("lastRun"),
            "ok": st.get("lastOk"),
            "partial": st.get("lastPartial", False),
            "error": st.get("lastError", ""),
            "file": st.get("lastFile"),
            "bytes": st.get("lastBytes", 0),
            "trigger": st.get("lastTrigger"),
            "busy": st.get("lastBusy", False),
            "filesMissing": st.get("lastFilesMissing", 0),
            "filesIncluded": st.get("lastFilesIncluded", 0),
            "durationSec": st.get("lastDurationSec"),
            "targets": st.get("targets", {}),
        },
        "nextRun": st.get("nextRun"),
    }


# ---------- 定时线程 ----------

TICK_SECONDS = 60
_thread_started = False


def start_scheduler() -> bool:
    """按配置启动定时备份线程;返回是否真的启动了。

    关掉的情形(不启动):未配置目标目录,或间隔为 0 —— 没有目标就没有备份可言,
    起线程只会空转并不断失败。
    """
    global _thread_started
    if _thread_started:
        return False
    if not configured_dirs() or BACKUP_INTERVAL_HOURS <= 0:
        return False
    _thread_started = True
    t = threading.Thread(target=_loop, name="teamdoc-backup", daemon=True)
    t.start()
    log.info("定时备份已启动:每 %s 小时 → %s", BACKUP_INTERVAL_HOURS,
             ", ".join(str(d) for d in configured_dirs()))
    return True


def _loop() -> None:
    # 启动时不立即备份:服务重启(含升级、恢复后重启)会很频繁,
    # 每次重启都打一份包既浪费又掩盖真正的计划节奏。到点才跑。
    while True:
        try:
            cap = utcnow() + timedelta(hours=BACKUP_INTERVAL_HOURS)
            state = load_state()
            nxt = _parse_ts(state.get("nextRun"))
            if nxt is None or nxt > cap:
                # 两种情况都把计划重排到"从现在起一个间隔之后":
                #   1) 从未备份过(没有 nextRun)
                #   2) 两次重启之间把间隔改短了 —— 旧 nextRun 还在很远的未来,
                #      不重算的话新间隔要等到原时间点才生效,看起来像"改了没反应"
                state["nextRun"] = cap.isoformat(sep=" ", timespec="seconds")
                save_state(state)
            elif utcnow() >= nxt:
                run_backup(trigger="scheduled")
        except Exception:
            # 关键:线程绝不能因单次异常退出。定时任务静默死掉是最糟的失败模式。
            log.exception("定时备份本轮异常(线程继续运行)")
        time.sleep(TICK_SECONDS)


# ============================================================
# 恢复
# ============================================================
#
# 为什么不在请求里直接换库:替换正在运行的 SQLite 库(以及它旁边的 -wal/-shm)是
# 危险动作 —— 连接池里还有句柄、并发请求可能正在写。所以走"上传 → 校验 → 标记得
# 以生效 → 重启时应用":重启瞬间没有任何连接,替换是原子的。
#
# 为什么暂存在数据目录里而不是系统临时目录:备份包与数据同量级,放系统盘会让
# "恢复"在一台健康的数据盘上因 C: 满而失败。

STAGING_UPLOAD = STAGING_DIR / "restore-upload.zip"
STAGING_META = STAGING_DIR / "restore-meta.json"
STAGING_ARMED = STAGING_DIR / "RESTORE-ARMED"


def _validate_member(name: str) -> str | None:
    """备份内条目白名单校验,返回 None 表示合法,否则返回错误原因。

    这是**安全边界**:zip 里的条目名由上传方控制,若直接按名字落盘,一个
    `../../evil` 或 `C:\\Windows\\...` 就能写到数据目录之外(经典的 zip slip)。
    本项目的备份只会含三类条目,所以用白名单而不是"过滤掉危险字符"——
    白名单不依赖对攻击手法的穷举。
    """
    norm = name.replace("\\", "/")
    if norm in ("teamdoc.db", "RESTORE.txt"):
        return None
    if norm.startswith("files/"):
        rest = norm[len("files/"):]
        # 物理文件名就是 16 位十六进制平铺在 files/ 下,不应有任何层级
        if rest and "/" not in rest and rest not in (".", "..") and ":" not in rest:
            return None
    return f"备份内有意外条目:{name}(本项目的备份只含 teamdoc.db、RESTORE.txt、files/<文件名>)" 


def inspect_archive(path: Path) -> tuple[dict | None, str]:
    """校验并读取一份备份的摘要信息。返回 (info, 错误信息)。

    上传时调用一次,arm/应用前再调用一次 —— 两次之间文件可能被替换或改坏。
    """
    ok, err = verify_archive(path)
    if not ok:
        return None, err

    try:
        with zipfile.ZipFile(path) as z:
            members = z.infolist()
            # zip bomb 防护:声明的解压总量若超过数据盘可用空间,根本别开始解。
            # 用"磁盘可用空间"而不是写死一个字节数 —— 备份本来就该放得下,
            # 而一个声明 1TB 的恶意包在 100GB 的盘上会立刻被拒。
            declared = sum(i.file_size for i in members)
            limit = _restore_size_limit()
            if declared > limit:
                return None, (f"备份解压后需要 {declared} 字节,超过可用空间 {limit} 字节;"
                              "已拒绝(也可能是压缩炸弹)")
            for info in members:
                bad = _validate_member(info.filename)
                if bad:
                    return None, bad
            with tempfile.TemporaryDirectory(prefix=".inspect-", dir=_staging_dir()) as tmp:
                z.extract("teamdoc.db", tmp)
                counts = _db_counts(Path(tmp) / "teamdoc.db")
    except (zipfile.BadZipFile, OSError) as e:
        return None, f"读取备份失败:{e}"

    return {"bytes": path.stat().st_size, **counts}, ""


def _restore_size_limit() -> int:
    """解压总量上限 = 数据盘当前可用空间。取它是为了"写不下的就别开始解"。"""
    try:
        return shutil.disk_usage(str(DATA_DIR)).free
    except OSError:
        return 0


def _db_counts(dbpath: Path) -> dict:
    """从备份的库里读出"恢复后大概是什么样",供管理员确认时核对。"""
    out = {"users": None, "projects": None, "docs": None, "files": None}
    try:
        conn = sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)
        try:
            for key, table in (("users", "users"), ("projects", "projects"),
                               ("docs", "docs"), ("files", "files")):
                try:
                    out[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    pass
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return out


def stage_upload(part: Path) -> tuple[dict | None, str]:
    """校验已流式落盘的 .part,通过后转正为待恢复备份。返回 (info, 错误信息)。

    校验在"转正"之前:坏包绝不占用正式名,重启时的应用逻辑也就不会拿到它。
    失败时删除 .part —— 暂存区不留任何残骸。
    """
    info, err = inspect_archive(part)
    if not info:
        part.unlink(missing_ok=True)
        return None, err
    os.replace(part, STAGING_UPLOAD)
    info["stagedAt"] = utcnow().isoformat(sep=" ", timespec="seconds")
    STAGING_META.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    clear_arm()  # 新上传使旧的"待生效"失效:不能拿 A 的确认去生效 B 的包
    return info, ""


def restore_status() -> dict:
    if not STAGING_UPLOAD.is_file():
        return {"staged": False, "armed": False}
    meta = {}
    try:
        meta = json.loads(STAGING_META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {"staged": True, "armed": STAGING_ARMED.is_file(), **meta}


def clear_restore() -> None:
    STAGING_UPLOAD.unlink(missing_ok=True)
    STAGING_META.unlink(missing_ok=True)
    clear_arm()


def clear_arm() -> None:
    STAGING_ARMED.unlink(missing_ok=True)


def arm_restore() -> tuple[bool, str]:
    """标记"下次重启应用暂存的备份"。会重新校验一次。"""
    if not STAGING_UPLOAD.is_file():
        return False, "没有待恢复的备份,请先上传"
    info, err = inspect_archive(STAGING_UPLOAD)
    if not info:
        return False, err
    STAGING_ARMED.write_text(
        utcnow().isoformat(sep=" ", timespec="seconds"), encoding="utf-8")
    return True, ""


def apply_pending_restore() -> bool:
    """在启动时应用待恢复的备份。必须在 schema.init() **之前**调用。

    步骤:当前库与 files/ 整体挪到 <data>.pre-restore-<时间戳>/ → 解包备份 → 清暂存。
    先挪后放:若解包失败,原数据仍在旁边那份目录里,人工可以挪回来 ——
    恢复是破坏性操作,必须留下退路。
    """
    if not (STAGING_ARMED.is_file() and STAGING_UPLOAD.is_file()):
        return False

    info, err = inspect_archive(STAGING_UPLOAD)
    if not info:
        # 标记后包又坏了(被替换/磁盘故障):拒绝应用,保留现数据,并清掉标记避免每次重启都试
        log.error("待恢复的备份校验失败,已放弃本次恢复(当前数据未改动):%s", err)
        clear_arm()
        return False
    try:  # 上传时间在 meta 里(日志里报出来,事后能对上"恢复的是哪一份")
        info.update(json.loads(STAGING_META.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = DATA_DIR.parent / f"{DATA_DIR.name}.pre-restore-{stamp}"
    try:
        keep.mkdir(parents=True, exist_ok=False)
        # 库与它的 WAL 附属文件一起挪走:只挪 .db 会把未 checkpoint 的写入留在原地,
        # 恢复后那些记录会以"幽灵"形式跟着新库跑(WAL 属于旧库)。
        for name in ("teamdoc.db", "teamdoc.db-wal", "teamdoc.db-shm"):
            p = DATA_DIR / name
            if p.exists():
                shutil.move(str(p), str(keep / name))
        if FILES_DIR.is_dir():
            shutil.move(str(FILES_DIR), str(keep / "files"))
        FILES_DIR.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(STAGING_UPLOAD) as z:
            for zi in z.infolist():
                if zi.filename == "teamdoc.db":
                    with z.open(zi) as src, open(DB_PATH, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                elif zi.filename.startswith("files/"):
                    base = Path(zi.filename.replace("\\", "/")).name
                    with z.open(zi) as src, open(FILES_DIR / base, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except Exception:
        log.exception("恢复失败 —— 原数据保留在 %s,可人工挪回", keep)
        return False

    clear_restore()
    log.warning("已从备份恢复数据(备份上传于 %s)。恢复前的数据保留在 %s",
                info.get("stagedAt") or "未知时间", keep)
    return True

