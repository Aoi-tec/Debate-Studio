"""TokenBudgetGuard + UsageQuotaReader + ModelRouter (DESIGN §7A).

* Agent × Tier budgets (input+output) and shared ``quota_pool_id`` thresholds are
  checked separately; both must pass.
* Pool threshold: ``limit = U0 + U0/6`` (OpenAI/Claude API) or ``U0 + U0/4``
  (other clouds), with U0 fixed at session start. This is an app-defined
  threshold, *not* an official remaining quota.
* U0 = 0 or unavailable → an explicit manual / bootstrap budget is required.
* Reservations are taken atomically before a request and settled afterwards.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.ids import new_id, now_iso
from app.models.schemas import TIERS, AgentConfig, ApiProfile, DebateSettings
from app.providers.registry import ProviderRegistry
from app.services.usage import UsageMeter
from app.storage.db import Database
from app.storage.secret_store import SecretStore

MIN_OUTPUT = 64


class BudgetPaused(Exception):
    def __init__(self, reasons: list[str], hard: bool = False):
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.hard = hard


class StartRefused(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Reservation:
    id: str
    session_id: str
    agent_id: str
    tier: str
    pool_id: str
    profile: ApiProfile
    model: str
    tokens: int
    max_output: int
    settled: bool = False


@dataclass
class RouteChoice:
    tier: str
    profile: ApiProfile
    model: str
    reservation: Reservation
    skipped: list[str] = field(default_factory=list)


def month_start_unix(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())


def threshold(family: str, u0: int, s: DebateSettings) -> int:
    frac = s.routing.openai_claude_extra_fraction if family == "openai_claude" else s.routing.other_cloud_extra_fraction
    return int(u0 + u0 * frac)


class BudgetGuard:
    def __init__(self, db: Database, meter: UsageMeter):
        self.db = db
        self.meter = meter
        self._lock = threading.Lock()
        self._inflight_agent: dict[tuple[str, str, str], int] = {}
        self._inflight_pool: dict[str, int] = {}
        self._inflight_session: dict[str, int] = {}
        self._res: dict[str, Reservation] = {}
        self.cooldown: dict[str, float] = {}  # profile_id -> until (rate limit / billing stop)

    # ------------------------------------------------------------ agent budgets
    def init_agent_budgets(self, session_id: str, agents: list[AgentConfig]) -> None:
        for a in agents:
            for tier in TIERS:
                lim = a.tier_token_budgets.get(tier)
                self.db.execute(
                    "INSERT OR IGNORE INTO agent_budgets(session_id, agent_id, tier, used_tokens, limit_tokens) VALUES (?,?,?,?,?)",
                    (session_id, a.id, tier, 0, lim))

    def set_agent_limits(self, session_id: str, agent_id: str, limits: dict[str, Optional[int]]) -> None:
        for tier, lim in limits.items():
            self.db.execute(
                "INSERT INTO agent_budgets(session_id, agent_id, tier, used_tokens, limit_tokens) VALUES (?,?,?,0,?) "
                "ON CONFLICT(session_id, agent_id, tier) DO UPDATE SET limit_tokens=excluded.limit_tokens",
                (session_id, agent_id, tier, lim))

    def agent_budget(self, session_id: str, agent_id: str, tier: str) -> dict:
        r = self.db.one("SELECT * FROM agent_budgets WHERE session_id=? AND agent_id=? AND tier=?",
                        (session_id, agent_id, tier))
        if not r:
            return {"used_tokens": 0, "limit_tokens": None, "inflight": 0}
        r["inflight"] = self._inflight_agent.get((session_id, agent_id, tier), 0)
        return r

    # ------------------------------------------------------------ pool baselines
    async def snapshot_baselines(self, session_id: str, profiles: list[ApiProfile], settings: DebateSettings,
                                 registry: ProviderRegistry, secrets: SecretStore) -> list[dict]:
        """Fix U0 per quota pool at session start. Raises StartRefused when a manual value is required."""
        problems: list[str] = []
        out: list[dict] = []
        seen: set[str] = set()
        for p in profiles:
            pool = p.pool_id
            if pool in seen:
                continue
            seen.add(pool)
            q = p.quota
            row = {"id": new_id("base"), "session_id": session_id, "pool_id": pool, "profile_id": p.id,
                   "family": p.billing_family, "period": q.period_label, "u0": None, "limit_tokens": None,
                   "source": "disabled", "fetched_at": now_iso(), "estimated": 0, "detail": ""}
            if q.mode == "disabled" or p.billing_family == "local":
                row["detail"] = "口座閾値は無効（Agent別予算とセッション上限のみ適用）"
                self.db.insert("usage_baselines", row)
                out.append(row)
                continue
            snap = None
            if q.mode == "auto":
                adapter = registry.get(p.protocol)
                snap = await adapter.read_usage_snapshot(base_url=p.base_url, admin_key=secrets.get(q.admin_env_var),
                                                         start_unix=month_start_unix())
            if snap is not None and snap.status == "readable" and (snap.tokens or 0) > 0:
                u0 = int(snap.tokens or 0)
                row.update(u0=u0, limit_tokens=threshold(p.billing_family, u0, settings), source="reported",
                           detail=snap.detail)
            elif snap is not None and snap.status == "readable":  # U0 == 0
                if q.initial_bootstrap_budget:
                    row.update(u0=0, limit_tokens=q.initial_bootstrap_budget, source="bootstrap",
                               detail="U0=0 のため手動の初期予算を使用")
                else:
                    problems.append(f"{p.name}: 当期使用量U0=0のため式では追加許容量も0です。初期予算(initial_bootstrap_budget)を設定してください")
                    continue
            else:
                if q.manual_budget_tokens:
                    row.update(u0=None, limit_tokens=q.manual_budget_tokens, source="manual", estimated=1,
                               detail=("使用量API取得不可: " + (snap.detail if snap else "manualモード")) + " → 手動予算＋ローカル集計")
                else:
                    reason = snap.detail if snap else "manualモード"
                    problems.append(f"{p.name}: 口座使用量を取得できません（{reason}）。手動予算(manual_budget_tokens)を設定するか、口座閾値を無効にしてください")
                    continue
            self.db.insert("usage_baselines", row)
            out.append(row)
        if problems:
            self.db.execute("DELETE FROM usage_baselines WHERE session_id=?", (session_id,))
            raise StartRefused(problems)
        return out

    def baseline(self, session_id: str, pool_id: str) -> dict | None:
        return self.db.one("SELECT * FROM usage_baselines WHERE session_id=? AND pool_id=? ORDER BY fetched_at DESC LIMIT 1",
                           (session_id, pool_id))

    def pool_projection(self, session_id: str, pool_id: str) -> dict:
        b = self.baseline(session_id, pool_id)
        inflight = self._inflight_pool.get(pool_id, 0)
        if not b or b["limit_tokens"] is None:
            return {"limit": None, "projected": None, "source": b["source"] if b else "none", "inflight": inflight}
        inc = self.meter.pool_increment_since(pool_id, b["fetched_at"])
        projected = (b["u0"] or 0) + inc + inflight
        return {"limit": b["limit_tokens"], "projected": projected, "u0": b["u0"], "increment": inc,
                "inflight": inflight, "source": b["source"], "fetched_at": b["fetched_at"]}

    # ------------------------------------------------------------ session limit
    def session_check(self, session_id: str, settings: DebateSettings, est: int) -> Optional[int]:
        """Raise when the session hard limit leaves no room; return the max output still allowed."""
        lim = settings.limits.max_session_tokens
        room = None
        if lim is not None:
            used = self.meter.session_total(session_id) + self._inflight_session.get(session_id, 0)
            room = lim - used - est
            if room < MIN_OUTPUT:
                raise BudgetPaused([f"セッションのトークン上限 {lim} に到達（使用 {used}）。Tier切替では回避しません"], hard=True)
        if settings.limits.max_estimated_cost is not None:
            cost, unknown = self.meter.session_cost(session_id)
            if cost >= settings.limits.max_estimated_cost:
                raise BudgetPaused([f"セッションの概算費用上限 {settings.limits.max_estimated_cost} に到達"], hard=True)
        return room

    # ------------------------------------------------------------ reserve / settle
    def try_reserve(self, *, session_id: str, agent: AgentConfig, tier: str, profile: ApiProfile, model: str,
                    est_input: int, desired_output: int) -> tuple[Reservation | None, str]:
        with self._lock:
            b = self.agent_budget(session_id, agent.id, tier)
            out = desired_output
            if b["limit_tokens"] is not None:
                remaining = b["limit_tokens"] - b["used_tokens"] - b["inflight"]
                out = min(out, remaining - est_input)
                if out < MIN_OUTPUT:
                    return None, f"{tier}: Agent Tier予算到達（使用 {b['used_tokens']}/{b['limit_tokens']}）"
            proj = self.pool_projection(session_id, profile.pool_id)
            if proj["limit"] is not None:
                if proj["projected"] + est_input + out >= proj["limit"]:
                    room = proj["limit"] - proj["projected"] - est_input - 1
                    if room < MIN_OUTPUT:
                        return None, f"{tier}: API口座の閾値到達（見込み {proj['projected']} / 閾値 {proj['limit']}）"
                    out = min(out, room)
            tokens = est_input + out
            r = Reservation(new_id("rsv"), session_id, agent.id, tier, profile.pool_id, profile, model, tokens, out)
            k = (session_id, agent.id, tier)
            self._inflight_agent[k] = self._inflight_agent.get(k, 0) + tokens
            self._inflight_pool[profile.pool_id] = self._inflight_pool.get(profile.pool_id, 0) + tokens
            self._inflight_session[session_id] = self._inflight_session.get(session_id, 0) + tokens
            self._res[r.id] = r
            return r, ""

    def settle(self, r: Reservation, actual_tokens: int) -> None:
        with self._lock:
            if r.settled:
                return
            r.settled = True
            k = (r.session_id, r.agent_id, r.tier)
            self._inflight_agent[k] = max(0, self._inflight_agent.get(k, 0) - r.tokens)
            self._inflight_pool[r.pool_id] = max(0, self._inflight_pool.get(r.pool_id, 0) - r.tokens)
            self._inflight_session[r.session_id] = max(0, self._inflight_session.get(r.session_id, 0) - r.tokens)
            self._res.pop(r.id, None)
            self.db.execute("UPDATE agent_budgets SET used_tokens=used_tokens+? WHERE session_id=? AND agent_id=? AND tier=?",
                            (actual_tokens, r.session_id, r.agent_id, r.tier))

    def over_budget_live(self, r: Reservation, live_tokens: int) -> bool:
        b = self.agent_budget(r.session_id, r.agent_id, r.tier)
        if b["limit_tokens"] is None:
            return False
        other_inflight = b["inflight"] - r.tokens
        return b["used_tokens"] + other_inflight + live_tokens > b["limit_tokens"]

    def warn_level(self, session_id: str, agent_id: str, tier: str) -> float | None:
        b = self.agent_budget(session_id, agent_id, tier)
        if not b["limit_tokens"]:
            return None
        return b["used_tokens"] / b["limit_tokens"]


class ModelRouter:
    """primary_cloud → downstream_cloud → local_llm."""

    def __init__(self, guard: BudgetGuard, secrets: SecretStore):
        self.guard = guard
        self.secrets = secrets

    def tier_profiles(self, agent: AgentConfig, profiles: dict[str, ApiProfile]) -> list[tuple[str, ApiProfile | None, str]]:
        out = []
        for tier in TIERS:
            pid = agent.routing_profiles.get(tier)
            if tier == "primary_cloud" and not pid:
                pid = agent.profile_id
            if not pid:
                continue
            p = profiles.get(pid)
            out.append((tier, p, pid))
        return out

    def model_for(self, agent: AgentConfig, tier: str, p: ApiProfile) -> str:
        if tier == "primary_cloud" and agent.model_override:
            return agent.model_override
        return p.default_model

    def select(self, *, session_id: str, agent: AgentConfig, profiles: dict[str, ApiProfile], settings: DebateSettings,
               est_input: int, desired_output: int, needs_document_transfer: bool,
               exclude_tiers: set[str] | None = None) -> RouteChoice:
        room = self.guard.session_check(session_id, settings, est_input)
        if room is not None:
            desired_output = min(desired_output, room)
        skipped: list[str] = []
        order = [t for t in settings.routing.priority if t in TIERS]
        cands = {t: (p, pid) for t, p, pid in self.tier_profiles(agent, profiles)}
        for tier in order:
            if exclude_tiers and tier in exclude_tiers:
                continue
            if tier not in cands:
                continue
            p, pid = cands[tier]
            if p is None:
                skipped.append(f"{tier}: プロファイル {pid} が存在しません")
                continue
            if not p.enabled:
                skipped.append(f"{tier}: {p.name} は無効")
                continue
            model = self.model_for(agent, tier, p)
            if not model:
                skipped.append(f"{tier}: {p.name} のモデル名が未設定")
                continue
            if p.env_var and not self.secrets.has(p.env_var):
                skipped.append(f"{tier}: {p.name} のAPIキー未登録")
                continue
            if self.guard.cooldown.get(p.id, 0) > time.time():
                skipped.append(f"{tier}: {p.name} はレート制限/停止中")
                continue
            if (tier != "primary_cloud" and settings.routing.require_quality_validation_for_downstream
                    and not (p.quality.status == "passed" and p.quality.validated_model == model)):
                skipped.append(f"{tier}: {p.name}/{model} は品質検証未合格")
                continue
            if needs_document_transfer and not p.allow_document_transfer:
                skipped.append(f"{tier}: {p.name} への資料由来データ送信が許可されていません")
                continue
            r, why = self.guard.try_reserve(session_id=session_id, agent=agent, tier=tier, profile=p, model=model,
                                            est_input=est_input, desired_output=desired_output)
            if r is None:
                skipped.append(why)
                continue
            return RouteChoice(tier, p, model, r, skipped)
        raise BudgetPaused(skipped or ["利用可能なモデル階層がありません"])
