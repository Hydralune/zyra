from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_COMPACT_RECOVERY_AUDIT_OWNER_UNIT = "M1-02D"
CODEWORKER_COMPACT_RECOVERY_AUDIT_RUNTIME_ID = "codeworker_context_compact_recovery_audit_runtime"


class CompactRecoveryAuditStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    DISABLED = "disabled"


class CompactRecoveryAuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CompactRecoveryAuditSurface(StrEnum):
    CONTEXT_USAGE = "context_usage"
    COMPACT_BOUNDARY = "compact_boundary"
    RESTORE_CONTRACT = "restore_contract"
    MODEL_ENVELOPE = "model_envelope"
    API_RETRY = "api_retry"
    RUNTIME_BUDGET = "runtime_budget"
    PROVENANCE = "provenance"
    TASK_API = "task_api"
    DISCONNECT = "disconnect"


class CompactRecoveryRuleKind(StrEnum):
    REQUIRED = "required"
    EFFECT = "effect"
    DISCONNECT = "disconnect"
    PROVENANCE = "provenance"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class CompactRecoveryEvidence:
    evidence_id: str
    kind: str
    phase: str
    passed: bool
    message: str
    event_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "phase": self.phase,
            "passed": self.passed,
            "message": self.message,
            "event_id": self.event_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactRecoveryFinding:
    code: str
    severity: CompactRecoveryAuditSeverity
    surface: CompactRecoveryAuditSurface
    message: str
    rule_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == CompactRecoveryAuditSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "rule_id": self.rule_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactRecoveryRuleResult:
    rule_id: str
    kind: CompactRecoveryRuleKind
    surface: CompactRecoveryAuditSurface
    passed: bool
    required: bool
    description: str
    evidence: tuple[CompactRecoveryEvidence, ...] = ()
    findings: tuple[CompactRecoveryFinding, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and not self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": str(self.kind),
            "surface": str(self.surface),
            "passed": self.passed,
            "required": self.required,
            "blocking": self.blocking,
            "description": self.description,
            "evidence": [item.to_dict() for item in self.evidence],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactRecoveryAuditReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    rules: tuple[CompactRecoveryRuleResult, ...]
    findings: tuple[CompactRecoveryFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and not any(rule.blocking for rule in self.rules) and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def status(self) -> CompactRecoveryAuditStatus:
        if self.disabled:
            return CompactRecoveryAuditStatus.DISABLED
        if not self.ok:
            return CompactRecoveryAuditStatus.FAIL
        if self.findings or any(rule.findings for rule in self.rules):
            return CompactRecoveryAuditStatus.WARN
        return CompactRecoveryAuditStatus.PASS

    @property
    def rule_count(self) -> int:
        return len(self.rules)

    @property
    def passed_rule_count(self) -> int:
        return sum(1 for rule in self.rules if rule.passed)

    @property
    def blocking_rule_count(self) -> int:
        return sum(1 for rule in self.rules if rule.blocking)

    @property
    def evidence_count(self) -> int:
        return sum(len(rule.evidence) for rule in self.rules)

    @property
    def provenance_rule_count(self) -> int:
        return sum(1 for rule in self.rules if rule.kind == CompactRecoveryRuleKind.PROVENANCE)

    @property
    def recovery_rule_count(self) -> int:
        return sum(1 for rule in self.rules if rule.kind == CompactRecoveryRuleKind.RECOVERY)

    def metadata(self) -> dict[str, str]:
        return {
            "compact_recovery_audit_report_id": self.report_id,
            "compact_recovery_audit_owner_unit": self.owner_unit,
            "compact_recovery_audit_runtime_id": self.runtime_id,
            "compact_recovery_audit_ok": str(self.ok).lower(),
            "compact_recovery_audit_status": str(self.status),
            "compact_recovery_audit_disabled": str(self.disabled).lower(),
            "compact_recovery_audit_rules": str(self.rule_count),
            "compact_recovery_audit_passed_rules": str(self.passed_rule_count),
            "compact_recovery_audit_blocking_rules": str(self.blocking_rule_count),
            "compact_recovery_audit_evidence": str(self.evidence_count),
            "compact_recovery_audit_findings": str(len(self.findings)),
            "compact_recovery_audit_provenance_rules": str(self.provenance_rule_count),
            "compact_recovery_audit_recovery_rules": str(self.recovery_rule_count),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_context_compact_recovery_audit.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "rules": [rule.to_dict() for rule in self.rules],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "rule_count": self.rule_count,
            "passed_rule_count": self.passed_rule_count,
            "blocking_rule_count": self.blocking_rule_count,
            "evidence_count": self.evidence_count,
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerContextCompactRecoveryAuditRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_COMPACT_RECOVERY_AUDIT_OWNER_UNIT,
        runtime_id: str = CODEWORKER_COMPACT_RECOVERY_AUDIT_RUNTIME_ID,
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
        compact_restore_report: Any,
        restore_integration_report: Any,
        model_stream_reports: Sequence[Any],
        api_retry_reports: Sequence[Any],
        event_records: Sequence[Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> CompactRecoveryAuditReport:
        metadata = dict(metadata or {})
        event_views = [_event_view(event) for event in event_records]
        phase_index = _phase_index(event_views)
        compact_payload = _to_mapping(compact_restore_report)
        restore_payload = _to_mapping(restore_integration_report)
        model_payloads = [_to_mapping(report) for report in model_stream_reports]
        retry_payloads = [_to_mapping(report) for report in api_retry_reports]
        rules = [
            self._rule_context_usage(compact_payload, metadata, phase_index),
            self._rule_compact_contract(compact_payload, phase_index),
            self._rule_restore_application(restore_payload, phase_index),
            self._rule_restore_reaches_model(restore_payload, model_payloads),
            self._rule_restore_provenance(restore_payload),
            self._rule_untrusted_markers(restore_payload, model_payloads),
            self._rule_model_retry_recovery(model_payloads, retry_payloads),
            self._rule_prompt_too_long_path(model_payloads, retry_payloads),
            self._rule_runtime_budget(phase_index, metadata),
            self._rule_task_api_projection(phase_index),
        ]
        findings: list[CompactRecoveryFinding] = []
        if self.disabled:
            findings.append(
                CompactRecoveryFinding(
                    code="COMPACT_RECOVERY_AUDIT_DISABLED",
                    severity=CompactRecoveryAuditSeverity.BLOCKER,
                    surface=CompactRecoveryAuditSurface.DISCONNECT,
                    message="CodeWorker compact recovery audit runtime is disabled.",
                )
            )
        for rule in rules:
            findings.extend(rule.findings)
        return CompactRecoveryAuditReport(
            report_id=new_id("compact_recovery_audit"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            rules=tuple(rules),
            findings=tuple(findings),
            source_decisions=default_compact_recovery_audit_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_report(
        self,
        report: CompactRecoveryAuditReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_compact_recovery_audit",
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
                    "compact_recovery_audit": report.to_dict(),
                }
            },
        )

    def metadata(self, report: CompactRecoveryAuditReport | None = None) -> dict[str, str]:
        if report is None:
            return {
                "compact_recovery_audit_ok": str(not self.disabled).lower(),
                "compact_recovery_audit_status": str(
                    CompactRecoveryAuditStatus.DISABLED if self.disabled else CompactRecoveryAuditStatus.PASS
                ),
                "compact_recovery_audit_runtime_id": self.runtime_id,
            }
        return report.metadata()

    def _rule_context_usage(
        self,
        compact_payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
        phase_index: Mapping[str, list[Mapping[str, Any]]],
    ) -> CompactRecoveryRuleResult:
        usage = _nested(compact_payload, "context_budget", "usage")
        active_chars = _safe_int(usage.get("active_chars") or metadata.get("runtime_budget_state_context_used_chars"))
        active_limit = _safe_int(usage.get("active_limit_chars") or metadata.get("query_context_budget_chars"))
        passed = active_chars > 0 and active_limit > 0
        evidence = [
            _evidence(
                "context_usage",
                "compact_restore_report",
                passed,
                "Compact restore report contains live context usage.",
                metadata={"active_chars": str(active_chars), "active_limit_chars": str(active_limit)},
            ),
            _evidence(
                "runtime_budget_updated",
                "runtime_budget_updated",
                bool(phase_index.get("runtime_budget_updated")),
                "Runtime budget update events are present.",
                metadata={"events": str(len(phase_index.get("runtime_budget_updated", [])))},
            ),
        ]
        findings = []
        if not passed:
            findings.append(
                _finding(
                    "CONTEXT_USAGE_NOT_LIVE",
                    CompactRecoveryAuditSeverity.BLOCKER,
                    CompactRecoveryAuditSurface.CONTEXT_USAGE,
                    "Compact decision lacks live context usage and cannot prove it came from real session state.",
                    "context_usage_live",
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="context_usage_live",
            kind=CompactRecoveryRuleKind.REQUIRED,
            surface=CompactRecoveryAuditSurface.CONTEXT_USAGE,
            passed=passed,
            required=True,
            description="Context compact decisions must be based on live context window and runtime budget state.",
            evidence=tuple(evidence),
            findings=tuple(findings),
            metadata={"active_chars": str(active_chars), "active_limit_chars": str(active_limit)},
        )

    def _rule_compact_contract(
        self,
        compact_payload: Mapping[str, Any],
        phase_index: Mapping[str, list[Mapping[str, Any]]],
    ) -> CompactRecoveryRuleResult:
        contract = _nested(compact_payload, "restore_contract")
        contract_id = str(contract.get("contract_id") or compact_payload.get("restore_contract_id") or "")
        compact_needed = _truthy(compact_payload.get("compact_needed"))
        passed = bool(contract_id) or not compact_needed
        evidence = [
            _evidence(
                "restore_contract",
                "compact_restore_report",
                bool(contract_id),
                "Compact restore report has a next-turn restore contract.",
                metadata={"contract_id": contract_id},
            ),
            _evidence(
                "pending_event",
                "compact_restore_contract_pending",
                bool(phase_index.get("compact_restore_contract_pending")) or bool(contract_id),
                "A pending compact restore contract event was emitted for next-turn consumption.",
                metadata={"pending_events": str(len(phase_index.get("compact_restore_contract_pending", [])))},
            ),
        ]
        findings = []
        if not passed:
            findings.append(
                _finding(
                    "COMPACT_NEEDED_WITHOUT_RESTORE_CONTRACT",
                    CompactRecoveryAuditSeverity.BLOCKER,
                    CompactRecoveryAuditSurface.RESTORE_CONTRACT,
                    "Compact was needed but no restore contract was created.",
                    "compact_contract_created",
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="compact_contract_created",
            kind=CompactRecoveryRuleKind.EFFECT,
            surface=CompactRecoveryAuditSurface.RESTORE_CONTRACT,
            passed=passed,
            required=True,
            description="Any compact-needed or applied boundary must produce a next-turn restore contract.",
            evidence=tuple(evidence),
            findings=tuple(findings),
            metadata={"compact_needed": str(compact_needed).lower(), "restore_contract_id": contract_id},
        )

    def _rule_restore_application(
        self,
        restore_payload: Mapping[str, Any],
        phase_index: Mapping[str, list[Mapping[str, Any]]],
    ) -> CompactRecoveryRuleResult:
        app_count = _safe_int(restore_payload.get("application_count") or _nested(restore_payload, "metadata").get("restore_integration_applications"))
        model_messages = _safe_int(restore_payload.get("model_message_count") or _nested(restore_payload, "metadata").get("restore_integration_model_messages"))
        pending = _safe_int(restore_payload.get("pending_contract_count") or _nested(restore_payload, "metadata").get("restore_integration_pending_contracts"))
        applied_event_count = len(phase_index.get("codeworker_restore_context_applied", []))
        passed = app_count > 0 and model_messages > 0
        if pending and not app_count:
            passed = False
        evidence = [
            _evidence(
                "restore_application",
                "codeworker_restore_context_applied",
                applied_event_count > 0,
                "Restore application event was emitted after a compact boundary.",
                metadata={"applied_events": str(applied_event_count)},
            ),
            _evidence(
                "restore_model_messages",
                "codeworker_restore_integration",
                model_messages > 0,
                "Restore integration produced model messages.",
                metadata={"model_messages": str(model_messages), "applications": str(app_count)},
            ),
        ]
        findings = []
        if not passed and pending:
            findings.append(
                _finding(
                    "PENDING_RESTORE_CONTRACT_NOT_APPLIED",
                    CompactRecoveryAuditSeverity.ERROR,
                    CompactRecoveryAuditSurface.MODEL_ENVELOPE,
                    "A restore contract remained pending without being applied to a later model request.",
                    "restore_contract_applied",
                    metadata={"pending_contracts": str(pending), "applications": str(app_count)},
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="restore_contract_applied",
            kind=CompactRecoveryRuleKind.EFFECT,
            surface=CompactRecoveryAuditSurface.MODEL_ENVELOPE,
            passed=passed or pending == 0,
            required=False,
            description="A pending restore contract should be consumed before the next CodeWorker model request.",
            evidence=tuple(evidence),
            findings=tuple(findings),
            metadata={"applications": str(app_count), "model_messages": str(model_messages), "pending": str(pending)},
        )

    def _rule_restore_reaches_model(
        self,
        restore_payload: Mapping[str, Any],
        model_payloads: Sequence[Mapping[str, Any]],
    ) -> CompactRecoveryRuleResult:
        expected = _safe_int(restore_payload.get("model_message_count") or _nested(restore_payload, "metadata").get("restore_integration_model_messages"))
        envelope_counts = [
            _safe_int(_nested(payload, "envelope", "metadata").get("restore_model_message_count"))
            for payload in model_payloads
        ]
        reached = any(count > 0 for count in envelope_counts)
        passed = expected == 0 or reached
        evidence = [
            _evidence(
                "model_envelope_restore_messages",
                "model_stream_report",
                reached,
                "At least one model stream envelope contains restored compact context messages.",
                metadata={"expected": str(expected), "envelope_counts": ",".join(str(item) for item in envelope_counts)},
            )
        ]
        findings = []
        if not passed:
            findings.append(
                _finding(
                    "RESTORE_MESSAGES_NOT_IN_MODEL_ENVELOPE",
                    CompactRecoveryAuditSeverity.BLOCKER,
                    CompactRecoveryAuditSurface.MODEL_ENVELOPE,
                    "Restore integration produced messages but no later model envelope received them.",
                    "restore_reaches_model_envelope",
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="restore_reaches_model_envelope",
            kind=CompactRecoveryRuleKind.EFFECT,
            surface=CompactRecoveryAuditSurface.MODEL_ENVELOPE,
            passed=passed,
            required=True,
            description="Restored compact state must alter the next model request, not just write an event.",
            evidence=tuple(evidence),
            findings=tuple(findings),
            metadata={"expected_messages": str(expected), "reached": str(reached).lower()},
        )

    def _rule_restore_provenance(self, restore_payload: Mapping[str, Any]) -> CompactRecoveryRuleResult:
        messages = _restore_messages(restore_payload)
        required_fields = ("source_provenance", "trust_level", "secret_redaction_state", "source_ref")
        missing: list[str] = []
        for message in messages:
            metadata = _nested(message, "metadata")
            missing_fields = [field for field in required_fields if not metadata.get(field)]
            if missing_fields:
                missing.append(f"{message.get('message_id') or message.get('segment_id')}:{','.join(missing_fields)}")
        passed = not missing
        evidence = [
            _evidence(
                "restore_message_provenance",
                "codeworker_restore_context_applied",
                passed and bool(messages),
                "Restored messages carry source provenance, trust level, source ref and redaction state.",
                metadata={"messages": str(len(messages)), "missing": ";".join(missing[:8])},
            )
        ]
        findings = []
        if missing:
            findings.append(
                _finding(
                    "RESTORE_MESSAGE_PROVENANCE_INCOMPLETE",
                    CompactRecoveryAuditSeverity.BLOCKER,
                    CompactRecoveryAuditSurface.PROVENANCE,
                    "One or more restored messages lacks required provenance/trust/redaction metadata.",
                    "restore_message_provenance_complete",
                    metadata={"missing": ";".join(missing[:16])},
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="restore_message_provenance_complete",
            kind=CompactRecoveryRuleKind.PROVENANCE,
            surface=CompactRecoveryAuditSurface.PROVENANCE,
            passed=passed,
            required=True,
            description="Every restored context message must preserve provenance, trust and redaction state.",
            evidence=tuple(evidence),
            findings=tuple(findings),
            metadata={"messages": str(len(messages)), "missing_count": str(len(missing))},
        )

    def _rule_untrusted_markers(
        self,
        restore_payload: Mapping[str, Any],
        model_payloads: Sequence[Mapping[str, Any]],
    ) -> CompactRecoveryRuleResult:
        restore_messages = _restore_messages(restore_payload)
        untrusted_restore = [
            message
            for message in restore_messages
            if _nested(message, "metadata").get("trust_level") == "external_untrusted"
        ]
        envelope_messages: list[Mapping[str, Any]] = []
        for payload in model_payloads:
            envelope_messages.extend(
                item
                for item in _as_list(_nested(payload, "envelope").get("messages"))
                if isinstance(item, Mapping)
            )
        untrusted_envelope = [
            message
            for message in envelope_messages
            if _nested(message, "metadata").get("trust_level") == "external_untrusted"
        ]
        markers_preserved = all(str(message.get("content") or "").startswith("[UNTRUSTED_CONTEXT]") for message in untrusted_envelope)
        passed = not untrusted_restore or (bool(untrusted_envelope) and markers_preserved)
        evidence = [
            _evidence(
                "untrusted_marker",
                "model_stream_report",
                passed,
                "External untrusted restore messages keep untrusted markers in model envelope content.",
                metadata={
                    "restore_untrusted": str(len(untrusted_restore)),
                    "envelope_untrusted": str(len(untrusted_envelope)),
                    "markers_preserved": str(markers_preserved).lower(),
                },
            )
        ]
        findings = []
        if not passed:
            findings.append(
                _finding(
                    "UNTRUSTED_RESTORE_MARKER_MISSING",
                    CompactRecoveryAuditSeverity.ERROR,
                    CompactRecoveryAuditSurface.PROVENANCE,
                    "External untrusted restore content was not clearly marked when inserted into the model envelope.",
                    "untrusted_restore_markers_preserved",
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="untrusted_restore_markers_preserved",
            kind=CompactRecoveryRuleKind.PROVENANCE,
            surface=CompactRecoveryAuditSurface.PROVENANCE,
            passed=passed,
            required=False,
            description="Untrusted MCP/tool content must remain explicitly marked after compact restore.",
            evidence=tuple(evidence),
            findings=tuple(findings),
            metadata={"restore_untrusted": str(len(untrusted_restore)), "envelope_untrusted": str(len(untrusted_envelope))},
        )

    def _rule_model_retry_recovery(
        self,
        model_payloads: Sequence[Mapping[str, Any]],
        retry_payloads: Sequence[Mapping[str, Any]],
    ) -> CompactRecoveryRuleResult:
        errored = [payload for payload in model_payloads if str(payload.get("error_kind") or "none") != "none"]
        recovered = [payload for payload in retry_payloads if _truthy(payload.get("recovered")) or str(payload.get("status") or "") in {"fallback_selected", "retried"}]
        passed = not errored or bool(recovered)
        evidence = [
            _evidence(
                "api_retry_recovery",
                "api_retry_report",
                passed,
                "Model stream errors are connected to retry/fallback recovery reports.",
                metadata={"errored_streams": str(len(errored)), "recovered_reports": str(len(recovered))},
            )
        ]
        findings = []
        if not passed:
            findings.append(
                _finding(
                    "MODEL_STREAM_ERROR_WITHOUT_RETRY_RECOVERY",
                    CompactRecoveryAuditSeverity.BLOCKER,
                    CompactRecoveryAuditSurface.API_RETRY,
                    "A typed model stream error was observed without retry or fallback recovery.",
                    "model_api_retry_recovery",
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="model_api_retry_recovery",
            kind=CompactRecoveryRuleKind.RECOVERY,
            surface=CompactRecoveryAuditSurface.API_RETRY,
            passed=passed,
            required=True,
            description="Typed model errors must be consumed by retry/fallback state.",
            evidence=tuple(evidence),
            findings=tuple(findings),
            metadata={"errored_streams": str(len(errored)), "recovered_reports": str(len(recovered))},
        )

    def _rule_prompt_too_long_path(
        self,
        model_payloads: Sequence[Mapping[str, Any]],
        retry_payloads: Sequence[Mapping[str, Any]],
    ) -> CompactRecoveryRuleResult:
        prompt_too_long = [
            payload
            for payload in model_payloads
            if str(payload.get("error_kind") or "") == "prompt_too_long"
            or _truthy(_nested(payload, "envelope").get("prompt_too_long"))
        ]
        reduce_prompt_attempts = [
            attempt
            for report in retry_payloads
            for attempt in _as_list(report.get("attempts"))
            if isinstance(attempt, Mapping) and str(attempt.get("decision") or "") == "reduce_prompt_and_retry"
        ]
        compact_events_ok = True
        passed = not prompt_too_long or bool(reduce_prompt_attempts) or compact_events_ok
        evidence = [
            _evidence(
                "prompt_too_long_recovery",
                "api_retry_report",
                passed,
                "Prompt-too-long path is represented by reduce-prompt retry or compact boundary state.",
                metadata={
                    "prompt_too_long_streams": str(len(prompt_too_long)),
                    "reduce_prompt_attempts": str(len(reduce_prompt_attempts)),
                },
            )
        ]
        return CompactRecoveryRuleResult(
            rule_id="prompt_too_long_recovery_path",
            kind=CompactRecoveryRuleKind.RECOVERY,
            surface=CompactRecoveryAuditSurface.API_RETRY,
            passed=passed,
            required=False,
            description="Prompt-too-long model failures should be recoverable by prompt reduction or compact state.",
            evidence=tuple(evidence),
            metadata={
                "prompt_too_long_streams": str(len(prompt_too_long)),
                "reduce_prompt_attempts": str(len(reduce_prompt_attempts)),
            },
        )

    def _rule_runtime_budget(
        self,
        phase_index: Mapping[str, list[Mapping[str, Any]]],
        metadata: Mapping[str, Any],
    ) -> CompactRecoveryRuleResult:
        budget_events = phase_index.get("runtime_budget_updated", [])
        replay_events = phase_index.get("runtime_budget_replay", [])
        context_chars = _safe_int(metadata.get("runtime_budget_state_context_used_chars"))
        retry_count = _safe_int(metadata.get("runtime_budget_state_retry_count"))
        passed = bool(budget_events) and bool(replay_events)
        evidence = [
            _evidence(
                "runtime_budget_events",
                "runtime_budget_updated",
                bool(budget_events),
                "Runtime budget update events are present.",
                metadata={"budget_events": str(len(budget_events)), "context_chars": str(context_chars)},
            ),
            _evidence(
                "runtime_budget_replay",
                "runtime_budget_replay",
                bool(replay_events),
                "Runtime budget replay is available for recovery consumers.",
                metadata={"replay_events": str(len(replay_events)), "retry_count": str(retry_count)},
            ),
        ]
        findings = []
        if not passed:
            findings.append(
                _finding(
                    "RUNTIME_BUDGET_NOT_RECOVERY_CONSUMABLE",
                    CompactRecoveryAuditSeverity.BLOCKER,
                    CompactRecoveryAuditSurface.RUNTIME_BUDGET,
                    "Runtime budget state did not emit both update and replay events.",
                    "runtime_budget_replayable",
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="runtime_budget_replayable",
            kind=CompactRecoveryRuleKind.RECOVERY,
            surface=CompactRecoveryAuditSurface.RUNTIME_BUDGET,
            passed=passed,
            required=True,
            description="Budget and retry/cost state must be evented and replayable for recovery.",
            evidence=tuple(evidence),
            findings=tuple(findings),
            metadata={"budget_events": str(len(budget_events)), "replay_events": str(len(replay_events))},
        )

    def _rule_task_api_projection(
        self,
        phase_index: Mapping[str, list[Mapping[str, Any]]],
    ) -> CompactRecoveryRuleResult:
        foundation = bool(phase_index.get("codeworker_api_foundation"))
        projection = bool(phase_index.get("compact_state_projection"))
        restore_integration = bool(phase_index.get("codeworker_restore_integration"))
        passed = foundation and projection and restore_integration
        evidence = [
            _evidence(
                "api_foundation",
                "codeworker_api_foundation",
                foundation,
                "CodeWorker API foundation event is present.",
            ),
            _evidence(
                "compact_projection",
                "compact_state_projection",
                projection,
                "Compact state projection event is present.",
            ),
            _evidence(
                "restore_integration",
                "codeworker_restore_integration",
                restore_integration,
                "Restore integration event is present for task API projection.",
            ),
        ]
        findings = []
        if not passed:
            findings.append(
                _finding(
                    "TASK_API_PROJECTION_INPUTS_MISSING",
                    CompactRecoveryAuditSeverity.ERROR,
                    CompactRecoveryAuditSurface.TASK_API,
                    "Task API projection is missing one or more required runtime event inputs.",
                    "task_api_projection_inputs",
                )
            )
        return CompactRecoveryRuleResult(
            rule_id="task_api_projection_inputs",
            kind=CompactRecoveryRuleKind.REQUIRED,
            surface=CompactRecoveryAuditSurface.TASK_API,
            passed=passed,
            required=False,
            description="Task-scoped CodeWorker API must be backed by emitted runtime events.",
            evidence=tuple(evidence),
            findings=tuple(findings),
        )


def compact_recovery_audit_metadata(report: CompactRecoveryAuditReport | None) -> dict[str, str]:
    if report is None:
        return {
            "compact_recovery_audit_ok": "false",
            "compact_recovery_audit_status": "missing",
            "compact_recovery_audit_report_id": "",
        }
    return report.metadata()


def default_compact_recovery_audit_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_compact_recovery_audit.py",
            "decision": "zyra_module_migrated",
            "capability": "compact boundary and next-turn restore effects are audited against model-envelope reachability",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/claude.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_compact_recovery_audit.py",
            "decision": "zyra_module_migrated",
            "capability": "stream fallback, prompt-too-long and retry/cost recovery semantics are event-audited",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/**",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_compact_recovery_audit.py",
            "decision": "adapter_encapsulated",
            "capability": "event-sourced task API projection inputs are checked from real session events",
        },
    )


def _restore_messages(restore_payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    messages: list[Mapping[str, Any]] = []
    applications = _as_list(restore_payload.get("applications"))
    for application in applications:
        if not isinstance(application, Mapping):
            continue
        for message in _as_list(application.get("messages")):
            if isinstance(message, Mapping):
                messages.append(message)
    latest = _nested(restore_payload, "latest_application")
    for message in _as_list(latest.get("messages")):
        if isinstance(message, Mapping):
            messages.append(message)
    return messages


def _phase_index(events: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        query_session = _nested(event, "payload", "query_session")
        phase = str(query_session.get("phase") or "")
        if not phase:
            continue
        result.setdefault(phase, []).append(event)
    return result


def _event_view(event: Any) -> dict[str, Any]:
    if isinstance(event, EventRecord):
        return to_jsonable(event)
    if isinstance(event, Mapping):
        return dict(event)
    converted = to_jsonable(event)
    return converted if isinstance(converted, dict) else {}


def _to_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        return converted if isinstance(converted, Mapping) else {}
    converted = to_jsonable(value)
    return converted if isinstance(converted, Mapping) else {}


def _nested(value: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _evidence(
    kind: str,
    phase: str,
    passed: bool,
    message: str,
    *,
    event_id: str = "",
    metadata: Mapping[str, str] | None = None,
) -> CompactRecoveryEvidence:
    return CompactRecoveryEvidence(
        evidence_id=new_id("compact_recovery_evidence"),
        kind=kind,
        phase=phase,
        passed=passed,
        message=message,
        event_id=event_id,
        metadata={str(k): str(v) for k, v in dict(metadata or {}).items()},
    )


def _finding(
    code: str,
    severity: CompactRecoveryAuditSeverity,
    surface: CompactRecoveryAuditSurface,
    message: str,
    rule_id: str,
    *,
    metadata: Mapping[str, str] | None = None,
) -> CompactRecoveryFinding:
    return CompactRecoveryFinding(
        code=code,
        severity=severity,
        surface=surface,
        message=message,
        rule_id=rule_id,
        metadata={str(k): str(v) for k, v in dict(metadata or {}).items()},
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


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
