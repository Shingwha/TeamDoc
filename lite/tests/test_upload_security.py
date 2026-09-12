"""上传安全:inline 白名单与 mime 服务端判定(第 0 批 0.2)。

背景:上传的 mime 原先完全采信客户端声明,且 ?inline=1 时以该 mime + inline 返回 ——
任何人上传一个 SVG(或 HTML)再点"预览",浏览器就在同源下执行其中的脚本,
中招的是点开它的管理员。本脚本实测修复后的行为。

验证点:
  1. 危险类型(svg / html / 未知)即使客户端伪声明 mime=image/png 且请求 inline=1,也强制 attachment
  2. 安全类型(png / pdf / text/plain / md)允许 inline
  3. 危险类型的响应 media_type 回落 octet-stream(防浏览器按扩展名嗅探)
  4. 上传接口不采信客户端 mime(服务端按扩展名判定)
  5. 全站安全响应头存在
  6. 文件列表 / 上传响应带 canInline / isText 标志

用法:先启动隔离实例,再设 TD_BASE 运行。
    TD_BASE=http://127.0.0.1:8137 python tests/test_upload_security.py
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("TD_BASE", "http://127.0.0.1:8123")
FAIL, PASS = [], []
SID = None


def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}" + (f"   <- {extra}" if extra and not cond else ""))


def _headers(resp) -> dict:
    """响应头小写化,避免 Content-Type / content-type 大小写差异"""
    return {k.lower(): v for k, v in resp.headers.items()}


def _parse(raw: bytes):
    """能解析成 JSON 就解析,否则原样返回字节(下载类响应不是 JSON)"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw


def call(method, path, body=None, ctype="application/json", raw_body=None, want_headers=False):
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None and ctype:
        req.add_header("Content-Type", ctype)
    if SID:
        req.add_header("Cookie", "td_sid=" + SID)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = _parse(r.read())
            return (r.status, out, _headers(r)) if want_headers else (r.status, out)
    except urllib.error.HTTPError as e:
        out = _parse(e.read())
        return (e.code, out, _headers(e)) if want_headers else (e.code, out)


def login():
    global SID
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"email": "admin@teamdoc.local",
                                                  "password": "admin12345"}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        sc = r.headers.get("Set-Cookie", "")
    SID = sc.split("td_sid=")[1].split(";")[0] if "td_sid=" in sc else None
    return bool(SID)


def upload(pid, filename, content, declared=None, folder_id=None):
    """raw body 上传。文件名在 query 上,请求体即文件内容。

    declared 参数保留但已无意义(端点不再接受客户端提供的 mime,也没有 part 头可以伪造),
    调用方仍传着它是为了让"攻击者伪声明 image/png"这个意图留在测试里可读 ——
    现在的真实攻击面只剩**文件名**。
    """
    qs = "?projectId=" + urllib.parse.quote(str(pid)) + "&name=" + urllib.parse.quote(filename)
    if folder_id:
        qs += "&folderId=" + urllib.parse.quote(str(folder_id))
    return call("POST", "/api/files/upload" + qs, raw_body=content,
                ctype=declared or "application/octet-stream")


def download(fid, inline):
    url = f"/api/files/{fid}/download" + ("?inline=1" if inline else "")
    return call("GET", url, want_headers=True)


def abort_upload(pid, filename, total_bytes=5 * 1024 * 1024, send_bytes=512 * 1024):
    """裸 socket 发一个 raw body 上传,发到一半直接断开连接。

    模拟"传大文件时网络断/用户关页面":服务端已写了部分字节到 data/files,
    但没有 DB 记录 —— 若不清理,这个文件就永久占着磁盘且界面里看不见。
    用裸 socket 是为了能在任意时刻切断(urllib 会一次性写完再等响应)。
    """
    parsed = urllib.parse.urlsplit(BASE)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = ("/api/files/upload?projectId=" + urllib.parse.quote(str(pid))
            + "&name=" + urllib.parse.quote(filename))
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Cookie: td_sid={SID}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {total_bytes}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        s = socket.create_connection((host, port), timeout=15)
        s.sendall(head)
        # 分块发送,便于在中间切断(一次 sendall 大块会被内核缓冲,断点不精确)
        chunk = b"x" * (64 * 1024)
        sent = 0
        while sent < send_bytes:
            s.sendall(chunk)
            sent += len(chunk)
        # 发完就断开,不等响应
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()
        return True
    except OSError:
        return False


SVG_XSS = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.domain)</script></svg>'
HTML_XSS = b'<html><body><script>alert(document.domain)</script></body></html>'
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001d7c8e1f80000000049454e44ae426082")


def main():
    print("=" * 60)
    print("上传安全:inline 白名单与 mime 服务端判定")
    print("=" * 60)
    if not login():
        print("登录失败,请先跑 tests/_bootstrap.py")
        return 1

    st, proj = call("POST", "/api/projects", {"name": "安全测试项目", "description": "s"})
    pid = proj["id"]

    print("\n=== 场景1:SVG 伪装成图片并要求 inline(核心 XSS 路径) ===")
    st, r = upload(pid, "evil.svg", SVG_XSS, declared="image/png")
    check("上传 svg(伪声明 image/png)成功", st == 200, str(r))
    check("mime 按扩展名判定为 svg(不采信客户端)", r.get("mime") == "image/svg+xml", str(r.get("mime")))
    check("上传响应 canInline=false", r.get("canInline") is False, str(r.get("canInline")))
    st, _, h = download(r["id"], inline=True)
    check("下载(inline=1)返回 200", st == 200, str(st))
    check("强制 attachment(而非 inline)", h.get("content-disposition", "").startswith("attachment"),
          h.get("content-disposition"))
    check("media_type 回落 octet-stream(防嗅探)",
          h.get("content-type", "").startswith("application/octet-stream"), h.get("content-type"))
    check("带 nosniff 头", h.get("x-content-type-options") == "nosniff", str(h.get("x-content-type-options")))

    print("\n=== 场景2:HTML 同样被拒 ===")
    st, r = upload(pid, "page.html", HTML_XSS, declared="text/html")
    check("上传 html 成功", st == 200, str(r))
    check("html 的 canInline=false", r.get("canInline") is False)
    st, _, h = download(r["id"], inline=True)
    check("html 强制 attachment", h.get("content-disposition", "").startswith("attachment"),
          h.get("content-disposition"))

    print("\n=== 场景3:未知类型不给 inline ===")
    st, r = upload(pid, "weird.xyzabc", b"whatever", declared="image/svg+xml")
    check("未知扩展名 → octet-stream", r.get("mime") == "application/octet-stream", str(r.get("mime")))
    check("未知类型 canInline=false", r.get("canInline") is False)
    st, _, h = download(r["id"], inline=True)
    check("未知类型强制 attachment", h.get("content-disposition", "").startswith("attachment"),
          h.get("content-disposition"))

    print("\n=== 场景4:安全类型仍可 inline(不能一刀切) ===")
    st, r = upload(pid, "pic.png", PNG_1PX, declared="image/png")
    check("上传 png 成功", st == 200, str(r))
    check("png 的 canInline=true", r.get("canInline") is True, str(r.get("canInline")))
    st, _, h = download(r["id"], inline=True)
    check("png 允许 inline", h.get("content-disposition", "").startswith("inline"),
          h.get("content-disposition"))
    check("png 的 media_type 保留 image/png", h.get("content-type", "").startswith("image/png"),
          h.get("content-type"))

    st, r = upload(pid, "doc.md", b"# hi\n", declared="application/octet-stream")
    check("md → text/markdown", r.get("mime") == "text/markdown", str(r.get("mime")))
    check("md 标记 isText=true(前端走站内预览)", r.get("isText") is True, str(r.get("isText")))

    st, r = upload(pid, "note.txt", b"plain text")
    check("txt → text/plain", r.get("mime") == "text/plain", str(r.get("mime")))
    st, _, h = download(r["id"], inline=True)
    check("txt 允许 inline", h.get("content-disposition", "").startswith("inline"),
          h.get("content-disposition"))

    print("\n=== 场景5:不带 inline 参数一律 attachment ===")
    st, r = upload(pid, "pic2.png", PNG_1PX, declared="image/png")
    st, _, h = download(r["id"], inline=False)
    check("png 默认下载为 attachment", h.get("content-disposition", "").startswith("attachment"),
          h.get("content-disposition"))

    print("\n=== 场景6:全站安全响应头 ===")
    st, _, h = call("GET", "/api/auth/status", want_headers=True)
    check("nosniff", h.get("x-content-type-options") == "nosniff", str(h.get("x-content-type-options")))
    check("X-Frame-Options", h.get("x-frame-options") == "SAMEORIGIN", str(h.get("x-frame-options")))
    check("Referrer-Policy", h.get("referrer-policy") == "same-origin", str(h.get("referrer-policy")))

    print("\n=== 场景7:文件列表带 canInline / isText ===")
    st, lst = call("GET", f"/api/files?project_id={pid}")
    by_name = {f["name"]: f for f in lst.get("files", [])}
    check("列表含 evil.svg", "evil.svg" in by_name, str(sorted(by_name)))
    check("列表 evil.svg 的 canInline=false", by_name.get("evil.svg", {}).get("canInline") is False)
    check("列表 pic.png 的 canInline=true", by_name.get("pic.png", {}).get("canInline") is True)

    print("\n=== 场景8:搜索结果同样带 canInline(编辑器 @ 浮层用) ===")
    st, s = call("GET", "/api/search?q=evil&type=files")
    hit = next((f for f in s.get("files", []) if f["name"] == "evil.svg"), None)
    check("搜到 evil.svg", hit is not None, str(s.get("files")))
    check("搜索结果 canInline=false", hit is not None and hit.get("canInline") is False, str(hit))
    check("搜索结果带项目名", hit is not None and hit.get("projectName") == "安全测试项目", str(hit))

    print("\n=== 场景9:客户端中途断开不留孤儿文件(原代码只清 _TooLarge 分支) ===")
    data_dir = os.environ.get("TD_DATA_DIR", "")
    if not data_dir:
        print("  SKIP  未设 TD_DATA_DIR,无法检查物理文件")
    else:
        files_dir = os.path.join(data_dir, "files")
        before = set(os.listdir(files_dir))
        check("半途断开的请求已发出", abort_upload(pid, "aborted.bin"))
        # 服务端读到断开时抛 ClientDisconnect → 异常路径 unlink,给清理留点时间
        orphan = None
        for _ in range(20):
            time.sleep(0.25)
            new = set(os.listdir(files_dir)) - before
            if not new:
                orphan = None
                break
            orphan = new
        check("未留下孤儿物理文件", not orphan, f"多出 {orphan}")

        print("\n=== 场景10:超限文件被拒且不留残留 ===")
        # 上限默认 20480MB(见 server/files.py),这里必须与实例实际值一致,
        # 否则会误判成"3MB 不会超限"而跳过。要测超限请用
        # MAX_UPLOAD_MB=1 启动实例并同时设 TD_MAX_UPLOAD_MB=1。
        limit_mb = int(os.environ.get("TD_MAX_UPLOAD_MB", "20480"))
        if limit_mb >= 3:
            print(f"  SKIP  服务端上限 {limit_mb}MB,3MB 不会超限"
                  f"(要测请用 MAX_UPLOAD_MB=1 启动实例,并设 TD_MAX_UPLOAD_MB=1)")
        else:
            before = set(os.listdir(files_dir))
            st, r = upload(pid, "big.bin", b"x" * (3 * 1024 * 1024))
            check("超限上传被拒(400)", st == 400, f"实际 {st}: {r}")
            time.sleep(0.5)
            orphan = set(os.listdir(files_dir)) - before
            check("未留下孤儿物理文件", not orphan, f"多出 {orphan}")

    print("\n=== 清理 ===")
    st, _ = call("DELETE", f"/api/projects/{pid}")
    check("测试项目已删除", st == 200, str(st))

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
