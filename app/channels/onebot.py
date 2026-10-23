"""OneBot v11 通道（QQ 桥）。

三种连接方式：
- forward_ws：我们连后端（推荐，如 NapCat ws://127.0.0.1:3001，断线自动重连）；
- reverse_ws：后端连我们（FastAPI /onebot/ws 把事件喂给 feed_event()）；
- http：定时轮询消息历史（不依赖 WS，兼容纯 HTTP 后端）。

发送统一走 HTTP API（send_private_msg / send_group_msg / record）。
复刻自 dsh-qq-bridge 的要点：白名单、message_id 去重、图片三形态抠取、
inbox 落盘、文案带本地路径、markdown 抠图、450ms 节流、试运行安全门。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.channels.base import ChannelHub
from app.config.store import ConfigStore
from app.core.types import InboundMessage
from app.logbus import get_logger

log = get_logger("app.onebot")

SEND_SEG_DELAY = 0.45          # 每段之间隔一下，像真人一条条发
DEDUP_MAX = 300                # message_id 去重表上限
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)\s*\)")
CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]")
CQ_IMG_RE = re.compile(r"\[CQ:image,[^\]]*?file=([^,\]]+)[^\]]*\]")


def _now_ts() -> int:
    return int(datetime.now().timestamp())


class OneBotChannel:
    name = "qq"

    def __init__(self, store: ConfigStore, hub: ChannelHub, media_dir: Path) -> None:
        self.store = store
        self.hub = hub
        self.inbox = media_dir / "qq_inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.self_id: str = ""
        self.nickname: str = ""
        self.connected: bool = False
        self._dedup: OrderedDict[str, None] = OrderedDict()
        self._ws_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()   # 串行发送，保证顺序
        self._http: httpx.AsyncClient | None = None

    # ================================================== 生命周期

    async def start(self) -> None:
        cfg = self.store.get().qq
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        if not cfg.enabled:
            log.info("QQ 通道：总开关关着，不连接（配置页可打开）")
            return
        await self._refresh_login_info()
        if cfg.mode == "forward_ws":
            self._ws_task = asyncio.create_task(self._ws_loop())
        elif cfg.mode == "reverse_ws":
            log.info("QQ 通道：等待后端连接 /onebot/ws（反向 WS）")
        else:
            self._poll_task = asyncio.create_task(self._poll_loop())
        log.info("QQ 通道已启动：模式=%s｜后端 %s", cfg.mode, cfg.http_url)

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._ws_task, self._poll_task):
            if t:
                t.cancel()
        if self._http:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        cfg = self.store.get().qq
        h = {"Content-Type": "application/json"}
        if cfg.token:
            h["Authorization"] = f"Bearer {cfg.token}"
        return h

    async def _refresh_login_info(self) -> None:
        r = await self._call("get_login_info", {})
        if r.get("ok"):
            data = r.get("data") or {}
            self.self_id = str(data.get("user_id", ""))
            self.nickname = str(data.get("nickname", ""))
            log.info("QQ 已连接：%s（%s）", self.nickname, self.self_id)

    # ================================================== HTTP 调用

    async def _call(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        cfg = self.store.get().qq
        url = f"{cfg.http_url.rstrip('/')}/{action}"
        try:
            assert self._http is not None
            r = await self._http.post(url, json=body, headers=self._headers())
            j = r.json()
            return {"ok": bool(j and j.get("status") == "ok" and j.get("retcode") == 0),
                    "data": j.get("data"), "raw": j}
        except Exception as e:
            return {"ok": False, "data": None, "error": str(e)}

    # ================================================== 收消息

    async def feed_event(self, event: dict[str, Any]) -> None:
        """任何来源（forward WS / reverse WS / HTTP 轮询）的事件统一入口。"""
        if event.get("post_type") != "message":
            return
        cfg = self.store.get().qq
        if not cfg.enabled:
            return
        mtype = event.get("message_type")
        mid = str(event.get("message_id", ""))
        if mid and self._is_dup(mid):
            return

        if mtype == "private":
            uid = str(event.get("user_id", ""))
            if not (cfg.private_on and cfg.owner_qq and uid == cfg.owner_qq):
                return
            conv_id = f"qq:private:{uid}"
        elif mtype == "group":
            gid = str(event.get("group_id", ""))
            if not (cfg.group_on and gid in [str(g) for g in cfg.allow_groups]):
                return
            conv_id = f"qq:group:{gid}"
        else:
            return

        parsed = await self._parse_segments(event, conv_id)
        if parsed is None:
            return
        text, images, triggered = parsed
        if not triggered and mtype == "group":
            return
        if not text.strip() and not images:
            return

        sender = (event.get("sender") or {})
        who = str(sender.get("card") or sender.get("nickname") or event.get("user_id", ""))
        await self._log_inbox(mtype, who, text, images)
        msg = InboundMessage(
            channel="qq",
            conv_id=conv_id,
            text=text,
            images=images,
            sender_name=who,
            source="qq",
            meta={
                "qq_user": str(event.get("user_id", "")),
                "qq_group": str(event.get("group_id", "")),
                "message_id": mid,
                "message_type": mtype,
            },
        )
        if self.hub.on_inbound:
            # 必须 await（决不 fire-and-forget）：异常在这里被收住，绝不吞掉
            try:
                await self.hub.on_inbound(msg)
            except Exception:
                log.exception("处理 QQ 消息出错")

    async def _parse_segments(self, event: dict[str, Any], conv_id: str) -> tuple[str, list[str], bool] | None:
        """段数组 → (文本, 本地图片路径, 是否触发)。

        - 群聊触发：@ 它 / 前缀 / 全部（按配置）；
        - at 段从文本中移除；mface（表情包）也当图；
        - 图片三形态抠字节并存 inbox；文案里附本地路径。
        """
        cfg = self.store.get().qq
        raw = event.get("message")
        segs: list[dict[str, Any]] = []
        if isinstance(raw, list):
            segs = raw
        elif isinstance(raw, str):
            # CQ 字符串形态的兜底
            segs = []
            rest = raw
            for m in CQ_AT_RE.finditer(rest):
                segs.append({"type": "at", "data": {"qq": m.group(1)}})
            rest2 = CQ_AT_RE.sub("", rest)
            for m in CQ_IMG_RE.finditer(rest2):
                segs.append({"type": "image", "data": {"file": m.group(1)}})
            rest2 = CQ_IMG_RE.sub("", rest2)
            if rest2:
                segs.insert(0, {"type": "text", "data": {"text": rest2}})

        texts: list[str] = []
        image_segs: list[dict[str, Any]] = []
        at_me = False
        for s in segs:
            if not isinstance(s, dict):
                continue
            st = s.get("type")
            data = s.get("data") or {}
            if st == "text":
                texts.append(str(data.get("text", "")))
            elif st == "at":
                q = str(data.get("qq", ""))
                if q == self.self_id or q == "all":
                    at_me = True
            elif st in ("image", "mface"):
                image_segs.append({"type": "image", "data": data})

        text = "".join(texts).strip()
        triggered = True
        if conv_id.startswith("qq:group:"):
            if cfg.group_trigger == "at":
                triggered = at_me
            elif cfg.group_trigger == "prefix":
                triggered = text.startswith(cfg.prefix)
                if triggered and cfg.prefix:
                    text = text[len(cfg.prefix):].strip()
            # all → True

        images: list[str] = []
        if cfg.recv_images and image_segs:
            max_n = max(1, min(int(cfg.max_images), 10))
            max_bytes = max(1.0, min(float(cfg.max_image_mb), 30.0)) * 1024 * 1024
            for i, seg in enumerate(image_segs[:max_n], start=1):
                data = await self._fetch_image_bytes(seg.get("data") or {})
                if data is None:
                    log.warning("第 %s 张图下载失败，跳过", i)
                    continue
                if len(data) > max_bytes:
                    log.warning("第 %s 张图太大（%.1fMB），跳过", i, len(data) / 1048576)
                    continue
                ext = _sniff_ext(data)
                mid = str(event.get("message_id", "qq"))
                path = self.inbox / f"{mid}-{i}.{ext}"
                try:
                    path.write_bytes(data)
                    images.append(str(path))
                except OSError as e:
                    log.warning("第 %s 张图存盘失败：%s", i, e)

        if images:
            parts = []
            for i, p in enumerate(images, 1):
                try:
                    kb = max(1, Path(p).stat().st_size // 1024)
                except OSError:
                    kb = 0
                extra = f" → {p}" if cfg.send_images else ""
                parts.append(f"第{i}张 {kb}KB{extra}")
            desc = f"\n（对方发来 {len(images)} 张图：{'；'.join(parts)}；要发回去就用对应本地路径）"
            text = (text + desc) if text else f"（对方发来了图片，没有配字）{desc}"
        return text, images, triggered

    @staticmethod
    async def _fetch_image_bytes(data: dict[str, Any]) -> bytes | None:
        url = str(data.get("url", ""))
        file = str(data.get("file", ""))
        try:
            if url.startswith("file://"):
                return Path(url[7:].replace("/", "\\") if re.match(r"file:///[A-Za-z]:", url) else url[7:]).read_bytes()
            if url.startswith(("http://", "https://")):
                async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as cx:
                    r = await cx.get(url)
                    if r.status_code == 200:
                        return r.content
                    return None
            if file.startswith("base64://"):
                return base64.b64decode(file[9:])
            if file and ("\\" in file or "/" in file):
                p = Path(file)
                if p.is_file():
                    return p.read_bytes()
            if file:  # 仅有 file 名（后端未给 url）
                if file.startswith(("http://", "https://")):
                    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as cx:
                        r = await cx.get(file)
                        if r.status_code == 200:
                            return r.content
            return None
        except Exception as e:
            log.debug("抠图失败：%s", e)
            return None

    async def _log_inbox(self, mtype: str, who: str, text: str, images: list[str]) -> None:
        kind = "话" if text.strip() and not text.startswith("（对方发来了图片") else "图片"
        extra = f"（含 {len(images)} 张图）" if images else ""
        log.info("收到 QQ 里的%s%s ← %s：「%s」", kind, extra, who, text[:60])

    # ---- 去重 ----
    def _is_dup(self, mid: str) -> bool:
        if mid in self._dedup:
            return True
        self._dedup[mid] = None
        while len(self._dedup) > DEDUP_MAX:
            self._dedup.popitem(last=False)
        return False

    # ---- forward WS ----
    async def _ws_loop(self) -> None:
        import websockets

        backoff = 3.0
        while not self._stop.is_set():
            cfg = self.store.get().qq
            if not cfg.enabled:
                await asyncio.sleep(3)
                continue
            if cfg.mode != "forward_ws":
                await asyncio.sleep(2)
                continue
            try:
                headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else None
                async with websockets.connect(cfg.ws_url, additional_headers=headers, ping_interval=30) as ws:
                    log.info("已连上 OneBot WS：%s", cfg.ws_url)
                    self.connected = True
                    backoff = 3.0
                    await self._refresh_login_info()
                    async for raw in ws:
                        try:
                            await self.feed_event(json.loads(raw))
                        except json.JSONDecodeError:
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                log.warning("OneBot WS 断开（%s），%s 秒后重连", e, int(backoff))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.6, 60.0)
        self.connected = False

    # ---- HTTP 轮询 ----
    async def _poll_loop(self) -> None:
        cursor = _now_ts()  # 冷启动：只认此刻起的新消息，不补旧的
        cursors: dict[str, int] = {"private": cursor, **{g: cursor for g in self.store.get().qq.allow_groups}}
        while not self._stop.is_set():
            cfg = self.store.get().qq
            try:
                if not cfg.enabled:
                    await asyncio.sleep(3)
                    continue
                # 私聊
                if cfg.private_on and cfg.owner_qq:
                    r = await self._call("get_friend_msg_history", {"user_id": _int_or_str(cfg.owner_qq), "count": 10})
                    msgs = (r.get("data") or {}).get("messages") if r.get("ok") else None
                    if isinstance(msgs, list):
                        for m in sorted(msgs, key=lambda x: x.get("time", 0)):
                            t = int(m.get("time", 0))
                            if t < cursors["private"]:
                                continue
                            cursors["private"] = max(cursors["private"], t)
                            if str(m.get("user_id")) != str(cfg.owner_qq):
                                continue
                            await self.feed_event(_hist_to_event(m, "private"))
                # 群聊
                if cfg.group_on:
                    for gid in cfg.allow_groups:
                        g = str(gid)
                        r = await self._call("get_group_msg_history", {"group_id": _int_or_str(g), "count": 30})
                        msgs = (r.get("data") or {}).get("messages") if r.get("ok") else None
                        if isinstance(msgs, list):
                            cur = cursors.get(g, cursor)
                            for m in sorted(msgs, key=lambda x: x.get("time", 0)):
                                t = int(m.get("time", 0))
                                if t < cur:
                                    continue
                                cur = max(cur, t)
                                if str(m.get("user_id")) != str(cfg.owner_qq):
                                    continue
                                await self.feed_event(_hist_to_event(m, "group", gid=g))
                            cursors[g] = cur
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("QQ 轮询出错：%s", e)
            await asyncio.sleep(max(1.0, float(self.store.get().qq.poll_interval)))

    # ================================================== 发消息

    async def send_message(self, conv_id: str, text: str, images: list[str] | None = None,
                           meta: dict[str, Any] | None = None) -> None:
        cfg = self.store.get().qq
        if not cfg.enabled:
            log.info("QQ 总开关关着，这轮该发但没发：「%s」", text[:40])
            return

        clean_text, md_images = self._extract_markdown_images(text) if cfg.send_images else (text, [])
        all_images: list[str] = list(md_images)
        for p in images or []:
            if p not in all_images:
                all_images.append(p)

        if cfg.dry_run:
            icon = f"（还有 {len(all_images)} 张图）" if all_images else ""
            log.info("试运行：本来要发到 QQ →「%s」%s", clean_text[:40], icon)
            return

        async with self._send_lock:
            await self._send_paced(conv_id, clean_text, all_images)

    async def _send_paced(self, conv_id: str, text: str, images: list[str]) -> None:
        cfg = self.store.get().qq
        kind, target = _split_conv(conv_id)
        if target is None:
            log.warning("会话 id 不认识，发不了：%s", conv_id)
            return

        segs = [s.strip() for s in re.split(r"\n\s*\n", text) if s.strip()]
        if not segs and text.strip():
            segs = [text.strip()]
        ok_all = True
        for seg in segs:
            r = await self._send_one(kind, target, [{"type": "text", "data": {"text": seg}}])
            ok_all = ok_all and r
            await asyncio.sleep(SEND_SEG_DELAY)
        # 图片排在文字之后，一张一条
        max_n = 4
        sent = 0
        for p in images:
            if sent >= max_n:
                log.info("图片超过 4 张，后面的不发")
                break
            path = self._resolve_image(p)
            if path is None:
                log.warning("找不到这张图：%s", p)
                continue
            size = path.stat().st_size
            if size > max(1.0, min(float(cfg.max_image_mb), 30.0)) * 1024 * 1024:
                log.warning("这张图太大（%.1fMB），跳过", size / 1048576)
                continue
            r = await self._send_one(kind, target, [
                {"type": "image", "data": {"file": str(path).replace("\\", "/")}}
            ])
            ok_all = ok_all and r
            sent += 1
            await asyncio.sleep(SEND_SEG_DELAY)
        if ok_all:
            log.info("✅ 已发到 QQ：「%s」%s", text[:50], f"（{sent} 张图）" if sent else "")
        else:
            log.warning("⚠ 发 QQ 有问题：「%s」", text[:50])

    async def _send_one(self, kind: str, target: str, message: list[dict[str, Any]]) -> bool:
        if kind == "private":
            action, body = "send_private_msg", {"user_id": _int_or_str(target), "message": message}
        else:
            action, body = "send_group_msg", {"group_id": _int_or_str(target), "message": message}
        r = await self._call(action, body)
        if not r.get("ok"):
            log.warning("   %s 失败：%s", action, str(r.get("error") or r.get("raw"))[:140])
        return bool(r.get("ok"))

    async def send_voice(self, conv_id: str, audio_path: str) -> None:
        cfg = self.store.get().qq
        if not cfg.enabled:
            return
        kind, target = _split_conv(conv_id)
        if target is None:
            return
        path = Path(audio_path)
        if not path.is_file():
            log.warning("语音文件不存在：%s", audio_path)
            return
        if cfg.dry_run:
            log.info("试运行：本来要发语音 → %s", path.name)
            return
        async with self._send_lock:
            await self._send_one(kind, target, [
                {"type": "record", "data": {"file": path.resolve().as_posix()}}
            ])
            log.info("已发语音条：%s", path.name)

    # ---- markdown 抠图与相对路径 ----
    @staticmethod
    def _extract_markdown_images(text: str) -> tuple[str, list[str]]:
        found: list[str] = []
        for m in MD_IMG_RE.finditer(text):
            v = m.group(1).strip()
            if v and v not in found:
                found.append(v)
        clean = MD_IMG_RE.sub("", text)
        clean = re.sub(r"[ \t]+$", "", clean, flags=re.M)
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
        return clean, found

    def _resolve_image(self, ref: str) -> Path | None:
        ref = ref.strip().strip("'\"")
        if re.match(r"^[a-zA-Z]:[\\/]", ref) or ref.startswith(("\\\\", "/")):
            p = Path(ref)
            return p if p.is_file() else None
        cfg = self.store.get().qq
        roots: list[Path] = []
        if cfg.image_root:
            roots.append(Path(cfg.image_root))
        roots += [Path.cwd(), Path.cwd() / "data" / "media", self.inbox]
        for root in roots:
            p = root / ref
            try:
                if p.is_file():
                    return p
            except OSError:
                continue
        return None

    # ---- 管理接口用 ----
    async def test_send(self, text: str, target: str) -> dict[str, Any]:
        """真发一条测试句（绕过试运行；但总开关必须开着或临时无视）。"""
        kind, tgt = _split_conv(target)
        if tgt is None:
            # 目标形如 "private:123" / "group:456"
            if ":" in target:
                k, v = target.split(":", 1)
                kind, tgt = ("private" if k == "private" else "group"), v
            elif self.store.get().qq.owner_qq:
                kind, tgt = "private", self.store.get().qq.owner_qq
        if tgt is None:
            return {"ok": False, "msg": "没有可用目标：填「主人的 QQ 号」或用 private:号 / group:号 指定"}
        r = await self._send_one(kind, tgt, [{"type": "text", "data": {"text": text}}])
        return {"ok": r, "msg": "已发出" if r else "发送失败（看日志）"}

    def status(self) -> dict[str, Any]:
        cfg = self.store.get().qq
        return {
            "enabled": cfg.enabled,
            "dry_run": cfg.dry_run,
            "mode": cfg.mode,
            "connected": self.connected,
            "self_id": self.self_id,
            "nickname": self.nickname,
            "inbox": str(self.inbox),
        }


def _int_or_str(s: str) -> int | str:
    try:
        return int(s)
    except (TypeError, ValueError):
        return s


def _split_conv(conv_id: str) -> tuple[str, str | None]:
    parts = conv_id.split(":")
    if len(parts) == 3 and parts[0] == "qq":
        return parts[1], parts[2]
    return "private", None


def _hist_to_event(m: dict[str, Any], message_type: str, gid: str | None = None) -> dict[str, Any]:
    """把 get_*_msg_history 的一条历史消息，包装成 message 事件形状。"""
    evt: dict[str, Any] = {
        "post_type": "message",
        "message_type": message_type,
        "message_id": m.get("message_id"),
        "user_id": m.get("user_id"),
        "message": m.get("message"),
        "time": m.get("time"),
        "sender": m.get("sender") or {},
    }
    if message_type == "group" and gid:
        evt["group_id"] = gid
    return evt


def _sniff_ext(data: bytes) -> str:
    if len(data) >= 2 and data[0] == 0x89 and data[1] == 0x50:
        return "png"
    if len(data) >= 2 and data[0] == 0x47 and data[1] == 0x49:
        return "gif"
    if len(data) >= 12 and data[0] == 0x52 and data[1] == 0x49 and data[8] == 0x57:
        return "webp"
    return "jpg"