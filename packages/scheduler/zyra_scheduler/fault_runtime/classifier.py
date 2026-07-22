from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import (
    FaultDisposition,
    FaultKind,
    FaultSeverity,
    FaultSignal,
    ObservationCategory,
    SignalOrigin,
    StructuredObservation,
)
from .errors import FaultRuntimeError, FaultRuntimeErrorCode


Predicate = Callable[[StructuredObservation], bool]


@dataclass(frozen=True, slots=True)
class ClassificationRule:
    rule_id: str
    category: ObservationCategory
    kind: FaultKind
    severity: FaultSeverity
    disposition: FaultDisposition
    retryable: bool
    terminal: bool
    summary: str
    predicate: Predicate
    required_refs: tuple[str, ...] = ()

    def matches(self, observation: StructuredObservation) -> bool:
        return observation.category is self.category and self.predicate(observation)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    signal: FaultSignal | None
    ignored: bool
    reason: str
    matched_rule_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": None if self.signal is None else self.signal.to_dict(),
            "ignored": self.ignored,
            "reason": self.reason,
            "matched_rule_id": self.matched_rule_id,
        }


def _code(*values: str) -> Predicate:
    allowed = frozenset(values)
    return lambda observation: observation.code in allowed


def _status(*values: str) -> Predicate:
    allowed = frozenset(values)
    return lambda observation: observation.status in allowed


def _code_or_status(codes: Sequence[str], statuses: Sequence[str]) -> Predicate:
    allowed_codes = frozenset(codes)
    allowed_statuses = frozenset(statuses)
    return lambda observation: observation.code in allowed_codes or observation.status in allowed_statuses


def _provider_rate_limit(observation: StructuredObservation) -> bool:
    return observation.status_code == 429 or observation.code in {
        "rate_limit_exceeded",
        "too_many_requests",
        "model_capacity_exhausted",
    }


def _provider_quota(observation: StructuredObservation) -> bool:
    return observation.code in {
        "quota_exhausted",
        "usage_limit_reached",
        "insufficient_quota",
        "credits_exhausted",
    }


def _provider_failure(observation: StructuredObservation) -> bool:
    return (
        observation.status_code in {408, 500, 502, 503, 504, 529}
        or observation.code
        in {
            "provider_error",
            "provider_timeout",
            "incomplete_stream",
            "empty_body",
            "malformed_envelope",
            "connection_closed",
            "transport_error",
            "retry_exhausted",
            "api_retry_exhausted",
        }
    )


DEFAULT_CLASSIFICATION_RULES: tuple[ClassificationRule, ...] = (
    ClassificationRule(
        "tool.deadline.expired.v1",
        ObservationCategory.TOOL,
        FaultKind.TOOL_TIMEOUT,
        FaultSeverity.ERROR,
        FaultDisposition.RECOVER,
        True,
        True,
        "A tool call exceeded its deterministic deadline.",
        _code_or_status(("deadline_expired", "tool_timeout", "request_timeout"), ("timed_out", "timeout")),
        ("tool_call_id",),
    ),
    ClassificationRule(
        "worker.lease-or-heartbeat-lost.v1",
        ObservationCategory.WORKER,
        FaultKind.WORKER_UNAVAILABLE,
        FaultSeverity.CRITICAL,
        FaultDisposition.RECOVER,
        True,
        True,
        "A bound worker became unavailable.",
        _code_or_status(("worker_lost", "lease_lost", "heartbeat_lost", "worker_unavailable"), ("lost", "unavailable")),
        ("worker_id",),
    ),
    ClassificationRule(
        "subagent.lifecycle-failed.v1",
        ObservationCategory.WORKER,
        FaultKind.SUBAGENT_FAILED,
        FaultSeverity.ERROR,
        FaultDisposition.RECOVER,
        True,
        True,
        "A logical subagent task failed before a committed terminal result.",
        _code_or_status(("subagent_failed", "background_task_failed", "child_task_failed"), ("failed",)),
        ("subagent_task_id",),
    ),
    ClassificationRule(
        "browser.process-exit.v1",
        ObservationCategory.BROWSER,
        FaultKind.BROWSER_CRASH,
        FaultSeverity.CRITICAL,
        FaultDisposition.RECOVER,
        True,
        True,
        "The observed browser process exited unexpectedly.",
        _code("process_exited", "browser_process_crashed", "target_crashed"),
        ("browser_session_id",),
    ),
    ClassificationRule(
        "browser.cdp-disconnect.v1",
        ObservationCategory.BROWSER,
        FaultKind.BROWSER_DISCONNECT,
        FaultSeverity.ERROR,
        FaultDisposition.RECOVER,
        True,
        True,
        "The browser CDP transport disconnected unexpectedly.",
        _code("cdp_disconnected", "heartbeat_late", "request_timeout"),
        ("browser_session_id",),
    ),
    ClassificationRule(
        "permission.denied.v1",
        ObservationCategory.PERMISSION,
        FaultKind.PERMISSION_DENIED,
        FaultSeverity.ERROR,
        FaultDisposition.BLOCK,
        False,
        True,
        "A canonical permission decision denied the operation.",
        _code_or_status(("permission_denied", "policy_denied", "sealed_denial"), ("denied", "blocked")),
    ),
    ClassificationRule(
        "provider.quota.v1",
        ObservationCategory.PROVIDER,
        FaultKind.MODEL_QUOTA_EXHAUSTED,
        FaultSeverity.ERROR,
        FaultDisposition.RECOVER,
        True,
        True,
        "The selected provider credential exhausted its usable quota.",
        _provider_quota,
        ("provider_id",),
    ),
    ClassificationRule(
        "provider.rate-limit.v1",
        ObservationCategory.PROVIDER,
        FaultKind.MODEL_RATE_LIMIT,
        FaultSeverity.WARNING,
        FaultDisposition.DEGRADE,
        True,
        False,
        "The selected provider reported a transient rate or capacity limit.",
        _provider_rate_limit,
        ("provider_id",),
    ),
    ClassificationRule(
        "provider.failure.v1",
        ObservationCategory.PROVIDER,
        FaultKind.MODEL_FAILURE,
        FaultSeverity.ERROR,
        FaultDisposition.RECOVER,
        True,
        True,
        "The selected model provider failed before a safe terminal response.",
        _provider_failure,
        ("provider_id",),
    ),
    ClassificationRule(
        "schema.validation-failure.v1",
        ObservationCategory.SCHEMA,
        FaultKind.SCHEMA_FAILURE,
        FaultSeverity.ERROR,
        FaultDisposition.RECOVER,
        True,
        True,
        "A structured runtime boundary rejected invalid schema output.",
        _code_or_status(("schema_invalid", "validation_failed", "decode_failed", "cardinality_mismatch"), ("invalid", "failed")),
    ),
    ClassificationRule(
        "workspace.integrity-failure.v1",
        ObservationCategory.WORKSPACE,
        FaultKind.WORKSPACE_CORRUPT,
        FaultSeverity.CRITICAL,
        FaultDisposition.BLOCK,
        False,
        True,
        "The bound workspace failed an integrity or custody check.",
        _code_or_status(("workspace_corrupt", "attestation_failed", "path_escape", "transaction_conflict"), ("corrupt", "invalid")),
        ("workspace_id",),
    ),
    ClassificationRule(
        "mcp.transport-disconnected.v1",
        ObservationCategory.MCP,
        FaultKind.MCP_DISCONNECTED,
        FaultSeverity.ERROR,
        FaultDisposition.RECOVER,
        True,
        True,
        "The MCP transport closed while its server was bound to the run.",
        _code_or_status(("transport_closed", "request_timeout", "reconnect_breaker_open", "server_exited"), ("disconnected", "unavailable")),
        ("mcp_server_id",),
    ),
    ClassificationRule(
        "process.unexpected-exit.v1",
        ObservationCategory.PROCESS,
        FaultKind.PROCESS_EXITED,
        FaultSeverity.ERROR,
        FaultDisposition.RECOVER,
        True,
        True,
        "A supervised runtime process exited unexpectedly.",
        _code_or_status(("process_exited", "process_crashed", "pipe_closed"), ("exited", "failed")),
    ),
)


class WatchdogSignalClassifier:
    """Deterministic structured classifier.

    Rules may inspect category, code, status, numeric status and non-identity
    details.  Worker/backend/provider/workspace/tool/session identities are
    copied only from ``CorrelationRefs`` and are never inferred from summary,
    exception text or other free-form strings.
    """

    def __init__(self, rules: Sequence[ClassificationRule] | None = None) -> None:
        self.rules = tuple(rules or DEFAULT_CLASSIFICATION_RULES)
        ids = [item.rule_id for item in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("classification rule ids must be unique")

    def classify(
        self,
        observation: StructuredObservation,
        *,
        origin: SignalOrigin | None = None,
    ) -> ClassificationResult:
        if observation.category is ObservationCategory.REQUIREMENT_CHANGE:
            return ClassificationResult(
                signal=None,
                ignored=True,
                reason="RequirementChanged is a 03D/05C control fact, not a fault.",
            )
        for rule in self.rules:
            if not rule.matches(observation):
                continue
            missing = self._missing_refs(observation, rule.required_refs)
            if missing:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.CLASSIFICATION_REJECTED,
                    (
                        f"structured observation lacks required refs for {rule.rule_id}: "
                        + ", ".join(missing)
                    ),
                    details={
                        "rule_id": rule.rule_id,
                        "missing_refs": list(missing),
                        "observation_id": observation.observation_id,
                        "critical_ref_source": "structured_refs_only",
                    },
                )
            selected_origin = origin or (
                SignalOrigin.INJECTION
                if observation.provenance.injection_id
                else SignalOrigin.OBSERVER
            )
            retryable = (
                observation.retryable_hint
                if observation.retryable_hint is not None
                else rule.retryable
            )
            terminal = (
                observation.terminal_hint
                if observation.terminal_hint is not None
                else rule.terminal
            )
            signal = FaultSignal(
                kind=rule.kind,
                severity=rule.severity,
                disposition=rule.disposition,
                refs=observation.refs,
                provenance=observation.provenance,
                summary=rule.summary,
                origin=selected_origin,
                retryable=retryable,
                terminal=terminal,
                classification_rule=rule.rule_id,
                observed_code=observation.code,
                evidence_event_ids=observation.evidence_event_ids,
                details={
                    "observation_status": observation.status,
                    "observation_error_type": observation.error_type,
                    "status_code": observation.status_code,
                    "elapsed_ms": observation.elapsed_ms,
                    "deadline_ms": observation.deadline_ms,
                    "observation_details": dict(observation.details),
                    "critical_ref_source": "structured_refs_only",
                },
            )
            return ClassificationResult(
                signal=signal,
                ignored=False,
                reason="structured rule matched",
                matched_rule_id=rule.rule_id,
            )
        return ClassificationResult(
            signal=None,
            ignored=True,
            reason="no structured fault rule matched",
        )

    def explain(self, observation: StructuredObservation) -> dict[str, Any]:
        candidates = []
        for rule in self.rules:
            if rule.category is not observation.category:
                continue
            matched = rule.predicate(observation)
            candidates.append(
                {
                    "rule_id": rule.rule_id,
                    "matched": matched,
                    "missing_refs": list(self._missing_refs(observation, rule.required_refs)) if matched else [],
                }
            )
        return {
            "observation_id": observation.observation_id,
            "category": observation.category.value,
            "code": observation.code,
            "candidates": candidates,
            "summary_inspected_for_identity": False,
            "critical_ref_source": "structured_refs_only",
        }

    @staticmethod
    def _missing_refs(
        observation: StructuredObservation,
        required: Sequence[str],
    ) -> tuple[str, ...]:
        refs = observation.refs
        missing: list[str] = []
        for name in required:
            if not hasattr(refs, name):
                raise ValueError(f"unknown correlation ref: {name}")
            if not str(getattr(refs, name) or "").strip():
                missing.append(name)
        return tuple(missing)


def default_classifier_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.watchdog-classifier-contract/v1",
        "owner": "WatchdogSignalClassifier",
        "rules": [
            {
                "rule_id": item.rule_id,
                "category": item.category.value,
                "fault_kind": item.kind.value,
                "severity": item.severity.value,
                "disposition": item.disposition.value,
                "required_refs": list(item.required_refs),
            }
            for item in DEFAULT_CLASSIFICATION_RULES
        ],
        "critical_ref_source": "structured_refs_only",
        "free_text_identity_inference": False,
        "requirement_change_is_fault": False,
    }
