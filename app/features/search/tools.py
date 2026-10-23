"""工具注册表：把「联网搜集 / B站采集 / 记忆」能力挂给聊天模型。

- 是否提供某个工具由配置的「工具开关」决定，模型只看到开着的；
- bili_reply / bili_send_message 属于"对外说话"的工具，默认关（要发时用户在配置页显式开）。
"""
from __future__ import annotations

from typing import Any

from app.config.store import ConfigStore
from app.features.memory.core import MemoryService
from app.features.search.bilibili import BiliClient
from app.features.search.websearch import WebSearch
from app.logbus import get_logger

log = get_logger("app.tools")


def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


class ToolRegistry:
    def __init__(self, store: ConfigStore, web: WebSearch, bili: BiliClient,
                 memory: MemoryService | None = None) -> None:
        self.store = store
        self.web = web
        self.bili = bili
        self.memory = memory

    def schemas(self) -> list[dict]:
        cfg = self.store.get().search
        out: list[dict] = []
        if cfg.enabled:
            toggles = dict(cfg.tools or {})
            if toggles.get("web_search"):
                out.append(_tool(
                    "web_search", "联网搜索（返回标题/链接/摘要；要读正文再配合 web_fetch）",
                    {"query": {"type": "string", "description": "搜索关键词"}}, ["query"]))
            if toggles.get("web_fetch"):
                out.append(_tool(
                    "web_fetch", "打开一个网页读正文（粗暴提取主文本，适合文章/文档页）",
                    {"url": {"type": "string"}}, ["url"]))
            if cfg.bili_enabled:
                if toggles.get("bili_scan"):
                    out.append(_tool(
                        "bili_scan",
                        "去 B 站刷一圈：有一个内建的子代理会自己搜视频、读字幕、翻评论，"
                        "最后交回一份内部报告（每条含标题、BV号、讲了什么、是否值得）。想看 B 站内容优先用这个。",
                        {"direction": {"type": "string", "description": "想看的方向/话题"},
                         "n": {"type": "integer", "description": "最多挑几条（默认 3）"}},
                        ["direction"]))
                if toggles.get("bili_search"):
                    out.append(_tool(
                        "bili_search", "搜 B 站视频（返回 bvid 与播放量等；要了解内容用 bili_subtitle）",
                        {"keyword": {"type": "string"}, "num": {"type": "integer"},
                         "order": {"type": "string", "enum": ["totalrank", "click", "pubdate", "dm"]}},
                        ["keyword"]))
                if toggles.get("bili_subtitle"):
                    out.append(_tool(
                        "bili_subtitle", "取 B 站视频的 AI 字幕全文（判断内容靠它；没有字幕是正常结果）",
                        {"bvid": {"type": "string"}}, ["bvid"]))
                if toggles.get("bili_comments"):
                    out.append(_tool(
                        "bili_comments", "取 B 站视频的热门评论（按点赞）",
                        {"bvid": {"type": "string"}, "num": {"type": "integer"}}, ["bvid"]))
                if toggles.get("bili_video_info"):
                    out.append(_tool(
                        "bili_video_info", "看 B 站视频详情（简介/标签/播放数据）",
                        {"bvid": {"type": "string"}}, ["bvid"]))
                if toggles.get("bili_reply"):
                    out.append(_tool(
                        "bili_reply",
                        "给 B 站视频发评论 / 回复评论（**对外公开**：内容要极短、只说一个点、不写私密的话。"
                        "发新评论时 @ 必须写进正文；回复楼中楼不用手打 @）",
                        {"bvid": {"type": "string"}, "text": {"type": "string"},
                         "rpid": {"type": "integer", "description": ">0 = 回复这条评论"}},
                        ["bvid", "text"]))
                if toggles.get("bili_send_message"):
                    out.append(_tool(
                        "bili_send_message",
                        "给 B 站用户发私信（只用 {owner} 本人的 UID；内容是给人看的人话，≤500 字，一轮只发一条）",
                        {"uid": {"type": "integer"}, "text": {"type": "string"}}, ["uid", "text"]))
        mem_cfg = self.store.get().memory
        if self.memory is not None and mem_cfg.enabled and mem_cfg.auto_extract:
            out.append(self.memory.remember_tool_spec())
        return out

    async def execute(self, name: str, args: dict[str, Any]) -> dict:
        try:
            if name == "web_search":
                return await self.web.search(str(args.get("query", "")))
            if name == "web_fetch":
                return await self.web.fetch(str(args.get("url", "")))
            if name == "bili_scan":
                return await self.bili.scan(str(args.get("direction", "")), int(args.get("n", 3) or 3))
            if name == "bili_search":
                return await self.bili.search(str(args.get("keyword", "")),
                                              int(args.get("num", 10) or 10),
                                              str(args.get("order", "totalrank")))
            if name == "bili_subtitle":
                return await self.bili.subtitle(str(args.get("bvid", "")))
            if name == "bili_comments":
                return await self.bili.comments(str(args.get("bvid", "")), int(args.get("num", 30) or 30))
            if name == "bili_video_info":
                return await self.bili.video_info(str(args.get("bvid", "")))
            if name == "bili_reply":
                return await self.bili.reply(str(args.get("bvid", "")), str(args.get("text", "")),
                                             int(args.get("rpid", 0) or 0))
            if name == "bili_send_message":
                return await self.bili.send_message(int(args.get("uid", 0) or 0), str(args.get("text", "")))
            if name == "remember" and self.memory is not None:
                return await self.memory.remember(args)
            return {"ok": False, "message": f"没有这个工具：{name}"}
        except PermissionError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            log.exception("工具 %s 执行出错", name)
            return {"ok": False, "message": f"工具出错：{e}"}