"""LLMRunner: one logical generation with routing, retries, streaming, usage,
mid-stream budget handoff and the downstream quality gate (DESIGN §7A, §10, §11).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.ids import new_id, now_iso
from app.core.tokens import estimate_tokens
from app.models.schemas import AgentConfig, ApiProfile, DebateSettings, RoleMeta
from app.providers.base import LLMRequest, ProviderError, Usage
from app.providers.registry import ProviderRegistry
from app.services.budget import BudgetGuard, BudgetPaused, ModelRouter, Reservation
from app.services.context import validate_no_file_or_tool_payload
from app.services.events import EventBus
from app.services.usage import UsageMeter
from app.storage.db import Database
from app.storage.secret_store import SecretStore


class TurnFailed(Exception):
    def __init__(self, message: str, kind: str = "error", agent_id: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.agent_id = agent_id


class QualityFailed(TurnFailed):
    pass


@dataclass
class SessionCtx:
    id: str
    topic: str
    agents: dict[str, AgentConfig]
    profiles: dict[str, ApiProfile]
    roles: dict[str, tuple[RoleMeta, str]]
    settings: DebateSettings
    last_tier: dict[str, str] = field(default_factory=dict)

    @property
    def chair(self) -> AgentConfig:
        return next(a for a in self.agents.values() if a.kind == "chair")

    def participants(self) -> list[AgentConfig]:
        return [a for a in self.agents.values() if a.kind == "participant" and a.enabled]

    def role_name(self, agent: AgentConfig) -> str:
        r = self.roles.get(agent.role_id)
        return r[0].name if r else agent.role_id

    def role_md(self, agent: AgentConfig) -> str:
        r = self.roles.get(agent.role_id)
        return r[1] if r else ""


@dataclass
class RunResult:
    text: str
    usage: Usage
    tier: str
    profile_id: str
    model: str
    message_id: Optional[str]
    finish_reason: str


_EV = re.compile(r"\bev_[0-9a-f]{6,}\b")


class LLMRunner:
    def __init__(self, db: Database, bus: EventBus, registry: ProviderRegistry, router: ModelRouter,
                 guard: BudgetGuard, meter: UsageMeter, secrets: SecretStore):
        self.db, self.bus, self.registry, self.router = db, bus, registry, router
        self.guard, self.meter, self.secrets = guard, meter, secrets
        self.backoff_base = 1.0
        self.max_backoff = 30.0

    def next_seq(self, session_id: str) -> int:
        r = self.db.one("SELECT COALESCE(MAX(seq),0) m FROM messages WHERE session_id=?", (session_id,))
        return int(r["m"]) + 1

    async def run(self, ctx: SessionCtx, agent: AgentConfig, *, purpose: str, build: Callable[[str, int], LLMRequest],
                  est_input: int, stream_message: Optional[dict] = None, needs_document_transfer: bool = False,
                  participant_guard: bool = False, forbidden_excerpts: list[str] | None = None,
                  allowed_evidence_ids: set[str] | None = None, json_required: bool = False,
                  validator: Callable[[str], Optional[str]] | None = None) -> RunResult:
        """``build(model, max_output)`` returns the request for the chosen model.

        ``stream_message`` (kind/phase/round/scope/packet_refs/meta) creates a chat bubble
        streamed via SSE; otherwise the call is silent (chair JSON work).
        """
        s = ctx.settings
        request_id = new_id("req")
        attempt = 0
        exclude: set[str] = set()
        quality_failures = 0
        transient_tries = 0
        prev_partial_id: str | None = None
        handoff_reason: str | None = None
        while True:
            attempt += 1
            choice = self.router.select(session_id=ctx.id, agent=agent, profiles=ctx.profiles, settings=s,
                                        est_input=est_input, desired_output=s.debate.max_output_tokens,
                                        needs_document_transfer=needs_document_transfer, exclude_tiers=exclude)
            prev_tier = ctx.last_tier.get(agent.id)
            if prev_tier and prev_tier != choice.tier:
                reason = handoff_reason or "; ".join(choice.skipped) or "上位Tierが利用不可"
                self.bus.publish(ctx.id, "model.fallback_started", {
                    "agent_id": agent.id, "from_tier": prev_tier, "to_tier": choice.tier, "reason": reason,
                    "to_model": choice.model})
            elif choice.skipped and not prev_tier and choice.tier != "primary_cloud":
                handoff_reason = "; ".join(choice.skipped)
            req = build(choice.model, choice.reservation.max_output)
            req.metadata.update({"purpose": purpose, "agent_id": agent.id, "agent_name": agent.name,
                                 "role_id": agent.role_id, "tier": choice.tier})
            if participant_guard:
                try:
                    validate_no_file_or_tool_payload(req, forbidden_excerpts)
                except Exception as e:
                    self.guard.settle(choice.reservation, 0)
                    self.bus.publish(ctx.id, "error", {"agent_id": agent.id, "message": f"送信前検査で遮断: {e}"})
                    raise TurnFailed(f"送信前検査で遮断: {e}", "payload_violation", agent.id) from e
            msg_id = None
            if stream_message is not None:
                msg_id = self._create_message(ctx, agent, choice, stream_message)
                self.bus.publish(ctx.id, "turn.started", {
                    "agent_id": agent.id, "message_id": msg_id, "round": stream_message.get("round"),
                    "phase": stream_message.get("phase"), "kind": stream_message.get("kind", "turn"),
                    "tier": choice.tier, "model_id": choice.model, "profile_id": choice.profile.id,
                    "attempt": attempt})
                if prev_tier and prev_tier != choice.tier:
                    hid = new_id("ho")
                    self.db.insert("model_handoffs", {
                        "id": hid, "session_id": ctx.id, "agent_id": agent.id, "from_tier": prev_tier,
                        "to_tier": choice.tier, "from_model": None, "to_model": choice.model,
                        "reason": handoff_reason or "; ".join(choice.skipped), "partial_message_id": prev_partial_id,
                        "new_message_id": msg_id, "quality": None, "created_at": now_iso()})
                    self.db.update("messages", {"id": msg_id}, {"handoff_id": hid})
                    self.bus.publish(ctx.id, "model.handoff_completed", {
                        "agent_id": agent.id, "handoff_id": hid, "from_tier": prev_tier, "to_tier": choice.tier,
                        "message_id": msg_id, "reason": handoff_reason or "; ".join(choice.skipped)})
            ctx.last_tier[agent.id] = choice.tier
            outcome = await self._stream(ctx, agent, choice.reservation, req, msg_id, est_input)
            text, usage, finish, err, budget_cut = outcome
            self._record_usage(ctx, agent, request_id, attempt, purpose, choice.reservation, usage, s)
            self.guard.settle(choice.reservation, usage.total)
            self._budget_events(ctx, agent, choice.tier)
            if budget_cut:
                self._finish_message(ctx, msg_id, text, "incomplete", {"reason": "tier_budget_reached"})
                exclude.add(choice.tier)
                prev_partial_id = msg_id
                handoff_reason = f"{choice.tier} のAgent予算に到達したため生成を中断"
                self.bus.publish(ctx.id, "budget.exhausted", {"agent_id": agent.id, "tier": choice.tier})
                continue
            if err is not None:
                self._finish_message(ctx, msg_id, text, "incomplete" if text else "failed", {"error": err.kind, "detail": err.message})
                if msg_id:
                    self.bus.publish(ctx.id, "turn.failed", {"agent_id": agent.id, "message_id": msg_id,
                                                              "error": err.kind, "detail": err.message, "partial": bool(text)})
                if err.kind in ("rate_limit", "transient", "timeout", "stream_cut"):
                    transient_tries += 1
                    if transient_tries <= s.debate.max_retries:
                        delay = err.retry_after if err.retry_after is not None else self.backoff_base * (2 ** (transient_tries - 1))
                        delay = min(delay, self.max_backoff)
                        self.bus.publish(ctx.id, "warning", {"agent_id": agent.id, "message": f"{err.kind}: {delay:.1f}秒後に再試行（{transient_tries}/{s.debate.max_retries}）", "retry_in": delay})
                        await asyncio.sleep(delay)
                        prev_partial_id = msg_id
                        continue
                    # this tier is unusable for now → try the next one
                    import time as _t
                    self.guard.cooldown[choice.profile.id] = _t.time() + 60
                    exclude.add(choice.tier)
                    transient_tries = 0
                    handoff_reason = f"{choice.tier} が再試行上限に到達（{err.kind}）"
                    prev_partial_id = msg_id
                    continue
                raise TurnFailed(f"{err.kind}: {err.message}", err.kind, agent.id)
            # FINISHED
            problem = None
            if validator is not None:
                problem = validator(text)
            if problem is None and choice.tier != "primary_cloud":
                problem = self._quality_check(text, allowed_evidence_ids, json_required)
            if problem is not None and (choice.tier != "primary_cloud" or validator is not None):
                quality_failures += 1
                self._finish_message(ctx, msg_id, text, "quality_failed", {"problem": problem})
                self.bus.publish(ctx.id, "model.quality_failed", {"agent_id": agent.id, "tier": choice.tier,
                                                                   "message_id": msg_id, "problem": problem,
                                                                   "will_retry": quality_failures <= 1})
                if quality_failures <= 1:
                    prev_partial_id = msg_id
                    continue
                raise QualityFailed(f"品質検証に不合格: {problem}", "quality", agent.id)
            self._finish_message(ctx, msg_id, text, "completed", {"finish_reason": finish})
            if msg_id:
                self.bus.publish(ctx.id, "turn.completed", {"agent_id": agent.id, "message_id": msg_id,
                                                             "finish_reason": finish, "tier": choice.tier,
                                                             "model_id": choice.model})
            return RunResult(text, usage, choice.tier, choice.profile.id, choice.model, msg_id, finish or "stop")

    # ------------------------------------------------------------ internals
    def _create_message(self, ctx: SessionCtx, agent: AgentConfig, choice, sm: dict) -> str:
        mid = new_id("m")
        self.db.insert("messages", {
            "id": mid, "session_id": ctx.id, "agent_id": agent.id, "seq": self.next_seq(ctx.id),
            "round": sm.get("round"), "phase": sm.get("phase"), "kind": sm.get("kind", "turn"), "content": "",
            "status": "streaming", "tier": choice.tier, "profile_id": choice.profile.id, "model_id": choice.model,
            "scope_json": sm.get("scope", []), "packet_refs_json": sm.get("packet_refs", []),
            "created_at": now_iso(), "meta_json": sm.get("meta", {}),
        })
        return mid

    def _finish_message(self, ctx: SessionCtx, msg_id: str | None, text: str, status: str, meta: dict) -> None:
        if not msg_id:
            return
        row = self.db.one("SELECT meta_json FROM messages WHERE id=?", (msg_id,))
        import json as _j
        m = _j.loads(row["meta_json"] or "{}") if row else {}
        m.update(meta)
        self.db.update("messages", {"id": msg_id}, {"content": text, "status": status, "completed_at": now_iso(),
                                                    "meta_json": m})

    async def _stream(self, ctx: SessionCtx, agent: AgentConfig, r: Reservation, req: LLMRequest,
                      msg_id: str | None, est_input: int):
        adapter = self.registry.get(r.profile.protocol)
        api_key = self.secrets.get(r.profile.env_var)
        parts: list[str] = []
        usage: Usage | None = None
        finish = None
        err: ProviderError | None = None
        budget_cut = False
        out_est = 0
        agen = adapter.stream_completion(req, base_url=r.profile.base_url, api_key=api_key)
        try:
            async for ev in agen:
                if ev.kind == "TEXT_DELTA":
                    parts.append(ev.text)
                    out_est += estimate_tokens(ev.text)
                    if msg_id:
                        self.bus.publish(ctx.id, "turn.delta", {"agent_id": agent.id, "message_id": msg_id, "delta": ev.text})
                    if self.guard.over_budget_live(r, est_input + out_est):
                        budget_cut = True
                        break
                elif ev.kind == "USAGE":
                    usage = ev.usage
                elif ev.kind == "FINISHED":
                    finish = ev.finish_reason or "stop"
                elif ev.kind == "ERROR":
                    err = ev.error
                    break
        except asyncio.CancelledError:
            text = "".join(parts)
            self._finish_message(ctx, msg_id, text, "incomplete", {"reason": "cancelled"})
            self.guard.settle(r, (usage.total if usage else est_input + out_est))
            raise
        finally:
            try:
                await agen.aclose()
            except Exception:  # noqa: BLE001
                pass
        text = "".join(parts)
        if usage is None:
            usage = Usage(input_tokens=est_input, output_tokens=estimate_tokens(text), source="estimated")
        if finish is None and err is None and not budget_cut:
            err = ProviderError("stream_cut", "完了イベントなしでストリームが終了しました")
        return text, usage, finish, err, budget_cut

    def _record_usage(self, ctx: SessionCtx, agent: AgentConfig, request_id: str, attempt: int, purpose: str,
                      r: Reservation, usage: Usage, s: DebateSettings) -> None:
        row = self.meter.record(session_id=ctx.id, agent_id=agent.id, request_id=request_id, attempt=attempt,
                                purpose=purpose, profile_id=r.profile.id, pool_id=r.pool_id, tier=r.tier,
                                model_id=r.model, usage=usage, settings=s)
        self.bus.publish(ctx.id, "usage.updated", {
            "agent_id": agent.id, "purpose": purpose, "tier": r.tier, "model_id": r.model,
            "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens, "reasoning_tokens": usage.reasoning_tokens,
            "usage_source": usage.source, "cost": row["cost"], "cost_status": row["cost_status"],
            "session_total": self.meter.session_total(ctx.id)})

    def _budget_events(self, ctx: SessionCtx, agent: AgentConfig, tier: str) -> None:
        lvl = self.guard.warn_level(ctx.id, agent.id, tier)
        if lvl is not None and lvl >= 0.8:
            b = self.guard.agent_budget(ctx.id, agent.id, tier)
            if not b.get("warned"):
                self.db.execute("UPDATE agent_budgets SET warned=1 WHERE session_id=? AND agent_id=? AND tier=?",
                                (ctx.id, agent.id, tier))
                self.bus.publish(ctx.id, "budget.warning", {"agent_id": agent.id, "tier": tier,
                                                             "used": b["used_tokens"], "limit": b["limit_tokens"]})

    def _quality_check(self, text: str, allowed: set[str] | None, json_required: bool) -> Optional[str]:
        if not text.strip():
            return "空の出力"
        if json_required:
            from app.services.context import parse_json_loose
            if parse_json_loose(text) is None:
                return "JSONスキーマ不一致"
        if allowed is not None:
            cited = set(_EV.findall(text))
            bad = cited - allowed
            if bad:
                return f"配布Packetに存在しない出典IDを引用: {', '.join(sorted(bad))}"
        return None


def text_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


__all__ = ["LLMRunner", "SessionCtx", "RunResult", "TurnFailed", "QualityFailed", "BudgetPaused", "text_hash"]
