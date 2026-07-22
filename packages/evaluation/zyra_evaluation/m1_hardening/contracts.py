from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def audit_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class SourceRole(StrEnum):
    PRIMARY = "primary"
    SUPPLEMENTARY = "supplementary"
    CONFORMANCE = "conformance"
    REFERENCE = "reference"
    EXPERIMENTAL = "experimental"
    DEFERRED = "deferred"
    REJECTED = "rejected"

    @property
    def production_bearing(self) -> bool:
        return self in {self.PRIMARY, self.SUPPLEMENTARY}


class Maturity(StrEnum):
    ACTIVE_REAL = "active_real"
    CONFORMANCE_VERIFIED = "conformance_verified"
    SOURCE_INACTIVE = "source_inactive"
    EXPERIMENTAL = "experimental"
    DEFERRED = "deferred"
    DEBT = "debt"
    BLOCKED = "blocked"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"

    @property
    def failing(self) -> bool:
        return self in {self.ERROR, self.BLOCKER}


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PARTIAL = "partial"
    NOT_RUN = "not_run"


class ProbeStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class EvidencePointer:
    kind: str
    location: str
    summary: str
    revision: str = ""
    causation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "location": self.location,
            "summary": self.summary,
            "revision": self.revision,
            "causation_id": self.causation_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    summary: str
    detail: str = ""
    capability: str = ""
    location: str = ""
    remediation: str = ""
    evidence: tuple[EvidencePointer, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "summary": self.summary,
            "detail": self.detail,
            "capability": self.capability,
            "location": self.location,
            "remediation": self.remediation,
            "evidence": [item.to_dict() for item in self.evidence],
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class GateResult:
    gate_id: str
    status: GateStatus
    summary: str
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidencePointer] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now)
    completed_at: str = ""
    limitations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is GateStatus.PASSED

    @property
    def blocker_count(self) -> int:
        return sum(1 for item in self.findings if item.severity is Severity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.findings if item.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.findings if item.severity is Severity.WARNING)

    def finish(self, *, default_partial: bool = False) -> GateResult:
        self.completed_at = self.completed_at or utc_now()
        if self.blocker_count:
            self.status = GateStatus.BLOCKED
        elif self.error_count:
            self.status = GateStatus.FAILED
        elif default_partial and self.limitations:
            self.status = GateStatus.PARTIAL
        elif self.status is GateStatus.NOT_RUN:
            self.status = GateStatus.PASSED
        return self

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "status": self.status.value,
            "ok": self.ok,
            "summary": self.summary,
            "counts": {
                "blockers": self.blocker_count,
                "errors": self.error_count,
                "warnings": self.warning_count,
                "findings": len(self.findings),
            },
            "metrics": dict(self.metrics),
            "findings": [item.to_dict() for item in self.findings],
            "evidence": [item.to_dict() for item in self.evidence],
            "limitations": list(self.limitations),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class SourceCoverageItem:
    capability_id: str
    source_repository: str
    source_paths: tuple[str, ...]
    source_language: str
    role: SourceRole
    maturity: Maturity
    target_paths: tuple[str, ...]
    target_language: str
    canonical_owner: str
    production_entries: tuple[str, ...] = ()
    state_owners: tuple[str, ...] = ()
    scenario_ids: tuple[str, ...] = ()
    disable_probe_ids: tuple[str, ...] = ()
    cleanroom_decision: str = ""
    limitations: tuple[str, ...] = ()
    next_owner: str = ""
    evidence: tuple[EvidencePointer, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "source_repository": self.source_repository,
            "source_paths": list(self.source_paths),
            "source_language": self.source_language,
            "role": self.role.value,
            "maturity": self.maturity.value,
            "target_paths": list(self.target_paths),
            "target_language": self.target_language,
            "canonical_owner": self.canonical_owner,
            "production_entries": list(self.production_entries),
            "state_owners": list(self.state_owners),
            "scenario_ids": list(self.scenario_ids),
            "disable_probe_ids": list(self.disable_probe_ids),
            "cleanroom_decision": self.cleanroom_decision,
            "limitations": list(self.limitations),
            "next_owner": self.next_owner,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class StateCustodyEntry:
    state_family: str
    canonical_owner: str
    schema_paths: tuple[str, ...]
    store_paths: tuple[str, ...]
    write_entries: tuple[str, ...]
    read_entries: tuple[str, ...]
    restore_entries: tuple[str, ...]
    event_types: tuple[str, ...]
    revision_fields: tuple[str, ...]
    idempotency_fields: tuple[str, ...]
    correlation_fields: tuple[str, ...]
    derivative_consumers: tuple[str, ...] = ()
    forbidden_owners: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_family": self.state_family,
            "canonical_owner": self.canonical_owner,
            "schema_paths": list(self.schema_paths),
            "store_paths": list(self.store_paths),
            "write_entries": list(self.write_entries),
            "read_entries": list(self.read_entries),
            "restore_entries": list(self.restore_entries),
            "event_types": list(self.event_types),
            "revision_fields": list(self.revision_fields),
            "idempotency_fields": list(self.idempotency_fields),
            "correlation_fields": list(self.correlation_fields),
            "derivative_consumers": list(self.derivative_consumers),
            "forbidden_owners": list(self.forbidden_owners),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class TransitionObservation:
    transition_id: str
    run_id: str
    task_id: str
    event_type: str
    before_revision: int | str | None
    after_revision: int | str | None
    causation_id: str
    semantic_family: str
    semantic_key: str
    before_digest: str
    after_digest: str
    action_id: str = ""
    actor_id: str = ""
    timestamp: str = ""
    payload_bytes: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "causation_id": self.causation_id,
            "semantic_family": self.semantic_family,
            "semantic_key": self.semantic_key,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "action_id": self.action_id,
            "actor_id": self.actor_id,
            "timestamp": self.timestamp,
            "payload_bytes": self.payload_bytes,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class EnvelopeObservation:
    message_id: str
    policy: str
    route_kind: str
    envelope_bytes: int
    inline_bytes: int
    source_bytes: int
    summary_bytes: int
    artifact_refs: int
    recipient_count: int
    eligible_recipient_count: int
    token_count: int
    duplicate_fact_count: int = 0
    fact_count: int = 0
    redelivery_count: int = 0
    effective_transition_count: int = 0
    task_succeeded: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "policy": self.policy,
            "route_kind": self.route_kind,
            "envelope_bytes": self.envelope_bytes,
            "inline_bytes": self.inline_bytes,
            "source_bytes": self.source_bytes,
            "summary_bytes": self.summary_bytes,
            "artifact_refs": self.artifact_refs,
            "recipient_count": self.recipient_count,
            "eligible_recipient_count": self.eligible_recipient_count,
            "token_count": self.token_count,
            "duplicate_fact_count": self.duplicate_fact_count,
            "fact_count": self.fact_count,
            "redelivery_count": self.redelivery_count,
            "effective_transition_count": self.effective_transition_count,
            "task_succeeded": self.task_succeeded,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class HardeningContext:
    project_root: Path
    workspace_root: Path | None = None
    artifact_root: Path | None = None
    task: Mapping[str, Any] | None = None
    events: Sequence[Mapping[str, Any]] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    def resolved_project_root(self) -> Path:
        return self.project_root.resolve()

    def resolved_workspace_root(self) -> Path:
        return (self.workspace_root or self.project_root).resolve()

    def resolved_artifact_root(self) -> Path:
        return (self.artifact_root or self.project_root / ".tmp" / "m1-hardening").resolve()


@dataclass(slots=True)
class HardeningReport:
    report_id: str
    baseline_commit: str
    project_root: str
    gates: list[GateResult]
    generated_at: str = field(default_factory=utc_now)
    scenario_id: str = ""
    task_id: str = ""
    run_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blockers(self) -> int:
        return sum(item.blocker_count for item in self.gates)

    @property
    def failures(self) -> int:
        return sum(1 for item in self.gates if item.status in {GateStatus.FAILED, GateStatus.BLOCKED})

    @property
    def ok(self) -> bool:
        return self.blockers == 0 and self.failures == 0

    def gate(self, gate_id: str) -> GateResult | None:
        return next((item for item in self.gates if item.gate_id == gate_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m1-hardening-report/v1",
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "baseline_commit": self.baseline_commit,
            "project_root": self.project_root,
            "scenario_id": self.scenario_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "ok": self.ok,
            "blockers": self.blockers,
            "failures": self.failures,
            "gates": [item.to_dict() for item in self.gates],
            "metadata": dict(self.metadata),
        }


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def as_sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else ()


def strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in values if str(item).strip())
