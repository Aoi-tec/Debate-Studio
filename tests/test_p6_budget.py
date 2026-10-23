"""P6: thresholds, reservations, tier fallback, quality gate, hard limit, compression
(acceptance 9–10, 23–29, 36–37)."""
import json

import pytest

from app.models.schemas import AgentConfig, ApiProfile, DebateSettings
from app.services.budget import BudgetGuard, threshold
from app.services.usage import UsageMeter

pytestmark = pytest.mark.anyio


def test_threshold_formula():
    s = DebateSettings()
    assert threshold("openai_claude", 12_000, s) == 14_000
    assert threshold("other_cloud", 12_000, s) == 15_000


async def _profiles(e, extra: list[dict]):
    profiles = e.c.config.profiles()
    for x in extra:
        profiles.append(ApiProfile.model_validate(x))
    e.c.config.save_profiles(profiles)


def _fake_profile(pid, name, family="other_cloud", quota=None, quality=True, model="fake-model", docs=True):
    return {"id": pid, "name": name, "protocol": "fake", "default_model": model, "billing_family": family,
            "allow_document_transfer": docs, "quota": quota or {"mode": "disabled"},
            "quality": {"status": "passed", "validated_model": model} if quality else {"status": "unvalidated"}}


async def _route_all(e, routing: dict, budgets: dict, chair_too=False):
    agents = e.c.config.agents()
    out = []
    for a in agents:
        if a.kind == "participant" or chair_too:
            a = a.model_copy(update={"routing_profiles": routing, "tier_token_budgets": budgets})
        out.append(a)
    e.c.config.save_agents(out)


async def test_u0_threshold_fixed_at_start_and_refusals(env_factory):
    async with env_factory() as e:
        e.script.usage_snapshot_tokens = 12_000
        await _profiles(e, [_fake_profile("api_oa", "OA-like", "openai_claude", {"mode": "auto", "scope": "shared"}),
                            _fake_profile("api_oc", "Other cloud", "other_cloud", {"mode": "auto", "scope": "shared"})])
        await _route_all(e, {"primary_cloud": "api_oa", "downstream_cloud": "api_oc"}, {})
        sid = await e.new_session()
        r = await e.client.post(f"/api/sessions/{sid}/start")
        assert r.status_code == 200, r.text
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        lim = {b["pool_id"]: b["limit_tokens"] for b in s["baselines"]}
        assert lim["api_oa"] == 14_000 and lim["api_oc"] == 15_000
        # threshold does not move while usage grows
        b2 = e.c.db.all("SELECT limit_tokens FROM usage_baselines WHERE session_id=? AND pool_id='api_oa'", (sid,))
        assert [x["limit_tokens"] for x in b2] == [14_000]
        # U0 == 0 without bootstrap → refused
        e.script.usage_snapshot_tokens = 0
        sid2 = await e.new_session()
        r = await e.client.post(f"/api/sessions/{sid2}/start")
        assert r.status_code == 409 and "U0=0" in r.text
        # usage API unavailable without manual budget → refused
        e.script.usage_snapshot_tokens = None
        sid3 = await e.new_session()
        r = await e.client.post(f"/api/sessions/{sid3}/start")
        assert r.status_code == 409 and "手動予算" in r.text
        # with manual budget → allowed, marked as manual/estimated
        profiles = e.c.config.profiles()
        profiles = [p.model_copy(update={"quota": p.quota.model_copy(update={"manual_budget_tokens": 50_000})}) if p.id in ("api_oa", "api_oc") else p for p in profiles]
        e.c.config.save_profiles(profiles)
        sid4 = await e.new_session()
        r = await e.client.post(f"/api/sessions/{sid4}/start")
        assert r.status_code == 200
        await e.c.debate.wait_idle(sid4)
        bs = (await e.session(sid4))["baselines"]
        assert {b["source"] for b in bs if b["pool_id"] in ("api_oa", "api_oc")} == {"manual"}


async def test_tier_fallback_primary_downstream_local(env_factory):
    async with env_factory() as e:
        await _profiles(e, [_fake_profile("api_p", "Primary"), _fake_profile("api_d", "Downstream"),
                            _fake_profile("api_l", "Local", "local")])
        await _route_all(e, {"primary_cloud": "api_p", "downstream_cloud": "api_d", "local_llm": "api_l"},
                         {"primary_cloud": 1500, "downstream_cloud": 1500, "local_llm": None})
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_DECISION", s["blockers"]
        tiers = {m["tier"] for m in s["messages"] if m["agent_id"] == "agent_a" and m["status"] == "completed"}
        assert {"primary_cloud", "downstream_cloud", "local_llm"} <= tiers
        assert s["handoffs"], "handoffs recorded"
        types = [ev["type"] for ev in e.events(sid)]
        assert "model.fallback_started" in types and "model.handoff_completed" in types
        # each downstream call got the same Agent.MD system prompt as primary
        a_calls = e.calls("turn", "agent_a")
        assert len({c["payload"]["system"] for c in a_calls}) == 1
        # agent tier usage never above limit
        for b in s["budgets"]:
            if b["limit_tokens"]:
                assert b["used_tokens"] <= b["limit_tokens"]


async def test_downstream_requires_quality_validation(env_factory):
    async with env_factory() as e:
        await _profiles(e, [_fake_profile("api_p", "Primary"), _fake_profile("api_d", "Unvalidated", quality=False)])
        await _route_all(e, {"primary_cloud": "api_p", "downstream_cloud": "api_d"}, {"primary_cloud": 1200})
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "PAUSED_BUDGET"
        assert any("品質検証未合格" in r for r in s["blockers"][0]["reasons"])
        # validate-quality endpoint marks it passed; raising the budget allows resume
        r = await e.client.post("/api/profiles/api_d/validate-quality")
        assert r.json()["status"] == "passed"


async def test_quality_gate_rejects_fabricated_evidence_on_downstream(env_factory):
    async with env_factory() as e:
        await _profiles(e, [_fake_profile("api_p", "Primary"), _fake_profile("api_d", "Down")])
        await _route_all(e, {"primary_cloud": "api_p", "downstream_cloud": "api_d"}, {"primary_cloud": 0})
        e.script.add("根拠は [ev_deadbeef01] です", times=2, agent_id="agent_a", tier="downstream_cloud")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "PAUSED" and s["blockers"][0]["type"] == "quality"
        assert [m["status"] for m in s["messages"] if m["agent_id"] == "agent_a"].count("quality_failed") == 2
        assert any(ev["type"] == "model.quality_failed" for ev in e.events(sid))


async def test_session_hard_limit_is_not_bypassed_by_fallback(env_factory):
    async with env_factory() as e:
        await _profiles(e, [_fake_profile("api_l", "Local", "local")])
        await _route_all(e, {"local_llm": "api_l"}, {}, chair_too=True)
        st = e.c.config.settings()
        st.limits.max_session_tokens = 3000
        e.c.config.save_settings(st)
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "PAUSED_BUDGET" and s["blockers"][0]["hard"] is True
        assert s["usage"]["total_tokens"] <= 3000, "each request's output is capped to the remaining session room"
        r = await e.client.post(f"/api/sessions/{sid}/resume")
        await e.c.debate.wait_idle(sid)
        s = await e.session(sid)
        assert s["state"] == "PAUSED_BUDGET" and s["usage"]["total_tokens"] <= 3000
        counts = []
        for _ in range(4):  # repeated resumes: total stays under the hard limit and calls stop growing
            await e.client.post(f"/api/sessions/{sid}/resume")
            await e.c.debate.wait_idle(sid)
            s = await e.session(sid)
            assert s["state"] == "PAUSED_BUDGET" and s["usage"]["total_tokens"] <= 3000
            counts.append(len(e.script.calls))
        assert counts[-1] == counts[-2], "no new generation once the hard limit has no room left"


async def test_shared_pool_reservations_are_atomic(tmp_path):
    from app.storage.db import Database
    db = Database(tmp_path / "x.sqlite3")
    meter = UsageMeter(db)
    g = BudgetGuard(db, meter)
    p = ApiProfile(id="api_x", name="x", protocol="fake", default_model="m",
                   quota={"mode": "manual", "quota_pool_id": "pool1", "scope": "shared"})
    db.insert("usage_baselines", {"id": "b1", "session_id": "s1", "pool_id": "pool1", "profile_id": "api_x", "family": "other_cloud",
                                  "period": "", "u0": 0, "limit_tokens": 1000, "source": "manual", "fetched_at": "2000-01-01T00:00:00Z",
                                  "estimated": 1, "detail": ""})
    a1 = AgentConfig(id="a1", name="A1", kind="participant", role_id="r", profile_id="api_x")
    a2 = AgentConfig(id="a2", name="A2", kind="participant", role_id="r", profile_id="api_x")
    r1, _ = g.try_reserve(session_id="s1", agent=a1, tier="primary_cloud", profile=p, model="m", est_input=400, desired_output=400)
    r2, why = g.try_reserve(session_id="s1", agent=a2, tier="primary_cloud", profile=p, model="m", est_input=400, desired_output=400)
    assert r1 is not None
    assert r2 is None or r2.tokens + r1.tokens < 1000, "parallel agents must not double-spend the shared pool"
    g.settle(r1, 100)
    r3, _ = g.try_reserve(session_id="s1", agent=a2, tier="primary_cloud", profile=p, model="m", est_input=400, desired_output=400)
    assert r3 is not None


async def test_usage_sources_and_breakdown(env_factory):
    async with env_factory() as e:
        f = await e.upload_ready("p.txt", "単価は100円。".encode())
        sid = await e.new_session(attachment_ids=[f["file_id"]])
        s = await e.start_and_idle(sid)
        u = s["usage"]
        assert u["total_tokens"] == u["input_tokens"] + u["output_tokens"] > 0
        chair = u["by_agent"]["agent_chair"]["by_purpose"]
        assert "chair_analysis" in chair and "checkpoint" in chair
        assert "chair_analysis" not in u["by_agent"]["agent_a"]["by_purpose"]
        assert u["estimated_cost"] is None or u["cost_complete"] is False  # no pricing configured → not computed
        # pricing configured → cost computed
        st = e.c.config.settings()
        st = DebateSettings.model_validate({**st.model_dump(), "pricing": {"fake-model": {"input_price_per_1m": 1.0, "output_price_per_1m": 2.0}}})
        e.c.config.save_settings(st)
        sid2 = await e.new_session()
        s2 = await e.start_and_idle(sid2)
        assert s2["usage"]["estimated_cost"] > 0


async def test_history_compression_keeps_full_log_and_ids(env_factory):
    async with env_factory() as e:
        st = e.c.config.settings()
        st.compression.history_compress_threshold = 500
        st.compression.keep_recent_messages = 3
        e.c.config.save_settings(st)
        e.script.add("提案: 根拠 ev_aaaaaa11 を参照。" + "詳細な説明。" * 120, agent_id="agent_a", phase="INDEPENDENT")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        summ = e.c.db.all("SELECT * FROM context_summaries WHERE session_id=?", (sid,))
        assert summ, "compression should run at round end"
        assert "ev_aaaaaa11" in summ[0]["summary"]
        # full log intact
        assert any("詳細な説明。" * 120 in m["content"] for m in s["messages"])
        types = [ev["type"] for ev in e.events(sid)]
        assert "compression.started" in types and "compression.completed" in types
        # later participant prompts use the summary
        later = [c for c in e.calls("turn") if c["meta"].get("phase") == "DEBATE"][-1]
        assert "これまでの討論の要約" in later["payload"]["messages"][0]["content"]
        # compression usage charged
        assert e.c.db.one("SELECT COUNT(*) n FROM usage_records WHERE purpose='compress'")["n"] >= 1


async def test_document_transfer_permission_is_enforced(env_factory):
    async with env_factory() as e:
        await _profiles(e, [_fake_profile("api_nodoc", "No docs", docs=False)])
        agents = [a.model_copy(update={"profile_id": "api_nodoc"}) if a.kind == "chair" else a for a in e.c.config.agents()]
        e.c.config.save_agents(agents)
        f = await e.upload_ready("p.txt", "単価は100円。".encode())
        sid = await e.new_session(attachment_ids=[f["file_id"]])
        s = await e.start_and_idle(sid)
        assert s["state"] == "PAUSED_BUDGET"
        assert any("資料由来データ送信が許可されていません" in r for r in s["blockers"][0]["reasons"])
        assert not e.calls("chair_analysis"), "document text never sent to a non-permitted destination"


async def test_packet_bodies_match_contract(env_factory):
    import pathlib
    schema = json.loads((pathlib.Path(__file__).resolve().parents[1] / "contracts" / "context_packet.schema.json").read_text(encoding="utf-8"))
    async with env_factory() as e:
        f = await e.upload_ready("p.txt", "単価は100円。適用開始日は2026年4月1日。".encode())
        sid = await e.new_session(attachment_ids=[f["file_id"]])
        await e.start_and_idle(sid)
        for p in (await e.client.get(f"/api/sessions/{sid}/context-packets")).json()["packets"]:
            body = p["body"]
            assert set(schema["required"]) <= set(body) <= set(schema["properties"])


async def test_per_agent_pools_are_independent(env_factory):
    """Default scope=per_agent: each agent has its own allowance on a shared connection (e.g. Strata)."""
    async with env_factory() as e:
        await _profiles(e, [_fake_profile("api_s", "Shared server", "other_cloud",
                                          {"mode": "manual", "manual_budget_tokens": 6000})])
        await _route_all(e, {"primary_cloud": "api_s"}, {}, chair_too=True)
        # agent_a talks a lot and exhausts *its own* allowance only
        e.script.add("長い発言。" * 900, times=10, agent_id="agent_a")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        rows = {b["pool_id"]: b for b in s["baselines"]}
        assert {"api_s@agent_a", "api_s@agent_b", "api_s@agent_c", "api_s@agent_chair"} <= set(rows)
        assert all(r["limit_tokens"] == 6000 for r in rows.values())
        used = {r["pool_id"]: r["t"] for r in e.c.db.all(
            "SELECT pool_id, SUM(input_tokens+output_tokens) t FROM usage_records WHERE session_id=? GROUP BY pool_id", (sid,))}
        assert set(used) <= set(rows), "usage is recorded against the agent's own pool"
        assert used["api_s@agent_b"] < 6000 and used["api_s@agent_a"] <= 6000
        # b and c were never blocked by a's consumption
        ok = {m["agent_id"] for m in s["messages"] if m["phase"] == "INDEPENDENT" and m["status"] == "completed"}
        assert {"agent_b", "agent_c"} <= ok


async def test_per_connection_concurrency_limit(env_factory):
    """max_concurrent_requests=1 → a local server gets one agent at a time (full context each)."""
    async with env_factory() as e:
        e.script.delay_s = 0.002
        p = _fake_profile("api_local", "Local single slot", "local")
        p["max_concurrent_requests"] = 1
        await _profiles(e, [p])
        await _route_all(e, {"primary_cloud": "api_local"}, {})
        sid = await e.new_session()
        await e.start_and_idle(sid)
        evs = [ev for ev in e.events(sid) if ev["type"] in ("turn.started", "turn.completed") and ev.get("phase") in (None, "INDEPENDENT")]
        live, peak = 0, 0
        ind = {ev["message_id"] for ev in e.events(sid) if ev["type"] == "turn.started" and ev.get("phase") == "INDEPENDENT"}
        for ev in e.events(sid):
            if ev.get("message_id") in ind:
                live += 1 if ev["type"] == "turn.started" else (-1 if ev["type"] == "turn.completed" else 0)
                peak = max(peak, live)
        assert peak == 1, "independent proposals are serialized on a single-slot connection"


async def test_thinking_budget_added_and_truncation_retried(env_factory):
    async with env_factory() as e:
        p = _fake_profile("api_think", "Thinking local", "local")
        p["reasoning"] = {"effort": "medium", "budget_tokens": 1000}
        await _profiles(e, [p])
        await _route_all(e, {"primary_cloud": "api_think"}, {}, chair_too=True)
        # first attempt: thinking alone overflows the cap → cut off → retried with 2x cap → answer arrives
        e.script.add({"thinking": "考" * 2400, "text": "結論です。"}, times=2, agent_id="agent_a", phase="INDEPENDENT")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_DECISION", s["blockers"]
        a_calls = [c for c in e.calls("turn", "agent_a") if c["meta"].get("phase") == "INDEPENDENT"]
        assert a_calls[0]["payload"]["max_output_tokens"] == 1500 + 1000, "answer budget + thinking budget"
        assert a_calls[0]["reasoning_effort"] == "medium" and a_calls[0]["thinking_budget"] == 1000
        assert a_calls[1]["payload"]["max_output_tokens"] == 2 * (1500 + 1000)
        msgs = [m for m in s["messages"] if m["agent_id"] == "agent_a" and m["phase"] == "INDEPENDENT"]
        assert [m["status"] for m in msgs] == ["incomplete", "completed"]
        assert msgs[-1]["content"] == "結論です。" and msgs[-1]["meta"]["thinking_tokens"] >= 2400
        evs = e.events(sid)
        th = [ev for ev in evs if ev["type"] == "turn.thinking"]
        assert th and all("delta" not in ev and "text" not in ev for ev in th), "only token counts, never thinking text"
        assert not any("考考考" in json_dumps(c["payload"]) for c in e.calls() if c["meta"].get("agent_id") != "agent_a")


async def test_inline_think_tags_are_stripped(env_factory):
    async with env_factory() as e:
        e.script.add("<think>内部の推論メモSECRET</think>公開する意見です。", agent_id="agent_b", phase="INDEPENDENT")
        e.script.add("前置き推論ORPHAN</think>本文のみ。", agent_id="agent_c", phase="INDEPENDENT")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        b = next(m for m in s["messages"] if m["agent_id"] == "agent_b" and m["phase"] == "INDEPENDENT")
        c = next(m for m in s["messages"] if m["agent_id"] == "agent_c" and m["phase"] == "INDEPENDENT")
        assert b["content"] == "公開する意見です。" and c["content"] == "本文のみ。"
        for call in e.calls():
            blob = json_dumps(call["payload"])
            assert "SECRET" not in blob and "ORPHAN" not in blob, "private reasoning never reaches other agents"
        assert not any("SECRET" in ev.get("delta", "") for ev in e.events(sid))


def test_settings_migration_raises_old_800_default(tmp_path):
    import json as _j
    from app.storage.config_store import ConfigStore
    cfg = tmp_path / "config"
    cfg.mkdir()
    old = DebateSettings().model_dump()
    old.pop("settings_version")
    old["debate"]["max_output_tokens"] = 800
    (cfg / "debate_settings.json").write_text(_j.dumps(old), encoding="utf-8")
    st = ConfigStore(cfg, tmp_path / "roles").settings()
    assert st.debate.max_output_tokens == 1500 and st.settings_version == 2


def json_dumps(x):
    import json as _j
    return _j.dumps(x, ensure_ascii=False)


async def test_context_overflow_compresses_and_retries(env_factory):
    async with env_factory() as e:
        st = e.c.config.settings()
        st.compression.keep_recent_messages = 2
        e.c.config.save_settings(st)
        e.script.add({"error": "context_overflow", "message": "maximum context length exceeded"}, agent_id="agent_a", phase="DEBATE")
        sid = await e.new_session()
        s = await e.start_and_idle(sid)
        assert s["state"] == "WAITING_USER_DECISION", s["blockers"]
        assert e.c.db.all("SELECT * FROM context_summaries WHERE session_id=?", (sid,)), "history compressed before retry"
        a = [m for m in s["messages"] if m["agent_id"] == "agent_a" and m["phase"] == "DEBATE"]
        assert a[0]["status"] == "failed" and a[1]["status"] == "completed"
