# -*- coding: utf-8 -*-
"""一次性迁移:旧十六进制 id 数据库 → 整数自增 id。

背景:2026-09 起资源表主键从 secrets.token_hex(8) 改为 Integer 自增(见 HANDOFF §2)。
新代码遇到旧库会在启动自检处失败,旧数据本身不会自动转换 —— 本脚本就是那把钥匙。

用法(**先停服务**):
    cd lite/server
    python ../tools/convert_ids_to_int.py
脚本操作 TEAMDOC_DATA_DIR(默认 server/data):
    1. 把现有数据目录整体改名为 data-pre-convert-<时间戳>(保留一切,随时可退回)
    2. 按 当前 models.py 重建空库,迁移全部业务数据并重编号:
       users / pats / projects / project_members / docs / doc_versions / folders / files
       - 新 id 按原 created_at 顺序分配(管理员 = 1)
       - PAT 的 token_hash 原样保留:已有令牌继续可用,无需重新创建
    3. 重写文档正文与历史版本里的 teamdoc://doc/…、teamdoc://file/…、
       /api/files/…/download 引用到新 id(引用删过的资源则保持原样,链接本就已死)
    4. 物理文件按原 basename 整体复制(文件名与 id 本就解耦,无需改名)
不迁移会话:所有用户需要重新登录(密码不变)。
"""
import argparse
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))  # noqa: E402

HEX = r"[0-9a-f]{16}"


def main():
    ap = argparse.ArgumentParser(description="旧十六进制 id 库 → 整数自增 id 库(一次性)")
    ap.add_argument("--start", type=int, default=10000,
                    help="新 id 的起始值(默认 10000,五位数;与 schema.py 的种子一致)")
    args = ap.parse_args()
    START = args.start

    import models  # noqa: E402  需要 TEAMDOC_DATA_DIR 先就位,import 即建目录
    import schema  # noqa: E402

    data_dir = models.DATA_DIR
    old_db = data_dir / "teamdoc.db"
    if not old_db.exists():
        print(f"未找到旧库:{old_db}")
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir.parent / (data_dir.name + "-pre-convert-" + stamp)
    print(f"1) 保留旧数据目录 → {backup_dir}")
    data_dir.rename(backup_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 重建空库(与当前 models 完全一致的结构)
    schema.init(models.engine)
    new_db = sqlite3.connect(models.DB_PATH)
    new_db.execute("PRAGMA foreign_keys=OFF")  # 迁移中允许乱序插入,结束前统一做外键体检

    old = sqlite3.connect(f"file:{backup_dir / 'teamdoc.db'}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row

    def rows(table, order="created_at ASC, rowid ASC"):
        return old.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()

    def renumber(table, order="created_at ASC, rowid ASC"):
        """旧 id → 新 id 的映射(按创建顺序,起点 = START;键统一为字符串)"""
        m = {}
        for new_id, r in enumerate(rows(table, order), start=START):
            m[str(r["id"])] = new_id
        return m

    print("2) 迁移业务数据(重编号)…")
    u_map = renumber("users")
    p_map = renumber("projects")
    d_map = renumber("docs")
    f_map = renumber("files")
    fo_map = renumber("folders")
    maps = {"users": u_map, "projects": p_map, "docs": d_map, "files": f_map}

    # id 模式:十六进制串(原始旧库)或十进制串(本脚本已跑过的库)都要能识别,
    # 所以脚本可以重复执行、任意次重跑,结果收敛一致
    ID = rf"({HEX}|[1-9][0-9]*)"

    def rewrite(text):
        """正文里的引用链接重写到新 id;映射里没有的(指向已删资源)保持原样(本来就死链)"""
        if not text:
            return text

        def doc_repl(m):
            p, d = p_map.get(m.group(1)), d_map.get(m.group(2))
            return m.group(0) if (p is None or d is None) else f"teamdoc://doc/{p}/{d}"

        def file_repl(m):
            f = f_map.get(m.group(1))
            return m.group(0) if f is None else f"teamdoc://file/{f}"

        def dl_repl(m):
            f = f_map.get(m.group(1))
            return m.group(0) if f is None else f"/api/files/{f}/download"

        text = re.sub(rf"teamdoc://doc/{ID}/{ID}", doc_repl, text)
        text = re.sub(rf"teamdoc://file/{ID}", file_repl, text)
        text = re.sub(rf"/api/files/{ID}/download", dl_repl, text)
        return text

    def opt(m, v):
        """可空引用重映射:NULL 保持 NULL"""
        return m.get(str(v)) if v is not None else None

    def insert(table, columns, values):
        new_db.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            values)

    # users
    for r in rows("users"):
        insert("users", ["id", "email", "name", "password_hash", "is_admin", "is_disabled", "created_at"],
               [u_map[str(r["id"])], r["email"], r["name"], r["password_hash"],
                bool(r["is_admin"]), bool(r["is_disabled"]), r["created_at"]])
    # pats(token_hash 原样 → 令牌继续可用)
    for new_pat_id, r in enumerate(rows("pats"), start=START):
        insert("pats", ["id", "user_id", "name", "token_hash", "scopes",
                        "last_used_at", "revoked_at", "created_at"],
               [new_pat_id, u_map[str(r["user_id"])], r["name"], r["token_hash"], r["scopes"],
                r["last_used_at"], r["revoked_at"], r["created_at"]])
    # projects
    for r in rows("projects"):
        insert("projects", ["id", "name", "description", "is_personal", "visibility",
                            "created_by", "created_at"],
               [p_map[str(r["id"])], r["name"], r["description"], bool(r["is_personal"]),
                r["visibility"], opt(u_map, r["created_by"]), r["created_at"]])
    # project_members
    for new_mid, r in enumerate(rows("project_members"), start=START):
        insert("project_members", ["id", "project_id", "user_id", "role", "created_at"],
               [new_mid, p_map[str(r["project_id"])], u_map[str(r["user_id"])], r["role"], r["created_at"]])
    # folders(先根后子由 created_at 保证;外键已关闭,顺序不敏感)
    for r in rows("folders"):
        insert("folders", ["id", "name", "project_id", "parent_id", "created_at", "deleted_at"],
               [fo_map[str(r["id"])], r["name"], p_map[str(r["project_id"])], opt(fo_map, r["parent_id"]),
                r["created_at"], r["deleted_at"]])
    # docs(正文引用重写)
    for r in rows("docs"):
        insert("docs", ["id", "project_id", "parent_id", "title", "content", "sort", "version",
                        "created_by", "updated_by", "created_at", "updated_at", "deleted_at"],
               [d_map[str(r["id"])], p_map[str(r["project_id"])], opt(d_map, r["parent_id"]), r["title"],
                rewrite(r["content"]), r["sort"], r["version"],
                opt(u_map, r["created_by"]), opt(u_map, r["updated_by"]),
                r["created_at"], r["updated_at"], r["deleted_at"]])
    # doc_versions(历史版本里的引用同样重写:否则恢复旧版本会复活死链)
    for new_vid, r in enumerate(rows("doc_versions"), start=START):
        insert("doc_versions", ["id", "doc_id", "content", "label", "created_by", "created_at"],
               [new_vid, d_map[str(r["doc_id"])], rewrite(r["content"]), r["label"],
                opt(u_map, r["created_by"]), r["created_at"]])
    # files(storage_path = 原 basename,物理文件名不变 → 无需改名)
    for r in rows("files"):
        insert("files", ["id", "name", "project_id", "folder_id", "mime", "size", "storage_path",
                         "is_public", "created_by", "created_at", "deleted_at"],
               [f_map[str(r["id"])], r["name"], p_map[str(r["project_id"])], opt(fo_map, r["folder_id"]),
                r["mime"], r["size"], r["storage_path"], bool(r["is_public"]),
                opt(u_map, r["created_by"]), r["created_at"], r["deleted_at"]])

    new_db.commit()

    bad = new_db.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        print("!! 外键体检失败,迁移已中止(旧数据在备份目录中完好):", bad[:5])
        return 1
    new_db.close()
    old.close()

    print("3) 复制物理文件…")
    old_files = backup_dir / "files"
    if old_files.exists():
        shutil.copytree(old_files, models.FILES_DIR, dirs_exist_ok=True)

    print("\n迁移完成:")
    for name, m in (("用户", u_map), ("项目", p_map), ("文档", d_map), ("文件夹", fo_map), ("文件", f_map)):
        print(f"  {name}: {len(m)}")
    print(f"  旧库备份:{backup_dir}(确认无误后可删除)")
    print("  所有用户需重新登录(密码不变);PAT 令牌继续可用。")
    print("  现在可以启动服务了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
