"""v6: usage_final flag, read indexes, native local-time buckets, timeseries by=model, response cache."""
import os
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest import mock

from helpers import M, asst, write_jsonl
from test_dashboard_v5 import Base, req

import dashboard

NY = "America/New_York"


class IndexerV6Test(Base):
    def test_usage_final_and_read_indexes(self):
        self.account("personal")
        self.build({("personal", "p/s1.jsonl"): [
            asst("2026-10-01T10:00:00.000Z", "s1", "a", inp=M),
            asst("2026-10-01T10:01:00.000Z", "s1", "b", out=8, stop=None, isSidechain=True),
        ]})
        conn = sqlite3.connect(self.db)
        flags = dict(conn.execute("SELECT msg_id, usage_final FROM messages"))
        idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        plan = " ".join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT SUM(cost_usd), COUNT(DISTINCT session_id) FROM messages WHERE ts >= ? AND ts < ?",
            ("2026", "2027")))
        conn.close()
        self.assertEqual(flags, {"a": 1, "b": 0})
        self.assertTrue({"idx_messages_cover", "idx_messages_session_ts", "idx_messages_account_ts"} <= idx)
        self.assertFalse({"idx_messages_ts", "idx_messages_session", "idx_messages_account"} & idx)
        self.assertIn("COVERING INDEX idx_messages_cover", plan)


class LocalBucketsTest(Base):
    """The native strftime buckets must equal the Python zone conversion, across DST changes."""

    TS = ["2025-03-09T06:59:00.000Z", "2025-03-09T07:00:00.000Z",   # 01:59 EST -> 03:00 EDT
          "2025-11-02T05:30:00.000Z", "2025-11-02T06:30:00.000Z",   # 01:30 EDT, then 01:30 EST
          "2025-07-04T03:59:00.000Z", "2025-12-31T23:59:59.000Z"]

    def setUp(self):
        super().setUp()
        self.account("personal")
        self.build({("personal", "p/s1.jsonl"): [asst(t, "s1", f"m{i}", inp=M) for i, t in enumerate(self.TS)]})

    def expected(self, tz):
        z = dashboard._zone(tz)
        loc = [datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(z) for t in self.TS]
        return loc

    def check(self, tz, window="all"):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_TZ": tz}):
            f, t, _ = dashboard._resolve_window(window, None, None)
            loc = self.expected(tz)
            hours = sorted(r["bucket"] for r in dashboard.q_timeseries(f, t, "hour"))
            self.assertEqual(hours, sorted({d.strftime("%Y-%m-%dT%H") for d in loc}))
            days = {r["day"]: r["msgs"] for r in dashboard.q_heatmap(f, t)}
            want: dict[str, int] = {}
            for d in loc:
                want[d.strftime("%Y-%m-%d")] = want.get(d.strftime("%Y-%m-%d"), 0) + 1
            self.assertEqual(days, want)
            dist = dashboard.q_distributions(f, t)
            self.assertEqual(sorted(r["dow"] for r in dist["dows"]), sorted({d.weekday() for d in loc}))
            self.assertEqual(sorted(r["hour"] for r in dist["hours"]), sorted({d.hour for d in loc}))

    def test_dst_zone(self):
        self.check(NY)

    def test_fixed_and_half_hour_zones(self):
        self.check("UTC")
        self.check("Asia/Kolkata")

    def test_explicit_range_inside_one_offset(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_TZ": NY}):
            f, t, _ = dashboard._resolve_window(None, "2025-07-01", "2025-07-31")
            self.assertEqual([r["day"] for r in dashboard.q_heatmap(f, t)], ["2025-07-03"])  # 23:59 EDT


class TimeseriesByModelTest(Base):
    def setUp(self):
        super().setUp()
        self.account("personal")
        self.build({("personal", "p/s1.jsonl"): [
            asst("2026-10-01T10:00:00.000Z", "s1", "a", model="claude-opus-5", inp=M, out=10, cr=5, c5=7),
            asst("2026-10-01T11:00:00.000Z", "s1", "b", inp=M),
            asst("2026-10-02T10:00:00.000Z", "s1", "c", inp=M),
        ]})

    def test_rows_per_day_and_model(self):
        r = dashboard.api_timeseries(req(window="all", bucket="day", by="model"))
        self.assertEqual(r["by"], "model")
        got = [(x["bucket"], x["model"], x["msgs"]) for x in r["rows"]]
        self.assertEqual(got, [("2026-10-01", "claude-opus-5", 1), ("2026-10-01", "claude-sonnet-5", 1),
                               ("2026-10-02", "claude-sonnet-5", 1)])
        need = {"bucket", "model", "cost", "fast_cost_usd", "fast_premium_usd", "tokens", "input_tokens",
                "output_tokens", "cache_read", "cache_write", "msgs", "estimated_msgs", "unpriced_msgs"}
        self.assertTrue(need <= set(r["rows"][0]))
        opus = r["rows"][0]
        self.assertEqual((opus["tokens"], opus["cache_read"], opus["cache_write"]), (M + 10 + 5 + 7, 5, 7))
        # the split adds up to the unsplit series
        plain = dashboard.api_timeseries(req(window="all", bucket="day"))
        day1 = next(x for x in plain["rows"] if x["bucket"] == "2026-10-01")
        self.assertAlmostEqual(day1["cost"], sum(x["cost"] for x in r["rows"] if x["bucket"] == "2026-10-01"))

    def test_without_by_is_unchanged_and_bad_by_is_400(self):
        plain = dashboard.api_timeseries(req(window="all", bucket="day"))
        self.assertEqual(set(plain), {"window", "bucket", "rows"})
        self.assertNotIn("model", plain["rows"][0])
        self.assertEqual(dashboard.api_timeseries(req(window="all", by="nope")).status_code, 400)


class AccountsWindowTest(Base):
    def setUp(self):
        super().setUp()
        self.account("personal")
        self.account("claudex")
        self.build({
            ("personal", "p/s1.jsonl"): [asst("2026-09-01T10:00:00.000Z", "s1", "a", inp=M),
                                         asst("2026-10-01T10:00:00.000Z", "s1", "b", inp=M)],
            ("claudex", "p/g1.jsonl"): [
                asst("2026-09-01T10:00:00.000Z", "g1", "g1", model="gpt-5.6-luna", inp=M),
                asst("2026-09-01T10:01:00.000Z", "g1", "g2", model="mystery-model-x", inp=M),   # unpriced
                asst("2026-10-01T10:00:00.000Z", "g1", "g3", model="gpt-5.6-sol",
                     usage={"input_tokens": 0, "output_tokens": 0}),                          # usage unreported
            ],
        })

    def test_flags_follow_the_window_and_keep_all_time(self):
        acc = {a["name"]: a for a in dashboard.api_accounts(req(**{"from": "2026-10-01", "to": "2026-10-01"}))["accounts"]}
        cx = acc["claudex"]
        # v8: the usage-less message is estimated (usage_estimated_msgs), not an unestimated gap
        self.assertEqual((cx["unpriced_msgs"], cx["usage_estimated_msgs"], cx["usage_unreported_msgs"],
                          cx["window_msgs"]), (0, 1, 0, 1))
        self.assertEqual((cx["all_time"]["unpriced_msgs"], cx["all_time"]["usage_estimated_msgs"]), (1, 1))
        self.assertEqual(cx["msgs"], 3)  # the account list itself stays all-time
        acc = {a["name"]: a for a in dashboard.api_accounts(req(project="nope"))["accounts"]}
        self.assertEqual((acc["claudex"]["unpriced_msgs"], acc["claudex"]["window_msgs"]), (0, 0))

    def test_non_claude_cost(self):
        f, t, _ = dashboard._resolve_window("all", None, None)
        s = dashboard.q_summary(f, t)
        with dashboard._conn() as c:
            want = c.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM messages "
                             "WHERE model NOT LIKE 'claude%'").fetchone()
        self.assertAlmostEqual(s["non_claude_cost_usd"], round(want[0], 2))
        self.assertGreater(s["non_claude_cost_usd"], 0)
        self.assertEqual(s["non_claude_msgs"], 3)
        self.assertEqual(dashboard.q_summary(f, t, acct="personal")["non_claude_cost_usd"], 0.0)


class ResponseCacheTest(Base):
    def setUp(self):
        super().setUp()
        self.account("personal")
        self.build({("personal", "p/s1.jsonl"): [asst("2026-10-01T10:00:00.000Z", "s1", "a", inp=M)]})

    def test_cached_until_the_index_changes(self):
        first = dashboard.api_summary(req(window="all"))
        self.assertIs(dashboard.api_summary(req(window="all")), first)       # served from cache
        self.assertIsNot(dashboard.api_summary(req(window="all", account="x")), first)  # filters are in the key
        write_jsonl(self.accounts[0]["root"] / "p" / "s2.jsonl", [asst("2026-10-02T10:00:00.000Z", "s2", "b", inp=M)])
        from helpers import index
        index(self.db, self.accounts)                                       # a commit changes the data version
        self.assertEqual(dashboard.api_summary(req(window="all"))["msgs"], 2)

    def test_invalidate_cache(self):
        first = dashboard.api_summary(req(window="all"))
        dashboard.invalidate_cache()
        again = dashboard.api_summary(req(window="all"))
        self.assertIsNot(again, first)
        self.assertEqual(again["msgs"], first["msgs"])

    def test_error_responses_are_not_cached(self):
        a = dashboard.api_breakdown(req(dim="nope"))
        self.assertIsNot(dashboard.api_breakdown(req(dim="nope")), a)


if __name__ == "__main__":
    unittest.main()
