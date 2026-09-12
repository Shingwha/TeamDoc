"""同事目录与成员发现。

背景:此前全站唯一的用户列表是管理后台(仅 is_admin),非管理员能看到的用户只有
"我已加入项目的成员",而添加成员只能**精确输入邮箱** —— 没人告诉你同事邮箱,
你就无法与任何人协作,新人入职后除管理员谁也找不到。

覆盖:
  1. 任意登录用户可读同事目录;返回字段只有 id/name/email/avatarColor
  2. 不泄露 isAdmin / isDisabled / createdAt(那是管理后台的字段面)
  3. 被禁用的账号不出现在目录里
  4. 未登录访问 → 401
  5. 目录中的 email/id 可直接用于添加成员(与成员接口的契约一致)
"""
from _harness import Client, make_user

ADMIN_EMAIL = "admin@teamdoc.local"


def test_directory_readable_and_field_surface(admin):
    r = admin.get("/api/users/directory")
    assert r.status == 200, f"管理员可读目录: {r.status}"
    d = r.data
    assert isinstance(d, list), f"返回数组: {type(d).__name__}"
    assert any(u["email"] == ADMIN_EMAIL for u in d), \
        f"至少含管理员本人: {[u.get('email') for u in d[:5]]}"
    if d:
        keys = set(d[0].keys())
        assert keys == {"id", "name", "email", "avatarColor"}, \
            f"字段恰为 id/name/email/avatarColor: {sorted(keys)}"
        assert all(u.get("avatarColor") for u in d), "avatarColor 非空"


def test_plain_user_can_read_directory(base_url, admin):
    plain_email, _ = make_user(admin, "目录测试员", "plain12345")
    plain = Client(base_url)
    plain.login(plain_email, "plain12345")
    admin_dir = admin.get("/api/users/directory").data
    r = plain.get("/api/users/directory")
    assert r.status == 200, f"普通用户可读目录 → 200(此前只有管理后台能看到人): {r.status}"
    d2 = r.data
    assert isinstance(d2, list) and len(d2) >= len(admin_dir) - 1, \
        f"普通用户看到的人数与管理员的相近: {len(d2) if d2 else 0} vs {len(admin_dir)}"
    assert plain.get("/api/users").status == 403, "但用户管理接口仍拒绝普通用户(403)"
    assert Client(base_url).get("/api/users/directory").status == 401, "未登录访问目录 → 401"


def test_disabled_account_hidden(admin):
    disabled_email, created = make_user(admin, "待禁用", "dis12345")
    uid = created["id"]
    d3 = admin.get("/api/users/directory").data
    assert any(u["id"] == uid for u in d3), "启用时出现在目录"
    admin.patch(f"/api/users/{uid}", {"isDisabled": True})
    d4 = admin.get("/api/users/directory").data
    assert not any(u["id"] == uid for u in d4), "禁用后从目录消失"


def test_directory_id_usable_for_members(admin):
    pid = admin.post("/api/projects", {"name": "目录契约测试", "description": "d"}).data["id"]
    d = admin.get("/api/users/directory").data
    target = next(u for u in d if u["email"] != ADMIN_EMAIL)
    r = admin.post(f"/api/projects/{pid}/members",
                   {"userId": target["id"], "role": "EDITOR"})
    assert r.status == 200, f"用目录里的 id 加成员 → 200: {r.status} {r.data}"
    members = admin.get(f"/api/projects/{pid}/members").data
    assert any(m["userId"] == target["id"] for m in members), \
        f"成员列表含该用户: {[m['userId'] for m in members]}"
    # 重复添加应 409(前端据此在选人器里排除已有成员)
    r = admin.post(f"/api/projects/{pid}/members",
                   {"userId": target["id"], "role": "EDITOR"})
    assert r.status == 409, f"重复添加 → 409(故选择器需排除已有成员): {r.status}"
