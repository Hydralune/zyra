from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from zyra_core import ArtifactRef, EventRecord


@dataclass(frozen=True, slots=True)
class ClaudeQueryEngineConfig:
    """Narrow Python process/durability configuration for the TypeScript owner."""

    max_turns: int | None = None
    max_tool_result_chars: int = 8000
    max_query_context_chars: int = 32000
    continue_on_error: bool = False
    max_read_only_concurrency: int = 10
    emit_tool_use_summaries: bool = True
    trace_title: str = "CodeWorker Runtime Trace"
    context_artifact_title: str = "CodeWorker context compaction"
    allow_empty_turns: bool = False
    control_commands: Sequence[Any] = field(default_factory=tuple)
    project_root: str | Path | None = None
    session_seed: Mapping[str, Any] | None = None
    context_snapshot: Mapping[str, Any] | None = None
    preprocessed_messages: Sequence[Any] = field(default_factory=tuple)
    session_foundation_metadata: Mapping[str, str] = field(default_factory=dict)
    max_turn_tool_result_chars: int | None = None
    disable_tool_registry_runtime: bool = False
    disable_tool_execution_runtime: bool = False
    disable_tool_result_budget_runtime: bool = False
    disable_tool_permission_handoff_runtime: bool = False
    disable_tool_permission_runtime: bool = False
    disable_permission_rule_store: bool = False
    disable_permission_request_queue: bool = False
    disable_permission_decision_log: bool = False
    permission_mode: str = "default"
    permission_approval_ttl_seconds: float = 300.0
    permission_execution_grant_ttl_seconds: float = 30.0
    permission_interactive: bool = True
    permission_headless: bool = False
    permission_bypass_available: bool = False
    permission_auto_available: bool = True
    permission_state_path: str | Path | None = None
    permission_extension_registry: Any = None
    permission_continuation_payload_writer: Callable[..., None] | None = None
    permission_continuation_payload_tombstoner: Callable[..., None] | None = None
    disable_permission_continuation_runtime: bool = False
    disable_runtime_budget_state: bool = False
    disable_compact_restore_runtime: bool = False
    disable_model_stream_runtime: bool = False
    disable_api_retry_runtime: bool = False
    disable_codeworker_api_foundation_runtime: bool = False
    disable_context_security_runtime: bool = False
    disable_restore_integration_runtime: bool = False
    model_name: str = "zyra-local-code-model"
    model_input_token_limit: int = 200000
    model_output_token_limit: int = 8192
    api_retry_max_attempts: int = 3
    api_retry_fallback_models: Sequence[str] = field(default_factory=lambda: ("zyra-local-fallback",))
    runtime_constraints: Mapping[str, Any] = field(default_factory=dict)
    restored_runtime_state: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ClaudeQueryEngineResult:
    ok: bool
    event_records: list[EventRecord]
    artifacts: list[ArtifactRef]
    step_summaries: list[str]
    turn_count: int
    tool_call_count: int
    context_compaction_count: int = 0
    stopped_reason: str | None = None
    session_snapshot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


def permission_continuation_payloads(restored: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = restored.get("permission_continuation_payloads")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}


def synchronize_permission_continuations(*_args: Any, **_kwargs: Any) -> None:
    """E02 PermissionStateStore remains the only durable reconciliation owner."""


def reconcile_permission_continuation_payloads(
    continuation: Any,
    payloads: dict[str, dict[str, Any]],
    *,
    payload_tombstoner: Callable[[str, str, str], None] | None = None,
) -> None:
    active = {record.payload_locator for record in continuation.active()}
    for locator in tuple(payloads):
        if locator in active:
            continue
        raw = payloads.pop(locator, {})
        metadata = raw.get("metadata") if isinstance(raw, Mapping) else {}
        request_id = str(metadata.get("request_id") or "orphaned-continuation") if isinstance(metadata, Mapping) else "orphaned-continuation"
        if payload_tombstoner is not None:
            payload_tombstoner(request_id, locator, "terminal_or_orphaned_continuation_reconciled")
