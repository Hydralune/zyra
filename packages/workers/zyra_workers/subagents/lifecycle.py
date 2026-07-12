from __future__ import annotations

import copy
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Mapping

from zyra_core import EventRecord

from .budget import BudgetReservation, SubagentBudgetReservationStore
from .dispatch import SubagentExecutionPort
from .errors import IsolationCleanupFailed, ParentCancelled, SubagentDisabled
from .events import cleanup_event, dispatch_event, handoff_event, progress_event, subagent_event
from .handoff import SubagentHandoffRuntime
from .isolation import SubagentIsolationRequestPort
from .models import (
    IsolationCleanupReceipt,
    RecoverySignal,
    StructuredHandoff,
    SubagentDispatchReceipt,
    SubagentDispatchRequest,
    SubagentExecutionResult,
    SubagentProgress,
    SubagentTaskRecord,
    SubagentTaskStatus,
    TranscriptEntryKind,
    UsageLedger,
)
from .recovery import SubagentRecoverySignalRuntime
from .task_store import SubagentTaskStore
from .transcript import SubagentTranscriptStore


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    record: SubagentTaskRecord
    handoff: StructuredHandoff | None
    execution_result: SubagentExecutionResult | None
    events: tuple[EventRecord, ...]
    cleanup: IsolationCleanupReceipt | None
    reservation: BudgetReservation | None

    @property
    def ok(self) -> bool:
        return self.record.status == SubagentTaskStatus.COMPLETED and self.handoff is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "record": self.record.safe_dict(),
            "handoff": self.handoff.safe_dict() if self.handoff else None,
            "execution_result": self.execution_result.safe_dict() if self.execution_result else None,
            "events": [
                {
                    "event_id": item.event_id,
                    "event_type": item.event_type.value,
                    "created_at": item.created_at,
                    "payload": copy.deepcopy(item.payload),
                }
                for item in self.events
            ],
            "cleanup": self.cleanup.to_dict() if self.cleanup else None,
            "reservation": self.reservation.to_dict() if self.reservation else None,
        }


class AgentTaskLifecycleRuntime:
    """Durable logical task lifecycle around the existing worker port."""

    def __init__(
        self,
        *,
        task_store: SubagentTaskStore,
        transcript_store: SubagentTranscriptStore,
        budget_store: SubagentBudgetReservationStore,
        isolation_port: SubagentIsolationRequestPort,
        execution_port: SubagentExecutionPort,
        handoff_runtime: SubagentHandoffRuntime,
        recovery_runtime: SubagentRecoverySignalRuntime | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.task_store = task_store
        self.transcript_store = transcript_store
        self.budget_store = budget_store
        self.isolation_port = isolation_port
        self.execution_port = execution_port
        self.handoff_runtime = handoff_runtime
        self.recovery_runtime = recovery_runtime or SubagentRecoverySignalRuntime()
        self.event_sink = event_sink
        self.disabled = disabled
        self._lock = RLock()

    def prepare(
        self,
        record: SubagentTaskRecord,
        *,
        idempotency_key: str,
        context_payload: Mapping[str, Any],
    ) -> tuple[SubagentTaskRecord, tuple[EventRecord, ...]]:
        self._require_enabled()
        created = self.task_store.create(record, idempotency_key=idempotency_key)
        if created.revision > 0 or created.status != SubagentTaskStatus.CREATED:
            return created, ()
        self.transcript_store.initialize(
            created.task_id,
            context_payload=context_payload,
            metadata={
                "agent_type": created.agent_type,
                "definition_id": created.definition_id,
                "tool_scope_digest": created.tool_scope.digest,
                "permission_digest": created.permission.digest,
                "parent_session_id": created.parent_session_id,
            },
        )
        validating, _ = self.task_store.transition(
            created.task_id,
            SubagentTaskStatus.VALIDATING,
            expected_revision=created.revision,
            mutation="validation_started",
        )
        ready, _ = self.task_store.transition(
            validating.task_id,
            SubagentTaskStatus.READY,
            expected_revision=validating.revision,
            mutation="validation_completed",
        )
        events = (
            subagent_event(created, "task_created"),
            subagent_event(ready, "validated", payload={
                "tool_scope": ready.tool_scope.to_dict(),
                "permission": ready.permission.to_dict(),
                "context_snapshot_id": ready.context_snapshot.snapshot_id,
            }),
        )
        self._emit(events)
        return ready, events

    def execute(
        self,
        dispatch_request: SubagentDispatchRequest,
        *,
        reservation_id: str,
        cancellation_check: Callable[[], bool],
    ) -> LifecycleResult:
        self._require_enabled()
        record = self.task_store.get(dispatch_request.task_id)
        if record.status not in {SubagentTaskStatus.READY, SubagentTaskStatus.RESUMING}:
            raise RuntimeError(f"subagent task is not dispatchable from {record.status}")
        execution_ref = self.execution_port.execution_ref(dispatch_request)
        dispatched, _ = self.task_store.attach_dispatch(
            record.task_id,
            dispatch_request=dispatch_request.safe_dict(),
            execution_ref=execution_ref,
            expected_revision=record.revision,
        )
        receipt = SubagentDispatchReceipt(
            task_id=record.task_id,
            dispatch_id=dispatch_request.dispatch_id,
            accepted=True,
            execution_ref=execution_ref,
            status=SubagentTaskStatus.DISPATCHED,
            worker_projection={
                "task_id": record.task_id,
                "dispatch_request": dispatch_request.safe_dict(),
                "execution_ref": execution_ref,
                "physical_worker_owned": False,
                "worker_lease_owned": False,
            },
        )
        running, _ = self.task_store.transition(
            record.task_id,
            SubagentTaskStatus.RUNNING,
            expected_revision=dispatched.revision,
            mutation="execution_started",
        )
        self.transcript_store.append(
            record.task_id,
            TranscriptEntryKind.METADATA,
            {
                "phase": "dispatch",
                "dispatch": receipt.to_dict(),
                "execution_mode": dispatch_request.execution_mode.value,
            },
        )
        events: list[EventRecord] = [dispatch_event(running, receipt), subagent_event(running, "running")]
        self._emit(events)

        execution_result: SubagentExecutionResult | None = None
        handoff: StructuredHandoff | None = None
        cleanup: IsolationCleanupReceipt | None = None
        settled: BudgetReservation | None = None

        def on_progress(raw: Mapping[str, Any]) -> None:
            current = self.task_store.get(record.task_id)
            progress = SubagentProgress(
                task_id=record.task_id,
                status=current.status,
                summary=str(raw.get("phase") or "subagent progress"),
                usage=current.usage,
                execution_ref=current.execution_ref,
                tool_name=str(raw.get("tool_name") or ""),
                metadata=dict(raw),
            )
            self.transcript_store.append(record.task_id, TranscriptEntryKind.PROGRESS, progress.to_dict())
            event = progress_event(current, progress)
            events.append(event)
            self._emit((event,))

        try:
            if cancellation_check():
                raise ParentCancelled(record.parent_task_id)
            execution_result = self.execution_port.execute(
                dispatch_request,
                cancel_check=cancellation_check,
                progress=on_progress,
            )
            if cancellation_check():
                raise ParentCancelled(record.parent_task_id)
            self.transcript_store.append(
                record.task_id,
                TranscriptEntryKind.ASSISTANT,
                {
                    "content": execution_result.summary,
                    "ok": execution_result.ok,
                    "execution_ref": execution_result.execution_ref,
                    "artifact_refs": [item.artifact_id for item in execution_result.artifacts],
                },
            )
            if execution_result.ok:
                handoff = self.handoff_runtime.success(
                    self.task_store.get(record.task_id),
                    execution_result,
                    transcript_ref=str(self.transcript_store.path(record.task_id)),
                )
                self.handoff_runtime.validate(handoff)
                final = self.task_store.attach_handoff(
                    record.task_id,
                    handoff,
                    status=SubagentTaskStatus.COMPLETED,
                )
            else:
                signal = self.recovery_runtime.signal(
                    self.task_store.get(record.task_id),
                    None,
                    error_code=execution_result.error_code,
                    reason=execution_result.error_message or execution_result.summary,
                )
                handoff = self.handoff_runtime.failure(
                    self.task_store.get(record.task_id),
                    execution_result,
                    signal,
                    transcript_ref=str(self.transcript_store.path(record.task_id)),
                )
                self.handoff_runtime.validate(handoff)
                final = self.task_store.attach_handoff(
                    record.task_id,
                    handoff,
                    status=SubagentTaskStatus.FAILED,
                )
            self.transcript_store.append(record.task_id, TranscriptEntryKind.HANDOFF, handoff.safe_dict())
            settled = self.budget_store.settle(reservation_id, execution_result.usage)
            terminal_event = handoff_event(final, handoff)
            events.append(terminal_event)
            self._emit((terminal_event,))
        except Exception as error:
            current = self.task_store.get(record.task_id)
            signal = self.recovery_runtime.signal(current, error)
            self.task_store.append_recovery_signal(record.task_id, signal)
            target = SubagentTaskStatus.CANCELLED if isinstance(error, ParentCancelled) else SubagentTaskStatus.FAILED
            try:
                final, _ = self.task_store.transition(
                    record.task_id,
                    target,
                    mutation="execution_exception",
                )
            except Exception:
                final = self.task_store.get(record.task_id)
            self.transcript_store.append(
                record.task_id,
                TranscriptEntryKind.FAILURE if target == SubagentTaskStatus.FAILED else TranscriptEntryKind.CANCEL,
                {"recovery_signal": signal.to_dict(), "error_type": type(error).__name__},
            )
            self.budget_store.release(reservation_id, reason=signal.error_code or signal.reason)
            failure_event = subagent_event(
                final,
                "cancelled" if target == SubagentTaskStatus.CANCELLED else "failed",
                payload={"recovery_signal": signal.to_dict()},
            )
            events.append(failure_event)
            self._emit((failure_event,))
        finally:
            current = self.task_store.get(record.task_id)
            try:
                cleanup = self.isolation_port.cleanup(dispatch_request.isolation)
                cleanup_record = self.task_store.get(record.task_id)
                event = cleanup_event(cleanup_record, cleanup)
                events.append(event)
                self._emit((event,))
            except Exception as cleanup_error:
                signal = self.recovery_runtime.signal(
                    current,
                    cleanup_error,
                    error_code="subagent_isolation_cleanup_failed",
                )
                self.task_store.append_recovery_signal(record.task_id, signal)
                if current.status != SubagentTaskStatus.CLEANUP_FAILED:
                    try:
                        current, _ = self.task_store.transition(
                            record.task_id,
                            SubagentTaskStatus.CLEANUP_FAILED,
                            mutation="cleanup_failed",
                        )
                    except Exception:
                        current = self.task_store.get(record.task_id)
                event = subagent_event(current, "failed", payload={"recovery_signal": signal.to_dict()})
                events.append(event)
                self._emit((event,))
                cleanup = None

        final_record = self.task_store.get(record.task_id)
        return LifecycleResult(
            record=final_record,
            handoff=handoff,
            execution_result=execution_result,
            events=tuple(events),
            cleanup=cleanup,
            reservation=settled,
        )

    def cancel(self, task_id: str, *, reason: str, cascade: bool = True) -> tuple[SubagentTaskRecord, ...]:
        self._require_enabled()
        targets = [self.task_store.get(task_id)]
        if cascade:
            targets.extend(self.task_store.descendants(task_id))
        cancelled: list[SubagentTaskRecord] = []
        for record in reversed(targets):
            if record.status.terminal:
                cancelled.append(record)
                continue
            if record.execution_ref:
                self.execution_port.cancel(record.execution_ref)
            try:
                current, _ = self.task_store.transition(
                    record.task_id,
                    SubagentTaskStatus.CANCELLED,
                    mutation="parent_cancel_cascade" if record.task_id != task_id else "cancel_requested",
                    update=lambda item: item.metadata.update({"cancel_reason": reason, "cancel_requested": True}),
                )
            except Exception:
                current = self.task_store.get(record.task_id)
            self.transcript_store.append(
                record.task_id,
                TranscriptEntryKind.CANCEL,
                {"reason": reason, "cascade": cascade, "requested_at": current.updated_at},
            )
            event = subagent_event(current, "cancelled", payload={"reason": reason, "cascade": cascade})
            self._emit((event,))
            cancelled.append(current)
        return tuple(cancelled)

    def _emit(self, events: tuple[EventRecord, ...] | list[EventRecord]) -> None:
        if self.event_sink is None:
            return
        for event in events:
            self.event_sink(event)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise SubagentDisabled("AgentTaskLifecycleRuntime")
