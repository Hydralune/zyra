from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "skills",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, PlanNodeStatus, create_task_state, to_jsonable
from zyra_evaluation import evaluate_task_trace
from zyra_evaluation.live_benchmark import LiveBenchmarkFreezeGate
from zyra_evaluation.live_benchmark.canonical import digest
from zyra_evaluation.regression_hardening import RegressionFreezeGate
from zyra_orchestration.deployment import DeploymentEvidenceGate
from zyra_orchestration import GraphExecutionContext, run_task_graph
from zyra_runtime import default_tool_registry, default_worker_descriptors
from zyra_symbolic import ConstraintKeeper, TopologyRouter, apply_failure_injection, apply_requirement_change


def verify_typescript_skill_owner() -> None:
    candidates = (
        ROOT / "node_modules" / ".bin" / "bun.exe",
        ROOT / "node_modules" / ".bin" / "bun",
    )
    bun = next((str(path) for path in candidates if path.is_file()), None)
    if bun is None:
        bun = shutil.which("bun")
    if bun is None:
        raise AssertionError(
            "Bun is required to verify the canonical TypeScript skill owner"
        )
    completed = subprocess.run(
        [bun, str(ROOT / "scripts" / "verify_m3_skills.ts")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise AssertionError(
            "canonical TypeScript skill verification failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


def verify_runtime() -> None:
    workers = {worker.name for worker in default_worker_descriptors()}
    assert "CodeWorkerRuntime" in workers
    assert "BrowserWorker" in workers
    assert default_tool_registry().get("trace") is not None
    verify_typescript_skill_owner()

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        state = create_task_state("M3 verify: edit code, route workers, preserve symbolic trace.")
        context = GraphExecutionContext.from_paths(
            project_root=ROOT,
            workspace_root=base / "workspace",
            artifact_root=base / "artifacts",
        )
        events = [
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.TASK_CREATED,
                node_id=state.root_node_id,
                payload={"task": to_jsonable(state)},
            )
        ]
        events.extend(run_task_graph(state, execution_context=context))

        assert state.status == PlanNodeStatus.COMPLETED
        assert state.metadata["graph_version"] == "m3-symbolic-v1"
        assert state.decisions
        assert state.decisions[0].checks
        assert any(event.event_type == EventType.CONSTRAINT_CHECK for event in events)
        assert any(event.event_type == EventType.TOPOLOGY_ROUTE for event in events)
        assert any(event.event_type == EventType.AGENT_MESSAGE for event in events)
        assert any("tool_result" in event.payload for event in events)

        execute_node = next(node for node in state.plan_nodes.values() if node.metadata.get("stage") == "execute")
        assert execute_node.assigned_worker_id == "CodeWorkerRuntime"
        assert ConstraintKeeper().check_task_state(state, node=execute_node)
        route_decision, route_event = TopologyRouter().route(state, node=execute_node)
        assert route_decision.selected in workers
        assert route_event.payload["selected_worker"] == route_decision.selected
        assert route_event.event_type == EventType.TOPOLOGY_ROUTE

        page = base / "workspace" / "m3.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<html><head><title>M3</title></head><body>M3 browser route.</body></html>", encoding="utf-8")
        browser_state = create_task_state(f"M3 verify browser route {page.resolve().as_uri()}")
        browser_state.metadata["runtime_hints"] = {
            "preferred_worker": "BrowserWorker",
            "browser_backend": "static",
            "browser_plan": [
                {
                    "action": "open_url",
                    "arguments": {"url": page.resolve().as_uri()},
                }
            ],
            "allowed_schemes": ["file"],
        }
        run_task_graph(
            browser_state,
            execution_context=GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=base / "browser-workspace",
                artifact_root=base / "browser-artifacts",
            ),
        )
        browser_execute = next(node for node in browser_state.plan_nodes.values() if node.metadata.get("stage") == "execute")
        assert browser_execute.assigned_worker_id == "BrowserWorker", (
            browser_execute.assigned_worker_id
        )

        change_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.REQUIREMENT_CHANGE,
            node_id=state.root_node_id,
            payload={"raw": "tighten verifier evidence and keep existing artifacts"},
        )
        events.append(change_event)
        events.extend(apply_requirement_change(state, change_event))
        assert state.metadata["requirement_changes"][0]["affected_node_ids"]
        assert state.decisions[-1].decision_type == "requirement_change_replan"
        assert state.decisions[-1].checks

        failure_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.FAILURE_INJECTED,
            node_id=state.root_node_id,
            payload={"raw": "node=execute transient worker failure"},
        )
        events.append(failure_event)
        events.extend(apply_failure_injection(state, failure_event))
        assert state.metadata["failure_injections"][0]["recovery_node_id"] in state.plan_nodes
        assert state.decisions[-1].decision_type == "failure_recovery_route"
        assert state.decisions[-1].checks

        events.extend(run_task_graph(state, execution_context=context))
        assert state.status == PlanNodeStatus.COMPLETED

        evaluation = evaluate_task_trace(to_jsonable(state), [to_jsonable(event) for event in events])
        assert evaluation["metrics"]["constraint_check_count"] >= 4
        assert evaluation["metrics"]["topology_route_count"] >= 3
        assert evaluation["metrics"]["structured_message_count"] >= 5
        assert evaluation["metrics"]["decision_record_count"] >= 3
        assert evaluation["metrics"]["replanned_node_count"] >= 1

    print("M3 runtime verification passed")


def verify_regression_freeze_admission() -> None:
    evidence_root = (
        ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M3-S02A-01"
    )
    metadata_path = evidence_root / "implementation-metadata.json"
    receipt_path = (
        evidence_root
        / "runtime-artifacts"
        / "suite"
        / "suite-receipt.json"
    )
    live_matrix_path = evidence_root / "live-matrix-receipt.json"
    if not metadata_path.is_file():
        raise AssertionError(
            f"M3 regression implementation metadata is missing: {metadata_path}"
        )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    implementation_commit = str(metadata.get("implementation_commit") or "")
    if len(implementation_commit) != 40:
        raise AssertionError(
            "M3 regression implementation metadata lacks an exact commit"
        )
    report = RegressionFreezeGate(
        ROOT,
        live_matrix_path=live_matrix_path,
    ).verify_file(
        receipt_path,
        expected_revision=implementation_commit,
    )
    if not report.valid:
        raise AssertionError(
            "M3 regression freeze admission failed: "
            + json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        )
    if report.live_matrix_digest != str(
        metadata.get("live_matrix_receipt_digest") or ""
    ):
        raise AssertionError(
            "M3 regression live-matrix digest differs from implementation metadata"
        )
    if report.suite_digest != str(
        metadata.get("suite_receipt_digest") or ""
    ):
        raise AssertionError(
            "M3 regression suite digest differs from implementation metadata"
        )


def verify_live_benchmark_freeze_admission() -> None:
    evidence_parent = (
        ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M3-S02A-02"
    )
    pointer_path = evidence_parent / "formal-current.json"
    if not pointer_path.is_file():
        raise AssertionError(
            f"M3 formal live benchmark pointer is missing: {pointer_path}"
        )
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    projection = dict(pointer)
    declared = str(projection.pop("pointer_digest", "") or "")
    if declared != digest(projection):
        raise AssertionError("M3 formal live benchmark pointer digest is invalid")
    relative = str(pointer.get("relative_evidence_root") or "")
    evidence_root = (ROOT / relative).resolve(strict=True)
    parent = evidence_parent.resolve(strict=True)
    if evidence_root == parent or parent not in evidence_root.parents:
        raise AssertionError("M3 formal live benchmark evidence path escaped")
    implementation_commit = str(pointer.get("implementation_commit") or "")
    if len(implementation_commit) != 40:
        raise AssertionError(
            "M3 formal live benchmark pointer lacks an exact implementation commit"
        )
    if pointer.get("no_new_provider_call") is not True:
        raise AssertionError(
            "M3 formal live benchmark changed the no-new-provider-call boundary"
        )
    receipt = LiveBenchmarkFreezeGate().verify(
        evidence_root,
        expected_commit=implementation_commit,
    )
    stored_path = evidence_root / "freeze-admission.json"
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    stored_projection = dict(stored)
    stored_digest = str(stored_projection.pop("receipt_digest", "") or "")
    if stored_digest != digest(stored_projection):
        raise AssertionError(
            "M3 stored formal live benchmark freeze receipt digest is invalid"
        )
    if stored_digest != pointer.get("freeze_admission_digest"):
        raise AssertionError(
            "M3 stored formal live benchmark freeze receipt differs from its pointer"
        )
    stable_fields = (
        "valid",
        "target_commit",
        "campaign_id",
        "report_digest",
        "evidence_index_digest",
        "manifest_digest",
        "score",
        "human_intervention_count",
        "operator_intervention_count",
    )
    if any(stored.get(key) != receipt.get(key) for key in stable_fields):
        raise AssertionError(
            "M3 formal live benchmark repeat verification changed stable fields"
        )


def verify_deployment_freeze_admission() -> None:
    evidence_path = (
        ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M3-S02B-01"
        / "verification-summary.json"
    )
    if not evidence_path.is_file():
        raise AssertionError(
            f"M3 deployment verification summary is missing: {evidence_path}"
        )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    implementation_commit = str(evidence.get("target_commit") or "")
    if len(implementation_commit) != 40:
        raise AssertionError(
            "M3 deployment verification summary lacks an exact implementation commit"
        )
    result = DeploymentEvidenceGate(
        ROOT,
        evidence_path=evidence_path,
    ).require(expected_commit=implementation_commit)
    if result.get("ready") is not True:
        raise AssertionError(
            "M3 deployment freeze admission did not return a ready receipt"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the M3 runtime and exact-revision regression freeze gate."
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help=(
            "run the inner generated-task runtime probe used by the live matrix; "
            "the outer default invocation also requires regression admission"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    verify_runtime()
    if args.runtime_only:
        return
    verify_regression_freeze_admission()
    verify_live_benchmark_freeze_admission()
    verify_deployment_freeze_admission()
    print("M3 verification passed")


if __name__ == "__main__":
    main()
