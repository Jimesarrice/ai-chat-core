"""打包发布版（不含任何模型）。

用法：
  .venv\\Scripts\\python.exe scripts\\make_release.py            # 用 app/version.py 的版本号
  .venv\\Scripts\\python.exe scripts\\make_release.py --version 0.1.1
  .venv\\Scripts\\python.exe scripts\\make_release.py --no-zip   # 只出目录

产物：dist/ai-chat-core-v<版本>-win-x64.zip（含目录）
包含：app / web / scripts / engines（内置 llama.cpp + qwentts.cpp）/ run.py / run.bat / stop.bat /
      requirements.txt / README.md（发布版说明）/ docs/ARCHITECTURE.md / 空的 Models/
不包含：模型文件、.venv、data、logs、config.json、references、docs/reference-analysis、__pycache__
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = [
    "app", "web", "scripts", "engines",
    "run.py", "run.bat", "stop.bat", "requirements.txt",
]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.log")

MODELS_TXT = """请把模型（.gguf 文件）放到这个文件夹里，程序会自动识别。

推荐（发布版不含模型，需自行下载）：
1) 聊天 / 视觉模型 —— ModelScope 或 HuggingFace 搜索：
   Qwen/Qwen3-VL-8B-Instruct-GGUF（推荐，效果更好）
   Qwen/Qwen3-VL-4B-Instruct-GGUF（轻量省显存）
   下载 Q4_K_M 主模型，例如：Qwen3VL-8B-Instruct-Q4_K_M.gguf
   想看图片还需视觉投影器：mmproj-Qwen3VL-8B-Instruct-F16.gguf（或 Q8_0 版）

2) 语音合成模型 —— Qwen3-TTS-GGUF（HuggingFace: Serveurperso/Qwen3-TTS-GGUF；ModelScope 搜同名）：
   qwen-talker-1.7b-customvoice-Q4_K_M.gguf
   qwen-tokenizer-12hz-Q4_K_M.gguf
   两个文件要成对放进来。

下载后打开 管理页 → 模型接入/语音合成，点「刷新」即可在列表里选择。
详见 README.md 第三节「模型下载」。
"""


def read_version() -> str:
    ns: dict = {}
    exec((ROOT / "app" / "version.py").read_text(encoding="utf-8"), ns)
    return str(ns.get("APP_VERSION", "0.0.0"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="")
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()
    ver = args.version or read_version()
    name = f"ai-chat-core-v{ver}-win-x64"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    stage = dist / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    for item in INCLUDE:
        src = ROOT / item
        dst = stage / item
        if src.is_dir():
            shutil.copytree(src, dst, ignore=IGNORE)
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            print(f"缺文件：{item}")
            sys.exit(1)

    # 发布版 README（正文）+ 架构文档
    (stage / "README.md").write_bytes((ROOT / "docs" / "release-README.md").read_bytes())
    (stage / "docs").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "docs" / "ARCHITECTURE.md", stage / "docs" / "ARCHITECTURE.md")

    # 空 Models + 说明（utf-8-sig：老记事本也正常）
    models = stage / "Models"
    models.mkdir()
    (models / "_放模型说明.txt").write_bytes(MODELS_TXT.encode("utf-8-sig"))

    # 引擎许可证补齐
    qwentts_license = ROOT / "engines" / "qwentts.cpp" / "LICENSE-qwentts.cpp"
    if not qwentts_license.exists() and (ROOT.parent / "qwentts.cpp" / "LICENSE").exists():
        shutil.copy2(ROOT.parent / "qwentts.cpp" / "LICENSE", qwentts_license)

    total = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
    print(f"目录就绪：{stage}")
    print(f"  文件数 {sum(1 for p in stage.rglob('*') if p.is_file())}，共 {total / 1048576:.1f} MB")

    if args.no_zip:
        return

    zip_path = dist / f"{name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    z7 = Path(r"C:\Program Files\7-Zip\7z.exe")
    if z7.exists():
        subprocess.run([str(z7), "a", "-tzip", "-mx=5", str(zip_path), name],
                       cwd=dist, check=True, stdout=subprocess.DEVNULL)
    else:
        shutil.make_archive(str(dist / name), "zip", root_dir=dist, base_dir=name)
    size = zip_path.stat().st_size / 1048576
    print(f"压缩包：{zip_path}（{size:.1f} MB）")
    print(f"sha256：{sha256(zip_path)}")


if __name__ == "__main__":
    main()