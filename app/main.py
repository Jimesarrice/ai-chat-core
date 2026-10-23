"""应用装配：把配置、存储、模型路由、六大能力与两个通道接到一起。"""
from __future__ import annotations

import os

# ⚠️ 必须在任何 httpx 客户端创建之前设置：
# Windows 系统代理（注册表 ProxyServer）会被 httpx 采纳，但注册表里的
# ProxyOverride（<local>;127.*;192.168.*）它**不认**——本地 llama.cpp / OneBot / TTS
# 的 127.0.0.1 请求会被塞进代理，端口没人监听时表现为"HTTP 503 空响应"（踩过）。
# 这里显式写 NO_PROXY：本地与局域网直连，云端地址照旧走系统代理。
_LOCAL_BYPASS = "127.0.0.1,localhost,::1,192.168."


def _ensure_no_proxy() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(var, "")
        entries = [e.strip() for e in cur.split(",") if e.strip()]
        for e in _LOCAL_BYPASS.split(","):
            if e not in entries:
                entries.append(e)
        os.environ[var] = ",".join(entries)


_ensure_no_proxy()

from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import routes_admin_core, routes_chat, routes_memory
from app.channels.base import ChannelHub
from app.channels.onebot import OneBotChannel
from app.channels.web import WebChannel
from app.config.store import ConfigStore
from app.core.pipeline import ChatPipeline
from app.core.proc_manager import ManagedProc
from app.features.memory import MemoryAdmin, MemoryService
from app.features.mood import MoodService
from app.features.search.bilibili import BiliClient
from app.features.search.tools import ToolRegistry
from app.features.search.websearch import WebSearch
from app.features.speakup import SpeakUpService
from app.features.tts.service import TTSService
from app.llm.router import LLMRouter
from app.logbus import get_logger, set_log_level, setup_logging
from app.storage.db import db as db_singleton
from app.version import APP_DESC, APP_NAME, APP_VERSION


def create_app(base_dir: Path) -> FastAPI:
    base_dir = base_dir.resolve()
    data_dir = base_dir / "data"

    # ---- 配置与日志 ----
    store = ConfigStore(base_dir / "config.json")
    store.load()
    setup_logging(base_dir / "logs", store.get().system.log_level)
    log = get_logger("app.main")

    # ---- 存储 ----
    db_singleton.path = data_dir / "state.db"
    db_singleton.connect()

    # ---- 通道 ----
    hub = ChannelHub()
    web = WebChannel()
    hub.register(web)
    qq = OneBotChannel(store, hub, data_dir / "media")
    hub.register(qq)

    # ---- 模型与能力 ----
    llm_router = LLMRouter(store)
    llm_local = ManagedProc("llama-server", data_dir / "llm" / "local_server.log", base_dir)
    memory = MemoryService(store, llm_router, data_dir)
    memory_admin = MemoryAdmin(memory)
    mood = MoodService(store, db_singleton, llm_router)
    tts = TTSService(store, hub, data_dir)
    speakup = SpeakUpService(store, db_singleton)
    websearch = WebSearch(store)
    bili = BiliClient(store, llm_router, data_dir)
    tools = ToolRegistry(store, websearch, bili, memory)
    pipeline = ChatPipeline(store, db_singleton, hub, llm_router, memory, mood, tts, tools)

    hub.on_inbound = pipeline.handle_inbound
    speakup.send_fn = pipeline.push_proactive
    speakup.busy_fn = pipeline.is_busy

    async def _list_convs() -> list[str]:
        return [c["id"] for c in await db_singleton.list_conversations(limit=1000)]

    speakup.list_convs_fn = _list_convs

    store.on_change(lambda cfg: set_log_level(cfg.system.log_level))

    services = {
        "store": store, "db": db_singleton, "hub": hub, "router": llm_router,
        "memory": memory, "memory_admin": memory_admin, "mood": mood, "tts": tts,
        "speakup": speakup, "websearch": websearch, "bili": bili, "tools": tools,
        "pipeline": pipeline, "data_dir": data_dir, "base_dir": base_dir,
        "llm_local": llm_local,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await memory.start()
        await hub.start_all()
        await speakup.start()
        await tts.start()
        llm_cfg = store.get().llm
        if llm_cfg.local_auto_start and llm_cfg.local_command.strip():
            r = await llm_local.start(llm_cfg.local_command)
            log.info("本地 llama-server 自动拉起：%s", r.get("message"))
        s = store.get()
        log.info("=" * 60)
        log.info("%s v%s 已就绪：http://%s:%s（聊天页）/ admin.html（配置与日志）",
                 APP_NAME, APP_VERSION, s.system.host, s.system.port)
        log.info("模型：聊天→%s；记忆/心情→%s｜QQ %s｜TTS %s｜记忆 %s 条",
                 s.llm.tasks["chat"].provider, s.llm.tasks["memory_extract"].provider,
                 "开" if s.qq.enabled else "关", "开" if s.tts.enabled else "关",
                 memory.stats()["total"])
        yield
        await tts.stop()
        await speakup.stop()
        await llm_local.stop()
        await hub.stop_all()
        await memory.stop()
        db_singleton.close()
        log.info("已停下")

    app = FastAPI(title=f"{APP_NAME} · {APP_DESC}", version=APP_VERSION, lifespan=lifespan)
    app.state.services = services

    @app.middleware("http")
    async def _no_cache_frontend(request, call_next):
        """前端静态文件（html/js/css）每次重新校验：改完刷新就见效，不被浏览器缓存坑。"""
        resp = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app.include_router(routes_chat.router)
    app.include_router(routes_admin_core.router)
    app.include_router(routes_memory.router)

    # ---- 静态资源 ----
    web_dir = base_dir / "web"
    tts_dir = data_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media/tts", StaticFiles(directory=str(tts_dir)), name="tts")
    media_dir = data_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app