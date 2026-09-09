from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "paper"
    / "experiments"
    / "formal"
    / "summarize_zyra_run.py"
)
SPEC = importlib.util.spec_from_file_location("summarize_zyra_run", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FormalExperimentEvidenceTests(unittest.TestCase):
    def test_transport_failure_preserves_canonical_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "run-metadata.json").write_text(
                json.dumps({
                    "instance_id": "example__repo-1",
                    "system": "ZYRA",
                    "analysis_class": "calibration",
                    "task_id": "task-1",
                    "task_status": "failed",
                    "started_at": "2026-09-09T00:00:00+00:00",
                    "ended_at": "2026-09-09T00:45:00+00:00",
                }),
                encoding="utf-8",
            )
            (run_dir / "task-run.json").write_text(
                json.dumps({
                    "schema": "zyra.swebench-task-run-envelope/v1",
                    "task": {"task_id": "task-1", "run_id": "run-1", "status": "failed"},
                    "canonical_task_record_observed": True,
                    "run_transport": {"ok": False, "error": "HTTP 503"},
                }),
                encoding="utf-8",
            )

            summary = MODULE.summarize(run_dir)

        self.assertEqual(summary["task_status"], "failed")
        self.assertFalse(summary["strict_success"])
        self.assertEqual(summary["elapsed_seconds"], 2700.0)
        self.assertEqual(summary["evidence_integrity"], {
            "task_run_present": True,
            "canonical_task_record_observed": True,
            "terminal_state_observed": True,
            "run_transport_ok": False,
            "run_transport_error": "HTTP 503",
        })

    def test_missing_canonical_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "run-metadata.json").write_text(
                json.dumps({
                    "instance_id": "example__repo-2",
                    "system": "ZYRA",
                    "analysis_class": "calibration",
                    "task_status": "unknown",
                }),
                encoding="utf-8",
            )
            (run_dir / "task-run.json").write_text(
                json.dumps({
                    "schema": "zyra.swebench-task-run-envelope/v1",
                    "task": {"task_id": "", "run_id": "", "status": "unknown"},
                    "canonical_task_record_observed": False,
                    "run_transport": {"ok": False, "error": "API unavailable"},
                }),
                encoding="utf-8",
            )

            summary = MODULE.summarize(run_dir)

        self.assertFalse(summary["strict_success"])
        self.assertFalse(summary["evidence_integrity"]["canonical_task_record_observed"])
        self.assertFalse(summary["evidence_integrity"]["terminal_state_observed"])


if __name__ == "__main__":
    unittest.main()
