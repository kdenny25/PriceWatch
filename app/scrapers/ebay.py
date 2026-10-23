"""eBay via the official Browse API (needs a free developer keyset; see README)."""
from __future__ import annotations

import base64
import time

from ..models import Item, Blocked, ScrapeError, ScrapeResult, safe_url, search_terms

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
CURRENCY = {"EBAY_US": "USD", "EBAY_GB": "GBP", "EBAY_CA": "CAD", "EBAY_AU": "AUD",
            "EBAY_DE": "EUR", "EBAY_FR": "EUR", "EBAY_IT": "EUR", "EBAY_ES": "EUR"}


class EbayScraper:
    type = "ebay"
    label = "eBay"

    def __init__(self):
        self._token: str | None = None
        self._exp = 0.0

    def validate(self, cfg: dict) -> dict:
        condition = cfg.get("condition", "any")
        buying = cfg.get("buying", "fixed")
        if condition not in ("any", "new", "used"):
            raise ValueError("eBay condition must be any, new or used")
        if buying not in ("fixed", "auction", "any"):
            raise ValueError("eBay listing type must be fixed, auction or any")
        pages = max(1, min(int(cfg.get("pages", 2)), 5))
        return {"type": "ebay", "condition": condition, "buying": buying, "pages": pages}

    async def _get_token(self, ctx, s: dict, force: bool = False) -> str:
        if self._token and not force and time.time() < self._exp - 60:
            return self._token
        basic = base64.b64encode(f"{s['app_id']}:{s['cert_id']}".encode()).decode()
        r = await ctx.http.request(
            "POST", TOKEN_URL, delay=0.2,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE})
        if r.status_code != 200:
            raise ScrapeError(f"eBay rejected the API keys (HTTP {r.status_code}). "
                              "Check App ID / Cert ID in Settings, and that they are Production keys.")
        j = r.json()
        self._token = j["access_token"]
        self._exp = time.time() + int(j.get("expires_in", 7200))
        return self._token

    async def search(self, ctx, mon: dict, cfg: dict) -> ScrapeResult:
        s = ctx.settings.get()["ebay"]
        if not (s["app_id"] and s["cert_id"]):
            raise ScrapeError("eBay API keys are not set (Settings > eBay)")
        marketplace = s.get("marketplace") or "EBAY_US"
        cur = CURRENCY.get(marketplace, "USD")
        q = search_terms(mon["query"])
        if not q:
            raise ScrapeError("search query has no positive terms")

        filters = []
        if mon.get("min_price"):
            filters.append(f"price:[{mon['min_price']:g}..],priceCurrency:{cur}")
        if cfg["condition"] != "any":
            filters.append("conditions:{%s}" % cfg["condition"].upper())
        filters.append({"fixed": "buyingOptions:{FIXED_PRICE}",
                        "auction": "buyingOptions:{AUCTION}",
                        "any": "buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER}"}[cfg["buying"]])

        items: dict[str, Item] = {}
        limit = 100
        for page in range(cfg["pages"]):
            params = {"q": q, "limit": limit, "offset": page * limit, "filter": ",".join(filters)}
            token = await self._get_token(ctx, s)
            r = await ctx.http.request(
                "GET", SEARCH_URL, params=params, delay=0.3,
                headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace})
            if r.status_code == 401:
                token = await self._get_token(ctx, s, force=True)
                r = await ctx.http.request(
                    "GET", SEARCH_URL, params=params, delay=0.3,
                    headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace})
            if r.status_code == 429:
                raise Blocked("eBay API rate limit reached")
            if r.status_code != 200:
                raise ScrapeError(f"eBay search failed (HTTP {r.status_code}): {r.text[:200]}")
            batch = r.json().get("itemSummaries") or []
            for it in batch:
                parsed = self._parse(it, cur)
                if parsed:
                    items[parsed.external_id] = parsed
            if len(batch) < limit:
                break
        return ScrapeResult(items=list(items.values()), ok_scopes={"ebay"})

    @staticmethod
    def _parse(it: dict, default_currency: str) -> Item | None:
        try:
            price = float(it["price"]["value"])
        except (KeyError, TypeError, ValueError):
            return None
        url = safe_url(it.get("itemWebUrl"))
        if not url or not it.get("itemId"):
            return None
        shipping = None
        for opt in it.get("shippingOptions") or []:
            try:
                shipping = float(opt["shippingCost"]["value"])
                break
            except (KeyError, TypeError, ValueError):
                continue
        loc = it.get("itemLocation") or {}
        location = ", ".join(x for x in (loc.get("city"), loc.get("stateOrProvince") or loc.get("country")) if x) or None
        return Item(
            external_id=str(it["itemId"]), title=str(it.get("title", ""))[:300], price=price,
            shipping=shipping, currency=it["price"].get("currency", default_currency), url=url,
            image=safe_url((it.get("image") or {}).get("imageUrl")), location=location,
            condition=it.get("condition"), source="ebay", scope="ebay")
