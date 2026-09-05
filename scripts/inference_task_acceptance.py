"""Task-provided acceptance: python -B -m unittest -v inference_task_acceptance.

Copied into the task before submission. The production algorithm is authored
by the Agent; these tests check its public behavior and a changed input case.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class InferenceTaskAcceptance(unittest.TestCase):
    def test_delivered_predictions_and_metrics_match_real_outputs(self):
        report = json.loads((ROOT / "inference.json").read_text(encoding="utf-8"))
        labels = json.loads((ROOT / "labels.json").read_text(encoding="utf-8"))
        scores = report["outputs"]["Plus214_Output_0"]
        expected = [max(range(10), key=row.__getitem__) for row in scores]
        with (ROOT / "predictions.csv").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, ["sample_index", "prediction", "label"])
            rows = list(reader)
        self.assertEqual(len(rows), len(labels))
        self.assertEqual([int(r["sample_index"]) for r in rows], list(range(len(labels))))
        self.assertEqual([int(r["prediction"]) for r in rows], expected)
        self.assertEqual([int(r["label"]) for r in rows], labels)
        wrong = [{"sample_index": i, "prediction": p, "label": y}
                 for i, (p, y) in enumerate(zip(expected, labels, strict=True)) if p != y]
        metrics = json.loads((ROOT / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["sample_count"], len(labels))
        self.assertEqual(metrics["correct"], len(labels) - len(wrong))
        self.assertAlmostEqual(metrics["accuracy"], (len(labels) - len(wrong)) / len(labels), places=6)
        self.assertEqual(metrics["mismatches"], wrong)

    def test_analysis_recomputes_when_logits_and_labels_change(self):
        # A distinct small fixture verifies algorithm behavior, not inference
        # accuracy. The separate real-data judge validates the model itself.
        report = json.loads((ROOT / "inference.json").read_text(encoding="utf-8"))
        classes = [2, 7, 0, 9, 3]
        labels = [2, 1, 0, 9, 4]
        report["outputs"] = {"Plus214_Output_0": [[10.0 if j == c else -2.0 for j in range(10)] for c in classes]}
        report.update(sample_count=5, stage_execution_count=10)
        with tempfile.TemporaryDirectory(prefix="acceptance-", dir=ROOT) as temp:
            root = Path(temp)
            shutil.copyfile(ROOT / "analyze.py", root / "analyze.py")
            shutil.copyfile(ROOT / "input-metadata.json", root / "input-metadata.json")
            (root / "inference.json").write_text(json.dumps(report), encoding="utf-8")
            (root / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "analyze.py"], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "predictions.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([int(r["prediction"]) for r in rows], classes)
            metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["correct"], 3)
            self.assertAlmostEqual(metrics["accuracy"], .6, places=6)

    def test_receipts_and_report_are_present_and_honest(self):
        report = json.loads((ROOT / "inference.json").read_text(encoding="utf-8"))
        self.assertEqual(report["selected_route"], "split")
        receipts = report["stage_receipts"]
        self.assertEqual(len(receipts), 2 * report["sample_count"])
        self.assertEqual({r["part"] for r in receipts}, {"front", "back"})
        self.assertTrue(all(r["simulated"] is False for r in receipts))
        self.assertEqual(report["distinct_process_count"], 2)
        self.assertEqual(report["distinct_reported_host_count"], 1)
        self.assertEqual(report["agent_reasoning_step_count"], 0)
        self.assertTrue((ROOT / "REPORT.md").read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
