"""FastAPI dashboard server.

Endpoints all accept ?window=all|1y|6m|3m|1m|15d|1w|today (or explicit
?from=YYYY-MM-DD&to=YYYY-MM-DD) and return JSON. The HTML shell is served at /.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from billing import subscription_cost
from indexer import reindex
import gmail_scraper

ROOT = Path(__file__).parent
DB_PATH = ROOT / "usage.db"
HOST = "127.0.0.1"
PORT = 8765

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


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _resolve_window(window: str | None, frm: str | None, to: str | None) -> tuple[str, str, str]:
    """Return (from_iso, to_iso, label) -- both bounds in UTC ISO."""
    now = datetime.now(timezone.utc)
    if frm and to:
        f = datetime.fromisoformat(frm).replace(tzinfo=timezone.utc)
        t = datetime.fromisoformat(to).replace(tzinfo=timezone.utc) + timedelta(days=1)
        return f.isoformat(), t.isoformat(), f"{frm}..{to}"

    window = (window or "all").lower()
    if window == "today":
        local = datetime.now().astimezone()
        local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        f = local_start.astimezone(timezone.utc)
        t = (local_start + timedelta(days=1)).astimezone(timezone.utc)
        return f.isoformat(), t.isoformat(), "today"

    days = WINDOWS.get(window)
    if days is None:
        return "1970-01-01T00:00:00+00:00", (now + timedelta(days=1)).isoformat(), "all"
    f = (now - timedelta(days=days))
    return f.isoformat(), (now + timedelta(days=1)).isoformat(), window


def _qparams(req: Request) -> tuple[str, str, str]:
    q = req.query_params
    return _resolve_window(q.get("window"), q.get("from"), q.get("to"))


# ---------- aggregate queries ----------

def q_summary(frm: str, to: str) -> dict:
    with _conn() as c:
        row = c.execute(
            """
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
              MIN(ts)                                     AS first_ts,
              MAX(ts)                                     AS last_ts
            FROM messages
            WHERE ts >= ? AND ts < ?
            """,
            (frm, to),
        ).fetchone()
    d = dict(row) if row else {}
    d["total_tokens"] = (
        d.get("input_tokens", 0)
        + d.get("output_tokens", 0)
        + d.get("cache_5m_write", 0)
        + d.get("cache_1h_write", 0)
        + d.get("cache_read", 0)
    )
    sub = subscription_cost(frm, to)
    d["subscription_cost"] = sub["total"]
    d["billing_charges"] = sub["charges"]
    d["billing_coverage"] = sub["coverage"]
    d["api_cost"] = round(float(d.get("api_cost") or 0.0), 2)
    d["savings"] = round(d["api_cost"] - d["subscription_cost"], 2)
    d["multiplier"] = (
        round(d["api_cost"] / d["subscription_cost"], 1)
        if d["subscription_cost"] > 0 else None
    )
    return d


def q_timeseries(frm: str, to: str, bucket: str) -> list[dict]:
    grp = "ts_day" if bucket == "day" else (
        "substr(ts,1,13)" if bucket == "hour" else "ts_day"
    )
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
              COALESCE(SUM(cost_usd),0)       AS cost
            FROM messages
            WHERE ts >= ? AND ts < ?
            GROUP BY bucket
            ORDER BY bucket
            """,
            (frm, to),
        ).fetchall()
    return [dict(r) for r in rows]


def q_by_model(frm: str, to: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT
              model,
              tier,
              COUNT(*) AS msgs,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost
            FROM messages
            WHERE ts >= ? AND ts < ?
            GROUP BY model, tier
            ORDER BY tokens DESC
            """,
            (frm, to),
        ).fetchall()
    return [dict(r) for r in rows]


def q_by_project(frm: str, to: str, limit: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT
              project,
              MAX(project_path) AS project_path,
              COUNT(*)          AS msgs,
              COUNT(DISTINCT session_id) AS sessions,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost,
              MAX(ts) AS last_ts
            FROM messages
            WHERE ts >= ? AND ts < ?
            GROUP BY project
            ORDER BY tokens DESC
            LIMIT ?
            """,
            (frm, to, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def q_heatmap(frm: str, to: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT
              ts_day AS day,
              COUNT(*) AS msgs,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost
            FROM messages
            WHERE ts >= ? AND ts < ?
            GROUP BY ts_day
            ORDER BY ts_day
            """,
            (frm, to),
        ).fetchall()
    return [dict(r) for r in rows]


def q_distributions(frm: str, to: str) -> dict:
    with _conn() as c:
        hours = c.execute(
            """SELECT ts_hour AS hour, COUNT(*) AS msgs,
                      COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens
               FROM messages WHERE ts >= ? AND ts < ? GROUP BY ts_hour ORDER BY ts_hour""",
            (frm, to),
        ).fetchall()
        dows = c.execute(
            """SELECT ts_dow AS dow, COUNT(*) AS msgs,
                      COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens
               FROM messages WHERE ts >= ? AND ts < ? GROUP BY ts_dow ORDER BY ts_dow""",
            (frm, to),
        ).fetchall()
    return {"hours": [dict(r) for r in hours], "dows": [dict(r) for r in dows]}


def q_top_sessions(frm: str, to: str, by: str, limit: int) -> list[dict]:
    order_col = {
        "msgs": "msgs",
        "tokens": "tokens",
        "cost": "cost",
        "duration": "duration_minutes",
    }.get(by, "tokens")
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              session_id,
              MAX(project) AS project,
              MAX(project_path) AS project_path,
              MAX(model)   AS model,
              COUNT(*)     AS msgs,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost,
              MIN(ts) AS first_ts,
              MAX(ts) AS last_ts,
              CAST((julianday(MAX(ts)) - julianday(MIN(ts))) * 24 * 60 AS INTEGER) AS duration_minutes
            FROM messages
            WHERE ts >= ? AND ts < ? AND session_id IS NOT NULL
            GROUP BY session_id
            ORDER BY {order_col} DESC
            LIMIT ?
            """,
            (frm, to, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def q_top_days(frm: str, to: str, limit: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT
              ts_day AS day,
              COUNT(*) AS msgs,
              COUNT(DISTINCT session_id) AS sessions,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost
            FROM messages
            WHERE ts >= ? AND ts < ?
            GROUP BY ts_day
            ORDER BY tokens DESC
            LIMIT ?
            """,
            (frm, to, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def q_meta() -> dict:
    if not DB_PATH.exists():
        return {"last_index_at": None, "rows": 0}
    with _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k='last_index_at'").fetchone()
        n = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return {"last_index_at": (r[0] if r else None), "rows": n}


# ---------- app ----------

app = FastAPI(title="Claude Usage Dashboard")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/summary")
def api_summary(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "from": f, "to": t, **q_summary(f, t)}


@app.get("/api/timeseries")
def api_timeseries(request: Request):
    f, t, label = _qparams(request)
    bucket = request.query_params.get("bucket", "day")
    return {"window": label, "bucket": bucket, "rows": q_timeseries(f, t, bucket)}


@app.get("/api/by_model")
def api_by_model(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_by_model(f, t)}


@app.get("/api/by_project")
def api_by_project(request: Request):
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "20"))
    return {"window": label, "rows": q_by_project(f, t, limit)}


@app.get("/api/heatmap")
def api_heatmap(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_heatmap(f, t)}


@app.get("/api/distributions")
def api_distributions(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, **q_distributions(f, t)}


@app.get("/api/top_sessions")
def api_top_sessions(request: Request):
    f, t, label = _qparams(request)
    by = request.query_params.get("by", "tokens")
    limit = int(request.query_params.get("limit", "20"))
    return {"window": label, "by": by, "rows": q_top_sessions(f, t, by, limit)}


@app.get("/api/top_days")
def api_top_days(request: Request):
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "10"))
    return {"window": label, "rows": q_top_days(f, t, limit)}


@app.get("/api/meta")
def api_meta():
    return q_meta()


@app.post("/refresh")
def refresh():
    return reindex(DB_PATH, verbose=False)


@app.get("/api/gmail/status")
def api_gmail_status():
    return gmail_scraper.status()


@app.post("/api/gmail/scrape")
def api_gmail_scrape():
    try:
        return gmail_scraper.scrape_and_save()
    except FileNotFoundError as e:
        return JSONResponse({"error": "missing_credentials", "detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "scrape_failed", "detail": str(e)}, status_code=500)


def _open_browser_later():
    threading.Timer(0.8, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()


def main():
    print(f"[startup] indexing logs (incremental)...")
    summary = reindex(DB_PATH)
    print(f"[startup] ready. {summary['rows_total']} rows in DB.")
    print(f"[startup] open http://{HOST}:{PORT}")
    if "--no-browser" not in sys.argv:
        _open_browser_later()
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
