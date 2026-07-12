from __future__ import annotations

"""Canonical-owner session lifecycle command transactions.

The runtime stores immutable command checkpoints and receipts, not a second
session truth.  Actual read/mutation callbacks are supplied by the existing
02B/02D session owner.  A command cannot succeed without before/after owner
digests and revisions.
"""

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, Sequence

from zyra_core import new_id, now_iso


class SessionControlError(RuntimeError):
    pass


class SessionControlDisabled(SessionControlError):
    pass


class SessionOwnerUnavailable(SessionControlError):
    pass


class SessionRevisionConflict(SessionControlError):
    pass


class SessionControlConflict(SessionControlError):
    pass


class SessionAction(StrEnum):
    CLEAR = "clear"
    BRANCH = "branch"
    REWIND = "rewind"
    RESUME = "resume"
    SET_MODEL = "set_model"
    SET_EFFORT = "set_effort"
    SET_THINKING = "set_thinking"
    INTERRUPT = "interrupt"
    END_SESSION = "end_session"


class SessionTransactionStatus(StrEnum):
    PREPARED = "prepared"
    APPLYING = "applying"
    COMMITTED = "committed"
    ABORTED = "aborted"
    EFFECT_UNKNOWN = "effect_unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            SessionTransactionStatus.COMMITTED,
            SessionTransactionStatus.ABORTED,
            SessionTransactionStatus.EFFECT_UNKNOWN,
        }


@dataclass(frozen=True, slots=True)
class CanonicalSessionSnapshot:
    snapshot_id: str
    session_id: str
    run_id: str
    task_id: str
    revision: int
    epoch: int
    state_digest: str
    transcript_digest: str
    checkpoint_ref: str
    parent_session_id: str = ""
    branch_name: str = ""
    model: str = ""
    effort: str = ""
    thinking: str = ""
    compact_boundary_id: str = ""
    context_epoch: int = 0
    active: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    captured_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "session_id", "run_id", "task_id", "state_digest", "checkpoint_ref"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.revision < 0 or self.epoch < 0 or self.context_epoch < 0:
            raise ValueError("session snapshot counters cannot be negative")
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "epoch": self.epoch,
            "state_digest": self.state_digest,
            "transcript_digest": self.transcript_digest,
            "checkpoint_ref": self.checkpoint_ref,
            "parent_session_id": self.parent_session_id,
            "branch_name": self.branch_name,
            "model": self.model,
            "effort": self.effort,
            "thinking": self.thinking,
            "compact_boundary_id": self.compact_boundary_id,
            "context_epoch": self.context_epoch,
            "active": self.active,
            "metadata": _safe_mapping(self.metadata),
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalSessionSnapshot":
        return cls(
            snapshot_id=str(value.get("snapshot_id") or new_id("sessionsnap")),
            session_id=str(value.get("session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            revision=max(0, int(value.get("revision") or 0)),
            epoch=max(0, int(value.get("epoch") or 0)),
            state_digest=str(value.get("state_digest") or ""),
            transcript_digest=str(value.get("transcript_digest") or ""),
            checkpoint_ref=str(value.get("checkpoint_ref") or ""),
            parent_session_id=str(value.get("parent_session_id") or ""),
            branch_name=str(value.get("branch_name") or ""),
            model=str(value.get("model") or ""),
            effort=str(value.get("effort") or ""),
            thinking=str(value.get("thinking") or ""),
            compact_boundary_id=str(value.get("compact_boundary_id") or ""),
            context_epoch=max(0, int(value.get("context_epoch") or 0)),
            active=bool(value.get("active", True)),
            metadata=_safe_mapping(value.get("metadata")),
            captured_at=str(value.get("captured_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class SessionMutationRequest:
    request_id: str
    idempotency_key: str
    action: SessionAction
    run_id: str
    task_id: str
    session_id: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    expected_revision: int | None = None
    actor_id: str = "control-command"
    causation_id: str = ""
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        for name in ("request_id", "idempotency_key", "run_id", "task_id", "session_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.expected_revision is not None and self.expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        object.__setattr__(self, "arguments", _safe_mapping(self.arguments))

    @property
    def digest(self) -> str:
        return _digest({
            "action": self.action.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "arguments": self.arguments,
            "expected_revision": self.expected_revision,
        })


@dataclass(frozen=True, slots=True)
class SessionMutationEffect:
    action: SessionAction
    changed: bool
    session_id: str
    revision: int
    checkpoint_ref: str
    result: Mapping[str, Any] = field(default_factory=dict)
    event_ids: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id or self.revision < 0 or not self.checkpoint_ref:
            raise ValueError("session mutation effect lacks canonical owner receipt")
        object.__setattr__(self, "result", _safe_mapping(self.result))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "changed": self.changed,
            "session_id": self.session_id,
            "revision": self.revision,
            "checkpoint_ref": self.checkpoint_ref,
            "result": _safe_mapping(self.result),
            "event_ids": list(self.event_ids),
            "artifact_refs": list(self.artifact_refs),
            "metadata": _safe_mapping(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionMutationReceipt:
    transaction_id: str
    request_id: str
    idempotency_key: str
    request_digest: str
    action: SessionAction
    status: SessionTransactionStatus
    before: CanonicalSessionSnapshot
    after: CanonicalSessionSnapshot | None = None
    effect: SessionMutationEffect | None = None
    error_code: str = ""
    error_message: str = ""
    revision: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @property
    def committed(self) -> bool:
        return self.status is SessionTransactionStatus.COMMITTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "action": self.action.value,
            "status": self.status.value,
            "before": self.before.to_dict(),
            "after": self.after.to_dict() if self.after else None,
            "effect": self.effect.to_dict() if self.effect else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionMutationReceipt":
        before = _mapping(value.get("before"))
        after = value.get("after")
        effect = value.get("effect")
        parsed_effect = None
        if isinstance(effect, Mapping):
            parsed_effect = SessionMutationEffect(
                action=SessionAction(str(effect.get("action") or value.get("action") or SessionAction.CLEAR.value)),
                changed=bool(effect.get("changed", False)),
                session_id=str(effect.get("session_id") or ""),
                revision=max(0, int(effect.get("revision") or 0)),
                checkpoint_ref=str(effect.get("checkpoint_ref") or ""),
                result=_safe_mapping(effect.get("result")),
                event_ids=tuple(str(item) for item in effect.get("event_ids") or ()),
                artifact_refs=tuple(str(item) for item in effect.get("artifact_refs") or ()),
                metadata=_safe_mapping(effect.get("metadata")),
            )
        return cls(
            transaction_id=str(value.get("transaction_id") or new_id("sessiontx")),
            request_id=str(value.get("request_id") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
            request_digest=str(value.get("request_digest") or ""),
            action=SessionAction(str(value.get("action") or SessionAction.CLEAR.value)),
            status=SessionTransactionStatus(str(value.get("status") or SessionTransactionStatus.PREPARED.value)),
            before=CanonicalSessionSnapshot.from_dict(before),
            after=CanonicalSessionSnapshot.from_dict(after) if isinstance(after, Mapping) else None,
            effect=parsed_effect,
            error_code=str(value.get("error_code") or ""),
            error_message=str(value.get("error_message") or ""),
            revision=max(0, int(value.get("revision") or 0)),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )


class CanonicalSessionOwner(Protocol):
    def snapshot(self, *, run_id: str, task_id: str, session_id: str) -> CanonicalSessionSnapshot: ...

    def mutate(self, request: SessionMutationRequest, before: CanonicalSessionSnapshot) -> SessionMutationEffect: ...


class CallbackSessionOwner:
    def __init__(
        self,
        snapshot_callback: Callable[..., CanonicalSessionSnapshot | Mapping[str, Any]],
        mutation_callback: Callable[[SessionMutationRequest, CanonicalSessionSnapshot], SessionMutationEffect | Mapping[str, Any]],
    ) -> None:
        self.snapshot_callback = snapshot_callback
        self.mutation_callback = mutation_callback

    def snapshot(self, *, run_id: str, task_id: str, session_id: str) -> CanonicalSessionSnapshot:
        value = self.snapshot_callback(run_id=run_id, task_id=task_id, session_id=session_id)
        return value if isinstance(value, CanonicalSessionSnapshot) else CanonicalSessionSnapshot.from_dict(value)

    def mutate(self, request: SessionMutationRequest, before: CanonicalSessionSnapshot) -> SessionMutationEffect:
        value = self.mutation_callback(request, before)
        if isinstance(value, SessionMutationEffect):
            return value
        raw = _mapping(value)
        return SessionMutationEffect(
            action=request.action,
            changed=bool(raw.get("changed", True)),
            session_id=str(raw.get("session_id") or before.session_id),
            revision=max(0, int(raw.get("revision") or before.revision + 1)),
            checkpoint_ref=str(raw.get("checkpoint_ref") or ""),
            result=_safe_mapping(raw.get("result") or raw),
            event_ids=tuple(str(item) for item in raw.get("event_ids") or ()),
            artifact_refs=tuple(str(item) for item in raw.get("artifact_refs") or ()),
            metadata=_safe_mapping(raw.get("metadata")),
        )


class SessionControlStore:
    schema = "zyra.session-control-transactions/v1"

    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = bool(disabled)
        self._lock = RLock()
        self._receipts: dict[str, SessionMutationReceipt] = {}
        self._idempotency: dict[str, str] = {}
        self._load()

    def prepare(self, request: SessionMutationRequest, before: CanonicalSessionSnapshot) -> SessionMutationReceipt:
        self._require_enabled()
        with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id:
                existing = self._receipts[existing_id]
                if existing.request_digest != request.digest:
                    raise SessionControlConflict("session idempotency key reused with different request")
                return copy.deepcopy(existing)
            receipt = SessionMutationReceipt(
                transaction_id=new_id("sessiontx"),
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                request_digest=request.digest,
                action=request.action,
                status=SessionTransactionStatus.PREPARED,
                before=copy.deepcopy(before),
            )
            self._receipts[receipt.transaction_id] = receipt
            self._idempotency[request.idempotency_key] = receipt.transaction_id
            self._persist()
            return copy.deepcopy(receipt)

    def mark_applying(self, transaction_id: str) -> SessionMutationReceipt:
        return self._transition(transaction_id, SessionTransactionStatus.APPLYING)

    def commit(
        self,
        transaction_id: str,
        *,
        effect: SessionMutationEffect,
        after: CanonicalSessionSnapshot,
    ) -> SessionMutationReceipt:
        self._require_enabled()
        with self._lock:
            current = self._require(transaction_id)
            if current.status is SessionTransactionStatus.COMMITTED:
                if current.after and current.after.state_digest == after.state_digest:
                    return copy.deepcopy(current)
                raise SessionControlConflict("session transaction already committed differently")
            if current.status is not SessionTransactionStatus.APPLYING:
                raise SessionControlConflict(f"cannot commit session transaction from {current.status.value}")
            if effect.session_id != after.session_id or effect.revision != after.revision:
                raise SessionControlConflict("session owner effect and after snapshot disagree")
            if effect.checkpoint_ref != after.checkpoint_ref:
                raise SessionControlConflict("session owner effect lacks matching after checkpoint")
            changed = replace(
                current,
                status=SessionTransactionStatus.COMMITTED,
                effect=copy.deepcopy(effect),
                after=copy.deepcopy(after),
                revision=current.revision + 1,
                updated_at=now_iso(),
            )
            self._receipts[transaction_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def abort(self, transaction_id: str, *, error: Exception, effect_may_have_happened: bool) -> SessionMutationReceipt:
        self._require_enabled()
        with self._lock:
            current = self._require(transaction_id)
            if current.terminal:
                return copy.deepcopy(current)
            status = (
                SessionTransactionStatus.EFFECT_UNKNOWN
                if effect_may_have_happened else SessionTransactionStatus.ABORTED
            )
            changed = replace(
                current,
                status=status,
                error_code=type(error).__name__,
                error_message=str(error),
                revision=current.revision + 1,
                updated_at=now_iso(),
            )
            self._receipts[transaction_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def get(self, transaction_id: str) -> SessionMutationReceipt:
        self._require_enabled()
        with self._lock:
            return copy.deepcopy(self._require(transaction_id))

    def recover(self) -> tuple[SessionMutationReceipt, ...]:
        """Never replay APPLYING transactions after restart."""

        recovered = []
        with self._lock:
            for transaction_id, current in list(self._receipts.items()):
                if current.status is not SessionTransactionStatus.APPLYING:
                    continue
                changed = replace(
                    current,
                    status=SessionTransactionStatus.EFFECT_UNKNOWN,
                    error_code="session_effect_outcome_unknown_after_restart",
                    error_message="canonical owner must reconcile before retry",
                    revision=current.revision + 1,
                    updated_at=now_iso(),
                )
                self._receipts[transaction_id] = changed
                recovered.append(copy.deepcopy(changed))
            if recovered:
                self._persist()
        return tuple(recovered)

    def list(self, *, task_id: str | None = None) -> tuple[SessionMutationReceipt, ...]:
        self._require_enabled()
        with self._lock:
            return tuple(sorted(
                (
                    copy.deepcopy(item)
                    for item in self._receipts.values()
                    if task_id is None or item.before.task_id == task_id
                ),
                key=lambda item: (item.created_at, item.transaction_id),
            ))

    def _transition(self, transaction_id: str, status: SessionTransactionStatus) -> SessionMutationReceipt:
        with self._lock:
            current = self._require(transaction_id)
            if current.terminal:
                if current.status is status:
                    return copy.deepcopy(current)
                raise SessionControlConflict("session transaction is terminal")
            allowed = {
                SessionTransactionStatus.PREPARED: {SessionTransactionStatus.APPLYING, SessionTransactionStatus.ABORTED},
                SessionTransactionStatus.APPLYING: {
                    SessionTransactionStatus.COMMITTED,
                    SessionTransactionStatus.ABORTED,
                    SessionTransactionStatus.EFFECT_UNKNOWN,
                },
            }
            if status not in allowed[current.status]:
                raise SessionControlConflict(f"invalid session transaction transition {current.status.value}->{status.value}")
            changed = replace(current, status=status, revision=current.revision + 1, updated_at=now_iso())
            self._receipts[transaction_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def _require(self, transaction_id: str) -> SessionMutationReceipt:
        value = self._receipts.get(transaction_id)
        if value is None:
            raise SessionControlConflict(f"session transaction not found: {transaction_id}")
        return value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            body = {
                "schema": self.schema,
                "owner": "M1-03D SessionControlStore",
                "canonical_session_owner": "02B/02D session store callbacks",
                "parallel_session_truth": False,
                "receipts": [item.to_dict() for item in sorted(self._receipts.values(), key=lambda value: value.transaction_id)],
                "idempotency": dict(sorted(self._idempotency.items())),
            }
            return {**body, "checksum": _digest(body)}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SessionControlError(f"cannot load session control store: {error}") from error
        if not isinstance(value, Mapping):
            raise SessionControlError("session control store root must be an object")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        expected = str(value.get("checksum") or "")
        if expected and expected != _digest(body):
            raise SessionControlError("session control store checksum mismatch")
        for item in value.get("receipts") or ():
            if isinstance(item, Mapping):
                receipt = SessionMutationReceipt.from_dict(item)
                self._receipts[receipt.transaction_id] = receipt
        raw_idempotency = value.get("idempotency")
        if isinstance(raw_idempotency, Mapping):
            self._idempotency = {str(key): str(item) for key, item in raw_idempotency.items()}

    def _persist(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise SessionControlDisabled("SessionControlStore is disabled")


class SessionControlRuntime:
    def __init__(
        self,
        store: SessionControlStore,
        owner: CanonicalSessionOwner | None,
        *,
        disabled: bool = False,
    ) -> None:
        self.store = store
        self.owner = owner
        self.disabled = bool(disabled)

    def execute(self, request: SessionMutationRequest) -> SessionMutationReceipt:
        self._require_enabled()
        if self.owner is None:
            raise SessionOwnerUnavailable("canonical session owner is unavailable")
        before = self.owner.snapshot(
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=request.session_id,
        )
        if before.session_id != request.session_id or before.run_id != request.run_id or before.task_id != request.task_id:
            raise SessionControlConflict("canonical session snapshot identity mismatch")
        if request.expected_revision is not None and before.revision != request.expected_revision:
            raise SessionRevisionConflict(
                f"session revision conflict: expected {request.expected_revision}, actual {before.revision}"
            )
        receipt = self.store.prepare(request, before)
        if receipt.terminal:
            return receipt
        self.store.mark_applying(receipt.transaction_id)
        effect_started = False
        try:
            effect_started = True
            effect = self.owner.mutate(request, before)
            after = self.owner.snapshot(
                run_id=request.run_id,
                task_id=request.task_id,
                session_id=effect.session_id,
            )
            self._validate_semantics(request, before, effect, after)
            return self.store.commit(receipt.transaction_id, effect=effect, after=after)
        except Exception as error:
            self.store.abort(receipt.transaction_id, error=error, effect_may_have_happened=effect_started)
            raise

    @staticmethod
    def _validate_semantics(
        request: SessionMutationRequest,
        before: CanonicalSessionSnapshot,
        effect: SessionMutationEffect,
        after: CanonicalSessionSnapshot,
    ) -> None:
        if effect.changed and after.revision <= before.revision:
            raise SessionControlConflict("mutating session action did not advance canonical revision")
        if not effect.changed and after.state_digest != before.state_digest:
            raise SessionControlConflict("read/no-op session effect changed canonical state digest")
        if request.action is SessionAction.CLEAR:
            if after.epoch <= before.epoch:
                raise SessionControlConflict("clear did not advance session epoch")
            if after.session_id == before.session_id and not bool(effect.metadata.get("same_session_new_epoch")):
                raise SessionControlConflict("clear neither created a session nor declared same-session epoch reset")
        elif request.action is SessionAction.BRANCH:
            if after.session_id == before.session_id or after.parent_session_id != before.session_id:
                raise SessionControlConflict("branch lineage is invalid")
        elif request.action is SessionAction.RESUME:
            target = str(request.arguments.get("target_session_id") or request.arguments.get("target") or "")
            if target and after.session_id != target:
                raise SessionControlConflict("resume did not activate requested session")
        elif request.action is SessionAction.REWIND:
            target = str(request.arguments.get("checkpoint_ref") or request.arguments.get("target") or "")
            if target and target != after.checkpoint_ref and target != str(effect.result.get("rewound_to") or ""):
                raise SessionControlConflict("rewind did not restore requested checkpoint")
        elif request.action is SessionAction.SET_MODEL:
            model = str(request.arguments.get("model") or "")
            if model and after.model != model:
                raise SessionControlConflict("model switch did not affect canonical session")
        elif request.action is SessionAction.SET_EFFORT:
            effort = str(request.arguments.get("effort") or "")
            if effort and after.effort != effort:
                raise SessionControlConflict("effort switch did not affect canonical session")

    def _require_enabled(self) -> None:
        if self.disabled:
            raise SessionControlDisabled("SessionControlRuntime is disabled")


def snapshot_from_state(
    *,
    run_id: str,
    task_id: str,
    session_id: str,
    revision: int,
    epoch: int,
    state: Mapping[str, Any],
    checkpoint_ref: str,
    transcript: Sequence[Mapping[str, Any]] = (),
    metadata: Mapping[str, Any] | None = None,
) -> CanonicalSessionSnapshot:
    safe_state = _safe_mapping(state)
    return CanonicalSessionSnapshot(
        snapshot_id=new_id("sessionsnap"),
        session_id=session_id,
        run_id=run_id,
        task_id=task_id,
        revision=max(0, int(revision)),
        epoch=max(0, int(epoch)),
        state_digest=_digest(safe_state),
        transcript_digest=_digest([_safe_mapping(item) for item in transcript]),
        checkpoint_ref=checkpoint_ref,
        parent_session_id=str(safe_state.get("parent_session_id") or ""),
        branch_name=str(safe_state.get("branch_name") or ""),
        model=str(safe_state.get("model") or ""),
        effort=str(safe_state.get("effort") or ""),
        thinking=str(safe_state.get("thinking") or ""),
        compact_boundary_id=str(safe_state.get("compact_boundary_id") or ""),
        context_epoch=max(0, int(safe_state.get("context_epoch") or 0)),
        active=bool(safe_state.get("active", True)),
        metadata=_safe_mapping(metadata),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        if any(token in name.casefold() for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        elif isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            result[name] = [_safe_value(entry) for entry in item]
        else:
            result[name] = _safe_value(item)
    return result


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    return str(value)


def _digest(value: Any) -> str:
    payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


__all__ = [
    "CallbackSessionOwner",
    "CanonicalSessionOwner",
    "CanonicalSessionSnapshot",
    "SessionAction",
    "SessionControlConflict",
    "SessionControlDisabled",
    "SessionControlError",
    "SessionControlRuntime",
    "SessionControlStore",
    "SessionMutationEffect",
    "SessionMutationReceipt",
    "SessionMutationRequest",
    "SessionOwnerUnavailable",
    "SessionRevisionConflict",
    "SessionTransactionStatus",
    "snapshot_from_state",
]
