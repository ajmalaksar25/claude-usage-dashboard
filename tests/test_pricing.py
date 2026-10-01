"""Pricing tests: stdlib only, fake fetcher, temp DB. Never touches the network or usage.db."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricing  # noqa: E402


class PricingTest(unittest.TestCase):
    def setUp(self):
        pricing._live, pricing._meta = {}, {}

    def test_canonical_ids(self):
        self.assertEqual(pricing.canonical("claude-opus-4-5-20251101"), "claude-opus-4-5")
        self.assertEqual(pricing.canonical("us.anthropic.claude-sonnet-4-5-20250929-v1:0"), "claude-sonnet-4-5")
        self.assertEqual(pricing.canonical("claude-opus-4-5@20251101"), "claude-opus-4-5")

    def test_live_rates(self):
        # 1M of each bucket on Sonnet 5 = 2 + 10 + 2.5 + 4 + 0.2
        cost, tier = pricing.cost_for_model("claude-sonnet-5", 10**6, 10**6, 10**6, 10**6, 10**6)
        self.assertAlmostEqual(cost, 18.7)
        self.assertEqual(tier, "claude-sonnet-5")
        self.assertEqual(pricing.cost_for_model("gpt-5.6-luna", 1, 1, 1, 1, 1), (0.0, None))
        self.assertEqual(pricing.rate_for("claude-opus-9")[1], pricing.FAMILY_FALLBACK["opus"])

    def test_sync_detects_change_and_reprices(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages (msg_id TEXT, model TEXT, tier TEXT, input_tokens INT, output_tokens INT,"
                         " cache_5m_write INT, cache_1h_write INT, cache_read INT, cost_usd REAL)")
            conn.execute("INSERT INTO messages VALUES ('a','claude-sonnet-5',NULL,1000000,0,0,0,0,999.0)")
            conn.commit()
            logs = []
            # first run: no cache file -> fetch, no diff vs seed, but first_run reprices
            r = pricing.sync(db, conn, fetch=lambda: dict(pricing.PRICING), log=logs.append)
            self.assertTrue(r["fetched"]); self.assertEqual(r["changed"], [])
            self.assertAlmostEqual(conn.execute("SELECT cost_usd FROM messages").fetchone()[0], 2.0)
            self.assertTrue(pricing.cache_path_for(db).exists())
            # second run same day: cached, no fetch
            r = pricing.sync(db, conn, fetch=lambda: 1 / 0, log=logs.append)
            self.assertFalse(r["fetched"])
            # forced run with a moved rate -> reprice
            bumped = {**pricing.PRICING, "claude-sonnet-5": (4.0, 10.0, 2.5, 4.0, 0.2)}
            r = pricing.sync(db, conn, force=True, fetch=lambda: bumped, log=logs.append)
            self.assertEqual(r["changed"], ["claude-sonnet-5"])
            self.assertAlmostEqual(conn.execute("SELECT cost_usd FROM messages").fetchone()[0], 4.0)
            # offline forced run keeps cached rates
            r = pricing.sync(db, conn, force=True, fetch=lambda: 1 / 0, log=logs.append)
            self.assertFalse(r["fetched"]); self.assertEqual(pricing.rate_for("claude-sonnet-5")[0][0], 4.0)
            self.assertTrue(any("refresh failed" in l for l in logs))


if __name__ == "__main__":
    unittest.main()
