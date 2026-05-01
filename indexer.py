"""Incremental indexer: ~/.claude/projects/**/*.jsonl -> SQLite.

Only re-parses files whose (size, mtime_ns) changed since the last run.
Dedupes assistant messages by Anthropic message id (handles resumed sessions).
Pricing comes from pricing.py; project name is derived from the message's cwd.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from pricing import cost_for_model

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
SCHEMA_VERSION = "1"

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
  cost_usd        REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts        ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_day       ON messages(ts_day);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project, ts);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_model     ON messages(model);

CREATE TABLE IF NOT EXISTS files (
  path      TEXT PRIMARY KEY,
  size      INTEGER,
  mtime_ns  INTEGER,
  rows      INTEGER,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
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


def _extract_usage_row(rec: dict, project_fallback: str) -> dict | None:
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
    cost, tier = cost_for_model(model, inp, out, c5w, c1w, cr=cread)

    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    dt_local = dt.astimezone()  # local timezone for day/hour buckets
    project, project_path = _project_name_from_cwd(rec.get("cwd"), project_fallback)

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
        "tier": tier,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_5m_write": c5w,
        "cache_1h_write": c1w,
        "cache_read": cread,
        "cost_usd": cost,
    }


def _project_fallback(jsonl_path: Path, root: Path) -> str:
    rel = jsonl_path.relative_to(root)
    parts = rel.parts
    return parts[0] if parts else jsonl_path.stem


def _process_file(conn: sqlite3.Connection, jf: Path, root: Path) -> int:
    fallback = _project_fallback(jf, root)
    rows = []
    with open(jf, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            row = _extract_usage_row(rec, fallback)
            if row:
                rows.append(row)
    if not rows:
        return 0
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO messages
          (msg_id, ts, ts_day, ts_hour, ts_dow, session_id, project, project_path,
           model, tier, input_tokens, output_tokens, cache_5m_write, cache_1h_write,
           cache_read, cost_usd)
        VALUES
          (:msg_id, :ts, :ts_day, :ts_hour, :ts_dow, :session_id, :project, :project_path,
           :model, :tier, :input_tokens, :output_tokens, :cache_5m_write, :cache_1h_write,
           :cache_read, :cost_usd)
        ON CONFLICT(msg_id) DO NOTHING
        """,
        rows,
    )
    return cur.rowcount or 0


def reindex(db_path: Path, root: Path = CLAUDE_PROJECTS, force: bool = False, verbose: bool = True) -> dict:
    """Walk root, ingest changed/new .jsonl files. Returns summary dict."""
    conn = open_db(db_path)
    cur = conn.cursor()
    cur.execute("SELECT path, size, mtime_ns FROM files")
    seen = {p: (s, m) for p, s, m in cur.fetchall()}

    files_total = files_changed = rows_inserted = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for jf in root.rglob("*.jsonl"):
        files_total += 1
        try:
            st = jf.stat()
        except FileNotFoundError:
            continue
        key = str(jf)
        prev = seen.get(key)
        if not force and prev == (st.st_size, st.st_mtime_ns):
            continue
        files_changed += 1
        before = conn.total_changes
        try:
            _process_file(conn, jf, root)
        except Exception as e:
            if verbose:
                print(f"[indexer] error in {jf}: {e}", file=sys.stderr)
            continue
        added = conn.total_changes - before
        rows_inserted += added
        cur.execute(
            "INSERT INTO files(path,size,mtime_ns,rows,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "rows=excluded.rows, last_seen=excluded.last_seen",
            (key, st.st_size, st.st_mtime_ns, added, now_iso),
        )
        if verbose and files_changed % 25 == 0:
            print(f"[indexer] processed {files_changed} files, +{rows_inserted} rows...")
        conn.commit()

    cur.execute(
        "INSERT INTO meta(k,v) VALUES('last_index_at', ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (now_iso,),
    )
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM messages")
    total_rows = cur.fetchone()[0]
    conn.close()

    summary = {
        "files_total": files_total,
        "files_changed": files_changed,
        "rows_inserted": rows_inserted,
        "rows_total": total_rows,
        "last_index_at": now_iso,
    }
    if verbose:
        print(
            f"[indexer] files: {files_total} (changed {files_changed}), "
            f"new rows: {rows_inserted}, total rows: {total_rows}"
        )
    return summary


if __name__ == "__main__":
    db = Path(__file__).parent / "usage.db"
    force = "--force" in sys.argv
    reindex(db, force=force)
