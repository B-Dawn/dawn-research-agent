@echo off
chcp 65001 >nul 2>&1
setlocal

REM  科研智能体 - Windows 一键安装
REM  双击本文件即可。会以 Bypass 方式调用 install.ps1，绕过执行策略限制。

echo.
echo  科研智能体 Research Agent  v1.0.0  -  安装程序
echo  ==============================================
echo.

set "PS1=%~dp0install.ps1"

if not exist "%PS1%" (
    echo  [X] 找不到 install.ps1
    echo      请确认 install.bat 与 install.ps1 在同一目录。
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo  安装程序退出码: %RC%
) 
pause
exit /b %RC%
