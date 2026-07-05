"""claude-usage: the dashboard's numbers, in your terminal.

Same SQLite database, same filters (window / account / project), no browser
and no server. Reads usage.db produced by the indexer; `refresh` re-indexes
your ~/.claude*/projects logs first.

    claude-usage                      headline numbers (all time)
    claude-usage summary -w 1m        headline numbers, last month
    claude-usage projects -w 1w       top projects this week
    claude-usage models               tokens + cost per model
    claude-usage accounts             per-account rollup
    claude-usage tools                tool usage        (needs: refresh --all)
    claude-usage calls -n 20          latest tool calls (needs: refresh --all)
    claude-usage refresh --all        re-index logs incl. tool calls
    claude-usage dash                 launch the web dashboard

Filters compose: `claude-usage tools -w 1m -a work -p myproject`.
DB resolution: --db flag > CLAUDE_USAGE_DB env > usage.db next to this file
> ~/.claude-usage/usage.db (used when installed as a package).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINDOWS = {"today": 0, "1w": 7, "15d": 15, "1m": 30, "3m": 90, "6m": 180, "1y": 365}

TOKEN_SUM = "input_tokens+output_tokens+cache_5m_write+cache_1h_write+cache_read"


# ------------------------------------------------------------------ helpers

def db_path(cli_arg: str | None) -> Path:
    if cli_arg:
        return Path(cli_arg).expanduser()
    env = os.environ.get("CLAUDE_USAGE_DB")
    if env:
        return Path(env).expanduser()
    local = Path(__file__).resolve().parent / "usage.db"
    if local.exists() or os.access(local.parent, os.W_OK):
        return local
    return Path.home() / ".claude-usage" / "usage.db"


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path} - run `claude-usage refresh` first")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def resolve_window(window: str) -> tuple[str, str, str]:
    """(from_iso, to_iso, label) in UTC, mirroring the dashboard."""
    now = datetime.now(timezone.utc)
    window = (window or "all").lower()
    if window == "today":
        local = datetime.now().astimezone()
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (start.astimezone(timezone.utc).isoformat(),
                (start + timedelta(days=1)).astimezone(timezone.utc).isoformat(), "today")
    days = WINDOWS.get(window)
    if days is None:
        return "1970-01-01T00:00:00+00:00", (now + timedelta(days=1)).isoformat(), "all"
    return (now - timedelta(days=days)).isoformat(), (now + timedelta(days=1)).isoformat(), window


def filters(args) -> tuple[str, list]:
    """WHERE fragment + params for window/account/project."""
    frm, to, _ = resolve_window(args.window)
    frag, params = "ts >= ? AND ts < ?", [frm, to]
    if getattr(args, "account", None):
        frag += " AND account = ?"
        params.append(args.account)
    if getattr(args, "project", None):
        frag += " AND project = ?"
        params.append(args.project)
    return frag, params


def fmt_money(v) -> str:
    return f"${v:,.2f}" if v is not None else "-"


def fmt_compact(n) -> str:
    n = n or 0
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suffix}"
    return str(int(n))


def table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> None:
    if not rows:
        print("  (nothing in this window)")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    def line(cells):
        out = []
        for i, c in enumerate(cells):
            c = str(c)
            out.append(c.rjust(widths[i]) if i in right else c.ljust(widths[i]))
        return "  " + "  ".join(out)
    print(line(headers))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))


def scope_line(args) -> str:
    bits = [f"window: {args.window}"]
    if getattr(args, "account", None):
        bits.append(f"account: {args.account}")
    if getattr(args, "project", None):
        bits.append(f"project: {args.project}")
    return " | ".join(bits)


def has_extras(conn) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    ).fetchone()
    return r is not None


# ------------------------------------------------------------------ commands

def cmd_summary(conn, args) -> int:
    frag, p = filters(args)
    r = conn.execute(f"""
        SELECT COUNT(*) AS msgs, COUNT(DISTINCT session_id) AS sessions,
               COUNT(DISTINCT project) AS projects,
               COALESCE(SUM({TOKEN_SUM}),0) AS tokens,
               COALESCE(SUM(input_tokens),0) AS inp,
               COALESCE(SUM(output_tokens),0) AS out,
               COALESCE(SUM(cost_usd),0) AS cost
        FROM messages WHERE {frag}""", p).fetchone()
    print(f"\n  Claude usage  ({scope_line(args)})\n")
    print(f"  Would have cost   {fmt_money(r['cost'])}  at standard API rates")
    print(f"  Total tokens      {fmt_compact(r['tokens'])}   (in {fmt_compact(r['inp'])} / out {fmt_compact(r['out'])})")
    print(f"  Messages          {r['msgs']:,}")
    print(f"  Sessions          {r['sessions']:,}  across {r['projects']:,} projects")
    if has_extras(conn):
        t = conn.execute(f"SELECT COUNT(*) AS n, SUM(is_error) AS errs FROM tool_calls WHERE {frag}", p).fetchone()
        print(f"  Tool calls        {t['n']:,}  ({t['errs'] or 0:,} errors)")
    print()
    return 0


def cmd_projects(conn, args) -> int:
    frag, p = filters(args)
    rows = conn.execute(f"""
        SELECT project, COUNT(*) AS msgs, COUNT(DISTINCT session_id) AS sessions,
               COALESCE(SUM({TOKEN_SUM}),0) AS tokens, COALESCE(SUM(cost_usd),0) AS cost
        FROM messages WHERE {frag}
        GROUP BY project ORDER BY tokens DESC LIMIT ?""", (*p, args.limit)).fetchall()
    print(f"\n  Top projects  ({scope_line(args)})\n")
    table(["project", "msgs", "sessions", "tokens", "cost"],
          [[r["project"] or "-", f"{r['msgs']:,}", f"{r['sessions']:,}",
            fmt_compact(r["tokens"]), fmt_money(r["cost"])] for r in rows],
          right={1, 2, 3, 4})
    print()
    return 0


def cmd_models(conn, args) -> int:
    frag, p = filters(args)
    rows = conn.execute(f"""
        SELECT model, COUNT(*) AS msgs,
               COALESCE(SUM({TOKEN_SUM}),0) AS tokens, COALESCE(SUM(cost_usd),0) AS cost
        FROM messages WHERE {frag}
        GROUP BY model ORDER BY tokens DESC""", p).fetchall()
    print(f"\n  By model  ({scope_line(args)})\n")
    table(["model", "msgs", "tokens", "cost"],
          [[r["model"], f"{r['msgs']:,}", fmt_compact(r["tokens"]), fmt_money(r["cost"])]
           for r in rows], right={1, 2, 3})
    print()
    return 0


def cmd_accounts(conn, args) -> int:
    frag, p = filters(args)
    rows = conn.execute(f"""
        SELECT account, COUNT(*) AS msgs, COUNT(DISTINCT project) AS projects,
               COALESCE(SUM({TOKEN_SUM}),0) AS tokens, COALESCE(SUM(cost_usd),0) AS cost
        FROM messages WHERE {frag}
        GROUP BY account ORDER BY tokens DESC""", p).fetchall()
    print(f"\n  By account  ({scope_line(args)})\n")
    table(["account", "msgs", "projects", "tokens", "would-have-cost"],
          [[r["account"], f"{r['msgs']:,}", f"{r['projects']:,}",
            fmt_compact(r["tokens"]), fmt_money(r["cost"])] for r in rows],
          right={1, 2, 3, 4})
    print()
    return 0


def cmd_tools(conn, args) -> int:
    if not has_extras(conn):
        sys.exit("tool calls are not indexed - run `claude-usage refresh --all` first")
    frag, p = filters(args)
    rows = conn.execute(f"""
        SELECT tool_name, COUNT(*) AS uses, SUM(is_error) AS errors
        FROM tool_calls WHERE {frag}
        GROUP BY tool_name ORDER BY uses DESC LIMIT ?""", (*p, args.limit)).fetchall()
    print(f"\n  Tool usage  ({scope_line(args)})\n")
    table(["tool", "uses", "errors"],
          [[r["tool_name"], f"{r['uses']:,}", f"{r['errors'] or 0:,}"] for r in rows],
          right={1, 2})
    print()
    return 0


def cmd_calls(conn, args) -> int:
    if not has_extras(conn):
        sys.exit("tool calls are not indexed - run `claude-usage refresh --all` first")
    frag, p = filters(args)
    if args.tool:
        frag += " AND tool_name = ?"
        p.append(args.tool)
    if args.errors:
        frag += " AND is_error = 1"
    rows = conn.execute(f"""
        SELECT ts, tool_name, project, target_path, command_verb, skill,
               subagent_type, mcp_server, is_error
        FROM tool_calls WHERE {frag}
        ORDER BY ts DESC LIMIT ?""", (*p, args.limit)).fetchall()
    def detail(r):
        d = (r["command_verb"] if r["tool_name"] == "Bash" and r["command_verb"] else None) \
            or (("/" + r["skill"]) if r["skill"] else None) \
            or r["subagent_type"] or r["target_path"] or r["mcp_server"] or ""
        return d if len(d) <= 60 else "..." + d[-57:]
    print(f"\n  Latest tool calls  ({scope_line(args)})\n")
    table(["time", "tool", "detail", "project", ""],
          [[r["ts"][:19].replace("T", " "), r["tool_name"], detail(r),
            r["project"] or "-", "ERR" if r["is_error"] else ""] for r in rows])
    print()
    return 0


def cmd_refresh(args) -> int:
    from indexer import reindex
    path = db_path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = reindex(path, force=args.force, extras=args.all)
    print(f"db: {path}")
    return 0 if summary else 1


def cmd_dash(args) -> int:
    import dashboard
    if args.all and "--all" not in sys.argv:
        sys.argv.append("--all")
    dashboard.main()
    return 0


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="claude-usage",
        description="Claude Code usage stats in the terminal (same data as the dashboard).",
    )
    ap.add_argument("--db", help="path to usage.db (default: auto-resolve)")
    sub = ap.add_subparsers(dest="cmd")

    def common(sp, limit=None):
        sp.add_argument("-w", "--window", default="all",
                        help="all|1y|6m|3m|1m|15d|1w|today (default: all)")
        sp.add_argument("-a", "--account", default="", help="filter to one account")
        sp.add_argument("-p", "--project", default="", help="filter to one project")
        if limit:
            sp.add_argument("-n", "--limit", type=int, default=limit)

    common(sub.add_parser("summary", help="headline numbers (default command)"))
    common(sub.add_parser("projects", help="top projects"), limit=15)
    common(sub.add_parser("models", help="tokens + cost per model"))
    common(sub.add_parser("accounts", help="per-account rollup"))
    common(sub.add_parser("tools", help="tool usage counts"), limit=20)
    calls = sub.add_parser("calls", help="latest individual tool calls")
    common(calls, limit=25)
    calls.add_argument("-t", "--tool", default="", help="filter to one tool name")
    calls.add_argument("--errors", action="store_true", help="errored calls only")

    ref = sub.add_parser("refresh", help="re-index ~/.claude*/projects logs")
    ref.add_argument("--all", action="store_true", help="also index tool calls + slash prompts")
    ref.add_argument("--force", action="store_true", help="re-parse every file")

    dash = sub.add_parser("dash", help="launch the web dashboard")
    dash.add_argument("--all", action="store_true", help="enable the Activity & tools tab")

    args = ap.parse_args(argv)
    cmd = args.cmd or "summary"
    if cmd == "refresh":
        return cmd_refresh(args)
    if cmd == "dash":
        return cmd_dash(args)
    if args.cmd is None:  # bare `claude-usage` -> summary with defaults
        args.window, args.account, args.project = "all", "", ""

    conn = connect(db_path(args.db))
    try:
        return {"summary": cmd_summary, "projects": cmd_projects, "models": cmd_models,
                "accounts": cmd_accounts, "tools": cmd_tools, "calls": cmd_calls}[cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
