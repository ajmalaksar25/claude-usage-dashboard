"""FastAPI dashboard server.

Endpoints all accept ?window=all|1y|6m|3m|1m|15d|1w|today (or explicit
?from=YYYY-MM-DD&to=YYYY-MM-DD) and return JSON. The HTML shell is served at /.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
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
import ccm
import gmail_scraper

ROOT = Path(__file__).parent
DB_PATH = ROOT / "usage.db"
HOST = "127.0.0.1"
PORT = 8765

# Set by main() at startup based on `--all` CLI flag. Persisted across /refresh.
EXTRAS_ON = False

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


def _acct(req: Request) -> str:
    """Account filter from ?account=... ('' or 'all' means no filter)."""
    a = (req.query_params.get("account") or "").strip()
    return "" if a.lower() == "all" else a


def _af(acct: str) -> tuple[str, tuple]:
    """(SQL fragment, extra params) for an optional account filter."""
    return (" AND account = ?", (acct,)) if acct else ("", ())


# ---------- aggregate queries ----------

def q_summary(frm: str, to: str, acct: str = "") -> dict:
    frag, ap = _af(acct)
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
              MIN(ts)                                     AS first_ts,
              MAX(ts)                                     AS last_ts
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            """,
            (frm, to, *ap),
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


def q_timeseries(frm: str, to: str, bucket: str, acct: str = "") -> list[dict]:
    grp = "ts_day" if bucket == "day" else (
        "substr(ts,1,13)" if bucket == "hour" else "ts_day"
    )
    frag, ap = _af(acct)
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
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY bucket
            ORDER BY bucket
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_by_model(frm: str, to: str, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              model,
              tier,
              COUNT(*) AS msgs,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY model, tier
            ORDER BY tokens DESC
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_by_project(frm: str, to: str, limit: int, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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


def q_heatmap(frm: str, to: str, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              ts_day AS day,
              COUNT(*) AS msgs,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY ts_day
            ORDER BY ts_day
            """,
            (frm, to, *ap),
        ).fetchall()
    return [dict(r) for r in rows]


def q_distributions(frm: str, to: str, acct: str = "") -> dict:
    frag, ap = _af(acct)
    with _conn() as c:
        hours = c.execute(
            f"""SELECT ts_hour AS hour, COUNT(*) AS msgs,
                      COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens
               FROM messages WHERE ts >= ? AND ts < ?{frag} GROUP BY ts_hour ORDER BY ts_hour""",
            (frm, to, *ap),
        ).fetchall()
        dows = c.execute(
            f"""SELECT ts_dow AS dow, COUNT(*) AS msgs,
                      COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens
               FROM messages WHERE ts >= ? AND ts < ?{frag} GROUP BY ts_dow ORDER BY ts_dow""",
            (frm, to, *ap),
        ).fetchall()
    return {"hours": [dict(r) for r in hours], "dows": [dict(r) for r in dows]}


def q_top_sessions(frm: str, to: str, by: str, limit: int, acct: str = "") -> list[dict]:
    order_col = {
        "msgs": "msgs",
        "tokens": "tokens",
        "cost": "cost",
        "duration": "duration_minutes",
    }.get(by, "tokens")
    frag, ap = _af(acct)
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
            WHERE ts >= ? AND ts < ?{frag} AND session_id IS NOT NULL
            GROUP BY session_id
            ORDER BY {order_col} DESC
            LIMIT ?
            """,
            (frm, to, *ap, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def q_top_days(frm: str, to: str, limit: int, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              ts_day AS day,
              COUNT(*) AS msgs,
              COUNT(DISTINCT session_id) AS sessions,
              COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens,
              COALESCE(SUM(cost_usd),0) AS cost
            FROM messages
            WHERE ts >= ? AND ts < ?{frag}
            GROUP BY ts_day
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
    }


# ---------- extras queries ----------

def _extras_table_present() -> bool:
    if not DB_PATH.exists():
        return False
    with _conn() as c:
        r = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
        ).fetchone()
    return r is not None


def q_extras_skills(frm: str, to: str, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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


def q_extras_tools(frm: str, to: str, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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


def q_extras_mcp(frm: str, to: str, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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


def q_extras_agents(frm: str, to: str, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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


def q_extras_slash(frm: str, to: str, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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


def q_extras_files(frm: str, to: str, limit: int, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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


def q_extras_bash(frm: str, to: str, limit: int, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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


def q_extras_errors(frm: str, to: str, acct: str = "") -> list[dict]:
    frag, ap = _af(acct)
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
) -> dict:
    """Individual tool calls, newest first, with total for pagination."""
    where = ["ts >= ?", "ts < ?"]
    params: list[Any] = [frm, to]
    if acct:
        where.append("account = ?")
        params.append(acct)
    if tool:
        where.append("tool_name = ?")
        params.append(tool)
    if status == "errors":
        where.append("is_error = 1")
    cond = " AND ".join(where)
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


def q_extras_overview(frm: str, to: str, acct: str = "") -> dict:
    """Single-roundtrip aggregate for the Activity tab."""
    frag, ap = _af(acct)
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
            f"FROM slash_prompts WHERE ts >= ? AND ts < ?{frag}",
            (frm, to, *ap),
        ).fetchone()
    d = dict(tot) if tot else {}
    d["slash_prompts"] = sp["n"] if sp else 0
    d["distinct_slash_commands"] = sp["distinct_commands"] if sp else 0
    return d


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
    return {"window": label, "from": f, "to": t, **q_summary(f, t, _acct(request))}


@app.get("/api/timeseries")
def api_timeseries(request: Request):
    f, t, label = _qparams(request)
    bucket = request.query_params.get("bucket", "day")
    return {"window": label, "bucket": bucket, "rows": q_timeseries(f, t, bucket, _acct(request))}


@app.get("/api/by_model")
def api_by_model(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_by_model(f, t, _acct(request))}


@app.get("/api/by_project")
def api_by_project(request: Request):
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "20"))
    return {"window": label, "rows": q_by_project(f, t, limit, _acct(request))}


@app.get("/api/heatmap")
def api_heatmap(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_heatmap(f, t, _acct(request))}


@app.get("/api/distributions")
def api_distributions(request: Request):
    f, t, label = _qparams(request)
    return {"window": label, **q_distributions(f, t, _acct(request))}


@app.get("/api/top_sessions")
def api_top_sessions(request: Request):
    f, t, label = _qparams(request)
    by = request.query_params.get("by", "tokens")
    limit = int(request.query_params.get("limit", "20"))
    return {"window": label, "by": by, "rows": q_top_sessions(f, t, by, limit, _acct(request))}


@app.get("/api/top_days")
def api_top_days(request: Request):
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "10"))
    return {"window": label, "rows": q_top_days(f, t, limit, _acct(request))}


@app.get("/api/meta")
def api_meta():
    m = q_meta()
    m["extras_enabled"] = EXTRAS_ON
    return m


@app.get("/api/accounts")
def api_accounts():
    """Accounts present in the DB, with row counts and activity span."""
    if not DB_PATH.exists():
        return {"accounts": []}
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT account AS name, COUNT(*) AS msgs, "
                "MIN(ts) AS first_ts, MAX(ts) AS last_ts "
                "FROM messages GROUP BY account ORDER BY msgs DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return {"accounts": []}
    return {"accounts": [dict(r) for r in rows]}


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
    """Every Claude config profile on this machine, with link + usage status."""
    usage: dict[str, dict] = {}
    if DB_PATH.exists():
        try:
            with _conn() as c:
                for r in c.execute(
                    "SELECT account, COUNT(*) AS msgs, "
                    "COALESCE(SUM(input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read),0) AS tokens, "
                    "COALESCE(SUM(cost_usd),0) AS cost "
                    "FROM messages GROUP BY account"
                ):
                    usage[r["account"]] = {
                        "msgs": r["msgs"], "tokens": r["tokens"],
                        "cost": round(r["cost"], 2),
                    }
        except sqlite3.OperationalError:
            pass
    profiles = []
    for name, path in ccm.discover_profiles():
        shared = {d: ccm.dir_status(path, d) for d in ccm.SHARED_DIRS}
        profiles.append({
            "name": name,
            "path": str(path),
            "credentials": (path / ".credentials.json").exists(),
            "sessions": ccm.session_count(path),
            "shared": shared,
            "linked": sum(1 for s in shared.values() if s == "linked"),
            "alias": {
                "zsh": ccm.alias_lines(name, "zsh"),
                "powershell": ccm.alias_lines(name, "powershell"),
            },
            "usage": usage.get(name),
        })
    shared_root = ccm.shared_home()
    return {
        "profiles": profiles,
        "shared_root": str(shared_root),
        "shared_exists": shared_root.is_dir(),
        "shared_dirs": list(ccm.SHARED_DIRS),
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


@app.post("/refresh")
def refresh():
    return reindex(DB_PATH, extras=EXTRAS_ON, verbose=False)


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
def api_extras_overview(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, **q_extras_overview(f, t, _acct(request))}


@app.get("/api/extras/skills")
def api_extras_skills(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_skills(f, t, _acct(request))}


@app.get("/api/extras/tools")
def api_extras_tools(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_tools(f, t, _acct(request))}


@app.get("/api/extras/mcp")
def api_extras_mcp(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_mcp(f, t, _acct(request))}


@app.get("/api/extras/agents")
def api_extras_agents(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_agents(f, t, _acct(request))}


@app.get("/api/extras/slash")
def api_extras_slash(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_slash(f, t, _acct(request))}


@app.get("/api/extras/files")
def api_extras_files(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "30"))
    return {"window": label, "rows": q_extras_files(f, t, limit, _acct(request))}


@app.get("/api/extras/bash")
def api_extras_bash(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    limit = int(request.query_params.get("limit", "30"))
    return {"window": label, "rows": q_extras_bash(f, t, limit, _acct(request))}


@app.get("/api/extras/errors")
def api_extras_errors(request: Request):
    fail = _extras_ready_resp()
    if fail is not None:
        return fail
    f, t, label = _qparams(request)
    return {"window": label, "rows": q_extras_errors(f, t, _acct(request))}


@app.get("/api/extras/calls")
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
        **q_extras_calls(f, t, tool, status, limit, offset, _acct(request)),
    }


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
    global EXTRAS_ON
    EXTRAS_ON = "--all" in sys.argv
    if EXTRAS_ON:
        print("[startup] extras mode ON — indexing tool calls and slash prompts")
    print(f"[startup] indexing logs (incremental)...")
    summary = reindex(DB_PATH, extras=EXTRAS_ON)
    print(f"[startup] ready. {summary['rows_total']} rows in DB.")
    if EXTRAS_ON:
        print(f"[startup] extras: {summary.get('extras_rows_total', 0)} tool calls indexed")
    print(f"[startup] open http://{HOST}:{PORT}")
    if "--no-browser" not in sys.argv:
        _open_browser_later()
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
