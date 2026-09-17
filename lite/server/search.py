"""搜索与最近动态(构建文档 §7.6):SQLite LIKE(标题 + 正文 / 文件名),权限过滤。

可见集合走 projects.visible_project_ids(单一来源):搜索 = 我参加的项目
(+ 管理员的全量);公开项目**不在其中** —— 公开只意味着"可发现 + 可自助加入",
加入前不可读,自然也就搜不到。

**"最近动态"就是 q 为空的搜索**,不是另一个端点:两者的行构造、可见性过滤、
项目名解析完全相同,差别只有筛选与上限(见 search 的 docstring)。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session as DbSession

from auth import AuthContext, clamp_int, current_user, pat_write_guard
from errors import bad_request
from models import Doc, File, Project, get_db
from projects import visible_project_ids
from serialize import file_json, iso

router = APIRouter(dependencies=[Depends(pat_write_guard)])

TYPES = ("all", "docs", "files")


def _snippet(content: str, q: str) -> str:
    """content 中首次命中位置前后各 60 字符拼接;正文未命中(标题命中)时取前 120 字符"""
    idx = content.find(q)
    if idx < 0:
        return content[:120]
    start = max(0, idx - 60)
    return content[start:idx + len(q) + 60]


def _doc_row(d: Doc, project_name: str, snippet: str = "") -> dict:
    """文档结果行。两种模式同形 —— 前端一套渲染代码,不必按来源分支。"""
    return {"id": d.id, "projectId": d.project_id, "title": d.title,
            "projectName": project_name, "updatedAt": iso(d.updated_at),
            "snippet": snippet}


def _file_row(f: File, project_name: str) -> dict:
    """文件结果行。projectId/folderId/projectName 是"它在哪里"的上下文,不是文件的
    属性 —— 搜索结果与最近动态都要它,否则用户知道了文件名也不知道去哪找。"""
    return {**file_json(f), "projectId": f.project_id, "folderId": f.folder_id,
            "projectName": project_name}


@router.get("/api/search")
def search(q: str = "", type: str = "all", limit: int = 0,
           ctx: AuthContext = Depends(current_user), db: DbSession = Depends(get_db)):
    """搜索;`q` 为空时退化为**最近动态**(最近改动的文档与最近上传的文件)。

    最近动态这一档存在的理由:"我昨天传的那个东西在哪"是最常见的找文件场景,
    而按项目浏览要求用户先记起它在哪个项目 —— NAS 式使用下他往往根本不记得。

    两种模式共用行构造与可见性过滤,差别只有三点:

    * 可见范围:最近动态是"我的工作台"视角,**连管理员也只看自己参加的项目**
      (要看全量内容走搜索/项目列表/管理后台);搜索给管理员并入全站非个人项目
      (接管失联项目时要搜得到)。
    * 筛选与排序:最近按时间倒序(文档按更新时间、文件按创建时间);搜索按 LIKE 命中。
    * 上限:最近 100,搜索 500。
    """
    q = (q or "").strip()
    if type not in TYPES:
        bad_request(f"type 必须为 {'/'.join(TYPES)}")
    recent_mode = not q
    if recent_mode:
        limit = clamp_int(limit or 20, 1, 100, "limit")
        visible = visible_project_ids(db, ctx.user)
    else:
        limit = clamp_int(limit or 100, 1, 500, "limit")
        visible = visible_project_ids(db, ctx.user, site_wide=True)
    if not visible:
        return {"docs": [], "files": []}
    names = {p.id: p.name for p in db.query(Project).filter(Project.id.in_(visible)).all()}
    like = f"%{q}%"
    docs_out, files_out = [], []
    if type in ("all", "docs"):
        qy = db.query(Doc).filter(Doc.deleted_at.is_(None), Doc.project_id.in_(visible))
        if not recent_mode:
            qy = qy.filter(or_(Doc.title.like(like), Doc.content.like(like)))
        for d in qy.order_by(Doc.updated_at.desc()).limit(limit).all():
            docs_out.append(_doc_row(d, names.get(d.project_id, ""),
                                     "" if recent_mode else _snippet(d.content, q)))
    if type in ("all", "files"):
        qy = db.query(File).filter(File.deleted_at.is_(None), File.project_id.in_(visible))
        if not recent_mode:
            qy = qy.filter(File.name.like(like))
        for f in qy.order_by(File.created_at.desc()).limit(limit).all():
            files_out.append(_file_row(f, names.get(f.project_id, "")))
    return {"docs": docs_out, "files": files_out}
