from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zyra_runtime.tools import DynamicToolProvenance, ToolSpec

from .models import (
    ActionDefinition,
    ArgumentRule,
    PlanValidationIssue,
    SchemaValueType,
    ValidationIssue,
    digest_value,
)


@dataclass(frozen=True, slots=True)
class ValidatedArguments:
    action: str
    values: Mapping[str, Any]
    supplied: tuple[str, ...]
    defaulted: tuple[str, ...]
    aliases_used: Mapping[str, str]
    issues: tuple[ValidationIssue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", copy.deepcopy(dict(self.values)))
        object.__setattr__(self, "supplied", tuple(self.supplied))
        object.__setattr__(self, "defaulted", tuple(self.defaulted))
        object.__setattr__(self, "aliases_used", dict(self.aliases_used))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def ok(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    @property
    def digest(self) -> str:
        return digest_value(self.values)

    def require(self) -> "ValidatedArguments":
        if not self.ok:
            raise ActionSchemaError(self.action, self.issues)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "values": copy.deepcopy(dict(self.values)),
            "supplied": list(self.supplied),
            "defaulted": list(self.defaulted),
            "aliases_used": dict(self.aliases_used),
            "issues": [issue.to_dict() for issue in self.issues],
            "ok": self.ok,
            "digest": self.digest,
        }


class ActionSchemaError(ValueError):
    def __init__(self, action: str, issues: Sequence[ValidationIssue]) -> None:
        self.action = action
        self.issues = tuple(issues)
        summary = "; ".join(f"{item.path or '$'}: {item.code}" for item in self.issues if item.blocking)
        super().__init__(f"browser action {action!r} failed schema validation: {summary}")


class ActionArgumentValidator:
    def __init__(
        self,
        *,
        max_depth: int = 16,
        max_items: int = 20_000,
        max_key_chars: int = 256,
        reject_unknown: bool = True,
    ) -> None:
        if max_depth < 2 or max_items < 32 or max_key_chars < 16:
            raise ValueError("browser action validator limits are too small")
        self.max_depth = max_depth
        self.max_items = max_items
        self.max_key_chars = max_key_chars
        self.reject_unknown = reject_unknown

    def validate(self, definition: ActionDefinition, arguments: Mapping[str, Any] | None) -> ValidatedArguments:
        issues: list[ValidationIssue] = []
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            return ValidatedArguments(
                action=definition.name,
                values={},
                supplied=(),
                defaulted=(),
                aliases_used={},
                issues=(
                    ValidationIssue(
                        "arguments_not_object",
                        "browser action arguments must be an object",
                        "$",
                        expected="object",
                        actual=type(arguments).__name__,
                    ),
                ),
            )
        self._validate_structure(arguments, issues)
        normalized: dict[str, Any] = {}
        supplied: list[str] = []
        defaulted: list[str] = []
        aliases_used: dict[str, str] = {}
        consumed: set[str] = set()
        for rule in definition.arguments:
            present_name = next((name for name in rule.all_names if name in arguments), "")
            if present_name:
                consumed.add(present_name)
                supplied.append(rule.name)
                if present_name != rule.name:
                    aliases_used[present_name] = rule.name
                duplicate_names = [name for name in rule.all_names if name != present_name and name in arguments]
                if duplicate_names:
                    issues.append(
                        ValidationIssue(
                            "argument_alias_conflict",
                            f"argument {rule.name!r} was supplied more than once through aliases",
                            f"$.{rule.name}",
                            expected=list(rule.all_names),
                            actual=[present_name, *duplicate_names],
                        )
                    )
                    consumed.update(duplicate_names)
                raw_value = arguments[present_name]
                value, value_issues = self._validate_rule(rule, raw_value, path=f"$.{rule.name}")
                issues.extend(value_issues)
                if not any(issue.blocking for issue in value_issues):
                    normalized[rule.name] = value
            elif rule.required:
                issues.append(
                    ValidationIssue(
                        "missing_required_argument",
                        f"required browser action argument {rule.name!r} is missing",
                        f"$.{rule.name}",
                        expected=str(rule.value_type),
                    )
                )
            elif rule.default is not None:
                defaulted.append(rule.name)
                normalized[rule.name] = copy.deepcopy(rule.default)
        unknown = sorted(str(key) for key in arguments if str(key) not in consumed)
        for key in unknown:
            issues.append(
                ValidationIssue(
                    "unknown_argument",
                    f"unknown browser action argument {key!r}",
                    f"$.{key}",
                    expected=sorted(rule.name for rule in definition.arguments),
                    actual=key,
                    blocking=self.reject_unknown,
                )
            )
            if not self.reject_unknown:
                normalized[key] = copy.deepcopy(arguments[key])
        issues.extend(self._cross_field_validation(definition, normalized))
        return ValidatedArguments(
            action=definition.name,
            values=normalized,
            supplied=tuple(supplied),
            defaulted=tuple(defaulted),
            aliases_used=aliases_used,
            issues=tuple(issues),
        )

    def _validate_rule(self, rule: ArgumentRule, value: Any, *, path: str) -> tuple[Any, list[ValidationIssue]]:
        issues: list[ValidationIssue] = []
        if value is None:
            if rule.required:
                issues.append(
                    ValidationIssue(
                        "required_argument_null",
                        f"required argument {rule.name!r} cannot be null",
                        path,
                        expected=str(rule.value_type),
                        actual=None,
                    )
                )
            return None, issues
        coerced, type_issue = self._coerce_type(rule, value, path=path)
        if type_issue is not None:
            return value, [type_issue]
        if rule.enum and coerced not in rule.enum:
            issues.append(
                ValidationIssue(
                    "argument_not_in_enum",
                    f"argument {rule.name!r} is not an admitted value",
                    path,
                    expected=list(rule.enum),
                    actual=coerced,
                )
            )
        if isinstance(coerced, str):
            if rule.min_length is not None and len(coerced) < rule.min_length:
                issues.append(
                    ValidationIssue(
                        "string_too_short",
                        f"argument {rule.name!r} is shorter than {rule.min_length}",
                        path,
                        expected={"min_length": rule.min_length},
                        actual=len(coerced),
                    )
                )
            if rule.max_length is not None and len(coerced) > rule.max_length:
                issues.append(
                    ValidationIssue(
                        "string_too_long",
                        f"argument {rule.name!r} exceeds {rule.max_length} characters",
                        path,
                        expected={"max_length": rule.max_length},
                        actual=len(coerced),
                    )
                )
            if rule.pattern and re.fullmatch(rule.pattern, coerced) is None:
                issues.append(
                    ValidationIssue(
                        "string_pattern_mismatch",
                        f"argument {rule.name!r} does not match its admitted pattern",
                        path,
                        expected=rule.pattern,
                        actual=coerced[:200],
                    )
                )
        if isinstance(coerced, int | float) and not isinstance(coerced, bool):
            if not math.isfinite(float(coerced)):
                issues.append(
                    ValidationIssue(
                        "number_not_finite",
                        f"argument {rule.name!r} must be finite",
                        path,
                        expected="finite number",
                        actual=str(coerced),
                    )
                )
            if rule.minimum is not None and coerced < rule.minimum:
                issues.append(
                    ValidationIssue(
                        "number_below_minimum",
                        f"argument {rule.name!r} is below its minimum",
                        path,
                        expected={"minimum": rule.minimum},
                        actual=coerced,
                    )
                )
            if rule.maximum is not None and coerced > rule.maximum:
                issues.append(
                    ValidationIssue(
                        "number_above_maximum",
                        f"argument {rule.name!r} exceeds its maximum",
                        path,
                        expected={"maximum": rule.maximum},
                        actual=coerced,
                    )
                )
        if isinstance(coerced, list):
            if rule.min_items is not None and len(coerced) < rule.min_items:
                issues.append(
                    ValidationIssue(
                        "array_too_short",
                        f"argument {rule.name!r} contains too few items",
                        path,
                        expected={"min_items": rule.min_items},
                        actual=len(coerced),
                    )
                )
            if rule.max_items is not None and len(coerced) > rule.max_items:
                issues.append(
                    ValidationIssue(
                        "array_too_long",
                        f"argument {rule.name!r} contains too many items",
                        path,
                        expected={"max_items": rule.max_items},
                        actual=len(coerced),
                    )
                )
            for index, item in enumerate(coerced):
                item_issue = self._type_issue(rule.item_type, item, path=f"{path}[{index}]")
                if item_issue is not None:
                    issues.append(item_issue)
        return copy.deepcopy(coerced), issues

    def _coerce_type(self, rule: ArgumentRule, value: Any, *, path: str) -> tuple[Any, ValidationIssue | None]:
        expected = rule.value_type
        if expected == SchemaValueType.ANY:
            return copy.deepcopy(value), None
        if expected == SchemaValueType.STRING and isinstance(value, str):
            return value, None
        if expected == SchemaValueType.INTEGER and isinstance(value, int) and not isinstance(value, bool):
            return value, None
        if expected == SchemaValueType.NUMBER and isinstance(value, int | float) and not isinstance(value, bool):
            return value, None
        if expected == SchemaValueType.BOOLEAN and isinstance(value, bool):
            return value, None
        if expected == SchemaValueType.OBJECT and isinstance(value, Mapping):
            return copy.deepcopy(dict(value)), None
        if expected == SchemaValueType.ARRAY and isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return copy.deepcopy(list(value)), None
        return value, ValidationIssue(
            "argument_type_mismatch",
            f"argument {rule.name!r} has the wrong type",
            path,
            expected=str(expected),
            actual=type(value).__name__,
        )

    @staticmethod
    def _type_issue(expected: SchemaValueType, value: Any, *, path: str) -> ValidationIssue | None:
        if expected == SchemaValueType.ANY:
            return None
        matches = {
            SchemaValueType.STRING: isinstance(value, str),
            SchemaValueType.INTEGER: isinstance(value, int) and not isinstance(value, bool),
            SchemaValueType.NUMBER: isinstance(value, int | float) and not isinstance(value, bool),
            SchemaValueType.BOOLEAN: isinstance(value, bool),
            SchemaValueType.OBJECT: isinstance(value, Mapping),
            SchemaValueType.ARRAY: isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray),
        }[expected]
        if matches:
            return None
        return ValidationIssue(
            "array_item_type_mismatch",
            "browser action array item has the wrong type",
            path,
            expected=str(expected),
            actual=type(value).__name__,
        )

    def _validate_structure(self, value: Any, issues: list[ValidationIssue]) -> None:
        count = 0
        active: set[int] = set()

        def visit(item: Any, path: str, depth: int) -> None:
            nonlocal count
            count += 1
            if count > self.max_items:
                issues.append(
                    ValidationIssue(
                        "arguments_item_limit",
                        "browser action arguments exceed the structural item limit",
                        path,
                        expected={"max_items": self.max_items},
                        actual=count,
                    )
                )
                return
            if depth > self.max_depth:
                issues.append(
                    ValidationIssue(
                        "arguments_depth_limit",
                        "browser action arguments exceed the structural depth limit",
                        path,
                        expected={"max_depth": self.max_depth},
                        actual=depth,
                    )
                )
                return
            if isinstance(item, Mapping):
                identity = id(item)
                if identity in active:
                    issues.append(ValidationIssue("arguments_cycle", "browser action arguments contain a cycle", path))
                    return
                active.add(identity)
                for key, child in item.items():
                    if not isinstance(key, str):
                        issues.append(
                            ValidationIssue(
                                "argument_key_not_string",
                                "browser action object keys must be strings",
                                path,
                                expected="string",
                                actual=type(key).__name__,
                            )
                        )
                        continue
                    if not key or len(key) > self.max_key_chars or any(ord(character) < 0x20 for character in key):
                        issues.append(
                            ValidationIssue(
                                "argument_key_invalid",
                                "browser action object key is empty, too long or contains a control character",
                                f"{path}.{key[:40]}",
                            )
                        )
                    visit(child, f"{path}.{key}", depth + 1)
                active.remove(identity)
            elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
                identity = id(item)
                if identity in active:
                    issues.append(ValidationIssue("arguments_cycle", "browser action arguments contain a cycle", path))
                    return
                active.add(identity)
                for index, child in enumerate(item):
                    visit(child, f"{path}[{index}]", depth + 1)
                active.remove(identity)
            elif not isinstance(item, str | int | float | bool | type(None)):
                issues.append(
                    ValidationIssue(
                        "argument_value_unsupported",
                        "browser action argument contains an unsupported runtime value",
                        path,
                        expected="JSON-compatible value",
                        actual=type(item).__name__,
                    )
                )

        visit(value, "$", 0)

    @staticmethod
    def _cross_field_validation(definition: ActionDefinition, values: Mapping[str, Any]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if definition.selector_required:
            target_names = ("selector_ref", "selector", "index", "backend_node_id", "element_id", "id")
            if not any(values.get(name) not in (None, "") for name in target_names):
                issues.append(
                    ValidationIssue(
                        "missing_element_target",
                        "element action requires a selector_ref, selector index or backend node id",
                        "$",
                        expected=list(target_names),
                    )
                )
        if definition.name == "click_element" and "coordinate_x" in values and "coordinate_y" not in values:
            issues.append(
                ValidationIssue(
                    "incomplete_coordinate",
                    "coordinate click requires both coordinate_x and coordinate_y",
                    "$.coordinate_y",
                )
            )
        if definition.name == "click_element" and "coordinate_y" in values and "coordinate_x" not in values:
            issues.append(
                ValidationIssue(
                    "incomplete_coordinate",
                    "coordinate click requires both coordinate_x and coordinate_y",
                    "$.coordinate_x",
                )
            )
        if definition.name == "select_dropdown" and not values.get("option") and not values.get("options"):
            issues.append(
                ValidationIssue(
                    "dropdown_option_missing",
                    "select_dropdown requires option or options",
                    "$",
                )
            )
        if definition.name == "drag_element" and not values.get("target_selector_ref") and not (
            "target_x" in values and "target_y" in values
        ):
            issues.append(
                ValidationIssue(
                    "drag_target_missing",
                    "drag_element requires target_selector_ref or target_x/target_y",
                    "$",
                )
            )
        if definition.name == "download_file" and not values.get("url") and not values.get("selector_ref"):
            issues.append(
                ValidationIssue(
                    "download_source_missing",
                    "download_file requires a URL or selector_ref",
                    "$",
                )
            )
        return issues


class BrowserActionSchemaProjector:
    def __init__(self, *, namespace: str = "browser", version: str = "04c-foundation-v1") -> None:
        self.namespace = namespace
        self.version = version

    def project(self, definition: ActionDefinition) -> ToolSpec:
        metadata = {
            "tool_namespace": self.namespace,
            "tool_version": self.version,
            "action_name": definition.name,
            "source_action": definition.source_name,
            "access_mode": self._access_mode(definition),
            "read_only": str(definition.read_only).lower(),
            "concurrency_safe": str(definition.read_only).lower(),
            "mutates_browser": str(not definition.read_only).lower(),
            "permission_required": str(definition.permission_required).lower(),
            "selector_required": str(definition.selector_required).lower(),
            "terminates_sequence": str(definition.terminates_sequence).lower(),
            "risk": str(definition.base_risk),
            "result_budget_chars": str(definition.result_budget_chars),
            "schema_identity": definition.identity,
            "source_paths": ",".join(item.path for item in definition.sources),
            "owner_unit": "M1-S04C-01",
        }
        return ToolSpec(
            name=definition.name,
            purpose=definition.description,
            source="browser-use action registry/security productized by Zyra",
            input_schema=definition.to_schema(),
            output_schema={
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "output": {"type": "object"},
                    "artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "error_code": {"type": "string"},
                },
                "required": ["ok", "summary"],
            },
            metadata=metadata,
            execution_provenance=DynamicToolProvenance(
                tool_name=definition.name,
                namespace=self.namespace,
                version=self.version,
                handler_kind="browser_action",
                external_boundary=False,
                requires_exact_grant=True,
                source="zyra_workers.browser_action",
            ),
        )

    def project_all(self, definitions: Sequence[ActionDefinition]) -> list[ToolSpec]:
        return [self.project(definition) for definition in definitions]

    @staticmethod
    def _access_mode(definition: ActionDefinition) -> str:
        if definition.read_only:
            return "read_only"
        if any(str(item) == "external_side_effect" for item in definition.access):
            return "external_side_effect"
        if any(str(item) == "file_write" for item in definition.access):
            return "file_write"
        if any(str(item) == "script" for item in definition.access):
            return "script"
        return "browser_mutation"


def plan_issues(
    definitions: Mapping[str, ActionDefinition],
    aliases: Mapping[str, str],
    plan: Sequence[Mapping[str, Any]],
    *,
    validator: ActionArgumentValidator | None = None,
) -> list[PlanValidationIssue]:
    selected = validator or ActionArgumentValidator()
    issues: list[PlanValidationIssue] = []
    for index, step in enumerate(plan, start=1):
        if not isinstance(step, Mapping):
            issues.append(PlanValidationIssue(index, "", "invalid_plan_step", "$", {"actual": type(step).__name__}))
            continue
        raw_action = str(step.get("action") or step.get("browser_action") or "").strip()
        action = aliases.get(raw_action, raw_action)
        definition = definitions.get(action)
        if definition is None:
            issues.append(PlanValidationIssue(index, raw_action, "unknown_browser_action", "$.action"))
            continue
        arguments = step.get("arguments")
        result = selected.validate(definition, arguments if isinstance(arguments, Mapping) else arguments)
        for issue in result.issues:
            if not issue.blocking:
                continue
            reason = issue.code
            if issue.code == "missing_required_argument":
                reason = "missing_required_argument:" + issue.path.rsplit(".", 1)[-1]
            elif issue.code == "missing_element_target":
                reason = "missing_required_target:index|id|selector"
            issues.append(
                PlanValidationIssue(
                    index,
                    raw_action,
                    reason,
                    issue.path,
                    {"message": issue.message, "expected": issue.expected, "actual": issue.actual},
                )
            )
    return issues
