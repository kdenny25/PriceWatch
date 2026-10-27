import asyncio
import unittest

from app import analytics, notify
from app.models import is_relevant, parse_price, parse_query, safe_url, search_terms
from app.scrapers.craigslist import CraigslistScraper, parse_results
from app.scrapers.ebay import EbayScraper
from app.scrapers.generic import GenericScraper, parse_page
from app.settings import MASK, Settings
from app.db import DB
from tests.helpers import cl_html


class TestModels(unittest.TestCase):
    def test_parse_price(self):
        self.assertEqual(parse_price("$1,299.99"), 1299.99)
        self.assertEqual(parse_price("USD 450"), 450.0)
        self.assertEqual(parse_price("$12 to $20"), 12.0)
        self.assertIsNone(parse_price("free"))
        self.assertIsNone(parse_price(None))

    def test_safe_url(self):
        self.assertEqual(safe_url("https://a.com/x"), "https://a.com/x")
        self.assertIsNone(safe_url("javascript:alert(1)"))
        self.assertIsNone(safe_url(""))

    def test_parse_query(self):
        pos, neg = parse_query('rtx 3080 "founders edition" -laptop')
        self.assertEqual(pos, ["rtx", "3080", "founders edition"])
        self.assertEqual(neg, ["laptop"])
        self.assertEqual(search_terms('rtx -laptop "x y"'), "rtx x y")
        self.assertEqual(parse_query("- foo")[0], ["foo"])

    def test_relevance(self):
        mon = {"query": "rtx 3080 -laptop", "require_all_terms": True, "skip_junk": True,
               "exclude_words": ["case"], "include_words": []}
        self.assertTrue(is_relevant("NVIDIA RTX-3080 Founders Edition", mon))
        self.assertTrue(is_relevant("RTX 3080Ti 12GB", mon))           # substring: 3080 in 3080ti
        self.assertFalse(is_relevant("RTX 3070", mon))                 # missing term
        self.assertFalse(is_relevant("RTX 3080 laptop", mon))          # negative term
        self.assertFalse(is_relevant("RTX 3080 for parts", mon))       # junk phrase
        self.assertFalse(is_relevant("RTX 3080 box and case", mon))    # exclude word
        self.assertTrue(is_relevant("RTX 3080 showcase edition", mon))  # excludes match whole words only
        mon2 = dict(mon, include_words=["12gb"])
        self.assertFalse(is_relevant("RTX 3080 10GB", mon2))


class TestAnalytics(unittest.TestCase):
    def test_percentile(self):
        v = [1, 2, 3, 4, 5]
        self.assertEqual(analytics.percentile(v, 50), 3)
        self.assertEqual(analytics.percentile(v, 0), 1)
        self.assertEqual(analytics.percentile(v, 100), 5)
        self.assertAlmostEqual(analytics.percentile(v, 25), 2.0)
        self.assertIsNone(analytics.percentile([], 50))

    def test_outliers_removed(self):
        prices = [500 + 5 * i for i in range(18)] + [20, 4000]
        kept, removed, (lo, hi) = analytics.split_outliers(prices)
        self.assertNotIn(20, kept)
        self.assertNotIn(4000, kept)
        self.assertEqual(sorted(removed), [20, 4000])
        m = analytics.market(prices)
        self.assertEqual(m["n"], 18)
        self.assertEqual(m["removed"], 2)
        self.assertAlmostEqual(m["median"], 542.5)
        self.assertEqual(m["confidence"], "medium")

    def test_small_sets_untouched(self):
        m = analytics.market([100, 200, 300])
        self.assertEqual(m["n"], 3)
        self.assertEqual(m["confidence"], "low")
        self.assertIsNone(analytics.market([]))

    def test_histogram_counts_all(self):
        kept = [10, 11, 12, 20, 21, 30]
        h = analytics.histogram(kept, bins=4)
        self.assertEqual(sum(b["n"] for b in h), len(kept))
        self.assertEqual(len(analytics.histogram([5, 5, 5])), 1)

    def test_alert_reason_rules(self):
        prices = [500 + 5 * i for i in range(20)]
        m = analytics.market(prices)
        mon = {"max_price": None, "auto_target": True, "target_percentile": 10, "min_discount_pct": None}
        self.assertIsNotNone(analytics.alert_reason(500, mon, m))
        self.assertIsNone(analytics.alert_reason(560, mon, m))
        mon2 = dict(mon, auto_target=False, max_price=510)
        self.assertIn("limit", analytics.alert_reason(505, mon2, m))
        self.assertIsNone(analytics.alert_reason(520, mon2, m))
        mon3 = dict(mon, auto_target=False, min_discount_pct=10)
        self.assertIn("below", analytics.alert_reason(450, mon3, m))
        self.assertIsNone(analytics.alert_reason(520, mon3, m))
        # bait far under the market is never an alert
        self.assertIsNone(analytics.alert_reason(30, dict(mon, max_price=1000), m))
        # not enough data: auto target stays quiet
        self.assertIsNone(analytics.alert_reason(1, mon, analytics.market([100, 110, 120])))

    def test_flags(self):
        prices = [500 + 5 * i for i in range(20)] + [20, 190]
        out = analytics.build([{"total": p, "source": "ebay"} for p in prices])
        flags = {l["total"]: l["flag"] for l in out["listings"]}
        self.assertEqual(flags[20], "outlier_low")
        self.assertEqual(flags[190], "suspicious")
        self.assertEqual(flags[500], None)
        self.assertEqual(out["listings"][0]["total"], 20)
        near = analytics.build([{"total": p, "source": "ebay"} for p in [500 + 5 * i for i in range(20)] + [400]])
        self.assertEqual({l["total"]: l["flag"] for l in near["listings"]}[400], "below_range")
        self.assertIsNotNone(out["suggested"]["great_deal"])

    def test_sold_estimate(self):
        gone = [{"total": 400 + i, "first_seen": 0, "gone_at": 86400 * 3} for i in range(5)]
        s = analytics.sold_estimate(gone)
        self.assertEqual(s["n"], 5)
        self.assertEqual(s["avg_days_listed"], 3.0)
        self.assertIsNone(analytics.sold_estimate(gone[:2]))


class TestScrapers(unittest.TestCase):
    def test_craigslist_parse(self):
        html = cl_html([("7712345678", "RTX 3080 FE", "$420", "Tampa"),
                        ("7712345679", "Free stuff", "", "Tampa"),
                        ("7712345680", "Card 3080 Ti", "$1,050", "Brandon")])
        items = parse_results(html, "tampa", "https://tampa.craigslist.org")
        self.assertEqual([i.price for i in items], [420.0, 1050.0])
        self.assertEqual(items[0].external_id, "7712345678")
        self.assertEqual(items[0].scope, "craigslist:tampa")
        self.assertEqual(items[1].location, "Brandon")

    def test_craigslist_validate(self):
        s = CraigslistScraper()
        self.assertEqual(s.validate({"regions": "Tampa, orlando"})["regions"], ["tampa", "orlando"])
        with self.assertRaises(ValueError):
            s.validate({"regions": []})
        with self.assertRaises(ValueError):
            s.validate({"regions": ["evil.com/x"]})
        with self.assertRaises(ValueError):
            s.validate({"regions": ["tampa"], "category": "../x"})

    def test_ebay_validate_and_parse(self):
        s = EbayScraper()
        self.assertEqual(s.validate({})["buying"], "fixed")
        with self.assertRaises(ValueError):
            s.validate({"condition": "refurb"})
        it = EbayScraper._parse({"itemId": "v1|1|0", "title": "x", "price": {"value": "10.50", "currency": "USD"},
                                 "itemWebUrl": "https://www.ebay.com/itm/1",
                                 "shippingOptions": [{"shippingCost": {"value": "4.50"}}]}, "USD")
        self.assertEqual(it.total, 15.0)
        self.assertIsNone(EbayScraper._parse({"itemId": "1", "price": {"value": "1"}, "itemWebUrl": "javascript:x"}, "USD"))
        self.assertIsNone(EbayScraper._parse({"itemId": "1"}, "USD"))

    def test_generic(self):
        cfg = {"name": "Shop", "url": "https://shop.example.com/s?q={query}", "item_selector": "div.item",
               "title_selector": "h2", "price_selector": ".p", "link_selector": "a", "image_selector": "img",
               "location_selector": "", "base_url": "", "render": "http"}
        html = ('<div class="item"><h2>Camera A</h2><span class="p">$1,200.00</span><a href="/p/1">x</a>'
                '<img src="/i/1.jpg"></div>'
                '<div class="item"><h2>Camera B</h2><span class="p">call us</span><a href="/p/2">x</a></div>'
                '<div class="item"><h2>Camera C</h2><span class="p">$80</span><a href="javascript:void(0)">x</a></div>')
        items = parse_page(html, cfg, "https://shop.example.com/s?q=camera")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, 1200.0)
        self.assertEqual(items[0].url, "https://shop.example.com/p/1")
        self.assertEqual(items[0].image, "https://shop.example.com/i/1.jpg")
        self.assertEqual(items[0].source, "custom:Shop")

    def test_generic_validate(self):
        g = GenericScraper()
        ok = {"name": "Shop", "url": "https://shop.example.com/s?q={query}", "item_selector": "div",
              "title_selector": "h2", "price_selector": ".p"}
        self.assertEqual(g.validate(ok)["render"], "http")
        for bad in (dict(ok, url="https://shop.example.com/s"),           # no {query}
                    dict(ok, url="ftp://x/{query}"),
                    dict(ok, url="http://127.0.0.1:8080/s?q={query}"),     # SSRF guard
                    dict(ok, url="http://169.254.169.254/{query}"),
                    dict(ok, item_selector=""),
                    dict(ok, name="bad<name>"),
                    dict(ok, price_selector="div[")):
            with self.assertRaises(ValueError, msg=str(bad)):
                g.validate(bad)


class TestSettingsAndNotify(unittest.TestCase):
    def test_secret_masking_roundtrip(self):
        s = Settings(DB(":memory:"))
        s.update({"ebay": {"app_id": "abc", "cert_id": "supersecret"},
                  "notify": {"discord": {"enabled": True, "webhook_url": "https://discord.com/api/webhooks/1/xyz"}}})
        pub = s.public()
        self.assertEqual(pub["ebay"]["app_id"], "abc")
        self.assertEqual(pub["ebay"]["cert_id"], MASK)
        self.assertEqual(pub["notify"]["discord"]["webhook_url"], MASK)
        # saving the masked form back must not overwrite the real secret
        s.update(pub)
        self.assertEqual(s.get()["ebay"]["cert_id"], "supersecret")
        # unknown keys ignored, bad values rejected
        s.update({"bogus": 1, "ebay": {"nope": 1}})
        self.assertNotIn("bogus", s.get())
        with self.assertRaises(ValueError):
            s.update({"http": {"request_delay_s": 0.1}})

    def test_senders(self):
        sent = []

        def post(url, **kw):
            sent.append((url, kw))

        st = Settings(DB(":memory:"))
        st.update({"notify": {"ntfy": {"enabled": True, "topic": "deals"},
                              "discord": {"enabled": True, "webhook_url": "https://discord.com/api/webhooks/1/x"},
                              "telegram": {"enabled": True, "bot_token": "123:ABCDEF", "chat_id": "9"},
                              "slack": {"enabled": True}}})
        res = asyncio.run(notify.send(st.get(), "T", "B", "https://x.y/z", post=post))
        self.assertEqual(res["ntfy"], "ok")
        self.assertEqual(res["discord"], "ok")
        self.assertEqual(res["telegram"], "ok")
        self.assertIn("not set", res["slack"])
        by_url = {u: kw for u, kw in sent}
        self.assertEqual(by_url["https://ntfy.sh"]["json"]["topic"], "deals")
        self.assertEqual(by_url["https://ntfy.sh"]["json"]["click"], "https://x.y/z")
        discord = by_url["https://discord.com/api/webhooks/1/x"]["json"]
        self.assertEqual(discord["allowed_mentions"], {"parse": []})   # listing titles can't @everyone

    def test_error_text_hides_secrets(self):
        def post(url, **kw):
            raise RuntimeError(f"failed calling {url}")

        st = Settings(DB(":memory:"))
        st.update({"notify": {"telegram": {"enabled": True, "bot_token": "123:ABCDEF", "chat_id": "9"}}})
        res = asyncio.run(notify.send(st.get(), "T", "B", post=post))
        self.assertNotIn("123:ABCDEF", res["telegram"])
        self.assertIn("***", res["telegram"])


if __name__ == "__main__":
    unittest.main()


class TestRequestErrors(unittest.TestCase):
    """Rejected requests must say what to fix (the UI shows these messages verbatim)."""

    def msg(self, body):
        from pydantic import ValidationError
        from app.main import friendly, parse_monitor
        try:
            parse_monitor(body)
        except ValidationError as e:
            return "; ".join(friendly(x) for x in e.errors())
        except ValueError as e:
            return str(e)
        return None

    def test_messages(self):
        self.assertIn("at least one place to look", self.msg({"query": "x", "sources": []}))
        self.assertIn("what you're looking for", self.msg({"query": "", "sources": [{"type": "ebay"}]}))
        self.assertIn("at least one region", self.msg({"query": "x", "sources": [{"type": "craigslist", "regions": ""}]}))
        self.assertIn("Check interval", self.msg({"query": "x", "sources": [{"type": "ebay"}], "interval_minutes": 1}))
        self.assertIsNone(self.msg({"query": "x", "sources": [{"type": "ebay"}]}))
