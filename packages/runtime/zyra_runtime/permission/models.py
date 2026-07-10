from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("expected a mapping")
    return {str(key): item for key, item in value.items()}


def _enum(enum_type: type[StrEnum], value: Any, field_name: str) -> Any:
    try:
        return enum_type(str(value))
    except ValueError as error:
        raise ValueError(f"invalid {field_name}: {value!r}") from error


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("expected a sequence of strings")
    return tuple(str(item) for item in value)


class PermissionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionMode(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    DONT_ASK = "dont_ask"
    PLAN = "plan"
    AUTO = "auto"
    BYPASS = "bypass"
    SEALED = "sealed"


class PermissionRuleSource(StrEnum):
    BUILTIN_SAFETY = "builtin_safety"
    POLICY = "policy"
    CLI = "cli"
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
    COMMAND = "command"
    SESSION = "session"


class PermissionScopeKind(StrEnum):
    ACTION = "action"
    SESSION = "session"
    TASK = "task"
    RUN = "run"
    WORKSPACE = "workspace"
    PROJECT = "project"
    USER = "user"
    GLOBAL = "global"


class PermissionRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ABORTED = "aborted"


class PermissionRequestPhase(StrEnum):
    CREATED = "created"
    DELIVERED = "delivered"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    namespace: str
    name: str
    server_id: str = ""
    version: str = ""
    schema_digest: str = ""

    def __post_init__(self) -> None:
        if not self.namespace.strip() or not self.name.strip():
            raise ValueError("tool namespace and name are required")

    @property
    def canonical_name(self) -> str:
        server = f"/{self.server_id}" if self.server_id else ""
        version = f"@{self.version}" if self.version else ""
        return f"{self.namespace}{server}/{self.name}{version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "server_id": self.server_id,
            "version": self.version,
            "schema_digest": self.schema_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolIdentity":
        item = _mapping(data)
        return cls(
            namespace=str(item.get("namespace") or "builtin"),
            name=str(item.get("name") or item.get("tool_name") or ""),
            server_id=str(item.get("server_id") or ""),
            version=str(item.get("version") or ""),
            schema_digest=str(item.get("schema_digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class PermissionScope:
    kind: PermissionScopeKind
    session_id: str = ""
    task_id: str = ""
    run_id: str = ""
    workspace_root: str = ""
    project_id: str = ""
    principal_id: str = ""
    tool_namespace: str = ""
    tool_name: str = ""
    server_id: str = ""
    argument_digest: str = ""
    request_fingerprint: str = ""
    path_prefixes: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    expires_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = {
            PermissionScopeKind.ACTION: self.request_fingerprint or self.argument_digest,
            PermissionScopeKind.SESSION: self.session_id,
            PermissionScopeKind.TASK: self.task_id,
            PermissionScopeKind.RUN: self.run_id,
            PermissionScopeKind.WORKSPACE: self.workspace_root,
            PermissionScopeKind.PROJECT: self.project_id,
            PermissionScopeKind.USER: self.principal_id,
            PermissionScopeKind.GLOBAL: "global",
        }[self.kind]
        if not required:
            raise ValueError(f"{self.kind} scope is missing its required selector")
        canonical_workspace = ""
        if self.workspace_root:
            canonical_workspace = str(Path(self.workspace_root).resolve())
            object.__setattr__(self, "workspace_root", canonical_workspace)
        canonical_prefixes: list[str] = []
        for value in self.path_prefixes:
            candidate = Path(str(value))
            if not candidate.is_absolute():
                if not canonical_workspace:
                    raise ValueError(
                        "relative permission path prefixes require a workspace_root"
                    )
                candidate = Path(canonical_workspace) / candidate
            canonical_prefixes.append(str(candidate.resolve()))
        object.__setattr__(self, "path_prefixes", tuple(canonical_prefixes))
        object.__setattr__(
            self,
            "domains",
            tuple(str(value).lower().rstrip(".") for value in self.domains if str(value)),
        )

    def is_expired(self, at: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        moment = at or datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return moment >= expiry

    def contains(self, request: "PermissionEvaluationRequest", *, at: datetime | None = None) -> bool:
        if self.is_expired(at):
            return False
        comparisons = (
            (self.session_id, request.session_id),
            (self.task_id, request.task_id),
            (self.run_id, request.run_id),
            (self.project_id, request.project_id),
            (self.principal_id, request.principal_id),
            (self.tool_namespace, request.tool_identity.namespace),
            (self.tool_name, request.tool_identity.name),
            (self.server_id, request.tool_identity.server_id),
            (self.argument_digest, request.arguments_digest),
            (self.request_fingerprint, request.request_fingerprint),
        )
        if any(expected and expected != actual for expected, actual in comparisons):
            return False
        if self.workspace_root:
            if not request.workspace_root:
                return False
            try:
                Path(request.workspace_root).resolve().relative_to(Path(self.workspace_root).resolve())
            except (ValueError, OSError):
                return False
        request_path = str(request.attributes.get("path") or "")
        if self.path_prefixes:
            if not request_path or not request.workspace_root:
                return False
            try:
                candidate = Path(request_path)
                if not candidate.is_absolute():
                    candidate = Path(request.workspace_root).resolve() / candidate
                canonical_request_path = candidate.resolve()
            except (OSError, ValueError):
                return False
            if not any(
                _path_within(canonical_request_path, Path(prefix))
                for prefix in self.path_prefixes
            ):
                return False
        request_domain = str(request.attributes.get("domain") or "").lower().rstrip(".")
        if self.domains and not any(_domain_within(request_domain, domain) for domain in self.domains):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "workspace_root": self.workspace_root,
            "project_id": self.project_id,
            "principal_id": self.principal_id,
            "tool_namespace": self.tool_namespace,
            "tool_name": self.tool_name,
            "server_id": self.server_id,
            "argument_digest": self.argument_digest,
            "request_fingerprint": self.request_fingerprint,
            "path_prefixes": list(self.path_prefixes),
            "domains": list(self.domains),
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionScope":
        item = _mapping(data)
        return cls(
            kind=_enum(PermissionScopeKind, item.get("kind") or "action", "scope kind"),
            session_id=str(item.get("session_id") or ""),
            task_id=str(item.get("task_id") or ""),
            run_id=str(item.get("run_id") or ""),
            workspace_root=str(item.get("workspace_root") or ""),
            project_id=str(item.get("project_id") or ""),
            principal_id=str(item.get("principal_id") or ""),
            tool_namespace=str(item.get("tool_namespace") or ""),
            tool_name=str(item.get("tool_name") or ""),
            server_id=str(item.get("server_id") or ""),
            argument_digest=str(item.get("argument_digest") or ""),
            request_fingerprint=str(item.get("request_fingerprint") or ""),
            path_prefixes=_strings(item.get("path_prefixes")),
            domains=_strings(item.get("domains")),
            expires_at=None if item.get("expires_at") is None else str(item.get("expires_at")),
            metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class PermissionEvaluationRequest:
    run_id: str
    task_id: str
    session_id: str
    tool_use_id: str
    tool_identity: ToolIdentity
    arguments: dict[str, Any]
    worker_request_id: str = ""
    turn_id: str = ""
    node_id: str | None = None
    operation: str = "execute"
    mode: PermissionMode = PermissionMode.DEFAULT
    workspace_root: str = ""
    project_id: str = ""
    principal_id: str = ""
    interactive: bool = True
    headless: bool = False
    requires_interaction: bool = False
    safety_flags: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    arguments_digest: str = ""
    request_fingerprint: str = ""
    created_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.run_id, self.task_id, self.session_id, self.tool_use_id)):
            raise ValueError("run_id, task_id, session_id and tool_use_id are required")
        if not isinstance(self.arguments, dict):
            raise ValueError("arguments must be a dictionary")
        if not self.arguments_digest or not self.request_fingerprint:
            from .canonical import arguments_digest, build_request_fingerprint

            digest = self.arguments_digest or arguments_digest(self.arguments)
            fingerprint = self.request_fingerprint or build_request_fingerprint(
                self.tool_identity,
                digest,
                session_id=self.session_id,
                tool_use_id=self.tool_use_id,
                run_id=self.run_id,
                task_id=self.task_id,
            )
            object.__setattr__(self, "arguments_digest", digest)
            object.__setattr__(self, "request_fingerprint", fingerprint)

    def to_dict(self, *, include_arguments: bool = True) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "turn_id": self.turn_id,
            "node_id": self.node_id,
            "tool_use_id": self.tool_use_id,
            "tool_identity": self.tool_identity.to_dict(),
            "arguments": dict(self.arguments) if include_arguments else {},
            "operation": self.operation,
            "mode": str(self.mode),
            "workspace_root": self.workspace_root,
            "project_id": self.project_id,
            "principal_id": self.principal_id,
            "interactive": self.interactive,
            "headless": self.headless,
            "requires_interaction": self.requires_interaction,
            "safety_flags": list(self.safety_flags),
            "risk_tags": list(self.risk_tags),
            "attributes": dict(self.attributes),
            "arguments_digest": self.arguments_digest,
            "request_fingerprint": self.request_fingerprint,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionEvaluationRequest":
        item = _mapping(data)
        return cls(
            run_id=str(item.get("run_id") or ""),
            task_id=str(item.get("task_id") or ""),
            session_id=str(item.get("session_id") or ""),
            worker_request_id=str(item.get("worker_request_id") or ""),
            turn_id=str(item.get("turn_id") or ""),
            node_id=None if item.get("node_id") is None else str(item.get("node_id")),
            tool_use_id=str(item.get("tool_use_id") or item.get("tool_call_id") or ""),
            tool_identity=ToolIdentity.from_dict(_mapping(item.get("tool_identity"))),
            arguments=_mapping(item.get("arguments")),
            operation=str(item.get("operation") or "execute"),
            mode=_enum(PermissionMode, item.get("mode") or "default", "permission mode"),
            workspace_root=str(item.get("workspace_root") or ""),
            project_id=str(item.get("project_id") or ""),
            principal_id=str(item.get("principal_id") or ""),
            interactive=bool(item.get("interactive", True)),
            headless=bool(item.get("headless", False)),
            requires_interaction=bool(item.get("requires_interaction", False)),
            safety_flags=_strings(item.get("safety_flags")),
            risk_tags=_strings(item.get("risk_tags")),
            attributes=_mapping(item.get("attributes")),
            arguments_digest=str(item.get("arguments_digest") or ""),
            request_fingerprint=str(item.get("request_fingerprint") or ""),
            created_at=str(item.get("created_at") or _now_iso()),
            metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class PermissionRuleRecord:
    effect: PermissionEffect
    source: PermissionRuleSource
    scope: PermissionScope
    tool_pattern: str = "*"
    namespace_pattern: str = "*"
    server_pattern: str = "*"
    operation_pattern: str = "*"
    argument_pattern: str = ""
    reason: str = ""
    priority: int = 0
    rule_id: str = field(default_factory=lambda: _id("permrule"))
    created_at: str = field(default_factory=_now_iso)
    expires_at: str | None = None
    enabled: bool = True
    max_uses: int | None = None
    use_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.rule_id or not self.tool_pattern:
            raise ValueError("rule_id and tool_pattern are required")
        if self.max_uses is not None and self.max_uses < 1:
            raise ValueError("max_uses must be positive")
        if self.use_count < 0:
            raise ValueError("use_count cannot be negative")

    def is_active(self, at: datetime | None = None) -> bool:
        if not self.enabled or (self.max_uses is not None and self.use_count >= self.max_uses):
            return False
        if not self.expires_at:
            return True
        moment = at or datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return moment < expiry

    def with_use(self) -> "PermissionRuleRecord":
        return replace(self, use_count=self.use_count + 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "effect": str(self.effect),
            "source": str(self.source),
            "scope": self.scope.to_dict(),
            "tool_pattern": self.tool_pattern,
            "namespace_pattern": self.namespace_pattern,
            "server_pattern": self.server_pattern,
            "operation_pattern": self.operation_pattern,
            "argument_pattern": self.argument_pattern,
            "reason": self.reason,
            "priority": self.priority,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "enabled": self.enabled,
            "max_uses": self.max_uses,
            "use_count": self.use_count,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionRuleRecord":
        item = _mapping(data)
        return cls(
            rule_id=str(item.get("rule_id") or _id("permrule")),
            effect=_enum(PermissionEffect, item.get("effect") or "ask", "permission effect"),
            source=_enum(PermissionRuleSource, item.get("source") or "session", "rule source"),
            scope=PermissionScope.from_dict(_mapping(item.get("scope"))),
            tool_pattern=str(item.get("tool_pattern") or "*"),
            namespace_pattern=str(item.get("namespace_pattern") or "*"),
            server_pattern=str(item.get("server_pattern") or "*"),
            operation_pattern=str(item.get("operation_pattern") or "*"),
            argument_pattern=str(item.get("argument_pattern") or ""),
            reason=str(item.get("reason") or ""),
            priority=int(item.get("priority") or 0),
            created_at=str(item.get("created_at") or _now_iso()),
            expires_at=None if item.get("expires_at") is None else str(item.get("expires_at")),
            enabled=bool(item.get("enabled", True)),
            max_uses=None if item.get("max_uses") is None else int(item.get("max_uses")),
            use_count=int(item.get("use_count") or 0),
            metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class PermissionRecoveryInput:
    decision_id: str
    session_id: str
    task_id: str
    run_id: str
    tool_use_id: str
    reason_code: str
    retryable: bool
    alternatives: tuple[dict[str, Any], ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    recovery_input_id: str = field(default_factory=lambda: _id("recovery"))
    created_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovery_input_id": self.recovery_input_id,
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "tool_use_id": self.tool_use_id,
            "reason_code": self.reason_code,
            "retryable": self.retryable,
            "alternatives": [dict(item) for item in self.alternatives],
            "constraints": dict(self.constraints),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionRecoveryInput":
        item = _mapping(data)
        return cls(
            recovery_input_id=str(item.get("recovery_input_id") or _id("recovery")),
            decision_id=str(item.get("decision_id") or ""), session_id=str(item.get("session_id") or ""),
            task_id=str(item.get("task_id") or ""), run_id=str(item.get("run_id") or ""),
            tool_use_id=str(item.get("tool_use_id") or ""), reason_code=str(item.get("reason_code") or ""),
            retryable=bool(item.get("retryable", False)),
            alternatives=tuple(_mapping(value) for value in item.get("alternatives", [])),
            constraints=_mapping(item.get("constraints")), created_at=str(item.get("created_at") or _now_iso()),
            metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class PermissionDecisionRecord:
    effect: PermissionEffect
    mode: PermissionMode
    request_fingerprint: str
    arguments_digest: str
    tool_use_id: str
    tool_identity: ToolIdentity
    session_id: str
    task_id: str
    run_id: str
    reason_code: str
    reason: str
    scope: PermissionScope
    matched_rule_ids: tuple[str, ...] = ()
    matched_rule_sources: tuple[PermissionRuleSource, ...] = ()
    request_id: str = ""
    worker_request_id: str = ""
    rule_snapshot_id: str = ""
    mode_revision: int = 0
    hook_evidence: tuple[dict[str, Any], ...] = ()
    classifier_evidence: dict[str, Any] = field(default_factory=dict)
    recovery_input: PermissionRecoveryInput | None = None
    decision_id: str = field(default_factory=lambda: _id("permdecision"))
    created_at: str = field(default_factory=_now_iso)
    expires_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            (
                self.decision_id,
                self.request_fingerprint,
                self.arguments_digest,
                self.tool_use_id,
                self.session_id,
                self.task_id,
                self.run_id,
                self.reason_code,
            )
        ):
            raise ValueError("permission decision identity and reason fields are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "effect": str(self.effect), "mode": str(self.mode),
            "request_fingerprint": self.request_fingerprint, "arguments_digest": self.arguments_digest,
            "tool_use_id": self.tool_use_id, "tool_identity": self.tool_identity.to_dict(),
            "session_id": self.session_id, "task_id": self.task_id, "run_id": self.run_id,
            "worker_request_id": self.worker_request_id, "reason_code": self.reason_code, "reason": self.reason,
            "scope": self.scope.to_dict(), "matched_rule_ids": list(self.matched_rule_ids),
            "matched_rule_sources": [str(value) for value in self.matched_rule_sources],
            "request_id": self.request_id, "rule_snapshot_id": self.rule_snapshot_id,
            "mode_revision": self.mode_revision, "hook_evidence": [dict(value) for value in self.hook_evidence],
            "classifier_evidence": dict(self.classifier_evidence),
            "recovery_input": self.recovery_input.to_dict() if self.recovery_input else None,
            "created_at": self.created_at, "expires_at": self.expires_at, "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionDecisionRecord":
        item = _mapping(data)
        recovery = item.get("recovery_input")
        return cls(
            decision_id=str(item.get("decision_id") or _id("permdecision")),
            effect=_enum(PermissionEffect, item.get("effect") or "deny", "permission effect"),
            mode=_enum(PermissionMode, item.get("mode") or "default", "permission mode"),
            request_fingerprint=str(item.get("request_fingerprint") or ""), arguments_digest=str(item.get("arguments_digest") or ""),
            tool_use_id=str(item.get("tool_use_id") or ""), tool_identity=ToolIdentity.from_dict(_mapping(item.get("tool_identity"))),
            session_id=str(item.get("session_id") or ""), task_id=str(item.get("task_id") or ""), run_id=str(item.get("run_id") or ""),
            worker_request_id=str(item.get("worker_request_id") or ""), reason_code=str(item.get("reason_code") or ""), reason=str(item.get("reason") or ""),
            scope=PermissionScope.from_dict(_mapping(item.get("scope"))), matched_rule_ids=_strings(item.get("matched_rule_ids")),
            matched_rule_sources=tuple(_enum(PermissionRuleSource, value, "rule source") for value in item.get("matched_rule_sources", [])),
            request_id=str(item.get("request_id") or ""), rule_snapshot_id=str(item.get("rule_snapshot_id") or ""),
            mode_revision=int(item.get("mode_revision") or 0), hook_evidence=tuple(_mapping(value) for value in item.get("hook_evidence", [])),
            classifier_evidence=_mapping(item.get("classifier_evidence")),
            recovery_input=PermissionRecoveryInput.from_dict(_mapping(recovery)) if isinstance(recovery, Mapping) else None,
            created_at=str(item.get("created_at") or _now_iso()), expires_at=None if item.get("expires_at") is None else str(item.get("expires_at")),
            metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class PermissionRequestRecord:
    session_id: str
    task_id: str
    run_id: str
    tool_use_id: str
    tool_identity: ToolIdentity
    arguments_digest: str
    request_fingerprint: str
    scope: PermissionScope
    expires_at: str
    reason_code: str
    reason: str
    request_id: str = field(default_factory=lambda: _id("permreq"))
    worker_request_id: str = ""
    status: PermissionRequestStatus = PermissionRequestStatus.PENDING
    phase: PermissionRequestPhase = PermissionRequestPhase.CREATED
    revision: int = 0
    created_at: str = field(default_factory=_now_iso)
    delivered_at: str | None = None
    resolved_at: str | None = None
    resolved_by: str = ""
    resolution_channel: str = ""
    resolution_effect: PermissionEffect | None = None
    rule_snapshot_id: str = ""
    mode: PermissionMode = PermissionMode.DEFAULT
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            (
                self.request_id,
                self.session_id,
                self.task_id,
                self.run_id,
                self.tool_use_id,
                self.arguments_digest,
                self.request_fingerprint,
                self.expires_at,
                self.reason_code,
            )
        ):
            raise ValueError("permission request identity, expiry and reason fields are required")
        if self.revision < 0:
            raise ValueError("permission request revision cannot be negative")
        if self.phase in {PermissionRequestPhase.CREATED, PermissionRequestPhase.DELIVERED} and self.status != PermissionRequestStatus.PENDING:
            raise ValueError("created/delivered permission requests must remain pending")
        if self.phase == PermissionRequestPhase.RESOLVED and self.status not in {PermissionRequestStatus.APPROVED, PermissionRequestStatus.DENIED}:
            raise ValueError("resolved permission request must be approved or denied")

    @property
    def terminal(self) -> bool:
        return self.phase in {PermissionRequestPhase.RESOLVED, PermissionRequestPhase.EXPIRED, PermissionRequestPhase.CANCELLED, PermissionRequestPhase.ABORTED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "session_id": self.session_id, "task_id": self.task_id, "run_id": self.run_id,
            "worker_request_id": self.worker_request_id, "tool_use_id": self.tool_use_id, "tool_identity": self.tool_identity.to_dict(),
            "arguments_digest": self.arguments_digest, "request_fingerprint": self.request_fingerprint, "scope": self.scope.to_dict(),
            "expires_at": self.expires_at, "reason_code": self.reason_code, "reason": self.reason, "status": str(self.status),
            "phase": str(self.phase), "revision": self.revision, "created_at": self.created_at, "delivered_at": self.delivered_at,
            "resolved_at": self.resolved_at, "resolved_by": self.resolved_by, "resolution_channel": self.resolution_channel,
            "resolution_effect": str(self.resolution_effect) if self.resolution_effect else None,
            "rule_snapshot_id": self.rule_snapshot_id, "mode": str(self.mode), "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionRequestRecord":
        item = _mapping(data)
        effect = item.get("resolution_effect")
        return cls(
            request_id=str(item.get("request_id") or _id("permreq")), session_id=str(item.get("session_id") or ""),
            task_id=str(item.get("task_id") or ""), run_id=str(item.get("run_id") or ""), worker_request_id=str(item.get("worker_request_id") or ""),
            tool_use_id=str(item.get("tool_use_id") or ""), tool_identity=ToolIdentity.from_dict(_mapping(item.get("tool_identity"))),
            arguments_digest=str(item.get("arguments_digest") or ""), request_fingerprint=str(item.get("request_fingerprint") or ""),
            scope=PermissionScope.from_dict(_mapping(item.get("scope"))), expires_at=str(item.get("expires_at") or ""),
            reason_code=str(item.get("reason_code") or ""), reason=str(item.get("reason") or ""),
            status=_enum(PermissionRequestStatus, item.get("status") or "pending", "request status"),
            phase=_enum(PermissionRequestPhase, item.get("phase") or "created", "request phase"), revision=int(item.get("revision") or 0),
            created_at=str(item.get("created_at") or _now_iso()), delivered_at=None if item.get("delivered_at") is None else str(item.get("delivered_at")),
            resolved_at=None if item.get("resolved_at") is None else str(item.get("resolved_at")), resolved_by=str(item.get("resolved_by") or ""),
            resolution_channel=str(item.get("resolution_channel") or ""), resolution_effect=_enum(PermissionEffect, effect, "resolution effect") if effect else None,
            rule_snapshot_id=str(item.get("rule_snapshot_id") or ""), mode=_enum(PermissionMode, item.get("mode") or "default", "permission mode"),
            metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class PermissionResolutionResponse:
    request_id: str
    session_id: str
    tool_use_id: str
    tool_identity: ToolIdentity
    arguments_digest: str
    request_fingerprint: str
    scope: PermissionScope
    effect: PermissionEffect
    actor_id: str
    expected_revision: int
    channel: str = "user"
    create_rule: bool = False
    rule_scope: PermissionScope | None = None
    reason: str = ""
    idempotency_key: str = ""
    responded_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.effect == PermissionEffect.ASK:
            raise ValueError("a permission response must allow or deny")
        if not all(
            (
                self.request_id,
                self.session_id,
                self.tool_use_id,
                self.arguments_digest,
                self.request_fingerprint,
                self.actor_id,
            )
        ):
            raise ValueError("permission response exact identity and actor fields are required")
        if self.expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "session_id": self.session_id, "tool_use_id": self.tool_use_id,
            "tool_identity": self.tool_identity.to_dict(), "arguments_digest": self.arguments_digest,
            "request_fingerprint": self.request_fingerprint, "scope": self.scope.to_dict(), "effect": str(self.effect),
            "actor_id": self.actor_id, "expected_revision": self.expected_revision, "channel": self.channel,
            "create_rule": self.create_rule, "rule_scope": self.rule_scope.to_dict() if self.rule_scope else None,
            "reason": self.reason, "idempotency_key": self.idempotency_key, "responded_at": self.responded_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionResolutionResponse":
        item = _mapping(data)
        rule_scope = item.get("rule_scope")
        return cls(
            request_id=str(item.get("request_id") or ""), session_id=str(item.get("session_id") or ""),
            tool_use_id=str(item.get("tool_use_id") or ""), tool_identity=ToolIdentity.from_dict(_mapping(item.get("tool_identity"))),
            arguments_digest=str(item.get("arguments_digest") or ""), request_fingerprint=str(item.get("request_fingerprint") or ""),
            scope=PermissionScope.from_dict(_mapping(item.get("scope"))), effect=_enum(PermissionEffect, item.get("effect") or "deny", "permission effect"),
            actor_id=str(item.get("actor_id") or ""), expected_revision=int(item.get("expected_revision") or 0),
            channel=str(item.get("channel") or "user"), create_rule=bool(item.get("create_rule", False)),
            rule_scope=PermissionScope.from_dict(_mapping(rule_scope)) if isinstance(rule_scope, Mapping) else None,
            reason=str(item.get("reason") or ""), idempotency_key=str(item.get("idempotency_key") or ""),
            responded_at=str(item.get("responded_at") or _now_iso()), metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class PermissionExecutionGrant:
    decision_id: str
    session_id: str
    tool_use_id: str
    tool_identity: ToolIdentity
    arguments_digest: str
    request_fingerprint: str
    scope: PermissionScope
    grant_id: str = field(default_factory=lambda: _id("permgrant"))
    request_id: str = ""
    issued_at: str = field(default_factory=_now_iso)
    expires_at: str | None = None
    single_use: bool = True
    consumed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            (
                self.grant_id,
                self.decision_id,
                self.session_id,
                self.tool_use_id,
                self.arguments_digest,
                self.request_fingerprint,
            )
        ):
            raise ValueError("permission execution grant identity fields are required")

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    def valid_for(self, request: PermissionEvaluationRequest, *, at: datetime | None = None) -> bool:
        if self.single_use and self.consumed:
            return False
        if self.expires_at:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            moment = at or datetime.now(timezone.utc)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if moment >= expiry:
                return False
        return (
            self.session_id == request.session_id and self.tool_use_id == request.tool_use_id
            and self.tool_identity == request.tool_identity and self.arguments_digest == request.arguments_digest
            and self.request_fingerprint == request.request_fingerprint and self.scope.contains(request, at=at)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id, "decision_id": self.decision_id, "request_id": self.request_id,
            "session_id": self.session_id, "tool_use_id": self.tool_use_id, "tool_identity": self.tool_identity.to_dict(),
            "arguments_digest": self.arguments_digest, "request_fingerprint": self.request_fingerprint,
            "scope": self.scope.to_dict(), "issued_at": self.issued_at, "expires_at": self.expires_at,
            "single_use": self.single_use, "consumed_at": self.consumed_at, "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionExecutionGrant":
        item = _mapping(data)
        return cls(
            grant_id=str(item.get("grant_id") or _id("permgrant")), decision_id=str(item.get("decision_id") or ""),
            request_id=str(item.get("request_id") or ""), session_id=str(item.get("session_id") or ""),
            tool_use_id=str(item.get("tool_use_id") or ""), tool_identity=ToolIdentity.from_dict(_mapping(item.get("tool_identity"))),
            arguments_digest=str(item.get("arguments_digest") or ""), request_fingerprint=str(item.get("request_fingerprint") or ""),
            scope=PermissionScope.from_dict(_mapping(item.get("scope"))), issued_at=str(item.get("issued_at") or _now_iso()),
            expires_at=None if item.get("expires_at") is None else str(item.get("expires_at")), single_use=bool(item.get("single_use", True)),
            consumed_at=None if item.get("consumed_at") is None else str(item.get("consumed_at")), metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class DenialTrackingState:
    consecutive_denials: int = 0
    total_denials: int = 0
    consecutive_limit: int = 3
    total_limit: int = 20
    last_denial_at: str | None = None
    last_reason_code: str = ""

    @property
    def limit_reached(self) -> bool:
        return self.consecutive_denials >= self.consecutive_limit or self.total_denials >= self.total_limit

    def record_denial(self, reason_code: str, *, at: str | None = None) -> "DenialTrackingState":
        return replace(self, consecutive_denials=self.consecutive_denials + 1, total_denials=self.total_denials + 1,
                       last_denial_at=at or _now_iso(), last_reason_code=reason_code)

    def record_allow(self) -> "DenialTrackingState":
        return replace(self, consecutive_denials=0)

    def to_dict(self) -> dict[str, Any]:
        return {"consecutive_denials": self.consecutive_denials, "total_denials": self.total_denials,
                "consecutive_limit": self.consecutive_limit, "total_limit": self.total_limit,
                "last_denial_at": self.last_denial_at, "last_reason_code": self.last_reason_code,
                "limit_reached": self.limit_reached}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DenialTrackingState":
        item = _mapping(data)
        return cls(consecutive_denials=int(item.get("consecutive_denials") or 0), total_denials=int(item.get("total_denials") or 0),
                   consecutive_limit=int(item.get("consecutive_limit") or 3), total_limit=int(item.get("total_limit") or 20),
                   last_denial_at=None if item.get("last_denial_at") is None else str(item.get("last_denial_at")),
                   last_reason_code=str(item.get("last_reason_code") or ""))


def _path_within(value: str | Path, prefix: str | Path) -> bool:
    if not str(value):
        return False
    try:
        Path(value).resolve().relative_to(Path(prefix).resolve())
        return True
    except (ValueError, OSError):
        return False


def _domain_within(value: str, domain: str) -> bool:
    expected = domain.lower().rstrip(".")
    return bool(value and expected and (value == expected or value.endswith(f".{expected}")))
