"""端到端自测（需要 mock_llm + mock_onebot + 主服务都在跑）。

python scripts/e2e_test.py
"""
from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8760"
MOCK_OB = "http://127.0.0.1:3000"

passed, failed = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name} {extra}")


def main() -> None:
    c = httpx.Client(timeout=30)

    print("== 1. 网页聊天全链路 ==")
    conv = c.post(f"{BASE}/api/conversations").json()["id"]
    c.post(f"{BASE}/api/conversations/{conv}/messages", json={"text": "你好呀，今天过得怎么样"})
    time.sleep(4.5)
    msgs = c.get(f"{BASE}/api/conversations/{conv}/messages").json()["messages"]
    check("用户消息已落库", any(m["role"] == "user" for m in msgs))
    reply = [m for m in msgs if m["role"] == "assistant"]
    check("模型回复已落库（mock 回声）", bool(reply) and "mock" in reply[-1]["content"], str(reply))

    print("== 2. 自动起标题 ==")
    time.sleep(2.0)
    convs = c.get(f"{BASE}/api/conversations").json()["conversations"]
    mine = [x for x in convs if x["id"] == conv][0]
    check("会话被自动起了标题", mine["title"] == "自测会话", mine["title"])

    print("== 3. 心情判断与衰减 ==")
    time.sleep(1.5)
    mood = c.get(f"{BASE}/api/mood").json()
    check("心情被判断过（>70 或时间线非空）", mood["mood"] != 70 or len(mood["timeline"]) > 0,
          f"mood={mood['mood']} timeline={len(mood['timeline'])}")
    check("时间线里有记录", len(mood["timeline"]) >= 1, str(mood["timeline"][:1]))

    print("== 4. 记忆：remember 工具 ==")
    c.post(f"{BASE}/api/conversations/{conv}/messages", json={"text": "记住：我喜欢蓝色的衣服"})
    time.sleep(5.0)
    mem = c.get(f"{BASE}/api/memory/list").json()
    check("记忆库有了一条", mem["stats"]["total"] >= 1, json.dumps(mem["stats"], ensure_ascii=False))
    text = mem["memories"][0]["text"] if mem["memories"] else ""
    check("记忆内容对得上", "蓝色" in text, text)

    print("== 5. 记忆：召回与诊断 ==")
    c.post(f"{BASE}/api/conversations/{conv}/messages", json={"text": "我还喜欢蓝色吗，你还记得我喜欢什么"})
    time.sleep(4.5)
    tr = c.get(f"{BASE}/api/memory/traces?limit=5").json()["traces"]
    check("有召回记录", len(tr) >= 1)
    if tr:
        rows = tr[0].get("rows") or []
        check("召回有打分行（C/P）", bool(rows), json.dumps(tr[0], ensure_ascii=False)[:200])
        picked = tr[0].get("picked") or []
        check("蓝色那条被想起来了", bool(picked), json.dumps(rows[:3], ensure_ascii=False))

    print("== 6. 主动搭话（演练） ==")
    c.post(f"{BASE}/api/config", json={"patch": {"speakup": {"targets": [conv]}}})
    r = c.post(f"{BASE}/api/speakup/roll", json={"mode": "drill"}).json()
    rec = r.get("record", {}).get("text", "")
    check("演练有记录且绝不真发", ("演练" in rec) or ("不掷" in rec) or ("没掷中" in rec) or ("不能说话" in rec), rec)
    logs = c.get(f"{BASE}/api/speakup/logs").json()["logs"]
    check("搭话日志有了", len(logs) >= 1, str(logs[:1]))

    print("== 7. QQ：出站（test-send 真发） ==")
    r = c.post(f"{BASE}/api/qq/test-send", json={"text": "自测测试句"}).json()
    check("测试句发送成功", bool(r.get("ok")), str(r))
    sent = httpx.get(f"{MOCK_OB}/sent").json()["data"]["sent"]
    check("mock 后端收到了", any("自测测试句" in json.dumps(x["body"], ensure_ascii=False) for x in sent), str(sent[-1:]))

    print("== 8. QQ：入站（轮询转进来 → 回复发回） ==")
    httpx.post(f"{MOCK_OB}/push_fake", json={"message_type": "private", "user_id": 20002, "text": "在吗，帮我看看"})
    time.sleep(6.0)
    sent = httpx.get(f"{MOCK_OB}/sent").json()["data"]["sent"]
    replied = [x for x in sent if "mock" in json.dumps(x["body"], ensure_ascii=False)]
    check("QQ 消息被接进来并回复了", bool(replied), json.dumps([x["body"] for x in sent][-3:], ensure_ascii=False))
    qq_conv = [x for x in c.get(f"{BASE}/api/conversations").json()["conversations"]
               if x["channel"] == "qq"]
    check("QQ 会话出现在会话列表", bool(qq_conv), str(qq_conv))

    print("== 9. 配置热存 ==")
    r = c.post(f"{BASE}/api/config", json={"patch": {"persona": {"catchphrases": "自测口头禅"}}}).json()
    check("配置保存成功", r.get("ok") is True)
    cfg = c.get(f"{BASE}/api/config").json()["current"]
    check("配置生效", cfg["persona"]["catchphrases"] == "自测口头禅")

    print("== 10. 日志总线 ==")
    logs = c.get(f"{BASE}/api/logs/recent?limit=200").json()["logs"]
    joined = "\n".join(x["message"] for x in logs)
    check("日志里有记忆注入/召回线索", ("记忆" in joined) or ("注入" in joined), joined[-300:])
    check("日志里有 QQ 相关记录", "QQ" in joined)

    print(f"\n结果：通过 {passed} / 失败 {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()