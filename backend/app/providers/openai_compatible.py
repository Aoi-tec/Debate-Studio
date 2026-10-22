"""OpenAI Chat Completions (and compatible servers) streaming adapter."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from app.providers import base as _pb

from app.providers.base import (
    LLMEvent, LLMRequest, ProviderAdapter, ProviderError, Usage, UsageSnapshot,
    http_error, iter_sse, loads, map_transport_error, timeout_for,
)

OPENAI_HOST = "api.openai.com"


class OpenAICompatibleAdapter(ProviderAdapter):
    protocol = "openai_chat_compatible"

    def _payload(self, req: LLMRequest, base_url: str, *, include_usage: bool, drop: set[str]) -> dict:
        msgs = [{"role": "system", "content": req.system}] + [
            {"role": m["role"] if m["role"] in ("user", "assistant") else "user", "content": m["content"]}
            for m in req.messages
        ]
        body: dict = {"model": req.model, "messages": msgs, "stream": True}
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        if req.temperature is not None and "temperature" not in drop:
            body["temperature"] = req.temperature
        key = "max_completion_tokens" if OPENAI_HOST in base_url else "max_tokens"
        if key not in drop:
            body[key] = req.max_output_tokens
        if req.json_mode and "response_format" not in drop:
            body["response_format"] = {"type": "json_object"}
        return body

    async def stream_completion(self, req: LLMRequest, *, base_url: str | None, api_key: str | None) -> AsyncIterator[LLMEvent]:
        base = (base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        include_usage = True
        drop: set[str] = set()
        for _attempt in range(4):  # parameter-compat retries only (not error retries)
            body = self._payload(req, base, include_usage=include_usage, drop=drop)
            try:
                async with _pb.client(timeout_for(req)) as client:
                    async with client.stream("POST", f"{base}/chat/completions", json=body, headers=headers) as resp:
                        if resp.status_code >= 400:
                            text = (await resp.aread()).decode("utf-8", "replace")
                            err = http_error(resp, text)
                            low = text.lower()
                            if err.kind == "bad_request":
                                if include_usage and "stream_options" in low:
                                    include_usage = False
                                    continue
                                if "temperature" in low and "temperature" not in drop:
                                    drop.add("temperature")
                                    continue
                                if "response_format" in low and "response_format" not in drop:
                                    drop.add("response_format")
                                    continue
                            yield LLMEvent("ERROR", error=err)
                            return
                        yield LLMEvent("START")
                        finish = None
                        usage: Usage | None = None
                        async for _ev, data in iter_sse(resp):
                            if data.strip() == "[DONE]":
                                break
                            obj = loads(data)
                            if "error" in obj:
                                yield LLMEvent("ERROR", error=ProviderError("transient", str(obj["error"])))
                                return
                            for ch in obj.get("choices") or []:
                                delta = (ch.get("delta") or {}).get("content")
                                if delta:
                                    yield LLMEvent("TEXT_DELTA", text=delta)
                                if ch.get("finish_reason"):
                                    finish = ch["finish_reason"]
                            u = obj.get("usage")
                            if u:
                                usage = Usage(
                                    input_tokens=int(u.get("prompt_tokens") or 0),
                                    output_tokens=int(u.get("completion_tokens") or 0),
                                    cached_input_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                                    reasoning_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                                    source="reported",
                                )
                        if usage:
                            yield LLMEvent("USAGE", usage=usage)
                        if finish is None:
                            yield LLMEvent("ERROR", error=ProviderError("stream_cut", "完了シグナル(finish_reason)を受信せずに終了しました"))
                            return
                        yield LLMEvent("FINISHED", finish_reason=finish)
                        return
            except httpx.HTTPError as e:
                yield LLMEvent("ERROR", error=map_transport_error(e))
                return
        yield LLMEvent("ERROR", error=ProviderError("bad_request", "パラメータ互換性の調整に失敗しました"))

    async def read_usage_snapshot(self, *, base_url: str | None, admin_key: str | None, start_unix: int) -> UsageSnapshot:
        base = (base_url or "https://api.openai.com/v1").rstrip("/")
        if OPENAI_HOST not in base:
            return UsageSnapshot(status="unavailable", detail="OpenAI互換サーバーの使用量APIは未対応")
        if not admin_key:
            return UsageSnapshot(status="unavailable", detail="Admin API Key 未設定（組織Usage APIには管理者キーが必要）")
        total = 0
        page = None
        try:
            async with _pb.client(30) as client:
                for _ in range(20):
                    params = {"start_time": start_unix, "bucket_width": "1d", "limit": 31}
                    if page:
                        params["page"] = page
                    r = await client.get(f"{base}/organization/usage/completions", params=params,
                                         headers={"Authorization": f"Bearer {admin_key}"})
                    if r.status_code >= 400:
                        return UsageSnapshot(status="unavailable", detail=str(http_error(r, r.text)))
                    obj = r.json()
                    for bucket in obj.get("data", []):
                        for res in bucket.get("results", []):
                            total += int(res.get("input_tokens") or 0) + int(res.get("output_tokens") or 0)
                    if not obj.get("has_more"):
                        break
                    page = obj.get("next_page")
        except httpx.HTTPError as e:
            return UsageSnapshot(status="unavailable", detail=str(map_transport_error(e)))
        return UsageSnapshot(status="readable", tokens=total, window=f"since {start_unix}",
                             fetched_at=datetime.now(timezone.utc).isoformat(),
                             detail="OpenAI organization usage/completions（input+output）")
