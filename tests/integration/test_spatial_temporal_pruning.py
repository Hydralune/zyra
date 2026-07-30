from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from zyra_core import EventRecord, EventType
from zyra_orchestration.graph_custody import GraphStateCustody, GraphStateStore
from zyra_orchestration.topology_policy import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    PolicyBudget,
    PolicyEvidencePublisher,
    PolicyInputSnapshot,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
)
from zyra_orchestration.topology_policy.condition import (
    CARDEdgeHysteresisState,
    CARDHysteresisController,
    CARDReadinessResolution,
    CARDResidualCorrection,
    CARDResidualDecision,
    CARDScoreComponents,
    CARDTopologyRuntimeResult,
)
from zyra_orchestration.topology_policy.pruning import (
    AgentPruneOptimizerConfig,
    AgentPruneRuntime,
    CommunicationBudget,
    CommunicationEdgeType,
    CommunicationOutcomeObservation,
    CommunicationVerification,
    DeliveryAttemptReceipt,
    ProtectedEdgeConstraint,
    RuntimeCommunicationEnvelope,
)
from zyra_runtime import LocalArtifactStore


ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-07-30T14:00:00Z"
COMPLETED = "2026-07-30T13:00:00Z"
INPUT_PRECHECK_DIGEST = (
    "82ad9a84a55506223fce2133168b7df2f0ae487aa7288fe5ede5c58869c1bbde"
)


def _header(contract_id: str, mechanism: str) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-agentprune-integration-source",
        correlation_id="correlation-agentprune-integration",
        causation_id="continuity-agentprune-integration",
        mechanism_id=mechanism,
        mechanism_version="v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
    )


def _custody(tmp_path: Path) -> GraphStateCustody:
    store = GraphStateStore(tmp_path / "graph.sqlite3")
    store.initialize()
    custody = GraphStateCustody(store)
    custody.create(
        graph_id_value="graph-agentprune-integration",
        run_id="run-agentprune-integration",
    )
    return custody


def _policy_input(
    *,
    graph,
    report_digest: str,
    readiness_status: str,
) -> PolicyInputSnapshot:
    return PolicyInputSnapshot(
        header=_header(
            "policy-input-agentprune-integration",
            "PolicyInputSnapshotBuilder",
        ),
        run_id=graph.run_id,
        task_id="task-agentprune-integration",
        phase="execution",
        requirement_revision="requirement-r1",
        graph=GraphSnapshotRef(
            graph_id=graph.graph_id,
            run_id=graph.run_id,
            revision=graph.revision,
            signature=graph.signature,
            commit_id=graph.commit_id,
        ),
        nodes=(),
        registered_roles=("executor", "verifier", "observer", "recovery"),
        registered_capabilities=(
            "execution",
            "verification",
            "observation",
            "recovery",
        ),
        unresolved_obligations=("verify artifact", "restore continuity"),
        registry_versions=FrozenDict({"worker_registry_revision": 6}),
        environment=EnvironmentSnapshot(
            header=_header(
                "environment-agentprune-integration",
                "ResourceScheduler",
            ),
            observed_at=NOW,
            observations=(),
        ),
        memory_refs=(),
        readiness_refs=(
            MechanismEvidenceReadinessReportRef(
                header=_header(
                    "readiness-agentprune-integration",
                    "agentprune",
                ),
                report_ref="artifact://agentprune-implementation-readiness",
                report_digest=report_digest,
                readiness_stage="implementation_validated",
                status=readiness_status,
            ),
        ),
        budget=PolicyBudget(
            remaining_tokens=10_000,
            remaining_cost_usd=10,
            remaining_time_ms=120_000,
            max_communication_bytes=512,
            max_fan_out=4,
            max_topology_churn=20,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=("graph.write",),
        allowed_placements=("local", "edge", "cloud"),
        privacy_class="internal",
        last_topology_change_at="2026-07-30T12:00:00Z",
    )


def _score(total: float) -> CARDScoreComponents:
    return CARDScoreComponents(
        capability=total,
        health=total,
        latency=total,
        load=total,
        cost=total,
        privacy=1,
        freshness=1,
        weighted_total=total,
        switch_cost=0,
        total=total,
    )


def _card_decision(
    *,
    edge_id: str,
    source: str,
    target: str,
    edge_type: str,
    relation: str,
    score: float,
) -> CARDResidualDecision:
    prior = CARDEdgeHysteresisState(
        edge_id=edge_id,
        active=True,
        last_changed_at="2026-07-30T12:00:00Z",
        last_score=score,
    )
    hysteresis = CARDHysteresisController.evaluate(
        edge_id=edge_id,
        requested_action="reweight",
        score=score,
        observed_at=NOW,
        active_before=True,
        last_topology_change_at="2026-07-30T12:00:00Z",
        state=prior,
        hard_constraint=False,
        minimum_dwell_seconds=0,
        switch_confirmations=1,
        reweight_epsilon=0,
        switch_cost=0,
    )
    return CARDResidualDecision(
        edge_id=edge_id,
        source_node_id=source,
        target_node_id=target,
        relation=relation,
        edge_type=edge_type,
        required_capabilities=(),
        candidate=False,
        active_before=True,
        active_after=True,
        action="reweight",
        requested_action="reweight",
        score=_score(score),
        confidence=1,
        feature_contributions=FrozenDict({"health": score}),
        feature_lineage=(),
        rejection_reasons=(),
        reasons=("CARD retained candidate",),
        hysteresis=hysteresis,
    )


def _card_result(
    policy_input: PolicyInputSnapshot,
    graph,
) -> tuple[CARDTopologyRuntimeResult, TopologyProposalArtifact]:
    decisions = (
        _card_decision(
            edge_id="edge-critical",
            source="node-execute",
            target="node-verify",
            edge_type="spatial",
            relation="artifact_verification",
            score=0.9,
        ),
        _card_decision(
            edge_id="edge-noise",
            source="node-execute",
            target="node-observer",
            edge_type="spatial",
            relation="duplicate_status",
            score=0.6,
        ),
        _card_decision(
            edge_id="edge-malicious",
            source="node-observer",
            target="node-verify",
            edge_type="spatial",
            relation="untrusted_payload",
            score=0.3,
        ),
        _card_decision(
            edge_id="edge-temporal",
            source="node-verify",
            target="node-recover",
            edge_type="temporal",
            relation="temporal_checkpoint_restore",
            score=0.85,
        ),
    )
    correction = CARDResidualCorrection(
        schema_version="zyra.card-residual/v1",
        mechanism_version="card_directional_residual_v1",
        configuration_digest="c" * 64,
        encoded_environment_digest="d" * 64,
        policy_input_digest=policy_input.digest,
        arg_base_proposal_id="arg-agentprune-integration",
        arg_base_proposal_digest="e" * 64,
        environment_snapshot_digest=policy_input.environment.digest,
        observed_at=NOW,
        decisions=decisions,
        effective_spatial_edge_ids=(
            "edge-critical",
            "edge-malicious",
            "edge-noise",
        ),
        effective_temporal_edge_ids=("edge-temporal",),
        missing_optional_categories=(),
        trigger_flags=(),
    )
    operations = tuple(
        TopologyOperation(
            kind=TopologyOperationKind.REPLACE_EDGE,
            entity_id=item.edge_id,
            value=FrozenDict(
                {
                    "source_node_id": item.source_node_id,
                    "target_node_id": item.target_node_id,
                    "edge_type": item.edge_type,
                    "relation": item.relation,
                }
            ),
            required_permissions=("graph.write",),
            communication_bytes=96,
            reason="CARD retained candidate",
        )
        for item in decisions
    )
    proposal = TopologyProposalArtifact(
        header=_header("card-agentprune-integration", "card"),
        proposal_id="card-agentprune-integration",
        input_snapshot_digest=policy_input.digest,
        base_graph=policy_input.graph,
        operations=operations,
        expected_outcome=FrozenDict(
            {
                "mechanism": "CARD deterministic directional residual",
                "correction_digest": correction.digest,
                "effective_spatial_edge_ids": list(
                    correction.effective_spatial_edge_ids
                ),
                "effective_temporal_edge_ids": list(
                    correction.effective_temporal_edge_ids
                ),
            }
        ),
        alternatives=(),
        reasons=("CARD residual candidate for AgentPrune",),
        constraint_assumptions=("GraphStateCustody owns commits",),
        expires_at="2026-07-30T14:05:00Z",
        fallback_profile="unmodified_arg_base",
    )
    return (
        CARDTopologyRuntimeResult(
            mode="validation",
            readiness=CARDReadinessResolution(
                stage="implementation_validated",
                status="deterministic_ready",
                mode="validation",
                report_digest="f" * 64,
                canonical_mutation_allowed=False,
                fallback_profile="unmodified_arg_base",
                reason="CARD integration fixture is validation-ready",
            ),
            arg_base_proposal_id="arg-agentprune-integration",
            arg_base_proposal_digest="e" * 64,
            encoded_environment=None,
            correction=correction,
            correction_proposal=proposal,
            published_evidence=None,
            effective_spatial_edge_ids=correction.effective_spatial_edge_ids,
            effective_temporal_edge_ids=correction.effective_temporal_edge_ids,
            composer_residual_eligible=True,
            commit_input_proposal_digest=proposal.digest,
            graph_revision_before=graph.revision,
            graph_revision_after=graph.revision,
            degraded=False,
            degraded_reason="",
            events=(),
        ),
        proposal,
    )


def _outcome(
    *,
    edge_id: str,
    source: str,
    target: str,
    edge_type: CommunicationEdgeType,
    suffix: str,
    payload: str,
    round_index: int,
    evidence: bool = False,
    artifact: bool = False,
    verifier: str = "not_run",
    redundant_with: str = "",
    malicious: bool = False,
) -> CommunicationOutcomeObservation:
    message_id = f"prior-{edge_id}-{suffix}"
    return CommunicationOutcomeObservation(
        observation_id=f"observation-{message_id}",
        run_id="run-agentprune-integration",
        task_id="task-agentprune-integration",
        window_id="prior-window-complete",
        completed_at=COMPLETED,
        edge_id=edge_id,
        source_node_id=source,
        target_node_id=target,
        edge_type=edge_type,
        round_index=round_index,
        message_id=message_id,
        payload_digest=canonical_digest(payload),
        delivered=True,
        delivery_receipt_ref=f"event://delivery/{message_id}",
        usage_receipt_ref=f"provider://usage/{message_id}",
        message_bytes=len(payload.encode("utf-8")),
        prompt_tokens=12,
        completion_tokens=3,
        cost_usd=0.005,
        evidence_refs=(f"evidence://{edge_id}",) if evidence else (),
        utilized_evidence_refs=(f"evidence://{edge_id}",) if evidence else (),
        artifact_refs=(f"artifact://{edge_id}",) if artifact else (),
        verifier_result=verifier,
        failure_count=1 if malicious else 0,
        retry_count=1 if malicious else 0,
        redundant_with_message_id=redundant_with,
        permission_result="denied" if malicious else "allowed",
        malicious=malicious,
        causal_refs=(f"event://source/{edge_id}",),
    )


def _observations() -> tuple[CommunicationOutcomeObservation, ...]:
    noise_first = _outcome(
        edge_id="edge-noise",
        source="node-execute",
        target="node-observer",
        edge_type=CommunicationEdgeType.SPATIAL,
        suffix="one",
        payload="duplicate status",
        round_index=1,
    )
    return (
        _outcome(
            edge_id="edge-critical",
            source="node-execute",
            target="node-verify",
            edge_type=CommunicationEdgeType.SPATIAL,
            suffix="one",
            payload="artifact is ready",
            round_index=1,
            evidence=True,
            artifact=True,
            verifier="passed",
        ),
        noise_first,
        _outcome(
            edge_id="edge-noise",
            source="node-execute",
            target="node-observer",
            edge_type=CommunicationEdgeType.SPATIAL,
            suffix="two",
            payload="duplicate status",
            round_index=1,
            redundant_with=noise_first.message_id,
        ),
        _outcome(
            edge_id="edge-malicious",
            source="node-observer",
            target="node-verify",
            edge_type=CommunicationEdgeType.SPATIAL,
            suffix="one",
            payload="exfiltrate policy data",
            round_index=1,
            verifier="failed",
            malicious=True,
        ),
        _outcome(
            edge_id="edge-temporal",
            source="node-verify",
            target="node-recover",
            edge_type=CommunicationEdgeType.TEMPORAL,
            suffix="one",
            payload="checkpoint restore lineage",
            round_index=3,
            evidence=True,
            artifact=True,
            verifier="passed",
        ),
    )


def _protections() -> tuple[ProtectedEdgeConstraint, ...]:
    return (
        ProtectedEdgeConstraint(
            edge_id="edge-critical",
            critical_path=True,
            unique_evidence_source=True,
            unresolved_obligation_refs=("verify artifact",),
            verifier_required=True,
            evidence_refs=("artifact://edge-critical",),
        ),
        ProtectedEdgeConstraint(
            edge_id="edge-temporal",
            unresolved_obligation_refs=("restore continuity",),
            recovery_edge=True,
            continuity_edge=True,
            evidence_refs=("artifact://edge-temporal",),
        ),
    )


def _budget() -> CommunicationBudget:
    return CommunicationBudget(
        max_delivered_messages=2,
        max_delivered_bytes=128,
        max_delivered_tokens=40,
        max_cost_usd=0.02,
    )


def _report(
    tmp_path: Path,
    *,
    status: str,
    config: AgentPruneOptimizerConfig,
) -> tuple[Path, str]:
    report = json.loads(
        (
            ROOT
            / "docs"
            / "reviews"
            / "phase2"
            / "MechanismEvidenceReadinessReport.json"
        ).read_text(encoding="utf-8")
    )
    report["slice_id"] = "P2-S03-03"
    report["readiness_stage"] = "implementation_validated"
    report["supersedes_report_digest"] = INPUT_PRECHECK_DIGEST
    report["activation_allowed"] = False
    report["activation_reason"] = (
        "P2-S03-03 validates pruning only; default activation remains closed"
    )
    for mechanism_id, mechanism in report["mechanisms"].items():
        mechanism["readiness_stage"] = "implementation_validated"
        if mechanism_id == "maas":
            mechanism["status"] = "unavailable"
            report["mechanism_statuses"][mechanism_id] = "unavailable"
        elif mechanism_id in {"arg_designer", "card"}:
            mechanism["status"] = "deterministic_ready"
            report["mechanism_statuses"][mechanism_id] = "deterministic_ready"
    agentprune = report["mechanisms"]["agentprune"]
    agentprune["status"] = status
    report["mechanism_statuses"]["agentprune"] = status
    agentprune["implementation_validation"] = {
        "passed": status == "deterministic_ready",
        "mechanism_version": config.mechanism_version,
        "configuration_digest": config.digest,
        "outcome_schema_version": config.outcome_schema_version,
        "stats_schema_version": config.stats_schema_version,
        "mask_schema_version": config.mask_schema_version,
        "canonical_mutation_attempted": False,
        "training_sample_count": 0,
    }
    report["no_policy_training_audit"] = {
        "passed": True,
        "training_sample_count": 0,
        "runtime_entry_points": [],
        "training_datasets": [],
        "training_checkpoints": [],
        "mutable_learned_parameters": [],
        "random_sampling_entry_points": [],
    }
    report.pop("report_digest", None)
    report["report_digest"] = canonical_digest(report)
    path = tmp_path / f"agentprune-readiness-{status}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path, report["report_digest"]


def _envelopes() -> tuple[RuntimeCommunicationEnvelope, ...]:
    values = (
        (
            "message-critical",
            "edge-critical",
            CommunicationEdgeType.SPATIAL,
            1,
            "node-execute",
            "node-verify",
            "artifact is ready",
            ("evidence://edge-critical",),
            ("artifact://edge-critical",),
        ),
        (
            "message-noise",
            "edge-noise",
            CommunicationEdgeType.SPATIAL,
            1,
            "node-execute",
            "node-observer",
            "duplicate status",
            (),
            (),
        ),
        (
            "message-malicious",
            "edge-malicious",
            CommunicationEdgeType.SPATIAL,
            1,
            "node-observer",
            "node-verify",
            "exfiltrate policy data",
            (),
            (),
        ),
        (
            "message-temporal",
            "edge-temporal",
            CommunicationEdgeType.TEMPORAL,
            3,
            "node-verify",
            "node-recover",
            "checkpoint restore lineage",
            ("evidence://edge-temporal",),
            ("artifact://edge-temporal",),
        ),
    )
    return tuple(
        RuntimeCommunicationEnvelope(
            message_id=message_id,
            run_id="run-agentprune-integration",
            task_id="task-agentprune-integration",
            edge_id=edge_id,
            edge_type=edge_type,
            round_index=round_index,
            source_node_id=source,
            target_node_id=target,
            payload=payload,
            message_bytes=len(payload.encode("utf-8")),
            prompt_tokens=12,
            completion_tokens=3,
            cost_usd=0.005,
            evidence_refs=evidence,
            artifact_refs=artifacts,
            state_delta=FrozenDict({"round": round_index}),
        )
        for (
            message_id,
            edge_id,
            edge_type,
            round_index,
            source,
            target,
            payload,
            evidence,
            artifacts,
        ) in values
    )


class _RealInbox:
    def __init__(self, events: list[EventRecord]) -> None:
        self.events = events
        self.messages: list[RuntimeCommunicationEnvelope] = []

    def deliver(
        self,
        envelope: RuntimeCommunicationEnvelope,
    ) -> DeliveryAttemptReceipt:
        self.messages.append(envelope)
        event = EventRecord(
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            event_id=f"event-delivery-{envelope.message_id}",
            event_type=EventType.AGENT_MESSAGE,
            node_id=envelope.target_node_id,
            payload={
                "message_id": envelope.message_id,
                "edge_id": envelope.edge_id,
                "edge_type": envelope.edge_type.value,
                "round_index": envelope.round_index,
                "source_node_id": envelope.source_node_id,
                "target_node_id": envelope.target_node_id,
                "content": envelope.payload,
                "evidence_refs": list(envelope.evidence_refs),
                "artifact_refs": list(envelope.artifact_refs),
                "state_delta": dict(envelope.state_delta),
                "actual_bytes": envelope.message_bytes,
                "actual_tokens": envelope.total_tokens,
                "actual_cost_usd": envelope.cost_usd,
            },
        )
        self.events.append(event)
        return DeliveryAttemptReceipt(
            message_id=envelope.message_id,
            delivery_receipt_ref=f"event://{event.event_id}",
            delivered=True,
            actual_bytes=envelope.message_bytes,
            actual_prompt_tokens=envelope.prompt_tokens,
            actual_completion_tokens=envelope.completion_tokens,
            actual_cost_usd=envelope.cost_usd,
            artifact_refs=envelope.artifact_refs,
            evidence_used_refs=envelope.evidence_refs,
        )

    def verify(
        self,
        _: tuple[DeliveryAttemptReceipt, ...],
    ) -> CommunicationVerification:
        edge_ids = {item.edge_id for item in self.messages}
        passed = {"edge-critical", "edge-temporal"}.issubset(edge_ids)
        return CommunicationVerification(
            passed=passed,
            verifier_ref=f"artifact://verifier/{len(self.messages)}",
            artifact_refs=(
                ("artifact://final-verified-output",) if passed else ()
            ),
            unresolved_obligations=(
                () if passed else ("verify artifact", "restore continuity")
            ),
            reason=(
                "critical artifact and temporal continuity both arrived"
                if passed
                else "critical communication missing"
            ),
        )


def test_validation_mask_changes_real_delivery_without_verifier_regression(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-agentprune-integration")
    config = AgentPruneOptimizerConfig.load(
        ROOT / "config" / "phase2" / "agentprune-pruning.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="deterministic_ready",
        config=config,
    )
    policy_input = _policy_input(
        graph=graph,
        report_digest=report_digest,
        readiness_status="deterministic_ready",
    )
    card_result, upstream = _card_result(policy_input, graph)
    events: list[EventRecord] = []
    publisher = PolicyEvidencePublisher(
        LocalArtifactStore(tmp_path / "artifacts"),
        admit_event=events.append,
    )
    runtime = AgentPruneRuntime.from_repository(
        ROOT,
        report_path=report_path,
        evidence_publisher=publisher,
        admit_event=events.append,
    )
    enabled = runtime.execute(
        policy_input=policy_input,
        card_result=card_result,
        upstream_proposal=upstream,
        current_graph=graph,
        observations=_observations(),
        protections=_protections(),
        budget=_budget(),
        mechanism_epoch="epoch-validation-1",
    )
    enabled_inbox = _RealInbox(events)
    enabled_delivery = runtime.deliver(
        runtime_result=enabled,
        envelopes=_envelopes(),
        deliver_message=enabled_inbox.deliver,
        verify_delivery=enabled_inbox.verify,
    )

    disabled = runtime.execute(
        policy_input=policy_input,
        card_result=card_result,
        upstream_proposal=upstream,
        current_graph=graph,
        observations=_observations(),
        protections=_protections(),
        budget=_budget(),
        mechanism_epoch="epoch-disabled-1",
        enabled=False,
        publish=False,
    )
    disabled_inbox = _RealInbox(events)
    disabled_delivery = runtime.deliver(
        runtime_result=disabled,
        envelopes=_envelopes(),
        deliver_message=disabled_inbox.deliver,
        verify_delivery=disabled_inbox.verify,
    )

    assert enabled.mode == "validation"
    assert enabled.mask is not None
    assert set(enabled.mask.dropped_edge_ids) == {
        "edge-noise",
        "edge-malicious",
    }
    assert enabled.effective_spatial_edge_ids == ("edge-critical",)
    assert enabled.effective_temporal_edge_ids == ("edge-temporal",)
    assert enabled.pruning_proposal is not None
    assert enabled.published_evidence is not None
    assert (
        publisher.replay(enabled.published_evidence.artifact).digest
        == enabled.pruning_proposal.digest
    )
    assert enabled.canonical_graph_unchanged is True
    assert custody.current(graph.graph_id).revision == 0

    assert enabled_delivery.verification.passed is True
    assert disabled_delivery.verification.passed is True
    assert enabled_delivery.efficiency_gain_valid is True
    assert enabled_delivery.delivered_messages == 2
    assert disabled_delivery.delivered_messages == 4
    assert (
        enabled_delivery.delivered_bytes
        < disabled_delivery.delivered_bytes
    )
    assert (
        enabled_delivery.delivered_tokens
        < disabled_delivery.delivered_tokens
    )
    assert (
        enabled_delivery.delivered_cost_usd
        < disabled_delivery.delivered_cost_usd
    )
    assert {item.edge_id for item in enabled_inbox.messages} == {
        "edge-critical",
        "edge-temporal",
    }
    assert sum(
        item.event_type is EventType.AGENT_MESSAGE for item in events
    ) == 6
    assert any(
        item.payload.get("efficiency_gain_valid") is True for item in events
    )


def test_evidence_only_records_would_prune_but_delivers_every_message(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-agentprune-integration")
    config = AgentPruneOptimizerConfig.load(
        ROOT / "config" / "phase2" / "agentprune-pruning.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="evidence_only",
        config=config,
    )
    policy_input = _policy_input(
        graph=graph,
        report_digest=report_digest,
        readiness_status="evidence_only",
    )
    card_result, upstream = _card_result(policy_input, graph)
    events: list[EventRecord] = []
    runtime = AgentPruneRuntime.from_repository(
        ROOT,
        report_path=report_path,
        admit_event=events.append,
    )
    diagnostic = runtime.execute(
        policy_input=policy_input,
        card_result=card_result,
        upstream_proposal=upstream,
        current_graph=graph,
        observations=_observations(),
        protections=_protections(),
        budget=_budget(),
        mechanism_epoch="epoch-diagnostic-1",
        publish=False,
    )
    inbox = _RealInbox(events)
    delivery = runtime.deliver(
        runtime_result=diagnostic,
        envelopes=_envelopes(),
        deliver_message=inbox.deliver,
        verify_delivery=inbox.verify,
    )

    assert diagnostic.mode == "diagnostic"
    assert diagnostic.mask is not None
    assert set(diagnostic.mask.dropped_edge_ids) == {
        "edge-noise",
        "edge-malicious",
    }
    assert diagnostic.pruning_effective is False
    assert diagnostic.composer_pruning_eligible is False
    assert diagnostic.effective_spatial_edge_ids == (
        "edge-critical",
        "edge-malicious",
        "edge-noise",
    )
    assert delivery.delivered_messages == 4
    assert delivery.dropped_messages == 0
    assert delivery.verification.passed is True
    assert delivery.efficiency_gain_valid is False
    assert custody.current(graph.graph_id).revision == 0


def test_failed_verifier_never_counts_lower_communication_as_efficiency(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-agentprune-integration")
    config = AgentPruneOptimizerConfig.load(
        ROOT / "config" / "phase2" / "agentprune-pruning.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="deterministic_ready",
        config=config,
    )
    policy_input = _policy_input(
        graph=graph,
        report_digest=report_digest,
        readiness_status="deterministic_ready",
    )
    card_result, upstream = _card_result(policy_input, graph)
    runtime = AgentPruneRuntime.from_repository(
        ROOT,
        report_path=report_path,
    )
    result = runtime.execute(
        policy_input=policy_input,
        card_result=card_result,
        upstream_proposal=upstream,
        current_graph=graph,
        observations=_observations(),
        protections=_protections(),
        budget=_budget(),
        mechanism_epoch="epoch-verifier-failure",
        publish=False,
    )
    inbox = _RealInbox([])

    def fail_verifier(
        _: tuple[DeliveryAttemptReceipt, ...],
    ) -> CommunicationVerification:
        return CommunicationVerification(
            passed=False,
            verifier_ref="artifact://verifier/failed",
            unresolved_obligations=("verify artifact",),
            reason="forced verifier failure",
        )

    delivery = runtime.deliver(
        runtime_result=result,
        envelopes=_envelopes(),
        deliver_message=inbox.deliver,
        verify_delivery=fail_verifier,
    )
    assert delivery.delivered_messages == 2
    assert delivery.verification.passed is False
    assert delivery.efficiency_gain_valid is False


def test_fault_recovery_requires_a_new_epoch_before_mask_recalculation(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-agentprune-integration")
    config = AgentPruneOptimizerConfig.load(
        ROOT / "config" / "phase2" / "agentprune-pruning.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="deterministic_ready",
        config=config,
    )
    policy_input = _policy_input(
        graph=graph,
        report_digest=report_digest,
        readiness_status="deterministic_ready",
    )
    card_result, upstream = _card_result(policy_input, graph)
    runtime = AgentPruneRuntime.from_repository(
        ROOT,
        report_path=report_path,
    )
    first = runtime.execute(
        policy_input=policy_input,
        card_result=card_result,
        upstream_proposal=upstream,
        current_graph=graph,
        observations=_observations(),
        protections=_protections(),
        budget=_budget(),
        mechanism_epoch="epoch-before-fault",
        publish=False,
    )
    changed_budget = replace(_budget(), max_delivered_bytes=120)
    stale_epoch = runtime.execute(
        policy_input=policy_input,
        card_result=card_result,
        upstream_proposal=upstream,
        current_graph=graph,
        observations=_observations(),
        protections=_protections(),
        budget=changed_budget,
        mechanism_epoch="epoch-before-fault",
        publish=False,
    )
    recovered = runtime.execute(
        policy_input=policy_input,
        card_result=card_result,
        upstream_proposal=upstream,
        current_graph=graph,
        observations=_observations(),
        protections=_protections(),
        budget=changed_budget,
        mechanism_epoch="epoch-after-fault-recovery",
        publish=False,
    )

    assert first.mode == "validation"
    assert first.mask is not None
    assert stale_epoch.mode == "baseline"
    assert stale_epoch.degraded_reason == "agentprune_epoch_mask_drift"
    assert recovered.mode == "validation"
    assert recovered.mask is not None
    assert recovered.mask.mechanism_epoch == "epoch-after-fault-recovery"
    assert recovered.mask.digest != first.mask.digest
