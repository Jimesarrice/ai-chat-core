"""管理端：记忆 / 批量整理 / 联网搜集（B站登录与自检）。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


def _svc(request: Request) -> dict[str, Any]:
    return request.app.state.services


# ================================================================ 记忆浏览与管理

@router.get("/api/memory/list")
async def memory_list(request: Request, include_superseded: bool = True, include_deleted: bool = False):
    svc = _svc(request)
    mems = svc["memory"].list_memories(include_superseded=include_superseded, include_deleted=include_deleted)
    return {"ok": True, "memories": mems, "stats": svc["memory"].stats()}


@router.get("/api/memory/traces")
async def memory_traces(request: Request, limit: int = 10):
    return {"ok": True, "traces": _svc(request)["memory"].traces(limit)}


@router.post("/api/memory/add")
async def memory_add(request: Request, body: dict):
    svc = _svc(request)
    r = await svc["memory"].add_manual(
        str(body.get("text", "")),
        importance=float(body.get("importance", 0.5) or 0.5),
        tags=[str(t) for t in (body.get("tags") or [])],
        at=(str(body.get("at")) if body.get("at") else None),
        fact_key=str(body.get("fact_key", "") or ""),
    )
    return r


@router.post("/api/memory/edit")
async def memory_edit(request: Request, body: dict):
    return await _svc(request)["memory"].edit(
        str(body.get("id", "")),
        text=body.get("text"),
        importance=(float(body["importance"]) if "importance" in body and body["importance"] is not None else None),
        at=body.get("at"),
        fact_key=body.get("fact_key"),
    )


@router.post("/api/memory/delete")
async def memory_delete(request: Request, body: dict):
    return await _svc(request)["memory"].delete(str(body.get("id", "")))


@router.post("/api/memory/restore")
async def memory_restore(request: Request, body: dict):
    return await _svc(request)["memory"].restore(str(body.get("id", "")))


@router.post("/api/memory/unsupersede")
async def memory_unsupersede(request: Request, body: dict):
    return await _svc(request)["memory"].unsupersede(str(body.get("id", "")))


@router.post("/api/memory/change-person")
async def memory_change_person(request: Request, body: dict):
    svc = _svc(request)
    new_py = str(body.get("person", "ta"))
    apply_existing = bool(body.get("apply", False))
    if not apply_existing:
        r = svc["memory"].change_person(new_py)
        return {"ok": True, "message": f"已有 {r['changed']} 条旧记忆里还写着旧的字", **r}
    # 两段式：先只报数（默认），再一起改；这里 apply=True 时先改配置再改库
    svc["store"].save_patch({"memory": {"person": new_py}})
    r = svc["memory"].change_person(new_py, apply_existing=True)
    return {"ok": True, "message": f"改了 {r['changed']} 条旧记忆", **r}


# ================================================================ 批量整理（一批一批调）

@router.post("/api/memory/refine")
async def memory_refine(request: Request, body: dict | None = None):
    limit = int((body or {}).get("limit", 6) or 6)
    return await _svc(request)["memory_admin"].refine_batch(limit)


@router.post("/api/memory/refine/undo")
async def memory_refine_undo(request: Request, body: dict | None = None):
    ids = (body or {}).get("ids")
    return await _svc(request)["memory_admin"].refine_undo(ids)


@router.post("/api/memory/refine/retry")
async def memory_refine_retry(request: Request):
    return await _svc(request)["memory_admin"].refine_retry()


@router.post("/api/memory/expand")
async def memory_expand(request: Request, body: dict | None = None):
    limit = int((body or {}).get("limit", 8) or 8)
    return await _svc(request)["memory"].expand_batch(limit=limit)


@router.post("/api/memory/wash-keys")
async def memory_wash_keys(request: Request, body: dict | None = None):
    b = body or {}
    return await _svc(request)["memory_admin"].wash_keys(
        limit=int(b.get("limit", 8) or 8), dry_run=bool(b.get("dry", False)))


@router.post("/api/memory/wash-keys/undo")
async def memory_wash_keys_undo(request: Request):
    return await _svc(request)["memory_admin"].wash_keys_undo()


@router.post("/api/memory/backfill-stable")
async def memory_backfill_stable(request: Request, body: dict | None = None):
    b = body or {}
    return await _svc(request)["memory_admin"].backfill_stable(
        limit=int(b.get("limit", 20) or 20), redo=bool(b.get("redo", False)))


@router.post("/api/memory/backfill-stable/undo")
async def memory_backfill_stable_undo(request: Request):
    return await _svc(request)["memory_admin"].backfill_stable_undo()


@router.get("/api/memory/duplicates")
async def memory_duplicates(request: Request):
    return _svc(request)["memory_admin"].duplicates()


@router.post("/api/memory/merge")
async def memory_merge(request: Request, body: dict):
    return await _svc(request)["memory_admin"].merge(
        str(body.get("keep", "")), str(body.get("drop", "")))


# ================================================================ 旧档案导入

@router.post("/api/memory/import/scan")
async def memory_import_scan(request: Request, body: dict):
    path = str(body.get("path", "") or "").strip()
    if not path:
        return {"ok": False, "message": "先填档案文件夹路径"}
    from app.features.memory.admin import scan_archive
    return await asyncio.to_thread(scan_archive, Path(path))


@router.post("/api/memory/import/apply")
async def memory_import_apply(request: Request, body: dict):
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        return {"ok": False, "message": "没有勾选条目"}
    return await _svc(request)["memory_admin"].import_apply(items)


@router.post("/api/memory/import/undo")
async def memory_import_undo(request: Request, body: dict):
    batch = str(body.get("batch", "") or "")
    if not batch:
        return {"ok": False, "message": "缺少 batch"}
    return await _svc(request)["memory_admin"].import_undo(batch)


# ================================================================ 联网搜集（B站）

@router.get("/api/bili/status")
async def bili_status(request: Request):
    svc = _svc(request)
    bili = svc["bili"]
    from app.features.search.bilibili import _need_lib
    miss = _need_lib()
    if miss:
        return {"ok": True, "lib": False, "logged_in": False, "message": miss["message"]}
    cred = await bili.check_credential()
    n = 0
    try:
        n = len(list(bili.out_dir.glob("*.md")))
    except OSError:
        pass
    return {"ok": True, "lib": True, **cred, "reports": n}


@router.post("/api/bili/login/start")
async def bili_login_start(request: Request):
    return await _svc(request)["bili"].login_start()


@router.post("/api/bili/login/poll")
async def bili_login_poll(request: Request, body: dict):
    return await _svc(request)["bili"].login_poll(str(body.get("key", "")))


@router.post("/api/search/test")
async def search_test(request: Request, body: dict):
    q = str(body.get("query", "") or "").strip()
    if not q:
        return {"ok": False, "message": "先填关键词"}
    return await _svc(request)["websearch"].search(q)