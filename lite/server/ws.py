"""实时协同 WebSocket(构建文档 §8)。

端点 /ws/docs/{doc_id};Cookie 认证;LWW(后写者赢)+ **基线校验** + 编辑保护提示。

基线校验(见 docs.save_doc_content):保存消息必须带 baseVersion,与服务端当前版本
不一致就回 conflict 给发送者本人 —— 不写库、不回 saved、也不向其他人广播。LWW 本身
保留("谁最后保存谁的内容在库里"),但它不再是一条静默路径:被覆盖的一方当场被告知,
由人决定是采纳远端还是保留自己这份。

**所有数据库操作都在 worker 线程里做**(run_in_threadpool)。本模块的处理器是
async、跑在事件循环上,而 SQLAlchemy 是同步的:直接在这里查库,一旦数据库卡顿
(等写锁、等磁盘 I/O),冻结的是**整个进程** —— HTTP、WebSocket、静态页一起无响应,
控制台却没有任何输出。放进线程池后,一次慢查询只影响自己这条连接。

数据库函数一律自建短会话、用完即关,并且**每条消息都重新复核权限**:WS 是长连接,
REST 的每请求校验覆盖不到它 —— 连接建立后管理员可能禁用该用户、把他移出项目或降为
VIEWER,不复查的话他仍能通过已开的连接持续写入文档。
"""
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from auth import SESSION_COOKIE, has_role, project_role, session_context
from docs import ContentConflict, save_doc_content, str_content
from ids import IdPath
from models import Doc, SessionLocal, is_live
from serialize import avatar_color

logger = logging.getLogger("teamdoc.ws")

router = APIRouter()

# doc_id -> [{ws, userId, name, avatarColor, editing, readonly}]
POOL: dict[str, list[dict]] = {}


class _WsClose(Exception):
    """要求以指定关闭码结束连接(会话失效 4401 / 无权限 4403 / 文档没了 4404)。

    用异常而不是返回值:数据库函数就能按"先复核、再干活"的顺序平铺直叙,
    不必把每个失败分支都编码进返回类型。
    """

    def __init__(self, code: int):
        super().__init__(str(code))
        self.code = code


async def _broadcast(doc_id: str, message: dict, exclude: dict | None = None):
    conns = POOL.get(doc_id, [])
    dead = []
    for conn in conns:
        if exclude is not None and conn is exclude:
            continue
        try:
            await conn["ws"].send_json(message)
        except Exception:
            dead.append(conn)
    for conn in dead:
        if conn in conns:
            conns.remove(conn)


async def _broadcast_presence(doc_id: str):
    users = [{"userId": c["userId"], "name": c["name"], "editing": c["editing"],
              "avatarColor": c["avatarColor"]} for c in POOL.get(doc_id, [])]
    await _broadcast(doc_id, {"type": "presence", "users": users})


def _access(db, token: str | None, doc_id: str):
    """取当前用户与文档并确认仍可访问;返回 (user, doc, role)。

    握手与每条消息都走这里:会话失效抛 4401、文档被删抛 4404、不再是成员抛 4403。
    会话判定复用 auth.session_context(含滑动续期),不另写一份。
    """
    ctx = session_context(db, token)
    if ctx is None:
        raise _WsClose(4401)
    doc = db.get(Doc, doc_id)
    if not is_live(doc):
        raise _WsClose(4404)
    role = project_role(db, doc.project_id, ctx.user)
    if role is None:
        raise _WsClose(4403)
    return ctx.user, doc, role


def _handshake(token: str | None, doc_id: str) -> dict:
    """Worker 线程:握手鉴权,返回连接信息(§8 从 Cookie 读会话)。"""
    with SessionLocal() as db:
        user, _doc, role = _access(db, token, doc_id)
        return {"userId": user.id, "name": user.name,
                "avatarColor": avatar_color(user.id),
                "editing": False,
                "readonly": not has_role(role, "EDITOR")}


def _save_content(token: str | None, doc_id: str, content: str, base_version: int):
    """Worker 线程:保存一条内容(含逐条权限复查),返回 (version, changed, 用户名)。

    基线不符时由 save_doc_content 抛 ContentConflict,原样冒泡给调用方映射成消息。
    """
    with SessionLocal() as db:
        user, doc, role = _access(db, token, doc_id)
        if not has_role(role, "EDITOR"):
            raise _WsClose(4403)
        # 版本快照与保留策略与 REST 共用同一实现(docs.save_doc_content);
        # WS 与 REST 只是传输方式,不是两种成因,所以 kind 用默认的"save"
        changed, version = save_doc_content(db, doc, content, user.id,
                                            base_version=base_version)
        db.commit()
        return version, changed, user.name


@router.websocket("/ws/docs/{doc_id}")
async def doc_ws(websocket: WebSocket, doc_id: IdPath):
    await websocket.accept()
    conn = None
    try:
        token = websocket.cookies.get(SESSION_COOKIE)
        try:
            info = await run_in_threadpool(_handshake, token, doc_id)
        except _WsClose as exc:
            await websocket.close(code=exc.code)
            return
        conn = {"ws": websocket, **info}
        POOL.setdefault(doc_id, []).append(conn)
        logger.info("WS 建立 doc=%s user=%s", doc_id, conn["userId"])
        # 连接一注册服务端就推 presence:客户端据此判断"这条连接真的被接纳了"
        # (被鉴权/权限拒绝的连接收不到它,前端因此不会无脑重连)
        await _broadcast_presence(doc_id)

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            mtype = msg.get("type")
            if mtype == "presence":
                conn["editing"] = bool(msg.get("editing"))
                await _broadcast_presence(doc_id)
            elif mtype == "content":
                if conn["readonly"]:
                    continue  # readonly 连接直接忽略(§8)
                content = str_content(msg)
                if content is None:
                    continue
                # baseVersion 缺失/非法一律不写:不接受"没声明基线"的整篇覆盖。
                # 该保存明确失败(用户刷新页面即可),而不是无声地盖掉别人刚写的内容
                base_version = msg.get("baseVersion")
                if isinstance(base_version, bool) or not isinstance(base_version, int):
                    logger.warning("WS 非法保存消息(缺 baseVersion) doc=%s user=%s",
                                   doc_id, conn["userId"])
                    continue
                try:
                    version, changed, by_name = await run_in_threadpool(
                        _save_content, token, doc_id, content, base_version)
                except ContentConflict as c:
                    # 只回发送者:其他人根本没被改动,收到 conflict 只会莫名其妙。
                    # 用 continue 而不是关闭连接 —— 冲突是可恢复的正常状态
                    logger.info("WS 冲突 doc=%s user=%s base=%s current=%s",
                                doc_id, conn["userId"], base_version, c.version)
                    # 冲突现场与 REST 409 detail 同形(currentVersion/currentContent/by):
                    # "服务端冲突现场"是一个契约,两个传输层共用同一形状,前端无需归一
                    await websocket.send_json({"type": "conflict", "currentVersion": c.version,
                                               "currentContent": c.content, "by": c.by_name})
                    continue
                except _WsClose as exc:
                    logger.info("WS 关闭 doc=%s user=%s code=%s",
                                doc_id, conn["userId"], exc.code)
                    await websocket.close(code=exc.code)
                    return
                await websocket.send_json({"type": "saved", "version": version})
                if changed:
                    await _broadcast(doc_id, {"type": "remote", "content": content,
                                              "version": version, "by": by_name},
                                     exclude=conn)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # 一行一条(不展开栈):重连风暴时栈会把日志刷爆,而异常类型本身就是
        # "服务端是否在批量踢连接"的关键线索
        logger.warning("WS 异常 doc=%s user=%s: %s: %s", doc_id,
                       conn["userId"] if conn else None, type(exc).__name__, exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if conn is not None:
            conns = POOL.get(doc_id, [])
            if conn in conns:
                conns.remove(conn)
            if not conns:
                POOL.pop(doc_id, None)
            else:
                await _broadcast_presence(doc_id)
            logger.info("WS 断开 doc=%s user=%s", doc_id, conn["userId"])
