from __future__ import annotations

from types import SimpleNamespace

from zyra_orchestration.topology_policy.contracts import FrozenDict, canonical_digest
from zyra_orchestration.topology_policy.production import (
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
    assert _is_json_array("smoke.txt") is False
