from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_orchestration.topology_policy import (
    ContractHeader,
    FrozenDict,
    NeuroSymbolicEvidenceBundle,
    PolicyDecisionDisposition,
    PolicyProjectionResult,
    StableArtifactRef,
    TopologyProposalArtifact,
    canonical_digest,
)


ADVERSARIAL_CORPUS_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "continuity_neuro_symbolic_adversarial_v1.json"
)


class NeuroSymbolicEvidenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PretrainedModelObservation:
    model_id: str
    model_version: str
    input_ref: StableArtifactRef
    output_ref: StableArtifactRef
    observation_ref: StableArtifactRef
    confidence: float

    def __post_init__(self) -> None:
        if not self.model_id or not self.model_version:
            raise ValueError("pretrained model id and version are required")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("model observation confidence must be between zero and one")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "input_ref": self.input_ref.to_dict(),
            "output_ref": self.output_ref.to_dict(),
            "observation_ref": self.observation_ref.to_dict(),
            "confidence": float(self.confidence),
            "read_only_proposal_feature": True,
            "canonical_mutation_authority": False,
        }


@dataclass(frozen=True, slots=True)
class AdversarialProposalCase:
    case_id: str
    attack_class: str
    mutation: str
    expected_constraint: str
    expected_reason_code: str
    expected_disposition: str
    unsafe_commit_allowed: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AdversarialProposalCase":
        result = cls(
            case_id=str(value.get("case_id") or ""),
            attack_class=str(value.get("attack_class") or ""),
            mutation=str(value.get("mutation") or ""),
            expected_constraint=str(value.get("expected_constraint") or ""),
            expected_reason_code=str(value.get("expected_reason_code") or ""),
            expected_disposition=str(value.get("expected_disposition") or ""),
            unsafe_commit_allowed=bool(value.get("unsafe_commit_allowed", False)),
        )
        for name in (
            "case_id",
            "attack_class",
            "mutation",
            "expected_constraint",
            "expected_reason_code",
            "expected_disposition",
        ):
            if not getattr(result, name):
                raise NeuroSymbolicEvidenceError(
                    f"adversarial corpus case is missing {name}"
                )
        if result.unsafe_commit_allowed:
            raise NeuroSymbolicEvidenceError(
                "adversarial corpus cannot allow unsafe commit"
            )
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "attack_class": self.attack_class,
            "mutation": self.mutation,
            "expected_constraint": self.expected_constraint,
            "expected_reason_code": self.expected_reason_code,
            "expected_disposition": self.expected_disposition,
            "unsafe_commit_allowed": self.unsafe_commit_allowed,
        }


@dataclass(frozen=True, slots=True)
class AdversarialProposalCorpus:
    schema: str
    corpus_id: str
    cases: tuple[AdversarialProposalCase, ...]
    digest: str

    @property
    def attack_classes(self) -> tuple[str, ...]:
        return tuple(sorted({item.attack_class for item in self.cases}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "corpus_id": self.corpus_id,
            "cases": [item.to_dict() for item in self.cases],
            "digest": self.digest,
            "training_dataset": False,
            "policy_training_input": False,
        }


def load_adversarial_proposal_corpus(
    path: str | Path = ADVERSARIAL_CORPUS_PATH,
) -> AdversarialProposalCorpus:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise NeuroSymbolicEvidenceError("adversarial corpus must be a JSON object")
    schema = str(value.get("schema") or "")
    corpus_id = str(value.get("corpus_id") or "")
    if schema != "zyra.phase2.adversarial-proposal-corpus/v1" or not corpus_id:
        raise NeuroSymbolicEvidenceError("unsupported adversarial corpus identity")
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise NeuroSymbolicEvidenceError("adversarial corpus cases are required")
    cases = tuple(
        sorted(
            (
                AdversarialProposalCase.from_mapping(item)
                for item in raw_cases
                if isinstance(item, Mapping)
            ),
            key=lambda item: item.case_id,
        )
    )
    if len(cases) != len(raw_cases) or len({item.case_id for item in cases}) != len(cases):
        raise NeuroSymbolicEvidenceError(
            "adversarial corpus contains invalid or duplicate cases"
        )
    digest = canonical_digest(
        {
            "schema": schema,
            "corpus_id": corpus_id,
            "cases": [item.to_dict() for item in cases],
        }
    )
    return AdversarialProposalCorpus(
        schema=schema,
        corpus_id=corpus_id,
        cases=cases,
        digest=digest,
    )


class NeuroSymbolicEvidenceBuilder:
    """Builds a causal evidence bundle from the real symbolic projector result."""

    def build(
        self,
        *,
        header: ContractHeader,
        proposal: TopologyProposalArtifact,
        proposal_ref: StableArtifactRef,
        projection: PolicyProjectionResult,
        attack_class: str,
        proposal_signal_mode: str,
        permission_ref: str,
        lease_ref: str,
        verification_ref: str,
        outcome_ref: str,
        artifact_refs: Iterable[StableArtifactRef] = (),
        model_observations: Iterable[PretrainedModelObservation] = (),
    ) -> NeuroSymbolicEvidenceBundle:
        observations = tuple(model_observations)
        self._validate_signal_mode(proposal_signal_mode, observations)
        for name, value in (
            ("attack_class", attack_class),
            ("permission_ref", permission_ref),
            ("lease_ref", lease_ref),
            ("verification_ref", verification_ref),
            ("outcome_ref", outcome_ref),
        ):
            if not str(value or "").strip():
                raise NeuroSymbolicEvidenceError(f"{name} is required")
        receipt = projection.receipt
        if receipt.proposal_digest != proposal.digest:
            raise NeuroSymbolicEvidenceError(
                "symbolic receipt is not bound to the proposal"
            )
        failed_constraints = tuple(
            item for item in receipt.constraint_results if not item.passed
        )
        commit_present = bool(
            projection.commit is not None
            and projection.commit.receipt.committed
        )
        unsafe_commit = commit_present and bool(failed_constraints)
        if unsafe_commit:
            raise NeuroSymbolicEvidenceError(
                "unsafe symbolic violation reached canonical commit"
            )
        proposed = {
            canonical_digest(item.to_dict()): item.to_dict()
            for item in proposal.operations
        }
        projected = {
            canonical_digest(item.to_dict()): item.to_dict()
            for item in receipt.projected_operations
        }
        removed = tuple(sorted(set(proposed) - set(projected)))
        added = tuple(sorted(set(projected) - set(proposed)))
        repaired = bool(removed or added)
        if receipt.disposition in {
            PolicyDecisionDisposition.REJECT,
            PolicyDecisionDisposition.CONFLICT,
        }:
            result = (
                "forced_baseline"
                if receipt.fallback_profile
                else "rejected"
            )
        elif repaired:
            result = "repaired"
        else:
            result = "accepted"
        model_values = [item.to_dict() for item in observations]
        refs = tuple(sorted(artifact_refs, key=lambda item: item.ref_id))
        commit_or_no_commit = {
            "result": result,
            "attack_class": attack_class,
            "mechanism_family": proposal.header.mechanism_id,
            "mechanism_version": proposal.header.mechanism_version,
            "configuration_digest": proposal.header.configuration_digest,
            "proposal_confidence": proposal.expected_outcome.get("confidence", 1.0),
            "proposal_digest": proposal.digest,
            "decision_ref": receipt.header.contract_id,
            "decision_digest": receipt.digest,
            "decision_disposition": receipt.disposition.value,
            "projected_delta_ref": receipt.delta_id,
            "projected_delta_digest": receipt.delta_digest,
            "proposal_operation_digests": sorted(proposed),
            "projected_operation_digests": sorted(projected),
            "removed_operation_digests": list(removed),
            "added_operation_digests": list(added),
            "constraint_failure_codes": [
                item.reason_code for item in failed_constraints
            ],
            "canonical_commit_present": commit_present,
            "canonical_commit": (
                projection.commit.receipt.to_dict()
                if projection.commit is not None
                else {}
            ),
            "unsafe_commit": unsafe_commit,
            "permission_ref": permission_ref,
            "lease_ref": lease_ref,
            "verification_ref": verification_ref,
            "outcome_ref": outcome_ref,
            "artifact_refs": [item.to_dict() for item in refs],
            "pretrained_model_observations": model_values,
            "model_signal_is_read_only": True,
            "symbolic_owner_controls_commit": True,
            "projector_bypass_production_reachable": False,
        }
        return NeuroSymbolicEvidenceBundle(
            header=header,
            proposal_signal_mode=proposal_signal_mode,
            proposal_ref=proposal_ref,
            model_observation_refs=tuple(
                item.observation_ref for item in observations
            ),
            constraint_results=receipt.constraint_results,
            projected_delta_ref=receipt.delta_id or "no-delta",
            commit_or_no_commit=FrozenDict(commit_or_no_commit),
            permission_ref=permission_ref,
            lease_ref=lease_ref,
            verification_ref=verification_ref,
        )

    @staticmethod
    def _validate_signal_mode(
        signal_mode: str,
        observations: tuple[PretrainedModelObservation, ...],
    ) -> None:
        if signal_mode == "deterministic_only":
            if observations:
                raise NeuroSymbolicEvidenceError(
                    "deterministic-only proposal cannot cite a model observation"
                )
            return
        if signal_mode not in {"pretrained_model_assisted", "mixed"}:
            raise NeuroSymbolicEvidenceError("invalid proposal signal mode")
        if not observations:
            raise NeuroSymbolicEvidenceError(
                "model-assisted proposal requires fixed model input/output evidence"
            )


def summarize_neuro_symbolic_bundles(
    bundles: Iterable[NeuroSymbolicEvidenceBundle],
) -> dict[str, Any]:
    values = tuple(bundles)
    unsafe = sum(
        1
        for item in values
        if bool(item.commit_or_no_commit.get("unsafe_commit"))
    )
    bypass = sum(
        1
        for item in values
        if bool(
            item.commit_or_no_commit.get(
                "projector_bypass_production_reachable"
            )
        )
    )
    return {
        "schema": "zyra.neuro-symbolic-evidence-summary/v1",
        "bundle_count": len(values),
        "unsafe_commit_count": unsafe,
        "production_projector_bypass_count": bypass,
        "signal_modes": sorted({item.proposal_signal_mode for item in values}),
        "all_constraints_attributed": all(
            bool(item.constraint_results) for item in values
        ),
        "hard_gates_passed": bool(values) and unsafe == 0 and bypass == 0,
    }


__all__ = [
    "ADVERSARIAL_CORPUS_PATH",
    "AdversarialProposalCase",
    "AdversarialProposalCorpus",
    "NeuroSymbolicEvidenceBuilder",
    "NeuroSymbolicEvidenceError",
    "PretrainedModelObservation",
    "load_adversarial_proposal_corpus",
    "summarize_neuro_symbolic_bundles",
]
