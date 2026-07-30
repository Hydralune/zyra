from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType

from ...graph_custody import GraphStateSnapshot
from ..contracts import (
    PolicyInputSnapshot,
    TopologyProposalArtifact,
    canonical_digest,
)
from ..evidence import PolicyEvidencePublisher, PublishedPolicyEvidence
from .environment_encoder import (
    CARDEncodedEnvironment,
    CARDEnvironmentEncoder,
    CARDReplacementCandidate,
)
from .hysteresis import CARDEdgeHysteresisState
from .residual_corrector import (
    CARDResidualCorrection,
    CARDResidualCorrector,
    CARDResidualCorrectorConfig,
)


READINESS_REPORT_SCHEMA = "zyra.mechanism-evidence-readiness-report/v1"
ALLOWED_STAGES = {
    "input_precheck",
    "implementation_validated",
    "activation_ready",
}
ALLOWED_STATUSES = {
    "deterministic_ready",
    "evidence_only",
    "unavailable",
}


EventSink = Callable[[EventRecord], None]


@dataclass(frozen=True, slots=True)
class CARDReadinessResolution:
    stage: str
    status: str
    mode: str
    report_digest: str
    canonical_mutation_allowed: bool
    fallback_profile: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_id": "card",
            "stage": self.stage,
            "status": self.status,
            "mode": self.mode,
            "report_digest": self.report_digest,
            "canonical_mutation_allowed": self.canonical_mutation_allowed,
            "fallback_profile": self.fallback_profile,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CARDTopologyRuntimeResult:
    mode: str
    readiness: CARDReadinessResolution
    arg_base_proposal_id: str
    arg_base_proposal_digest: str
    encoded_environment: CARDEncodedEnvironment | None
    correction: CARDResidualCorrection | None
    correction_proposal: TopologyProposalArtifact | None
    published_evidence: PublishedPolicyEvidence | None
    effective_spatial_edge_ids: tuple[str, ...]
    effective_temporal_edge_ids: tuple[str, ...]
    composer_residual_eligible: bool
    commit_input_proposal_digest: str
    graph_revision_before: int
    graph_revision_after: int
    degraded: bool
    degraded_reason: str
    events: tuple[EventRecord, ...]

    @property
    def canonical_graph_unchanged(self) -> bool:
        return self.graph_revision_before == self.graph_revision_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "readiness": self.readiness.to_dict(),
            "arg_base_proposal_id": self.arg_base_proposal_id,
            "arg_base_proposal_digest": self.arg_base_proposal_digest,
            "encoded_environment_digest": (
                self.encoded_environment.digest
                if self.encoded_environment is not None
                else ""
            ),
            "correction": (
                self.correction.to_dict()
                if self.correction is not None
                else None
            ),
            "correction_proposal": (
                self.correction_proposal.to_dict()
                if self.correction_proposal is not None
                else None
            ),
            "published_artifact_id": (
                self.published_evidence.artifact.artifact_id
                if self.published_evidence is not None
                else ""
            ),
            "effective_spatial_edge_ids": list(
                self.effective_spatial_edge_ids
            ),
            "effective_temporal_edge_ids": list(
                self.effective_temporal_edge_ids
            ),
            "composer_residual_eligible": self.composer_residual_eligible,
            "commit_input_proposal_digest": self.commit_input_proposal_digest,
            "graph_revision_before": self.graph_revision_before,
            "graph_revision_after": self.graph_revision_after,
            "canonical_graph_unchanged": self.canonical_graph_unchanged,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "events": [
                {
                    "run_id": item.run_id,
                    "task_id": item.task_id,
                    "event_id": item.event_id,
                    "event_type": item.event_type.value,
                    "node_id": item.node_id,
                    "created_at": item.created_at,
                    "payload": dict(item.payload),
                }
                for item in self.events
            ],
        }


class CARDTopologyRuntime:
    """Readiness-bound CARD residual runtime; it cannot commit graph state."""

    def __init__(
        self,
        *,
        config: CARDResidualCorrectorConfig | None,
        readiness: CARDReadinessResolution,
        evidence_publisher: PolicyEvidencePublisher | None = None,
        admit_event: EventSink | None = None,
        disconnected_reason: str = "",
    ) -> None:
        self.config = config
        self.readiness = readiness
        self.evidence_publisher = evidence_publisher
        self.admit_event = admit_event or (lambda event: None)
        self.disconnected_reason = disconnected_reason
        self.encoder = CARDEnvironmentEncoder()
        self.corrector = CARDResidualCorrector(config) if config is not None else None

    @classmethod
    def from_repository(
        cls,
        repository_root: Path,
        *,
        config_path: Path | None = None,
        report_path: Path | None = None,
        evidence_publisher: PolicyEvidencePublisher | None = None,
        admit_event: EventSink | None = None,
    ) -> CARDTopologyRuntime:
        root = repository_root.resolve()
        selected_config = (
            config_path.resolve()
            if config_path is not None
            else root
            / "config"
            / "phase2"
            / "card-directional-residual.json"
        )
        try:
            config = CARDResidualCorrectorConfig.load(selected_config)
        except Exception as exc:  # noqa: BLE001 - fail closed to ARG base.
            reason = getattr(exc, "code", f"card_config:{type(exc).__name__}")
            return cls(
                config=None,
                readiness=cls._unavailable(
                    fallback_profile="unmodified_arg_base",
                    reason=str(reason),
                ),
                evidence_publisher=evidence_publisher,
                admit_event=admit_event,
                disconnected_reason=str(reason),
            )
        selected_report = (
            report_path.resolve()
            if report_path is not None
            else root
            / "docs"
            / "reviews"
            / "evidence"
            / "P2-S03-02"
            / "MechanismEvidenceReadinessReport.json"
        )
        try:
            readiness = cls._load_readiness(
                root,
                config=config,
                report_path=selected_report,
            )
            disconnected = ""
        except Exception as exc:  # noqa: BLE001 - corrupt/unknown is unavailable.
            disconnected = getattr(
                exc,
                "code",
                f"card_readiness:{type(exc).__name__}",
            )
            readiness = cls._unavailable(
                fallback_profile=config.fallback_profile,
                reason=str(disconnected),
            )
        return cls(
            config=config,
            readiness=readiness,
            evidence_publisher=evidence_publisher,
            admit_event=admit_event,
            disconnected_reason=str(disconnected),
        )

    def execute(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        arg_base: TopologyProposalArtifact,
        current_graph: GraphStateSnapshot,
        replacement_candidates: Iterable[CARDReplacementCandidate] = (),
        hysteresis_state: Mapping[str, CARDEdgeHysteresisState] | None = None,
        enabled: bool = True,
        publish: bool = True,
    ) -> CARDTopologyRuntimeResult:
        events: list[EventRecord] = []
        if not enabled:
            return self._baseline_result(
                policy_input=policy_input,
                arg_base=arg_base,
                current_graph=current_graph,
                reason="card_disabled",
                events=events,
            )
        if (
            self.config is None
            or self.corrector is None
            or self.readiness.mode == "baseline"
        ):
            return self._baseline_result(
                policy_input=policy_input,
                arg_base=arg_base,
                current_graph=current_graph,
                reason=(
                    self.disconnected_reason
                    or self.readiness.reason
                    or "card_unavailable"
                ),
                events=events,
            )
        try:
            self._validate_graph(
                policy_input=policy_input,
                current_graph=current_graph,
            )
            self._validate_input_readiness(policy_input)
            encoded = self.encoder.encode(
                policy_input=policy_input,
                arg_base=arg_base,
                mechanism_version=self.config.mechanism_version,
                configuration_digest=self.config.digest,
                required_categories=self.config.required_categories,
                optional_categories=self.config.optional_categories,
                required_observation_fields=(
                    self.config.required_observation_fields
                ),
                minimum_confidence=self.config.minimum_confidence,
                stale_confidence_multiplier=(
                    self.config.stale_confidence_multiplier
                ),
                replacement_candidates=replacement_candidates,
            )
            correction = self.corrector.correct(
                encoded,
                hysteresis_state=hysteresis_state,
            )
            proposal = self.corrector.build_proposal(
                policy_input=policy_input,
                arg_base=arg_base,
                encoded=encoded,
                correction=correction,
                readiness_stage=self.readiness.stage,
                readiness_status=self.readiness.status,
                readiness_report_digest=self.readiness.report_digest,
            )
            if proposal is not None:
                self.validate_proposal(
                    policy_input=policy_input,
                    arg_base=arg_base,
                    proposal=proposal,
                )
        except Exception as exc:  # noqa: BLE001 - explicit unmodified ARG base.
            reason = getattr(exc, "code", f"card_exception:{type(exc).__name__}")
            return self._baseline_result(
                policy_input=policy_input,
                arg_base=arg_base,
                current_graph=current_graph,
                reason=str(reason),
                events=events,
            )

        published = None
        if (
            publish
            and proposal is not None
            and self.evidence_publisher is not None
        ):
            published = self.evidence_publisher.publish(
                proposal,
                run_id=policy_input.run_id,
                task_id=policy_input.task_id,
            )
        action_counts = {
            action: sum(
                item.action == action for item in correction.decisions
            )
            for action in ("add", "drop", "reweight", "hold", "reject")
        }
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_card_" + canonical_digest(
                (
                    arg_base.digest,
                    correction.digest,
                    self.readiness.mode,
                    current_graph.revision,
                )
            )[:24],
            event_type=EventType.RESOURCE_DECISION,
            payload={
                "schema": "zyra.card-runtime-result/v1",
                "mechanism_id": "card",
                "mode": self.readiness.mode,
                "readiness_stage": self.readiness.stage,
                "readiness_status": self.readiness.status,
                "readiness_report_digest": self.readiness.report_digest,
                "arg_base_proposal_id": arg_base.proposal_id,
                "arg_base_proposal_digest": arg_base.digest,
                "correction_proposal_id": (
                    proposal.proposal_id if proposal is not None else ""
                ),
                "correction_proposal_digest": (
                    proposal.digest if proposal is not None else ""
                ),
                "environment_snapshot_digest": (
                    encoded.environment_snapshot_digest
                ),
                "correction_digest": correction.digest,
                "action_counts": action_counts,
                "spatial_edge_count": len(
                    correction.effective_spatial_edge_ids
                ),
                "temporal_edge_count": len(
                    correction.effective_temporal_edge_ids
                ),
                "composer_residual_eligible": (
                    self.readiness.mode in {"validation", "default"}
                ),
                "canonical_mutation_attempted": False,
                "canonical_graph_revision_before": current_graph.revision,
                "canonical_graph_revision_after": current_graph.revision,
                "fallback_profile": self.config.fallback_profile,
            },
        )
        self.admit_event(event)
        events.append(event)

        base_spatial, base_temporal = self._arg_base_edge_ids(arg_base)
        residual_eligible = self.readiness.mode in {"validation", "default"}
        return CARDTopologyRuntimeResult(
            mode=self.readiness.mode,
            readiness=self.readiness,
            arg_base_proposal_id=arg_base.proposal_id,
            arg_base_proposal_digest=arg_base.digest,
            encoded_environment=encoded,
            correction=correction,
            correction_proposal=proposal,
            published_evidence=published,
            effective_spatial_edge_ids=(
                correction.effective_spatial_edge_ids
                if residual_eligible
                else base_spatial
            ),
            effective_temporal_edge_ids=(
                correction.effective_temporal_edge_ids
                if residual_eligible
                else base_temporal
            ),
            composer_residual_eligible=residual_eligible,
            commit_input_proposal_digest=arg_base.digest,
            graph_revision_before=current_graph.revision,
            graph_revision_after=current_graph.revision,
            degraded=False,
            degraded_reason="",
            events=tuple(events),
        )

    @staticmethod
    def validate_proposal(
        *,
        policy_input: PolicyInputSnapshot,
        arg_base: TopologyProposalArtifact,
        proposal: TopologyProposalArtifact,
    ) -> None:
        if proposal.header.mechanism_id != "card":
            raise ValueError("CARD runtime rejects a proposal from another mechanism")
        if proposal.input_snapshot_digest != policy_input.digest:
            raise ValueError("CARD correction belongs to another policy input")
        if proposal.header.causation_id != arg_base.proposal_id:
            raise ValueError("CARD correction is not caused by the ARG base")
        if (
            proposal.expected_outcome.get("arg_base_proposal_digest")
            != arg_base.digest
        ):
            raise ValueError("CARD correction ARG base digest is stale")

    def _validate_input_readiness(
        self,
        policy_input: PolicyInputSnapshot,
    ) -> None:
        matches = tuple(
            item
            for item in policy_input.readiness_refs
            if item.header.mechanism_id == "card"
        )
        if len(matches) != 1:
            raise ValueError("card_input_readiness_ref_missing")
        selected = matches[0]
        if (
            selected.report_digest != self.readiness.report_digest
            or selected.readiness_stage != self.readiness.stage
            or selected.status != self.readiness.status
        ):
            raise ValueError("card_input_readiness_ref_drift")

    @staticmethod
    def _validate_graph(
        *,
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
    ) -> None:
        if (
            policy_input.graph.graph_id != current_graph.graph_id
            or policy_input.graph.revision != current_graph.revision
            or policy_input.graph.signature != current_graph.signature
        ):
            raise ValueError("card_current_graph_binding_drift")

    def _baseline_result(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        arg_base: TopologyProposalArtifact,
        current_graph: GraphStateSnapshot,
        reason: str,
        events: list[EventRecord],
    ) -> CARDTopologyRuntimeResult:
        base_spatial, base_temporal = self._arg_base_edge_ids(arg_base)
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_card_degraded_" + canonical_digest(
                (
                    policy_input.digest,
                    arg_base.digest,
                    current_graph.signature,
                    reason,
                )
            )[:24],
            event_type=EventType.RECOVERY_PLANNED,
            payload={
                "schema": "zyra.card-runtime-degraded/v1",
                "mechanism_id": "card",
                "reason": reason,
                "fallback_profile": self.readiness.fallback_profile,
                "readiness_stage": self.readiness.stage,
                "readiness_status": self.readiness.status,
                "readiness_report_digest": self.readiness.report_digest,
                "arg_base_proposal_id": arg_base.proposal_id,
                "arg_base_proposal_digest": arg_base.digest,
                "canonical_mutation_attempted": False,
                "graph_revision": current_graph.revision,
            },
        )
        self.admit_event(event)
        events.append(event)
        return CARDTopologyRuntimeResult(
            mode="baseline",
            readiness=self.readiness,
            arg_base_proposal_id=arg_base.proposal_id,
            arg_base_proposal_digest=arg_base.digest,
            encoded_environment=None,
            correction=None,
            correction_proposal=None,
            published_evidence=None,
            effective_spatial_edge_ids=base_spatial,
            effective_temporal_edge_ids=base_temporal,
            composer_residual_eligible=False,
            commit_input_proposal_digest=arg_base.digest,
            graph_revision_before=current_graph.revision,
            graph_revision_after=current_graph.revision,
            degraded=True,
            degraded_reason=reason,
            events=tuple(events),
        )

    @staticmethod
    def _arg_base_edge_ids(
        arg_base: TopologyProposalArtifact,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        hypothesis = arg_base.expected_outcome.get("joint_hypothesis")
        if not isinstance(hypothesis, Mapping):
            return (), ()
        spatial: set[str] = set()
        temporal: set[str] = set()
        for step in hypothesis.get("steps") or ():
            if not isinstance(step, Mapping):
                continue
            for edge in step.get("incident_edges") or ():
                if not isinstance(edge, Mapping) or edge.get("persisted") is not True:
                    continue
                edge_id = str(edge.get("edge_id") or "")
                relation = str(edge.get("relation") or "")
                edge_type = str(edge.get("edge_type") or "").lower()
                if edge_type == "temporal" or relation.lower().startswith("temporal"):
                    temporal.add(edge_id)
                else:
                    spatial.add(edge_id)
        return tuple(sorted(spatial)), tuple(sorted(temporal))

    @classmethod
    def _load_readiness(
        cls,
        repository_root: Path,
        *,
        config: CARDResidualCorrectorConfig,
        report_path: Path,
    ) -> CARDReadinessResolution:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("card_readiness_report_missing_or_corrupt") from exc
        if not isinstance(report, Mapping):
            raise ValueError("card_readiness_report_invalid")
        supplied_digest = str(report.get("report_digest") or "")
        unsigned = dict(report)
        unsigned.pop("report_digest", None)
        if (
            len(supplied_digest) != 64
            or canonical_digest(unsigned) != supplied_digest
        ):
            raise ValueError("card_readiness_report_digest_mismatch")
        if report.get("schema") != READINESS_REPORT_SCHEMA:
            raise ValueError("card_readiness_report_schema_unknown")
        stage = str(report.get("readiness_stage") or "")
        if stage not in ALLOWED_STAGES:
            raise ValueError("card_readiness_stage_unknown")

        readiness_config_path = (
            repository_root / "config" / "phase2" / "mechanism-readiness.json"
        )
        readiness_config = json.loads(
            readiness_config_path.read_text(encoding="utf-8")
        )
        if (
            report.get("p2_base_commit")
            != readiness_config.get("p2_base_commit")
            or (
                report.get("baseline_manifest") or {}
            ).get("manifest_digest")
            != readiness_config.get("baseline_manifest_digest")
            or (report.get("contract") or {}).get("sha256")
            != _file_digest(readiness_config_path)
        ):
            raise ValueError("card_readiness_frozen_binding_mismatch")
        no_training = report.get("no_policy_training_audit")
        if not isinstance(no_training, Mapping) or no_training.get("passed") is not True:
            raise ValueError("card_readiness_no_training_audit_failed")
        mechanisms = report.get("mechanisms")
        if not isinstance(mechanisms, Mapping):
            raise ValueError("card_readiness_mechanisms_missing")
        card = mechanisms.get("card")
        if not isinstance(card, Mapping):
            raise ValueError("card_readiness_verdict_missing")
        status = str(card.get("status") or "")
        if status not in ALLOWED_STATUSES:
            raise ValueError("card_readiness_status_unknown")
        if (
            card.get("readiness_stage") != stage
            or (
                report.get("mechanism_statuses") or {}
            ).get("card")
            != status
        ):
            raise ValueError("card_readiness_stage_or_status_drift")
        if stage == "input_precheck":
            if supplied_digest != config.input_precheck_report_digest:
                raise ValueError("card_input_precheck_digest_mismatch")
        else:
            if (
                report.get("supersedes_report_digest")
                != config.input_precheck_report_digest
            ):
                raise ValueError("card_readiness_lineage_mismatch")
            validation = card.get("implementation_validation")
            if status == "deterministic_ready" and (
                not isinstance(validation, Mapping)
                or validation.get("passed") is not True
                or validation.get("mechanism_version")
                != config.mechanism_version
                or validation.get("configuration_digest") != config.digest
                or validation.get("environment_schema_version")
                != config.environment_schema_version
                or validation.get("encoded_schema_version")
                != config.encoded_schema_version
                or validation.get("residual_schema_version")
                != config.residual_schema_version
                or validation.get("canonical_mutation_attempted") is not False
                or validation.get("training_sample_count") != 0
            ):
                raise ValueError("card_implementation_validation_incomplete")
        if status == "unavailable":
            return cls._unavailable(
                fallback_profile=config.fallback_profile,
                reason="readiness status is unavailable",
                stage=stage,
                report_digest=supplied_digest,
            )
        if status == "evidence_only":
            return CARDReadinessResolution(
                stage=stage,
                status=status,
                mode="diagnostic",
                report_digest=supplied_digest,
                canonical_mutation_allowed=False,
                fallback_profile=config.fallback_profile,
                reason=(
                    "evidence_only exposes a correction diff but keeps ARG as "
                    "the effective composer input"
                ),
            )
        if stage == "activation_ready":
            return CARDReadinessResolution(
                stage=stage,
                status=status,
                mode="default",
                report_digest=supplied_digest,
                canonical_mutation_allowed=True,
                fallback_profile=config.fallback_profile,
                reason=(
                    "activation_ready CARD residual may be sent with ARG to "
                    "the symbolic composer"
                ),
            )
        return CARDReadinessResolution(
            stage=stage,
            status=status,
            mode="validation",
            report_digest=supplied_digest,
            canonical_mutation_allowed=False,
            fallback_profile=config.fallback_profile,
            reason=(
                "deterministic_ready CARD runs in isolated validation until "
                "activation_ready"
            ),
        )

    @staticmethod
    def _unavailable(
        *,
        fallback_profile: str,
        reason: str,
        stage: str = "input_precheck",
        report_digest: str = "",
    ) -> CARDReadinessResolution:
        return CARDReadinessResolution(
            stage=stage,
            status="unavailable",
            mode="baseline",
            report_digest=report_digest,
            canonical_mutation_allowed=False,
            fallback_profile=fallback_profile,
            reason=reason,
        )


def _file_digest(path: Path) -> str:
    payload = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()
