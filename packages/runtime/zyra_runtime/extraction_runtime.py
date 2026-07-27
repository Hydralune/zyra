from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .scaffold import RuntimeScaffold, RuntimeSurface, default_m1_01b_runtime_scaffold
from .scaffold_lifecycle import DisconnectProbe, RuntimeScaffoldLifecycle, build_default_lifecycle, scaffold_lifecycle_payload


class ExtractionRuntimePhase(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    CHECKING = "checking"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class RuntimeCheckKind(StrEnum):
    RULE_AUDIT = "rule_audit"
    LEDGER_ACCOUNTING = "ledger_accounting"
    LEDGER_REACHABILITY = "ledger_reachability"
    SOURCE_LINEAGE = "source_lineage"
    LIFECYCLE = "lifecycle"
    WORKER_BRIDGE = "worker_bridge"
    CLEAN_BOUNDARY = "clean_boundary"


class RuntimeFailureMode(StrEnum):
    NONE = "none"
    MISSING_TARGET = "missing_target"
    UNREACHABLE = "unreachable"
    RULE_BLOCKED = "rule_blocked"
    LIFECYCLE_DISCONNECTED = "lifecycle_disconnected"
    WORKER_FAILED = "worker_failed"
    EXTERNAL_DEPENDENCY = "external_dependency"


class RuntimeControlAction(StrEnum):
    START = "start"
    REFRESH_LEDGER = "refresh_ledger"
    DISCONNECT_SURFACE = "disconnect_surface"
    RECORD_CHECK = "record_check"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class RuntimeCheckResult:
    check_id: str
    kind: RuntimeCheckKind
    ok: bool
    message: str
    failure_mode: RuntimeFailureMode = RuntimeFailureMode.NONE
    started_at: str = field(default_factory=now_iso)
    finished_at: str = field(default_factory=now_iso)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class RuntimeControlMutation:
    mutation_id: str
    action: RuntimeControlAction
    phase_before: ExtractionRuntimePhase
    phase_after: ExtractionRuntimePhase
    ok: bool
    created_at: str = field(default_factory=now_iso)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class RuntimeExecutionPlan:
    plan_id: str
    owner_unit: str
    project_root: str
    source_workspace_root: str
    scaffold_id: str
    required_checks: list[RuntimeCheckKind]
    required_surfaces: list[RuntimeSurface]
    expected_workers: list[str]
    ledger_owner_unit: str = "M1-01B"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class RuntimeReadinessReport:
    plan: RuntimeExecutionPlan
    phase: ExtractionRuntimePhase
    checks: list[RuntimeCheckResult]
    mutations: list[RuntimeControlMutation]
    disconnect_probes: list[DisconnectProbe] = field(default_factory=list)
    events: list[EventRecord] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks) and self.phase == ExtractionRuntimePhase.READY

    @property
    def failed_checks(self) -> list[RuntimeCheckResult]:
        return [check for check in self.checks if not check.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "phase": str(self.phase),
            "plan": self.plan.to_dict(),
            "checks": [check.to_dict() for check in self.checks],
            "failed_checks": [check.to_dict() for check in self.failed_checks],
            "mutations": [mutation.to_dict() for mutation in self.mutations],
            "disconnect_probes": [probe.to_dict() for probe in self.disconnect_probes],
            "events": [to_jsonable(event) for event in self.events],
            "summary": dict(self.summary),
        }


class ExtractionRuntimeController:
    def __init__(
        self,
        project_root: str | Path,
        *,
        source_workspace_root: str | Path | None = None,
        scaffold: RuntimeScaffold | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.source_workspace_root = Path(source_workspace_root or self.project_root.parent).resolve()
        self.scaffold = scaffold or default_m1_01b_runtime_scaffold(self.project_root)
        self.phase = ExtractionRuntimePhase.CREATED
        self.mutations: list[RuntimeControlMutation] = []
        self.checks: list[RuntimeCheckResult] = []
        self.lifecycle: RuntimeScaffoldLifecycle | None = None

    def build_plan(self) -> RuntimeExecutionPlan:
        workers = [worker.worker_id for worker in self.scaffold.worker_bridges]
        return RuntimeExecutionPlan(
            plan_id=new_id("m1-01b-runtime-plan"),
            owner_unit=self.scaffold.owner_unit,
            project_root=str(self.project_root),
            source_workspace_root=str(self.source_workspace_root),
            scaffold_id=self.scaffold.scaffold_id,
            required_checks=[
                RuntimeCheckKind.RULE_AUDIT,
                RuntimeCheckKind.LEDGER_ACCOUNTING,
                RuntimeCheckKind.LEDGER_REACHABILITY,
                RuntimeCheckKind.SOURCE_LINEAGE,
                RuntimeCheckKind.LIFECYCLE,
                RuntimeCheckKind.WORKER_BRIDGE,
                RuntimeCheckKind.CLEAN_BOUNDARY,
            ],
            required_surfaces=list(RuntimeSurface),
            expected_workers=workers,
        )

    def run(self, *, include_worker_probes: bool = True) -> RuntimeReadinessReport:
        plan = self.build_plan()
        self._mutate(RuntimeControlAction.START, ExtractionRuntimePhase.PLANNED, {"plan_id": plan.plan_id})
        self._mutate(RuntimeControlAction.RECORD_CHECK, ExtractionRuntimePhase.CHECKING, {"checks": [str(item) for item in plan.required_checks]})
        check_builders: list[Callable[[], RuntimeCheckResult]] = [
            self._check_rule_audit,
            self._check_accounting,
            self._check_reachability,
            self._check_lineage,
            self._check_lifecycle,
            self._check_worker_bridge if include_worker_probes else self._check_worker_contracts,
            self._check_clean_boundary,
        ]
        for builder in check_builders:
            self.checks.append(builder())
        target_phase = ExtractionRuntimePhase.READY if all(check.ok for check in self.checks) else ExtractionRuntimePhase.FAILED
        self._mutate(RuntimeControlAction.COMPLETE, target_phase, {"failed_checks": [str(check.kind) for check in self.checks if not check.ok]})
        events = self.event_records(plan)
        probes = [self.lifecycle.disconnect_probe(surface) for surface in RuntimeSurface] if self.lifecycle else []
        return RuntimeReadinessReport(
            plan=plan,
            phase=self.phase,
            checks=list(self.checks),
            mutations=list(self.mutations),
            disconnect_probes=probes,
            events=events,
            summary=self._summary(plan, probes),
        )

    def disconnect_surface(self, surface: RuntimeSurface) -> RuntimeReadinessReport:
        if self.lifecycle is None:
            self.lifecycle = build_default_lifecycle(self.project_root, complete=True)
        probe = self.lifecycle.disconnect_probe(surface)
        self._mutate(
            RuntimeControlAction.DISCONNECT_SURFACE,
            ExtractionRuntimePhase.DEGRADED if probe.ok else ExtractionRuntimePhase.FAILED,
            {"surface": str(surface), "probe": probe.to_dict()},
        )
        check = RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.LIFECYCLE,
            ok=probe.ok,
            message=f"disconnect probe for {surface} changed runtime health",
            failure_mode=RuntimeFailureMode.NONE if probe.ok else RuntimeFailureMode.LIFECYCLE_DISCONNECTED,
            evidence=probe.to_dict(),
        )
        self.checks.append(check)
        plan = self.build_plan()
        return RuntimeReadinessReport(
            plan=plan,
            phase=self.phase,
            checks=list(self.checks),
            mutations=list(self.mutations),
            disconnect_probes=[probe],
            events=self.event_records(plan),
            summary=self._summary(plan, [probe]),
        )

    def event_records(self, plan: RuntimeExecutionPlan) -> list[EventRecord]:
        return [
            EventRecord(
                run_id="m1-01b",
                task_id="extraction-runtime-controller",
                node_id="runtime-controller",
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "extraction_runtime_controller": {
                        "phase": str(self.phase),
                        "plan_id": plan.plan_id,
                        "ok": all(check.ok for check in self.checks),
                        "check_count": len(self.checks),
                        "failed_checks": [str(check.kind) for check in self.checks if not check.ok],
                    }
                },
            )
        ]

    def _check_rule_audit(self) -> RuntimeCheckResult:
        from zyra_integrations.extraction_rules import audit_extraction_plan
        from zyra_integrations.source_extraction import claude_code_m1_01b_plan

        plan = claude_code_m1_01b_plan(project_root=self.project_root, source_workspace_root=self.source_workspace_root, dry_run=True)
        report = audit_extraction_plan(plan)
        summary = report.summary()
        return RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.RULE_AUDIT,
            ok=report.ok,
            message="extraction rule audit passed" if report.ok else "extraction rule audit failed",
            failure_mode=RuntimeFailureMode.NONE if report.ok else RuntimeFailureMode.RULE_BLOCKED,
            evidence=summary,
        )

    def _check_accounting(self) -> RuntimeCheckResult:
        from zyra_integrations.ledger_accounting import build_accounting_report
        from zyra_integrations.ledger_store import load_project_ledger

        ledger = load_project_ledger(self.project_root, bootstrap=True)
        report = build_accounting_report(self.project_root, ledger, owner_unit="M1-01B", include_entries=False)
        return RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.LEDGER_ACCOUNTING,
            ok=report.ok and report.total_entries > 0,
            message="ledger accounting has wired M1-01B entries",
            failure_mode=RuntimeFailureMode.NONE if report.ok else RuntimeFailureMode.MISSING_TARGET,
            evidence=report.summary(),
        )

    def _check_reachability(self) -> RuntimeCheckResult:
        from zyra_integrations.ledger_reachability import build_reachability_report
        from zyra_integrations.ledger_store import load_project_ledger

        ledger = load_project_ledger(self.project_root, bootstrap=True)
        report = build_reachability_report(self.project_root, ledger, owner_unit="M1-01B", include_entries=False, strict_audit=False)
        return RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.LEDGER_REACHABILITY,
            ok=report.ok and report.unreachable_entries == 0,
            message="ledger reachability found runtime, CLI, event, target, and test surfaces",
            failure_mode=RuntimeFailureMode.NONE if report.ok else RuntimeFailureMode.UNREACHABLE,
            evidence={
                "reachable_entries": report.reachable_entries,
                "unreachable_entries": report.unreachable_entries,
                "cli_command_count": report.cli_command_count,
                "event_type_count": report.event_type_count,
            },
        )

    def _check_lineage(self) -> RuntimeCheckResult:
        from zyra_integrations.extraction_lineage import build_m1_01b_lineage_report

        report = build_m1_01b_lineage_report(self.project_root, source_workspace_root=self.source_workspace_root)
        return RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.SOURCE_LINEAGE,
            ok=report.ok,
            message="source-to-target lineage is connected",
            failure_mode=RuntimeFailureMode.NONE if report.ok else RuntimeFailureMode.MISSING_TARGET,
            evidence=report.summary,
        )

    def _check_lifecycle(self) -> RuntimeCheckResult:
        self.lifecycle = build_default_lifecycle(self.project_root, complete=True)
        payload = scaffold_lifecycle_payload(self.project_root)
        return RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.LIFECYCLE,
            ok=bool(payload.get("ok")),
            message="runtime scaffold lifecycle is healthy and disconnect probes fail as expected",
            failure_mode=RuntimeFailureMode.NONE if payload.get("ok") else RuntimeFailureMode.LIFECYCLE_DISCONNECTED,
            evidence={"event_count": payload.get("event_count"), "disconnect_probe_count": len(payload.get("disconnect_probes", []))},
        )

    def _check_worker_contracts(self) -> RuntimeCheckResult:
        workers = self.scaffold.worker_bridges
        ok = len(workers) >= 5 and all(worker.worker_id and worker.capabilities for worker in workers)
        return RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.WORKER_BRIDGE,
            ok=ok,
            message="worker bridge contracts are declared",
            failure_mode=RuntimeFailureMode.NONE if ok else RuntimeFailureMode.WORKER_FAILED,
            evidence={"worker_count": len(workers), "workers": [worker.worker_id for worker in workers]},
        )

    def _check_worker_bridge(self) -> RuntimeCheckResult:
        from zyra_workers.scaffold_supervisor import run_supervised_scaffold_workers

        report = run_supervised_scaffold_workers(self.project_root, max_attempts=1)
        return RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.WORKER_BRIDGE,
            ok=report.ok,
            message="worker bridge supervisor executed all worker probes",
            failure_mode=RuntimeFailureMode.NONE if report.ok else RuntimeFailureMode.WORKER_FAILED,
            evidence=report.summary,
        )

    def _check_clean_boundary(self) -> RuntimeCheckResult:
        from zyra_integrations.ledger_audit import (
            _forbidden_fragments,
            _python_runtime_dependency_fragments,
        )

        forbidden = set(_forbidden_fragments())
        forbidden.update(fragment.replace("\\", "/") for fragment in list(forbidden))
        scanned = []
        violations = []
        for root_name in ["packages", "apps", "scripts"]:
            root = self.project_root / root_name
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".mjs", ".ts", ".tsx", ".json"}:
                    continue
                relative = path.relative_to(self.project_root).as_posix()
                if relative.startswith("scripts/remediation/"):
                    continue
                scanned.append(path)
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                fragments = (
                    _python_runtime_dependency_fragments(text, list(forbidden))
                    if path.suffix.lower() == ".py"
                    else [fragment for fragment in forbidden if fragment in text]
                )
                if fragments:
                    violations.append(relative)
        ok = not violations
        return RuntimeCheckResult(
            check_id=new_id("runtime-check"),
            kind=RuntimeCheckKind.CLEAN_BOUNDARY,
            ok=ok,
            message="runtime code does not depend on parent source repositories",
            failure_mode=RuntimeFailureMode.NONE if ok else RuntimeFailureMode.EXTERNAL_DEPENDENCY,
            evidence={"scanned_files": len(scanned), "violations": violations[:20]},
        )

    def _mutate(self, action: RuntimeControlAction, next_phase: ExtractionRuntimePhase, payload: dict[str, Any]) -> None:
        before = self.phase
        self.phase = next_phase
        self.mutations.append(
            RuntimeControlMutation(
                mutation_id=new_id("runtime-mutation"),
                action=action,
                phase_before=before,
                phase_after=next_phase,
                ok=next_phase != ExtractionRuntimePhase.FAILED,
                payload=payload,
            )
        )

    def _summary(self, plan: RuntimeExecutionPlan, probes: Iterable[DisconnectProbe]) -> dict[str, Any]:
        failed = [check for check in self.checks if not check.ok]
        return {
            "ok": not failed and self.phase == ExtractionRuntimePhase.READY,
            "phase": str(self.phase),
            "plan_id": plan.plan_id,
            "check_count": len(self.checks),
            "failed_check_count": len(failed),
            "failed_checks": [str(check.kind) for check in failed],
            "mutation_count": len(self.mutations),
            "disconnect_probe_count": len(list(probes)),
            "required_surface_count": len(plan.required_surfaces),
            "expected_worker_count": len(plan.expected_workers),
        }


def run_extraction_runtime_controller(
    project_root: str | Path,
    *,
    source_workspace_root: str | Path | None = None,
    include_worker_probes: bool = True,
) -> RuntimeReadinessReport:
    return ExtractionRuntimeController(project_root, source_workspace_root=source_workspace_root).run(include_worker_probes=include_worker_probes)


def extraction_runtime_payload(report: RuntimeReadinessReport) -> dict[str, Any]:
    return report.to_dict()


def assert_extraction_runtime_ready(report: RuntimeReadinessReport) -> None:
    if report.ok:
        return
    failed = "\n".join(f"- {check.kind}: {check.message}" for check in report.failed_checks)
    raise AssertionError(f"M1-01B extraction runtime controller failed:\n{failed}")
