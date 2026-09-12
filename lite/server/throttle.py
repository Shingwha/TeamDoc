"""凭据尝试节流(机制层;策略与阈值由调用方声明,见 auth.py 的 LOGIN_*_POLICY)。

解决的问题:登录这类"口令可被反复猜"的端点,必须在**执行校验之前**挡住同一账号、
同一来源的连续失败 —— 否则撞库只有算力成本,没有次数成本。

只做机制,不掺业务语义:键、窗口内计数、冷却与退避、过期清扫、管理端查询。
"哪个维度、上限多少、成功要不要清零"全部落在 Policy 上,由 auth.py 定义。

## 为什么状态落库(models.ThrottleState)而不是进程内计数

进程内计数一重启就清零 = "重启即重置"的后门:运维发版、进程崩溃、看门狗重启,
都会顺手把攻击者的计数抹掉。落库后重启也仍然锁着(测试里有专门一条断言)。
代价是失败登录要写一次库 —— 而失败登录本就是低频事件(成功登录不写),
单 worker + SQLite 下完全可承受。

## 并发与读写方式

计数走单条 UPSERT 原子自增(SQLite 的 ON CONFLICT DO UPDATE),不做"读-改-写":
FastAPI 的同步端点跑在线程池里,两个并发失败不该互相覆盖计数。锁定判定在自增后
读回,极端并发下最多少记一次失败,不影响防护强度。

读回刻意走 Core 的列表查询而不是 ORM 实体:同一次请求里 `locked()` 已经把行载入
Session 的 identity map,再 `db.get()` 会拿到**自增前的旧值**(SQLAlchemy 对已在
identity map 中的对象不会重新查库)—— 那会让锁定晚一拍触发。
"""
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as DbSession

from models import ThrottleState, utcnow

# 清扫节流阀:失败登录是低频事件,不必每次记账都全表扫一遍
SWEEP_INTERVAL_SECONDS = 600


@dataclass(frozen=True)
class Policy:
    """一条节流策略。max_fails <= 0 表示该策略关闭(不写状态、不拦请求)。

    - subject: 键的主体。"ident" 用调用方给的身份(账号维度);"client_ip" 用来源地址。
    - lockout/max_lockout: 冷却基数与上限;max_lockout > lockout 时按 strikes 倍增
      (首次 lockout,第二次 2×,第三次 4×…封顶),max_lockout == lockout 即固定时长。
    - clear_on_success: 成功校验后是否清零。账号维度该清(成功即清白);来源维度**不能**
      清 —— 否则攻击者只要手里有一个有效账号,就能不断重置来源桶继续喷洒。
    - idle_reset: 静默多久后 strikes 归零(只影响退避倍数)。没有它,攻击者可以靠
      "每轮打几下"把受害者的冷却永久顶到上限;它必须大于 max_lockout,否则连续攻击
      每过一个冷却周期就被判成"静默"(攻击者总能回到最短冷却)。
    """
    name: str
    max_fails: int
    window: int
    lockout: int
    subject: str = "ident"
    max_lockout: int = 0
    clear_on_success: bool = False
    idle_reset: int = 7200

    def cooldown_seconds(self, strikes: int) -> int:
        base = max(1, self.lockout)
        top = max(base, self.max_lockout or base)
        return int(min(base * (2 ** max(0, strikes - 1)), top))


@dataclass(frozen=True)
class Lock:
    """一次冷却判定结果(仅表示"当前正被拦",不落库)。"""
    key: str
    retry_after: int   # 剩余冷却秒数(>=1,直接进 Retry-After)
    strikes: int
    fail_count: int


@dataclass(frozen=True)
class _Row:
    fail_count: int
    strikes: int
    locked_until: datetime | None
    updated_at: datetime
    window_start: datetime | None = None


def key(policy: Policy, subject: str) -> str:
    return f"{policy.name}:{subject}"


def _read(db: DbSession, k: str) -> _Row | None:
    r = db.execute(select(ThrottleState.fail_count, ThrottleState.strikes,
                          ThrottleState.locked_until, ThrottleState.updated_at,
                          ThrottleState.window_start)
                   .where(ThrottleState.key == k)).first()
    return _Row(*r) if r else None


def _lock_of(k: str, row: _Row | None, now: datetime) -> Lock | None:
    if row is None or row.locked_until is None or row.locked_until <= now:
        return None
    return Lock(key=k, retry_after=max(1, math.ceil((row.locked_until - now).total_seconds())),
                strikes=row.strikes, fail_count=row.fail_count)


def locked(db: DbSession, policy: Policy, subject: str) -> Lock | None:
    """当前是否处于冷却中(只读,不写库)。"""
    if policy.max_fails <= 0:
        return None
    k = key(policy, subject)
    return _lock_of(k, _read(db, k), utcnow())


def count_failure(db: DbSession, policy: Policy, subject: str) -> Lock | None:
    """记一次失败。若本次把计数顶到上限,则进入冷却并返回 Lock;否则返回 None。

    调用方只在**真正跑过校验**之后调用它(冷却期内的拒绝不记账:既不跑校验,
    也不该在一个人连点按钮时把来源桶刷爆)。
    """
    if policy.max_fails <= 0:
        return None
    k = key(policy, subject)
    now = utcnow()
    # 退避倍数的判据是**上一次**失败/锁定的时间,必须在自增前取(自增会把 updated_at 刷成
    # now,之后再读就只能看到"刚刚",静默判据会永远为假)
    prev = _read(db, k)
    window_floor = now - timedelta(seconds=policy.window)
    # 两种"重新开始数"的情形:窗口已过(上次失败太久远)、冷却刚结束(从头数,
    # 但 strikes 保留 → 再犯冷却更长)
    restart = or_(ThrottleState.window_start <= window_floor,
                  ThrottleState.locked_until.isnot(None) & (ThrottleState.locked_until <= now))
    # 冷却期内的调用(正常路径不会有:调用方先用 locked() 挡下)不该消耗/增长计数:
    # 计数只记"真正跑过校验的失败",被拦下的尝试不算一次机会。
    in_lock = ThrottleState.locked_until.isnot(None) & (ThrottleState.locked_until > now)
    stmt = sqlite_insert(ThrottleState).values(
        key=k, fail_count=1, window_start=now, strikes=0, locked_until=None, updated_at=now)
    db.execute(stmt.on_conflict_do_update(
        index_elements=[ThrottleState.key],
        set_={
            "fail_count": case((restart, 1), (in_lock, ThrottleState.fail_count),
                               else_=ThrottleState.fail_count + 1),
            "window_start": case((restart, now), (in_lock, ThrottleState.window_start),
                                 else_=ThrottleState.window_start),
            # 不清 locked_until:并发下另一线程刚设的冷却不该被这一条抹掉
            "locked_until": case((restart, None), else_=ThrottleState.locked_until),
            "updated_at": now,
        }))
    row = _read(db, k)

    lock = _lock_of(k, row, now)
    if lock is not None:  # 已在冷却中(并发下可能发生):不动计数,也不延长
        db.commit()
        return lock
    if row.fail_count < policy.max_fails:
        db.commit()
        return None

    quiet = prev is None or (now - prev.updated_at).total_seconds() > policy.idle_reset
    strikes = 1 if quiet else prev.strikes + 1
    seconds = policy.cooldown_seconds(strikes)
    # 冷却结束后从零数(fail_count=0),但 strikes 保留 → 再犯冷却更长
    db.execute(update(ThrottleState).where(ThrottleState.key == k).values(
        strikes=strikes, locked_until=now + timedelta(seconds=seconds),
        fail_count=0, window_start=now, updated_at=now))
    db.commit()
    return Lock(key=k, retry_after=seconds, strikes=strikes, fail_count=0)


def clear(db: DbSession, policy: Policy, subject: str) -> int:
    """清零(删行):成功校验、管理员解锁都走它。返回是否删掉了状态。"""
    n = db.query(ThrottleState).filter(ThrottleState.key == key(policy, subject)) \
        .delete(synchronize_session=False)
    db.commit()
    return int(n)


def remaining(db: DbSession, policy: Policy, subject: str) -> int | None:
    """上限内还剩几次机会(无状态行则返回 None)。用于"还可尝试 N 次"这类提示。"""
    if policy.max_fails <= 0:
        return None
    row = _read(db, key(policy, subject))
    return None if row is None else max(0, policy.max_fails - row.fail_count)


def state(db: DbSession, policy: Policy, subject: str) -> dict | None:
    """某个主体的原始节流状态(管理端展示用);无状态返回 None。"""
    if policy.max_fails <= 0:
        return None
    row = _read(db, key(policy, subject))
    if row is None:
        return None
    now = utcnow()
    return {
        "failCount": row.fail_count,
        "strikes": row.strikes,
        "lockedUntil": row.locked_until.isoformat()
        if row.locked_until and row.locked_until > now else None,
        "windowStart": row.window_start.isoformat() if row.window_start else None,
    }


def locks_for(db: DbSession, policy: Policy, subjects: list[str]) -> dict[str, str]:
    """批量查"仍在冷却中"的主体 → locked_until(ISO)。管理端列表用,避免 N+1。"""
    if policy.max_fails <= 0 or not subjects:
        return {}
    now = utcnow()
    keys = [key(policy, s) for s in subjects]
    rows = (db.query(ThrottleState.key, ThrottleState.locked_until)
            .filter(ThrottleState.key.in_(keys), ThrottleState.locked_until > now).all())
    return {k.split(":", 1)[1]: lu.isoformat() for k, lu in rows}


def active_count(db: DbSession) -> int:
    """仍在冷却中的键数量(诊断快照用)。"""
    return db.query(ThrottleState).filter(ThrottleState.locked_until > utcnow()).count()


_swept_at = 0.0


def sweep(db: DbSession, max_window: int) -> int:
    """清掉"既不在冷却中、窗口也已过期"的行 —— 纯垃圾(攻击者拿随机邮箱失败会造行)。
    带进程内节流阀(单 worker 部署下进程内状态就是精确的,见 models.INFLIGHT_STORAGE)。"""
    global _swept_at
    now_mono = time.monotonic()
    if now_mono - _swept_at < SWEEP_INTERVAL_SECONDS:
        return 0
    _swept_at = now_mono
    now = utcnow()
    cutoff = now - timedelta(seconds=max(60, max_window))
    n = (db.query(ThrottleState)
         .filter(or_(ThrottleState.locked_until.is_(None), ThrottleState.locked_until <= now),
                 ThrottleState.updated_at <= cutoff)
         .delete(synchronize_session=False))
    db.commit()
    return int(n)
