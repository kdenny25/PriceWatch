"""Test doubles: a fake network transport and a controllable clock (no real I/O anywhere)."""
import json

from app.http import Response


class FakeNet:
    """Route by substring of the URL. A route is a Response, a callable(url, params, data) or an Exception."""

    def __init__(self):
        self.routes = []      # (substring, handler)
        self.calls = []       # (method, url, params, data, headers)

    def on(self, substr, handler):
        self.routes = [r for r in self.routes if r[0] != substr] + [(substr, handler)]

    def __call__(self, method, url, headers, params, data, json_body, timeout):
        self.calls.append((method, url, params, data, headers))
        for substr, h in self.routes:
            if substr in url:
                if isinstance(h, Exception):
                    raise h
                return h(url, params, data) if callable(h) else h
        return Response(404, "no route in FakeNet for " + url, {})


def jresp(obj, status=200):
    return Response(status, json.dumps(obj), {"Content-Type": "application/json"})


def ebay_item(i, price, title, ship=None):
    d = {"itemId": f"v1|{1000 + i}|0", "title": title, "price": {"value": f"{price:.2f}", "currency": "USD"},
         "itemWebUrl": f"https://www.ebay.com/itm/{1000 + i}", "condition": "Used",
         "image": {"imageUrl": f"https://i.ebayimg.com/{i}.jpg"},
         "itemLocation": {"city": "Tampa", "stateOrProvince": "FL"}}
    if ship is not None:
        d["shippingOptions"] = [{"shippingCost": {"value": f"{ship:.2f}", "currency": "USD"}}]
    return d


def cl_html(rows, region="tampa"):
    """rows: [(id, title, price_text, location)] in the structure of Craigslist's static results page."""
    lis = "".join(
        f'<li class="cl-static-search-result" title="{t}"><a href="https://{region}.craigslist.org/hil/d/x/{i}.html">'
        f'<div class="title">{t}</div><div class="details"><div class="price">{p}</div>'
        f'<div class="location">{loc}</div></div></a></li>' for i, t, p, loc in rows)
    return f'<html><body><ol class="cl-static-search-results">{lis}</ol></body></html>'


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
