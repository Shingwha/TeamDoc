"""上传安全:inline 白名单与 mime 服务端判定。

背景:上传的 mime 原先完全采信客户端声明,且 ?inline=1 时以该 mime + inline 返回 ——
任何人上传一个 SVG(或 HTML)再点"预览",浏览器就在同源下执行其中的脚本,
中招的是点开它的管理员。本文件实测修复后的行为。

验证点:
  1. 危险类型(svg / html / 未知)即使客户端伪声明 mime=image/png 且请求 inline=1,也强制 attachment
  2. 安全类型(png / pdf / text/plain / md)允许 inline
  3. 危险类型的响应 media_type 回落 octet-stream(防浏览器按扩展名嗅探)
  4. 上传接口不采信客户端 mime(服务端按扩展名判定)
  5. 全站安全响应头存在
  6. 文件列表 / 搜索 / 上传响应带 canInline / isText 标志
  7. 客户端中途断开不留孤儿物理文件
  8. 超限文件被拒且不留残留(需以 TD_MAX_UPLOAD_MB=1 起测试实例,默认跳过)
"""
import time

import pytest

from _harness import abort_raw_upload

SVG_XSS = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.domain)</script></svg>'
HTML_XSS = b'<html><body><script>alert(document.domain)</script></body></html>'
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001d7c8e1f80000000049454e44ae426082")


def test_inline_whitelist_and_server_mime(base_url, admin):
    """场景 1-8 共用一个项目(7、8 要回看前面上传的文件),按序一条链。"""
    pid = admin.post("/api/projects", {"name": "安全测试项目", "description": "s"}).data["id"]

    def download(fid, inline):
        url = f"/api/files/{fid}/download" + ("?inline=1" if inline else "")
        return admin.get(url)

    # === 场景1:SVG 伪装成图片并要求 inline(核心 XSS 路径) ===
    r = admin.upload(pid, "evil.svg", SVG_XSS, ctype="image/png")
    assert r.status == 200, f"上传 svg(伪声明 image/png)成功: {r}"
    assert r.data.get("mime") == "image/svg+xml", \
        f"mime 按扩展名判定为 svg(不采信客户端): {r.data.get('mime')}"
    assert r.data.get("canInline") is False, f"上传响应 canInline=false: {r.data}"
    h = download(r.data["id"], inline=True).headers
    assert h.get("content-disposition", "").startswith("attachment"), \
        f"强制 attachment(而非 inline): {h.get('content-disposition')}"
    assert h.get("content-type", "").startswith("application/octet-stream"), \
        f"media_type 回落 octet-stream(防嗅探): {h.get('content-type')}"
    assert h.get("x-content-type-options") == "nosniff", \
        f"带 nosniff 头: {h.get('x-content-type-options')}"

    # === 场景2:HTML 同样被拒 ===
    r = admin.upload(pid, "page.html", HTML_XSS, ctype="text/html")
    assert r.status == 200, f"上传 html 成功: {r}"
    assert r.data.get("canInline") is False, "html 的 canInline=false"
    h = download(r.data["id"], inline=True).headers
    assert h.get("content-disposition", "").startswith("attachment"), \
        f"html 强制 attachment: {h.get('content-disposition')}"

    # === 场景3:未知类型不给 inline ===
    r = admin.upload(pid, "weird.xyzabc", b"whatever", ctype="image/svg+xml")
    assert r.data.get("mime") == "application/octet-stream", \
        f"未知扩展名 → octet-stream: {r.data.get('mime')}"
    assert r.data.get("canInline") is False, "未知类型 canInline=false"
    h = download(r.data["id"], inline=True).headers
    assert h.get("content-disposition", "").startswith("attachment"), \
        f"未知类型强制 attachment: {h.get('content-disposition')}"

    # === 场景4:安全类型仍可 inline(不能一刀切) ===
    r = admin.upload(pid, "pic.png", PNG_1PX, ctype="image/png")
    assert r.status == 200, f"上传 png 成功: {r}"
    assert r.data.get("canInline") is True, f"png 的 canInline=true: {r.data}"
    h = download(r.data["id"], inline=True).headers
    assert h.get("content-disposition", "").startswith("inline"), \
        f"png 允许 inline: {h.get('content-disposition')}"
    assert h.get("content-type", "").startswith("image/png"), \
        f"png 的 media_type 保留 image/png: {h.get('content-type')}"

    r = admin.upload(pid, "doc.md", b"# hi\n", ctype="application/octet-stream")
    assert r.data.get("mime") == "text/markdown", f"md → text/markdown: {r.data.get('mime')}"
    assert r.data.get("isText") is True, f"md 标记 isText=true(前端走站内预览): {r.data}"
    r = admin.upload(pid, "note.txt", b"plain text")
    assert r.data.get("mime") == "text/plain", f"txt → text/plain: {r.data.get('mime')}"
    h = download(r.data["id"], inline=True).headers
    assert h.get("content-disposition", "").startswith("inline"), \
        f"txt 允许 inline: {h.get('content-disposition')}"

    # === 场景5:不带 inline 参数一律 attachment ===
    r = admin.upload(pid, "pic2.png", PNG_1PX, ctype="image/png")
    h = download(r.data["id"], inline=False).headers
    assert h.get("content-disposition", "").startswith("attachment"), \
        f"png 默认下载为 attachment: {h.get('content-disposition')}"

    # === 场景6:全站安全响应头 ===
    h = admin.get("/api/auth/status").headers
    assert h.get("x-content-type-options") == "nosniff", "nosniff"
    assert h.get("x-frame-options") == "SAMEORIGIN", "X-Frame-Options"
    assert h.get("referrer-policy") == "same-origin", "Referrer-Policy"

    # === 场景7:文件列表带 canInline / isText ===
    lst = admin.get(f"/api/files?project_id={pid}").data
    by_name = {f["name"]: f for f in lst.get("files", [])}
    assert "evil.svg" in by_name, f"列表含 evil.svg: {sorted(by_name)}"
    assert by_name.get("evil.svg", {}).get("canInline") is False, "列表 evil.svg 的 canInline=false"
    assert by_name.get("pic.png", {}).get("canInline") is True, "列表 pic.png 的 canInline=true"

    # === 场景8:搜索结果同样带 canInline(编辑器 @ 浮层用) ===
    s = admin.get("/api/search?q=evil&type=files").data
    hit = next((f for f in s.get("files", []) if f["name"] == "evil.svg"), None)
    assert hit is not None, f"搜到 evil.svg: {s.get('files')}"
    assert hit.get("canInline") is False, f"搜索结果 canInline=false: {hit}"
    assert hit.get("projectName") == "安全测试项目", f"搜索结果带项目名: {hit}"


def test_aborted_upload_leaves_no_orphan(base_url, admin, data_dir):
    """场景9:客户端中途断开不留孤儿文件(原代码只清 _TooLarge 分支)。"""
    pid = admin.post("/api/projects", {"name": "断连测试项目"}).data["id"]
    files_dir = data_dir / "files"
    before = set(files_dir.iterdir())
    assert abort_raw_upload(base_url, admin.sid, pid, "aborted.bin",
                            total_bytes=5 * 1024 * 1024, send_bytes=512 * 1024), \
        "半途断开的请求已发出"
    # 服务端读到断开时抛 ClientDisconnect → 异常路径 unlink,给清理留点时间
    orphan = None
    for _ in range(20):
        time.sleep(0.25)
        new = set(files_dir.iterdir()) - before
        if not new:
            orphan = None
            break
        orphan = new
    assert not orphan, f"未留下孤儿物理文件: 多出 {orphan}"


def test_oversize_upload_rejected(base_url, admin, data_dir, max_upload_mb):
    """场景10:超限文件被拒且不留残留。"""
    if max_upload_mb >= 3:
        pytest.skip(f"服务端上限 {max_upload_mb}MB,3MB 不会超限;要测请设 TD_MAX_UPLOAD_MB=1 后再跑")
    pid = admin.post("/api/projects", {"name": "超限测试项目"}).data["id"]
    files_dir = data_dir / "files"
    before = set(files_dir.iterdir())
    try:
        r = admin.upload(pid, "big.bin", b"x" * (3 * 1024 * 1024))
        assert r.status == 400, f"超限上传被拒(400): 实际 {r.status}: {r.data}"
    except (ConnectionResetError, BrokenPipeError):
        # 服务端在读 body 之前就回 400 并关闭连接:客户端若还在发 body,连接会被 RST。
        # urllib 读不到那个 400 属客户端局限(浏览器能处理早响应),被拒结论不变,
        # 真正的硬断言是下面的磁盘检查:一个字节都不能落盘。
        pass
    time.sleep(0.5)
    orphan = set(files_dir.iterdir()) - before
    assert not orphan, f"未留下孤儿物理文件: 多出 {orphan}"
