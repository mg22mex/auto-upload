from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path

from src.sync.process_lock import ProcessLock


def _try_acquire(path: str, q: multiprocessing.Queue) -> None:
    try:
        ProcessLock(Path(path)).acquire()
        q.put("acquired")
    except SystemExit as exc:
        q.put(("exit", exc.code, str(exc)))


class TestProcessLock(unittest.TestCase):
    def test_other_process_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auto_upload_bump.lock"
            first = ProcessLock(path)
            first.acquire()
            try:
                q: multiprocessing.Queue = multiprocessing.Queue()
                proc = multiprocessing.Process(target=_try_acquire, args=(str(path), q))
                proc.start()
                proc.join(timeout=5)
                self.assertEqual(proc.exitcode, 0)
                kind, code, _msg = q.get(timeout=2)
                self.assertEqual(kind, "exit")
                self.assertEqual(code, 0)
            finally:
                first.release()

    def test_release_allows_reacquire(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auto_upload_bump.lock"
            first = ProcessLock(path)
            first.acquire()
            first.release()
            second = ProcessLock(path)
            second.acquire()
            second.release()


if __name__ == "__main__":
    unittest.main()
