"""TTS 服务：队列 + 合成 worker + 出口（网页推送 / QQ 语音条）。

队列移植自朗读桥的要点：
- 队列上限 6，满了丢最旧；
- 入队前先「去颜文字」清洗，洗空不排；
- 同文本在队列中不重复排；
- 合成超时判死（超时清扫），失败进历史并记 lastError；
- 停止 = 清队列 + 通知网页停声（服务器不做流式，点播即时合成）。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.channels.base import ChannelHub
from app.config.store import ConfigStore
from app.features.tts.engines import Qwen3TTSEngine, TTSError
from app.features.tts.text_clean import clean_for_tts
from app.logbus import get_logger

log = get_logger("app.tts.service")

QUEUE_MAX = 6
SYNTH_TIMEOUT = 120.0   # 单条合成超时（判死）
HISTORY_MAX = 20


def apply_device_flag(cmd: str, device: str) -> str:
    """把「GPU/CPU」选择落到启动命令上（先摘掉命令里已有的 --device）。"""
    cmd = re.sub(r"--device\s+\S+", "", cmd or "").strip()
    dev = {"gpu": "Vulkan0", "cpu": "CPU"}.get((device or "gpu").strip().lower(), "")
    return f"{cmd} --device {dev}" if dev else cmd


class TTSService:
    def __init__(self, store: ConfigStore, hub: ChannelHub, data_dir: Path) -> None:
        self.store = store
        self.hub = hub
        self.out_dir = data_dir / "tts"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.engine = Qwen3TTSEngine(store)
        self._queue: deque[dict[str, Any]] = deque()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._next_id = 1
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAX)
        self.last_error = ""
        self.synth_count = 0
        # 本地部署：托管进程（本地 TTS 服务）
        self._local_proc: asyncio.subprocess.Process | None = None
        self._local_logf = None
        self.local_log = self.out_dir / "local_server.log"

    # ---- 生命周期 ----
    async def start(self) -> None:
        self._task = asyncio.create_task(self._worker())
        cfg = self.store.get().tts
        log.info("TTS：%s｜后端 %s｜音色 %s｜模型 %s",
                 "开着" if cfg.enabled else "关着", cfg.backend, cfg.voice, cfg.model or "（未填）")
        if (cfg.enabled and cfg.backend in ("local_http", "qwentts")
                and cfg.local_auto_start and cfg.local_command.strip()):
            r = await self.local_start()
            log.info("本地 TTS 自动拉起：%s", r.get("message"))

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
        await self.local_stop()

    # ================================================== 本地部署（托管进程）

    async def local_start(self, command: str = "") -> dict:
        """按「本地启动命令」拉起本地 TTS 服务（qwen3-tts 服务 / qwentts.cpp / 任何兼容服务都能这样托管）。"""
        cfg = self.store.get().tts
        cmd = (command or cfg.local_command or "").strip()
        if not cmd:
            return {"ok": False, "message": "还没填「本地启动命令」（配置页・语音合成里那一格）"}
        cmd = apply_device_flag(cmd, cfg.local_device)
        if self._local_proc and self._local_proc.returncode is None:
            return {"ok": True, "message": f"本地服务已经在跑（pid {self._local_proc.pid}）"}
        try:
            self.local_log.parent.mkdir(parents=True, exist_ok=True)
            self._local_logf = open(self.local_log, "ab")
            self._local_logf.write(
                f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动：{cmd}\n".encode("utf-8"))
            self._local_logf.flush()
            env = {
                **os.environ,
                "PYTHONUTF8": "1",          # Windows 默认 GBK：中文一进管道就乱码（踩过）
                "PYTHONIOENCODING": "utf-8",
            }
            self._local_proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=self._local_logf,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.out_dir.parent.parent),   # 项目根目录，方便写相对路径
                env=env,
            )
        except Exception as e:
            return {"ok": False, "message": f"起不来：{e}"}
        await asyncio.sleep(1.5)
        proc = self._local_proc
        if proc.returncode is not None:   # 秒退：多半是命令/路径错
            tail = self.local_log_tail(600)
            self._close_local_log()
            self._local_proc = None
            return {"ok": False, "message": f"命令一启动就退出了（code {proc.returncode}）。日志尾巴：{tail}"}
        log.info("本地 TTS 服务已拉起：pid %s（%s）", proc.pid, cmd)
        return {"ok": True, "message": f"已拉起（pid {proc.pid}）。点「检测本地服务」确认端口通了，再「试合成一句」"}

    async def local_stop(self) -> dict:
        proc = self._local_proc
        if not proc or proc.returncode is not None:
            self._local_proc = None
            self._close_local_log()
            return {"ok": True, "message": "本来就没在跑"}
        pid = proc.pid
        try:
            if os.name == "nt":
                # 命令是 shell 起的，杀整棵进程树，别留孤儿
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            else:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=6)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception as e:
            log.warning("停止本地服务出错（继续收尾）：%s", e)
        self._local_proc = None
        self._close_local_log()
        log.info("本地 TTS 服务已停止（pid %s）", pid)
        return {"ok": True, "message": f"已停止（pid {pid}）"}

    def _close_local_log(self) -> None:
        if self._local_logf:
            try:
                self._local_logf.close()
            except OSError:
                pass
            self._local_logf = None

    def local_log_tail(self, n: int = 1200) -> str:
        try:
            if not self.local_log.exists():
                return ""
            data = self.local_log.read_bytes()[-max(200, n):]
            return data.decode("utf-8", "replace")
        except OSError:
            return ""

    async def local_probe(self) -> dict:
        """检测本地服务端口通不通（依次试 /health、/v1/models、/）。"""
        cfg = self.store.get().tts
        base = (cfg.base_url or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "message": "还没填「后端地址」（配置页・语音合成）"}
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
                            "message": f"有服务在听（{path} → HTTP {r.status_code}）{extra}。用「试合成一句」做真验证",
                            "url": base}
                except (httpx.ConnectError, httpx.TimeoutException):
                    continue
                except Exception:
                    continue
        return {"ok": False, "message": f"连不上 {base}：服务没起来？先「启动本地服务」，或检查地址/端口"}

    def _backend_from_log(self) -> str:
        """从服务日志里抠最近一次实际使用的推理后端（CPU / Vulkan0 …）。"""
        try:
            if not self.local_log.exists():
                return ""
            data = self.local_log.read_bytes()[-200_000:].decode("utf-8", "replace")
            hits = re.findall(r"Talker backend:\s*([A-Za-z0-9]+)", data)
            return hits[-1] if hits else ""
        except OSError:
            return ""

    def local_status(self) -> dict:
        cfg = self.store.get().tts
        proc = self._local_proc
        running = bool(proc and proc.returncode is None)
        return {
            "backend": self._backend_from_log(),
            "running": running,
            "pid": proc.pid if running else None,
            "command": cfg.local_command or "",
            "effective_command": apply_device_flag(cfg.local_command or "", cfg.local_device),
            "device": cfg.local_device or "gpu",
            "auto_start": bool(cfg.local_auto_start),
            "base_url": cfg.base_url or "",
            "model": cfg.model or "",
            "log_file": str(self.local_log),
            "log_tail": self.local_log_tail(1200),
        }

    # ---- 队列 ----
    def enqueue(self, text: str, *, conv_id: str = "", speak: bool = False) -> dict:
        """speak=True 为「网页点播」：合成完推给网页播放事件。"""
        cfg = self.store.get().tts
        if not cfg.enabled and not speak:
            return {"ok": False, "message": "TTS 总开关关着（配置页可打开）"}
        clean = clean_for_tts(text)
        if not clean:
            return {"ok": False, "message": "清洗后没剩下能念的字"}
        clean = clean[: max(50, int(cfg.max_chars))]
        for item in self._queue:
            if item["text"] == clean and item["conv_id"] == conv_id:
                return {"ok": False, "message": "这句已经在队列里了"}
        if len(self._queue) >= QUEUE_MAX:
            self._queue.popleft()
        item = {
            "id": self._next_id,
            "text": clean,
            "conv_id": conv_id,
            "speak": speak,
            "at": time.time(),
        }
        self._next_id += 1
        self._queue.append(item)
        log.info("排队 %s 条：%s", len(self._queue), clean[:40].replace("\n", " "))
        return {"ok": True, "queue": len(self._queue)}

    def clear(self) -> int:
        n = len(self._queue)
        self._queue.clear()
        self.hub.get("web") and self._notify_stop()
        return n

    def _notify_stop(self) -> None:
        web = self.hub.get("web")
        if web:
            web.emit({"type": "voice_stop"})

    def status(self) -> dict:
        return {
            "enabled": self.store.get().tts.enabled,
            "backend": self.store.get().tts.backend,
            "queued": len(self._queue),
            "synth_count": self.synth_count,
            "last_error": self.last_error,
            "history": list(self.history)[-10:],
        }

    # ---- worker ----
    async def _worker(self) -> None:
        while not self._stop.is_set():
            async with self._lock:
                item = self._queue.popleft() if self._queue else None
            if item is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                    return
                except asyncio.TimeoutError:
                    continue
            await self._process(item)

    async def _process(self, item: dict) -> None:
        cfg = self.store.get().tts
        stem = self.out_dir / f"{int(item['at'])}-{item['id']}"
        try:
            path = await asyncio.wait_for(
                self.engine.synthesize(item["text"], stem), timeout=SYNTH_TIMEOUT
            )
            self.synth_count += 1
            self.history.append({"id": item["id"], "text": item["text"][:60], "ok": True,
                                 "file": path.name, "at": item["at"]})
            log.info("合成完成：%s（%s 字）", path.name, len(item["text"]))
            await self._deliver(item, str(path))
            self._cleanup_old()
        except asyncio.TimeoutError:
            self.last_error = f"合成超时（{SYNTH_TIMEOUT:.0f}s）"
            log.warning("TTS %s", self.last_error)
            self.history.append({"id": item["id"], "text": item["text"][:60], "ok": False,
                                 "error": self.last_error, "at": item["at"]})
        except TTSError as e:
            self.last_error = str(e)
            log.warning("TTS 合成失败：%s", e)
            self.history.append({"id": item["id"], "text": item["text"][:60], "ok": False,
                                 "error": str(e)[:200], "at": item["at"]})
        except Exception as e:
            self.last_error = f"合成出错：{e}"
            log.exception("TTS 合成出错")
            self.history.append({"id": item["id"], "text": item["text"][:60], "ok": False,
                                 "error": str(e)[:200], "at": item["at"]})

    async def _deliver(self, item: dict, audio_path: str) -> None:
        cfg = self.store.get().tts
        qq_cfg = self.store.get().qq
        conv_id = item.get("conv_id") or ""
        if item.get("speak") and conv_id.startswith("web"):
            web = self.hub.get("web")
            if web:
                web.emit({"type": "voice", "conv_id": conv_id, "path": f"/media/tts/{Path(audio_path).name}"})
            return
        # QQ 语音条
        if conv_id.startswith("qq") and qq_cfg.reply_voice:
            ch = self.hub.get("qq")
            if ch:
                await ch.send_voice(conv_id, audio_path)

    def _cleanup_old(self) -> None:
        try:
            files = sorted(self.out_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
            files += sorted(self.out_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[100:]:
                f.unlink()
        except OSError:
            pass

    # ---- 点播（即时合成，不排队）----
    async def speak_now(self, text: str, conv_id: str = "") -> dict:
        """网页单条点播：立即合成并返回文件 URL。"""
        cfg = self.store.get().tts
        if not cfg.enabled:
            return {"ok": False, "message": "TTS 总开关关着（配置页可打开）"}
        clean = clean_for_tts(text)[: max(50, int(cfg.max_chars))]
        if not clean:
            return {"ok": False, "message": "清洗后没剩下能念的字"}
        stem = self.out_dir / f"say-{int(time.time() * 1000)}"
        try:
            path = await asyncio.wait_for(self.engine.synthesize(clean, stem), timeout=SYNTH_TIMEOUT)
            return {"ok": True, "url": f"/media/tts/{path.name}"}
        except TTSError as e:
            self.last_error = str(e)
            return {"ok": False, "message": str(e)}
        except asyncio.TimeoutError:
            return {"ok": False, "message": "合成超时"}
        except Exception as e:
            return {"ok": False, "message": f"合成出错：{e}"}

    async def test(self) -> dict:
        ok, detail = await self.engine.test()
        return {"ok": ok, "message": detail}