from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_DISABLE_SEMANTICS_OWNER_UNIT = "M1-02D"
CODEWORKER_DISABLE_SEMANTICS_RUNTIME_ID = "codeworker_disable_semantics_runtime"


class DisableSemanticsStatus(StrEnum):
    READY = "ready"
    OBSERVED = "observed"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class DisableSemanticsSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class DisableSemanticsSurface(StrEnum):
    RUNTIME_BUDGET = "runtime_budget"
    COMPACT_RESTORE = "compact_restore"
    MODEL_STREAM = "model_stream"
    API_RETRY = "api_retry"
    RESTORE_INTEGRATION = "restore_integration"
    CONTEXT_SECURITY = "context_security"
    CODEWORKER_API = "codeworker_api"


class DisableSemanticsEffect(StrEnum):
    NOT_REQUESTED = "not_requested"
    EXPECTED_FAILURE = "expected_failure"
    EXPECTED_MISSING_BEHAVIOR = "expected_missing_behavior"
    UNEXPECTED_SUCCESS = "unexpected_success"
    UNEXPECTED_FAILURE = "unexpected_failure"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class DisableSemanticsFinding:
    code: str
    severity: DisableSemanticsSeverity
    surface: DisableSemanticsSurface
    message: str
    scenario_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == DisableSemanticsSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "scenario_id": self.scenario_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DisableSemanticsScenario:
    scenario_id: str
    constraint_name: str
    surface: DisableSemanticsSurface
    requested: bool
    expected_missing_keys: tuple[str, ...]
    expected_failed_keys: tuple[str, ...]
    expected_event_phases: tuple[str, ...]
    observed_effect: DisableSemanticsEffect
    metadata_values: dict[str, str] = field(default_factory=dict)
    phase_counts: dict[str, int] = field(default_factory=dict)
    findings: tuple[DisableSemanticsFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def observed(self) -> bool:
        return self.observed_effect in {
            DisableSemanticsEffect.EXPECTED_FAILURE,
            DisableSemanticsEffect.EXPECTED_MISSING_BEHAVIOR,
            DisableSemanticsEffect.OBSERVED,
            DisableSemanticsEffect.NOT_REQUESTED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "constraint_name": self.constraint_name,
            "surface": str(self.surface),
            "requested": self.requested,
            "expected_missing_keys": list(self.expected_missing_keys),
            "expected_failed_keys": list(self.expected_failed_keys),
            "expected_event_phases": list(self.expected_event_phases),
            "observed_effect": str(self.observed_effect),
            "metadata_values": dict(self.metadata_values),
            "phase_counts": dict(self.phase_counts),
            "ok": self.ok,
            "observed": self.observed,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class DisableSemanticsReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    scenarios: tuple[DisableSemanticsScenario, ...]
    findings: tuple[DisableSemanticsFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and all(scenario.ok for scenario in self.scenarios) and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def status(self) -> DisableSemanticsStatus:
        if self.disabled:
            return DisableSemanticsStatus.DISABLED
        if not self.ok:
            return DisableSemanticsStatus.BLOCKED
        if any(scenario.requested for scenario in self.scenarios):
            return DisableSemanticsStatus.OBSERVED
        if self.findings or any(scenario.findings for scenario in self.scenarios):
            return DisableSemanticsStatus.DEGRADED
        return DisableSemanticsStatus.READY

    @property
    def requested_count(self) -> int:
        return sum(1 for scenario in self.scenarios if scenario.requested)

    @property
    def observed_count(self) -> int:
        return sum(1 for scenario in self.scenarios if scenario.observed)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking) + sum(
            1 for scenario in self.scenarios for finding in scenario.findings if finding.blocking
        )

    def metadata(self) -> dict[str, str]:
        return {
            "disable_semantics_report_id": self.report_id,
            "disable_semantics_owner_unit": self.owner_unit,
            "disable_semantics_runtime_id": self.runtime_id,
            "disable_semantics_ok": str(self.ok).lower(),
            "disable_semantics_status": str(self.status),
            "disable_semantics_disabled": str(self.disabled).lower(),
            "disable_semantics_scenarios": str(len(self.scenarios)),
            "disable_semantics_requested": str(self.requested_count),
            "disable_semantics_observed": str(self.observed_count),
            "disable_semantics_blocking_count": str(self.blocking_count),
            "disable_semantics_findings": str(len(self.findings)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_disable_semantics.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "requested_count": self.requested_count,
            "observed_count": self.observed_count,
            "blocking_count": self.blocking_count,
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerDisableSemanticsRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_DISABLE_SEMANTICS_OWNER_UNIT,
        runtime_id: str = CODEWORKER_DISABLE_SEMANTICS_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        constraints: Mapping[str, Any],
        metadata: Mapping[str, Any],
        event_records: Sequence[Any],
        worker_ok: bool,
    ) -> DisableSemanticsReport:
        phase_counts = _phase_counts(event_records)
        scenarios = tuple(
            self._scenario(definition, constraints=constraints, metadata=metadata, phase_counts=phase_counts, worker_ok=worker_ok)
            for definition in _scenario_definitions()
        )
        findings = [finding for scenario in scenarios for finding in scenario.findings]
        if self.disabled:
            findings.append(
                DisableSemanticsFinding(
                    code="DISABLE_SEMANTICS_RUNTIME_DISABLED",
                    severity=DisableSemanticsSeverity.BLOCKER,
                    surface=DisableSemanticsSurface.CODEWORKER_API,
                    message="Disable semantics runtime is disabled.",
                )
            )
        return DisableSemanticsReport(
            report_id=new_id("disable_semantics"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            scenarios=scenarios,
            findings=tuple(findings),
            source_decisions=default_disable_semantics_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_report(
        self,
        report: DisableSemanticsReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_disable_semantics",
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": phase,
                    "disable_semantics": report.to_dict(),
                }
            },
        )

    def metadata(self, report: DisableSemanticsReport | None = None) -> dict[str, str]:
        if report is None:
            return {
                "disable_semantics_ok": str(not self.disabled).lower(),
                "disable_semantics_status": str(
                    DisableSemanticsStatus.DISABLED if self.disabled else DisableSemanticsStatus.READY
                ),
                "disable_semantics_runtime_id": self.runtime_id,
            }
        return report.metadata()

    def _scenario(
        self,
        definition: Mapping[str, Any],
        *,
        constraints: Mapping[str, Any],
        metadata: Mapping[str, Any],
        phase_counts: Mapping[str, int],
        worker_ok: bool,
    ) -> DisableSemanticsScenario:
        constraint_name = str(definition["constraint_name"])
        requested = _truthy(constraints.get(constraint_name))
        expected_failed_keys = tuple(str(item) for item in definition.get("expected_failed_keys", ()))
        expected_missing_keys = tuple(str(item) for item in definition.get("expected_missing_keys", ()))
        expected_event_phases = tuple(str(item) for item in definition.get("expected_event_phases", ()))
        metadata_values = {
            key: str(metadata.get(key) or "")
            for key in (*expected_failed_keys, *expected_missing_keys)
            if key
        }
        scenario_phase_counts = {phase: int(phase_counts.get(phase, 0)) for phase in expected_event_phases}
        findings: list[DisableSemanticsFinding] = []
        effect = DisableSemanticsEffect.NOT_REQUESTED
        if requested:
            failed_keys_observed = any(str(metadata.get(key) or "").lower() == "false" for key in expected_failed_keys)
            missing_behavior = any(int(phase_counts.get(phase, 0)) == 0 for phase in expected_event_phases)
            if failed_keys_observed or not worker_ok:
                effect = DisableSemanticsEffect.EXPECTED_FAILURE
            elif missing_behavior:
                effect = DisableSemanticsEffect.EXPECTED_MISSING_BEHAVIOR
            else:
                effect = DisableSemanticsEffect.UNEXPECTED_SUCCESS
                findings.append(
                    DisableSemanticsFinding(
                        code="DISABLE_SCENARIO_DID_NOT_CHANGE_BEHAVIOR",
                        severity=DisableSemanticsSeverity.BLOCKER,
                        surface=definition["surface"],
                        message="Runtime disable constraint was requested but expected failure or missing behavior was not observed.",
                        scenario_id=str(definition["scenario_id"]),
                        metadata={"constraint_name": constraint_name},
                    )
                )
        return DisableSemanticsScenario(
            scenario_id=str(definition["scenario_id"]),
            constraint_name=constraint_name,
            surface=definition["surface"],
            requested=requested,
            expected_missing_keys=expected_missing_keys,
            expected_failed_keys=expected_failed_keys,
            expected_event_phases=expected_event_phases,
            observed_effect=effect,
            metadata_values=metadata_values,
            phase_counts=scenario_phase_counts,
            findings=tuple(findings),
        )


def disable_semantics_metadata(report: DisableSemanticsReport | None) -> dict[str, str]:
    if report is None:
        return {
            "disable_semantics_ok": "false",
            "disable_semantics_status": "missing",
            "disable_semantics_report_id": "",
        }
    return report.metadata()


def default_disable_semantics_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_disable_semantics.py",
            "decision": "zyra_module_migrated",
            "capability": "compact restore disable changes next-turn restore behavior",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/claude.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_disable_semantics.py",
            "decision": "zyra_module_migrated",
            "capability": "model stream and API retry disable scenarios are checked as semantic behavior changes",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/**",
            "target_path": "packages/runtime/zyra_runtime/codeworker_disable_semantics.py",
            "decision": "adapter_encapsulated",
            "capability": "event phase presence is used as disable-effect evidence",
        },
    )


def _scenario_definitions() -> tuple[dict[str, Any], ...]:
    return (
        {
            "scenario_id": "disable_runtime_budget_state",
            "constraint_name": "disable_runtime_budget_state",
            "surface": DisableSemanticsSurface.RUNTIME_BUDGET,
            "expected_failed_keys": ("runtime_budget_state_ok", "codeworker_api_foundation_ok"),
            "expected_event_phases": ("runtime_budget_replay",),
        },
        {
            "scenario_id": "disable_compact_restore_runtime",
            "constraint_name": "disable_compact_restore_runtime",
            "surface": DisableSemanticsSurface.COMPACT_RESTORE,
            "expected_failed_keys": ("compact_restore_ok", "codeworker_api_foundation_ok"),
            "expected_event_phases": ("compact_restore_report", "next_turn_restore_contract"),
        },
        {
            "scenario_id": "disable_model_stream_runtime",
            "constraint_name": "disable_model_stream_runtime",
            "surface": DisableSemanticsSurface.MODEL_STREAM,
            "expected_failed_keys": ("model_stream_ok", "codeworker_api_foundation_ok"),
            "expected_event_phases": ("model_stream_report",),
        },
        {
            "scenario_id": "disable_api_retry_runtime",
            "constraint_name": "disable_api_retry_runtime",
            "surface": DisableSemanticsSurface.API_RETRY,
            "expected_failed_keys": ("api_retry_ok", "codeworker_api_foundation_ok"),
            "expected_event_phases": ("api_retry_report",),
        },
        {
            "scenario_id": "disable_restore_integration_runtime",
            "constraint_name": "disable_restore_integration_runtime",
            "surface": DisableSemanticsSurface.RESTORE_INTEGRATION,
            "expected_failed_keys": ("restore_integration_ok",),
            "expected_event_phases": ("codeworker_restore_context_applied",),
        },
        {
            "scenario_id": "disable_context_security_runtime",
            "constraint_name": "disable_context_security_runtime",
            "surface": DisableSemanticsSurface.CONTEXT_SECURITY,
            "expected_failed_keys": ("context_security_ok",),
            "expected_event_phases": ("codeworker_restore_context_security",),
        },
    )


def _phase_counts(event_records: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in event_records:
        event_map = _event_view(event)
        phase = str(_nested(event_map, "payload", "query_session").get("phase") or "")
        if phase:
            counts[phase] = counts.get(phase, 0) + 1
    return counts


def _event_view(event: Any) -> dict[str, Any]:
    if isinstance(event, EventRecord):
        return to_jsonable(event)
    if isinstance(event, Mapping):
        return dict(event)
    data = to_jsonable(event)
    return data if isinstance(data, dict) else {}


def _nested(value: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
