"""Web 通道：浏览器通过 WebSocket 连接聊天页/管理页，接收流式与推送事件。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.logbus import get_logger

log = get_logger("app.web")


class WebChannel:
    name = "web"

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()

    # ---- 连接管理（由 FastAPI 路由调用） ----
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    def client_count(self) -> int:
        return len(self._clients)

    def emit(self, event: dict[str, Any]) -> None:
        """向所有网页客户端广播事件。"""
        if not self._clients:
            return
        payload = json.dumps(event, ensure_ascii=False)
        dead: list[asyncio.Queue] = []
        for q in self._clients:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._clients.discard(q)

    # ---- Channel 协议 ----
    async def start(self) -> None:
        log.info("Web 通道就绪")

    async def stop(self) -> None:
        pass

    async def send_message(self, conv_id: str, text: str, images: list[str] | None = None,
                           meta: dict[str, Any] | None = None) -> None:
        """把一条消息推给网页（主动搭话 / 其它通道回显都会走这）。"""
        self.emit({
            "type": "message",
            "conv_id": conv_id,
            "text": text,
            "images": images or [],
            "meta": meta or {},
        })

    async def send_voice(self, conv_id: str, audio_path: str) -> None:
        self.emit({"type": "voice", "conv_id": conv_id, "path": audio_path})

    # ---- 流式辅助 ----
    def stream_delta(self, conv_id: str, text: str) -> None:
        self.emit({"type": "delta", "conv_id": conv_id, "text": text})

    def stream_tool(self, conv_id: str, name: str) -> None:
        self.emit({"type": "tool", "conv_id": conv_id, "name": name})

    def stream_end(self, conv_id: str, message: dict[str, Any]) -> None:
        self.emit({"type": "done", "conv_id": conv_id, "message": message})

    def error(self, conv_id: str, text: str) -> None:
        self.emit({"type": "error", "conv_id": conv_id, "text": text})