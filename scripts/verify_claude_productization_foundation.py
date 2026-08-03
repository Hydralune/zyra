from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "skills",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import (  # noqa: E402
    ClaudeCleanRuntimeAuditor,
    WorkerRequest,
    assert_clean_runtime_probe,
    assert_clean_runtime_report,
    assert_foundation_ready,
    clean_runtime_metadata,
    clean_runtime_probe,
    disconnect_evidence_from_worker_result,
)
from zyra_workers import ClaudeProductizationFoundationWorker, CodeWorkerRuntime  # noqa: E402


def build_payload(*, artifact_root: Path | None = None, clean_source: bool = False) -> dict[str, Any]:
    if clean_source:
        return _build_clean_payload(artifact_root=artifact_root)
    worker = ClaudeProductizationFoundationWorker(
        project_root=ROOT,
        artifact_root=artifact_root,
        source_workspace_root=ROOT / "provenance",
    )
    run = worker.run()
    assert_foundation_ready(run.probe_result)
    return {
        "ok": run.worker_result.ok,
        "summary": run.worker_result.summary,
        "metadata": dict(run.worker_result.metadata),
        "artifactCount": len(run.worker_result.artifacts),
        "eventCount": len(run.event_records),
        "probe": run.probe_result.to_dict(),
    }


def _build_clean_payload(*, artifact_root: Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as runtime_tmp:
        source_workspace = Path(source_tmp)
        runtime_root = Path(runtime_tmp)
        worker = ClaudeProductizationFoundationWorker(
            project_root=ROOT,
            artifact_root=artifact_root or runtime_root / "foundation-artifacts",
            source_workspace_root=source_workspace,
        )
        foundation_request = WorkerRequest(
            run_id="m1-02a-clean-foundation",
            task_id="claude-clean-source-productization-foundation",
            worker_name="CodeWorkerRuntime",
            request_id="m1-02a-clean-foundation-worker",
            node_id="code-worker",
            constraints={
                "require_primary_source_files": False,
                "allow_source_pool_fallback": False,
                "require_clean_source_pool": True,
            },
        )
        foundation_run = worker.run(foundation_request)
        assert_foundation_ready(foundation_run.probe_result)

        state = create_task_state("Clean source CodeWorker runtime verification.")
        code_runtime = CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=runtime_root / "workspace",
            artifact_root=runtime_root / "artifacts",
        )
        code_run = code_runtime.run(
            WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {
                            "tool_name": "file_write",
                            "arguments": {"path": "clean/result.txt", "content": "clean runtime ok"},
                        },
                        {"tool_name": "file_read", "arguments": {"path": "clean/result.txt"}},
                    ],
                    "control_commands": [
                        {"name": "context"},
                        {"name": "tools"},
                        {"name": "resume"},
                        {"name": "doctor", "artifact_policy": "artifact"},
                    ],
                },
            )
        )
        disconnected_runtime = CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=runtime_root / "disconnect-workspace",
            artifact_root=runtime_root / "disconnect-artifacts",
            query_engine_factory=None,
        )
        disconnected_run = disconnected_runtime.run(
            WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                request_id="m1-02a-clean-disconnect",
                constraints={
                    "tool_plan": [
                        {
                            "tool_name": "file_write",
                            "arguments": {"path": "should-not-exist.txt", "content": "blocked"},
                        }
                    ]
                },
            )
        )
        source_pool_only = int(
            foundation_run.probe_result.coverage.get("source_pool_only_primary_sources", 0)
        )
        probe = clean_runtime_probe(
            project_root=ROOT,
            source_workspace_root=source_workspace,
            sidecar_used=code_run.worker_result.metadata.get("sidecar_contracts_used") == "true",
            default_path_exercised=code_run.worker_result.ok,
            source_pool_only_primary_sources=source_pool_only,
            extra_metadata={
                "code_worker_ok": str(code_run.worker_result.ok).lower(),
                "query_contract_source": code_run.worker_result.metadata.get("query_contract_source", ""),
                "loop": code_run.worker_result.metadata.get("loop", ""),
                "tool_steps": code_run.worker_result.metadata.get("tool_steps", ""),
            },
        )
        assert_clean_runtime_probe(probe)
        audit = ClaudeCleanRuntimeAuditor(
            project_root=ROOT,
            source_workspace_root=source_workspace,
            contracts=code_runtime.runtime_contracts,
        ).audit(
            sidecar_used=code_run.worker_result.metadata.get("sidecar_contracts_used") == "true",
            default_path_exercised=code_run.worker_result.ok,
            source_pool_only_primary_sources=source_pool_only,
            runtime_metadata=code_run.worker_result.metadata,
            disconnect_evidence=[
                disconnect_evidence_from_worker_result(
                    target="ZyraClaudeQueryEngine",
                    ok=disconnected_run.worker_result.ok,
                    error=disconnected_run.worker_result.error,
                    expected_error="productized_query_engine_runtime_disabled",
                    metadata=disconnected_run.worker_result.metadata,
                )
            ],
        )
        assert_clean_runtime_report(audit)
        return {
            "ok": foundation_run.worker_result.ok and code_run.worker_result.ok and probe.ok and audit.ok,
            "summary": "Clean source Claude productization foundation verification passed.",
            "metadata": {
                **foundation_run.worker_result.metadata,
                **code_run.worker_result.metadata,
                **probe.metadata,
                **clean_runtime_metadata(audit),
            },
            "artifactCount": len(foundation_run.worker_result.artifacts) + len(code_run.worker_result.artifacts),
            "eventCount": len(foundation_run.event_records) + len(code_run.event_records) + len(disconnected_run.event_records),
            "probe": foundation_run.probe_result.to_dict(),
            "cleanRuntimeProbe": probe.to_dict(),
            "cleanRuntimeAudit": audit.to_dict(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Zyra-owned Claude Code productization foundation.")
    parser.add_argument("--json", action="store_true", help="print machine-readable payload")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=ROOT / "tmp" / "artifacts",
        help="artifact root used by the foundation worker",
    )
    parser.add_argument(
        "--clean-source",
        action="store_true",
        help="verify with an empty source workspace and the default Zyra-owned CodeWorker runtime path",
    )
    args = parser.parse_args()

    payload = build_payload(artifact_root=args.artifact_root, clean_source=args.clean_source)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    coverage = payload["probe"]["coverage"]
    print(
        "Claude productization foundation verification passed "
        f"(boundaries={coverage['boundary_count']} zyra_targets={coverage['zyra_target_count']} "
        f"events={payload['eventCount']} artifacts={payload['artifactCount']})"
    )


if __name__ == "__main__":
    main()
