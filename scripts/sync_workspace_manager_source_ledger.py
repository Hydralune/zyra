from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for package_path in (ROOT / "packages" / "core", ROOT / "packages" / "integrations"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import InternalizationLedger, InternalizationLedgerEntry  # noqa: E402
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "OpenHands",
        "source_path": (
            "openhands/app_server/app_conversation/app_conversation_start_task_service.py;"
            "openhands/app_server/file_store/files.py;"
            "openhands/app_server/file_store/local.py;"
            "openhands/app_server/sandbox/sandbox_service.py;"
            "openhands/app_server/sandbox/workspace_archive.py"
        ),
        "capability_name": "workspace_lifecycle_binding_snapshot_archive",
        "capability_summary": (
            "Start-task workspace lifecycle, ready barrier, durable backend/location binding, "
            "atomic file persistence, committed snapshot and archive-before-delete semantics."
        ),
        "target_paths": [
            "packages/workspace/zyra_workspace/manager.py",
            "packages/workspace/zyra_workspace/store.py",
            "packages/workspace/zyra_workspace/local_backend.py",
            "packages/workspace/zyra_workspace/snapshots.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "runtime_required": True,
        "rationale": (
            "The lifecycle was decomposed into Zyra binding, lease, event, quota and snapshot owners; "
            "no OpenHands server, runtime, database, or source path is required at runtime."
        ),
    },
    {
        "source_repo": "agentscope",
        "source_path": (
            "src/agentscope/app/workspace_manager/_base.py;"
            "src/agentscope/app/workspace_manager/_local_workspace_manager.py;"
            "src/agentscope/workspace/_base.py;"
            "src/agentscope/workspace/_local_workspace.py"
        ),
        "capability_name": "workspace_backend_manager_local_mounts",
        "capability_summary": (
            "Backend-aware workspace manager, local filesystem implementation, mount layout, "
            "concurrent lifecycle access and explicit disabled semantics."
        ),
        "target_paths": [
            "packages/workspace/zyra_workspace/local_backend.py",
            "packages/workspace/zyra_workspace/manager.py",
            "packages/workspace/zyra_workspace/mounts.py",
            "packages/workspace/zyra_workspace/quota.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "runtime_required": True,
        "rationale": (
            "Only backend and local workspace lifecycle mechanisms supplement the OpenHands primary; "
            "AgentScope router, Docker, E2B, MCP gateway and state owners are not copied."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_path": (
            "packages/coding-agent/src/task/isolation-runner.ts;"
            "packages/coding-agent/src/task/worktree.ts;"
            "packages/coding-agent/src/edit/hashline/filesystem.ts;"
            "packages/coding-agent/src/edit/hashline/execute.ts"
        ),
        "capability_name": "workspace_dirty_nested_repo_read_epoch_fencing",
        "capability_summary": (
            "Dirty baseline ownership, nested repository discovery, read epoch/base hash preflight, "
            "fenced worker handoff and typed isolation/cleanup receipts."
        ),
        "target_paths": [
            "packages/workspace/zyra_workspace/dirty_state.py",
            "packages/workspace/zyra_workspace/git_boundary.py",
            "packages/workspace/zyra_workspace/file_state.py",
            "packages/workspace/zyra_workspace/paths.py",
            "packages/workspace/zyra_workspace/models.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "runtime_required": True,
        "rationale": (
            "Hashline and worktree semantics are mapped to Zyra leases/read receipts and read-only Git; "
            "OMP process, CLI, agent loop and worktree owner are not imported."
        ),
    },
    {
        "source_repo": "claude-code-best",
        "source_path": "src/tools/**;src/utils/permissions/**;src/bridge/**",
        "capability_name": "workspace_tool_context_permission_conformance",
        "capability_summary": (
            "Tool-use context, workspace-scoped permission and worker handoff behavior used only for conformance."
        ),
        "target_paths": ["packages/workspace/zyra_workspace/manager.py"],
        "source_role": "conformance_only",
        "migration_strategy": "not_selected",
        "runtime_required": False,
        "rationale": (
            "The CodeWorker runtime remains its existing owner; WorkspaceManager only supplies the fenced task mount."
        ),
    },
    {
        "source_repo": "opencode",
        "source_path": "packages/opencode/src/session/**;packages/opencode/src/tool/**",
        "capability_name": "workspace_session_tool_protocol_reference",
        "capability_summary": "Durable session/tool protocol comparison without a second workspace owner.",
        "target_paths": ["packages/workspace/zyra_workspace/api_service.py"],
        "source_role": "reference_only",
        "migration_strategy": "not_selected",
        "runtime_required": False,
        "rationale": "OpenCode is retained as protocol reference; no production workspace mechanism is migrated.",
    },
)


def ledger_id(decision: dict[str, Any]) -> str:
    identity = "|".join(
        (
            str(decision["source_repo"]),
            str(decision["source_path"]),
            str(decision["capability_name"]),
        )
    )
    return "ledger_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    targets = list(decision["target_paths"])
    runtime_required = bool(decision["runtime_required"])
    return {
        "ledger_id": ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "target_bindings": [
            {
                "path": path,
                "role": "primary" if index == 0 else "supporting",
                "required_for_main_path": runtime_required,
            }
            for index, path in enumerate(targets)
        ],
        "migration_strategy": decision["migration_strategy"],
        "main_path_status": "tested_main_path" if runtime_required else "inventoried",
        "lifecycle": "productized" if runtime_required else "candidate",
        "owner_unit": "M1-S05A-01",
        "main_path": {
            "surfaces": ["workspace_manager", "task_api", "code_worker", "browser_worker"]
            if runtime_required
            else [],
            "event_types": ["system_notice"] if runtime_required else [],
            "api_routes": [
                "POST /tasks",
                "GET /workspaces/{workspace_id}",
                "POST /workspaces/{workspace_id}/snapshot",
                "POST /tasks/{task_id}/workers/code",
                "POST /tasks/{task_id}/workers/browser",
            ]
            if runtime_required
            else [],
            "control_commands": [],
            "artifact_kinds": [],
            "worker_runtime": "WorkspaceManagerRuntime.create_for_task"
            if runtime_required
            else "",
            "ui_panels": [],
        },
        "line_count_policy": "counts_as_runtime" if runtime_required else "excluded_inventory_only",
        "runtime_entry": {
            "module": "zyra_workspace.manager",
            "function": "WorkspaceManagerRuntime.create_for_task"
            if runtime_required
            else "WorkspaceManagerRuntime.health",
            "protocol": "zyra-workspace-manager-v1",
            "health_check": "python -m pytest tests/unit/test_workspace_manager_foundation.py -q",
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": "tests/unit/test_workspace_manager_foundation.py",
                "command": "python -m pytest tests/unit/test_workspace_manager_foundation.py -q",
                "kind": "unit",
                "expected_signal": "workspace lifecycle, fencing, snapshot, restore, quota and cleanup are real",
                "required": runtime_required,
            },
            {
                "path": "tests/integration/test_workspace_manager_api.py",
                "command": "python -m pytest tests/integration/test_workspace_manager_api.py -q",
                "kind": "integration",
                "expected_signal": "task API and worker handoff use redacted task-scoped workspace bindings",
                "required": runtime_required,
            },
        ],
        "notes": (
            f"source_role={decision['source_role']}; "
            f"runtime_required={str(runtime_required).lower()}; "
            f"{decision['rationale']}"
        ),
        "metadata": {
            "owner_unit": "M1-S05A-01",
            "source_role": decision["source_role"],
            "canonical_workspace_owner": "WorkspaceManagerRuntime+WorkspaceBindingStore",
            "canonical_task_owner": "SQLiteStore/TaskState",
            "canonical_event_owner": "EventLog",
            "canonical_artifact_owner": "LocalArtifactStore",
            "physical_worker_lease_owner": "M1-07A-deferred",
            "root_source_runtime_dependency": False,
        },
    }


def rewrite(document: Any) -> Any:
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {item["ledger_id"]: item for item in (entry(decision) for decision in DECISIONS)}
    output = [replacements.pop(str(item.get("ledger_id") or ""), item) for item in entries]
    output.extend(replacements.values())
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    if isinstance(document, list):
        return output
    return {
        **document,
        "entries": output,
        "summary": to_jsonable(InternalizationLedger(typed).summary()),
    }


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    current = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite(current)
    aligned = canonical(current) == canonical(expected)
    if write and not aligned:
        path.write_text(canonical(expected), encoding="utf-8", newline="\n")
        aligned = True
    return aligned, len(DECISIONS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    write = args.write or not args.check
    aligned, count = synchronize(args.ledger.resolve(), write=write)
    print(f"workspace_manager_source_ledger_aligned={str(aligned).lower()}")
    print(f"workspace_manager_source_decision_count={count}")
    print("workspace_manager_owner_unit=M1-S05A-01")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
