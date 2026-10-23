"""Custom site scraper driven by CSS selectors you enter in the UI.

Works for any site whose search results are present in the page HTML. For sites that
build results with JavaScript, set render="browser" (needs `pip install playwright` and
`playwright install chromium`).
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import Item, ScrapeError, ScrapeResult, parse_price, safe_url, search_terms


def is_public_url(url: str) -> bool:
    """Block loopback/private/link-local targets so a custom source can't probe your LAN.
    Set PRICEWATCH_ALLOW_PRIVATE=1 to allow them (e.g. a self-hosted shop)."""
    if os.environ.get("PRICEWATCH_ALLOW_PRIVATE") == "1":
        return True
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True                       # unresolvable: fetching will simply fail
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def parse_page(html: str, cfg: dict, page_url: str) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    base = cfg.get("base_url") or page_url
    name = cfg["name"]
    items: list[Item] = []
    for el in soup.select(cfg["item_selector"]):
        title_el = el.select_one(cfg["title_selector"]) if cfg.get("title_selector") else el
        price_el = el.select_one(cfg["price_selector"]) if cfg.get("price_selector") else el
        link_el = el.select_one(cfg["link_selector"]) if cfg.get("link_selector") else el.find("a", href=True)
        if el.name == "a" and not cfg.get("link_selector"):
            link_el = el
        if not (title_el and price_el and link_el and link_el.get("href")):
            continue
        price = parse_price(price_el.get_text(" ", strip=True))
        url = safe_url(urljoin(base, link_el["href"]))
        if price is None or not url:
            continue
        img = None
        if cfg.get("image_selector"):
            im = el.select_one(cfg["image_selector"])
            if im is not None:
                img = safe_url(urljoin(base, im.get("src") or im.get("data-src") or ""))
        loc = None
        if cfg.get("location_selector"):
            le = el.select_one(cfg["location_selector"])
            loc = le.get_text(" ", strip=True) if le else None
        ext = hashlib.sha1(url.encode()).hexdigest()[:16]
        items.append(Item(
            external_id=ext, title=title_el.get_text(" ", strip=True)[:300], price=price, url=url,
            source=f"custom:{name}", scope=f"custom:{name}", image=img, location=loc))
    return items


async def fetch_rendered(url: str, user_agent: str) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise ScrapeError("Browser rendering needs: pip install playwright && playwright install chromium") from e
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(user_agent=user_agent)
                await page.goto(url, wait_until="networkidle", timeout=45000)
                return await page.content()
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001
        raise ScrapeError(f"browser rendering failed: {e}") from e


class GenericScraper:
    type = "generic"
    label = "Custom site"

    def validate(self, cfg: dict) -> dict:
        out = {"type": "generic"}
        for k in ("name", "url", "item_selector", "title_selector", "price_selector",
                  "link_selector", "image_selector", "location_selector", "base_url"):
            out[k] = (cfg.get(k) or "").strip()
        out["render"] = cfg.get("render", "http")
        if out["render"] not in ("http", "browser"):
            raise ValueError("render must be http or browser")
        if not re.match(r"^[\w .-]{1,40}$", out["name"]):
            raise ValueError("Custom site needs a short name (letters, numbers, spaces)")
        if "{query}" not in out["url"] or not safe_url(out["url"]):
            raise ValueError("Custom site URL must start with http(s):// and contain {query}")
        if not is_public_url(out["url"]):
            raise ValueError("Custom site URL points at a private/local address (blocked)")
        for k in ("item_selector", "title_selector", "price_selector"):
            if not out[k]:
                raise ValueError(f"Custom site needs a {k.replace('_', ' ')}")
        # selectors are only parsed by BeautifulSoup; fail fast on invalid ones
        probe = BeautifulSoup("<div></div>", "html.parser")
        for k in ("item_selector", "title_selector", "price_selector", "link_selector",
                  "image_selector", "location_selector"):
            if out[k]:
                try:
                    probe.select(out[k])
                except Exception as e:  # noqa: BLE001
                    raise ValueError(f"Invalid CSS selector in {k}: {e}") from e
        return out

    async def search(self, ctx, mon: dict, cfg: dict) -> ScrapeResult:
        q = search_terms(mon["query"])
        if not q:
            raise ScrapeError("search query has no positive terms")
        url = cfg["url"].replace("{query}", quote_plus(q))
        if not is_public_url(url):
            raise ScrapeError("URL points at a private/local address (blocked)")
        if cfg["render"] == "browser":
            html = await fetch_rendered(url, ctx.settings.get()["http"]["user_agent"])
        else:
            r = await ctx.http.request("GET", url)
            if r.status_code != 200:
                raise ScrapeError(f"{urlparse(url).hostname} answered HTTP {r.status_code}")
            html = r.text
        items = parse_page(html, cfg, url)
        if not items and BeautifulSoup(html, "html.parser").select_one(cfg["item_selector"]) is None:
            raise ScrapeError("item selector matched nothing: the selectors may be wrong, "
                              "or the page builds results with JavaScript (try render = browser)")
        scope = f"custom:{cfg['name']}"
        return ScrapeResult(items=items, ok_scopes={scope})
