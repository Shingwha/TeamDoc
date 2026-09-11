"""实时协同 WebSocket(构建文档 §8)。

端点 /ws/docs/{doc_id};Cookie 认证;语义 LWW(后写者赢)+ 编辑保护提示。
"""
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from auth import ROLE_RANK, avatar_color, project_role
from docs import save_doc_content
from models import AuthSession, Doc, SessionLocal, User, utcnow

router = APIRouter()

# doc_id -> [{ws, userId, name, avatarColor, editing, readonly}]
POOL: dict[str, list[dict]] = {}


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


@router.websocket("/ws/docs/{doc_id}")
async def doc_ws(websocket: WebSocket, doc_id: str):
    await websocket.accept()
    db = SessionLocal()
    conn = None
    try:
        # 1. 认证:服务端从 websocket.cookies 读取会话(§8)
        token = websocket.cookies.get("td_sid")
        sess = db.get(AuthSession, token) if token else None
        user = db.get(User, sess.user_id) if sess and sess.expires_at > utcnow() else None
        if not user or user.is_disabled:
            await websocket.close(code=4401)
            return
        doc = db.get(Doc, doc_id)
        if not doc or doc.deleted_at is not None:
            await websocket.close(code=4404)
            return
        role = project_role(db, doc.project_id, user)
        if role is None:
            # 文档未定义非成员连接的关闭码,取 4403
            await websocket.close(code=4403)
            return
        readonly = ROLE_RANK.get(role, -1) < ROLE_RANK["EDITOR"]
        conn = {"ws": websocket, "userId": user.id, "name": user.name,
                "avatarColor": avatar_color(user.id),
                "editing": False, "readonly": readonly}
        POOL.setdefault(doc_id, []).append(conn)
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
                # 每条消息独立短事务,重取文档避免脏读
                db.expire_all()
                doc = db.get(Doc, doc_id)
                if not doc or doc.deleted_at is not None:
                    await websocket.close(code=4404)
                    return
                # **逐条复查权限**:只握手时校验是不够的 —— 连接建立后管理员可能
                # 禁用该用户、把他移出项目、或把角色从 EDITOR 降为 VIEWER,
                # 而 WS 是长连接,REST 的每请求校验覆盖不到它。
                # 不复查的话,被禁用/降权的人仍能通过已开的连接持续写入文档。
                sess = db.get(AuthSession, websocket.cookies.get("td_sid"))
                if not sess or sess.expires_at <= utcnow():
                    await websocket.close(code=4401)
                    return
                user = db.get(User, sess.user_id)
                if not user or user.is_disabled:
                    await websocket.close(code=4401)
                    return
                role = project_role(db, doc.project_id, user)
                if role is None or ROLE_RANK.get(role, -1) < ROLE_RANK["EDITOR"]:
                    await websocket.close(code=4403)
                    return
                # 版本快照与保留策略与 REST 共用同一实现(docs.save_doc_content)
                changed, version = save_doc_content(db, doc, content, user.id, label="自动")
                db.commit()
                await websocket.send_json({"type": "saved", "version": version})
                if changed:
                    await _broadcast(doc_id, {"type": "remote", "content": content,
                                              "version": version, "by": user.name},
                                     exclude=conn)
    except WebSocketDisconnect:
        pass
    except Exception:
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
        db.close()
