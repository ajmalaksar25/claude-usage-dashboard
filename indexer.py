"""Incremental indexer: ~/.claude/projects/**/*.jsonl -> SQLite.

Only re-parses files whose (size, mtime_ns) changed since the last run.
Dedupes assistant messages by Anthropic message id (handles resumed sessions).
Pricing comes from pricing.py; project name is derived from the message's cwd.

When called with extras=True (CLI: --all), also indexes tool calls and slash
prompts into separate tables. Extras are tracked per file in extras_files so
the toggle is incremental on its own track.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from pricing import cost_for_model

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
SCHEMA_VERSION = "2"

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
  is_error        INTEGER DEFAULT 0
);
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
  command         TEXT NOT NULL
);
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


def open_db(db_path: Path, extras: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    if extras:
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


def _process_file(
    conn: sqlite3.Connection,
    jf: Path,
    root: Path,
    do_messages: bool = True,
    do_extras: bool = False,
) -> tuple[int, int]:
    """Returns (messages_inserted, extras_inserted)."""
    if not (do_messages or do_extras):
        return 0, 0
    fallback = _project_fallback(jf, root)
    msg_rows: list[dict] = []
    tool_rows: list[dict] = []
    slash_rows: list[dict] = []
    err_marks: dict[str, bool] = {}

    with open(jf, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if do_messages:
                row = _extract_usage_row(rec, fallback)
                if row:
                    msg_rows.append(row)
            if do_extras:
                tool_rows.extend(_extract_extras_from_assistant(rec, fallback))
                srows, errs = _extract_extras_from_user(rec, fallback)
                slash_rows.extend(srows)
                if errs:
                    err_marks.update(errs)

    cur = conn.cursor()
    msgs_inserted = 0
    extras_inserted = 0

    if do_messages and msg_rows:
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
            msg_rows,
        )
        msgs_inserted = cur.rowcount or 0

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
                   command_verb, is_error)
                VALUES
                  (:tool_use_id, :msg_id, :ts, :ts_day, :session_id, :project, :project_path,
                   :tool_name, :mcp_server, :skill, :subagent_type, :target_path,
                   :command_verb, :is_error)
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
                  (prompt_id, ts, ts_day, session_id, project, command)
                VALUES
                  (:prompt_id, :ts, :ts_day, :session_id, :project, :command)
                ON CONFLICT(prompt_id) DO NOTHING
                """,
                slash_rows,
            )
            extras_inserted += cur.rowcount or 0
    return msgs_inserted, extras_inserted


def _iter_jsonl(root: Path):
    """Walk `root` and yield .jsonl paths, tolerating directories that
    disappear mid-walk (Claude Code rotates session files concurrently)."""
    for dirpath, _dirs, filenames in os.walk(str(root), onerror=lambda _e: None):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield Path(dirpath) / fn


def reindex(
    db_path: Path,
    root: Path = CLAUDE_PROJECTS,
    force: bool = False,
    extras: bool = False,
    verbose: bool = True,
) -> dict:
    """Walk root, ingest changed/new .jsonl files. Returns summary dict."""
    conn = open_db(db_path, extras=extras)
    cur = conn.cursor()
    cur.execute("SELECT path, size, mtime_ns FROM files")
    seen = {p: (s, m) for p, s, m in cur.fetchall()}
    seen_extras: dict[str, tuple[int, int]] = {}
    if extras:
        cur.execute("SELECT path, size, mtime_ns FROM extras_files")
        seen_extras = {p: (s, m) for p, s, m in cur.fetchall()}

    files_total = files_touched = 0
    msgs_inserted = extras_inserted = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for jf in _iter_jsonl(root):
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
            mi, ei = _process_file(conn, jf, root, do_messages=do_messages, do_extras=do_extras)
        except Exception as e:
            if verbose:
                print(f"[indexer] error in {jf}: {e}", file=sys.stderr)
            continue
        msgs_inserted += mi
        extras_inserted += ei
        if do_messages:
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
