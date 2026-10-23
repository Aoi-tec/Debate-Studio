"""/api/profiles, /api/roles, /api/agents, /api/settings."""
from __future__ import annotations

import difflib
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app.api.deps import C, http_error
from app.container import Container
from app.core.redact import redact
from app.models.schemas import (AgentConfig, ApiProfile, ApiProfileCreate, ApiProfilePatch, BudgetPut,
                                DebateSettings, ImportAgentMd, QualityStatus, QuotaPut, ReasoningConfig, RoleMeta, RoleUpsert, SecretPut)
from app.providers.base import LLMRequest
from app.services.budget import month_start_unix

router = APIRouter(prefix="/api")


def profile_public(c: Container, p: ApiProfile) -> dict:
    d = p.model_dump()
    d["has_key"] = c.secrets.has(p.env_var) if p.env_var else None
    d["quota"]["has_admin_key"] = c.secrets.has(p.quota.admin_env_var) if p.quota.admin_env_var else False
    return d


# ---------------------------------------------------------------- profiles
@router.get("/profiles")
async def list_profiles(c: Container = Depends(C)):
    return {"profiles": [profile_public(c, p) for p in c.config.profiles()],
            "protocols": ["openai_chat_compatible", "openai_responses", "anthropic_messages", "gemini_generate_content"]
            + (["fake"] if c.settings.enable_fake_provider else [])}


@router.post("/profiles", status_code=201)
async def create_profile(body: ApiProfileCreate, c: Container = Depends(C)):
    if body.protocol == "fake" and not c.settings.enable_fake_provider:
        raise http_error(422, "fake 方式はデモモードでのみ利用できます")
    profiles = c.config.profiles()
    pid = c.config.next_profile_id()
    env_var = None
    if body.requires_key:
        env_var = f"LLM_API_{pid.split('_')[1]}_KEY"
    try:
        p = ApiProfile(id=pid, name=body.name, protocol=body.protocol, base_url=body.base_url,
                       default_model=body.default_model, env_var=env_var, billing_family=body.billing_family,
                       allow_document_transfer=body.allow_document_transfer,
                       reasoning=body.reasoning or ReasoningConfig(),
                       max_concurrent_requests=body.max_concurrent_requests,
                       quota={"mode": "auto" if body.billing_family != "local" else "disabled", "scope": "per_agent"})
    except ValidationError as e:
        raise http_error(422, "入力が不正です", [redact(x["msg"]) for x in e.errors()])
    if body.api_key and env_var:
        c.secrets.set(env_var, body.api_key.strip())
    c.config.save_profiles(profiles + [p])
    c.db.audit("profile.create", {"profile_id": pid})
    return profile_public(c, p)


@router.patch("/profiles/{pid}")
async def patch_profile(pid: str, body: ApiProfilePatch, c: Container = Depends(C)):
    profiles = c.config.profiles()
    p = next((x for x in profiles if x.id == pid), None)
    if not p:
        raise http_error(404, "プロファイルが見つかりません")
    data = p.model_dump()
    for k, v in body.model_dump(exclude_unset=True).items():
        data[k] = v
    if data.get("protocol") == "fake" and not c.settings.enable_fake_provider:
        raise http_error(422, "fake 方式はデモモードでのみ利用できます")
    if data["protocol"] != p.protocol or data.get("default_model") != p.default_model or data.get("base_url") != p.base_url:
        data["quality"] = QualityStatus().model_dump()  # quality evidence no longer applies
    try:
        newp = ApiProfile.model_validate(data)
    except ValidationError as e:
        raise http_error(422, "入力が不正です", [redact(x["msg"]) for x in e.errors()])
    assert newp.id == p.id and newp.env_var == p.env_var  # fixed id / env var
    c.config.save_profiles([newp if x.id == pid else x for x in profiles])
    c.db.audit("profile.patch", {"profile_id": pid, "fields": list(body.model_dump(exclude_unset=True))})
    return profile_public(c, newp)


@router.put("/profiles/{pid}/secret", status_code=204)
async def put_secret(pid: str, body: SecretPut, request: Request, c: Container = Depends(C)):
    p = c.config.profile(pid)
    if not p:
        raise http_error(404, "プロファイルが見つかりません")
    if not p.env_var:
        # a keyless local profile gains a key: allocate a fixed env var once
        profiles = c.config.profiles()
        env_var = f"LLM_API_{pid.split('_')[-1].upper()}_KEY" if re.match(r"^api_\d+$", pid) else c.secrets.next_free_var()
        p = p.model_copy(update={"env_var": env_var})
        c.config.save_profiles([p if x.id == pid else x for x in profiles])
    host = request.client.host if request.client else ""
    if request.url.scheme != "https" and host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise http_error(400, "秘密の登録は HTTPS 経由でのみ可能です")
    c.secrets.set(p.env_var, body.api_key.strip())
    c.db.audit("profile.secret_set", {"profile_id": pid, "env_var": p.env_var})


@router.post("/profiles/{pid}/test")
async def test_profile(pid: str, c: Container = Depends(C)):
    p = c.config.profile(pid)
    if not p:
        raise http_error(404, "プロファイルが見つかりません")
    if not p.default_model:
        return {"ok": False, "stage": "config", "message": "既定モデルが未設定です"}
    if p.env_var and not c.secrets.has(p.env_var):
        return {"ok": False, "stage": "config", "message": "APIキーが未登録です"}
    adapter = c.registry.get(p.protocol)
    req = LLMRequest(model=p.default_model, system="Reply with the single word: OK",
                     messages=[{"role": "user", "content": "ping"}], temperature=0, max_output_tokens=16, timeout_s=30,
                     metadata={"purpose": "connection_test"})
    t0 = time.time()
    got_text, streamed, finished, err = "", False, None, None
    async for ev in adapter.stream_completion(req, base_url=p.base_url, api_key=c.secrets.get(p.env_var)):
        if ev.kind == "TEXT_DELTA":
            streamed = True
            got_text += ev.text
        elif ev.kind == "FINISHED":
            finished = ev.finish_reason
        elif ev.kind == "ERROR":
            err = ev.error
            break
    if err:
        stage = {"auth": "auth", "bad_request": "model_or_params"}.get(err.kind, "network")
        return {"ok": False, "stage": stage, "message": redact(str(err))}
    return {"ok": True, "streaming": streamed, "finish_reason": finished, "latency_ms": int((time.time() - t0) * 1000),
            "sample": redact(got_text[:40])}


@router.get("/profiles/{pid}/models")
async def list_models(pid: str, c: Container = Depends(C)):
    """Model IDs offered by an OpenAI-compatible server (Strata, Ollama, LM Studio, OpenAI…)."""
    import httpx

    from app.providers import base as _pb
    p = c.config.profile(pid)
    if not p:
        raise http_error(404, "プロファイルが見つかりません")
    if p.protocol not in ("openai_chat_compatible", "openai_responses"):
        return {"models": [], "message": "この方式ではモデル一覧の自動取得に未対応です"}
    base = (p.base_url or "https://api.openai.com/v1").rstrip("/")
    headers = {}
    key = c.secrets.get(p.env_var)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with _pb.client(15) as client:
            r = await client.get(f"{base}/models", headers=headers)
        if r.status_code >= 400:
            return {"models": [], "message": redact(f"HTTP {r.status_code}: {r.text[:200]}")}
        data = r.json().get("data", [])
        return {"models": sorted({m.get("id") for m in data if isinstance(m, dict) and m.get("id")})}
    except (httpx.HTTPError, ValueError) as e:
        return {"models": [], "message": redact(f"取得できません: {type(e).__name__}")}


@router.get("/profiles/{pid}/quota")
async def get_quota(pid: str, c: Container = Depends(C)):
    p = c.config.profile(pid)
    if not p:
        raise http_error(404, "プロファイルが見つかりません")
    last = c.db.one("SELECT * FROM usage_baselines WHERE pool_id=? ORDER BY fetched_at DESC LIMIT 1", (p.pool_id,))
    return {"profile_id": pid, "pool_id": p.pool_id, "quota": profile_public(c, p)["quota"],
            "billing_family": p.billing_family, "formula": "U0 + U0/6" if p.billing_family == "openai_claude" else ("U0 + U0/4" if p.billing_family == "other_cloud" else "（ローカル: 口座閾値なし）"),
            "last_baseline": last,
            "note": "閾値はアプリ独自の自動上限であり、提供元の公式な残り枠ではありません。チャットアプリ契約の利用枠とも別物です。"}


@router.put("/profiles/{pid}/quota")
async def put_quota(pid: str, body: QuotaPut, c: Container = Depends(C)):
    profiles = c.config.profiles()
    p = next((x for x in profiles if x.id == pid), None)
    if not p:
        raise http_error(404, "プロファイルが見つかりません")
    q = body.quota
    if body.admin_api_key:
        if not q.admin_env_var:
            q = q.model_copy(update={"admin_env_var": "LLM_ADMIN_USAGE_KEY"})
        c.secrets.set(q.admin_env_var, body.admin_api_key.strip())
    newp = p.model_copy(update={"quota": q})
    c.config.save_profiles([newp if x.id == pid else x for x in profiles])
    c.db.audit("profile.quota", {"profile_id": pid, "mode": q.mode})
    return await get_quota(pid, c)


@router.post("/profiles/{pid}/quota/refresh")
async def refresh_quota(pid: str, c: Container = Depends(C)):
    p = c.config.profile(pid)
    if not p:
        raise http_error(404, "プロファイルが見つかりません")
    snap = await c.registry.get(p.protocol).read_usage_snapshot(
        base_url=p.base_url, admin_key=c.secrets.get(p.quota.admin_env_var), start_unix=month_start_unix())
    d = {"status": snap.status, "tokens": snap.tokens, "window": snap.window, "fetched_at": snap.fetched_at,
         "detail": redact(snap.detail)}
    if snap.status == "readable" and snap.tokens:
        frac = 1 / 6 if p.billing_family == "openai_claude" else 0.25
        d["threshold_if_started_now"] = int(snap.tokens + snap.tokens * frac)
    return d


QUALITY_PROBES = [
    {"packet": "ev_a1b2c3: 単価表の適用開始日は2026年4月1日", "q": "適用開始日は？", "ids": ["ev_a1b2c3"]},
    {"packet": "ev_d4e5f6: 月間処理件数は1,200件", "q": "月間処理件数は？", "ids": ["ev_d4e5f6"]},
    {"packet": "ev_0a0b0c: 保守契約は年額360万円", "q": "保守費用は？", "ids": ["ev_0a0b0c"]},
    {"packet": "（資料なし）", "q": "競合の価格は？（資料にない場合は UNKNOWN と答える）", "ids": []},
    {"packet": "ev_9f8e7d: 障害時の復旧目標は4時間", "q": "復旧目標は？", "ids": ["ev_9f8e7d"]},
]


@router.post("/profiles/{pid}/validate-quality")
async def validate_quality(pid: str, c: Container = Depends(C)):
    from app.services.context import parse_json_loose
    profiles = c.config.profiles()
    p = next((x for x in profiles if x.id == pid), None)
    if not p:
        raise http_error(404, "プロファイルが見つかりません")
    if not p.default_model:
        raise http_error(422, "既定モデルが未設定です")
    adapter = c.registry.get(p.protocol)
    passed = 0
    details = []
    for probe in QUALITY_PROBES:
        req = LLMRequest(model=p.default_model,
                         system="与えられたPacketの根拠だけで答え、JSON {\"answer\": str, \"evidence_ids\": [str]} のみを出力する。根拠がなければ answer を UNKNOWN、evidence_ids を空にする。",
                         messages=[{"role": "user", "content": f"Packet:\n{probe['packet']}\n質問: {probe['q']}"}],
                         temperature=0, max_output_tokens=200, timeout_s=60, json_mode=True,
                         metadata={"purpose": "quality_probe", "fake_hint": {"evidence_ids": probe["ids"]}})
        text, err = "", None
        async for ev in adapter.stream_completion(req, base_url=p.base_url, api_key=c.secrets.get(p.env_var)):
            if ev.kind == "TEXT_DELTA":
                text += ev.text
            elif ev.kind == "ERROR":
                err = ev.error
        obj = parse_json_loose(text)
        ok = (err is None and obj is not None and isinstance(obj.get("evidence_ids", []), list)
              and set(obj.get("evidence_ids", [])) <= set(probe["ids"]))
        passed += int(ok)
        details.append({"q": probe["q"], "ok": ok, "error": redact(str(err)) if err else None})
    rate = passed / len(QUALITY_PROBES)
    status = "passed" if rate >= 0.8 else "failed"
    newp = p.model_copy(update={"quality": QualityStatus(
        status=status, validated_model=p.default_model, validated_at=datetime.now(timezone.utc).isoformat(),
        summary=f"{passed}/{len(QUALITY_PROBES)} 合格（スキーマ・出典ID・UNKNOWN応答）")})
    c.config.save_profiles([newp if x.id == pid else x for x in profiles])
    return {"status": status, "pass_rate": rate, "details": details,
            "note": "簡易プローブ5件による最低限の検査です。設計書が推奨する過去議題20件以上の比較評価は別途実施してください。同等精度を保証するものではありません。"}


# ---------------------------------------------------------------- roles
@router.get("/roles")
async def list_roles(c: Container = Depends(C)):
    return {"roles": [{**r.model_dump(), "in_use_by": c.config.role_in_use(r.id)} for r in c.config.roles()]}


@router.post("/roles", status_code=201)
async def create_role(body: RoleUpsert, c: Container = Depends(C)):
    rid = body.id or re.sub(r"[^a-z0-9_\-]", "", body.name.lower().replace(" ", "_"))[:40] or f"role_{int(time.time())}"
    if not re.match(r"^[a-z0-9][a-z0-9_\-]{0,62}$", rid):
        rid = f"role_{int(time.time())}"
    if c.config.role(rid):
        raise http_error(409, f"役割ID {rid} は既に存在します")
    md = body.agent_md
    if body.copy_from:
        src = c.config.role(body.copy_from)
        if not src:
            raise http_error(404, "複製元の役割が見つかりません")
        md = md or src[1]
    meta = RoleMeta(id=rid, name=body.name, description=body.description)
    c.config.save_role(meta, md)
    return {**meta.model_dump(), "agent_md": md}


@router.get("/roles/{rid}")
async def get_role(rid: str, c: Container = Depends(C)):
    r = c.config.role(rid)
    if not r:
        raise http_error(404, "役割が見つかりません")
    return {**r[0].model_dump(), "agent_md": r[1], "in_use_by": c.config.role_in_use(rid)}


@router.put("/roles/{rid}")
async def put_role(rid: str, body: RoleUpsert, c: Container = Depends(C)):
    r = c.config.role(rid)
    if not r:
        raise http_error(404, "役割が見つかりません")
    if body.id and body.id != rid:
        raise http_error(422, "役割IDは変更できません")
    meta = r[0].model_copy(update={"name": body.name, "description": body.description})
    c.config.save_role(meta, body.agent_md)
    return await get_role(rid, c)


@router.delete("/roles/{rid}", status_code=204)
async def delete_role(rid: str, c: Container = Depends(C)):
    try:
        c.config.delete_role(rid)
    except ValueError as e:
        raise http_error(409, str(e))


@router.post("/roles/{rid}/import-agent-md")
async def import_agent_md(rid: str, body: ImportAgentMd, c: Container = Depends(C)):
    r = c.config.role(rid)
    if not r:
        raise http_error(404, "役割が見つかりません")
    diff = "\n".join(difflib.unified_diff(r[1].splitlines(), body.content.splitlines(), "現在のAgent.MD", "取込内容", lineterm=""))
    if not body.confirm:
        return {"applied": False, "diff": diff}
    c.config.save_role(r[0], body.content)
    c.db.audit("role.import_md", {"role_id": rid})
    return {"applied": True, "diff": diff}


# ---------------------------------------------------------------- agents
@router.get("/agents")
async def get_agents(c: Container = Depends(C)):
    s = c.config.settings()
    return {"agents": [{**a.model_dump(), "capabilities": a.capabilities} for a in c.config.agents()],
            "min_participants": 3, "max_agents": s.debate.max_agents}


@router.put("/agents")
async def put_agents(body: dict, c: Container = Depends(C)):
    try:
        agents = [AgentConfig.model_validate({k: v for k, v in a.items() if k != "capabilities"}) for a in body.get("agents", [])]
        c.config.save_agents(agents, max_agents=c.config.settings().debate.max_agents)
    except (ValidationError, ValueError) as e:
        msg = [redact(x["msg"]) for x in e.errors()] if isinstance(e, ValidationError) else str(e)
        raise http_error(422, "編成を保存できません", msg)
    c.db.audit("agents.save", {"count": len(agents)})
    return await get_agents(c)


@router.get("/agents/{aid}/budgets")
async def get_budgets(aid: str, c: Container = Depends(C)):
    a = next((x for x in c.config.agents() if x.id == aid), None)
    if not a:
        raise http_error(404, "Agentが見つかりません")
    return {"agent_id": aid, "tier_token_budgets": a.tier_token_budgets}


@router.put("/agents/{aid}/budgets")
async def put_budgets(aid: str, body: BudgetPut, c: Container = Depends(C)):
    agents = c.config.agents()
    a = next((x for x in agents if x.id == aid), None)
    if not a:
        raise http_error(404, "Agentが見つかりません")
    try:
        na = a.model_copy(update={"tier_token_budgets": body.tier_token_budgets})
        AgentConfig.model_validate(na.model_dump())
        c.config.save_agents([na if x.id == aid else x for x in agents])
    except (ValidationError, ValueError) as e:
        raise http_error(422, "予算を保存できません", str(e))
    # also apply to sessions that are paused for budget, so the user can raise a limit and resume
    for s in c.db.all("SELECT id FROM debate_sessions WHERE state IN ('PAUSED_BUDGET','PAUSED','WAITING_USER_DECISION','WAITING_USER_EVIDENCE')"):
        c.guard.set_agent_limits(s["id"], aid, body.tier_token_budgets)
    c.db.audit("agent.budgets", {"agent_id": aid, "budgets": body.tier_token_budgets})
    return {"agent_id": aid, "tier_token_budgets": body.tier_token_budgets}


@router.get("/agents/{aid}/routing")
async def get_routing(aid: str, session_id: str | None = None, c: Container = Depends(C)):
    a = next((x for x in c.config.agents() if x.id == aid), None)
    if not a:
        raise http_error(404, "Agentが見つかりません")
    profiles = {p.id: p for p in c.config.profiles()}
    tiers = []
    for tier, p, pid in c.router.tier_profiles(a, profiles):
        info = {"tier": tier, "profile_id": pid, "profile_name": p.name if p else None,
                "model": c.router.model_for(a, tier, p) if p else None,
                "quality": p.quality.model_dump() if p else None}
        if session_id:
            info["budget"] = c.guard.agent_budget(session_id, aid, tier)
            if p:
                info["pool"] = c.guard.pool_projection(session_id, p.pool_key(aid))
        tiers.append(info)
    handoffs = c.db.all("SELECT * FROM model_handoffs WHERE agent_id=? " + ("AND session_id=? " if session_id else "") + "ORDER BY created_at DESC LIMIT 50",
                        (aid, session_id) if session_id else (aid,))
    current = None
    if session_id:
        r = c.db.one("SELECT tier FROM messages WHERE session_id=? AND agent_id=? AND tier IS NOT NULL ORDER BY seq DESC LIMIT 1", (session_id, aid))
        current = r["tier"] if r else None
    return {"agent_id": aid, "current_tier": current, "tiers": tiers, "handoffs": handoffs}


# ---------------------------------------------------------------- settings
@router.get("/settings")
async def get_settings(c: Container = Depends(C)):
    return c.config.settings().model_dump()


@router.put("/settings")
async def put_settings(body: dict, c: Container = Depends(C)):
    try:
        s = DebateSettings.model_validate(body)
    except ValidationError as e:
        raise http_error(422, "設定が不正です", [redact(x["msg"]) for x in e.errors()])
    c.config.save_settings(s)
    return s.model_dump()
