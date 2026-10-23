"""FakeProvider: deterministic, offline, scriptable LLM for tests and demos.

Real adapters ignore ``LLMRequest.metadata``; the fake uses ``metadata['fake_hint']``
so that it can produce schema-valid chair JSON without a model.
Scripts (tests) can force specific outputs, errors, partial streams or
``context_request`` blocks.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.core.tokens import estimate_tokens
from app.providers.base import LLMEvent, LLMRequest, ProviderAdapter, ProviderError, Usage, UsageSnapshot


@dataclass
class FakeRule:
    match: dict[str, Any]
    response: Any
    times: int = 1


@dataclass
class FakeScript:
    rules: list[FakeRule] = field(default_factory=list)
    delay_s: float = 0.0
    chunk_chars: int = 12
    calls: list[dict] = field(default_factory=list)  # every request actually "sent"
    usage_snapshot_tokens: int | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, response: Any, times: int = 1, **match: Any) -> None:
        self.rules.append(FakeRule(match=match, response=response, times=times))

    def take(self, meta: dict) -> Any:
        with self._lock:
            for r in self.rules:
                if r.times <= 0:
                    continue
                if all(meta.get(k) == v for k, v in r.match.items()):
                    r.times -= 1
                    return r.response
        return None


class FakeAdapter(ProviderAdapter):
    protocol = "fake"

    def __init__(self, script: FakeScript | None = None):
        self.script = script or FakeScript()

    async def stream_completion(self, req: LLMRequest, *, base_url: str | None, api_key: str | None) -> AsyncIterator[LLMEvent]:
        meta = dict(req.metadata)
        self.script.calls.append({"meta": meta, "payload": req.to_audit_dict(), "model": req.model,
                                  "reasoning_effort": req.reasoning_effort, "thinking_budget": req.thinking_budget})
        scripted = self.script.take(meta)
        yield LLMEvent("START")
        partial_error: ProviderError | None = None
        thinking = ""
        if isinstance(scripted, dict) and "thinking" in scripted:
            thinking = scripted["thinking"]
            scripted = scripted.get("text", "") if "context_request" not in scripted else {
                k: v for k, v in scripted.items() if k != "thinking"}
            if scripted == "":
                scripted = {"text": ""}
        if isinstance(scripted, dict) and "error" in scripted:
            text = scripted.get("partial", "")
            partial_error = ProviderError(scripted["error"], scripted.get("message", "fake error"),
                                          retry_after=scripted.get("retry_after"))
            if not text:
                yield LLMEvent("ERROR", error=partial_error)
                return
        elif isinstance(scripted, str):
            text = scripted
        elif isinstance(scripted, dict) and "text" in scripted:
            text = scripted["text"]
            if "context_request" in scripted:
                text += "\n\n```context_request\n" + json.dumps(scripted["context_request"], ensure_ascii=False) + "\n```"
        elif callable(scripted):
            text = scripted(req)
        else:
            text = self._default(req)
            if req.reasoning_effort in ("low", "medium", "high") and not thinking:
                # demo: simulate private reasoning proportional to the effort (never shown in the UI)
                thinking = "（擬似思考）前提と反論を検討する。" * {"low": 8, "medium": 20, "high": 40}[req.reasoning_effort]
        step = max(1, self.script.chunk_chars)
        out_tokens = 0
        for i in range(0, len(thinking), step):  # thinking consumes the same output cap
            piece = thinking[i:i + step]
            out_tokens += estimate_tokens(piece)
            if req.max_output_tokens and out_tokens > req.max_output_tokens:
                yield LLMEvent("USAGE", usage=self._usage(req, thinking[:i]))
                yield LLMEvent("FINISHED", finish_reason="length")
                return
            await asyncio.sleep(0)
            yield LLMEvent("THINKING_DELTA", text=piece)
        for i in range(0, len(text), step):
            piece = text[i:i + step]
            out_tokens += estimate_tokens(piece)
            if req.max_output_tokens and out_tokens > req.max_output_tokens:
                yield LLMEvent("USAGE", usage=self._usage(req, thinking + text[:i]))
                yield LLMEvent("FINISHED", finish_reason="length")
                return
            if self.script.delay_s:
                await asyncio.sleep(self.script.delay_s)
            else:
                await asyncio.sleep(0)
            yield LLMEvent("TEXT_DELTA", text=piece)
        if partial_error:
            yield LLMEvent("ERROR", error=partial_error)
            return
        yield LLMEvent("USAGE", usage=self._usage(req, thinking + text, estimate_tokens(thinking) if thinking else None))
        yield LLMEvent("FINISHED", finish_reason="stop")

    def _usage(self, req: LLMRequest, text: str, reasoning: int | None = None) -> Usage:
        inp = estimate_tokens(req.system) + sum(estimate_tokens(m["content"]) for m in req.messages)
        return Usage(input_tokens=inp, output_tokens=estimate_tokens(text), reasoning_tokens=reasoning, source="reported")

    async def read_usage_snapshot(self, *, base_url: str | None, admin_key: str | None, start_unix: int) -> UsageSnapshot:
        if self.script.usage_snapshot_tokens is None:
            return UsageSnapshot(status="unavailable", detail="fake: 使用量APIなし")
        return UsageSnapshot(status="readable", tokens=self.script.usage_snapshot_tokens, detail="fake snapshot")

    # ------------------------------------------------------------ defaults
    def _default(self, req: LLMRequest) -> str:
        meta = req.metadata
        hint = meta.get("fake_hint") or {}
        purpose = meta.get("purpose", "turn")
        if purpose == "chair_analysis":
            cands = hint.get("candidates", [])
            facts = []
            for c in cands[:6]:
                first = re.split(r"(?<=[。.!?\n])", c["text"].strip())[0].strip()[:160]
                if first:
                    facts.append({"claim": f"資料に「{first}」と記載がある", "evidence_ids": [c["evidence_id"]],
                                  "confidence": "document_explicit"})
            return json.dumps({
                "summary": f"添付資料{len({c.get('file_id') for c in cands})}件から議題に関係する記述を整理した。" if cands else "参照可能な資料記述はない。",
                "facts": facts,
                "assumptions": ["資料に記載のない事項は未確認"],
                "open_questions": [],
                "omitted_or_unreadable": hint.get("unreadable", []),
            }, ensure_ascii=False)
        if purpose == "evidence_check":
            cands = hint.get("candidates", [])
            if cands:
                c = cands[0]
                return json.dumps({"answered": True, "facts": [{
                    "claim": f"資料に「{c['text'].strip()[:120]}」と記載がある", "evidence_ids": [c["evidence_id"]],
                    "confidence": "document_explicit"}], "reason_code": None, "missing_fields": []}, ensure_ascii=False)
            return json.dumps({"answered": False, "facts": [], "reason_code": "NOT_FOUND",
                               "missing_fields": hint.get("required_evidence", []),
                               "question_for_user": "必要な情報が記載された資料を追加してください。",
                               "needed_materials": "該当事項が明記された資料"}, ensure_ascii=False)
        if purpose == "checkpoint":
            return json.dumps({
                "progress_summary": "各Agentの提案と反論が出揃い、主要な論点が整理された。",
                "resolved_issues": ["議題の目的と前提の共有"],
                "unresolved_issues": ["採用案の最終比較"],
                "stagnation_reasons": [],
                "missing_evidence": hint.get("missing_evidence", []),
                "next_round_questions": ["採用案のリスクをどう低減するか"],
                "agent_dissent": [],
                "new_information_since_last": True,
            }, ensure_ascii=False)
        if purpose == "final":
            return json.dumps({
                "adopted_proposal": "提案担当の案を条件付きで採用する。",
                "rationale": "批判・代替案の指摘を反映した修正版であるため。",
                "verified_findings": hint.get("verified_findings", []),
                "minority_opinions": ["代替案担当は別ルートの検討継続を主張"],
                "uncertainties": ["実運用での費用"],
                "next_actions": ["小規模に試行する"],
            }, ensure_ascii=False)
        if purpose == "compress":
            return "【要約】これまでの討論で、提案・批判・代替案が提示され、議長が論点を整理した。"
        if purpose == "quality_probe":
            ids = hint.get("evidence_ids", [])
            return json.dumps({"answer": "probe ok", "evidence_ids": ids[:1]}, ensure_ascii=False)
        # free text turns
        name = meta.get("agent_name", "Agent")
        role = meta.get("role_id", "")
        phase = meta.get("phase", "")
        ev = hint.get("evidence_ids", [])
        cite = f"（根拠: {ev[0]}）" if ev else ""
        if role == "chair":
            if phase == "OPENING":
                return f"議長です。議題を確認しました。各Agentは独立に提案してください。{cite}"
            return "議長として整理します。合意点: 目的の共有。対立点: 実装方式と費用。次は相互に反論してください。"
        base = {
            "proposer": f"{name}（提案）: 段階的に導入する案を提案します。長所は低リスク、短所は時間がかかる点です。{cite}",
            "critic": f"{name}（批判）: 前提となるデータの鮮度が未確認です。検証手順として小規模試験を提案します。{cite}",
            "alternative": f"{name}（代替案）: 外部サービス利用案も比較すべきです。工数は少ないが維持費が上がります。{cite}",
        }.get(role, f"{name}: 意見を述べます。{cite}")
        if (hint.get("demo_context_request") and role == "critic" and not hint.get("already_requested")):
            base += "\n\n```context_request\n" + json.dumps({
                "topic": "根拠確認", "question": "判断に必要な数値の適用日がいつか",
                "required_evidence": ["適用開始日"], "why_needed": "古い値だと結論が変わる",
                "criticality": "critical"}, ensure_ascii=False) + "\n```"
        return base
