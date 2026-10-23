"""聊天页接口：会话列表 / 消息读写 / 发消息（走管道）/ 图片上传 / WebSocket。"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect

from app.channels.web import WebChannel
from app.core.types import InboundMessage

router = APIRouter()

ALLOWED_IMG = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _svc(request: Request) -> dict[str, Any]:
    return request.app.state.services


@router.get("/api/conversations")
async def list_conversations(request: Request, channel: str | None = None):
    db = _svc(request)["db"]
    rows = await db.list_conversations(channel=channel)
    out = []
    for r in rows:
        last = await db.query_one(
            "SELECT content, created_at FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT 1",
            (r["id"],),
        )
        out.append({
            "id": r["id"], "channel": r["channel"], "title": r["title"] or "",
            "updated_at": r["updated_at"],
            "last": (last or {}).get("content", "")[:60],
        })
    return {"ok": True, "conversations": out}


@router.post("/api/conversations")
async def create_conversation(request: Request):
    db = _svc(request)["db"]
    conv_id = f"web:{uuid.uuid4().hex[:12]}"
    await db.ensure_conversation(conv_id, "web", title="")
    return {"ok": True, "id": conv_id}


@router.delete("/api/conversations/{conv_id}")
async def delete_conversation(request: Request, conv_id: str):
    db = _svc(request)["db"]
    await db.delete_conversation(conv_id)
    return {"ok": True}


@router.get("/api/conversations/{conv_id}/messages")
async def list_messages(request: Request, conv_id: str,
                        limit: int = Query(200, ge=1, le=1000), after_id: int = 0):
    db = _svc(request)["db"]
    rows = await db.list_messages(conv_id, limit=limit, after_id=after_id)
    return {"ok": True, "messages": rows}


@router.post("/api/conversations/{conv_id}/messages")
async def send_message(request: Request, conv_id: str, body: dict):
    """发一条消息（立即返回；回复通过 WebSocket 推送）。"""
    svc = _svc(request)
    text = str(body.get("text", "") or "").strip()
    images = [str(p) for p in (body.get("images") or [])][:4]
    if not text and not images:
        raise HTTPException(400, "空消息")
    msg = InboundMessage(channel="web", conv_id=conv_id, text=text, images=images, source="user")
    task = asyncio.ensure_future(svc["pipeline"].handle_inbound(msg))
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return {"ok": True, "accepted": True}


@router.get("/api/media_url")
async def media_url(request: Request, path: str):
    """把本地媒体路径换成可访问的 /media URL（只放行 data 目录下的）。"""
    svc = _svc(request)
    data_dir: Path = svc["data_dir"]
    try:
        p = Path(path).resolve()
        media = (data_dir / "media").resolve()
        tts = (data_dir / "tts").resolve()
        if p.is_file() and p.is_relative_to(media):
            return {"ok": True, "url": "/media/" + p.relative_to(media).as_posix()}
        if p.is_file() and p.is_relative_to(tts):
            return {"ok": True, "url": "/media/tts/" + p.relative_to(tts).as_posix()}
    except (OSError, ValueError):
        pass
    return {"ok": False, "url": None}


@router.post("/api/upload")
async def upload(request: Request, file: UploadFile):
    svc = _svc(request)
    data_dir: Path = svc["data_dir"]
    ext = Path(file.filename or "img.png").suffix.lower()
    if ext not in ALLOWED_IMG:
        raise HTTPException(400, f"不认的图片类型：{ext}")
    dest = data_dir / "media" / "uploads"
    dest.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}{ext}"
    path = dest / name
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "单张图别超过 10MB")
    path.write_bytes(content)
    return {"ok": True, "path": str(path), "url": f"/media/uploads/{name}"}


@router.websocket("/api/ws")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    web: WebChannel = ws.app.state.services["hub"].get("web")
    q = web.subscribe()
    try:
        while True:
            payload = await q.get()
            await ws.send_text(payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        web.unsubscribe(q)


@router.websocket("/onebot/ws")
async def ws_onebot(ws: WebSocket, access_token: str = Query("")):
    """反向 WS：OneBot 后端连过来推事件。"""
    svc = ws.app.state.services
    cfg = svc["store"].get().qq
    if cfg.token and access_token != cfg.token:
        await ws.close(code=4003)
        return
    await ws.accept()
    ch = svc["hub"].get("qq")
    if ch is None:
        await ws.close(code=4004)
        return
    try:
        import json as _json
        while True:
            raw = await ws.receive_text()
            try:
                event = _json.loads(raw)
            except _json.JSONDecodeError:
                continue
            await ch.feed_event(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass