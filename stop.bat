@echo off
rem 停止 ai-chat-core（读 datapp.pid，连同它拉起的本地服务一起停）。本文件为 GBK 编码。
cd /d "%~dp0"
if not exist datapp.pid goto nopid
set /p APP_PID=<datapp.pid
echo 停掉进程 %APP_PID% ...
taskkill /F /T /PID %APP_PID% >/dev/null 2>/dev/null
del datapp.pid >/dev/null 2>/dev/null
echo 已停。
goto end
:nopid
echo 没找到 datapp.pid —— 应用可能没在跑，或不是用 run.py 启动的。
:end
pause
