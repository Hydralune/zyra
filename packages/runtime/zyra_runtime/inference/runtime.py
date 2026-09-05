from __future__ import annotations

import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .graph import load_bundle
from .protocol import decode_tensors, encode_tensors, request_json, tensor_digest


SENSITIVITY = {"public": 0, "internal": 1, "sensitive": 2, "restricted": 3}


class PartitionedInferenceRuntime:
    """Choose actual executable partitions, then dispatch pure inference.

    The operator-owned configuration establishes the privacy floor and trusted
    endpoints. A model/tool caller cannot supply arbitrary endpoints or tokens.
    """
    def __init__(self, config_path: str | Path, *, token: str | None = None) -> None:
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        if self.config.get("schema") != "zyra.inference-config/v1":
            raise ValueError("unsupported inference configuration")
        self.token = token if token is not None else os.environ.get("ZYRA_INFERENCE_TOKEN", "")
        self.timeout = min(60.0, max(0.1, float(self.config.get("timeout_seconds", 15))))

    def infer(self, model_id: str, inputs: Mapping[str, Any], *, mode: str = "auto",
              sensitivity: str = "internal", latency_sla_ms: int = 30000,
              batch: bool = False, request_id: str | None = None) -> dict[str, Any]:
        import numpy as np

        if mode not in {"auto", "full", "split"}:
            raise ValueError("mode must be auto, full or split")
        floor = str(self.config.get("minimum_sensitivity", "internal"))
        if sensitivity not in SENSITIVITY or floor not in SENSITIVITY:
            raise ValueError("unknown sensitivity classification")
        sensitivity = max((floor, sensitivity), key=SENSITIVITY.__getitem__)
        model = self.config.get("models", {}).get(model_id)
        if not isinstance(model, dict):
            raise ValueError("model is not present in the operator-owned registry")
        bundle = (self.config_path.parent / model["bundle"]).resolve()
        manifest = load_bundle(bundle)
        if manifest["model_id"] != model_id:
            raise ValueError("model registry identity differs from its bundle")
        if set(inputs) != set(manifest["parts"]["full"]["inputs"]):
            raise ValueError("input names do not match the full model contract")
        if not inputs or any(not isinstance(v, np.ndarray) or v.dtype.kind not in "biuf" for v in inputs.values()):
            raise ValueError("inputs must contain numeric or boolean arrays")
        if batch and any(v.ndim == 0 for v in inputs.values()):
            raise ValueError("batched inputs require a leading sample dimension")
        count = len(next(iter(inputs.values()))) if batch else 1
        if not 1 <= count <= int(self.config.get("maximum_samples", 1000)):
            raise ValueError("sample count exceeds the configured execution budget")
        if batch and any(v.ndim == 0 or len(v) != count for v in inputs.values()):
            raise ValueError("batched inputs must share the leading sample dimension")
        if not 1 <= latency_sla_ms <= 3600000:
            raise ValueError("latency SLA is outside supported bounds")
        base_id = request_id or uuid.uuid4().hex
        started = time.perf_counter()

        def remaining_timeout():
            remaining = latency_sla_ms / 1000 - (time.perf_counter() - started)
            if remaining <= 0:
                raise TimeoutError("inference task exceeded its latency SLA")
            return min(self.timeout, remaining)

        observations, rejected = {}, []
        for name in ("device", "edge"):
            spec = self.config.get("nodes", {}).get(name)
            if not isinstance(spec, dict):
                rejected.append({"node": name, "reason": "node_not_configured"})
                continue
            try:
                begin = time.perf_counter()
                health = request_json(spec["url"].rstrip("/") + "/health", self.token, timeout=remaining_timeout())
                rtt = (time.perf_counter() - begin) * 1000
                if (health.get("source_sha256") != manifest["source_sha256"]
                        or health.get("identity", {}).get("location") != name):
                    raise ValueError("node/model identity differs from registry")
                observations[name] = {"health": health, "spec": spec, "rtt_ms": rtt}
            except Exception as exc:
                rejected.append({"node": name, "reason": "unavailable", "error_type": type(exc).__name__})
        candidates = []
        for route, stages in (("full", (("device", "full"),)),
                              ("split", (("device", "front"), ("edge", "back")))):
            reasons = []
            estimated = 0.0
            for name, part in stages:
                node = observations.get(name)
                if node is None:
                    reasons.append(f"{name}:unavailable")
                    continue
                health, spec = node["health"], node["spec"]
                advertised = health.get("parts", {}).get(part)
                if not advertised or advertised.get("sha256") != manifest["parts"][part]["sha256"]:
                    reasons.append(f"{name}:{part}:model_or_capacity_unavailable")
                allowed = spec.get("allowed_sensitivity", ["public"])
                if sensitivity not in allowed or (sensitivity == "restricted" and name != "device"):
                    reasons.append(f"{name}:sensitivity_not_allowed")
                compute = health.get("last_run_ms", {}).get(part, spec.get("cold_compute_estimate_ms", 10))
                if not math.isfinite(float(compute)) or float(compute) < 0:
                    reasons.append(f"{name}:invalid_compute_estimate")
                    compute = 0
                estimated += node["rtt_ms"] + float(compute)
            estimated *= count
            if estimated > latency_sla_ms:
                reasons.append("estimated_latency_exceeds_sla")
            if mode != "auto" and mode != route:
                reasons.append("explicit_mode_excludes_route")
            candidates.append({"route": route, "admitted": not reasons,
                               "estimated_latency_ms": estimated, "reasons": reasons})
        viable = [c for c in candidates if c["admitted"]]
        if not viable:
            raise ValueError("no admissible inference route: " + json.dumps(candidates, separators=(",", ":")))
        selected = min(viable, key=lambda c: (c["estimated_latency_ms"], c["route"] != "full"))
        route = selected["route"]
        receipts, sample_outputs, failures = [], [], []

        def call(name: str, part: str, values: Mapping[str, Any], suffix: str) -> dict[str, Any]:
            if (time.perf_counter() - started) * 1000 > latency_sla_ms:
                raise TimeoutError("inference task exceeded its latency SLA")
            wanted = manifest["parts"][part]
            payload = {"request_id": f"{base_id}:{suffix}:{part}", "part": part,
                       "model_sha256": wanted["sha256"], "tensors": encode_tensors(values)}
            response = request_json(observations[name]["spec"]["url"].rstrip("/") + "/infer",
                                    self.token, payload, timeout=remaining_timeout())
            result = decode_tensors(response["tensors"])
            receipt = response["receipt"]
            identity = observations[name]["health"]["identity"]
            if (receipt.get("request_id") != payload["request_id"] or receipt.get("identity") != identity
                    or receipt.get("model_sha256") != wanted["sha256"]
                    or receipt.get("source_sha256") != manifest["source_sha256"]
                    or receipt.get("part") != part or receipt.get("simulated") is not False
                    or receipt.get("input_sha256") != tensor_digest(values)
                    or receipt.get("output_sha256") != tensor_digest(result)
                    or set(result) != set(wanted["outputs"])):
                raise ValueError("inference receipt does not match the dispatched computation")
            receipts.append(receipt)
            return result

        for index in range(count):
            values = {k: v[index:index + 1] for k, v in inputs.items()} if batch else dict(inputs)
            try:
                if route == "split":
                    front = call("device", "front", {k: values[k] for k in manifest["parts"]["front"]["inputs"]}, str(index))
                    back = {**front, **{k: values[k] for k in manifest["raw_inputs_required_by_back"]}}
                    output = call("edge", "back", back, str(index))
                else:
                    output = call("device", "full", values, str(index))
            except Exception as exc:
                full_candidate = next(c for c in candidates if c["route"] == "full")
                if mode != "auto" or route != "split" or not full_candidate["admitted"]:
                    raise
                failures.append({"sample": index, "route": route, "error_type": type(exc).__name__,
                                 "recovery": "full_device_recompute_pure_inference"})
                output = call("device", "full", values, f"{index}:recovery")
                route = "full"
            sample_outputs.append(output)
        outputs = {k: np.concatenate([o[k] for o in sample_outputs], axis=0) if batch else sample_outputs[0][k]
                   for k in manifest["parts"]["full"]["outputs"]}
        identities = [r["identity"] for r in receipts]
        remaining_timeout()
        return {
            "schema": "zyra.partitioned-inference/v1", "request_id": base_id,
            "model_id": model_id, "source_sha256": manifest["source_sha256"],
            "selected_route": selected["route"], "final_route": route, "sensitivity": sensitivity,
            "candidates": candidates, "unavailable_nodes": rejected, "recoveries": failures,
            "sample_count": count, "stage_execution_count": len(receipts),
            "inference_request_count": 1, "agent_reasoning_step_count": 0,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "input_sha256": tensor_digest(inputs), "output_sha256": tensor_digest(outputs),
            "distinct_process_count": len({(i["hostname"], i["pid"], i["generation"]) for i in identities}),
            "distinct_reported_host_count": len({i["hostname"] for i in identities}),
            "host_identity_scope": "authenticated_node_report_not_hardware_attestation",
            "stage_receipts": receipts, "outputs": {k: v.tolist() for k, v in outputs.items()},
        }
