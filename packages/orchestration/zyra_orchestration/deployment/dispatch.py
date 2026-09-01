from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from queue import Queue
from typing import Any, Callable

from .errors import DispatchRejected, DispatchTimeout, NodeProtocolError
from .models import (
    DispatchReceipt,
    DispatchStatus,
    PlacementDecision,
    Workload,
    digest,
    new_id,
    now_iso,
)
from .node_client import DeploymentNodeClient
from .state_store import DeploymentStateStore


class DeploymentDispatchRuntime:
    def __init__(
        self,
        store: DeploymentStateStore,
        *,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.enabled = enabled

    def dispatch(
        self,
        workload: Workload,
        decision: PlacementDecision,
        client: DeploymentNodeClient,
        *,
        predecessor_attempt_id: str = "",
        checkpoint_ref: str = "",
        timeout_seconds: float | None = None,
        attempt_id: str = "",
        runtime_event_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> DispatchReceipt:
        if not self.enabled:
            raise DispatchRejected(
                "deployment_dispatch_disabled",
                "deployment dispatch runtime is disabled",
                operation="dispatch",
                profile=decision.selected_profile.value,
            )
        if decision.workload_id != workload.workload_id:
            raise DispatchRejected(
                "deployment_decision_workload_mismatch",
                "placement decision does not belong to the workload",
                operation="dispatch",
                profile=decision.selected_profile.value,
            )
        candidate = next(
            (
                item
                for item in decision.candidates
                if item.profile is decision.selected_profile
            ),
            None,
        )
        if candidate is None or not candidate.admitted:
            raise DispatchRejected(
                "deployment_selected_candidate_invalid",
                "selected deployment candidate was not admitted",
                operation="dispatch",
                profile=decision.selected_profile.value,
            )
        idempotency_key = workload.idempotency_key or digest(
            {
                "workload_id": workload.workload_id,
                "profile": decision.selected_profile.value,
                "predecessor_attempt_id": predecessor_attempt_id,
            }
        )
        existing = self.store.dispatch_by_idempotency(idempotency_key)
        if existing is not None:
            if existing.workload_id != workload.workload_id:
                raise DispatchRejected(
                    "deployment_idempotency_conflict",
                    "deployment idempotency key belongs to another workload",
                    operation="dispatch",
                    profile=decision.selected_profile.value,
                )
            return existing
        observed_attempt_id = attempt_id or new_id("attempt")
        effective_workload = (
            replace(workload, checkpoint_ref=checkpoint_ref)
            if checkpoint_ref
            else workload
        )
        payload = {
            "schema": "zyra.deployment-node-execution-request/v1",
            "attempt_id": observed_attempt_id,
            "idempotency_key": idempotency_key,
            "workload": effective_workload.semantic_dict(),
            "placement": {
                "decision_id": decision.decision_id,
                "policy_digest": decision.policy_digest,
                "selected_profile": decision.selected_profile.value,
                "candidate": candidate.to_dict(),
            },
            "runtime_event_stream": {
                "schema": "zyra.deployment-runtime-event-stream-request/v1",
                "enabled": runtime_event_sink is not None,
                "content_policy": "assistant-presentation-only",
                "durable": False,
            },
            "predecessor_attempt_id": predecessor_attempt_id,
        }
        started = time.monotonic()
        runtime_event_warnings: list[str] = []
        try:
            if runtime_event_sink is None:
                response = client.execute(
                    payload,
                    timeout_seconds=timeout_seconds,
                )
            else:
                response, runtime_event_warnings = self._execute_with_runtime_events(
                    client=client,
                    payload=payload,
                    workload=effective_workload,
                    attempt_id=observed_attempt_id,
                    timeout_seconds=timeout_seconds,
                    sink=runtime_event_sink,
                )
        except TimeoutError as error:
            raise DispatchTimeout(
                "deployment_dispatch_timeout",
                "deployment node execution timed out",
                operation=workload.operation,
                profile=decision.selected_profile.value,
                retryable=True,
                details={"attempt_id": observed_attempt_id},
            ) from error
        elapsed_ms = round((time.monotonic() - started) * 1000)
        receipt = self._validate_response(
            workload=effective_workload,
            decision=decision,
            response=response,
            predecessor_attempt_id=predecessor_attempt_id,
            expected_attempt_id=observed_attempt_id,
        )
        saved = self.store.save_dispatch(
            receipt,
            idempotency_key=idempotency_key,
        )
        self.store.append_event(
            "deployment.dispatch_effect_verified",
            {
                "dispatch_id": saved.dispatch_id,
                "attempt_id": saved.attempt_id,
                "workload_id": saved.workload_id,
                "status": saved.status.value,
                "profile": saved.profile.value,
                "elapsed_ms": elapsed_ms,
                "artifact_refs": list(saved.artifact_refs),
                "checkpoint_ref": saved.checkpoint_ref,
                "result_digest": saved.result_digest,
            },
            component_id=saved.node_id,
            profile=saved.profile.value,
            task_id=saved.task_id,
            run_id=saved.run_id,
            causation_id=decision.decision_id,
            correlation_id=saved.dispatch_id,
        )
        for warning in runtime_event_warnings[-16:]:
            self.store.append_event(
                "deployment.runtime_event_forwarding_warning",
                {
                    "attempt_id": observed_attempt_id,
                    "workload_id": effective_workload.workload_id,
                    "warning": warning[:1_000],
                    "task_outcome_affected": False,
                },
                component_id=saved.node_id,
                profile=saved.profile.value,
                task_id=saved.task_id,
                run_id=saved.run_id,
                correlation_id=saved.dispatch_id,
            )
        return saved

    def _execute_with_runtime_events(
        self,
        *,
        client: DeploymentNodeClient,
        payload: Mapping[str, Any],
        workload: Workload,
        attempt_id: str,
        timeout_seconds: float | None,
        sink: Callable[[Mapping[str, Any]], None],
    ) -> tuple[dict[str, Any], list[str]]:
        outcome: Queue[tuple[str, Any]] = Queue(maxsize=1)

        def execute() -> None:
            try:
                outcome.put(
                    (
                        "result",
                        client.execute(payload, timeout_seconds=timeout_seconds),
                    )
                )
            except BaseException as error:  # preserve the original transport failure.
                outcome.put(("error", error))

        thread = threading.Thread(
            target=execute,
            name=f"zyra-node-dispatch-{attempt_id[:32]}",
            daemon=True,
        )
        thread.start()
        after_ordinal = 0
        warnings: list[str] = []
        while thread.is_alive():
            try:
                previous_ordinal = after_ordinal
                after_ordinal, _count, dropped_before = (
                    self._forward_runtime_event_page(
                        client=client,
                        workload=workload,
                        attempt_id=attempt_id,
                        after_ordinal=after_ordinal,
                        sink=sink,
                    )
                )
                if dropped_before > previous_ordinal:
                    warnings.append(
                        "runtime-event presentation queue dropped ordinals "
                        f"{previous_ordinal + 1}..{dropped_before}"
                    )
            except BaseException as error:
                warnings.append(f"{type(error).__name__}: {error}")
            thread.join(timeout=0.05)
        thread.join()
        for _ in range(64):
            try:
                previous_ordinal = after_ordinal
                next_ordinal, count, dropped_before = (
                    self._forward_runtime_event_page(
                        client=client,
                        workload=workload,
                        attempt_id=attempt_id,
                        after_ordinal=after_ordinal,
                        sink=sink,
                    )
                )
                if dropped_before > previous_ordinal:
                    warnings.append(
                        "runtime-event presentation queue dropped ordinals "
                        f"{previous_ordinal + 1}..{dropped_before}"
                    )
                after_ordinal = next_ordinal
                if count == 0:
                    break
            except BaseException as error:
                warnings.append(f"{type(error).__name__}: {error}")
                break
        try:
            client.runtime_events(
                attempt_id=attempt_id,
                after_ordinal=after_ordinal,
                limit=1,
                release=True,
            )
        except BaseException as error:
            warnings.append(f"release {type(error).__name__}: {error}")
        kind, value = outcome.get()
        if kind == "error":
            if isinstance(value, BaseException):
                raise value
            raise RuntimeError("deployment dispatch thread returned invalid error state")
        if not isinstance(value, Mapping):
            raise NodeProtocolError(
                "deployment_node_response_shape_invalid",
                "deployment node execution response is not an object",
                operation="execute",
            )
        return dict(value), warnings

    @staticmethod
    def _forward_runtime_event_page(
        *,
        client: DeploymentNodeClient,
        workload: Workload,
        attempt_id: str,
        after_ordinal: int,
        sink: Callable[[Mapping[str, Any]], None],
    ) -> tuple[int, int, int]:
        page = client.runtime_events(
            attempt_id=attempt_id,
            after_ordinal=after_ordinal,
            limit=1_000,
        )
        if (
            page.get("schema") != "zyra.deployment-node-runtime-events/v1"
            or str(page.get("attempt_id") or "") != attempt_id
        ):
            raise NodeProtocolError(
                "deployment_runtime_event_page_invalid",
                "deployment node returned an invalid runtime-event page",
                operation="runtime-events",
            )
        try:
            dropped_before = int(page.get("dropped_before_ordinal") or 0)
        except (TypeError, ValueError) as error:
            raise NodeProtocolError(
                "deployment_runtime_event_cursor_invalid",
                "deployment runtime-event drop cursor is invalid",
                operation="runtime-events",
            ) from error
        if dropped_before < 0:
            raise NodeProtocolError(
                "deployment_runtime_event_cursor_invalid",
                "deployment runtime-event drop cursor is invalid",
                operation="runtime-events",
            )
        expected = max(after_ordinal, dropped_before)
        events = page.get("events")
        if not isinstance(events, list):
            raise NodeProtocolError(
                "deployment_runtime_event_page_invalid",
                "deployment runtime-event page has no event list",
                operation="runtime-events",
            )
        forwarded = 0
        for raw in events:
            if not isinstance(raw, Mapping):
                raise NodeProtocolError(
                    "deployment_runtime_event_invalid",
                    "deployment runtime event is not an object",
                    operation="runtime-events",
                )
            event = dict(raw)
            ordinal = int(event.get("ordinal") or 0)
            event_payload = event.get("payload")
            if (
                event.get("schema")
                != "zyra.deployment-node-runtime-event/v1"
                or ordinal != expected + 1
                or str(event.get("attempt_id") or "") != attempt_id
                or str(event.get("workload_id") or "") != workload.workload_id
                or str(event.get("run_id") or "") != workload.run_id
                or str(event.get("task_id") or "") != workload.task_id
                or not isinstance(event_payload, Mapping)
                or event_payload.get("schema")
                != "zyra.provider-assistant-presentation/v1"
                or str(event.get("phase") or "")
                != str(event_payload.get("phase") or "")
                or str(event.get("payload_digest") or "") != digest(event_payload)
            ):
                raise NodeProtocolError(
                    "deployment_runtime_event_binding_invalid",
                    "deployment runtime event failed identity or digest validation",
                    operation="runtime-events",
                )
            sink(event)
            expected = ordinal
            forwarded += 1
        next_ordinal = int(page.get("next_ordinal") or expected)
        if next_ordinal < expected:
            raise NodeProtocolError(
                "deployment_runtime_event_cursor_regressed",
                "deployment runtime-event cursor regressed",
                operation="runtime-events",
            )
        return max(expected, next_ordinal), forwarded, dropped_before

    @staticmethod
    def _validate_response(
        *,
        workload: Workload,
        decision: PlacementDecision,
        response: Mapping[str, Any],
        predecessor_attempt_id: str,
        expected_attempt_id: str,
    ) -> DispatchReceipt:
        if str(response.get("schema") or "") != "zyra.deployment-node-receipt/v1":
            raise NodeProtocolError(
                "deployment_node_receipt_schema_invalid",
                "deployment node returned an unsupported receipt schema",
                operation=workload.operation,
                profile=decision.selected_profile.value,
            )
        attempt_id = str(response.get("attempt_id") or "")
        if attempt_id != expected_attempt_id:
            raise NodeProtocolError(
                "deployment_node_attempt_mismatch",
                "deployment node receipt attempt identity does not match",
                operation=workload.operation,
                profile=decision.selected_profile.value,
                details={
                    "expected": expected_attempt_id,
                    "actual": attempt_id,
                },
            )
        if str(response.get("workload_id") or "") != workload.workload_id:
            raise NodeProtocolError(
                "deployment_node_workload_mismatch",
                "deployment node receipt belongs to another workload",
                operation=workload.operation,
                profile=decision.selected_profile.value,
            )
        if str(response.get("profile") or "") != decision.selected_profile.value:
            raise NodeProtocolError(
                "deployment_node_profile_receipt_mismatch",
                "deployment node receipt profile does not match placement",
                operation=workload.operation,
                profile=decision.selected_profile.value,
            )
        try:
            status = DispatchStatus(str(response.get("status") or ""))
        except ValueError as error:
            raise NodeProtocolError(
                "deployment_node_status_invalid",
                "deployment node receipt status is invalid",
                operation=workload.operation,
                profile=decision.selected_profile.value,
            ) from error
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise NodeProtocolError(
                "deployment_node_result_invalid",
                "deployment node receipt result is not an object",
                operation=workload.operation,
                profile=decision.selected_profile.value,
            )
        result_digest = str(response.get("result_digest") or "")
        if not result_digest or digest(result) != result_digest:
            raise NodeProtocolError(
                "deployment_node_result_digest_invalid",
                "deployment node result digest is invalid",
                operation=workload.operation,
                profile=decision.selected_profile.value,
            )
        artifact_refs = tuple(
            str(item) for item in response.get("artifact_refs") or () if str(item)
        )
        checkpoint_ref = str(response.get("checkpoint_ref") or "")
        if status is DispatchStatus.SUCCEEDED:
            if not artifact_refs:
                raise NodeProtocolError(
                    "deployment_node_artifact_missing",
                    "successful deployment node receipt has no artifact",
                    operation=workload.operation,
                    profile=decision.selected_profile.value,
                )
            if not checkpoint_ref:
                raise NodeProtocolError(
                    "deployment_node_checkpoint_missing",
                    "successful deployment node receipt has no checkpoint",
                    operation=workload.operation,
                    profile=decision.selected_profile.value,
                )
        return DispatchReceipt(
            dispatch_id=new_id("dispatch"),
            attempt_id=attempt_id,
            workload_id=workload.workload_id,
            task_id=workload.task_id,
            run_id=workload.run_id,
            profile=decision.selected_profile,
            node_id=str(response.get("node_id") or ""),
            status=status,
            started_at=str(response.get("started_at") or now_iso()),
            completed_at=str(response.get("completed_at") or now_iso()),
            result=dict(result),
            artifact_refs=artifact_refs,
            checkpoint_ref=checkpoint_ref,
            predecessor_attempt_id=predecessor_attempt_id,
            failure_code=str(response.get("failure_code") or result.get("error") or ""),
            degraded=status is DispatchStatus.DEGRADED,
            result_digest=result_digest,
        )


__all__ = ["DeploymentDispatchRuntime"]
