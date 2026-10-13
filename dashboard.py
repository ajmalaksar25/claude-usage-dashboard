"""FastAPI dashboard server.

Endpoints all accept ?window=all|1y|6m|3m|1m|15d|1w|today (or explicit
?from=YYYY-MM-DD&to=YYYY-MM-DD, or ?day=YYYY-MM-DD for one local day) and return JSON. The HTML
shell is served at /.

Filters on every data endpoint: account=, project=, tool= (claude-code | codex-cli | pi; repeatable
or comma-separated) and model= (canonical model id, e.g. claude-opus-4-5 or gpt-5.6-sol; repeatable
or comma-separated; matched through pricing.model_key, so dated/prefixed raw ids match too). Tables
that only Claude Code writes (api_events, tool_calls, slash_prompts) are empty under a tool filter
without claude-code; model= narrows tool_calls through their message and does not apply to
api_events / slash_prompts (they carry no model).

Time zones: stored timestamps are UTC and stay the truth. Day/hour/weekday buckets and the
from/to/today bounds are computed at query time in ONE configured zone: $CLAUDE_USAGE_TZ, else
"timezone" in config.json, else the system zone. (The ts_day/ts_hour/ts_dow columns written at
index time are kept for the CLI but no longer used here.)
"""
from __future__ import annotations

import contextvars
import functools
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time as _time
import urllib.parse
import webbrowser
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import billing
from billing import subscription_cost
import indexer
from indexer import discover_accounts, reindex
import pricing
import ccm
import gmail_scraper
import watcher as watcher_mod

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", ROOT / "usage.db")).expanduser()
HOST = "127.0.0.1"
PORT = 8765

# Set by main() at startup based on `--all` CLI flag. Persisted across /refresh.
EXTRAS_ON = False

# Set by main() unless started with `--no-watch`. See watcher.py.
WATCHER: watcher_mod.Watcher | None = None

WINDOWS = {
    "today": 0,
    "1w": 7,
    "15d": 15,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    "all": None,
}


# Additive pricing-modifier aggregates appended to every cost rollup (fast mode, web search, estimates).
# estimated_msgs = priced from an estimated RATE (family fallback / model alias; in cost_usd).
# usage_estimated_msgs / estimated_cost_usd = messages whose USAGE was never reported, with an
# estimated cost that is NOT in cost_usd (indexer.estimate_usage). internal_msgs = a vendor's internal
# model with no public price (pricing.INTERNAL_MODELS); unpriced_msgs = truly unknown models only.
_INTERNAL_SQL = "LOWER(model) IN (" + ", ".join(f"'{m}'" for m in pricing.INTERNAL_MODELS) + ")"
_MOD = f"""COALESCE(SUM(CASE WHEN speed='fast' THEN cost_usd ELSE 0 END),0) AS fast_cost_usd,
              COALESCE(SUM(fast_premium_usd),0)      AS fast_premium_usd,
              COALESCE(SUM(CASE WHEN speed='fast' THEN 1 ELSE 0 END),0) AS fast_msgs,
              COALESCE(SUM(server_tool_cost_usd),0)  AS web_search_fees_usd,
              COALESCE(SUM(web_search_requests),0)   AS web_search_requests,
              COALESCE(SUM(price_estimated),0)       AS estimated_msgs,
              COALESCE(SUM(CASE WHEN tier IS NULL AND NOT {_INTERNAL_SQL} THEN 1 ELSE 0 END),0) AS unpriced_msgs,
              COALESCE(SUM(CASE WHEN tier IS NULL AND {_INTERNAL_SQL} THEN 1 ELSE 0 END),0) AS internal_msgs,
              COALESCE(SUM(usage_estimated),0)       AS usage_estimated_msgs,
              COALESCE(SUM(est_cost_usd),0)          AS estimated_cost_usd"""


# ---------- time zone ----------

@lru_cache(maxsize=32)
def _zone(name: str) -> tzinfo:
    return ZoneInfo(name.lstrip(":"))


@lru_cache(maxsize=1)
def _system_tz() -> tzinfo:
    cands = [os.environ["TZ"]] if os.environ.get("TZ") else []
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            cands.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for c in cands:
        try:
            return _zone(c)
        except Exception:
            continue
    return datetime.now().astimezone().tzinfo  # fixed offset: no DST, last resort


def get_tz() -> tzinfo:
    """The one zone every bucket and date bound uses: env > config.json > system."""
    name = os.environ.get("CLAUDE_USAGE_TZ") or billing.load_config().get("timezone")
    if name:
        try:
            return _zone(str(name))
        except Exception:
            pass
    return _system_tz()


def tz_name(tz: tzinfo | None = None) -> str:
    tz = tz or get_tz()
    return getattr(tz, "key", None) or str(tz)


_TZS: dict[str, tzinfo] = {}


@lru_cache(maxsize=400_000)
def _local_parts(prefix: str, tzkey: str) -> tuple[str, int, int, str]:
    """UTC 'YYYY-MM-DDTHH:MM' -> (local day, local hour, local weekday 0=Mon, 'YYYY-MM-DDTHH' local).

    Minute precision because some zones are offset by :30/:45 (IST is +05:30)."""
    dt = datetime.fromisoformat(prefix).replace(tzinfo=timezone.utc).astimezone(_TZS[tzkey])
    return dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday(), dt.strftime("%Y-%m-%dT%H")


def _register_tz_functions(c: sqlite3.Connection, tz: tzinfo) -> None:
    key = tz_name(tz)
    _TZS[key] = tz

    def part(i):
        def f(ts):
            if not ts or len(ts) < 16:
                return None
            try:
                return _local_parts(ts[:16], key)[i]
            except ValueError:
                return None
        return f

    for i, name in enumerate(("local_day", "local_hour", "local_dow", "local_hour_key")):
        c.create_function(name, 1, part(i), deterministic=True)


_SQL_FMT = {"day": "'%Y-%m-%d'", "hour": "'%H'", "dow": "'%w'", "hour_key": "'%Y-%m-%dT%H'"}
_SEG_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)


@lru_cache(maxsize=64)
def _offset_segments(tzkey: str, lo_day: str, hi_day: str) -> tuple[tuple[str | None, int], ...]:
    """UTC-offset segments of the zone over [lo_day, hi_day]: ((utc upper bound | None, offset minutes), ...).

    Transitions are found day by day, then to the minute. The first segment also covers everything
    before lo_day and the last everything after hi_day."""
    tz = _TZS[tzkey]
    off = lambda dt: int(dt.astimezone(tz).utcoffset().total_seconds() // 60)
    cur = max(datetime.fromisoformat(lo_day).replace(tzinfo=timezone.utc), _SEG_FLOOR)
    end = max(datetime.fromisoformat(hi_day).replace(tzinfo=timezone.utc), cur)
    segs: list[tuple[str | None, int]] = []
    o = off(cur)
    while cur < end:
        nxt = cur + timedelta(days=1)
        if off(nxt) != o:
            a, b = cur, nxt
            while (b - a) > timedelta(minutes=1):
                m = a + (b - a) / 2
                a, b = (m, b) if off(m) == o else (a, m)
            segs.append((b.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S"), o))
            o = off(b)
        cur = nxt
    segs.append((None, o))
    return tuple(segs)


def _local_sql(part: str, frm: str, to: str, col: str = "ts") -> str:
    """Native-SQLite equivalent of local_<part>(col) for rows in [frm, to).

    WHY: a Python UDF per row cost ~1 us x 285K rows on every bucketed query; strftime with the
    zone's offset (a CASE over its DST transitions in the window) gives the same buckets in C."""
    tz = get_tz()
    key = tz_name(tz)
    _TZS[key] = tz
    segs = _offset_segments(key, frm[:10], to[:10])
    if len(segs) == 1:
        mod = f"'{segs[0][1]:+d} minutes'"
    else:
        mod = "CASE " + " ".join(f"WHEN {col} < '{b}' THEN '{o:+d} minutes'" for b, o in segs[:-1]) \
              + f" ELSE '{segs[-1][1]:+d} minutes' END"
    e = f"strftime({_SQL_FMT[part]}, {col}, {mod})"
    if part == "hour":
        return f"CAST({e} AS INTEGER)"
    if part == "dow":  # strftime %w is 0=Sunday; ours is 0=Monday
        return f"((CAST({e} AS INTEGER) + 6) % 7)"
    return e


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """`with _conn() as c:` commits on success and always closes (sqlite3's own context manager
    only commits, which leaked one connection per query)."""
    c = sqlite3.connect(str(DB_PATH))
    try:
        c.row_factory = sqlite3.Row
        _register_tz_functions(c, get_tz())
        with c:
            yield c
    finally:
        c.close()


# ---------- data version + response cache ----------
#
# Every /api read is a pure function of (query, index contents, settings files, clock). The cache key
# carries the first three: the query string, the index version (meta.last_index_at, which every
# reindex writes, plus the main DB file's stat) and the settings files' stat. The clock is handled by
# a per-entry TTL: rolling windows, "today" and subscription proration drift slowly, so an entry
# lives at most _CACHE_TTL_S. In-process reindexes also call
# invalidate_cache(). The -wal file is deliberately not part of the key: SQLite creates and deletes
# it as connections open and close, which flipped the key on every request.

_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 512
_CACHE_TTL_S = 300
_GEN = 0  # bumped by invalidate_cache()


def _index_stamp() -> str | None:
    try:
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            r = c.execute("SELECT v FROM meta WHERE k='last_index_at'").fetchone()
        finally:
            c.close()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def data_version() -> tuple:
    parts: list = [str(DB_PATH), _GEN, os.environ.get("CLAUDE_USAGE_TZ", ""), _index_stamp()]
    for p in (DB_PATH, billing.config_path(), billing.BILLING_PATH, indexer.ACCOUNTS_FILE):
        try:
            st = os.stat(p)
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


def invalidate_cache() -> None:
    global _GEN
    with _CACHE_LOCK:
        _GEN += 1
        _CACHE.clear()
        _RECON_CACHE.clear()


def cached(fn):
    """Memoise an endpoint on (name, query params, data_version()) for up to _CACHE_TTL_S.
    Error responses are not kept."""
    @functools.wraps(fn)
    def wrapper(request: Request):
        key = (fn.__name__, tuple(sorted(request.query_params.multi_items())), data_version())
        now = _time.monotonic()
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and now - hit[0] < _CACHE_TTL_S:
                _CACHE.move_to_end(key)
                return hit[1]
        with _filtering(request):
            out = fn(request)
        if not isinstance(out, Response):
            with _CACHE_LOCK:
                _CACHE[key] = (now, out)
                while len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
        return out
    return wrapper


def _reindex_and_invalidate() -> dict:
    try:
        return reindex(DB_PATH, extras=EXTRAS_ON, verbose=False)
    finally:
        invalidate_cache()
        # rebuild the per-session reconciliation now, off the request path
        threading.Thread(target=_prewarm, daemon=True).start()


def _prewarm() -> None:
    try:
        _recon_data()
    except Exception:  # best effort: the next request computes it anyway
        pass


def _local_iso(utc_iso: str, tz: tzinfo | None = None) -> str:
    """UTC bound -> same instant with the configured zone's offset (billing prorates on its local date)."""
    try:
        return datetime.fromisoformat(utc_iso).astimezone(tz or get_tz()).isoformat()
    except ValueError:
        return utc_iso


def _resolve_window(window: str | None, frm: str | None, to: str | None,
                    tz: tzinfo | None = None) -> tuple[str, str, str]:
    """Return (from_iso, to_iso, label) -- both bounds in UTC ISO.

    from/to are calendar days in the configured zone: from=2026-10-07 starts at local midnight.
    """
    tz = tz or get_tz()
    now = datetime.now(timezone.utc)
    if frm and to:
        f = datetime.combine(date.fromisoformat(frm[:10]), time.min, tzinfo=tz)
        t = datetime.combine(date.fromisoformat(to[:10]) + timedelta(days=1), time.min, tzinfo=tz)
        return f.astimezone(timezone.utc).isoformat(), t.astimezone(timezone.utc).isoformat(), f"{frm}..{to}"

    window = (window or "all").lower()
    if window == "today":
        local_start = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz)
        f = local_start.astimezone(timezone.utc)
        t = datetime.combine(local_start.date() + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
        return f.isoformat(), t.isoformat(), "today"

    days = WINDOWS.get(window)
    if days is None:
        return "1970-01-01T00:00:00+00:00", (now + timedelta(days=1)).isoformat(), "all"
    f = (now - timedelta(days=days))
    return f.isoformat(), (now + timedelta(days=1)).isoformat(), window


def _qparams(req: Request) -> tuple[str, str, str]:
    q = req.query_params
    day = (q.get("day") or "").strip()
    if day:  # single local-day drill-down: same as from=day&to=day
        return _resolve_window(None, day, day)
    return _resolve_window(q.get("window"), q.get("from"), q.get("to"))


# tool= / model= filters for the request being served (set by @cached and the few uncached readers).
_FILTERS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("cud_filters", default=None)


def _multi(req: Request, key: str) -> list[str]:
    out: list[str] = []
    for v in req.query_params.getlist(key):
        out += [x.strip() for x in v.split(",") if x.strip() and x.strip().lower() != "all"]
    return list(dict.fromkeys(out))


# Breakdown dimensions usable as page filters: ?<param>=<a value from /api/breakdown?dim=<dim>>
# (repeatable). sidechain also takes 0|1. The value is compared with the breakdown's own SQL
# expression, so sentinel rows such as "(main thread)", "(none)" or "(no PR)" filter too.
DIM_FILTERS = ("agent", "skill", "mcp", "plugin", "branch", "pr", "entrypoint", "effort", "sidechain")


def _filters_from(req: Request) -> dict:
    dims = {d: _multi(req, d) for d in DIM_FILTERS}
    if dims["sidechain"]:
        dims["sidechain"] = [{"1": "subagent", "0": "main", "true": "subagent", "false": "main"}.get(v.lower(), v)
                             for v in dims["sidechain"]]
    hours = _ints(req, "hour", 0, 23)
    dows = _ints(req, "dow", 0, 6)
    out = {"tools": _multi(req, "tool"), "models": _multi(req, "model"),
           "dims": {d: v for d, v in dims.items() if v}, "hours": hours, "dows": dows}
    if hours or dows:  # bounds for the zone's offset segments (see _local_sql)
        try:
            out["bounds"] = _qparams(req)[:2]
        except ValueError:
            out["bounds"] = _resolve_window("all", None, None)[:2]
    return out


def _ints(req: Request, key: str, lo: int, hi: int) -> list[int]:
    """Repeatable / comma-separated integer filter (?hour=9&hour=10, ?dow=0,4); out-of-range or
    non-numeric values are ignored."""
    out: list[int] = []
    for v in _multi(req, key):
        try:
            n = int(v)
        except ValueError:
            continue
        if lo <= n <= hi:
            out.append(n)
    return sorted(set(out))


@contextmanager
def _filtering(req: Request):
    tok = _FILTERS.set(_filters_from(req))
    try:
        yield
    finally:
        _FILTERS.reset(tok)


@contextmanager
def _without(models: bool = False, dim: str | None = None):
    """Drop the model filter and/or one dimension filter for a nested query (a breakdown or by_model
    view keeps showing every value of its own dimension so the UI can highlight the selection)."""
    f = dict(_FILTERS.get() or {})
    if models:
        f["models"] = []
    if dim:
        f["dims"] = {k: v for k, v in (f.get("dims") or {}).items() if k != dim}
    tok = _FILTERS.set(f)
    try:
        yield
    finally:
        _FILTERS.reset(tok)


def _dim_frag(dims: dict) -> tuple[str, list]:
    """Message-level SQL for the breakdown-dimension filters (columns of messages, unqualified)."""
    frag, params = "", []
    for d, vals in dims.items():
        if d == "pr":
            prs = [v for v in vals if v != "(no PR)"]
            parts = []
            if prs:
                parts.append(f"session_id IN (SELECT session_id FROM pr_links WHERE repo || '#' || pr_number "
                             f"IN ({_in_list(prs)}))")
                params += prs
            if len(prs) < len(vals):
                parts.append("session_id NOT IN (SELECT session_id FROM pr_links WHERE session_id IS NOT NULL)")
            frag += " AND (" + " OR ".join(parts) + ")"
        else:
            frag += f" AND ({BREAKDOWN_DIMS[d]}) IN ({_in_list(vals)})"
            params += vals
    return frag, params


_RAW_MODELS: dict = {}


def _raw_models(models: list[str]) -> list[str]:
    """Raw messages.model values whose canonical id (pricing.model_key) or raw id is requested."""
    stamp = (str(DB_PATH), _index_stamp(), _GEN)
    if _RAW_MODELS.get("stamp") != stamp:
        with _conn() as c:
            _RAW_MODELS.update(stamp=stamp, all=[r[0] for r in c.execute("SELECT DISTINCT model FROM messages")
                                                 if r[0] is not None])
    want = {m.lower() for m in models}
    return [m for m in _RAW_MODELS["all"] if m.lower() in want or pricing.model_key(m) in want]


def _filter_frag(table: str = "messages") -> tuple[str, tuple]:
    f = _FILTERS.get() or {}
    tools, models, dims = f.get("tools") or [], f.get("models") or [], f.get("dims") or {}
    frag, params = "", []
    if tools:
        if table == "messages":
            frag += f" AND tool IN ({_in_list(tools)})"
            params += tools
        elif indexer.TOOL_CLAUDE not in tools:
            frag += " AND 0"
    if models:
        raw = _raw_models(models)
        if not raw:
            frag += " AND 0"
        elif table == "messages":
            frag += f" AND model IN ({_in_list(raw)})"
            params += raw
        elif table == "tool_calls":
            frag += f" AND msg_id IN (SELECT msg_id FROM messages WHERE model IN ({_in_list(raw)}))"
            params += raw
    if dims:
        df, dp = _dim_frag(dims)
        if table == "messages":
            frag += df
            params += dp
        elif table == "tool_calls":
            frag += f" AND msg_id IN (SELECT msg_id FROM messages WHERE 1{df})"
            params += dp
    # hour= / dow= (local hour 0-23, weekday 0=Monday in the configured zone): every table has ts
    lo, hi = f.get("bounds") or ("1970-01-01", "1970-01-01")
    for part, vals in (("hour", f.get("hours")), ("dow", f.get("dows"))):
        if vals:
            frag += f" AND {_local_sql(part, lo, hi, 'ts')} IN ({','.join(str(int(v)) for v in vals)})"
    return frag, tuple(params)


def _filtered() -> bool:
    f = _FILTERS.get() or {}
    return bool(f.get("tools") or f.get("models") or f.get("dims") or f.get("hours") or f.get("dows"))


def _acct(req: Request) -> str:
    """Account filter from ?account=... ('' or 'all' means no filter)."""
    a = (req.query_params.get("account") or "").strip()
    return "" if a.lower() == "all" else a


def _proj(req: Request) -> str:
    """Project filter from ?project=... ('' means no filter)."""
    return (req.query_params.get("project") or "").strip()


def _af(acct: str, proj: str = "", table: str = "messages") -> tuple[str, tuple]:
    """(SQL fragment, extra params) for optional account/project filters plus the request's
    tool=/model= filters (see _filter_frag)."""
    frag, params = "", []
    if acct:
        frag += " AND account = ?"
        params.append(acct)
    if proj:
        frag += " AND project = ?"
        params.append(proj)
    ff, fp = _filter_frag(table)
    return frag + ff, (*params, *fp)


# ---------- aggregate queries ----------

def q_summary(frm: str, to: str, acct: str = "", proj: str = "") -> dict:
    frag, ap = _af(acct, proj)
    with _conn() as c:
        row = c.execute(
            f"""
            SELECT
              COUNT(*)                                    AS msgs,
              COUNT(DISTINCT session_id)                  AS sessions,
              COUNT(DISTINCT project)                     AS projects,
              COALESCE(SUM(input_tokens),0)               AS input_tokens,
              COALESCE(SUM(output_tokens),0)              AS output_tokens,
              COALESCE(SUM(cache_5m_write),0)             AS cache_5m_write,
              COALESCE(SUM(cache_1h_write),0)             AS cache_1h_write,
              COALESCE(SUM(cache_read),0)                 AS cache_read,
              COALESCE(SUM(cost_usd),0)                   AS api_cost,
              COALESCE(SUM(thinking_tokens),0)            AS thinking_tokens,
              {_MOD},
              MIN(ts)                                     AS first_ts,
              MAX(ts)                                     AS last_ts
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            """,
            (frm, to, *ap),
        ).fetchone()
        by_am = c.execute(
            f"SELECT account, model, COALESCE(SUM(cost_usd),0) AS api_cost, COUNT(*) AS msgs FROM messages "
            f"WHERE ts >= ? AND ts < ?{frag} GROUP BY account, model", (frm, to, *ap)).fetchall()
    acct_cost: dict[str, float] = {}
    non_claude, non_claude_msgs = 0.0, 0
    for r in by_am:
        acct_cost[r["account"]] = acct_cost.get(r["account"], 0.0) + float(r["api_cost"] or 0)
        if not pricing.canonical(r["model"] or "").startswith("claude"):  # gateway models (gpt-*, ...)
            non_claude += float(r["api_cost"] or 0)
            non_claude_msgs += r["msgs"]
    per_acct = [{"account": a, "api_cost": v} for a, v in acct_cost.items()]
    d = dict(row) if row else {}
    d["non_claude_cost_usd"] = round(non_claude, 2)
    d["non_claude_msgs"] = non_claude_msgs
    d["total_tokens"] = (
        d.get("input_tokens", 0)
        + d.get("output_tokens", 0)
        + d.get("cache_5m_write", 0)
        + d.get("cache_1h_write", 0)
        + d.get("cache_read", 0)
    )
    infos = account_billing_map()
    sub, shares = _plan_shares(frm, to, infos)
    usage = _usage_share(frm, to, proj)
    shares = _allocated(shares, usage)  # slicing filters: each account's plan cost x its usage share
    d["plan_cost_basis"] = "window" if usage is None else "allocated_by_usage"
    if acct or _slicing(proj):  # account / tool / model / project / hour / weekday / dimension scope
        # receipts follow the accounts in scope; a plan shared by several accounts is split by usage
        scope = [acct] if acct else list(acct_cost)
        groups = {pb for a in scope if (pb := (infos.get(a) or {}).get("paid_by"))}
        related = set(scope) | {n for n, i in infos.items() if i.get("paid_by") in groups}
        charges = [ch for ch in sub["charges"]
                   if ch["account"] in related and billing._counts_for(ch["account"], ch["provider"])]
        d["subscription_cost"] = round(sum(shares.get(a, 0.0) for a in scope), 2)
        d["billing_charges"] = charges
        d["billing_coverage"] = {"first": min((ch["start"] for ch in charges), default=None),
                                 "last": max((ch["end"] for ch in charges), default=None)}
    else:
        d["subscription_cost"] = sub["total"]
        d["billing_charges"] = sub["charges"]
        d["billing_coverage"] = sub["coverage"]
    d["api_cost"] = round(float(d.get("api_cost") or 0.0), 2)
    d["savings"] = round(d["api_cost"] - d["subscription_cost"], 2)
    # additive: who pays the plan. subscription_cost stays the total plan value (you + employer).
    employer = {n for n, i in infos.items() if i.get("payer") == "employer"}
    if acct or _slicing(proj):
        emp_scope = sorted(a for a in scope if a in employer)
    else:
        emp_scope = sorted(employer)
    d["paid_by_employer_usd"] = round(sum(shares.get(a, 0.0) for a in emp_scope), 2)
    d["paid_by_you_usd"] = round(max(d["subscription_cost"] - d["paid_by_employer_usd"], 0.0), 2)
    d["employer_accounts"] = emp_scope
    self_api = sum(per["api_cost"] for per in per_acct if per["account"] not in employer)
    d["savings_you"] = round(round(self_api, 2) - d["paid_by_you_usd"], 2)
    d["multiplier"] = (
        round(d["api_cost"] / d["subscription_cost"], 1)
        if d["subscription_cost"] > 0 else None
    )
    # additive v5 keys: what each account actually paid on its billing path, and side calls
    actual, unknown = 0.0, []
    for r in per_acct:
        info = infos.get(r["account"]) or billing.account_billing(r["account"], [])
        a = billing.actual_cost(info, float(r["api_cost"] or 0), shares.get(r["account"], 0.0))
        if a is None:
            unknown.append(r["account"])
        else:
            actual += a
    d["actual_cost_usd"] = round(actual, 2)
    d["actual_cost_unknown_accounts"] = sorted(unknown)
    sc = q_reconciliation(frm, to, acct, proj)["totals"]
    d["side_call_cost_usd"] = sc["side_call_cost_usd"]  # evidenced unlogged calls only (see _RECON_NOTE)
    d["side_call_web_search_usd"] = sc["side_call_web_search_usd"]
    d["side_call_web_searches"] = sc["web_search_requests"]
    d["cc_gap_usd"] = sc["cc_gap_usd"]
    d["subagent_partial_output_usd"] = sc["subagent_partial_output_usd"]
    d["unexplained_gap_usd"] = sc["unexplained_gap_usd"]
    d["unexplained_gap_note"] = sc["unexplained_gap_note"]
    d["reconciled_sessions"] = sc["reconciled_sessions"]
    d["total_sessions"] = sc["total_sessions"]
    d["reconciliation_coverage_pct"] = sc["coverage_pct"]
    d["api_cost_incl_side_calls"] = round(d["api_cost"] + sc["side_call_cost_usd"], 2)
    # usage estimates (messages a gateway logged without usage) stay out of api_cost; opt-in total:
    d["estimated_cost_usd"] = round(float(d.get("estimated_cost_usd") or 0.0), 2)
    d["api_cost_incl_estimates"] = round(d["api_cost"] + d["estimated_cost_usd"], 2)
    d["timezone"] = tz_name()
    d.update(_day_stats(frm, to, acct, proj, d.get("first_ts"), d["api_cost"]))
    return d


def _day_stats(frm: str, to: str, acct: str, proj: str, first_ts: str | None, api_cost: float) -> dict:
    """active_days (local days with usage), window_days (days the window spans up to now, rounded up;
    1w -> 7, today -> 1, from=a&to=b -> b-a+1, all -> since the first message), avg_cost_per_active_day
    (API-equivalent cost / active_days), busiest_day (costliest local day)."""
    days = q_heatmap(frm, to, acct, proj)
    active = [r for r in days if r["msgs"]]
    now = datetime.now(timezone.utc)
    lo = datetime.fromisoformat(frm)
    if lo.year <= 1971 and first_ts:  # window=all
        lo = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
    hi = min(datetime.fromisoformat(to), now)
    # ceil of the span in days; the 0.05 slack absorbs a DST hour in an explicit from/to range
    span = (hi - lo).total_seconds() / 86400
    window_days = max(1, math.ceil(span - 0.05))
    busiest = max(active, key=lambda r: (r["cost"], r["msgs"]), default=None)
    return {"active_days": len(active), "window_days": window_days,
            "avg_cost_per_active_day": round(api_cost / len(active), 2) if active else None,
            "busiest_day": {"day": busiest["day"], "cost": round(busiest["cost"], 2)} if busiest else None}


def _plan_shares(frm: str, to: str, infos: dict[str, dict]) -> tuple[dict, dict[str, float]]:
    """(subscription_cost over every account, account -> receipts applied after plan-group sharing).
    Shares use each account's unfiltered API-equivalent cost in the window."""
    sub = subscription_cost(_local_iso(frm), _local_iso(to))
    with _conn() as c:
        api = {r[0]: float(r[1] or 0) for r in c.execute(
            "SELECT account, SUM(cost_usd) FROM messages WHERE ts >= ? AND ts < ? GROUP BY account", (frm, to))}
    return sub, billing.plan_receipts(infos, api, sub["by_account_provider"])


def _slicing(proj: str = "") -> bool:
    """A SLICING filter keeps part of each account's usage in the window: project, tool, model, hour,
    weekday or a breakdown dimension. Window (from/to/day) and account are not slicing filters."""
    return bool(proj) or _filtered()


def _usage_share(frm: str, to: str, proj: str = "") -> dict[str, float] | None:
    """account -> share of its window usage the slicing filters keep (filtered api_cost / unfiltered
    api_cost, same window and account; by message count when its unfiltered cost is $0). None when
    no slicing filter is active. A whole-window plan cost times this share is the plan cost
    allocated to the slice, like the paid_by split allocates one plan between accounts."""
    if not _slicing(proj):
        return None
    frag, ap = _af("", proj)
    q = "SELECT account, COALESCE(SUM(cost_usd),0), COUNT(*) FROM messages WHERE ts >= ? AND ts < ?"
    with _conn() as c:
        part = {a: (float(v), n) for a, v, n in c.execute(f"{q}{frag} GROUP BY account", (frm, to, *ap))}
        tok = _FILTERS.set({})
        try:
            whole = {a: (float(v), n) for a, v, n in c.execute(f"{q} GROUP BY account", (frm, to))}
        finally:
            _FILTERS.reset(tok)
    out = {}
    for a, (wv, wn) in whole.items():
        pv, pn = part.get(a, (0.0, 0))
        out[a] = min(1.0, pv / wv) if wv > 0 else (min(1.0, pn / wn) if wn else 0.0)
    return out


def _allocated(shares: dict[str, float], usage: dict[str, float] | None) -> dict[str, float]:
    return dict(shares) if usage is None else {a: v * usage.get(a, 0.0) for a, v in shares.items()}


TIMESERIES_BY = ("model", "token_type")


def q_timeseries(frm: str, to: str, bucket: str, acct: str = "", proj: str = "", by: str | None = None) -> list[dict]:
    grp = _local_sql("hour_key" if bucket == "hour" else "day", frm, to)
    frag, ap = _af(acct, proj)
    if by == "token_type":  # one row per (bucket, token type): the API-rate cost split by rate x tokens
        with _conn() as c:
            groups = _tt_groups(c, frm, to, frag, ap, grp)
        acc: dict[tuple[str, str], dict[str, float]] = {}
        for g in groups:
            for n, part in _split_token_types(g).items():
                if part["cost"] > 0:
                    a = acc.setdefault((g["bucket"], n), {"cost": 0.0, "fast_cost_usd": 0.0, "fast_premium_usd": 0.0})
                    for k, v in part.items():
                        a[k] += v
        return [{"bucket": b, "token_type": n, **{k: round(v, 6) for k, v in a.items()}}
                for (b, n), a in sorted(acc.items())]
    if by == "model":  # one row per (bucket, model), costliest model first within a bucket
        with _conn() as c:
            rows = c.execute(
                f"""
                SELECT
                  {grp} AS bucket,
                  model,
                  COALESCE(SUM(cost_usd),0)       AS cost,
                  COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
                  COALESCE(SUM(input_tokens),0)   AS input_tokens,
                  COALESCE(SUM(output_tokens),0)  AS output_tokens,
                  COALESCE(SUM(cache_read),0)     AS cache_read,
                  COALESCE(SUM(cache_5m_write+cache_1h_write),0) AS cache_write,
                  COUNT(*) AS msgs,
                  {_MOD}
                FROM messages
                WHERE ts >= ? AND ts < ?{frag}
                GROUP BY bucket, model
                ORDER BY bucket, cost DESC, model
                """,
                (frm, to, *ap),
            ).fetchall()
        return [dict(r) for r in rows]
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              {grp} AS bucket,
              COUNT(*) AS msgs,
              COALESCE(SUM(input_tokens),0)   AS input_tokens,
              COALESCE(SUM(output_tokens),0)  AS output_tokens,
              COALESCE(SUM(cache_5m_write+cache_1h_write),0) AS cache_write,
              COALESCE(SUM(cache_read),0)     AS cache_read,
              COALESCE(SUM(cost_usd),0)       AS cost,
              {_MOD}
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY bucket
            ORDER BY bucket
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_by_model(frm: str, to: str, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              model,
              tier,
              COUNT(*) AS msgs,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost,
              {_MOD}
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY model, tier
            ORDER BY tokens DESC
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_by_account(frm: str, to: str, proj: str = "") -> list[dict]:
    """Per-account usage with prorated subscription cost and savings (plan receipts shared by usage
    between the accounts with the same paid_by)."""
    frag, ap = _af("", proj)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              account,
              COUNT(*) AS msgs,
              COUNT(DISTINCT session_id) AS sessions,
              COUNT(DISTINCT project)    AS projects,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS api_cost,
              {_MOD},
              MIN(ts) AS first_ts,
              MAX(ts) AS last_ts
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY account
            ORDER BY tokens DESC
            """,
            (frm, to, *ap),
        ).fetchall()
    infos = account_billing_map()
    _sub, shares = _plan_shares(frm, to, infos)
    usage = _usage_share(frm, to, proj)
    shares = _allocated(shares, usage)  # slicing filters: each account's plan cost x its usage share
    sc = q_reconciliation(frm, to, "", proj)
    side_by_acct: dict[str, list] = {}
    for s_ in sc["rows"]:
        side_by_acct.setdefault(s_["account"], []).append(s_)
    out = []
    for r in rows:
        d = dict(r)
        raw_api = float(d["api_cost"] or 0.0)
        d["api_cost"] = round(raw_api, 2)
        d["estimated_cost_usd"] = round(float(d.get("estimated_cost_usd") or 0.0), 2)
        d["api_cost_incl_estimates"] = round(raw_api + d["estimated_cost_usd"], 2)
        d["subscription_cost"] = round(shares.get(d["account"], 0.0), 2)
        d["savings"] = round(d["api_cost"] - d["subscription_cost"], 2)
        d["multiplier"] = (
            round(d["api_cost"] / d["subscription_cost"], 1)
            if d["subscription_cost"] > 0 else None
        )
        info = infos.get(d["account"]) or billing.account_billing(d["account"], [])
        d["billing_mode"] = info["billing_mode"]
        d["payment"] = info["payment"]
        d["tool"] = info.get("tool", indexer.TOOL_CLAUDE)
        d["paid_by"] = info.get("paid_by")
        d["actual_cost_usd"] = billing.actual_cost(info, raw_api, d["subscription_cost"])
        d["payer"] = info.get("payer", "self")
        d["plan_value_usd"] = d["subscription_cost"]
        d["you_paid_usd"] = 0.0 if d["payer"] == "employer" else d["actual_cost_usd"]
        d["plan_cost_basis"] = "window" if usage is None else "allocated_by_usage"
        mine = side_by_acct.get(d["account"], [])
        for k in ("side_call_cost_usd", "subagent_partial_output_usd", "unexplained_gap_usd"):
            d[k] = round(sum(x[k] for x in mine), 2)
        d["cc_gap_usd"] = round(sum(x["delta_usd"] for x in mine), 2)
        d["reconciled_sessions"] = len(mine)
        out.append(d)
    return out


def q_by_project(frm: str, to: str, limit: int, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              project,
              MAX(project_path) AS project_path,
              COUNT(*)          AS msgs,
              COUNT(DISTINCT session_id) AS sessions,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost,
              {_MOD},
              MAX(ts) AS last_ts
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY project
            ORDER BY tokens DESC
            LIMIT ?
            """,
            (frm, to, *ap, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def q_heatmap(frm: str, to: str, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              {_local_sql("day", frm, to)} AS day,
              COUNT(*) AS msgs,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost,
              {_MOD}
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY day
            ORDER BY day
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_distributions(frm: str, to: str, acct: str = "", proj: str = "") -> dict:
    frag, ap = _af(acct, proj)
    with _conn() as c:
        hours = c.execute(
            f"""SELECT {_local_sql("hour", frm, to)} AS hour, COUNT(*) AS msgs,
                      COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
                      COALESCE(SUM(cost_usd),0) AS cost
               FROM messages WHERE ts >= ? AND ts < ?{frag} GROUP BY hour ORDER BY hour""",
            (frm, to, *ap),
        ).fetchall()
        dows = c.execute(
            f"""SELECT {_local_sql("dow", frm, to)} AS dow, COUNT(*) AS msgs,
                      COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
                      COALESCE(SUM(cost_usd),0) AS cost
               FROM messages WHERE ts >= ? AND ts < ?{frag} GROUP BY dow ORDER BY dow""",
            (frm, to, *ap),
        ).fetchall()
        # weekday x hour rhythm (sparse: only cells with usage); dow 0=Mon, hour 0-23 local
        matrix = c.execute(
            f"""SELECT {_local_sql("dow", frm, to)} AS dow, {_local_sql("hour", frm, to)} AS hour, COUNT(*) AS msgs,
                      COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
                      COALESCE(SUM(cost_usd),0) AS cost
               FROM messages WHERE ts >= ? AND ts < ?{frag} GROUP BY dow, hour ORDER BY dow, hour""",
            (frm, to, *ap),
        ).fetchall()
    return {"hours": [dict(r) for r in hours], "dows": [dict(r) for r in dows],
            "hour_dow": [dict(r) for r in matrix]}


def q_top_sessions(frm: str, to: str, by: str, limit: int, acct: str = "", proj: str = "") -> list[dict]:
    order_col = {
        "msgs": "msgs",
        "tokens": "tokens",
        "cost": "cost",
        "duration": "duration_minutes",
        "active": "COALESCE(active_ms, duration_minutes * 60000.0)",
    }.get(by, "tokens")
    frag, ap = _af(acct, proj)
    with _conn() as c:
        rows = c.execute(
            f"""
            WITH s AS (
            SELECT
              session_id,
              MAX(account) AS account,
              MAX(project) AS project,
              MAX(project_path) AS project_path,
              COUNT(*)     AS msgs,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost,
              {_MOD},
              MIN(ts) AS first_ts,
              MAX(ts) AS last_ts,
              CAST((julianday(MAX(ts)) - julianday(MIN(ts))) * 24 * 60 AS INTEGER) AS duration_minutes
            FROM messages
            WHERE ts >= ? AND ts < ?{frag} AND session_id IS NOT NULL
            GROUP BY session_id)
            SELECT s.*, td.active_ms FROM s LEFT JOIN ({_ACTIVE_SQL}) td USING(session_id)
            ORDER BY {order_col} DESC
            LIMIT ?
            """,
            (frm, to, *ap, frm, to, limit),
        ).fetchall()
        out = [dict(r) for r in rows]
        mix = _model_mix(c, [r["session_id"] for r in out], frm, to)
    side = {r["session_id"]: r for r in q_reconciliation(frm, to, acct, proj, [d["session_id"] for d in out])["rows"]}
    for d in out:
        d["model"] = mix[d["session_id"]][0]["model"] if mix.get(d["session_id"]) else None  # most costly model
        _active(d)
        d.pop("active_ms", None)
        sc = side.get(d["session_id"])
        for k in ("side_call_cost_usd", "subagent_partial_output_usd", "unexplained_gap_usd"):
            d[k] = sc[k] if sc else None
    return out


# Main-thread turn time per session in the window (subagent turns overlap their parent's turn).
_ACTIVE_SQL = ("SELECT session_id, SUM(duration_ms) AS active_ms FROM turn_durations "
               "WHERE ts >= ? AND ts < ? AND COALESCE(is_sidechain,0)=0 GROUP BY session_id")


def _active(d: dict) -> None:
    """active_minutes = sum of turn durations; falls back to the first-to-last span when none were logged."""
    if d.get("active_ms"):
        d["active_minutes"] = round(d["active_ms"] / 60000, 1)
        d["active_source"] = "turn_duration"
    else:
        d["active_minutes"] = d.get("duration_minutes")
        d["active_source"] = "span"


def _in_list(ids: list) -> str:
    return ",".join("?" * len(ids))


def _model_mix(c: sqlite3.Connection, ids: list[str], frm: str, to: str) -> dict[str, list[dict]]:
    """session -> [{model, cost, msgs}] by cost desc (ties: more messages first)."""
    out: dict[str, list[dict]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in c.execute(
            f"SELECT session_id, model, COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS msgs FROM messages "
            f"WHERE session_id IN ({_in_list(chunk)}) AND ts >= ? AND ts < ? GROUP BY session_id, model "
            f"ORDER BY session_id, cost DESC, msgs DESC", (*chunk, frm, to)):
            out.setdefault(r["session_id"], []).append({"model": r["model"], "cost": r["cost"], "msgs": r["msgs"]})
    return out


def q_top_days(frm: str, to: str, limit: int, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              {_local_sql("day", frm, to)} AS day,
              COUNT(*) AS msgs,
              COUNT(DISTINCT session_id) AS sessions,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost,
              {_MOD}
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY day
            ORDER BY tokens DESC
            LIMIT ?
            """,
            (frm, to, *ap, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def q_meta() -> dict:
    if not DB_PATH.exists():
        return {"last_index_at": None, "rows": 0, "extras_indexed": False}
    with _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k='last_index_at'").fetchone()
        n = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        e = c.execute("SELECT v FROM meta WHERE k='extras_indexed'").fetchone()
    return {
        "last_index_at": (r[0] if r else None),
        "rows": n,
        "extras_indexed": bool(e and e[0] == "1"),
        "pricing": pricing.meta(),
        "timezone": tz_name(),
    }

# ---------- v5: reconciliation against Claude Code's cost-state ----------

UNEXPLAINED_GAP_NOTE = (
    "Claude Code's cost minus ours, inside the time window of each process that wrote a cost-state, "
    "after removing (1) the subagent partial-usage shortfall and (2) evidenced side calls. It is mostly "
    "input and cache tokens on the main models, in sessions run in auto permission mode, which fits the "
    "auto-mode safety classifier's requests (they are not written to transcripts); that link is "
    "correlational, so this is not counted as side calls. Negative = our transcripts carry usage that "
    "Claude Code's last checkpoint did not.")

_RECON_NOTE = (
    "Coverage is partial: only sessions whose transcript carries a Claude Code cost-state record can be "
    "reconciled, and only messages inside each recorded process's window [startTime, startTime + "
    "totalDuration] are compared (a resume without a cost-state record is excluded, reported as "
    "out_of_window_cost_usd). cc_gap_usd = Claude Code's cost - ours, signed, and splits into: "
    "side_call_cost_usd (evidenced unlogged calls: Haiku-model usage and web-search fees, which Claude Code "
    "books under Haiku while transcripts log no searches), subagent_partial_output_usd (subagent records "
    "written before the final usage, so output_tokens is the stream-start placeholder; the output-token "
    "shortfall priced at list rate) and unexplained_gap_usd. None of it is added to api_cost.")

_RECON_GRACE_MS = 5000  # a record written just after the checkpoint still belongs to the process
_TOK = ("input", "output", "cache_read", "cache_write")


def _recon_empty_totals() -> dict:
    return {"total_sessions": 0, "reconciled_sessions": 0, "coverage_pct": 0.0,
            "our_cost_reconciled_usd": 0.0, "cc_cost_reconciled_usd": 0.0, "cc_gap_usd": 0.0,
            "side_call_cost_usd": 0.0, "side_call_token_cost_usd": 0.0, "side_call_web_search_usd": 0.0,
            "web_search_requests": 0, "subagent_partial_output_usd": 0.0, "subagent_partial_output_tokens": 0,
            "partial_usage_msgs": 0, "unexplained_gap_usd": 0.0, "unexplained_gap_note": UNEXPLAINED_GAP_NOTE,
            "out_of_window_cost_usd": 0.0, "our_cost_scope_usd": 0.0, "cost_coverage_pct": 0.0,
            "sessions_with_unknown_model_cost": 0,
            "gap_by_token_type_usd": {n: 0.0 for n in _TOK}, "gap_tokens": {n: 0 for n in _TOK}}


def _ms_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _recon_compute(c: sqlite3.Connection) -> dict:
    """Window-independent per-session reconciliation, plus each session's lifetime cost.

    One row per session with a cost-state. Ours = messages inside the session's process windows.
    Per (session, model): gap = CC cost - our cost. Output-token shortfall where the model has partial
    records = subagent_partial_output; web-search fee shortfall, and on Haiku the rest = side calls;
    everything else = unexplained (signed).
    """
    procs = c.execute("SELECT session_id, start_time_ms, total_duration_ms, total_cost_usd, has_unknown_model_cost "
                      "FROM session_costs").fetchall()
    sess_cost = {r[0]: float(r[1] or 0) for r in c.execute(
        "SELECT session_id, SUM(cost_usd) FROM messages WHERE session_id IS NOT NULL GROUP BY session_id")}
    out: dict[str, dict] = {}
    if not procs:
        return {"rows": out, "session_cost": sess_cost}
    by_sid: dict[str, dict] = {}
    c.execute("CREATE TEMP TABLE IF NOT EXISTS _recon_w (session_id TEXT, a TEXT, b TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS temp._recon_w_sid ON _recon_w(session_id, a, b)")
    c.execute("DELETE FROM _recon_w")
    for sid, start, dur, total, unk in procs:
        p = by_sid.setdefault(sid, {"processes": 0, "cc_total": 0.0, "unknown": False})
        p["processes"] += 1
        p["cc_total"] += float(total or 0)
        p["unknown"] |= bool(unk)
        c.execute("INSERT INTO _recon_w VALUES (?,?,?)",
                  (sid, _ms_iso(int(start)), _ms_iso(int(start) + int(dur or 0) + _RECON_GRACE_MS)))
    cc: dict[tuple[str, str], dict] = {}
    for r in c.execute("SELECT session_id, model_key, SUM(cost_usd) AS cost, SUM(COALESCE(web_search_requests,0)) AS ws, "
                       "SUM(COALESCE(input_tokens,0)) AS i, SUM(COALESCE(output_tokens,0)) AS o, "
                       "SUM(COALESCE(cache_read,0)) AS cr, SUM(COALESCE(cache_creation,0)) AS cw "
                       "FROM session_cost_models GROUP BY session_id, model_key"):
        cc[(r["session_id"], r["model_key"] or "")] = dict(r)
    ours: dict[tuple[str, str], dict] = {}
    info: dict[str, dict] = {}
    try:
        partial_col = "SUM(CASE WHEN usage_final = 0 THEN 1 ELSE 0 END)"
        c.execute("SELECT usage_final FROM messages LIMIT 0")
    except sqlite3.OperationalError:  # pre-v6 DB
        partial_col = "0"
    for r in c.execute(
            f"""SELECT session_id, model, inwin, COALESCE(SUM(cost_usd),0) AS cost,
                       COALESCE(SUM(server_tool_cost_usd),0) AS tool, COALESCE(SUM(input_tokens),0) AS i,
                       COALESCE(SUM(output_tokens),0) AS o, COALESCE(SUM(cache_read),0) AS cr,
                       COALESCE(SUM(cache_5m_write),0) AS c5, COALESCE(SUM(cache_1h_write),0) AS c1,
                       {partial_col} AS partial, MAX(account) AS account, MAX(project) AS project,
                       MIN(ts) AS first_ts, MAX(ts) AS last_ts
                FROM (SELECT m.session_id, m.model, m.ts, m.cost_usd, m.server_tool_cost_usd, m.input_tokens,
                             m.output_tokens, m.cache_read, m.cache_5m_write, m.cache_1h_write, m.account, m.project,
                             {"m.usage_final" if partial_col != "0" else "1 AS usage_final"},
                             EXISTS(SELECT 1 FROM _recon_w w WHERE w.session_id = m.session_id
                                    AND w.a <= m.ts AND w.b >= m.ts) AS inwin
                      FROM messages m WHERE m.session_id IN (SELECT DISTINCT session_id FROM _recon_w))
                GROUP BY session_id, model, inwin"""):
        sid = r["session_id"]
        i = info.setdefault(sid, {"account": None, "project": None, "first_ts": None, "last_ts": None, "outwin": 0.0})
        i["account"] = max(filter(None, (i["account"], r["account"])), default=None)
        i["project"] = max(filter(None, (i["project"], r["project"])), default=None)
        i["first_ts"] = min(filter(None, (i["first_ts"], r["first_ts"])), default=None)
        i["last_ts"] = max(filter(None, (i["last_ts"], r["last_ts"])), default=None)
        if not r["inwin"]:
            i["outwin"] += r["cost"]
            continue
        o = ours.setdefault((sid, pricing.model_key(r["model"] or "")),
                            {"cost": 0.0, "tool": 0.0, "i": 0, "o": 0, "cr": 0, "c5": 0, "c1": 0, "partial": 0})
        for k in o:
            o[k] += r[k] or 0
    fee = pricing.WEB_SEARCH_USD
    zero_o = {"cost": 0.0, "tool": 0.0, "i": 0, "o": 0, "cr": 0, "c5": 0, "c1": 0, "partial": 0}
    zero_c = {"cost": 0.0, "ws": 0, "i": 0, "o": 0, "cr": 0, "cw": 0}
    keys_by_sid: dict[str, set] = {}
    for sid, k in (*cc, *ours):
        keys_by_sid.setdefault(sid, set()).add(k)
    for sid, p in by_sid.items():
        models, tok_usd, tok_n = [], [0.0] * 4, [0] * 4
        s_side = s_ws = s_part = s_unexp = 0.0
        s_part_tok = s_partial = n_ws = 0
        our_total = 0.0
        for k in sorted(keys_by_sid.get(sid, ())):
            o, t = ours.get((sid, k), zero_o), cc.get((sid, k), zero_c)
            rate = pricing.rate_for(k)
            r = rate[0] if rate else None
            gap = float(t["cost"] or 0) - o["cost"]
            out_gap = int(t["o"]) - o["o"]
            part = out_gap * r[1] / 1e6 if (r and o["partial"] and out_gap > 0) else 0.0
            rest = gap - part
            # Claude Code's costUSD includes the search fee; transcripts report no searches.
            ws_usd = min(max(0.0, int(t["ws"]) * fee - o["tool"]), max(0.0, rest))
            rest -= ws_usd
            side_tok = max(0.0, rest) if pricing.family_of(k) == "haiku" else 0.0
            unexp = rest - side_tok
            models.append({"model": k, "our_cost_usd": round(o["cost"], 4), "cc_cost_usd": round(float(t["cost"] or 0), 4),
                           "gap_usd": round(gap, 4), "side_call_cost_usd": round(side_tok + ws_usd, 4),
                           "subagent_partial_output_usd": round(part, 4), "unexplained_gap_usd": round(unexp, 4),
                           "web_search_requests": int(t["ws"]), "partial_usage_msgs": int(o["partial"])})
            s_side += side_tok + ws_usd
            s_ws += ws_usd
            s_part += part
            s_part_tok += max(0, out_gap) if part else 0
            s_partial += int(o["partial"])
            s_unexp += unexp
            n_ws += int(t["ws"])
            our_total += o["cost"]
            if r:  # diagnostic, signed; cache writes at our observed 5m/1h split (5m when we have none)
                cw_rate = (r[2] * o["c5"] + r[3] * o["c1"]) / (o["c5"] + o["c1"]) if (o["c5"] + o["c1"]) else r[2]
                for j, (d, rt) in enumerate(((int(t["i"]) - o["i"], r[0]), (out_gap, r[1]),
                                             (int(t["cr"]) - o["cr"], r[4]), (int(t["cw"]) - o["c5"] - o["c1"], cw_rate))):
                    tok_n[j] += d
                    tok_usd[j] += d * rt / 1e6
        i = info.get(sid, {})
        models.sort(key=lambda m: -abs(m["gap_usd"]))
        out[sid] = {
            "session_id": sid, "account": i.get("account"), "project": i.get("project"),
            "first_ts": i.get("first_ts"), "last_ts": i.get("last_ts"),
            "our_cost_usd": round(our_total, 4), "cc_cost_usd": round(p["cc_total"], 4),
            "delta_usd": round(p["cc_total"] - our_total, 4),
            "side_call_cost_usd": round(s_side, 4), "side_call_web_search_usd": round(s_ws, 4),
            "web_search_requests": n_ws, "subagent_partial_output_usd": round(s_part, 4),
            "subagent_partial_output_tokens": s_part_tok, "partial_usage_msgs": s_partial,
            "unexplained_gap_usd": round(s_unexp, 4), "out_of_window_cost_usd": round(i.get("outwin", 0.0), 4),
            "processes": p["processes"], "has_unknown_model_cost": p["unknown"], "models": models,
            "_tok_usd": tok_usd, "_tok_n": tok_n,
        }
    return {"rows": out, "session_cost": sess_cost}


_RECON_CACHE: dict[str, Any] = {}
_RECON_LOCK = threading.Lock()  # the page fires ~15 requests at once; compute once, not per thread


def _recon_data() -> dict:
    """_recon_compute, memoised per data version (it does not depend on the window or filters)."""
    with _RECON_LOCK:
        v = data_version()
        with _CACHE_LOCK:
            hit = _RECON_CACHE.get("v") == v and _RECON_CACHE.get("data")
        if hit:
            return hit
        with _conn() as c:
            data = _recon_compute(c)
        with _CACHE_LOCK:
            _RECON_CACHE.update(v=v, data=data)
        return data


def _session_scope(frm: str, to: str, acct: str, proj: str) -> tuple[str, tuple]:
    frag, ap = _af(acct, proj)
    return (f"SELECT DISTINCT session_id FROM messages WHERE ts >= ? AND ts < ?{frag} AND session_id IS NOT NULL",
            (frm, to, *ap))


def q_reconciliation(frm: str, to: str, acct: str = "", proj: str = "",
                     session_ids: list[str] | None = None) -> dict:
    """Reconciliation of our transcript cost against Claude Code's own cost-state, for the sessions
    active in the window (or the given ids). Per-session figures cover the session's whole life."""
    totals = _recon_empty_totals()
    try:
        data = _recon_data()
        if session_ids is None:
            scope_sql, sp = _session_scope(frm, to, acct, proj)
            with _conn() as c:
                scope = [r[0] for r in c.execute(scope_sql, sp)]
        else:
            scope = list(dict.fromkeys(session_ids))
    except sqlite3.OperationalError:  # pre-v5 DB: side tables not created yet
        return {"totals": totals, "rows": []}
    rows = [data["rows"][s] for s in scope if s in data["rows"]]
    totals["total_sessions"] = len(scope)
    totals["our_cost_scope_usd"] = sum(data["session_cost"].get(s, 0.0) for s in scope)
    tok_usd, tok_n = [0.0] * 4, [0] * 4
    for r in rows:
        totals["our_cost_reconciled_usd"] += r["our_cost_usd"]
        totals["cc_cost_reconciled_usd"] += r["cc_cost_usd"]
        totals["side_call_cost_usd"] += r["side_call_cost_usd"]
        totals["side_call_web_search_usd"] += r["side_call_web_search_usd"]
        totals["web_search_requests"] += r["web_search_requests"]
        totals["subagent_partial_output_usd"] += r["subagent_partial_output_usd"]
        totals["subagent_partial_output_tokens"] += r["subagent_partial_output_tokens"]
        totals["partial_usage_msgs"] += r["partial_usage_msgs"]
        totals["unexplained_gap_usd"] += r["unexplained_gap_usd"]
        totals["out_of_window_cost_usd"] += r["out_of_window_cost_usd"]
        totals["sessions_with_unknown_model_cost"] += int(r["has_unknown_model_cost"])
        for j in range(4):
            tok_usd[j] += r["_tok_usd"][j]
            tok_n[j] += r["_tok_n"][j]
    totals["reconciled_sessions"] = len(rows)
    totals["cc_gap_usd"] = totals["cc_cost_reconciled_usd"] - totals["our_cost_reconciled_usd"]
    totals["side_call_token_cost_usd"] = totals["side_call_cost_usd"] - totals["side_call_web_search_usd"]
    # where the gap sits, signed and priced at list rate (diagnostic; cache writes at our 5m/1h split)
    totals["gap_by_token_type_usd"] = {n: round(v, 2) for n, v in zip(_TOK, tok_usd)}
    totals["gap_tokens"] = dict(zip(_TOK, tok_n))
    if scope:
        totals["coverage_pct"] = round(100.0 * len(rows) / len(scope), 1)
    if totals["our_cost_scope_usd"]:
        totals["cost_coverage_pct"] = round(
            100.0 * (totals["our_cost_reconciled_usd"] + totals["out_of_window_cost_usd"]) / totals["our_cost_scope_usd"], 1)
    for k in ("our_cost_reconciled_usd", "cc_cost_reconciled_usd", "cc_gap_usd", "side_call_cost_usd",
              "side_call_token_cost_usd", "side_call_web_search_usd", "subagent_partial_output_usd",
              "unexplained_gap_usd", "out_of_window_cost_usd", "our_cost_scope_usd"):
        totals[k] = round(totals[k], 2)
    return {"totals": totals, "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]}


# ---------- v5: breakdowns, reliability, sessions ----------

# Rows indexed before v5 whose transcript is no longer on disk could not be re-parsed, so their
# attribution is unknown (every on-disk record carries `version`, so cc_version IS NULL marks them).
GONE = "(transcript gone)"
_V5 = "CASE WHEN cc_version IS NULL THEN '" + GONE + "' ELSE {} END"
BREAKDOWN_DIMS = {
    "agent": _V5.format("COALESCE(attribution_agent, CASE WHEN is_sidechain=1 THEN '(subagent, unattributed)' "
                        "ELSE '(main thread)' END)"),
    "sidechain": _V5.format("CASE WHEN is_sidechain=1 THEN 'subagent' ELSE 'main' END"),
    "skill": _V5.format("COALESCE(attribution_skill, '(none)')"),
    "mcp": _V5.format("COALESCE(attribution_mcp_server, '(none)')"),
    "plugin": _V5.format("COALESCE(attribution_plugin, '(none)')"),
    "branch": _V5.format("COALESCE(git_branch, '(none)')"),
    "entrypoint": _V5.format("COALESCE(entrypoint, '(none)')"),
    "effort": _V5.format("COALESCE(effort, '(unset)')"),
    "model_speed": "COALESCE(model,'?') || ' · ' || COALESCE(speed,'standard')",
    "pr": "COALESCE(p.repo || '#' || p.pr_number, '(no PR)')",
}


_BREAKDOWN_AGGS = """COUNT(*) AS msgs,
                   COUNT(DISTINCT session_id) AS sessions,
                   COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(thinking_tokens),0) AS thinking_tokens,
                   COALESCE(SUM(cost_usd),0) AS cost,
                   """ + _MOD
_BREAKDOWN_COLS = ("msgs", "tokens", "input_tokens", "output_tokens", "thinking_tokens", "cost", "fast_cost_usd",
                   "fast_premium_usd", "fast_msgs", "web_search_fees_usd", "web_search_requests", "estimated_msgs",
                   "unpriced_msgs", "internal_msgs", "usage_estimated_msgs", "estimated_cost_usd")


# ---------- spend by token type ----------
# A message stores one cost, so each token type's part is rebuilt from the rate card: within a
# (model, speed, region) group, the cost net of web-search fees is split by tokens x rate. The
# parts add back up to the recorded cost, fast-mode and regional modifiers included.
TOKEN_TYPES = (("input", "input_tokens", 0), ("output", "output_tokens", 1),
               ("cache_5m_write", "cache_5m_write", 2), ("cache_1h_write", "cache_1h_write", 3),
               ("cache_read", "cache_read", 4))
WEB_SEARCH_TYPE = "web_search"
_TT_GROUP_SQL = """{bucket}model,
                   COALESCE(SUM(cost_usd),0)              AS cost,
                   COALESCE(SUM(server_tool_cost_usd),0)  AS web_search_fees_usd,
                   COALESCE(SUM(CASE WHEN speed='fast' THEN cost_usd ELSE 0 END),0) AS fast_cost_usd,
                   COALESCE(SUM(fast_premium_usd),0)      AS fast_premium_usd,
                   """ + ",\n                   ".join(f"COALESCE(SUM({col}),0) AS {col}" for _, col, _ in TOKEN_TYPES)


def _tt_groups(c: sqlite3.Connection, frm: str, to: str, frag: str, ap: list, bucket_sql: str = "") -> list:
    b = f"{bucket_sql} AS bucket, " if bucket_sql else ""
    return c.execute(
        f"SELECT {_TT_GROUP_SQL.format(bucket=b)} FROM messages WHERE ts >= ? AND ts < ?{frag} "
        f"GROUP BY {'bucket, ' if bucket_sql else ''}model, COALESCE(speed,'standard'), COALESCE(inference_geo,'')",
        (frm, to, *ap)).fetchall()


def _split_token_types(r) -> dict[str, dict[str, float]]:
    """One group's cost -> {type: {cost, fast_cost_usd, fast_premium_usd}}, web search fees as their own type."""
    tool = float(r["web_search_fees_usd"] or 0)
    cost = float(r["cost"] or 0)
    priced = cost - tool
    rate = pricing.resolve(r["model"] or "")
    weights = [(r[col] or 0) * (rate[0][i] if rate else 0.0) for _, col, i in TOKEN_TYPES]
    if sum(weights) <= 0:  # no rate any more but a stored cost: fall back to token counts
        weights = [float(r[col] or 0) for _, col, _ in TOKEN_TYPES]
    tot = sum(weights)
    fast_share = float(r["fast_cost_usd"] or 0) / cost if cost else 0.0
    prem_share = float(r["fast_premium_usd"] or 0) / priced if priced else 0.0
    out = {}
    for (name, _, _), w in zip(TOKEN_TYPES, weights):
        part = priced * w / tot if tot > 0 else 0.0
        out[name] = {"cost": part, "fast_cost_usd": part * fast_share, "fast_premium_usd": part * prem_share}
    out[WEB_SEARCH_TYPE] = {"cost": tool, "fast_cost_usd": tool * fast_share, "fast_premium_usd": 0.0}
    return out


def _breakdown_token_type(c: sqlite3.Connection, frm: str, to: str, frag: str, ap: list) -> list[dict]:
    sums = {n: {"cost": 0.0, "fast_cost_usd": 0.0, "fast_premium_usd": 0.0}
            for n in [t[0] for t in TOKEN_TYPES] + [WEB_SEARCH_TYPE]}
    for g in _tt_groups(c, frm, to, frag, ap):
        for n, part in _split_token_types(g).items():
            for k, v in part.items():
                sums[n][k] += v
    cols = [(n, col) for n, col, _ in TOKEN_TYPES] + [(WEB_SEARCH_TYPE, "web_search_requests")]
    sel = ",\n               ".join(
        f"COALESCE(SUM({col}),0) AS {n}_tokens, COUNT(CASE WHEN {col}>0 THEN 1 END) AS {n}_msgs, "
        f"COUNT(DISTINCT CASE WHEN {col}>0 THEN session_id END) AS {n}_sessions, "
        f"COUNT(CASE WHEN {col}>0 AND speed='fast' THEN 1 END) AS {n}_fast_msgs, "
        f"COUNT(CASE WHEN {col}>0 AND price_estimated=1 THEN 1 END) AS {n}_estimated_msgs" for n, col in cols)
    cnt = c.execute(f"SELECT {sel}, COALESCE(SUM(thinking_tokens),0) AS thinking_tokens "
                    f"FROM messages WHERE ts >= ? AND ts < ?{frag}", (frm, to, *ap)).fetchone()
    rows = []
    for n, _ in cols:
        s = sums[n]
        if s["cost"] <= 0 and not cnt[f"{n}_msgs"]:
            continue
        rows.append({"value": n, "cost": s["cost"], "fast_cost_usd": s["fast_cost_usd"],
                     "fast_premium_usd": s["fast_premium_usd"], "fast_msgs": cnt[f"{n}_fast_msgs"],
                     # web search has requests, not tokens
                     "tokens": 0 if n == WEB_SEARCH_TYPE else cnt[f"{n}_tokens"],
                     "web_search_requests": cnt[f"{n}_tokens"] if n == WEB_SEARCH_TYPE else 0,
                     "msgs": cnt[f"{n}_msgs"], "sessions": cnt[f"{n}_sessions"],
                     "estimated_msgs": cnt[f"{n}_estimated_msgs"],
                     "thinking_tokens": cnt["thinking_tokens"] if n == "output" else 0})
    return sorted(rows, key=lambda r: -r["cost"])


def q_breakdown(frm: str, to: str, dim: str, limit: int = 50, acct: str = "", proj: str = "") -> dict:
    with _without(dim=dim if dim in DIM_FILTERS else None):  # keep every row of the dim being broken down
        frag, ap = _af(acct, proj)
    with _conn() as c:
        if dim == "token_type":
            out = _breakdown_token_type(c, frm, to, frag, ap)
            tot = c.execute(f"SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM messages WHERE ts >= ? AND ts < ?"
                            f"{frag}", (frm, to, *ap)).fetchone()
            # every message carries several token types, so msgs and sessions overlap across rows
            return {"dim": dim, "total_cost": tot[0], "total_msgs": tot[1], "overlapping": True,
                    "truncated": False, "rows": out}
        if dim == "pr":
            # A session linked to several PRs counts toward each of them. Roll messages up per session
            # first (covering index), then join the few PR links: joining 285K rows to pr_links took ~1.7s.
            sums = ",\n                   ".join(f"SUM(s.{k}) AS {k}" for k in _BREAKDOWN_COLS)
            sql = f"""
            WITH s AS (SELECT session_id, {_BREAKDOWN_AGGS} FROM messages
                       WHERE ts >= ? AND ts < ?{frag} GROUP BY session_id)
            SELECT {BREAKDOWN_DIMS["pr"]} AS value, MAX(p.repo) AS repo, MAX(p.pr_url) AS pr_url,
                   COUNT(s.session_id) AS sessions,
                   {sums}
            FROM s LEFT JOIN (SELECT session_id, repo, pr_number, MAX(pr_url) AS pr_url FROM pr_links
                              GROUP BY session_id, repo, pr_number) p USING(session_id)
            GROUP BY value
            ORDER BY cost DESC, msgs DESC"""
        else:
            sql = f"""
            SELECT {BREAKDOWN_DIMS[dim]} AS value,
                   {_BREAKDOWN_AGGS}
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY value
            ORDER BY cost DESC, msgs DESC"""
        rows = c.execute(sql, (frm, to, *ap)).fetchall()
        tot = c.execute(f"SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM messages WHERE ts >= ? AND ts < ?"
                        f"{frag}", (frm, to, *ap)).fetchone()
    out = [dict(r) for r in rows]
    return {"dim": dim, "total_cost": tot[0], "total_msgs": tot[1], "overlapping": dim == "pr",
            "truncated": len(out) > limit, "rows": out[:limit]}


_EVENT_KINDS = ("rate_limit", "server_error", "retry", "quota", "other_error")


def q_reliability(frm: str, to: str, acct: str = "", proj: str = "") -> dict:
    frag, ap = _af(acct, proj, "api_events")
    cols = ", ".join(f"SUM(CASE WHEN kind='{k}' THEN 1 ELSE 0 END) AS {k}" for k in _EVENT_KINDS)
    try:
        with _conn() as c:
            rows = [dict(r) for r in c.execute(
                f"SELECT {_local_sql('day', frm, to)} AS day, {cols} FROM api_events WHERE ts >= ? AND ts < ?{frag} "
                f"GROUP BY day ORDER BY day", (frm, to, *ap))]
            detail = [dict(r) for r in c.execute(
                f"SELECT kind, detail, COUNT(*) AS n FROM api_events WHERE ts >= ? AND ts < ?{frag} "
                f"GROUP BY kind, detail ORDER BY n DESC LIMIT 50", (frm, to, *ap))]
    except sqlite3.OperationalError:
        rows, detail = [], []
    totals = {k: sum(r[k] or 0 for r in rows) for k in _EVENT_KINDS}
    return {"rows": rows, "totals": totals, "details": detail}


def q_sessions(frm: str, to: str, by: str = "cost", limit: int = 50, acct: str = "", proj: str = "") -> list[dict]:
    """Sessions in the window: title, model mix by cost, active time, cost, PRs, branch, side calls."""
    order = {"cost": "cost", "tokens": "tokens", "msgs": "msgs", "recent": "last_ts",
             "active": "COALESCE(active_ms, span_minutes * 60000.0)", "duration": "span_minutes"}.get(by, "cost")
    frag, ap = _af(acct, proj)
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            f"""
            WITH s AS (
              SELECT session_id, MAX(account) AS account, MAX(project) AS project, MAX(project_path) AS project_path,
                     COUNT(*) AS msgs,
                     COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
                     COALESCE(SUM(cost_usd),0) AS cost,
                     COALESCE(SUM(CASE WHEN is_sidechain=1 THEN cost_usd ELSE 0 END),0) AS subagent_cost,
                     MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                     CAST((julianday(MAX(ts)) - julianday(MIN(ts))) * 24 * 60 AS INTEGER) AS span_minutes
              FROM messages WHERE ts >= ? AND ts < ?{frag} AND session_id IS NOT NULL GROUP BY session_id)
            SELECT s.*, td.active_ms FROM s LEFT JOIN ({_ACTIVE_SQL}) td USING(session_id)
            ORDER BY {order} DESC LIMIT ?""",
            (frm, to, *ap, frm, to, limit))]
        ids = [r["session_id"] for r in rows]
        mix = _model_mix(c, ids, frm, to)
        titles: dict[str, dict] = {}
        prs: dict[str, list] = {}
        branches: dict[str, str] = {}
        for i in range(0, len(ids), 500):
            ch = ids[i:i + 500]
            q = _in_list(ch)
            try:
                for r in c.execute(f"SELECT session_id, source, title FROM session_titles WHERE session_id IN ({q})", ch):
                    titles.setdefault(r["session_id"], {})[r["source"]] = r["title"]
                for r in c.execute(f"SELECT session_id, pr_number, pr_url, repo FROM pr_links "
                                   f"WHERE session_id IN ({q}) ORDER BY ts", ch):
                    prs.setdefault(r["session_id"], []).append(
                        {"number": r["pr_number"], "url": r["pr_url"], "repo": r["repo"]})
            except sqlite3.OperationalError:
                pass
            for r in c.execute(f"SELECT session_id, git_branch, COUNT(*) AS n FROM messages WHERE session_id IN ({q}) "
                               f"AND git_branch IS NOT NULL GROUP BY session_id, git_branch ORDER BY n", ch):
                branches[r["session_id"]] = r["git_branch"]  # ascending, so the most used one is kept
    side = {r["session_id"]: r for r in q_reconciliation(frm, to, acct, proj, ids)["rows"]}
    for d in rows:
        sid = d["session_id"]
        t = titles.get(sid, {})
        d["title"] = t.get("custom") or t.get("ai")
        d["title_source"] = "custom" if t.get("custom") else ("ai" if t.get("ai") else None)
        m = mix.get(sid, [])
        d["top_model"] = m[0]["model"] if m else None
        d["model_mix"] = [{"model": x["model"], "cost": round(x["cost"], 4), "msgs": x["msgs"],
                           "share": round(x["cost"] / d["cost"], 4) if d["cost"] else None} for x in m]
        d["duration_minutes"] = d.pop("span_minutes")
        _active(d)
        d.pop("active_ms", None)
        d["prs"] = prs.get(sid, [])
        d["branch"] = branches.get(sid)
        sc = side.get(sid)
        for k in ("side_call_cost_usd", "subagent_partial_output_usd", "unexplained_gap_usd", "cc_cost_usd"):
            d[k] = sc[k] if sc else None
        d["reconciled"] = sc is not None
    return rows


# ---------- v5: accounts / billing paths ----------

def account_billing_map() -> dict[str, dict]:
    """account name -> billing path info (billing.account_billing) for every discovered account
    (Claude config dirs and the other agents' homes: ~/.codex, ~/.pi/agent)."""
    dirs: dict[str, list[tuple[int, Path]]] = {}
    tools: dict[str, str] = {}
    for a in discover_accounts():
        dirs.setdefault(a["name"], []).append((int(a.get("priority", 0)), Path(a["root"]).parent))
        tools.setdefault(a["name"], a.get("tool") or indexer.TOOL_CLAUDE)
    return {n: billing.account_billing(n, [p for _, p in sorted(v, key=lambda x: -x[0])], tool=tools[n])
            for n, v in dirs.items()}


def q_account_flags(c: sqlite3.Connection, frm: str | None = None, to: str | None = None,
                    proj: str = "") -> dict[str, dict]:
    """Per-account data-quality flags: cache tokens reported?, usage-less rows (estimated or not),
    unpriced vs internal-model rows, estimated-rate rows, failed requests.
    Over [frm, to) and the project filter when given, else all time."""
    out = {}
    where, params = "", ()
    if frm and to:
        where, params = " WHERE ts >= ? AND ts < ?", (frm, to)
        if proj:
            where, params = where + " AND project = ?", (*params, proj)
        ff, fp = _filter_frag()
        where, params = where + ff, (*params, *fp)
    for r in c.execute(
        "SELECT account, COUNT(*) AS msgs, "
        "SUM(CASE WHEN input_tokens > 0 THEN 1 ELSE 0 END) AS with_input, "
        "SUM(CASE WHEN COALESCE(cache_read,0)+COALESCE(cache_5m_write,0)+COALESCE(cache_1h_write,0) > 0 "
        "    THEN 1 ELSE 0 END) AS with_cache, "
        "SUM(CASE WHEN COALESCE(usage_reported,1)=0 AND COALESCE(usage_estimated,0)=0 THEN 1 ELSE 0 END)"
        "    AS usage_unreported, "
        "SUM(COALESCE(usage_estimated,0)) AS usage_estimated, COALESCE(SUM(est_cost_usd),0) AS est_cost, "
        "SUM(COALESCE(price_estimated,0)) AS estimated, COALESCE(SUM(cost_usd),0) AS api_cost "
        f"FROM messages{where} GROUP BY account", params
    ):
        with_input = r["with_input"] or 0
        out[r["account"]] = {
            # None = too few rows to judge; False = the account's client never reports cache tokens,
            # so the API-equivalent figure prices every cached prompt token at the full input rate.
            "cache_tokens_reported": (None if with_input < 20 else bool(r["with_cache"])),
            "cache_msgs": r["with_cache"] or 0,
            # no usage reported AND no estimate (a gap that is still a real understatement)
            "usage_unreported_msgs": r["usage_unreported"] or 0,
            # no usage reported, estimated (indexer.estimate_usage); cost NOT in api_cost
            "usage_estimated_msgs": r["usage_estimated"] or 0,
            "estimated_cost_usd": round(r["est_cost"] or 0.0, 2),
            "unpriced_msgs": 0, "unpriced_models": [], "internal_msgs": 0, "internal_models": [],
            "estimated_msgs": r["estimated"] or 0,
            "failed_msgs": 0,
            "msgs": r["msgs"],
            "api_cost": round(r["api_cost"], 2),
        }
    # unpriced vs internal: an internal model (pricing.INTERNAL_MODELS) has no public price to miss
    tw = where + (" AND" if where else " WHERE") + " tier IS NULL"
    for r in c.execute(f"SELECT account, model, COUNT(*) AS n FROM messages{tw} GROUP BY account, model "
                       "ORDER BY n DESC", params):
        f = out.get(r["account"])
        if f is None:
            continue
        kind = "internal" if pricing.is_internal(r["model"] or "") else "unpriced"
        f[f"{kind}_msgs"] += r["n"]
        f[f"{kind}_models"].append(r["model"])
    for acct, n in _failed_counts(c, frm, to, proj).items():
        if acct in out:
            out[acct]["failed_msgs"] = n
        else:  # an account whose only rows are failed requests
            out[acct] = {"cache_tokens_reported": None, "cache_msgs": 0, "usage_unreported_msgs": 0,
                         "usage_estimated_msgs": 0, "estimated_cost_usd": 0.0, "unpriced_msgs": 0,
                         "unpriced_models": [], "internal_msgs": 0, "internal_models": [], "estimated_msgs": 0,
                         "failed_msgs": n, "msgs": 0, "api_cost": 0.0}
    return out


def _failed_counts(c: sqlite3.Connection, frm: str | None, to: str | None, proj: str = "") -> dict[str, int]:
    """Failed/aborted requests that reported no usage (indexer: failed_requests), per account.
    Follows the window, project and tool/model filters; a breakdown-dimension filter matches none."""
    where, params = " WHERE 1", []
    if frm and to:
        where += " AND ts >= ? AND ts < ?"
        params += [frm, to]
        if proj:
            where += " AND project = ?"
            params.append(proj)
        f = _FILTERS.get() or {}
        if f.get("tools"):
            where += f" AND tool IN ({_in_list(f['tools'])})"
            params += f["tools"]
        if f.get("models"):
            want = list(dict.fromkeys([*f["models"], *_raw_models(f["models"])]))
            where += f" AND model IN ({_in_list(want)})"
            params += want
        if f.get("dims"):
            where += " AND 0"
    try:
        return {a: n for a, n in c.execute(
            f"SELECT account, COUNT(*) FROM failed_requests{where} GROUP BY account", params)}
    except sqlite3.OperationalError:  # DB indexed before v8
        return {}


# CLIProxyAPI (a local gateway in front of ChatGPT/Codex) can drop usage from the responses it
# streams. Its own per-request usage record is off unless usage-statistics-enabled is true. Only the
# binary's presence and two non-secret config keys (port, usage-statistics-enabled) are read.
_CLIPROXY_CONFIGS = ("/opt/homebrew/etc/cliproxyapi.conf", "/usr/local/etc/cliproxyapi.conf",
                     "~/.cli-proxy-api/config.yaml", "~/.cliproxyapi/config.yaml")
_CLIPROXY_KEY = re.compile(r"^(port|usage-statistics-enabled)\s*:\s*([^#\s]+)")


def _cliproxy_install() -> dict | None:
    import shutil
    binary = shutil.which("cliproxyapi") or next(
        (b for b in ("/opt/homebrew/bin/cliproxyapi", "/usr/local/bin/cliproxyapi") if Path(b).exists()), None)
    conf = next((Path(p).expanduser() for p in _CLIPROXY_CONFIGS if Path(p).expanduser().is_file()), None)
    if not binary and not conf:
        return None
    keys: dict[str, str] = {}
    if conf:
        try:
            with open(conf, encoding="utf-8", errors="replace") as fh:
                for line in fh:  # top-level keys only; every other line is discarded unread
                    m = _CLIPROXY_KEY.match(line)
                    if m:
                        keys[m.group(1)] = m.group(2).strip("'\"").lower()
        except OSError:
            pass
    enabled = {"true": True, "false": False}.get(keys.get("usage-statistics-enabled", ""))
    return {"binary": binary, "config_path": str(conf) if conf else None, "port": keys.get("port"),
            "usage_statistics_enabled": enabled}


def _gateway_hint(info: dict, flags: dict) -> tuple[str | None, dict | None]:
    """(hint, proxy facts) for a gateway account whose host is a local CLIProxyAPI."""
    if info.get("billing_mode") != "gateway":
        return None, None
    host = (info.get("gateway_host") or "").lower()
    hostname, _, port = host.rpartition(":") if host.count(":") == 1 else (host, "", "")
    inst = _cliproxy_install()
    if info.get("provider") != "cliproxyapi":
        if not (inst and hostname in billing.LOOPBACK and (not inst.get("port") or inst["port"] == port)):
            return None, None
    proxy = {"kind": "cliproxyapi", **(inst or {})}
    if proxy.get("usage_statistics_enabled"):
        return None, proxy
    conf = proxy.get("config_path") or "the CLIProxyAPI config"
    gap = (flags.get("usage_estimated_msgs") or 0) + (flags.get("usage_unreported_msgs") or 0)
    hint = (f"Enable usage-statistics-enabled: true in {conf} to get usage for every request"
            + (f"; {gap:,} past messages had none and stay estimated" if gap else ""))
    return hint, proxy


# ---------- extras queries ----------

def _extras_table_present() -> bool:
    if not DB_PATH.exists():
        return False
    with _conn() as c:
        r = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
        ).fetchone()
    return r is not None


def q_extras_skills(frm: str, to: str, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj, "tool_calls")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT skill, COUNT(*) AS uses, MAX(ts) AS last_ts
            FROM tool_calls
            WHERE ts >= ? AND ts < ?{frag} AND skill IS NOT NULL AND skill <> ''
            GROUP BY skill
            ORDER BY uses ASC, last_ts ASC
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_extras_tools(frm: str, to: str, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj, "tool_calls")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT tool_name, COUNT(*) AS uses,
                   SUM(is_error) AS errors
            FROM tool_calls
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY tool_name
            ORDER BY uses DESC
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_extras_mcp(frm: str, to: str, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj, "tool_calls")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT mcp_server, COUNT(*) AS uses
            FROM tool_calls
            WHERE ts >= ? AND ts < ?{frag} AND mcp_server IS NOT NULL
            GROUP BY mcp_server
            ORDER BY uses DESC
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_extras_agents(frm: str, to: str, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj, "tool_calls")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT subagent_type, COUNT(*) AS uses
            FROM tool_calls
            WHERE ts >= ? AND ts < ?{frag} AND subagent_type IS NOT NULL
            GROUP BY subagent_type
            ORDER BY uses DESC
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_extras_slash(frm: str, to: str, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj, "slash_prompts")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT command, COUNT(*) AS uses, MAX(ts) AS last_ts
            FROM slash_prompts
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY command
            ORDER BY uses DESC
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_extras_files(frm: str, to: str, limit: int, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj, "tool_calls")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT target_path AS path, COUNT(*) AS edits,
                   SUM(CASE WHEN tool_name='Edit' THEN 1 ELSE 0 END) AS edit_calls,
                   SUM(CASE WHEN tool_name='Write' THEN 1 ELSE 0 END) AS write_calls,
                   SUM(CASE WHEN tool_name='Read' THEN 1 ELSE 0 END) AS read_calls
            FROM tool_calls
            WHERE ts >= ? AND ts < ?{frag} AND target_path IS NOT NULL
              AND tool_name IN ('Edit','Write','Read','NotebookEdit')
            GROUP BY target_path
            ORDER BY edits DESC
            LIMIT ?
            """,
            (frm, to, *ap, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def q_extras_bash(frm: str, to: str, limit: int, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj, "tool_calls")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT command_verb AS verb, COUNT(*) AS uses
            FROM tool_calls
            WHERE ts >= ? AND ts < ?{frag} AND tool_name='Bash' AND command_verb IS NOT NULL
            GROUP BY command_verb
            ORDER BY uses DESC
            LIMIT ?
            """,
            (frm, to, *ap, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def q_extras_errors(frm: str, to: str, acct: str = "", proj: str = "") -> list[dict]:
    frag, ap = _af(acct, proj, "tool_calls")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT tool_name,
                   COUNT(*) AS total,
                   SUM(is_error) AS errors,
                   ROUND(100.0 * SUM(is_error) / COUNT(*), 1) AS error_pct
            FROM tool_calls
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY tool_name
            HAVING SUM(is_error) > 0
            ORDER BY errors DESC
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_extras_calls(
    frm: str,
    to: str,
    tool: str | None,
    status: str | None,
    limit: int,
    offset: int,
    acct: str = "",
    proj: str = "",
) -> dict:
    """Individual tool calls, newest first, with total for pagination."""
    where = ["ts >= ?", "ts < ?"]
    params: list[Any] = [frm, to]
    if acct:
        where.append("account = ?")
        params.append(acct)
    if proj:
        where.append("project = ?")
        params.append(proj)
    if tool:
        where.append("tool_name = ?")
        params.append(tool)
    if status == "errors":
        where.append("is_error = 1")
    ff, fp = _filter_frag("tool_calls")
    cond = " AND ".join(where) + ff
    params += list(fp)
    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) FROM tool_calls WHERE {cond}", params
        ).fetchone()[0]
        rows = c.execute(
            f"""
            SELECT ts, tool_name, mcp_server, skill, subagent_type,
                   target_path, command_verb, project, session_id, is_error
            FROM tool_calls
            WHERE {cond}
            ORDER BY ts DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


def q_extras_overview(frm: str, to: str, acct: str = "", proj: str = "") -> dict:
    """Single-roundtrip aggregate for the Activity tab."""
    frag, ap = _af(acct, proj, "tool_calls")
    sfrag, sap = _af(acct, proj, "slash_prompts")
    with _conn() as c:
        # totals + cardinality
        tot = c.execute(
            f"""
            SELECT
              COUNT(*)                              AS tool_calls,
              COUNT(DISTINCT tool_name)             AS distinct_tools,
              COUNT(DISTINCT skill)                 AS distinct_skills,
              COUNT(DISTINCT subagent_type)         AS distinct_agents,
              COUNT(DISTINCT mcp_server)            AS distinct_mcp,
              SUM(CASE WHEN is_error=1 THEN 1 ELSE 0 END) AS errors
            FROM tool_calls WHERE ts >= ? AND ts < ?{frag}
            """,
            (frm, to, *ap),
        ).fetchone()
        sp = c.execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT command) AS distinct_commands "
            f"FROM slash_prompts WHERE ts >= ? AND ts < ?{sfrag}",
            (frm, to, *sap),
        ).fetchone()
    d = dict(tot) if tot else {}
    d["slash_prompts"] = sp["n"] if sp else 0
    d["distinct_slash_commands"] = sp["distinct_commands"] if sp else 0
    return d


# ---------- app ----------

app = FastAPI(title="Claude Usage Dashboard")
# WHY: local decks (file:// or another localhost port) read /api/* live; the server binds 127.0.0.1 only.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))


def _asset_version() -> str:
    """Max mtime of static assets; cache-busts ?v= URLs after an update."""
    try:
        return str(max(int(p.stat().st_mtime) for p in (ROOT / "static").iterdir()))
    except (ValueError, OSError):
        return "0"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"v": _asset_version()})


@app.get("/api/summary")
@cached
def api_summary(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "from": f, "to": t, **q_summary(f, t, _acct(request), _proj(request))}


@app.get("/api/timeseries")
@cached
def api_timeseries(request: Request):
    f, t, label = _qparams(request)
    bucket = request.query_params.get("bucket", "day")
    by = request.query_params.get("by")
    if by is None:  # unchanged response shape
        return {"window": label, "bucket": bucket, "rows": q_timeseries(f, t, bucket, _acct(request), _proj(request))}
    if by not in TIMESERIES_BY:
        return JSONResponse({"error": "bad_by", "by": list(TIMESERIES_BY)}, status_code=400)
    return {"window": label, "bucket": bucket, "by": by,
            "rows": q_timeseries(f, t, bucket, _acct(request), _proj(request), by=by)}


@app.get("/api/by_model")
@cached
def api_by_model(request: Request):
    """Unfiltered by model= (the UI highlights the selected model here); every other filter applies."""
    f, t, label = _qparams(request)
    with _without(models=True):
        return {"window": label, "rows": q_by_model(f, t, _acct(request), _proj(request))}


@app.get("/api/by_account")
@cached
def api_by_account(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_by_account(f, t, _proj(request))}


@app.get("/api/by_project")
@cached
def api_by_project(request: Request):
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "20"))
    return {"window": label, "rows": q_by_project(f, t, limit, _acct(request), _proj(request))}


@app.get("/api/heatmap")
@cached
def api_heatmap(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_heatmap(f, t, _acct(request), _proj(request))}


@app.get("/api/distributions")
@cached
def api_distributions(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, **q_distributions(f, t, _acct(request), _proj(request))}


@app.get("/api/top_sessions")
@cached
def api_top_sessions(request: Request):
    f, t, label = _qparams(request)
    by = request.query_params.get("by", "tokens")
    limit = int(request.query_params.get("limit", "20"))
    return {"window": label, "by": by, "rows": q_top_sessions(f, t, by, limit, _acct(request), _proj(request))}


@app.get("/api/reconciliation")
@cached
def api_reconciliation(request: Request):
    f, t, label = _qparams(request)
    limit = max(1, min(int(request.query_params.get("limit", "100")), 5000))
    r = q_reconciliation(f, t, _acct(request), _proj(request))
    rows = sorted(r["rows"], key=lambda x: (-abs(x["delta_usd"]), -x["cc_cost_usd"]))
    return {"window": label, "from": f, "to": t, "totals": r["totals"], "note": _RECON_NOTE,
            "rows": rows[:limit]}


@app.get("/api/breakdown")
@cached
def api_breakdown(request: Request):
    f, t, label = _qparams(request)
    dim = (request.query_params.get("dim") or "agent").lower()
    if dim not in BREAKDOWN_DIMS and dim != "token_type":
        return JSONResponse({"error": "bad_dim", "dims": [*BREAKDOWN_DIMS, "token_type"]}, status_code=400)
    limit = max(1, min(int(request.query_params.get("limit", "50")), 1000))
    return {"window": label, **q_breakdown(f, t, dim, limit, _acct(request), _proj(request))}


@app.get("/api/reliability")
@cached
def api_reliability(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "timezone": tz_name(), **q_reliability(f, t, _acct(request), _proj(request))}


@app.get("/api/sessions")
@cached
def api_sessions(request: Request):
    f, t, label = _qparams(request)
    by = request.query_params.get("by", "cost")
    limit = max(1, min(int(request.query_params.get("limit", "50")), 1000))
    return {"window": label, "by": by, "rows": q_sessions(f, t, by, limit, _acct(request), _proj(request))}


@app.get("/api/top_days")
@cached
def api_top_days(request: Request):
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "10"))
    return {"window": label, "rows": q_top_days(f, t, limit, _acct(request), _proj(request))}


@app.get("/api/meta")
def api_meta():
    m = q_meta()
    m["extras_enabled"] = EXTRAS_ON
    m["watcher"] = WATCHER.status() if WATCHER else watcher_mod.DISABLED_STATUS
    return m


@app.get("/api/accounts")
@cached
def api_accounts(request: Request):
    """Accounts present in the DB (all time), with row counts and activity span.

    Data-quality flags (cache_tokens_reported, usage_unreported_msgs, usage_estimated_msgs,
    estimated_cost_usd, unpriced_msgs/_models, internal_msgs/_models, estimated_msgs, failed_msgs,
    window_msgs, window_api_cost) follow the window and project filter; the same flags over all
    time are under "all_time". usage_estimate: how usage-less messages were estimated and the
    hold-out validation of that estimate. gateway_hint: how to make a CLIProxyAPI gateway report
    usage (None when not applicable or already enabled)."""
    if not DB_PATH.exists():
        return {"accounts": []}
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT account AS name, COUNT(*) AS msgs, "
                "MIN(ts) AS first_ts, MAX(ts) AS last_ts "
                "FROM messages GROUP BY account ORDER BY msgs DESC"
            ).fetchall()
            tools_by_acct: dict[str, list[str]] = {}
            for r in c.execute("SELECT account, tool, COUNT(*) AS n FROM messages GROUP BY account, tool ORDER BY n DESC"):
                tools_by_acct.setdefault(r[0], []).append(r[1])
            all_time = q_account_flags(c)
            f, t, label = _qparams(request)
            flags = q_account_flags(c, f, t, _proj(request))
            try:
                est_stats = json.loads((c.execute("SELECT v FROM meta WHERE k='usage_estimate'").fetchone()
                                        or ["{}"])[0])
            except (sqlite3.OperationalError, ValueError):
                est_stats = {}
    except sqlite3.OperationalError:
        return {"accounts": []}
    infos = account_billing_map()
    _s, win_shares = _plan_shares(f, t, infos)
    usage = _usage_share(f, t, _proj(request))
    win_shares = _allocated(win_shares, usage)
    basis = "window" if usage is None else "allocated_by_usage"
    out = []
    for r in rows:
        d = dict(r)
        info = infos.get(d["name"]) or billing.account_billing(d["name"], [])
        d.update({k: info.get(k) for k in ("billing_mode", "payment", "payment_basis", "provider", "gateway_host",
                                           "signals", "detected", "markup_pct", "regional_premium_pct",
                                           "has_receipts", "paid_by", "plan_members", "payer")})
        d["payer"] = d.get("payer") or "self"
        _w = flags.get(d["name"]) or {}
        d["plan_value_usd"] = round(win_shares.get(d["name"], 0.0), 2)
        d["plan_cost_basis"] = basis
        _raw = float(_w.get("api_cost") or 0.0)
        d["you_paid_usd"] = 0.0 if d["payer"] == "employer" else billing.actual_cost(info, _raw, d["plan_value_usd"])
        tl = tools_by_acct.get(d["name"]) or [info.get("tool") or indexer.TOOL_CLAUDE]
        d["tool"] = info.get("tool") if info.get("tool") in tl else tl[0]  # the account's agent
        d["tools"] = tl
        w = flags.get(d["name"]) or {"cache_tokens_reported": None, "cache_msgs": 0, "usage_unreported_msgs": 0,
                                     "usage_estimated_msgs": 0, "estimated_cost_usd": 0.0, "unpriced_msgs": 0,
                                     "unpriced_models": [], "internal_msgs": 0, "internal_models": [],
                                     "estimated_msgs": 0, "failed_msgs": 0, "msgs": 0, "api_cost": 0.0}
        w = dict(w)
        d["window_msgs"], d["window_api_cost"] = w.pop("msgs"), w.pop("api_cost")
        d.update(w)
        d["all_time"] = all_time.get(d["name"], {})
        d["api_cost"] = d["all_time"].get("api_cost", 0.0)  # unchanged: all-time, like msgs/first_ts/last_ts
        d["api_cost_incl_estimates"] = round(d["api_cost"] + d["all_time"].get("estimated_cost_usd", 0.0), 2)
        d["usage_estimate"] = est_stats.get(d["name"])  # method, calibration and hold-out validation
        d["gateway_hint"], d["gateway_proxy"] = _gateway_hint(info, d["all_time"])
        out.append(d)
    return {"accounts": out, "window": label, "timezone": tz_name(), "tools": list(indexer.TOOLS)}


# ---------- profile manager (ccm) ----------

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def _run_ccm(*args: str) -> dict:
    """Run a ccm.py subcommand; return exit code and transcript."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "ccm.py"), *args],
        capture_output=True, text=True, timeout=60,
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": out.strip()}


@app.get("/api/profiles")
def api_profiles():
    """Every Claude config profile on this machine: sharing status per shared dir (ccm.sharing_report),
    the canonical account its rows are indexed under, usage, and shell alias lines.

    Profiles whose rows land in the same account (default ~/.claude is pre-split history indexed as
    "personal") get merged_into = the profile that shows the usage; their own usage is None, so it is
    never shown twice. other_tools lists the non-Claude agents (Codex CLI, Pi) with their usage."""
    usage: dict[str, dict] = {}
    if DB_PATH.exists():
        try:
            with _conn() as c:
                for r in c.execute(
                    "SELECT account, COUNT(*) AS msgs, "
                    "COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens, "
                    "COALESCE(SUM(cost_usd),0) AS cost, MAX(ts) AS last_ts "
                    "FROM messages GROUP BY account"
                ):
                    usage[r["account"]] = {"msgs": r["msgs"], "tokens": r["tokens"], "cost": round(r["cost"], 2),
                                           "last_ts": r["last_ts"]}
        except sqlite3.OperationalError:
            pass
    accts = discover_accounts()
    # ccm names profiles by directory (default, codex); the index tags rows by the canonical account
    # (personal, claudex) -- map by config dir. Highest priority dir of an account shows its usage.
    by_dir: dict[str, tuple[str, int]] = {}
    for a in accts:
        if (a.get("tool") or indexer.TOOL_CLAUDE) == indexer.TOOL_CLAUDE:
            by_dir[os.path.realpath(Path(a["root"]).parent)] = (a["name"], int(a.get("priority", 0)))
    layout = ccm.layout_report()
    profs = layout["profiles"]
    owner: dict[str, tuple[int, str]] = {}  # account -> (priority, profile name) of the profile showing usage
    for pr in profs:
        acct, prio = by_dir.get(os.path.realpath(pr["path"]), (pr["name"], 0))
        pr["account"] = acct
        pr["_prio"] = prio
        if acct not in owner or prio > owner[acct][0]:
            owner[acct] = (prio, pr["name"])
    for pr in profs:
        acct = pr["account"]
        main = owner[acct][1]
        pr["profile"] = pr["name"]
        pr["account_differs"] = acct != pr["name"]
        pr["display_name"] = pr["name"] if acct == pr["name"] else f"{pr['name']} \u2192 {acct}"
        pr["merged_into"] = main if main != pr["name"] else None
        pr["shares_account_with"] = sorted(q["name"] for q in profs if q["account"] == acct and q is not pr)
        pr["usage"] = usage.get(acct) if pr["merged_into"] is None else None
        al = pr["alias"]
        pr["alias_installed"] = al["installed"]
        pr["alias_installed_name"] = al.get("installed_name")
        al.update({sh: ccm.alias_lines(pr["name"], sh) for sh in ("zsh", "bash", "fish", "powershell")})
        pr.pop("_prio")
    infos = account_billing_map()
    other_tools = []
    for a in accts:
        tool = a.get("tool") or indexer.TOOL_CLAUDE
        if tool == indexer.TOOL_CLAUDE or any(o["account"] == a["name"] for o in other_tools):
            continue
        info = infos.get(a["name"]) or {}
        other_tools.append({"tool": tool, "account": a["name"], "path": str(Path(a["root"]).parent),
                            "billing_mode": info.get("billing_mode"), "paid_by": info.get("paid_by"),
                            "usage": usage.get(a["name"])})
    return {
        "profiles": profs,
        "other_tools": other_tools,
        "shared_root": layout["shared_root"],
        "shared_exists": layout["shared_exists"],
        "shared_sources": layout["shared_sources"],
        "shared_dirs": layout["shared_dirs"],
        "platform": layout["platform"],
        "shell": layout["shell"],
        "rc_file": layout["rc_file"],
    }


@app.post("/api/profiles/create")
def api_profiles_create(request: Request):
    name = (request.query_params.get("name") or "").strip()
    if not _PROFILE_NAME_RE.match(name) or name in ("default", "shared"):
        return JSONResponse({"ok": False, "output": f"invalid profile name: {name!r}"}, status_code=400)
    return _run_ccm("create", name)


@app.post("/api/profiles/link")
def api_profiles_link(request: Request):
    name = (request.query_params.get("name") or "").strip()
    if name != "default" and not _PROFILE_NAME_RE.match(name):
        return JSONResponse({"ok": False, "output": f"invalid profile name: {name!r}"}, status_code=400)
    return _run_ccm("link", name)


@app.post("/api/profiles/init_shared")
def api_profiles_init_shared(request: Request):
    frm = (request.query_params.get("from") or "").strip()
    if frm and frm != "default" and not _PROFILE_NAME_RE.match(frm):
        return JSONResponse({"ok": False, "output": f"invalid profile name: {frm!r}"}, status_code=400)
    args = ["init-shared"] + (["--from-profile", frm] if frm else [])
    return _run_ccm(*args)


# ---------- receipts (billing.json) ----------

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_BILLING_LOCK = threading.Lock()  # serialises billing.json writers (CRUD; the scraper merges under it too)


def _local_mutation_guard(request: Request) -> JSONResponse | None:
    """Mutations only from this machine and same-origin JSON: the server binds 127.0.0.1, the Host
    header must be loopback (DNS rebinding), and a JSON content type forces a CORS preflight that
    the GET-only CORS policy refuses for other origins."""
    client = request.client.host if request.client else "127.0.0.1"
    try:
        host = urllib.parse.urlsplit("//" + (request.headers.get("host") or "127.0.0.1")).hostname or ""
    except ValueError:
        host = ""
    if client not in ("127.0.0.1", "::1", "localhost", "testclient") or host not in _LOOPBACK_HOSTS:
        return JSONResponse({"ok": False, "error": "forbidden", "detail": "local requests only"}, status_code=403)
    return None


def _receipt_view(c: dict) -> dict:
    out = dict(c)
    out.setdefault("account", "personal")
    out.setdefault("provider", "anthropic")
    out.setdefault("source", "manual")
    return out


@app.get("/api/receipts")
def api_receipts(request: Request):
    """billing.json records (recurring ones unexpanded), optionally filtered by account/provider/source."""
    q = request.query_params
    rows = [_receipt_view(c) for c in billing.load_charges() if isinstance(c, dict)]
    for k in ("account", "provider", "source"):
        if q.get(k):
            rows = [r for r in rows if r.get(k) == q.get(k)]
    return {"receipts": rows, "providers": list(billing.PROVIDERS), "path": str(billing.BILLING_PATH)}


@app.post("/api/receipts")
async def api_receipts_upsert(request: Request):
    """Create or replace a manual receipt: {receipt_id?, start, end?, plan, amount_usd, account, provider,
    recurring?, note?}. end is required unless recurring (then it ends the recurrence). Scraped
    receipts are never overwritten. billing.json is backed up to billing.json.bak and written atomically."""
    bad = _local_mutation_guard(request)
    if bad:
        return bad
    if "application/json" not in (request.headers.get("content-type") or ""):
        return JSONResponse({"ok": False, "error": "content_type", "detail": "send application/json"}, status_code=415)
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)
    try:
        with _BILLING_LOCK:
            rec = billing.upsert_manual(body)
    except billing.ReceiptError as e:
        return JSONResponse({"ok": False, "error": "invalid", "detail": str(e)}, status_code=400)
    invalidate_cache()
    return {"ok": True, "receipt": rec}


@app.delete("/api/receipts")
def api_receipts_delete(request: Request):
    """Delete a manual receipt: ?receipt_id=..."""
    bad = _local_mutation_guard(request)
    if bad:
        return bad
    rid = (request.query_params.get("receipt_id") or "").strip()
    if not rid:
        return JSONResponse({"ok": False, "error": "missing_receipt_id"}, status_code=400)
    try:
        with _BILLING_LOCK:
            gone = billing.delete_manual(rid)
    except billing.ReceiptError as e:
        return JSONResponse({"ok": False, "error": "not_manual", "detail": str(e)}, status_code=409)
    if not gone:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    invalidate_cache()
    return {"ok": True, "deleted": rid}


@app.post("/refresh")
def refresh():
    # The lock is shared with the background watcher so the two never overlap.
    with watcher_mod.INDEX_LOCK:
        return _reindex_and_invalidate()


# ---------- extras endpoints ----------

def _extras_ready_resp() -> JSONResponse | None:
    """Return a JSONResponse if extras are unavailable, else None."""
    if not _extras_table_present():
        return JSONResponse(
            {"enabled": False, "reason": "extras_not_indexed",
             "hint": "start with --all to index tool calls and slash prompts"},
            status_code=200,
        )
    return None


@app.get("/api/extras/status")
def api_extras_status(request: Request):
    return {
        "enabled": _extras_table_present(),
        "extras_on": EXTRAS_ON,
        **q_meta(),
    }


@app.get("/api/extras/overview")
@cached
def api_extras_overview(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, **q_extras_overview(f, t, _acct(request), _proj(request))}


@app.get("/api/extras/skills")
@cached
def api_extras_skills(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_skills(f, t, _acct(request), _proj(request))}


@app.get("/api/extras/tools")
@cached
def api_extras_tools(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_tools(f, t, _acct(request), _proj(request))}


@app.get("/api/extras/mcp")
@cached
def api_extras_mcp(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_mcp(f, t, _acct(request), _proj(request))}


@app.get("/api/extras/agents")
@cached
def api_extras_agents(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_agents(f, t, _acct(request), _proj(request))}


@app.get("/api/extras/slash")
@cached
def api_extras_slash(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_slash(f, t, _acct(request), _proj(request))}


@app.get("/api/extras/files")
@cached
def api_extras_files(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "30"))
    return {"window": label, "rows": q_extras_files(f, t, limit, _acct(request), _proj(request))}


@app.get("/api/extras/bash")
@cached
def api_extras_bash(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "30"))
    return {"window": label, "rows": q_extras_bash(f, t, limit, _acct(request), _proj(request))}


@app.get("/api/extras/errors")
@cached
def api_extras_errors(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_errors(f, t, _acct(request), _proj(request))}


@app.get("/api/extras/calls")
@cached
def api_extras_calls(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    q = request.query_params
    tool = q.get("tool") or None
    status = q.get("status") or None
    limit = max(1, min(int(q.get("limit", "50")), 500))
    offset = max(0, int(q.get("offset", "0")))
    return {
        "window": label, "limit": limit, "offset": offset,
        **q_extras_calls(f, t, tool, status, limit, offset, _acct(request), _proj(request)),
    }


@app.get("/api/gmail/status")
def api_gmail_status():
    return gmail_scraper.status()


@app.post("/api/gmail/scrape")
def api_gmail_scrape():
    try:
        with _BILLING_LOCK:
            out = gmail_scraper.scrape_and_save()
        invalidate_cache()
        return out
    except FileNotFoundError as e:
        return JSONResponse({"error": "missing_credentials", "detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "scrape_failed", "detail": str(e)}, status_code=500)


def _open_browser_later():
    threading.Timer(0.8, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()


def _start_watcher():
    """Daemon thread that reindexes when new Claude activity shows up."""
    global WATCHER
    WATCHER = watcher_mod.Watcher(
        _reindex_and_invalidate,
        discover_accounts,
    ).start()
    print(
        f"[startup] auto-refresh on — checking every "
        f"{int(WATCHER.base_interval)}s, backing off to "
        f"{int(WATCHER.max_interval)}s when idle (--no-watch to disable)"
    )


def main():
    global EXTRAS_ON
    EXTRAS_ON = "--all" in sys.argv
    if EXTRAS_ON:
        print("[startup] extras mode ON — indexing tool calls and slash prompts")
    print(f"[startup] indexing logs (incremental)...")
    summary = reindex(DB_PATH, extras=EXTRAS_ON)
    threading.Thread(target=_prewarm, daemon=True).start()
    print(f"[startup] ready. {summary['rows_total']} rows in DB.")
    if EXTRAS_ON:
        print(f"[startup] extras: {summary.get('extras_rows_total', 0)} tool calls indexed")
    if "--no-watch" not in sys.argv:
        _start_watcher()
    print(f"[startup] open http://{HOST}:{PORT}")
    if "--no-browser" not in sys.argv:
        _open_browser_later()
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
