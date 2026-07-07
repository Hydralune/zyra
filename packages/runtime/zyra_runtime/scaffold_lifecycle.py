from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .scaffold import (
    McpServerContract,
    PermissionDecisionContract,
    RuntimeOperation,
    RuntimeScaffold,
    RuntimeSessionContract,
    RuntimeSurface,
    SkillContract,
    SubagentExecutionContract,
    ToolLoopStateContract,
    WorkerBridgeContract,
    default_m1_01b_runtime_scaffold,
)


class ScaffoldLifecyclePhase(StrEnum):
    CREATED = "created"
    SESSION_STARTED = "session_started"
    TOOL_LOOP_OPENED = "tool_loop_opened"
    PERMISSION_RECORDED = "permission_recorded"
    MCP_REGISTERED = "mcp_registered"
    SKILL_LOADED = "skill_loaded"
    SUBAGENT_DECLARED = "subagent_declared"
    WORKER_BRIDGE_READY = "worker_bridge_ready"
    SMOKE_RUNNING = "smoke_running"
    COMPLETED = "completed"
    FAILED = "failed"


class ScaffoldMutationKind(StrEnum):
    START_SESSION = "start_session"
    OPEN_TOOL_LOOP = "open_tool_loop"
    RECORD_PERMISSION = "record_permission"
    REGISTER_MCP = "register_mcp"
    LOAD_SKILL = "load_skill"
    DECLARE_SUBAGENT = "declare_subagent"
    REGISTER_WORKER = "register_worker"
    RECORD_SMOKE = "record_smoke"
    COMPLETE = "complete"
    DISCONNECT = "disconnect"


class SurfaceFaultKind(StrEnum):
    DISCONNECT = "disconnect"
    NOT_READY = "not_ready"
    CLEAR_STATE_REF = "clear_state_ref"
    DROP_EVENT_REF = "drop_event_ref"


class ScaffoldInvariantSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ScaffoldLifecycleEvent:
    event_id: str
    phase: ScaffoldLifecyclePhase
    mutation: ScaffoldMutationKind
    surface: RuntimeSurface | None
    message: str
    created_at: str = field(default_factory=now_iso)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "phase": str(self.phase),
            "mutation": str(self.mutation),
            "surface": str(self.surface) if self.surface else "",
            "message": self.message,
            "created_at": self.created_at,
            "payload": to_jsonable(self.payload),
        }


@dataclass(frozen=True, slots=True)
class ScaffoldInvariant:
    code: str
    severity: ScaffoldInvariantSeverity
    message: str
    surface: RuntimeSurface | None = None
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "surface": str(self.surface) if self.surface else "",
            "remediation": self.remediation,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(slots=True)
class RuntimeSurfaceState:
    surface: RuntimeSurface
    ready: bool = False
    connected: bool = False
    owner: str = "zyra"
    state_ref: str = ""
    last_event_id: str = ""
    mutation_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def apply(self, event: ScaffoldLifecycleEvent, *, connected: bool | None = None, ready: bool | None = None) -> None:
        self.last_event_id = event.event_id
        self.mutation_count += 1
        if connected is not None:
            self.connected = connected
        if ready is not None:
            self.ready = ready
        self.metadata.update(event.payload)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ScaffoldStateSnapshot:
    scaffold_id: str
    owner_unit: str
    phase: ScaffoldLifecyclePhase
    session: RuntimeSessionContract | None
    tool_loop: ToolLoopStateContract | None
    permission_decisions: list[PermissionDecisionContract]
    mcp_servers: list[McpServerContract]
    skills: list[SkillContract]
    subagents: list[SubagentExecutionContract]
    workers: list[WorkerBridgeContract]
    surfaces: dict[str, RuntimeSurfaceState]
    events: list[ScaffoldLifecycleEvent]
    invariants: list[ScaffoldInvariant]
    created_at: str
    updated_at: str

    @property
    def ok(self) -> bool:
        return not any(item.severity in {ScaffoldInvariantSeverity.ERROR, ScaffoldInvariantSeverity.BLOCKER} for item in self.invariants)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scaffold_id": self.scaffold_id,
            "owner_unit": self.owner_unit,
            "phase": str(self.phase),
            "session": self.session.to_dict() if self.session else None,
            "tool_loop": self.tool_loop.to_dict() if self.tool_loop else None,
            "permission_decisions": [item.to_dict() for item in self.permission_decisions],
            "mcp_servers": [item.to_dict() for item in self.mcp_servers],
            "skills": [item.to_dict() for item in self.skills],
            "subagents": [item.to_dict() for item in self.subagents],
            "workers": [item.to_dict() for item in self.workers],
            "surfaces": {key: value.to_dict() for key, value in self.surfaces.items()},
            "events": [item.to_dict() for item in self.events],
            "invariants": [item.to_dict() for item in self.invariants],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class DisconnectProbe:
    surface: RuntimeSurface
    disabled: bool
    baseline_ok: bool
    disconnected_ok: bool
    changed: bool
    message: str
    failing_invariants: list[ScaffoldInvariant] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.disabled and self.baseline_ok and not self.disconnected_ok and self.changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "surface": str(self.surface),
            "disabled": self.disabled,
            "baseline_ok": self.baseline_ok,
            "disconnected_ok": self.disconnected_ok,
            "changed": self.changed,
            "message": self.message,
            "failing_invariants": [item.to_dict() for item in self.failing_invariants],
        }


@dataclass(frozen=True, slots=True)
class SurfaceFaultInjection:
    injection_id: str
    surface: RuntimeSurface
    fault_kind: SurfaceFaultKind
    expected_invariant_codes: tuple[str, ...]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class SurfaceFaultResult:
    injection: SurfaceFaultInjection
    baseline_ok: bool
    faulted_ok: bool
    changed: bool
    matched_expected_invariant: bool
    invariant_codes: tuple[str, ...]
    event_count_before: int
    event_count_after: int

    @property
    def ok(self) -> bool:
        return self.baseline_ok and not self.faulted_ok and self.changed and self.matched_expected_invariant

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ScaffoldFaultMatrixReport:
    scaffold_id: str
    owner_unit: str
    results: list[SurfaceFaultResult]
    summary: dict[str, Any]

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(result.ok for result in self.results)

    @property
    def failed_results(self) -> list[SurfaceFaultResult]:
        return [result for result in self.results if not result.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scaffold_id": self.scaffold_id,
            "owner_unit": self.owner_unit,
            "results": [result.to_dict() for result in self.results],
            "summary": dict(self.summary),
        }


class RuntimeScaffoldLifecycle:
    def __init__(self, scaffold: RuntimeScaffold, *, project_root: str | Path | None = None) -> None:
        self.scaffold = scaffold
        self.project_root = Path(project_root or ".").resolve()
        self.phase = ScaffoldLifecyclePhase.CREATED
        self.created_at = now_iso()
        self.updated_at = self.created_at
        self.session: RuntimeSessionContract | None = None
        self.tool_loop: ToolLoopStateContract | None = None
        self.permission_decisions: list[PermissionDecisionContract] = []
        self.mcp_servers: list[McpServerContract] = []
        self.skills: list[SkillContract] = []
        self.subagents: list[SubagentExecutionContract] = []
        self.workers: list[WorkerBridgeContract] = []
        self.events: list[ScaffoldLifecycleEvent] = []
        self.surfaces: dict[str, RuntimeSurfaceState] = {
            str(surface): RuntimeSurfaceState(surface=surface, state_ref=f"runtime-scaffold:{surface}")
            for surface in RuntimeSurface
        }

    @classmethod
    def default(cls, project_root: str | Path | None = None) -> "RuntimeScaffoldLifecycle":
        return cls(default_m1_01b_runtime_scaffold(project_root), project_root=project_root)

    def bootstrap(self) -> "RuntimeScaffoldLifecycle":
        self.start_session(self.scaffold.session_contract)
        self.open_tool_loop(self.scaffold.tool_loop_contract)
        self.record_permission(self.scaffold.permission_contract)
        for server in self.scaffold.mcp_servers:
            self.register_mcp(server)
        for skill in self.scaffold.skills:
            self.load_skill(skill)
        self.declare_subagent(self.scaffold.subagent_contract)
        for worker in self.scaffold.worker_bridges:
            self.register_worker(worker)
        return self

    def start_session(self, session: RuntimeSessionContract) -> ScaffoldLifecycleEvent:
        self.session = session
        return self._record(
            ScaffoldMutationKind.START_SESSION,
            RuntimeSurface.SESSION,
            ScaffoldLifecyclePhase.SESSION_STARTED,
            "runtime session contract attached",
            {"session_id": session.session_id, "worker_id": session.worker_id, "event_log_ref": session.event_log_ref},
            connected=True,
            ready=True,
        )

    def open_tool_loop(self, tool_loop: ToolLoopStateContract) -> ScaffoldLifecycleEvent:
        self.tool_loop = tool_loop
        return self._record(
            ScaffoldMutationKind.OPEN_TOOL_LOOP,
            RuntimeSurface.TOOL_LOOP,
            ScaffoldLifecyclePhase.TOOL_LOOP_OPENED,
            "tool loop state contract attached",
            {"loop_id": tool_loop.loop_id, "session_id": tool_loop.session_id, "phase": tool_loop.phase},
            connected=True,
            ready=True,
        )

    def record_permission(self, decision: PermissionDecisionContract) -> ScaffoldLifecycleEvent:
        self.permission_decisions.append(decision)
        return self._record(
            ScaffoldMutationKind.RECORD_PERMISSION,
            RuntimeSurface.PERMISSION,
            ScaffoldLifecyclePhase.PERMISSION_RECORDED,
            "permission envelope recorded",
            {"request_id": decision.request_id, "effect": decision.effect, "operation": str(decision.operation)},
            connected=True,
            ready=True,
        )

    def register_mcp(self, server: McpServerContract) -> ScaffoldLifecycleEvent:
        self.mcp_servers.append(server)
        return self._record(
            ScaffoldMutationKind.REGISTER_MCP,
            RuntimeSurface.MCP,
            ScaffoldLifecyclePhase.MCP_REGISTERED,
            "MCP registry entry attached",
            {"server_id": server.server_id, "transport": server.transport, "tool_count": len(server.tools)},
            connected=server.enabled,
            ready=server.health in {"ready", "ok", "healthy"},
        )

    def load_skill(self, skill: SkillContract) -> ScaffoldLifecycleEvent:
        self.skills.append(skill)
        return self._record(
            ScaffoldMutationKind.LOAD_SKILL,
            RuntimeSurface.SKILL,
            ScaffoldLifecyclePhase.SKILL_LOADED,
            "skill metadata attached",
            {"skill_id": skill.skill_id, "name": skill.name, "allowed_tools": list(skill.allowed_tools)},
            connected=True,
            ready=bool(skill.name and skill.description),
        )

    def declare_subagent(self, subagent: SubagentExecutionContract) -> ScaffoldLifecycleEvent:
        self.subagents.append(subagent)
        return self._record(
            ScaffoldMutationKind.DECLARE_SUBAGENT,
            RuntimeSurface.SUBAGENT,
            ScaffoldLifecyclePhase.SUBAGENT_DECLARED,
            "subagent execution contract attached",
            {"subagent_id": subagent.subagent_id, "agent_type": subagent.agent_type, "tool_scope": list(subagent.tool_scope)},
            connected=True,
            ready=bool(subagent.task and subagent.parent_session_id),
        )

    def register_worker(self, worker: WorkerBridgeContract) -> ScaffoldLifecycleEvent:
        self.workers.append(worker)
        return self._record(
            ScaffoldMutationKind.REGISTER_WORKER,
            RuntimeSurface.WORKER_BRIDGE,
            ScaffoldLifecyclePhase.WORKER_BRIDGE_READY,
            "worker bridge contract attached",
            {"worker_id": worker.worker_id, "worker_kind": worker.worker_kind, "capabilities": list(worker.capabilities)},
            connected=True,
            ready=bool(worker.worker_id and worker.entrypoint and worker.capabilities),
        )

    def record_smoke(self, worker_id: str, ok: bool, payload: Mapping[str, Any]) -> ScaffoldLifecycleEvent:
        return self._record(
            ScaffoldMutationKind.RECORD_SMOKE,
            RuntimeSurface.WORKER_BRIDGE,
            ScaffoldLifecyclePhase.SMOKE_RUNNING,
            f"worker smoke {'passed' if ok else 'failed'} for {worker_id}",
            {"worker_id": worker_id, "ok": ok, "smoke": dict(payload)},
            connected=True,
            ready=ok,
        )

    def complete(self, ok: bool = True) -> ScaffoldLifecycleEvent:
        invariants = self.invariants()
        success = ok and not any(item.severity in {ScaffoldInvariantSeverity.ERROR, ScaffoldInvariantSeverity.BLOCKER} for item in invariants)
        return self._record(
            ScaffoldMutationKind.COMPLETE,
            None,
            ScaffoldLifecyclePhase.COMPLETED if success else ScaffoldLifecyclePhase.FAILED,
            "runtime scaffold lifecycle completed" if success else "runtime scaffold lifecycle failed invariants",
            {"ok": success, "invariant_count": len(invariants), "invariants": [item.to_dict() for item in invariants]},
        )

    def disconnected(self, surface: RuntimeSurface) -> "RuntimeScaffoldLifecycle":
        clone = RuntimeScaffoldLifecycle(self.scaffold, project_root=self.project_root)
        clone.session = self.session
        clone.tool_loop = self.tool_loop
        clone.permission_decisions = list(self.permission_decisions)
        clone.mcp_servers = list(self.mcp_servers)
        clone.skills = list(self.skills)
        clone.subagents = list(self.subagents)
        clone.workers = list(self.workers)
        clone.events = list(self.events)
        clone.phase = self.phase
        clone.surfaces = {key: RuntimeSurfaceState(**value.to_dict()) for key, value in self.surfaces.items()}
        key = str(surface)
        if key in clone.surfaces:
            clone.surfaces[key].connected = False
            clone.surfaces[key].ready = False
            clone.surfaces[key].metadata["disconnect_probe"] = True
        clone._record(
            ScaffoldMutationKind.DISCONNECT,
            surface,
            ScaffoldLifecyclePhase.FAILED,
            f"disconnect probe disabled {surface}",
            {"surface": str(surface)},
            connected=False,
            ready=False,
        )
        return clone

    def disconnect_probe(self, surface: RuntimeSurface) -> DisconnectProbe:
        baseline = self.snapshot()
        disconnected = self.disconnected(surface).snapshot()
        failing = [item for item in disconnected.invariants if item.surface == surface or item.severity == ScaffoldInvariantSeverity.BLOCKER]
        return DisconnectProbe(
            surface=surface,
            disabled=True,
            baseline_ok=baseline.ok,
            disconnected_ok=disconnected.ok,
            changed=baseline.to_dict() != disconnected.to_dict(),
            message=f"disconnecting {surface} changed scaffold health from {baseline.ok} to {disconnected.ok}",
            failing_invariants=failing,
        )

    def faulted(self, injection: SurfaceFaultInjection) -> "RuntimeScaffoldLifecycle":
        clone = self._clone()
        state = clone.surfaces[str(injection.surface)]
        if injection.fault_kind == SurfaceFaultKind.DISCONNECT:
            state.connected = False
            state.ready = False
        elif injection.fault_kind == SurfaceFaultKind.NOT_READY:
            state.ready = False
        elif injection.fault_kind == SurfaceFaultKind.CLEAR_STATE_REF:
            state.state_ref = ""
        elif injection.fault_kind == SurfaceFaultKind.DROP_EVENT_REF:
            state.last_event_id = ""
        state.metadata["fault_injection_id"] = injection.injection_id
        state.metadata["fault_kind"] = str(injection.fault_kind)
        event = clone._record(
            ScaffoldMutationKind.DISCONNECT,
            injection.surface,
            ScaffoldLifecyclePhase.FAILED,
            injection.message,
            {"fault_injection_id": injection.injection_id, "fault_kind": str(injection.fault_kind)},
            connected=state.connected,
            ready=state.ready,
        )
        if injection.fault_kind == SurfaceFaultKind.CLEAR_STATE_REF:
            clone.surfaces[str(injection.surface)].state_ref = ""
        elif injection.fault_kind == SurfaceFaultKind.DROP_EVENT_REF:
            clone.surfaces[str(injection.surface)].last_event_id = ""
        clone.surfaces[str(injection.surface)].metadata["last_fault_event_id"] = event.event_id
        return clone

    def fault_matrix(self, fault_kinds: Iterable[SurfaceFaultKind] | None = None) -> ScaffoldFaultMatrixReport:
        baseline = self.snapshot()
        kinds = tuple(fault_kinds or (SurfaceFaultKind.DISCONNECT, SurfaceFaultKind.NOT_READY, SurfaceFaultKind.CLEAR_STATE_REF, SurfaceFaultKind.DROP_EVENT_REF))
        results: list[SurfaceFaultResult] = []
        for surface in RuntimeSurface:
            for kind in kinds:
                injection = _surface_fault_injection(surface, kind)
                faulted = self.faulted(injection).snapshot()
                invariant_codes = tuple(sorted({item.code for item in faulted.invariants if item.surface == surface}))
                expected = set(injection.expected_invariant_codes)
                results.append(
                    SurfaceFaultResult(
                        injection=injection,
                        baseline_ok=baseline.ok,
                        faulted_ok=faulted.ok,
                        changed=baseline.to_dict() != faulted.to_dict(),
                        matched_expected_invariant=bool(expected & set(invariant_codes)),
                        invariant_codes=invariant_codes,
                        event_count_before=len(baseline.events),
                        event_count_after=len(faulted.events),
                    )
                )
        by_surface: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for result in results:
            by_surface[str(result.injection.surface)] = by_surface.get(str(result.injection.surface), 0) + 1
            by_kind[str(result.injection.fault_kind)] = by_kind.get(str(result.injection.fault_kind), 0) + 1
        failed = [result for result in results if not result.ok]
        return ScaffoldFaultMatrixReport(
            scaffold_id=self.scaffold.scaffold_id,
            owner_unit=self.scaffold.owner_unit,
            results=results,
            summary={
                "ok": not failed and bool(results),
                "result_count": len(results),
                "failed_result_count": len(failed),
                "surfaces": dict(sorted(by_surface.items())),
                "fault_kinds": dict(sorted(by_kind.items())),
            },
        )

    def invariants(self) -> list[ScaffoldInvariant]:
        findings: list[ScaffoldInvariant] = []
        _require(self.session is not None, findings, "SESSION_MISSING", RuntimeSurface.SESSION, "runtime session must be owned by Zyra scaffold")
        _require(self.tool_loop is not None, findings, "TOOL_LOOP_MISSING", RuntimeSurface.TOOL_LOOP, "tool loop state must be owned by Zyra scaffold")
        _require(bool(self.permission_decisions), findings, "PERMISSION_MISSING", RuntimeSurface.PERMISSION, "permission envelope must be present")
        _require(bool(self.mcp_servers), findings, "MCP_MISSING", RuntimeSurface.MCP, "MCP registry must be present")
        _require(bool(self.skills), findings, "SKILL_MISSING", RuntimeSurface.SKILL, "skill metadata must be present")
        _require(bool(self.subagents), findings, "SUBAGENT_MISSING", RuntimeSurface.SUBAGENT, "subagent contract must be present")
        _require(len(self.workers) >= 5, findings, "WORKERS_MISSING", RuntimeSurface.WORKER_BRIDGE, "five worker bridge contracts are required")
        for key, state in self.surfaces.items():
            surface = RuntimeSurface(key)
            if not state.connected:
                findings.append(
                    ScaffoldInvariant(
                        code="SURFACE_DISCONNECTED",
                        severity=ScaffoldInvariantSeverity.ERROR,
                        message=f"{surface} is disconnected from the scaffold lifecycle",
                        surface=surface,
                        remediation="Call the corresponding lifecycle mutation before completing the scaffold.",
                    )
                )
            if not state.ready:
                findings.append(
                    ScaffoldInvariant(
                        code="SURFACE_NOT_READY",
                        severity=ScaffoldInvariantSeverity.ERROR,
                        message=f"{surface} did not report ready state",
                        surface=surface,
                        remediation="Attach real health/smoke evidence for this runtime surface.",
                    )
                )
            if not state.state_ref:
                findings.append(
                    ScaffoldInvariant(
                        code="SURFACE_STATE_REF_MISSING",
                        severity=ScaffoldInvariantSeverity.ERROR,
                        message=f"{surface} has no state ownership reference",
                        surface=surface,
                        remediation="Persist or expose the Zyra-owned state reference for this runtime surface.",
                    )
                )
            if state.mutation_count > 0 and not state.last_event_id:
                findings.append(
                    ScaffoldInvariant(
                        code="SURFACE_EVENT_REF_MISSING",
                        severity=ScaffoldInvariantSeverity.ERROR,
                        message=f"{surface} has no latest event reference",
                        surface=surface,
                        remediation="Record event-log evidence when mutating scaffold surface state.",
                    )
                )
        if self.session and self.tool_loop and self.session.session_id != self.tool_loop.session_id:
            findings.append(
                ScaffoldInvariant(
                    code="SESSION_TOOL_LOOP_MISMATCH",
                    severity=ScaffoldInvariantSeverity.BLOCKER,
                    message="tool loop session_id does not match runtime session",
                    surface=RuntimeSurface.TOOL_LOOP,
                    remediation="Bind tool loop state to the active session id.",
                    metadata={"session_id": self.session.session_id, "tool_loop_session_id": self.tool_loop.session_id},
                )
            )
        worker_ids = [worker.worker_id for worker in self.workers]
        if len(worker_ids) != len(set(worker_ids)):
            findings.append(
                ScaffoldInvariant(
                    code="DUPLICATE_WORKER_ID",
                    severity=ScaffoldInvariantSeverity.ERROR,
                    message="worker bridge ids must be unique",
                    surface=RuntimeSurface.WORKER_BRIDGE,
                    remediation="Use stable unique worker ids.",
                )
            )
        return findings

    def _clone(self) -> "RuntimeScaffoldLifecycle":
        clone = RuntimeScaffoldLifecycle(self.scaffold, project_root=self.project_root)
        clone.session = self.session
        clone.tool_loop = self.tool_loop
        clone.permission_decisions = list(self.permission_decisions)
        clone.mcp_servers = list(self.mcp_servers)
        clone.skills = list(self.skills)
        clone.subagents = list(self.subagents)
        clone.workers = list(self.workers)
        clone.events = list(self.events)
        clone.phase = self.phase
        clone.created_at = self.created_at
        clone.updated_at = self.updated_at
        clone.surfaces = {key: RuntimeSurfaceState(**value.to_dict()) for key, value in self.surfaces.items()}
        return clone

    def snapshot(self) -> ScaffoldStateSnapshot:
        return ScaffoldStateSnapshot(
            scaffold_id=self.scaffold.scaffold_id,
            owner_unit=self.scaffold.owner_unit,
            phase=self.phase,
            session=self.session,
            tool_loop=self.tool_loop,
            permission_decisions=list(self.permission_decisions),
            mcp_servers=list(self.mcp_servers),
            skills=list(self.skills),
            subagents=list(self.subagents),
            workers=list(self.workers),
            surfaces=dict(self.surfaces),
            events=list(self.events),
            invariants=self.invariants(),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def event_records(self, *, run_id: str = "m1-01b", task_id: str = "runtime-scaffold") -> list[EventRecord]:
        records: list[EventRecord] = []
        for event in self.events:
            records.append(
                EventRecord(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=str(event.surface) if event.surface else "runtime-scaffold",
                    event_type=EventType.AGENT_MESSAGE,
                    payload={"runtime_scaffold_lifecycle": event.to_dict()},
                )
            )
        return records

    def write_journal(self, path: str | Path) -> Path:
        output = Path(path)
        if not output.is_absolute():
            output = self.project_root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            "\n".join(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) for event in self.events) + "\n",
            encoding="utf-8",
        )
        return output

    def _record(
        self,
        mutation: ScaffoldMutationKind,
        surface: RuntimeSurface | None,
        phase: ScaffoldLifecyclePhase,
        message: str,
        payload: Mapping[str, Any],
        *,
        connected: bool | None = None,
        ready: bool | None = None,
    ) -> ScaffoldLifecycleEvent:
        event = ScaffoldLifecycleEvent(
            event_id=new_id("scaffold"),
            phase=phase,
            mutation=mutation,
            surface=surface,
            message=message,
            payload=dict(payload),
        )
        self.events.append(event)
        self.phase = phase
        self.updated_at = event.created_at
        if surface is not None:
            self.surfaces[str(surface)].apply(event, connected=connected, ready=ready)
        return event


def build_default_lifecycle(project_root: str | Path | None = None, *, complete: bool = True) -> RuntimeScaffoldLifecycle:
    lifecycle = RuntimeScaffoldLifecycle.default(project_root).bootstrap()
    if complete:
        lifecycle.complete()
    return lifecycle


def scaffold_lifecycle_payload(project_root: str | Path | None = None) -> dict[str, Any]:
    lifecycle = build_default_lifecycle(project_root)
    probes = [lifecycle.disconnect_probe(surface) for surface in RuntimeSurface]
    fault_matrix = lifecycle.fault_matrix()
    snapshot = lifecycle.snapshot()
    return {
        "ok": snapshot.ok and all(probe.ok for probe in probes) and fault_matrix.ok,
        "snapshot": snapshot.to_dict(),
        "disconnect_probes": [probe.to_dict() for probe in probes],
        "fault_matrix": fault_matrix.to_dict(),
        "event_count": len(lifecycle.events),
    }


def run_scaffold_fault_matrix(project_root: str | Path | None = None) -> ScaffoldFaultMatrixReport:
    return build_default_lifecycle(project_root).fault_matrix()


def _surface_fault_injection(surface: RuntimeSurface, kind: SurfaceFaultKind) -> SurfaceFaultInjection:
    expected = {
        SurfaceFaultKind.DISCONNECT: ("SURFACE_DISCONNECTED", "SURFACE_NOT_READY"),
        SurfaceFaultKind.NOT_READY: ("SURFACE_NOT_READY",),
        SurfaceFaultKind.CLEAR_STATE_REF: ("SURFACE_STATE_REF_MISSING",),
        SurfaceFaultKind.DROP_EVENT_REF: ("SURFACE_EVENT_REF_MISSING",),
    }[kind]
    return SurfaceFaultInjection(
        injection_id=new_id("surface-fault"),
        surface=surface,
        fault_kind=kind,
        expected_invariant_codes=expected,
        message=f"fault injection {kind} for {surface}",
    )


def _require(condition: bool, findings: list[ScaffoldInvariant], code: str, surface: RuntimeSurface, message: str) -> None:
    if condition:
        return
    findings.append(
        ScaffoldInvariant(
            code=code,
            severity=ScaffoldInvariantSeverity.BLOCKER,
            message=message,
            surface=surface,
            remediation="Bootstrap the required Zyra-owned scaffold surface before declaring M1-01B complete.",
        )
    )
