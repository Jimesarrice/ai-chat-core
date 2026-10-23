"""配置模式（schema）。

每个字段的 description 会直接展示在配置页上（悬停/说明文字），
字段的 ui 元数据决定渲染控件（文本框 / 大文本框 / 开关 / 数字 / 下拉 / 密码框）。
新增能力时：加一个 Section 类 + 在 Config 里挂上即可，配置页自动出现。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


def _meta(label: str, desc: str = "", order: int = 99, icon: str = "") -> dict[str, Any]:
    return {"json_schema_extra": {"label": label, "desc": desc, "order": order, "icon": icon}}


def fld(default: Any, label: str, desc: str = "", ui: str = "text", **kw: Any) -> Any:
    """字段快捷构造：ui ∈ text | textarea | switch | number | select | password | json"""
    extra = {"label": label, "ui": ui, "desc": desc}
    if "choices" in kw:
        extra["choices"] = kw.pop("choices")
    return Field(default=default, description=desc, json_schema_extra=extra, **kw)


# ============================================================ 人设卡

class PersonaConfig(BaseModel):
    """人设卡：每个部分一个输入框，保存后注入聊天/心情/主动搭话的提示词。"""
    model_config = _meta("人设卡", "决定它是谁、怎么说话。每个框对应人设的一个部分，留空则该部分不注入。", order=1, icon="persona")

    name: str = fld("小鱼", "它叫什么", "AI 伙伴的名字，提示词与人设里统一使用", ui="text")
    user_title: str = fld("你", "TA 叫什么", "你对它而言的称呼（用于记忆人称、心情判断提示词）", ui="text")
    nicknames: str = fld("", "TA 怎么称呼它", "逗号分隔的昵称（如：宝宝,鱼鱼）。心情判断时这些词算称呼不算名词", ui="text")
    relation: str = fld("伴侣", "关系", "它和 TA 是什么关系（伴侣/朋友/搭档…），写进心情判断提示词", ui="text")
    identity: str = fld("", "身份与背景", "它是谁：物种/职业/来历/所处的世界。自由书写", ui="textarea")
    personality: str = fld("", "性格特质", "开朗、嘴硬心软、慢热……写得越具体越像", ui="textarea")
    speech_style: str = fld("", "说话风格", "语气、长度偏好、口头习惯、标点风格（如：句子短、少用感叹号）", ui="textarea")
    likes: str = fld("", "喜好", "喜欢的东西、话题、活动", ui="textarea")
    limits: str = fld("", "禁忌与雷区", "不能做的事、不愿聊的话题、底线", ui="textarea")
    catchphrases: str = fld("", "口头禅与小动作", "常挂嘴边的话、习惯用语", ui="textarea")
    extra: str = fld("", "其他设定", "补充世界观、关系、约定等任何内容", ui="textarea")


# ============================================================ 模型接入

class ProviderConfig(BaseModel):
    """一个模型接入点。本地 llama.cpp 与云端 API 配置结构一致（都走 OpenAI 兼容协议）。"""
    label: str = fld("", "名称", "显示在配置页上的名字", ui="text")
    kind: Literal["local", "cloud"] = fld("cloud", "类型", "local=本地 llama.cpp（Vulkan），cloud=云端 API", ui="select", choices=["local", "cloud"])
    base_url: str = fld("http://127.0.0.1:8080", "服务地址", "本地：llama-server 地址；云端：OpenAI 兼容端点，如 https://dashscope.aliyuncs.com/compatible-mode", ui="text")
    api_key: str = fld("", "API Key", "云端必填；本地 llama-server 若未启用 --api-key 则留空", ui="password")
    model: str = fld("", "默认模型", "本地：llama-server 的模型名（-m 加载的那个）；云端：如 qwen3-max / deepseek-chat", ui="text")
    multimodal: bool = fld(True, "支持多模态", "本地需带 mmproj 的视觉模型；云端需选支持图像的模型", ui="switch")
    thinking: Literal["default", "off", "low", "high"] = fld("low", "思考模式", "默认低思考：关闭/压低思维链以省 token 提速。default=不发这类参数", ui="select", choices=["default", "off", "low", "high"])
    thinking_preset: Literal["auto", "dashscope", "openai", "llama_cpp", "none"] = fld("auto", "思考参数风格", "auto 按厂商自动选择；none=完全不发", ui="select", choices=["auto", "dashscope", "openai", "llama_cpp", "none"])
    extra_body: dict = fld({}, "额外请求体", "透传给接口的额外 JSON（如 chat_template_kwargs），与思考模式合并，冲突时以这里为准", ui="json")
    timeout_read: int = fld(600, "读取超时（秒）", "长回复等待上限", ui="number")
    note: str = fld("", "备注", "写给自己看的说明", ui="text")


class TaskRoute(BaseModel):
    provider: str = fld("", "接入点", "选 local / cloud 或自定义接入点 id", ui="text")
    model: str = fld("", "模型覆盖", "留空 = 用接入点默认模型", ui="text")
    temperature: float = fld(0.8, "温度", "0~2，聊天 0.7~1.0，判断类 0~0.3", ui="number")
    max_tokens: int = fld(0, "最大输出", "0 = 不限制", ui="number")
    enabled: bool = fld(True, "启用", "关掉则该任务停用（如关掉自动起标题）", ui="switch")


class LLMConfig(BaseModel):
    """双路径模型接入：本地 llama.cpp（Vulkan 后端）+ 云端 OpenAI 兼容 API，按任务分工。"""
    model_config = _meta("模型接入", "本地与云端两条路径；每个任务（聊天/记忆/心情/联网…）可单独指定走哪条", order=2, icon="llm")

    providers: dict[str, ProviderConfig] = fld(
        {
            "local": ProviderConfig(
                label="本地 llama.cpp", kind="local",
                base_url="http://127.0.0.1:8080", model="", multimodal=True,
                thinking="off", thinking_preset="llama_cpp",
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            ),
            "cloud": ProviderConfig(
                label="云端 API", kind="cloud",
                base_url="https://dashscope.aliyuncs.com/compatible-mode", model="qwen3-max",
                multimodal=True, thinking="low", thinking_preset="auto",
            ),
        },
        "接入点", "可增删。key 是 id（如 local / cloud），任务分工里引用它", ui="providers",
    )
    tasks: dict[str, TaskRoute] = fld(
        {
            "chat": TaskRoute(provider="local", temperature=0.85),
            "memory_extract": TaskRoute(provider="cloud", temperature=0.3, max_tokens=800),
            "mood_judge": TaskRoute(provider="cloud", temperature=0.2, max_tokens=300),
            "title": TaskRoute(provider="cloud", temperature=0.3, max_tokens=32),
            "search_summary": TaskRoute(provider="cloud", temperature=0.4),
        },
        "任务分工", "chat=聊天主回复；memory_extract=记忆提炼；mood_judge=心情判断；title=会话起名；search_summary=联网归纳", ui="tasks",
    )
    # —— 本地 llama.cpp（引擎已内置在 engines/llama.cpp，模型从项目 Models/ 自动识别）——
    local_command: str = fld(
        "",
        "本地启动命令",
        "启动 llama-server 的命令；「本地 llama.cpp」卡片里可一键生成（选模型后自动带上 --mmproj）",
        ui="text",
    )
    local_auto_start: bool = fld(False, "开机自动拉起", "随本程序启动时自动执行上面的命令", ui="switch")


# ============================================================ 记忆

class MemoryConfig(BaseModel):
    """鱼的记忆（对齐 dsh-fish-memory v1.2.2 的 policy）。

    会淡忘、带时间、按证据召回；门槛只卡证据（C），鲜活度/重要度/情绪只参与排序。
    """
    model_config = _meta("记忆", "该记的记下来，想起来时自己会想起来，不重要的事慢慢淡掉", order=3, icon="memory")

    enabled: bool = fld(True, "总开关", "关掉 = 不记、不想、不注入", ui="switch")
    auto_extract: bool = fld(True, "自动记（remember 工具）", "每轮给模型一个「记下来」的工具；关掉后只能手动添加与导入", ui="switch")
    # —— 权重：四项合计 100%（保存时自动调平）——
    baseline_weight: float = fld(0.55, "基础权重", "只要被想起来就有的一份（四项合计 100%）", ui="number")
    activation_weight: float = fld(0.25, "鲜活度权重", "越近、越常被想起的事，加得越多", ui="number")
    importance_weight: float = fld(0.15, "重要度权重", "标成「非常重要」的事，加得越多", ui="number")
    emotion_weight: float = fld(0.05, "情绪权重", "带情绪的事，加得越多", ui="number")
    # —— 时间 ——
    base_half_life_days: float = fld(30.0, "半衰期（天）", "普通的事多久淡一半（15~60）；越大忘得越慢。淡了只是不容易被想起来，不会删", ui="number")
    emotion_half_life_days: float = fld(7.0, "情绪半衰期（天）", "情绪多久淡一半，比普通的事快（3.5~14）", ui="number")
    # —— 门槛与条数 ——
    threshold: float = fld(0.35, "证据门槛", "它只卡证据（C），不掺鲜活度——调它不会误伤该想起的（0.1~0.8）", ui="number")
    max_items: int = fld(6, "最多注入几条", "一次最多塞几条（1~12）。越大越占上下文", ui="number")
    max_chars: int = fld(800, "注入字数上限", "一次注入加起来最多多少字；超了的这次先不进（下次可能就进了）", ui="number")
    daily_quota: int = fld(8, "每天最多记几条", "模型自己一天最多记几条（1~30）；手动加/导入不受限", ui="number")
    rarity_k: float = fld(1.0, "稀有度饱和点", "命中词稀有度（0~1）加起来到多少算「一半相关」；越小越容易给（0.3~6）", ui="number")
    query_gate: float = fld(0.8, "问句信息量门槛", "这句话本身太没内容（像「在吗」）就不翻记忆；0 = 关掉这道门", ui="number")
    facts_max: int = fld(2, "长期事实最多占几条", "要记一辈子的事最多占几个位置，剩下留给事件；0 = 事实不参与召回", ui="number")
    unknown_activation: float = fld(0.5, "没写时间的算多鲜活", "没时间戳的记忆（含长期事实）鲜活度按几成算；只影响排序、不影响谁进得来", ui="number")
    person: Literal["ta", "他", "她"] = fld("ta", "记忆里的人称", "记忆里用户写成「她/他/ta」；改了可以一起改旧记忆", ui="select", choices=["ta", "他", "她"])
    auto_expand: bool = fld(True, "自动扩词", "记下一条后顺手为它补检索词（每次最多 2 条，失败只记日志）", ui="switch")


# ============================================================ 心情

class MoodConfig(BaseModel):
    """鱼的心情：会起伏、会自己回稳，嘴上有分寸。

    对齐 dsh-feiyu-mood v0.4.2：心情 0~100 基线 70、七根情绪、
    moodDelta ÷ 3 的步长、指数衰减（心情半衰 12h / 情绪半衰 90min 固定）。
    """
    model_config = _meta("心情", "每轮聊完判断一次心情走向；有衰减，像活人", order=4, icon="mood")

    enabled: bool = fld(True, "每轮判断心情", "关掉 = 不判断、不改数值、不花判断的钱", ui="switch")
    baseline: float = fld(70.0, "基线心情", "没情绪时回落到多少（30~100）", ui="number")
    floor: float = fld(0.0, "心情下限", "心情最低掉到多少；0 = 不设底线", ui="number")
    decay_half_hours: float = fld(12.0, "心情淡掉一半的时间（小时）", "受一次影响后，心里多久淡掉一半（1~168）；大 = 隔夜留余温", ui="number")
    tell_mode: Literal["off", "label", "event"] = fld(
        "event", "心情怎么告诉它",
        "off=不告诉它（面板照常看）；label=只说此刻心情；event=说这会儿的底子+是什么事（每轮都不同，最不像被安排）",
        ui="select", choices=["off", "label", "event"],
    )
    confidence_threshold: float = fld(60.0, "可信度门槛", "判断模型自评分（0-100）低于它只记后台、不进它的嘴", ui="number")
    sig_mood: float = fld(3.0, "显著变化：心情", "原始 moodDelta 够这个数才算显著（实际心情只吃 1/3）", ui="number")
    sig_dim: float = fld(5.0, "显著变化：情绪", "某根情绪涨跌够这个数也算显著", ui="number")
    inject_per_day: int = fld(6, "每天最多告诉它几次", "先到先得；0 = 一次都不说（只记账）", ui="number")


# ============================================================ 主动搭话

class SpeakUpConfig(BaseModel):
    """主动搭话：没人在跟它说话的时候，它也有可能先开口。"""
    model_config = _meta("主动搭话", "掷骰子决定要不要自己起话头；只有掷中之后那一步才花模型", order=5, icon="speakup")

    enabled: bool = fld(True, "总开关", "关掉就完全不动", ui="switch")
    dry_run: bool = fld(True, "试运行", "开着时掷中了也只记账、不真发", ui="switch")
    interval_minutes: int = fld(30, "掷骰子间隔（分钟）", "1~1440；下一轮起按新值走，不用重启", ui="number")
    cooldown_minutes: int = fld(20, "搭话冷却（分钟）", "它刚自己开过口之后，这段时间内不再开口；0 = 不冷却", ui="number")
    miss_bonus: float = fld(10.0, "没中加成（百分点）", "每没掷中一次，下一次命中线加这么多；0 = 关掉", ui="number")
    miss_bonus_cap: float = fld(40.0, "加成上限（百分点）", "加成最多加到这么多", ui="number")
    quiet_start: int = fld(1, "安静时段起（小时）", "含起不含止；起 === 止 = 不启用；跨午夜也行（23~7）", ui="number")
    quiet_end: int = fld(8, "安静时段止（小时）", "", ui="number")
    near_minutes: int = fld(20, "刚聊过：时间窗（分钟）", "距上次说话不到这么久 → 命中线乘「近因子」（更难开口）", ui="number")
    near_factor: float = fld(0.15, "近因子", "刚聊过时的乘数，越小越难开口", ui="number")
    far_minutes: int = fld(360, "冷太久：时间窗（分钟）", "超过这么久没说话 → 命中线乘「远因子」（更容易开口）；中间 ×1", ui="number")
    far_factor: float = fld(1.5, "远因子", "冷太久时的乘数，越大越容易开口", ui="number")
    peak_hitline: float = fld(20.0, "峰时命中线（%）", "峰时（贵）→ 少开口；掷到它以下才开口", ui="number")
    valley_hitline: float = fld(60.0, "谷时命中线（%）", "谷时（便宜）→ 多开口", ui="number")
    peak_windows: list = fld(
        [["09:00", "12:00"], ["14:00", "18:00"]],
        "峰时时段", "周一~周五内的峰时窗口（默认与 DeepSeek 定价一致：9-12、14-18）；其余含周末全为谷时", ui="json",
    )
    targets: list = fld([], "目标会话", "勾选要搭话的会话（web 会话 id / qq 私聊/群会话 id）", ui="targets")
    trigger_text: str = fld(
        "好久没人跟我说话了，我该主动去打个招呼",
        "触发文案", "用它自己的口吻写——这是它的动机，不是系统提示", ui="textarea",
    )
    role: Literal["user", "developer"] = fld("user", "角色", "默认 user；developer 属于系统级消息（本实现不提供）", ui="select", choices=["user", "developer"])


# ============================================================ QQ（OneBot）

class QQConfig(BaseModel):
    """通到 QQ：OneBot 11 桥接（正向 WS / 反向 WS / HTTP 轮询）。"""
    model_config = _meta("QQ 桥接", "和 NapCat / SnowLuma 等 OneBot 11 后端对接；也支持纯 HTTP 轮询收消息", order=6, icon="qq")

    enabled: bool = fld(False, "总开关", "关着 = 一句都不发；收消息也不会触发回复", ui="switch")
    dry_run: bool = fld(True, "试运行", "开着只记账不真发（收消息照常处理，方便看日志）", ui="switch")
    mode: Literal["forward_ws", "reverse_ws", "http"] = fld(
        "forward_ws", "连接方式",
        "forward_ws=我们连后端的 WS（推荐）；reverse_ws=后端连我们的 /onebot/ws；http=定时轮询消息历史（无需 WS）",
        ui="select", choices=["forward_ws", "reverse_ws", "http"],
    )
    ws_url: str = fld("ws://127.0.0.1:3001", "WS 地址", "forward_ws 模式：OneBot 后端的正向 WS 地址", ui="text")
    http_url: str = fld("http://127.0.0.1:3000", "HTTP 地址", "OneBot HTTP API 地址（发送与轮询都用它）", ui="text")
    token: str = fld("", "Access Token", "OneBot 配置的 token，没有就留空", ui="password")
    poll_interval: float = fld(4.0, "轮询间隔（秒）", "http 模式专用", ui="number")
    owner_qq: str = fld("", "主人的 QQ 号", "私聊里只有这个号说话会被接进来（留空 = 私聊全不接）", ui="text")
    private_on: bool = fld(True, "接私聊", "", ui="switch")
    group_on: bool = fld(False, "接群聊", "群里需要 @ 它（或按触发前缀）才会回", ui="switch")
    allow_groups: list = fld([], "允许的群号", "留空 = 不接任何群", ui="json")
    group_trigger: Literal["at", "prefix", "all"] = fld("at", "群触发方式", "at=被@才回；prefix=消息以触发前缀开头；all=每条都回（慎用）", ui="select", choices=["at", "prefix", "all"])
    prefix: str = fld("/", "触发前缀", "prefix 方式用的前缀", ui="text")
    recv_images: bool = fld(True, "收图（QQ→它）", "你在 QQ 里发的图也转给它看", ui="switch")
    send_images: bool = fld(True, "发图（它→QQ）", "它回复里的本地图片也发到 QQ", ui="switch")
    max_images: int = fld(4, "一条最多几张图", "1~10，多出来的不转", ui="number")
    max_image_mb: float = fld(5.0, "单张最大（MB）", "超过就跳过，两个方向共用", ui="number")
    image_root: str = fld("", "图片根目录", "它回复里 ![名字](相对路径) 按哪个目录找；留空 = 自动（项目目录 + data/media）", ui="text")
    reply_voice: bool = fld(False, "语音回复", "接 TTS：回复同时发一条语音（需要后端支持 record）", ui="switch")


# ============================================================ 语音合成

class TTSConfig(BaseModel):
    """Qwen3-TTS 语音合成：本地服务 / DashScope 云 API / OpenAI 兼容 TTS 三种后端。"""
    model_config = _meta("语音合成", "用 Qwen3-TTS 把回复念出来：网页端点播 / QQ 发语音条", order=7, icon="tts")

    enabled: bool = fld(False, "总开关", "默认关；打开后聊天回复生成完会按策略合成", ui="switch")
    backend: Literal["dashscope", "qwentts", "local_http", "openai_compat"] = fld(
        "dashscope", "后端",
        "qwentts=本地 qwentts.cpp 服务（GGUF，CPU/Vulkan，推荐）；dashscope=阿里云百炼；"
        "local_http=其它本地服务；openai_compat=OpenAI 风格 /v1/audio/speech",
        ui="select", choices=["dashscope", "qwentts", "local_http", "openai_compat"],
    )
    local_language: str = fld(
        "chinese", "本地语言（qwentts）",
        "qwentts 后端的语言标记（取自模型元数据：12 种，含北京话/四川话方言）",
        ui="select",
        choices=["chinese", "english", "japanese", "korean", "french", "german", "italian",
                 "portuguese", "spanish", "russian", "beijing_dialect", "sichuan_dialect"],
    )
    model: str = fld(
        "qwen3-tts-flash",
        "模型名",
        "dashscope 例：qwen3-tts-flash；换本地后端时用下面「本地部署」面板的下拉选本地模型",
        ui="text",
    )
    base_url: str = fld("", "后端地址", "local_http / openai_compat 时填写，如 http://127.0.0.1:7860", ui="text")
    api_key: str = fld("", "API Key", "dashscope 用 DASHSCOPE_API_KEY；留空则读环境变量", ui="password")
    voice: str = fld(
        "serena", "音色",
        "qwentts 本地可选：serena/vivian/uncle_fu/ryan/aiden/ono_anna/sohee/eric(四川话)/dylan(北京话)；"
        "dashscope 选项：Cherry/Serena/Ethan/Chelsie",
        ui="text",
    )
    speed: float = fld(1.0, "语速", "0.5~2.0", ui="number")
    auto_web: bool = fld(False, "网页自动播放", "每条回复自动合成并播放；关掉则每条消息手工点 ▶", ui="switch")
    max_chars: int = fld(300, "朗读字数上限", "超出截断（语音条不宜过长）", ui="number")
    # —— 本地部署（local_http 时用；面板里可一键启动/停止/检测）——
    local_command: str = fld(
        "",
        "本地启动命令（可选）",
        "在本机启动本地 TTS 服务的命令，填了就能在下面「本地部署」里一键启动/停止；留空 = 你自己在外面起好服务，只填地址",
        ui="text",
    )
    local_auto_start: bool = fld(False, "开机自动拉起", "随本程序启动时自动执行「本地启动命令」", ui="switch")
    local_device: str = fld(
        "gpu",
        "本地推理设备",
        "默认 GPU（Vulkan 加速）；可切 CPU。注意：本地部署大语言模型时可能会抢占显存",
        ui="select",
        choices=["gpu", "cpu", "auto"],
    )
    local_payload: dict = fld(
        {},
        "本地请求体附加",
        "合并进 POST /tts 的额外 JSON（服务对字段有特殊要求时用；常规服务留空即可）",
        ui="json",
    )


# ============================================================ 联网搜集

class SearchConfig(BaseModel):
    """联网搜集信息：网页搜索 / 抓取 / B站采集，作为工具交给模型按需调用。"""
    model_config = _meta("联网搜集", "模型可以自己决定「去查一下」；也支持你在聊天里直接说「搜搜看」", order=8, icon="search")

    enabled: bool = fld(True, "总开关", "", ui="switch")
    web_provider: Literal["searxng", "duckduckgo", "off"] = fld(
        "off", "网页搜索源", "searxng=自建或公共实例（推荐）；duckduckgo=免密钥但需外网；off=只用抓取与B站",
        ui="select", choices=["searxng", "duckduckgo", "off"],
    )
    searxng_url: str = fld("", "SearXNG 地址", "如 http://127.0.0.1:8888", ui="text")
    max_results: int = fld(5, "搜索条数", "每次搜几条", ui="number")
    fetch_enabled: bool = fld(True, "抓取网页正文", "打开链接读全文（粗暴提取主文本）", ui="switch")
    fetch_max_chars: int = fld(8000, "正文截断字数", "喂给模型的正文上限", ui="number")
    timeout: float = fld(20.0, "网络超时（秒）", "", ui="number")
    bili_enabled: bool = fld(True, "B站采集", "搜视频 / 读字幕 / 看评论（需要 pip 安装 bilibili-api-python）", ui="switch")
    bili_cookie: str = fld("", "B站 Cookie（可选）", "登录后 Cookie 字符串；不填则匿名访问（部分接口受限）", ui="password")
    bili_min_play: int = fld(0, "最低播放量", "搜索时过滤，0 = 不过滤", ui="number")
    bili_min_days: int = fld(0, "至少发布几天", "0 = 不过滤", ui="number")
    tools: dict = fld(
        {"web_search": True, "web_fetch": True, "bili_scan": True, "bili_search": True,
         "bili_subtitle": True, "bili_comments": True, "bili_video_info": True,
         "bili_reply": False, "bili_send_message": False},
        "工具开关", "按需裁剪交给模型的工具清单；bili_reply/bili_send_message 是「对外说话」，默认关", ui="json",
    )
    tool_max_rounds: int = fld(4, "工具回合上限", "一轮对话里模型最多连调几轮工具", ui="number")


# ============================================================ 系统

class SystemConfig(BaseModel):
    """系统：监听地址、日志、上下文窗口。"""
    model_config = _meta("系统", "Web 服务与运行时参数", order=9, icon="system")

    host: str = fld("127.0.0.1", "监听地址", "想给局域网其他设备访问就填 0.0.0.0", ui="text")
    port: int = fld(8760, "端口", "", ui="number")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = fld("INFO", "日志级别", "改完立刻生效", ui="select", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    history_turns: int = fld(24, "上下文条数", "带进模型的历史消息条数（不含系统提示）", ui="number")
    time_context: bool = fld(True, "注入当前时间", "让模型知道现在几点、星期几", ui="switch")
    title_auto: bool = fld(True, "自动起标题", "新会话首轮后自动起名", ui="switch")


# ============================================================ 总装

class Config(BaseModel):
    persona: PersonaConfig = PersonaConfig()
    llm: LLMConfig = LLMConfig()
    memory: MemoryConfig = MemoryConfig()
    mood: MoodConfig = MoodConfig()
    speakup: SpeakUpConfig = SpeakUpConfig()
    qq: QQConfig = QQConfig()
    tts: TTSConfig = TTSConfig()
    search: SearchConfig = SearchConfig()
    system: SystemConfig = SystemConfig()


SECTION_META: dict[str, dict[str, Any]] = {}
for _name in ("persona", "llm", "memory", "mood", "speakup", "qq", "tts", "search", "system"):
    _f = Config.model_fields[_name]
    SECTION_META[_name] = dict(_f.json_schema_extra or {})