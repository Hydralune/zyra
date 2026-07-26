from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    ROOT / "apps" / "api",
    ROOT / "packages" / "code_index",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "core",
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "skills",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "workspace",
):
    resolved = str(package_path)
    if resolved not in sys.path:
        sys.path.insert(0, resolved)

from zyra_api import main as api
from zyra_runtime.productization.contracts import (
    M3WorkQueue,
    RuntimeDomain,
    canonical_json,
    digest_payload,
)


SCHEMA = "zyra.m3-s01b-01.runtime-absorption-evidence/v1"
EXPECTED_QUEUE_DIGEST = (
    "sha256:f5b931f50c960856dfa7c8197d1c22d7c4ec3f6110832ed5114143ef4eb3d4d6"
)
EXPECTED_PARENT_RECEIPT_DIGEST = (
    "sha256:8d2e533a42681301cf5b5b142f0bbf391c0c78042ee9c06adcaeb25ba478cdec"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify M3-S01B-01 dynamic owner/default absorption evidence."
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path(
            "docs/reviews/evidence/M3-S01B-01/"
            "state-owner-reachability-inventory.json"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "docs/reviews/evidence/M3-S01B-01/runtime-absorption-receipt.json"
        ),
    )
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _section(receipt: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    sections = receipt.get("sections")
    if not isinstance(sections, list):
        raise ValueError("inventory receipt has no section list")
    for value in sections:
        if isinstance(value, Mapping) and value.get("name") == name:
            return value
    raise ValueError(f"inventory section is missing: {name}")


def _reset_runtime_composition() -> None:
    api.reset_runtime_owner_composition()
    api.reset_memory_curator_runtime()
    api.reset_worker_pool_api()
    api.reset_recovery_runtime_api()
    api.reset_terminal_api()
    api.reset_mcp_runtime()
    api.reset_provider_control_client()
    api.reset_runtime_event_spine_bridge()


def _clean_environment(root: Path) -> dict[str, str]:
    return {
        "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
        "ZYRA_SQLITE_PATH": str(root / "zyra.sqlite3"),
        "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
        "ZYRA_WORKER_POOL_STORE": str(root / "worker-pool.json"),
        "ZYRA_GRAPH_STATE_STORE": str(root / "graph-state.json"),
        "ZYRA_RECOVERY_RUNTIME_STORE": str(root / "recovery.json"),
        "ZYRA_PERMISSION_STATE": str(root / "permissions.json"),
        "ZYRA_PERMISSION_STORE": str(root / "permission-compat.json"),
        "ZYRA_MEMORY_INDEX_PATH": str(root / "memory-index.sqlite3"),
        "ZYRA_MCP_STATE": str(root / "mcp-state.json"),
        "ZYRA_SANDBOX_GATEWAY_STATE": str(root / "gateway-state"),
        "ZYRA_TERMINAL_STATE": str(root / "terminal-state.json"),
        "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
        "ZYRA_WORKSPACE_ROOT": str(root / "managed-workspaces"),
        "ZYRA_RUNTIME_OWNER_ENFORCEMENT_DISABLED": "",
        "ZYRA_MEMORY_CURATOR_DISABLED": "",
        "ZYRA_TERMINAL_DISABLED": "",
        "ZYRA_DISABLE_RECOVERY_RUNTIME": "",
    }


def _runtime_evidence() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="zyra-m3-s01b01-") as temporary:
        environment = _clean_environment(Path(temporary))
        previous = {name: os.environ.get(name) for name in environment}
        os.environ.update(environment)
        _reset_runtime_composition()
        try:
            composition = api.get_runtime_owner_composition()
            snapshot = composition.probe_all()
            proofs = [
                composition.prove_owner_loss(domain)
                for domain in RuntimeDomain
            ]
        finally:
            _reset_runtime_composition()
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    return snapshot, proofs


def main() -> int:
    arguments = _arguments()
    project_root = arguments.project_root.resolve()
    inventory_path = (
        arguments.inventory
        if arguments.inventory.is_absolute()
        else project_root / arguments.inventory
    )
    output_path = (
        arguments.output
        if arguments.output.is_absolute()
        else project_root / arguments.output
    )
    queue_path = (
        project_root
        / "docs/reviews/evidence/M3-S01A-02/downstream/m3_01b.json"
    )
    parent_path = (
        project_root
        / "docs/reviews/evidence/M3-S01A-02/"
        "state-owner-reachability-receipt.json"
    )

    queue = M3WorkQueue.from_dict(_load_json(queue_path))
    parent = _load_json(parent_path)
    inventory = _load_json(inventory_path)
    runtime_snapshot, owner_loss_proofs = _runtime_evidence()

    causal = _section(inventory, "event_mutation_causality")
    source = _section(inventory, "source_risk_bridge")
    effective = _section(inventory, "effective_lines_slice")
    queue_valid = (
        queue.digest == EXPECTED_QUEUE_DIGEST
        and queue.receipt_digest == EXPECTED_PARENT_RECEIPT_DIGEST
        and len(queue.items) == 119
        and sum(item.blocking for item in queue.items) == 92
    )
    parent_valid = (
        parent.get("receipt_digest") == EXPECTED_PARENT_RECEIPT_DIGEST
    )
    runtime_valid = (
        runtime_snapshot.get("ready") is True
        and runtime_snapshot.get("blockers") == []
        and len(runtime_snapshot.get("domains") or []) == 11
        and all(item.get("valid") for item in owner_loss_proofs)
        and all(not item.get("fallback_success") for item in owner_loss_proofs)
    )
    inventory_valid = (
        inventory.get("revision") == arguments.revision
        and causal.get("valid") is True
        and causal.get("metrics", {}).get("valid_links") == 11
        and source.get("valid") is True
        and source.get("metrics", {}).get("blocking_risk_records") == 0
        and effective.get("valid") is True
        and effective.get("metrics", {}).get("effective_production", 0) >= 4500
    )
    release_ready = queue_valid and parent_valid and runtime_valid and inventory_valid
    body = {
        "schema": SCHEMA,
        "slice_id": "M3-S01B-01",
        "revision": arguments.revision,
        "protected_input": {
            "queue_digest": queue.digest,
            "parent_receipt_digest": queue.receipt_digest,
            "items": len(queue.items),
            "blocking_items": sum(item.blocking for item in queue.items),
            "valid": queue_valid and parent_valid,
        },
        "runtime_composition": runtime_snapshot,
        "owner_loss_proofs": owner_loss_proofs,
        "candidate_inventory": {
            "receipt_digest": inventory.get("receipt_digest"),
            "release_ready": inventory.get("release_ready"),
            "release_ready_expected": False,
            "reason": (
                "The reusable M3-01A inventory retains static reachability, "
                "M3-01B parent-minimum, and future M3 requirement findings. "
                "This receipt consumes only M3-S01B-01 assigned boundaries."
            ),
            "causality": causal.get("metrics"),
            "source_risks": source.get("metrics"),
            "effective_lines": effective.get("metrics"),
            "valid_for_slice": inventory_valid,
        },
        "validation": {
            "queue_valid": queue_valid,
            "parent_valid": parent_valid,
            "runtime_valid": runtime_valid,
            "inventory_valid": inventory_valid,
        },
        "release_ready": release_ready,
    }
    body["receipt_digest"] = digest_payload(body)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(canonical_json(body) + "\n", encoding="utf-8")
    print(
        canonical_json(
            {
                "release_ready": release_ready,
                "receipt_digest": body["receipt_digest"],
                "revision": arguments.revision,
                "runtime_domains": len(runtime_snapshot.get("domains") or []),
                "owner_loss_proofs": len(owner_loss_proofs),
                "queue_items": len(queue.items),
            }
        )
    )
    return 0 if release_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
