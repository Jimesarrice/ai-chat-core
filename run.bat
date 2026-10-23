@echo off
rem 双击即可启动（Windows）。本文件为 GBK 编码（cmd 原生），改完请按 ANSI/GBK 保存。
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py %*
) else (
  echo 还没建虚拟环境，先执行这两条：
  echo    python -m venv .venv
  echo    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
pause
