"""ContextPacketBuilder / ContextDistributor / EvidenceProvenanceGuard (DESIGN 4.7, 4.8.6).

Packets are per-recipient, versioned, and validated against the evidence ledger,
current file versions and attachment ACLs right before every delivery.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Optional

from app.core.ids import new_id, now_iso
from app.core.tokens import estimate_tokens
from app.services.attachments import location_label
from app.services.context import render_packet
from app.services.events import EventBus
from app.storage.db import Database, dec


class PacketInvalid(Exception):
    pass


_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return s.replace(",", "")


def numbers_in(s: str) -> list[str]:
    return [n.replace(",", "") for n in _NUM.findall(_norm(s))]


def validate_facts(facts: Any, quotes: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Keep facts whose evidence ids exist and whose numbers appear in the cited quotes."""
    valid: list[dict] = []
    demoted: list[str] = []
    if not isinstance(facts, list):
        return valid, demoted
    for f in facts:
        if not isinstance(f, dict):
            continue
        claim = str(f.get("claim") or "").strip()
        ids = [i for i in (f.get("evidence_ids") or []) if isinstance(i, str)]
        if not claim:
            continue
        if not ids or any(i not in quotes for i in ids):
            demoted.append(f"{claim}（根拠IDが無効なため未確認扱い）")
            continue
        text = _norm(" ".join(quotes[i] for i in ids))
        missing = [n for n in numbers_in(claim) if n not in text]
        if missing:
            demoted.append(f"{claim}（数値 {', '.join(missing)} が出典本文と一致しないため未確認扱い）")
            continue
        conf = f.get("confidence") if f.get("confidence") in ("document_explicit", "document_inferred") else "document_inferred"
        valid.append({"claim": claim, "evidence_ids": ids, "confidence": conf,
                      "source_kind": "document_extract", "verification_status": "supported_by_source"})
    return valid, demoted


class PacketService:
    def __init__(self, db: Database, bus: EventBus):
        self.db = db
        self.bus = bus

    # ------------------------------------------------------------ ACL
    def attachments(self, session_id: str, include_disabled: bool = False) -> list[dict]:
        rows = self.db.all(
            """SELECT a.*, u.original_name, u.status, u.extract_version, u.kind, u.deleted_at
               FROM session_attachments a JOIN uploaded_files u ON u.file_id=a.file_id
               WHERE a.session_id=? ORDER BY a.attached_at""", (session_id,))
        out = []
        for r in rows:
            r["allowed_agent_ids"] = dec(r.pop("allowed_agent_ids_json"), None)
            if not include_disabled and (not r["enabled"] or r["deleted_at"]):
                continue
            out.append(r)
        return out

    def usable(self, att: dict) -> bool:
        return att["status"] == "READY" or (att["status"] == "PARTIAL" and bool(att["allow_partial"]))

    def allowed_files(self, session_id: str, agent_id: str, chair: bool = False) -> list[dict]:
        out = []
        for a in self.attachments(session_id):
            if not self.usable(a):
                continue
            if chair or a["allowed_agent_ids"] is None or agent_id in a["allowed_agent_ids"]:
                out.append(a)
        return out

    # ------------------------------------------------------------ evidence ledger
    def add_evidence(self, session_id: str, hit, file_name: str, allowed_agents: list[str] | None) -> str:
        existing = self.db.one(
            "SELECT evidence_id FROM chair_evidence WHERE session_id=? AND chunk_id=? AND file_version=? AND status='valid'",
            (session_id, hit.chunk_id, hit.file_version))
        if existing:
            return existing["evidence_id"]
        eid = new_id("ev")
        self.db.insert("chair_evidence", {
            "evidence_id": eid, "session_id": session_id, "file_id": hit.file_id, "file_version": hit.file_version,
            "chunk_id": hit.chunk_id, "location_json": hit.location, "source_label": location_label(file_name, hit.location),
            "quote": hit.text[:4000], "quality": hit.quality, "allowed_agents_json": allowed_agents,
            "status": "valid", "created_at": now_iso()})
        return eid

    def evidence(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        q = ",".join("?" for _ in ids)
        rows = self.db.all(f"SELECT * FROM chair_evidence WHERE evidence_id IN ({q})", ids)
        return {r["evidence_id"]: r for r in rows}

    # ------------------------------------------------------------ packets
    @staticmethod
    def packet_id(session_id: str, agent_id: str) -> str:
        return f"ctx_{session_id[-6:]}_{agent_id}"

    def current(self, session_id: str, agent_id: str) -> Optional[dict]:
        r = self.db.one(
            "SELECT * FROM context_packets WHERE session_id=? AND recipient_agent_id=? AND status='active' ORDER BY packet_version DESC LIMIT 1",
            (session_id, agent_id))
        return dec(r["body_json"]) if r else None

    def latest_any(self, session_id: str, agent_id: str) -> Optional[dict]:
        r = self.db.one(
            "SELECT * FROM context_packets WHERE session_id=? AND recipient_agent_id=? ORDER BY packet_version DESC LIMIT 1",
            (session_id, agent_id))
        return dec(r["body_json"]) if r else None

    def create(self, *, session_id: str, agent_id: str, topic: str, scope: str, summary: str, facts: list[dict],
               assumptions: list[str], open_questions: list[str], unreadable: list[str],
               user_assertions: list[dict] | None = None, hypotheses: list[dict] | None = None,
               blocked_ids: list[str] | None = None, analysis_run_id: str | None = None,
               target_tokens: int = 1500) -> dict:
        prev = self.latest_any(session_id, agent_id)
        version = (prev["packet_version"] + 1) if prev else 1
        ev_ids = sorted({i for f in facts for i in f["evidence_ids"]})
        ev = self.evidence(ev_ids)
        file_versions: dict[str, int] = {}
        for e in ev.values():
            file_versions[e["file_id"]] = e["file_version"]
        body = {
            "packet_id": self.packet_id(session_id, agent_id), "packet_version": version, "session_id": session_id,
            "topic": topic, "scope": scope, "recipient_agent_ids": [agent_id], "summary": summary,
            "facts": facts, "assumptions": assumptions, "open_questions": open_questions,
            "user_assertions": user_assertions or [], "hypotheses": hypotheses or [],
            "evidence": [{"id": i, "source_label": ev[i]["source_label"], "file_version": ev[i]["file_version"]} for i in ev_ids if i in ev],
            "omitted_or_unreadable": unreadable,
            "evidence_status": "blocked" if blocked_ids else ("supported" if facts or not ev_ids else "partial"),
            "blocked_evidence_request_ids": blocked_ids or [],
            "source_file_versions": file_versions,
        }
        # trim to target size (keep earliest facts; record omission)
        while estimate_tokens(render_packet(body)) > target_tokens and len(body["facts"]) > 1:
            dropped = body["facts"].pop()
            body["omitted_or_unreadable"].append(f"（容量制限のため省略）{dropped['claim'][:40]}…")
            keep = {i for f in body["facts"] for i in f["evidence_ids"]}
            body["evidence"] = [e for e in body["evidence"] if e["id"] in keep]
            body["source_file_versions"] = {ev[i]["file_id"]: ev[i]["file_version"] for i in keep if i in ev}
        with self.db.tx():
            self.db.conn.execute(
                "UPDATE context_packets SET status='superseded' WHERE session_id=? AND recipient_agent_id=? AND status='active'",
                (session_id, agent_id))
            self.db.conn.execute(
                """INSERT INTO context_packets(packet_id, packet_version, session_id, recipient_agent_id, body_json,
                   evidence_ids_json, source_file_versions_json, analysis_run_id, token_estimate, scope_files_json, created_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (body["packet_id"], version, session_id, agent_id, json.dumps(body, ensure_ascii=False),
                 json.dumps([e["id"] for e in body["evidence"]]), json.dumps(body["source_file_versions"]),
                 analysis_run_id, estimate_tokens(render_packet(body)),
                 json.dumps(sorted(body["source_file_versions"].keys())), now_iso(), "active"))
        self.bus.publish(session_id, "context_packet.created", {
            "packet_id": body["packet_id"], "packet_version": version, "recipient_agent_id": agent_id,
            "evidence_count": len(body["evidence"]), "token_estimate": estimate_tokens(render_packet(body)),
            "evidence_status": body["evidence_status"]})
        return body

    def validate_for_delivery(self, session_id: str, agent_id: str, packet: dict) -> None:
        allowed = {a["file_id"]: a for a in self.allowed_files(session_id, agent_id)}
        ids = [e["id"] for e in packet.get("evidence", [])]
        ev = self.evidence(ids)
        for i in ids:
            e = ev.get(i)
            if not e or e["status"] != "valid":
                raise PacketInvalid(f"根拠 {i} が存在しないか失効しています")
            if e["session_id"] != session_id:
                raise PacketInvalid(f"根拠 {i} は別セッションのものです")
            att = allowed.get(e["file_id"])
            if att is None:
                raise PacketInvalid(f"根拠 {i} の資料は {agent_id} への配布が許可されていません")
            if att["extract_version"] != e["file_version"]:
                raise PacketInvalid(f"根拠 {i} の資料版が古くなっています")
        for f in packet.get("facts", []):
            for i in f.get("evidence_ids", []):
                if i not in ids:
                    raise PacketInvalid(f"事実の根拠 {i} が出典一覧にありません")

    def deliver(self, session_id: str, agent_id: str, packet: dict, request_id: str | None = None) -> None:
        h = hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        acl = [a["file_id"] for a in self.allowed_files(session_id, agent_id)]
        last = self.db.one(
            "SELECT hash FROM context_packet_deliveries WHERE packet_id=? AND packet_version=? AND recipient_agent_id=? ORDER BY delivered_at DESC LIMIT 1",
            (packet["packet_id"], packet["packet_version"], agent_id))
        self.db.insert("context_packet_deliveries", {
            "id": new_id("dlv"), "packet_id": packet["packet_id"], "packet_version": packet["packet_version"],
            "recipient_agent_id": agent_id, "acl_json": acl, "request_id": request_id, "hash": h,
            "delivered_at": now_iso()})
        if not last:
            self.bus.publish(session_id, "context_packet.distributed", {
                "packet_id": packet["packet_id"], "packet_version": packet["packet_version"],
                "recipient_agent_id": agent_id, "evidence_count": len(packet.get("evidence", []))})

    def invalidate_for_files(self, session_id: str, file_ids: set[str], reason: str) -> list[str]:
        affected = []
        for r in self.db.all("SELECT * FROM context_packets WHERE session_id=? AND status='active'", (session_id,)):
            files = set(dec(r["scope_files_json"], []))
            if files & file_ids:
                self.db.execute("UPDATE context_packets SET status='invalidated' WHERE packet_id=? AND packet_version=?",
                                (r["packet_id"], r["packet_version"]))
                affected.append(r["recipient_agent_id"])
                self.bus.publish(session_id, "context_packet.invalidated", {
                    "packet_id": r["packet_id"], "packet_version": r["packet_version"],
                    "recipient_agent_id": r["recipient_agent_id"], "reason": reason})
        for fid in file_ids:
            if reason in ("detached", "disabled", "deleted"):
                self.db.execute("UPDATE chair_evidence SET status='invalidated' WHERE session_id=? AND file_id=?",
                                (session_id, fid))
            else:
                f = self.db.one("SELECT extract_version FROM uploaded_files WHERE file_id=?", (fid,))
                self.db.execute(
                    "UPDATE chair_evidence SET status='invalidated' WHERE session_id=? AND file_id=? AND file_version<>?",
                    (session_id, fid, f["extract_version"] if f else -1))
        return affected

    def list_for_session(self, session_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM context_packets WHERE session_id=? ORDER BY recipient_agent_id, packet_version",
                           (session_id,))
        out = []
        for r in rows:
            body = dec(r["body_json"], {})
            out.append({"packet_id": r["packet_id"], "packet_version": r["packet_version"],
                        "recipient_agent_id": r["recipient_agent_id"], "status": r["status"],
                        "token_estimate": r["token_estimate"], "created_at": r["created_at"],
                        "evidence_count": len(body.get("evidence", [])), "body": body,
                        "deliveries": self.db.all(
                            "SELECT request_id, delivered_at, hash FROM context_packet_deliveries WHERE packet_id=? AND packet_version=?",
                            (r["packet_id"], r["packet_version"]))})
        return out

    def scope_files(self, packet: Optional[dict]) -> list[str]:
        return sorted((packet or {}).get("source_file_versions", {}).keys())
