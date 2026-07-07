from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable
from zyra_runtime import WorkerBridgeContract
from zyra_runtime.scaffold_lifecycle import RuntimeScaffoldLifecycle, build_default_lifecycle

from .scaffold_bridge_runtime import BridgeProbeResult, build_bridge_runtimes


class SupervisorPhase(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkerProbeRisk(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class RetryAction(StrEnum):
    NONE = "none"
    RETRY = "retry"
    FAIL_FAST = "fail_fast"


class WorkerContractCriterion(StrEnum):
    WORKER_ID = "worker_id"
    RUNTIME_KIND = "runtime_kind"
    CAPABILITIES = "capabilities"
    ENTRYPOINT = "entrypoint"
    HEALTH_PROFILE = "health_profile"
    SMOKE_PROFILE = "smoke_profile"
    RESOURCE_PROFILE = "resource_profile"
    EVENT_OUTPUT = "event_output"
    ARTIFACT_OUTPUT = "artifact_output"
    PERMISSION_SCOPE = "permission_scope"


@dataclass(frozen=True, slots=True)
class WorkerProbeAttempt:
    attempt_id: str
    worker_id: str
    worker_kind: str
    attempt_index: int
    ok: bool
    started_at: str
    finished_at: str
    result: dict[str, Any]
    retry_action: RetryAction = RetryAction.NONE

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class SupervisorFinding:
    code: str
    risk: WorkerProbeRisk
    message: str
    worker_id: str = ""
    worker_kind: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class WorkerContractCriterionResult:
    worker_id: str
    worker_kind: str
    criterion: WorkerContractCriterion
    ok: bool
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class WorkerContractRow:
    worker_id: str
    worker_kind: str
    runtime_kind: str
    criteria: tuple[WorkerContractCriterionResult, ...]
    capabilities: tuple[str, ...]
    tools: tuple[str, ...]
    entrypoint: str
    latest_attempt_ok: bool | None = None

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.criteria) and self.latest_attempt_ok is not False

    @property
    def failed_criteria(self) -> list[WorkerContractCriterionResult]:
        return [item for item in self.criteria if not item.ok]

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class WorkerContractMatrixReport:
    project_root: Path
    rows: list[WorkerContractRow]
    findings: list[SupervisorFinding]
    summary: dict[str, Any]

    @property
    def ok(self) -> bool:
        return bool(self.rows) and all(row.ok for row in self.rows) and not any(
            finding.risk in {WorkerProbeRisk.ERROR, WorkerProbeRisk.BLOCKER}
            for finding in self.findings
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_root": str(self.project_root),
            "rows": [row.to_dict() for row in self.rows],
            "findings": [finding.to_dict() for finding in self.findings],
            "summary": dict(self.summary),
        }


@dataclass(slots=True)
class WorkerSupervisorReport:
    project_root: Path
    phase: SupervisorPhase
    attempts: list[WorkerProbeAttempt]
    findings: list[SupervisorFinding]
    events: list[EventRecord]
    summary: dict[str, Any]
    contract_matrix: WorkerContractMatrixReport | None = None

    @property
    def ok(self) -> bool:
        return self.phase == SupervisorPhase.COMPLETED and not any(
            finding.risk in {WorkerProbeRisk.ERROR, WorkerProbeRisk.BLOCKER}
            for finding in self.findings
        )

    @property
    def failed_attempts(self) -> list[WorkerProbeAttempt]:
        return [attempt for attempt in self.attempts if not attempt.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_root": str(self.project_root),
            "phase": str(self.phase),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "findings": [finding.to_dict() for finding in self.findings],
            "events": [to_jsonable(event) for event in self.events],
            "summary": dict(self.summary),
            "contract_matrix": self.contract_matrix.to_dict() if self.contract_matrix else None,
        }


class WorkerBridgeSupervisor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        lifecycle: RuntimeScaffoldLifecycle | None = None,
        max_attempts: int = 2,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.lifecycle = lifecycle or build_default_lifecycle(self.project_root, complete=False)
        self.max_attempts = max(1, max_attempts)
        self.phase = SupervisorPhase.CREATED
        self.attempts: list[WorkerProbeAttempt] = []
        self.findings: list[SupervisorFinding] = []
        self.events: list[EventRecord] = []

    def run(self) -> WorkerSupervisorReport:
        self.phase = SupervisorPhase.RUNNING
        runtimes = build_bridge_runtimes(project_root=self.project_root, lifecycle=self.lifecycle)
        for runtime in runtimes:
            final_result: BridgeProbeResult | None = None
            for attempt_index in range(1, self.max_attempts + 1):
                started = now_iso()
                result = runtime.probe()
                final_result = result
                finished = now_iso()
                retry = self._retry_action(result, attempt_index)
                self.attempts.append(
                    WorkerProbeAttempt(
                        attempt_id=new_id("worker-probe"),
                        worker_id=result.worker_id,
                        worker_kind=result.worker_kind,
                        attempt_index=attempt_index,
                        ok=result.ok,
                        started_at=started,
                        finished_at=finished,
                        result=result.to_dict(),
                        retry_action=retry,
                    )
                )
                self.events.append(runtime.event(result))
                if retry != RetryAction.RETRY:
                    break
                self.phase = SupervisorPhase.RETRYING
            if final_result is not None:
                self.findings.extend(self._findings_for_result(final_result))
        all_latest_ok = all(attempt.ok for attempt in self._latest_attempts())
        self.lifecycle.complete(ok=all_latest_ok)
        self.events.extend(self.lifecycle.event_records(run_id="m1-01b", task_id="worker-supervisor"))
        contract_matrix = build_worker_contract_matrix(
            self.project_root,
            contracts=self.lifecycle.workers,
            attempts=self.attempts,
            events=self.events,
        )
        self.findings.extend(contract_matrix.findings)
        self.phase = SupervisorPhase.COMPLETED if all_latest_ok and not self._has_blocking_findings() else SupervisorPhase.FAILED
        return WorkerSupervisorReport(
            project_root=self.project_root,
            phase=self.phase,
            attempts=list(self.attempts),
            findings=sorted(self.findings, key=lambda item: (str(item.risk), item.worker_kind, item.code)),
            events=list(self.events),
            summary=self._summary(),
            contract_matrix=contract_matrix,
        )

    def assert_disconnect_changes_health(self) -> list[SupervisorFinding]:
        findings: list[SupervisorFinding] = []
        baseline = self.lifecycle.snapshot()
        for surface in baseline.surfaces:
            try:
                from zyra_runtime import RuntimeSurface

                probe = self.lifecycle.disconnect_probe(RuntimeSurface(surface))
            except Exception as error:
                findings.append(
                    SupervisorFinding(
                        code="DISCONNECT_PROBE_ERROR",
                        risk=WorkerProbeRisk.ERROR,
                        message=f"disconnect probe failed for {surface}: {error}",
                        remediation="Keep lifecycle surface names aligned with RuntimeSurface.",
                    )
                )
                continue
            if not probe.ok:
                findings.append(
                    SupervisorFinding(
                        code="DISCONNECT_DID_NOT_FAIL",
                        risk=WorkerProbeRisk.ERROR,
                        message=f"disconnecting {surface} did not change runtime health",
                        remediation="Make the surface part of lifecycle invariants before claiming reachability.",
                        metadata=probe.to_dict(),
                    )
                )
        self.findings.extend(findings)
        return findings

    def event_records(self) -> list[EventRecord]:
        return list(self.events)

    def _retry_action(self, result: BridgeProbeResult, attempt_index: int) -> RetryAction:
        if result.ok:
            return RetryAction.NONE
        if _has_permission_failure(result):
            return RetryAction.FAIL_FAST
        if attempt_index < self.max_attempts:
            return RetryAction.RETRY
        return RetryAction.FAIL_FAST

    def _findings_for_result(self, result: BridgeProbeResult) -> list[SupervisorFinding]:
        findings: list[SupervisorFinding] = []
        failed_steps = [step for step in result.steps if not step.ok]
        warning_steps = [step for step in result.steps if str(step.status) == "warning"]
        if failed_steps:
            findings.append(
                SupervisorFinding(
                    code="WORKER_PROBE_FAILED",
                    risk=WorkerProbeRisk.ERROR,
                    message=f"{result.worker_kind} worker probe has failing steps.",
                    worker_id=result.worker_id,
                    worker_kind=result.worker_kind,
                    remediation="Fix the worker bridge behavior or downgrade its runtime contract.",
                    metadata={"failed_steps": [step.to_dict() for step in failed_steps]},
                )
            )
        if warning_steps:
            findings.append(
                SupervisorFinding(
                    code="WORKER_PROBE_WARNING",
                    risk=WorkerProbeRisk.WARNING,
                    message=f"{result.worker_kind} worker probe has warning-only steps.",
                    worker_id=result.worker_id,
                    worker_kind=result.worker_kind,
                    remediation="Replace optional or missing entrypoints with materialized runtime code when the unit owning it lands.",
                    metadata={"warning_steps": [step.to_dict() for step in warning_steps]},
                )
            )
        if not result.artifacts:
            findings.append(
                SupervisorFinding(
                    code="WORKER_PROBE_ARTIFACT_MISSING",
                    risk=WorkerProbeRisk.ERROR,
                    message=f"{result.worker_kind} worker probe did not write an artifact.",
                    worker_id=result.worker_id,
                    worker_kind=result.worker_kind,
                    remediation="Write structured probe evidence through LocalArtifactStore.",
                )
            )
        return findings

    def _latest_attempts(self) -> list[WorkerProbeAttempt]:
        latest: dict[str, WorkerProbeAttempt] = {}
        for attempt in self.attempts:
            latest[attempt.worker_id] = attempt
        return list(latest.values())

    def _has_blocking_findings(self) -> bool:
        return any(finding.risk in {WorkerProbeRisk.ERROR, WorkerProbeRisk.BLOCKER} for finding in self.findings)

    def _summary(self) -> dict[str, Any]:
        latest = self._latest_attempts()
        by_kind = Counter(attempt.worker_kind for attempt in latest)
        failures_by_kind = Counter(attempt.worker_kind for attempt in latest if not attempt.ok)
        attempts_by_kind = Counter(attempt.worker_kind for attempt in self.attempts)
        return {
            "ok": self.phase == SupervisorPhase.COMPLETED and not self._has_blocking_findings(),
            "phase": str(self.phase),
            "worker_count": len(latest),
            "attempt_count": len(self.attempts),
            "failed_worker_count": sum(1 for attempt in latest if not attempt.ok),
            "finding_count": len(self.findings),
            "blocking_findings": sum(1 for finding in self.findings if finding.risk in {WorkerProbeRisk.ERROR, WorkerProbeRisk.BLOCKER}),
            "workers_by_kind": dict(sorted(by_kind.items())),
            "failures_by_kind": dict(sorted(failures_by_kind.items())),
            "attempts_by_kind": dict(sorted(attempts_by_kind.items())),
        }


def run_supervised_scaffold_workers(
    project_root: str | Path,
    *,
    max_attempts: int = 2,
) -> WorkerSupervisorReport:
    return WorkerBridgeSupervisor(project_root, max_attempts=max_attempts).run()


def supervised_worker_payload(report: WorkerSupervisorReport) -> dict[str, Any]:
    return report.to_dict()


def assert_supervised_workers_ok(report: WorkerSupervisorReport) -> None:
    if report.ok:
        return
    blocking = "\n".join(
        f"- {finding.risk} {finding.worker_id} {finding.code}: {finding.message}"
        for finding in report.findings
        if finding.risk in {WorkerProbeRisk.ERROR, WorkerProbeRisk.BLOCKER}
    )
    raise AssertionError(f"Worker bridge supervisor failed:\n{blocking}")


def supervisor_event_records(report: WorkerSupervisorReport) -> list[EventRecord]:
    return [
        *report.events,
        EventRecord(
            run_id="m1-01b",
            task_id="worker-supervisor",
            node_id="worker-supervisor",
            event_type=EventType.AGENT_MESSAGE,
            payload={"worker_bridge_supervisor": report.summary},
        ),
    ]


def build_worker_contract_matrix(
    project_root: str | Path,
    *,
    contracts: Iterable[WorkerBridgeContract] | None = None,
    attempts: Iterable[WorkerProbeAttempt] | None = None,
    events: Iterable[EventRecord] | None = None,
) -> WorkerContractMatrixReport:
    root = Path(project_root).resolve()
    lifecycle = build_default_lifecycle(root, complete=False)
    active_contracts = list(contracts or lifecycle.workers)
    attempt_list = list(attempts or [])
    event_list = list(events or [])
    latest_attempts: dict[str, WorkerProbeAttempt] = {}
    for attempt in attempt_list:
        latest_attempts[attempt.worker_id] = attempt
    events_by_node = Counter(str(event.node_id) for event in event_list)
    rows: list[WorkerContractRow] = []
    findings: list[SupervisorFinding] = []
    for contract in active_contracts:
        latest = latest_attempts.get(contract.worker_id)
        criteria = tuple(
            _worker_contract_criteria(
                root,
                contract,
                latest_attempt=latest,
                event_count=events_by_node.get(contract.worker_id, 0),
            )
        )
        row = WorkerContractRow(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            runtime_kind=contract.runtime_kind,
            criteria=criteria,
            capabilities=tuple(contract.capabilities),
            tools=tuple(contract.tools),
            entrypoint=contract.entrypoint,
            latest_attempt_ok=None if latest is None else latest.ok,
        )
        rows.append(row)
        for failed in row.failed_criteria:
            findings.append(
                SupervisorFinding(
                    code=f"CONTRACT_{str(failed.criterion).upper()}_FAILED",
                    risk=WorkerProbeRisk.ERROR,
                    message=failed.message,
                    worker_id=contract.worker_id,
                    worker_kind=contract.worker_kind,
                    remediation="Materialize this worker bridge field or prove it through the scaffold probe.",
                    metadata=failed.evidence,
                )
            )
    duplicate_ids = [worker_id for worker_id, count in Counter(row.worker_id for row in rows).items() if count > 1]
    if duplicate_ids:
        findings.append(
            SupervisorFinding(
                code="CONTRACT_DUPLICATE_WORKER_ID",
                risk=WorkerProbeRisk.BLOCKER,
                message="worker contract matrix contains duplicate worker ids",
                remediation="Use stable unique worker bridge identifiers.",
                metadata={"duplicate_worker_ids": duplicate_ids},
            )
        )
    required_kinds = {"code", "browser", "sandbox", "memory", "scheduler"}
    present_kinds = {row.worker_kind for row in rows}
    missing_kinds = sorted(required_kinds - present_kinds)
    if missing_kinds:
        findings.append(
            SupervisorFinding(
                code="CONTRACT_REQUIRED_WORKER_KIND_MISSING",
                risk=WorkerProbeRisk.ERROR,
                message="worker contract matrix is missing required worker kinds",
                remediation="Declare Code, Browser, Sandbox, Memory, and Scheduler worker bridge contracts.",
                metadata={"missing_worker_kinds": missing_kinds},
            )
        )
    criteria_counts = Counter(str(result.criterion) for row in rows for result in row.criteria)
    failed_criteria_counts = Counter(str(result.criterion) for row in rows for result in row.criteria if not result.ok)
    summary = {
        "ok": bool(rows) and not any(finding.risk in {WorkerProbeRisk.ERROR, WorkerProbeRisk.BLOCKER} for finding in findings),
        "worker_count": len(rows),
        "finding_count": len(findings),
        "blocking_findings": sum(1 for finding in findings if finding.risk in {WorkerProbeRisk.ERROR, WorkerProbeRisk.BLOCKER}),
        "worker_kinds": dict(sorted(Counter(row.worker_kind for row in rows).items())),
        "criteria_counts": dict(sorted(criteria_counts.items())),
        "failed_criteria_counts": dict(sorted(failed_criteria_counts.items())),
    }
    return WorkerContractMatrixReport(project_root=root, rows=rows, findings=findings, summary=summary)


def _worker_contract_criteria(
    project_root: Path,
    contract: WorkerBridgeContract,
    *,
    latest_attempt: WorkerProbeAttempt | None,
    event_count: int,
) -> list[WorkerContractCriterionResult]:
    entrypoint = _resolve_entrypoint(project_root, contract.entrypoint)
    artifact_count = 0
    if latest_attempt:
        artifact_count = len(latest_attempt.result.get("artifacts") or [])
    permission_tools = {"shell", "file_read", "file_write"}
    return [
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.WORKER_ID,
            ok=bool(contract.worker_id and contract.worker_id.endswith("-worker-pilot")),
            message="worker id is stable and namespaced",
            evidence={"worker_id": contract.worker_id},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.RUNTIME_KIND,
            ok=bool(contract.runtime_kind),
            message="runtime kind is declared",
            evidence={"runtime_kind": contract.runtime_kind},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.CAPABILITIES,
            ok=len(contract.capabilities) >= 2,
            message="worker declares at least two capabilities",
            evidence={"capabilities": list(contract.capabilities)},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.ENTRYPOINT,
            ok=bool(contract.entrypoint) and (entrypoint.exists() or ":" in contract.entrypoint or contract.worker_kind in {"browser", "memory", "scheduler"}),
            message="entrypoint is materialized or explicitly delegated to owning later unit",
            evidence={"entrypoint": contract.entrypoint, "resolved": str(entrypoint), "exists": entrypoint.exists()},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.HEALTH_PROFILE,
            ok=bool(contract.health_profile),
            message="health profile is declared",
            evidence={"health_profile": dict(contract.health_profile)},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.SMOKE_PROFILE,
            ok=bool(contract.smoke_profile),
            message="smoke profile is declared",
            evidence={"smoke_profile": dict(contract.smoke_profile)},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.RESOURCE_PROFILE,
            ok=contract.resource_profile.get("location") == "local" and contract.resource_profile.get("privacy") == "workspace",
            message="resource profile stays in local workspace custody",
            evidence={"resource_profile": dict(contract.resource_profile)},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.EVENT_OUTPUT,
            ok=event_count > 0 if latest_attempt is not None else True,
            message="worker probe emitted an event when attempts were provided",
            evidence={"event_count": event_count, "attempt_seen": latest_attempt is not None},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.ARTIFACT_OUTPUT,
            ok=artifact_count > 0 if latest_attempt is not None else True,
            message="worker probe wrote structured artifact evidence when attempts were provided",
            evidence={"artifact_count": artifact_count, "attempt_seen": latest_attempt is not None},
        ),
        WorkerContractCriterionResult(
            worker_id=contract.worker_id,
            worker_kind=contract.worker_kind,
            criterion=WorkerContractCriterion.PERMISSION_SCOPE,
            ok=bool(set(contract.tools) & permission_tools) if contract.worker_kind in {"code", "sandbox"} else bool(contract.tools),
            message="worker exposes a tool or permission scope relevant to its kind",
            evidence={"tools": list(contract.tools)},
        ),
    ]


def _resolve_entrypoint(project_root: Path, entrypoint: str) -> Path:
    raw = entrypoint.split(":", 1)[0]
    path = Path(raw)
    if not raw:
        return Path()
    if path.is_absolute():
        return path
    return project_root / path


def _has_permission_failure(result: BridgeProbeResult) -> bool:
    for step in result.steps:
        text = f"{step.name} {step.message} {step.payload}".lower()
        if "permission_denied" in text or "permission deny" in text:
            return True
    return False
