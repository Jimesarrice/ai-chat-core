"""鱼的记忆 · 核心（移植自 dsh-fish-memory v1.2.2 的宿主半边）。

三件事：存（会淡忘的长期记忆）/ 取（按 P = C×(基础+鲜活+重要+情绪) 选几条）/ 注（每条带时间）。

要害口径（别改）：
- 鲜活度 A = A0 × 2^(-天数/H)，H = 半衰期 × (1 + 2×重要度)；长期事实不衰减（按 unknownActivation 常数）；
- 证据 C = s/(s+rarityK)，s = Σ 命中词 × 库里稀有度 × 说话稀有度 × 价（扩词全价/干净碎片 0.8/正文 0.6）；
- **门槛只卡 C**（不掺鲜活度，不误伤该想起的）；排序才用 P；
- 条数分档：头名 C≥0.6→maxItems、≥0.5→4、≥0.4→3、否则 1（不硬凑条数）；
- 时间词只提排序（P×1.3），不提相关度；整句原话命中直接顶 C=0.85（问句 ≥6 字才算）。
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config.store import ConfigStore
from app.core.utils import clamp
from app.features.memory import prompts as P
from app.llm.router import LLMRouter
from app.logbus import get_logger

log = get_logger("app.memory")

DAY_MS = 86400000
TZ_OFFSET = -time.timezone * 1000 + (3600000 if time.localtime().tm_isdst > 0 else 0)

# 时间词（只提排序）与权重
TIME_CUES = [
    {"re": re.compile(r"今天|今日|今儿"), "from": 0, "to": 0},
    {"re": re.compile(r"昨天|昨日|昨晚"), "from": 1, "to": 1},
    {"re": re.compile(r"前天"), "from": 2, "to": 2},
    {"re": re.compile(r"这几天|最近|前几天|这一阵|前阵子"), "from": 0, "to": 7},
    {"re": re.compile(r"上周|上星期"), "from": 7, "to": 14},
]
TIME_WEIGHT = 1.3

# 说话尺/召回 的常数
DUPLICATE_RATE = 0.80        # 召回去重像度
REAL_DUP_RATE = 0.85         # remember 自动作废像度
IMPORT_DUP_RATE = 0.9        # 导入近似去重像度
CUE_GENERAL_THRESHOLD = 0.4  # 通用中文词频门

NOT_A_NAME = {"她", "他", "我", "你", "ta", "TA", "Ta", "tA", "用户", "助手", "AI", "ai", "人", "自己"}
PRONOUN_FORMS = ["ta", "Ta", "TA", "tA", "他", "她"]
NAME_IS_MENTIONED = re.compile(r"(?:叫|叫做|名叫|名字是|名字叫|称呼|自称|绰号|昵称|是|就是|才是|成为|变成)$")
NAME_IS_DEFINED = re.compile(r"^(?:是|就是|这个名字|的名字|这个称呼|即|＝|=)")
NAME_IN_QUOTES_BEFORE = re.compile(r"[「『\"“'‘]$")
NAME_IN_QUOTES_AFTER = re.compile(r"^[」』\"”'’]")

UNIVERSAL_KEY_TAILS = {"内容", "相关", "其他", "其它", "的事", "记录", "情况", "事项", "东西", "事情", "杂项", "各种", "综合"}

# 单字虚词（兜底丢弃用；内容单字如「胃/饿/疼」必须留）
_SINGLE_STOPS = set(
    "的了是在和与或但而就都也还又很太不要没被把给对从向于之吗呢吧啊呀哦嗯么着过们那这有其啥咧哎唉哦哟而啦"
    "我你他她它咱您呗嘛咯唔哈呵嘿唉"
)

_jieba_ready = False
_freq_table: dict[str, int] | None = None
_max_freq: int = 0


def _ensure_jieba() -> None:
    global _jieba_ready, _freq_table, _max_freq
    if _jieba_ready:
        return
    import jieba  # 延迟导入：第一次分词前会建前缀词典（几百毫秒）

    jieba.setLogLevel(40)
    jieba.initialize()
    _freq_table = jieba.dt.FREQ
    _max_freq = max(_freq_table.values()) if _freq_table else 1
    _jieba_ready = True


def normalize(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).lower()


def tokenize(s: str) -> list[str]:
    """分词：jieba 词表切（对应原插件的「词典.txt 最长匹配」），兜底丢纯虚字单字。"""
    _ensure_jieba()
    import jieba

    text = normalize(s)
    out: list[str] = []
    seen: set[str] = set()
    for tok in jieba.lcut(text):
        tok = tok.strip()
        if not tok:
            continue
        if re.fullmatch(r"[a-z0-9]{2,}", tok):
            keep = True
        elif re.fullmatch(r"[\u4e00-\u9fa5]+", tok):
            keep = len(tok) >= 2 or tok not in _SINGLE_STOPS
        else:
            keep = False
        if keep and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def general_rarity(k: str) -> float:
    """通用中文稀有度（0~1）：词频表值代入 1 - log10(f+1)/log10(max+1)；不在表里 = 1。"""
    _ensure_jieba()
    if not _freq_table or not _max_freq:
        return 1.0
    f = _freq_table.get(k)
    if not f:
        return 1.0
    denom = math.log10(_max_freq + 1)
    if denom <= 0:
        return 1.0
    return clamp(1.0 - math.log10(f + 1) / denom, 0.0, 1.0)


def is_cue_word(k: str) -> bool:
    return general_rarity(k) >= CUE_GENERAL_THRESHOLD


def similarity(a: set, b: set) -> float:
    """像度：交集 / 较小集合；小集合少于 3 个词不比（假重复）。"""
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    if len(small) < 3:
        return 0.0
    return sum(1 for x in small if x in big) / len(small)


def day_key(ms: float) -> str:
    t = datetime.fromtimestamp(ms / 1000)
    return t.strftime("%Y-%m-%d")


def rel_time(at_ms: float | None, now: float) -> str:
    """相对时间显示：今天 HH:MM / 昨天 / N 天前(MM-DD) / 去年 MM-DD …（按日历天）。"""
    if at_ms is None:
        return ""
    t = datetime.fromtimestamp(at_ms / 1000)
    n = datetime.fromtimestamp(now / 1000)
    di = (at_ms + TZ_OFFSET) // DAY_MS
    ni = (now + TZ_OFFSET) // DAY_MS
    days = int(ni - di)
    hm = t.strftime("%H:%M")
    md = t.strftime("%m-%d")
    if days <= 0:
        return f"今天 {hm}"
    if days == 1:
        return f"昨天 {hm}"
    if days == 2:
        return f"前天 {hm}"
    if days <= 14:
        return f"{days} 天前（{md}）"
    if days <= 60:
        return f"{md}（{days} 天前）"
    if t.year == n.year - 1:
        return f"去年 {md}"
    if t.year < n.year - 1:
        return f"{t.strftime('%Y-%m-%d')}（{n.year - t.year} 年前）"
    return f"{md}（约 {round(days / 30)} 个月前）"


# ================================================================ 库（JSON + 三层备份）

def blank_store(policy: dict) -> dict:
    return {"version": 2, "policy": dict(policy), "memories": [], "traces": [], "deleted": []}


def mem_sig(memories: list[dict]) -> str:
    h = 0
    for m in memories or []:
        s = f"{m.get('id')}|{m.get('text')}|{m.get('at')}|{m.get('importance')}|{m.get('category')}"
        for ch in s:
            h = (h * 31 + ord(ch)) & 0x7FFFFFFF
    return f"{len(memories or [])}:{h}"


class MemoryStoreFile:
    """单库文件（data/memory/store.json）+ .bak + 每天一份（14 天）+ 销毁前另存。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "store.json"
        self.backup_dir = root / "backups"
        self.pre_destroy_dir = root / "销毁前"
        self._sig: str | None = None
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, policy: dict) -> dict:
        raw = None
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                log.error("记忆库解析失败，改用空白库：%s", e)
        data = blank_store(policy)
        if isinstance(raw, dict):
            data["policy"] = _fit_policy(raw.get("policy"), policy)
            for k in ("memories", "traces", "deleted"):
                v = raw.get(k)
                data[k] = v if isinstance(v, list) else []
            if len(data["traces"]) > 100:
                data["traces"] = data["traces"][-100:]
        self._sig = mem_sig(data["memories"])
        return data

    def save(self, data: dict) -> None:
        sig = mem_sig(data["memories"])
        if self._sig is not None and sig != self._sig and self.path.exists():
            self._backup()
        self._sig = sig
        if len(data["traces"]) > 100:
            data["traces"] = data["traces"][-100:]
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.error("记忆库写盘失败：%s", e)

    def _backup(self) -> None:
        try:
            (self.root / "store.json.bak").write_bytes(self.path.read_bytes())
            day = datetime.now().strftime("%Y-%m-%d")
            daily = self.backup_dir / day / "store.json"
            if not daily.exists():
                daily.parent.mkdir(parents=True, exist_ok=True)
                daily.write_bytes(self.path.read_bytes())
                self._trim_daily()
        except OSError as e:
            log.warning("备份失败（不影响保存）：%s", e)

    def _trim_daily(self) -> None:
        days = sorted([d for d in self.backup_dir.iterdir() if d.is_dir()], reverse=True)
        for d in days[14:]:
            try:
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
            except OSError:
                pass

    def save_before_destroy(self, label: str) -> None:
        if not self.path.exists():
            return
        try:
            self.pre_destroy_dir.mkdir(parents=True, exist_ok=True)
            stamp = int(time.time() * 1000)
            name = f"{label}·{stamp}.json"
            i = 1
            while (self.pre_destroy_dir / name).exists():
                name = f"{label}·{stamp}-{i}.json"
                i += 1
            (self.pre_destroy_dir / name).write_bytes(self.path.read_bytes())
            files = sorted(self.pre_destroy_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[20:]:
                f.unlink()
        except OSError:
            pass


def _fit_policy(raw: Any, defaults: dict) -> dict:
    """把库里的 policy 收进合法范围；缺的键用默认。"""
    out = dict(defaults)
    if not isinstance(raw, dict):
        return out
    for k, dflt in defaults.items():
        v = raw.get(k, dflt)
        try:
            if isinstance(dflt, bool):
                out[k] = bool(v)
            elif isinstance(dflt, (int, float)):
                out[k] = float(v)
            else:
                out[k] = v
        except (TypeError, ValueError):
            out[k] = dflt
    return out


# ================================================================ 服务

class MemoryService:
    """常驻单例：由 main 启动，pipeline 调 recall_injection / remember_tool。"""

    def __init__(self, store: ConfigStore, router: LLMRouter, data_dir: Path) -> None:
        self.store = store
        self.router = router
        self.file = MemoryStoreFile(data_dir / "memory")
        self._data: dict | None = None
        self._lock = asyncio.Lock()
        self._ruler_cache: dict[str, tuple[float, dict]] = {}
        self._expand_tasks: set[asyncio.Task] = set()

    # ---- 基础 ----
    def _policy(self) -> dict:
        c = self.store.get().memory
        return {
            "baselineWeight": c.baseline_weight,
            "activationWeight": c.activation_weight,
            "importanceWeight": c.importance_weight,
            "emotionWeight": c.emotion_weight,
            "baseHalfLifeDays": c.base_half_life_days,
            "emotionHalfLifeDays": c.emotion_half_life_days,
            "threshold": c.threshold,
            "maxItems": int(c.max_items),
            "maxChars": int(c.max_chars),
            "dailyQuota": int(c.daily_quota),
            "rarityK": c.rarity_k,
            "queryGate": c.query_gate,
            "factsMax": int(c.facts_max),
            "unknownActivation": c.unknown_activation,
            "autoExpand": bool(c.auto_expand),
            "person": c.person,
        }

    def data(self) -> dict:
        if self._data is None:
            self._data = self.file.load(self._policy())
        return self._data

    def save(self) -> None:
        if self._data is not None:
            self._data["policy"] = self._policy()  # 参数始终跟随配置
            self.file.save(self._data)

    async def start(self) -> None:
        await asyncio.to_thread(_ensure_jieba)
        self.save()
        n = len(self.data()["memories"])
        log.info("记忆库就绪：%s 条（%s）", n, self.file.path)

    async def stop(self) -> None:
        for t in list(self._expand_tasks):
            t.cancel()
        self.save()

    # ---- 人称 ----
    def self_name(self) -> str:
        return (self.store.get().persona.name or "").strip()

    def user_py(self) -> str:
        return self.store.get().memory.person or "ta"

    def normalize_person(self, text: str) -> str:
        name = self.self_name()
        if not name or name in NOT_A_NAME:
            return text
        out = []
        i = 0
        while True:
            j = text.find(name, i)
            if j < 0:
                out.append(text[i:])
                break
            before = text[:j]
            after = text[j + len(name):]
            protected = (
                NAME_IS_MENTIONED.search(before[-8:]) is not None
                or NAME_IS_DEFINED.match(after) is not None
                or NAME_IN_QUOTES_BEFORE.search(before[-1:]) is not None
                or NAME_IN_QUOTES_AFTER.match(after[:1]) is not None
            )
            if protected:
                out.append(text[i:j + len(name)])
            else:
                out.append(text[i:j])
                out.append("我")
            i = j + len(name)
        return "".join(out)

    def change_person(self, new_py: str, apply_existing: bool = False) -> dict:
        """改人称：默认只报数；apply_existing=True 才连旧记忆一起改。"""
        data = self.data()
        changed = 0
        examples: list[str] = []
        for m in data["memories"] + data.get("deleted", []):
            t = str(m.get("text", ""))
            nt = _swap_pronoun(t, new_py)
            if nt != t:
                changed += 1
                if len(examples) < 3:
                    examples.append(f"{t[:20]} → {nt[:20]}")
                if apply_existing:
                    m["text"] = nt
        if apply_existing and changed:
            self.save()
        return {"changed": changed, "examples": examples}

    # ---- 淡忘 ----
    def freshness(self, m: dict, now: float) -> float:
        pol = self.data()["policy"]
        i = float(m.get("importance", 0) or 0)
        h_days = float(pol.get("baseHalfLifeDays", 30)) * (1 + 2 * i)
        const = clamp(float(pol.get("unknownActivation", 0.5)), 0, 1)
        if m.get("category") == "stable_profile":
            return const
        anchor = m.get("activationAnchor") or m.get("at") or m.get("createdAt") or m.get("importAt")
        ms = _parse_ms(anchor)
        if ms is None:
            return const
        days_a = max(0.0, (now - ms) / DAY_MS)
        a0 = float(m.get("activation0", 1) or 1)
        return clamp(a0, 0, 1) * math.pow(2, -days_a / h_days)

    # ---- 库内稀有度 ----
    def _df(self) -> tuple[Counter, float, int]:
        data = self.data()
        mems = [m for m in data["memories"] if not m.get("refinedSkip") and not m.get("已被取代")]
        df: Counter = Counter()
        for m in mems:
            for k in set(tokenize(self._face(m))):
                df[k] += 1
        n = len(mems)
        top = math.log((n + 1) / 2)
        return df, top, n

    @staticmethod
    def _face(m: dict) -> str:
        parts = [str(m.get("text", "")), " ".join(m.get("tags") or []), " ".join(m.get("related") or [])]
        return normalize(" ".join(parts))

    @staticmethod
    def _wordset(m: dict) -> set[str]:
        return set(tokenize(MemoryService._face(m)))

    @staticmethod
    def _rarity(df: Counter, top: float, n: int, k: str) -> float:
        if n <= 1:
            return 1.0 if df.get(k) else 0.0
        if not (top > 0):
            return 0.0
        val = math.log((n + 1) / (df.get(k, 0) + 1)) / top
        return clamp(val, 0.0, 1.0)

    # ---- 说话尺（她自己怎么说话：句级 df） ----
    async def _ruler(self, conv_id: str, db) -> dict:
        cached = self._ruler_cache.get(conv_id)
        now = time.time()
        if cached and now - cached[0] < 30:
            return cached[1]
        df: Counter = Counter()
        n = 0
        try:
            rows = await db.list_messages(conv_id, limit=300)
            for m in rows:
                if m["role"] != "user" or m.get("source") == "speak-up":
                    continue
                for sent in re.split(r"[。！？!?\n]", str(m.get("content", ""))):
                    sent = sent.strip()
                    if not sent or len(sent) > 1000:
                        continue
                    n += 1
                    for k in set(tokenize(sent)):
                        df[k] += 1
        except Exception:
            pass
        ruler = {"n": n, "df": df}
        self._ruler_cache[conv_id] = (now, ruler)
        return ruler

    def _speech_rarity(self, ruler: dict, k: str) -> float:
        r_n = ruler.get("n", 0)
        if not r_n or r_n < 20:
            return 1.0
        top = math.log(r_n + 1)
        if not (top > 0):
            return 1.0
        v = math.log((r_n + 1) / (ruler["df"].get(k, 0) + 1)) / top
        return clamp(v, 0.0, 1.0)

    def _query_info(self, ruler: dict, query: str, df: Counter) -> float:
        r_n = ruler.get("n", 0)
        if not r_n or r_n < 20:
            return float("inf")
        top = math.log(r_n + 1)
        v = 0.0
        for k in tokenize(query):
            if df.get(k):  # 只算「在记忆库里真能当线索用」的词
                v += math.log((r_n + 1) / (ruler["df"].get(k, 0) + 1))
        return v / top if top > 0 else float("inf")

    # ---- 召回 ----
    async def recall(self, query: str, conv_id: str, db) -> tuple[list[dict], list[dict], dict | None, float]:
        """返回 (selected 行, 打分行, gate, now_ms)。"""
        if not self.store.get().memory.enabled or not str(query).strip():
            return [], [], None, time.time() * 1000
        now = time.time() * 1000
        data = self.data()
        pol = data["policy"]
        mems = [m for m in data["memories"] if not m.get("refinedSkip") and not m.get("已被取代")]
        if not mems:
            return [], [], None, now

        df, top, n = self._df()
        ruler = await self._ruler(conv_id, db)

        # ① 问句信息量门（尺样本 <20 句时 v=inf → 不拦）
        gate_th = float(pol.get("queryGate", 0.8))
        gate = None
        if gate_th > 0:
            v = self._query_info(ruler, query, df)
            gate = {"info": round(v, 2) if v != float("inf") else None, "threshold": gate_th}
            if v < gate_th:
                return [], [], gate, now

        # ② 问句词三道筛选 + 两道安全垫
        q_all = tokenize(query)
        rare_gate = max(3.0, n * 0.01)
        q_rare = [k for k in q_all if 0 < df.get(k, 0) <= rare_gate]
        q_general = [k for k in q_rare if is_cue_word(k)]
        usable = len(q_general) >= 2 and any(self._rarity(df, top, n, k) > 0 for k in q_general)
        q_keys = q_general if usable else q_all
        if not q_keys:
            q_keys = q_all

        # ③ 逐条打分
        scored: list[dict] = []
        for m in mems:
            row = self._evaluate(m, q_keys, query, now, pol, df, top, n, ruler)
            scored.append({"m": m, "s": row, "omission": None})

        # ④ 门槛：只卡 C
        threshold = float(pol.get("threshold", 0.35))
        picked: list[dict] = []
        for row in scored:
            c = row["s"]["C"]
            if c <= 0:
                row["omission"] = "hard_gate"
            elif c < threshold:
                row["omission"] = "below_threshold"
            else:
                picked.append(row)
        picked.sort(key=lambda r: -r["s"]["P"])

        # ⑤ 条数分档（跟着最高的 C 走）
        tiers = [(0.6, int(pol.get("maxItems", 6))), (0.5, 4), (0.4, 3), (0.0, 1)]
        cap = 0
        if picked:
            for line, c in tiers:
                if picked[0]["s"]["C"] >= line:
                    cap = c
                    break

        # ⑥ 召回去重（0.80，位置在分档之前）
        deduped: list[dict] = []
        for row in picked:
            frag = set(tokenize(str(row["m"].get("text", ""))))
            dup_of = None
            for kept in deduped:
                if similarity(frag, set(tokenize(str(kept["m"].get("text", ""))))) >= DUPLICATE_RATE:
                    dup_of = kept
                    break
            if dup_of is not None:
                row["omission"] = "duplicate"
            else:
                deduped.append(row)

        # ⑦ 分档截断
        capped = deduped[:cap]
        for row in deduped[cap:]:
            row["omission"] = "weak"

        # ⑧ 两条队：长期事实 ≤ factsMax，总量 ≤ maxItems
        facts_cap = int(pol.get("factsMax", 2))
        max_items = int(pol.get("maxItems", 6))
        selected: list[dict] = []
        facts_used = 0
        for row in capped:
            if len(selected) >= max_items:
                break
            if row["m"].get("category") == "stable_profile":
                if facts_used >= facts_cap:
                    row["omission"] = "fact_cap"
                    continue
                facts_used += 1
            selected.append(row)

        # ⑨ 字数预算（第一条不管多长都放行）
        budget = int(pol.get("maxChars", 800))
        final: list[dict] = []
        used = 0
        for row in capped:
            if row not in selected:
                continue
            ln = len(str(row["m"].get("text", "")))
            if final and used + ln > budget:
                row["omission"] = "budget"
                selected.remove(row)
                continue
            used += ln
            final.append(row)

        for row in picked:
            if row["omission"] is None and row not in final:
                row["omission"] = "limit"

        scored.sort(key=lambda r: (-r["s"]["P"], -r["s"]["C"]))
        return final, scored, gate, now

    def _evaluate(self, m: dict, q_keys: list[str], query: str, now: float,
                  pol: dict, df: Counter, top: float, n: int, ruler: dict) -> dict:
        m_text = self._face(m)
        m_words = self._wordset(m)
        expand = self._expand_terms(m)
        beta = 0.6
        piece = 0.8
        s = 0.0
        for k in q_keys:
            if k not in m_words:
                continue
            if not expand:
                price = 1.0
            elif k in expand["whole"]:
                price = 1.0
            elif k in expand["pieces"]:
                price = piece
            else:
                price = beta
            s += self._rarity(df, top, n, k) * self._speech_rarity(ruler, k) * price
        k_sat = float(pol.get("rarityK", 1) or 1)
        c = s / (s + k_sat) if (s + k_sat) > 0 else 0.0
        if len(query.strip()) >= 6 and q_keys and normalize(query).strip() in m_text:
            c = max(c, 0.85)

        i = float(m.get("importance", 0) or 0)
        a = self.freshness(m, now)
        at_ms = _parse_ms(m.get("at"))
        days = max(0.0, (now - at_ms) / DAY_MS) if at_ms is not None else 0.0
        e_raw = m.get("emotion")
        e = 0.0 if e_raw is None else float(e_raw) * math.pow(2, -days / float(pol.get("emotionHalfLifeDays", 7)))
        p = c * (float(pol.get("baselineWeight", 0.55))
                 + float(pol.get("activationWeight", 0.25)) * a
                 + float(pol.get("importanceWeight", 0.15)) * i
                 + float(pol.get("emotionWeight", 0.05)) * e)
        time_hit = False
        cue = _time_cue(query)
        if cue and at_ms is not None:
            md = int((now - at_ms) // DAY_MS)
            if cue["from"] <= md <= cue["to"]:
                time_hit = True
                p *= TIME_WEIGHT
        return {"C": c, "A": a, "E": e, "I": i, "P": p, "timeHit": time_hit, "s": s}

    @staticmethod
    def _expand_terms(m: dict) -> dict | None:
        related = [str(x) for x in (m.get("related") or []) if str(x).strip()]
        if not related:
            return None
        whole = set(related)
        pieces: set[str] = set()
        for w in related:
            parts = tokenize(w)
            if len(parts) > 1 and all(len(p) >= 2 for p in parts):
                pieces.update(parts)
        return {"whole": whole, "pieces": pieces - whole}

    # ---- 注入 ----
    def render(self, selected: list[dict], now: float) -> str:
        if not selected:
            return ""
        lines = []
        for row in selected:
            m = row["m"]
            body = self.normalize_person(str(m.get("text", "")).strip())
            at_ms = _parse_ms(m.get("at"))
            if at_ms is not None:
                lines.append(f"- {rel_time(at_ms, now)}：{body}")
            elif m.get("category") == "stable_profile":
                lines.append(f"- 一直：{body}")
            else:
                lines.append(f"- {body}")
        return "\n".join(P.RENDER_HEADER + lines)

    async def recall_injection(self, query: str, conv_id: str, db) -> tuple[str, list[dict]]:
        """pipeline 用：召回 → trace → 注入文本。任何异常都吞掉（记忆坏了不能影响对话）。"""
        try:
            selected, scored, gate, now = await self.recall(query, conv_id, db)
            if not self.store.get().memory.enabled:
                return "", []
            if gate is not None and not scored and gate.get("info") is not None:
                log.debug("问句信息量 %.2f < %.2f，这轮不翻记忆", gate["info"], gate["threshold"])
            self._write_trace(query, selected, scored, gate, conv_id, now)
            text = self.render(selected, now)
            if text:
                log.info("注入 %s 条记忆（%s 字）", len(selected), len(text))
            return text, selected
        except Exception:
            log.exception("召回出错（本轮不注入）")
            return "", []

    def _write_trace(self, query: str, selected: list[dict], scored: list[dict],
                     gate: dict | None, conv_id: str, now: float) -> None:
        data = self.data()
        data["traces"].append({
            "at": now,
            "session": conv_id,
            "query": query[:200],
            "gate": gate["info"] if gate else None,
            "gateLimit": gate["threshold"] if gate else None,
            "picked": [r["m"].get("id") for r in selected],
            "rows": [
                {
                    "id": r["m"].get("id"),
                    "C": round(r["s"]["C"], 3),
                    "P": round(r["s"]["P"], 3),
                    "omission": r["omission"],
                    "timeHit": r["s"]["timeHit"],
                    "status": None if r["omission"] else "assembled",
                }
                for r in scored[:24]
            ],
        })
        if len(data["traces"]) > 100:
            data["traces"] = data["traces"][-100:]
        self.save()

    def mark_consumed(self, selected: list[dict], reply: str) -> None:
        """一轮结束后：回复里实词重合够多 → 标『像用上了』。"""
        try:
            data = self.data()
            picked_ids = {r["m"].get("id") for r in selected if r.get("m")}
            if not picked_ids:
                return
            reply_words = set(tokenize(reply)) if reply else set()
            for tr in reversed(data["traces"]):
                for row in tr.get("rows") or []:
                    if row.get("id") in picked_ids and row.get("status") == "assembled":
                        m = _find_mem(data["memories"], row["id"])
                        if not m:
                            continue
                        keys = tokenize(str(m.get("text", "")))
                        hit = sum(1 for w in keys if w in reply_words)
                        if hit >= 2 and keys and hit / len(keys) >= 1 / 3:
                            row["status"] = "consumed"
                break
            self.save()
        except Exception:
            pass

    def _invalidate_traces(self, mem_id: str) -> None:
        for tr in self.data()["traces"]:
            for row in tr.get("rows") or []:
                if row.get("id") == mem_id:
                    row["status"] = "invalidated"

    # ---- remember（模型工具） ----
    def remember_tool_spec(self) -> dict:
        py = self.user_py()
        return {
            "type": "function",
            "function": {
                "name": "remember",
                "description": P.REMEMBER_DESC,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": P.REMEMBER_TEXT_DESC.replace("{py}", py)},
                        "importance": {"type": "number", "description": P.REMEMBER_IMPORTANCE_DESC},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": P.REMEMBER_TAGS_DESC},
                        "取代": {"type": "string", "description": P.REMEMBER_SUPERSEDE_DESC},
                        "事实键": {"type": "string", "description": P.REMEMBER_FACTKEY_DESC.replace("{py}", py)},
                    },
                    "required": ["text"],
                },
            },
        }

    async def remember(self, args: dict) -> dict:
        """执行 remember 工具调用。返回 {ok, message}（渲染给模型看）。"""
        if not self.store.get().memory.enabled or not self.store.get().memory.auto_extract:
            return {"ok": False, "message": "记忆开关关着"}
        text = self.normalize_person(str(args.get("text", "")).strip())
        if not text:
            return {"ok": False, "message": "text 不能为空"}
        data = self.data()
        pol = data["policy"]
        now = time.time() * 1000

        today = day_key(now)
        today_count = sum(1 for m in data["memories"]
                          if m.get("createdAt") and day_key(_parse_ms(m["createdAt"]) or 0) == today)
        if today_count >= int(pol.get("dailyQuota", 8)):
            return {"ok": False, "message": f"今天记满了（{pol.get('dailyQuota')} 条），明天再说。"}

        importance = clamp(_num(args.get("importance"), 0.5), 0, 1)
        tags = [str(t) for t in (args.get("tags") or [])][:6]

        # 去重 + 强化（A' = A + 0.2(1-A)）
        nkey = _dedup_key(text)
        for m in data["memories"]:
            if m.get("已被取代"):
                continue
            other = _dedup_key(str(m.get("text", "")))
            if other == nkey or (len(other) > 8 and len(nkey) > 8 and (other in nkey or nkey in other)):
                i0 = float(m.get("importance", 0) or 0)
                h0 = float(pol.get("baseHalfLifeDays", 30)) * (1 + 2 * i0)
                anchor = _parse_ms(m.get("activationAnchor") or m.get("at"))
                old_a = 1.0 if m.get("category") == "stable_profile" else (
                    clamp(float(pol.get("unknownActivation", 0.5)), 0, 1) if anchor is None
                    else clamp(float(m.get("activation0", 1) or 1), 0, 1) * math.pow(2, -max(0.0, (now - anchor) / DAY_MS) / h0)
                )
                new_a = old_a + 0.2 * (1 - old_a)
                m["activation0"] = clamp(new_a, 0, 1)
                m["activationAnchor"] = _iso(now)
                m["reinforcedAt"] = _iso(now)
                m["lastReinforcedDay"] = day_key(now)
                m["reinforceCount"] = int(m.get("reinforceCount", 0)) + 1
                self.save()
                return {"ok": True, "message": f"这条记过了，已经把它记得更牢一点（第 {m['reinforceCount']} 次）"}

        # 新条
        mem_id = "m" + _b36(int(now)) + str(random.randint(0, 999))
        new_m: dict[str, Any] = {
            "id": mem_id,
            "text": text,
            "at": _iso(now),
            "createdAt": _iso(now),
            "category": "stable_profile" if importance >= 1 else "event",
            "importance": importance,
            "emotion": None,
            "tags": tags,
            "source": "她自己记的",
        }
        fk = _norm_fact_key(str(args.get("事实键", "") or ""))
        if fk:
            new_m["事实键"] = fk

        # 顺手作废旧条：来源一（本地自动，像度 >=0.85 且长度比 >=0.8）
        new_words = set(tokenize(text))
        for m in data["memories"]:
            if m.get("已被取代"):
                continue
            old_text = str(m.get("text", ""))
            if not old_text:
                continue
            sim = similarity(new_words, set(tokenize(old_text)))
            ratio = min(len(old_text), len(text)) / max(len(old_text), len(text))
            if sim >= REAL_DUP_RATE and ratio >= 0.8:
                _mark_superseded(m, mem_id, "跟新记的几乎一样")

        # 来源二（模型指认的「取代」）
        hint = str(args.get("取代", "") or "").strip()
        if hint:
            target = _find_by_hint(data["memories"], hint)
            if target is not None and not target.get("已被取代"):
                _mark_superseded(target, mem_id, "新记的这条说它取代了它")

        data["memories"].append(new_m)
        # 按键收敛（先 push 再收敛：一定留最新的它自己）
        converged = self._converge_keys(mem_id)
        self.save()
        log.info("记下一条：%s", text[:60])
        msg = f"记下了（{mem_id}）"
        if converged:
            msg += f"，同一件事的旧条目标了 {converged} 条「已被取代」"

        # 自动扩词（fire-and-forget，必须接住异常）
        if pol.get("autoExpand", True):
            task = asyncio.ensure_future(self._auto_expand())
            self._expand_tasks.add(task)
            task.add_done_callback(self._expand_tasks.discard)
        return {"ok": True, "message": msg}

    def _converge_keys(self, keep_id: str) -> int:
        """同一事实键只留最新：分不出新旧就不动。"""
        data = self.data()
        groups: dict[str, list[dict]] = {}
        for m in data["memories"]:
            if m.get("已被取代"):
                continue
            k = _norm_fact_key(str(m.get("事实键", "") or ""))
            if not k:
                continue
            groups.setdefault(k, []).append(m)
        marked = 0
        for k, items in groups.items():
            if len(items) < 2:
                continue
            varied = []
            for m in items:
                t = _parse_ms(m.get("at")) or _parse_ms(m.get("createdAt"))
                varied.append((t, m))
            stamps = [t for t, _ in varied]
            if any(t is None for t in stamps) or len(set(stamps)) != len(stamps):
                continue  # 分不出谁新谁旧 → 宁可不标
            varied.sort(key=lambda x: x[0], reverse=True)
            newest = varied[0][1]
            for t, m in varied[1:]:
                if m.get("id") != newest.get("id"):
                    _mark_superseded(m, newest.get("id"), f"同一个「{k}」只留最新那条")
                    marked += 1
        return marked

    async def _auto_expand(self) -> None:
        try:
            await self.expand_batch(limit=2, only_missing=True)
        except Exception as e:
            log.warning("自动扩词失败：%s", e)

    # ---- 扩词（v2 提示词） ----
    async def expand_batch(self, limit: int = 8, only_missing: bool = True, dry_run: bool = False) -> dict:
        data = self.data()
        if only_missing:
            targets = [m for m in data["memories"]
                       if not m.get("refinedSkip") and not m.get("已被取代") and not (m.get("related") or [])
                       and not (m.get("relatedAt") and not (m.get("related") or []))]
        else:
            targets = [m for m in data["memories"] if not m.get("已被取代")]
        targets = targets[:max(1, min(20, limit))]
        if not targets:
            return {"ok": True, "done": True, "message": "没有需要扩词的条目"}
        inp = "\n".join(f"{i + 1}|{m.get('text')}" for i, m in enumerate(targets))
        text = await self.router.quick("memory_extract", P.EXPAND_SYSTEM_V2, inp,
                                       max_tokens=6000, temperature=0.1)
        parsed = _parse_expand(text)
        got = 0
        for i, m in enumerate(targets, start=1):
            words = parsed.get(i)
            if words and not dry_run:
                m["related"] = words[:8]
                m["relatedAt"] = _iso(time.time() * 1000)
                got += 1
        if not dry_run:
            self.save()
        return {"ok": True, "got": got, "total": len(targets), "done": len(targets) < limit}

    # ---- 库管理（手动/编辑/删除） ----
    async def add_manual(self, text: str, importance: float = 0.5, tags: list[str] | None = None,
                         at: str | None = None, fact_key: str = "") -> dict:
        """手动加一条（开关关着也能加，不受配额）——唯一限制：一字不差重复。"""
        data = self.data()
        text = self.normalize_person(str(text).strip())
        if not text:
            return {"ok": False, "message": "内容不能为空"}
        nkey = _dedup_key(text)
        for m in data["memories"]:
            if not m.get("已被取代") and _dedup_key(str(m.get("text", ""))) == nkey:
                return {"ok": False, "message": "一字不差的重复，没有加（要更新就编辑原来那条）"}
        now = time.time() * 1000
        mem_id = "mm" + _b36(int(now)) + str(random.randint(0, 999))
        new_m: dict[str, Any] = {
            "id": mem_id,
            "text": text,
            "at": at or _iso(now),
            "createdAt": _iso(now),
            "category": "stable_profile" if importance >= 1 else "event",
            "importance": clamp(float(importance), 0, 1),
            "emotion": None,
            "tags": [str(t) for t in (tags or [])][:6],
            "source": "手动添加",
        }
        fk = _norm_fact_key(fact_key)
        if fk:
            new_m["事实键"] = fk
        data["memories"].append(new_m)
        self._converge_keys(mem_id)
        self.save()
        return {"ok": True, "message": "加好了", "id": mem_id}

    async def edit(self, mem_id: str, text: str | None = None, importance: float | None = None,
                   at: str | None = None, fact_key: str | None = None) -> dict:
        m = _find_mem(self.data()["memories"], mem_id)
        if not m:
            return {"ok": False, "message": "没找到这条记忆"}
        if text is not None and text.strip():
            m["text"] = self.normalize_person(text.strip())
        if importance is not None:
            m["importance"] = clamp(float(importance), 0, 1)
            m["category"] = "stable_profile" if m["importance"] >= 1 else "event"
        if at is not None:
            m["at"] = at or None
        if fact_key is not None:
            fk = _norm_fact_key(fact_key)
            if fk:
                m["事实键"] = fk
            else:
                m.pop("事实键", None)
        m["source"] = "她改过的"
        m["editedAt"] = _iso(time.time() * 1000)
        self._invalidate_traces(mem_id)
        self.save()
        return {"ok": True, "message": "改好了"}

    async def delete(self, mem_id: str) -> dict:
        data = self.data()
        m = _find_mem(data["memories"], mem_id)
        if not m:
            return {"ok": False, "message": "没找到这条记忆"}
        data["memories"].remove(m)
        m["deletedAt"] = _iso(time.time() * 1000)
        data["deleted"].append(m)
        self._invalidate_traces(mem_id)
        self.save()
        return {"ok": True, "message": "删了（进回收站，可以找回来）"}

    async def restore(self, mem_id: str) -> dict:
        data = self.data()
        m = _find_mem(data["deleted"], mem_id)
        if not m:
            return {"ok": False, "message": "回收站里没有这条"}
        data["deleted"].remove(m)
        m.pop("deletedAt", None)
        data["memories"].append(m)
        self.save()
        return {"ok": True, "message": "找回来了"}

    async def unsupersede(self, mem_id: str) -> dict:
        m = _find_mem(self.data()["memories"], mem_id)
        if not m:
            return {"ok": False, "message": "没找到这条记忆"}
        m.pop("已被取代", None)
        self.save()
        return {"ok": True, "message": "已恢复（不再标着「已被取代」）"}

    def list_memories(self, include_superseded: bool = True, include_deleted: bool = False) -> list[dict]:
        data = self.data()
        now = time.time() * 1000
        out = []
        for m in data["memories"]:
            if not include_superseded and m.get("已被取代"):
                continue
            a = self.freshness(m, now)
            fresh_kind = ("fact" if m.get("category") == "stable_profile"
                          else "dated" if m.get("at") else "recorded")
            out.append({
                "id": m.get("id"), "text": m.get("text"), "at": m.get("at"),
                "createdAt": m.get("createdAt"), "importance": m.get("importance", 0.5),
                "category": m.get("category"), "source": m.get("source"),
                "related": m.get("related") or [], "事实键": m.get("事实键", ""),
                "已被取代": m.get("已被取代"), "activation": round(a, 3),
                "freshKind": fresh_kind, "refinedSkip": bool(m.get("refinedSkip")),
                "reinforceCount": m.get("reinforceCount", 0),
                "deleted": False,
            })
        if include_deleted:
            for m in data.get("deleted", []):
                out.append({
                    "id": m.get("id"), "text": m.get("text"), "at": m.get("at"),
                    "createdAt": m.get("createdAt"), "importance": m.get("importance", 0.5),
                    "category": m.get("category"), "source": m.get("source"),
                    "related": m.get("related") or [], "事实键": m.get("事实键", ""),
                    "已被取代": m.get("已被取代"), "activation": 0,
                    "freshKind": "deleted", "deleted": True,
                })
        out.sort(key=lambda r: _parse_ms(r.get("at")) or _parse_ms(r.get("createdAt")) or 0, reverse=True)
        return out

    def traces(self, limit: int = 10) -> list[dict]:
        tr = list(reversed(self.data()["traces"]))[:limit]

        def label(t: dict) -> dict:
            return {
                "at": t.get("at"), "query": t.get("query"), "gate": t.get("gate"),
                "gateLimit": t.get("gateLimit"), "picked": t.get("picked"),
                "rows": t.get("rows") or [],
            }
        return [label(t) for t in tr]

    def stats(self) -> dict:
        data = self.data()
        mems = data["memories"]
        return {
            "total": len(mems),
            "active": sum(1 for m in mems if not m.get("已被取代")),
            "facts": sum(1 for m in mems if m.get("category") == "stable_profile" and not m.get("已被取代")),
            "superseded": sum(1 for m in mems if m.get("已被取代")),
            "deleted": len(data.get("deleted", [])),
            "file": str(self.file.path),
        }


# ================================================================ 工具函数

def _time_cue(query: str) -> dict | None:
    for c in TIME_CUES:
        if c["re"].search(query):
            return c
    return None


def _num(v: Any, dflt: float) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else dflt
    except (TypeError, ValueError):
        return dflt


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_ms(s: Any) -> float | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s)[:19], fmt).timestamp() * 1000
        except ValueError:
            continue
    return None


def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n <= 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def _dedup_key(text: str) -> str:
    return re.sub(r"[\s，。、！？,.!?]", "", text)


def _swap_pronoun(text: str, new_py: str) -> str:
    out = text
    for form in PRONOUN_FORMS:
        if form == new_py:
            continue
        i = 0
        while True:
            j = out.find(form, i)
            if j < 0:
                break
            prev = out[j - 1] if j > 0 else ""
            nxt = out[j + len(form)] if j + len(form) < len(out) else ""
            skip = False
            if form == "他" and prev == "其":
                skip = True
            if form.isascii() and form.isalpha():
                if (prev.isascii() and prev.isalpha()) or (nxt.isascii() and nxt.isalpha()):
                    skip = True
            if not skip:
                out = out[:j] + new_py + out[j + len(form):]
            i = j + len(new_py)
    return out


def _norm_fact_key(k: str) -> str:
    s = str(k or "").strip()
    if not s:
        return ""
    s = re.sub(r"[．。｡]", ".", s)
    s = re.sub(r"[：:]", ".", s)
    s = re.sub(r"[｜|]", ".", s)
    s = re.sub(r"\s+", "", s)
    s = s.lower()
    s = s.strip("「」『』\"'`*#-—")
    s = re.sub(r"^[0-9]+[.、)]*", "", s)
    parts = re.split(r"[/／>＞-]+", s)
    if len(parts) > 1:
        s = ".".join(parts[:-1]) + "." + parts[-1]
    s = s[:24]
    # 形状不许带数字、不许万能键
    if not re.fullmatch(r"[^.]{1,12}\.[^.]{1,12}", s):
        return ""
    if re.search(r"[0-9]", s):
        return ""
    tail = s.split(".")[-1]
    if tail in UNIVERSAL_KEY_TAILS:
        return ""
    return s


def _find_mem(mems: list[dict], mem_id: str) -> dict | None:
    for m in mems:
        if m.get("id") == mem_id:
            return m
    return None


def _find_by_hint(mems: list[dict], hint: str) -> dict | None:
    hint = hint.strip()
    if not hint:
        return None
    m = _find_mem(mems, hint)
    if m:
        return m
    nk = _dedup_key(hint)
    best, best_sim = None, 0.0
    for cand in mems:
        if cand.get("已被取代"):
            continue
        ct = str(cand.get("text", ""))
        ck = _dedup_key(ct)
        if ck == nk or (len(ck) > 6 and len(nk) > 6 and (ck in nk or nk in ck)):
            return cand
        sim = similarity(set(tokenize(ct)), set(tokenize(hint)))
        if sim > best_sim:
            best, best_sim = cand, sim
    return best if best_sim >= 0.5 else None


def _mark_superseded(m: dict, by_id: str | None, why: str) -> bool:
    if not m or m.get("已被取代"):
        return False
    m["已被取代"] = {"被谁": by_id, "为什么": why, "作废于": _iso(time.time() * 1000)}
    return True


def _parse_expand(text: str) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for line in str(text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*[|｜]\s*(.+)$", line)
        if not m:
            continue
        idx = int(m.group(1))
        words = [w.strip(" -*·") for w in re.split(r"[，,、。；;\s]+", m.group(2)) if w.strip(" -*·")]
        words = [w for w in words if 1 <= len(w) <= 12][:8]
        if words:
            out[idx] = words
    return out