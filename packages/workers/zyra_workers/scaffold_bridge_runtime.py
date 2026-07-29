from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
from zyra_runtime import (
    LocalArtifactStore,
    RuntimeScaffold,
    RuntimeSurface,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolPermissionPolicy,
    ToolResult,
    WorkerBridgeContract,
    default_m1_01b_runtime_scaffold,
)
from zyra_runtime.scaffold_lifecycle import RuntimeScaffoldLifecycle, build_default_lifecycle

from .runtime_scaffold import WorkerScaffoldKind


class BridgeProbeStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    FAILING = "failing"


@dataclass(frozen=True, slots=True)
class BridgeProbeStep:
    name: str
    ok: bool
    status: BridgeProbeStatus
    message: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class BridgeProbeResult:
    worker_id: str
    worker_kind: str
    ok: bool
    scenario: str
    steps: list[BridgeProbeStep]
    artifacts: list[str] = field(default_factory=list)
    event_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "worker_kind": self.worker_kind,
            "ok": self.ok,
            "scenario": self.scenario,
            "steps": [step.to_dict() for step in self.steps],
            "artifacts": list(self.artifacts),
            "event_payload": to_jsonable(self.event_payload),
        }


class WorkerBridgeRuntime:
    def __init__(
        self,
        contract: WorkerBridgeContract,
        lifecycle: RuntimeScaffoldLifecycle,
        *,
        project_root: str | Path,
        artifact_root: str | Path | None = None,
    ) -> None:
        self.contract = contract
        self.lifecycle = lifecycle
        self.project_root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root or self.project_root / "tmp" / "m1_01b_scaffold_artifacts").resolve()
        self.artifact_store = LocalArtifactStore(self.artifact_root)

    @property
    def worker_kind(self) -> WorkerScaffoldKind:
        return WorkerScaffoldKind(self.contract.worker_kind)

    def probe(self) -> BridgeProbeResult:
        common = self._common_steps()
        specialized = {
            WorkerScaffoldKind.CODE: self._probe_code,
            WorkerScaffoldKind.BROWSER: self._probe_browser,
            WorkerScaffoldKind.SANDBOX: self._probe_sandbox,
            WorkerScaffoldKind.MEMORY: self._probe_memory,
            WorkerScaffoldKind.SCHEDULER: self._probe_scheduler,
        }[self.worker_kind]()
        steps = [*common, *specialized]
        ok = all(step.ok for step in steps)
        artifact = self.artifact_store.write_text(
            run_id="m1-01b",
            task_id="worker-bridge",
            content=json.dumps([step.to_dict() for step in steps], ensure_ascii=False, indent=2, sort_keys=True),
            title=f"worker bridge probe {self.contract.worker_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=self.contract.worker_id,
        )
        payload = {
            "worker_bridge_probe": {
                "worker_id": self.contract.worker_id,
                "worker_kind": self.contract.worker_kind,
                "ok": ok,
                "scenario": self.contract.smoke_profile,
                "step_count": len(steps),
                "artifact_id": artifact.artifact_id,
            }
        }
        self.lifecycle.record_smoke(self.contract.worker_id, ok, payload)
        return BridgeProbeResult(
            worker_id=self.contract.worker_id,
            worker_kind=self.contract.worker_kind,
            ok=ok,
            scenario=str(self.contract.smoke_profile.get("scenario") or self.contract.smoke_profile.get("tool_plan") or "bridge-probe"),
            steps=steps,
            artifacts=[artifact.artifact_id],
            event_payload=payload,
        )

    def event(self, result: BridgeProbeResult) -> EventRecord:
        return EventRecord(
            run_id="m1-01b",
            task_id="worker-bridge",
            node_id=self.contract.worker_id,
            event_type=EventType.AGENT_MESSAGE,
            payload=result.event_payload,
        )

    def _common_steps(self) -> list[BridgeProbeStep]:
        entrypoint = self.project_root / self.contract.entrypoint if self.contract.entrypoint and not Path(self.contract.entrypoint).is_absolute() else Path(self.contract.entrypoint or "")
        return [
            _step("worker_id", bool(self.contract.worker_id), "worker id is present", {"worker_id": self.contract.worker_id}),
            _step("runtime_kind", bool(self.contract.runtime_kind), "runtime kind is present", {"runtime_kind": self.contract.runtime_kind}),
            _step("capabilities", bool(self.contract.capabilities), "capabilities are present", {"capabilities": list(self.contract.capabilities)}),
            _step(
                "entrypoint_declared",
                bool(self.contract.entrypoint),
                "entrypoint is declared",
                {"entrypoint": self.contract.entrypoint, "exists": entrypoint.exists()},
                warning_only=bool(self.contract.entrypoint) and not entrypoint.exists(),
            ),
            _step(
                "lifecycle_healthy",
                self.lifecycle.snapshot().ok,
                "runtime lifecycle snapshot is healthy",
                {"phase": str(self.lifecycle.phase)},
            ),
        ]

    def _probe_code(self) -> list[BridgeProbeStep]:
        runtime_root = self.project_root / "packages" / "runtime" / "claude-runtime"
        package_manifest = runtime_root / "package.json"
        source_identity = runtime_root / "zyra-source.json"
        entrypoint = self.project_root / "apps" / "code-worker" / "src" / "main.ts"
        query_contract = any(surface.surface == RuntimeSurface.TOOL_LOOP for surface in self.lifecycle.scaffold.components)
        return [
            _step("formal_runtime_root", runtime_root.is_dir(), "formal claude runtime root exists", {"path": str(runtime_root)}),
            _step("formal_runtime_package", package_manifest.is_file(), "formal runtime package exists", {"path": str(package_manifest)}),
            _step("formal_source_identity", source_identity.is_file(), "formal source identity exists", {"path": str(source_identity)}),
            _step("canonical_typescript_entrypoint", entrypoint.is_file(), "canonical TypeScript entrypoint exists", {"path": str(entrypoint)}),
            _step("tool_loop_surface", query_contract, "tool loop surface is declared"),
            _step("permission_surface", str(RuntimeSurface.PERMISSION) in self.lifecycle.surfaces, "permission surface is tracked"),
        ]

    def _probe_browser(self) -> list[BridgeProbeStep]:
        context = _tool_context(self.project_root, self.artifact_root)
        result = self._execute_guarded_probe(
            context,
            ToolCall(
                run_id="m1-01b",
                task_id="browser-probe",
                node_id=self.contract.worker_id,
                tool_name="browser",
                arguments={
                    "action": "extract_text",
                    "html": "<html><title>Zyra Browser Probe</title><body><a href='/ok'>ok</a><p>browser scaffold</p></body></html>",
                    "capture_html": True,
                },
            ),
            explicit_low_risk=True,
        )
        raw_state = (
            result.output.get("state")
            if isinstance(result.output, dict)
            else None
        )
        state = raw_state if isinstance(raw_state, dict) else {}
        return [
            _step("browser_tool_registered", "browser" in self.contract.tools, "browser tool is declared"),
            _step("browser_tool_executes", result.ok, result.summary, {"artifact_count": len(result.artifacts)}),
            _step("browser_extracts_text", "browser scaffold" in str(state.get("text_preview") or ""), "browser text extraction changes output"),
        ]

    def _probe_sandbox(self) -> list[BridgeProbeStep]:
        with tempfile.TemporaryDirectory(dir=self.project_root / "tmp" if (self.project_root / "tmp").exists() else None) as tmpdir:
            sandbox_root = Path(tmpdir).resolve()
            context = ToolExecutionContext.for_workspace(
                workspace_root=sandbox_root,
                artifact_root=self.artifact_root,
                permission_policy=ToolPermissionPolicy.for_workspace(sandbox_root),
            )
            write = self._execute_guarded_probe(
                context,
                ToolCall(
                    run_id="m1-01b",
                    task_id="sandbox-probe",
                    node_id=self.contract.worker_id,
                    tool_name="file_write",
                    arguments={"path": "probe.txt", "content": "sandbox scaffold"},
                ),
            )
            executor = ToolExecutor(context)
            read = executor.execute(
                ToolCall(
                    run_id="m1-01b",
                    task_id="sandbox-probe",
                    node_id=self.contract.worker_id,
                    tool_name="file_read",
                    arguments={"path": "probe.txt"},
                )
            )
            outside = executor.execute(
                ToolCall(
                    run_id="m1-01b",
                    task_id="sandbox-probe",
                    node_id=self.contract.worker_id,
                    tool_name="file_read",
                    arguments={"path": str(self.project_root / "README.md")},
                )
            )
        return [
            _step("sandbox_write", write.ok, write.summary, write.output),
            _step("sandbox_read", read.ok and "sandbox scaffold" in json.dumps(read.output), read.summary, read.output),
            _step("sandbox_blocks_parent_read", not outside.ok and outside.error == "permission_denied", outside.summary, outside.metadata),
        ]

    def _execute_guarded_probe(
        self,
        context: ToolExecutionContext,
        call: ToolCall,
        *,
        explicit_low_risk: bool = False,
    ):
        del context, explicit_low_risk
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary="Legacy scaffold probe requires the TypeScript E02 permission host.",
            error="typescript_permission_receipt_required",
            metadata={
                "canonical_permission_owner": "typescript",
                "python_policy_fallback": "false",
                "probe_execution_disabled": "true",
            },
        )

    def _probe_memory(self) -> list[BridgeProbeStep]:
        memory_root = self.project_root / "packages" / "memory" / "zyra_memory"
        event_surfaces = [event for event in self.lifecycle.events if event.surface in {RuntimeSurface.SESSION, RuntimeSurface.TOOL_LOOP}]
        artifact = self.artifact_store.write_text(
            run_id="m1-01b",
            task_id="memory-probe",
            content=json.dumps([event.to_dict() for event in event_surfaces], ensure_ascii=False, indent=2, sort_keys=True),
            title="runtime scaffold memory projection",
            kind=ArtifactKind.TRACE,
            extension=".json",
            producer_node_id=self.contract.worker_id,
        )
        return [
            _step("memory_package", memory_root.exists(), "memory package boundary exists", {"path": str(memory_root)}),
            _step("memory_projection", bool(event_surfaces), "session/tool loop events can be projected into memory", {"event_count": len(event_surfaces)}),
            _step("memory_artifact", bool(artifact.artifact_id), "memory projection artifact was written", {"artifact_id": artifact.artifact_id}),
        ]

    def _probe_scheduler(self) -> list[BridgeProbeStep]:
        scheduler_root = self.project_root / "packages" / "scheduler" / "zyra_scheduler"
        worker_count = len(self.lifecycle.workers)
        code_worker = next((worker for worker in self.lifecycle.workers if worker.worker_kind == "code"), None)
        selected = code_worker or (self.lifecycle.workers[0] if self.lifecycle.workers else None)
        return [
            _step("scheduler_package", scheduler_root.exists(), "scheduler package boundary exists", {"path": str(scheduler_root)}),
            _step("worker_pool_visible", worker_count >= 5, "runtime lifecycle exposes worker pool", {"worker_count": worker_count}),
            _step("selects_worker", selected is not None, "scheduler scaffold can select a worker", {"selected": selected.worker_id if selected else ""}),
        ]


def build_bridge_runtimes(
    *,
    project_root: str | Path,
    lifecycle: RuntimeScaffoldLifecycle | None = None,
    scaffold: RuntimeScaffold | None = None,
) -> list[WorkerBridgeRuntime]:
    root = Path(project_root).resolve()
    active_lifecycle = lifecycle or RuntimeScaffoldLifecycle(scaffold or default_m1_01b_runtime_scaffold(root), project_root=root).bootstrap()
    return [
        WorkerBridgeRuntime(worker, active_lifecycle, project_root=root)
        for worker in active_lifecycle.scaffold.worker_bridges
    ]


def run_worker_bridge_probes(project_root: str | Path) -> dict[str, Any]:
    lifecycle = build_default_lifecycle(project_root, complete=False)
    runtimes = build_bridge_runtimes(project_root=project_root, lifecycle=lifecycle)
    results = [runtime.probe() for runtime in runtimes]
    lifecycle.complete(ok=all(result.ok for result in results))
    return {
        "ok": all(result.ok for result in results) and lifecycle.snapshot().ok,
        "worker_count": len(results),
        "results": [result.to_dict() for result in results],
        "lifecycle": lifecycle.snapshot().to_dict(),
    }


def worker_bridge_event_records(project_root: str | Path) -> list[EventRecord]:
    lifecycle = build_default_lifecycle(project_root, complete=False)
    records: list[EventRecord] = []
    for runtime in build_bridge_runtimes(project_root=project_root, lifecycle=lifecycle):
        result = runtime.probe()
        records.append(runtime.event(result))
    lifecycle.complete()
    records.extend(lifecycle.event_records(run_id="m1-01b", task_id="worker-bridge"))
    return records


def _tool_context(project_root: Path, artifact_root: Path) -> ToolExecutionContext:
    workspace = project_root / "tmp" / "m1_01b_worker_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolExecutionContext.for_workspace(
        workspace_root=workspace,
        artifact_root=artifact_root,
        permission_policy=ToolPermissionPolicy.for_workspace(workspace),
    )


def _step(
    name: str,
    ok: bool,
    message: str,
    payload: Mapping[str, Any] | None = None,
    *,
    warning_only: bool = False,
) -> BridgeProbeStep:
    status = BridgeProbeStatus.PASSING if ok else BridgeProbeStatus.WARNING if warning_only else BridgeProbeStatus.FAILING
    return BridgeProbeStep(
        name=name,
        ok=ok or warning_only,
        status=status,
        message=message,
        payload=dict(payload or {}),
    )
