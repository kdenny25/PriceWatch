"""Craigslist: reads the plain-HTML search results page for each region you list.

Craigslist has no API and its terms restrict automated access, so this is intentionally
gentle (one request per region per run, spaced out) and is meant for personal use only.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Blocked, Item, ScrapeError, ScrapeResult, parse_price, safe_url, search_terms

REGION_RE = re.compile(r"^[a-z0-9-]{2,40}$")
CATEGORY_RE = re.compile(r"^[a-z]{3}$")
ID_RE = re.compile(r"/(\d{6,})\.html")


def parse_results(html: str, region: str, base: str) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    for li in soup.select("li.cl-static-search-result"):
        a = li.find("a", href=True)
        if not a:
            continue
        url = safe_url(urljoin(base, a["href"]))
        if not url:
            continue
        title_el = li.select_one(".title")
        title = (title_el.get_text(" ", strip=True) if title_el else li.get("title") or a.get_text(" ", strip=True))
        price_el = li.select_one(".price")
        price = parse_price(price_el.get_text(" ", strip=True)) if price_el else None
        if price is None:
            continue                      # "free"/no-price posts can't be compared
        loc_el = li.select_one(".location")
        m = ID_RE.search(url)
        ext = m.group(1) if m else url
        items.append(Item(
            external_id=ext, title=title[:300], price=price, url=url, source="craigslist",
            scope=f"craigslist:{region}",
            location=(loc_el.get_text(" ", strip=True) if loc_el else region) or region))
    return items


class CraigslistScraper:
    type = "craigslist"
    label = "Craigslist"

    def validate(self, cfg: dict) -> dict:
        regions = cfg.get("regions", [])
        if isinstance(regions, str):
            regions = [r for r in re.split(r"[,\s]+", regions) if r]
        regions = [r.strip().lower() for r in regions]
        if not regions:
            raise ValueError("Craigslist needs at least one region (e.g. tampa, orlando)")
        if len(regions) > 12:
            raise ValueError("Craigslist: at most 12 regions per monitor")
        for r in regions:
            if not REGION_RE.match(r):
                raise ValueError(f"Craigslist region '{r}' looks wrong (use the subdomain, like 'tampa')")
        category = (cfg.get("category") or "sss").lower()
        if not CATEGORY_RE.match(category):
            raise ValueError("Craigslist category must be a 3-letter code such as sss, ela, cta")
        return {"type": "craigslist", "regions": regions, "category": category,
                "titles_only": bool(cfg.get("titles_only", False))}

    async def search(self, ctx, mon: dict, cfg: dict) -> ScrapeResult:
        q = search_terms(mon["query"])
        if not q:
            raise ScrapeError("search query has no positive terms")
        res = ScrapeResult()
        for region in cfg["regions"]:
            base = f"https://{region}.craigslist.org"
            params = {"query": q, "sort": "date"}
            if mon.get("min_price"):
                params["min_price"] = f"{mon['min_price']:g}"
            if cfg.get("titles_only"):
                params["srchType"] = "T"
            try:
                r = await ctx.http.request("GET", f"{base}/search/{cfg['category']}", params=params)
            except Blocked as e:
                res.errors.append(f"craigslist/{region}: {e}")
                break                      # blocked: stop hitting the site this run
            except ScrapeError as e:
                res.errors.append(f"craigslist/{region}: {e}")
                continue
            if r.status_code != 200:
                res.errors.append(f"craigslist/{region}: HTTP {r.status_code}")
                continue
            found = parse_results(r.text, region, base)
            if not found and not re.search(r"nothing found|no results|0 results", r.text, re.I):
                res.errors.append(f"craigslist/{region}: could not read results "
                                  "(page layout changed or request was blocked)")
                continue
            res.items.extend(found)
            res.ok_scopes.add(f"craigslist:{region}")
        return res
