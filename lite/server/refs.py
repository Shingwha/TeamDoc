"""引用契约的**唯一真相源**(服务端半边;前端半边是 lite/web/js/ref.js)。

正文里指向站内资源的引用有两种形态,都是普通 Markdown 链接:

    chip     [@标题](teamdoc://doc/{docId})      文档引用
             [@名称](teamdoc://file/{fileId})     文件引用
    附件/内嵌 [名称](/api/files/{fileId}/download)
             ![名称](/api/files/{fileId}/download?inline=1)

**两种形态都要认**:chip 是编辑器 @ 菜单给出的默认形态,下载链接是手写附件与内嵌
图片的形态。只认后者,chip 引用的文件在删除时就没有"被 N 篇文档引用"的提醒。

**只认 id-only 形态**:引用里不带项目 id —— 项目归属会变(文档可以跨项目移动),
而引用必须跟着资源走。带项目 id 的两段式不是本契约的形态,解析器不认。

消费方:docs.list_backlinks(反链)、files.list_files(被引用标记)、docs.move_check
(移动前提示)。三处判定**必须同源**,否则会出现"反链说有、删除警告说没有"这种自相矛盾。
"""
import re

from ids import ID_PATTERN

DOC_PREFIX = "teamdoc://doc/"
FILE_PREFIX = "teamdoc://file/"

# 形如 /api/files/{id}/download(?inline=1) —— 允许前后被 Markdown 包裹,故只匹配路径本身。
# chip 取"前缀之后的整段",**不在正则里排除 `/`**:两段式 `teamdoc://doc/{pid}/{did}`
# 的候选串会带上斜杠,由形状判定统一拒掉(严格 id-only)。让正则去表达"什么算候选",
# 等于把这条契约的实现又分了一份出去。
_DOWNLOAD_RE = re.compile(r"/api/files/([^/?#\s)]+)/download")
_DOC_CHIP_RE = re.compile(re.escape(DOC_PREFIX) + r"([^)\s]+)")
_FILE_CHIP_RE = re.compile(re.escape(FILE_PREFIX) + r"([^)\s]+)")


def doc_ref(doc_id: str) -> str:
    """文档引用的规范形态(生成侧只有这一处,前端对应 Ref.docChip)。"""
    return f"{DOC_PREFIX}{doc_id}"


def file_ref(file_id: str) -> str:
    """文件引用的规范形态(前端对应 Ref.fileChip)。"""
    return f"{FILE_PREFIX}{file_id}"


def download_url(file_id: str, inline: bool = False) -> str:
    """下载地址的规范形态(前端对应 Ref.downloadUrl)。"""
    return f"/api/files/{file_id}/download" + ("?inline=1" if inline else "")


def file_ids_in(content: str) -> set[str]:
    """正文里被引用的全部文件 id(两种形态都算)。

    提取后仍要过 ID_PATTERN:正则只能切出"候选串",形状判定归 ids.py ——
    两处判断合成一个"是不是引用",调用方不必自己再校验一遍。
    """
    text = content or ""
    if "/api/files/" not in text and FILE_PREFIX not in text:
        return set()  # 快路径:绝大多数文档不含文件引用,不为它们跑正则
    found: set[str] = set()
    for m in _DOWNLOAD_RE.finditer(text):
        if ID_PATTERN.fullmatch(m.group(1)):
            found.add(m.group(1))
    for m in _FILE_CHIP_RE.finditer(text):
        if ID_PATTERN.fullmatch(m.group(1)):
            found.add(m.group(1))
    return found


def doc_ids_in(content: str) -> set[str]:
    """正文里被引用的全部文档 id(反链用)。"""
    text = content or ""
    if DOC_PREFIX not in text:
        return set()
    return {m.group(1) for m in _DOC_CHIP_RE.finditer(text) if ID_PATTERN.fullmatch(m.group(1))}


def sql_prefilter(*columns):
    """给"正文里引用了某类资源"的 SQL 查询用的粗筛条件(把全表扫描挡在正则之前)。

    返回 SQLAlchemy or_ 条件:任一列含任一种引用前缀。粗筛只要求"可能含引用",
    精确判定仍由上面的 *_ids_in 做 —— 两个层次的责任分开写清楚,SQL 里不塞正则。
    """
    from sqlalchemy import or_
    clauses = []
    for col in columns:
        for prefix in ("/api/files/", DOC_PREFIX, FILE_PREFIX):
            clauses.append(col.like(f"%{prefix}%"))
    return or_(*clauses)
