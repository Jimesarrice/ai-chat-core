"""任务路由：把「任务」映射到「接入点」，统一处理思考模式参数。

两条路径（本地 llama.cpp / 云端 API）由用户在配置页里按任务分工：
chat / memory_extract / mood_judge / title / search_summary … 各选各的。
"""
from __future__ import annotations

from typing import Any

from app.config.store import ConfigStore
from app.llm.client import LLMError, OpenAICompatClient, message_text
from app.logbus import get_logger

log = get_logger("app.llm.router")


class LLMRouter:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    # ---- 解析 ----
    def resolve(self, task: str) -> tuple[OpenAICompatClient, str, dict[str, Any], str]:
        """返回 (客户端, 模型名, 采样参数, 接入点id)。找不到配置时抛 LLMError。"""
        cfg = self.store.get().llm
        route = cfg.tasks.get(task)
        if route is None:
            raise LLMError(f"任务「{task}」没有分工配置（去配置页・模型接入里加上）")
        if not route.enabled:
            raise LLMError(f"任务「{task}」已被禁用（配置页里可以重新打开）")
        pid = route.provider or (next(iter(cfg.providers), ""))
        prov = cfg.providers.get(pid)
        if prov is None:
            raise LLMError(f"任务「{task}」指向的接入点「{pid}」不存在（检查配置页）")
        model = route.model or prov.model
        if not model:
            raise LLMError(f"接入点「{pid}」还没填默认模型（配置页・模型接入）")
        extra = self._thinking_params(prov.model_dump(), pid)
        client = OpenAICompatClient(
            prov.base_url,
            prov.api_key,
            timeout_read=float(prov.timeout_read or 600),
            extra_body=extra,
            name=pid,
        )
        params = {
            "temperature": route.temperature,
            "max_tokens": route.max_tokens or None,
        }
        return client, model, params, pid

    # ---- 思考模式 ----
    @staticmethod
    def _thinking_params(prov: dict, pid: str) -> dict[str, Any]:
        mode = prov.get("thinking", "low")
        preset = prov.get("thinking_preset", "auto")
        user_extra = dict(prov.get("extra_body") or {})
        if mode == "default" or preset == "none":
            generated: dict[str, Any] = {}
        else:
            if preset == "auto":
                base = str(prov.get("base_url", ""))
                if prov.get("kind") == "local" or ":8080" in base or "llama" in base:
                    preset = "llama_cpp"
                elif "dashscope" in base or "aliyuncs" in base:
                    preset = "dashscope"
                else:
                    preset = "openai"
            if preset == "llama_cpp":
                generated = {"chat_template_kwargs": {"enable_thinking": mode == "high"}}
            elif preset == "dashscope":
                generated = {"enable_thinking": mode == "high"}
            else:  # openai 风格
                effort = {"off": "minimal", "low": "low", "high": "high"}[mode]
                generated = {"reasoning_effort": effort}
        # 用户手填的 extra_body 优先级最高
        merged = {**generated, **user_extra}
        if merged and mode != "default":
            log.debug("[%s] 思考模式 %s → 附带参数 %s", pid, mode, merged)
        return merged

    # ---- 工具类任务便捷入口 ----
    async def quick(
        self,
        task: str,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """一次性短任务（记忆提炼/判断/起名类）：返回纯文本（通常是一段 JSON）。"""
        client, model, params, pid = self.resolve(task)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        msg = await client.chat_once(
            messages,
            model,
            temperature=params["temperature"] if temperature is None else temperature,
            max_tokens=max_tokens or params["max_tokens"],
        )
        return message_text(msg)