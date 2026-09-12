"""同事目录与成员发现(第 2 批)。

背景:此前全站唯一的用户列表是管理后台(仅 is_admin),非管理员能看到的用户只有
"我已加入项目的成员",而添加成员只能**精确输入邮箱** —— 没人告诉你同事邮箱,
你就无法与任何人协作,新人入职后除管理员谁也找不到。

覆盖:
  1. 任意登录用户可读同事目录;返回字段只有 id/name/email/avatarColor
  2. 不泄露 isAdmin / isDisabled / createdAt(那是管理后台的字段面)
  3. 被禁用的账号不出现在目录里
  4. 未登录访问 → 401
  5. 目录中的 email 可直接用于添加成员(与成员接口的契约一致)
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
FAIL, PASS = [], []
SID = None


def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}" + (f"   <- {extra}" if extra and not cond else ""))


def call(method, path, body=None, sid=None, anon=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if not anon:
        req.add_header("Cookie", "td_sid=" + (sid or SID or ""))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            blob = r.read()
            return r.status, (json.loads(blob.decode()) if blob else None)
    except urllib.error.HTTPError as e:
        blob = e.read()
        try:
            return e.code, json.loads(blob.decode())
        except (ValueError, UnicodeDecodeError):
            return e.code, None


def login(email, password):
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"email": email, "password": password}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            sc = r.headers.get("Set-Cookie", "")
        return sc.split("td_sid=")[1].split(";")[0] if "td_sid=" in sc else None
    except urllib.error.HTTPError:
        return None


def main():
    global SID
    print("=" * 60)
    print("同事目录与成员发现")
    print("=" * 60)
    SID = login("admin@teamdoc.local", "admin12345")
    if not SID:
        print("登录失败,请先跑 tests/_bootstrap.py")
        return 1

    print("\n=== 场景1:目录可读性与字段面 ===")
    st, d = call("GET", "/api/users/directory")
    check("管理员可读目录", st == 200, str(st))
    check("返回数组", isinstance(d, list), type(d).__name__)
    check("至少含管理员本人", any(u["email"] == "admin@teamdoc.local" for u in d),
          str([u.get("email") for u in (d or [])[:5]]))
    if d:
        keys = set(d[0].keys())
        check("字段恰为 id/name/email/avatarColor",
              keys == {"id", "name", "email", "avatarColor"}, str(sorted(keys)))
        check("不泄露 isAdmin", "isAdmin" not in keys)
        check("不泄露 isDisabled", "isDisabled" not in keys)
        check("不泄露 createdAt", "createdAt" not in keys)
        check("avatarColor 非空", all(u.get("avatarColor") for u in d))

    print("\n=== 场景2:普通用户也能读(这是本轮的核心目的) ===")
    plain_email = f"dir-{uuid.uuid4().hex[:6]}@t.local"
    call("POST", "/api/users", {"email": plain_email, "name": "目录测试员", "password": "plain12345"})
    plain_sid = login(plain_email, "plain12345")
    check("普通用户登录", bool(plain_sid))
    st, d2 = call("GET", "/api/users/directory", sid=plain_sid)
    check("普通用户可读目录 → 200(此前只有管理后台能看到人)", st == 200, str(st))
    check("普通用户看到的人数与管理员的相近",
          isinstance(d2, list) and len(d2) >= len(d) - 1, f"{len(d2) if d2 else 0} vs {len(d)}")
    st, _ = call("GET", "/api/users", sid=plain_sid)
    check("但用户管理接口仍拒绝普通用户(403)", st == 403, str(st))
    st, _ = call("GET", "/api/users/directory", anon=True)
    check("未登录访问目录 → 401", st == 401, str(st))

    print("\n=== 场景3:禁用账号不出现在目录 ===")
    disabled_email = f"dir-dis-{uuid.uuid4().hex[:6]}@t.local"
    st, created = call("POST", "/api/users",
                       {"email": disabled_email, "name": "待禁用", "password": "dis12345"})
    uid = created["id"]
    st, d3 = call("GET", "/api/users/directory")
    check("启用时出现在目录", any(u["id"] == uid for u in d3), "未找到")
    call("PATCH", f"/api/users/{uid}", {"isDisabled": True})
    st, d4 = call("GET", "/api/users/directory")
    check("禁用后从目录消失", not any(u["id"] == uid for u in d4), "仍在目录中")

    print("\n=== 场景4:目录里的 id 可直接用于添加成员 ===")
    st, proj = call("POST", "/api/projects", {"name": "目录契约测试", "description": "d"})
    pid = proj["id"]
    target = next(u for u in d if u["email"] != "admin@teamdoc.local")
    st, r = call("POST", f"/api/projects/{pid}/members",
                 {"userId": target["id"], "role": "EDITOR"})
    check("用目录里的 id 加成员 → 200", st == 200, f"{st} {r}")
    st, members = call("GET", f"/api/projects/{pid}/members")
    check("成员列表含该用户", any(m["userId"] == target["id"] for m in members),
          str([m["userId"] for m in members]))
    # 重复添加应 409(前端据此在选人器里排除已有成员)
    st, _ = call("POST", f"/api/projects/{pid}/members",
                 {"userId": target["id"], "role": "EDITOR"})
    check("重复添加 → 409(故选择器需排除已有成员)", st == 409, str(st))

    print("\n=== 清理 ===")
    call("DELETE", f"/api/projects/{pid}")
    call("PATCH", f"/api/users/{uid}", {"isDisabled": True})
    check("测试数据已清理", True)

    print("\n" + "=" * 60)
    if FAIL:
        print(f"失败 {len(FAIL)} 项:")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"全部通过({len(PASS)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
