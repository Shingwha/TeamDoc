"""媒体类型判定与 inline 白名单(横切关注点,安全边界,勿放宽)。

mime 由**服务端**按扩展名判定,不采信上传方(mimetypes 对多媒体类型有修正)。
独立成模块的原因:docs(回收站序列化)/search(搜索结果)/files(上传下载)
都要消费这套判定,放在 files.py 会让路由模块之间互相依赖,靠约定维持无环。
"""
import mimetypes
from pathlib import Path

_EXTRA_MIME = {
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".yaml": "text/yaml", ".yml": "text/yaml",
    ".toml": "text/plain", ".ini": "text/plain", ".conf": "text/plain",
    ".log": "text/plain", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".ts": "text/plain", ".tsx": "text/plain", ".jsx": "text/plain",
    ".py": "text/x-python", ".rb": "text/x-ruby", ".go": "text/x-go",
    ".rs": "text/x-rust", ".java": "text/x-java", ".c": "text/x-c",
    ".h": "text/x-c", ".cpp": "text/x-c++", ".hpp": "text/x-c++",
    ".sh": "text/x-sh", ".bash": "text/x-sh", ".zsh": "text/x-sh",
    ".ps1": "text/plain", ".sql": "text/x-sql", ".xml": "text/xml",
    ".css": "text/css", ".scss": "text/plain", ".less": "text/plain",
    ".vue": "text/plain", ".svelte": "text/plain", ".diff": "text/plain", ".patch": "text/plain",
}

# 可 inline 显示的**显式白名单**。危险点:同源渲染的可执行格式会让上传者拿到 XSS
# (svg 内可含 <script>、html 直接是文档),故 svg/html/xhtml 与一切未知类型
# 一律强制 attachment 下载。
_INLINE_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "image/avif", "image/x-icon", "image/vnd.microsoft.icon",
    "application/pdf",
} | {v for v in _EXTRA_MIME.values()} | {"text/plain"}

# 文本类:前端据此走站内模态框预览(而非图片/PDF 的新标签页)
TEXT_MIME_PREFIXES = ("text/",)
TEXT_MIME_EXACT = {"application/json", "application/xml", "application/x-yaml"}


def guess_mime(name: str) -> str:
    """按文件名判定 mime(服务端唯一真相,不采信客户端声明)"""
    ext = Path(name).suffix.lower()
    if ext in _EXTRA_MIME:
        return _EXTRA_MIME[ext]
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def can_inline(mime: str) -> bool:
    return mime in _INLINE_MIME


def is_text(mime: str) -> bool:
    return mime.startswith(TEXT_MIME_PREFIXES) or mime in TEXT_MIME_EXACT
