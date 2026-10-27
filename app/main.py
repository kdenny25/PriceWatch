"""Web API + UI server (Starlette)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, analytics, notify
from .db import DB
from .engine import Engine
from .http import Http
from .scrapers import SCRAPERS
from .settings import Settings

log = logging.getLogger("pricewatch")
STATIC = Path(__file__).parent / "static"


class MonitorIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field("", max_length=120)
    query: str = Field(..., min_length=1, max_length=200)
    sources: list[dict] = Field(..., min_length=1, max_length=12)
    min_price: float | None = Field(None, ge=0)
    max_price: float | None = Field(None, ge=0)
    include_words: list[str] = []
    exclude_words: list[str] = []
    require_all_terms: bool = True
    skip_junk: bool = True
    auto_target: bool = True
    target_percentile: float = Field(10, ge=1, le=50)
    min_discount_pct: float | None = Field(None, gt=0, lt=100)
    interval_minutes: int = Field(30, ge=10, le=1440)
    enabled: bool = True
    notify_channels: list[str] | None = None
    max_alerts_per_run: int = Field(5, ge=1, le=20)

    @field_validator("include_words", "exclude_words", mode="before")
    @classmethod
    def _split(cls, v):
        if isinstance(v, str):
            v = v.split(",")
        return [x.strip() for x in (v or []) if isinstance(x, str) and x.strip()]


FIELD_NAMES = {
    "query": "Search text", "sources": "Where to look", "interval_minutes": "Check interval",
    "max_alerts_per_run": "Max deals per notification", "target_percentile": "Cheapest-% setting",
    "min_discount_pct": "Discount %", "min_price": "Minimum price", "max_price": "Maximum price", "name": "Name",
}


def friendly(e: dict) -> str:
    """Turn a pydantic error into a sentence a person can act on."""
    field = str(e["loc"][0]) if e["loc"] else "request"
    label = FIELD_NAMES.get(field, field)
    if field == "sources" and e["type"] == "too_short":
        return "Pick at least one place to look (eBay, Craigslist or a custom site)"
    if field == "query" and e["type"] == "string_too_short":
        return "Enter what you're looking for"
    return f"{label}: {e['msg']}"


def parse_monitor(body) -> dict:
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    d = MonitorIn.model_validate(body).model_dump()
    cleaned, seen = [], set()
    for s in d["sources"]:
        scraper = SCRAPERS.get(s.get("type"))
        if scraper is None:
            raise ValueError(f"unknown source type: {s.get('type')!r}")
        c = scraper.validate(s)
        key = (c["type"], c.get("name", ""))
        if key in seen:
            raise ValueError("duplicate source (custom sites need different names)")
        seen.add(key)
        cleaned.append(c)
    d["sources"] = cleaned
    for ch in d["notify_channels"] or []:
        if ch not in notify.CHANNELS:
            raise ValueError(f"unknown notification channel: {ch}")
    if not d["name"].strip():
        d["name"] = d["query"][:60]
    return d


class BasicAuth:
    """Optional password gate for the whole app (set PRICEWATCH_PASSWORD)."""

    def __init__(self, app, user: str, password: str):
        self.app, self.user, self.password = app, user, password

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.password or scope["path"] == "/api/health":
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        ok = False
        if auth[:6].lower() == "basic ":
            try:
                u, _, p = base64.b64decode(auth[6:]).decode().partition(":")
                ok = secrets.compare_digest(u.encode(), self.user.encode()) & \
                    secrets.compare_digest(p.encode(), self.password.encode())
            except Exception:  # noqa: BLE001
                ok = False
        if ok:
            return await self.app(scope, receive, send)
        resp = Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="PriceWatch"'})
        await resp(scope, receive, send)


def create_app(db_path: str | None = None, start_engine: bool = True, transport=None,
               notifier=None, clock=time.time, tick: float = 15.0) -> Starlette:
    db = DB(db_path or os.environ.get("PRICEWATCH_DB", "data/pricewatch.db"), clock=clock)
    settings = Settings(db)
    http = Http(settings, transport=transport)
    engine = Engine(db, settings, http, notifier=notifier, clock=clock, tick=tick)
    password = os.environ.get("PRICEWATCH_PASSWORD", "")
    user = os.environ.get("PRICEWATCH_USER", "admin")

    def err(status: int, msg: str) -> JSONResponse:
        return JSONResponse({"detail": msg}, status_code=status)

    def api(fn):
        async def wrapper(request):
            try:
                return await fn(request)
            except ValidationError as e:
                msg = "; ".join(friendly(x) for x in e.errors())
                log.warning("%s %s rejected (422): %s", request.method, request.url.path, msg)
                return err(422, msg)
            except LookupError:
                return err(404, "not found")
            except ValueError as e:
                log.warning("%s %s rejected (400): %s", request.method, request.url.path, e)
                return err(400, str(e))
        return wrapper

    def summary(m: dict) -> dict:
        now = clock()
        mid = m["id"]
        snap = db.one("SELECT * FROM snapshots WHERE monitor_id=? ORDER BY ts DESC LIMIT 1", (mid,))
        return {
            **m,
            "running": mid in engine.running,
            "snapshot": snap,
            "active": db.one("SELECT COUNT(*) c FROM listings WHERE monitor_id=? AND active=1", (mid,))["c"],
            "new_24h": db.one("SELECT COUNT(*) c FROM listings WHERE monitor_id=? AND active=1 AND first_seen>?",
                              (mid, now - 86400))["c"],
            "alerts_24h": db.one("SELECT COUNT(*) c FROM alerts WHERE monitor_id=? AND ts>?",
                                 (mid, now - 86400))["c"],
        }

    # ---- handlers -------------------------------------------------------
    async def health(request):
        return JSONResponse({"ok": True})

    @api
    async def status(request):
        s = settings.get()
        return JSONResponse({
            "version": __version__, "auth": bool(password),
            "ebay_configured": bool(s["ebay"]["app_id"] and s["ebay"]["cert_id"]),
            "channels": notify.enabled_channels(s),
            "monitors": db.one("SELECT COUNT(*) c FROM monitors")["c"],
        })

    @api
    async def monitors(request):
        if request.method == "POST":
            data = parse_monitor(await request.json())
            mid = db.save_monitor(data)
            return JSONResponse(summary(db.get_monitor(mid)), status_code=201)
        return JSONResponse([summary(m) for m in db.list_monitors()])

    @api
    async def monitor_one(request):
        mid = request.path_params["mid"]
        m = db.get_monitor(mid)
        if not m:
            raise LookupError
        if request.method == "DELETE":
            db.x("DELETE FROM monitors WHERE id=?", (mid,))
            return Response(status_code=204)
        if request.method == "PUT":
            db.save_monitor(parse_monitor(await request.json()), mid)
            return JSONResponse(summary(db.get_monitor(mid)))

        status_q = request.query_params.get("status", "active")
        if status_q not in ("active", "gone", "all"):
            raise ValueError("status must be active, gone or all")
        now = clock()
        active_rows = db.q("SELECT * FROM listings WHERE monitor_id=? AND active=1", (mid,))
        a = analytics.build(active_rows, m["target_percentile"])
        if status_q == "active":
            listings = a["listings"]
        else:
            where = "" if status_q == "all" else "AND active=0"
            rows = db.q(f"SELECT * FROM listings WHERE monitor_id=? {where} ORDER BY last_seen DESC LIMIT 1500", (mid,))
            mk = analytics.market([r["total"] for r in active_rows])
            listings = analytics.annotate(rows, mk)
        gone = db.q("SELECT total,first_seen,gone_at FROM listings WHERE monitor_id=? AND active=0 AND gone_at>?",
                    (mid, now - 60 * 86400))
        trend = db.q("SELECT ts,n,median,p25,min FROM snapshots WHERE monitor_id=? ORDER BY ts DESC LIMIT 400", (mid,))
        return JSONResponse({
            "monitor": summary(m),
            "market": a["market"], "suggested": a["suggested"], "histogram": a["histogram"],
            "by_source": a["by_source"], "listings": listings[:1500],
            "trend": list(reversed(trend)),
            "sold_estimate": analytics.sold_estimate(gone),
            "alerts": db.q("SELECT * FROM alerts WHERE monitor_id=? ORDER BY ts DESC LIMIT 20", (mid,)),
            "runs": db.q("SELECT ts,source,ok,count,error,ms FROM runs WHERE monitor_id=? ORDER BY ts DESC LIMIT 12", (mid,)),
        })

    @api
    async def monitor_run(request):
        mid = request.path_params["mid"]
        if not db.get_monitor(mid):
            raise LookupError
        return JSONResponse({"started": engine.spawn(mid)})

    @api
    async def monitor_toggle(request):
        mid = request.path_params["mid"]
        m = db.get_monitor(mid)
        if not m:
            raise LookupError
        db.x("UPDATE monitors SET enabled=?, next_run_at=? WHERE id=?",
             (0 if m["enabled"] else 1, clock(), mid))
        return JSONResponse(summary(db.get_monitor(mid)))

    @api
    async def analyze(request):
        data = parse_monitor(await request.json())
        data["id"] = None
        out = await engine.analyze(data)
        out["listings"] = out["listings"][:1500]
        return JSONResponse(out)

    @api
    async def alerts(request):
        limit = max(1, min(int(request.query_params.get("limit", 100)), 500))
        rows = db.q("SELECT a.*, m.name AS monitor_name FROM alerts a JOIN monitors m ON m.id=a.monitor_id "
                    "ORDER BY a.ts DESC LIMIT ?", (limit,))
        for r in rows:
            r["channels"] = json.loads(r["channels"] or "{}")
        return JSONResponse(rows)

    @api
    async def settings_route(request):
        if request.method == "PUT":
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            return JSONResponse(settings.update(body))
        return JSONResponse(settings.public())

    @api
    async def settings_test(request):
        ch = request.path_params["channel"]
        if ch not in notify.CHANNELS:
            raise LookupError
        res = await notify.send(settings.get(), "PriceWatch test",
                                "If you can read this, notifications from PriceWatch work.",
                                None, [ch], force=True)
        return JSONResponse({"results": res})

    async def index(request):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @asynccontextmanager
    async def lifespan(app):
        task = None
        if not password:
            log.warning("PRICEWATCH_PASSWORD is not set: anyone who can reach this port can use the app. "
                        "Set it, or keep the server bound to 127.0.0.1 / behind your own auth.")
        if start_engine:
            task = asyncio.create_task(engine.run_forever())
        try:
            yield
        finally:
            if task:
                task.cancel()
            for t in list(engine.tasks):
                t.cancel()

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/status", status),
        Route("/api/monitors", monitors, methods=["GET", "POST"]),
        Route("/api/monitors/{mid:int}", monitor_one, methods=["GET", "PUT", "DELETE"]),
        Route("/api/monitors/{mid:int}/run", monitor_run, methods=["POST"]),
        Route("/api/monitors/{mid:int}/toggle", monitor_toggle, methods=["POST"]),
        Route("/api/analyze", analyze, methods=["POST"]),
        Route("/api/alerts", alerts),
        Route("/api/settings", settings_route, methods=["GET", "PUT"]),
        Route("/api/settings/test/{channel}", settings_test, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan,
                    middleware=[Middleware(BasicAuth, user=user, password=password)])
    app.state.db, app.state.engine, app.state.settings = db, engine, settings
    return app
