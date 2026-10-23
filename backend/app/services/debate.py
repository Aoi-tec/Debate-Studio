"""DebateController — the session state machine (DESIGN §5).

CREATED → INITIALIZING → DOCUMENT_PREP → CHAIR_DOCUMENT_ANALYSIS → CONTEXT_DISTRIBUTION
→ OPENING → INDEPENDENT → CHAIR_REVIEW → DEBATE → CHECKPOINT_REPORT
→ WAITING_USER_DECISION → (DEBATE | FINALIZING) → COMPLETED
plus CONTEXT_REQUESTED / CHAIR_EVIDENCE_SEARCH / WAITING_USER_EVIDENCE /
EVIDENCE_REVALIDATION, PAUSED, PAUSED_BUDGET, FAILED, CANCELLED.

The run loop is a resumable step machine: progress lives in ``progress_json`` and
every step is re-entrant, so restarts and user waits never re-send confirmed turns.
Waiting states end the task; user actions spawn a new one. No LLM call is made
while waiting for a user decision or user evidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable, Optional

from app.core.ids import new_id, now_iso
from app.core.tokens import estimate_tokens
from app.models.schemas import AgentConfig, ApiProfile, DebateSettings, RoleMeta
from app.providers.base import LLMRequest
from app.services.budget import BudgetGuard, BudgetPaused, StartRefused
from app.services.chair import ChairOrchestrator
from app.services.compression import CompressionService
from app.services.context import (build_participant_request, chair_system, parse_context_request,
                                  parse_json_loose, render_history)
from app.services.events import EventBus
from app.services.evidence import WAITING_STATES, EvidenceService
from app.services.llm_runner import LLMRunner, QualityFailed, SessionCtx, TurnFailed
from app.services.packets import PacketInvalid, PacketService
from app.storage.db import Database, dec

log = logging.getLogger("app.debate")

TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")
WAITING = ("WAITING_USER_DECISION", "WAITING_USER_EVIDENCE", "PAUSED", "PAUSED_BUDGET", "CREATED")
_EV = re.compile(r"\bev_[0-9a-f]{6,}\b")

CHECKPOINT_INSTRUCTIONS = """指定ラウンドが終了した。中間報告をJSONのみで出力せよ（人間が続行・方向修正・終了を判断する材料）。
{"progress_summary": "現在までに到達した結論", "resolved_issues": ["合意済み"], "unresolved_issues": ["未合意"],
 "stagnation_reasons": ["停滞の理由（なければ空）"], "missing_evidence": ["不足している根拠"],
 "next_round_questions": ["次ラウンドの候補論点"], "agent_dissent": [{"agent": "名前", "point": "少数意見"}],
 "new_information_since_last": true}
根拠が不十分な事項はその旨を明記し、確定事項として書かない。"""

FINAL_INSTRUCTIONS = """ユーザーが終了を選択した。最終統合をJSONのみで出力せよ。無理に全員一致にしない。
{"adopted_proposal": "採用案（根拠不足なら暫定と明記）", "rationale": "理由", "minority_opinions": ["少数意見"],
 "uncertainties": ["不確実性"], "next_actions": ["次の行動"]}
未解決の根拠要求や仮説がある場合、それに依存する結論を確定事項として書かない。"""

CHECKPOINT_KEYS = {"progress_summary": "", "resolved_issues": [], "unresolved_issues": [], "stagnation_reasons": [],
                   "missing_evidence": [], "next_round_questions": [], "agent_dissent": [],
                   "new_information_since_last": None}


class SessionError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class DebateController:
    def __init__(self, *, db: Database, bus: EventBus, config, runner: LLMRunner, chair: ChairOrchestrator,
                 packets: PacketService, evidence: EvidenceService, files, guard: BudgetGuard,
                 compression: CompressionService, registry, secrets):
        self.db, self.bus, self.config, self.runner = db, bus, config, runner
        self.chair, self.packets, self.evidence, self.files = chair, packets, evidence, files
        self.guard, self.compression, self.registry, self.secrets = guard, compression, registry, secrets
        self.tasks: dict[str, asyncio.Task] = {}
        self._ctx: dict[str, SessionCtx] = {}
        evidence.on_terminal = self._on_evidence_terminal
        files.listeners.append(self._on_file_status)

    # ================================================================ session CRUD
    def create(self, topic: str, attachment_ids: list[str], rounds: Optional[int]) -> dict:
        profiles = self.config.profiles()
        agents = self.config.agents()
        roles = {r.id: self.config.role(r.id) for r in self.config.roles()}
        settings = self.config.settings()
        if rounds:
            settings.debate.debate_rounds = rounds
        for a in agents:
            if a.role_id not in roles or roles[a.role_id] is None:
                raise SessionError(422, f"{a.name}: 役割 {a.role_id} が見つかりません")
        sid = new_id("s")
        snapshot = {
            "profiles": [p.model_dump() for p in profiles],
            "agents": [a.model_dump() for a in agents],
            "roles": {rid: {"meta": m.model_dump(), "md": md} for rid, (m, md) in roles.items() if rid in {a.role_id for a in agents}},
            "settings": settings.model_dump(),
        }
        self.db.insert("debate_sessions", {
            "id": sid, "topic": topic, "state": "CREATED", "created_at": now_iso(), "snapshot_json": snapshot,
            "progress_json": {"phase": "PREP", "round": 0, "rounds_target": settings.debate.debate_rounds,
                              "independent_done": [], "debate_done": [], "pending_redo": [],
                              "context_ready": False, "needs_context_refresh": False}})
        for fid in attachment_ids:
            self.attach(sid, fid, None, required=False, allow_partial=False, emit=False)
        self.db.audit("session.create", {"session_id": sid})
        return self.public(sid)

    def ctx(self, sid: str) -> SessionCtx:
        if sid in self._ctx:
            return self._ctx[sid]
        r = self.db.one("SELECT * FROM debate_sessions WHERE id=?", (sid,))
        if not r:
            raise SessionError(404, "セッションが見つかりません")
        snap = dec(r["snapshot_json"], {})
        c = SessionCtx(
            id=sid, topic=r["topic"],
            agents={a["id"]: AgentConfig.model_validate(a) for a in snap["agents"]},
            profiles={p["id"]: ApiProfile.model_validate(p) for p in snap["profiles"]},
            roles={rid: (RoleMeta.model_validate(v["meta"]), v["md"]) for rid, v in snap["roles"].items()},
            settings=DebateSettings.model_validate(snap["settings"]))
        for row in self.db.all("SELECT agent_id, tier FROM messages WHERE session_id=? AND tier IS NOT NULL ORDER BY seq", (sid,)):
            c.last_tier[row["agent_id"]] = row["tier"]
        self._ctx[sid] = c
        return c

    def row(self, sid: str) -> dict:
        r = self.db.one("SELECT * FROM debate_sessions WHERE id=?", (sid,))
        if not r:
            raise SessionError(404, "セッションが見つかりません")
        return r

    def progress(self, sid: str) -> dict:
        return dec(self.row(sid)["progress_json"], {})

    def mutate(self, sid: str, fn: Callable[[dict], None]) -> dict:
        p = self.progress(sid)
        fn(p)
        self.db.update("debate_sessions", {"id": sid}, {"progress_json": p})
        return p

    def set_state(self, sid: str, state: str, **extra: Any) -> None:
        old = self.row(sid)["state"]
        vals: dict[str, Any] = {"state": state}
        vals.update(extra)
        self.db.update("debate_sessions", {"id": sid}, vals)
        if old != state:
            self.bus.publish(sid, "phase.changed", {"from": old, "phase": state})

    def set_blockers(self, sid: str, blockers: list[dict]) -> None:
        self.db.update("debate_sessions", {"id": sid}, {"blockers_json": blockers})

    def blockers(self, sid: str) -> list[dict]:
        r = self.row(sid)
        out = list(dec(r["blockers_json"], []))
        for er in self.evidence.open_requests(sid):
            out.append({"type": "evidence", "evidence_request_id": er["id"], "state": er["state"],
                        "criticality": er["criticality"], "message": "資料不足のためユーザーの回答待ち"})
        if r["state"] == "WAITING_USER_DECISION":
            out.append({"type": "checkpoint", "message": "中間報告に対する判断待ち"})
        return out

    def public(self, sid: str) -> dict:
        r = self.row(sid)
        snap = dec(r["snapshot_json"], {})
        prog = dec(r["progress_json"], {})
        return {
            "id": r["id"], "topic": r["topic"], "state": r["state"], "created_at": r["created_at"],
            "started_at": r["started_at"], "ended_at": r["ended_at"], "error": r["error"],
            "progress": {k: prog.get(k) for k in ("phase", "round", "rounds_target", "independent_done", "debate_done", "pending_redo")},
            "agents": [{"id": a["id"], "name": a["name"], "kind": a["kind"], "role_id": a["role_id"],
                        "role_name": snap["roles"].get(a["role_id"], {}).get("meta", {}).get("name", a["role_id"]),
                        "avatar": a.get("avatar"), "enabled": a["enabled"], "profile_id": a["profile_id"],
                        "routing_profiles": a.get("routing_profiles", {}), "tier_token_budgets": a.get("tier_token_budgets", {})}
                       for a in snap["agents"]],
            "settings": {"debate_rounds": snap["settings"]["debate"]["debate_rounds"]},
            "final_report": dec(r["final_report_json"]), "conclusion_status": r["conclusion_status"],
            "blockers": self.blockers(sid),
        }

    def list(self) -> list[dict]:
        return [{"id": r["id"], "topic": r["topic"], "state": r["state"], "created_at": r["created_at"]}
                for r in self.db.all("SELECT id, topic, state, created_at FROM debate_sessions ORDER BY created_at DESC")]

    def messages(self, sid: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM messages WHERE session_id=? ORDER BY seq", (sid,))
        return [{**{k: r[k] for k in ("id", "agent_id", "seq", "round", "phase", "kind", "content", "status", "tier",
                                      "profile_id", "model_id", "created_at", "completed_at", "handoff_id")},
                 "scope": dec(r["scope_json"], []), "packet_refs": dec(r["packet_refs_json"], []),
                 "meta": dec(r["meta_json"], {})} for r in rows]

    # ================================================================ lifecycle
    async def start(self, sid: str) -> dict:
        r = self.row(sid)
        if r["state"] != "CREATED":
            raise SessionError(409, f"開始できる状態ではありません（{r['state']}）")
        ctx = self.ctx(sid)
        problems = []
        parts = ctx.participants()
        if len(parts) < 3:
            problems.append("有効な一般Agentが3人未満です")
        if not ctx.chair.enabled:
            problems.append("議長が無効です")
        used_profiles: dict[str, ApiProfile] = {}
        for a in [ctx.chair] + parts:
            for tier, p, pid in self.runner.router.tier_profiles(a, ctx.profiles):
                if p is None:
                    problems.append(f"{a.name}: {tier} のプロファイル {pid} が存在しません")
                    continue
                used_profiles[p.id] = p
                if tier == "primary_cloud":
                    if not p.enabled:
                        problems.append(f"{a.name}: 主接続 {p.name} が無効です")
                    if not self.runner.router.model_for(a, tier, p):
                        problems.append(f"{a.name}: 主接続 {p.name} のモデル名が未設定です")
                    if p.env_var and not self.secrets.has(p.env_var):
                        problems.append(f"{a.name}: 主接続 {p.name} のAPIキー（{p.env_var}）が未登録です")
        if problems:
            raise SessionError(409, "討論を開始できません", problems)
        self.set_state(sid, "INITIALIZING", started_at=now_iso())
        self.guard.init_agent_budgets(sid, list(ctx.agents.values()))
        try:
            by_profile: dict[str, list[str]] = {}
            for a in [ctx.chair] + parts:
                for _tier, p, _pid in self.runner.router.tier_profiles(a, ctx.profiles):
                    if p is not None:
                        by_profile.setdefault(p.id, []).append(a.id)
            await self.guard.snapshot_baselines(sid, list(used_profiles.values()), ctx.settings, self.registry,
                                                self.secrets, agents_by_profile=by_profile)
        except StartRefused as e:
            self.set_state(sid, "CREATED", started_at=None)
            raise SessionError(409, "予算の基準値を確定できないため開始を拒否しました", e.problems) from e
        for b in self.db.all("SELECT * FROM usage_baselines WHERE session_id=?", (sid,)):
            self.bus.publish(sid, "quota.refreshed" if b["source"] == "reported" else "quota.unavailable", {
                "pool_id": b["pool_id"], "source": b["source"], "u0": b["u0"], "limit": b["limit_tokens"], "detail": b["detail"]})
        self.bus.publish(sid, "session.started", {"topic": ctx.topic})
        self.spawn(sid)
        return self.public(sid)

    def spawn(self, sid: str) -> None:
        t = self.tasks.get(sid)
        if t and not t.done():
            return
        self.tasks[sid] = asyncio.get_event_loop().create_task(self._run(sid))

    async def wait_idle(self, sid: str, timeout: float = 30) -> None:
        """Test helper: wait until the run task (if any) finishes."""
        for _ in range(int(timeout * 20)):
            t = self.tasks.get(sid)
            pending = [x for x in self.evidence._tasks.values() if not x.done()]
            if (not t or t.done()) and not pending:
                await asyncio.sleep(0)
                t = self.tasks.get(sid)
                if not t or t.done():
                    return
            await asyncio.sleep(0.05)
        raise TimeoutError("session did not become idle")

    def pause(self, sid: str) -> dict:
        r = self.row(sid)
        t = self.tasks.get(sid)
        if not t or t.done() or r["state"] in WAITING + TERMINAL:
            raise SessionError(409, f"実行中ではないため一時停止できません（{r['state']}）")
        self.mutate(sid, lambda p: p.__setitem__("pause_requested", True))
        self.bus.publish(sid, "session.pause_requested", {})
        return self.public(sid)

    def resume(self, sid: str) -> dict:
        r = self.row(sid)
        if r["state"] not in ("PAUSED", "PAUSED_BUDGET"):
            raise SessionError(409, f"再開できる状態ではありません（{r['state']}）")
        self.set_blockers(sid, [])
        self.mutate(sid, lambda p: p.pop("pause_requested", None))
        self.bus.publish(sid, "session.resumed", {})
        self.set_state(sid, "RESUMING")
        self.spawn(sid)
        return self.public(sid)

    async def cancel(self, sid: str) -> dict:
        r = self.row(sid)
        if r["state"] in TERMINAL:
            raise SessionError(409, "既に終了しています")
        t = self.tasks.get(sid)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.db.execute("UPDATE messages SET status='incomplete' WHERE session_id=? AND status='streaming'", (sid,))
        self.set_state(sid, "CANCELLED", ended_at=now_iso())
        self.bus.publish(sid, "session.completed", {"state": "CANCELLED"})
        return self.public(sid)

    def skip_agent(self, sid: str, agent_id: str) -> dict:
        r = self.row(sid)
        if r["state"] != "PAUSED":
            raise SessionError(409, "一時停止中のみスキップできます")
        ctx = self.ctx(sid)
        if agent_id not in ctx.agents or ctx.agents[agent_id].kind != "participant":
            raise SessionError(422, "一般Agentを指定してください")

        def f(p: dict) -> None:
            key = "independent_done" if p["phase"] == "INDEPENDENT" else "debate_done"
            if agent_id not in p[key]:
                p[key].append(agent_id)
            p["pending_redo"] = [x for x in p["pending_redo"] if x["agent_id"] != agent_id]
            p.setdefault("skipped", []).append({"agent_id": agent_id, "phase": p["phase"], "round": p["round"]})
        self.mutate(sid, f)
        self._system_message(sid, f"{ctx.agents[agent_id].name} の発言はユーザー操作によりスキップされました。")
        return self.resume(sid)

    def recover_after_restart(self) -> None:
        active = self.db.all("SELECT id, state FROM debate_sessions WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED','CREATED','WAITING_USER_DECISION','WAITING_USER_EVIDENCE','PAUSED','PAUSED_BUDGET')")
        for r in active:
            for m in self.db.all("SELECT id FROM messages WHERE session_id=? AND status='streaming'", (r["id"],)):
                self.db.update("messages", {"id": m["id"]}, {"status": "incomplete"})
                self._merge_meta(m["id"], {"reason": "restart"})
            self.db.update("debate_sessions", {"id": r["id"]}, {"state": "PAUSED", "blockers_json": [
                {"type": "restart", "message": "アプリ再起動により中断しました。未確定の発言は採用していません。再開してください。"}]})
            self.bus.publish(r["id"], "phase.changed", {"from": r["state"], "phase": "PAUSED", "reason": "restart"})
        # evidence revalidation that was in flight
        for er in self.db.all("SELECT * FROM evidence_requests WHERE state IN ('ANSWER_RECEIVED','REVALIDATING')"):
            resp = self.db.one("SELECT file_ids_json FROM evidence_responses WHERE evidence_request_id=? AND action='file_upload' ORDER BY created_at DESC LIMIT 1", (er["id"],))
            if resp:
                ctx = self.ctx(er["session_id"])
                if er["state"] == "ANSWER_RECEIVED":
                    self.db.update("evidence_requests", {"id": er["id"]}, {"state": "REVALIDATING"})
                self.evidence._tasks[er["id"]] = asyncio.get_event_loop().create_task(
                    self.evidence._revalidate(ctx, er["id"], dec(resp["file_ids_json"], []), self.files))

    # ================================================================ user inputs
    def decide(self, sid: str, decision: str, instruction: Optional[str], key: Optional[str]) -> dict:
        r = self.row(sid)
        cp = self.db.one("SELECT * FROM debate_checkpoints WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (sid,))
        if cp and cp["decision"] and key and cp["idempotency_key"] == key:
            return {**self.public(sid), "idempotent_replay": True}
        if r["state"] != "WAITING_USER_DECISION" or not cp or cp["decision"]:
            raise SessionError(409, f"中間判断の待機中ではありません（{r['state']}）")
        if decision in ("continue", "redirect") and self.evidence.open_requests(sid):
            raise SessionError(409, "資料不足の確認が未解決です。先に資料不足カードに回答してください（終了は可能です）")
        if decision == "redirect" and not (instruction or "").strip():
            raise SessionError(422, "方向修正の指示を入力してください")
        self.db.update("debate_checkpoints", {"id": cp["id"]}, {"decision": decision, "instruction": instruction,
                                                                "decided_by": "user", "decided_at": now_iso(),
                                                                "idempotency_key": key})
        if decision == "redirect":
            self._user_message(sid, f"【方向修正の指示】{instruction}", kind="user", meta={"decision": "redirect"})
        elif instruction and instruction.strip():
            self._user_message(sid, instruction.strip(), kind="user")

        def f(p: dict) -> None:
            if decision in ("continue", "redirect"):
                p["rounds_target"] = p.get("rounds_target", 0) + 1
                p["round"] = p.get("round", 0) + 1
                p["debate_done"] = []
                p["phase"] = "DEBATE"
            else:
                p["phase"] = "FINALIZING"
        self.mutate(sid, f)
        self.bus.publish(sid, "checkpoint.decision_received", {"checkpoint_id": cp["id"], "decision": decision,
                                                                "instruction": instruction})
        self.set_state(sid, "DEBATE" if decision != "finish" else "FINALIZING")
        self.spawn(sid)
        return self.public(sid)

    def add_user_message(self, sid: str, text: str, attachment_ids: list[str]) -> dict:
        r = self.row(sid)
        if r["state"] in TERMINAL:
            raise SessionError(409, "終了したセッションです")
        mid = None
        if text.strip():
            mid = self._user_message(sid, text.strip(), kind="user")
        for fid in attachment_ids:
            self.attach(sid, fid, None, required=False, allow_partial=False, message_id=mid)
        return {"message_id": mid}

    def _user_message(self, sid: str, text: str, kind: str = "user", meta: dict | None = None) -> str:
        mid = new_id("m")
        seq = self.runner.next_seq(sid)
        self.db.insert("messages", {"id": mid, "session_id": sid, "agent_id": None, "seq": seq,
                                    "round": self.progress(sid).get("round"), "phase": "USER", "kind": kind,
                                    "content": text, "status": "completed", "created_at": now_iso(),
                                    "completed_at": now_iso(), "scope_json": [], "meta_json": meta or {}})
        self.bus.publish(sid, "turn.started", {"agent_id": None, "message_id": mid, "kind": kind, "content": text,
                                               "round": self.progress(sid).get("round"), "phase": "USER"})
        self.bus.publish(sid, "turn.completed", {"agent_id": None, "message_id": mid, "kind": kind})
        return mid

    def _system_message(self, sid: str, text: str) -> str:
        return self._user_message(sid, text, kind="system")

    # ---------------------------------------------------------------- attachments
    def attach(self, sid: str, file_id: str, allowed: Optional[list[str]], *, required: bool, allow_partial: bool,
               message_id: str | None = None, emit: bool = True) -> None:
        f = self.db.one("SELECT * FROM uploaded_files WHERE file_id=? AND deleted_at IS NULL", (file_id,))
        if not f:
            raise SessionError(404, f"ファイル {file_id} が見つかりません")
        if self.db.one("SELECT 1 FROM session_attachments WHERE session_id=? AND file_id=?", (sid, file_id)):
            raise SessionError(409, "既に添付されています")
        n, b = self.files.session_bytes(sid)
        if n + 1 > self.files.s.max_files_per_session:
            raise SessionError(413, f"1セッションの添付数上限（{self.files.s.max_files_per_session}）を超えます")
        if b + f["size"] > self.files.s.max_session_bytes:
            raise SessionError(413, "1セッションの合計サイズ上限を超えます")
        snap_agents = {a["id"] for a in dec(self.row(sid)["snapshot_json"], {})["agents"]}
        if allowed is not None and any(a not in snap_agents for a in allowed):
            raise SessionError(422, "不明なAgentが配布先に含まれています")
        self.db.insert("session_attachments", {
            "session_id": sid, "file_id": file_id, "message_id": message_id, "attached_at": now_iso(),
            "allowed_agent_ids_json": allowed, "enabled": 1, "apply_from_seq": self.runner.next_seq(sid),
            "attach_version": 1, "required": int(required), "allow_partial": int(allow_partial)})
        self.mutate(sid, lambda p: p.__setitem__("needs_context_refresh", True))
        if emit:
            self.bus.publish(sid, "file.attached", {"file": self.files.public(file_id), "allowed_agent_ids": allowed})

    def patch_attachment(self, sid: str, file_id: str, *, allowed: Any = "__keep__", enabled: Optional[bool] = None,
                         required: Optional[bool] = None, allow_partial: Optional[bool] = None) -> None:
        a = self.db.one("SELECT * FROM session_attachments WHERE session_id=? AND file_id=?", (sid, file_id))
        if not a:
            raise SessionError(404, "添付が見つかりません")
        vals: dict[str, Any] = {"attach_version": a["attach_version"] + 1}
        reason = "updated"
        if allowed != "__keep__":
            vals["allowed_agent_ids_json"] = allowed
            reason = "acl_changed"
        if enabled is not None:
            vals["enabled"] = int(enabled)
            reason = "disabled" if not enabled else reason
        if required is not None:
            vals["required"] = int(required)
        if allow_partial is not None:
            vals["allow_partial"] = int(allow_partial)
        self.db.update("session_attachments", {"session_id": sid, "file_id": file_id}, vals)
        self.packets.invalidate_for_files(sid, {file_id}, reason)
        self.mutate(sid, lambda p: p.__setitem__("needs_context_refresh", True))
        self.bus.publish(sid, "file.attached", {"file": self.files.public(file_id), "updated": True})

    def detach(self, sid: str, file_id: str) -> None:
        a = self.db.one("SELECT * FROM session_attachments WHERE session_id=? AND file_id=?", (sid, file_id))
        if not a:
            raise SessionError(404, "添付が見つかりません")
        self.db.execute("UPDATE session_attachments SET enabled=0 WHERE session_id=? AND file_id=?", (sid, file_id))
        self.packets.invalidate_for_files(sid, {file_id}, "detached")
        self.db.execute("DELETE FROM session_attachments WHERE session_id=? AND file_id=?", (sid, file_id))
        self.mutate(sid, lambda p: p.__setitem__("needs_context_refresh", True))
        self.bus.publish(sid, "file.detached", {"file_id": file_id})

    def reanalyze(self, sid: str, file_id: str) -> None:
        if not self.db.one("SELECT 1 FROM session_attachments WHERE session_id=? AND file_id=?", (sid, file_id)):
            raise SessionError(404, "添付が見つかりません")
        self.packets.invalidate_for_files(sid, {file_id}, "reanalyze")
        self.mutate(sid, lambda p: p.__setitem__("needs_context_refresh", True))

    def _on_file_status(self, file_id: str, status: str) -> None:
        if status == "DELETED":
            for r in self.db.all("SELECT session_id FROM session_attachments WHERE file_id=?", (file_id,)):
                self.packets.invalidate_for_files(r["session_id"], {file_id}, "deleted")
                self.mutate(r["session_id"], lambda p: p.__setitem__("needs_context_refresh", True))

    def _on_evidence_terminal(self, sid: str, er_id: str, action: str) -> None:
        if action == "finish_with_unresolved":
            def f(p: dict) -> None:
                p["finish_requested"] = True
            self.mutate(sid, f)
        if not self.evidence.open_requests(sid) and self.row(sid)["state"] == "WAITING_USER_EVIDENCE":
            self.set_state(sid, "RESUMING")
            self.spawn(sid)

    # ================================================================ run loop
    async def _run(self, sid: str) -> None:
        ctx = self.ctx(sid)
        try:
            while True:
                r = self.row(sid)
                if r["state"] in TERMINAL:
                    return
                p = self.progress(sid)
                if p.get("pause_requested"):
                    self.mutate(sid, lambda x: x.pop("pause_requested", None))
                    self.set_state(sid, "PAUSED")
                    self.bus.publish(sid, "session.paused", {})
                    return
                if self.evidence.open_requests(sid):
                    self.set_state(sid, "WAITING_USER_EVIDENCE")
                    return
                if not p.get("context_ready") or p.get("needs_context_refresh"):
                    if not await self._prepare_context(ctx, initial=not p.get("context_ready")):
                        return
                    continue
                if p.get("retry_context_requests"):
                    rid = p["retry_context_requests"][0]
                    self.set_state(sid, "CHAIR_EVIDENCE_SEARCH")
                    res = await self.evidence._search_and_route(ctx, rid)
                    cr = self.db.one("SELECT * FROM context_requests WHERE request_id=?", (rid,))
                    self._queue_redo(sid, cr["agent_id"], cr["turn_message_id"], res)
                    self.mutate(sid, lambda x: x["retry_context_requests"].pop(0))
                    continue
                runnable = self._next_redo(sid)
                if runnable is not None:
                    await self._run_redo(ctx, runnable)
                    continue
                if p.get("pending_redo"):
                    self.set_state(sid, "WAITING_USER_EVIDENCE")
                    return
                if p.get("finish_requested") and p["phase"] not in ("CHECKPOINT", "AWAIT_DECISION", "FINALIZING"):
                    self.mutate(sid, lambda x: x.update(phase="CHECKPOINT", finish_after_checkpoint=True))
                    continue
                phase = p["phase"]
                if phase == "PREP":
                    self.mutate(sid, lambda x: x.__setitem__("phase", "OPENING"))
                elif phase == "OPENING":
                    self.set_state(sid, "OPENING")
                    await self._chair_turn(ctx, "OPENING")
                    self.mutate(sid, lambda x: x.__setitem__("phase", "INDEPENDENT"))
                elif phase == "INDEPENDENT":
                    self.set_state(sid, "INDEPENDENT")
                    await self._independent(ctx)
                    if not self.progress(sid).get("pending_redo") and set(self.progress(sid)["independent_done"]) >= {a.id for a in ctx.participants()}:
                        self.mutate(sid, lambda x: x.__setitem__("phase", "CHAIR_REVIEW"))
                elif phase == "CHAIR_REVIEW":
                    self.set_state(sid, "CHAIR_REVIEW")
                    await self._chair_turn(ctx, "CHAIR_REVIEW")
                    self.mutate(sid, lambda x: x.update(phase="DEBATE", round=1, debate_done=[]))
                elif phase == "DEBATE":
                    self.set_state(sid, "DEBATE")
                    todo = [a for a in ctx.participants() if a.id not in p["debate_done"]]
                    if todo:
                        agent = todo[0]
                        redo = await self._participant_turn(ctx, agent, "DEBATE", p["round"])
                        self.mutate(sid, lambda x: x["debate_done"].append(agent.id))
                        if redo:
                            self._queue_redo(sid, agent.id, redo["message_id"], redo["result"], "DEBATE", p["round"])
                        continue
                    await self._round_end_compression(ctx)
                    if p["round"] < p["rounds_target"]:
                        self.mutate(sid, lambda x: x.update(round=x["round"] + 1, debate_done=[]))
                    else:
                        self.mutate(sid, lambda x: x.__setitem__("phase", "CHECKPOINT"))
                elif phase == "CHECKPOINT":
                    self.set_state(sid, "CHECKPOINT_REPORT")
                    await self._checkpoint(ctx)
                    if self.progress(sid).get("finish_after_checkpoint"):
                        cp = self.db.one("SELECT id FROM debate_checkpoints WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (sid,))
                        self.db.update("debate_checkpoints", {"id": cp["id"]}, {"decision": "finish", "decided_by": "user",
                                                                                "decided_at": now_iso(),
                                                                                "instruction": "資料不足カードで「未解決のまま終了」を選択"})
                        self.bus.publish(sid, "checkpoint.decision_received", {"checkpoint_id": cp["id"], "decision": "finish"})
                        self.mutate(sid, lambda x: x.update(phase="FINALIZING", finish_requested=False))
                        continue
                    self.mutate(sid, lambda x: x.__setitem__("phase", "AWAIT_DECISION"))
                    self.set_state(sid, "WAITING_USER_DECISION")
                    self.bus.publish(sid, "checkpoint.awaiting_user", {})
                    return
                elif phase == "AWAIT_DECISION":
                    self.set_state(sid, "WAITING_USER_DECISION")
                    return
                elif phase == "FINALIZING":
                    self.set_state(sid, "FINALIZING")
                    await self._finalize(ctx)
                    return
                else:
                    raise RuntimeError(f"unknown phase {phase}")
        except BudgetPaused as e:
            self.set_blockers(sid, [{"type": "budget", "hard": e.hard, "reasons": e.reasons,
                                     "message": "予算・利用可能モデルの不足により停止しました"}])
            self.set_state(sid, "PAUSED_BUDGET")
            self.bus.publish(sid, "session.paused_budget", {"reasons": e.reasons, "hard": e.hard})
        except (QualityFailed, TurnFailed) as e:
            self.set_blockers(sid, [{"type": "quality" if isinstance(e, QualityFailed) else "turn_failed",
                                     "agent_id": e.agent_id, "kind": e.kind, "message": str(e)}])
            self.set_state(sid, "PAUSED")
            self.bus.publish(sid, "error", {"agent_id": e.agent_id, "kind": e.kind, "message": str(e)})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("session %s failed", sid)
            self.set_state(sid, "FAILED", error=str(e)[:500], ended_at=now_iso())
            self.bus.publish(sid, "session.failed", {"error": str(e)[:500]})

    async def _prepare_context(self, ctx: SessionCtx, initial: bool) -> bool:
        sid = ctx.id
        self.set_state(sid, "DOCUMENT_PREP")
        if not self.packets.attachments(sid):
            self.mutate(sid, lambda x: x.update(context_ready=True, needs_context_refresh=False))
            return True
        self.set_state(sid, "CHAIR_DOCUMENT_ANALYSIS")
        res = await self.chair.build_packets(ctx, reason="initial" if initial else "refresh")
        if res["blockers"]:
            self.set_blockers(sid, res["blockers"])
            self.set_state(sid, "PAUSED")
            for b in res["blockers"]:
                self.bus.publish(sid, "warning", {"message": b["message"]})
            return False
        self.set_state(sid, "CONTEXT_DISTRIBUTION")
        for a in ctx.participants():
            pk = self.packets.current(sid, a.id)
            if pk:
                try:
                    self.packets.validate_for_delivery(sid, a.id, pk)
                except PacketInvalid as e:
                    raise TurnFailed(f"Packet検証に失敗: {e}", "packet_invalid", a.id) from e
        self.mutate(sid, lambda x: x.update(context_ready=True, needs_context_refresh=False))
        return True

    # ---------------------------------------------------------------- visibility
    def _allowed_ids(self, sid: str, agent_id: str) -> set[str]:
        return {a["file_id"] for a in self.packets.attachments(sid, include_disabled=True)
                if a["allowed_agent_ids"] is None or agent_id in a["allowed_agent_ids"]}

    def _common_ids(self, ctx: SessionCtx) -> set[str]:
        sets = [self._allowed_ids(ctx.id, a.id) for a in ctx.participants()]
        return set.intersection(*sets) if sets else set()

    def _history(self, ctx: SessionCtx, allowed: set[str], *, independent: bool = False,
                 include_restricted_note: bool = True) -> tuple[Optional[str], list[dict]]:
        summ = self.compression.latest(ctx.id)
        after = summ["to_seq"] if summ else 0
        rows = self.db.all("SELECT * FROM messages WHERE session_id=? AND status='completed' AND kind IN ('turn','chair','user') ORDER BY seq",
                           (ctx.id,))
        out = []
        for m in rows:
            scope = set(dec(m["scope_json"], []))
            visible = scope <= allowed
            if independent and m["kind"] == "turn":
                continue
            if m["seq"] <= after and (visible and scope <= self._common_ids(ctx)):
                continue  # covered by the shared summary
            speaker = self._speaker(ctx, m)
            if not visible:
                if include_restricted_note:
                    out.append({"speaker": speaker, "round": m["round"], "seq": m["seq"], "kind": m["kind"],
                                "text": "（この発言は閲覧権限のない資料に基づくため省略されています）"})
                continue
            text, cr = parse_context_request(m["content"])
            if cr:
                text += "\n（※議長へ追加根拠を要求）"
            out.append({"speaker": speaker, "round": m["round"], "seq": m["seq"], "kind": m["kind"], "text": text,
                        "content": text})
        return (summ["summary"] if summ else None), out

    def _speaker(self, ctx: SessionCtx, m: dict) -> str:
        if m["kind"] == "user":
            return "ユーザー介入"
        a = ctx.agents.get(m["agent_id"] or "")
        return f"{a.name}（{ctx.role_name(a)}）" if a else "システム"

    def _forbidden_excerpts(self, ctx: SessionCtx, agent_id: str) -> list[str]:
        allowed = self._allowed_ids(ctx.id, agent_id)
        out: list[str] = []
        for a in self.packets.attachments(ctx.id, include_disabled=True):
            if a["file_id"] in allowed:
                continue
            for c in self.db.all("SELECT text FROM file_chunks WHERE file_id=? ORDER BY seq LIMIT 50", (a["file_id"],)):
                for line in c["text"].split("\n"):
                    line = line.strip()
                    if len(line) >= 16:
                        out.append(line[:40])
        return out[:400]

    # ---------------------------------------------------------------- turns
    async def _participant_turn(self, ctx: SessionCtx, agent: AgentConfig, phase: str, round_: Optional[int],
                                note: Optional[str] = None) -> Optional[dict]:
        """One participant turn; on a context-length overflow the shared history is compressed and retried once."""
        try:
            return await self._participant_turn_once(ctx, agent, phase, round_, note)
        except (TurnFailed, BudgetPaused) as e:
            overflow = (isinstance(e, TurnFailed) and e.kind == "context_overflow") or (
                isinstance(e, BudgetPaused) and any("コンテキスト長" in r for r in e.reasons))
            if not overflow:
                raise
            self.bus.publish(ctx.id, "warning", {"agent_id": agent.id,
                                                 "message": f"{agent.name}: 入力がモデルのコンテキスト長を超えたため、古い履歴を要約して再試行します"})
            self.mutate(ctx.id, lambda x: x.__setitem__("compress_requested", True))
            await self._round_end_compression(ctx)
            return await self._participant_turn_once(ctx, agent, phase, round_, note)

    async def _participant_turn_once(self, ctx: SessionCtx, agent: AgentConfig, phase: str, round_: Optional[int],
                                     note: Optional[str] = None) -> Optional[dict]:
        sid = ctx.id
        packet = self.packets.current(sid, agent.id)
        if packet is None and self.packets.latest_any(sid, agent.id) is not None:
            await self.chair.build_packets(ctx, reason="refresh", only_agents={agent.id})
            packet = self.packets.current(sid, agent.id)
        if packet is not None:
            try:
                self.packets.validate_for_delivery(sid, agent.id, packet)
            except PacketInvalid:
                await self.chair.build_packets(ctx, reason="acl_revalidation", only_agents={agent.id})
                packet = self.packets.current(sid, agent.id)
                if packet is not None:
                    self.packets.validate_for_delivery(sid, agent.id, packet)  # raises → turn fails
        allowed = self._allowed_ids(sid, agent.id)
        summary, hist = self._history(ctx, allowed, independent=(phase == "INDEPENDENT"))
        s = ctx.settings
        roster = [{"name": a.name, "role_name": ctx.role_name(a)} for a in [ctx.chair] + ctx.participants()]
        p = self.progress(sid)
        hint = {"evidence_ids": [e["id"] for e in (packet or {}).get("evidence", [])],
                "demo_context_request": "#根拠不足デモ" in ctx.topic,
                "already_requested": bool(self.db.one("SELECT 1 FROM context_requests WHERE session_id=? AND agent_id=?", (sid, agent.id)))}

        def build(model: str, max_out: int) -> LLMRequest:
            return build_participant_request(
                agent={"name": agent.name, "role_name": ctx.role_name(agent)}, role_md=ctx.role_md(agent),
                topic=ctx.topic, participants=roster, round_=round_, phase=phase, summary=summary, history=hist,
                packet=packet, note=note, model=model, temperature=s.debate.temperature, max_output=max_out,
                timeout_s=s.debate.request_timeout_s, metadata={"fake_hint": hint, "phase": phase, "round": round_})

        sample = build("x", s.debate.max_output_tokens)
        est = estimate_tokens(sample.system) + sum(estimate_tokens(m["content"]) for m in sample.messages)
        ev_ids = {e["id"] for e in (packet or {}).get("evidence", [])}
        scope = self.packets.scope_files(packet)
        res = await self.runner.run(
            ctx, agent, purpose="turn", build=build, est_input=est,
            stream_message={"kind": "turn", "phase": phase if phase != "REDO" else p.get("phase"), "round": round_,
                            "scope": scope, "meta": {"redo": phase == "REDO"},
                            "packet_refs": [{"packet_id": packet["packet_id"], "packet_version": packet["packet_version"]}] if packet else []},
            needs_document_transfer=bool(packet and packet.get("evidence")), participant_guard=True,
            forbidden_excerpts=self._forbidden_excerpts(ctx, agent.id), allowed_evidence_ids=ev_ids)
        if packet is not None:
            self.packets.deliver(sid, agent.id, packet, request_id=res.message_id)
        bad = sorted(set(_EV.findall(res.text)) - ev_ids)
        if bad:
            self._merge_meta(res.message_id, {"invalid_citations": bad})
            self.bus.publish(sid, "warning", {"agent_id": agent.id, "message_id": res.message_id,
                                              "message": f"配布Packetにない出典IDが引用されました: {', '.join(bad)}（未検証として扱います）"})
        _clean, cr = parse_context_request(res.text)
        if cr is None:
            return None
        self._merge_meta(res.message_id, {"context_request": cr})
        self.set_state(sid, "CONTEXT_REQUESTED")
        try:
            result = await self.evidence.handle_context_request(ctx, agent.id, res.message_id, cr)
        except BudgetPaused:
            row = self.db.one("SELECT request_id FROM context_requests WHERE session_id=? AND turn_message_id=?", (sid, res.message_id))
            if row:
                self.mutate(sid, lambda x: x.setdefault("retry_context_requests", []).append(row["request_id"]))
            raise
        if result["status"] in ("invalid", "loop_stopped", "duplicate"):
            return None
        return {"message_id": res.message_id, "result": result}

    def _merge_meta(self, mid: Optional[str], extra: dict) -> None:
        if not mid:
            return
        r = self.db.one("SELECT meta_json FROM messages WHERE id=?", (mid,))
        m = dec(r["meta_json"], {}) if r else {}
        m.update(extra)
        self.db.update("messages", {"id": mid}, {"meta_json": m})

    def _queue_redo(self, sid: str, agent_id: str, message_id: str, result: dict, phase: str | None = None,
                    round_: int | None = None) -> None:
        if result.get("status") not in ("resolved", "waiting"):
            return
        p = self.progress(sid)
        entry = {"agent_id": agent_id, "origin_message_id": message_id, "phase": phase or p.get("phase"),
                 "round": round_ if round_ is not None else p.get("round"),
                 "evidence_request_id": result.get("evidence_request_id"),
                 "context_request_id": result.get("context_request_id")}
        self.mutate(sid, lambda x: x["pending_redo"].append(entry))

    def _next_redo(self, sid: str) -> Optional[dict]:
        for e in self.progress(sid).get("pending_redo", []):
            er = e.get("evidence_request_id")
            if not er:
                return e
            row = self.db.one("SELECT state FROM evidence_requests WHERE id=?", (er,))
            if row and row["state"] not in WAITING_STATES:
                return {**e, "er_state": row["state"]}
        return None

    async def _run_redo(self, ctx: SessionCtx, e: dict) -> None:
        notes = {
            None: "議長が許可済み資料から追加根拠を確認し、Packetを更新しました。",
            "RESOLVED": "ユーザーの回答を受けて議長がPacketを更新しました（ユーザー申告は未検証として扱うこと）。",
            "DEFERRED": "ユーザーは必要な資料を提供できませんでした。該当事項はUNKNOWN/仮説のまま扱い、確定事項として述べないこと。",
            "WITHDRAWN": "ユーザーの判断によりこの追加根拠要求は取り下げられました。",
        }
        agent = ctx.agents[e["agent_id"]]
        self.set_state(ctx.id, "DEBATE" if e.get("phase") == "DEBATE" else "INDEPENDENT")
        redo = await self._participant_turn(ctx, agent, "REDO", e.get("round"), note=notes.get(e.get("er_state")))
        self.mutate(ctx.id, lambda x: x.__setitem__("pending_redo", [r for r in x["pending_redo"] if r["origin_message_id"] != e["origin_message_id"]]))
        self._merge_meta(e["origin_message_id"], {"superseded_by_redo": True})
        if redo:
            self._queue_redo(ctx.id, agent.id, redo["message_id"], redo["result"], e.get("phase"), e.get("round"))

    async def _independent(self, ctx: SessionCtx) -> None:
        sid = ctx.id
        done = set(self.progress(sid)["independent_done"])
        todo = [a for a in ctx.participants() if a.id not in done]
        if not todo:
            return
        sem = asyncio.Semaphore(ctx.settings.debate.max_parallel_requests)

        async def one(a: AgentConfig):
            async with sem:
                return a, await self._participant_turn(ctx, a, "INDEPENDENT", None)

        results = await asyncio.gather(*(one(a) for a in todo), return_exceptions=True)
        first_exc: Optional[BaseException] = None
        for res in results:
            if isinstance(res, BaseException):
                first_exc = first_exc or res
                continue
            a, redo = res
            self.mutate(sid, lambda x, aid=a.id: x["independent_done"].append(aid) if aid not in x["independent_done"] else None)
            if redo:
                self._queue_redo(sid, a.id, redo["message_id"], redo["result"], "INDEPENDENT", None)
        if first_exc is not None:  # barrier: successful turns are kept, failed ones retried/skipped by the user
            raise first_exc

    async def _chair_turn(self, ctx: SessionCtx, phase: str) -> None:
        chair = ctx.chair
        common = self._common_ids(ctx)
        summary, hist = self._history(ctx, common, include_restricted_note=False)
        facts = self._common_facts(ctx)
        s = ctx.settings
        roster = "\n".join(f"- {a.name}（{ctx.role_name(a)}）" for a in ctx.participants())
        task = {"OPENING": "討論の開始を宣言し、議題を論点に分解して、各Agentに独立提案を促す短い発言をせよ。",
                "CHAIR_REVIEW": "各Agentの独立提案を整理し、合意点と対立点を明示して、次の討論で検討すべき論点を示せ。"}[phase]
        user = (f"## 議題\n{ctx.topic}\n## 参加Agent\n{roster}\n"
                + (f"## 要約\n{summary}\n" if summary else "")
                + f"## 確定済み会話\n{render_history(hist)}\n## 全員に配布済みの資料要点\n"
                + ("\n".join(f"- {f['claim']} [{', '.join(f['evidence_ids'])}]" for f in facts) or "（なし）")
                + f"\n\n{task}")
        system = chair_system(ctx.role_md(chair))

        def build(model: str, max_out: int) -> LLMRequest:
            return LLMRequest(model=model, system=system, messages=[{"role": "user", "content": user}],
                              temperature=s.debate.temperature, max_output_tokens=max_out,
                              timeout_s=s.debate.request_timeout_s,
                              metadata={"phase": phase, "fake_hint": {"evidence_ids": [i for f in facts for i in f["evidence_ids"]]}})

        await self.runner.run(ctx, chair, purpose="chair_turn", build=build,
                              est_input=estimate_tokens(system) + estimate_tokens(user),
                              stream_message={"kind": "chair", "phase": phase, "round": self.progress(ctx.id).get("round") or None,
                                              "scope": []},
                              needs_document_transfer=bool(facts))

    def _common_facts(self, ctx: SessionCtx) -> list[dict]:
        packets = [self.packets.current(ctx.id, a.id) for a in ctx.participants()]
        if not packets or any(p is None for p in packets):
            return []
        common = self._common_ids(ctx)
        first = packets[0]
        out = []
        for f in first.get("facts", []):
            if all(any(g["claim"] == f["claim"] for g in p.get("facts", [])) for p in packets[1:]):
                ev = self.packets.evidence(f["evidence_ids"])
                if all(e["file_id"] in common for e in ev.values()):
                    out.append(f)
        return out

    async def _round_end_compression(self, ctx: SessionCtx) -> None:
        common = self._common_ids(ctx)
        rows = self.db.all("SELECT * FROM messages WHERE session_id=? AND status='completed' AND kind IN ('turn','chair','user') ORDER BY seq", (ctx.id,))
        shareable = []
        for m in rows:
            if set(dec(m["scope_json"], [])) <= common:
                text, _ = parse_context_request(m["content"])
                shareable.append({"seq": m["seq"], "kind": m["kind"], "speaker": self._speaker(ctx, m), "content": text})
        force = bool(self.progress(ctx.id).get("compress_requested"))
        await self.compression.maybe_compress(ctx, shareable, force=force)
        if force:
            self.mutate(ctx.id, lambda x: x.pop("compress_requested", None))

    def request_compression(self, sid: str) -> None:
        self.mutate(sid, lambda x: x.__setitem__("compress_requested", True))

    # ---------------------------------------------------------------- checkpoint / final
    async def _chair_json(self, ctx: SessionCtx, purpose: str, instructions: str, extra: str, hint: dict) -> dict:
        chair = ctx.chair
        common = self._common_ids(ctx)
        summary, hist = self._history(ctx, common, include_restricted_note=False)
        user = (f"## 議題\n{ctx.topic}\n" + (f"## 要約\n{summary}\n" if summary else "")
                + f"## 確定済み会話\n{render_history(hist)}\n{extra}")
        system = chair_system(ctx.role_md(chair)) + "\n\n" + instructions

        def build(model: str, max_out: int) -> LLMRequest:
            return LLMRequest(model=model, system=system, messages=[{"role": "user", "content": user}], temperature=0.3,
                              max_output_tokens=max(max_out, 256), timeout_s=ctx.settings.debate.request_timeout_s,
                              json_mode=True, metadata={"fake_hint": hint})

        res = await self.runner.run(ctx, chair, purpose=purpose, build=build,
                                    est_input=estimate_tokens(system) + estimate_tokens(user), json_required=True,
                                    needs_document_transfer=bool(self._common_facts(ctx)),
                                    validator=lambda t: None if parse_json_loose(t) is not None else "JSONとして解析できません")
        return parse_json_loose(res.text) or {}

    def _evidence_status_text(self, sid: str) -> tuple[str, list[str]]:
        lines, missing = [], []
        for er in self.evidence.list(sid):
            if er["state"] in ("RESOLVED", "WITHDRAWN"):
                continue
            q = er.get("agent_question") or er["question"]
            missing.append(f"{q}（{er['criticality']} / {er['state']}）")
            lines.append(f"- {q} [{er['criticality']}, {er['state']}, {er['reason_code']}]")
        return ("## 未解決の根拠要求\n" + ("\n".join(lines) or "なし")), missing

    async def _checkpoint(self, ctx: SessionCtx) -> None:
        sid = ctx.id
        self.bus.publish(sid, "checkpoint.started", {"round": self.progress(sid).get("round")})
        prev = self.db.one("SELECT report_json FROM debate_checkpoints WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (sid,))
        ev_text, missing = self._evidence_status_text(sid)
        extra = ev_text + ("\n## 前回の中間報告\n" + prev["report_json"] if prev else "")
        out = await self._chair_json(ctx, "checkpoint", CHECKPOINT_INSTRUCTIONS, extra, {"missing_evidence": missing})
        report = {k: out.get(k, v) for k, v in CHECKPOINT_KEYS.items()}
        for k, v in CHECKPOINT_KEYS.items():
            if isinstance(v, list) and not isinstance(report[k], list):
                report[k] = [str(report[k])] if report[k] else []
        report["missing_evidence"] = list(dict.fromkeys([*report["missing_evidence"], *missing]))
        report["round"] = self.progress(sid).get("round")
        report["evidence_blockers"] = [b for b in self.blockers(sid) if b["type"] == "evidence"]
        report["insufficient_evidence"] = bool(report["missing_evidence"])
        cp_id = new_id("cp")
        self.db.insert("debate_checkpoints", {"id": cp_id, "session_id": sid, "round": report["round"],
                                              "report_json": report, "created_at": now_iso()})
        mid = new_id("m")
        self.db.insert("messages", {"id": mid, "session_id": sid, "agent_id": ctx.chair.id, "seq": self.runner.next_seq(sid),
                                    "round": report["round"], "phase": "CHECKPOINT", "kind": "checkpoint",
                                    "content": json.dumps(report, ensure_ascii=False), "status": "completed",
                                    "created_at": now_iso(), "completed_at": now_iso(), "scope_json": [],
                                    "meta_json": {"checkpoint_id": cp_id}})
        self.bus.publish(sid, "checkpoint.reported", {"checkpoint_id": cp_id, "message_id": mid, "report": report})

    def checkpoint(self, sid: str) -> dict:
        cp = self.db.one("SELECT * FROM debate_checkpoints WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (sid,))
        if not cp:
            raise SessionError(404, "中間報告はまだありません")
        return {"id": cp["id"], "round": cp["round"], "report": dec(cp["report_json"], {}), "decision": cp["decision"],
                "instruction": cp["instruction"], "decided_at": cp["decided_at"], "created_at": cp["created_at"],
                "options": ["continue", "redirect", "finish"], "awaiting": self.row(sid)["state"] == "WAITING_USER_DECISION",
                "evidence_blocking_continue": bool(self.evidence.open_requests(sid))}

    async def _finalize(self, ctx: SessionCtx) -> None:
        sid = ctx.id
        cp = self.db.one("SELECT * FROM debate_checkpoints WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (sid,))
        if not cp or cp["decision"] != "finish":  # never finalise without a checkpoint + explicit finish
            raise RuntimeError("中間報告とユーザーの終了判断なしに最終統合へは進めません")
        sections = self.evidence.report_sections(sid, [a.id for a in ctx.participants()])
        ev_text, _ = self._evidence_status_text(sid)
        extra = (ev_text + "\n## 最終チェックポイント\n" + (cp["report_json"] or "")
                 + f"\n## 結論の状態（サーバー判定）\n{sections['conclusion_status']}")
        out = await self._chair_json(ctx, "final", FINAL_INSTRUCTIONS, extra,
                                     {"verified_findings": [v["claim"] for v in sections["verified_findings"]]})
        status = sections["conclusion_status"]
        adopted = str(out.get("adopted_proposal") or "")
        if status == "blocked":
            adopted = "【確定不可・未解決の重要事項あり】" + adopted
        elif status == "provisional":
            adopted = "【暫定】" + adopted
        report = {"adopted_proposal": adopted, "rationale": out.get("rationale", ""),
                  "minority_opinions": out.get("minority_opinions", []), "uncertainties": out.get("uncertainties", []),
                  "next_actions": out.get("next_actions", []), **sections,
                  "checkpoint_id": cp["id"], "skipped_turns": self.progress(sid).get("skipped", [])}
        mid = new_id("m")
        self.db.insert("messages", {"id": mid, "session_id": sid, "agent_id": ctx.chair.id, "seq": self.runner.next_seq(sid),
                                    "round": self.progress(sid).get("round"), "phase": "FINALIZING", "kind": "final",
                                    "content": json.dumps(report, ensure_ascii=False), "status": "completed",
                                    "created_at": now_iso(), "completed_at": now_iso(), "scope_json": [], "meta_json": {}})
        self.db.update("debate_sessions", {"id": sid}, {"final_report_json": report, "conclusion_status": status})
        self.set_state(sid, "COMPLETED", ended_at=now_iso())
        self.bus.publish(sid, "session.completed", {"state": "COMPLETED", "conclusion_status": status, "message_id": mid,
                                                    "report": report})
