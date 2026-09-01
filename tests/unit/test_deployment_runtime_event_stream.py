from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from zyra_orchestration.deployment.dispatch import DeploymentDispatchRuntime
from zyra_orchestration.deployment.models import (
    DeploymentProfile,
    Sensitivity,
    Workload,
    digest,
)
from zyra_orchestration.deployment.node_runtime import DeploymentNodeRuntime
from zyra_orchestration.deployment.profiles import default_profile_policies
from zyra_orchestration.deployment.state_store import DeploymentStateStore


def _workload() -> Workload:
    return Workload(
        workload_id="workload-runtime-stream",
        task_id="task-runtime-stream",
        run_id="run-runtime-stream",
        operation="phase2-operator-execution",
        payload={},
        sensitivity=Sensitivity.INTERNAL,
        complexity=1,
        latency_sla_ms=1_000,
        cpu_units=1,
        memory_mb=16,
    )


def _runtime(tmp_path: Path) -> DeploymentNodeRuntime:
    return DeploymentNodeRuntime(
        node_id="device-runtime-stream",
        generation_id="generation-runtime-stream",
        policy=default_profile_policies()[DeploymentProfile.DEVICE],
        data_root=tmp_path / "node",
        credential_presence={},
    )


def _presentation_payload(
    workload: Workload,
    *,
    phase: str,
    sequence: int,
    content: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "zyra.provider-assistant-presentation/v1",
        "phase": phase,
        "sequence": sequence,
        "delta_kind": "assistant_text",
        "run_id": workload.run_id,
        "task_id": workload.task_id,
        "session_id": "session-runtime-stream",
        "worker_request_id": "worker-request-runtime-stream",
        "stream_id": "provider:dispatch-runtime-stream",
        "assistant_message_id": (
            "message:assistant:provider:dispatch-runtime-stream"
        ),
        "segment_index": sequence,
    }
    if content:
        payload["content"] = content
    return payload


def _transport_event(
    workload: Workload,
    *,
    ordinal: int,
    phase: str,
    content: str = "",
) -> dict[str, Any]:
    payload = _presentation_payload(
        workload,
        phase=phase,
        sequence=ordinal,
        content=content,
    )
    return {
        "schema": "zyra.deployment-node-runtime-event/v1",
        "attempt_id": "attempt-runtime-stream",
        "workload_id": workload.workload_id,
        "run_id": workload.run_id,
        "task_id": workload.task_id,
        "ordinal": ordinal,
        "transport_sequence": ordinal,
        "phase": phase,
        "payload": payload,
        "payload_digest": digest(payload),
        "observed_at": "2026-09-01T00:00:00.000Z",
    }


def test_node_runtime_event_queue_is_transient_filtered_and_releasable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    workload = _workload()
    attempt_id = "attempt-runtime-stream"

    runtime._append_runtime_event(
        attempt_id=attempt_id,
        workload=workload,
        payload={
            **_presentation_payload(
                workload,
                phase="assistant_text_delta",
                sequence=0,
                content="must-not-forward",
            ),
            "phase": "tool_call_started",
        },
        transport_sequence=0,
    )
    for sequence, (phase, content) in enumerate(
        (
            ("assistant_text_started", ""),
            ("assistant_text_delta", "你好 Zyra"),
            ("assistant_text_ended", ""),
        ),
        start=1,
    ):
        runtime._append_runtime_event(
            attempt_id=attempt_id,
            workload=workload,
            payload=_presentation_payload(
                workload,
                phase=phase,
                sequence=sequence,
                content=content,
            ),
            transport_sequence=sequence,
        )

    first = runtime.runtime_event_page(
        attempt_id=attempt_id,
        after_ordinal=0,
        limit=2,
    )
    assert first["durable"] is False
    assert first["canonical_owner"] is False
    assert [item["ordinal"] for item in first["events"]] == [1, 2]
    assert [item["phase"] for item in first["events"]] == [
        "assistant_text_started",
        "assistant_text_delta",
    ]
    assert first["events"][1]["payload"]["content"] == "你好 Zyra"
    assert all(
        item["payload_digest"] == digest(item["payload"])
        for item in first["events"]
    )

    second = runtime.runtime_event_page(
        attempt_id=attempt_id,
        after_ordinal=first["next_ordinal"],
    )
    assert [item["ordinal"] for item in second["events"]] == [3]
    released = runtime.runtime_event_page(
        attempt_id=attempt_id,
        after_ordinal=3,
        release=True,
    )
    assert released["released"] is True
    assert runtime.runtime_event_page(
        attempt_id=attempt_id,
        after_ordinal=0,
    )["events"] == []


def test_node_runtime_event_queue_rejects_cross_task_binding(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    workload = _workload()
    payload = _presentation_payload(
        workload,
        phase="assistant_text_delta",
        sequence=1,
        content="forged",
    )
    payload["task_id"] = "task-foreign"

    with pytest.raises(ValueError, match="binding is invalid"):
        runtime._append_runtime_event(
            attempt_id="attempt-runtime-stream",
            workload=workload,
            payload=payload,
            transport_sequence=1,
        )


def test_node_runtime_event_queue_ignores_bound_child_presentation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    workload = _workload()
    payload = _presentation_payload(
        workload,
        phase="assistant_text_delta",
        sequence=1,
        content="child-private-stream",
    )
    payload.update(
        {
            "task_id": f"{workload.task_id}:skill:codebase-analysis:child",
            "session_id": "skill-session-child",
            "runtime_lineage": {
                "schema": "zyra.runtime-lineage/v1",
                "relation": "skill",
                "relation_id": "skill-call-child",
                "parent_run_id": workload.run_id,
                "parent_task_id": workload.task_id,
                "parent_session_id": "session-runtime-stream",
                "parent_worker_request_id": "worker-request-runtime-stream",
            },
        }
    )

    runtime._append_runtime_event(
        attempt_id="attempt-runtime-stream",
        workload=workload,
        payload=payload,
        transport_sequence=1,
    )

    assert runtime.runtime_event_page(
        attempt_id="attempt-runtime-stream",
        after_ordinal=0,
    )["events"] == []


def test_node_runtime_event_queue_rejects_forged_child_lineage(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    workload = _workload()
    payload = _presentation_payload(
        workload,
        phase="assistant_text_delta",
        sequence=1,
        content="forged-child",
    )
    payload.update(
        {
            "task_id": "task-foreign",
            "runtime_lineage": {
                "schema": "zyra.runtime-lineage/v1",
                "relation": "skill",
                "relation_id": "skill-call-forged",
                "parent_run_id": workload.run_id,
                "parent_task_id": "task-foreign-parent",
                "parent_session_id": "session-runtime-stream",
                "parent_worker_request_id": "worker-request-runtime-stream",
            },
        }
    )

    with pytest.raises(ValueError, match="binding is invalid"):
        runtime._append_runtime_event(
            attempt_id="attempt-runtime-stream",
            workload=workload,
            payload=payload,
            transport_sequence=1,
        )


class _ConcurrentRuntimeEventClient:
    def __init__(self, workload: Workload) -> None:
        self.workload = workload
        self.execute_started = threading.Event()
        self.event_polled = threading.Event()
        self.execute_finished = threading.Event()
        self.released = False
        self.event = _transport_event(
            workload,
            ordinal=1,
            phase="assistant_text_delta",
            content="streamed before receipt",
        )

    def execute(
        self,
        _payload: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        del timeout_seconds
        self.execute_started.set()
        assert self.event_polled.wait(timeout=2)
        self.execute_finished.set()
        return {"completed": True}

    def runtime_events(
        self,
        *,
        attempt_id: str,
        after_ordinal: int = 0,
        limit: int = 256,
        release: bool = False,
    ) -> dict[str, Any]:
        assert attempt_id == "attempt-runtime-stream"
        assert limit >= 1
        if release:
            self.released = True
        events = []
        if self.execute_started.is_set() and after_ordinal < 1 and not release:
            events = [self.event]
            self.event_polled.set()
        return {
            "schema": "zyra.deployment-node-runtime-events/v1",
            "attempt_id": attempt_id,
            "events": events,
            "next_ordinal": 1 if events else after_ordinal,
            "dropped_before_ordinal": 0,
            "released": release,
        }


def test_dispatch_polls_runtime_events_before_execution_receipt(
    tmp_path: Path,
) -> None:
    workload = _workload()
    client = _ConcurrentRuntimeEventClient(workload)
    forwarded: list[dict[str, Any]] = []
    runtime = DeploymentDispatchRuntime(
        DeploymentStateStore(tmp_path / "deployment.sqlite3")
    )

    response, warnings = runtime._execute_with_runtime_events(
        client=client,  # type: ignore[arg-type]
        payload={"attempt_id": "attempt-runtime-stream"},
        workload=workload,
        attempt_id="attempt-runtime-stream",
        timeout_seconds=2,
        sink=lambda event: forwarded.append(dict(event)),
    )

    assert response == {"completed": True}
    assert warnings == []
    assert client.execute_finished.is_set()
    assert client.released is True
    assert [item["ordinal"] for item in forwarded] == [1]
    assert forwarded[0]["payload"]["content"] == "streamed before receipt"


def test_dispatch_recovers_from_bounded_presentation_queue_drop(
    tmp_path: Path,
) -> None:
    workload = _workload()
    events = [
        _transport_event(
            workload,
            ordinal=ordinal,
            phase="assistant_text_delta",
            content=f"chunk-{ordinal}",
        )
        for ordinal in (2, 3)
    ]

    class DroppedPageClient:
        def runtime_events(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "schema": "zyra.deployment-node-runtime-events/v1",
                "attempt_id": "attempt-runtime-stream",
                "events": events,
                "next_ordinal": 3,
                "dropped_before_ordinal": 1,
            }

    forwarded: list[Mapping[str, Any]] = []
    cursor, count, dropped = DeploymentDispatchRuntime(
        DeploymentStateStore(tmp_path / "deployment.sqlite3")
    )._forward_runtime_event_page(
        client=DroppedPageClient(),  # type: ignore[arg-type]
        workload=workload,
        attempt_id="attempt-runtime-stream",
        after_ordinal=0,
        sink=forwarded.append,
    )

    assert (cursor, count, dropped) == (3, 2, 1)
    assert [item["ordinal"] for item in forwarded] == [2, 3]
