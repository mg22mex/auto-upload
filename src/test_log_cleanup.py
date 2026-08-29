"""Unit tests — debug screenshot / temp log retention sweep."""
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.log_cleanup import cleanup_default_targets, cleanup_path


def _touch(path: Path, *, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    stamp = time.time() - age_days * 86_400
    os.utime(path, (stamp, stamp))
    return path


class CleanupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class TestCleanupPath(CleanupTestCase):
    def test_deletes_only_files_older_than_window(self):
        old = _touch(self.tmp / "old.png", age_days=10)
        fresh = _touch(self.tmp / "fresh.png", age_days=1)
        other = _touch(self.tmp / "keep.txt", age_days=30)

        result = cleanup_path(self.tmp, "*.png", max_age_days=7)

        self.assertEqual(result.deleted, [old])
        self.assertEqual(result.freed_bytes, 10)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())

    def test_dry_run_keeps_files(self):
        old = _touch(self.tmp / "old.png", age_days=10)

        result = cleanup_path(self.tmp, "*.png", max_age_days=7, dry_run=True)

        self.assertEqual(result.deleted, [old])
        self.assertTrue(old.exists())
        self.assertIn("would delete=1", result.summary(dry_run=True))

    def test_directories_and_symlinks_are_skipped(self):
        nested = self.tmp / "nested.png"
        nested.mkdir()
        target = _touch(self.tmp / "real.log", age_days=30)
        link = self.tmp / "link.png"
        link.symlink_to(target)

        result = cleanup_path(self.tmp, "*.png", max_age_days=7)

        self.assertEqual(result.deleted, [])
        self.assertTrue(nested.is_dir())
        self.assertTrue(link.is_symlink())

    def test_missing_directory_is_noop(self):
        result = cleanup_path(self.tmp / "nope", "*.png")

        self.assertEqual(result.deleted, [])
        self.assertEqual(result.scanned, 0)


class TestCleanupDefaultTargets(CleanupTestCase):
    def test_relative_targets_resolve_against_root(self):
        screenshot = _touch(
            self.tmp / "data" / "logs" / "facebook" / "obj1.png", age_days=9
        )
        tmp_logs = self.tmp / "tmp"
        run_log = _touch(tmp_logs / "run.log", age_days=9)

        result = cleanup_default_targets(
            self.tmp,
            max_age_days=7,
            targets=(("data/logs/facebook", "*.png"), (str(tmp_logs), "*.log")),
        )

        self.assertEqual(len(result.deleted), 2)
        self.assertFalse(screenshot.exists())
        self.assertFalse(run_log.exists())
        self.assertIn("deleted=2", result.summary())


if __name__ == "__main__":
    unittest.main()
