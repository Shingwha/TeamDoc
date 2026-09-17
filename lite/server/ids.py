"""资源 id:ULID(26 字符 Crockford Base32 = 48 位毫秒时间戳 + 80 位随机)。

设计要点:

- **不可枚举**。公开分享的下载地址是 `/api/files/{id}/download`,顺序可猜的 id
  (自增整数)等于让任何人从起点一路扫走所有公开内容。不透明 id 把这条路堵死。
- **一种形态**。id 是字符串:URL 里是它、JSON 里是它、dataset 里还是它,
  全站 `===` 直接比较即可 —— 不需要"这里转数字、那里转字符串"的归一约定。
- **时间有序**。时间戳在上、随机在下,故字典序 = 创建顺序(列表与树按 id 排序
  即按创建先后),索引局部性也好。

形态遵循 ULID 规范:大写、无连字符;字母表排除 I/L/O/U,手抄与口述不易混淆。
解码在**边界处**大小写不敏感(链接被人手工改成小写也认得),库里只存大写形态。

这里同时提供 FastAPI 的类型别名 `IdPath`:资源 id 的形状只有这一处定义,路由签名写
`doc_id: IdPath` 即获得校验,非法值走 main.py 的 RequestValidationError 处理
→ 400 VALIDATION(坏链接在边界上就被拦下,不会进到查询里变成一次 404)。
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Annotated

from pydantic import AfterValidator

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford Base32:无 I L O U
ID_LEN = 26
_RAND_BITS = 80

# 形状判定只有这一处:is_id / 引用解析(refs.py)/ payload 与查询串(auth.id_field)
# 全走它。字母表里没有需要转义的字符,可以直接进字符类。
ID_PATTERN = re.compile(rf"[{_ALPHABET}]{{{ID_LEN}}}")


def new_id() -> str:
    """新的资源 id。时间戳(毫秒)在上、随机在下,故字典序 = 时间序。"""
    value = (int(time.time() * 1000) << _RAND_BITS) | secrets.randbits(_RAND_BITS)
    chars = []
    for _ in range(ID_LEN):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def is_id(value) -> bool:
    """形状校验(不归一大小写)。"""
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def normalize_id(value) -> str | None:
    """边界归一:合法则返回大写形态,否则 None(调用方决定怎么报错)。

    只做大小写归一,不做别的猜测 —— 解码大小写不敏感是 ULID 规范的规定。
    """
    if not isinstance(value, str):
        return None
    up = value.upper()
    return up if is_id(up) else None


def _validate(value: str) -> str:
    got = normalize_id(value)
    if got is None:
        raise ValueError("id 形状非法")
    return got


IdPath = Annotated[str, AfterValidator(_validate)]   # 路由路径参数用:doc_id: IdPath
