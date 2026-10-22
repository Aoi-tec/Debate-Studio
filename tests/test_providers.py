"""ProviderAdapter normalisation against mocked vendor streams (no network, no billing)."""
import json

import httpx
import pytest

from app.core.redact import register_secret
from app.providers import base
from app.providers.anthropic import AnthropicAdapter
from app.providers.base import LLMRequest
from app.providers.gemini import GeminiAdapter
from app.providers.openai_compatible import OpenAICompatibleAdapter
from app.providers.openai_responses import OpenAIResponsesAdapter

pytestmark = pytest.mark.anyio
KEY = "sk-unit-TESTKEY-abcdefghijkl"


def sse(*objs, done=False, events=None):
    out = []
    for i, o in enumerate(objs):
        ev = f"event: {events[i]}\n" if events else ""
        out.append(f"{ev}data: {json.dumps(o, ensure_ascii=False)}\n\n")
    if done:
        out.append("data: [DONE]\n\n")
    return "".join(out).encode()


@pytest.fixture
def mock():
    seen = {}

    def install(handler):
        def h(request: httpx.Request):
            seen["request"] = request
            seen["body"] = json.loads(request.content or b"{}")
            return handler(request)
        base.HTTP_TRANSPORT = httpx.MockTransport(h)
        return seen
    yield install
    base.HTTP_TRANSPORT = None


async def collect(adapter, req, url=None, key=KEY):
    return [ev async for ev in adapter.stream_completion(req, base_url=url, api_key=key)]


REQ = LLMRequest(model="m1", system="sys", messages=[{"role": "user", "content": "hi"}], max_output_tokens=50)


async def test_openai_chat_stream_usage_and_finish(mock):
    seen = mock(lambda r: httpx.Response(200, content=sse(
        {"choices": [{"delta": {"content": "こん"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "にちは"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                                  "prompt_tokens_details": {"cached_tokens": 4},
                                  "completion_tokens_details": {"reasoning_tokens": 1}}}, done=True)))
    evs = await collect(OpenAICompatibleAdapter(), REQ, "https://api.openai.com/v1")
    kinds = [e.kind for e in evs]
    assert kinds == ["START", "TEXT_DELTA", "TEXT_DELTA", "USAGE", "FINISHED"]
    u = evs[3].usage
    assert (u.input_tokens, u.output_tokens, u.cached_input_tokens, u.reasoning_tokens) == (10, 3, 4, 1)
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert "max_completion_tokens" in seen["body"] and "tools" not in seen["body"]
    assert seen["request"].headers["authorization"] == f"Bearer {KEY}"


async def test_openai_stream_without_finish_is_cut(mock):
    mock(lambda r: httpx.Response(200, content=sse({"choices": [{"delta": {"content": "途中"}}]})))
    evs = await collect(OpenAICompatibleAdapter(), REQ, "http://localhost:11434/v1")
    assert evs[-1].kind == "ERROR" and evs[-1].error.kind == "stream_cut"
    assert "FINISHED" not in [e.kind for e in evs]


async def test_openai_compat_retries_without_stream_options(mock):
    calls = []

    def h(r):
        body = json.loads(r.content)
        calls.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": "unknown field stream_options"})
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}, done=True))
    mock(h)
    evs = await collect(OpenAICompatibleAdapter(), REQ, "http://localhost:11434/v1")
    assert evs[-1].kind == "FINISHED" and len(calls) == 2 and "max_tokens" in calls[1]


async def test_openai_errors_mapped_and_redacted(mock):
    register_secret(KEY)
    mock(lambda r: httpx.Response(429, headers={"retry-after": "7"}, json={"error": f"rate limited for key {KEY}"}))
    evs = await collect(OpenAICompatibleAdapter(), REQ)
    err = evs[-1].error
    assert err.kind == "rate_limit" and err.retry_after == 7.0 and KEY not in err.message
    mock(lambda r: httpx.Response(401, json={"error": "bad key"}))
    assert (await collect(OpenAICompatibleAdapter(), REQ))[-1].error.kind == "auth"
    mock(lambda r: httpx.Response(503, text="busy"))
    assert (await collect(OpenAICompatibleAdapter(), REQ))[-1].error.kind == "transient"


async def test_responses_api(mock):
    seen = mock(lambda r: httpx.Response(200, content=sse(
        {"type": "response.output_text.delta", "delta": "A"},
        {"type": "response.output_text.delta", "delta": "B"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 2,
                                                              "input_tokens_details": {"cached_tokens": 0}}}})))
    evs = await collect(OpenAIResponsesAdapter(), REQ)
    assert "".join(e.text for e in evs if e.kind == "TEXT_DELTA") == "AB"
    assert evs[-1].kind == "FINISHED" and evs[-2].usage.total == 7
    assert seen["body"]["instructions"] == "sys" and seen["request"].url.path.endswith("/responses")


async def test_anthropic_messages(mock):
    seen = mock(lambda r: httpx.Response(200, content=sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 8, "cache_read_input_tokens": 2, "output_tokens": 1}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "やあ"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}},
        {"type": "message_stop"})))
    evs = await collect(AnthropicAdapter(), REQ)
    u = [e for e in evs if e.kind == "USAGE"][0].usage
    assert (u.input_tokens, u.output_tokens, u.cached_input_tokens) == (10, 6, 2)
    assert evs[-1].kind == "FINISHED" and evs[-1].finish_reason == "end_turn"
    assert seen["request"].headers["x-api-key"] == KEY and seen["body"]["system"] == "sys"
    mock(lambda r: httpx.Response(200, content=sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}})))
    assert (await collect(AnthropicAdapter(), REQ))[-1].error.kind == "stream_cut"


async def test_gemini(mock):
    seen = mock(lambda r: httpx.Response(200, content=sse(
        {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "!"}]}, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2, "thoughtsTokenCount": 1}})))
    evs = await collect(GeminiAdapter(), REQ)
    assert "".join(e.text for e in evs if e.kind == "TEXT_DELTA") == "Hi!"
    u = [e for e in evs if e.kind == "USAGE"][0].usage
    assert (u.input_tokens, u.output_tokens, u.reasoning_tokens) == (4, 3, 1)
    assert ":streamGenerateContent" in str(seen["request"].url) and seen["request"].headers["x-goog-api-key"] == KEY
    assert "key=" not in str(seen["request"].url), "API key must not be placed in the URL"


async def test_usage_snapshot_openai_admin(mock):
    mock(lambda r: httpx.Response(200, json={"data": [{"results": [{"input_tokens": 9000, "output_tokens": 3000}]}], "has_more": False}))
    snap = await OpenAICompatibleAdapter().read_usage_snapshot(base_url=None, admin_key="sk-admin-x", start_unix=0)
    assert snap.status == "readable" and snap.tokens == 12_000
    snap = await OpenAICompatibleAdapter().read_usage_snapshot(base_url=None, admin_key=None, start_unix=0)
    assert snap.status == "unavailable"
    snap = await GeminiAdapter().read_usage_snapshot(base_url=None, admin_key="x", start_unix=0)
    assert snap.status == "unavailable", "unsupported providers never report zero usage"
