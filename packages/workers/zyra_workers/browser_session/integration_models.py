from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from zyra_core import ArtifactRef, EventRecord

from .models import browser_id, browser_now


def integration_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def public_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(key): _public_value(str(key), item) for key, item in (value or {}).items()}


def _public_value(key: str, value: Any) -> Any:
    normalized = key.casefold().replace("-", "_")
    if any(token in normalized for token in ("authorization", "cookie", "password", "secret", "token", "api_key")):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return public_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return [_public_value(key, item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str) and (
        normalized.endswith("url")
        or normalized in {"url", "endpoint", "endpoint_url", "proxy_url", "current_url", "target_url"}
    ):
        return _public_url(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _public_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return "[INVALID_URL]"
    if not parsed.scheme:
        return value.split("?", 1)[0].split("#", 1)[0]
    host = parsed.hostname or ""
    if port:
        host = f"{host}:{port}"
    query_names = sorted({name for name, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    query = urlencode([(name, "[REDACTED]") for name in query_names])
    return urlunsplit((parsed.scheme, host, parsed.path, query, ""))


def _execution_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in (value or {}).items():
        if isinstance(item, Mapping):
            result[str(key)] = _execution_mapping(item)
        elif isinstance(item, list):
            result[str(key)] = [
                _execution_mapping(element) if isinstance(element, Mapping) else element
                for element in item
            ]
        elif isinstance(item, tuple):
            result[str(key)] = tuple(
                _execution_mapping(element) if isinstance(element, Mapping) else element
                for element in item
            )
        elif isinstance(item, Path):
            result[str(key)] = str(item)
        else:
            result[str(key)] = item
    return result


class BrowserActionName(StrEnum):
    NAVIGATE = "navigate"
    SCREENSHOT = "take_screenshot"
    EVALUATE_JS = "evaluate_js"
    COLLECT_DOWNLOADS = "collect_downloads"
    CAPTURE_TRACE = "capture_trace"
    LIST_TARGETS = "list_targets"
    FOCUS_TARGET = "focus_target"


ACTION_ALIASES: dict[str, BrowserActionName] = {
    "open_url": BrowserActionName.NAVIGATE,
    "navigate": BrowserActionName.NAVIGATE,
    "screenshot": BrowserActionName.SCREENSHOT,
    "take_screenshot": BrowserActionName.SCREENSHOT,
    "evaluate": BrowserActionName.EVALUATE_JS,
    "evaluate_js": BrowserActionName.EVALUATE_JS,
    "downloads": BrowserActionName.COLLECT_DOWNLOADS,
    "collect_download": BrowserActionName.COLLECT_DOWNLOADS,
    "collect_downloads": BrowserActionName.COLLECT_DOWNLOADS,
    "trace": BrowserActionName.CAPTURE_TRACE,
    "capture_trace": BrowserActionName.CAPTURE_TRACE,
    "targets": BrowserActionName.LIST_TARGETS,
    "list_targets": BrowserActionName.LIST_TARGETS,
    "focus": BrowserActionName.FOCUS_TARGET,
    "focus_target": BrowserActionName.FOCUS_TARGET,
}


def normalize_action_name(value: str | BrowserActionName) -> BrowserActionName:
    if isinstance(value, BrowserActionName):
        return value
    normalized = str(value).strip().casefold().replace("-", "_")
    try:
        return ACTION_ALIASES[normalized]
    except KeyError as error:
        raise ValueError(f"unsupported productized browser action: {value}") from error


class BrowserActionStatus(StrEnum):
    PENDING = "pending"
    PERMISSION_BLOCKED = "permission_blocked"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BrowserLeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class BrowserActionRequest:
    run_id: str
    task_id: str
    worker_request_id: str
    browser_session_id: str
    step_index: int
    action: BrowserActionName
    arguments: Mapping[str, Any] = field(default_factory=dict)
    node_id: str = ""
    continue_on_error: bool = False
    request_id: str = field(default_factory=lambda: browser_id("braction"))
    created_at: str = field(default_factory=browser_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id or not self.task_id or not self.worker_request_id or not self.browser_session_id:
            raise ValueError("browser action identity is incomplete")
        if self.step_index < 1:
            raise ValueError("browser action step index must be positive")
        object.__setattr__(self, "action", normalize_action_name(self.action))
        # Execution values and persisted/public values are separate. Permission
        # and CDP receive the exact request; to_dict/receipts redact it.
        object.__setattr__(self, "arguments", _execution_mapping(self.arguments))
        object.__setattr__(self, "metadata", public_mapping(self.metadata))
        self._validate_arguments()

    @property
    def fingerprint(self) -> str:
        return integration_digest({
            "run_id": self.run_id,
            "task_id": self.task_id,
            "browser_session_id": self.browser_session_id,
            "step_index": self.step_index,
            "action": str(self.action),
            "arguments": _execution_mapping(self.arguments),
        })

    def _validate_arguments(self) -> None:
        required = {
            BrowserActionName.NAVIGATE: "url",
            BrowserActionName.EVALUATE_JS: "code",
            BrowserActionName.FOCUS_TARGET: "target_id",
        }
        field_name = required.get(self.action)
        if field_name and not str(self.arguments.get(field_name) or "").strip():
            raise ValueError(f"browser action {self.action} requires {field_name}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "worker_request_id": self.worker_request_id,
            "browser_session_id": self.browser_session_id,
            "step_index": self.step_index,
            "action": str(self.action),
            "arguments": public_mapping(self.arguments),
            "continue_on_error": self.continue_on_error,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_plan_item(
        cls,
        item: Mapping[str, Any],
        *,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        browser_session_id: str,
        step_index: int,
        node_id: str = "",
        default_continue_on_error: bool = False,
    ) -> "BrowserActionRequest":
        arguments = item.get("arguments")
        raw_arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
        action = normalize_action_name(str(item.get("action") or item.get("browser_action") or ""))
        execution_identity = str(
            item.get("idempotency_key")
            or item.get("action_request_id")
            or item.get("_zyra_execution_id")
            or worker_request_id
        )
        identity = integration_digest({
            "run_id": run_id,
            "task_id": task_id,
            "execution_identity": execution_identity,
            "browser_session_id": browser_session_id,
            "step_index": step_index,
            "action": str(action),
            "arguments": raw_arguments,
        }).removeprefix("sha256:")[:24]
        return cls(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_request_id=worker_request_id,
            browser_session_id=browser_session_id,
            step_index=step_index,
            action=action,
            arguments=raw_arguments,
            continue_on_error=bool(item.get("continue_on_error", default_continue_on_error)),
            request_id=f"braction_{identity}",
            metadata=public_mapping(item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}),
        )


@dataclass(frozen=True, slots=True)
class BrowserArtifactHandoff:
    browser_session_id: str
    action_request_id: str
    artifact_id: str
    kind: str
    uri: str
    sha256: str
    size_bytes: int
    media_type: str
    run_id: str
    task_id: str
    handoff_id: str = field(default_factory=lambda: browser_id("brhandoff"))
    created_at: str = field(default_factory=browser_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.browser_session_id or not self.artifact_id or not self.run_id or not self.task_id:
            raise ValueError("browser artifact handoff identity is incomplete")
        if self.size_bytes < 0:
            raise ValueError("browser artifact size cannot be negative")
        object.__setattr__(self, "metadata", public_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "browser_session_id": self.browser_session_id,
            "action_request_id": self.action_request_id,
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BrowserActionReceipt:
    request_id: str
    request_fingerprint: str
    browser_session_id: str
    action: BrowserActionName
    step_index: int
    status: BrowserActionStatus
    started_at: str
    completed_at: str
    output: Mapping[str, Any] = field(default_factory=dict)
    artifact_handoffs: tuple[BrowserArtifactHandoff, ...] = ()
    permission_metadata: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    receipt_id: str = field(default_factory=lambda: browser_id("brreceipt"))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", normalize_action_name(self.action))
        object.__setattr__(self, "status", BrowserActionStatus(str(self.status)))
        object.__setattr__(self, "output", public_mapping(self.output))
        object.__setattr__(self, "permission_metadata", public_mapping(self.permission_metadata))
        object.__setattr__(self, "metadata", public_mapping(self.metadata))

    @property
    def ok(self) -> bool:
        return self.status == BrowserActionStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "browser_session_id": self.browser_session_id,
            "action": str(self.action),
            "step_index": self.step_index,
            "status": str(self.status),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "output": dict(self.output),
            "artifact_handoffs": [item.to_dict() for item in self.artifact_handoffs],
            "permission_metadata": dict(self.permission_metadata),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
            "ok": self.ok,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BrowserActionReceipt":
        return cls(
            receipt_id=str(value.get("receipt_id") or browser_id("brreceipt")),
            request_id=str(value.get("request_id") or ""),
            request_fingerprint=str(value.get("request_fingerprint") or ""),
            browser_session_id=str(value.get("browser_session_id") or ""),
            action=normalize_action_name(str(value.get("action") or "")),
            step_index=int(value.get("step_index") or 0),
            status=BrowserActionStatus(str(value.get("status") or BrowserActionStatus.FAILED)),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            output=dict(value.get("output") or {}),
            artifact_handoffs=tuple(BrowserArtifactHandoff(**dict(item)) for item in value.get("artifact_handoffs", ())),
            permission_metadata=dict(value.get("permission_metadata") or {}),
            error_code=str(value.get("error_code") or ""),
            error_message=str(value.get("error_message") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class BrowserSessionLease:
    browser_session_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    owner_id: str
    generation: int
    acquired_at: str
    expires_at_epoch: float
    status: BrowserLeaseStatus = BrowserLeaseStatus.ACTIVE
    lease_id: str = field(default_factory=lambda: browser_id("brlease"))
    released_at: str = ""
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.browser_session_id, self.run_id, self.task_id, self.worker_request_id, self.owner_id)):
            raise ValueError("browser session lease identity is incomplete")
        if self.generation < 1 or self.expires_at_epoch <= 0:
            raise ValueError("browser session lease generation/expiry is invalid")
        object.__setattr__(self, "status", BrowserLeaseStatus(str(self.status)))
        object.__setattr__(self, "metadata", public_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "browser_session_id": self.browser_session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "owner_id": self.owner_id,
            "generation": self.generation,
            "acquired_at": self.acquired_at,
            "expires_at_epoch": self.expires_at_epoch,
            "status": str(self.status),
            "released_at": self.released_at,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BrowserSessionLease":
        return cls(
            lease_id=str(value.get("lease_id") or browser_id("brlease")),
            browser_session_id=str(value.get("browser_session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            worker_request_id=str(value.get("worker_request_id") or ""),
            owner_id=str(value.get("owner_id") or ""),
            generation=int(value.get("generation") or 0),
            acquired_at=str(value.get("acquired_at") or ""),
            expires_at_epoch=float(value.get("expires_at_epoch") or 0),
            status=BrowserLeaseStatus(str(value.get("status") or BrowserLeaseStatus.REVOKED)),
            released_at=str(value.get("released_at") or ""),
            reason=str(value.get("reason") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class BrowserActionExecution:
    receipt: BrowserActionReceipt
    events: tuple[EventRecord, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()

    @property
    def ok(self) -> bool:
        return self.receipt.ok


@dataclass(frozen=True, slots=True)
class BrowserApplicationResult:
    ok: bool
    events: tuple[EventRecord, ...]
    artifacts: tuple[ArtifactRef, ...]
    action_receipts: tuple[BrowserActionReceipt, ...]
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    control_receipts: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", public_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "events": [event.payload | {"event_id": event.event_id, "event_type": str(event.event_type)} for event in self.events],
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "kind": str(item.kind),
                    "uri": item.uri,
                    "title": item.title,
                    "producer_node_id": item.producer_node_id,
                    "created_at": item.created_at,
                    "metadata": dict(item.metadata),
                }
                for item in self.artifacts
            ],
            "action_receipts": [item.to_dict() for item in self.action_receipts],
            "control_receipts": [dict(item) for item in self.control_receipts],
            "error": self.error,
            "metadata": dict(self.metadata),
        }


def validate_plan(plan: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    issues: list[str] = []
    for index, item in enumerate(plan, start=1):
        try:
            action = normalize_action_name(str(item.get("action") or item.get("browser_action") or ""))
        except ValueError as error:
            issues.append(f"step {index}: {error}")
            continue
        arguments = item.get("arguments")
        if arguments is not None and not isinstance(arguments, Mapping):
            issues.append(f"step {index} {action}: arguments must be an object")
    return tuple(issues)
