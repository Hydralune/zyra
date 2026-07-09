from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .claude_context_window import (
    ClaudeContextBlock,
    ClaudeContextBlockRole,
    ClaudeContextBlockState,
    ClaudeContextSource,
    ClaudeContextWindowManager,
)
from .codeworker_context_security_runtime import (
    CodeWorkerContextSecurityRuntime,
    ContextProvenanceKind,
    ContextSecuritySnapshot,
    ContextSecurityVerdict,
    context_security_metadata,
    default_context_security_source_decisions,
    provenance_from_mapping,
)
from .compact_restore_runtime import NextTurnRestoreContract, RestoreSegment, RestoreSegmentKind
from .runtime_budget_state import RuntimeBudgetState


M1_02D_RESTORE_INTEGRATION_OWNER_UNIT = "M1-02D"
CODEWORKER_RESTORE_INTEGRATION_RUNTIME_ID = "codeworker_restore_integration_runtime"


class RestoreIntegrationStatus(StrEnum):
    READY = "ready"
    APPLIED = "applied"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"
    SKIPPED = "skipped"


class RestoreIntegrationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class RestoreIntegrationSurface(StrEnum):
    CONTRACT = "contract"
    CONTEXT_WINDOW = "context_window"
    MODEL_ENVELOPE = "model_envelope"
    SECURITY = "security"
    RUNTIME_BUDGET = "runtime_budget"
    QUERY_SESSION = "query_session"


class RestoreMessageKind(StrEnum):
    BOUNDARY = "boundary"
    SEGMENT = "segment"
    SECURITY_NOTICE = "security_notice"


@dataclass(frozen=True, slots=True)
class RestoreIntegrationFinding:
    code: str
    severity: RestoreIntegrationSeverity
    surface: RestoreIntegrationSurface
    message: str
    segment_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == RestoreIntegrationSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "segment_id": self.segment_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RestoreContextMessage:
    message_id: str
    role: str
    content: str
    kind: RestoreMessageKind
    segment_id: str = ""
    boundary_id: str = ""
    artifact_id: str = ""
    source_id: str = ""
    block_id: str = ""
    turn_index: int = 0
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def chars(self) -> int:
        return len(self.content)

    def to_model_message(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "metadata": {
                "restore_message_id": self.message_id,
                "restore_message_kind": str(self.kind),
                "restore_segment_id": self.segment_id,
                "restore_boundary_id": self.boundary_id,
                "restore_block_id": self.block_id,
                "artifact_id": self.artifact_id,
                "source_id": self.source_id,
                **dict(self.metadata),
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "kind": str(self.kind),
            "segment_id": self.segment_id,
            "boundary_id": self.boundary_id,
            "artifact_id": self.artifact_id,
            "source_id": self.source_id,
            "block_id": self.block_id,
            "turn_index": self.turn_index,
            "chars": self.chars,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RestoreContextBlockProjection:
    block_id: str
    role: str
    state: str
    chars: int
    source_kind: str
    source_id: str
    segment_id: str = ""
    boundary_id: str = ""
    artifact_id: str = ""
    turn_index: int = 0
    priority: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "role": self.role,
            "state": self.state,
            "chars": self.chars,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "segment_id": self.segment_id,
            "boundary_id": self.boundary_id,
            "artifact_id": self.artifact_id,
            "turn_index": self.turn_index,
            "priority": self.priority,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RestoreContractApplication:
    application_id: str
    session_id: str
    worker_request_id: str
    turn_index: int
    contract_id: str
    boundary_id: str
    status: RestoreIntegrationStatus
    messages: tuple[RestoreContextMessage, ...]
    blocks: tuple[RestoreContextBlockProjection, ...]
    security_snapshot: ContextSecuritySnapshot | None
    findings: tuple[RestoreIntegrationFinding, ...] = field(default_factory=tuple)
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and not any(finding.blocking for finding in self.findings)

    @property
    def model_messages(self) -> list[dict[str, Any]]:
        return [message.to_model_message() for message in self.messages]

    @property
    def restored_chars(self) -> int:
        return sum(message.chars for message in self.messages)

    @property
    def segment_count(self) -> int:
        return sum(1 for message in self.messages if message.kind == RestoreMessageKind.SEGMENT)

    @property
    def boundary_message_count(self) -> int:
        return sum(1 for message in self.messages if message.kind == RestoreMessageKind.BOUNDARY)

    @property
    def untrusted_message_count(self) -> int:
        return sum(1 for message in self.messages if message.metadata.get("trust_level") == "external_untrusted")

    @property
    def redacted_message_count(self) -> int:
        return sum(1 for message in self.messages if message.metadata.get("secret_redaction_state") == "redacted")

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    def metadata(self) -> dict[str, str]:
        return {
            "restore_integration_application_id": self.application_id,
            "restore_integration_ok": str(self.ok).lower(),
            "restore_integration_status": str(self.status),
            "restore_integration_disabled": str(self.disabled).lower(),
            "restore_integration_turn_index": str(self.turn_index),
            "restore_integration_contract_id": self.contract_id,
            "restore_integration_boundary_id": self.boundary_id,
            "restore_integration_model_messages": str(len(self.messages)),
            "restore_integration_segments": str(self.segment_count),
            "restore_integration_restored_chars": str(self.restored_chars),
            "restore_integration_context_blocks": str(len(self.blocks)),
            "restore_integration_untrusted_messages": str(self.untrusted_message_count),
            "restore_integration_redacted_messages": str(self.redacted_message_count),
            "restore_integration_blocking_count": str(self.blocking_count),
            **context_security_metadata(self.security_snapshot),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_restore_application.v1",
            "application_id": self.application_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "turn_index": self.turn_index,
            "contract_id": self.contract_id,
            "boundary_id": self.boundary_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "messages": [message.to_dict() for message in self.messages],
            "model_messages": self.model_messages,
            "blocks": [block.to_dict() for block in self.blocks],
            "security_snapshot": self.security_snapshot.to_dict() if self.security_snapshot is not None else None,
            "findings": [finding.to_dict() for finding in self.findings],
            "restored_chars": self.restored_chars,
            "segment_count": self.segment_count,
            "boundary_message_count": self.boundary_message_count,
            "untrusted_message_count": self.untrusted_message_count,
            "redacted_message_count": self.redacted_message_count,
            "blocking_count": self.blocking_count,
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RestoreIntegrationReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    applications: tuple[RestoreContractApplication, ...]
    pending_contract_ids: tuple[str, ...] = ()
    skipped_contract_ids: tuple[str, ...] = ()
    findings: tuple[RestoreIntegrationFinding, ...] = field(default_factory=tuple)
    source_decisions: tuple[dict[str, str], ...] = field(default_factory=tuple)
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and all(application.ok for application in self.applications) and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def status(self) -> RestoreIntegrationStatus:
        if self.disabled:
            return RestoreIntegrationStatus.DISABLED
        if any(finding.blocking for finding in self.findings) or any(
            not application.ok for application in self.applications
        ):
            return RestoreIntegrationStatus.BLOCKED
        if self.pending_contract_ids:
            return RestoreIntegrationStatus.DEGRADED
        if self.applications:
            return RestoreIntegrationStatus.APPLIED
        return RestoreIntegrationStatus.READY

    @property
    def application_count(self) -> int:
        return len(self.applications)

    @property
    def model_message_count(self) -> int:
        return sum(len(application.messages) for application in self.applications)

    @property
    def context_block_count(self) -> int:
        return sum(len(application.blocks) for application in self.applications)

    @property
    def restored_chars(self) -> int:
        return sum(application.restored_chars for application in self.applications)

    @property
    def untrusted_message_count(self) -> int:
        return sum(application.untrusted_message_count for application in self.applications)

    @property
    def redacted_message_count(self) -> int:
        return sum(application.redacted_message_count for application in self.applications)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking) + sum(
            application.blocking_count for application in self.applications
        )

    def metadata(self) -> dict[str, str]:
        latest = self.applications[-1] if self.applications else None
        return {
            "restore_integration_report_id": self.report_id,
            "restore_integration_owner_unit": self.owner_unit,
            "restore_integration_runtime_id": self.runtime_id,
            "restore_integration_ok": str(self.ok).lower(),
            "restore_integration_status": str(self.status),
            "restore_integration_disabled": str(self.disabled).lower(),
            "restore_integration_applications": str(self.application_count),
            "restore_integration_model_messages": str(self.model_message_count),
            "restore_integration_context_blocks": str(self.context_block_count),
            "restore_integration_restored_chars": str(self.restored_chars),
            "restore_integration_pending_contracts": str(len(self.pending_contract_ids)),
            "restore_integration_skipped_contracts": str(len(self.skipped_contract_ids)),
            "restore_integration_untrusted_messages": str(self.untrusted_message_count),
            "restore_integration_redacted_messages": str(self.redacted_message_count),
            "restore_integration_blocking_count": str(self.blocking_count),
            "restore_integration_latest_contract_id": latest.contract_id if latest else "",
            "restore_integration_latest_turn_index": str(latest.turn_index if latest else 0),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_restore_integration.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "applications": [application.to_dict() for application in self.applications],
            "pending_contract_ids": list(self.pending_contract_ids),
            "skipped_contract_ids": list(self.skipped_contract_ids),
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "application_count": self.application_count,
            "model_message_count": self.model_message_count,
            "context_block_count": self.context_block_count,
            "restored_chars": self.restored_chars,
            "untrusted_message_count": self.untrusted_message_count,
            "redacted_message_count": self.redacted_message_count,
            "blocking_count": self.blocking_count,
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerRestoreIntegrationRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_RESTORE_INTEGRATION_OWNER_UNIT,
        runtime_id: str = CODEWORKER_RESTORE_INTEGRATION_RUNTIME_ID,
        security_runtime: CodeWorkerContextSecurityRuntime | None = None,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.security_runtime = security_runtime or CodeWorkerContextSecurityRuntime()
        self.disabled = disabled

    def apply_contract(
        self,
        contract: NextTurnRestoreContract | None,
        *,
        context_window: ClaudeContextWindowManager,
        budget_state: RuntimeBudgetState,
        turn_index: int,
        run_id: str,
        task_id: str,
        node_id: str | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RestoreContractApplication | None:
        if contract is None:
            return None
        metadata = dict(metadata or {})
        findings: list[RestoreIntegrationFinding] = []
        if self.disabled:
            findings.append(
                RestoreIntegrationFinding(
                    code="RESTORE_INTEGRATION_RUNTIME_DISABLED",
                    severity=RestoreIntegrationSeverity.BLOCKER,
                    surface=RestoreIntegrationSurface.CONTRACT,
                    message="CodeWorker restore integration runtime is disabled; restore contract was not applied.",
                    metadata={"contract_id": contract.contract_id},
                )
            )
            return RestoreContractApplication(
                application_id=new_id("restore_app"),
                session_id=contract.session_id,
                worker_request_id=contract.worker_request_id,
                turn_index=turn_index,
                contract_id=contract.contract_id,
                boundary_id=contract.boundary_id,
                status=RestoreIntegrationStatus.DISABLED,
                messages=(),
                blocks=(),
                security_snapshot=None,
                findings=tuple(findings),
                disabled=True,
            )
        if not contract.ok:
            findings.append(
                RestoreIntegrationFinding(
                    code="RESTORE_CONTRACT_NOT_OK",
                    severity=RestoreIntegrationSeverity.BLOCKER,
                    surface=RestoreIntegrationSurface.CONTRACT,
                    message="Restore contract is missing required segments and cannot safely feed the next turn.",
                    metadata={"missing_required_segments": str(contract.missing_required_segments)},
                )
            )
        if not budget_state.ok:
            findings.append(
                RestoreIntegrationFinding(
                    code="RUNTIME_BUDGET_NOT_READY_FOR_RESTORE",
                    severity=RestoreIntegrationSeverity.BLOCKER,
                    surface=RestoreIntegrationSurface.RUNTIME_BUDGET,
                    message="Restore application requires RuntimeBudgetState so restored chars and next-turn context can be audited.",
                    metadata={"contract_id": contract.contract_id},
                )
            )
        security_items = self._security_items(contract)
        security_snapshot = self.security_runtime.build_snapshot(
            security_items,
            default_kind=ContextProvenanceKind.COMPACT_SUMMARY,
        )
        if not security_snapshot.ok:
            findings.append(
                RestoreIntegrationFinding(
                    code="RESTORE_CONTEXT_SECURITY_BLOCKED",
                    severity=RestoreIntegrationSeverity.BLOCKER,
                    surface=RestoreIntegrationSurface.SECURITY,
                    message="Context security runtime blocked one or more restore messages.",
                    metadata={"context_security_snapshot_id": security_snapshot.snapshot_id},
                )
            )
        messages: list[RestoreContextMessage] = []
        blocks: list[RestoreContextBlockProjection] = []
        verdict_by_source = {verdict.source_id: verdict for verdict in security_snapshot.verdicts}
        if contract.boundary_id:
            boundary_message, boundary_block = self._boundary_message_and_block(
                contract,
                context_window=context_window,
                turn_index=turn_index,
                verdict=verdict_by_source.get(contract.boundary_id),
                metadata=metadata,
            )
            messages.append(boundary_message)
            blocks.append(boundary_block)
        for segment in contract.restore_segments:
            if not segment.available:
                findings.append(
                    RestoreIntegrationFinding(
                        code="RESTORE_SEGMENT_UNAVAILABLE",
                        severity=RestoreIntegrationSeverity.BLOCKER
                        if segment.required
                        else RestoreIntegrationSeverity.WARNING,
                        surface=RestoreIntegrationSurface.CONTRACT,
                        message="Restore segment was unavailable and was not inserted into next-turn context.",
                        segment_id=segment.segment_id,
                        metadata={
                            "required": str(segment.required).lower(),
                            "kind": str(segment.kind),
                            "artifact_id": segment.artifact_id,
                        },
                    )
                )
                continue
            verdict = verdict_by_source.get(segment.segment_id) or verdict_by_source.get(segment.source_id)
            if verdict is not None and not verdict.ok:
                findings.append(
                    RestoreIntegrationFinding(
                        code="RESTORE_SEGMENT_SECURITY_BLOCKED",
                        severity=RestoreIntegrationSeverity.BLOCKER,
                        surface=RestoreIntegrationSurface.SECURITY,
                        message="Restore segment was blocked by context security verdict.",
                        segment_id=segment.segment_id,
                        metadata=verdict.metadata(),
                    )
                )
                continue
            restore_message, projection = self._segment_message_and_block(
                segment,
                contract=contract,
                context_window=context_window,
                turn_index=turn_index,
                verdict=verdict,
                metadata=metadata,
            )
            messages.append(restore_message)
            blocks.append(projection)
        if messages:
            budget_state.record_context_usage(
                active_chars=context_window.active_chars,
                source="restore_contract_applied",
                metadata={
                    "restore_contract_id": contract.contract_id,
                    "restore_application_turn_index": str(turn_index),
                    "restore_model_messages": str(len(messages)),
                    "restore_context_blocks": str(len(blocks)),
                },
            )
        status = RestoreIntegrationStatus.APPLIED if messages else RestoreIntegrationStatus.SKIPPED
        if findings and any(finding.blocking for finding in findings):
            status = RestoreIntegrationStatus.BLOCKED
        elif findings:
            status = RestoreIntegrationStatus.DEGRADED
        return RestoreContractApplication(
            application_id=new_id("restore_app"),
            session_id=contract.session_id,
            worker_request_id=contract.worker_request_id,
            turn_index=turn_index,
            contract_id=contract.contract_id,
            boundary_id=contract.boundary_id,
            status=status,
            messages=tuple(messages),
            blocks=tuple(blocks),
            security_snapshot=security_snapshot,
            findings=tuple(findings),
        )

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        applications: Sequence[RestoreContractApplication | None],
        pending_contracts: Sequence[NextTurnRestoreContract | None] = (),
        skipped_contract_ids: Sequence[str] = (),
        findings: Sequence[RestoreIntegrationFinding] = (),
    ) -> RestoreIntegrationReport:
        real_applications = tuple(application for application in applications if application is not None)
        pending_ids = tuple(
            contract.contract_id
            for contract in pending_contracts
            if contract is not None and contract.contract_id
        )
        report_findings = list(findings)
        if self.disabled:
            report_findings.append(
                RestoreIntegrationFinding(
                    code="RESTORE_INTEGRATION_RUNTIME_DISABLED",
                    severity=RestoreIntegrationSeverity.BLOCKER,
                    surface=RestoreIntegrationSurface.CONTRACT,
                    message="CodeWorker restore integration runtime is disabled.",
                )
            )
        if pending_ids:
            report_findings.append(
                RestoreIntegrationFinding(
                    code="RESTORE_CONTRACT_PENDING_AT_SESSION_END",
                    severity=RestoreIntegrationSeverity.WARNING,
                    surface=RestoreIntegrationSurface.MODEL_ENVELOPE,
                    message="One or more restore contracts were created after the final model turn and remain pending.",
                    metadata={"pending_contract_ids": ",".join(pending_ids)},
                )
            )
        return RestoreIntegrationReport(
            report_id=new_id("restore_integration"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            applications=real_applications,
            pending_contract_ids=pending_ids,
            skipped_contract_ids=tuple(str(item) for item in skipped_contract_ids if str(item)),
            findings=tuple(report_findings),
            source_decisions=default_restore_integration_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_application(
        self,
        application: RestoreContractApplication,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_restore_context_applied",
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": application.session_id,
                    "worker_request_id": application.worker_request_id,
                    "phase": phase,
                    "restore_application": application.to_dict(),
                }
            },
        )

    def event_for_report(
        self,
        report: RestoreIntegrationReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_restore_integration",
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
                    "restore_integration": report.to_dict(),
                }
            },
        )

    def metadata(self, report: RestoreIntegrationReport | None = None) -> dict[str, str]:
        if report is None:
            return {
                "restore_integration_ok": str(not self.disabled).lower(),
                "restore_integration_status": str(
                    RestoreIntegrationStatus.DISABLED if self.disabled else RestoreIntegrationStatus.READY
                ),
                "restore_integration_runtime_id": self.runtime_id,
            }
        return report.metadata()

    def _boundary_message_and_block(
        self,
        contract: NextTurnRestoreContract,
        *,
        context_window: ClaudeContextWindowManager,
        turn_index: int,
        verdict: ContextSecurityVerdict | None,
        metadata: Mapping[str, Any],
    ) -> tuple[RestoreContextMessage, RestoreContextBlockProjection]:
        content = f"Context compact boundary {contract.boundary_id} restored for CodeWorker turn {turn_index}."
        message_metadata = {
            "kind": "compact_restore_boundary",
            "restore_contract_id": contract.contract_id,
            "boundary_id": contract.boundary_id,
            "compact_artifact_id": contract.compact_artifact_id,
            "source_provenance": "compact_summary",
            "source_ref": contract.compact_artifact_id or contract.boundary_id,
            "trust_level": "trusted_system",
            "secret_redaction_state": "clean",
            "runtime_owner": "zyra",
            "source_path": "packages/runtime/zyra_runtime/codeworker_restore_integration.py",
            "upstream_source_path": "src/services/compact/compact.ts",
            **_metadata_strings(metadata),
        }
        if verdict is not None:
            content = verdict.sanitized_text or content
            message_metadata.update(verdict.metadata())
        block = self._add_context_block(
            context_window,
            role=ClaudeContextBlockRole.SYSTEM,
            text=content,
            priority=890,
            turn_index=turn_index,
            source_id=contract.boundary_id,
            source_kind="compact_restore_boundary",
            upstream_source_path="src/services/compact/compact.ts",
            metadata=message_metadata,
            artifact_ids=[contract.compact_artifact_id] if contract.compact_artifact_id else [],
        )
        message = RestoreContextMessage(
            message_id=new_id("restore_msg"),
            role="system",
            content=content,
            kind=RestoreMessageKind.BOUNDARY,
            boundary_id=contract.boundary_id,
            artifact_id=contract.compact_artifact_id,
            source_id=contract.boundary_id,
            block_id=block.block_id,
            turn_index=turn_index,
            metadata=message_metadata,
        )
        projection = self._projection_from_block(
            block,
            segment_id="",
            boundary_id=contract.boundary_id,
            artifact_id=contract.compact_artifact_id,
        )
        return message, projection

    def _segment_message_and_block(
        self,
        segment: RestoreSegment,
        *,
        contract: NextTurnRestoreContract,
        context_window: ClaudeContextWindowManager,
        turn_index: int,
        verdict: ContextSecurityVerdict | None,
        metadata: Mapping[str, Any],
    ) -> tuple[RestoreContextMessage, RestoreContextBlockProjection]:
        role = _message_role_for_segment(segment)
        block_role = _block_role_for_segment(segment)
        priority = _priority_for_segment(segment)
        raw_content = segment.content or f"Restore artifact {segment.artifact_id}"
        message_metadata = {
            "kind": str(segment.kind),
            "restore_contract_id": contract.contract_id,
            "restore_segment_id": segment.segment_id,
            "boundary_id": contract.boundary_id,
            "artifact_id": segment.artifact_id,
            "source_id": segment.source_id,
            "source_ref": segment.metadata.get("source_ref") or segment.source_id,
            "source_provenance": segment.metadata.get("source_provenance") or _provenance_for_segment(segment),
            "trust_level": segment.metadata.get("trust_level") or _trust_for_segment(segment),
            "secret_redaction_state": segment.metadata.get("secret_redaction_state") or "clean",
            "retrieval_query": segment.metadata.get("retrieval_query", ""),
            "retrieval_scope": segment.metadata.get("retrieval_scope", ""),
            "retrieval_budget": segment.metadata.get("retrieval_budget", ""),
            "code_index_source": segment.metadata.get("code_index_source", "false"),
            "runtime_owner": "zyra",
            "source_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
            "upstream_source_path": "src/services/compact/sessionMemoryCompact.ts",
            **_metadata_strings(segment.metadata),
            **_metadata_strings(metadata),
        }
        content = raw_content
        if verdict is not None:
            content = verdict.sanitized_text
            message_metadata.update(verdict.metadata())
        if message_metadata.get("trust_level") == "external_untrusted" and not content.startswith("[UNTRUSTED_CONTEXT]"):
            content = f"[UNTRUSTED_CONTEXT source={message_metadata.get('source_ref') or segment.source_id}]\n{content}"
        block = self._add_context_block(
            context_window,
            role=block_role,
            text=content,
            priority=priority,
            turn_index=turn_index,
            source_id=segment.source_id or segment.segment_id,
            source_kind=f"restore:{segment.kind}",
            upstream_source_path=_upstream_source_for_segment(segment),
            metadata=message_metadata,
            artifact_ids=[segment.artifact_id] if segment.artifact_id else [],
            tool_call_id=segment.source_id if segment.kind == RestoreSegmentKind.TOOL_RESULT else "",
            tool_name=segment.label if segment.kind == RestoreSegmentKind.TOOL_RESULT else "",
        )
        message = RestoreContextMessage(
            message_id=new_id("restore_msg"),
            role=role,
            content=content,
            kind=RestoreMessageKind.SEGMENT,
            segment_id=segment.segment_id,
            boundary_id=contract.boundary_id,
            artifact_id=segment.artifact_id,
            source_id=segment.source_id,
            block_id=block.block_id,
            turn_index=turn_index,
            metadata=message_metadata,
        )
        projection = self._projection_from_block(
            block,
            segment_id=segment.segment_id,
            boundary_id=contract.boundary_id,
            artifact_id=segment.artifact_id,
        )
        return message, projection

    def _security_items(self, contract: NextTurnRestoreContract) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if contract.boundary_id:
            items.append(
                {
                    "segment_id": contract.boundary_id,
                    "source_id": contract.boundary_id,
                    "kind": "compact_summary",
                    "content": f"Context compact boundary {contract.boundary_id} restored for next CodeWorker turn.",
                    "metadata": {
                        "source_provenance": "compact_summary",
                        "trust_level": "trusted_system",
                        "secret_redaction_state": "clean",
                        "source_ref": contract.compact_artifact_id or contract.boundary_id,
                        "source_path": "packages/runtime/zyra_runtime/codeworker_restore_integration.py",
                        "upstream_source_path": "src/services/compact/compact.ts",
                    },
                }
            )
        for segment in contract.restore_segments:
            data = segment.to_dict()
            data["content"] = segment.content
            data["metadata"] = {
                "source_provenance": _provenance_for_segment(segment),
                "trust_level": _trust_for_segment(segment),
                "secret_redaction_state": "clean",
                "source_ref": segment.source_id or segment.artifact_id,
                **dict(segment.metadata),
            }
            items.append(data)
        return items

    def _add_context_block(
        self,
        context_window: ClaudeContextWindowManager,
        *,
        role: ClaudeContextBlockRole,
        text: str,
        priority: int,
        turn_index: int,
        source_id: str,
        source_kind: str,
        upstream_source_path: str,
        metadata: Mapping[str, Any],
        artifact_ids: Sequence[str] = (),
        tool_call_id: str = "",
        tool_name: str = "",
    ) -> ClaudeContextBlock:
        block = ClaudeContextBlock(
            role=role,
            text=text,
            priority=priority,
            source=ClaudeContextSource(
                source_id=source_id,
                source_kind=source_kind,
                source_path="packages/runtime/zyra_runtime/codeworker_restore_integration.py",
                upstream_source_path=upstream_source_path,
                runtime_owner="zyra",
                metadata=_metadata_strings(metadata),
            ),
            state=ClaudeContextBlockState.RESTORED,
            turn_index=turn_index,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            artifact_ids=[artifact_id for artifact_id in artifact_ids if artifact_id],
            metadata=_metadata_strings(metadata),
        )
        return context_window.add_block(block)

    def _projection_from_block(
        self,
        block: ClaudeContextBlock,
        *,
        segment_id: str,
        boundary_id: str,
        artifact_id: str,
    ) -> RestoreContextBlockProjection:
        source = block.source
        return RestoreContextBlockProjection(
            block_id=block.block_id,
            role=str(block.role),
            state=str(block.state),
            chars=block.chars,
            source_kind=source.source_kind if source else "",
            source_id=source.source_id if source else "",
            segment_id=segment_id,
            boundary_id=boundary_id,
            artifact_id=artifact_id,
            turn_index=block.turn_index or 0,
            priority=block.priority,
            metadata={str(k): str(v) for k, v in dict(block.metadata or {}).items()},
        )


def restore_integration_metadata(report: RestoreIntegrationReport | None) -> dict[str, str]:
    if report is None:
        return {
            "restore_integration_ok": "false",
            "restore_integration_status": "missing",
            "restore_integration_report_id": "",
        }
    return report.metadata()


def restore_application_from_payload(payload: Mapping[str, Any]) -> RestoreContractApplication:
    messages = tuple(_message_from_payload(item) for item in _as_list(payload.get("messages")))
    blocks = tuple(_block_projection_from_payload(item) for item in _as_list(payload.get("blocks")))
    findings = tuple(_finding_from_payload(item) for item in _as_list(payload.get("findings")))
    return RestoreContractApplication(
        application_id=str(payload.get("application_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        turn_index=_safe_int(payload.get("turn_index")),
        contract_id=str(payload.get("contract_id") or ""),
        boundary_id=str(payload.get("boundary_id") or ""),
        status=_enum_or_default(
            RestoreIntegrationStatus,
            payload.get("status"),
            RestoreIntegrationStatus.READY,
        ),
        messages=messages,
        blocks=blocks,
        security_snapshot=None,
        findings=findings,
        disabled=_truthy(payload.get("disabled")),
        created_at=str(payload.get("created_at") or now_iso()),
    )


def restore_integration_report_from_payload(payload: Mapping[str, Any]) -> RestoreIntegrationReport:
    applications = tuple(
        restore_application_from_payload(item)
        for item in _as_list(payload.get("applications"))
        if isinstance(item, Mapping)
    )
    findings = tuple(_finding_from_payload(item) for item in _as_list(payload.get("findings")))
    return RestoreIntegrationReport(
        report_id=str(payload.get("report_id") or ""),
        owner_unit=str(payload.get("owner_unit") or M1_02D_RESTORE_INTEGRATION_OWNER_UNIT),
        runtime_id=str(payload.get("runtime_id") or CODEWORKER_RESTORE_INTEGRATION_RUNTIME_ID),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        applications=applications,
        pending_contract_ids=tuple(str(item) for item in _as_list(payload.get("pending_contract_ids"))),
        skipped_contract_ids=tuple(str(item) for item in _as_list(payload.get("skipped_contract_ids"))),
        findings=findings,
        source_decisions=tuple(
            {str(k): str(v) for k, v in dict(item).items()}
            for item in _as_list(payload.get("source_decisions"))
            if isinstance(item, Mapping)
        ),
        disabled=_truthy(payload.get("disabled")),
        created_at=str(payload.get("created_at") or now_iso()),
    )


def default_restore_integration_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_restore_integration.py",
            "decision": "zyra_module_migrated",
            "capability": "compact boundary restore contract is inserted into the next CodeWorker model request",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/sessionMemoryCompact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_restore_integration.py",
            "decision": "zyra_module_migrated",
            "capability": "restored files, skills, MCP instruction deltas, deferred tools, and budget state become context blocks",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/*",
            "target_path": "packages/runtime/zyra_runtime/codeworker_restore_integration.py",
            "decision": "adapter_encapsulated",
            "capability": "event-sourced restore applications are replayable from API/session projections",
        },
        *default_context_security_source_decisions(),
    )


def _message_role_for_segment(segment: RestoreSegment) -> str:
    if segment.kind == RestoreSegmentKind.TOOL_RESULT:
        return "tool"
    return "system"


def _block_role_for_segment(segment: RestoreSegment) -> ClaudeContextBlockRole:
    if segment.kind == RestoreSegmentKind.TOOL_RESULT:
        return ClaudeContextBlockRole.TOOL
    if segment.kind == RestoreSegmentKind.CONTEXT_SUMMARY:
        return ClaudeContextBlockRole.SUMMARY
    if segment.kind in {RestoreSegmentKind.FILE_ATTACHMENT, RestoreSegmentKind.INVOKED_SKILL}:
        return ClaudeContextBlockRole.MEMORY
    return ClaudeContextBlockRole.SYSTEM


def _priority_for_segment(segment: RestoreSegment) -> int:
    priorities = {
        RestoreSegmentKind.CONTEXT_SUMMARY: 880,
        RestoreSegmentKind.BUDGET_STATE: 870,
        RestoreSegmentKind.ACTIVE_PLAN: 860,
        RestoreSegmentKind.FILE_ATTACHMENT: 830,
        RestoreSegmentKind.MCP_INSTRUCTION_DELTA: 810,
        RestoreSegmentKind.INVOKED_SKILL: 790,
        RestoreSegmentKind.DEFERRED_TOOL: 780,
        RestoreSegmentKind.TOOL_RESULT: 760,
    }
    return priorities.get(segment.kind, 740)


def _provenance_for_segment(segment: RestoreSegment) -> str:
    explicit = str(segment.metadata.get("source_provenance") or "").strip()
    if explicit:
        return explicit
    mapping = {
        RestoreSegmentKind.CONTEXT_SUMMARY: "compact_summary",
        RestoreSegmentKind.TOOL_RESULT: "tool_result",
        RestoreSegmentKind.FILE_ATTACHMENT: "workspace_file",
        RestoreSegmentKind.ACTIVE_PLAN: "active_plan",
        RestoreSegmentKind.INVOKED_SKILL: "invoked_skill",
        RestoreSegmentKind.MCP_INSTRUCTION_DELTA: "mcp_instruction",
        RestoreSegmentKind.DEFERRED_TOOL: "deferred_tool",
        RestoreSegmentKind.BUDGET_STATE: "runtime_budget",
    }
    return mapping.get(segment.kind, "unknown")


def _trust_for_segment(segment: RestoreSegment) -> str:
    explicit = str(segment.metadata.get("trust_level") or "").strip()
    if explicit:
        return explicit
    mapping = {
        RestoreSegmentKind.CONTEXT_SUMMARY: "workspace",
        RestoreSegmentKind.TOOL_RESULT: "tool_output",
        RestoreSegmentKind.FILE_ATTACHMENT: "workspace",
        RestoreSegmentKind.ACTIVE_PLAN: "workspace",
        RestoreSegmentKind.INVOKED_SKILL: "workspace",
        RestoreSegmentKind.MCP_INSTRUCTION_DELTA: "external_untrusted",
        RestoreSegmentKind.DEFERRED_TOOL: "trusted_system",
        RestoreSegmentKind.BUDGET_STATE: "trusted_system",
    }
    return mapping.get(segment.kind, "unknown")


def _upstream_source_for_segment(segment: RestoreSegment) -> str:
    if segment.kind == RestoreSegmentKind.MCP_INSTRUCTION_DELTA:
        return "src/services/mcpClient.ts"
    if segment.kind == RestoreSegmentKind.TOOL_RESULT:
        return "src/utils/toolResultStorage.ts"
    if segment.kind == RestoreSegmentKind.INVOKED_SKILL:
        return "src/tools/SkillTool"
    return "src/services/compact/sessionMemoryCompact.ts"


def _message_from_payload(payload: Mapping[str, Any]) -> RestoreContextMessage:
    return RestoreContextMessage(
        message_id=str(payload.get("message_id") or ""),
        role=str(payload.get("role") or "system"),
        content=str(payload.get("content") or ""),
        kind=_enum_or_default(RestoreMessageKind, payload.get("kind"), RestoreMessageKind.SEGMENT),
        segment_id=str(payload.get("segment_id") or ""),
        boundary_id=str(payload.get("boundary_id") or ""),
        artifact_id=str(payload.get("artifact_id") or ""),
        source_id=str(payload.get("source_id") or ""),
        block_id=str(payload.get("block_id") or ""),
        turn_index=_safe_int(payload.get("turn_index")),
        metadata={str(k): str(v) for k, v in dict(_as_mapping(payload.get("metadata"))).items()},
        created_at=str(payload.get("created_at") or now_iso()),
    )


def _block_projection_from_payload(payload: Mapping[str, Any]) -> RestoreContextBlockProjection:
    return RestoreContextBlockProjection(
        block_id=str(payload.get("block_id") or ""),
        role=str(payload.get("role") or ""),
        state=str(payload.get("state") or ""),
        chars=_safe_int(payload.get("chars")),
        source_kind=str(payload.get("source_kind") or ""),
        source_id=str(payload.get("source_id") or ""),
        segment_id=str(payload.get("segment_id") or ""),
        boundary_id=str(payload.get("boundary_id") or ""),
        artifact_id=str(payload.get("artifact_id") or ""),
        turn_index=_safe_int(payload.get("turn_index")),
        priority=_safe_int(payload.get("priority")),
        metadata={str(k): str(v) for k, v in dict(_as_mapping(payload.get("metadata"))).items()},
    )


def _finding_from_payload(payload: Mapping[str, Any]) -> RestoreIntegrationFinding:
    return RestoreIntegrationFinding(
        code=str(payload.get("code") or ""),
        severity=_enum_or_default(
            RestoreIntegrationSeverity,
            payload.get("severity"),
            RestoreIntegrationSeverity.INFO,
        ),
        surface=_enum_or_default(
            RestoreIntegrationSurface,
            payload.get("surface"),
            RestoreIntegrationSurface.CONTRACT,
        ),
        message=str(payload.get("message") or ""),
        segment_id=str(payload.get("segment_id") or ""),
        metadata={str(k): str(v) for k, v in dict(_as_mapping(payload.get("metadata"))).items()},
    )


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return default


def _metadata_strings(value: Mapping[str, Any]) -> dict[str, str]:
    return {str(k): str(v) for k, v in dict(value or {}).items() if v is not None}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _jsonable(value: Any) -> Any:
    return to_jsonable(value)
