"""ProviderAdapter contract (DESIGN §10).

Every adapter normalises its vendor stream to ``START / TEXT_DELTA / USAGE /
FINISHED / ERROR``. A stream that ends without a vendor completion signal is an
ERROR(stream_cut), never FINISHED.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Optional

import httpx

from app.core.redact import redact

EventKind = Literal["START", "TEXT_DELTA", "THINKING_DELTA", "USAGE", "FINISHED", "ERROR"]
ErrorKind = Literal["auth", "rate_limit", "transient", "bad_request", "timeout", "stream_cut", "unsupported", "config",
                    "context_overflow"]
# finish reasons meaning "the output cap was hit" across vendors
TRUNCATED_FINISH = {"length", "max_tokens", "MAX_TOKENS", "max_output_tokens", "incomplete"}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    source: Literal["reported", "estimated", "unavailable"] = "reported"

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ProviderError(Exception):
    kind: ErrorKind
    message: str
    status: Optional[int] = None
    retry_after: Optional[float] = None

    def __post_init__(self) -> None:
        self.message = redact(self.message)[:500]

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


@dataclass
class LLMEvent:
    kind: EventKind
    text: str = ""
    usage: Optional[Usage] = None
    finish_reason: Optional[str] = None
    error: Optional[ProviderError] = None


@dataclass
class LLMRequest:
    model: str
    system: str
    messages: list[dict]
    temperature: Optional[float] = 0.7
    max_output_tokens: int = 800
    timeout_s: float = 120.0
    json_mode: bool = False
    # thinking control (set by the runner from the connection's ReasoningConfig)
    reasoning_effort: Optional[str] = None  # None/provider_default → send nothing
    thinking_budget: int = 0  # tokens reserved for thinking inside max_output_tokens
    metadata: dict[str, Any] = field(default_factory=dict)  # NOT sent to vendors

    def to_audit_dict(self) -> dict:
        """What is actually sent (used for payload inspection tests)."""
        return {"model": self.model, "system": self.system, "messages": self.messages,
                "max_output_tokens": self.max_output_tokens}


@dataclass
class UsageSnapshot:
    status: Literal["readable", "unavailable", "stale"]
    tokens: Optional[int] = None
    window: Optional[str] = None
    fetched_at: Optional[str] = None
    detail: str = ""


class ProviderAdapter:
    protocol: str = "base"

    async def stream_completion(self, req: LLMRequest, *, base_url: str | None, api_key: str | None) -> AsyncIterator[LLMEvent]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def read_usage_snapshot(self, *, base_url: str | None, admin_key: str | None, start_unix: int) -> UsageSnapshot:
        return UsageSnapshot(status="unavailable", detail="この接続方式は使用量APIに未対応です")


# ---------------------------------------------------------------- helpers
def http_error(resp: httpx.Response, body: str) -> ProviderError:
    st = resp.status_code
    ra = resp.headers.get("retry-after")
    retry_after = None
    if ra:
        try:
            retry_after = float(ra)
        except ValueError:
            retry_after = None
    msg = f"HTTP {st}: {body[:300]}"
    low = body.lower()
    if st in (400, 413, 422) and ("context" in low or "prompt is too long" in low or "too many tokens" in low) and (
            "length" in low or "window" in low or "exceed" in low or "too long" in low or "maximum" in low):
        return ProviderError("context_overflow", msg, st)
    if st in (401, 403):
        return ProviderError("auth", msg, st)
    if st == 429:
        return ProviderError("rate_limit", msg, st, retry_after)
    if st >= 500 or st in (408, 409, 529):
        return ProviderError("transient", msg, st, retry_after)
    return ProviderError("bad_request", msg, st)


async def iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    """Yield (event, data) pairs from an SSE response."""
    event: str | None = None
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield event, "\n".join(data_lines)


def loads(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def merge_roles(messages: list[dict], assistant_name: str = "assistant") -> list[dict]:
    """Merge consecutive same-role messages; ensure first message is from user."""
    out: list[dict] = []
    for m in messages:
        role = "user" if m["role"] not in ("assistant",) else assistant_name
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + m["content"]
        else:
            out.append({"role": role, "content": m["content"]})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(討論の続きです)"})
    return out


# Tests may inject an ``httpx.MockTransport`` here; production leaves it None.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


def client(timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=HTTP_TRANSPORT)


def timeout_for(req: LLMRequest) -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=req.timeout_s, write=30.0, pool=10.0)


def map_transport_error(e: Exception) -> ProviderError:
    if isinstance(e, httpx.TimeoutException):
        return ProviderError("timeout", f"応答タイムアウト: {type(e).__name__}")
    if isinstance(e, (httpx.RemoteProtocolError, httpx.ReadError)):
        return ProviderError("stream_cut", f"ストリーム切断: {type(e).__name__}")
    if isinstance(e, httpx.ConnectError):
        return ProviderError("transient", f"接続できません: {type(e).__name__}")
    return ProviderError("transient", f"{type(e).__name__}: {e}")


class ThinkFilter:
    """Streams visible text while removing inline ``<think>…</think>`` blocks.

    Qwen/DeepSeek-style servers sometimes put the model's private reasoning inside
    the normal content. That text must not be shown nor passed to other agents
    (DESIGN 3.1: internal reasoning is not displayed). Feed chunks with ``feed``;
    it returns (visible, thinking). ``final_clean`` handles an orphan ``</think>``
    (templates that omit the opening tag).
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.inside = False
        self.buf = ""

    def feed(self, chunk: str) -> tuple[str, str]:
        self.buf += chunk
        vis, think = [], []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                (think if self.inside else vis).append(self.buf[:i])
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            # keep a possible partial tag at the end of the buffer
            keep = 0
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-k:]):
                    keep = k
                    break
            out, self.buf = self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
            (think if self.inside else vis).append(out)
            break
        return "".join(vis), "".join(think)

    def flush(self) -> tuple[str, str]:
        rest, self.buf = self.buf, ""
        return ("", rest) if self.inside else (rest, "")

    @classmethod
    def final_clean(cls, text: str) -> str:
        if cls.CLOSE in text and cls.OPEN not in text:
            text = text.split(cls.CLOSE, 1)[1]
        while cls.OPEN in text:
            a = text.find(cls.OPEN)
            b = text.find(cls.CLOSE, a)
            text = text[:a] + ("" if b < 0 else text[b + len(cls.CLOSE):])
        return text.strip()
