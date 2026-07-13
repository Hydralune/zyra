from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from .canonical_ports import BrowserCanonicalPorts


class BrowserIntegrationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class BrowserIntegrationStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class BrowserIntegrationFinding:
    check_id: str
    ok: bool
    severity: BrowserIntegrationSeverity
    summary: str
    owner: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "ok": self.ok,
            "severity": str(self.severity),
            "summary": self.summary,
            "owner": self.owner,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class BrowserIntegrationAuditReport:
    status: BrowserIntegrationStatus
    findings: tuple[BrowserIntegrationFinding, ...]
    runtime_id: str = "zyra-browser-session-integration-audit"
    owner_unit: str = "M1-S04A-02"

    @property
    def ready(self) -> bool:
        return self.status == BrowserIntegrationStatus.READY

    @property
    def blocking_findings(self) -> tuple[BrowserIntegrationFinding, ...]:
        return tuple(
            item
            for item in self.findings
            if not item.ok and item.severity == BrowserIntegrationSeverity.ERROR
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "owner_unit": self.owner_unit,
            "status": str(self.status),
            "ready": self.ready,
            "findings": [item.to_dict() for item in self.findings],
            "blocking_check_ids": [item.check_id for item in self.blocking_findings],
        }


class BrowserSessionIntegrationAudit:
    """Read-only wiring audit for the productized browser application path."""

    def __init__(
        self,
        browser_runtime: Any,
        *,
        application: Any = None,
        control_runtime: Any = None,
        canonical_ports: BrowserCanonicalPorts | None = None,
    ) -> None:
        self.browser_runtime = browser_runtime
        self.application = application
        self.control_runtime = control_runtime
        self.canonical_ports = canonical_ports

    def inspect(self) -> BrowserIntegrationAuditReport:
        findings: list[BrowserIntegrationFinding] = []
        runtime = self.browser_runtime
        session_owner = getattr(runtime, "_runtime", None) if runtime is not None else None
        findings.append(self._finding(
            "browser_runtime_injected",
            runtime is not None,
            BrowserIntegrationSeverity.ERROR,
            "BrowserRuntime is injected rather than constructed by the application layer.",
            "M1-S04A-02",
            runtime_type=type(runtime).__name__ if runtime is not None else "missing",
        ))
        findings.append(self._finding(
            "session_runtime_owner",
            session_owner is not None,
            BrowserIntegrationSeverity.ERROR,
            "BrowserRuntime exposes one BrowserSessionRuntime owner.",
            "M1-S04A-01",
            owner_type=type(session_owner).__name__ if session_owner is not None else "missing",
        ))
        for check_id, attribute, summary in (
            ("worker_session_bridge", "worker_bridge", "Worker requests bind to the productized browser session owner."),
            ("artifact_event_bridge", "artifact_bridge", "Browser artifacts and lifecycle events use the Zyra bridge."),
            ("permission_port", "permission_port", "Browser lifecycle/action handoff has an injected permission owner."),
            ("target_runtime_registry", "_targets", "Target/focus state remains in the session runtime registry."),
            ("cdp_runtime_registry", "_cdp", "CDP transports remain in the session runtime registry."),
            ("event_bus_registry", "_event_buses", "Transient browser buses remain in the session runtime registry."),
        ):
            findings.append(self._finding(
                check_id,
                session_owner is not None and getattr(session_owner, attribute, None) is not None,
                BrowserIntegrationSeverity.ERROR,
                summary,
                "M1-S04A-01",
                attribute=attribute,
            ))

        executor = self._action_executor(runtime, session_owner)
        findings.append(self._finding(
            "session_bound_action_executor",
            executor is not None,
            BrowserIntegrationSeverity.ERROR,
            "A session-bound action executor is reachable from the existing runtime.",
            "M1-S04A-02",
            executor_type=type(executor).__name__ if executor is not None else "missing",
        ))
        owner_match, owner_evidence = self._executor_owner_match(executor, runtime, session_owner)
        findings.append(self._finding(
            "single_runtime_identity",
            owner_match,
            BrowserIntegrationSeverity.ERROR,
            "Action execution does not bind a second BrowserRuntime or BrowserSessionRuntime.",
            "M1-S04A-02",
            **owner_evidence,
        ))

        if self.application is not None:
            findings.append(self._finding(
                "application_runtime_identity",
                getattr(self.application, "browser_runtime", None) is runtime,
                BrowserIntegrationSeverity.ERROR,
                "BrowserSessionApplication uses the audited BrowserRuntime instance.",
                "M1-S04A-02",
            ))
            shared_store = getattr(self.application, "receipt_store", None)
            control_store = getattr(getattr(self.control_runtime, "transactions", None), "lease_store", None)
            findings.append(self._finding(
                "shared_integration_state_owner",
                shared_store is not None and shared_store is control_store,
                BrowserIntegrationSeverity.ERROR,
                "Application and lifecycle control share one lease/receipt owner.",
                "M1-S04A-02",
                application_store=type(shared_store).__name__ if shared_store is not None else "missing",
                control_store=type(control_store).__name__ if control_store is not None else "missing",
            ))
        else:
            findings.append(self._finding(
                "application_runtime_identity",
                False,
                BrowserIntegrationSeverity.WARNING,
                "BrowserSessionApplication was not supplied to the audit.",
                "M1-S04A-02",
            ))

        if self.control_runtime is not None:
            findings.append(self._finding(
                "control_runtime_identity",
                getattr(self.control_runtime, "browser_runtime", None) is runtime,
                BrowserIntegrationSeverity.ERROR,
                "Control commands use the audited BrowserRuntime instance.",
                "M1-S04A-02",
            ))
        else:
            findings.append(self._finding(
                "control_runtime_identity",
                False,
                BrowserIntegrationSeverity.WARNING,
                "BrowserSessionControlRuntime was not supplied to the audit.",
                "M1-S04A-02",
            ))

        if self.canonical_ports is not None:
            description = self.canonical_ports.describe()
            port_ok = (
                description.get("owns_facts") is False
                and description.get("retains_refs") is False
                and description.get("artifact_projector") != "unbound"
                and description.get("event_projector") != "unbound"
            )
            findings.append(self._finding(
                "canonical_reference_only",
                port_ok,
                BrowserIntegrationSeverity.ERROR,
                "Canonical ports project refs without becoming a fact store.",
                "M1-S04A-02",
                **description,
            ))
        else:
            findings.append(self._finding(
                "canonical_reference_only",
                False,
                BrowserIntegrationSeverity.WARNING,
                "Canonical artifact/event projection ports are not bound.",
                "M1-S04A-02",
            ))

        registry_entry = getattr(runtime, "_browser_registry_entry", None) if runtime is not None else None
        findings.append(self._finding(
            "registry_entry_active",
            registry_entry is not None and str(getattr(registry_entry, "status", "")) == "active",
            BrowserIntegrationSeverity.ERROR,
            "BrowserRuntime is owned by one active process registry entry.",
            "M1-S04A-02",
            status=str(getattr(registry_entry, "status", "missing")),
        ))
        resume_runtime = getattr(runtime, "_browser_resume_runtime", None) if runtime is not None else None
        findings.append(self._finding(
            "resume_runtime_reachable",
            resume_runtime is not None and callable(getattr(resume_runtime, "capture", None)) and callable(getattr(resume_runtime, "resume", None)),
            BrowserIntegrationSeverity.ERROR,
            "Resume capsule capture and typed recovery are reachable from the registry-owned runtime.",
            "M1-S04A-02",
            runtime_type=type(resume_runtime).__name__ if resume_runtime is not None else "missing",
        ))
        source_mapper = None
        if session_owner is not None:
            source_mapper = getattr(session_owner, "source_mapper", None) or getattr(session_owner, "cdp_source_mapper", None)
        target_registry = getattr(session_owner, "_targets", {}) if session_owner is not None else {}
        mapping_available = source_mapper is not None or all(
            callable(getattr(target, "cdp_session", None)) or callable(getattr(target, "cdp_session_id", None))
            for target in target_registry.values()
        )
        findings.append(self._finding(
            "target_cdp_source_mapping",
            mapping_available,
            BrowserIntegrationSeverity.ERROR,
            "Active page targets can resolve an attached CDP session route.",
            "M1-S04A-02",
            mapper_type=type(source_mapper).__name__ if source_mapper is not None else "target-runtime-compat",
            target_runtimes=len(target_registry),
        ))

        errors = [item for item in findings if not item.ok and item.severity == BrowserIntegrationSeverity.ERROR]
        warnings = [item for item in findings if not item.ok and item.severity == BrowserIntegrationSeverity.WARNING]
        status = (
            BrowserIntegrationStatus.BLOCKED
            if errors
            else BrowserIntegrationStatus.DEGRADED
            if warnings
            else BrowserIntegrationStatus.READY
        )
        return BrowserIntegrationAuditReport(status=status, findings=tuple(findings))

    @staticmethod
    def _action_executor(runtime: Any, session_owner: Any) -> Any | None:
        for owner in (runtime, session_owner):
            if owner is None:
                continue
            for name in ("session_bound_action_runtime", "action_runtime", "actions"):
                candidate = getattr(owner, name, None)
                if candidate is not None and (
                    callable(getattr(candidate, "execute_plan", None))
                    or callable(getattr(candidate, "execute", None))
                ):
                    return candidate
            if callable(getattr(owner, "execute_plan", None)):
                return owner
        return None

    @staticmethod
    def _executor_owner_match(executor: Any, runtime: Any, session_owner: Any) -> tuple[bool, dict[str, Any]]:
        if executor is None:
            return False, {"reason": "executor_missing"}
        declared_runtime = getattr(executor, "browser_runtime", None)
        declared_session = getattr(executor, "session_runtime", None)
        runtime_matches = declared_runtime is None or declared_runtime is runtime
        session_matches = declared_session is None or declared_session is session_owner
        return runtime_matches and session_matches, {
            "declares_browser_runtime": declared_runtime is not None,
            "declares_session_runtime": declared_session is not None,
            "browser_runtime_matches": runtime_matches,
            "session_runtime_matches": session_matches,
        }

    @staticmethod
    def _finding(
        check_id: str,
        ok: bool,
        severity: BrowserIntegrationSeverity,
        summary: str,
        owner: str,
        **evidence: Any,
    ) -> BrowserIntegrationFinding:
        return BrowserIntegrationFinding(
            check_id=check_id,
            ok=bool(ok),
            severity=severity,
            summary=summary,
            owner=owner,
            evidence=evidence,
        )


def browser_integration_metadata(report: BrowserIntegrationAuditReport) -> dict[str, str]:
    return {
        "browser_integration_audit_runtime": report.runtime_id,
        "browser_integration_audit_owner_unit": report.owner_unit,
        "browser_integration_audit_status": str(report.status),
        "browser_integration_audit_ready": str(report.ready).lower(),
        "browser_integration_audit_findings": str(len(report.findings)),
        "browser_integration_audit_blockers": str(len(report.blocking_findings)),
    }
