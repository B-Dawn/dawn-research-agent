@echo off
chcp 65001 >nul
REM 启动科研智能体 Web 应用（零依赖，标准库 http.server）
setlocal
set APP_DIR=%~dp0skills\research-agent\app
set PY=python
cd /d "%APP_DIR%"
echo 正在启动科研智能体 Web 服务...
"%PY%" web_app.py %*
endlocal
