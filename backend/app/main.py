"""FastAPI entry point: ``uvicorn app.main:app --app-dir backend``."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import config_api, sessions_api
from app.container import Container, build_container
from app.core.auth import COOKIE, AuthMiddleware, is_loopback
from app.core.redact import install_log_redaction, redact
from app.core.settings import AppSettings, load_settings
from app.providers.fake import FakeScript


def create_app(settings: AppSettings | None = None, fake_script: FakeScript | None = None,
               container: Container | None = None) -> FastAPI:
    settings = settings or load_settings()
    install_log_redaction()
    token = secrets.token_urlsafe(32)
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = container or build_container(settings, fake_script)
        app.state.container = c
        holder["c"] = c
        c.debate.recover_after_restart()
        yield
        for t in list(c.debate.tasks.values()):
            t.cancel()

    app = FastAPI(title="Debate Studio", version=__version__, lifespan=lifespan)
    app.state.auth_token = token
    app.add_middleware(AuthMiddleware, token=token, allowed_origins=settings.allowed_origins)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errs = [{"loc": e.get("loc"), "msg": redact(e.get("msg"))} for e in exc.errors()]  # never echo input values
        return JSONResponse({"detail": {"message": "入力が不正です", "detail": errs}}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logging.getLogger("app").exception("unhandled error")
        return JSONResponse({"detail": {"message": "サーバー内部エラー", "detail": redact(type(exc).__name__)}}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/auth/bootstrap")
    def bootstrap(request: Request):
        host = request.client.host if request.client else None
        if not is_loopback(host):
            return JSONResponse({"detail": {"message": "localhost からのみ利用できます"}}, status_code=403)
        resp = JSONResponse({"ok": True, "demo_mode": settings.enable_fake_provider})
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
        return resp

    app.include_router(config_api.router)
    app.include_router(sessions_api.router)

    dist = settings.frontend_dist
    if dist.exists() and (dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return Response(status_code=404)
            f = (dist / path).resolve()
            if path and f.is_file() and dist.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
