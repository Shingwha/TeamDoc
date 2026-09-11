"""FastAPI 入口(构建文档 §9):挂路由、启动建表、托管静态页。

启动:
    cd lite/server
    uv sync
    uv run uvicorn main:app --host 0.0.0.0 --port 8000   # 必须单 worker
"""
import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import admin
import auth
import backup
import docs
import files
import schema
import models
import search
import ws

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR.parent / "web"  # 相对路径定位 ../web(§9.4)
if not WEB_DIR.is_dir():
    raise RuntimeError(f"前端目录不存在:{WEB_DIR} —— 请先构建 lite/web/ 静态页")

# 根日志只在这里配置一次:uvicorn 自带的 logger 是 propagate=False 的,互不干扰。
# 没有它,backup 的 INFO 级日志会因根 logger 无 handler 而静默丢弃 ——
# 定时备份最怕的就是"看起来在跑,其实早就不备份了"。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# 1. 恢复必须在建表/自检**之前**应用:它要替换整个库文件与 files/,
#    若先 schema.init() 就已经打开了数据库连接、甚至因结构不符直接失败。
backup.apply_pending_restore()

# 2. 建表 + 结构自检(唯一来源是 models.py;漂移直接启动失败,见 schema.py)
#    不预置任何账号(初始化由 §7.1 bootstrap 完成)
schema.init(models.engine)

app = FastAPI(title="TeamDoc Lite")


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """参数校验失败统一 400 VALIDATION(§7 统一约定)"""
    return JSONResponse(status_code=400,
                        content={"detail": {"code": "VALIDATION", "message": "参数校验失败"}})


# 2. 路由注册顺序:auth → users → projects/docs → files → search → admin → ws → 静态托管
app.include_router(auth.router)   # 认证 + 用户管理 + 同事目录 + PAT
app.include_router(docs.router)   # 项目 / 成员 / 文档
app.include_router(files.router)  # 云空间
app.include_router(search.router)  # 搜索
app.include_router(admin.router)  # 管理后台:存储统计 / 孤儿清理 / 备份(仅管理员)
app.include_router(ws.router)     # 实时协同 WebSocket

# 3. 静态托管必须放在所有 API 路由之后(§14.1)
# 静态资源统一 Cache-Control: no-cache——浏览器每次携 ETag 重验证(未变 304,几乎零开销),
# 避免启发式缓存导致改版后用户端仍跑旧 JS/CSS。vendor/ 同为本地第三方资源,一并纳入
# (文件名不含内容哈希,长缓存会让升级后的资源无法生效)
@app.middleware("http")
async def _static_no_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith(("/css/", "/js/", "/vendor/")) or request.url.path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-cache"
    # 基础安全响应头(全站):
    # - nosniff:禁掉按内容嗅探类型 —— 上传的未知类型文件即使被直接打开也不会被当页面渲染
    # - X-Frame-Options:防点击劫持(本应用无被嵌 iframe 的需求)
    # - Referrer-Policy:内网地址不外泄给外部站点(正文里的外链点击时)
    # 未上 CSP:index.html 有内联防闪烁脚本、视图大量内联 style,需 nonce/hash 改造,收益不抵返工
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

# 4. 定时备份线程(项目里唯一的后台任务):仅在配置了目标目录且间隔 > 0 时启动。
#    放在这里而不是模块顶层 import 时,是为了让"是否启动"取决于生效中的配置,
#    并且日志已经配置好(上面 basicConfig),启动信息能被记录下来。
backup.start_scheduler()

PORT = int(os.environ.get("PORT", "8000"))

if __name__ == "__main__":
    # 支持直接 python main.py;单 worker(SQLite 单写者,§14.6)
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, workers=1)
