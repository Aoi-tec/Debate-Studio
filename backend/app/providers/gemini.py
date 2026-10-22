"""Gemini generateContent streaming adapter (``streamGenerateContent?alt=sse``)."""
from __future__ import annotations

from typing import AsyncIterator
from urllib.parse import quote

import httpx

from app.providers import base as _pb

from app.providers.base import (
    LLMEvent, LLMRequest, ProviderAdapter, ProviderError, Usage, http_error,
    iter_sse, loads, map_transport_error, merge_roles, timeout_for,
)


class GeminiAdapter(ProviderAdapter):
    protocol = "gemini_generate_content"

    async def stream_completion(self, req: LLMRequest, *, base_url: str | None, api_key: str | None) -> AsyncIterator[LLMEvent]:
        base = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["x-goog-api-key"] = api_key
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in merge_roles(req.messages)]
        gen: dict = {"maxOutputTokens": req.max_output_tokens}
        if req.temperature is not None:
            gen["temperature"] = req.temperature
        if req.json_mode:
            gen["responseMimeType"] = "application/json"
        body = {"systemInstruction": {"parts": [{"text": req.system}]}, "contents": contents, "generationConfig": gen}
        url = f"{base}/models/{quote(req.model, safe='')}:streamGenerateContent?alt=sse"
        try:
            async with _pb.client(timeout_for(req)) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")
                        yield LLMEvent("ERROR", error=http_error(resp, text))
                        return
                    yield LLMEvent("START")
                    finish = None
                    usage = None
                    async for _ev, data in iter_sse(resp):
                        obj = loads(data)
                        if "error" in obj:
                            yield LLMEvent("ERROR", error=ProviderError("transient", str(obj["error"])))
                            return
                        for cand in obj.get("candidates") or []:
                            for part in (cand.get("content") or {}).get("parts") or []:
                                if part.get("text") and not part.get("thought"):
                                    yield LLMEvent("TEXT_DELTA", text=part["text"])
                            if cand.get("finishReason"):
                                finish = cand["finishReason"]
                        um = obj.get("usageMetadata")
                        if um:
                            thoughts = um.get("thoughtsTokenCount")
                            usage = Usage(
                                input_tokens=int(um.get("promptTokenCount") or 0),
                                output_tokens=int(um.get("candidatesTokenCount") or 0) + int(thoughts or 0),
                                cached_input_tokens=um.get("cachedContentTokenCount"),
                                reasoning_tokens=thoughts,
                            )
                    if usage:
                        yield LLMEvent("USAGE", usage=usage)
                    if not finish:
                        yield LLMEvent("ERROR", error=ProviderError("stream_cut", "finishReason を受信せずに終了しました"))
                        return
                    yield LLMEvent("FINISHED", finish_reason=finish)
        except httpx.HTTPError as e:
            yield LLMEvent("ERROR", error=map_transport_error(e))
