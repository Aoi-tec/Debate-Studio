"""P1: profiles, secrets, roles, agents (acceptance 1–5, 30)."""
import json
import logging

import pytest

pytestmark = pytest.mark.anyio


async def test_default_four_agents_and_minimum(env_factory):
    async with env_factory() as e:
        r = await e.client.get("/api/agents")
        agents = r.json()["agents"]
        assert len(agents) == 4
        assert [a["kind"] for a in agents].count("chair") == 1
        assert agents[0]["capabilities"] == ["document_analysis", "retrieval", "context_distribution"]
        assert all(a["capabilities"] == ["text_debate"] for a in agents if a["kind"] == "participant")
        # cannot reduce participants below 3
        r = await e.client.put("/api/agents", json={"agents": agents[:3]})
        assert r.status_code == 422
        # cannot remove chair
        r = await e.client.put("/api/agents", json={"agents": agents[1:] + [dict(agents[1], id="agent_d", name="D")]})
        assert r.status_code == 422
        # add a 5th participant sharing the same API profile, with its own role/model
        new = dict(agents[1], id="agent_d", name="Agent D", role_id="critic", model_override="fake-2")
        r = await e.client.put("/api/agents", json={"agents": agents + [new]})
        assert r.status_code == 200, r.text
        assert len(r.json()["agents"]) == 5
        # participant cannot be granted chair role / document capabilities
        bad = dict(new, role_id="chair")
        r = await e.client.put("/api/agents", json={"agents": agents + [bad]})
        assert r.status_code == 422


async def test_profile_crud_secret_never_returned(env_factory, caplog):
    secret = "sk-test-SUPERSECRET-1234567890"
    async with env_factory() as e:
        r = await e.client.post("/api/profiles", json={"name": "My Cloud", "protocol": "openai_chat_compatible",
                                                       "base_url": "https://example.invalid/v1", "default_model": "m1",
                                                       "api_key": secret, "billing_family": "other_cloud"})
        assert r.status_code == 201, r.text
        p = r.json()
        assert secret not in r.text and p["has_key"] is True
        pid, env_var = p["id"], p["env_var"]
        # rename + protocol change keep id/env_var
        r = await e.client.patch(f"/api/profiles/{pid}", json={"name": "Renamed", "protocol": "anthropic_messages"})
        assert r.status_code == 200
        assert r.json()["id"] == pid and r.json()["env_var"] == env_var and r.json()["name"] == "Renamed"
        # rotate key
        r = await e.client.put(f"/api/profiles/{pid}/secret", json={"api_key": secret + "X"})
        assert r.status_code == 204 and secret not in r.text
        listing = (await e.client.get("/api/profiles")).text
        assert secret not in listing
        # stored only in .env
        env_text = (e.home / ".env").read_text()
        assert f"{env_var}={secret}X" in env_text
        for f in (e.home / "config").glob("*.json"):
            assert secret not in f.read_text()
        dbdump = "\n".join(e.c.db.conn.iterdump())
        assert secret not in dbdump
        # logs are redacted
        logging.getLogger("app").warning("leak attempt %s", secret)
        assert secret not in caplog.text
        # connection test failure does not echo the key
        r = await e.client.post(f"/api/profiles/{pid}/test")
        assert secret not in r.text


async def test_agents_reference_profile_by_id_after_rename(env_factory):
    async with env_factory() as e:
        r = await e.client.patch("/api/profiles/api_fake", json={"name": "Fake renamed"})
        assert r.status_code == 200
        agents = (await e.client.get("/api/agents")).json()["agents"]
        assert all(a["profile_id"] == "api_fake" for a in agents)


async def test_roles_save_reload_and_constraints(env_factory):
    async with env_factory() as e:
        r = await e.client.post("/api/roles", json={"id": "facilitator", "name": "ファシリ", "description": "d",
                                                    "agent_md": "# ファシリ\n進行を補助する。"})
        assert r.status_code == 201
        r = await e.client.get("/api/roles/facilitator")
        assert r.json()["agent_md"].startswith("# ファシリ")
        assert json.loads((e.home / "roles" / "facilitator" / "role.json").read_text())["agent_file"] == "Agent.MD"
        # id immutable
        r = await e.client.put("/api/roles/facilitator", json={"id": "other", "name": "x", "agent_md": ""})
        assert r.status_code == 422
        # in-use role can't be deleted
        r = await e.client.delete("/api/roles/critic")
        assert r.status_code == 409
        r = await e.client.delete("/api/roles/chair")
        assert r.status_code == 409
        # import requires diff confirmation
        r = await e.client.post("/api/roles/facilitator/import-agent-md", json={"content": "# 新しい内容"})
        assert r.json()["applied"] is False and "+# 新しい内容" in r.json()["diff"]
        assert (await e.client.get("/api/roles/facilitator")).json()["agent_md"].startswith("# ファシリ")
        r = await e.client.post("/api/roles/facilitator/import-agent-md", json={"content": "# 新しい内容", "confirm": True})
        assert r.json()["applied"] is True


async def test_role_md_is_sent_as_system_instruction(env_factory):
    async with env_factory() as e:
        await e.client.put("/api/roles/critic", json={"name": "批判・検証担当", "agent_md": "# 批判役 MARKER-XYZ"})
        sid = await e.new_session()
        await e.start_and_idle(sid)
        crit_calls = [c for c in e.calls("turn") if c["meta"]["role_id"] == "critic"]
        assert crit_calls and all("MARKER-XYZ" in c["payload"]["system"] for c in crit_calls)


async def test_auth_and_csrf(env_factory):
    async with env_factory() as e:
        import httpx
        transport = httpx.ASGITransport(app=e.app, client=("127.0.0.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as anon:
            assert (await anon.get("/api/agents")).status_code == 401
            await anon.get("/api/auth/bootstrap")
            assert (await anon.get("/api/agents")).status_code == 200
            # missing CSRF header on a mutating call
            assert (await anon.post("/api/sessions", json={"topic": "x"})).status_code == 403
            # foreign origin
            r = await anon.get("/api/agents", headers={"Origin": "https://evil.example"})
            assert r.status_code == 403
        remote = httpx.ASGITransport(app=e.app, client=("203.0.113.5", 1))
        async with httpx.AsyncClient(transport=remote, base_url="http://x") as rc:
            assert (await rc.get("/api/auth/bootstrap")).status_code == 403
