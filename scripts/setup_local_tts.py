"""把内置 Qwen3-TTS 引擎（qwentts.cpp）与 Models/ 里的模型一键写进配置。

自动识别 Models/ 中的 talker / tokenizer 并配对；也可手动指定。
用法（默认全自动）：
  .venv\\Scripts\\python.exe scripts\\setup_local_tts.py
  .venv\\Scripts\\python.exe scripts\\setup_local_tts.py --port 7865 --voice serena
  .venv\\Scripts\\python.exe scripts\\setup_local_tts.py --talker 路径 --codec 路径
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config.store import ConfigStore  # noqa: E402
from app.core.model_scan import scan_models  # noqa: E402


def _pick_engine() -> str:
    """用项目内置的 Vulkan 版引擎（发布包内 engines/qwentts.cpp，GPU 默认）。"""
    return str(ROOT / "engines" / "qwentts.cpp" / "tts-server.exe")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=_pick_engine(), help="tts-server.exe 路径（默认用内置引擎）")
    ap.add_argument("--talker", default="", help="qwen-talker-*.gguf；留空 = 自动识别 Models/")
    ap.add_argument("--codec", default="", help="qwen-tokenizer-12hz-*.gguf；留空 = 自动配对")
    ap.add_argument("--port", type=int, default=7865)
    ap.add_argument("--voice", default="serena")
    ap.add_argument("--language", default="chinese")
    args = ap.parse_args()

    scan = scan_models(ROOT / "Models", refresh=True)
    talker_path, codec_path = args.talker, args.codec
    if not talker_path or not codec_path:
        talkers = scan.get("tts_talkers") or []
        if not talkers:
            print("Models/ 里没有扫到 Qwen3-TTS 的 talker 模型。")
            print("请从 ModelScope / HuggingFace 下载 Qwen3-TTS-GGUF：")
            print("  qwen-talker-1.7b-customvoice-Q4_K_M.gguf")
            print("  qwen-tokenizer-12hz-Q4_K_M.gguf")
            print("放进 Models/ 后重跑本脚本，或用 --talker/--codec 指定路径。")
            sys.exit(1)
        t = talkers[0]
        talker_path = talker_path or t["path"]
        if not codec_path:
            paired = t.get("tokenizer") or {}
            codec_path = paired.get("path") or ""
        if not codec_path:
            print("找不到配套的 tokenizer（qwen-tokenizer-12hz-*.gguf），请一并放进 Models/")
            sys.exit(1)

    engine, talker, codec = Path(args.engine), Path(talker_path), Path(codec_path)
    missing = [str(p) for p in (engine, talker, codec) if not p.exists()]
    if missing:
        print("这些文件不存在，先核对路径：")
        for m in missing:
            print("  -", m)
        sys.exit(1)

    command = (
        f'"{engine}" --model "{talker}" --codec "{codec}" '
        f"--host 127.0.0.1 --port {args.port}"
    )
    store = ConfigStore(ROOT / "config.json")
    store.load()
    store.save_patch({"tts": {
        "enabled": True,
        "backend": "qwentts",
        "base_url": f"http://127.0.0.1:{args.port}",
        "model": talker.stem,
        "voice": args.voice,
        "local_language": args.language,
        "local_command": command,
        "local_auto_start": True,
        "local_device": "gpu",     # 默认 GPU；慢/爆显存可在界面切 cpu
    }})
    c = store.get().tts
    print("已写入配置：")
    print("  backend:", c.backend, "| base_url:", c.base_url, "| voice:", c.voice,
          "| device:", c.local_device, "| auto_start:", c.local_auto_start)
    print("  启动命令:", c.local_command)
    print("下一步：启动应用（或管理页「本地部署 → 启动」）后点「试合成一句」验证。")


if __name__ == "__main__":
    main()