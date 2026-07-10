from __future__ import annotations

"""Session-scoped approval queue view over :mod:`permission.store`.

``PermissionStateStore`` is the only JSON/state owner.  This module does not
keep a second request dictionary, response index, lock file, or request DTO.
It translates queue operations into the store's atomic request transitions and
projects typed outcomes for transports.  Consequently two queue objects (or
processes) that point at stores for the same path race through one store CAS.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
import hashlib
import hmac
from typing import Any

from .canonical import canonical_arguments_json
from .models import (
    PermissionEffect,
    PermissionRequestPhase,
    PermissionRequestRecord,
    PermissionRequestStatus,
    PermissionResolutionResponse,
    PermissionScope,
    ToolIdentity,
)
from .store import (
    PermissionIdentityMismatch,
    PermissionRequestExpired,
    PermissionRequestTerminal,
    PermissionStateConflict,
    PermissionStateCorrupt,
    PermissionStateDisabled,
    PermissionStateStore,
)


# Compatibility names are aliases to the authoritative models, not competing
# DTOs/enums.  New code should use the model names directly.
QueuedPermissionRequest = PermissionRequestRecord
PermissionApprovalIdentity = PermissionRequestRecord
PermissionResolutionEffect = PermissionEffect
PermissionQueueCorruptError = PermissionStateCorrupt


class PermissionResolutionCode(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    NOT_FOUND = "not_found"
    NOT_PENDING = "not_pending"
    EXPIRED = "expired"
    IDENTITY_MISMATCH = "identity_mismatch"
    VERSION_CONFLICT = "version_conflict"
    INVALID_EFFECT = "invalid_effect"
    INVALID_SOURCE = "invalid_source"
    MALFORMED = "malformed"


class PermissionQueueDisabledError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PermissionResolutionOutcome:
    accepted: bool
    code: PermissionResolutionCode
    request: PermissionRequestRecord | None
    response_id: str
    reason: str
    mismatch_fields: tuple[str, ...] = ()
    winner_resolution_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "code": self.code.value,
            "request": self.request.to_dict() if self.request else None,
            "response_id": self.response_id,
            "reason": self.reason,
            "mismatch_fields": list(self.mismatch_fields),
            "winner_resolution_id": self.winner_resolution_id,
            "metadata": dict(self.metadata),
        }


class PermissionRequestQueue:
    """Logical, session-scoped request queue backed by one state store.

    Public API:

    - ``create(record)``
    - ``mark_delivered(request_id, ...)``
    - ``resolve(response)``
    - ``expire_due()``, ``cancel(...)``, ``abort(...)``
    - ``get()``, ``list()``, ``pending()``
    - ``snapshot()``, ``restore(snapshot)``

    The store's file lock and revisioned mutation are the single-winner
    boundary.  Rejected responses are never written to request metadata or an
    auxiliary response index; their returned outcome is the audit input.
    """

    _ALLOWED_RESOLUTION_CHANNELS = frozenset(
        {
            "user",
            "structured_io",
            "bridge",
            "hook",
            "gateway",
            "api",
            "cli",
            "policy",
            "sealed",
            "runtime",
            "test",
        }
    )

    def __init__(
        self,
        state_store: PermissionStateStore,
        session_id: str,
        *,
        disabled: bool = False,
    ) -> None:
        if not isinstance(state_store, PermissionStateStore):
            raise TypeError(
                "PermissionRequestQueue requires PermissionStateStore; "
                "a queue path would create a forbidden second JSON owner"
            )
        if not str(session_id).strip():
            raise ValueError("PermissionRequestQueue requires a non-empty session_id")
        self.state_store = state_store
        self.session_id = str(session_id)
        self.disabled = bool(disabled)

    @property
    def path(self):
        """Compatibility projection; the path remains owned by state_store."""

        return self.state_store.path

    @property
    def generation(self) -> int:
        self._ensure_enabled()
        return int(self.state_store.read_state().get("revision") or 0)

    def create(
        self,
        record: PermissionRequestRecord | Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> PermissionRequestRecord:
        """Create or reuse an exact pending request.

        Canonical arguments are intentionally not accepted or persisted.  The
        authoritative model carries only their digest and request fingerprint.
        """

        self._ensure_enabled()
        candidate = (
            record
            if isinstance(record, PermissionRequestRecord)
            else PermissionRequestRecord.from_dict(_strict_request_mapping(record))
        )
        self._assert_owned(candidate)
        if candidate.phase is not PermissionRequestPhase.CREATED:
            raise ValueError("new queue request must be in created phase")
        if candidate.status is not PermissionRequestStatus.PENDING:
            raise ValueError("new queue request must have pending status")
        if candidate.revision != 0:
            raise ValueError("new queue request revision must be zero")
        if not candidate.arguments_digest or not candidate.request_fingerprint:
            raise ValueError("request arguments_digest and request_fingerprint are required")
        _require_aware_timestamp(candidate.expires_at, "request expires_at")

        safe_record = _safe_request_record(candidate)
        serialized = safe_record.to_dict()
        # Anti-fragmentation/privacy invariant: no replay payload can enter the
        # shared JSON through this facade; exact identity is digest-backed.
        if "arguments" in serialized or "canonical_arguments" in serialized:
            raise AssertionError("PermissionRequestRecord unexpectedly contains raw arguments")
        return self.state_store.create_request(
            safe_record,
            expected_revision=expected_revision,
        )

    create_request = create

    def mark_delivered(
        self,
        request_id: str,
        *,
        expected_request_revision: int | None = None,
        expected_revision: int | None = None,
        expected_state_revision: int | None = None,
        channel: str = "",
        actor: str = "",
    ) -> PermissionRequestRecord:
        self._ensure_enabled()
        record = self._require_owned(request_id)
        if record.phase is PermissionRequestPhase.DELIVERED:
            return record
        # ``expected_revision`` is retained as the historical request-revision
        # spelling.  Store CAS, when needed, is explicitly named state revision.
        supplied_request_revision = (
            expected_request_revision
            if expected_request_revision is not None
            else expected_revision
        )
        revision = record.revision if supplied_request_revision is None else int(supplied_request_revision)
        return self.state_store.mark_delivered(
            request_id,
            expected_request_revision=revision,
            channel=channel or actor,
            expected_revision=expected_state_revision,
        )

    def resolve(
        self,
        response: PermissionResolutionResponse | Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> PermissionResolutionOutcome:
        """Validate and atomically resolve one pending request.

        All malformed, forged, stale, or unauthorized responses return without
        invoking a store mutation.  Store identity/version exceptions also
        abort its mutator before write.  Thus an invalid response cannot change
        pending phase, status, revision, expiry, or idempotency ownership.
        """

        self._ensure_enabled()
        response_id = _response_id_from_value(response)
        try:
            candidate = _coerce_response(response)
        except _InvalidEffect as exc:
            return self._outcome(
                False,
                PermissionResolutionCode.INVALID_EFFECT,
                None,
                response_id,
                str(exc),
            )
        except (TypeError, ValueError, KeyError) as exc:
            return self._outcome(
                False,
                PermissionResolutionCode.MALFORMED,
                None,
                response_id,
                f"malformed permission response: {type(exc).__name__}: {exc}",
            )

        response_id = candidate.idempotency_key
        if candidate.channel not in self._ALLOWED_RESOLUTION_CHANNELS:
            return self._outcome(
                False,
                PermissionResolutionCode.INVALID_SOURCE,
                None,
                response_id,
                "permission response channel is not an authority transport",
            )
        if candidate.session_id != self.session_id:
            return self._outcome(
                False,
                PermissionResolutionCode.IDENTITY_MISMATCH,
                None,
                response_id,
                "permission response belongs to a different queue session",
                mismatch_fields=("session_id",),
            )

        record = self.get(candidate.request_id)
        if record is None:
            return self._outcome(
                False,
                PermissionResolutionCode.NOT_FOUND,
                None,
                response_id,
                "permission request does not exist in this session",
            )

        mismatch = _response_mismatch_fields(record, candidate)
        if mismatch:
            return self._outcome(
                False,
                PermissionResolutionCode.IDENTITY_MISMATCH,
                record,
                response_id,
                "response does not match exact pending request identity",
                mismatch_fields=mismatch,
            )
        if record.terminal:
            return self._terminal_outcome(record, candidate)
        if candidate.expected_revision != record.revision:
            return self._outcome(
                False,
                PermissionResolutionCode.VERSION_CONFLICT,
                record,
                response_id,
                "permission response expected a stale request revision",
            )

        safe_candidate = _safe_response(candidate)
        try:
            resolved = self.state_store.resolve_request(
                safe_candidate,
                expected_revision=expected_revision,
            )
        except PermissionRequestExpired:
            current = self.get(candidate.request_id)
            return self._outcome(
                False,
                PermissionResolutionCode.EXPIRED,
                current,
                response_id,
                "permission request expired before resolution",
            )
        except PermissionIdentityMismatch as exc:
            current = self.get(candidate.request_id)
            return self._outcome(
                False,
                PermissionResolutionCode.IDENTITY_MISMATCH,
                current,
                response_id,
                str(exc),
                mismatch_fields=_response_mismatch_fields(current, candidate) if current else (),
            )
        except PermissionRequestTerminal:
            current = self.get(candidate.request_id)
            if current is None:
                return self._outcome(
                    False,
                    PermissionResolutionCode.NOT_FOUND,
                    None,
                    response_id,
                    "permission request disappeared during resolution",
                )
            return self._terminal_outcome(current, candidate)
        except PermissionStateConflict:
            current = self.get(candidate.request_id)
            if current is not None and current.terminal:
                return self._terminal_outcome(current, candidate)
            return self._outcome(
                False,
                PermissionResolutionCode.VERSION_CONFLICT,
                current,
                response_id,
                "permission request or store revision changed before resolution",
            )

        return self._outcome(
            True,
            PermissionResolutionCode.ACCEPTED,
            resolved,
            response_id,
            "permission response accepted",
            winner_resolution_id=response_id,
        )

    def expire_due(
        self,
        *,
        expected_revision: int | None = None,
    ) -> tuple[PermissionRequestRecord, ...]:
        self._ensure_enabled()
        return tuple(
            self.state_store.expire_requests(
                session_id=self.session_id,
                expected_revision=expected_revision,
            )
        )

    def cancel(
        self,
        request_id: str,
        *,
        reason: str,
        actor: str = "runtime",
        expected_request_revision: int | None = None,
        expected_revision: int | None = None,
    ) -> PermissionRequestRecord:
        return self._terminal(
            request_id,
            PermissionRequestPhase.CANCELLED,
            actor=actor,
            reason=reason,
            expected_request_revision=expected_request_revision,
            expected_revision=expected_revision,
        )

    def abort(
        self,
        request_id: str,
        *,
        reason: str,
        actor: str = "runtime",
        expected_request_revision: int | None = None,
        expected_revision: int | None = None,
    ) -> PermissionRequestRecord:
        return self._terminal(
            request_id,
            PermissionRequestPhase.ABORTED,
            actor=actor,
            reason=reason,
            expected_request_revision=expected_request_revision,
            expected_revision=expected_revision,
        )

    def get(self, request_id: str) -> PermissionRequestRecord | None:
        self._ensure_enabled()
        record = self.state_store.get_request(request_id)
        if record is None or record.session_id != self.session_id:
            return None
        return record

    def list(
        self,
        *,
        phases: Iterable[PermissionRequestPhase | str] | None = None,
        status: PermissionRequestStatus | str | None = None,
        session_id: str | None = None,
    ) -> tuple[PermissionRequestRecord, ...]:
        self._ensure_enabled()
        if session_id is not None and session_id != self.session_id:
            return ()
        phase_set = (
            {PermissionRequestPhase(str(getattr(item, "value", item))) for item in phases}
            if phases is not None
            else None
        )
        status_value = (
            PermissionRequestStatus(str(getattr(status, "value", status)))
            if status is not None
            else None
        )
        records = self.state_store.list_requests(session_id=self.session_id)
        return tuple(
            record
            for record in records
            if (phase_set is None or record.phase in phase_set)
            and (status_value is None or record.status is status_value)
        )

    def pending(self, *, session_id: str | None = None) -> tuple[PermissionRequestRecord, ...]:
        return self.list(
            session_id=session_id,
            phases=(PermissionRequestPhase.CREATED, PermissionRequestPhase.DELIVERED),
            status=PermissionRequestStatus.PENDING,
        )

    def snapshot(self) -> dict[str, Any]:
        self._ensure_enabled()
        snapshot = self.state_store.snapshot(self.session_id)
        if _snapshot_requests_contain_raw_arguments(snapshot):
            raise AssertionError("permission queue snapshot contains a raw argument payload")
        return snapshot

    def restore(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        session_id: str | None = None,
        replace_session: bool = True,
    ) -> tuple[PermissionRequestRecord, ...]:
        self._ensure_enabled()
        if session_id is not None and session_id != self.session_id:
            raise ValueError("permission queue restore target differs from queue session")
        if not replace_session:
            raise ValueError("single-owner store restores a session atomically; replace_session must be true")
        if _snapshot_requests_contain_raw_arguments(snapshot):
            raise ValueError("permission queue snapshot must not contain raw canonical arguments")
        self.state_store.restore_snapshot(
            snapshot,
            self.session_id,
            expected_revision=expected_revision,
        )
        return self.list()

    def metrics(self) -> dict[str, int]:
        records = self.list()
        counts = {phase.value: 0 for phase in PermissionRequestPhase}
        for record in records:
            counts[record.phase.value] += 1
        return {
            **counts,
            "total": len(records),
            "pending": sum(record.status is PermissionRequestStatus.PENDING for record in records),
            "generation": self.generation,
        }

    def _terminal(
        self,
        request_id: str,
        phase: PermissionRequestPhase,
        *,
        actor: str,
        reason: str,
        expected_request_revision: int | None,
        expected_revision: int | None,
    ) -> PermissionRequestRecord:
        self._ensure_enabled()
        record = self._require_owned(request_id)
        if record.phase is phase:
            return record
        revision = record.revision if expected_request_revision is None else int(expected_request_revision)
        return self.state_store.transition_request(
            request_id,
            phase,
            expected_request_revision=revision,
            reason=f"{actor}: {reason}" if actor else reason,
            expected_revision=expected_revision,
        )

    def _terminal_outcome(
        self,
        record: PermissionRequestRecord,
        response: PermissionResolutionResponse,
    ) -> PermissionResolutionOutcome:
        winner = str(record.metadata.get("resolution_idempotency_key") or "")
        if winner and hmac.compare_digest(winner, response.idempotency_key):
            return self._outcome(
                False,
                PermissionResolutionCode.DUPLICATE,
                record,
                response.idempotency_key,
                "response id was already accepted",
                winner_resolution_id=winner,
            )
        if record.phase is PermissionRequestPhase.EXPIRED:
            return self._outcome(
                False,
                PermissionResolutionCode.EXPIRED,
                record,
                response.idempotency_key,
                "permission request expired",
                winner_resolution_id=winner,
            )
        return self._outcome(
            False,
            PermissionResolutionCode.NOT_PENDING,
            record,
            response.idempotency_key,
            f"permission request is already {record.phase.value}",
            winner_resolution_id=winner,
        )

    def _require_owned(self, request_or_id: PermissionRequestRecord | str) -> PermissionRequestRecord:
        if isinstance(request_or_id, PermissionRequestRecord):
            record = request_or_id
        else:
            record = self.get(request_or_id)
            if record is None:
                raise KeyError(request_or_id)
        self._assert_owned(record)
        return record

    def _assert_owned(self, record: PermissionRequestRecord) -> None:
        if record.session_id != self.session_id:
            raise PermissionIdentityMismatch(
                "permission request belongs to a different queue session"
            )

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise PermissionQueueDisabledError("PermissionRequestQueue is disabled")
        if self.state_store.disabled:
            raise PermissionStateDisabled("PermissionStateStore is disabled")

    @staticmethod
    def _outcome(
        accepted: bool,
        code: PermissionResolutionCode,
        request: PermissionRequestRecord | None,
        response_id: str,
        reason: str,
        *,
        mismatch_fields: tuple[str, ...] = (),
        winner_resolution_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> PermissionResolutionOutcome:
        return PermissionResolutionOutcome(
            accepted=accepted,
            code=code,
            request=request,
            response_id=response_id,
            reason=reason,
            mismatch_fields=mismatch_fields,
            winner_resolution_id=winner_resolution_id,
            metadata=dict(metadata or {}),
        )


class _InvalidEffect(ValueError):
    pass


def _coerce_response(
    value: PermissionResolutionResponse | Mapping[str, Any],
) -> PermissionResolutionResponse:
    if isinstance(value, PermissionResolutionResponse):
        response = value
    else:
        if not isinstance(value, Mapping):
            raise TypeError("permission response must be a model or mapping")
        item = dict(value)
        effect_raw = item.get("effect", item.get("decision"))
        if effect_raw is None:
            raise _InvalidEffect("permission response effect is required")
        try:
            effect = PermissionEffect(str(getattr(effect_raw, "value", effect_raw)))
        except ValueError as exc:
            raise _InvalidEffect("permission response effect must be allow or deny") from exc
        if effect not in {PermissionEffect.ALLOW, PermissionEffect.DENY}:
            raise _InvalidEffect("permission response effect must be allow or deny")

        tool_value = item.get("tool_identity")
        if isinstance(tool_value, ToolIdentity):
            tool_identity = tool_value.to_dict()
        elif isinstance(tool_value, Mapping):
            tool_identity = dict(tool_value)
        else:
            tool_identity = {
                "namespace": item.get("namespace"),
                "name": item.get("tool_name"),
                "server_id": item.get("server_id", item.get("server_name", item.get("server"))),
                "version": item.get("tool_version", ""),
                "schema_digest": item.get("tool_schema_digest", ""),
            }
        if not str(tool_identity.get("namespace") or "").strip():
            raise ValueError("permission response tool namespace is required")
        if not str(tool_identity.get("name") or tool_identity.get("tool_name") or "").strip():
            raise ValueError("permission response tool name is required")

        scope_value = item.get("scope")
        if isinstance(scope_value, PermissionScope):
            scope = scope_value.to_dict()
        elif isinstance(scope_value, Mapping):
            scope = dict(scope_value)
        else:
            raise ValueError("permission response scope is required")

        expected = item.get("expected_revision", item.get("expected_version"))
        if expected is None:
            raise ValueError("permission response expected_revision is required")
        idempotency_key = item.get("idempotency_key", item.get("response_id"))
        if not str(idempotency_key or "").strip():
            raise ValueError("permission response idempotency_key is required")
        actor_id = item.get("actor_id", item.get("actor", item.get("source")))
        if not str(actor_id or "").strip():
            raise ValueError("permission response actor_id is required")

        required_text = {
            "request_id": item.get("request_id"),
            "session_id": item.get("session_id"),
            "tool_use_id": item.get("tool_use_id", item.get("tool_call_id")),
            "arguments_digest": item.get("arguments_digest"),
            "request_fingerprint": item.get("request_fingerprint"),
        }
        missing = [name for name, found in required_text.items() if not str(found or "").strip()]
        if missing:
            raise ValueError(f"permission response missing: {', '.join(missing)}")

        response = PermissionResolutionResponse.from_dict(
            {
                **item,
                **required_text,
                "tool_identity": tool_identity,
                "scope": scope,
                "effect": effect.value,
                "actor_id": actor_id,
                "expected_revision": expected,
                "channel": item.get("channel", item.get("source", "user")),
                "idempotency_key": idempotency_key,
            }
        )

    if response.effect not in {PermissionEffect.ALLOW, PermissionEffect.DENY}:
        raise _InvalidEffect("permission response effect must be allow or deny")
    if not response.idempotency_key.strip():
        raise ValueError("permission response idempotency_key is required")
    if not response.actor_id.strip():
        raise ValueError("permission response actor_id is required")
    return response


def _strict_request_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("permission request must be a model or mapping")
    item = dict(value)
    required = (
        "request_id",
        "session_id",
        "task_id",
        "run_id",
        "tool_use_id",
        "tool_identity",
        "arguments_digest",
        "request_fingerprint",
        "scope",
        "expires_at",
        "reason_code",
        "reason",
    )
    missing = [
        name
        for name in required
        if item.get(name) is None or item.get(name) == ""
    ]
    if missing:
        raise ValueError(f"permission request missing: {', '.join(missing)}")
    if "arguments" in item or "canonical_arguments" in item:
        raise ValueError("raw canonical arguments cannot be stored in the permission queue")
    return item


def _safe_request_record(record: PermissionRequestRecord) -> PermissionRequestRecord:
    safe_metadata = _safe_projection(record.metadata)
    safe_scope_metadata = _safe_projection(record.scope.metadata)
    scope = replace(record.scope, metadata=dict(safe_scope_metadata))
    return replace(record, scope=scope, metadata=dict(safe_metadata))


def _safe_response(response: PermissionResolutionResponse) -> PermissionResolutionResponse:
    safe_metadata = _safe_projection(response.metadata)
    rule_scope = response.rule_scope
    if rule_scope is not None:
        rule_scope = replace(rule_scope, metadata=dict(_safe_projection(rule_scope.metadata)))
    return replace(response, metadata=dict(safe_metadata), rule_scope=rule_scope)


def _response_mismatch_fields(
    record: PermissionRequestRecord | None,
    response: PermissionResolutionResponse,
) -> tuple[str, ...]:
    if record is None:
        return ()
    mismatch: list[str] = []
    for expected, actual, name in (
        (record.request_id, response.request_id, "request_id"),
        (record.session_id, response.session_id, "session_id"),
        (record.tool_use_id, response.tool_use_id, "tool_use_id"),
    ):
        if expected != actual:
            mismatch.append(name)
    for expected, actual, name in (
        (record.arguments_digest, response.arguments_digest, "arguments_digest"),
        (record.request_fingerprint, response.request_fingerprint, "request_fingerprint"),
    ):
        if not hmac.compare_digest(expected, actual):
            mismatch.append(name)
    if record.tool_identity.namespace != response.tool_identity.namespace:
        mismatch.append("tool_namespace")
    if record.tool_identity.name != response.tool_identity.name:
        mismatch.append("tool_name")
    if record.tool_identity.server_id != response.tool_identity.server_id:
        mismatch.append("server_name")
    if record.tool_identity.version != response.tool_identity.version:
        mismatch.append("tool_version")
    if record.tool_identity.schema_digest != response.tool_identity.schema_digest:
        mismatch.append("tool_schema_digest")
    if canonical_arguments_json(record.scope.to_dict()) != canonical_arguments_json(response.scope.to_dict()):
        mismatch.append("scope")
    return tuple(mismatch)


def _response_id_from_value(value: Any) -> str:
    if isinstance(value, PermissionResolutionResponse):
        return value.idempotency_key
    if isinstance(value, Mapping):
        return str(value.get("idempotency_key") or value.get("response_id") or "")
    return ""


def _require_aware_timestamp(value: str, name: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "api_key",
    "access_key",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "signature",
    "token",
    "canonical_arguments",
    "tool_input",
)


def _safe_projection(value: Any, *, key: str = "", depth: int = 0) -> Any:
    normalized_key = key.casefold().replace("-", "_")
    if any(marker in normalized_key for marker in _SENSITIVE_KEY_MARKERS):
        return "[REDACTED]"
    if depth >= 12:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_projection(
                child,
                key=str(child_key),
                depth=depth + 1,
            )
            for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_projection(child, depth=depth + 1) for child in list(value)[:256]]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[REDACTED_BINARY bytes:{len(value)}]"
    if isinstance(value, str) and len(value) > 2048:
        return f"[REDACTED_LARGE_VALUE chars:{len(value)}]"
    return value


def _contains_raw_arguments(value: Any, *, key: str = "") -> bool:
    normalized_key = key.casefold().replace("-", "_")
    if normalized_key in {"arguments", "canonical_arguments", "tool_input"}:
        return value not in (None, {}, [], (), "", "[REDACTED]")
    if isinstance(value, Mapping):
        return any(_contains_raw_arguments(child, key=str(child_key)) for child_key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_raw_arguments(child) for child in value)
    return False


def _snapshot_requests_contain_raw_arguments(snapshot: Mapping[str, Any]) -> bool:
    payload = snapshot.get("payload")
    if not isinstance(payload, Mapping):
        return False
    requests = payload.get("requests")
    return _contains_raw_arguments(requests) if isinstance(requests, Mapping) else False


def _request_identity_digest(record: PermissionRequestRecord) -> str:
    """Audit helper: stable digest without raw arguments or secret metadata."""

    material = canonical_arguments_json(
        {
            "request_id": record.request_id,
            "session_id": record.session_id,
            "tool_use_id": record.tool_use_id,
            "tool_identity": record.tool_identity.to_dict(),
            "arguments_digest": record.arguments_digest,
            "request_fingerprint": record.request_fingerprint,
            "scope": record.scope.to_dict(),
            "expires_at": record.expires_at,
        }
    )
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


__all__ = [
    "PermissionApprovalIdentity",
    "PermissionQueueCorruptError",
    "PermissionQueueDisabledError",
    "PermissionRequestPhase",
    "PermissionRequestQueue",
    "PermissionResolutionCode",
    "PermissionResolutionEffect",
    "PermissionResolutionOutcome",
    "PermissionResolutionResponse",
    "QueuedPermissionRequest",
]
