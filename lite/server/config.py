"""运行期配置的唯一来源:所有环境变量在这里读取、校验、给默认值。

三条规则:

1. **解析失败即启动失败**。写错的 `MAX_UPLOAD_MB=abc` 会让服务起不来,而不是
   悄悄退回默认值跑下去 —— 后者最坏的情况是"以为设了上限,其实没有"。
2. **默认值只写一次**。`LIMITS` 是全部生效值的唯一来源:管理后台的 diagnostics
   直接输出它,DEPLOY.md 的环境变量表与它一一对应,不再各存一份。
3. **这里的每个常量都有一个环境变量对应**。模块内的其它配置(如节流策略的组合方式、
   密码强度)不是"可调参数",属于代码,不放这里。

模块之间的依赖方向:`config` 不 import 任何项目模块,谁都能 import 它。
"""
import os
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent


def _raw(name: str) -> str | None:
    v = os.environ.get(name)
    return None if v is None or v.strip() == "" else v.strip()


def env_str(name: str, default: str) -> str:
    return _raw(name) or default


def env_int(name: str, default: int, *, min: int | None = None,
            max: int | None = None) -> int:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"环境变量 {name}={raw!r} 不是整数") from None
    if min is not None and value < min or max is not None and value > max:
        raise RuntimeError(f"环境变量 {name}={value} 超出允许范围 "
                           f"[{min if min is not None else '-∞'}, "
                           f"{max if max is not None else '+∞'}]")
    return value


def env_float(name: str, default: float, *, min: float | None = None,
              max: float | None = None) -> float:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"环境变量 {name}={raw!r} 不是数字") from None
    if min is not None and value < min or max is not None and value > max:
        raise RuntimeError(f"环境变量 {name}={value} 超出允许范围 "
                           f"[{min if min is not None else '-∞'}, "
                           f"{max if max is not None else '+∞'}]")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    low = raw.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"环境变量 {name}={raw!r} 不是布尔值(true/false/1/0/yes/no/on/off)")


def env_list(name: str, default: list[str]) -> list[str]:
    """逗号分隔的列表。刻意不用 os.pathsep:Windows 的路径分隔符是 ";" 而盘符
    自带冒号("D:\\"),按 pathsep 切会把盘符切坏,而逗号几乎不会出现在路径里。"""
    raw = _raw(name)
    if raw is None:
        return list(default)
    return [d.strip() for d in raw.split(",") if d.strip()]


# ---------- 部署与存储 ----------

# 数据目录(库 + 物理文件)。默认 server/data;测试与多实例部署靠它隔离。
DATA_DIR = Path(env_str("TEAMDOC_DATA_DIR", str(_BASE_DIR / "data"))).resolve()
PORT = env_int("PORT", 8000, min=1, max=65535)
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 20480, min=1)
STORAGE_RESERVE_MB = env_int("STORAGE_RESERVE_MB", 1024, min=0)
ZIP_MAX_MB = env_int("ZIP_MAX_BYTES_MB", 4096, min=1)

# ---------- 会话与登录节流 ----------

SESSION_TTL_DAYS = env_int("SESSION_TTL_DAYS", 7, min=1)
REMEMBER_TTL_DAYS = env_int("REMEMBER_TTL_DAYS", 30, min=1)
LOGIN_MAX_FAILS = env_int("LOGIN_MAX_FAILS", 5, min=0)
LOGIN_FAIL_WINDOW = env_int("LOGIN_FAIL_WINDOW", 900, min=1)
LOGIN_LOCKOUT = env_int("LOGIN_LOCKOUT", 60, min=0)
LOGIN_LOCKOUT_MAX = env_int("LOGIN_LOCKOUT_MAX", 3600, min=0)
LOGIN_IP_MAX_FAILS = env_int("LOGIN_IP_MAX_FAILS", 20, min=0)
LOGIN_IP_LOCKOUT = env_int("LOGIN_IP_LOCKOUT", 300, min=0)
LOGIN_EVENT_KEEP_DAYS = env_int("LOGIN_EVENT_KEEP_DAYS", 30, min=0)

# ---------- 文档版本 ----------

# 合并窗口(分钟):同一个人、同一成因、窗口内的连续保存合并成一版历史。
# 0 = 每次保存都留一版。
VERSION_MERGE_MINUTES = env_int("VERSION_MERGE_MINUTES", 0, min=0)

# ---------- 备份 ----------

BACKUP_DIRS = env_list("BACKUP_DIRS", [])
# 0 = 关闭定时备份(仍可手动触发/下载)
BACKUP_INTERVAL_HOURS = env_float("BACKUP_INTERVAL_HOURS", 24.0, min=0)
BACKUP_KEEP = env_int("BACKUP_KEEP", 7, min=1)

# ---------- 可观测性 ----------

# 事件循环卡顿的判定阈值(秒);命中即 dump 全部线程栈
WATCHDOG_STALL_SECONDS = env_float("WATCHDOG_STALL_SECONDS", 10.0, min=1)


def limits_json() -> dict:
    """全部生效限额(管理后台 diagnostics 与前端"当前生效值"面板共用)。

    这是这些数字对外暴露的唯一入口:界面不写死默认值,所以改了环境变量、
    界面跟着变,不会出现"页面上写着 20480、实际跑的是另一个数"。
    """
    return {
        "maxUploadMb": MAX_UPLOAD_MB,
        "zipMaxMb": ZIP_MAX_MB,
        "storageReserveMb": STORAGE_RESERVE_MB,
        "sessionTtlDays": SESSION_TTL_DAYS,
        "rememberTtlDays": REMEMBER_TTL_DAYS,
        "versionMergeMinutes": VERSION_MERGE_MINUTES,
        "loginMaxFails": LOGIN_MAX_FAILS,
        "loginFailWindowSeconds": LOGIN_FAIL_WINDOW,
        "loginLockoutSeconds": LOGIN_LOCKOUT,
        "loginLockoutMaxSeconds": LOGIN_LOCKOUT_MAX,
        "loginIpMaxFails": LOGIN_IP_MAX_FAILS,
        "loginIpLockoutSeconds": LOGIN_IP_LOCKOUT,
        "loginEventKeepDays": LOGIN_EVENT_KEEP_DAYS,
        "backupIntervalHours": BACKUP_INTERVAL_HOURS,
        "backupKeep": BACKUP_KEEP,
        "watchdogStallSeconds": WATCHDOG_STALL_SECONDS,
    }
