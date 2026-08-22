"""Validate GitHub Actions workflow YAML (timeouts + age inputs)."""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _on(payload: dict) -> dict:
    return payload.get("on") or payload.get(True) or {}


class TestWorkflowTimeouts(unittest.TestCase):
    def test_every_job_timeout_is_90(self):
        files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
        self.assertTrue(files, "no workflow files found")
        for path in files:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            jobs = payload.get("jobs") or {}
            self.assertTrue(jobs, f"{path.name}: no jobs")
            for name, job in jobs.items():
                self.assertEqual(
                    job.get("timeout-minutes"),
                    90,
                    f"{path.name} job {name!r} timeout-minutes is not 90",
                )


class TestRepostAgeInputs(unittest.TestCase):
    def test_repost_accepts_min_age_and_older_than(self):
        payload = yaml.safe_load(
            (WORKFLOWS / "repost.yml").read_text(encoding="utf-8")
        )
        inputs = _on(payload)["workflow_dispatch"]["inputs"]
        self.assertIn("min_age_days", inputs)
        self.assertEqual(str(inputs["min_age_days"].get("default")), "2")
        self.assertIn("older_than", inputs)
        self.assertIn("force", inputs)

    def test_sync_accepts_min_age_and_older_than(self):
        payload = yaml.safe_load((WORKFLOWS / "sync.yml").read_text(encoding="utf-8"))
        inputs = _on(payload)["workflow_dispatch"]["inputs"]
        self.assertIn("min_age_days", inputs)
        self.assertIn("older_than", inputs)
        self.assertIn("force", inputs)


if __name__ == "__main__":
    unittest.main()
