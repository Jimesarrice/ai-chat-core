"""主动搭话（移植自 dsh-speak-up v0.2.1）。

定时器到点 → 本地掷骰（一个 token 都不花）
  ├─ 没掷中 → 只写一条记录
  └─ 掷中   → 往目标会话送一句话（source.kind = 'speak-up'），让聊天管道照常开一轮

保留的口径：
- 命中线 = min(100, max(0, 基础命中线 × 时机因子 + min(加成上限, 连没中 × 没中加成)))
  —— **先乘时机因子、再加攒下来的加成**，顺序别写反；
- 峰时（周一~五 9-12、14-18，含起不含止）默认 20%，其余（含周末全天）谷时 60%；
- 掷值 = random()*100 ∈ [0,100)，**掷值 < 线** 才算中（线=100 必中，线=0 必不中）；
- 安静时段：含起不含止、跨午夜、起===止 视为不启用；
- 冷却是「我们自己多久前开过口」；试运行默认开（掷中也只记账）；
- 一次只在一个会话开口。
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any, Callable, Awaitable

from app.config.store import ConfigStore
from app.core.utils import clamp, human_duration, is_quiet_now
from app.logbus import get_logger
from app.storage.db import Database

log = get_logger("app.speakup")

# 峰时窗口（分钟，含起不含止）；默认与 DeepSeek 定价一致
DEFAULT_PEAK_WINDOWS = [(9 * 60, 12 * 60), (14 * 60, 18 * 60)]


# ================================================================ 纯函数（可单测）

def is_peak(now: datetime, windows: list[list[str]] | None = None) -> bool:
    """先判周末（全天谷时），再按分钟数看是否落在峰时窗口。"""
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    wins = _parse_windows(windows) or DEFAULT_PEAK_WINDOWS
    return any(lo <= minute < hi for lo, hi in wins)


def _parse_windows(windows: list | None) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for w in windows or []:
        try:
            a, b = str(w[0]), str(w[1])
            lo = int(a.split(":")[0]) * 60 + int(a.split(":")[1])
            hi = int(b.split(":")[0]) * 60 + int(b.split(":")[1])
            out.append((lo, hi))
        except (IndexError, ValueError):
            continue
    return out


def base_hitline(cfg, now: datetime) -> tuple[bool, float, str]:
    """返回 (是否峰时, 基础命中线, 短语)。"""
    windows = getattr(cfg, "peak_windows", None)
    peak = is_peak(now, windows)
    if peak:
        return True, float(cfg.peak_hitline), f"峰时命中线 {cfg.peak_hitline}%"
    return False, float(cfg.valley_hitline), f"谷时命中线 {cfg.valley_hitline}%"


def calc_hitline(base: float, factor: float, miss_streak: int, miss_bonus: float, bonus_cap: float) -> tuple[float, float]:
    """返回 (加成, 线)。"""
    bonus = min(bonus_cap, miss_streak * miss_bonus)
    line = min(100.0, max(0.0, base * factor + bonus))
    return bonus, line


def hitline_equation(line: float, phrase: str, factor: float, distance: str, bonus: float) -> str:
    bonus_part = f" + 没中加成 {bonus:.1f}" if bonus > 0 else ""
    return f"{line:.1f} = {phrase} × 时机因子 {_fmt_factor(factor)}（{distance}）{bonus_part}"


def _fmt_factor(f: float) -> str:
    return f"{f:g}"


# ================================================================ 服务

class SpeakUpService:
    """掷骰 + 调度 + 记录。真正发送委托给注入的 send_fn（聊天管道）。"""

    def __init__(self, store: ConfigStore, db: Database) -> None:
        self.store = store
        self.db = db
        self.send_fn: Callable[[str, str], Awaitable[None]] | None = None   # (conv_id, text)
        self.busy_fn: Callable[[str], bool] | None = None                   # 会话是否正忙
        self.list_convs_fn: Callable[[], Awaitable[list[str]]] | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._kv_prefix = "speakup.state"

    # ---- 生命周期 ----
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        log.info("主动搭话：每 %s 分钟掷一次骰子｜总开关 %s｜%s",
                 self.store.get().speakup.interval_minutes,
                 "开" if self.store.get().speakup.enabled else "关",
                 "**试运行中（只记账、不真发）**" if self.store.get().speakup.dry_run else "试运行已关（掷中会真发）")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        # 启动时不立即掷：等第一个间隔到点
        while not self._stop.is_set():
            interval = max(1, int(self.store.get().speakup.interval_minutes))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval * 60)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.roll(source="定时", mode="auto")
            except Exception as e:
                await self._note(f"这一轮出错：{e}")
                log.exception("搭话轮出错")

    # ---- 状态 ----
    async def _state(self) -> dict[str, Any]:
        st = await self.db.kv_get(self._kv_prefix, None)
        if not isinstance(st, dict):
            st = {"last_spoke_at": 0.0, "miss_streak": 0}
        st.setdefault("last_spoke_at", 0.0)
        st.setdefault("miss_streak", 0)
        return st

    async def _save_state(self, st: dict[str, Any]) -> None:
        await self.db.kv_set(self._kv_prefix, st)

    async def _note(self, text: str) -> dict:
        await self.db.add_speakup_log("info", text)
        log.info("speak-up: %s", text)
        return {"time": datetime.now().strftime("%m-%d %H:%M:%S"), "text": text}

    async def recent_logs(self, limit: int = 60) -> list[dict]:
        return await self.db.speakup_logs(limit)

    # ---- 核心：掷一次 ----
    async def roll(self, *, source: str = "手动", mode: str = "drill") -> dict[str, Any]:
        """mode: auto（定时）/ drill（演练，绝不真发）/ send（真发）。"""
        cfg = self.store.get().speakup
        now = datetime.now()   # ⚠ 整轮只取一次时间
        auto = mode == "auto"

        if auto and not cfg.enabled:
            return await self._note("总开关是关着的，这轮不动")
        if auto and is_quiet_now(int(cfg.quiet_start), int(cfg.quiet_end), now):
            return await self._note(f"现在 {now.hour} 点在安静时段（{cfg.quiet_start}~{cfg.quiet_end}）里，这轮不掷")
        if not cfg.targets:
            return await self._note("还没勾「目标会话」，什么都不做（去设置里勾一个）")

        available, blocked = await self._pick_available(cfg.targets)
        if not available:
            return await self._note(f"勾的目标会话现在都不能说话（{'、'.join(blocked) or '（空）'}）—— 按设计不自动唤醒，这轮什么都不做")

        chosen = available[random.randrange(len(available))]
        pick_note = f"（勾了 {len(available)} 个可用，这次挑 {_short_id(chosen)}）" if len(available) > 1 else ""

        st = await self._state()
        if auto and float(cfg.cooldown_minutes) > 0 and st["last_spoke_at"]:
            elapsed = (now.timestamp() * 1000 - st["last_spoke_at"]) / 60000.0
            if elapsed < float(cfg.cooldown_minutes):
                return await self._note(f"自己刚开过口（{elapsed:.1f} 分钟前，冷却 {cfg.cooldown_minutes} 分钟），这轮不重复")

        if auto:
            # 时机因子（严格 < / >；拿不到分钟数 → 1）
            minutes = await self._minutes_since_user(chosen)
            factor = 1.0
            if minutes is not None:
                if minutes < float(cfg.near_minutes):
                    factor = float(cfg.near_factor)
                elif minutes > float(cfg.far_minutes):
                    factor = float(cfg.far_factor)
            distance = f"距上次说话 {minutes:.1f} 分钟" if minutes is not None else "距上次说话时间不明"

            peak, base, phrase = base_hitline(cfg, now)
            bonus, line = calc_hitline(base, factor, int(st["miss_streak"]), float(cfg.miss_bonus), float(cfg.miss_bonus_cap))
            eq = hitline_equation(line, phrase, factor, distance, bonus)
            roll_val = random.random() * 100

            if roll_val >= line:
                st["miss_streak"] = int(st["miss_streak"]) + 1
                await self._save_state(st)
                _, next_line = calc_hitline(base, factor, int(st["miss_streak"]), float(cfg.miss_bonus), float(cfg.miss_bonus_cap))
                if float(cfg.miss_bonus) == 0:
                    next_text = f"下次命中线还是 {next_line:.1f}（没中加成关着）"
                else:
                    next_text = f"下次命中线变 {next_line:.1f}（没中加成 +{cfg.miss_bonus:g}）"
                return await self._note(
                    f"没掷中 ✗ 掷出 {roll_val:.1f}（命中线 {line:.1f} —— 要掷到 {line:.1f} 以下才开口）｜{eq}｜{next_text}"
                )
            # 掷中：清零
            before = int(st["miss_streak"])
            st["miss_streak"] = 0
            await self._save_state(st)
            zero_text = (f"没中加成清零（之前连着 {before} 次没中）" if before > 0 else "没中加成本来就是 0（还没攒过）")
            prefix = f"掷中 ✓ 掷出 {roll_val:.1f}（命中线 {line:.1f} —— 掷到 {line:.1f} 以下就开口）｜{eq}｜{zero_text}｜{pick_note}"
        else:
            prefix = f"【{source}/{('演练' if mode == 'drill' else '真发')}】手动触发（不掷骰子、不动没中加成）｜{pick_note}"

        if cfg.role == "developer":
            return await self._note(f"{prefix}角色填的是 developer，本实现不提供系统级消息，这轮主动不发")

        if mode == "drill" or (auto and cfg.dry_run):
            return await self._note(f"{prefix}演练：本来要往「{chosen}」送一句「{cfg.trigger_text}」")

        # 真发
        if self.send_fn is None:
            return await self._note(f"{prefix}followup 失败：发送通道还没接上")
        try:
            await self.send_fn(chosen, cfg.trigger_text)
        except Exception as e:
            return await self._note(f"{prefix}followup 失败：{e}")
        st["last_spoke_at"] = now.timestamp() * 1000
        await self._save_state(st)
        rec = await self._note(f"{prefix}已送出一句，会照常开一轮：「{cfg.trigger_text}」")
        return rec

    # ---- 可用会话过滤 ----
    async def _pick_available(self, targets: list) -> tuple[list[str], list[str]]:
        available: list[str] = []
        blocked: list[str] = []
        known: list[str] = []
        if self.list_convs_fn:
            known = await self.list_convs_fn()
        for t in targets:
            cid = str(t)
            if known and cid not in known:
                blocked.append(f"{_short_id(cid)}(不在)")
                continue
            if self.busy_fn and self.busy_fn(cid):
                blocked.append(f"{_short_id(cid)}(忙)")
                continue
            available.append(cid)
        return available, blocked

    async def _minutes_since_user(self, conv_id: str) -> float | None:
        """距「她上次说话」的分钟数：只认真人来源（跳过 speak-up）。"""
        try:
            conv = await self.db.get_conversation(conv_id)
            if conv is None:
                return None
            rows = await self.db.list_messages(conv_id, limit=60)
            for m in reversed(rows):
                if m["role"] == "user" and m.get("source") not in ("speak-up",):
                    from app.core.utils import parse_dt
                    dt = parse_dt(str(m.get("created_at", "")))
                    if dt:
                        return (datetime.now() - dt).total_seconds() / 60.0
            return None
        except Exception:
            return None


def _short_id(cid: str) -> str:
    return cid[-8:]


def _near_factor() -> float:
    return 0.15


def _far_factor() -> float:
    return 1.5