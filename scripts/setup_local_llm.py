"""把内置 llama.cpp（engines/llama.cpp）与 Models/ 里的聊天模型一键写进配置。

用法：
  .venv\\Scripts\\python.exe scripts\\setup_local_llm.py                  # 自动选 Models/ 里最大的聊天模型
  .venv\\Scripts\\python.exe scripts\\setup_local_llm.py --model 名称或完整路径
  .venv\\Scripts\\python.exe scripts\\setup_local_llm.py --port 8080 --ctx 8192
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.store import ConfigStore  # noqa: E402
from app.core.model_scan import build_llama_command, scan_models  # noqa: E402


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="", help="模型名或完整路径；留空 = 取 Models/ 里最大的聊天模型")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ctx", type=int, default=8192)
    args = ap.parse_args()

    scan = scan_models(root / "Models", refresh=True)
    engine = Path(scan["engines"]["llama_server"])
    if not engine.exists():
        print("找不到内置引擎：", engine)
        print("（应位于 engines/llama.cpp/llama-server.exe）")
        sys.exit(1)
    llms = scan["llm"]
    if not llms:
        print("Models/ 里没扫到聊天模型（.gguf）。放进去后重跑即可。")
        sys.exit(1)

    pick = None
    if args.model:
        for m in llms:
            if args.model in (m["name"], m["path"], m["file"]):
                pick = m
                break
        if pick is None:
            print("没找到指定模型。可用：")
            for m in llms:
                print("  -", m["name"])
            sys.exit(1)
    else:
        pick = max(llms, key=lambda m: m["size_mb"])

    mmproj = (pick.get("mmproj") or {}).get("path")
    cmd = build_llama_command(engine, pick["path"], port=args.port, ctx=args.ctx, mmproj=mmproj)

    store = ConfigStore(root / "config.json")
    store.load()
    store.save_patch({"llm": {
        "local_command": cmd,
        "local_auto_start": True,
        "providers": {"local": {
            "base_url": f"http://127.0.0.1:{args.port}",
            "model": pick["name"],
        }},
    }})
    print("已写入配置：")
    print("  模型:", pick["name"], f"({pick['size_mb']}MB)")
    print("  mmproj:", mmproj or "（无——视觉模型没接投影器，图片不可用）")
    print("  命令:", cmd)
    print("下一步：启动应用（或管理页「本地服务 → 启动」）验证。")


if __name__ == "__main__":
    main()