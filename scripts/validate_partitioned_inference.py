"""Independently compare real public model partitions against the source ONNX."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort

from zyra_runtime.inference.local import LocalInferenceCluster
from zyra_runtime.inference.runtime import PartitionedInferenceRuntime


def validate(root: Path):
    with np.load(root / "task/images.npz", allow_pickle=False) as archive:
        inputs = {k: archive[k] for k in archive.files}
    labels = np.array(json.loads((root / "task/labels.json").read_text()))
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    source = ort.InferenceSession(str(root / "mnist-12.onnx"), options, providers=["CPUExecutionProvider"])
    # Judge uses the original downloaded model, never either generated partition.
    reference = np.concatenate([source.run(None, {"Input3": sample[None]})[0] for sample in inputs["Input3"]])
    with LocalInferenceCluster(root / "bundle", root / "numeric-validation", constrain_device=True) as cluster:
        result = PartitionedInferenceRuntime(cluster.config_path, token=cluster.token).infer(
            "mnist-cnn", inputs, batch=True, sensitivity="public", latency_sla_ms=600000)
    actual = np.array(next(iter(result["outputs"].values())))
    np.testing.assert_allclose(actual, reference, atol=1e-5, rtol=1e-5)
    np.testing.assert_array_equal(actual.argmax(axis=1), reference.argmax(axis=1))
    correct = int(np.count_nonzero(actual.argmax(axis=1) == labels))
    assert correct / len(labels) >= .9, "public test-set accuracy unexpectedly low"
    assert result["selected_route"] == "split" and result["distinct_process_count"] == 2
    report = {"schema": "zyra.real-model-validation/v1", "passed": True,
              "sample_count": len(labels), "correct": correct, "accuracy": correct / len(labels),
              "max_abs_error_against_original_model": float(np.abs(actual - reference).max()),
              "absolute_tolerance": 1e-5, "relative_tolerance": 1e-5,
              "stage_execution_count": result["stage_execution_count"],
              "distinct_process_count": result["distinct_process_count"],
              "distinct_reported_host_count": result["distinct_reported_host_count"],
              "agent_reasoning_steps": 0, "scope": "real model/data; local processes; no provider involved"}
    (root / "numeric-validation/receipts.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (root / "numeric-validation/verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.root.resolve()), indent=2))
