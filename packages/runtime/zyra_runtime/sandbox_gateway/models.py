from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .canonical import (
    canonical_logical_path,
    command_digest,
    content_digest,
    digest,
    environment_digest,
    normalize_environment,
    normalize_text,
    stable_id,
)
from .constants import (
    COMMAND_ENVELOPE_SCHEMA,
    COMMAND_RECEIPT_SCHEMA,
    CREDENTIAL_ENVELOPE_SCHEMA,
    DEFAULT_CANCEL_GRACE_SECONDS,
    DEFAULT_COMBINED_OUTPUT_LIMIT_BYTES,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_MAX_PROCESSES,
    DEFAULT_STDERR_LIMIT_BYTES,
    DEFAULT_STDOUT_LIMIT_BYTES,
    EVENT_SCHEMA,
    FILE_ARTIFACT_SCHEMA,
    GATEWAY_SCHEMA_VERSION,
    PATCH_SET_SCHEMA,
    PERMISSION_BINDING_SCHEMA,
    PROVENANCE_SCHEMA,
    STATE_SCHEMA,
)
from .errors import GatewayErrorCode, SandboxGatewayError


class GatewayLifecycleState(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    CLEANING = "cleaning"
    CLOSED = "closed"
    FAILED = "failed"
    ORPHANED = "orphaned"


class CommandEffect(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class CommandRisk(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OperationKind(str, Enum):
    COMMAND = "command"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    FILE_EDIT = "file_edit"
    ARCHIVE_IMPORT = "archive_import"
    ARTIFACT_EXPORT = "artifact_export"
    NETWORK = "network"
    BROWSER_TRANSFER = "browser_transfer"
    CREDENTIAL_USE = "credential_use"
    PATCH_APPLY = "patch_apply"


class ProvenanceKind(str, Enum):
    INTERNAL = "internal"
    WORKSPACE = "workspace"
    GENERATED = "generated"
    USER_UPLOAD = "user_upload"
    DOWNLOAD = "download"
    WEB = "web"
    BROWSER = "browser"
    MCP = "mcp"
    REMOTE_TOOL = "remote_tool"
    NETWORK = "network"
    UNKNOWN = "unknown"


class TrustLevel(str, Enum):
    TRUSTED = "trusted"
    CONSTRAINED = "constrained"
    UNTRUSTED = "untrusted"
    QUARANTINED = "quarantined"


class ProcessTermination(str, Enum):
    EXITED = "exited"
    FAILED_TO_START = "failed_to_start"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    KILLED = "killed"
    TREE_LEAK = "tree_leak"


class PatchOperation(str, Enum):
    CREATE = "create"
    WRITE = "write"
    EDIT = "edit"
    DELETE = "delete"


class GatewayEventKind(str, Enum):
    SESSION_CREATED = "sandbox_session_created"
    SESSION_STATE_CHANGED = "sandbox_session_state_changed"
    SESSION_RECOVERED = "sandbox_session_recovered"
    SESSION_CLOSED = "sandbox_session_closed"
    COMMAND_PROPOSED = "gateway_command_proposed"
    COMMAND_POLICY = "gateway_command_policy"
    COMMAND_PERMISSION = "gateway_command_permission"
    COMMAND_STARTED = "gateway_command_started"
    COMMAND_OUTPUT = "gateway_command_output"
    COMMAND_FINISHED = "gateway_command_finished"
    COMMAND_REJECTED = "gateway_command_rejected"
    PROCESS_CANCELLED = "gateway_process_cancelled"
    ARTIFACT_INSPECTED = "gateway_artifact_inspected"
    ARTIFACT_QUARANTINED = "gateway_artifact_quarantined"
    ARTIFACT_COMMITTED = "gateway_artifact_committed"
    PATCH_PREPARED = "gateway_patch_prepared"
    PATCH_COMMITTED = "gateway_patch_committed"
    PATCH_REJECTED = "gateway_patch_rejected"
    CREDENTIAL_ISSUED = "gateway_credential_issued"
    CREDENTIAL_CONSUMED = "gateway_credential_consumed"
    RECOVERY_INPUT = "recovery_input"


def _now() -> float:
    return time.time()


def _mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _strings(value: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(str(item) for item in (value or ()))


@dataclass(frozen=True, slots=True)
class CommandBudget:
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS
    cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS
    stdout_limit_bytes: int = DEFAULT_STDOUT_LIMIT_BYTES
    stderr_limit_bytes: int = DEFAULT_STDERR_LIMIT_BYTES
    combined_output_limit_bytes: int = DEFAULT_COMBINED_OUTPUT_LIMIT_BYTES
    max_processes: int = DEFAULT_MAX_PROCESSES

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.cancel_grace_seconds < 0:
            raise ValueError("cancel_grace_seconds cannot be negative")
        if min(
            self.stdout_limit_bytes,
            self.stderr_limit_bytes,
            self.combined_output_limit_bytes,
            self.max_processes,
        ) <= 0:
            raise ValueError("output and process limits must be positive")
        if self.combined_output_limit_bytes > self.stdout_limit_bytes + self.stderr_limit_bytes:
            raise ValueError("combined output limit cannot exceed stream limits")

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "cancel_grace_seconds": self.cancel_grace_seconds,
            "stdout_limit_bytes": self.stdout_limit_bytes,
            "stderr_limit_bytes": self.stderr_limit_bytes,
            "combined_output_limit_bytes": self.combined_output_limit_bytes,
            "max_processes": self.max_processes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommandBudget":
        return cls(
            timeout_seconds=float(value.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS)),
            cancel_grace_seconds=float(value.get("cancel_grace_seconds", DEFAULT_CANCEL_GRACE_SECONDS)),
            stdout_limit_bytes=int(value.get("stdout_limit_bytes", DEFAULT_STDOUT_LIMIT_BYTES)),
            stderr_limit_bytes=int(value.get("stderr_limit_bytes", DEFAULT_STDERR_LIMIT_BYTES)),
            combined_output_limit_bytes=int(
                value.get("combined_output_limit_bytes", DEFAULT_COMBINED_OUTPUT_LIMIT_BYTES)
            ),
            max_processes=int(value.get("max_processes", DEFAULT_MAX_PROCESSES)),
        )

    def restrict(self, other: "CommandBudget") -> "CommandBudget":
        return CommandBudget(
            timeout_seconds=min(self.timeout_seconds, other.timeout_seconds),
            cancel_grace_seconds=min(self.cancel_grace_seconds, other.cancel_grace_seconds),
            stdout_limit_bytes=min(self.stdout_limit_bytes, other.stdout_limit_bytes),
            stderr_limit_bytes=min(self.stderr_limit_bytes, other.stderr_limit_bytes),
            combined_output_limit_bytes=min(
                self.combined_output_limit_bytes,
                other.combined_output_limit_bytes,
            ),
            max_processes=min(self.max_processes, other.max_processes),
        )


@dataclass(frozen=True, slots=True)
class GatewayCommandEnvelope:
    command_id: str
    session_id: str
    run_id: str
    task_id: str
    worker_id: str
    executable: str
    argv: tuple[str, ...] = ()
    cwd: str = "."
    environment: Mapping[str, str] = field(default_factory=dict)
    budget: CommandBudget = field(default_factory=CommandBudget)
    operation: OperationKind = OperationKind.COMMAND
    tool_use_id: str = ""
    permission_request_id: str = ""
    workspace_id: str = ""
    owner_epoch: int = 0
    fence_digest: str = ""
    network_profile: str = "offline"
    provenance_ref: str = ""
    idempotency_key: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    created_at: float = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = COMMAND_ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        required = {
            "command_id": self.command_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "executable": self.executable,
        }
        for field_name, value in required.items():
            normalize_text(value, field=field_name, allow_empty=False)
        if self.owner_epoch < 0:
            raise ValueError("owner_epoch cannot be negative")
        if len(self.argv) > 512:
            raise ValueError("argv exceeds gateway limit")
        canonical_logical_path(self.cwd, allow_root=True)

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        worker_id: str,
        executable: str,
        argv: Sequence[Any] = (),
        cwd: str = ".",
        environment: Mapping[str, Any] | None = None,
        budget: CommandBudget | None = None,
        operation: OperationKind | str = OperationKind.COMMAND,
        tool_use_id: str = "",
        permission_request_id: str = "",
        workspace_id: str = "",
        owner_epoch: int = 0,
        fence_digest: str = "",
        network_profile: str = "offline",
        provenance_ref: str = "",
        idempotency_key: str = "",
        causation_id: str = "",
        correlation_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "GatewayCommandEnvelope":
        normalized_argv = tuple(str(item) for item in argv)
        normalized_environment = normalize_environment(environment or {})
        normalized_cwd = canonical_logical_path(cwd, allow_root=True)
        normalized_operation = (
            operation if isinstance(operation, OperationKind) else OperationKind(str(operation))
        )
        command_id = stable_id(
            "gateway-command",
            session_id,
            run_id,
            task_id,
            tool_use_id,
            executable,
            normalized_argv,
            normalized_cwd,
            normalized_environment,
            idempotency_key,
        )
        return cls(
            command_id=command_id,
            session_id=str(session_id),
            run_id=str(run_id),
            task_id=str(task_id),
            worker_id=str(worker_id),
            executable=str(executable),
            argv=normalized_argv,
            cwd=normalized_cwd,
            environment=normalized_environment,
            budget=budget or CommandBudget(),
            operation=normalized_operation,
            tool_use_id=str(tool_use_id),
            permission_request_id=str(permission_request_id),
            workspace_id=str(workspace_id),
            owner_epoch=int(owner_epoch),
            fence_digest=str(fence_digest),
            network_profile=str(network_profile),
            provenance_ref=str(provenance_ref),
            idempotency_key=str(idempotency_key),
            causation_id=str(causation_id),
            correlation_id=str(correlation_id),
            metadata=_mapping(metadata),
        )

    @property
    def environment_digest(self) -> str:
        return environment_digest(self.environment)

    @property
    def identity_digest(self) -> str:
        return command_digest(
            self.executable,
            self.argv,
            self.cwd,
            self.environment_digest,
        )

    def permission_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "command_id": self.command_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "operation": self.operation.value,
            "executable": self.executable,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "environment_digest": self.environment_digest,
            "budget": self.budget.to_dict(),
            "workspace_id": self.workspace_id,
            "owner_epoch": self.owner_epoch,
            "fence_digest": self.fence_digest,
            "network_profile": self.network_profile,
            "provenance_ref": self.provenance_ref,
            "tool_use_id": self.tool_use_id,
        }

    def to_dict(self, *, include_environment: bool = False) -> dict[str, Any]:
        value = {
            **self.permission_material(),
            "permission_request_id": self.permission_request_id,
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "identity_digest": self.identity_digest,
        }
        if include_environment:
            value["environment"] = dict(self.environment)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayCommandEnvelope":
        return cls(
            command_id=str(value["command_id"]),
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            worker_id=str(value["worker_id"]),
            executable=str(value["executable"]),
            argv=_strings(value.get("argv")),
            cwd=str(value.get("cwd", ".")),
            environment={
                str(key): str(item)
                for key, item in dict(value.get("environment") or {}).items()
            },
            budget=CommandBudget.from_dict(dict(value.get("budget") or {})),
            operation=OperationKind(str(value.get("operation", OperationKind.COMMAND.value))),
            tool_use_id=str(value.get("tool_use_id", "")),
            permission_request_id=str(value.get("permission_request_id", "")),
            workspace_id=str(value.get("workspace_id", "")),
            owner_epoch=int(value.get("owner_epoch", 0)),
            fence_digest=str(value.get("fence_digest", "")),
            network_profile=str(value.get("network_profile", "offline")),
            provenance_ref=str(value.get("provenance_ref", "")),
            idempotency_key=str(value.get("idempotency_key", "")),
            causation_id=str(value.get("causation_id", "")),
            correlation_id=str(value.get("correlation_id", "")),
            created_at=float(value.get("created_at", _now())),
            metadata=_mapping(value.get("metadata")),
            schema=str(value.get("schema", COMMAND_ENVELOPE_SCHEMA)),
        )

    def with_permission_request(self, permission_request_id: str) -> "GatewayCommandEnvelope":
        return replace(self, permission_request_id=str(permission_request_id))


@dataclass(frozen=True, slots=True)
class CommandEvidence:
    code: str
    effect: CommandEffect
    reason: str
    risk: CommandRisk = CommandRisk.MEDIUM
    source: str = "zyra"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "effect": self.effect.value,
            "reason": self.reason,
            "risk": self.risk.value,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CommandPolicyDecision:
    effect: CommandEffect
    reason: str
    evidence: tuple[CommandEvidence, ...]
    command_digest: str
    policy_digest: str
    requires_permission_runtime: bool = True
    eligible_for_sealed_auto_allow: bool = False
    recovery: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.effect is CommandEffect.ALLOW

    @property
    def denied(self) -> bool:
        return self.effect is CommandEffect.DENY

    @property
    def requires_approval(self) -> bool:
        return self.effect is CommandEffect.ASK

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect.value,
            "reason": self.reason,
            "evidence": [item.to_dict() for item in self.evidence],
            "command_digest": self.command_digest,
            "policy_digest": self.policy_digest,
            "requires_permission_runtime": self.requires_permission_runtime,
            "eligible_for_sealed_auto_allow": self.eligible_for_sealed_auto_allow,
            "recovery": list(self.recovery),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PermissionBinding:
    binding_id: str
    session_id: str
    command_id: str
    tool_use_id: str
    request_fingerprint: str
    command_digest: str
    grant_digest: str
    effect: CommandEffect
    issued_at: float
    expires_at: float
    consumed_at: float | None = None
    consumption_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = PERMISSION_BINDING_SCHEMA

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def expired(self) -> bool:
        return _now() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "binding_id": self.binding_id,
            "session_id": self.session_id,
            "command_id": self.command_id,
            "tool_use_id": self.tool_use_id,
            "request_fingerprint": self.request_fingerprint,
            "command_digest": self.command_digest,
            "grant_digest": self.grant_digest,
            "effect": self.effect.value,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
            "consumption_id": self.consumption_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PermissionBinding":
        consumed = value.get("consumed_at")
        return cls(
            binding_id=str(value["binding_id"]),
            session_id=str(value["session_id"]),
            command_id=str(value["command_id"]),
            tool_use_id=str(value.get("tool_use_id", "")),
            request_fingerprint=str(value["request_fingerprint"]),
            command_digest=str(value["command_digest"]),
            grant_digest=str(value["grant_digest"]),
            effect=CommandEffect(str(value["effect"])),
            issued_at=float(value["issued_at"]),
            expires_at=float(value["expires_at"]),
            consumed_at=float(consumed) if consumed is not None else None,
            consumption_id=str(value.get("consumption_id", "")),
            metadata=_mapping(value.get("metadata")),
            schema=str(value.get("schema", PERMISSION_BINDING_SCHEMA)),
        )

    def consume(self, consumption_id: str, *, at: float | None = None) -> "PermissionBinding":
        if self.consumed:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_REPLAY,
                "permission binding has already been consumed",
                operation="consume_permission",
            )
        when = _now() if at is None else float(at)
        if when >= self.expires_at:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_EXPIRED,
                "permission binding expired before execution",
                operation="consume_permission",
            )
        return replace(self, consumed_at=when, consumption_id=str(consumption_id))


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    combined_truncated: bool = False

    @property
    def total_bytes(self) -> int:
        return len(self.stdout) + len(self.stderr)

    def text(self, stream: str = "stdout", encoding: str = "utf-8") -> str:
        content = self.stdout if stream == "stdout" else self.stderr
        return content.decode(encoding, errors="replace")

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = {
            "stdout_digest": content_digest(self.stdout),
            "stderr_digest": content_digest(self.stderr),
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "combined_truncated": self.combined_truncated,
        }
        if include_content:
            value["stdout"] = self.stdout.decode("utf-8", errors="replace")
            value["stderr"] = self.stderr.decode("utf-8", errors="replace")
        return value


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command_id: str
    termination: ProcessTermination
    return_code: int | None
    started_at: float
    finished_at: float
    output: ProcessOutput
    process_id: int | None = None
    process_group_id: int | None = None
    backend_id: str = ""
    cancellation_reason: str = ""
    error_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def ok(self) -> bool:
        return self.termination is ProcessTermination.EXITED and self.return_code == 0

    def to_dict(self, *, include_output: bool = False) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "termination": self.termination.value,
            "return_code": self.return_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "output": self.output.to_dict(include_content=include_output),
            "process_id": self.process_id,
            "process_group_id": self.process_group_id,
            "backend_id": self.backend_id,
            "cancellation_reason": self.cancellation_reason,
            "error_code": self.error_code,
            "metadata": dict(self.metadata),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    receipt_id: str
    command_id: str
    session_id: str
    command_digest: str
    policy_digest: str
    permission_binding_id: str
    permission_consumption_id: str
    result: ProcessResult
    workspace_id: str = ""
    owner_epoch_before: int = 0
    owner_epoch_after: int = 0
    patch_receipt_id: str = ""
    artifact_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    created_at: float = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = COMMAND_RECEIPT_SCHEMA

    @property
    def ok(self) -> bool:
        return self.result.ok

    def to_dict(self, *, include_output: bool = False) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "command_id": self.command_id,
            "session_id": self.session_id,
            "command_digest": self.command_digest,
            "policy_digest": self.policy_digest,
            "permission_binding_id": self.permission_binding_id,
            "permission_consumption_id": self.permission_consumption_id,
            "result": self.result.to_dict(include_output=include_output),
            "workspace_id": self.workspace_id,
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "patch_receipt_id": self.patch_receipt_id,
            "artifact_refs": list(self.artifact_refs),
            "event_refs": list(self.event_refs),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    provenance_id: str
    kind: ProvenanceKind
    trust: TrustLevel
    source_id: str
    source_uri_digest: str = ""
    parent_refs: tuple[str, ...] = ()
    content_digest: str = ""
    received_at: float = field(default_factory=_now)
    untrusted_instructions: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = PROVENANCE_SCHEMA

    @classmethod
    def build(
        cls,
        *,
        kind: ProvenanceKind | str,
        trust: TrustLevel | str,
        source_id: str,
        source_uri_digest: str = "",
        parent_refs: Iterable[str] = (),
        content_digest_value: str = "",
        untrusted_instructions: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ArtifactProvenance":
        normalized_kind = kind if isinstance(kind, ProvenanceKind) else ProvenanceKind(str(kind))
        normalized_trust = trust if isinstance(trust, TrustLevel) else TrustLevel(str(trust))
        provenance_id = stable_id(
            "provenance",
            normalized_kind.value,
            normalized_trust.value,
            source_id,
            source_uri_digest,
            tuple(parent_refs),
            content_digest_value,
        )
        return cls(
            provenance_id=provenance_id,
            kind=normalized_kind,
            trust=normalized_trust,
            source_id=str(source_id),
            source_uri_digest=str(source_uri_digest),
            parent_refs=tuple(str(item) for item in parent_refs),
            content_digest=str(content_digest_value),
            untrusted_instructions=bool(untrusted_instructions),
            metadata=_mapping(metadata),
        )

    @property
    def untrusted(self) -> bool:
        return self.trust in {TrustLevel.UNTRUSTED, TrustLevel.QUARANTINED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "provenance_id": self.provenance_id,
            "kind": self.kind.value,
            "trust": self.trust.value,
            "source_id": self.source_id,
            "source_uri_digest": self.source_uri_digest,
            "parent_refs": list(self.parent_refs),
            "content_digest": self.content_digest,
            "received_at": self.received_at,
            "untrusted_instructions": self.untrusted_instructions,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactProvenance":
        return cls(
            provenance_id=str(value["provenance_id"]),
            kind=ProvenanceKind(str(value["kind"])),
            trust=TrustLevel(str(value["trust"])),
            source_id=str(value["source_id"]),
            source_uri_digest=str(value.get("source_uri_digest", "")),
            parent_refs=_strings(value.get("parent_refs")),
            content_digest=str(value.get("content_digest", "")),
            received_at=float(value.get("received_at", _now())),
            untrusted_instructions=bool(value.get("untrusted_instructions", False)),
            metadata=_mapping(value.get("metadata")),
            schema=str(value.get("schema", PROVENANCE_SCHEMA)),
        )


@dataclass(frozen=True, slots=True)
class FileArtifactRequest:
    request_id: str
    session_id: str
    logical_path: str
    content: bytes
    content_type: str
    provenance: ArtifactProvenance
    operation: OperationKind = OperationKind.FILE_WRITE
    expected_digest: str = ""
    expected_previous_digest: str = ""
    mount_kind: str = "task"
    executable_allowed: bool = False
    archive_expansion_allowed: bool = False
    idempotency_key: str = ""
    causation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = FILE_ARTIFACT_SCHEMA

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        logical_path: str,
        content: bytes | str,
        content_type: str,
        provenance: ArtifactProvenance,
        operation: OperationKind | str = OperationKind.FILE_WRITE,
        expected_digest: str = "",
        expected_previous_digest: str = "",
        mount_kind: str = "task",
        executable_allowed: bool = False,
        archive_expansion_allowed: bool = False,
        idempotency_key: str = "",
        causation_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "FileArtifactRequest":
        encoded = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        path = canonical_logical_path(logical_path)
        calculated = content_digest(encoded)
        if expected_digest and expected_digest != calculated:
            raise SandboxGatewayError(
                GatewayErrorCode.CONTENT_MISMATCH,
                "artifact content does not match expected digest",
                operation="build_artifact_request",
            )
        request_id = stable_id(
            "artifact-request",
            session_id,
            path,
            calculated,
            provenance.provenance_id,
            idempotency_key,
        )
        return cls(
            request_id=request_id,
            session_id=str(session_id),
            logical_path=path,
            content=encoded,
            content_type=str(content_type),
            provenance=provenance,
            operation=operation if isinstance(operation, OperationKind) else OperationKind(str(operation)),
            expected_digest=expected_digest or calculated,
            expected_previous_digest=str(expected_previous_digest),
            mount_kind=str(mount_kind),
            executable_allowed=bool(executable_allowed),
            archive_expansion_allowed=bool(archive_expansion_allowed),
            idempotency_key=str(idempotency_key),
            causation_id=str(causation_id),
            metadata=_mapping(metadata),
        )

    @property
    def content_digest(self) -> str:
        return content_digest(self.content)

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "logical_path": self.logical_path,
            "content_digest": self.content_digest,
            "content_bytes": len(self.content),
            "content_type": self.content_type,
            "provenance": self.provenance.to_dict(),
            "operation": self.operation.value,
            "expected_digest": self.expected_digest,
            "expected_previous_digest": self.expected_previous_digest,
            "mount_kind": self.mount_kind,
            "executable_allowed": self.executable_allowed,
            "archive_expansion_allowed": self.archive_expansion_allowed,
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "metadata": dict(self.metadata),
        }
        if include_content:
            value["content_hex"] = self.content.hex()
        return value


@dataclass(frozen=True, slots=True)
class FileArtifactReceipt:
    receipt_id: str
    request_id: str
    session_id: str
    logical_path: str
    content_digest: str
    content_bytes: int
    provenance_id: str
    committed: bool
    quarantined: bool
    quarantine_id: str = ""
    workspace_id: str = ""
    owner_epoch_before: int = 0
    owner_epoch_after: int = 0
    transaction_id: str = ""
    artifact_ref: str = ""
    event_refs: tuple[str, ...] = ()
    reason: str = ""
    created_at: float = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = FILE_ARTIFACT_SCHEMA

    @property
    def ok(self) -> bool:
        return self.committed and not self.quarantined

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "logical_path": self.logical_path,
            "content_digest": self.content_digest,
            "content_bytes": self.content_bytes,
            "provenance_id": self.provenance_id,
            "committed": self.committed,
            "quarantined": self.quarantined,
            "quarantine_id": self.quarantine_id,
            "workspace_id": self.workspace_id,
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "transaction_id": self.transaction_id,
            "artifact_ref": self.artifact_ref,
            "event_refs": list(self.event_refs),
            "reason": self.reason,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class PatchMutation:
    operation: PatchOperation
    logical_path: str
    content: bytes = b""
    expected_previous_digest: str = ""
    content_type: str = "application/octet-stream"
    provenance_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        operation: PatchOperation | str,
        logical_path: str,
        *,
        content: bytes | str = b"",
        expected_previous_digest: str = "",
        content_type: str = "application/octet-stream",
        provenance_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "PatchMutation":
        encoded = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return cls(
            operation=operation if isinstance(operation, PatchOperation) else PatchOperation(str(operation)),
            logical_path=canonical_logical_path(logical_path),
            content=encoded,
            expected_previous_digest=str(expected_previous_digest),
            content_type=str(content_type),
            provenance_id=str(provenance_id),
            metadata=_mapping(metadata),
        )

    @property
    def content_digest(self) -> str:
        return content_digest(self.content)

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = {
            "operation": self.operation.value,
            "logical_path": self.logical_path,
            "content_digest": self.content_digest,
            "content_bytes": len(self.content),
            "expected_previous_digest": self.expected_previous_digest,
            "content_type": self.content_type,
            "provenance_id": self.provenance_id,
            "metadata": dict(self.metadata),
        }
        if include_content:
            value["content_hex"] = self.content.hex()
        return value


@dataclass(frozen=True, slots=True)
class GatewayPatchSet:
    patch_set_id: str
    session_id: str
    mutations: tuple[PatchMutation, ...]
    base_workspace_id: str
    base_owner_epoch: int
    reason: str
    idempotency_key: str
    command_id: str = ""
    provenance_refs: tuple[str, ...] = ()
    created_at: float = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = PATCH_SET_SCHEMA

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        mutations: Sequence[PatchMutation],
        base_workspace_id: str,
        base_owner_epoch: int,
        reason: str,
        idempotency_key: str,
        command_id: str = "",
        provenance_refs: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> "GatewayPatchSet":
        items = tuple(mutations)
        path_keys = [item.logical_path.casefold() for item in items]
        if len(path_keys) != len(set(path_keys)):
            raise SandboxGatewayError(
                GatewayErrorCode.PATCH_REJECTED,
                "patch set contains duplicate logical paths",
                operation="build_patch_set",
            )
        patch_set_id = stable_id(
            "gateway-patch",
            session_id,
            base_workspace_id,
            base_owner_epoch,
            [item.to_dict() for item in items],
            idempotency_key,
        )
        return cls(
            patch_set_id=patch_set_id,
            session_id=str(session_id),
            mutations=items,
            base_workspace_id=str(base_workspace_id),
            base_owner_epoch=int(base_owner_epoch),
            reason=str(reason),
            idempotency_key=str(idempotency_key),
            command_id=str(command_id),
            provenance_refs=tuple(str(item) for item in provenance_refs),
            metadata=_mapping(metadata),
        )

    @property
    def content_bytes(self) -> int:
        return sum(len(item.content) for item in self.mutations)

    @property
    def identity_digest(self) -> str:
        return digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "patch_set_id": self.patch_set_id,
            "session_id": self.session_id,
            "mutations": [item.to_dict() for item in self.mutations],
            "base_workspace_id": self.base_workspace_id,
            "base_owner_epoch": self.base_owner_epoch,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "command_id": self.command_id,
            "provenance_refs": list(self.provenance_refs),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "content_bytes": self.content_bytes,
        }


@dataclass(frozen=True, slots=True)
class PatchReceipt:
    receipt_id: str
    patch_set_id: str
    committed: bool
    workspace_id: str
    owner_epoch_before: int
    owner_epoch_after: int
    transaction_ids: tuple[str, ...] = ()
    rejected_paths: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    reason: str = ""
    created_at: float = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.committed and not self.rejected_paths

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "patch_set_id": self.patch_set_id,
            "committed": self.committed,
            "workspace_id": self.workspace_id,
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "transaction_ids": list(self.transaction_ids),
            "rejected_paths": list(self.rejected_paths),
            "artifact_refs": list(self.artifact_refs),
            "reason": self.reason,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class GatewayLease:
    lease_id: str
    session_id: str
    owner_id: str
    owner_epoch: int
    fence_token_digest: str
    issued_at: float
    expires_at: float
    released_at: float | None = None

    @property
    def active(self) -> bool:
        return self.released_at is None and _now() < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "owner_epoch": self.owner_epoch,
            "fence_token_digest": self.fence_token_digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "released_at": self.released_at,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayLease":
        released = value.get("released_at")
        return cls(
            lease_id=str(value["lease_id"]),
            session_id=str(value["session_id"]),
            owner_id=str(value["owner_id"]),
            owner_epoch=int(value["owner_epoch"]),
            fence_token_digest=str(value["fence_token_digest"]),
            issued_at=float(value["issued_at"]),
            expires_at=float(value["expires_at"]),
            released_at=float(released) if released is not None else None,
        )


@dataclass(frozen=True, slots=True)
class GatewaySessionRecord:
    session_id: str
    run_id: str
    task_id: str
    workspace_id: str
    worker_id: str
    state: GatewayLifecycleState
    owner_epoch: int
    generation: int
    backend_id: str
    created_at: float
    updated_at: float
    ready_at: float | None = None
    closed_at: float | None = None
    active_command_id: str = ""
    lease: GatewayLease | None = None
    failure_code: str = ""
    failure_reason: str = ""
    recovery_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = STATE_SCHEMA

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        workspace_id: str,
        worker_id: str,
        backend_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> "GatewaySessionRecord":
        now = _now()
        return cls(
            session_id=str(session_id),
            run_id=str(run_id),
            task_id=str(task_id),
            workspace_id=str(workspace_id),
            worker_id=str(worker_id),
            state=GatewayLifecycleState.CREATED,
            owner_epoch=0,
            generation=1,
            backend_id=str(backend_id),
            created_at=now,
            updated_at=now,
            metadata=_mapping(metadata),
        )

    @property
    def terminal(self) -> bool:
        return self.state in {GatewayLifecycleState.CLOSED, GatewayLifecycleState.FAILED}

    def transition(
        self,
        state: GatewayLifecycleState,
        *,
        active_command_id: str | None = None,
        failure_code: str | None = None,
        failure_reason: str | None = None,
        lease: GatewayLease | None | object = ...,
        metadata: Mapping[str, Any] | None = None,
    ) -> "GatewaySessionRecord":
        now = _now()
        ready_at = self.ready_at
        closed_at = self.closed_at
        if state is GatewayLifecycleState.READY and ready_at is None:
            ready_at = now
        if state is GatewayLifecycleState.CLOSED:
            closed_at = now
        next_lease = self.lease if lease is ... else lease
        return replace(
            self,
            state=state,
            generation=self.generation + 1,
            updated_at=now,
            ready_at=ready_at,
            closed_at=closed_at,
            active_command_id=(
                self.active_command_id if active_command_id is None else active_command_id
            ),
            failure_code=self.failure_code if failure_code is None else failure_code,
            failure_reason=self.failure_reason if failure_reason is None else failure_reason,
            lease=next_lease,  # type: ignore[arg-type]
            metadata={**dict(self.metadata), **dict(metadata or {})},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "worker_id": self.worker_id,
            "state": self.state.value,
            "owner_epoch": self.owner_epoch,
            "generation": self.generation,
            "backend_id": self.backend_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ready_at": self.ready_at,
            "closed_at": self.closed_at,
            "active_command_id": self.active_command_id,
            "lease": self.lease.to_dict() if self.lease else None,
            "failure_code": self.failure_code,
            "failure_reason": self.failure_reason,
            "recovery_count": self.recovery_count,
            "metadata": dict(self.metadata),
            "runtime_schema": GATEWAY_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewaySessionRecord":
        lease = value.get("lease")
        return cls(
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            workspace_id=str(value.get("workspace_id", "")),
            worker_id=str(value["worker_id"]),
            state=GatewayLifecycleState(str(value["state"])),
            owner_epoch=int(value.get("owner_epoch", 0)),
            generation=int(value.get("generation", 1)),
            backend_id=str(value.get("backend_id", "")),
            created_at=float(value["created_at"]),
            updated_at=float(value["updated_at"]),
            ready_at=float(value["ready_at"]) if value.get("ready_at") is not None else None,
            closed_at=float(value["closed_at"]) if value.get("closed_at") is not None else None,
            active_command_id=str(value.get("active_command_id", "")),
            lease=GatewayLease.from_dict(lease) if isinstance(lease, Mapping) else None,
            failure_code=str(value.get("failure_code", "")),
            failure_reason=str(value.get("failure_reason", "")),
            recovery_count=int(value.get("recovery_count", 0)),
            metadata=_mapping(value.get("metadata")),
            schema=str(value.get("schema", STATE_SCHEMA)),
        )


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    transition_id: str
    session_id: str
    from_state: GatewayLifecycleState
    to_state: GatewayLifecycleState
    generation_before: int
    generation_after: int
    reason: str
    occurred_at: float = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "session_id": self.session_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GatewayEvent:
    event_id: str
    kind: GatewayEventKind
    run_id: str
    task_id: str
    session_id: str
    worker_id: str
    sequence: int
    payload: Mapping[str, Any]
    causation_id: str = ""
    correlation_id: str = ""
    occurred_at: float = field(default_factory=_now)
    schema: str = EVENT_SCHEMA

    @classmethod
    def build(
        cls,
        *,
        kind: GatewayEventKind | str,
        run_id: str,
        task_id: str,
        session_id: str,
        worker_id: str,
        sequence: int,
        payload: Mapping[str, Any],
        causation_id: str = "",
        correlation_id: str = "",
    ) -> "GatewayEvent":
        normalized_kind = kind if isinstance(kind, GatewayEventKind) else GatewayEventKind(str(kind))
        event_id = stable_id(
            "gateway-event",
            session_id,
            sequence,
            normalized_kind.value,
            causation_id,
            payload,
        )
        return cls(
            event_id=event_id,
            kind=normalized_kind,
            run_id=str(run_id),
            task_id=str(task_id),
            session_id=str(session_id),
            worker_id=str(worker_id),
            sequence=int(sequence),
            payload=dict(payload),
            causation_id=str(causation_id),
            correlation_id=str(correlation_id),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_id": self.worker_id,
            "sequence": self.sequence,
            "payload": dict(self.payload),
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True, slots=True)
class CredentialRequest:
    request_id: str
    session_id: str
    command_id: str
    provider: str
    credential_name: str
    audience: str
    scope: tuple[str, ...]
    ttl_seconds: float
    provenance_ref: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        command_id: str,
        provider: str,
        credential_name: str,
        audience: str,
        scope: Iterable[str],
        ttl_seconds: float,
        provenance_ref: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "CredentialRequest":
        scopes = tuple(sorted({str(item) for item in scope if str(item)}))
        if ttl_seconds <= 0:
            raise ValueError("credential ttl must be positive")
        request_id = stable_id(
            "credential-request",
            session_id,
            command_id,
            provider,
            credential_name,
            audience,
            scopes,
        )
        return cls(
            request_id=request_id,
            session_id=str(session_id),
            command_id=str(command_id),
            provider=str(provider),
            credential_name=str(credential_name),
            audience=str(audience),
            scope=scopes,
            ttl_seconds=float(ttl_seconds),
            provenance_ref=str(provenance_ref),
            metadata=_mapping(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "command_id": self.command_id,
            "provider": self.provider,
            "credential_name": self.credential_name,
            "audience": self.audience,
            "scope": list(self.scope),
            "ttl_seconds": self.ttl_seconds,
            "provenance_ref": self.provenance_ref,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    envelope_id: str
    request_id: str
    session_id: str
    command_id: str
    audience: str
    scope: tuple[str, ...]
    secret_digest: str
    issued_at: float
    expires_at: float
    nonce: str
    consumed_at: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = CREDENTIAL_ENVELOPE_SCHEMA

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def expired(self) -> bool:
        return _now() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "envelope_id": self.envelope_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "command_id": self.command_id,
            "audience": self.audience,
            "scope": list(self.scope),
            "secret_digest": self.secret_digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "consumed_at": self.consumed_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    quarantine_id: str
    session_id: str
    request_id: str
    content_digest: str
    logical_path: str
    reason_codes: tuple[str, ...]
    released: bool = False
    release_reason: str = ""
    created_at: float = field(default_factory=_now)
    released_at: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "quarantine_id": self.quarantine_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "content_digest": self.content_digest,
            "logical_path": self.logical_path,
            "reason_codes": list(self.reason_codes),
            "released": self.released,
            "release_reason": self.release_reason,
            "created_at": self.created_at,
            "released_at": self.released_at,
            "metadata": dict(self.metadata),
        }
