"""全文搜索(构建文档 §7.6):SQLite LIKE(标题 + 正文 / 文件名),权限过滤。

需求变更:docs/files 都只搜"我是成员的项目";管理员额外覆盖全部非个人项目
(不搜他人的个人项目)。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session as DbSession

from auth import AuthContext, current_user
from files import can_inline, guess_mime
from models import Doc, File, Project, ProjectMember, get_db

router = APIRouter()


def _snippet(content: str, q: str) -> str:
    """content 中首次命中位置前后各 60 字符拼接;正文未命中(标题命中)时取前 120 字符"""
    idx = content.find(q)
    if idx < 0:
        return content[:120]
    start = max(0, idx - 60)
    return content[start:idx + len(q) + 60]


@router.get("/api/search")
def search(q: str = "", type: str = "all",
           ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    q = (q or "").strip()
    if not q:
        return {"docs": [], "files": []}
    if type not in ("all", "docs", "files"):
        type = "all"
    like = f"%{q}%"
    # 可见项目 = 我是成员的项目(含自己的个人项目)
    visible = {m.project_id for m in
               db.query(ProjectMember).filter_by(user_id=ctx.user.id).all()}
    if ctx.user.is_admin:
        # 管理员额外覆盖全部非个人项目(不搜他人个人项目)
        visible |= {p.id for p in db.query(Project.id).filter(Project.is_personal.is_(False)).all()}
    docs_out, files_out = [], []
    # 结果里带项目名(前端要显示"文件在哪个项目",否则用户无从定位)
    names = {p.id: p.name for p in db.query(Project).filter(Project.id.in_(visible)).all()} \
        if visible else {}
    if visible:
        if type in ("all", "docs"):
            qy = (db.query(Doc).filter(Doc.deleted_at.is_(None))
                  .filter(Doc.project_id.in_(visible))
                  .filter(or_(Doc.title.like(like), Doc.content.like(like))))
            for d in qy.order_by(Doc.updated_at.desc()).limit(500).all():
                docs_out.append({"id": d.id, "projectId": d.project_id, "title": d.title,
                                 "projectName": names.get(d.project_id, ""),
                                 "snippet": _snippet(d.content, q)})
        if type in ("all", "files"):
            qy = (db.query(File).filter(File.deleted_at.is_(None))
                  .filter(File.project_id.in_(visible))
                  .filter(File.name.like(like)))
            for f in qy.order_by(File.created_at.desc()).limit(500).all():
                # mime 走服务端判定(存量记录里的旧值是客户端声明的,不可信)
                mime = guess_mime(f.name)
                files_out.append({"id": f.id, "name": f.name, "projectId": f.project_id,
                                  "mime": mime, "size": f.size,
                                  "canInline": can_inline(mime),
                                  "folderId": f.folder_id,
                                  "projectName": names.get(f.project_id, "")})
    return {"docs": docs_out, "files": files_out}
