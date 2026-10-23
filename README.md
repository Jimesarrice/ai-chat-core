# 🐟 ai-chat-core

> **源码仓库说明**：本仓库只放源码——**不含二进制引擎（`engines/`）与任何模型文件**。
> 完整运行包（内置 llama.cpp Win-Vulkan 与 qwentts.cpp TTS Vulkan 引擎）见 **Releases** 中的
> `ai-chat-core-v0.1.0-win-x64.zip`。模型请按下文自行下载（ModelScope / HuggingFace，
> 推荐 `Qwen3-VL-4B/8B-Instruct-GGUF` 与 `Qwen3-TTS-GGUF` 的 **Q4_K_M** 量化）。

**通用可扩展的 AI 大模型聊天接入子模块**：一条聊天管道串起网页对话、QQ 机器人、本地/云端大模型，
外加从 6 个 DSH 插件移植来的「记忆 / 心情 / 主动搭话 / 联网搜集 / QQ 桥 / 朗读」能力。
自带配置页与实时日志页，全部零构建（Python + 原生前端），开箱即用。

- 🤖 **双路径模型接入**：本地 llama.cpp（Vulkan 后端）与云端 OpenAI 兼容 API，**按任务分工**
  （聊天走本地、记忆/心情/起标题走云端……各选各的）；默认**低思考模式**省 token；支持多模态图片。
- 💬 **网页聊天**：流式回复、图片上传、多会话、消息点播朗读（Qwen3-TTS）。
- 🧠 **鱼的记忆**：会淡忘（半衰期随重要度伸缩）、按证据召回、事实键只留最新、带时间注入、
  旧档案导入与提炼、召回诊断（为什么没想起来）。
- 🌊 **鱼的心情**：每轮用模型判一次心情走向；指数衰减、可信度门槛、「一次变化只说一次」。
- ⏰ **主动搭话**：骰子算法（峰谷命中线×时机因子+没中加成），安静时段 / 冷却 / 试运行，安全默认。
- 📱 **QQ 桥**：OneBot 11（NapCat/SnowLuma），正/反向 WS 或 HTTP 轮询，白名单 + 图片双向 + 语音条。
- 🔍 **联网搜集**：网页搜索（SearXNG/DDG）+ 正文抓取 + B站采集（搜索/字幕/评论）+
  `bili_scan`（内建扫描代理：自己去搜、读字幕、翻评论，交回报告）。
- ⚙️ **配置与日志 UI**：所有配置项带中文说明、热生效；日志实时看（SSE）；记忆/心情/搭话/收藏全可视化。
- 🧩 **可扩展**：加功能 = 加一个模块；加配置 = 加一行字段（配置页自动出现）；加通道 = 实现一个协议。

> 本项目的功能是 [Yuuyuko-uu](https://github.com/Yuuyuko-uu) 的 6 个 DSH 插件（MIT）的**独立重实现**：
> `dsh-fish-memory`（鱼的记忆）、`dsh-feiyu-mood`（鱼的心情）、`dsh-speak-up`（主动搭话）、
> `dsh-qq-bridge`（通到QQ）、`dsh-tts-bridge`（朗读桥）、`dsh-bili-subagent`（刷B站）。
> 算法公式、常量与提示词按源码逐字移植，机制规格书见 [`docs/reference-analysis/`](docs/reference-analysis/)。
> 与原项目及 DeepSeek 官方均无关联。

---

## 快速开始（Windows）

```bash
cd ai-chat-core
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py          # 或双击 run.bat；停止用 stop.bat
```

内置了 llama.cpp Vulkan 引擎（`engines/`）与本地 TTS 引擎接入；把 `.gguf` 模型（聊天 / Qwen3-TTS）放进
项目里的 `Models/` 目录，配置页会自动识别出来供选择。

打开 <http://127.0.0.1:8760>（聊天页）与 <http://127.0.0.1:8760/admin.html>（配置与日志）。

**第一步建议**：先只说聊天——去「模型接入」把**云端 API** 填上（地址 + Key + 模型名），
或按下面把**本地 llama.cpp** 跑起来；任一条通了就能聊。

## 模型接入

### 云端（OpenAI 兼容）

配置页 → 模型接入 → `cloud` 接入点：

| 字段 | 例（阿里云百炼） | 例（其它中转） |
|---|---|---|
| 服务地址 | `https://dashscope.aliyuncs.com/compatible-mode` | 你的中转 `/v1` 前的地址 |
| API Key | `sk-...` | 中转的 key |
| 默认模型 | `qwen3-max` / `deepseek-chat` 等 | 按中转文档填 |

「思考模式」默认**低思考**：dashscope 会带 `enable_thinking:false`、openai 系会带
`reasoning_effort:low`；不想要的厂商参数可在「思考参数风格」里选 `none`，或直接改「额外请求体」。

### 本地 llama.cpp（Vulkan 后端）——引擎已内置，模型自动识别

**引擎内置**：`engines/llama.cpp/`（llama.cpp Win-Vulkan 版，AMD/Intel/NVIDIA 通吃，随 Releases 发布包分发；
本源码仓库不含二进制，见顶部说明）。不用自己下载编译。

**模型目录**：项目里建一个 `Models/`，把 `.gguf` 丢进去即可（子目录也会找）。

> **模型去哪下（发布版不含模型）**：到 [ModelScope](https://modelscope.cn/) 或 [HuggingFace](https://huggingface.co/) 下载——
> 聊天/视觉：`Qwen/Qwen3-VL-8B-Instruct-GGUF`（效果更好）或 `Qwen/Qwen3-VL-4B-Instruct-GGUF`（省显存），
> 主模型选 **Q4_K_M**，看图再加 `mmproj-Qwen3VL-*-Instruct-F16.gguf`；
> 语音：`Qwen3-TTS-GGUF`（HF: `Serveurperso/Qwen3-TTS-GGUF`）的
> `qwen-talker-1.7b-customvoice-Q4_K_M.gguf` + `qwen-tokenizer-12hz-Q4_K_M.gguf`（成对）。
> 详细文件名与下载命令见 `docs/release-README.md`（发布版 README）。

**用法（全在配置页完成）**：模型接入 → 「本地 llama.cpp」卡片：

1. **检测到的模型**下拉自动列出 `Models/` 里的聊天模型（会读 GGUF 元数据标出大小、是否视觉模型、缺不缺 mmproj）；
2. 选中 →「**用这个模型**」（写进 local 接入点）→「**🧩 生成启动命令**」（自动带上引擎路径与 `--mmproj`，并设为开机自动拉起）；
3. 「**▶ 启动本地服务**」→「检测」→ 变成"有服务在听"就能用；
4. 聊天任务分工里 `chat` 指向 `local` 即走本地显卡推理（实测 8B Q4：生成 100+ t/s，首问多几秒是一次性开销）。

命令行一键配置也行：`python scripts\setup_local_llm.py`（自动挑 Models/ 里最大的模型）。

> 视觉模型（Qwen3VL 等）要**看图**还需要配套的 `mmproj`（投影器）文件，从该模型发布页下载后放进 `Models/`，
> 「生成启动命令」会自动带上；缺了就只能文本聊天。

### 手动方式（可选）

```bat
:: 想自己接管也行：
::（引擎在 engines\llama.cpp\llama-server.exe，实测端口 8080）
engines\llama.cpp\llama-server.exe -m Models\你的模型.gguf --host 127.0.0.1 --port 8080 -ngl 99 -c 8192
:: 视觉模型：
... -m Models\xx.gguf --mmproj Models\mmproj-xx.gguf --host 127.0.0.1 --port 8080 -ngl 99
```

**任务分工**：聊天默认走 `local`，记忆/心情/起标题/扫描默认走 `cloud`（便宜、低思考）。
只想要一条路径也行——把几个任务的接入点都改成同一个。

## QQ 接入（OneBot 11）

1. 本机跑一个 OneBot 11 后端（二选一，都用过）：
   - [NapCat](https://github.com/NapNeko/NapCatQQ)：下载 Windows 版 → 启动 → 网页后台（默认 `http://127.0.0.1:6099`）扫码登录；
   - [SnowLuma](https://github.com/SnowLuma/SnowLuma)：同理。
2. 在 NapCat 里开 **HTTP 服务（3000）** 与/或 **WebSocket 服务（3001）**；
3. 配置页 → QQ 桥接：
   - 连接方式：`forward_ws`（我们连 `ws://127.0.0.1:3001`，推荐）/ `reverse_ws`（后端连我们 `http://<本机>:8760/onebot/ws`）/ `http`（轮询消息历史，无需 WS）；
   - 填「主人的 QQ 号」（私聊只接这个号；群聊填允许的群号并默认需要 @）；
   - 总开关（默认关）与试运行（默认开）都检查一遍，先点「📨 真发测试句」验证通道；
4. **改连接方式后重启一次进程**（收消息的循环在启动时挂）。

图片双向：QQ 发来的图会存 `data/media/qq_inbox/` 并把路径写进文字；
它回复里写 `![名字](相对路径)` 会被解析成本地图发到 QQ（不露 markdown）。

## 语音合成（Qwen3-TTS）——支持本地部署

四种后端，配置页 → 语音合成里切：

| 后端 | 说明 | 需要 |
|---|---|---|
| `qwentts` | **本地 qwentts.cpp**（GGUF + CPU/Vulkan，推荐本地路线）：9 个预设音色、12 种语言（含北京话/四川话） | 见下面「本地部署」 |
| `dashscope` | 阿里云百炼 `qwen3-tts-flash`（音色 Cherry/Serena/Ethan/Chelsie） | API Key（填配置或用环境变量 `DASHSCOPE_API_KEY`） |
| `local_http` | 其它本地 Qwen3-TTS 服务；按 `POST {地址}/tts` 说话，收到 raw wav / `audio_base64` / `url` 都能接 | `base_url` 指到服务 |
| `openai_compat` | 任何 `/v1/audio/speech` 兼容端点 | 地址 + Key |

打开总开关后：网页每条回复有「▶ 朗读」（可开自动播放）；QQ 侧打开「语音回复」会发语音条
（NapCat 接受 wav，自动转码）。「试合成一句」按钮可先验证。

### 本地部署（qwentts.cpp + GGUF 模型）

1. **模型**（两个文件成对使用，放任意目录）：`qwen-talker-<尺寸>-<模式>-<量化>.gguf` +
   `qwen-tokenizer-12hz-<量化>.gguf`（本机已放好在项目根目录）；
2. **引擎**（一次编译，CPU/显卡通用）：

   ```bash
   git clone --recurse-submodules https://github.com/ServeurpersoCom/qwentts.cpp
   cd qwentts.cpp && mkdir build && cd build
   # 用 VS 自带 CMake（无需单独装）：
   "C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" .. -G "Visual Studio 17 2022" -A x64
   # 上面那条命令 --build 一下：
   "...\cmake.exe" --build . --config Release --parallel
   ```

   想要**显卡加速**（AMD/NVIDIA 通吃）：装 [Vulkan SDK](https://vulkan.lunarg.com/) 后把第一条命令加 `-DGGML_VULKAN=ON` 重编，
   启动参数加 `--device Vulkan0`。
3. **接进来**：跑一次 `python scripts/setup_local_tts.py`（把引擎与模型路径写进配置，本机默认值已对）；
   或手动：后端选 `qwentts`、地址 `http://127.0.0.1:7865`、「本地启动命令」填
   `"…\tts-server.exe" --model "…qwen-talker-*.gguf" --codec "…qwen-tokenizer-12hz-*.gguf" --port 7865`；
4. 配置页 → 语音合成 → 「本地部署」卡片：「▶ 启动本地服务」→「检测」→「试合成一句」；
   打开「开机自动拉起」后，本程序启动时会自动把 TTS 服务带起来。

**设备选择（GPU / CPU）**：「本地部署」卡片 →「推理设备」——**默认 GPU（Vulkan 加速）**，
可随时切到 CPU（不占显存、慢一些）；切完点「停止 → 启动」生效。
> ⚠ **本地部署大语言模型时可能会抢占显存** —— TTS 若变慢或报“显存不足”，把设备切到 CPU，
> 或降低 LLM 的显存占用（`-ngl` / `-c`）即可。
> 本项目的 TTS 引擎内置在 `engines/qwentts.cpp`（**Vulkan 版**，默认走 GPU；同一切换器选 CPU 时不占显存）。

**音色**（customvoice 模型）：`serena` / `vivian` / `uncle_fu`（中文）· `dylan`（北京话）· `eric`（四川话）·
`ryan` / `aiden`（英文）· `ono_anna`（日语）· `sohee`（韩语）；**语言**：chinese/english/japanese/korean/french/
german/italian/portuguese/spanish/russian/beijing_dialect/sichuan_dialect。
本机实测（CPU 16 线程、1.7B Q4_K_M）：约 0.5~0.8 倍语速实时合成；Vulkan 版会明显更快。

## 联网搜集

- **网页搜索**：配置里选 `searxng`（填你自建/公共实例地址，推荐）或 `duckduckgo`（免密钥，需能上外网）；
- **B站采集**：可匿名用；要更稳在「联网搜集 → 扫码登录」用手机 B站 扫（选「记住我」），凭证存本机（0600）；
- 模型聊天时可自己决定调用 `web_search / web_fetch / bili_scan / bili_search / bili_subtitle / bili_comments`；
  `bili_reply / bili_send_message`（对外发评论/私信）默认关闭，要用再去「工具开关」里打开；
- 扫描代理会把每次报告追加写 `data/bili/out/<日期>.md`。

## 自测（不用真实模型也能跑）

```bat
:: 三个终端：
.venv\Scripts\python.exe scripts\mock_llm.py --port 8081
.venv\Scripts\python.exe scripts\mock_onebot.py --port 3000
.venv\Scripts\python.exe run.py
:: 第四个终端跑断言（20 项：聊天/标题/心情/记忆/召回/搭话/QQ 双向/配置/日志）：
.venv\Scripts\python.exe scripts\e2e_test.py

:: 没有本地 TTS 时，也可以用 mock 语音服务把 TTS 链路跑通：
.venv\Scripts\python.exe scripts\mock_tts.py --port 7866
```

把「模型接入 → local」暂时指到 `http://127.0.0.1:8081`（模型名 `mock`）、
QQ 指向 `http://127.0.0.1:3000` 即可全链路演练。

## 数据在哪 / 怎么备份

- `config.json`：全部配置（含密钥，别提交到公开仓库）；
- `data/state.db`：会话与消息（SQLite）；
- `data/memory/store.json`：记忆库（纯 JSON；每次改动自动 `.bak` + 每天一份留 14 天）；
- `logs/app.log`：日志（界面上能实时看）；
- 想重置：停服务，删 `data/` 与 `config.json` 即回到出厂默认。

## 常见问题

| 现象 | 怎么办 |
|---|---|
| 聊天报「连不上模型服务」 | 本地：llama-server 起了吗、端口对不对；云端：地址要填到 `/compatible-mode` 级（不要带 `/v1/chat/completions`） |
| 云端报 401 | API Key 错了/过期；检查接入点里粘贴有没有多余空格 |
| 多模态图片不生效 | 本地要带 `--mmproj` 启动；云端要选支持图像的模型；接入点「支持多模态」开关要开 |
| 记忆一直想不起来 | 看「记忆 → 最近一次召回」：证据不够 / 时间太久淡了 / 问句太短被信息量门挡住（常见原因都写着） |
| 主动搭话没动静 | 试运行默认开着（只记账）；还要在「目标会话」里勾上至少一个会话 |
| QQ 收不到消息 | 后端 WS/HTTP 开了吗；总开关开没开；「主人的 QQ 号」填对没；群聊要 @ 它；改过连接方式要重启 |
| 语音不出声 | 先点「试合成一句」看通不通；dashscope 要 Key；本地后端地址要点到服务根路径 |
| 本地服务「检测」显示 HTTP 503 / 连不上 | 大概率是**系统代理**把 127.0.0.1 也劫持了：本程序启动时会自动写 NO_PROXY 绕开；若你在外面用脚本调它，记得给脚本也设 `NO_PROXY=127.0.0.1,localhost` |
| 本地合成太慢 / 报“显存不足” | 语音合成 → 本地部署 →「推理设备」：默认 GPU（Vulkan），可切 CPU 再重启服务。注意**本地部署大语言模型时可能会抢占显存**，两者同卡时把其中一个调小（LLM 的 `-ngl`/`-c`）或让 TTS 走 CPU |
| 双击 bat 出现乱码或报奇怪的"不是内部或外部命令" | `run.bat`/`stop.bat` 是**无 BOM 的 GBK 编码 + CRLF 行尾**（cmd 原生读取，中文正常）。用编辑器改动后请另存为 **ANSI/GBK** 且保留 CRLF；**不要**在脚本里加 `chcp 65001`——UTF-8 批处理在 cmd 下会串行解析（已知坑） |
| 日志页没动静 | 确认实时日志级别不是 ERROR 以上；切到 DEBUG 看更细 |

## 许可与致谢

本仓库代码 MIT。机制移植自 Yuuyuko-uu 的 6 个 MIT 插件（见文件头）；B站采集走
[bilibili-api-python](https://github.com/Nemo2011/bilibili-api)（MIT）；本地 TTS 走
[qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp)（Qwen3-TTS 的 C++/GGML 移植）与
[Serveurperso/Qwen3-TTS-GGUF](https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF) 模型。
涉及各平台（B站/QQ）使用时请遵守平台协议，公开发布 AI 生成内容请按平台要求标注。
本项目由蓝色大肥鱼DeepSeek深度参与，感谢梁总发放的鲸圆券！
