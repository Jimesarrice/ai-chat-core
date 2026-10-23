# 🐟 ai-chat-core v0.1.0（发布版 · 不含模型）

通用可扩展的 AI 大模型聊天接入模块：**网页聊天 + OneBot QQ 机器人 + 本地 llama.cpp(Vulkan)/云端双路径模型接入**，
带人设卡、记忆、心情、主动搭话、联网搜集、Qwen3-TTS 语音合成，以及完整的**配置与实时日志界面**。

- 内置 **llama.cpp Win-Vulkan 引擎**（`engines/llama.cpp`）与 **Qwen3-TTS Vulkan 引擎**（`engines/qwentts.cpp`）
- **本发布版不包含任何模型文件**，模型请按下文自行下载，放进项目里的 `Models/` 目录即可被自动识别
- 全部开箱即用：解压 → 装 Python 依赖 → 放模型 → `run.bat`

---

## 一、运行环境

- Windows 10/11 64 位（本版内置引擎为 Windows x64 + Vulkan；AMD / NVIDIA / Intel 显卡通用）
- [Python 3.10+](https://www.python.org/downloads/)（建议 3.12，安装时勾选 Add to PATH）
- 磁盘：程序约 150MB + Python 依赖约 100MB + 模型（见下，按需 1~8GB）

## 二、快速开始

```bat
:: 1. 在解压目录执行一次（建虚拟环境 + 装依赖）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
:: 国内网络可加镜像：
:: .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

:: 2. 下载模型放进 Models\（见第三节，必读）

:: 3. 启动
run.bat
```

打开 <http://127.0.0.1:8760>（聊天页）与 <http://127.0.0.1:8760/admin.html>（配置与日志）。
停止用 `stop.bat`（会把本地 llama-server / TTS 服务一起停掉）。

> 首次启动会自动生成默认 `config.json` 与 `data/`、`logs/`。云端 API Key 也可以之后在配置页再填。

## 三、模型下载（发布版不含模型，需自行下载）

> 推荐国内用 [ModelScope（魔搭）](https://modelscope.cn/)，海外用 [HuggingFace](https://huggingface.co/)。
> 把下载的 `.gguf` 直接放进项目里的 **`Models/`** 目录（子目录也会扫描），界面会自动识别。

### 1) 聊天 / 视觉模型（llama.cpp 用）

| 推荐 | 仓库 | 下载文件 |
|---|---|---|
| **Qwen3-VL-8B-Instruct-GGUF**（效果更好） | ModelScope：[Qwen/Qwen3-VL-8B-Instruct-GGUF](https://modelscope.cn/models/Qwen/Qwen3-VL-8B-Instruct-GGUF)<br>HuggingFace：[Qwen/Qwen3-VL-8B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF) | `Qwen3VL-8B-Instruct-Q4_K_M.gguf`<br>`mmproj-Qwen3VL-8B-Instruct-F16.gguf` |
| **Qwen3-VL-4B-Instruct-GGUF**（轻量省显存） | ModelScope：[Qwen/Qwen3-VL-4B-Instruct-GGUF](https://modelscope.cn/models/Qwen/Qwen3-VL-4B-Instruct-GGUF)<br>HuggingFace：[Qwen/Qwen3-VL-4B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF) | `Qwen3VL-4B-Instruct-Q4_K_M.gguf`<br>`mmproj-Qwen3VL-4B-Instruct-F16.gguf` |

- **量化选 Q4_K_M**（体积/质量最均衡，8B 约 5GB，4B 约 2.5GB）。
- **`mmproj` 是视觉投影器**：想看图片/截图就必须一并下载（约 0.75~1.2GB，官方只有 F16/Q8_0 两档，选 Q8_0 更省）；
  只做纯文本聊天可以不放。
- 下载命令行示例（ModelScope）：

```bat
.venv\Scripts\python.exe -m pip install modelscope
.venv\Scripts\python.exe -m modelscope download --model Qwen/Qwen3-VL-8B-Instruct-GGUF Qwen3VL-8B-Instruct-Q4_K_M.gguf mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf --local_dir Models
```

### 2) 语音合成模型（Qwen3-TTS，qwentts.cpp 用）

| 仓库 | 下载文件 |
|---|---|
| **Qwen3-TTS-GGUF**<br>HuggingFace：[Serveurperso/Qwen3-TTS-GGUF](https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF)<br>ModelScope：搜「Qwen3-TTS-GGUF」 | `qwen-talker-1.7b-customvoice-Q4_K_M.gguf`（约 1.2GB）<br>`qwen-tokenizer-12hz-Q4_K_M.gguf`（约 255MB） |

- 两个文件**成对使用**，必须放同一个 `Models/` 里；量化同样推荐 **Q4_K_M**。
- `customvoice` 版自带 9 个音色（serena/vivian/uncle_fu/ryan/aiden/ono_anna/sohee/eric/dylan）
  与 12 种语言（含北京话/四川话），无需参考音频。

> 模型许可与使用条款以各自仓库为准。发布版仅提供引擎与代码，不分发任何权重。

## 四、接上本地模型（三步）

1. 配置页 → **模型接入** → 「本地 llama.cpp」卡片：
   模型下拉里应能看到 `Models/` 里的模型（带大小与"视觉 ✓ / 缺 mmproj"标记）；
2. 选模型 →「**用这个模型**」→「**🧩 生成启动命令**」（自动带上引擎路径、`--mmproj`，并设开机自动拉起）；
3. 「**▶ 启动本地服务**」→「检测」显示"有服务在听"即可。
   任务分工里 `chat` 指向 `local`（默认就是），回聊天页发消息即走本地显卡推理。

> 也可以在命令行一键：`.venv\Scripts\python.exe scripts\setup_local_llm.py`（自动选 Models/ 里最大的聊天模型）。

**接语音合成**（可选）：`.venv\Scripts\python.exe scripts\setup_local_tts.py`，
或在「语音合成 → 本地部署」里选模型/音色/**推理设备（默认 GPU，可切 CPU）**→ 启动 → 试合成一句。
> ⚠ 本地部署大语言模型时可能会抢占显存 —— TTS 变慢或报错时切 CPU，或调小 LLM 的 `-ngl`/`-c`。

## 五、接云端 API（可选，两条路径可分工）

配置页 → 模型接入 → `cloud` 接入点：填 OpenAI 兼容地址（如
`https://dashscope.aliyuncs.com/compatible-mode`）、API Key、模型名（如 `qwen3-max`）。
「任务分工」里可把 记忆提炼 / 心情判断 / 起标题 / 联网归纳 指到云端，聊天仍走本地——省钱又省显存。
默认**低思考模式**（自动带厂商参数，可在接入点里改）。

## 六、QQ 机器人（可选）

1. 本机装并登录一个 OneBot 11 后端：[NapCat](https://github.com/NapNeko/NapCatQQ)（推荐，网页后台扫码）
   或 [SnowLuma](https://github.com/SnowLuma/SnowLuma)，开启 HTTP(3000) 和/或 WebSocket(3001)；
2. 配置页 → QQ 桥接：连接方式 `forward_ws`（推荐）/ `reverse_ws` / `http`，填「主人的 QQ 号」，群聊填允许的群号；
3. 安全默认：总开关关、试运行开——先点「📨 真发测试句」验证，再逐步打开；
4. 改连接方式后**重启一次程序**。图片双向、语音条都可选。

## 七、常见问题

| 现象 | 解决 |
|---|---|
| 模型没被识别 | 放进 `Models/`（`.gguf`），刷新配置页；扫描有 30 秒缓存，点「刷新」可强刷 |
| 图片看不了 / 显示"缺 mmproj" | 补下 `mmproj-*.gguf` 放进 `Models/`，重新「生成启动命令」 |
| 本地服务显示 HTTP 503 / 连不上 | 程序会自动为本地地址设置 `NO_PROXY`；若你在外部脚本里调本地端口，也要设 `NO_PROXY=127.0.0.1,localhost` |
| TTS 太慢 / 显存不足 | 语音合成 → 本地部署 →「推理设备」切 CPU → 停止再启动；或降低 LLM 的 `-ngl` / 上下文 |
| 双击 bat 中文乱码 | 内置 bat 为 **GBK + CRLF**（cmd 原生）。用编辑器改后请另存为 ANSI/GBK，**不要**加 `chcp 65001` |
| 想换端口 | 配置页 → 系统 → 端口；本地 llama-server/TTS 端口在各自启动命令里改 |
| 重置全部数据 | 停程序，删 `config.json`、`data/`、`logs/` 即回出厂 |

## 八、目录速览

```
ai-chat-core/
├── run.bat / run.py / stop.bat    # 启动 / 停止
├── requirements.txt
├── Models/                        # 模型目录（本发布版为空，放 .gguf 进来）
├── engines/
│   ├── llama.cpp/                 # 内置 llama.cpp Win-Vulkan（llama-server 等）
│   └── qwentts.cpp/               # 内置 Qwen3-TTS Vulkan 引擎（tts-server）
├── app/  web/  scripts/  docs/ARCHITECTURE.md
├── data/  logs/  config.json      # 首次运行自动生成
└── README.md                      # 本文件
```

## 九、许可与致谢

本项目代码 MIT。内置/对接的开源组件：
[llama.cpp](https://github.com/ggml-org/llama.cpp)（MIT）、
[qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp)（Qwen3-TTS 的 C++/GGML 移植）、
[bilibili-api-python](https://github.com/Nemo2011/bilibili-api)（MIT）。
功能设计参考 Yuuyuko-uu 的 6 个 DSH 插件（MIT）。
使用 B站 / QQ 等平台时请遵守平台协议；公开发布 AI 生成内容请按平台要求标注。