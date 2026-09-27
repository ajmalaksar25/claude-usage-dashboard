"""Anthropic API pricing per 1M tokens (USD).

Two layers:
  * PRICING  - hard-coded seed, keyed by canonical model id. Verified against
               platform.claude.com/docs/en/about-claude/pricing on 2026-09-27.
               Retired models live here permanently; that's fine, they don't move.
  * live     - Anthropic publishes no pricing API, so active models are refreshed
               from LiteLLM's community price table (same numbers as the docs page,
               machine-readable). sync() fetches at most once a day, stores the
               result in pricing.json next to usage.db, and re-prices every row in
               the DB when a rate changes.

Tuple order: (input, output, cache_5m_write, cache_1h_write, cache_read).
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

Rate = tuple[float, float, float, float, float]

PRICING: dict[str, Rate] = {
    "claude-fable-5-1":  (10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-fable-5":    (10.00, 50.00, 12.50, 20.00, 1.00),
    "claude-mythos-5":   (10.00, 50.00, 12.50, 20.00, 1.00),
    "claude-opus-5-5":   (4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-opus-5":     (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-8":   (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-7":   (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-6":   (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-5":   (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-1":   (15.00, 75.00, 18.75, 30.00, 1.50),
    "claude-opus-4":     (15.00, 75.00, 18.75, 30.00, 1.50),
    "claude-sonnet-5":   (2.00, 10.00, 2.50, 4.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 3.75, 6.00, 0.30),
    "claude-sonnet-4-5": (3.00, 15.00, 3.75, 6.00, 0.30),
    "claude-sonnet-4":   (3.00, 15.00, 3.75, 6.00, 0.30),
    "claude-haiku-4-5":  (1.00, 5.00, 1.25, 2.00, 0.10),
    # legacy 3.x family -- retired, never changes
    "claude-3-7-sonnet": (3.00, 15.00, 3.75, 6.00, 0.30),
    "claude-3-5-sonnet": (3.00, 15.00, 3.75, 6.00, 0.30),
    "claude-3-5-haiku":  (0.80, 4.00, 1.00, 1.60, 0.08),
    "claude-3-opus":     (15.00, 75.00, 18.75, 30.00, 1.50),
    "claude-3-sonnet":   (3.00, 15.00, 3.75, 6.00, 0.30),
    "claude-3-haiku":    (0.25, 1.25, 0.30, 0.50, 0.03),
}

# Unknown version of a known family -> price it like the newest member so a
# brand-new release isn't silently billed at $0 until the next live refresh.
FAMILY_FALLBACK = {"fable": "claude-fable-5-1", "mythos": "claude-mythos-5-1", "opus": "claude-opus-5-5",
                   "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}

LIVE_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
MAX_AGE_S = 24 * 3600

_live: dict[str, Rate] = {}  # merged over PRICING at lookup time
_meta: dict = {}

_SUFFIX = re.compile(r"[-@]\d{8}$|-v\d+:\d+$")


def canonical(model: str) -> str:
    """'us.anthropic.claude-opus-4-5-20251101-v1:0' -> 'claude-opus-4-5'."""
    m = (model or "").lower().strip()
    m = m.split("anthropic.")[-1]
    m = m.replace("claude-3-5-sonnet-latest", "claude-3-5-sonnet")
    for _ in range(2):
        m = _SUFFIX.sub("", m)
    return m.removesuffix("-latest")


def rate_for(model: str) -> tuple[Rate, str] | None:
    m = canonical(model)
    if m in _live:
        return _live[m], m
    if m in PRICING:
        return PRICING[m], m
    if not m.startswith("claude"):
        return None
    for fam, target in FAMILY_FALLBACK.items():
        if fam in m:
            return rate_for(target)[0], target
    return None


def model_tier(model: str) -> str | None:  # kept for callers; now the canonical id
    r = rate_for(model)
    return r[1] if r else None


def cost_for_model(model: str, inp: int, out: int, c5w: int, c1w: int, cr: int) -> tuple[float, str | None]:
    r = rate_for(model)
    if r is None:
        return 0.0, None
    p, tier = r
    return (inp * p[0] + out * p[1] + c5w * p[2] + c1w * p[3] + cr * p[4]) / 1_000_000, tier


# ---------- live refresh ----------

def _fetch_live(url: str = LIVE_URL, timeout: float = 15.0) -> dict[str, Rate]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.load(resp)
    out: dict[str, Rate] = {}
    for key, v in data.items():
        if v.get("litellm_provider") != "anthropic" or not key.startswith("claude"):
            continue
        try:
            inp, o = v["input_cost_per_token"], v["output_cost_per_token"]
        except KeyError:
            continue
        c5 = v.get("cache_creation_input_token_cost", inp * 1.25)
        c1 = v.get("cache_creation_input_token_cost_above_1hr", inp * 2)
        cr = v.get("cache_read_input_token_cost", inp * 0.1)
        out[canonical(key)] = tuple(round(x * 1_000_000, 4) for x in (inp, o, c5, c1, cr))
    if len(out) < 5:  # a broken/partial payload must not wipe good prices
        raise ValueError(f"live table looks wrong: {len(out)} claude rows")
    return out


def cache_path_for(db_path: Path) -> Path:
    return Path(db_path).with_name("pricing.json")


def load(cache: Path) -> None:
    global _live, _meta
    try:
        d = json.loads(cache.read_text())
        _live = {k: tuple(v) for k, v in d["prices"].items()}
        _meta = {k: d.get(k) for k in ("fetched_at", "source")}
    except Exception:
        _live, _meta = {}, {}


def sync(db_path: Path, conn: sqlite3.Connection | None = None, force: bool = False,
         fetch=_fetch_live, log=print) -> dict:
    """Load cached prices; refresh from the live table if stale; reprice DB rows on change.

    Never raises: offline means "keep what we have" (cache, else the seed table).
    """
    cache = cache_path_for(db_path)
    load(cache)
    first_run = not cache.exists()
    age = float("inf")
    if _meta.get("fetched_at"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(_meta["fetched_at"])).total_seconds()
    changed: list[str] = []
    fetched = False
    if force or age > MAX_AGE_S:
        try:
            fresh = fetch()
            fetched = True
            before = {**PRICING, **_live}
            changed = sorted(k for k, v in fresh.items() if before.get(k) != v)
            _live.update(fresh)
            _meta.update(fetched_at=datetime.now(timezone.utc).isoformat(), source=LIVE_URL)
            cache.write_text(json.dumps({**_meta, "prices": _live}, indent=1, sort_keys=True))
            if changed:
                log(f"[pricing] rates changed for: {', '.join(changed)}")
        except Exception as e:
            log(f"[pricing] live refresh failed ({e}); using {'cached' if _live else 'built-in'} rates")
    if conn is not None and (changed or first_run or force):
        n = reprice(conn)
        log(f"[pricing] repriced {n} rows")
    return {"fetched": fetched, "changed": changed, "fetched_at": _meta.get("fetched_at")}


def reprice(conn: sqlite3.Connection) -> int:
    """Recompute cost_usd/tier for every row from its stored token counts. One UPDATE per model."""
    n = 0
    for (model,) in conn.execute("SELECT DISTINCT model FROM messages").fetchall():
        r = rate_for(model or "")
        if r is None:
            continue
        p, tier = r
        cur = conn.execute(
            "UPDATE messages SET tier=?, cost_usd=(COALESCE(input_tokens,0)*? + COALESCE(output_tokens,0)*? "
            "+ COALESCE(cache_5m_write,0)*? + COALESCE(cache_1h_write,0)*? + COALESCE(cache_read,0)*?)/1000000.0 "
            "WHERE model=?",
            (tier, *p, model),
        )
        n += cur.rowcount
    conn.commit()
    return n


def meta() -> dict:
    return {"fetched_at": _meta.get("fetched_at"), "source": _meta.get("source"), "live_models": len(_live)}
