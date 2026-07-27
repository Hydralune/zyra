#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute one isolated current-commit M3 live source task."
    )
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--domain",
        required=True,
        choices=("software-delivery", "cross-source-research"),
    )
    parser.add_argument("--input-revision", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    return parser.parse_args()


def configure_imports() -> None:
    for path in reversed(PACKAGE_PATHS):
        selected = str(path)
        if selected not in sys.path:
            sys.path.insert(0, selected)


def configure_environment(root: Path) -> None:
    environment = {
        "ZYRA_SQLITE_PATH": root / "api.sqlite3",
        "ZYRA_EVENT_LOG": root / "events.jsonl",
        "ZYRA_ARTIFACT_ROOT": root / "artifacts",
        "ZYRA_WORKER_POOL_STORE": root / "worker-pool.sqlite3",
        "ZYRA_GRAPH_STATE_STORE": root / "graph.sqlite3",
        "ZYRA_WORKSPACE_STATE_ROOT": root / "workspace-state",
        "ZYRA_WORKSPACE_DATA_ROOT": root / "workspace-data",
        "ZYRA_CONTROL_STATE": root / "control",
        "ZYRA_SUBAGENT_STATE": root / "subagents",
    }
    for key, value in environment.items():
        os.environ[key] = str(value)
    os.environ["ZYRA_SCENARIO_RUNNER_DISABLED"] = "0"
    os.environ["ZYRA_LIVE_SCHEDULER_DISABLED"] = "0"
    os.environ["ZYRA_LIVE_MEMORY_DISABLED"] = "0"
    os.environ["ZYRA_LIVE_RECOVERY_DISABLED"] = "0"
    os.environ["ZYRA_LIVE_LOW_ENTROPY_DISABLED"] = "0"
    os.environ["ZYRA_LIVE_VERIFIER_DISABLED"] = "0"


def fault_schedule(domain: str, revision: str) -> list[dict[str, Any]]:
    requirement = (
        "Preserve the new failure-path test and bind it to the input revision "
        f"{revision}."
        if domain == "software-delivery"
        else "Separate acceptance uncertainty from deterministic claims and bind "
        f"the revised criteria to {revision}."
    )
    if domain == "software-delivery":
        values = (
            ("requirement-change", "patch", "requirement_change", 240, "software-plan"),
            ("tool-exception", "test", "tool_exception", 480, "terminal-test"),
            ("worker-loss", "test", "worker_unavailable", 720, "software-worker-primary"),
            ("provider-failure", "verification", "provider_failure", 900, "provider-primary"),
            ("edge-disconnect", "delivery", "edge_network_loss", 1020, "edge-primary"),
        )
    else:
        values = (
            ("requirement-change", "source-index", "requirement_change", 220, "research-plan"),
            ("tool-timeout", "source-acquisition", "tool_timeout", 440, "http-source-worker"),
            ("node-loss", "claim-extraction", "node_lost", 660, "researcher-primary"),
            ("provider-rate-limit", "claim-extraction", "provider_rate_limit", 880, "provider-primary"),
            ("network-loss", "citation-verification", "network_loss", 1020, "edge-source-route"),
        )
    output: list[dict[str, Any]] = []
    for suffix, stage, kind, offset, target in values:
        payload: dict[str, Any] = {
            "benchmark_revision": revision,
            "failover_required": kind
            in {
                "worker_unavailable",
                "node_lost",
                "provider_failure",
                "provider_rate_limit",
                "edge_network_loss",
                "network_loss",
            },
        }
        if kind == "requirement_change":
            payload["requirement"] = requirement
        output.append(
            {
                "injection_id": f"m3-{revision}-{suffix}",
                "stage": stage,
                "kind": kind,
                "after_effective_step": offset,
                "target": target,
                "payload": payload,
            }
        )
    return output


def input_text(domain: str, revision: str) -> str:
    if domain == "software-delivery":
        return (
            "Implement a deterministic delivery marker for formal benchmark "
            f"revision {revision}, with executable tests, checksum-bound patch, "
            "clean-state receipts, autonomous recovery, and no external model call."
        )
    return json.dumps(
        {
            "question": (
                "For formal benchmark revision "
                f"{revision}, compare the semantics and operational roles of HTTP "
                "status registration, HTTP message semantics, and browser-facing "
                "status documentation using newly acquired exact source bytes."
            ),
            "source_urls": [
                "https://www.rfc-editor.org/rfc/rfc9110.html",
                "https://www.iana.org/assignments/http-status-codes/http-status-codes-1.csv",
                "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status",
            ],
            "requirements": [
                "Every material claim must resolve to exact newly acquired source bytes.",
                "At least one claim must be checked across two independent authorities.",
                "Checksums, citation spans, contradictions and uncertainty must be retained.",
            ],
            "privacy_class": "public",
            "maximum_cost_usd": 0.0,
            "maximum_latency_ms": 600000,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve(strict=False)
    outcome_path = Path(args.outcome).resolve(strict=False)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"source root must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if outcome_path.parent != root:
        raise RuntimeError("outcome must be written inside the isolated source root")
    configure_environment(root)
    configure_imports()

    from apps.api.zyra_api import main as api_main
    from apps.api.zyra_api.scenario_api import (
        get_scenario_runner_api,
        reset_scenario_runner_api,
    )
    from zyra_evaluation.scenario_runner.canonical import digest

    reset_scenario_runner_api(wait=True)
    api_main.reset_workspace_manager()
    api_main.reset_control_runtime()
    api_main.reset_subagent_runtime()
    api = get_scenario_runner_api()
    catalog = api.service.registry.catalog()
    policy = catalog["policies"][0]
    scenario_id = (
        "live.software-delivery"
        if args.domain == "software-delivery"
        else "live.cross-source-research"
    )
    request = {
        "scenario_id": scenario_id,
        "profile_id": "live.heterogeneous-sealed",
        "policy_id": policy["policy_id"],
        "policy_digest": policy["policy_digest"],
        "mode": "sealed",
        "input": input_text(args.domain, args.input_revision),
        "seed": args.seed,
        "faults": fault_schedule(args.domain, args.input_revision),
        "labels": {
            "slice": "M3-S02A-02",
            "input_revision": args.input_revision,
            "no_new_provider_call": "true",
        },
        "requested_by": "m3-live-benchmark",
    }
    created = api.service.create(request)
    completed = api.service.start(
        created.scenario_run_id,
        wait=True,
        timeout=args.timeout_seconds,
    )
    if completed.phase.value != "succeeded":
        raise RuntimeError(
            f"source scenario failed: {json.dumps(completed.to_dict(), ensure_ascii=False)}"
        )
    verification = api.service.verify(completed.scenario_run_id)
    archive_path = (
        root
        / "artifacts"
        / "causal-archives"
        / f"live-archive-{completed.scenario_run_id}"
        / "manifest.json"
    )
    if not archive_path.is_file():
        raise RuntimeError(f"causal archive manifest missing: {archive_path}")
    archive_manifest = json.loads(archive_path.read_text(encoding="utf-8"))
    outcome = {
        "schema": "zyra.m3-live-source-outcome/v1",
        "domain": args.domain,
        "input_revision": args.input_revision,
        "scenario_run": completed.to_dict(),
        "preflight_receipt": completed.preflight_receipt,
        "policy_decisions": list(completed.policy_decisions),
        "verification_receipt": verification,
        "archive_manifest_path": str(archive_path),
        "archive_manifest_digest": archive_manifest["manifest_digest"],
        "no_new_provider_call": True,
        "authenticated_provider_cli_invoked": False,
        "external_model_request_made": False,
    }
    outcome["outcome_digest"] = digest(outcome)
    outcome_path.write_text(
        json.dumps(outcome, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    reset_scenario_runner_api(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
