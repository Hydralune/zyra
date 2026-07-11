from __future__ import annotations

from dataclasses import replace
from threading import RLock
from typing import Any, Iterable, Mapping

from .errors import SkillInvocationConflict, SkillInvocationStateError, SkillStateCorrupt
from .models import (
    SKILL_STATE_SCHEMA,
    InvokedSkillState,
    SkillInvocationStatus,
    SkillPolicySnapshot,
    SkillVersionRef,
)


class SkillInvocationStateStore:
    """03C aggregate embedded in the existing runtime state checkpoint.

    This object owns invoked-skill transitions but not run/session identity.
    Callers restore and persist its snapshot inside the existing 02B/02D
    session checkpoint. It intentionally has no independent SQLite database.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._states: dict[str, InvokedSkillState] = {}
        self._idempotency: dict[str, str] = {}
        self._active_by_session: dict[str, list[str]] = {}
        self._active_by_agent: dict[tuple[str, str], list[str]] = {}
        self._revision = 0

    def create(self, state: InvokedSkillState, *, idempotency_key: str = "") -> InvokedSkillState:
        with self._lock:
            if state.invocation_id in self._states:
                raise SkillInvocationConflict("skill invocation id already exists", detail={"invocation_id": state.invocation_id})
            if idempotency_key:
                existing_id = self._idempotency.get(idempotency_key)
                if existing_id:
                    existing = self._states.get(existing_id)
                    if existing is None:
                        raise SkillStateCorrupt("skill idempotency index references missing state")
                    return existing
            self._states[state.invocation_id] = state
            self._active_by_session.setdefault(state.session_id, []).append(state.invocation_id)
            self._active_by_agent.setdefault((state.session_id, state.agent_id), []).append(state.invocation_id)
            if idempotency_key:
                self._idempotency[idempotency_key] = state.invocation_id
            self._revision += 1
            return state

    def get(self, invocation_id: str) -> InvokedSkillState:
        with self._lock:
            state = self._states.get(invocation_id)
            if state is None:
                raise SkillInvocationStateError("skill invocation state was not found", detail={"invocation_id": invocation_id})
            return state

    def get_by_idempotency(self, idempotency_key: str) -> InvokedSkillState | None:
        with self._lock:
            invocation_id = self._idempotency.get(idempotency_key, "")
            return self._states.get(invocation_id) if invocation_id else None

    def transition(
        self,
        invocation_id: str,
        status: SkillInvocationStatus,
        *,
        expected_revision: int | None = None,
        **changes: Any,
    ) -> InvokedSkillState:
        with self._lock:
            current = self.get(invocation_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise SkillInvocationConflict(
                    "skill invocation revision changed",
                    detail={"expected": expected_revision, "actual": current.revision},
                )
            updated = current.transition(status, **changes)
            self._states[invocation_id] = updated
            if updated.status.terminal:
                self._remove_active_indexes(updated)
            self._revision += 1
            return updated

    def replace(self, state: InvokedSkillState, *, expected_revision: int | None = None) -> InvokedSkillState:
        with self._lock:
            current = self.get(state.invocation_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise SkillInvocationConflict("skill invocation compare-and-swap failed")
            if state.revision <= current.revision:
                state = replace(state, revision=current.revision + 1)
            self._states[state.invocation_id] = state
            if state.status.terminal:
                self._remove_active_indexes(state)
            self._revision += 1
            return state

    def active_for_session(self, session_id: str) -> tuple[InvokedSkillState, ...]:
        with self._lock:
            return tuple(
                self._states[invocation_id]
                for invocation_id in self._active_by_session.get(session_id, ())
                if invocation_id in self._states and not self._states[invocation_id].status.terminal
            )

    def active_for_agent(self, session_id: str, agent_id: str) -> tuple[InvokedSkillState, ...]:
        with self._lock:
            return tuple(
                self._states[invocation_id]
                for invocation_id in self._active_by_agent.get((session_id, agent_id), ())
                if invocation_id in self._states and not self._states[invocation_id].status.terminal
            )

    def all_for_session(self, session_id: str) -> tuple[InvokedSkillState, ...]:
        with self._lock:
            return tuple(state for state in self._states.values() if state.session_id == session_id)

    def all_states(self) -> tuple[InvokedSkillState, ...]:
        with self._lock:
            return tuple(self._states.values())

    def terminate_session(
        self,
        session_id: str,
        *,
        status: SkillInvocationStatus = SkillInvocationStatus.CANCELLED,
        reason: str = "session ended",
    ) -> tuple[InvokedSkillState, ...]:
        if not status.terminal:
            raise ValueError("session termination status must be terminal")
        terminated: list[InvokedSkillState] = []
        for state in self.active_for_session(session_id):
            terminated.append(
                self.transition(
                    state.invocation_id,
                    status,
                    error_code="skill_session_terminated",
                    error_message=reason,
                )
            )
        return tuple(terminated)

    def terminate_version(
        self,
        version_ref: SkillVersionRef,
        *,
        reason: str,
    ) -> tuple[InvokedSkillState, ...]:
        with self._lock:
            candidates = [
                state
                for state in self._states.values()
                if state.version_ref.content_digest == version_ref.content_digest and not state.status.terminal
            ]
        return tuple(
            self.transition(
                state.invocation_id,
                SkillInvocationStatus.REVOKED,
                error_code="skill_revision_revoked",
                error_message=reason,
            )
            for state in candidates
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": SKILL_STATE_SCHEMA,
                "revision": self._revision,
                "states": {key: state.to_dict() for key, state in self._states.items()},
                "idempotency": dict(self._idempotency),
            }

    def restore(self, snapshot: Mapping[str, Any], *, session_id: str | None = None) -> None:
        if str(snapshot.get("schema") or "") != SKILL_STATE_SCHEMA:
            raise SkillStateCorrupt("unsupported invoked skill state schema")
        raw_states = snapshot.get("states")
        if not isinstance(raw_states, Mapping):
            raise SkillStateCorrupt("invoked skill snapshot states must be a mapping")
        states: dict[str, InvokedSkillState] = {}
        for invocation_id, raw in raw_states.items():
            if not isinstance(raw, Mapping):
                raise SkillStateCorrupt("invalid invoked skill state payload")
            state = _state_from_dict(raw)
            if state.invocation_id != str(invocation_id):
                raise SkillStateCorrupt("invoked skill state key mismatch")
            if session_id is not None and state.session_id != session_id:
                continue
            states[state.invocation_id] = state
        idempotency = {
            str(key): str(value)
            for key, value in dict(snapshot.get("idempotency") or {}).items()
            if str(value) in states
        }
        with self._lock:
            self._states = states
            self._idempotency = idempotency
            self._active_by_session = {}
            self._active_by_agent = {}
            for state in states.values():
                if not state.status.terminal:
                    self._active_by_session.setdefault(state.session_id, []).append(state.invocation_id)
                    self._active_by_agent.setdefault((state.session_id, state.agent_id), []).append(state.invocation_id)
            self._revision = int(snapshot.get("revision") or 0)

    def _remove_active_indexes(self, state: InvokedSkillState) -> None:
        session_items = self._active_by_session.get(state.session_id, [])
        self._active_by_session[state.session_id] = [value for value in session_items if value != state.invocation_id]
        if not self._active_by_session[state.session_id]:
            self._active_by_session.pop(state.session_id, None)
        key = (state.session_id, state.agent_id)
        agent_items = self._active_by_agent.get(key, [])
        self._active_by_agent[key] = [value for value in agent_items if value != state.invocation_id]
        if not self._active_by_agent[key]:
            self._active_by_agent.pop(key, None)


def _state_from_dict(raw: Mapping[str, Any]) -> InvokedSkillState:
    version_raw = raw.get("version_ref")
    if not isinstance(version_raw, Mapping):
        raise SkillStateCorrupt("invoked skill version ref is missing")
    policy_raw = raw.get("policy_snapshot")
    policy = _policy_snapshot_from_dict(policy_raw) if isinstance(policy_raw, Mapping) else None
    return InvokedSkillState(
        invocation_id=str(raw.get("invocation_id") or ""),
        run_id=str(raw.get("run_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        session_id=str(raw.get("session_id") or ""),
        agent_id=str(raw.get("agent_id") or ""),
        version_ref=SkillVersionRef.from_dict(version_raw),
        status=SkillInvocationStatus(str(raw.get("status") or "requested")),
        policy_snapshot=policy,
        hook_lease_ids=tuple(str(value) for value in raw.get("hook_lease_ids") or ()),
        attachment_refs=tuple(str(value) for value in raw.get("attachment_refs") or ()),
        message_delta_refs=tuple(str(value) for value in raw.get("message_delta_refs") or ()),
        fork_request_id=str(raw.get("fork_request_id") or ""),
        outcome_refs=tuple(str(value) for value in raw.get("outcome_refs") or ()),
        evidence_refs=tuple(str(value) for value in raw.get("evidence_refs") or ()),
        artifact_refs=tuple(str(value) for value in raw.get("artifact_refs") or ()),
        error_code=str(raw.get("error_code") or ""),
        error_message=str(raw.get("error_message") or ""),
        requested_at=str(raw.get("requested_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        completed_at=str(raw.get("completed_at") or ""),
        revision=int(raw.get("revision") or 0),
    )


def _policy_snapshot_from_dict(raw: Mapping[str, Any]) -> SkillPolicySnapshot:
    version_raw = raw.get("version_ref")
    if not isinstance(version_raw, Mapping):
        raise SkillStateCorrupt("skill policy snapshot version ref is missing")
    from .models import ToolSelector

    tools_raw = raw.get("effective_tools")
    effective = None
    if tools_raw is not None:
        if not isinstance(tools_raw, list):
            raise SkillStateCorrupt("skill policy effective_tools must be a list or null")
        effective = tuple(ToolSelector.parse(value) for value in tools_raw)
    return SkillPolicySnapshot(
        snapshot_id=str(raw.get("snapshot_id") or ""),
        invocation_id=str(raw.get("invocation_id") or ""),
        session_id=str(raw.get("session_id") or ""),
        version_ref=SkillVersionRef.from_dict(version_raw),
        effective_tools=effective,
        parent_snapshot_ids=tuple(str(value) for value in raw.get("parent_snapshot_ids") or ()),
        policy_digest=str(raw.get("policy_digest") or ""),
        created_at=str(raw.get("created_at") or ""),
    )
