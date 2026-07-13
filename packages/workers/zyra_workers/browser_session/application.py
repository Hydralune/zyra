from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType, to_jsonable
from zyra_runtime import WorkerRequest, WorkerResult

from .action_runtime import LeaseBoundBrowserSessionApplication
from .canonical_ports import BrowserCanonicalPorts
from .integration_audit import BrowserSessionIntegrationAudit
from .integration_models import BrowserApplicationResult, public_mapping
from .models import BrowserSessionStartResult
from .resume_runtime import BrowserSessionResumeRuntime
from .session_lease import BrowserSessionLeaseStore


@dataclass(frozen=True, slots=True)
class BrowserSessionApplicationResult:
    worker_result: WorkerResult
    event_records: tuple[EventRecord, ...]
    receipts: tuple[Any, ...]
    session_start: BrowserSessionStartResult
    domain_result: BrowserApplicationResult

    @property
    def ok(self) -> bool:
        return self.worker_result.ok

    @property
    def error(self) -> str | None:
        return self.worker_result.error

    @property
    def artifacts(self) -> tuple[Any, ...]:
        return tuple(self.worker_result.artifacts)

    @property
    def events(self) -> tuple[EventRecord, ...]:
        return self.event_records

    @property
    def action_receipts(self) -> tuple[Any, ...]:
        return self.receipts

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self.worker_result.metadata


class BrowserSessionApplication:
    """The single public BrowserWorker facade for M1-S04A-02.

    One instance is attached to each registry-owned BrowserRuntime. Repeated
    BrowserWorker construction returns that same instance, preserving lease,
    control, receipt and audit ownership across HTTP requests.
    """

    def __new__(cls, browser_runtime: Any, *args: Any, **kwargs: Any):
        if browser_runtime is None:
            return super().__new__(cls)
        disabled = bool(kwargs.get("disabled", args[3] if len(args) > 3 else False))
        if disabled:
            # A disabled caller must not inherit an enabled singleton and its
            # cached successful receipts. Keep this facade isolated/fail-closed.
            return super().__new__(cls)
        existing = getattr(browser_runtime, "_browser_session_application", None)
        if isinstance(existing, cls):
            return existing
        instance = super().__new__(cls)
        setattr(browser_runtime, "_browser_session_application", instance)
        return instance

    def __init__(
        self,
        browser_runtime: Any,
        artifact_store: Any = None,
        receipt_store: BrowserSessionLeaseStore | None = None,
        canonical_ports: BrowserCanonicalPorts | None = None,
        disabled: bool = False,
    ) -> None:
        if getattr(self, "_initialized", False):
            if artifact_store is not None and self.artifact_store is None:
                self.artifact_store = artifact_store
            return
        if browser_runtime is None:
            raise ValueError("BrowserSessionApplication requires an existing BrowserRuntime")
        self.browser_runtime = browser_runtime
        self.artifact_store = artifact_store
        shared_store = getattr(browser_runtime, "_browser_integration_lease_store", None)
        self.receipt_store = receipt_store or shared_store or BrowserSessionLeaseStore(
            Path(browser_runtime.config.state_root) / "integration"
        )
        if not disabled:
            setattr(browser_runtime, "_browser_integration_lease_store", self.receipt_store)
        self.canonical_ports = canonical_ports or getattr(browser_runtime, "_browser_canonical_ports", None)
        self.disabled = disabled
        self.delegate = LeaseBoundBrowserSessionApplication(
            browser_runtime,
            artifact_store,
            receipt_store=self.receipt_store,
            canonical_ports=None,
            disabled=disabled,
        )
        self.actions = self.delegate.actions
        self.lifecycle_transactions = self.delegate.lifecycle_transactions
        self.resume_runtime = getattr(browser_runtime, "_browser_resume_runtime", None) or BrowserSessionResumeRuntime(
            browser_runtime,
            disabled=disabled,
        )
        if not disabled:
            setattr(browser_runtime, "_browser_resume_runtime", self.resume_runtime)
        self.audit_runtime = BrowserSessionIntegrationAudit(
            browser_runtime,
            application=self,
            control_runtime=getattr(browser_runtime, "_browser_control_runtime", None),
            canonical_ports=self.canonical_ports,
        )
        if not disabled:
            setattr(browser_runtime, "session_bound_action_runtime", self.actions)
            setattr(browser_runtime, "_browser_integration_audit", self.audit_runtime)
        self._lock = threading.RLock()
        self._executions = 0
        self._failures = 0
        self._initialized = True

    def execute_plan(
        self,
        request: WorkerRequest,
        session_start: BrowserSessionStartResult,
        plan: Sequence[Mapping[str, Any]],
        permission_gate: Any,
    ) -> BrowserSessionApplicationResult:
        if self.disabled:
            return self._failure(request, session_start, "browser_session_application_disabled")
        error = self._validate(request, session_start, plan, permission_gate)
        if error:
            return self._failure(request, session_start, error)
        with self._lock:
            constraints = dict(request.constraints or {})
            execution_identity = str(
                constraints.get("browser_idempotency_key")
                or constraints.get("idempotency_key")
            )
            stable_plan = tuple(
                {
                    **dict(item),
                    **({"_zyra_execution_id": execution_identity} if execution_identity else {}),
                }
                for item in plan
            )
            result = self.delegate.execute_plan(request, session_start, stable_plan, permission_gate)
            self._bind_sealed_keepalive_custody(request, permission_gate, result)
            audit = self.audit_runtime.inspect()
            projection_error = ""
            if self.canonical_ports is not None:
                projected = self.canonical_ports.project(
                    artifacts=result.artifacts,
                    events=result.events,
                )
                if not projected.ok:
                    projection_error = "canonical_browser_projection_failed"
            metadata = {
                **dict(result.metadata),
                "browser_canonical_session_id": session_start.session.canonical_session_id,
                "browser_application_runtime": "zyra-browser-session-application",
                "browser_integration_audit_status": str(audit.status),
                "browser_integration_audit_blockers": len(audit.blocking_findings),
                "browser_canonical_projection_error": projection_error,
            }
            audit_blocked = bool(audit.blocking_findings)
            ok = result.ok and not projection_error and not audit_blocked
            final_error = result.error or projection_error or (
                "browser_integration_audit_blocked" if audit_blocked else ""
            )
            final = replace(result, ok=ok, error=final_error, metadata=metadata)
            self._executions += 1
            if not final.ok:
                self._failures += 1
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=final.ok,
                summary=(
                    f"Browser session executed {len(final.action_receipts)} action(s)."
                    if final.ok
                    else "Browser session action plan failed."
                ),
                artifacts=list(final.artifacts),
                events=[to_jsonable(item) for item in final.events],
                error=final.error or None,
                metadata={str(key): str(value) for key, value in final.metadata.items()},
            )
            return BrowserSessionApplicationResult(
                worker_result=worker_result,
                event_records=final.events,
                receipts=final.action_receipts,
                session_start=session_start,
                domain_result=final,
            )

    @staticmethod
    def _bind_sealed_keepalive_custody(request: WorkerRequest, permission_gate: Any, result: Any) -> None:
        """Carry an authenticated sealed-session claim to the next keepalive request.

        The gate itself remains request-scoped. Only its designated bearer fields
        are attached to the caller-owned constraints mapping; they are never
        projected into events, receipts, artifacts, or application metadata.
        """

        constraints = request.constraints
        if not isinstance(constraints, dict) or not bool(constraints.get("keep_alive")):
            return
        if str(getattr(permission_gate, "effective_mode", "")).lower() != "sealed":
            return
        if not bool(getattr(result, "ok", False)):
            return
        session_id = str(getattr(permission_gate, "session_id", "") or "")
        custody_token = str(getattr(permission_gate, "custody_token", "") or "")
        if session_id and custody_token:
            constraints.setdefault("permission_session_id", session_id)
            constraints.setdefault("permission_session_custody_token", custody_token)

    @staticmethod
    def _validate(
        request: WorkerRequest,
        session_start: BrowserSessionStartResult,
        plan: Sequence[Mapping[str, Any]],
        permission_gate: Any,
    ) -> str:
        if not isinstance(request, WorkerRequest):
            return "invalid_browser_worker_request"
        if not isinstance(session_start, BrowserSessionStartResult):
            return "invalid_browser_session_start"
        if not session_start.ok or session_start.session.status != "running":
            return "browser_session_not_running"
        if session_start.session.run_id != request.run_id or session_start.session.task_id != request.task_id:
            return "browser_session_identity_mismatch"
        if permission_gate is None:
            return "browser_permission_gate_unavailable"
        if isinstance(plan, (str, bytes, bytearray)) or not isinstance(plan, Sequence) or not plan:
            return "invalid_browser_plan"
        if any(not isinstance(item, Mapping) for item in plan):
            return "invalid_browser_plan"
        return ""

    @staticmethod
    def _failure(
        request: Any,
        session_start: Any,
        code: str,
        *,
        details: str = "",
    ) -> BrowserSessionApplicationResult:
        run_id = str(getattr(request, "run_id", ""))
        task_id = str(getattr(request, "task_id", ""))
        node_id = getattr(request, "node_id", None)
        session = getattr(session_start, "session", None)
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.BROWSER_SESSION_LIFECYCLE,
            payload={
                "browser_application": {
                    "phase": "plan_failed",
                    "error": code,
                    "details": details,
                    "browser_session_id": str(getattr(session, "session_id", "") or ""),
                }
            },
        )
        domain = BrowserApplicationResult(
            ok=False,
            events=(event,),
            artifacts=(),
            action_receipts=(),
            error=code,
            metadata=public_mapping({
                "browser_backend": "zyra-browser-productized",
                "browser_application_runtime": "zyra-browser-session-application",
                "failure_details": details,
            }),
        )
        worker_result = WorkerResult(
            request_id=str(getattr(request, "request_id", "")),
            ok=False,
            summary="Browser session action plan failed.",
            artifacts=[],
            events=[to_jsonable(event)],
            error=code,
            metadata={
                "browser_backend": "zyra-browser-productized",
                "browser_application_runtime": "zyra-browser-session-application",
            },
        )
        return BrowserSessionApplicationResult(
            worker_result=worker_result,
            event_records=(event,),
            receipts=(),
            session_start=session_start,
            domain_result=domain,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-session-application",
            "owner_unit": "M1-S04A-02",
            "disabled": self.disabled,
            "executions": self._executions,
            "failures": self._failures,
            "actions": self.actions.snapshot(),
            "lifecycle_transactions": self.lifecycle_transactions.snapshot(),
            "integration_state": self.receipt_store.snapshot(),
            "audit": self.audit_runtime.inspect().to_dict(),
        }
