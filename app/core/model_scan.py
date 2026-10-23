"""扫描本地模型目录（默认项目内 Models/），按 GGUF 元数据分类，供界面下拉选择。

分类口径（读 gguf 头，不加载权重，毫秒级）：
- arch == qwen3-tts            → tts_talker（带 variant：custom_voice/base/voice_design、音色表、语言表）
- arch == qwen3-tts-tokenizer  → tts_tokenizer
- 名字含 mmproj / arch 为 clip → mmproj（视觉投影器）
- 其余（qwen3vl/llama/qwen2…） → llm（聊天候选；qwen3vl 类标 vision=true，
  同一目录有 mmproj 时自动配上）
"""
from __future__ import annotations

import time
from pathlib import Path

from app.logbus import get_logger

log = get_logger("app.models")

CACHE_TTL = 30.0
_cache: dict = {"at": 0.0, "root": "", "data": None}


def scan_models(root: Path, *, refresh: bool = False) -> dict:
    now = time.time()
    if (not refresh and _cache["data"] is not None
            and _cache["root"] == str(root) and now - _cache["at"] < CACHE_TTL):
        return _cache["data"]

    data: dict = {
        "root": str(root),
        "exists": root.exists(),
        "llm": [], "tts_talkers": [], "tts_tokenizers": [], "mmproj": [], "unknown": [],
        # 引擎是否内置（给"生成启动命令"用）
        "engines": {
            "llama_server": str(_engine_path(root, "llama.cpp", "llama-server.exe")),
            "llama_server_ok": _engine_path(root, "llama.cpp", "llama-server.exe").exists(),
        },
    }
    if root.exists():
        for f in sorted(root.rglob("*.gguf")):
            item = _inspect(f)
            kind = item.pop("kind", "unknown")
            if kind == "tts_talker":
                data["tts_talkers"].append(item)
            elif kind == "tts_tokenizer":
                data["tts_tokenizers"].append(item)
            elif kind == "mmproj":
                data["mmproj"].append(item)
            elif kind == "llm":
                data["llm"].append(item)
            else:
                data["unknown"].append(item)

    # 配对：talker × tokenizer（按量化后缀匹配；只有一个 tokenizer 就直接配上）
    toks = data["tts_tokenizers"]
    for t in data["tts_talkers"]:
        t["tokenizer"] = _pair_tokenizer(t, toks)
    # 配对：llm 视觉模型的 mmproj
    for m in data["llm"]:
        m["mmproj"] = _pair_mmproj(m, data["mmproj"]) if m.get("vision") else None
    data["_cache_at"] = now

    _cache.update({"at": now, "root": str(root), "data": data})
    return data


def _engine_path(root: Path, *parts: str) -> Path:
    # root = <项目>/Models → 引擎在 <项目>/engines/...
    return root.parent.joinpath("engines", *parts)


def _pair_tokenizer(talker: dict, toks: list[dict]) -> dict | None:
    if not toks:
        return None
    quant = talker.get("quant", "")
    for t in toks:
        if quant and t.get("quant") == quant:
            return {"name": t["name"], "path": t["path"]}
    return {"name": toks[0]["name"], "path": toks[0]["path"]}


def _pair_mmproj(model: dict, projectors: list[dict]) -> dict | None:
    if not projectors:
        return None
    if len(projectors) == 1:
        return {"name": projectors[0]["name"], "path": projectors[0]["path"]}
    base = model["name"].split("-Q")[0].lower()
    for p in projectors:
        if base[:8] in p["name"].lower():
            return {"name": p["name"], "path": p["path"]}
    return None


def _inspect(path: Path) -> dict:
    item: dict = {
        "name": path.stem,
        "file": path.name,
        "path": str(path),
        "size_mb": round(path.stat().st_size / 1048576, 1),
        "kind": "unknown",
        "quant": _quant_of(path.stem),
    }
    try:
        from gguf import GGUFReader

        r = GGUFReader(str(path))

        def val(key: str):
            f = r.fields.get(key)
            if f is None:
                return None
            try:
                return f.contents()
            except Exception:
                return None

        arch = str(val("general.architecture") or "")
        title = str(val("general.name") or "")
        item["arch"] = arch
        item["title"] = title

        if arch == "qwen3-tts":
            item["kind"] = "tts_talker"
            low = (title or path.stem).lower()
            item["variant"] = ("custom_voice" if "custom" in low
                               else "base" if "base" in low
                               else "voice_design" if "design" in low else "")
            size = "1.7B" if "1.7b" in low else "0.6B" if "0.6b" in low else ""
            item["size"] = size
            speakers = val("qwen3-tts.codec.speaker_names")
            languages = val("qwen3-tts.codec.language_names")
            item["speakers"] = [str(s) for s in speakers] if isinstance(speakers, (list, tuple)) else []
            item["languages"] = [str(s) for s in languages] if isinstance(languages, (list, tuple)) else []
        elif arch == "qwen3-tts-tokenizer":
            item["kind"] = "tts_tokenizer"
        elif "mmproj" in path.stem.lower() or arch in ("clip", "projector"):
            item["kind"] = "mmproj"
        elif arch:
            item["kind"] = "llm"
            item["vision"] = ("vl" in arch.lower()) or ("vision" in arch.lower())
        else:
            item["kind"] = "unknown"
    except Exception as e:
        item["error"] = f"{type(e).__name__}: {e}"[:120]
    return item


def _quant_of(name: str) -> str:
    for q in ("Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q4_K_S", "Q3_K_M", "Q2_K", "F16", "BF16", "F32"):
        if q.lower() in name.lower():
            return q
    return ""


def build_llama_command(engine: Path, model_path: str, *, port: int = 8080,
                        ctx: int = 8192, mmproj: str | None = None, ngl: int = 99) -> str:
    """拼 llama-server 启动命令（给界面「自动生成」用）。"""
    parts = [
        f'"{engine}"',
        f'-m "{model_path}"',
    ]
    if mmproj:
        parts.append(f'--mmproj "{mmproj}"')
    parts += [f"--host 127.0.0.1 --port {port}", f"-ngl {ngl}", f"-c {ctx}"]
    return " ".join(parts)


def build_tts_command(engine: Path, talker_path: str, codec_path: str, *, port: int = 7865) -> str:
    return (f'"{engine}" --model "{talker_path}" --codec "{codec_path}" '
            f"--host 127.0.0.1 --port {port}")