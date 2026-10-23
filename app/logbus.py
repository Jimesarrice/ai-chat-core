"""日志总线：Python logging → 文件 + 内存环形缓冲 → Web UI (SSE)。

设计：
- 任何模块用标准 `logging.getLogger("app.xxx")` 打日志；
- `LogBusHandler` 把每条记录同时推进环形缓冲，供配置/日志 UI 实时查看；
- 文件侧使用 RotatingFileHandler；日志级别可在配置页里热切换。
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class LogEntry:
    __slots__ = ("id", "ts", "level", "logger", "message")

    def __init__(self, idx: int, ts: str, level: str, logger: str, message: str) -> None:
        self.id = idx
        self.ts = ts
        self.level = level
        self.logger = logger
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
        }


class LogBus:
    """线程安全环形缓冲 + asyncio 订阅广播。"""

    def __init__(self, ring_size: int = 2000) -> None:
        self._ring: deque[LogEntry] = deque(maxlen=ring_size)
        self._lock = threading.Lock()
        self._next_id = 1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._threshold = logging.INFO

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_threshold(self, level: int) -> None:
        """SSE 推送的最低级别（UI 过滤用，文件日志级别另算）。"""
        self._threshold = level

    # ---- 写入（可在任意线程调用） ----
    def add(self, level: str, logger: str, message: str) -> None:
        with self._lock:
            idx = self._next_id
            self._next_id += 1
            entry = LogEntry(idx, datetime.now().strftime(DATE_FORMAT), level, logger, message)
            self._ring.append(entry)
            subs = list(self._subscribers)
        if not subs or self._loop is None:
            return
        # 跨线程投递到事件循环
        for q in subs:
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, entry)
            except RuntimeError:
                pass

    # ---- 读取 ----
    def snapshot(self, after_id: int = 0, level: str | None = None, limit: int = 500) -> list[dict]:
        """返回 id > after_id 的最近 limit 条（可选级别过滤）。"""
        min_level = _level_of(level) if level else 0
        with self._lock:
            items = [e for e in self._ring if e.id > after_id]
        items = [e for e in items if _level_of(e.level) >= min_level]
        items = items[-limit:]
        return [e.to_dict() for e in items]

    # ---- SSE 订阅 ----
    async def subscribe(self) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        try:
            while True:
                entry = await q.get()
                yield entry.to_dict()
        finally:
            self._subscribers.discard(q)

    def subscriber_count(self) -> int:
        return len(self._subscribers)


_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _level_of(name: str) -> int:
    return _LEVELS.get(str(name).upper(), 0)


class LogBusHandler(logging.Handler):
    def __init__(self, bus: LogBus) -> None:
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            self.bus.add(record.levelname, record.name, msg)
        except Exception:  # 日志系统自身绝不能让主流程崩溃
            pass


# ---- 全局单例 ----
log_bus = LogBus()

_configured = False


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """初始化根日志：控制台 + 轮转文件 + LogBus。幂等。"""
    global _configured
    root = logging.getLogger()
    root.setLevel(_level_of(level) or logging.INFO)
    if _configured:
        set_log_level(level)
        return
    _configured = True

    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    fh = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    bus_h = LogBusHandler(log_bus)
    bus_h.setLevel(logging.DEBUG)

    root.handlers.clear()
    root.addHandler(ch)
    root.addHandler(fh)
    root.addHandler(bus_h)

    # 降低第三方库噪音
    for noisy in ("httpx", "httpcore", "uvicorn.access", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def set_log_level(level: str) -> None:
    lv = _level_of(level) or logging.INFO
    logging.getLogger().setLevel(lv)
    log_bus.add("INFO", "app.logbus", f"日志级别切换为 {level.upper()}")


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("app"):
        name = f"app.{name}"
    return logging.getLogger(name)