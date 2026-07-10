from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from typing import Any
from uuid import uuid4


_TOKEN_VERSION = "zg1"
_SNAPSHOT_VERSION = 1
_MINIMUM_KEY_BYTES = 32


class GrantState(StrEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    INVALIDATED_AFTER_RESTORE = "invalidated_after_restore"


class GrantValidationCode(StrEnum):
    ACCEPTED = "accepted"
    INVALID_TOKEN = "invalid_token"
    INVALID_BINDING = "invalid_binding"
    BINDING_MISMATCH = "binding_mismatch"
    EXPIRED = "expired"
    ALREADY_CONSUMED = "already_consumed"
    INVALIDATED = "invalidated"
    RESTORE_INVALIDATED = "restore_invalidated"


class GrantRestorePolicy(StrEnum):
    """Snapshot policy for credentials whose signing key is never persisted."""

    INVALIDATE = "invalidate"


RestorePolicy = GrantRestorePolicy


class GrantError(RuntimeError):
    pass


class GrantBindingError(ValueError):
    pass


class GrantIssueError(GrantError):
    pass


class GrantRestoreError(GrantError):
    pass


class CanonicalArgumentsError(GrantBindingError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionGrantBinding:
    """Immutable identity to which exactly one tool execution is authorized."""

    request_id: str
    decision_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    arguments_digest: str
    scope: str
    expiry: float
    tool_namespace: str = ""
    server_name: str = ""

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "decision_id",
            "session_id",
            "tool_call_id",
            "tool_name",
        ):
            value = _required_text(getattr(self, name), name)
            object.__setattr__(self, name, value)
        object.__setattr__(self, "tool_namespace", _identity_text(self.tool_namespace))
        object.__setattr__(self, "server_name", _identity_text(self.server_name))
        object.__setattr__(self, "arguments_digest", _normalize_arguments_digest(self.arguments_digest))
        object.__setattr__(self, "scope", _canonical_scope(self.scope))
        object.__setattr__(self, "expiry", _timestamp(self.expiry, "expiry"))

    @property
    def expires_at(self) -> float:
        return self.expiry

    @property
    def namespace(self) -> str:
        return self.tool_namespace

    @property
    def fingerprint(self) -> str:
        return _sha256_text(_canonical_json_bytes(self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_namespace": self.tool_namespace,
            "server_name": self.server_name,
            "arguments_digest": self.arguments_digest,
            "scope": self.scope,
            "expiry": self.expiry,
        }

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | "ExecutionGrantBinding") -> "ExecutionGrantBinding":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise GrantBindingError("grant binding must be a mapping")

        identity = value.get("tool_identity")
        identity_map = identity if isinstance(identity, Mapping) else {}
        tool_name = _first_present(value, "tool_name", "name", "tool")
        if tool_name is None:
            tool_name = _first_present(identity_map, "tool_name", "name", "tool")
        namespace = _first_present(value, "tool_namespace", "namespace")
        if namespace is None:
            namespace = _first_present(identity_map, "tool_namespace", "namespace")
        server_name = _first_present(value, "server_name", "server", "server_label")
        if server_name is None:
            server_name = _first_present(identity_map, "server_name", "server", "server_label")

        supplied_digest = _first_present(value, "arguments_digest", "args_digest", "canonical_arguments_digest")
        arguments_present, arguments = _first_present_with_presence(
            value,
            "arguments",
            "args",
            "tool_input",
            "input",
        )
        computed_digest: str | None = None
        if arguments_present:
            if not isinstance(arguments, Mapping):
                raise GrantBindingError("grant arguments must be a mapping")
            computed_digest = canonical_arguments_digest(arguments)
        if supplied_digest is None and computed_digest is None:
            raise GrantBindingError("grant binding requires arguments or arguments_digest")
        if supplied_digest is not None:
            normalized_digest = _normalize_arguments_digest(supplied_digest)
            if computed_digest is not None and not hmac.compare_digest(normalized_digest, computed_digest):
                raise GrantBindingError("supplied arguments_digest does not match canonical arguments")
        else:
            normalized_digest = computed_digest or ""

        expiry = _first_present(value, "expiry", "expires_at")
        return cls(
            request_id=_first_present(value, "request_id") or "",
            decision_id=_first_present(value, "decision_id") or "",
            session_id=_first_present(value, "session_id") or "",
            tool_call_id=_first_present(value, "tool_call_id", "tool_use_id") or "",
            tool_name=tool_name or "",
            tool_namespace=namespace or "",
            server_name=server_name or "",
            arguments_digest=normalized_digest,
            scope=_first_present(value, "scope", "permission_scope") or "",
            expiry=expiry,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionGrant:
    grant_id: str
    nonce: str
    binding: ExecutionGrantBinding
    issued_at: float
    token: str = field(repr=False)

    @property
    def token_hash(self) -> str:
        return _hash_token(self.token)

    @property
    def authorization_token(self) -> str:
        return self.token

    def to_dict(self) -> dict[str, Any]:
        """Safe projection. The bearer token and HMAC are intentionally absent."""

        return {
            "grant_id": self.grant_id,
            "nonce": self.nonce,
            "binding": self.binding.to_dict(),
            "binding_fingerprint": self.binding.fingerprint,
            "issued_at": self.issued_at,
        }

    def event_payload(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "binding_fingerprint": self.binding.fingerprint,
            "request_id": self.binding.request_id,
            "decision_id": self.binding.decision_id,
            "session_id": self.binding.session_id,
            "tool_call_id": self.binding.tool_call_id,
            "expires_at": self.binding.expiry,
        }

    def to_event(self) -> dict[str, Any]:
        return self.event_payload()

    def __repr__(self) -> str:
        return (
            "ExecutionGrant("
            f"grant_id={self.grant_id!r}, nonce={self.nonce!r}, "
            f"binding={self.binding!r}, issued_at={self.issued_at!r}, token='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True)
class GrantValidation:
    accepted: bool
    code: GrantValidationCode
    reason: str
    grant: ExecutionGrant | None = field(default=None, repr=False)
    grant_id: str | None = None
    audited_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "accepted": self.accepted,
            "code": self.code.value,
            "reason": self.reason,
            "grant_id": self.grant_id,
            "audited_at": self.audited_at,
        }
        if self.grant is not None:
            data["grant"] = self.grant.to_dict()
        return data


@dataclass(frozen=True, slots=True)
class GrantAuditEvent:
    sequence: int
    event_type: str
    grant_id: str | None
    occurred_at: float
    code: str
    reason: str
    binding_fingerprint: str | None = None
    presented_binding_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "grant_id": self.grant_id,
            "occurred_at": self.occurred_at,
            "code": self.code,
            "reason": self.reason,
            "binding_fingerprint": self.binding_fingerprint,
            "presented_binding_fingerprint": self.presented_binding_fingerprint,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class _GrantRecord:
    grant_id: str
    nonce: str
    token_hash: str
    binding: ExecutionGrantBinding
    issued_at: float
    state: GrantState = GrantState.ISSUED
    consumed_at: float | None = None
    invalidated_at: float | None = None

    def safe_snapshot(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "nonce": self.nonce,
            "token_hash": self.token_hash,
            "binding": self.binding.to_dict(),
            "binding_fingerprint": self.binding.fingerprint,
            "issued_at": self.issued_at,
            "state": self.state.value,
            "consumed_at": self.consumed_at,
            "invalidated_at": self.invalidated_at,
        }


class ExecutionGrantStore:
    """Thread-safe, single-process owner of one-use execution grants.

    Validation and consumption share one lock, so exactly one thread can win.
    The store deliberately does not claim cross-process atomicity: a session
    approval queue must choose one store owner. Snapshots never contain the HMAC
    key or bearer token. Because the key is not restorable, every still-issued
    grant is deterministically invalidated when a snapshot is restored.
    """

    concurrency_model = "single-process-owner; external queue selects one owner"

    def __init__(
        self,
        *,
        secret_key: bytes | bytearray | memoryview | None = None,
        clock: Any = time.time,
        nonce_factory: Any = None,
        restore_policy: GrantRestorePolicy | str = GrantRestorePolicy.INVALIDATE,
        audit_limit: int = 4096,
        owner_id: str | None = None,
    ) -> None:
        key = secrets.token_bytes(_MINIMUM_KEY_BYTES) if secret_key is None else bytes(secret_key)
        if len(key) < _MINIMUM_KEY_BYTES:
            raise ValueError(f"secret_key must contain at least {_MINIMUM_KEY_BYTES} bytes")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if nonce_factory is not None and not callable(nonce_factory):
            raise TypeError("nonce_factory must be callable")
        if audit_limit < 1:
            raise ValueError("audit_limit must be positive")

        self._secret_key = key
        self._clock = clock
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(24))
        self._restore_policy = _restore_policy(restore_policy)
        self._audit_limit = int(audit_limit)
        self._owner_id = owner_id or f"grant-owner-{uuid4().hex}"
        self._lock = threading.RLock()
        self._records: dict[str, _GrantRecord] = {}
        self._binding_index: dict[str, str] = {}
        self._audit: deque[GrantAuditEvent] = deque(maxlen=self._audit_limit)
        self._audit_sequence = 0

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def restore_policy(self) -> GrantRestorePolicy:
        return self._restore_policy

    def issue(self, binding: Mapping[str, Any] | ExecutionGrantBinding) -> ExecutionGrant:
        normalized = ExecutionGrantBinding.from_value(binding)
        now = _timestamp(self._clock(), "clock")
        if normalized.expiry <= now:
            raise GrantIssueError("cannot issue an already-expired execution grant")

        with self._lock:
            if normalized.fingerprint in self._binding_index:
                raise GrantIssueError("this decision and exact execution binding already has a grant")

            grant_id = f"grant-{uuid4().hex}"
            nonce = _required_text(self._nonce_factory(), "nonce")
            unsigned = _token_unsigned_payload(grant_id, nonce, normalized, now)
            signature = _base64url(hmac.digest(self._secret_key, unsigned, "sha256"))
            token = f"{_TOKEN_VERSION}.{grant_id}.{nonce}.{signature}"
            token_hash = _hash_token(token)
            if token_hash in self._records:
                raise GrantIssueError("grant token collision")

            record = _GrantRecord(
                grant_id=grant_id,
                nonce=nonce,
                token_hash=token_hash,
                binding=normalized,
                issued_at=now,
            )
            self._records[token_hash] = record
            self._binding_index[normalized.fingerprint] = token_hash
            grant = ExecutionGrant(grant_id, nonce, normalized, now, token)
            self._append_audit(
                "execution_grant_issued",
                grant_id,
                now,
                "issued",
                "one-use execution grant issued",
                binding_fingerprint=normalized.fingerprint,
            )
            return grant

    def validate_and_consume(
        self,
        token: str,
        binding: Mapping[str, Any] | ExecutionGrantBinding,
    ) -> GrantValidation:
        now = _timestamp(self._clock(), "clock")
        token_text = token if isinstance(token, str) else ""
        token_hash = _hash_token(token_text) if token_text else ""

        try:
            presented = ExecutionGrantBinding.from_value(binding)
        except (GrantBindingError, TypeError, ValueError) as exc:
            with self._lock:
                record = self._records.get(token_hash)
                grant_id = record.grant_id if record is not None else None
                self._append_audit(
                    "execution_grant_rejected",
                    grant_id,
                    now,
                    GrantValidationCode.INVALID_BINDING.value,
                    "presented execution binding is invalid",
                    binding_fingerprint=record.binding.fingerprint if record else None,
                    metadata={"error_type": type(exc).__name__},
                )
            return GrantValidation(
                False,
                GrantValidationCode.INVALID_BINDING,
                "presented execution binding is invalid",
                grant_id=grant_id,
                audited_at=now,
            )

        with self._lock:
            record = self._records.get(token_hash)
            if record is None:
                self._append_audit(
                    "execution_grant_rejected",
                    None,
                    now,
                    GrantValidationCode.INVALID_TOKEN.value,
                    "execution grant token is unknown or has been tampered with",
                    presented_binding_fingerprint=presented.fingerprint,
                )
                return GrantValidation(
                    False,
                    GrantValidationCode.INVALID_TOKEN,
                    "execution grant token is unknown or has been tampered with",
                    audited_at=now,
                )

            if record.state is GrantState.INVALIDATED_AFTER_RESTORE:
                return self._reject_record(
                    record,
                    now,
                    GrantValidationCode.RESTORE_INVALIDATED,
                    "unconsumed grant was invalidated when state was restored without its signing key",
                    presented.fingerprint,
                )
            if record.state is GrantState.INVALIDATED:
                return self._reject_record(
                    record,
                    now,
                    GrantValidationCode.INVALIDATED,
                    "execution grant was invalidated before handoff",
                    presented.fingerprint,
                )
            if record.state is GrantState.CONSUMED:
                return self._reject_record(
                    record,
                    now,
                    GrantValidationCode.ALREADY_CONSUMED,
                    "execution grant has already been consumed",
                    presented.fingerprint,
                )
            if record.state is GrantState.EXPIRED or now >= record.binding.expiry:
                record.state = GrantState.EXPIRED
                record.invalidated_at = record.invalidated_at or now
                return self._reject_record(
                    record,
                    now,
                    GrantValidationCode.EXPIRED,
                    "execution grant has expired",
                    presented.fingerprint,
                )

            expected = self._token_for_record(record)
            if not hmac.compare_digest(expected, token_text):
                return self._reject_record(
                    record,
                    now,
                    GrantValidationCode.INVALID_TOKEN,
                    "execution grant HMAC validation failed",
                    presented.fingerprint,
                )

            if not hmac.compare_digest(record.binding.fingerprint, presented.fingerprint):
                return self._reject_record(
                    record,
                    now,
                    GrantValidationCode.BINDING_MISMATCH,
                    "execution grant does not match the exact request, tool identity, arguments, scope, or expiry",
                    presented.fingerprint,
                )

            record.state = GrantState.CONSUMED
            record.consumed_at = now
            grant = ExecutionGrant(
                record.grant_id,
                record.nonce,
                record.binding,
                record.issued_at,
                token_text,
            )
            self._append_audit(
                "execution_grant_consumed",
                record.grant_id,
                now,
                GrantValidationCode.ACCEPTED.value,
                "execution grant atomically validated and consumed",
                binding_fingerprint=record.binding.fingerprint,
            )
            return GrantValidation(
                True,
                GrantValidationCode.ACCEPTED,
                "execution grant atomically validated and consumed",
                grant=grant,
                grant_id=record.grant_id,
                audited_at=now,
            )

    def invalidate(self, grant: ExecutionGrant, *, reason: str) -> bool:
        """Invalidate an issued grant that cannot be durably committed."""

        if not isinstance(grant, ExecutionGrant):
            return False
        token_hash = _hash_token(grant.authorization_token)
        now = _timestamp(self._clock(), "clock")
        with self._lock:
            record = self._records.get(token_hash)
            if record is None or record.state is not GrantState.ISSUED:
                return False
            record.state = GrantState.INVALIDATED
            record.invalidated_at = now
            self._append_audit(
                "execution_grant_invalidated",
                record.grant_id,
                now,
                GrantValidationCode.INVALIDATED.value,
                str(reason or "grant invalidated before execution handoff"),
                binding_fingerprint=record.binding.fingerprint,
            )
            return True

    def snapshot(self) -> dict[str, Any]:
        """Return safe state: no signing key, bearer token, or HMAC signature."""

        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "captured_at": _timestamp(self._clock(), "clock"),
                "owner_id": self._owner_id,
                "concurrency_model": self.concurrency_model,
                "restore_policy": self._restore_policy.value,
                "contains_signing_key": False,
                "records": [record.safe_snapshot() for record in self._records.values()],
                "audit": [event.to_dict() for event in self._audit],
            }

    def restore(
        self,
        snapshot: Mapping[str, Any],
        *,
        restore_policy: GrantRestorePolicy | str | None = None,
        replace: bool = True,
    ) -> tuple[GrantAuditEvent, ...]:
        if not isinstance(snapshot, Mapping):
            raise GrantRestoreError("grant snapshot must be a mapping")
        if snapshot.get("version") != _SNAPSHOT_VERSION:
            raise GrantRestoreError("unsupported grant snapshot version")
        policy = self._restore_policy if restore_policy is None else _restore_policy(restore_policy)
        if policy is not GrantRestorePolicy.INVALIDATE:
            raise GrantRestoreError("only invalidate-on-restore is supported without persisting the signing key")

        records_value = snapshot.get("records", ())
        if not isinstance(records_value, Sequence) or isinstance(records_value, (str, bytes, bytearray)):
            raise GrantRestoreError("grant snapshot records must be a sequence")

        imported = [_record_from_snapshot(item) for item in records_value]
        token_hashes = [record.token_hash for record in imported]
        fingerprints = [record.binding.fingerprint for record in imported]
        if len(token_hashes) != len(set(token_hashes)):
            raise GrantRestoreError("grant snapshot contains duplicate token hashes")
        if len(fingerprints) != len(set(fingerprints)):
            raise GrantRestoreError("grant snapshot contains duplicate execution bindings")

        now = _timestamp(self._clock(), "clock")
        with self._lock:
            if self._records and not replace:
                raise GrantRestoreError("grant store is not empty; pass replace=True to restore")
            self._records.clear()
            self._binding_index.clear()
            self._audit.clear()
            self._audit_sequence = 0
            self._restore_audit(snapshot.get("audit", ()))

            generated: list[GrantAuditEvent] = []
            for record in imported:
                if record.state is GrantState.ISSUED:
                    record.state = GrantState.INVALIDATED_AFTER_RESTORE
                    record.invalidated_at = now
                self._records[record.token_hash] = record
                self._binding_index[record.binding.fingerprint] = record.token_hash
                if record.state is GrantState.INVALIDATED_AFTER_RESTORE:
                    generated.append(
                        self._append_audit(
                            "execution_grant_restore_invalidated",
                            record.grant_id,
                            now,
                            GrantValidationCode.RESTORE_INVALIDATED.value,
                            "unconsumed grant invalidated because snapshot excludes the signing key",
                            binding_fingerprint=record.binding.fingerprint,
                            metadata={"restore_policy": policy.value},
                        )
                    )
            return tuple(generated)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, Any],
        *,
        secret_key: bytes | bytearray | memoryview | None = None,
        clock: Any = time.time,
        restore_policy: GrantRestorePolicy | str = GrantRestorePolicy.INVALIDATE,
        audit_limit: int = 4096,
        owner_id: str | None = None,
    ) -> "ExecutionGrantStore":
        store = cls(
            secret_key=secret_key,
            clock=clock,
            restore_policy=restore_policy,
            audit_limit=audit_limit,
            owner_id=owner_id,
        )
        store.restore(snapshot, restore_policy=restore_policy)
        return store

    def audit_events(self) -> tuple[GrantAuditEvent, ...]:
        with self._lock:
            return tuple(self._audit)

    def state_for_token(self, token: str) -> GrantState | None:
        token_hash = _hash_token(token) if isinstance(token, str) and token else ""
        with self._lock:
            record = self._records.get(token_hash)
            return record.state if record else None

    def __repr__(self) -> str:
        with self._lock:
            return (
                "ExecutionGrantStore("
                f"owner_id={self._owner_id!r}, grants={len(self._records)}, "
                f"restore_policy={self._restore_policy.value!r}, secret_key='[REDACTED]')"
            )

    def __getstate__(self) -> Any:
        raise TypeError("ExecutionGrantStore cannot be pickled; use snapshot() without signing material")

    def _token_for_record(self, record: _GrantRecord) -> str:
        unsigned = _token_unsigned_payload(
            record.grant_id,
            record.nonce,
            record.binding,
            record.issued_at,
        )
        signature = _base64url(hmac.digest(self._secret_key, unsigned, "sha256"))
        return f"{_TOKEN_VERSION}.{record.grant_id}.{record.nonce}.{signature}"

    def _reject_record(
        self,
        record: _GrantRecord,
        now: float,
        code: GrantValidationCode,
        reason: str,
        presented_fingerprint: str,
    ) -> GrantValidation:
        self._append_audit(
            "execution_grant_rejected",
            record.grant_id,
            now,
            code.value,
            reason,
            binding_fingerprint=record.binding.fingerprint,
            presented_binding_fingerprint=presented_fingerprint,
        )
        return GrantValidation(
            False,
            code,
            reason,
            grant_id=record.grant_id,
            audited_at=now,
        )

    def _append_audit(
        self,
        event_type: str,
        grant_id: str | None,
        occurred_at: float,
        code: str,
        reason: str,
        *,
        binding_fingerprint: str | None = None,
        presented_binding_fingerprint: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> GrantAuditEvent:
        self._audit_sequence += 1
        event = GrantAuditEvent(
            sequence=self._audit_sequence,
            event_type=event_type,
            grant_id=grant_id,
            occurred_at=occurred_at,
            code=code,
            reason=reason,
            binding_fingerprint=binding_fingerprint,
            presented_binding_fingerprint=presented_binding_fingerprint,
            metadata=dict(metadata or {}),
        )
        self._audit.append(event)
        return event

    def _restore_audit(self, value: Any) -> None:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return
        for raw in value[-self._audit_limit :]:
            if not isinstance(raw, Mapping):
                continue
            try:
                event = GrantAuditEvent(
                    sequence=int(raw.get("sequence", self._audit_sequence + 1)),
                    event_type=str(raw.get("event_type") or "restored_grant_audit"),
                    grant_id=_optional_text(raw.get("grant_id")),
                    occurred_at=_timestamp(raw.get("occurred_at"), "audit occurred_at"),
                    code=str(raw.get("code") or "restored"),
                    reason=str(raw.get("reason") or "restored grant audit event"),
                    binding_fingerprint=_optional_text(raw.get("binding_fingerprint")),
                    presented_binding_fingerprint=_optional_text(
                        raw.get("presented_binding_fingerprint")
                    ),
                    metadata=_safe_audit_metadata(raw.get("metadata")),
                )
            except (TypeError, ValueError, GrantBindingError):
                continue
            self._audit_sequence = max(self._audit_sequence, event.sequence)
            self._audit.append(event)


OneTimeExecutionGrantStore = ExecutionGrantStore


def canonical_arguments_digest(arguments: Mapping[str, Any]) -> str:
    if not isinstance(arguments, Mapping):
        raise CanonicalArgumentsError("tool arguments must be a mapping")
    return _sha256_text(_canonical_json_bytes(arguments))


def _canonical_json_bytes(value: Any) -> bytes:
    normalized = _normalize_json(value, path="$", active=set())
    try:
        text = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalArgumentsError("tool arguments are not canonical JSON values") from exc
    return text.encode("utf-8")


def _normalize_json(value: Any, *, path: str, active: set[int]) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalArgumentsError(f"non-finite number at {path}")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise CanonicalArgumentsError(f"cyclic mapping at {path}")
        active.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise CanonicalArgumentsError(f"non-string object key at {path}")
                result[key] = _normalize_json(child, path=f"{path}.{key}", active=active)
            return result
        finally:
            active.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        identity = id(value)
        if identity in active:
            raise CanonicalArgumentsError(f"cyclic sequence at {path}")
        active.add(identity)
        try:
            return [
                _normalize_json(child, path=f"{path}[{index}]", active=active)
                for index, child in enumerate(value)
            ]
        finally:
            active.remove(identity)
    raise CanonicalArgumentsError(f"unsupported argument value {type(value).__name__} at {path}")


def _token_unsigned_payload(
    grant_id: str,
    nonce: str,
    binding: ExecutionGrantBinding,
    issued_at: float,
) -> bytes:
    return _canonical_json_bytes(
        {
            "version": _TOKEN_VERSION,
            "grant_id": grant_id,
            "nonce": nonce,
            "binding_fingerprint": binding.fingerprint,
            "issued_at": issued_at,
            "expires_at": binding.expiry,
        }
    )


def _record_from_snapshot(value: Any) -> _GrantRecord:
    if not isinstance(value, Mapping):
        raise GrantRestoreError("grant snapshot record must be a mapping")
    binding_value = value.get("binding")
    try:
        binding = ExecutionGrantBinding.from_value(binding_value)
    except (GrantBindingError, TypeError, ValueError) as exc:
        raise GrantRestoreError("grant snapshot record has an invalid binding") from exc
    declared_fingerprint = value.get("binding_fingerprint")
    if declared_fingerprint is not None and not hmac.compare_digest(
        str(declared_fingerprint), binding.fingerprint
    ):
        raise GrantRestoreError("grant snapshot binding fingerprint mismatch")
    token_hash = _normalize_token_hash(value.get("token_hash"))
    try:
        state = GrantState(str(value.get("state") or GrantState.ISSUED.value))
    except ValueError as exc:
        raise GrantRestoreError("grant snapshot record has an invalid state") from exc
    return _GrantRecord(
        grant_id=_required_text(value.get("grant_id"), "grant_id"),
        nonce=_required_text(value.get("nonce"), "nonce"),
        token_hash=token_hash,
        binding=binding,
        issued_at=_timestamp(value.get("issued_at"), "issued_at"),
        state=state,
        consumed_at=_optional_timestamp(value.get("consumed_at"), "consumed_at"),
        invalidated_at=_optional_timestamp(value.get("invalidated_at"), "invalidated_at"),
    )


def _normalize_token_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        digest = text[7:]
    else:
        digest = text
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise GrantRestoreError("grant snapshot token_hash is invalid")
    return f"sha256:{digest}"


def _normalize_arguments_digest(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise GrantBindingError("arguments_digest must be non-empty")
    if ":" not in text:
        if len(text) == 64 and all(character in "0123456789abcdefABCDEF" for character in text):
            return f"sha256:{text.lower()}"
        raise GrantBindingError("arguments_digest must include an algorithm prefix")
    algorithm, digest = text.split(":", 1)
    if algorithm.lower() != "sha256" or not digest.strip():
        raise GrantBindingError("arguments_digest must be a non-empty sha256 digest")
    digest = digest.strip()
    if len(digest) == 64 and all(character in "0123456789abcdefABCDEF" for character in digest):
        digest = digest.lower()
    return f"sha256:{digest}"


def _canonical_scope(value: Any) -> str:
    if isinstance(value, Mapping):
        return f"sha256:{hashlib.sha256(_canonical_json_bytes(value)).hexdigest()}"
    text = str(value or "").strip()
    if not text:
        raise GrantBindingError("scope must be non-empty")
    return text


def _timestamp(value: Any, name: str) -> float:
    if isinstance(value, datetime):
        timestamp = value.timestamp()
    elif isinstance(value, str):
        text = value.strip()
        try:
            timestamp = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise GrantBindingError(f"{name} must be a numeric or ISO-8601 timestamp") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = parsed.timestamp()
    else:
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise GrantBindingError(f"{name} must be a timestamp") from exc
    if not math.isfinite(timestamp):
        raise GrantBindingError(f"{name} must be finite")
    return timestamp


def _optional_timestamp(value: Any, name: str) -> float | None:
    return None if value is None else _timestamp(value, name)


def _required_text(value: Any, name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise GrantBindingError(f"{name} must be non-empty")
    if any(character in text for character in "\r\n\0"):
        raise GrantBindingError(f"{name} contains forbidden control characters")
    return text


def _identity_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if any(character in text for character in "\r\n\0"):
        raise GrantBindingError("tool identity contains forbidden control characters")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _first_present(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return None


def _first_present_with_presence(value: Mapping[str, Any], *names: str) -> tuple[bool, Any]:
    for name in names:
        if name in value:
            return True, value[name]
    return False, None


def _hash_token(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def _sha256_text(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _restore_policy(value: GrantRestorePolicy | str) -> GrantRestorePolicy:
    try:
        return GrantRestorePolicy(str(getattr(value, "value", value)))
    except ValueError as exc:
        raise ValueError(f"unsupported grant restore policy: {value!r}") from exc


_AUDIT_SECRET_KEYS = frozenset(
    {
        "token",
        "signature",
        "secret",
        "secret_key",
        "hmac",
        "authorization",
        "credential",
        "password",
    }
)


def _safe_audit_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key).lower().replace("-", "_")
        if any(secret in normalized_key for secret in _AUDIT_SECRET_KEYS):
            result[str(key)] = "[REDACTED]"
        elif item is None or isinstance(item, (str, bool, int, float)):
            result[str(key)] = item
    return result


__all__ = [
    "CanonicalArgumentsError",
    "ExecutionGrant",
    "ExecutionGrantBinding",
    "ExecutionGrantStore",
    "GrantAuditEvent",
    "GrantBindingError",
    "GrantError",
    "GrantIssueError",
    "GrantRestoreError",
    "GrantRestorePolicy",
    "GrantState",
    "GrantValidation",
    "GrantValidationCode",
    "OneTimeExecutionGrantStore",
    "RestorePolicy",
    "canonical_arguments_digest",
]
