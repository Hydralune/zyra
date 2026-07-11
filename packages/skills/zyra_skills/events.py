from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .models import InvokedSkillState, SkillInvocationPlan, SkillInvocationRequest, SkillRevision, utc_now


class SkillRuntimeEventKind(StrEnum):
    DISCOVERY_STARTED = "skill_discovery_started"
    DISCOVERY_COMPLETED = "skill_discovery_completed"
    RELOAD_REJECTED = "skill_reload_rejected"
    REGISTRY_SWAPPED = "skill_registry_swapped"
    INVOCATION_REQUESTED = "skill_invocation_requested"
    REVISION_RESOLVED = "skill_revision_resolved"
    BODY_LOADED = "skill_body_loaded"
    POLICY_BOUND = "skill_policy_bound"
    HOOKS_REGISTERED = "skill_hooks_registered"
    INLINE_READY = "skill_inline_ready"
    FORK_PENDING = "skill_fork_pending"
    INVOCATION_COMPLETED = "skill_invocation_completed"
    INVOCATION_FAILED = "skill_invocation_failed"
    INVOCATION_CANCELLED = "skill_invocation_cancelled"
    REVISION_REVOKED = "skill_revision_revoked"
    COMPACT_RESTORED = "skill_compact_restored"


@dataclass(frozen=True, slots=True)
class SkillRuntimeEvent:
    kind: SkillRuntimeEventKind
    run_id: str
    task_id: str
    session_id: str
    invocation_id: str = ""
    agent_id: str = ""
    node_id: str | None = None
    cause_event_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "agent_id": self.agent_id,
            "node_id": self.node_id,
            "cause_event_id": self.cause_event_id,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }

    def to_event_record(self) -> Any:
        try:
            from zyra_core import EventRecord, EventType
        except ImportError:
            return self.to_dict()
        event_type = EventType.SKILL_INVOKED
        return EventRecord(
            run_id=self.run_id,
            task_id=self.task_id,
            event_type=event_type,
            node_id=self.node_id,
            payload={
                "skill_runtime": self.to_dict(),
                "skill_invocation": dict(self.payload.get("skill_invocation") or {}),
            },
        )


class SkillEventProjector:
    def invocation_events(
        self,
        request: SkillInvocationRequest,
        *,
        revision: SkillRevision,
        plan: SkillInvocationPlan,
        cause_event_id: str = "",
    ) -> tuple[SkillRuntimeEvent, ...]:
        common = {
            "run_id": request.run_id,
            "task_id": request.task_id,
            "session_id": request.session_id,
            "invocation_id": request.invocation_id,
            "agent_id": request.agent_id,
            "node_id": request.node_id,
        }
        requested = SkillRuntimeEvent(
            kind=SkillRuntimeEventKind.INVOCATION_REQUESTED,
            cause_event_id=cause_event_id,
            payload={
                "skill_invocation": {
                    "invocation_id": request.invocation_id,
                    "skill_name": revision.metadata.name,
                    "qualified_name": revision.qualified_name,
                    "arguments_digest_only": True,
                    "status": "requested",
                }
            },
            **common,
        )
        resolved = SkillRuntimeEvent(
            kind=SkillRuntimeEventKind.REVISION_RESOLVED,
            cause_event_id="",
            payload={"version_ref": revision.version_ref.to_dict()},
            **common,
        )
        body = SkillRuntimeEvent(
            kind=SkillRuntimeEventKind.BODY_LOADED,
            payload={
                "immutable_ref": plan.body.version_ref.immutable_ref,
                "body_digest": plan.body.version_ref.body_digest,
                "token_estimate": plan.body.token_estimate,
                "body_in_event": False,
            },
            **common,
        )
        policy = SkillRuntimeEvent(
            kind=SkillRuntimeEventKind.POLICY_BOUND,
            payload={
                "policy_snapshot_id": plan.policy_snapshot.snapshot_id,
                "policy_digest": plan.policy_snapshot.policy_digest,
                "permission_owner": "M1-03A",
                "grant_in_event": False,
            },
            **common,
        )
        ready_kind = (
            SkillRuntimeEventKind.FORK_PENDING
            if plan.fork_request is not None
            else SkillRuntimeEventKind.INLINE_READY
        )
        ready = SkillRuntimeEvent(
            kind=ready_kind,
            payload={
                "status": str(plan.state.status),
                "attachment_refs": [item.immutable_ref for item in plan.attachments],
                "fork_request": plan.fork_request.to_dict() if plan.fork_request else None,
            },
            **common,
        )
        return (requested, resolved, body, policy, ready)

    def terminal_event(
        self,
        state: InvokedSkillState,
        *,
        cause_event_id: str = "",
    ) -> SkillRuntimeEvent:
        status_map = {
            "completed": SkillRuntimeEventKind.INVOCATION_COMPLETED,
            "cancelled": SkillRuntimeEventKind.INVOCATION_CANCELLED,
            "revoked": SkillRuntimeEventKind.REVISION_REVOKED,
        }
        kind = status_map.get(str(state.status), SkillRuntimeEventKind.INVOCATION_FAILED)
        return SkillRuntimeEvent(
            kind=kind,
            run_id=state.run_id,
            task_id=state.task_id,
            session_id=state.session_id,
            invocation_id=state.invocation_id,
            agent_id=state.agent_id,
            cause_event_id=cause_event_id,
            payload={
                "status": str(state.status),
                "version_ref": state.version_ref.to_dict(),
                "outcome_refs": list(state.outcome_refs),
                "evidence_refs": list(state.evidence_refs),
                "artifact_refs": list(state.artifact_refs),
                "error_code": state.error_code,
            },
        )


def event_records(events: Iterable[SkillRuntimeEvent]) -> tuple[Any, ...]:
    return tuple(event.to_event_record() for event in events)
