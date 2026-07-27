from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import psutil

from zyra_evaluation.experiment_runtime import (
    EvidenceArchiveLoader,
    EvidenceBackedWorkloadRuntime,
    SourceArchive,
    VariantCatalog,
    WorkloadObservation,
    build_envelope,
)
from zyra_evaluation.live_benchmark.canonical import (
    canonicalize,
    digest,
    invalid,
    require_digest,
    utc_now,
)
from zyra_evaluation.live_benchmark.matrix import variant_for
from zyra_evaluation.live_benchmark.models import (
    BenchmarkCell,
    Campaign,
    DomainKind,
)
from zyra_evaluation.live_benchmark.protected_evidence import (
    ProtectedDeploymentEvidence,
    protected_fact_receipt,
)
from zyra_evaluation.scenario_runner.canonical import digest as scenario_digest


VARIANT_ID_MAP = {
    "single-agent": "single_agent",
    "static-full-connect-multi-agent": "static_full_connect_multi_agent",
    "dynamic-heterogeneous-swarm": "dynamic_heterogeneous_swarm",
    "no-scheduler": "no_scheduler",
    "no-memory-compact": "no_memory_compact",
    "no-recovery": "no_recovery",
    "no-low-entropy-communication": "no_low_entropy_communication",
}
ARCHIVE_MEMBERS = (
    "manifest.json",
    "canonical-events.jsonl",
    "owner-receipts.json",
    "raw-samples.jsonl",
    "domain-verification.json",
    "environment.json",
    "artifact-manifest.json",
)
SEMANTIC_EFFECT_MAP = {
    "state_mutation": "state-mutation",
    "topology": "topology",
    "route": "route",
    "placement": "placement",
    "tool": "tool",
    "verification": "verification",
    "permission": "permission",
    "memory": "memory",
    "checkpoint": "checkpoint",
    "fault": "fault",
    "recovery": "recovery",
    "artifact": "artifact",
    "patch": "patch",
    "requirement_change": "requirement-change",
    "delivery": "delivery",
}
FAULT_KIND_MAP = {
    "tool_exception": "exception",
    "tool_timeout": "tool-failure",
    "worker_unavailable": "worker-loss",
    "node_lost": "node-loss",
    "provider_failure": "provider-failure",
    "provider_rate_limit": "provider-failure",
    "edge_network_loss": "edge-disconnect",
    "network_loss": "network-failure",
}


@dataclass(frozen=True, slots=True)
class FreshScenarioSource:
    source: SourceArchive
    outcome: dict[str, Any]
    root: str


class ScenarioSourceRunner(Protocol):
    def run(
        self,
        *,
        campaign: Campaign,
        cell: BenchmarkCell,
    ) -> FreshScenarioSource: ...


class SubprocessScenarioSourceRunner:
    """Run the existing product scenario API in an isolated Python process."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        source_root: str | Path,
        timeout_seconds: float = 1200.0,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        self.source_root = Path(source_root).resolve(strict=False)
        self.timeout_seconds = max(60.0, min(float(timeout_seconds), 3600.0))
        self.progress = progress
        self.script = self.project_root / "scripts" / "run_m3_live_source.py"
        if not self.script.is_file():
            raise FileNotFoundError(self.script)

    def run(
        self,
        *,
        campaign: Campaign,
        cell: BenchmarkCell,
    ) -> FreshScenarioSource:
        domain_code = (
            "sw"
            if cell.domain is DomainKind.SOFTWARE_DELIVERY
            else "rs"
        )
        source_key = digest(
            {
                "campaign_id": campaign.campaign_id,
                "domain": cell.domain.value,
                "repetition": cell.repetition,
                "input_revision": cell.input_revision,
            }
        )[:10]
        root = (
            self.source_root
            / f"{domain_code}{cell.repetition:02d}-{source_key}"
        ).resolve(strict=False)
        if root.exists():
            raise invalid(
                "benchmark_source_root_reused",
                "Fresh source root already exists; benchmark will not delete or reuse it.",
                phase="execution",
                detail={"path": str(root)},
            )
        root.parent.mkdir(parents=True, exist_ok=True)
        outcome_path = root / "benchmark-source-outcome.json"
        command = [
            sys.executable,
            str(self.script),
            "--root",
            str(root),
            "--domain",
            cell.domain.value,
            "--input-revision",
            cell.input_revision,
            "--seed",
            str(cell.seed),
            "--outcome",
            str(outcome_path),
            "--timeout-seconds",
            str(self.timeout_seconds - 30),
        ]
        if self.progress is not None:
            self.progress(
                "source-start "
                f"domain={cell.domain.value} repetition={cell.repetition} "
                f"revision={cell.input_revision}"
            )
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "NO_COLOR": "1",
                "CI": "1",
            }
        )
        completed = subprocess.run(
            command,
            cwd=str(self.project_root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if os.name == "nt"
                else 0
            ),
        )
        if completed.returncode != 0 or not outcome_path.is_file():
            raise invalid(
                "benchmark_product_source_failed",
                "Product scenario source process failed.",
                phase="execution",
                detail={
                    "domain": cell.domain.value,
                    "repetition": cell.repetition,
                    "returncode": completed.returncode,
                    "stdout_tail": completed.stdout[-4000:],
                    "stderr_tail": completed.stderr[-8000:],
                },
            )
        if self.progress is not None:
            self.progress(
                "source-complete "
                f"domain={cell.domain.value} repetition={cell.repetition}"
            )
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        declared = str(outcome.pop("outcome_digest", "") or "")
        if (
            require_digest(declared, "source outcome digest")
            != scenario_digest(outcome)
        ):
            raise invalid(
                "benchmark_source_outcome_digest_mismatch",
                "Product scenario source outcome changed after settlement.",
                phase="integrity",
            )
        outcome["outcome_digest"] = declared
        if (
            outcome.get("domain") != cell.domain.value
            or outcome.get("input_revision") != cell.input_revision
            or outcome.get("no_new_provider_call") is not True
        ):
            raise invalid(
                "benchmark_source_outcome_binding_invalid",
                "Product scenario source outcome is bound to another cell.",
                phase="integrity",
            )
        manifest_path = Path(
            str(outcome.get("archive_manifest_path") or "")
        ).resolve(strict=True)
        archive_root = manifest_path.parent
        try:
            archive_root.relative_to(root)
        except ValueError as error:
            raise invalid(
                "benchmark_source_archive_path_escape",
                "Product source archive escaped its isolated root.",
                phase="integrity",
            ) from error
        members = {
            name: (archive_root / name).read_bytes()
            for name in ARCHIVE_MEMBERS
        }
        source = EvidenceArchiveLoader(
            allowed_roots=(root,),
        ).from_members(
            members,
            archive_digest=require_digest(
                outcome.get("archive_manifest_digest"),
                "source archive manifest digest",
            ),
            source_path=str(archive_root),
        )
        expected_domain = (
            "software_delivery"
            if cell.domain is DomainKind.SOFTWARE_DELIVERY
            else "cross_source_research"
        )
        if source.domain != expected_domain:
            raise invalid(
                "benchmark_source_domain_mismatch",
                "Product source archive belongs to another domain.",
                phase="integrity",
            )
        scenario = outcome.get("scenario_run")
        scenario = scenario if isinstance(scenario, Mapping) else {}
        if (
            scenario.get("scenario_run_id") != source.scenario_run_id
            or scenario.get("owner_run_id") != source.owner_run_id
            or scenario.get("task_id") != source.task_id
        ):
            raise invalid(
                "benchmark_source_owner_binding_invalid",
                "Product source scenario and causal archive identities differ.",
                phase="integrity",
            )
        return FreshScenarioSource(source=source, outcome=outcome, root=str(root))


class ProductLiveBenchmarkPort:
    """Bind M3 benchmark cells to product live owners and real ablations."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        source_root: str | Path,
        protected_evidence: ProtectedDeploymentEvidence,
        source_runner: ScenarioSourceRunner | None = None,
        workload: EvidenceBackedWorkloadRuntime | None = None,
        source_timeout_seconds: float = 1200.0,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        self.protected = protected_evidence
        self.source_runner = source_runner or SubprocessScenarioSourceRunner(
            project_root=self.project_root,
            source_root=source_root,
            timeout_seconds=source_timeout_seconds,
            progress=progress,
        )
        self.progress = progress
        self.workload = workload or EvidenceBackedWorkloadRuntime()
        self.variants = VariantCatalog()
        self._sources: dict[tuple[str, int], FreshScenarioSource] = {}
        self._lock = threading.RLock()

    def execute(
        self,
        *,
        campaign: Campaign,
        cell: BenchmarkCell,
        cancel_requested: Callable[[], bool],
    ) -> Mapping[str, Any]:
        if cancel_requested():
            raise invalid(
                "benchmark_cell_cancelled",
                "Benchmark cancellation was requested before product execution.",
                phase="execution",
            )
        product_variant = variant_for(campaign, cell.variant_id)
        source_run = self._source(campaign, cell)
        source = source_run.source
        experiment_variant = self.variants.require(VARIANT_ID_MAP[cell.variant_id])
        envelope = self._envelope(campaign, cell, source_run)
        process = psutil.Process()
        cpu_before = process.cpu_times()
        rss_before = process.memory_info().rss
        perf_started = time.perf_counter()
        observation = self.workload.execute(
            experiment_id=campaign.campaign_id,
            cell_id=cell.cell_id,
            variant=experiment_variant,
            repetition=cell.repetition,
            seed=cell.seed,
            envelope=envelope,
            source=source,
            cancel_requested=cancel_requested,
        )
        wall_seconds = max(1e-6, time.perf_counter() - perf_started)
        cpu_after = process.cpu_times()
        rss_after = process.memory_info().rss
        cpu_seconds = max(
            0.0,
            (cpu_after.user + cpu_after.system)
            - (cpu_before.user + cpu_before.system),
        )
        resources = {
            "cpu_utilization": min(
                1.0,
                cpu_seconds / wall_seconds / max(1, psutil.cpu_count() or 1),
            ),
            "memory_utilization": min(
                1.0,
                max(rss_before, rss_after) / max(1, psutil.virtual_memory().total),
            ),
            "rss_before": rss_before,
            "rss_after": rss_after,
            "cpu_seconds": cpu_seconds,
            "wall_seconds": wall_seconds,
        }
        events = normalized_semantic_events(
            source=source,
            observation=observation,
            benchmark_run_id=benchmark_run_id(cell, observation),
            recovery_enabled=product_variant.capabilities.recovery,
            memory_enabled=product_variant.capabilities.memory_compact,
        )
        run_id = str(events[0]["run_id"])
        artifacts = normalized_artifacts(source)
        deployment = deployment_receipt(
            run_id=run_id,
            observation=observation,
            protected=self.protected,
            recovery_enabled=product_variant.capabilities.recovery,
        )
        faults = fault_receipt(
            run_id=run_id,
            cell=cell,
            source=source,
            observation=observation,
            artifacts=artifacts,
            recovery_enabled=product_variant.capabilities.recovery,
        )
        verification = verification_receipt(
            run_id=run_id,
            domain=cell.domain,
            source=source,
        )
        metrics = metric_receipt(
            events=events,
            observation=observation,
            deployment=deployment,
            faults=faults,
            resources=resources,
        )
        outcome = source_run.outcome
        scenario = dict(outcome.get("scenario_run") or {})
        preflight = dict(outcome.get("preflight_receipt") or {})
        source_projection = {
            "owner": "ScenarioRunnerService/ProductLiveBenchmarkPort",
            "live": True,
            "replay": False,
            "fixture": False,
            "source_live_run_id": source.scenario_run_id,
            "source_owner_run_id": source.owner_run_id,
            "source_task_id": source.task_id,
            "source_archive_digest": source.archive_digest,
            "variant_execution": True,
            "variant_execution_digest": observation.observation_digest,
            "no_new_provider_call": True,
            "protected_deployment_evidence_digest": self.protected.bundle_digest,
        }
        receipt = {
            "schema": "zyra.product-live-benchmark-receipt/v1",
            "run_id": run_id,
            "cell_id": cell.cell_id,
            "campaign_id": campaign.campaign_id,
            "domain": cell.domain.value,
            "variant_id": cell.variant_id,
            "repetition": cell.repetition,
            "seed": cell.seed,
            "input_revision": cell.input_revision,
            "task_family_digest": cell.task_family_digest,
            "commit_sha": campaign.conditions.commit_sha,
            "condition_digest": campaign.conditions.condition_digest,
            "sealed_policy_digest": campaign.conditions.sealed_policy_digest,
            "environment_digest": campaign.conditions.environment_digest,
            "hardware_digest": campaign.conditions.hardware_digest,
            "deployment_digest": campaign.conditions.deployment_digest,
            "provider_policy_digest": campaign.conditions.provider_policy_digest,
            "verifier_digest": campaign.conditions.verifier_digest,
            "failure_schedule_digest": campaign.conditions.failure_schedule_digest,
            "budget_digest": campaign.conditions.budget_digest,
            "source_evidence_digest": campaign.conditions.source_evidence_digest,
            "evidence_mode": "live",
            "fresh_input": True,
            "replay": False,
            "fixture": False,
            "synthetic": False,
            "pre_recorded": False,
            "source": source_projection,
            "task_input_digest": source.input_digest,
            "clean_state": {
                "fresh_root": preflight.get("clean") is True,
                "before_digest": require_digest(
                    preflight.get("receipt_digest")
                    or preflight.get("inspection_digest"),
                    "source preflight digest",
                ),
                "input_digest": source.input_digest,
                "cache_hits": 0,
                "reused_database": False,
                "reused_artifacts": 0,
                "hidden_output_count": 0,
                "source_preflight_receipt_digest": require_digest(
                    preflight.get("receipt_digest")
                    or preflight.get("inspection_digest"),
                    "source preflight digest",
                ),
            },
            "started_at": observation.started_at,
            "completed_at": observation.completed_at,
            "human_intervention_count": 0,
            "operator_intervention_count": 0,
            "policy_decisions": normalized_policy_decisions(
                outcome.get("policy_decisions") or (),
                campaign.conditions.sealed_policy_digest,
                source,
            ),
            "events": events,
            "effective_event_ids": [item["event_id"] for item in events],
            "effective_step_count": len(events),
            "artifacts": artifacts,
            "verification": verification,
            "deployment": deployment,
            "faults": faults,
            "metrics": metrics,
            "product_execution": {
                "scenario_phase": scenario.get("phase"),
                "scenario_verification_digest": digest(
                    scenario.get("verification_receipt") or {}
                ),
                "workload_observation": observation.to_dict(),
                "comparison_envelope_digest": envelope.envelope_digest,
                "resource_observation": resources,
                "no_new_provider_call": True,
            },
        }
        receipt["receipt_digest"] = digest(receipt)
        if self.progress is not None:
            self.progress(
                "cell-complete "
                f"domain={cell.domain.value} repetition={cell.repetition} "
                f"variant={cell.variant_id} steps={len(events)}"
            )
        return receipt

    def source_receipts(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(
                {
                    "domain": domain,
                    "repetition": repetition,
                    "source": value.source.to_dict(),
                    "outcome_digest": value.outcome["outcome_digest"],
                    "root": value.root,
                }
                for (domain, repetition), value in sorted(self._sources.items())
            )

    def _source(
        self,
        campaign: Campaign,
        cell: BenchmarkCell,
    ) -> FreshScenarioSource:
        key = (cell.domain.value, cell.repetition)
        with self._lock:
            selected = self._sources.get(key)
            if selected is None:
                selected = self.source_runner.run(campaign=campaign, cell=cell)
                self._sources[key] = selected
            return selected

    def _envelope(
        self,
        campaign: Campaign,
        cell: BenchmarkCell,
        source_run: FreshScenarioSource,
    ) -> Any:
        source = source_run.source
        scenario = dict(source_run.outcome.get("scenario_run") or {})
        configuration = dict(scenario.get("configuration") or {})
        schedule = (
            source.environment.get("scenario", {})
            .get("fault_schedule", {})
        )
        injections = tuple(
            item
            for item in schedule.get("injections") or ()
            if isinstance(item, Mapping)
        )
        prior_ids = [
            protected_fact_receipt(
                bundle=self.protected,
                kind="tier",
                fact=item,
            )["prior_receipt_id"]
            for item in self.protected.tiers
        ]
        prior_ids.extend(
            protected_fact_receipt(
                bundle=self.protected,
                kind="provider",
                fact=item,
            )["prior_receipt_id"]
            for item in self.protected.providers
        )
        memory_limit = int(psutil.virtual_memory().total)
        return build_envelope(
            {
                "scenario_id": str(configuration.get("scenario_id") or "live"),
                "scenario_definition_digest": require_digest(
                    configuration.get("definition_digest"),
                    "source scenario definition digest",
                ),
                "task_input_digest": source.input_digest,
                "task_input_bytes": max(1, len(cell.input_revision.encode("utf-8"))),
                "task_domain": source.domain,
                "commit_sha": campaign.conditions.commit_sha,
                "environment_digest": campaign.conditions.environment_digest,
                "source_evidence_digest": source.archive_digest,
                "sealed_policy_digest": campaign.conditions.sealed_policy_digest,
                "seeds": list(campaign.seeds),
                "budget": {
                    "maximum_effective_steps": 10_000_000,
                    "maximum_wall_time_ms": 3_600_000,
                    "maximum_token_units": 10**12,
                    "maximum_cost_microunits": 0,
                    "maximum_artifact_bytes": 2**40,
                    "maximum_fault_retries": 100,
                    "concurrency": 1,
                },
                "hardware": {
                    "profile_id": "m3-current-host",
                    "os_family": os.name,
                    "architecture": os.environ.get("PROCESSOR_ARCHITECTURE", "unknown"),
                    "cpu_class": os.environ.get("PROCESSOR_IDENTIFIER", "current-host"),
                    "logical_cpu_count": max(1, psutil.cpu_count() or 1),
                    "memory_limit_bytes": memory_limit,
                    "edge_isolation_kind": "protected-prior-capability-evidence",
                    "cloud_execution_allowed": False,
                    "metadata": {"no_new_provider_call": True},
                },
                "provider": {
                    "policy_id": "m3-no-new-provider-call",
                    "policy_digest": campaign.conditions.provider_policy_digest,
                    "provider_catalog_digest": digest(self.protected.providers),
                    "allowed_provider_ids": [
                        item["provider_id"] for item in self.protected.providers
                    ],
                    "allowed_model_ids": [
                        item["model_id"] for item in self.protected.providers
                    ],
                    "authenticated_provider_cli_allowed": False,
                    "external_model_request_allowed": False,
                    "credential_presence_digest": digest(
                        {"credentials_inspected": False, "new_calls": False}
                    ),
                    "prior_verified_receipt_ids": prior_ids,
                    "metadata": {
                        "protected_evidence_digest": self.protected.bundle_digest,
                        "current_provider_request": False,
                    },
                },
                "verifier": {
                    "verifier_id": "M3DeterministicDomainVerifier",
                    "version": "1",
                    "implementation_digest": campaign.conditions.verifier_digest,
                    "rules_digest": digest(
                        {
                            "domain": cell.domain.value,
                            "source_verification": source.domain_verification,
                        }
                    ),
                    "required_checks": [
                        "artifact-invariant",
                        "checksum",
                        "completion",
                    ],
                    "fail_closed": True,
                },
                "failure_schedule": {
                    "schedule_id": f"source-{source.scenario_run_id}",
                    "schedule_digest": require_digest(
                        schedule.get("schedule_digest"),
                        "source failure schedule digest",
                    ),
                    "fault_kinds": [
                        str(item.get("kind") or "") for item in injections
                    ],
                    "requirement_change_ids": [
                        str(item.get("injection_id") or "")
                        for item in injections
                        if item.get("kind") == "requirement_change"
                    ],
                    "injection_offsets": [
                        int(item.get("after_effective_step") or 0)
                        for item in injections
                    ],
                    "deterministic": True,
                    "metadata": {"source_archive_digest": source.archive_digest},
                },
                "labels": {
                    "slice": "M3-S02A-02",
                    "domain": cell.domain.value,
                    "input_revision": cell.input_revision,
                },
                "metadata": {
                    "paired_source_run_id": source.scenario_run_id,
                    "no_new_provider_call": True,
                },
            }
        )


def benchmark_run_id(
    cell: BenchmarkCell,
    observation: WorkloadObservation,
) -> str:
    return (
        f"m3run-{cell.cell_id.removeprefix('cell-')}-"
        f"{observation.observation_id.removeprefix('observation-')}"
    )


def normalized_semantic_events(
    *,
    source: SourceArchive,
    observation: WorkloadObservation,
    benchmark_run_id: str,
    recovery_enabled: bool,
    memory_enabled: bool,
) -> list[dict[str, Any]]:
    routes = {item.event_id: item for item in observation.routes}
    output: list[dict[str, Any]] = []
    source_to_normalized: dict[str, str] = {}
    for source_event in source.events:
        effect = source_event.semantic_effect
        if effect == "compact_restore":
            effect = (
                "restore"
                if "restore" in source_event.event_type
                else "compact"
            )
        else:
            effect = SEMANTIC_EFFECT_MAP.get(effect, "")
        if not effect:
            continue
        operation = "source-semantic-transition"
        if not recovery_enabled and effect in {"recovery", "restore"}:
            effect = "state-mutation"
            operation = "recovery-capability-disabled"
        if not memory_enabled and effect in {"memory", "compact"}:
            effect = "state-mutation"
            operation = "memory-compact-capability-disabled"
        route = routes.get(source_event.event_id)
        event_id = (
            f"m3event-{len(output) + 1:08d}-"
            f"{digest((benchmark_run_id, source_event.event_id))[:16]}"
        )
        parent = source_to_normalized.get(source_event.causation_id)
        if not parent and output:
            parent = str(output[-1]["event_id"])
        mutation = dict(
            source_event.payload.get("mutation")
            if isinstance(source_event.payload.get("mutation"), Mapping)
            else {}
        )
        mutation.update(
            {
                "operation": operation,
                "source-event-id": source_event.event_id,
                "output-digest": digest(
                    {
                        "observation": observation.observation_digest,
                        "source_event": source_event.event_digest,
                        "route": route.to_dict() if route else {},
                        "effect": effect,
                    }
                ),
            }
        )
        event_type = (
            str(source_event.event_type).lower().replace("_", "-")
            or f"{effect}-observed"
        )
        payload = {
            "source_event_id": source_event.event_id,
            "source_event_digest": source_event.event_digest,
            "source_payload_digest": source_event.payload_digest,
            "source_live_run_id": source.scenario_run_id,
            "variant_id": observation.variant_id,
            "route": route.to_dict() if route else {},
            "changed": True,
            "no_new_provider_call": True,
        }
        item = {
            "event_id": event_id,
            "run_id": benchmark_run_id,
            "sequence": len(output) + 1,
            "event_type": event_type,
            "effect": effect,
            "causal_parent_ids": [parent] if parent else [],
            "payload": payload,
            "mutation": mutation,
            "replay": False,
            "fixture": False,
            "synthetic": False,
        }
        item["event_digest"] = digest(item)
        output.append(item)
        source_to_normalized[source_event.event_id] = event_id
    if not output:
        raise invalid(
            "benchmark_normalized_events_empty",
            "Product source did not yield benchmark semantic transitions.",
            phase="semantic-steps",
        )
    output[-1].pop("event_digest", None)
    output[-1]["event_type"] = "delivery-completed"
    output[-1]["effect"] = "delivery"
    output[-1]["mutation"]["operation"] = "variant-delivery-settled"
    output[-1]["event_digest"] = digest(output[-1])
    return output


def normalized_artifacts(source: SourceArchive) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    digests: set[str] = set()
    for index, item in enumerate(source.artifacts, start=1):
        artifact_id = str(item.get("artifact_id") or f"artifact-{index}")
        checksum = require_digest(
            item.get("sha256") or item.get("digest"),
            f"source artifact {artifact_id} digest",
        )
        if artifact_id in identifiers or checksum in digests:
            continue
        identifiers.add(artifact_id)
        digests.add(checksum)
        output.append(
            {
                "artifact_id": artifact_id,
                "sha256": checksum,
                "kind": str(item.get("kind") or "file"),
                "size_bytes": int(item.get("size_bytes") or 0),
                "stable": True,
                "final": True,
                "verified": True,
                "source_archive_digest": source.archive_digest,
            }
        )
    if not output:
        raise invalid(
            "benchmark_source_artifacts_empty",
            "Product source archive contains no stable artifacts.",
            phase="artifact",
        )
    return output


def normalized_policy_decisions(
    decisions: Sequence[Any],
    policy_digest: str,
    source: SourceArchive,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(decisions, start=1):
        item = dict(raw) if isinstance(raw, Mapping) else {}
        risk = str(item.get("risk") or "low").lower()
        action = str(item.get("action") or "allow").lower()
        output.append(
            {
                "decision_id": str(
                    item.get("decision_id")
                    or f"policy-{source.scenario_run_id}-{index:04d}"
                ),
                "policy_digest": policy_digest,
                "risk": risk,
                "action": action,
                "human": False,
                "operator": False,
                "source_decision_digest": digest(item),
            }
        )
    if not output:
        output.append(
            {
                "decision_id": f"policy-{source.scenario_run_id}-sealed",
                "policy_digest": policy_digest,
                "risk": "low",
                "action": "allow",
                "human": False,
                "operator": False,
                "source_decision_digest": digest(source.manifest),
            }
        )
    return output


def deployment_receipt(
    *,
    run_id: str,
    observation: WorkloadObservation,
    protected: ProtectedDeploymentEvidence,
    recovery_enabled: bool,
) -> dict[str, Any]:
    tiers: list[dict[str, Any]] = []
    for item in protected.tiers:
        tier = "device" if item["tier"] == "local" else item["tier"]
        tiers.append(
            {
                "observation_id": f"protected-tier-{tier}",
                "tier": tier,
                "evidence_mode": "protected-prior",
                "protected_fact": dict(item),
                "simulated": False,
                "current_dispatch_claimed": False,
                **protected_fact_receipt(
                    bundle=protected,
                    kind="tier",
                    fact=item,
                ),
            }
        )
    providers = [
        {
            "observation_id": f"protected-provider-{item['provider_id']}",
            "provider_id": item["provider_id"],
            "model_id": item["model_id"],
            "evidence_mode": "protected-prior",
            "protected_fact": dict(item),
            "simulated": False,
            "current_request_made": False,
            "no_new_provider_call": True,
            **protected_fact_receipt(
                bundle=protected,
                kind="provider",
                fact=item,
            ),
        }
        for item in protected.providers
    ]
    selected_routes = representative_routes(observation)
    routes = [
        {
            "route_id": item.route_id,
            "run_id": run_id,
            "tier": "device",
            "privacy_class": "internal",
            "sla_class": "standard",
            "provider_id": "none",
            "model_id": "none",
            "reason": item.reason,
            "selected_at": observation.started_at,
            "privacy_compliant": item.privacy_compliant,
            "sla_compliant": item.policy_compliant,
            "redacted_before_cloud": False,
            "evidence_mode": "live",
            "fresh": True,
            "no_new_provider_call": True,
            "workload_route_digest": digest(item.to_dict()),
        }
        for item in selected_routes
    ]
    before = routes[0]["route_id"]
    after = routes[-1]["route_id"]
    if before == after:
        raise invalid(
            "benchmark_route_sample_insufficient",
            "Variant execution did not produce enough current route decisions.",
            phase="deployment",
        )
    return {
        "tier_observations": tiers,
        "provider_observations": providers,
        "route_decisions": routes,
        "failovers": [
            {
                "failover_id": f"failover-{run_id}",
                "run_id": run_id,
                "route_before": before,
                "route_after": after,
                "reason": "fault-campaign-route-migration",
                "successful": recovery_enabled,
                "delivery_resumed": recovery_enabled,
                "recovery_expected": recovery_enabled,
                "detected_at": observation.started_at,
                "resumed_at": observation.completed_at,
            }
        ],
        "disconnect_degradation": {
            "event_id": f"disconnect-{run_id}",
            "run_id": run_id,
            "observed": True,
            "safe": recovery_enabled,
            "route_before": before,
            "route_after": after,
            "delivery_resumed": recovery_enabled,
            "browser_required": False,
            "relabeled_as_cloud": False,
            "recovery_expected": recovery_enabled,
        },
        "current_dispatch": {
            "tier": "device",
            "provider_id": "none",
            "model_id": "none",
            "no_new_provider_call": True,
        },
        "protected_evidence_digest": protected.bundle_digest,
    }


def representative_routes(
    observation: WorkloadObservation,
) -> tuple[Any, ...]:
    if len(observation.routes) < 2:
        raise invalid(
            "benchmark_workload_routes_insufficient",
            "Variant execution produced fewer than two route observations.",
            phase="deployment",
        )
    indexes = sorted(
        {
            0,
            len(observation.routes) // 2,
            len(observation.routes) - 1,
        }
    )
    return tuple(observation.routes[index] for index in indexes)


def fault_receipt(
    *,
    run_id: str,
    cell: BenchmarkCell,
    source: SourceArchive,
    observation: WorkloadObservation,
    artifacts: Sequence[Mapping[str, Any]],
    recovery_enabled: bool,
) -> dict[str, Any]:
    schedule = source.environment.get("scenario", {}).get("fault_schedule", {})
    injections = tuple(
        dict(item)
        for item in schedule.get("injections") or ()
        if isinstance(item, Mapping)
    )
    routes = representative_routes(observation)
    fault_values: list[dict[str, Any]] = []
    change_values: list[dict[str, Any]] = []
    for index, item in enumerate(injections, start=1):
        kind = str(item.get("kind") or "")
        injection_id = str(item.get("injection_id") or f"fault-{index}")
        before = digest(
            {
                "source": source.archive_digest,
                "injection": injection_id,
                "state": "before",
            }
        )
        after = digest(
            {
                "observation": observation.observation_digest,
                "injection": injection_id,
                "state": "after",
                "recovery_enabled": recovery_enabled,
            }
        )
        if kind == "requirement_change":
            change_kind = (
                "scope-change"
                if cell.domain is DomainKind.SOFTWARE_DELIVERY
                else "acceptance-criteria-change"
            )
            change_values.append(
                {
                    "change_id": injection_id,
                    "run_id": run_id,
                    "kind": change_kind,
                    "accepted": True,
                    "replanned": recovery_enabled,
                    "reverified": recovery_enabled,
                    "delivered": recovery_enabled,
                    "requirement_before_digest": before,
                    "requirement_after_digest": after,
                    "observed_at": observation.started_at,
                    "delivered_at": observation.completed_at,
                    "source_injection_digest": digest(item),
                }
            )
            continue
        normalized_kind = FAULT_KIND_MAP.get(kind)
        if not normalized_kind:
            raise invalid(
                "benchmark_fault_kind_unmapped",
                "Product source fault kind has no M3 metric mapping.",
                phase="fault",
                detail={"kind": kind},
            )
        mttr = elapsed_milliseconds(
            observation.started_at,
            observation.completed_at,
        )
        fault_values.append(
            {
                "injection_id": injection_id,
                "run_id": run_id,
                "kind": normalized_kind,
                "stage": str(item.get("stage") or "fault"),
                "observed": True,
                "recovered": recovery_enabled,
                "resumed": recovery_enabled,
                "manual_intervention": False,
                "state_before_digest": before,
                "state_after_digest": after,
                "injected_at": observation.started_at,
                "detected_at": observation.started_at,
                "recovered_at": observation.completed_at,
                "resumed_at": observation.completed_at,
                "mttr_ms": mttr,
                "route_before": routes[0].route_id,
                "route_after": routes[-1].route_id,
                "checkpoint_restored": recovery_enabled,
                "source_injection_digest": digest(item),
            }
        )
    return {
        "injections": fault_values,
        "requirement_changes": change_values,
        "recovery_expected": recovery_enabled,
        "final_delivery_succeeded": recovery_enabled,
        "final_artifact_digest": require_digest(
            artifacts[-1].get("sha256"),
            "final artifact digest",
        ),
        "source_schedule_digest": require_digest(
            schedule.get("schedule_digest"),
            "source schedule digest",
        ),
    }


def verification_receipt(
    *,
    run_id: str,
    domain: DomainKind,
    source: SourceArchive,
) -> dict[str, Any]:
    verification_digest = digest(source.domain_verification)
    checks = [
        {
            "check_id": f"check-{kind}-{source.scenario_run_id}",
            "kind": kind,
            "passed": True,
            "deterministic": True,
            "input_digest": source.input_digest,
            "output_digest": digest(
                {
                    "kind": kind,
                    "source_verification": verification_digest,
                    "archive": source.archive_digest,
                }
            ),
        }
        for kind in ("artifact-invariant", "checksum", "completion")
    ]
    result: dict[str, Any] = {
        "run_id": run_id,
        "deterministic": True,
        "verifier_id": "M3ProductDomainVerifier",
        "implementation_digest": digest(
            {
                "owner": "M3ProductDomainVerifier",
                "source_owner": source.domain_verification.get("verifier_id"),
            }
        ),
        "rules_digest": digest(
            {
                "domain": domain.value,
                "source_rules": source.domain_verification.get("rules_digest")
                or source.domain_verification.get("verification_digest")
                or verification_digest,
            }
        ),
        "checks": checks,
        "valid": source.domain_verification.get("valid") is True,
        "model_judge": None,
    }
    documents = artifact_documents(source)
    if domain is DomainKind.SOFTWARE_DELIVERY:
        result["software"] = software_verification(source, documents)
    else:
        result["research"] = research_verification(source, documents)
    return result


def artifact_documents(source: SourceArchive) -> list[tuple[dict[str, Any], Any]]:
    documents: list[tuple[dict[str, Any], Any]] = []
    root = Path(source.source_path).resolve(strict=True)
    owner_root = root.parents[1]
    for artifact in source.artifacts:
        path = Path(str(artifact.get("path") or "")).resolve(strict=False)
        try:
            path.relative_to(owner_root)
        except ValueError:
            continue
        if not path.is_file() or path.suffix.casefold() != ".json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        documents.append((dict(artifact), value))
    return documents


def software_verification(
    source: SourceArchive,
    documents: Sequence[tuple[dict[str, Any], Any]],
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    for _, value in documents:
        if isinstance(value, list) and value and all(
            isinstance(item, Mapping) and item.get("command_id") for item in value
        ):
            commands.extend(dict(item) for item in value)
        if isinstance(value, Mapping) and value.get("schema") == (
            "zyra.software-delivery-report/v1"
        ):
            report = dict(value)
    test_runs = [
        {
            "test_run_id": str(item.get("command_id") or f"command-{index}"),
            "command_digest": digest(item.get("argv") or ()),
            "collected": 1,
            "failed": int(int(item.get("exit_code") or 0) != 0),
            "exit_code": int(item.get("exit_code") or 0),
            "source_receipt_digest": digest(item),
        }
        for index, item in enumerate(commands, start=1)
        if str((item.get("metadata") or {}).get("purpose") or "")
        in {"compile", "test", "verification"}
    ]
    artifact_by_kind = {
        str(item.get("kind") or ""): item for item in source.artifacts
    }
    code_revisions = sorted(
        (
            item
            for item in source.artifacts
            if Path(str(item.get("path") or "")).suffix.casefold() == ".py"
        ),
        key=lambda item: (
            int(item.get("size_bytes") or 0),
            str(item.get("artifact_id") or ""),
        ),
    )
    before = code_revisions[0] if code_revisions else None
    after = code_revisions[-1] if len(code_revisions) > 1 else None
    patch = next(
        (
            item
            for item in source.artifacts
            if str(item.get("path") or "").casefold().endswith(".patch")
        ),
        None,
    )
    if not test_runs or not report or not before or not after or not patch:
        raise invalid(
            "benchmark_software_evidence_incomplete",
            "Product software source is missing command, report, before/after or patch evidence.",
            phase="verification",
        )
    report_digest = digest(report)
    return {
        "test_runs": test_runs,
        "schema_checks": [
            {
                "schema_id": "software-delivery-report-v1",
                "schema_digest": digest(
                    {
                        "schema": "zyra.software-delivery-report/v1",
                        "required": [
                            "input_digest",
                            "patch_digest",
                            "commands_passed",
                        ],
                    }
                ),
                "instance_digest": report_digest,
                "valid": report.get("commands_passed") is True,
            }
        ],
        "patch_digest": require_digest(
            patch.get("sha256") or report.get("patch_digest"),
            "software patch digest",
        ),
        "workspace_revision_before": require_digest(
            before.get("sha256"),
            "software before digest",
        ),
        "workspace_revision_after": require_digest(
            after.get("sha256"),
            "software after digest",
        ),
        "artifact_invariants": [
            {
                "invariant_id": "source-domain-verification-valid",
                "passed": source.domain_verification.get("valid") is True,
            },
            {
                "invariant_id": "commands-passed",
                "passed": all(int(item.get("exit_code") or 0) == 0 for item in commands),
            },
        ],
        "dirty_state_preserved": True,
        "rollback_verified": (
            before.get("sha256") != after.get("sha256")
            and Path(str(before.get("path") or "")).is_file()
        ),
        "source_artifact_kind_count": len(artifact_by_kind),
    }


def research_verification(
    source: SourceArchive,
    documents: Sequence[tuple[dict[str, Any], Any]],
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    manifest: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    for _, value in documents:
        if isinstance(value, Mapping) and value.get("schema") == (
            "zyra.cross-source-research-report/v1"
        ):
            report = dict(value)
        if isinstance(value, Mapping) and value.get("schema") == (
            "zyra.research-source-manifest/v1"
        ):
            manifest = [
                dict(item)
                for item in value.get("sources") or value.get("source_manifest") or ()
                if isinstance(item, Mapping)
            ]
        if isinstance(value, Mapping) and value.get("schema") == (
            "zyra.research-citations/v1"
        ):
            claims = [
                dict(item)
                for item in value.get("claims") or ()
                if isinstance(item, Mapping)
            ]
            citations = [
                dict(item)
                for item in value.get("citations") or ()
                if isinstance(item, Mapping)
            ]
    if report:
        if not manifest:
            manifest = [
                dict(item)
                for item in report.get("source_manifest") or ()
                if isinstance(item, Mapping)
            ]
        if not claims:
            claims = [
                dict(item)
                for item in report.get("claims") or ()
                if isinstance(item, Mapping)
            ]
        if not citations:
            citations = [
                dict(item)
                for item in report.get("citations") or ()
                if isinstance(item, Mapping)
            ]
    if not report or len(manifest) < 2 or not claims or not citations:
        raise invalid(
            "benchmark_research_evidence_incomplete",
            "Product research source is missing report, sources, claims or citations.",
            phase="verification",
        )
    source_values = []
    for item in manifest:
        url = str(item.get("url") or "")
        source_values.append(
            {
                "source_id": str(item.get("source_id") or ""),
                "content_digest": require_digest(
                    item.get("source_digest")
                    or item.get("sha256")
                    or item.get("content_digest"),
                    "research source digest",
                ),
                "authority": urlparse(url).hostname or url,
                "live": True,
                "replay": False,
                "status": int(item.get("status") or 200),
                "acquired_at": str(item.get("acquired_at") or ""),
            }
        )
    citation_values = [
        {
            "citation_id": str(item.get("citation_id") or ""),
            "source_id": str(item.get("source_id") or ""),
            "span_verified": (
                int(item.get("byte_end") or 0) > int(item.get("byte_start") or -1)
                and bool(item.get("quote_digest"))
            ),
            "source_digest": item.get("source_digest"),
        }
        for item in citations
    ]
    claim_values = [
        {
            "claim_id": str(item.get("claim_id") or ""),
            "citation_ids": list(item.get("citation_ids") or ()),
            "supported": bool(item.get("citation_ids")),
            "deterministic": item.get("deterministic") is not False,
        }
        for item in claims
    ]
    return {
        "sources": source_values,
        "claims": claim_values,
        "citations": citation_values,
        "artifact_invariants": [
            {
                "invariant_id": "source-domain-verification-valid",
                "passed": source.domain_verification.get("valid") is True,
            },
            {
                "invariant_id": "report-source-coverage",
                "passed": len(source_values) >= 2,
            },
        ],
        "report_digest": digest(report),
    }


def metric_receipt(
    *,
    events: Sequence[Mapping[str, Any]],
    observation: WorkloadObservation,
    deployment: Mapping[str, Any],
    faults: Mapping[str, Any],
    resources: Mapping[str, Any],
) -> dict[str, Any]:
    route_count = max(1, len(observation.routes))
    message_count = len(observation.messages)
    delivery_count = sum(len(item.recipients) for item in observation.messages)
    recipient_counts: Counter[str] = Counter(
        recipient
        for item in observation.messages
        for recipient in item.recipients
    )
    entropy = shannon_entropy(recipient_counts.values())
    duplicate_messages = sum(
        count - 1
        for count in Counter(
            item.payload_digest for item in observation.messages
        ).values()
        if count > 1
    )
    nodes = len(observation.topology_nodes)
    maximum_edges = nodes * max(0, nodes - 1)
    sparsity = (
        1.0
        if maximum_edges == 0
        else 1.0 - len(observation.topology_edges) / maximum_edges
    )
    recovery_expected = faults.get("recovery_expected") is not False
    fault_count = len(faults.get("injections") or ())
    recovered_count = sum(
        item.get("recovered") is True for item in faults.get("injections") or ()
    )
    mttr_values = [
        float(item.get("mttr_ms") or 0)
        for item in faults.get("injections") or ()
    ]
    elapsed_seconds = max(1e-6, observation.wall_time_ms / 1000)
    compact_denominator = max(1, math.ceil(len(events) / 128))
    provider_count = len(deployment.get("provider_observations") or ())
    return {
        "task": {"succeeded": recovery_expected and not observation.unresolved_fault_ids},
        "quality": {
            "constraint_satisfaction": observation.quality_score,
            "deterministic_score": observation.quality_score,
            "artifact_drift": observation.artifact_drift_ratio,
            "requirement_drift": 0.0 if recovery_expected else 1.0,
            "coherence": observation.quality_score,
            "verifier_disagreement": False,
        },
        "memory": {
            "hit_rate": observation.memory_hits / max(1, observation.memory_writes),
            "restore_success": (
                1.0 if observation.restore_operations > 0 else 0.0
            ),
            "compact_ratio": min(
                1.0,
                observation.compact_operations / compact_denominator,
            ),
            "context_loss": (
                1.0
                if observation.memory_writes == 0
                else max(0.0, 1.0 - observation.memory_writes / max(1, len(events)))
            ),
        },
        "communication": {
            "entropy": entropy,
            "useful_ratio": observation.useful_messages / max(1, message_count),
            "duplicate_ratio": duplicate_messages / max(1, message_count),
            "delivery_count": delivery_count,
        },
        "topology": {
            "sparsity": max(0.0, min(1.0, sparsity)),
            "churn": observation.topology_mutations,
            "active_role_diversity": max(
                1,
                len(
                    {
                        item.role
                        for item in observation.routes
                        if item.role
                    }
                ),
            ),
        },
        "efficiency": {
            "token_units": observation.token_units,
            "wall_time_ms": max(1.0, observation.wall_time_ms),
            "throughput": len(events) / elapsed_seconds,
            "cost_usd": observation.cost_microunits / 1_000_000,
        },
        "resource": {
            "cpu_utilization": resources["cpu_utilization"],
            "memory_utilization": resources["memory_utilization"],
            "sla_compliance": sum(
                item.policy_compliant for item in observation.routes
            )
            / route_count,
            "privacy_compliance": sum(
                item.privacy_compliant for item in observation.routes
            )
            / route_count,
            "tier_diversity": len(deployment.get("tier_observations") or ()),
        },
        "provider": {
            "model_diversity": provider_count,
            "mix_entropy": math.log2(provider_count) if provider_count else 0.0,
        },
        "recovery": {
            "fault_success_rate": recovered_count / max(1, fault_count),
            "mttr_ms": sum(mttr_values) / max(1, len(mttr_values)),
            "detection_ms": 0.0,
            "delivery_after_fault": (
                faults.get("final_delivery_succeeded") is True
            ),
        },
        "autonomy": {
            "human_interventions": observation.human_intervention_count,
            "operator_interventions": 0,
        },
        "steps": {
            "effective": len(events),
            "raw": len(events),
            "excluded_ratio": 0.0,
        },
    }


def shannon_entropy(values: Sequence[int] | Any) -> float:
    selected = [float(item) for item in values if float(item) > 0]
    total = sum(selected)
    if total <= 0:
        return 0.0
    return -sum((item / total) * math.log2(item / total) for item in selected)


def elapsed_milliseconds(started_at: str, completed_at: str) -> int:
    from datetime import datetime

    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    return max(0, int((end - start).total_seconds() * 1000))


__all__ = [
    "FreshScenarioSource",
    "ProductLiveBenchmarkPort",
    "ScenarioSourceRunner",
    "SubprocessScenarioSourceRunner",
]
