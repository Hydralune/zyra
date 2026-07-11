from __future__ import annotations

"""Projection of MCP tool descriptors into Zyra's 02C tool runtime."""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from zyra_core import EventRecord, new_id, now_iso
from zyra_runtime import (
    DynamicToolProvenance,
    ProvenancedDynamicHandler,
    ToolCall,
    ToolResult,
    ToolSpec,
)

from .capabilities import McpCapabilityCatalog, McpCapabilitySnapshot
from .models import JsonValue, McpRiskClass, McpToolDescriptor, redact_value, stable_digest
from .output import McpOutputBudgetRuntime, McpOutputReceipt


class McpProjectionError(RuntimeError):
    pass


class McpProjectionRuntimeDisabled(McpProjectionError):
    pass


class McpToolInvokePort(Protocol):
    def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        progress: Callable[[Mapping[str, Any]], None] | None = None,
        task_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class McpProjectionDecision:
    server_id: str
    remote_name: str
    local_name: str
    accepted: bool
    reason: str
    schema_fingerprint: str
    connection_generation: int
    capability_generation: int
    read_only: bool
    concurrency_safe: bool
    destructive: bool
    open_world: bool
    risk_tags: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "remote_name": self.remote_name,
            "local_name": self.local_name,
            "accepted": self.accepted,
            "reason": self.reason,
            "schema_fingerprint": self.schema_fingerprint,
            "connection_generation": self.connection_generation,
            "capability_generation": self.capability_generation,
            "read_only": self.read_only,
            "concurrency_safe": self.concurrency_safe,
            "destructive": self.destructive,
            "open_world": self.open_world,
            "risk_tags": list(self.risk_tags),
        }


@dataclass(frozen=True, slots=True)
class McpProjectionBundle:
    bundle_id: str
    tool_specs: tuple[ToolSpec, ...]
    handlers: Mapping[str, Callable[[ToolCall], ToolResult]]
    decisions: tuple[McpProjectionDecision, ...]
    server_generations: Mapping[str, tuple[int, int]]
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_specs", tuple(self.tool_specs))
        object.__setattr__(self, "handlers", MappingProxyType(dict(self.handlers)))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(
            self,
            "server_generations",
            MappingProxyType(
                {
                    str(server_id): (int(value[0]), int(value[1]))
                    for server_id, value in self.server_generations.items()
                }
            ),
        )

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.tool_specs)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "bundle_id": self.bundle_id,
            "tool_names": list(self.tool_names),
            "decisions": [item.to_dict() for item in self.decisions],
            "server_generations": {
                key: {"connection": value[0], "capability": value[1]}
                for key, value in self.server_generations.items()
            },
            "created_at": self.created_at,
        }


class McpToolProjectionRuntime:
    """Build an immutable ToolSpec + handler snapshot.

    The handler is deliberately dumb about permission.  It can only be reached
    through ``ToolExecutionRuntime`` after 03A has minted and the executor has
    consumed an exact-call grant.  Direct calls are rejected by the generic
    dynamic dispatcher in ``ToolExecutor``.
    """

    def __init__(
        self,
        catalog: McpCapabilityCatalog,
        invoker: McpToolInvokePort,
        output_runtime: McpOutputBudgetRuntime,
        *,
        event_sink: Callable[[EventRecord], None] | None = None,
        progress_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.catalog = catalog
        self.invoker = invoker
        self.output_runtime = output_runtime
        self.event_sink = event_sink
        self.progress_sink = progress_sink
        self.disabled = disabled
        self._lock = threading.RLock()

    def build_bundle(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        reserved_names: Sequence[str] = (),
        include_servers: Sequence[str] | None = None,
    ) -> McpProjectionBundle:
        if self.disabled:
            raise McpProjectionRuntimeDisabled("McpToolProjectionRuntime is disabled")
        reserved = set(reserved_names)
        selected = set(include_servers or ())
        specs: list[ToolSpec] = []
        handlers: dict[str, Callable[[ToolCall], ToolResult]] = {}
        decisions: list[McpProjectionDecision] = []
        server_generations: dict[str, tuple[int, int]] = {}

        for snapshot in self.catalog.list():
            if selected and snapshot.server_id not in selected:
                continue
            server_generations[snapshot.server_id] = (
                snapshot.connection_generation,
                snapshot.generation,
            )
            for descriptor in snapshot.tools:
                if descriptor.local_name in reserved or descriptor.local_name in handlers:
                    decisions.append(
                        self._decision(
                            snapshot,
                            descriptor,
                            accepted=False,
                            reason="tool name collides with an existing registry entry",
                        )
                    )
                    continue
                spec = self._tool_spec(snapshot, descriptor)
                specs.append(spec)
                provenance = spec.execution_provenance
                if provenance is None:
                    raise McpProjectionError(
                        f"MCP projection lost dynamic provenance for {spec.name}"
                    )
                handlers[spec.name] = ProvenancedDynamicHandler(
                    provenance=provenance,
                    _handler=self._handler(
                        snapshot,
                        descriptor,
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                    ),
                )
                reserved.add(spec.name)
                decisions.append(
                    self._decision(snapshot, descriptor, accepted=True, reason="projected")
                )

        if any(not item.accepted for item in decisions):
            rejected = [item.local_name for item in decisions if not item.accepted]
            raise McpProjectionError(
                "MCP projection contains tool-name collisions: " + ", ".join(sorted(rejected))
            )
        return McpProjectionBundle(
            bundle_id=new_id("mcpprojection"),
            tool_specs=tuple(specs),
            handlers=handlers,
            decisions=tuple(decisions),
            server_generations=server_generations,
        )

    def _tool_spec(
        self,
        snapshot: McpCapabilitySnapshot,
        descriptor: McpToolDescriptor,
    ) -> ToolSpec:
        annotations = dict(descriptor.annotations)
        read_only = annotations.get("readOnlyHint") is True
        destructive = annotations.get("destructiveHint") is True
        open_world = annotations.get("openWorldHint") is not False
        idempotent = annotations.get("idempotentHint") is True
        # Unknown MCP annotations stay conservative: serial, open-world and
        # subject to a network permission decision.
        concurrency_safe = read_only and not destructive and idempotent
        risk_tags = list(_risk_tags(descriptor, read_only, destructive, open_world))
        capabilities = ["network", "mcp"]
        if read_only:
            capabilities.append("read_only")
        if destructive:
            capabilities.append("destructive")
        if open_world:
            capabilities.append("open_world")
        metadata = {
            "access_mode": "read_only" if read_only else "control",
            "read_only": str(read_only).lower(),
            "concurrency_safe": str(concurrency_safe).lower(),
            "mutates_workspace": "false",
            "destructive": str(destructive).lower(),
            "open_world": str(open_world).lower(),
            "idempotent": str(idempotent).lower(),
            "tool_namespace": "mcp",
            "namespace": "mcp",
            "server_id": descriptor.server_id,
            "server_name": descriptor.server_id,
            "mcp_tool_name": descriptor.remote_name,
            "mcp_tool_identity": descriptor.identity,
            "mcp_schema_fingerprint": descriptor.schema_fingerprint,
            "mcp_projection_revision": str(descriptor.projection_revision),
            "mcp_connection_generation": str(snapshot.connection_generation),
            "mcp_capability_generation": str(snapshot.generation),
            "mcp_task_support": str(descriptor.task_support),
            "capabilities": ",".join(capabilities),
            "risk_tags": ",".join(risk_tags),
            "requires_interaction": "false",
            "source_path": "claude-code-best/src/tools/MCPTool/MCPTool.ts",
            "projection_source": "zyra_integrations.mcp.projection.McpToolProjectionRuntime",
        }
        return ToolSpec(
            name=descriptor.local_name,
            purpose=descriptor.description or f"MCP tool {descriptor.remote_name} from {descriptor.server_id}",
            source=f"mcp:{descriptor.server_id}",
            input_schema=dict(descriptor.input_schema),
            output_schema=dict(descriptor.output_schema),
            metadata=metadata,
            execution_provenance=DynamicToolProvenance(
                tool_name=descriptor.local_name,
                namespace="mcp",
                server_id=descriptor.server_id,
                handler_kind="mcp_json_rpc_tool",
                external_boundary=True,
                requires_exact_grant=True,
                source="zyra_integrations.mcp.projection",
            ),
        )

    def _handler(
        self,
        snapshot: McpCapabilitySnapshot,
        descriptor: McpToolDescriptor,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> Callable[[ToolCall], ToolResult]:
        expected_server = descriptor.server_id
        expected_remote_name = descriptor.remote_name
        expected_local_name = descriptor.local_name
        expected_schema = descriptor.schema_fingerprint
        expected_projection_revision = descriptor.projection_revision
        expected_connection_generation = snapshot.connection_generation
        expected_capability_generation = snapshot.generation

        def execute(call: ToolCall) -> ToolResult:
            with self._lock:
                current = self.catalog.get(expected_server)
                if current is None:
                    return _stale_result(call, "MCP server capability snapshot is unavailable")
                current_tool = next(
                    (item for item in current.tools if item.local_name == expected_local_name),
                    None,
                )
                if current_tool is None:
                    return _stale_result(call, "MCP tool was removed by list_changed refresh")
                if current.connection_generation != expected_connection_generation:
                    return _stale_result(call, "MCP connection generation changed; rematerialize tool registry")
                if current.generation != expected_capability_generation:
                    return _stale_result(call, "MCP capability generation changed; rematerialize tool registry")
                if (
                    current_tool.schema_fingerprint != expected_schema
                    or current_tool.projection_revision != expected_projection_revision
                    or current_tool.remote_name != expected_remote_name
                ):
                    return _stale_result(call, "MCP tool schema or identity changed")

            progress_events: list[Mapping[str, Any]] = []

            def progress(value: Mapping[str, Any]) -> None:
                safe = redact_value(value)
                if isinstance(safe, Mapping):
                    progress_events.append(safe)
                    if self.progress_sink is not None:
                        self.progress_sink(call.tool_call_id, safe)

            try:
                raw_result = self.invoker.call_tool(
                    expected_server,
                    expected_remote_name,
                    call.arguments,
                    progress=progress,
                    task_metadata={
                        "run_id": run_id,
                        "task_id": task_id,
                        "node_id": node_id or "",
                        "tool_call_id": call.tool_call_id,
                        "projection_revision": expected_projection_revision,
                    },
                )
                receipt = self.output_runtime.normalize_tool_result(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    server_id=expected_server,
                    tool_name=expected_remote_name,
                    tool_call_id=call.tool_call_id,
                    raw_result=raw_result,
                )
            except Exception as error:  # noqa: BLE001 - become an auditable ToolResult.
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    ok=False,
                    summary=f"MCP tool {expected_server}/{expected_remote_name} failed",
                    error="mcp_runtime_error",
                    metadata={
                        "tool_namespace": "mcp",
                        "server_id": expected_server,
                        "mcp_tool_name": expected_remote_name,
                        "exception_type": type(error).__name__,
                        "message": str(error)[:1000],
                        "mcp_invoke_started": "true",
                    },
                )
            result = _with_progress(receipt.result, progress_events)
            if self.event_sink is not None:
                self.event_sink(
                    self.output_runtime.event_for_receipt(
                        receipt,
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        connection_generation=expected_connection_generation,
                        capability_generation=expected_capability_generation,
                    )
                )
            return result

        return execute

    @staticmethod
    def _decision(
        snapshot: McpCapabilitySnapshot,
        descriptor: McpToolDescriptor,
        *,
        accepted: bool,
        reason: str,
    ) -> McpProjectionDecision:
        annotations = dict(descriptor.annotations)
        read_only = annotations.get("readOnlyHint") is True
        destructive = annotations.get("destructiveHint") is True
        open_world = annotations.get("openWorldHint") is not False
        concurrency_safe = read_only and not destructive and annotations.get("idempotentHint") is True
        return McpProjectionDecision(
            server_id=descriptor.server_id,
            remote_name=descriptor.remote_name,
            local_name=descriptor.local_name,
            accepted=accepted,
            reason=reason,
            schema_fingerprint=descriptor.schema_fingerprint,
            connection_generation=snapshot.connection_generation,
            capability_generation=snapshot.generation,
            read_only=read_only,
            concurrency_safe=concurrency_safe,
            destructive=destructive,
            open_world=open_world,
            risk_tags=tuple(_risk_tags(descriptor, read_only, destructive, open_world)),
        )


def _risk_tags(
    descriptor: McpToolDescriptor,
    read_only: bool,
    destructive: bool,
    open_world: bool,
) -> tuple[str, ...]:
    values = ["mcp", "network"]
    if descriptor.risk is not McpRiskClass.UNKNOWN:
        values.append(str(descriptor.risk))
    if not read_only:
        values.append("side_effect_unknown")
    if destructive:
        values.append("destructive")
    if open_world:
        values.append("open_world")
    return tuple(dict.fromkeys(values))


def _stale_result(call: ToolCall, reason: str) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        ok=False,
        summary=reason,
        error="mcp_projection_stale",
        metadata={
            "tool_namespace": "mcp",
            "mcp_projection_stale": "true",
            "mcp_invoke_started": "false",
        },
    )


def _with_progress(
    result: ToolResult,
    progress: Sequence[Mapping[str, Any]],
) -> ToolResult:
    return ToolResult(
        tool_call_id=result.tool_call_id,
        ok=result.ok,
        summary=result.summary,
        output=result.output,
        artifacts=result.artifacts,
        error=result.error,
        completed_at=result.completed_at,
        metadata={
            **result.metadata,
            "mcp_progress_event_count": str(len(progress)),
            "mcp_progress_digest": stable_digest(list(progress)) if progress else "",
            "mcp_invoke_started": "true",
        },
    )


__all__ = [
    "McpProjectionBundle",
    "McpProjectionDecision",
    "McpProjectionError",
    "McpProjectionRuntimeDisabled",
    "McpToolInvokePort",
    "McpToolProjectionRuntime",
]
