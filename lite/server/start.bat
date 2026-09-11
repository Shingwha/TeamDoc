@echo off
rem TeamDoc Lite 一键启动:读取同目录 .env 配置;必须单 worker(SQLite 单写者,勿加 --workers)
rem 注意:本文件必须保存为 GBK/ANSI 编码,UTF-8 会在中文 Windows 的 cmd 下乱码。
cd /d "%~dp0"

rem 已有实例在跑就不再起第二个(检查 8000 端口监听)
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [TeamDoc] 端口 8000 已被监听,服务可能已在运行,直接访问 http://127.0.0.1:8000
    pause
    exit /b 0
)

echo [TeamDoc] 启动中... 浏览器访问 http://127.0.0.1:8000(在此窗口按 Ctrl+C 或直接关窗即停止)
"%USERPROFILE%\.local\bin\uv.exe" run uvicorn main:app --host 0.0.0.0 --port 8000 --env-file .env

rem 走到这说明服务退出了,停一下让人看清报错,别让窗口闪退
echo.
echo [TeamDoc] 服务已退出。若有报错请把上面的信息截图留存。
pause
