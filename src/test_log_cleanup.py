from __future__ import annotations

import os
import time

from src.log_cleanup import cleanup_default_targets, cleanup_path


def _touch(path, *, age_days: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    stamp = time.time() - age_days * 86_400
    os.utime(path, (stamp, stamp))


def test_deletes_only_files_older_than_window(tmp_path):
    old = tmp_path / "old.png"
    fresh = tmp_path / "fresh.png"
    other = tmp_path / "keep.txt"
    _touch(old, age_days=10)
    _touch(fresh, age_days=1)
    _touch(other, age_days=30)

    result = cleanup_path(tmp_path, "*.png", max_age_days=7)

    assert result.deleted == [old]
    assert result.freed_bytes == 10
    assert not old.exists()
    assert fresh.exists()
    assert other.exists()


def test_dry_run_keeps_files(tmp_path):
    old = tmp_path / "old.png"
    _touch(old, age_days=10)

    result = cleanup_path(tmp_path, "*.png", max_age_days=7, dry_run=True)

    assert result.deleted == [old]
    assert old.exists()
    assert "would delete=1" in result.summary(dry_run=True)


def test_missing_directory_is_noop(tmp_path):
    result = cleanup_path(tmp_path / "nope", "*.png")
    assert result.deleted == []
    assert result.scanned == 0


def test_default_targets_resolve_relative_to_root(tmp_path):
    screenshot = tmp_path / "data" / "logs" / "facebook" / "obj1.png"
    _touch(screenshot, age_days=9)
    tmp_logs = tmp_path / "tmp"
    _touch(tmp_logs / "run.log", age_days=9)

    result = cleanup_default_targets(
        tmp_path,
        max_age_days=7,
        targets=(("data/logs/facebook", "*.png"), (str(tmp_logs), "*.log")),
    )

    assert len(result.deleted) == 2
    assert not screenshot.exists()
