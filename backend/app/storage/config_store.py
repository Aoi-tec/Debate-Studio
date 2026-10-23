"""JSON config files and role library (DESIGN 4.2–4.4, 3.4).

Writes are atomic (temp + fsync + replace) and keep a ``.bak`` of the previous
version for rollback when the new file fails validation on reload.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.core.fsutil import replace_with_retry
from app.models.schemas import AgentConfig, AgentsDoc, ApiProfile, DebateSettings, RoleMeta

DEFAULT_ROLES: dict[str, tuple[dict, str]] = {
    "chair": (
        {"id": "chair", "name": "議長（Orchestrator）", "description": "資料解析・根拠照合・議論進行・ユーザー差し戻し",
         "agent_file": "Agent.MD", "capabilities": ["document_analysis", "retrieval", "context_distribution"]},
        "# Chair — Agent.MD\n\nあなたは複数AIの討論を進行する議長である。\n",
    ),
    "proposer": (
        {"id": "proposer", "name": "提案担当", "description": "実装可能な解決策・長短・前提を示す",
         "agent_file": "Agent.MD", "capabilities": ["text_debate"]},
        "# 提案担当 — Agent.MD\n\nあなたは解決策を提案する一般Agentである。\n",
    ),
    "critic": (
        {"id": "critic", "name": "批判・検証担当", "description": "根拠不足・誤り・矛盾・リスクを指摘する",
         "agent_file": "Agent.MD", "capabilities": ["text_debate"]},
        "# 批判・検証担当 — Agent.MD\n\nあなたは一般Agentの批判・検証担当である。\n",
    ),
    "alternative": (
        {"id": "alternative", "name": "代替案担当", "description": "別の実装ルート・費用・保守性を比較する",
         "agent_file": "Agent.MD", "capabilities": ["text_debate"]},
        "# 代替案担当 — Agent.MD\n\nあなたは一般Agentの代替案担当である。\n",
    ),
}


def atomic_write_text(path: Path, content: str, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ConfigStore:
    def __init__(self, config_dir: Path, roles_dir: Path, enable_fake: bool = False):
        self.config_dir = Path(config_dir)
        self.roles_dir = Path(roles_dir)
        self.enable_fake = enable_fake
        self._lock = threading.RLock()
        self.seed_defaults()

    # ------------------------------------------------------------ files
    @property
    def profiles_path(self) -> Path:
        return self.config_dir / "api_profiles.json"

    @property
    def agents_path(self) -> Path:
        return self.config_dir / "agents.json"

    @property
    def settings_path(self) -> Path:
        return self.config_dir / "debate_settings.json"

    def _read(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bak = path.with_suffix(path.suffix + ".bak")
            if bak.exists():
                data = json.loads(bak.read_text(encoding="utf-8"))
                atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2), backup=False)
                return data
            raise

    def _write(self, path: Path, data: Any) -> None:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    # ------------------------------------------------------------ seeding
    def seed_defaults(self) -> None:
        with self._lock:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.roles_dir.mkdir(parents=True, exist_ok=True)
            for rid, (meta, md) in DEFAULT_ROLES.items():
                d = self.roles_dir / rid
                if not (d / "role.json").exists():
                    d.mkdir(parents=True, exist_ok=True)
                    self._write(d / "role.json", meta)
                    if not (d / "Agent.MD").exists():
                        atomic_write_text(d / "Agent.MD", md, backup=False)
            if not self.profiles_path.exists():
                profiles = [
                    ApiProfile(id="api_001", name="Local LLM", protocol="openai_chat_compatible",
                               base_url="http://localhost:11434/v1", default_model="", env_var=None,
                               billing_family="local", allow_document_transfer=True).model_dump(),
                    ApiProfile(id="api_002", name="OpenAI", protocol="openai_chat_compatible",
                               base_url="https://api.openai.com/v1", default_model="",
                               env_var="LLM_API_002_KEY", billing_family="openai_claude",
                               quota={"mode": "auto", "admin_env_var": "LLM_ADMIN_USAGE_KEY"}).model_dump(),
                ]
                if self.enable_fake:
                    profiles.append(ApiProfile(id="api_fake", name="Fake（デモ用・課金なし）", protocol="fake",
                                               default_model="fake-model", billing_family="local",
                                               allow_document_transfer=True).model_dump())
                self._write(self.profiles_path, {"profiles": profiles})
            if not self.agents_path.exists():
                pid = "api_fake" if self.enable_fake else "api_002"
                agents = [
                    AgentConfig(id="agent_chair", name="Chair", kind="chair", role_id="chair", profile_id=pid, avatar="🧑‍⚖️"),
                    AgentConfig(id="agent_a", name="Agent A", kind="participant", role_id="proposer", profile_id=pid, avatar="💡"),
                    AgentConfig(id="agent_b", name="Agent B", kind="participant", role_id="critic", profile_id=pid, avatar="🔍"),
                    AgentConfig(id="agent_c", name="Agent C", kind="participant", role_id="alternative", profile_id=pid, avatar="🔀"),
                ]
                self._write(self.agents_path, {"agents": [a.model_dump() for a in agents]})
            if not self.settings_path.exists():
                self._write(self.settings_path, DebateSettings().model_dump())
            # ensure the fake profile exists when demo mode is switched on later
            if self.enable_fake:
                data = self._read(self.profiles_path)
                if not any(p["id"] == "api_fake" for p in data["profiles"]):
                    data["profiles"].append(ApiProfile(id="api_fake", name="Fake（デモ用・課金なし）", protocol="fake",
                                                       default_model="fake-model", billing_family="local",
                                                       allow_document_transfer=True).model_dump())
                    self._write(self.profiles_path, data)

    # ------------------------------------------------------------ profiles
    def profiles(self) -> list[ApiProfile]:
        with self._lock:
            data = self._read(self.profiles_path)
            out = [ApiProfile.model_validate(p) for p in data.get("profiles", [])]
            if not self.enable_fake:
                out = [p for p in out if p.protocol != "fake"]
            return out

    def profile(self, pid: str) -> ApiProfile | None:
        return next((p for p in self.profiles() if p.id == pid), None)

    def save_profiles(self, profiles: list[ApiProfile]) -> None:
        with self._lock:
            ids = [p.id for p in profiles]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate profile id")
            existing = self._read(self.profiles_path).get("profiles", [])
            hidden = [p for p in existing if p.get("protocol") == "fake" and not self.enable_fake]
            self._write(self.profiles_path, {"profiles": [p.model_dump() for p in profiles] + hidden})

    def next_profile_id(self) -> str:
        data = self._read(self.profiles_path).get("profiles", [])
        nums = [int(m.group(1)) for p in data if (m := re.match(r"^api_(\d+)$", p["id"]))]
        return f"api_{(max(nums) + 1 if nums else 1):03d}"

    # ------------------------------------------------------------ agents
    def agents(self) -> list[AgentConfig]:
        with self._lock:
            data = self._read(self.agents_path)
            return AgentsDoc.model_validate(data).agents

    def save_agents(self, agents: list[AgentConfig], max_agents: int | None = None) -> None:
        with self._lock:
            doc = AgentsDoc(agents=agents)
            if max_agents and len(doc.agents) > max_agents:
                raise ValueError(f"Agent数の上限({max_agents})を超えています")
            pids = {p.id for p in self.profiles()}
            rids = {r.id for r in self.roles()}
            for a in doc.agents:
                if a.profile_id not in pids:
                    raise ValueError(f"{a.id}: 不明なAPIプロファイル {a.profile_id}")
                if a.role_id not in rids:
                    raise ValueError(f"{a.id}: 不明な役割 {a.role_id}")
                for tier, p in a.routing_profiles.items():
                    if p and p not in pids:
                        raise ValueError(f"{a.id}: {tier} の不明なプロファイル {p}")
            # chair id is immutable once created
            old = self._read(self.agents_path).get("agents", [])
            old_chair = next((x for x in old if x.get("kind") == "chair"), None)
            new_chair = next(a for a in doc.agents if a.kind == "chair")
            if old_chair and old_chair["id"] != new_chair.id:
                raise ValueError("議長スロットは削除・差し替えできません")
            self._write(self.agents_path, {"agents": [a.model_dump() for a in doc.agents]})

    # ------------------------------------------------------------ settings
    def settings(self) -> DebateSettings:
        with self._lock:
            raw = self._read(self.settings_path)
            if int(raw.get("settings_version") or 1) < 2:
                # v1 → v2: the old 800-token default left no room once a model starts thinking
                deb = raw.setdefault("debate", {})
                if deb.get("max_output_tokens", 800) == 800:
                    deb["max_output_tokens"] = 1500
                raw["settings_version"] = 2
                s = DebateSettings.model_validate(raw)
                self._write(self.settings_path, s.model_dump())
                return s
            return DebateSettings.model_validate(raw)

    def save_settings(self, s: DebateSettings) -> None:
        with self._lock:
            self._write(self.settings_path, s.model_dump())

    # ------------------------------------------------------------ roles
    def roles(self) -> list[RoleMeta]:
        out = []
        with self._lock:
            for d in sorted(self.roles_dir.iterdir() if self.roles_dir.exists() else []):
                rj = d / "role.json"
                if d.is_dir() and rj.exists():
                    try:
                        out.append(RoleMeta.model_validate(json.loads(rj.read_text(encoding="utf-8"))))
                    except Exception:
                        continue
        return out

    def role(self, rid: str) -> tuple[RoleMeta, str] | None:
        if not re.match(r"^[a-z0-9][a-z0-9_\-]{0,62}$", rid):
            return None
        d = self.roles_dir / rid
        rj = d / "role.json"
        if not rj.exists():
            return None
        meta = RoleMeta.model_validate(json.loads(rj.read_text(encoding="utf-8")))
        md_path = d / meta.agent_file
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        return meta, md

    def save_role(self, meta: RoleMeta, agent_md: str) -> None:
        with self._lock:
            d = self.roles_dir / meta.id
            d.mkdir(parents=True, exist_ok=True)
            self._write(d / "role.json", meta.model_dump())
            atomic_write_text(d / meta.agent_file, agent_md)

    def role_in_use(self, rid: str) -> list[str]:
        return [a.id for a in self.agents() if a.role_id == rid]

    def delete_role(self, rid: str) -> None:
        if rid == "chair":
            raise ValueError("chair 役割は削除できません")
        users = self.role_in_use(rid)
        if users:
            raise ValueError(f"使用中の役割です: {', '.join(users)}")
        d = self.roles_dir / rid
        if d.exists():
            shutil.rmtree(d)
