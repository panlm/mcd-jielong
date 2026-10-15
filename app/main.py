"""FastAPI app factory."""
import dataclasses
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import group, host_api, member_api
from app import config
from app.config import Settings
from app.host_api import DEMO_CREATES_PER_IP_PER_HOUR
from app.mcp_client import McdClient, MockMcdClient
from app.security import HostAuthError, HostAuthThrottle, RateLimiter, install_log_redaction
from app.store import Store


def create_app(settings: Settings, client, now: Callable[[], datetime] | None = None) -> FastAPI:
    if settings.mode == "demo" and isinstance(client, McdClient):
        raise ValueError("demo mode must never be wired to the real McdClient")
    if settings.mode == "real" and not settings.host_key:
        raise ValueError("real mode requires a non-empty host key")
    install_log_redaction()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.client = client
    app.state.store = Store(settings.db_path)
    # A process that died inside create-order leaves ORDERING behind; nobody will ever finish
    # it, and the order may exist at McD. UNCERTAIN makes the host check recent orders first.
    swept = app.state.store.transition_all({group.ORDERING}, group.UNCERTAIN)
    if swept:
        logging.getLogger("app.order").warning("startup: %d ORDERING session(s) -> UNCERTAIN", swept)
    app.state.now = now or (lambda: datetime.now(settings.tz))
    # M7: enforce retention at startup too, not only when the next session is created
    purged = app.state.store.purge_older_than(settings.retention_s, app.state.now())
    if purged:
        logging.getLogger("app.order").info("startup: purged %d expired session(s)", purged)
    clock = lambda: app.state.now().timestamp()  # noqa: E731  (injected clock drives the limiters)
    app.state.host_throttle = HostAuthThrottle(10, 600, clock)
    app.state.demo_create_limiter = RateLimiter(DEMO_CREATES_PER_IP_PER_HOUR, 3600, clock)

    @app.exception_handler(HostAuthError)
    async def host_auth_error(request, exc: HostAuthError):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get("/")
    def root():
        # relative on purpose: `/` -> /host, and `/demo/` (prefix stripped by Caddy) -> /demo/host
        return RedirectResponse("host", status_code=302)

    @app.get("/api/meta")
    def meta():
        return {"mode": settings.mode}

    app.include_router(member_api.build_router(clock))
    app.include_router(host_api.build_router(settings))
    app.mount("/static", StaticFiles(directory=member_api.WEB_DIR), name="static")
    return app


def app_from_env() -> FastAPI:
    """uvicorn app.main:app_from_env --factory. Demo never touches secrets or boto3."""
    settings = config.load_settings()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    if settings.mode == "demo":
        return create_app(settings, MockMcdClient())
    token, host_key = config.load_secrets(settings)
    settings = dataclasses.replace(settings, host_key=host_key)
    return create_app(settings, McdClient(token))
