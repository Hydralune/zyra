from __future__ import annotations

from pathlib import Path

from zyra_orchestration.topology_policy.arg import ARGTopologyRuntime
from zyra_orchestration.topology_policy.condition import CARDTopologyRuntime
from zyra_orchestration.topology_policy.pruning import AgentPruneRuntime


ROOT = Path(__file__).resolve().parents[2]


def test_readiness_contract_digest_is_stable_across_windows_checkout(
    tmp_path: Path,
) -> None:
    readiness_source = (
        ROOT / "config" / "phase2" / "mechanism-readiness.json"
    ).read_bytes()
    readiness_path = (
        tmp_path / "config" / "phase2" / "mechanism-readiness.json"
    )
    readiness_path.parent.mkdir(parents=True)
    readiness_path.write_bytes(
        readiness_source.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    )

    arg = ARGTopologyRuntime.from_repository(
        tmp_path,
        config_path=ROOT / "config" / "phase2" / "arg-joint-topology.json",
        report_path=(
            ROOT
            / "docs"
            / "reviews"
            / "evidence"
            / "P2-S03-01"
            / "MechanismEvidenceReadinessReport.json"
        ),
    )
    card = CARDTopologyRuntime.from_repository(
        tmp_path,
        config_path=(
            ROOT / "config" / "phase2" / "card-directional-residual.json"
        ),
        report_path=(
            ROOT
            / "docs"
            / "reviews"
            / "evidence"
            / "P2-S03-02"
            / "MechanismEvidenceReadinessReport.json"
        ),
    )
    agentprune = AgentPruneRuntime.from_repository(
        tmp_path,
        config_path=ROOT / "config" / "phase2" / "agentprune-pruning.json",
        report_path=(
            ROOT
            / "docs"
            / "reviews"
            / "evidence"
            / "P2-S03-03"
            / "MechanismEvidenceReadinessReport.json"
        ),
    )

    assert arg.disconnected_reason == ""
    assert card.disconnected_reason == ""
    assert agentprune.disconnected_reason == ""
    assert arg.readiness.status == "deterministic_ready"
    assert card.readiness.status == "deterministic_ready"
    assert agentprune.readiness.status == "deterministic_ready"
