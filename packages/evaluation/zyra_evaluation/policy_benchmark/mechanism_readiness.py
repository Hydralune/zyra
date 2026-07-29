from __future__ import annotations

import ast
import fnmatch
import json
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    P2_BASE_COMMIT,
    ContractViolation,
    MechanismReadinessStatus,
    canonical_digest,
    file_digest,
    parse_mechanism_status,
)
from .evidence_index import (
    BASELINE_MANIFEST_DIGEST,
    EvidenceEvent,
    ReadOnlyEvidenceIndex,
    SourceRunEvidence,
    build_read_only_evidence_index,
    parse_timestamp,
)


READINESS_REPORT_SCHEMA = "zyra.mechanism-evidence-readiness-report/v1"
READINESS_CONFIG_SCHEMA = "zyra.phase2-mechanism-readiness-config/v1"
READINESS_STAGE = "input_precheck"
MECHANISM_IDS = ("arg_designer", "card", "agentprune", "maas")
CAUSAL_LINKS = (
    "proposal",
    "symbolic_verdict",
    "commit_or_route",
    "execution",
    "artifact",
    "verification_outcome",
)
ALLOWED_STAGES = {
    "input_precheck",
    "implementation_validated",
    "activation_ready",
}


class MechanismExecutionMode(str, Enum):
    DEFAULT = "default"
    VALIDATION = "validation"
    DIAGNOSTIC = "diagnostic"
    BASELINE = "baseline"


@dataclass(frozen=True)
class MechanismResolution:
    mechanism_id: str
    status: MechanismReadinessStatus
    stage: str
    mode: MechanismExecutionMode
    canonical_mutation_allowed: bool
    fallback_profile: str
    reason: str
    report_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_id": self.mechanism_id,
            "status": self.status.value,
            "stage": self.stage,
            "mode": self.mode.value,
            "canonical_mutation_allowed": self.canonical_mutation_allowed,
            "fallback_profile": self.fallback_profile,
            "reason": self.reason,
            "report_digest": self.report_digest,
        }


@dataclass(frozen=True)
class InputFieldContract:
    field_id: str
    canonical_owner: str
    owner_domain: str
    decision_timing: str
    evidence_key: str
    max_age_seconds: int
    minimum_confidence: float
    missingness: str
    required: bool

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        required: bool,
    ) -> "InputFieldContract":
        field_id = _text(value.get("field_id"), "input field_id")
        maximum_age = int(value.get("max_age_seconds", -1))
        if maximum_age < 0:
            raise ContractViolation(
                "readiness-field-freshness-invalid",
                f"{field_id} must declare a non-negative freshness bound.",
                path=f"mechanism_readiness.{field_id}.max_age_seconds",
            )
        confidence = float(value.get("minimum_confidence", -1.0))
        if not 0.0 <= confidence <= 1.0:
            raise ContractViolation(
                "readiness-field-confidence-invalid",
                f"{field_id} has an invalid confidence threshold.",
                path=f"mechanism_readiness.{field_id}.minimum_confidence",
            )
        missingness = _text(value.get("missingness"), f"{field_id}.missingness")
        if required and missingness not in {"fail_closed", "explicit_baseline"}:
            raise ContractViolation(
                "readiness-required-missingness-invalid",
                f"{field_id} must fail closed or use the explicit baseline.",
                path=f"mechanism_readiness.{field_id}.missingness",
            )
        return cls(
            field_id=field_id,
            canonical_owner=_text(
                value.get("canonical_owner"),
                f"{field_id}.canonical_owner",
            ),
            owner_domain=_text(value.get("owner_domain"), f"{field_id}.owner_domain"),
            decision_timing=_text(
                value.get("decision_timing"),
                f"{field_id}.decision_timing",
            ),
            evidence_key=_text(value.get("evidence_key"), f"{field_id}.evidence_key"),
            max_age_seconds=maximum_age,
            minimum_confidence=confidence,
            missingness=missingness,
            required=required,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "canonical_owner": self.canonical_owner,
            "owner_domain": self.owner_domain,
            "decision_timing": self.decision_timing,
            "evidence_key": self.evidence_key,
            "max_age_seconds": self.max_age_seconds,
            "minimum_confidence": self.minimum_confidence,
            "missingness": self.missingness,
            "required": self.required,
        }


@dataclass(frozen=True)
class MechanismContract:
    mechanism_id: str
    display_name: str
    decision_event_type: str
    semantic_effect: str
    deterministic_decision_contract: str
    causal_receipt_contract: tuple[str, ...]
    fallback: Mapping[str, Any]
    required_inputs: tuple[InputFieldContract, ...]
    optional_inputs: tuple[InputFieldContract, ...]
    required_scenarios: tuple[str, ...]
    required_failure_paths: tuple[str, ...]
    allowed_follow_up_scope: tuple[str, ...]
    instrumentation_requirements: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MechanismContract":
        mechanism_id = _text(value.get("mechanism_id"), "mechanism_id")
        required = tuple(
            InputFieldContract.from_dict(_mapping(item, "required input"), required=True)
            for item in _sequence(
                value.get("required_inputs"),
                f"{mechanism_id}.required_inputs",
            )
        )
        optional = tuple(
            InputFieldContract.from_dict(_mapping(item, "optional input"), required=False)
            for item in _sequence(
                value.get("optional_inputs"),
                f"{mechanism_id}.optional_inputs",
            )
        )
        field_ids = [item.field_id for item in (*required, *optional)]
        if len(field_ids) != len(set(field_ids)):
            raise ContractViolation(
                "readiness-input-field-duplicate",
                f"{mechanism_id} contains a duplicate input field.",
                path=f"mechanism_readiness.{mechanism_id}",
            )
        causal_receipt = tuple(
            _text(item, f"{mechanism_id}.causal_receipt_contract")
            for item in _sequence(
                value.get("causal_receipt_contract"),
                f"{mechanism_id}.causal_receipt_contract",
            )
        )
        if causal_receipt != CAUSAL_LINKS:
            raise ContractViolation(
                "readiness-causal-contract-invalid",
                f"{mechanism_id} must preserve the full causal-link order.",
                path=f"mechanism_readiness.{mechanism_id}.causal_receipt_contract",
            )
        return cls(
            mechanism_id=mechanism_id,
            display_name=_text(value.get("display_name"), f"{mechanism_id}.display_name"),
            decision_event_type=_text(
                value.get("decision_event_type"),
                f"{mechanism_id}.decision_event_type",
            ),
            semantic_effect=_text(
                value.get("semantic_effect"),
                f"{mechanism_id}.semantic_effect",
            ),
            deterministic_decision_contract=_text(
                value.get("deterministic_decision_contract"),
                f"{mechanism_id}.deterministic_decision_contract",
            ),
            causal_receipt_contract=causal_receipt,
            fallback=MappingProxyType(
                dict(_mapping(value.get("fallback"), f"{mechanism_id}.fallback"))
            ),
            required_inputs=required,
            optional_inputs=optional,
            required_scenarios=tuple(
                _text(item, f"{mechanism_id}.required_scenarios")
                for item in _sequence(
                    value.get("required_scenarios"),
                    f"{mechanism_id}.required_scenarios",
                )
            ),
            required_failure_paths=tuple(
                _text(item, f"{mechanism_id}.required_failure_paths")
                for item in _sequence(
                    value.get("required_failure_paths"),
                    f"{mechanism_id}.required_failure_paths",
                )
            ),
            allowed_follow_up_scope=tuple(
                _text(item, f"{mechanism_id}.allowed_follow_up_scope")
                for item in _sequence(
                    value.get("allowed_follow_up_scope"),
                    f"{mechanism_id}.allowed_follow_up_scope",
                )
            ),
            instrumentation_requirements=tuple(
                _text(item, f"{mechanism_id}.instrumentation_requirements")
                for item in _sequence(
                    value.get("instrumentation_requirements"),
                    f"{mechanism_id}.instrumentation_requirements",
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_id": self.mechanism_id,
            "display_name": self.display_name,
            "decision_event_type": self.decision_event_type,
            "semantic_effect": self.semantic_effect,
            "deterministic_decision_contract": self.deterministic_decision_contract,
            "causal_receipt_contract": list(self.causal_receipt_contract),
            "fallback": dict(self.fallback),
            "required_inputs": [item.to_dict() for item in self.required_inputs],
            "optional_inputs": [item.to_dict() for item in self.optional_inputs],
            "required_scenarios": list(self.required_scenarios),
            "required_failure_paths": list(self.required_failure_paths),
            "allowed_follow_up_scope": list(self.allowed_follow_up_scope),
            "instrumentation_requirements": list(
                self.instrumentation_requirements
            ),
        }


@dataclass(frozen=True)
class MechanismReadinessConfig:
    path: Path
    digest: str
    p2_base_commit: str
    baseline_manifest_digest: str
    default_unknown_status: MechanismReadinessStatus
    fallback_profile: str
    contracts: Mapping[str, MechanismContract]
    no_training_audit: Mapping[str, Any]

    @classmethod
    def load(
        cls,
        repository_root: Path,
        *,
        path: Path | None = None,
    ) -> "MechanismReadinessConfig":
        root = repository_root.resolve()
        selected = (
            path.resolve()
            if path is not None
            else root / "config" / "phase2" / "mechanism-readiness.json"
        )
        try:
            raw_value = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractViolation(
                "readiness-config-invalid",
                "The mechanism readiness config is missing or invalid.",
                path=str(selected),
            ) from exc
        value = _mapping(raw_value, "mechanism_readiness")
        if value.get("schema") != READINESS_CONFIG_SCHEMA:
            raise ContractViolation(
                "readiness-config-schema-invalid",
                "Unsupported mechanism readiness config schema.",
                path=str(selected),
            )
        if value.get("p2_base_commit") != P2_BASE_COMMIT:
            raise ContractViolation(
                "readiness-config-base-mismatch",
                "The readiness config is not bound to the frozen P2 base.",
                path=str(selected),
            )
        if value.get("baseline_manifest_digest") != BASELINE_MANIFEST_DIGEST:
            raise ContractViolation(
                "readiness-config-manifest-mismatch",
                "The readiness config does not use the frozen baseline manifest.",
                path=str(selected),
            )
        if value.get("readiness_stage") != READINESS_STAGE:
            raise ContractViolation(
                "readiness-config-stage-invalid",
                "P2-S00-03 may emit only input_precheck readiness.",
                path=str(selected),
            )
        contracts = tuple(
            MechanismContract.from_dict(_mapping(item, "mechanism contract"))
            for item in _sequence(value.get("mechanisms"), "mechanisms")
        )
        ids = tuple(item.mechanism_id for item in contracts)
        if ids != MECHANISM_IDS:
            raise ContractViolation(
                "readiness-mechanism-order-invalid",
                "Readiness config must contain ARG, CARD, AgentPrune, and MaAS.",
                path="mechanism_readiness.mechanisms",
            )
        owner_domains = _load_owner_domains(root)
        unknown_owner_domains = sorted(
            {
                item.owner_domain
                for contract in contracts
                for item in (*contract.required_inputs, *contract.optional_inputs)
                if item.owner_domain not in owner_domains
            }
        )
        if unknown_owner_domains:
            raise ContractViolation(
                "readiness-owner-domain-unknown",
                "Readiness fields reference unknown canonical owner domains: "
                f"{', '.join(unknown_owner_domains)}.",
                path="mechanism_readiness.mechanisms",
            )
        status = parse_mechanism_status(value.get("default_unknown_status"))
        if status is not MechanismReadinessStatus.UNAVAILABLE:
            raise ContractViolation(
                "readiness-unknown-status-not-closed",
                "Unknown readiness must resolve to unavailable.",
                path="mechanism_readiness.default_unknown_status",
            )
        return cls(
            path=selected,
            digest=file_digest(selected),
            p2_base_commit=P2_BASE_COMMIT,
            baseline_manifest_digest=BASELINE_MANIFEST_DIGEST,
            default_unknown_status=status,
            fallback_profile=_text(value.get("fallback_profile"), "fallback_profile"),
            contracts=MappingProxyType(
                {item.mechanism_id: item for item in contracts}
            ),
            no_training_audit=MappingProxyType(
                dict(
                    _mapping(
                        value.get("no_training_audit"),
                        "no_training_audit",
                    )
                )
            ),
        )


@dataclass(frozen=True)
class FieldObservation:
    field_id: str
    canonical_owner: str
    value_digest: str
    evidence_ref: str
    observed_at: str
    observed_sequence: int
    confidence: float
    corrupt: bool = False

    def to_snapshot_value(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "canonical_owner": self.canonical_owner,
            "value_digest": self.value_digest,
            "evidence_ref": self.evidence_ref,
            "observed_at": self.observed_at,
            "observed_sequence": self.observed_sequence,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class MechanismAuditSample:
    independent_run_id: str
    source_digest: str
    domain: str
    decision_at: str
    decision_sequence: int
    observations: Mapping[str, tuple[FieldObservation, ...]]
    scenarios: frozenset[str]
    failure_paths: frozenset[str]
    causal_links: frozenset[str]
    baseline_causal_chain_complete: bool
    negative_evidence: frozenset[str]
    raw_event_count: int
    derived_run_count: int


@dataclass(frozen=True)
class FieldCoverage:
    field_id: str
    required: bool
    sample_count: int
    available_count: int
    missing_count: int
    future_information_count: int
    stale_count: int
    low_confidence_count: int
    owner_mismatch_count: int
    corrupt_count: int
    conflicting_value_count: int
    coverage_ratio: float
    freshness_ratio: float
    confidence_ratio: float
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "required": self.required,
            "sample_count": self.sample_count,
            "available_count": self.available_count,
            "missing_count": self.missing_count,
            "future_information_count": self.future_information_count,
            "stale_count": self.stale_count,
            "low_confidence_count": self.low_confidence_count,
            "owner_mismatch_count": self.owner_mismatch_count,
            "corrupt_count": self.corrupt_count,
            "conflicting_value_count": self.conflicting_value_count,
            "coverage_ratio": self.coverage_ratio,
            "freshness_ratio": self.freshness_ratio,
            "confidence_ratio": self.confidence_ratio,
            "evidence_refs": list(self.evidence_refs),
        }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractViolation(
            "readiness-mapping-required",
            f"{label} must be a mapping.",
            path=label,
        )
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractViolation(
            "readiness-sequence-required",
            f"{label} must be a sequence.",
            path=label,
        )
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation(
            "readiness-text-required",
            f"{label} must be non-empty text.",
            path=label,
        )
    return value.strip()


def _load_owner_domains(repository_root: Path) -> set[str]:
    path = repository_root / "config" / "phase2" / "state-owners.yaml"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractViolation(
            "readiness-owner-registry-invalid",
            "The state-owner registry is missing or invalid.",
            path=str(path),
        ) from exc
    selected = _mapping(value, "state_owners")
    return {
        _text(_mapping(item, "owner domain").get("domain_id"), "owner domain id")
        for item in _sequence(selected.get("state_domains"), "state domains")
    }


def _value_digest(value: object) -> str:
    return canonical_digest(value)


def _event_observation(
    field: InputFieldContract,
    event: EvidenceEvent,
    value: object,
    source: SourceRunEvidence,
) -> FieldObservation:
    return FieldObservation(
        field_id=field.field_id,
        canonical_owner=field.canonical_owner,
        value_digest=_value_digest(value),
        evidence_ref=f"{source.archive_path}#event:{event.event_id}",
        observed_at=event.created_at,
        observed_sequence=event.sequence,
        confidence=1.0,
    )


def _external_observation(
    field: InputFieldContract,
    *,
    value: object,
    evidence_ref: str,
    observed_at: str,
    sequence: int,
    confidence: float = 1.0,
) -> FieldObservation:
    return FieldObservation(
        field_id=field.field_id,
        canonical_owner=field.canonical_owner,
        value_digest=_value_digest(value),
        evidence_ref=evidence_ref,
        observed_at=observed_at,
        observed_sequence=sequence,
        confidence=confidence,
    )


def _first_planned_at(source: SourceRunEvidence) -> str:
    values = [
        str(_mapping(receipt.get("cell"), "run receipt cell").get("planned_at", ""))
        for receipt in source.run_receipts
    ]
    return min((value for value in values if value), default="")


def _input_revision(source: SourceRunEvidence) -> str:
    values = {
        str(_mapping(receipt.get("cell"), "run receipt cell").get("input_revision", ""))
        for receipt in source.run_receipts
    }
    values.discard("")
    return next(iter(values)) if len(values) == 1 else ""


def _scenario_profile(source: SourceRunEvidence) -> Mapping[str, Any]:
    scenario = source.environment.get("scenario")
    if not isinstance(scenario, Mapping):
        return {}
    profile = scenario.get("profile")
    return profile if isinstance(profile, Mapping) else {}


def _owner_routes(source: SourceRunEvidence) -> tuple[Mapping[str, Any], ...]:
    placement = source.owner_receipts.get("placement_receipt")
    if not isinstance(placement, Mapping):
        return ()
    routes: list[Mapping[str, Any]] = []
    initial = placement.get("initial_route")
    if isinstance(initial, Mapping):
        routes.append(initial)
    migrated = placement.get("migrated_routes")
    if isinstance(migrated, Sequence) and not isinstance(migrated, (str, bytes)):
        routes.extend(item for item in migrated if isinstance(item, Mapping))
    return tuple(routes)


def _first_metric_sample(
    source: SourceRunEvidence,
    metric_id: str,
) -> Mapping[str, Any] | None:
    values = source.raw_samples_for(metric_id)
    return values[0] if values else None


def _field_observations(
    field: InputFieldContract,
    source: SourceRunEvidence,
) -> tuple[FieldObservation, ...]:
    key = field.evidence_key
    maximum_sequence = source.maximum_sequence
    if key == "event.task_created.phase":
        event = source.first_event("task_created")
        return (
            (_event_observation(field, event, event.stage, source),)
            if event is not None and event.stage
            else ()
        )
    if key == "campaign.input_revision":
        revision = _input_revision(source)
        planned_at = _first_planned_at(source)
        return (
            (
                _external_observation(
                    field,
                    value=revision,
                    evidence_ref=(
                        "docs/reviews/evidence/M3-S02A-02/"
                        "formal-current-09e99cdc/run-receipts.json"
                    ),
                    observed_at=planned_at,
                    sequence=0,
                ),
            )
            if revision and planned_at
            else ()
        )
    if key == "event.task_updated.unresolved":
        values = []
        for event in source.events_of_type("task_updated"):
            state = str(event.mutation.get("action_state", ""))
            if state not in {"completed", "settled"}:
                values.append(
                    _event_observation(
                        field,
                        event,
                        {
                            "action_id": event.mutation.get("action_id"),
                            "action_state": state,
                        },
                        source,
                    )
                )
        return tuple(values)
    if key == "environment.worker_classes":
        profile = _scenario_profile(source)
        workers = profile.get("worker_classes")
        captured_at = str(source.environment.get("captured_at", ""))
        return (
            (
                _external_observation(
                    field,
                    value=workers,
                    evidence_ref=f"{source.archive_path}#environment.json",
                    observed_at=captured_at,
                    sequence=maximum_sequence + 1,
                ),
            )
            if isinstance(workers, Sequence)
            and not isinstance(workers, (str, bytes))
            and captured_at
            else ()
        )
    if key == "event.topology_mutation.snapshot":
        event = source.first_event("topology_mutation")
        return (
            (
                _event_observation(
                    field,
                    event,
                    {
                        "nodes": event.mutation.get("added_nodes", ()),
                        "edges": event.mutation.get("added_edges", ()),
                        "revision": event.mutation.get("topology_revision"),
                    },
                    source,
                ),
            )
            if event is not None
            else ()
        )
    if key == "event.topology_mutation.edges":
        event = source.first_event("topology_mutation")
        return (
            (
                _event_observation(
                    field,
                    event,
                    event.mutation.get("added_edges", ()),
                    source,
                ),
            )
            if event is not None
            else ()
        )
    if key == "domain_verification.continuity":
        verified_at = str(source.domain_verification.get("verified_at", ""))
        checks = source.domain_verification.get("checks")
        value = (
            {
                key: selected
                for key, selected in checks.items()
                if key.startswith("faults.") or key.startswith("events.")
            }
            if isinstance(checks, Mapping)
            else {}
        )
        return (
            (
                _external_observation(
                    field,
                    value=value,
                    evidence_ref=f"{source.archive_path}#domain-verification.json",
                    observed_at=verified_at,
                    sequence=maximum_sequence + 1,
                ),
            )
            if value and verified_at
            else ()
        )
    if key == "event.permission.allowed_tiers":
        event = source.first_event("permission_decision")
        return (
            (
                _event_observation(
                    field,
                    event,
                    {
                        "decision": event.mutation.get("decision"),
                        "allowed_tiers": event.mutation.get("allowed_tiers", ()),
                        "denied_tiers": event.mutation.get("denied_tiers", ()),
                    },
                    source,
                ),
            )
            if event is not None
            else ()
        )
    if key in {"owner_route.privacy", "owner_route.location", "owner_route.capability"}:
        observations = []
        for route in _owner_routes(source):
            acquired_at = str(route.get("acquired_at", ""))
            if key == "owner_route.privacy":
                value = route.get("privacy_class")
            elif key == "owner_route.location":
                value = {
                    "location": route.get("location"),
                    "tier": route.get("tier"),
                    "backend_id": route.get("backend_id"),
                }
            else:
                value = {
                    "requested": route.get("requested_capabilities", ()),
                    "leased": route.get("leased_capabilities", ()),
                }
            if acquired_at and value:
                observations.append(
                    _external_observation(
                        field,
                        value=value,
                        evidence_ref=f"{source.archive_path}#owner-receipts.json",
                        observed_at=acquired_at,
                        sequence=maximum_sequence + 1,
                    )
                )
        return tuple(observations)
    if key == "current_tier.health":
        return tuple(
            _external_observation(
                field,
                value={
                    "tier": item.get("tier"),
                    "handshake_ok": item.get("handshake_ok"),
                    "dispatch_status": item.get("dispatch_status"),
                },
                evidence_ref=(
                    "docs/reviews/evidence/M3-S02A-02/formal-current-09e99cdc/"
                    "current-campaign-evidence.json"
                ),
                observed_at=str(item.get("completed_at", "")),
                sequence=maximum_sequence + 1,
                confidence=1.0 if item.get("fresh") is True else 0.0,
            )
            for item in source.tier_observations
            if item.get("completed_at")
        )
    if key == "current_tier.freshness":
        return tuple(
            _external_observation(
                field,
                value={"tier": item.get("tier"), "fresh": item.get("fresh")},
                evidence_ref=(
                    "docs/reviews/evidence/M3-S02A-02/formal-current-09e99cdc/"
                    "current-campaign-evidence.json"
                ),
                observed_at=str(item.get("completed_at", "")),
                sequence=maximum_sequence + 1,
                confidence=1.0 if item.get("fresh") is True else 0.0,
            )
            for item in source.tier_observations
            if item.get("completed_at")
        )
    if key == "current_provider.latency":
        return tuple(
            _external_observation(
                field,
                value={
                    "provider_id": item.get("provider_id"),
                    "latency_ms": item.get("latency_ms"),
                },
                evidence_ref=(
                    "docs/reviews/evidence/M3-S02A-02/formal-current-09e99cdc/"
                    "current-campaign-evidence.json"
                ),
                observed_at=str(item.get("completed_at", "")),
                sequence=maximum_sequence + 1,
                confidence=1.0 if item.get("fresh") is True else 0.0,
            )
            for item in source.provider_observations
            if item.get("completed_at")
        )
    if key == "current_provider.cost_tokens":
        return tuple(
            _external_observation(
                field,
                value={
                    "provider_id": item.get("provider_id"),
                    "usage": item.get("usage"),
                },
                evidence_ref=(
                    "docs/reviews/evidence/M3-S02A-02/formal-current-09e99cdc/"
                    "current-campaign-evidence.json"
                ),
                observed_at=str(item.get("completed_at", "")),
                sequence=maximum_sequence + 1,
                confidence=1.0 if item.get("fresh") is True else 0.0,
            )
            for item in source.provider_observations
            if item.get("completed_at")
        )
    if key.startswith("raw_metric."):
        metric_id = key.removeprefix("raw_metric.")
        sample = _first_metric_sample(source, metric_id)
        if sample is None:
            return ()
        observed_at = str(sample.get("observed_at", ""))
        return (
            _external_observation(
                field,
                value={
                    "metric_id": metric_id,
                    "unit": sample.get("unit"),
                    "value": sample.get("value"),
                },
                evidence_ref=(
                    "docs/reviews/evidence/M3-S02A-02/formal-current-09e99cdc/"
                    "raw-samples.json"
                ),
                observed_at=observed_at,
                sequence=maximum_sequence + 1,
            ),
        )
    if key == "campaign.budget":
        planned_at = _first_planned_at(source)
        receipt = source.run_receipts[0] if source.run_receipts else {}
        cell = receipt.get("cell") if isinstance(receipt, Mapping) else {}
        return (
            (
                _external_observation(
                    field,
                    value={
                        "condition_digest": (
                            cell.get("condition_digest")
                            if isinstance(cell, Mapping)
                            else ""
                        )
                    },
                    evidence_ref=(
                        "docs/reviews/evidence/M3-S02A-02/"
                        "formal-current-09e99cdc/campaign.json"
                    ),
                    observed_at=planned_at,
                    sequence=0,
                ),
            )
            if planned_at
            else ()
        )
    if key == "campaign.verifier_contract":
        planned_at = _first_planned_at(source)
        return (
            (
                _external_observation(
                    field,
                    value={
                        "domain_verifier": source.domain_verification.get(
                            "verifier_id"
                        )
                    },
                    evidence_ref=(
                        "docs/reviews/evidence/M3-S02A-02/"
                        "formal-current-09e99cdc/campaign.json"
                    ),
                    observed_at=planned_at,
                    sequence=0,
                ),
            )
            if planned_at
            else ()
        )
    return ()


def _decision_boundary(
    source: SourceRunEvidence,
    event_type: str,
) -> tuple[str, int]:
    event = source.first_event(event_type)
    if event is None:
        first = source.events[0] if source.events else None
        return (
            first.created_at if first is not None else "",
            first.sequence if first is not None else 0,
        )
    return event.created_at, event.sequence


def _scenarios(source: SourceRunEvidence) -> frozenset[str]:
    scenarios = {"normal"}
    event_types = source.event_types
    if "requirement_change" in event_types:
        scenarios.add("requirement_change")
    if {"fault_observed", "recovery_planned"} & event_types:
        scenarios.add("fault_degraded")
    if "recovery_applied" in event_types:
        scenarios.add("recovery")
    if source.tier_observations:
        tiers = {str(item.get("tier")) for item in source.tier_observations}
        if {"device", "edge", "cloud"}.issubset(tiers):
            scenarios.add("placement")
    permission = source.first_event("permission_decision")
    if permission is not None and (
        permission.mutation.get("denied_tiers")
        or permission.mutation.get("privacy_class") not in {None, "", "public"}
    ):
        scenarios.add("privacy")
    return frozenset(scenarios)


def _failure_paths(source: SourceRunEvidence) -> frozenset[str]:
    paths: set[str] = set()
    if source.events_of_type("recovery_applied"):
        paths.add("recovery")
    if len(source.events_of_type("resource_decision")) > 1:
        paths.add("reroute")
    scenario = source.environment.get("scenario")
    if isinstance(scenario, Mapping):
        schedule = scenario.get("fault_schedule")
        if isinstance(schedule, Mapping):
            injections = schedule.get("injections")
            if isinstance(injections, Sequence) and not isinstance(
                injections, (str, bytes)
            ):
                kinds = {
                    str(item.get("kind", ""))
                    for item in injections
                    if isinstance(item, Mapping)
                }
                if {
                    "provider_rate_limit",
                    "provider_failure",
                    "network_loss",
                    "edge_disconnect",
                    "node_lost",
                    "worker_lost",
                } & kinds:
                    paths.add("unavailable")
                if {"tool_timeout", "exception"} & kinds:
                    paths.add("exception")
    permission = source.first_event("permission_decision")
    if permission is not None and permission.mutation.get("decision") == "deny":
        paths.add("reject")
    if any(
        receipt.get("task_succeeded") is False for receipt in source.run_receipts
    ):
        paths.add("invalid")
    return frozenset(paths)


def _causal_links(source: SourceRunEvidence) -> tuple[frozenset[str], bool]:
    event_types = source.event_types
    links: set[str] = set()
    if {
        "policy_proposal",
        "topology_proposal",
        "operator_proposal",
    } & event_types:
        links.add("proposal")
    if "permission_decision" in event_types:
        links.add("symbolic_verdict")
    if {"topology_mutation", "resource_decision", "topology_route"} & event_types:
        links.add("commit_or_route")
    if "tool_call" in event_types:
        links.add("execution")
    if {"artifact_committed", "artifact_written"} & event_types:
        links.add("artifact")
    if {"verification", "delivery_committed"} & event_types:
        links.add("verification_outcome")
    checks = source.domain_verification.get("checks")
    baseline_complete = bool(
        isinstance(checks, Mapping)
        and checks.get("events.single_causal_root") is True
        and checks.get("events.single_delivery_leaf") is True
        and checks.get("events.effect_coverage") is True
    )
    return frozenset(links), baseline_complete


def _negative_evidence(
    source: SourceRunEvidence,
    mechanism_id: str,
) -> frozenset[str]:
    variants = {
        str(_mapping(receipt.get("cell"), "run receipt cell").get("variant_id", ""))
        for receipt in source.run_receipts
    }
    evidence = {"baseline_fallback"} if variants else set()
    if mechanism_id == "arg_designer" and {
        "single-agent",
        "static-full-connect-multi-agent",
    }.issubset(variants):
        evidence.add("mechanism_disable")
    if (
        mechanism_id == "agentprune"
        and "no-low-entropy-communication" in variants
    ):
        evidence.add("mechanism_disable")
    if mechanism_id == "maas" and "no-scheduler" in variants:
        evidence.add("mechanism_disable")
    return frozenset(evidence)


def build_mechanism_samples(
    index: ReadOnlyEvidenceIndex,
    contract: MechanismContract,
) -> tuple[MechanismAuditSample, ...]:
    samples = []
    fields = (*contract.required_inputs, *contract.optional_inputs)
    for source in index.source_runs:
        decision_at, decision_sequence = _decision_boundary(
            source,
            contract.decision_event_type,
        )
        observations = {
            field.field_id: _field_observations(field, source) for field in fields
        }
        causal_links, baseline_complete = _causal_links(source)
        samples.append(
            MechanismAuditSample(
                independent_run_id=source.source_run_id,
                source_digest=source.archive_digest,
                domain=source.domain,
                decision_at=decision_at,
                decision_sequence=decision_sequence,
                observations=MappingProxyType(observations),
                scenarios=_scenarios(source),
                failure_paths=_failure_paths(source),
                causal_links=causal_links,
                baseline_causal_chain_complete=baseline_complete,
                negative_evidence=_negative_evidence(
                    source,
                    contract.mechanism_id,
                ),
                raw_event_count=source.event_count,
                derived_run_count=len(source.run_receipts),
            )
        )
    return tuple(samples)


def _deduplicate_samples(
    samples: Iterable[MechanismAuditSample],
) -> tuple[tuple[MechanismAuditSample, ...], int, bool]:
    selected: dict[str, MechanismAuditSample] = {}
    duplicates = 0
    conflict = False
    for sample in samples:
        existing = selected.get(sample.independent_run_id)
        if existing is None:
            selected[sample.independent_run_id] = sample
            continue
        duplicates += 1
        if existing.source_digest != sample.source_digest:
            conflict = True
    return (
        tuple(
            sorted(
                selected.values(),
                key=lambda item: (item.domain, item.independent_run_id),
            )
        ),
        duplicates,
        conflict,
    )


def _is_future(
    observation: FieldObservation,
    sample: MechanismAuditSample,
) -> bool:
    if observation.observed_sequence >= sample.decision_sequence:
        return True
    observed = parse_timestamp(observation.observed_at)
    decision = parse_timestamp(sample.decision_at)
    return bool(observed is not None and decision is not None and observed > decision)


def _age_seconds(
    observation: FieldObservation,
    sample: MechanismAuditSample,
) -> float:
    observed = parse_timestamp(observation.observed_at)
    decision = parse_timestamp(sample.decision_at)
    if observed is None or decision is None:
        return float("inf")
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    if decision.tzinfo is None:
        decision = decision.replace(tzinfo=timezone.utc)
    return max(0.0, (decision - observed).total_seconds())


def _select_observation(
    field: InputFieldContract,
    sample: MechanismAuditSample,
) -> tuple[FieldObservation | None, Counter[str]]:
    counts: Counter[str] = Counter()
    values = tuple(sample.observations.get(field.field_id, ()))
    if not values:
        counts["missing"] += 1
        return None, counts
    candidates: list[FieldObservation] = []
    valid_digests: set[str] = set()
    for observation in values:
        if observation.corrupt:
            counts["corrupt"] += 1
            continue
        if observation.canonical_owner != field.canonical_owner:
            counts["owner_mismatch"] += 1
            continue
        if _is_future(observation, sample):
            counts["future"] += 1
            continue
        if _age_seconds(observation, sample) > field.max_age_seconds:
            counts["stale"] += 1
            continue
        if observation.confidence < field.minimum_confidence:
            counts["low_confidence"] += 1
            continue
        candidates.append(observation)
        valid_digests.add(observation.value_digest)
    if len(valid_digests) > 1:
        counts["conflicting"] += 1
        return None, counts
    if not candidates:
        counts["missing"] += 1
        return None, counts
    selected = min(
        candidates,
        key=lambda item: (
            item.observed_sequence,
            item.observed_at,
            item.evidence_ref,
            item.value_digest,
        ),
    )
    counts["available"] += 1
    return selected, counts


def _field_coverage(
    field: InputFieldContract,
    samples: Sequence[MechanismAuditSample],
) -> tuple[FieldCoverage, dict[str, FieldObservation]]:
    aggregate: Counter[str] = Counter()
    selected: dict[str, FieldObservation] = {}
    refs: set[str] = set()
    for sample in samples:
        observation, counts = _select_observation(field, sample)
        aggregate.update(counts)
        if observation is not None:
            selected[sample.independent_run_id] = observation
            refs.add(observation.evidence_ref)
    total = len(samples)
    available = aggregate["available"]
    coverage = available / total if total else 0.0
    freshness_denominator = available + aggregate["stale"]
    confidence_denominator = available + aggregate["low_confidence"]
    return (
        FieldCoverage(
            field_id=field.field_id,
            required=field.required,
            sample_count=total,
            available_count=available,
            missing_count=aggregate["missing"],
            future_information_count=aggregate["future"],
            stale_count=aggregate["stale"],
            low_confidence_count=aggregate["low_confidence"],
            owner_mismatch_count=aggregate["owner_mismatch"],
            corrupt_count=aggregate["corrupt"],
            conflicting_value_count=aggregate["conflicting"],
            coverage_ratio=coverage,
            freshness_ratio=(
                available / freshness_denominator if freshness_denominator else 0.0
            ),
            confidence_ratio=(
                available / confidence_denominator
                if confidence_denominator
                else 0.0
            ),
            evidence_refs=tuple(sorted(refs)),
        ),
        selected,
    )


def _snapshot_replay(
    contract: MechanismContract,
    samples: Sequence[MechanismAuditSample],
    selected_fields: Mapping[str, Mapping[str, FieldObservation]],
    *,
    duplicate_conflict: bool,
) -> dict[str, Any]:
    digests: dict[str, str] = {}
    stable = not duplicate_conflict
    for sample in samples:
        values = {
            field.field_id: selected_fields.get(field.field_id, {}).get(
                sample.independent_run_id
            ).to_snapshot_value()
            for field in (*contract.required_inputs, *contract.optional_inputs)
            if selected_fields.get(field.field_id, {}).get(
                sample.independent_run_id
            )
            is not None
        }
        snapshot = {
            "schema": "zyra.phase2-mechanism-input-snapshot/v1",
            "mechanism_id": contract.mechanism_id,
            "independent_run_id": sample.independent_run_id,
            "source_digest": sample.source_digest,
            "decision_at": sample.decision_at,
            "decision_sequence": sample.decision_sequence,
            "fields": values,
        }
        first = canonical_digest(snapshot)
        reordered = {
            **snapshot,
            "fields": {
                key: values[key] for key in sorted(values, reverse=True)
            },
        }
        second = canonical_digest(reordered)
        if first != second:
            stable = False
        digests[sample.independent_run_id] = first
    replay_digest = canonical_digest(dict(sorted(digests.items())))
    return {
        "match": stable,
        "input_snapshot_count": len(digests),
        "input_snapshot_digests": dict(sorted(digests.items())),
        "replay_digest": replay_digest,
        "mechanism_output_determinism_deferred_to": "P2-03/P2-04",
    }


def evaluate_mechanism(
    contract: MechanismContract,
    samples: Iterable[MechanismAuditSample],
    *,
    no_training_passed: bool = True,
) -> dict[str, Any]:
    unique_samples, discarded_duplicates, duplicate_conflict = (
        _deduplicate_samples(samples)
    )
    field_results: list[FieldCoverage] = []
    selected_fields: dict[str, Mapping[str, FieldObservation]] = {}
    for field in (*contract.required_inputs, *contract.optional_inputs):
        coverage, selected = _field_coverage(field, unique_samples)
        field_results.append(coverage)
        selected_fields[field.field_id] = selected
    required_results = [item for item in field_results if item.required]
    required_inputs_complete = bool(required_results) and all(
        item.coverage_ratio == 1.0
        and item.owner_mismatch_count == 0
        and item.corrupt_count == 0
        and item.conflicting_value_count == 0
        for item in required_results
    )

    scenarios = {
        scenario for sample in unique_samples for scenario in sample.scenarios
    }
    failure_paths = {
        path for sample in unique_samples for path in sample.failure_paths
    }
    negative_evidence = {
        item for sample in unique_samples for item in sample.negative_evidence
    }
    domains = {sample.domain for sample in unique_samples}
    causal_complete = sum(
        set(contract.causal_receipt_contract).issubset(sample.causal_links)
        for sample in unique_samples
    )
    baseline_causal_complete = sum(
        sample.baseline_causal_chain_complete for sample in unique_samples
    )
    causal_ratio = (
        causal_complete / len(unique_samples) if unique_samples else 0.0
    )
    scenario_complete = (
        len(domains) >= 2
        and set(contract.required_scenarios).issubset(scenarios)
    )
    failure_complete = set(contract.required_failure_paths).issubset(
        failure_paths
    )
    negative_complete = {
        "mechanism_disable",
        "baseline_fallback",
        "stale_or_corrupt_fail_closed",
        "projector_reject",
        "mechanism_exception",
    }.issubset(negative_evidence)
    replay = _snapshot_replay(
        contract,
        unique_samples,
        selected_fields,
        duplicate_conflict=(
            duplicate_conflict
            or any(
                item.conflicting_value_count > 0 or item.corrupt_count > 0
                for item in field_results
            )
        ),
    )

    reasons: list[str] = []
    gaps: list[str] = []
    if not unique_samples:
        reasons.append("no independent frozen source runs were available")
    if not required_inputs_complete:
        missing_fields = [
            item.field_id
            for item in required_results
            if item.coverage_ratio != 1.0
            or item.owner_mismatch_count
            or item.corrupt_count
            or item.conflicting_value_count
        ]
        reasons.append(
            "required decision-time canonical inputs are incomplete: "
            + ", ".join(missing_fields)
        )
        gaps.extend(f"required_input:{field_id}" for field_id in missing_fields)
    if causal_ratio != 1.0:
        reasons.append(
            "mechanism proposal-to-outcome causal chains are not complete"
        )
        gaps.append("mechanism_causal_receipt")
    if not scenario_complete:
        missing_scenarios = sorted(
            set(contract.required_scenarios) - scenarios
        )
        reasons.append(
            "required scenario coverage is incomplete: "
            + ", ".join(missing_scenarios)
        )
        gaps.extend(f"scenario:{item}" for item in missing_scenarios)
    if not failure_complete:
        missing_failures = sorted(
            set(contract.required_failure_paths) - failure_paths
        )
        reasons.append(
            "required failure-path coverage is incomplete: "
            + ", ".join(missing_failures)
        )
        gaps.extend(f"failure_path:{item}" for item in missing_failures)
    if replay["match"] is not True:
        reasons.append("canonical input snapshot replay is inconsistent")
        gaps.append("deterministic_input_snapshot")
    if not negative_complete:
        reasons.append("mechanism disconnect and fail-closed evidence is incomplete")
        gaps.append("negative_evidence")
    if not no_training_passed:
        reasons.append("the no-policy-training audit failed")
        gaps.append("no_policy_training")

    if not no_training_passed or not required_inputs_complete:
        status = MechanismReadinessStatus.UNAVAILABLE
    elif (
        causal_ratio != 1.0
        or not scenario_complete
        or not failure_complete
        or replay["match"] is not True
        or not negative_complete
    ):
        status = MechanismReadinessStatus.EVIDENCE_ONLY
    else:
        status = MechanismReadinessStatus.DETERMINISTIC_READY
    if not reasons:
        reasons.append(
            "all frozen input-precheck gates passed; implementation behavior "
            "validation remains deferred"
        )

    return {
        "mechanism_id": contract.mechanism_id,
        "display_name": contract.display_name,
        "readiness_stage": READINESS_STAGE,
        "status": status.value,
        "status_reasons": reasons,
        "gaps": sorted(set(gaps)),
        "contract": contract.to_dict(),
        "volume": {
            "independent_source_run_count": len(unique_samples),
            "discarded_duplicate_source_run_count": discarded_duplicates,
            "derived_formal_run_count": sum(
                sample.derived_run_count for sample in unique_samples
            ),
            "raw_event_count": sum(
                sample.raw_event_count for sample in unique_samples
            ),
            "training_sample_count": 0,
            "semantic_label": "evidence_volume_only",
        },
        "field_coverage": [item.to_dict() for item in field_results],
        "required_input_coverage_ratio": (
            sum(item.coverage_ratio for item in required_results)
            / len(required_results)
            if required_results
            else 0.0
        ),
        "required_input_gate_passed": required_inputs_complete,
        "domains": sorted(domains),
        "scenario_coverage": {
            "observed": sorted(scenarios),
            "required": list(contract.required_scenarios),
            "passed": scenario_complete,
        },
        "failure_path_coverage": {
            "observed": sorted(failure_paths),
            "required": list(contract.required_failure_paths),
            "passed": failure_complete,
        },
        "causal_links": {
            "required": list(contract.causal_receipt_contract),
            "complete_chain_count": causal_complete,
            "completeness_ratio": causal_ratio,
            "baseline_chain_complete_count": baseline_causal_complete,
            "baseline_chain_completeness_ratio": (
                baseline_causal_complete / len(unique_samples)
                if unique_samples
                else 0.0
            ),
            "formal_mechanism_effect_evidence_allowed": causal_ratio == 1.0,
        },
        "deterministic_input_snapshot_replay": replay,
        "negative_evidence": {
            "observed": sorted(negative_evidence),
            "passed": negative_complete,
            "missing": sorted(
                {
                    "mechanism_disable",
                    "baseline_fallback",
                    "stale_or_corrupt_fail_closed",
                    "projector_reject",
                    "mechanism_exception",
                }
                - negative_evidence
            ),
        },
        "diagnostic_boundary": {
            "evidence_only_is_read_only": True,
            "may_change_graph_route_lease_or_side_effect": False,
        },
        "fallback": dict(contract.fallback),
        "allowed_follow_up_scope": list(contract.allowed_follow_up_scope),
        "required_instrumentation": list(contract.instrumentation_requirements),
    }


def run_no_training_audit(
    repository_root: Path,
    config: MechanismReadinessConfig,
) -> dict[str, Any]:
    root = repository_root.resolve()
    policy = config.no_training_audit
    findings: list[dict[str, str]] = []
    for field_name in (
        "runtime_entry_points",
        "training_datasets",
        "training_checkpoints",
        "mutable_learned_parameters",
    ):
        values = _sequence(policy.get(field_name), f"no_training_audit.{field_name}")
        if values:
            findings.append(
                {
                    "code": "declared-training-artifact",
                    "path": f"no_training_audit.{field_name}",
                    "detail": f"{field_name} must remain empty",
                }
            )

    forbidden_patterns = tuple(
        _text(item, "forbidden training path pattern")
        for item in _sequence(
            policy.get("forbidden_path_patterns"),
            "no_training_audit.forbidden_path_patterns",
        )
    )
    forbidden_symbols = tuple(
        _text(item, "forbidden training symbol").casefold()
        for item in _sequence(
            policy.get("forbidden_symbol_tokens"),
            "no_training_audit.forbidden_symbol_tokens",
        )
    )
    scanned_files: list[str] = []
    checked_roots: list[str] = []
    for value in _sequence(policy.get("scan_roots"), "no_training_audit.scan_roots"):
        relative = Path(_text(value, "no_training_audit scan root").replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            findings.append(
                {
                    "code": "training-scan-root-invalid",
                    "path": str(relative),
                    "detail": "scan root must remain repository-relative",
                }
            )
            continue
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            findings.append(
                {
                    "code": "training-scan-root-invalid",
                    "path": str(relative),
                    "detail": "scan root resolved outside the repository",
                }
            )
            continue
        checked_roots.append(relative.as_posix())
        if not target.exists():
            continue
        files = (target,) if target.is_file() else tuple(target.rglob("*"))
        for candidate in files:
            if not candidate.is_file():
                continue
            relative_candidate = candidate.relative_to(root).as_posix()
            scanned_files.append(relative_candidate)
            lowered = relative_candidate.casefold()
            for pattern in forbidden_patterns:
                if fnmatch.fnmatch(lowered, pattern.casefold()):
                    findings.append(
                        {
                            "code": "training-path-detected",
                            "path": relative_candidate,
                            "detail": f"matched forbidden pattern {pattern}",
                        }
                    )
            if candidate.suffix.casefold() == ".py":
                try:
                    tree = ast.parse(
                        candidate.read_text(encoding="utf-8"),
                        filename=relative_candidate,
                    )
                except (OSError, UnicodeDecodeError, SyntaxError) as exc:
                    findings.append(
                        {
                            "code": "mechanism-python-scan-failed",
                            "path": relative_candidate,
                            "detail": type(exc).__name__,
                        }
                    )
                    continue
                symbols: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(
                        node,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    ):
                        symbols.add(node.name.casefold())
                    elif isinstance(node, ast.Name):
                        symbols.add(node.id.casefold())
                    elif isinstance(node, ast.Attribute):
                        symbols.add(node.attr.casefold())
                matched_symbols = sorted(
                    symbol
                    for symbol in symbols
                    if any(
                        token == symbol
                        or symbol.startswith(f"{token}_")
                        or symbol.endswith(f"_{token}")
                        for token in forbidden_symbols
                    )
                )
                if matched_symbols:
                    findings.append(
                        {
                            "code": "training-symbol-detected",
                            "path": relative_candidate,
                            "detail": ", ".join(matched_symbols),
                        }
                    )

    forbidden_dependencies = {
        _text(item, "forbidden training dependency").casefold()
        for item in _sequence(
            policy.get("forbidden_dependencies"),
            "no_training_audit.forbidden_dependencies",
        )
    }
    pyproject = root / "pyproject.toml"
    dependencies: list[str] = []
    if pyproject.is_file():
        value = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = value.get("project")
        if isinstance(project, Mapping):
            raw_dependencies = project.get("dependencies")
            if isinstance(raw_dependencies, Sequence) and not isinstance(
                raw_dependencies, (str, bytes)
            ):
                dependencies = [str(item) for item in raw_dependencies]
        for dependency in dependencies:
            normalized = (
                dependency.split("[", 1)[0]
                .split("=", 1)[0]
                .split("<", 1)[0]
                .split(">", 1)[0]
                .strip()
                .casefold()
            )
            if normalized in forbidden_dependencies:
                findings.append(
                    {
                        "code": "training-dependency-detected",
                        "path": "pyproject.toml",
                        "detail": dependency,
                    }
                )
    return {
        "passed": not findings,
        "training_sample_count": 0,
        "runtime_entry_point_count": len(policy.get("runtime_entry_points", ())),
        "training_dataset_count": len(policy.get("training_datasets", ())),
        "training_checkpoint_count": len(
            policy.get("training_checkpoints", ())
        ),
        "mutable_learned_parameter_count": len(
            policy.get("mutable_learned_parameters", ())
        ),
        "checked_roots": checked_roots,
        "scanned_file_count": len(scanned_files),
        "declared_dependencies": dependencies,
        "findings": findings,
    }


def build_mechanism_readiness_report(
    repository_root: Path,
    *,
    baseline_manifest: Path | None = None,
    config_path: Path | None = None,
    implementation_commit: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = repository_root.resolve()
    config = MechanismReadinessConfig.load(root, path=config_path)
    index = build_read_only_evidence_index(
        root,
        baseline_manifest=baseline_manifest,
    )
    no_training = run_no_training_audit(root, config)
    mechanisms = {
        mechanism_id: evaluate_mechanism(
            contract,
            build_mechanism_samples(index, contract),
            no_training_passed=no_training["passed"] is True,
        )
        for mechanism_id, contract in config.contracts.items()
    }
    statuses = {
        mechanism_id: value["status"]
        for mechanism_id, value in mechanisms.items()
    }
    valid_commit = (
        len(implementation_commit) == 40
        and all(character in "0123456789abcdef" for character in implementation_commit)
    )
    if not valid_commit:
        raise ContractViolation(
            "readiness-implementation-commit-invalid",
            "The report requires a lowercase 40-character implementation commit.",
            path="implementation_commit",
        )
    report: dict[str, Any] = {
        "schema": READINESS_REPORT_SCHEMA,
        "slice_id": "P2-S00-03",
        "readiness_stage": READINESS_STAGE,
        "p2_base_commit": P2_BASE_COMMIT,
        "implementation_commit": implementation_commit,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "valid": no_training["passed"] is True,
        "activation_allowed": False,
        "activation_reason": (
            "input_precheck never activates phase2_strongest_v1; "
            "implementation_validated and activation_ready remain required"
        ),
        "contract": {
            "path": config.path.relative_to(root).as_posix(),
            "sha256": config.digest,
        },
        "baseline_manifest": {
            "path": index.baseline_manifest_path,
            "manifest_digest": index.baseline_manifest_digest,
            "sha256": file_digest(root / index.baseline_manifest_path),
        },
        "evidence_index": index.to_dict(),
        "no_policy_training_audit": no_training,
        "mechanism_statuses": statuses,
        "mechanisms": mechanisms,
        "resolver_policy": {
            "unknown_status": MechanismReadinessStatus.UNAVAILABLE.value,
            "missing_report": MechanismReadinessStatus.UNAVAILABLE.value,
            "corrupt_report": MechanismReadinessStatus.UNAVAILABLE.value,
            "digest_mismatch": MechanismReadinessStatus.UNAVAILABLE.value,
            "deterministic_ready_at_input_precheck": (
                MechanismExecutionMode.VALIDATION.value
            ),
            "evidence_only": MechanismExecutionMode.DIAGNOSTIC.value,
            "unavailable": MechanismExecutionMode.BASELINE.value,
            "fallback_profile": config.fallback_profile,
        },
        "training_sample_count": 0,
        "raw_transition_count_semantics": "evidence_volume_only",
    }
    report["report_digest"] = canonical_digest(report)
    return report


def validate_readiness_report(
    report: Mapping[str, Any],
    *,
    config: MechanismReadinessConfig,
    repository_root: Path,
) -> Mapping[str, Any]:
    selected = dict(report)
    digest = selected.pop("report_digest", None)
    if not isinstance(digest, str) or canonical_digest(selected) != digest:
        raise ContractViolation(
            "readiness-report-digest-mismatch",
            "The mechanism readiness report digest is missing or invalid.",
            path="MechanismEvidenceReadinessReport.json",
        )
    if report.get("schema") != READINESS_REPORT_SCHEMA:
        raise ContractViolation(
            "readiness-report-schema-invalid",
            "Unsupported mechanism readiness report schema.",
            path="MechanismEvidenceReadinessReport.json",
        )
    if (
        report.get("p2_base_commit") != P2_BASE_COMMIT
        or report.get("readiness_stage") not in ALLOWED_STAGES
    ):
        raise ContractViolation(
            "readiness-report-binding-invalid",
            "The readiness report has an invalid base commit or stage.",
            path="MechanismEvidenceReadinessReport.json",
        )
    contract = _mapping(report.get("contract"), "readiness report contract")
    if contract.get("sha256") != config.digest:
        raise ContractViolation(
            "readiness-report-contract-mismatch",
            "The report does not match the installed readiness contract.",
            path="MechanismEvidenceReadinessReport.json",
        )
    baseline = _mapping(
        report.get("baseline_manifest"),
        "readiness report baseline manifest",
    )
    if baseline.get("manifest_digest") != BASELINE_MANIFEST_DIGEST:
        raise ContractViolation(
            "readiness-report-baseline-mismatch",
            "The report does not match the frozen evidence baseline.",
            path="MechanismEvidenceReadinessReport.json",
        )
    baseline_path_value = baseline.get("path")
    if not isinstance(baseline_path_value, str):
        raise ContractViolation(
            "readiness-report-baseline-path-invalid",
            "The report baseline path is missing.",
            path="MechanismEvidenceReadinessReport.json",
        )
    baseline_relative = Path(baseline_path_value.replace("\\", "/"))
    if baseline_relative.is_absolute() or ".." in baseline_relative.parts:
        raise ContractViolation(
            "readiness-report-baseline-path-invalid",
            "The report baseline path must remain repository-relative.",
            path="MechanismEvidenceReadinessReport.json",
        )
    baseline_target = (repository_root.resolve() / baseline_relative).resolve()
    try:
        baseline_target.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise ContractViolation(
            "readiness-report-baseline-path-invalid",
            "The report baseline path resolves outside the repository.",
            path="MechanismEvidenceReadinessReport.json",
        ) from exc
    if (
        not baseline_target.is_file()
        or baseline.get("sha256") != file_digest(baseline_target)
    ):
        raise ContractViolation(
            "readiness-report-baseline-file-mismatch",
            "The frozen baseline manifest is missing or changed.",
            path=baseline_path_value,
        )
    mechanisms = _mapping(report.get("mechanisms"), "readiness report mechanisms")
    if set(mechanisms) != set(MECHANISM_IDS):
        raise ContractViolation(
            "readiness-report-mechanism-set-invalid",
            "The report must contain exactly the four audited mechanisms.",
            path="MechanismEvidenceReadinessReport.json",
        )
    for mechanism_id in MECHANISM_IDS:
        mechanism = _mapping(mechanisms.get(mechanism_id), mechanism_id)
        parse_mechanism_status(mechanism.get("status"))
        if mechanism.get("readiness_stage") != report.get("readiness_stage"):
            raise ContractViolation(
                "readiness-report-stage-mismatch",
                f"{mechanism_id} does not match the report stage.",
                path=f"MechanismEvidenceReadinessReport.{mechanism_id}",
            )
    no_training = _mapping(
        report.get("no_policy_training_audit"),
        "readiness report no-policy-training audit",
    )
    if no_training.get("passed") is not True:
        raise ContractViolation(
            "readiness-report-training-audit-failed",
            "A report with a failed no-policy-training audit is unusable.",
            path="MechanismEvidenceReadinessReport.json",
        )
    if (
        report.get("readiness_stage") == READINESS_STAGE
        and report.get("activation_allowed") is not False
    ):
        raise ContractViolation(
            "input-precheck-activation-forbidden",
            "P2-S00-03 cannot directly activate the strongest profile.",
            path="MechanismEvidenceReadinessReport.json",
        )
    return report


class MechanismModeResolver:
    """Fail-closed per-mechanism mode resolution from a versioned report."""

    def __init__(
        self,
        repository_root: Path,
        *,
        config_path: Path | None = None,
        report_path: Path | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.config = MechanismReadinessConfig.load(
            self.repository_root,
            path=config_path,
        )
        self.report_path = (
            report_path.resolve()
            if report_path is not None
            else self.repository_root
            / "docs"
            / "reviews"
            / "phase2"
            / "MechanismEvidenceReadinessReport.json"
        )
        self.report: Mapping[str, Any] | None = None
        self.disconnect_reason = ""
        try:
            raw = json.loads(self.report_path.read_text(encoding="utf-8"))
            selected = _mapping(raw, "mechanism readiness report")
            self.report = validate_readiness_report(
                selected,
                config=self.config,
                repository_root=self.repository_root,
            )
        except (OSError, json.JSONDecodeError, ContractViolation) as exc:
            self.disconnect_reason = (
                exc.code if isinstance(exc, ContractViolation) else type(exc).__name__
            )

    def resolve(self, mechanism_id: str) -> MechanismResolution:
        if mechanism_id not in self.config.contracts:
            return self._baseline(
                mechanism_id,
                reason="unknown mechanism resolves to unavailable",
            )
        if self.report is None:
            return self._baseline(
                mechanism_id,
                reason=(
                    "readiness report disconnected: "
                    f"{self.disconnect_reason or 'missing'}"
                ),
            )
        mechanisms = _mapping(self.report.get("mechanisms"), "report mechanisms")
        mechanism = mechanisms.get(mechanism_id)
        if not isinstance(mechanism, Mapping):
            return self._baseline(
                mechanism_id,
                reason="mechanism verdict missing from report",
            )
        try:
            status = parse_mechanism_status(mechanism.get("status"))
        except ContractViolation:
            return self._baseline(
                mechanism_id,
                reason="mechanism verdict is invalid",
            )
        stage = str(self.report.get("readiness_stage", READINESS_STAGE))
        report_digest = str(self.report.get("report_digest", ""))
        if status is MechanismReadinessStatus.UNAVAILABLE:
            return self._baseline(
                mechanism_id,
                reason="readiness status is unavailable",
                report_digest=report_digest,
                stage=stage,
            )
        if status is MechanismReadinessStatus.EVIDENCE_ONLY:
            return MechanismResolution(
                mechanism_id=mechanism_id,
                status=status,
                stage=stage,
                mode=MechanismExecutionMode.DIAGNOSTIC,
                canonical_mutation_allowed=False,
                fallback_profile=self.config.fallback_profile,
                reason="evidence_only is restricted to read-only diagnostic execution",
                report_digest=report_digest,
            )
        if stage != "activation_ready":
            return MechanismResolution(
                mechanism_id=mechanism_id,
                status=status,
                stage=stage,
                mode=MechanismExecutionMode.VALIDATION,
                canonical_mutation_allowed=False,
                fallback_profile=self.config.fallback_profile,
                reason=(
                    "deterministic_ready input is validation-only until an "
                    "activation_ready revision"
                ),
                report_digest=report_digest,
            )
        return MechanismResolution(
            mechanism_id=mechanism_id,
            status=status,
            stage=stage,
            mode=MechanismExecutionMode.DEFAULT,
            canonical_mutation_allowed=True,
            fallback_profile=self.config.fallback_profile,
            reason="activation_ready deterministic mechanism may enter default",
            report_digest=report_digest,
        )

    def resolve_all(self) -> dict[str, MechanismResolution]:
        return {
            mechanism_id: self.resolve(mechanism_id)
            for mechanism_id in MECHANISM_IDS
        }

    def _baseline(
        self,
        mechanism_id: str,
        *,
        reason: str,
        report_digest: str = "",
        stage: str = READINESS_STAGE,
    ) -> MechanismResolution:
        return MechanismResolution(
            mechanism_id=mechanism_id,
            status=MechanismReadinessStatus.UNAVAILABLE,
            stage=stage,
            mode=MechanismExecutionMode.BASELINE,
            canonical_mutation_allowed=False,
            fallback_profile=self.config.fallback_profile,
            reason=reason,
            report_digest=report_digest,
        )


__all__ = [
    "ALLOWED_STAGES",
    "CAUSAL_LINKS",
    "MECHANISM_IDS",
    "READINESS_CONFIG_SCHEMA",
    "READINESS_REPORT_SCHEMA",
    "READINESS_STAGE",
    "FieldCoverage",
    "FieldObservation",
    "InputFieldContract",
    "MechanismAuditSample",
    "MechanismContract",
    "MechanismExecutionMode",
    "MechanismModeResolver",
    "MechanismReadinessConfig",
    "MechanismResolution",
    "build_mechanism_readiness_report",
    "build_mechanism_samples",
    "evaluate_mechanism",
    "run_no_training_audit",
    "validate_readiness_report",
]
