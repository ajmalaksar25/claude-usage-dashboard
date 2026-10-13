"""v8: request outcomes (failed/aborted requests are not messages), internal models and model
aliases, usage ESTIMATES for messages a gateway logged without usage, CLIProxyAPI gateway hint.

Everything runs in a temp dir (transcripts, DB, config.json). No network, no real proxy config."""
import json
import sqlite3
import unittest
from unittest import mock

from helpers import M, asst, index, write_jsonl
from test_dashboard_v5 import req
from test_v7 import PARENT, V7Base, meta, pi_msg, tc, turn

import dashboard
import indexer
import pricing


def pi_failed(ts, rid, stop, err, usage=None):
    m = pi_msg(ts, rid, inp=0, out=0)
    m["message"].update(stopReason=stop, errorMessage=err)
    if usage:
        m["message"]["usage"].update(usage)
    return m


class PiOutcomeTest(V7Base):
    def setUp(self):
        super().setUp()
        self.tool_account("pi", "pi")
        sess = {"type": "session", "version": 3, "id": "pi-s1", "timestamp": "2026-07-03T10:00:00.000Z",
                "cwd": "/work/acme"}
        self.recs = [sess,
                     pi_msg("2026-07-03T10:00:01.000Z", "ok1"),
                     pi_failed("2026-07-03T10:00:02.000Z", "ab1", "aborted", "Operation aborted"),
                     pi_failed("2026-07-03T10:00:03.000Z", "er1", "error", "No API key for provider: openai-codex"),
                     # aborted after usage was reported: billed, so it stays a message
                     pi_failed("2026-07-03T10:00:04.000Z", "ab2", "aborted", "Operation aborted",
                               usage={"input": 500, "output": 20})]
        self.build({("pi", "--work-acme--/a.jsonl"): self.recs})

    def test_failed_requests_are_not_messages(self):
        rows = self.rows()
        self.assertEqual([(r["msg_id"], r["outcome"], r["stop_reason"]) for r in rows],
                         [("pi:ok1", "ok", "stop"), ("pi:ab2", "aborted", "aborted")])
        c = sqlite3.connect(self.db)
        failed = c.execute("SELECT msg_id, outcome, error FROM failed_requests ORDER BY ts").fetchall()
        c.close()
        self.assertEqual(failed, [("pi:ab1", "aborted", "Operation aborted"),
                                  ("pi:er1", "error", "No API key for provider: openai-codex")])
        a = next(x for x in dashboard.api_accounts(req(window="all"))["accounts"] if x["name"] == "pi")
        self.assertEqual((a["failed_msgs"], a["usage_unreported_msgs"], a["window_msgs"]), (2, 0, 2))
        self.assertEqual(dashboard.api_summary(req(window="all"))["msgs"], 2)
        # a model filter that excludes them hides them too
        a = next(x for x in dashboard.api_accounts(req(window="all", model="gpt-9"))["accounts"] if x["name"] == "pi")
        self.assertEqual(a["failed_msgs"], 0)

    def test_v7_usageless_row_is_moved_out_on_reindex(self):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO messages (msg_id, ts, ts_day, ts_hour, ts_dow, account, tool, usage_reported) "
                  "VALUES ('pi:ab1', '2026-07-03T10:00:02.000Z', '2026-07-03', 10, 4, 'pi', 'pi', 0)")
        c.execute("DELETE FROM files")
        c.commit()
        c.close()
        index(self.db, self.accounts)
        self.assertNotIn("pi:ab1", [r["msg_id"] for r in self.rows()])


class InternalModelTest(V7Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(pricing.set_model_aliases, None)
        self.tool_account("codex-cli", "codex-cli")
        review = [meta("2026-10-02T10:00:00.000Z", "rev"), turn("2026-10-02T10:00:01.000Z", model="codex-auto-review"),
                  tc("2026-10-02T10:00:05.000Z", 1000, 0, 100, 0)]
        mystery = [meta("2026-10-03T10:00:00.000Z", "mys"), turn("2026-10-03T10:00:01.000Z", model="gpt-9-mystery"),
                   tc("2026-10-03T10:00:05.000Z", 1000, 0, 100, 0)]
        self.build({("codex-cli", "2026/10/01/rollout-parent.jsonl"): PARENT,
                    ("codex-cli", "2026/10/02/rollout-rev.jsonl"): review,
                    ("codex-cli", "2026/10/03/rollout-mys.jsonl"): mystery})

    def flags(self):
        return next(x for x in dashboard.api_accounts(req(window="all"))["accounts"] if x["name"] == "codex-cli")

    def test_internal_is_not_unpriced(self):
        f = self.flags()
        self.assertEqual((f["internal_msgs"], f["internal_models"]), (1, ["codex-auto-review"]))
        self.assertEqual((f["unpriced_msgs"], f["unpriced_models"]), (1, ["gpt-9-mystery"]))
        s = dashboard.api_summary(req(window="all"))
        self.assertEqual((s["internal_msgs"], s["unpriced_msgs"]), (1, 1))
        row = dashboard.api_by_account(req(window="all"))["rows"][0]
        self.assertEqual((row["internal_msgs"], row["unpriced_msgs"]), (1, 1))
        self.assertEqual(pricing.price_source("codex-auto-review"), "internal")

    def test_alias_opt_in_and_back(self):
        (self.d / "config.json").write_text(json.dumps({"model_aliases": {"codex-auto-review": "gpt-5.6-luna"}}))
        c = sqlite3.connect(self.db)
        self.assertTrue(pricing._estimated_now_exact(c))
        pricing.reprice(c)
        r = c.execute("SELECT tier, price_estimated, cost_usd FROM messages WHERE model='codex-auto-review'").fetchone()
        luna = pricing.EXT_SEED["gpt-5.6-luna"]
        self.assertEqual(r[:2], ("gpt-5.6-luna", 1))
        self.assertAlmostEqual(r[2], (1000 * luna[0] + 100 * luna[1]) / 1e6)
        self.assertEqual(pricing.price_source("codex-auto-review"), "alias")
        dashboard.invalidate_cache()
        f = self.flags()
        self.assertEqual((f["internal_msgs"], f["unpriced_msgs"], f["estimated_msgs"]), (0, 1, 1))
        # alias removed: the stale price is detected and the rows go back to $0 internal
        (self.d / "config.json").write_text(json.dumps({}))
        self.assertTrue(pricing._estimated_now_exact(c))
        pricing.reprice(c)
        r = c.execute("SELECT tier, price_estimated, cost_usd FROM messages WHERE model='codex-auto-review'").fetchone()
        c.close()
        self.assertEqual(r, (None, 0, 0))
        dashboard.invalidate_cache()
        self.assertEqual(self.flags()["internal_msgs"], 1)


def rec(ts, sid, mid, chars, inp=0, out=0, cr=0, uuid=None, **kw):
    r = asst(ts, sid, mid, inp=inp, out=out, cr=cr, stop="tool_use", **kw)
    r["message"]["content"] = [{"type": "tool_use", "id": "t", "name": "Bash", "input": "x" * chars}]
    if uuid:
        r["uuid"] = uuid
    return r


class EstimateTest(V7Base):
    SON = pricing.PRICING["claude-sonnet-5"]

    def setUp(self):
        super().setUp()
        self.account("claudex", {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"}})
        # 30 calibration messages: output = 2 x chars/4 (ratio 2.0)
        cal = [rec(f"2026-10-01T09:{i:02d}:00.000Z", "s0", f"c{i}", 400, inp=1000, cr=0, out=200)
               for i in range(30)]
        thread = [rec("2026-10-01T10:00:00.000Z", "s1", "a", 400, inp=1000, cr=9000, out=200),
                  # a response logged without usage, as two content-block records
                  rec("2026-10-01T10:00:05.000Z", "s1", "b", 500, uuid="b1"),
                  rec("2026-10-01T10:00:10.000Z", "s1", "b", 300, uuid="b2"),
                  rec("2026-10-01T10:00:20.000Z", "s1", "c", 400, inp=2000, cr=18000, out=200)]
        self.build({("claudex", "p/s0.jsonl"): cal, ("claudex", "p/s1.jsonl"): thread})

    def test_estimate_is_flagged_and_kept_out_of_measured_totals(self):
        b = self.rows("msg_id='b'")[0]
        self.assertEqual((b["usage_reported"], b["usage_estimated"], b["content_chars"]), (0, 1, 800))
        self.assertEqual((b["input_tokens"], b["output_tokens"], b["cost_usd"]), (0, 0, 0))
        # b (stamped by its last record, t=10s) is midway between a (t=0, prompt 10K) and c (t=20s,
        # prompt 20K): 15K, split by the neighbours' 90% cache-read share
        self.assertEqual((b["est_input_tokens"], b["est_cache_read"]), (1500, 13500))
        self.assertEqual(b["est_output_tokens"], 400)  # 800 chars / 4 x calibrated ratio 2.0
        self.assertEqual(b["est_method"], "thread")
        want = (1500 * self.SON[0] + 400 * self.SON[1] + 13500 * self.SON[4]) / 1e6
        self.assertAlmostEqual(b["est_cost_usd"], want)

        measured = sum(r["cost_usd"] for r in self.rows())
        s = dashboard.api_summary(req(window="all"))
        self.assertAlmostEqual(s["api_cost"], round(measured, 2))
        self.assertEqual((s["usage_estimated_msgs"], s["estimated_cost_usd"]), (1, round(want, 2)))
        self.assertAlmostEqual(s["api_cost_incl_estimates"], round(round(measured, 2) + round(want, 2), 2))
        a = dashboard.api_accounts(req(window="all"))["accounts"][0]
        self.assertEqual((a["usage_unreported_msgs"], a["usage_estimated_msgs"]), (0, 1))
        est = a["usage_estimate"]
        self.assertEqual(est["calibration"]["claude-sonnet-5"]["ratio"], 2.0)
        self.assertEqual(est["msgs"], 1)
        self.assertIn("median_abs_pct_err", est["validation"])
        self.assertGreater(est["validation"]["n"], 0)

    def test_reprice_follows_the_estimate(self):
        c = sqlite3.connect(self.db)
        c.execute("UPDATE messages SET est_cost_usd=99 WHERE msg_id='b'")
        pricing.reprice(c)
        v = c.execute("SELECT est_cost_usd FROM messages WHERE msg_id='b'").fetchone()[0]
        c.close()
        self.assertLess(v, 1)

    def test_gateway_hint_for_cliproxyapi(self):
        facts = {"binary": "/x/cliproxyapi", "config_path": "/x/config.yaml", "port": "8317",
                 "usage_statistics_enabled": False}
        with mock.patch.object(dashboard, "_cliproxy_install", lambda: facts):
            a = dashboard.api_accounts(req(window="all"))["accounts"][0]
        self.assertIn("usage-statistics-enabled: true in /x/config.yaml", a["gateway_hint"])
        dashboard.invalidate_cache()
        with mock.patch.object(dashboard, "_cliproxy_install", lambda: {**facts, "usage_statistics_enabled": True}):
            a = dashboard.api_accounts(req(window="all"))["accounts"][0]
        self.assertIsNone(a["gateway_hint"])
        self.assertTrue(a["gateway_proxy"]["usage_statistics_enabled"])

    def test_proxy_config_read_without_secrets(self):
        conf = self.d / "config.yaml"
        conf.write_text("port: 8317\napi-keys:\n  - sk-SECRET-123\nusage-statistics-enabled: false # off\n")
        with mock.patch.object(dashboard, "_CLIPROXY_CONFIGS", (str(conf),)), \
                mock.patch("shutil.which", return_value=None):
            got = dashboard._cliproxy_install()
        self.assertEqual((got["port"], got["usage_statistics_enabled"]), ("8317", False))
        self.assertNotIn("SECRET", json.dumps(got))


class EstimateCoreTest(unittest.TestCase):
    def row(self, mid, t, inp=0, cr=0, out=0, chars=0, sid="s", reported=1):
        return {"msg_id": mid, "account": "a", "session_id": sid, "is_sidechain": 0, "agent_id": "", "ts": "",
                "_t": t, "model": "m", "input_tokens": inp, "output_tokens": out, "cache_read": cr,
                "cache_5m_write": 0, "cache_1h_write": 0, "content_chars": chars, "usage_reported": reported,
                "usage_final": 1, "outcome": "ok"}

    def test_nearest_side_and_fallbacks(self):
        rows = [self.row("k1", 0, inp=100, cr=900), self.row("u1", 5, reported=0, chars=40),
                self.row("u2", 9, reported=0, chars=0, sid="other")]
        est = indexer._estimate(rows, {"u1", "u2"}, {})
        self.assertEqual(est["u1"], (100, 900, 0, 10, "thread"))  # only a previous neighbour; ratio 1.0
        self.assertEqual(est["u2"][4], "model_median")


if __name__ == "__main__":
    unittest.main()


class PlanAllocationTest(V7Base):
    """Slicing filters (weekday, hour, tool, model, project, dimensions) allocate each account's plan
    cost by its usage share; window and account filters keep the whole prorated plan."""

    def setUp(self):
        super().setUp()
        import billing
        self.account("personal")
        self.account("work")
        self.config({"accounts": {"work": {"payer": "employer"}}})
        # 2026-10-07 is a Wednesday (dow=2, 0=Monday). sonnet-5 input: $2/M
        self.build({
            ("personal", "p/s1.jsonl"): [asst("2026-10-07T10:00:00.000Z", "s1", "p-wed", inp=10 * M),   # $20
                                         asst("2026-10-08T10:00:00.000Z", "s1", "p-thu", inp=30 * M)],  # $60
            ("work", "p/s2.jsonl"): [asst("2026-10-07T10:00:00.000Z", "s2", "w-wed", inp=50 * M),      # $100
                                     asst("2026-10-09T10:00:00.000Z", "s2", "w-fri", inp=50 * M)],     # $100
        })
        billing.save_charges([
            {"receipt_id": "p1", "start": "2026-10-01", "end": "2026-10-31", "plan": "Pro",
             "amount_usd": 20.0, "account": "personal", "provider": "anthropic", "source": "manual"},
            {"receipt_id": "w1", "start": "2026-10-01", "end": "2026-10-31", "plan": "Team Premium",
             "amount_usd": 125.0, "account": "work", "provider": "anthropic", "source": "manual"},
        ])
        self.q = {"from": "2026-10-01", "to": "2026-10-31"}

    def test_wednesdays_get_their_usage_share_of_the_plan(self):
        whole = dashboard.api_summary(req(**self.q))
        self.assertEqual(whole["plan_cost_basis"], "window")
        self.assertAlmostEqual(whole["subscription_cost"], 145.0)
        wed = dashboard.api_summary(req(**self.q, dow=2))
        self.assertEqual(wed["plan_cost_basis"], "allocated_by_usage")
        # personal: Wednesday is 20/80 of its usage -> 25% of $20; work: 100/200 -> 50% of $125
        self.assertAlmostEqual(wed["paid_by_you_usd"], 5.0)
        self.assertAlmostEqual(wed["paid_by_employer_usd"], 62.5)
        self.assertAlmostEqual(wed["subscription_cost"], 67.5)
        self.assertAlmostEqual(wed["actual_cost_usd"], 67.5)
        self.assertAlmostEqual(wed["savings_you"], 20.0 - 5.0)
        self.assertGreater(wed["savings"], 0)  # never a whole month's plan against one weekday
        rows = {r["account"]: r for r in dashboard.api_by_account(req(**self.q, dow=2))["rows"]}
        allr = {r["account"]: r for r in dashboard.api_by_account(req(**self.q))["rows"]}
        for a, plan in (("personal", 5.0), ("work", 62.5)):
            r = rows[a]
            self.assertEqual(r["plan_cost_basis"], "allocated_by_usage")
            self.assertAlmostEqual((r["subscription_cost"], r["plan_value_usd"]), (plan, plan))
            self.assertEqual(r["multiplier"], allr[a]["multiplier"])  # same rate as the whole window
            self.assertEqual(r["savings"] > 0, allr[a]["savings"] > 0)
        self.assertAlmostEqual(rows["personal"]["you_paid_usd"], 5.0)
        self.assertEqual(rows["work"]["you_paid_usd"], 0.0)
        acc = {a["name"]: a for a in dashboard.api_accounts(req(**self.q, dow=2))["accounts"]}
        self.assertEqual((acc["personal"]["plan_value_usd"], acc["personal"]["you_paid_usd"],
                          acc["personal"]["plan_cost_basis"]), (5.0, 5.0, "allocated_by_usage"))

    def test_window_and_account_are_not_slicing(self):
        p = dashboard.api_summary(req(**self.q, account="personal"))
        self.assertEqual((p["plan_cost_basis"], p["subscription_cost"]), ("window", 20.0))
        p = dashboard.api_summary(req(**{"from": "2026-10-07", "to": "2026-10-07"}))
        self.assertEqual(p["plan_cost_basis"], "window")
        # project and tool are slicing; a slice with all of an account's usage keeps its whole plan
        t = dashboard.api_summary(req(**self.q, tool="claude-code"))
        self.assertEqual(t["plan_cost_basis"], "allocated_by_usage")
        self.assertAlmostEqual(t["subscription_cost"], 145.0)
        n = dashboard.api_summary(req(**self.q, project="nope"))
        self.assertEqual((n["plan_cost_basis"], n["subscription_cost"]), ("allocated_by_usage", 0.0))
