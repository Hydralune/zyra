from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .gateway import (
    AuthorizedBrowserAction,
    BrowserActionGateway,
    BrowserActionGatewayError,
    CompletedBrowserAction,
    PreparedBrowserAction,
)
from .models import ActionRequest, digest_value, stable_id
from .selector_guard import SelectorExpectation


class SequenceAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class SequenceState(StrEnum):
    RECEIVED = "received"
    PREFLIGHTED = "preflighted"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class SequenceIdentity:
    sequence_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    action_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_ids", tuple(self.action_ids))
        if not self.sequence_id or not self.run_id or not self.task_id or not self.worker_request_id:
            raise ValueError("browser action sequence identity is incomplete")
        if not self.action_ids or len(self.action_ids) != len(set(self.action_ids)):
            raise ValueError("browser action sequence requires unique action ids")

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_id": self.sequence_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "action_ids": list(self.action_ids),
        }


@dataclass(frozen=True, slots=True)
class PreflightedSequence:
    identity: SequenceIdentity
    prepared: tuple[PreparedBrowserAction, ...]
    state: SequenceState = SequenceState.PREFLIGHTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "prepared", tuple(self.prepared))
        if tuple(item.action_id for item in self.prepared) != self.identity.action_ids:
            raise ValueError("preflighted sequence actions do not match sequence identity")

    @property
    def digest(self) -> str:
        return digest_value(
            {
                "identity": self.identity.to_dict(),
                "receipts": [item.receipt.receipt_id for item in self.prepared],
                "request_digests": [item.request.request_digest for item in self.prepared],
            }
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "state": str(self.state),
            "digest": self.digest,
            "actions": [item.public_dict() for item in self.prepared],
        }


@dataclass(frozen=True, slots=True)
class AuthorizedSequence:
    preflight: PreflightedSequence
    actions: tuple[AuthorizedBrowserAction, ...]
    state: SequenceState = SequenceState.AUTHORIZED

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))
        if tuple(item.prepared.action_id for item in self.actions) != self.preflight.identity.action_ids:
            raise ValueError("authorized sequence actions do not match preflight identity")
        if not all(item.allowed for item in self.actions):
            raise ValueError("authorized browser sequence contains a non-allowed action")

    @property
    def authorization_digest(self) -> str:
        return digest_value(
            {
                "preflight": self.preflight.digest,
                "decisions": [item.permission.decision.decision_id for item in self.actions],
                "requests": [item.permission.decision.request_id for item in self.actions],
            }
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "preflight": self.preflight.public_dict(),
            "state": str(self.state),
            "authorization_digest": self.authorization_digest,
            "permissions": [item.permission.public_dict() for item in self.actions],
        }


@dataclass(frozen=True, slots=True)
class CompletedSequence:
    authorization: AuthorizedSequence
    actions: tuple[CompletedBrowserAction, ...]
    state: SequenceState
    failed_action_id: str = ""
    failure_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))
        if self.state not in {SequenceState.COMPLETED, SequenceState.PARTIAL, SequenceState.BLOCKED}:
            raise ValueError("completed browser sequence has invalid terminal state")

    def public_dict(self) -> dict[str, Any]:
        return {
            "authorization": self.authorization.public_dict(),
            "state": str(self.state),
            "completed_actions": [item.public_dict() for item in self.actions],
            "failed_action_id": self.failed_action_id,
            "failure_code": self.failure_code,
        }


class BrowserActionSequenceCoordinator:
    """Hashline-inspired all-actions-first browser sequence admission.

    The coordinator provides atomic *admission*: every schema, selector,
    network, file, secret, hook and permission check completes before any
    action grant is consumed or browser side effect is dispatched.  Browser
    side effects themselves cannot be transactionally rolled back, so a later
    runtime failure yields explicit PARTIAL state and never replays a consumed
    grant.  Planners should use stop-on-error sequences and recovery/replan.
    """

    def __init__(self, gateway: BrowserActionGateway, *, disabled: bool = False) -> None:
        self.gateway = gateway
        self.disabled = disabled
        self._states: dict[str, SequenceState] = {}
        self._digests: dict[str, str] = {}
        self._lock = threading.RLock()

    def preflight(
        self,
        requests: Sequence[ActionRequest],
        *,
        selector_expectations: Mapping[str, SelectorExpectation] | None = None,
    ) -> PreflightedSequence:
        self._ensure_available()
        identity = sequence_identity(requests)
        with self._lock:
            if identity.sequence_id in self._states:
                raise SequenceAdmissionError("sequence_replayed", "browser action sequence id is already known")
            self._states[identity.sequence_id] = SequenceState.RECEIVED
        try:
            prepared = self.gateway.prepare_batch(requests, selector_expectations=selector_expectations)
        except Exception as exc:
            with self._lock:
                self._states[identity.sequence_id] = SequenceState.BLOCKED
            raise SequenceAdmissionError(
                "sequence_preflight_failed",
                "browser action sequence failed all-actions-first preflight",
                details={"sequence_id": identity.sequence_id, "cause": str(exc)},
            ) from exc
        result = PreflightedSequence(identity, prepared)
        with self._lock:
            self._states[identity.sequence_id] = SequenceState.PREFLIGHTED
            self._digests[identity.sequence_id] = result.digest
        return result

    def authorize(self, preflight: PreflightedSequence) -> AuthorizedSequence:
        self._ensure_state(preflight.identity.sequence_id, SequenceState.PREFLIGHTED, preflight.digest)
        authorized: list[AuthorizedBrowserAction] = []
        try:
            for prepared in preflight.prepared:
                selected = self.gateway.authorize(prepared)
                if not selected.allowed:
                    raise SequenceAdmissionError(
                        "sequence_permission_not_allowed",
                        "one browser action did not receive permission",
                        details={"action_id": prepared.action_id},
                    )
                authorized.append(selected)
        except Exception as exc:
            with self._lock:
                self._states[preflight.identity.sequence_id] = SequenceState.BLOCKED
            raise SequenceAdmissionError(
                "sequence_authorization_failed",
                "browser action sequence authorization failed before grant consumption",
                details={
                    "sequence_id": preflight.identity.sequence_id,
                    "authorized_action_ids": [item.prepared.action_id for item in authorized],
                    "cause": str(exc),
                    "side_effect_count": 0,
                },
            ) from exc
        result = AuthorizedSequence(preflight, tuple(authorized))
        with self._lock:
            self._states[preflight.identity.sequence_id] = SequenceState.AUTHORIZED
            self._digests[preflight.identity.sequence_id] = result.authorization_digest
        return result

    def execute(self, authorization: AuthorizedSequence) -> CompletedSequence:
        self._ensure_state(
            authorization.preflight.identity.sequence_id,
            SequenceState.AUTHORIZED,
            authorization.authorization_digest,
        )
        sequence_id = authorization.preflight.identity.sequence_id
        with self._lock:
            self._states[sequence_id] = SequenceState.EXECUTING
        completed: list[CompletedBrowserAction] = []
        for action in authorization.actions:
            try:
                completed.append(self.gateway.execute(action))
            except BrowserActionGatewayError as exc:
                state = SequenceState.PARTIAL if completed else SequenceState.BLOCKED
                with self._lock:
                    self._states[sequence_id] = state
                return CompletedSequence(
                    authorization=authorization,
                    actions=tuple(completed),
                    state=state,
                    failed_action_id=action.prepared.action_id,
                    failure_code=exc.code,
                )
        with self._lock:
            self._states[sequence_id] = SequenceState.COMPLETED
        return CompletedSequence(authorization, tuple(completed), SequenceState.COMPLETED)

    def state(self, sequence_id: str) -> SequenceState | None:
        with self._lock:
            return self._states.get(sequence_id)

    def _ensure_state(self, sequence_id: str, expected: SequenceState, digest: str) -> None:
        self._ensure_available()
        with self._lock:
            current = self._states.get(sequence_id)
            stored_digest = self._digests.get(sequence_id)
        if current != expected:
            raise SequenceAdmissionError(
                "sequence_state_mismatch",
                f"browser action sequence expected {expected} but is {current}",
            )
        if stored_digest != digest:
            raise SequenceAdmissionError("sequence_digest_mismatch", "browser action sequence material changed")

    def _ensure_available(self) -> None:
        if self.disabled:
            raise SequenceAdmissionError("sequence_coordinator_disabled", "browser action sequence coordinator is disabled")


def sequence_identity(requests: Sequence[ActionRequest]) -> SequenceIdentity:
    if not requests:
        raise SequenceAdmissionError("sequence_empty", "browser action sequence cannot be empty")
    first = requests[0]
    action_ids: list[str] = []
    for request in requests:
        if request.identity.run_id != first.identity.run_id or request.identity.task_id != first.identity.task_id:
            raise SequenceAdmissionError("sequence_task_mismatch", "browser sequence actions must belong to one run/task")
        if request.identity.worker_request_id != first.identity.worker_request_id:
            raise SequenceAdmissionError("sequence_worker_mismatch", "browser sequence actions must belong to one worker request")
        action_ids.append(request.identity.action_id)
    sequence_id = stable_id(
        "brsequence",
        first.identity.run_id,
        first.identity.task_id,
        first.identity.worker_request_id,
        action_ids,
    )
    return SequenceIdentity(
        sequence_id=sequence_id,
        run_id=first.identity.run_id,
        task_id=first.identity.task_id,
        worker_request_id=first.identity.worker_request_id,
        action_ids=tuple(action_ids),
    )
