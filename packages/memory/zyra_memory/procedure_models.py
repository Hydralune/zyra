from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import now_iso


PROCEDURE_PROTOCOL = "zyra.reusable-procedure/v1"
PROCEDURE_MINING_PROTOCOL = "zyra.reusable-procedure-mining/v1"
PROCEDURE_STORE_PROTOCOL = "zyra.reusable-procedure-store/v1"
PROCEDURE_SIGNAL_PROTOCOL = "zyra.procedure-signal/v1"


class ProcedureContractError(ValueError):
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class ProcedureStoreConflictError(RuntimeError):
    pass


class ProcedureMiningError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ProcedureValidationStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ProcedureConsumer(StrEnum):
    ROUTING = "routing"
    RECOVERY = "recovery"
    CONTEXT = "context"
    AUDIT = "audit"


class ProcedureStepState(StrEnum):
    OBSERVED = "observed"
    VERIFIED = "verified"
    FAILED = "failed"
    OMITTED = "omitted"


class ProcedureEvidenceKind(StrEnum):
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    EVENT = "event"
    MEMORY = "memory"
    SKILL_VERSION = "skill_version"
    PERMISSION = "permission"
    VERIFICATION = "verification"


class ProcedureMiningDisposition(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    UPDATED = "updated"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class ProcedureSignalKind(StrEnum):
    MINED = "procedure_mined"
    VALIDATED = "procedure_validated"
    REJECTED = "procedure_rejected"
    SUPERSEDED = "procedure_superseded"


def canonical_json(value: object) -> str:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *values: object) -> str:
    return f"{prefix}-{stable_digest(values)[:32]}"


def mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def unique_strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(text for item in values if (text := str(item or "").strip())))


def required(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProcedureContractError("procedure_required_value", f"{label} is required", {"label": label})
    return text


def optional(value: object) -> str:
    return str(value or "").strip()


def sha256_digest(value: object, label: str) -> str:
    text = required(value, label).removeprefix("sha256:").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ProcedureContractError("procedure_digest_invalid", f"{label} must be a sha256 digest")
    return text


def timestamp(value: object, label: str) -> str:
    text = required(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProcedureContractError("procedure_timestamp_invalid", f"{label} must be ISO-8601") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def non_negative_integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ProcedureContractError("procedure_integer_invalid", f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ProcedureContractError("procedure_integer_invalid", f"{label} must be an integer") from error
    if number < 0:
        raise ProcedureContractError("procedure_integer_invalid", f"{label} must be non-negative")
    return number


def finite_number(value: object, label: str, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ProcedureContractError("procedure_number_invalid", f"{label} must be numeric") from error
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ProcedureContractError(
            "procedure_number_invalid",
            f"{label} must be between {minimum} and {maximum}",
        )
    return number


@dataclass(frozen=True, slots=True)
class ProcedureEvidenceRef:
    evidence_id: str
    kind: ProcedureEvidenceKind
    source_id: str
    source_digest: str
    sequence: int
    occurred_at: str
    tool_name: str = ""
    tool_call_id: str = ""
    artifact_id: str = ""
    event_id: str = ""
    memory_id: str = ""
    trusted_runtime: bool = False
    canonical: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> ProcedureEvidenceRef:
        required(self.evidence_id, "procedure evidence id")
        required(self.source_id, "procedure evidence source id")
        sha256_digest(self.source_digest, "procedure evidence source digest")
        non_negative_integer(self.sequence, "procedure evidence sequence")
        timestamp(self.occurred_at, "procedure evidence timestamp")
        if self.kind is ProcedureEvidenceKind.TOOL_CALL and not self.tool_call_id:
            raise ProcedureContractError(
                "procedure_tool_call_provenance_missing",
                "tool-call evidence requires tool_call_id",
            )
        if self.kind is ProcedureEvidenceKind.ARTIFACT and not self.artifact_id:
            raise ProcedureContractError(
                "procedure_artifact_provenance_missing",
                "artifact evidence requires artifact_id",
            )
        if self.kind is ProcedureEvidenceKind.MEMORY and not self.memory_id:
            raise ProcedureContractError(
                "procedure_memory_provenance_missing",
                "memory evidence requires memory_id",
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "source_id": self.source_id,
            "source_digest": self.source_digest,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "artifact_id": self.artifact_id,
            "event_id": self.event_id,
            "memory_id": self.memory_id,
            "trusted_runtime": self.trusted_runtime,
            "canonical": self.canonical,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcedureEvidenceRef:
        return cls(
            evidence_id=optional(value.get("evidence_id")),
            kind=ProcedureEvidenceKind(str(value.get("kind") or ProcedureEvidenceKind.EVENT.value)),
            source_id=optional(value.get("source_id")),
            source_digest=optional(value.get("source_digest")),
            sequence=int(value.get("sequence", 0)),
            occurred_at=optional(value.get("occurred_at")) or now_iso(),
            tool_name=optional(value.get("tool_name")),
            tool_call_id=optional(value.get("tool_call_id")),
            artifact_id=optional(value.get("artifact_id")),
            event_id=optional(value.get("event_id")),
            memory_id=optional(value.get("memory_id")),
            trusted_runtime=bool(value.get("trusted_runtime", False)),
            canonical=bool(value.get("canonical", False)),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class ProcedureStep:
    step_id: str
    ordinal: int
    action: str
    expected_effect: str
    tool_name: str = ""
    input_shape_digest: str = ""
    success_evidence_ids: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()
    retryable: bool = False
    failure_routes: tuple[str, ...] = ()
    state: ProcedureStepState = ProcedureStepState.OBSERVED
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> ProcedureStep:
        required(self.step_id, "procedure step id")
        if self.ordinal < 1:
            raise ProcedureContractError("procedure_step_ordinal", "procedure step ordinal must be positive")
        required(self.action, "procedure step action")
        required(self.expected_effect, "procedure step expected effect")
        if self.input_shape_digest:
            sha256_digest(self.input_shape_digest, "procedure step input shape digest")
        if self.state is ProcedureStepState.VERIFIED and not self.success_evidence_ids:
            raise ProcedureContractError(
                "procedure_step_verification_evidence",
                "verified procedure step requires success evidence",
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "ordinal": self.ordinal,
            "action": self.action,
            "tool_name": self.tool_name or None,
            "input_shape_digest": self.input_shape_digest or None,
            "expected_effect": self.expected_effect,
            "success_evidence_ids": list(self.success_evidence_ids),
            "artifact_kinds": list(self.artifact_kinds),
            "retryable": self.retryable,
            "failure_routes": list(self.failure_routes),
            "state": self.state.value,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcedureStep:
        return cls(
            step_id=optional(value.get("step_id")),
            ordinal=int(value.get("ordinal", 0)),
            action=optional(value.get("action")),
            tool_name=optional(value.get("tool_name")),
            input_shape_digest=optional(value.get("input_shape_digest")),
            expected_effect=optional(value.get("expected_effect")),
            success_evidence_ids=unique_strings(value.get("success_evidence_ids") or ()),
            artifact_kinds=unique_strings(value.get("artifact_kinds") or ()),
            retryable=bool(value.get("retryable", False)),
            failure_routes=unique_strings(value.get("failure_routes") or ()),
            state=ProcedureStepState(str(value.get("state") or ProcedureStepState.OBSERVED.value)),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class ProcedureApplicability:
    languages: tuple[str, ...] = ()
    workspace_kinds: tuple[str, ...] = ()
    goal_patterns: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    required_artifact_kinds: tuple[str, ...] = ()
    provider_capabilities: tuple[str, ...] = ()
    minimum_trust: str = "verified"
    constraints: tuple[Mapping[str, Any], ...] = ()

    def validated(self) -> ProcedureApplicability:
        if self.minimum_trust not in {"internal", "verified"}:
            raise ProcedureContractError(
                "procedure_minimum_trust",
                "procedure minimum trust must be internal or verified",
            )
        overlap = set(self.required_tools) & set(self.forbidden_tools)
        if overlap:
            raise ProcedureContractError(
                "procedure_tool_applicability_conflict",
                "required and forbidden tools overlap",
                {"tools": sorted(overlap)},
            )
        for pattern in self.goal_patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ProcedureContractError(
                    "procedure_goal_pattern_invalid",
                    f"invalid goal pattern: {pattern}",
                ) from error
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "languages": list(self.languages),
            "workspace_kinds": list(self.workspace_kinds),
            "goal_patterns": list(self.goal_patterns),
            "required_tools": list(self.required_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "required_artifact_kinds": list(self.required_artifact_kinds),
            "provider_capabilities": list(self.provider_capabilities),
            "minimum_trust": self.minimum_trust,
            "constraints": [dict(item) for item in self.constraints],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcedureApplicability:
        return cls(
            languages=unique_strings(value.get("languages") or ()),
            workspace_kinds=unique_strings(value.get("workspace_kinds") or ()),
            goal_patterns=unique_strings(value.get("goal_patterns") or ()),
            required_tools=unique_strings(value.get("required_tools") or ()),
            forbidden_tools=unique_strings(value.get("forbidden_tools") or ()),
            required_artifact_kinds=unique_strings(value.get("required_artifact_kinds") or ()),
            provider_capabilities=unique_strings(value.get("provider_capabilities") or ()),
            minimum_trust=optional(value.get("minimum_trust")) or "verified",
            constraints=mapping_sequence(value.get("constraints")),
        ).validated()


@dataclass(frozen=True, slots=True)
class ProcedureProvenance:
    curator_outcome_id: str
    curator_job_id: str
    curator_decision_id: str
    evidence_bundle_id: str
    evidence_digest: str
    memory_id: str
    memory_revision: int
    run_id: str
    task_id: str
    session_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    skill_versions: tuple[Mapping[str, Any], ...]
    memory_event_ids: tuple[str, ...]

    def validated(self) -> ProcedureProvenance:
        required(self.curator_outcome_id, "curator outcome id")
        required(self.curator_job_id, "curator job id")
        required(self.curator_decision_id, "curator decision id")
        required(self.evidence_bundle_id, "curator evidence bundle id")
        sha256_digest(self.evidence_digest, "curator evidence digest")
        required(self.memory_id, "canonical memory id")
        if self.memory_revision < 1:
            raise ProcedureContractError(
                "procedure_memory_revision",
                "procedure provenance requires a positive canonical memory revision",
            )
        required(self.run_id, "procedure run id")
        required(self.task_id, "procedure task id")
        if not self.memory_event_ids:
            raise ProcedureContractError(
                "procedure_memory_event_provenance",
                "procedure provenance requires at least one memory event id",
            )
        for version in self.skill_versions:
            _validate_skill_version(version)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "curator_outcome_id": self.curator_outcome_id,
            "curator_job_id": self.curator_job_id,
            "curator_decision_id": self.curator_decision_id,
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_digest": self.evidence_digest,
            "memory_id": self.memory_id,
            "memory_revision": self.memory_revision,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_ids": list(self.session_ids),
            "tool_call_ids": list(self.tool_call_ids),
            "artifact_ids": list(self.artifact_ids),
            "skill_versions": [dict(item) for item in self.skill_versions],
            "memory_event_ids": list(self.memory_event_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcedureProvenance:
        return cls(
            curator_outcome_id=optional(value.get("curator_outcome_id")),
            curator_job_id=optional(value.get("curator_job_id")),
            curator_decision_id=optional(value.get("curator_decision_id")),
            evidence_bundle_id=optional(value.get("evidence_bundle_id")),
            evidence_digest=optional(value.get("evidence_digest")),
            memory_id=optional(value.get("memory_id")),
            memory_revision=int(value.get("memory_revision", 0)),
            run_id=optional(value.get("run_id")),
            task_id=optional(value.get("task_id")),
            session_ids=unique_strings(value.get("session_ids") or ()),
            tool_call_ids=unique_strings(value.get("tool_call_ids") or ()),
            artifact_ids=unique_strings(value.get("artifact_ids") or ()),
            skill_versions=mapping_sequence(value.get("skill_versions")),
            memory_event_ids=unique_strings(value.get("memory_event_ids") or ()),
        ).validated()


@dataclass(frozen=True, slots=True)
class ReusableProcedure:
    procedure_id: str
    name: str
    summary: str
    state: ProcedureValidationStatus
    revision: int
    steps: tuple[ProcedureStep, ...]
    applicability: ProcedureApplicability
    provenance: ProcedureProvenance
    evidence: tuple[ProcedureEvidenceRef, ...]
    consumers: tuple[ProcedureConsumer, ...]
    confidence: float
    success_count: int
    failure_count: int
    validated_at: str
    created_at: str
    updated_at: str
    supersedes_procedure_id: str
    procedure_digest: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def semantic_projection(self) -> Mapping[str, Any]:
        return {
            "procedure_id": self.procedure_id,
            "protocol": PROCEDURE_PROTOCOL,
            "name": self.name,
            "summary": self.summary,
            "state": self.state.value,
            "revision": self.revision,
            "steps": [step.to_dict() for step in self.steps],
            "applicability": self.applicability.to_dict(),
            "provenance": self.provenance.to_dict(),
            "consumers": [consumer.value for consumer in self.consumers],
            "confidence": self.confidence,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "validated_at": self.validated_at or None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "supersedes_procedure_id": self.supersedes_procedure_id or None,
            "metadata": dict(self.metadata),
        }

    def validated(self) -> ReusableProcedure:
        required(self.procedure_id, "procedure id")
        required(self.name, "procedure name")
        required(self.summary, "procedure summary")
        if self.revision < 1:
            raise ProcedureContractError("procedure_revision", "procedure revision must be positive")
        if not self.steps:
            raise ProcedureContractError("procedure_steps", "procedure requires at least one step")
        ordinals = [step.validated().ordinal for step in self.steps]
        if ordinals != list(range(1, len(self.steps) + 1)):
            raise ProcedureContractError(
                "procedure_step_order",
                "procedure step ordinals must be contiguous and ordered",
            )
        self.applicability.validated()
        self.provenance.validated()
        for evidence in self.evidence:
            evidence.validated()
        evidence_ids = {item.evidence_id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ProcedureContractError(
                "procedure_duplicate_evidence",
                "procedure evidence ids must be unique",
            )
        for step in self.steps:
            missing = set(step.success_evidence_ids) - evidence_ids
            if missing:
                raise ProcedureContractError(
                    "procedure_step_evidence_missing",
                    f"procedure step {step.step_id} references missing evidence",
                    {"evidence_ids": sorted(missing)},
                )
        finite_number(self.confidence, "procedure confidence")
        non_negative_integer(self.success_count, "procedure success count")
        non_negative_integer(self.failure_count, "procedure failure count")
        timestamp(self.created_at, "procedure created timestamp")
        timestamp(self.updated_at, "procedure updated timestamp")
        if self.state is ProcedureValidationStatus.VALIDATED:
            timestamp(self.validated_at, "procedure validated timestamp")
            if not self.provenance.tool_call_ids:
                raise ProcedureContractError(
                    "procedure_tool_provenance",
                    "validated procedure requires tool-call provenance",
                )
            if not any(item.trusted_runtime for item in self.evidence):
                raise ProcedureContractError(
                    "procedure_trusted_evidence",
                    "validated procedure requires trusted runtime evidence",
                )
            if not all(step.state is ProcedureStepState.VERIFIED for step in self.steps):
                raise ProcedureContractError(
                    "procedure_unverified_step",
                    "validated procedure cannot contain unverified steps",
                )
        expected = stable_digest(self.semantic_projection())
        if expected != self.procedure_digest:
            raise ProcedureContractError(
                "procedure_digest_mismatch",
                "procedure digest does not match semantic projection",
            )
        return self

    @property
    def routable(self) -> bool:
        return (
            self.state is ProcedureValidationStatus.VALIDATED
            and ProcedureConsumer.ROUTING in self.consumers
        )

    @property
    def recoverable(self) -> bool:
        return (
            self.state is ProcedureValidationStatus.VALIDATED
            and ProcedureConsumer.RECOVERY in self.consumers
        )

    def to_dict(self, *, include_evidence: bool = True) -> dict[str, Any]:
        value = dict(self.semantic_projection())
        value["procedure_digest"] = self.procedure_digest
        if include_evidence:
            value["evidence"] = [item.to_dict() for item in self.evidence]
        return value

    def with_state(
        self,
        state: ProcedureValidationStatus,
        *,
        reason: str,
        validated_at: str = "",
        updated_at: str | None = None,
    ) -> ReusableProcedure:
        changed = replace(
            self,
            state=state,
            revision=self.revision + 1,
            validated_at=(validated_at if state is ProcedureValidationStatus.VALIDATED else ""),
            updated_at=updated_at or now_iso(),
            metadata={**dict(self.metadata), "state_reason": reason},
            procedure_digest="",
        )
        return replace(
            changed,
            procedure_digest=stable_digest(changed.semantic_projection()),
        ).validated()

    @classmethod
    def build(
        cls,
        *,
        name: str,
        summary: str,
        state: ProcedureValidationStatus,
        steps: Sequence[ProcedureStep],
        applicability: ProcedureApplicability,
        provenance: ProcedureProvenance,
        evidence: Sequence[ProcedureEvidenceRef],
        consumers: Sequence[ProcedureConsumer],
        confidence: float,
        success_count: int = 1,
        failure_count: int = 0,
        validated_at: str = "",
        supersedes_procedure_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> ReusableProcedure:
        created = created_at or now_iso()
        normalized_name = required(name, "procedure name")[:256]
        normalized_summary = required(summary, "procedure summary")[:16_000]
        ordered_steps = tuple(sorted((item.validated() for item in steps), key=lambda item: item.ordinal))
        normalized_evidence = tuple(
            sorted(
                (item.validated() for item in evidence),
                key=lambda item: (item.sequence, item.occurred_at, item.evidence_id),
            )
        )
        procedure_id = stable_id(
            "procedure",
            provenance.task_id,
            provenance.curator_outcome_id,
            provenance.evidence_digest,
            [step.action for step in ordered_steps],
        )
        value = cls(
            procedure_id=procedure_id,
            name=normalized_name,
            summary=normalized_summary,
            state=state,
            revision=1,
            steps=ordered_steps,
            applicability=applicability.validated(),
            provenance=provenance.validated(),
            evidence=normalized_evidence,
            consumers=tuple(dict.fromkeys(consumers)),
            confidence=finite_number(confidence, "procedure confidence"),
            success_count=non_negative_integer(success_count, "procedure success count"),
            failure_count=non_negative_integer(failure_count, "procedure failure count"),
            validated_at=(validated_at or created) if state is ProcedureValidationStatus.VALIDATED else "",
            created_at=created,
            updated_at=created,
            supersedes_procedure_id=optional(supersedes_procedure_id),
            procedure_digest="",
            metadata=dict(metadata or {}),
        )
        return replace(
            value,
            procedure_digest=stable_digest(value.semantic_projection()),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReusableProcedure:
        procedure = cls(
            procedure_id=optional(value.get("procedure_id")),
            name=optional(value.get("name")),
            summary=optional(value.get("summary")),
            state=ProcedureValidationStatus(
                str(value.get("state") or ProcedureValidationStatus.CANDIDATE.value)
            ),
            revision=int(value.get("revision", 0)),
            steps=tuple(
                ProcedureStep.from_dict(item)
                for item in mapping_sequence(value.get("steps"))
            ),
            applicability=ProcedureApplicability.from_dict(mapping(value.get("applicability"))),
            provenance=ProcedureProvenance.from_dict(mapping(value.get("provenance"))),
            evidence=tuple(
                ProcedureEvidenceRef.from_dict(item)
                for item in mapping_sequence(value.get("evidence"))
            ),
            consumers=tuple(
                ProcedureConsumer(str(item))
                for item in value.get("consumers") or ()
            ),
            confidence=float(value.get("confidence", 0.0)),
            success_count=int(value.get("success_count", 0)),
            failure_count=int(value.get("failure_count", 0)),
            validated_at=optional(value.get("validated_at")),
            created_at=optional(value.get("created_at")) or now_iso(),
            updated_at=optional(value.get("updated_at")) or now_iso(),
            supersedes_procedure_id=optional(value.get("supersedes_procedure_id")),
            procedure_digest=optional(value.get("procedure_digest")),
            metadata=mapping(value.get("metadata")),
        )
        return procedure.validated()


@dataclass(frozen=True, slots=True)
class ProcedureMiningReceipt:
    receipt_id: str
    outcome_id: str
    task_id: str
    disposition: ProcedureMiningDisposition
    procedure_id: str
    reason: str
    evidence_digest: str
    state_before: str
    state_after: str
    created_at: str
    receipt_digest: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> ProcedureMiningReceipt:
        required(self.receipt_id, "procedure mining receipt id")
        required(self.outcome_id, "procedure mining outcome id")
        required(self.task_id, "procedure mining task id")
        sha256_digest(self.evidence_digest, "procedure mining evidence digest")
        timestamp(self.created_at, "procedure mining receipt timestamp")
        unsigned = self.to_dict()
        unsigned.pop("receipt_digest")
        if stable_digest(unsigned) != self.receipt_digest:
            raise ProcedureContractError(
                "procedure_mining_receipt_digest",
                "procedure mining receipt digest mismatch",
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "outcome_id": self.outcome_id,
            "task_id": self.task_id,
            "disposition": self.disposition.value,
            "procedure_id": self.procedure_id,
            "reason": self.reason,
            "evidence_digest": self.evidence_digest,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "created_at": self.created_at,
            "receipt_digest": self.receipt_digest,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def build(
        cls,
        *,
        outcome_id: str,
        task_id: str,
        disposition: ProcedureMiningDisposition,
        procedure_id: str,
        reason: str,
        evidence_digest: str,
        state_before: str = "",
        state_after: str = "",
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> ProcedureMiningReceipt:
        created = created_at or now_iso()
        receipt_id = stable_id(
            "procedure-mining-receipt",
            outcome_id,
            disposition.value,
            procedure_id,
            evidence_digest,
        )
        unsigned = {
            "receipt_id": receipt_id,
            "outcome_id": outcome_id,
            "task_id": task_id,
            "disposition": disposition.value,
            "procedure_id": procedure_id,
            "reason": reason,
            "evidence_digest": evidence_digest,
            "state_before": state_before,
            "state_after": state_after,
            "created_at": created,
            "metadata": dict(metadata or {}),
        }
        return cls(
            receipt_id=receipt_id,
            outcome_id=required(outcome_id, "procedure mining outcome id"),
            task_id=required(task_id, "procedure mining task id"),
            disposition=disposition,
            procedure_id=optional(procedure_id),
            reason=required(reason, "procedure mining reason"),
            evidence_digest=sha256_digest(evidence_digest, "procedure mining evidence digest"),
            state_before=optional(state_before),
            state_after=optional(state_after),
            created_at=created,
            receipt_digest=stable_digest(unsigned),
            metadata=dict(metadata or {}),
        ).validated()


@dataclass(frozen=True, slots=True)
class ProcedureSignal:
    signal_id: str
    kind: ProcedureSignalKind
    run_id: str
    task_id: str
    procedure_id: str
    outcome_id: str
    causation_id: str
    payload: Mapping[str, Any]
    payload_digest: str
    created_at: str
    signal_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": PROCEDURE_SIGNAL_PROTOCOL,
            "signal_id": self.signal_id,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "procedure_id": self.procedure_id,
            "outcome_id": self.outcome_id,
            "causation_id": self.causation_id,
            "payload": dict(self.payload),
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
            "signal_digest": self.signal_digest,
        }

    @classmethod
    def build(
        cls,
        *,
        kind: ProcedureSignalKind,
        run_id: str,
        task_id: str,
        procedure_id: str,
        outcome_id: str,
        causation_id: str,
        payload: Mapping[str, Any],
        created_at: str | None = None,
    ) -> ProcedureSignal:
        created = created_at or now_iso()
        payload_value = dict(payload)
        payload_digest = stable_digest(payload_value)
        signal_id = stable_id(
            "procedure-signal",
            kind.value,
            task_id,
            procedure_id,
            outcome_id,
            payload_digest,
        )
        unsigned = {
            "protocol": PROCEDURE_SIGNAL_PROTOCOL,
            "signal_id": signal_id,
            "kind": kind.value,
            "run_id": run_id,
            "task_id": task_id,
            "procedure_id": procedure_id,
            "outcome_id": outcome_id,
            "causation_id": causation_id,
            "payload": payload_value,
            "payload_digest": payload_digest,
            "created_at": created,
        }
        return cls(
            signal_id=signal_id,
            kind=kind,
            run_id=required(run_id, "procedure signal run id"),
            task_id=required(task_id, "procedure signal task id"),
            procedure_id=required(procedure_id, "procedure signal procedure id"),
            outcome_id=required(outcome_id, "procedure signal outcome id"),
            causation_id=required(causation_id, "procedure signal causation id"),
            payload=payload_value,
            payload_digest=payload_digest,
            created_at=created,
            signal_digest=stable_digest(unsigned),
        )


def _canonical(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "to_dict"):
        return _canonical(value.to_dict())
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical(item) for item in value]
    return str(value)


def _validate_skill_version(value: Mapping[str, Any]) -> None:
    required(value.get("skill_id"), "procedure skill version skill id")
    required(value.get("skill_name"), "procedure skill version skill name")
    if int(value.get("registry_revision", 0)) < 1:
        raise ProcedureContractError(
            "procedure_skill_registry_revision",
            "procedure skill version registry revision must be positive",
        )
    sha256_digest(value.get("descriptor_digest"), "procedure skill descriptor digest")
    sha256_digest(value.get("body_digest"), "procedure skill body digest")
    resources = mapping(value.get("resource_digests"))
    for path, digest_value in resources.items():
        sha256_digest(digest_value, f"procedure skill resource digest {path}")


__all__ = [
    "PROCEDURE_MINING_PROTOCOL",
    "PROCEDURE_PROTOCOL",
    "PROCEDURE_SIGNAL_PROTOCOL",
    "PROCEDURE_STORE_PROTOCOL",
    "ProcedureApplicability",
    "ProcedureConsumer",
    "ProcedureContractError",
    "ProcedureEvidenceKind",
    "ProcedureEvidenceRef",
    "ProcedureMiningDisposition",
    "ProcedureMiningError",
    "ProcedureMiningReceipt",
    "ProcedureProvenance",
    "ProcedureSignal",
    "ProcedureSignalKind",
    "ProcedureStep",
    "ProcedureStepState",
    "ProcedureStoreConflictError",
    "ProcedureValidationStatus",
    "ReusableProcedure",
    "canonical_json",
    "mapping",
    "mapping_sequence",
    "stable_digest",
    "stable_id",
    "unique_strings",
]
