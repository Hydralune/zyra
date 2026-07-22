from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import FaultKind, FaultSignal, RecoveryHandoff
from .state_store import FaultStateStore


_ACTIONS: dict[FaultKind, tuple[str, ...]] = {
    FaultKind.TOOL_TIMEOUT: (
        "close_or_fence_timed_out_tool_call",
        "verify_side_effect_idempotency",
        "retry_or_reroute_tool_execution",
    ),
    FaultKind.WORKER_UNAVAILABLE: (
        "expire_worker_lease",
        "preserve_attempt_checkpoint",
        "reroute_to_healthy_worker",
    ),
    FaultKind.BROWSER_CRASH: (
        "close_stale_browser_session",
        "restore_browser_checkpoint",
        "reattach_browser_worker",
    ),
    FaultKind.BROWSER_DISCONNECT: (
        "probe_browser_process",
        "reattach_cdp_transport",
        "restore_browser_checkpoint_if_needed",
    ),
    FaultKind.PERMISSION_DENIED: (
        "preserve_permission_receipt",
        "remove_denied_action_from_plan",
        "replan_with_allowed_capabilities",
    ),
    FaultKind.MODEL_FAILURE: (
        "preserve_partial_provider_response",
        "select_compatible_provider_route",
        "resume_from_last_committed_turn",
    ),
    FaultKind.MODEL_RATE_LIMIT: (
        "respect_retry_after",
        "select_capacity_compatible_route",
    ),
    FaultKind.MODEL_QUOTA_EXHAUSTED: (
        "quarantine_exhausted_credential",
        "select_credential_or_provider_fallback",
    ),
    FaultKind.SCHEMA_FAILURE: (
        "preserve_rejected_payload",
        "request_schema_conforming_revision",
    ),
    FaultKind.WORKSPACE_CORRUPT: (
        "block_workspace_writes",
        "verify_checkpoint_and_artifact_digests",
        "restore_or_recreate_isolated_workspace",
    ),
    FaultKind.MCP_DISCONNECTED: (
        "fence_pending_mcp_requests",
        "reconnect_with_backoff",
        "refresh_mcp_capabilities",
    ),
    FaultKind.PROCESS_EXITED: (
        "record_process_generation_exit",
        "restart_or_reroute_supervised_process",
    ),
    FaultKind.SUBAGENT_FAILED: (
        "preserve_subagent_terminal_receipt",
        "restore_child_checkpoint_or_spawn_replacement",
        "reconcile_parent_mailbox_and_artifacts",
    ),
    FaultKind.UNKNOWN: ("collect_more_structured_evidence",),
}


class WatchdogRecoveryBridge:
    """Durable 07B-to-07C handoff; deliberately not a recovery planner."""

    def __init__(self, store: FaultStateStore) -> None:
        self.store = store

    def handoff(
        self,
        signal: FaultSignal,
        *,
        injection_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> RecoveryHandoff:
        prior = self.store.handoffs(task_id=signal.refs.task_id, limit=5_000)
        for item in prior:
            if item.signal_id == signal.signal_id:
                return item
        actions = _ACTIONS.get(signal.kind, _ACTIONS[FaultKind.UNKNOWN])
        value = RecoveryHandoff(
            run_id=signal.refs.run_id,
            task_id=signal.refs.task_id,
            signal_id=signal.signal_id,
            fault_kind=signal.kind,
            refs=signal.refs,
            requested_actions=actions,
            recoverable=signal.retryable,
            reason=(
                "Fault signal requires an explicit recovery-planner decision."
                if signal.retryable
                else "Fault signal is blocking and requires a safe replan or terminal decision."
            ),
            injection_id=injection_id or signal.provenance.injection_id,
            metadata={
                "producer": "M1-S07B-01.WatchdogRecoveryBridge",
                "consumer": "M1-S07C.RecoveryPlanner",
                "planner_executed": False,
                "canonical_fault_signal_id": signal.signal_id,
                "source_observation_id": signal.refs.observation_id,
                **dict(metadata or {}),
            },
        )
        stored, _duplicate = self.store.append_handoff(value)
        return stored

    def pending(self, *, task_id: str = "") -> tuple[RecoveryHandoff, ...]:
        deliveries = self.store.handoff_deliveries(
            task_id=task_id,
            statuses=("pending", "leased"),
            limit=5_000,
        )
        output: list[RecoveryHandoff] = []
        for delivery in deliveries:
            handoff = self.store.handoff(str(delivery["handoff_id"]))
            if handoff is not None:
                output.append(handoff)
        return tuple(output)

    def claim(
        self,
        *,
        consumer: str,
        now_ms: int,
        lease_ms: int,
        task_id: str = "",
        limit: int = 20,
    ) -> tuple[Mapping[str, Any], ...]:
        return self.store.claim_handoffs(
            consumer=consumer,
            now_ms=now_ms,
            lease_ms=lease_ms,
            task_id=task_id,
            limit=limit,
        )

    def acknowledge(
        self,
        handoff_id: str,
        *,
        consumer: str,
        lease_token: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return self.store.acknowledge_handoff(
            handoff_id,
            consumer=consumer,
            lease_token=lease_token,
            receipt=receipt,
        )

    def release(
        self,
        handoff_id: str,
        *,
        consumer: str,
        lease_token: str,
        now_ms: int,
        retry_after_ms: int,
        error: str,
        max_attempts: int = 5,
    ) -> Mapping[str, Any]:
        return self.store.release_handoff(
            handoff_id,
            consumer=consumer,
            lease_token=lease_token,
            now_ms=now_ms,
            retry_after_ms=retry_after_ms,
            error=error,
            max_attempts=max_attempts,
        )

    def delivery_snapshot(
        self,
        *,
        task_id: str = "",
        statuses: tuple[str, ...] = (),
    ) -> tuple[Mapping[str, Any], ...]:
        return self.store.handoff_deliveries(
            task_id=task_id,
            statuses=statuses,
            limit=5_000,
        )

    @staticmethod
    def contract() -> dict[str, Any]:
        return {
            "schema": "zyra.watchdog-recovery-bridge/v1",
            "producer": "M1-S07B-01",
            "consumer": "M1-S07C",
            "owns_recovery_plan": False,
            "owns_fault_signal": False,
            "owns_handoff_journal": True,
            "delivery": {
                "durable_outbox": True,
                "lease_fenced": True,
                "expired_lease_reclaim": True,
                "acknowledgement": True,
                "dead_letter_after_bounded_attempts": True,
                "recovery_plan_executed_by_producer": False,
            },
            "action_catalog": {kind.value: list(actions) for kind, actions in _ACTIONS.items()},
        }


__all__ = ["WatchdogRecoveryBridge"]
