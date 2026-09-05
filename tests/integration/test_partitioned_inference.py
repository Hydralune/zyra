from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError

import psutil
import pytest

np = pytest.importorskip("numpy")
onnx = pytest.importorskip("onnx")
ort = pytest.importorskip("onnxruntime")

from zyra_runtime import ToolCall, ToolExecutionContext, ToolExecutor
from zyra_runtime.inference.graph import load_bundle, partition_model
from zyra_runtime.inference.protocol import decode_tensors, encode_tensors, request_json
from zyra_runtime.inference.runtime import PartitionedInferenceRuntime


TOKEN = "integration-test-inference-token-0123456789"


@pytest.fixture
def bundle(tmp_path):
    h = onnx.helper
    tensor = onnx.TensorProto
    # A skip connection crosses the cut through the original input, testing
    # a real partition-boundary dependency that a single activation can miss.
    graph = h.make_graph([
        h.make_node("MatMul", ["x", "w"], ["z"]),
        h.make_node("Relu", ["z"], ["h"]),
        h.make_node("Add", ["h", "x"], ["skip"]),
        h.make_node("MatMul", ["skip", "w2"], ["y"]),
    ], "skip-connection", [h.make_tensor_value_info("x", tensor.FLOAT, [1, 3])],
        [h.make_tensor_value_info("y", tensor.FLOAT, [1, 3])],
        [onnx.numpy_helper.from_array(np.array([[1, -2, 3], [2, 1, -1], [-3, 2, 1]], dtype=np.float32), "w"),
         onnx.numpy_helper.from_array(np.eye(3, dtype=np.float32), "w2")])
    model = h.make_model(graph, opset_imports=[h.make_opsetid("", 13)], ir_version=10)
    source = tmp_path / "model.onnx"
    onnx.save(model, source)
    destination = tmp_path / "bundle"
    manifest = partition_model(source, destination, cut_after=1, model_id="skip")
    return destination, manifest


@contextmanager
def nodes(tmp_path, bundle_path, *, device_parts="full,front", edge=True, limit=None):
    processes = []
    config = {"schema": "zyra.inference-config/v1", "minimum_sensitivity": "internal",
              "maximum_samples": 100, "timeout_seconds": 1,
              "models": {"skip": {"bundle": str(bundle_path)}}, "nodes": {}}
    try:
        for location, parts in (("device", device_parts), ("edge", "back")):
            if location == "edge" and not edge:
                continue
            command = [sys.executable, "-m", "zyra_runtime.inference.node", "--bundle", str(bundle_path),
                       "--node-id", f"test-{location}", "--location", location, "--parts", parts]
            if location == "device" and limit is not None:
                command += ["--max-model-bytes", str(limit)]
            environment = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
            environment["ZYRA_INFERENCE_TOKEN"] = TOKEN
            process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            processes.append(process)
            line = process.stdout.readline()
            assert line, process.stderr.read()
            ready = json.loads(line)
            process.node_identity = ready["identity"]
            assert ready["identity"]["pid"] in {
                process.pid, *[p.pid for p in psutil.Process(process.pid).children(recursive=True)]
            }
            config["nodes"][location] = {"url": f"http://127.0.0.1:{ready['port']}",
                "allowed_sensitivity": ["public", "internal", "sensitive", "restricted"],
                "cold_compute_estimate_ms": 1}
        path = tmp_path / "inference.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        yield path, config, processes
    finally:
        for process in processes:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()


def test_real_process_partitions_preserve_skip_input_and_match_full_model(tmp_path, bundle):
    path, manifest = bundle
    assert manifest["raw_inputs_required_by_back"] == ["x"]
    assert manifest["boundary_tensors"] == ["h"]
    values = {"x": np.array([[1, -2, 3], [0.1, 0.2, 0.3]], dtype=np.float32)}
    full = ort.InferenceSession(str(path / "full.onnx"), providers=["CPUExecutionProvider"])
    expected = np.concatenate([full.run(None, {"x": v[None]})[0] for v in values["x"]])
    with nodes(tmp_path, path) as (config, _, processes):
        result = PartitionedInferenceRuntime(config, token=TOKEN).infer("skip", values, mode="split", batch=True)
    np.testing.assert_allclose(result["outputs"]["y"], expected, atol=1e-6)
    assert result["stage_execution_count"] == 4
    assert result["distinct_process_count"] == 2
    assert result["distinct_reported_host_count"] == 1
    assert {r["identity"]["pid"] for r in result["stage_receipts"]} == {p.node_identity["pid"] for p in processes}
    assert result["agent_reasoning_step_count"] == 0
    assert result["inference_request_count"] == 1
    assert "agent_tool_call_count" not in result


def test_capacity_constraint_changes_auto_route_to_split(tmp_path, bundle):
    path, manifest = bundle
    limit = manifest["parts"]["full"]["bytes"] - 1
    assert manifest["parts"]["front"]["bytes"] <= limit
    with nodes(tmp_path, path, limit=limit) as (config, _, _):
        result = PartitionedInferenceRuntime(config, token=TOKEN).infer("skip", {"x": np.ones((1, 3), np.float32)})
    assert result["selected_route"] == "split"
    assert "device:full:model_or_capacity_unavailable" in result["candidates"][0]["reasons"]


def test_missing_edge_has_explicit_auto_full_and_forced_split_rejection(tmp_path, bundle):
    with nodes(tmp_path, bundle[0], edge=False) as (config, _, _):
        runtime = PartitionedInferenceRuntime(config, token=TOKEN)
        values = {"x": np.ones((1, 3), np.float32)}
        result = runtime.infer("skip", values)
        assert result["selected_route"] == "full"
        assert result["unavailable_nodes"] == [{"node": "edge", "reason": "node_not_configured"}]
        with pytest.raises(ValueError, match="no admissible"):
            runtime.infer("skip", values, mode="split")


def test_privacy_floor_cannot_be_downgraded_and_restricted_stays_device(tmp_path, bundle):
    with nodes(tmp_path, bundle[0]) as (path, config, _):
        config["nodes"]["edge"]["allowed_sensitivity"] = ["public"]
        path.write_text(json.dumps(config), encoding="utf-8")
        runtime = PartitionedInferenceRuntime(path, token=TOKEN)
        values = {"x": np.ones((1, 3), np.float32)}
        result = runtime.infer("skip", values, sensitivity="public")
        assert result["sensitivity"] == "internal"
        assert result["selected_route"] == "full"
        with pytest.raises(ValueError, match="sensitivity_not_allowed"):
            runtime.infer("skip", values, mode="split", sensitivity="restricted")


def test_node_rejects_bad_authentication_and_wrong_model_identity(tmp_path, bundle):
    with nodes(tmp_path, bundle[0]) as (_, config, _):
        url = config["nodes"]["device"]["url"]
        with pytest.raises(HTTPError) as error:
            request_json(url + "/health", "wrong-token" * 4)
        assert error.value.code == 401
        with pytest.raises(HTTPError) as error:
            request_json(url + "/infer", TOKEN, {"part": "front", "model_sha256": "wrong", "request_id": "test",
                                                "tensors": encode_tensors({"x": np.ones((1, 3), np.float32)})})
        assert error.value.code == 400


def test_corrupt_bundle_and_unsafe_tensor_types_are_rejected(bundle):
    path, _ = bundle
    with (path / "back.onnx").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_bundle(path)
    with pytest.raises(ValueError, match="numeric"):
        encode_tensors({"x": np.array([object()], dtype=object)})
    roundtrip = decode_tensors(encode_tensors({"x": np.array([1, 2], dtype=np.int64)}))
    np.testing.assert_array_equal(roundtrip["x"], [1, 2])


def test_inference_tool_cannot_execute_without_one_use_permission_grant(tmp_path):
    context = ToolExecutionContext.for_workspace(tmp_path / "workspace", tmp_path / "artifacts")
    result = ToolExecutor(context).execute(ToolCall(run_id="run", task_id="task", tool_name="model_inference",
                                                   arguments={"model_id": "skip", "path": "input.npz"}))
    assert not result.ok
    assert result.error == "permission_required"


def test_remote_plaintext_endpoint_is_rejected():
    with pytest.raises(ValueError, match="require HTTPS"):
        request_json("http://192.0.2.1:9000/health", TOKEN)


def test_edge_dies_after_admission_and_auto_recomputes_on_device(tmp_path, bundle, monkeypatch):
    import zyra_runtime.inference.runtime as runtime_module

    with nodes(tmp_path, bundle[0]) as (path, config, processes):
        config["nodes"]["device"]["cold_compute_estimate_ms"] = 1000
        path.write_text(json.dumps(config), encoding="utf-8")
        values = {"x": np.ones((1, 3), np.float32)}
        # A real front run supplies a measured estimate; full remains cold.
        request_json(config["nodes"]["device"]["url"] + "/infer", TOKEN,
                     {"part": "front", "model_sha256": bundle[1]["parts"]["front"]["sha256"],
                      "request_id": "warm", "tensors": encode_tensors(values)})
        killed = []

        def kill_during_dispatch(url, token, payload=None, **kwargs):
            if payload and url.startswith(config["nodes"]["edge"]["url"]) and not killed:
                edge_pid = processes[1].node_identity["pid"]
                victim = psutil.Process(edge_pid)
                victim.terminate()
                victim.wait(timeout=10)
                killed.append(edge_pid)
            return request_json(url, token, payload, **kwargs)

        monkeypatch.setattr(runtime_module, "request_json", kill_during_dispatch)
        result = PartitionedInferenceRuntime(path, token=TOKEN).infer("skip", values)
    assert killed
    assert result["selected_route"] == "split"
    assert result["final_route"] == "full"
    assert result["recoveries"][0]["recovery"] == "full_device_recompute_pure_inference"
    assert [r["part"] for r in result["stage_receipts"]] == ["front", "full"]
    np.testing.assert_allclose(result["outputs"]["y"], [[1, 2, 4]])


def test_changed_node_generation_is_not_accepted_as_old_execution(tmp_path, bundle, monkeypatch):
    import zyra_runtime.inference.runtime as runtime_module

    with nodes(tmp_path, bundle[0]) as (path, _, _):
        def changed_receipt(url, token, payload=None, **kwargs):
            response = request_json(url, token, payload, **kwargs)
            if payload:
                response["receipt"]["identity"]["generation"] = "different-generation"
            return response

        monkeypatch.setattr(runtime_module, "request_json", changed_receipt)
        with pytest.raises(ValueError, match="receipt does not match"):
            PartitionedInferenceRuntime(path, token=TOKEN).infer("skip", {"x": np.ones((1, 3), np.float32)}, mode="split")


def test_estimated_latency_can_reject_both_routes(tmp_path, bundle):
    with nodes(tmp_path, bundle[0]) as (path, config, _):
        for spec in config["nodes"].values():
            spec["cold_compute_estimate_ms"] = 1000
        path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(ValueError, match="estimated_latency_exceeds_sla"):
            PartitionedInferenceRuntime(path, token=TOKEN).infer("skip", {"x": np.ones((1, 3), np.float32)}, latency_sla_ms=100)


def test_model_tool_never_bypasses_required_workspace_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("ZYRA_INFERENCE_CONFIG", str(tmp_path / "unused.json"))
    context = ToolExecutionContext.for_workspace(tmp_path / "workspace", tmp_path / "artifacts",
                                                runtime_services={"workspace_gateway_required": True})
    result = ToolExecutor(context)._model_inference(ToolCall(run_id="r", task_id="t", tool_name="model_inference",
                                                            arguments={"model_id": "skip", "path": "input.npz"}), authorized=True)
    assert result.error == "workspace_gateway_unavailable"


def test_capacity_manifest_cannot_underreport_actual_model_bytes(bundle):
    path, manifest = bundle
    manifest["parts"]["full"]["bytes"] = 1
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="byte size mismatch"):
        load_bundle(path)
