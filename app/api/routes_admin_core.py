"""管理端核心接口：配置 / 日志(SSE) / 模型自检 / 心情 / 主动搭话 / QQ / TTS / 系统。"""
from __future__ import annotations

import asyncio
import json as _json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.logbus import log_bus
from app.version import APP_DESC, APP_NAME, APP_VERSION

router = APIRouter()
STARTED_AT = time.time()


def _svc(request: Request) -> dict[str, Any]:
    return request.app.state.services


# ================================================================ 配置

@router.get("/api/config")
async def get_config(request: Request):
    store = _svc(request)["store"]
    return {"ok": True, **store.ui_schema()}


@router.post("/api/config")
async def save_config(request: Request, body: dict):
    store = _svc(request)["store"]
    patch = body.get("patch") or body
    try:
        store.save_patch(patch)
    except ValidationError as e:
        raise HTTPException(400, {"message": "配置不合法", "errors": e.errors()[:6]})
    return {"ok": True, "message": "存好了（改过的部分立刻生效）"}


@router.post("/api/config/replace")
async def replace_config(request: Request, body: dict):
    store = _svc(request)["store"]
    try:
        store.replace_all(body.get("config") or body)
    except ValidationError as e:
        raise HTTPException(400, {"message": "配置不合法", "errors": e.errors()[:6]})
    return {"ok": True, "message": "整体替换完成"}


# ================================================================ 日志

@router.get("/api/logs/recent")
async def logs_recent(limit: int = Query(400, ge=1, le=2000), after_id: int = 0, level: str | None = None):
    return {"ok": True, "logs": log_bus.snapshot(after_id=after_id, level=level, limit=limit)}


@router.get("/api/logs/stream")
async def logs_stream(level: str | None = None):
    async def gen():
        # 先补 100 条历史，再流式
        for item in log_bus.snapshot(limit=100, level=level):
            yield "data: " + _json.dumps(item, ensure_ascii=False) + "\n\n"
        async for item in log_bus.subscribe():
            lv = item["level"]
            if level and _level_rank(lv) < _level_rank(level):
                continue
            yield "data: " + _json.dumps(item, ensure_ascii=False) + "\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _level_rank(name: str) -> int:
    return {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}.get(str(name).upper(), 0)


# ================================================================ 模型接入

@router.get("/api/llm/status")
async def llm_status(request: Request):
    """逐接入点自检（连通性 + 模型清单），并回任务分工。"""
    from app.llm.client import OpenAICompatClient

    store = _svc(request)["store"]
    cfg = store.get().llm

    async def probe(pid: str, prov) -> dict:
        client = OpenAICompatClient(prov.base_url, prov.api_key, name=pid)
        ok, detail = await client.health()
        models = await client.list_models() if ok else []
        return {"id": pid, "label": prov.label, "kind": prov.kind, "base_url": prov.base_url,
                "model": prov.model, "ok": ok, "detail": detail, "models": models[:50]}

    out = await asyncio.gather(*[probe(pid, p) for pid, p in cfg.providers.items()])
    return {"ok": True, "providers": list(out),
            "routes": {k: {"provider": v.provider, "model": v.model, "enabled": v.enabled}
                       for k, v in cfg.tasks.items()}}


@router.get("/api/llm/models")
async def llm_models(request: Request, provider_id: str):
    store = _svc(request)["store"]
    prov = store.get().llm.providers.get(provider_id)
    if prov is None:
        raise HTTPException(404, f"没有接入点 {provider_id}")
    from app.llm.client import OpenAICompatClient
    client = OpenAICompatClient(prov.base_url, prov.api_key, name=provider_id)
    models = await client.list_models()
    return {"ok": True, "models": models}


# ================================================================ 本地模型目录 + llama-server 托管

@router.get("/api/models/local")
async def models_local(request: Request, refresh: bool = False):
    """扫描项目 Models/ 目录：llm / tts_talkers / tts_tokenizers / mmproj（按 GGUF 元数据分类）。"""
    svc = _svc(request)
    from app.core.model_scan import scan_models
    root = svc["base_dir"] / "Models"
    return {"ok": True, **scan_models(root, refresh=refresh)}


@router.get("/api/llm/local/status")
async def llm_local_status(request: Request):
    svc = _svc(request)
    proc = svc["llm_local"]
    cfg = svc["store"].get().llm
    prov = cfg.providers.get("local")
    return {
        "ok": True,
        "running": proc.running,
        "pid": proc.pid,
        "command": cfg.local_command or "",
        "auto_start": bool(cfg.local_auto_start),
        "base_url": prov.base_url if prov else "",
        "model": prov.model if prov else "",
        "log_file": str(proc.log_path),
        "log_tail": proc.log_tail(1500),
    }


@router.post("/api/llm/local/start")
async def llm_local_start(request: Request, body: dict | None = None):
    svc = _svc(request)
    cmd = str((body or {}).get("command", "") or "") or svc["store"].get().llm.local_command
    return await svc["llm_local"].start(cmd)


@router.post("/api/llm/local/stop")
async def llm_local_stop(request: Request):
    return await _svc(request)["llm_local"].stop()


@router.post("/api/llm/local/probe")
async def llm_local_probe(request: Request):
    svc = _svc(request)
    prov = svc["store"].get().llm.providers.get("local")
    from app.core.proc_manager import probe_service
    return await probe_service(prov.base_url if prov else "")


# ================================================================ 心情

@router.get("/api/mood")
async def mood_panel(request: Request):
    mood = _svc(request)["mood"]
    return {"ok": True, **(await mood.panel())}


@router.post("/api/mood/reset")
async def mood_reset(request: Request):
    mood = _svc(request)["mood"]
    await mood.reset()
    return {"ok": True, "message": "心情已回到基线"}


# ================================================================ 主动搭话

@router.get("/api/speakup/logs")
async def speakup_logs(request: Request, limit: int = 60):
    speakup = _svc(request)["speakup"]
    return {"ok": True, "logs": await speakup.recent_logs(limit)}


@router.post("/api/speakup/roll")
async def speakup_roll(request: Request, body: dict | None = None):
    """演练（默认，绝不真发）/ 真发一次。"""
    speakup = _svc(request)["speakup"]
    mode = str((body or {}).get("mode", "drill"))
    if mode not in ("drill", "send"):
        raise HTTPException(400, "mode 只能是 drill / send")
    rec = await speakup.roll(source="手动", mode=mode)
    return {"ok": True, "record": rec}


# ================================================================ QQ

@router.get("/api/qq/status")
async def qq_status(request: Request):
    ch = _svc(request)["hub"].get("qq")
    if ch is None:
        return {"ok": False, "message": "QQ 通道没起来"}
    return {"ok": True, **ch.status()}


@router.post("/api/qq/test-send")
async def qq_test_send(request: Request, body: dict | None = None):
    """真发一条测试句（绕过试运行；手动才发）。"""
    ch = _svc(request)["hub"].get("qq")
    if ch is None:
        raise HTTPException(400, "QQ 通道没起来")
    text = str((body or {}).get("text") or "（这是一条测试消息：通了）")
    target = str((body or {}).get("target") or "")
    r = await ch.test_send(text, target)
    return r


# ================================================================ TTS

@router.get("/api/tts/status")
async def tts_status(request: Request):
    tts = _svc(request)["tts"]
    return {"ok": True, **tts.status()}


@router.post("/api/tts/test")
async def tts_test(request: Request):
    tts = _svc(request)["tts"]
    r = await tts.test()
    if r.get("ok"):
        from pathlib import Path
        name = Path(str(r["message"])).name
        r["url"] = f"/media/tts/{name}"
    return r


@router.post("/api/tts/speak")
async def tts_speak(request: Request, body: dict):
    tts = _svc(request)["tts"]
    text = str(body.get("text", "") or "")
    if not text.strip():
        raise HTTPException(400, "空文本")
    return await tts.speak_now(text, conv_id=str(body.get("conv_id", "")))


@router.post("/api/tts/stop")
async def tts_stop(request: Request):
    tts = _svc(request)["tts"]
    n = tts.clear()
    return {"ok": True, "cleared": n}


# ---- 本地部署（托管本地 TTS 服务） ----

@router.get("/api/tts/local/status")
async def tts_local_status(request: Request):
    return {"ok": True, **_svc(request)["tts"].local_status()}


@router.post("/api/tts/local/start")
async def tts_local_start(request: Request, body: dict | None = None):
    return await _svc(request)["tts"].local_start(str((body or {}).get("command", "") or ""))


@router.post("/api/tts/local/stop")
async def tts_local_stop(request: Request):
    return await _svc(request)["tts"].local_stop()


@router.post("/api/tts/local/probe")
async def tts_local_probe(request: Request):
    return await _svc(request)["tts"].local_probe()


@router.get("/api/tts/favorites")
async def tts_favorites(request: Request):
    db = _svc(request)["db"]
    cats = await db.tts_categories()
    items = await db.list_tts_favorites()
    return {"ok": True, "categories": cats, "items": items}


@router.post("/api/tts/favorites")
async def tts_favorite_add(request: Request, body: dict):
    db = _svc(request)["db"]
    text = str(body.get("text", "") or "").strip()
    if not text:
        raise HTTPException(400, "空文本")
    fid = await db.add_tts_favorite(text, str(body.get("category", "默认") or "默认"))
    return {"ok": True, "id": fid}


@router.delete("/api/tts/favorites/{fid}")
async def tts_favorite_delete(request: Request, fid: int):
    await _svc(request)["db"].delete_tts_favorite(fid)
    return {"ok": True}


@router.post("/api/tts/favorites/move")
async def tts_favorite_move(request: Request, body: dict):
    await _svc(request)["db"].move_tts_favorite(int(body.get("id", 0)), str(body.get("category", "默认")))
    return {"ok": True}


@router.get("/api/tts/favorites/export")
async def tts_favorites_export(request: Request):
    db = _svc(request)["db"]
    items = await db.list_tts_favorites()
    lines = ["# 语音收藏", ""]
    last_cat = None
    for it in sorted(items, key=lambda x: (x["category"], -x["id"])):
        if it["category"] != last_cat:
            lines += [f"## {it['category']}", ""]
            last_cat = it["category"]
        text = " ".join(str(it["text"]).split("\n"))
        lines.append(f"- （{str(it['created_at'])[5:16]}）：{text}")
    body = "\n".join(lines)
    from fastapi.responses import Response
    return Response(content=body, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=voice-favorites.md"})


# ================================================================ 系统

@router.get("/api/system/info")
async def system_info(request: Request):
    svc = _svc(request)
    db = svc["db"]
    convs = await db.list_conversations(limit=1000)
    return {
        "ok": True,
        "name": APP_NAME, "version": APP_VERSION, "desc": APP_DESC,
        "uptime": int(time.time() - STARTED_AT),
        "conversations": len(convs),
        "log_subscribers": log_bus.subscriber_count(),
        "web_clients": svc["hub"].get("web").client_count() if svc["hub"].get("web") else 0,
        "data_dir": str(svc["data_dir"]),
        "config_file": str(svc["store"].path),
    }