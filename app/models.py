"""Shared data types and text helpers (price parsing, query parsing, relevance)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class ScrapeError(Exception):
    """A source could not be read (bad config, layout change, network...)."""


class Blocked(ScrapeError):
    """The site refused us (403 / 429). The engine backs off when this happens."""


@dataclass
class Item:
    external_id: str
    title: str
    price: float
    url: str
    source: str                 # "ebay", "craigslist", "custom:<name>"
    scope: str = ""             # finer grain used to detect vanished listings
    shipping: float | None = None
    currency: str = "USD"
    image: str | None = None
    location: str | None = None
    condition: str | None = None

    @property
    def total(self) -> float:
        return self.price + (self.shipping or 0.0)


@dataclass
class ScrapeResult:
    items: list[Item] = field(default_factory=list)
    ok_scopes: set[str] = field(default_factory=set)   # scopes read successfully
    errors: list[str] = field(default_factory=list)


_PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


def parse_price(text) -> float | None:
    """'$1,299.99' -> 1299.99. Returns None when no number is present."""
    if text is None:
        return None
    m = _PRICE_RE.search(str(text).replace("\xa0", " "))
    return float(m.group(1).replace(",", "")) if m else None


def safe_url(u: str | None) -> str | None:
    """Only http(s) links are ever stored or shown (blocks javascript: URLs)."""
    if not u:
        return None
    u = u.strip()
    return u if re.match(r"^https?://", u, re.I) else None


_TOKEN_RE = re.compile(r'(-?)"([^"]+)"|(-?)(\S+)')


def norm(s: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def parse_query(q: str) -> tuple[list[str], list[str]]:
    """'rtx 3080 "founders edition" -laptop' -> (positive terms, negative terms)."""
    pos: list[str] = []
    neg: list[str] = []
    for m in _TOKEN_RE.finditer(q or ""):
        if m.group(2) is not None:
            flag, term = m.group(1), m.group(2)
        else:
            flag, term = m.group(3), m.group(4)
        term = term.strip().lower()
        if not norm(term):
            continue
        (neg if flag else pos).append(term)
    return pos, neg


def search_terms(q: str) -> str:
    """The positive part of a query, which is what we send to marketplaces."""
    pos, _ = parse_query(q)
    return " ".join(pos)


# Phrases that usually mean "not a working, complete item" and skew the market price.
JUNK_PHRASES = [
    "for parts", "parts only", "parts or repair", "not working", "broken",
    "cracked", "untested", "needs repair", "icloud locked", "read description",
    "salvage", "damaged", "faulty", "defective",
]


def _has_word_phrase(normalized_title: str, phrase: str) -> bool:
    p = norm(phrase)
    return bool(p) and f" {p} " in f" {normalized_title} "


def is_relevant(title: str, mon: dict) -> bool:
    """Local relevance filter so analytics aren't polluted by loosely-matched results."""
    t = norm(title)
    pos, neg = parse_query(mon.get("query", ""))
    if mon.get("require_all_terms", True):
        for term in pos:
            if norm(term) not in t:
                return False
    for term in neg + list(mon.get("exclude_words") or []):
        if _has_word_phrase(t, term):
            return False
    for term in mon.get("include_words") or []:
        n = norm(term)
        if n and n not in t:
            return False
    if mon.get("skip_junk"):
        for p in JUNK_PHRASES:
            if _has_word_phrase(t, p):
                return False
    return True
