"""Billing: recurring periods anchored on the original day; billing-path detection per config dir.

Temp dirs only; the gateway probe is faked, billing.json and config.json are redirected.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import billing  # noqa: E402

SECRET = "sk-ant-api03-DO-NOT-LEAK"


class PeriodTest(unittest.TestCase):
    def test_add_months_clamps_without_drift(self):
        self.assertEqual(billing._add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(billing._add_months(date(2026, 1, 31), 2), date(2026, 3, 31))
        self.assertEqual(billing._add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(billing._add_months(date(2026, 12, 15), 1), date(2027, 1, 15))

    def test_recurring_periods_anchor_on_start_day(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(billing, "BILLING_PATH", Path(d) / "b.json"):
            billing.save_charges([{"receipt_id": "W", "recurring": True, "start": "2026-01-31", "plan": "Team",
                                   "amount_usd": 100.0, "account": "work"}])
            got = [(c["start"], c["end"]) for c in billing.effective_charges(today=date(2026, 5, 15))]
        self.assertEqual(got, [("2026-01-31", "2026-02-28"), ("2026-02-28", "2026-03-31"),
                               ("2026-03-31", "2026-04-30"), ("2026-04-30", "2026-05-31")])


class BillingModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.patches = [mock.patch.object(billing, "BILLING_PATH", self.d / "billing.json"),
                        mock.patch.dict(os.environ, {"CLAUDE_USAGE_CONFIG": str(self.d / "config.json")})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def cfg(self, name, settings=None, local=None):
        p = self.d / name
        p.mkdir()
        if settings is not None:
            (p / "settings.json").write_text(json.dumps(settings))
        if local is not None:
            (p / "settings.local.json").write_text(json.dumps(local))
        return p

    def detect(self, settings=None, local=None, probe=lambda h: "unknown"):
        return billing.detect_billing_mode(self.cfg(f"c{len(list(self.d.iterdir()))}", settings, local), probe)

    def test_modes(self):
        self.assertEqual(self.detect()["billing_mode"], "subscription")  # no settings.json at all
        self.assertEqual(self.detect({"env": {}})["billing_mode"], "subscription")
        self.assertEqual(self.detect({"env": {"ANTHROPIC_API_KEY": SECRET}})["billing_mode"], "api")
        self.assertEqual(self.detect({"apiKeyHelper": "~/bin/key.sh"})["billing_mode"], "api")
        self.assertEqual(self.detect({}, {"env": {"ANTHROPIC_API_KEY": SECRET}})["billing_mode"], "api")
        cloud = self.detect({"env": {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": SECRET}})
        self.assertEqual((cloud["billing_mode"], cloud["provider"]), ("cloud", "bedrock"))
        self.assertEqual(self.detect({"env": {"CLAUDE_CODE_USE_VERTEX": "1"}})["provider"], "vertex")
        # a base URL on an Anthropic host is still Anthropic, not a gateway
        self.assertEqual(self.detect({"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                                              "ANTHROPIC_API_KEY": SECRET}})["billing_mode"], "api")
        gw = self.detect({"env": {"ANTHROPIC_BASE_URL": "https://user:pw@openrouter.ai/api/v1?k=" + SECRET,
                                  "ANTHROPIC_AUTH_TOKEN": SECRET}})
        self.assertEqual((gw["billing_mode"], gw["provider"], gw["gateway_host"]), ("gateway", "openrouter", "openrouter.ai"))
        local = self.detect({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"}}, probe=lambda h: "cliproxyapi")
        self.assertEqual((local["provider"], local["gateway_host"]), ("cliproxyapi", "127.0.0.1:8317"))

    def test_secret_values_never_returned(self):
        info = self.detect({"apiKeyHelper": SECRET, "env": {"ANTHROPIC_API_KEY": SECRET,
                                                            "ANTHROPIC_BASE_URL": "https://x:" + SECRET + "@gw.example/v1"}})
        self.assertNotIn(SECRET, json.dumps(info))
        self.assertEqual(info["signals"], ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "apiKeyHelper"])

    def test_remote_gateways_are_never_probed(self):
        probe = mock.Mock(return_value="cliproxyapi")
        info = self.detect({"env": {"ANTHROPIC_BASE_URL": "https://gw.example.com"}}, probe=probe)
        probe.assert_not_called()
        self.assertEqual(info["provider"], "unknown")

    def test_payment_and_actual_cost(self):
        gw = self.cfg("gw", {"env": {"ANTHROPIC_BASE_URL": "http://localhost:4000"}})
        info = billing.account_billing("router", [gw], probe=lambda h: "litellm_proxy")
        self.assertEqual(info["payment"], "per_token")
        self.assertEqual(billing.actual_cost(info, 100.0, 0.0), 100.0)
        info = billing.account_billing("unknown_gw", [gw], probe=lambda h: "unknown")
        self.assertEqual(info["payment"], "unknown")
        self.assertIsNone(billing.actual_cost(info, 100.0, 0.0))
        # receipts (any plan name) make a gateway account a subscription
        billing.save_charges([{"receipt_id": "c1", "start": "2026-09-01", "end": "2026-10-01",
                               "plan": "ChatGPT Pro", "amount_usd": 200.0, "account": "unknown_gw"}])
        info = billing.account_billing("unknown_gw", [gw], probe=lambda h: "unknown")
        self.assertEqual((info["payment"], info["has_receipts"]), ("subscription", True))
        self.assertEqual(billing.actual_cost(info, 100.0, 200.0), 200.0)
        # api accounts pay list price; cloud adds the optional regional premium from config
        api = self.cfg("api", {"env": {"ANTHROPIC_API_KEY": SECRET}})
        self.assertEqual(billing.actual_cost(billing.account_billing("a", [api]), 50.0, 0.0), 50.0)
        cloud = self.cfg("cloud", {"env": {"CLAUDE_CODE_USE_VERTEX": "1"}})
        (self.d / "config.json").write_text(json.dumps({"accounts": {"c": {"regional_premium": True},
                                                                     "gwx": {"billing_mode": "gateway",
                                                                             "markup_pct": 5.5}}}))
        self.assertEqual(billing.actual_cost(billing.account_billing("c", [cloud]), 50.0, 0.0), 55.0)
        # config override of the mode itself
        info = billing.account_billing("gwx", [api], probe=lambda h: "unknown")
        self.assertEqual((info["billing_mode"], info["detected"], info["payment"]), ("gateway", False, "per_token"))
        self.assertAlmostEqual(billing.actual_cost(info, 100.0, 0.0), 105.5)


if __name__ == "__main__":
    unittest.main()
