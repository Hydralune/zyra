from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATHS = [
    PROJECT_ROOT / "packages" / "core",
    PROJECT_ROOT / "packages" / "runtime",
    PROJECT_ROOT / "packages" / "workers",
    PROJECT_ROOT / "packages" / "integrations",
]
for package_path in PACKAGE_PATHS:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state, to_jsonable  # noqa: E402
from zyra_runtime import (  # noqa: E402
    WorkerRequest,
    assemble_claude_runtime_context,
    assert_claude_productization_integration_ready,
    build_claude_productization_integration_report,
    build_claude_source_graph_audit,
    build_productized_claude_runtime_contracts,
    default_tool_registry,
)
from zyra_workers import CodeWorkerRuntime  # noqa: E402


class ExplodingSidecarClient:
    def health(self) -> dict[str, Any]:
        raise AssertionError("sidecar must not be called by productized integration verification")

    def runtime_inventory(self) -> dict[str, Any]:
        raise AssertionError("sidecar must not be called by productized integration verification")

    def query_contract(self) -> dict[str, Any]:
        raise AssertionError("sidecar must not be called by productized integration verification")

    def session_contract(self) -> dict[str, Any]:
        raise AssertionError("sidecar must not be called by productized integration verification")

    def tool_loop_contract(self) -> dict[str, Any]:
        raise AssertionError("sidecar must not be called by productized integration verification")


def build_payload(*, run_clean_source: bool) -> dict[str, Any]:
    contracts = build_productized_claude_runtime_contracts(project_root=PROJECT_ROOT)
    integration = build_claude_productization_integration_report(
        project_root=PROJECT_ROOT,
        runtime_contracts=contracts,
    )
    assert_claude_productization_integration_ready(integration)
    tool_specs = default_tool_registry().list()
    runtime_context = assemble_claude_runtime_context(
        request=WorkerRequest(
            run_id="verify",
            task_id="claude-productization-integration",
            worker_name="CodeWorkerRuntime",
            constraints={"permission_mode": "workspace"},
            metadata={"source": "verify_claude_productization_integration"},
        ),
        integration_report=integration,
        runtime_contracts=contracts,
        project_root=PROJECT_ROOT,
        workspace_root=PROJECT_ROOT / "tmp" / "verify-workspace",
        artifact_root=PROJECT_ROOT / "tmp" / "verify-artifacts",
        tool_names=tuple(tool.name for tool in tool_specs),
        read_only_tool_names=tuple(tool.name for tool in tool_specs if tool.metadata.get("read_only") == "true"),
        mutating_tool_names=tuple(tool.name for tool in tool_specs if tool.metadata.get("read_only") != "true"),
        permission_mode="workspace",
    )
    source_graph_audit = build_claude_source_graph_audit(
        project_root=PROJECT_ROOT,
        integration_report=integration,
        runtime_contracts=contracts,
        runtime_context_report=runtime_context,
    )
    if not source_graph_audit.ok:
        raise AssertionError(f"Source graph audit failed: {source_graph_audit.first_blocker_code}")
    success = _run_code_worker_success()
    blocked_source_graph = _run_code_worker_blocked(
        constraints={"disable_source_graph_crosswalk": True},
        expected_error="source_graph_crosswalk_disabled",
    )
    blocked_runtime_port = _run_code_worker_blocked(
        constraints={"disabled_runtime_context_ports": ["source_graph"]},
        expected_error="runtime_context_source_graph_port_disabled",
    )
    payload: dict[str, Any] = {
        "ok": True,
        "project_root": str(PROJECT_ROOT),
        "integration": integration.to_dict(),
        "runtime_context": runtime_context.to_dict(),
        "source_graph_audit": source_graph_audit.to_dict(),
        "runtime_contracts": contracts.to_dict(),
        "success_run": success,
        "blocked_source_graph": blocked_source_graph,
        "blocked_runtime_port": blocked_runtime_port,
        "clean_source": None,
    }
    if run_clean_source:
        payload["clean_source"] = _run_clean_source_probe()
        payload["ok"] = payload["ok"] and bool(payload["clean_source"]["ok"])
    return payload


def _run_code_worker_success() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_root = Path(tmpdir)
        state = create_task_state("Verify Claude productization integration.")
        workspace = temp_root / "workspace"
        runtime = CodeWorkerRuntime(
            project_root=PROJECT_ROOT,
            workspace_root=workspace,
            artifact_root=temp_root / "artifacts",
            sidecar_client=ExplodingSidecarClient(),
        )
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="CodeWorkerRuntime",
            constraints={
                "tool_plan": [
                    {
                        "tool_name": "file_write",
                        "arguments": {"path": "verify/source-graph.txt", "content": "source graph ready"},
                    },
                    {"tool_name": "file_read", "arguments": {"path": "verify/source-graph.txt"}},
                ]
            },
        )
        run = runtime.run(request)
        if not run.worker_result.ok:
            raise AssertionError(f"CodeWorker integration success probe failed: {run.worker_result.error}")
        integration_events = [
            event.payload["claude_productization_integration"]
            for event in run.event_records
            if "claude_productization_integration" in event.payload
        ]
        if not integration_events:
            raise AssertionError("CodeWorker integration probe emitted no source graph events")
        audit_events = [
            event.payload["claude_source_graph_audit"]
            for event in run.event_records
            if "claude_source_graph_audit" in event.payload
        ]
        if not audit_events:
            raise AssertionError("CodeWorker integration probe emitted no source graph audit event")
        if run.worker_result.metadata.get("source_graph_audit_ok") != "true":
            raise AssertionError("CodeWorker integration probe did not pass source graph audit")
        if not (workspace / "verify" / "source-graph.txt").exists():
            raise AssertionError("CodeWorker integration probe did not execute tool after gate passed")
        return {
            "ok": run.worker_result.ok,
            "metadata": dict(run.worker_result.metadata),
            "integration_event_phases": [event["phase"] for event in integration_events],
            "audit_event_phases": [event["phase"] for event in audit_events],
            "artifact_count": len(run.worker_result.artifacts),
            "tool_file_written": True,
        }


def _run_code_worker_blocked(*, constraints: dict[str, Any], expected_error: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_root = Path(tmpdir)
        state = create_task_state("Verify Claude productization blocked path.")
        workspace = temp_root / "workspace"
        runtime = CodeWorkerRuntime(
            project_root=PROJECT_ROOT,
            workspace_root=workspace,
            artifact_root=temp_root / "artifacts",
            sidecar_client=ExplodingSidecarClient(),
        )
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="CodeWorkerRuntime",
            constraints={
                **constraints,
                "tool_plan": [
                    {
                        "tool_name": "file_write",
                        "arguments": {"path": "blocked/should-not-exist.txt", "content": "blocked"},
                    }
                ],
            },
        )
        run = runtime.run(request)
        if run.worker_result.ok:
            raise AssertionError("CodeWorker blocked probe unexpectedly succeeded")
        if run.worker_result.error != expected_error:
            raise AssertionError(f"Expected {expected_error}, got {run.worker_result.error}")
        blocked_file = workspace / "blocked" / "should-not-exist.txt"
        if blocked_file.exists():
            raise AssertionError(f"Blocked probe wrote {blocked_file}")
        integration_events = [
            event.payload["claude_productization_integration"]
            for event in run.event_records
            if "claude_productization_integration" in event.payload
        ]
        return {
            "ok": False,
            "expected_error": expected_error,
            "actual_error": run.worker_result.error,
            "metadata": dict(run.worker_result.metadata),
            "integration_event_phases": [event["phase"] for event in integration_events],
            "tool_file_written": False,
        }


def _run_clean_source_probe() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        clean_root = Path(tmpdir) / "zyra-clean"
        _copy_clean_project(clean_root)
        env = os.environ.copy()
        clean_package_paths = [
            clean_root / "packages" / "core",
            clean_root / "packages" / "runtime",
            clean_root / "packages" / "workers",
            clean_root / "packages" / "integrations",
        ]
        env["PYTHONPATH"] = os.pathsep.join(str(path) for path in clean_package_paths)
        command = [
            sys.executable,
            "scripts/verify_claude_productization_integration.py",
            "--json",
            "--no-clean-source",
        ]
        completed = subprocess.run(
            command,
            cwd=clean_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            return {
                "ok": False,
                "clean_root": str(clean_root),
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        nested = json.loads(completed.stdout)
        return {
            "ok": bool(nested.get("ok")),
            "clean_root": str(clean_root),
            "source_repo_present": (clean_root.parent / "claude-code-best").exists(),
            "vendor_present": (clean_root / "vendor").exists(),
            "vendor_runtimes_present": (clean_root / "vendor-runtimes").exists(),
            "nested_metadata": nested["integration"]["metadata"],
            "nested_success": nested["success_run"],
        }


def _copy_clean_project(clean_root: Path) -> None:
    clean_root.mkdir(parents=True, exist_ok=True)
    for name in ("packages", "apps", "scripts"):
        source = PROJECT_ROOT / name
        if source.exists():
            shutil.copytree(source, clean_root / name, ignore=_copy_ignore)
    for optional_name in ("pyproject.toml", "README.md"):
        source_file = PROJECT_ROOT / optional_name
        if source_file.exists():
            shutil.copy2(source_file, clean_root / optional_name)


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "tmp",
        "vendor",
        "vendor-runtimes",
        "source-pool",
        "runtime-sources",
    }
    return {name for name in names if name in ignored or name.endswith(".pyc")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify M1-02A-02 Claude source graph productization integration.")
    parser.add_argument("--json", action="store_true", help="Write JSON payload.")
    parser.add_argument("--clean-source", dest="clean_source", action="store_true", default=True)
    parser.add_argument("--no-clean-source", dest="clean_source", action="store_false")
    args = parser.parse_args(argv)

    payload = build_payload(run_clean_source=args.clean_source)
    if args.json:
        print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"ok={str(payload['ok']).lower()}")
        print(f"source_graph_batch_count={payload['integration']['validation']['batch_count']}")
        print(f"runtime_context_ports={payload['integration']['validation']['runtime_context_port_count']}")
        print(f"downstream_contracts={payload['integration']['validation']['downstream_contract_count']}")
        print(f"source_graph_audit_ok={str(payload['source_graph_audit']['ok']).lower()}")
        print(f"success_error={payload['success_run']['metadata'].get('claude_productization_integration_error', '')}")
        if payload.get("clean_source"):
            print(f"clean_source_ok={str(payload['clean_source']['ok']).lower()}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
