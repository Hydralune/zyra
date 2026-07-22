from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import (
    RecoveryAction,
    RecoveryRefs,
    RecoverySignal,
    RecoverySignalKind,
    RecoverySource,
    recovery_id,
)


class RecoverySignalClassificationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClassificationRule:
    source: RecoverySource
    source_kind: str
    signal_kind: RecoverySignalKind
    reason_code: str
    retryable: bool
    terminal: bool
    suggested_actions: tuple[RecoveryAction, ...]
    require_any_refs: tuple[str, ...] = ()
    auth_required: bool = False
    permission_effect: str = ""
    severity: str = "warning"


class RecoverySignalClassifier:
    """Normalize typed owner receipts into one recovery signal taxonomy.

    Classification is intentionally based on explicit source/kind/status fields.
    Human-readable summaries are copied only after a rule matches and are never
    searched for identity or policy keywords.
    """

    POLICY_REVISION = 1

    def __init__(self) -> None:
        self._rules = self._build_rules()

    def classify(self, payload: Mapping[str, Any], *, source: RecoverySource | str | None = None) -> RecoverySignal:
        value = self._mapping(payload)
        normalized_source = self._source(value, source)
        source_kind = self._source_kind(value)
        rule = self._rules.get((normalized_source, source_kind))
        if rule is None:
            rule = self._generic_rule(normalized_source, source_kind, value)
        refs = self._refs(value)
        self._validate_required_refs(rule, refs)
        details = self._details(value)
        partial_output = self._boolean(value, "partial_output", nested=("stream", "partial_output"))
        observable_side_effect = self._boolean(
            value,
            "observable_side_effect",
            nested=("side_effect", "committed"),
        ) or bool(refs.tool_call_id and self._boolean(value, "tool_effect_committed"))
        if source_kind in {
            "stream_interrupted_after_output", "partial_stream", "stream_interrupted_after_content",
        }:
            partial_output = True
        if observable_side_effect:
            details["replay_fence_required"] = True
        retryable = bool(value.get("retryable", rule.retryable))
        terminal = bool(value.get("terminal", rule.terminal))
        suggested = self._actions(value.get("suggested_actions")) or rule.suggested_actions
        reason_code = str(value.get("reason_code") or rule.reason_code)
        summary = self._summary(value, rule)
        attempt_count = self._non_negative(value.get("attempt_count") or value.get("retry_attempt") or 0, "attempt_count")
        retry_after_ms = self._non_negative(value.get("retry_after_ms") or 0, "retry_after_ms")
        signal_id = str(value.get("signal_id") or value.get("source_signal_id") or recovery_id("recoverysig"))
        evidence = tuple(str(item) for item in value.get("evidence_event_ids") or () if str(item))
        return RecoverySignal(
            signal_id=signal_id,
            kind=rule.signal_kind,
            source=normalized_source,
            refs=refs,
            reason_code=reason_code,
            summary=summary,
            recoverable=bool(value.get("recoverable", not terminal)),
            retryable=retryable,
            terminal=terminal,
            partial_output=partial_output,
            observable_side_effect=observable_side_effect,
            auth_required=bool(value.get("auth_required", rule.auth_required)),
            permission_effect=str(value.get("permission_effect") or rule.permission_effect),
            severity=str(value.get("severity") or rule.severity),
            causation_id=str(value.get("causation_id") or value.get("handoff_id") or ""),
            correlation_id=str(value.get("correlation_id") or refs.request_id or refs.turn_id or ""),
            observed_at=str(value.get("observed_at") or value.get("created_at") or "") or self._now(),
            attempt_count=attempt_count,
            retry_after_ms=retry_after_ms,
            evidence_event_ids=evidence,
            suggested_actions=suggested,
            details={
                **details,
                "source_kind": source_kind,
                "classification_rule": f"recovery.signal.v{self.POLICY_REVISION}:{normalized_source.value}:{source_kind}",
                "typed_classification": True,
                "free_text_identity_inference": False,
            },
        )

    def from_fault_handoff(self, handoff: Mapping[str, Any]) -> RecoverySignal:
        value = self._mapping(handoff)
        fault_kind = str(value.get("fault_kind") or "").strip()
        mapping = {
            "tool_timeout": "tool_timeout",
            "tool_error": "tool_error",
            "mcp_auth_required": "mcp_auth_required",
            "mcp_disconnect": "mcp_disconnected",
            "provider_timeout": "stream_stall",
            "provider_transport": "provider_unavailable",
            "provider_rate_limit": "rate_limited",
            "worker_heartbeat_stale": "worker_heartbeat_stale",
            "worker_lost": "worker_lost",
            # The watchdog/fault runtime canonicalizes the public
            # ``worker_lost`` injection kind to its containment-domain
            # ``worker_unavailable`` fault kind.  Preserve the typed handoff
            # across that domain boundary instead of degrading it to an
            # unknown failure that requires an unsafe allow_unknown escape.
            "worker_unavailable": "worker_lost",
            "backend_unavailable": "backend_unavailable",
            "subagent_failed": "subagent_failed",
            "workspace_conflict": "workspace_conflict",
            "permission_denied": "permission_denied",
            "schema_mismatch": "tool_error",
        }
        source_kind = mapping.get(fault_kind)
        if source_kind is None:
            requested = tuple(str(item) for item in value.get("requested_actions") or ())
            if "switch_backend" in requested:
                source_kind = "backend_unavailable"
            elif "reroute" in requested:
                source_kind = "worker_lost"
            elif "authenticate" in requested or "authenticate_mcp" in requested:
                source_kind = "mcp_auth_required"
            elif "retry" in requested:
                source_kind = "tool_error"
            else:
                source_kind = "unknown_failure"
        payload = {
            **copy.deepcopy(value),
            "source": RecoverySource.WATCHDOG_HANDOFF.value,
            "kind": source_kind,
            "source_kind": source_kind,
            "refs": copy.deepcopy(dict(value.get("refs") or {})),
            "causation_id": str(value.get("handoff_id") or value.get("signal_id") or ""),
            "summary": str(value.get("reason") or "watchdog recovery handoff"),
            "details": {
                **copy.deepcopy(dict(value.get("metadata") or {})),
                "fault_kind": fault_kind,
                "fault_signal_id": str(value.get("signal_id") or ""),
                "injection_id": str(value.get("injection_id") or ""),
                "watchdog_handoff_id": str(value.get("handoff_id") or ""),
            },
        }
        return self.classify(payload)

    def from_worker_evidence(self, evidence: Mapping[str, Any]) -> RecoverySignal:
        value = self._mapping(evidence)
        kind = str(value.get("signal_kind") or "").strip()
        source_kind = {
            "heartbeat_stale": "worker_heartbeat_stale",
            "worker_lost": "worker_lost",
            "lease_expired": "worker_lease_expired",
            "lease_fenced": "worker_lease_expired",
            "route_unavailable": "backend_unavailable",
            "renewal_rejected": "worker_lease_expired",
            "checkpoint_diverged": "checkpoint_diverged",
            "cancel_pending": "subagent_cancelled",
            "drain_active": "worker_heartbeat_stale",
        }.get(kind, "unknown_failure")
        route_ref = self._mapping(value.get("route_ref") or {})
        graph_ref = self._mapping(value.get("graph_ref") or {})
        workspace_ref = self._mapping(value.get("workspace_ref") or {})
        refs = {
            "run_id": value.get("run_id"),
            "task_id": value.get("task_id"),
            "attempt_id": value.get("attempt_id"),
            "worker_lease_id": value.get("lease_id"),
            "worker_id": value.get("worker_id"),
            "backend_id": value.get("backend_id") or route_ref.get("state_id"),
            "workspace_id": workspace_ref.get("state_id"),
            "graph_id": graph_ref.get("state_id"),
            "graph_revision": graph_ref.get("revision") or 0,
        }
        return self.classify({
            **copy.deepcopy(value),
            "source": RecoverySource.WORKER_HANDOFF.value,
            "source_kind": source_kind,
            "refs": {name: item for name, item in refs.items() if item not in {None, ""}},
            "causation_id": str(value.get("evidence_id") or ""),
            "summary": str(value.get("reason") or "worker recovery evidence"),
            "details": {
                **copy.deepcopy(dict(value.get("metadata") or {})),
                "worker_signal_kind": kind,
                "recommended_disposition": str(value.get("recommended_disposition") or ""),
                "route_ref": route_ref,
                "graph_ref": graph_ref,
                "workspace_ref": workspace_ref,
            },
        })

    def from_worker_handoff(self, handoff: Mapping[str, Any]) -> tuple[RecoverySignal, ...]:
        value = self._mapping(handoff)
        schema = str(value.get("schema") or "")
        if schema and schema != "zyra.worker-recovery-handoff/v1":
            raise RecoverySignalClassificationError(f"unsupported worker handoff schema: {schema}")
        evidence = value.get("evidence") or ()
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes, bytearray)):
            raise RecoverySignalClassificationError("worker handoff evidence must be an array")
        return tuple(self.from_worker_evidence(self._mapping(item)) for item in evidence)

    def from_omp_receipt(self, receipt: Mapping[str, Any]) -> RecoverySignal:
        value = self._mapping(receipt)
        schema = str(value.get("schema") or "")
        if schema != "zyra.omp-recovery-receipt/v1":
            raise RecoverySignalClassificationError(f"unsupported OMP recovery receipt schema: {schema!r}")
        receipt_kind = str(value.get("kind") or value.get("signal_kind") or "")
        mapping = {
            "retry_exhausted": "api_retry_exhausted",
            "api_retry_exhausted": "api_retry_exhausted",
            "rate_limited": "rate_limited",
            "auth_rotation_exhausted": "credential_exhausted",
            "stream_stall": "stream_stall",
            "partial_stream": "stream_interrupted_after_output",
            "context_overflow": "prompt_too_long",
            "session_resume": "compact_needed",
            "session_fork_failed": "checkpoint_diverged",
            "task_failed": "subagent_failed",
            "task_cancelled": "subagent_cancelled",
            "worktree_merge_conflict": "workspace_conflict",
            "provider_unavailable": "provider_unavailable",
            "backend_unavailable": "backend_unavailable",
            "mcp_auth_required": "mcp_auth_required",
            "api_retry_exhausted": "api_retry_exhausted",
            "provider_rate_limit": "rate_limited",
            "provider_quota": "credential_exhausted",
            "worker_unavailable": "worker_lost",
            "tool_timeout": "tool_timeout",
            "mcp_disconnected": "mcp_disconnected",
            "process_exited": "worker_lost",
        }
        source_kind = mapping.get(receipt_kind)
        if source_kind is None:
            raise RecoverySignalClassificationError(f"unknown OMP recovery receipt kind: {receipt_kind!r}")
        return self.classify({
            **copy.deepcopy(value),
            "source": RecoverySource.OMP_SUPPLEMENT.value,
            "source_kind": source_kind,
            "kind": source_kind,
            "causation_id": str(value.get("receipt_id") or ""),
            "details": {
                **copy.deepcopy(dict(value.get("details") or {})),
                "omp_receipt_id": str(value.get("receipt_id") or ""),
                "omp_receipt_kind": receipt_kind,
                "supplementary_only": True,
                "decision_owner": "python.RecoveryDecisionRuntime",
            },
        })

    def classify_permission(self, payload: Mapping[str, Any]) -> RecoverySignal:
        value = self._mapping(payload)
        effect = str(value.get("effect") or value.get("status") or "").casefold()
        phase = str(value.get("phase") or "").casefold()
        if effect in {"ask", "pending"} or phase in {"created", "delivered"}:
            source_kind = "permission_ask" if effect == "ask" else "permission_pending"
        elif effect in {"deny", "denied", "rejected"}:
            source_kind = "permission_denied"
        else:
            raise RecoverySignalClassificationError("permission receipt is not ask/pending/denied")
        return self.classify({
            **copy.deepcopy(value),
            "source": RecoverySource.PERMISSION_RUNTIME.value,
            "source_kind": source_kind,
            "permission_effect": effect,
        })

    def classify_api_error(self, payload: Mapping[str, Any]) -> RecoverySignal:
        value = self._mapping(payload)
        error_kind = str(value.get("error_kind") or value.get("code") or "").casefold()
        source_kind = {
            "prompt_too_long": "prompt_too_long",
            "context_overflow": "prompt_too_long",
            "retry_exhausted": "api_retry_exhausted",
            "api_retry_exhausted": "api_retry_exhausted",
            "stream_stall": "stream_stall",
            "stream_idle_timeout": "stream_stall",
            "stream_interrupted_after_output": "stream_interrupted_after_output",
            "rate_limited": "rate_limited",
            "provider_unavailable": "provider_unavailable",
            "credential_exhausted": "credential_exhausted",
        }.get(error_kind)
        if source_kind is None:
            raise RecoverySignalClassificationError(f"unsupported API error kind: {error_kind!r}")
        return self.classify({
            **copy.deepcopy(value),
            "source": RecoverySource.API_RUNTIME.value,
            "source_kind": source_kind,
        })

    def classification_contract(self) -> dict[str, Any]:
        rows = []
        for (_, _), rule in sorted(self._rules.items(), key=lambda item: (item[0][0].value, item[0][1])):
            rows.append({
                "source": rule.source.value,
                "source_kind": rule.source_kind,
                "signal_kind": rule.signal_kind.value,
                "reason_code": rule.reason_code,
                "retryable": rule.retryable,
                "terminal": rule.terminal,
                "suggested_actions": [item.value for item in rule.suggested_actions],
                "required_ref_alternatives": list(rule.require_any_refs),
            })
        return {
            "schema": "zyra.recovery-signal-classifier-contract/v1",
            "policy_revision": self.POLICY_REVISION,
            "rules": rows,
            "identity_from_summary": False,
            "llm_classifier": False,
            "requirement_change_is_fault": False,
        }

    @staticmethod
    def _build_rules() -> dict[tuple[RecoverySource, str], ClassificationRule]:
        rules: list[ClassificationRule] = []

        def add(
            sources: Sequence[RecoverySource],
            source_kind: str,
            signal_kind: RecoverySignalKind,
            reason_code: str,
            actions: Sequence[RecoveryAction],
            *,
            retryable: bool = False,
            terminal: bool = False,
            refs: Sequence[str] = (),
            auth_required: bool = False,
            permission_effect: str = "",
            severity: str = "warning",
        ) -> None:
            for source in sources:
                rules.append(ClassificationRule(
                    source=source,
                    source_kind=source_kind,
                    signal_kind=signal_kind,
                    reason_code=reason_code,
                    retryable=retryable,
                    terminal=terminal,
                    suggested_actions=tuple(actions),
                    require_any_refs=tuple(refs),
                    auth_required=auth_required,
                    permission_effect=permission_effect,
                    severity=severity,
                ))

        add((RecoverySource.PERMISSION_RUNTIME,), "permission_ask", RecoverySignalKind.PERMISSION_ASK, "permission.ask", (RecoveryAction.ASK_PERMISSION,), refs=("tool_call_id", "request_id"), permission_effect="ask")
        add((RecoverySource.PERMISSION_RUNTIME,), "permission_pending", RecoverySignalKind.PERMISSION_PENDING, "permission.pending", (RecoveryAction.ASK_PERMISSION,), refs=("permission_request_id", "request_id", "tool_call_id"), permission_effect="pending")
        add((RecoverySource.PERMISSION_RUNTIME, RecoverySource.WATCHDOG_HANDOFF), "permission_denied", RecoverySignalKind.PERMISSION_DENIED, "permission.denied", (RecoveryAction.REPLAN, RecoveryAction.ABORT), refs=("tool_call_id", "request_id"), permission_effect="deny")
        add((RecoverySource.TOOL_RUNTIME, RecoverySource.WATCHDOG_HANDOFF), "tool_error", RecoverySignalKind.TOOL_ERROR, "tool.failed", (RecoveryAction.RETRY, RecoveryAction.REPLAN), retryable=True, refs=("tool_call_id", "node_id"))
        add((RecoverySource.TOOL_RUNTIME, RecoverySource.WATCHDOG_HANDOFF), "tool_timeout", RecoverySignalKind.TOOL_TIMEOUT, "tool.timeout", (RecoveryAction.RETRY, RecoveryAction.REROUTE), retryable=True, refs=("tool_call_id", "node_id"))
        add((RecoverySource.MCP_RUNTIME, RecoverySource.WATCHDOG_HANDOFF, RecoverySource.OMP_SUPPLEMENT), "mcp_auth_required", RecoverySignalKind.MCP_AUTH_REQUIRED, "mcp.auth_required", (RecoveryAction.AUTHENTICATE_MCP,), refs=("mcp_server_id", "request_id"), auth_required=True)
        add((RecoverySource.MCP_RUNTIME, RecoverySource.WATCHDOG_HANDOFF), "mcp_disconnected", RecoverySignalKind.MCP_DISCONNECTED, "mcp.disconnected", (RecoveryAction.RETRY, RecoveryAction.REPLAN), retryable=True, refs=("mcp_server_id", "request_id"))
        add((RecoverySource.API_RUNTIME, RecoverySource.PROVIDER_RUNTIME, RecoverySource.OMP_SUPPLEMENT), "api_retry_exhausted", RecoverySignalKind.API_RETRY_EXHAUSTED, "api.retry_exhausted", (RecoveryAction.DEGRADE_MODEL, RecoveryAction.SWITCH_PROVIDER, RecoveryAction.REPLAN), refs=("request_id", "turn_id"))
        add((RecoverySource.API_RUNTIME, RecoverySource.PROVIDER_RUNTIME, RecoverySource.OMP_SUPPLEMENT), "rate_limited", RecoverySignalKind.RATE_LIMITED, "provider.rate_limited", (RecoveryAction.RETRY, RecoveryAction.SWITCH_PROVIDER, RecoveryAction.DEGRADE_MODEL), retryable=True, refs=("request_id", "provider_id", "turn_id"))
        add((RecoverySource.API_RUNTIME, RecoverySource.PROVIDER_RUNTIME, RecoverySource.WATCHDOG_HANDOFF, RecoverySource.OMP_SUPPLEMENT), "stream_stall", RecoverySignalKind.STREAM_STALL, "stream.stalled", (RecoveryAction.RETRY, RecoveryAction.DEGRADE_MODEL, RecoveryAction.SWITCH_PROVIDER), retryable=True, refs=("request_id", "turn_id"))
        add((RecoverySource.API_RUNTIME, RecoverySource.PROVIDER_RUNTIME, RecoverySource.OMP_SUPPLEMENT), "stream_interrupted_after_output", RecoverySignalKind.STREAM_INTERRUPTED_AFTER_OUTPUT, "stream.partial_output", (RecoveryAction.RESUME_CHECKPOINT, RecoveryAction.REPLAN), refs=("request_id", "turn_id"), severity="error")
        add((RecoverySource.SESSION_RUNTIME, RecoverySource.COMPACT_RUNTIME, RecoverySource.API_RUNTIME, RecoverySource.OMP_SUPPLEMENT), "prompt_too_long", RecoverySignalKind.PROMPT_TOO_LONG, "context.prompt_too_long", (RecoveryAction.COMPACT, RecoveryAction.RESUME_CHECKPOINT), refs=("session_id", "turn_id"))
        add((RecoverySource.SESSION_RUNTIME, RecoverySource.COMPACT_RUNTIME, RecoverySource.OMP_SUPPLEMENT), "compact_needed", RecoverySignalKind.COMPACT_NEEDED, "context.compact_needed", (RecoveryAction.COMPACT, RecoveryAction.RESUME_CHECKPOINT), refs=("session_id", "turn_id"))
        add((RecoverySource.SUBAGENT_RUNTIME, RecoverySource.WATCHDOG_HANDOFF, RecoverySource.OMP_SUPPLEMENT), "subagent_failed", RecoverySignalKind.SUBAGENT_FAILED, "subagent.failed", (RecoveryAction.REROUTE, RecoveryAction.REPLAN), refs=("subagent_id", "worker_id", "attempt_id"), severity="error")
        add((RecoverySource.SUBAGENT_RUNTIME, RecoverySource.OMP_SUPPLEMENT), "subagent_cancelled", RecoverySignalKind.SUBAGENT_CANCELLED, "subagent.cancelled", (RecoveryAction.REPLAN, RecoveryAction.ABORT), refs=("subagent_id", "worker_id", "attempt_id"))
        add((RecoverySource.WORKER_HANDOFF, RecoverySource.WATCHDOG_HANDOFF), "worker_heartbeat_stale", RecoverySignalKind.WORKER_HEARTBEAT_STALE, "worker.heartbeat_stale", (RecoveryAction.REROUTE, RecoveryAction.RETRY), retryable=True, refs=("worker_id", "worker_lease_id"), severity="error")
        add((RecoverySource.WORKER_HANDOFF, RecoverySource.WATCHDOG_HANDOFF), "worker_lost", RecoverySignalKind.WORKER_LOST, "worker.lost", (RecoveryAction.REROUTE, RecoveryAction.REPLAN), refs=("worker_id", "worker_lease_id"), severity="critical")
        add((RecoverySource.WORKER_HANDOFF,), "worker_lease_expired", RecoverySignalKind.WORKER_LEASE_EXPIRED, "worker.lease_expired", (RecoveryAction.REROUTE,), refs=("worker_lease_id", "attempt_id"), severity="error")
        add((RecoverySource.BACKEND_RUNTIME, RecoverySource.WORKER_HANDOFF, RecoverySource.WATCHDOG_HANDOFF, RecoverySource.OMP_SUPPLEMENT), "backend_unavailable", RecoverySignalKind.BACKEND_UNAVAILABLE, "backend.unavailable", (RecoveryAction.SWITCH_BACKEND, RecoveryAction.REROUTE), refs=("backend_id", "backend_lease_id", "worker_id"), severity="error")
        add((RecoverySource.PROVIDER_RUNTIME, RecoverySource.API_RUNTIME, RecoverySource.OMP_SUPPLEMENT), "provider_unavailable", RecoverySignalKind.PROVIDER_UNAVAILABLE, "provider.unavailable", (RecoveryAction.SWITCH_PROVIDER, RecoveryAction.DEGRADE_MODEL), refs=("provider_id", "provider_route_id", "request_id"), severity="error")
        add((RecoverySource.PROVIDER_RUNTIME, RecoverySource.API_RUNTIME, RecoverySource.OMP_SUPPLEMENT), "credential_exhausted", RecoverySignalKind.CREDENTIAL_EXHAUSTED, "provider.credential_exhausted", (RecoveryAction.SWITCH_PROVIDER, RecoveryAction.REPLAN), refs=("provider_id", "credential_id", "request_id"), severity="error")
        add((RecoverySource.WORKSPACE_RUNTIME, RecoverySource.WATCHDOG_HANDOFF, RecoverySource.OMP_SUPPLEMENT), "workspace_conflict", RecoverySignalKind.WORKSPACE_CONFLICT, "workspace.conflict", (RecoveryAction.REPLAN, RecoveryAction.REROUTE), refs=("workspace_id", "subagent_id", "node_id"), severity="error")
        add((RecoverySource.CHECKPOINT_RUNTIME, RecoverySource.WORKER_HANDOFF, RecoverySource.OMP_SUPPLEMENT), "checkpoint_diverged", RecoverySignalKind.CHECKPOINT_DIVERGED, "checkpoint.diverged", (RecoveryAction.REPLAN, RecoveryAction.ABORT), refs=("checkpoint_id", "graph_id"), severity="critical")
        add((RecoverySource.CONTROL_RUNTIME,), "requirement_changed", RecoverySignalKind.REQUIREMENT_CHANGED, "control.requirement_changed", (RecoveryAction.REPLAN,), refs=("node_id", "request_id"))
        return {(rule.source, rule.source_kind): rule for rule in rules}

    @staticmethod
    def _generic_rule(source: RecoverySource, source_kind: str, value: Mapping[str, Any]) -> ClassificationRule:
        if source_kind != "unknown_failure":
            raise RecoverySignalClassificationError(
                f"no recovery classification rule for {source.value}:{source_kind}"
            )
        if not bool(value.get("allow_unknown", False)):
            raise RecoverySignalClassificationError("unknown failure requires explicit allow_unknown")
        return ClassificationRule(
            source=source,
            source_kind=source_kind,
            signal_kind=RecoverySignalKind.UNKNOWN_FAILURE,
            reason_code="runtime.unknown_failure",
            retryable=False,
            terminal=True,
            suggested_actions=(RecoveryAction.REPLAN, RecoveryAction.ABORT),
            require_any_refs=("node_id", "request_id", "worker_id"),
            severity="critical",
        )

    @staticmethod
    def _source(value: Mapping[str, Any], source: RecoverySource | str | None) -> RecoverySource:
        candidate = source if source is not None else value.get("source") or value.get("origin")
        if isinstance(candidate, RecoverySource):
            return candidate
        try:
            return RecoverySource(str(candidate))
        except ValueError as error:
            raise RecoverySignalClassificationError(f"unknown recovery source: {candidate!r}") from error

    @staticmethod
    def _source_kind(value: Mapping[str, Any]) -> str:
        candidate = value.get("source_kind") or value.get("kind") or value.get("signal_kind") or value.get("error_kind")
        result = str(candidate or "").strip().casefold().replace("-", "_")
        if not result:
            raise RecoverySignalClassificationError("typed recovery source kind is required")
        return result

    @classmethod
    def _refs(cls, value: Mapping[str, Any]) -> RecoveryRefs:
        nested = cls._mapping(value.get("refs") or value.get("target") or {})
        aliases = {
            "lease_id": "worker_lease_id",
            "route_id": "provider_route_id",
            "graph_version": "graph_revision",
        }
        merged: dict[str, Any] = {}
        for field_name in RecoveryRefs.__dataclass_fields__:
            candidate = nested.get(field_name, value.get(field_name))
            if candidate not in {None, ""}:
                merged[field_name] = candidate
        for source_name, target_name in aliases.items():
            if target_name not in merged:
                candidate = nested.get(source_name, value.get(source_name))
                if candidate not in {None, ""}:
                    merged[target_name] = candidate
        if "run_id" not in merged or "task_id" not in merged:
            raise RecoverySignalClassificationError("recovery signal requires run_id and task_id refs")
        try:
            return RecoveryRefs.from_dict(merged)
        except (TypeError, ValueError) as error:
            raise RecoverySignalClassificationError(f"invalid recovery refs: {error}") from error

    @staticmethod
    def _validate_required_refs(rule: ClassificationRule, refs: RecoveryRefs) -> None:
        if rule.require_any_refs and not any(getattr(refs, name, "") for name in rule.require_any_refs):
            raise RecoverySignalClassificationError(
                f"{rule.source.value}:{rule.source_kind} requires one of refs {rule.require_any_refs}"
            )

    @classmethod
    def _details(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        details = cls._mapping(value.get("details") or value.get("metadata") or {})
        excluded = {
            "schema", "signal_id", "source_signal_id", "source", "origin", "source_kind",
            "kind", "signal_kind", "refs", "target", "summary", "reason", "message",
            "recoverable", "retryable", "terminal", "severity", "attempt_count",
            "retry_attempt", "retry_after_ms", "suggested_actions", "observed_at", "created_at",
            "evidence_event_ids", "causation_id", "correlation_id", "details", "metadata",
        }
        structured = {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in excluded and isinstance(item, (str, int, float, bool, list, tuple, dict, type(None)))
        }
        return {**structured, **copy.deepcopy(details)}

    @staticmethod
    def _summary(value: Mapping[str, Any], rule: ClassificationRule) -> str:
        candidate = value.get("summary") or value.get("reason") or value.get("message")
        result = str(candidate or f"{rule.signal_kind.value} requires recovery").strip()
        if not result:
            result = f"{rule.signal_kind.value} requires recovery"
        return result[:4000]

    @staticmethod
    def _actions(value: Any) -> tuple[RecoveryAction, ...]:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            items = (value,)
        elif isinstance(value, Sequence):
            items = tuple(value)
        else:
            raise RecoverySignalClassificationError("suggested_actions must be a string or array")
        result: list[RecoveryAction] = []
        for item in items:
            normalized = str(item).casefold().replace("-", "_")
            aliases = {
                "fallback": RecoveryAction.DEGRADE_MODEL,
                "switch_model": RecoveryAction.DEGRADE_MODEL,
                "authenticate": RecoveryAction.AUTHENTICATE_MCP,
                "resume": RecoveryAction.RESUME_CHECKPOINT,
            }
            try:
                action = aliases.get(normalized) or RecoveryAction(normalized)
            except ValueError as error:
                raise RecoverySignalClassificationError(f"unknown recovery action hint: {item!r}") from error
            if action not in result:
                result.append(action)
        return tuple(result)

    @classmethod
    def _boolean(cls, value: Mapping[str, Any], key: str, *, nested: tuple[str, str] | None = None) -> bool:
        if key in value:
            return bool(value[key])
        if nested:
            parent = cls._mapping(value.get(nested[0]) or {})
            return bool(parent.get(nested[1], False))
        return False

    @staticmethod
    def _non_negative(value: Any, name: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise RecoverySignalClassificationError(f"{name} must be an integer") from error
        if result < 0:
            raise RecoverySignalClassificationError(f"{name} must be non-negative")
        return result

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RecoverySignalClassificationError("recovery payload object is required")
        return dict(value)

    @staticmethod
    def _now() -> str:
        from .contracts import utc_now
        return utc_now()


__all__ = [
    "ClassificationRule",
    "RecoverySignalClassificationError",
    "RecoverySignalClassifier",
]
