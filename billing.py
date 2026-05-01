"""Subscription cost over an arbitrary [from_ts, to_ts] window.

billing.json is a list of charge records. Each record has:
  receipt_id (str, unique), start (YYYY-MM-DD), end (YYYY-MM-DD),
  plan (str), amount_usd (float, excl tax), source ("manual" | "gmail").

subscription_cost() prorates each charge by the fraction of its day-span
that intersects the window. merge_scraped() lets the Gmail scraper
upsert charges by receipt_id without disturbing manual rows.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

BILLING_PATH = Path(__file__).parent / "billing.json"


def _to_date(s: str | datetime | date) -> date:
    if isinstance(s, datetime):
        return s.astimezone(timezone.utc).date() if s.tzinfo else s.date()
    if isinstance(s, date):
        return s
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date() if "T" in s else date.fromisoformat(s)


def load_charges() -> list[dict]:
    if not BILLING_PATH.exists():
        return []
    try:
        return json.loads(BILLING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_charges(charges: list[dict]) -> None:
    charges = sorted(charges, key=lambda c: (c.get("start") or "", c.get("receipt_id") or ""))
    BILLING_PATH.write_text(json.dumps(charges, indent=2) + "\n", encoding="utf-8")


def merge_scraped(scraped: Iterable[dict]) -> dict:
    """Upsert scraped charges by receipt_id. Returns counts."""
    existing = load_charges()
    by_id = {c.get("receipt_id"): c for c in existing if c.get("receipt_id")}
    added = updated = 0
    for s in scraped:
        rid = s.get("receipt_id")
        if not rid:
            continue
        s.setdefault("source", "gmail")
        if rid in by_id:
            cur = by_id[rid]
            # Don't clobber manual entries the user maintains by hand
            if cur.get("source") == "manual":
                continue
            cur.update(s)
            updated += 1
        else:
            by_id[rid] = s
            added += 1
    out = list(by_id.values())
    save_charges(out)
    return {"added": added, "updated": updated, "total": len(out)}


def subscription_cost(from_ts: str, to_ts: str) -> dict:
    """Return total subscription spend in [from_ts, to_ts]; prorated by intersection."""
    f = _to_date(from_ts)
    t = _to_date(to_ts)
    charges = load_charges()
    total = 0.0
    out = []
    coverage_first = coverage_last = None
    for c in charges:
        try:
            cs, ce = _to_date(c["start"]), _to_date(c["end"])
        except Exception:
            continue
        if coverage_first is None or cs < coverage_first:
            coverage_first = cs
        if coverage_last is None or ce > coverage_last:
            coverage_last = ce
        overlap_days = max(0, (min(t, ce) - max(f, cs)).days)
        span_days = max(1, (ce - cs).days)
        applied = c["amount_usd"] * (overlap_days / span_days)
        if applied > 0:
            total += applied
            out.append({
                "receipt_id": c.get("receipt_id"),
                "plan": c["plan"],
                "start": cs.isoformat(),
                "end": ce.isoformat(),
                "amount": c["amount_usd"],
                "applied": round(applied, 2),
                "source": c.get("source", "manual"),
            })
    return {
        "total": round(total, 2),
        "charges": out,
        "coverage": {
            "first": coverage_first.isoformat() if coverage_first else None,
            "last": coverage_last.isoformat() if coverage_last else None,
        },
    }


if __name__ == "__main__":
    import sys
    a, b = sys.argv[1], sys.argv[2]
    print(json.dumps(subscription_cost(a, b), indent=2))
