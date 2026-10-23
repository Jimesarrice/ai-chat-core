"""杂项工具：宽松 JSON 解析、文本截断、时间格式化。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta


def parse_json_loose(text: str) -> dict | list | None:
    """从模型输出里抠出一段 JSON。容忍代码围栏、前后废话、尾逗号。"""
    if not text:
        return None
    s = text.strip()
    # 去代码围栏
    m = re.search(r"```(?:json)?\s*(.+?)```", s, flags=re.S)
    if m:
        s = m.group(1).strip()
    # 直接解析
    for candidate in (s, _first_balanced(s, "{", "}"), _first_balanced(s, "[", "]")):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)  # 尾逗号
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                continue
    return None


def _first_balanced(s: str, open_ch: str, close_ch: str) -> str | None:
    start = s.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def fmt_dt(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def human_time(dt: datetime | None = None) -> str:
    """给模型看的时间：2026年10月23日 星期五 14:30。"""
    dt = dt or datetime.now()
    week = "一二三四五六日"[dt.weekday()]
    return f"{dt.year}年{dt.month}月{dt.day}日 星期{week} {dt.hour:02d}:{dt.minute:02d}"


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def parse_dt(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def parse_hhmm(s: str) -> int | None:
    """'09:00' → 分钟数 540。"""
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s).strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h * 60 + mi
    return None


def is_quiet_now(quiet_start: int, quiet_end: int, now: datetime | None = None) -> bool:
    """含起不含止；起 === 止 = 不启用；跨午夜（23~7）支持。"""
    if quiet_start == quiet_end:
        return False
    h = (now or datetime.now()).hour
    if quiet_start < quiet_end:
        return quiet_start <= h < quiet_end
    return h >= quiet_start or h < quiet_end


def split_sentences(text: str) -> list[str]:
    """粗粒度分句（用于语音分段/展示）。"""
    parts = re.split(r"(?<=[。！？!?\n])", text)
    return [p.strip() for p in parts if p.strip()]


def safe_filename(name: str, fallback: str = "file") -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")
    return name[:60] or fallback