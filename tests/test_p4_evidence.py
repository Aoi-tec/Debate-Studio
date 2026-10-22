"""P4: evidence shortage → user hand-back → revalidation → resume (acceptance 34, 38–46)."""
import asyncio
import json

import pytest

from samples import xlsx_bytes

pytestmark = pytest.mark.anyio

CR = {"topic": "部品単価", "question": "部品単価表の適用開始日はいつか", "required_evidence": ["適用開始日"],
      "why_needed": "旧単価だと見積金額が変わる", "criticality": "critical"}


def ask(e, cr=CR, agent="agent_b", phase="INDEPENDENT"):
    e.script.add({"text": "単価の妥当性は判断できない。", "context_request": cr}, agent_id=agent, phase=phase)


async def respond(e, sid, er, action, **kw):
    body = {"evidence_request_id": er["evidence_request_id"], "expected_version": er["version"], "action": action,
            "idempotency_key": kw.pop("key", f"k-{action}-{er['version']}"), **kw}
    return await e.client.post(f"/api/sessions/{sid}/evidence-requests/{er['evidence_request_id']}/respond", json=body)


async def open_er(e, sid):
    ers = (await e.client.get(f"/api/sessions/{sid}/evidence-requests")).json()["evidence_requests"]
    return [x for x in ers if x["state"] in ("WAITING_USER_EVIDENCE", "STILL_MISSING")]


async def test_resolved_by_existing_source_without_user(env_factory):
    async with env_factory() as e:
        f = await e.upload_ready("price.txt", "部品単価表\n単価の適用開始日は2026年4月1日。\n".encode())
        ask(e)
        sid = await e.new_session(attachment_ids=[f["file_id"]])
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_DECISION"
        assert not s["evidence_requests"], "found in permitted docs → no user hand-back"
        pk = [p for p in (await e.client.get(f"/api/sessions/{sid}/context-packets")).json()["packets"] if p["recipient_agent_id"] == "agent_b"]
        assert len(pk) >= 2 and pk[-1]["packet_version"] > pk[0]["packet_version"]
        # dependent turn re-ran (REDO) with the new packet
        redo = [m for m in s["messages"] if m["agent_id"] == "agent_b" and m["meta"].get("redo")]
        assert redo and redo[0]["packet_refs"][0]["packet_version"] == pk[-1]["packet_version"]
        # only the chair searched
        assert all(c["meta"]["agent_id"] == "agent_chair" for c in e.calls("evidence_check"))


async def test_not_found_waits_for_user_then_file_upload_resumes(env_factory):
    async with env_factory() as e:
        ask(e)
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_EVIDENCE"
        ers = await open_er(e, sid)
        assert len(ers) == 1
        er = ers[0]
        # card content: reason, missing, searched, impact, acceptable inputs
        assert er["reason_code"] == "NOT_FOUND" and er["criticality"] == "critical"
        assert er["missing_fields"] == ["適用開始日"] and er["impact_if_unresolved"]
        assert "continue_as_hypothesis" not in er["acceptable_inputs"]
        assert er["searched_sources"] == [], "nothing was searched → nothing claimed as searched"
        # no model calls while waiting
        n = len(e.script.calls)
        await asyncio.sleep(0.2)
        assert len(e.script.calls) == n
        # 'continue' at checkpoint is impossible meanwhile (no checkpoint reached)
        assert (await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "continue"})).status_code == 409
        # wrong version → 409, critical hypothesis → 422
        bad = dict(er, version=er["version"] + 5)
        assert (await respond(e, sid, bad, "withdraw")).status_code == 409
        assert (await respond(e, sid, er, "continue_as_hypothesis")).status_code == 422
        # upload a file that does NOT contain the answer → still missing → re-asked
        nf = await e.upload_ready("other.txt", "関係のない議事録".encode())
        r = await respond(e, sid, er, "file_upload", file_ids=[nf["file_id"]], key="up1")
        assert r.status_code == 200 and r.json()["state"] == "REVALIDATING"
        await e.c.debate.wait_idle(sid)
        er2 = (await open_er(e, sid))[0]
        assert er2["state"] == "WAITING_USER_EVIDENCE" and er2["ask_count"] == 2 and "再確認" in er2["question"]
        assert er2["searched_sources"] and er2["searched_sources"][0]["file_id"] == nf["file_id"]
        assert (await e.session(sid))["state"] == "WAITING_USER_EVIDENCE"
        # now upload the right file → resolved → packet bumped → dependent turn resumes → checkpoint
        good = await e.upload_ready("単価表.xlsx", xlsx_bytes([["項目", "値"], ["適用開始日", "2026-04-01"], ["通貨", "円"]], formula_cell=False))
        r = await respond(e, sid, er2, "file_upload", file_ids=[good["file_id"]], key="up2")
        assert r.status_code == 200
        # duplicate click with the same key: no second job / no second resume
        r2 = await respond(e, sid, er2, "file_upload", file_ids=[good["file_id"]], key="up2")
        assert r2.json().get("idempotent_replay") is True
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["state"] == "WAITING_USER_DECISION", s["blockers"]
        final_er = s["evidence_requests"][0]
        assert final_er["state"] == "RESOLVED"
        redo = [m for m in s["messages"] if m["agent_id"] == "agent_b" and m["meta"].get("redo")]
        assert len(redo) == 1, "dependent turn re-run exactly once"
        pk = (await e.client.get(f"/api/sessions/{sid}/context-packets")).json()["packets"]
        b = [p for p in pk if p["recipient_agent_id"] == "agent_b" and p["status"] == "active"][0]["body"]
        assert any("2026-04-01" in f["claim"] for f in b["facts"])
        assert not b["blocked_evidence_request_ids"]
        types = [ev["type"] for ev in e.events(sid)]
        for t in ("evidence.requested", "evidence.searching", "evidence.awaiting_user", "evidence.user_responded",
                  "evidence.revalidating", "evidence.still_missing", "evidence.resolved"):
            assert t in types, t


async def test_user_statement_is_not_document_evidence(env_factory):
    async with env_factory() as e:
        ask(e)
        sid = await e.new_session()
        await e.start_and_idle(sid)
        er = (await open_er(e, sid))[0]
        r = await respond(e, sid, er, "user_statement", statement="適用開始日は2026年4月1日です（担当者確認済み）")
        assert r.json()["state"] == "RESOLVED"
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["state"] == "WAITING_USER_DECISION"
        prov = e.c.db.all("SELECT * FROM provenance_items WHERE session_id=?", (sid,))
        assert prov[0]["source_kind"] == "user_statement" and prov[0]["verification_status"] == "user_provided_unverified"
        b = (await e.client.get(f"/api/sessions/{sid}/context-packets")).json()["packets"]
        b = [p for p in b if p["recipient_agent_id"] == "agent_b" and p["status"] == "active"][0]["body"]
        assert b["user_assertions"] and not b["facts"], "user statement must not become a document fact"
        redo = [c for c in e.calls("turn", "agent_b") if c["meta"].get("phase") == "REDO"][0]
        assert "ユーザー申告（資料による検証なし）" in redo["payload"]["messages"][0]["content"]
        await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "finish"})
        await e.c.debate.wait_idle(sid)
        rep = (await e.session(sid))["final_report"]
        assert rep["user_assertions"] and not rep["verified_findings"]
        assert rep["relies_on_user_assertions"] is True


async def test_critical_unavailable_blocks_conclusion(env_factory):
    async with env_factory() as e:
        ask(e)
        sid = await e.new_session()
        await e.start_and_idle(sid)
        er = (await open_er(e, sid))[0]
        r = await respond(e, sid, er, "unavailable")
        assert r.json()["state"] == "DEFERRED"
        await e.c.debate.wait_idle(sid)
        assert (await e.session(sid))["state"] == "WAITING_USER_DECISION"
        await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "finish"})
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["conclusion_status"] == "blocked"
        assert s["final_report"]["unresolved_evidence"][0]["criticality"] == "critical"
        assert s["final_report"]["adopted_proposal"].startswith("【確定不可")


async def test_noncritical_hypothesis_and_finish_with_unresolved(env_factory):
    async with env_factory() as e:
        ask(e, dict(CR, criticality="noncritical"))
        sid = await e.new_session()
        await e.start_and_idle(sid)
        er = (await open_er(e, sid))[0]
        assert "continue_as_hypothesis" in er["acceptable_inputs"]
        r = await respond(e, sid, er, "continue_as_hypothesis", statement="適用開始日は年度初めと仮定")
        assert r.json()["state"] == "DEFERRED"
        await e.c.debate.wait_idle(sid)
        b = [p for p in (await e.client.get(f"/api/sessions/{sid}/context-packets")).json()["packets"]
             if p["recipient_agent_id"] == "agent_b" and p["status"] == "active"][0]["body"]
        assert b["hypotheses"] and not b["facts"]
        await e.client.post(f"/api/sessions/{sid}/decision", json={"decision": "finish"})
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["conclusion_status"] == "provisional" and s["final_report"]["hypotheses"]


async def test_finish_with_unresolved_goes_through_checkpoint(env_factory):
    async with env_factory() as e:
        ask(e)
        sid = await e.new_session()
        await e.start_and_idle(sid)
        er = (await open_er(e, sid))[0]
        r = await respond(e, sid, er, "finish_with_unresolved")
        assert r.status_code == 200
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["state"] == "COMPLETED" and s["conclusion_status"] == "blocked"
        kinds = [m["kind"] for m in s["messages"]]
        assert kinds.index("checkpoint") < kinds.index("final"), "never FINALIZING without CHECKPOINT_REPORT"


async def test_conflicting_sources_are_not_auto_resolved(env_factory):
    async with env_factory() as e:
        f1 = await e.upload_ready("v1.txt", "単価表 第1版\n適用開始日は2026年4月1日。\n".encode())
        f2 = await e.upload_ready("v2.txt", "単価表 第2版\n適用開始日は2026年10月1日。\n".encode())
        ask(e)
        e.script.add(lambda req: json.dumps({"answered": False, "facts": [], "reason_code": "CONFLICTING",
                                             "conflicts": [{"evidence_ids": [x["evidence_id"] for x in req.metadata["fake_hint"]["candidates"][:2]],
                                                            "note": "日付が異なる"}],
                                             "missing_fields": ["正しい版"]}, ensure_ascii=False), purpose="evidence_check")
        sid = await e.new_session(attachment_ids=[f1["file_id"], f2["file_id"]])
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_EVIDENCE"
        er = (await open_er(e, sid))[0]
        assert er["reason_code"] == "CONFLICTING" and "矛盾" in er["question"]
        assert "v1.txt" in er["question"] and "v2.txt" in er["question"]


async def test_access_denied_does_not_leak(env_factory):
    async with env_factory() as e:
        f = await e.upload_ready("restricted.txt", "部品単価表\n単価の適用開始日は2026年4月1日（機密）。\n".encode())
        ask(e, agent="agent_b")
        sid = await e.new_session()
        await e.client.post(f"/api/sessions/{sid}/attachments", json={"file_id": f["file_id"], "allowed_agent_ids": ["agent_a"]})
        s = await e.start_and_idle(sid)
        er = (await open_er(e, sid))[0]
        assert er["reason_code"] == "ACCESS_DENIED"
        for c in e.calls():
            if c["meta"].get("agent_id") == "agent_b":
                assert "機密" not in json.dumps(c["payload"], ensure_ascii=False)
        # evidence check prompt to the chair never included the restricted excerpt for agent_b
        assert not e.calls("evidence_check")


async def test_duplicate_requests_and_loop_guard(env_factory):
    async with env_factory() as e:
        ask(e)
        sid = await e.new_session()
        await e.start_and_idle(sid)
        er = (await open_er(e, sid))[0]
        ctx = e.c.debate.ctx(sid)
        res = await e.c.evidence.handle_context_request(ctx, "agent_b", "m_dup", CR)
        assert res["status"] == "waiting" and res["evidence_request_id"] == er["evidence_request_id"]
        assert len(await open_er(e, sid)) == 1


async def test_waiting_state_survives_restart(env_factory):
    async with env_factory() as e:
        ask(e)
        sid = await e.new_session()
        await e.start_and_idle(sid)
        e.c.debate._ctx.clear()
        e.c.debate.recover_after_restart()
        s = await e.session(sid)
        assert s["state"] == "WAITING_USER_EVIDENCE" and s["evidence_requests"][0]["state"] == "WAITING_USER_EVIDENCE"


async def test_contract_schema_matches(env_factory):
    """Stored EvidenceRequest bodies validate against contracts/evidence_request.schema.json."""
    import pathlib
    schema = json.loads((pathlib.Path(__file__).resolve().parents[1] / "contracts" / "evidence_request.schema.json").read_text(encoding="utf-8"))
    req = set(schema["required"])
    props = set(schema["properties"])
    async with env_factory() as e:
        ask(e)
        sid = await e.new_session()
        await e.start_and_idle(sid)
        row = e.c.db.one("SELECT body_json FROM evidence_requests WHERE session_id=?", (sid,))
        body = json.loads(row["body_json"])
        body.pop("_extra")
        assert req <= set(body) and set(body) <= props
        assert body["state"] in schema["properties"]["state"]["enum"]
        assert body["reason_code"] in schema["properties"]["reason_code"]["enum"]
