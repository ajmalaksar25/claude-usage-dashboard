"""Incremental indexer: ~/.claude/projects/**/*.jsonl -> SQLite.

Only re-parses files whose (size, mtime_ns) changed since the last run.
Dedupes assistant messages by Anthropic message id (handles resumed sessions).
Pricing comes from pricing.py; project name is derived from the message's cwd.

Every row carries its account (config dir) and a source priority. When the same
Anthropic message id shows up in several config dirs the highest-priority copy
wins, so a later scan of a lower-priority copy cannot steal the canonical account.

When called with extras=True (CLI: --all), also indexes tool calls and slash
prompts into separate tables. Extras are tracked per file in extras_files so
the toggle is incremental on its own track.

v7: every row carries the coding TOOL that produced it. Besides Claude Code ('claude-code') two
other agents are ingested into the same messages table:
  codex-cli  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl ($CODEX_HOME, ~/.codex-*). Usage comes
             from event_msg/token_count records whose total_token_usage is CUMULATIVE per session,
             so one row per token_count = the delta of the running total (repeats are skipped).
             A forked rollout (subagent / fork: first session_meta carries forked_from_id) starts
             with a replay of the parent's history, stamped at the fork instant; those copied
             token_counts only set the baseline, so the parent's usage is not counted twice.
             OpenAI semantics: input_tokens INCLUDES cached_input_tokens; output INCLUDES reasoning.
  pi         ~/.pi/agent/sessions/<cwd>/<ts>_<id>.jsonl ($PI_CODING_AGENT_DIR): per-message usage
             {input (uncached), output, cacheRead, cacheWrite}; deduped by responseId.
Cursor (~/.cursor agent-transcripts) and Gemini/Antigravity transcripts carry no token usage, so
they are not ingested.

v8: every row carries the response's stop_reason and an outcome (ok | aborted | error) where the
source records one (Pi: stopReason "aborted"/"error"). A failed request that reported no usage is
not a message: it goes to failed_requests, not messages. Messages whose usage was never reported
(a gateway that drops it) get a flagged ESTIMATE in separate est_* columns (see estimate_usage);
the measured token/cost columns are never touched by it.
"""
from __future__ import annotations

import bisect
import json
import os
import re
import shlex
import sqlite3
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pricing

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
# Canonical multi-profile layout (CLAUDE_CONFIG_DIR aliases). Higher priority wins on
# duplicate message ids. Used when accounts.json is absent; the ~/.claude* glob is the
# fallback for machines that use a different convention.
ACCOUNT_ROOTS = [
    (Path.home() / ".claude-work" / "projects", "work", 40),
    (Path.home() / ".claude-codex" / "projects", "claudex", 30),
    (Path.home() / ".claude-personal" / "projects", "personal", 20),
    (Path.home() / ".claude" / "projects", "personal", 10),
]
# Other coding agents (see module doc). Account per tool/config dir; priority only orders the walk.
CODEX_HOMES_GLOB = ".codex*"
PI_HOME = Path.home() / ".pi" / "agent"
TOOL_CLAUDE, TOOL_CODEX, TOOL_PI = "claude-code", "codex-cli", "pi"
TOOLS = (TOOL_CLAUDE, TOOL_CODEX, TOOL_PI)
SCHEMA_VERSION = "8"
SYNTHETIC_MODEL = "<synthetic>"  # client-side placeholder turns (errors, interrupts): no API call, no tokens

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
  msg_id          TEXT PRIMARY KEY,
  ts              TEXT NOT NULL,
  ts_day          TEXT NOT NULL,
  ts_hour         INTEGER NOT NULL,
  ts_dow          INTEGER NOT NULL,
  session_id      TEXT,
  project         TEXT,
  project_path    TEXT,
  model           TEXT,
  tier            TEXT,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  cache_5m_write  INTEGER,
  cache_1h_write  INTEGER,
  cache_read      INTEGER,
  cost_usd        REAL,
  speed           TEXT DEFAULT 'standard',
  inference_geo   TEXT,
  web_search_requests INTEGER DEFAULT 0,
  web_fetch_requests  INTEGER DEFAULT 0,
  fast_premium_usd    REAL DEFAULT 0,
  server_tool_cost_usd REAL DEFAULT 0,
  price_estimated INTEGER DEFAULT 0,
  account         TEXT NOT NULL DEFAULT 'personal',
  source_priority INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_day       ON messages(ts_day);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project, ts);
CREATE INDEX IF NOT EXISTS idx_messages_model     ON messages(model);

CREATE TABLE IF NOT EXISTS files (
  path      TEXT PRIMARY KEY,
  size      INTEGER,
  mtime_ns  INTEGER,
  rows      INTEGER,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- v5 side tables. Every one is keyed so a record copied into several transcripts (resumed
-- sessions, several config dirs) lands once.

-- Claude Code's own cost tracker (type "cost-state"), one row per session *process*: the record
-- is cumulative within a process (keyed by startTime) and restarts when the session is resumed,
-- so the session total is the sum over its processes of each process's latest record.
CREATE TABLE IF NOT EXISTS session_costs (
  session_id            TEXT NOT NULL,
  start_time_ms         INTEGER NOT NULL,
  start_ts              TEXT,
  total_cost_usd        REAL,
  total_api_duration_ms INTEGER,
  total_duration_ms     INTEGER,
  lines_added           INTEGER,
  lines_removed         INTEGER,
  has_unknown_model_cost INTEGER DEFAULT 0,
  account               TEXT,
  PRIMARY KEY (session_id, start_time_ms)
);
CREATE TABLE IF NOT EXISTS session_cost_models (
  session_id      TEXT NOT NULL,
  start_time_ms   INTEGER NOT NULL,
  model           TEXT NOT NULL,
  model_key       TEXT,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  thinking_tokens INTEGER,
  cache_read      INTEGER,
  cache_creation  INTEGER,
  web_search_requests INTEGER,
  cost_usd        REAL,
  PRIMARY KEY (session_id, start_time_ms, model)
);
CREATE INDEX IF NOT EXISTS idx_scm_session ON session_cost_models(session_id);

CREATE TABLE IF NOT EXISTS pr_links (
  session_id TEXT NOT NULL,
  pr_number  INTEGER,
  pr_url     TEXT NOT NULL,
  repo       TEXT,
  ts         TEXT,
  account    TEXT,
  PRIMARY KEY (session_id, pr_url)
);
CREATE TABLE IF NOT EXISTS session_titles (
  session_id TEXT NOT NULL,
  title      TEXT,
  source     TEXT NOT NULL,          -- 'custom' (user /rename) | 'ai'
  account    TEXT,
  PRIMARY KEY (session_id, source)
);
CREATE TABLE IF NOT EXISTS turn_durations (
  uuid        TEXT PRIMARY KEY,
  session_id  TEXT,
  ts          TEXT NOT NULL,
  duration_ms INTEGER,
  message_count INTEGER,
  is_sidechain INTEGER DEFAULT 0,
  project     TEXT,
  account     TEXT
);
CREATE INDEX IF NOT EXISTS idx_td_session ON turn_durations(session_id);
CREATE INDEX IF NOT EXISTS idx_td_ts      ON turn_durations(ts);

-- kind: rate_limit | server_error | retry | quota | other_error
CREATE TABLE IF NOT EXISTS api_events (
  uuid       TEXT NOT NULL,
  kind       TEXT NOT NULL,
  ts         TEXT NOT NULL,
  session_id TEXT,
  detail     TEXT,
  project    TEXT,
  account    TEXT,
  PRIMARY KEY (uuid, kind)
);
CREATE INDEX IF NOT EXISTS idx_ae_ts ON api_events(ts);

-- v8: requests that failed or were aborted and reported no usage (nothing billed). Not messages:
-- they never count toward message totals or the "without usage" data-quality flag.
CREATE TABLE IF NOT EXISTS failed_requests (
  msg_id      TEXT PRIMARY KEY,
  ts          TEXT NOT NULL,
  session_id  TEXT,
  project     TEXT,
  model       TEXT,
  tool        TEXT,
  account     TEXT,
  stop_reason TEXT,
  outcome     TEXT NOT NULL,      -- aborted | error
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fr_account_ts ON failed_requests(account, ts);
"""

EXTRAS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tool_calls (
  tool_use_id     TEXT PRIMARY KEY,
  msg_id          TEXT,
  ts              TEXT NOT NULL,
  ts_day          TEXT NOT NULL,
  session_id      TEXT,
  project         TEXT,
  project_path    TEXT,
  tool_name       TEXT NOT NULL,
  mcp_server      TEXT,
  skill           TEXT,
  subagent_type   TEXT,
  target_path     TEXT,
  command_verb    TEXT,
  is_error        INTEGER DEFAULT 0,
  account         TEXT DEFAULT 'personal'
);
CREATE INDEX IF NOT EXISTS idx_tc_account  ON tool_calls(account);
CREATE INDEX IF NOT EXISTS idx_tc_ts       ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_tc_day      ON tool_calls(ts_day);
CREATE INDEX IF NOT EXISTS idx_tc_name     ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tc_skill    ON tool_calls(skill);
CREATE INDEX IF NOT EXISTS idx_tc_session  ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tc_project  ON tool_calls(project);
CREATE INDEX IF NOT EXISTS idx_tc_subagent ON tool_calls(subagent_type);
CREATE INDEX IF NOT EXISTS idx_tc_mcp      ON tool_calls(mcp_server);

CREATE TABLE IF NOT EXISTS slash_prompts (
  prompt_id       TEXT PRIMARY KEY,
  ts              TEXT NOT NULL,
  ts_day          TEXT NOT NULL,
  session_id      TEXT,
  project         TEXT,
  command         TEXT NOT NULL,
  account         TEXT DEFAULT 'personal'
);
CREATE INDEX IF NOT EXISTS idx_sp_account ON slash_prompts(account);
CREATE INDEX IF NOT EXISTS idx_sp_ts      ON slash_prompts(ts);
CREATE INDEX IF NOT EXISTS idx_sp_command ON slash_prompts(command);

CREATE TABLE IF NOT EXISTS extras_files (
  path      TEXT PRIMARY KEY,
  size      INTEGER,
  mtime_ns  INTEGER,
  rows      INTEGER,
  last_seen TEXT
);
"""

# Slash command names look like /foo, /foo:bar, /foo-bar — first token only.
_SLASH_RE = re.compile(r"^/([A-Za-z][\w:.-]{0,63})\b")
# <command-name>foo</command-name> wrapper variant
_CMD_TAG_RE = re.compile(r"<command-name>\s*([^<\s][^<]*?)\s*</command-name>", re.IGNORECASE)
# Bash verbs to ignore as the "real" command (they wrap the actual command)
_BASH_SKIP = {"cd", "set", "export", "&&", "||", ";", "|", "(", "{", "if", "for", "while"}


def _ensure_account_column(conn: sqlite3.Connection, table: str) -> None:
    """Migrate a pre-v3 table: add account (+ source_priority on messages).

    WHY: v1 rows all came from ~/.claude, i.e. the personal account. Rows whose
    transcript has since been deleted are kept; the file scan is forced to rerun so
    surviving copies pick up exact priorities.
    """
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if not cols:
        return
    if "account" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN account TEXT NOT NULL DEFAULT 'personal'")
        conn.execute(f"UPDATE {table} SET account='personal' WHERE account IS NULL OR account='default'")
    if table == "messages" and "source_priority" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN source_priority INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE messages SET source_priority=40 WHERE account='work'")
        conn.execute("DELETE FROM files")


# Dedup on msg_id: a streamed message is logged several times with a growing output_tokens, and
# resumed sessions copy it across files, so last-write-wins can keep a partial copy. Token/cost
# columns keep the more complete copy (larger output_tokens); usage_final records whether any copy
# carried stop_reason; account/priority columns still follow source_priority.
_MORE_COMPLETE = "excluded.output_tokens >= COALESCE(messages.output_tokens, 0)"

# v4: pricing modifiers (fast mode, data residency, web search) + estimated-price flag.
_MODIFIER_COLS = {
    "speed": "TEXT DEFAULT 'standard'", "inference_geo": "TEXT",
    "web_search_requests": "INTEGER DEFAULT 0", "web_fetch_requests": "INTEGER DEFAULT 0",
    "fast_premium_usd": "REAL DEFAULT 0", "server_tool_cost_usd": "REAL DEFAULT 0",
    "price_estimated": "INTEGER DEFAULT 0",
}


# v5: who/where/how each message was made (subagent, skill, MCP server, plugin, branch, client).
_V5_COLS = {
    "is_sidechain": "INTEGER DEFAULT 0", "agent_id": "TEXT", "attribution_agent": "TEXT",
    "attribution_skill": "TEXT", "attribution_mcp_server": "TEXT", "attribution_plugin": "TEXT",
    "git_branch": "TEXT", "entrypoint": "TEXT", "cc_version": "TEXT", "effort": "TEXT",
    "thinking_tokens": "INTEGER DEFAULT 0", "usage_reported": "INTEGER DEFAULT 1",
    # v6: 1 = some copy of the message carries stop_reason, so its usage is final; 0 = every copy is a
    # partial record whose output_tokens is the stream-start placeholder (subagent transcripts write
    # most tool-use turns before the final usage arrives); NULL = indexed before v6, transcript gone.
    "usage_final": "INTEGER",
    # v7: which coding agent produced the row (claude-code | codex-cli | pi)
    "tool": "TEXT NOT NULL DEFAULT 'claude-code'",
}
_V5_META = [c for c in _V5_COLS if c not in ("thinking_tokens", "usage_reported", "usage_final", "tool")]

# v8: how the response ended, and the size of its own content (for output estimates).
_V8_COLS = {
    "stop_reason": "TEXT",           # source's value: end_turn/tool_use (Claude), stop/toolUse/aborted/error (Pi)
    "outcome": "TEXT DEFAULT 'ok'",  # ok | aborted | error
    "content_chars": "INTEGER DEFAULT 0",  # chars of text + thinking + tool-call input JSON, all blocks
}
OUTCOMES = ("ok", "aborted", "error")
# v8: usage ESTIMATES for messages whose usage was never reported (usage_reported=0). Kept apart from
# the measured columns so no total mixes them in silently; est_method says how it was derived.
_EST_COLS = {
    "usage_estimated": "INTEGER DEFAULT 0", "est_input_tokens": "INTEGER DEFAULT 0",
    "est_cache_read": "INTEGER DEFAULT 0", "est_cache_write": "INTEGER DEFAULT 0",
    "est_output_tokens": "INTEGER DEFAULT 0", "est_cost_usd": "REAL DEFAULT 0", "est_method": "TEXT",
}

# Dashboard read paths. The covering index serves every windowed aggregate without touching the
# wide rows; (session_id, ts, cost_usd) serves per-session rollups and reconciliation windows.
_COVER_COLS = ("ts, account, tool, project, project_path, session_id, model, tier, speed, cost_usd, input_tokens, "
               "output_tokens, cache_5m_write, cache_1h_write, cache_read, thinking_tokens, fast_premium_usd, "
               "server_tool_cost_usd, web_search_requests, price_estimated, is_sidechain, usage_estimated, est_cost_usd")
_READ_INDEXES = f"""
CREATE INDEX IF NOT EXISTS idx_messages_cover ON messages({_COVER_COLS});
CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, ts, cost_usd);
CREATE INDEX IF NOT EXISTS idx_messages_account_ts ON messages(account, ts);
DROP INDEX IF EXISTS idx_messages_ts;        -- prefix of idx_messages_cover
DROP INDEX IF EXISTS idx_messages_session;   -- prefix of idx_messages_session_ts
DROP INDEX IF EXISTS idx_messages_account;   -- duplicate of idx_messages_account_ts
"""


def _ensure_modifier_columns(conn: sqlite3.Connection) -> None:
    """Idempotent: ALTER TABLE ADD COLUMN for any v4/v5 column the messages table lacks."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    for name, decl in {**_MODIFIER_COLS, **_V5_COLS, **_V8_COLS, **_EST_COLS}.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")


def discover_tool_sources(home: Path | None = None, env: dict | None = None) -> list[dict]:
    """[{'name', 'root', 'priority', 'tool'}] for Codex CLI and Pi session dirs that exist.

    Codex: $CODEX_HOME, ~/.codex and every ~/.codex-<x> with a sessions/ (archived_sessions/ too);
    account 'codex-cli' for ~/.codex, else 'codex-cli-<x>'. Pi: $PI_CODING_AGENT_DIR or ~/.pi/agent,
    account 'pi'."""
    home = home or Path.home()
    env = os.environ if env is None else env
    out: list[dict] = []
    homes: list[Path] = []
    if env.get("CODEX_HOME"):
        homes.append(Path(env["CODEX_HOME"]).expanduser())
    homes += sorted(d for d in home.glob(CODEX_HOMES_GLOB) if d.is_dir())
    seen: set[str] = set()
    for h in homes:
        key = os.path.realpath(h)
        if key in seen:
            continue
        seen.add(key)
        suffix = h.name[len(".codex"):].lstrip("-_.") if h.name.startswith(".codex") else h.name
        name = TOOL_CODEX if not suffix else f"{TOOL_CODEX}-{suffix}"
        for sub in ("sessions", "archived_sessions"):
            if (h / sub).is_dir():
                out.append({"name": name, "root": h / sub, "priority": 5, "tool": TOOL_CODEX})
    pi = Path(env["PI_CODING_AGENT_DIR"]).expanduser() if env.get("PI_CODING_AGENT_DIR") else home / ".pi" / "agent"
    if (pi / "sessions").is_dir():
        out.append({"name": TOOL_PI, "root": pi / "sessions", "priority": 5, "tool": TOOL_PI})
    return out


def discover_accounts(include_tools: bool = True) -> list[dict]:
    """Claude config dirs (tool 'claude-code') plus, unless include_tools=False, the other agents'
    session dirs (discover_tool_sources). Each entry: {'name', 'root', 'priority', 'tool'}."""
    out = _discover_claude_accounts()
    for a in out:
        a.setdefault("tool", TOOL_CLAUDE)
    return out + (discover_tool_sources() if include_tools else [])


def _discover_claude_accounts() -> list[dict]:
    """Return [{'name', 'root', 'priority'}] of Claude config dirs to index.

    Resolution order: accounts.json next to this file (optional "priority" per entry,
    else list order = precedence) > ACCOUNT_ROOTS that exist > ~/.claude*/projects glob.
    WHY: priority decides which copy of a duplicated message id owns the account tag.
    """
    if ACCOUNTS_FILE.exists():
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            out = []
            for i, a in enumerate(data):
                root = Path(a["root"]).expanduser()
                if root.is_dir():
                    out.append({
                        "name": str(a["name"]), "root": root,
                        "priority": int(a.get("priority", len(data) - i)),
                    })
            if out:
                return out
        except Exception as e:
            print(f"[indexer] ignoring bad accounts.json: {e}", file=sys.stderr)
    out = [{"name": n, "root": r, "priority": p} for r, n, p in ACCOUNT_ROOTS if r.is_dir()]
    if out:
        return out
    dirs = [d for d in sorted(Path.home().glob(".claude*")) if (d / "projects").is_dir()]
    for i, d in enumerate(dirs):
        name = d.name[len(".claude"):].lstrip("-_.") or "default"
        out.append({"name": name, "root": d / "projects", "priority": len(dirs) - i})
    return out or [{"name": "default", "root": CLAUDE_PROJECTS, "priority": 0}]


def open_db(db_path: Path, extras: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_account_column(conn, "messages")
    conn.executescript(SCHEMA_SQL)
    _ensure_modifier_columns(conn)
    # v7: the covering index gained `tool`; rebuild a pre-v7 one in place (same name)
    cover = [r[2] for r in conn.execute("PRAGMA index_info(idx_messages_cover)")]
    if cover and ("tool" not in cover or "est_cost_usd" not in cover):  # v8 added the estimate columns
        conn.execute("DROP INDEX idx_messages_cover")
    conn.executescript(_READ_INDEXES)
    # v5: synthetic placeholder rows are not API calls; they inflated message counts.
    conn.execute("DELETE FROM messages WHERE model = ?", (SYNTHETIC_MODEL,))
    if extras:
        _ensure_account_column(conn, "tool_calls")
        _ensure_account_column(conn, "slash_prompts")
        conn.executescript(EXTRAS_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO meta(k,v) VALUES('schema_version', ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def _project_name_from_cwd(cwd: str | None, fallback: str) -> tuple[str, str]:
    """Return (display_name, full_path). Prefer real cwd; fall back to folder."""
    if cwd:
        full = cwd.replace("\\", "/").rstrip("/")
        base = full.rsplit("/", 1)[-1] or full
        return base, cwd
    return fallback, fallback


def _extract_usage_row(rec: dict, project_fallback: str, account: str = "personal",
                       source_priority: int = 0) -> dict | None:
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {}
    if not usage:
        return None
    msg_id = msg.get("id") or rec.get("uuid")
    if not msg_id:
        return None
    ts = rec.get("timestamp")
    if not ts:
        return None

    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cread = int(usage.get("cache_read_input_tokens", 0) or 0)
    cc = usage.get("cache_creation") or {}
    c5w = int(cc.get("ephemeral_5m_input_tokens", 0) or 0)
    c1w = int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
    if c5w == 0 and c1w == 0:
        c5w = int(usage.get("cache_creation_input_tokens", 0) or 0)

    model = msg.get("model") or "unknown"
    if model == SYNTHETIC_MODEL:
        return None
    speed = str(usage.get("speed") or "standard").lower()
    geo = usage.get("inference_geo") or None
    stu = usage.get("server_tool_use") or {}
    web_search = int(stu.get("web_search_requests", 0) or 0)
    web_fetch = int(stu.get("web_fetch_requests", 0) or 0)
    c = pricing.cost_for(model, inp, out, c5w, c1w, cread, speed=speed, inference_geo=geo,
                         web_search_requests=web_search, ts=ts)

    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    dt_local = dt.astimezone()  # local timezone for day/hour buckets
    project, project_path = _project_name_from_cwd(rec.get("cwd"), project_fallback)
    otd = usage.get("output_tokens_details") or {}

    return {
        "msg_id": msg_id,
        "ts": ts,
        "ts_day": dt_local.strftime("%Y-%m-%d"),
        "ts_hour": dt_local.hour,
        "ts_dow": dt_local.weekday(),  # 0=Mon ... 6=Sun
        "session_id": rec.get("sessionId"),
        "project": project,
        "project_path": project_path,
        "model": model,
        "tier": c.tier,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_5m_write": c5w,
        "cache_1h_write": c1w,
        "cache_read": cread,
        "cost_usd": c.cost,
        "speed": speed,
        "inference_geo": geo,
        "web_search_requests": web_search,
        "web_fetch_requests": web_fetch,
        "fast_premium_usd": c.fast_premium,
        "server_tool_cost_usd": c.server_tool,
        "price_estimated": int(c.estimated),
        "account": account,
        "source_priority": source_priority,
        **_record_meta(rec),
        "thinking_tokens": int(otd.get("thinking_tokens", 0) or 0) if isinstance(otd, dict) else 0,
        # A gateway can stream a message without reporting usage ({"input_tokens":0,"output_tokens":0}).
        "usage_reported": int(bool(inp or out or cread or c5w or c1w)),
        "usage_final": int(bool(msg.get("stop_reason"))),
        "tool": TOOL_CLAUDE,
        "stop_reason": _str_or_none(msg.get("stop_reason")),
        "outcome": "error" if (rec.get("isApiErrorMessage") or msg.get("error")) else "ok",
        "content_chars": _content_chars(msg.get("content")),
        "_uuid": rec.get("uuid"),
    }


def _tool_row(*, msg_id: str, ts: str, session_id, cwd, fallback: str, model: str, inp: int, out: int,
              c5w: int, cr: int, thinking: int, account: str, source_priority: int, tool: str,
              meta: dict | None = None) -> dict | None:
    """A messages row for a non-Claude-Code agent, priced through pricing.cost_for (LiteLLM path)."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    c = pricing.cost_for(model, inp, out, c5w, 0, cr, ts=ts)
    dt_local = dt.astimezone()
    project, project_path = _project_name_from_cwd(cwd, fallback)
    row = {
        "msg_id": msg_id, "ts": ts, "ts_day": dt_local.strftime("%Y-%m-%d"), "ts_hour": dt_local.hour,
        "ts_dow": dt_local.weekday(), "session_id": session_id, "project": project, "project_path": project_path,
        "model": model, "tier": c.tier, "input_tokens": inp, "output_tokens": out, "cache_5m_write": c5w,
        "cache_1h_write": 0, "cache_read": cr, "cost_usd": c.cost, "speed": "standard", "inference_geo": None,
        "web_search_requests": 0, "web_fetch_requests": 0, "fast_premium_usd": c.fast_premium,
        "server_tool_cost_usd": c.server_tool, "price_estimated": int(c.estimated), "account": account,
        "source_priority": source_priority,
        **{k: None for k in _V5_META}, "is_sidechain": 0,
        "thinking_tokens": thinking, "usage_reported": int(bool(inp or out or cr or c5w)), "usage_final": 1,
        "tool": tool, "stop_reason": None, "outcome": "ok", "content_chars": 0,
    }
    row.update(meta or {})
    return row


_FORK_REPLAY_S = 1.0  # a fork's replayed parent history is stamped within ms of its session_meta


def _codex_rows(jf: Path, fallback: str, account: str, source_priority: int) -> list[dict]:
    """One row per Codex token_count whose cumulative total moved (delta of total_token_usage).

    Forked rollouts replay the parent's history (token_counts included) right after their
    session_meta, stamped at the fork instant: those set the baseline and are not emitted."""
    rows: list[dict] = []
    sid = cwd = model = effort = branch = origin = version = None
    sidechain = 0
    fork_t0 = None
    prev = {"i": 0, "c": 0, "w": 0, "o": 0, "r": 0, "t": 0}
    with open(jf, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            t, pl = rec.get("type"), rec.get("payload")
            if not isinstance(pl, dict):
                continue
            if t == "session_meta":
                if sid is None:  # the first meta is this rollout's own; later ones are replayed parents
                    sid, cwd = pl.get("id") or pl.get("session_id"), pl.get("cwd")
                    origin, version = _str_or_none(pl.get("originator")), _str_or_none(pl.get("cli_version"))
                    branch = _str_or_none((pl.get("git") or {}).get("branch")) if isinstance(pl.get("git"), dict) else None
                    src = pl.get("source")
                    sidechain = int(isinstance(src, dict) and "subagent" in src)
                    if pl.get("forked_from_id") and rec.get("timestamp"):
                        try:
                            fork_t0 = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00"))
                        except ValueError:
                            fork_t0 = None
                continue
            if t == "turn_context":
                model = pl.get("model") or model
                effort = _str_or_none(pl.get("effort")) or effort
                cwd = pl.get("cwd") or cwd
                continue
            if t != "event_msg" or pl.get("type") != "token_count":
                continue
            info = pl.get("info")
            tu = info.get("total_token_usage") if isinstance(info, dict) else None
            ts = rec.get("timestamp")
            if not isinstance(tu, dict) or not ts:
                continue
            cur = {"i": int(tu.get("input_tokens") or 0), "c": int(tu.get("cached_input_tokens") or 0),
                   "w": int(tu.get("cache_write_input_tokens") or 0), "o": int(tu.get("output_tokens") or 0),
                   "r": int(tu.get("reasoning_output_tokens") or 0), "t": int(tu.get("total_tokens") or 0)}
            replay = False
            if fork_t0 is not None:
                try:
                    replay = (datetime.fromisoformat(ts.replace("Z", "+00:00")) - fork_t0).total_seconds() <= _FORK_REPLAY_S
                except ValueError:
                    replay = False
            d = {k: cur[k] - prev[k] for k in cur}
            if cur["t"] < prev["t"]:  # counter reset (not seen in practice): count the new total in full
                d = dict(cur)
            prev = cur
            if replay or d["t"] <= 0 or not sid:
                continue
            cached = max(0, d["c"])
            write = max(0, d["w"])
            inp = max(0, d["i"] - cached - write)
            row = _tool_row(msg_id=f"codex:{sid}:{cur['t']}", ts=ts, session_id=sid, cwd=cwd, fallback=fallback,
                            model=model or "unknown", inp=inp, out=max(0, d["o"]), c5w=write, cr=cached,
                            thinking=max(0, d["r"]), account=account, source_priority=source_priority,
                            tool=TOOL_CODEX,
                            meta={"is_sidechain": sidechain, "git_branch": branch, "entrypoint": origin,
                                  "cc_version": version, "effort": effort})
            if row:
                rows.append(row)
    return rows


def _pi_rows(jf: Path, fallback: str, account: str, source_priority: int) -> list[dict]:
    """One row per Pi assistant message with usage (input is already uncached)."""
    rows: list[dict] = []
    sid = cwd = version = None
    with open(jf, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") == "session":
                sid, cwd, version = rec.get("id"), rec.get("cwd"), _str_or_none(rec.get("version"))
                continue
            msg = rec.get("message")
            if rec.get("type") != "message" or not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            u = msg.get("usage")
            ts = rec.get("timestamp")
            if not isinstance(u, dict) or not ts:
                continue
            mid = msg.get("responseId") or rec.get("id")
            if not mid:
                continue
            row = _tool_row(msg_id=f"pi:{mid}", ts=ts, session_id=sid, cwd=cwd, fallback=fallback,
                            model=msg.get("model") or "unknown", inp=int(u.get("input") or 0),
                            out=int(u.get("output") or 0), c5w=int(u.get("cacheWrite") or 0),
                            cr=int(u.get("cacheRead") or 0), thinking=0, account=account,
                            source_priority=source_priority, tool=TOOL_PI,
                            meta={"entrypoint": _str_or_none(msg.get("provider")),
                                  "cc_version": f"pi-{version}" if version else "pi"})
            if row:
                stop = _str_or_none(msg.get("stopReason"))
                row["usage_final"] = int(bool(stop))
                row["stop_reason"] = stop
                row["outcome"] = stop if stop in ("aborted", "error") else "ok"
                row["content_chars"] = _content_chars(msg.get("content"))
                if row["outcome"] != "ok":
                    row["_error"] = _str_or_none(msg.get("errorMessage"))
                rows.append(row)
    return rows


def _content_chars(content) -> int:
    """Characters the model produced in one message: text, thinking, tool-call input as JSON.

    Encrypted/redacted reasoning and signatures carry no countable text and are skipped."""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    n = 0
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            n += len(b.get("text") or "")
        elif t == "thinking":
            n += len(b.get("thinking") or "")
        elif t in ("tool_use", "toolCall", "server_tool_use"):
            args = b.get("input") if "input" in b else b.get("arguments")
            if isinstance(args, str):
                n += len(args)
            elif args is not None:
                n += len(json.dumps(args, ensure_ascii=False))
    return n


def _str_or_none(v) -> str | None:
    return str(v) if v not in (None, "") else None


def _record_meta(rec: dict) -> dict:
    """v5 attribution/context fields shared by every transcript record type."""
    return {
        "is_sidechain": int(bool(rec.get("isSidechain"))),
        "agent_id": _str_or_none(rec.get("agentId")),
        "attribution_agent": _str_or_none(rec.get("attributionAgent")),
        "attribution_skill": _str_or_none(rec.get("attributionSkill")),
        "attribution_mcp_server": _str_or_none(rec.get("attributionMcpServer")),
        "attribution_plugin": _str_or_none(rec.get("attributionPlugin")),
        "git_branch": _str_or_none(rec.get("gitBranch")),
        "entrypoint": _str_or_none(rec.get("entrypoint")),
        "cc_version": _str_or_none(rec.get("version")),
        "effort": _str_or_none(rec.get("effort") or rec.get("perTurnEffort")),
    }


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except Exception:
        return None


def _extract_side_rows(rec: dict, project_fallback: str, out: dict[str, list]) -> None:
    """Append rows for the v5 side tables (cost-state, PR links, titles, turn durations, API events)."""
    t = rec.get("type")
    sid = rec.get("sessionId")
    if t == "cost-state":
        if not sid or rec.get("startTime") is None:
            return
        key = (sid, int(rec["startTime"]))
        out["cost_state"][key] = rec  # cumulative within a process: the last one in the file wins
        return
    if t == "pr-link":
        if sid and rec.get("prUrl"):
            out["pr_links"].append({"session_id": sid, "pr_number": rec.get("prNumber"), "pr_url": rec["prUrl"],
                                    "repo": rec.get("prRepository"), "ts": rec.get("timestamp")})
        return
    if t in ("ai-title", "custom-title"):
        title = rec.get("aiTitle") if t == "ai-title" else rec.get("customTitle")
        if sid and title:
            out["titles"][(sid, "ai" if t == "ai-title" else "custom")] = str(title)
        return
    ts = rec.get("timestamp")
    uuid = rec.get("uuid")
    if not ts or not uuid:
        return
    project, _ = _project_name_from_cwd(rec.get("cwd"), project_fallback)
    if t == "system" and rec.get("subtype") == "turn_duration":
        out["turns"].append({"uuid": uuid, "session_id": sid, "ts": ts, "duration_ms": rec.get("durationMs"),
                             "message_count": rec.get("messageCount"),
                             "is_sidechain": int(bool(rec.get("isSidechain"))), "project": project})
        return
    ev = []
    if t == "system" and rec.get("subtype") == "api_error":
        ev.append(("retry", f"attempt {rec.get('retryAttempt')}/{rec.get('maxRetries')}"))
    if rec.get("isApiErrorMessage"):
        err = str(rec.get("error") or "unknown")
        kind = err if err in ("rate_limit", "server_error") else "other_error"
        status = rec.get("apiErrorStatus")
        ev.append((kind, err if status in (None, "") else f"{err} {status}"))
    ql = rec.get("quotaLimits")
    if isinstance(ql, dict) and ql:
        ev.append(("quota", f"{ql.get('rateLimitType') or '?'}:{ql.get('status') or '?'}"))
    for kind, detail in ev:
        out["events"].append({"uuid": uuid, "kind": kind, "ts": ts, "session_id": sid,
                              "detail": detail, "project": project})


def _write_side_rows(cur: sqlite3.Cursor, side: dict[str, list], account: str) -> int:
    n = 0
    for (sid, start), rec in side["cost_state"].items():
        cur.execute(
            "INSERT INTO session_costs (session_id, start_time_ms, start_ts, total_cost_usd, total_api_duration_ms,"
            " total_duration_ms, lines_added, lines_removed, has_unknown_model_cost, account)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, start_time_ms) DO UPDATE SET total_cost_usd=excluded.total_cost_usd,"
            " total_api_duration_ms=excluded.total_api_duration_ms, total_duration_ms=excluded.total_duration_ms,"
            " lines_added=excluded.lines_added, lines_removed=excluded.lines_removed,"
            " has_unknown_model_cost=excluded.has_unknown_model_cost, account=excluded.account"
            # a copy of an older checkpoint of the same process must not overwrite a later one
            " WHERE (excluded.total_cost_usd, excluded.total_duration_ms)"
            "    >= (session_costs.total_cost_usd, session_costs.total_duration_ms)",
            (sid, start, _ms_to_iso(start), float(rec.get("totalCostUSD") or 0), rec.get("totalAPIDuration"),
             rec.get("totalDuration"), rec.get("totalLinesAdded"), rec.get("totalLinesRemoved"),
             int(bool(rec.get("hasUnknownModelCost"))), account),
        )
        if cur.rowcount <= 0:
            continue
        n += 1
        cur.execute("DELETE FROM session_cost_models WHERE session_id=? AND start_time_ms=?", (sid, start))
        for model, mu in (rec.get("modelUsage") or {}).items():
            if not isinstance(mu, dict):
                continue
            cur.execute(
                "INSERT INTO session_cost_models VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, start, model, pricing.model_key(model), mu.get("inputTokens"), mu.get("outputTokens"),
                 mu.get("thinkingTokens"), mu.get("cacheReadInputTokens"), mu.get("cacheCreationInputTokens"),
                 mu.get("webSearchRequests") or 0, float(mu.get("costUSD") or 0)),
            )
    if side["pr_links"]:
        cur.executemany(
            "INSERT INTO pr_links (session_id, pr_number, pr_url, repo, ts, account)"
            " VALUES (:session_id, :pr_number, :pr_url, :repo, :ts, :account) ON CONFLICT DO NOTHING",
            [{**r, "account": account} for r in side["pr_links"]],
        )
    for (sid, src), title in side["titles"].items():
        cur.execute(
            "INSERT INTO session_titles (session_id, title, source, account) VALUES (?,?,?,?)"
            " ON CONFLICT(session_id, source) DO UPDATE SET title=excluded.title",
            (sid, title, src, account),
        )
    if side["turns"]:
        cur.executemany(
            "INSERT INTO turn_durations (uuid, session_id, ts, duration_ms, message_count, is_sidechain, project, account)"
            " VALUES (:uuid, :session_id, :ts, :duration_ms, :message_count, :is_sidechain, :project, :account)"
            " ON CONFLICT DO NOTHING",
            [{**r, "account": account} for r in side["turns"]],
        )
    if side["events"]:
        cur.executemany(
            "INSERT INTO api_events (uuid, kind, ts, session_id, detail, project, account)"
            " VALUES (:uuid, :kind, :ts, :session_id, :detail, :project, :account) ON CONFLICT DO NOTHING",
            [{**r, "account": account} for r in side["events"]],
        )
    return n


def _project_fallback(jsonl_path: Path, root: Path) -> str:
    rel = jsonl_path.relative_to(root)
    parts = rel.parts
    return parts[0] if parts else jsonl_path.stem


def _ts_parts(ts: str) -> tuple[str, str] | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    dt_local = dt.astimezone()
    return dt_local.strftime("%Y-%m-%d"), dt


_BASH_CHUNK_SEP = re.compile(r"\s*(?:&&|\|\||;)\s*")
_BASH_WRAP = {"sudo", "time", "exec", "nice", "ionice", "env"}


def _bash_verb(command: str) -> str | None:
    """Return the first 'real' command verb in a bash string. None if unparseable.

    Splits on `&&`/`||`/`;` and walks chunks left-to-right. Skips `cd` chunks
    and env-assignment prefixes. Strips path separators from the executable.
    """
    if not command:
        return None
    for chunk in _BASH_CHUNK_SEP.split(command):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            tokens = shlex.split(chunk, posix=True)
        except Exception:
            tokens = chunk.split()
        i = 0
        skip_chunk = False
        while i < len(tokens):
            t = tokens[i]
            if "=" in t and re.match(r"^[A-Za-z_]\w*=", t):
                i += 1
                continue
            if t in _BASH_WRAP:
                i += 1
                continue
            if t == "cd":
                skip_chunk = True
                break
            verb = t.split("/")[-1].split("\\")[-1].strip('"').strip("'")
            if not verb or verb.startswith("-"):
                return None
            return verb
        if skip_chunk:
            continue
    return None


def _extract_extras_from_assistant(rec: dict, fallback: str) -> list[dict]:
    """Yield tool_calls rows for tool_use blocks in an assistant record."""
    if rec.get("type") != "assistant":
        return []
    msg = rec.get("message") or {}
    content = msg.get("content") or []
    if not isinstance(content, list):
        return []
    ts = rec.get("timestamp")
    if not ts:
        return []
    parts = _ts_parts(ts)
    if not parts:
        return []
    ts_day, _dt = parts
    msg_id = msg.get("id") or rec.get("uuid")
    project, project_path = _project_name_from_cwd(rec.get("cwd"), fallback)
    session_id = rec.get("sessionId")

    out = []
    for blk in content:
        if not isinstance(blk, dict) or blk.get("type") != "tool_use":
            continue
        tool_use_id = blk.get("id")
        if not tool_use_id:
            continue
        name = blk.get("name") or "?"
        inp = blk.get("input") or {}
        mcp_server = None
        if name.startswith("mcp__"):
            # mcp__<server>__<tool>
            after = name[5:]
            mcp_server = after.split("__", 1)[0] if "__" in after else after
        skill = inp.get("skill") if name == "Skill" else None
        # Sub-agent dispatch tool is named `Agent` in current Claude Code; older
        # logs may use `Task`. Treat both as the same signal.
        subagent = inp.get("subagent_type") if name in ("Agent", "Task") else None
        target_path = None
        if name in {"Read", "Edit", "Write", "NotebookEdit"}:
            target_path = inp.get("file_path") or inp.get("notebook_path")
        cmd_verb = None
        if name == "Bash":
            cmd_verb = _bash_verb(inp.get("command") or "")
        out.append({
            "tool_use_id": tool_use_id,
            "msg_id": msg_id,
            "ts": ts,
            "ts_day": ts_day,
            "session_id": session_id,
            "project": project,
            "project_path": project_path,
            "tool_name": name,
            "mcp_server": mcp_server,
            "skill": skill,
            "subagent_type": subagent,
            "target_path": target_path,
            "command_verb": cmd_verb,
            "is_error": 0,
        })
    return out


def _extract_extras_from_user(rec: dict, fallback: str) -> tuple[list[dict], dict[str, bool]]:
    """Return (slash_rows, error_marks_by_tool_use_id)."""
    if rec.get("type") != "user":
        return [], {}
    msg = rec.get("message") or {}
    content = msg.get("content")
    ts = rec.get("timestamp")
    parts = _ts_parts(ts) if ts else None
    project, project_path = _project_name_from_cwd(rec.get("cwd"), fallback)
    session_id = rec.get("sessionId")
    slash_rows: list[dict] = []
    err_marks: dict[str, bool] = {}

    if isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_result" and blk.get("is_error"):
                tid = blk.get("tool_use_id")
                if tid:
                    err_marks[tid] = True
        return slash_rows, err_marks

    if isinstance(content, str) and parts:
        ts_day, _ = parts
        text = content.lstrip()
        cmd = None
        m = _SLASH_RE.match(text)
        if m:
            cmd = m.group(1)
        else:
            m2 = _CMD_TAG_RE.search(text)
            if m2:
                inner = m2.group(1).strip().split()[0] if m2.group(1).strip() else ""
                cmd = inner.lstrip("/") if inner else None
        if cmd:
            cmd = cmd.lstrip("/")
            pid = rec.get("promptId") or rec.get("uuid") or f"{session_id}:{ts}"
            slash_rows.append({
                "prompt_id": pid,
                "ts": ts,
                "ts_day": ts_day,
                "session_id": session_id,
                "project": project,
                "command": cmd,
            })
    return slash_rows, err_marks


def _scan_claude_file(jf: Path, fallback: str, account: str, source_priority: int, do_messages: bool,
                      do_extras: bool, msg_rows: list, side: dict, tool_rows: list, slash_rows: list,
                      err_marks: dict) -> None:
    with open(jf, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            if do_messages:
                row = _extract_usage_row(rec, fallback, account, source_priority)
                if row:
                    msg_rows.append(row)
                _extract_side_rows(rec, fallback, side)
            if do_extras:
                tool_rows.extend(_extract_extras_from_assistant(rec, fallback))
                srows, errs = _extract_extras_from_user(rec, fallback)
                slash_rows.extend(srows)
                if errs:
                    err_marks.update(errs)
    # Claude Code writes one record per content block, all with the same message id: the message's
    # content size is the sum over its distinct records (a uuid seen twice counts once).
    per_rec: dict[str, dict] = {}
    for r in msg_rows:
        per_rec.setdefault(r["msg_id"], {})[r.pop("_uuid", None) or id(r)] = r["content_chars"]
    for r in msg_rows:
        r["content_chars"] = sum(per_rec[r["msg_id"]].values())


def _process_file(
    conn: sqlite3.Connection,
    jf: Path,
    root: Path,
    account: str = "personal",
    source_priority: int = 0,
    do_messages: bool = True,
    do_extras: bool = False,
    tool: str = TOOL_CLAUDE,
) -> tuple[int, int]:
    """Returns (messages_inserted, extras_inserted)."""
    if tool != TOOL_CLAUDE:
        do_extras = False  # tool_calls / slash_prompts are Claude Code concepts
    if not (do_messages or do_extras):
        return 0, 0
    fallback = _project_fallback(jf, root)
    msg_rows: list[dict] = []
    tool_rows: list[dict] = []
    slash_rows: list[dict] = []
    err_marks: dict[str, bool] = {}
    side: dict = {"cost_state": {}, "pr_links": [], "titles": {}, "turns": [], "events": []}

    if tool == TOOL_CODEX:
        msg_rows = _codex_rows(jf, fallback, account, source_priority) if do_messages else []
    elif tool == TOOL_PI:
        msg_rows = _pi_rows(jf, fallback, account, source_priority) if do_messages else []
    else:
        _scan_claude_file(jf, fallback, account, source_priority, do_messages, do_extras,
                          msg_rows, side, tool_rows, slash_rows, err_marks)

    cur = conn.cursor()
    msgs_inserted = 0
    extras_inserted = 0

    for r in msg_rows:
        r["account"] = account
    # v8: a failed/aborted request that reported no usage billed nothing: failed_requests, not messages
    failed = [r for r in msg_rows if r.get("outcome", "ok") != "ok" and not r.get("usage_reported")]
    if failed:
        ids = {r["msg_id"] for r in failed}
        msg_rows = [r for r in msg_rows if r["msg_id"] not in ids]
    for r in tool_rows:
        r["account"] = account
    for r in slash_rows:
        r["account"] = account

    if do_messages and msg_rows:
        cur.executemany(
            """
            INSERT INTO messages
              (msg_id, ts, ts_day, ts_hour, ts_dow, session_id, project, project_path,
               model, tier, input_tokens, output_tokens, cache_5m_write, cache_1h_write,
               cache_read, cost_usd, speed, inference_geo, web_search_requests, web_fetch_requests,
               fast_premium_usd, server_tool_cost_usd, price_estimated, account, source_priority,
               {v5})
            VALUES
              (:msg_id, :ts, :ts_day, :ts_hour, :ts_dow, :session_id, :project, :project_path,
               :model, :tier, :input_tokens, :output_tokens, :cache_5m_write, :cache_1h_write,
               :cache_read, :cost_usd, :speed, :inference_geo, :web_search_requests, :web_fetch_requests,
               :fast_premium_usd, :server_tool_cost_usd, :price_estimated, :account, :source_priority,
               {v5_params})
            ON CONFLICT(msg_id) DO UPDATE SET
              ts=CASE WHEN excluded.source_priority >= messages.source_priority THEN excluded.ts ELSE messages.ts END,
              ts_day=CASE WHEN excluded.source_priority >= messages.source_priority THEN excluded.ts_day ELSE messages.ts_day END,
              ts_hour=CASE WHEN excluded.source_priority >= messages.source_priority THEN excluded.ts_hour ELSE messages.ts_hour END,
              ts_dow=CASE WHEN excluded.source_priority >= messages.source_priority THEN excluded.ts_dow ELSE messages.ts_dow END,
              session_id=CASE WHEN excluded.source_priority >= messages.source_priority THEN excluded.session_id ELSE messages.session_id END,
              project=CASE WHEN excluded.source_priority >= messages.source_priority THEN excluded.project ELSE messages.project END,
              project_path=CASE WHEN excluded.source_priority >= messages.source_priority THEN excluded.project_path ELSE messages.project_path END,
              account=CASE WHEN excluded.source_priority >= messages.source_priority THEN excluded.account ELSE messages.account END,
              source_priority=MAX(excluded.source_priority, messages.source_priority),
              model=CASE WHEN {more} THEN excluded.model ELSE messages.model END,
              tier=CASE WHEN {more} THEN excluded.tier ELSE messages.tier END,
              input_tokens=CASE WHEN {more} THEN excluded.input_tokens ELSE messages.input_tokens END,
              output_tokens=CASE WHEN {more} THEN excluded.output_tokens ELSE messages.output_tokens END,
              cache_5m_write=CASE WHEN {more} THEN excluded.cache_5m_write ELSE messages.cache_5m_write END,
              cache_1h_write=CASE WHEN {more} THEN excluded.cache_1h_write ELSE messages.cache_1h_write END,
              cache_read=CASE WHEN {more} THEN excluded.cache_read ELSE messages.cache_read END,
              cost_usd=CASE WHEN {more} THEN excluded.cost_usd ELSE messages.cost_usd END,
              speed=CASE WHEN {more} THEN excluded.speed ELSE messages.speed END,
              inference_geo=CASE WHEN {more} THEN excluded.inference_geo ELSE messages.inference_geo END,
              web_search_requests=CASE WHEN {more} THEN excluded.web_search_requests ELSE messages.web_search_requests END,
              web_fetch_requests=CASE WHEN {more} THEN excluded.web_fetch_requests ELSE messages.web_fetch_requests END,
              fast_premium_usd=CASE WHEN {more} THEN excluded.fast_premium_usd ELSE messages.fast_premium_usd END,
              server_tool_cost_usd=CASE WHEN {more} THEN excluded.server_tool_cost_usd ELSE messages.server_tool_cost_usd END,
              price_estimated=CASE WHEN {more} THEN excluded.price_estimated ELSE messages.price_estimated END,
              thinking_tokens=CASE WHEN {more} THEN excluded.thinking_tokens ELSE messages.thinking_tokens END,
              usage_reported=CASE WHEN {more} THEN excluded.usage_reported ELSE messages.usage_reported END,
              usage_final=MAX(excluded.usage_final, COALESCE(messages.usage_final, 0)),
              stop_reason=COALESCE(excluded.stop_reason, messages.stop_reason),
              outcome=COALESCE(excluded.outcome, messages.outcome),
              content_chars=MAX(COALESCE(excluded.content_chars, 0), COALESCE(messages.content_chars, 0)),
              {v5_set}
            """.replace("{more}", _MORE_COMPLETE)
               .replace("{v5_params}", ", ".join(":" + c for c in (*_V5_COLS, *_V8_COLS)))
               .replace("{v5_set}", ",\n              ".join(f"{c}=COALESCE(excluded.{c}, messages.{c})" for c in _V5_META))
               .replace("{v5}", ", ".join((*_V5_COLS, *_V8_COLS))),
            msg_rows,
        )
        msgs_inserted = cur.rowcount or 0
    if do_messages and failed:
        cur.executemany(
            "INSERT INTO failed_requests (msg_id, ts, session_id, project, model, tool, account, stop_reason,"
            " outcome, error) VALUES (:msg_id, :ts, :session_id, :project, :model, :tool, :account, :stop_reason,"
            " :outcome, :error) ON CONFLICT(msg_id) DO UPDATE SET outcome=excluded.outcome,"
            " stop_reason=excluded.stop_reason, error=excluded.error",
            [{**r, "error": (r.get("_error") or "")[:200] or None} for r in failed])
        # indexed before v8 as a usage-less message
        cur.executemany("DELETE FROM messages WHERE msg_id = ?", [(r["msg_id"],) for r in failed])
    if do_messages:
        _write_side_rows(cur, side, account)

    if do_extras:
        if tool_rows:
            for tr in tool_rows:
                if tr["tool_use_id"] in err_marks:
                    tr["is_error"] = 1
            cur.executemany(
                """
                INSERT INTO tool_calls
                  (tool_use_id, msg_id, ts, ts_day, session_id, project, project_path,
                   tool_name, mcp_server, skill, subagent_type, target_path,
                   command_verb, is_error, account)
                VALUES
                  (:tool_use_id, :msg_id, :ts, :ts_day, :session_id, :project, :project_path,
                   :tool_name, :mcp_server, :skill, :subagent_type, :target_path,
                   :command_verb, :is_error, :account)
                ON CONFLICT(tool_use_id) DO UPDATE SET
                  is_error      = excluded.is_error,
                  skill         = excluded.skill,
                  subagent_type = excluded.subagent_type,
                  mcp_server    = excluded.mcp_server,
                  target_path   = excluded.target_path,
                  command_verb  = excluded.command_verb
                """,
                tool_rows,
            )
            extras_inserted += cur.rowcount or 0
        if slash_rows:
            cur.executemany(
                """
                INSERT INTO slash_prompts
                  (prompt_id, ts, ts_day, session_id, project, command, account)
                VALUES
                  (:prompt_id, :ts, :ts_day, :session_id, :project, :command, :account)
                ON CONFLICT(prompt_id) DO NOTHING
                """,
                slash_rows,
            )
            extras_inserted += cur.rowcount or 0
    return msgs_inserted, extras_inserted


# ---------- v8: usage estimates for messages whose usage was never reported ----------
#
# Some gateways (CLIProxyAPI in front of the Codex API) stream a response without its usage, so the
# message is logged with zero tokens. Such messages get an ESTIMATE, flagged usage_estimated=1 and
# stored in est_* columns; measured totals never include it.
#   prompt (input + cache read + cache write): interpolated in time between the nearest messages with
#     real usage in the same session thread (session, sidechain, agent); the neighbours' cache-read and
#     cache-write shares split it. No neighbour in the thread -> the session; none -> the account's
#     median for the model.
#   output: the message's own content (text + thinking + tool-call JSON) at chars/4, scaled by a ratio
#     calibrated per (account, model) on messages with real usage: median(output_tokens / (chars/4)).
# validate: 10% of the messages WITH usage (crc32(msg_id) % 10 == 0) are hidden and estimated the same
# way; the cost error over them is stored with the estimates (meta k='usage_estimate').

EST_CHARS_PER_TOKEN = 4.0
_EST_MIN_CALIB = 20  # fewer samples for a model -> the account's pooled ratio (then 1.0)


def _epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _quartiles(xs: list[float]) -> tuple[float, float, float] | None:
    if not xs:
        return None
    v = sorted(xs)

    def q(p: float) -> float:
        i = p * (len(v) - 1)
        lo = int(i)
        hi = min(lo + 1, len(v) - 1)
        return v[lo] + (v[hi] - v[lo]) * (i - lo)
    return q(0.25), q(0.5), q(0.75)


def _prompt(r: dict) -> int:
    return (r["input_tokens"] or 0) + (r["cache_read"] or 0) + (r["cache_5m_write"] or 0) + (r["cache_1h_write"] or 0)


def _calibrate(rows: list[dict], hidden: set) -> dict:
    """{(account, model) | (account, None): {"ratio", "p25", "p75", "n"}} of output_tokens / (chars/4)."""
    samples: dict = {}
    for r in rows:
        if (r["usage_reported"] and r["usage_final"] != 0 and (r["output_tokens"] or 0) > 0
                and (r["content_chars"] or 0) > 0 and r["msg_id"] not in hidden):
            ratio = r["output_tokens"] / (r["content_chars"] / EST_CHARS_PER_TOKEN)
            samples.setdefault((r["account"], r["model"]), []).append(ratio)
            samples.setdefault((r["account"], None), []).append(ratio)
    out = {}
    for k, xs in samples.items():
        p25, med, p75 = _quartiles(xs)
        out[k] = {"ratio": med, "p25": p25, "p75": p75, "n": len(xs)}
    return out


def _ratio_for(calib: dict, account: str, model: str) -> float:
    c = calib.get((account, model))
    if c and c["n"] >= _EST_MIN_CALIB:
        return c["ratio"]
    c = calib.get((account, None))
    return c["ratio"] if c and c["n"] >= _EST_MIN_CALIB else 1.0


def _estimate(rows: list[dict], unknown: set, calib: dict) -> dict:
    """msg_id -> (input, cache_read, cache_write, output, method) for every row in `unknown`."""
    known = [r for r in rows if r["usage_reported"] and r["msg_id"] not in unknown and _prompt(r) > 0]
    by_thread: dict = {}
    by_session: dict = {}
    by_model: dict = {}
    for r in known:
        by_thread.setdefault((r["account"], r["session_id"], r["is_sidechain"], r["agent_id"]), []).append(r)
        by_session.setdefault((r["account"], r["session_id"]), []).append(r)
        by_model.setdefault((r["account"], r["model"]), []).append(r)
    idx = {}
    for d in (by_thread, by_session):
        for k, lst in d.items():
            lst.sort(key=lambda x: x["_t"])
            idx[id(lst)] = [x["_t"] for x in lst]
    med: dict = {}
    for k, lst in by_model.items():
        ps = [_prompt(x) for x in lst]
        med[k] = (_quartiles(ps)[1], sum((x["cache_read"] or 0) for x in lst) / max(1, sum(ps)),
                  sum((x["cache_5m_write"] or 0) + (x["cache_1h_write"] or 0) for x in lst) / max(1, sum(ps)))

    def shares(x: dict) -> tuple[int, float, float]:
        p = _prompt(x)
        return p, (x["cache_read"] or 0) / p, ((x["cache_5m_write"] or 0) + (x["cache_1h_write"] or 0)) / p

    out = {}
    for r in rows:
        if r["msg_id"] not in unknown:
            continue
        lst, method = by_thread.get((r["account"], r["session_id"], r["is_sidechain"], r["agent_id"])), "thread"
        if not lst:
            lst, method = by_session.get((r["account"], r["session_id"])), "session"
        if lst:
            ts = idx[id(lst)]
            i = bisect.bisect_left(ts, r["_t"])
            prev = lst[i - 1] if i > 0 else None
            nxt = lst[i] if i < len(lst) else None
            if prev and nxt:
                span = nxt["_t"] - prev["_t"]
                w = 0.5 if span <= 0 else (r["_t"] - prev["_t"]) / span
                (pa, ra, wa), (pb, rb, wb) = shares(prev), shares(nxt)
                prompt, rs, ws = pa + (pb - pa) * w, ra + (rb - ra) * w, wa + (wb - wa) * w
            else:
                prompt, rs, ws = shares(prev or nxt)
        elif (r["account"], r["model"]) in med:
            (prompt, rs, ws), method = med[(r["account"], r["model"])], "model_median"
        else:
            prompt, rs, ws, method = 0, 0.0, 0.0, "none"
        cr, cw = round(prompt * rs), round(prompt * ws)
        inp = max(0, round(prompt) - cr - cw)
        outp = round(_ratio_for(calib, r["account"], r["model"]) * (r["content_chars"] or 0) / EST_CHARS_PER_TOKEN)
        out[r["msg_id"]] = (inp, cr, cw, outp, method)
    return out


def _est_cost(r: dict, e: tuple) -> float:
    return pricing.cost_for(r["model"] or "", e[0], e[3], e[2], 0, e[1], ts=r["ts"]).cost


def _validate(rows: list[dict], targets: set) -> dict:
    """Hide 10% of the messages with real, final usage, estimate them like the usage-less ones and
    compare cost (both sides priced with cost_for on tokens only, so modifiers do not blur it)."""
    hidden = {r["msg_id"] for r in rows
              if r["usage_reported"] and r["usage_final"] != 0 and r["msg_id"] not in targets
              and _prompt(r) > 0 and zlib.crc32(r["msg_id"].encode()) % 10 == 0}
    if not hidden:
        return {"n": 0}
    calib = _calibrate(rows, hidden)
    est = _estimate(rows, hidden | targets, calib)
    errs, perr, oerr = [], [], []
    tot_a = tot_e = 0.0
    for r in rows:
        if r["msg_id"] not in hidden:
            continue
        e = est[r["msg_id"]]
        a = pricing.cost_for(r["model"] or "", r["input_tokens"] or 0, r["output_tokens"] or 0,
                             (r["cache_5m_write"] or 0) + (r["cache_1h_write"] or 0), 0, r["cache_read"] or 0,
                             ts=r["ts"]).cost
        c = _est_cost(r, e)
        tot_a += a
        tot_e += c
        if a > 0:
            errs.append(abs(c - a) / a * 100)
        perr.append(abs(sum(e[:3]) - _prompt(r)) / _prompt(r) * 100)
        if r["output_tokens"]:
            oerr.append(abs(e[3] - r["output_tokens"]) / r["output_tokens"] * 100)
    q = _quartiles(errs) or (None, None, None)
    return {"n": len(hidden), "cost_actual_usd": round(tot_a, 4), "cost_estimated_usd": round(tot_e, 4),
            "median_abs_pct_err": round(q[1], 1) if q[1] is not None else None,
            "abs_pct_err_p25": round(q[0], 1) if q[0] is not None else None,
            "abs_pct_err_p75": round(q[2], 1) if q[2] is not None else None,
            "total_bias_pct": round((tot_e - tot_a) / tot_a * 100, 1) if tot_a else None,
            "prompt_median_abs_pct_err": round(_quartiles(perr)[1], 1) if perr else None,
            "output_median_abs_pct_err": round(_quartiles(oerr)[1], 1) if oerr else None}


def estimate_usage(conn: sqlite3.Connection, validate: bool = True) -> dict:
    """(Re)compute usage estimates for every message with no reported usage; returns per-account stats
    (also stored in meta k='usage_estimate'). Idempotent: previous estimates are cleared first."""
    conn.execute("UPDATE messages SET usage_estimated=0, est_input_tokens=0, est_cache_read=0, est_cache_write=0,"
                 " est_output_tokens=0, est_cost_usd=0, est_method=NULL WHERE usage_estimated=1")
    accts = [a for (a,) in conn.execute(
        "SELECT DISTINCT account FROM messages WHERE usage_reported=0 AND COALESCE(outcome,'ok')='ok'")]
    stats: dict = {}
    if accts:
        cols = ("msg_id, account, session_id, COALESCE(is_sidechain,0), COALESCE(agent_id,''), ts, model, "
                "input_tokens, output_tokens, cache_read, cache_5m_write, cache_1h_write, content_chars, "
                "COALESCE(usage_reported,1), usage_final, COALESCE(outcome,'ok')")
        keys = ("msg_id", "account", "session_id", "is_sidechain", "agent_id", "ts", "model", "input_tokens",
                "output_tokens", "cache_read", "cache_5m_write", "cache_1h_write", "content_chars",
                "usage_reported", "usage_final", "outcome")
        rows = [dict(zip(keys, t)) for t in conn.execute(
            f"SELECT {cols} FROM messages WHERE account IN ({','.join('?' * len(accts))})"
            " AND COALESCE(outcome,'ok')='ok'", accts)]
        for r in rows:
            r["_t"] = _epoch(r["ts"])
        targets = {r["msg_id"] for r in rows if not r["usage_reported"]}
        calib = _calibrate(rows, set())
        est = _estimate(rows, targets, calib)
        upd = []
        for r in rows:
            e = est.get(r["msg_id"])
            if e is None or e[4] == "none":  # nothing to estimate from: stays "without usage"
                continue
            c = _est_cost(r, e)
            upd.append((e[0], e[2], e[1], e[3], c, e[4], r["msg_id"]))
            s = stats.setdefault(r["account"], {"msgs": 0, "est_cost_usd": 0.0, "methods": {}})
            s["msgs"] += 1
            s["est_cost_usd"] += c
            s["methods"][e[4]] = s["methods"].get(e[4], 0) + 1
        conn.executemany("UPDATE messages SET usage_estimated=1, est_input_tokens=?, est_cache_write=?,"
                         " est_cache_read=?, est_output_tokens=?, est_cost_usd=?, est_method=? WHERE msg_id=?", upd)
        for a in accts:
            s = stats.setdefault(a, {"msgs": 0, "est_cost_usd": 0.0, "methods": {}})
            s["est_cost_usd"] = round(s["est_cost_usd"], 4)
            s["calibration"] = {m: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in c.items()}
                                for (acc, m), c in calib.items() if acc == a and m is not None}
            s["chars_per_token"] = EST_CHARS_PER_TOKEN
            if validate:
                s["validation"] = _validate([r for r in rows if r["account"] == a], targets)
    conn.execute("INSERT INTO meta(k,v) VALUES('usage_estimate', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (json.dumps(stats, sort_keys=True),))
    conn.commit()
    return stats


def _iter_jsonl(root: Path):
    """Walk `root` and yield .jsonl paths, tolerating directories that
    disappear mid-walk (Claude Code rotates session files concurrently)."""
    for dirpath, _dirs, filenames in os.walk(str(root), onerror=lambda _e: None):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield Path(dirpath) / fn


def reindex(
    db_path: Path,
    root: Path | None = None,
    force: bool = False,
    extras: bool = False,
    verbose: bool = True,
    accounts: list[dict] | None = None,
) -> dict:
    """Walk each account root, ingest changed/new .jsonl files. Returns summary.

    Explicit `root` pins a single 'default' account (old behaviour); otherwise
    accounts come from accounts.json / ~/.claude* discovery.
    """
    if accounts is None:
        accounts = (
            [{"name": "personal", "root": root, "priority": 0}] if root is not None else discover_accounts()
        )
    conn = open_db(db_path, extras=extras)
    # daily live-rate check; reprices existing rows if a rate moved (see pricing.py)
    pricing.sync(db_path, conn, force=force, log=print if verbose else (lambda *_: None))
    cur = conn.cursor()
    # One-time backfill per schema version (v4 modifiers, v5 attribution columns + side tables):
    # re-parse every transcript still on disk (upserts keyed on msg_id/uuid, so no duplicates).
    # Rows whose file is gone keep the column defaults. The flag is set only after a clean pass,
    # so a failed pass retries next time.
    backfill = (cur.execute("SELECT v FROM meta WHERE k='backfill_version'").fetchone() or [None])[0] != SCHEMA_VERSION
    if backfill:
        cur.execute("DELETE FROM files")
        conn.commit()
    errors = 0
    cur.execute("SELECT path, size, mtime_ns FROM files")
    seen = {p: (s, m) for p, s, m in cur.fetchall()}
    seen_extras: dict[str, tuple[int, int]] = {}
    if extras:
        cur.execute("SELECT path, size, mtime_ns FROM extras_files")
        seen_extras = {p: (s, m) for p, s, m in cur.fetchall()}

    files_total = files_touched = 0
    msgs_inserted = extras_inserted = 0
    touched_accounts: set[str] = set()
    now_iso = datetime.now(timezone.utc).isoformat()

    for acct in accounts:
        acct_name, acct_root = acct["name"], Path(acct["root"])
        if verbose and len(accounts) > 1:
            print(f"[indexer] account '{acct_name}': {acct_root}")
        for jf in _iter_jsonl(acct_root):
            files_total += 1
            try:
                st = jf.stat()
            except FileNotFoundError:
                continue
            key = str(jf)
            prev = seen.get(key)
            prev_extras = seen_extras.get(key) if extras else None

            do_messages = force or prev != (st.st_size, st.st_mtime_ns)
            do_extras = bool(extras) and (force or prev_extras != (st.st_size, st.st_mtime_ns))
            if not (do_messages or do_extras):
                continue
            files_touched += 1
            try:
                mi, ei = _process_file(
                    conn, jf, acct_root, account=acct_name,
                    source_priority=int(acct.get("priority", 0)),
                    do_messages=do_messages, do_extras=do_extras, tool=acct.get("tool") or TOOL_CLAUDE,
                )
            except Exception as e:
                errors += 1
                if verbose:
                    print(f"[indexer] error in {jf}: {e}", file=sys.stderr)
                continue
            msgs_inserted += mi
            extras_inserted += ei
            if do_messages:
                touched_accounts.add(acct_name)
                cur.execute(
                    "INSERT INTO files(path,size,mtime_ns,rows,last_seen) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
                    "rows=excluded.rows, last_seen=excluded.last_seen",
                    (key, st.st_size, st.st_mtime_ns, mi, now_iso),
                )
            if do_extras:
                cur.execute(
                    "INSERT INTO extras_files(path,size,mtime_ns,rows,last_seen) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
                    "rows=excluded.rows, last_seen=excluded.last_seen",
                    (key, st.st_size, st.st_mtime_ns, ei, now_iso),
                )
            if verbose and files_touched % 25 == 0:
                print(
                    f"[indexer] processed {files_touched} files, "
                    f"+{msgs_inserted} msgs, +{extras_inserted} extras..."
                )
            conn.commit()

    # estimates are re-derived only when an account that has usage-less messages got new data (reprice
    # keeps est_cost_usd current on a rate change); ~0.5s per 10K messages of such an account
    if backfill or cur.execute("SELECT 1 FROM meta WHERE k='usage_estimate'").fetchone() is None or (
            touched_accounts and cur.execute(
                f"SELECT 1 FROM messages WHERE account IN ({','.join('?' * len(touched_accounts))})"
                " AND usage_reported=0 LIMIT 1", sorted(touched_accounts)).fetchone()):
        try:
            estimate_usage(conn)
        except Exception as e:  # an estimate must never break indexing
            errors += 1
            if verbose:
                print(f"[indexer] usage estimate failed: {e}", file=sys.stderr)
    if backfill and not errors:
        cur.execute("INSERT INTO meta(k,v) VALUES('backfill_version',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (SCHEMA_VERSION,))
    cur.execute(
        "INSERT INTO meta(k,v) VALUES('last_index_at', ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (now_iso,),
    )
    if extras:
        cur.execute(
            "INSERT INTO meta(k,v) VALUES('extras_indexed', '1') "
            "ON CONFLICT(k) DO UPDATE SET v='1'",
        )
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM messages")
    total_rows = cur.fetchone()[0]
    extras_rows = 0
    if extras:
        cur.execute("SELECT COUNT(*) FROM tool_calls")
        extras_rows = cur.fetchone()[0]
    conn.close()

    summary = {
        "files_total": files_total,
        "files_changed": files_touched,
        "rows_inserted": msgs_inserted,
        "extras_inserted": extras_inserted,
        "rows_total": total_rows,
        "extras_rows_total": extras_rows,
        "extras_enabled": bool(extras),
        "accounts": [{"name": a["name"], "root": str(a["root"]), "priority": a.get("priority", 0),
                      "tool": a.get("tool") or TOOL_CLAUDE} for a in accounts],
        "last_index_at": now_iso,
    }
    if verbose:
        extra_msg = (
            f", +{extras_inserted} extras (total {extras_rows})" if extras else ""
        )
        print(
            f"[indexer] files: {files_total} (touched {files_touched}), "
            f"new msgs: {msgs_inserted}, total msgs: {total_rows}{extra_msg}"
        )
    return summary


if __name__ == "__main__":
    db = Path(__file__).parent / "usage.db"
    force = "--force" in sys.argv
    extras = "--all" in sys.argv
    reindex(db, force=force, extras=extras)
