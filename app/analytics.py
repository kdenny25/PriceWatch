"""Market analytics: going price, deal thresholds, outlier handling, alert rules.

Pure functions only (no I/O) so they are easy to test.
"""
from __future__ import annotations

import math
import statistics

MIN_N_AUTO = 8          # need at least this many clean listings before auto-thresholds apply
SUSPICIOUS_RATIO = 0.35  # below this fraction of the median we flag "too good to be true"


def money(x) -> str:
    if x is None:
        return "-"
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def split_outliers(totals) -> tuple[list[float], list[float], tuple[float, float]]:
    """Separate 'real' market prices from junk (accessories, $1 bait, absurd asks).

    Returns (kept_sorted, removed_sorted, (low_fence, high_fence)).
    """
    v = sorted(t for t in totals if t is not None and t > 0)
    if len(v) < 4:
        return v, [], (0.0, float("inf"))
    m = percentile(v, 50)
    lo, hi = 0.2 * m, 5 * m
    v1 = [p for p in v if lo <= p <= hi]
    if len(v1) >= 8:
        q1, q3 = percentile(v1, 25), percentile(v1, 75)
        iqr = q3 - q1
        lo = max(lo, q1 - 1.5 * iqr)
        hi = min(hi, q3 + 1.5 * iqr)
    kept = [p for p in v if lo <= p <= hi]
    if len(kept) < 0.6 * len(v):          # fences too aggressive; fall back to the coarse pass
        lo, hi = 0.2 * m, 5 * m
        kept = v1
    removed = [p for p in v if not (lo <= p <= hi)]
    return kept, removed, (lo, hi)


def market(totals) -> dict | None:
    kept, removed, (lo, hi) = split_outliers(totals)
    if not kept:
        return None
    n = len(kept)
    return {
        "n": n,
        "n_raw": n + len(removed),
        "removed": len(removed),
        "min": kept[0],
        "p10": percentile(kept, 10),
        "p25": percentile(kept, 25),
        "median": percentile(kept, 50),
        "mean": sum(kept) / n,
        "p75": percentile(kept, 75),
        "p90": percentile(kept, 90),
        "max": kept[-1],
        "stdev": statistics.pstdev(kept) if n > 1 else 0.0,
        "confidence": "low" if n < MIN_N_AUTO else "medium" if n < 25 else "high",
        "low_fence": lo,            # below this a listing is excluded from the stats...
        "high_fence": hi,
        "bait_floor": 0.2 * percentile(kept, 50),   # ...and below THIS it is junk/bait, never alerted
        "clean": kept,
    }


def public(m: dict | None) -> dict | None:
    return None if m is None else {k: v for k, v in m.items() if k != "clean"}


def histogram(kept: list[float], bins: int = 12) -> list[dict]:
    if not kept:
        return []
    lo, hi = kept[0], kept[-1]
    if hi == lo:
        return [{"lo": lo, "hi": hi, "n": len(kept)}]
    w = (hi - lo) / bins
    counts = [0] * bins
    for p in kept:
        counts[min(int((p - lo) / w), bins - 1)] += 1
    return [{"lo": lo + i * w, "hi": lo + (i + 1) * w, "n": c} for i, c in enumerate(counts)]


def suggested(m: dict | None, target_percentile: float = 10) -> dict | None:
    if not m:
        return None
    return {
        "going_price": m["median"],
        "typical_low": m["p25"],
        "typical_high": m["p75"],
        "good_deal": m["p25"],
        "great_deal": m["p10"],
        "floor": m["min"],
        "target": percentile(m["clean"], target_percentile),
    }


def by_source(rows: list[dict], m: dict | None) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for r in rows:
        if m and not (m["low_fence"] <= r["total"] <= m["high_fence"]):
            continue
        groups.setdefault(r["source"], []).append(r["total"])
    out = []
    for src, vals in groups.items():
        vals.sort()
        out.append({"source": src, "n": len(vals), "median": percentile(vals, 50),
                    "p25": percentile(vals, 25), "min": vals[0]})
    return sorted(out, key=lambda d: -d["n"])


def flag_for(total: float, m: dict | None) -> str | None:
    if not m:
        return None
    if total < m["bait_floor"]:
        return "outlier_low"        # probably an accessory, parts listing or bait: never alerted
    if total > m["high_fence"]:
        return "outlier_high"
    if total < SUSPICIOUS_RATIO * m["median"]:
        return "suspicious"         # real maybe, but verify before buying
    if total < m["low_fence"]:
        return "below_range"        # well under the normal spread (left out of the stats) but plausible
    return None


def annotate(rows: list[dict], m: dict | None) -> list[dict]:
    """Sort cheapest-first and add deal_pct (vs the going price) and an outlier/suspicious flag."""
    med = m["median"] if m else None
    out = []
    for r in sorted(rows, key=lambda r: r["total"]):
        d = dict(r)
        d["deal_pct"] = round((med - r["total"]) / med * 100, 1) if med else None
        d["flag"] = flag_for(r["total"], m)
        out.append(d)
    return out


def build(rows: list[dict], target_percentile: float = 10) -> dict:
    """Everything the UI needs from a set of listing rows (each needs total, source)."""
    m = market([r["total"] for r in rows])
    return {
        "market": public(m),
        "suggested": suggested(m, target_percentile),
        "histogram": histogram(m["clean"]) if m else [],
        "by_source": by_source(rows, m),
        "listings": annotate(rows, m),
    }


def sold_estimate(gone_rows: list[dict]) -> dict | None:
    """Rough 'what things sell for': listings that disappeared, at their last seen price.

    This is an estimate: a listing can vanish because it sold *or* was removed.
    """
    totals = [r["total"] for r in gone_rows]
    kept, _, _ = split_outliers(totals)
    if len(kept) < 3:
        return None
    days = [(r["gone_at"] - r["first_seen"]) / 86400.0 for r in gone_rows if r.get("gone_at")]
    return {
        "n": len(kept),
        "median": percentile(kept, 50),
        "p25": percentile(kept, 25),
        "avg_days_listed": round(sum(days) / len(days), 1) if days else None,
    }


def alert_reason(total: float, mon: dict, m: dict | None) -> str | None:
    """Why (if at all) a listing deserves a notification. Rules are OR'd together."""
    if m and total < m["bait_floor"]:
        return None                         # junk / bait: never alert
    reasons = []
    med = m["median"] if m else None
    mp = mon.get("max_price")
    if mp is not None and total <= mp:
        reasons.append(f"at or under your {money(mp)} limit")
    if mon.get("auto_target") and m and m["n"] >= MIN_N_AUTO:
        pct = mon.get("target_percentile") or 10
        thr = percentile(m["clean"], pct)
        if total <= thr:
            reasons.append(f"in the cheapest {pct:g}% of the market")
    md = mon.get("min_discount_pct")
    if md and med and total <= med * (1 - md / 100.0):
        reasons.append(f"{(1 - total / med) * 100:.0f}% below the going price")
    return "; ".join(reasons) or None
