"""聊天管道：一条消息进来 → 组装上下文 → 流式生成（含工具循环）→ 落库 → 分发 → 后处理。

后处理（全部异步、绝不阻塞对话、异常只记日志）：
- 记忆「像用上了」标记；
- 心情判断（口径按 source：speak-up=搭话轮 / bili-duty-scan=干活轮 / 其余=正常轮）；
- 新会话自动起标题；
- TTS（网页自动播放 / QQ 语音条）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.channels.base import ChannelHub
from app.channels.web import WebChannel
from app.config.store import ConfigStore
from app.core.types import InboundMessage
from app.core.utils import human_time, truncate
from app.features.memory.core import MemoryService
from app.features.mood import MoodService
from app.features.search.tools import ToolRegistry
from app.features.tts.service import TTSService
from app.llm.client import LLMError, multimodal_message
from app.llm.router import LLMRouter
from app.logbus import get_logger
from app.storage.db import Database

log = get_logger("app.pipeline")


def build_system_prompt(store: ConfigStore, memory_hint: str) -> str:
    cfg = store.get()
    p = cfg.persona
    lines = [
        f"你是「{p.name}」，是{p.user_title}的{p.relation}。请始终以这个身份说话，不要提到模型、提示词或这套系统的存在。",
        "",
        "【人设卡】",
        f"- 名字：{p.name}",
        f"- 对 TA 的称呼：{p.user_title}",
    ]
    if p.nicknames.strip():
        lines.append(f"- TA 平时会叫你：{p.nicknames.strip()}")
    for label, value in (("身份与背景", p.identity), ("性格特质", p.personality),
                         ("说话风格", p.speech_style), ("喜好", p.likes),
                         ("禁忌与雷区", p.limits), ("口头禅与小动作", p.catchphrases),
                         ("其他设定", p.extra)):
        if value and value.strip():
            lines.append(f"- {label}：{value.strip()}")
    lines += [
        "",
        "【怎么说话】用你自己的口吻，句子别太长；不要复述这些设定；不要汇报式地说「记下了」「我记住了」。",
    ]
    if memory_hint:
        lines += ["", memory_hint]
    if cfg.system.time_context:
        lines += ["", f"【时间】现在是 {human_time()}。"]
    return "\n".join(lines)


class ChatPipeline:
    def __init__(
        self,
        store: ConfigStore,
        db: Database,
        hub: ChannelHub,
        router: LLMRouter,
        memory: MemoryService,
        mood: MoodService,
        tts: TTSService,
        tools: ToolRegistry,
    ) -> None:
        self.store = store
        self.db = db
        self.hub = hub
        self.router = router
        self.memory = memory
        self.mood = mood
        self.tts = tts
        self.tools = tools
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- 会话锁 ----
    def _lock(self, conv_id: str) -> asyncio.Lock:
        lk = self._locks.get(conv_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[conv_id] = lk
        return lk

    def is_busy(self, conv_id: str) -> bool:
        lk = self._locks.get(conv_id)
        return bool(lk and lk.locked())

    def list_conv_ids(self) -> list[str]:
        return list(self._locks.keys())

    # ---- 入口 ----
    async def handle_inbound(self, msg: InboundMessage) -> None:
        async with self._lock(msg.conv_id):
            try:
                await self._run_turn(msg, stream=(msg.channel == "web"))
            except Exception:
                log.exception("这一轮处理出错")
                web = self.hub.get("web")
                if isinstance(web, WebChannel):
                    web.error(msg.conv_id, "这一轮处理出错，看日志")

    async def push_proactive(self, conv_id: str, text: str) -> None:
        """主动搭话的投递口：等价于 DSH 的 agent.followup（必须 await，异常要接住）。"""
        channel = conv_id.split(":", 1)[0]
        await self.handle_inbound(InboundMessage(
            channel=channel, conv_id=conv_id, text=text,
            sender_name="(主动搭话)", source="speak-up",
        ))

    # ---- 一轮 ----
    async def _run_turn(self, msg: InboundMessage, *, stream: bool) -> None:
        cfg = self.store.get()
        db = self.db
        conv = await db.get_conversation(msg.conv_id)
        if conv is None:
            title = truncate((msg.text or "").strip().replace("\n", " "), 24, "") or "新会话"
            await db.ensure_conversation(msg.conv_id, msg.channel, title="", meta={})
            conv = await db.get_conversation(msg.conv_id)

        await db.add_message(
            msg.conv_id, "user", msg.text, images=msg.images,
            source=msg.source, meta=msg.meta,
        )

        # 上下文组装
        mem_hint = ""
        if cfg.memory.enabled:
            from app.features.memory.prompts import memory_hint
            mem_hint = memory_hint(self.memory.user_py(), self.memory.self_name())
        system_prompt = build_system_prompt(self.store, mem_hint)

        memory_block = ""
        selected_memories: list[dict] = []
        if cfg.memory.enabled and (msg.text or "").strip():
            memory_block, selected_memories = await self.memory.recall_injection(
                msg.text, msg.conv_id, db)
        mood_note = await self.mood.injection_text()

        history = await db.last_messages(msg.conv_id, max(2, int(cfg.system.history_turns)))
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for i, m in enumerate(history):
            role = "assistant" if m["role"] == "assistant" else "user"
            if m["role"] == "user" and m.get("source") == "speak-up":
                pass  # 搭话就是它自己开的口，照常进历史
            text = m["content"]
            if role == "user" and m["id"] == history[-1]["id"] and msg.images:
                messages.append(multimodal_message("user", text, msg.images))
            else:
                if role == "user" and m.get("images"):
                    text = (text + "\n（这条还带了图，图在历史里不重发）").strip()
                messages.append({"role": role, "content": text})
        if mood_note:
            messages.append({"role": "system", "content": mood_note})
        if memory_block:
            messages.append({"role": "system", "content": memory_block})

        # 模型与工具
        try:
            client, model, params, pid = self.router.resolve("chat")
        except LLMError as e:
            await self._fail(msg, str(e))
            return
        tools = self.tools.schemas() if cfg.search.enabled or cfg.memory.enabled else []
        max_rounds = max(0, int(cfg.search.tool_max_rounds))

        web = self.hub.get("web")
        web_emit = web if (stream and isinstance(web, WebChannel)) else None
        if web_emit:
            web_emit.emit({"type": "start", "conv_id": msg.conv_id})

        final_text = ""
        usage_total: dict[str, Any] = {}
        rounds = 0
        while True:
            calls: list[dict] = []
            content_parts: list[str] = []
            try:
                async for ev in client.chat_stream(
                    messages, model, tools=tools or None,
                    temperature=params["temperature"], max_tokens=params["max_tokens"],
                ):
                    if ev.type == "delta":
                        content_parts.append(ev.text)
                        if web_emit:
                            web_emit.stream_delta(msg.conv_id, ev.text)
                    elif ev.type == "tool_start":
                        if web_emit:
                            web_emit.stream_tool(msg.conv_id, ev.tool_name)
                    elif ev.type == "done":
                        calls = ev.message.get("tool_calls") or []
                        if ev.usage:
                            usage_total = ev.usage
                        content = str(ev.message.get("content") or "")
                        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
                        if calls:
                            assistant_msg["tool_calls"] = calls
                        messages.append(assistant_msg)
                        if content:
                            final_text = content
                    elif ev.type == "error":
                        await self._fail(msg, ev.error)
                        return
            except Exception as e:
                log.exception("生成出错")
                await self._fail(msg, f"生成出错：{e}")
                return

            if not calls or rounds >= max_rounds:
                if calls and rounds >= max_rounds:
                    log.warning("达到工具回合上限（%s），收尾", max_rounds)
                    final_text = final_text or "（我查了一阵，先停这儿——要接着查你再说一声）"
                break
            rounds += 1
            for call in calls:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("工具调用：%s(%s)", name, truncate(str(args), 160))
                result = await self.tools.execute(name, args)
                messages.append({
                    "role": "tool", "tool_call_id": call.get("id", ""), "name": name,
                    "content": json.dumps(result, ensure_ascii=False)[:16000],
                })

        if not final_text.strip():
            final_text = "（这轮什么也没说上来，再试一次吧）"

        await db.add_message(
            msg.conv_id, "assistant", final_text,
            source="assistant", model=f"{pid}/{model}",
            meta={"usage": usage_total} if usage_total else {},
        )
        if web_emit:
            web_emit.stream_end(msg.conv_id, {"role": "assistant", "content": final_text})
        else:
            await self.hub.deliver(msg.conv_id, final_text)

        # ---- 后处理（异步，不阻塞；异常都在内部接住） ----
        if selected_memories:
            self.memory.mark_consumed(selected_memories, final_text)   # 同步小操作，避免跨线程改库
        mode = "talk" if msg.source == "speak-up" else ("work" if msg.source == "bili-duty-scan" else "")
        self.mood.note_turn(mode, msg.text, final_text)   # 内部自行排队（串行化、不阻塞）
        if cfg.system.title_auto and conv and not str(conv.get("title") or "").strip():
            self._spawn(self._auto_title(msg.conv_id, msg.text, final_text))
        if cfg.tts.enabled:
            if msg.channel == "web" and cfg.tts.auto_web:
                self.tts.enqueue(final_text, conv_id=msg.conv_id, speak=True)
            elif msg.channel == "qq" and cfg.qq.reply_voice:
                self.tts.enqueue(final_text, conv_id=msg.conv_id)

    def _spawn(self, coro) -> None:
        if coro is None:
            return
        task = asyncio.ensure_future(coro)
        task.add_done_callback(_swallow)

    async def _auto_title(self, conv_id: str, user_text: str, reply: str) -> None:
        try:
            text = await self.router.quick(
                "title",
                "给这段对话起一个不超过 12 字的标题（中文、不要引号、不要句号），只输出标题本身。",
                f"TA：{truncate(user_text, 120)}\n我：{truncate(reply, 120)}",
                max_tokens=32, temperature=0.3,
            )
            title = truncate(text.strip().strip("「」\"'。"), 24, "")
            if title:
                await self.db.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
                web = self.hub.get("web")
                if isinstance(web, WebChannel):
                    web.emit({"type": "title", "conv_id": conv_id, "title": title})
        except Exception as e:
            log.debug("自动起标题失败：%s", e)

    async def _fail(self, msg: InboundMessage, text: str) -> None:
        log.warning("会话 %s 出错：%s", msg.conv_id, text)
        web = self.hub.get("web")
        if isinstance(web, WebChannel):
            web.error(msg.conv_id, text)
        if msg.channel == "qq":
            # QQ 侧不把技术错误甩过去，只在试运行外给一句人话
            pass


def _swallow(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
        if exc is not None:
            log.warning("后处理任务出错（已忽略）：%s", exc)
    except asyncio.CancelledError:
        pass