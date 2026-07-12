from __future__ import annotations

"""Typed, bounded child-to-parent yield protocol.

Oh My Pi's task executor correctly treats an explicit yield as different from
arbitrary final model text.  This is a Zyra-owned implementation of that
semantic boundary.  It validates deterministic schemas, assembles incremental
parts, records usage/progress and requires one terminal yield before fan-in.
"""

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import new_id, now_iso


class TypedYieldError(RuntimeError):
    pass


class TypedYieldDisabled(TypedYieldError):
    pass


class TypedYieldValidationError(TypedYieldError, ValueError):
    def __init__(self, findings: Sequence["YieldFinding"]) -> None:
        self.findings = tuple(findings)
        super().__init__("; ".join(item.message for item in self.findings if item.blocking))


class TypedYieldConflict(TypedYieldError):
    pass


class YieldKind(StrEnum):
    PROGRESS = "progress"
    PARTIAL = "partial"
    TERMINAL = "terminal"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    DETACHED = "detached"

    @property
    def terminal(self) -> bool:
        return self in {
            YieldKind.TERMINAL,
            YieldKind.FAILURE,
            YieldKind.CANCELLED,
        }


class YieldMergeStrategy(StrEnum):
    REPLACE = "replace"
    APPEND = "append"
    OBJECT_MERGE = "object_merge"
    LIST_EXTEND = "list_extend"
    FIRST_WINS = "first_wins"


class YieldSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class YieldFinding:
    code: str
    message: str
    path: str = "$"
    severity: YieldSeverity = YieldSeverity.ERROR
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity.value,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class YieldBudget:
    maximum_parts: int = 128
    maximum_total_chars: int = 256_000
    maximum_summary_chars: int = 16_000
    maximum_data_depth: int = 16
    maximum_object_keys: int = 512
    maximum_list_items: int = 4096
    maximum_artifact_refs: int = 128
    maximum_evidence_refs: int = 256
    maximum_warnings: int = 128

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")

    def to_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "YieldBudget":
        raw = value or {}
        defaults = cls()
        return cls(**{
            name: max(1, int(raw.get(name) or getattr(defaults, name)))
            for name in defaults.__dataclass_fields__
        })


@dataclass(frozen=True, slots=True)
class YieldContract:
    contract_id: str
    schema: Mapping[str, Any]
    schema_version: str = "1"
    required_terminal: bool = True
    merge_strategy: YieldMergeStrategy = YieldMergeStrategy.REPLACE
    budget: YieldBudget = field(default_factory=YieldBudget)
    allowed_kinds: tuple[YieldKind, ...] = (
        YieldKind.PROGRESS,
        YieldKind.PARTIAL,
        YieldKind.TERMINAL,
        YieldKind.FAILURE,
        YieldKind.CANCELLED,
        YieldKind.DETACHED,
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.contract_id.strip():
            raise ValueError("contract_id is required")
        if not isinstance(self.schema, Mapping):
            raise ValueError("yield schema must be an object")
        object.__setattr__(self, "schema", _json_mapping(self.schema))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))
        object.__setattr__(self, "allowed_kinds", tuple(dict.fromkeys(self.allowed_kinds)))

    @property
    def digest(self) -> str:
        return _digest({
            "contract_id": self.contract_id,
            "schema": self.schema,
            "schema_version": self.schema_version,
            "required_terminal": self.required_terminal,
            "merge_strategy": self.merge_strategy.value,
            "budget": self.budget.to_dict(),
            "allowed_kinds": [item.value for item in self.allowed_kinds],
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "schema": copy.deepcopy(dict(self.schema)),
            "schema_version": self.schema_version,
            "required_terminal": self.required_terminal,
            "merge_strategy": self.merge_strategy.value,
            "budget": self.budget.to_dict(),
            "allowed_kinds": [item.value for item in self.allowed_kinds],
            "metadata": _safe_mapping(self.metadata),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "YieldContract":
        return cls(
            contract_id=str(value.get("contract_id") or ""),
            schema=_json_mapping(value.get("schema")),
            schema_version=str(value.get("schema_version") or "1"),
            required_terminal=bool(value.get("required_terminal", True)),
            merge_strategy=YieldMergeStrategy(str(value.get("merge_strategy") or YieldMergeStrategy.REPLACE.value)),
            budget=YieldBudget.from_dict(_mapping_or_none(value.get("budget"))),
            allowed_kinds=tuple(
                YieldKind(str(item)) for item in value.get("allowed_kinds") or tuple(item.value for item in YieldKind)
            ),
            metadata=_safe_mapping(value.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class TypedYield:
    yield_id: str
    task_id: str
    parent_task_id: str
    execution_ref: str
    attempt: int
    sequence: int
    kind: YieldKind
    summary: str
    data: Any = None
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    progress_current: int = 0
    progress_total: int = 0
    usage: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    retryable: bool = False
    causation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.yield_id or not self.task_id or not self.parent_task_id:
            raise ValueError("yield_id, task_id and parent_task_id are required")
        if self.attempt <= 0 or self.sequence <= 0:
            raise ValueError("attempt and sequence must be positive")
        if self.progress_current < 0 or self.progress_total < 0:
            raise ValueError("progress values cannot be negative")
        if self.progress_total and self.progress_current > self.progress_total:
            raise ValueError("progress_current cannot exceed progress_total")
        object.__setattr__(self, "summary", str(self.summary))
        object.__setattr__(self, "artifact_refs", _unique_strings(self.artifact_refs))
        object.__setattr__(self, "evidence_refs", _unique_strings(self.evidence_refs))
        object.__setattr__(self, "warnings", _unique_strings(self.warnings))
        object.__setattr__(self, "usage", _safe_mapping(self.usage))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    @property
    def digest(self) -> str:
        return _digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "yield_id": self.yield_id,
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "execution_ref": self.execution_ref,
            "attempt": self.attempt,
            "sequence": self.sequence,
            "kind": self.kind.value,
            "summary": self.summary,
            "data": _json_value(self.data),
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "warnings": list(self.warnings),
            "progress_current": self.progress_current,
            "progress_total": self.progress_total,
            "usage": _safe_mapping(self.usage),
            "error_code": self.error_code,
            "retryable": self.retryable,
            "causation_id": self.causation_id,
            "metadata": _safe_mapping(self.metadata),
            "created_at": self.created_at,
        }
        if include_digest:
            value["digest"] = _digest(value)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TypedYield":
        return cls(
            yield_id=str(value.get("yield_id") or new_id("yield")),
            task_id=str(value.get("task_id") or ""),
            parent_task_id=str(value.get("parent_task_id") or ""),
            execution_ref=str(value.get("execution_ref") or ""),
            attempt=max(1, int(value.get("attempt") or 1)),
            sequence=max(1, int(value.get("sequence") or 1)),
            kind=YieldKind(str(value.get("kind") or YieldKind.PARTIAL.value)),
            summary=str(value.get("summary") or ""),
            data=copy.deepcopy(value.get("data")),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
            warnings=tuple(str(item) for item in value.get("warnings") or ()),
            progress_current=max(0, int(value.get("progress_current") or 0)),
            progress_total=max(0, int(value.get("progress_total") or 0)),
            usage=_safe_mapping(value.get("usage")),
            error_code=str(value.get("error_code") or ""),
            retryable=bool(value.get("retryable", False)),
            causation_id=str(value.get("causation_id") or ""),
            metadata=_safe_mapping(value.get("metadata")),
            created_at=str(value.get("created_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class YieldAssembly:
    task_id: str
    contract_id: str
    contract_digest: str
    execution_ref: str
    attempt: int
    parts: tuple[TypedYield, ...]
    terminal_yield_id: str = ""
    assembled_data: Any = None
    summary: str = ""
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    total_chars: int = 0
    revision: int = 0
    completed: bool = False
    failed: bool = False
    cancelled: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def next_sequence(self) -> int:
        return len(self.parts) + 1

    @property
    def terminal(self) -> TypedYield | None:
        return next((item for item in self.parts if item.yield_id == self.terminal_yield_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "contract_id": self.contract_id,
            "contract_digest": self.contract_digest,
            "execution_ref": self.execution_ref,
            "attempt": self.attempt,
            "parts": [item.to_dict() for item in self.parts],
            "terminal_yield_id": self.terminal_yield_id,
            "assembled_data": _json_value(self.assembled_data),
            "summary": self.summary,
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "warnings": list(self.warnings),
            "total_chars": self.total_chars,
            "revision": self.revision,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "YieldAssembly":
        return cls(
            task_id=str(value.get("task_id") or ""),
            contract_id=str(value.get("contract_id") or ""),
            contract_digest=str(value.get("contract_digest") or ""),
            execution_ref=str(value.get("execution_ref") or ""),
            attempt=max(1, int(value.get("attempt") or 1)),
            parts=tuple(
                TypedYield.from_dict(item) for item in value.get("parts") or () if isinstance(item, Mapping)
            ),
            terminal_yield_id=str(value.get("terminal_yield_id") or ""),
            assembled_data=copy.deepcopy(value.get("assembled_data")),
            summary=str(value.get("summary") or ""),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
            warnings=tuple(str(item) for item in value.get("warnings") or ()),
            total_chars=max(0, int(value.get("total_chars") or 0)),
            revision=max(0, int(value.get("revision") or 0)),
            completed=bool(value.get("completed", False)),
            failed=bool(value.get("failed", False)),
            cancelled=bool(value.get("cancelled", False)),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )


class JsonSchemaSubsetValidator:
    """Deterministic validator for the JSON Schema subset used by agents."""

    def validate(self, value: Any, schema: Mapping[str, Any], *, budget: YieldBudget) -> tuple[YieldFinding, ...]:
        findings: list[YieldFinding] = []
        self._visit(value, schema, path="$", depth=0, budget=budget, findings=findings)
        return tuple(findings)

    def _visit(
        self,
        value: Any,
        schema: Mapping[str, Any],
        *,
        path: str,
        depth: int,
        budget: YieldBudget,
        findings: list[YieldFinding],
    ) -> None:
        if depth > budget.maximum_data_depth:
            findings.append(YieldFinding("yield_depth_exceeded", "yield data exceeds maximum depth", path))
            return
        if bool(schema.get("nullable")) and value is None:
            return
        if "const" in schema and value != schema["const"]:
            findings.append(YieldFinding("yield_const_mismatch", "value does not match const", path))
        enum_values = schema.get("enum")
        if isinstance(enum_values, Sequence) and not isinstance(enum_values, (str, bytes)) and value not in enum_values:
            findings.append(YieldFinding("yield_enum_mismatch", "value is not in enum", path))
        any_of = schema.get("anyOf")
        if isinstance(any_of, Sequence) and not isinstance(any_of, (str, bytes)):
            alternatives = []
            for item in any_of:
                if not isinstance(item, Mapping):
                    continue
                candidate: list[YieldFinding] = []
                self._visit(value, item, path=path, depth=depth, budget=budget, findings=candidate)
                alternatives.append(candidate)
            if alternatives and all(any(f.blocking for f in candidate) for candidate in alternatives):
                findings.append(YieldFinding("yield_anyof_mismatch", "value matches no anyOf branch", path))
                return
        expected = schema.get("type")
        if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes)):
            if any(self._matches_type(value, str(item)) for item in expected):
                selected = next((str(item) for item in expected if self._matches_type(value, str(item))), "")
            else:
                findings.append(YieldFinding("yield_type_mismatch", f"expected one of {list(expected)}", path))
                return
        else:
            selected = str(expected or self._infer_type(value))
            if expected and not self._matches_type(value, selected):
                findings.append(YieldFinding("yield_type_mismatch", f"expected {selected}", path))
                return
        if selected == "object" and isinstance(value, Mapping):
            self._validate_object(value, schema, path=path, depth=depth, budget=budget, findings=findings)
        elif selected == "array" and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            self._validate_array(value, schema, path=path, depth=depth, budget=budget, findings=findings)
        elif selected == "string" and isinstance(value, str):
            minimum = int(schema.get("minLength") or 0)
            maximum = int(schema.get("maxLength") or budget.maximum_total_chars)
            if len(value) < minimum:
                findings.append(YieldFinding("yield_string_too_short", f"minimum length is {minimum}", path))
            if len(value) > maximum:
                findings.append(YieldFinding("yield_string_too_long", f"maximum length is {maximum}", path))
        elif selected in {"number", "integer"} and isinstance(value, (int, float)) and not isinstance(value, bool):
            if schema.get("minimum") is not None and value < schema["minimum"]:
                findings.append(YieldFinding("yield_number_too_small", "value is below minimum", path))
            if schema.get("maximum") is not None and value > schema["maximum"]:
                findings.append(YieldFinding("yield_number_too_large", "value is above maximum", path))

    def _validate_object(
        self,
        value: Mapping[str, Any],
        schema: Mapping[str, Any],
        *,
        path: str,
        depth: int,
        budget: YieldBudget,
        findings: list[YieldFinding],
    ) -> None:
        if len(value) > budget.maximum_object_keys:
            findings.append(YieldFinding("yield_object_too_large", "object has too many keys", path))
            return
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        required = {str(item) for item in schema.get("required") or ()}
        for name in sorted(required):
            if name not in value:
                findings.append(YieldFinding("yield_required_missing", f"required property {name!r} is missing", f"{path}.{name}"))
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            name = str(key)
            child_schema = properties.get(name)
            if isinstance(child_schema, Mapping):
                self._visit(item, child_schema, path=f"{path}.{name}", depth=depth + 1, budget=budget, findings=findings)
            elif additional is False:
                findings.append(YieldFinding("yield_additional_property", f"unexpected property {name!r}", f"{path}.{name}"))
            elif isinstance(additional, Mapping):
                self._visit(item, additional, path=f"{path}.{name}", depth=depth + 1, budget=budget, findings=findings)

    def _validate_array(
        self,
        value: Sequence[Any],
        schema: Mapping[str, Any],
        *,
        path: str,
        depth: int,
        budget: YieldBudget,
        findings: list[YieldFinding],
    ) -> None:
        maximum = min(int(schema.get("maxItems") or budget.maximum_list_items), budget.maximum_list_items)
        minimum = int(schema.get("minItems") or 0)
        if len(value) < minimum:
            findings.append(YieldFinding("yield_array_too_short", f"minimum items is {minimum}", path))
        if len(value) > maximum:
            findings.append(YieldFinding("yield_array_too_long", f"maximum items is {maximum}", path))
            return
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                self._visit(item, item_schema, path=f"{path}[{index}]", depth=depth + 1, budget=budget, findings=findings)
        if bool(schema.get("uniqueItems")):
            digests = [_digest(_json_value(item)) for item in value]
            if len(digests) != len(set(digests)):
                findings.append(YieldFinding("yield_array_not_unique", "array items must be unique", path))

    @staticmethod
    def _matches_type(value: Any, expected: str) -> bool:
        return {
            "null": value is None,
            "object": isinstance(value, Mapping),
            "array": isinstance(value, Sequence) and not isinstance(value, (str, bytes)),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }.get(expected, True)

    @staticmethod
    def _infer_type(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, Mapping):
            return "object"
        if isinstance(value, Sequence):
            return "array"
        return "unknown"


class TypedYieldStore:
    schema = "zyra.typed-yield-store/v1"

    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = bool(disabled)
        self._lock = RLock()
        self._contracts: dict[str, YieldContract] = {}
        self._assemblies: dict[str, YieldAssembly] = {}
        self._idempotency: dict[str, str] = {}
        self._load()

    def register(self, contract: YieldContract) -> YieldContract:
        self._require_enabled()
        with self._lock:
            existing = self._contracts.get(contract.contract_id)
            if existing and existing.digest != contract.digest:
                raise TypedYieldConflict(f"yield contract {contract.contract_id} already has a different digest")
            self._contracts[contract.contract_id] = copy.deepcopy(contract)
            self._persist()
            return copy.deepcopy(contract)

    def begin(
        self,
        *,
        task_id: str,
        contract: YieldContract,
        execution_ref: str,
        attempt: int,
    ) -> YieldAssembly:
        self._require_enabled()
        with self._lock:
            self.register(contract)
            existing = self._assemblies.get(task_id)
            if existing:
                if existing.contract_digest != contract.digest or existing.execution_ref != execution_ref:
                    raise TypedYieldConflict("yield assembly identity mismatch")
                return copy.deepcopy(existing)
            assembly = YieldAssembly(
                task_id=task_id,
                contract_id=contract.contract_id,
                contract_digest=contract.digest,
                execution_ref=execution_ref,
                attempt=max(1, int(attempt)),
                parts=(),
            )
            self._assemblies[task_id] = assembly
            self._persist()
            return copy.deepcopy(assembly)

    def append(self, value: TypedYield, *, idempotency_key: str) -> YieldAssembly:
        self._require_enabled()
        with self._lock:
            existing_yield_id = self._idempotency.get(idempotency_key)
            if existing_yield_id:
                assembly = self._assemblies.get(value.task_id)
                if assembly is None:
                    raise TypedYieldConflict("idempotency points to missing assembly")
                existing = next((item for item in assembly.parts if item.yield_id == existing_yield_id), None)
                if existing is None or existing.digest != value.digest:
                    raise TypedYieldConflict("typed yield idempotency conflict")
                return copy.deepcopy(assembly)
            assembly = self._assemblies.get(value.task_id)
            if assembly is None:
                raise TypedYieldConflict(f"yield assembly is not started for {value.task_id}")
            contract = self._contracts[assembly.contract_id]
            findings = self.validate(value, contract, assembly)
            if any(item.blocking for item in findings):
                raise TypedYieldValidationError(findings)
            merged = self._merge(assembly.assembled_data, value.data, contract.merge_strategy, value.kind)
            total_chars = assembly.total_chars + _char_size(value.to_dict())
            changed = replace(
                assembly,
                parts=(*assembly.parts, copy.deepcopy(value)),
                assembled_data=merged,
                summary=value.summary if value.summary else assembly.summary,
                artifact_refs=_unique_strings((*assembly.artifact_refs, *value.artifact_refs)),
                evidence_refs=_unique_strings((*assembly.evidence_refs, *value.evidence_refs)),
                warnings=_unique_strings((*assembly.warnings, *value.warnings)),
                total_chars=total_chars,
                revision=assembly.revision + 1,
                terminal_yield_id=value.yield_id if value.kind.terminal else assembly.terminal_yield_id,
                completed=value.kind is YieldKind.TERMINAL,
                failed=value.kind is YieldKind.FAILURE,
                cancelled=value.kind is YieldKind.CANCELLED,
                updated_at=now_iso(),
            )
            self._assemblies[value.task_id] = changed
            self._idempotency[idempotency_key] = value.yield_id
            self._persist()
            return copy.deepcopy(changed)

    def validate(self, value: TypedYield, contract: YieldContract, assembly: YieldAssembly) -> tuple[YieldFinding, ...]:
        findings: list[YieldFinding] = []
        budget = contract.budget
        if value.kind not in contract.allowed_kinds:
            findings.append(YieldFinding("yield_kind_forbidden", f"yield kind {value.kind.value} is not allowed"))
        if value.execution_ref != assembly.execution_ref or value.attempt != assembly.attempt:
            findings.append(YieldFinding("yield_execution_mismatch", "yield is fenced to another execution attempt"))
        if value.sequence != assembly.next_sequence:
            findings.append(YieldFinding("yield_sequence_mismatch", f"expected sequence {assembly.next_sequence}"))
        if assembly.terminal_yield_id:
            findings.append(YieldFinding("yield_after_terminal", "cannot append after terminal yield"))
        if len(assembly.parts) >= budget.maximum_parts:
            findings.append(YieldFinding("yield_part_limit", "yield part limit exceeded"))
        if len(value.summary) > budget.maximum_summary_chars:
            findings.append(YieldFinding("yield_summary_limit", "yield summary is too large", "$.summary"))
        if len(value.artifact_refs) > budget.maximum_artifact_refs:
            findings.append(YieldFinding("yield_artifact_limit", "too many artifact references", "$.artifact_refs"))
        if len(value.evidence_refs) > budget.maximum_evidence_refs:
            findings.append(YieldFinding("yield_evidence_limit", "too many evidence references", "$.evidence_refs"))
        if len(value.warnings) > budget.maximum_warnings:
            findings.append(YieldFinding("yield_warning_limit", "too many warnings", "$.warnings"))
        if assembly.total_chars + _char_size(value.to_dict()) > budget.maximum_total_chars:
            findings.append(YieldFinding("yield_total_chars", "yield payload budget exceeded"))
        if value.kind in {YieldKind.TERMINAL, YieldKind.PARTIAL}:
            findings.extend(JsonSchemaSubsetValidator().validate(value.data, contract.schema, budget=budget))
        if value.kind is YieldKind.FAILURE and not value.error_code:
            findings.append(YieldFinding("yield_failure_code_missing", "failure yield requires error_code"))
        if value.kind is YieldKind.PROGRESS and value.data not in (None, {}, []):
            findings.append(YieldFinding(
                "yield_progress_data_ignored",
                "progress yield data is not part of terminal assembly",
                severity=YieldSeverity.WARNING,
                blocking=False,
            ))
        return tuple(findings)

    def require_terminal(self, task_id: str) -> YieldAssembly:
        assembly = self.get(task_id)
        contract = self._contracts[assembly.contract_id]
        if contract.required_terminal and not assembly.terminal_yield_id:
            raise TypedYieldValidationError((YieldFinding(
                "yield_terminal_missing",
                "subagent completed without the required terminal typed yield",
            ),))
        return assembly

    def get(self, task_id: str) -> YieldAssembly:
        self._require_enabled()
        with self._lock:
            value = self._assemblies.get(task_id)
            if value is None:
                raise TypedYieldConflict(f"yield assembly not found: {task_id}")
            return copy.deepcopy(value)

    def list(self, *, parent_task_id: str | None = None) -> tuple[YieldAssembly, ...]:
        self._require_enabled()
        with self._lock:
            values = []
            for assembly in self._assemblies.values():
                if parent_task_id is not None:
                    first = assembly.parts[0] if assembly.parts else None
                    if first is None or first.parent_task_id != parent_task_id:
                        continue
                values.append(copy.deepcopy(assembly))
            return tuple(sorted(values, key=lambda item: item.task_id))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            body = {
                "schema": self.schema,
                "owner": "M1-03D TypedYieldStore",
                "contracts": [item.to_dict() for item in sorted(self._contracts.values(), key=lambda value: value.contract_id)],
                "assemblies": [item.to_dict() for item in sorted(self._assemblies.values(), key=lambda value: value.task_id)],
                "idempotency": dict(sorted(self._idempotency.items())),
            }
            return {**body, "checksum": _digest(body)}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TypedYieldError(f"cannot load typed yield store: {error}") from error
        if not isinstance(value, Mapping):
            raise TypedYieldError("typed yield store root must be an object")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        expected = str(value.get("checksum") or "")
        if expected and expected != _digest(body):
            raise TypedYieldError("typed yield store checksum mismatch")
        for item in value.get("contracts") or ():
            if isinstance(item, Mapping):
                contract = YieldContract.from_dict(item)
                self._contracts[contract.contract_id] = contract
        for item in value.get("assemblies") or ():
            if isinstance(item, Mapping):
                assembly = YieldAssembly.from_dict(item)
                self._assemblies[assembly.task_id] = assembly
        raw_idempotency = value.get("idempotency")
        if isinstance(raw_idempotency, Mapping):
            self._idempotency = {str(key): str(item) for key, item in raw_idempotency.items()}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    @staticmethod
    def _merge(current: Any, incoming: Any, strategy: YieldMergeStrategy, kind: YieldKind) -> Any:
        if kind is YieldKind.PROGRESS:
            return copy.deepcopy(current)
        if strategy is YieldMergeStrategy.FIRST_WINS and current is not None:
            return copy.deepcopy(current)
        if strategy is YieldMergeStrategy.APPEND:
            return f"{'' if current is None else current}{'' if incoming is None else incoming}"
        if strategy is YieldMergeStrategy.LIST_EXTEND:
            left = list(current) if isinstance(current, Sequence) and not isinstance(current, (str, bytes)) else []
            right = list(incoming) if isinstance(incoming, Sequence) and not isinstance(incoming, (str, bytes)) else [incoming]
            return [*_json_value(left), *_json_value(right)]
        if strategy is YieldMergeStrategy.OBJECT_MERGE:
            left = dict(current) if isinstance(current, Mapping) else {}
            right = dict(incoming) if isinstance(incoming, Mapping) else {}
            return _deep_merge(left, right)
        return copy.deepcopy(_json_value(incoming))

    def _require_enabled(self) -> None:
        if self.disabled:
            raise TypedYieldDisabled("TypedYieldStore is disabled")


def default_yield_contract(task_id: str, schema: Mapping[str, Any] | None = None) -> YieldContract:
    selected = schema or {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 16000},
            "result": {},
            "next_actions": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
        },
        "required": ["summary"],
        "additionalProperties": True,
    }
    return YieldContract(
        contract_id=f"yield-contract:{task_id}",
        schema=selected,
        merge_strategy=YieldMergeStrategy.REPLACE,
        metadata={"source": "OMP task/executor typed-yield semantics", "owner": "Zyra M1-03D"},
    )


def _deep_merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(left))
    for key, value in right.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[str(key)] = _deep_merge(result[str(key)], value)
        else:
            result[str(key)] = copy.deepcopy(_json_value(value))
    return result


def _json_mapping(value: Any) -> dict[str, Any]:
    return _json_value(value) if isinstance(value, Mapping) else {}


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    method = getattr(value, "safe_dict", None) or getattr(value, "to_dict", None)
    if callable(method):
        return _json_value(method())
    return str(value)


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        if any(token in name.casefold() for token in ("secret", "token", "password", "credential", "authorization")):
            result[name] = "<redacted>"
        else:
            result[name] = _json_value(item)
    return result


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        selected = str(value).strip()
        if not selected or selected in seen:
            continue
        seen.add(selected)
        result.append(selected)
    return tuple(result)


def _char_size(value: Any) -> int:
    return len(json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True))


def _digest(value: Any) -> str:
    payload = json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


__all__ = [
    "JsonSchemaSubsetValidator",
    "TypedYield",
    "TypedYieldConflict",
    "TypedYieldDisabled",
    "TypedYieldError",
    "TypedYieldStore",
    "TypedYieldValidationError",
    "YieldAssembly",
    "YieldBudget",
    "YieldContract",
    "YieldFinding",
    "YieldKind",
    "YieldMergeStrategy",
    "YieldSeverity",
    "default_yield_contract",
]
