"""FastAPI server (dashboard JSON). Everything under /api is behind APP_TOKEN (hard rule 5/6).

Fail closed: if no APP_TOKEN is configured the API refuses everything. The token is only accepted in a
header, never in a URL. A wrong token costs the caller 0.8 s.
"""
from __future__ import annotations

import asyncio
import hmac
import time

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import config
from bus import Hub
from db import Database

VERSION = "0.1.0"
BAD_TOKEN_DELAY_S = 0.8


def _extract(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-app-token", "").strip()


def create_app(db: Database, hub: Hub, token: str, supervisor=None, bad_token_delay: float = BAD_TOKEN_DELAY_S) -> FastAPI:
    app = FastAPI(title="fluffcore3 // FOMO DESK", docs_url=None, redoc_url=None, openapi_url=None)
    started = time.time()
    expected = (token or "").encode()

    async def require_token(request: Request) -> None:
        if not expected:
            raise HTTPException(503, "APP_TOKEN is not set on the desk")
        given = _extract(request)
        if not given:
            raise HTTPException(401, "token required")
        if not hmac.compare_digest(given.encode(), expected):
            await asyncio.sleep(bad_token_delay)
            raise HTTPException(401, "bad token")

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @api.get("/ping")
    async def ping():
        return {"ok": True, "ts": time.time()}

    @api.get("/state")
    async def state():
        mode = await db.get_setting("mode", config.DEFAULT_MODE)
        return {
            "mode": mode,
            "version": VERSION,
            "uptime_s": round(time.time() - started, 1),
            "strategies": [],  # no strategy has run yet: "no data", never an invented balance
        }

    @api.get("/health")
    async def health():
        ages = hub.heartbeats.ages()
        return {
            "ok": True,
            "uptime_s": round(time.time() - started, 1),
            "heartbeat_age_s": {k: round(v, 1) for k, v in ages.items()},
            "loop_lag_ms": hub.lag.snapshot(),
            "stages": hub.metrics.snapshot(),
            "db": {
                "journal_mode": db.writer_pragmas.get("journal_mode"),
                "synchronous": db.writer_pragmas.get("synchronous"),
            },
            "restarts": list(getattr(supervisor, "restarts", []))[-20:],
            **{name: fn() for name, fn in hub.status.items()},
        }

    import api_dash

    api_dash.mount(api, db, hub, supervisor)
    app.include_router(api)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        # API answers are never stored. The page's own files (no data, no secrets) may be kept but are re-checked every time,
        # so a reopen costs a few tiny "not modified" answers instead of downloading everything again.
        resp.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # The dashboard itself (plain HTML/CSS/JS). It holds no data and no secrets: the token is typed into its login screen.
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(config.ROOT / "web"), html=True, check_dir=False), name="web")

    return app
