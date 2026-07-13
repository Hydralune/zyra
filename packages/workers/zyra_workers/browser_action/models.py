from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

from zyra_core import now_iso, to_jsonable


def canonical_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(canonical_json(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


class ActionAccess(StrEnum):
    READ_ONLY = "read_only"
    BROWSER_MUTATION = "browser_mutation"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    NETWORK = "network"
    SCRIPT = "script"
    CLIPBOARD = "clipboard"
    TARGET_CONTROL = "target_control"


class ActionRisk(StrEnum):
    PASSIVE = "passive"
    LOW = "low"
    GUARDED = "guarded"
    SENSITIVE = "sensitive"
    HIGH = "high"
    PROHIBITED = "prohibited"


class PermissionDisposition(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ActionPhase(StrEnum):
    RECEIVED = "received"
    SCHEMA_VALIDATED = "schema_validated"
    SECRET_BOUND = "secret_bound"
    NETWORK_CHECKED = "network_checked"
    FILE_CHECKED = "file_checked"
    SELECTOR_CHECKED = "selector_checked"
    GEOMETRY_CHECKED = "geometry_checked"
    HOOKS_CHECKED = "hooks_checked"
    PERMISSION_CHECKED = "permission_checked"
    GRANT_CONSUMED = "grant_consumed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class ActionFailureKind(StrEnum):
    UNKNOWN_ACTION = "unknown_action"
    SCHEMA = "schema"
    SECRET_SCOPE = "secret_scope"
    NETWORK = "network"
    REDIRECT = "redirect"
    DNS_REBINDING = "dns_rebinding"
    FILE_CONTAINMENT = "file_containment"
    SELECTOR_STALE = "selector_stale"
    SELECTOR_IDENTITY = "selector_identity"
    OCCLUDED = "occluded"
    NOT_INTERACTIVE = "not_interactive"
    PERMISSION = "permission"
    HOOK = "hook"
    DEADLINE = "deadline"
    EXECUTION = "execution"
    RESULT_BUDGET = "result_budget"
    INTERNAL = "internal"


class ActionTargetKind(StrEnum):
    NONE = "none"
    PAGE = "page"
    ELEMENT = "element"
    FILE_INPUT = "file_input"
    SELECT = "select"
    FRAME = "frame"
    TARGET = "target"


class SchemaValueType(StrEnum):
    ANY = "any"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"


@dataclass(frozen=True, slots=True)
class ArgumentRule:
    name: str
    value_type: SchemaValueType = SchemaValueType.ANY
    required: bool = False
    default: Any = None
    description: str = ""
    enum: tuple[Any, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str = ""
    item_type: SchemaValueType = SchemaValueType.ANY
    min_items: int | None = None
    max_items: int | None = None
    secret_capable: bool = False
    identifier: bool = False
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid browser action argument name: {self.name!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", str(self.description).strip())
        object.__setattr__(self, "enum", tuple(self.enum))
        object.__setattr__(self, "aliases", tuple(dict.fromkeys(str(item).strip() for item in self.aliases if str(item).strip())))
        if self.minimum is not None and not math.isfinite(float(self.minimum)):
            raise ValueError("argument minimum must be finite")
        if self.maximum is not None and not math.isfinite(float(self.maximum)):
            raise ValueError("argument maximum must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("argument minimum exceeds maximum")
        if self.min_length is not None and self.min_length < 0:
            raise ValueError("argument min_length cannot be negative")
        if self.max_length is not None and self.max_length < 0:
            raise ValueError("argument max_length cannot be negative")
        if self.min_length is not None and self.max_length is not None and self.min_length > self.max_length:
            raise ValueError("argument min_length exceeds max_length")
        if self.min_items is not None and self.min_items < 0:
            raise ValueError("argument min_items cannot be negative")
        if self.max_items is not None and self.max_items < 0:
            raise ValueError("argument max_items cannot be negative")
        if self.min_items is not None and self.max_items is not None and self.min_items > self.max_items:
            raise ValueError("argument min_items exceeds max_items")
        if self.pattern:
            re.compile(self.pattern)

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def schema(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.value_type != SchemaValueType.ANY:
            result["type"] = str(self.value_type)
        if self.description:
            result["description"] = self.description
        if self.enum:
            result["enum"] = list(self.enum)
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.min_length is not None:
            result["minLength"] = self.min_length
        if self.max_length is not None:
            result["maxLength"] = self.max_length
        if self.pattern:
            result["pattern"] = self.pattern
        if self.value_type == SchemaValueType.ARRAY and self.item_type != SchemaValueType.ANY:
            result["items"] = {"type": str(self.item_type)}
        if self.min_items is not None:
            result["minItems"] = self.min_items
        if self.max_items is not None:
            result["maxItems"] = self.max_items
        if self.default is not None:
            result["default"] = to_jsonable(self.default)
        if self.secret_capable:
            result["x-zyra-secret-capable"] = True
        if self.identifier:
            result["x-zyra-identifier"] = True
        if self.aliases:
            result["x-zyra-aliases"] = list(self.aliases)
        return result


@dataclass(frozen=True, slots=True)
class ActionSource:
    repository: str
    path: str
    symbol: str
    role: str
    strategy: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.repository.strip() or not self.path.strip() or not self.symbol.strip():
            raise ValueError("action source requires repository, path and symbol")
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    name: str
    source_name: str
    description: str
    arguments: tuple[ArgumentRule, ...]
    aliases: tuple[str, ...] = ()
    access: frozenset[ActionAccess] = frozenset()
    base_risk: ActionRisk = ActionRisk.LOW
    target_kind: ActionTargetKind = ActionTargetKind.NONE
    permission_required: bool = False
    selector_required: bool = False
    terminates_sequence: bool = False
    result_budget_chars: int = 20_000
    timeout_seconds: float = 30.0
    sources: tuple[ActionSource, ...] = ()
    tags: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"invalid browser action name: {self.name!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "source_name", str(self.source_name).strip() or name)
        object.__setattr__(self, "description", str(self.description).strip())
        object.__setattr__(self, "arguments", tuple(self.arguments))
        object.__setattr__(self, "aliases", tuple(dict.fromkeys(str(item).strip() for item in self.aliases if str(item).strip())))
        object.__setattr__(self, "access", frozenset(self.access))
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "tags", tuple(dict.fromkeys(str(item) for item in self.tags if str(item))))
        names: set[str] = set()
        for rule in self.arguments:
            for candidate in rule.all_names:
                if candidate in names:
                    raise ValueError(f"duplicate argument or alias {candidate!r} in {name}")
                names.add(candidate)
        if self.result_budget_chars < 256:
            raise ValueError("browser action result budget is too small")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("browser action timeout must be finite and positive")
        if self.schema_version < 1:
            raise ValueError("browser action schema version must be positive")

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self.arguments if rule.required)

    @property
    def optional_arguments(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self.arguments if not rule.required)

    @property
    def read_only(self) -> bool:
        mutating = {
            ActionAccess.BROWSER_MUTATION,
            ActionAccess.EXTERNAL_SIDE_EFFECT,
            ActionAccess.FILE_WRITE,
            ActionAccess.SCRIPT,
            ActionAccess.CLIPBOARD,
            ActionAccess.TARGET_CONTROL,
        }
        return ActionAccess.READ_ONLY in self.access and not bool(self.access.intersection(mutating))

    @property
    def identity(self) -> str:
        return digest_value(self.to_schema())

    def argument(self, name: str) -> ArgumentRule | None:
        for rule in self.arguments:
            if name in rule.all_names:
                return rule
        return None

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {rule.name: rule.schema() for rule in self.arguments},
            "required": list(self.required_arguments),
            "x-zyra-browser-action": self.name,
            "x-zyra-source-action": self.source_name,
            "x-zyra-access": sorted(str(item) for item in self.access),
            "x-zyra-risk": str(self.base_risk),
            "x-zyra-selector-required": self.selector_required,
            "x-zyra-permission-required": self.permission_required,
            "x-zyra-target-kind": str(self.target_kind),
            "x-zyra-schema-version": self.schema_version,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "action": self.name,
            "source_action": self.source_name,
            "description": self.description,
            "implemented": True,
            "zyra_required_arguments": list(self.required_arguments),
            "zyra_optional_arguments": list(self.optional_arguments),
            "source_required_fields": list(self.required_arguments),
            "source_optional_fields": list(self.optional_arguments),
            "aliases": list(self.aliases),
            "access": sorted(str(item) for item in self.access),
            "risk": str(self.base_risk),
            "target_kind": str(self.target_kind),
            "permission_required": self.permission_required,
            "selector_required": self.selector_required,
            "terminates_sequence": self.terminates_sequence,
            "result_budget_chars": self.result_budget_chars,
            "timeout_seconds": self.timeout_seconds,
            "schema": self.to_schema(),
            "schema_identity": self.identity,
            "sources": [item.to_dict() for item in self.sources],
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    path: str = ""
    expected: Any = None
    actual: Any = None
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PlanValidationIssue:
    step_index: int
    action: str
    reason: str
    path: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", dict(self.details))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SelectorBinding:
    browser_session_id: str
    target_id: str
    cdp_session_id: str
    selector_revision_id: str
    selector_generation: int
    selector_ref: str
    backend_node_id: int
    frame_id: str = ""
    document_loader_id: str = ""
    target_generation: int = 0
    cdp_generation: int = 0
    capture_digest: str = ""

    def __post_init__(self) -> None:
        required = (
            self.browser_session_id,
            self.target_id,
            self.cdp_session_id,
            self.selector_revision_id,
            self.selector_ref,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("selector binding identity is incomplete")
        if min(self.selector_generation, self.backend_node_id, self.target_generation, self.cdp_generation) <= 0:
            raise ValueError("selector binding generations and backend node id must be positive")

    @property
    def identity_digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionIdentity:
    run_id: str
    task_id: str
    worker_request_id: str
    session_id: str
    browser_session_id: str
    step_index: int
    node_id: str = ""
    turn_id: str = ""
    action_id: str = ""

    def __post_init__(self) -> None:
        if not all(str(value).strip() for value in (self.run_id, self.task_id, self.worker_request_id, self.session_id)):
            raise ValueError("browser action identity is incomplete")
        if self.step_index < 1:
            raise ValueError("browser action step index must be positive")
        if not self.action_id:
            object.__setattr__(
                self,
                "action_id",
                stable_id("braction", self.run_id, self.task_id, self.worker_request_id, self.step_index),
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionRequest:
    identity: ActionIdentity
    action: str
    arguments: Mapping[str, Any]
    backend: str
    current_url: str = ""
    target_url: str = ""
    selector: SelectorBinding | None = None
    deadline_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    received_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not str(self.action).strip() or not str(self.backend).strip():
            raise ValueError("browser action and backend are required")
        object.__setattr__(self, "action", str(self.action).strip())
        object.__setattr__(self, "backend", str(self.backend).strip())
        object.__setattr__(self, "arguments", dict(self.arguments))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def arguments_digest(self) -> str:
        return digest_value(self.arguments)

    @property
    def request_digest(self) -> str:
        return digest_value(
            {
                "identity": self.identity.to_dict(),
                "action": self.action,
                "arguments": self.arguments,
                "backend": self.backend,
                "current_url": self.current_url,
                "target_url": self.target_url,
                "selector": self.selector.to_dict() if self.selector else None,
            }
        )

    def with_arguments(self, arguments: Mapping[str, Any], **metadata: Any) -> "ActionRequest":
        return replace(self, arguments=dict(arguments), metadata={**dict(self.metadata), **metadata})

    def public_dict(self, *, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "action": self.action,
            "arguments": dict(arguments),
            "arguments_digest": self.arguments_digest,
            "request_digest": self.request_digest,
            "backend": self.backend,
            "current_url": self.current_url,
            "target_url": self.target_url,
            "selector": self.selector.to_dict() if self.selector else None,
            "deadline_at": self.deadline_at,
            "metadata": dict(self.metadata),
            "received_at": self.received_at,
        }


@dataclass(frozen=True, slots=True)
class ActionRiskAssessment:
    action: str
    risk: ActionRisk
    permission: PermissionDisposition
    capabilities: tuple[str, ...]
    risk_tags: tuple[str, ...]
    safety_flags: tuple[str, ...]
    reasons: tuple[str, ...]
    policy_version: str

    @property
    def allowed_without_prompt(self) -> bool:
        return self.permission == PermissionDisposition.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionPreflightReceipt:
    receipt_id: str
    action_id: str
    request_digest: str
    registry_digest: str
    arguments_digest: str
    public_arguments: Mapping[str, Any]
    execution_arguments: Mapping[str, Any] = field(repr=False)
    definition: ActionDefinition = field(repr=False, compare=False)
    assessment: ActionRiskAssessment = field(repr=False)
    selector_binding: SelectorBinding | None = None
    network_receipt_id: str = ""
    file_receipt_id: str = ""
    secret_receipt_id: str = ""
    clipboard_receipt_id: str = ""
    form_receipt_id: str = ""
    hook_receipt_ids: tuple[str, ...] = ()
    permission_decision_id: str = ""
    permission_request_id: str = ""
    permission_tool_use_id: str = ""
    permission_grant_consumed: bool = False
    expires_at: str = ""
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.receipt_id or not self.action_id or not self.request_digest or not self.registry_digest:
            raise ValueError("browser action preflight receipt identity is incomplete")
        object.__setattr__(self, "public_arguments", dict(self.public_arguments))
        object.__setattr__(self, "execution_arguments", dict(self.execution_arguments))
        object.__setattr__(self, "hook_receipt_ids", tuple(self.hook_receipt_ids))

    @property
    def authorizes_execution(self) -> bool:
        return self.assessment.permission != PermissionDisposition.DENY and self.permission_grant_consumed

    def with_permission(
        self,
        *,
        decision_id: str,
        request_id: str,
        tool_use_id: str,
        consumed: bool,
    ) -> "ActionPreflightReceipt":
        return replace(
            self,
            permission_decision_id=decision_id,
            permission_request_id=request_id,
            permission_tool_use_id=tool_use_id,
            permission_grant_consumed=consumed,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "request_digest": self.request_digest,
            "registry_digest": self.registry_digest,
            "arguments_digest": self.arguments_digest,
            "public_arguments": dict(self.public_arguments),
            "definition": self.definition.public_dict(),
            "assessment": self.assessment.to_dict(),
            "selector_binding": self.selector_binding.to_dict() if self.selector_binding else None,
            "network_receipt_id": self.network_receipt_id,
            "file_receipt_id": self.file_receipt_id,
            "secret_receipt_id": self.secret_receipt_id,
            "clipboard_receipt_id": self.clipboard_receipt_id,
            "form_receipt_id": self.form_receipt_id,
            "hook_receipt_ids": list(self.hook_receipt_ids),
            "permission_decision_id": self.permission_decision_id,
            "permission_request_id": self.permission_request_id,
            "permission_tool_use_id": self.permission_tool_use_id,
            "permission_grant_consumed": self.permission_grant_consumed,
            "authorizes_execution": self.authorizes_execution,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ActionExecutionResult:
    action_id: str
    ok: bool
    summary: str
    output: Mapping[str, Any] = field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    error_code: str = ""
    error_message: str = ""
    failure_kind: ActionFailureKind | None = None
    side_effect_count: int = 0
    network_effect_count: int = 0
    file_effect_count: int = 0
    cdp_effect_count: int = 0
    completed_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", dict(self.output))
        object.__setattr__(self, "artifact_ids", tuple(self.artifact_ids))
        if min(self.side_effect_count, self.network_effect_count, self.file_effect_count, self.cdp_effect_count) < 0:
            raise ValueError("browser action effect counters cannot be negative")
        if self.ok and self.error_code:
            raise ValueError("successful browser action cannot contain error_code")

    def public_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "ok": self.ok,
            "summary": self.summary,
            "output": dict(self.output),
            "artifact_ids": list(self.artifact_ids),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "failure_kind": str(self.failure_kind) if self.failure_kind else "",
            "side_effect_count": self.side_effect_count,
            "network_effect_count": self.network_effect_count,
            "file_effect_count": self.file_effect_count,
            "cdp_effect_count": self.cdp_effect_count,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class ActionTransition:
    action_id: str
    sequence: int
    phase: ActionPhase
    cause_id: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("browser action transition sequence must be positive")
        object.__setattr__(self, "details", dict(self.details))

    @property
    def transition_id(self) -> str:
        return stable_id("brtransition", self.action_id, self.sequence, self.phase, self.cause_id)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "transition_id": self.transition_id}


def string_rule(name: str, *, required: bool = False, **kwargs: Any) -> ArgumentRule:
    return ArgumentRule(name=name, value_type=SchemaValueType.STRING, required=required, **kwargs)


def integer_rule(name: str, *, required: bool = False, **kwargs: Any) -> ArgumentRule:
    return ArgumentRule(name=name, value_type=SchemaValueType.INTEGER, required=required, **kwargs)


def number_rule(name: str, *, required: bool = False, **kwargs: Any) -> ArgumentRule:
    return ArgumentRule(name=name, value_type=SchemaValueType.NUMBER, required=required, **kwargs)


def boolean_rule(name: str, *, required: bool = False, **kwargs: Any) -> ArgumentRule:
    return ArgumentRule(name=name, value_type=SchemaValueType.BOOLEAN, required=required, **kwargs)


def array_rule(name: str, *, required: bool = False, item_type: SchemaValueType = SchemaValueType.ANY, **kwargs: Any) -> ArgumentRule:
    return ArgumentRule(name=name, value_type=SchemaValueType.ARRAY, item_type=item_type, required=required, **kwargs)


def object_rule(name: str, *, required: bool = False, **kwargs: Any) -> ArgumentRule:
    return ArgumentRule(name=name, value_type=SchemaValueType.OBJECT, required=required, **kwargs)


def sequence_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()
