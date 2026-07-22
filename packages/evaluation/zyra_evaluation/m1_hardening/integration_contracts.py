from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity, utc_now


INTEGRATION_SCHEMA = "zyra.m1-hardening-integration/v1"
HANDOFF_SCHEMA = "zyra.m1-m2-handoff/v1"


class ScenarioKind(StrEnum):
    QUERY_TOOL = "query-session-context-tool"
    PERMISSION = "dangerous-tool-permission"
    MCP = "mcp-auth-elicitation"
    SKILL_MEMORY = "skill-memory-compact-restore"
    SUBAGENT_RECOVERY = "subagent-worker-recovery"
    STREAM_FAILOVER = "api-stream-provider-failover"


class EvidenceMaturity(StrEnum):
    ACTIVE_REAL = "active_real"
    CONFORMANCE = "conformance_verified"
    EXPERIMENTAL = "experimental"
    DEFERRED = "deferred"
    INVALID = "invalid"


class ActionRisk(StrEnum):
    LOW = "low"
    DANGEROUS = "dangerous"
    UNKNOWN = "unknown"


class ExpectedEffect(StrEnum):
    SUCCESS = "success"
    EXPLICIT_FAILURE = "explicit_failure"
    MATERIAL_DIFFERENCE = "material_difference"


@dataclass(frozen=True, slots=True)
class RequestSpec:
    step_id: str
    method: str
    path: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    expected_statuses: tuple[int, ...] = (200,)
    bind: Mapping[str, str] = field(default_factory=dict)
    optional: bool = False

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,80}", self.step_id):
            issues.append("step_id must be a stable kebab-case identifier")
        if self.method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            issues.append("unsupported HTTP method")
        if not self.path.startswith("/") or ".." in self.path.split("/"):
            issues.append("path must be an absolute normalized API path")
        if not self.expected_statuses:
            issues.append("at least one expected status is required")
        if any(status < 100 or status > 599 for status in self.expected_statuses):
            issues.append("expected status is outside HTTP range")
        for name, value in self.headers.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", str(name)):
                issues.append(f"invalid request header name: {name}")
            if any(character in str(value) for character in "\r\n\0"):
                issues.append(f"invalid request header value: {name}")
        for name, expression in self.bind.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
                issues.append(f"invalid binding name: {name}")
            if not expression.strip():
                issues.append(f"empty binding expression: {name}")
        return tuple(issues)


@dataclass(frozen=True, slots=True)
class ScenarioAssertion:
    assertion_id: str
    source: str
    expression: str
    operator: str
    expected: Any = True
    severity: Severity = Severity.BLOCKER
    summary: str = ""

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,100}", self.assertion_id):
            issues.append("invalid assertion id")
        if self.source not in {"response", "event", "task", "artifact", "binding", "comparison"}:
            issues.append(f"unsupported assertion source: {self.source}")
        if self.operator not in {
            "exists",
            "missing",
            "equals",
            "not_equals",
            "contains",
            "matches",
            "truthy",
            "falsey",
            "greater_than",
            "at_least",
        }:
            issues.append(f"unsupported assertion operator: {self.operator}")
        if not self.expression.strip():
            issues.append("assertion expression is empty")
        return tuple(issues)


@dataclass(frozen=True, slots=True)
class DisconnectRequirement:
    capability: str
    probe_id: str
    expected_effect: ExpectedEffect
    exercise_step_id: str
    expected_errors: tuple[str, ...] = ()
    forbid_fallback: bool = True

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not self.capability.strip():
            issues.append("disconnect capability is empty")
        if not re.fullmatch(r"disable-[a-z0-9-]{3,100}", self.probe_id):
            issues.append("disconnect probe id must start with disable-")
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,80}", self.exercise_step_id):
            issues.append("disconnect must name one explicit scenario exercise step")
        if self.expected_effect is ExpectedEffect.EXPLICIT_FAILURE and not self.expected_errors:
            issues.append("explicit failure disconnect requires stable error codes")
        return tuple(issues)


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: str
    kind: ScenarioKind
    summary: str
    requests: tuple[RequestSpec, ...]
    assertions: tuple[ScenarioAssertion, ...]
    disconnects: tuple[DisconnectRequirement, ...]
    required_event_families: tuple[str, ...]
    required_state_families: tuple[str, ...]
    minimum_revision_advances: int = 1
    require_causation: bool = True
    require_same_run: bool = True

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        if self.scenario_id != f"m1-integration-{self.kind.value}":
            issues.append("scenario id must be derived from the canonical kind")
        if len(self.summary.strip()) < 16:
            issues.append("scenario summary is not descriptive")
        if not self.requests:
            issues.append("scenario has no executable API requests")
        if not self.assertions:
            issues.append("scenario has no semantic assertions")
        if not self.disconnects:
            issues.append("scenario has no disconnect requirement")
        if self.minimum_revision_advances < 1:
            issues.append("scenario must require a real revision advance")
        step_ids: set[str] = set()
        for request in self.requests:
            issues.extend(f"request {request.step_id}: {issue}" for issue in request.validate())
            if request.step_id in step_ids:
                issues.append(f"duplicate request step: {request.step_id}")
            step_ids.add(request.step_id)
        assertion_ids: set[str] = set()
        for assertion in self.assertions:
            issues.extend(f"assertion {assertion.assertion_id}: {issue}" for issue in assertion.validate())
            if assertion.assertion_id in assertion_ids:
                issues.append(f"duplicate assertion: {assertion.assertion_id}")
            assertion_ids.add(assertion.assertion_id)
        probe_ids: set[str] = set()
        for disconnect in self.disconnects:
            issues.extend(f"disconnect {disconnect.probe_id}: {issue}" for issue in disconnect.validate())
            if disconnect.exercise_step_id not in step_ids:
                issues.append(
                    f"disconnect {disconnect.probe_id}: unknown exercise step "
                    f"{disconnect.exercise_step_id}"
                )
            if disconnect.probe_id in probe_ids:
                issues.append(f"duplicate disconnect: {disconnect.probe_id}")
            probe_ids.add(disconnect.probe_id)
        if len(set(self.required_event_families)) != len(self.required_event_families):
            issues.append("required event families contain duplicates")
        if len(set(self.required_state_families)) != len(self.required_state_families):
            issues.append("required state families contain duplicates")
        return tuple(issues)


@dataclass(slots=True)
class StepEvidence:
    step_id: str
    method: str
    path: str
    status: int
    started_at: str
    completed_at: str
    request_digest: str
    response: Mapping[str, Any]
    bindings: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request_digest": self.request_digest,
            "response": redact_mapping(self.response),
            "bindings": redact_mapping(self.bindings),
            "error": self.error,
            "ok": self.ok,
        }


@dataclass(slots=True)
class AssertionEvidence:
    assertion_id: str
    passed: bool
    observed: Any
    expected: Any
    source_location: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "passed": self.passed,
            "observed": redact_value(self.observed),
            "expected": redact_value(self.expected),
            "source_location": self.source_location,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ScenarioEvidence:
    scenario_id: str
    kind: ScenarioKind
    run_id: str
    task_id: str
    started_at: str
    completed_at: str
    baseline_revision: int | None
    final_revision: int | None
    steps: list[StepEvidence] = field(default_factory=list)
    assertions: list[AssertionEvidence] = field(default_factory=list)
    events: list[Mapping[str, Any]] = field(default_factory=list)
    artifacts: list[Mapping[str, Any]] = field(default_factory=list)
    disconnect_evidence: list[Mapping[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def revision_advance(self) -> int:
        if self.baseline_revision is None or self.final_revision is None:
            return 0
        return max(0, self.final_revision - self.baseline_revision)

    @property
    def passed(self) -> bool:
        return (
            bool(self.steps)
            and all(step.ok for step in self.steps)
            and bool(self.assertions)
            and all(assertion.passed for assertion in self.assertions)
            and not self.limitations
        )

    def digest(self) -> str:
        value = self.to_dict(include_digest=False)
        return stable_digest(value)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": INTEGRATION_SCHEMA,
            "scenario_id": self.scenario_id,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "baseline_revision": self.baseline_revision,
            "final_revision": self.final_revision,
            "revision_advance": self.revision_advance,
            "steps": [step.to_dict() for step in self.steps],
            "assertions": [assertion.to_dict() for assertion in self.assertions],
            "events": [redact_mapping(event) for event in self.events],
            "artifacts": [redact_mapping(artifact) for artifact in self.artifacts],
            "disconnect_evidence": [redact_mapping(item) for item in self.disconnect_evidence],
            "limitations": list(self.limitations),
            "metadata": redact_mapping(self.metadata),
            "passed": self.passed,
        }
        if include_digest:
            value["content_digest"] = stable_digest(value)
        return value


class JsonPath:
    _TOKEN = re.compile(r"(?P<name>[^.\[\]]+)|\[(?P<index>\d+|\*)\]")

    @classmethod
    def select(cls, value: Any, expression: str) -> list[Any]:
        expression = expression.strip().removeprefix("$.").removeprefix("$")
        if not expression:
            return [value]
        current = [value]
        for match in cls._TOKEN.finditer(expression):
            name = match.group("name")
            index = match.group("index")
            next_values: list[Any] = []
            for item in current:
                if name is not None:
                    if isinstance(item, Mapping) and name in item:
                        next_values.append(item[name])
                elif index == "*":
                    if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                        next_values.extend(item)
                elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                    position = int(index or 0)
                    if position < len(item):
                        next_values.append(item[position])
            current = next_values
            if not current:
                break
        return current

    @classmethod
    def first(cls, value: Any, expression: str, default: Any = None) -> Any:
        selected = cls.select(value, expression)
        return selected[0] if selected else default


class AssertionEvaluator:
    def evaluate(self, assertion: ScenarioAssertion, source: Any) -> AssertionEvidence:
        observed_values = JsonPath.select(source, assertion.expression)
        observed: Any = observed_values
        if len(observed_values) == 1:
            observed = observed_values[0]
        passed, detail = self._compare(assertion.operator, observed_values, assertion.expected)
        return AssertionEvidence(
            assertion_id=assertion.assertion_id,
            passed=passed,
            observed=observed,
            expected=assertion.expected,
            source_location=f"{assertion.source}:{assertion.expression}",
            detail=detail,
        )

    def _compare(self, operator: str, values: Sequence[Any], expected: Any) -> tuple[bool, str]:
        if operator == "exists":
            return bool(values), "value was present" if values else "value was absent"
        if operator == "missing":
            return not values, "value was absent" if not values else "unexpected value was present"
        if operator == "truthy":
            return any(bool(value) for value in values), "truthy comparison"
        if operator == "falsey":
            return bool(values) and all(not bool(value) for value in values), "falsey comparison"
        if operator == "equals":
            return any(value == expected for value in values), "equality comparison"
        if operator == "not_equals":
            return bool(values) and all(value != expected for value in values), "inequality comparison"
        if operator == "contains":
            passed = any(self._contains(value, expected) for value in values)
            return passed, "membership comparison"
        if operator == "matches":
            try:
                pattern = re.compile(str(expected), re.IGNORECASE | re.MULTILINE)
            except re.error as error:
                return False, f"invalid assertion regex: {error}"
            return any(pattern.search(self._text(value)) is not None for value in values), "regex comparison"
        if operator in {"greater_than", "at_least"}:
            numbers = [self._number(value) for value in values]
            numbers = [value for value in numbers if value is not None]
            threshold = self._number(expected)
            if threshold is None or not numbers:
                return False, "numeric comparison had no numeric operands"
            if operator == "greater_than":
                return any(value > threshold for value in numbers), "greater-than comparison"
            return any(value >= threshold for value in numbers), "at-least comparison"
        return False, f"unsupported operator: {operator}"

    @staticmethod
    def _contains(value: Any, expected: Any) -> bool:
        if isinstance(value, Mapping):
            return expected in value or expected in value.values()
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return expected in value
        return str(expected).lower() in str(value).lower()

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class ScenarioEvidenceGate:
    def evaluate(
        self,
        definition: ScenarioDefinition,
        evidence: ScenarioEvidence,
        *,
        require_disconnects: bool = True,
    ) -> GateResult:
        result = GateResult(
            gate_id=f"scenario:{definition.scenario_id}",
            status=GateStatus.NOT_RUN,
            summary=definition.summary,
        )
        for issue in definition.validate():
            result.add(
                Finding(
                    code="integration.scenario_contract_invalid",
                    severity=Severity.BLOCKER,
                    summary="Integration scenario definition is invalid.",
                    detail=issue,
                    location=definition.scenario_id,
                )
            )
        if evidence.scenario_id != definition.scenario_id or evidence.kind is not definition.kind:
            result.add(
                Finding(
                    code="integration.scenario_identity_mismatch",
                    severity=Severity.BLOCKER,
                    summary="Scenario evidence does not match the executed definition.",
                    detail=f"expected={definition.scenario_id}; observed={evidence.scenario_id}",
                )
            )
        if not evidence.task_id or not evidence.run_id:
            result.add(
                Finding(
                    code="integration.canonical_identity_missing",
                    severity=Severity.BLOCKER,
                    summary="Scenario evidence is missing canonical task or run identity.",
                )
            )
        if evidence.revision_advance < definition.minimum_revision_advances:
            result.add(
                Finding(
                    code="integration.revision_not_advanced",
                    severity=Severity.BLOCKER,
                    summary="Scenario did not produce enough canonical revision movement.",
                    detail=f"required={definition.minimum_revision_advances}; observed={evidence.revision_advance}",
                )
            )
        self._validate_step_coverage(definition, evidence, result)
        self._validate_assertions(definition, evidence, result)
        self._validate_events(definition, evidence, result)
        self._validate_disconnects(
            definition,
            evidence,
            result,
            require_disconnects=require_disconnects,
        )
        for limitation in evidence.limitations:
            disconnect_only = limitation in {
                "scenario disconnects were not executed",
                "no real disconnect executor is configured",
            }
            result.add(
                Finding(
                    code="integration.scenario_limitation",
                    severity=(
                        Severity.WARNING
                        if disconnect_only and not require_disconnects
                        else Severity.BLOCKER
                    ),
                    summary="Scenario has an unresolved execution limitation.",
                    detail=limitation,
                )
            )
        result.metrics.update(
            {
                "kind": definition.kind.value,
                "task_id": evidence.task_id,
                "run_id": evidence.run_id,
                "step_count": len(evidence.steps),
                "assertion_count": len(evidence.assertions),
                "event_count": len(evidence.events),
                "artifact_count": len(evidence.artifacts),
                "disconnect_count": len(evidence.disconnect_evidence),
                "revision_advance": evidence.revision_advance,
                "evidence_digest": evidence.digest(),
            }
        )
        result.evidence.append(
            EvidencePointer(
                kind="integration_scenario",
                location=evidence.scenario_id,
                summary=f"{definition.kind.value}: {'passed' if evidence.passed else 'incomplete'}",
                revision=str(evidence.final_revision or ""),
                metadata={"content_digest": evidence.digest(), "task_id": evidence.task_id, "run_id": evidence.run_id},
            )
        )
        return result.finish()

    @staticmethod
    def _validate_step_coverage(
        definition: ScenarioDefinition,
        evidence: ScenarioEvidence,
        result: GateResult,
    ) -> None:
        receipts = {step.step_id: step for step in evidence.steps}
        for request in definition.requests:
            receipt = receipts.get(request.step_id)
            if receipt is None and not request.optional:
                result.add(
                    Finding(
                        code="integration.required_step_missing",
                        severity=Severity.BLOCKER,
                        summary="Required real API scenario step has no receipt.",
                        detail=request.step_id,
                    )
                )
                continue
            if receipt and receipt.status not in request.expected_statuses:
                result.add(
                    Finding(
                        code="integration.step_status_unexpected",
                        severity=Severity.BLOCKER,
                        summary="Scenario step returned an unexpected status.",
                        detail=f"{request.step_id}: {receipt.status} not in {request.expected_statuses}",
                    )
                )
            if receipt and receipt.error:
                result.add(
                    Finding(
                        code="integration.step_transport_failed",
                        severity=Severity.BLOCKER,
                        summary="Scenario transport or binding failed.",
                        detail=f"{request.step_id}: {receipt.error}",
                    )
                )

    @staticmethod
    def _validate_assertions(
        definition: ScenarioDefinition,
        evidence: ScenarioEvidence,
        result: GateResult,
    ) -> None:
        observed = {item.assertion_id: item for item in evidence.assertions}
        expected = {item.assertion_id: item for item in definition.assertions}
        for assertion_id, contract in expected.items():
            receipt = observed.get(assertion_id)
            if receipt is None:
                result.add(
                    Finding(
                        code="integration.assertion_missing",
                        severity=contract.severity,
                        summary="Required semantic assertion was not evaluated.",
                        detail=assertion_id,
                    )
                )
            elif not receipt.passed:
                result.add(
                    Finding(
                        code="integration.assertion_failed",
                        severity=contract.severity,
                        summary=contract.summary or "Integration scenario semantic assertion failed.",
                        detail=f"{assertion_id}: {receipt.detail}",
                        metadata={"observed": redact_value(receipt.observed), "expected": redact_value(receipt.expected)},
                    )
                )

    @staticmethod
    def _validate_events(
        definition: ScenarioDefinition,
        evidence: ScenarioEvidence,
        result: GateResult,
    ) -> None:
        text_by_family: dict[str, list[Mapping[str, Any]]] = {}
        run_ids: set[str] = set()
        causation_count = 0
        for event in evidence.events:
            event_type = str(event.get("event_type") or event.get("type") or "").lower()
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            text = " ".join((event_type, json.dumps(payload, ensure_ascii=False, default=str))).lower()
            for family in definition.required_event_families:
                if all(token in text for token in family.lower().split("+")):
                    text_by_family.setdefault(family, []).append(event)
            run_id = str(event.get("run_id") or payload.get("run_id") or "")
            if run_id:
                run_ids.add(run_id)
            if (
                event.get("causation_id")
                or payload.get("causation_id")
                or payload.get("request_id")
                or ScenarioEvidenceGate._has_causal_identity(payload)
            ):
                causation_count += 1
        for family in definition.required_event_families:
            if not text_by_family.get(family):
                result.add(
                    Finding(
                        code="integration.event_family_missing",
                        severity=Severity.BLOCKER,
                        summary="Scenario lacks canonical evidence for a required event family.",
                        detail=family,
                    )
                )
        if definition.require_same_run and run_ids and run_ids != {evidence.run_id}:
            result.add(
                Finding(
                    code="integration.cross_run_evidence",
                    severity=Severity.BLOCKER,
                    summary="Scenario combined evidence from different canonical runs.",
                    detail=", ".join(sorted(run_ids)),
                )
            )
        if definition.require_causation and causation_count == 0:
            result.add(
                Finding(
                    code="integration.causation_missing",
                    severity=Severity.BLOCKER,
                    summary="Scenario events have no real causation or request correlation.",
                )
            )

    @staticmethod
    def _has_causal_identity(value: Any) -> bool:
        causal_keys = {
            "causation_id",
            "correlation_id",
            "request_id",
            "worker_request_id",
            "tool_call_id",
            "tool_use_id",
            "command_id",
            "decision_id",
        }
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).lower() in causal_keys and str(item or "").strip():
                    return True
                if ScenarioEvidenceGate._has_causal_identity(item):
                    return True
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(ScenarioEvidenceGate._has_causal_identity(item) for item in value)
        return False

    @staticmethod
    def _validate_disconnects(
        definition: ScenarioDefinition,
        evidence: ScenarioEvidence,
        result: GateResult,
        *,
        require_disconnects: bool,
    ) -> None:
        observed = {str(item.get("probe_id") or ""): item for item in evidence.disconnect_evidence}
        for requirement in definition.disconnects:
            item = observed.get(requirement.probe_id)
            if not item:
                result.add(
                    Finding(
                        code="integration.disconnect_missing",
                        severity=Severity.BLOCKER if require_disconnects else Severity.WARNING,
                        summary="Scenario has no executed disconnect evidence for its owner.",
                        detail=requirement.probe_id,
                    )
                )
                continue
            status = str(item.get("status") or "").lower()
            expected = bool(item.get("expected_failure_observed") or item.get("material_difference"))
            if status not in {"passed", "success"} or not expected:
                result.add(
                    Finding(
                        code="integration.disconnect_no_effect",
                        severity=Severity.BLOCKER,
                        summary="Disconnect did not produce its required semantic effect.",
                        detail=requirement.probe_id,
                    )
                )
            if requirement.forbid_fallback and item.get("fallback_masked") is True:
                result.add(
                    Finding(
                        code="integration.disconnect_fallback_masked",
                        severity=Severity.BLOCKER,
                        summary="An alternate owner masked the disconnected canonical owner.",
                        detail=requirement.probe_id,
                    )
                )
            error = str(item.get("error_code") or item.get("error") or "")
            if requirement.expected_errors and error not in requirement.expected_errors:
                result.add(
                    Finding(
                        code="integration.disconnect_error_unexpected",
                        severity=Severity.BLOCKER,
                        summary="Disconnect produced an unexpected failure contract.",
                        detail=f"{requirement.probe_id}: {error}",
                    )
                )


def stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


SECRET_KEY_PATTERN = re.compile(
    r"(^|[_-])(secret|token|password|passwd|authorization|api[_-]?key|credential|cookie|private[_-]?key)($|[_-])",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERN = re.compile(
    r"(bearer\s+[a-z0-9._~+/-]{8,}|sk-[a-z0-9_-]{8,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)",
    re.IGNORECASE,
)


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if SECRET_KEY_PATTERN.search(str(key)):
            redacted[str(key)] = "[REDACTED]"
        else:
            redacted[str(key)] = redact_value(item)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE_PATTERN.sub("[REDACTED]", value)
    return value


def request_digest(method: str, path: str, payload: Mapping[str, Any]) -> str:
    return stable_digest({"method": method.upper(), "path": path, "payload": redact_mapping(payload)})


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def endpoint_identity(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    loopback = False
    private = False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        loopback = host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost")
        private = host.endswith(".local") or host.endswith(".internal")
    else:
        loopback = address.is_loopback or address.is_unspecified
        private = address.is_private or address.is_link_local
    return {
        "scheme": parsed.scheme.lower(),
        "host": host,
        "port": parsed.port,
        "path_digest": stable_digest(parsed.path or "/")[:16],
        "loopback": loopback,
        "private": private,
        "credential_in_url": parsed.username is not None or parsed.password is not None,
    }


def canonical_revision(value: Mapping[str, Any]) -> int | None:
    candidates: list[Any] = [
        value.get("revision"),
        value.get("version"),
        value.get("sequence"),
    ]
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
    candidates.extend((payload.get("after_revision"), payload.get("revision"), payload.get("sequence")))
    for candidate in candidates:
        if isinstance(candidate, bool):
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def event_identity(value: Mapping[str, Any]) -> tuple[str, str, str]:
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
    return (
        str(value.get("event_id") or payload.get("event_id") or ""),
        str(value.get("run_id") or payload.get("run_id") or ""),
        str(value.get("task_id") or payload.get("task_id") or ""),
    )


def new_events(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    known = {event_identity(item)[0] for item in before if event_identity(item)[0]}
    if known:
        return [dict(item) for item in after if event_identity(item)[0] not in known]
    baseline_digests = {stable_digest(redact_mapping(item)) for item in before}
    return [dict(item) for item in after if stable_digest(redact_mapping(item)) not in baseline_digests]


def integration_started_metadata(*, baseline_commit: str, target_commit: str = "") -> dict[str, Any]:
    return {
        "schema": INTEGRATION_SCHEMA,
        "started_at": utc_now(),
        "baseline_commit": baseline_commit,
        "target_commit": target_commit,
        "migration_mode": "audit_and_hardening_only",
        "canonical_state_owner": "existing M1 runtime stores",
        "derivative_owner": "M1HardeningReportStore",
    }
