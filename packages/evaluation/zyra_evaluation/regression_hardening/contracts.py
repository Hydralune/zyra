from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "zyra.m3-regression-hardening/v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"(?i)(?:authorization|cookie|credential|password|passwd|secret|token|api[_-]?key)"
)


class ContractError(ValueError):
    pass


class CaseStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class AssertionSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class IsolationMode(StrEnum):
    READ_ONLY = "read_only"
    TEMPORARY = "temporary"
    CHECKOUT = "checkout"
    PROCESS = "process"


class FailureKind(StrEnum):
    ASSERTION = "assertion"
    DEPENDENCY = "dependency"
    TIMEOUT = "timeout"
    PROCESS = "process"
    SECURITY = "security"
    POLLUTION = "pollution"
    CONTRACT = "contract"
    INTERNAL = "internal"


class ObservationKind(StrEnum):
    ENTRYPOINT = "entrypoint"
    OWNER = "owner"
    EVENT = "event"
    EFFECT = "effect"
    ARTIFACT = "artifact"
    ROUTE = "route"
    MUTATION = "mutation"
    CONTROL = "control"
    SECURITY = "security"
    CLEAN_STATE = "clean_state"
    PROCESS = "process"
    INDEX = "index"
    APPROVAL = "approval"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def stable_digest(*values: Any) -> str:
    payload = canonical_json(values).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def bounded_text(value: object, *, maximum: int = 4_000) -> str:
    text = str(value).replace("\x00", "")
    if len(text) <= maximum:
        return text
    return text[:maximum] + f"\n…<{len(text) - maximum} characters omitted>"


def validate_identifier(value: str, *, field_name: str) -> str:
    normalized = str(value).strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ContractError(f"{field_name} is not a stable identifier: {value!r}")
    return normalized


def validate_relative_path(value: str, *, field_name: str) -> str:
    normalized = str(value).replace("\\", "/").strip("/")
    candidate = Path(normalized)
    if (
        not normalized
        or candidate.is_absolute()
        or ".." in candidate.parts
        or any(part in {"", "."} for part in candidate.parts)
    ):
        raise ContractError(f"{field_name} must be a confined relative path: {value!r}")
    return normalized


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"cannot encode {type(value).__name__}")


def _public_mapping(value: Mapping[str, Any], *, depth: int = 0) -> dict[str, Any]:
    if depth > 20:
        raise ContractError("metadata nesting exceeds 20 levels")
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if _SECRET_KEY.search(key):
            result[key] = "<redacted>"
            continue
        if isinstance(raw_value, Mapping):
            result[key] = _public_mapping(raw_value, depth=depth + 1)
        elif isinstance(raw_value, (list, tuple)):
            result[key] = [
                _public_mapping(item, depth=depth + 1)
                if isinstance(item, Mapping)
                else item
                for item in raw_value[:1_000]
            ]
        elif isinstance(raw_value, bytes):
            result[key] = {
                "kind": "bytes",
                "length": len(raw_value),
                "digest": stable_digest(raw_value.hex()),
            }
        else:
            result[key] = raw_value
    return result


@dataclass(frozen=True, slots=True)
class ArtifactDeclaration:
    name: str
    relative_path: str
    media_type: str = "application/json"
    required: bool = True
    maximum_bytes: int = 16 * 1024 * 1024
    contains_secrets: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_identifier(self.name, field_name="artifact name"))
        object.__setattr__(
            self,
            "relative_path",
            validate_relative_path(self.relative_path, field_name="artifact path"),
        )
        if self.maximum_bytes < 1:
            raise ContractError("artifact maximum_bytes must be positive")
        if not self.media_type.strip():
            raise ContractError("artifact media_type is required")
        if self.contains_secrets:
            raise ContractError("regression artifacts may never declare secret content")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "required": self.required,
            "maximum_bytes": self.maximum_bytes,
            "contains_secrets": False,
        }


@dataclass(frozen=True, slots=True)
class CaseSpec:
    case_id: str
    version: str
    title: str
    tags: tuple[str, ...]
    timeout_seconds: float
    isolation: IsolationMode
    dependencies: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    artifacts: tuple[ArtifactDeclaration, ...] = ()
    mutation_required: bool = False
    disable_required: bool = False
    clean_state_required: bool = False
    security_required: bool = False
    maximum_attempts: int = 1
    exclusive_resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", validate_identifier(self.case_id, field_name="case_id"))
        if not self.version.strip():
            raise ContractError("case version is required")
        if not self.title.strip():
            raise ContractError("case title is required")
        if not self.tags:
            raise ContractError(f"{self.case_id} must have at least one tag")
        normalized_tags = tuple(
            sorted({validate_identifier(item, field_name="tag") for item in self.tags})
        )
        object.__setattr__(self, "tags", normalized_tags)
        dependencies = tuple(
            dict.fromkeys(
                validate_identifier(item, field_name="dependency")
                for item in self.dependencies
            )
        )
        if self.case_id in dependencies:
            raise ContractError(f"{self.case_id} cannot depend on itself")
        object.__setattr__(self, "dependencies", dependencies)
        capabilities = tuple(
            sorted(
                {
                    validate_identifier(item, field_name="capability")
                    for item in self.required_capabilities
                }
            )
        )
        object.__setattr__(self, "required_capabilities", capabilities)
        if self.timeout_seconds <= 0 or self.timeout_seconds > 3_600:
            raise ContractError(f"{self.case_id} timeout must be in (0, 3600]")
        if self.maximum_attempts < 1 or self.maximum_attempts > 3:
            raise ContractError(f"{self.case_id} maximum_attempts must be in [1, 3]")
        artifact_names = [item.name for item in self.artifacts]
        artifact_paths = [item.relative_path for item in self.artifacts]
        if len(set(artifact_names)) != len(artifact_names):
            raise ContractError(f"{self.case_id} has duplicate artifact names")
        if len(set(artifact_paths)) != len(artifact_paths):
            raise ContractError(f"{self.case_id} has duplicate artifact paths")
        object.__setattr__(
            self,
            "exclusive_resources",
            tuple(
                sorted(
                    {
                        validate_identifier(item, field_name="exclusive resource")
                        for item in self.exclusive_resources
                    }
                )
            ),
        )
        if self.isolation is IsolationMode.READ_ONLY and (
            self.mutation_required or self.clean_state_required
        ):
            raise ContractError(
                f"{self.case_id} cannot require mutation/clean-state in read-only isolation"
            )

    @property
    def identity(self) -> str:
        return f"{self.case_id}@{self.version}"

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "version": self.version,
            "title": self.title,
            "tags": list(self.tags),
            "timeout_seconds": self.timeout_seconds,
            "isolation": self.isolation.value,
            "dependencies": list(self.dependencies),
            "required_capabilities": list(self.required_capabilities),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "mutation_required": self.mutation_required,
            "disable_required": self.disable_required,
            "clean_state_required": self.clean_state_required,
            "security_required": self.security_required,
            "maximum_attempts": self.maximum_attempts,
            "exclusive_resources": list(self.exclusive_resources),
        }


@dataclass(frozen=True, slots=True)
class Observation:
    observation_id: str
    kind: ObservationKind
    subject: str
    status: str
    started_monotonic: float
    completed_monotonic: float
    attributes: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    causation_id: str = ""
    run_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observation_id",
            validate_identifier(self.observation_id, field_name="observation_id"),
        )
        if not self.subject.strip() or not self.status.strip():
            raise ContractError("observation subject and status are required")
        if self.completed_monotonic < self.started_monotonic:
            raise ContractError("observation completion precedes its start")
        object.__setattr__(self, "attributes", _public_mapping(self.attributes))

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.completed_monotonic - self.started_monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "kind": self.kind.value,
            "subject": self.subject,
            "status": self.status,
            "started_monotonic": round(self.started_monotonic, 6),
            "completed_monotonic": round(self.completed_monotonic, 6),
            "duration_seconds": round(self.duration_seconds, 6),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "run_id": self.run_id,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class AssertionResult:
    assertion_id: str
    passed: bool
    severity: AssertionSeverity
    summary: str
    evidence_ids: tuple[str, ...] = ()
    failure_kind: FailureKind = FailureKind.ASSERTION
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assertion_id",
            validate_identifier(self.assertion_id, field_name="assertion_id"),
        )
        if not self.summary.strip():
            raise ContractError("assertion summary is required")
        object.__setattr__(self, "evidence_ids", tuple(dict.fromkeys(self.evidence_ids)))
        object.__setattr__(self, "details", _public_mapping(self.details))

    @property
    def blocking(self) -> bool:
        return not self.passed and self.severity is AssertionSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "passed": self.passed,
            "severity": self.severity.value,
            "blocking": self.blocking,
            "summary": self.summary,
            "evidence_ids": list(self.evidence_ids),
            "failure_kind": self.failure_kind.value,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    name: str
    relative_path: str
    media_type: str
    size_bytes: int
    digest: str
    redacted: bool

    def __post_init__(self) -> None:
        validate_identifier(self.name, field_name="artifact receipt name")
        validate_relative_path(self.relative_path, field_name="artifact receipt path")
        if self.size_bytes < 0:
            raise ContractError("artifact size cannot be negative")
        if not _DIGEST.fullmatch(self.digest):
            raise ContractError("artifact receipt has invalid SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "redacted": self.redacted,
        }


@dataclass(frozen=True, slots=True)
class AttemptReceipt:
    attempt: int
    status: CaseStatus
    started_at: str
    completed_at: str
    duration_seconds: float
    observations: tuple[Observation, ...]
    assertions: tuple[AssertionResult, ...]
    artifacts: tuple[ArtifactReceipt, ...] = ()
    failure_message: str = ""
    failure_kind: FailureKind | None = None

    @property
    def passed(self) -> bool:
        return (
            self.status is CaseStatus.PASSED
            and bool(self.assertions)
            and all(item.passed for item in self.assertions)
        )

    def validate(self, spec: CaseSpec) -> None:
        if self.attempt < 1 or self.attempt > spec.maximum_attempts:
            raise ContractError(f"{spec.case_id} attempt is outside declared range")
        observation_ids = [item.observation_id for item in self.observations]
        if len(set(observation_ids)) != len(observation_ids):
            raise ContractError(f"{spec.case_id} produced duplicate observation IDs")
        known = set(observation_ids)
        for assertion in self.assertions:
            missing = set(assertion.evidence_ids) - known
            if missing:
                raise ContractError(
                    f"{spec.case_id}/{assertion.assertion_id} references unknown observations: "
                    f"{sorted(missing)}"
                )
        declared_artifacts = {item.name for item in spec.artifacts}
        actual_artifacts = {item.name for item in self.artifacts}
        undeclared = actual_artifacts - declared_artifacts
        if undeclared:
            raise ContractError(f"{spec.case_id} produced undeclared artifacts: {sorted(undeclared)}")
        required = {item.name for item in spec.artifacts if item.required}
        if self.status is CaseStatus.PASSED and required - actual_artifacts:
            raise ContractError(
                f"{spec.case_id} passed without required artifacts: "
                f"{sorted(required - actual_artifacts)}"
            )
        if self.status is CaseStatus.PASSED and not self.assertions:
            raise ContractError(f"{spec.case_id} cannot pass without assertions")
        if self.status is CaseStatus.PASSED and any(not item.passed for item in self.assertions):
            raise ContractError(f"{spec.case_id} passed with a failed assertion")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "status": self.status.value,
            "passed": self.passed,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 6),
            "observations": [item.to_dict() for item in self.observations],
            "assertions": [item.to_dict() for item in self.assertions],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "failure_message": bounded_text(self.failure_message),
            "failure_kind": self.failure_kind.value if self.failure_kind else "",
        }


@dataclass(frozen=True, slots=True)
class CaseReceipt:
    spec: CaseSpec
    shard_id: str
    input_digest: str
    isolation_id: str
    attempts: tuple[AttemptReceipt, ...]
    dependency_receipts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_identifier(self.shard_id, field_name="shard_id")
        if not _DIGEST.fullmatch(self.input_digest):
            raise ContractError("case input digest is invalid")
        if not self.isolation_id.strip():
            raise ContractError("case isolation identity is required")
        if not self.attempts:
            raise ContractError("case receipt requires at least one attempt")
        for attempt in self.attempts:
            attempt.validate(self.spec)

    @property
    def final_attempt(self) -> AttemptReceipt:
        return self.attempts[-1]

    @property
    def status(self) -> CaseStatus:
        return self.final_attempt.status

    @property
    def passed(self) -> bool:
        return self.final_attempt.passed

    @property
    def digest(self) -> str:
        return stable_digest(self.material())

    def material(self) -> dict[str, Any]:
        return {
            "spec_digest": self.spec.digest,
            "shard_id": self.shard_id,
            "input_digest": self.input_digest,
            "isolation_id": self.isolation_id,
            "attempts": [item.to_dict() for item in self.attempts],
            "dependency_receipts": dict(sorted(self.dependency_receipts.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": f"{SCHEMA_VERSION}/case-receipt",
            "case": self.spec.to_dict(),
            **self.material(),
            "status": self.status.value,
            "passed": self.passed,
            "receipt_digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class SuiteProfile:
    profile_id: str
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    case_ids: tuple[str, ...] = ()
    shard_count: int = 1
    maximum_workers: int = 1
    fail_fast: bool = False
    retry_transient: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_id",
            validate_identifier(self.profile_id, field_name="profile_id"),
        )
        if self.shard_count < 1 or self.shard_count > 128:
            raise ContractError("shard_count must be in [1, 128]")
        if self.maximum_workers < 1 or self.maximum_workers > 64:
            raise ContractError("maximum_workers must be in [1, 64]")
        object.__setattr__(
            self,
            "case_ids",
            tuple(dict.fromkeys(validate_identifier(item, field_name="case_id") for item in self.case_ids)),
        )
        object.__setattr__(
            self,
            "include_tags",
            tuple(sorted({validate_identifier(item, field_name="tag") for item in self.include_tags})),
        )
        object.__setattr__(
            self,
            "exclude_tags",
            tuple(sorted({validate_identifier(item, field_name="tag") for item in self.exclude_tags})),
        )
        if set(self.include_tags) & set(self.exclude_tags):
            raise ContractError("profile cannot include and exclude the same tag")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "include_tags": list(self.include_tags),
            "exclude_tags": list(self.exclude_tags),
            "case_ids": list(self.case_ids),
            "shard_count": self.shard_count,
            "maximum_workers": self.maximum_workers,
            "fail_fast": self.fail_fast,
            "retry_transient": self.retry_transient,
        }


@dataclass(frozen=True, slots=True)
class SuiteReceipt:
    suite_id: str
    suite_version: str
    profile: SuiteProfile
    revision: str
    started_at: str
    completed_at: str
    duration_seconds: float
    cases: tuple[CaseReceipt, ...]
    registry_digest: str
    artifact_root: str
    cancelled: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_identifier(self.suite_id, field_name="suite_id")
        if len(self.revision) != 40 or any(char not in "0123456789abcdef" for char in self.revision):
            raise ContractError("suite revision must be an exact lowercase Git SHA")
        case_ids = [item.spec.case_id for item in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ContractError("suite receipt has duplicate case receipts")
        object.__setattr__(self, "metadata", _public_mapping(self.metadata))

    @property
    def passed(self) -> bool:
        return bool(self.cases) and not self.cancelled and all(item.passed for item in self.cases)

    @property
    def status_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.status.value for item in self.cases).items()))

    @property
    def digest(self) -> str:
        return stable_digest(self.material())

    def material(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "profile": self.profile.to_dict(),
            "revision": self.revision,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 6),
            "cases": [item.to_dict() for item in self.cases],
            "registry_digest": self.registry_digest,
            "artifact_root": self.artifact_root,
            "cancelled": self.cancelled,
            "metadata": dict(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": f"{SCHEMA_VERSION}/suite-receipt",
            **self.material(),
            "passed": self.passed,
            "status_counts": self.status_counts,
            "receipt_digest": self.digest,
        }

    def verify_digest(self, claimed: str) -> None:
        if claimed != self.digest:
            raise ContractError(
                f"suite receipt digest mismatch: expected {self.digest}, got {claimed}"
            )


@dataclass(slots=True)
class CaseExecutionBuffer:
    observations: list[Observation] = field(default_factory=list)
    assertions: list[AssertionResult] = field(default_factory=list)
    started_monotonic: float = field(default_factory=time.monotonic)

    def observe(
        self,
        observation_id: str,
        kind: ObservationKind,
        subject: str,
        status: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        correlation_id: str = "",
        causation_id: str = "",
        run_id: str = "",
        started: float | None = None,
    ) -> Observation:
        completed = time.monotonic()
        observation = Observation(
            observation_id=observation_id,
            kind=kind,
            subject=subject,
            status=status,
            started_monotonic=started if started is not None else completed,
            completed_monotonic=completed,
            attributes=attributes or {},
            correlation_id=correlation_id,
            causation_id=causation_id,
            run_id=run_id,
        )
        self.observations.append(observation)
        return observation

    def assert_that(
        self,
        assertion_id: str,
        condition: bool,
        summary: str,
        *,
        severity: AssertionSeverity = AssertionSeverity.BLOCKER,
        evidence: Sequence[str] = (),
        failure_kind: FailureKind = FailureKind.ASSERTION,
        details: Mapping[str, Any] | None = None,
    ) -> AssertionResult:
        assertion = AssertionResult(
            assertion_id=assertion_id,
            passed=bool(condition),
            severity=severity,
            summary=summary,
            evidence_ids=tuple(evidence),
            failure_kind=failure_kind,
            details=details or {},
        )
        self.assertions.append(assertion)
        return assertion

    def renamed(self, prefix: str) -> "CaseExecutionBuffer":
        validated = validate_identifier(prefix, field_name="buffer prefix")
        observations = [
            replace(item, observation_id=f"{validated}.{item.observation_id}")
            for item in self.observations
        ]
        mapping = {
            old.observation_id: new.observation_id
            for old, new in zip(self.observations, observations, strict=True)
        }
        assertions = [
            replace(
                item,
                assertion_id=f"{validated}.{item.assertion_id}",
                evidence_ids=tuple(mapping.get(value, value) for value in item.evidence_ids),
            )
            for item in self.assertions
        ]
        return CaseExecutionBuffer(
            observations=observations,
            assertions=assertions,
            started_monotonic=self.started_monotonic,
        )


__all__ = [
    "ArtifactDeclaration",
    "ArtifactReceipt",
    "AssertionResult",
    "AssertionSeverity",
    "AttemptReceipt",
    "CaseExecutionBuffer",
    "CaseReceipt",
    "CaseSpec",
    "CaseStatus",
    "ContractError",
    "FailureKind",
    "IsolationMode",
    "Observation",
    "ObservationKind",
    "SCHEMA_VERSION",
    "SuiteProfile",
    "SuiteReceipt",
    "bounded_text",
    "canonical_json",
    "stable_digest",
    "utc_now",
    "validate_identifier",
    "validate_relative_path",
]
