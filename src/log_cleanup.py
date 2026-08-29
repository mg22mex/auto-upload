"""Retention sweep for debug screenshots and temporary run logs.

Deliberately conservative: only files matching an explicit glob under an
explicit root are considered, and directories are never removed.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_TARGETS: tuple[tuple[str, str], ...] = (
    ("data/logs/facebook", "*.png"),
    ("/tmp", "*.log"),
)


@dataclass
class CleanupResult:
    deleted: list[Path] = field(default_factory=list)
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    scanned: int = 0

    def merge(self, other: "CleanupResult") -> "CleanupResult":
        self.deleted.extend(other.deleted)
        self.freed_bytes += other.freed_bytes
        self.errors.extend(other.errors)
        self.scanned += other.scanned
        return self

    def summary(self, *, dry_run: bool = False) -> str:
        verb = "would delete" if dry_run else "deleted"
        mib = self.freed_bytes / (1024 * 1024)
        return (
            f"Cleanup: scanned={self.scanned} {verb}={len(self.deleted)} "
            f"freed={mib:.1f} MiB errors={len(self.errors)}"
        )


def cleanup_path(
    directory: Path,
    pattern: str,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    dry_run: bool = False,
    now: float | None = None,
) -> CleanupResult:
    """Delete files matching ``pattern`` in ``directory`` older than the cutoff."""
    result = CleanupResult()
    directory = Path(directory)
    if not directory.is_dir():
        return result

    cutoff = (now if now is not None else time.time()) - max_age_days * 86_400
    for path in sorted(directory.glob(pattern)):
        if not path.is_file() or path.is_symlink():
            continue
        result.scanned += 1
        try:
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            # /tmp is shared: never touch another user's files.
            if hasattr(os, "getuid") and stat.st_uid != os.getuid():
                continue
            size = stat.st_size
            if not dry_run:
                path.unlink()
            result.deleted.append(path)
            result.freed_bytes += size
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
    return result


def cleanup_default_targets(
    root: Path,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    dry_run: bool = False,
    targets: tuple[tuple[str, str], ...] = DEFAULT_TARGETS,
    now: float | None = None,
) -> CleanupResult:
    """Sweep every configured (directory, glob) target; paths may be relative to root."""
    total = CleanupResult()
    for raw_dir, pattern in targets:
        directory = Path(raw_dir)
        if not directory.is_absolute():
            directory = Path(root) / directory
        total.merge(
            cleanup_path(
                directory,
                pattern,
                max_age_days=max_age_days,
                dry_run=dry_run,
                now=now,
            )
        )
    return total


__all__ = [
    "CleanupResult",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_TARGETS",
    "cleanup_default_targets",
    "cleanup_path",
]
