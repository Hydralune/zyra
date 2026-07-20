"""Typed Python contracts for the Zyra runtime event spine.

The canonical writer lives in the TypeScript runtime-event-spine package.  This
module deliberately contains only transport and read-model contracts: it must
never become a second event store or independently assign sequence numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence, TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class RuntimeEventContractError(ValueError):
    """Raised when a TypeScript spine response violates the shared contract."""


class RuntimeEventProcessError(RuntimeError):
    """Raised when the canonical TypeScript process cannot serve a request."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "runtime_event_process_error",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class DeliveryState(str, Enum):
    PENDING = "pending"
    LEASED = "leased"
    ACKNOWLEDGED = "acknowledged"
    DEAD_LETTER = "dead_letter"


class ProjectionStatus(str, Enum):
    EMPTY = "empty"
    CURRENT = "current"
    LAGGING = "lagging"
    FAILED = "failed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeEventContractError(f"{field_name} must be an object")
    return value


def require_sequence(value: Any, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise RuntimeEventContractError(f"{field_name} must be an array")
    return value


def require_string(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise RuntimeEventContractError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise RuntimeEventContractError(f"{field_name} must not be empty")
    return normalized if not allow_empty else value


def optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return require_string(value, field_name)


def require_integer(value: Any, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeEventContractError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise RuntimeEventContractError(f"{field_name} must be >= {minimum}")
    return value


def optional_integer(
    value: Any,
    field_name: str,
    *,
    minimum: int | None = None,
) -> int | None:
    if value is None:
        return None
    return require_integer(value, field_name, minimum=minimum)


def require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeEventContractError(f"{field_name} must be a boolean")
    return value


def coerce_json(value: Any, *, path: str = "$") -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return coerce_json(value.value, path=path)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeEventContractError(f"{path} contains a non-string key")
            result[key] = coerce_json(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [coerce_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if hasattr(value, "to_jsonable"):
        return coerce_json(value.to_jsonable(), path=path)
    if hasattr(value, "model_dump"):
        return coerce_json(value.model_dump(mode="json"), path=path)
    if hasattr(value, "__dict__"):
        public = {key: item for key, item in vars(value).items() if not key.startswith("_")}
        return coerce_json(public, path=path)
    raise RuntimeEventContractError(f"{path} contains unsupported value {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        coerce_json(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    sha256: str
    media_type: str
    byte_length: int
    role: str
    uri: str | None = None

    @classmethod
    def from_json(cls, value: Any) -> "ArtifactReference":
        data = require_mapping(value, "artifact reference")
        return cls(
            artifact_id=require_string(data.get("artifactId", data.get("artifact_id")), "artifactId"),
            sha256=require_string(data.get("sha256", data.get("digest")), "sha256"),
            media_type=require_string(data.get("mediaType", data.get("media_type")), "mediaType"),
            byte_length=require_integer(
                data.get("byteLength", data.get("byte_length", data.get("sizeBytes"))),
                "byteLength",
                minimum=0,
            ),
            role=require_string(data.get("role", data.get("title", "artifact")), "role"),
            uri=optional_string(data.get("uri"), "uri"),
        )

    def to_jsonable(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "artifactId": self.artifact_id,
            "sha256": self.sha256,
            "mediaType": self.media_type,
            "byteLength": self.byte_length,
            "role": self.role,
        }
        if self.uri is not None:
            result["uri"] = self.uri
        return result


@dataclass(frozen=True, slots=True)
class RuntimeEventEnvelope:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_sequence: int
    global_sequence: int
    occurred_at: str
    committed_at: str
    producer: str
    subject: str
    correlation_id: str
    causation_id: str | None
    idempotency_key: str
    trust: str
    intent: str
    payload: Mapping[str, JsonValue]
    summary: str
    artifact_refs: tuple[ArtifactReference, ...]
    content_digest: str
    envelope_bytes: int
    schema_version: int = 1
    canonical: Mapping[str, JsonValue] | None = None

    @classmethod
    def from_json(cls, value: Any) -> "RuntimeEventEnvelope":
        data = require_mapping(value, "runtime event")
        refs = tuple(
            ArtifactReference.from_json(item)
            for item in require_sequence(data.get("artifactRefs", []), "artifactRefs")
        )
        payload = require_mapping(data.get("payload", data.get("inline", {})), "payload")
        identity = require_mapping(data.get("identity", {}), "identity")
        sender = require_mapping(data.get("sender", {}), "sender")
        provenance = require_mapping(data.get("provenance", {}), "provenance")
        aggregate_id = require_string(data.get("aggregateId"), "aggregateId")
        aggregate_sequence = require_integer(data.get("aggregateSequence"), "aggregateSequence", minimum=0)
        is_canonical_spine = data.get("schema") == "zyra.runtime-event/v1"
        return cls(
            event_id=require_string(data.get("eventId"), "eventId"),
            event_type=require_string(data.get("eventType"), "eventType"),
            aggregate_type=require_string(
                data.get("aggregateType", aggregate_id.partition(":")[0] or "runtime"),
                "aggregateType",
            ),
            aggregate_id=aggregate_id,
            aggregate_sequence=aggregate_sequence,
            global_sequence=require_integer(
                data.get("globalSequence", aggregate_sequence + 1),
                "globalSequence",
                minimum=1,
            ),
            occurred_at=require_string(data.get("occurredAt", data.get("createdAt")), "occurredAt"),
            committed_at=require_string(data.get("committedAt"), "committedAt"),
            producer=require_string(data.get("producer", sender.get("id", "runtime")), "producer"),
            subject=require_string(data.get("subject", identity.get("taskId", aggregate_id)), "subject"),
            correlation_id=require_string(data.get("correlationId"), "correlationId"),
            causation_id=optional_string(data.get("causationId"), "causationId"),
            idempotency_key=require_string(data.get("idempotencyKey"), "idempotencyKey"),
            trust=require_string(data.get("trust", provenance.get("trust", "internal")), "trust"),
            intent=require_string(data.get("intent"), "intent"),
            payload={key: coerce_json(item) for key, item in payload.items()},
            summary=require_string(data.get("summary", ""), "summary", allow_empty=True),
            artifact_refs=refs,
            content_digest=require_string(data.get("contentDigest"), "contentDigest"),
            envelope_bytes=require_integer(data.get("envelopeBytes"), "envelopeBytes", minimum=0),
            schema_version=require_integer(
                data.get("schemaVersion", data.get("eventVersion", 1)),
                "schemaVersion",
                minimum=1,
            ),
            canonical=(
                {key: coerce_json(item) for key, item in data.items()}
                if is_canonical_spine
                else None
            ),
        )

    def to_jsonable(self) -> dict[str, JsonValue]:
        if self.canonical is not None:
            return dict(self.canonical)
        result: dict[str, JsonValue] = {
            "eventId": self.event_id,
            "eventType": self.event_type,
            "aggregateType": self.aggregate_type,
            "aggregateId": self.aggregate_id,
            "aggregateSequence": self.aggregate_sequence,
            "globalSequence": self.global_sequence,
            "occurredAt": self.occurred_at,
            "committedAt": self.committed_at,
            "producer": self.producer,
            "subject": self.subject,
            "correlationId": self.correlation_id,
            "idempotencyKey": self.idempotency_key,
            "trust": self.trust,
            "intent": self.intent,
            "payload": dict(self.payload),
            "summary": self.summary,
            "artifactRefs": [ref.to_jsonable() for ref in self.artifact_refs],
            "contentDigest": self.content_digest,
            "envelopeBytes": self.envelope_bytes,
            "schemaVersion": self.schema_version,
        }
        if self.causation_id is not None:
            result["causationId"] = self.causation_id
        return result


@dataclass(frozen=True, slots=True)
class AppendReceipt:
    event: RuntimeEventEnvelope
    duplicate: bool
    routed_deliveries: int
    projection_cursor: int

    @classmethod
    def from_json(cls, value: Any) -> "AppendReceipt":
        data = require_mapping(value, "append receipt")
        raw_event = data.get("event", data.get("envelope"))
        return cls(
            event=RuntimeEventEnvelope.from_json(raw_event),
            duplicate=require_bool(data.get("duplicate", False), "duplicate"),
            routed_deliveries=require_integer(
                data.get(
                    "routedDeliveries",
                    data.get(
                        "deliveryCount",
                        len(require_sequence(data.get("deliveryIds", []), "deliveryIds")),
                    ),
                ),
                "routedDeliveries",
                minimum=0,
            ),
            projection_cursor=require_integer(
                data.get(
                    "projectionCursor",
                    RuntimeEventEnvelope.from_json(raw_event).aggregate_sequence + 1,
                ),
                "projectionCursor",
                minimum=0,
            ),
        )


@dataclass(frozen=True, slots=True)
class AppendBatchResult:
    receipts: tuple[AppendReceipt, ...]
    first_global_sequence: int | None
    last_global_sequence: int | None
    duplicate_count: int

    @classmethod
    def from_json(cls, value: Any) -> "AppendBatchResult":
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            receipts = tuple(AppendReceipt.from_json(item) for item in value)
            return cls.from_receipts(receipts)
        data = require_mapping(value, "append batch result")
        receipts = tuple(
            AppendReceipt.from_json(item)
            for item in require_sequence(data.get("receipts", []), "receipts")
        )
        computed = cls.from_receipts(receipts)
        return cls(
            receipts=receipts,
            first_global_sequence=optional_integer(
                data.get("firstGlobalSequence", computed.first_global_sequence),
                "firstGlobalSequence",
                minimum=1,
            ),
            last_global_sequence=optional_integer(
                data.get("lastGlobalSequence", computed.last_global_sequence),
                "lastGlobalSequence",
                minimum=1,
            ),
            duplicate_count=require_integer(
                data.get("duplicateCount", computed.duplicate_count),
                "duplicateCount",
                minimum=0,
            ),
        )

    @classmethod
    def from_receipts(cls, receipts: Iterable[AppendReceipt]) -> "AppendBatchResult":
        materialized = tuple(receipts)
        sequences = [receipt.event.global_sequence for receipt in materialized]
        return cls(
            receipts=materialized,
            first_global_sequence=min(sequences) if sequences else None,
            last_global_sequence=max(sequences) if sequences else None,
            duplicate_count=sum(1 for receipt in materialized if receipt.duplicate),
        )

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "receipts": [
                {
                    "event": receipt.event.to_jsonable(),
                    "duplicate": receipt.duplicate,
                    "routedDeliveries": receipt.routed_deliveries,
                    "projectionCursor": receipt.projection_cursor,
                }
                for receipt in self.receipts
            ],
            "firstGlobalSequence": self.first_global_sequence,
            "lastGlobalSequence": self.last_global_sequence,
            "duplicateCount": self.duplicate_count,
        }


@dataclass(frozen=True, slots=True)
class RuntimeEventQuery:
    after_sequence: int = 0
    limit: int = 100
    event_types: tuple[str, ...] = ()
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    task_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    producer: str | None = None
    subject: str | None = None
    intent: str | None = None
    trust: str | None = None
    artifact_id: str | None = None
    descending: bool = False

    def __post_init__(self) -> None:
        if self.after_sequence < 0:
            raise RuntimeEventContractError("after_sequence must be non-negative")
        if not 1 <= self.limit <= 1000:
            raise RuntimeEventContractError("limit must be between 1 and 1000")
        for event_type in self.event_types:
            require_string(event_type, "event_types item")

    def to_jsonable(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "afterGlobalSequence": self.after_sequence,
            "limit": self.limit,
            "descending": self.descending,
        }
        optional: tuple[tuple[str, Any], ...] = (
            ("eventTypes", list(self.event_types) if self.event_types else None),
            ("aggregateType", self.aggregate_type),
            ("aggregateId", self.aggregate_id),
            ("taskId", self.task_id),
            ("correlationId", self.correlation_id),
            ("causationId", self.causation_id),
            ("producer", self.producer),
            ("subject", self.subject),
            ("intent", self.intent),
            ("trust", self.trust),
            ("artifactId", self.artifact_id),
        )
        for key, value in optional:
            if value is not None:
                result[key] = coerce_json(value)
        return result

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "RuntimeEventQuery":
        raw_types = params.get("event_types", params.get("eventTypes", ()))
        if isinstance(raw_types, str):
            types = tuple(item.strip() for item in raw_types.split(",") if item.strip())
        elif raw_types is None:
            types = ()
        else:
            types = tuple(require_string(item, "event_types item") for item in require_sequence(raw_types, "event_types"))
        descending_raw = params.get("descending", False)
        if isinstance(descending_raw, str):
            descending = descending_raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            descending = bool(descending_raw)
        return cls(
            after_sequence=int(params.get("after_sequence", params.get("afterSequence", 0)) or 0),
            limit=int(params.get("limit", 100) or 100),
            event_types=types,
            aggregate_type=optional_string(params.get("aggregate_type", params.get("aggregateType")), "aggregate_type"),
            aggregate_id=optional_string(params.get("aggregate_id", params.get("aggregateId")), "aggregate_id"),
            task_id=optional_string(params.get("task_id", params.get("taskId")), "task_id"),
            correlation_id=optional_string(params.get("correlation_id", params.get("correlationId")), "correlation_id"),
            causation_id=optional_string(params.get("causation_id", params.get("causationId")), "causation_id"),
            producer=optional_string(params.get("producer"), "producer"),
            subject=optional_string(params.get("subject"), "subject"),
            intent=optional_string(params.get("intent"), "intent"),
            trust=optional_string(params.get("trust"), "trust"),
            artifact_id=optional_string(params.get("artifact_id", params.get("artifactId")), "artifact_id"),
            descending=descending,
        )


@dataclass(frozen=True, slots=True)
class RuntimeEventPage:
    events: tuple[RuntimeEventEnvelope, ...]
    next_sequence: int
    has_more: bool
    scanned: int
    matched: int
    high_watermark: int

    @classmethod
    def from_json(cls, value: Any) -> "RuntimeEventPage":
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            events = tuple(RuntimeEventEnvelope.from_json(item) for item in value)
            high = max((item.global_sequence for item in events), default=0)
            return cls(events, high, False, len(events), len(events), high)
        data = require_mapping(value, "runtime event page")
        events = tuple(
            RuntimeEventEnvelope.from_json(item)
            for item in require_sequence(data.get("events", data.get("items", [])), "events")
        )
        default_next = events[-1].global_sequence if events else 0
        return cls(
            events=events,
            next_sequence=require_integer(
                data.get("nextSequence", data.get("cursor", default_next)),
                "nextSequence",
                minimum=0,
            ),
            has_more=require_bool(data.get("hasMore", False), "hasMore"),
            scanned=require_integer(data.get("scanned", len(events)), "scanned", minimum=0),
            matched=require_integer(data.get("matched", len(events)), "matched", minimum=0),
            high_watermark=require_integer(
                data.get("highWatermark", default_next),
                "highWatermark",
                minimum=0,
            ),
        )

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "events": [event.to_jsonable() for event in self.events],
            "nextSequence": self.next_sequence,
            "hasMore": self.has_more,
            "scanned": self.scanned,
            "matched": self.matched,
            "highWatermark": self.high_watermark,
        }


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    projection: str
    key: str
    cursor: int
    version: int
    state: Mapping[str, JsonValue]
    updated_at: str
    status: ProjectionStatus = ProjectionStatus.CURRENT

    @classmethod
    def from_json(cls, value: Any) -> "ProjectionSnapshot":
        data = require_mapping(value, "projection snapshot")
        raw_state = require_mapping(data.get("state", data.get("value", {})), "state")
        raw_status = data.get("status", ProjectionStatus.CURRENT.value)
        try:
            status = ProjectionStatus(str(raw_status))
        except ValueError as error:
            raise RuntimeEventContractError(f"unknown projection status: {raw_status}") from error
        return cls(
            projection=require_string(data.get("projection", data.get("name", "runtime")), "projection"),
            key=require_string(data.get("key", data.get("id", "global")), "key"),
            cursor=require_integer(data.get("cursor", 0), "cursor", minimum=0),
            version=require_integer(data.get("version", 1), "version", minimum=1),
            state={key: coerce_json(item) for key, item in raw_state.items()},
            updated_at=require_string(data.get("updatedAt", utc_now_iso()), "updatedAt"),
            status=status,
        )

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "projection": self.projection,
            "key": self.key,
            "cursor": self.cursor,
            "version": self.version,
            "state": dict(self.state),
            "updatedAt": self.updated_at,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class SpineHealth:
    ok: bool
    database_path: str
    artifact_root: str
    high_watermark: int
    projection_cursor: int
    projection_lag: int
    pending_deliveries: int
    leased_deliveries: int
    dead_letters: int
    process_pid: int | None
    schema_version: int
    details: Mapping[str, JsonValue] = field(default_factory=dict)

    @classmethod
    def from_json(cls, value: Any) -> "SpineHealth":
        data = require_mapping(value, "spine health")
        store = require_mapping(data.get("store", {}), "spine store health")
        high = require_integer(
            data.get("highWatermark", store.get("highWatermark", 0)),
            "highWatermark",
            minimum=0,
        )
        cursor = require_integer(data.get("projectionCursor", high), "projectionCursor", minimum=0)
        details_raw = require_mapping(data.get("details", data), "details")
        return cls(
            ok=require_bool(data.get("ok", True), "ok"),
            database_path=require_string(data.get("databasePath", store.get("path", "unknown")), "databasePath"),
            artifact_root=require_string(data.get("artifactRoot", "unknown"), "artifactRoot"),
            high_watermark=high,
            projection_cursor=cursor,
            projection_lag=require_integer(data.get("projectionLag", max(0, high - cursor)), "projectionLag", minimum=0),
            pending_deliveries=require_integer(data.get("pendingDeliveries", store.get("pendingDeliveryCount", 0)), "pendingDeliveries", minimum=0),
            leased_deliveries=require_integer(data.get("leasedDeliveries", 0), "leasedDeliveries", minimum=0),
            dead_letters=require_integer(data.get("deadLetters", store.get("deadLetterCount", 0)), "deadLetters", minimum=0),
            process_pid=optional_integer(data.get("processPid"), "processPid", minimum=1),
            schema_version=require_integer(data.get("schemaVersion", 1), "schemaVersion", minimum=1),
            details={key: coerce_json(item) for key, item in details_raw.items()},
        )

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "ok": self.ok,
            "databasePath": self.database_path,
            "artifactRoot": self.artifact_root,
            "highWatermark": self.high_watermark,
            "projectionCursor": self.projection_cursor,
            "projectionLag": self.projection_lag,
            "pendingDeliveries": self.pending_deliveries,
            "leasedDeliveries": self.leased_deliveries,
            "deadLetters": self.dead_letters,
            "processPid": self.process_pid,
            "schemaVersion": self.schema_version,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class DeliveryLease:
    delivery_id: str
    subscription_id: str
    consumer_id: str
    event: RuntimeEventEnvelope
    attempt: int
    lease_token: str
    lease_expires_at: str
    first_available_at: str

    @classmethod
    def from_json(cls, value: Any) -> "DeliveryLease":
        data = require_mapping(value, "delivery lease")
        return cls(
            delivery_id=require_string(data.get("deliveryId"), "deliveryId"),
            subscription_id=require_string(data.get("subscriptionId"), "subscriptionId"),
            consumer_id=require_string(data.get("consumerId"), "consumerId"),
            event=RuntimeEventEnvelope.from_json(data.get("event")),
            attempt=require_integer(data.get("attempt", 1), "attempt", minimum=1),
            lease_token=require_string(data.get("leaseToken"), "leaseToken"),
            lease_expires_at=require_string(data.get("leaseExpiresAt"), "leaseExpiresAt"),
            first_available_at=require_string(data.get("firstAvailableAt"), "firstAvailableAt"),
        )


@dataclass(frozen=True, slots=True)
class RpcErrorPayload:
    code: str
    message: str
    details: Mapping[str, JsonValue]
    retryable: bool

    @classmethod
    def from_json(cls, value: Any) -> "RpcErrorPayload":
        data = require_mapping(value, "rpc error")
        details = require_mapping(data.get("details", {}), "details")
        return cls(
            code=require_string(data.get("code", "runtime_event_rpc_error"), "code"),
            message=require_string(data.get("message", "runtime event request failed"), "message"),
            details={key: coerce_json(item) for key, item in details.items()},
            retryable=bool(data.get("retryable", False)),
        )


def merge_json_objects(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Deterministically merge JSON objects without mutating caller state."""

    result: dict[str, JsonValue] = {key: coerce_json(value) for key, value in base.items()}
    for key in sorted(overlay):
        value = overlay[key]
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            result[key] = merge_json_objects(current, value)
        else:
            result[key] = coerce_json(value)
    return result


def redact_sensitive_fields(value: Any) -> JsonValue:
    """Redact transport diagnostics while retaining useful failure structure."""

    sensitive = {
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "cookie",
    }
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in sensitive or normalized.endswith("_secret") or normalized.endswith("_token"):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact_sensitive_fields(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_fields(item) for item in value]
    return coerce_json(value)
