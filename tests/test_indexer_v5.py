"""Indexer v5: attribution columns, synthetic exclusion, side tables, migration + backfill."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import M, asst, cost_state, index, reset_pricing, write_jsonl

import indexer


class IndexerV5Test(unittest.TestCase):
    def setUp(self):
        reset_pricing()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.root = self.d / "projects"
        self.db = self.d / "usage.db"
        self.acct = [{"name": "personal", "root": self.root, "priority": 20}]

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        return c

    def test_attribution_columns_and_thinking_tokens(self):
        u = {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": 0,
             "output_tokens_details": {"thinking_tokens": 30}}
        write_jsonl(self.root / "p" / "s1.jsonl", [
            asst("2026-10-01T10:00:00.000Z", "s1", "m1", usage=u, isSidechain=True, agentId="a1",
                 attributionAgent="Explore", attributionSkill="dataviz", attributionMcpServer="context7",
                 attributionPlugin="superpowers", gitBranch="main", entrypoint="cli", version="2.1.283",
                 effort="xhigh"),
        ])
        index(self.db, self.acct)
        r = dict(self.conn().execute("SELECT * FROM messages").fetchone())
        self.assertEqual((r["is_sidechain"], r["agent_id"], r["attribution_agent"], r["attribution_skill"]),
                         (1, "a1", "Explore", "dataviz"))
        self.assertEqual((r["attribution_mcp_server"], r["attribution_plugin"], r["git_branch"]),
                         ("context7", "superpowers", "main"))
        self.assertEqual((r["entrypoint"], r["cc_version"], r["effort"], r["thinking_tokens"]),
                         ("cli", "2.1.283", "xhigh", 30))

    def test_synthetic_rows_are_not_messages(self):
        zero = {"input_tokens": 0, "output_tokens": 0}
        write_jsonl(self.root / "p" / "s1.jsonl", [
            asst("2026-10-01T10:00:00.000Z", "s1", "m1", inp=M),
            asst("2026-10-01T10:01:00.000Z", "s1", "syn1", model="<synthetic>", usage=zero,
                 isApiErrorMessage=True, error="rate_limit", apiErrorStatus=429,
                 quotaLimits={"status": "rejected", "rateLimitType": "five_hour"}),
        ])
        index(self.db, self.acct)
        c = self.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        # ...but the error it carried is still an API event (rate_limit + quota)
        kinds = sorted(r[0] for r in c.execute("SELECT kind FROM api_events"))
        self.assertEqual(kinds, ["quota", "rate_limit"])

    def test_side_tables(self):
        write_jsonl(self.root / "p" / "s1.jsonl", [
            asst("2026-10-01T10:00:00.000Z", "s1", "m1", inp=M),
            {"type": "pr-link", "sessionId": "s1", "prNumber": 7, "prUrl": "https://gh/x/pull/7",
             "prRepository": "o/x", "timestamp": "2026-10-01T10:05:00Z"},
            {"type": "pr-link", "sessionId": "s1", "prNumber": 7, "prUrl": "https://gh/x/pull/7",
             "prRepository": "o/x", "timestamp": "2026-10-01T10:06:00Z"},  # duplicate: lands once
            {"type": "ai-title", "aiTitle": "Old title", "sessionId": "s1"},
            {"type": "ai-title", "aiTitle": "New title", "sessionId": "s1"},
            {"type": "custom-title", "customTitle": "my-name", "sessionId": "s1"},
            {"type": "system", "subtype": "turn_duration", "durationMs": 60000, "messageCount": 4,
             "timestamp": "2026-10-01T10:02:00Z", "uuid": "t1", "sessionId": "s1", "isSidechain": False,
             "cwd": "/work/proj"},
            {"type": "system", "subtype": "api_error", "retryAttempt": 2, "maxRetries": 10,
             "timestamp": "2026-10-01T10:03:00Z", "uuid": "e1", "sessionId": "s1", "cwd": "/work/proj"},
            {"type": "assistant", "timestamp": "2026-10-01T10:04:00Z", "uuid": "e2", "sessionId": "s1",
             "isApiErrorMessage": True, "error": "server_error", "apiErrorStatus": 503,
             "message": {"id": "x", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0}}},
            {"type": "assistant", "timestamp": "2026-10-01T10:04:30Z", "uuid": "e3", "sessionId": "s1",
             "isApiErrorMessage": True, "error": "invalid_request",
             "message": {"id": "y", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0}}},
        ])
        index(self.db, self.acct)
        c = self.conn()
        self.assertEqual([tuple(r) for r in c.execute("SELECT session_id, pr_number, repo FROM pr_links")],
                         [("s1", 7, "o/x")])
        titles = {r["source"]: r["title"] for r in c.execute("SELECT * FROM session_titles")}
        self.assertEqual(titles, {"ai": "New title", "custom": "my-name"})
        self.assertEqual(c.execute("SELECT duration_ms, project FROM turn_durations").fetchone()[:], (60000, "proj"))
        ev = {r["kind"]: r["detail"] for r in c.execute("SELECT kind, detail FROM api_events")}
        self.assertEqual(ev, {"retry": "attempt 2/10", "server_error": "server_error 503",
                              "other_error": "invalid_request"})

    def test_cost_state_per_process_latest_checkpoint_wins(self):
        p = self.root / "p"
        # process A: two checkpoints in file order; process B (a resume) restarts from 0
        write_jsonl(p / "s1.jsonl", [
            asst("2026-10-01T10:00:00.000Z", "s1", "m1", inp=M),
            cost_state("s1", 1000, 0.5, {"claude-haiku-4-5-20251001": (0.5, 0)}, duration=10),
            cost_state("s1", 1000, 0.9, {"claude-haiku-4-5-20251001": (0.6, 5), "claude-sonnet-5[1m]": (0.3, 0)},
                       duration=20),
            cost_state("s1", 2000, 0.25, {"claude-haiku-4-5-20251001": (0.25, 0)}),
        ])
        # a stale copy of process A's first checkpoint in another file must not win
        write_jsonl(p / "copy.jsonl", [cost_state("s1", 1000, 0.5, {"claude-haiku-4-5-20251001": (0.5, 0)},
                                                  duration=10)])
        index(self.db, self.acct)
        c = self.conn()
        procs = {r["start_time_ms"]: r["total_cost_usd"] for r in c.execute("SELECT * FROM session_costs")}
        self.assertEqual(procs, {1000: 0.9, 2000: 0.25})
        models = {(r["start_time_ms"], r["model_key"]): (r["cost_usd"], r["web_search_requests"])
                  for r in c.execute("SELECT * FROM session_cost_models")}
        self.assertEqual(models, {(1000, "claude-haiku-4-5"): (0.6, 5), (1000, "claude-sonnet-5"): (0.3, 0),
                                  (2000, "claude-haiku-4-5"): (0.25, 0)})

    def test_migrates_old_db_drops_synthetic_and_backfills_once(self):
        c = sqlite3.connect(self.db)
        c.executescript("""
            CREATE TABLE messages (msg_id TEXT PRIMARY KEY, ts TEXT NOT NULL, ts_day TEXT NOT NULL,
              ts_hour INTEGER NOT NULL, ts_dow INTEGER NOT NULL, session_id TEXT, project TEXT, project_path TEXT,
              model TEXT, tier TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_5m_write INTEGER,
              cache_1h_write INTEGER, cache_read INTEGER, cost_usd REAL,
              account TEXT NOT NULL DEFAULT 'personal', source_priority INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, rows INTEGER, last_seen TEXT);
            CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
            INSERT INTO meta VALUES ('schema_version','4'), ('modifiers_backfill','1');
            INSERT INTO messages VALUES ('syn','2026-09-01T00:00:00Z','2026-09-01',0,0,'s0','p','p','<synthetic>',
              NULL,0,0,0,0,0,0,'personal',20);
            INSERT INTO messages VALUES ('gone','2026-09-01T00:00:00Z','2026-09-01',0,0,'s0','p','p',
              'claude-sonnet-5','claude-sonnet-5',1000000,0,0,0,0,2.0,'personal',20);
        """)
        c.commit()
        c.close()
        f = self.root / "p" / "s1.jsonl"
        write_jsonl(f, [asst("2026-10-01T10:00:00.000Z", "s1", "m1", inp=M, entrypoint="cli", version="2.1.1")])
        # pretend the file was already indexed under v4, so only the v5 backfill can pick it up
        c = sqlite3.connect(self.db)
        st = f.stat()
        c.execute("INSERT INTO files VALUES (?,?,?,0,'x')", (str(f), st.st_size, st.st_mtime_ns))
        c.commit()
        c.close()
        index(self.db, self.acct)
        c = self.conn()
        rows = {r["msg_id"]: dict(r) for r in c.execute("SELECT * FROM messages")}
        self.assertEqual(set(rows), {"gone", "m1"})          # synthetic dropped, gone row kept
        self.assertIsNone(rows["gone"]["cc_version"])         # its transcript is gone: unknown attribution
        self.assertEqual(rows["m1"]["entrypoint"], "cli")     # re-parsed by the backfill
        meta = dict(c.execute("SELECT k, v FROM meta").fetchall())
        self.assertEqual((meta["schema_version"], meta["backfill_version"]), (indexer.SCHEMA_VERSION,) * 2)
        # second run: nothing re-parsed
        self.assertEqual(index(self.db, self.acct)["files_changed"], 0)

    def test_gateway_rows_priced_from_provider_list(self):
        write_jsonl(self.root / "p" / "s1.jsonl", [
            asst("2026-10-01T10:00:00.000Z", "s1", "g1", model="gpt-5.6-luna", inp=M, out=M),
            asst("2026-10-01T10:00:01.000Z", "s1", "g2", model="mystery-model-x", inp=M),
            asst("2026-10-01T10:00:02.000Z", "s1", "g3", model="gpt-5.6-sol",
                 usage={"input_tokens": 0, "output_tokens": 0}),
        ])
        index(self.db, self.acct)
        rows = {r["msg_id"]: r for r in self.conn().execute("SELECT * FROM messages")}
        self.assertAlmostEqual(rows["g1"]["cost_usd"], 0.20 + 1.20)
        self.assertEqual(rows["g1"]["tier"], "gpt-5.6-luna")
        self.assertIsNone(rows["g2"]["tier"])  # no price anywhere: unpriced, flagged by tier NULL
        self.assertEqual(rows["g3"]["usage_reported"], 0)


if __name__ == "__main__":
    unittest.main()
