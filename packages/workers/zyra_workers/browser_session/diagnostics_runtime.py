from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .integration_models import integration_digest, public_mapping
from .models import BrowserSessionRef, browser_now
from .resume_runtime import BrowserResumeCapsulePort
from .runtime import BrowserRuntime
from .session_lease import BrowserSessionLeaseStore


class BrowserDiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class BrowserDiagnosticCheck:
    check_id: str
    component: str
    ok: bool
    severity: BrowserDiagnosticSeverity
    summary: str
    session_id: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    remediation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", BrowserDiagnosticSeverity(str(self.severity)))
        object.__setattr__(self, "evidence", public_mapping(self.evidence))

    @property
    def blocking(self) -> bool:
        return not self.ok and self.severity == BrowserDiagnosticSeverity.BLOCKING

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "component": self.component,
            "ok": self.ok,
            "severity": str(self.severity),
            "summary": self.summary,
            "session_id": self.session_id,
            "evidence": dict(self.evidence),
            "remediation": self.remediation,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class BrowserDiagnosticFinding:
    finding_id: str
    component: str
    code: str
    severity: BrowserDiagnosticSeverity
    summary: str
    session_id: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    remediation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", BrowserDiagnosticSeverity(str(self.severity)))
        object.__setattr__(self, "evidence", public_mapping(self.evidence))

    @property
    def blocking(self) -> bool:
        return self.severity == BrowserDiagnosticSeverity.BLOCKING

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "component": self.component,
            "code": self.code,
            "severity": str(self.severity),
            "summary": self.summary,
            "session_id": self.session_id,
            "evidence": dict(self.evidence),
            "remediation": self.remediation,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class BrowserSessionDiagnosticView:
    session_id: str
    status: str
    checks: tuple[BrowserDiagnosticCheck, ...]
    findings: tuple[BrowserDiagnosticFinding, ...]
    state: Mapping[str, Any]
    runtime: Mapping[str, Any]

    @property
    def ok(self) -> bool:
        return not any(item.blocking for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "ok": self.ok,
            "checks": [item.to_dict() for item in self.checks],
            "findings": [item.to_dict() for item in self.findings],
            "state": public_mapping(self.state),
            "runtime": public_mapping(self.runtime),
        }


@dataclass(frozen=True, slots=True)
class BrowserDiagnosticsReport:
    ok: bool
    runtime_id: str
    registry: Mapping[str, Any]
    state_store: Mapping[str, Any]
    action_runtime: Mapping[str, Any]
    lease_store: Mapping[str, Any]
    sessions: tuple[BrowserSessionDiagnosticView, ...]
    checks: tuple[BrowserDiagnosticCheck, ...]
    findings: tuple[BrowserDiagnosticFinding, ...]
    generated_at: str = field(default_factory=browser_now)

    @property
    def blocking_findings(self) -> tuple[BrowserDiagnosticFinding, ...]:
        return tuple(item for item in self.findings if item.blocking)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "ok": self.ok,
            "runtime_id": self.runtime_id,
            "registry": public_mapping(self.registry),
            "state_store": public_mapping(self.state_store),
            "action_runtime": public_mapping(self.action_runtime),
            "lease_store": public_mapping(self.lease_store),
            "sessions": [item.to_dict() for item in self.sessions],
            "checks": [item.to_dict() for item in self.checks],
            "findings": [item.to_dict() for item in self.findings],
            "blocking_findings": [item.to_dict() for item in self.blocking_findings],
            "generated_at": self.generated_at,
        }
        value["checksum"] = integration_digest(value)
        return value


class BrowserDiagnosticsRuntime:
    def __init__(
        self,
        browser_runtime: BrowserRuntime,
        *,
        registry_snapshot: Callable[[], Mapping[str, Any]] | None = None,
        action_runtime: Any | None = None,
        lease_store: BrowserSessionLeaseStore | None = None,
        disabled: bool = False,
    ) -> None:
        self.browser_runtime = browser_runtime
        self.registry_snapshot = registry_snapshot
        self.action_runtime = action_runtime
        self.lease_store = lease_store or BrowserSessionLeaseStore(
            Path(browser_runtime.config.state_root) / "integration"
        )
        self.disabled = disabled
        self.session_runtime = getattr(browser_runtime, "_runtime", None)
        if self.session_runtime is None:
            raise TypeError("BrowserRuntime has no BrowserSessionRuntime")

    def diagnose(self, *, session_id: str = "") -> BrowserDiagnosticsReport:
        checks: list[BrowserDiagnosticCheck] = []
        findings: list[BrowserDiagnosticFinding] = []
        if self.disabled:
            findings.append(self._finding(
                "diagnostics_disabled",
                "diagnostics",
                BrowserDiagnosticSeverity.BLOCKING,
                "browser diagnostics runtime is disabled",
                remediation="Enable M1-S04A-02 diagnostics before accepting browser traffic.",
            ))
        checks.extend(self._root_checks())
        checks.extend(self._state_store_checks())
        checks.extend(self._lease_checks())
        action_snapshot = self._action_snapshot()
        checks.extend(self._action_checks(action_snapshot))
        for check in checks:
            if not check.ok:
                findings.append(self._finding_from_check(check))

        sessions = self.browser_runtime.list_sessions()
        if session_id:
            sessions = tuple(item for item in sessions if item.session_id == session_id)
            if not sessions:
                findings.append(self._finding(
                    "session_not_found",
                    "state_store",
                    BrowserDiagnosticSeverity.BLOCKING,
                    f"browser session {session_id} does not exist",
                    session_id=session_id,
                    remediation="Use the session list projection before requesting diagnostics.",
                ))
        session_views: list[BrowserSessionDiagnosticView] = []
        for session in sessions:
            view = self._diagnose_session(session)
            session_views.append(view)
            findings.extend(view.findings)

        registry = dict(self.registry_snapshot() if self.registry_snapshot is not None else {})
        registry_check = BrowserDiagnosticCheck(
            check_id="registry.owner",
            component="runtime_registry",
            ok=bool(registry) and not bool(registry.get("disabled")),
            severity=BrowserDiagnosticSeverity.BLOCKING,
            summary="browser runtime has a process-level registry owner" if registry else "browser runtime registry snapshot is unavailable",
            evidence={"entries": registry.get("entries", 0), "disabled": registry.get("disabled", False)},
            remediation="Construct browser runtimes through BrowserRuntimeRegistry.get_or_create().",
        )
        checks.append(registry_check)
        if not registry_check.ok:
            findings.append(self._finding_from_check(registry_check))

        state_snapshot = self._safe_snapshot(self.browser_runtime.state_store)
        lease_snapshot = self._safe_snapshot(self.lease_store)
        ok = not any(item.blocking for item in findings)
        return BrowserDiagnosticsReport(
            ok=ok,
            runtime_id="zyra-browser-session-diagnostics",
            registry=registry,
            state_store=state_snapshot,
            action_runtime=action_snapshot,
            lease_store=lease_snapshot,
            sessions=tuple(session_views),
            checks=tuple(checks),
            findings=tuple(self._dedupe_findings(findings)),
        )

    def _root_checks(self) -> tuple[BrowserDiagnosticCheck, ...]:
        values = (
            ("state_root", Path(self.browser_runtime.config.state_root)),
            ("runtime_root", Path(self.browser_runtime.config.runtime_root)),
            ("artifact_root", Path(self.browser_runtime.config.artifact_root)),
        )
        checks: list[BrowserDiagnosticCheck] = []
        for name, path in values:
            exists = path.exists() and path.is_dir()
            writable = False
            if exists:
                try:
                    writable = os_access_write(path)
                except OSError:
                    writable = False
            checks.append(BrowserDiagnosticCheck(
                check_id=f"root.{name}",
                component="filesystem",
                ok=exists and writable,
                severity=BrowserDiagnosticSeverity.BLOCKING,
                summary=f"browser {name} exists and is writable" if exists and writable else f"browser {name} is missing or not writable",
                evidence={"path_ref": f"browser-root://{name}", "exists": exists, "writable": writable},
                remediation=f"Create a writable Zyra-owned {name} before starting the browser runtime.",
            ))
        return tuple(checks)

    def _state_store_checks(self) -> tuple[BrowserDiagnosticCheck, ...]:
        issues: tuple[str, ...] = ()
        if hasattr(self.browser_runtime.state_store, "audit"):
            try:
                issues = tuple(str(item) for item in self.browser_runtime.state_store.audit())
            except Exception as error:
                issues = (f"state audit raised {type(error).__name__}: {error}",)
        return (BrowserDiagnosticCheck(
            check_id="state_store.audit",
            component="state_store",
            ok=not issues,
            severity=BrowserDiagnosticSeverity.BLOCKING,
            summary="browser state store is internally consistent" if not issues else "browser state store audit found inconsistencies",
            evidence={"issues": list(issues)},
            remediation="Recover the JsonBrowserStateStore backup or stop affected sessions before continuing.",
        ),)

    def _lease_checks(self) -> tuple[BrowserDiagnosticCheck, ...]:
        try:
            issues = self.lease_store.audit()
        except Exception as error:
            issues = (f"lease audit raised {type(error).__name__}: {error}",)
        return (BrowserDiagnosticCheck(
            check_id="logical_lease.audit",
            component="logical_lease",
            ok=not issues,
            severity=BrowserDiagnosticSeverity.BLOCKING,
            summary="browser logical action leases are consistent" if not issues else "browser logical action lease audit failed",
            evidence={"issues": list(issues)},
            remediation="Revoke stale logical leases and repair dangling action receipt indexes.",
        ),)

    def _action_snapshot(self) -> dict[str, Any]:
        if self.action_runtime is None:
            return {}
        try:
            return dict(self.action_runtime.snapshot())
        except Exception as error:
            return {"snapshot_error": f"{type(error).__name__}: {error}"}

    def _action_checks(self, snapshot: Mapping[str, Any]) -> tuple[BrowserDiagnosticCheck, ...]:
        available = bool(snapshot) and not bool(snapshot.get("disabled")) and not bool(snapshot.get("snapshot_error"))
        return (BrowserDiagnosticCheck(
            check_id="action_runtime.reachable",
            component="action_runtime",
            ok=available,
            severity=BrowserDiagnosticSeverity.BLOCKING,
            summary="session-bound action runtime is reachable" if available else "session-bound action runtime is absent, disabled, or failed snapshot",
            evidence={
                "runtime_id": snapshot.get("runtime_id", ""),
                "disabled": snapshot.get("disabled", False),
                "snapshot_error": snapshot.get("snapshot_error", ""),
            },
            remediation="Bind BrowserSessionApplication.actions when requesting registry diagnostics.",
        ),)

    def _diagnose_session(self, session: BrowserSessionRef) -> BrowserSessionDiagnosticView:
        checks: list[BrowserDiagnosticCheck] = []
        findings: list[BrowserDiagnosticFinding] = []
        runtime_state: dict[str, Any] = {}
        running = session.status == "running"

        cdp = getattr(self.session_runtime, "_cdp", {}).get(session.session_id)
        if cdp is not None:
            cdp_snapshot = cdp.snapshot()
            transport = getattr(cdp, "_transport", None)
            transport_name = type(transport).__name__ if transport is not None else ""
            runtime_state["cdp"] = cdp_snapshot.to_dict()
            runtime_state["cdp_transport"] = transport_name
            cdp_ok = str(cdp_snapshot.status) == "open"
            checks.append(self._session_check(
                session,
                "cdp.open",
                "cdp_runtime",
                cdp_ok or not running,
                "CDP runtime is open" if cdp_ok else "CDP runtime is not open",
                {"status": str(cdp_snapshot.status), "generation": cdp_snapshot.generation, "transport": transport_name},
                remediation="Resume/reconnect the session through BrowserSessionResumeRuntime.",
            ))
            memory_transport = transport_name == "MemoryCdpTransport"
            checks.append(self._session_check(
                session,
                "cdp.production_transport",
                "cdp_transport",
                not memory_transport,
                "CDP uses a production transport" if not memory_transport else "CDP is using MemoryCdpTransport on a production session",
                {"transport": transport_name},
                remediation="Inject a WebSocket-backed CdpTransportPort; memory transport is test-only.",
            ))
        else:
            checks.append(self._session_check(
                session,
                "cdp.present",
                "cdp_runtime",
                not running,
                "non-running session has no CDP runtime" if not running else "persisted running session has no registry-owned CDP runtime",
                {},
                remediation="Mark the stale session failed and explicitly resume it; do not fall back to static actions.",
            ))

        target = getattr(self.session_runtime, "_targets", {}).get(session.session_id)
        if target is not None:
            target_snapshot = target.snapshot()
            runtime_state["target"] = target_snapshot.to_dict()
            target_ok = bool(target_snapshot.active_target_id) and bool(target_snapshot.targets)
            checks.append(self._session_check(
                session,
                "target.focus",
                "target_runtime",
                target_ok or not running,
                "browser target runtime has a focused page" if target_ok else "browser target runtime has no focused page",
                {"active_target_id": target_snapshot.active_target_id, "target_count": len(target_snapshot.targets)},
                remediation="Reconcile targets and run focus recovery before executing an action.",
            ))
        else:
            checks.append(self._session_check(
                session,
                "target.present",
                "target_runtime",
                not running,
                "non-running session has no target runtime" if not running else "persisted running session has no target runtime",
                {},
                remediation="Explicitly resume the session to rebuild target ownership.",
            ))

        event_bus = getattr(self.session_runtime, "_event_buses", {}).get(session.session_id)
        if event_bus is not None:
            bus_snapshot = event_bus.snapshot()
            bus_value = bus_snapshot.to_dict() if hasattr(bus_snapshot, "to_dict") else {"state": str(bus_snapshot.state), "generation": bus_snapshot.generation}
            runtime_state["event_bus"] = bus_value
            bus_ok = str(bus_snapshot.state) == "running"
            checks.append(self._session_check(
                session,
                "event_bus.running",
                "event_bus",
                bus_ok or not running,
                "browser event bus is running" if bus_ok else "browser event bus is stopped",
                bus_value,
                remediation="Resume the session to restart the event bus generation.",
            ))
        else:
            checks.append(self._session_check(
                session,
                "event_bus.present",
                "event_bus",
                not running,
                "non-running session has no event bus" if not running else "persisted running session has no event bus",
                {},
                remediation="Explicitly resume the session; event delivery cannot be reconstructed from logs alone.",
            ))

        profile = self.session_runtime.profile_store.get(session.profile_id) if session.profile_id else None
        if profile is not None:
            profile_health = self.session_runtime.profile_store.health(profile)
            runtime_state["profile"] = profile_health.to_dict()
            checks.append(self._session_check(
                session,
                "profile.healthy",
                "profile_store",
                profile_health.healthy,
                "browser profile is healthy" if profile_health.healthy else "browser profile is missing or corrupt",
                profile_health.to_dict(),
                remediation="Quarantine the corrupt profile and start a new isolated profile.",
            ))
        elif running or session.profile_id:
            checks.append(self._session_check(
                session,
                "profile.present",
                "profile_store",
                False,
                "browser session profile reference cannot be resolved",
                {"profile_id": session.profile_id},
                remediation="Treat the session as state_lost; do not continue with another profile silently.",
            ))

        process = self._process_evidence(session)
        runtime_state["process"] = process
        process_required = session.process_id is not None and running
        checks.append(self._session_check(
            session,
            "process.live",
            "process_controller",
            (not process_required) or bool(process.get("alive")),
            "owned browser process is live" if process.get("alive") else "owned browser process handle is unavailable or exited",
            process,
            remediation="Restart the session from its redacted capsule or report state_lost.",
        ))

        capsule = BrowserResumeCapsulePort(self.browser_runtime.state_store).get(session.session_id, session_revision=session.revision)
        runtime_state["resume_capsule"] = capsule.to_dict() if capsule else {}
        checks.append(self._session_check(
            session,
            "resume.capsule",
            "resume_runtime",
            capsule is not None or not session.keep_alive,
            "browser resume capsule is persisted" if capsule else "keep-alive browser session has no resume capsule",
            {"capsule_id": capsule.capsule_id if capsule else "", "keep_alive": session.keep_alive},
            remediation="Capture the redacted resume capsule immediately after session start.",
        ))

        lease = self.lease_store.get_lease(session.session_id)
        runtime_state["logical_lease"] = lease.to_dict() if lease else {}
        runtime_state["action_receipts"] = [item.to_dict() for item in self.lease_store.list_actions(session.session_id, limit=20)]
        runtime_state["artifact_handoffs"] = list(self.lease_store.list_handoffs(session.session_id, limit=20))
        artifact_checks = self._artifact_checks(session.session_id)
        checks.extend(artifact_checks)

        for check in checks:
            if not check.ok:
                findings.append(self._finding_from_check(check))
        return BrowserSessionDiagnosticView(
            session_id=session.session_id,
            status=session.status,
            checks=tuple(checks),
            findings=tuple(findings),
            state=session.to_dict(),
            runtime=runtime_state,
        )

    def _artifact_checks(self, session_id: str) -> tuple[BrowserDiagnosticCheck, ...]:
        handoffs = self.lease_store.list_handoffs(session_id, limit=1000)
        if not handoffs:
            return ()
        root = Path(self.browser_runtime.config.artifact_root).resolve()
        issues: list[str] = []
        verified = 0
        for handoff in handoffs:
            uri = str(handoff.get("uri") or "")
            try:
                path = Path(uri).resolve()
                path.relative_to(root)
            except (OSError, ValueError):
                issues.append(f"artifact outside root: {handoff.get('artifact_id')}")
                continue
            if not path.is_file():
                issues.append(f"artifact missing: {handoff.get('artifact_id')}")
                continue
            expected_size = int(handoff.get("size_bytes") or -1)
            if path.stat().st_size != expected_size:
                issues.append(f"artifact size mismatch: {handoff.get('artifact_id')}")
                continue
            expected_digest = str(handoff.get("sha256") or "")
            actual_digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            if expected_digest != actual_digest:
                issues.append(f"artifact digest mismatch: {handoff.get('artifact_id')}")
                continue
            verified += 1
        return (BrowserDiagnosticCheck(
            check_id="artifact_handoff.integrity",
            component="artifact_pipeline",
            ok=not issues,
            severity=BrowserDiagnosticSeverity.BLOCKING,
            summary="browser artifact handoffs are present and verified" if not issues else "browser artifact handoff integrity failed",
            session_id=session_id,
            evidence={"handoffs": len(handoffs), "verified": verified, "issues": issues},
            remediation="Repair or remove invalid handoffs before projecting the session as healthy.",
        ),)

    def _process_evidence(self, session: BrowserSessionRef) -> dict[str, Any]:
        if session.process_id is None:
            return {"process_id": None, "owned": False, "alive": bool(session.endpoint_url), "remote": True}
        controller = self.browser_runtime.process_controller
        if controller is None:
            return {"process_id": session.process_id, "owned": True, "alive": False, "reason": "controller_missing"}
        if hasattr(controller, "is_alive"):
            try:
                return {"process_id": session.process_id, "owned": True, "alive": bool(controller.is_alive(session.process_id))}
            except Exception as error:
                return {"process_id": session.process_id, "owned": True, "alive": False, "error": f"{type(error).__name__}: {error}"}
        handle = getattr(controller, "_owned", {}).get(session.process_id)
        if handle is None:
            return {"process_id": session.process_id, "owned": True, "alive": False, "reason": "handle_not_registered"}
        try:
            exit_code = handle.poll()
            snapshot = handle.snapshot()
            value = {
                "process_id": session.process_id,
                "owned": True,
                "alive": exit_code is None,
                "exit_code": exit_code,
                "state": str(snapshot.state),
                "endpoint": snapshot.endpoint,
            }
        except Exception as error:
            value = {"process_id": session.process_id, "owned": True, "alive": False, "error": f"{type(error).__name__}: {error}"}
        return value

    @staticmethod
    def _safe_snapshot(component: Any) -> dict[str, Any]:
        if component is None or not hasattr(component, "snapshot"):
            return {}
        try:
            value = component.snapshot()
            return value.to_dict() if hasattr(value, "to_dict") else dict(value)
        except Exception as error:
            return {"snapshot_error": f"{type(error).__name__}: {error}"}

    @staticmethod
    def _session_check(
        session: BrowserSessionRef,
        check_id: str,
        component: str,
        ok: bool,
        summary: str,
        evidence: Mapping[str, Any],
        *,
        remediation: str,
    ) -> BrowserDiagnosticCheck:
        return BrowserDiagnosticCheck(
            check_id=check_id,
            component=component,
            ok=ok,
            severity=BrowserDiagnosticSeverity.BLOCKING,
            summary=summary,
            session_id=session.session_id,
            evidence=evidence,
            remediation=remediation,
        )

    @staticmethod
    def _finding_from_check(check: BrowserDiagnosticCheck) -> BrowserDiagnosticFinding:
        return BrowserDiagnosticFinding(
            finding_id=integration_digest({"check_id": check.check_id, "session_id": check.session_id})[:32],
            component=check.component,
            code=check.check_id.replace(".", "_"),
            severity=check.severity,
            summary=check.summary,
            session_id=check.session_id,
            evidence=check.evidence,
            remediation=check.remediation,
        )

    @staticmethod
    def _finding(
        code: str,
        component: str,
        severity: BrowserDiagnosticSeverity,
        summary: str,
        *,
        session_id: str = "",
        evidence: Mapping[str, Any] | None = None,
        remediation: str = "",
    ) -> BrowserDiagnosticFinding:
        return BrowserDiagnosticFinding(
            finding_id=integration_digest({"code": code, "session_id": session_id})[:32],
            component=component,
            code=code,
            severity=severity,
            summary=summary,
            session_id=session_id,
            evidence=evidence or {},
            remediation=remediation,
        )

    @staticmethod
    def _dedupe_findings(values: Sequence[BrowserDiagnosticFinding]) -> tuple[BrowserDiagnosticFinding, ...]:
        selected: dict[tuple[str, str], BrowserDiagnosticFinding] = {}
        for item in values:
            selected[(item.code, item.session_id)] = item
        return tuple(sorted(selected.values(), key=lambda item: (not item.blocking, item.component, item.code, item.session_id)))


def os_access_write(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK)
