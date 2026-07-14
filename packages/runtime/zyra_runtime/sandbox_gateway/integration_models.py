from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST_PATTERN = re.compile(r"^(?:sha256:)?[a-f0-9]{64}$")


def canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {
            "encoding": "sha256",
            "bytes": len(value),
            "digest": content_digest(value),
        }
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (canonical_value(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, default=str),
        )
    if hasattr(value, "safe_dict") and callable(value.safe_dict):
        return canonical_value(value.safe_dict())
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return canonical_value(value.to_dict())
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def stable_identifier(namespace: str, *parts: Any, length: int = 32) -> str:
    if not namespace or not _ID_PATTERN.fullmatch(namespace):
        raise ValueError("namespace must be a stable identifier")
    if not 12 <= length <= 64:
        raise ValueError("stable identifier length must be between 12 and 64")
    material = canonical_json([namespace, *parts]).encode("utf-8")
    suffix = hashlib.sha256(material).hexdigest()[:length]
    return f"{namespace}:{suffix}"


def require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if not _ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} is not a stable identifier")
    return normalized


def require_optional_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if normalized:
        require_identifier(normalized, field_name)
    return normalized


def require_digest(value: str, field_name: str, *, optional: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if optional and not normalized:
        return ""
    if not _DIGEST_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a sha256 digest")
    return normalized if normalized.startswith("sha256:") else f"sha256:{normalized}"


def freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            str(key): canonical_value(item)
            for key, item in sorted(dict(value or {}).items(), key=lambda pair: str(pair[0]))
        }
    )


def freeze_strings(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(item) for item in values)


class GatewaySurface(StrEnum):
    CODE_WORKER = "code_worker"
    BROWSER_WORKER = "browser_worker"
    MCP_TOOL = "mcp_tool"
    HOST_TOOL = "host_tool"
    REMOTE_WORKER = "remote_worker"
    CONTROL_COMMAND = "control_command"
    ARTIFACT_PIPELINE = "artifact_pipeline"


class GatewayAction(StrEnum):
    COMMAND = "command"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_EDIT = "file_edit"
    FILE_DELETE = "file_delete"
    ARTIFACT_PUBLISH = "artifact_publish"
    BROWSER_FETCH = "browser_fetch"
    BROWSER_UPLOAD = "browser_upload"
    BROWSER_DOWNLOAD = "browser_download"
    MCP_CALL = "mcp_call"
    BACKEND_DISPATCH = "backend_dispatch"
    BACKEND_CANCEL = "backend_cancel"
    SESSION_CLOSE = "session_close"


class GatewayOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING = "pending"
    COMMITTED = "committed"
    QUARANTINED = "quarantined"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class FailureClass(StrEnum):
    POLICY = "policy"
    PERMISSION = "permission"
    WORKSPACE = "workspace"
    BACKEND = "backend"
    TIMEOUT = "timeout"
    CANCEL = "cancel"
    INTEGRITY = "integrity"
    PROVENANCE = "provenance"
    CREDENTIAL = "credential"
    OUTPUT_BUDGET = "output_budget"
    DISABLED = "disabled"
    INTERNAL = "internal"


class TrustDisposition(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class RecoveryAction(StrEnum):
    RETRY = "retry"
    REPLAN = "replan"
    REBIND_WORKSPACE = "rebind_workspace"
    REPLACE_BACKEND = "replace_backend"
    REQUEST_PERMISSION = "request_permission"
    REDUCE_SCOPE = "reduce_scope"
    DISCARD_OUTPUT = "discard_output"
    QUARANTINE = "quarantine"
    CANCEL_TASK = "cancel_task"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class WorkerGatewayIdentity:
    run_id: str
    task_id: str
    worker_id: str
    session_id: str
    node_id: str = ""
    request_id: str = ""
    workspace_id: str = ""
    owner_epoch: int = 0
    backend_id: str = "local-process"
    generation: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", require_identifier(self.run_id, "run_id"))
        object.__setattr__(self, "task_id", require_identifier(self.task_id, "task_id"))
        object.__setattr__(self, "worker_id", require_identifier(self.worker_id, "worker_id"))
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        object.__setattr__(self, "node_id", require_optional_identifier(self.node_id, "node_id"))
        object.__setattr__(self, "request_id", require_optional_identifier(self.request_id, "request_id"))
        object.__setattr__(self, "workspace_id", require_optional_identifier(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "backend_id", require_identifier(self.backend_id, "backend_id"))
        if self.owner_epoch < 0:
            raise ValueError("owner_epoch cannot be negative")
        if self.generation < 0:
            raise ValueError("generation cannot be negative")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def binding_digest(self) -> str:
        return content_digest(
            {
                "run_id": self.run_id,
                "task_id": self.task_id,
                "worker_id": self.worker_id,
                "session_id": self.session_id,
                "node_id": self.node_id,
                "request_id": self.request_id,
                "workspace_id": self.workspace_id,
                "owner_epoch": self.owner_epoch,
                "backend_id": self.backend_id,
                "generation": self.generation,
            }
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "node_id": self.node_id,
            "request_id": self.request_id,
            "workspace_id": self.workspace_id,
            "owner_epoch": self.owner_epoch,
            "backend_id": self.backend_id,
            "generation": self.generation,
            "binding_digest": self.binding_digest,
            "metadata": canonical_value(self.metadata),
        }

    def advance(self, *, owner_epoch: int | None = None, generation: int | None = None) -> "WorkerGatewayIdentity":
        return replace(
            self,
            owner_epoch=self.owner_epoch if owner_epoch is None else owner_epoch,
            generation=self.generation if generation is None else generation,
        )


@dataclass(frozen=True, slots=True)
class GatewaySubject:
    surface: GatewaySurface
    action: GatewayAction
    logical_name: str
    subject_digest: str
    trust: TrustDisposition = TrustDisposition.TRUSTED
    provenance_ref: str = ""
    content_bytes: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface", GatewaySurface(self.surface))
        object.__setattr__(self, "action", GatewayAction(self.action))
        if not str(self.logical_name or "").strip():
            raise ValueError("logical_name is required")
        object.__setattr__(self, "logical_name", str(self.logical_name).strip())
        object.__setattr__(self, "subject_digest", require_digest(self.subject_digest, "subject_digest"))
        object.__setattr__(self, "trust", TrustDisposition(self.trust))
        object.__setattr__(
            self,
            "provenance_ref",
            require_optional_identifier(self.provenance_ref, "provenance_ref"),
        )
        if self.content_bytes < 0:
            raise ValueError("content_bytes cannot be negative")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface.value,
            "action": self.action.value,
            "logical_name": self.logical_name,
            "subject_digest": self.subject_digest,
            "trust": self.trust.value,
            "provenance_ref": self.provenance_ref,
            "content_bytes": self.content_bytes,
            "metadata": canonical_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GatewayInvocation:
    invocation_id: str
    identity: WorkerGatewayIdentity
    subject: GatewaySubject
    tool_call_id: str
    arguments_digest: str
    policy_digest: str = ""
    permission_request_id: str = ""
    permission_grant_digest: str = ""
    idempotency_key: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = "zyra.gateway-invocation.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "invocation_id", require_identifier(self.invocation_id, "invocation_id"))
        object.__setattr__(self, "tool_call_id", require_identifier(self.tool_call_id, "tool_call_id"))
        object.__setattr__(self, "arguments_digest", require_digest(self.arguments_digest, "arguments_digest"))
        object.__setattr__(
            self,
            "policy_digest",
            require_digest(self.policy_digest, "policy_digest", optional=True),
        )
        object.__setattr__(
            self,
            "permission_request_id",
            require_optional_identifier(self.permission_request_id, "permission_request_id"),
        )
        object.__setattr__(
            self,
            "permission_grant_digest",
            require_digest(self.permission_grant_digest, "permission_grant_digest", optional=True),
        )
        if self.created_at <= 0:
            raise ValueError("created_at must be positive")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def binding_digest(self) -> str:
        return content_digest(
            {
                "invocation_id": self.invocation_id,
                "identity": self.identity.safe_dict(),
                "subject": self.subject.safe_dict(),
                "tool_call_id": self.tool_call_id,
                "arguments_digest": self.arguments_digest,
                "policy_digest": self.policy_digest,
                "permission_request_id": self.permission_request_id,
                "permission_grant_digest": self.permission_grant_digest,
                "idempotency_key": self.idempotency_key,
                "causation_id": self.causation_id,
                "correlation_id": self.correlation_id,
            }
        )

    def with_policy(self, policy_digest: str) -> "GatewayInvocation":
        return replace(self, policy_digest=require_digest(policy_digest, "policy_digest"))

    def with_permission(self, request_id: str, grant_digest: str) -> "GatewayInvocation":
        return replace(
            self,
            permission_request_id=require_identifier(request_id, "permission_request_id"),
            permission_grant_digest=require_digest(grant_digest, "permission_grant_digest"),
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "invocation_id": self.invocation_id,
            "identity": self.identity.safe_dict(),
            "subject": self.subject.safe_dict(),
            "tool_call_id": self.tool_call_id,
            "arguments_digest": self.arguments_digest,
            "policy_digest": self.policy_digest,
            "permission_request_id": self.permission_request_id,
            "permission_grant_digest": self.permission_grant_digest,
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "binding_digest": self.binding_digest,
            "metadata": canonical_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GatewayExecutionReceipt:
    receipt_id: str
    invocation: GatewayInvocation
    outcome: GatewayOutcome
    result_digest: str
    permission_consumption_id: str = ""
    command_receipt_id: str = ""
    patch_receipt_id: str = ""
    artifact_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    owner_epoch_before: int = 0
    owner_epoch_after: int = 0
    backend_generation: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = field(default_factory=time.time)
    failure_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = "zyra.gateway-execution-receipt.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", require_identifier(self.receipt_id, "receipt_id"))
        object.__setattr__(self, "outcome", GatewayOutcome(self.outcome))
        object.__setattr__(self, "result_digest", require_digest(self.result_digest, "result_digest"))
        for field_name in (
            "permission_consumption_id",
            "command_receipt_id",
            "patch_receipt_id",
        ):
            object.__setattr__(
                self,
                field_name,
                require_optional_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "artifact_refs", freeze_strings(self.artifact_refs))
        object.__setattr__(self, "event_refs", freeze_strings(self.event_refs))
        if self.owner_epoch_before < 0 or self.owner_epoch_after < 0:
            raise ValueError("owner epochs cannot be negative")
        if self.owner_epoch_after and self.owner_epoch_after < self.owner_epoch_before:
            raise ValueError("owner epoch cannot move backwards")
        if self.backend_generation < 0:
            raise ValueError("backend_generation cannot be negative")
        if self.started_at <= 0 or self.finished_at < self.started_at:
            raise ValueError("receipt timestamps are invalid")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def receipt_digest(self) -> str:
        return content_digest(self.safe_dict(include_digest=False))

    @property
    def committed(self) -> bool:
        return self.outcome in {GatewayOutcome.ALLOWED, GatewayOutcome.COMMITTED}

    def safe_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "invocation": self.invocation.safe_dict(),
            "outcome": self.outcome.value,
            "result_digest": self.result_digest,
            "permission_consumption_id": self.permission_consumption_id,
            "command_receipt_id": self.command_receipt_id,
            "patch_receipt_id": self.patch_receipt_id,
            "artifact_refs": list(self.artifact_refs),
            "event_refs": list(self.event_refs),
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "backend_generation": self.backend_generation,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_code": self.failure_code,
            "metadata": canonical_value(self.metadata),
        }
        if include_digest:
            payload["receipt_digest"] = content_digest(payload)
        return payload


@dataclass(frozen=True, slots=True)
class GatewayFailureSignal:
    signal_id: str
    identity: WorkerGatewayIdentity
    invocation_id: str
    failure_class: FailureClass
    code: str
    reason: str
    retryable: bool
    recovery_actions: tuple[RecoveryAction, ...]
    attempt: int = 1
    backend_signal: str = ""
    artifact_refs: tuple[str, ...] = ()
    causation_id: str = ""
    occurred_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = "zyra.gateway-failure-signal.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_id", require_identifier(self.signal_id, "signal_id"))
        object.__setattr__(
            self,
            "invocation_id",
            require_optional_identifier(self.invocation_id, "invocation_id"),
        )
        object.__setattr__(self, "failure_class", FailureClass(self.failure_class))
        if not self.code or not self.reason:
            raise ValueError("failure signal code and reason are required")
        object.__setattr__(
            self,
            "recovery_actions",
            tuple(RecoveryAction(item) for item in self.recovery_actions),
        )
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        object.__setattr__(self, "artifact_refs", freeze_strings(self.artifact_refs))
        if self.occurred_at <= 0:
            raise ValueError("occurred_at must be positive")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def signal_digest(self) -> str:
        return content_digest(self.safe_dict(include_digest=False))

    def safe_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "signal_id": self.signal_id,
            "identity": self.identity.safe_dict(),
            "invocation_id": self.invocation_id,
            "failure_class": self.failure_class.value,
            "code": self.code,
            "reason": self.reason,
            "retryable": self.retryable,
            "recovery_actions": [item.value for item in self.recovery_actions],
            "attempt": self.attempt,
            "backend_signal": self.backend_signal,
            "artifact_refs": list(self.artifact_refs),
            "causation_id": self.causation_id,
            "occurred_at": self.occurred_at,
            "metadata": canonical_value(self.metadata),
        }
        if include_digest:
            payload["signal_digest"] = content_digest(payload)
        return payload


@dataclass(frozen=True, slots=True)
class GatewayDispatchReceipt:
    receipt_id: str
    envelope_id: str
    run_id: str
    task_id: str
    worker_id: str
    backend: str
    location: str
    sandbox: str
    gateway: str
    workspace_digest: str
    artifact_root_digest: str
    policy_digest: str
    owner_epoch: int = 0
    backend_generation: int = 0
    expires_at: float = 0.0
    issued_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = "zyra.gateway-dispatch-receipt.v1"

    def __post_init__(self) -> None:
        for field_name in (
            "receipt_id",
            "envelope_id",
            "run_id",
            "task_id",
            "worker_id",
            "backend",
            "location",
            "sandbox",
            "gateway",
        ):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "workspace_digest", require_digest(self.workspace_digest, "workspace_digest"))
        object.__setattr__(self, "artifact_root_digest", require_digest(self.artifact_root_digest, "artifact_root_digest"))
        object.__setattr__(self, "policy_digest", require_digest(self.policy_digest, "policy_digest"))
        if self.owner_epoch < 0 or self.backend_generation < 0:
            raise ValueError("dispatch epochs cannot be negative")
        if self.issued_at <= 0:
            raise ValueError("issued_at must be positive")
        if self.expires_at and self.expires_at <= self.issued_at:
            raise ValueError("dispatch receipt expiry must follow issuance")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def receipt_digest(self) -> str:
        return content_digest(self.safe_dict(include_digest=False))

    def safe_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "envelope_id": self.envelope_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "backend": self.backend,
            "location": self.location,
            "sandbox": self.sandbox,
            "gateway": self.gateway,
            "workspace_digest": self.workspace_digest,
            "artifact_root_digest": self.artifact_root_digest,
            "policy_digest": self.policy_digest,
            "owner_epoch": self.owner_epoch,
            "backend_generation": self.backend_generation,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "metadata": canonical_value(self.metadata),
        }
        if include_digest:
            payload["receipt_digest"] = content_digest(payload)
        return payload

    def assert_fresh(self, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        if self.expires_at and current >= self.expires_at:
            raise ValueError("gateway dispatch receipt expired")


@dataclass(frozen=True, slots=True)
class GatewayControlRequest:
    request_id: str
    action: GatewayAction
    session_id: str
    command_id: str = ""
    reason: str = ""
    expected_generation: int = 0
    expected_owner_epoch: int = 0
    actor_id: str = "runtime"
    causation_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", require_identifier(self.request_id, "request_id"))
        object.__setattr__(self, "action", GatewayAction(self.action))
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        object.__setattr__(
            self,
            "command_id",
            require_optional_identifier(self.command_id, "command_id"),
        )
        object.__setattr__(self, "actor_id", require_identifier(self.actor_id, "actor_id"))
        if self.expected_generation < 0 or self.expected_owner_epoch < 0:
            raise ValueError("control expectations cannot be negative")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def request_digest(self) -> str:
        return content_digest(self.safe_dict())

    def safe_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "action": self.action.value,
            "session_id": self.session_id,
            "command_id": self.command_id,
            "reason": self.reason,
            "expected_generation": self.expected_generation,
            "expected_owner_epoch": self.expected_owner_epoch,
            "actor_id": self.actor_id,
            "causation_id": self.causation_id,
            "created_at": self.created_at,
            "metadata": canonical_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GatewayControlResult:
    request_id: str
    accepted: bool
    outcome: GatewayOutcome
    session_id: str
    command_id: str = ""
    generation: int = 0
    state: str = ""
    failure_code: str = ""
    event_refs: tuple[str, ...] = ()
    completed_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", require_identifier(self.request_id, "request_id"))
        object.__setattr__(self, "outcome", GatewayOutcome(self.outcome))
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        object.__setattr__(
            self,
            "command_id",
            require_optional_identifier(self.command_id, "command_id"),
        )
        if self.generation < 0:
            raise ValueError("generation cannot be negative")
        object.__setattr__(self, "event_refs", freeze_strings(self.event_refs))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "accepted": self.accepted,
            "outcome": self.outcome.value,
            "session_id": self.session_id,
            "command_id": self.command_id,
            "generation": self.generation,
            "state": self.state,
            "failure_code": self.failure_code,
            "event_refs": list(self.event_refs),
            "completed_at": self.completed_at,
            "metadata": canonical_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GatewayMcpExchange:
    exchange_id: str
    identity: WorkerGatewayIdentity
    server_id: str
    tool_name: str
    tool_call_id: str
    request_digest: str
    response_digest: str = ""
    outcome: GatewayOutcome = GatewayOutcome.PENDING
    provenance_ref: str = ""
    artifact_refs: tuple[str, ...] = ()
    redaction_count: int = 0
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = "zyra.gateway-mcp-exchange.v1"

    def __post_init__(self) -> None:
        for field_name in ("exchange_id", "server_id", "tool_name", "tool_call_id"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "request_digest", require_digest(self.request_digest, "request_digest"))
        object.__setattr__(
            self,
            "response_digest",
            require_digest(self.response_digest, "response_digest", optional=True),
        )
        object.__setattr__(self, "outcome", GatewayOutcome(self.outcome))
        object.__setattr__(
            self,
            "provenance_ref",
            require_optional_identifier(self.provenance_ref, "provenance_ref"),
        )
        object.__setattr__(self, "artifact_refs", freeze_strings(self.artifact_refs))
        if self.redaction_count < 0:
            raise ValueError("redaction_count cannot be negative")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def complete(
        self,
        *,
        response: Any,
        outcome: GatewayOutcome,
        provenance_ref: str = "",
        artifact_refs: Sequence[str] = (),
        redaction_count: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> "GatewayMcpExchange":
        return replace(
            self,
            response_digest=content_digest(response),
            outcome=outcome,
            provenance_ref=provenance_ref,
            artifact_refs=tuple(artifact_refs),
            redaction_count=redaction_count,
            completed_at=time.time(),
            metadata={**dict(self.metadata), **dict(metadata or {})},
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "exchange_id": self.exchange_id,
            "identity": self.identity.safe_dict(),
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
            "outcome": self.outcome.value,
            "provenance_ref": self.provenance_ref,
            "artifact_refs": list(self.artifact_refs),
            "redaction_count": self.redaction_count,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "metadata": canonical_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GatewayAuditFinding:
    finding_id: str
    code: str
    severity: str
    path: str
    line: int
    symbol: str
    reason: str
    evidence_digest: str
    blocking: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", require_identifier(self.finding_id, "finding_id"))
        if self.severity not in {"info", "warning", "error", "critical"}:
            raise ValueError("unsupported finding severity")
        if self.line < 0:
            raise ValueError("finding line cannot be negative")
        object.__setattr__(self, "evidence_digest", require_digest(self.evidence_digest, "evidence_digest"))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "reason": self.reason,
            "evidence_digest": self.evidence_digest,
            "blocking": self.blocking,
            "metadata": canonical_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GatewayAuditReport:
    report_id: str
    scope: tuple[str, ...]
    findings: tuple[GatewayAuditFinding, ...]
    scanned_files: int
    scanned_lines: int
    disabled_probe_passed: bool
    external_dependency_count: int = 0
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = "zyra.gateway-audit-report.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_id", require_identifier(self.report_id, "report_id"))
        object.__setattr__(self, "scope", freeze_strings(self.scope))
        object.__setattr__(self, "findings", tuple(self.findings))
        if self.scanned_files < 0 or self.scanned_lines < 0:
            raise ValueError("audit counters cannot be negative")
        if self.external_dependency_count < 0:
            raise ValueError("external_dependency_count cannot be negative")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def blocking_findings(self) -> tuple[GatewayAuditFinding, ...]:
        return tuple(item for item in self.findings if item.blocking)

    @property
    def passed(self) -> bool:
        return not self.blocking_findings and self.disabled_probe_passed

    @property
    def report_digest(self) -> str:
        return content_digest(self.safe_dict(include_digest=False))

    def safe_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "report_id": self.report_id,
            "scope": list(self.scope),
            "findings": [item.safe_dict() for item in self.findings],
            "scanned_files": self.scanned_files,
            "scanned_lines": self.scanned_lines,
            "disabled_probe_passed": self.disabled_probe_passed,
            "external_dependency_count": self.external_dependency_count,
            "passed": self.passed,
            "created_at": self.created_at,
            "metadata": canonical_value(self.metadata),
        }
        if include_digest:
            payload["report_digest"] = content_digest(payload)
        return payload


@dataclass(frozen=True, slots=True)
class GatewayBoundarySnapshot:
    snapshot_id: str
    sessions: tuple[Mapping[str, Any], ...]
    active_commands: tuple[str, ...]
    pending_permissions: tuple[str, ...]
    failure_signals: tuple[GatewayFailureSignal, ...]
    receipt_digests: tuple[str, ...]
    backend_descriptors: tuple[Mapping[str, Any], ...]
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", require_identifier(self.snapshot_id, "snapshot_id"))
        object.__setattr__(
            self,
            "sessions",
            tuple(freeze_mapping(item) for item in self.sessions),
        )
        object.__setattr__(self, "active_commands", freeze_strings(self.active_commands))
        object.__setattr__(self, "pending_permissions", freeze_strings(self.pending_permissions))
        object.__setattr__(self, "failure_signals", tuple(self.failure_signals))
        object.__setattr__(self, "receipt_digests", freeze_strings(self.receipt_digests))
        object.__setattr__(
            self,
            "backend_descriptors",
            tuple(freeze_mapping(item) for item in self.backend_descriptors),
        )
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def snapshot_digest(self) -> str:
        return content_digest(self.safe_dict(include_digest=False))

    def safe_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "snapshot_id": self.snapshot_id,
            "sessions": [canonical_value(item) for item in self.sessions],
            "active_commands": list(self.active_commands),
            "pending_permissions": list(self.pending_permissions),
            "failure_signals": [item.safe_dict() for item in self.failure_signals],
            "receipt_digests": list(self.receipt_digests),
            "backend_descriptors": [canonical_value(item) for item in self.backend_descriptors],
            "created_at": self.created_at,
            "metadata": canonical_value(self.metadata),
        }
        if include_digest:
            payload["snapshot_digest"] = content_digest(payload)
        return payload


def invocation_for(
    identity: WorkerGatewayIdentity,
    *,
    surface: GatewaySurface,
    action: GatewayAction,
    tool_call_id: str,
    logical_name: str,
    arguments: Mapping[str, Any],
    trust: TrustDisposition = TrustDisposition.TRUSTED,
    provenance_ref: str = "",
    policy_digest: str = "",
    idempotency_key: str = "",
    causation_id: str = "",
    correlation_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> GatewayInvocation:
    arguments_digest = content_digest(arguments)
    subject = GatewaySubject(
        surface=surface,
        action=action,
        logical_name=logical_name,
        subject_digest=content_digest(
            {
                "logical_name": logical_name,
                "arguments_digest": arguments_digest,
                "provenance_ref": provenance_ref,
            }
        ),
        trust=trust,
        provenance_ref=provenance_ref,
        metadata=metadata or {},
    )
    return GatewayInvocation(
        invocation_id=stable_identifier(
            "gateway-invocation",
            identity.binding_digest,
            tool_call_id,
            arguments_digest,
        ),
        identity=identity,
        subject=subject,
        tool_call_id=tool_call_id,
        arguments_digest=arguments_digest,
        policy_digest=policy_digest,
        idempotency_key=idempotency_key,
        causation_id=causation_id,
        correlation_id=correlation_id,
        metadata=metadata or {},
    )


__all__ = [
    "FailureClass",
    "GatewayAction",
    "GatewayAuditFinding",
    "GatewayAuditReport",
    "GatewayBoundarySnapshot",
    "GatewayControlRequest",
    "GatewayControlResult",
    "GatewayDispatchReceipt",
    "GatewayExecutionReceipt",
    "GatewayFailureSignal",
    "GatewayInvocation",
    "GatewayMcpExchange",
    "GatewayOutcome",
    "GatewaySubject",
    "GatewaySurface",
    "RecoveryAction",
    "TrustDisposition",
    "WorkerGatewayIdentity",
    "canonical_json",
    "canonical_value",
    "content_digest",
    "freeze_mapping",
    "invocation_for",
    "require_digest",
    "require_identifier",
    "stable_identifier",
]
