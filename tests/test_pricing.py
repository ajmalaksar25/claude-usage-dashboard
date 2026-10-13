"""Pricing tests: stdlib only, fake fetcher, temp DB. Never touches the network or usage.db."""
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricing  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pricing.md"
NODOC = lambda: (_ for _ in ()).throw(RuntimeError("offline"))  # noqa: E731
MSG_COLS = ("(msg_id TEXT, ts TEXT, model TEXT, tier TEXT, input_tokens INT, output_tokens INT, cache_5m_write INT,"
            " cache_1h_write INT, cache_read INT, cost_usd REAL, speed TEXT, inference_geo TEXT,"
            " web_search_requests INT, fast_premium_usd REAL, server_tool_cost_usd REAL, price_estimated INT)")
M = 10**6


class PricingTest(unittest.TestCase):
    def setUp(self):
        pricing._live, pricing._meta = {}, {}

    def test_canonical_strips_context_suffix(self):
        self.assertEqual(pricing.canonical("claude-opus-5[1m]"), "claude-opus-5")
        self.assertEqual(pricing.canonical("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(pricing.rate_for("claude-opus-5[1m]"), (pricing.PRICING["claude-opus-5"], "claude-opus-5"))
        self.assertFalse(pricing.resolve("claude-opus-5[1m]")[2])  # exact row, not an estimate

    def test_canonical_ids(self):
        self.assertEqual(pricing.canonical("claude-opus-4-5-20251101"), "claude-opus-4-5")
        self.assertEqual(pricing.canonical("us.anthropic.claude-sonnet-4-5-20250929-v1:0"), "claude-sonnet-4-5")
        self.assertEqual(pricing.canonical("claude-opus-4-5@20251101"), "claude-opus-4-5")

    def test_live_rates(self):
        # 1M of each bucket on Sonnet 5 = 2 + 10 + 2.5 + 4 + 0.2
        cost, tier = pricing.cost_for_model("claude-sonnet-5", 10**6, 10**6, 10**6, 10**6, 10**6)
        self.assertAlmostEqual(cost, 18.7)
        self.assertEqual(tier, "claude-sonnet-5")
        self.assertEqual(pricing.cost_for_model("mystery-model-x", 1, 1, 1, 1, 1), (0.0, None))  # unpriced
        self.assertEqual(pricing.rate_for("claude-opus-9")[1], pricing.fallback_target("opus"))

    def test_sync_detects_change_and_reprices(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages " + MSG_COLS)
            conn.execute("INSERT INTO messages VALUES ('a','2026-10-01T00:00:00Z','claude-sonnet-5',NULL,1000000,0,0,0,0,999.0,"
                         "'standard',NULL,0,0,0,0)")
            conn.commit()
            logs = []
            # first run: no cache file -> fetch, no diff vs seed, but first_run reprices
            r = pricing.sync(db, conn, fetch=lambda: (dict(pricing.PRICING), {}), fetch_doc=NODOC, log=logs.append)
            self.assertTrue(r["fetched"]); self.assertEqual(r["changed"], [])
            self.assertAlmostEqual(conn.execute("SELECT cost_usd FROM messages").fetchone()[0], 2.0)
            self.assertTrue(pricing.cache_path_for(db).exists())
            # second run same day: cached, no fetch
            r = pricing.sync(db, conn, fetch=lambda: 1 / 0, fetch_doc=NODOC, log=logs.append)
            self.assertFalse(r["fetched"])
            # forced run with a moved rate -> reprice
            bumped = {**pricing.PRICING, "claude-sonnet-5": (4.0, 10.0, 2.5, 4.0, 0.2)}
            r = pricing.sync(db, conn, force=True, fetch=lambda: (bumped, {}), fetch_doc=NODOC, log=logs.append)
            self.assertEqual(r["changed"], ["claude-sonnet-5"])
            self.assertAlmostEqual(conn.execute("SELECT cost_usd FROM messages").fetchone()[0], 4.0)
            # offline forced run keeps cached rates
            r = pricing.sync(db, conn, force=True, fetch=lambda: 1 / 0, fetch_doc=NODOC, log=logs.append)
            self.assertFalse(r["fetched"]); self.assertEqual(pricing.rate_for("claude-sonnet-5")[0][0], 4.0)
            self.assertTrue(any("refresh failed" in l or "rejected" in l for l in logs))


def cost(model, speed="standard", geo=None, **kw):
    """1M of each of the five token categories."""
    return pricing.cost_for(model, M, M, M, M, M, speed=speed, inference_geo=geo, **kw)


class ModifierTest(unittest.TestCase):
    def setUp(self):
        pricing._live, pricing._src, pricing._fast, pricing._fast_src, pricing._geo, pricing._meta = {}, {}, {}, {}, {}, {}
        pricing.ANOMALIES.clear()

    def test_fast_doubles_all_five_categories(self):
        for model in ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"):
            std, fast = cost(model), cost(model, "fast")
            self.assertAlmostEqual(fast.cost, 2 * std.cost)
            self.assertAlmostEqual(fast.fast_premium, std.cost)
        self.assertAlmostEqual(cost("claude-opus-5-5", "fast").cost, 2 * (4 + 20 + 5 + 8 + 0.2))

    def test_standard_is_1x_and_opus_47_has_no_premium(self):
        self.assertEqual(cost("claude-opus-5-5").fast_premium, 0.0)
        self.assertAlmostEqual(cost("claude-opus-4-7", "fast").cost, cost("claude-opus-4-7").cost)

    def test_sonnet_fast_is_1x_and_flagged(self):
        c = cost("claude-sonnet-5", "fast")
        self.assertAlmostEqual(c.cost, 18.7)
        self.assertEqual(pricing.ANOMALIES["claude-sonnet-5:fast-without-premium"], 1)

    def test_opus_46_historical_window(self):
        std = cost("claude-opus-4-6").cost
        self.assertAlmostEqual(cost("claude-opus-4-6", "fast", ts="2026-02-15T23:59:59Z").cost, 3 * std)
        self.assertAlmostEqual(cost("claude-opus-4-6", "fast", ts="2026-02-16T00:00:00.000Z").cost, 6 * std)
        self.assertAlmostEqual(cost("claude-opus-4-6", "fast", ts="2026-03-01T00:00:00Z").cost, 6 * std)

    def test_unknown_future_opus_uses_fallback_multiplier(self):
        self.assertEqual(pricing.fast_multiplier("claude-opus-9"), 2.0)
        c = cost("claude-opus-9", "fast")
        self.assertTrue(c.estimated)
        self.assertAlmostEqual(c.cost, 2 * cost("claude-opus-5-5").cost)

    def test_geo_us_stacks_on_4_6_plus_only(self):
        std = cost("claude-opus-5-5").cost
        self.assertAlmostEqual(cost("claude-opus-5-5", geo="us").cost, 1.1 * std)
        self.assertAlmostEqual(cost("claude-opus-5-5", "fast", geo="us").cost, 2.2 * std)
        self.assertAlmostEqual(cost("claude-opus-4-6", geo="us").cost, 1.1 * cost("claude-opus-4-6").cost)
        self.assertAlmostEqual(cost("claude-opus-4-5", geo="us").cost, cost("claude-opus-4-5").cost)
        for g in ("global", "not_available", "", None):
            self.assertAlmostEqual(cost("claude-opus-5-5", geo=g).cost, std)

    def test_web_search_fee(self):
        c = pricing.cost_for("claude-sonnet-5", 0, 0, 0, 0, 0, web_search_requests=3)
        self.assertAlmostEqual(c.cost, 0.03)
        self.assertAlmostEqual(c.server_tool, 0.03)
        # fee is not multiplied by fast/geo
        c = pricing.cost_for("claude-opus-5-5", 0, 0, 0, 0, 0, speed="fast", inference_geo="us", web_search_requests=3)
        self.assertAlmostEqual(c.cost, 0.03)

    def test_reprice_mixed_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE messages " + MSG_COLS)
        ins = "INSERT INTO messages VALUES (?,?,?,NULL,?,0,0,0,0,999,?,?,?,0,0,0)"
        rows = [("a", "2026-10-01T00:00:00Z", "claude-opus-5-5", M, "standard", None, 0),
                ("b", "2026-10-01T00:00:00Z", "claude-opus-5-5", M, "fast", None, 0),
                ("c", "2026-10-01T00:00:00Z", "claude-opus-5-5", M, "fast", "us", 2),
                ("d", "2026-02-10T00:00:00Z", "claude-opus-4-6", M, "fast", None, 0),
                ("e", "2026-03-10T00:00:00Z", "claude-opus-4-6", M, "fast", None, 0),
                ("f", "2026-10-01T00:00:00Z", "claude-opus-9", M, "fast", None, 0)]
        conn.executemany(ins, rows)
        pricing.reprice(conn)
        got = {r[0]: r[1:] for r in conn.execute(
            "SELECT msg_id, cost_usd, fast_premium_usd, server_tool_cost_usd, price_estimated FROM messages")}
        self.assertAlmostEqual(got["a"][0], 4.0)
        self.assertAlmostEqual(got["b"][0], 8.0); self.assertAlmostEqual(got["b"][1], 4.0)
        self.assertAlmostEqual(got["c"][0], 8.8 + 0.02); self.assertAlmostEqual(got["c"][2], 0.02)
        self.assertAlmostEqual(got["d"][0], 15.0)  # 3x of $5
        self.assertAlmostEqual(got["e"][0], 30.0)  # 6x of $5
        self.assertEqual(got["f"][3], 1); self.assertAlmostEqual(got["f"][0], 8.0)

    def test_litellm_parse_picks_up_fast(self):
        data = {f"claude-x-{i}": {"litellm_provider": "anthropic", "input_cost_per_token": 1e-6,
                                  "output_cost_per_token": 5e-6} for i in range(6)}
        data["claude-opus-5-5"] = {"litellm_provider": "anthropic", "input_cost_per_token": 4e-6,
                                   "output_cost_per_token": 20e-6, "provider_specific_entry": {"fast": 2.0}}
        prices, fast = pricing.parse_litellm(data)
        self.assertEqual(fast, {"claude-opus-5-5": 2.0})
        self.assertEqual(prices["claude-opus-5-5"][0], 4.0)


class DocParseTest(unittest.TestCase):
    def setUp(self):
        pricing._live, pricing._src, pricing._fast, pricing._fast_src, pricing._geo, pricing._meta = {}, {}, {}, {}, {}, {}
        self.md = FIXTURE.read_text()

    def test_fixture_parse(self):
        d = pricing.parse_doc(self.md)
        self.assertEqual(d["prices"]["claude-opus-5-5"], (4.0, 20.0, 5.0, 8.0, 0.20))
        self.assertEqual(d["prices"]["claude-fable-5-1"][4], 0.25)
        self.assertEqual(d["prices"]["claude-mythos-5-1"], (10.0, 50.0, 12.5, 20.0, 0.25))  # note stripped
        self.assertIn("claude-opus-4", d["prices"]); self.assertIn("claude-opus-4-1", d["prices"])  # retired rows
        self.assertEqual(d["prices"]["claude-3-5-haiku"], (0.8, 4.0, 1.0, 1.6, 0.08))
        self.assertEqual(d["prices"]["claude-sonnet-5-5"][0], 2.0)
        for m in ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"):
            self.assertEqual(d["fast"][m], 2.0)
        self.assertEqual(len(d["fast"]), 3)
        self.assertEqual(d["geo"], {"mult": 1.1, "min": [4, 6]})

    def _sync(self, doc_md, db, conn=None, **kw):
        return pricing.sync(db, conn, force=True, fetch=NODOC, fetch_doc=lambda: pricing.parse_doc(doc_md),
                            log=lambda *_: None, **kw)

    def test_new_model_priced_without_code_change(self):
        md = self.md.replace("| Claude Opus 5.5 ", "| Claude Opus 6 | $6 / MTok | $7.50 / MTok | $12 / MTok | $0.60 / MTok | $30 / MTok |\n| Claude Opus 5.5 ", 1)
        md = md.replace("| Claude Opus 5.5                 | $8 / MTok", "| Claude Opus 6                   | $12 / MTok | $60 / MTok |\n| Claude Opus 5.5                 | $8 / MTok", 1)
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages " + MSG_COLS)
            conn.execute("INSERT INTO messages VALUES ('a','2026-10-07T00:00:00Z','claude-opus-6',NULL,1000000,0,0,0,0,0,'fast',NULL,0,0,0,0)")
            self._sync(self.md, db, conn)  # page without Opus 6: estimated via the newest Opus
            self.assertEqual(conn.execute("SELECT price_estimated FROM messages").fetchone()[0], 1)
            r = self._sync(md, db, conn)   # launch day: page lists it -> changed -> auto reprice
            self.assertIn("claude-opus-6", r["changed"])
            cost_, est = conn.execute("SELECT cost_usd, price_estimated FROM messages").fetchone()
            self.assertEqual(est, 0); self.assertAlmostEqual(cost_, 12.0)
            self.assertEqual(pricing.meta()["model_sources"]["claude-opus-6"], "anthropic_doc")
            self.assertEqual(pricing.fallback_target("opus"), "claude-opus-6")

    def test_malformed_doc_rejected_and_cache_kept(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            self._sync(self.md, db)
            before = pricing.cache_path_for(db).read_text()
            for bad in ("# nothing here", self.md.replace("$4 / MTok ", "$0 / MTok ", 1),
                        "\n".join(self.md.splitlines()[:14])):  # too few models
                with self.assertRaises(ValueError):
                    pricing.parse_doc(bad)
            logs = []
            pricing.sync(db, force=True, fetch=NODOC, fetch_doc=lambda: pricing.parse_doc("garbage"), log=logs.append)
            self.assertTrue(any("rejected" in l for l in logs))
            self.assertEqual(pricing.cache_path_for(db).read_text(), before)
            self.assertEqual(pricing.rate_for("claude-opus-5-5")[0], (4.0, 20.0, 5.0, 8.0, 0.20))

    def test_merge_order_doc_over_litellm_over_seed(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            lite = {**pricing.PRICING, "claude-opus-5-5": (9.0, 9.0, 9.0, 9.0, 9.0), "claude-test-1": (1.0, 1.0, 1.0, 1.0, 1.0)}
            pricing.sync(db, force=True, fetch=lambda: (lite, {"claude-haiku-4-5": 1.5}),
                         fetch_doc=lambda: pricing.parse_doc(self.md), log=lambda *_: None)
            self.assertEqual(pricing.rate_for("claude-opus-5-5")[0][0], 4.0)      # doc wins
            m = pricing.meta()["model_sources"]
            self.assertEqual((m["claude-opus-5-5"], m["claude-test-1"]), ("anthropic_doc", "litellm"))
            self.assertEqual(pricing.fast_multiplier("claude-haiku-4-5"), 1.5)    # litellm fast fills the gap
            data = json.loads(pricing.cache_path_for(db).read_text())
            self.assertEqual(data["sources"]["claude-opus-5-5"], "anthropic_doc")

    def test_dynamic_family_fallback(self):
        self.assertEqual(pricing.fallback_target("opus"), "claude-opus-5-5")
        self.assertEqual(pricing.fallback_target("sonnet"), "claude-sonnet-5-5")
        self.assertEqual(pricing.fallback_target("fable"), "claude-fable-5-1")
        pricing._live = {"claude-opus-7": (7.0, 7.0, 7.0, 7.0, 7.0)}
        self.assertEqual(pricing.fallback_target("opus"), "claude-opus-7")
        self.assertEqual(pricing.rate_for("claude-opus-8")[1], "claude-opus-7")
        self.assertEqual(pricing.version_of("claude-opus-5-5") > pricing.version_of("claude-opus-5") > pricing.version_of("claude-opus-4-8"), True)


class DedupTest(unittest.TestCase):
    def test_partial_streamed_copy_never_beats_complete(self):
        import indexer
        def rec(out, uid):
            return {"type": "assistant", "timestamp": "2026-10-01T00:00:00Z", "uuid": uid, "sessionId": "s",
                    "message": {"id": "msg_1", "model": "claude-opus-5-5",
                                "usage": {"input_tokens": 10, "output_tokens": out, "speed": "fast"}}}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "projects"; (root / "p").mkdir(parents=True)
            full, part = root / "p" / "a.jsonl", root / "p" / "b.jsonl"
            full.write_text(json.dumps(rec(500, "u1")) + "\n")
            part.write_text(json.dumps(rec(7, "u2")) + "\n")
            for order in ((full, part), (part, full)):
                db = Path(d) / f"{order[0].stem}.db"
                conn = indexer.open_db(db)
                for f in order:
                    indexer._process_file(conn, f, root)
                conn.commit()
                out, cost_, speed = conn.execute("SELECT output_tokens, cost_usd, speed FROM messages").fetchone()
                self.assertEqual((out, speed), (500, "fast"))
                self.assertAlmostEqual(cost_, (10 * 4 + 500 * 20) / M * 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                conn.close()

    def test_backfill_populates_columns_once_without_duplicates(self):
        import indexer
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "projects"; (root / "p").mkdir(parents=True)
            line = {"type": "assistant", "timestamp": "2026-10-01T00:00:00Z", "uuid": "u", "sessionId": "s",
                    "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {
                        "input_tokens": M, "output_tokens": 0, "speed": "fast", "inference_geo": "us",
                        "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1}}}}
            (root / "p" / "a.jsonl").write_text(json.dumps(line) + "\n")
            db = Path(d) / "usage.db"
            # old-schema DB: row indexed before the modifier columns existed
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages (msg_id TEXT PRIMARY KEY, ts TEXT NOT NULL, ts_day TEXT NOT NULL, ts_hour INTEGER NOT NULL,"
                         " ts_dow INTEGER NOT NULL, session_id TEXT, project TEXT, project_path TEXT, model TEXT, tier TEXT,"
                         " input_tokens INTEGER, output_tokens INTEGER, cache_5m_write INTEGER, cache_1h_write INTEGER,"
                         " cache_read INTEGER, cost_usd REAL, account TEXT NOT NULL DEFAULT 'personal', source_priority INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO messages VALUES ('m1','2026-10-01T00:00:00Z','2026-10-01',0,3,'s','p','p','claude-opus-5-5','claude-opus-5-5',"
                         "1000000,0,0,0,0,4.0,'personal',0)")
            conn.commit(); conn.close()
            for _ in range(2):  # second run must be a no-op
                with mock.patch.object(indexer.pricing, "sync", lambda *a, **k: {}):  # no network
                    indexer.reindex(db, root=root, verbose=False)
                conn = sqlite3.connect(db)
                row = conn.execute("SELECT COUNT(*), MAX(speed), MAX(inference_geo), MAX(web_search_requests), MAX(web_fetch_requests),"
                                   " MAX(cost_usd), MAX(fast_premium_usd), MAX(server_tool_cost_usd) FROM messages").fetchone()
                self.assertEqual(row[:5], (1, "fast", "us", 2, 1))
                self.assertAlmostEqual(row[5], 4.0 * 2 * 1.1 + 0.02)
                self.assertAlmostEqual(row[6], 4.0 * 1.1); self.assertAlmostEqual(row[7], 0.02)
                self.assertEqual(conn.execute("SELECT v FROM meta WHERE k='backfill_version'").fetchone()[0],
                                 indexer.SCHEMA_VERSION)
                conn.close()


class GatewayPricingTest(unittest.TestCase):
    """Non-Claude models (gateway accounts) priced at their own provider's list price via LiteLLM."""

    def setUp(self):
        pricing._live, pricing._src, pricing._fast, pricing._fast_src, pricing._geo, pricing._meta = {}, {}, {}, {}, {}, {}
        pricing._ext = {}

    def test_ext_key_normalises_router_names(self):
        self.assertEqual(pricing.ext_key("gpt-5.6-sol(high)"), "gpt-5.6-sol")
        self.assertEqual(pricing.ext_key("gpt-5.4-mini-2026-03-17"), "gpt-5.4-mini")
        self.assertEqual(pricing.ext_key("openai/gpt-5.6-luna"), "gpt-5.6-luna")
        self.assertEqual(pricing.ext_key("openrouter/openai/gpt-5.6-terra"), "gpt-5.6-terra")
        self.assertEqual(pricing.model_key("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(pricing.model_key("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_seed_prices_gateway_models(self):
        for name in ("gpt-5.6-sol(high)", "openai/gpt-5.6-sol", "gpt-5.6-sol"):
            c = pricing.cost_for(name, M, M, 0, 0, M)
            self.assertAlmostEqual(c.cost, 4.0 + 20.0 + 0.4)
            self.assertEqual((c.tier, c.estimated), ("gpt-5.6-sol", False))
        self.assertEqual(pricing.price_source("gpt-5.6-luna"), "seed")
        self.assertEqual(pricing.price_source("mystery-model-x"), "unpriced")
        self.assertIsNone(pricing.resolve("mystery-model-x"))
        self.assertEqual(pricing.geo_multiplier("gpt-5.6-sol", "us"), 1.0)  # Anthropic residency rule only

    def test_parse_litellm_ext_prefers_first_party_rows(self):
        data = {
            "gpt-9": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-6,
                      "output_cost_per_token": 2e-6, "cache_read_input_token_cost": 1e-7},
            "openrouter/openai/gpt-9": {"litellm_provider": "openrouter", "mode": "chat",
                                        "input_cost_per_token": 9e-6, "output_cost_per_token": 9e-6},
            "gemini/gemini-9-pro": {"litellm_provider": "gemini", "mode": "chat", "input_cost_per_token": 2e-6,
                                    "output_cost_per_token": 4e-6},
            "dall-e-9": {"litellm_provider": "openai", "mode": "image_generation", "input_cost_per_token": 1e-6,
                         "output_cost_per_token": 1e-6},
            "claude-x": {"litellm_provider": "anthropic", "mode": "chat", "input_cost_per_token": 1e-6,
                         "output_cost_per_token": 1e-6},
        }
        ext = pricing.parse_litellm_ext(data)
        self.assertEqual(ext["gpt-9"], (1.0, 2.0, 1.0, 1.0, 0.1))  # no cache write column -> input rate
        self.assertEqual(ext["gemini-9-pro"], (2.0, 4.0, 2.0, 2.0, 2.0))
        self.assertNotIn("dall-e-9", ext)
        self.assertNotIn("claude-x", ext)

    def test_sync_with_ext_table_reprices_unpriced_rows(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages " + MSG_COLS)
            conn.execute("INSERT INTO messages VALUES ('a','2026-10-01T00:00:00Z','gemini-9-pro',NULL,1000000,0,0,0,0,"
                         "0.0,'standard',NULL,0,0,0,0)")
            conn.commit()
            pricing.sync(db, conn, fetch=lambda: (dict(pricing.PRICING), {}), fetch_doc=NODOC, log=lambda *_: None)
            self.assertEqual(conn.execute("SELECT tier FROM messages").fetchone()[0], None)  # still unpriced
            ext = {"gemini-9-pro": (2.0, 4.0, 2.0, 2.0, 2.0)}
            r = pricing.sync(db, conn, force=True, fetch=lambda: (dict(pricing.PRICING), {}, ext), fetch_doc=NODOC,
                             log=lambda *_: None)
            self.assertIn("gemini-9-pro", r["changed"])
            self.assertEqual(conn.execute("SELECT tier, cost_usd FROM messages").fetchone(), ("gemini-9-pro", 2.0))
            # the cache keeps ext prices for the next process
            pricing._ext = {}
            pricing.load(pricing.cache_path_for(db))
            self.assertIn("gemini-9-pro", pricing._ext)


if __name__ == "__main__":
    unittest.main()
