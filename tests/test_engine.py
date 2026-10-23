import unittest

from app.http import Response
from app.main import create_app, parse_monitor
from app.scrapers import SCRAPERS
from tests.helpers import Clock, FakeNet, cl_html, ebay_item, jresp

EBAY_MON = {"name": "3080 hunt", "query": "rtx 3080", "sources": [{"type": "ebay"}],
            "interval_minutes": 30, "auto_target": True, "target_percentile": 10}


class EngineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        SCRAPERS["ebay"]._token = None
        self.net, self.clock, self.sent = FakeNet(), Clock(), []
        self.fail_notify = False

        async def notifier(settings, title, body, click, channels):
            self.sent.append((title, body, click, channels))
            return {c: ("boom" if self.fail_notify else "ok") for c in channels}

        self.app = create_app(":memory:", start_engine=False, transport=self.net,
                              notifier=notifier, clock=self.clock)
        st = self.app.state
        self.db, self.eng, self.settings = st.db, st.engine, st.settings
        self.settings.update({"ebay": {"app_id": "id", "cert_id": "cert"},
                              "notify": {"ntfy": {"enabled": True, "topic": "deals"}}})
        self.settings.get()["http"]["request_delay_s"] = 0     # no sleeping in tests
        self.net.on("identity/v1/oauth2/token", jresp({"access_token": "tok", "expires_in": 7200}))

    def ebay_listings(self, items):
        """items: {id: (price, title[, shipping])}"""
        out = [ebay_item(i, v[0], v[1], v[2] if len(v) > 2 else None) for i, v in items.items()]
        self.net.on("buy/browse/v1/item_summary/search", jresp({"itemSummaries": out}))

    def monitor(self, **over):
        return self.db.save_monitor(parse_monitor({**EBAY_MON, **over}))

    def base_items(self):
        items = {i: (500 + 5 * i, f"NVIDIA RTX 3080 10GB card #{i}") for i in range(18)}
        items[5] = (525, "NVIDIA RTX 3080 10GB card #5", 10.0)       # shipping counts toward total
        items[100] = (350, "RTX 3080 founders edition great price")
        items[101] = (20, "RTX 3080 backplate only")
        return items

    async def test_full_lifecycle_ebay(self):
        mid = self.monitor()
        items = self.base_items()
        self.ebay_listings(items)

        r1 = await self.eng.run_monitor(mid)
        self.assertEqual(r1["status"], "ok")
        self.assertEqual(r1["found"], 20)
        active = self.db.q("SELECT * FROM listings WHERE monitor_id=? AND active=1", (mid,))
        self.assertEqual(len(active), 20)
        by_ext = {r["external_id"]: r for r in active}
        self.assertEqual(by_ext["v1|1005|0"]["total"], 535.0)          # 525 + 10 shipping
        snap = self.db.one("SELECT * FROM snapshots WHERE monitor_id=?", (mid,))
        self.assertEqual(snap["n"], 18)    # $20 backplate (bait) and the $350 steal sit outside the stats range
        # one grouped notification; cheap deals included, the $20 bait never alerted
        self.assertEqual(len(self.sent), 1)
        title, body, click, channels = self.sent[0]
        self.assertIn("$350", body)
        self.assertNotIn("backplate", body)
        self.assertEqual(channels, ["ntfy"])
        self.assertTrue(click.startswith("https://www.ebay.com/itm/"))
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM alerts")["c"], 3)   # $350, $500, $505

        # ---- second run: a listing vanishes, one drops in price, one new bargain appears
        self.clock.advance(1800)
        del items[3]
        items[100] = (330, "RTX 3080 founders edition great price")
        items[102] = (300, "RTX 3080 FE open box")
        self.ebay_listings(items)
        r2 = await self.eng.run_monitor(mid)
        self.assertEqual(r2["new"], 1)
        gone = self.db.one("SELECT * FROM listings WHERE external_id='v1|1003|0'")
        self.assertEqual(gone["active"], 0)
        self.assertEqual(gone["gone_at"], self.clock.t)
        drop = self.db.one("SELECT * FROM listings WHERE external_id='v1|1100|0'")
        self.assertEqual(drop["total"], 330.0)
        self.assertEqual(drop["first_total"], 350.0)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM price_points WHERE listing_id=?", (drop["id"],))["c"], 2)
        self.assertEqual(len(self.sent), 2)
        body2 = self.sent[1][1]
        self.assertIn("$300", body2)
        self.assertIn("$330", body2)
        self.assertNotIn("$500 on", body2)                             # already-notified listing is not repeated
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM snapshots")["c"], 2)

        # ---- third run, nothing changed: no duplicate alerts
        self.clock.advance(1800)
        await self.eng.run_monitor(mid)
        self.assertEqual(len(self.sent), 2)

    async def test_notification_failure_retries(self):
        mid = self.monitor()
        self.ebay_listings(self.base_items())
        self.fail_notify = True
        r = await self.eng.run_monitor(mid)
        self.assertEqual(r["alerts"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM alerts")["c"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM listings WHERE notified=1")["c"], 0)
        self.assertIn("notification failed", self.db.get_monitor(mid)["last_error"])
        self.fail_notify = False
        self.clock.advance(1800)
        r = await self.eng.run_monitor(mid)
        self.assertEqual(r["alerts"], 3)

    async def test_max_price_rule_without_auto(self):
        mid = self.monitor(auto_target=False, max_price=510)
        self.ebay_listings(self.base_items())
        await self.eng.run_monitor(mid)
        alerts = self.db.q("SELECT total FROM alerts ORDER BY total")
        self.assertEqual([a["total"] for a in alerts], [350.0, 500.0, 505.0, 510.0])

    async def test_partial_and_total_failure(self):
        mid = self.monitor(query="bike", sources=[{"type": "ebay"}, {"type": "craigslist", "regions": ["tampa"]}])
        self.ebay_listings({i: (100 + i, f"Trek bike {i}") for i in range(10)})
        self.net.on("tampa.craigslist.org", Response(403, "", {}))
        r = await self.eng.run_monitor(mid)
        self.assertEqual(r["status"], "partial")
        self.assertIn("craigslist/tampa", r["errors"][0])
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM listings WHERE active=1")["c"], 10)

        # everything fails: keep old data, mark error, back off
        SCRAPERS["ebay"]._token = None
        self.net.on("identity/v1/oauth2/token", Response(401, "{}", {}))
        self.clock.advance(1800)
        r = await self.eng.run_monitor(mid)
        self.assertEqual(r["status"], "error")
        m = self.db.get_monitor(mid)
        self.assertEqual(m["fail_count"], 1)
        self.assertGreaterEqual(m["next_run_at"] - self.clock.t, 2 * 30 * 60 * 0.9 - 1)      # doubled backoff
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM listings WHERE active=1")["c"], 10)  # nothing "vanished"

    async def test_craigslist_regions_and_vanish_scope(self):
        mid = self.monitor(query="bike", sources=[{"type": "craigslist", "regions": ["tampa", "orlando"]}],
                           auto_target=False)
        tampa = [(str(7700000000 + i), f"Trek bike {i}", f"${100 + i}", "Tampa") for i in range(5)]
        orlando = [(str(7800000000 + i), f"Giant bike {i}", f"${200 + i}", "Orlando") for i in range(5)]
        self.net.on("tampa.craigslist.org", Response(200, cl_html(tampa, "tampa"), {}))
        self.net.on("orlando.craigslist.org", Response(200, cl_html(orlando, "orlando"), {}))
        r = await self.eng.run_monitor(mid)
        self.assertEqual(r["found"], 10)

        # tampa loses a listing, orlando errors out: only tampa's missing listing may be marked gone
        self.clock.advance(1800)
        self.net.on("tampa.craigslist.org", Response(200, cl_html(tampa[1:], "tampa"), {}))
        self.net.on("orlando.craigslist.org", Response(500, "oops", {}))
        r = await self.eng.run_monitor(mid)
        self.assertEqual(r["status"], "partial")
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM listings WHERE active=1")["c"], 9)
        gone = self.db.q("SELECT external_id FROM listings WHERE active=0")
        self.assertEqual([g["external_id"] for g in gone], ["7700000000"])
        # request shape: query + region subdomain + polite params
        call = next(c for c in self.net.calls if "tampa.craigslist.org" in c[1])
        self.assertEqual(call[2]["query"], "bike")

    async def test_layout_change_is_reported_not_silent(self):
        mid = self.monitor(query="bike", sources=[{"type": "craigslist", "regions": ["tampa"]}])
        self.net.on("tampa.craigslist.org", Response(200, "<html><body><div>new layout</div></body></html>", {}))
        r = await self.eng.run_monitor(mid)
        self.assertEqual(r["status"], "error")
        self.assertIn("could not read results", r["errors"][0])

    async def test_analyze_does_not_persist(self):
        self.ebay_listings(self.base_items())
        out = await self.eng.analyze(parse_monitor(EBAY_MON))
        self.assertEqual(out["market"]["n"], 18)
        self.assertEqual(out["listings"][0]["total"], 20.0)
        self.assertEqual(out["listings"][0]["flag"], "outlier_low")
        self.assertAlmostEqual(out["market"]["median"], 540.0, delta=15)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM listings")["c"], 0)

    async def test_ebay_request_shape(self):
        mid = self.monitor(query='rtx 3080 -laptop', min_price=100, sources=[{"type": "ebay", "condition": "used"}])
        self.ebay_listings({1: (400, "RTX 3080 laptop"), 2: (450, "RTX 3080 desktop")})
        await self.eng.run_monitor(mid)
        call = next(c for c in self.net.calls if "item_summary/search" in c[1])
        self.assertEqual(call[2]["q"], "rtx 3080")                       # negative terms handled locally
        self.assertIn("price:[100..],priceCurrency:USD", call[2]["filter"])
        self.assertIn("conditions:{USED}", call[2]["filter"])
        self.assertIn("buyingOptions:{FIXED_PRICE}", call[2]["filter"])
        self.assertEqual(call[4]["Authorization"], "Bearer tok")
        titles = [r["title"] for r in self.db.q("SELECT title FROM listings")]
        self.assertEqual(titles, ["RTX 3080 desktop"])                   # "laptop" filtered out locally

    async def test_spawn_does_not_double_start(self):
        mid = self.monitor()
        self.ebay_listings(self.base_items())
        self.assertTrue(self.eng.spawn(mid))
        self.assertFalse(self.eng.spawn(mid))
        for t in list(self.eng.tasks):
            await t
        self.assertEqual(self.db.get_monitor(mid)["last_status"], "ok")
        self.assertNotIn(mid, self.eng.running)


if __name__ == "__main__":
    unittest.main()
