"""Exclusive file lock so VPS and GitHub Actions never share a Chromium profile."""
from __future__ import annotations

import atexit
import fcntl
import os
import signal
import sys
from pathlib import Path
from types import FrameType, TracebackType

DEFAULT_BUMP_LOCK = Path("/tmp/auto_upload_bump.lock")


def bump_lock_path() -> Path:
    raw = (os.getenv("BUMP_LOCK_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_BUMP_LOCK


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(fd: int) -> int | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode("utf-8", errors="replace").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


class ProcessLock:
    """Non-blocking flock. Abort if another live bump holds the file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else bump_lock_path()
        self._fd: int | None = None
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        self._fd = fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = _read_pid(fd)
            os.close(fd)
            self._fd = None
            extra = f" pid={other}" if other else ""
            alive = f" (alive={_pid_alive(other)})" if other else ""
            print("Another instance is currently running. Exiting cleanly.", flush=True)
            print(f"Lock: {self.path}{extra}{alive}", flush=True)
            sys.exit(0)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        os.fsync(fd)
        self._held = True
        print(f"Bump lock acquired: {self.path} pid={os.getpid()}", flush=True)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            if self._held:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        self._fd = None
        self._held = False

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: FrameType | None) -> None:
            print(f"Bump lock: signal {signum}; releasing {self.path}", flush=True)
            self.release()
            raise SystemExit(128 + int(signum))

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
        atexit.register(self.release)
