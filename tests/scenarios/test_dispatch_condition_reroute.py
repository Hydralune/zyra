from __future__ import annotations

import json

import psutil

from zyra_evaluation.policy_benchmark import (
    build_redacted_dispatch_evidence_index,
    evaluate_physical_dispatch_gate,
)

from tests.integration.physical_dispatch_harness import (
    ROOT,
    build_physical_harness,
    read_env_file,
)


def _execute_and_close(harness):
    try:
        result = harness.execute()
        return result, harness.physical_port.receipts[-1]
    finally:
        harness.close()


def test_same_minimal_task_really_reroutes_across_frozen_conditions(
    tmp_path,
) -> None:
    environment = read_env_file(ROOT / ".env.glm.local")
    assert environment.get("ZAI_API_KEY"), (
        "condition-switch cloud lane is unclosed without a real credential"
    )
    lanes = []

    _result, receipt = _execute_and_close(
        build_physical_harness(
            tmp_path / "normal",
            location="cloud",
            environment=environment,
            condition="normal",
            condition_signals={"profile": "cloud-verification"},
        )
    )
    lanes.append(("normal", receipt))

    _result, receipt = _execute_and_close(
        build_physical_harness(
            tmp_path / "privacy",
            location="local",
            privacy_class="local-only",
            allowed_placements=("local",),
            condition="privacy_local_only",
            condition_signals={"privacy_class": "local-only"},
        )
    )
    lanes.append(("privacy_local_only", receipt))

    _result, receipt = _execute_and_close(
        build_physical_harness(
            tmp_path / "load",
            location="edge",
            fallback_locations=("local",),
            prefer_selected_location=False,
            logical_manifest_overrides={
                "local": {"current_load": 3, "latency_ms": 10},
                "edge": {"current_load": 0, "latency_ms": 30},
            },
            condition="local_load",
            condition_signals={
                "local_current_load": 3,
                "edge_current_load": 0,
            },
        )
    )
    assert receipt.physical_identity["location"] == "edge"
    lanes.append(("local_load", receipt))

    edge_health = build_physical_harness(
        tmp_path / "health",
        location="edge",
        fallback_locations=("local",),
        condition="edge_health",
        condition_signals={"edge_health": "degraded"},
    )
    edge_health.clients["edge"].inject_fault(
        {"kind": "network-loss", "enabled": True}
    )
    _result, receipt = _execute_and_close(edge_health)
    assert receipt.physical_identity["location"] == "local"
    assert receipt.recovery_evidence
    lanes.append(("edge_health", receipt))

    edge_disconnect = build_physical_harness(
        tmp_path / "disconnect",
        location="edge",
        fallback_locations=("local",),
        condition="edge_disconnect",
        condition_signals={"edge_network": "disconnected"},
    )
    edge_pid = int(edge_disconnect.clients["edge"].health()["pid"])
    edge_process = psutil.Process(edge_pid)
    edge_process.kill()
    edge_process.wait(timeout=10)
    _result, receipt = _execute_and_close(edge_disconnect)
    assert receipt.physical_identity["location"] == "local"
    assert receipt.recovery_evidence
    lanes.append(("edge_disconnect", receipt))

    _result, receipt = _execute_and_close(
        build_physical_harness(
            tmp_path / "cloud-unavailable",
            location="cloud",
            fallback_locations=("local",),
            environment={"ZAI_API_KEY": ""},
            condition="cloud_unavailable",
            condition_signals={"cloud_credential_ready": False},
        )
    )
    assert receipt.physical_identity["location"] == "local"
    assert receipt.recovery_evidence
    lanes.append(("cloud_unavailable", receipt))

    _result, receipt = _execute_and_close(
        build_physical_harness(
            tmp_path / "latency-cost",
            location="edge",
            fallback_locations=("local",),
            prefer_selected_location=False,
            logical_manifest_overrides={
                "local": {
                    "latency_ms": 500,
                    "cost_per_1k_tokens": 0.1,
                },
                "edge": {
                    "latency_ms": 5,
                    "cost_per_1k_tokens": 0,
                },
            },
            condition="latency_cost",
            condition_signals={
                "local_latency_ms": 500,
                "edge_latency_ms": 5,
                "maximum_cost_usd": 0.01,
            },
        )
    )
    assert receipt.physical_identity["location"] == "edge"
    lanes.append(("latency_cost", receipt))

    report = evaluate_physical_dispatch_gate(lanes)
    assert report.gate_closed is True
    assert report.blockers == ()
    assert report.real_location_count == 3
    assert report.privacy_cloud_dispatch_count == 0
    assert {item.receipt.input_signals["condition"] for item in report.lanes} == {
        condition for condition, _receipt in lanes
    }
    assert len(
        {
            item.receipt.privacy_evidence["payload_digest"]
            for item in report.lanes
        }
    ) == 1

    index = build_redacted_dispatch_evidence_index(
        report=report,
        environment_profile={
            "operating_system": "windows",
            "edge_transport": "authenticated-loopback-http",
            "cloud_provider": "zhipu",
            "api_key": environment["ZAI_API_KEY"],
        },
        target_commit="test-target",
    )
    encoded = json.dumps(index, ensure_ascii=False, sort_keys=True)
    assert index["gate_closed"] is True
    assert index["credential_material_persisted"] is False
    assert "api_key" not in index["environment_profile"]
    assert environment["ZAI_API_KEY"] not in encoded
