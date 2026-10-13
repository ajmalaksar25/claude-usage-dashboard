"""Dashboard v5: time zones, side-call reconciliation, breakdowns, reliability, sessions, accounts.

Builds transcripts in a temp dir, indexes them, and points dashboard.DB_PATH at the temp DB.
Account discovery, billing.json and config.json are redirected to temp paths; the gateway probe
is faked. Nothing touches the network, ~/.claude* or usage.db.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from helpers import M, asst, cost_state, index, ms, reset_pricing, write_jsonl

from starlette.requests import Request

import billing
import dashboard
import pricing

IST = "Asia/Kolkata"


def req(**q):
    qs = "&".join(f"{k}={v}" for k, v in q.items()).encode()
    return Request({"type": "http", "query_string": qs, "headers": []})


class Base(unittest.TestCase):
    def setUp(self):
        reset_pricing()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.db = self.d / "usage.db"
        self.patches = [
            mock.patch.object(dashboard, "DB_PATH", self.db),
            mock.patch.object(billing, "BILLING_PATH", self.d / "billing.json"),
            mock.patch.dict(os.environ, {"CLAUDE_USAGE_CONFIG": str(self.d / "config.json"),
                                         "CLAUDE_USAGE_TZ": "UTC"}),
            mock.patch.object(billing, "_probe_gateway", lambda host: "cliproxyapi"),
        ]
        for p in self.patches:
            p.start()
        self.accounts = []
        self.patches.append(mock.patch.object(dashboard, "discover_accounts", lambda: self.accounts))
        self.patches[-1].start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def account(self, name, settings=None, priority=10):
        cfg = self.d / f".claude-{name}"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        if settings is not None:
            (cfg / "settings.json").write_text(json.dumps(settings))
        a = {"name": name, "root": cfg / "projects", "priority": priority}
        self.accounts.append(a)
        return a

    def build(self, files: dict):
        """files: {(account, relpath): [records]}"""
        for (acct, rel), recs in files.items():
            root = next(a["root"] for a in self.accounts if a["name"] == acct)
            write_jsonl(root / rel, recs)
        index(self.db, self.accounts)


class TimezoneTest(Base):
    def setUp(self):
        super().setUp()
        self.account("personal")
        # 18:29Z = 23:59 IST on the 6th; 18:30Z = 00:00 IST on the 7th
        self.build({("personal", "p/s1.jsonl"): [
            asst("2026-10-06T18:29:00.000Z", "s1", "a", inp=M),
            asst("2026-10-06T18:30:00.000Z", "s1", "b", inp=M),
        ]})

    def test_from_to_are_local_days(self):
        f, t, _ = dashboard._resolve_window(None, "2026-10-07", "2026-10-07", tz=dashboard._zone(IST))
        self.assertEqual((f, t), ("2026-10-06T18:30:00+00:00", "2026-10-07T18:30:00+00:00"))
        f, t, _ = dashboard._resolve_window(None, "2026-10-07", "2026-10-07", tz=timezone.utc)
        self.assertEqual((f, t), ("2026-10-07T00:00:00+00:00", "2026-10-08T00:00:00+00:00"))

    def test_ist_day_boundary_buckets(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_TZ": IST}):
            f, t, _ = dashboard._resolve_window(None, "2026-10-07", "2026-10-07")
            self.assertEqual(dashboard.q_summary(f, t)["msgs"], 1)  # only the 00:00 IST row
            f, t, _ = dashboard._resolve_window("all", None, None)
            self.assertEqual([(r["day"], r["msgs"]) for r in dashboard.q_heatmap(f, t)],
                             [("2026-10-06", 1), ("2026-10-07", 1)])
            self.assertEqual([r["bucket"] for r in dashboard.q_timeseries(f, t, "hour")],
                             ["2026-10-06T23", "2026-10-07T00"])
            dist = dashboard.q_distributions(f, t)
            self.assertEqual([r["hour"] for r in dist["hours"]], [0, 23])
            self.assertEqual(sorted(r["dow"] for r in dist["dows"]), [1, 2])  # Tue 6th, Wed 7th
            self.assertEqual(dashboard.q_summary(f, t)["timezone"], IST)
        # same rows in UTC: both on the 6th
        f, t, _ = dashboard._resolve_window("all", None, None)
        self.assertEqual([(r["day"], r["msgs"]) for r in dashboard.q_heatmap(f, t)], [("2026-10-06", 2)])

    def test_today_starts_at_local_midnight(self):
        tz = dashboard._zone(IST)
        f, t, _ = dashboard._resolve_window("today", None, None, tz=tz)
        fl = datetime.fromisoformat(f).astimezone(tz)
        self.assertEqual((fl.hour, fl.minute), (0, 0))
        self.assertEqual((datetime.fromisoformat(t) - datetime.fromisoformat(f)).total_seconds(), 86400)

    def test_config_file_sets_zone(self):
        (self.d / "config.json").write_text(json.dumps({"timezone": IST}))
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_TZ": ""}):
            self.assertEqual(dashboard.tz_name(), IST)


class ReconciliationTest(Base):
    """cc_gap = side calls (evidenced) + subagent partial output + unexplained; out-of-window excluded."""

    def setUp(self):
        super().setUp()
        self.account("personal")
        H = 3600 * 1000
        self.out_rate = pricing.rate_for("claude-sonnet-5")[0][1]
        self.partial_usd = 100_000 * self.out_rate / M
        self.build({
            ("personal", "p/s1.jsonl"): [
                # before the first recorded process: a resume whose process wrote no cost-state
                asst("2026-10-01T09:00:00.000Z", "s1", "early", inp=M // 2),
                asst("2026-10-01T10:00:00.000Z", "s1", "a", inp=M),
                # subagent partial record: output_tokens is the stream-start placeholder
                asst("2026-10-01T10:05:00.000Z", "s1", "b", out=8, stop=None, isSidechain=True),
                # process 1: sonnet = our $2 + 100k under-logged output + $0.50 unexplained; haiku side calls
                cost_state("s1", ms("2026-10-01T09:59:00.000Z"), 0,
                           {"claude-sonnet-5": (2.0 + 8 * self.out_rate / M + self.partial_usd + 0.5, 0,
                                                {"outputTokens": 100_008}),
                            "claude-haiku-4-5-20251001": (0.6, 10)}, duration=H),
                # process 2 (a resume restarts the counter): haiku only
                cost_state("s1", ms("2026-10-01T12:00:00.000Z"), 0.25,
                           {"claude-haiku-4-5-20251001": (0.25, 0)}, duration=H),
            ],
            # s2: no cost-state -> not reconcilable
            ("personal", "p/s2.jsonl"): [asst("2026-10-01T11:00:00.000Z", "s2", "c", inp=M)],
            # s3: our transcript cost exceeds Claude Code's -> negative unexplained, no side call
            ("personal", "p/s3.jsonl"): [
                asst("2026-10-01T12:00:00.000Z", "s3", "d", inp=M),
                cost_state("s3", ms("2026-10-01T11:59:00.000Z"), 1.0, {"claude-sonnet-5": (1.0, 0)}, duration=H),
            ],
        })
        # process 1's totalCostUSD = its model costs
        with dashboard._conn() as c:
            c.execute("UPDATE session_costs SET total_cost_usd = (SELECT SUM(cost_usd) FROM session_cost_models m "
                      "WHERE m.session_id = session_costs.session_id AND m.start_time_ms = session_costs.start_time_ms)")
        self.f, self.t, _ = dashboard._resolve_window("all", None, None)

    def test_gap_decomposition(self):
        r = dashboard.q_reconciliation(self.f, self.t)
        tot = r["totals"]
        self.assertEqual((tot["total_sessions"], tot["reconciled_sessions"]), (3, 2))
        self.assertAlmostEqual(tot["coverage_pct"], 66.7)
        # haiku: 0.85 charged, of which 0.10 is search fees; nothing in transcripts
        self.assertAlmostEqual(tot["side_call_cost_usd"], 0.85)
        self.assertAlmostEqual(tot["side_call_web_search_usd"], 0.10)
        self.assertAlmostEqual(tot["side_call_token_cost_usd"], 0.75)
        self.assertEqual(tot["web_search_requests"], 10)
        self.assertAlmostEqual(tot["subagent_partial_output_usd"], round(self.partial_usd, 2))
        self.assertEqual((tot["subagent_partial_output_tokens"], tot["partial_usage_msgs"]), (100_000, 1))
        self.assertAlmostEqual(tot["unexplained_gap_usd"], 0.5 - 1.0)
        self.assertAlmostEqual(tot["out_of_window_cost_usd"], 1.0)  # the 09:00 message
        self.assertAlmostEqual(tot["cc_gap_usd"], round(
            tot["side_call_cost_usd"] + tot["subagent_partial_output_usd"] + tot["unexplained_gap_usd"], 2))
        self.assertTrue(tot["unexplained_gap_note"])
        s1 = next(x for x in r["rows"] if x["session_id"] == "s1")
        self.assertEqual(s1["processes"], 2)
        self.assertAlmostEqual(s1["our_cost_usd"], 2.0 + 8 * self.out_rate / M, places=4)  # in-window only
        s3 = next(x for x in r["rows"] if x["session_id"] == "s3")
        self.assertEqual(s3["side_call_cost_usd"], 0.0)
        self.assertAlmostEqual(s3["delta_usd"], -1.0)
        self.assertAlmostEqual(s3["unexplained_gap_usd"], -1.0)

    def test_summary_and_account_keys(self):
        s = dashboard.api_summary(req(window="all"))
        self.assertAlmostEqual(s["api_cost"], 7.0)  # side calls and gaps are never added to api_cost
        self.assertAlmostEqual(s["side_call_cost_usd"], 0.85)
        self.assertAlmostEqual(s["api_cost_incl_side_calls"], 7.85)
        self.assertAlmostEqual(s["unexplained_gap_usd"], -0.5)
        self.assertIn("subagent_partial_output_usd", s)
        self.assertEqual((s["reconciled_sessions"], s["total_sessions"]), (2, 3))
        acct = dashboard.q_by_account(self.f, self.t)[0]
        self.assertAlmostEqual(acct["side_call_cost_usd"], 0.85)
        self.assertAlmostEqual(acct["unexplained_gap_usd"], -0.5)
        self.assertEqual(acct["reconciled_sessions"], 2)

    def test_reconciliation_endpoint(self):
        r = dashboard.api_reconciliation(req(window="all"))
        self.assertEqual(r["rows"][0]["session_id"], "s1")  # largest absolute gap first
        self.assertIn("partial", r["note"])
        # signed token gaps: s1 sonnet input 1 - 1M, haiku 1 + 1; s3 sonnet 1 - 1M
        self.assertEqual(r["totals"]["gap_tokens"]["input"], (1 - M) + 2 + (1 - M))
        self.assertEqual(r["totals"]["gap_tokens"]["output"], 100_000 + 2 + 1)

    def test_partial_flag_survives_a_final_copy(self):
        # a resumed transcript re-logs the message with its final usage: the row becomes final
        write_jsonl(self.accounts[0]["root"] / "p" / "s1-copy.jsonl",
                    [asst("2026-10-01T10:05:00.000Z", "s1", "b", out=900, isSidechain=True)])
        index(self.db, self.accounts)
        with dashboard._conn() as c:
            self.assertEqual(tuple(c.execute("SELECT usage_final, output_tokens FROM messages WHERE msg_id='b'").fetchone()),
                             (1, 900))


class BreakdownSessionsTest(Base):
    def setUp(self):
        super().setUp()
        self.account("personal")
        self.build({("personal", "p/s1.jsonl"): [
            asst("2026-10-01T10:00:00.000Z", "s1", "a", model="claude-opus-5", inp=M, gitBranch="feat",
                 entrypoint="cli", version="2.1.1", effort="high"),
            asst("2026-10-01T10:01:00.000Z", "s1", "b", inp=M // 10, gitBranch="feat", entrypoint="cli",
                 version="2.1.1"),
            asst("2026-10-01T10:02:00.000Z", "s1", "c", inp=M // 10, gitBranch="main", entrypoint="cli",
                 version="2.1.1", isSidechain=True, attributionAgent="Explore",
                 attributionSkill="dataviz", attributionMcpServer="context7"),
            asst("2026-10-01T15:00:00.000Z", "s1", "d", inp=M // 10, gitBranch="feat", version="2.1.1"),
            {"type": "custom-title", "customTitle": "my-session", "sessionId": "s1"},
            {"type": "ai-title", "aiTitle": "AI title", "sessionId": "s1"},
            {"type": "pr-link", "sessionId": "s1", "prNumber": 1, "prUrl": "u1", "prRepository": "o/r",
             "timestamp": "2026-10-01T10:03:00Z"},
            {"type": "pr-link", "sessionId": "s1", "prNumber": 2, "prUrl": "u2", "prRepository": "o/r",
             "timestamp": "2026-10-01T10:04:00Z"},
            {"type": "system", "subtype": "turn_duration", "durationMs": 120000, "timestamp": "2026-10-01T10:02:30Z",
             "uuid": "t1", "sessionId": "s1", "isSidechain": False},
            {"type": "system", "subtype": "turn_duration", "durationMs": 60000, "timestamp": "2026-10-01T15:00:30Z",
             "uuid": "t2", "sessionId": "s1", "isSidechain": False},
            {"type": "system", "subtype": "turn_duration", "durationMs": 999999, "timestamp": "2026-10-01T10:02:10Z",
             "uuid": "t3", "sessionId": "s1", "isSidechain": True},
            {"type": "assistant", "timestamp": "2026-10-02T09:00:00Z", "uuid": "e1", "sessionId": "s1",
             "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
             "quotaLimits": {"status": "rejected", "rateLimitType": "seven_day"},
             "message": {"id": "z", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0}}},
            {"type": "system", "subtype": "api_error", "retryAttempt": 1, "maxRetries": 10,
             "timestamp": "2026-10-02T09:01:00Z", "uuid": "e2", "sessionId": "s1"},
        ]})
        self.f, self.t, _ = dashboard._resolve_window("all", None, None)

    def test_breakdown_dims(self):
        side = {r["value"]: r for r in dashboard.q_breakdown(self.f, self.t, "sidechain")["rows"]}
        self.assertEqual((side["main"]["msgs"], side["subagent"]["msgs"]), (3, 1))
        self.assertAlmostEqual(side["subagent"]["cost"], 0.2)
        agents = {r["value"] for r in dashboard.q_breakdown(self.f, self.t, "agent")["rows"]}
        self.assertEqual(agents, {"(main thread)", "Explore"})
        self.assertIn("dataviz", {r["value"] for r in dashboard.q_breakdown(self.f, self.t, "skill")["rows"]})
        self.assertIn("context7", {r["value"] for r in dashboard.q_breakdown(self.f, self.t, "mcp")["rows"]})
        br = {r["value"]: r["msgs"] for r in dashboard.q_breakdown(self.f, self.t, "branch")["rows"]}
        self.assertEqual(br, {"feat": 3, "main": 1})
        ent = {r["value"]: r["msgs"] for r in dashboard.q_breakdown(self.f, self.t, "entrypoint")["rows"]}
        self.assertEqual(ent, {"cli": 3, "(none)": 1})
        pr = dashboard.q_breakdown(self.f, self.t, "pr")
        self.assertTrue(pr["overlapping"])
        self.assertEqual({r["value"]: r["msgs"] for r in pr["rows"]}, {"o/r#1": 4, "o/r#2": 4})
        self.assertEqual({r["value"]: (r["repo"], r["pr_url"], r["sessions"]) for r in pr["rows"]},
                         {"o/r#1": ("o/r", "u1", 1), "o/r#2": ("o/r", "u2", 1)})
        ms = {r["value"] for r in dashboard.q_breakdown(self.f, self.t, "model_speed")["rows"]}
        self.assertIn("claude-opus-5 · standard", ms)
        bad = dashboard.api_breakdown(req(dim="nope"))
        self.assertEqual(bad.status_code, 400)

    def test_breakdown_marks_rows_whose_transcript_is_gone(self):
        with dashboard._conn() as c:
            c.execute("UPDATE messages SET cc_version=NULL WHERE msg_id='d'")
        side = {r["value"]: r["msgs"] for r in dashboard.q_breakdown(self.f, self.t, "sidechain")["rows"]}
        self.assertEqual(side, {"main": 2, "subagent": 1, dashboard.GONE: 1})

    def test_sessions(self):
        s = dashboard.api_sessions(req(window="all"))["rows"][0]
        self.assertEqual(s["title"], "my-session")      # custom beats ai
        self.assertEqual(s["top_model"], "claude-opus-5")  # by cost, not alphabetical
        self.assertEqual(s["model_mix"][0]["model"], "claude-opus-5")
        self.assertEqual(s["active_minutes"], 3.0)       # main-thread turns only, idle gap excluded
        self.assertEqual(s["active_source"], "turn_duration")
        self.assertEqual(s["duration_minutes"], 300)
        self.assertEqual([p["number"] for p in s["prs"]], [1, 2])
        self.assertEqual(s["branch"], "feat")
        self.assertFalse(s["reconciled"])

    def test_top_sessions_uses_most_costly_model(self):
        r = dashboard.q_top_sessions(self.f, self.t, "cost", 5)[0]
        self.assertEqual(r["model"], "claude-opus-5")  # MAX(model) would say claude-sonnet-5
        self.assertEqual(r["active_minutes"], 3.0)

    def test_reliability(self):
        r = dashboard.api_reliability(req(window="all"))
        self.assertEqual(r["rows"], [{"day": "2026-10-02", "rate_limit": 1, "server_error": 0, "retry": 1,
                                      "quota": 1, "other_error": 0}])
        self.assertEqual(r["totals"]["quota"], 1)
        r = dashboard.api_reliability(req(window="all", account="nobody"))
        self.assertEqual(r["rows"], [])


class AccountsTest(Base):
    def setUp(self):
        super().setUp()
        self.account("personal", {"env": {}}, priority=20)
        self.account("claudex", {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                                         "ANTHROPIC_AUTH_TOKEN": "sk-very-secret-value"}}, priority=30)
        luna = [asst(f"2026-10-01T10:{i:02d}:00.000Z", "g1", f"g{i}", model="gpt-5.6-luna", inp=M // 10)
                for i in range(25)]
        luna.append(asst("2026-10-01T11:00:00.000Z", "g1", "gz", model="gpt-5.6-sol",
                         usage={"input_tokens": 0, "output_tokens": 0}))
        luna.append(asst("2026-10-01T11:01:00.000Z", "g1", "gx", model="mystery-model-x", inp=M))
        self.build({
            ("personal", "p/s1.jsonl"): [asst("2026-10-01T10:00:00.000Z", "s1", "a", inp=M, cr=M)],
            ("claudex", "p/g1.jsonl"): luna,
        })
        billing.save_charges([{"receipt_id": "r1", "start": "2026-08-01", "end": "2026-09-01", "plan": "Max 20x",
                               "amount_usd": 200.0, "account": "personal"}])

    def test_accounts_billing_paths_and_flags(self):
        out = dashboard.api_accounts(req())
        self.assertNotIn("sk-very-secret-value", json.dumps(out))
        acc = {a["name"]: a for a in out["accounts"]}
        self.assertEqual((acc["personal"]["billing_mode"], acc["personal"]["payment"]), ("subscription", "subscription"))
        cx = acc["claudex"]
        self.assertEqual((cx["billing_mode"], cx["provider"], cx["payment"]), ("gateway", "cliproxyapi", "subscription"))
        self.assertEqual(cx["gateway_host"], "127.0.0.1:8317")
        self.assertIs(cx["cache_tokens_reported"], False)  # 25 rows with input, none with cache tokens
        self.assertTrue(acc["personal"]["cache_tokens_reported"] is None)  # too few rows to judge
        # v8: the usage-less message is estimated (flagged), no longer an unestimated gap
        self.assertEqual((cx["usage_unreported_msgs"], cx["usage_estimated_msgs"], cx["unpriced_msgs"]), (0, 1, 1))

    def test_actual_cost_by_billing_path(self):
        f, t, _ = dashboard._resolve_window("all", None, None)
        rows = {r["account"]: r for r in dashboard.q_by_account(f, t)}
        self.assertIsNone(rows["claudex"]["actual_cost_usd"])  # subscription gateway without a receipt
        self.assertAlmostEqual(rows["personal"]["actual_cost_usd"], 200.0)
        self.assertAlmostEqual(rows["claudex"]["api_cost"], round(25 * 0.02, 2))  # luna list price ($0.20/M input)
        # a ChatGPT receipt for the gateway account makes it known
        billing.save_charges(billing.load_charges() + [{"receipt_id": "c1", "start": "2026-08-01",
                                                        "end": "2026-09-01", "plan": "ChatGPT Pro",
                                                        "amount_usd": 200.0, "account": "claudex"}])
        s = dashboard.q_summary(f, t)
        self.assertEqual(s["actual_cost_unknown_accounts"], [])
        self.assertAlmostEqual(s["actual_cost_usd"], 400.0)
        # a markup override turns it into a pay-per-token router
        (self.d / "config.json").write_text(json.dumps({"accounts": {"claudex": {"payment": "per_token",
                                                                                  "markup_pct": 10}}}))
        rows = {r["account"]: r for r in dashboard.q_by_account(f, t)}
        self.assertAlmostEqual(rows["claudex"]["actual_cost_usd"], round(25 * 0.02 * 1.1, 2))


if __name__ == "__main__":
    unittest.main()


class TokenTypeSplitTest(Base):
    """Cost split by token type: each part is tokens x rate, the parts add back up to the total."""

    def setUp(self):
        super().setUp()
        self.account("personal")
        ws = {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
              "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 0}, "speed": "standard"}
        self.build({("personal", "p/s1.jsonl"): [
            asst("2026-10-01T10:00:00.000Z", "s1", "a", inp=M, out=M // 10, cr=M, c5=M // 2),
            asst("2026-10-01T11:00:00.000Z", "s1", "b", model="claude-opus-5", inp=M // 2, cr=2 * M),
            asst("2026-10-02T09:00:00.000Z", "s2", "c", usage=ws),
        ]})
        self.f, self.t, _ = dashboard._resolve_window("all", None, None)

    def expected(self):
        son, opus = pricing.resolve("claude-sonnet-5")[0], pricing.resolve("claude-opus-5")[0]
        return {"input": (M * son[0] + M // 2 * opus[0] + 10 * son[0]) / M,
                "output": (M // 10 * son[1] + 10 * son[1]) / M,
                "cache_5m_write": M // 2 * son[2] / M,
                "cache_read": (M * son[4] + 2 * M * opus[4]) / M,
                "web_search": 3 * pricing.WEB_SEARCH_USD}

    def test_breakdown_splits_cost_by_rate(self):
        b = dashboard.q_breakdown(self.f, self.t, "token_type")
        got = {r["value"]: r for r in b["rows"]}
        exp = self.expected()
        self.assertEqual(set(got), set(exp))  # no 1-hour cache writes, so no row for them
        for k, v in exp.items():
            self.assertAlmostEqual(got[k]["cost"], v, places=6, msg=k)
        self.assertAlmostEqual(sum(r["cost"] for r in b["rows"]), b["total_cost"], places=6)
        self.assertEqual([r["value"] for r in b["rows"]], sorted(got, key=lambda k: -got[k]["cost"]))
        self.assertEqual((got["input"]["msgs"], got["input"]["sessions"]), (3, 2))
        self.assertEqual((got["web_search"]["tokens"], got["web_search"]["web_search_requests"]), (0, 3))
        self.assertTrue(b["overlapping"])
        self.assertEqual(dashboard.api_breakdown(req(window="all", dim="token_type"))["rows"], b["rows"])

    def test_timeseries_by_token_type_adds_up_per_day(self):
        rows = dashboard.q_timeseries(self.f, self.t, "day", by="token_type")
        days = {r["bucket"]: r["cost"] for r in dashboard.q_timeseries(self.f, self.t, "day")}
        per_day: dict = {}
        for r in rows:
            per_day[r["bucket"]] = per_day.get(r["bucket"], 0) + r["cost"]
        self.assertEqual(set(per_day), set(days))
        for d, v in days.items():
            self.assertAlmostEqual(per_day[d], v, places=5)
        self.assertIn("web_search", {r["token_type"] for r in rows if r["bucket"] == "2026-10-02"})
        r = dashboard.api_timeseries(req(window="all", by="token_type"))
        self.assertEqual(r["by"], "token_type")
