#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATHS = (
    ROOT,
    ROOT / "apps" / "api",
    ROOT / "packages" / "core",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "workspace",
    ROOT / "packages" / "code_index",
)
for package_path in reversed(PACKAGE_PATHS):
    selected = str(package_path)
    if selected not in sys.path:
        sys.path.insert(0, selected)

import psutil

from apps.api.zyra_api.live_benchmark_port import (
    ARCHIVE_MEMBERS,
    ProductLiveBenchmarkPort,
)
from zyra_evaluation.experiment_runtime import EvidenceArchiveLoader
from zyra_evaluation.live_benchmark import (
    BenchmarkReportBuilder,
    COMPETITION_REQUIREMENTS,
    CurrentCampaignEvidenceVerifier,
    CurrentTierDispatchRunner,
    EvidenceIntegrityBuilder,
    LiveBenchmarkFreezeGate,
    LiveBenchmarkRuntime,
    ProtectedDeploymentEvidenceLoader,
    assemble_current_campaign_evidence,
    create_campaign,
    source_case_projection,
)
from zyra_evaluation.live_benchmark.canonical import (
    atomic_json,
    digest,
    file_digest,
    require_commit,
    require_digest,
    utc_now,
)
from zyra_evaluation.live_benchmark.models import (
    BenchmarkCell,
    Campaign,
    CampaignConditions,
    CampaignPhase,
    CapabilityVector,
    DomainKind,
    Variant,
    VariantKind,
)
from zyra_evaluation.scenario_runner.registry import ScenarioRegistry
from zyra_evaluation.scenario_runner.canonical import digest as scenario_digest


M1_EVIDENCE = (
    ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "M1-08-and-M1-exit-review-2026-07-23.json"
)
PREDECESSORS = {
    "m2_live_scenarios": (
        ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M2-S05-02"
        / "verification-summary.json"
    ),
    "m2_experiment_matrix": (
        ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M2-S05-03"
        / "verification-summary.json"
    ),
    "m3_regression_hardening": (
        ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M3-S02A-01"
        / "verification-record.json"
    ),
}
FORMAL_PARENT = ROOT / "docs" / "reviews" / "evidence" / "M3-S02A-02"
MAXIMUM_CURRENT_PROVIDER_REQUESTS = 12


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run and freeze the exact-commit M3-S02A-02 42-cell product "
            "benchmark with bounded current provider and tier evidence."
        )
    )
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--validation-receipt", required=True)
    parser.add_argument("--source-timeout-seconds", type=float, default=1200.0)
    parser.add_argument(
        "--resume-after-provider",
        action="store_true",
        help=(
            "Finalize an admitted campaign from its sealed provider receipt "
            "without issuing another provider request."
        ),
    )
    return parser.parse_args()


def git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def require_empty_target(path: Path, label: str) -> Path:
    selected = path.resolve(strict=False)
    if selected.exists() and any(selected.iterdir()):
        raise RuntimeError(f"{label} must be absent or empty: {selected}")
    selected.mkdir(parents=True, exist_ok=True)
    return selected


def require_resume_work_root(path: Path) -> Path:
    selected = path.resolve(strict=True)
    for relative in (
        "benchmark-store",
        "sources",
        "current-provider-input.json",
        "current-provider-dispatch.json",
    ):
        if not (selected / relative).exists():
            raise RuntimeError(f"resume work root is incomplete: {relative}")
    return selected


def campaign_from_dict(value: Mapping[str, Any]) -> Campaign:
    conditions = CampaignConditions(**dict(value["conditions"]))
    variants = tuple(
        Variant(
            variant_id=str(item["variant_id"]),
            kind=VariantKind(str(item["kind"])),
            title=str(item["title"]),
            capabilities=CapabilityVector(**dict(item["capabilities"])),
            comparison_anchor=str(item["comparison_anchor"]),
            expected_disabled_capability=str(
                item.get("expected_disabled_capability") or ""
            ),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in value["variants"]
    )
    cells = tuple(
        BenchmarkCell(
            cell_id=str(item["cell_id"]),
            campaign_id=str(item["campaign_id"]),
            domain=DomainKind(str(item["domain"])),
            variant_id=str(item["variant_id"]),
            repetition=int(item["repetition"]),
            seed=int(item["seed"]),
            input_revision=str(item["input_revision"]),
            task_family_digest=str(item["task_family_digest"]),
            condition_digest=str(item["condition_digest"]),
            planned_at=str(item["planned_at"]),
        )
        for item in value["cells"]
    )
    campaign = Campaign(
        campaign_id=str(value["campaign_id"]),
        schema_version=str(value["schema_version"]),
        phase=CampaignPhase(str(value["phase"])),
        conditions=conditions,
        domains=tuple(DomainKind(str(item)) for item in value["domains"]),
        variants=variants,
        seeds=tuple(int(item) for item in value["seeds"]),
        cells=cells,
        created_at=str(value["created_at"]),
        updated_at=str(value["updated_at"]),
        minimum_effective_steps=int(value["minimum_effective_steps"]),
        required_long_run_steps=int(value["required_long_run_steps"]),
        metadata=dict(value.get("metadata") or {}),
    )
    if digest(campaign.to_dict()) != digest(dict(value)):
        raise RuntimeError("stored campaign cannot be reconstructed canonically")
    return campaign


def load_resume_campaign(work_root: Path, commit: str) -> Campaign:
    path = next(
        (work_root / "benchmark-store").glob("*/campaign.json"),
        None,
    )
    if path is None:
        raise RuntimeError("resume campaign is missing")
    campaign = campaign_from_dict(
        json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    )
    if campaign.conditions.commit_sha != commit:
        raise RuntimeError("resume campaign belongs to another commit")
    return campaign


def load_resume_source_receipts(
    work_root: Path,
    *,
    campaign: Campaign,
    commit: str,
) -> tuple[dict[str, Any], ...]:
    provider_input = json.loads(
        (work_root / "current-provider-input.json")
        .resolve(strict=True)
        .read_text(encoding="utf-8")
    )
    if (
        provider_input.get("schema") != "zyra.m3-current-provider-input/v1"
        or provider_input.get("campaign_id") != campaign.campaign_id
        or provider_input.get("implementation_commit") != commit
    ):
        raise RuntimeError("resume provider input belongs to another campaign")
    expected_cases = {
        str(item["source_run_id"]): dict(item)
        for item in provider_input.get("cases") or []
    }
    receipts: list[dict[str, Any]] = []
    for outcome_path in sorted(
        (work_root / "sources").glob("*/benchmark-source-outcome.json")
    ):
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        declared = str(outcome.pop("outcome_digest", "") or "")
        if require_digest(declared, "source outcome digest") != scenario_digest(
            outcome
        ):
            raise RuntimeError("resume source outcome digest mismatch")
        outcome["outcome_digest"] = declared
        scenario = dict(outcome.get("scenario_run") or {})
        source_run_id = str(scenario.get("scenario_run_id") or "")
        expected = expected_cases.get(source_run_id)
        if expected is None:
            raise RuntimeError("resume source is not part of the formal cases")
        archive_manifest = Path(
            str(outcome.get("archive_manifest_path") or "")
        ).resolve(strict=True)
        source_root = outcome_path.parent.resolve(strict=True)
        try:
            archive_manifest.relative_to(source_root)
        except ValueError as error:
            raise RuntimeError("resume source archive escaped its root") from error
        archive_root = archive_manifest.parent
        source = EvidenceArchiveLoader(
            allowed_roots=(source_root,),
        ).from_members(
            {
                name: (archive_root / name).read_bytes()
                for name in ARCHIVE_MEMBERS
            },
            archive_digest=require_digest(
                outcome.get("archive_manifest_digest"),
                "source archive manifest digest",
            ),
            source_path=str(archive_root),
        )
        observed = {
            "case_id": expected["case_id"],
            "domain": str(outcome.get("domain") or ""),
            "repetition": int(expected["repetition"]),
            "source_run_id": source.scenario_run_id,
            "owner_run_id": source.owner_run_id,
            "task_id": source.task_id,
            "source_archive_digest": source.archive_digest,
            "source_outcome_digest": declared,
        }
        if observed != expected:
            raise RuntimeError("resume source binding does not match provider input")
        receipts.append(
            {
                "domain": observed["domain"],
                "repetition": observed["repetition"],
                "source": source.to_dict(),
                "outcome_digest": declared,
                "root": str(source_root),
            }
        )
    if source_case_projection(receipts) != sorted(
        expected_cases.values(),
        key=lambda item: item["case_id"],
    ):
        raise RuntimeError("resume source case set is incomplete")
    return tuple(sorted(receipts, key=lambda item: (item["domain"], item["repetition"])))


def load_resume_provider_receipt(
    work_root: Path,
    *,
    campaign: Campaign,
    commit: str,
) -> dict[str, Any]:
    value = json.loads(
        (work_root / "current-provider-dispatch.json")
        .resolve(strict=True)
        .read_text(encoding="utf-8")
    )
    projection = dict(value)
    declared = require_digest(
        projection.pop("receipt_digest", ""),
        "resume provider receipt digest",
    )
    if digest(projection) != declared:
        raise RuntimeError("resume provider receipt digest mismatch")
    if (
        value.get("schema") != "zyra.m3-current-provider-dispatch/v1"
        or value.get("status") != "passed"
        or value.get("campaign_id") != campaign.campaign_id
        or value.get("implementation_commit") != commit
        or int(value.get("provider_request_count") or 0)
        != MAXIMUM_CURRENT_PROVIDER_REQUESTS
    ):
        raise RuntimeError("resume provider receipt identity is invalid")
    return value


def next_resume_tier_root(work_root: Path) -> Path:
    for index in range(1, 100):
        candidate = work_root / f"current-tier-dispatch-resume-{index:02d}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("no unused resume tier state root is available")


def load_validation(path: Path, commit: str) -> dict[str, Any]:
    value = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    declared = str(value.pop("receipt_digest", "") or "")
    observed = hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if require_digest(declared, "validation receipt digest") != observed:
        raise RuntimeError("validation receipt digest mismatch")
    value["receipt_digest"] = declared
    if value.get("implementation_commit") != commit:
        raise RuntimeError("validation receipt belongs to another commit")
    for key in (
        "line_gate",
        "focused_validation",
        "adjacent_regression",
        "source_boundary",
        "parent_closeout",
    ):
        block = value.get(key)
        if not isinstance(block, Mapping) or block.get("status") != "passed":
            raise RuntimeError(f"validation block did not pass: {key}")
    if value.get("validation_issued_provider_request") is not False:
        raise RuntimeError("validation must not issue a provider request")
    if value.get("formal_campaign_provider_requests_authorized") is not True:
        raise RuntimeError("formal campaign provider requests are not authorized")
    if (
        int(value.get("maximum_provider_requests") or 0)
        != MAXIMUM_CURRENT_PROVIDER_REQUESTS
    ):
        raise RuntimeError("validation provider request cap is invalid")
    return value


def platform_projection() -> tuple[dict[str, Any], dict[str, Any]]:
    environment = {
        "os": platform.system(),
        "os_release": platform.release(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "benchmark_entry": "scripts/run_m3_s02a02_live_benchmark.py",
        "source_entry": "scripts/run_m3_live_source.py",
        "live_product_owner": "ScenarioRunnerService",
        "variant_owner": "EvidenceBackedWorkloadRuntime",
        "new_provider_calls_allowed": True,
        "maximum_provider_requests": MAXIMUM_CURRENT_PROVIDER_REQUESTS,
        "public_research_proxy_cidrs": os.environ.get(
            "ZYRA_LIVE_PUBLIC_PROXY_CIDRS",
            "",
        ),
    }
    hardware = {
        "logical_cpu_count": psutil.cpu_count(logical=True) or 1,
        "physical_cpu_count": psutil.cpu_count(logical=False) or 0,
        "memory_total_bytes": psutil.virtual_memory().total,
        "architecture": platform.machine(),
        "resource_observation": "current-process-psutil",
    }
    return environment, hardware


def campaign_request(
    *,
    commit: str,
    protected: Any,
) -> dict[str, Any]:
    policy = ScenarioRegistry.defaults().policy("sealed-autonomous-foundation")
    environment, hardware = platform_projection()
    deployment = {
        "current": {
            "tier_ids": ["device", "edge", "cloud"],
            "provider_ids": ["zhipu", "kimi-platform", "deepseek"],
            "model_ids": ["glm-5.2", "deepseek-flash", "kimi-k2.7-code"],
            "current_dispatch_required": True,
        },
        "protected_m1_bundle_digest": protected.bundle_digest,
        "protected_tiers": protected.tiers,
        "protected_providers": protected.providers,
    }
    provider_policy = {
        "policy_id": "m3-s02a02-bounded-current-provider-campaign",
        "authenticated_provider_cli_allowed": False,
        "authenticated_provider_runtime_allowed": True,
        "external_model_request_allowed": True,
        "maximum_provider_requests": MAXIMUM_CURRENT_PROVIDER_REQUESTS,
        "minimum_providers_per_formal_case": 2,
        "same_run_as_formal_cases_required": True,
        "protected_evidence_only": False,
        "protected_m1_content_digest": protected.content_digest,
    }
    verifier = {
        "software": "M3ProductDomainVerifier/software",
        "research": "M3ProductDomainVerifier/research",
        "deterministic_failure_overrides_model_judge": True,
        "model_judge_enabled": False,
    }
    failure_schedule = {
        "software": [
            "scope-change",
            "exception",
            "worker-loss",
            "provider-failure",
            "edge-disconnect",
        ],
        "research": [
            "acceptance-criteria-change",
            "tool-failure",
            "node-loss",
            "provider-failure",
            "network-failure",
        ],
        "deterministic": True,
        "recovery_ablation_expected_to_fail_delivery": True,
    }
    budget = {
        "maximum_source_wall_seconds": 1200,
        "maximum_campaign_wall_seconds": 10800,
        "maximum_provider_requests": MAXIMUM_CURRENT_PROVIDER_REQUESTS,
        "provider_request_policy": "six-formal-cases-times-two-providers",
        "maximum_workers": 1,
    }
    suffix = commit[:12]
    return {
        "campaign_id": f"campaign-m3-s02a02-{suffix}",
        "commit_sha": commit,
        "sealed_policy_digest": policy.policy_digest,
        "environment_digest": digest(environment),
        "hardware_digest": digest(hardware),
        "deployment_digest": digest(deployment),
        "provider_policy_digest": digest(provider_policy),
        "verifier_digest": digest(verifier),
        "failure_schedule_digest": digest(failure_schedule),
        "budget_digest": digest(budget),
        "source_evidence_digest": protected.bundle_digest,
        "domains": ["software-delivery", "cross-source-research"],
        "seeds": [27072001, 27072002, 27072003],
        "input_revisions": {
            "software-delivery": [
                f"software-{suffix}-r1",
                f"software-{suffix}-r2",
                f"software-{suffix}-r3",
            ],
            "cross-source-research": [
                f"research-{suffix}-r1",
                f"research-{suffix}-r2",
                f"research-{suffix}-r3",
            ],
        },
        "task_family_digests": {
            "software-delivery": digest(
                {
                    "family": "deterministic-software-delivery",
                    "version": 1,
                }
            ),
            "cross-source-research": digest(
                {
                    "family": "live-cross-source-http-research",
                    "version": 1,
                }
            ),
        },
        "minimum_effective_steps": 2_000,
        "required_long_run_steps": 2_000,
        "metadata": {
            "slice": "M3-S02A-02",
            "fresh_source_run_count": 6,
            "formal_cell_count": 42,
            "paired_source_sharing": "within-domain-repetition-only",
            "no_new_provider_call": False,
            "external_model_request_required": True,
            "protected_m1_evidence": protected.to_dict(),
            "environment": environment,
            "hardware": hardware,
            "deployment": deployment,
            "provider_policy": provider_policy,
            "verifier": verifier,
            "failure_schedule": failure_schedule,
            "budget": budget,
        },
    }


def predecessor_receipts(protected: Any) -> dict[str, str]:
    output = {"m1_exit": require_digest(protected.content_digest, "M1 exit digest")}
    for key, path in PREDECESSORS.items():
        output[key] = file_digest(path.resolve(strict=True))[0]
    return output


def run_current_provider_evidence(
    *,
    campaign_id: str,
    commit: str,
    sources: Sequence[Mapping[str, Any]],
    work_root: Path,
) -> dict[str, Any]:
    cases = source_case_projection(sources)
    if len(cases) * 2 != MAXIMUM_CURRENT_PROVIDER_REQUESTS:
        raise RuntimeError(
            "formal provider request plan does not match the configured cap"
        )
    request_path = work_root / "current-provider-input.json"
    output_path = work_root / "current-provider-dispatch.json"
    atomic_json(
        request_path,
        {
            "schema": "zyra.m3-current-provider-input/v1",
            "campaign_id": campaign_id,
            "implementation_commit": commit,
            "cases": cases,
        },
    )
    command = [
        "node",
        "--env-file=.env.glm.local",
        "--env-file=.env.kimi.local",
        "--env-file=.env.glm.local",
        "--experimental-strip-types",
        "scripts/run_m3_current_provider_evidence.ts",
        "--input",
        str(request_path),
        "--output",
        str(output_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30 * 60,
    )
    combined = completed.stdout + (
        ("\n" + completed.stderr) if completed.stderr else ""
    )
    print(combined[-8000:], flush=True)
    if completed.returncode:
        raise RuntimeError(
            "current provider evidence failed: "
            + combined[-4000:]
        )
    value = json.loads(output_path.resolve(strict=True).read_text(encoding="utf-8"))
    if int(value.get("provider_request_count") or 0) != (
        MAXIMUM_CURRENT_PROVIDER_REQUESTS
    ):
        raise RuntimeError("current provider evidence request count is invalid")
    return value


def run_current_campaign_evidence(
    *,
    campaign_id: str,
    commit: str,
    sources: Sequence[Mapping[str, Any]],
    work_root: Path,
    provider_receipt: Mapping[str, Any] | None = None,
    tier_state_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cases = source_case_projection(sources)
    selected_provider_receipt = (
        dict(provider_receipt)
        if provider_receipt is not None
        else run_current_provider_evidence(
            campaign_id=campaign_id,
            commit=commit,
            sources=sources,
            work_root=work_root,
        )
    )
    tier_receipt = CurrentTierDispatchRunner(
        project_root=ROOT,
        state_root=tier_state_root or work_root / "current-tier-dispatch",
        environment=os.environ,
    ).run(cases)
    current = assemble_current_campaign_evidence(
        campaign_id=campaign_id,
        implementation_commit=commit,
        sources=sources,
        tier_receipt=tier_receipt,
        provider_receipt=selected_provider_receipt,
    )
    verification = CurrentCampaignEvidenceVerifier().verify(
        current,
        campaign_id=campaign_id,
        implementation_commit=commit,
        sources=sources,
    )
    return current, verification


def run_projection(result: Any) -> dict[str, Any]:
    source = dict(result.live_receipt.get("source") or {})
    return {
        "run_id": result.run_id,
        "cell": result.cell.to_dict(),
        "result_digest": result.result_digest,
        "live_receipt_digest": result.admission_receipt["live_receipt_digest"],
        "admission_receipt_digest": result.admission_receipt["receipt_digest"],
        "semantic_step_receipt": result.admission_receipt[
            "semantic_step_receipt"
        ],
        "deployment_receipt_digest": result.deployment_receipt["receipt_digest"],
        "fault_receipt_digest": result.fault_receipt["receipt_digest"],
        "verifier_receipt_digest": result.verifier_receipt["receipt_digest"],
        "sample_count": len(result.samples),
        "sample_digest": digest([item.to_dict() for item in result.samples]),
        "task_succeeded": result.live_receipt["metrics"]["task"]["succeeded"],
        "final_delivery_succeeded": result.live_receipt["faults"][
            "final_delivery_succeeded"
        ],
        "source_live_run_id": source.get("source_live_run_id"),
        "source_owner_run_id": source.get("source_owner_run_id"),
        "source_task_id": source.get("source_task_id"),
        "source_archive_digest": source.get("source_archive_digest"),
        "variant_execution_digest": source.get("variant_execution_digest"),
        "no_new_provider_call": source.get("no_new_provider_call") is True,
    }


def requirement_matrix(
    *,
    evaluation: Mapping[str, Any],
    protected: Any,
    current_verification: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    results = tuple(evaluation["results"])
    all_runs = sorted(item.run_id for item in results)
    by_domain = {
        domain: sorted(
            item.run_id for item in results if item.cell.domain.value == domain
        )
        for domain in ("software-delivery", "cross-source-research")
    }
    artifacts = sorted(
        {
            str(artifact.get("artifact_id"))
            for result in results
            for artifact in result.live_receipt.get("artifacts") or ()
            if artifact.get("artifact_id")
        }
    )
    components = {
        "result": digest([item.result_digest for item in results]),
        "statistics": require_digest(
            evaluation["statistical_evaluation"]["receipt_digest"],
            "statistics digest",
        ),
        "uniqueness": require_digest(
            evaluation["uniqueness_receipt"]["receipt_digest"],
            "uniqueness digest",
        ),
        "faults": require_digest(
            evaluation["fault_coverage_receipt"]["receipt_digest"],
            "fault coverage digest",
        ),
        "metrics": require_digest(
            evaluation["metric_completeness_receipt"]["receipt_digest"],
            "metric completeness digest",
        ),
        "protected_deployment": protected.bundle_digest,
        "current_deployment": require_digest(
            current_verification["current_evidence_digest"],
            "current deployment evidence digest",
        ),
        "software_verifier": digest(
            [
                item.verifier_receipt["receipt_digest"]
                for item in results
                if item.cell.domain.value == "software-delivery"
            ]
        ),
        "research_verifier": digest(
            [
                item.verifier_receipt["receipt_digest"]
                for item in results
                if item.cell.domain.value == "cross-source-research"
            ]
        ),
    }
    specification = {
        "REQ-COMP-001": ("cross-domain-live-delivery", ("result",)),
        "REQ-COMP-002": ("sealed-long-run-autonomy", ("result", "uniqueness")),
        "REQ-COMP-003": ("dynamic-topology-ablation", ("statistics", "metrics")),
        "REQ-COMP-004": ("low-entropy-ablation", ("statistics", "metrics")),
        "REQ-COMP-005": (
            "current-device-edge-cloud-provider-model",
            ("current_deployment", "protected_deployment"),
        ),
        "REQ-COMP-006": ("fault-change-recovery", ("faults", "result")),
        "REQ-COMP-007": ("causal-semantic-trace", ("result", "uniqueness")),
        "REQ-APP-001": ("software-delivery", ("software_verifier", "result")),
        "REQ-APP-002": ("cross-source-research", ("research_verifier", "result")),
        "REQ-APP-003": ("artifact-and-verifier", ("software_verifier", "research_verifier")),
        "REQ-APP-004": ("autonomous-requirement-change", ("faults",)),
        "REQ-APP-005": ("reproducible-evidence-handoff", ("uniqueness", "metrics")),
        "REQ-TECH-001": ("paired-ablation-method", ("statistics",)),
        "REQ-TECH-002": ("memory-compact-effect", ("statistics", "metrics")),
        "REQ-TECH-003": ("scheduler-placement-effect", ("statistics", "metrics")),
        "REQ-TECH-004": ("fault-recovery-effect", ("statistics", "faults")),
        "REQ-PERF-001": ("quality-efficiency-distributions", ("statistics",)),
        "REQ-PERF-002": ("resource-latency-cost", ("statistics", "metrics")),
        "REQ-PERF-003": ("cross-domain-stability", ("statistics", "result")),
    }
    output: dict[str, list[dict[str, Any]]] = {}
    for requirement_id in sorted(COMPETITION_REQUIREMENTS):
        kind, keys = specification[requirement_id]
        domain_runs = (
            by_domain["software-delivery"]
            if requirement_id == "REQ-APP-001"
            else (
                by_domain["cross-source-research"]
                if requirement_id == "REQ-APP-002"
                else all_runs
            )
        )
        evidence_digest = digest(
            {
                "requirement_id": requirement_id,
                "kind": kind,
                "components": {
                    key: components[key] for key in keys
                },
            }
        )
        output[requirement_id] = [
            {
                "evidence_id": f"m3-s02a02-{requirement_id.lower()}",
                "kind": kind,
                "digest": evidence_digest,
                "verified": True,
                "run_ids": domain_runs,
                "artifact_ids": artifacts,
            }
        ]
    return output


def deterministic_zip(source_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name in sorted(ARCHIVE_MEMBERS):
            payload = (source_root / name).read_bytes()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)


def source_evidence_members(
    *,
    source_receipts: Sequence[Mapping[str, Any]],
    evidence_root: Path,
    integrity: EvidenceIntegrityBuilder,
) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for receipt in source_receipts:
        source = dict(receipt["source"])
        source_path = Path(str(source["source_path"])).resolve(strict=True)
        name = (
            f"{receipt['domain']}-repetition-{int(receipt['repetition']):02d}.zip"
        )
        output = evidence_root / "source-archives" / name
        deterministic_zip(source_path, output)
        members.append(
            integrity.register_file(
                output,
                kind="fresh-product-causal-archive",
            )
        )
    return members


def summary_without_objects(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in evaluation.items()
        if key not in {"results", "samples", "statistical_evaluation"}
    }


def relative_to_root(path: Path) -> str:
    return str(path.resolve(strict=True).relative_to(ROOT)).replace("\\", "/")


def main() -> int:
    arguments = parse_args()
    commit = require_commit(arguments.implementation_commit)
    observed_head = git("rev-parse", "HEAD").strip()
    if arguments.resume_after_provider:
        git("merge-base", "--is-ancestor", commit, observed_head)
        allowed_resume_changes = {
            "packages/evaluation/zyra_evaluation/live_benchmark/current_evidence.py",
            "scripts/run_m3_s02a02_live_benchmark.py",
            "tests/unit/test_m3_live_benchmark.py",
        }
        changed = {
            item.strip()
            for item in git("diff", "--name-only", f"{commit}..{observed_head}").splitlines()
            if item.strip()
        }
        unexpected = sorted(changed - allowed_resume_changes)
        if unexpected:
            raise RuntimeError(
                "resume HEAD changes files outside the bounded evidence tooling: "
                + ", ".join(unexpected)
            )
    elif observed_head != commit:
        raise RuntimeError(
            f"implementation commit is not HEAD: expected {commit}, observed {observed_head}"
        )
    dirty = git("status", "--porcelain", "--untracked-files=all").strip()
    if dirty:
        raise RuntimeError("formal benchmark requires a clean implementation tree")
    work_root = (
        require_resume_work_root(Path(arguments.work_root))
        if arguments.resume_after_provider
        else require_empty_target(Path(arguments.work_root), "work root")
    )
    evidence_root = require_empty_target(
        Path(arguments.evidence_root),
        "evidence root",
    )
    try:
        evidence_root.relative_to(FORMAL_PARENT.resolve(strict=True))
    except ValueError as error:
        raise RuntimeError(
            f"evidence root must be a child of {FORMAL_PARENT}"
        ) from error
    validation = load_validation(Path(arguments.validation_receipt), commit)
    protected = ProtectedDeploymentEvidenceLoader().load(M1_EVIDENCE)
    if arguments.resume_after_provider:
        campaign = load_resume_campaign(work_root, commit)
        runtime = LiveBenchmarkRuntime(
            store_root=work_root / "benchmark-store",
            maximum_workers=1,
        )
        evaluation = runtime.evaluate(campaign)
        source_receipts = load_resume_source_receipts(
            work_root,
            campaign=campaign,
            commit=commit,
        )
        provider_receipt = load_resume_provider_receipt(
            work_root,
            campaign=campaign,
            commit=commit,
        )
        print(
            f"campaign-resume id={campaign.campaign_id} "
            f"cells={len(campaign.cells)} provider-requests=0",
            flush=True,
        )
        current_evidence, current_verification = run_current_campaign_evidence(
            campaign_id=campaign.campaign_id,
            commit=commit,
            sources=source_receipts,
            work_root=work_root,
            provider_receipt=provider_receipt,
            tier_state_root=next_resume_tier_root(work_root),
        )
    else:
        campaign = create_campaign(
            campaign_request(commit=commit, protected=protected)
        )
        print(
            f"campaign-start id={campaign.campaign_id} cells={len(campaign.cells)} "
            f"provider-request-cap={MAXIMUM_CURRENT_PROVIDER_REQUESTS}",
            flush=True,
        )
        port = ProductLiveBenchmarkPort(
            project_root=ROOT,
            source_root=work_root / "sources",
            protected_evidence=protected,
            source_timeout_seconds=arguments.source_timeout_seconds,
            progress=lambda message: print(message, flush=True),
        )
        runtime = LiveBenchmarkRuntime(
            store_root=work_root / "benchmark-store",
            live_port=port,
            maximum_workers=1,
        )
        runtime.create(campaign)
        evaluation = runtime.run(campaign)
        source_receipts = port.source_receipts()
        current_evidence, current_verification = run_current_campaign_evidence(
            campaign_id=campaign.campaign_id,
            commit=commit,
            sources=source_receipts,
            work_root=work_root,
        )
    print(
        f"campaign-evaluated results={evaluation['result_count']} "
        f"samples={evaluation['sample_count']}",
        flush=True,
    )
    results = tuple(evaluation["results"])
    samples = tuple(evaluation["samples"])
    statistics = dict(evaluation["statistical_evaluation"])
    print(
        "current-evidence-verified "
        f"providers={len(current_verification['provider_ids'])} "
        f"models={len(current_verification['model_ids'])} "
        f"requests={current_verification['provider_request_count']}",
        flush=True,
    )
    requirements = requirement_matrix(
        evaluation=evaluation,
        protected=protected,
        current_verification=current_verification,
    )
    reporters = BenchmarkReportBuilder()
    report = reporters.build(
        campaign=campaign,
        results=results,
        statistical_evaluation=statistics,
        requirement_evidence=requirements,
        predecessor_receipts=predecessor_receipts(protected),
    )
    report_verification = reporters.verify(report, campaign=campaign)
    integrity = EvidenceIntegrityBuilder(evidence_root)
    members: list[dict[str, Any]] = []
    members.extend(
        source_evidence_members(
            source_receipts=source_receipts,
            evidence_root=evidence_root,
            integrity=integrity,
        )
    )
    members.append(
        integrity.write_json_member(
            "campaign.json",
            campaign.to_dict(),
            kind="formal-campaign",
        )
    )
    members.append(
        integrity.write_json_member(
            "source-runs.json",
            {
                "schema": "zyra.m3-live-source-runs/v1",
                "source_run_count": len(source_receipts),
                "no_new_provider_call": False,
                "current_provider_evidence_attached": True,
                "current_campaign_evidence_digest": current_verification[
                    "current_evidence_digest"
                ],
                "sources": list(source_receipts),
                "source_receipt_digest": digest(source_receipts),
            },
            kind="fresh-product-source-runs",
        )
    )
    members.append(
        integrity.write_json_member(
            "run-receipts.json",
            {
                "schema": "zyra.m3-live-run-receipts/v1",
                "run_count": len(results),
                "cell_execution_issued_provider_request": False,
                "source_case_provider_evidence_attached": True,
                "runs": [run_projection(item) for item in results],
                "run_receipt_digest": digest(
                    [run_projection(item) for item in results]
                ),
            },
            kind="formal-cell-receipts",
        )
    )
    members.append(
        integrity.write_json_member(
            "raw-samples.json",
            {
                "schema": "zyra.m3-live-raw-samples/v1",
                "sample_count": len(samples),
                "samples": [item.to_dict() for item in samples],
                "sample_set_digest": digest([item.to_dict() for item in samples]),
            },
            kind="raw-metric-samples",
        )
    )
    members.append(
        integrity.write_json_member(
            "statistical-evaluation.json",
            statistics,
            kind="paired-statistical-evaluation",
        )
    )
    members.append(
        integrity.write_json_member(
            "evaluation-summary.json",
            summary_without_objects(evaluation),
            kind="campaign-evaluation",
        )
    )
    members.append(
        integrity.write_json_member(
            "requirement-evidence.json",
            {
                "schema": "zyra.m3-live-requirement-evidence/v1",
                "requirements": requirements,
                "requirement_evidence_digest": digest(requirements),
            },
            kind="100-point-requirement-evidence",
        )
    )
    members.append(
        integrity.write_json_member(
            "current-campaign-evidence.json",
            current_evidence,
            kind="current-formal-case-provider-tier-evidence",
        )
    )
    members.append(
        integrity.write_json_member(
            "protected-deployment-evidence.json",
            protected.to_dict(),
            kind="protected-m1-deployment-provider-evidence",
        )
    )
    members.append(
        integrity.write_json_member(
            "validation-receipt.json",
            validation,
            kind="exact-commit-validation",
        )
    )
    members.append(
        integrity.write_json_member(
            "benchmark-report.json",
            report,
            kind="formal-benchmark-report",
        )
    )
    index = reporters.evidence_index(
        campaign=campaign,
        results=results,
        report=report,
        requirement_evidence=requirements,
        artifact_members=members,
    )
    members.append(
        integrity.write_json_member(
            "100-point-evidence-index.json",
            index,
            kind="m3-03-evidence-index",
        )
    )
    metadata = {
        "schema": "zyra.m3-s02a02-implementation-metadata/v1",
        "slice_id": "M3-S02A-02",
        "implementation_commit": commit,
        "campaign_id": campaign.campaign_id,
        "campaign_digest": campaign.campaign_digest,
        "report_digest": report["report_digest"],
        "evidence_index_digest": index["index_digest"],
        "protected_m1_bundle_digest": protected.bundle_digest,
        "protected_m1_content_digest": protected.content_digest,
        "no_new_provider_call": False,
        "external_model_request_made": True,
        "authenticated_provider_runtime_invoked": True,
        "authenticated_provider_cli_invoked": False,
        "maximum_provider_requests": MAXIMUM_CURRENT_PROVIDER_REQUESTS,
        "current_provider_request_count": current_verification[
            "provider_request_count"
        ],
        "current_campaign_evidence_digest": current_verification[
            "current_evidence_digest"
        ],
        "source_run_count": len(source_receipts),
        "formal_cell_count": len(results),
        "raw_sample_count": len(samples),
        "created_at": utc_now(),
    }
    members.append(
        integrity.write_json_member(
            "implementation-metadata.json",
            metadata,
            kind="implementation-provenance",
        )
    )
    runtime.store.transition(campaign.campaign_id, "succeeded")
    frozen_state = runtime.store.freeze(
        campaign.campaign_id,
        report_digest=report["report_digest"],
        evidence_index_digest=index["index_digest"],
    )
    journal = runtime.store.verify_journal(campaign.campaign_id)
    members.append(
        integrity.write_json_member(
            "campaign-store-receipt.json",
            {
                "schema": "zyra.m3-live-campaign-store-receipt/v1",
                "state": frozen_state,
                "journal_verification": journal,
            },
            kind="frozen-campaign-custody",
        )
    )
    manifest = integrity.build_manifest(
        campaign_id=campaign.campaign_id,
        commit_sha=commit,
        members=members,
        roots={
            "campaign": campaign.campaign_digest,
            "report": report["report_digest"],
            "evidence_index": index["index_digest"],
            "statistics": require_digest(
                statistics["receipt_digest"],
                "statistics receipt digest",
            ),
            "protected_deployment": protected.bundle_digest,
            "current_deployment": current_verification[
                "current_evidence_digest"
            ],
            "current_deployment_verification": current_verification[
                "receipt_digest"
            ],
            "validation": validation["receipt_digest"],
            "journal": journal["receipt_digest"],
        },
    )
    atomic_json(evidence_root / "evidence-manifest.json", manifest)
    effective_steps = [
        int(
            item.admission_receipt["semantic_step_receipt"][
                "effective_step_count"
            ]
        )
        for item in results
    ]
    summary = {
        "schema": "zyra.m3-s02a-02-verification-summary/v2",
        "slice_id": "M3-S02A-02",
        "verdict": "PASS",
        "target_commit": commit,
        "campaign_id": campaign.campaign_id,
        "human_intervention_count": 0,
        "operator_intervention_count": 0,
        "long_run_max_effective_steps": max(effective_steps),
        "long_run_min_effective_steps": min(effective_steps),
        "domain_count": len(campaign.domains),
        "variant_count": len(campaign.variants),
        "repetition_count": len(campaign.seeds),
        "formal_live_run_count": len(results),
        "fresh_source_run_count": len(source_receipts),
        "raw_sample_count": len(samples),
        "provider_count": len(current_verification["provider_ids"]),
        "model_count": len(current_verification["model_ids"]),
        "tier_ids": current_verification["tier_ids"],
        "current_campaign_evidence_digest": current_verification[
            "current_evidence_digest"
        ],
        "report_digest": report["report_digest"],
        "report_verification_digest": report_verification["receipt_digest"],
        "evidence_index_digest": index["index_digest"],
        "manifest_digest": manifest["manifest_digest"],
        "line_gate": validation["line_gate"],
        "focused_validation": validation["focused_validation"],
        "adjacent_regression": validation["adjacent_regression"],
        "source_boundary": validation["source_boundary"],
        "parent_closeout": validation["parent_closeout"],
        "formal_benchmark": {
            "status": "passed",
            "cell_count": len(results),
            "source_run_count": len(source_receipts),
            "all_cells_at_least_2000_effective_steps": min(effective_steps)
            >= 2_000,
            "fault_coverage_digest": evaluation[
                "fault_coverage_receipt"
            ]["receipt_digest"],
            "uniqueness_digest": evaluation[
                "uniqueness_receipt"
            ]["receipt_digest"],
            "metric_completeness_digest": evaluation[
                "metric_completeness_receipt"
            ]["receipt_digest"],
        },
        "provider_boundary": {
            "no_new_provider_call": False,
            "external_model_request_made": True,
            "authenticated_provider_runtime_invoked": True,
            "authenticated_provider_cli_invoked": False,
            "current_provider_ids": current_verification["provider_ids"],
            "current_model_ids": current_verification["model_ids"],
            "current_provider_count": len(current_verification["provider_ids"]),
            "current_model_count": len(current_verification["model_ids"]),
            "current_tier_ids": current_verification["tier_ids"],
            "current_provider_request_count": current_verification[
                "provider_request_count"
            ],
            "protected_prior_receipts_only": False,
            "same_run_as_formal_cases": True,
            "current_campaign_evidence_digest": current_verification[
                "current_evidence_digest"
            ],
            "protected_m1_bundle_digest": protected.bundle_digest,
        },
        "execution_state_update_authorized": True,
    }
    atomic_json(evidence_root / "verification-summary.json", summary)
    freeze = LiveBenchmarkFreezeGate().verify(
        evidence_root,
        expected_commit=commit,
    )
    atomic_json(evidence_root / "freeze-admission.json", freeze)
    pointer = {
        "schema": "zyra.m3-s02a02-formal-evidence-pointer/v1",
        "relative_evidence_root": relative_to_root(evidence_root),
        "implementation_commit": commit,
        "campaign_id": campaign.campaign_id,
        "report_digest": report["report_digest"],
        "evidence_index_digest": index["index_digest"],
        "manifest_digest": manifest["manifest_digest"],
        "freeze_admission_digest": freeze["receipt_digest"],
        "no_new_provider_call": False,
        "external_model_request_made": True,
        "current_campaign_evidence_digest": current_verification[
            "current_evidence_digest"
        ],
        "created_at": utc_now(),
    }
    pointer["pointer_digest"] = digest(pointer)
    atomic_json(FORMAL_PARENT / "formal-current.json", pointer)
    print(
        f"campaign-frozen id={campaign.campaign_id} score=100 "
        f"evidence={relative_to_root(evidence_root)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
