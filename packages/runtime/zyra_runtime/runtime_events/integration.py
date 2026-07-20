"""Zyra-owned integration boundary for canonical TypeScript runtime events."""

from __future__ import annotations

import atexit
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    AppendBatchResult,
    AppendReceipt,
    ArtifactReference,
    DeliveryLease,
    JsonValue,
    ProjectionSnapshot,
    RuntimeEventContractError,
    RuntimeEventEnvelope,
    RuntimeEventPage,
    RuntimeEventProcessError,
    RuntimeEventQuery,
    SpineHealth,
    canonical_json,
    coerce_json,
    optional_string,
    require_mapping,
    require_sequence,
)
from .typescript_port import TypeScriptRuntimeEventPort


LEGACY_EVENT_TYPE_MAP: dict[str, str] = {
    "task_created": "runtime.session.created",
    "task_started": "runtime.session.started",
    "task_completed": "runtime.session.completed",
    "task_failed": "runtime.session.failed",
    "task_cancelled": "runtime.session.cancelled",
    "run_started": "runtime.session.started",
    "run_completed": "runtime.session.completed",
    "run_failed": "runtime.session.failed",
    "step_started": "runtime.agent.turn.started",
    "step_completed": "runtime.agent.turn.completed",
    "step_failed": "runtime.agent.turn.failed",
    "agent_message": "runtime.agent.message",
    "message": "runtime.agent.message",
    "assistant_message": "runtime.agent.message",
    "user_message": "runtime.agent.message",
    "system_notice": "runtime.system.notice",
    "tool_call_started": "runtime.tool.call.started",
    "tool_call": "runtime.tool.call.started",
    "tool_result": "runtime.tool.result.committed",
    "tool_call_completed": "runtime.tool.result.committed",
    "tool_call_failed": "runtime.tool.result.failed",
    "artifact_created": "runtime.artifact.committed",
    "artifact_updated": "runtime.artifact.committed",
    "artifact_deleted": "runtime.artifact.deleted",
    "permission_requested": "runtime.permission.requested",
    "permission_granted": "runtime.permission.resolved",
    "permission_denied": "runtime.permission.resolved",
    "control_requested": "runtime.control.requested",
    "control_applied": "runtime.control.applied",
    "control_rejected": "runtime.control.rejected",
    "checkpoint_created": "runtime.checkpoint.committed",
    "checkpoint_restored": "runtime.checkpoint.restored",
    "memory_written": "runtime.memory.committed",
    "memory_retrieved": "runtime.memory.retrieved",
    "worker_registered": "runtime.worker.registered",
    "worker_health": "runtime.worker.health",
    "worker_assigned": "runtime.worker.assigned",
    "worker_released": "runtime.worker.released",
    "route_selected": "runtime.scheduler.route.selected",
    "route_failed": "runtime.scheduler.route.failed",
    "fault_detected": "runtime.fault.detected",
    "recovery_started": "runtime.recovery.started",
    "recovery_completed": "runtime.recovery.completed",
    "recovery_failed": "runtime.recovery.failed",
    "browser_observation": "runtime.browser.observation",
    "browser_failure": "runtime.browser.failure",
    "skill_invoked": "runtime.skill.invocation.started",
    "skill_completed": "runtime.skill.invocation.completed",
    "subagent_created": "runtime.subagent.created",
    "subagent_progress": "runtime.subagent.progress",
    "subagent_completed": "runtime.subagent.completed",
    "audit_finding": "runtime.audit.finding",
}


TERMINAL_TYPES = {
    "runtime.session.completed",
    "runtime.session.failed",
    "runtime.session.cancelled",
    "runtime.agent.turn.completed",
    "runtime.agent.turn.failed",
    "runtime.tool.result.committed",
    "runtime.tool.result.failed",
    "runtime.control.applied",
    "runtime.control.rejected",
    "runtime.recovery.completed",
    "runtime.recovery.failed",
    "runtime.skill.invocation.completed",
    "runtime.subagent.completed",
}


def _first_string(data: Mapping[str, Any], names: Sequence[str]) -> str | None:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_mapping(data: Mapping[str, Any], names: Sequence[str]) -> Mapping[str, Any]:
    for name in names:
        value = data.get(name)
        if isinstance(value, Mapping):
            return value
    return {}


def _normalize_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        normalized = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return value.strip()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_event_name(value: Any) -> str:
    if hasattr(value, "value"):
        value = getattr(value, "value")
    raw = str(value or "unknown").strip()
    lowered = raw.lower().replace("-", "_").replace(".", "_")
    if raw.startswith("runtime."):
        return raw
    return LEGACY_EVENT_TYPE_MAP.get(lowered, f"runtime.legacy.{lowered or 'unknown'}")


def _derive_subject(data: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    return (
        _first_string(data, ("subject", "worker_id", "workerId", "agent_id", "agentId", "actor"))
        or _first_string(payload, ("worker_id", "workerId", "agent_id", "agentId", "actor"))
        or "zyra-runtime"
    )


def _derive_aggregate(data: Mapping[str, Any], event_type: str) -> tuple[str, str]:
    aggregate_type = _first_string(data, ("aggregate_type", "aggregateType"))
    aggregate_id = _first_string(data, ("aggregate_id", "aggregateId"))
    if aggregate_type and aggregate_id:
        return aggregate_type, aggregate_id
    candidates = (
        ("session", ("session_id", "sessionId", "run_id", "runId")),
        ("task", ("task_id", "taskId")),
        ("tool_call", ("tool_call_id", "toolCallId", "call_id", "callId")),
        ("artifact", ("artifact_id", "artifactId")),
        ("worker", ("worker_id", "workerId")),
        ("subagent", ("subagent_id", "subagentId")),
    )
    payload = _first_mapping(data, ("payload", "data", "details"))
    for inferred_type, names in candidates:
        identifier = _first_string(data, names) or _first_string(payload, names)
        if identifier:
            return aggregate_type or inferred_type, aggregate_id or identifier
    if event_type.startswith("runtime.session."):
        inferred_type = "session"
    elif event_type.startswith("runtime.tool."):
        inferred_type = "tool_call"
    elif event_type.startswith("runtime.worker."):
        inferred_type = "worker"
    else:
        inferred_type = "task"
    correlation = _first_string(data, ("correlation_id", "correlationId", "task_id", "taskId"))
    event_id = _first_string(data, ("event_id", "eventId", "id"))
    return aggregate_type or inferred_type, aggregate_id or correlation or event_id or "global"


def _normalize_artifact_refs(data: Mapping[str, Any]) -> list[dict[str, JsonValue]]:
    raw = data.get("artifact_refs", data.get("artifactRefs", []))
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    refs: list[dict[str, JsonValue]] = []
    for item in require_sequence(raw, "artifact_refs"):
        if isinstance(item, ArtifactReference):
            refs.append(item.to_jsonable())
            continue
        mapping = require_mapping(item, "artifact reference")
        artifact_id = _first_string(mapping, ("artifact_id", "artifactId", "id"))
        digest = _first_string(mapping, ("sha256", "digest"))
        if not artifact_id or not digest:
            continue
        byte_length_raw = mapping.get("byte_length", mapping.get("byteLength", 0))
        try:
            byte_length = max(0, int(byte_length_raw))
        except (TypeError, ValueError):
            byte_length = 0
        ref: dict[str, JsonValue] = {
            "artifactId": artifact_id,
            "sha256": digest,
            "mediaType": str(mapping.get("media_type", mapping.get("mediaType", "application/octet-stream"))),
            "byteLength": byte_length,
            "role": str(mapping.get("role", "payload")),
        }
        uri = _first_string(mapping, ("uri", "path"))
        if uri:
            ref["uri"] = uri
        refs.append(ref)
    return refs


@dataclass(frozen=True, slots=True)
class NormalizedLegacyEvent:
    draft: Mapping[str, JsonValue]
    legacy_event_id: str
    legacy_event_type: str


class LegacyEventNormalizer:
    """Map old EventRecord shapes into the TypeScript event draft contract."""

    def normalize(self, value: Any) -> NormalizedLegacyEvent:
        data = self._to_mapping(value)
        raw_type = data.get("event_type", data.get("eventType", data.get("type")))
        if hasattr(raw_type, "value"):
            raw_type = getattr(raw_type, "value")
        legacy_type = str(raw_type or "unknown")
        event_type = _normalize_event_name(raw_type)
        payload_source = _first_mapping(data, ("payload", "data", "details", "metadata"))
        payload: dict[str, JsonValue] = {
            key: coerce_json(item)
            for key, item in payload_source.items()
        }
        task_id = _first_string(data, ("task_id", "taskId"))
        run_id = _first_string(data, ("run_id", "runId", "session_id", "sessionId"))
        worker_id = _first_string(data, ("worker_id", "workerId"))
        for key, value_to_add in (
            ("taskId", task_id),
            ("sessionId", run_id),
            ("workerId", worker_id),
            ("legacyEventType", legacy_type),
        ):
            if value_to_add is not None and key not in payload:
                payload[key] = value_to_add
        legacy_event_id = (
            _first_string(data, ("event_id", "eventId", "id"))
            or hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
        )
        correlation_id = (
            _first_string(data, ("correlation_id", "correlationId"))
            or run_id
            or task_id
            or legacy_event_id
        )
        causation_id = _first_string(data, ("causation_id", "causationId", "parent_event_id", "parentEventId"))
        if event_type in TERMINAL_TYPES and causation_id is None:
            started_event_id = _first_string(payload_source, ("startedEventId", "started_event_id", "requestEventId"))
            if started_event_id:
                causation_id = started_event_id
        aggregate_type, aggregate_id = _derive_aggregate(data, event_type)
        summary = _first_string(data, ("summary", "message", "name"))
        if not summary:
            summary = f"{legacy_type} for {aggregate_type}:{aggregate_id}"
        draft: dict[str, JsonValue] = {
            "eventId": legacy_event_id,
            "eventType": event_type,
            "aggregateType": aggregate_type,
            "aggregateId": aggregate_id,
            "occurredAt": _normalize_timestamp(
                data.get("occurred_at", data.get("occurredAt", data.get("timestamp")))
            ),
            "producer": _first_string(data, ("producer", "source", "component")) or "zyra-python-runtime",
            "subject": _derive_subject(data, payload_source),
            "correlationId": correlation_id,
            "idempotencyKey": _first_string(data, ("idempotency_key", "idempotencyKey")) or legacy_event_id,
            "trust": _first_string(data, ("trust", "trust_level", "trustLevel")) or "trusted_internal",
            "intent": _first_string(data, ("intent", "delivery_intent", "deliveryIntent")) or self._infer_intent(event_type),
            "payload": payload,
            "summary": summary[:1024],
            "artifactRefs": _normalize_artifact_refs(data),
            "legacy": {
                "eventId": legacy_event_id,
                "eventType": legacy_type,
                "taskId": task_id,
                "runId": run_id,
            },
        }
        if causation_id:
            draft["causationId"] = causation_id
        target_consumers = data.get("target_consumers", data.get("targetConsumers"))
        if target_consumers is not None:
            if isinstance(target_consumers, str):
                draft["targetConsumers"] = [target_consumers]
            else:
                draft["targetConsumers"] = [str(item) for item in require_sequence(target_consumers, "targetConsumers")]
        return NormalizedLegacyEvent(
            draft=draft,
            legacy_event_id=legacy_event_id,
            legacy_event_type=legacy_type,
        )

    def normalize_many(self, values: Iterable[Any]) -> tuple[NormalizedLegacyEvent, ...]:
        return tuple(self.normalize(value) for value in values)

    def as_mapping(self, value: Any) -> Mapping[str, Any]:
        return self._to_mapping(value)

    def _to_mapping(self, value: Any) -> Mapping[str, Any]:
        if isinstance(value, Mapping):
            return value
        if hasattr(value, "to_jsonable"):
            converted = value.to_jsonable()
            return require_mapping(converted, "legacy event")
        if hasattr(value, "model_dump"):
            converted = value.model_dump(mode="json")
            return require_mapping(converted, "legacy event")
        if is_dataclass(value) and not isinstance(value, type):
            return {field.name: getattr(value, field.name) for field in fields(value)}
        if hasattr(value, "__dict__"):
            return {key: item for key, item in vars(value).items() if not key.startswith("_")}
        raise RuntimeEventContractError(f"unsupported legacy event value: {type(value).__name__}")

    def _infer_intent(self, event_type: str) -> str:
        if event_type.startswith("runtime.audit."):
            return "audit"
        if event_type.startswith("runtime.artifact."):
            return "artifact_reference"
        if event_type.startswith("runtime.control.") or event_type.startswith("runtime.permission."):
            return "control"
        if event_type.startswith("runtime.scheduler.") or event_type.startswith("runtime.worker."):
            return "coordination"
        if event_type.startswith("runtime.browser."):
            return "observation"
        return "state_transition"


class RuntimeEventSpineBridge:
    """Python facade over the unique TypeScript canonical store owner."""

    def __init__(
        self,
        port: TypeScriptRuntimeEventPort,
        *,
        normalizer: LegacyEventNormalizer | None = None,
    ) -> None:
        self.port = port
        self.normalizer = normalizer or LegacyEventNormalizer()

    @classmethod
    def create(
        cls,
        *,
        database_path: str | Path,
        artifact_root: str | Path,
        workspace_root: str | Path | None = None,
        node_binary: str | None = None,
    ) -> "RuntimeEventSpineBridge":
        return cls(
            TypeScriptRuntimeEventPort.for_workspace(
                database_path=database_path,
                artifact_root=artifact_root,
                workspace_root=workspace_root,
                node_binary=node_binary,
            )
        )

    def append_draft(self, draft: Mapping[str, Any]) -> AppendReceipt:
        result = self.port.call(
            "append_legacy",
            {"event": coerce_json(draft), "options": {}},
        )
        return AppendReceipt.from_json(result)

    def append_legacy_event(self, event: Any) -> AppendReceipt:
        return self.append_draft(self.normalizer.as_mapping(event))

    def append_legacy_events(self, events: Iterable[Any]) -> AppendBatchResult:
        materialized = tuple(events)
        if not materialized:
            return AppendBatchResult.from_receipts(())
        receipts: list[AppendReceipt] = []
        for index, event in enumerate(materialized):
            mapping = self.normalizer.as_mapping(event)
            try:
                receipts.append(self.append_draft(mapping))
            except RuntimeEventProcessError as error:
                event_type = mapping.get("event_type", mapping.get("eventType", "unknown"))
                raise RuntimeEventProcessError(
                    f"legacy event {index} ({event_type}) failed: {error}",
                    code=error.code,
                    details={**error.details, "legacyEventIndex": index, "legacyEventType": str(event_type)},
                ) from error
        return AppendBatchResult.from_receipts(receipts)

    def query(self, query: RuntimeEventQuery | None = None) -> RuntimeEventPage:
        effective = query or RuntimeEventQuery()
        result = self.port.call("query", {"query": effective.to_jsonable()})
        return RuntimeEventPage.from_json(result)

    def get_event(self, event_id: str) -> RuntimeEventEnvelope | None:
        event_id = event_id.strip()
        if not event_id:
            raise RuntimeEventContractError("event_id must not be empty")
        result = require_mapping(
            self.port.call("get", {"event_id": event_id}),
            "runtime event lookup",
        ).get("event")
        if result is None:
            return None
        return RuntimeEventEnvelope.from_json(result)

    def get_projection(self, projection: str, key: str) -> ProjectionSnapshot | None:
        if not projection.strip() or not key.strip():
            raise RuntimeEventContractError("projection and key must not be empty")
        lookup_key = key.strip()
        raw = require_mapping(
            self.port.call("projection", {"aggregate_id": lookup_key}),
            "runtime projection",
        )
        if raw.get("session") is None:
            page = require_mapping(
                self.port.call(
                    "query",
                    {"query": {"taskId": lookup_key, "descending": True, "limit": 1}},
                ),
                "runtime task projection lookup",
            )
            items = require_sequence(page.get("items", []), "runtime task projection events")
            if not items:
                return None
            aggregate_id = str(require_mapping(items[0], "runtime task projection event").get("aggregateId") or "")
            if not aggregate_id:
                return None
            raw = require_mapping(
                self.port.call("projection", {"aggregate_id": aggregate_id}),
                "runtime projection",
            )
        cursor = require_mapping(raw.get("cursor", {}), "runtime projection cursor")
        projection_name = projection.strip()
        if projection_name == "session":
            state = raw.get("session")
        elif projection_name in {"task", "runtime"}:
            state = raw
        else:
            state = raw.get(projection_name)
        if state is None:
            return None
        return ProjectionSnapshot.from_json(
            {
                "projection": projection_name,
                "key": lookup_key,
                "cursor": int(cursor.get("sequence", 0) or 0),
                "version": 1,
                "state": require_mapping(state, "runtime projection state"),
                "updatedAt": cursor.get("updatedAt") or datetime.now(timezone.utc).isoformat(),
                "status": "current",
            }
        )

    def health(self) -> SpineHealth:
        result = self.port.call("health", {})
        return SpineHealth.from_json(result)

    def metrics(self) -> Mapping[str, JsonValue]:
        result = self.port.call("metrics", {})
        return require_mapping(result, "runtime event metrics")

    def baselines(self) -> Mapping[str, JsonValue]:
        result = self.port.call("baselines", {})
        return require_mapping(result, "runtime event baselines")

    def lease(
        self,
        *,
        consumer_id: str,
        limit: int = 16,
        lease_seconds: int = 30,
    ) -> tuple[DeliveryLease, ...]:
        if not 1 <= limit <= 256:
            raise RuntimeEventContractError("lease limit must be between 1 and 256")
        if not 1 <= lease_seconds <= 3600:
            raise RuntimeEventContractError("lease_seconds must be between 1 and 3600")
        result = self.port.call(
            "poll",
            {"subscription_id": consumer_id, "limit": limit},
        )
        return tuple(
            DeliveryLease.from_json(item)
            for item in require_sequence(result, "delivery leases")
        )

    def acknowledge(self, *, delivery_id: str, lease_token: str) -> bool:
        result = self.port.call(
            "ack",
            {"delivery_id": delivery_id, "lease_token": lease_token},
        )
        if isinstance(result, Mapping):
            return result.get("state") == "acknowledged"
        return bool(result)

    def reject(
        self,
        *,
        delivery_id: str,
        lease_token: str,
        reason: str,
        retry_delay_seconds: int = 1,
    ) -> Mapping[str, JsonValue]:
        result = self.port.call(
            "nack",
            {
                "delivery_id": delivery_id,
                "lease_token": lease_token,
                "error": reason,
                "delay_ms": retry_delay_seconds * 1000,
            },
        )
        return require_mapping(result, "delivery rejection")

    def replay(self, event_id: str, *, idempotency_key: str | None = None) -> AppendReceipt:
        result = self.port.call(
            "replay",
            {"event_id": event_id, "idempotency_key": idempotency_key},
        )
        return AppendReceipt.from_json(result)

    def close(self) -> None:
        self.port.close()


_BRIDGES: dict[tuple[str, str, str], RuntimeEventSpineBridge] = {}
_BRIDGE_LOCK = threading.RLock()


def get_runtime_event_spine(
    *,
    database_path: str | Path,
    artifact_root: str | Path,
    workspace_root: str | Path | None = None,
) -> RuntimeEventSpineBridge:
    database = str(Path(database_path).expanduser().resolve())
    artifacts = str(Path(artifact_root).expanduser().resolve())
    workspace = str(Path(workspace_root).expanduser().resolve()) if workspace_root else ""
    key = (database, artifacts, workspace)
    with _BRIDGE_LOCK:
        bridge = _BRIDGES.get(key)
        if bridge is None:
            bridge = RuntimeEventSpineBridge.create(
                database_path=database,
                artifact_root=artifacts,
                workspace_root=workspace_root,
            )
            _BRIDGES[key] = bridge
        return bridge


def reset_runtime_event_spines() -> None:
    with _BRIDGE_LOCK:
        bridges = tuple(_BRIDGES.values())
        _BRIDGES.clear()
    for bridge in bridges:
        bridge.close()


atexit.register(reset_runtime_event_spines)
