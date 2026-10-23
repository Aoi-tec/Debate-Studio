"""Anthropic Messages API streaming adapter."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from app.providers import base as _pb

from app.providers.base import (
    LLMEvent, LLMRequest, ProviderAdapter, ProviderError, Usage, UsageSnapshot,
    http_error, iter_sse, loads, map_transport_error, merge_roles, timeout_for,
)

API_VERSION = "2023-06-01"


class AnthropicAdapter(ProviderAdapter):
    protocol = "anthropic_messages"

    async def stream_completion(self, req: LLMRequest, *, base_url: str | None, api_key: str | None) -> AsyncIterator[LLMEvent]:
        base = (base_url or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        headers = {"Content-Type": "application/json", "anthropic-version": API_VERSION, "Accept": "text/event-stream"}
        if api_key:
            headers["x-api-key"] = api_key
        system = req.system
        if req.json_mode:
            system += "\n\n出力は有効なJSONオブジェクトのみとし、前後に説明文を付けないこと。"
        body: dict = {
            "model": req.model,
            "system": system,
            "messages": merge_roles(req.messages),
            "max_tokens": req.max_output_tokens,
            "stream": True,
        }
        eff = req.reasoning_effort
        if eff and eff not in ("provider_default", "off") and req.thinking_budget > 0:
            # extended thinking: budget >= 1024 and strictly below max_tokens; temperature must stay default
            budget = max(1024, req.thinking_budget)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body["max_tokens"] = max(req.max_output_tokens, budget + 1024)
        elif req.temperature is not None:
            body["temperature"] = min(req.temperature, 1.0)
        try:
            async with _pb.client(timeout_for(req)) as client:
                async with client.stream("POST", f"{base}/v1/messages", json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")
                        yield LLMEvent("ERROR", error=http_error(resp, text))
                        return
                    yield LLMEvent("START")
                    inp = cached = 0
                    out = 0
                    stop = None
                    stopped = False
                    async for ev, data in iter_sse(resp):
                        obj = loads(data)
                        t = obj.get("type") or ev
                        if t == "message_start":
                            u = (obj.get("message") or {}).get("usage") or {}
                            cached = int(u.get("cache_read_input_tokens") or 0)
                            inp = int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0)
                            out = int(u.get("output_tokens") or 0)
                        elif t == "content_block_delta":
                            d = obj.get("delta") or {}
                            if d.get("type") == "text_delta" and d.get("text"):
                                yield LLMEvent("TEXT_DELTA", text=d["text"])
                            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                                yield LLMEvent("THINKING_DELTA", text=d["thinking"])
                        elif t == "message_delta":
                            stop = (obj.get("delta") or {}).get("stop_reason") or stop
                            u = obj.get("usage") or {}
                            if "output_tokens" in u:
                                out = int(u["output_tokens"])
                        elif t == "message_stop":
                            stopped = True
                            break
                        elif t == "error":
                            err = obj.get("error") or {}
                            kind = "rate_limit" if err.get("type") == "rate_limit_error" else "transient"
                            yield LLMEvent("ERROR", error=ProviderError(kind, str(err)))
                            return
                    if inp or out:
                        yield LLMEvent("USAGE", usage=Usage(input_tokens=inp, output_tokens=out,
                                                            cached_input_tokens=cached or None))
                    if not stopped:
                        yield LLMEvent("ERROR", error=ProviderError("stream_cut", "message_stop を受信せずに終了しました"))
                        return
                    yield LLMEvent("FINISHED", finish_reason=stop or "end_turn")
        except httpx.HTTPError as e:
            yield LLMEvent("ERROR", error=map_transport_error(e))

    async def read_usage_snapshot(self, *, base_url: str | None, admin_key: str | None, start_unix: int) -> UsageSnapshot:
        if not admin_key:
            return UsageSnapshot(status="unavailable", detail="Admin API Key 未設定（Usage and Cost APIには管理者キーが必要）")
        base = (base_url or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        start = datetime.fromtimestamp(start_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        total = 0
        page = None
        try:
            async with _pb.client(30) as client:
                for _ in range(20):
                    params = {"starting_at": start, "bucket_width": "1d", "limit": 31}
                    if page:
                        params["page"] = page
                    r = await client.get(f"{base}/v1/organizations/usage_report/messages", params=params,
                                         headers={"x-api-key": admin_key, "anthropic-version": API_VERSION})
                    if r.status_code >= 400:
                        return UsageSnapshot(status="unavailable", detail=str(http_error(r, r.text)))
                    obj = r.json()
                    for bucket in obj.get("data", []):
                        for res in bucket.get("results", []):
                            cc = res.get("cache_creation") or {}
                            total += int(res.get("uncached_input_tokens") or 0)
                            total += int(res.get("cache_read_input_tokens") or 0)
                            total += sum(int(v or 0) for v in cc.values()) if isinstance(cc, dict) else 0
                            total += int(res.get("output_tokens") or 0)
                    if not obj.get("has_more"):
                        break
                    page = obj.get("next_page")
        except httpx.HTTPError as e:
            return UsageSnapshot(status="unavailable", detail=str(map_transport_error(e)))
        return UsageSnapshot(status="readable", tokens=total, window=f"since {start}",
                             fetched_at=datetime.now(timezone.utc).isoformat(),
                             detail="Anthropic usage_report/messages（集計は数分以上遅延し得る）")
