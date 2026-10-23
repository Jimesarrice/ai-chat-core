"""Qwen3-TTS 后端适配：本地服务 / qwentts.cpp / DashScope 云 API / OpenAI 兼容 TTS。

所有后端的共同出口：一段音频写进 out_path，返回实际文件路径。
- dashscope：阿里云百炼（qwen3-tts-flash / qwen-tts），HTTP，返回音频 URL 再下载；
- qwentts：本地 qwentts.cpp 的 tts-server（GGUF + CPU/Vulkan），
  `POST /v1/audio/speech {"text","voice","language","response_format":"wav"}` → 二进制 WAV；
  音色/语言取自模型元数据：9 个预设音色、12 种语言（见配置页下拉）；
- local_http：其它本地服务（如 qwen3-tts 的 WebUI 端口），先试 POST /tts
  （兼容 raw wav / audio_base64 / url 三种回包），404 时再试 OpenAI 风格 /v1/audio/speech；
- openai_compat：直接走 /v1/audio/speech（返回音频字节流）。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

from app.config.store import ConfigStore
from app.logbus import get_logger

log = get_logger("app.tts")

_RAW_EXTS = {"wav": "wav", "mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
             "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/flac": "flac"}


class TTSError(Exception):
    pass


class Qwen3TTSEngine:
    """Qwen3-TTS 统一适配器。"""

    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    def _cfg(self):
        return self.store.get().tts

    async def synthesize(self, text: str, out_stem: Path) -> Path:
        cfg = self._cfg()
        if cfg.backend == "dashscope":
            return await self._dashscope(text, out_stem)
        if cfg.backend == "qwentts":
            return await self._qwentts(text, out_stem)
        if cfg.backend == "local_http":
            return await self._local_http(text, out_stem)
        return await self._openai_compat(text, out_stem, base_url=cfg.base_url or "", api_key=cfg.api_key)

    async def test(self) -> tuple[bool, str]:
        """配置页「试合成」：合成一句话并返回音频文件路径。"""
        cfg = self._cfg()
        if cfg.backend == "dashscope" and not self._dashscope_key():
            return False, "没配 API Key：填 tts.api_key 或设环境变量 DASHSCOPE_API_KEY"
        if cfg.backend in ("local_http", "openai_compat") and not cfg.base_url:
            return False, "本地/OpenAI 兼容后端需要填「后端地址」"
        out = Path("data/tts") / "试听.wav"
        try:
            path = await self.synthesize("你好呀，语音合成已经接通了。", out)
            return True, str(path)
        except TTSError as e:
            return False, str(e)
        except Exception as e:
            return False, f"合成失败：{e}"

    # ---------------------------------------------------------- dashscope
    def _dashscope_key(self) -> str:
        return (self._cfg().api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()

    async def _dashscope(self, text: str, out_stem: Path) -> Path:
        cfg = self._cfg()
        key = self._dashscope_key()
        if not key:
            raise TTSError("没配 API Key：填 tts.api_key 或设环境变量 DASHSCOPE_API_KEY")
        base = (cfg.base_url or "https://dashscope.aliyuncs.com").rstrip("/")
        url = f"{base}/api/v1/services/aigc/multimodal-generation/generation"
        body: dict = {
            "model": cfg.model or "qwen3-tts-flash",
            "input": {"text": text, "voice": cfg.voice or "Cherry"},
        }
        if cfg.speed and abs(cfg.speed - 1.0) > 1e-6:
            body["parameters"] = {"rate": cfg.speed}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as cx:
            r = await cx.post(url, json=body, headers=headers)
            if r.status_code == 400 and "parameters" in body:
                # 部分模型不认 rate 参数：去掉重试一次
                body.pop("parameters", None)
                r = await cx.post(url, json=body, headers=headers)
            if r.status_code != 200:
                raise TTSError(f"DashScope HTTP {r.status_code}：{r.text[:300]}")
            j = r.json()
            audio_url = (((j.get("output") or {}).get("audio") or {}).get("url")) or ""
            if not audio_url:
                raise TTSError(f"DashScope 未返回音频地址：{str(j)[:300]}")
            audio = await cx.get(audio_url)
            if audio.status_code != 200:
                raise TTSError(f"下载合成音频失败：HTTP {audio.status_code}")
            return _write(out_stem, audio.content, "wav")

    # ---------------------------------------------------------- qwentts.cpp（本地 GGUF）

    async def _qwentts(self, text: str, out_stem: Path) -> Path:
        """本地 qwentts.cpp 的 tts-server：POST /v1/audio/speech → 二进制 WAV。"""
        cfg = self._cfg()
        base = (cfg.base_url or "").strip().rstrip("/")
        if not base:
            raise TTSError("qwentts 后端要填「后端地址」，如 http://127.0.0.1:7865（服务器里的 --port）")
        voice = (cfg.voice or "").strip().lower()
        body: dict = {
            "input": text,          # 注意：qwentts 服务端要的是 input（OpenAI 风格），不是 text
            "voice": voice,
            "language": (cfg.local_language or "chinese").strip().lower(),
            "response_format": "wav",
        }
        body.update(cfg.local_payload or {})
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as cx:
            r = await cx.post(f"{base}/v1/audio/speech", json=body)
            if r.status_code != 200:
                raise TTSError(f"qwentts 服务 HTTP {r.status_code}：{r.text[:300]}")
            data = r.content
            if not data[:4] == b"RIFF":
                raise TTSError("qwentts 服务没回 WAV（检查 response_format / 服务日志）")
            return _write(out_stem, data, "wav")

    # ---------------------------------------------------------- local_http
    async def _local_http(self, text: str, out_stem: Path) -> Path:
        cfg = self._cfg()
        base = cfg.base_url.rstrip("/")
        # 兼容常见本地服务的两套命名（voice/speaker、language/lang）；多发的字段一般会被忽略，
        # 遇到严格服务可在「本地请求体附加」里覆盖/补充。
        body: dict = {"text": text, "speed": cfg.speed or 1.0, "language": "zh", "lang": "zh"}
        if cfg.model:
            body["model"] = cfg.model
        if cfg.voice:
            body["voice"] = cfg.voice
            body["speaker"] = cfg.voice
        body.update(cfg.local_payload or {})
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as cx:
            r = await cx.post(f"{base}/tts", json=body)
            if r.status_code == 404:
                return await self._openai_compat(text, out_stem, base_url=base, api_key=cfg.api_key)
            if r.status_code != 200:
                raise TTSError(f"本地 TTS HTTP {r.status_code}：{r.text[:300]}")
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
            if ctype.startswith("audio/") or r.content[:4] == b"RIFF":
                ext = _RAW_EXTS.get(ctype, "wav")
                return _write(out_stem, r.content, ext)
            # JSON 回包：audio_base64 / base64 / url / data
            try:
                j = r.json()
            except Exception:
                raise TTSError("本地 TTS 返回了无法识别的内容（既不是音频也不是 JSON）")
            b64 = j.get("audio_base64") or j.get("base64") or ((j.get("data") or {}).get("audio_base64") if isinstance(j.get("data"), dict) else None)
            if b64:
                return _write(out_stem, base64.b64decode(b64), j.get("format", "wav"))
            aurl = j.get("url") or ((j.get("data") or {}).get("url") if isinstance(j.get("data"), dict) else None)
            if aurl:
                audio = await cx.get(aurl)
                if audio.status_code == 200:
                    return _write(out_stem, audio.content, "wav")
                raise TTSError(f"下载本地合成音频失败：HTTP {audio.status_code}")
            raise TTSError(f"本地 TTS 回包里没有音频：{str(j)[:200]}")

    # ---------------------------------------------------------- openai 兼容
    async def _openai_compat(self, text: str, out_stem: Path, *, base_url: str, api_key: str) -> Path:
        cfg = self._cfg()
        if not base_url:
            raise TTSError("OpenAI 兼容 TTS 需要填「后端地址」")
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        body = {
            "model": cfg.model or "tts-1",
            "input": text,
            "voice": cfg.voice or "alloy",
            "response_format": "wav",
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as cx:
            r = await cx.post(f"{base}/v1/audio/speech", json=body, headers=headers)
            if r.status_code != 200:
                raise TTSError(f"TTS HTTP {r.status_code}：{r.text[:300]}")
            return _write(out_stem, r.content, "wav")


def _write(stem: Path, data: bytes, ext: str) -> Path:
    stem.parent.mkdir(parents=True, exist_ok=True)
    path = stem.with_suffix(f".{ext}")
    path.write_bytes(data)
    return path