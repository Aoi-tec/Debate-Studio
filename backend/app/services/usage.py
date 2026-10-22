"""UsageMeter / pricing (DESIGN §7).

Provider-reported usage wins; otherwise the record is marked ``estimated``.
Cached input and reasoning tokens are stored as *subsets* of input/output and are
never added again (no double counting).
"""
from __future__ import annotations

from typing import Optional

from app.core.ids import new_id, now_iso
from app.models.schemas import DebateSettings
from app.providers.base import Usage
from app.storage.db import Database


def compute_cost(settings: DebateSettings, model: str, u: Usage) -> tuple[Optional[float], str]:
    price = settings.pricing.get(model)
    if not price:
        return None, "uncalculated"
    cached = u.cached_input_tokens or 0
    cached_price = price.cached_price_per_1m if price.cached_price_per_1m is not None else price.input_price_per_1m
    cost = ((u.input_tokens - cached) * price.input_price_per_1m + cached * cached_price
            + u.output_tokens * price.output_price_per_1m) / 1_000_000
    return round(cost, 6), "estimated"


class UsageMeter:
    def __init__(self, db: Database):
        self.db = db

    def record(self, *, session_id: str | None, agent_id: str | None, request_id: str, attempt: int,
               purpose: str, profile_id: str, pool_id: str, tier: str, model_id: str, usage: Usage,
               settings: DebateSettings) -> dict:
        cost, status = compute_cost(settings, model_id, usage)
        row = {
            "id": new_id("use"), "session_id": session_id, "agent_id": agent_id, "request_id": request_id,
            "attempt": attempt, "purpose": purpose, "profile_id": profile_id, "pool_id": pool_id, "tier": tier,
            "model_id": model_id, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens, "reasoning_tokens": usage.reasoning_tokens,
            "usage_source": usage.source, "cost": cost, "cost_status": status, "created_at": now_iso(),
        }
        self.db.insert("usage_records", row)
        return row

    def session_total(self, session_id: str) -> int:
        r = self.db.one("SELECT COALESCE(SUM(input_tokens+output_tokens),0) t FROM usage_records WHERE session_id=?",
                        (session_id,))
        return int(r["t"])

    def session_cost(self, session_id: str) -> tuple[float, bool]:
        r = self.db.one(
            "SELECT COALESCE(SUM(cost),0) c, SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) unknown FROM usage_records WHERE session_id=?",
            (session_id,))
        return float(r["c"] or 0), bool(r["unknown"])

    def pool_increment_since(self, pool_id: str, since_iso: str) -> int:
        r = self.db.one(
            "SELECT COALESCE(SUM(input_tokens+output_tokens),0) t FROM usage_records WHERE pool_id=? AND created_at>=?",
            (pool_id, since_iso))
        return int(r["t"])

    def summary(self, session_id: str) -> dict:
        rows = self.db.all(
            """SELECT agent_id, tier, purpose, model_id, usage_source,
                      SUM(input_tokens) i, SUM(output_tokens) o, SUM(COALESCE(cached_input_tokens,0)) c,
                      SUM(COALESCE(reasoning_tokens,0)) r, SUM(cost) cost,
                      SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) uncosted, COUNT(*) n
               FROM usage_records WHERE session_id=? GROUP BY agent_id, tier, purpose, model_id, usage_source""",
            (session_id,))
        total_i = sum(r["i"] or 0 for r in rows)
        total_o = sum(r["o"] or 0 for r in rows)
        cost, unknown = self.session_cost(session_id)
        by_agent: dict[str, dict] = {}
        for r in rows:
            a = by_agent.setdefault(r["agent_id"] or "-", {"input": 0, "output": 0, "cached": 0, "reasoning": 0,
                                                            "calls": 0, "by_purpose": {}, "sources": set()})
            a["input"] += r["i"] or 0
            a["output"] += r["o"] or 0
            a["cached"] += r["c"] or 0
            a["reasoning"] += r["r"] or 0
            a["calls"] += r["n"]
            p = a["by_purpose"].setdefault(r["purpose"], 0)
            a["by_purpose"][r["purpose"]] = p + (r["i"] or 0) + (r["o"] or 0)
            a["sources"].add(r["usage_source"])
        for a in by_agent.values():
            a["sources"] = sorted(a["sources"])
        return {"input_tokens": total_i, "output_tokens": total_o, "total_tokens": total_i + total_o,
                "estimated_cost": cost if not unknown or cost else None, "cost_complete": not unknown,
                "by_agent": by_agent, "rows": rows}
