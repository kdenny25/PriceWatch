"""The monitor engine: schedules runs, stores listings, tracks the market, sends alerts."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

from . import analytics, notify
from .analytics import money
from .models import ScrapeError, is_relevant, safe_url
from .scrapers import SCRAPERS

log = logging.getLogger("pricewatch.engine")
MAX_BACKOFF_S = 6 * 3600


class Ctx:
    def __init__(self, http, settings):
        self.http = http
        self.settings = settings


def source_label(source: str) -> str:
    if source.startswith("custom:"):
        return source[7:]
    return {"ebay": "eBay", "craigslist": "Craigslist"}.get(source, source)


class Engine:
    def __init__(self, db, settings, http, notifier=None, clock=time.time,
                 max_parallel: int = 3, tick: float = 15.0):
        self.db = db
        self.settings = settings
        self.http = http
        self.ctx = Ctx(http, settings)
        self.notifier = notifier or notify.send
        self.clock = clock
        self.sem = asyncio.Semaphore(max_parallel)
        self.tick = tick
        self.running: set[int] = set()
        self.tasks: set[asyncio.Task] = set()

    # ---- scheduling -----------------------------------------------------
    async def run_forever(self) -> None:
        last_prune = 0.0
        while True:
            try:
                now = self.clock()
                for r in self.db.q("SELECT id FROM monitors WHERE enabled=1 AND next_run_at<=? "
                                   "ORDER BY next_run_at", (now,)):
                    self.spawn(r["id"])
                if now - last_prune > 3600:
                    self.prune()
                    last_prune = now
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick)

    def spawn(self, mid: int) -> bool:
        if mid in self.running:
            return False
        self.running.add(mid)          # claim immediately so ticks never double-start
        t = asyncio.create_task(self._guarded(mid))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return True

    async def _guarded(self, mid: int) -> None:
        try:
            async with self.sem:
                await self.run_monitor(mid, _claimed=True)
        except Exception:  # noqa: BLE001
            log.exception("monitor %s crashed", mid)
        finally:
            self.running.discard(mid)

    def prune(self) -> None:
        now = self.clock()
        self.db.x("DELETE FROM runs WHERE ts < ?", (now - 14 * 86400,))
        self.db.x("DELETE FROM listings WHERE active=0 AND gone_at < ?", (now - 365 * 86400,))
        self.db.x("DELETE FROM snapshots WHERE ts < ?", (now - 730 * 86400,))

    # ---- collecting -----------------------------------------------------
    async def collect(self, mon: dict, mid: int | None = None):
        """Run every source of a monitor. Returns (items, ok_scopes, errors, attempted)."""
        items: dict[tuple[str, str], object] = {}
        scopes_ok: set[str] = set()
        errors: list[str] = []
        attempted = 0
        for cfg in mon["sources"]:
            scraper = SCRAPERS.get(cfg.get("type"))
            if scraper is None:
                errors.append(f"unknown source type {cfg.get('type')!r}")
                continue
            attempted += 1
            t0 = time.monotonic()
            try:
                res = await scraper.search(self.ctx, mon, cfg)
            except ScrapeError as e:
                errors.append(f"{scraper.label}: {e}")
                self._log_run(mid, cfg["type"], False, 0, str(e), t0)
                continue
            except Exception as e:  # noqa: BLE001 - never let one source kill the run
                log.exception("source %s crashed", cfg.get("type"))
                errors.append(f"{scraper.label}: unexpected error: {e}")
                self._log_run(mid, cfg["type"], False, 0, str(e), t0)
                continue
            kept = 0
            for it in res.items:
                if it.price is None or it.price < 0 or not safe_url(it.url):
                    continue
                if mon.get("min_price") and it.total < mon["min_price"]:
                    continue
                if not is_relevant(it.title, mon):
                    continue
                items[(it.source, it.external_id)] = it
                kept += 1
            scopes_ok |= res.ok_scopes
            errors.extend(res.errors)
            self._log_run(mid, cfg["type"], bool(res.ok_scopes), kept,
                          "; ".join(res.errors) or None, t0)
        return list(items.values()), scopes_ok, errors, attempted

    def _log_run(self, mid, source, ok, count, error, t0) -> None:
        if mid is None:
            return
        self.db.x("INSERT INTO runs(monitor_id,ts,source,ok,count,error,ms) VALUES(?,?,?,?,?,?,?)",
                  (mid, self.clock(), source, int(ok), count, error, int((time.monotonic() - t0) * 1000)))

    # ---- storing --------------------------------------------------------
    def _store(self, mon: dict, items, scopes_ok: set[str], now: float) -> int:
        mid = mon["id"]
        new = 0
        with self.db.tx():
            for it in items:
                row = self.db.one(
                    "SELECT id,total FROM listings WHERE monitor_id=? AND source=? AND external_id=?",
                    (mid, it.source, it.external_id))
                if row is None:
                    lid = self.db.x(
                        "INSERT INTO listings(monitor_id,source,scope,external_id,title,price,shipping,total,"
                        "currency,url,image,location,condition,first_seen,last_seen,active,first_total) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (mid, it.source, it.scope, it.external_id, it.title, it.price, it.shipping, it.total,
                         it.currency, it.url[:1000], it.image, it.location, it.condition, now, now, it.total))
                    self.db.x("INSERT INTO price_points(listing_id,ts,total) VALUES(?,?,?)", (lid, now, it.total))
                    new += 1
                    continue
                dropped = it.total < row["total"] * 0.995
                self.db.x(
                    "UPDATE listings SET title=?,price=?,shipping=?,total=?,url=?,image=?,location=?,"
                    "condition=?,scope=?,last_seen=?,active=1,gone_at=NULL,"
                    "notified=CASE WHEN ? THEN 0 ELSE notified END WHERE id=?",
                    (it.title, it.price, it.shipping, it.total, it.url[:1000], it.image, it.location,
                     it.condition, it.scope, now, int(dropped), row["id"]))
                if abs(it.total - row["total"]) > 0.005:
                    self.db.x("INSERT INTO price_points(listing_id,ts,total) VALUES(?,?,?)",
                              (row["id"], now, it.total))
            # Listings that stopped appearing in a scope we read successfully have gone away.
            for scope in {i.scope for i in items} & scopes_ok:
                self.db.x("UPDATE listings SET active=0, gone_at=? "
                          "WHERE monitor_id=? AND scope=? AND active=1 AND last_seen<?",
                          (now, mid, scope, now))
        return new

    def _snapshot(self, mid: int, mkt: dict | None, now: float) -> None:
        if not mkt:
            return
        self.db.x("INSERT INTO snapshots(monitor_id,ts,n,median,p10,p25,p75,min,mean) VALUES(?,?,?,?,?,?,?,?,?)",
                  (mid, now, mkt["n"], mkt["median"], mkt["p10"], mkt["p25"], mkt["p75"], mkt["min"], mkt["mean"]))

    # ---- alerting -------------------------------------------------------
    async def _alerts(self, mon: dict, mkt: dict | None, rows: list[dict], now: float) -> tuple[int, str | None]:
        cands = []
        for l in rows:
            if l["notified"]:
                continue
            reason = analytics.alert_reason(l["total"], mon, mkt)
            if reason:
                cands.append((l, reason))
        if not cands:
            return 0, None
        cands.sort(key=lambda t: t[0]["total"])
        cap = mon["max_alerts_per_run"]
        picks, extra = cands[:cap], max(0, len(cands) - cap)

        settings = self.settings.get()
        channels = notify.enabled_channels(settings)
        if mon.get("notify_channels") is not None:
            channels = [c for c in channels if c in mon["notify_channels"]]
        title, body, click = self.format_alert(mon, picks, extra, mkt, settings.get("app_base_url", ""))
        results = await self.notifier(settings, title, body, click, channels) if channels else {}

        if results and all(v != "ok" for v in results.values()):
            err = "; ".join(f"{k}: {v}" for k, v in results.items())
            log.warning("monitor %s: all notifications failed: %s", mon["id"], err)
            return 0, f"notification failed ({err})"      # leave notified=0 so the next run retries

        med = mkt["median"] if mkt else None
        with self.db.tx():
            for l, reason in picks:
                self.db.x("INSERT INTO alerts(monitor_id,ts,title,url,source,total,median,reason,channels) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (mon["id"], now, l["title"], l["url"], l["source"], l["total"], med, reason,
                           json.dumps(results)))
            ids = [l["id"] for l, _ in cands]
            self.db.x(f"UPDATE listings SET notified=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        return len(picks), None

    @staticmethod
    def format_alert(mon, picks, extra, mkt, base_url):
        n = len(picks) + extra
        best = min(l["total"] for l, _ in picks)
        title = f"{mon['name']}: {n} deal{'s' if n != 1 else ''} from {money(best)}"
        med = mkt["median"] if mkt else None
        blocks = []
        for l, reason in picks:
            bits = [reason]
            if med:
                bits.append(f"going price {money(med)}")
            if mkt and l["total"] < analytics.SUSPICIOUS_RATIO * med:
                bits.append("unusually low, verify before buying")
            blocks.append(f"{money(l['total'])} on {source_label(l['source'])}: {l['title'][:90]}\n"
                          f"{' | '.join(bits)}\n{l['url']}")
        if extra:
            blocks.append(f"...and {extra} more on the dashboard")
        if base_url:
            blocks.append(f"{base_url.rstrip('/')}/#/monitor/{mon['id']}")
        return title, "\n\n".join(blocks), picks[0][0]["url"]

    # ---- one full run ---------------------------------------------------
    async def run_monitor(self, mid: int, _claimed: bool = False) -> dict | None:
        if not _claimed:
            if mid in self.running:
                return {"skipped": "already running"}
            self.running.add(mid)
        try:
            mon = self.db.get_monitor(mid)
            if not mon:
                return None
            started = self.clock()
            items, scopes_ok, errors, attempted = await self.collect(mon, mid)
            all_failed = attempted > 0 and not scopes_ok
            new = 0
            alerts = 0
            notify_error = None
            if not all_failed:
                new = self._store(mon, items, scopes_ok, started)
                rows = self.db.q("SELECT * FROM listings WHERE monitor_id=? AND active=1", (mid,))
                mkt = analytics.market([r["total"] for r in rows])
                self._snapshot(mid, mkt, started)
                alerts, notify_error = await self._alerts(mon, mkt, rows, started)
            status = "error" if all_failed else ("partial" if errors else "ok")
            problems = errors + ([notify_error] if notify_error else [])
            fails = mon["fail_count"] + 1 if all_failed else 0
            interval = mon["interval_minutes"] * 60
            delay = min(interval * (2 ** min(fails, 4)) if all_failed else interval, MAX_BACKOFF_S)
            delay = max(delay, interval) * random.uniform(0.9, 1.1)
            self.db.x("UPDATE monitors SET last_run_at=?, last_status=?, last_error=?, fail_count=?, "
                      "next_run_at=? WHERE id=?",
                      (started, status, "; ".join(problems)[:1000] or None, fails, self.clock() + delay, mid))
            return {"status": status, "found": len(items), "new": new, "alerts": alerts, "errors": problems}
        finally:
            if not _claimed:
                self.running.discard(mid)

    # ---- ad-hoc price check (nothing is saved) ---------------------------
    async def analyze(self, mon: dict) -> dict:
        items, scopes_ok, errors, attempted = await self.collect(mon, None)
        rows = [{"source": i.source, "title": i.title, "price": i.price, "shipping": i.shipping,
                 "total": i.total, "url": i.url, "image": i.image, "location": i.location,
                 "condition": i.condition, "first_seen": None, "first_total": i.total} for i in items]
        out = analytics.build(rows, mon.get("target_percentile", 10))
        out["errors"] = errors
        return out
