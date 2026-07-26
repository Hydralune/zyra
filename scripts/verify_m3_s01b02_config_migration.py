from __future__ import annotations

import argparse
import json
import os
import sqlite3
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
from zyra_runtime.productization import (
    MigrationJournal,
    MigrationRuntime,
    ProcessIdentity,
    SqliteOwnerMigrationAdapter,
)
from zyra_runtime.productization.contracts import (
    RuntimeDomain,
    canonical_json,
    digest_payload,
)


SCHEMA = "zyra.m3-s01b-02.config-migration-evidence/v1"
EXPECTED_B01_REVISION = "d65856eebafd2a046953886b4605ec4ed2c06642"
EXPECTED_B01_RECEIPT = (
    "sha256:0bc4f7579f2fbab4a939024efb73884eb7d6ab6e69bb7ce5331dd2a85ad88ec9"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify M3-S01B-02 config, migration, and clean defaults."
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path(
            "docs/reviews/evidence/M3-S01B-02/"
            "state-owner-reachability-inventory.json"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "docs/reviews/evidence/M3-S01B-02/"
            "config-migration-runtime-receipt.json"
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


def _seed_legacy_sqlite(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE durable_payload("
            "id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO durable_payload(id, payload) VALUES (1, ?)",
            (payload,),
        )
        connection.execute("PRAGMA user_version=0")
        connection.commit()
    finally:
        connection.close()


def _sqlite_projection(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        row = connection.execute(
            "SELECT payload FROM durable_payload WHERE id=1"
        ).fetchone()
        return {
            "path": str(path),
            "version": version,
            "payload": None if row is None else str(row[0]),
        }
    finally:
        connection.close()


def _reset_api() -> None:
    api.reset_api_product_bootstrap()
    api.reset_runtime_owner_composition()
    api.reset_memory_curator_runtime()
    api.reset_worker_pool_api()
    api.reset_fault_runtime_api()
    api.reset_recovery_runtime_api()
    api.reset_terminal_api()
    api.reset_mcp_runtime()
    api.reset_provider_control_client()
    api.reset_runtime_event_spine_bridge()


def _environment(root: Path) -> dict[str, str]:
    return {
        "ZYRA_STATE_ROOT": str(root),
        "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
        "ZYRA_SQLITE_PATH": str(root / "zyra.sqlite3"),
        "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
        "ZYRA_WORKER_POOL_STORE": str(root / "worker-pool.json"),
        "ZYRA_GRAPH_STATE_STORE": str(root / "graph-state.sqlite3"),
        "ZYRA_FAULT_RUNTIME_STORE": str(root / "fault.json"),
        "ZYRA_RECOVERY_RUNTIME_STORE": str(root / "recovery.json"),
        "ZYRA_PERMISSION_STATE": str(root / "permissions.json"),
        "ZYRA_PERMISSION_STORE": str(root / "permission-compat.json"),
        "ZYRA_MEMORY_INDEX_PATH": str(root / "memory-index.sqlite3"),
        "ZYRA_CODE_INDEX_ROOT": str(root / "code-index"),
        "ZYRA_MCP_STATE": str(root / "mcp-state.json"),
        "ZYRA_SANDBOX_GATEWAY_STATE": str(root / "gateway-state"),
        "ZYRA_TERMINAL_STATE": str(root / "terminal-state.json"),
        "ZYRA_CONTROL_STATE": str(root / "control"),
        "ZYRA_SUBAGENT_STATE": str(root / "subagents"),
        "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
        "ZYRA_WORKSPACE_ROOT": str(root / "managed-workspaces"),
        "ZYRA_PROVIDER_STATE": str(root / "provider.sqlite3"),
        "ZYRA_RUNTIME_OWNER_ENFORCEMENT_DISABLED": "",
        "ZYRA_MEMORY_CURATOR_DISABLED": "",
        "ZYRA_TERMINAL_DISABLED": "",
        "ZYRA_DISABLE_RECOVERY_RUNTIME": "",
    }


def _bootstrap_evidence(root: Path) -> dict[str, Any]:
    environment = _environment(root)
    previous = {name: os.environ.get(name) for name in environment}
    os.environ.update(environment)
    session_path = Path(environment["ZYRA_SQLITE_PATH"])
    graph_path = Path(environment["ZYRA_GRAPH_STATE_STORE"])
    artifact_root = Path(environment["ZYRA_ARTIFACT_ROOT"])
    permission_path = Path(environment["ZYRA_PERMISSION_STATE"])
    _seed_legacy_sqlite(session_path, "session-preserved")
    _seed_legacy_sqlite(graph_path, "graph-preserved")
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / "proof.txt").write_text(
        "artifact-preserved",
        encoding="utf-8",
    )
    permission_path.parent.mkdir(parents=True, exist_ok=True)
    permission_path.write_text(
        json.dumps({"rules": [], "requests": {}}),
        encoding="utf-8",
    )
    _reset_api()
    try:
        bootstrap = api.get_api_product_bootstrap()
        first = bootstrap.start()
        first_readiness = bootstrap.readiness()
        composition = api.get_runtime_owner_composition()
        owner_loss = [
            composition.prove_owner_loss(domain)
            for domain in RuntimeDomain
        ]
        first_projection = {
            "receipt": first.to_dict(),
            "readiness": first_readiness,
            "session": _sqlite_projection(session_path),
            "graph": _sqlite_projection(graph_path),
            "artifact": (artifact_root / "proof.txt").read_text(
                encoding="utf-8"
            ),
            "artifact_manifest": json.loads(
                (artifact_root / ".zyra-artifact-store.json").read_text(
                    encoding="utf-8"
                )
            ),
            "permission": json.loads(
                permission_path.read_text(encoding="utf-8")
            ),
        }
        _reset_api()
        restarted = api.get_api_product_bootstrap()
        second = restarted.start()
        second_readiness = restarted.readiness()
        restart_projection = {
            "receipt": second.to_dict(),
            "readiness": second_readiness,
            "session": _sqlite_projection(session_path),
            "graph": _sqlite_projection(graph_path),
        }
        valid = (
            first_projection["readiness"].get("ready") is True
            and restart_projection["readiness"].get("ready") is True
            and first_projection["session"]["version"] == 1
            and first_projection["session"]["payload"] == "session-preserved"
            and first_projection["graph"]["version"] == 1
            and first_projection["graph"]["payload"] == "graph-preserved"
            and first_projection["artifact"] == "artifact-preserved"
            and first_projection["artifact_manifest"].get("schema")
            == "zyra.artifact-store-manifest/v1"
            and first_projection["permission"].get("schema")
            == "zyra.permission-state"
            and first.migration is not None
            and first.migration.migrated is True
            and second.migration is not None
            and second.migration.migrated is False
            and all(item.get("valid") is True for item in owner_loss)
            and all(
                item.get("fallback_success") is False for item in owner_loss
            )
        )
        return {
            "valid": valid,
            "first_start": first_projection,
            "restart": restart_projection,
            "owner_loss_proofs": owner_loss,
            "demo_fallback": False,
            "source_store_fallback": False,
            "source_process_fallback": False,
        }
    finally:
        _reset_api()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _rollback_evidence(root: Path) -> dict[str, Any]:
    owner = root / "rollback-owner.sqlite3"
    journal_path = root / "rollback-journal.sqlite3"
    _seed_legacy_sqlite(owner, "rollback-preserved")
    identity = ProcessIdentity.create(configuration_digest="sha256:" + "8" * 64)

    def interrupt(phase: str, _transaction: str, _adapter: str) -> None:
        if phase == "after_apply":
            raise SystemExit("simulated process exit")

    def build_runtime(
        journal: MigrationJournal,
        *,
        crash_hook=None,
    ) -> MigrationRuntime:
        adapter = SqliteOwnerMigrationAdapter(
            adapter_id="rollback-owner",
            owner="evidence.RollbackOwner",
            path=owner,
            target_version=1,
            required_tables=("durable_payload",),
            allow_missing=False,
        )
        return MigrationRuntime(
            journal,
            (adapter,),
            backup_root=root / "backups",
            process_generation=identity.generation_id,
            configuration_digest=identity.configuration_digest,
            target_version=1,
            lease_seconds=30,
            crash_hook=crash_hook,
        )

    first_journal = MigrationJournal(journal_path)
    first = build_runtime(first_journal, crash_hook=interrupt)
    interrupted = False
    try:
        first.execute(first.plan())
    except SystemExit:
        interrupted = True
    active = first_journal.active_transaction()
    active_state = None if active is None else active.state.value
    applied = _sqlite_projection(owner)
    first_journal.close()

    restart_journal = MigrationJournal(journal_path)
    restarted = build_runtime(restart_journal)
    try:
        recovery = restarted.recover_incomplete()
        rolled_back = _sqlite_projection(owner)
        committed = restarted.execute(restarted.plan())
        final = _sqlite_projection(owner)
    finally:
        restart_journal.close()
    valid = (
        interrupted
        and active_state == "applying"
        and applied["version"] == 1
        and recovery is not None
        and recovery.clean is True
        and recovery.final_state == "rolled_back"
        and rolled_back["version"] == 0
        and rolled_back["payload"] == "rollback-preserved"
        and committed.migrated is True
        and final["version"] == 1
        and final["payload"] == "rollback-preserved"
    )
    return {
        "valid": valid,
        "interrupted": interrupted,
        "interrupted_state": active_state,
        "applied_before_restart": applied,
        "recovery": None if recovery is None else recovery.to_dict(),
        "rolled_back": rolled_back,
        "committed": committed.to_dict(),
        "final": final,
        "source_store_fallback": False,
    }


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
    b01_path = (
        project_root
        / "docs/reviews/evidence/M3-S01B-01/runtime-absorption-receipt.json"
    )
    inventory = _load_json(inventory_path)
    b01 = _load_json(b01_path)
    slice_lines = _section(inventory, "effective_lines_slice")
    parent_lines = _section(inventory, "effective_lines_parent")
    causality = _section(inventory, "event_mutation_causality")
    source = _section(inventory, "source_risk_bridge")
    revision_valid = (
        inventory.get("revision") == arguments.revision
        and b01.get("revision") == EXPECTED_B01_REVISION
        and b01.get("receipt_digest") == EXPECTED_B01_RECEIPT
        and b01.get("release_ready") is True
    )
    inventory_valid = (
        slice_lines.get("valid") is True
        and slice_lines.get("metrics", {}).get("effective_production", 0) >= 4500
        and slice_lines.get("metrics", {})
        .get("effective_by_language", {})
        .get("python", 0)
        > 0
        and slice_lines.get("metrics", {})
        .get("effective_by_language", {})
        .get("typescript", 0)
        > 0
        and parent_lines.get("valid") is True
        and parent_lines.get("metrics", {}).get("effective_production", 0)
        >= 9000
        and causality.get("valid") is True
        and causality.get("metrics", {}).get("valid_links") == 11
        and source.get("valid") is True
        and source.get("metrics", {}).get("blocking_risk_records") == 0
    )

    temporary_parent = project_root / ".tmp"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="zyra-m3-s01b02-",
        dir=temporary_parent,
    ) as temporary:
        temporary_root = Path(temporary)
        bootstrap = _bootstrap_evidence(temporary_root / "bootstrap")
        rollback = _rollback_evidence(temporary_root / "rollback")

    release_ready = (
        revision_valid
        and inventory_valid
        and bootstrap["valid"]
        and rollback["valid"]
    )
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "slice_id": "M3-S01B-02",
        "revision": arguments.revision,
        "protected_predecessor": {
            "revision": b01.get("revision"),
            "receipt_digest": b01.get("receipt_digest"),
            "valid": revision_valid,
        },
        "candidate_inventory": {
            "receipt_digest": inventory.get("receipt_digest"),
            "global_release_ready": inventory.get("release_ready"),
            "global_release_ready_expected": False,
            "reason": (
                "The reusable audit intentionally retains future M3-02/M3-03 "
                "requirements and static owner/default findings. B01 and this "
                "receipt dynamically execute the M3-01B boundaries."
            ),
            "effective_lines_slice": slice_lines.get("metrics"),
            "effective_lines_parent": parent_lines.get("metrics"),
            "causality": causality.get("metrics"),
            "source_risks": source.get("metrics"),
            "valid_for_parent_closure": inventory_valid,
        },
        "clean_default_bootstrap": bootstrap,
        "crash_rollback_restart": rollback,
        "validation": {
            "revision_valid": revision_valid,
            "inventory_valid": inventory_valid,
            "clean_default_valid": bootstrap["valid"],
            "rollback_valid": rollback["valid"],
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
                "slice_effective": slice_lines.get("metrics", {}).get(
                    "effective_production"
                ),
                "parent_effective": parent_lines.get("metrics", {}).get(
                    "effective_production"
                ),
                "owner_loss_proofs": len(
                    bootstrap.get("owner_loss_proofs") or []
                ),
            }
        )
    )
    return 0 if release_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
