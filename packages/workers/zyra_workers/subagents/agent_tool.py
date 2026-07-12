from __future__ import annotations

"""Worker-visible Agent/Task tools backed by the durable SubagentRuntime."""

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_core import new_id
from zyra_runtime import (
    DynamicToolProvenance,
    ProvenancedDynamicHandler,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)

from .models import (
    AgentContextMode,
    AgentExecutionMode,
    PermissionMode,
    SubagentSpawnRequest,
)
from .parent_scope import ParentExecutionScopeSnapshot
from .runtime import SubagentRuntime


class AgentToolError(RuntimeError):
    pass


class AgentToolScopeMismatch(AgentToolError, PermissionError):
    pass


class AgentToolInputError(AgentToolError, ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AgentToolParentContext:
    run_id: str
    task_id: str
    session_id: str
    worker_request_id: str
    workspace_root: str
    parent_scope: ParentExecutionScopeSnapshot
    context_payload: Mapping[str, Any]
    root_task_id: str
    node_id: str = ""

    def __post_init__(self) -> None:
        if not all((self.run_id, self.task_id, self.session_id, self.worker_request_id)):
            raise ValueError("Agent tool parent identity is incomplete")
        if self.parent_scope.run_id != self.run_id:
            raise ValueError("Agent tool scope run identity mismatch")
        if self.parent_scope.parent_task_id != self.task_id:
            raise ValueError("Agent tool scope task identity mismatch")
        if self.parent_scope.parent_session_id != self.session_id:
            raise ValueError("Agent tool scope session identity mismatch")


@dataclass(frozen=True, slots=True)
class AgentToolBinding:
    registry: ToolRegistry
    handlers: Mapping[str, ProvenancedDynamicHandler]
    tool_names: tuple[str, ...]
    scope_snapshot_id: str


class AgentToolRuntime:
    """Translate a model tool call into one canonical logical child task.

    The binding is immutable for one WorkerRequest.  Its parent ceiling is a
    server-signed snapshot captured before the model sees either tool schema,
    which prevents arguments from becoming an authority channel.
    """

    TOOL_NAMES = ("Agent", "Task")

    def __init__(
        self,
        runtime: SubagentRuntime,
        parent: AgentToolParentContext,
        *,
        maximum_prompt_chars: int = 64_000,
        disabled: bool = False,
    ) -> None:
        self.runtime = runtime
        self.parent = parent
        self.maximum_prompt_chars = max(1024, int(maximum_prompt_chars))
        self.disabled = bool(disabled)

    def bind(self, base_registry: ToolRegistry) -> AgentToolBinding:
        self._require_enabled()
        specs: list[ToolSpec] = []
        handlers: dict[str, ProvenancedDynamicHandler] = {}
        for name in self.TOOL_NAMES:
            provenance = DynamicToolProvenance(
                tool_name=name,
                namespace="zyra-subagent",
                version="M1-S03D-02",
                handler_kind="logical-child-runtime",
                external_boundary=False,
                requires_exact_grant=True,
                source="zyra_workers.subagents.agent_tool",
            )
            specs.append(self._spec(name, provenance))
            handlers[name] = ProvenancedDynamicHandler(provenance, self.handle)
        return AgentToolBinding(
            registry=base_registry.merged(specs, keep_existing=True),
            handlers=handlers,
            tool_names=self.TOOL_NAMES,
            scope_snapshot_id=self.parent.parent_scope.snapshot_id,
        )

    def handle(self, call: ToolCall) -> ToolResult:
        try:
            self._require_enabled()
            self._assert_call_identity(call)
            request = self._spawn_request(call)
            result = self.runtime.spawn(request)
            record = result.record
            output = {
                "schema": "zyra.agent-tool-result/v1",
                "logical_task_id": record.task_id,
                "parent_task_id": record.parent_task_id,
                "child_session_id": str(record.metadata.get("child_session_id") or ""),
                "status": record.status.value,
                "background": result.background,
                "replayed": result.replayed,
                "handoff": record.handoff.to_dict() if record.handoff else None,
                "usage": record.usage.to_dict(),
                "execution_ref": record.execution_ref,
                "scope_snapshot_id": self.parent.parent_scope.snapshot_id,
                "state_owner": "SubagentRuntime",
                "physical_worker_identity_allocated": False,
            }
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=result.ok,
                summary=(
                    f"Logical child {record.task_id} accepted in background."
                    if result.background
                    else f"Logical child {record.task_id} finished with status {record.status.value}."
                ),
                output=output,
                artifacts=list(record.handoff.artifacts) if record.handoff else [],
                error=None if result.ok else str(record.error_message or record.error_code or "subagent_failed"),
                metadata={
                    "owner_unit": "M1-S03D-02",
                    "logical_subagent": "true",
                    "scope_snapshot_id": self.parent.parent_scope.snapshot_id,
                },
            )
        except Exception as error:  # Tool boundary returns a bounded failure.
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="Agent tool rejected the logical child request.",
                output={
                    "schema": "zyra.agent-tool-result/v1",
                    "error_type": type(error).__name__,
                    "scope_snapshot_id": self.parent.parent_scope.snapshot_id,
                },
                error=str(error),
                metadata={"owner_unit": "M1-S03D-02", "logical_subagent": "true"},
            )

    def _spawn_request(self, call: ToolCall) -> SubagentSpawnRequest:
        raw = copy.deepcopy(call.arguments)
        prompt = str(raw.get("prompt") or raw.get("description") or "").strip()
        if not prompt:
            raise AgentToolInputError("Agent/Task requires a non-empty prompt")
        if len(prompt) > self.maximum_prompt_chars:
            raise AgentToolInputError("Agent/Task prompt exceeds the bounded input size")
        scope = self.parent.parent_scope
        requested_tools = self._tokens(raw.get("tools") or raw.get("requested_tools") or ())
        requested_mcp = self._tokens(raw.get("mcp_servers") or raw.get("requested_mcp_servers") or ())
        requested_permission = self._optional_enum(PermissionMode, raw.get("permission_mode"))
        execution_mode = self._enum(
            AgentExecutionMode,
            raw.get("execution_mode") or ("background" if raw.get("background") else "foreground"),
            AgentExecutionMode.FOREGROUND,
        )
        context_mode = self._enum(
            AgentContextMode,
            raw.get("context_mode") or "isolated",
            AgentContextMode.ISOLATED,
        )
        constraints = self._constraints(raw)
        idempotency_key = str(raw.get("idempotency_key") or self._idempotency_key(call, prompt))
        task_id = str(raw.get("task_id") or new_id("subagenttask"))
        return SubagentSpawnRequest(
            run_id=self.parent.run_id,
            parent_task_id=self.parent.task_id,
            parent_session_id=self.parent.session_id,
            parent_worker_request_id=self.parent.worker_request_id,
            agent_type=str(raw.get("agent_type") or raw.get("subagent_type") or "general-purpose"),
            prompt=prompt,
            parent_tools=scope.tool_names,
            parent_permission_mode=scope.permission_mode,
            requested_tools=requested_tools,
            requested_permission_mode=requested_permission,
            available_mcp_servers=scope.mcp_servers,
            requested_mcp_servers=requested_mcp,
            context_mode=context_mode,
            execution_mode=execution_mode,
            workspace_root=scope.workspace_root,
            requested_cwd=str(raw.get("cwd") or ""),
            parent_scope_snapshot_id=scope.snapshot_id,
            context_payload=self._context_payload(raw),
            constraints=constraints,
            idempotency_key=idempotency_key,
            task_id=task_id,
            metadata={
                "root_task_id": self.parent.root_task_id,
                "node_id": self.parent.node_id,
                "origin": "AgentTool",
                "tool_call_id": call.tool_call_id,
                "parent_scope_snapshot_id": scope.snapshot_id,
                "expected_parent_session_revision": scope.session_revision,
                "expected_parent_permission_revision": scope.permission_revision,
                "expected_parent_tool_generation": scope.tool_generation,
                "expected_parent_mcp_generations": dict(scope.mcp_catalog_generations),
            },
        )

    def _context_payload(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        inherited = copy.deepcopy(dict(self.parent.context_payload))
        # A model may request fewer references but cannot supply a new parent
        # transcript, system prompt, permission denial set, or cache prefix.
        selected_refs = set(self._tokens(raw.get("artifact_refs") or ()))
        artifacts = list(inherited.get("artifact_refs") or ())
        if selected_refs:
            artifacts = [item for item in artifacts if self._ref(item) in selected_refs]
        inherited["artifact_refs"] = artifacts
        inherited["parent_permission_rule_ids"] = list(self.parent.parent_scope.permission_rule_ids)
        inherited["parent_permission_denials"] = list(self.parent.parent_scope.deny_rule_ids)
        inherited["context_epoch"] = self.parent.parent_scope.context_epoch
        inherited["compact_boundary_id"] = self.parent.parent_scope.compact_boundary_id
        return inherited

    def _constraints(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        scope = self.parent.parent_scope
        values: dict[str, Any] = {
            "model_name": str(raw.get("model_name") or raw.get("model") or scope.effective_model),
            "effort": str(raw.get("effort") or "default"),
            "network_allowed": bool(raw.get("network_allowed", False)),
            "writable_paths": list(self._tokens(raw.get("writable_paths") or ())),
            "read_only_paths": list(self._tokens(raw.get("read_only_paths") or ())),
            "agent_skills": list(self._tokens(raw.get("skills") or ())),
            "agent_hooks": list(self._tokens(raw.get("hooks") or ())),
        }
        for name in (
            "max_turns",
            "max_tool_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_result_chars",
            "max_wall_time_ms",
            "max_children",
            "max_depth",
        ):
            if raw.get(name) is not None:
                values[name] = max(0, int(raw[name]))
        if isinstance(raw.get("typed_yield_schema"), Mapping):
            values["typed_yield_schema"] = copy.deepcopy(dict(raw["typed_yield_schema"]))
            values["typed_yield_required"] = True
        return values

    def _assert_call_identity(self, call: ToolCall) -> None:
        if call.tool_name not in self.TOOL_NAMES:
            raise AgentToolInputError("unsupported Agent tool alias")
        if (call.run_id, call.task_id) != (self.parent.run_id, self.parent.task_id):
            raise AgentToolScopeMismatch("Agent tool call crosses its bound parent identity")

    @classmethod
    def _spec(cls, name: str, provenance: DynamicToolProvenance) -> ToolSpec:
        return ToolSpec(
            name=name,
            purpose="Create one bounded logical child task with isolated or forked context and durable handoff.",
            source="claude-code-best AgentTool + oh-my-pi task isolation, internalized by M1-S03D-02",
            input_schema={
                "type": "object",
                "required": ["prompt"],
                "additionalProperties": False,
                "properties": {
                    "prompt": {"type": "string", "minLength": 1},
                    "agent_type": {"type": "string"},
                    "execution_mode": {"type": "string", "enum": ["foreground", "background"]},
                    "context_mode": {"type": "string", "enum": ["isolated", "fork", "resume"]},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "permission_mode": {"type": "string"},
                    "mcp_servers": {"type": "array", "items": {"type": "string"}},
                    "model_name": {"type": "string"},
                    "effort": {"type": "string"},
                    "artifact_refs": {"type": "array", "items": {"type": "string"}},
                    "typed_yield_schema": {"type": "object"},
                    "idempotency_key": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "required": ["logical_task_id", "status", "background"],
                "properties": {
                    "logical_task_id": {"type": "string"},
                    "status": {"type": "string"},
                    "background": {"type": "boolean"},
                    "handoff": {"type": ["object", "null"]},
                },
            },
            metadata={
                "access_mode": "stateful",
                "read_only": "false",
                "concurrency_safe": "true",
                "requires_exact_grant": "true",
                "logical_child_only": "true",
                "physical_lease_owner": "M1-07A",
                "source_path": "src/tools/AgentTool",
            },
            execution_provenance=provenance,
        )

    @staticmethod
    def _tokens(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, Sequence):
            return ()
        return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @staticmethod
    def _ref(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("artifact_id") or value.get("ref") or "")
        return str(value)

    @staticmethod
    def _enum(enum_type: Any, value: Any, default: Any) -> Any:
        try:
            return enum_type(str(getattr(value, "value", value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _optional_enum(enum_type: Any, value: Any) -> Any | None:
        if value in (None, ""):
            return None
        try:
            return enum_type(str(getattr(value, "value", value)))
        except (TypeError, ValueError) as error:
            raise AgentToolInputError(f"invalid {enum_type.__name__}: {value}") from error

    def _idempotency_key(self, call: ToolCall, prompt: str) -> str:
        value = json.dumps(
            {
                "run_id": call.run_id,
                "task_id": call.task_id,
                "tool_call_id": call.tool_call_id,
                "prompt": prompt,
                "scope_snapshot_id": self.parent.parent_scope.snapshot_id,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "agenttool:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _require_enabled(self) -> None:
        if self.disabled:
            raise AgentToolError("AgentToolRuntime is disabled")


__all__ = [
    "AgentToolBinding",
    "AgentToolError",
    "AgentToolInputError",
    "AgentToolParentContext",
    "AgentToolRuntime",
    "AgentToolScopeMismatch",
]
