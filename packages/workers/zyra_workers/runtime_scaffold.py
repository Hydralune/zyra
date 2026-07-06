from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, to_jsonable
from zyra_runtime.scaffold import RuntimeScaffold, WorkerBridgeContract, default_m1_01b_runtime_scaffold


class WorkerScaffoldKind(StrEnum):
    CODE = "code"
    BROWSER = "browser"
    SANDBOX = "sandbox"
    MEMORY = "memory"
    SCHEDULER = "scheduler"


class WorkerSmokeStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class WorkerScaffoldHealth:
    worker_id: str
    worker_kind: WorkerScaffoldKind
    ok: bool
    status: WorkerSmokeStatus
    checks: dict[str, bool] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class WorkerSmokeResult:
    worker_id: str
    worker_kind: WorkerScaffoldKind
    ok: bool
    scenario: str
    event_payload: dict[str, Any]
    artifacts: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class WorkerScaffold(Protocol):
    contract: WorkerBridgeContract

    def health(self) -> WorkerScaffoldHealth:
        ...

    def smoke(self) -> WorkerSmokeResult:
        ...

    def event(self, run_id: str, task_id: str) -> EventRecord:
        ...


@dataclass(slots=True)
class BaseWorkerScaffold:
    contract: WorkerBridgeContract
    runtime_scaffold: RuntimeScaffold
    project_root: Path = field(default_factory=lambda: Path(".").resolve())

    @property
    def worker_kind(self) -> WorkerScaffoldKind:
        return WorkerScaffoldKind(self.contract.worker_kind)

    def health(self) -> WorkerScaffoldHealth:
        checks = self._checks()
        ok = all(checks.values())
        return WorkerScaffoldHealth(
            worker_id=self.contract.worker_id,
            worker_kind=self.worker_kind,
            ok=ok,
            status=WorkerSmokeStatus.PASSING if ok else WorkerSmokeStatus.WARNING,
            checks=checks,
            messages=self._messages(checks),
            metadata={
                "runtime_kind": self.contract.runtime_kind,
                "capability_count": len(self.contract.capabilities),
                "tool_count": len(self.contract.tools),
                "skill_count": len(self.contract.skills),
                "entrypoint": self.contract.entrypoint,
            },
        )

    def smoke(self) -> WorkerSmokeResult:
        health = self.health()
        scenario = str(self.contract.smoke_profile.get("scenario") or self.contract.smoke_profile.get("tool_plan") or "scaffold")
        return WorkerSmokeResult(
            worker_id=self.contract.worker_id,
            worker_kind=self.worker_kind,
            ok=health.ok,
            scenario=scenario,
            event_payload={
                "worker_scaffold_smoke": {
                    "worker_id": self.contract.worker_id,
                    "worker_kind": self.contract.worker_kind,
                    "runtime_kind": self.contract.runtime_kind,
                    "scenario": scenario,
                    "health": health.to_dict(),
                }
            },
            messages=health.messages,
        )

    def event(self, run_id: str, task_id: str) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=self.contract.worker_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "worker_scaffold_health": self.health().to_dict(),
                "worker_bridge_contract": self.contract.to_dict(),
            },
        )

    def _checks(self) -> dict[str, bool]:
        return {
            "has_worker_id": bool(self.contract.worker_id),
            "has_runtime_kind": bool(self.contract.runtime_kind),
            "has_entrypoint": bool(self.contract.entrypoint),
            "has_capabilities": bool(self.contract.capabilities),
            "runtime_scaffold_ok": bool(self.runtime_scaffold.health()["ok"]),
        }

    def _messages(self, checks: dict[str, bool]) -> list[str]:
        return [name for name, ok in checks.items() if not ok]


class CodeWorkerScaffold(BaseWorkerScaffold):
    def _checks(self) -> dict[str, bool]:
        runtime_root = self.project_root / "vendor-runtimes" / "claude-code-runtime"
        return {
            **super()._checks(),
            "has_tool_loop_contract": any(component.surface == "tool_loop" for component in self.runtime_scaffold.components),
            "has_permission_contract": any(component.surface == "permission" for component in self.runtime_scaffold.components),
            "has_runtime_root": runtime_root.exists(),
            "has_pilot_manifest": (runtime_root / "src" / "zyra-pilot-manifest.mjs").exists(),
            "has_productized_manifest": (runtime_root / "src" / "zyra-productized-manifest.mjs").exists(),
            "has_reference_crosswalk": (runtime_root / "metadata" / "reference_crosswalk.json").exists(),
        }


class BrowserWorkerScaffold(BaseWorkerScaffold):
    def _checks(self) -> dict[str, bool]:
        return {
            **super()._checks(),
            "has_browser_tool": "browser" in self.contract.tools,
            "has_artifact_capability": any("artifact" in item for item in self.contract.capabilities),
        }


class SandboxWorkerScaffold(BaseWorkerScaffold):
    def _checks(self) -> dict[str, bool]:
        tmp_root = self.project_root / "tmp"
        return {
            **super()._checks(),
            "has_workspace_boundary": any("workspace" in item for item in self.contract.capabilities),
            "project_tmp_available": tmp_root.exists() or tmp_root.parent.exists(),
        }


class MemoryWorkerScaffold(BaseWorkerScaffold):
    def _checks(self) -> dict[str, bool]:
        return {
            **super()._checks(),
            "has_memory_capability": any("memory" in item for item in self.contract.capabilities),
            "memory_package_present": (self.project_root / "packages" / "memory" / "zyra_memory").exists(),
        }


class SchedulerWorkerScaffold(BaseWorkerScaffold):
    def _checks(self) -> dict[str, bool]:
        return {
            **super()._checks(),
            "has_scheduler_capability": any("resource" in item or "recovery" in item for item in self.contract.capabilities),
            "scheduler_package_present": (self.project_root / "packages" / "scheduler" / "zyra_scheduler").exists(),
        }


WORKER_SCAFFOLD_TYPES = {
    WorkerScaffoldKind.CODE: CodeWorkerScaffold,
    WorkerScaffoldKind.BROWSER: BrowserWorkerScaffold,
    WorkerScaffoldKind.SANDBOX: SandboxWorkerScaffold,
    WorkerScaffoldKind.MEMORY: MemoryWorkerScaffold,
    WorkerScaffoldKind.SCHEDULER: SchedulerWorkerScaffold,
}


def build_worker_scaffold(
    contract: WorkerBridgeContract,
    runtime_scaffold: RuntimeScaffold,
    *,
    project_root: str | Path | None = None,
) -> BaseWorkerScaffold:
    kind = WorkerScaffoldKind(contract.worker_kind)
    scaffold_type = WORKER_SCAFFOLD_TYPES[kind]
    return scaffold_type(
        contract=contract,
        runtime_scaffold=runtime_scaffold,
        project_root=Path(project_root or ".").resolve(),
    )


def build_m1_01b_worker_scaffolds(
    runtime_scaffold: RuntimeScaffold | None = None,
    *,
    project_root: str | Path | None = None,
) -> list[BaseWorkerScaffold]:
    scaffold = runtime_scaffold or default_m1_01b_runtime_scaffold(project_root)
    return [
        build_worker_scaffold(contract, scaffold, project_root=project_root)
        for contract in scaffold.worker_bridges
    ]


def worker_scaffold_health_payload(
    runtime_scaffold: RuntimeScaffold | None = None,
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    workers = build_m1_01b_worker_scaffolds(runtime_scaffold, project_root=project_root)
    health = [worker.health() for worker in workers]
    return {
        "ok": all(item.ok for item in health),
        "worker_count": len(health),
        "workers": [item.to_dict() for item in health],
    }


def worker_scaffold_events(
    runtime_scaffold: RuntimeScaffold | None = None,
    *,
    project_root: str | Path | None = None,
    run_id: str = "m1-01b",
    task_id: str = "worker-scaffold",
) -> list[EventRecord]:
    workers = build_m1_01b_worker_scaffolds(runtime_scaffold, project_root=project_root)
    return [worker.event(run_id, task_id) for worker in workers]


__all__ = [
    "BaseWorkerScaffold",
    "BrowserWorkerScaffold",
    "CodeWorkerScaffold",
    "MemoryWorkerScaffold",
    "SandboxWorkerScaffold",
    "SchedulerWorkerScaffold",
    "WorkerScaffold",
    "WorkerScaffoldHealth",
    "WorkerScaffoldKind",
    "WorkerSmokeResult",
    "WorkerSmokeStatus",
    "build_m1_01b_worker_scaffolds",
    "build_worker_scaffold",
    "worker_scaffold_events",
    "worker_scaffold_health_payload",
]
