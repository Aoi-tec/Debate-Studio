"""OpenAI Responses API streaming adapter."""
from __future__ import annotations

from typing import AsyncIterator

import httpx

from app.providers import base as _pb

from app.providers.base import (
    LLMEvent, LLMRequest, ProviderError, Usage, http_error, iter_sse, loads,
    map_transport_error, timeout_for,
)
from app.providers.openai_compatible import OpenAICompatibleAdapter


class OpenAIResponsesAdapter(OpenAICompatibleAdapter):
    protocol = "openai_responses"

    async def stream_completion(self, req: LLMRequest, *, base_url: str | None, api_key: str | None) -> AsyncIterator[LLMEvent]:
        base = (base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        drop: set[str] = set()
        for _ in range(3):
            body: dict = {
                "model": req.model,
                "instructions": req.system,
                "input": [{"role": m["role"] if m["role"] in ("user", "assistant") else "user", "content": m["content"]}
                          for m in req.messages],
                "stream": True,
                "max_output_tokens": req.max_output_tokens,
            }
            if req.temperature is not None and "temperature" not in drop:
                body["temperature"] = req.temperature
            if req.json_mode and "text" not in drop:
                body["text"] = {"format": {"type": "json_object"}}
            try:
                async with _pb.client(timeout_for(req)) as client:
                    async with client.stream("POST", f"{base}/responses", json=body, headers=headers) as resp:
                        if resp.status_code >= 400:
                            text = (await resp.aread()).decode("utf-8", "replace")
                            err = http_error(resp, text)
                            if err.kind == "bad_request" and "temperature" in text.lower() and "temperature" not in drop:
                                drop.add("temperature")
                                continue
                            if err.kind == "bad_request" and "text.format" in text.lower() and "text" not in drop:
                                drop.add("text")
                                continue
                            yield LLMEvent("ERROR", error=err)
                            return
                        yield LLMEvent("START")
                        async for ev, data in iter_sse(resp):
                            obj = loads(data)
                            etype = obj.get("type") or ev
                            if etype == "response.output_text.delta":
                                if obj.get("delta"):
                                    yield LLMEvent("TEXT_DELTA", text=obj["delta"])
                            elif etype in ("response.completed", "response.incomplete"):
                                r = obj.get("response") or {}
                                u = r.get("usage") or {}
                                if u:
                                    yield LLMEvent("USAGE", usage=Usage(
                                        input_tokens=int(u.get("input_tokens") or 0),
                                        output_tokens=int(u.get("output_tokens") or 0),
                                        cached_input_tokens=(u.get("input_tokens_details") or {}).get("cached_tokens"),
                                        reasoning_tokens=(u.get("output_tokens_details") or {}).get("reasoning_tokens"),
                                    ))
                                reason = "stop" if etype == "response.completed" else (
                                    ((r.get("incomplete_details") or {}).get("reason")) or "incomplete")
                                yield LLMEvent("FINISHED", finish_reason=reason)
                                return
                            elif etype in ("response.failed", "error"):
                                msg = str((obj.get("response") or {}).get("error") or obj.get("message") or obj)
                                yield LLMEvent("ERROR", error=ProviderError("transient", msg))
                                return
                        yield LLMEvent("ERROR", error=ProviderError("stream_cut", "response.completed を受信せずに終了しました"))
                        return
            except httpx.HTTPError as e:
                yield LLMEvent("ERROR", error=map_transport_error(e))
                return
        yield LLMEvent("ERROR", error=ProviderError("bad_request", "パラメータ互換性の調整に失敗しました"))
