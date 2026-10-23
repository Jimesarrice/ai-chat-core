"""开发自测用的 mock 大模型（OpenAI 兼容，含流式与工具调用）。

python scripts/mock_llm.py [--port 8081] [--model mock]
用途：没有真实模型/接口时，把「本地」接入点指到这里就能跑通全链路自测。
行为：
- 心情判断请求 → 回一段合法 JSON（moodDelta +4）；
- 提炼/扩词/洗键等整理请求 → 回一行像样的格式；
- 用户说「记住…」→ 下一轮发起 remember 工具调用；
- 其它 → 复述式回复（流式吐出）。
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _sse(chunk: dict) -> bytes:
    return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-llm"

    def log_message(self, fmt, *args):  # 安静点
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": [{"id": "mock", "object": "model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        messages = req.get("messages") or []
        tools = req.get("tools") or []
        stream = bool(req.get("stream"))

        system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        last_user = ""
        for m in messages:
            if m.get("role") == "user":
                last_user = str(m.get("content", ""))

        tool_calls = None
        text = ""

        if "判断**这一轮里你的心情" in system or "moodDelta" in system:
            text = json.dumps({
                "moodDelta": 4,
                "dims": {"happy": 3, "calm": 1, "sweet": 2, "heartache": 0, "worry": 0, "sad": 0, "jealous": 0},
                "reason": "对方打了个招呼，我当时轻轻应了一声，心里挺高兴",
                "confidence": 85,
            }, ensure_ascii=False)
        elif "记忆整理助手" in system:
            text = "1|（整理结果示例）|词一 词二||0.5|"
        elif "只输出标题" in system:
            text = "自测会话"
        elif ("记住" in last_user or "remember" in last_user) and not any(m.get("role") == "tool" for m in messages):
            # 用户让记东西 → 发起 remember 工具调用
            if any((t.get("function") or {}).get("name") == "remember" for t in tools):
                idx = str(last_user).replace("记住", "").strip() or "测试记忆一条"
                tool_calls = [{
                    "id": "call_mock_1", "type": "function",
                    "function": {"name": "remember",
                                 "arguments": json.dumps({"text": f"对方说过：{idx}", "importance": 0.5,
                                                          "tags": ["自测"]}, ensure_ascii=False)},
                }]
        else:
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            if has_tool_result:
                text = "（mock）我记下了，接着说～"
            else:
                text = f"（mock 回声）收到：{last_user[:60]}"

        if not stream:
            msg = {"role": "assistant", "content": text}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            self._json(200, {
                "id": "cmp", "object": "chat.completion", "created": int(time.time()), "model": "mock",
                "choices": [{"index": 0, "message": msg,
                             "finish_reason": "tool_calls" if tool_calls else "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def chunk(delta: dict, finish=None) -> None:
            self.wfile.write(_sse({
                "id": "cmp", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }))
            self.wfile.flush()

        if tool_calls:
            chunk({"role": "assistant", "content": ""})
            chunk({"tool_calls": [{"index": 0, "id": tool_calls[0]["id"], "type": "function",
                                   "function": {"name": tool_calls[0]["function"]["name"], "arguments": ""}}]})
            args = tool_calls[0]["function"]["arguments"]
            for i in range(0, len(args), 24):
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 24]}}]})
            chunk({}, finish="tool_calls")
        else:
            chunk({"role": "assistant", "content": ""})
            for i in range(0, len(text), 12):
                chunk({"content": text[i:i + 12]})
                time.sleep(0.01)
            chunk({}, finish="stop")
        self.wfile.write(_sse({"id": "cmp", "object": "chat.completion.chunk",
                               "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock-llm 在 http://127.0.0.1:{args.port}/v1（Ctrl+C 退出）")
    srv.serve_forever()


if __name__ == "__main__":
    main()