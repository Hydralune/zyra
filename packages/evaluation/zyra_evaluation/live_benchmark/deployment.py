from __future__ import annotations

import ipaddress
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import urlparse
from typing import Any

from .canonical import (
    bounded_integer,
    bounded_text,
    digest,
    elapsed_ms,
    identity,
    invalid,
    mapping,
    require_digest,
    require_keys,
    sequence,
    utc_now,
)
from .models import EvidenceMode


REQUIRED_TIERS = ("device", "edge", "cloud")
REQUIRED_ROUTE_FACTS = (
    "route_id",
    "tier",
    "privacy_class",
    "sla_class",
    "provider_id",
    "model_id",
    "reason",
    "selected_at",
)


class DeploymentEvidenceVerifier:
    def verify(self, value: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
        selected_run_id = identity(run_id, "run id")
        require_keys(
            value,
            (
                "tier_observations",
                "provider_observations",
                "route_decisions",
                "failovers",
                "disconnect_degradation",
            ),
            "deployment receipt",
        )
        tiers = tuple(
            mapping(item, "tier observation")
            for item in sequence(
                value.get("tier_observations"),
                "tier observations",
            )
        )
        providers = tuple(
            mapping(item, "provider observation")
            for item in sequence(
                value.get("provider_observations"),
                "provider observations",
            )
        )
        routes = tuple(
            mapping(item, "route decision")
            for item in sequence(value.get("route_decisions"), "route decisions")
        )
        failovers = tuple(
            mapping(item, "failover")
            for item in sequence(value.get("failovers"), "failovers")
        )
        disconnect = mapping(
            value.get("disconnect_degradation"),
            "disconnect degradation",
        )
        tier_receipt = self._tiers(tiers, selected_run_id)
        provider_receipt = self._providers(providers, selected_run_id)
        route_receipt = self._routes(routes, tiers, providers, selected_run_id)
        failover_receipt = self._failovers(failovers, routes, selected_run_id)
        disconnect_receipt = self._disconnect(disconnect, selected_run_id)
        output = {
            "schema": "zyra.live-benchmark-deployment-verification/v1",
            "valid": True,
            "run_id": selected_run_id,
            "tier_receipt": tier_receipt,
            "provider_receipt": provider_receipt,
            "route_receipt": route_receipt,
            "failover_receipt": failover_receipt,
            "disconnect_receipt": disconnect_receipt,
            "deployment_digest": digest(value),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _tiers(
        self,
        observations: Sequence[Mapping[str, Any]],
        run_id: str,
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        tier_counts: Counter[str] = Counter()
        isolation_ids: dict[str, str] = {}
        runtime_ids: dict[str, str] = {}
        process_ids: dict[str, str] = {}
        endpoints: dict[str, str] = {}
        evidence_modes: Counter[str] = Counter()
        latencies: dict[str, list[int]] = defaultdict(list)
        for index, item in enumerate(observations):
            observation_id = identity(
                item.get("observation_id"),
                f"tier observation[{index}] id",
            )
            tier = bounded_text(
                item.get("tier"),
                f"tier observation {observation_id} tier",
                maximum_bytes=32,
            ).lower()
            if tier not in REQUIRED_TIERS:
                findings.append(
                    {
                        "code": "tier-unknown",
                        "observation_id": observation_id,
                        "tier": tier,
                    }
                )
                continue
            tier_counts[tier] += 1
            if item.get("run_id") not in {None, run_id}:
                findings.append(
                    {
                        "code": "tier-run-mismatch",
                        "observation_id": observation_id,
                    }
                )
            if item.get("simulated") is True:
                findings.append(
                    {
                        "code": "tier-simulated",
                        "observation_id": observation_id,
                    }
                )
            if item.get("handshake_ok") is not True:
                findings.append(
                    {
                        "code": "tier-handshake-failed",
                        "observation_id": observation_id,
                    }
                )
            if item.get("task_success") is not True:
                findings.append(
                    {
                        "code": "tier-task-failed",
                        "observation_id": observation_id,
                    }
                )
            mode = evidence_mode(item, f"tier observation {observation_id}")
            evidence_modes[mode.value] += 1
            self._validate_protected_evidence(item, mode, findings, observation_id)
            isolation_id = identity(
                item.get("isolation_id"),
                f"tier observation {observation_id} isolation id",
            )
            runtime_id = identity(
                item.get("runtime_id"),
                f"tier observation {observation_id} runtime id",
            )
            process_id = identity(
                item.get("process_id"),
                f"tier observation {observation_id} process id",
            )
            endpoint = bounded_text(
                item.get("endpoint"),
                f"tier observation {observation_id} endpoint",
                maximum_bytes=2048,
            )
            isolation_ids[tier] = isolation_id
            runtime_ids[tier] = runtime_id
            process_ids[tier] = process_id
            endpoints[tier] = endpoint
            findings.extend(validate_tier_endpoint(tier, endpoint, observation_id))
            latencies[tier].append(
                elapsed_ms(
                    item.get("started_at"),
                    item.get("completed_at"),
                    f"tier observation {observation_id}",
                )
            )
            require_digest(
                item.get("request_digest"),
                f"tier observation {observation_id} request digest",
            )
            require_digest(
                item.get("response_digest"),
                f"tier observation {observation_id} response digest",
            )
        missing = sorted(set(REQUIRED_TIERS) - set(tier_counts))
        if missing:
            findings.append({"code": "tier-missing", "tiers": missing})
        for label, values in (
            ("isolation", isolation_ids),
            ("runtime", runtime_ids),
            ("process", process_ids),
            ("endpoint", endpoints),
        ):
            if len(values) == len(REQUIRED_TIERS) and len(set(values.values())) != len(
                REQUIRED_TIERS
            ):
                findings.append({"code": f"tier-{label}-not-isolated"})
        if findings:
            raise invalid(
                "benchmark_tier_evidence_invalid",
                "Device, edge and cloud deployment evidence is incomplete.",
                phase="deployment",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-tier-verification/v1",
            "valid": True,
            "tiers": list(REQUIRED_TIERS),
            "tier_counts": dict(sorted(tier_counts.items())),
            "isolation_ids": dict(sorted(isolation_ids.items())),
            "runtime_ids": dict(sorted(runtime_ids.items())),
            "process_ids": dict(sorted(process_ids.items())),
            "endpoint_digests": {
                key: digest(value) for key, value in sorted(endpoints.items())
            },
            "evidence_modes": dict(sorted(evidence_modes.items())),
            "latency_ms": {
                key: {
                    "minimum": min(values),
                    "maximum": max(values),
                    "count": len(values),
                }
                for key, values in sorted(latencies.items())
            },
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _providers(
        self,
        observations: Sequence[Mapping[str, Any]],
        run_id: str,
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        provider_ids: set[str] = set()
        model_ids: set[str] = set()
        provider_models: dict[str, set[str]] = defaultdict(set)
        request_ids: set[str] = set()
        response_digests: set[str] = set()
        modes: Counter[str] = Counter()
        total_cost = 0.0
        latencies: list[int] = []
        for index, item in enumerate(observations):
            observation_id = identity(
                item.get("observation_id"),
                f"provider observation[{index}] id",
            )
            provider_id = identity(
                item.get("provider_id"),
                f"provider observation {observation_id} provider id",
            )
            model_id = identity(
                item.get("model_id"),
                f"provider observation {observation_id} model id",
            )
            request_id = identity(
                item.get("request_id"),
                f"provider observation {observation_id} request id",
            )
            response_digest = require_digest(
                item.get("response_digest"),
                f"provider observation {observation_id} response digest",
            )
            require_digest(
                item.get("request_digest"),
                f"provider observation {observation_id} request digest",
            )
            mode = evidence_mode(item, f"provider observation {observation_id}")
            modes[mode.value] += 1
            self._validate_protected_evidence(item, mode, findings, observation_id)
            if item.get("run_id") not in {None, run_id}:
                findings.append(
                    {
                        "code": "provider-run-mismatch",
                        "observation_id": observation_id,
                    }
                )
            if item.get("simulated") is True:
                findings.append(
                    {
                        "code": "provider-simulated",
                        "observation_id": observation_id,
                    }
                )
            if item.get("authenticated") is not True:
                findings.append(
                    {
                        "code": "provider-not-authenticated",
                        "observation_id": observation_id,
                    }
                )
            status = bounded_integer(
                item.get("response_status"),
                f"provider observation {observation_id} status",
                minimum=100,
                maximum=599,
            )
            if status < 200 or status >= 300:
                findings.append(
                    {
                        "code": "provider-response-failed",
                        "observation_id": observation_id,
                        "status": status,
                    }
                )
            tool_calls = {
                identity(value, "tool call id")
                for value in sequence(
                    item.get("tool_call_ids"),
                    f"provider observation {observation_id} tool call ids",
                )
            }
            tool_results = {
                identity(value, "tool result id")
                for value in sequence(
                    item.get("tool_result_ids"),
                    f"provider observation {observation_id} tool result ids",
                )
            }
            if not tool_calls or tool_calls != tool_results:
                findings.append(
                    {
                        "code": "provider-tool-roundtrip-invalid",
                        "observation_id": observation_id,
                    }
                )
            provider_ids.add(provider_id)
            model_ids.add(model_id)
            provider_models[provider_id].add(model_id)
            if request_id in request_ids:
                findings.append(
                    {
                        "code": "provider-request-reused",
                        "request_id": request_id,
                    }
                )
            request_ids.add(request_id)
            if response_digest in response_digests:
                findings.append(
                    {
                        "code": "provider-response-replayed",
                        "observation_id": observation_id,
                    }
                )
            response_digests.add(response_digest)
            latency = bounded_integer(
                item.get("latency_ms"),
                f"provider observation {observation_id} latency",
                minimum=0,
                maximum=7 * 24 * 60 * 60 * 1000,
            )
            latencies.append(latency)
            total_cost += float(item.get("cost_usd") or 0.0)
        if len(provider_ids) < 2:
            findings.append(
                {
                    "code": "provider-diversity-insufficient",
                    "provider_ids": sorted(provider_ids),
                }
            )
        if len(model_ids) < 2:
            findings.append(
                {
                    "code": "model-diversity-insufficient",
                    "model_ids": sorted(model_ids),
                }
            )
        if findings:
            raise invalid(
                "benchmark_provider_evidence_invalid",
                "Provider/model evidence is incomplete or replayed.",
                phase="deployment",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-provider-verification/v1",
            "valid": True,
            "provider_ids": sorted(provider_ids),
            "model_ids": sorted(model_ids),
            "provider_models": {
                key: sorted(values)
                for key, values in sorted(provider_models.items())
            },
            "request_count": len(request_ids),
            "evidence_modes": dict(sorted(modes.items())),
            "total_cost_usd": round(total_cost, 8),
            "latency_ms": {
                "minimum": min(latencies),
                "maximum": max(latencies),
                "count": len(latencies),
            },
            "provider_observation_digest": digest(observations),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _routes(
        self,
        routes: Sequence[Mapping[str, Any]],
        tiers: Sequence[Mapping[str, Any]],
        providers: Sequence[Mapping[str, Any]],
        run_id: str,
    ) -> dict[str, Any]:
        if not routes:
            raise invalid(
                "benchmark_routes_empty",
                "Deployment evidence contains no scheduler decisions.",
                phase="deployment",
            )
        known_tiers = {str(item.get("tier") or "") for item in tiers}
        known_providers = {str(item.get("provider_id") or "") for item in providers}
        known_models = {str(item.get("model_id") or "") for item in providers}
        findings: list[dict[str, Any]] = []
        route_ids: set[str] = set()
        privacy_counts: Counter[str] = Counter()
        sla_counts: Counter[str] = Counter()
        tier_counts: Counter[str] = Counter()
        model_counts: Counter[str] = Counter()
        for index, route in enumerate(routes):
            require_keys(route, REQUIRED_ROUTE_FACTS, f"route decision[{index}]")
            route_id = identity(route.get("route_id"), f"route[{index}] id")
            if route_id in route_ids:
                findings.append(
                    {"code": "route-id-duplicate", "route_id": route_id}
                )
            route_ids.add(route_id)
            if route.get("run_id") not in {None, run_id}:
                findings.append(
                    {"code": "route-run-mismatch", "route_id": route_id}
                )
            tier = str(route.get("tier") or "")
            provider = str(route.get("provider_id") or "")
            model = str(route.get("model_id") or "")
            privacy = str(route.get("privacy_class") or "").lower()
            sla = str(route.get("sla_class") or "").lower()
            if tier not in known_tiers:
                findings.append(
                    {
                        "code": "route-tier-unobserved",
                        "route_id": route_id,
                        "tier": tier,
                    }
                )
            if provider not in known_providers:
                findings.append(
                    {
                        "code": "route-provider-unobserved",
                        "route_id": route_id,
                        "provider_id": provider,
                    }
                )
            if model not in known_models:
                findings.append(
                    {
                        "code": "route-model-unobserved",
                        "route_id": route_id,
                        "model_id": model,
                    }
                )
            if privacy not in {"public", "internal", "confidential", "restricted"}:
                findings.append(
                    {
                        "code": "route-privacy-invalid",
                        "route_id": route_id,
                        "privacy_class": privacy,
                    }
                )
            if sla not in {"interactive", "standard", "batch", "recovery"}:
                findings.append(
                    {
                        "code": "route-sla-invalid",
                        "route_id": route_id,
                        "sla_class": sla,
                    }
                )
            if route.get("privacy_compliant") is not True:
                findings.append(
                    {"code": "route-privacy-failed", "route_id": route_id}
                )
            if route.get("sla_compliant") is not True:
                findings.append(
                    {"code": "route-sla-failed", "route_id": route_id}
                )
            if privacy in {"confidential", "restricted"} and tier == "cloud":
                if route.get("redacted_before_cloud") is not True:
                    findings.append(
                        {
                            "code": "sensitive-cloud-route-unredacted",
                            "route_id": route_id,
                        }
                    )
            privacy_counts[privacy] += 1
            sla_counts[sla] += 1
            tier_counts[tier] += 1
            model_counts[model] += 1
        if len(tier_counts) < 2:
            findings.append({"code": "route-tier-split-missing"})
        if len(model_counts) < 2:
            findings.append({"code": "route-model-split-missing"})
        if findings:
            raise invalid(
                "benchmark_route_evidence_invalid",
                "Scheduler route evidence failed privacy, SLA or split validation.",
                phase="deployment",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-route-verification/v1",
            "valid": True,
            "route_count": len(routes),
            "privacy_counts": dict(sorted(privacy_counts.items())),
            "sla_counts": dict(sorted(sla_counts.items())),
            "tier_counts": dict(sorted(tier_counts.items())),
            "model_counts": dict(sorted(model_counts.items())),
            "route_digest": digest(routes),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _failovers(
        self,
        failovers: Sequence[Mapping[str, Any]],
        routes: Sequence[Mapping[str, Any]],
        run_id: str,
    ) -> dict[str, Any]:
        if not failovers:
            raise invalid(
                "benchmark_failover_evidence_empty",
                "Formal deployment evidence requires at least one failover.",
                phase="deployment",
            )
        known_routes = {str(item.get("route_id") or "") for item in routes}
        findings: list[dict[str, Any]] = []
        recovery_latencies: list[int] = []
        kinds: Counter[str] = Counter()
        for index, item in enumerate(failovers):
            failover_id = identity(item.get("failover_id"), f"failover[{index}] id")
            before = identity(
                item.get("route_before"),
                f"failover {failover_id} route before",
            )
            after = identity(
                item.get("route_after"),
                f"failover {failover_id} route after",
            )
            kind = bounded_text(
                item.get("reason"),
                f"failover {failover_id} reason",
                maximum_bytes=128,
            ).lower()
            if item.get("run_id") not in {None, run_id}:
                findings.append(
                    {"code": "failover-run-mismatch", "failover_id": failover_id}
                )
            if before == after:
                findings.append(
                    {"code": "failover-route-unchanged", "failover_id": failover_id}
                )
            if before not in known_routes or after not in known_routes:
                findings.append(
                    {"code": "failover-route-unobserved", "failover_id": failover_id}
                )
            if item.get("successful") is not True:
                findings.append(
                    {"code": "failover-unsuccessful", "failover_id": failover_id}
                )
            if item.get("delivery_resumed") is not True:
                findings.append(
                    {"code": "failover-delivery-not-resumed", "failover_id": failover_id}
                )
            recovery_latencies.append(
                elapsed_ms(
                    item.get("detected_at"),
                    item.get("resumed_at"),
                    f"failover {failover_id}",
                )
            )
            kinds[kind] += 1
        if findings:
            raise invalid(
                "benchmark_failover_evidence_invalid",
                "Placement/provider failover evidence is invalid.",
                phase="deployment",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-failover-verification/v1",
            "valid": True,
            "failover_count": len(failovers),
            "reason_counts": dict(sorted(kinds.items())),
            "recovery_latency_ms": {
                "minimum": min(recovery_latencies),
                "maximum": max(recovery_latencies),
            },
            "failover_digest": digest(failovers),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _disconnect(self, value: Mapping[str, Any], run_id: str) -> dict[str, Any]:
        require_keys(
            value,
            (
                "event_id",
                "observed",
                "safe",
                "route_before",
                "route_after",
                "delivery_resumed",
                "browser_required",
            ),
            "disconnect degradation",
        )
        identity(value.get("event_id"), "disconnect event id")
        findings: list[dict[str, Any]] = []
        if value.get("run_id") not in {None, run_id}:
            findings.append({"code": "disconnect-run-mismatch"})
        if value.get("observed") is not True:
            findings.append({"code": "disconnect-not-observed"})
        if value.get("safe") is not True:
            findings.append({"code": "disconnect-not-safe"})
        if value.get("delivery_resumed") is not True:
            findings.append({"code": "disconnect-delivery-not-resumed"})
        if value.get("browser_required") is True:
            findings.append({"code": "browser-required-for-degradation"})
        if value.get("relabeled_as_cloud") is True:
            findings.append({"code": "edge-relabeled-as-cloud"})
        before = identity(value.get("route_before"), "disconnect route before")
        after = identity(value.get("route_after"), "disconnect route after")
        if before == after:
            findings.append({"code": "disconnect-route-unchanged"})
        if findings:
            raise invalid(
                "benchmark_disconnect_degradation_invalid",
                "Edge/network disconnect did not safely degrade.",
                phase="deployment",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-disconnect-verification/v1",
            "valid": True,
            "event_id": value["event_id"],
            "route_before": before,
            "route_after": after,
            "browser_required": False,
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _validate_protected_evidence(
        self,
        item: Mapping[str, Any],
        mode: EvidenceMode,
        findings: list[dict[str, Any]],
        observation_id: str,
    ) -> None:
        if mode is EvidenceMode.LIVE:
            if item.get("fresh") is not True:
                findings.append(
                    {
                        "code": "live-observation-not-fresh",
                        "observation_id": observation_id,
                    }
                )
            return
        if mode is EvidenceMode.PROTECTED_PRIOR:
            receipt_id = str(item.get("prior_receipt_id") or "").strip()
            receipt_digest = str(item.get("prior_receipt_digest") or "").strip()
            still_valid = item.get("prior_receipt_still_valid") is True
            if not receipt_id or not receipt_digest or not still_valid:
                findings.append(
                    {
                        "code": "protected-prior-not-bound",
                        "observation_id": observation_id,
                    }
                )
                return
            try:
                identity(receipt_id, "prior receipt id")
                require_digest(receipt_digest, "prior receipt digest")
            except ValueError:
                findings.append(
                    {
                        "code": "protected-prior-invalid",
                        "observation_id": observation_id,
                    }
                )
            return
        findings.append(
            {
                "code": "deployment-evidence-non-live",
                "observation_id": observation_id,
                "mode": mode.value,
            }
        )


def evidence_mode(item: Mapping[str, Any], label: str) -> EvidenceMode:
    try:
        return EvidenceMode(str(item.get("evidence_mode") or "live"))
    except ValueError as error:
        raise invalid(
            "benchmark_deployment_evidence_mode_invalid",
            f"{label} has an invalid evidence mode.",
            phase="deployment",
        ) from error


def validate_tier_endpoint(
    tier: str,
    endpoint: str,
    observation_id: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    parsed = urlparse(endpoint)
    if tier == "device":
        if parsed.scheme not in {"local", "unix", "pipe", "process"}:
            findings.append(
                {
                    "code": "device-endpoint-not-local",
                    "observation_id": observation_id,
                }
            )
        return findings
    if parsed.scheme not in {"https", "tcp", "tls", "grpc"}:
        findings.append(
            {
                "code": f"{tier}-endpoint-transport-invalid",
                "observation_id": observation_id,
            }
        )
    host = parsed.hostname
    if not host:
        findings.append(
            {
                "code": f"{tier}-endpoint-host-missing",
                "observation_id": observation_id,
            }
        )
        return findings
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if tier == "edge":
        if address is not None and address.is_loopback:
            findings.append(
                {
                    "code": "edge-endpoint-loopback",
                    "observation_id": observation_id,
                }
            )
    if tier == "cloud":
        if address is not None and (
            address.is_loopback or address.is_private or address.is_link_local
        ):
            findings.append(
                {
                    "code": "cloud-endpoint-not-external",
                    "observation_id": observation_id,
                }
            )
    return findings
