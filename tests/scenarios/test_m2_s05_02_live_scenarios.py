from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from zyra_evaluation.scenario_runner import (
    DualDomainOwnerBindings,
    DualDomainScenarioExecutor,
    ScenarioRegistry,
    build_configuration,
)
from zyra_evaluation.scenario_runner.canonical import digest, utc_now
from zyra_evaluation.scenario_runner.dual_domain import compare_live_domains
from zyra_evaluation.scenario_runner.errors import ScenarioRunnerError
from zyra_evaluation.scenario_runner.live_models import LiveDomain
from zyra_evaluation.scenario_runner.research_delivery import (
    LiveHttpSourceAcquirer,
    SourceAcquisition,
)


ROOT = Path(__file__).resolve().parents[2]


class LiveOwnerHarness:
    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root.resolve()
        self.run_id = "run-live-owner"
        self.task_id = "task-live-owner"
        self.route_counter = 0
        self.checkpoint_counter = 0
        self.event_batches: list[tuple[dict[str, Any], ...]] = []
        self.published: list[dict[str, Any]] = []

    def begin(self, **_: Any) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "root_node_id": "root-live-owner",
            "started_at": utc_now(),
        }

    def commit_events(
        self,
        *,
        events: Any,
        **_: Any,
    ) -> tuple[dict[str, Any], ...]:
        selected = tuple(dict(item) for item in events)
        self.event_batches.append(selected)
        return selected

    def curate_memory(self, **_: Any) -> dict[str, Any]:
        return {
            "receipt_id": "memory-live-owner",
            "event_id": "memory-live-owner-event",
            "memory_id": "memory-live-owner-record",
            "worker_id": "MemoryCuratorRuntime",
            "state": "committed",
            "content_digest": "a" * 64,
        }

    def finalize(
        self,
        *,
        success: bool,
        summary: str,
        artifacts: Any,
        events: Any,
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "root_node_id": "root-live-owner",
            "status": "completed" if success else "failed",
            "summary": summary,
            "artifacts": list(artifacts),
            "event_count": len(events),
        }

    def publish(
        self,
        *,
        source_paths: Any,
        domain: LiveDomain,
        metadata: Any,
        **_: Any,
    ) -> tuple[dict[str, Any], ...]:
        output: list[dict[str, Any]] = []
        for raw in source_paths:
            source = Path(raw).resolve()
            assert source.is_relative_to(self.artifact_root)
            value = {
                "artifact_id": (
                    f"artifact-{len(self.published) + len(output) + 1:04d}"
                ),
                "kind": "structured_data",
                "uri": str(source),
                "path": str(source),
                "title": source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "metadata": {
                    **dict(metadata),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "domain": domain.value,
                },
            }
            output.append(value)
        self.published.extend(output)
        return tuple(output)

    def acquire_route(self, **_: Any) -> dict[str, Any]:
        return self._route("initial")

    def migrate_route(self, *, reason: str, **_: Any) -> dict[str, Any]:
        return self._route(reason)

    def execute_tiers(self, **_: Any) -> tuple[dict[str, Any], ...]:
        now = utc_now()
        values = []
        for index, tier in enumerate(("device", "edge", "cloud"), start=1):
            values.append(
                {
                    "observation_id": f"tier-{tier}",
                    "tier": tier,
                    "endpoint": (
                        f"local://device/{index}"
                        if tier == "device"
                        else f"https://203.0.113.{index}/tier"
                    ),
                    "endpoint_id": f"endpoint-{tier}",
                    "runtime_id": f"runtime-{tier}",
                    "process_id": f"process-{tier}",
                    "isolation_id": f"isolation-{tier}",
                    "request_id": f"request-{tier}",
                    "route_id": f"route-{tier}",
                    "lease_id": f"lease-{tier}",
                    "artifact_ids": [f"artifact-{tier}"],
                    "started_at": now,
                    "completed_at": now,
                    "request_digest": digest(("request", tier)),
                    "response_digest": digest(("response", tier)),
                    "handshake_ok": True,
                    "heartbeat_ok": True,
                    "task_success": True,
                    "simulated": False,
                    "loopback": False,
                    "metadata": {"test_owner": True},
                }
            )
        return tuple(values)

    def execute_providers(self, **_: Any) -> tuple[dict[str, Any], ...]:
        now = utc_now()
        values = []
        for index, model in enumerate(("model-alpha", "model-beta"), start=1):
            values.append(
                {
                    "observation_id": f"provider-{index}",
                    "provider_id": f"provider-{index}",
                    "model_id": model,
                    "endpoint": f"https://provider-{index}.example/v1",
                    "request_id": f"provider-request-{index}",
                    "attempt_id": f"provider-attempt-{index}",
                    "route_id": f"provider-route-{index}",
                    "credential_custodian": "test-provider-owner",
                    "authenticated": True,
                    "response_status": 200,
                    "request_digest": digest(("provider-request", index)),
                    "response_digest": digest(("provider-response", index)),
                    "tool_call_ids": [f"tool-{index}"],
                    "tool_result_ids": [f"tool-{index}"],
                    "started_at": now,
                    "completed_at": now,
                    "cost_usd": 0.01,
                    "latency_ms": 25,
                    "metadata": {"test_owner": True},
                }
            )
        return tuple(values)

    def disconnected_degradation(
        self,
        *,
        route: Any,
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "event_id": "edge-disconnected-test",
            "observed": True,
            "safe": True,
            "relabeled_as_cloud": False,
            "route_before": "route-edge",
            "route_after": route["route_id"],
        }

    def checkpoint(
        self,
        *,
        scenario_state: Any,
        **_: Any,
    ) -> dict[str, Any]:
        self.checkpoint_counter += 1
        return {
            "checkpoint_id": f"checkpoint-{self.checkpoint_counter}",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "state_digest": digest(scenario_state),
            "owner": "test-checkpoint-owner",
            "committed": True,
        }

    def inject(self, *, injection: Any, **_: Any) -> dict[str, Any]:
        return {
            "receipt_id": f"fault-{injection.injection_id}",
            "event_id": f"fault-event-{injection.injection_id}",
            "kind": injection.kind,
            "observed": True,
            "reason": f"observed {injection.injection_id}",
        }

    def recover(self, *, injection: Any, **_: Any) -> dict[str, Any]:
        return {
            "receipt_id": f"recovery-{injection.injection_id}",
            "event_ids": [f"recovery-event-{injection.injection_id}"],
            "state": "recovered",
            "checkpoint_restored": True,
            "reason": f"recovered {injection.injection_id}",
        }

    def migrate(
        self,
        *,
        injection: Any,
        recovery_receipt: Any,
        scenario_state: Any,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route(str(recovery_receipt["reason"]))
        before = str(scenario_state["route_id"])
        return {
            "receipt_id": route["receipt_id"],
            "lease_id": route["lease_id"],
            "route_before": before,
            "route_after": route["route_id"],
            "before": dict(scenario_state["route"]),
            "after": route,
        }

    def reverify(
        self,
        *,
        injection: Any,
        scenario_state: Any,
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "receipt_id": f"reverify-{injection.injection_id}",
            "event_id": f"reverify-event-{injection.injection_id}",
            "valid": True,
            "input_digest": scenario_state["input_digest"],
            "state_digest": digest(
                (injection.injection_id, scenario_state["plan_digest"])
            ),
        }

    def _route(self, reason: str) -> dict[str, Any]:
        self.route_counter += 1
        return {
            "route_id": f"route-{self.route_counter}",
            "lease_id": f"lease-{self.route_counter}",
            "worker_id": f"worker-{self.route_counter}",
            "receipt_id": f"route-receipt-{self.route_counter}",
            "provider_id": "provider-1",
            "tier": "device",
            "reason": reason,
            "simulated": False,
        }


def configuration(
    tmp_path: Path,
    scenario_id: str,
    input_text: str,
) -> Any:
    return build_configuration(
        ScenarioRegistry.defaults(),
        {
            "scenario_id": scenario_id,
            "input": input_text,
            "mode": "sealed",
            "seed": 2502,
        },
        project_root=str(ROOT),
        default_preflight_paths={
            "database": str(tmp_path / "scenario.sqlite3"),
            "cache": str(tmp_path / "cache"),
            "index": str(tmp_path / "index"),
            "artifact": str(tmp_path / "preflight-artifacts"),
            "build": str(tmp_path / "build"),
        },
    )


def execute(
    tmp_path: Path,
    *,
    scenario_id: str,
    input_text: str,
) -> Any:
    artifact_root = tmp_path / "artifacts"
    owner = LiveOwnerHarness(artifact_root)
    selected = configuration(tmp_path, scenario_id, input_text)
    return DualDomainScenarioExecutor(
        project_root=ROOT,
        artifact_root=artifact_root,
        scratch_root=tmp_path / "scratch",
        bindings=DualDomainOwnerBindings(
            task=owner,
            artifact=owner,
            placement=owner,
            fault=owner,
            source_commit="a" * 40,
        ),
    ).execute(
        scenario_run_id=f"scenario-{scenario_id.replace('.', '-')}",
        configuration=selected,
        goal=input_text,
        policy_decisions=(),
        cancel_requested=lambda: False,
    )


def test_software_delivery_produces_real_patch_tests_and_two_thousand_events(
    tmp_path: Path,
) -> None:
    result = execute(
        tmp_path,
        scenario_id="live.software-delivery",
        input_text=(
            "Add a deterministic delivery marker to a clean project, verify the "
            "new behavior with executable tests, retain requirement-change "
            "evidence, and publish a checksum-bound patch and report."
        ),
    )
    summary = result.task["live_domain"]
    assert summary["domain"] == "software_delivery"
    assert summary["effective_transition_count"] >= 2_000
    assert summary["fault_count"] >= 5
    assert summary["recovered_fault_count"] == summary["fault_count"]
    assert result.task["domain_verification"]["valid"] is True
    assert result.task["placement"]["verification"]["valid"] is True
    assert result.task["causal_archive"]["manifest_digest"]
    assert any(
        Path(item["uri"]).suffix == ".patch" for item in result.artifacts
    )
    effects = {
        str((item.get("metadata") or {}).get("semantic_effect") or "")
        for item in result.events
    }
    assert {
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
    }.issubset(effects)


def test_research_delivery_binds_claims_to_exact_acquired_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def acquire_live_test_sources(
        acquirer: LiveHttpSourceAcquirer,
        urls: Any,
        *,
        cancel_requested: Any = None,
    ) -> tuple[SourceAcquisition, ...]:
        assert cancel_requested is None or cancel_requested() is False
        values: list[SourceAcquisition] = []
        for index, url in enumerate(urls, start=1):
            text = (
                "HTTP semantics define status codes and deterministic message "
                "processing requirements. Independent authorities document "
                "method safety, retry behavior, cache constraints, transport "
                "failure recovery, and verification of acquired source bytes. "
                f"This source has independent ordinal {index} and must retain "
                "citation offsets, request identity, and checksum evidence.\n"
            )
            acquired = acquirer.artifact_root / f"source-{index:02d}.txt"
            wire = acquirer.artifact_root / f"source-{index:02d}.wire"
            metadata = acquirer.artifact_root / f"source-{index:02d}.json"
            encoded = text.encode("utf-8")
            acquired.write_bytes(encoded)
            wire.write_bytes(encoded)
            metadata.write_text(
                '{"schema":"zyra.live-source-test/v1"}',
                encoding="utf-8",
            )
            checksum = hashlib.sha256(encoded).hexdigest()
            authority = url.split("/", 3)[2]
            values.append(
                SourceAcquisition(
                    source_id=f"source-{index:02d}",
                    requested_url=url,
                    url=url,
                    authority=authority,
                    status=200,
                    media_type="text/plain",
                    charset="utf-8",
                    content_encoding="identity",
                    request_id=f"request-{index:02d}",
                    acquired_at=utc_now(),
                    elapsed_ms=index,
                    response_headers={"content-type": "text/plain"},
                    source_digest=checksum,
                    wire_digest=checksum,
                    acquired_path=str(acquired),
                    wire_path=str(wire),
                    byte_count=len(encoded),
                    text_byte_count=len(encoded),
                    redirect_chain=(),
                    live=True,
                    replay=False,
                    metadata={"metadata_path": str(metadata), "test_transport": True},
                )
            )
        return tuple(values)

    monkeypatch.setattr(
        LiveHttpSourceAcquirer,
        "acquire_all",
        acquire_live_test_sources,
    )
    result = execute(
        tmp_path,
        scenario_id="live.cross-source-research",
        input_text=(
            "Compare independent HTTP authorities on status codes, retries, "
            "cache constraints, transport failure recovery, and exact citation "
            "verification from new live source bytes."
        ),
    )
    summary = result.task["live_domain"]
    verification = result.task["domain_verification"]
    assert summary["domain"] == "cross_source_research"
    assert summary["effective_transition_count"] >= 2_000
    assert summary["fault_count"] >= 5
    assert summary["recovered_fault_count"] == summary["fault_count"]
    assert verification["valid"] is True
    assert verification["checks"]["citation_integrity"] is True
    assert verification["checks"]["authority_diversity"] is True
    assert verification["checks"]["report_input_bound"] is True
    assert len(result.task["placement"]["migrated_routes"]) == 3
    assert result.task["causal_archive"]["manifest_digest"]


def test_research_public_egress_proxy_requires_explicit_bounded_cidr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquirer = LiveHttpSourceAcquirer(artifact_root=tmp_path / "sources")
    monkeypatch.setattr(
        acquirer,
        "_resolve_addresses",
        lambda host: ["198.18.0.144"],
    )
    with pytest.raises(ScenarioRunnerError) as blocked:
        acquirer._validated_url("https://public.example/standards")
    assert blocked.value.code == "research_private_address_forbidden"

    monkeypatch.setenv("ZYRA_LIVE_PUBLIC_PROXY_CIDRS", "198.18.0.0/15")
    assert (
        acquirer._validated_url("https://public.example/standards")
        == "https://public.example/standards"
    )
    with pytest.raises(ScenarioRunnerError) as literal:
        acquirer._validated_url("https://198.18.0.144/standards")
    assert literal.value.code == "research_private_address_forbidden"


@pytest.mark.parametrize(
    "environment_name",
    (
        "ZYRA_WORKER_POOL_INTEGRATION_DISABLED",
        "ZYRA_MEMORY_RETRIEVAL_DISABLED",
        "ZYRA_DISABLE_RECOVERY_RUNTIME",
        "ZYRA_TARGETED_COMMUNICATION_DISABLED",
        "ZYRA_SCENARIO_DOMAIN_VERIFIER_DISABLED",
    ),
)
def test_formal_live_components_have_no_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
) -> None:
    monkeypatch.setenv(environment_name, "1")
    with pytest.raises(ScenarioRunnerError):
        execute(
            tmp_path,
            scenario_id="live.software-delivery",
            input_text=(
                "Implement a deterministic change with executable tests and "
                "checksum-bound delivery evidence under a sealed policy."
            ),
        )


def test_cross_domain_comparison_rejects_same_domain(
    tmp_path: Path,
) -> None:
    first = execute(
        tmp_path / "left",
        scenario_id="live.software-delivery",
        input_text=(
            "Implement one deterministic change with executable tests and "
            "produce a patch, report, and causal evidence archive."
        ),
    )
    second = execute(
        tmp_path / "right",
        scenario_id="live.software-delivery",
        input_text=(
            "Implement another deterministic change with executable tests and "
            "produce a patch, report, and causal evidence archive."
        ),
    )
    comparison = compare_live_domains(first, second)
    assert comparison["dual_domain_complete"] is False
    assert comparison["both_verified"] is True
    assert comparison["combined_effective_transition_count"] >= 4_000
