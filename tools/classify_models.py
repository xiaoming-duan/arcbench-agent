"""模型能力分类：区分「推理模型」与「直答模型」。

判据：给一个极小的 max_tokens（60）。
  - 直答模型 -> 有 content 正文，finish_reason=stop
  - 推理模型 -> reasoning_content 有、content 空，finish_reason=length

推理模型在本网关下会把 max_tokens 全烧在 reasoning 上，
导致大输出调用（写测试/写实现）返回空正文 —— 不适合本流水线。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ["OPENAI_BASE_URL"].rstrip("/")
KEY = os.environ["OPENAI_API_KEY"]

MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5.2",
    "glm-5.3",
    "glm-5.3-flash",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "kimi-k3",
    "minimax-m3",
    "qwen3.6-flash",
    "qwen3.6-plus",
    "qwen3.7-plus",
    "qwen3.8-max",
]


def probe(model: str) -> tuple[str, str]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
        "max_tokens": 60,
        "temperature": 0.0,
    }
    request = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return "ERROR", f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:120]}"
    except Exception as exc:  # noqa: BLE001
        return "ERROR", f"{type(exc).__name__}: {exc}"

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning_content") or "").strip()
    finish = choice.get("finish_reason")
    if content:
        return "DIRECT", f"content={content[:40]!r} finish={finish}"
    if reasoning:
        return "REASONING", f"reasoning={len(reasoning)}字符 content=空 finish={finish}"
    return "EMPTY", f"finish={finish} usage={data.get('usage')}"


for name in MODELS:
    t0 = time.time()
    kind, detail = probe(name)
    print(f"{kind:9s} {name:26s} {time.time()-t0:5.1f}s  {detail}", flush=True)

print("CLASSIFY DONE", flush=True)
