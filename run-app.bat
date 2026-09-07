@echo off
chcp 65001 >nul
REM 启动科研智能体 Web 应用（零依赖，标准库 http.server）
setlocal
set APP_DIR=%~dp0skills\research-agent\app
REM 依次尝试：PATH 中的 python → py 启动器 → Python 官方安装目录
set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" where py >nul 2>nul && set PY=py -3
if "%PY%"=="" (
  for /d %%P in ("%LocalAppData%\Programs\Python\Python*") do set PY=%%P\python.exe
)
if "%PY%"=="" (
  for /d %%P in ("C:\Python*") do set PY=%%P\python.exe
)
if "%PY%"=="" set PY=python
cd /d "%APP_DIR%"
echo 正在启动科研智能体 Web 服务（使用 %PY%）...
"%PY%" web_app.py %*
echo.
echo 服务已退出。若上方有报错信息，请将其反馈给维护者。
pause
