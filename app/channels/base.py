"""通道抽象：Web 与 QQ 都是「一条收消息进、一条回复出」的通道。

conv_id 命名约定：
  web:<uuid>              —— 网页会话
  qq:private:<user_id>    —— QQ 私聊
  qq:group:<group_id>     —— QQ 群
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol


class Channel(Protocol):
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_message(self, conv_id: str, text: str, images: list[str] | None = None,
                           meta: dict[str, Any] | None = None) -> None: ...
    async def send_voice(self, conv_id: str, audio_path: str) -> None: ...


class ChannelHub:
    """通道注册表 + 出站分发。"""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self.on_inbound: Callable[[Any], Awaitable[None]] | None = None

    def register(self, channel: Channel) -> None:
        self._channels[channel.name] = channel

    def get(self, name: str) -> Channel | None:
        return self._channels.get(name)

    def channel_of(self, conv_id: str) -> Channel | None:
        return self._channels.get(conv_id.split(":", 1)[0])

    async def start_all(self) -> None:
        for ch in self._channels.values():
            await ch.start()

    async def stop_all(self) -> None:
        for ch in self._channels.values():
            try:
                await ch.stop()
            except Exception:
                pass

    async def deliver(self, conv_id: str, text: str, images: list[str] | None = None,
                      meta: dict[str, Any] | None = None) -> bool:
        ch = self.channel_of(conv_id)
        if ch is None:
            return False
        await ch.send_message(conv_id, text, images or [], meta or {})
        return True