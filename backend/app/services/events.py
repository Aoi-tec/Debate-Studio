"""Event ledger + live fan-out for SSE (DESIGN §9).

Every event is persisted with a monotonically increasing ``event_id`` before it
is pushed to subscribers, so a reconnecting client can replay exactly what it
missed via ``Last-Event-ID``.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from app.core.ids import now_iso
from app.core.redact import redact
from app.storage.db import Database


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def publish(self, session_id: str, type_: str, payload: dict[str, Any] | None = None) -> dict:
        ts = now_iso()
        body = dict(payload or {})
        raw = redact(json.dumps(body, ensure_ascii=False))  # secrets never leave the server
        body = json.loads(raw)
        with self.db._lock:
            cur = self.db.conn.execute(
                "INSERT INTO stream_events(session_id, type, payload_json, created_at) VALUES (?,?,?,?)",
                (session_id, type_, "{}", ts),
            )
            eid = cur.lastrowid
            event = {"event_id": eid, "session_id": session_id, "timestamp": ts, "type": type_, **body}
            self.db.conn.execute("UPDATE stream_events SET payload_json=? WHERE id=?",
                                 (json.dumps(event, ensure_ascii=False), eid))
        for q in list(self._subs.get(session_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # slow consumer: it will replay from DB on reconnect
                pass
        return event

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._subs[session_id].add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        self._subs.get(session_id, set()).discard(q)

    def replay(self, session_id: str, after_id: int = 0, limit: int = 50_000) -> list[dict]:
        rows = self.db.all(
            "SELECT payload_json FROM stream_events WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
            (session_id, after_id, limit),
        )
        return [json.loads(r["payload_json"]) for r in rows]
