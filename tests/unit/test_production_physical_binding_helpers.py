from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from zyra_memory import MemoryLayer, MemoryRecord
from zyra_orchestration.topology_policy.contracts import FrozenDict, canonical_digest
from zyra_orchestration.topology_policy.production import (
    _canonical_memory_record_digest,
    _has_verifiable_interrupted_delivery,
    _is_json_array,
    _physical_dispatch_payload_binding,
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
