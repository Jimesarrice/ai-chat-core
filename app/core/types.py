"""核心数据类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """一条进站消息（来自任意通道）。"""
    channel: str                     # web | qq
    conv_id: str                     # web:<id> | qq:private:<uid> | qq:group:<gid>
    text: str
    images: list[str] = field(default_factory=list)   # 本地图片路径
    sender_name: str = ""
    source: str = "user"             # user | qq | speak-up | tool
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnContext:
    """一轮对话的组装结果（供日志与调试）。"""
    system_prompt: str = ""
    history_count: int = 0
    memory_injected: list[int] = field(default_factory=list)   # 记忆 id
    mood_note: str = ""
    tools_available: list[str] = field(default_factory=list)