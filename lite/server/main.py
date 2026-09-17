"""FastAPI 入口(构建文档 §9):挂路由、启动建表、托管静态页。

启动:
    cd lite/server
    uv sync
    uv run uvicorn main:app --host 0.0.0.0 --port 8000   # 必须单 worker

## 启动顺序(改动这里前先读完整段)

1. 日志装配 —— 后面每一步的失败都要能被记录
2. `backup.apply_pending_restore()` —— 待应用的恢复要**先于**建表:它替换整个库文件
   与 files/。若先跑 schema.init,库已被打开(甚至因结构不符直接失败)。
3. `schema.init()` —— 建表 + 结构自检。不预置任何账号,bootstrap 负责初始化。
4. 路由注册 → 静态托管(必须最后,否则 / 会吃掉所有 API 路径)
5. `backup.start_scheduler()` —— 唯一的后台线程
"""
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import admin
import auth
import backup
import config
import docs
import files
import logsetup
import projects
import schema
import models
import search
import trash
import watchdog
import ws

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR.parent / "web"  # 相对路径定位 ../web(§9.4)
if not WEB_DIR.is_dir():
    raise RuntimeError(f"前端目录不存在:{WEB_DIR} —— 请先构建 lite/web/ 静态页")

# 日志装配见 logsetup:调用方只入队,写出(控制台 + 轮转文件)在独立线程上 ——
# 事件循环不碰任何可能阻塞的 I/O(控制台被鼠标选中、磁盘卡住,都会把主路径拖死)。
# uvicorn 会在不同时机重配自己的 logger,故导入时与 __main__ 里各调一次。
logsetup.setup()

backup.apply_pending_restore()
schema.init(models.engine)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 停滞看门狗:事件循环卡住 >10s 就把所有线程栈转储到 logs/(见 watchdog.py)
    watchdog.start()
    yield


app = FastAPI(title="TeamDoc Lite", lifespan=_lifespan)


@app.exception_handler(HTTPException)
async def _http_error_handler(request: Request, exc: HTTPException):
    """契约错误原样下发({detail:{code,message}});401 额外清掉会话 Cookie。

    为什么 401 必须清 Cookie:会话失效之后(数据目录被重建、管理员强制下线、
    服务端换了库),浏览器仍会带着那条死凭据重放**每一个**请求 —— 每个视图撞一次
    401,而它自己永远不会消失,控制台刷屏、用户以为"功能全坏了"。清掉之后下一次
    请求就是干净的未登录态,只跳一次登录页。

    extra 里的现场数据(文档冲突的 currentContent)原样带出,前端据此做差异对比。
    """
    resp = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    for k, v in (exc.headers or {}).items():
        resp.headers[k] = v
    if exc.status_code == 401:
        resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """参数校验失败统一 400 VALIDATION(§7 统一约定)"""
    return JSONResponse(status_code=400,
                        content={"detail": {"code": "VALIDATION", "message": "参数校验失败"}})


# 路由注册顺序:auth → projects/docs → files/trash → search → admin → ws → 静态托管
app.include_router(auth.router)      # 认证 + 用户管理 + 同事目录 + PAT
app.include_router(projects.router)  # 项目 / 成员 / 发现广场
app.include_router(docs.router)      # 文档树 / 内容 / 版本 / 反链
app.include_router(files.router)     # 云空间(上传/下载/列表/移动/打包)
app.include_router(trash.router)     # 回收站(三资源的删/恢复/彻底删除 + 列表)
app.include_router(search.router)    # 搜索 + 最近动态
app.include_router(admin.router)     # 管理后台:项目总览 / 存储统计 / 孤儿清理 / 备份(仅管理员)
app.include_router(ws.router)        # 实时协同 WebSocket

# 静态托管必须放在所有 API 路由之后(§14.1)
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

# 定时备份线程(项目里唯一的后台任务):仅在配置了目标目录且间隔 > 0 时启动。
# 放在这里而不是模块顶层 import 时,是为了让"是否启动"取决于生效中的配置,
# 并且日志已经装配好(上面 logsetup.setup),启动信息能被记录下来。
backup.start_scheduler()

PORT = config.PORT

if __name__ == "__main__":
    # 支持直接 python main.py;单 worker(SQLite 单写者,§14.6)。
    # 传 app 对象而不是 "main:app":后者会让 uvicorn 把本文件再导入一遍
    # (__main__ 与 main 两份模块状态:建表、engine、日志 handler 各来一次)。
    # Config() 构造时会重配 uvicorn 自己的 logger,所以构造之后再装配一次。
    _config = uvicorn.Config(app, host="0.0.0.0", port=PORT, workers=1)
    logsetup.setup()
    uvicorn.Server(_config).run()
