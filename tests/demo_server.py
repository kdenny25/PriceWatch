"""Demo mode: runs the real app against SYNTHETIC marketplace data (no network, no keys).

    python -m tests.demo_server [port]

Useful for exploring the UI. Nothing here talks to eBay or Craigslist.
"""
import json
import random
import sys
import tempfile

import uvicorn

from app.http import Response
from app.main import create_app
from tests.helpers import FakeNet, cl_html, ebay_item, jresp


def build(port=8765):
    rnd = random.Random(7)
    catalogue = {}
    for i in range(70):
        base = rnd.gauss(545, 38)
        if i % 17 == 3:
            base *= 0.72                       # a few genuine bargains
        if i == 65:
            base = 24                          # bait / accessory
        if i == 66:
            base = 1900                        # absurd ask
        catalogue[i] = (round(max(base, 10) / 5) * 5, rnd.choice([
            "NVIDIA GeForce RTX 3080 10GB Founders Edition", "ASUS TUF RTX 3080 OC 10GB", "EVGA RTX 3080 FTW3 Ultra",
            "MSI Gaming X Trio RTX 3080", "Gigabyte RTX 3080 Eagle OC", "Zotac RTX 3080 Trinity"]) + f" #{i}")
    catalogue[65] = (24, "RTX 3080 backplate only")
    catalogue[66] = (1900, "RTX 3080 10GB sealed collector")
    catalogue[67] = (180, "RTX 3080 for parts not working")
    state = {"calls": 0}

    def ebay(url, params, data):
        state["calls"] += 1
        rr = random.Random(state["calls"])
        present = [i for i in catalogue if rr.random() < 0.88]
        items = []
        for i in present:
            p, t = catalogue[i]
            if rr.random() < 0.04:
                p = round(p * 0.93)            # occasional price drop
                catalogue[i] = (p, t)
            items.append(ebay_item(i, p, t, ship=rr.choice([None, 0.0, 12.0, 18.5])))
        return jresp({"itemSummaries": items})

    def craigslist(url, params, data):
        rr = random.Random(state["calls"] * 31)
        rows = []
        for i in range(14):
            price = round(rr.gauss(505, 55) / 5) * 5
            title = rr.choice(["RTX 3080 10GB", "Gaming PC w/ RTX 3080", "RTX 3070 8GB", "rtx 3080 ti", "3080 FE"])
            rows.append((str(7700000000 + i), title, f"${price}", rr.choice(["Tampa", "Brandon", "Clearwater"])))
        return Response(200, cl_html(rows, "tampa"), {})

    net = FakeNet()
    net.on("identity/v1/oauth2/token", jresp({"access_token": "demo", "expires_in": 7200}))
    net.on("buy/browse/v1/item_summary/search", ebay)
    net.on("craigslist.org", craigslist)

    sent = []

    async def notifier(settings, title, body, click, channels):
        sent.append((title, body))
        print("[demo] notification:", title)
        return {c: "ok" for c in channels}

    app = create_app(tempfile.mktemp(suffix=".db"), start_engine=True, transport=net, notifier=notifier, tick=2.0)
    app.state.settings.update({"ebay": {"app_id": "demo", "cert_id": "demo"},
                               "notify": {"ntfy": {"enabled": True, "topic": "demo-topic"}}})
    app.state.settings.update({"http": {"request_delay_s": 0.5}})
    return app


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    uvicorn.run(build(port), host="127.0.0.1", port=port, log_level="warning")
