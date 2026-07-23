from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zyra_evaluation.m1_hardening import (
    ExecutionTierError,
    ExecutionTierProbeSuite,
    ManagedLiveEvidenceRuntime,
    ManagedProviderReceipt,
    discover_non_loopback_ipv4,
)
from zyra_evaluation.m1_hardening.contracts import utc_now
from zyra_evaluation.m1_hardening.integration_contracts import stable_digest
from zyra_evaluation.m1_hardening.live_evidence import (
    LiveEvidenceSuite,
    ProviderWireAttestation,
)


def _provider_receipt(
    *,
    provider_id: str = "openai",
    model_id: str = "gpt-test",
    dialect: str = "openai-compatible",
    cli_kind: str = "codex",
) -> ManagedProviderReceipt:
    now = utc_now()
    attestation = ProviderWireAttestation.from_mapping(
        {
            "provider_id": provider_id,
            "model_id": model_id,
            "dialect": dialect,
            "endpoint": (
                "https://api.anthropic.com/v1/messages"
                if provider_id == "anthropic"
                else "https://api.openai.com/v1/responses"
            ),
            "request_id": f"provider-request-{provider_id}-live-tier",
            "attempt_id": f"provider-attempt-{provider_id}-live-tier",
            "route_id": f"provider-route-{provider_id}-live-tier",
            "request_method": "POST",
            "request_path": (
                "/v1/messages" if provider_id == "anthropic" else "/v1/responses"
            ),
            "request_digest": "a" * 64,
            "response_status": 200,
            "response_digest": "b" * 64,
            "chunks": [
                {
                    "sequence": 0,
                    "event": "item.started",
                    "content_digest": "c" * 64,
                    "byte_count": 64,
                    "timestamp": now,
                    "tool_call_id": f"provider-tool-{provider_id}-live-tier",
                },
                {
                    "sequence": 1,
                    "event": "turn.completed",
                    "content_digest": "d" * 64,
                    "byte_count": 64,
                    "timestamp": now,
                    "tool_call_id": f"provider-tool-{provider_id}-live-tier",
                    "finish_reason": "end_turn",
                },
            ],
            "tool_call_ids": [f"provider-tool-{provider_id}-live-tier"],
            "tool_result_ids": [f"provider-tool-{provider_id}-live-tier"],
            "started_at": now,
            "completed_at": now,
            "retry_count": 0,
            "metadata": {
                "transport_kind": "provider_owned_managed_cli",
                "authenticated_session": True,
                "auth_custodian": "provider_cli",
                "direct_wire_headers_observed": False,
                "status_source": "provider_cli_success_exit",
                "cli_kind": cli_kind,
                "cli_version": "test",
                "command_id": f"provider-command-{provider_id}-live-tier",
            },
        }
    )
    return ManagedProviderReceipt(
        attestation=attestation,
        trace_path="provider-owned-test-trace",
        trace_digest="e" * 64,
        command_id="provider-command-live-tier",
    )


def test_execution_tier_suite_uses_real_worker_leases_for_local_edge_and_cloud(
    tmp_path: Path,
) -> None:
    try:
        edge_host = discover_non_loopback_ipv4()
    except ExecutionTierError as error:
        pytest.skip(str(error))
    provider = _provider_receipt()
    receipt = ExecutionTierProbeSuite(Path(__file__).resolve().parents[2]).execute(
        artifact_root=tmp_path,
        cloud_provider=provider,
        edge_host=edge_host,
        run_id="m1-live-tier-behavior",
    )

    assert [item.tier.value for item in receipt.attestations] == ["local", "edge", "cloud"]
    assert len({item.lease_id for item in receipt.attestations}) == 3
    assert all(item.handshake_ok and item.heartbeat_ok for item in receipt.attestations)
    assert all(item.task_success and item.artifact_ids for item in receipt.attestations)
    assert Path(receipt.receipt_path).is_file()
    cloud = receipt.attestations[-1]
    assert cloud.metadata["worker_location"] == "cloud"
    assert cloud.metadata["receipt_id"]

    tier_gate, _provider_gate, _combined = LiveEvidenceSuite().evaluate(
        tier_observations=receipt.attestations,
        provider_observations=(provider.attestation,),
        final_completion=True,
    )
    assert tier_gate.status.value == "passed"
    assert not tier_gate.findings


def test_persisted_managed_live_receipt_reloads_traces_and_revalidates_gates(
    tmp_path: Path,
) -> None:
    try:
        edge_host = discover_non_loopback_ipv4()
    except ExecutionTierError as error:
        pytest.skip(str(error))
    run_id = "m1-persisted-live-behavior"
    anthropic = _provider_receipt(
        provider_id="anthropic",
        model_id="claude-test",
        dialect="anthropic-compatible",
        cli_kind="claude",
    )
    openai = _provider_receipt()
    providers: list[ManagedProviderReceipt] = []
    for receipt in (anthropic, openai):
        trace_path = tmp_path / f"{receipt.attestation.provider_id}-trace.json"
        trace_path.write_text(
            json.dumps(
                {
                    "provider_id": receipt.attestation.provider_id,
                    "request_id": receipt.attestation.request_id,
                    "tool_call_ids": list(receipt.attestation.tool_call_ids),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        providers.append(
            ManagedProviderReceipt(
                attestation=receipt.attestation,
                trace_path=str(trace_path),
                trace_digest=hashlib.sha256(trace_path.read_bytes()).hexdigest(),
                command_id=receipt.command_id,
            )
        )
    tiers = ExecutionTierProbeSuite(Path(__file__).resolve().parents[2]).execute(
        artifact_root=tmp_path,
        cloud_provider=providers[1],
        edge_host=edge_host,
        run_id=run_id,
    )
    gates = LiveEvidenceSuite().evaluate(
        tier_observations=tiers.attestations,
        provider_observations=tuple(item.attestation for item in providers),
        final_completion=True,
    )
    assert all(item.ok for item in gates)
    aggregate_path = tmp_path / "managed-live-evidence.json"
    payload = {
        "schema": "zyra.managed-live-evidence/v1",
        "run_id": run_id,
        "started_at": utc_now(),
        "completed_at": utc_now(),
        "providers": [item.to_dict() for item in providers],
        "execution_tiers": tiers.to_dict(),
        "gates": [item.to_dict() for item in gates],
        "receipt_path": str(aggregate_path),
    }
    aggregate_path.write_text(
        json.dumps(
            {**payload, "content_digest": stable_digest(payload)},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )

    reloaded = ManagedLiveEvidenceRuntime.load_receipt(aggregate_path)

    assert reloaded.accepted
    assert reloaded.run_id == run_id
    assert [item.attestation.provider_id for item in reloaded.providers] == [
        "anthropic",
        "openai",
    ]
    assert all(
        item.attestation.metadata["auth_custodian"] == "provider_cli"
        for item in reloaded.providers
    )
    assert [item.tier.value for item in reloaded.execution_tiers.attestations] == [
        "local",
        "edge",
        "cloud",
    ]
