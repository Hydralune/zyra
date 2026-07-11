from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .compact_bridge import SkillCompactBridge, SkillCompactReference
from .digests import digest_object
from .events import SkillRuntimeEvent
from .models import (
    InvokedSkillState,
    SkillInvocationPlan,
    SkillInvocationStatus,
    SkillOutcomeProjection,
    utc_now,
)
from .state import SkillInvocationStateStore


@dataclass(frozen=True, slots=True)
class SkillSessionCheckpointProjection:
    session_id: str
    state_snapshot: dict[str, Any]
    active_invocation_ids: tuple[str, ...]
    compact_references: tuple[SkillCompactReference, ...]
    latest_outcomes: tuple[SkillOutcomeProjection, ...]
    checkpoint_digest: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state_snapshot": dict(self.state_snapshot),
            "active_invocation_ids": list(self.active_invocation_ids),
            "compact_references": [item.to_dict() for item in self.compact_references],
            "latest_outcomes": [item.to_dict() for item in self.latest_outcomes],
            "checkpoint_digest": self.checkpoint_digest,
            "created_at": self.created_at,
            "body_in_checkpoint": False,
            "authority_in_checkpoint": False,
        }


@dataclass(frozen=True, slots=True)
class SkillSessionMutation:
    invocation_id: str
    message_deltas: tuple[dict[str, Any], ...]
    attachment_deltas: tuple[dict[str, Any], ...]
    policy_snapshot: dict[str, Any]
    state: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    mutation_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "message_deltas": [dict(item) for item in self.message_deltas],
            "attachment_deltas": [dict(item) for item in self.attachment_deltas],
            "policy_snapshot": dict(self.policy_snapshot),
            "state": dict(self.state),
            "events": [dict(item) for item in self.events],
            "mutation_digest": self.mutation_digest,
        }


class SkillSessionBridge:
    """Maps 03C state into the existing 02B/02D session aggregate."""

    def __init__(
        self,
        *,
        state_store: SkillInvocationStateStore,
        compact_bridge: SkillCompactBridge,
    ) -> None:
        self.state_store = state_store
        self.compact_bridge = compact_bridge

    def mutation_for_plan(
        self,
        plan: SkillInvocationPlan,
        events: Iterable[SkillRuntimeEvent],
    ) -> SkillSessionMutation:
        messages = tuple(message.to_dict() for message in plan.messages)
        attachments = tuple(attachment.to_dict() for attachment in plan.attachments)
        event_payloads = tuple(event.to_dict() for event in events)
        state = plan.state.to_dict()
        policy = plan.policy_snapshot.to_dict()
        payload = {
            "invocation_id": plan.state.invocation_id,
            "messages": messages,
            "attachments": attachments,
            "policy": policy,
            "state": state,
            "events": event_payloads,
        }
        return SkillSessionMutation(
            invocation_id=plan.state.invocation_id,
            message_deltas=messages,
            attachment_deltas=attachments,
            policy_snapshot=policy,
            state=state,
            events=event_payloads,
            mutation_digest=digest_object(payload),
        )

    def checkpoint(
        self,
        *,
        session_id: str,
        agent_id: str,
        runtime_state_snapshot: Mapping[str, Any] | None = None,
    ) -> SkillSessionCheckpointProjection:
        snapshot = dict(runtime_state_snapshot or self.state_store.snapshot())
        all_states = self.state_store.all_for_session(session_id)
        active = tuple(state.invocation_id for state in all_states if not state.status.terminal)
        references = self.compact_bridge.references(
            all_states,
            session_id=session_id,
            agent_id=agent_id,
        )
        outcomes: list[SkillOutcomeProjection] = []
        for state in all_states:
            if state.policy_snapshot is None or not state.status.terminal:
                continue
            outcomes.append(self.compact_bridge.memory_projection(state))
        payload = {
            "session_id": session_id,
            "snapshot": snapshot,
            "active": active,
            "references": [item.to_dict() for item in references],
            "outcomes": [item.to_dict() for item in outcomes],
        }
        return SkillSessionCheckpointProjection(
            session_id=session_id,
            state_snapshot=snapshot,
            active_invocation_ids=active,
            compact_references=references,
            latest_outcomes=tuple(outcomes),
            checkpoint_digest=digest_object(payload),
        )

    def restore(self, checkpoint: Mapping[str, Any], *, session_id: str) -> None:
        snapshot = checkpoint.get("state_snapshot")
        if not isinstance(snapshot, Mapping):
            raise ValueError("skill session checkpoint is missing state_snapshot")
        expected = str(checkpoint.get("checkpoint_digest") or "")
        # Older outer checkpoints may not carry this bridge digest; the inner
        # state schema is still validated by SkillInvocationStateStore.
        self.state_store.restore(snapshot, session_id=session_id)
        if expected:
            # Runtime checkpoints carry adjacent budget/revision state.  This
            # bridge owns only the invocation aggregate, so compare its exact
            # durable subset instead of rejecting valid outer checkpoint data.
            durable_keys = ("schema", "revision", "states", "idempotency")
            expected_state = {key: snapshot.get(key) for key in durable_keys}
            restored_state = self.state_store.snapshot()
            actual_state = {key: restored_state.get(key) for key in durable_keys}
            if digest_object(expected_state) != digest_object(actual_state):
                raise ValueError("skill session checkpoint state digest mismatch")

    def memory_handoff(self, invocation_id: str) -> dict[str, Any]:
        state = self.state_store.get(invocation_id)
        projection = self.compact_bridge.memory_projection(state)
        payload = projection.to_dict()
        payload.update(
            {
                "owner": "M1-06C read-only consumer",
                "loader_owner": "M1-03C",
                "body_in_projection": False,
                "allowed_tools_authority": False,
                "mutable_policy": False,
            }
        )
        return payload
