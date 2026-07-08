from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, new_id, now_iso, to_jsonable

from .artifacts import LocalArtifactStore
from .executor import ToolExecutionContext, ToolExecutor
from .permissions import PermissionEffect, ToolPermissionPolicy
from .tool_loop import (
    ToolAccessMode,
    ToolBudgetDecision,
    ToolFailureSignal,
    ToolLoopBatch,
    ToolLoopRequest,
    ToolLoopScheduler,
    ToolResultBudgeter,
    tool_failure_signal_from_result,
)
from .tools import ToolRegistry, ToolResult, ToolSpec


TOOL_LOOP_FOUNDATION_OWNER_UNIT = "M1-02C"
TOOL_LOOP_FOUNDATION_RUNTIME_ID = "zyra-tool-loop-budget-foundation"


class ToolRegistryFilterDecision(StrEnum):
    ACTIVE = "active"
    ASK_VISIBLE = "ask_visible"
    FILTERED_DENY = "filtered_deny"
    FILTERED_DISABLED = "filtered_disabled"


class ToolContextModifierKind(StrEnum):
    MESSAGE_APPEND = "message_append"
    READ_FILE_STATE = "read_file_state"
    CONTENT_REPLACEMENT = "content_replacement"
    PERMISSION_HANDOFF = "permission_handoff"
    BUDGET_LEDGER = "budget_ledger"
    ARTIFACT_REF = "artifact_ref"
    QUERY_TRACKING = "query_tracking"


class ToolRuntimeSourceDecision(StrEnum):
    ZYRA_MODULE_MIGRATED = "zyra_module_migrated"
    ACTIVE_PORT = "active_adapter_port"
    LEGACY_VENDOR_DEBT = "legacy_vendor_debt"
    REFERENCE_ONLY = "reference_only"
    DEFERRED = "deferred"


class ToolRuntimeDisabledError(RuntimeError):
    def __init__(self, component: str) -> None:
        super().__init__(f"{component} is disabled")
        self.component = component


@dataclass(frozen=True, slots=True)
class ToolRuntimeSourceLedgerRow:
    source_repo: str
    source_path: str
    capability: str
    target_path: str
    decision: ToolRuntimeSourceDecision
    owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT
    upstream_signal: str = ""
    required_for_default_path: bool = True
    effective_code: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "capability": self.capability,
            "target_path": self.target_path,
            "decision": str(self.decision),
            "owner_unit": self.owner_unit,
            "upstream_signal": self.upstream_signal,
            "required_for_default_path": self.required_for_default_path,
            "effective_code": self.effective_code,
        }


@dataclass(frozen=True, slots=True)
class ToolMaterializedEntry:
    name: str
    purpose: str
    source: str
    access_mode: ToolAccessMode
    read_only: bool
    concurrency_safe: bool
    mutates_workspace: bool
    source_path: str
    filter_decision: ToolRegistryFilterDecision
    permission_effect: str
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.filter_decision in {ToolRegistryFilterDecision.ACTIVE, ToolRegistryFilterDecision.ASK_VISIBLE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "source": self.source,
            "access_mode": str(self.access_mode),
            "read_only": self.read_only,
            "concurrency_safe": self.concurrency_safe,
            "mutates_workspace": self.mutates_workspace,
            "source_path": self.source_path,
            "filter_decision": str(self.filter_decision),
            "permission_effect": self.permission_effect,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolRegistryMaterialization:
    materialization_id: str
    owner_unit: str
    runtime_id: str
    worker_request_id: str
    session_id: str
    workspace_root: str
    active_tools: tuple[ToolSpec, ...]
    entries: tuple[ToolMaterializedEntry, ...]
    filtered_tool_names: tuple[str, ...]
    ask_visible_tool_names: tuple[str, ...]
    created_at: str = field(default_factory=now_iso)
    generation: int = 1
    source_ledger: tuple[ToolRuntimeSourceLedgerRow, ...] = field(default_factory=tuple)

    @property
    def active_tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.active_tools)

    @property
    def filtered_count(self) -> int:
        return len(self.filtered_tool_names)

    @property
    def ask_visible_count(self) -> int:
        return len(self.ask_visible_tool_names)

    def to_registry(self) -> ToolRegistry:
        return ToolRegistry(list(self.active_tools))

    def to_dict(self) -> dict[str, Any]:
        return {
            "materialization_id": self.materialization_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "worker_request_id": self.worker_request_id,
            "session_id": self.session_id,
            "workspace_root": self.workspace_root,
            "generation": self.generation,
            "created_at": self.created_at,
            "active_tool_names": list(self.active_tool_names),
            "filtered_tool_names": list(self.filtered_tool_names),
            "ask_visible_tool_names": list(self.ask_visible_tool_names),
            "entries": [entry.to_dict() for entry in self.entries],
            "source_ledger": [row.to_dict() for row in self.source_ledger],
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_registry_runtime_owner_unit": self.owner_unit,
            "tool_registry_runtime_id": self.runtime_id,
            "tool_registry_materialization_id": self.materialization_id,
            "tool_registry_active_count": str(len(self.active_tools)),
            "tool_registry_filtered_count": str(self.filtered_count),
            "tool_registry_ask_visible_count": str(self.ask_visible_count),
            "tool_registry_source_ledger_count": str(len(self.source_ledger)),
        }


@dataclass(slots=True)
class ToolRegistryRuntime:
    registry: ToolRegistry
    permission_policy: ToolPermissionPolicy | None = None
    owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT
    runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID
    hide_denied_tools: bool = True
    disabled: bool = False
    _generation: int = field(default=0, init=False, repr=False)

    def materialize(
        self,
        *,
        worker_request_id: str,
        session_id: str,
        workspace_root: str | Path,
        source_ledger: Sequence[ToolRuntimeSourceLedgerRow] | None = None,
    ) -> ToolRegistryMaterialization:
        if self.disabled:
            raise ToolRuntimeDisabledError("ToolRegistryRuntime")
        self._generation += 1
        entries: list[ToolMaterializedEntry] = []
        active_tools: list[ToolSpec] = []
        filtered: list[str] = []
        ask_visible: list[str] = []
        for spec in _stable_tool_order(self.registry.list()):
            entry = self._entry_for_spec(spec)
            entries.append(entry)
            if entry.active:
                active_tools.append(spec)
            else:
                filtered.append(spec.name)
            if entry.filter_decision == ToolRegistryFilterDecision.ASK_VISIBLE:
                ask_visible.append(spec.name)
        return ToolRegistryMaterialization(
            materialization_id=new_id("toolreg"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            worker_request_id=worker_request_id,
            session_id=session_id,
            workspace_root=str(Path(workspace_root)),
            active_tools=tuple(active_tools),
            entries=tuple(entries),
            filtered_tool_names=tuple(filtered),
            ask_visible_tool_names=tuple(ask_visible),
            generation=self._generation,
            source_ledger=tuple(source_ledger or tool_runtime_source_to_target_rows()),
        )

    def _entry_for_spec(self, spec: ToolSpec) -> ToolMaterializedEntry:
        access_mode = _access_mode_from_spec(spec)
        read_only = _metadata_bool(spec.metadata, "read_only", default=access_mode == ToolAccessMode.READ_ONLY)
        concurrency_safe = read_only and _metadata_bool(spec.metadata, "concurrency_safe", default=True)
        mutates_workspace = _metadata_bool(
            spec.metadata,
            "mutates_workspace",
            default=access_mode in {ToolAccessMode.WORKSPACE_WRITE, ToolAccessMode.SHELL},
        )
        effect, reason = self._prefilter_effect(spec, access_mode)
        if spec.metadata.get("disabled") == "true":
            decision = ToolRegistryFilterDecision.FILTERED_DISABLED
        elif effect == PermissionEffect.DENY and self.hide_denied_tools:
            decision = ToolRegistryFilterDecision.FILTERED_DENY
        elif effect == PermissionEffect.ASK:
            decision = ToolRegistryFilterDecision.ASK_VISIBLE
        else:
            decision = ToolRegistryFilterDecision.ACTIVE
        return ToolMaterializedEntry(
            name=spec.name,
            purpose=spec.purpose,
            source=spec.source,
            access_mode=access_mode,
            read_only=read_only,
            concurrency_safe=concurrency_safe,
            mutates_workspace=mutates_workspace,
            source_path=str(spec.metadata.get("source_path") or ""),
            filter_decision=decision,
            permission_effect=str(effect),
            reason=reason,
            metadata={str(key): str(value) for key, value in spec.metadata.items()},
        )

    def _prefilter_effect(self, spec: ToolSpec, access_mode: ToolAccessMode) -> tuple[PermissionEffect, str]:
        configured = spec.metadata.get("registry_filter_effect")
        if configured:
            try:
                effect = PermissionEffect(str(configured))
            except ValueError:
                effect = PermissionEffect.ALLOW
            return effect, "tool metadata registry_filter_effect"
        if spec.metadata.get("denied_by_default") == "true":
            return PermissionEffect.DENY, "tool metadata denied_by_default"
        if access_mode == ToolAccessMode.SHELL and self.permission_policy is not None:
            effect = self.permission_policy.default_effect
            return effect, "shell default policy is visible but marked for runtime review"
        if access_mode == ToolAccessMode.WORKSPACE_WRITE and self.permission_policy is not None:
            return self.permission_policy.default_effect, "workspace write requires argument-scoped runtime review"
        return PermissionEffect.ALLOW, "tool has no blanket deny rule"


@dataclass(frozen=True, slots=True)
class ToolContextModifier:
    kind: ToolContextModifierKind
    tool_call_id: str
    key: str
    value: Any
    phase: str
    source: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID
    created_at: str = field(default_factory=now_iso)

    @property
    def conflict_key(self) -> str:
        return f"{self.kind}:{self.key}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "tool_call_id": self.tool_call_id,
            "key": self.key,
            "value": to_jsonable(self.value),
            "phase": self.phase,
            "source": self.source,
            "created_at": self.created_at,
            "conflict_key": self.conflict_key,
        }


@dataclass(slots=True)
class ToolUseContext:
    run_id: str
    task_id: str
    node_id: str | None
    worker_request_id: str
    session_id: str
    turn_id: str
    turn_index: int
    materialization: ToolRegistryMaterialization
    source_contract: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    active_tool_names: tuple[str, ...] = ()
    in_progress_tool_call_ids: set[str] = field(default_factory=set)
    read_file_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    content_replacements: dict[str, dict[str, Any]] = field(default_factory=dict)
    query_tracking: dict[str, Any] = field(default_factory=dict)
    permission_handoffs: list[dict[str, Any]] = field(default_factory=list)
    budget_ledger: list[dict[str, Any]] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    modifier_log: list[dict[str, Any]] = field(default_factory=list)
    pending_concurrent_modifiers: list[ToolContextModifier] = field(default_factory=list)
    tool_result_chars: int = 0
    _lock: Any = field(default_factory=RLock, init=False, repr=False)

    @classmethod
    def for_turn(
        cls,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        turn_id: str,
        turn_index: int,
        materialization: ToolRegistryMaterialization,
        source_contract: Mapping[str, Any] | None = None,
        seed_messages: Sequence[Mapping[str, Any]] = (),
    ) -> "ToolUseContext":
        return cls(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_request_id=worker_request_id,
            session_id=session_id,
            turn_id=turn_id,
            turn_index=turn_index,
            materialization=materialization,
            source_contract=dict(source_contract or {}),
            messages=[dict(message) for message in seed_messages],
            active_tool_names=materialization.active_tool_names,
            query_tracking={
                "owner_unit": TOOL_LOOP_FOUNDATION_OWNER_UNIT,
                "runtime_id": TOOL_LOOP_FOUNDATION_RUNTIME_ID,
                "materialization_id": materialization.materialization_id,
                "turn_started_at": now_iso(),
            },
        )

    def start_tool(self, request: ToolLoopRequest) -> None:
        with self._lock:
            self.in_progress_tool_call_ids.add(request.call.tool_call_id)
            self.query_tracking["last_started_tool_call_id"] = request.call.tool_call_id

    def finish_tool(self, request: ToolLoopRequest) -> None:
        with self._lock:
            self.in_progress_tool_call_ids.discard(request.call.tool_call_id)
            self.query_tracking["last_completed_tool_call_id"] = request.call.tool_call_id

    def queue_modifier(self, modifier: ToolContextModifier) -> None:
        with self._lock:
            self.pending_concurrent_modifiers.append(modifier)

    def flush_concurrent_modifiers(self) -> list[dict[str, Any]]:
        with self._lock:
            pending = sorted(
                self.pending_concurrent_modifiers,
                key=lambda item: (item.conflict_key, item.tool_call_id, item.created_at),
            )
            self.pending_concurrent_modifiers = []
        return [self.apply_modifier(modifier, queued=True) for modifier in pending]

    def apply_modifier(self, modifier: ToolContextModifier, *, queued: bool = False) -> dict[str, Any]:
        with self._lock:
            status = "applied"
            if modifier.kind == ToolContextModifierKind.MESSAGE_APPEND:
                self.messages.append(_mapping_value(modifier.value))
            elif modifier.kind == ToolContextModifierKind.READ_FILE_STATE:
                self.read_file_state[modifier.key] = _mapping_value(modifier.value)
            elif modifier.kind == ToolContextModifierKind.CONTENT_REPLACEMENT:
                existing = self.content_replacements.get(modifier.key)
                value = _mapping_value(modifier.value)
                if existing is not None and existing != value:
                    status = "applied_with_conflict"
                self.content_replacements[modifier.key] = value
            elif modifier.kind == ToolContextModifierKind.PERMISSION_HANDOFF:
                self.permission_handoffs.append(_mapping_value(modifier.value))
            elif modifier.kind == ToolContextModifierKind.BUDGET_LEDGER:
                self.budget_ledger.append(_mapping_value(modifier.value))
            elif modifier.kind == ToolContextModifierKind.ARTIFACT_REF:
                artifact_id = str(modifier.value)
                if artifact_id and artifact_id not in self.artifact_refs:
                    self.artifact_refs.append(artifact_id)
            elif modifier.kind == ToolContextModifierKind.QUERY_TRACKING:
                self.query_tracking[modifier.key] = to_jsonable(modifier.value)
            else:
                status = "ignored_unknown_modifier"
            entry = {
                **modifier.to_dict(),
                "status": status,
                "queued": queued,
                "applied_at": now_iso(),
            }
            self.modifier_log.append(entry)
            return entry

    def record_result_chars(self, chars: int) -> int:
        with self._lock:
            self.tool_result_chars += max(0, int(chars))
            return self.tool_result_chars

    def to_dict(self, *, include_messages: bool = False) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "worker_request_id": self.worker_request_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "active_tool_names": list(self.active_tool_names),
            "in_progress_tool_call_ids": sorted(self.in_progress_tool_call_ids),
            "read_file_state": to_jsonable(self.read_file_state),
            "content_replacements": to_jsonable(self.content_replacements),
            "query_tracking": to_jsonable(self.query_tracking),
            "permission_handoffs": to_jsonable(self.permission_handoffs),
            "budget_ledger": to_jsonable(self.budget_ledger),
            "artifact_refs": list(self.artifact_refs),
            "modifier_log": to_jsonable(self.modifier_log),
            "tool_result_chars": self.tool_result_chars,
            "messages": to_jsonable(self.messages) if include_messages else [],
            "materialization": self.materialization.to_dict(),
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_use_context_owner_unit": TOOL_LOOP_FOUNDATION_OWNER_UNIT,
            "tool_use_context_runtime_id": TOOL_LOOP_FOUNDATION_RUNTIME_ID,
            "tool_use_context_active_tools": str(len(self.active_tool_names)),
            "tool_use_context_modifiers": str(len(self.modifier_log)),
            "tool_use_context_permission_handoffs": str(len(self.permission_handoffs)),
            "tool_use_context_budget_entries": str(len(self.budget_ledger)),
            "tool_use_context_artifact_refs": str(len(self.artifact_refs)),
            "tool_use_context_result_chars": str(self.tool_result_chars),
        }


@dataclass(frozen=True, slots=True)
class ToolBudgetReceipt:
    bounded_result: ToolResult
    decision: ToolBudgetDecision
    payload_chars: int
    aggregate_chars: int
    externalized: bool
    shaping_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bounded_result": to_jsonable(self.bounded_result),
            "decision": self.decision.to_dict(),
            "payload_chars": self.payload_chars,
            "aggregate_chars": self.aggregate_chars,
            "externalized": self.externalized,
            "shaping_source": self.shaping_source,
        }


@dataclass(slots=True)
class ToolResultBudgetRuntime:
    max_result_chars: int
    max_turn_chars: int | None = None
    owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT
    runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID
    disabled: bool = False

    def apply(
        self,
        *,
        request: ToolLoopRequest,
        result: ToolResult,
        context: ToolUseContext,
        artifact_store: LocalArtifactStore,
    ) -> ToolBudgetReceipt:
        if self.disabled:
            raise ToolRuntimeDisabledError("ToolResultBudgetRuntime")
        bounded, decision = ToolResultBudgeter(max_chars=max(1, self.max_result_chars)).apply(
            request=request,
            result=result,
            artifact_store=artifact_store,
        )
        payload_chars = _result_chars(bounded)
        aggregate_chars = context.tool_result_chars + payload_chars
        if (
            self.max_turn_chars is not None
            and self.max_turn_chars > 0
            and aggregate_chars > self.max_turn_chars
            and not decision.applied
        ):
            bounded, decision = self._externalize_turn_overflow(
                request=request,
                result=bounded,
                artifact_store=artifact_store,
                original_chars=payload_chars,
            )
            payload_chars = _result_chars(bounded)
            aggregate_chars = context.tool_result_chars + payload_chars
        aggregate_after = context.record_result_chars(payload_chars)
        return ToolBudgetReceipt(
            bounded_result=_with_result_metadata(
                bounded,
                {
                    "tool_result_budget_owner_unit": self.owner_unit,
                    "tool_result_shaping_source": "opencode.ToolOutputStore+hermes.tool_result_budget",
                    "tool_result_payload_chars": str(payload_chars),
                    "tool_result_aggregate_chars": str(aggregate_after),
                    "tool_result_untrusted_envelope": "true",
                },
            ),
            decision=decision,
            payload_chars=payload_chars,
            aggregate_chars=aggregate_after,
            externalized=decision.applied,
            shaping_source="claude-code-best.toolResultStorage/opencode.ToolOutputStore/hermes.budget",
        )

    def _externalize_turn_overflow(
        self,
        *,
        request: ToolLoopRequest,
        result: ToolResult,
        artifact_store: LocalArtifactStore,
        original_chars: int,
    ) -> tuple[ToolResult, ToolBudgetDecision]:
        payload = json.dumps(to_jsonable(result.output), ensure_ascii=False, sort_keys=True)
        artifact = artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=payload,
            title=f"tool_result_turn_budget:{request.tool_name}:{request.call.tool_call_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=request.node_id,
        )
        preview_chars = min(max(1, self.max_result_chars), len(payload))
        bounded = ToolResult(
            tool_call_id=result.tool_call_id,
            ok=result.ok,
            summary=f"{result.summary} (tool result turn budget overflow stored as artifact)",
            output={
                "truncated": True,
                "output_preview": payload[:preview_chars],
                "original_chars": original_chars,
                "budget_chars": int(self.max_turn_chars or 0),
                "full_output_artifact_id": artifact.artifact_id,
                "turn_budget_overflow": True,
            },
            artifacts=[*result.artifacts, artifact],
            error=result.error,
            completed_at=result.completed_at,
            metadata={
                **result.metadata,
                "tool_result_budget_applied": "true",
                "tool_result_budget_scope": "turn",
                "tool_result_original_chars": str(original_chars),
                "tool_result_budget_chars": str(self.max_turn_chars or 0),
            },
        )
        return bounded, ToolBudgetDecision(
            True,
            original_chars,
            int(self.max_turn_chars or 0),
            preview_chars=preview_chars,
            artifact_id=artifact.artifact_id,
            reason="turn_tool_result_budget_exceeded",
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionReceipt:
    request: ToolLoopRequest
    raw_result: ToolResult
    bounded_result: ToolResult
    budget_decision: ToolBudgetDecision
    budget_receipt: ToolBudgetReceipt
    failure_signal: ToolFailureSignal | None
    context_modifiers: tuple[ToolContextModifier, ...]
    modifier_applications: tuple[dict[str, Any], ...]
    started_at: str
    completed_at: str
    executor_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "raw_result": to_jsonable(self.raw_result),
            "bounded_result": to_jsonable(self.bounded_result),
            "budget_decision": self.budget_decision.to_dict(),
            "budget_receipt": self.budget_receipt.to_dict(),
            "failure_signal": self.failure_signal.to_dict() if self.failure_signal else None,
            "context_modifiers": [modifier.to_dict() for modifier in self.context_modifiers],
            "modifier_applications": to_jsonable(self.modifier_applications),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "executor_name": self.executor_name,
        }


@dataclass(slots=True)
class ToolExecutionRuntime:
    context: ToolExecutionContext
    scheduler: ToolLoopScheduler
    budget_runtime: ToolResultBudgetRuntime
    owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT
    runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID
    disabled: bool = False
    executor: ToolExecutor | None = None

    def execute_request(self, request: ToolLoopRequest, tool_context: ToolUseContext) -> ToolExecutionReceipt:
        if self.disabled:
            raise ToolRuntimeDisabledError("ToolExecutionRuntime")
        executor = self.executor or ToolExecutor(self.context)
        started_at = now_iso()
        tool_context.start_tool(request)
        try:
            raw_result = self.scheduler.schema_error_result(request) if not request.valid else executor.execute(request.call)
        finally:
            tool_context.finish_tool(request)
        budget_receipt = self.budget_runtime.apply(
            request=request,
            result=raw_result,
            context=tool_context,
            artifact_store=self.context.artifact_store,
        )
        bounded_result = budget_receipt.bounded_result
        failure_signal = tool_failure_signal_from_result(
            request,
            bounded_result,
            budget_decision=budget_receipt.decision if budget_receipt.decision.applied else None,
        )
        modifiers = tuple(
            _modifiers_for_result(
                request=request,
                raw_result=raw_result,
                bounded_result=bounded_result,
                budget_receipt=budget_receipt,
            )
        )
        return ToolExecutionReceipt(
            request=request,
            raw_result=raw_result,
            bounded_result=bounded_result,
            budget_decision=budget_receipt.decision,
            budget_receipt=budget_receipt,
            failure_signal=failure_signal,
            context_modifiers=modifiers,
            modifier_applications=(),
            started_at=started_at,
            completed_at=now_iso(),
            executor_name=type(executor).__name__,
        )

    def execute_batch(
        self,
        batch: ToolLoopBatch,
        *,
        tool_context: ToolUseContext,
        max_workers: int,
    ) -> list[ToolExecutionReceipt]:
        if self.disabled:
            raise ToolRuntimeDisabledError("ToolExecutionRuntime")
        requests = list(batch.requests)
        concurrent = len(requests) > 1 and all(request.read_only and request.concurrency_safe for request in requests)
        if concurrent:
            with ThreadPoolExecutor(max_workers=min(max(1, max_workers), len(requests))) as pool:
                receipts = list(pool.map(lambda item: self.execute_request(item, tool_context), requests))
            for receipt in receipts:
                for modifier in receipt.context_modifiers:
                    tool_context.queue_modifier(modifier)
            applications = tuple(tool_context.flush_concurrent_modifiers())
            return [_with_receipt_applications(receipt, applications) for receipt in receipts]
        receipts = []
        for request in requests:
            receipt = self.execute_request(request, tool_context)
            applications = tuple(tool_context.apply_modifier(modifier) for modifier in receipt.context_modifiers)
            receipts.append(_with_receipt_applications(receipt, applications))
        return receipts


def tool_runtime_source_to_target_rows() -> tuple[ToolRuntimeSourceLedgerRow, ...]:
    return (
        ToolRuntimeSourceLedgerRow(
            source_repo="claude-code-best",
            source_path="src/query.ts",
            capability="assistant tool_use to tool_result continuation loop, max-turn stop and query-state handoff.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_continuation.py:ToolContinuationRuntime",
            decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            upstream_signal="State.transition/tool_result continuation",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="claude-code-best",
            source_path="src/tools.ts",
            capability="Stable tool pool assembly, built-in before MCP, deny prefilter before model exposure.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_foundation.py:ToolRegistryRuntime",
            decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            upstream_signal="getTools/filterToolsByDenyRules",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="claude-code-best",
            source_path="src/Tool.ts",
            capability="Tool identity, schema, read-only/concurrency/destructive metadata.",
            target_path="packages/runtime/zyra_runtime/tools.py:ToolSpec",
            decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            upstream_signal="Tool/inputSchema/isConcurrencySafe",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="claude-code-best",
            source_path="src/services/tools/toolExecution.ts",
            capability="Lookup, validation, permission handoff, execution receipt and result mapping.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_foundation.py:ToolExecutionRuntime",
            decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            upstream_signal="runToolUse/checkPermissionsAndCallTool",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="claude-code-best",
            source_path="src/services/tools/toolOrchestration.ts",
            capability="Concurrent read-only batch execution and serial mutating execution.",
            target_path="packages/runtime/zyra_runtime/tool_loop.py:ToolLoopScheduler",
            decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            upstream_signal="runToolsConcurrently/runToolsSerially",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="claude-code-best",
            source_path="src/services/tools/StreamingToolExecutor.ts",
            capability="Streaming progress frames around real tool execution, result shaping and context modifier application.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_streaming.py:ToolStreamingRuntime",
            decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            upstream_signal="StreamingToolExecutor/tool_use_progress",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="claude-code-best",
            source_path="src/utils/toolResultStorage.ts",
            capability="Persisted output tag, tool result externalization and content replacement metadata.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_foundation.py:ToolResultBudgetRuntime",
            decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            upstream_signal="persistToolResult/enforceToolResultBudget",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="opencode",
            source_path="packages/opencode/src/tool/registry.ts",
            capability="Materialized registry generation, stale materialization guard and permission-aware tool visibility.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_foundation.py:ToolRegistryRuntime",
            decision=ToolRuntimeSourceDecision.ACTIVE_PORT,
            upstream_signal="ToolRegistry.materialize/settle",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="opencode",
            source_path="packages/opencode/src/tool/output.ts",
            capability="Bound model-visible output while keeping full output in managed storage.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_foundation.py:ToolResultBudgetRuntime",
            decision=ToolRuntimeSourceDecision.ACTIVE_PORT,
            upstream_signal="ToolOutputStore",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="opencode",
            source_path="packages/opencode/src/permission/**",
            capability="Permission question/reply event shape for ask-once and ask-always flows.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_foundation.py:ToolUseContext.permission_handoffs",
            decision=ToolRuntimeSourceDecision.ACTIVE_PORT,
            upstream_signal="PermissionV2.ask/Deferred",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="hermes-agent",
            source_path="hermes/agents/approval/**",
            capability="Approval floor/gateway semantics that block execution before side effects.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_foundation.py:ToolExecutionRuntime",
            decision=ToolRuntimeSourceDecision.ACTIVE_PORT,
            upstream_signal="approval gateway",
        ),
        ToolRuntimeSourceLedgerRow(
            source_repo="hermes-agent",
            source_path="hermes/tools/search/**",
            capability="Tool search/progressive disclosure boundary used as later registry extension point.",
            target_path="packages/runtime/zyra_runtime/tool_runtime_foundation.py:ToolRegistryMaterialization.source_ledger",
            decision=ToolRuntimeSourceDecision.DEFERRED,
            upstream_signal="tool_search",
            required_for_default_path=False,
            effective_code=False,
        ),
    )


def tool_foundation_contract_summary() -> dict[str, Any]:
    rows = tool_runtime_source_to_target_rows()
    return {
        "ownerUnit": TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        "runtimeId": TOOL_LOOP_FOUNDATION_RUNTIME_ID,
        "sourceLedger": [row.to_dict() for row in rows],
        "sourceRepos": sorted({row.source_repo for row in rows}),
        "requiredRows": sum(1 for row in rows if row.required_for_default_path),
        "activePorts": sum(1 for row in rows if row.decision == ToolRuntimeSourceDecision.ACTIVE_PORT),
        "zyraModuleMigrations": sum(1 for row in rows if row.decision == ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED),
    }


def _stable_tool_order(tools: Iterable[ToolSpec]) -> list[ToolSpec]:
    def key(spec: ToolSpec) -> tuple[int, str, str]:
        source = spec.source.lower()
        dynamic = 1 if any(token in source for token in ("mcp", "plugin", "dynamic", "marketplace")) else 0
        return (dynamic, spec.name, spec.source)

    return sorted(tools, key=key)


def _access_mode_from_spec(spec: ToolSpec) -> ToolAccessMode:
    value = str(spec.metadata.get("access_mode") or "")
    try:
        return ToolAccessMode(value)
    except ValueError:
        return ToolAccessMode.UNKNOWN


def _metadata_bool(metadata: Mapping[str, str], key: str, *, default: bool) -> bool:
    value = metadata.get(key)
    if value is None:
        return default
    return str(value).lower() == "true"


def _mapping_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {"value": to_jsonable(value)}


def _result_chars(result: ToolResult) -> int:
    return len(json.dumps(to_jsonable(result.output), ensure_ascii=False, sort_keys=True))


def _with_result_metadata(result: ToolResult, metadata: Mapping[str, str]) -> ToolResult:
    return ToolResult(
        tool_call_id=result.tool_call_id,
        ok=result.ok,
        summary=result.summary,
        output=result.output,
        artifacts=result.artifacts,
        error=result.error,
        completed_at=result.completed_at,
        metadata={**result.metadata, **{str(key): str(value) for key, value in metadata.items()}},
    )


def _with_receipt_applications(
    receipt: ToolExecutionReceipt,
    applications: Sequence[Mapping[str, Any]],
) -> ToolExecutionReceipt:
    return ToolExecutionReceipt(
        request=receipt.request,
        raw_result=receipt.raw_result,
        bounded_result=receipt.bounded_result,
        budget_decision=receipt.budget_decision,
        budget_receipt=receipt.budget_receipt,
        failure_signal=receipt.failure_signal,
        context_modifiers=receipt.context_modifiers,
        modifier_applications=tuple(dict(item) for item in applications),
        started_at=receipt.started_at,
        completed_at=receipt.completed_at,
        executor_name=receipt.executor_name,
    )


def _modifiers_for_result(
    *,
    request: ToolLoopRequest,
    raw_result: ToolResult,
    bounded_result: ToolResult,
    budget_receipt: ToolBudgetReceipt,
) -> list[ToolContextModifier]:
    modifiers: list[ToolContextModifier] = [
        ToolContextModifier(
            kind=ToolContextModifierKind.MESSAGE_APPEND,
            tool_call_id=request.call.tool_call_id,
            key=f"{request.turn_index}:{request.step_index}:{request.call.tool_call_id}",
            value={
                "role": "tool",
                "tool_call_id": request.call.tool_call_id,
                "tool_name": request.tool_name,
                "summary": bounded_result.summary,
                "ok": bounded_result.ok,
                "error": bounded_result.error,
            },
            phase="after_tool_result",
        )
    ]
    if request.tool_name == "file_read" and raw_result.ok:
        key = str(raw_result.output.get("relative_path") or raw_result.output.get("path") or request.arguments.get("path") or "")
        modifiers.append(
            ToolContextModifier(
                kind=ToolContextModifierKind.READ_FILE_STATE,
                tool_call_id=request.call.tool_call_id,
                key=key,
                value={
                    "tool_call_id": request.call.tool_call_id,
                    "chars": raw_result.output.get("chars"),
                    "truncated": raw_result.output.get("truncated"),
                    "artifact_ids": [artifact.artifact_id for artifact in raw_result.artifacts],
                },
                phase="after_read_tool",
            )
        )
    if request.tool_name in {"file_write", "file_edit"} and raw_result.ok:
        key = str(raw_result.output.get("relative_path") or raw_result.output.get("path") or request.arguments.get("path") or "")
        modifiers.append(
            ToolContextModifier(
                kind=ToolContextModifierKind.CONTENT_REPLACEMENT,
                tool_call_id=request.call.tool_call_id,
                key=key,
                value={
                    "tool_call_id": request.call.tool_call_id,
                    "tool_name": request.tool_name,
                    "summary": raw_result.summary,
                    "output": raw_result.output,
                },
                phase="after_mutating_tool",
            )
        )
    if bounded_result.metadata.get("permission_effect") in {"ask", "deny"} or bounded_result.error in {
        "permission_required",
        "permission_denied",
    }:
        modifiers.append(
            ToolContextModifier(
                kind=ToolContextModifierKind.PERMISSION_HANDOFF,
                tool_call_id=request.call.tool_call_id,
                key=request.call.tool_call_id,
                value={
                    "tool_call_id": request.call.tool_call_id,
                    "tool_name": request.tool_name,
                    "permission_effect": bounded_result.metadata.get("permission_effect", ""),
                    "permission_request_id": bounded_result.metadata.get("permission_request_id", ""),
                    "error": bounded_result.error,
                },
                phase="permission_result",
            )
        )
    if budget_receipt.decision.applied:
        modifiers.append(
            ToolContextModifier(
                kind=ToolContextModifierKind.BUDGET_LEDGER,
                tool_call_id=request.call.tool_call_id,
                key=request.call.tool_call_id,
                value=budget_receipt.decision.to_dict(),
                phase="after_budget_shaping",
            )
        )
    for artifact in bounded_result.artifacts:
        modifiers.append(
            ToolContextModifier(
                kind=ToolContextModifierKind.ARTIFACT_REF,
                tool_call_id=request.call.tool_call_id,
                key=artifact.artifact_id,
                value=artifact.artifact_id,
                phase="after_tool_result",
            )
        )
    return modifiers
