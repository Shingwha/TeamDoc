"""全文搜索(构建文档 §7.6):SQLite LIKE(标题 + 正文 / 文件名),权限过滤。

可见集合走 projects.visible_project_ids(单一来源):搜索 = 我参加的项目
(+ 管理员的全量)。公开项目**不在其中** —— 公开只意味着"可发现 + 可自助加入",
加入前不可读,自然也就搜不到。文件序列化复用 files.file_json —— mime 在
启动时已回填为服务端口径(schema.normalize_mimes),读时不再重算。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session as DbSession

from auth import AuthContext, current_user, pat_write_guard
from files import file_json
from models import Doc, File, Project, get_db
from projects import visible_project_ids

router = APIRouter(dependencies=[Depends(pat_write_guard)])


def _snippet(content: str, q: str) -> str:
    """content 中首次命中位置前后各 60 字符拼接;正文未命中(标题命中)时取前 120 字符"""
    idx = content.find(q)
    if idx < 0:
        return content[:120]
    start = max(0, idx - 60)
    return content[start:idx + len(q) + 60]


@router.get("/api/recent")
def recent(limit: int = 20, ctx: AuthContext = Depends(current_user),
           db: DbSession = Depends(get_db)):
    """最近的文件与文档(仅我参与的项目,零新表)。

    存在的理由:"我昨天传的那个东西在哪"是最常见的找文件场景,而按项目浏览要求
    用户先记起它在哪个项目 —— 而在 NAS 式使用下,用户往往根本不记得。

    可见范围 = 我已参加的项目(个人项目天然计入,创建者有成员行)。
    与 /api/search 唯一的差别是管理员:动态是"我的工作台"视角,连管理员也只看
    自己参与的项目(要看全量内容走项目列表/搜索/管理后台)。
    消费方:发现广场「最近动态」与搜索页空态(后者本就是个人召回场景)。
    """
    limit = max(1, min(int(limit or 20), 100))
    joined = visible_project_ids(db, ctx.user)
    if not joined:
        return {"files": [], "docs": []}
    names = {p.id: p.name for p in db.query(Project).filter(Project.id.in_(joined)).all()}
    files = (db.query(File).filter(File.deleted_at.is_(None), File.project_id.in_(joined))
             .order_by(File.created_at.desc()).limit(limit).all())
    docs = (db.query(Doc).filter(Doc.deleted_at.is_(None), Doc.project_id.in_(joined))
            .order_by(Doc.updated_at.desc()).limit(limit).all())
    return {
        "files": [{**file_json(f), "projectId": f.project_id,
                   "projectName": names.get(f.project_id, ""), "folderId": f.folder_id}
                  for f in files],
        "docs": [{"id": d.id, "title": d.title, "projectId": d.project_id,
                  "projectName": names.get(d.project_id, ""),
                  "updatedAt": d.updated_at.isoformat()} for d in docs],
    }


@router.get("/api/search")
def search(q: str = "", type: str = "all",
           ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    q = (q or "").strip()
    if not q:
        return {"docs": [], "files": []}
    if type not in ("all", "docs", "files"):
        type = "all"
    like = f"%{q}%"
    # 可见项目 = 我参加的(含个人),管理员再并入全量非个人(visible_project_ids 单一来源)。
    # 公开项目不在其中:加入前不可读,自然也搜不到
    visible = visible_project_ids(db, ctx.user, site_wide=True)
    docs_out, files_out = [], []
    # 结果里带项目名(前端要显示"文件在哪个项目",否则用户无从定位)
    names = {p.id: p.name for p in db.query(Project).filter(Project.id.in_(visible)).all()}         if visible else {}
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
                files_out.append({**file_json(f), "projectId": f.project_id,
                                  "folderId": f.folder_id,
                                  "projectName": names.get(f.project_id, "")})
    return {"docs": docs_out, "files": files_out}
