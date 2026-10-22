"""P2/P5: state machine, parallel barrier, confirmed-only history, checkpoint, SSE ledger
(acceptance 6–8, 11–12, 21–22)."""
import asyncio

import pytest

pytestmark = pytest.mark.anyio


def order(events, *types):
    return [ev for ev in events if ev["type"] in types]


async def test_full_flow_parallel_barrier_and_checkpoint(env_factory):
    async with env_factory() as e:
        e.script.delay_s = 0.002
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_DECISION"
        evs = e.events(sid)
        turns = [ev for ev in evs if ev["type"] in ("turn.started", "turn.completed")]
        # chair opening completes before independents start
        assert turns[0]["agent_id"] == "agent_chair" and turns[1]["type"] == "turn.completed"
        indep = [ev for ev in turns if ev.get("phase") == "INDEPENDENT" or ev.get("message_id") in
                 {m["id"] for m in s["messages"] if m["phase"] == "INDEPENDENT"}]
        starts = [i for i, ev in enumerate(indep) if ev["type"] == "turn.started"]
        completes = [i for i, ev in enumerate(indep) if ev["type"] == "turn.completed"]
        assert len(starts) == 3 and len(completes) == 3
        assert max(starts) < min(completes), "3 independent proposals must run in parallel"
        # chair review only after all three independents completed
        review = next(m for m in s["messages"] if m["phase"] == "CHAIR_REVIEW")
        indep_msgs = [m for m in s["messages"] if m["phase"] == "INDEPENDENT"]
        assert all(m["completed_at"] <= review["created_at"] for m in indep_msgs)
        # independent prompts must not contain other agents' proposals
        for c in e.calls("turn"):
            if c["meta"].get("phase") == "INDEPENDENT":
                body = c["payload"]["messages"][0]["content"]
                assert "（提案）" not in body and "（批判）" not in body
        # rounds: 2 debate rounds x 3 agents
        debate = [m for m in s["messages"] if m["phase"] == "DEBATE" and m["status"] == "completed"]
        assert len(debate) == 6
        # checkpoint present, decision required
        cp = (await e.client.get(f"/api/sessions/{sid}/checkpoint")).json()
        assert cp["awaiting"] and set(cp["report"]) >= {"progress_summary", "unresolved_issues", "stagnation_reasons",
                                                         "missing_evidence", "next_round_questions", "agent_dissent"}
        # waiting forever: no LLM calls while waiting
        n = len(e.script.calls)
        await asyncio.sleep(0.2)
        assert len(e.script.calls) == n
        # continue → one more round → new checkpoint
        r = await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "continue"})
        assert r.status_code == 200
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["state"] == "WAITING_USER_DECISION"
        assert len([m for m in s["messages"] if m["phase"] == "DEBATE" and m["status"] == "completed"]) == 9
        # redirect requires instruction and is shared as a user message
        r = await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "redirect"})
        assert r.status_code == 422
        r = await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "redirect", "instruction": "費用面に集中して"})
        assert r.status_code == 200
        await e.c.debate.wait_idle(sid)
        later = [c for c in e.calls("turn") if c["meta"].get("phase") == "DEBATE"][-1]
        assert "費用面に集中して" in later["payload"]["messages"][0]["content"]
        # finish → final report, completed
        r = await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "finish", "idempotency_key": "k1"})
        assert r.status_code == 200
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["state"] == "COMPLETED" and s["conclusion_status"] == "final"
        rep = s["final_report"]
        assert {"verified_findings", "user_assertions", "hypotheses", "unresolved_evidence"} <= set(rep)
        # decision endpoint not usable outside waiting state
        r = await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "continue"})
        assert r.status_code == 409


async def test_debate_history_contains_only_confirmed_messages(env_factory):
    async with env_factory() as e:
        # agent_b's first debate turn: partial output then stream cut; retried
        e.script.add({"error": "stream_cut", "partial": "途中まで書いた未確定の文章PARTIALMARK"}, agent_id="agent_b", phase="DEBATE")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_DECISION"
        inc = [m for m in s["messages"] if m["status"] == "incomplete"]
        assert inc and "PARTIALMARK" in inc[0]["content"]
        for c in e.calls():
            body = c["payload"]["messages"][0]["content"] if c["payload"]["messages"] else ""
            assert "PARTIALMARK" not in body, "incomplete output must never reach other agents"
        assert any(ev["type"] == "turn.failed" for ev in e.events(sid))


async def test_pause_resume_cancel(env_factory):
    async with env_factory() as e:
        e.script.delay_s = 0.01
        sid = await e.new_session()
        await e.client.post(f"/api/sessions/{sid}/start")
        await asyncio.sleep(0.05)
        r = await e.client.post(f"/api/sessions/{sid}/pause")
        assert r.status_code == 200
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["state"] == "PAUSED"
        assert not [m for m in s["messages"] if m["status"] == "streaming"]
        n_completed = len([m for m in s["messages"] if m["status"] == "completed"])
        r = await e.client.post(f"/api/sessions/{sid}/resume")
        assert r.status_code == 200
        await asyncio.sleep(0.05)
        r = await e.client.post(f"/api/sessions/{sid}/cancel")
        assert r.status_code == 200
        s = await e.session(sid)
        assert s["state"] == "CANCELLED"
        assert len([m for m in s["messages"] if m["status"] == "completed"]) >= n_completed
        # no message sent twice: each (agent, phase, round) completed at most once
        keys = [(m["agent_id"], m["phase"], m["round"]) for m in s["messages"] if m["status"] == "completed" and m["kind"] == "turn"]
        assert len(keys) == len(set(keys))


async def test_initial_parallel_failure_keeps_successes_then_skip(env_factory):
    async with env_factory() as e:
        e.script.add({"error": "auth", "message": "invalid key"}, agent_id="agent_c", phase="INDEPENDENT")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "PAUSED"
        assert s["blockers"][0]["type"] == "turn_failed" and s["blockers"][0]["agent_id"] == "agent_c"
        ok = [m for m in s["messages"] if m["phase"] == "INDEPENDENT" and m["status"] == "completed"]
        assert {m["agent_id"] for m in ok} == {"agent_a", "agent_b"}
        r = await e.client.post(f"/api/sessions/{sid}/skip-agent/agent_c")
        assert r.status_code == 200
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["state"] == "WAITING_USER_DECISION"
        assert len([m for m in s["messages"] if m["phase"] == "INDEPENDENT" and m["status"] == "completed"]) == 2


async def test_rate_limit_retry_then_success(env_factory):
    async with env_factory() as e:
        e.script.add({"error": "rate_limit", "retry_after": 0}, agent_id="agent_a", phase="INDEPENDENT")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_DECISION"
        a_ind = [m for m in s["messages"] if m["agent_id"] == "agent_a" and m["phase"] == "INDEPENDENT"]
        assert [m["status"] for m in a_ind] == ["failed", "completed"]
        # both attempts are metered
        reqs = e.c.db.all("SELECT attempt, usage_source FROM usage_records WHERE agent_id='agent_a' AND purpose='turn' ORDER BY created_at")
        assert len(reqs) >= 2


async def test_sse_ledger_replay_without_duplicates(env_factory):
    async with env_factory() as e:
        sid = await e.new_session()
        await e.start_and_idle(sid)
        evs = e.events(sid)
        ids = [ev["event_id"] for ev in evs]
        assert ids == sorted(ids) and len(ids) == len(set(ids))
        mid = ids[len(ids) // 2]
        replay = e.c.bus.replay(sid, mid)
        assert [ev["event_id"] for ev in replay] == [i for i in ids if i > mid]
        # deltas joined per message_id reproduce the stored content
        s = await e.session(sid)
        for m in s["messages"]:
            if m["kind"] in ("turn", "chair") and m["status"] == "completed":
                joined = "".join(ev["delta"] for ev in evs if ev["type"] == "turn.delta" and ev["message_id"] == m["id"])
                assert joined == m["content"]


async def test_restart_recovery_marks_streaming_incomplete(env_factory):
    async with env_factory() as e:
        sid = await e.new_session()
        e.c.db.update("debate_sessions", {"id": sid}, {"state": "DEBATE"})
        e.c.db.insert("messages", {"id": "m_x", "session_id": sid, "agent_id": "agent_a", "seq": 1, "round": 1,
                                   "phase": "DEBATE", "kind": "turn", "content": "half", "status": "streaming",
                                   "created_at": "2026-01-01T00:00:00Z"})
        e.c.debate.recover_after_restart()
        s = await e.session(sid)
        assert s["state"] == "PAUSED" and s["blockers"][0]["type"] == "restart"
        assert s["messages"][0]["status"] == "incomplete"
