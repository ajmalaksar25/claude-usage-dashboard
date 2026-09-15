"""Background watcher: re-index when new Claude activity shows up.

One daemon thread polls the account roots that indexer.discover_accounts()
reports and calls the existing incremental reindex() only when something
changed. Detection never opens a transcript -- it only stats:

  tier 1 (every poll)   the projects/ dir of each account root, every project
                        subdirectory (a directory mtime moves when a session
                        file is created or removed), plus a "hot set" of
                        transcript files touched in the last 24h, kept as
                        (path, size, mtime_ns) tuples.
  tier 2 (hourly, or    one os.scandir walk to rebuild the hot set. reindex()
   after a tier-1 hit)  walks everything anyway, so on a change we just call it
                        and let it decide per file.

The interval starts at CUD_WATCH_INTERVAL (120s) and multiplies by 1.5 after
each quiet poll, up to CUD_WATCH_MAX_INTERVAL (15 min); any change reindexes
and resets it. On macOS battery power the sleep is multiplied by
CUD_WATCH_BATTERY_FACTOR (2x) with a 5 minute floor.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Shared with the dashboard's POST /refresh so the two never overlap.
INDEX_LOCK = threading.Lock()

HOT_WINDOW_S = 24 * 3600      # a file counts as "hot" if touched this recently
FULL_SCAN_S = 60 * 60         # tier 2 cadence when nothing changed
BATTERY_CACHE_S = 300         # how long a `pmset -g batt` answer is reused
BATTERY_FLOOR_S = 300         # minimum interval while on battery
BACKOFF = 1.5


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


def default_interval() -> float:
    return _env_float("CUD_WATCH_INTERVAL", 120.0)


def default_max_interval() -> float:
    return _env_float("CUD_WATCH_MAX_INTERVAL", 900.0)


def default_battery_factor() -> float:
    return _env_float("CUD_WATCH_BATTERY_FACTOR", 2.0)


def _on_battery_macos() -> bool:
    """True when `pmset -g batt` says we are on battery. Failure means AC."""
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return False
    return "Battery Power" in out


class Watcher:
    """Polls account roots and reindexes on change. Cheap, single-threaded."""

    def __init__(
        self,
        reindex_fn,
        accounts_fn,
        *,
        interval: float | None = None,
        max_interval: float | None = None,
        battery_factor: float | None = None,
        clock=time.time,
        battery_fn=_on_battery_macos,
        lock: threading.Lock | None = None,
        log=print,
    ):
        self.reindex_fn = reindex_fn
        self.accounts_fn = accounts_fn
        self.base_interval = interval if interval is not None else default_interval()
        self.max_interval = max_interval if max_interval is not None else default_max_interval()
        self.battery_factor = (
            battery_factor if battery_factor is not None else default_battery_factor()
        )
        self.clock = clock
        self.battery_fn = battery_fn
        self.lock = lock if lock is not None else INDEX_LOCK
        self.log = log

        self.interval = self.base_interval
        self.last_check_at: str | None = None
        self.last_change_at: str | None = None
        self.on_battery = False

        self._dirs: dict[str, int] = {}
        self._hot: dict[str, tuple[int, int]] = {}
        self._last_full = 0.0
        self._batt_checked_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- scanning ----------

    def _roots(self) -> list[Path]:
        try:
            return [Path(a["root"]) for a in self.accounts_fn()]
        except Exception:
            return []

    def _scan_dirs(self) -> dict[str, int]:
        """mtime_ns of every projects/ root and its immediate project dirs."""
        dirs: dict[str, int] = {}
        for root in self._roots():
            try:
                dirs[str(root)] = root.stat().st_mtime_ns
                with os.scandir(root) as it:
                    for e in it:
                        try:
                            if e.is_dir():
                                dirs[e.path] = e.stat().st_mtime_ns
                        except OSError:
                            continue
            except OSError:
                continue
        return dirs

    def _scan_hot(self) -> dict[str, tuple[int, int]]:
        """Full walk -> {path: (size, mtime_ns)} for recently touched .jsonl."""
        cutoff = int((self.clock() - HOT_WINDOW_S) * 1e9)
        hot: dict[str, tuple[int, int]] = {}
        for root in self._roots():
            for dirpath, _dirs, files in os.walk(str(root), onerror=lambda _e: None):
                for fn in files:
                    if not fn.endswith(".jsonl"):
                        continue
                    p = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if st.st_mtime_ns >= cutoff:
                        hot[p] = (st.st_size, st.st_mtime_ns)
        return hot

    def _hot_changed(self) -> bool:
        """Re-stat only the hot set; True if any entry moved or vanished."""
        for p, sig in self._hot.items():
            try:
                st = os.stat(p)
            except OSError:
                return True
            if (st.st_size, st.st_mtime_ns) != sig:
                return True
        return False

    # ---------- battery ----------

    def _check_battery(self) -> bool:
        now = self.clock()
        if self._batt_checked_at and now - self._batt_checked_at < BATTERY_CACHE_S:
            return self.on_battery
        self._batt_checked_at = now
        try:
            self.on_battery = bool(self.battery_fn())
        except Exception:
            self.on_battery = False
        return self.on_battery

    def effective_interval(self) -> float:
        """Sleep length: the adaptive interval, stretched while on battery."""
        if self.on_battery:
            return max(self.interval * self.battery_factor, BATTERY_FLOOR_S)
        return self.interval

    # ---------- polling ----------

    def prime(self) -> None:
        """Record the current state so the first poll only sees new activity."""
        self._dirs = self._scan_dirs()
        self._hot = self._scan_hot()
        self._last_full = self.clock()

    def poll(self) -> bool:
        """One tick. Reindexes if something changed; returns True if it did."""
        now = self.clock()
        self.last_check_at = datetime.now(timezone.utc).isoformat()
        self._check_battery()

        dirs = self._scan_dirs()
        changed = dirs != self._dirs or self._hot_changed()
        if not changed:
            self._dirs = dirs
            if (now - self._last_full) >= FULL_SCAN_S:
                self._hot = self._scan_hot()
                self._last_full = now
            self.interval = min(self.interval * BACKOFF, self.max_interval)
            return False
        if not self.lock.acquire(blocking=False):
            # a manual /refresh is running -- leave the state alone so the
            # change is still pending on the next tick
            return False
        try:
            t0 = time.perf_counter()
            summary = self.reindex_fn() or {}
            self.log(
                f"[watcher] reindexed: {summary.get('files_changed', 0)} files changed, "
                f"+{summary.get('rows_inserted', 0)} rows, {time.perf_counter() - t0:.1f}s"
            )
        except Exception as e:  # never let one bad tick kill the thread
            self.log(f"[watcher] reindex failed: {e}")
        finally:
            self.lock.release()
        # files kept moving while we indexed; resync so the next poll is quiet
        self._dirs = self._scan_dirs()
        self._hot = self._scan_hot()
        self._last_full = self.clock()
        self.last_change_at = datetime.now(timezone.utc).isoformat()
        self.interval = self.base_interval
        return True

    # ---------- thread ----------

    def _run(self) -> None:
        self.prime()
        while not self._stop.is_set():
            if self._stop.wait(self.effective_interval()):
                return
            try:
                self.poll()
            except Exception as e:
                self.log(f"[watcher] poll failed: {e}")

    def start(self) -> "Watcher":
        self._thread = threading.Thread(target=self._run, name="cud-watcher", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "enabled": True,
            "interval_s": round(self.effective_interval()),
            "last_check_at": self.last_check_at,
            "last_change_at": self.last_change_at,
            "on_battery": self.on_battery,
        }


DISABLED_STATUS = {
    "enabled": False,
    "interval_s": None,
    "last_check_at": None,
    "last_change_at": None,
    "on_battery": False,
}
