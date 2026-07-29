from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .contracts import (
    BridgeCommand,
    LoopXClaimConflictError,
    stable_digest,
)


LOOPX_STATE_EVENT_SCHEMA = "loopx_state_event_v0"
LOOPX_STATE_PROJECTION_VERSION = "event_sourced_state_contract_v0"
LOOPX_BRIDGE_PRODUCER = "zyra.loopx.bridge"

TODO_ADDED = "todo_added"
TODO_CLAIMED = "todo_claimed"
RUN_RECORDED = "run_recorded"
QUOTA_SPENT = "quota_spent"
EVIDENCE_ATTACHED = "evidence_attached"


def _event_id(command: BridgeCommand, kind: str, identity: str = "") -> str:
    digest = stable_digest(
        {
            "idempotency_key": command.idempotency_key,
            "mapping_version": command.mapping_version,
            "kind": kind,
            "identity": identity,
        }
    )
    return f"zyra-bridge-{kind}-{digest[:24]}"


def _event(
    command: BridgeCommand,
    *,
    kind: str,
    event_type: str,
    identity: str = "",
    refs: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": LOOPX_STATE_EVENT_SCHEMA,
        "event_id": _event_id(command, kind, identity),
        "goal_id": command.update.goal_id,
        "event_type": event_type,
        "recorded_at": command.created_at,
        "producer": LOOPX_BRIDGE_PRODUCER,
        "privacy": "local_private",
        "projection_version": LOOPX_STATE_PROJECTION_VERSION,
        "refs": {
            "zyra_commit_id": command.canonical_commit.commit_id,
            "zyra_delta_id": command.canonical_commit.delta_id,
            "zyra_causation_id": command.causation_id,
            "zyra_correlation_id": command.correlation_id,
            **dict(refs or {}),
        },
        "payload": {
            "bridge_schema": command.schema,
            "mapping_version": command.mapping_version,
            "idempotency_key": command.idempotency_key,
            **dict(payload or {}),
        },
    }


def quota_spent_slots(events: Iterable[Mapping[str, Any]]) -> int:
    spent = 0
    for event in events:
        if (
            event.get("producer") != LOOPX_BRIDGE_PRODUCER
            or event.get("event_type") != QUOTA_SPENT
        ):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        try:
            spent += max(0, int(payload.get("slots") or 0))
        except (TypeError, ValueError):
            continue
    return spent


def projected_claims(projection: Mapping[str, Any]) -> dict[str, str]:
    claims: dict[str, str] = {}
    for summary_name in ("user_todos", "agent_todos"):
        summary = projection.get(summary_name)
        items = summary.get("items") if isinstance(summary, Mapping) else ()
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, Mapping):
                continue
            todo_id = str(item.get("todo_id") or "")
            claimed_by = str(item.get("claimed_by") or "")
            if todo_id and claimed_by:
                claims[todo_id] = claimed_by
    return claims


def projected_todo_ids(projection: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for summary_name in ("user_todos", "agent_todos"):
        summary = projection.get(summary_name)
        items = summary.get("items") if isinstance(summary, Mapping) else ()
        for item in items if isinstance(items, list) else ():
            if isinstance(item, Mapping) and item.get("todo_id"):
                result.add(str(item["todo_id"]))
    return result


@dataclass(frozen=True, slots=True)
class StateMappingPlan:
    events: tuple[dict[str, Any], ...]
    requested_spend_slots: int
    spend_applied_slots: int
    spend_rejected_slots: int
    prior_spent_slots: int
    quota_limit_slots: int
    quota_exhausted: bool
    failed_spend_gates: tuple[str, ...]
    interaction_accepted: bool


class LoopXStateMapper:
    """Thin, deterministic mapping from committed Zyra refs to LoopX events."""

    def plan(
        self,
        command: BridgeCommand,
        *,
        current_events: Iterable[Mapping[str, Any]],
        current_projection: Mapping[str, Any],
    ) -> StateMappingPlan:
        materialized_events = tuple(dict(item) for item in current_events)
        existing_todos = projected_todo_ids(current_projection)
        incoming_todos = {item.todo_id for item in command.update.todos}
        claims = projected_claims(current_projection)
        for request in command.update.claims:
            existing = claims.get(request.todo_id)
            if existing and existing != request.claimant:
                raise LoopXClaimConflictError(
                    "LoopX private claim conflicts with an existing claimant.",
                    code="loopx_claim_conflict",
                    details={
                        "goal_id": command.update.goal_id,
                        "todo_id": request.todo_id,
                        "existing_claimant": existing,
                        "requested_claimant": request.claimant,
                        "worker_lease_changed": False,
                    },
                )
            if request.todo_id not in existing_todos | incoming_todos:
                raise LoopXClaimConflictError(
                    "LoopX private claim references an unknown todo.",
                    code="loopx_claim_unknown_todo",
                    details={
                        "goal_id": command.update.goal_id,
                        "todo_id": request.todo_id,
                        "worker_lease_changed": False,
                    },
                )

        events: list[dict[str, Any]] = [
            _event(
                command,
                kind="goal",
                event_type=RUN_RECORDED,
                payload={
                    "bridge_kind": "goal_synced",
                    "summary": "Zyra committed objective reference synchronized",
                    "objective_ref": command.update.objective_ref,
                    "requirement_revision": command.update.requirement_revision,
                    "canonical_revision": command.canonical_commit.committed_revision,
                    "canonical_owner": "GraphStateCustody",
                    "loopx_owner": "loopx_private_control",
                },
            )
        ]
        for todo in command.update.todos:
            events.append(
                _event(
                    command,
                    kind="todo",
                    identity=todo.todo_id,
                    event_type=TODO_ADDED,
                    refs={"todo_id": todo.todo_id},
                    payload={
                        "role": todo.role,
                        "priority": todo.priority,
                        "title": todo.title,
                        "action_kind": todo.action_kind,
                        "continuation_policy": todo.continuation_policy,
                    },
                )
            )
        for claim in command.update.claims:
            events.append(
                _event(
                    command,
                    kind="claim",
                    identity=claim.todo_id,
                    event_type=TODO_CLAIMED,
                    refs={"todo_id": claim.todo_id},
                    payload={
                        "claimed_by": claim.claimant,
                        "claim_owner": "loopx_private_control",
                        "worker_lease_changed": False,
                    },
                )
            )

        requested = command.update.quota.requested_spend_slots
        prior_spent = quota_spent_slots(
            event
            for event in materialized_events
            if not (
                isinstance(event.get("payload"), Mapping)
                and event["payload"].get("idempotency_key")
                == command.idempotency_key
            )
        )
        remaining = max(0, command.update.quota.limit_slots - prior_spent)
        gate_allowed = command.update.validation.spend_allowed
        spend = min(requested, remaining) if gate_allowed else 0
        exhausted = requested > 0 and gate_allowed and spend < requested
        rejected = requested - spend
        validation_payload = {
            **command.update.validation.to_dict(),
            "requested_slots": requested,
            "spend_applied_slots": spend,
            "spend_rejected_slots": rejected,
            "prior_spent_slots": prior_spent,
            "quota_limit_slots": command.update.quota.limit_slots,
            "quota_owner": "loopx_private_control",
            "zyra_execution_budget_changed": False,
        }
        if spend:
            events.append(
                _event(
                    command,
                    kind="quota-spend",
                    event_type=QUOTA_SPENT,
                    payload={
                        **validation_payload,
                        "slots": spend,
                        "summary": f"validated LoopX private quota spend: {spend} slot(s)",
                    },
                )
            )
        elif requested:
            events.append(
                _event(
                    command,
                    kind="quota-rejected",
                    event_type=RUN_RECORDED,
                    payload={
                        **validation_payload,
                        "bridge_kind": (
                            "quota_exhausted" if exhausted else "quota_spend_rejected"
                        ),
                        "summary": (
                            "LoopX private quota exhausted; no spend recorded"
                            if exhausted
                            else "Zyra validation gate rejected LoopX spend; no spend recorded"
                        ),
                    },
                )
            )

        for history in command.update.history:
            events.append(
                _event(
                    command,
                    kind="history",
                    identity=history.source_event_id,
                    event_type=(
                        EVIDENCE_ATTACHED if history.verified else RUN_RECORDED
                    ),
                    refs={
                        "source_event_id": history.source_event_id,
                        "evidence_refs": list(history.evidence_refs),
                    },
                    payload={
                        "bridge_kind": (
                            "verified_history" if history.verified else "unverified_history_rejected"
                        ),
                        "verified": history.verified,
                        "summary": history.summary,
                    },
                )
            )

        interaction_accepted = bool(
            command.update.interaction
            and command.update.validation.validation_passed
            and command.update.validation.permission_allowed
        )
        if command.update.interaction:
            interaction = command.update.interaction
            events.append(
                _event(
                    command,
                    kind=(
                        "interaction" if interaction_accepted else "interaction-rejected"
                    ),
                    identity=interaction.input_ref,
                    event_type=RUN_RECORDED,
                    refs={
                        "interaction_input_ref": interaction.input_ref,
                        "interaction_feedback_ref": interaction.feedback_ref,
                    },
                    payload={
                        "bridge_kind": (
                            "interaction_synced"
                            if interaction_accepted
                            else "interaction_permission_rejected"
                        ),
                        "summary": (
                            "permission-checked interaction synchronized"
                            if interaction_accepted
                            else "interaction rejected before LoopX continuation"
                        ),
                        "continuation_hint": (
                            interaction.continuation_hint
                            if interaction_accepted
                            else ""
                        ),
                        "permission_receipt_id": (
                            command.update.validation.permission_receipt_id
                        ),
                    },
                )
            )

        events.append(
            _event(
                command,
                kind="sync",
                event_type=RUN_RECORDED,
                payload={
                    "bridge_kind": "sync_validated",
                    "summary": "Zyra to LoopX state mapping validated",
                    "spend_applied_slots": spend,
                    "spend_rejected_slots": rejected,
                    "quota_exhausted": exhausted,
                    "interaction_accepted": interaction_accepted,
                    "canonical_receipt_digest": (
                        command.canonical_commit.receipt_digest
                    ),
                },
            )
        )
        return StateMappingPlan(
            events=tuple(events),
            requested_spend_slots=requested,
            spend_applied_slots=spend,
            spend_rejected_slots=rejected,
            prior_spent_slots=prior_spent,
            quota_limit_slots=command.update.quota.limit_slots,
            quota_exhausted=exhausted,
            failed_spend_gates=command.update.validation.failed_gates,
            interaction_accepted=interaction_accepted,
        )


__all__ = [
    "EVIDENCE_ATTACHED",
    "LOOPX_BRIDGE_PRODUCER",
    "LoopXStateMapper",
    "QUOTA_SPENT",
    "RUN_RECORDED",
    "StateMappingPlan",
    "TODO_ADDED",
    "TODO_CLAIMED",
    "projected_claims",
    "projected_todo_ids",
    "quota_spent_slots",
]
