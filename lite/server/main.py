"""FastAPI 入口(构建文档 §9):挂路由、启动建表、托管静态页。

启动:
    cd lite/server
    uv sync
    uv run uvicorn main:app --host 0.0.0.0 --port 8000   # 必须单 worker
"""
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import docs
import files
import models
import search
import ws

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR.parent / "web"  # 相对路径定位 ../web(§9.4)
if not WEB_DIR.is_dir():
    raise RuntimeError(f"前端目录不存在:{WEB_DIR} —— 请先构建 lite/web/ 静态页")

# 1. 建表;不预置任何账号(初始化由 §7.1 bootstrap 完成)
models.Base.metadata.create_all(models.engine)

app = FastAPI(title="TeamDoc Lite")


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """参数校验失败统一 400 VALIDATION(§7 统一约定)"""
    return JSONResponse(status_code=400,
                        content={"detail": {"code": "VALIDATION", "message": "参数校验失败"}})


# 2. 路由注册顺序:auth → users → projects/docs → files → search → ws → 静态托管
app.include_router(auth.router)   # 认证 + 用户管理 + TOTP + PAT
app.include_router(docs.router)   # 项目 / 成员 / 文档
app.include_router(files.router)  # 云空间
app.include_router(search.router)  # 搜索
app.include_router(ws.router)     # 实时协同 WebSocket

# 3. 静态托管必须放在所有 API 路由之后(§14.1)
# 静态资源统一 Cache-Control: no-cache——浏览器每次携 ETag 重验证(未变 304,几乎零开销),
# 避免启发式缓存导致改版后用户端仍跑旧 JS/CSS
@app.middleware("http")
async def _static_no_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith(("/css/", "/js/")) or request.url.path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

PORT = int(os.environ.get("PORT", "8000"))

if __name__ == "__main__":
    # 支持直接 python main.py;单 worker(SQLite 单写者,§14.6)
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, workers=1)
