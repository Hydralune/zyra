from __future__ import annotations

import json
from pathlib import Path

import pytest

from zyra_evaluation.policy_benchmark.long_run_validator import (
    IndependentTransitionValidator,
    canonical_digest,
)
from zyra_evaluation.policy_benchmark.sealed_mechanisms import (
    SealedMechanismEvidenceRuntime,
)
from zyra_evaluation.policy_benchmark.sealed_long_run import (
    SealedLongRunError,
    SealedLongRunRunner,
    _evidence_digest,
    _json,
)
from zyra_evaluation.policy_benchmark.sealed_physical import (
    SealedPhysicalDispatchError,
    _receipt_evidence,
)
from zyra_evaluation.scenario_runner.live_models import TierKind, TierObservation
from zyra_evaluation.scenario_runner.errors import ScenarioRunnerError
from zyra_evaluation.scenario_runner.dual_domain import CanonicalEventBuilder
from zyra_evaluation.scenario_runner.research_delivery import (
    LiveHttpSourceAcquirer,
)


ROOT = Path(__file__).resolve().parents[2]


def _analysis_receipt(
    unit_id: str,
    *,
    byte_start: int,
    byte_end: int,
    source_locator: str = "source.py",
) -> dict:
    receipt = {
        "schema": "zyra.analysis-unit-owner-receipt/v1",
        "owner": "SQLiteStore.MemoryRecord",
        "run_id": "run-analysis",
        "task_id": "task-analysis",
        "work_unit_id": unit_id,
        "memory_id": f"memory-{unit_id}",
        "kind": "source-analysis",
        "input_digest": "a" * 64,
        "output_digest": "b" * 64,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "analysis_index": 1,
        "source_locator": source_locator,
        "content_digest": "c" * 64,
        "committed": True,
        "readback_verified": True,
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    return receipt


def _owner_bound_event(sequence: int, previous: str) -> tuple[dict, dict]:
    event_id = f"event-{sequence}"
    source = {
        "event_id": event_id,
        "event_type": "canonical_state_mutation",
        "run_id": "run-owner",
        "task_id": "task-owner",
        "sequence": sequence,
        "causation_id": previous,
        "payload": {
            "semantic_effect": "state_mutation",
            "mutation": {"revision": sequence, "state": f"v{sequence}"},
            "causation_id": previous,
        },
        "metadata": {"stage": "owner-test"},
    }
    persisted = {
        "event_id": event_id,
        "event_type": "task.updated",
        "run_id": "run-owner",
        "task_id": "task-owner",
        "payload": {"canonical_revision": sequence},
    }
    receipt = {
        "schema": "zyra.canonical-event-owner-receipt/v1",
        "owner": "SQLiteStore.EventRecord",
        "run_id": "run-owner",
        "task_id": "task-owner",
        "event_id": event_id,
        "source_event_type": source["event_type"],
        "persisted_event_type": persisted["event_type"],
        "source_event_digest": canonical_digest(source),
        "persisted_event_digest": canonical_digest(persisted),
        "persisted_payload_digest": canonical_digest(persisted["payload"]),
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    source["owner_receipt"] = receipt
    return source, persisted


def test_independent_validator_rejects_two_thousand_unowned_claims() -> None:
    events = []
    previous = ""
    for sequence in range(1, 2_001):
        event = _owner_bound_event(sequence, previous)[0]
        event.pop("owner_receipt")
        events.append(event)
        previous = event["event_id"]
    result = IndependentTransitionValidator().validate(
        events,
        run_id="run-owner",
        task_id="task-owner",
    )
    assert result.valid_count == 0
    assert len(result.invalid) == 2_000
    assert all(
        "owner_receipt_missing" in item["reason_codes"]
        for item in result.invalid
    )


def test_independent_validator_recomputes_owner_snapshot_and_rejects_tamper() -> None:
    first, persisted_first = _owner_bound_event(1, "")
    second, persisted_second = _owner_bound_event(2, first["event_id"])
    valid = IndependentTransitionValidator().validate(
        (first, second),
        run_id="run-owner",
        task_id="task-owner",
        owner_events=(persisted_first, persisted_second),
    )
    assert valid.valid_count == 2
    persisted_second["payload"]["canonical_revision"] = 99
    invalid = IndependentTransitionValidator().validate(
        (first, second),
        run_id="run-owner",
        task_id="task-owner",
        owner_events=(persisted_first, persisted_second),
    )
    assert invalid.valid_count == 1
    assert "owner_event_digest" in invalid.invalid[0]["reason_codes"]


def test_work_units_reject_overlapping_owner_backed_source_ranges() -> None:
    builder = CanonicalEventBuilder(
        run_id="run-analysis",
        task_id="task-analysis",
    )
    with pytest.raises(ScenarioRunnerError) as blocked:
        builder.work_units(
            (
                _analysis_receipt("unit-1", byte_start=0, byte_end=100),
                _analysis_receipt("unit-2", byte_start=50, byte_end=150),
            ),
            worker_id="worker-analysis",
            provider_id="zyra-local",
            stage="source-index",
        )
    assert blocked.value.code == "live_work_unit_not_distinct"


def test_validator_binds_analysis_range_to_persisted_memory_content() -> None:
    persisted_content = {
        "work_unit_id": "unit-1",
        "input_digest": "a" * 64,
        "output_digest": "b" * 64,
        "byte_start": 0,
        "byte_end": 100,
        "source_locator": "persisted.py",
    }
    memory = {
        "memory_id": "memory-unit-1",
        "run_id": "run-analysis",
        "task_id": "task-analysis",
        "source_id": "unit-1",
        "content": persisted_content,
    }
    unit = _analysis_receipt(
        "unit-1",
        byte_start=0,
        byte_end=100,
        source_locator="claimed.py",
    )
    unit["content_digest"] = canonical_digest(persisted_content)
    unsigned_unit = dict(unit)
    unsigned_unit.pop("receipt_digest")
    unit["receipt_digest"] = canonical_digest(unsigned_unit)
    event = {
        "event_id": "event-analysis-1",
        "event_type": "analysis_unit_indexed",
        "run_id": "run-analysis",
        "task_id": "task-analysis",
        "sequence": 1,
        "causation_id": "",
        "payload": {
            "semantic_effect": "memory",
            "mutation": {"owner_receipt": unit, "state": "owner-indexed"},
            "causation_id": "",
        },
        "metadata": {"stage": "source-index"},
    }
    owner_receipt = {
        "schema": "zyra.canonical-analysis-event-owner-receipt/v1",
        "owner": "SQLiteStore.MemoryRecord",
        "run_id": "run-analysis",
        "task_id": "task-analysis",
        "event_id": event["event_id"],
        "source_event_type": event["event_type"],
        "source_event_digest": canonical_digest(event),
        "memory_id": memory["memory_id"],
        "analysis_receipt_digest": unit["receipt_digest"],
        "persisted_memory_digest": canonical_digest(memory),
        "persisted_content_digest": canonical_digest(persisted_content),
    }
    owner_receipt["receipt_digest"] = canonical_digest(owner_receipt)
    event["owner_receipt"] = owner_receipt
    result = IndependentTransitionValidator().validate(
        (event,),
        run_id="run-analysis",
        task_id="task-analysis",
        owner_analysis_records=(memory,),
    )
    assert result.valid_count == 0
    assert "analysis_memory_content_binding" in result.invalid[0]["reason_codes"]


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


def test_physical_receipt_contract_envelope_is_flattened_for_evidence() -> None:
    flattened = _receipt_evidence(
        {
            "contract_id": "physical-dispatch:test",
            "created_at": "2026-07-30T00:00:00Z",
            "digest": "a" * 64,
            "payload": {
                "physical_attempt_id": "attempt-test",
                "physical_identity": {"location": "local"},
                "simulated": False,
                "semantic_only": False,
            },
        }
    )
    assert flattened["physical_identity"]["location"] == "local"
    assert flattened["digest"] == "a" * 64
    assert flattened["contract_id"] == "physical-dispatch:test"
    with pytest.raises(
        SealedPhysicalDispatchError,
        match="contract payload is missing",
    ):
        _receipt_evidence({"digest": "b" * 64})


def test_live_research_redirects_stay_inside_frozen_host_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquirer = LiveHttpSourceAcquirer(
        artifact_root=tmp_path / "sources",
        allowed_hostnames=("www.rfc-editor.org",),
    )
    monkeypatch.setattr(
        acquirer,
        "_resolve_addresses",
        lambda host: ["93.184.216.34"],
    )
    assert (
        acquirer._validated_url("https://www.rfc-editor.org/rfc/rfc9110.txt")
        == "https://www.rfc-editor.org/rfc/rfc9110.txt"
    )
    with pytest.raises(ScenarioRunnerError) as blocked:
        acquirer._validated_url("https://redirect.example/rfc9110.txt")
    assert blocked.value.code == "research_host_not_allowed"


def test_sealed_manifest_preflight_rejects_noncanonical_fault_kind() -> None:
    runner = object.__new__(SealedLongRunRunner)
    with pytest.raises(
        SealedLongRunError,
        match="sealed fault schedule kind is invalid: worker_loss",
    ):
        runner._validate_fault_schedules(
            (
                {
                    "failure_schedule": (
                        {"kind": "requirement_change"},
                        {"kind": "worker_loss"},
                    )
                },
            )
        )


def test_final_research_artifact_uses_canonical_kind_after_publication(
    tmp_path: Path,
) -> None:
    randomized_report = tmp_path / "artifact_abcd1234.json"
    randomized_report.write_text('{"valid": true}\n', encoding="utf-8")
    selected = SealedLongRunRunner._final_artifact(
        (
            {
                "kind": "report",
                "path": str(randomized_report),
            },
        ),
        {"domain": "cross_source_research"},
    )
    assert selected == randomized_report.resolve()


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
    mechanism_path = tmp_path / "mechanism-bundle.json"
    _json(mechanism_path, bundle)
    persisted = json.loads(mechanism_path.read_text(encoding="utf-8"))
    assert persisted["topology_operator"]["canonical_custody_commit"] is True
    assert len(_evidence_digest(bundle["continuity"])) == 64
