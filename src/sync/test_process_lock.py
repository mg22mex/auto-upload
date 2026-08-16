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
        q.put(f"abort:{exc}")


class TestProcessLock(unittest.TestCase):
    def test_other_process_aborts(self):
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
                msg = q.get(timeout=2)
                self.assertTrue(str(msg).startswith("abort:"), msg)
                self.assertIn("ABORT", str(msg))
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
