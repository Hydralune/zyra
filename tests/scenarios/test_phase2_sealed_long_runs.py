from __future__ import annotations

import json
from pathlib import Path

import pytest

from zyra_evaluation.policy_benchmark.long_run_validator import (
    EVIDENCE_INDEX_SCHEMA,
    IndependentTransitionValidator,
    SealedLongRunValidator,
    canonical_digest,
    file_digest,
)
from zyra_evaluation.policy_benchmark.sealed_mechanisms import (
    SealedMechanismEvidenceRuntime,
)
from zyra_evaluation.scenario_runner.live_models import TierKind, TierObservation
from zyra_evaluation.scenario_runner.errors import ScenarioRunnerError


ROOT = Path(__file__).resolve().parents[2]
EFFECTS = (
    "state_mutation",
    "route",
    "placement",
    "tool",
    "verification",
    "permission",
    "compact_restore",
    "fault",
    "recovery",
    "artifact",
    "delivery",
    "topology",
    "memory",
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
            for item in values
        ),
        encoding="utf-8",
    )


def _hard_gates() -> dict:
    return {
        "schema": "zyra.phase2-sealed-hard-gates/v1",
        "human_intervention_count": 0,
        "early_exit_false_positive": 0,
        "critical_fact_recall": 1.0,
        "obligation_retention": 1.0,
        "superseded_requirement_execution": 0,
        "critical_retrieval_without_provenance": 0,
        "duplicate_completed_work": 0,
        "duplicate_commit": 0,
        "duplicate_claim": 0,
        "duplicate_spend": 0,
        "duplicate_lease": 0,
        "duplicate_side_effect": 0,
        "privacy_permission_violation": 0,
        "unsafe_commit": 0,
        "adversarial_proposals": {
            "total": 3,
            "rejected_or_projected": 3,
        },
        "physical_dispatch": {
            "lanes": [
                {
                    "lane": lane,
                    "receipt_digest": canonical_digest(lane),
                    "real_gate_closed": True,
                    "simulated": False,
                    "semantic_only": False,
                }
                for lane in ("local", "edge", "cloud")
            ],
            "condition_change_effect": "safe_fail_closed_recovery",
            "artifact_continuity": True,
        },
        "continuity": {
            "verified_transitions": [
                "compact_restore",
                "process_restart",
                "handoff",
                "requirement_revision",
            ],
            "poisoned_rejected": True,
            "stale_rejected": True,
            "conflicting_rejected": True,
        },
        "loopx": {
            "restart_recovered": True,
            "claim_conflict_rejected": True,
            "quota_exhaustion_fail_closed": True,
            "worker_lease_owner_preserved": True,
            "execution_budget_owner_preserved": True,
        },
        "topology_operator": {
            "role_added": True,
            "role_removed": True,
            "operator_added": True,
            "operator_removed": True,
            "canonical_custody_commit": True,
        },
        "permission_recovery": {
            "denial_observed": True,
            "autonomous_recovery": True,
        },
        "disable_evidence": {
            name: {"disabled_changed_outcome": True}
            for name in (
                "memory_continuity",
                "symbolic_projector",
                "loopx",
                "dynamic_topology",
                "operator_selection",
                "physical_dispatch",
            )
        },
        "production_bypass_reachable": False,
    }


def _build_bundle(tmp_path: Path) -> Path:
    manifest = {
        "schema": "zyra.phase2-sealed-long-run-manifest/v1",
        "slice": "P2-S06-02",
        "candidate_commit": "a" * 40,
        "minimum_valid_transitions_per_run": 2_000,
        "runs": [
            {"run_key": "software", "domain": "software_delivery"},
            {"run_key": "research", "domain": "cross_source_research"},
        ],
    }
    manifest_path = tmp_path / "sealed-manifest.json"
    _write_json(manifest_path, manifest)
    runs = []
    for run_key, domain in (
        ("software", "software_delivery"),
        ("research", "cross_source_research"),
    ):
        root = tmp_path / "runs" / run_key
        root.mkdir(parents=True)
        run_id = f"run-{run_key}"
        task_id = f"task-{run_key}"
        events = []
        previous = ""
        for sequence in range(1, 2_001):
            effect = EFFECTS[(sequence - 1) % len(EFFECTS)]
            event_id = f"event-{run_key}-{sequence:06d}"
            events.append(
                {
                    "event_id": event_id,
                    "event_type": f"canonical_{effect}",
                    "run_id": run_id,
                    "task_id": task_id,
                    "sequence": sequence,
                    "causation_id": previous,
                    "created_at": "2026-07-30T00:00:00Z",
                    "payload": {
                        "semantic_effect": effect,
                        "mutation": {
                            "revision": sequence,
                            "state": f"settled-{sequence}",
                        },
                        "causation_id": previous,
                    },
                    "metadata": {
                        "semantic_effect": effect,
                        "stage": effect,
                    },
                }
            )
            previous = event_id
        raw = root / "raw.jsonl"
        _write_jsonl(raw, events)
        transition = IndependentTransitionValidator().validate(
            events,
            run_id=run_id,
            task_id=task_id,
        ).index(run_id=run_id, task_id=task_id)
        transition_path = root / "transitions.json"
        _write_json(transition_path, transition)
        gates = root / "hard-gates.json"
        _write_json(gates, _hard_gates())
        artifact = root / "artifact.txt"
        artifact.write_text(f"verified {domain}\n", encoding="utf-8")
        verifier = root / "verifier.json"
        _write_json(
            verifier,
            {
                "schema": "zyra.phase2-sealed-final-verifier/v1",
                "run_id": run_id,
                "task_id": task_id,
                "passed": True,
                "artifact_digest": file_digest(artifact),
            },
        )
        runs.append(
            {
                "run_key": run_key,
                "run_id": run_id,
                "task_id": task_id,
                "domain": domain,
                "candidate_commit": "a" * 40,
                "raw_events": raw.relative_to(tmp_path).as_posix(),
                "raw_events_digest": file_digest(raw),
                "transition_index": transition_path.relative_to(
                    tmp_path
                ).as_posix(),
                "hard_gate_bundle": gates.relative_to(tmp_path).as_posix(),
                "hard_gate_bundle_digest": file_digest(gates),
                "final_artifact": artifact.relative_to(tmp_path).as_posix(),
                "final_verifier": verifier.relative_to(tmp_path).as_posix(),
            }
        )
    index = {
        "schema": EVIDENCE_INDEX_SCHEMA,
        "sealed_manifest": manifest_path.name,
        "sealed_manifest_digest": file_digest(manifest_path),
        "runs": runs,
    }
    index_path = tmp_path / "sealed-evidence-index.json"
    _write_json(index_path, index)
    return index_path


def test_independent_validator_recomputes_two_thousand_transitions_per_domain(
    tmp_path: Path,
) -> None:
    report = SealedLongRunValidator().validate(_build_bundle(tmp_path))
    assert report["valid"] is True
    assert {item["domain"] for item in report["runs"]} == {
        "software_delivery",
        "cross_source_research",
    }
    assert all(
        item["valid_transition_count"] == 2_000
        and item["invalid_transition_count"] == 0
        for item in report["runs"]
    )


def test_independent_validator_rejects_runner_count_and_duplicate_semantics(
    tmp_path: Path,
) -> None:
    index_path = _build_bundle(tmp_path)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    first = index["runs"][0]
    transition_path = tmp_path / first["transition_index"]
    claimed = json.loads(transition_path.read_text(encoding="utf-8"))
    claimed["valid_transition_count"] = 9_999
    _write_json(transition_path, claimed)
    report = SealedLongRunValidator().validate(index_path)
    first_report = next(
        item for item in report["runs"] if item["run_key"] == "software"
    )
    assert report["valid"] is False
    assert first_report["valid_transition_count"] == 2_000
    assert "transition_index_recompute_mismatch" in first_report["blockers"]


def test_loopback_remote_lane_requires_real_physical_boundary() -> None:
    base = {
        "observation_id": "edge-real",
        "tier": TierKind.EDGE,
        "endpoint": "http://127.0.0.1:51000",
        "endpoint_id": "edge-node",
        "runtime_id": "edge-generation",
        "process_id": "12345",
        "isolation_id": "edge-failure-boundary",
        "request_id": "edge-request",
        "route_id": "edge-route",
        "lease_id": "edge-lease",
        "artifact_ids": ("edge-artifact",),
        "started_at": "2026-07-30T00:00:00Z",
        "completed_at": "2026-07-30T00:00:01Z",
        "request_digest": "a" * 64,
        "response_digest": "b" * 64,
        "handshake_ok": True,
        "heartbeat_ok": True,
        "task_success": True,
        "simulated": False,
        "loopback": True,
    }
    TierObservation(
        **base,
        metadata={
            "remote_boundary": "isolated-process",
            "failure_boundary_id": "edge-failure-boundary",
            "independent_process": True,
            "physical_dispatch_validation": {"real_gate_closed": True},
        },
    ).validate()
    with pytest.raises(ScenarioRunnerError, match="loopback"):
        TierObservation(**base, metadata={}).validate()


def test_actual_mechanisms_cover_restart_attacks_and_disable_paths(
    tmp_path: Path,
) -> None:
    bundle = SealedMechanismEvidenceRuntime(
        project_root=ROOT,
        state_root=tmp_path,
    ).execute(run_id="sealed-mechanism-run", task_id="sealed-mechanism-task")
    assert bundle["continuity"]["verified_transitions"] == [
        "compact_restore",
        "process_restart",
        "handoff",
        "requirement_revision",
    ]
    assert bundle["continuity"]["poisoned_rejected"] is True
    assert bundle["continuity"]["stale_rejected"] is True
    assert bundle["continuity"]["conflicting_rejected"] is True
    assert bundle["topology_operator"]["invalid_proposal_count"] == 3
    assert bundle["topology_operator"]["unsafe_commit_count"] == 0
    assert bundle["loopx"]["restart_recovered"] is True
    assert bundle["loopx"]["claim_conflict_rejected"] is True
    assert bundle["loopx"]["quota_exhaustion_fail_closed"] is True
    assert all(
        value["disabled_changed_outcome"] is True
        for value in bundle["disable_evidence"].values()
    )
