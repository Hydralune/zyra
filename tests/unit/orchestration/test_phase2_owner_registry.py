from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from zyra_evaluation.policy_benchmark import (
    ContractViolation,
    Phase2PolicyContractBundle,
    validate_source_role_registry,
    validate_state_owner_registry,
)


ROOT = Path(__file__).resolve().parents[3]
CONFIG_ROOT = ROOT / "config" / "phase2"


def _load(name: str) -> dict[str, object]:
    return json.loads((CONFIG_ROOT / name).read_text(encoding="utf-8"))


def _owner_domain(
    registry: dict[str, object],
    domain_id: str,
) -> dict[str, object]:
    return next(
        item
        for item in registry["state_domains"]  # type: ignore[union-attr]
        if item["domain_id"] == domain_id
    )


def test_owner_overlay_references_frozen_catalog_without_taking_state() -> None:
    summary = validate_state_owner_registry(
        _load("state-owners.yaml"),
        repository_root=ROOT,
    )

    assert summary["canonical_owner_count"] == summary["domain_count"]
    assert summary["proposal_owner_count"] == 0
    assert summary["loopx_private_domains"] == ["loopx_private_control"]


def test_duplicate_owner_domain_is_rejected() -> None:
    registry = _load("state-owners.yaml")
    registry["state_domains"].append(  # type: ignore[union-attr]
        copy.deepcopy(registry["state_domains"][0])  # type: ignore[index]
    )

    with pytest.raises(ContractViolation) as failure:
        validate_state_owner_registry(registry)

    assert failure.value.code == "canonical-owner-domain-duplicate"


def test_maas_cannot_become_scheduler_lease_or_execution_owner() -> None:
    registry = _load("state-owners.yaml")
    placement = _owner_domain(registry, "resource_placement")
    placement["canonical_owner"]["source_id"] = "maas"  # type: ignore[index]

    with pytest.raises(ContractViolation) as failure:
        validate_state_owner_registry(registry)

    assert failure.value.code == "proposal-source-became-owner"

    registry = _load("state-owners.yaml")
    maas = next(
        item
        for item in registry["mechanism_constraints"]  # type: ignore[union-attr]
        if item["mechanism_id"] == "maas"
    )
    maas["forbidden_domains"].remove("worker_lease_physical_attempt")
    with pytest.raises(ContractViolation) as failure:
        validate_state_owner_registry(registry)
    assert failure.value.code == "maas-owner-boundary-invalid"


@pytest.mark.parametrize(
    "forbidden_domain",
    [
        "task_session_attempt",
        "graph_state",
        "permission",
        "worker_lease_physical_attempt",
        "execution_budget",
    ],
)
def test_loopx_cannot_own_zyra_state(forbidden_domain: str) -> None:
    registry = _load("state-owners.yaml")
    domain = _owner_domain(registry, forbidden_domain)
    domain["canonical_owner"]["source_id"] = "loopx"  # type: ignore[index]

    with pytest.raises(ContractViolation) as failure:
        validate_state_owner_registry(registry)

    assert failure.value.code == "loopx-owner-scope-exceeded"


def test_tampered_frozen_owner_catalog_binding_fails_closed() -> None:
    registry = _load("state-owners.yaml")
    registry["frozen_owner_evidence_catalog"]["sha256"] = "0" * 64  # type: ignore[index]

    with pytest.raises(ContractViolation) as failure:
        validate_state_owner_registry(registry, repository_root=ROOT)

    assert failure.value.code == "frozen-owner-catalog-tampered"


def test_topology_source_quota_is_exact_and_cannot_be_expanded() -> None:
    registry = _load("source-roles.yaml")
    topology = next(
        item
        for item in registry["capability_domains"]  # type: ignore[union-attr]
        if item["domain_id"] == "topology_proposal"
    )
    assert [entry["source_id"] for entry in topology["entries"]] == [
        "arg_designer",
        "card",
        "agentprune",
    ]

    topology["entries"].append(copy.deepcopy(topology["entries"][1]))
    topology["entries"][-1]["source_id"] = "extra_topology_source"
    topology["entries"][-1]["source_name"] = "ExtraTopologySource"
    with pytest.raises(ContractViolation) as failure:
        validate_source_role_registry(registry)
    assert failure.value.code == "source-supplementary-limit-exceeded"


def test_openclaw_cannot_reenter_role_table() -> None:
    registry = _load("source-roles.yaml")
    topology = next(
        item
        for item in registry["capability_domains"]  # type: ignore[union-attr]
        if item["domain_id"] == "topology_proposal"
    )
    topology["entries"][0]["source_name"] = "OpenClaw"

    with pytest.raises(ContractViolation) as failure:
        validate_source_role_registry(registry)

    assert failure.value.code == "openclaw-forward-role-forbidden"


def test_strongest_activation_api_fails_closed_until_readiness_audit() -> None:
    with pytest.raises(ContractViolation) as failure:
        Phase2PolicyContractBundle.load(ROOT).require_strongest_activation()

    assert failure.value.code == "strongest-profile-readiness-failed"
