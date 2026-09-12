"""回收站:三类资源(文档/文件夹/文件)的软删除、恢复、彻底删除,以及回收站列表。

删除/恢复/彻底删除三者**对称,均作用于整棵子树**(文件夹/文档;文件是单条资源,
等价于单元素子树);单事务完成。此前这套流程散在 docs.py 与 files.py 各写一份
(9 个端点、两份逐字相同的子树 DFS),现收敛为一份树引擎 + 薄端点。

次序约定(物理文件清理):先删记录、提交,再 unlink_quiet —— 失败不回滚,
残留由管理后台孤儿清理兜底。反过来(先删文件再删记录)一旦提交失败,
就变成"文件没了但记录还在"。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from auth import (AuthContext, err, is_project_member, pat_write_guard,
                  require_doc_role, require_file_role, require_folder_role,
                  require_project_role)
from files import file_json
from models import (Doc, DocVersion, File, Folder, collect_subtree,
                    file_abspath, get_db, unlink_quiet, utcnow)

router = APIRouter(dependencies=[Depends(pat_write_guard)])


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
    父级仍在回收站(或已不存在)时回落到项目根,否则恢复出来的东西仍然看不见。
    返回 (主资源数, 连带文件数)。"""
    ids = collect_subtree(db, model, root)
    main = (db.query(model).filter(model.id.in_(ids), model.deleted_at.isnot(None))
            .update({"deleted_at": None}, synchronize_session=False))
    files = 0
    if model is Folder:
        files = (db.query(File).filter(File.folder_id.in_(ids), File.deleted_at.isnot(None))
                 .update({"deleted_at": None}, synchronize_session=False))
    # 父级回落用 update 而非改 ORM 属性:root 对象的状态已被上面的批量语句改过,
    # 直接赋属性会把陈旧的 deleted_at 一起写回去
    if root.parent_id and root.parent_id not in set(ids):
        parent = db.get(model, root.parent_id)
        if parent is None or parent.deleted_at is not None:
            db.query(model).filter(model.id == root.id) \
                .update({"parent_id": None}, synchronize_session=False)
    db.commit()
    return main, files


def commit_and_unlink(db: DbSession, paths) -> None:
    """物理文件清理的统一次序:先提交记录删除,再逐个 unlink(失败不回滚)。
    彻底删除文件夹/文件与删项目共用。"""
    db.commit()
    for p in paths:
        unlink_quiet(p)


# ---------- 文档 ----------

@router.delete("/api/docs/{doc_id}")
def delete_doc(doc_id: int, dep=Depends(require_doc_role("EDITOR")),
               db: DbSession = Depends(get_db)):
    _, doc = dep
    removed, _files = soft_delete_tree(db, Doc, doc)
    return {"removed": removed}


@router.post("/api/docs/{doc_id}/restore")
def restore_doc(doc_id: int, dep=Depends(require_doc_role("EDITOR", for_trash=True)),
                db: DbSession = Depends(get_db)):
    _, doc = dep
    if doc.deleted_at is None:
        err(409, "CONFLICT", "文档不在回收站")
    restored, _files = restore_tree(db, Doc, doc)
    return {"restored": restored}


@router.delete("/api/docs/{doc_id}/permanent")
def permanent_delete_doc(doc_id: int,
                         dep=Depends(require_doc_role("EDITOR", for_trash=True)),
                         db: DbSession = Depends(get_db)):
    """彻底删除:仅回收站中的文档可删;连同子树与历史版本一起清除"""
    _, doc = dep
    if doc.deleted_at is None:
        err(409, "CONFLICT", "文档不在回收站")
    ids = collect_subtree(db, Doc, doc)
    db.query(DocVersion).filter(DocVersion.doc_id.in_(ids)).delete(synchronize_session=False)
    db.query(Doc).filter(Doc.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return {"deleted": len(ids)}


# ---------- 文件夹 ----------

@router.delete("/api/files/folders/{folder_id}")
def delete_folder(folder_id: int, dep=Depends(require_folder_role("EDITOR")),
                  db: DbSession = Depends(get_db)):
    """删除文件夹 = **递归软删除整棵子树**(文件夹 + 后代文件夹 + 文件)。

    历史语义是"非空拒删",后果是用户必须自底向上手工清空才能删掉一个目录:
    层级越深步骤越多,中途失败还会留下删了一半的状态。现改为一次性整棵软删除,
    与文档删除的语义对齐,且因为是软删除所以完全可恢复。

    回收站只列**子树根**(见 project_trash),不会因为删一个目录而多出几十条子项。
    """
    _, folder = dep
    folders, files = soft_delete_tree(db, Folder, folder)
    return {"ok": True, "removedFolders": folders, "removedFiles": files}


@router.post("/api/files/folders/{folder_id}/restore")
def restore_folder(folder_id: int,
                   dep=Depends(require_folder_role("EDITOR", for_trash=True)),
                   db: DbSession = Depends(get_db)):
    """从回收站恢复文件夹 = **整棵子树**(与删除对称);父级回退语义见 restore_tree。"""
    _, folder = dep
    if folder.deleted_at is None:
        err(409, "CONFLICT", "文件夹不在回收站")
    folders, files = restore_tree(db, Folder, folder)
    return {"ok": True, "restoredFolders": folders, "restoredFiles": files}


@router.delete("/api/files/folders/{folder_id}/permanent")
def permanent_delete_folder(folder_id: int,
                            dep=Depends(require_folder_role("EDITOR", for_trash=True)),
                            db: DbSession = Depends(get_db)):
    """彻底删除文件夹:仅回收站中的可删;连同子树内所有文件夹与文件一起清除(含物理文件)。

    由于删除即整棵软删除(见 delete_folder),回收站里一个文件夹的子树必然整体处于
    已删除状态;这里仍按"全部后代"清除,不区分删除状态 —— 用户要彻底删掉这个目录,
    目录里的东西自然一并消失。
    """
    _, folder = dep
    if folder.deleted_at is None:
        err(409, "CONFLICT", "文件夹不在回收站")
    ids = collect_subtree(db, Folder, folder)
    files = db.query(File).filter(File.folder_id.in_(ids)).all()
    paths = [file_abspath(f.storage_path) for f in files]
    for f in files:
        db.delete(f)
    db.query(Folder).filter(Folder.id.in_(ids)).delete(synchronize_session=False)
    commit_and_unlink(db, paths)
    return {"ok": True, "removedFolders": len(ids), "removedFiles": len(files)}


# ---------- 文件(单条资源,无子树) ----------

@router.delete("/api/files/{file_id}")
def delete_file(file_id: int, dep=Depends(require_file_role("EDITOR")),
                db: DbSession = Depends(get_db)):
    _, f = dep
    f.deleted_at = utcnow()
    db.commit()
    return {"ok": True}


@router.post("/api/files/{file_id}/restore")
def restore_file(file_id: int, dep=Depends(require_file_role("EDITOR", for_trash=True)),
                 db: DbSession = Depends(get_db)):
    """从回收站恢复;所在文件夹也被删(或已不存在)时回落到项目根目录(对齐文档恢复语义)"""
    _, f = dep
    if f.deleted_at is None:
        err(409, "CONFLICT", "文件不在回收站")
    if f.folder_id:
        folder = db.get(Folder, f.folder_id)
        if not folder or folder.deleted_at is not None:
            f.folder_id = None
    f.deleted_at = None
    db.commit()
    return {"ok": True}


@router.delete("/api/files/{file_id}/permanent")
def permanent_delete_file(file_id: int,
                          dep=Depends(require_file_role("EDITOR", for_trash=True)),
                          db: DbSession = Depends(get_db)):
    """彻底删除:仅回收站中的文件可删;清记录并删除物理文件"""
    _, f = dep
    if f.deleted_at is None:
        err(409, "CONFLICT", "文件不在回收站")
    path = file_abspath(f.storage_path)
    db.delete(f)
    commit_and_unlink(db, [path])
    return {"ok": True}


# ---------- 回收站列表 ----------

@router.get("/api/projects/{project_id}/trash")
def project_trash(project_id: int,
                  ctx: AuthContext = Depends(require_project_role("VIEWER")),
                  db: DbSession = Depends(get_db)):
    """项目回收站:文档 + 云空间文件 + 文件夹统一返回(均按删除时间倒序)。

    **只列"子树根"**:删除文件夹/文档时整棵子树都被软删除,若把子项也列出来,
    删一个目录会让回收站一次多出几十条,而它们本就该随根一起恢复(恢复是递归的)。
    判据:父级未删除、或父级不存在(父级被彻底删掉后子项已在同一事务中清除)。

    注意 500 条上限作用在**过滤前**的原始集合上:极端情况下(单项目回收站里
    超过 500 个已删项)可能少列一些根。要彻底解决需引入分页,见 HANDOFF §4.9。
    """
    # 回收站只对**真成员与全局管理员**开放:公开项目的访客能读正式内容,
    # 但不该看到别人删掉了什么(删除历史属于项目内部信息)。
    if not (is_project_member(db, project_id, ctx.user) or ctx.user.is_admin):
        err(403, "FORBIDDEN", "回收站仅项目成员可见")
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
        "docs": [{"id": d.id, "title": d.title, "deletedAt": d.deleted_at.isoformat()}
                 for d in docs],
        "files": [{**file_json(f), "deletedAt": f.deleted_at.isoformat()} for f in files],
        "folders": [{"id": f.id, "name": f.name, "deletedAt": f.deleted_at.isoformat()}
                    for f in folders],
    }
