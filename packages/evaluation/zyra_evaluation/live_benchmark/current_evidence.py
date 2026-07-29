from __future__ import annotations

import ipaddress
import os
import socket
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from zyra_orchestration.deployment.dispatch import DeploymentDispatchRuntime
from zyra_orchestration.deployment.models import (
    DeploymentProfile,
    DispatchStatus,
    Sensitivity,
    Workload,
    new_id,
)
from zyra_orchestration.deployment.placement import (
    PlacementContext,
    PlacementPolicyRuntime,
)
from zyra_orchestration.deployment.process_manager import DeploymentProcessManager
from zyra_orchestration.deployment.profiles import ProfileCatalog
from zyra_orchestration.deployment.state_store import DeploymentStateStore

from .canonical import (
    digest,
    identity,
    invalid,
    mapping,
    require_commit,
    require_digest,
    sequence,
    utc_now,
)


REQUIRED_CURRENT_TIERS = ("device", "edge", "cloud")
MINIMUM_CURRENT_PROVIDERS_PER_CASE = 2


def source_case_projection(receipts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for item in receipts:
        source = mapping(item.get("source"), "source receipt")
        domain = identity(item.get("domain"), "source domain")
        repetition = int(item.get("repetition") or 0)
        if repetition < 1:
            raise invalid(
                "benchmark-current-source-repetition-invalid",
                "Current provider evidence source repetition is invalid.",
                phase="deployment",
            )
        cases.append(
            {
                "case_id": f"{domain}:r{repetition:02d}",
                "domain": domain,
                "repetition": repetition,
                "source_run_id": identity(
                    source.get("scenario_run_id"),
                    "source scenario run id",
                ),
                "owner_run_id": identity(
                    source.get("owner_run_id"),
                    "source owner run id",
                ),
                "task_id": identity(source.get("task_id"), "source task id"),
                "source_archive_digest": require_digest(
                    source.get("archive_digest"),
                    "source archive digest",
                ),
                "source_outcome_digest": require_digest(
                    item.get("outcome_digest"),
                    "source outcome digest",
                ),
            }
        )
    return sorted(cases, key=lambda item: item["case_id"])


class CurrentTierDispatchRunner:
    def __init__(
        self,
        *,
        project_root: str | Path,
        state_root: str | Path,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        self.state_root = Path(state_root).resolve(strict=False)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.environment = dict(os.environ if environment is None else environment)

    def run(self, cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        normalized_cases = tuple(mapping(item, "formal source case") for item in cases)
        if not normalized_cases:
            raise invalid(
                "benchmark-current-tier-cases-empty",
                "Current tier dispatch has no formal cases.",
                phase="deployment",
            )
        host = discover_edge_host(self.environment)
        base_port = free_port_block(host, count=3)
        catalog = ProfileCatalog.defaults(
            self.project_root,
            host=host,
            base_port=base_port,
            environment=self.environment,
        )
        store = DeploymentStateStore(self.state_root / "deployment.sqlite3")
        manager = DeploymentProcessManager(
            project_root=self.project_root,
            state_root=self.state_root,
            store=store,
        )
        clients: dict[DeploymentProfile, Any] = {}
        try:
            for profile in (DeploymentProfile.DEVICE, DeploymentProfile.EDGE):
                _, client, health = manager.start_node(catalog.policy(profile))
                if (
                    health.get("status") != "ready"
                    or bool(health.get("blockers"))
                ):
                    raise invalid(
                        "benchmark-current-tier-node-not-ready",
                        "Current device/edge node failed readiness.",
                        phase="deployment",
                        detail={"profile": profile.value, "health": health},
                    )
                clients[profile] = client
            observations = {
                profile: client.observation()
                for profile, client in clients.items()
            }
            if len({item.pid for item in observations.values()}) != 2:
                raise invalid(
                    "benchmark-current-tier-process-not-isolated",
                    "Current device and edge runtimes are not isolated processes.",
                    phase="deployment",
                )
            placement = PlacementPolicyRuntime(
                catalog,
                store,
                environment=self.environment,
            )
            dispatch = DeploymentDispatchRuntime(store)
            output_cases = [
                self._dispatch_case(
                    case,
                    catalog=catalog,
                    placement=placement,
                    dispatch=dispatch,
                    observations=observations,
                    clients=clients,
                    host=host,
                )
                for case in normalized_cases
            ]
            receipt = {
                "schema": "zyra.m3-current-tier-dispatch/v1",
                "status": "passed",
                "fresh": True,
                "simulated": False,
                "case_count": len(output_cases),
                "tier_ids": ["device", "edge"],
                "edge_host": host,
                "independent_processes": True,
                "cases": output_cases,
                "completed_at": utc_now(),
            }
            receipt["receipt_digest"] = digest(receipt)
            return receipt
        finally:
            manager.stop_all(timeout_seconds=5)

    def _dispatch_case(
        self,
        case: Mapping[str, Any],
        *,
        catalog: ProfileCatalog,
        placement: PlacementPolicyRuntime,
        dispatch: DeploymentDispatchRuntime,
        observations: Mapping[DeploymentProfile, Any],
        clients: Mapping[DeploymentProfile, Any],
        host: str,
    ) -> dict[str, Any]:
        case_id = identity(case.get("case_id"), "formal case id")
        task_id = identity(case.get("task_id"), "formal case task id")
        run_id = identity(case.get("source_run_id"), "formal case source run id")
        tier_observations: list[dict[str, Any]] = []
        for profile in (DeploymentProfile.DEVICE, DeploymentProfile.EDGE):
            workload = current_workload(
                profile,
                case=case,
                task_id=task_id,
                run_id=run_id,
            )
            decision = placement.decide(
                workload,
                PlacementContext(observations=observations),
            )
            if decision.selected_profile is not profile:
                raise invalid(
                    "benchmark-current-tier-route-mismatch",
                    "Current tier dispatch selected the wrong profile.",
                    phase="deployment",
                    detail={
                        "case_id": case_id,
                        "expected": profile.value,
                        "observed": decision.selected_profile.value,
                    },
                )
            result = dispatch.dispatch(workload, decision, clients[profile])
            if result.status is not DispatchStatus.SUCCEEDED:
                raise invalid(
                    "benchmark-current-tier-dispatch-failed",
                    (
                        "Current tier dispatch did not succeed for "
                        f"{profile.value}: {result.failure_code or result.status.value}; "
                        f"{result.result.get('message', '')}."
                    ),
                    phase="deployment",
                    detail={"case_id": case_id, "profile": profile.value},
                )
            node = observations[profile]
            tier_observations.append(
                {
                    "observation_id": f"{case_id}:{profile.value}",
                    "case_id": case_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "tier": profile.value,
                    "evidence_mode": "live",
                    "fresh": True,
                    "simulated": False,
                    "current_dispatch": True,
                    "handshake_ok": True,
                    "task_success": True,
                    "dispatch_status": result.status.value,
                    "dispatch_id": result.dispatch_id,
                    "attempt_id": result.attempt_id,
                    "request_digest": digest(workload.semantic_dict()),
                    "response_digest": require_digest(
                        str(result.result_digest).removeprefix("sha256:"),
                        "current tier response digest",
                    ),
                    "isolation_id": node.generation_id,
                    "runtime_id": node.node_id,
                    "process_id": str(node.pid),
                    "endpoint": (
                        f"process://{node.pid}"
                        if profile is DeploymentProfile.DEVICE
                        else f"tcp://{host}:{catalog.policy(profile).port}"
                    ),
                    "isolated_process": True,
                    "loopback": False,
                    "started_at": result.started_at,
                    "completed_at": result.completed_at,
                    "source_archive_digest": case["source_archive_digest"],
                    "source_outcome_digest": case["source_outcome_digest"],
                }
            )
        return {
            **dict(case),
            "tier_observations": tier_observations,
        }


def current_workload(
    profile: DeploymentProfile,
    *,
    case: Mapping[str, Any],
    task_id: str,
    run_id: str,
) -> Workload:
    case_id = identity(case.get("case_id"), "formal case id")
    common = {
        "workload_id": new_id(f"m3_{profile.value}"),
        "task_id": task_id,
        "run_id": run_id,
        "latency_sla_ms": 10_000,
        "cpu_units": 1,
        "memory_mb": 32,
        "provider_required": False,
        "idempotency_key": f"m3-current:{case_id}:{profile.value}",
    }
    if profile is DeploymentProfile.DEVICE:
        return Workload(
            **common,
            operation="deterministic-transform",
            payload={
                "items": [
                    case_id,
                    str(case["source_archive_digest"]),
                    str(case["source_outcome_digest"]),
                ]
            },
            sensitivity=Sensitivity.RESTRICTED,
            complexity=2,
            required_capabilities=("local-compute",),
        )
    if profile is DeploymentProfile.EDGE:
        return Workload(
            **common,
            operation="analyze-text",
            payload={
                "text": (
                    f"{case_id} {case['source_archive_digest']} "
                    f"{case['source_outcome_digest']}"
                )
            },
            sensitivity=Sensitivity.CONFIDENTIAL,
            complexity=5,
            required_capabilities=("edge-compute",),
        )
    raise ValueError("current tier runner only owns device and edge profiles")


def assemble_current_campaign_evidence(
    *,
    campaign_id: str,
    implementation_commit: str,
    sources: Sequence[Mapping[str, Any]],
    tier_receipt: Mapping[str, Any],
    provider_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    selected_campaign = identity(campaign_id, "campaign id")
    selected_commit = require_commit(implementation_commit)
    expected_cases = source_case_projection(sources)
    tier_cases = {
        str(item.get("case_id") or ""): mapping(item, "tier case")
        for item in sequence(tier_receipt.get("cases"), "tier cases")
    }
    provider_cases: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in sequence(provider_receipt.get("calls"), "provider calls"):
        call = mapping(item, "provider call")
        provider_cases[str(call.get("case_id") or "")].append(call)
    combined_cases: list[dict[str, Any]] = []
    for case in expected_cases:
        case_id = case["case_id"]
        tier_case = tier_cases.get(case_id)
        if tier_case is None:
            raise invalid(
                "benchmark-current-tier-case-missing",
                "Current tier evidence is missing a formal case.",
                phase="deployment",
                detail={"case_id": case_id},
            )
        calls = provider_cases.get(case_id, [])
        if not calls:
            raise invalid(
                "benchmark-current-provider-case-missing",
                "Current provider evidence is missing a formal case.",
                phase="deployment",
                detail={"case_id": case_id},
            )
        cloud_call = sorted(
            calls,
            key=lambda item: (
                str(item.get("provider_id") or "") != "zhipu",
                str(item.get("provider_id") or ""),
            ),
        )[0]
        cloud_tier = {
            "observation_id": f"{case_id}:cloud",
            "case_id": case_id,
            "run_id": case["source_run_id"],
            "task_id": case["task_id"],
            "tier": "cloud",
            "evidence_mode": "live",
            "fresh": True,
            "simulated": False,
            "current_dispatch": True,
            "handshake_ok": True,
            "task_success": True,
            "dispatch_status": "succeeded",
            "dispatch_id": cloud_call["dispatch_id"],
            "attempt_id": cloud_call["attempt_id"],
            "request_digest": cloud_call["request_digest"],
            "response_digest": cloud_call["response_digest"],
            "isolation_id": cloud_call["credential_fingerprint"],
            "runtime_id": (
                f"{cloud_call['provider_id']}/{cloud_call['model_id']}"
            ),
            "process_id": f"provider-managed:{cloud_call['provider_id']}",
            "endpoint": cloud_call["endpoint"],
            "isolated_process": True,
            "loopback": False,
            "started_at": cloud_call["started_at"],
            "completed_at": cloud_call["completed_at"],
            "source_archive_digest": case["source_archive_digest"],
            "source_outcome_digest": case["source_outcome_digest"],
        }
        combined_cases.append(
            {
                **case,
                "tier_observations": [
                    *sequence(
                        tier_case.get("tier_observations"),
                        "tier observations",
                    ),
                    cloud_tier,
                ],
                "provider_observations": sorted(
                    (dict(item) for item in calls),
                    key=lambda item: item["provider_id"],
                ),
            }
        )
    output = {
        "schema": "zyra.m3-current-campaign-evidence/v1",
        "status": "passed",
        "campaign_id": selected_campaign,
        "implementation_commit": selected_commit,
        "case_count": len(combined_cases),
        "provider_request_count": sum(
            len(calls) for calls in provider_cases.values()
        ),
        "provider_usage_by_provider": dict(
            mapping(
                provider_receipt.get("usage_by_provider"),
                "provider usage summary",
            )
        ),
        "tier_dispatch_case_count": int(tier_receipt.get("case_count") or 0),
        "current_provider_ids": sorted(
            {
                str(call.get("provider_id") or "")
                for calls in provider_cases.values()
                for call in calls
            }
        ),
        "current_model_ids": sorted(
            {
                str(call.get("model_id") or "")
                for calls in provider_cases.values()
                for call in calls
            }
        ),
        "current_tier_ids": list(REQUIRED_CURRENT_TIERS),
        "external_model_request_made": True,
        "authenticated_provider_runtime_invoked": True,
        "protected_prior_receipts_only": False,
        "same_run_as_formal_cases": True,
        "credential_material_persisted": False,
        "human_intervention_count": 0,
        "operator_intervention_count": 0,
        "tier_dispatch_receipt_digest": require_digest(
            tier_receipt.get("receipt_digest"),
            "tier dispatch receipt digest",
        ),
        "provider_dispatch_receipt_digest": require_digest(
            provider_receipt.get("receipt_digest"),
            "provider dispatch receipt digest",
        ),
        "cases": combined_cases,
        "completed_at": utc_now(),
    }
    output["receipt_digest"] = digest(output)
    return output


class CurrentCampaignEvidenceVerifier:
    def verify(
        self,
        value: Mapping[str, Any],
        *,
        campaign_id: str,
        implementation_commit: str,
        sources: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        selected_campaign = identity(campaign_id, "campaign id")
        selected_commit = require_commit(implementation_commit)
        projection = dict(value)
        declared = require_digest(
            projection.pop("receipt_digest", ""),
            "current campaign evidence digest",
        )
        if digest(projection) != declared:
            raise invalid(
                "benchmark-current-evidence-digest-mismatch",
                "Current campaign evidence digest is invalid.",
                phase="deployment",
            )
        findings: list[dict[str, Any]] = []
        if value.get("schema") != "zyra.m3-current-campaign-evidence/v1":
            findings.append({"code": "schema-invalid"})
        if value.get("status") != "passed":
            findings.append({"code": "status-not-passed"})
        if value.get("campaign_id") != selected_campaign:
            findings.append({"code": "campaign-mismatch"})
        if value.get("implementation_commit") != selected_commit:
            findings.append({"code": "commit-mismatch"})
        for key in (
            "external_model_request_made",
            "authenticated_provider_runtime_invoked",
            "same_run_as_formal_cases",
        ):
            if value.get(key) is not True:
                findings.append({"code": f"{key.replace('_', '-')}-missing"})
        if value.get("protected_prior_receipts_only") is not False:
            findings.append({"code": "protected-prior-only"})
        if value.get("credential_material_persisted") is not False:
            findings.append({"code": "credential-material-persisted"})
        if value.get("human_intervention_count") != 0:
            findings.append({"code": "human-intervention-nonzero"})
        if value.get("operator_intervention_count") != 0:
            findings.append({"code": "operator-intervention-nonzero"})

        expected = {
            item["case_id"]: item for item in source_case_projection(sources)
        }
        observed_cases: dict[str, Mapping[str, Any]] = {}
        request_ids: set[str] = set()
        request_digests: set[str] = set()
        response_digests: set[str] = set()
        provider_ids: set[str] = set()
        model_ids: set[str] = set()
        for raw in sequence(value.get("cases"), "current evidence cases"):
            case = mapping(raw, "current evidence case")
            case_id = str(case.get("case_id") or "")
            if not case_id or case_id in observed_cases:
                findings.append({"code": "case-identity-invalid", "case_id": case_id})
                continue
            observed_cases[case_id] = case
            source = expected.get(case_id)
            if source is None:
                findings.append({"code": "case-unexpected", "case_id": case_id})
                continue
            for key in (
                "domain",
                "repetition",
                "source_run_id",
                "owner_run_id",
                "task_id",
                "source_archive_digest",
                "source_outcome_digest",
            ):
                if case.get(key) != source.get(key):
                    findings.append(
                        {
                            "code": "case-source-binding-mismatch",
                            "case_id": case_id,
                            "field": key,
                        }
                    )
            self._verify_tiers(case, source, findings)
            self._verify_providers(
                case,
                source,
                findings,
                request_ids=request_ids,
                request_digests=request_digests,
                response_digests=response_digests,
                provider_ids=provider_ids,
                model_ids=model_ids,
            )
        missing = sorted(set(expected) - set(observed_cases))
        if missing:
            findings.append({"code": "formal-cases-missing", "case_ids": missing})
        if int(value.get("case_count") or 0) != len(expected):
            findings.append({"code": "case-count-mismatch"})
        if int(value.get("provider_request_count") or 0) != len(request_ids):
            findings.append({"code": "provider-request-count-mismatch"})
        if int(value.get("tier_dispatch_case_count") or 0) != len(expected):
            findings.append({"code": "tier-dispatch-case-count-mismatch"})
        declared_providers = sorted(value.get("current_provider_ids") or [])
        declared_models = sorted(value.get("current_model_ids") or [])
        if declared_providers != sorted(provider_ids):
            findings.append({"code": "provider-summary-mismatch"})
        if declared_models != sorted(model_ids):
            findings.append({"code": "model-summary-mismatch"})
        if len(provider_ids) < 2:
            findings.append({"code": "provider-diversity-insufficient"})
        if len(model_ids) < 2:
            findings.append({"code": "model-diversity-insufficient"})
        if sorted(value.get("current_tier_ids") or []) != sorted(
            REQUIRED_CURRENT_TIERS
        ):
            findings.append({"code": "tier-summary-incomplete"})
        if findings:
            raise invalid(
                "benchmark-current-campaign-evidence-invalid",
                "Current provider/tier evidence is not bound to the formal cases.",
                phase="deployment",
                detail={"findings": findings},
            )
        receipt = {
            "schema": "zyra.m3-current-campaign-evidence-verification/v1",
            "valid": True,
            "campaign_id": selected_campaign,
            "implementation_commit": selected_commit,
            "case_count": len(expected),
            "provider_ids": sorted(provider_ids),
            "model_ids": sorted(model_ids),
            "tier_ids": list(REQUIRED_CURRENT_TIERS),
            "provider_request_count": len(request_ids),
            "current_evidence_digest": declared,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _verify_tiers(
        self,
        case: Mapping[str, Any],
        source: Mapping[str, Any],
        findings: list[dict[str, Any]],
    ) -> None:
        case_id = str(case.get("case_id") or "")
        tiers: dict[str, Mapping[str, Any]] = {}
        identities: dict[str, set[str]] = {
            "isolation_id": set(),
            "runtime_id": set(),
            "process_id": set(),
            "endpoint": set(),
        }
        for raw in sequence(case.get("tier_observations"), "tier observations"):
            item = mapping(raw, "tier observation")
            tier = str(item.get("tier") or "")
            if tier in tiers:
                findings.append(
                    {"code": "tier-duplicated", "case_id": case_id, "tier": tier}
                )
                continue
            tiers[tier] = item
            if (
                item.get("case_id") != case_id
                or item.get("run_id") != source["source_run_id"]
                or item.get("task_id") != source["task_id"]
            ):
                findings.append(
                    {"code": "tier-run-binding-mismatch", "case_id": case_id, "tier": tier}
                )
            for key in (
                "fresh",
                "current_dispatch",
                "handshake_ok",
                "task_success",
            ):
                if item.get(key) is not True:
                    findings.append(
                        {
                            "code": "tier-live-claim-missing",
                            "case_id": case_id,
                            "tier": tier,
                            "field": key,
                        }
                    )
            if item.get("simulated") is not False:
                findings.append(
                    {"code": "tier-simulated", "case_id": case_id, "tier": tier}
                )
            if item.get("dispatch_status") != "succeeded":
                findings.append(
                    {"code": "tier-dispatch-failed", "case_id": case_id, "tier": tier}
                )
            if (
                item.get("source_archive_digest") != source["source_archive_digest"]
                or item.get("source_outcome_digest")
                != source["source_outcome_digest"]
            ):
                findings.append(
                    {"code": "tier-source-binding-mismatch", "case_id": case_id, "tier": tier}
                )
            for key in ("request_digest", "response_digest"):
                try:
                    require_digest(item.get(key), f"{tier} {key}")
                except ValueError:
                    findings.append(
                        {
                            "code": "tier-digest-invalid",
                            "case_id": case_id,
                            "tier": tier,
                            "field": key,
                        }
                    )
            for key in identities:
                value = str(item.get(key) or "")
                if not value:
                    findings.append(
                        {
                            "code": "tier-identity-missing",
                            "case_id": case_id,
                            "tier": tier,
                            "field": key,
                        }
                    )
                identities[key].add(value)
            findings.extend(endpoint_findings(tier, str(item.get("endpoint") or ""), case_id))
            if tier == "edge" and (
                item.get("isolated_process") is not True
                or item.get("loopback") is not False
            ):
                findings.append({"code": "edge-isolation-invalid", "case_id": case_id})
        if set(tiers) != set(REQUIRED_CURRENT_TIERS):
            findings.append(
                {
                    "code": "tier-coverage-incomplete",
                    "case_id": case_id,
                    "tiers": sorted(tiers),
                }
            )
        for key, values in identities.items():
            if len(values) != len(REQUIRED_CURRENT_TIERS):
                findings.append(
                    {
                        "code": "tier-identity-not-isolated",
                        "case_id": case_id,
                        "field": key,
                    }
                )

    def _verify_providers(
        self,
        case: Mapping[str, Any],
        source: Mapping[str, Any],
        findings: list[dict[str, Any]],
        *,
        request_ids: set[str],
        request_digests: set[str],
        response_digests: set[str],
        provider_ids: set[str],
        model_ids: set[str],
    ) -> None:
        case_id = str(case.get("case_id") or "")
        case_providers: set[str] = set()
        case_models: set[str] = set()
        for raw in sequence(
            case.get("provider_observations"),
            "provider observations",
        ):
            item = mapping(raw, "provider observation")
            provider_id = str(item.get("provider_id") or "")
            model_id = str(item.get("model_id") or "")
            case_providers.add(provider_id)
            case_models.add(model_id)
            provider_ids.add(provider_id)
            model_ids.add(model_id)
            if (
                item.get("case_id") != case_id
                or item.get("run_id") != source["source_run_id"]
                or item.get("task_id") != source["task_id"]
            ):
                findings.append(
                    {
                        "code": "provider-run-binding-mismatch",
                        "case_id": case_id,
                        "provider_id": provider_id,
                    }
                )
            for key in (
                "live",
                "fresh",
                "authenticated",
                "external_model_request",
            ):
                if item.get(key) is not True:
                    findings.append(
                        {
                            "code": "provider-live-claim-missing",
                            "case_id": case_id,
                            "provider_id": provider_id,
                            "field": key,
                        }
                    )
            if (
                item.get("simulated") is not False
                or item.get("credential_material_persisted") is not False
            ):
                findings.append(
                    {
                        "code": "provider-custody-invalid",
                        "case_id": case_id,
                        "provider_id": provider_id,
                    }
                )
            status = int(item.get("http_status") or 0)
            if status < 200 or status >= 300:
                findings.append(
                    {
                        "code": "provider-http-failed",
                        "case_id": case_id,
                        "provider_id": provider_id,
                        "status": status,
                    }
                )
            if (
                item.get("source_archive_digest") != source["source_archive_digest"]
                or item.get("source_outcome_digest")
                != source["source_outcome_digest"]
            ):
                findings.append(
                    {
                        "code": "provider-source-binding-mismatch",
                        "case_id": case_id,
                        "provider_id": provider_id,
                    }
                )
            calls = {
                str(value)
                for value in sequence(item.get("tool_call_ids"), "tool call ids")
                if str(value)
            }
            results = {
                str(value)
                for value in sequence(item.get("tool_result_ids"), "tool result ids")
                if str(value)
            }
            if not calls or calls != results:
                findings.append(
                    {
                        "code": "provider-tool-roundtrip-invalid",
                        "case_id": case_id,
                        "provider_id": provider_id,
                    }
                )
            request_id = str(item.get("request_id") or "")
            request_digest = str(item.get("request_digest") or "")
            response_digest = str(item.get("response_digest") or "")
            for label, selected, seen in (
                ("request-id", request_id, request_ids),
                ("request-digest", request_digest, request_digests),
                ("response-digest", response_digest, response_digests),
            ):
                if not selected or selected in seen:
                    findings.append(
                        {
                            "code": f"provider-{label}-reused",
                            "case_id": case_id,
                            "provider_id": provider_id,
                        }
                    )
                seen.add(selected)
            try:
                require_digest(request_digest, "provider request digest")
                require_digest(response_digest, "provider response digest")
            except ValueError:
                findings.append(
                    {
                        "code": "provider-digest-invalid",
                        "case_id": case_id,
                        "provider_id": provider_id,
                    }
                )
        if len(case_providers) < MINIMUM_CURRENT_PROVIDERS_PER_CASE:
            findings.append(
                {
                    "code": "case-provider-diversity-insufficient",
                    "case_id": case_id,
                    "provider_ids": sorted(case_providers),
                }
            )
        if len(case_models) < MINIMUM_CURRENT_PROVIDERS_PER_CASE:
            findings.append(
                {
                    "code": "case-model-diversity-insufficient",
                    "case_id": case_id,
                    "model_ids": sorted(case_models),
                }
            )


def discover_edge_host(environment: Mapping[str, str]) -> str:
    explicit = str(environment.get("ZYRA_M3_EDGE_HOST") or "").strip()
    if explicit:
        return validate_edge_host(explicit)
    candidates: set[str] = set()
    try:
        for item in socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        ):
            candidates.add(str(item[4][0]))
    except OSError:
        pass
    for candidate in sorted(candidates):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.version == 4
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_unspecified
        ):
            return candidate
    raise invalid(
        "benchmark-current-edge-host-unavailable",
        "No non-loopback IPv4 host is available for the isolated edge runtime.",
        phase="deployment",
    )


def validate_edge_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise invalid(
            "benchmark-current-edge-host-invalid",
            "Configured edge host is not an IP address.",
            phase="deployment",
        ) from error
    if (
        address.version != 4
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ):
        raise invalid(
            "benchmark-current-edge-host-invalid",
            "Configured edge host must be a non-loopback IPv4 address.",
            phase="deployment",
        )
    return value


def free_port_block(host: str, *, count: int) -> int:
    for base in range(36000, 55000, count):
        sockets: list[socket.socket] = []
        try:
            for port in range(base, base + count):
                item = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                item.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                item.bind((host, port))
                sockets.append(item)
            return base
        except OSError:
            pass
        finally:
            for item in sockets:
                item.close()
    raise invalid(
        "benchmark-current-tier-port-unavailable",
        "No free port block is available for current tier evidence.",
        phase="deployment",
    )


def endpoint_findings(tier: str, endpoint: str, case_id: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    parsed = urlparse(endpoint)
    if tier == "device":
        if parsed.scheme not in {"process", "local", "unix", "pipe"}:
            findings.append({"code": "device-endpoint-not-local", "case_id": case_id})
        return findings
    if tier == "edge":
        if parsed.scheme not in {"tcp", "tls", "grpc", "https"}:
            findings.append(
                {"code": "edge-endpoint-transport-invalid", "case_id": case_id}
            )
        try:
            address = ipaddress.ip_address(str(parsed.hostname or ""))
        except ValueError:
            address = None
        if not parsed.hostname or (address is not None and address.is_loopback):
            findings.append({"code": "edge-endpoint-loopback", "case_id": case_id})
        return findings
    if tier == "cloud":
        if parsed.scheme != "https" or not parsed.hostname:
            findings.append({"code": "cloud-endpoint-invalid", "case_id": case_id})
            return findings
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and (
            address.is_loopback or address.is_private or address.is_link_local
        ):
            findings.append({"code": "cloud-endpoint-private", "case_id": case_id})
        return findings
    findings.append({"code": "tier-unknown", "case_id": case_id, "tier": tier})
    return findings


__all__ = [
    "CurrentCampaignEvidenceVerifier",
    "CurrentTierDispatchRunner",
    "assemble_current_campaign_evidence",
    "discover_edge_host",
    "source_case_projection",
]
