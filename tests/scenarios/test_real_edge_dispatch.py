from __future__ import annotations

import os

import psutil

from tests.integration.physical_dispatch_harness import build_physical_harness


def test_killed_edge_process_preserves_orchestrator_and_reroutes_local(
    tmp_path,
) -> None:
    harness = build_physical_harness(
        tmp_path,
        location="edge",
        fallback_locations=("local",),
    )
    orchestrator_pid = os.getpid()
    edge_pid = int(harness.clients["edge"].health()["pid"])
    local_pid = int(harness.clients["local"].health()["pid"])
    try:
        assert edge_pid not in {orchestrator_pid, local_pid}
        edge_process = psutil.Process(edge_pid)
        edge_process.kill()
        edge_process.wait(timeout=10)

        result = harness.execute()
        receipt = harness.physical_port.receipts[-1]
        failure = harness.physical_port.failure_receipts[0]

        assert result.recovery_plan_refs
        assert result.attempt_receipts[0].worker_id == "physical-local-worker"
        assert receipt.physical_identity["location"] == "local"
        assert int(receipt.physical_identity["pid"]) == local_pid
        assert failure.location == "edge"
        assert failure.physical_attempt_id != receipt.physical_attempt_id
        assert failure.side_effect_started is False
        assert failure.retry_safe is True
        assert any(
            item["physical_attempt_id"] == failure.physical_attempt_id
            for item in receipt.recovery_evidence
        )
        assert psutil.pid_exists(orchestrator_pid)
        assert psutil.pid_exists(local_pid)
        assert not psutil.pid_exists(edge_pid)
    finally:
        harness.close()


def test_real_edge_network_disconnect_fails_preflight_and_reroutes(
    tmp_path,
) -> None:
    harness = build_physical_harness(
        tmp_path,
        location="edge",
        fallback_locations=("local",),
    )
    try:
        fault = harness.clients["edge"].inject_fault(
            {"kind": "network-loss", "enabled": True}
        )
        assert fault["faults"]["network_down"] is True

        result = harness.execute()
        receipt = harness.physical_port.receipts[-1]
        failure = harness.physical_port.failure_receipts[0]

        assert result.recovery_plan_refs
        assert receipt.physical_identity["location"] == "local"
        assert failure.location == "edge"
        assert failure.side_effect_started is False
        assert failure.retry_safe is True
        assert "physical_dispatch_node_unhealthy" in failure.failure_code
        assert receipt.recovery_evidence
    finally:
        harness.close()
