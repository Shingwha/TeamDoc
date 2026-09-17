"""回收站:三类资源(文档/文件夹/文件)的软删除、恢复、彻底删除,以及回收站列表。

删除 / 恢复 / 彻底删除三者**对称,均作用于整棵子树**(文件夹与文档;文件是单条
资源,等价于单元素子树),单事务完成 —— 一个树引擎 + 薄端点,不让每个端点各写一份
子树遍历。

次序约定(物理文件清理):先删记录、提交,再 unlink_quiet —— 失败不回滚,
残留由管理后台孤儿清理兜底。反过来(先删文件再删记录)一旦提交失败,
就变成"文件没了但记录还在"。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, pat_write_guard, require_project_role, require_role)
from errors import CONFLICT, err
from ids import IdPath
from models import (Doc, DocVersion, File, Folder, collect_subtree,
                    file_abspath, get_db, next_sort, unlink_quiet, utcnow)
from serialize import file_json, iso

router = APIRouter(dependencies=[Depends(pat_write_guard)])

# 权限依赖:三种资源共用一个工厂(auth.require_role)
DOC_EDIT = require_role(Doc, "文档", "EDITOR", path_param="doc_id")
DOC_TRASH = require_role(Doc, "文档", "EDITOR", path_param="doc_id", for_trash=True)
FOLDER_EDIT = require_role(Folder, "文件夹", "EDITOR", path_param="folder_id")
FOLDER_TRASH = require_role(Folder, "文件夹", "EDITOR", path_param="folder_id", for_trash=True)
FILE_EDIT = require_role(File, "文件", "EDITOR", path_param="file_id")
FILE_TRASH = require_role(File, "文件", "EDITOR", path_param="file_id", for_trash=True)


def _result(kind: str, count: int) -> dict:
    """回收站 9 个变更端点的统一响应形状。

    三种资源、三种操作的响应必须同形:调用方(前端提示、CLI、脚本)才能只有一套处理,
    而不是按端点分支去猜"这次影响了几个"。
    """
    return {"ok": True, "kind": kind, "count": count}


# ---------- 树引擎(Doc 与 Folder 同构:id / parent_id / project_id) ----------

def soft_delete_tree(db: DbSession, model, root) -> tuple:
    """整棵子树软删除;返回 (主资源数, 连带文件数)。文件夹会把子树内文件一起删。"""
    ids = collect_subtree(db, model, root)
    now = utcnow()
    main = (db.query(model).filter(model.id.in_(ids), model.deleted_at.is_(None))
            .update({"deleted_at": now}, synchronize_session=False))
    files = 0
    if model is Folder:
        files = (db.query(File).filter(File.folder_id.in_(ids), File.deleted_at.is_(None))
                 .update({"deleted_at": now}, synchronize_session=False))
    db.commit()
    return main, files


def restore_tree(db: DbSession, model, root) -> tuple:
    """整棵子树恢复(先前单独删掉的子项会一起回来,与删除语义对称);
    父级仍在回收站(或已不存在、或已不在本项目)时回落到项目根,否则恢复出来的东西
    仍然挂在看不见的位置。返回 (主资源数, 连带文件数)。"""
    ids = collect_subtree(db, model, root)
    main = (db.query(model).filter(model.id.in_(ids), model.deleted_at.isnot(None))
            .update({"deleted_at": None}, synchronize_session=False))
    files = 0
    if model is Folder:
        files = (db.query(File).filter(File.folder_id.in_(ids), File.deleted_at.isnot(None))
                 .update({"deleted_at": None}, synchronize_session=False))
    # 父级回落用 update 而非改 ORM 属性:root 对象的状态已被上面的批量语句改过,
    # 直接赋属性会把陈旧的 deleted_at 一起写回去。判据收敛在 _parent_gone,
    # 与单资源路径(restore_file)共用同一份"父级不可用"
    if root.parent_id and root.parent_id not in set(ids) \
            and _parent_gone(db, model, root.parent_id, root.project_id):
        # 回落到项目根,并落到根层末尾(文档有显式顺序,文件夹按名称排,无 sort 列)
        patch = {"parent_id": None}
        if model is Doc:
            patch["sort"] = next_sort(db, model, root.project_id, None)
        db.query(model).filter(model.id == root.id).update(patch, synchronize_session=False)
    db.commit()
    return main, files


def commit_and_unlink(db: DbSession, paths) -> None:
    """物理文件清理的统一次序:先提交记录删除,再逐个 unlink(失败不回滚)。
    彻底删除文件夹/文件与删项目共用。"""
    db.commit()
    for p in paths:
        unlink_quiet(p)


def _require_in_trash(obj, kind: str) -> None:
    """恢复 / 彻底删除的 409 前置:只作用于回收站中的资源(状态冲突,不是权限)。"""
    if obj.deleted_at is None:
        err(409, CONFLICT, f"{kind}不在回收站")


def _parent_gone(db: DbSession, model, parent_id, project_id) -> bool:
    """父级不可用(不存在 / 仍在回收站 / 已不在本项目)—— 恢复时回落到项目根的判据。
    否则恢复出来的东西挂在看不见的位置,等于没恢复。

    跨项目这条是移动语义的配套:文档可以被搬到别的项目,而回收站里的孩子不随迁;
    恢复时若父级已在别处,挂上去就是一条跨项目的父链(树渲染会把它当成根,但
    location 路径与权限判定会各错一半)。
    """
    if not parent_id:
        return False
    parent = db.get(model, parent_id)
    return (parent is None or parent.deleted_at is not None
            or parent.project_id != project_id)


# ---------- 文档 ----------

@router.delete("/api/docs/{doc_id}")
def delete_doc(doc_id: IdPath, dep=Depends(DOC_EDIT), db: DbSession = Depends(get_db)):
    _, doc = dep
    removed, _files = soft_delete_tree(db, Doc, doc)
    return _result("doc", removed)


@router.post("/api/docs/{doc_id}/restore")
def restore_doc(doc_id: IdPath, dep=Depends(DOC_TRASH), db: DbSession = Depends(get_db)):
    _, doc = dep
    _require_in_trash(doc, "文档")
    restored, _files = restore_tree(db, Doc, doc)
    return _result("doc", restored)


@router.delete("/api/docs/{doc_id}/permanent")
def permanent_delete_doc(doc_id: IdPath, dep=Depends(DOC_TRASH),
                         db: DbSession = Depends(get_db)):
    """彻底删除:仅回收站中的文档可删;连同子树与历史版本一起清除"""
    _, doc = dep
    _require_in_trash(doc, "文档")
    ids = collect_subtree(db, Doc, doc)
    db.query(DocVersion).filter(DocVersion.doc_id.in_(ids)).delete(synchronize_session=False)
    db.query(Doc).filter(Doc.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return _result("doc", len(ids))


# ---------- 文件夹 ----------

@router.delete("/api/files/folders/{folder_id}")
def delete_folder(folder_id: IdPath, dep=Depends(FOLDER_EDIT),
                  db: DbSession = Depends(get_db)):
    """删除文件夹 = **递归软删除整棵子树**(文件夹 + 后代文件夹 + 文件)。

    一次整棵软删除,与文档删除的语义对齐;因为是软删除,所以完全可恢复。
    回收站只列**子树根**(见 project_trash),不会因为删一个目录而多出几十条子项。
    """
    _, folder = dep
    folders, files = soft_delete_tree(db, Folder, folder)
    return _result("folder", folders + files)


@router.post("/api/files/folders/{folder_id}/restore")
def restore_folder(folder_id: IdPath, dep=Depends(FOLDER_TRASH),
                   db: DbSession = Depends(get_db)):
    """从回收站恢复文件夹 = **整棵子树**(与删除对称);父级回落语义见 restore_tree。"""
    _, folder = dep
    _require_in_trash(folder, "文件夹")
    folders, files = restore_tree(db, Folder, folder)
    return _result("folder", folders + files)


@router.delete("/api/files/folders/{folder_id}/permanent")
def permanent_delete_folder(folder_id: IdPath, dep=Depends(FOLDER_TRASH),
                            db: DbSession = Depends(get_db)):
    """彻底删除文件夹:仅回收站中的可删;连同子树内所有文件夹与文件一起清除(含物理文件)。

    由于删除即整棵软删除(见 delete_folder),回收站里一个文件夹的子树必然整体处于
    已删除状态;这里仍按"全部后代"清除,不区分删除状态 —— 用户要彻底删掉这个目录,
    目录里的东西自然一并消失。
    """
    _, folder = dep
    _require_in_trash(folder, "文件夹")
    ids = collect_subtree(db, Folder, folder)
    files = db.query(File).filter(File.folder_id.in_(ids)).all()
    paths = [file_abspath(f.storage_path) for f in files]
    for f in files:
        db.delete(f)
    db.query(Folder).filter(Folder.id.in_(ids)).delete(synchronize_session=False)
    commit_and_unlink(db, paths)
    return _result("folder", len(ids) + len(files))


# ---------- 文件(单条资源,无子树) ----------

@router.delete("/api/files/{file_id}")
def delete_file(file_id: IdPath, dep=Depends(FILE_EDIT), db: DbSession = Depends(get_db)):
    _, f = dep
    f.deleted_at = utcnow()
    db.commit()
    return _result("file", 1)


@router.post("/api/files/{file_id}/restore")
def restore_file(file_id: IdPath, dep=Depends(FILE_TRASH), db: DbSession = Depends(get_db)):
    """从回收站恢复;所在文件夹也被删(或已不存在)时回落到项目根目录(对齐文档恢复语义)"""
    _, f = dep
    _require_in_trash(f, "文件")
    if _parent_gone(db, Folder, f.folder_id, f.project_id):
        f.folder_id = None
    f.deleted_at = None
    db.commit()
    return _result("file", 1)


@router.delete("/api/files/{file_id}/permanent")
def permanent_delete_file(file_id: IdPath, dep=Depends(FILE_TRASH),
                          db: DbSession = Depends(get_db)):
    """彻底删除:仅回收站中的文件可删;清记录并删除物理文件"""
    _, f = dep
    _require_in_trash(f, "文件")
    path = file_abspath(f.storage_path)
    db.delete(f)
    commit_and_unlink(db, [path])
    return _result("file", 1)


# ---------- 回收站列表 ----------

@router.get("/api/projects/{project_id}/trash")
def project_trash(project_id: IdPath,
                  ctx: AuthContext = Depends(require_project_role("VIEWER")),
                  db: DbSession = Depends(get_db)):
    """项目回收站:文档 + 云空间文件 + 文件夹统一返回(均按删除时间倒序)。

    **只列"子树根"**:删除文件夹/文档时整棵子树都被软删除,若把子项也列出来,
    删一个目录会让回收站一次多出几十条,而它们本就该随根一起恢复(恢复是递归的)。
    判据:父级未删除、或父级不存在(父级被彻底删掉后子项已在同一事务中清除)。

    注意 500 条上限作用在**过滤前**的原始集合上:极端情况下(单项目回收站里
    超过 500 个已删项)可能少列一些根。

    非成员进不来:VIEWER 依赖已经保证来者是真成员或全局管理员 —— 回收站含
    "删了什么"这类项目内部信息,不对外开放。
    """
    docs = (db.query(Doc).filter_by(project_id=project_id).filter(Doc.deleted_at.isnot(None))
            .order_by(Doc.deleted_at.desc()).limit(500).all())
    files = (db.query(File).filter_by(project_id=project_id).filter(File.deleted_at.isnot(None))
             .order_by(File.deleted_at.desc()).limit(500).all())
    folders = (db.query(Folder).filter_by(project_id=project_id).filter(Folder.deleted_at.isnot(None))
               .order_by(Folder.deleted_at.desc()).limit(500).all())
    # 已删文档 id(判断某文档的父级是否也在回收站)
    deleted_doc_ids = {d.id for d in docs}
    docs = [d for d in docs if not d.parent_id or d.parent_id not in deleted_doc_ids]
    # 已删文件夹 id:一次取全(不带上限),否则父级不在前 500 条时会被误判成根
    deleted_folder_ids = {fid for (fid,) in db.query(Folder.id)
                          .filter_by(project_id=project_id)
                          .filter(Folder.deleted_at.isnot(None)).all()}
    folders = [f for f in folders if not f.parent_id or f.parent_id not in deleted_folder_ids]
    files = [f for f in files if not f.folder_id or f.folder_id not in deleted_folder_ids]
    return {
        "docs": [{"id": d.id, "title": d.title, "deletedAt": iso(d.deleted_at)} for d in docs],
        "files": [{**file_json(f), "deletedAt": iso(f.deleted_at)} for f in files],
        "folders": [{"id": f.id, "name": f.name, "deletedAt": iso(f.deleted_at)}
                    for f in folders],
    }
