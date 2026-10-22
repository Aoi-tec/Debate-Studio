"""EvidenceGapResolver + UserEvidenceInbox + provenance records (DESIGN 4.8).

context_request → chair search (ACL-bound) → resolved by source, or EvidenceRequest
→ WAITING_USER_EVIDENCE → user answer (idempotent, version-checked) → chair
revalidation → new packet → dependent turn resumes. Missing facts are never invented.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.core.ids import new_id, now_iso
from app.models.schemas import EvidenceResponseIn
from app.services.budget import BudgetPaused
from app.services.chair import ChairOrchestrator, SearchOutcome
from app.services.events import EventBus
from app.services.llm_runner import SessionCtx
from app.services.packets import PacketService
from app.storage.db import Database, dec

WAITING_STATES = ("WAITING_USER_EVIDENCE", "ANSWER_RECEIVED", "REVALIDATING", "STILL_MISSING")
TERMINAL_STATES = ("RESOLVED", "DEFERRED", "WITHDRAWN")


class EvidenceConflict(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class SearchedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str
    label: Optional[str] = None
    scope: str


class EvidenceRequestContract(BaseModel):
    """Mirror of contracts/evidence_request.schema.json (additionalProperties=false)."""
    model_config = ConfigDict(extra="forbid")
    evidence_request_id: str
    session_id: str
    origin_context_request_id: str
    state: Literal["OPEN", "SEARCHING", "WAITING_USER_EVIDENCE", "ANSWER_RECEIVED", "REVALIDATING",
                   "RESOLVED", "STILL_MISSING", "DEFERRED", "WITHDRAWN"]
    reason_code: Literal["NOT_FOUND", "UNREADABLE", "CONFLICTING", "ACCESS_DENIED", "INSUFFICIENT_DETAIL", "ANALYSIS_FAILED"]
    criticality: Literal["critical", "noncritical"]
    question: str = Field(min_length=1)
    why_needed: str
    searched_sources: list[SearchedSource]
    missing_fields: list[str]
    acceptable_inputs: list[Literal["file_upload", "user_statement", "unavailable", "withdraw",
                                    "continue_as_hypothesis", "finish_with_unresolved"]]
    impact_if_unresolved: str
    created_at: str


class EvidenceService:
    def __init__(self, db: Database, bus: EventBus, chair: ChairOrchestrator, packets: PacketService):
        self.db, self.bus, self.chair, self.packets = db, bus, chair, packets
        self.on_terminal = None  # callback(session_id, er_id) set by controller
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------ intake
    @staticmethod
    def normalise(cr: dict) -> Optional[dict]:
        q = str(cr.get("question") or "").strip()
        if not q:
            return None
        crit = cr.get("criticality")
        return {
            "topic": str(cr.get("topic") or "").strip()[:200],
            "question": q[:1000],
            "required_evidence": [str(x)[:200] for x in (cr.get("required_evidence") or []) if str(x).strip()][:10],
            "why_needed": str(cr.get("why_needed") or "").strip()[:1000],
            "criticality": crit if crit in ("critical", "noncritical") else "critical",
        }

    async def handle_context_request(self, ctx: SessionCtx, agent_id: str, turn_message_id: str, raw: dict) -> dict:
        cr = self.normalise(raw)
        if cr is None:
            self.bus.publish(ctx.id, "warning", {"agent_id": agent_id, "message": "context_request の形式が不正なため無視しました"})
            return {"status": "invalid"}
        key = hashlib.sha256(f"{agent_id}|{''.join(cr['question'].split())}".encode()).hexdigest()[:24]
        open_dup = self.db.one(
            "SELECT * FROM context_requests WHERE session_id=? AND dedupe_key=? AND status IN ('OPEN','SEARCHING','WAITING_USER_EVIDENCE')",
            (ctx.id, key))
        if open_dup:
            er = self.db.one("SELECT id FROM evidence_requests WHERE origin_context_request_id=?", (open_dup["request_id"],))
            return {"status": "waiting" if er else "duplicate", "evidence_request_id": er["id"] if er else None,
                    "context_request_id": open_dup["request_id"]}
        prior = self.db.one("SELECT COUNT(*) n FROM context_requests WHERE session_id=? AND dedupe_key=?", (ctx.id, key))["n"]
        if prior >= ctx.settings.chair.max_evidence_reasks + 1:
            self.bus.publish(ctx.id, "warning", {"agent_id": agent_id,
                                                 "message": "同一の追加根拠要求が繰り返されたため自動ループを停止しました"})
            return {"status": "loop_stopped"}
        packet = self.packets.current(ctx.id, agent_id)
        rid = new_id("cr")
        body = {"request_id": rid, "agent_id": agent_id, "turn_id": turn_message_id, **cr,
                "current_packet_id": packet["packet_id"] if packet else None,
                "current_packet_version": packet["packet_version"] if packet else None}
        self.db.insert("context_requests", {"request_id": rid, "session_id": ctx.id, "agent_id": agent_id,
                                            "turn_message_id": turn_message_id, "body_json": body, "status": "OPEN",
                                            "dedupe_key": key, "created_at": now_iso()})
        self.bus.publish(ctx.id, "chair.context_requested", {"context_request": body})
        self.bus.publish(ctx.id, "evidence.requested", {"context_request_id": rid, "agent_id": agent_id,
                                                         "question": cr["question"], "criticality": cr["criticality"]})
        return await self._search_and_route(ctx, rid)

    async def _search_and_route(self, ctx: SessionCtx, rid: str) -> dict:
        row = self.db.one("SELECT * FROM context_requests WHERE request_id=?", (rid,))
        body = dec(row["body_json"], {})
        agent_id = row["agent_id"]
        self.db.update("context_requests", {"request_id": rid}, {"status": "SEARCHING"})
        self.bus.publish(ctx.id, "evidence.searching", {"context_request_id": rid, "agent_id": agent_id})
        try:
            outcome = await self.chair.search_for_request(ctx, agent_id, body)
        except BudgetPaused:
            self.db.update("context_requests", {"request_id": rid}, {"status": "OPEN"})
            raise
        except Exception as e:  # noqa: BLE001 - chair/tool failure is not "no document"
            outcome = SearchOutcome(False, reason_code="ANALYSIS_FAILED", missing_fields=body.get("required_evidence", []),
                                    detail=f"議長の解析処理が失敗しました: {str(e)[:200]}")
        if outcome.resolved:
            self._resolve_with_facts(ctx, agent_id, outcome.facts, body)
            self.db.update("context_requests", {"request_id": rid}, {"status": "RESOLVED_BY_SOURCE"})
            self.bus.publish(ctx.id, "evidence.resolved", {"context_request_id": rid, "agent_id": agent_id,
                                                            "by": "source", "facts": len(outcome.facts)})
            return {"status": "resolved", "context_request_id": rid}
        er_id = self._create_evidence_request(ctx, rid, body, outcome)
        self.db.update("context_requests", {"request_id": rid}, {"status": "WAITING_USER_EVIDENCE"})
        return {"status": "waiting", "evidence_request_id": er_id, "context_request_id": rid}

    def _resolve_with_facts(self, ctx: SessionCtx, agent_id: str, facts: list[dict], cr: dict,
                            remove_blocked: Optional[str] = None) -> dict:
        prev = self.packets.current(ctx.id, agent_id) or self.packets.latest_any(ctx.id, agent_id) or {}
        merged = list(prev.get("facts", []))
        for f in facts:
            if f not in merged:
                merged.append(f)
        oq = [q for q in prev.get("open_questions", []) if cr["question"] not in q]
        blocked = [b for b in prev.get("blocked_evidence_request_ids", []) if b != remove_blocked]
        return self.packets.create(
            session_id=ctx.id, agent_id=agent_id, topic=ctx.topic,
            scope=prev.get("scope") or f"{ctx.agents[agent_id].name}への追加根拠",
            summary=prev.get("summary") or "議長が追加根拠を確認した。", facts=merged,
            assumptions=prev.get("assumptions", []), open_questions=oq,
            unreadable=prev.get("omitted_or_unreadable", []), user_assertions=prev.get("user_assertions", []),
            hypotheses=prev.get("hypotheses", []), blocked_ids=blocked,
            target_tokens=ctx.settings.chair.agent_packet_target_tokens)

    def _create_evidence_request(self, ctx: SessionCtx, rid: str, cr: dict, o: SearchOutcome) -> str:
        er_id = new_id("er")
        crit = cr["criticality"]
        acceptable = ["file_upload", "user_statement", "unavailable", "withdraw"]
        if crit == "noncritical":
            acceptable.append("continue_as_hypothesis")
        acceptable.append("finish_with_unresolved")
        agent_name = ctx.agents[cr["agent_id"]].name
        question = o.question_for_user or (
            f"{agent_name} の質問「{cr['question']}」に答えられる記載が、確認した資料にありません。"
            f"{o.needed_materials or '該当事項が明記された資料'}を追加するか、文章で回答してください。")
        if o.reason_code == "CONFLICTING" and o.conflicts:
            ev = self.packets.evidence([i for c in o.conflicts for i in c.get("evidence_ids", [])])
            pairs = " / ".join(", ".join(ev[i]["source_label"] for i in c.get("evidence_ids", []) if i in ev) for c in o.conflicts)
            question = f"資料間で内容が矛盾しています（{pairs}）。どの版・基準日の値が正しいか確認してください。"
        why = cr.get("why_needed") or "判断に必要な根拠が不足しています"
        impact = ("この論点の確定結論は保留され、最終レポートは blocked になります" if crit == "critical"
                  else "この論点は未確認のまま扱われ、最終レポートは provisional になります")
        body = EvidenceRequestContract(
            evidence_request_id=er_id, session_id=ctx.id, origin_context_request_id=rid,
            state="WAITING_USER_EVIDENCE", reason_code=o.reason_code or "NOT_FOUND", criticality=crit,
            question=question, why_needed=why, searched_sources=[SearchedSource(**s) for s in o.searched_sources],
            missing_fields=o.missing_fields or cr.get("required_evidence", []), acceptable_inputs=acceptable,
            impact_if_unresolved=impact, created_at=now_iso()).model_dump()
        extra = {"agent_id": cr["agent_id"], "agent_name": agent_name, "agent_question": cr["question"],
                 "detail": o.detail, "needed_materials": o.needed_materials,
                 "conflicts": o.conflicts, "search_run_id": o.run_id}
        packet = self.packets.current(ctx.id, cr["agent_id"])
        self.db.insert("evidence_requests", {
            "id": er_id, "session_id": ctx.id, "origin_context_request_id": rid, "agent_id": cr["agent_id"],
            "turn_message_id": cr.get("turn_id"), "body_json": {**body, "_extra": extra}, "state": "WAITING_USER_EVIDENCE",
            "reason_code": body["reason_code"], "criticality": crit, "version": 1, "ask_count": 1,
            "packet_id": packet["packet_id"] if packet else None, "packet_version": packet["packet_version"] if packet else None,
            "created_at": now_iso(), "updated_at": now_iso()})
        self._event(er_id, "SEARCHING", "WAITING_USER_EVIDENCE", "chair", o.run_id)
        # mark the gap inside the requester's packet (no other agent sees it)
        if packet is not None or self.packets.latest_any(ctx.id, cr["agent_id"]):
            prev = packet or self.packets.latest_any(ctx.id, cr["agent_id"])
            self.packets.create(
                session_id=ctx.id, agent_id=cr["agent_id"], topic=ctx.topic, scope=prev.get("scope", ""),
                summary=prev.get("summary", ""), facts=prev.get("facts", []), assumptions=prev.get("assumptions", []),
                open_questions=prev.get("open_questions", []) + [f"UNKNOWN（ユーザー確認待ち）: {cr['question']}"],
                unreadable=prev.get("omitted_or_unreadable", []), user_assertions=prev.get("user_assertions", []),
                hypotheses=prev.get("hypotheses", []), blocked_ids=prev.get("blocked_evidence_request_ids", []) + [er_id],
                target_tokens=ctx.settings.chair.agent_packet_target_tokens)
        self.bus.publish(ctx.id, "evidence.awaiting_user", {"evidence_request": self.public(er_id)})
        return er_id

    # ------------------------------------------------------------ queries
    def public(self, er_id: str) -> dict:
        r = self.db.one("SELECT * FROM evidence_requests WHERE id=?", (er_id,))
        if not r:
            return {}
        body = dec(r["body_json"], {})
        extra = body.pop("_extra", {})
        body["state"] = r["state"]
        responses = self.db.all("SELECT action, statement, file_ids_json, source_kind, verification_status, created_at FROM evidence_responses WHERE evidence_request_id=? ORDER BY created_at", (er_id,))
        return {**body, "version": r["version"], "ask_count": r["ask_count"], "updated_at": r["updated_at"],
                "agent_id": extra.get("agent_id"), "agent_name": extra.get("agent_name"),
                "agent_question": extra.get("agent_question"), "detail": extra.get("detail"),
                "needed_materials": extra.get("needed_materials"), "conflicts": extra.get("conflicts", []),
                "responses": [{**x, "file_ids": dec(x.pop("file_ids_json"), [])} for x in responses]}

    def list(self, session_id: str) -> list[dict]:
        return [self.public(r["id"]) for r in self.db.all(
            "SELECT id FROM evidence_requests WHERE session_id=? ORDER BY created_at", (session_id,))]

    def open_requests(self, session_id: str) -> list[dict]:
        q = ",".join("?" for _ in WAITING_STATES)
        return self.db.all(f"SELECT * FROM evidence_requests WHERE session_id=? AND state IN ({q})", (session_id, *WAITING_STATES))

    def _event(self, er_id: str, frm: str | None, to: str, actor: str, run_id: str | None = None,
               packet_id: str | None = None, key: str | None = None) -> None:
        self.db.insert("evidence_resolution_events", {
            "id": new_id("ere"), "evidence_request_id": er_id, "from_state": frm, "to_state": to, "actor": actor,
            "search_run_id": run_id, "packet_id": packet_id, "idempotency_key": key, "created_at": now_iso()})

    def _set_state(self, er_id: str, frm: str, to: str, actor: str, **kw: Any) -> int:
        with self.db.tx():
            r = self.db.conn.execute("SELECT version FROM evidence_requests WHERE id=?", (er_id,)).fetchone()
            v = r[0] + 1
            sets = {"state": to, "version": v, "updated_at": now_iso(), **{k: val for k, val in kw.items() if k in ("ask_count", "reason_code", "body_json")}}
            cols = ",".join(f"{k}=?" for k in sets)
            vals = [json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x for x in sets.values()]
            self.db.conn.execute(f"UPDATE evidence_requests SET {cols} WHERE id=?", (*vals, er_id))
        self._event(er_id, frm, to, actor, kw.get("run_id"), kw.get("packet_id"), kw.get("key"))
        return v

    # ------------------------------------------------------------ user responses
    async def respond(self, ctx: SessionCtx, er_id: str, inp: EvidenceResponseIn, files_service) -> dict:
        if inp.evidence_request_id != er_id:
            raise EvidenceConflict(422, "evidence_request_id が一致しません")
        dup = self.db.one("SELECT * FROM evidence_responses WHERE idempotency_key=?", (inp.idempotency_key,))
        if dup:
            if dup["evidence_request_id"] != er_id:
                raise EvidenceConflict(409, "同じ Idempotency-Key が別の要求に使われています")
            return {**dec(dup["result_json"], {}), "idempotent_replay": True}
        r = self.db.one("SELECT * FROM evidence_requests WHERE id=? AND session_id=?", (er_id, ctx.id))
        if not r:
            raise EvidenceConflict(404, "要求が見つかりません")
        if r["version"] != inp.expected_version:
            raise EvidenceConflict(409, f"版数が一致しません（現在 v{r['version']}）。画面を更新してください")
        if r["state"] not in ("WAITING_USER_EVIDENCE", "STILL_MISSING"):
            raise EvidenceConflict(409, f"この要求は現在 {r['state']} のため回答できません")
        body = dec(r["body_json"], {})
        if inp.action not in body.get("acceptable_inputs", []):
            raise EvidenceConflict(422, f"この要求では {inp.action} は選択できません")
        crit = r["criticality"]
        if inp.action == "continue_as_hypothesis" and crit != "noncritical":
            raise EvidenceConflict(422, "critical な不足は仮説として続行できません（保留または未解決のまま終了を選択）")
        if inp.action == "user_statement" and not (inp.statement or "").strip():
            raise EvidenceConflict(422, "文章回答が空です")
        if inp.action == "file_upload":
            if not inp.file_ids:
                raise EvidenceConflict(422, "file_ids が必要です")
            for fid in inp.file_ids:
                if not self.db.one("SELECT 1 FROM uploaded_files WHERE file_id=? AND deleted_at IS NULL", (fid,)):
                    raise EvidenceConflict(422, f"ファイル {fid} が見つかりません")
        source_kind = {"user_statement": "user_statement", "continue_as_hypothesis": "agent_hypothesis",
                       "file_upload": "document_extract"}.get(inp.action)
        verification = {"user_statement": "user_provided_unverified", "continue_as_hypothesis": "unverified",
                        "file_upload": "unverified"}.get(inp.action)
        resp_id = new_id("eresp")
        extra = dec(r["body_json"], {}).get("_extra", {})
        agent_id = r["agent_id"]
        question = extra.get("agent_question") or body.get("question")
        result: dict
        if inp.action == "file_upload":
            v = self._set_state(er_id, r["state"], "ANSWER_RECEIVED", "user", key=inp.idempotency_key)
            for fid in inp.file_ids:
                self.db.execute(
                    "INSERT OR IGNORE INTO session_attachments(session_id, file_id, attached_at, allowed_agent_ids_json, enabled, required, allow_partial) VALUES (?,?,?,?,1,0,1)",
                    (ctx.id, fid, now_iso(), None))
                self.bus.publish(ctx.id, "file.attached", {"file": files_service.public(fid), "evidence_request_id": er_id})
            v = self._set_state(er_id, "ANSWER_RECEIVED", "REVALIDATING", "chair")
            result = {"state": "REVALIDATING", "version": v}
        elif inp.action == "user_statement":
            self._provenance(ctx.id, "user_statement", "user_provided_unverified", inp.statement or "", er_id, crit)
            prev = self.packets.current(ctx.id, agent_id) or self.packets.latest_any(ctx.id, agent_id) or {}
            ua = prev.get("user_assertions", []) + [{"text": f"{question} → {inp.statement}", "evidence_request_id": er_id,
                                                      "source_kind": "user_statement", "verification_status": "user_provided_unverified"}]
            self._repacket(ctx, agent_id, prev, er_id, question, user_assertions=ua)
            v = self._set_state(er_id, r["state"], "RESOLVED", "user", key=inp.idempotency_key)
            result = {"state": "RESOLVED", "version": v, "resolution": "user_statement"}
        elif inp.action == "continue_as_hypothesis":
            text = (inp.statement or question or "").strip()
            self._provenance(ctx.id, "agent_hypothesis", "unverified", text, er_id, crit)
            prev = self.packets.current(ctx.id, agent_id) or self.packets.latest_any(ctx.id, agent_id) or {}
            hy = prev.get("hypotheses", []) + [{"text": text, "evidence_request_id": er_id}]
            self._repacket(ctx, agent_id, prev, er_id, question, hypotheses=hy)
            v = self._set_state(er_id, r["state"], "DEFERRED", "user", key=inp.idempotency_key)
            result = {"state": "DEFERRED", "version": v, "resolution": "hypothesis"}
        elif inp.action in ("unavailable", "finish_with_unresolved"):
            self._provenance(ctx.id, "unresolved", "unverified", question or "", er_id, crit)
            prev = self.packets.current(ctx.id, agent_id) or self.packets.latest_any(ctx.id, agent_id) or {}
            self._repacket(ctx, agent_id, prev, er_id, question, unknown=True)
            v = self._set_state(er_id, r["state"], "DEFERRED", "user", key=inp.idempotency_key)
            result = {"state": "DEFERRED", "version": v, "resolution": inp.action}
        else:  # withdraw
            prev = self.packets.current(ctx.id, agent_id) or self.packets.latest_any(ctx.id, agent_id) or {}
            self._repacket(ctx, agent_id, prev, er_id, question)
            v = self._set_state(er_id, r["state"], "WITHDRAWN", "user", key=inp.idempotency_key)
            result = {"state": "WITHDRAWN", "version": v, "resolution": "withdraw"}
        self.db.insert("evidence_responses", {
            "id": resp_id, "evidence_request_id": er_id, "action": inp.action, "statement": inp.statement,
            "file_ids_json": inp.file_ids, "source_kind": source_kind, "verification_status": verification,
            "idempotency_key": inp.idempotency_key, "result_json": result, "created_at": now_iso()})
        self.bus.publish(ctx.id, "evidence.user_responded", {"evidence_request_id": er_id, "action": inp.action,
                                                              "state": result["state"]})
        if inp.action == "file_upload":
            self.bus.publish(ctx.id, "evidence.revalidating", {"evidence_request_id": er_id})
            self._tasks[er_id] = asyncio.get_event_loop().create_task(self._revalidate(ctx, er_id, inp.file_ids, files_service))
        else:
            if result["state"] == "RESOLVED":
                self.bus.publish(ctx.id, "evidence.resolved", {"evidence_request_id": er_id, "by": inp.action})
            else:
                self.bus.publish(ctx.id, "evidence.deferred", {"evidence_request_id": er_id, "by": inp.action})
            self.db.execute("UPDATE context_requests SET status=? WHERE request_id=?", (result["state"], r["origin_context_request_id"]))
            if self.on_terminal:
                self.on_terminal(ctx.id, er_id, inp.action)
        self.db.audit("evidence.respond", {"evidence_request_id": er_id, "action": inp.action})
        return result

    def _repacket(self, ctx: SessionCtx, agent_id: str, prev: dict, er_id: str, question: str | None, *,
                  user_assertions: list | None = None, hypotheses: list | None = None, unknown: bool = False) -> None:
        oq = [q for q in prev.get("open_questions", []) if not (question and question in q)]
        if unknown and question:
            oq.append(f"UNKNOWN（ユーザーは資料を提供できない）: {question}")
        self.packets.create(
            session_id=ctx.id, agent_id=agent_id, topic=ctx.topic, scope=prev.get("scope", ""),
            summary=prev.get("summary", ""), facts=prev.get("facts", []), assumptions=prev.get("assumptions", []),
            open_questions=oq, unreadable=prev.get("omitted_or_unreadable", []),
            user_assertions=user_assertions if user_assertions is not None else prev.get("user_assertions", []),
            hypotheses=hypotheses if hypotheses is not None else prev.get("hypotheses", []),
            blocked_ids=[b for b in prev.get("blocked_evidence_request_ids", []) if b != er_id],
            target_tokens=ctx.settings.chair.agent_packet_target_tokens)

    def _provenance(self, session_id: str, kind: str, status: str, text: str, er_id: str, crit: str) -> None:
        self.db.insert("provenance_items", {"id": new_id("prov"), "session_id": session_id, "source_kind": kind,
                                            "verification_status": status, "text": text, "evidence_request_id": er_id,
                                            "evidence_ids_json": [], "criticality": crit, "created_at": now_iso()})

    async def _revalidate(self, ctx: SessionCtx, er_id: str, file_ids: list[str], files_service) -> None:
        await files_service.wait(file_ids)
        r = self.db.one("SELECT * FROM evidence_requests WHERE id=?", (er_id,))
        body = dec(r["body_json"], {})
        extra = body.get("_extra", {})
        cr_row = self.db.one("SELECT * FROM context_requests WHERE request_id=?", (r["origin_context_request_id"],))
        cr = dec(cr_row["body_json"], {})
        try:
            outcome = await self.chair.search_for_request(ctx, r["agent_id"], cr)
        except BudgetPaused as e:
            outcome = SearchOutcome(False, reason_code="ANALYSIS_FAILED", detail=f"議長の予算不足で再検証できません: {e}",
                                    missing_fields=cr.get("required_evidence", []))
        except Exception as e:  # noqa: BLE001
            outcome = SearchOutcome(False, reason_code="ANALYSIS_FAILED", detail=f"再検証が失敗しました: {str(e)[:200]}",
                                    missing_fields=cr.get("required_evidence", []))
        if outcome.resolved:
            pkt = self._resolve_with_facts(ctx, r["agent_id"], outcome.facts, cr, remove_blocked=er_id)
            self._set_state(er_id, "REVALIDATING", "RESOLVED", "chair", run_id=outcome.run_id, packet_id=pkt["packet_id"])
            self.db.execute("UPDATE context_requests SET status='RESOLVED' WHERE request_id=?", (r["origin_context_request_id"],))
            self.bus.publish(ctx.id, "evidence.resolved", {"evidence_request_id": er_id, "by": "file_upload",
                                                            "packet_version": pkt["packet_version"]})
            if self.on_terminal:
                self.on_terminal(ctx.id, er_id, "file_upload")
            return
        ask = r["ask_count"] + 1
        new_body = dict(body)
        detail = outcome.detail or "追加資料でも確認できませんでした"
        new_body["reason_code"] = outcome.reason_code or "NOT_FOUND"
        new_body["searched_sources"] = outcome.searched_sources or body.get("searched_sources", [])
        new_body["_extra"] = {**extra, "detail": detail}
        if ask <= ctx.settings.chair.max_evidence_reasks + 1:
            new_body["question"] = f"（再確認 {ask - 1}回目）追加された資料でも確認できませんでした: {detail}。" + (
                outcome.question_for_user or "該当事項が明記された資料を追加するか、文章で回答してください。")
            new_body["state"] = "WAITING_USER_EVIDENCE"
            self._set_state(er_id, "REVALIDATING", "STILL_MISSING", "chair", run_id=outcome.run_id)
            self._set_state(er_id, "STILL_MISSING", "WAITING_USER_EVIDENCE", "chair", ask_count=ask,
                            reason_code=new_body["reason_code"], body_json=new_body)
        else:
            new_body["acceptable_inputs"] = [a for a in body.get("acceptable_inputs", []) if a != "file_upload"]
            new_body["question"] = f"再確認の上限に達しました: {detail}。文章で回答するか、未解決として扱ってください。"
            new_body["state"] = "STILL_MISSING"
            self._set_state(er_id, "REVALIDATING", "STILL_MISSING", "chair", ask_count=ask,
                            reason_code=new_body["reason_code"], body_json=new_body, run_id=outcome.run_id)
        self.bus.publish(ctx.id, "evidence.still_missing", {"evidence_request": self.public(er_id)})
        self.bus.publish(ctx.id, "evidence.awaiting_user", {"evidence_request": self.public(er_id)})

    # ------------------------------------------------------------ report helpers
    def report_sections(self, session_id: str, participants: list[str]) -> dict:
        verified: list[dict] = []
        seen = set()
        for aid in participants:
            p = self.packets.current(session_id, aid) or {}
            ev = {e["id"]: e for e in p.get("evidence", [])}
            for f in p.get("facts", []):
                if f["claim"] in seen:
                    continue
                seen.add(f["claim"])
                verified.append({"claim": f["claim"], "evidence": [ev[i]["source_label"] for i in f["evidence_ids"] if i in ev],
                                 "evidence_ids": f["evidence_ids"], "source_kind": "document_extract",
                                 "verification_status": "supported_by_source", "visible_to": [aid]})
        prov = self.db.all("SELECT * FROM provenance_items WHERE session_id=? ORDER BY created_at", (session_id,))
        user_assertions = [{"text": p["text"], "verification_status": p["verification_status"],
                            "evidence_request_id": p["evidence_request_id"]} for p in prov if p["source_kind"] == "user_statement"]
        hypotheses = [{"text": p["text"], "evidence_request_id": p["evidence_request_id"]} for p in prov if p["source_kind"] == "agent_hypothesis"]
        unresolved = []
        for r in self.db.all("SELECT * FROM evidence_requests WHERE session_id=? AND state NOT IN ('RESOLVED','WITHDRAWN')", (session_id,)):
            pub = self.public(r["id"])
            unresolved.append({"evidence_request_id": r["id"], "question": pub.get("agent_question") or pub.get("question"),
                               "criticality": r["criticality"], "state": r["state"], "reason_code": r["reason_code"]})
        if any(u["criticality"] == "critical" for u in unresolved):
            status = "blocked"
        elif unresolved or hypotheses:
            status = "provisional"
        else:
            status = "final"
        return {"verified_findings": verified, "user_assertions": user_assertions, "hypotheses": hypotheses,
                "unresolved_evidence": unresolved, "conclusion_status": status,
                "relies_on_user_assertions": bool(user_assertions)}
