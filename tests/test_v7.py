"""v7: other coding agents (Codex CLI, Pi), tool/model/day filters, weekly rhythm, day stats,
plan-shared receipts (paid_by), manual receipts CRUD, profiles API.

Everything runs in a temp dir: transcripts, DB, billing.json, config.json, CCM_HOME. No network."""
import asyncio
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from helpers import M, asst, index, write_jsonl
from test_dashboard_v5 import Base, req

from starlette.requests import Request

import billing
import ccm
import dashboard
import indexer
import pricing

SOL = pricing.EXT_SEED["gpt-5.6-sol"]


def tc(ts, inp, cached, out, reasoning, total=None):
    """A Codex event_msg/token_count with CUMULATIVE totals (last_token_usage is irrelevant)."""
    tu = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
          "reasoning_output_tokens": reasoning, "total_tokens": total if total is not None else inp + out}
    return {"timestamp": ts, "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": tu, "last_token_usage": tu},
                        "rate_limits": {"plan_type": "plus"}}}


def meta(ts, sid, cwd="/work/cx", **extra):
    return {"timestamp": ts, "type": "session_meta",
            "payload": {"id": sid, "cwd": cwd, "originator": "codex_exec", "cli_version": "0.145.0",
                        "source": "exec", "model_provider": "openai", "git": {"branch": "main"}, **extra}}


def turn(ts, model="gpt-5.6-sol", effort="high"):
    return {"timestamp": ts, "type": "turn_context", "payload": {"model": model, "effort": effort, "cwd": "/work/cx"}}


PARENT = [
    meta("2026-10-01T10:00:00.000Z", "parent"),
    turn("2026-10-01T10:00:01.000Z"),
    tc("2026-10-01T10:00:05.000Z", 1000, 400, 100, 50),
    tc("2026-10-01T10:00:05.500Z", 1000, 400, 100, 50),            # repeated total: no new usage
    {"timestamp": "2026-10-01T10:00:06.000Z", "type": "event_msg",
     "payload": {"type": "token_count", "info": None}},              # rate-limit-only event
    tc("2026-10-01T10:00:09.000Z", 3000, 2000, 300, 60),
]
# A subagent fork replays the parent's history (token_counts included) stamped at the fork instant,
# then continues the cumulative counter with its own calls.
FORK = [
    meta("2026-10-01T11:00:00.000Z", "child", forked_from_id="parent", source={"subagent": {"x": 1}}),
    meta("2026-10-01T11:00:00.001Z", "parent"),
    turn("2026-10-01T11:00:00.002Z"),
    tc("2026-10-01T11:00:00.003Z", 1000, 400, 100, 50),
    tc("2026-10-01T11:00:00.004Z", 3000, 2000, 300, 60),
    turn("2026-10-01T11:00:05.000Z"),
    tc("2026-10-01T11:00:08.000Z", 3600, 2400, 400, 70),
]


def pi_msg(ts, rid, model="gpt-5.5", inp=1000, out=100, cr=0, cw=0):
    return {"type": "message", "id": f"r-{rid}", "parentId": None, "timestamp": ts,
            "message": {"role": "assistant", "content": [], "api": "openai-codex-responses",
                        "provider": "openai-codex", "model": model, "responseId": rid, "stopReason": "stop",
                        "usage": {"input": inp, "output": out, "cacheRead": cr, "cacheWrite": cw,
                                  "totalTokens": inp + out + cr + cw, "cost": {"total": 0}}}}


class V7Base(Base):
    def tool_account(self, name, tool, auth=None):
        home = self.d / f".{name}"
        (home / "sessions").mkdir(parents=True, exist_ok=True)
        if auth is not None:
            (home / "auth.json").write_text(json.dumps(auth))
        a = {"name": name, "root": home / "sessions", "priority": 5, "tool": tool}
        self.accounts.append(a)
        return a

    def config(self, d):
        (self.d / "config.json").write_text(json.dumps(d))

    def rows(self, where="1"):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(f"SELECT * FROM messages WHERE {where} ORDER BY ts")]
        finally:
            c.close()


class CodexIngestTest(V7Base):
    def setUp(self):
        super().setUp()
        self.tool_account("codex-cli", "codex-cli")
        self.build({("codex-cli", "2026/10/01/rollout-parent.jsonl"): PARENT,
                    ("codex-cli", "2026/10/01/rollout-child.jsonl"): FORK})

    def test_deltas_not_sums(self):
        rows = self.rows("session_id='parent'")
        self.assertEqual([(r["input_tokens"], r["cache_read"], r["output_tokens"], r["thinking_tokens"]) for r in rows],
                         [(600, 400, 100, 50), (400, 1600, 200, 10)])  # input excludes cached; output incl. reasoning
        r = rows[0]
        self.assertEqual((r["tool"], r["account"], r["model"], r["tier"], r["project"], r["effort"], r["git_branch"]),
                         ("codex-cli", "codex-cli", "gpt-5.6-sol", "gpt-5.6-sol", "cx", "high", "main"))
        self.assertAlmostEqual(r["cost_usd"], (600 * SOL[0] + 100 * SOL[1] + 400 * SOL[4]) / 1e6)

    def test_fork_replay_is_not_counted_twice(self):
        child = self.rows("session_id='child'")
        self.assertEqual(len(child), 1)
        self.assertEqual((child[0]["input_tokens"], child[0]["cache_read"], child[0]["output_tokens"]), (200, 400, 100))
        self.assertEqual(child[0]["is_sidechain"], 1)
        total = sum(r["input_tokens"] + r["cache_read"] + r["output_tokens"] for r in self.rows())
        self.assertEqual(total, 3300 + (4000 - 3300))  # parent's final total + the child's own delta

    def test_reindex_is_idempotent(self):
        with mock.patch.object(indexer.pricing, "sync", return_value={}):
            indexer.reindex(self.db, accounts=self.accounts, verbose=False, force=True)
        self.assertEqual(len(self.rows()), 3)


class PiIngestTest(V7Base):
    def test_pi_rows_dedup_by_response_id(self):
        self.tool_account("pi", "pi")
        sess = {"type": "session", "version": 3, "id": "pi-s1", "timestamp": "2026-07-03T10:00:00.000Z",
                "cwd": "/work/acme"}
        self.build({("pi", "--work-acme--/a.jsonl"): [sess, pi_msg("2026-07-03T10:00:01.000Z", "resp1", cr=500)],
                    ("pi", "--work-acme--/b.jsonl"): [sess, pi_msg("2026-07-03T10:00:01.000Z", "resp1", cr=500)]})
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        g = pricing.EXT_SEED["gpt-5.5"]
        self.assertEqual((r["tool"], r["account"], r["session_id"], r["project"], r["cache_read"]),
                         ("pi", "pi", "pi-s1", "acme", 500))
        self.assertAlmostEqual(r["cost_usd"], (1000 * g[0] + 100 * g[1] + 500 * g[4]) / 1e6)


class DiscoveryTest(unittest.TestCase):
    def test_tool_sources(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            h = Path(t)
            for p in (".codex/sessions", ".codex-alt/sessions", ".codex-empty", ".pi/agent/sessions"):
                (h / p).mkdir(parents=True)
            got = {(a["name"], a["tool"], a["root"].name) for a in indexer.discover_tool_sources(h, env={})}
        self.assertEqual(got, {("codex-cli", "codex-cli", "sessions"), ("codex-cli-alt", "codex-cli", "sessions"),
                               ("pi", "pi", "sessions")})


class SchemaV7Test(V7Base):
    def test_tool_column_backfill_from_v6(self):
        self.account("personal")
        self.build({("personal", "p/s1.jsonl"): [asst("2026-10-01T10:00:00.000Z", "s1", "a", inp=M)]})
        c = sqlite3.connect(self.db)  # make it look like a v6 DB
        c.execute("DROP INDEX idx_messages_cover")
        c.execute("ALTER TABLE messages DROP COLUMN tool")
        c.execute("CREATE INDEX idx_messages_cover ON messages(ts, account, project, model, cost_usd)")
        c.execute("UPDATE meta SET v='6' WHERE k IN ('schema_version', 'backfill_version')")
        c.commit()
        c.close()
        index(self.db, self.accounts)
        c = sqlite3.connect(self.db)
        cover = [r[2] for r in c.execute("PRAGMA index_info(idx_messages_cover)")]
        meta = dict(c.execute("SELECT k, v FROM meta"))
        tools = [r[0] for r in c.execute("SELECT tool FROM messages")]
        c.close()
        self.assertIn("tool", cover)
        self.assertEqual((meta["schema_version"], meta["backfill_version"]),
                         (indexer.SCHEMA_VERSION, indexer.SCHEMA_VERSION))
        self.assertEqual(tools, ["claude-code"])


class FilterTest(V7Base):
    def setUp(self):
        super().setUp()
        self.account("personal")
        self.tool_account("codex-cli", "codex-cli")
        self.build({
            ("personal", "p/s1.jsonl"): [
                asst("2026-10-01T10:00:00.000Z", "s1", "a", model="claude-sonnet-5-20260101", inp=M),
                asst("2026-10-02T23:00:00.000Z", "s1", "b", model="claude-opus-4-5", inp=M),
                {"type": "system", "subtype": "api_error", "uuid": "e1", "timestamp": "2026-10-01T10:00:01.000Z",
                 "sessionId": "s1", "retryAttempt": 1, "maxRetries": 3},
            ],
            ("codex-cli", "2026/10/01/rollout-parent.jsonl"): PARENT,
        })

    def test_tool_filter_on_every_kind_of_endpoint(self):
        s = dashboard.api_summary(req(window="all", tool="codex-cli"))
        self.assertEqual(s["msgs"], 2)
        self.assertEqual(s["non_claude_msgs"], 2)
        both = dashboard.api_summary(req(window="all", tool="claude-code,codex-cli"))
        self.assertEqual(both["msgs"], 4)
        self.assertEqual(dashboard.api_summary(req(window="all"))["msgs"], 4)
        self.assertEqual({r["model"] for r in dashboard.api_by_model(req(window="all", tool="claude-code"))["rows"]},
                         {"claude-sonnet-5-20260101", "claude-opus-4-5"})
        # api_events are Claude Code only
        self.assertEqual(dashboard.api_reliability(req(window="all", tool="codex-cli"))["totals"]["retry"], 0)
        self.assertEqual(dashboard.api_reliability(req(window="all"))["totals"]["retry"], 1)
        self.assertEqual([r["session_id"] for r in dashboard.api_sessions(req(window="all", tool="codex-cli"))["rows"]],
                         ["parent"])

    def test_model_filter_matches_canonical_ids(self):
        s = dashboard.api_summary(req(window="all", model="claude-sonnet-5"))  # raw id carries a date suffix
        self.assertEqual(s["msgs"], 1)
        two = dashboard.api_timeseries(req(window="all", model="gpt-5.6-sol,claude-opus-4-5"))["rows"]
        self.assertEqual(sum(r["msgs"] for r in two), 3)
        self.assertEqual(dashboard.api_summary(req(window="all", model="nope"))["msgs"], 0)

    def test_day_shortcut_equals_from_to(self):
        a = dashboard.api_summary(req(day="2026-10-01"))
        b = dashboard.api_summary(req(**{"from": "2026-10-01", "to": "2026-10-01"}))
        self.assertEqual((a["from"], a["to"], a["msgs"]), (b["from"], b["to"], b["msgs"]))
        self.assertEqual(a["msgs"], 3)

    def test_distribution_matrix_and_heatmap_cost(self):
        d = dashboard.api_distributions(req(window="all"))
        self.assertEqual(sum(c["msgs"] for c in d["hour_dow"]), 4)
        cell = next(c for c in d["hour_dow"] if c["dow"] == 3 and c["hour"] == 10)  # Thu 2026-10-01 10:00 UTC
        self.assertEqual(cell["msgs"], 3)
        self.assertTrue({"dow", "hour", "msgs", "tokens", "cost"} <= set(cell))
        self.assertIn("cost", d["hours"][0])
        hm = dashboard.api_heatmap(req(window="all"))["rows"]
        self.assertTrue(all("cost" in r and "tokens" in r for r in hm))

    def test_summary_day_stats(self):
        s = dashboard.api_summary(req(**{"from": "2026-10-01", "to": "2026-10-03"}))
        self.assertEqual((s["active_days"], s["window_days"]), (2, 3))
        self.assertEqual(s["busiest_day"]["day"], "2026-10-02")  # opus 1M input > sonnet 1M + codex
        self.assertAlmostEqual(s["avg_cost_per_active_day"], round(s["api_cost"] / 2, 2), places=2)
        w = dashboard.api_summary(req(window="1w"))
        self.assertEqual(w["window_days"], 7)
        empty = dashboard.api_summary(req(day="2020-01-01"))
        self.assertEqual((empty["active_days"], empty["busiest_day"], empty["avg_cost_per_active_day"]), (0, None, None))

    def test_accounts_expose_tool(self):
        acc = {a["name"]: a for a in dashboard.api_accounts(req())["accounts"]}
        self.assertEqual((acc["codex-cli"]["tool"], acc["personal"]["tool"]), ("codex-cli", "claude-code"))
        self.assertEqual(acc["codex-cli"]["tools"], ["codex-cli"])


class ToolBillingTest(unittest.TestCase):
    SECRET = "sk-or-token-should-never-leak"

    def detect(self, tool, auth):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "auth.json").write_text(json.dumps(auth))
            return billing.detect_tool_billing(tool, Path(t))

    def test_codex_chatgpt_login_vs_api_key(self):
        sub = self.detect("codex-cli", {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                                        "tokens": {"id_token": self.SECRET, "access_token": self.SECRET,
                                                   "refresh_token": self.SECRET, "account_id": "acc"}})
        self.assertEqual((sub["billing_mode"], sub["paid_by"], sub["signals"]),
                         ("subscription", "openai_chatgpt", ["chatgpt_login"]))
        api = self.detect("codex-cli", {"OPENAI_API_KEY": self.SECRET, "tokens": None})
        self.assertEqual((api["billing_mode"], api["paid_by"]), ("api", "openai_api"))
        self.assertNotIn(self.SECRET, json.dumps([sub, api]))

    def test_pi_oauth(self):
        d = self.detect("pi", {"openai-codex": {"type": "oauth", "access": self.SECRET, "refresh": self.SECRET}})
        self.assertEqual((d["billing_mode"], d["provider"], d["paid_by"]), ("subscription", "openai-codex", "openai_chatgpt"))
        self.assertNotIn(self.SECRET, json.dumps(d))


class PlanShareTest(V7Base):
    """claudex (Claude Code via CLIProxyAPI) and Codex CLI share one ChatGPT plan."""

    def setUp(self):
        super().setUp()
        self.account("claudex", {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "x"}})
        self.tool_account("codex-cli", "codex-cli", auth={"tokens": {"access_token": "t"}})
        self.config({"accounts": {"claudex": {"paid_by": "openai_chatgpt"}}})
        self.build({
            ("claudex", "p/s1.jsonl"): [asst("2026-10-01T10:00:00.000Z", "s1", f"g{i}", model="gpt-5.6-sol", inp=M)
                                        for i in range(3)],  # 3 x $4
            ("codex-cli", "2026/10/01/rollout-parent.jsonl"): [
                meta("2026-10-01T10:00:00.000Z", "cx"), turn("2026-10-01T10:00:01.000Z"),
                tc("2026-10-01T10:00:05.000Z", M, 0, 0, 0)],  # 1 x $4
        })
        billing.save_charges([
            {"receipt_id": "GPA.1", "start": "2026-10-01", "end": "2026-10-31", "plan": "ChatGPT Pro",
             "amount_usd": 100.0, "account": "claudex", "provider": "openai_chatgpt", "source": "gmail"},
            {"receipt_id": "api-1", "start": "2026-10-01", "end": "2026-10-31", "plan": "OpenAI API credits",
             "amount_usd": 50.0, "account": "openai-api", "provider": "openai_api", "source": "gmail"},
            {"receipt_id": "bad", "start": "2026-10-01", "end": "2026-10-31", "plan": "OpenAI API credits",
             "amount_usd": 999.0, "account": "claudex", "provider": "openai_api", "source": "gmail"},
        ])

    def test_plan_split_by_usage_and_never_double_counted(self):
        f, t, _ = dashboard._resolve_window(None, "2026-10-01", "2026-10-30")
        rows = {r["account"]: r for r in dashboard.q_by_account(f, t)}
        self.assertEqual((rows["claudex"]["paid_by"], rows["codex-cli"]["paid_by"]), ("openai_chatgpt", "openai_chatgpt"))
        self.assertEqual(rows["codex-cli"]["tool"], "codex-cli")
        self.assertAlmostEqual(rows["claudex"]["actual_cost_usd"], 75.0)   # 3/4 of the plan
        self.assertAlmostEqual(rows["codex-cli"]["actual_cost_usd"], 25.0)
        s = dashboard.q_summary(f, t)
        self.assertAlmostEqual(s["actual_cost_usd"], 100.0)  # the API-org receipt is not a ChatGPT-plan payment
        self.assertEqual(s["actual_cost_unknown_accounts"], [])
        cx = dashboard.api_summary(req(**{"from": "2026-10-01", "to": "2026-10-30", "tool": "codex-cli"}))
        self.assertAlmostEqual(cx["subscription_cost"], 25.0)
        self.assertEqual({c["receipt_id"] for c in cx["billing_charges"]}, {"GPA.1"})

    def test_openai_api_never_lands_on_plan_accounts(self):
        for acct in ("claudex", "codex-cli"):
            with self.assertRaises(billing.ReceiptError):
                billing.validate_receipt({"start": "2026-10-01", "end": "2026-11-01", "plan": "API",
                                          "amount_usd": 5, "account": acct, "provider": "openai_api"})
        self.config({"accounts": {"claudex": {"paid_by": "openai_api"}}})
        self.assertIsNone(billing.account_billing("claudex", [])["paid_by"])


def _request(method, body=None, query="", host="127.0.0.1:8765", ctype="application/json"):
    raw = json.dumps(body).encode() if body is not None else b""
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": raw, "more_body": False}

    headers = [(b"host", host.encode())] + ([(b"content-type", ctype.encode())] if ctype else [])
    return Request({"type": "http", "method": method, "query_string": query.encode(), "headers": headers,
                    "client": ("127.0.0.1", 50000)}, receive)


class ReceiptsCrudTest(V7Base):
    def post(self, body, **kw):
        return asyncio.run(dashboard.api_receipts_upsert(_request("POST", body, **kw)))

    def test_create_list_replace_delete(self):
        billing.save_charges([{"receipt_id": "G1", "start": "2026-01-01", "end": "2026-02-01", "plan": "Pro",
                               "amount_usd": 20.0, "source": "gmail"}])
        out = self.post({"start": "2026-07-01", "plan": "ChatGPT Pro", "amount_usd": 200, "account": "claudex",
                         "provider": "openai_chatgpt", "recurring": True})
        self.assertTrue(out["ok"])
        rid = out["receipt"]["receipt_id"]
        self.assertEqual(out["receipt"]["source"], "manual")
        self.assertTrue((self.d / "billing.json.bak").exists())  # backed up before the write
        got = dashboard.api_receipts(req(provider="openai_chatgpt"))["receipts"]
        self.assertEqual([r["receipt_id"] for r in got], [rid])
        # replace (same id) adds an end date that stops the recurrence
        self.post({"receipt_id": rid, "start": "2026-07-01", "end": "2026-09-01", "plan": "ChatGPT Pro",
                   "amount_usd": 200, "account": "claudex", "provider": "openai_chatgpt", "recurring": True})
        periods = [c for c in billing.effective_charges() if c["receipt_id"].startswith(rid)]
        self.assertEqual([(c["start"], c["end"]) for c in periods], [("2026-07-01", "2026-08-01"), ("2026-08-01", "2026-09-01")])
        # scraped receipts are neither overwritten nor deleted
        r = self.post({"receipt_id": "G1", "start": "2026-01-01", "end": "2026-02-01", "plan": "x", "amount_usd": 1,
                       "account": "personal"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(dashboard.api_receipts_delete(_request("DELETE", query="receipt_id=G1")).status_code, 409)
        self.assertEqual(dashboard.api_receipts_delete(_request("DELETE", query="receipt_id=nope")).status_code, 404)
        self.assertTrue(dashboard.api_receipts_delete(_request("DELETE", query=f"receipt_id={rid}"))["ok"])
        self.assertEqual([c["receipt_id"] for c in billing.load_charges()], ["G1"])

    def test_validation_and_guards(self):
        bad = [{"start": "2026-07-01", "plan": "x", "amount_usd": 5, "account": "a"},                  # no end
               {"start": "2026-07-01", "end": "2026-06-01", "plan": "x", "amount_usd": 5, "account": "a"},
               {"start": "07/01/2026", "end": "2026-08-01", "plan": "x", "amount_usd": 5, "account": "a"},
               {"start": "2026-07-01", "end": "2026-08-01", "plan": "x", "amount_usd": -5, "account": "a"},
               {"start": "2026-07-01", "end": "2026-08-01", "plan": "x", "amount_usd": 5, "account": "a b"},
               {"start": "2026-07-01", "end": "2026-08-01", "plan": "x", "amount_usd": 5, "account": "a",
                "provider": "bogus"},
               {"start": "2026-07-01", "end": "2026-08-01", "plan": "x", "amount_usd": 5, "account": "a", "evil": 1}]
        for b in bad:
            self.assertEqual(self.post(b).status_code, 400, b)
        ok = {"start": "2026-07-01", "end": "2026-08-01", "plan": "x", "amount_usd": 5, "account": "a"}
        self.assertEqual(self.post(ok, ctype="text/plain").status_code, 415)  # no CORS-simple POSTs
        self.assertEqual(self.post(ok, host="evil.example:8765").status_code, 403)  # DNS rebinding
        self.assertFalse((self.d / "billing.json").exists())


class ProfilesApiTest(V7Base):
    def setUp(self):
        super().setUp()
        self.home = self.d / "home"
        for prof in (".claude", ".claude-personal", ".claude-work", ".claude-codex"):
            (self.home / prof / "projects").mkdir(parents=True)
        for d in ("skills", "agents", "rules"):
            (self.home / ".claude" / d).mkdir()
            (self.home / ".claude" / d / "x.md").write_text("x")
            for prof in (".claude-personal", ".claude-work", ".claude-codex"):
                os.symlink(self.home / ".claude" / d, self.home / prof / d)
        (self.home / ".zshrc").write_text("alias claude-work='CLAUDE_CONFIG_DIR=$HOME/.claude-work claude'\n"
                                          "alias claudex='CLAUDE_CONFIG_DIR=$HOME/.claude-codex claude'\n")
        self.accounts += [
            {"name": "work", "root": self.home / ".claude-work" / "projects", "priority": 40},
            {"name": "claudex", "root": self.home / ".claude-codex" / "projects", "priority": 30},
            {"name": "personal", "root": self.home / ".claude-personal" / "projects", "priority": 20},
            {"name": "personal", "root": self.home / ".claude" / "projects", "priority": 10},
        ]
        write_jsonl(self.home / ".claude" / "projects" / "p" / "s.jsonl",
                    [asst("2026-10-01T10:00:00.000Z", "s1", "a", inp=M)])
        index(self.db, self.accounts)
        env = {"CCM_HOME": str(self.home), "SHELL": "/bin/zsh"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.patches.append(p)

    def test_layout_accounts_and_aliases(self):
        out = dashboard.api_profiles()
        prof = {p["name"]: p for p in out["profiles"]}
        self.assertEqual((out["platform"] in ("darwin", "linux", "windows"), out["shell"]), (True, "zsh"))
        self.assertTrue(out["shared_sources"]["skills"].endswith("/.claude/skills"))
        self.assertEqual(prof["default"]["shared"]["skills"], "source")
        self.assertEqual(prof["codex"]["shared"]["skills"], "linked")
        self.assertEqual(prof["work"]["shared"]["commands"], "missing")
        # default is pre-split personal history: same account, usage shown once (on personal)
        self.assertEqual((prof["default"]["account"], prof["default"]["merged_into"]), ("personal", "personal"))
        self.assertIsNone(prof["default"]["usage"])
        self.assertEqual(prof["personal"]["usage"]["msgs"], 1)
        self.assertIsNone(prof["personal"]["merged_into"])
        self.assertEqual((prof["codex"]["account"], prof["codex"]["display_name"]), ("claudex", "codex → claudex"))
        self.assertTrue(prof["codex"]["account_differs"])
        self.assertEqual((prof["codex"]["alias_installed"], prof["codex"]["alias_installed_name"]), (True, "claudex"))
        self.assertFalse(prof["personal"]["alias_installed"])
        al = prof["work"]["alias"]
        self.assertEqual(al["primary"]["shell"], "zsh")
        self.assertEqual(set(al["other"]), {"bash", "fish", "powershell"})
        self.assertTrue(al["primary"]["rc_file"].endswith(".zshrc"))


class FrontendContractTest(V7Base):
    """The UI's contracts: model= everywhere except by_model, day drill-down with hourly buckets,
    hour_dow, and breakdown dimensions as page filters."""

    def setUp(self):
        super().setUp()
        self.account("personal")
        v = {"version": "2.1.0"}
        self.build({("personal", "p/s1.jsonl"): [
            asst("2026-10-01T10:05:00.000Z", "s1", "a", model="claude-sonnet-5", inp=M, gitBranch="main",
                 entrypoint="cli", **v),
            asst("2026-10-01T10:30:00.000Z", "s1", "b", model="claude-opus-4-5", inp=M, isSidechain=True,
                 attributionAgent="Explore", attributionSkill="pdf", gitBranch="main", entrypoint="cli", **v),
            asst("2026-10-01T13:00:00.000Z", "s1", "c", model="claude-opus-4-5", out=M, attributionMcpServer="plane",
                 gitBranch="feat/x", entrypoint="cli", effort="high", **v),
            {"type": "pr-link", "sessionId": "s1", "prNumber": 7, "prUrl": "https://g/r/pull/7", "prRepository": "o/r",
             "timestamp": "2026-10-01T13:01:00.000Z"},
        ], ("personal", "p/s2.jsonl"): [
            asst("2026-10-02T09:00:00.000Z", "s2", "d", model="claude-sonnet-5", inp=M, entrypoint="sdk-py", **v),
        ]})

    def test_model_filters_everything_but_by_model(self):
        q = {"window": "all", "model": "claude-opus-4-5"}
        self.assertEqual(dashboard.api_summary(req(**q))["msgs"], 2)
        self.assertEqual(sum(r["msgs"] for r in dashboard.api_heatmap(req(**q))["rows"]), 2)
        self.assertEqual(len(dashboard.api_by_model(req(**q))["rows"]), 2)  # both models, for highlighting
        rep = Request({"type": "http", "headers": [],
                       "query_string": b"window=all&model=claude-opus-4-5&model=claude-sonnet-5"})
        self.assertEqual(dashboard.api_summary(rep)["msgs"], 4)  # repeated param

    def test_day_drilldown_hourly_by_model(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_TZ": "Asia/Kolkata"}):
            q = {"from": "2026-10-01", "to": "2026-10-01", "bucket": "hour"}
            rows = dashboard.api_timeseries(req(**q))["rows"]
            self.assertEqual([r["bucket"] for r in rows], ["2026-10-01T15", "2026-10-01T16", "2026-10-01T18"])
            by = dashboard.api_timeseries(req(**q, by="model"))["rows"]
            self.assertEqual([(r["bucket"], r["model"]) for r in by],
                             [("2026-10-01T15", "claude-sonnet-5"), ("2026-10-01T16", "claude-opus-4-5"),
                              ("2026-10-01T18", "claude-opus-4-5")])

    def test_hour_dow_in_configured_zone(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_TZ": "Asia/Kolkata"}):
            hd = dashboard.api_distributions(req(window="all"))["hour_dow"]
        cells = {(c["dow"], c["hour"]): c["msgs"] for c in hd}
        # Thu 2026-10-01 10:05Z = 15:35 IST; Fri 09:00Z = 14:30 IST; dow 0 = Monday
        self.assertEqual(cells, {(3, 15): 1, (3, 16): 1, (3, 18): 1, (4, 14): 1})
        self.assertTrue(all({"dow", "hour", "msgs", "tokens", "cost"} <= set(c) for c in hd))

    def test_breakdown_dimensions_filter_every_endpoint(self):
        def msgs(**q):
            return dashboard.api_summary(req(window="all", **q))["msgs"]
        self.assertEqual(msgs(agent="Explore"), 1)
        self.assertEqual(msgs(agent="(main thread)"), 3)
        self.assertEqual(msgs(skill="pdf"), 1)
        self.assertEqual(msgs(mcp="plane"), 1)
        self.assertEqual(msgs(branch="main"), 2)
        self.assertEqual(msgs(entrypoint="sdk-py"), 1)
        self.assertEqual(msgs(effort="high"), 1)
        self.assertEqual(msgs(sidechain="1"), 1)
        self.assertEqual(msgs(sidechain="0"), 3)
        self.assertEqual(msgs(plugin="(none)"), 4)
        self.assertEqual(msgs(pr="o/r#7"), 3)
        self.assertEqual(msgs(pr="(no PR)"), 1)
        self.assertEqual(msgs(tool="claude-code", branch="main", model="claude-opus-4-5"), 1)
        self.assertEqual(sum(r["msgs"] for r in dashboard.api_timeseries(req(window="all", branch="feat/x"))["rows"]), 1)
        self.assertEqual([r["session_id"] for r in dashboard.api_sessions(req(window="all", entrypoint="sdk-py"))["rows"]],
                         ["s2"])
        # a breakdown keeps every row of its own dimension, filtered by the others
        bd = dashboard.api_breakdown(req(window="all", dim="branch", branch="main"))
        self.assertEqual({r["value"] for r in bd["rows"]}, {"main", "feat/x", "(none)"})
        bd = dashboard.api_breakdown(req(window="all", dim="branch", entrypoint="cli"))
        self.assertEqual({r["value"] for r in bd["rows"]}, {"main", "feat/x"})


if __name__ == "__main__":
    unittest.main()


class PayerTest(V7Base):
    """accounts.<name>.payer = employer: the plan counts as value, not as your out-of-pocket cost."""

    def setUp(self):
        super().setUp()
        self.account("personal")
        self.account("work")
        self.config({"accounts": {"work": {"payer": "employer"}}})
        self.build({
            ("personal", "p/s1.jsonl"): [asst("2026-10-01T10:00:00.000Z", "s1", "a1", inp=M)],  # $3
            ("work", "p/s2.jsonl"): [asst("2026-10-01T10:00:00.000Z", "s2", "b1", inp=2 * M),
                                     asst("2026-09-01T10:00:00.000Z", "s3", "b2", inp=M)],
        })
        billing.save_charges([
            {"receipt_id": "p1", "start": "2026-10-01", "end": "2026-10-31", "plan": "Pro",
             "amount_usd": 20.0, "account": "personal", "provider": "anthropic", "source": "manual"},
            {"receipt_id": "w1", "start": "2026-10-01", "end": "2026-10-31", "plan": "Team Premium",
             "amount_usd": 125.0, "account": "work", "provider": "anthropic", "source": "manual"},
        ])

    def summary(self, **q):
        return dashboard.api_summary(req(**{"from": "2026-10-01", "to": "2026-10-31", **q}))

    def test_summary_splits_payer(self):
        s = self.summary()
        self.assertAlmostEqual(s["subscription_cost"], 145.0)
        self.assertAlmostEqual(s["paid_by_employer_usd"], 125.0)
        self.assertAlmostEqual(s["paid_by_you_usd"], 20.0)
        self.assertEqual(s["employer_accounts"], ["work"])
        self.assertAlmostEqual(s["savings"], s["api_cost"] - 145.0, places=2)
        personal_api = {r["account"]: r["api_cost"] for r in dashboard.q_by_account(*dashboard._resolve_window(
            None, "2026-10-01", "2026-10-31")[:2])}["personal"]
        self.assertAlmostEqual(s["savings_you"], personal_api - 20.0)  # only personal's API cost vs what you paid

    def test_filters_respect_payer(self):
        w = self.summary(account="work")
        self.assertAlmostEqual((w["subscription_cost"], w["paid_by_employer_usd"], w["paid_by_you_usd"]),
                               (125.0, 125.0, 0.0))
        self.assertEqual(w["employer_accounts"], ["work"])
        p = self.summary(account="personal")
        self.assertAlmostEqual((p["paid_by_employer_usd"], p["paid_by_you_usd"]), (0.0, 20.0))
        self.assertEqual(p["employer_accounts"], [])
        # a window with no work receipt period: employer share is zero
        sep = dashboard.api_summary(req(**{"from": "2026-09-01", "to": "2026-09-30"}))
        self.assertEqual((sep["paid_by_employer_usd"], sep["paid_by_you_usd"]), (0.0, 0.0))

    def test_account_rows(self):
        f, t, _ = dashboard._resolve_window(None, "2026-10-01", "2026-10-30")
        rows = {r["account"]: r for r in dashboard.q_by_account(f, t)}
        self.assertEqual((rows["work"]["payer"], rows["work"]["you_paid_usd"]), ("employer", 0.0))
        self.assertAlmostEqual(rows["work"]["plan_value_usd"], rows["work"]["subscription_cost"])
        self.assertGreater(rows["work"]["plan_value_usd"], 0)
        self.assertEqual(rows["personal"]["payer"], "self")
        self.assertAlmostEqual(rows["personal"]["you_paid_usd"], rows["personal"]["actual_cost_usd"])
        acc = {a["name"]: a for a in dashboard.api_accounts(req(**{"from": "2026-10-01", "to": "2026-10-30"}))["accounts"]}
        self.assertEqual((acc["work"]["payer"], acc["work"]["you_paid_usd"]), ("employer", 0.0))
        self.assertAlmostEqual(acc["work"]["plan_value_usd"], rows["work"]["plan_value_usd"], places=2)
        self.assertEqual(acc["personal"]["payer"], "self")
        self.assertAlmostEqual(acc["personal"]["plan_value_usd"], 20.0, places=1)

    def test_bad_payer_defaults_to_self(self):
        self.config({"accounts": {"work": {"payer": "nobody"}}})
        self.assertEqual(billing.account_billing("work", [])["payer"], "self")


class GmailStatusTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        import gmail_scraper
        self.g = gmail_scraper
        self.tmp = tempfile.TemporaryDirectory()
        self.tok = Path(self.tmp.name) / "token.json"
        self.tok.write_text("{}")
        self.creds = Path(self.tmp.name) / "credentials.json"
        gmail_scraper._refresh_cache.clear()

    def tearDown(self):
        self.g._refresh_cache.clear()
        self.tmp.cleanup()

    def fake(self, valid=False, refresh=None):
        c = mock.Mock(valid=valid, refresh_token="r")
        c.refresh.side_effect = refresh
        return mock.patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=c), c

    def test_revoked_token_is_not_connected_and_cached(self):
        p, c = self.fake(refresh=Exception("invalid_grant: Token has been expired or revoked."))
        with p:
            s = self.g.status(self.creds, self.tok)
            self.g.status(self.creds, self.tok)
        self.assertFalse(s["connected"])
        self.assertFalse(s["refresh_ok"])
        self.assertIn("invalid_grant", s["error"])
        self.assertEqual(c.refresh.call_count, 1)  # second call served from the cache

    def test_cache_expires(self):
        p, c = self.fake(refresh=Exception("invalid_grant"))
        with p:
            self.g.status(self.creds, self.tok)
            key = str(self.tok)
            ts, ok, err = self.g._refresh_cache[key]
            self.g._refresh_cache[key] = (ts - 700, ok, err)
            self.g.status(self.creds, self.tok)
        self.assertEqual(c.refresh.call_count, 2)

    def test_good_refresh_and_missing_token(self):
        c = mock.Mock(valid=False, refresh_token="r")
        c.refresh.side_effect = lambda req: setattr(c, "valid", True)
        with mock.patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=c):
            s = self.g.status(self.creds, self.tok)
        self.assertEqual((s["connected"], s["refresh_ok"], s["error"]), (True, True, None))
        none = self.g.status(self.creds, Path(self.tmp.name) / "absent.json")
        self.assertEqual((none["connected"], none["refresh_ok"]), (False, None))

    def test_token_values_are_redacted(self):
        secret = "ya29." + "A" * 60
        p, _ = self.fake(refresh=Exception(f"bad {secret}"))
        with p:
            s = self.g.status(self.creds, self.tok)
        self.assertNotIn(secret, json.dumps(s))
