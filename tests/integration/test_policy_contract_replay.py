from __future__ import annotations

import importlib
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from zyra_orchestration.topology_policy import (
    ContractHeader,
    FrozenDict,
    GraphSnapshotRef,
    PolicyEvidencePublisher,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
)
from zyra_runtime import LocalArtifactStore


NOW = "2026-07-29T12:00:00Z"


def _proposal() -> TopologyProposalArtifact:
    return TopologyProposalArtifact(
        header=ContractHeader(
            contract_id="proposal-replay",
            created_at=NOW,
            source_event_id="event-source",
            correlation_id="correlation-policy",
            causation_id="causation-policy",
            mechanism_id="ARG",
            mechanism_version="deterministic-adapter-v1",
            input_version="zyra.policy-input-snapshot/v1",
            idempotency_key="replay-key",
        ),
        proposal_id="proposal-replay",
        input_snapshot_digest="a" * 64,
        base_graph=GraphSnapshotRef(
            graph_id="graph-policy",
            run_id="run-policy",
            revision=0,
            signature="b" * 64,
            commit_id="",
        ),
        operations=(
            TopologyOperation(
                kind=TopologyOperationKind.SET_GRAPH_METADATA,
                entity_id="policy-marker",
                value=FrozenDict({"value": "replay-tested"}),
                reason="exercise canonical artifact replay",
            ),
        ),
        expected_outcome=FrozenDict({"time_ms": 1}),
        alternatives=(),
        reasons=("deterministic",),
        constraint_assumptions=(),
        expires_at="2026-07-29T12:05:00Z",
        fallback_profile="phase1_deterministic_baseline",
    )


def test_policy_contract_uses_existing_artifact_event_owners_and_replays(tmp_path: Path) -> None:
    events = []
    store = LocalArtifactStore(tmp_path / "artifacts")
    publisher = PolicyEvidencePublisher(store, admit_event=events.append)
    proposal = _proposal()

    published = publisher.publish(
        proposal,
        run_id="run-policy",
        task_id="task-policy",
    )
    replayed = publisher.replay(published.artifact)

    assert replayed.to_dict() == proposal.to_dict()
    assert published.artifact_ref.digest == published.artifact.metadata["sha256"]
    assert published.artifact.metadata["policy_contract_digest"] == proposal.digest
    assert len(events) == 1
    assert events[0].payload["policy_artifact"] == published.artifact_ref.to_dict()
    assert events[0].payload["source_event_id"] == proposal.header.source_event_id
    assert events[0].payload["correlation_id"] == proposal.header.correlation_id
    assert events[0].payload["causation_id"] == proposal.header.causation_id


def test_policy_replay_rejects_tampered_artifact_bytes(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    publisher = PolicyEvidencePublisher(store, admit_event=lambda event: None)
    published = publisher.publish(
        _proposal(),
        run_id="run-policy",
        task_id="task-policy",
    )
    store.resolve_path(published.artifact).write_text("{}", encoding="utf-8")

    with pytest.raises(Exception, match="mismatch"):
        publisher.replay(published.artifact)


def test_policy_package_does_not_define_a_parallel_state_store() -> None:
    package = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "orchestration"
        / "zyra_orchestration"
        / "topology_policy"
    )
    joined = "\n".join(
        source.read_text(encoding="utf-8") for source in package.glob("*.py")
    )
    assert "sqlite3" not in joined
    assert "CREATE TABLE" not in joined


def test_policy_contract_catalog_is_reachable_through_real_api_handler() -> None:
    module = importlib.import_module("apps.api.zyra_api.main")
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/schema/policy-contracts",
            timeout=30,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert response.headers["Cache-Control"] == "public, max-age=300"
        assert payload["schema"] == "zyra.policy-contract-catalog/v1"
        assert len(payload["contracts"]) == 9
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
