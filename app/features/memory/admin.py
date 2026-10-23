"""鱼的记忆 · 批量整理（移植自 dsh-fish-memory：旧档案导入 / 提炼 / 清洗补键 / 补标不淡 / 查重合并）。

- 导入：只扫候选→用户挑→再落库；**已遗忘的内容不能因为再导一次就复活**；
- 提炼：一批批循环调模型，改写 + 出检索词/日期/重要度/事实键；整批没回不标记，试满 3 次作罢；
- 洗键：只从面板点，绝不从对话触发；干跑模式一个字段都不写；只摘自动补的键；
- 补标不淡：判「一直成立的」还是「某天发生的」；记录变更前值以便撤掉。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.utils import clamp
from app.features.memory import prompts as P
from app.features.memory.core import (
    UNIVERSAL_KEY_TAILS, MemoryService, _b36, _iso, _norm_fact_key, _parse_ms,
    similarity, tokenize,
)
from app.logbus import get_logger

log = get_logger("app.memory.admin")

FILE_TYPES = re.compile(r"\.(md|markdown|mdown|mkd|txt|text|log|rst|org)$", re.I)
SKIP_DIRS = re.compile(r"node_modules|\.git|\.svn|\.dsh|备份|backup")
SKIP_SECTIONS = re.compile(r"阅读须知|使用说明|怎么用|目录|索引|范例|模板|格式|更新记录|改动记录")
BULLET_STRIP = re.compile(r"^\s*(?:[-*+·○•※▪◦‣–—]|\d+[.、)]|\(\d+\)|\[\d+\]|（\d+）|【\d+】)\s*")
BOLD_ONLY = re.compile(r"^\*\*[^*]{2,24}\*\*[：:]?$")
INVISIBLE = re.compile(r"[\u200b-\u200f\u2028-\u202f\ufeff\u00ad]")
STABLE_SECTION = re.compile(r"我是谁|性格|人格|说话|语气|称呼|口头禅|关系|时间线|身份|约定|承诺|偏好|喜好")

DATE_PATS = [
    (re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?"), "ymd"),
    (re.compile(r"(\d{1,2})[-](\d{2})"), "md"),
    (re.compile(r"(\d{1,2})月(\d{1,2})[日号]"), "md"),
]
LONG_TERM_BEFORE = re.compile(r"(?:生日|出生|生于|诞生|纪念日)$")
LONG_TERM_AFTER = re.compile(r"^(?:生日|出生|满\d+岁)|^(?:vs|还是|不是)")
FUTURE_BEFORE = re.compile(r"(?:下次|下回|明天|后天|以后|将来|明年|下个月|下周|到时候|要去|打算)$")


def _clean_invisible(s: str) -> str:
    return INVISIBLE.sub("", s)


# ================================================================ 导入：扫描与候选拆分

def scan_archive(root: Path) -> dict:
    """扫档案：认 8 种纯文本、子目录往下两层；返回候选列表（不写库）。"""
    if not root.exists() or not root.is_dir():
        return {"ok": False, "message": f"文件夹不存在：{root}", "candidates": []}
    files: list[Path] = []

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for p in entries:
            name = p.name
            if p.is_dir():
                if level < 2 and not SKIP_DIRS.search(name):
                    walk(p, level + 1)
            elif p.is_file() and FILE_TYPES.search(name):
                files.append(p)

    walk(root, 0)
    candidates: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        for it in parse_archive(text, rel):
            candidates.append(it)
    if not files:
        return {"ok": True, "candidates": [], "files": 0,
                "message": "没扫到文件：只认 .md/.markdown/.txt/.text/.log/.rst/.org，"  "子文件夹会往下找两层"}
    if not candidates:
        return {"ok": True, "candidates": [], "files": len(files),
                "message": "有文件但一条候选都没拆出来：常见原因是整篇是一大段话（没有分行）、"
                           "或者每行太短（不到 6 个字）、或者内容都在表格/代码块/引用里"}
    return {"ok": True, "candidates": candidates, "files": len(files)}


def parse_archive(text: str, filename: str) -> list[dict]:
    """把一份档案拆成候选：跳过目录/引用/表格/加粗小标题；剥项目符号；≥6 字；超长按句拆。"""
    text = _clean_invisible(text)
    out: list[dict] = []
    seen: set[str] = set()
    section = ""
    in_code = False
    for ln, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        hm = re.match(r"^#{1,6}\s+(.*)$", line.strip())
        if hm:
            section = hm.group(1).strip()
            continue
        if not line.strip():
            continue
        if SKIP_SECTIONS.search(section):
            continue
        if line.lstrip().startswith(">"):
            continue
        if re.match(r"^\s*([-—=*_]{3,})\s*$", line):
            continue
        if "|" in line:
            continue
        if BOLD_ONLY.match(line.strip()):
            continue
        body = BULLET_STRIP.sub("", line).strip()
        body = body.strip("*_`~ ").strip()
        if len(body) < 6:
            continue
        for piece in split_long(body, 300):
            if len(piece) < 6:
                continue
            key = filename + "#" + str(ln) + "#" + piece[:20]
            if key in seen:
                continue
            seen.add(key)
            det = extract_date(piece)
            out.append({
                "file": filename, "line": ln, "section": section, "text": piece,
                "importance": 1.0 if STABLE_SECTION.search(section) else 0.5,
                "at": det["at"] if det else None,
                "atGuessed": bool(det and det.get("guessed")),
                "key": key,
            })
    return out


def split_long(text: str, limit: int) -> list[str]:
    """超长行按句末标点拆开（尽量切在句末；找不到就硬切）。"""
    if len(text) <= limit:
        return [text.strip()]
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for ch in "。！？；!?;，,、":
            idx = window.rfind(ch)
            if idx > cut:
                cut = idx
        if cut < int(limit * 0.4):
            sp = window.rfind(" ")
            cut = sp if sp > int(limit * 0.4) else limit - 1
        out.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        out.append(rest)
    return [s for s in out if s]


def extract_date(text: str) -> dict | None:
    """抠「这件事发生的日期」：猜不到就 None（绝不编）；生日/出生/纪念日/未来日期不算。"""
    hits: list[tuple[int, int, int, int, int, bool]] = []  # (pos, len, y, m, d, ymd?)
    for rx, kind in DATE_PATS:
        for m in rx.finditer(text):
            pos, ln = m.start(), len(m.group(0))
            if kind == "ymd":
                y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
                hits.append((pos, ln, y, mo, dd, True))
            else:
                mo, dd = int(m.group(1)), int(m.group(2))
                if not (1 <= mo <= 12 and 1 <= dd <= 31):
                    continue
                hits.append((pos, ln, 0, mo, dd, False))
    if not hits:
        return None
    # 去被包住的
    hits.sort(key=lambda x: x[0])
    filtered = []
    last_end = -1
    for h in hits:
        if h[0] >= last_end:
            filtered.append(h)
            last_end = h[0] + h[1]
    now = datetime.now()

    def is_long_term(pos: int, ln: int) -> bool:
        before = text[max(0, pos - 6):pos]
        after = text[pos + ln:pos + ln + 6]
        if LONG_TERM_BEFORE.search(before) or LONG_TERM_AFTER.search(after):
            return True
        if FUTURE_BEFORE.search(before):
            return True
        return False

    pick = None
    for h in filtered:
        if h[0] <= 5 and not is_long_term(h[0], h[1]):
            pick = h
            break
    if pick is None:
        for h in filtered:
            if not is_long_term(h[0], h[1]):
                pick = h
                break
    if pick is None:
        return None
    pos, ln, y, mo, dd, ymd = pick
    guessed = False
    if not ymd:
        guessed = True
        candidates_years = [now.year - 1, now.year, now.year + 1]
        best = None
        for cy in candidates_years:
            try:
                dt = datetime(cy, mo, dd)
            except ValueError:
                continue
            dist = abs((dt - now).total_seconds())
            if best is None or dist < best[1]:
                best = (cy, dist)
        if best is None:
            return None
        y = best[0]
    try:
        dt = datetime(y, mo, dd, 12, 0, 0)
    except ValueError:
        return None  # 2026-02-30 这类不许进位
    return {"at": dt.strftime("%Y-%m-%dT%H:%M:%S"), "guessed": guessed}


# ================================================================ 导入：落库 / 撤销 / 提炼

class MemoryAdmin:
    def __init__(self, svc: MemoryService) -> None:
        self.svc = svc

    # ---- 落库 ----
    async def import_apply(self, items: list[dict]) -> dict:
        data = self.svc.data()
        batch = "b" + _b36(int(time.time())) + str(int(time.time() * 1000) % 1000)
        now_iso = _iso(time.time() * 1000)
        existing = {_dk(str(m.get("text", ""))) for m in data["memories"]}
        deleted_texts = {_dk(str(m.get("text", ""))) for m in data.get("deleted", [])}
        added = dup = similar = completed = cameback = 0
        for it in items:
            text = self.svc.normalize_person(str(it.get("text", "")).strip())
            if not text:
                continue
            key = _dk(text)
            if key in existing:
                dup += 1
                continue
            if key in deleted_texts:
                cameback += 1  # 删过的不能因为再导一次就复活
                continue
            new_words = set(tokenize(text))
            hit = None
            for m in data["memories"]:
                if m.get("已被取代"):
                    continue
                if similarity(new_words, set(tokenize(str(m.get("text", ""))))) >= 0.9:
                    hit = m
                    break
            if hit is not None:
                if len(text) > len(str(hit.get("text", ""))):
                    hit["text"] = text  # 新的更长 → 把旧的补全
                    existing.add(key)
                    completed += 1
                else:
                    similar += 1
                continue
            imp = clamp(float(it.get("importance", 0.5)), 0, 1)
            new_m: dict[str, Any] = {
                "id": "i" + _b36(int(time.time() * 1000)) + str(len(data["memories"])),
                "text": text,
                "at": it.get("at") or None,
                "createdAt": now_iso,
                "category": "stable_profile" if imp >= 1 else "event",
                "importance": imp,
                "emotion": None,
                "tags": [],
                "source": f"档案导入：{it.get('file', '')} 第 {it.get('line', 0)} 行",
                "importBatch": batch,
                "importAt": now_iso,
            }
            if it.get("atGuessed"):
                new_m["atGuessed"] = True
            data["memories"].append(new_m)
            existing.add(key)
            added += 1
        self.svc.save()
        log.info("导入一批：加 %s / 重复 %s / 近似 %s / 补全 %s / 删过 %s", added, dup, similar, completed, cameback)
        return {"ok": True, "added": added, "dup": dup, "similar": similar,
                "completed": completed, "cameback": cameback, "batch": batch}

    async def import_undo(self, batch: str) -> dict:
        data = self.svc.data()
        moved = 0
        for m in list(data["memories"]):
            if m.get("importBatch") == batch:
                data["memories"].remove(m)
                m["deletedAt"] = _iso(time.time() * 1000)
                m["撤销原因"] = "导入撤销"
                data["deleted"].append(m)
                self.svc._invalidate_traces(m.get("id"))
                moved += 1
        self.svc.save()
        return {"ok": True, "moved": moved}

    # ---- 提炼 ----
    async def refine_batch(self, limit: int = 6) -> dict:
        data = self.svc.data()
        limit = max(1, min(20, limit))
        targets = [m for m in data["memories"]
                   if str(m.get("source", "")).startswith("档案导入")
                   and not m.get("refinedAt") and int(m.get("refineTries", 0) or 0) < 3]
        batch = targets[:limit]
        if not batch:
            return {"ok": True, "done": True, "message": "没有待提炼的条目"}
        inp = "\n".join(f"{i + 1}|{m.get('text')}" for i, m in enumerate(batch))
        text = await self.svc.router.quick("memory_extract", P.refine_system(self.svc.user_py()), inp,
                                           max_tokens=6000, temperature=0.1)
        parsed = _parse_refine(text)
        stat = {"changed": 0, "skipped": 0, "words": 0, "dates": 0, "imp": 0, "keys": 0, "unparsed": 0}
        now_iso = _iso(time.time() * 1000)
        for i, m in enumerate(batch, start=1):
            row = parsed.get(i)
            if row is None:
                m["refineTries"] = int(m.get("refineTries", 0) or 0) + 1
                if int(m["refineTries"]) >= 3:
                    m["refinedAt"] = now_iso
                    m["refinedSkip"] = True
                    m["refineFailed"] = True
                stat["unparsed"] += 1
                continue
            if row.get("skip"):
                m["refinedAt"] = now_iso
                m["refinedSkip"] = True
                stat["skipped"] += 1
                continue
            if row.get("text"):
                m["textBefore"] = m.get("text")
                m["text"] = self.svc.normalize_person(row["text"])
                stat["changed"] += 1
            words = row.get("words") or []
            if words:
                m["related"] = words[:8]
                m["relatedAt"] = now_iso
                stat["words"] += 1
            if row.get("date") and not m.get("at"):
                det = extract_date(str(row["date"])) or _date_from_iso(str(row["date"]))
                if det:
                    m["at"] = det["at"] if isinstance(det, dict) else det
                    stat["dates"] += 1
            if row.get("importance") in (1.0, 0.5):
                m["importance"] = row["importance"]
                m["category"] = "stable_profile" if row["importance"] >= 1 else "event"
                stat["imp"] += 1
            fk = _norm_fact_key(str(row.get("key", "") or ""))
            if fk:
                m["事实键"] = fk
                stat["keys"] += 1
            m["refinedAt"] = now_iso
            m.pop("refinedSkip", None)
            m.pop("refineTries", None)
        self.svc._converge_keys("")
        self.svc.save()
        stat["remaining"] = sum(1 for m in data["memories"]
                                if str(m.get("source", "")).startswith("档案导入")
                                and not m.get("refinedAt") and int(m.get("refineTries", 0) or 0) < 3)
        stat["done"] = stat["remaining"] == 0
        return {"ok": True, **stat}

    async def refine_undo(self, ids: list[str] | None = None) -> dict:
        data = self.svc.data()
        n = 0
        for m in data["memories"]:
            if ids and m.get("id") not in ids:
                continue
            if m.get("textBefore"):
                m["text"] = m["textBefore"]
                m.pop("textBefore", None)
                m.pop("refinedAt", None)
                m.pop("refinedSkip", None)
                m["refinedUndoAt"] = _iso(time.time() * 1000)
                n += 1
        self.svc.save()
        return {"ok": True, "reverted": n}

    async def refine_retry(self) -> dict:
        data = self.svc.data()
        n = 0
        for m in data["memories"]:
            if m.get("refinedSkip") and not m.get("textBefore"):
                m.pop("refinedAt", None)
                m.pop("refinedSkip", None)
                m.pop("refineFailed", None)
                m.pop("refineTries", None)
                n += 1
        self.svc.save()
        return {"ok": True, "retried": n}

    # ---- 清洗补键 ----
    async def wash_keys(self, limit: int = 8, dry_run: bool = False) -> dict:
        data = self.svc.data()
        limit = max(1, min(20, limit))
        targets = [m for m in data["memories"]
                   if not m.get("已被取代") and not m.get("refinedSkip")
                   and not m.get("洗键过") and not _norm_fact_key(str(m.get("事实键", "") or ""))
                   and str(m.get("text", "")).strip()]
        batch = targets[:limit]
        if not batch:
            return {"ok": True, "done": True, "message": "没有需要补键的条目"}
        existing: list[str] = []
        for m in data["memories"]:
            k = _norm_fact_key(str(m.get("事实键", "") or ""))
            if k and k not in existing:
                existing.append(k)
        existing = existing[:400]
        inp = "\n".join(f"{i + 1}|{m.get('text')}" for i, m in enumerate(batch))
        py = self.svc.user_py()
        text = await self.svc.router.quick("memory_extract", P.wash_keys_system(py, existing), inp,
                                           max_tokens=6000, temperature=0.1)
        answered = _parse_wash(text)
        if not answered:
            return {"ok": True, "answered": 0, "message": "模型整批没回，这次不标记，下次可以重跑"}
        now_iso = _iso(time.time() * 1000)
        results: list[dict] = []
        given = 0
        for i, m in enumerate(batch, start=1):
            if i not in answered:
                results.append({"id": m.get("id"), "text": str(m.get("text", ""))[:40], "key": None, "answered": False})
                continue
            k = answered[i]
            results.append({"id": m.get("id"), "text": str(m.get("text", ""))[:40], "key": k or None, "answered": True})
            if dry_run:
                continue
            m["洗键过"] = now_iso
            if k:
                m["事实键"] = k
                m["键来源"] = "清洗补键"
                given += 1
        if not dry_run:
            if given:
                self.svc.file.save_before_destroy("清洗补键之前")
                self.svc._converge_keys("")
            self.svc.save()
        return {"ok": True, "answered": len(answered), "given": given,
                "total": len(batch), "dry": dry_run, "results": results,
                "done": len(batch) < limit}

    async def wash_keys_undo(self) -> dict:
        data = self.svc.data()
        n = 0
        for m in data["memories"]:
            if m.get("键来源") == "清洗补键":
                m.pop("事实键", None)
                m.pop("键来源", None)
                n += 1
            m.pop("洗键过", None)
            sp = m.get("已被取代")
            if isinstance(sp, dict) and "同一个「" in str(sp.get("为什么", "")):
                m.pop("已被取代", None)
        self.svc.save()
        return {"ok": True, "reverted": n}

    # ---- 补标不淡 ----
    async def backfill_stable(self, limit: int = 20, redo: bool = False) -> dict:
        data = self.svc.data()
        limit = max(1, min(40, limit))
        if redo:
            for m in data["memories"]:
                m.pop("stableChecked", None)
        targets = [m for m in data["memories"]
                   if not m.get("已被取代") and not m.get("stableChecked") and not m.get("refinedSkip")]
        batch = targets[:limit]
        if not batch:
            return {"ok": True, "done": True, "message": "没有需要判的条目"}
        inp = "\n".join(f"{i + 1}|{m.get('text')}" for i, m in enumerate(batch))
        text = await self.svc.router.quick("memory_extract", P.BACKFILL_STABLE_SYSTEM, inp,
                                           max_tokens=2000, temperature=0.1)
        ans: dict[int, float] = {}
        for line in str(text or "").splitlines():
            m = re.match(r"^\s*(\d+)\s*[|｜]\s*(1|0\.5|0|1\.0)\s*$", line)
            if m:
                idx = int(m.group(1))
                if 1 <= idx <= len(batch):
                    ans[idx] = 1.0 if m.group(2) in ("1", "1.0") else 0.5
        if not ans:
            return {"ok": True, "answered": 0, "message": "模型整批没回，这次不标记，下次可以重跑"}
        self.svc.file.save_before_destroy("补标不淡之前")
        now_iso = _iso(time.time() * 1000)
        changed = 0
        for i, m in enumerate(batch, start=1):
            if i not in ans:
                continue
            v = ans[i]
            before = {"category": m.get("category"), "importance": m.get("importance")}
            if m.get("category") != ("stable_profile" if v == 1 else "event"):
                changed += 1
            m["stableBefore"] = before
            m["stableChecked"] = True
            if v == 1:
                m["category"] = "stable_profile"
                m["importance"] = 1
            else:
                m["category"] = "event"
                m["importance"] = 0.5
        self.svc.save()
        remaining = sum(1 for m in data["memories"]
                        if not m.get("已被取代") and not m.get("stableChecked") and not m.get("refinedSkip"))
        return {"ok": True, "answered": len(ans), "changed": changed,
                "remaining": remaining, "done": remaining == 0}

    async def backfill_stable_undo(self) -> dict:
        data = self.svc.data()
        n = 0
        for m in data["memories"]:
            before = m.pop("stableBefore", None)
            m.pop("stableChecked", None)
            if isinstance(before, dict):
                m["category"] = before.get("category", m.get("category"))
                m["importance"] = before.get("importance", m.get("importance"))
                n += 1
        self.svc.save()
        return {"ok": True, "reverted": n}

    # ---- 查重合并 ----
    def duplicates(self) -> dict:
        data = self.svc.data()
        actives = [m for m in data["memories"] if not m.get("已被取代") and not m.get("refinedSkip")]
        pairs = []
        for i in range(len(actives)):
            for j in range(i + 1, len(actives)):
                a, b = actives[i], actives[j]
                sim = similarity(set(tokenize(str(a.get("text", "")))), set(tokenize(str(b.get("text", "")))))
                if sim >= 0.75:
                    pairs.append({
                        "a": a.get("id"), "b": b.get("id"),
                        "sim": round(sim, 3),
                        "one_key": sim >= 0.85 and _len_ratio(a, b) >= 0.8,
                    })
        pairs.sort(key=lambda p: -p["sim"])
        return {"ok": True, "pairs": pairs[:200]}

    async def merge(self, keep_id: str, drop_id: str) -> dict:
        data = self.svc.data()
        keep = self.svc and _find(data["memories"], keep_id)
        drop = _find(data["memories"], drop_id)
        if not keep or not drop:
            return {"ok": False, "message": "有一边找不到了"}
        # 服务端独立复验：不像的一条都不收
        sim = similarity(set(tokenize(str(keep.get("text", "")))), set(tokenize(str(drop.get("text", "")))))
        if sim < 0.75:
            return {"ok": False, "message": f"复验不通过（像度 {sim:.2f}），没有合"}
        data["memories"].remove(drop)
        drop["deletedAt"] = _iso(time.time() * 1000)
        drop["合并掉的"] = keep_id
        data["deleted"].append(drop)
        self.svc._invalidate_traces(drop_id)
        self.svc.save()
        return {"ok": True, "message": "合了（被合的那条在回收站里，能找回）"}

    def reset_daily_hint(self) -> None:
        pass


# ================================================================ 解析器

def _parse_refine(text: str) -> dict[int, dict]:
    """解析提炼输出：每行「编号|记忆|检索词|日期|重要度|事实键」，容错从右往左切格。"""
    out: dict[int, dict] = {}
    for line in str(text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*[|｜]\s*(.*)$", line)
        if not m:
            continue
        idx = int(m.group(1))
        rest = m.group(2)
        cells = [c.strip() for c in re.split(r"[|｜]", rest)]
        # 「跳过」看第一格
        first = cells[0] if cells else ""
        if re.fullmatch(r"\*{0,2}跳过\*{0,2}", first or ""):
            out[idx] = {"skip": True}
            continue
        key = ""
        # ① 尾格像键就摘
        if len(cells) >= 2 and _looks_like_key(cells[-1]) and _has_wordish(cells[:-1]):
            key = cells.pop()
        importance = None
        date = ""
        words: list[str] = []
        wi = -1
        for i in range(len(cells) - 1, -1, -1):
            if _looks_like_words(cells[i]):
                wi = i
                break
        if wi >= 0:
            words = _split_words(cells[wi])
            for cell in cells[wi + 1:]:
                if not date and re.search(r"\d", cell):
                    date = cell
                im = re.match(r"^(?:重要度\s*[:：]?\s*)?(1|0\.5|0|1\.0|0\.50)$", cell)
                if im:
                    importance = 1.0 if im.group(1) in ("1", "1.0") else 0.5
        else:
            tail = list(cells)
            if tail:
                im = re.match(r"^(?:重要度\s*[:：]?\s*)?(1|0\.5|0|1\.0|0\.50)$", tail[-1])
                if im:
                    importance = 1.0 if im.group(1) in ("1", "1.0") else 0.5
                    tail.pop()
                if tail and (tail[-1] == "" or re.search(r"\d{1,2}[-/.月]\d{1,2}", tail[-1])):
                    date = tail.pop() if tail[-1] else ""
            cells = tail
            wi = len(cells)
        body = "|".join(cells[1:wi]) if wi > 1 else (cells[1] if len(cells) > 1 else "")
        body = body.strip().strip("*_` ").strip()
        if body:
            out[idx] = {"text": body, "words": words, "date": date if date else "",
                        "importance": importance, "key": key}
    return out


def _looks_like_key(cell: str) -> bool:
    s = cell.strip()
    if not s or len(s) > 24 or re.search(r"\s", s) or re.search(r"[0-9]", s):
        return False
    if not re.fullmatch(r"[^.|｜]{1,12}[.．][^.|｜]{1,12}", s):
        return False
    tail = re.sub(r"[．。]", ".", s).split(".")[-1]
    return tail not in UNIVERSAL_KEY_TAILS


def _has_wordish(cells: list[str]) -> bool:
    return any(_looks_like_words(c) for c in cells if c)


def _looks_like_words(cell: str) -> bool:
    s = cell.strip()
    if not s:
        return False
    parts = [w for w in re.split(r"[，,、。；;\s]+", s) if w]
    if not (2 <= len(parts) <= 12):
        return False
    if any(len(w) > 12 for w in parts):
        return False
    # 含空格或标点的才算「词串」（一整句话没有分隔不像词表）
    return bool(re.search(r"[，,、。；;\s]", s)) or len(parts) >= 2


def _split_words(cell: str) -> list[str]:
    parts = [w.strip(" -*·") for w in re.split(r"[，,、。；;\s]+", cell) if w.strip(" -*·")]
    return [w for w in parts if 1 <= len(w) <= 12][:8]


def _parse_wash(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in str(text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*[|｜]\s*(.*)$", line)
        if not m:
            continue
        idx = int(m.group(1))
        raw = m.group(2).strip()
        if raw in ("跳过", "无", "没有", "留空", "none", "null", "na", "n/a", "不详", "未知", ""):
            out[idx] = ""
            continue
        k = _norm_fact_key(raw)
        out[idx] = k  # 形状不对/带数字/万能键 → 归一函数返回空
    return out


def _dk(text: str) -> str:
    return re.sub(r"[\s，。、！？,.!?]", "", text)


def _find(mems: list[dict], mem_id: str) -> dict | None:
    for m in mems:
        if m.get("id") == mem_id:
            return m
    return None


def _len_ratio(a: dict, b: dict) -> float:
    la, lb = len(str(a.get("text", ""))), len(str(b.get("text", "")))
    if not la or not lb:
        return 0.0
    return min(la, lb) / max(la, lb)


def _date_from_iso(s: str) -> dict | None:
    try:
        dt = datetime.strptime(s.strip()[:10], "%Y-%m-%d")
        return {"at": dt.strftime("%Y-%m-%dT%H:%M:%S")}
    except ValueError:
        return None