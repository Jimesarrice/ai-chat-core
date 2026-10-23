"""OpenAI 兼容 LLM 客户端。

本地 llama.cpp（llama-server，Vulkan 后端）与云端 API 都走这一条路径——
llama-server 暴露的正是 OpenAI 兼容接口，所以「双路径」在客户端层是统一的，
差别只落在 provider 配置（base_url / api_key / 额外请求体 / 是否支持多模态）。

支持：
- 流式输出（SSE）；
- 多模态消息（text + image_url，本地图片自动编码为 data URL）；
- 工具调用（function calling，含流式分片聚合）；
- extra_body 透传（用于「低思考模式」等厂商专有参数）；
- 统一错误处理与重试。
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from app.logbus import get_logger

log = get_logger("app.llm")

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 单张图片上限 10MB


class LLMError(Exception):
    """带人话的模型调用错误。"""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


# ---------------------------------------------------------------- 消息构造

def text_message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": text}


def multimodal_message(role: str, text: str, image_paths: list[str] | None = None) -> dict[str, Any]:
    """构造多模态消息；图片可以是本地路径、http(s) URL 或 data URL。"""
    if not image_paths:
        return {"role": role, "content": text}
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for p in image_paths:
        url = to_image_url(p)
        if url is None:
            parts.append({"type": "text", "text": f"[图片无法读取：{p}，已跳过]"})
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": role, "content": parts}


def to_image_url(path_or_url: str) -> str | None:
    s = str(path_or_url).strip()
    if s.startswith(("http://", "https://", "data:")):
        return s
    try:
        p = Path(s)
        if not p.is_file():
            return None
        data = p.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            log.warning("图片 %s 超过 10MB，跳过", p.name)
            return None
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        log.warning("读取图片 %s 失败：%s", path_or_url, e)
        return None


def message_text(msg: dict[str, Any]) -> str:
    """从消息里抽出纯文本（多模态时拼接文本部分）。"""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(part.get("text", "") for part in c if part.get("type") == "text")
    return ""


# ---------------------------------------------------------------- 流式事件

@dataclass
class StreamEvent:
    type: str                    # delta | tool_start | done | error
    text: str = ""               # delta: 增量文本
    tool_name: str = ""          # tool_start: 开始调用某个工具
    message: dict[str, Any] = field(default_factory=dict)  # done: 完整 assistant 消息
    error: str = ""              # error: 人话错误
    usage: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 客户端

class OpenAICompatClient:
    """对任何 OpenAI 兼容端点（llama-server / DashScope 兼容模式 / vLLM / 各类中转）的客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout_read: float = 600.0,
        extra_body: dict[str, Any] | None = None,
        name: str = "llm",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.extra_body = dict(extra_body or {})
        self.timeout = httpx.Timeout(connect=15.0, read=timeout_read, write=60.0, pool=30.0)

    # ---- 内部 ----
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _url(self, path: str) -> str:
        # base_url 允许写成 http://host:port 或 http://host:port/v1
        if self.base_url.endswith("/v1"):
            base = self.base_url[:-3]
        else:
            base = self.base_url
        return f"{base}/v1{path}"

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as cx:
                r = await cx.get(self._url("/models"), headers=self._headers())
                r.raise_for_status()
                data = r.json().get("data", [])
                return sorted(m.get("id", "") for m in data if m.get("id"))
        except Exception as e:
            log.debug("[%s] 获取模型列表失败：%s", self.name, e)
            return []

    async def health(self) -> tuple[bool, str]:
        """连通性自检，返回 (是否可用, 人话说明)。"""
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as cx:
                r = await cx.get(self._url("/models"), headers=self._headers())
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                models = [m.get("id", "") for m in r.json().get("data", [])]
                brief = ", ".join(models[:5]) or "（未列出）"
                return True, f"连通 ✓ {ms}ms，模型：{brief}"
            if r.status_code == 401:
                return False, f"鉴权失败（HTTP 401）——检查 API Key（{ms}ms）"
            return False, f"HTTP {r.status_code}（{ms}ms）"
        except httpx.ConnectError:
            return False, "连不上——确认地址端口正确、服务已启动"
        except httpx.TimeoutException:
            return False, "连接超时"
        except Exception as e:
            return False, f"自检失败：{e}"

    # ---- 请求体 ----
    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        stream: bool,
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
        extra_body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if max_tokens:
            payload["max_tokens"] = max_tokens
        # extra_body：先给 provider 级默认（如低思考开关），调用级覆盖
        merged = {**self.extra_body, **(extra_body or {})}
        for k, v in merged.items():
            payload[k] = v
        return payload

    # ---- 流式对话（核心） ----
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        retries: int = 1,
    ) -> AsyncIterator[StreamEvent]:
        payload = self._payload(
            messages, model, stream=True, tools=tools,
            temperature=temperature, top_p=top_p, max_tokens=max_tokens, extra_body=extra_body,
        )
        attempt = 0
        while True:
            got_any = False
            try:
                async for ev in self._stream_once(payload):
                    got_any = True
                    yield ev
                    if ev.type in ("done", "error"):
                        return
                # 流结束但没收到 done/error（极少见）
                yield StreamEvent(type="error", error="流意外中断（未收到结束标记）")
                return
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if attempt < retries:
                    attempt += 1
                    log.warning("[%s] 连接失败，重试 %s/%s：%s", self.name, attempt, retries, e)
                    await asyncio.sleep(1.0 * attempt)
                    continue
                yield StreamEvent(type="error", error=f"连不上模型服务（{self.base_url}）。请确认本地 llama-server 已启动，或云端地址/网络可用。")
                return
            except httpx.ReadTimeout:
                yield StreamEvent(type="error", error="模型迟迟没有回应（读取超时）。")
                return
            except LLMError as e:
                yield StreamEvent(type="error", error=str(e))
                return
            except Exception as e:
                if got_any and attempt < retries:
                    attempt += 1
                    log.warning("[%s] 流中断，重试 %s/%s：%s", self.name, attempt, retries, e)
                    await asyncio.sleep(0.8 * attempt)
                    continue
                log.exception("[%s] 调用异常", self.name)
                yield StreamEvent(type="error", error=f"调用出错：{e}")
                return

    async def _stream_once(self, payload: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        announced: set[int] = set()
        finish_reason = ""
        usage: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=self.timeout) as cx:
            async with cx.stream(
                "POST", self._url("/chat/completions"),
                headers=self._headers(), json=payload,
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")[:600]
                    if r.status_code == 401:
                        raise LLMError("鉴权失败（HTTP 401）：检查 API Key。", status=401, body=body)
                    if r.status_code == 404:
                        raise LLMError(
                            f"HTTP 404：端点或模型名不存在（model={payload.get('model')}）。",
                            status=404, body=body,
                        )
                    if r.status_code == 400:
                        raise LLMError(f"请求被拒绝（HTTP 400）：{body}", status=400, body=body)
                    raise LLMError(f"HTTP {r.status_code}：{body}", status=r.status_code, body=body)

                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr
                        piece = delta.get("content")
                        if piece:
                            text_parts.append(piece)
                            yield StreamEvent(type="delta", text=piece)
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = tool_calls.setdefault(
                                idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["function"]["arguments"] += fn["arguments"]
                            if idx not in announced and slot["function"]["name"]:
                                announced.add(idx)
                                yield StreamEvent(type="tool_start", tool_name=slot["function"]["name"])

        final: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts),
        }
        if tool_calls:
            final["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        yield StreamEvent(type="done", message=final, usage=usage)

    # ---- 一次性（工具类任务用，如记忆提炼/心情判断） ----
    async def chat_once(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        retries: int = 1,
    ) -> dict[str, Any]:
        """返回 assistant 消息 dict：{"role","content",...}；出错抛 LLMError。"""
        last_err = "未知错误"
        async for ev in self.chat_stream(
            messages, model, temperature=temperature, max_tokens=max_tokens,
            extra_body=extra_body, retries=retries,
        ):
            if ev.type == "done":
                return ev.message
            if ev.type == "error":
                last_err = ev.error
        raise LLMError(last_err)