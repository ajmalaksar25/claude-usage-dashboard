"""Subscription cost over an arbitrary [from_ts, to_ts] window.

billing.json is a list of charge records. Each record has:
  receipt_id (str, unique), start (YYYY-MM-DD), end (YYYY-MM-DD),
  plan (str), amount_usd (float, excl tax), source ("manual" | "gmail"),
  account (for example "personal", "work", or "claudex"; default "personal").

A record may instead set "recurring": true with only a start date; it is
expanded at read time into one charge per billing month from start through
today (used for the work plan, whose receipts go to the work inbox and
can't be scraped here). An optional "end" on a recurring record stops the
expansion: no period starts on or after it (a cancellation date).

v7 fields: provider (who was paid: "anthropic" default, "openai_chatgpt",
"openai_api", ...), plus optional store/merchant/currency/amount_original/
vat_original from the receipt scrapers. An account can be mapped to a
subscription provider in config.json ({"accounts": {"claudex": {"paid_by":
"openai_chatgpt"}}}); every account paid by the same provider forms a plan
group whose receipts are shared by usage (see plan_receipts). openai_api
receipts are never attributed to Codex/ChatGPT plan accounts.

subscription_cost() prorates each charge by the fraction of its day-span
that intersects the window. merge_scraped() lets the Gmail scraper
upsert charges by receipt_id without disturbing manual rows. Plan names are
free text, so a gateway account can carry e.g. a "ChatGPT Pro" receipt.

Billing paths (how each account is actually paid) are detected per Claude config
dir from settings.json `env` -- only the PRESENCE of variables is checked; secret
values are never read or logged (ANTHROPIC_BASE_URL is parsed for host:port only):

  subscription  Claude plan via OAuth: no API key, no base URL. List price is a
                "would have cost"; actually paid = billing.json receipts.
  api           ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / apiKeyHelper, no base URL.
                List price is the actual cost.
  cloud         CLAUDE_CODE_USE_BEDROCK / _VERTEX / _FOUNDRY. List price, plus an
                optional regional-endpoint premium (Bedrock/Vertex regional: +10%).
  gateway       ANTHROPIC_BASE_URL on a non-Anthropic host (CLIProxyAPI, OpenRouter,
                LiteLLM proxy...). Models may come from any provider; "would have
                cost" = that provider's list price (pricing.py, via LiteLLM).
                Actually paid = receipts for the account (payment "subscription"),
                or list price x (1 + markup_pct/100) for pay-per-token routers
                (payment "per_token"), else unknown.

Overrides live in config.json next to this file (or $CLAUDE_USAGE_CONFIG):
  {"timezone": "Asia/Kolkata",
   "accounts": {"claudex": {"billing_mode": "gateway", "payment": "subscription",
                            "markup_pct": 5.5, "regional_premium_pct": 10}}}
"""
from __future__ import annotations

import calendar
import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

BILLING_PATH = Path(__file__).parent / "billing.json"
CONFIG_PATH = Path(__file__).parent / "config.json"


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


def _add_months(d: date, n: int, anchor_day: int | None = None) -> date:
    """d + n months on `anchor_day` (default d.day), clamped to the target month's length."""
    k = d.month - 1 + n
    y, m = d.year + k // 12, k % 12 + 1
    return date(y, m, min(anchor_day or d.day, calendar.monthrange(y, m)[1]))


def _add_month(d: date) -> date:
    return _add_months(d, 1)


def _expand_recurring(c: dict, today: date) -> list[dict]:
    """One charge per billing month from c['start'] through the period covering today.

    Every period is anchored on the ORIGINAL start day (Jan 31 -> Feb 28 -> Mar 31), so a short
    month clamps one period without dragging every later period onto the clamped day.
    """
    start = _to_date(c["start"])
    stop = _to_date(c["end"]) if c.get("end") else None
    out = []
    i = 0
    period_start = start
    while period_start <= today and (stop is None or period_start < stop):
        period_end = _add_months(start, i + 1)
        out.append({
            **{k: v for k, v in c.items() if k != "recurring"},
            "receipt_id": f"{c.get('receipt_id', 'RECURRING')}-{i}",
            "start": period_start.isoformat(),
            "end": (min(period_end, stop) if stop else period_end).isoformat(),
        })
        period_start = period_end
        i += 1
    return out


def effective_charges(today: date | None = None) -> list[dict]:
    """Charges with recurring records expanded and account/provider defaulted."""
    today = today or datetime.now(timezone.utc).date()
    out = []
    for c in load_charges():
        if not isinstance(c, dict):
            continue
        expanded = _expand_recurring(c, today) if c.get("recurring") else [dict(c)]
        for e in expanded:
            e.setdefault("account", "personal")
            e.setdefault("provider", "anthropic")
            out.append(e)
    return out


def save_charges(charges: list[dict], backup: bool = False) -> None:
    """Atomic write (temp file + rename in the same dir); backup=True first copies the current file
    to billing.json.bak."""
    import shutil
    import tempfile
    charges = sorted(charges, key=lambda c: (c.get("start") or "", c.get("receipt_id") or ""))
    path = BILLING_PATH
    if backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    fd, tmp = tempfile.mkstemp(prefix=".billing-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(charges, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- manual receipts (CRUD) ----------

PROVIDERS = ("anthropic", "openai_chatgpt", "openai_api", "other")
# Accounts that run on a ChatGPT/Codex plan; API-org charges must never land on them.
CHATGPT_PLAN_ACCOUNTS = ("claudex", "codex-cli")
_ACCOUNT_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ReceiptError(ValueError):
    pass


def validate_receipt(r: dict) -> dict:
    """Normalise and validate a manual receipt; raises ReceiptError. Returns the record to store."""
    if not isinstance(r, dict):
        raise ReceiptError("body must be a JSON object")
    allowed = {"receipt_id", "start", "end", "plan", "amount_usd", "account", "provider", "recurring", "note",
               "currency", "amount_original"}
    extra = set(r) - allowed - {"source"}
    if extra:
        raise ReceiptError(f"unknown fields: {sorted(extra)}")
    out: dict = {}
    try:
        start = date.fromisoformat(str(r.get("start") or ""))
    except ValueError:
        raise ReceiptError("start must be YYYY-MM-DD")
    recurring = bool(r.get("recurring"))
    end = None
    if r.get("end") not in (None, ""):
        try:
            end = date.fromisoformat(str(r["end"]))
        except ValueError:
            raise ReceiptError("end must be YYYY-MM-DD")
        if end <= start:
            raise ReceiptError("end must be after start")
    elif not recurring:
        raise ReceiptError("end is required unless recurring is true")
    try:
        amount = float(r.get("amount_usd"))
    except (TypeError, ValueError):
        raise ReceiptError("amount_usd must be a number")
    if not (0 < amount < 1_000_000) or amount != amount:
        raise ReceiptError("amount_usd must be > 0")
    plan = str(r.get("plan") or "").strip()
    if not plan or len(plan) > 80:
        raise ReceiptError("plan is required (max 80 chars)")
    account = str(r.get("account") or "").strip()
    if not _ACCOUNT_RE.match(account):
        raise ReceiptError("account is required (letters, digits, . _ -)")
    provider = str(r.get("provider") or "anthropic").strip()
    if provider not in PROVIDERS:
        raise ReceiptError(f"provider must be one of {list(PROVIDERS)}")
    if provider == "openai_api" and account in CHATGPT_PLAN_ACCOUNTS:
        raise ReceiptError(f"openai_api charges cannot be attributed to {account} (a ChatGPT-plan account)")
    rid = str(r.get("receipt_id") or "").strip() or f"manual-{provider}-{account}-{start.isoformat()}"
    if len(rid) > 100:
        raise ReceiptError("receipt_id too long")
    out.update(receipt_id=rid, start=start.isoformat(), plan=plan, amount_usd=round(amount, 2), account=account,
               provider=provider, source="manual")
    if end:
        out["end"] = end.isoformat()
    if recurring:
        out["recurring"] = True
    if r.get("note"):
        out["note"] = str(r["note"])[:200]
    if r.get("currency"):
        out["currency"] = str(r["currency"])[:8].upper()
    if r.get("amount_original") not in (None, ""):
        try:
            out["amount_original"] = float(r["amount_original"])
        except (TypeError, ValueError):
            raise ReceiptError("amount_original must be a number")
    return out


def upsert_manual(r: dict) -> dict:
    """Validate and insert/replace a manual receipt (never overwrites a scraped one). Backs up first."""
    rec = validate_receipt(r)
    charges = load_charges()
    for i, c in enumerate(charges):
        if c.get("receipt_id") == rec["receipt_id"]:
            if c.get("source", "manual") != "manual":
                raise ReceiptError(f"receipt {rec['receipt_id']} came from {c.get('source')}; not overwritten")
            charges[i] = rec
            break
    else:
        charges.append(rec)
    save_charges(charges, backup=True)
    return rec


def delete_manual(receipt_id: str) -> bool:
    """Delete a manual receipt by id. False if absent; ReceiptError for a scraped one."""
    charges = load_charges()
    keep = [c for c in charges if c.get("receipt_id") != receipt_id]
    if len(keep) == len(charges):
        return False
    gone = [c for c in charges if c.get("receipt_id") == receipt_id]
    if any(c.get("source", "manual") != "manual" for c in gone):
        raise ReceiptError("only manual receipts can be deleted (a scraped one would come back on the next scrape)")
    save_charges(keep, backup=True)
    return True


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
        s.setdefault("account", "personal")  # scraper reads the personal inbox
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


def subscription_cost(from_ts: str, to_ts: str, account: str | None = None) -> dict:
    """Return total subscription spend in [from_ts, to_ts]; prorated by intersection.

    An explicit account label filters to that account; None/"all" includes all.
    """
    f = _to_date(from_ts)
    t = _to_date(to_ts)
    charges = effective_charges()
    total = 0.0
    by_account: dict[str, float] = {}
    by_provider: dict[str, float] = {}
    by_acct_prov: dict[tuple[str, str], float] = {}
    out = []
    coverage_first = coverage_last = None
    for c in charges:
        if account and account != "all" and c["account"] != account:
            continue
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
            by_account[c["account"]] = by_account.get(c["account"], 0.0) + applied
            by_provider[c["provider"]] = by_provider.get(c["provider"], 0.0) + applied
            k = (c["account"], c["provider"])
            by_acct_prov[k] = by_acct_prov.get(k, 0.0) + applied
            out.append({
                "receipt_id": c.get("receipt_id"),
                "plan": c["plan"],
                "start": cs.isoformat(),
                "end": ce.isoformat(),
                "amount": c["amount_usd"],
                "applied": round(applied, 2),
                "source": c.get("source", "manual"),
                "account": c["account"],
                "provider": c["provider"],
            })
    return {
        "total": round(total, 2),
        "by_account": {k: round(v, 2) for k, v in by_account.items()},
        "by_provider": {k: round(v, 2) for k, v in by_provider.items()},
        "by_account_provider": by_acct_prov,
        "charges": out,
        "coverage": {
            "first": coverage_first.isoformat() if coverage_first else None,
            "last": coverage_last.isoformat() if coverage_last else None,
        },
    }


# ---------- config ----------

_config_cache: tuple[float, dict] | None = None


def config_path() -> Path:
    env = os.environ.get("CLAUDE_USAGE_CONFIG")
    return Path(env).expanduser() if env else CONFIG_PATH


def load_config() -> dict:
    """Optional user config (timezone, per-account billing overrides). {} when absent or invalid."""
    global _config_cache
    p = config_path()
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    if _config_cache and _config_cache[0] == mt and _config_cache[1].get("_path") == str(p):
        return _config_cache[1]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except Exception:
        d = {}
    d["_path"] = str(p)
    _config_cache = (mt, d)
    return d


# ---------- billing paths ----------

MODES = ("subscription", "api", "cloud", "gateway")
CLOUD_FLAGS = {"CLAUDE_CODE_USE_BEDROCK": "bedrock", "CLAUDE_CODE_USE_VERTEX": "vertex",
               "CLAUDE_CODE_USE_FOUNDRY": "foundry"}
KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
SIGNAL_VARS = (*KEY_VARS, "ANTHROPIC_BASE_URL", *CLOUD_FLAGS)
ANTHROPIC_HOSTS = ("anthropic.com", "claude.ai", "claude.com")
LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
REGIONAL_PREMIUM_PCT = 10.0  # Bedrock / Vertex regional endpoints


def _settings_signals(config_dir: Path) -> tuple[set[str], str | None]:
    """(names of billing-relevant settings present, base-URL host:port). Values are never kept."""
    names: set[str] = set()
    host = None
    for fn in ("settings.json", "settings.local.json"):
        try:
            d = json.loads((Path(config_dir) / fn).read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("apiKeyHelper"):
            names.add("apiKeyHelper")
        env = d.get("env") if isinstance(d.get("env"), dict) else {}
        names.update(k for k in env if k in SIGNAL_VARS and env[k] not in (None, ""))
        if env.get("ANTHROPIC_BASE_URL"):
            try:  # host[:port] only: userinfo, path and query can carry secrets
                u = urllib.parse.urlsplit(str(env["ANTHROPIC_BASE_URL"]))
                host = u.hostname + (f":{u.port}" if u.port else "") if u.hostname else host
            except ValueError:
                host = host or "invalid"
    return names, host


_probe_cache: dict[str, str] = {}


def _probe_gateway(host: str) -> str:
    """Identify a LOCAL gateway by its unauthenticated root page. Remote hosts are never probed."""
    if host in _probe_cache:
        return _probe_cache[host]
    kind = "unknown"
    try:
        with urllib.request.urlopen(f"http://{host}/", timeout=0.5) as r:
            body = r.read(4096).decode("utf-8", "replace").lower()
        if "cli proxy api" in body:
            kind = "cliproxyapi"
        elif "litellm" in body:
            kind = "litellm_proxy"
    except Exception:
        pass
    _probe_cache[host] = kind
    return kind


def _gateway_kind(host: str | None, probe) -> str:
    h = (host or "").lower()
    hostname = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if "openrouter" in hostname:
        return "openrouter"
    if hostname in LOOPBACK:
        return probe(h) if probe else "unknown"
    return "unknown"


def detect_billing_mode(config_dir: Path, probe=None) -> dict:
    """Billing path of one Claude config dir, from which settings are PRESENT (never their values)."""
    names, host = _settings_signals(config_dir)
    hostname = (host or "").rsplit(":", 1)[0].lower()
    on_anthropic = bool(hostname) and any(hostname == a or hostname.endswith("." + a) for a in ANTHROPIC_HOSTS)
    cloud = sorted(v for k, v in CLOUD_FLAGS.items() if k in names)
    out = {"billing_mode": "subscription", "provider": "anthropic", "gateway_host": None,
           "signals": sorted(names), "detected": True}
    if cloud:
        out.update(billing_mode="cloud", provider=cloud[0])
    elif "ANTHROPIC_BASE_URL" in names and host and not on_anthropic:
        out.update(billing_mode="gateway", provider=_gateway_kind(host, probe or _probe_gateway), gateway_host=host)
    elif names & {*KEY_VARS, "apiKeyHelper"}:
        out.update(billing_mode="api")
    return out


def _counts_for(account: str, provider: str) -> bool:
    """openai_api (API org) charges never count toward a ChatGPT-plan account."""
    return not (provider == "openai_api" and account in CHATGPT_PLAN_ACCOUNTS)


def has_receipts(account: str, today: date | None = None, members: Iterable[str] | None = None) -> bool:
    """Any receipt for the account (or, for a plan group, for any member account)."""
    names = set(members or ()) | {account}
    return any(c.get("account") in names and _counts_for(account, c.get("provider", "anthropic"))
               for c in effective_charges(today))


def paid_by_groups(names: Iterable[str] = ()) -> dict[str, list[str]]:
    """provider -> accounts paid by it (config.json accounts.<name>.paid_by, plus tool defaults)."""
    acc = load_config().get("accounts") or {}
    out: dict[str, list[str]] = {}
    for n in sorted(set(acc) | set(names)):
        pb = (acc.get(n) or {}).get("paid_by")
        if pb:
            out.setdefault(str(pb), []).append(n)
    return out


# ---------- other coding agents (Codex CLI, Pi): billing from their auth files ----------

def _json_keys(path: Path) -> dict | None:
    """Parse a JSON auth file and return only {key: kind} -- never the values."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    kinds = {}
    for k, v in d.items():
        if v is None or v == "" or v == {}:
            kinds[k] = None
        elif isinstance(v, dict):
            # Pi: {"<provider>": {"type": "oauth"|"api_key", ...}} -- only the type label is kept
            kinds[k] = {"type": v.get("type") if isinstance(v.get("type"), str) else None,
                        "keys": sorted(x for x in v if v[x] not in (None, ""))}
        else:
            kinds[k] = "set"
    del d
    return kinds


def detect_tool_billing(tool: str, config_dir: Path) -> dict:
    """Billing path of a non-Claude-Code agent from the PRESENCE of credentials (values never kept).

    codex-cli: auth.json with a tokens object = ChatGPT login (subscription, paid_by openai_chatgpt);
               OPENAI_API_KEY set = API key (list price, paid_by openai_api).
    pi:        auth.json per provider: type oauth = that provider's subscription, else an API key."""
    out = {"billing_mode": "subscription", "provider": "openai", "gateway_host": None, "signals": [],
           "detected": False, "paid_by": None}
    kinds = _json_keys(Path(config_dir) / "auth.json")
    if kinds is None:
        return out
    if tool == "codex-cli":
        if isinstance(kinds.get("tokens"), dict) and kinds["tokens"]["keys"]:
            out.update(billing_mode="subscription", signals=["chatgpt_login"], detected=True, paid_by="openai_chatgpt")
        elif kinds.get("OPENAI_API_KEY"):
            out.update(billing_mode="api", signals=["OPENAI_API_KEY"], detected=True, paid_by="openai_api")
    elif tool == "pi":
        oauth = sorted(k for k, v in kinds.items() if isinstance(v, dict) and v["type"] == "oauth")
        keys = sorted(k for k, v in kinds.items() if v and not (isinstance(v, dict) and v["type"] == "oauth"))
        out["signals"] = [f"{k}:oauth" for k in oauth] + [f"{k}:api_key" for k in keys]
        if oauth:
            prov = oauth[0]
            out.update(billing_mode="subscription", detected=True, provider=prov,
                       paid_by="openai_chatgpt" if "openai" in prov else ("anthropic" if "anthropic" in prov else None))
        elif keys:
            out.update(billing_mode="api", detected=True, provider=keys[0],
                       paid_by="openai_api" if "openai" in keys[0] else None)
    return out


def account_billing(name: str, config_dirs: list[Path], probe=None, tool: str = "claude-code") -> dict:
    """Detected billing path for one account (first config dir = highest priority), with config overrides.

    payment: "subscription" (receipts are what was paid) | "list" (list price is the bill) |
             "per_token" (list x (1 + markup_pct/100)) | "unknown".
    paid_by: the subscription provider whose receipts pay for this account (config.json or tool default);
             accounts with the same paid_by share those receipts by usage.
    """
    if tool != "claude-code":
        det = detect_tool_billing(tool, config_dirs[0]) if config_dirs else \
            {"billing_mode": "subscription", "provider": "openai", "gateway_host": None, "signals": [],
             "detected": False, "paid_by": None}
    else:
        det = detect_billing_mode(config_dirs[0], probe) if config_dirs else \
            {"billing_mode": "subscription", "provider": "anthropic", "gateway_host": None, "signals": [],
             "detected": False}
    ov = (load_config().get("accounts") or {}).get(name) or {}
    info = {**det, "account": name, "tool": tool, "markup_pct": None, "regional_premium_pct": 0.0,
            "payment_basis": None}
    info["paid_by"] = ov.get("paid_by") or det.get("paid_by")
    info["payer"] = ov.get("payer") if ov.get("payer") in ("self", "employer") else "self"
    if info["paid_by"] == "openai_api" and name in CHATGPT_PLAN_ACCOUNTS:
        info["paid_by"] = None  # the API org is never a ChatGPT-plan account's payer
    if ov.get("billing_mode") in MODES:
        info["billing_mode"], info["detected"] = ov["billing_mode"], False
    members = paid_by_groups([name]).get(info["paid_by"], []) if info["paid_by"] else []
    if info["paid_by"] and name not in members:
        members.append(name)
    info["plan_members"] = sorted(members)
    receipts = has_receipts(name, members=members)
    mode = info["billing_mode"]
    if mode == "subscription":
        payment, basis = "subscription", "billing.json receipts"
    elif mode == "api":
        payment, basis = "list", "API list price"
    elif mode == "cloud":
        payment, basis = "list", "cloud list price"
        rp = ov.get("regional_premium_pct", ov.get("regional_premium"))
        if rp is True:
            rp = REGIONAL_PREMIUM_PCT
        info["regional_premium_pct"] = float(rp or 0.0)
    elif receipts:
        payment, basis = "subscription", "billing.json receipts"
    elif info["paid_by"] and info["paid_by"] != "openai_api":
        payment, basis = "subscription", f"{info['paid_by']} plan; no receipt recorded"
    elif ov.get("markup_pct") is not None:
        payment, basis = "per_token", "list price + router markup"
    elif info["provider"] == "cliproxyapi":
        payment, basis = "subscription", "CLIProxyAPI routes through a ChatGPT/Codex subscription; no receipt recorded"
    elif info["provider"] in ("openrouter", "litellm_proxy"):
        payment, basis = "per_token", "pay-per-token router at list price (no markup configured)"
    else:
        payment, basis = "unknown", "gateway payment unknown; set payment/markup_pct in config.json"
    if ov.get("payment") in ("subscription", "list", "per_token", "unknown"):
        payment, basis = ov["payment"], "config.json"
    if ov.get("markup_pct") is not None:
        info["markup_pct"] = float(ov["markup_pct"])
    info.update(payment=payment, payment_basis=basis, has_receipts=receipts)
    return info


def plan_receipts(infos: dict[str, dict], api_cost: dict[str, float], by_account_provider: dict) -> dict[str, float]:
    """account -> receipts applied to it over a window.

    Accounts without paid_by keep their own receipts (minus openai_api ones for ChatGPT-plan accounts).
    Accounts sharing a paid_by form a plan group: the group's receipts (every member's) are split
    between members by their share of the group's API-equivalent cost in the window (by own receipts
    when the group has no usage), so a plan is never counted twice."""
    own: dict[str, float] = {}
    for (acct, prov), v in by_account_provider.items():
        if _counts_for(acct, prov):
            own[acct] = own.get(acct, 0.0) + v
    names = set(infos) | set(api_cost) | set(own)
    groups: dict[str, list[str]] = {}
    for n in names:
        pb = (infos.get(n) or {}).get("paid_by")
        if pb:
            groups.setdefault(pb, []).append(n)
    out = {n: own.get(n, 0.0) for n in names}
    for members in groups.values():
        pool = sum(own.get(m, 0.0) for m in members)
        weight = {m: max(0.0, float(api_cost.get(m, 0.0) or 0.0)) for m in members}
        tw = sum(weight.values())
        for m in members:
            out[m] = pool * weight[m] / tw if tw > 0 else own.get(m, 0.0)
    return out


def actual_cost(info: dict, api_cost: float, receipts_applied: float) -> float | None:
    """What the account actually paid over a window, or None when it cannot be known."""
    p = info.get("payment")
    if p == "subscription":
        return round(receipts_applied, 2) if info.get("has_receipts") else None
    if p == "list":
        return round(api_cost * (1 + (info.get("regional_premium_pct") or 0) / 100), 2)
    if p == "per_token":
        return round(api_cost * (1 + (info.get("markup_pct") or 0) / 100), 2)
    return None


if __name__ == "__main__":
    import sys
    a, b = sys.argv[1], sys.argv[2]
    print(json.dumps(subscription_cost(a, b), indent=2))
