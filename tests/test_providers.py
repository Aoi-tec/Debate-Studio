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


async def test_reasoning_params_and_thinking_stream_openai_compat(mock):
    """Strata-style local server: reasoning_effort sent, reasoning_content is thinking (not answer text)."""
    seen = mock(lambda r: httpx.Response(200, content=sse(
        {"choices": [{"delta": {"reasoning_content": "まず前提を…"}}]},
        {"choices": [{"delta": {"content": "答え"}, "finish_reason": "stop"}]}, done=True)))
    req = LLMRequest(model="qwen", system="s", messages=[{"role": "user", "content": "q"}], max_output_tokens=5500,
                     reasoning_effort="medium", thinking_budget=4000)
    evs = await collect(OpenAICompatibleAdapter(), req, "http://127.0.0.1:8080/v1", key=None)
    assert [e.kind for e in evs if e.kind in ("THINKING_DELTA", "TEXT_DELTA")] == ["THINKING_DELTA", "TEXT_DELTA"]
    assert seen["body"]["reasoning_effort"] == "medium" and seen["body"]["max_tokens"] == 5500
    req_off = LLMRequest(model="qwen", system="s", messages=[{"role": "user", "content": "q"}], reasoning_effort="off")
    await collect(OpenAICompatibleAdapter(), req_off, "http://127.0.0.1:8080/v1", key=None)
    assert seen["body"]["reasoning_effort"] == "off"
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    await collect(OpenAICompatibleAdapter(), req_off, "https://api.openai.com/v1")
    assert seen["body"]["reasoning_effort"] == "none" and "chat_template_kwargs" not in seen["body"]


async def test_unknown_reasoning_param_is_dropped(mock):
    calls = []

    def h(r):
        body = json.loads(r.content)
        calls.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": "unrecognized field: reasoning_effort"})
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}, done=True))
    mock(h)
    req = LLMRequest(model="m", system="s", messages=[{"role": "user", "content": "q"}], reasoning_effort="high")
    evs = await collect(OpenAICompatibleAdapter(), req, "http://localhost:1234/v1", key=None)
    assert evs[-1].kind == "FINISHED" and "reasoning_effort" not in calls[-1]


async def test_context_overflow_is_classified(mock):
    mock(lambda r: httpx.Response(400, json={"error": "This model's maximum context length is 8192 tokens, requested 9000"}))
    assert (await collect(OpenAICompatibleAdapter(), REQ))[-1].error.kind == "context_overflow"


async def test_anthropic_extended_thinking(mock):
    seen = mock(lambda r: httpx.Response(200, content=sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "検討…"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "結論"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40}},
        {"type": "message_stop"})))
    req = LLMRequest(model="claude", system="s", messages=[{"role": "user", "content": "q"}], temperature=0.7,
                     max_output_tokens=5500, reasoning_effort="medium", thinking_budget=4000)
    evs = await collect(AnthropicAdapter(), req)
    assert seen["body"]["thinking"] == {"type": "enabled", "budget_tokens": 4000}
    assert "temperature" not in seen["body"] and seen["body"]["max_tokens"] > 4000
    assert [e.kind for e in evs if e.kind in ("THINKING_DELTA", "TEXT_DELTA")] == ["THINKING_DELTA", "TEXT_DELTA"]


async def test_gemini_thinking_budget(mock):
    seen = mock(lambda r: httpx.Response(200, content=sse(
        {"candidates": [{"content": {"parts": [{"text": "考え", "thought": True}, {"text": "答"}]}, "finishReason": "STOP"}]})))
    req = LLMRequest(model="g", system="s", messages=[{"role": "user", "content": "q"}], reasoning_effort="low", thinking_budget=2048)
    evs = await collect(GeminiAdapter(), req)
    assert seen["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 2048}
    assert [e.kind for e in evs if e.kind in ("THINKING_DELTA", "TEXT_DELTA")] == ["THINKING_DELTA", "TEXT_DELTA"]
