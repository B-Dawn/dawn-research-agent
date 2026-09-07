@echo off
chcp 65001 >nul 2>&1
setlocal

REM  科研智能体 - 卸载
REM  会把已安装的 skill 重命名为 .bak-时间戳，不会直接删除。
REM  画像文件 %USERPROFILE%\.workbuddy\profile\ 会保留。

echo.
echo  科研智能体 - 卸载程序
echo  ==============================================
echo.

set "PS1=%~dp0install.ps1"

if not exist "%PS1%" (
    echo  [X] 找不到 install.ps1
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Uninstall

echo.
pause
exit /b 0
