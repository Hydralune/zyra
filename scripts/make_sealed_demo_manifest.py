"""Generate the sealed long-run demo manifest for the current checkout.

The sealed runner (``scripts/run_phase2_sealed_scenarios.py``) requires a
manifest that freezes the candidate commit, every protected file digest, the
model profile and the network allowlist.  This helper rebuilds that manifest
from the current ``HEAD`` so the demo stays reproducible after any commit.

Usage (run from the repository root):
    python scripts/make_sealed_demo_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files whose digests the sealed manifest freezes.  The runner re-verifies each
# one before and after execution and refuses to run if any changed.
FROZEN_FILES = (
    "packages/evaluation/zyra_evaluation/policy_benchmark/long_run_validator.py",
    "packages/evaluation/zyra_evaluation/policy_benchmark/sealed_long_run.py",
    "packages/evaluation/zyra_evaluation/policy_benchmark/sealed_mechanisms.py",
    "packages/evaluation/zyra_evaluation/policy_benchmark/sealed_physical.py",
    "packages/evaluation/zyra_evaluation/scenario_runner/dual_domain.py",
    "packages/evaluation/zyra_evaluation/scenario_runner/live_models.py",
    "packages/evaluation/zyra_evaluation/scenario_runner/fault_campaign.py",
    "packages/evaluation/zyra_evaluation/scenario_runner/software_delivery.py",
    "packages/evaluation/zyra_evaluation/scenario_runner/research_delivery.py",
    "packages/scheduler/zyra_scheduler/dispatch_evidence.py",
    "scripts/run_phase2_sealed_scenarios.py",
)

# The two cross-domain runs the sealed slice requires.  Inputs and fault
# schedules are taken verbatim from the frozen P2-S06-02 campaign.
RUN_SPECS = (
    {
        "run_key": "software-sdk",
        "scenario_id": "live.software-delivery",
        "domain": "software_delivery",
        "seed": 60201,
        "privacy_class": "internal",
        "input": (
            "Add an input-digest-bound sealed delivery marker to a clean SDK "
            "workspace, preserve a requirement-change failure-path test, execute "
            "syntax and unit verification, and publish a checksum-bound patch, "
            "command receipts, causal transition index, and final verifier."
        ),
        "budget": {
            "maximum_effective_transitions": 10000,
            "maximum_wall_time_ms": 3600000,
            "maximum_cost_usd": 0.5,
            "maximum_latency_ms": 300000,
            "maximum_communication_bytes": 67108864,
        },
        "failure_schedule": [
            {
                "injection_id": "software-requirement-change",
                "stage": "patch",
                "kind": "requirement_change",
                "after_effective_step": 240,
                "target": "software-plan",
                "payload": {"requirement": "Retain an input-bound failure-path test and causal receipt."},
            },
            {
                "injection_id": "software-tool-timeout",
                "stage": "test",
                "kind": "tool_timeout",
                "after_effective_step": 480,
                "target": "terminal-test",
                "payload": {"deadline_ms": 10, "retry_deadline_ms": 120000},
            },
            {
                "injection_id": "software-worker-loss",
                "stage": "test",
                "kind": "worker_unavailable",
                "after_effective_step": 720,
                "target": "software-worker-primary",
                "payload": {"successor_required": True},
            },
            {
                "injection_id": "software-provider-failure",
                "stage": "verification",
                "kind": "provider_failure",
                "after_effective_step": 900,
                "target": "provider-primary",
                "payload": {"failover_required": True},
            },
            {
                "injection_id": "software-edge-network-loss",
                "stage": "delivery",
                "kind": "edge_network_loss",
                "after_effective_step": 1020,
                "target": "edge-primary",
                "payload": {"migrate_to": "device"},
            },
        ],
    },
    {
        "run_key": "technical-intelligence",
        "scenario_id": "live.cross-source-research",
        "domain": "cross_source_research",
        "seed": 60202,
        "privacy_class": "public",
        "input": {
            "question": (
                "Using live primary web sources, produce a checksum-bound technical "
                "intelligence report that explains HTTP semantics and status-code "
                "governance, distinguishes normative claims from implementation "
                "guidance, surfaces contradictions and uncertainty after the frozen "
                "requirement change, and binds every material claim to exact "
                "acquired bytes."
            ),
            "source_urls": [
                "https://www.rfc-editor.org/rfc/rfc9110.txt",
                "https://www.iana.org/assignments/http-status-codes/http-status-codes-1.csv",
                "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status",
            ],
            "requirements": [
                "Every material claim must resolve to exact acquired source bytes.",
                "At least one claim must be corroborated across independent authorities.",
                "Normative protocol facts, registry facts, and implementation guidance must remain separately attributed.",
                "Source checksums, request identities, acquisition times, uncertainty, and contradictions must be retained.",
            ],
        },
        "budget": {
            "maximum_effective_transitions": 10000,
            "maximum_wall_time_ms": 3600000,
            "maximum_cost_usd": 0.5,
            "maximum_latency_ms": 300000,
            "maximum_communication_bytes": 67108864,
        },
        "failure_schedule": [
            {
                "injection_id": "research-requirement-change",
                "stage": "source-index",
                "kind": "requirement_change",
                "after_effective_step": 220,
                "target": "research-plan",
                "payload": {"requirement": "Expose contradictions and uncertainty separately from deterministic claims."},
            },
            {
                "injection_id": "research-network-timeout",
                "stage": "source-acquisition",
                "kind": "tool_timeout",
                "after_effective_step": 440,
                "target": "http-source-worker",
                "payload": {"deadline_ms": 50, "retry_deadline_ms": 45000},
            },
            {
                "injection_id": "research-node-loss",
                "stage": "claim-extraction",
                "kind": "node_lost",
                "after_effective_step": 660,
                "target": "researcher-primary",
                "payload": {"successor_required": True},
            },
            {
                "injection_id": "research-provider-rate-limit",
                "stage": "claim-extraction",
                "kind": "provider_rate_limit",
                "after_effective_step": 880,
                "target": "provider-primary",
                "payload": {"retry_after_ms": 250, "failover_required": True},
            },
            {
                "injection_id": "research-network-loss",
                "stage": "citation-verification",
                "kind": "network_loss",
                "after_effective_step": 1020,
                "target": "edge-source-route",
                "payload": {"continue_from_acquired_checksums": True},
            },
        ],
    },
)

MANIFEST_PATH = ROOT / "docs" / "evidence" / "phase2" / "sealed" / "webui-demo-manifest.json"
EVIDENCE_ROOT = "docs/evidence/phase2/sealed/webui-demo-evidence"


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def head_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def build() -> dict:
    commit = head_commit()
    frozen = {}
    for relative in FROZEN_FILES:
        path = ROOT / relative
        if not path.is_file():
            raise SystemExit(f"frozen file is missing: {relative}")
        frozen[relative] = file_digest(path)
    return {
        "schema": "zyra.phase2-sealed-long-run-manifest/v1",
        "slice": "P2-S06-02",
        "manifest_id": "p2-s06-02-webui-demo-v1",
        "candidate_commit": commit,
        "base_commit": commit,
        "phase2_profile": "phase2_strongest_v1",
        "minimum_valid_transitions_per_run": 2000,
        "human_intervention_count": 0,
        "evidence_root": EVIDENCE_ROOT,
        "credential_env_file": ".env.deepseek.local",
        "credential_env_files": [".env.deepseek.local"],
        "provider_profile": {
            "cloud_provider": "deepseek",
            "cloud_model": "deepseek-flash",
            "cloud_endpoint": "https://api.deepseek.com",
            "credential_ref": "env://DEEPSEEK_API_KEY",
            "credential_material_persisted": False,
            "local_provider": "zyra-local",
            "local_model": "local-deterministic",
            "cloud_models": [{"provider_id": "deepseek", "model_id": "deepseek-flash"}],
            "multiple_model_capabilities_required": 1,
            "live_external_request_required": True,
        },
        "hardware_profile": {
            "local": "current terminal host with isolated device deployment process",
            "edge": "independent authenticated edge deployment process and failure boundary",
            "cloud": "isolated cloud gateway process plus authenticated external provider request",
            "simulated_lanes_allowed": False,
            "loopback_control_endpoint_requires_physical_boundary_receipt": True,
        },
        "network_profile": {
            "live_public_proxy_cidrs": ["198.18.0.0/15"],
            "source_host_allowlist": [
                "www.rfc-editor.org",
                "www.iana.org",
                "developer.mozilla.org",
            ],
            "https_hostname_required_for_proxy_mapping": True,
            "literal_proxy_address_forbidden": True,
        },
        "policy": {
            "policy_id": "sealed-autonomous-foundation",
            "allowlist": [
                "task.create", "workspace.create", "source.discover", "source.acquire",
                "code_index.query", "scenario.plan", "scheduler.route",
                "scheduler.tier_dispatch", "provider.dispatch", "memory.retrieve",
                "permission.evaluate", "recovery.checkpoint", "workspace.patch",
                "terminal.execute", "fault.inject", "recovery.replan",
                "recovery.restore", "domain.verify", "artifact.write", "evidence.verify",
            ],
            "denylist": [
                "operator.approve", "operator.steer", "operator.mutate",
                "shell.destructive", "credential.export", "network.unknown",
                "workspace.outside-boundary",
            ],
            "ask_disposition": "deny_and_replan",
            "unknown_disposition": "deny_and_replan",
            "manual_mutation_disposition": "record_reject_fail",
            "zero_human_loop": True,
        },
        "verifier": {
            "independent_validator": "zyra_evaluation.policy_benchmark.long_run_validator",
            "transition_count_source": "raw canonical events only",
            "runner_claim_trusted": False,
            "exclude_event_types": ["heartbeat", "log", "replay", "ui_repaint", "noop"],
            "state_before_after_hash_chain_required": True,
            "final_artifact_digest_binding_required": True,
            "invalid_proposal_reject_or_project_rate": 1.0,
            "unsafe_commit_maximum": 0,
        },
        "frozen_files": frozen,
        "runs": RUN_SPECS,
    }


def main() -> int:
    manifest = build()
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": True,
        "manifest": str(MANIFEST_PATH),
        "candidate_commit": manifest["candidate_commit"],
        "frozen_file_count": len(manifest["frozen_files"]),
        "runs": [item["run_key"] for item in manifest["runs"]],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
