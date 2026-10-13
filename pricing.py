"""Anthropic API pricing per 1M tokens (USD), plus pricing modifiers (fast mode, data residency, web search).

Three layers, merged per model and per field (anthropic_doc > litellm > seed):
  * anthropic_doc - Anthropic's own pricing page as markdown (authoritative, updated on launch
                    day). Parsed generically: a new model needs no code change.
  * litellm       - LiteLLM's community price table (secondary; also exposes the fast multiplier
                    as provider_specific_entry.fast).
  * PRICING/SEED  - hard-coded offline fallback. Verified against platform.claude.com on 2026-10-07.
sync() fetches at most once a day, stores the merge (with the source per model) in pricing.json
next to usage.db, and re-prices every row in the DB when a rate, multiplier or model set changes.
A model unknown to every layer is priced like the newest member of its family and flagged
estimated; it is repriced automatically once a live source lists it.

Tuple order: (input, output, cache_5m_write, cache_1h_write, cache_read).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("pricing")

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
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 4.00, 0.20),
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

# Non-Claude models (gateway accounts: CLIProxyAPI, OpenRouter, LiteLLM proxy...), priced at their
# own provider's list price. Live rows come from LiteLLM across every provider; this seed is the
# offline fallback, copied from LiteLLM's first-party "openai" rows on 2026-10-07.
# OpenAI bills no cache writes, so the write columns equal the input rate when LiteLLM omits them.
EXT_SEED: dict[str, Rate] = {
    "gpt-5.6-sol":   (4.00, 20.00, 5.00, 5.00, 0.40),
    "gpt-5.6":       (4.00, 20.00, 5.00, 5.00, 0.40),
    "gpt-5.6-terra": (2.00, 12.00, 2.50, 2.50, 0.20),
    "gpt-5.6-luna":  (0.20, 1.20, 0.25, 0.25, 0.02),
    "gpt-5.5":       (5.00, 30.00, 5.00, 5.00, 0.50),
    "gpt-5.4-mini":  (0.75, 4.50, 0.75, 0.75, 0.075),
}

# Fast mode: multiplier on ALL five token categories (caching stacks on top of fast pricing).
# Seed only; live sources override. Opus 4.7 errors on fast, so it has no premium.
SEED_FAST: dict[str, float] = {"claude-opus-5-5": 2.0, "claude-opus-5": 2.0, "claude-opus-4-8": 2.0}
# Date-windowed seed: (mult before cutoff, cutoff ts (UTC ISO prefix), mult from cutoff on).
# Opus 4.6 fast research preview: $30/$150 = 6.0x standard, 50% intro discount (3.0x) until
# 2026-02-16 (the-decoder / Medium / OpenRouter claude-opus-4.6-fast listing). Today 4.6 runs
# at standard and reports speed "standard", so only historical rows hit this.
SEED_FAST_TIMED: dict[str, tuple[float, str, float]] = {"claude-opus-4-6": (3.0, "2026-02-16T00:00:00", 6.0)}
# Unmodelled: >200K-token long-context surcharges on fast mode.

# Data residency: inference_geo == "us" -> multiplier on all token categories, for models at or
# above a minimum version. Parsed from the pricing page; these are the offline fallbacks.
GEO_MULT, GEO_MIN = 1.1, (4, 6)
WEB_SEARCH_USD = 0.01  # $10 / 1,000 searches, on top of tokens; web fetch is free

FAMILIES = ("fable", "mythos", "opus", "sonnet", "haiku")

LIVE_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
DOC_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
MAX_AGE_S = 24 * 3600

_live: dict[str, Rate] = {}   # merged live prices (doc over litellm), over PRICING at lookup
_src: dict[str, str] = {}     # model -> "anthropic_doc" | "litellm"
_fast: dict[str, float] = {}  # live fast multipliers
_fast_src: dict[str, str] = {}
_geo: dict = {}               # {"mult": 1.1, "min": [4, 6]} from the doc
_meta: dict = {}
_ext: dict[str, Rate] = {}    # live non-Claude prices from LiteLLM (all providers), over EXT_SEED
ANOMALIES: dict[str, int] = {}  # "<model>:fast-without-premium" -> count, for the UI/tests

# Internal models: a vendor's own helper models with no public API price, covered by the plan that
# runs them. They are not "unpriced" (nothing is missing; there is no list price to miss) and stay
# at $0 unless config.json maps them to an API-equivalent model under "model_aliases".
INTERNAL_MODELS: dict[str, str] = {
    "codex-auto-review": "Codex CLI's internal approval/review model; no public API price, covered by the ChatGPT plan",
}
_alias_override: dict[str, str] | None = None  # tests: set_model_aliases({...}); None = read config.json

_SUFFIX = re.compile(r"[-@]\d{8}$|-v\d+:\d+$")


def canonical(model: str) -> str:
    """'us.anthropic.claude-opus-4-5-20251101-v1:0' -> 'claude-opus-4-5'."""
    m = (model or "").lower().strip()
    m = re.sub(r"\[[^\]]*\]$", "", m)  # context-window suffix, e.g. claude-opus-5[1m] (no surcharge on 4.6+)
    m = m.split("anthropic.")[-1]
    m = m.replace("claude-3-5-sonnet-latest", "claude-3-5-sonnet")
    for _ in range(2):
        m = _SUFFIX.sub("", m)
    return m.removesuffix("-latest")


_EXT_DATE = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def ext_key(model: str) -> str:
    """Normalise a non-Claude model id: 'openai/gpt-5.6-sol(high)' -> 'gpt-5.6-sol'.

    Drops a provider prefix ('openai/', 'openrouter/openai/'), a trailing '(effort)' or '[ctx]'
    tag, and a release-date suffix ('-2026-03-17'). The undated name is what LiteLLM keys on.
    """
    m = (model or "").lower().strip()
    m = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]$", "", m)
    m = m.rsplit("/", 1)[-1]
    return _EXT_DATE.sub("", m)


def model_key(model: str) -> str:
    """One id per model across transcripts and cost-state records (Claude canonical, else ext_key)."""
    c = canonical(model)
    return c if c.startswith("claude") else ext_key(model)


def _ext_table() -> dict[str, Rate]:
    return {**EXT_SEED, **_ext}


def _resolve_ext(model: str) -> tuple[Rate, str] | None:
    t = _ext_table()
    raw = (model or "").lower().strip().rsplit("/", 1)[-1]
    for k in (raw, ext_key(model)):
        if k in t:
            return t[k], k
    return None


def version_of(model: str) -> tuple[int, ...]:
    """'claude-opus-5-5' -> (5, 5); 'claude-3-5-haiku' -> (3, 5); no number -> ()."""
    return tuple(int(t) for t in canonical(model).split("-") if t.isdigit())


def family_of(model: str) -> str | None:
    m = canonical(model)
    return next((f for f in FAMILIES if f in m), None)


def _table() -> dict[str, Rate]:
    return {**PRICING, **_live}


def fallback_target(family: str) -> str | None:
    """Newest member (by id version) of a family in the merged table."""
    ids = [k for k in _table() if family in k]
    return max(ids, key=version_of) if ids else None


def set_model_aliases(aliases: dict[str, str] | None) -> None:
    """Pin the alias map (tests); None goes back to config.json."""
    global _alias_override
    _alias_override = None if aliases is None else {str(k).lower(): str(v) for k, v in aliases.items()}


def model_aliases() -> dict[str, str]:
    """config.json "model_aliases": {model: priced-as model}, e.g. {"codex-auto-review": "gpt-5.6-luna"}.

    Opt-in: the default is no alias. Lower-cased keys; non-string entries are ignored."""
    if _alias_override is not None:
        return _alias_override
    try:
        import billing  # lazy: billing owns config.json (and does not import pricing)
        raw = billing.load_config().get("model_aliases") or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k.lower(): v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v}


def alias_for(model: str) -> str | None:
    al = model_aliases()
    if not al:
        return None
    raw = (model or "").lower().strip()
    for k in (raw, raw.rsplit("/", 1)[-1], model_key(model)):
        if k in al:
            return al[k]
    return None


def is_internal(model: str) -> bool:
    """A known internal model left unpriced (no alias configured for it)."""
    raw = (model or "").lower().strip().rsplit("/", 1)[-1]
    return (raw in INTERNAL_MODELS or ext_key(model) in INTERNAL_MODELS) and alias_for(model) is None


def resolve(model: str) -> tuple[Rate, str, bool] | None:
    """(rate, tier, estimated). Estimated = priced via the family fallback, not its own row, or via a
    configured model alias (an API-equivalent the user opted into, not the model's own price)."""
    target = alias_for(model)
    if target and target.lower() != (model or "").lower():
        r = _resolve_own(target)
        return (r[0], r[1], True) if r else None
    return _resolve_own(model)


def _resolve_own(model: str) -> tuple[Rate, str, bool] | None:
    m = canonical(model)
    t = _table()
    if m in t:
        return t[m], m, False
    if not m.startswith("claude"):
        e = _resolve_ext(model)  # gateway models (gpt-*, gemini-*, ...) at their provider's list price
        return (e[0], e[1], False) if e else None
    fam = family_of(m)
    target = fallback_target(fam) if fam else None
    return (t[target], target, True) if target else None


def rate_for(model: str) -> tuple[Rate, str] | None:
    r = resolve(model)
    return (r[0], r[1]) if r else None


def model_tier(model: str) -> str | None:  # kept for callers; now the canonical id
    r = rate_for(model)
    return r[1] if r else None


def price_source(model: str) -> str:
    if alias_for(model):
        return "alias"
    if is_internal(model):
        return "internal"
    m = canonical(model)
    if not m.startswith("claude"):
        e = _resolve_ext(model)
        return "unpriced" if e is None else ("litellm" if e[1] in _ext else "seed")
    return _src.get(m, "litellm") if m in _live else ("seed" if m in PRICING else "estimated")


# ---------- modifiers ----------

def fast_spec(model: str) -> tuple[float, str, float]:
    """(multiplier before cutoff, cutoff ts, multiplier from cutoff on); cutoff '' = no window."""
    m = canonical(model)
    fast = {**SEED_FAST, **_fast}
    if m in _fast or m in SEED_FAST:
        return fast[m], "", fast[m]
    if m in SEED_FAST_TIMED:
        b, cut, a = SEED_FAST_TIMED[m]
        return b, cut, a
    fam = family_of(m)
    if m not in _table():  # unknown model: borrow from its fallback target (never silent 1x)
        t = fallback_target(fam) if fam else None
        return fast_spec(t) if t else (1.0, "", 1.0)
    # known model with no fast row: a model newer than every fast-capable family member
    # (e.g. a just-launched Opus the page lists without a fast row yet) inherits the newest one
    cand = [(version_of(k), v) for k, v in fast.items() if fam and fam in k and v > 1.0]
    if cand:
        v, mult = max(cand)
        if version_of(m) > v:
            return mult, "", mult
    return 1.0, "", 1.0


def fast_multiplier(model: str, ts: str | None = None) -> float:
    b, cut, a = fast_spec(model)
    return b if (cut and ts and ts < cut) else a


def geo_multiplier(model: str, inference_geo: str | None) -> float:
    """1.1x for inference_geo 'us' on models at/above the minimum version; else 1.0."""
    if (inference_geo or "").lower() != "us" or not canonical(model).startswith("claude"):
        return 1.0
    mult = _geo.get("mult", GEO_MULT)
    lo = tuple(_geo.get("min", GEO_MIN))
    v = version_of(model) or (version_of(resolve(model)[1]) if resolve(model) else ())
    return mult if v and v >= lo else 1.0


class Cost(NamedTuple):
    cost: float          # total incl. fast premium and web search fees
    tier: str | None
    fast_premium: float  # part of cost that is the fast-mode surcharge
    server_tool: float   # web search fees
    estimated: bool      # priced via family fallback


def _anomaly(model: str) -> None:
    k = f"{canonical(model)}:fast-without-premium"
    if k not in ANOMALIES:
        log.warning("speed=fast on %s but no fast multiplier is known; pricing at 1.0x", model)
    ANOMALIES[k] = ANOMALIES.get(k, 0) + 1


def cost_for(model: str, inp: int, out: int, c5w: int, c1w: int, cr: int, speed: str = "standard",
             inference_geo: str | None = None, web_search_requests: int = 0, ts: str | None = None) -> Cost:
    r = resolve(model)
    if r is None:
        return Cost(0.0, None, 0.0, 0.0, False)
    p, tier, est = r
    base = (inp * p[0] + out * p[1] + c5w * p[2] + c1w * p[3] + cr * p[4]) / 1_000_000
    geo = geo_multiplier(model, inference_geo)
    fm = fast_multiplier(model, ts) if speed == "fast" else 1.0
    if speed == "fast" and fm == 1.0:
        _anomaly(model)
    tool = (web_search_requests or 0) * WEB_SEARCH_USD
    return Cost(base * geo * fm + tool, tier, base * geo * (fm - 1.0), tool, est)


def cost_for_model(model: str, inp: int, out: int, c5w: int, c1w: int, cr: int) -> tuple[float, str | None]:
    c = cost_for(model, inp, out, c5w, c1w, cr)
    return c.cost, c.tier


# ---------- anthropic doc (primary live source) ----------

_MONEY = re.compile(r"\$\s*([\d,]*\.?\d+)")
_PAREN = re.compile(r"\s*\((?:[^()]|\([^()]*\))*\)")
_SUP = re.compile(r"<sup>.*?</sup>", re.S)
_LEGACY = re.compile(r"^claude-(haiku|sonnet|opus)-(3(?:-\d+)?)$")


def _model_id(cell: str) -> str:
    """'Claude Opus 5.5 ([limited availability](...))' -> 'claude-opus-5-5'."""
    s = _PAREN.sub("", _SUP.sub("", cell)).strip().lower()
    s = re.sub(r"[\s.]+", "-", s)
    m = _LEGACY.match(s)  # page says 'Claude Haiku 3.5'; transcripts say claude-3-5-haiku
    return f"claude-{m.group(2)}-{m.group(1)}" if m else s


def _money(cell: str) -> float | None:
    m = _MONEY.search(_SUP.sub("", cell))
    return float(m.group(1).replace(",", "")) if m else None


def _section(md: str, heading: str) -> str:
    m = re.search(rf"^(#+)\s+{re.escape(heading)}\s*$", md, re.M | re.I)
    if not m:
        return ""
    nxt = re.search(rf"^#{{1,{len(m.group(1))}}}\s", md[m.end():], re.M)
    return md[m.end(): m.end() + nxt.start()] if nxt else md[m.end():]


def _rows(text: str) -> tuple[list[str], list[list[str]]]:
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith("|")]
    cells = [[c.strip() for c in l.strip("|").split("|")] for l in lines]
    cells = [r for r in cells if not all(re.fullmatch(r":?-+:?", c) for c in r if c)]
    if not cells:
        return [], []
    return [c.lower() for c in cells[0]], cells[1:]


def parse_doc(md: str) -> dict:
    """Parse the pricing page markdown -> {"prices", "fast", "geo"}. Raises ValueError if implausible."""
    hdr, rows = _rows(_section(md, "Model pricing"))
    col = {}
    for i, h in enumerate(hdr):
        for key, pat in (("inp", "base input"), ("c5", "5m"), ("c1", "1h"), ("cr", "cache hit"), ("out", "output")):
            if pat in h:
                col.setdefault(key, i)
    if len(col) < 5:
        raise ValueError("model pricing table not found")
    prices: dict[str, Rate] = {}
    for r in rows:
        if len(r) <= max(col.values()) or "claude" not in r[0].lower():
            continue
        v = [_money(r[col[k]]) for k in ("inp", "out", "c5", "c1", "cr")]
        if any(x is None for x in v):
            continue
        if any(x <= 0 for x in v):
            raise ValueError(f"non-positive rate for {r[0]}")
        prices[_model_id(r[0])] = tuple(v)
    fams = {f for k in prices for f in ("opus", "sonnet", "haiku") if f in k}
    if len(prices) < 8 or len(fams) < 3:
        raise ValueError(f"doc parse looks wrong: {len(prices)} models, families {sorted(fams)}")

    fast: dict[str, float] = {}
    fhdr, frows = _rows(_section(md, "Fast mode pricing"))
    if "input" in "".join(fhdr) and len(fhdr) >= 3:
        for r in frows:
            fi, fo = _money(r[1]), _money(r[2])
            for cell in re.split(r"\s*(?:/|,|\band\b)\s*", r[0]):
                mid = _model_id(cell)
                if mid in prices and fi and fo:
                    bi, bo = prices[mid][0], prices[mid][1]
                    if abs(fi / bi - fo / bo) < 0.01:  # input and output ratios must agree
                        fast[mid] = round(fi / bi, 4)
    geo = {"mult": GEO_MULT, "min": list(GEO_MIN)}
    g = re.search(r"Claude\s+(\d+)\.(\d+)\s+and\s+later.{0,200}?(\d+(?:\.\d+)?)\s*[x×]\s+multiplier",
                  _section(md, "Data residency pricing"), re.S | re.I)
    if g:
        geo = {"mult": float(g.group(3)), "min": [int(g.group(1)), int(g.group(2))]}
    return {"prices": prices, "fast": fast, "geo": geo}


def _fetch_doc(url: str = DOC_URL, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "claude-usage-dashboard"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_doc(resp.read().decode("utf-8"))


# ---------- litellm (secondary live source) ----------

def parse_litellm(data: dict) -> tuple[dict[str, Rate], dict[str, float]]:
    out: dict[str, Rate] = {}
    fast: dict[str, float] = {}
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
        f = (v.get("provider_specific_entry") or {}).get("fast")
        if isinstance(f, (int, float)) and f > 0:
            fast[canonical(key)] = float(f)
    if len(out) < 5:  # a broken/partial payload must not wipe good prices
        raise ValueError(f"live table looks wrong: {len(out)} claude rows")
    return out, fast


_EXT_MODES = {"chat", "responses", "completion", None}


def parse_litellm_ext(data: dict) -> dict[str, Rate]:
    """Every non-Claude chat model LiteLLM prices, keyed by ext_key().

    When several providers list the same model (openai, azure, openrouter, bedrock...), the
    first-party row wins: a key with no provider prefix, then the shortest key. Missing cache
    columns default to the input rate (no cache discount or write surcharge is assumed).
    """
    best: dict[str, tuple[tuple[int, int], Rate]] = {}
    for key, v in data.items():
        if not isinstance(v, dict) or key.startswith("claude") or "claude" in key.rsplit("/", 1)[-1]:
            continue
        if v.get("mode") not in _EXT_MODES:
            continue
        inp, o = v.get("input_cost_per_token"), v.get("output_cost_per_token")
        if not isinstance(inp, (int, float)) or not isinstance(o, (int, float)) or inp <= 0:
            continue
        cw = v.get("cache_creation_input_token_cost")
        cr = v.get("cache_read_input_token_cost")
        cw = cw if isinstance(cw, (int, float)) else inp
        cr = cr if isinstance(cr, (int, float)) else inp
        rate = tuple(round(x * 1_000_000, 6) for x in (inp, o, cw, cw, cr))
        rank = (0 if "/" not in key else 1, len(key))
        for name in {key.lower().rsplit("/", 1)[-1], ext_key(key)}:
            if name not in best or rank < best[name][0]:
                best[name] = (rank, rate)
    return {k: r for k, (_, r) in best.items()}


def _fetch_live(url: str = LIVE_URL, timeout: float = 15.0):
    """(claude rates, fast multipliers, non-Claude rates)."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.load(resp)
    out, fast = parse_litellm(data)
    return out, fast, parse_litellm_ext(data)


# ---------- cache + sync ----------

def cache_path_for(db_path: Path) -> Path:
    return Path(db_path).with_name("pricing.json")


def load(cache: Path) -> None:
    global _live, _src, _fast, _fast_src, _geo, _meta, _ext
    try:
        d = json.loads(cache.read_text())
        _ext = {k: tuple(v) for k, v in (d.get("ext_prices") or {}).items()}
        _live = {k: tuple(v) for k, v in d["prices"].items()}
        _src = {k: d.get("sources", {}).get(k, "litellm") for k in _live}
        _fast = {k: float(v) for k, v in d.get("fast", {}).items()}
        _fast_src = {k: d.get("fast_sources", {}).get(k, "litellm") for k in _fast}
        _geo = d.get("geo") or {}
        _meta = {k: d.get(k) for k in ("fetched_at", "source")}
    except Exception:
        _live, _src, _fast, _fast_src, _geo, _meta, _ext = {}, {}, {}, {}, {}, {}, {}


def _merge(vals: dict, srcs: dict, lite: dict | None, doc: dict | None) -> None:
    """Per-key merge, anthropic_doc > litellm. A failed source leaves its cached entries alone."""
    for k, v in (lite or {}).items():
        if doc is not None and k in doc:
            continue
        if doc is None and srcs.get(k) == "anthropic_doc":
            continue  # a stale doc value still beats a fresh litellm one
        vals[k], srcs[k] = v, "litellm"
    for k, v in (doc or {}).items():
        vals[k], srcs[k] = v, "anthropic_doc"


def sync(db_path: Path, conn: sqlite3.Connection | None = None, force: bool = False,
         fetch=_fetch_live, fetch_doc=_fetch_doc, log=print) -> dict:
    """Load cached prices; refresh from the live sources if stale; reprice DB rows on change.

    Never raises: offline or a broken page means "keep what we have" (cache, else the seed).
    """
    cache = cache_path_for(db_path)
    load(cache)
    first_run = not cache.exists()
    age = float("inf")
    if _meta.get("fetched_at"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(_meta["fetched_at"])).total_seconds()
    changed: list[str] = []
    fetched = geo_changed = False
    if force or age > MAX_AGE_S:
        doc = lite = None
        try:
            doc = fetch_doc()
        except Exception as e:
            log(f"[pricing] anthropic doc rejected ({e}); keeping cached values")
        try:
            lite = fetch()
        except Exception as e:
            log(f"[pricing] litellm refresh failed ({e})")
        if doc is not None or lite is not None:
            fetched = True
            before = {**PRICING, **_live}
            before_fast = {**SEED_FAST, **_fast}
            before_geo = {"mult": GEO_MULT, "min": list(GEO_MIN), **_geo}
            before_ext = _ext_table()
            if lite is not None and len(lite) > 2 and lite[2]:
                _ext.clear()
                _ext.update(lite[2])
            _merge(_live, _src, (lite or ({}, {}))[0], doc["prices"] if doc else None)
            if doc is not None:  # doc is authoritative: drop doc-sourced fast rows it no longer lists
                for k in [k for k, s in _fast_src.items() if s == "anthropic_doc" and k not in doc["fast"]]:
                    del _fast[k], _fast_src[k]
                _geo.update(doc["geo"])
            _merge(_fast, _fast_src, (lite or ({}, {}))[1], doc["fast"] if doc else None)
            after_fast = {**SEED_FAST, **_fast}
            changed = sorted({k for k, v in {**PRICING, **_live}.items() if before.get(k) != v}
                             | {k for k in before_fast.keys() | after_fast.keys()
                                if before_fast.get(k) != after_fast.get(k)}
                             | {k for k in before_ext.keys() | _ext_table().keys()
                                if before_ext.get(k) != _ext_table().get(k)})
            geo_changed = before_geo != {"mult": GEO_MULT, "min": list(GEO_MIN), **_geo}
            _meta.update(fetched_at=datetime.now(timezone.utc).isoformat(), source=DOC_URL + " + " + LIVE_URL)
            cache.write_text(json.dumps({**_meta, "prices": _live, "sources": _src, "fast": _fast,
                                         "fast_sources": _fast_src, "geo": _geo, "ext_prices": _ext},
                                        indent=1, sort_keys=True))
            if changed:
                shown = changed if len(changed) <= 12 else changed[:12] + [f"... +{len(changed) - 12} more"]
                log(f"[pricing] rates changed for: {', '.join(shown)}")
        else:
            log(f"[pricing] live refresh failed; using {'cached' if _live else 'built-in'} rates")
    if conn is not None:
        stale_est = _estimated_now_exact(conn)  # fallback-priced rows whose model now has a real row
        if changed or geo_changed or first_run or force or stale_est:
            n = reprice(conn)
            log(f"[pricing] repriced {n} rows")
    return {"fetched": fetched, "changed": changed, "fetched_at": _meta.get("fetched_at")}


def _estimated_now_exact(conn: sqlite3.Connection) -> bool:
    """True if a fallback-priced row now has its own rate, or an unpriced row now has any rate."""
    try:
        models = conn.execute("SELECT DISTINCT model, tier FROM messages WHERE price_estimated=1").fetchall()
        unpriced = [m for (m,) in conn.execute("SELECT DISTINCT model FROM messages WHERE tier IS NULL")]
    except sqlite3.OperationalError:
        return False
    # an estimated row is stale when its model now has its own rate, or its alias was removed/retargeted
    return (any((r := resolve(m or "")) is None or not r[2] or r[1] != t for m, t in models)
            or any(resolve(m or "") is not None for m in unpriced))


def reprice(conn: sqlite3.Connection) -> int:
    """Recompute cost/tier/fast premium/web fees for every row from stored token counts and modifiers.

    One set-based UPDATE per distinct (model, speed, inference_geo); the date-windowed fast
    multiplier is a CASE on ts inside the UPDATE.
    """
    n = 0
    groups = conn.execute("SELECT DISTINCT model, COALESCE(speed,'standard'), COALESCE(inference_geo,'') "
                          "FROM messages").fetchall()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    has_est = "est_cost_usd" in cols  # v8: usage estimates for messages whose usage was never reported
    for model, speed, geo in groups:
        r = resolve(model or "")
        if r is None:
            # no rate (any more): an internal model, or a model whose alias was removed, goes back to $0
            cur = conn.execute(
                "UPDATE messages SET tier=NULL, price_estimated=0, cost_usd=0, fast_premium_usd=0, "
                "server_tool_cost_usd=0" + (", est_cost_usd=0" if has_est else "") +
                " WHERE model=? AND COALESCE(speed,'standard')=? AND COALESCE(inference_geo,'')=?"
                " AND (price_estimated=1 OR ?)", (model, speed, geo, int(is_internal(model or ""))))
            n += cur.rowcount
            continue
        p, tier, est = r
        if speed == "fast":
            mb, cut, ma = fast_spec(model or "")
            if mb == ma == 1.0:
                _anomaly(model or "")
        else:
            mb, cut, ma = 1.0, "", 1.0
        g = geo_multiplier(model or "", geo)
        base = ("(COALESCE(input_tokens,0)*:r0 + COALESCE(output_tokens,0)*:r1 + COALESCE(cache_5m_write,0)*:r2 "
                "+ COALESCE(cache_1h_write,0)*:r3 + COALESCE(cache_read,0)*:r4)/1000000.0*:g")
        fm = "(CASE WHEN ts < :cut THEN :mb ELSE :ma END)"
        est_base = ("(COALESCE(est_input_tokens,0)*:r0 + COALESCE(est_output_tokens,0)*:r1 "
                    "+ COALESCE(est_cache_write,0)*:r2 + COALESCE(est_cache_read,0)*:r4)/1000000.0*:g")
        cur = conn.execute(
            f"UPDATE messages SET tier=:tier, price_estimated=:est, "
            f"cost_usd={base}*{fm} + COALESCE(web_search_requests,0)*:fee, "
            f"fast_premium_usd={base}*({fm}-1.0), "
            f"server_tool_cost_usd=COALESCE(web_search_requests,0)*:fee"
            + (f", est_cost_usd=CASE WHEN usage_estimated=1 THEN {est_base}*{fm} ELSE 0 END " if has_est else " ") +
            f"WHERE model=:model AND COALESCE(speed,'standard')=:speed AND COALESCE(inference_geo,'')=:geo",
            dict(tier=tier, est=int(est), r0=p[0], r1=p[1], r2=p[2], r3=p[3], r4=p[4], g=g, cut=cut,
                 mb=mb, ma=ma, fee=WEB_SEARCH_USD, model=model, speed=speed, geo=geo),
        )
        n += cur.rowcount
    conn.commit()
    return n


def meta() -> dict:
    srcs: dict[str, int] = {}
    for m in _table():
        s = _src.get(m, "litellm") if m in _live else "seed"
        srcs[s] = srcs.get(s, 0) + 1
    return {"fetched_at": _meta.get("fetched_at"), "source": _meta.get("source"), "live_models": len(_live),
            "ext_models": len(_ext_table()),
            "sources": srcs, "model_sources": {m: price_source(m) for m in sorted(_table())},
            "fast_multipliers": {**SEED_FAST, **_fast}, "anomalies": dict(ANOMALIES)}
