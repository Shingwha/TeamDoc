"""HTTP 契约错误的唯一出口。

错误响应信封是 `{"detail": {"code": ..., "message": ..., ...}}`:

- **code 是机器判据** —— 前端与 teamdoc-cli 按它分支(如 READ_ONLY_TOKEN 决定提示
  "去网页端建一个 read,write 令牌")。改 code 等于改契约,调用方会静默走错分支。
- **message 是人话** —— 只给人看,调用方不得嗅探其中的字样;文案随时可以改。

所以错误码在这里各有一个常量,调用方引用常量而不是重复字面量:一处改名,
全站跟着改,不会出现"响应里是这个拼写、判断里是另一个拼写"这种只在失败路径
才暴露的漂移。

`extra` 用于 409 这类需要附带现场数据的场景(文档内容冲突要把服务端当前正文
交给客户端做差异对比,只给一句错误文案不够用)。
"""
from typing import NoReturn

from fastapi import HTTPException

# 400:入参本身不合法(形状/类型/取值域),与"资源状态不对"区分开
VALIDATION = "VALIDATION"
# 401:没有可用凭据,或提供的凭据失效 —— 唯一的出路是重新登录/换令牌
UNAUTHORIZED = "UNAUTHORIZED"
# 403:凭据有效但权限不够
FORBIDDEN = "FORBIDDEN"
# 403:公开项目的非成员。与 FORBIDDEN 分开,因为它是唯一能用一句话说清出路的拒绝
# (去发现页点"加入"),而通用文案只会让人无从下手
JOIN_REQUIRED = "JOIN_REQUIRED"
# 403:凭据是只读令牌却发起了写请求。与 FORBIDDEN 分开的理由同上:客户端要据此
# 提示"换一个 read,write 令牌",而不是"你没有权限"
READ_ONLY_TOKEN = "READ_ONLY_TOKEN"
# 404:资源不存在,或存在但对调用方不可见(两种情况不区分,避免探测)
NOT_FOUND = "NOT_FOUND"
# 409:资源都在,是这次操作本身不成立(重名、成环、最后一个所有者……)
CONFLICT = "CONFLICT"
# 429:凭据尝试节流
TOO_MANY_ATTEMPTS = "TOO_MANY_ATTEMPTS"


def err(status: int, code: str, message: str, *, headers: dict | None = None,
        extra: dict | None = None) -> NoReturn:
    """抛一个契约错误。headers 供 429 带 Retry-After、401 带清 Cookie 用。"""
    detail = {"code": code, "message": message}
    if extra:
        detail.update(extra)
    raise HTTPException(status_code=status, detail=detail, headers=headers)


def bad_request(message: str) -> NoReturn:
    """400 VALIDATION 的简写(入参校验失败是最常见的错误,占全站错误的四成)。"""
    err(400, VALIDATION, message)
