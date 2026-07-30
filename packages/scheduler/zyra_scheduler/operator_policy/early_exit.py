from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from zyra_core import DecisionRecord, PlanNodeStatus, TaskState, now_iso
from zyra_orchestration.topology_policy.contracts import (
    ContractHeader,
    FrozenDict,
    MemoryContinuityReceipt,
    PolicyInputSnapshot,
    PolicyOutcome,
    StableArtifactRef,
    canonical_digest,
    thaw_json,
)

from zyra_scheduler.recovery_runtime.checkpoint_runtime import (
    CheckpointCommitRequest,
)
from zyra_scheduler.recovery_runtime.contracts import (
    CheckpointPhase,
    RecoveryCheckpoint,
    SideEffectState,
)

from .selector import OperatorCandidate, OperatorSelectionProposal


EARLY_EXIT_CONFIG_SCHEMA = "zyra.maas-early-exit-config/v1"
EXIT_ELIGIBILITY_SCHEMA = "zyra.exit-eligibility-snapshot/v1"
EXIT_DECISION_SCHEMA = "zyra.early-exit-decision-receipt/v1"
EXIT_CHECKPOINT_BINDING_SCHEMA = "zyra.early-exit-checkpoint-binding/v1"
EXIT_RESTORE_SCHEMA = "zyra.early-exit-restore-validation/v1"
FINAL_VERIFIER_SCHEMA = "zyra.final-verifier-receipt/v1"
OPERATOR_EXECUTION_SCHEMA = "zyra.operator-execution-receipt/v1"


class EarlyExitError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExitDecision(StrEnum):
    CONTINUE = "continue"
    EXIT = "exit"


class ExitPosteriorResult(StrEnum):
    PENDING = "pending"
    TRUE_EXIT = "true_exit"
    FALSE_EXIT = "false_exit"
    NOT_EXITED = "not_exited"


def _parse_time(value: str, label: str) -> datetime:
    rendered = str(value or "").strip()
    if not rendered:
        raise EarlyExitError("early_exit_timestamp_missing", f"{label} is required")
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise EarlyExitError(
            "early_exit_timestamp_invalid",
            f"{label} is not an ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def _operator_ref(operator_id: str, version: str) -> str:
    return f"{str(operator_id).strip()}@{str(version).strip()}"


def _candidate_ref(candidate: OperatorCandidate) -> str:
    return _operator_ref(candidate.operator_id, candidate.version)


def _safe_owner_ref(value: Any) -> str:
    return str(value or "").strip()


def _valid_sha256(value: Any) -> bool:
    rendered = str(value or "").strip().lower()
    return (
        len(rendered) == 64
        and all(character in "0123456789abcdef" for character in rendered)
    )


@dataclass(frozen=True, slots=True)
class EarlyExitGateConfig:
    mechanism_id: str
    mechanism_version: str
    snapshot_schema_version: str
    decision_schema_version: str
    freshness_ttl_seconds: int
    final_verifier_ttl_seconds: int
    minimum_confidence: float
    minimum_avoided_operator_count: int
    minimum_avoided_tokens: int
    minimum_avoided_cost_usd: float
    require_artifact: bool
    fallback_profile: str
    no_policy_training: FrozenDict
    digest: str
    path: str = ""

    @classmethod
    def load(cls, path: Path) -> "EarlyExitGateConfig":
        selected = path.resolve()
        try:
            value = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EarlyExitError(
                "early_exit_config_invalid",
                f"early-exit configuration is missing or corrupt: {selected}",
            ) from exc
        if not isinstance(value, Mapping) or value.get("schema") != EARLY_EXIT_CONFIG_SCHEMA:
            raise EarlyExitError(
                "early_exit_config_schema_invalid",
                "unsupported early-exit configuration",
            )
        no_training = value.get("no_policy_training")
        if (
            not isinstance(no_training, Mapping)
            or no_training.get("training_allowed") is not False
            or no_training.get("sampling_allowed") is not False
            or no_training.get("pretrained_model_used") is not False
            or no_training.get("datasets") not in ([], ())
            or no_training.get("checkpoints") not in ([], ())
            or no_training.get("mutable_learned_parameters") not in ([], ())
        ):
            raise EarlyExitError(
                "early_exit_policy_training_forbidden",
                "early exit cannot enable training, sampling, datasets, checkpoints, or learned parameters",
            )
        integer_fields = {
            "freshness_ttl_seconds": int(value.get("freshness_ttl_seconds") or 0),
            "final_verifier_ttl_seconds": int(
                value.get("final_verifier_ttl_seconds") or 0
            ),
            "minimum_avoided_operator_count": int(
                value.get("minimum_avoided_operator_count") or 0
            ),
            "minimum_avoided_tokens": int(
                value.get("minimum_avoided_tokens") or 0
            ),
        }
        if (
            integer_fields["freshness_ttl_seconds"] < 1
            or integer_fields["final_verifier_ttl_seconds"] < 1
            or integer_fields["minimum_avoided_operator_count"] < 1
            or integer_fields["minimum_avoided_tokens"] < 0
        ):
            raise EarlyExitError(
                "early_exit_config_limits_invalid",
                "early-exit TTL and avoided-work thresholds are invalid",
            )
        confidence = float(value.get("minimum_confidence") or 0)
        if confidence <= 0 or confidence > 1:
            raise EarlyExitError(
                "early_exit_confidence_invalid",
                "minimum confidence must be in (0, 1]",
            )
        mechanism_id = str(value.get("mechanism_id") or "")
        mechanism_version = str(value.get("mechanism_version") or "")
        snapshot_schema_version = str(
            value.get("snapshot_schema_version") or ""
        )
        decision_schema_version = str(
            value.get("decision_schema_version") or ""
        )
        if not mechanism_id or not mechanism_version:
            raise EarlyExitError(
                "early_exit_mechanism_identity_missing",
                "early-exit mechanism identity is required",
            )
        if (
            snapshot_schema_version != EXIT_ELIGIBILITY_SCHEMA
            or decision_schema_version != EXIT_DECISION_SCHEMA
        ):
            raise EarlyExitError(
                "early_exit_contract_version_invalid",
                "early-exit config must bind the supported snapshot and decision schemas",
            )
        return cls(
            mechanism_id=mechanism_id,
            mechanism_version=mechanism_version,
            snapshot_schema_version=snapshot_schema_version,
            decision_schema_version=decision_schema_version,
            freshness_ttl_seconds=integer_fields["freshness_ttl_seconds"],
            final_verifier_ttl_seconds=integer_fields[
                "final_verifier_ttl_seconds"
            ],
            minimum_confidence=confidence,
            minimum_avoided_operator_count=integer_fields[
                "minimum_avoided_operator_count"
            ],
            minimum_avoided_tokens=integer_fields["minimum_avoided_tokens"],
            minimum_avoided_cost_usd=max(
                0.0, float(value.get("minimum_avoided_cost_usd") or 0)
            ),
            require_artifact=bool(value.get("require_artifact", True)),
            fallback_profile=str(
                value.get("fallback_profile") or "continue_operator_execution"
            ),
            no_policy_training=FrozenDict(no_training),
            digest=canonical_digest(value),
            path=selected.as_posix(),
        )


@dataclass(frozen=True, slots=True)
class ExitConditionResult:
    condition_id: str
    passed: bool
    reason: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.condition_id).strip() or not str(self.reason).strip():
            raise EarlyExitError(
                "early_exit_condition_invalid",
                "condition id and reason are required",
            )
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "passed": self.passed,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class ExitEligibilitySnapshot:
    header: ContractHeader
    snapshot_id: str
    run_id: str
    task_id: str
    policy_input_digest: str
    proposal_id: str
    proposal_digest: str
    requirement_revision: str
    observed_at: str
    expires_at: str
    expected_obligation_ids: tuple[str, ...]
    observed_obligation_ids: tuple[str, ...]
    unresolved_critical_obligation_ids: tuple[str, ...]
    obligation_owner_ref: str
    obligation_owner_digest: str
    required_artifact_ids: tuple[str, ...]
    verified_artifact_refs: tuple[StableArtifactRef, ...]
    invalid_artifact_ids: tuple[str, ...]
    artifact_owner_ref: str
    artifact_owner_digest: str
    permission_pending_ids: tuple[str, ...]
    permission_owner_ref: str
    permission_owner_digest: str
    side_effect_required_ids: tuple[str, ...]
    side_effect_pending_ids: tuple[str, ...]
    side_effect_unknown_ids: tuple[str, ...]
    side_effect_owner_ref: str
    side_effect_owner_digest: str
    minimum_operator_refs: tuple[str, ...]
    executed_operator_refs: tuple[str, ...]
    minimum_verification_refs: tuple[str, ...]
    executed_verification_refs: tuple[str, ...]
    execution_owner_ref: str
    execution_owner_digest: str
    final_verifier_ref: str
    final_verifier_digest: str
    final_verifier_passed: bool
    final_verifier_fresh_until: str
    checkpoint_ref: str
    checkpoint_digest: str
    checkpoint_requirement_revision: str
    continuity_ref: str
    continuity_digest: str
    continuity_passed: bool
    candidate_confidence: float
    estimated_avoided_operator_count: int
    estimated_avoided_tokens: int
    estimated_avoided_cost_usd: float
    owner_refs: FrozenDict = field(default_factory=FrozenDict)
    schema_version: str = EXIT_ELIGIBILITY_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id",
            "run_id",
            "task_id",
            "policy_input_digest",
            "proposal_id",
            "proposal_digest",
            "requirement_revision",
            "observed_at",
            "expires_at",
        ):
            if not str(getattr(self, name) or "").strip():
                raise EarlyExitError(
                    "early_exit_snapshot_field_missing",
                    f"{name} is required",
                )
        if self.schema_version != EXIT_ELIGIBILITY_SCHEMA:
            raise EarlyExitError(
                "early_exit_snapshot_schema_invalid",
                "unsupported exit eligibility snapshot schema",
            )
        for name in ("policy_input_digest", "proposal_digest"):
            if not _valid_sha256(getattr(self, name)):
                raise EarlyExitError(
                    "early_exit_snapshot_digest_invalid",
                    f"{name} must be SHA-256",
                )
        for name in (
            "obligation_owner_digest",
            "artifact_owner_digest",
            "permission_owner_digest",
            "side_effect_owner_digest",
            "execution_owner_digest",
            "final_verifier_digest",
            "checkpoint_digest",
            "continuity_digest",
        ):
            value = getattr(self, name)
            if value and not _valid_sha256(value):
                raise EarlyExitError(
                    "early_exit_snapshot_digest_invalid",
                    f"{name} must be empty or SHA-256",
                )
        _parse_time(self.observed_at, "observed_at")
        _parse_time(self.expires_at, "expires_at")
        if _parse_time(self.expires_at, "expires_at") < _parse_time(
            self.observed_at, "observed_at"
        ):
            raise EarlyExitError(
                "early_exit_snapshot_expiry_invalid",
                "eligibility snapshot expires before observation",
            )
        if self.final_verifier_fresh_until:
            _parse_time(
                self.final_verifier_fresh_until,
                "final_verifier_fresh_until",
            )
        for name in (
            "expected_obligation_ids",
            "observed_obligation_ids",
            "unresolved_critical_obligation_ids",
            "required_artifact_ids",
            "invalid_artifact_ids",
            "permission_pending_ids",
            "side_effect_required_ids",
            "side_effect_pending_ids",
            "side_effect_unknown_ids",
            "minimum_operator_refs",
            "executed_operator_refs",
            "minimum_verification_refs",
            "executed_verification_refs",
        ):
            object.__setattr__(self, name, _strings(getattr(self, name)))
        object.__setattr__(
            self,
            "verified_artifact_refs",
            tuple(sorted(self.verified_artifact_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(self, "owner_refs", FrozenDict(self.owner_refs))
        object.__setattr__(
            self,
            "candidate_confidence",
            min(1.0, max(0.0, float(self.candidate_confidence))),
        )
        object.__setattr__(
            self,
            "estimated_avoided_operator_count",
            max(0, int(self.estimated_avoided_operator_count)),
        )
        object.__setattr__(
            self,
            "estimated_avoided_tokens",
            max(0, int(self.estimated_avoided_tokens)),
        )
        object.__setattr__(
            self,
            "estimated_avoided_cost_usd",
            max(0.0, float(self.estimated_avoided_cost_usd)),
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.canonical_data())

    @property
    def artifact_refs(self) -> tuple[str, ...]:
        return tuple(item.ref_id for item in self.verified_artifact_refs)

    def canonical_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_kind": "exit_eligibility_snapshot",
            **self.header.to_dict(),
            "payload": {
                "snapshot_id": self.snapshot_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "policy_input_digest": self.policy_input_digest,
                "proposal_id": self.proposal_id,
                "proposal_digest": self.proposal_digest,
                "requirement_revision": self.requirement_revision,
                "observed_at": self.observed_at,
                "expires_at": self.expires_at,
                "expected_obligation_ids": list(self.expected_obligation_ids),
                "observed_obligation_ids": list(self.observed_obligation_ids),
                "unresolved_critical_obligation_ids": list(
                    self.unresolved_critical_obligation_ids
                ),
                "obligation_owner_ref": self.obligation_owner_ref,
                "obligation_owner_digest": self.obligation_owner_digest,
                "required_artifact_ids": list(self.required_artifact_ids),
                "verified_artifact_refs": [
                    item.to_dict() for item in self.verified_artifact_refs
                ],
                "invalid_artifact_ids": list(self.invalid_artifact_ids),
                "artifact_owner_ref": self.artifact_owner_ref,
                "artifact_owner_digest": self.artifact_owner_digest,
                "permission_pending_ids": list(self.permission_pending_ids),
                "permission_owner_ref": self.permission_owner_ref,
                "permission_owner_digest": self.permission_owner_digest,
                "side_effect_required_ids": list(self.side_effect_required_ids),
                "side_effect_pending_ids": list(self.side_effect_pending_ids),
                "side_effect_unknown_ids": list(self.side_effect_unknown_ids),
                "side_effect_owner_ref": self.side_effect_owner_ref,
                "side_effect_owner_digest": self.side_effect_owner_digest,
                "minimum_operator_refs": list(self.minimum_operator_refs),
                "executed_operator_refs": list(self.executed_operator_refs),
                "minimum_verification_refs": list(
                    self.minimum_verification_refs
                ),
                "executed_verification_refs": list(
                    self.executed_verification_refs
                ),
                "execution_owner_ref": self.execution_owner_ref,
                "execution_owner_digest": self.execution_owner_digest,
                "final_verifier_ref": self.final_verifier_ref,
                "final_verifier_digest": self.final_verifier_digest,
                "final_verifier_passed": self.final_verifier_passed,
                "final_verifier_fresh_until": self.final_verifier_fresh_until,
                "checkpoint_ref": self.checkpoint_ref,
                "checkpoint_digest": self.checkpoint_digest,
                "checkpoint_requirement_revision": (
                    self.checkpoint_requirement_revision
                ),
                "continuity_ref": self.continuity_ref,
                "continuity_digest": self.continuity_digest,
                "continuity_passed": self.continuity_passed,
                "candidate_confidence": self.candidate_confidence,
                "estimated_avoided_operator_count": (
                    self.estimated_avoided_operator_count
                ),
                "estimated_avoided_tokens": self.estimated_avoided_tokens,
                "estimated_avoided_cost_usd": self.estimated_avoided_cost_usd,
                "owner_refs": thaw_json(self.owner_refs),
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.canonical_data(), "digest": self.digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExitEligibilitySnapshot":
        required_top = {
            "schema_version",
            "contract_kind",
            "contract_id",
            "created_at",
            "source_event_id",
            "correlation_id",
            "causation_id",
            "mechanism_id",
            "mechanism_version",
            "input_version",
            "idempotency_key",
            "configuration_digest",
            "payload",
            "digest",
        }
        if set(value) != required_top:
            raise EarlyExitError(
                "early_exit_snapshot_fields_invalid",
                "eligibility snapshot contains missing or unknown top-level fields",
            )
        if (
            value.get("schema_version") != EXIT_ELIGIBILITY_SCHEMA
            or value.get("contract_kind") != "exit_eligibility_snapshot"
        ):
            raise EarlyExitError(
                "early_exit_snapshot_schema_invalid",
                "unsupported eligibility snapshot",
            )
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise EarlyExitError(
                "early_exit_snapshot_payload_invalid",
                "eligibility snapshot payload is required",
            )
        sequence_fields = {
            "expected_obligation_ids",
            "observed_obligation_ids",
            "unresolved_critical_obligation_ids",
            "required_artifact_ids",
            "verified_artifact_refs",
            "invalid_artifact_ids",
            "permission_pending_ids",
            "side_effect_required_ids",
            "side_effect_pending_ids",
            "side_effect_unknown_ids",
            "minimum_operator_refs",
            "executed_operator_refs",
            "minimum_verification_refs",
            "executed_verification_refs",
        }
        if any(
            not isinstance(payload.get(name), (list, tuple))
            for name in sequence_fields
        ):
            raise EarlyExitError(
                "early_exit_snapshot_payload_type_invalid",
                "eligibility snapshot sequence fields must be arrays",
            )
        if any(
            type(payload.get(name)) is not bool
            for name in ("final_verifier_passed", "continuity_passed")
        ):
            raise EarlyExitError(
                "early_exit_snapshot_payload_type_invalid",
                "eligibility snapshot boolean fields must be booleans",
            )
        if not isinstance(payload.get("owner_refs"), Mapping):
            raise EarlyExitError(
                "early_exit_snapshot_payload_type_invalid",
                "eligibility snapshot owner_refs must be an object",
            )
        numeric_fields = (
            "candidate_confidence",
            "estimated_avoided_operator_count",
            "estimated_avoided_tokens",
            "estimated_avoided_cost_usd",
        )
        if any(
            isinstance(payload.get(name), bool)
            or not isinstance(payload.get(name), (int, float))
            for name in numeric_fields
        ):
            raise EarlyExitError(
                "early_exit_snapshot_payload_type_invalid",
                "eligibility snapshot numeric fields must be numbers",
            )
        required_payload = {
            field.name
            for field in cls.__dataclass_fields__.values()
            if field.name not in {"header", "schema_version"}
        }
        if set(payload) != required_payload:
            raise EarlyExitError(
                "early_exit_snapshot_payload_fields_invalid",
                "eligibility snapshot payload contains missing or unknown fields",
            )
        header = ContractHeader.from_mapping(value)
        result = cls(
            header=header,
            snapshot_id=str(payload["snapshot_id"]),
            run_id=str(payload["run_id"]),
            task_id=str(payload["task_id"]),
            policy_input_digest=str(payload["policy_input_digest"]),
            proposal_id=str(payload["proposal_id"]),
            proposal_digest=str(payload["proposal_digest"]),
            requirement_revision=str(payload["requirement_revision"]),
            observed_at=str(payload["observed_at"]),
            expires_at=str(payload["expires_at"]),
            expected_obligation_ids=tuple(payload["expected_obligation_ids"]),
            observed_obligation_ids=tuple(payload["observed_obligation_ids"]),
            unresolved_critical_obligation_ids=tuple(
                payload["unresolved_critical_obligation_ids"]
            ),
            obligation_owner_ref=str(payload["obligation_owner_ref"]),
            obligation_owner_digest=str(payload["obligation_owner_digest"]),
            required_artifact_ids=tuple(payload["required_artifact_ids"]),
            verified_artifact_refs=tuple(
                StableArtifactRef.from_mapping(item)
                for item in payload["verified_artifact_refs"]
            ),
            invalid_artifact_ids=tuple(payload["invalid_artifact_ids"]),
            artifact_owner_ref=str(payload["artifact_owner_ref"]),
            artifact_owner_digest=str(payload["artifact_owner_digest"]),
            permission_pending_ids=tuple(payload["permission_pending_ids"]),
            permission_owner_ref=str(payload["permission_owner_ref"]),
            permission_owner_digest=str(payload["permission_owner_digest"]),
            side_effect_required_ids=tuple(payload["side_effect_required_ids"]),
            side_effect_pending_ids=tuple(payload["side_effect_pending_ids"]),
            side_effect_unknown_ids=tuple(payload["side_effect_unknown_ids"]),
            side_effect_owner_ref=str(payload["side_effect_owner_ref"]),
            side_effect_owner_digest=str(payload["side_effect_owner_digest"]),
            minimum_operator_refs=tuple(payload["minimum_operator_refs"]),
            executed_operator_refs=tuple(payload["executed_operator_refs"]),
            minimum_verification_refs=tuple(
                payload["minimum_verification_refs"]
            ),
            executed_verification_refs=tuple(
                payload["executed_verification_refs"]
            ),
            execution_owner_ref=str(payload["execution_owner_ref"]),
            execution_owner_digest=str(payload["execution_owner_digest"]),
            final_verifier_ref=str(payload["final_verifier_ref"]),
            final_verifier_digest=str(payload["final_verifier_digest"]),
            final_verifier_passed=bool(payload["final_verifier_passed"]),
            final_verifier_fresh_until=str(payload["final_verifier_fresh_until"]),
            checkpoint_ref=str(payload["checkpoint_ref"]),
            checkpoint_digest=str(payload["checkpoint_digest"]),
            checkpoint_requirement_revision=str(
                payload["checkpoint_requirement_revision"]
            ),
            continuity_ref=str(payload["continuity_ref"]),
            continuity_digest=str(payload["continuity_digest"]),
            continuity_passed=bool(payload["continuity_passed"]),
            candidate_confidence=float(payload["candidate_confidence"]),
            estimated_avoided_operator_count=int(
                payload["estimated_avoided_operator_count"]
            ),
            estimated_avoided_tokens=int(payload["estimated_avoided_tokens"]),
            estimated_avoided_cost_usd=float(
                payload["estimated_avoided_cost_usd"]
            ),
            owner_refs=FrozenDict(payload["owner_refs"]),
        )
        if str(value.get("digest") or "") != result.digest:
            raise EarlyExitError(
                "early_exit_snapshot_digest_mismatch",
                "eligibility snapshot digest does not match its content",
            )
        return result


@dataclass(frozen=True, slots=True)
class ExitDecisionReceipt:
    header: ContractHeader
    decision_id: str
    snapshot_id: str
    snapshot_digest: str
    decision: ExitDecision
    conditions: tuple[ExitConditionResult, ...]
    confidence: float
    avoided_operator_count: int
    avoided_tokens: int
    avoided_cost_usd: float
    verifier_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    posterior_result: ExitPosteriorResult = ExitPosteriorResult.PENDING
    posterior_outcome_ref: str = ""
    invalidated_by: tuple[str, ...] = ()
    schema_version: str = EXIT_DECISION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", ExitDecision(self.decision))
        object.__setattr__(
            self, "posterior_result", ExitPosteriorResult(self.posterior_result)
        )
        object.__setattr__(
            self,
            "conditions",
            tuple(sorted(self.conditions, key=lambda item: item.condition_id)),
        )
        object.__setattr__(self, "verifier_refs", _strings(self.verifier_refs))
        object.__setattr__(self, "artifact_refs", _strings(self.artifact_refs))
        object.__setattr__(self, "invalidated_by", _strings(self.invalidated_by))
        if self.decision is ExitDecision.EXIT and any(
            not item.passed for item in self.conditions
        ):
            raise EarlyExitError(
                "early_exit_unsafe_decision",
                "exit decision cannot contain a failed condition",
            )

    @property
    def digest(self) -> str:
        return canonical_digest(self.canonical_data())

    @property
    def failed_conditions(self) -> tuple[str, ...]:
        return tuple(item.condition_id for item in self.conditions if not item.passed)

    def canonical_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_kind": "early_exit_decision_receipt",
            **self.header.to_dict(),
            "payload": {
                "decision_id": self.decision_id,
                "snapshot_id": self.snapshot_id,
                "snapshot_digest": self.snapshot_digest,
                "decision": self.decision.value,
                "conditions": [item.to_dict() for item in self.conditions],
                "confidence": self.confidence,
                "avoided_operator_count": self.avoided_operator_count,
                "avoided_tokens": self.avoided_tokens,
                "avoided_cost_usd": self.avoided_cost_usd,
                "verifier_refs": list(self.verifier_refs),
                "artifact_refs": list(self.artifact_refs),
                "posterior_result": self.posterior_result.value,
                "posterior_outcome_ref": self.posterior_outcome_ref,
                "invalidated_by": list(self.invalidated_by),
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.canonical_data(), "digest": self.digest}


@dataclass(frozen=True, slots=True)
class EarlyExitCheckpointBinding:
    snapshot_id: str
    snapshot_digest: str
    decision_id: str
    decision_digest: str
    decision: str
    run_id: str
    task_id: str
    requirement_revision: str
    policy_input_digest: str
    proposal_digest: str
    gate_configuration_digest: str
    owner_refs: FrozenDict
    schema_version: str = EXIT_CHECKPOINT_BINDING_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_refs", FrozenDict(self.owner_refs))
        if self.schema_version != EXIT_CHECKPOINT_BINDING_SCHEMA:
            raise EarlyExitError(
                "early_exit_checkpoint_binding_schema_invalid",
                "unsupported early-exit checkpoint binding",
            )
        for name in (
            "snapshot_id",
            "decision_id",
            "run_id",
            "task_id",
            "requirement_revision",
        ):
            if not str(getattr(self, name) or "").strip():
                raise EarlyExitError(
                    "early_exit_checkpoint_binding_field_missing",
                    f"{name} is required",
                )
        for name in (
            "snapshot_digest",
            "decision_digest",
            "policy_input_digest",
            "proposal_digest",
            "gate_configuration_digest",
        ):
            if not _valid_sha256(getattr(self, name)):
                raise EarlyExitError(
                    "early_exit_checkpoint_binding_digest_invalid",
                    f"{name} must be SHA-256",
                )
        try:
            ExitDecision(self.decision)
        except ValueError as exc:
            raise EarlyExitError(
                "early_exit_checkpoint_binding_decision_invalid",
                "checkpoint binding has an invalid decision",
            ) from exc

    @property
    def digest(self) -> str:
        return canonical_digest(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_digest": self.snapshot_digest,
            "decision_id": self.decision_id,
            "decision_digest": self.decision_digest,
            "decision": self.decision,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "requirement_revision": self.requirement_revision,
            "policy_input_digest": self.policy_input_digest,
            "proposal_digest": self.proposal_digest,
            "gate_configuration_digest": self.gate_configuration_digest,
            "owner_refs": thaw_json(self.owner_refs),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "digest": self.digest}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EarlyExitCheckpointBinding":
        supplied = str(value.get("digest") or "")
        result = cls(
            snapshot_id=str(value.get("snapshot_id") or ""),
            snapshot_digest=str(value.get("snapshot_digest") or ""),
            decision_id=str(value.get("decision_id") or ""),
            decision_digest=str(value.get("decision_digest") or ""),
            decision=str(value.get("decision") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            requirement_revision=str(value.get("requirement_revision") or ""),
            policy_input_digest=str(value.get("policy_input_digest") or ""),
            proposal_digest=str(value.get("proposal_digest") or ""),
            gate_configuration_digest=str(
                value.get("gate_configuration_digest") or ""
            ),
            owner_refs=FrozenDict(value.get("owner_refs") or {}),
            schema_version=str(value.get("schema_version") or ""),
        )
        if supplied != result.digest:
            raise EarlyExitError(
                "early_exit_checkpoint_binding_digest_mismatch",
                "checkpoint early-exit binding is corrupt",
            )
        return result


@dataclass(frozen=True, slots=True)
class ExitRestoreValidation:
    checkpoint_ref: str
    prior_decision_ref: str
    prior_verdict_reused: bool
    invalidation_reasons: tuple[str, ...]
    revalidated_snapshot_digest: str
    decision: ExitDecisionReceipt
    schema_version: str = EXIT_RESTORE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "checkpoint_ref": self.checkpoint_ref,
            "prior_decision_ref": self.prior_decision_ref,
            "prior_verdict_reused": self.prior_verdict_reused,
            "invalidation_reasons": list(self.invalidation_reasons),
            "revalidated_snapshot_digest": self.revalidated_snapshot_digest,
            "decision": self.decision.to_dict(),
        }


class DeterministicEarlyExitGate:
    """Hard-condition early exit; uncertainty and missing evidence mean continue."""

    def __init__(self, config: EarlyExitGateConfig) -> None:
        self.config = config

    def evaluate(
        self,
        snapshot: ExitEligibilitySnapshot | Mapping[str, Any],
        *,
        enabled: bool = True,
        evaluated_at: str | None = None,
    ) -> ExitDecisionReceipt:
        if isinstance(snapshot, Mapping):
            raw = dict(snapshot)
            try:
                selected = ExitEligibilitySnapshot.from_dict(raw)
            except Exception as exc:  # noqa: BLE001 - untrusted evidence is fail-closed.
                return self._malformed_receipt(
                    raw,
                    reason=getattr(
                        exc,
                        "code",
                        f"early_exit_snapshot:{type(exc).__name__}",
                    ),
                    evaluated_at=evaluated_at,
                )
        else:
            selected = snapshot
        checked_at = evaluated_at or now_iso()
        checked = _parse_time(checked_at, "evaluated_at")
        expected = set(selected.expected_obligation_ids)
        observed = set(selected.observed_obligation_ids)
        minimum_operators = set(selected.minimum_operator_refs)
        executed_operators = set(selected.executed_operator_refs)
        minimum_verification = set(selected.minimum_verification_refs)
        executed_verification = set(selected.executed_verification_refs)
        owner_refs = thaw_json(selected.owner_refs)
        final_fresh_until = (
            _parse_time(
                selected.final_verifier_fresh_until,
                "final_verifier_fresh_until",
            )
            if selected.final_verifier_fresh_until
            else None
        )
        final_verifier_valid = bool(
            selected.final_verifier_passed
            and selected.final_verifier_ref
            and _valid_sha256(selected.final_verifier_digest)
            and final_fresh_until is not None
            and checked <= final_fresh_until
            and final_fresh_until
            <= (
                _parse_time(selected.observed_at, "observed_at")
                + timedelta(seconds=self.config.final_verifier_ttl_seconds)
            )
        )
        required_owner_keys = {
            "task",
            "artifact",
            "permission",
            "side_effect",
            "checkpoint",
            "continuity",
            "verifier",
            "execution",
        }
        conditions = (
            ExitConditionResult(
                "early_exit_enabled",
                enabled,
                "early exit enabled"
                if enabled
                else "early exit disabled; execute proposed depth",
            ),
            ExitConditionResult(
                "snapshot_fresh",
                checked <= _parse_time(selected.expires_at, "expires_at"),
                "eligibility snapshot is fresh"
                if checked <= _parse_time(selected.expires_at, "expires_at")
                else "eligibility snapshot is stale",
                (selected.snapshot_id,),
            ),
            ExitConditionResult(
                "canonical_owner_evidence_complete",
                required_owner_keys.issubset(owner_refs)
                and all(
                    _safe_owner_ref(owner_refs.get(key))
                    for key in required_owner_keys
                ),
                "all canonical owner projections are referenced"
                if required_owner_keys.issubset(owner_refs)
                and all(
                    _safe_owner_ref(owner_refs.get(key))
                    for key in required_owner_keys
                )
                else "one or more canonical owner projections are missing",
                tuple(str(item) for item in owner_refs.values()),
            ),
            ExitConditionResult(
                "final_verifier_passed",
                final_verifier_valid,
                "final verifier passed with a fresh digest-bound receipt"
                if final_verifier_valid
                else "final verifier is missing, failed, forged, or stale",
                (selected.final_verifier_ref,),
            ),
            ExitConditionResult(
                "critical_obligations_resolved",
                bool(
                    expected
                    and expected.issubset(observed)
                    and not selected.unresolved_critical_obligation_ids
                    and selected.obligation_owner_ref
                    and _valid_sha256(selected.obligation_owner_digest)
                ),
                "all expected critical obligations remain in scope and are resolved"
                if expected
                and expected.issubset(observed)
                and not selected.unresolved_critical_obligation_ids
                and selected.obligation_owner_ref
                and _valid_sha256(selected.obligation_owner_digest)
                else "critical obligation scope is incomplete or unresolved",
                (selected.obligation_owner_ref,),
            ),
            ExitConditionResult(
                "required_artifacts_complete",
                bool(
                    (selected.required_artifact_ids or not self.config.require_artifact)
                    and not selected.invalid_artifact_ids
                    and (
                        len(selected.verified_artifact_refs)
                        >= len(selected.required_artifact_ids)
                    )
                    and selected.artifact_owner_ref
                    and _valid_sha256(selected.artifact_owner_digest)
                ),
                "all required artifacts exist and their committed bytes verify"
                if (
                    selected.required_artifact_ids or not self.config.require_artifact
                )
                and not selected.invalid_artifact_ids
                and len(selected.verified_artifact_refs)
                >= len(selected.required_artifact_ids)
                and selected.artifact_owner_ref
                and _valid_sha256(selected.artifact_owner_digest)
                else "required artifact is missing or failed digest verification",
                (
                    selected.artifact_owner_ref,
                    *(item.ref_id for item in selected.verified_artifact_refs),
                ),
            ),
            ExitConditionResult(
                "permission_settled",
                bool(
                    selected.permission_owner_ref
                    and _valid_sha256(selected.permission_owner_digest)
                    and not selected.permission_pending_ids
                ),
                "permission pending count is zero"
                if selected.permission_owner_ref
                and _valid_sha256(selected.permission_owner_digest)
                and not selected.permission_pending_ids
                else "permission state is missing or pending",
                (selected.permission_owner_ref,),
            ),
            ExitConditionResult(
                "side_effects_settled",
                bool(
                    selected.side_effect_owner_ref
                    and _valid_sha256(selected.side_effect_owner_digest)
                    and not selected.side_effect_pending_ids
                    and not selected.side_effect_unknown_ids
                ),
                "side-effect pending and unknown counts are zero"
                if selected.side_effect_owner_ref
                and _valid_sha256(selected.side_effect_owner_digest)
                and not selected.side_effect_pending_ids
                and not selected.side_effect_unknown_ids
                else "side-effect state is missing, pending, or unknown",
                (selected.side_effect_owner_ref,),
            ),
            ExitConditionResult(
                "minimum_operator_path_executed",
                bool(
                    minimum_operators
                    and minimum_operators.issubset(executed_operators)
                    and minimum_verification
                    and minimum_verification.issubset(executed_verification)
                    and selected.execution_owner_ref
                    and _valid_sha256(selected.execution_owner_digest)
                ),
                "minimum operator and verification path executed"
                if minimum_operators
                and minimum_operators.issubset(executed_operators)
                and minimum_verification
                and minimum_verification.issubset(executed_verification)
                and selected.execution_owner_ref
                and _valid_sha256(selected.execution_owner_digest)
                else "minimum operator or verification path is incomplete",
                (selected.execution_owner_ref,),
            ),
            ExitConditionResult(
                "checkpoint_requirement_current",
                bool(
                    selected.checkpoint_ref
                    and _valid_sha256(selected.checkpoint_digest)
                    and selected.checkpoint_requirement_revision
                    == selected.requirement_revision
                ),
                "checkpoint and current requirement revision agree"
                if selected.checkpoint_ref
                and _valid_sha256(selected.checkpoint_digest)
                and selected.checkpoint_requirement_revision
                == selected.requirement_revision
                else "checkpoint is missing or bound to a stale requirement revision",
                (selected.checkpoint_ref,),
            ),
            ExitConditionResult(
                "memory_continuity_passed",
                bool(
                    selected.continuity_passed
                    and selected.continuity_ref
                    and _valid_sha256(selected.continuity_digest)
                ),
                "memory continuity verifier passed"
                if selected.continuity_passed
                and selected.continuity_ref
                and _valid_sha256(selected.continuity_digest)
                else "memory continuity evidence is missing or failed",
                (selected.continuity_ref,),
            ),
            ExitConditionResult(
                "confidence_and_cost_benefit",
                bool(
                    selected.candidate_confidence >= self.config.minimum_confidence
                    and selected.estimated_avoided_operator_count
                    >= self.config.minimum_avoided_operator_count
                    and selected.estimated_avoided_tokens
                    >= self.config.minimum_avoided_tokens
                    and selected.estimated_avoided_cost_usd
                    >= self.config.minimum_avoided_cost_usd
                ),
                "confidence and avoided-work benefit meet the frozen gate"
                if selected.candidate_confidence >= self.config.minimum_confidence
                and selected.estimated_avoided_operator_count
                >= self.config.minimum_avoided_operator_count
                and selected.estimated_avoided_tokens
                >= self.config.minimum_avoided_tokens
                and selected.estimated_avoided_cost_usd
                >= self.config.minimum_avoided_cost_usd
                else "confidence or avoided-work benefit is below the frozen gate",
            ),
        )
        decision = (
            ExitDecision.EXIT
            if all(item.passed for item in conditions)
            else ExitDecision.CONTINUE
        )
        seed = canonical_digest(
            (
                selected.digest,
                self.config.digest,
                checked_at,
                decision.value,
                [item.to_dict() for item in conditions],
            )
        )
        header = ContractHeader(
            contract_id=f"early-exit-decision-{seed[:24]}",
            created_at=checked_at,
            source_event_id=selected.header.source_event_id,
            correlation_id=selected.header.correlation_id,
            causation_id=selected.snapshot_id,
            mechanism_id=self.config.mechanism_id,
            mechanism_version=self.config.mechanism_version,
            input_version=EXIT_ELIGIBILITY_SCHEMA,
            idempotency_key=f"early-exit:{seed}",
            configuration_digest=self.config.digest,
        )
        return ExitDecisionReceipt(
            header=header,
            decision_id=header.contract_id,
            snapshot_id=selected.snapshot_id,
            snapshot_digest=selected.digest,
            decision=decision,
            conditions=conditions,
            confidence=selected.candidate_confidence,
            avoided_operator_count=(
                selected.estimated_avoided_operator_count
                if decision is ExitDecision.EXIT
                else 0
            ),
            avoided_tokens=(
                selected.estimated_avoided_tokens
                if decision is ExitDecision.EXIT
                else 0
            ),
            avoided_cost_usd=(
                selected.estimated_avoided_cost_usd
                if decision is ExitDecision.EXIT
                else 0.0
            ),
            verifier_refs=(selected.final_verifier_ref, selected.continuity_ref),
            artifact_refs=selected.artifact_refs,
        )

    def finalize_posterior(
        self,
        receipt: ExitDecisionReceipt,
        outcome: PolicyOutcome,
    ) -> ExitDecisionReceipt:
        if receipt.decision is not ExitDecision.EXIT:
            posterior = ExitPosteriorResult.NOT_EXITED
        else:
            metrics = thaw_json(outcome.metrics)
            early_exit = metrics.get("early_exit")
            condition_results = (
                early_exit.get("condition_results")
                if isinstance(early_exit, Mapping)
                else None
            )
            complete = (
                outcome.verifier_result == "passed"
                and bool(outcome.artifact_refs)
                and outcome.permission_result == "settled"
                and outcome.recovery_result == "not_required"
                and isinstance(early_exit, Mapping)
                and early_exit.get("artifact_complete") is True
                and early_exit.get("unresolved_critical_obligation_count") == 0
                and early_exit.get("permission_pending_count") == 0
                and early_exit.get("side_effect_pending_or_unknown_count") == 0
                and isinstance(condition_results, Mapping)
                and bool(condition_results)
                and all(value is True for value in condition_results.values())
            )
            posterior = (
                ExitPosteriorResult.TRUE_EXIT
                if complete
                else ExitPosteriorResult.FALSE_EXIT
            )
        return replace(
            receipt,
            posterior_result=posterior,
            posterior_outcome_ref=outcome.header.contract_id,
        )

    def checkpoint_binding(
        self,
        snapshot: ExitEligibilitySnapshot,
        receipt: ExitDecisionReceipt,
    ) -> EarlyExitCheckpointBinding:
        if receipt.snapshot_digest != snapshot.digest:
            raise EarlyExitError(
                "early_exit_checkpoint_snapshot_mismatch",
                "decision receipt does not belong to the supplied eligibility snapshot",
            )
        return EarlyExitCheckpointBinding(
            snapshot_id=snapshot.snapshot_id,
            snapshot_digest=snapshot.digest,
            decision_id=receipt.decision_id,
            decision_digest=receipt.digest,
            decision=receipt.decision.value,
            run_id=snapshot.run_id,
            task_id=snapshot.task_id,
            requirement_revision=snapshot.requirement_revision,
            policy_input_digest=snapshot.policy_input_digest,
            proposal_digest=snapshot.proposal_digest,
            gate_configuration_digest=self.config.digest,
            owner_refs=snapshot.owner_refs,
        )

    def bind_checkpoint_request(
        self,
        request: CheckpointCommitRequest,
        binding: EarlyExitCheckpointBinding,
        receipt: ExitDecisionReceipt,
    ) -> CheckpointCommitRequest:
        if (
            request.refs.run_id != binding.run_id
            or request.refs.task_id != binding.task_id
        ):
            raise EarlyExitError(
                "early_exit_checkpoint_scope_mismatch",
                "checkpoint request and early-exit binding have different scope",
            )
        if receipt.decision_id != binding.decision_id or receipt.digest != binding.decision_digest:
            raise EarlyExitError(
                "early_exit_checkpoint_receipt_mismatch",
                "checkpoint binding does not reference the supplied decision receipt",
            )
        metadata = {
            **dict(request.metadata),
            "early_exit": {
                "binding": binding.to_dict(),
                "decision_receipt": receipt.to_dict(),
                "restore_policy": "revalidate_fresh_owner_state_never_reuse_verdict",
            },
        }
        committed_refs = (
            *request.committed_refs,
            {
                "kind": "early_exit_decision",
                "ref": binding.decision_id,
                "digest": binding.decision_digest,
                "snapshot_ref": binding.snapshot_id,
                "snapshot_digest": binding.snapshot_digest,
            },
        )
        version_refs = {
            **dict(request.version_refs),
            "requirement_revision": binding.requirement_revision,
            "early_exit_gate_configuration": binding.gate_configuration_digest,
        }
        return replace(
            request,
            metadata=metadata,
            committed_refs=committed_refs,
            version_refs=version_refs,
        )

    def revalidate_restored(
        self,
        checkpoint: RecoveryCheckpoint,
        current_snapshot: ExitEligibilitySnapshot,
        *,
        evaluated_at: str | None = None,
        enabled: bool = True,
    ) -> ExitRestoreValidation:
        reasons: list[str] = []
        prior_ref = ""
        binding: EarlyExitCheckpointBinding | None = None
        raw_early_exit = checkpoint.metadata.get("early_exit")
        if not isinstance(raw_early_exit, Mapping):
            reasons.append("checkpoint_early_exit_binding_missing")
        else:
            raw_binding = raw_early_exit.get("binding")
            if not isinstance(raw_binding, Mapping):
                reasons.append("checkpoint_early_exit_binding_missing")
            else:
                try:
                    binding = EarlyExitCheckpointBinding.from_mapping(raw_binding)
                    prior_ref = binding.decision_id
                except Exception:  # noqa: BLE001 - restore evidence is untrusted.
                    reasons.append("checkpoint_early_exit_binding_corrupt")
        if checkpoint.phase is not CheckpointPhase.COMMITTED:
            reasons.append("checkpoint_not_committed")
        if checkpoint.content_digest != current_snapshot.checkpoint_digest:
            reasons.append("checkpoint_content_digest_changed")
        if (
            checkpoint.run_id != current_snapshot.run_id
            or checkpoint.task_id != current_snapshot.task_id
        ):
            reasons.append("checkpoint_scope_changed")
        if binding is not None:
            if binding.gate_configuration_digest != self.config.digest:
                reasons.append("gate_configuration_changed")
            if binding.requirement_revision != current_snapshot.requirement_revision:
                reasons.append("requirement_revision_changed")
            if binding.policy_input_digest != current_snapshot.policy_input_digest:
                reasons.append("policy_input_changed")
            if binding.proposal_digest != current_snapshot.proposal_digest:
                reasons.append("operator_proposal_changed")
            if binding.snapshot_digest != current_snapshot.digest:
                reasons.append("eligibility_inputs_changed")
        decision = self.evaluate(
            current_snapshot,
            enabled=enabled,
            evaluated_at=evaluated_at,
        )
        return ExitRestoreValidation(
            checkpoint_ref=checkpoint.checkpoint_id,
            prior_decision_ref=prior_ref,
            prior_verdict_reused=False,
            invalidation_reasons=_strings(reasons),
            revalidated_snapshot_digest=current_snapshot.digest,
            decision=decision,
        )

    def _malformed_receipt(
        self,
        raw: Mapping[str, Any],
        *,
        reason: str,
        evaluated_at: str | None,
    ) -> ExitDecisionReceipt:
        checked_at = evaluated_at or now_iso()
        snapshot_digest = canonical_digest(raw)
        seed = canonical_digest((snapshot_digest, self.config.digest, reason, checked_at))
        header = ContractHeader(
            contract_id=f"early-exit-decision-{seed[:24]}",
            created_at=checked_at,
            source_event_id=f"early-exit-invalid-{seed[:16]}",
            correlation_id=f"early-exit-invalid-{seed[:16]}",
            causation_id=f"early-exit-invalid-{snapshot_digest[:16]}",
            mechanism_id=self.config.mechanism_id,
            mechanism_version=self.config.mechanism_version,
            input_version=EXIT_ELIGIBILITY_SCHEMA,
            idempotency_key=f"early-exit-invalid:{seed}",
            configuration_digest=self.config.digest,
        )
        return ExitDecisionReceipt(
            header=header,
            decision_id=header.contract_id,
            snapshot_id=f"invalid-snapshot-{snapshot_digest[:20]}",
            snapshot_digest=snapshot_digest,
            decision=ExitDecision.CONTINUE,
            conditions=(
                ExitConditionResult(
                    condition_id="eligibility_snapshot_valid",
                    passed=False,
                    reason=reason,
                ),
            ),
            confidence=0,
            avoided_operator_count=0,
            avoided_tokens=0,
            avoided_cost_usd=0,
            verifier_refs=(),
            artifact_refs=(),
        )


def build_operator_execution_decision(
    *,
    task: TaskState,
    layer_index: int,
    candidates: Sequence[OperatorCandidate],
    owner_receipt_ref: str,
    actual_tokens: int,
    actual_cost_usd: float,
    actual_latency_ms: int,
    verification_refs: Iterable[str] = (),
    created_at: str | None = None,
) -> DecisionRecord:
    operator_refs = tuple(_candidate_ref(item) for item in candidates)
    payload = {
        "schema": OPERATOR_EXECUTION_SCHEMA,
        "layer_index": int(layer_index),
        "operator_refs": list(operator_refs),
        "owner_receipt_ref": str(owner_receipt_ref),
        "actual_tokens": max(0, int(actual_tokens)),
        "actual_cost_usd": max(0.0, float(actual_cost_usd)),
        "actual_latency_ms": max(0, int(actual_latency_ms)),
        "verification_refs": list(_strings(verification_refs)),
        "status": "completed",
    }
    payload["receipt_digest"] = canonical_digest(payload)
    return DecisionRecord(
        run_id=task.run_id,
        task_id=task.task_id,
        decision_type="operator_execution",
        selected="completed",
        summary=f"Executed adaptive operator layer {layer_index}.",
        rationale="Execution owner returned a completed receipt.",
        alternatives=[],
        checks=[{"operator_ref": item, "completed": True} for item in operator_refs],
        affected_node_ids=[task.root_node_id],
        created_at=created_at or now_iso(),
        metadata=payload,
    )


def build_final_verifier_decision(
    *,
    task: TaskState,
    requirement_revision: str,
    expected_obligation_ids: Iterable[str],
    verified_artifact_refs: Sequence[StableArtifactRef],
    passed: bool,
    verifier_version: str,
    verifier_receipt_ref: str,
    verified_at: str | None = None,
    fresh_until: str,
    verification_refs: Iterable[str] = ("final_verifier",),
) -> DecisionRecord:
    observed_at = verified_at or now_iso()
    input_digest = _final_verifier_input_digest(
        task=task,
        requirement_revision=requirement_revision,
        expected_obligation_ids=expected_obligation_ids,
        verified_artifact_refs=verified_artifact_refs,
    )
    payload = {
        "schema": FINAL_VERIFIER_SCHEMA,
        "run_id": task.run_id,
        "task_id": task.task_id,
        "requirement_revision": requirement_revision,
        "expected_obligation_ids": list(_strings(expected_obligation_ids)),
        "artifact_refs": [item.to_dict() for item in verified_artifact_refs],
        "passed": bool(passed),
        "verifier_version": str(verifier_version),
        "verifier_receipt_ref": str(verifier_receipt_ref),
        "verified_at": observed_at,
        "fresh_until": fresh_until,
        "input_digest": input_digest,
        "verification_refs": list(_strings(verification_refs)),
    }
    payload["receipt_digest"] = canonical_digest(payload)
    return DecisionRecord(
        run_id=task.run_id,
        task_id=task.task_id,
        decision_type="final_verifier",
        selected="passed" if passed else "failed",
        summary="Final verifier evaluated canonical task and artifact owner state.",
        rationale="Early exit consumes only this digest-bound final verifier receipt.",
        alternatives=[],
        checks=[
            {"condition": "artifact_digest", "passed": bool(passed)},
            {"condition": "requirement_revision", "passed": bool(passed)},
        ],
        affected_node_ids=[task.root_node_id],
        created_at=observed_at,
        metadata=payload,
    )


def _final_verifier_input_digest(
    *,
    task: TaskState,
    requirement_revision: str,
    expected_obligation_ids: Iterable[str],
    verified_artifact_refs: Sequence[StableArtifactRef],
) -> str:
    return canonical_digest(
        {
            "run_id": task.run_id,
            "task_id": task.task_id,
            "task_status": task.status.value,
            "requirement_revision": requirement_revision,
            "expected_obligation_ids": list(_strings(expected_obligation_ids)),
            "artifacts": [
                item.to_dict()
                for item in sorted(
                    verified_artifact_refs, key=lambda item: item.ref_id
                )
            ],
        }
    )


class CanonicalExitSnapshotBuilder:
    """Projects eligibility from existing owners without persisting gate state."""

    def __init__(
        self,
        *,
        config: EarlyExitGateConfig,
        artifact_store: Any,
        permission_queue: Any,
        side_effect_store: Any,
    ) -> None:
        self.config = config
        self.artifact_store = artifact_store
        self.permission_queue = permission_queue
        self.side_effect_store = side_effect_store

    def capture(
        self,
        *,
        task: TaskState,
        policy_input: PolicyInputSnapshot,
        proposal: OperatorSelectionProposal,
        checkpoint: RecoveryCheckpoint,
        continuity_receipt: MemoryContinuityReceipt,
        required_artifact_ids: Iterable[str],
        minimum_operator_refs: Iterable[str],
        minimum_verification_refs: Iterable[str],
        remaining_candidates: Sequence[OperatorCandidate],
        observed_at: str | None = None,
    ) -> ExitEligibilitySnapshot:
        captured_at = observed_at or now_iso()
        captured_time = _parse_time(captured_at, "observed_at")
        if (
            task.run_id != policy_input.run_id
            or task.task_id != policy_input.task_id
            or proposal.input_snapshot_digest != policy_input.digest
            or proposal.requirement_revision
            != policy_input.requirement_revision
            or proposal.committed_graph_id != policy_input.graph.graph_id
            or proposal.committed_graph_revision
            != policy_input.graph.revision
            or proposal.committed_graph_signature
            != policy_input.graph.signature
            or proposal.committed_graph_commit_id
            != policy_input.graph.commit_id
        ):
            raise EarlyExitError(
                "early_exit_scope_mismatch",
                "task, policy input, and operator proposal scope do not agree",
            )
        expected_obligations = _strings(policy_input.unresolved_obligations)
        observed_obligations = _task_obligation_scope(task)
        task_complete = task.status is PlanNodeStatus.COMPLETED and all(
            item.status
            in {
                PlanNodeStatus.COMPLETED,
                PlanNodeStatus.CANCELLED,
                PlanNodeStatus.SUPERSEDED,
            }
            for item in task.plan_nodes.values()
        )
        unresolved = () if task_complete else expected_obligations
        obligation_projection = {
            "run_id": task.run_id,
            "task_id": task.task_id,
            "task_status": task.status.value,
            "expected": expected_obligations,
            "observed": observed_obligations,
            "unresolved": unresolved,
            "task_updated_at": task.updated_at,
        }
        required_ids = _strings(
            tuple(required_artifact_ids) or tuple(task.constraints.required_artifacts)
        )
        verified_refs, invalid_artifacts = self._verify_artifacts(
            task,
            required_ids,
        )
        artifact_projection = {
            "required_ids": required_ids,
            "verified_refs": [item.to_dict() for item in verified_refs],
            "invalid_ids": invalid_artifacts,
        }
        permission_pending, permission_owner, permission_digest = (
            self._permission_projection()
        )
        (
            side_required,
            side_pending,
            side_unknown,
            side_owner,
            side_digest,
        ) = self._side_effect_projection(checkpoint)
        (
            executed_operators,
            executed_verification,
            execution_owner,
            execution_digest,
        ) = _execution_projection(task)
        (
            final_verifier_ref,
            final_verifier_digest,
            final_verifier_passed,
            final_verifier_fresh_until,
        ) = _final_verifier_projection(
            task=task,
            requirement_revision=policy_input.requirement_revision,
            expected_obligation_ids=expected_obligations,
            verified_artifact_refs=verified_refs,
        )
        continuity_obligations = thaw_json(continuity_receipt.obligation_results)
        consumed_ids = set(continuity_obligations.get("consumed_ids") or ())
        continuity_passed = bool(
            continuity_receipt.continuity_result == "passed"
            and continuity_receipt.requirement_revision
            == policy_input.requirement_revision
            and set(expected_obligations).issubset(consumed_ids)
            and not continuity_obligations.get("missing_consumption")
            and continuity_obligations.get("stale_requirement_execution") is not True
        )
        checkpoint_requirement_revision = str(
            checkpoint.version_refs.get("requirement_revision")
            or checkpoint.metadata.get("requirement_revision")
            or ""
        )
        confidence = min(
            (item.confidence for item in proposal.candidates),
            default=0.0,
        )
        avoided_operator_count = len(remaining_candidates)
        avoided_tokens = sum(
            max(0, int(item.estimated_tokens)) for item in remaining_candidates
        )
        avoided_cost = round(
            sum(
                max(0.0, float(item.estimated_cost_usd))
                for item in remaining_candidates
            ),
            12,
        )
        snapshot_seed = canonical_digest(
            {
                "policy_input": policy_input.digest,
                "proposal": proposal.digest,
                "task_projection": obligation_projection,
                "artifact_projection": artifact_projection,
                "permission": permission_digest,
                "side_effect": side_digest,
                "execution": execution_digest,
                "verifier": final_verifier_digest,
                "checkpoint": checkpoint.content_digest,
                "continuity": continuity_receipt.digest,
                "remaining": [_candidate_ref(item) for item in remaining_candidates],
                "captured_at": captured_at,
            }
        )
        header = ContractHeader(
            contract_id=f"exit-eligibility-{snapshot_seed[:24]}",
            created_at=captured_at,
            source_event_id=proposal.header.source_event_id,
            correlation_id=proposal.header.correlation_id,
            causation_id=proposal.proposal_id,
            mechanism_id=self.config.mechanism_id,
            mechanism_version=self.config.mechanism_version,
            input_version=proposal.schema_version,
            idempotency_key=f"exit-eligibility:{snapshot_seed}",
            configuration_digest=self.config.digest,
        )
        owner_refs = FrozenDict(
            {
                "task": f"task-state://{task.run_id}/{task.task_id}",
                "artifact": f"artifact-store://{task.run_id}/{task.task_id}",
                "permission": permission_owner,
                "side_effect": side_owner,
                "checkpoint": checkpoint.checkpoint_id,
                "continuity": continuity_receipt.header.contract_id,
                "verifier": final_verifier_ref,
                "execution": execution_owner,
            }
        )
        return ExitEligibilitySnapshot(
            header=header,
            snapshot_id=header.contract_id,
            run_id=task.run_id,
            task_id=task.task_id,
            policy_input_digest=policy_input.digest,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest,
            requirement_revision=policy_input.requirement_revision,
            observed_at=captured_at,
            expires_at=_iso(
                captured_time + timedelta(seconds=self.config.freshness_ttl_seconds)
            ),
            expected_obligation_ids=expected_obligations,
            observed_obligation_ids=observed_obligations,
            unresolved_critical_obligation_ids=unresolved,
            obligation_owner_ref=f"task-state://{task.run_id}/{task.task_id}",
            obligation_owner_digest=canonical_digest(obligation_projection),
            required_artifact_ids=required_ids,
            verified_artifact_refs=verified_refs,
            invalid_artifact_ids=invalid_artifacts,
            artifact_owner_ref=f"artifact-store://{task.run_id}/{task.task_id}",
            artifact_owner_digest=canonical_digest(artifact_projection),
            permission_pending_ids=permission_pending,
            permission_owner_ref=permission_owner,
            permission_owner_digest=permission_digest,
            side_effect_required_ids=side_required,
            side_effect_pending_ids=side_pending,
            side_effect_unknown_ids=side_unknown,
            side_effect_owner_ref=side_owner,
            side_effect_owner_digest=side_digest,
            minimum_operator_refs=tuple(minimum_operator_refs),
            executed_operator_refs=executed_operators,
            minimum_verification_refs=tuple(minimum_verification_refs),
            executed_verification_refs=executed_verification,
            execution_owner_ref=execution_owner,
            execution_owner_digest=execution_digest,
            final_verifier_ref=final_verifier_ref,
            final_verifier_digest=final_verifier_digest,
            final_verifier_passed=final_verifier_passed,
            final_verifier_fresh_until=final_verifier_fresh_until,
            checkpoint_ref=checkpoint.checkpoint_id,
            checkpoint_digest=checkpoint.content_digest,
            checkpoint_requirement_revision=checkpoint_requirement_revision,
            continuity_ref=continuity_receipt.header.contract_id,
            continuity_digest=continuity_receipt.digest,
            continuity_passed=continuity_passed,
            candidate_confidence=confidence,
            estimated_avoided_operator_count=avoided_operator_count,
            estimated_avoided_tokens=avoided_tokens,
            estimated_avoided_cost_usd=avoided_cost,
            owner_refs=owner_refs,
        )

    def _verify_artifacts(
        self,
        task: TaskState,
        required_ids: Sequence[str],
    ) -> tuple[tuple[StableArtifactRef, ...], tuple[str, ...]]:
        by_key: dict[str, Any] = {}
        for artifact in task.artifacts:
            by_key[str(artifact.artifact_id)] = artifact
            if str(artifact.title).strip():
                by_key[str(artifact.title).strip()] = artifact
        verified: list[StableArtifactRef] = []
        invalid: list[str] = []
        seen_artifacts: set[str] = set()
        for required in required_ids:
            artifact = by_key.get(required)
            if artifact is None:
                invalid.append(required)
                continue
            try:
                observed = self.artifact_store.verify(artifact)
            except Exception:  # noqa: BLE001 - owner verification failure is evidence.
                invalid.append(required)
                continue
            if artifact.artifact_id in seen_artifacts:
                invalid.append(required)
                continue
            seen_artifacts.add(artifact.artifact_id)
            verified.append(
                StableArtifactRef(
                    ref_id=artifact.artifact_id,
                    uri=artifact.uri,
                    digest=observed.sha256,
                    media_type=str(
                        artifact.metadata.get("content_type")
                        or "application/octet-stream"
                    ),
                )
            )
        return (
            tuple(sorted(verified, key=lambda item: item.ref_id)),
            _strings(invalid),
        )

    def _permission_projection(self) -> tuple[tuple[str, ...], str, str]:
        if self.permission_queue is None:
            return ("permission-owner-unavailable",), "", ""
        try:
            pending = tuple(self.permission_queue.pending())
        except Exception:  # noqa: BLE001 - permission uncertainty forbids exit.
            return ("permission-owner-unavailable",), "", ""
        rows = [
            {
                "request_id": str(getattr(item, "request_id", "")),
                "status": str(
                    getattr(getattr(item, "status", ""), "value", getattr(item, "status", ""))
                ),
                "revision": int(getattr(item, "revision", 0)),
                "session_id": str(getattr(item, "session_id", "")),
            }
            for item in pending
        ]
        session_id = str(getattr(self.permission_queue, "session_id", ""))
        owner = (
            f"permission-queue://{session_id}"
            if session_id
            else "permission-queue://canonical"
        )
        return (
            _strings(item["request_id"] or "unknown" for item in rows),
            owner,
            canonical_digest(rows),
        )

    def _side_effect_projection(
        self,
        checkpoint: RecoveryCheckpoint,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str, str]:
        checkpoint_required = _strings(checkpoint.side_effect_fence_keys)
        owner = f"recovery-side-effect-store://{checkpoint.run_id}/{checkpoint.task_id}"
        if self.side_effect_store is None:
            unknown = checkpoint_required or ("side-effect-owner-unavailable",)
            return checkpoint_required, (), unknown, "", ""
        try:
            canonical_fences = tuple(
                self.side_effect_store.side_effect_fences(
                    run_id=checkpoint.run_id,
                    task_id=checkpoint.task_id,
                )
            )
        except Exception:  # noqa: BLE001 - uncertainty forbids exit.
            unknown = checkpoint_required or ("side-effect-owner-unavailable",)
            return checkpoint_required, (), unknown, owner, ""
        by_key = {
            str(getattr(item, "fence_key", "")): item
            for item in canonical_fences
            if str(getattr(item, "fence_key", ""))
        }
        required = _strings((*checkpoint_required, *by_key))
        states: list[dict[str, Any]] = []
        pending: list[str] = []
        unknown: list[str] = []
        for key in required:
            fence = by_key.get(key)
            if fence is None:
                unknown.append(key)
                continue
            if (
                str(getattr(fence, "run_id", "")) != checkpoint.run_id
                or str(getattr(fence, "task_id", "")) != checkpoint.task_id
            ):
                unknown.append(key)
                continue
            state = SideEffectState(fence.state)
            states.append(
                {
                    "fence_key": key,
                    "state": state.value,
                    "revision": int(fence.revision),
                    "receipt_ref": str(fence.receipt_ref or ""),
                }
            )
            if state in {SideEffectState.RESERVED, SideEffectState.STARTED}:
                pending.append(key)
            elif state not in {
                SideEffectState.COMMITTED,
                SideEffectState.FAILED,
                SideEffectState.CANCELLED,
            }:
                unknown.append(key)
        return (
            required,
            _strings(pending),
            _strings(unknown),
            owner,
            canonical_digest(states),
        )


def _task_obligation_scope(task: TaskState) -> tuple[str, ...]:
    values: list[str] = [
        *task.constraints.requirements,
        *task.constraints.success_criteria,
    ]
    for node in task.plan_nodes.values():
        values.extend(node.constraints.requirements)
        values.extend(node.completion_criteria)
    return _strings(str(item).strip() for item in values)


def _execution_projection(
    task: TaskState,
) -> tuple[tuple[str, ...], tuple[str, ...], str, str]:
    operator_refs: list[str] = []
    verification_refs: list[str] = []
    rows: list[dict[str, Any]] = []
    for decision in task.decisions:
        if decision.decision_type == "operator_execution":
            metadata = dict(decision.metadata)
            supplied = str(metadata.pop("receipt_digest", ""))
            valid = (
                metadata.get("schema") == OPERATOR_EXECUTION_SCHEMA
                and supplied == canonical_digest(metadata)
                and metadata.get("status") == "completed"
            )
            if not valid:
                continue
            operator_refs.extend(str(item) for item in metadata.get("operator_refs") or ())
            verification_refs.extend(
                str(item) for item in metadata.get("verification_refs") or ()
            )
            rows.append({**metadata, "receipt_digest": supplied})
        elif decision.decision_type == "final_verifier":
            metadata = dict(decision.metadata)
            if metadata.get("schema") == FINAL_VERIFIER_SCHEMA:
                verification_refs.extend(
                    str(item) for item in metadata.get("verification_refs") or ()
                )
    owner = f"task-decisions://{task.run_id}/{task.task_id}"
    return (
        _strings(operator_refs),
        _strings(verification_refs),
        owner,
        canonical_digest(rows),
    )


def _final_verifier_projection(
    *,
    task: TaskState,
    requirement_revision: str,
    expected_obligation_ids: Iterable[str],
    verified_artifact_refs: Sequence[StableArtifactRef],
) -> tuple[str, str, bool, str]:
    selected = next(
        (
            item
            for item in reversed(task.decisions)
            if item.decision_type == "final_verifier"
        ),
        None,
    )
    if selected is None:
        return "", "", False, ""
    payload = dict(selected.metadata)
    supplied = str(payload.pop("receipt_digest", ""))
    if (
        payload.get("schema") != FINAL_VERIFIER_SCHEMA
        or supplied != canonical_digest(payload)
    ):
        return f"task-decision://{selected.decision_id}", supplied, False, ""
    expected_input = _final_verifier_input_digest(
        task=task,
        requirement_revision=requirement_revision,
        expected_obligation_ids=expected_obligation_ids,
        verified_artifact_refs=verified_artifact_refs,
    )
    artifact_payload = payload.get("artifact_refs")
    try:
        artifact_refs = (
            tuple(
                StableArtifactRef.from_mapping(item)
                for item in artifact_payload
                if isinstance(item, Mapping)
            )
            if isinstance(artifact_payload, Sequence)
            and not isinstance(artifact_payload, (str, bytes, bytearray))
            else ()
        )
    except (TypeError, ValueError):
        artifact_refs = ()
    valid = bool(
        payload.get("run_id") == task.run_id
        and payload.get("task_id") == task.task_id
        and payload.get("requirement_revision") == requirement_revision
        and _strings(payload.get("expected_obligation_ids") or ())
        == _strings(expected_obligation_ids)
        and tuple(item.to_dict() for item in artifact_refs)
        == tuple(item.to_dict() for item in verified_artifact_refs)
        and payload.get("input_digest") == expected_input
        and payload.get("passed") is True
        and payload.get("verifier_version")
        and payload.get("verifier_receipt_ref")
        and payload.get("fresh_until")
    )
    return (
        str(payload.get("verifier_receipt_ref") or f"task-decision://{selected.decision_id}"),
        supplied,
        valid,
        str(payload.get("fresh_until") or ""),
    )


def minimum_operator_path(
    proposal: OperatorSelectionProposal,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not proposal.layers or not proposal.layers[0].candidates:
        raise EarlyExitError(
            "early_exit_minimum_path_missing",
            "operator proposal has no executable first layer",
        )
    required = [_candidate_ref(proposal.layers[0].candidates[0])]
    verifier_candidate = next(
        (
            item
            for item in proposal.candidates
            if item.score_components.verifier_necessity >= 10_000
        ),
        None,
    )
    if verifier_candidate is not None:
        required.append(_candidate_ref(verifier_candidate))
    return _strings(required), ("final_verifier",)


__all__ = [
    "EARLY_EXIT_CONFIG_SCHEMA",
    "EXIT_CHECKPOINT_BINDING_SCHEMA",
    "EXIT_DECISION_SCHEMA",
    "EXIT_ELIGIBILITY_SCHEMA",
    "EXIT_RESTORE_SCHEMA",
    "FINAL_VERIFIER_SCHEMA",
    "OPERATOR_EXECUTION_SCHEMA",
    "CanonicalExitSnapshotBuilder",
    "DeterministicEarlyExitGate",
    "EarlyExitCheckpointBinding",
    "EarlyExitError",
    "EarlyExitGateConfig",
    "ExitConditionResult",
    "ExitDecision",
    "ExitDecisionReceipt",
    "ExitEligibilitySnapshot",
    "ExitPosteriorResult",
    "ExitRestoreValidation",
    "build_final_verifier_decision",
    "build_operator_execution_decision",
    "minimum_operator_path",
]
