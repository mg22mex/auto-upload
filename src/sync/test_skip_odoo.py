"""Unit tests — SKIP_ODOO / --skip-odoo fast FB debug path."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_weekly_bump_module():
    path = ROOT / "scripts" / "run_weekly_bump.py"
    spec = importlib.util.spec_from_file_location("run_weekly_bump_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WEEKLY = _load_weekly_bump_module()


class TestSkipOdooRequested(unittest.TestCase):
    def test_cli_true(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SKIP_ODOO", None)
            self.assertTrue(WEEKLY.skip_odoo_requested(cli=True))

    def test_env_true(self):
        with patch.dict(os.environ, {"SKIP_ODOO": "true"}):
            self.assertTrue(WEEKLY.skip_odoo_requested(cli=False))

    def test_env_false_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SKIP_ODOO", None)
            self.assertFalse(WEEKLY.skip_odoo_requested(cli=False))


class TestRunCatalogSyncSkipOdoo(unittest.TestCase):
    def test_skip_odoo_bypasses_scrape_and_odoo_xmlrpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "catalog_latest.json"
            catalog.write_text("[]", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(cmd: list[str]) -> int:
                calls.append(list(cmd))
                return 0

            with patch.dict(
                os.environ,
                {"ODOO_URL": "https://odoo.example", "ODOO_DB": "db"},
                clear=False,
            ):
                with patch.object(WEEKLY, "_run", side_effect=fake_run):
                    with patch.object(WEEKLY, "ROOT", Path(tmp)):
                        # catalog path is relative to ROOT; write under patched ROOT
                        (Path(tmp) / "data").mkdir(parents=True, exist_ok=True)
                        dest = Path(tmp) / "data" / "catalog_latest.json"
                        dest.write_text("[]", encoding="utf-8")
                        rc = WEEKLY.run_catalog_sync(
                            catalog="data/catalog_latest.json",
                            config_path="config.yaml",
                            dry_run=True,
                            accounts=None,
                            scrape=True,
                            skip_odoo=True,
                        )
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            cmd = calls[0]
            self.assertIn("run_sync.py", cmd[1])
            self.assertIn("--from-snapshot", cmd)
            self.assertIn("--skip-odoo", cmd)
            joined = " ".join(cmd)
            self.assertNotIn("sync_odoo_inventory.py", joined)
            self.assertNotIn("--scrape-only", joined)

    def test_skip_odoo_missing_catalog_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(WEEKLY, "ROOT", Path(tmp)):
                with patch.object(WEEKLY, "_run", side_effect=AssertionError("must not run")):
                    rc = WEEKLY.run_catalog_sync(
                        catalog="data/catalog_latest.json",
                        config_path="config.yaml",
                        dry_run=True,
                        accounts=None,
                        scrape=True,
                        skip_odoo=True,
                    )
            self.assertEqual(rc, 1)


class TestRunSyncSkipOdooCli(unittest.TestCase):
    def test_skip_odoo_loads_cached_catalog_without_scrape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "data" / "catalog_latest.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(
                json.dumps(
                    {
                        "count": 1,
                        "vehicles": [
                            {
                                "autosell_id": "obj1",
                                "slug": "obj1",
                                "title": "Test",
                                "brand": "Ford",
                                "year": "2020",
                                "price": "100000",
                                "mileage": "1 km",
                                "version": "",
                                "url": "https://www.autosell.mx/obj1",
                                "image_urls": ["https://example.com/a.jpg"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cfg = root / "config.yaml"
            cfg.write_text(
                "accounts:\n  - id: account_1\n    session_dir: sessions/account_1\n"
                "sync:\n  active_accounts: [account_1]\n  max_posts_per_account_per_run: 1\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["DRY_RUN"] = "true"
            env["DB_PATH"] = str(root / "data" / "sync.db")
            env["SNAPSHOT_DIR"] = str(root / "data" / "snapshots")
            env.pop("SKIP_ODOO", None)
            proc = __import__("subprocess").run(
                [
                    sys.executable,
                    str(ROOT / "run_sync.py"),
                    "--config",
                    str(cfg),
                    "--output",
                    str(catalog),
                    "--skip-odoo",
                    "--dry-run",
                    "--accounts",
                    "account_1",
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = proc.stdout + proc.stderr
            self.assertIn("SKIP_ODOO", out)
            self.assertIn("no live scrape", out)
            self.assertNotIn("Fetching public catalog", out)


if __name__ == "__main__":
    unittest.main()
