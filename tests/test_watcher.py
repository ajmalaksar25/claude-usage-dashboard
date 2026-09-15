"""Watcher tests: stdlib only, fake reindex + fake clock, temp dirs only.

Nothing here touches ~/.claude* or usage.db -- the watcher takes both the
account list and the reindex callable as arguments.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import Watcher  # noqa: E402


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.proj = self.root / "-Users-me-demo"
        self.proj.mkdir(parents=True)
        self.session = self.proj / "session-a.jsonl"
        self.session.write_text('{"type":"assistant"}\n', encoding="utf-8")
        self.calls = []
        self.clock = FakeClock()
        self.addCleanup(self.tmp.cleanup)

    def make(self, battery_fn=lambda: False):
        def fake_reindex():
            self.calls.append(self.clock.t)
            return {"files_changed": 1, "rows_inserted": 7}

        w = Watcher(
            fake_reindex,
            lambda: [{"name": "default", "root": self.root}],
            interval=120.0,
            max_interval=900.0,
            battery_factor=2.0,
            clock=self.clock,
            battery_fn=battery_fn,
            log=lambda *a, **k: None,
        )
        w.prime()
        return w

    def test_no_change_no_reindex(self):
        w = self.make()
        self.assertFalse(w.poll())
        self.assertEqual(self.calls, [])

    def test_new_file_triggers_reindex_and_resets_interval(self):
        w = self.make()
        w.poll()  # quiet tick: interval backs off
        self.assertGreater(w.interval, 120.0)
        (self.proj / "session-b.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertTrue(w.poll())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(w.interval, 120.0)
        self.assertIsNotNone(w.last_change_at)
        # the follow-up tick is quiet again -- one change, one reindex
        self.assertFalse(w.poll())
        self.assertEqual(len(self.calls), 1)

    def test_append_to_hot_file_triggers_reindex(self):
        w = self.make()
        with open(self.session, "a", encoding="utf-8") as f:
            f.write('{"type":"assistant"}\n')
        # keep the directory mtime untouched so only the hot-set stat can fire
        st = os.stat(self.proj)
        os.utime(self.proj, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertTrue(w.poll())
        self.assertEqual(len(self.calls), 1)

    def test_interval_backs_off_and_caps(self):
        w = self.make()
        seen = []
        for _ in range(20):
            w.poll()
            seen.append(w.interval)
        self.assertEqual(seen[0], 180.0)       # 120 * 1.5
        self.assertEqual(seen[1], 270.0)       # 180 * 1.5
        self.assertEqual(seen[-1], 900.0)      # capped at max_interval
        self.assertEqual(self.calls, [])

    def test_battery_doubles_interval_with_floor(self):
        w = self.make(battery_fn=lambda: True)
        w.poll()
        self.assertTrue(w.on_battery)
        # 180 * 2 = 360, above the 5 minute floor
        self.assertEqual(w.effective_interval(), 360.0)
        w.interval = 120.0
        # 120 * 2 = 240 -> lifted to the 300s battery floor
        self.assertEqual(w.effective_interval(), 300.0)

    def test_skips_tick_while_refresh_holds_the_lock(self):
        w = self.make()
        (self.proj / "session-c.jsonl").write_text("{}\n", encoding="utf-8")
        w.lock.acquire()
        try:
            self.assertFalse(w.poll())
        finally:
            w.lock.release()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
