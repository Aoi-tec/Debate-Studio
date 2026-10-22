"""Shared fixtures: temp home, FakeProvider-backed app, authenticated async client."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.container import build_container  # noqa: E402
from app.core.settings import AppSettings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.fake import FakeScript  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Env:
    def __init__(self, home: Path):
        self.home = home
        self.script = FakeScript(delay_s=0.0, chunk_chars=20)
        self.settings = AppSettings(home=home, enable_fake_provider=True, extraction_timeout_s=30)
        self.c = build_container(self.settings, self.script)
        self.c.files.in_process = True
        self.c.runner.backoff_base = 0.0
        self.app = create_app(self.settings, self.script, container=self.c)
        self.app.state.container = self.c
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 5555))
        self.client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000",
                                        headers={"X-Requested-With": "DebateStudio"})
        r = await self.client.get("/api/auth/bootstrap")
        assert r.status_code == 200
        return self

    async def __aexit__(self, *a):
        for t in list(self.c.debate.tasks.values()):
            t.cancel()
        await asyncio.sleep(0)
        await self.client.aclose()

    # helpers ------------------------------------------------------------
    async def upload(self, name: str, data: bytes):
        r = await self.client.post("/api/files", files={"files": (name, data)})
        return r

    async def upload_ready(self, name: str, data: bytes) -> dict:
        r = await self.upload(name, data)
        assert r.status_code == 201, r.text
        f = r.json()["files"][0]
        await self.c.files.wait([f["file_id"]])
        return self.c.files.public(f["file_id"])

    async def new_session(self, topic: str = "見積AIの設計比較", attachment_ids=None, rounds=None) -> str:
        r = await self.client.post("/api/sessions", json={"topic": topic, "attachment_ids": attachment_ids or [],
                                                          "debate_rounds": rounds})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def start_and_idle(self, sid: str) -> dict:
        r = await self.client.post(f"/api/sessions/{sid}/start")
        assert r.status_code == 200, r.text
        await self.c.debate.wait_idle(sid)
        return (await self.client.get(f"/api/sessions/{sid}")).json()

    async def session(self, sid: str) -> dict:
        return (await self.client.get(f"/api/sessions/{sid}")).json()

    def events(self, sid: str) -> list[dict]:
        return self.c.bus.replay(sid, 0)

    def calls(self, purpose: str | None = None, agent_id: str | None = None) -> list[dict]:
        return [x for x in self.script.calls
                if (purpose is None or x["meta"].get("purpose") == purpose)
                and (agent_id is None or x["meta"].get("agent_id") == agent_id)]


@pytest.fixture
def env_factory(tmp_path):
    def make() -> Env:
        return Env(tmp_path / "home")
    return make
