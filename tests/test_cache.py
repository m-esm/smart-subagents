"""Characterization tests for the on-disk usage cache: TTL boundary at 180s,
atomic writes, permissions, refresh-lock cleanup, and corrupt-cache handling.

`_CACHE_DIR` / `CACHE_PATH` are computed once at module import time from
`XDG_CACHE_HOME`, so these tests monkeypatch the loaded module's globals
directly (safe: functions resolve bare names through the module's __dict__
at call time) rather than trying to re-import with a different environment.

The `--fresh` CLI flag's cache bypass is characterized at the shell layer in
test_shell.py (it only matters together with `--cli all`, which would also
exercise the live credential probes this suite must not touch).
"""

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_usage_module  # noqa: E402


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        self.m = load_usage_module()
        self._tmp = tempfile.TemporaryDirectory(prefix="ssa-cache-")
        cache_dir = Path(self._tmp.name) / "smart-subagents"
        self.m._CACHE_DIR = cache_dir
        self.m.CACHE_PATH = cache_dir / "ai-cli-usage.json"
        self.fixed_now = 1_700_000_000.0
        self.m._now = lambda: self.fixed_now

    def tearDown(self):
        self._tmp.cleanup()

    def _write_cache(self, cached_at, extra=None):
        doc = {"clis": [], "cached_at": cached_at}
        if extra:
            doc.update(extra)
        self.m._CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.m.CACHE_PATH.write_text(json.dumps(doc))


class TtlBoundaryTests(CacheTestCase):
    def test_exactly_at_ttl_is_still_fresh(self):
        # age == CACHE_TTL_SEC (180): the check is a strict `>`, so this is
        # the last moment still considered fresh.
        self._write_cache(self.fixed_now - self.m.CACHE_TTL_SEC)
        self.assertIsNotNone(self.m._load_cache())

    def test_one_thousandth_past_ttl_is_stale(self):
        self._write_cache(self.fixed_now - self.m.CACHE_TTL_SEC - 0.001)
        self.assertIsNone(self.m._load_cache())

    def test_well_within_ttl(self):
        self._write_cache(self.fixed_now - 1.0)
        self.assertIsNotNone(self.m._load_cache())

    def test_well_past_ttl(self):
        self._write_cache(self.fixed_now - 3600.0)
        self.assertIsNone(self.m._load_cache())


class CorruptCacheTests(CacheTestCase):
    def test_missing_file_is_none(self):
        self.assertFalse(self.m.CACHE_PATH.exists())
        self.assertIsNone(self.m._load_cache())

    def test_invalid_json_is_treated_as_missing(self):
        self.m._CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.m.CACHE_PATH.write_text("{ not json at all")
        self.assertIsNone(self.m._load_cache())

    def test_missing_cached_at_field_defaults_to_epoch_and_is_stale(self):
        # characterization: current behavior: no "cached_at" key means
        # data.get("cached_at", 0) is 0, so the age is enormous and the
        # cache reads as missing rather than as an error.
        self.m._CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.m.CACHE_PATH.write_text(json.dumps({"clis": []}))
        self.assertIsNone(self.m._load_cache())


class SaveCacheTests(CacheTestCase):
    def test_atomic_write_leaves_no_stray_temp_files(self):
        self.m._save_cache({"clis": [{"cli": "codex"}]})
        entries = sorted(p.name for p in self.m._CACHE_DIR.iterdir())
        self.assertEqual(entries, ["ai-cli-usage.json"])

    def test_saved_payload_round_trips_and_gains_cached_at(self):
        self.m._save_cache({"clis": [{"cli": "codex"}]})
        loaded = json.loads(self.m.CACHE_PATH.read_text())
        self.assertEqual(loaded["clis"], [{"cli": "codex"}])
        self.assertIn("cached_at", loaded)

    def test_dir_is_0700_and_file_is_0600(self):
        self.m._save_cache({"clis": []})
        dir_mode = stat.S_IMODE(self.m._CACHE_DIR.stat().st_mode)
        file_mode = stat.S_IMODE(self.m.CACHE_PATH.stat().st_mode)
        self.assertEqual(oct(dir_mode), oct(0o700))
        self.assertEqual(oct(file_mode), oct(0o600))

    def test_second_save_overwrites_cleanly(self):
        self.m._save_cache({"clis": [{"cli": "codex"}]})
        self.m._save_cache({"clis": [{"cli": "grok"}]})
        loaded = json.loads(self.m.CACHE_PATH.read_text())
        self.assertEqual(loaded["clis"], [{"cli": "grok"}])
        entries = sorted(p.name for p in self.m._CACHE_DIR.iterdir())
        self.assertEqual(entries, ["ai-cli-usage.json"])


class RefreshLockTests(CacheTestCase):
    def test_first_acquire_succeeds_and_creates_the_lock_dir(self):
        self.assertTrue(self.m._acquire_refresh_lock())
        self.assertTrue((self.m._CACHE_DIR / "refresh.lock").is_dir())

    def test_release_removes_the_lock_dir(self):
        self.m._acquire_refresh_lock()
        self.m._release_refresh_lock()
        self.assertFalse((self.m._CACHE_DIR / "refresh.lock").exists())

    def test_release_without_acquire_is_a_no_op(self):
        # No lock dir exists; releasing must not raise.
        self.m._release_refresh_lock()

    def test_stale_lock_from_a_killed_refresher_is_broken_immediately(self):
        # A SIGKILLed refresher leaves refresh.lock behind forever. One on
        # this machine was 8 days old, so every cold-cache caller paid a flat
        # 10 s wait. Older than one TTL means nobody owns it.
        import os
        import time as _time

        # Real clock here: without the fix this waits out the full 10 s poll
        # and returns False, which is exactly the cost being removed.
        self.m._now = _time.time
        lock = self.m._CACHE_DIR / "refresh.lock"
        lock.mkdir(parents=True)
        old = _time.time() - self.m.CACHE_TTL_SEC - 60
        os.utime(str(lock), (old, old))

        started = _time.time()
        self.assertTrue(self.m._acquire_refresh_lock())
        self.assertLess(_time.time() - started, 1.0)
        self.assertTrue(lock.is_dir())

    def test_a_lock_younger_than_the_ttl_is_still_respected(self):
        import os

        lock = self.m._CACHE_DIR / "refresh.lock"
        lock.mkdir(parents=True)
        recent = self.fixed_now - 1.0
        os.utime(str(lock), (recent, recent))
        # A fresh cache lets the waiter give up at once; without it the wait
        # loop runs against a frozen clock.
        self._write_cache(self.fixed_now - 1.0)
        self.assertFalse(self.m._acquire_refresh_lock())
        # The live lock was neither removed nor re-taken.
        self.assertAlmostEqual(os.path.getmtime(str(lock)), recent, places=3)

    def test_second_acquire_returns_false_once_a_fresh_cache_appears(self):
        self.assertTrue(self.m._acquire_refresh_lock())
        # Simulate the first refresher finishing and publishing a fresh cache
        # while a second caller is waiting on the lock.
        self._write_cache(self.fixed_now - 1.0)
        self.assertFalse(self.m._acquire_refresh_lock())


if __name__ == "__main__":
    unittest.main()
