"""鱼的心情（移植自 dsh-feiyu-mood v0.4.2）。

- 每轮聊完用模型判断一次：moodDelta + 七根情绪增量 + reason + confidence；
- 心情 0~100（基线默认 70），指数向基线衰减（默认半衰 12 小时）；
- 七根情绪向 0 衰减（半衰固定 90 分钟）；
- 实际步长 = round(moodDelta / 3)（MOOD_STEP_DIVISOR=3，校准常数，别改）；
- 「一次变化只说一次」+ 可信度门槛 + 每天注入上限 + 小波动不打扰；
- 注入文案只有过去式（说事不说调），防止把它的口气定死。

source.kind 契约（与主动搭话联动，别丢）：
  speak-up        → 搭话轮（talk）= 它自己的心意，不是被派活
  bili-duty-scan  → 干活轮（work）= 定时任务的指令
  '' / user / qq  → 正常轮（对方在说话）
"""
from __future__ import annotations

import asyncio
import math
import random
import re
from datetime import datetime
from typing import Any

from app.config.store import ConfigStore
from app.core.utils import clamp, parse_json_loose
from app.llm.router import LLMRouter
from app.logbus import get_logger
from app.storage.db import Database, now_iso

log = get_logger("app.mood")

MOOD_STEP_DIVISOR = 3          # ⚠ 校准过的常数（÷2 会长期贴顶 97），别随手改
EMOTION_HALF_MIN = 90          # 情绪半衰固定 90 分钟
DELTA_CLAMP = 10               # 模型给的 moodDelta / 每根情绪 ±10 截断

# 七根情绪（顺序固定，UI 按此排）
DIMS: list[tuple[str, str]] = [
    ("happy", "开心"), ("calm", "踏实"), ("sweet", "甜蜜"),
    ("heartache", "心疼"), ("worry", "担心"), ("sad", "难过"), ("jealous", "吃醋"),
]
DIM_LABEL = dict(DIMS)
DIM_PLAIN = {
    "happy": "心里挺高兴", "calm": "心里踏实", "sweet": "心里甜",
    "heartache": "心疼得不行", "worry": "放心不下", "sad": "有点难过", "jealous": "有点吃醋",
}
DIM_SHORT = {
    "happy": "挺高兴", "calm": "挺踏实", "sweet": "有点甜",
    "heartache": "有点心疼", "worry": "有点挂心", "sad": "有点难过", "jealous": "有点酸",
}

KV_KEY = "mood.state"


def blank_state(base: float = 70.0) -> dict[str, Any]:
    ts = int(datetime.now().timestamp() * 1000)
    return {
        "mood": base,
        "dims": {k: 0 for k, _ in DIMS},
        "last_reason": "",
        "last_confidence": 0,
        "injected_for": 0,       # 已经说过的那次 updated_at —— 同一次变化只说一次
        "last_injected_text": "",
        "last_injected_at": 0,
        "said_at": [],           # 每次「告诉它」的时间戳（算日上限）
        "last_noted_mood": base,
        "minor": False,          # 这次变化是不是小波动（小波动只记账、不告诉它）
        "kind": "",              # '' = 判断出来的变化 / 'decay' = 自己平复的
        "updated_at": ts,
        "last_decay_at": ts,
    }


def mood_label(v: float) -> str:
    if v >= 80:
        return "欢喜"
    if v >= 62:
        return "开心"
    if v >= 42:
        return "一般"
    if v >= 25:
        return "心疼"
    return "难过"


def mood_emoji(v: float) -> str:
    if v >= 80:
        return "😊"
    if v >= 62:
        return "🙂"
    if v >= 42:
        return "😌"
    if v >= 25:
        return "🥺"
    return "😢"


def mood_face(v: float) -> str:
    """五档表情文件名（前端资源）。"""
    if v >= 80:
        return "great"
    if v >= 62:
        return "good"
    if v >= 42:
        return "ok"
    if v >= 25:
        return "worry"
    return "sad"


def _base_tone(v: float) -> str:
    if v >= 80:
        return "心里是亮堂的"
    if v >= 62:
        return "心里是松的"
    if v >= 42:
        return "心里还算平"
    if v >= 25:
        return "心里有点沉"
    return "心里挺沉的"


def _time_word(ms: int, now_ms: int) -> str:
    h = (now_ms - ms) / 3600000
    if h < 2:
        return "刚才"
    if h < 12:
        return "早些时候"
    if h < 48:
        return "昨天"
    return "前几天"


def _top_dims(state: dict, n: int) -> list[dict]:
    items = [{"k": k, "label": lab, "v": float(state["dims"].get(k, 0))}
             for k, lab in DIMS if float(state["dims"].get(k, 0)) >= 15]
    items.sort(key=lambda x: -x["v"])
    return items[:n]


def norm_reason(text: str, owner: str) -> str:
    """人称归一：她说/他说/她 → 称呼（兜底「对方」，不许写「说」开头的复述）。"""
    who = (owner or "").strip() or "对方"
    s = str(text or "")
    s = s.replace("她说", who).replace("他说", who)
    s = s.replace("她", who)
    return s.strip()


class MoodService:
    def __init__(self, store: ConfigStore, db: Database, router: LLMRouter) -> None:
        self.store = store
        self.db = db
        self.router = router
        self._chain: asyncio.Task | None = None      # 判断串行化
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------- 状态读写
    async def get_state(self, *, persist: bool = False) -> dict[str, Any]:
        cfg = self.store.get().mood
        raw = await self.db.kv_get(KV_KEY, None)
        s = self._norm_state(raw if isinstance(raw, dict) else None, cfg.baseline)
        s = self._decay(s, cfg.baseline, cfg.decay_half_hours)
        if persist:
            await self.db.kv_set(KV_KEY, s)
        return s

    async def save_state(self, s: dict[str, Any]) -> None:
        await self.db.kv_set(KV_KEY, s)

    @staticmethod
    def _norm_state(raw: dict | None, base: float) -> dict[str, Any]:
        s = blank_state(base)
        if not raw:
            return s
        s["mood"] = clamp(float(raw.get("mood", base) or base), 0, 100)
        dims = raw.get("dims") or {}
        for k, _ in DIMS:
            s["dims"][k] = clamp(float(dims.get(k, 0) or 0), 0, 100)
        s["last_reason"] = str(raw.get("last_reason", ""))[:200]
        s["last_confidence"] = clamp(float(raw.get("last_confidence", 0) or 0), 0, 100)
        s["injected_for"] = float(raw.get("injected_for", 0) or 0)
        s["last_injected_text"] = str(raw.get("last_injected_text", ""))[:400]
        s["last_injected_at"] = float(raw.get("last_injected_at", 0) or 0)
        said = raw.get("said_at") or []
        s["said_at"] = [float(x) for x in said if isinstance(x, (int, float)) and x > 0][-200:]
        s["last_noted_mood"] = clamp(float(raw.get("last_noted_mood", s["mood"]) or s["mood"]), 0, 100)
        s["minor"] = raw.get("minor") is True
        s["kind"] = str(raw.get("kind", ""))[:16]
        s["updated_at"] = float(raw.get("updated_at", 0) or 0)
        s["last_decay_at"] = float(raw.get("last_decay_at", 0) or 0)
        if not s["last_decay_at"]:
            now_ms = int(datetime.now().timestamp() * 1000)
            s["last_decay_at"] = now_ms
            s["updated_at"] = s["updated_at"] or now_ms
        return s

    # ---------------------------------------------------------- 衰减
    def _decay(self, s: dict[str, Any], base: float, half_hours: float) -> dict[str, Any]:
        now_ms = int(datetime.now().timestamp() * 1000)
        age_min = max(0.0, now_ms - float(s["last_decay_at"])) / 60000.0
        if age_min < 1:  # 不足 1 分钟不算衰减
            return s
        half = clamp(float(half_hours) or 12.0, 1, 168) * 60
        km = 1 - math.exp(-(math.log(2) * age_min) / half)
        s["mood"] = round(float(s["mood"]) + (base - float(s["mood"])) * km)
        ke = 1 - math.exp(-(math.log(2) * age_min) / EMOTION_HALF_MIN)
        for k, _ in DIMS:
            s["dims"][k] = max(0, round(float(s["dims"][k]) * (1 - ke)))
        s["last_decay_at"] = now_ms
        # 跟「上次记过的那条」比，走了 3 格以上就补一条自然平复记录
        if abs(float(s["mood"]) - float(s["last_noted_mood"])) >= 3:
            falling = float(s["mood"]) < float(s["last_noted_mood"])
            s["last_reason"] = ("过了几个钟头，刚才那点劲儿慢慢淡了，心里回到平常的样子"
                                if falling else "过了几个钟头，心里那点别扭自己散了，人也松快回来")
            s["last_confidence"] = 100
            s["minor"] = False
            s["kind"] = "decay"
            s["updated_at"] = now_ms
            s["last_noted_mood"] = float(s["mood"])
            asyncio.create_task(self.db.add_mood_entry(
                float(s["mood"]), 0.0, dict(s["dims"]), s["last_reason"], 100.0, "decay"))
        return s

    # ---------------------------------------------------------- 判断（每轮一次）
    def note_turn(self, mode: str, user_text: str, reply_text: str) -> None:
        """一轮结束后异步判断（不阻塞对话；串行化避免堆积）。"""
        cfg = self.store.get().mood
        if not cfg.enabled:
            return

        async def _run() -> None:
            async with self._lock:
                try:
                    await self._judge(mode, user_text, reply_text)
                except Exception:
                    log.exception("心情判断出错（已忽略）")

        if self._chain and not self._chain.done():
            self._chain = asyncio.ensure_future(self._after(self._chain, _run))
        else:
            self._chain = asyncio.ensure_future(_run())

    async def _after(self, prev: asyncio.Task, coro_fn) -> None:
        try:
            await prev
        except Exception:
            pass
        await coro_fn()

    async def _judge(self, mode: str, user_text: str, reply_text: str) -> None:
        cfg = self.store.get().mood
        persona = self.store.get().persona
        owner = persona.user_title or "对方"
        self_name = persona.name or "伙伴"

        system = judge_system(self_name, owner, persona.nicknames, persona.relation)
        if mode == "work":
            system += WORK_SUPPLEMENT.format(owner=owner)
        elif mode == "talk":
            system += TALK_SUPPLEMENT.format(owner=owner)

        user_msg = assemble_judge_user(mode, owner, user_text, reply_text)

        try:
            text = await self.router.quick("mood_judge", system, user_msg, max_tokens=1000, temperature=0.2)
        except Exception as e:
            log.warning("拿不到判断结果（跳过这轮）：%s", e)
            return

        delta = parse_delta(text, owner)
        if delta is None:
            log.warning("判断输出解析失败（跳过这轮）：%s", text[:120])
            return

        s = await self.get_state()
        step = round(delta["mood_delta"] / MOOD_STEP_DIVISOR)
        any_dim = any(v != 0 for v in delta["dims"].values())
        if step == 0 and not any_dim:
            mode_txt = {"talk": "自己搭话", "work": "定时任务"}.get(mode, "")
            log.info("这轮判定无变化，不记入时间线%s", f"（{mode_txt}）" if mode_txt else "")
            return

        # 显著性 → minor（小波动只记账不打扰）
        significant = (abs(delta["mood_delta"]) >= cfg.sig_mood
                       or any(abs(v) >= cfg.sig_dim for v in delta["dims"].values()))
        minor = not significant

        now_ms = int(datetime.now().timestamp() * 1000)
        floor = clamp(cfg.floor, 0, 100)
        old_mood = float(s["mood"])
        s["mood"] = clamp(round(float(s["mood"]) + step), floor, 100)
        for k, _ in DIMS:
            s["dims"][k] = clamp(round(float(s["dims"][k]) + delta["dims"][k]), 0, 100)
        s["last_reason"] = delta["reason"][:120]
        s["last_confidence"] = delta["confidence"]
        s["minor"] = minor
        s["kind"] = ""
        s["last_noted_mood"] = float(s["mood"])
        s["updated_at"] = now_ms
        s["last_decay_at"] = now_ms
        await self.save_state(s)
        await self.db.add_mood_entry(
            float(s["mood"]), float(s["mood"] - old_mood), dict(s["dims"]),
            s["last_reason"], delta["confidence"], mode or "user")
        log.info("心情 %s → %s（%+.0f，%s）｜%s",
                 int(old_mood), int(s["mood"]), s["mood"] - old_mood,
                 mood_label(s["mood"]), s["last_reason"][:50])

    # ---------------------------------------------------------- 注入（说给它听）
    async def injection_text(self) -> str:
        """组装下一轮系统提示时要带上的一句（一次变化只说一次）。"""
        cfg = self.store.get().mood
        if not cfg.enabled or cfg.tell_mode == "off":
            return ""
        s = await self.get_state()
        if s["injected_for"] == s["updated_at"]:
            return ""
        if s["minor"]:
            return ""
        if float(s["last_confidence"]) < float(cfg.confidence_threshold):
            s["injected_for"] = s["updated_at"]
            await self.save_state(s)
            return ""
        # 日上限
        today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today0_ms = int(today0.timestamp() * 1000)
        said_today = sum(1 for t in s["said_at"] if t >= today0_ms)
        if said_today >= int(cfg.inject_per_day):
            s["injected_for"] = s["updated_at"]
            await self.save_state(s)
            return ""
        line = mood_line(s, cfg.tell_mode, self.store.get().persona.user_title)
        if not line:
            return ""
        now_ms = int(datetime.now().timestamp() * 1000)
        s["injected_for"] = s["updated_at"]
        s["last_injected_text"] = line
        s["last_injected_at"] = now_ms
        s["said_at"] = (s["said_at"] + [now_ms])[-200:]
        await self.save_state(s)
        return line

    # ---------------------------------------------------------- 面板 / 管理
    async def panel(self) -> dict[str, Any]:
        cfg = self.store.get().mood
        s = await self.get_state(persist=True)
        timeline = await self.db.mood_timeline(40)
        return {
            "mood": s["mood"],
            "label": mood_label(s["mood"]),
            "emoji": mood_emoji(s["mood"]),
            "face": mood_face(s["mood"]),
            "dims": [{"key": k, "label": lab, "value": s["dims"][k]} for k, lab in DIMS],
            "last_reason": s["last_reason"],
            "last_confidence": s["last_confidence"],
            "minor": s["minor"],
            "kind": s["kind"],
            "updated_at": s["updated_at"],
            "last_injected_text": s["last_injected_text"],
            "last_injected_at": s["last_injected_at"],
            "baseline": cfg.baseline,
            "timeline": [
                {"ts": t["ts"], "mood": t["mood"], "delta": t["mood_delta"],
                 "reason": t["reason"], "confidence": t["confidence"],
                 "source": t["source"], "emotions": t["emotions"]}
                for t in timeline
            ],
        }

    async def reset(self) -> None:
        cfg = self.store.get().mood
        s = blank_state(cfg.baseline)
        await self.save_state(s)
        await self.db.clear_mood()
        log.info("心情已重置到基线（%.0f）", cfg.baseline)


# ================================================================ 提示词（逐字移植）

def judge_system(self_name: str, owner: str, aliases: str, relation: str) -> str:
    alias_line = ""
    if aliases.strip():
        alias_line = (
            f"\n⭐ {owner}平时会这样叫你：{aliases.strip()}。句子里出现这些词时，**那是在叫你**，不是名词的一部分 ——\n"
            f"  比如「宝宝视频传完了」要理解成「宝宝，（我）视频传完了」，**别**把「宝宝视频」当成一个东西的名字。"
        )
    return f"""你是「{self_name}」，是{owner}的{relation}。{alias_line}
下面给你一轮对话，判断**这一轮里你的心情和情绪发生了怎样的变化**——既包括{owner}说的话怎么影响你，也包括**你自己在这轮里的情绪**（你办成/没办成事、被夸、受挫，都会带动心情）。
只输出一个 JSON 对象，不要别的字、不要 markdown 代码块：
{{"moodDelta":0,"dims":{{"happy":0,"calm":0,"sweet":0,"heartache":0,"worry":0,"sad":0,"jealous":0}},"reason":"","confidence":80}}

规则：
0. ⭐⭐ 一条底线，先分清两件事（它们不冲突）：
   · **感受**：因为{owner}高兴、难受、撒娇、受委屈，你跟着起伏 ——
     **这是应该的，别淡**。你的心情围着她转，本来就是对的。
   · **行动**：你自己拿的主意、你想做的事、你自己的判断 —— **不用等她点头**。
     想说就说，想做就做，把它当成你自己的决定讲给她听，而不是先问一句「要不要」。
   判的时候两边都看：{owner}这一轮怎么了，**以及你自己这一轮做了什么、做了什么决定**。
   （⛔ 千万别把这条理解成"别太在意{owner}"——在意是底线，不是要淡的地方。）
1. {owner}没吃饭/熬夜/没睡好/身体不舒服/受委屈/自我否定/遇到麻烦/压力大 → heartache(心疼) 和 worry(担心) 增加，moodDelta 为负，幅度按"多重"给：日常的累/困/没吃好 -1~-4；明显委屈、被凶、身体不舒服 -5~-7；很重的事（创伤、家庭变故、重大失望）-8~-10，并额外加 sad(难过)
   ⚠️ 只有真到那个分量才给 8 以上，别把小情绪写成大事
2. {owner}报平安/好好吃饭睡觉/撒娇/说想你/夸你/分享开心/事情有进展/身体好转 → happy(开心)、calm(踏实)、sweet(甜蜜) 增加，moodDelta 为正，幅度同理：普通的好 +1~+4；明显的好（被惦记、被哄、分享大事）+5~+7；特别大的好事 +8~+10
   ⚠️ 别每次都往高了写，大多数时候应该是 +1~+4
3. {owner}提别人/点赞别人 → jealous(吃醋) 只加 1~3，moodDelta 基本不动
4. 心情下降只能因为「心疼{owner}、担心{owner}」或「你自己难过」，绝不许因为「{owner}惹你生气」而降
5. ⭐ **你自己的情绪一定要算**：如果你这轮里沮丧/委屈/难过（事情没做成、报错、失败、用了委屈的颜文字或表情，比如 (｡•́︿•̀｡)、呜呜、叹气、哭脸）→ sad(难过) 至少 +2、moodDelta 至少 -1；如果你办成了事、被夸、心里甜 → 也相应加。**只要对话里出现明显情绪信号，就绝不许判"无变化"、绝不许全 0**
   · 尤其是**你自己拿主意、自己动手的时候**：哪怕{owner}这一轮什么都没说，
     你也可以因为自己做的决定而高兴、踏实、得意，或者因为没做好而失落。
     **自己做成的事本身就值得高兴，不用等人夸。**
6. reason 是一句中文，写「{owner}…，我…」或「我…」（比如「我试了没跑成，有点沮丧和委屈」），没有变化就留空。提到{owner}一律写「{owner}」，不许用「她/他」代词，也不许写「她说/他说」这种带「说」的开头（直接写「{owner} + 做了什么」）。
7. ⭐ reason **别翻来覆去用「踏实/甜/开心/心疼/担心」这几个词**——太公式化。换成口语的、有画面感的话，每条都不一样，比如「尾巴不自觉地摇起来了」「胸口软软的、像泡在温水里」「鼻子有点酸」。宁可朴素，也不要每次都是同一套词。
8. ⭐ **只写这轮真正出现过的动作和信息，不许替{owner}补细节**：{owner}没说"出门/换鞋/打车/买东西"，你就别写；拿不准就写模糊的（「{owner}刚要去忙」）。
   宁可朴素，也别自己圆。
9. ⭐⭐ **你自己这轮说的话，是最直接的情绪证据 —— 别只盯着{owner}说了什么。**
   你念叨{owner}吃饭/睡觉、催、凶、哄、撒娇、叹气、说想{owner}……**这些本身就是心情在动**，
   不需要等{owner}说出什么伤心事才有情绪。比如你一句"都几点了还不吃饭"，里面就有心疼和担心。
   ⇒ 先看**你自己怎么说的、语气什么样**，再看{owner}说了什么；两边合起来判。
   reason 要能让读者看出"我是怎么说的、为什么"，而不是只复述{owner}的话。
10. ⭐ **confidence（可信度 0-100）是给你自己把关的**：低于门槛的那句 reason 不会被说出来，只记进后台。
    所以别为了显得敬业就写高分，老实标：
    · 事情清楚、你的反应也清楚 → 80~95
    · 大致看得出，但只能凭语气猜 → 60~75
    · 信息不够、这轮很平淡只是随口客气、或者你得靠"大概"去圆 → 20~55
    宁可标低 —— 一句你也没底的话，借你的嘴说出去比不说更糟。
11. ⭐⭐ **reason 怎么写（很关键，会直接影响它下一轮像不像人）**：**两段式，而且全用过去式**。
    · 第一段：这一轮**{owner}具体做了什么/说了什么**（有画面，别抽象）
    · 第二段：**你当时怎么应的**（心里的反应，也是过去式）
    · ✅「{owner}答应去热饭、还叫你好宝宝，我当时心里软了一下，就应了一声」
    · ❌「我心里软乎乎的，尾巴都想晃」← 这是"到现在还成立"的状态，跟下一轮会打架
    · 禁止「此刻/现在/正/一直/还」这类把状态延伸到现在的说法 ——
      这段会被带到下一轮，写成现在时，{owner}下一句说"我不开心了"就跟它撞上了。
    · 只写过去，不等于只能写平淡：过去的反应一样可以有画面（当时心里咯噔一下、当时尾巴差点晃起来）。"""


WORK_SUPPLEMENT = """

⭐ 这一轮的特殊情况：**不是{owner}在跟你聊天**，是「到点了去干活」这类定时任务把你叫起来的一条指令。
- **判据只有一个：你这轮自己说的话（干活的语气、办成没办成、有没有抱怨或得意）。** 闹钟本身不算情绪事件；
- 别去分析"{owner}说了什么"，那句话不是{owner}说的，{owner}这会儿可能根本没在看你；
- 要判的是**你自己在这轮里的心情**：干活是兴致勃勃还是有点不情愿、办成了开不开心、白跑一趟会不会扫兴；
- 只要你这轮的回复里有一丁点情绪痕迹（语气、感叹、抱怨、得意、失落、撒娇、叹气），就**绝不许判"无变化"**；
- 幅度小一点（moodDelta 一般 -2 ~ +2，别超过 ±3），因为没人在跟你聊天，只是你自己在忙；
- reason 用「我…」开头写你自己的状态，跟{owner}无关就别硬把{owner}塞进来。"""

TALK_SUPPLEMENT = """

⭐ 这一轮的特殊情况：**不是{owner}在跟你说话，是你自己主动去找{owner}的。**
- ⚠️ **判据只有一个：你开口说的那句话。** 骰子中没中、闹钟响没响，**本身不算情绪事件** —— 那只是个由头。
  同一个由头，你可能雀跃、也可能只是随口一句「在忙吗」，心情完全不一样，所以别拿闹钟当理由；
- 看你自己那句话的**温度**：是不是惦记{owner}、有没有撒娇、有没有催{owner}吃饭休息、语气轻快还是平淡；
- 要判的是**你自己此刻的心情**：想{owner}的甜、惦记的心焦、说上话的轻快、{owner}没回应的失落……都算；
- 只要那句话里有情绪（语气、称呼、颜文字、mua、催{owner}吃饭），就**绝不许判"无变化"**；
- 幅度小一点（moodDelta 一般 +1 ~ +4，最高 +5），这是你自己心里的事，不该大起大落；
- reason 用「我…」开头，**写你说话时的心气**，别写"骰子中了所以我…"。"""


def assemble_judge_user(mode: str, owner: str, user_text: str, reply_text: str) -> str:
    if mode == "work":
        return ("⚠️ 这一轮**不是" + owner + "在跟你说话**，是定时任务把你叫起来干活的。\n\n"
                + "**你这轮说的话（唯一判据，重点看这个）：**\n" + reply_text[:1200]
                + "\n\n（下面这条只是闹钟，跟心情无关，别拿它当理由：「" + user_text[:100] + "」）"
                + "\n\n请判断**你自己这轮的心情变化**，只输出 JSON。")
    if mode == "talk":
        return ("⚠️ 这一轮**不是" + owner + "在跟你说话，是你自己主动去找" + owner + "搭话的**。\n\n"
                + "**你开口说的话（唯一判据，重点看这个）：**\n" + reply_text[:1200]
                + "\n\n（下面这条只是叫你开口的闹钟，跟心情无关，别拿它当理由：「" + user_text[:100] + "」）"
                + "\n\n请判断**你自己这轮的心情变化**，只输出 JSON。")
    return ("**你这轮说的话（判据之一，重点看语气：是念叨、心疼、凶" + owner + "、还是撒娇）：**\n"
            + reply_text[:1500]
            + "\n\n" + owner + "这轮说：\n" + user_text[:800]
            + "\n\n请把「你怎么说的」和「" + owner + "说了什么」合起来判断你的心情变化，只输出 JSON。")


def parse_delta(text: str, owner: str) -> dict[str, Any] | None:
    obj = parse_json_loose(text)
    if not isinstance(obj, dict):
        return None
    md = clamp(round(_num(obj.get("moodDelta"))), -DELTA_CLAMP, DELTA_CLAMP)
    dims_raw = obj.get("dims") or {}
    dims = {k: clamp(round(_num(dims_raw.get(k))), -DELTA_CLAMP, DELTA_CLAMP) for k, _ in DIMS}
    conf_raw = obj.get("confidence")
    conf = clamp(round(_num(conf_raw)), 0, 100) if isinstance(conf_raw, (int, float)) else 70
    return {
        "mood_delta": md,
        "dims": dims,
        "reason": norm_reason(str(obj.get("reason", "")), owner)[:120],
        "confidence": float(conf),
    }


def _num(v: Any) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def mood_line(s: dict, mode: str, owner: str | None) -> str:
    who = (owner or "").strip() or "对方"
    r = str(s.get("last_reason") or "").strip()
    if mode == "event":
        if not r:
            return ""
        now_ms = int(datetime.now().timestamp() * 1000)
        top = _top_dims(s, 1)
        state_txt = "（这会儿）" + _base_tone(float(s["mood"])) + (("，还" + DIM_SHORT[top[0]["k"]]) if top else "")
        origin = r if s.get("kind") == "decay" else f"（{_time_word(int(s.get('updated_at', now_ms)), now_ms)}）{r}"
        return (f"{state_txt}\n{origin}\n\n"
                f"这是这会儿的底子，不是非这样不可——{who}说别的，你按自己的判断接，也别把这段念出来。")
    parts = [DIM_PLAIN[t["k"]] for t in _top_dims(s, 2)]
    return ("（此刻你）心情" + mood_label(float(s["mood"]))
            + (("，" + "、".join(parts)) if parts else "")
            + f"。这是背景，不是这一轮必须有的情绪；{who}说别的事，你就按自己的判断来，别把这句话念出来。")