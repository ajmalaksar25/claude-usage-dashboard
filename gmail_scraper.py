"""Gmail OAuth + Anthropic receipt scraper.

Public API:
  build_service(creds_path, token_path) -> Gmail service (runs OAuth if needed)
  parse_receipt(raw_bytes) -> dict | None     (parses an .eml/raw RFC822 receipt)
  scrape_all(service, query=...)  -> list[dict]
  scrape_and_save(creds_path, token_path) -> {found, added, updated, total}

Designed so it can be tested standalone against an .eml file with no Google
dependencies (parse_receipt only needs stdlib).
"""
from __future__ import annotations

import base64
import json
import re
from datetime import date
from email import message_from_bytes
from email.policy import default as email_policy
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).parent
DEFAULT_CREDS = ROOT / "credentials.json"
DEFAULT_TOKEN = ROOT / "token.json"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
ANTHROPIC_SENDER = "invoice+statements@mail.anthropic.com"
DEFAULT_QUERY = f"from:{ANTHROPIC_SENDER}"

# ---------- email parsing (stdlib only) ----------

_MONTH = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}

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


def _normalize_text(s: str) -> str:
    # collapse soft-wrapped quoted-printable artifacts already handled by email module,
    # but also strip soft hyphens, replace en-dashes with simple dashes for splitting
    s = s.replace(" ", " ").replace(" ", " ")
    s = re.sub(r"\s+", " ", s)
    return s


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


def parse_receipt(raw_bytes: bytes) -> dict | None:
    """Parse a raw RFC822 .eml receipt from Anthropic. Return charge dict or None."""
    if not raw_bytes:
        return None
    try:
        msg = message_from_bytes(raw_bytes, policy=email_policy)
    except Exception:
        return None
    sender = (msg.get("From") or "").lower()
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
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(msg.get("Date"))
            start = dt.date()
            end = start.replace(month=start.month % 12 + 1) if start.month < 12 else date(start.year + 1, 1, start.day)
        except Exception:
            return None
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

    # Friendly plan label
    plan_norm = _friendly_plan(plan_name)

    return {
        "receipt_id": receipt_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "plan": plan_norm,
        "raw_plan": plan_name,
        "amount_usd": round(amount, 2),
        "source": "gmail",
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


# ---------- Gmail OAuth + fetch ----------

def build_service(creds_path: Path = DEFAULT_CREDS, token_path: Path = DEFAULT_TOKEN, port: int = 0):
    """Create an authenticated Gmail API client. Triggers OAuth on first run."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
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
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Missing OAuth client credentials. Place credentials.json at {creds_path}.\n"
                "See README.md → 'Connect Gmail' for the 5-minute setup."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
        creds = flow.run_local_server(port=port, open_browser=True, prompt="consent")
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_receipt_ids(service, query: str = DEFAULT_QUERY, max_results: int = 200) -> list[str]:
    """Return Gmail message IDs matching the Anthropic invoice query."""
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


def scrape_all(service, query: str = DEFAULT_QUERY) -> list[dict]:
    out = []
    seen = set()
    for mid in list_receipt_ids(service, query=query):
        try:
            raw = fetch_raw(service, mid)
            charge = parse_receipt(raw)
            if charge and charge["receipt_id"] not in seen:
                out.append(charge)
                seen.add(charge["receipt_id"])
        except Exception as e:
            out.append({"_error": str(e), "_message_id": mid})
    return out


def scrape_and_save(creds_path: Path = DEFAULT_CREDS, token_path: Path = DEFAULT_TOKEN) -> dict:
    from billing import merge_scraped
    service = build_service(creds_path, token_path)
    rows = scrape_all(service)
    valid = [r for r in rows if "_error" not in r]
    errors = [r for r in rows if "_error" in r]
    res = merge_scraped(valid)
    res["found"] = len(valid)
    res["errors"] = len(errors)
    return res


def status(creds_path: Path = DEFAULT_CREDS, token_path: Path = DEFAULT_TOKEN) -> dict:
    return {
        "credentials_present": creds_path.exists(),
        "credentials_path": str(creds_path),
        "connected": token_path.exists(),
        "token_path": str(token_path),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "parse":
        # python gmail_scraper.py parse path/to/receipt.eml
        p = Path(sys.argv[2])
        out = parse_receipt(p.read_bytes())
        print(json.dumps(out, indent=2))
    elif len(sys.argv) >= 2 and sys.argv[1] == "scrape":
        print(json.dumps(scrape_and_save(), indent=2))
    elif len(sys.argv) >= 2 and sys.argv[1] == "status":
        print(json.dumps(status(), indent=2))
    else:
        print("usage: gmail_scraper.py {parse <eml>|scrape|status}")
