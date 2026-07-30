from __future__ import annotations

import json
from pathlib import Path

import pytest

from zyra_runtime.productization.absorption import (
    RuntimeAbsorptionCoordinator,
    load_m3_audit_input,
)
from zyra_runtime.productization.causality import (
    CausalEffectVerifier,
    CausalReceiptLedger,
    default_causal_contracts,
    envelope_for_receipt,
)
from zyra_runtime.productization.composition import (
    RuntimeActivationSet,
    RuntimeOwnerComposition,
    activation_result,
)
from zyra_runtime.productization.contracts import (
    CausalEffectKind,
    MutationIdentity,
    ProductizationContractError,
    RuntimeDomain,
)
from zyra_runtime.productization.defaults import (
    default_entry_bindings,
    default_owner_bindings,
)
from zyra_runtime.productization.source_boundary import (
    BoundaryDecision,
    PathScope,
    RepositoryBoundaryInspector,
    classify_scope,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def healthy_composition() -> RuntimeOwnerComposition:
    owners = {
        domain: (
            lambda selected=domain: activation_result(
                ready=True,
                owner=f"{selected.value}.owner",
                store=f"{selected.value}.store",
                state_root=str(PROJECT_ROOT / "tmp" / selected.value),
                revision=1,
            )
        )
        for domain in RuntimeDomain
    }

    def entry_probe(entry):
        return {
            "ready": True,
            "reachable": True,
            "write_path_ready": True,
            "write_symbols": list(entry.write_symbols),
            "fallback_bypass": False,
            "route": entry.command_or_route,
        }

    entries = {
        entry.entry_id: entry_probe
        for entry in default_entry_bindings()
    }
    return RuntimeOwnerComposition(
        PROJECT_ROOT,
        RuntimeActivationSet(owner=owners, entry=entries),
    )


def test_default_owner_contract_covers_exactly_eleven_domains() -> None:
    bindings = default_owner_bindings()
    assert len(bindings) == 11
    assert {item.domain for item in bindings} == set(RuntimeDomain)
    assert len({item.owner_token for item in bindings}) == 11
    assert all(item.default_entries for item in bindings)
    assert all(not item.fallback_refs for item in bindings)


def test_composition_probes_real_sources_and_every_default_entry() -> None:
    composition = healthy_composition()
    snapshot = composition.probe_all()

    assert snapshot["ready"] is True
    assert snapshot["blockers"] == []
    assert len(snapshot["domains"]) == 11
    assert {
        item["domain"] for item in snapshot["domains"]
    } == {item.value for item in RuntimeDomain}
    for domain in snapshot["domains"]:
        assert domain["owner"]["details"]["observations"][0]["ready"] is True
        source_observation = domain["owner"]["details"]["observations"][1]
        assert source_observation["probe_kind"] == "owner_source_contract"
        assert all(item["accepted"] for item in source_observation["references"])
        assert domain["default_entries"]
        assert all(item["ready"] for item in domain["default_entries"])


@pytest.mark.parametrize("domain", list(RuntimeDomain))
def test_owner_loss_rejects_mutation_gate_without_projection_fallback(
    domain: RuntimeDomain,
) -> None:
    proof = healthy_composition().prove_owner_loss(domain)

    assert proof["valid"] is True
    assert proof["ready_before"] is True
    assert proof["rejected_while_disabled"] is True
    assert proof["fallback_success"] is False
    assert proof["ready_after"] is True
    assert proof["generation_after"] > proof["generation_before"]


def test_owner_generation_revokes_an_existing_lease() -> None:
    composition = healthy_composition()
    lease = composition.owner_registry.acquire(
        RuntimeDomain.PERMISSION,
        operation="permission-decision",
        correlation_id="correlation-1",
    )
    composition.owner_registry.mark_lost(
        RuntimeDomain.PERMISSION,
        "permission audit owner disconnected",
    )

    with pytest.raises(ProductizationContractError) as captured:
        composition.owner_registry.validate_lease(lease)

    assert captured.value.code == "owner_lease_unknown"
    assert composition.owner_registry.probe(RuntimeDomain.PERMISSION).ready is False
    assert composition.owner_registry.lease_history()[-1]["outcome"] == "revoked"


def test_projection_cannot_be_registered_as_a_canonical_writer() -> None:
    binding = next(
        item
        for item in default_owner_bindings()
        if item.domain is RuntimeDomain.PERMISSION
    )
    projection_symbol = binding.projections[0].symbol
    assert projection_symbol == "PermissionConsoleRuntime"
    assert projection_symbol not in {item.symbol for item in binding.writers}
    assert projection_symbol != binding.owner.symbol
    assert projection_symbol != binding.store.symbol


def test_causal_receipt_is_committed_before_permission_event() -> None:
    composition = healthy_composition()
    lease = composition.owner_registry.acquire(
        RuntimeDomain.PERMISSION,
        operation="permission-decision",
        correlation_id="correlation-permission",
    )
    ledger = CausalReceiptLedger(composition.owner_registry)
    identity = MutationIdentity(
        run_id="run-1",
        task_id="task-1",
        mutation_id="decision-1",
        revision=1,
        correlation_id="correlation-permission",
        causation_id="request-1",
        span_id="span-1",
        call_id="tool-call-1",
        owner_generation=lease.generation,
    )
    receipt = ledger.commit(
        lease,
        identity=identity,
        effect_kind=CausalEffectKind.PERMISSION,
        effect_ref="decision-1",
        effect_payload={"effect": "allow", "tool": "read"},
    )
    event = envelope_for_receipt(
        receipt,
        event_name="permission.decision.recorded",
        attributes={
            "decision_id": "decision-1",
            "disposition": "allow",
            "session_id": "session-1",
        },
    )
    ledger.record_event(event)
    verification = CausalEffectVerifier(ledger).verify(
        "permission.decision.recorded",
        event,
    )
    composition.owner_registry.release(
        lease,
        outcome="completed",
        mutation_receipt_id=receipt.receipt_id,
    )

    assert verification.valid is True
    assert verification.missing_attributes == ()
    assert verification.mismatches == ()
    assert ledger.snapshot().mutation_receipts == (receipt,)
    assert composition.owner_registry.lease_history()[-1][
        "mutation_receipt_id"
    ] == receipt.receipt_id


def test_canonical_event_without_effect_receipt_is_rejected() -> None:
    composition = healthy_composition()
    ledger = CausalReceiptLedger(composition.owner_registry)
    receipt = ledger.snapshot()
    assert receipt.rejected_events == 0

    from zyra_runtime.productization.contracts import (
        CanonicalEventEnvelope,
        EventSemanticClass,
    )

    with pytest.raises(ProductizationContractError) as captured:
        ledger.record_event(
            CanonicalEventEnvelope(
                event_id="event-without-effect",
                event_name="permission.decision.recorded",
                domain=RuntimeDomain.PERMISSION,
                attributes={
                    "decision_id": "decision-1",
                    "disposition": "allow",
                },
                emitted_at_ns=1,
                semantic_class=EventSemanticClass.CANONICAL_EFFECT,
            )
        )

    assert captured.value.code == "canonical_event_receipt_missing"
    assert ledger.snapshot().rejected_events == 1


def test_source_boundary_distinguishes_test_command_from_install(
    tmp_path: Path,
) -> None:
    package = tmp_path / "packages" / "demo"
    package.mkdir(parents=True)
    manifest = package / "package.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "@zyra/demo",
                "scripts": {
                    "test": "bun test ./test",
                    "build": "npm --workspace @zyra/demo run compile",
                },
            }
        ),
        encoding="utf-8",
    )
    inspector = RepositoryBoundaryInspector(
        tmp_path,
        revision="a" * 40,
    )

    evidence = inspector.inspect_path(
        "packages/demo/package.json",
        finding_code="process_dynamic_installer",
    )

    assert evidence.resolved is True
    assert evidence.decision is BoundaryDecision.SAFE_RUNTIME
    assert not evidence.findings


def test_source_boundary_keeps_actual_dynamic_install_blocking(
    tmp_path: Path,
) -> None:
    package = tmp_path / "packages" / "demo"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@zyra/demo",
                "scripts": {"bootstrap": "bun install --frozen-lockfile"},
            }
        ),
        encoding="utf-8",
    )
    inspector = RepositoryBoundaryInspector(
        tmp_path,
        revision="b" * 40,
    )

    evidence = inspector.inspect_path(
        "packages/demo/package.json",
        finding_code="process_dynamic_installer",
    )

    assert evidence.resolved is False
    assert evidence.decision is BoundaryDecision.BLOCKED_DYNAMIC_INSTALL
    assert "package_script_dynamic_install" in evidence.findings


def test_source_boundary_treats_provenance_parent_path_as_non_executable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "packages" / "demo"
    path.mkdir(parents=True)
    source = path / "provenance.py"
    source.write_text(
        'SOURCE = "../claude-code-best/src/query.ts"\n'
        'OWNER = "Zyra"\n',
        encoding="utf-8",
    )
    inspector = RepositoryBoundaryInspector(
        tmp_path,
        revision="c" * 40,
    )

    evidence = inspector.inspect_path(
        "packages/demo/provenance.py",
        finding_code="python_parent_source_path",
    )

    assert evidence.resolved is True
    assert all(not item.production_effect for item in evidence.parent_source_uses)


def test_source_boundary_blocks_parent_path_used_by_process(
    tmp_path: Path,
) -> None:
    path = tmp_path / "packages" / "demo"
    path.mkdir(parents=True)
    source = path / "runtime.py"
    source.write_text(
        'import subprocess\n'
        'subprocess.run(["python", "../opencode/server.py"], check=True)\n',
        encoding="utf-8",
    )
    inspector = RepositoryBoundaryInspector(
        tmp_path,
        revision="d" * 40,
    )

    evidence = inspector.inspect_path(
        "packages/demo/runtime.py",
        finding_code="python_parent_source_path",
    )

    assert evidence.resolved is False
    assert evidence.decision is BoundaryDecision.BLOCKED_PARENT_SOURCE
    assert any(item.production_effect for item in evidence.parent_source_uses)


def test_source_boundary_excludes_embedded_loopx_vendor_like_runtime(
    tmp_path: Path,
) -> None:
    upstream = (
        tmp_path
        / "packages"
        / "integrations"
        / "loopx_runtime"
        / "examples"
    )
    upstream.mkdir(parents=True)
    (upstream / "install_probe.py").write_text(
        "import subprocess\n"
        "subprocess.run(['pip', 'install', 'mutable-package'], check=True)\n",
        encoding="utf-8",
    )
    zyra_owned = (
        tmp_path
        / "packages"
        / "integrations"
        / "zyra_integrations"
        / "loopx"
    )
    zyra_owned.mkdir(parents=True)
    (zyra_owned / "bridge.py").write_text(
        "BRIDGE_OWNER = 'Zyra'\n",
        encoding="utf-8",
    )

    report = RepositoryBoundaryInspector(
        tmp_path,
        revision="f" * 40,
    ).audit(())

    assert classify_scope(
        "packages/integrations/loopx_runtime/loopx/cli.py"
    ) is PathScope.VENDOR
    assert classify_scope(
        "packages/integrations/zyra_integrations/loopx/bridge.py"
    ) is PathScope.PRODUCTION
    assert report.ready is True
    assert report.dynamic_install_hits == ()


def test_parent_queue_is_checksum_bound_and_absorption_closes_all_blockers() -> None:
    evidence_root = (
        PROJECT_ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M3-S01A-02"
    )
    audit_input = load_m3_audit_input(
        evidence_root / "downstream" / "m3_01b.json",
        evidence_root / "state-owner-reachability-receipt.json",
    )
    composition = healthy_composition()
    ledger = CausalReceiptLedger(composition.owner_registry)
    verifier = CausalEffectVerifier(ledger)
    required_values = {
        "session_id": "session-witness",
        "sequence": 1,
        "decision_id": "decision-witness",
        "disposition": "allow",
        "candidate_id": "candidate-witness",
        "plan_id": "plan-witness",
        "attempt_id": "attempt-witness",
        "artifact_id": "artifact-witness",
        "backend_id": "backend-witness",
        "lease_id": "lease-witness",
        "graph_id": "graph-witness",
        "provider_id": "provider-witness",
        "model_id": "model-witness",
        "dispatch_id": "dispatch-witness",
        "request_id": "request-witness",
        "generation": 1,
    }
    for contract in default_causal_contracts():
        lease = composition.owner_registry.acquire(
            contract.domain,
            operation=f"causal-witness:{contract.link_id}",
            correlation_id=f"correlation-{contract.domain.value}",
        )
        identity = MutationIdentity(
            run_id="run-witness",
            task_id="task-witness",
            mutation_id=f"mutation-{contract.domain.value}",
            revision=1,
            correlation_id=f"correlation-{contract.domain.value}",
            causation_id=f"causation-{contract.domain.value}",
            owner_generation=lease.generation,
        )
        receipt = ledger.commit(
            lease,
            identity=identity,
            effect_kind=contract.effect_kind,
            effect_ref=f"effect-{contract.domain.value}",
            effect_payload={"domain": contract.domain.value, "committed": True},
        )
        attributes = {
            name: required_values.get(name, f"{name}-witness")
            for name in contract.required_attributes
        }
        if "digest" in attributes:
            attributes["digest"] = receipt.effect_digest
        event = envelope_for_receipt(
            receipt,
            event_name=contract.event_name,
            attributes=attributes,
        )
        ledger.record_event(event)
        composition.owner_registry.release(
            lease,
            outcome="completed",
            mutation_receipt_id=receipt.receipt_id,
        )

    source_inspector = RepositoryBoundaryInspector(
        PROJECT_ROOT,
        revision="e" * 40,
    )
    receipt = RuntimeAbsorptionCoordinator(
        project_root=PROJECT_ROOT,
        revision="e" * 40,
        audit_input=audit_input,
        owner_registry=composition.owner_registry,
        default_registry=composition.default_registry,
        causal_verifier=verifier,
        source_inspector=source_inspector,
    ).run()

    assert audit_input.queue.digest == (
        "sha256:f5b931f50c960856dfa7c8197d1c22d7c4ec3f6110832ed5114143ef4eb3d4d6"
    )
    assert receipt.summary.total == 119
    assert receipt.summary.unresolved == 0, [
        item.to_dict() for item in receipt.resolutions if not item.resolved
    ]
    assert receipt.summary.blocking_unresolved == 0, [
        item.to_dict()
        for queue_item, item in zip(audit_input.queue.items, receipt.resolutions)
        if queue_item.blocking and not item.resolved
    ]
    assert receipt.owner_boundary_ready is True
    assert receipt.default_boundary_ready is True
    assert receipt.causal_boundary_ready is True
    assert receipt.source_boundary_ready is True, {
        key: value
        for key, value in source_inspector.audit(audit_input.queue.items).to_dict().items()
        if key != "item_resolutions"
    }
    assert receipt.release_ready is True
