"""创建测试用管理员账号(admin@teamdoc.local / admin12345)。

用法:先启动服务(隔离数据目录 + 非常用端口),再运行本脚本。
已初始化过的实例会返回 403,属正常情况。
"""
import os
import json
import urllib.error
import urllib.request

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, (json.loads(raw) if raw else None)


if __name__ == "__main__":
    print("status:", get("/api/auth/status"))
    st, r = post("/api/auth/bootstrap", {
        "email": "admin@teamdoc.local",
        "name": "管理员",
        "password": "admin12345",
    })
    print(f"bootstrap -> {st}:", json.dumps(r, ensure_ascii=False))
