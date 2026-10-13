"""hour= / dow= page filters (heatmap cross-filtering): local hour 0-23 and weekday 0=Monday in the
configured zone, repeatable, accepted by every endpoint's shared filter builder."""
import os
import unittest
from unittest import mock

from helpers import M, asst
from test_dashboard_v5 import req
from test_v7 import V7Base

from starlette.requests import Request

import dashboard


class HourDowFilterTest(V7Base):
    def setUp(self):
        super().setUp()
        self.account("personal")
        self.build({("personal", "p/s1.jsonl"): [
            # Thu 2026-10-01: 10:05Z and 10:30Z (15:35 / 16:00 IST), 13:00Z (18:30 IST)
            asst("2026-10-01T10:05:00.000Z", "s1", "a", model="claude-sonnet-5", inp=M),
            asst("2026-10-01T10:30:00.000Z", "s1", "b", model="claude-opus-4-5", inp=M),
            asst("2026-10-01T13:00:00.000Z", "s1", "c", model="claude-opus-4-5", out=M),
            {"type": "system", "subtype": "api_error", "uuid": "e1", "timestamp": "2026-10-01T13:00:01.000Z",
             "sessionId": "s1", "retryAttempt": 1, "maxRetries": 3},
        ], ("personal", "p/s2.jsonl"): [
            # Fri 2026-10-02 09:00Z (14:30 IST)
            asst("2026-10-02T09:00:00.000Z", "s2", "d", model="claude-sonnet-5", inp=M),
        ]})

    def msgs(self, **q):
        return dashboard.api_summary(req(window="all", **q))["msgs"]

    def test_hour_filter_utc(self):
        self.assertEqual(self.msgs(hour="10"), 2)
        self.assertEqual(self.msgs(hour="13"), 1)
        self.assertEqual(self.msgs(hour="3"), 0)
        self.assertEqual(self.msgs(), 4)

    def test_dow_filter_monday_is_zero(self):
        self.assertEqual(self.msgs(dow="3"), 3)  # Thursday
        self.assertEqual(self.msgs(dow="4"), 1)  # Friday
        self.assertEqual(self.msgs(dow="0"), 0)
        self.assertEqual(self.msgs(dow="3", hour="10"), 2)
        self.assertEqual(self.msgs(dow="4", hour="10"), 0)

    def test_configured_zone(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_TZ": "Asia/Kolkata"}):
            self.assertEqual(self.msgs(hour="15"), 1)
            self.assertEqual(self.msgs(hour="16"), 1)
            self.assertEqual(self.msgs(hour="10"), 0)
            self.assertEqual(self.msgs(hour="14", dow="4"), 1)
            # the filter agrees with the hour_dow matrix it is clicked from
            for cell in dashboard.api_distributions(req(window="all"))["hour_dow"]:
                self.assertEqual(self.msgs(hour=str(cell["hour"]), dow=str(cell["dow"])), cell["msgs"])

    def test_repeatable_and_comma_separated(self):
        rep = Request({"type": "http", "headers": [], "query_string": b"window=all&hour=10&hour=13"})
        self.assertEqual(dashboard.api_summary(rep)["msgs"], 3)
        self.assertEqual(self.msgs(hour="9,13"), 2)
        self.assertEqual(self.msgs(dow="3,4"), 4)

    def test_invalid_values_ignored(self):
        self.assertEqual(self.msgs(hour="24"), 4)
        self.assertEqual(self.msgs(hour="x"), 4)
        self.assertEqual(self.msgs(dow="7"), 4)
        self.assertEqual(self.msgs(hour="all"), 4)

    def test_every_endpoint_kind(self):
        q = {"window": "all", "hour": "10"}
        self.assertEqual(sum(r["msgs"] for r in dashboard.api_timeseries(req(**q))["rows"]), 2)
        self.assertEqual(sum(r["msgs"] for r in dashboard.api_heatmap(req(**q))["rows"]), 2)
        self.assertEqual(sum(r["msgs"] for r in dashboard.api_by_model(req(**q))["rows"]), 2)
        self.assertEqual(sum(r["msgs"] for r in dashboard.api_by_project(req(**q))["rows"]), 2)
        self.assertEqual(sum(r["msgs"] for r in dashboard.api_by_account(req(**q))["rows"]), 2)
        self.assertEqual([r["session_id"] for r in dashboard.api_sessions(req(**q))["rows"]], ["s1"])
        self.assertEqual(sum(c["msgs"] for c in dashboard.api_distributions(req(**q))["hour_dow"]), 2)
        self.assertEqual(dashboard.api_breakdown(req(**q, dim="branch"))["total_msgs"], 2)
        # api_events carry their own ts
        self.assertEqual(dashboard.api_reliability(req(**q))["totals"]["retry"], 0)
        self.assertEqual(dashboard.api_reliability(req(window="all", hour="13"))["totals"]["retry"], 1)

    def test_with_explicit_range_and_day(self):
        self.assertEqual(dashboard.api_summary(req(day="2026-10-01", hour="10"))["msgs"], 2)
        self.assertEqual(dashboard.api_summary(req(**{"from": "2026-10-02", "to": "2026-10-02"}, hour="9"))["msgs"], 1)

    def test_filtered_scope_like_other_filters(self):
        s = dashboard.api_summary(req(window="all", hour="10"))
        self.assertEqual(s["sessions"], 1)
        self.assertGreater(s["api_cost"], 0)


if __name__ == "__main__":
    unittest.main()
