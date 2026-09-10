"""实时协同 WebSocket(构建文档 §8)。

端点 /ws/docs/{doc_id};Cookie 认证;语义 LWW(后写者赢)+ 编辑保护提示。
"""
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from auth import ROLE_RANK, project_role
from docs import VERSION_KEEP
from models import AuthSession, Doc, DocVersion, SessionLocal, User, utcnow

router = APIRouter()

# doc_id -> [{ws, userId, name, editing, readonly}]
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
    users = [{"userId": c["userId"], "name": c["name"], "editing": c["editing"]}
             for c in POOL.get(doc_id, [])]
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
        # TOTP 门禁与 REST 一致(文档未定义 WS 行为,按最简一致处理)
        if user.totp_enabled and not sess.totp_verified:
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
                if content == doc.content:
                    await websocket.send_json({"type": "saved", "version": doc.version})
                    continue
                # 旧内容存 DocVersion(label="自动"),版本只留 50 条;提交数据库
                db.add(DocVersion(doc_id=doc.id, content=doc.content, label="自动",
                                  created_by=user.id))
                doc.content = content
                doc.version += 1
                doc.updated_by = user.id
                doc.updated_at = utcnow()
                db.flush()
                ids = [v.id for v in db.query(DocVersion.id).filter_by(doc_id=doc.id)
                       .order_by(DocVersion.created_at.desc(), DocVersion.id.desc()).all()]
                if len(ids) > VERSION_KEEP:
                    db.query(DocVersion).filter(DocVersion.id.in_(ids[VERSION_KEEP:])) \
                        .delete(synchronize_session=False)
                db.commit()
                await websocket.send_json({"type": "saved", "version": doc.version})
                await _broadcast(doc_id, {"type": "remote", "content": content,
                                          "version": doc.version, "by": user.name},
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
