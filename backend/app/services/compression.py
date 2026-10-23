"""CompressionService (DESIGN §6.3): summarise only *old shared conversation*.

Full logs are never modified. Evidence IDs, user instructions and unresolved
evidence requests are appended verbatim so a summary cannot erase them.
"""
from __future__ import annotations

import re

from app.core.ids import new_id, now_iso
from app.core.tokens import estimate_tokens
from app.providers.base import LLMRequest
from app.services.context import chair_system
from app.services.events import EventBus
from app.services.llm_runner import LLMRunner, SessionCtx
from app.storage.db import Database

_EV = re.compile(r"\bev_[0-9a-f]{6,}\b")


class CompressionService:
    def __init__(self, db: Database, bus: EventBus, runner: LLMRunner):
        self.db, self.bus, self.runner = db, bus, runner

    def latest(self, session_id: str) -> dict | None:
        return self.db.one("SELECT * FROM context_summaries WHERE session_id=? ORDER BY version DESC LIMIT 1", (session_id,))

    async def maybe_compress(self, ctx: SessionCtx, shareable: list[dict], force: bool = False) -> bool:
        """``shareable``: completed messages visible to every participant (ordered by seq)."""
        cs = ctx.settings.compression
        if cs.compression_mode == "off" and not force:
            return False
        if cs.compression_mode == "manual" and not force:
            return False
        prev = self.latest(ctx.id)
        after = prev["to_seq"] if prev else 0
        pending = [m for m in shareable if m["seq"] > after]
        total = sum(estimate_tokens(m["content"]) for m in pending) + (estimate_tokens(prev["summary"]) if prev else 0)
        if not force and total <= cs.history_compress_threshold:
            return False
        keep = cs.keep_recent_messages
        to_compress = pending[:-keep] if len(pending) > keep else []
        if not to_compress:
            return False
        self.bus.publish(ctx.id, "compression.started", {"messages": len(to_compress), "tokens_before": total})
        chair = ctx.chair
        text_block = "\n\n".join(f"[{m['speaker']} / seq {m['seq']}]\n{m['content']}" for m in to_compress)
        prompt = (("## 既存の要約\n" + prev["summary"] + "\n\n") if prev else "") + \
                 f"## 要約対象の討論（古い順）\n{text_block}\n\n" \
                 f"上記を{cs.summary_target_tokens}トークン程度で要約せよ。各Agentの主張・合意点・対立点・未解決点を残し、" \
                 "出典ID（ev_...）は省略せずそのまま残すこと。新しい事実を加えないこと。"
        system = chair_system(ctx.role_md(chair))

        def build(model: str, max_out: int) -> LLMRequest:
            return LLMRequest(model=model, system=system, messages=[{"role": "user", "content": prompt}],
                              temperature=0.2, max_output_tokens=max_out,
                              timeout_s=ctx.settings.debate.request_timeout_s)

        try:
            res = await self.runner.run(ctx, chair, purpose="compress", build=build,
                                        est_input=estimate_tokens(system) + estimate_tokens(prompt))
        except Exception as e:  # noqa: BLE001
            self.bus.publish(ctx.id, "compression.failed", {"error": str(e)[:300]})
            return False
        ev_ids = sorted({i for m in to_compress for i in _EV.findall(m["content"])})
        user_instr = [m["content"] for m in to_compress if m["kind"] == "user"]
        open_er = self.db.all(
            "SELECT id, criticality FROM evidence_requests WHERE session_id=? AND state NOT IN ('RESOLVED','WITHDRAWN')", (ctx.id,))
        preserved = []
        if ev_ids:
            preserved.append("保持された出典ID: " + ", ".join(ev_ids))
        if user_instr:
            preserved.append("ユーザーの指示（原文）:\n" + "\n".join(f"- {u}" for u in user_instr))
        if open_er:
            preserved.append("未解決の根拠要求: " + ", ".join(f"{r['id']}({r['criticality']})" for r in open_er))
        summary = res.text.strip() + ("\n\n" + "\n".join(preserved) if preserved else "")
        version = (prev["version"] + 1) if prev else 1
        self.db.insert("context_summaries", {
            "id": new_id("sum"), "session_id": ctx.id, "version": version,
            "from_seq": to_compress[0]["seq"] if not prev else prev["from_seq"], "to_seq": to_compress[-1]["seq"],
            "summary": summary, "usage_json": {"input": res.usage.input_tokens, "output": res.usage.output_tokens,
                                               "source": res.usage.source}, "created_at": now_iso()})
        self.bus.publish(ctx.id, "compression.completed", {"version": version, "to_seq": to_compress[-1]["seq"],
                                                            "tokens_after": estimate_tokens(summary)})
        return True
