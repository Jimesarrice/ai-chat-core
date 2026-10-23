"""托管的本地服务进程（llama-server / tts-server 共用）。

- start：shell 起命令、stdout/stderr 落日志、UTF-8 钉死、1.5 秒内秒退视为失败并回日志尾巴；
- stop：Windows 用 taskkill /T /F 杀整棵进程树（不留孤儿）；
- status/log_tail：给界面看；
- probe_service：HTTP 探测（/health → /v1/models → /），回人话。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.logbus import get_logger

log = get_logger("app.proc")


class ManagedProc:
    def __init__(self, name: str, log_path: Path, cwd: Path) -> None:
        self.name = name
        self.log_path = log_path
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._logf = None

    # ---- 状态 ----
    @property
    def running(self) -> bool:
        return bool(self._proc and self._proc.returncode is None)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self.running else None

    def log_tail(self, n: int = 1200) -> str:
        try:
            if not self.log_path.exists():
                return ""
            data = self.log_path.read_bytes()[-max(200, n):]
            return data.decode("utf-8", "replace")
        except OSError:
            return ""

    # ---- 启停 ----
    async def start(self, command: str) -> dict:
        command = (command or "").strip()
        if not command:
            return {"ok": False, "message": f"还没填启动命令（配置页里那一格）"}
        if self.running:
            return {"ok": True, "message": f"{self.name} 已经在跑（pid {self._proc.pid}）"}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._logf = open(self.log_path, "ab")
            self._logf.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 {self.name}：{command}\n".encode("utf-8"))
            self._logf.flush()
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            self._proc = await asyncio.create_subprocess_shell(
                command,
                stdout=self._logf,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.cwd),
                env=env,
            )
        except Exception as e:
            return {"ok": False, "message": f"起不来：{e}"}
        await asyncio.sleep(1.5)
        if self._proc.returncode is not None:  # 秒退：多半是命令/路径错
            rc = self._proc.returncode
            tail = self.log_tail(600)
            self._close_log()
            self._proc = None
            return {"ok": False, "message": f"命令一启动就退出了（code {rc}）。日志尾巴：{tail}"}
        log.info("%s 已拉起：pid %s", self.name, self._proc.pid)
        return {"ok": True, "message": f"{self.name} 已拉起（pid {self._proc.pid}）"}

    async def stop(self) -> dict:
        proc = self._proc
        if not proc or proc.returncode is not None:
            self._proc = None
            self._close_log()
            return {"ok": True, "message": f"{self.name} 本来就没在跑"}
        pid = proc.pid
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            else:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception as e:
            log.warning("停止 %s 出错（继续收尾）：%s", self.name, e)
        self._proc = None
        self._close_log()
        log.info("%s 已停止（pid %s）", self.name, pid)
        return {"ok": True, "message": f"{self.name} 已停止（pid {pid}）"}

    def _close_log(self) -> None:
        if self._logf:
            try:
                self._logf.close()
            except OSError:
                pass
            self._logf = None


async def probe_service(base_url: str) -> dict:
    """探测一个本地服务端口（/health → /v1/models → /）。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "message": "还没填服务地址"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(6.0)) as cx:
        for path in ("/health", "/v1/models", "/"):
            try:
                r = await cx.get(base + path)
                if r.status_code >= 500:
                    continue
                extra = ""
                if path == "/v1/models":
                    try:
                        models = [str(m.get("id", "")) for m in (r.json().get("data") or [])]
                        if models:
                            extra = "；模型：" + "、".join(models[:5])
                    except Exception:
                        pass
                return {"ok": True,
                        "message": f"有服务在听（{path} → HTTP {r.status_code}）{extra}",
                        "url": base}
            except (httpx.ConnectError, httpx.TimeoutException):
                continue
            except Exception:
                continue
    return {"ok": False, "message": f"连不上 {base}：服务没起来？先点「启动」，或检查地址/端口"}