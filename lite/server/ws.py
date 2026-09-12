"""实时协同 WebSocket(构建文档 §8)。

端点 /ws/docs/{doc_id};Cookie 认证;语义 LWW(后写者赢)+ 编辑保护提示。

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

from auth import ROLE_RANK, avatar_color, project_role
from docs import save_doc_content
from models import AuthSession, Doc, SessionLocal, User, utcnow

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


async def _broadcast(doc_id: int, message: dict, exclude: dict | None = None):
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


async def _broadcast_presence(doc_id: int):
    users = [{"userId": c["userId"], "name": c["name"], "editing": c["editing"],
              "avatarColor": c["avatarColor"]} for c in POOL.get(doc_id, [])]
    await _broadcast(doc_id, {"type": "presence", "users": users})


def _access(db, token: str | None, doc_id: int):
    """取当前用户与文档并确认仍可访问;返回 (user, doc, role)。

    握手与每条消息都走这里:会话失效抛 4401、文档被删抛 4404、不再是成员抛 4403。
    """
    sess = db.get(AuthSession, token) if token else None
    user = db.get(User, sess.user_id) if sess and sess.expires_at > utcnow() else None
    if not user or user.is_disabled:
        raise _WsClose(4401)
    doc = db.get(Doc, doc_id)
    if not doc or doc.deleted_at is not None:
        raise _WsClose(4404)
    role = project_role(db, doc.project_id, user)
    if role is None:
        # 文档未定义非成员连接的关闭码,取 4403
        raise _WsClose(4403)
    return user, doc, role


def _handshake(token: str | None, doc_id: int) -> dict:
    """Worker 线程:握手鉴权,返回连接信息(§8 从 Cookie 读会话)。"""
    with SessionLocal() as db:
        user, _doc, role = _access(db, token, doc_id)
        return {"userId": user.id, "name": user.name,
                "avatarColor": avatar_color(user.id),
                "editing": False,
                "readonly": ROLE_RANK.get(role, -1) < ROLE_RANK["EDITOR"]}


def _save_content(token: str | None, doc_id: int, content: str):
    """Worker 线程:保存一条内容(含逐条权限复查),返回 (version, changed, 用户名)。"""
    with SessionLocal() as db:
        user, doc, role = _access(db, token, doc_id)
        if ROLE_RANK.get(role, -1) < ROLE_RANK["EDITOR"]:
            raise _WsClose(4403)
        # 版本快照与保留策略与 REST 共用同一实现(docs.save_doc_content)
        changed, version = save_doc_content(db, doc, content, user.id, label="自动")
        db.commit()
        return version, changed, user.name


@router.websocket("/ws/docs/{doc_id}")
async def doc_ws(websocket: WebSocket, doc_id: int):
    await websocket.accept()
    conn = None
    try:
        token = websocket.cookies.get("td_sid")
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
                content = msg.get("content")
                if not isinstance(content, str):
                    continue
                try:
                    version, changed, by_name = await run_in_threadpool(
                        _save_content, token, doc_id, content)
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
