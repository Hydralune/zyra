from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger_audit import InternalizationLedgerAuditor, LedgerAuditReport
from .ledger_events import append_jsonl_event, mutation_event_payload
from .ledger_models import (
    InternalizationLedgerEntry,
    LedgerLifecycle,
    MainPathStatus,
    now_iso,
    to_jsonable,
)
from .ledger_policy import TransitionPolicyResult, evaluate_transition
from .ledger_store import InternalizationLedger, LedgerMutation


@dataclass(slots=True)
class LedgerAdvanceRequest:
    ledger_id: str
    lifecycle: LedgerLifecycle | None = None
    main_path_status: MainPathStatus | None = None
    reason: str = ""
    note: str = ""
    effective_lines: int = 0
    actor: str = "agent"
    write_event: bool = True
    force: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerAdvanceRequest":
        lifecycle = data.get("lifecycle")
        status = data.get("main_path_status") or data.get("status")
        return cls(
            ledger_id=str(data.get("ledger_id") or ""),
            lifecycle=LedgerLifecycle(str(lifecycle)) if lifecycle else None,
            main_path_status=MainPathStatus(str(status)) if status else None,
            reason=str(data.get("reason") or ""),
            note=str(data.get("note") or ""),
            effective_lines=int(data.get("effective_lines") or 0),
            actor=str(data.get("actor") or "agent"),
            write_event=bool(data.get("write_event", True)),
            force=bool(data.get("force", False)),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerAdvanceResult:
    ok: bool
    request: LedgerAdvanceRequest
    policy: TransitionPolicyResult
    mutation: LedgerMutation | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    audit: LedgerAuditReport | None = None
    event_path: str = ""
    event_payload: dict[str, Any] | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerBulkAdvanceResult:
    ok: bool
    requested: int
    changed: int
    failed: int
    results: list[LedgerAdvanceResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class LedgerWorkflow:
    def __init__(
        self,
        project_root: str | Path,
        ledger: InternalizationLedger,
        *,
        event_log_path: str | Path | None = None,
        strict_audit: bool = True,
    ) -> None:
        self.project_root = Path(project_root)
        self.ledger = ledger
        self.event_log_path = Path(event_log_path) if event_log_path else self.project_root / "tmp" / "events.jsonl"
        self.strict_audit = strict_audit

    def preview_advance(self, request: LedgerAdvanceRequest) -> LedgerAdvanceResult:
        entry = self.ledger.get(request.ledger_id)
        if entry is None:
            return LedgerAdvanceResult(
                ok=False,
                request=request,
                policy=_missing_entry_policy(request),
                message=f"Unknown ledger entry: {request.ledger_id}",
            )
        policy = evaluate_transition(
            entry,
            to_lifecycle=request.lifecycle,
            to_status=request.main_path_status,
            reason=request.reason,
            effective_lines=request.effective_lines,
        )
        return LedgerAdvanceResult(
            ok=policy.allowed or request.force,
            request=request,
            policy=policy,
            before=entry.to_dict(),
            after=_preview_after(entry, request),
            message="preview",
        )

    def advance(self, request: LedgerAdvanceRequest) -> LedgerAdvanceResult:
        entry = self.ledger.get(request.ledger_id)
        if entry is None:
            return LedgerAdvanceResult(
                ok=False,
                request=request,
                policy=_missing_entry_policy(request),
                message=f"Unknown ledger entry: {request.ledger_id}",
            )
        policy = evaluate_transition(
            entry,
            to_lifecycle=request.lifecycle,
            to_status=request.main_path_status,
            reason=request.reason,
            effective_lines=request.effective_lines,
        )
        if not policy.allowed and not request.force:
            return LedgerAdvanceResult(
                ok=False,
                request=request,
                policy=policy,
                before=entry.to_dict(),
                after=_preview_after(entry, request),
                message="transition rejected by policy",
            )
        before = entry.to_dict()
        if request.lifecycle is not None:
            entry.lifecycle = request.lifecycle
        if request.main_path_status is not None:
            entry.main_path_status = request.main_path_status
        if request.note:
            entry.risk_notes.append(request.note)
        if request.reason:
            entry.metadata.setdefault("transition_reasons", []).append(
                {
                    "at": now_iso(),
                    "actor": request.actor,
                    "reason": request.reason,
                    "from_lifecycle": before.get("lifecycle"),
                    "to_lifecycle": str(entry.lifecycle),
                    "from_status": before.get("main_path_status"),
                    "to_status": str(entry.main_path_status),
                }
            )
        if request.effective_lines:
            entry.metadata["last_effective_lines"] = request.effective_lines
        if request.metadata:
            entry.metadata.setdefault("transition_metadata", []).append(
                {
                    "at": now_iso(),
                    "actor": request.actor,
                    "metadata": dict(request.metadata),
                }
            )
        entry.updated_at = now_iso()
        mutation = self.ledger.upsert(entry)
        audit = InternalizationLedgerAuditor(self.project_root, strict=self.strict_audit).audit(self.ledger)
        event_payload = None
        event_path = ""
        if request.write_event:
            event_payload = mutation_event_payload(mutation, trigger=f"advance:{request.actor}")
            event_payload["payload"]["integration_ledger_update"]["policy"] = policy.to_dict()
            event_payload["payload"]["integration_ledger_update"]["audit_after"] = {
                "ok": audit.ok,
                "error_count": audit.error_count,
                "blocker_count": audit.blocker_count,
                "warning_count": audit.warning_count,
            }
            append_jsonl_event(event_payload, self.event_log_path)
            event_path = str(self.event_log_path)
        return LedgerAdvanceResult(
            ok=audit.ok or request.force,
            request=request,
            policy=policy,
            mutation=mutation,
            before=before,
            after=entry.to_dict(),
            audit=audit,
            event_path=event_path,
            event_payload=event_payload,
            message="advanced",
        )

    def bulk_advance(self, requests: list[LedgerAdvanceRequest]) -> LedgerBulkAdvanceResult:
        results: list[LedgerAdvanceResult] = []
        for request in requests:
            results.append(self.advance(request))
        changed = sum(1 for result in results if result.mutation is not None)
        failed = sum(1 for result in results if not result.ok)
        return LedgerBulkAdvanceResult(
            ok=failed == 0,
            requested=len(requests),
            changed=changed,
            failed=failed,
            results=results,
        )

    def mark_unit_in_progress(self, owner_unit: str, *, actor: str = "agent") -> LedgerBulkAdvanceResult:
        requests = [
            LedgerAdvanceRequest(
                ledger_id=entry.ledger_id,
                lifecycle=LedgerLifecycle.IN_PROGRESS,
                main_path_status=entry.main_path_status,
                reason=f"{owner_unit} execution started",
                actor=actor,
            )
            for entry in self.ledger.by_owner_unit(owner_unit)
            if entry.lifecycle in {LedgerLifecycle.CANDIDATE, LedgerLifecycle.PLANNED}
        ]
        return self.bulk_advance(requests)

    def mark_unit_deferred(self, owner_unit: str, *, reason: str, actor: str = "agent") -> LedgerBulkAdvanceResult:
        requests = [
            LedgerAdvanceRequest(
                ledger_id=entry.ledger_id,
                lifecycle=LedgerLifecycle.DEFERRED,
                main_path_status=MainPathStatus.BLOCKED,
                reason=reason,
                actor=actor,
            )
            for entry in self.ledger.by_owner_unit(owner_unit)
            if entry.lifecycle not in {LedgerLifecycle.INTERNALIZED, LedgerLifecycle.PRODUCTIZED, LedgerLifecycle.REJECTED}
        ]
        return self.bulk_advance(requests)


def request_from_payload(ledger_id: str, payload: dict[str, Any]) -> LedgerAdvanceRequest:
    data = dict(payload)
    data.setdefault("ledger_id", ledger_id)
    return LedgerAdvanceRequest.from_dict(data)


def requests_from_payload(payload: dict[str, Any]) -> list[LedgerAdvanceRequest]:
    raw_requests = payload.get("requests")
    if isinstance(raw_requests, list):
        return [LedgerAdvanceRequest.from_dict(item) for item in raw_requests if isinstance(item, dict)]
    owner_unit = str(payload.get("owner_unit") or "")
    if owner_unit:
        raise ValueError("owner_unit bulk actions must be handled by LedgerWorkflow convenience methods")
    return [LedgerAdvanceRequest.from_dict(payload)]


def _preview_after(entry: InternalizationLedgerEntry, request: LedgerAdvanceRequest) -> dict[str, Any]:
    after = entry.to_dict()
    if request.lifecycle is not None:
        after["lifecycle"] = str(request.lifecycle)
    if request.main_path_status is not None:
        after["main_path_status"] = str(request.main_path_status)
    if request.note:
        after.setdefault("risk_notes", []).append(request.note)
    return after


def _missing_entry_policy(request: LedgerAdvanceRequest) -> TransitionPolicyResult:
    from .ledger_policy import LedgerPolicyCode, LedgerPolicyFinding, LedgerPolicySeverity

    return TransitionPolicyResult(
        allowed=False,
        from_lifecycle=LedgerLifecycle.CANDIDATE,
        to_lifecycle=request.lifecycle or LedgerLifecycle.CANDIDATE,
        from_status=MainPathStatus.PLANNED,
        to_status=request.main_path_status or MainPathStatus.PLANNED,
        findings=[
            LedgerPolicyFinding(
                code=LedgerPolicyCode.INVALID_TRANSITION,
                severity=LedgerPolicySeverity.ERROR,
                message=f"Unknown ledger entry: {request.ledger_id}",
                ledger_id=request.ledger_id,
                remediation="Create the ledger record before advancing it.",
            )
        ],
    )
