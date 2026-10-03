"""模型接入层（模型路由层的最小实现）。

Runner 注入的环境变量：OPENAI_API_KEY / OPENAI_BASE_URL / MODEL

双后端设计：
  openai-sdk   —— 装了 openai 包时优先使用
  stdlib-http  —— 仅用标准库 urllib 调用 OpenAI 兼容 /chat/completions

之所以要有 stdlib 回退：Agent 运行环境**不保证**能装上 openai 包
（离线/内网环境 pip 不可达）。模型路径不应因此整条失效。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("factory.llm")

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 3
# 超时这类「已经等满一整个 timeout」的失败，只再试一次。
# 理由：同一个 prompt、同一个超时，原样重发大概率再等满一次 ——
# 3 × 180s = 9 分钟换一次必然的重蹈覆辙，把整轮的时间预算烧光。
# 快速失败（连接被拒 / 5xx / 429）重试很便宜，仍用 MAX_RETRIES。
MAX_SLOW_RETRIES = 1
MAX_TOKEN_CEILING = 8000


@dataclass
class CallStats:
    """成本记账。

    gateway_retries 必须与 rewrite_rounds **分开计数**：
    网关不稳定造成的重试是环境失败，不是逻辑失败，混在一起会把
    环境问题误判成"模型不会修"。
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    gateway_retries: int = 0
    # 协议适配重试（temperature / response_format / max_tokens / 推理占满预算），
    # 与网络层的 gateway_retries 分开，避免污染重试成功率口径
    adaptation_retries: int = 0
    json_retries: int = 0
    # 重试成败拆分：用于回答"重试到底有没有用"
    #   retries_succeeded = 最终成功的调用所消耗的重试次数
    #   retries_exhausted = 最终失败的调用所消耗的重试次数
    retries_succeeded: int = 0
    retries_exhausted: int = 0
    # prompt 体积：用于验证「长 prompt 是否更易断连」这一假设
    prompt_chars: int = 0
    max_prompt_chars: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "gateway_retries": self.gateway_retries,
            "adaptation_retries": self.adaptation_retries,
            "json_retries": self.json_retries,
            "retries_succeeded": self.retries_succeeded,
            "retries_exhausted": self.retries_exhausted,
            "prompt_chars": self.prompt_chars,
            "max_prompt_chars": self.max_prompt_chars,
        }

    @property
    def retry_success_rate(self) -> float | None:
        """重试成功率 = 重试后成功 / 总重试次数。用于判断重试是否在烧钱。"""
        total = self.retries_succeeded + self.retries_exhausted
        return (self.retries_succeeded / total) if total else None

    def snapshot(self) -> dict[str, int]:
        return self.to_dict()

    def delta(self, before: dict[str, int]) -> dict[str, int]:
        """按需求切分成本：返回相对于 before 快照的增量。"""
        return {k: getattr(self, k) - int(before.get(k, 0)) for k in self.to_dict()}

    def summary(self) -> str:
        rate = self.retry_success_rate
        rate_text = "n/a" if rate is None else f"{rate * 100:.0f}%"
        return (
            f"调用 {self.calls} 次 / token {self.total_tokens}"
            f"（prompt {self.prompt_tokens} + completion {self.completion_tokens}"
            f"，其中推理 {self.reasoning_tokens}）"
            f" / 网关重试 {self.gateway_retries} 次"
            f"（成功 {self.retries_succeeded} / 耗尽 {self.retries_exhausted}，成功率 {rate_text}）"
            f" / 协议适配重试 {self.adaptation_retries} 次"
            f" / prompt {self.prompt_chars} 字符（单次最大 {self.max_prompt_chars}）"
        )


class ModelUnavailableError(RuntimeError):
    """模型不可用（缺少凭据）。"""


class ModelCallError(RuntimeError):
    """模型调用失败（网络或服务端错误）。"""


class ModelFatalError(ModelCallError):
    """★终局性模型故障：本进程内**不可能**靠重试或换提示词恢复。

    与普通 `ModelCallError` 的区别只有一条，但很关键：
    **要不要终止整轮**。普通失败只影响当前需求；终局故障会让后面
    *每一次* 调用都撞同一堵墙 —— 逐个需求去撞纯属烧时间。

    目前两类：
      · 配额 / 余额耗尽（429 insufficient_quota）
      · 凭据无效（401 invalid_api_key）
    """


class ModelAuthError(ModelFatalError):
    """凭据无效（401 invalid_api_key / unauthorized）。实测（平台 2026-10-02 07:19）：

        POST https://api.arc-bench.com/v1/chat/completions "HTTP/1.1 401 Unauthorized"
        Error code: 401 - {'code': 'invalid_api_key', 'message': 'unauthorized'}

    401 不被重试是**对的**（当时已经如此），但它此前**不会终止整轮**，
    于是每个需求都各自撞一次同一堵墙。
    """
    ...


class ModelQuotaExhaustedError(ModelFatalError):
    """模型配额 / 余额耗尽 —— **不是瞬时故障**，重试永远不会成功。

    实测（平台 2026-09-30 18:26）：
        429 {'code': 'Free quota exhausted and balance too low, please recharge
             compute credits.', 'type': 'insufficient_quota'}
    它带着 429 状态码，因此**必须与限流区分开**：
      - 429 rate_limit_exceeded  -> 瞬时，退避重试有意义
      - 429 insufficient_quota   -> 终局，重试纯属烧时间
    把两者混为一谈的代价实测可见：一次运行里「网关重试 11 次（成功 0）」。

    单独成类还有一个作用：它可以一路穿透到 pipeline，让整轮**提前停止**——
    配额没了，后面每个需求都注定失败。
    """
    ...


# 配额/计费耗尽的特征词（与限流区分）
_QUOTA_MARKERS = (
    "insufficient_quota", "quota exhausted", "exceeded your current quota",
    "balance too low", "recharge", "billing", "insufficient balance",
    "no credits", "compute credits",
)


def _is_quota_exhausted(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


#: 凭据无效的特征词（与「配额耗尽」区分：一个要充值，一个要换 key）
_AUTH_MARKERS = (
    "invalid_api_key", "invalid api key", "incorrect api key",
    "unauthorized", "authentication_error", "authentication failed",
)


def _is_auth_error(exc: Exception) -> bool:
    """是否凭据无效。

    优先看**状态码**（401 是权威信号），文本特征作为兜底 ——
    SDK 与 HTTP 两条后端抛出的异常类型不同，状态码是唯一的共同信号。
    """
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status == 401:
        return True
    lowered = str(exc or "").lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


class ModelClient:
    """OpenAI 兼容 Chat Completions 客户端（SDK / 标准库双后端）。"""

    BACKEND_SDK = "openai-sdk"
    BACKEND_HTTP = "stdlib-http"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_s: float = 600.0,
    ) -> None:
        self.model = (model or os.environ.get("MODEL", "")).strip()
        self.api_key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        raw_base = (base_url or os.environ.get("OPENAI_BASE_URL", "")).strip()
        if not raw_base:
            raw_base = DEFAULT_BASE_URL
            logger.warning("未设置 OPENAI_BASE_URL，回退到默认值 %s", DEFAULT_BASE_URL)
        self.base_url = raw_base.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.stats = CallStats()
        self._sdk: Any = None

    # ---- 后端选择 ----

    @staticmethod
    def _sdk_importable() -> bool:
        try:
            import openai  # noqa: F401

            return True
        except ImportError:
            return False

    def backend(self) -> str:
        return self.BACKEND_SDK if self._sdk_importable() else self.BACKEND_HTTP

    def is_available(self) -> bool:
        return bool(self.api_key and self.model)

    def describe(self) -> str:
        return f"{self.backend()} model={self.model} base_url={self.base_url}"

    # ---- 调用入口 ----

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> str:
        # 记录 prompt 体积：轻量、无副作用，用于「长 prompt 更易断连」的验证
        size = len(system or "") + len(user or "")
        self.stats.prompt_chars += size
        self.stats.max_prompt_chars = max(self.stats.max_prompt_chars, size)
        if not self.is_available():
            missing = [
                name
                for name, value in (("OPENAI_API_KEY", self.api_key), ("MODEL", self.model))
                if not value
            ]
            raise ModelUnavailableError("缺少模型配置: " + ", ".join(missing))
        if self.backend() == self.BACKEND_SDK:
            return self._complete_sdk(system=system, user=user, json_mode=json_mode)
        return self._complete_http(system=system, user=user, json_mode=json_mode)

    def complete_json(self, *, system: str, user: str, attempts: int = 2) -> dict[str, Any]:
        """取 JSON。截断/围栏导致的解析失败会重试，避免单次抖动毁掉整个需求。

        关键：重试**不是原样重发**。实测输出过长被 max_tokens 截断时，同样的
        prompt 会得到同样被截断的结果，两次必然全废（平台上正是如此：
        「第 1/2 次 -> 重试 -> 第 2 次」连着失败）。所以第 2 次起追加
        「精简输出」指令，把重试变成一次真正的修复尝试，而不是掷骰子。
        """
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            effective_user = user if attempt == 1 else user + _JSON_RETRY_NOTE
            text = self.complete(system=system, user=effective_user, json_mode=True)
            try:
                return _extract_json(text)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                # 原始输出必须落日志：模型「返回了什么」是定位 JSON 问题的唯一线索。
                # 头尾都给——只看头无法判断是否被截断（上一轮就是这么被误读的）。
                logger.warning(
                    "模型输出不是合法 JSON（第 %d/%d 次）。%s",
                    attempt,
                    attempts,
                    _describe_raw(text),
                )
                if attempt < attempts:
                    logger.warning(
                        "模型输出不是合法 JSON（第 %d 次），重试并追加精简指令", attempt
                    )
                    self.stats.json_retries += 1
                    _sleep_backoff(attempt)
        raise ModelCallError(f"模型多次未返回可解析 JSON: {last_error}")

    # ---- 后端 1: openai SDK ----

    @staticmethod
    def _retry_allowance(exc: Exception) -> int:
        """这次失败最多允许再试几次（**两条后端共用同一个口径**）。

        此前两条路径各写各的：HTTP 用 `attempt < MAX_RETRIES`（最多 2 次重试），
        而 SDK 用 `attempt <= retry_cap`（最多 3 次），同一次线上运行里
        两条路径的重试次数根本不一样 —— 记账自然也对不上。
        现在统一成「已重试次数 < 允许次数」，同时消掉 off-by-one。
        """
        return MAX_SLOW_RETRIES if _is_timeout_error(exc) else MAX_RETRIES - 1

    def _complete_sdk(self, *, system: str, user: str, json_mode: bool) -> str:
        """SDK 后端（openai 包）。

        ★实测缺陷（平台 2026-09-30 14:42）：平台上 `openai` 是**装好的**
        （requirements.txt 里就有），所以走的是这条路径 —— 而它此前只做
        「create 然后直接返回 content」，于是 HTTP 后端里那一整套东西全部缺席：

          - 用量记账：成本恒为「调用 0 次 / token 0」。
            平台上「prompt 69276 字符（单次最大 11804）但调用 0 次」这个
            自相矛盾的数字，就是这么来的：prompt 体积在 complete() 入口累加，
            而 calls 只在这条路径之外自增。
          - finish_reason=length 的截断告警永不触发（截断因此不可见）
          - 空正文 / 只有 reasoning_content 不报错
          - 传输层错误不重试（HTTP 路径会重试可恢复状态码与网络异常）
          - 协议适配降级（temperature / response_format / max_tokens）也没有

        现在两条路径共用 `_with_adaptation` 与 `_extract_content`，
        差别只剩「怎么把请求发出去」。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return self._with_adaptation(payload, self._sdk_chat)

    # ---- 后端 2: 标准库 HTTP ----

    def _complete_http(self, *, system: str, user: str, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return self._with_adaptation(payload, self._post_chat)

    def _with_adaptation(self, payload: dict[str, Any], call) -> str:  # noqa: ANN001
        """协议适配降级循环 —— 两条后端共用，不再各写一份。

        逐项降级重试：不同服务端对 sampling / response_format 的支持不一致。
        注意：这类是**协议适配**重试，与网络层的 gateway_retries 分开计数——
        否则 retry_success_rate 的口径会被混入两种不同性质的失败。
        """
        for _ in range(4):
            try:
                return call(payload)
            except ModelCallError as exc:
                message = str(exc)
                if "temperature" in message and payload.get("temperature") != 1:
                    logger.warning("服务端限制 temperature，改为 1 重试")
                    self.stats.adaptation_retries += 1
                    payload["temperature"] = 1
                    continue
                if "response_format" in message and "response_format" in payload:
                    logger.warning("服务端不支持 response_format，去掉后重试")
                    self.stats.adaptation_retries += 1
                    payload.pop("response_format", None)
                    continue
                if "max_tokens" in message and "max_completion_tokens" not in payload:
                    logger.warning("服务端不接受 max_tokens，改用 max_completion_tokens 重试")
                    self.stats.adaptation_retries += 1
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    continue
                # 推理模型可能把 max_tokens 全烧在 reasoning 上，正文为空。
                # 这种情况放大预算重试，而不是直接判失败。
                if ("reasoning_content" in message or "空内容" in message):
                    key = "max_tokens" if "max_tokens" in payload else "max_completion_tokens"
                    current = int(payload.get(key) or 0)
                    if current < MAX_TOKEN_CEILING:
                        # 一步跳到高预算：逐步翻倍会白烧一次慢调用
                        grown = min(max(current * 2, 6000), MAX_TOKEN_CEILING)
                        logger.warning(
                            "正文为空（推理占满预算），max_tokens %d -> %d 后重试", current, grown
                        )
                        self.stats.adaptation_retries += 1
                        payload[key] = grown
                        self.max_tokens = grown
                        continue
                raise
        raise ModelCallError("模型调用在多轮降级后仍失败")

    def _record_usage(self, data: dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or 0)
        # 审计发现的缺口：部分服务端不返回 total_tokens，此时 total 会恒为 0，
        # 而 prompt/completion 有值 —— 回退为两者之和，避免总量被静默少计。
        if not total:
            total = prompt + completion
        self.stats.prompt_tokens += prompt
        self.stats.completion_tokens += completion
        self.stats.total_tokens += total
        self.stats.reasoning_tokens += int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        )

    def _sdk_chat(self, payload: dict[str, Any]) -> str:
        """把一次 SDK 调用发出去，并做与 HTTP 后端**同样**的记账与正文校验。"""
        if self._sdk is None:
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout_s}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._sdk = OpenAI(**kwargs)

        last_error: Exception | None = None
        retries_used = 0
        succeeded = False
        try:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = self._sdk.chat.completions.create(**payload)
                except Exception as exc:  # noqa: BLE001 —— SDK 异常层次视版本而异
                    # ★配额耗尽优先判定：它也带 429，但重试永远不会成功。
                    if _is_quota_exhausted(exc):
                        raise ModelQuotaExhaustedError(f"模型配额/余额耗尽: {exc}") from exc
                    if _is_auth_error(exc):
                        raise ModelAuthError(f"模型凭据无效: {exc}") from exc
                    last_error = ModelCallError(f"openai SDK 调用失败: {exc}")
                    if (_is_retryable_sdk_error(exc)
                            and retries_used < self._retry_allowance(exc)):
                        self.stats.gateway_retries += 1
                        retries_used += 1
                        _sleep_backoff(attempt)
                        continue
                    raise last_error from exc
                data = _sdk_response_to_dict(response)
                # ★记账与校验：这两步此前在 SDK 路径上完全缺席，
                #   导致平台上成本恒为 0、截断永不告警。
                self._record_usage(data)
                self.stats.calls += 1
                succeeded = True
                return _extract_content(data)
            raise last_error or ModelCallError("openai SDK 调用失败")
        finally:
            # 与 HTTP 后端同一套归集，且同样放在 finally 里 ——
            # 循环内 raise 会绕过"循环末尾归集"，那正是 exhausted 恒为 0 的老 bug。
            # 实测（平台 2026-10-01 01:50）：「网关重试 6 次（成功 0 / 耗尽 0）」
            # 就是因为我上次移植记账时只补了 gateway_retries、漏了这一对。
            if succeeded:
                self.stats.retries_succeeded += retries_used
            else:
                self.stats.retries_exhausted += retries_used

    def _post_chat(self, payload: dict[str, Any]) -> str:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

        last_error: Exception | None = None
        retries_used = 0
        succeeded = False
        try:
            for attempt in range(1, MAX_RETRIES + 1):
                request = urllib.request.Request(url, data=body, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                        raw = response.read().decode("utf-8", errors="replace")
                    data = json.loads(raw)
                    self._record_usage(data)
                    self.stats.calls += 1
                    succeeded = True
                    return _extract_content(data)
                except urllib.error.HTTPError as exc:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:400]
                    except Exception:  # pragma: no cover
                        pass
                    # ★配额耗尽也返回 429，但绝不重试（与限流区分）
                    if _is_quota_exhausted(detail) or _is_quota_exhausted(exc.reason):
                        raise ModelQuotaExhaustedError(
                            f"模型配额/余额耗尽（HTTP {exc.code}）: {detail[:200]}"
                        ) from exc
                    if exc.code == 401 or _is_auth_error(exc.reason) or _is_auth_error(detail):
                        raise ModelAuthError(
                            f"模型凭据无效（HTTP {exc.code}）: {detail[:200]}"
                        ) from exc
                    message = f"HTTP {exc.code} {exc.reason}: {detail}"
                    last_error = ModelCallError(message)
                    if (exc.code in RETRYABLE_STATUS
                            and retries_used < self._retry_allowance(exc)):
                        self.stats.gateway_retries += 1
                        retries_used += 1
                        _sleep_backoff(attempt)
                        continue
                    raise last_error from exc
                except urllib.error.URLError as exc:
                    last_error = ModelCallError(f"网络错误: {exc.reason}")
                    if retries_used < self._retry_allowance(exc):
                        self.stats.gateway_retries += 1
                        retries_used += 1
                        _sleep_backoff(attempt)
                        continue
                    raise last_error from exc
                except TimeoutError as exc:
                    # 注意：TimeoutError 是 OSError 子类，必须排在下面的 OSError 之前
                    last_error = ModelCallError(f"读取超时（>{self.timeout_s}s）")
                    # 超时只再试一次（见 MAX_SLOW_RETRIES 与 _retry_allowance）
                    if retries_used < self._retry_allowance(exc):
                        self.stats.gateway_retries += 1
                        retries_used += 1
                        _sleep_backoff(attempt)
                        continue
                    raise last_error from exc
                except (ConnectionError, OSError) as exc:
                    # 实测缺陷：http.client.RemoteDisconnected（"Remote end closed connection
                    # without response"）**不是** URLError 的子类（它是 ConnectionResetError
                    # -> ConnectionError -> OSError），因此会穿透上面的 handler：
                    # 既不重试也不计入 gateway_retries。
                    # 表现是"网关不稳"，实质是客户端根本没重试。
                    last_error = ModelCallError(f"连接异常（{type(exc).__name__}）: {exc}")
                    if retries_used < self._retry_allowance(exc):
                        self.stats.gateway_retries += 1
                        retries_used += 1
                        _sleep_backoff(attempt)
                        continue
                    raise last_error from exc
                except json.JSONDecodeError as exc:
                    raise ModelCallError(f"响应不是合法 JSON: {raw[:200]}") from exc
            raise last_error or ModelCallError("模型调用失败")
        finally:
            # 统一归集：无论从哪条分支退出（含循环内直接 raise），重试次数都要记账。
            # 早前版本在循环末尾归集，循环内 raise 会绕过它，导致 exhausted 恒为 0。
            if succeeded:
                self.stats.retries_succeeded += retries_used
            else:
                self.stats.retries_exhausted += retries_used


def _sleep_backoff(attempt: int) -> None:
    delay = min(2 ** (attempt - 1), 8)
    logger.warning("模型调用失败，%ss 后重试（第 %d 次）", delay, attempt + 1)
    time.sleep(delay)


def _sdk_response_to_dict(response: Any) -> dict[str, Any]:
    """把 openai SDK 的响应对象转成与 HTTP 后端一致的 dict。

    统一成 dict 之后，`_record_usage` 与 `_extract_content` 才能两条路径共用——
    否则记账与正文校验就得在 SDK 路径上重写一遍，而那正是它们当初被漏掉的原因。
    """
    for attr in ("model_dump", "to_dict", "dict"):
        dump = getattr(response, attr, None)
        if callable(dump):
            try:
                data = dump()
            except TypeError:      # 某些版本的 dict() 需要参数
                continue
            if isinstance(data, dict):
                return data
    # 兜底：手工拼出最小可用结构
    choice = (getattr(response, "choices", None) or [None])[0]
    message = getattr(choice, "message", None)
    usage = getattr(response, "usage", None)
    return {
        "choices": [
            {
                "finish_reason": getattr(choice, "finish_reason", None),
                "message": {
                    "content": getattr(message, "content", None),
                    "reasoning_content": getattr(message, "reasoning_content", None),
                },
            }
        ],
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        },
    }


def _is_timeout_error(exc: Exception) -> bool:
    """是否是「等满了一整个超时」的失败（区别于快速失败）。

    平台实测（2026-09-30 14:46）：`openai SDK 调用失败: Request timed out.`
    —— 这类失败每次都要耗满 timeout_s，与连接被拒/5xx 的代价完全不同，
    因此重试上限要分开算。
    """
    if isinstance(exc, TimeoutError):
        return True
    if type(exc).__name__ in {"APITimeoutError", "Timeout", "ConnectTimeout",
                              "ReadTimeout", "TimeoutException", "ReadTimeoutError"}:
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text or "超时" in text


def _is_retryable_sdk_error(exc: Exception) -> bool:
    """SDK 异常是否值得重试 —— 与 HTTP 后端的 RETRYABLE_STATUS 对齐。

    HTTP 路径会重试 429/5xx 与网络异常；SDK 路径此前**一次都不重试**，
    于是平台上一次网关抖动就直接判该需求失败。
    """
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    name = type(exc).__name__
    return name in {
        "APIConnectionError", "APITimeoutError", "InternalServerError",
        "RateLimitError", "Timeout", "ConnectTimeout", "ReadTimeout",
        "ConnectionError", "RemoteDisconnected",
    } or isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _extract_content(data: dict[str, Any]) -> str:
    """从 Chat Completions 响应中取出正文，兼容 reasoning 字段。"""
    choices = data.get("choices") or []
    if not choices:
        raise ModelCallError(f"响应中没有 choices: {json.dumps(data, ensure_ascii=False)[:300]}")
    choice = choices[0]
    finish = choice.get("finish_reason")
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # 部分服务端返回分段数组
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        if message.get("reasoning_content"):
            raise ModelCallError(
                f"模型只返回了 reasoning_content，正文为空（finish_reason={finish}）"
            )
        raise ModelCallError(f"模型返回空内容（finish_reason={finish}）")
    usage = data.get("usage") or {}
    reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning_tokens:
        completion = usage.get("completion_tokens") or 0
        if completion and reasoning_tokens >= completion:
            logger.warning(
                "本次调用预算全部消耗在推理上（reasoning=%s/completion=%s）", reasoning_tokens, completion
            )
    if finish == "length":
        logger.warning("响应因 max_tokens 被截断（finish_reason=length），JSON 可能不完整")
    return str(content)


_RAW_IN_LOG = 2000
_RAW_IN_EXCEPTION = 500
_RAW_TAIL = 400

# 重试时追加的修复指令。原样重发等于掷骰子：输出被 max_tokens 截断时，
# 同一 prompt 会稳定地再次被截断。必须改变输出规模，而不是碰运气。
_JSON_RETRY_NOTE = """

⚠️ 上一次调用失败了：你的输出不是合法 JSON。最常见的原因是**输出过长被 max_tokens 截断**。
本次必须显著精简，宁可短而完整，不可长而被截断：
- steps 最多 4 条，每条不超过 120 字符；
- interfaces 最多 3 个，content 不超过 150 字符；
- tests 最多 4 条，intent 不超过 100 字符；
- 最后一个字符必须是 }，JSON 必须完整闭合。"""


def _json_truncation_note(text: str) -> str:
    """粗判 JSON 是否被 max_tokens 截断。

    为什么不能只看前 N 字符：正文字符串本身可能就很长，截取窗口**必然**停在
    半个字符串里，看上去像截断但其实未必。实测上一轮日志就是这样被误读的——
    只打了前 2000 字符，无法区分「模型输出被截断」与「日志窗口截断」。

    正确做法是扫描整体，看括号与字符串字面量是否闭合。
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    if in_string:
        return "疑似被截断：字符串字面量未闭合（结尾停在半个字符串里）"
    if depth > 0:
        return f"疑似被截断：还有 {depth} 个 '{{' 未闭合"
    if depth < 0:
        return f"结构异常：多出 {-depth} 个 '}}'"
    return "括号与字符串均已闭合（非截断，另有格式问题）"


def _describe_raw(text: str) -> str:
    """原始输出诊断：总长 + 截断判据 + 头 + 尾。

    头尾都要给：只看头无法判断是否截断，只看尾无法知道模型在答什么。
    """
    body = (text or "").strip()
    if not body:
        return "（空响应）"
    return (
        f"总长度={len(body)} 字符；{_json_truncation_note(body)}\n"
        f"--- 前 {_RAW_IN_LOG} 字符 ---\n{body[:_RAW_IN_LOG]}\n"
        f"--- 末尾 {_RAW_TAIL} 字符 ---\n{body[-_RAW_TAIL:]}"
    )


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象。

    实测模型会返回多种形态（HTTP 200 但内容不是 JSON），逐级回退：
      1. 纯 JSON
      2. ```json 围栏（可带前后解释文字）
      3. 前后混有解释文字 -> 第一个 { 到最后一个 }
      4. 顶层是数组     -> 第一个 [ 到最后一个 ]，取首个对象元素

    全部失败时把**原始输出**带进异常。否则下次仍然不知道模型返回了什么，
    只能看到"不是合法 JSON"——这条正是上一轮定位 JSON 问题时的最大障碍。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("模型返回空响应（content 为空）")

    candidates: list[str] = [cleaned]

    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())

    brace_start, brace_end = cleaned.find("{"), cleaned.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(cleaned[brace_start : brace_end + 1])

    bracket_start, bracket_end = cleaned.find("["), cleaned.rfind("]")
    if bracket_start != -1 and bracket_end > bracket_start:
        candidates.append(cleaned[bracket_start : bracket_end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        # 模型有时把对象包在数组里；取首个对象元素，而不是直接判失败
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]

    raise ValueError(
        f"模型未返回可解析的 JSON（{_json_truncation_note(cleaned)}，总长 {len(cleaned)} 字符）。"
        f"原始输出前 {_RAW_IN_EXCEPTION} 字符:\n{cleaned[:_RAW_IN_EXCEPTION]}"
    )
