from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType

from ...graph_custody import BranchGraphDelta, GraphStateSnapshot
from ..contracts import (
    PolicyInputSnapshot,
    TopologyProposalArtifact,
    canonical_digest,
)
from ..evidence import PolicyEvidencePublisher, PublishedPolicyEvidence
from .catalog import ARGRoleCatalog
from .encoder import (
    ARGEncodedInput,
    ARGInputEncoder,
    ARGModelObservation,
)
from .joint_builder import (
    ARGJointBuilder,
    ARGJointBuilderConfig,
    ARGJointHypothesis,
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


BaselineExecutor = Callable[
    [PolicyInputSnapshot, GraphStateSnapshot],
    Mapping[str, Any],
]
EventSink = Callable[[EventRecord], None]


@dataclass(frozen=True, slots=True)
class ARGReadinessResolution:
    stage: str
    status: str
    mode: str
    report_digest: str
    canonical_mutation_allowed: bool
    fallback_profile: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_id": "arg_designer",
            "stage": self.stage,
            "status": self.status,
            "mode": self.mode,
            "report_digest": self.report_digest,
            "canonical_mutation_allowed": self.canonical_mutation_allowed,
            "fallback_profile": self.fallback_profile,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ARGTopologyRuntimeResult:
    mode: str
    readiness: ARGReadinessResolution
    proposal: TopologyProposalArtifact | None
    encoded_input: ARGEncodedInput | None
    hypothesis: ARGJointHypothesis | None
    alternatives: tuple[ARGJointHypothesis, ...]
    baseline_decision: Mapping[str, Any]
    published_evidence: PublishedPolicyEvidence | None
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
            "proposal": self.proposal.to_dict() if self.proposal is not None else None,
            "encoded_input_digest": (
                self.encoded_input.digest if self.encoded_input is not None else ""
            ),
            "hypothesis": (
                self.hypothesis.to_dict() if self.hypothesis is not None else None
            ),
            "alternatives": [item.to_dict() for item in self.alternatives],
            "baseline_decision": dict(self.baseline_decision),
            "published_artifact_id": (
                self.published_evidence.artifact.artifact_id
                if self.published_evidence is not None
                else ""
            ),
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


class ARGTopologyRuntime:
    """Readiness-bound ARG proposal runtime; it never commits canonical graph state."""

    def __init__(
        self,
        *,
        config: ARGJointBuilderConfig | None,
        readiness: ARGReadinessResolution,
        baseline_executor: BaselineExecutor | None = None,
        evidence_publisher: PolicyEvidencePublisher | None = None,
        admit_event: EventSink | None = None,
        disconnected_reason: str = "",
    ) -> None:
        self.config = config
        self.readiness = readiness
        self.baseline_executor = baseline_executor or self._default_baseline
        self.evidence_publisher = evidence_publisher
        self.admit_event = admit_event or (lambda event: None)
        self.disconnected_reason = disconnected_reason
        self.encoder = ARGInputEncoder()
        self.builder = ARGJointBuilder(config) if config is not None else None

    @classmethod
    def from_repository(
        cls,
        repository_root: Path,
        *,
        config_path: Path | None = None,
        report_path: Path | None = None,
        baseline_executor: BaselineExecutor | None = None,
        evidence_publisher: PolicyEvidencePublisher | None = None,
        admit_event: EventSink | None = None,
    ) -> ARGTopologyRuntime:
        root = repository_root.resolve()
        selected_config = (
            config_path.resolve()
            if config_path is not None
            else root / "config" / "phase2" / "arg-joint-topology.json"
        )
        try:
            config = ARGJointBuilderConfig.load(selected_config)
        except Exception as exc:  # noqa: BLE001 - becomes explicit baseline evidence.
            reason = getattr(exc, "code", f"arg_config:{type(exc).__name__}")
            return cls(
                config=None,
                readiness=cls._unavailable(
                    fallback_profile="phase1_deterministic_baseline",
                    reason=str(reason),
                ),
                baseline_executor=baseline_executor,
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
            / "P2-S03-01"
            / "MechanismEvidenceReadinessReport.json"
        )
        try:
            readiness = cls._load_readiness(
                root,
                config=config,
                report_path=selected_report,
            )
            disconnected = ""
        except Exception as exc:  # noqa: BLE001 - unknown/corrupt must fail closed.
            disconnected = getattr(
                exc,
                "code",
                f"arg_readiness:{type(exc).__name__}",
            )
            readiness = cls._unavailable(
                fallback_profile=config.fallback_profile,
                reason=str(disconnected),
            )
        return cls(
            config=config,
            readiness=readiness,
            baseline_executor=baseline_executor,
            evidence_publisher=evidence_publisher,
            admit_event=admit_event,
            disconnected_reason=str(disconnected),
        )

    def execute(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        task_summary: str,
        current_graph: GraphStateSnapshot,
        role_catalog: ARGRoleCatalog,
        branch_delta: BranchGraphDelta | None = None,
        recent_recovery_outcome: Mapping[str, Any] | None = None,
        model_observation: ARGModelObservation | None = None,
        enabled: bool = True,
        publish: bool = True,
    ) -> ARGTopologyRuntimeResult:
        before_revision = current_graph.revision
        events: list[EventRecord] = []
        if not enabled:
            return self._baseline_result(
                policy_input=policy_input,
                current_graph=current_graph,
                reason="arg_disabled",
                events=events,
            )
        if (
            self.config is None
            or self.builder is None
            or self.readiness.mode == "baseline"
        ):
            return self._baseline_result(
                policy_input=policy_input,
                current_graph=current_graph,
                reason=(
                    self.disconnected_reason
                    or self.readiness.reason
                    or "arg_unavailable"
                ),
                events=events,
            )
        try:
            encoded = self.encoder.encode(
                policy_input=policy_input,
                task_summary=task_summary,
                current_graph=current_graph,
                role_catalog=role_catalog,
                readiness_stage=self.readiness.stage,
                readiness_status=self.readiness.status,
                readiness_report_digest=self.readiness.report_digest,
                mechanism_version=self.config.mechanism_version,
                configuration_digest=self.config.digest,
                phase_affinity=thaw_mapping(
                    self.config.phase_capability_affinity
                ),
                branch_delta=branch_delta,
                recent_recovery_outcome=recent_recovery_outcome,
                model_observation=model_observation,
            )
            built = self.builder.build(encoded)
            self.validate_proposal(
                policy_input=policy_input,
                role_catalog=role_catalog,
                proposal=built.proposal,
            )
        except Exception as exc:  # noqa: BLE001 - explicit degraded baseline.
            reason = getattr(exc, "code", f"arg_exception:{type(exc).__name__}")
            return self._baseline_result(
                policy_input=policy_input,
                current_graph=current_graph,
                reason=str(reason),
                events=events,
            )

        published = None
        if publish and self.evidence_publisher is not None:
            published = self.evidence_publisher.publish(
                built.proposal,
                run_id=policy_input.run_id,
                task_id=policy_input.task_id,
            )
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_arg_" + canonical_digest(
                (
                    built.proposal.digest,
                    self.readiness.mode,
                    before_revision,
                )
            )[:24],
            event_type=EventType.TOPOLOGY_ROUTE,
            payload={
                "schema": "zyra.arg-runtime-result/v1",
                "mechanism_id": "arg_designer",
                "mode": self.readiness.mode,
                "readiness_stage": self.readiness.stage,
                "readiness_status": self.readiness.status,
                "readiness_report_digest": self.readiness.report_digest,
                "proposal_id": built.proposal.proposal_id,
                "proposal_digest": built.proposal.digest,
                "hypothesis_id": built.hypothesis.hypothesis_id,
                "role_count": len(built.hypothesis.role_steps),
                "operation_count": len(built.proposal.operations),
                "end_reason": built.hypothesis.end_reason,
                "canonical_mutation_attempted": False,
                "canonical_graph_revision_before": before_revision,
                "canonical_graph_revision_after": current_graph.revision,
                "fallback_profile": self.config.fallback_profile,
            },
        )
        self.admit_event(event)
        events.append(event)
        return ARGTopologyRuntimeResult(
            mode=self.readiness.mode,
            readiness=self.readiness,
            proposal=built.proposal,
            encoded_input=encoded,
            hypothesis=built.hypothesis,
            alternatives=built.alternatives,
            baseline_decision={},
            published_evidence=published,
            graph_revision_before=before_revision,
            graph_revision_after=current_graph.revision,
            degraded=False,
            degraded_reason="",
            events=tuple(events),
        )

    @staticmethod
    def validate_proposal(
        *,
        policy_input: PolicyInputSnapshot,
        role_catalog: ARGRoleCatalog,
        proposal: TopologyProposalArtifact,
    ) -> None:
        if proposal.header.mechanism_id != "arg_designer":
            raise ValueError("ARG runtime rejects a proposal from another mechanism")
        if proposal.input_snapshot_digest != policy_input.digest:
            raise ValueError(
                "ARG proposal belongs to an old input or requirement revision"
            )
        expected = proposal.expected_outcome
        if (
            expected.get("requirement_revision")
            != policy_input.requirement_revision
            or expected.get("role_catalog_version") != role_catalog.catalog_version
            or expected.get("role_catalog_digest") != role_catalog.digest
        ):
            raise ValueError(
                "ARG proposal requirement revision or capability catalog is stale"
            )

    def _baseline_result(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
        reason: str,
        events: list[EventRecord],
    ) -> ARGTopologyRuntimeResult:
        baseline = dict(self.baseline_executor(policy_input, current_graph))
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_arg_degraded_" + canonical_digest(
                (
                    policy_input.digest,
                    current_graph.signature,
                    reason,
                    baseline,
                )
            )[:24],
            event_type=EventType.RECOVERY_PLANNED,
            payload={
                "schema": "zyra.arg-runtime-degraded/v1",
                "mechanism_id": "arg_designer",
                "reason": reason,
                "fallback_profile": self.readiness.fallback_profile,
                "readiness_stage": self.readiness.stage,
                "readiness_status": self.readiness.status,
                "readiness_report_digest": self.readiness.report_digest,
                "canonical_mutation_attempted": False,
                "graph_revision": current_graph.revision,
                "baseline_decision_digest": canonical_digest(baseline),
            },
        )
        self.admit_event(event)
        events.append(event)
        return ARGTopologyRuntimeResult(
            mode="baseline",
            readiness=self.readiness,
            proposal=None,
            encoded_input=None,
            hypothesis=None,
            alternatives=(),
            baseline_decision=baseline,
            published_evidence=None,
            graph_revision_before=current_graph.revision,
            graph_revision_after=current_graph.revision,
            degraded=True,
            degraded_reason=reason,
            events=tuple(events),
        )

    @staticmethod
    def _default_baseline(
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
    ) -> Mapping[str, Any]:
        return {
            "profile_id": "phase1_deterministic_baseline",
            "policy_input_digest": policy_input.digest,
            "graph_id": current_graph.graph_id,
            "graph_revision": current_graph.revision,
            "node_ids": [item.node_id for item in current_graph.nodes],
            "edge_ids": [item.edge_id for item in current_graph.edges],
            "phase_conditioned_joint_arg": False,
        }

    @classmethod
    def _load_readiness(
        cls,
        repository_root: Path,
        *,
        config: ARGJointBuilderConfig,
        report_path: Path,
    ) -> ARGReadinessResolution:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("arg_readiness_report_missing_or_corrupt") from exc
        if not isinstance(report, Mapping):
            raise ValueError("arg_readiness_report_invalid")
        supplied_digest = str(report.get("report_digest") or "")
        unsigned = dict(report)
        unsigned.pop("report_digest", None)
        if (
            len(supplied_digest) != 64
            or canonical_digest(unsigned) != supplied_digest
        ):
            raise ValueError("arg_readiness_report_digest_mismatch")
        if report.get("schema") != READINESS_REPORT_SCHEMA:
            raise ValueError("arg_readiness_report_schema_unknown")
        stage = str(report.get("readiness_stage") or "")
        if stage not in ALLOWED_STAGES:
            raise ValueError("arg_readiness_stage_unknown")

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
            raise ValueError("arg_readiness_frozen_binding_mismatch")
        no_training = report.get("no_policy_training_audit")
        if not isinstance(no_training, Mapping) or no_training.get("passed") is not True:
            raise ValueError("arg_readiness_no_training_audit_failed")
        mechanisms = report.get("mechanisms")
        if not isinstance(mechanisms, Mapping):
            raise ValueError("arg_readiness_mechanisms_missing")
        arg = mechanisms.get("arg_designer")
        if not isinstance(arg, Mapping):
            raise ValueError("arg_readiness_verdict_missing")
        status = str(arg.get("status") or "")
        if status not in ALLOWED_STATUSES:
            raise ValueError("arg_readiness_status_unknown")
        if (
            arg.get("readiness_stage") != stage
            or (
                report.get("mechanism_statuses") or {}
            ).get("arg_designer")
            != status
        ):
            raise ValueError("arg_readiness_stage_or_status_drift")
        if stage == "input_precheck":
            if supplied_digest != config.input_precheck_report_digest:
                raise ValueError("arg_input_precheck_digest_mismatch")
        else:
            preflight = report.get("preflight")
            activation_evidence_ready = (
                stage == "activation_ready"
                and report.get("valid") is True
                and report.get("sealed_run_admission_candidate") is True
                and isinstance(preflight, Mapping)
                and bool(preflight.get("preflight_id"))
                and bool(preflight.get("receipt_set_digest"))
            )
            if stage == "activation_ready" and not activation_evidence_ready:
                raise ValueError("arg_activation_evidence_missing")
            if stage != "activation_ready" and (
                report.get("supersedes_report_digest")
                != config.input_precheck_report_digest
            ):
                raise ValueError("arg_readiness_lineage_mismatch")
            validation = arg.get("implementation_validation")
            if status == "deterministic_ready" and (
                not isinstance(validation, Mapping)
                or validation.get("passed") is not True
                or validation.get("mechanism_version") != config.mechanism_version
                or validation.get("configuration_digest") != config.digest
                or validation.get("catalog_schema_version")
                != config.catalog_schema_version
                or validation.get("canonical_mutation_attempted") is not False
                or validation.get("training_sample_count") != 0
            ):
                raise ValueError("arg_implementation_validation_incomplete")
        if status == "unavailable":
            return cls._unavailable(
                fallback_profile=config.fallback_profile,
                reason="readiness status is unavailable",
                stage=stage,
                report_digest=supplied_digest,
            )
        if status == "evidence_only":
            return ARGReadinessResolution(
                stage=stage,
                status=status,
                mode="diagnostic",
                report_digest=supplied_digest,
                canonical_mutation_allowed=False,
                fallback_profile=config.fallback_profile,
                reason="evidence_only is restricted to read-only ARG proposal evidence",
            )
        if stage == "activation_ready":
            return ARGReadinessResolution(
                stage=stage,
                status=status,
                mode="default",
                report_digest=supplied_digest,
                canonical_mutation_allowed=True,
                fallback_profile=config.fallback_profile,
                reason="activation_ready ARG proposal may be sent to the symbolic composer",
            )
        return ARGReadinessResolution(
            stage=stage,
            status=status,
            mode="validation",
            report_digest=supplied_digest,
            canonical_mutation_allowed=False,
            fallback_profile=config.fallback_profile,
            reason=(
                "deterministic_ready ARG runs in isolated validation until "
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
    ) -> ARGReadinessResolution:
        return ARGReadinessResolution(
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


def thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): (
            list(item)
            if isinstance(item, tuple)
            else item
        )
        for key, item in value.items()
    }
