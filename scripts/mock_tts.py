"""开发自测用的 mock TTS 服务（零依赖，标准库正弦波）。

python scripts/mock_tts.py [--port 7865]
- GET  /health → {"ok": true}
- POST /tts    → 默认回 raw WAV（24kHz 单声道），响应头 audio/wav；
                 请求里带 "format":"json" 时回 {"audio_base64": ...}（两条解析分支都能自测）。
- 服务名/模型/音色只打印不影响输出，用来核对链路里带没带上。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import struct
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SR = 24000


def beep(duration: float, freq: float = 523.25) -> bytes:
    n = max(1, int(SR * duration))
    fade = max(1, int(0.04 * SR))
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / fade, (n - i) / fade)
        v = 0.32 * env * math.sin(2 * math.pi * freq * i / SR) \
            + 0.12 * env * math.sin(2 * math.pi * freq * 1.5 * i / SR)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-tts"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json(200, {"ok": True, "service": "mock-tts", "model": "mock"})
        else:
            self._json(404, {"ok": False})

    def do_POST(self):
        if not self.path.startswith("/tts"):
            self._json(404, {"ok": False, "error": "only /tts"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            req = {}
        text = str(req.get("text") or "")
        wav = beep(min(2.5, max(0.4, len(text) * 0.08)))
        print(f"[mock-tts] text={text[:30]!r} model={req.get('model')!r} voice={req.get('voice')!r} "
              f"speed={req.get('speed')!r} → {len(wav)}B wav", flush=True)
        if str(req.get("format", "")).lower() == "json":
            self._json(200, {"audio_base64": base64.b64encode(wav).decode(), "format": "wav"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7865)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock-tts 在 http://127.0.0.1:{args.port}（Ctrl+C 退出）")
    srv.serve_forever()


if __name__ == "__main__":
    main()