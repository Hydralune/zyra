from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import RLock
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .compact_bridge import compact_reference_from_dict
from .digests import digest_object
from .disclosure import (
    SkillDisclosureEnvelope,
    SkillDisclosureLedger,
    SkillDisclosureRenderer,
    SkillDisclosureRender,
)
from .integration_errors import (
    SkillCheckpointConflict,
    SkillCheckpointCorrupt,
    SkillEventAppendError,
    SkillEventCausalityError,
    SkillEventSequenceError,
    SkillRestoreMessageRejected,
    SkillSessionClosed,
    SkillSessionConflict,
    SkillSessionNotFound,
    SkillSessionOwnershipError,
)
from .models import (
    InvokedSkillState,
    SkillInvocationMode,
    SkillInvocationPlan,
    SkillInvocationRequest,
    SkillInvocationStatus,
    utc_now,
)


class SkillSessionStatus(StrEnum):
    OPEN = "open"
    COMPACTING = "compacting"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SkillCanonicalEvent:
    event_id: str
    sequence: int
    run_id: str
    task_id: str
    session_id: str
    agent_id: str
    event_type: str
    invocation_id: str = ""
    skill_ref: str = ""
    parent_event_id: str = ""
    tool_call_id: str = ""
    worker_request_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.event_id or self.sequence < 1:
            raise ValueError("skill canonical event requires id and positive sequence")
        if not self.run_id or not self.task_id or not self.session_id:
            raise ValueError("skill canonical event requires run/task/session identity")
        if not self.event_type:
            raise ValueError("skill canonical event requires event_type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "event_type": self.event_type,
            "invocation_id": self.invocation_id,
            "skill_ref": self.skill_ref,
            "parent_event_id": self.parent_event_id,
            "tool_call_id": self.tool_call_id,
            "worker_request_id": self.worker_request_id,
            "payload": copy.deepcopy(self.payload),
            "created_at": self.created_at,
        }


class SkillCanonicalEventPort(Protocol):
    def append(self, events: Sequence[SkillCanonicalEvent]) -> None: ...


class InMemorySkillCanonicalEventPort:
    def __init__(self) -> None:
        self._lock = RLock()
        self._events: list[SkillCanonicalEvent] = []

    def append(self, events: Sequence[SkillCanonicalEvent]) -> None:
        with self._lock:
            previous = self._events[-1].sequence if self._events else 0
            for event in events:
                if event.sequence != previous + 1:
                    raise SkillEventSequenceError(
                        "skill canonical event sequence is not contiguous",
                        detail={"expected": previous + 1, "actual": event.sequence},
                    )
                self._events.append(event)
                previous = event.sequence

    def events(self) -> tuple[SkillCanonicalEvent, ...]:
        with self._lock:
            return tuple(self._events)


@dataclass(frozen=True, slots=True)
class SkillSessionAggregate:
    schema_version: int
    run_id: str
    task_id: str
    session_id: str
    agent_id: str
    status: SkillSessionStatus
    revision: int
    last_event_sequence: int
    last_event_id: str
    runtime_state: dict[str, Any]
    disclosure_ledger: dict[str, Any]
    compact_references: tuple[dict[str, Any], ...]
    outcome_projections: tuple[dict[str, Any], ...]
    fork_handoffs: tuple[dict[str, Any], ...]
    registry_generation: int
    source_composition_id: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported SkillSessionAggregate schema")
        if not self.run_id or not self.task_id or not self.session_id or not self.agent_id:
            raise ValueError("skill session aggregate identity is incomplete")
        if self.revision < 0 or self.last_event_sequence < 0:
            raise ValueError("skill session aggregate counters cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "status": str(self.status),
            "revision": self.revision,
            "last_event_sequence": self.last_event_sequence,
            "last_event_id": self.last_event_id,
            "runtime_state": copy.deepcopy(self.runtime_state),
            "disclosure_ledger": copy.deepcopy(self.disclosure_ledger),
            "compact_references": [copy.deepcopy(item) for item in self.compact_references],
            "outcome_projections": [copy.deepcopy(item) for item in self.outcome_projections],
            "fork_handoffs": [copy.deepcopy(item) for item in self.fork_handoffs],
            "registry_generation": self.registry_generation,
            "source_composition_id": self.source_composition_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "body_in_checkpoint": False,
            "callbacks_in_checkpoint": False,
            "authority_in_checkpoint": False,
        }
        payload["aggregate_digest"] = digest_object(payload)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SkillSessionAggregate":
        raw = dict(value)
        supplied_digest = str(raw.pop("aggregate_digest", ""))
        if supplied_digest and supplied_digest != digest_object(raw):
            raise SkillCheckpointCorrupt("skill session aggregate digest mismatch")
        runtime_state = raw.get("runtime_state")
        disclosure_ledger = raw.get("disclosure_ledger")
        if not isinstance(runtime_state, Mapping) or not isinstance(disclosure_ledger, Mapping):
            raise SkillCheckpointCorrupt("skill session aggregate state is invalid")
        try:
            return cls(
                schema_version=int(raw.get("schema_version") or 0),
                run_id=str(raw.get("run_id") or ""),
                task_id=str(raw.get("task_id") or ""),
                session_id=str(raw.get("session_id") or ""),
                agent_id=str(raw.get("agent_id") or ""),
                status=SkillSessionStatus(str(raw.get("status") or "")),
                revision=int(raw.get("revision") or 0),
                last_event_sequence=int(raw.get("last_event_sequence") or 0),
                last_event_id=str(raw.get("last_event_id") or ""),
                runtime_state=dict(runtime_state),
                disclosure_ledger=dict(disclosure_ledger),
                compact_references=tuple(dict(item) for item in raw.get("compact_references") or ()),
                outcome_projections=tuple(dict(item) for item in raw.get("outcome_projections") or ()),
                fork_handoffs=tuple(dict(item) for item in raw.get("fork_handoffs") or ()),
                registry_generation=int(raw.get("registry_generation") or 0),
                source_composition_id=str(raw.get("source_composition_id") or ""),
                created_at=str(raw.get("created_at") or utc_now()),
                updated_at=str(raw.get("updated_at") or utc_now()),
            )
        except (TypeError, ValueError) as error:
            raise SkillCheckpointCorrupt("skill session aggregate is invalid") from error


class SkillSessionIntegrationRuntime:
    """One 02B/02D session aggregate around the complete 03C lifecycle."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        runtime: Any,
        run_id: str,
        task_id: str,
        session_id: str,
        agent_id: str,
        event_port: SkillCanonicalEventPort | None = None,
        aggregate_snapshot: Mapping[str, Any] | None = None,
        source_composition_id: str = "",
    ) -> None:
        self.runtime = runtime
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.session_id = str(session_id)
        self.agent_id = str(agent_id)
        self.event_port = event_port or InMemorySkillCanonicalEventPort()
        self.source_composition_id = source_composition_id
        self._lock = RLock()
        if aggregate_snapshot:
            aggregate = SkillSessionAggregate.from_dict(aggregate_snapshot)
            self._validate_aggregate_identity(aggregate)
            self._aggregate = aggregate
            self.disclosure_ledger = SkillDisclosureLedger(
                session_id=self.session_id,
                snapshot=aggregate.disclosure_ledger,
            )
        else:
            self.disclosure_ledger = SkillDisclosureLedger(session_id=self.session_id)
            self._aggregate = self._project_aggregate(
                status=SkillSessionStatus.OPEN,
                revision=0,
                last_event_sequence=0,
                last_event_id="",
            )
        self.disclosure_renderer = SkillDisclosureRenderer(
            runtime=self.runtime,
            ledger=self.disclosure_ledger,
        )

    def invoke(
        self,
        request: SkillInvocationRequest,
        *,
        workspace_paths: Sequence[str] = (),
        load_resources: Sequence[str] = (),
        require_model_invocable: bool = False,
        require_user_invocable: bool = True,
    ) -> SkillInvocationPlan:
        self._ensure_open()
        self._validate_request_identity(request)
        plan = self.runtime.invoke(
            request,
            workspace_paths=workspace_paths,
            load_resources=load_resources,
            require_model_invocable=require_model_invocable,
            require_user_invocable=require_user_invocable,
        )
        envelope = self.disclosure_ledger.append_plan(plan)
        events = self._events_for_plan(plan, envelope)
        self._append_events(events)
        self._refresh_aggregate()
        return plan

    def render_next_disclosures(
        self,
        *,
        worker_request_id: str,
        total_token_budget: int,
        maximum: int = 8,
    ) -> tuple[SkillDisclosureRender, ...]:
        self._ensure_open()
        rendered: list[SkillDisclosureRender] = []
        remaining = total_token_budget
        for envelope in self.disclosure_ledger.pending(agent_id=self.agent_id)[:maximum]:
            try:
                item = self.disclosure_renderer.render(
                    envelope.disclosure_id,
                    request_id=worker_request_id,
                    agent_id=self.agent_id,
                    total_token_budget=remaining,
                )
            except SkillRestoreMessageRejected:
                self.disclosure_ledger.invalidate_invocation(
                    envelope.invocation_id,
                    reason="disclosure mode/status cannot enter this session",
                )
                continue
            rendered.append(item)
            remaining -= item.token_estimate
            if remaining <= 0:
                break
        return tuple(rendered)

    def commit_disclosures(
        self,
        rendered: Sequence[SkillDisclosureRender],
        *,
        turn_id: str,
    ) -> tuple[SkillDisclosureEnvelope, ...]:
        committed = tuple(
            self.disclosure_renderer.commit(item, turn_id=turn_id) for item in rendered
        )
        events = [
            self._new_event(
                "skill_disclosure_consumed",
                invocation_id=item.invocation_id,
                skill_ref=item.version_ref.immutable_ref,
                worker_request_id=item.claim.request_id,
                payload={
                    "disclosure_id": item.disclosure_id,
                    "turn_id": turn_id,
                    "render_digest": item.render_digest,
                    "token_estimate": item.token_estimate,
                },
            )
            for item in rendered
        ]
        self._append_events(events)
        self._refresh_aggregate()
        return committed

    def abort_disclosures(
        self,
        rendered: Sequence[SkillDisclosureRender],
        *,
        reason: str,
    ) -> None:
        for item in rendered:
            self.disclosure_renderer.abort(item, reason=reason)
        if rendered:
            self._append_events(
                [
                    self._new_event(
                        "skill_disclosure_released",
                        invocation_id=item.invocation_id,
                        skill_ref=item.version_ref.immutable_ref,
                        worker_request_id=item.claim.request_id,
                        payload={"reason": reason, "disclosure_id": item.disclosure_id},
                    )
                    for item in rendered
                ]
            )
            self._refresh_aggregate()

    def complete(
        self,
        invocation_id: str,
        *,
        outcome_refs: Iterable[str] = (),
        evidence_refs: Iterable[str] = (),
        artifact_refs: Iterable[str] = (),
    ) -> InvokedSkillState:
        self._ensure_open()
        state = self.runtime.state_store.get(invocation_id)
        self._validate_state_identity(state)
        terminal = self.runtime.invocation_runtime.complete(
            invocation_id,
            outcome_refs=outcome_refs,
            evidence_refs=evidence_refs,
            artifact_refs=artifact_refs,
        )
        self.disclosure_ledger.invalidate_invocation(
            invocation_id,
            reason="skill invocation reached terminal state",
        )
        outcome = self.runtime.compact_bridge.memory_projection(terminal).to_dict()
        self._append_events(
            [
                self._new_event(
                    "skill_completed",
                    invocation_id=invocation_id,
                    skill_ref=terminal.version_ref.immutable_ref,
                    payload={
                        "status": str(terminal.status),
                        "outcome_projection": outcome,
                        "body_persisted": False,
                    },
                )
            ]
        )
        self._refresh_aggregate()
        return terminal

    def cancel(self, invocation_id: str, *, reason: str) -> InvokedSkillState:
        self._ensure_open()
        state = self.runtime.state_store.get(invocation_id)
        self._validate_state_identity(state)
        terminal = self.runtime.invocation_runtime.cancel(invocation_id, reason=reason)
        self.disclosure_ledger.invalidate_invocation(invocation_id, reason=reason)
        self._append_events(
            [
                self._new_event(
                    "skill_cancelled",
                    invocation_id=invocation_id,
                    skill_ref=terminal.version_ref.immutable_ref,
                    payload={"status": str(terminal.status), "reason": reason},
                )
            ]
        )
        self._refresh_aggregate()
        return terminal

    def compact_restore(self, references: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
        self._ensure_open()
        restored: list[Any] = []
        for raw in references:
            reference = compact_reference_from_dict(dict(raw))
            if reference.session_id != self.session_id or reference.agent_id != self.agent_id:
                raise SkillSessionOwnershipError("skill compact reference crossed session/agent scope")
            if reference.status is SkillInvocationStatus.FORK_PENDING:
                continue
            if reference.status.terminal:
                continue
            restored.extend(
                self.runtime.compact_bridge.restore(
                    (reference,),
                    session_id=self.session_id,
                    agent_id=self.agent_id,
                )
            )
        self._append_events(
            [
                self._new_event(
                    "skill_compact_restored",
                    payload={
                        "requested_count": len(references),
                        "restored_count": len(restored),
                        "fork_body_restored": False,
                        "terminal_body_restored": False,
                    },
                )
            ]
        )
        self._refresh_aggregate()
        return tuple(restored)

    def close(self, *, reason: str = "session_closed") -> SkillSessionAggregate:
        with self._lock:
            if self._aggregate.status is SkillSessionStatus.CLOSED:
                return self._aggregate
        states = self.runtime.invocation_runtime.session_end(self.session_id)
        for state in states:
            self.disclosure_ledger.invalidate_invocation(state.invocation_id, reason=reason)
        self._append_events(
            [self._new_event("skill_session_closed", payload={"reason": reason})]
        )
        self._refresh_aggregate(status=SkillSessionStatus.CLOSED)
        return self.aggregate()

    def aggregate(self) -> SkillSessionAggregate:
        with self._lock:
            return self._aggregate

    def checkpoint(self) -> dict[str, Any]:
        return self.aggregate().to_dict()

    def _events_for_plan(
        self,
        plan: SkillInvocationPlan,
        envelope: SkillDisclosureEnvelope,
    ) -> list[SkillCanonicalEvent]:
        events = [
            self._new_event(
                "skill_invoked",
                invocation_id=plan.state.invocation_id,
                skill_ref=plan.revision.version_ref.immutable_ref,
                tool_call_id=plan.request.parent_tool_use_id,
                worker_request_id=plan.request.worker_request_id,
                payload={
                    "mode": str(plan.revision.metadata.invocation.mode),
                    "status": str(plan.state.status),
                    "policy_digest": plan.policy_snapshot.policy_digest,
                    "disclosure_id": envelope.disclosure_id,
                    "disclosure_kind": str(envelope.kind),
                    "body_persisted": False,
                },
            ),
            self._new_event(
                "allowed_tools_delta",
                invocation_id=plan.state.invocation_id,
                skill_ref=plan.revision.version_ref.immutable_ref,
                tool_call_id=plan.request.parent_tool_use_id,
                payload={
                    "policy_snapshot": plan.policy_snapshot.to_dict(),
                    "permission_authority": False,
                },
            ),
        ]
        if plan.revision.metadata.invocation.mode is SkillInvocationMode.FORK:
            events.append(
                self._new_event(
                    "skill_fork_handoff",
                    invocation_id=plan.state.invocation_id,
                    skill_ref=plan.revision.version_ref.immutable_ref,
                    payload={
                        "fork_request": plan.fork_request.to_dict() if plan.fork_request else None,
                        "parent_body_disclosed": False,
                    },
                )
            )
        else:
            events.append(
                self._new_event(
                    "skill_disclosure_pending",
                    invocation_id=plan.state.invocation_id,
                    skill_ref=plan.revision.version_ref.immutable_ref,
                    payload={
                        "disclosure_id": envelope.disclosure_id,
                        "attachment_refs": list(envelope.attachment_refs),
                        "resource_refs": list(envelope.selected_resource_refs),
                    },
                )
            )
        return events

    def _new_event(
        self,
        event_type: str,
        *,
        invocation_id: str = "",
        skill_ref: str = "",
        tool_call_id: str = "",
        worker_request_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> SkillCanonicalEvent:
        with self._lock:
            sequence = self._aggregate.last_event_sequence + 1
            parent = self._aggregate.last_event_id
        event_id = digest_object(
            {
                "session_id": self.session_id,
                "sequence": sequence,
                "event_type": event_type,
                "invocation_id": invocation_id,
                "skill_ref": skill_ref,
                "parent": parent,
            }
        )[:32]
        return SkillCanonicalEvent(
            event_id=event_id,
            sequence=sequence,
            run_id=self.run_id,
            task_id=self.task_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            event_type=event_type,
            invocation_id=invocation_id,
            skill_ref=skill_ref,
            parent_event_id=parent,
            tool_call_id=tool_call_id,
            worker_request_id=worker_request_id,
            payload=dict(payload or {}),
        )

    def _append_events(self, events: Sequence[SkillCanonicalEvent]) -> None:
        if not events:
            return
        with self._lock:
            expected_sequence = self._aggregate.last_event_sequence + 1
            expected_parent = self._aggregate.last_event_id
            normalized: list[SkillCanonicalEvent] = []
            for event in events:
                if event.sequence != expected_sequence:
                    event = replace(
                        event,
                        sequence=expected_sequence,
                        parent_event_id=expected_parent,
                        event_id=digest_object(
                            {
                                "session_id": self.session_id,
                                "sequence": expected_sequence,
                                "event_type": event.event_type,
                                "invocation_id": event.invocation_id,
                                "skill_ref": event.skill_ref,
                                "parent": expected_parent,
                            }
                        )[:32],
                    )
                if event.parent_event_id != expected_parent:
                    raise SkillEventCausalityError("skill event parent does not match aggregate head")
                normalized.append(event)
                expected_sequence += 1
                expected_parent = event.event_id
            try:
                self.event_port.append(normalized)
            except Exception as error:
                raise SkillEventAppendError("failed to append canonical skill events") from error
            self._aggregate = replace(
                self._aggregate,
                last_event_sequence=normalized[-1].sequence,
                last_event_id=normalized[-1].event_id,
                revision=self._aggregate.revision + 1,
                updated_at=utc_now(),
            )

    def _refresh_aggregate(self, *, status: SkillSessionStatus | None = None) -> None:
        with self._lock:
            current = self._aggregate
            self._aggregate = self._project_aggregate(
                status=status or current.status,
                revision=current.revision + 1,
                last_event_sequence=current.last_event_sequence,
                last_event_id=current.last_event_id,
            )

    def _project_aggregate(
        self,
        *,
        status: SkillSessionStatus,
        revision: int,
        last_event_sequence: int,
        last_event_id: str,
    ) -> SkillSessionAggregate:
        checkpoint = self.runtime.session_bridge.checkpoint(
            session_id=self.session_id,
            agent_id=self.agent_id,
            runtime_state_snapshot=self.runtime.state_snapshot(),
        )
        outcomes: list[dict[str, Any]] = []
        forks: list[dict[str, Any]] = []
        for state in self.runtime.state_store.all_states():
            if state.session_id != self.session_id:
                continue
            if state.status.terminal:
                outcomes.append(self.runtime.compact_bridge.memory_projection(state).to_dict())
            if state.status is SkillInvocationStatus.FORK_PENDING:
                raw = self.runtime.state_snapshot().get("fork_handoff", {})
                forks.append(copy.deepcopy(raw))
        return SkillSessionAggregate(
            schema_version=self.SCHEMA_VERSION,
            run_id=self.run_id,
            task_id=self.task_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            status=status,
            revision=revision,
            last_event_sequence=last_event_sequence,
            last_event_id=last_event_id,
            runtime_state=checkpoint.to_dict(),
            disclosure_ledger=self.disclosure_ledger.snapshot().to_dict(include_arguments=True),
            compact_references=tuple(item.to_dict() for item in checkpoint.compact_references),
            outcome_projections=tuple(outcomes),
            fork_handoffs=tuple(forks),
            registry_generation=self.runtime.registry.generation,
            source_composition_id=self.source_composition_id,
            created_at=getattr(self, "_aggregate", None).created_at if hasattr(self, "_aggregate") else utc_now(),
        )

    def _ensure_open(self) -> None:
        if self.aggregate().status is not SkillSessionStatus.OPEN:
            raise SkillSessionClosed(
                "skill session is not open",
                detail={"status": str(self.aggregate().status)},
            )

    def _validate_request_identity(self, request: SkillInvocationRequest) -> None:
        if (
            request.run_id != self.run_id
            or request.task_id != self.task_id
            or request.session_id != self.session_id
            or request.agent_id != self.agent_id
        ):
            raise SkillSessionOwnershipError("skill invocation request crossed session scope")

    def _validate_state_identity(self, state: InvokedSkillState) -> None:
        if (
            state.run_id != self.run_id
            or state.task_id != self.task_id
            or state.session_id != self.session_id
            or state.agent_id != self.agent_id
        ):
            raise SkillSessionOwnershipError("invoked skill state crossed session scope")

    def _validate_aggregate_identity(self, aggregate: SkillSessionAggregate) -> None:
        if (
            aggregate.run_id != self.run_id
            or aggregate.task_id != self.task_id
            or aggregate.session_id != self.session_id
            or aggregate.agent_id != self.agent_id
        ):
            raise SkillSessionOwnershipError("skill session aggregate identity mismatch")
