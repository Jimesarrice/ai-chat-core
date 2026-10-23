"""开发自测用的 mock OneBot 11 HTTP 后端。

python scripts/mock_onebot.py [--port 3000]
- 支持 get_login_info / send_private_msg / send_group_msg / get_*_msg_history
- 收到的消息打屏 + 记到内存；POST /push_fake 可以伪造一条历史消息（供 http 轮询模式自测）
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SENT: list[dict] = []
FAKE: dict[str, list[dict]] = {"private": [], "group": []}


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-onebot"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}
        action = self.path.strip("/")

        if action == "get_login_info":
            return self._json({"status": "ok", "retcode": 0, "data": {"user_id": 10001, "nickname": "mock鱼"}})
        if action in ("send_private_msg", "send_group_msg"):
            SENT.append({"at": time.time(), "action": action, "body": body})
            print(f"[mock-onebot] {action}: {json.dumps(body, ensure_ascii=False)[:200]}", flush=True)
            return self._json({"status": "ok", "retcode": 0, "data": {"message_id": int(time.time() * 1000)}})
        if action == "get_friend_msg_history":
            uid = str(body.get("user_id"))
            msgs = [m for m in FAKE["private"] if str(m.get("user_id")) == uid]
            return self._json({"status": "ok", "retcode": 0, "data": {"messages": msgs}})
        if action == "get_group_msg_history":
            gid = str(body.get("group_id"))
            msgs = [m for m in FAKE["group"] if str(m.get("group_id", "")) == gid or not m.get("group_id")]
            return self._json({"status": "ok", "retcode": 0, "data": {"messages": msgs}})
        if action == "push_fake":
            # 非 OneBot 标准：仅供自测注入一条历史消息
            kind = body.get("message_type", "private")
            FAKE[kind].append({
                "message_id": int(time.time() * 1000),
                "time": int(time.time()),
                "user_id": body.get("user_id", 20002),
                "group_id": body.get("group_id"),
                "message": [{"type": "text", "data": {"text": body.get("text", "你好")}}],
                "sender": {"nickname": "测试者"},
            })
            return self._json({"status": "ok", "retcode": 0, "data": {"ok": True}})
        if action == "sent" :
            return self._json({"status": "ok", "retcode": 0, "data": {"sent": SENT}})
        return self._json({"status": "failed", "retcode": 1404, "data": None})

    def do_GET(self):
        if self.path.startswith("/sent"):
            return self._json({"status": "ok", "retcode": 0, "data": {"sent": SENT}})
        self._json({"status": "failed", "retcode": 1404, "data": None})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3000)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock-onebot 在 http://127.0.0.1:{args.port}（Ctrl+C 退出）")
    srv.serve_forever()


if __name__ == "__main__":
    main()