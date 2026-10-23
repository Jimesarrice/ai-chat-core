"""启动入口：python run.py [--host 0.0.0.0] [--port 8760]

不带参数时用配置页里填的 host/port（默认 127.0.0.1:8760）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    # Windows 控制台 ↔ UTF-8：中文日志不乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="ai-chat-core")
    parser.add_argument("--host", default=None, help="监听地址（默认取配置）")
    parser.add_argument("--port", type=int, default=None, help="端口（默认取配置）")
    args = parser.parse_args()

    from app.main import create_app

    base = Path(__file__).resolve().parent
    app = create_app(base)
    cfg = app.state.services["store"].get().system
    host = args.host or cfg.host
    port = args.port or cfg.port

    # 落一个 pid 文件，stop.bat 靠它精确停进程
    try:
        (base / "data").mkdir(parents=True, exist_ok=True)
        (base / "data" / "app.pid").write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()