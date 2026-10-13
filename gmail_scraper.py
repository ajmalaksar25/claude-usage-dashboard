"""Gmail OAuth + pluggable subscription-receipt scraper.

Receipt PROVIDERS (each: name, Gmail query, parse(msg_or_raw) -> dict | None):
  anthropic        Anthropic (Stripe) receipts from invoice+statements@mail.anthropic.com
  openai_chatgpt   ChatGPT Plus/Pro/Team receipts paid directly to OpenAI (Stripe)
  openai_api       OpenAI API credit purchases ("account has been funded", API receipts)
  apple_app_store  Apple receipts whose line items are ChatGPT or Claude subscriptions
  google_play      Google Play subscription mails (purchase, renewal, upgrade, cancel)

Every parser returns either a CHARGE ({"kind": "charge", ...}, see CHARGE_KEYS), a
CANCELLATION ({"kind": "cancellation", "effective": "YYYY-MM-DD", ...}) or None.
`provider` is the logical billing provider (any ChatGPT subscription is "openai_chatgpt"
whichever store sold it); the store is kept separately in `store`. amount_usd EXCLUDES
VAT/tax (same convention as the original Anthropic parser).

Public API:
  build_service(creds_path, token_path, port=0, interactive=True) -> Gmail service
  parse_receipt(raw_bytes) -> dict | None        (Anthropic receipts; unchanged keys + new ones)
  parse_any(raw_or_msg, providers=None) -> dict | None
  scrape_all(service, providers=None, query=None) -> list[dict]
  dry_run(service, providers=None, max_results=50) -> dict      (never writes billing.json)
  apply_cancellations(charges, cancellations) -> list[dict]
  suggest_account(provider) -> str
  fx_rate(currency) -> (units_per_usd | None, approximate)
  scrape_and_save(creds_path, token_path) -> {found, added, updated, total, errors, ...}
  status(), list_receipt_ids(service, query, max_results), fetch_raw(service, message_id)

Parsing is stdlib only, so it can be tested against .eml files with no Google dependencies.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import html as _html
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from email import message_from_bytes
from email.message import Message
from email.policy import default as email_policy
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).parent
DEFAULT_CREDS = ROOT / "credentials.json"
DEFAULT_TOKEN = ROOT / "token.json"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
ANTHROPIC_SENDER = "invoice+statements@mail.anthropic.com"
DEFAULT_QUERY = f"from:{ANTHROPIC_SENDER}"
GOOGLE_PLAY_SENDER = "googleplay-noreply@google.com"
APPLE_SENDER = "no_reply@email.apple.com"

CHARGE_KEYS = (
    "kind", "provider", "store", "merchant", "plan", "raw_plan", "receipt_id", "start", "end",
    "amount_usd", "amount_original", "currency", "vat_original", "fx_rate", "fx_approximate",
    "source", "account",
)

# ---------- shared text helpers (stdlib only) ----------

_MONTH = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}

_BIDI = re.compile("[​-‏‪-‮⁦-⁩؜﻿]")


def _as_msg(msg_or_raw) -> Message | None:
    if msg_or_raw is None:
        return None
    if isinstance(msg_or_raw, Message):
        return msg_or_raw
    if isinstance(msg_or_raw, str):
        msg_or_raw = msg_or_raw.encode("utf-8")
    if not msg_or_raw:
        return None
    try:
        return message_from_bytes(msg_or_raw, policy=email_policy)
    except Exception:
        return None


def _header(msg: Message, name: str) -> str:
    try:
        return str(msg.get(name) or "")
    except Exception:
        return ""


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return _html.unescape(s)


def _decode_text_body(msg) -> str:
    """Return the text/plain body as a string."""
    body = msg.get_body(preferencelist=("plain",))
    if body is None:
        # fall back to first text part
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part
                break
    if body is None:
        return ""
    try:
        return body.get_content()
    except Exception:
        return ""


def _body_text(msg: Message) -> str:
    """text/plain body, else the HTML body converted to text; bidi marks/nbsp cleaned, lines kept."""
    text = _decode_text_body(msg)
    if not text.strip():
        part = msg.get_body(preferencelist=("html",))
        try:
            text = _html_to_text(part.get_content()) if part is not None else ""
        except Exception:
            text = ""
    text = _BIDI.sub("", text).replace(" ", " ").replace(" ", " ")
    return "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _normalize_text(s: str) -> str:
    # collapse soft-wrapped quoted-printable artifacts already handled by email module,
    # but also strip soft hyphens, replace en-dashes with simple dashes for splitting
    s = s.replace(" ", " ").replace(" ", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _add_months(d: date, n: int) -> date:
    k = d.month - 1 + n
    y, m = d.year + k // 12, k % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


# ---------- dates ----------

_MON_NAME = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DATE_ANY_RE = re.compile(
    rf"(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m1>{_MON_NAME}),?\s+(?P<y1>\d{{4}})"
    rf"|(?P<m2>{_MON_NAME})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{{4}})"
    r"|(?P<y3>\d{4})-(?P<m3>\d{2})-(?P<d3>\d{2})",
    re.I,
)


def _date_from_match(m) -> date | None:
    try:
        if m.group("y3"):
            return date(int(m.group("y3")), int(m.group("m3")), int(m.group("d3")))
        if m.group("y1"):
            mon, d, y = m.group("m1"), m.group("d1"), m.group("y1")
        else:
            mon, d, y = m.group("m2"), m.group("d2"), m.group("y2")
        return date(int(y), _MONTH[mon[:3].title()], int(d))
    except Exception:
        return None


def _find_date(s: str) -> date | None:
    for m in _DATE_ANY_RE.finditer(s or ""):
        d = _date_from_match(m)
        if d:
            return d
    return None


def _date_after(text: str, label: str, window: int = 60, flags: int = re.I) -> date | None:
    """First date within `window` chars after any occurrence of `label`."""
    for m in re.finditer(label, text, flags):
        d = _find_date(text[m.end():m.end() + window])
        if d:
            return d
    return None


def _header_date(msg: Message) -> date | None:
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(_header(msg, "Date")).date()
    except Exception:
        return None


# ---------- money / currency ----------

_CUR_TOKENS = {
    "د.إ.": "AED", "د.إ": "AED", "AED": "AED", "Dhs.": "AED", "Dhs": "AED", "DHS": "AED",
    "ر.س.": "SAR", "ر.س": "SAR", "SAR": "SAR",
    "US$": "USD", "USD": "USD", "$": "USD",
    "CA$": "CAD", "C$": "CAD", "CAD": "CAD", "A$": "AUD", "AU$": "AUD", "AUD": "AUD",
    "NZ$": "NZD", "NZD": "NZD", "S$": "SGD", "SGD": "SGD", "HK$": "HKD", "HKD": "HKD",
    "€": "EUR", "EUR": "EUR", "£": "GBP", "GBP": "GBP", "₹": "INR", "INR": "INR",
    "Rs.": "INR", "Rs": "INR", "¥": "JPY", "JPY": "JPY", "CHF": "CHF", "QAR": "QAR",
    "KWD": "KWD", "BHD": "BHD", "OMR": "OMR", "PKR": "PKR",
}


def _tok_pat(t: str) -> str:
    p = re.escape(t)
    if t[0].isascii() and t[0].isalpha():
        p = r"(?<![A-Za-z])" + p
    if t[-1].isascii() and t[-1].isalpha():
        p += r"(?![A-Za-z])"
    return p


_CUR = "(?:" + "|".join(_tok_pat(t) for t in sorted(_CUR_TOKENS, key=len, reverse=True)) + ")"
_NUM = r"(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
MONEY = rf"(?:{_CUR}\s?{_NUM}|{_NUM}\s?{_CUR}(?!\s?\d))"  # embeddable, no capture groups
_MONEY_RE = re.compile(rf"(?P<pre>{_CUR})\s?(?P<n1>{_NUM})|(?P<n2>{_NUM})\s?(?P<post>{_CUR})(?!\s?\d)")


def _num(s: str) -> float:
    s = s.strip()
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.fullmatch(r"\d+,\d{1,2}", s) else s.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s)


def parse_money(s: str) -> tuple[float, str] | None:
    """First amount+currency in s: '76.99 د.إ.' -> (76.99, 'AED'); '$20.00' -> (20.0, 'USD')."""
    m = _MONEY_RE.search(s or "")
    if not m:
        return None
    tok = m.group("pre") or m.group("post")
    return _num(m.group("n1") or m.group("n2")), _CUR_TOKENS.get(tok, tok.upper())


def _money_after(text: str, label: str, window: int = 80, flags: int = re.I) -> tuple[float, str] | None:
    m = re.search(label, text, flags)
    return parse_money(text[m.end():m.end() + window]) if m else None


# Units of currency per 1 USD. USD-pegged currencies (AED 3.6725, SAR 3.75, QAR 3.64, BHD 0.376,
# OMR 0.3845, HKD band ~7.8 is not exact) are treated as exact; the rest are approximate defaults,
# overridable from config.json {"fx_per_usd": {"EUR": 0.92}} (an override is treated as exact).
FX_PER_USD = {
    "USD": 1.0, "AED": 3.6725, "EUR": 0.92, "GBP": 0.79, "INR": 83.5, "SAR": 3.75, "CAD": 1.37,
    "AUD": 1.52, "NZD": 1.66, "SGD": 1.35, "HKD": 7.8, "JPY": 150.0, "CHF": 0.88, "QAR": 3.64,
    "KWD": 0.307, "BHD": 0.376, "OMR": 0.3845, "PKR": 280.0,
}
FX_EXACT = {"USD", "AED", "SAR", "QAR", "BHD", "OMR"}


def _config() -> dict:
    try:
        import billing
        cfg = billing.load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def fx_rate(currency: str) -> tuple[float | None, bool]:
    """(units of `currency` per 1 USD, approximate?). (None, True) when unknown."""
    cur = (currency or "").upper()
    over = _config().get("fx_per_usd") or {}
    if isinstance(over, dict):
        try:
            v = float(over.get(cur)) if over.get(cur) is not None else None
        except (TypeError, ValueError):
            v = None
        if v and v > 0:
            return v, False
    if cur in FX_PER_USD:
        return FX_PER_USD[cur], cur not in FX_EXACT
    return None, True


def _money_fields(total: float, vat: float | None, currency: str) -> dict:
    vat = round(vat or 0.0, 2)
    excl = round(total - vat, 2)
    rate, approx = fx_rate(currency)
    return {
        "amount_usd": round(excl / rate, 2) if rate else None,
        "amount_original": round(total, 2),
        "currency": currency,
        "vat_original": vat,
        "fx_rate": rate,
        "fx_approximate": approx,
    }


# ---------- accounts ----------

DEFAULT_ACCOUNTS = {"anthropic": "personal", "openai_chatgpt": "claudex", "openai_api": "openai-api"}
FORBIDDEN_ACCOUNTS = {"openai_api": {"claudex", "codex-cli"}}


def suggest_account(provider: str) -> str:
    """First config account whose paid_by == provider, else a per-provider default.

    openai_api never resolves to claudex/codex-cli (the user's API org is billed separately)."""
    banned = FORBIDDEN_ACCOUNTS.get(provider, set())
    accts = _config().get("accounts") or {}
    items = accts.items() if isinstance(accts, dict) else (
        (a.get("name"), a) for a in accts if isinstance(a, dict)) if isinstance(accts, list) else ()
    for name, a in items:
        if name and isinstance(a, dict) and a.get("paid_by") == provider and name not in banned:
            return name
    return DEFAULT_ACCOUNTS.get(provider, "personal")


# ---------- record builders ----------

def _charge(*, provider: str, store: str, merchant: str, plan: str, raw_plan: str, receipt_id: str,
            start: date, end: date, total: float, vat: float | None, currency: str) -> dict:
    out = {
        "kind": "charge", "provider": provider, "store": store, "merchant": merchant,
        "plan": plan, "raw_plan": raw_plan, "receipt_id": receipt_id,
        "start": start.isoformat(), "end": end.isoformat(),
    }
    out.update(_money_fields(total, vat, currency))
    out["source"] = "gmail"
    out["account"] = suggest_account(provider)
    return out


def _synthetic_id(msg: Message, *parts) -> str:
    """Stable id when a mail carries no order/receipt number: Message-ID, else the given parts."""
    basis = _header(msg, "Message-ID").strip() or "|".join(str(p) for p in parts)
    return "msg-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


_CHATGPT_PLAN_RE = re.compile(r"ChatGPT\s+(Plus|Pro|Team|Business|Go|Enterprise|Edu)\b", re.I)


def _chatgpt_plan(raw: str) -> str:
    m = _CHATGPT_PLAN_RE.search(raw or "")
    return f"ChatGPT {m.group(1).title()}" if m else "ChatGPT"


def _app_vendor(text: str) -> str | None:
    """'openai' / 'anthropic' / None from free text (subject + body)."""
    if re.search(r"ChatGPT|OpenAI", text, re.I):
        return "openai"
    if re.search(r"\bClaude\b|Anthropic", text, re.I):
        return "anthropic"
    return None


def _merchant(text: str, vendor: str, default: str) -> str:
    if vendor == "openai":
        m = re.search(r"OpenAI(?:\s+OpCo)?,?\s+L\.?L\.?C\.?", text)
        return re.sub(r"\s+", " ", m.group(0)).rstrip(".") if m else default
    m = re.search(r"Anthropic,?\s+PBC", text)
    return m.group(0) if m else default


# ---------- provider: anthropic ----------

# "Receipt #2443-1340-7804"
_RECEIPT_ID_RE = re.compile(r"Receipt\s*#\s*(\d{4}-\d{4}-\d{4})")

# Date range: "Apr 6–May 6, 2026", "Mar 19 - Apr 19, 2026", "Dec 19, 2025 – Jan 19, 2026"
_DATE_RANGE_RE = re.compile(
    r"([A-Z][a-z]{2})\s+(\d{1,2})(?:,\s*(\d{4}))?\s*[–—\-]\s*"
    r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})"
)

# Total excluding tax (canonical net amount we want to record)
_TOTAL_EXCL_RE = re.compile(r"Total\s+excluding\s+tax\s*\$?\s*([\d,]+\.\d{2})", re.I)

# Plan line: "<Plan name> Qty 1 $XX.XX" — first plan line item (positive amount)
_PLAN_RE = re.compile(
    r"(?P<plan>[A-Z][^$\n]+?)\s+Qty\s+1\s+\$?\s*(?P<amt>[\d,]+\.\d{2})",
)
# "Prepaid extra usage, Individual plan Qty 1 $10.00" — no preceding date range, so handle separately
_PREPAID_RE = re.compile(r"(Prepaid\s+extra\s+usage[^$\n]*?)\s+Qty\s+1\s+\$?\s*([\d,]+\.\d{2})", re.I)


def _parse_date_range(s: str) -> tuple[date, date] | None:
    m = _DATE_RANGE_RE.search(s)
    if not m:
        return None
    sm, sd, sy, em, ed, ey = m.groups()
    end_year = int(ey)
    if sy is None:
        # Same-year range; if start month > end month, the range crosses year boundary
        start_year = end_year - 1 if _MONTH[sm] > _MONTH[em] else end_year
    else:
        start_year = int(sy)
    try:
        return date(start_year, _MONTH[sm], int(sd)), date(end_year, _MONTH[em], int(ed))
    except Exception:
        return None


def parse_receipt(raw_bytes) -> dict | None:
    """Parse a raw RFC822 .eml receipt from Anthropic. Return charge dict or None.

    Keys of the original parser (receipt_id, start, end, plan, raw_plan, amount_usd excl. tax,
    source) are unchanged; provider/store/merchant/account/kind and the currency fields are added."""
    msg = _as_msg(raw_bytes)
    if msg is None:
        return None
    sender = _header(msg, "From").lower()
    if ANTHROPIC_SENDER not in sender:
        return None
    text = _normalize_text(_decode_text_body(msg))
    if not text:
        return None

    rid_m = _RECEIPT_ID_RE.search(text)
    if not rid_m:
        return None
    receipt_id = rid_m.group(1)

    # The interesting region is between "Receipt #..." and "Subtotal"
    after = text[rid_m.end():]
    cut = after.find("Subtotal")
    region = after[:cut] if cut > 0 else after

    rng_m = _DATE_RANGE_RE.search(region)
    rng = _parse_date_range(region)
    # Look for the plan AFTER the date range so we don't capture the date itself
    plan_search_region = region[rng_m.end():] if rng_m else region
    plan_m = _PLAN_RE.search(plan_search_region)

    # Some receipts (one-time prepaid) don't have a date range in the same line
    if rng is None:
        prepaid = _PREPAID_RE.search(region) or _PLAN_RE.search(region)
        if prepaid is None:
            return None
        plan_name = prepaid.group(1).strip()
        # Use message Date header for a synthetic period
        start = _header_date(msg)
        if start is None:
            return None
        end = _add_months(start, 1)
    else:
        start, end = rng
        plan_name = plan_m.group("plan").strip() if plan_m else "Unknown"

    total_m = _TOTAL_EXCL_RE.search(text)
    if total_m:
        amount = float(total_m.group(1).replace(",", ""))
    elif plan_m:
        amount = float(plan_m.group("amt").replace(",", ""))
    else:
        return None

    paid = _money_after(text, r"Amount\s+paid", 40)
    total = paid[0] if paid and paid[1] == "USD" and paid[0] >= amount else amount
    return {
        "kind": "charge",
        "provider": "anthropic",
        "store": "anthropic",
        "merchant": "Anthropic",
        "receipt_id": receipt_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "plan": _friendly_plan(plan_name),
        "raw_plan": plan_name,
        "amount_usd": round(amount, 2),
        "amount_original": round(total, 2),
        "currency": "USD",
        "vat_original": round(total - amount, 2),
        "fx_rate": 1.0,
        "fx_approximate": False,
        "source": "gmail",
        "account": suggest_account("anthropic"),
    }


def _friendly_plan(raw: str) -> str:
    r = raw.lower()
    if "max" in r and "20" in r:
        return "Max 20x"
    if "max" in r and "5" in r:
        return "Max 5x"
    if "prepaid" in r or "extra" in r:
        return "Pro extra"
    if "pro" in r:
        return "Pro"
    return raw.strip()


# ---------- provider: OpenAI direct (Stripe) — ChatGPT subscriptions and API credits ----------

_TAX_LABEL = r"(?<!excluding )(?<!including )\b(?:Sales\s+tax|Tax|VAT|GST|HST)\b(?:\s*\([^)]*\))?"


def _openai_msg(msg: Message) -> tuple[str, str] | None:
    """(subject, flat body) for a mail sent by OpenAI or by Stripe on OpenAI's behalf."""
    sender = _header(msg, "From").lower()
    subject = _header(msg, "Subject")
    if not any(d in sender for d in ("openai.com", "stripe.com")):
        return None
    flat = _flat(_body_text(msg))
    if "openai" not in (sender + subject + flat).lower():
        return None
    return subject, flat


def _openai_kind(subject: str, flat: str) -> str | None:
    """'chatgpt' or 'api' — mutually exclusive so the two OpenAI providers never double-claim."""
    if _CHATGPT_PLAN_RE.search(flat) or _CHATGPT_PLAN_RE.search(subject):
        return "chatgpt"
    if re.search(r"\bAPI\b|\bcredits?\b|funded|platform\.openai", subject + " " + flat, re.I):
        return "api"
    if "chatgpt" in (subject + flat).lower():
        return "chatgpt"
    return None


def _stripe_id(msg: Message, flat: str, *fallback) -> str:
    m = (re.search(r"Receipt\s*(?:number|#)\s*:?\s*#?\s*(\d{4}-\d{4}(?:-\d{4})?)", flat, re.I)
         or re.search(r"Invoice\s*(?:number|#)\s*:?\s*([A-Z0-9][A-Z0-9-]{3,})", flat, re.I))
    return f"openai-{m.group(1)}" if m else _synthetic_id(msg, *fallback)


def _stripe_paid(flat: str) -> tuple[float, str] | None:
    return (_money_after(flat, r"Amount\s+paid", 40)
            or _money_after(flat, r"\bTotal\b(?!\s+excluding)", 30)
            or _money_after(flat, r"payment\s+of", 30))


def _stripe_vat(flat: str, paid: float | None) -> float:
    excl = _money_after(flat, r"Total\s+excluding\s+tax", 30)
    if excl and paid is not None and paid >= excl[0]:
        return round(paid - excl[0], 2)
    tax = _money_after(flat, _TAX_LABEL, 30)
    return tax[0] if tax else 0.0


def _stripe_date(msg: Message, flat: str) -> date | None:
    return (_date_after(flat, r"Date\s+paid", 40) or _date_after(flat, r"\bPaid\b", 30)
            or _header_date(msg))


def parse_openai_chatgpt(msg_or_raw) -> dict | None:
    msg = _as_msg(msg_or_raw)
    got = _openai_msg(msg) if msg is not None else None
    if not got:
        return None
    subject, flat = got
    if _openai_kind(subject, flat) != "chatgpt":
        return None
    paid = _stripe_paid(flat)
    if not paid:
        return None
    total, cur = paid
    line = re.search(rf"(ChatGPT[^$\n]{{0,60}}?)\s+(?:Qty\s*\d+\s+|[×x]\s*\d+\s+)?({MONEY})", flat)
    raw_plan = line.group(1).strip() if line else (_CHATGPT_PLAN_RE.search(flat + " " + subject).group(0)
                                                   if _CHATGPT_PLAN_RE.search(flat + " " + subject) else "ChatGPT")
    start = _stripe_date(msg, flat)
    rng = _parse_date_range(flat)
    if rng:
        start, end = rng
    elif start:
        end = _add_months(start, 1)
    else:
        return None
    return _charge(
        provider="openai_chatgpt", store="stripe", merchant=_merchant(flat, "openai", "OpenAI"),
        plan=_chatgpt_plan(raw_plan), raw_plan=raw_plan,
        receipt_id=_stripe_id(msg, flat, start, total), start=start, end=end,
        total=total, vat=_stripe_vat(flat, total), currency=cur,
    )


def parse_openai_api(msg_or_raw) -> dict | None:
    msg = _as_msg(msg_or_raw)
    got = _openai_msg(msg) if msg is not None else None
    if not got:
        return None
    subject, flat = got
    if _openai_kind(subject, flat) != "api":
        return None
    item = re.search(rf"\b(?:API|credits?)\b[^$\n]{{0,60}}?\s+(?:Qty\s*\d+\s+)?({MONEY})", flat, re.I)
    credits = (parse_money(item.group(1)) if item else None) or _money_after(flat, r"\badded\b", 30)
    paid = _stripe_paid(flat)
    if credits is None and paid is None:
        return None
    cur = (credits or paid)[1]
    if paid and credits and paid[1] == credits[1] and paid[0] >= credits[0]:
        total, vat = paid[0], round(paid[0] - credits[0], 2)
    elif paid:
        total, vat = paid[0], _stripe_vat(flat, paid[0])
    else:
        total, vat = credits[0], 0.0
    start = _stripe_date(msg, flat)
    if start is None:
        return None
    return _charge(
        provider="openai_api", store="stripe", merchant=_merchant(flat, "openai", "OpenAI"),
        plan="OpenAI API credits",
        raw_plan=(re.split(r"[.;:!]\s", re.sub(r"\s+Qty\s*\d+$", "",
                                                 item.group(0)[:item.start(1) - item.start()].strip()))[0]
                  if item else subject.strip()),
        receipt_id=_stripe_id(msg, flat, start, total), start=start, end=start + timedelta(days=1),
        total=total, vat=vat, currency=cur,
    )


# ---------- provider: Apple App Store ----------

_CLAUDE_PLAN_RE = re.compile(r"Claude\s+(?:Pro|Max)(?:\s*\(?\s*\d+x\)?)?", re.I)


def parse_apple(msg_or_raw) -> dict | None:
    msg = _as_msg(msg_or_raw)
    if msg is None or APPLE_SENDER not in _header(msg, "From").lower():
        return None
    subject = _header(msg, "Subject")
    flat = _flat(_body_text(msg))
    m_gpt, m_claude = _CHATGPT_PLAN_RE.search(flat), _CLAUDE_PLAN_RE.search(flat)
    if not (m_gpt or m_claude):
        return None
    vendor = "openai" if m_gpt and (not m_claude or m_gpt.start() < m_claude.start()) else "anthropic"
    m = m_gpt if vendor == "openai" else m_claude
    # raw plan = the item title through an optional "(Monthly)"-style suffix
    raw = re.match(r"[^$\n]{0,40}?(?=\s+(?:Renews|Report|\d|" + _CUR + r")|$)", flat[m.start():])
    raw_plan = (raw.group(0) if raw else m.group(0)).strip()
    total = (_money_after(flat, r"\bTOTAL\b", 30, 0) or _money_after(flat, r"\bTotal\b", 30, 0)
             or parse_money(flat[m.end():m.end() + 120]))
    if not total:
        return None
    tax = _money_after(flat, r"\b(?:VAT|GST|Tax)\b", 40, 0)
    start = _date_after(flat, r"\bDATE\b", 40, 0) or _date_after(flat, r"\bDate\b", 40, 0) or _header_date(msg)
    if start is None:
        return None
    renews = _date_after(flat[m.start():], r"Renews", 40)
    end = renews if renews and renews > start else _add_months(start, 1)
    oid = re.search(r"\bORDER\s+ID\b:?\s*([A-Z0-9]{6,})", flat)
    rid = f"apple-{oid.group(1)}" if oid else _synthetic_id(msg, start, total)
    provider = "openai_chatgpt" if vendor == "openai" else "anthropic"
    merchant = _merchant(flat + " " + subject, vendor, "OpenAI OpCo, LLC" if vendor == "openai" else "Anthropic")
    plan = _chatgpt_plan(raw_plan) if vendor == "openai" else _friendly_plan(raw_plan)
    return _charge(provider=provider, store="apple_app_store", merchant=merchant, plan=plan,
                   raw_plan=raw_plan, receipt_id=rid, start=start, end=end,
                   total=total[0], vat=tax[0] if tax and tax[1] == total[1] else 0.0, currency=total[1])


# ---------- provider: Google Play ----------

_GP_ORDER_RE = re.compile(r"Order\s+(?:number|ID)\s*:?\s*(GPA\.[\d-]+(?:\.\.\d+)?|[A-Z0-9][\w.-]{5,})", re.I)
_GP_ITEM_RE = re.compile(
    rf"^(?P<item>[^\n]+?)\s+(?P<money>{MONEY})\s*/\s*(?P<per>month|year|week|mo|yr)\b", re.M | re.I)


def parse_google_play(msg_or_raw) -> dict | None:
    msg = _as_msg(msg_or_raw)
    if msg is None or GOOGLE_PLAY_SENDER not in _header(msg, "From").lower():
        return None
    subject = _header(msg, "Subject")
    text = _body_text(msg)
    flat = _flat(text)
    vendor = _app_vendor(subject + " " + flat)
    if vendor is None:
        return None
    provider = "openai_chatgpt" if vendor == "openai" else "anthropic"
    merchant = _merchant(subject + " " + flat, vendor,
                         "OpenAI OpCo, LLC" if vendor == "openai" else "Anthropic")
    plan_of = _chatgpt_plan if vendor == "openai" else _friendly_plan
    plan_m = _CHATGPT_PLAN_RE.search(flat) if vendor == "openai" else _CLAUDE_PLAN_RE.search(flat)
    s_low, f_low = subject.lower(), flat.lower()

    # Cancellation notice (checked on the subject first so a "declined -> cancelled" mail still ends the period)
    cancel_re = r"(?:will\s+be|has\s+been|was|is)\s+cancell?ed"
    if "cancel" in s_low or re.search(cancel_re, f_low):
        eff = (_date_after(flat, cancel_re, 80) or _date_after(flat, r"cancell?ed", 80)
               or _date_after(flat, r"\buntil\b", 40))
        if eff is None:
            return None
        out = {"kind": "cancellation", "provider": provider, "store": "google_play",
               "merchant": merchant, "effective": eff.isoformat(),
               "account": suggest_account(provider),
               "receipt_id": _synthetic_id(msg, "cancel", eff)}
        if plan_m:
            out["plan"] = plan_of(plan_m.group(0))
        return out
    if re.search(r"declined|couldn.t process|could not process|payment (?:issue|problem|failed)",
                 s_low + " " + f_low):
        return None

    total = _money_after(flat, r"\bTotal\s*:", 40)
    vat = _money_after(flat, r"Includes\s+(?:VAT|tax|GST)\s+of", 40)
    order_d = _date_after(flat, r"Order\s+date\s*:?", 40) or _header_date(msg)
    if order_d is None:
        return None
    oid = _GP_ORDER_RE.search(flat)

    proration = re.search(r"from\s+today\s+to", flat, re.I)
    if proration or "updated your subscription" in s_low:
        end = _date_after(flat, r"from\s+today\s+to", 40)
        if total is None or end is None:
            return None
        new = re.search(r"New\s+Plan\s*:\s*([^\n]+)", text, re.I)
        raw_plan = new.group(1).split(" / ")[-1].strip() if new else (plan_m.group(0) if plan_m else "")
        start = order_d
    else:
        item = _GP_ITEM_RE.search(text)
        if item is None and total is None:
            return None
        if total is None:
            total = parse_money(item.group("money"))
        raw_plan = item.group("item").strip() if item else (plan_m.group(0) if plan_m else "")
        per = (item.group("per").lower() if item else "month")
        start = order_d
        end = (_add_months(start, 12) if per in ("year", "yr")
               else start + timedelta(days=7) if per == "week" else _add_months(start, 1))
    rid = oid.group(1) if oid else _synthetic_id(msg, start, total[0])
    return _charge(provider=provider, store="google_play", merchant=merchant, plan=plan_of(raw_plan),
                   raw_plan=raw_plan, receipt_id=rid, start=start, end=end, total=total[0],
                   vat=vat[0] if vat and vat[1] == total[1] else 0.0, currency=total[1])


# ---------- registry ----------

@dataclass(frozen=True)
class Provider:
    name: str
    query: str
    parse: Callable[[object], dict | None]


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider("anthropic", DEFAULT_QUERY, parse_receipt),
    "openai_chatgpt": Provider(
        "openai_chatgpt",
        "(from:noreply@tm.openai.com OR from:invoice+statements@stripe.com OR from:receipts@openai.com"
        " OR (from:stripe.com OpenAI)) subject:receipt",
        parse_openai_chatgpt),
    "openai_api": Provider(
        "openai_api",
        '(from:openai.com OR (from:stripe.com OpenAI)) (subject:funded OR subject:invoice OR subject:receipt'
        ' OR subject:credit) (API OR credit OR credits)',
        parse_openai_api),
    "apple_app_store": Provider("apple_app_store", f"from:{APPLE_SENDER} (ChatGPT OR Claude)", parse_apple),
    "google_play": Provider(
        "google_play", f"from:{GOOGLE_PLAY_SENDER} (ChatGPT OR Claude OR OpenAI OR Anthropic)",
        parse_google_play),
}


def _select(providers: Iterable[str] | None) -> list[Provider]:
    if providers is None:
        return list(PROVIDERS.values())
    names = [providers] if isinstance(providers, str) else list(providers)
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        raise ValueError(f"unknown provider(s): {', '.join(unknown)}; known: {', '.join(PROVIDERS)}")
    return [PROVIDERS[n] for n in names]


def parse_any(msg_or_raw, providers: Iterable[str] | None = None) -> dict | None:
    """First non-None parse across providers (charge or cancellation)."""
    msg = _as_msg(msg_or_raw)
    if msg is None:
        return None
    for p in _select(providers):
        try:
            rec = p.parse(msg)
        except Exception:
            rec = None
        if rec:
            return rec
    return None


def apply_cancellations(charges: list[dict], cancellations: list[dict]) -> list[dict]:
    """Clamp `end` of every charge (same provider + account) whose period runs past a cancellation's
    effective date. Pure: returns new dicts, inputs untouched. Charges starting on/after the
    effective date (a later re-subscription) are left alone."""
    cuts = [(c.get("provider"), c.get("account"), c.get("effective")) for c in cancellations
            if c.get("kind", "cancellation") == "cancellation" and c.get("effective")]
    out = []
    for ch in charges:
        ch = dict(ch)
        for prov, acct, eff in cuts:
            if (ch.get("provider") == prov and ch.get("account") == acct
                    and ch.get("start", "") < eff < ch.get("end", "")):
                ch["end"] = eff
                ch["cancelled_effective"] = eff
        out.append(ch)
    return out


def _dedupe(rows: list[dict]) -> list[dict]:
    """By (kind, receipt_id); then drop a synthetic-id ('msg-') charge that duplicates a real receipt
    (same provider, start, amount and currency), e.g. an OpenAI 'funded' notice + its Stripe receipt."""
    seen, out = set(), []
    for r in rows:
        key = (r.get("kind"), r.get("receipt_id"))
        if "_error" in r or key not in seen:
            seen.add(key)
            out.append(r)
    real = {(r.get("provider"), r.get("start"), r.get("amount_original"), r.get("currency"))
            for r in out if r.get("kind") == "charge" and not str(r.get("receipt_id", "")).startswith("msg-")}
    return [r for r in out if not (r.get("kind") == "charge" and str(r.get("receipt_id", "")).startswith("msg-")
                                   and (r.get("provider"), r.get("start"), r.get("amount_original"),
                                        r.get("currency")) in real)]


# ---------- Gmail OAuth + fetch ----------

class GmailAuthError(RuntimeError):
    """Token missing/expired/unrefreshable and interactive OAuth was not allowed."""


def build_service(creds_path: Path = DEFAULT_CREDS, token_path: Path = DEFAULT_TOKEN, port: int = 0,
                  interactive: bool = True):
    """Create an authenticated Gmail API client. Triggers OAuth on first run when interactive;
    with interactive=False raises GmailAuthError instead of opening a browser."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        except Exception:
            creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise GmailAuthError(
                f"Gmail token at {token_path} is missing, expired or could not be refreshed. "
                "Reconnect Gmail (dashboard 'Connect Gmail' or `python gmail_scraper.py scrape`).")
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Missing OAuth client credentials. Place credentials.json at {creds_path}.\n"
                "See README.md → 'Connect Gmail' for the 5-minute setup."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
        creds = flow.run_local_server(port=port, open_browser=True, prompt="consent")
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_receipt_ids(service, query: str = DEFAULT_QUERY, max_results: int = 200) -> list[str]:
    """Return Gmail message IDs matching a query (default: the Anthropic invoice query)."""
    ids: list[str] = []
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=min(100, max_results - len(ids)),
            pageToken=page_token,
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token or len(ids) >= max_results:
            break
    return ids[:max_results]


def fetch_raw(service, message_id: str) -> bytes:
    msg = service.users().messages().get(
        userId="me", id=message_id, format="raw"
    ).execute()
    return base64.urlsafe_b64decode(msg["raw"].encode("ascii"))


def scrape_all(service, providers: Iterable[str] | None = None, query: str | None = None) -> list[dict]:
    """Run each provider's query and parser. Returns charges, cancellations and error rows
    ({"_error", "_message_id", "_provider"}), deduped by receipt_id across providers.
    `query` (legacy) runs that single query and parses each hit with every selected provider."""
    selected = _select(providers)
    jobs = [(None, query)] if query is not None else [(p, p.query) for p in selected]
    raw_cache: dict[str, bytes] = {}
    out: list[dict] = []
    for prov, q in jobs:
        for mid in list_receipt_ids(service, query=q):
            try:
                if mid not in raw_cache:
                    raw_cache[mid] = fetch_raw(service, mid)
                rec = (prov.parse(raw_cache[mid]) if prov
                       else parse_any(raw_cache[mid], [p.name for p in selected]))
                if rec:
                    out.append(rec)
            except Exception as e:
                out.append({"_error": str(e), "_message_id": mid, "_provider": prov.name if prov else None})
    return _dedupe(out)


def dry_run(service, providers: Iterable[str] | None = None, max_results: int = 50) -> dict:
    """Per provider: ONE list call, From/Subject/Date headers and the parsed records. Never returns
    message bodies and never writes billing.json."""
    report: dict = {}
    for p in _select(providers):
        entry = {"query": p.query, "count": 0, "messages": [], "parsed": [], "errors": []}
        resp = service.users().messages().list(userId="me", q=p.query, maxResults=max_results).execute()
        ids = [m["id"] for m in resp.get("messages", [])]
        entry["count"] = len(ids)
        for mid in ids:
            try:
                meta = service.users().messages().get(
                    userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                hdrs = {h.get("name", "").lower(): h.get("value", "")
                        for h in (meta.get("payload") or {}).get("headers", [])}
                entry["messages"].append({k: hdrs.get(k, "") for k in ("from", "subject", "date")})
                rec = p.parse(fetch_raw(service, mid))
                if rec:
                    entry["parsed"].append(rec)
            except Exception as e:
                entry["errors"].append({"message_id": mid, "error": str(e)})
        report[p.name] = entry
    return report


def scrape_and_save(creds_path: Path = DEFAULT_CREDS, token_path: Path = DEFAULT_TOKEN) -> dict:
    from billing import merge_scraped
    service = build_service(creds_path, token_path)
    rows = scrape_all(service)
    errors = [r for r in rows if "_error" in r]
    cancellations = [r for r in rows if r.get("kind") == "cancellation"]
    charges = [r for r in rows if "_error" not in r and r.get("kind", "charge") == "charge"]
    unconverted = [r for r in charges if r.get("amount_usd") is None]
    valid = apply_cancellations([r for r in charges if r.get("amount_usd") is not None], cancellations)
    res = merge_scraped(valid)
    res["found"] = len(valid)
    res["errors"] = len(errors) + len(unconverted)
    res["cancellations"] = cancellations
    return res


_REFRESH_TTL = 600.0  # seconds a refresh verdict is cached
_refresh_cache: dict[str, tuple[float, bool, str | None]] = {}


def _short_error(e: Exception) -> str:
    """One short line for the UI; token-looking strings are redacted, nothing else is logged."""
    msg = re.sub(r"[A-Za-z0-9_\-\.]{30,}", "[redacted]", " ".join(str(e).split()))
    return (msg or type(e).__name__)[:120]


def check_refresh(token_path: Path = DEFAULT_TOKEN, force: bool = False) -> tuple[bool, str | None]:
    """(refresh_ok, error): can the stored token still be used? Loads token.json and refreshes it when
    the access token is expired (a live token needs no network call). Cached for ~10 minutes per path."""
    import time
    key = str(token_path)
    hit = _refresh_cache.get(key)
    if hit and not force and time.monotonic() - hit[0] < _REFRESH_TTL:
        return hit[1], hit[2]
    ok, err = False, None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        if creds.valid:
            ok = True
        elif creds.refresh_token:
            creds.refresh(Request())
            ok = bool(creds.valid)
            err = None if ok else "token not valid after refresh"
        else:
            err = "no refresh token"
    except Exception as e:
        ok, err = False, _short_error(e)
    _refresh_cache[key] = (time.monotonic(), ok, err)
    return ok, err


def status(creds_path: Path = DEFAULT_CREDS, token_path: Path = DEFAULT_TOKEN) -> dict:
    out = {"credentials_present": creds_path.exists(), "credentials_path": str(creds_path),
           "connected": token_path.exists(), "token_path": str(token_path),
           "refresh_ok": None, "error": None}
    if out["connected"]:
        out["refresh_ok"], out["error"] = check_refresh(token_path)
        out["connected"] = bool(out["refresh_ok"])
    return out


USAGE = ("usage: gmail_scraper.py {parse <eml>|scrape|status|dry-run [provider ...]}\n"
         f"providers: {', '.join(PROVIDERS)}")

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) >= 2 else ""
    if cmd == "parse" and len(sys.argv) >= 3:
        # python gmail_scraper.py parse path/to/receipt.eml
        print(json.dumps(parse_any(Path(sys.argv[2]).read_bytes()), indent=2, ensure_ascii=False))
    elif cmd == "scrape":
        print(json.dumps(scrape_and_save(), indent=2, ensure_ascii=False))
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "dry-run":
        try:
            svc = build_service(interactive=False)
            print(json.dumps(dry_run(svc, sys.argv[2:] or None), indent=2, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}, indent=2))
            sys.exit(1)
    else:
        print(USAGE)
        sys.exit(2 if cmd else 0)
