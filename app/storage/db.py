"""SQLite 存储层。

一张库管全部运行时数据：会话、消息、记忆、召回诊断、心情时间线、发声队列收藏、
主动搭话记录、键值状态。全部经 to_thread 调用，WAL 模式，单写锁。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.logbus import get_logger

log = get_logger("app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL DEFAULT 'web',
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  images TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT 'user',
  model TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);
-- 记忆不在这里：走 data/memory/store.json（JSON 库 + 三层备份，便于打开看与撤销）
CREATE TABLE IF NOT EXISTS mood_timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  mood REAL NOT NULL,
  mood_delta REAL NOT NULL DEFAULT 0,
  emotions TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'user'
);
CREATE TABLE IF NOT EXISTS tts_favorites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT '默认',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS speakup_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'info',
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ---- 生命周期 ----
    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        log.info("数据库就绪：%s", self.path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ---- 基础执行（同步，需在锁内） ----
    def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        assert self._conn is not None, "数据库未初始化"
        with self._lock:
            cur = self._conn.execute("BEGIN")
            try:
                result = fn(self._conn)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await asyncio.to_thread(self._run, lambda c: c.execute(sql, params))

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        def _q(c: sqlite3.Connection) -> list[dict]:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        return await asyncio.to_thread(self._run, _q)

    async def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def insert(self, sql: str, params: tuple = ()) -> int:
        def _i(c: sqlite3.Connection) -> int:
            cur = c.execute(sql, params)
            return int(cur.lastrowid or 0)
        return await asyncio.to_thread(self._run, _i)

    # ---- KV ----
    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.query_one("SELECT v FROM kv WHERE k=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except Exception:
            return default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---- 会话 ----
    async def ensure_conversation(self, conv_id: str, channel: str, title: str = "", meta: dict | None = None) -> None:
        ts = now_iso()
        await self.execute(
            "INSERT INTO conversations(id, channel, title, created_at, updated_at, meta) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (conv_id, channel, title, ts, ts, json.dumps(meta or {}, ensure_ascii=False)),
        )

    async def touch_conversation(self, conv_id: str) -> None:
        await self.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), conv_id))

    async def set_conversation_meta(self, conv_id: str, meta: dict) -> None:
        await self.execute(
            "UPDATE conversations SET meta=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), conv_id),
        )

    async def list_conversations(self, channel: str | None = None, limit: int = 100) -> list[dict]:
        if channel:
            rows = await self.query(
                "SELECT * FROM conversations WHERE channel=? ORDER BY updated_at DESC LIMIT ?",
                (channel, limit),
            )
        else:
            rows = await self.query(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        for r in rows:
            r["meta"] = _loads(r.get("meta"), {})
        return rows

    async def get_conversation(self, conv_id: str) -> dict | None:
        row = await self.query_one("SELECT * FROM conversations WHERE id=?", (conv_id,))
        if row:
            row["meta"] = _loads(row.get("meta"), {})
        return row

    async def delete_conversation(self, conv_id: str) -> None:
        def _del(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
            c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        await asyncio.to_thread(self._run, _del)

    # ---- 消息 ----
    async def add_message(
        self, conv_id: str, role: str, content: str,
        images: list[str] | None = None, source: str = "user",
        model: str = "", meta: dict | None = None,
    ) -> int:
        mid = await self.insert(
            "INSERT INTO messages(conv_id, role, content, images, source, model, created_at, meta) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (conv_id, role, content, json.dumps(images or [], ensure_ascii=False),
             source, model, now_iso(), json.dumps(meta or {}, ensure_ascii=False)),
        )
        await self.touch_conversation(conv_id)
        return mid

    async def list_messages(self, conv_id: str, limit: int = 200, after_id: int = 0) -> list[dict]:
        rows = await self.query(
            "SELECT * FROM messages WHERE conv_id=? AND id>? ORDER BY id DESC LIMIT ?",
            (conv_id, after_id, limit),
        )
        rows.reverse()
        for r in rows:
            r["images"] = _loads(r.get("images"), [])
            r["meta"] = _loads(r.get("meta"), {})
        return rows

    async def last_messages(self, conv_id: str, n: int) -> list[dict]:
        return await self.list_messages(conv_id, limit=n)

    async def latest_assistant_message(self, conv_id: str) -> dict | None:
        row = await self.query_one(
            "SELECT * FROM messages WHERE conv_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
            (conv_id,),
        )
        if row:
            row["images"] = _loads(row.get("images"), [])
        return row

    async def message_count(self, conv_id: str) -> int:
        row = await self.query_one("SELECT COUNT(*) AS n FROM messages WHERE conv_id=?", (conv_id,))
        return int(row["n"]) if row else 0

    # ---- 心情 ----
    async def add_mood_entry(self, mood: float, mood_delta: float, emotions: dict,
                             reason: str, confidence: float, source: str) -> None:
        await self.execute(
            "INSERT INTO mood_timeline(ts, mood, mood_delta, emotions, reason, confidence, source) "
            "VALUES(?,?,?,?,?,?,?)",
            (now_iso(), mood, mood_delta, json.dumps(emotions, ensure_ascii=False),
             reason, confidence, source),
        )

    async def mood_timeline(self, limit: int = 40) -> list[dict]:
        rows = await self.query("SELECT * FROM mood_timeline ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["emotions"] = _loads(r.get("emotions"), {})
        return rows

    async def clear_mood(self) -> None:
        await self.execute("DELETE FROM mood_timeline")
        await self.kv_set("mood.state", {})

    # ---- TTS 收藏 ----
    async def add_tts_favorite(self, text: str, category: str = "默认") -> int:
        return await self.insert(
            "INSERT INTO tts_favorites(text, category, created_at) VALUES(?,?,?)",
            (text, category, now_iso()),
        )

    async def list_tts_favorites(self, category: str | None = None) -> list[dict]:
        if category:
            return await self.query(
                "SELECT * FROM tts_favorites WHERE category=? ORDER BY id DESC", (category,)
            )
        return await self.query("SELECT * FROM tts_favorites ORDER BY id DESC")

    async def delete_tts_favorite(self, fid: int) -> None:
        await self.execute("DELETE FROM tts_favorites WHERE id=?", (fid,))

    async def tts_categories(self) -> list[str]:
        rows = await self.query("SELECT DISTINCT category FROM tts_favorites ORDER BY category")
        cats = [r["category"] for r in rows]
        return cats or ["默认"]

    async def move_tts_favorite(self, fid: int, category: str) -> None:
        await self.execute("UPDATE tts_favorites SET category=? WHERE id=?", (category, fid))

    # ---- 主动搭话记录 ----
    async def add_speakup_log(self, kind: str, text: str) -> None:
        await self.execute(
            "INSERT INTO speakup_log(ts, kind, text) VALUES(?,?,?)", (now_iso(), kind, text)
        )
        # 只留最近 300 条
        await self.execute(
            "DELETE FROM speakup_log WHERE id NOT IN (SELECT id FROM speakup_log ORDER BY id DESC LIMIT 300)"
        )

    async def speakup_logs(self, limit: int = 60) -> list[dict]:
        rows = await self.query("SELECT * FROM speakup_log ORDER BY id DESC LIMIT ?", (limit,))
        return rows


def _loads(s: Any, default: Any) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


# 全局单例（main 启动时 connect）
db: Database = Database(Path("data/state.db"))