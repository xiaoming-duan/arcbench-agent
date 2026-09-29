"""模型选型基准：测真正会失败的那个调用（write_tests / implement），而不是 design。

design 输出小、快；write_tests 要吐完整文件，是实际瓶颈。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logging.basicConfig(level=logging.ERROR)

from factory.adapter import _adapt
from factory.generator import LLMGenerator
from factory.llm import ModelClient
from factory.workspace import load_requirements_raw

REQ_FILE = Path("requirements_sample/requirements.yaml")
raw = load_requirements_raw(REQ_FILE)
req = _adapt(raw, source=REQ_FILE).requirements[0]
print(f"目标需求: {req.req_id} {req.name}", flush=True)

CONFIGS = [
    ("glm-5.3-flash", 1200),
    ("qwen3.6-flash", 1200),
    ("deepseek-v4-pro", 1200),
    ("glm-5.2", 1200),
]

for model, max_tokens in CONFIGS:
    os.environ["MODEL"] = model
    client = ModelClient(temperature=0.1, max_tokens=max_tokens, timeout_s=420)
    gen = LLMGenerator(client, "vitest")
    t0 = time.time()
    try:
        plan = gen.design(req)
        files = gen.write_tests(req, plan)
        size = sum(len(f.content) for f in files)
        print(f"OK    {model:18s} mt={max_tokens} {time.time()-t0:6.1f}s files={len(files)} bytes={size}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {model:18s} mt={max_tokens} {time.time()-t0:6.1f}s {type(exc).__name__}: {str(exc)[:110]}", flush=True)

print("BENCH DONE", flush=True)
