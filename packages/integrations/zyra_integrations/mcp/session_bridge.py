from __future__ import annotations

"""MCP session snapshot bridge for the CodeWorker session store.

This module deliberately owns no persistence.  It validates and reconciles
``McpClientRuntime.session_snapshot`` values, then delegates checkpoint and
load operations to the existing ``CodeWorkerSessionStore`` contract.  The
only key it owns inside a CodeWorker runtime-state checkpoint is
``runtime_state['mcp_runtime']``.

The bridge is intentionally expressed through Protocols.  Importing the MCP
runtime here would create a runtime -> bridge -> runtime cycle, while
importing the concrete CodeWorker store would make the integration package
the owner of a second session implementation.  Structural ports preserve the
single-owner boundary and make causality testable.
"""

import copy
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, TypeAlias, runtime_checkable


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

MCP_SESSION_SCHEMA = "zyra.mcp-session-state.v1"
MCP_INSTRUCTIONS_SCHEMA = "zyra.mcp-instructions-snapshot.v1"
MCP_BRIDGE_SCHEMA = "zyra.mcp-session-bridge.v1"
MCP_CAUSAL_RECEIPT_SCHEMA = "zyra.mcp-checkpoint-causality.v1"
MCP_DIFF_SCHEMA = "zyra.mcp-session-diff.v1"
MCP_MERGE_SCHEMA = "zyra.mcp-session-merge.v1"
MCP_RUNTIME_STATE_KEY = "mcp_runtime"
REDACTED = "<redacted>"


class McpSessionBridgeError(RuntimeError):
    """Base failure for bridge validation, merge, checkpoint, or restore."""


class McpSnapshotValidationError(McpSessionBridgeError):
    """Raised when a runtime snapshot violates the bridge contract."""


class McpSnapshotIdentityError(McpSnapshotValidationError):
    """Raised when session/run/task ownership cannot be proven."""


class McpSnapshotMergeError(McpSessionBridgeError):
    """Raised when two snapshots cannot be reconciled deterministically."""


class McpCheckpointError(McpSessionBridgeError):
    """Raised when the existing CodeWorker session store rejects a write."""


class McpRestoreError(McpSessionBridgeError):
    """Raised when a checkpoint cannot be safely restored into a runtime."""


class McpCausalityError(McpSessionBridgeError):
    """Raised when checkpoint ancestry or digests do not line up."""


class SnapshotIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SnapshotChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    REPLACED = "replaced"
    TYPE_CHANGED = "type_changed"
    GENERATION_ADVANCED = "generation_advanced"
    GENERATION_REGRESSED = "generation_regressed"
    STATUS_CHANGED = "status_changed"


class MergeDisposition(StrEnum):
    IDENTICAL = "identical"
    INCOMING_ONLY = "incoming_only"
    CURRENT_ONLY = "current_only"
    MERGED = "merged"
    CONFLICT = "conflict"


class CheckpointDisposition(StrEnum):
    APPENDED = "appended"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"


class RestoreDisposition(StrEnum):
    RESTORED = "restored"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"


class MergePreference(StrEnum):
    HIGHEST_GENERATION = "highest_generation"
    INCOMING = "incoming"
    CURRENT = "current"
    FAIL = "fail"


@runtime_checkable
class McpSnapshotRuntimePort(Protocol):
    """Small runtime surface needed by the session bridge."""

    def session_snapshot(self, session_id: str) -> Mapping[str, Any]: ...

    def restore_session_snapshot(self, snapshot: Mapping[str, Any] | None) -> int: ...


@runtime_checkable
class CodeWorkerRuntimeStateLoadPort(Protocol):
    ok: bool
    found: bool
    session_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    runtime_state: Mapping[str, Any]
    causal_receipt: Mapping[str, Any]
    sequence: int
    error: str
    metadata: Mapping[str, Any]


@runtime_checkable
class CodeWorkerSessionStoreReceiptPort(Protocol):
    ok: bool
    session_id: str
    worker_request_id: str
    error: str

    @property
    def last_sequence(self) -> int: ...


@runtime_checkable
class CodeWorkerSessionStorePort(Protocol):
    """Existing session owner; the bridge never implements this Protocol."""

    def load_runtime_state(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
    ) -> CodeWorkerRuntimeStateLoadPort: ...

    def append_runtime_state(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        run_id: str,
        task_id: str,
        runtime_state: Mapping[str, Any],
        causal_receipt: Mapping[str, Any] | None = None,
        expected_previous_sequence: int | None = None,
        disabled: bool = False,
    ) -> CodeWorkerSessionStoreReceiptPort: ...


@dataclass(frozen=True, slots=True)
class McpSessionIdentity:
    session_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    node_id: str = ""

    def __post_init__(self) -> None:
        for name in ("session_id", "run_id", "task_id", "worker_request_id"):
            if not str(getattr(self, name)).strip():
                raise McpSnapshotIdentityError(f"{name} is required")

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "node_id": self.node_id,
        }

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class McpSnapshotIssue:
    code: str
    path: str
    severity: SnapshotIssueSeverity
    message: str
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "path": self.path,
            "severity": self.severity.value,
            "message": self.message,
            "metadata": normalize_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class McpSnapshotValidation:
    ok: bool
    schema: str
    session_id: str
    snapshot_digest: str
    issues: tuple[McpSnapshotIssue, ...]
    server_ids: tuple[str, ...]
    connected_server_ids: tuple[str, ...]
    catalog_server_ids: tuple[str, ...]
    instruction_server_ids: tuple[str, ...]
    state_revision: int

    @property
    def errors(self) -> tuple[McpSnapshotIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is SnapshotIssueSeverity.ERROR)

    @property
    def warnings(self) -> tuple[McpSnapshotIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is SnapshotIssueSeverity.WARNING)

    def require_valid(self) -> "McpSnapshotValidation":
        if not self.ok:
            codes = ", ".join(sorted({issue.code for issue in self.errors}))
            raise McpSnapshotValidationError(f"invalid MCP session snapshot: {codes}")
        return self

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "ok": self.ok,
            "schema": self.schema,
            "session_id": self.session_id,
            "snapshot_digest": self.snapshot_digest,
            "issues": [issue.to_dict() for issue in self.issues],
            "server_ids": list(self.server_ids),
            "connected_server_ids": list(self.connected_server_ids),
            "catalog_server_ids": list(self.catalog_server_ids),
            "instruction_server_ids": list(self.instruction_server_ids),
            "state_revision": self.state_revision,
        }


@dataclass(frozen=True, slots=True)
class McpSnapshotEnvelope:
    identity: McpSessionIdentity
    snapshot: Mapping[str, JsonValue]
    validation: McpSnapshotValidation
    captured_at: str
    source: str

    def __post_init__(self) -> None:
        if self.snapshot.get("session_id") != self.identity.session_id:
            raise McpSnapshotIdentityError("snapshot session does not match envelope identity")
        if self.validation.snapshot_digest != stable_digest(self.snapshot):
            raise McpSnapshotValidationError("envelope digest does not match snapshot")

    @property
    def digest(self) -> str:
        return self.validation.snapshot_digest

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": MCP_BRIDGE_SCHEMA,
            "identity": self.identity.to_dict(),
            "snapshot": normalize_json(self.snapshot),
            "validation": self.validation.to_dict(),
            "captured_at": self.captured_at,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class McpSnapshotChange:
    change_id: str
    path: str
    kind: SnapshotChangeKind
    before_digest: str
    after_digest: str
    before_preview: JsonValue = None
    after_preview: JsonValue = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "change_id": self.change_id,
            "path": self.path,
            "kind": self.kind.value,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "before_preview": self.before_preview,
            "after_preview": self.after_preview,
        }


@dataclass(frozen=True, slots=True)
class McpSnapshotDiff:
    before_digest: str
    after_digest: str
    changes: tuple[McpSnapshotChange, ...]
    server_ids_added: tuple[str, ...] = ()
    server_ids_removed: tuple[str, ...] = ()
    connection_state_changes: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    catalog_generation_changes: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    instruction_generation_changes: Mapping[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": MCP_DIFF_SCHEMA,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "changed": self.changed,
            "changes": [change.to_dict() for change in self.changes],
            "server_ids_added": list(self.server_ids_added),
            "server_ids_removed": list(self.server_ids_removed),
            "connection_state_changes": {
                server_id: list(values)
                for server_id, values in sorted(self.connection_state_changes.items())
            },
            "catalog_generation_changes": {
                server_id: list(values)
                for server_id, values in sorted(self.catalog_generation_changes.items())
            },
            "instruction_generation_changes": {
                server_id: list(values)
                for server_id, values in sorted(self.instruction_generation_changes.items())
            },
        }


@dataclass(frozen=True, slots=True)
class McpMergeConflict:
    path: str
    code: str
    current_digest: str
    incoming_digest: str
    resolution: str
    blocking: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "code": self.code,
            "current_digest": self.current_digest,
            "incoming_digest": self.incoming_digest,
            "resolution": self.resolution,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class McpMergeReceipt:
    disposition: MergeDisposition
    snapshot: Mapping[str, JsonValue]
    current_digest: str
    incoming_digest: str
    merged_digest: str
    conflicts: tuple[McpMergeConflict, ...]
    diff_from_current: McpSnapshotDiff
    selected_connection_sources: Mapping[str, str]
    selected_catalog_sources: Mapping[str, str]
    selected_instruction_sources: Mapping[str, str]

    @property
    def ok(self) -> bool:
        return not any(conflict.blocking for conflict in self.conflicts)

    def require_merged(self) -> "McpMergeReceipt":
        if not self.ok:
            paths = ", ".join(conflict.path for conflict in self.conflicts if conflict.blocking)
            raise McpSnapshotMergeError(f"blocking MCP snapshot merge conflicts: {paths}")
        return self

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": MCP_MERGE_SCHEMA,
            "ok": self.ok,
            "disposition": self.disposition.value,
            "snapshot": normalize_json(self.snapshot),
            "current_digest": self.current_digest,
            "incoming_digest": self.incoming_digest,
            "merged_digest": self.merged_digest,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "diff_from_current": self.diff_from_current.to_dict(),
            "selected_connection_sources": dict(sorted(self.selected_connection_sources.items())),
            "selected_catalog_sources": dict(sorted(self.selected_catalog_sources.items())),
            "selected_instruction_sources": dict(sorted(self.selected_instruction_sources.items())),
        }


@dataclass(frozen=True, slots=True)
class McpCheckpointCausality:
    checkpoint_id: str
    identity_digest: str
    parent_sequence: int
    parent_snapshot_digest: str
    snapshot_digest: str
    state_revision: int
    change_ids: tuple[str, ...]
    runtime_state_digest: str
    created_at: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": MCP_CAUSAL_RECEIPT_SCHEMA,
            "checkpoint_id": self.checkpoint_id,
            "identity_digest": self.identity_digest,
            "parent_sequence": self.parent_sequence,
            "parent_snapshot_digest": self.parent_snapshot_digest,
            "snapshot_digest": self.snapshot_digest,
            "state_revision": self.state_revision,
            "change_ids": list(self.change_ids),
            "runtime_state_digest": self.runtime_state_digest,
            "created_at": self.created_at,
            "core_digest": self.core_digest,
        }

    @property
    def core_digest(self) -> str:
        return stable_digest(
            {
                "checkpoint_id": self.checkpoint_id,
                "identity_digest": self.identity_digest,
                "parent_sequence": self.parent_sequence,
                "parent_snapshot_digest": self.parent_snapshot_digest,
                "snapshot_digest": self.snapshot_digest,
                "state_revision": self.state_revision,
                "change_ids": list(self.change_ids),
                "runtime_state_digest": self.runtime_state_digest,
                "created_at": self.created_at,
            }
        )


@dataclass(frozen=True, slots=True)
class McpCheckpointReceipt:
    ok: bool
    disposition: CheckpointDisposition
    identity: McpSessionIdentity
    snapshot_digest: str
    runtime_state_digest: str
    previous_sequence: int
    checkpoint_sequence: int
    causality: McpCheckpointCausality
    validation: McpSnapshotValidation
    diff: McpSnapshotDiff
    merge: McpMergeReceipt | None = None
    error: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "ok": self.ok,
            "disposition": self.disposition.value,
            "identity": self.identity.to_dict(),
            "snapshot_digest": self.snapshot_digest,
            "runtime_state_digest": self.runtime_state_digest,
            "previous_sequence": self.previous_sequence,
            "checkpoint_sequence": self.checkpoint_sequence,
            "causality": self.causality.to_dict(),
            "validation": self.validation.to_dict(),
            "diff": self.diff.to_dict(),
            "merge": self.merge.to_dict() if self.merge else None,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class McpRestoreReceipt:
    ok: bool
    disposition: RestoreDisposition
    identity: McpSessionIdentity
    sequence: int
    snapshot_digest: str
    restored_instruction_count: int
    validation: McpSnapshotValidation | None
    causality_verified: bool
    causal_receipt: Mapping[str, JsonValue]
    error: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "ok": self.ok,
            "disposition": self.disposition.value,
            "identity": self.identity.to_dict(),
            "sequence": self.sequence,
            "snapshot_digest": self.snapshot_digest,
            "restored_instruction_count": self.restored_instruction_count,
            "validation": self.validation.to_dict() if self.validation else None,
            "causality_verified": self.causality_verified,
            "causal_receipt": normalize_json(self.causal_receipt),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class McpSessionBridgePolicy:
    strict_schema: bool = True
    require_no_credentials: bool = True
    require_no_live_transports: bool = True
    require_catalog_connection_alignment: bool = True
    reject_generation_regression: bool = True
    preserve_terminal_task_state: bool = True
    merge_preference: MergePreference = MergePreference.HIGHEST_GENERATION
    max_snapshot_bytes: int = 2_000_000
    max_connections: int = 256
    max_catalog_servers: int = 256
    max_instruction_states: int = 256
    max_pending_elicitations: int = 1_024
    max_depth: int = 32
    write_unchanged_checkpoint: bool = False
    compare_and_append: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_snapshot_bytes",
            "max_connections",
            "max_catalog_servers",
            "max_instruction_states",
            "max_pending_elicitations",
            "max_depth",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")


class McpSnapshotValidator:
    def __init__(self, policy: McpSessionBridgePolicy | None = None) -> None:
        self.policy = policy or McpSessionBridgePolicy()

    def validate(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_session_id: str = "",
    ) -> McpSnapshotValidation:
        issues: list[McpSnapshotIssue] = []
        normalized = normalize_json(snapshot, max_depth=self.policy.max_depth)
        if not isinstance(normalized, dict):
            raise McpSnapshotValidationError("MCP snapshot must normalize to an object")
        encoded = canonical_json_bytes(normalized)
        if len(encoded) > self.policy.max_snapshot_bytes:
            issues.append(
                self._error(
                    "snapshot_too_large",
                    "$",
                    "snapshot exceeds bridge size limit",
                    {"bytes": len(encoded), "limit": self.policy.max_snapshot_bytes},
                )
            )
        schema = str(normalized.get("schema") or "")
        if schema != MCP_SESSION_SCHEMA:
            severity = SnapshotIssueSeverity.ERROR if self.policy.strict_schema else SnapshotIssueSeverity.WARNING
            issues.append(
                McpSnapshotIssue(
                    "unsupported_schema",
                    "$.schema",
                    severity,
                    f"expected {MCP_SESSION_SCHEMA}",
                    {"actual": schema},
                )
            )
        session_id = str(normalized.get("session_id") or "")
        if not session_id:
            issues.append(self._error("session_id_missing", "$.session_id", "session_id is required"))
        if expected_session_id and session_id != expected_session_id:
            issues.append(
                self._error(
                    "session_id_mismatch",
                    "$.session_id",
                    "snapshot belongs to another session",
                    {"expected": expected_session_id, "actual": session_id},
                )
            )
        state_revision = _non_negative_int(normalized.get("state_revision"), default=-1)
        if state_revision < 0:
            issues.append(
                self._error(
                    "state_revision_invalid",
                    "$.state_revision",
                    "state_revision must be a non-negative integer",
                )
            )

        connections_raw = normalized.get("connections")
        connections = _mapping_sequence(connections_raw)
        if connections_raw is not None and not isinstance(connections_raw, list):
            issues.append(self._error("connections_not_list", "$.connections", "connections must be a list"))
        if len(connections) > self.policy.max_connections:
            issues.append(self._error("connections_limit", "$.connections", "too many connection snapshots"))
        connection_ids: list[str] = []
        connected_ids: list[str] = []
        connection_generations: dict[str, int] = {}
        for index, connection in enumerate(connections):
            path = f"$.connections[{index}]"
            server_id = str(connection.get("server_id") or "")
            if not server_id:
                issues.append(self._error("connection_server_missing", path, "connection server_id is required"))
                continue
            if server_id in connection_ids:
                issues.append(self._error("connection_duplicate", path, "duplicate connection server_id"))
            connection_ids.append(server_id)
            state = _state_value(connection.get("state"))
            if not state:
                issues.append(self._error("connection_state_missing", f"{path}.state", "state is required"))
            if state == "connected":
                connected_ids.append(server_id)
            generation = _non_negative_int(connection.get("generation"))
            connection_generations[server_id] = generation
            revision = _non_negative_int(connection.get("revision"))
            if generation <= 0 and state == "connected":
                issues.append(
                    self._warning(
                        "connected_generation_zero",
                        f"{path}.generation",
                        "connected server has no positive generation",
                    )
                )
            if revision <= 0:
                issues.append(self._warning("connection_revision_zero", f"{path}.revision", "revision is zero"))
            self._check_sensitive(connection, path, issues)

        catalog = _as_mapping(normalized.get("catalog"))
        catalog_servers = _catalog_server_map(catalog)
        if len(catalog_servers) > self.policy.max_catalog_servers:
            issues.append(self._error("catalog_limit", "$.catalog", "too many catalog snapshots"))
        for server_id, value in sorted(catalog_servers.items()):
            path = f"$.catalog.servers.{escape_pointer(server_id)}"
            if self.policy.require_catalog_connection_alignment and server_id not in connected_ids:
                issues.append(
                    self._error(
                        "catalog_without_connected_server",
                        path,
                        "active catalog must belong to a connected server",
                    )
                )
            connection_generation = _non_negative_int(value.get("connection_generation"))
            known_generation = connection_generations.get(server_id, 0)
            if known_generation and connection_generation and connection_generation != known_generation:
                issues.append(
                    self._error(
                        "catalog_connection_generation_mismatch",
                        f"{path}.connection_generation",
                        "catalog generation does not match connection generation",
                        {"connection": known_generation, "catalog": connection_generation},
                    )
                )
            tools = _mapping_sequence(value.get("tools"))
            local_names = [str(tool.get("local_name") or "") for tool in tools]
            if len([name for name in local_names if name]) != len(set(name for name in local_names if name)):
                issues.append(self._error("catalog_tool_collision", f"{path}.tools", "local tool names collide"))

        instructions = _as_mapping(normalized.get("instructions"))
        instructions_schema = str(instructions.get("schema") or "")
        if instructions and instructions_schema != MCP_INSTRUCTIONS_SCHEMA:
            issues.append(
                self._error(
                    "instructions_schema_invalid",
                    "$.instructions.schema",
                    "unsupported instructions snapshot schema",
                )
            )
        instruction_session = str(instructions.get("session_id") or "")
        if instructions and instruction_session != session_id:
            issues.append(
                self._error(
                    "instructions_session_mismatch",
                    "$.instructions.session_id",
                    "instructions belong to another session",
                )
            )
        active_instructions = _mapping_sequence(instructions.get("active"))
        if len(active_instructions) > self.policy.max_instruction_states:
            issues.append(self._error("instructions_limit", "$.instructions.active", "too many instructions states"))
        instruction_ids: list[str] = []
        for index, value in enumerate(active_instructions):
            path = f"$.instructions.active[{index}]"
            server_id = str(value.get("server_id") or "")
            if not server_id:
                issues.append(self._error("instruction_server_missing", path, "instruction server_id is required"))
                continue
            if server_id in instruction_ids:
                issues.append(self._error("instruction_duplicate", path, "duplicate active instruction server"))
            instruction_ids.append(server_id)
            generation = _non_negative_int(value.get("connection_generation"))
            if server_id in connection_generations and generation > connection_generations[server_id]:
                issues.append(
                    self._error(
                        "instruction_generation_ahead",
                        f"{path}.connection_generation",
                        "instructions cannot be newer than their connection",
                    )
                )
            text = str(value.get("instructions") or "")
            digest = str(value.get("instructions_hash") or "")
            if text and digest and digest != stable_digest(text):
                issues.append(
                    self._error(
                        "instruction_digest_mismatch",
                        f"{path}.instructions_hash",
                        "instruction text digest does not match",
                    )
                )

        pending = normalized.get("pending_elicitations")
        pending_values = _mapping_sequence(pending)
        if pending is not None and not isinstance(pending, list):
            issues.append(self._error("elicitations_not_list", "$.pending_elicitations", "must be a list"))
        if len(pending_values) > self.policy.max_pending_elicitations:
            issues.append(self._error("elicitations_limit", "$.pending_elicitations", "too many pending requests"))
        request_ids: set[str] = set()
        for index, value in enumerate(pending_values):
            request_id = str(value.get("request_id") or "")
            if not request_id:
                issues.append(
                    self._error(
                        "elicitation_request_missing",
                        f"$.pending_elicitations[{index}]",
                        "request_id is required",
                    )
                )
            elif request_id in request_ids:
                issues.append(
                    self._error(
                        "elicitation_duplicate",
                        f"$.pending_elicitations[{index}]",
                        "duplicate pending request_id",
                    )
                )
            request_ids.add(request_id)
            self._check_sensitive(value, f"$.pending_elicitations[{index}]", issues)

        if self.policy.require_no_credentials and normalized.get("credentials_included") is not False:
            issues.append(
                self._error(
                    "credentials_included",
                    "$.credentials_included",
                    "session checkpoint must not contain credentials",
                )
            )
        if self.policy.require_no_live_transports and normalized.get("live_transports_included") is not False:
            issues.append(
                self._error(
                    "live_transports_included",
                    "$.live_transports_included",
                    "live transport handles are not restorable",
                )
            )
        self._check_sensitive(_as_mapping(normalized.get("auth")), "$.auth", issues)
        errors = [issue for issue in issues if issue.severity is SnapshotIssueSeverity.ERROR]
        return McpSnapshotValidation(
            ok=not errors,
            schema=schema,
            session_id=session_id,
            snapshot_digest=stable_digest(normalized),
            issues=tuple(issues),
            server_ids=tuple(sorted(set(connection_ids) | set(catalog_servers) | set(instruction_ids))),
            connected_server_ids=tuple(sorted(set(connected_ids))),
            catalog_server_ids=tuple(sorted(catalog_servers)),
            instruction_server_ids=tuple(sorted(set(instruction_ids))),
            state_revision=max(state_revision, 0),
        )

    @staticmethod
    def _error(
        code: str,
        path: str,
        message: str,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> McpSnapshotIssue:
        return McpSnapshotIssue(code, path, SnapshotIssueSeverity.ERROR, message, metadata or {})

    @staticmethod
    def _warning(code: str, path: str, message: str) -> McpSnapshotIssue:
        return McpSnapshotIssue(code, path, SnapshotIssueSeverity.WARNING, message)

    def _check_sensitive(
        self,
        value: Mapping[str, Any],
        path: str,
        issues: list[McpSnapshotIssue],
    ) -> None:
        for pointer, leaf in walk_leaves(value, path=path, max_depth=self.policy.max_depth):
            key = pointer.rsplit(".", 1)[-1].casefold()
            if not any(marker in key for marker in ("token", "secret", "password", "authorization", "cookie")):
                continue
            text = str(leaf or "")
            if not text or text in {REDACTED, "false", "true", "none"}:
                continue
            if key.endswith(("_included", "_present", "_configured", "_expires_at", "_revision")):
                continue
            issues.append(
                self._error(
                    "sensitive_value_present",
                    pointer,
                    "checkpoint contains a value at a sensitive key",
                )
            )


class McpSnapshotDiffer:
    def diff(self, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> McpSnapshotDiff:
        left = normalize_json(before or {})
        right = normalize_json(after or {})
        changes: list[McpSnapshotChange] = []
        self._walk(left, right, "$", changes)
        before_connections = _connection_map(_as_mapping(left))
        after_connections = _connection_map(_as_mapping(right))
        before_servers = set(before_connections) | set(_catalog_server_map(_as_mapping(_as_mapping(left).get("catalog"))))
        after_servers = set(after_connections) | set(_catalog_server_map(_as_mapping(_as_mapping(right).get("catalog"))))
        state_changes: dict[str, tuple[str, str]] = {}
        for server_id in sorted(set(before_connections) & set(after_connections)):
            old = _state_value(before_connections[server_id].get("state"))
            new = _state_value(after_connections[server_id].get("state"))
            if old != new:
                state_changes[server_id] = (old, new)
        catalog_generation_changes = _generation_changes(
            _catalog_server_map(_as_mapping(_as_mapping(left).get("catalog"))),
            _catalog_server_map(_as_mapping(_as_mapping(right).get("catalog"))),
            key="generation",
        )
        instruction_generation_changes = _generation_changes(
            _instruction_map(_as_mapping(left)),
            _instruction_map(_as_mapping(right)),
            key="connection_generation",
        )
        return McpSnapshotDiff(
            before_digest=stable_digest(left),
            after_digest=stable_digest(right),
            changes=tuple(sorted(changes, key=lambda item: (item.path, item.kind.value))),
            server_ids_added=tuple(sorted(after_servers - before_servers)),
            server_ids_removed=tuple(sorted(before_servers - after_servers)),
            connection_state_changes=state_changes,
            catalog_generation_changes=catalog_generation_changes,
            instruction_generation_changes=instruction_generation_changes,
        )

    def _walk(self, before: JsonValue, after: JsonValue, path: str, output: list[McpSnapshotChange]) -> None:
        if type(before) is not type(after):
            output.append(self._change(path, SnapshotChangeKind.TYPE_CHANGED, before, after))
            return
        if isinstance(before, dict) and isinstance(after, dict):
            before_keys = set(before)
            after_keys = set(after)
            for key in sorted(before_keys - after_keys):
                child = f"{path}.{escape_pointer(key)}"
                output.append(self._change(child, SnapshotChangeKind.REMOVED, before[key], None))
            for key in sorted(after_keys - before_keys):
                child = f"{path}.{escape_pointer(key)}"
                output.append(self._change(child, SnapshotChangeKind.ADDED, None, after[key]))
            for key in sorted(before_keys & after_keys):
                self._walk(before[key], after[key], f"{path}.{escape_pointer(key)}", output)
            return
        if isinstance(before, list) and isinstance(after, list):
            if stable_digest(before) != stable_digest(after):
                output.append(self._change(path, SnapshotChangeKind.REPLACED, before, after))
            return
        if before != after:
            kind = SnapshotChangeKind.REPLACED
            if path.endswith(".state") or path.endswith(".status"):
                kind = SnapshotChangeKind.STATUS_CHANGED
            elif path.endswith(".generation") or path.endswith(".revision"):
                try:
                    kind = (
                        SnapshotChangeKind.GENERATION_ADVANCED
                        if int(after or 0) >= int(before or 0)
                        else SnapshotChangeKind.GENERATION_REGRESSED
                    )
                except (TypeError, ValueError):
                    pass
            output.append(self._change(path, kind, before, after))

    @staticmethod
    def _change(path: str, kind: SnapshotChangeKind, before: JsonValue, after: JsonValue) -> McpSnapshotChange:
        identity = stable_digest({"path": path, "kind": kind.value, "before": before, "after": after})
        return McpSnapshotChange(
            change_id=f"mcpchange_{identity[:24]}",
            path=path,
            kind=kind,
            before_digest=stable_digest(before),
            after_digest=stable_digest(after),
            before_preview=safe_preview(before),
            after_preview=safe_preview(after),
        )


class McpSnapshotMerger:
    TERMINAL_TASK_STATES = {"completed", "failed", "cancelled", "canceled", "rejected"}
    LIVE_CONNECTION_STATES = {"connected", "connecting", "reconnecting"}

    def __init__(
        self,
        policy: McpSessionBridgePolicy | None = None,
        *,
        validator: McpSnapshotValidator | None = None,
        differ: McpSnapshotDiffer | None = None,
    ) -> None:
        self.policy = policy or McpSessionBridgePolicy()
        self.validator = validator or McpSnapshotValidator(self.policy)
        self.differ = differ or McpSnapshotDiffer()

    def merge(
        self,
        current: Mapping[str, Any] | None,
        incoming: Mapping[str, Any] | None,
        *,
        expected_session_id: str = "",
    ) -> McpMergeReceipt:
        left = normalize_json(current or {})
        right = normalize_json(incoming or {})
        if not isinstance(left, dict) or not isinstance(right, dict):
            raise McpSnapshotMergeError("snapshots must be objects")
        if not left:
            validation = self.validator.validate(right, expected_session_id=expected_session_id)
            validation.require_valid()
            diff = self.differ.diff({}, right)
            return self._receipt(MergeDisposition.INCOMING_ONLY, left, right, right, (), diff, {}, {}, {})
        if not right:
            validation = self.validator.validate(left, expected_session_id=expected_session_id)
            validation.require_valid()
            diff = self.differ.diff(left, left)
            return self._receipt(MergeDisposition.CURRENT_ONLY, left, right, left, (), diff, {}, {}, {})
        left_session = str(left.get("session_id") or "")
        right_session = str(right.get("session_id") or "")
        if left_session != right_session or (expected_session_id and left_session != expected_session_id):
            raise McpSnapshotIdentityError("cannot merge MCP snapshots from different sessions")
        if stable_digest(left) == stable_digest(right):
            diff = self.differ.diff(left, right)
            return self._receipt(MergeDisposition.IDENTICAL, left, right, left, (), diff, {}, {}, {})

        conflicts: list[McpMergeConflict] = []
        merged: dict[str, JsonValue] = deep_merge_json(left, right)
        merged["schema"] = MCP_SESSION_SCHEMA
        merged["session_id"] = left_session
        merged["state_revision"] = max(
            _non_negative_int(left.get("state_revision")),
            _non_negative_int(right.get("state_revision")),
        )
        merged["credentials_included"] = False
        merged["live_transports_included"] = False

        connection_values, connection_sources = self._merge_connections(left, right, conflicts)
        merged["connections"] = connection_values
        catalog_value, catalog_sources = self._merge_catalog(left, right, connection_values, conflicts)
        merged["catalog"] = catalog_value
        instructions_value, instruction_sources = self._merge_instructions(left, right, connection_values, conflicts)
        merged["instructions"] = instructions_value
        merged["pending_elicitations"] = self._merge_pending(left, right, conflicts)
        merged["auth"] = self._merge_auth(left, right)

        if "tasks" in left or "tasks" in right:
            merged["tasks"] = self._merge_tasks(left.get("tasks"), right.get("tasks"), conflicts)
        validation = self.validator.validate(merged, expected_session_id=left_session)
        for issue in validation.errors:
            conflicts.append(
                McpMergeConflict(
                    path=issue.path,
                    code=f"merged_{issue.code}",
                    current_digest=stable_digest(left),
                    incoming_digest=stable_digest(right),
                    resolution="reject",
                    blocking=True,
                )
            )
        diff = self.differ.diff(left, merged)
        disposition = MergeDisposition.CONFLICT if any(item.blocking for item in conflicts) else MergeDisposition.MERGED
        return self._receipt(
            disposition,
            left,
            right,
            merged,
            tuple(conflicts),
            diff,
            connection_sources,
            catalog_sources,
            instruction_sources,
        )

    def _merge_connections(
        self,
        left: Mapping[str, Any],
        right: Mapping[str, Any],
        conflicts: list[McpMergeConflict],
    ) -> tuple[list[JsonValue], dict[str, str]]:
        current = _connection_map(left)
        incoming = _connection_map(right)
        output: list[JsonValue] = []
        sources: dict[str, str] = {}
        for server_id in sorted(set(current) | set(incoming)):
            old = current.get(server_id)
            new = incoming.get(server_id)
            selected, source = self._select_versioned(
                old,
                new,
                path=f"$.connections[{server_id}]",
                generation_keys=("generation", "revision"),
                conflicts=conflicts,
            )
            if selected is None:
                continue
            state = _state_value(selected.get("state"))
            if source == "current" and state == "connected" and new is not None:
                new_state = _state_value(new.get("state"))
                if new_state in {"failed", "closed", "disabled", "needs_auth"}:
                    selected = dict(new)
                    source = "incoming_terminal"
            output.append(normalize_json(selected))
            sources[server_id] = source
        return output, sources

    def _merge_catalog(
        self,
        left: Mapping[str, Any],
        right: Mapping[str, Any],
        connections: Sequence[JsonValue],
        conflicts: list[McpMergeConflict],
    ) -> tuple[dict[str, JsonValue], dict[str, str]]:
        left_catalog = _as_mapping(left.get("catalog"))
        right_catalog = _as_mapping(right.get("catalog"))
        current = _catalog_server_map(left_catalog)
        incoming = _catalog_server_map(right_catalog)
        connected = {
            str(value.get("server_id"))
            for value in connections
            if isinstance(value, Mapping) and _state_value(value.get("state")) == "connected"
        }
        selected_servers: dict[str, JsonValue] = {}
        sources: dict[str, str] = {}
        for server_id in sorted(set(current) | set(incoming)):
            if server_id not in connected:
                sources[server_id] = "withdrawn_not_connected"
                continue
            selected, source = self._select_versioned(
                current.get(server_id),
                incoming.get(server_id),
                path=f"$.catalog.servers.{escape_pointer(server_id)}",
                generation_keys=("connection_generation", "generation"),
                conflicts=conflicts,
            )
            if selected is not None:
                selected_servers[server_id] = normalize_json(selected)
                sources[server_id] = source
        template = deep_merge_json(left_catalog, right_catalog)
        if not isinstance(template, dict):
            template = {}
        if "servers" in template or "snapshots" not in template:
            template["servers"] = selected_servers
        else:
            template["snapshots"] = selected_servers
        template["server_count"] = len(selected_servers)
        template["tool_count"] = sum(len(_mapping_sequence(_as_mapping(value).get("tools"))) for value in selected_servers.values())
        template["resource_count"] = sum(
            len(_mapping_sequence(_as_mapping(value).get("resources"))) for value in selected_servers.values()
        )
        template["prompt_count"] = sum(
            len(_mapping_sequence(_as_mapping(value).get("prompts"))) for value in selected_servers.values()
        )
        return template, sources

    def _merge_instructions(
        self,
        left: Mapping[str, Any],
        right: Mapping[str, Any],
        connections: Sequence[JsonValue],
        conflicts: list[McpMergeConflict],
    ) -> tuple[dict[str, JsonValue], dict[str, str]]:
        current = _instruction_map(left)
        incoming = _instruction_map(right)
        generations = {
            str(value.get("server_id")): _non_negative_int(value.get("generation"))
            for value in connections
            if isinstance(value, Mapping)
        }
        selected: list[JsonValue] = []
        sources: dict[str, str] = {}
        for server_id in sorted(set(current) | set(incoming)):
            value, source = self._select_versioned(
                current.get(server_id),
                incoming.get(server_id),
                path=f"$.instructions.active[{server_id}]",
                generation_keys=("connection_generation", "revision"),
                conflicts=conflicts,
            )
            if value is None:
                continue
            if server_id not in generations:
                sources[server_id] = "withdrawn_missing_connection"
                continue
            instruction_generation = _non_negative_int(value.get("connection_generation"))
            if instruction_generation > generations[server_id]:
                conflicts.append(
                    McpMergeConflict(
                        path=f"$.instructions.active[{server_id}]",
                        code="instruction_generation_ahead",
                        current_digest=stable_digest(current.get(server_id)),
                        incoming_digest=stable_digest(incoming.get(server_id)),
                        resolution="withdraw",
                        blocking=False,
                    )
                )
                sources[server_id] = "withdrawn_generation_ahead"
                continue
            if bool(value.get("enabled", True)) and str(value.get("instructions") or ""):
                selected.append(normalize_json(value))
                sources[server_id] = source
        template = deep_merge_json(_as_mapping(left.get("instructions")), _as_mapping(right.get("instructions")))
        if not isinstance(template, dict):
            template = {}
        session_id = str(right.get("session_id") or left.get("session_id") or "")
        template["schema"] = MCP_INSTRUCTIONS_SCHEMA
        template["session_id"] = session_id
        template["active"] = selected
        template["restore_constraints"] = [instruction_restore_constraint(value) for value in selected]
        template["history"] = merge_history(
            _as_mapping(left.get("instructions")).get("history"),
            _as_mapping(right.get("instructions")).get("history"),
        )
        return template, sources

    def _merge_pending(
        self,
        left: Mapping[str, Any],
        right: Mapping[str, Any],
        conflicts: list[McpMergeConflict],
    ) -> list[JsonValue]:
        current = _identity_map(left.get("pending_elicitations"), "request_id")
        incoming = _identity_map(right.get("pending_elicitations"), "request_id")
        output: list[JsonValue] = []
        for request_id in sorted(set(current) | set(incoming)):
            selected, _ = self._select_versioned(
                current.get(request_id),
                incoming.get(request_id),
                path=f"$.pending_elicitations[{request_id}]",
                generation_keys=("revision",),
                conflicts=conflicts,
            )
            if selected is not None and str(selected.get("status") or "pending") == "pending":
                output.append(normalize_json(selected))
        return output

    @staticmethod
    def _merge_auth(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, JsonValue]:
        current = _as_mapping(left.get("auth"))
        incoming = _as_mapping(right.get("auth"))
        output: dict[str, JsonValue] = {}
        for server_id in sorted(set(current) | set(incoming)):
            old = _as_mapping(current.get(server_id))
            new = _as_mapping(incoming.get(server_id))
            old_revision = _non_negative_int(old.get("revision"))
            new_revision = _non_negative_int(new.get("revision"))
            output[server_id] = normalize_json(new if new_revision >= old_revision else old)
        return output

    def _merge_tasks(
        self,
        left: Any,
        right: Any,
        conflicts: list[McpMergeConflict],
    ) -> JsonValue:
        current = flatten_task_map(left)
        incoming = flatten_task_map(right)
        output: dict[str, JsonValue] = {}
        for identity in sorted(set(current) | set(incoming)):
            old = current.get(identity)
            new = incoming.get(identity)
            if old is not None and new is not None:
                old_status = _state_value(old.get("status"))
                new_status = _state_value(new.get("status"))
                if (
                    self.policy.preserve_terminal_task_state
                    and old_status in self.TERMINAL_TASK_STATES
                    and new_status not in self.TERMINAL_TASK_STATES
                ):
                    output[identity] = normalize_json(old)
                    conflicts.append(
                        McpMergeConflict(
                            path=f"$.tasks.{escape_pointer(identity)}",
                            code="terminal_task_regression_prevented",
                            current_digest=stable_digest(old),
                            incoming_digest=stable_digest(new),
                            resolution="current_terminal",
                            blocking=False,
                        )
                    )
                    continue
            selected, _ = self._select_versioned(
                old,
                new,
                path=f"$.tasks.{escape_pointer(identity)}",
                generation_keys=("revision",),
                conflicts=conflicts,
            )
            if selected is not None:
                output[identity] = normalize_json(selected)
        return output

    def _select_versioned(
        self,
        current: Mapping[str, Any] | None,
        incoming: Mapping[str, Any] | None,
        *,
        path: str,
        generation_keys: Sequence[str],
        conflicts: list[McpMergeConflict],
    ) -> tuple[Mapping[str, Any] | None, str]:
        if current is None:
            return incoming, "incoming"
        if incoming is None:
            return current, "current"
        if stable_digest(current) == stable_digest(incoming):
            return current, "identical"
        current_version = tuple(_non_negative_int(current.get(key)) for key in generation_keys)
        incoming_version = tuple(_non_negative_int(incoming.get(key)) for key in generation_keys)
        if incoming_version > current_version:
            return incoming, "incoming_newer"
        if current_version > incoming_version:
            if self.policy.reject_generation_regression:
                conflicts.append(
                    McpMergeConflict(
                        path=path,
                        code="generation_regression_prevented",
                        current_digest=stable_digest(current),
                        incoming_digest=stable_digest(incoming),
                        resolution="current_newer",
                        blocking=False,
                    )
                )
            return current, "current_newer"
        preference = self.policy.merge_preference
        if preference is MergePreference.INCOMING:
            return incoming, "incoming_preferred"
        if preference in {MergePreference.CURRENT, MergePreference.HIGHEST_GENERATION}:
            conflicts.append(
                McpMergeConflict(
                    path=path,
                    code="same_generation_content_conflict",
                    current_digest=stable_digest(current),
                    incoming_digest=stable_digest(incoming),
                    resolution="current_deterministic",
                    blocking=False,
                )
            )
            return current, "current_deterministic"
        conflicts.append(
            McpMergeConflict(
                path=path,
                code="same_generation_content_conflict",
                current_digest=stable_digest(current),
                incoming_digest=stable_digest(incoming),
                resolution="reject",
                blocking=True,
            )
        )
        return current, "conflict"

    @staticmethod
    def _receipt(
        disposition: MergeDisposition,
        current: Mapping[str, Any],
        incoming: Mapping[str, Any],
        merged: Mapping[str, Any],
        conflicts: tuple[McpMergeConflict, ...],
        diff: McpSnapshotDiff,
        connection_sources: Mapping[str, str],
        catalog_sources: Mapping[str, str],
        instruction_sources: Mapping[str, str],
    ) -> McpMergeReceipt:
        normalized = normalize_json(merged)
        assert isinstance(normalized, dict)
        return McpMergeReceipt(
            disposition=disposition,
            snapshot=normalized,
            current_digest=stable_digest(current),
            incoming_digest=stable_digest(incoming),
            merged_digest=stable_digest(normalized),
            conflicts=conflicts,
            diff_from_current=diff,
            selected_connection_sources=dict(connection_sources),
            selected_catalog_sources=dict(catalog_sources),
            selected_instruction_sources=dict(instruction_sources),
        )


class McpCheckpointCausalityVerifier:
    def verify(
        self,
        receipt: Mapping[str, Any],
        *,
        identity: McpSessionIdentity,
        snapshot: Mapping[str, Any],
        runtime_state: Mapping[str, Any],
        sequence: int,
    ) -> bool:
        if str(receipt.get("schema") or "") != MCP_CAUSAL_RECEIPT_SCHEMA:
            raise McpCausalityError("checkpoint causal receipt schema is invalid")
        if str(receipt.get("identity_digest") or "") != identity.digest:
            raise McpCausalityError("checkpoint identity digest mismatch")
        if str(receipt.get("snapshot_digest") or "") != stable_digest(snapshot):
            raise McpCausalityError("checkpoint snapshot digest mismatch")
        if str(receipt.get("runtime_state_digest") or "") != stable_digest(runtime_state):
            raise McpCausalityError("checkpoint runtime-state digest mismatch")
        parent_sequence = _non_negative_int(receipt.get("parent_sequence"))
        if parent_sequence > sequence:
            raise McpCausalityError("checkpoint parent sequence is ahead of loaded sequence")
        expected_core = stable_digest(
            {
                "checkpoint_id": str(receipt.get("checkpoint_id") or ""),
                "identity_digest": str(receipt.get("identity_digest") or ""),
                "parent_sequence": parent_sequence,
                "parent_snapshot_digest": str(receipt.get("parent_snapshot_digest") or ""),
                "snapshot_digest": str(receipt.get("snapshot_digest") or ""),
                "state_revision": _non_negative_int(receipt.get("state_revision")),
                "change_ids": list(_string_sequence(receipt.get("change_ids"))),
                "runtime_state_digest": str(receipt.get("runtime_state_digest") or ""),
                "created_at": str(receipt.get("created_at") or ""),
            }
        )
        if str(receipt.get("core_digest") or "") != expected_core:
            raise McpCausalityError("checkpoint causal receipt core digest mismatch")
        return True


class McpSessionBridge:
    """Coordinate MCP snapshots with the existing CodeWorker session owner."""

    def __init__(
        self,
        *,
        store: CodeWorkerSessionStorePort,
        policy: McpSessionBridgePolicy | None = None,
        validator: McpSnapshotValidator | None = None,
        differ: McpSnapshotDiffer | None = None,
        merger: McpSnapshotMerger | None = None,
        causality_verifier: McpCheckpointCausalityVerifier | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or McpSessionBridgePolicy()
        self.validator = validator or McpSnapshotValidator(self.policy)
        self.differ = differ or McpSnapshotDiffer()
        self.merger = merger or McpSnapshotMerger(
            self.policy,
            validator=self.validator,
            differ=self.differ,
        )
        self.causality_verifier = causality_verifier or McpCheckpointCausalityVerifier()

    def capture(
        self,
        runtime: McpSnapshotRuntimePort,
        identity: McpSessionIdentity,
        *,
        source: str = "McpClientRuntime.session_snapshot",
    ) -> McpSnapshotEnvelope:
        raw = runtime.session_snapshot(identity.session_id)
        normalized = normalize_json(raw)
        if not isinstance(normalized, dict):
            raise McpSnapshotValidationError("runtime returned a non-object MCP snapshot")
        validation = self.validator.validate(normalized, expected_session_id=identity.session_id)
        validation.require_valid()
        return McpSnapshotEnvelope(
            identity=identity,
            snapshot=normalized,
            validation=validation,
            captured_at=now_iso(),
            source=source,
        )

    def load(self, identity: McpSessionIdentity) -> CodeWorkerRuntimeStateLoadPort:
        load = self.store.load_runtime_state(
            session_id=identity.session_id,
            run_id=identity.run_id,
            task_id=identity.task_id,
        )
        if not load.ok:
            raise McpCheckpointError(load.error or "CodeWorker runtime-state load failed")
        if load.found and load.session_id and load.session_id != identity.session_id:
            raise McpSnapshotIdentityError("loaded runtime state belongs to another session")
        if load.found and load.run_id and load.run_id != identity.run_id:
            raise McpSnapshotIdentityError("loaded runtime state belongs to another run")
        if load.found and load.task_id and load.task_id != identity.task_id:
            raise McpSnapshotIdentityError("loaded runtime state belongs to another task")
        return load

    def checkpoint_runtime(
        self,
        runtime: McpSnapshotRuntimePort,
        identity: McpSessionIdentity,
        *,
        disabled: bool = False,
    ) -> McpCheckpointReceipt:
        return self.checkpoint(self.capture(runtime, identity), disabled=disabled)

    def checkpoint(
        self,
        envelope: McpSnapshotEnvelope,
        *,
        disabled: bool = False,
    ) -> McpCheckpointReceipt:
        identity = envelope.identity
        envelope.validation.require_valid()
        load = self.load(identity)
        existing_runtime_state = dict(load.runtime_state) if load.found else {}
        existing_snapshot = _as_mapping(existing_runtime_state.get(MCP_RUNTIME_STATE_KEY))
        merge: McpMergeReceipt | None = None
        if existing_snapshot:
            merge = self.merger.merge(
                existing_snapshot,
                envelope.snapshot,
                expected_session_id=identity.session_id,
            )
            merge.require_merged()
            selected_snapshot = dict(merge.snapshot)
        else:
            selected_snapshot = dict(envelope.snapshot)
        validation = self.validator.validate(selected_snapshot, expected_session_id=identity.session_id)
        validation.require_valid()
        diff = self.differ.diff(existing_snapshot, selected_snapshot)
        next_runtime_state = copy.deepcopy(existing_runtime_state)
        next_runtime_state[MCP_RUNTIME_STATE_KEY] = selected_snapshot
        runtime_state_digest = stable_digest(next_runtime_state)
        previous_digest = stable_digest(existing_snapshot) if existing_snapshot else ""
        checkpoint_id = checkpoint_identity(
            identity=identity,
            parent_sequence=int(load.sequence or 0),
            snapshot_digest=validation.snapshot_digest,
            runtime_state_digest=runtime_state_digest,
        )
        causality = McpCheckpointCausality(
            checkpoint_id=checkpoint_id,
            identity_digest=identity.digest,
            parent_sequence=int(load.sequence or 0),
            parent_snapshot_digest=previous_digest,
            snapshot_digest=validation.snapshot_digest,
            state_revision=validation.state_revision,
            change_ids=tuple(change.change_id for change in diff.changes),
            runtime_state_digest=runtime_state_digest,
            created_at=now_iso(),
        )
        if not diff.changed and load.found and not self.policy.write_unchanged_checkpoint:
            return McpCheckpointReceipt(
                ok=True,
                disposition=CheckpointDisposition.UNCHANGED,
                identity=identity,
                snapshot_digest=validation.snapshot_digest,
                runtime_state_digest=runtime_state_digest,
                previous_sequence=int(load.sequence or 0),
                checkpoint_sequence=int(load.sequence or 0),
                causality=causality,
                validation=validation,
                diff=diff,
                merge=merge,
            )
        expected_sequence = int(load.sequence or 0) if self.policy.compare_and_append else None
        store_receipt = self.store.append_runtime_state(
            session_id=identity.session_id,
            worker_request_id=identity.worker_request_id,
            run_id=identity.run_id,
            task_id=identity.task_id,
            runtime_state=next_runtime_state,
            causal_receipt=causality.to_dict(),
            expected_previous_sequence=expected_sequence,
            disabled=disabled,
        )
        if not store_receipt.ok:
            return McpCheckpointReceipt(
                ok=False,
                disposition=CheckpointDisposition.REJECTED,
                identity=identity,
                snapshot_digest=validation.snapshot_digest,
                runtime_state_digest=runtime_state_digest,
                previous_sequence=int(load.sequence or 0),
                checkpoint_sequence=int(load.sequence or 0),
                causality=causality,
                validation=validation,
                diff=diff,
                merge=merge,
                error=store_receipt.error or "CodeWorker session store rejected MCP checkpoint",
            )
        return McpCheckpointReceipt(
            ok=True,
            disposition=CheckpointDisposition.APPENDED,
            identity=identity,
            snapshot_digest=validation.snapshot_digest,
            runtime_state_digest=runtime_state_digest,
            previous_sequence=int(load.sequence or 0),
            checkpoint_sequence=int(getattr(store_receipt, "last_sequence", load.sequence or 0)),
            causality=causality,
            validation=validation,
            diff=diff,
            merge=merge,
        )

    def restore(
        self,
        runtime: McpSnapshotRuntimePort,
        identity: McpSessionIdentity,
        *,
        require_causality: bool = True,
    ) -> McpRestoreReceipt:
        load = self.load(identity)
        if not load.found:
            return McpRestoreReceipt(
                ok=True,
                disposition=RestoreDisposition.NOT_FOUND,
                identity=identity,
                sequence=int(load.sequence or 0),
                snapshot_digest="",
                restored_instruction_count=0,
                validation=None,
                causality_verified=False,
                causal_receipt={},
            )
        runtime_state = dict(load.runtime_state)
        snapshot = _as_mapping(runtime_state.get(MCP_RUNTIME_STATE_KEY))
        if not snapshot:
            return McpRestoreReceipt(
                ok=False,
                disposition=RestoreDisposition.REJECTED,
                identity=identity,
                sequence=int(load.sequence or 0),
                snapshot_digest="",
                restored_instruction_count=0,
                validation=None,
                causality_verified=False,
                causal_receipt=normalize_json(load.causal_receipt),
                error="mcp_runtime checkpoint missing",
            )
        validation = self.validator.validate(snapshot, expected_session_id=identity.session_id)
        try:
            validation.require_valid()
        except McpSnapshotValidationError as error:
            return McpRestoreReceipt(
                ok=False,
                disposition=RestoreDisposition.REJECTED,
                identity=identity,
                sequence=int(load.sequence or 0),
                snapshot_digest=validation.snapshot_digest,
                restored_instruction_count=0,
                validation=validation,
                causality_verified=False,
                causal_receipt=normalize_json(load.causal_receipt),
                error=str(error),
            )
        causal_receipt = _as_mapping(load.causal_receipt)
        verified = False
        if causal_receipt:
            try:
                verified = self.causality_verifier.verify(
                    causal_receipt,
                    identity=identity,
                    snapshot=snapshot,
                    runtime_state=runtime_state,
                    sequence=int(load.sequence or 0),
                )
            except McpCausalityError as error:
                if require_causality:
                    return McpRestoreReceipt(
                        ok=False,
                        disposition=RestoreDisposition.REJECTED,
                        identity=identity,
                        sequence=int(load.sequence or 0),
                        snapshot_digest=validation.snapshot_digest,
                        restored_instruction_count=0,
                        validation=validation,
                        causality_verified=False,
                        causal_receipt=normalize_json(causal_receipt),
                        error=str(error),
                    )
        elif require_causality:
            return McpRestoreReceipt(
                ok=False,
                disposition=RestoreDisposition.REJECTED,
                identity=identity,
                sequence=int(load.sequence or 0),
                snapshot_digest=validation.snapshot_digest,
                restored_instruction_count=0,
                validation=validation,
                causality_verified=False,
                causal_receipt={},
                error="checkpoint causal receipt missing",
            )
        restored_count = runtime.restore_session_snapshot(snapshot)
        return McpRestoreReceipt(
            ok=True,
            disposition=RestoreDisposition.RESTORED,
            identity=identity,
            sequence=int(load.sequence or 0),
            snapshot_digest=validation.snapshot_digest,
            restored_instruction_count=int(restored_count),
            validation=validation,
            causality_verified=verified,
            causal_receipt=normalize_json(causal_receipt),
        )

    def diff_checkpoint(self, identity: McpSessionIdentity, snapshot: Mapping[str, Any]) -> McpSnapshotDiff:
        load = self.load(identity)
        current = _as_mapping(load.runtime_state.get(MCP_RUNTIME_STATE_KEY)) if load.found else {}
        return self.differ.diff(current, snapshot)

    def validate_checkpoint(self, identity: McpSessionIdentity) -> McpSnapshotValidation | None:
        load = self.load(identity)
        if not load.found:
            return None
        snapshot = _as_mapping(load.runtime_state.get(MCP_RUNTIME_STATE_KEY))
        if not snapshot:
            raise McpSnapshotValidationError("runtime state has no mcp_runtime snapshot")
        return self.validator.validate(snapshot, expected_session_id=identity.session_id)


def normalize_json(value: Any, *, max_depth: int = 32, _depth: int = 0) -> JsonValue:
    if _depth > max_depth:
        raise McpSnapshotValidationError("snapshot exceeds maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise McpSnapshotValidationError("non-finite float is not JSON safe")
        return value
    if isinstance(value, Mapping):
        output: dict[str, JsonValue] = {}
        for raw_key, raw_value in sorted(value.items(), key=lambda item: str(item[0])):
            key = str(raw_key)
            if not key:
                raise McpSnapshotValidationError("snapshot contains an empty object key")
            output[key] = normalize_json(raw_value, max_depth=max_depth, _depth=_depth + 1)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [normalize_json(item, max_depth=max_depth, _depth=_depth + 1) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return normalize_json(value.to_dict(), max_depth=max_depth, _depth=_depth + 1)
    raise McpSnapshotValidationError(f"snapshot contains unsupported value {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def checkpoint_identity(
    *,
    identity: McpSessionIdentity,
    parent_sequence: int,
    snapshot_digest: str,
    runtime_state_digest: str,
) -> str:
    digest = stable_digest(
        {
            "identity": identity.to_dict(),
            "parent_sequence": parent_sequence,
            "snapshot_digest": snapshot_digest,
            "runtime_state_digest": runtime_state_digest,
        }
    )
    return f"mcpcheckpoint_{digest[:32]}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_preview(value: JsonValue, *, limit: int = 240) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        lowered = value.casefold()
        if any(marker in lowered for marker in ("bearer ", "access_token", "refresh_token", "password=")):
            return REDACTED
        return value if len(value) <= limit else value[:limit] + "..."
    if isinstance(value, list):
        return {"type": "array", "count": len(value), "digest": stable_digest(value)}
    if isinstance(value, dict):
        return {"type": "object", "keys": sorted(value)[:32], "digest": stable_digest(value)}
    return str(type(value).__name__)


def escape_pointer(value: str) -> str:
    return str(value).replace("~", "~0").replace(".", "~1")


def walk_leaves(
    value: Any,
    *,
    path: str = "$",
    max_depth: int = 32,
    _depth: int = 0,
) -> Iterable[tuple[str, JsonScalar]]:
    if _depth > max_depth:
        raise McpSnapshotValidationError("snapshot exceeds maximum nesting depth")
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from walk_leaves(
                child,
                path=f"{path}.{escape_pointer(str(key))}",
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            yield from walk_leaves(
                child,
                path=f"{path}[{index}]",
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        return
    if value is None or isinstance(value, (str, bool, int, float)):
        yield path, value


def deep_merge_json(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, JsonValue]:
    output = normalize_json(current)
    if not isinstance(output, dict):
        output = {}
    for key, raw_value in incoming.items():
        old = output.get(str(key))
        if isinstance(old, dict) and isinstance(raw_value, Mapping):
            output[str(key)] = deep_merge_json(old, raw_value)
        else:
            output[str(key)] = normalize_json(raw_value)
    return output


def merge_history(current: Any, incoming: Any) -> list[JsonValue]:
    output: dict[str, JsonValue] = {}
    for value in (*_mapping_sequence(current), *_mapping_sequence(incoming)):
        identity = str(value.get("delta_id") or value.get("id") or stable_digest(value))
        existing = output.get(identity)
        if existing is None or _non_negative_int(_as_mapping(existing).get("revision")) <= _non_negative_int(
            value.get("revision")
        ):
            output[identity] = normalize_json(value)
    return [output[key] for key in sorted(output)]


def instruction_restore_constraint(value: JsonValue) -> JsonValue:
    state = _as_mapping(value)
    server_id = str(state.get("server_id") or "")
    generation = _non_negative_int(state.get("connection_generation"))
    revision = _non_negative_int(state.get("revision"))
    return {
        "id": f"{server_id}:{generation}:{revision}",
        "server": server_id,
        "instructions": str(state.get("instructions") or ""),
        "instructions_hash": str(state.get("instructions_hash") or ""),
        "connection_generation": generation,
        "revision": revision,
        "artifact_id": str(state.get("artifact_id") or ""),
        "source_event_id": str(state.get("source_event_id") or ""),
        "source_provenance": "mcp_instruction_delta",
        "trust_level": "external_untrusted",
        "untrusted": True,
    }


def flatten_task_map(value: Any) -> dict[str, Mapping[str, Any]]:
    output: dict[str, Mapping[str, Any]] = {}

    def visit(candidate: Any, server_hint: str = "") -> None:
        if not isinstance(candidate, Mapping):
            return
        if "task_id" in candidate or "taskId" in candidate:
            task_id = str(candidate.get("task_id") or candidate.get("taskId") or "")
            server_id = str(candidate.get("server_id") or server_hint)
            if task_id:
                output[f"{server_id}:{task_id}"] = candidate
            return
        for key, child in candidate.items():
            visit(child, server_hint=server_hint or str(key))

    visit(value)
    return output


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_sequence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(str(item) for item in value)


def _state_value(value: Any) -> str:
    text = str(value or "").casefold()
    return text.rsplit(".", 1)[-1]


def _non_negative_int(value: Any, default: int = 0) -> int:
    try:
        selected = int(value)
    except (TypeError, ValueError):
        return default
    return selected if selected >= 0 else default


def _connection_map(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(value.get("server_id")): value
        for value in _mapping_sequence(snapshot.get("connections"))
        if str(value.get("server_id") or "")
    }


def _catalog_server_map(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    candidates = catalog.get("servers")
    if not isinstance(candidates, Mapping):
        candidates = catalog.get("snapshots")
    if isinstance(candidates, Mapping):
        return {
            str(server_id): value
            for server_id, value in candidates.items()
            if isinstance(value, Mapping)
        }
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes, bytearray)):
        return {
            str(value.get("server_id")): value
            for value in candidates
            if isinstance(value, Mapping) and str(value.get("server_id") or "")
        }
    return {}


def _instruction_map(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    instructions = _as_mapping(snapshot.get("instructions"))
    return {
        str(value.get("server_id")): value
        for value in _mapping_sequence(instructions.get("active"))
        if str(value.get("server_id") or "")
    }


def _identity_map(value: Any, key: str) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get(key)): item
        for item in _mapping_sequence(value)
        if str(item.get(key) or "")
    }


def _generation_changes(
    current: Mapping[str, Mapping[str, Any]],
    incoming: Mapping[str, Mapping[str, Any]],
    *,
    key: str,
) -> dict[str, tuple[int, int]]:
    output: dict[str, tuple[int, int]] = {}
    for identity in sorted(set(current) & set(incoming)):
        before = _non_negative_int(current[identity].get(key))
        after = _non_negative_int(incoming[identity].get(key))
        if before != after:
            output[identity] = (before, after)
    return output


__all__ = [
    "MCP_BRIDGE_SCHEMA",
    "MCP_CAUSAL_RECEIPT_SCHEMA",
    "MCP_DIFF_SCHEMA",
    "MCP_INSTRUCTIONS_SCHEMA",
    "MCP_MERGE_SCHEMA",
    "MCP_RUNTIME_STATE_KEY",
    "MCP_SESSION_SCHEMA",
    "CheckpointDisposition",
    "CodeWorkerRuntimeStateLoadPort",
    "CodeWorkerSessionStorePort",
    "CodeWorkerSessionStoreReceiptPort",
    "JsonScalar",
    "JsonValue",
    "McpCausalityError",
    "McpCheckpointCausality",
    "McpCheckpointCausalityVerifier",
    "McpCheckpointError",
    "McpCheckpointReceipt",
    "McpMergeConflict",
    "McpMergeReceipt",
    "McpRestoreError",
    "McpRestoreReceipt",
    "McpSessionBridge",
    "McpSessionBridgeError",
    "McpSessionBridgePolicy",
    "McpSessionIdentity",
    "McpSnapshotChange",
    "McpSnapshotDiff",
    "McpSnapshotDiffer",
    "McpSnapshotEnvelope",
    "McpSnapshotIdentityError",
    "McpSnapshotMergeError",
    "McpSnapshotMerger",
    "McpSnapshotRuntimePort",
    "McpSnapshotValidation",
    "McpSnapshotValidationError",
    "McpSnapshotValidator",
    "MergeDisposition",
    "MergePreference",
    "RestoreDisposition",
    "SnapshotChangeKind",
    "SnapshotIssueSeverity",
    "canonical_json_bytes",
    "checkpoint_identity",
    "deep_merge_json",
    "flatten_task_map",
    "instruction_restore_constraint",
    "merge_history",
    "normalize_json",
    "now_iso",
    "safe_preview",
    "stable_digest",
    "walk_leaves",
]
