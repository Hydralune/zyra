"""Read-only independent judge for a completed real Zyra/DeepSeek task."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np
import onnxruntime as ort

from zyra_runtime.inference.graph import load_bundle, sha256
from zyra_runtime.inference.protocol import tensor_digest


def verify(root: Path, attempt: str):
    run = root / attempt
    task = run / "task"
    execution = json.loads((run / "execution.json").read_text(encoding="utf-8"))
    assert execution["cli_exit_code"] == 0, "CLI did not complete successfully"
    assert not (run / "intervention.json").exists(), "final acceptance must have no operator intervention"
    cli = [json.loads(line) for line in (run / "cli.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    final = [item for item in cli if item.get("type") == "result"][-1]
    assert final["ok"] is True and final["exit_code"] == 0
    for name in ("images.npz", "labels.json", "input-metadata.json"):
        assert (task / name).read_bytes() == (root / "task" / name).read_bytes(), f"input changed: {name}"
    acceptance_source = Path(__file__).with_name("inference_task_acceptance.py")
    assert (task / acceptance_source.name).read_bytes() == acceptance_source.read_bytes(), "acceptance harness changed"
    with np.load(task / "images.npz", allow_pickle=False) as archive:
        inputs = {k: archive[k] for k in archive.files}
    labels = np.array(json.loads((task / "labels.json").read_text(encoding="utf-8")))
    manifest = load_bundle(root / "bundle")
    report = json.loads((task / "inference.json").read_text(encoding="utf-8"))
    input_metadata = json.loads((root / "task/input-metadata.json").read_text(encoding="utf-8"))
    assert manifest["source_sha256"] == input_metadata["sources"]["mnist-12.onnx"]["sha256"]
    assert report["source_sha256"] == sha256(root / "mnist-12.onnx") == manifest["source_sha256"]
    assert report["input_sha256"] == tensor_digest(inputs)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    source = ort.InferenceSession(str(root / "mnist-12.onnx"), options, providers=["CPUExecutionProvider"])
    expected_scores = np.concatenate([source.run(None, {"Input3": sample[None]})[0] for sample in inputs["Input3"]])
    actual = np.array(report["outputs"]["Plus214_Output_0"], dtype=np.float32)
    np.testing.assert_allclose(actual, expected_scores, atol=1e-5, rtol=1e-5)
    assert report["output_sha256"] == tensor_digest({"Plus214_Output_0": actual})
    expected = expected_scores.argmax(axis=1)
    with (task / "predictions.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(labels)
    np.testing.assert_array_equal([int(r["sample_index"]) for r in rows], np.arange(len(labels)))
    np.testing.assert_array_equal([int(r["prediction"]) for r in rows], expected)
    np.testing.assert_array_equal([int(r["label"]) for r in rows], labels)
    correct = int(np.count_nonzero(expected == labels))
    wrong = [{"sample_index": i, "prediction": int(p), "label": int(y)}
             for i, (p, y) in enumerate(zip(expected, labels, strict=True)) if p != y]
    metrics = json.loads((task / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["sample_count"] == len(labels) and metrics["correct"] == correct
    assert math.isclose(metrics["accuracy"], correct / len(labels), abs_tol=1e-6)
    assert metrics["mismatches"] == wrong
    receipts = report["stage_receipts"]
    identities = json.loads((run / "nodes/processes.json").read_text(encoding="utf-8"))
    assert len(receipts) == 2 * len(labels) == report["stage_execution_count"]
    assert report["selected_route"] == report["final_route"] == "split"
    for i in range(len(labels)):
        front, back = receipts[2*i:2*i+2]
        for r, part, identity in ((front, "front", identities[0]), (back, "back", identities[1])):
            assert r["identity"] == identity and r["part"] == part and r["simulated"] is False
            assert r["model_sha256"] == manifest["parts"][part]["sha256"]
            assert r["request_id"] == f"{report['request_id']}:{i}:{part}"
        assert front["input_sha256"] == tensor_digest({"Input3": inputs["Input3"][i:i+1]})
        assert front["output_sha256"] == back["input_sha256"]
        assert back["output_sha256"] == tensor_digest({"Plus214_Output_0": actual[i:i+1]})
    for key in ("stage_execution_count", "distinct_process_count", "distinct_reported_host_count"):
        assert metrics[key] == report[key]
    assert report["distinct_process_count"] == 2 and report["distinct_reported_host_count"] == 1

    # Read only execution/usage tables. Never read credentials, request headers
    # or owner tokens, and never export raw provider text or thinking frames.
    database = run / "state/artifacts/.provider-control-plane/provider.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        attempts = connection.execute("SELECT dispatch_id,outcome,json FROM provider_dispatch_attempts").fetchall()
        lifecycle = [json.loads(row[0]) for row in connection.execute("SELECT json FROM provider_dispatch_lifecycle")]
        routes = connection.execute("SELECT DISTINCT provider_id,model_id FROM provider_route_leases").fetchall()
    assert routes and all(p == "deepseek" for p, _ in routes), "a different provider was used"
    usage = Counter()
    request_receipts = []
    for item in lifecycle:
        result = item.get("result") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            usage[key] += int(result.get("usage", {}).get(key, 0))
        request_receipts.append({"dispatch_id": item["dispatchId"], "state": item["state"],
                                 "provider_id": result.get("providerId"), "model_id": result.get("modelId"),
                                 "total_tokens": result.get("usage", {}).get("total_tokens", 0)})
    assert usage["total_tokens"] > 0
    checkpoints = []
    for path in (run / "state/artifacts/.runtime-checkpoints").glob("*.json"):
        if path.name.endswith("handoff.json"):
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("modelIteration"):
            checkpoints.append(value)
    tools = {(c["worker_request_id"], t["callId"]): t for c in checkpoints for t in c["modelIteration"]["tools"]}
    inference_effects = []
    for checkpoint in checkpoints:
        for call_id, value in checkpoint.get("tool_effect_receipts", {}).items():
            result = value.get("result", {})
            if result.get("output", {}).get("model_id") == "mnist-cnn":
                assert result["ok"] is True
                assert result["metadata"]["permission_execution_grant_consumed"] == "true"
                inference_effects.append({"call_id": call_id, "permission_grant_consumed": True})
    assert inference_effects, "missing real permitted model-tool execution receipt"
    assert any("unittest" in json.dumps(t.get("arguments", {})) and t.get("state") == "succeeded" for t in tools.values())
    result = {"schema": "zyra.deepseek-inference-acceptance/v1", "passed": True,
              "task_id": final.get("task_id"), "cli_exit_code": 0, "operator_intervention": False,
              "sample_count": len(labels), "correct": correct, "accuracy": correct / len(labels), "mismatches": wrong,
              "max_abs_error_against_original_model": float(np.abs(actual - expected_scores).max()),
              "stage_execution_count": len(receipts), "distinct_process_count": 2, "distinct_reported_host_count": 1,
              "provider_request_count": len(lifecycle), "provider_attempt_count": len(attempts),
              "provider_http_statuses": dict(Counter(str(json.loads(r[2]).get("httpStatus")) for r in attempts)),
              "provider_models": [{"provider_id": p, "model_id": m} for p, m in routes], "usage": dict(usage),
              "provider_request_receipts": request_receipts,
              "agent_tool_calls_with_persisted_result": len(tools),
              "agent_tool_result_states": dict(Counter(t["state"] for t in tools.values())),
              "model_tool_effects": inference_effects,
              "scope": "one real domain task; model requests and tool calls are not thousand-step reasoning evidence",
              "delivery_sha256": {name: hashlib.sha256((task / name).read_bytes()).hexdigest()
                                  for name in ("predictions.csv", "metrics.json", "REPORT.md", "analyze.py", "inference.json")}}
    (run / "independent-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: v for k, v in result.items() if k not in ("provider_request_receipts", "delivery_sha256")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.root.resolve(), args.attempt), ensure_ascii=False, indent=2))
