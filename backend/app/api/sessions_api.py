"""/api/sessions/* , /api/files/* , evidence endpoints and the SSE stream."""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import C, http_error
from app.container import Container
from app.models.schemas import AttachIn, AttachPatch, DecisionIn, EvidenceResponseIn, MessageIn, SessionCreate
from app.services.attachments import UploadRejected
from app.services.debate import SessionError
from app.services.evidence import EvidenceConflict

router = APIRouter(prefix="/api")


def _wrap(fn):
    try:
        return fn()
    except SessionError as e:
        raise http_error(e.status, str(e), e.detail)
    except EvidenceConflict as e:
        raise http_error(e.status, str(e))


async def _wrap_async(coro):
    try:
        return await coro
    except SessionError as e:
        raise http_error(e.status, str(e), e.detail)
    except EvidenceConflict as e:
        raise http_error(e.status, str(e))


# ---------------------------------------------------------------- sessions
@router.get("/sessions")
async def list_sessions(c: Container = Depends(C)):
    return {"sessions": c.debate.list()}


@router.post("/sessions", status_code=201)
async def create_session(body: SessionCreate, c: Container = Depends(C)):
    return _wrap(lambda: c.debate.create(body.topic, body.attachment_ids, body.debate_rounds))


@router.get("/sessions/{sid}")
async def get_session(sid: str, c: Container = Depends(C)):
    def f():
        d = c.debate.public(sid)
        d["messages"] = c.debate.messages(sid)
        d["usage"] = c.meter.summary(sid)
        d["usage"].pop("rows", None)
        d["attachments"] = [{**c.files.public(a["file_id"]), "allowed_agent_ids": a["allowed_agent_ids"],
                             "enabled": bool(a["enabled"]), "required": bool(a["required"]),
                             "allow_partial": bool(a["allow_partial"])}
                            for a in c.packets.attachments(sid, include_disabled=True)]
        d["evidence_requests"] = c.evidence.list(sid)
        d["last_event_id"] = (c.db.one("SELECT MAX(id) m FROM stream_events WHERE session_id=?", (sid,)) or {}).get("m") or 0
        d["budgets"] = c.db.all("SELECT agent_id, tier, used_tokens, limit_tokens FROM agent_budgets WHERE session_id=?", (sid,))
        d["baselines"] = c.db.all("SELECT pool_id, profile_id, family, u0, limit_tokens, source, fetched_at, estimated, detail FROM usage_baselines WHERE session_id=?", (sid,))
        d["handoffs"] = c.db.all("SELECT * FROM model_handoffs WHERE session_id=? ORDER BY created_at", (sid,))
        d["analysis_runs"] = c.db.all("SELECT id, purpose, status, file_ids_json, created_at, ended_at, error FROM chair_analysis_runs WHERE session_id=? ORDER BY created_at", (sid,))
        return d
    return _wrap(f)


@router.post("/sessions/{sid}/start")
async def start(sid: str, c: Container = Depends(C)):
    return await _wrap_async(c.debate.start(sid))


@router.post("/sessions/{sid}/pause")
async def pause(sid: str, c: Container = Depends(C)):
    return _wrap(lambda: c.debate.pause(sid))


@router.post("/sessions/{sid}/resume")
async def resume(sid: str, c: Container = Depends(C)):
    return _wrap(lambda: c.debate.resume(sid))


@router.post("/sessions/{sid}/cancel")
async def cancel(sid: str, c: Container = Depends(C)):
    return await _wrap_async(c.debate.cancel(sid))


@router.post("/sessions/{sid}/skip-agent/{aid}")
async def skip_agent(sid: str, aid: str, c: Container = Depends(C)):
    return _wrap(lambda: c.debate.skip_agent(sid, aid))


@router.post("/sessions/{sid}/compress")
async def compress(sid: str, c: Container = Depends(C)):
    _wrap(lambda: c.debate.request_compression(sid))
    return {"requested": True, "applies_at": "次のラウンド終了時"}


@router.get("/sessions/{sid}/checkpoint")
async def get_checkpoint(sid: str, c: Container = Depends(C)):
    return _wrap(lambda: c.debate.checkpoint(sid))


@router.post("/sessions/{sid}/decision")
async def decision(sid: str, body: DecisionIn, c: Container = Depends(C),
             idempotency_key: Optional[str] = Header(default=None)):
    return _wrap(lambda: c.debate.decide(sid, body.decision, body.instruction, body.idempotency_key or idempotency_key))


@router.post("/sessions/{sid}/messages", status_code=201)
async def post_message(sid: str, body: MessageIn, c: Container = Depends(C)):
    if not body.text.strip() and not body.attachment_ids:
        raise http_error(422, "本文または添付が必要です")
    return _wrap(lambda: c.debate.add_user_message(sid, body.text, body.attachment_ids))


@router.get("/sessions/{sid}/usage")
async def usage(sid: str, c: Container = Depends(C)):
    _wrap(lambda: c.debate.row(sid))
    return c.meter.summary(sid)


# ---------------------------------------------------------------- attachments in a session
@router.post("/sessions/{sid}/attachments", status_code=201)
async def attach(sid: str, body: AttachIn, c: Container = Depends(C)):
    _wrap(lambda: c.debate.attach(sid, body.file_id, body.allowed_agent_ids, required=body.required,
                                  allow_partial=body.allow_partial))
    return {"file_id": body.file_id, "applies_at": "議長の解析・配布完了後、次の安全な発言境界から"}


@router.patch("/sessions/{sid}/attachments/{fid}")
async def patch_attachment(sid: str, fid: str, body: AttachPatch, c: Container = Depends(C)):
    data = body.model_dump(exclude_unset=True)
    _wrap(lambda: c.debate.patch_attachment(sid, fid, allowed=data.get("allowed_agent_ids", "__keep__"),
                                            enabled=data.get("enabled"), required=data.get("required"),
                                            allow_partial=data.get("allow_partial")))
    return {"file_id": fid, "updated": True, "note": "配布済みの過去のLLM入力からは回収できません（履歴は監査用に記録）"}


@router.delete("/sessions/{sid}/attachments/{fid}", status_code=204)
async def detach(sid: str, fid: str, c: Container = Depends(C)):
    _wrap(lambda: c.debate.detach(sid, fid))


@router.post("/sessions/{sid}/files/{fid}/reanalyze")
async def reanalyze(sid: str, fid: str, c: Container = Depends(C)):
    _wrap(lambda: c.debate.reanalyze(sid, fid))
    return {"requested": True}


@router.get("/sessions/{sid}/context-packets")
async def context_packets(sid: str, c: Container = Depends(C)):
    _wrap(lambda: c.debate.row(sid))
    return {"packets": c.packets.list_for_session(sid)}


@router.post("/sessions/{sid}/context-requests", status_code=202)
async def post_context_request(sid: str, body: dict, c: Container = Depends(C)):
    """Operator/UI entry point (participants never get HTTP access)."""
    agent_id = body.get("agent_id")
    ctx = _wrap(lambda: c.debate.ctx(sid))
    if agent_id not in ctx.agents or ctx.agents[agent_id].kind != "participant":
        raise http_error(422, "一般Agentを指定してください")
    res = await c.evidence.handle_context_request(ctx, agent_id, body.get("turn_id") or "", body)
    if res.get("status") == "waiting" and c.debate.row(sid)["state"] not in ("COMPLETED", "CANCELLED", "FAILED"):
        c.debate._queue_redo(sid, agent_id, body.get("turn_id") or "", res)
    return res


# ---------------------------------------------------------------- evidence requests
@router.get("/sessions/{sid}/evidence-requests")
async def list_evidence(sid: str, c: Container = Depends(C)):
    _wrap(lambda: c.debate.row(sid))
    return {"evidence_requests": c.evidence.list(sid)}


@router.get("/sessions/{sid}/evidence-requests/{er}")
async def get_evidence(sid: str, er: str, c: Container = Depends(C)):
    d = c.evidence.public(er)
    if not d or d.get("session_id") != sid:
        raise http_error(404, "要求が見つかりません")
    return d


@router.post("/sessions/{sid}/evidence-requests/{er}/respond")
async def respond(sid: str, er: str, body: EvidenceResponseIn, c: Container = Depends(C),
                  idempotency_key: Optional[str] = Header(default=None)):
    if idempotency_key and idempotency_key != body.idempotency_key:
        raise http_error(422, "Idempotency-Key ヘッダーと本文が一致しません")
    ctx = _wrap(lambda: c.debate.ctx(sid))
    return await _wrap_async(c.evidence.respond(ctx, er, body, c.files))


@router.post("/sessions/{sid}/evidence-requests/{er}/recheck", status_code=202)
async def recheck(sid: str, er: str, c: Container = Depends(C)):
    row = c.db.one("SELECT * FROM evidence_requests WHERE id=? AND session_id=?", (er, sid))
    if not row:
        raise http_error(404, "要求が見つかりません")
    if row["state"] not in ("WAITING_USER_EVIDENCE", "STILL_MISSING"):
        raise http_error(409, f"{row['state']} のため再検証できません")
    t = c.evidence._tasks.get(er)
    if t and not t.done():
        return {"merged": True}  # duplicate job merged
    ctx = c.debate.ctx(sid)
    c.evidence._set_state(er, row["state"], "REVALIDATING", "user")
    c.evidence._tasks[er] = asyncio.get_event_loop().create_task(c.evidence._revalidate(ctx, er, [], c.files))
    return {"state": "REVALIDATING"}


# ---------------------------------------------------------------- SSE
@router.get("/sessions/{sid}/events")
async def events(sid: str, request: Request, c: Container = Depends(C),
                 last_event_id: Optional[str] = Header(default=None)):
    _wrap(lambda: c.debate.row(sid))
    try:
        after = int(last_event_id or request.query_params.get("last_event_id") or 0)
    except ValueError:
        after = 0
    q = c.bus.subscribe(sid)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            last = after
            for ev in c.bus.replay(sid, after):
                last = ev["event_id"]
                yield f"event: {ev['type']}\nid: {ev['event_id']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=c.settings.heartbeat_s)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if ev["event_id"] <= last:
                    continue
                last = ev["event_id"]
                yield f"event: {ev['type']}\nid: {ev['event_id']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            c.bus.unsubscribe(sid, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- files
@router.get("/files")
async def list_files(c: Container = Depends(C)):
    return {"files": c.files.list(),
            "limits": {"max_file_mib": c.settings.max_file_bytes // (1024 * 1024),
                       "max_session_mib": c.settings.max_session_bytes // (1024 * 1024),
                       "max_files_per_session": c.settings.max_files_per_session,
                       "accepted": ["txt", "md", "csv", "json", "pdf", "docx", "xlsx", "pptx", "png", "jpg", "jpeg", "webp"]}}


@router.post("/files", status_code=201)
async def upload(files: list[UploadFile] = File(...), c: Container = Depends(C)):
    out, errors = [], []
    for f in files:
        try:
            out.append(await c.files.save_upload(f.filename or "file", f, f.content_type))
        except UploadRejected as e:
            errors.append({"name": f.filename, "status": e.status, "code": e.code, "message": str(e)})
        finally:
            await f.close()
    if errors and not out:
        raise http_error(errors[0]["status"], errors[0]["message"], errors)
    return {"files": out, "errors": errors}


@router.get("/files/{fid}")
async def get_file(fid: str, c: Container = Depends(C)):
    d = c.files.public(fid)
    if not d:
        raise http_error(404, "ファイルが見つかりません")
    return d


@router.get("/files/{fid}/preview")
async def preview(fid: str, c: Container = Depends(C)):
    d = c.files.preview(fid)
    if not d:
        raise http_error(404, "ファイルが見つかりません")
    return d


@router.get("/files/{fid}/download")
async def download(fid: str, c: Container = Depends(C)):
    r = c.files.path_for_download(fid)
    if not r:
        raise http_error(404, "ファイルが見つかりません")
    path, name, mime = r
    return FileResponse(path, media_type="application/octet-stream", filename=name,
                        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})


@router.delete("/files/{fid}", status_code=204)
async def delete_file(fid: str, c: Container = Depends(C)):
    try:
        c.files.delete(fid)
    except KeyError:
        raise http_error(404, "ファイルが見つかりません")
