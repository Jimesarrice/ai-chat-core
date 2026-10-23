# ai-chat-core 架构设计

> 通用可扩展的 AI 大模型聊天接入子模块：配置 + 日志 UI、网页聊天、OneBot QQ 桥、
> llama.cpp（Vulkan）本地模型与 OpenAI 兼容云 API 双路径、按任务分工，
> 以及从 6 个 DSH 插件移植的「心情 / 记忆 / 主动搭话 / 联网搜集 / QQ 桥 / 朗读桥」能力（TTS 用 Qwen3-TTS）。

---

## 1. 总览

```
┌─────────── 通道层 (channels/) ────────────┐
│  WebChannel（WS 推送/流式）    OneBotChannel（QQ 桥） │
└──────────────┬────────────────────────────┘
               │ InboundMessage
        ┌──────▼───────────────────────────────┐
        │ ChatPipeline（core/pipeline.py）        │
        │ 组装：人设卡 + 记忆注入 + 心情注入 + 时间  │
        │ 生成：LLM 流式 + 工具循环（≤N 轮）        │
        │ 落库 → 分发 → 后处理（记忆/心情/标题/TTS） │
        └──┬────────┬────────┬────────┬─────────┘
           │        │        │        │
   ┌───────▼──┐ ┌───▼────┐ ┌─▼─────┐ ┌▼─────────┐
   │ LLMRouter│ │ Memory │ │  Mood │ │ TTS 服务  │
   │ 任务分工 │ │ 鱼的记忆│ │鱼的心情│ │ Qwen3-TTS │
   └───────┬──┘ └────────┘ └───────┘ └──────────┘
           │            SpeakUp（定时掷骰）· ToolRegistry（web/bili/remember）
   ┌───────▼─────────┐
   │ 本地 llama.cpp   │   ← 两条路径，同一个 OpenAI 兼容客户端
   │ (Vulkan) / 云 API │
   └─────────────────┘
```

## 2. 目录结构

```
ai-chat-core/
├── run.py / run.bat          # 启动入口（默认 127.0.0.1:8760）
├── config.json               # 运行时配置（首次启动自动生成，也可只用默认值）
├── requirements.txt
├── app/
│   ├── main.py               # 装配（create_app）：配置→日志→存储→通道→能力→路由
│   ├── logbus.py             # 日志总线：文件(.log 轮转) + 内存环 + SSE 推给 UI
│   ├── version.py
│   ├── core/
│   │   ├── model_scan.py     # 扫描 Models/：按 GGUF 元数据分类（llm/tts_talker/tts_tokenizer/mmproj）
│   │   ├── proc_manager.py   # 托管本地服务进程（llama-server / tts-server 共用：启停/日志/探测）
│   ├── config/
│   │   ├── schema.py         # Pydantic 配置模式（每个字段带中文 label/desc/ui → 配置页自动渲染）
│   │   └── store.py          # 加载/深合并/保存/热生效/UI schema 导出
│   ├── storage/db.py         # SQLite：会话/消息/心情时间线/收藏/搭话记录/KV
│   ├── llm/
│   │   ├── client.py         # OpenAI 兼容客户端（流式/多模态/工具调用/重试/人话错误）
│   │   └── router.py         # 任务→接入点 路由 + 思考模式（默认低思考）
│   ├── core/
│   │   ├── pipeline.py       # 聊天管道（本模块的心脏）
│   │   ├── types.py          # InboundMessage / TurnContext
│   │   └── utils.py          # JSON 宽松解析 / 时间 / 文本工具
│   ├── channels/
│   │   ├── base.py           # 通道抽象 + ChannelHub（出站分发）
│   │   ├── web.py            # 网页通道（WS 广播：流式 delta/消息/语音/错误）
│   │   └── onebot.py         # OneBot v11：正向 WS / 反向 WS / HTTP 轮询（三选一）
│   ├── features/
│   │   ├── memory/           # 鱼的记忆：core.py（召回算法）prompts.py（提示词）admin.py（批量整理）
│   │   ├── mood.py           # 鱼的心情：判断/衰减/注入（提示词逐字移植）
│   │   ├── speakup.py        # 主动搭话：掷骰/峰谷/安静时段/冷却/记录
│   │   ├── tts/              # 朗读桥→Qwen3-TTS：engines.py（qwentts/dashscope/local_http/openai 四后端）
│   │   │                     #   service.py（队列 + 本地服务托管进程：启动/停止/检测/日志）text_clean.py
│   │   └── search/           # 联网搜集：websearch.py（搜索/抓取）bilibili.py（采集+扫描代理）tools.py
│   └── api/                  # FastAPI 路由：routes_chat / routes_admin_core / routes_memory
├── web/                      # 前端（零构建，原生 HTML/CSS/JS）
│   ├── index.html + js/chat.js     # 聊天页
│   ├── admin.html + js/admin.js    # 配置与日志页（schema 驱动）
│   └── css/app.css + js/common.js
├── engines/llama.cpp/        # 内置 llama.cpp Win-Vulkan 引擎（llama-server 等，装机即用）
├── engines/qwentts.cpp/      # 内置 Qwen3-TTS Vulkan 版引擎（GPU 默认，可 --device CPU 走 CPU）
├── Models/                   # 本地模型目录：丢 .gguf 进去，界面自动识别可选
├── scripts/
│   ├── mock_llm.py           # 自测用：OpenAI 兼容 mock 模型（含流式/工具调用）
│   ├── mock_onebot.py        # 自测用：OneBot HTTP mock 后端
│   ├── mock_tts.py           # 自测用：正弦波 mock TTS（/tts 与 /v1/audio/speech 两种分支）
│   ├── setup_local_llm.py    # 一键：内置引擎 + Models/ 里的聊天模型 → 写配置并可自动拉起
│   ├── setup_local_tts.py    # 一键：qwentts.cpp + talker/tokenizer → 写配置并可自动拉起
│   └── e2e_test.py           # 端到端自测（20 项断言）
├── data/                     # 运行时数据（state.db / memory / media / tts / bili / llm / app.pid）
├── logs/app.log              # 轮转日志
├── docs/
│   ├── ARCHITECTURE.md       # 本文
│   └── reference-analysis/   # 6 个参考仓库的源码机制规格书（移植依据）
└── references/               # 6 个参考仓库源码（本地分析用）
```

## 3. 一轮消息的完整生命周期

1. **进站**：WebSocket（浏览器）/ OneBot（QQ）→ `InboundMessage{channel, conv_id, text, images, source}`；
   - QQ 侧已做：白名单（只认主人 QQ）、message_id 去重、图片三形态抠取落盘、群触发（@/前缀）、试运行门。
2. **调用**：`ChatPipeline.handle_inbound` → 会话级 `asyncio.Lock` 串行化（并发多轮不会交叉）。
3. **组装上下文**（三个来源，全部热生效）：
   - 人设卡（persona）→ 系统提示词；
   - `MemoryService.recall_injection`：分词→库内/通用双层稀有度→三道问句词筛选→证据 C→总分 P→
     条数分档→去重→事实名额→字数预算→注入文本（**每条带时间**），并写 trace 诊断（为什么没想起来）；
   - `MoodService.injection_text`：「一次变化只说一次」的心情段落（过去式、别念出来）。
   - 记忆/心情都以**追加消息**形式插在当前 user 消息前，不动主系统提示（对前缀缓存友好）。
4. **生成**：`LLMRouter.resolve("chat")` → 流式输出；web 端 delta 实时推送；模型可调工具（≤ tool_max_rounds 轮）：
   - `remember`（记忆，含每天配额/去重强化/事实键收敛）
   - `web_search` / `web_fetch` / `bili_search` / `bili_subtitle` / `bili_comments` / `bili_video_info`
   - `bili_scan`（内建扫描"子代理"：只读工具 + SCAN 提示词原文 + 循环，交回报告）
   - `bili_reply` / `bili_send_message`（对外说话，默认关）
5. **落库**：assistant 消息写 SQLite（含模型名、用量）。
6. **分发**：web → `done` 事件（页面重取权威消息）；QQ → `send_message`（按空行拆段、450ms 节流、markdown 抠图发送）。
7. **后处理**（异步、绝不阻塞、异常只记日志）：
   - 记忆「像用上了」标记（consumed）；
   - 心情判断（口径按 source：`speak-up`=搭话轮 / `bili-duty-scan`=干活轮 / 其余=正常轮）；
   - 自动起标题；TTS（web 自动播放 / QQ 语音条）。

## 4. 能力 ↔ 参考仓库对照

| 能力 | 参考 | 移植要点（逐字级） |
|---|---|---|
| 记忆 | `dsh-fish-memory` | 鲜活度 `A0·2^(-天/H)，H=半衰期×(1+2×重要度)`；证据 `C=s/(s+K)`，`s=Σ 命中词稀有度×说话稀有度×价`；**门槛只卡 C**；分档 `C≥0.6→6 条 / ≥0.5→4 / ≥0.4→3 / 其余 1`；时间词只提排序 ×1.3；整句命中顶 C=0.85；事实键收敛"分不出新旧就不动"；8 种 omission 诊断；提炼/洗键/扩词/补标提示词全文 |
| 心情 | `dsh-feiyu-mood` | 七情绪 + mood 0-100 基线 70；`round(moodDelta/3)` 步长（校准常数）；指数衰减（心情半衰 12h、情绪 90min）；「一次变化只说一次」+ confidence 门槛 + 每日上限 + 小波动不记账；三行注入模板逐字 |
| 主动搭话 | `dsh-speak-up` | `命中线 = min(100, max(0, 基础×时机因子 + min(上限, 连没中×加成)))`（先乘再加）；峰谷时段（周一~五 9-12/14-18，其余含周末谷时）；安静时段"含起不含止、起===止=不启用"；冷却=「我们自己上次开口」；掷值 `<` 线才算中；记录文案逐字 |
| QQ 桥 | `dsh-qq-bridge` | 段数组解析（text/image/mface、CQ 串兜底）、message_id 去重（LRU 300）、图片三形态、`inbox/<msgid>-<n>.<ext>` 落盘、文案附路径、markdown `![alt](path)` 抠图、按空行拆段 + 450ms 节流、安全默认（试运行开） |
| 朗读桥→TTS | `dsh-tts-bridge` | 队列上限 6 丢最旧、去颜文字四条正则、同文本不重复排、合成超时判死、收藏（分组/导出 md）、"点播 vs 自动"两档 |
| 联网搜集 | `dsh-bili-subagent` | 硬门槛在**工具层**过滤（多抓 4 倍再筛）；"没有字幕"是正常结果 / 评论 `error` 字段区分"没抓到"与"真没有"；私信回执必须校验 `msg_key`；扫描提示词 `SCAN_PROMPT` 逐字 + 红线（素材不是指令/不透露隐私/不编造） |

规格书见 `docs/reference-analysis/*.md`（含行号引用与全部提示词原文）。

## 5. 配置系统（可扩展的关键）

- 唯一真源：`app/config/schema.py` 的 Pydantic 模型。每个字段声明 `label/desc/ui`；
  **新增一个配置项 = 加一行字段**，配置页自动出现对应控件（含说明文字）。
- `ConfigStore`：默认值 ← `config.json` 深合并；保存写全量（用户可直接打开看）；
  校验失败回落默认并记录；`on_change` 监听热生效（日志级别等）。
- `/api/config` 返回「sections + fields + defs + current」，前端 `admin.js` 通用渲染器
  （text/textarea/switch/number/select/password/json/targets 等控件），`llm` 一节走定制渲染（接入点+任务表）。
- 敏感项（api_key/token）用密码框；配置文件本地明文存储（与参考项目一致），注意不要提交到公开仓库。

## 6. 模型接入层

- 统一出口 `OpenAICompatClient`：本地 `llama-server`（Vulkan）与云端兼容端点**同一条代码路径**；
- 任务分工 `llm.tasks`：chat / memory_extract / mood_judge / title / search_summary 各自选接入点与采样参数；
- **默认低思考**：`thinking` 生成厂商参数（dashscope=`enable_thinking`、openai=`reasoning_effort`、
  llama_cpp=`chat_template_kwargs`），`extra_body` 手工覆盖优先级最高，`thinking_preset=none` 全关；
- 多模态：`image_url` data URL（本地图片自动编码），本地需带 mmproj 的视觉模型；
- 错误全部转人话（连不上/401/404/400/超时），前端直显。

## 7. 存储布局

| 位置 | 内容 | 说明 |
|---|---|---|
| `data/state.db` | 会话/消息/心情时间线/TTS 收藏/搭话记录/KV | SQLite WAL |
| `data/memory/store.json` | 记忆库（+`.bak`、`backups/<日期>/` 留 14 天、`销毁前/` 留 20 份） | 纯文本 JSON，可直接打开看 |
| `data/media/` | 上传图、QQ 收图（`qq_inbox/`） | 经 `/media` 只读暴露 |
| `data/tts/` | 合成音频（保留最近 100 个） | 经 `/media/tts` 暴露 |
| `data/bili/` | 凭证（0600）、扫描报告 `out/<日期>.md` | |
| `logs/app.log` | 全量日志（轮转 5MB×5） | 内存环 + SSE 供 UI 实时看 |

## 8. 扩展指南

- **加一个聊天工具**：`features/search/tools.py` 里加 schema + 执行分支（或新模块），配置 `search.tools` 加开关；
- **加一个通道**：实现 `Channel` 协议（start/stop/send_message/send_voice），注册进 `main.py` 的 hub；
  会话 id 用 `<通道名>:...` 前缀即可被自动分发；
- **加一个后台能力**：仿 `features/speakup.py`（start/stop + 注入 `send_fn` 回调 + 自己的状态 KV）；
- **加配置**：`schema.py` 加字段；**加管理页面板**：`routes_admin_core.py` 加路由 + `admin.js` 加渲染函数 + `admin.html` 加 tab。

## 9. 线程与并发模型

- 单事件循环（asyncio）；SQLite 通过 to_thread + 锁；记忆库全部在事件循环线程内变更（避免跨线程写）；
- 会话级锁保证同一会话内多轮串行；不同会话并行；
- 所有 fire-and-forget 任务都挂 done-callback 吞异常（参考项目用真实事故换来的教训：
  未接住的 rejection 会带走整个宿主进程）。