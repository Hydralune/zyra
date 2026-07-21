from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from zyra_scheduler.worker_pool import (
    BackendCapability,
    CapabilityAttestor,
    CapabilityRequirement,
    ResourceVector,
    WorkerLocation,
    WorkerPoolFoundationRuntime,
)
from zyra_workers.edge_pool import (
    EdgeGatewayRequest,
    EdgeProcessError,
    EdgeWorkerGatewayRuntime,
    EdgeWorkerProcessConnector,
    EdgeWorkerRegistrationRuntime,
)


def _package_roots() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[2]
    return tuple(path for path in (root / "packages").iterdir() if path.is_dir())


def test_real_edge_process_attests_heartbeats_executes_gateway_artifact_and_is_fenced(tmp_path: Path) -> None:
    secret = b"real-edge-worker-resource-pool-secret-32"
    pool = WorkerPoolFoundationRuntime(
        tmp_path / "worker-pool.sqlite3",
        attestation_secret=secret,
        default_lease_ttl_seconds=10,
    )
    connector = EdgeWorkerProcessConnector(
        worker_id="edge-real-worker",
        secret=secret,
        package_roots=_package_roots(),
        startup_timeout_seconds=10,
    )
    registration = EdgeWorkerRegistrationRuntime(
        connector,
        pool.lifecycle,
        pool.heartbeats,
        attestor=CapabilityAttestor(secret),
    )
    try:
        endpoint = registration.register(
            backend=BackendCapability(
                backend_id="edge-sandbox-gateway",
                backend_kind="sandbox_gateway",
                enabled=True,
                healthy=True,
                capabilities=("edge_execution", "artifact_return", "agent_task"),
                tool_ids=("edge.echo_artifact", "edge.hash_artifact", "edge.json_transform"),
            ),
            resources=ResourceVector(cpu_cores=1, memory_mb=512, process_slots=2),
        )
        assert endpoint.pid != os.getpid()
        assert endpoint.endpoint.startswith("tcp://127.0.0.1:")
        worker = pool.store.require_worker("edge-real-worker")
        assert worker.location is WorkerLocation.EDGE
        assert worker.process_identity.startswith("pid-")

        acquisition = pool.acquire_task(
            task_id="edge-only-task",
            run_id="edge-run",
            owner_session_id="edge-session",
            requirement=CapabilityRequirement(
                required=("edge_execution",),
                locations=(WorkerLocation.EDGE,),
                resources=ResourceVector(process_slots=1, memory_mb=64),
            ),
            preferred_worker_ids=("edge-real-worker",),
        )
        gateway = EdgeWorkerGatewayRuntime(
            connector,
            pool.leases,
            artifact_root=tmp_path / "artifacts",
        )
        gateway_receipt = gateway.execute(
            EdgeGatewayRequest(
                task_id=acquisition.attempt.task_id,
                run_id=acquisition.attempt.run_id,
                attempt_id=acquisition.attempt.attempt_id,
                lease_id=acquisition.lease.lease_id,
                worker_id=acquisition.worker.worker_id,
                fence_token=acquisition.lease.fence_token,
                fence_epoch=acquisition.lease.fence_epoch,
                operation="echo_artifact",
                input_payload={"content": "real edge process artifact"},
                artifact_name="result.txt",
                job_id="edge-job-1",
            )
        )
        assert Path(gateway_receipt.artifact_path).read_text(encoding="utf-8") == "real edge process artifact"
        receipt = gateway.finalize(gateway_receipt)
        assert receipt.outcome.value == "succeeded"
        heartbeat = registration.observe_heartbeat(ResourceVector(cpu_cores=1, memory_mb=512, process_slots=2))
        assert heartbeat.telemetry.metadata["edge_process"] is True
    finally:
        registration.stop()


def test_edge_cancel_really_interrupts_job_and_disabled_connector_has_no_local_fallback(tmp_path: Path) -> None:
    secret = b"real-edge-worker-cancellation-secret-32"
    connector = EdgeWorkerProcessConnector(
        worker_id="edge-cancel-worker",
        secret=secret,
        package_roots=_package_roots(),
        startup_timeout_seconds=10,
        request_timeout_seconds=10,
    )
    connector.start()
    challenge = "challenge-edge-cancel"
    connector.attest(manifest_digest="a" * 64, challenge_nonce=challenge)
    result: dict[str, object] = {}

    def execute() -> None:
        result["value"] = connector.execute(
            job_id="long-edge-job",
            task_id="edge-cancel-task",
            attempt_id="edge-attempt",
            lease_id="edge-lease",
            fence_epoch=1,
            fence_token_digest="b" * 64,
            operation="wait",
            input_payload={"duration_ms": 5000},
            timeout_seconds=8,
        )

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        import time

        time.sleep(0.2)
        assert connector.cancel(job_id="long-edge-job", reason="test cancellation") is True
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert result["value"].outcome == "cancelled"
    finally:
        connector.stop()

    disabled = EdgeWorkerProcessConnector(
        worker_id="edge-disabled",
        secret=secret,
        package_roots=_package_roots(),
        enabled=False,
    )
    with pytest.raises(EdgeProcessError, match="no local fallback"):
        disabled.start()
