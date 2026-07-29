from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from zyra_evaluation.policy_benchmark.neuro_symbolic import (
    NeuroSymbolicEvidenceBuilder,
    load_adversarial_proposal_corpus,
    summarize_neuro_symbolic_bundles,
)
from zyra_orchestration.topology_policy import (
    FrozenDict,
    PolicyDecisionDisposition,
    StableArtifactRef,
    TopologyConstraintProjector,
    TopologyOperation,
    TopologyOperationKind,
)

from tests.integration.test_neuro_symbolic_projector import (
    NOW,
    _custody,
    _environment,
    _header,
    _input,
    _operation,
    _proposal,
)


def _edge(edge_id: str, source: str, target: str) -> TopologyOperation:
    return TopologyOperation(
        kind=TopologyOperationKind.ADD_EDGE,
        entity_id=edge_id,
        value=FrozenDict(
            {
                "source_node_id": source,
                "target_node_id": target,
                "relation": "message",
                "required_capabilities": [],
            }
        ),
        required_permissions=("graph.write",),
        communication_bytes=8,
        reason="adversarial edge",
    )


def _execute_case(case, root: Path):
    custody = _custody(root / "graph.sqlite3")
    policy_input = _input(custody)
    evaluated_at = NOW
    operation = _operation()
    expected = FrozenDict(
        {"tokens": 10, "cost_usd": 0.01, "time_ms": 10}
    )

    if case.mutation == "expires_before_evaluation":
        evaluated_at = "2026-07-30T10:00:00Z"
    elif case.mutation == "downgrade_readiness_stage":
        readiness = replace(
            policy_input.readiness_refs[0],
            readiness_stage="input_precheck",
        )
        policy_input = replace(policy_input, readiness_refs=(readiness,))
    elif case.mutation == "unknown_capability":
        operation = replace(
            operation,
            value=FrozenDict(
                {
                    "role": "worker",
                    "capabilities": ["unknown-capability"],
                    "dependencies": [],
                }
            ),
        )
    elif case.mutation == "request_disallowed_permission":
        operation = replace(
            operation,
            required_permissions=("graph.admin",),
        )
    elif case.mutation == "request_forbidden_placement":
        operation = replace(operation, requested_placement="cloud")
    elif case.mutation == "request_excess_capacity":
        operation = replace(operation, required_capacity=3)
    elif case.mutation == "mark_lease_unavailable":
        policy_input = _input(
            custody,
            environment=_environment(lease_available=False),
        )
    elif case.mutation == "expire_telemetry_observation":
        policy_input = _input(
            custody,
            environment=_environment(
                fresh_until="2026-07-30T08:01:00Z",
            ),
        )
        evaluated_at = "2026-07-30T08:02:00Z"
    elif case.mutation == "exceed_token_budget":
        expected = FrozenDict(
            {"tokens": 1001, "cost_usd": 0.01, "time_ms": 10}
        )
    elif case.mutation == "declare_pending_side_effect":
        expected = FrozenDict(
            {
                "tokens": 10,
                "cost_usd": 0.01,
                "time_ms": 10,
                "pending_side_effects": ["unsettled-tool-call"],
            }
        )
    elif case.mutation == "exceed_topology_churn":
        policy_input = replace(
            policy_input,
            budget=replace(policy_input.budget, max_topology_churn=0),
        )
    elif case.mutation == "violate_minimum_dwell":
        policy_input = replace(
            policy_input,
            budget=replace(
                policy_input.budget,
                minimum_dwell_seconds=3600,
            ),
            last_topology_change_at="2026-07-30T07:59:00Z",
        )

    proposal = _proposal(
        policy_input,
        proposal_id=f"proposal-{case.case_id}",
        operation=operation,
        expected_outcome=expected,
    )

    if case.mutation == "create_dependency_cycle":
        proposal = replace(
            proposal,
            operations=(
                replace(
                    _operation("cycle-a"),
                    value=FrozenDict(
                        {
                            "role": "worker",
                            "capabilities": ["execute"],
                            "dependencies": ["cycle-b"],
                        }
                    ),
                ),
                replace(
                    _operation("cycle-b"),
                    value=FrozenDict(
                        {
                            "role": "worker",
                            "capabilities": ["execute"],
                            "dependencies": ["cycle-a"],
                        }
                    ),
                ),
            ),
        )
    elif case.mutation == "add_edge_with_missing_target":
        proposal = replace(
            proposal,
            operations=(_edge("edge-missing", "missing-a", "missing-b"),),
        )
    elif case.mutation == "exceed_fanout_budget":
        policy_input = replace(
            policy_input,
            budget=replace(
                policy_input.budget,
                max_topology_churn=20,
                max_fan_out=4,
            ),
        )
        nodes = tuple(
            _operation(node_id)
            for node_id in ("fanout-source", "fanout-a", "fanout-b", "fanout-c", "fanout-d", "fanout-e")
        )
        edges = tuple(
            _edge(f"edge-fanout-{index}", "fanout-source", target)
            for index, target in enumerate(
                ("fanout-a", "fanout-b", "fanout-c", "fanout-d", "fanout-e"),
                1,
            )
        )
        proposal = _proposal(
            policy_input,
            proposal_id=f"proposal-{case.case_id}",
        )
        proposal = replace(proposal, operations=nodes + edges)
    elif case.mutation == "advance_canonical_graph_head":
        seed = _proposal(
            policy_input,
            proposal_id="proposal-stale-seed",
            operation=_operation("stale-seed"),
        )
        seeded = TopologyConstraintProjector(custody).execute(
            policy_input,
            seed,
            decision_id="decision-stale-seed",
            evaluated_at=NOW,
        )
        assert seeded.receipt.accepted
    elif case.mutation == "reuse_key_with_different_content":
        shared_key = "corpus-idempotency-key"
        first = _proposal(
            policy_input,
            proposal_id="proposal-idempotency-first",
            operation=_operation("idempotency-first"),
        )
        first = replace(
            first,
            header=_header(
                "proposal-idempotency-first",
                idempotency_key=shared_key,
            ),
        )
        seeded = TopologyConstraintProjector(custody).execute(
            policy_input,
            first,
            decision_id="decision-idempotency-first",
            evaluated_at=NOW,
        )
        assert seeded.receipt.accepted
        policy_input = _input(custody)
        proposal = _proposal(
            policy_input,
            proposal_id="proposal-idempotency-second",
            operation=TopologyOperation(
                kind=TopologyOperationKind.SET_GRAPH_METADATA,
                entity_id="different-content",
                value=FrozenDict({"value": True}),
                required_permissions=("graph.write",),
                communication_bytes=1,
            ),
        )
        proposal = replace(
            proposal,
            header=_header(
                "proposal-idempotency-second",
                idempotency_key=shared_key,
            ),
        )

    result = TopologyConstraintProjector(custody).execute(
        policy_input,
        proposal,
        decision_id=f"decision-{case.case_id}",
        evaluated_at=evaluated_at,
    )
    return proposal, result


def test_adversarial_corpus_covers_every_symbolic_hard_constraint(
    tmp_path: Path,
) -> None:
    corpus = load_adversarial_proposal_corpus()
    required_attack_classes = {
        "budget",
        "capacity",
        "churn",
        "cycle",
        "expired_proposal",
        "fanout",
        "idempotency",
        "lease",
        "minimum_dwell",
        "missing_capability",
        "missing_endpoint",
        "pending_side_effect",
        "permission",
        "privacy",
        "readiness",
        "stale_environment",
        "stale_revision",
    }
    assert set(corpus.attack_classes) == required_attack_classes
    assert len(corpus.digest) == 64

    bundles = []
    matrix = []
    for case in corpus.cases:
        proposal, projection = _execute_case(case, tmp_path / case.case_id)
        reasons = {
            item.reason_code for item in projection.receipt.constraint_results
        }
        constraints = {
            item.constraint_id for item in projection.receipt.constraint_results
        }
        assert case.expected_reason_code in reasons, case.case_id
        assert case.expected_constraint in constraints, case.case_id
        assert (
            projection.receipt.disposition.value
            == case.expected_disposition
        ), case.case_id
        assert projection.commit is None, case.case_id
        proposal_ref = StableArtifactRef(
            ref_id=f"artifact-{case.case_id}",
            uri=f"contract://proposal/{proposal.proposal_id}",
            digest=proposal.digest,
        )
        bundle = NeuroSymbolicEvidenceBuilder().build(
            header=_header(
                f"bundle-{case.case_id}",
                mechanism="NeuroSymbolicEvidenceBuilder",
                causation_id=projection.receipt.header.contract_id,
            ),
            proposal=proposal,
            proposal_ref=proposal_ref,
            projection=projection,
            attack_class=case.attack_class,
            proposal_signal_mode="deterministic_only",
            permission_ref=f"permission-preflight:{case.case_id}",
            lease_ref=f"lease-not-issued:{case.case_id}",
            verification_ref=f"verification:no-commit:{case.case_id}",
            outcome_ref=f"outcome:no-commit:{case.case_id}",
        )
        assert bundle.commit_or_no_commit["unsafe_commit"] is False
        bundles.append(bundle)
        matrix.append(
            {
                "case_id": case.case_id,
                "reason_codes": sorted(reasons),
                "unsafe_commit": False,
            }
        )

    summary = summarize_neuro_symbolic_bundles(bundles)
    assert summary["bundle_count"] == len(corpus.cases)
    assert summary["unsafe_commit_count"] == 0
    assert summary["production_projector_bypass_count"] == 0
    assert summary["hard_gates_passed"] is True
    assert len(matrix) == len(corpus.cases)
