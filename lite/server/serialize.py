"""API 响应形状的唯一来源。

每个函数产出一类资源的响应体,`face` 决定给谁看。把字段面写成参数而不是让每个
端点手拼字典,是为了让"加/改一个字段"只改一处:同一个用户对象在不同端点的字段面
本来就不一样(本人 / 同事目录 / 项目成员 / 管理表格 / 登录详情),手拼就有多份副本,
加一个字段要记住改几处,漏一处的表现是"某个页面缺了个字段"而没人报错。

**字段面必须区分,不能"都给"**:邮箱、禁用状态、分享 token 属于不同受众 ——
同事目录给全员看,所以刻意不含 isAdmin;分享 token 是凭据,只该出现在单文件详情里,
不该随列表批量下发。

依赖方向:本模块只 import models 与 media。它被 auth / projects / admin 等 import,
自己不能再 import 它们(会成环)。需要查库才能算出的字段(myRole、isMember、
location)由调用方算好传进来。
"""
import hashlib

from media import can_inline, guess_mime, is_text

# ---------- 基础 ----------

def iso(dt) -> str | None:
    """时间戳的 API 形态(UTC、T 分隔)。None 原样返回 None。

    全站只有这一个格式化入口:前端只解析一种形状,不需要兼容两种分隔符。
    """
    return dt.isoformat() if dt else None


# 头像颜色:由 user id 哈希在固定调色板中取色(确定性,不落库 —— 它不是用户的属性,
# 而是 id 的呈现,所以没有对应的列)。
_PALETTE = ["#3370ff", "#7c3aed", "#db2777", "#ea580c", "#16a34a", "#0891b2", "#ca8a04", "#1f6feb"]


def avatar_color(user_id: str) -> str:
    h = int(hashlib.md5(str(user_id).encode("utf-8")).hexdigest(), 16)
    return _PALETTE[h % len(_PALETTE)]


_BROWSER_TOKENS = (("Edg", "Edge"), ("OPR", "Opera"), ("Firefox", "Firefox"),
                   ("Chrome", "Chrome"), ("Safari", "Safari"))
_OS_TOKENS = (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"),
              ("iPad", "iPad"), ("Mac OS X", "macOS"), ("Linux", "Linux"))
_MOBILE_TOKENS = ("Android", "iPhone", "iPad", "Mobile")


def describe_ua(ua: str) -> tuple[str, str]:
    """把 UA 归纳为 (可读标签, 设备档位 desktop|mobile)。认不出回落"未知设备"。

    刻意只做粗归纳(要在内网离线环境零依赖运行),且**只在服务端做一次** ——
    UA 解析放前端就变成同一份知识两处实现。原始 UA 一并返回给前端做 tooltip,
    排查时以它为准。
    """
    if not ua:
        return "未知设备", "desktop"
    browser = next((label for tok, label in _BROWSER_TOKENS if tok in ua), "")
    os_name = next((label for tok, label in _OS_TOKENS if tok in ua), "")
    label = " · ".join(p for p in (browser, os_name) if p) or "未知设备"
    kind = "mobile" if any(tok in ua for tok in _MOBILE_TOKENS) else "desktop"
    return label, kind


# ---------- 用户 ----------

def user_json(u, *, face: str = "self") -> dict:
    """用户对象的响应形状。

    self   本人(登录 / 初始化 / auth-me):含 isAdmin,只发给自己
    dir    同事目录:**全员可读**,刻意不含 isAdmin / isDisabled / createdAt
    member 项目成员与所有者列表(能到达该端点的只有成员与管理员)
    access 管理后台的用户详情(登录状态与审计面)
    admin  管理后台的用户表格(含 createdAt)
    """
    if face == "self":
        return {"id": u.id, "email": u.email, "name": u.name,
                "isAdmin": bool(u.is_admin), "avatarColor": avatar_color(u.id)}
    if face == "dir":
        return {"id": u.id, "name": u.name, "email": u.email,
                "avatarColor": avatar_color(u.id)}
    if face == "member":
        return {"id": u.id, "email": u.email, "name": u.name,
                "isDisabled": bool(u.is_disabled), "avatarColor": avatar_color(u.id)}
    if face == "access":
        return {"id": u.id, "email": u.email, "name": u.name,
                "isAdmin": bool(u.is_admin), "isDisabled": bool(u.is_disabled)}
    if face == "admin":
        return {"id": u.id, "email": u.email, "name": u.name,
                "isAdmin": bool(u.is_admin), "isDisabled": bool(u.is_disabled),
                "avatarColor": avatar_color(u.id), "createdAt": iso(u.created_at)}
    raise ValueError(f"未知的 user face: {face}")


# ---------- 项目 ----------

def project_json(p, *, face: str = "member", my_role: str | None = None,
                 is_member: bool = False, stats: dict | None = None,
                 owners: list | None = None) -> dict:
    """项目对象的响应形状。

    member 成员视角(列表 / 详情 / 加入后的返回)。myRole 与 isMember 由调用方算好传入:
           它们要查库,而查库的语义(project_role 是全站权限的咽喉)不属于"序列化"。
    admin  管理后台的项目总览:管理视角要的是"谁拥有、占多少、活跃吗",故带 owners 与
           storageBytes,且**不含** myRole/isMember/joinRole(那些是成员视角的概念)。
    """
    s = stats or {}
    last = s.get("lastUpdatedAt")
    if face == "member":
        return {
            "id": p.id, "name": p.name, "description": p.description,
            "isPersonal": bool(p.is_personal),
            "isPublic": p.visibility == "public",
            # joinRole 给广场卡片用:未加入者要能看见"加入后我会拿到什么角色"
            "joinRole": p.join_role,
            # isMember = 真实成员关系(管理员与未加入者都是 false)。前端据此渲染
            # 「退出项目」这类只对真成员成立的入口;而"能不能进这个项目"一律看 myRole
            "isMember": is_member,
            "createdAt": iso(p.created_at),
            "lastUpdatedAt": iso(last),
            "myRole": my_role,
            "memberCount": s.get("memberCount", 0),
            "docCount": s.get("docCount", 0),
        }
    if face == "admin":
        return {
            "id": p.id, "name": p.name, "description": p.description,
            "isPublic": p.visibility == "public",
            "createdAt": iso(p.created_at),
            "lastUpdatedAt": iso(last),
            "memberCount": s.get("memberCount", 0),
            "docCount": s.get("docCount", 0),
            "storageBytes": s.get("storageBytes", 0),
            "owners": owners or [],
        }
    raise ValueError(f"未知的 project face: {face}")


# ---------- 文档 ----------

def doc_json(d, *, face: str = "brief", location: dict | None = None,
             project_name: str = "") -> dict:
    """文档对象的响应形状。

    brief    创建 / 改标题 / 移动的返回:只给"这次操作改了什么"
    full     正文详情(get_doc):带 content 与 location
    tree     文档树节点(build_tree 要求节点自带 children)
    backlink 反向链接行:**跨项目**,所以必须带 projectName
    """
    if face == "brief":
        return {"id": d.id, "projectId": d.project_id, "parentId": d.parent_id,
                "title": d.title, "version": d.version,
                "createdAt": iso(d.created_at), "updatedAt": iso(d.updated_at)}
    if face == "full":
        return {"id": d.id, "projectId": d.project_id, "parentId": d.parent_id,
                "title": d.title, "content": d.content, "version": d.version,
                "location": location,
                "contentChars": len(d.content or ""),
                "updatedAt": iso(d.updated_at), "createdAt": iso(d.created_at)}
    if face == "tree":
        return {"id": d.id, "title": d.title, "parentId": d.parent_id,
                "updatedAt": iso(d.updated_at), "children": []}
    if face == "backlink":
        return {"id": d.id, "title": d.title, "updatedAt": iso(d.updated_at),
                "projectId": d.project_id, "projectName": project_name}
    raise ValueError(f"未知的 doc face: {face}")


def version_json(row, *, author_name: str | None = None) -> dict:
    """历史版本行(不含正文,只给"哪一版"的线索)。

    row 是 (id, kind, created_at, created_by, content_chars) 元组 —— 列表只要这几列:
    正文是 Text,把 100 条正文读进内存只为算字符数没有道理,长度在 SQL 里算。
    """
    vid, kind, created_at, created_by, chars = row
    return {"id": vid, "kind": kind, "createdAt": iso(created_at),
            "contentChars": chars or 0,
            "createdBy": ({"id": created_by, "name": author_name} if author_name else None)}


# ---------- 文件与文件夹 ----------

def file_json(f, *, face: str = "self", created_by=None, referenced: bool = False,
              location: dict | None = None) -> dict:
    """文件对象的响应形状。

    mime 由文件名**读时推导**(media.guess_mime 是全站唯一判定):它没有独立信息,
    存一列只会与判定规则的演进脱节 —— 改一次规则就得回填全表,而漏掉的行会
    以"预览打不开"的形式表现出来。

    face:
      self    文件名 + 类型 + 大小 + 时间(列表/上传/改名/回收站/搜索结果的基础)
      list    云空间列表:额外带"被引用"标记与创建人(由调用方查好传入)
      meta    单文件详情:**只有这里带 shareUrl** —— 分享 token 是凭据,
              不该随列表批量下发;列表只给 shared 布尔(徽标够用)
    """
    mime = guess_mime(f.name or "")
    out = {"id": f.id, "name": f.name, "mime": mime, "size": f.size,
           "canInline": can_inline(mime), "isText": is_text(mime),
           "shared": bool(f.share_token),
           "createdAt": iso(f.created_at)}
    if face == "self":
        return out
    if face == "list":
        cb = None
        if created_by is not None:
            cb = {"id": created_by.id, "name": created_by.name}
        return {**out, "referenced": bool(referenced), "createdBy": cb}
    if face == "meta":
        return {**out, "projectId": f.project_id, "folderId": f.folder_id,
                "shareUrl": f"/api/share/{f.share_token}" if f.share_token else None,
                "shareExpiresAt": iso(f.share_expires_at),
                "location": location}
    raise ValueError(f"未知的 file face: {face}")


def folder_json(f, *, face: str = "self") -> dict:
    """文件夹对象的响应形状(self 基础字段 / tree 树节点)。"""
    if face == "self":
        return {"id": f.id, "name": f.name, "createdAt": iso(f.created_at)}
    if face == "tree":
        return {"id": f.id, "name": f.name, "parentId": f.parent_id, "children": []}
    raise ValueError(f"未知的 folder face: {face}")
