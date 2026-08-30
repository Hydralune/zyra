from __future__ import annotations

from dataclasses import replace
import time
from types import SimpleNamespace

import pytest

from zyra_memory import MemoryLayer, MemoryRecord
from zyra_orchestration.topology_policy.contracts import FrozenDict, canonical_digest
from zyra_orchestration.topology_policy.production import (
    _canonical_memory_record_digest,
    _has_verifiable_interrupted_delivery,
    _is_json_array,
    _merge_task_workspace_delivery,
    _PhysicalLeaseHeartbeat,
    _physical_dispatch_payload_binding,
)
from zyra_scheduler.dispatch_evidence import _physical_dispatch_idempotency_key


class _HeartbeatStore:
    def __init__(self) -> None:
        self.lease = SimpleNamespace(
            lease_id="lease-1",
            worker_id="worker-1",
            acquired_at="2026-08-11T00:00:00+00:00",
            deadline_at="2026-08-11T01:00:00+00:00",
            terminal=False,
        )
        self.heartbeat = None

    def require_lease(self, lease_id: str):
        assert lease_id == self.lease.lease_id
        return self.lease

    def latest_heartbeat(self, worker_id: str):
        assert worker_id == self.lease.worker_id
        return self.heartbeat


class _HeartbeatPool:
    def __init__(self) -> None:
        self.store = _HeartbeatStore()
        self.renewals = 0
        self.leases = SimpleNamespace(renew=self._renew)

    def heartbeat_local_worker(
        self,
        worker_id: str,
        *,
        sequence: int,
        process_uptime_ms: int,
    ) -> None:
        assert worker_id == self.store.lease.worker_id
        assert process_uptime_ms > 0
        self.store.heartbeat = SimpleNamespace(sequence=sequence)

    def _renew(self, lease_id: str, **arguments):
        assert lease_id == self.store.lease.lease_id
        assert arguments == {
            "worker_id": "worker-1",
            "fence_token": "token-1",
            "fence_epoch": 3,
            "ttl_seconds": 30.0,
        }
        self.renewals += 1
        self.store.lease.deadline_at = (
            f"2026-08-11T01:00:{self.renewals:02d}+00:00"
        )
        return self.store.lease


def _physical_heartbeat(
    pool: _HeartbeatPool,
    *,
    generation: str = "generation-1",
    selected_location: str = "cloud",
) -> _PhysicalLeaseHeartbeat:
    process = SimpleNamespace(
        status=SimpleNamespace(value="ready"),
        endpoint="http://127.0.0.1:8123",
        generation_id=generation,
    )
    return _PhysicalLeaseHeartbeat(
        pool_api=SimpleNamespace(pool=pool),
        binding={
            "lease_id": "lease-1",
            "worker_id": "worker-1",
            "selected_location": selected_location,
            "worker_endpoint": "http://127.0.0.1:8123",
            "worker_deployment_generation_id": "generation-1",
        },
        lease_private={"fence_token": "token-1", "fence_epoch": 3},
        process_manager=SimpleNamespace(
            status=lambda component: (
                process
                if component
                == {
                    "cloud": "profile:cloud",
                    "edge": "profile:edge",
                    "local": "profile:device",
                }[selected_location]
                else None
            )
        ),
        interval_seconds=0.01,
        ttl_seconds=30.0,
    )


def test_physical_payload_binding_uses_enriched_dispatched_task() -> None:
    operator_task = {"schema": "operator-task/v1", "goal": "do the work"}
    enriched = {
        **operator_task,
        "orchestrator_task_digest": canonical_digest(operator_task),
        "provider": "zhipu",
        "code_worker_context": {"route_ref": "route-1"},
    }
    port = SimpleNamespace(task=SimpleNamespace(payload=FrozenDict(enriched)))

    digest, origin_bound = _physical_dispatch_payload_binding(
        port,
        operator_task,
    )

    assert digest == canonical_digest(enriched)
    assert digest != canonical_digest(operator_task)
    assert origin_bound is True


def test_physical_payload_binding_rejects_wrong_origin_commitment() -> None:
    operator_task = {"schema": "operator-task/v1", "goal": "do the work"}
    port = SimpleNamespace(
        task=SimpleNamespace(
            payload=FrozenDict(
                {
                    **operator_task,
                    "orchestrator_task_digest": "0" * 64,
                    "provider": "zhipu",
                }
            )
        )
    )

    _, origin_bound = _physical_dispatch_payload_binding(port, operator_task)

    assert origin_bound is False


def test_physical_dispatch_idempotency_is_stable_only_within_one_attempt() -> None:
    first = SimpleNamespace(
        operator_idempotency_key="logical-operator-key",
        attempt_id="attempt-1",
    )
    successor = SimpleNamespace(
        operator_idempotency_key="logical-operator-key",
        attempt_id="attempt-2",
    )

    first_key = _physical_dispatch_idempotency_key(first, "cloud")

    assert first_key == _physical_dispatch_idempotency_key(first, "cloud")
    assert first_key != _physical_dispatch_idempotency_key(successor, "cloud")
    assert first_key != _physical_dispatch_idempotency_key(first, "edge")
    assert "logical-operator-key" in first_key
    assert "attempt-1" in first_key


def test_immutable_json_array_is_valid_delivery_evidence() -> None:
    frozen = FrozenDict({"workspace_delta": {"changed": ["smoke.txt"]}})
    workspace_delta = dict(frozen["workspace_delta"])

    assert isinstance(workspace_delta["changed"], tuple)
    assert _is_json_array(workspace_delta["changed"]) is True
    assert _has_verifiable_interrupted_delivery(
        "needs_verification",
        workspace_delta["changed"],
    ) is True
    assert _is_json_array("smoke.txt") is False


def test_interrupted_delivery_requires_changed_paths_and_verification() -> None:
    assert _has_verifiable_interrupted_delivery("completed", ("smoke.txt",)) is False
    assert _has_verifiable_interrupted_delivery("needs_verification", ()) is False
    assert _has_verifiable_interrupted_delivery(
        "needs_verification",
        "smoke.txt",
    ) is False


def test_task_workspace_delivery_merges_retries_with_delete_and_recreate() -> None:
    first = _merge_task_workspace_delivery(
        None,
        {
            "created": ["deliverables/report.md", "scratch/temporary.txt"],
            "modified": ["src/service.py"],
            "deleted": ["obsolete.txt"],
            "changed": [
                "deliverables/report.md",
                "scratch/temporary.txt",
                "src/service.py",
            ],
        },
        workspace_id="workspace-a",
    )
    recovered = _merge_task_workspace_delivery(
        first,
        {
            "created": ["obsolete.txt"],
            "modified": ["src/worker.py"],
            "deleted": ["scratch/temporary.txt"],
            "changed": ["obsolete.txt", "src/worker.py"],
        },
        workspace_id="workspace-a",
    )

    assert recovered == {
        "schema": "zyra.task-workspace-delivery/v1",
        "workspace_id": "workspace-a",
        "created_paths": ["deliverables/report.md", "obsolete.txt"],
        "modified_paths": ["src/service.py", "src/worker.py"],
        "deleted_paths": ["scratch/temporary.txt"],
        "changed_paths": [
            "deliverables/report.md",
            "obsolete.txt",
            "src/service.py",
            "src/worker.py",
        ],
        "physical_location_redacted": True,
    }

    rebound = _merge_task_workspace_delivery(
        recovered,
        {
            "created": ["fresh.txt"],
            "modified": [],
            "deleted": [],
            "changed": ["fresh.txt"],
        },
        workspace_id="workspace-b",
    )
    assert rebound["changed_paths"] == ["fresh.txt"]
    assert rebound["deleted_paths"] == []


def test_memory_record_digest_ignores_only_refresh_timestamps() -> None:
    first = MemoryRecord(
        run_id="run-1",
        task_id="task-1",
        layer=MemoryLayer.SEMANTIC,
        source_type="checkpoint",
        source_id="goal",
        memory_id="memory-goal",
        summary="stable goal",
        content={"goal": "finish"},
        created_at="2026-08-08T01:00:00Z",
        updated_at="2026-08-08T01:00:00Z",
    )
    refreshed = replace(
        first,
        created_at="2026-08-08T02:00:00Z",
        updated_at="2026-08-08T02:00:00Z",
    )
    changed = replace(
        refreshed,
        summary="changed goal",
    )

    assert _canonical_memory_record_digest(first) == _canonical_memory_record_digest(
        refreshed
    )
    assert _canonical_memory_record_digest(first) != _canonical_memory_record_digest(
        changed
    )


def test_physical_lease_heartbeat_renews_while_exact_process_is_live() -> None:
    pool = _HeartbeatPool()
    heartbeat = _physical_heartbeat(pool)

    heartbeat.start()
    deadline = time.monotonic() + 1.0
    while pool.renewals < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    heartbeat.stop()
    heartbeat.raise_if_failed()

    report = heartbeat.report()
    assert pool.renewals >= 2
    assert report["lease_renewal_count"] == pool.renewals
    assert report["worker_heartbeat_count"] == pool.renewals
    assert report["failure_type"] == ""
    assert report["fence_token_persisted"] is False


def test_physical_lease_heartbeat_rejects_replacement_generation() -> None:
    heartbeat = _physical_heartbeat(
        _HeartbeatPool(),
        generation="generation-2",
    )

    with pytest.raises(
        RuntimeError,
        match="physical deployment generation changed",
    ):
        heartbeat._pulse()


def test_physical_lease_heartbeat_maps_local_worker_to_device_profile() -> None:
    pool = _HeartbeatPool()
    heartbeat = _physical_heartbeat(pool, selected_location="local")

    heartbeat._pulse()

    assert pool.renewals == 1
    assert heartbeat.report()["component_id"] == "profile:device"
