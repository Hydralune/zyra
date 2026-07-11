from __future__ import annotations

"""Generation-bound MCP resource tools and prompt command descriptors.

This module closes the capability-projection gap without creating another MCP
client, catalog, permission store, or command store.  It reads immutable
snapshots from :class:`McpCapabilityCatalog`, delegates I/O to the existing
connection runtime, and emits ordinary 02C dynamic tools.  Every executable
closure captures both connection and capability generations and fails closed
when either changes.
"""

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, new_id, now_iso
from zyra_runtime import (
    DynamicToolProvenance,
    ProvenancedDynamicHandler,
    ToolCall,
    ToolResult,
    ToolSpec,
)

from .capabilities import McpCapabilityCatalog, McpCapabilitySnapshot
from .models import (
    JsonValue,
    McpPromptDescriptor,
    McpResourceDescriptor,
    canonical_json,
    redact_value,
    stable_digest,
    to_json_value,
)


class McpResourceProjectionError(RuntimeError):
    """Base error for resource and prompt projection."""


class McpResourceProjectionDisabled(McpResourceProjectionError):
    pass


class McpResourceProjectionCollision(McpResourceProjectionError):
    pass


class McpResourceProjectionStale(McpResourceProjectionError):
    pass


class McpResourceConnectionPort(Protocol):
    def snapshot(self, server_id: str) -> Any: ...

    def read_resource(
        self,
        server_id: str,
        uri: str,
        **context: Any,
    ) -> Any: ...

    def get_prompt(
        self,
        server_id: str,
        name: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class McpResourceOutputPort(Protocol):
    def event_for_receipt(self, receipt: Any, **context: Any) -> EventRecord: ...


@dataclass(frozen=True, slots=True)
class McpResourceProjectionPolicy:
    """Limits applied before resource metadata reaches a model-visible tool."""

    max_resources_per_list: int = 500
    max_description_chars: int = 2_048
    max_uri_chars: int = 8_192
    max_filter_chars: int = 512
    expose_resource_templates: bool = True
    emit_projection_events: bool = True

    def __post_init__(self) -> None:
        if self.max_resources_per_list <= 0:
            raise ValueError("max_resources_per_list must be positive")
        if self.max_description_chars <= 0:
            raise ValueError("max_description_chars must be positive")
        if self.max_uri_chars <= 0:
            raise ValueError("max_uri_chars must be positive")
        if self.max_filter_chars <= 0:
            raise ValueError("max_filter_chars must be positive")


@dataclass(frozen=True, slots=True)
class McpGenerationLease:
    server_id: str
    connection_generation: int
    capability_generation: int
    snapshot_digest: str

    def __post_init__(self) -> None:
        if not self.server_id:
            raise ValueError("server_id is required")
        if self.connection_generation < 0 or self.capability_generation < 0:
            raise ValueError("generations cannot be negative")

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "connection_generation": self.connection_generation,
            "capability_generation": self.capability_generation,
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class McpResourceProjectionDecision:
    server_id: str
    surface: str
    remote_identity: str
    local_name: str
    accepted: bool
    reason: str
    lease: McpGenerationLease
    descriptor_digest: str

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "surface": self.surface,
            "remote_identity": self.remote_identity,
            "local_name": self.local_name,
            "accepted": self.accepted,
            "reason": self.reason,
            "lease": self.lease.safe_dict(),
            "descriptor_digest": self.descriptor_digest,
        }


@dataclass(frozen=True, slots=True)
class McpPromptCommandArgument:
    name: str
    description: str
    required: bool
    value_schema: Mapping[str, JsonValue] = field(
        default_factory=lambda: {"type": "string"}
    )

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("prompt command argument name is required")
        object.__setattr__(self, "value_schema", MappingProxyType(dict(self.value_schema)))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "schema": dict(self.value_schema),
        }


@dataclass(frozen=True, slots=True)
class McpPromptCommandDescriptor:
    """Transport-neutral command descriptor consumed by the 03D registry."""

    name: str
    server_id: str
    remote_name: str
    description: str
    arguments: tuple[McpPromptCommandArgument, ...]
    lease: McpGenerationLease
    source: str = "mcp"
    command_type: str = "prompt"
    user_invocable: bool = True
    remote_safe: bool = False
    untrusted_external_content: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.server_id or not self.remote_name:
            raise ValueError("prompt command identity is incomplete")
        object.__setattr__(self, "arguments", tuple(self.arguments))
        names = [item.name for item in self.arguments]
        if len(names) != len(set(names)):
            raise ValueError("prompt command argument names must be unique")

    @property
    def input_schema(self) -> dict[str, JsonValue]:
        properties = {
            item.name: {
                **dict(item.value_schema),
                "description": item.description,
            }
            for item in self.arguments
        }
        required = [item.name for item in self.arguments if item.required]
        schema: dict[str, JsonValue] = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "server_id": self.server_id,
            "remote_name": self.remote_name,
            "description": self.description,
            "arguments": [item.safe_dict() for item in self.arguments],
            "input_schema": self.input_schema,
            "lease": self.lease.safe_dict(),
            "source": self.source,
            "command_type": self.command_type,
            "user_invocable": self.user_invocable,
            "remote_safe": self.remote_safe,
            "untrusted_external_content": self.untrusted_external_content,
        }


@dataclass(frozen=True, slots=True)
class McpPromptCommandInvocation:
    command_name: str
    server_id: str
    remote_name: str
    arguments: Mapping[str, JsonValue]
    lease: McpGenerationLease
    result: Mapping[str, JsonValue]
    invoked_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))
        object.__setattr__(self, "result", MappingProxyType(dict(self.result)))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "command_name": self.command_name,
            "server_id": self.server_id,
            "remote_name": self.remote_name,
            "arguments": redact_value(self.arguments),
            "lease": self.lease.safe_dict(),
            "result": redact_value(self.result),
            "invoked_at": self.invoked_at,
            "untrusted_external_content": True,
        }


@dataclass(frozen=True, slots=True)
class McpResourceProjectionBundle:
    bundle_id: str
    tool_specs: tuple[ToolSpec, ...]
    handlers: Mapping[str, Callable[[ToolCall], ToolResult]]
    prompt_commands: tuple[McpPromptCommandDescriptor, ...]
    decisions: tuple[McpResourceProjectionDecision, ...]
    leases: Mapping[str, McpGenerationLease]
    session_id: str
    worker_request_id: str
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_specs", tuple(self.tool_specs))
        object.__setattr__(self, "handlers", MappingProxyType(dict(self.handlers)))
        object.__setattr__(self, "prompt_commands", tuple(self.prompt_commands))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "leases", MappingProxyType(dict(self.leases)))

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.tool_specs)

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.prompt_commands)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-resource-projection-bundle.v1",
            "bundle_id": self.bundle_id,
            "tool_names": list(self.tool_names),
            "prompt_commands": [item.safe_dict() for item in self.prompt_commands],
            "command_names": list(self.command_names),
            "decisions": [item.safe_dict() for item in self.decisions],
            "leases": {key: value.safe_dict() for key, value in self.leases.items()},
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "created_at": self.created_at,
            "dynamic_handler_count": len(self.handlers),
        }


class McpResourceProjectionRuntime:
    """Project resource list/read tools and MCP prompt commands.

    Permission remains owned by 03A and execution by 02C.  Handlers are wrapped
    in ``ProvenancedDynamicHandler`` and therefore require the same exact-call
    grant as normal MCP tools.
    """

    def __init__(
        self,
        catalog: McpCapabilityCatalog,
        connection_runtime: McpResourceConnectionPort,
        output_runtime: McpResourceOutputPort,
        *,
        event_sink: Callable[[EventRecord], None] | None = None,
        policy: McpResourceProjectionPolicy | None = None,
        disabled: bool = False,
    ) -> None:
        self.catalog = catalog
        self.connection_runtime = connection_runtime
        self.output_runtime = output_runtime
        self.event_sink = event_sink
        self.policy = policy or McpResourceProjectionPolicy()
        self.disabled = disabled
        self._lock = threading.RLock()

    def build_bundle(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
        reserved_names: Sequence[str] = (),
        include_servers: Sequence[str] | None = None,
    ) -> McpResourceProjectionBundle:
        self._assert_enabled()
        selected = frozenset(str(item) for item in (include_servers or ()) if str(item))
        occupied = set(str(item) for item in reserved_names)
        tools: list[ToolSpec] = []
        handlers: dict[str, Callable[[ToolCall], ToolResult]] = {}
        commands: list[McpPromptCommandDescriptor] = []
        decisions: list[McpResourceProjectionDecision] = []
        leases: dict[str, McpGenerationLease] = {}
        for snapshot in self.catalog.list():
            if selected and snapshot.server_id not in selected:
                continue
            lease = self._lease(snapshot)
            leases[snapshot.server_id] = lease
            if snapshot.resources or (
                self.policy.expose_resource_templates and snapshot.resource_templates
            ):
                list_spec = self._list_spec(snapshot, lease)
                read_spec = self._read_spec(snapshot, lease)
                for spec, kind in ((list_spec, "resource_list"), (read_spec, "resource_read")):
                    if spec.name in occupied:
                        raise McpResourceProjectionCollision(
                            f"MCP resource tool collides with registry entry: {spec.name}"
                        )
                    occupied.add(spec.name)
                    tools.append(spec)
                    handler = (
                        self._list_handler(snapshot, lease)
                        if kind == "resource_list"
                        else self._read_handler(
                            snapshot,
                            lease,
                            run_id=run_id,
                            task_id=task_id,
                            node_id=node_id,
                            session_id=session_id,
                            worker_request_id=worker_request_id,
                        )
                    )
                    provenance = spec.execution_provenance
                    if provenance is None:
                        raise McpResourceProjectionError("resource tool lost provenance")
                    handlers[spec.name] = ProvenancedDynamicHandler(provenance, handler)
                    decisions.append(
                        McpResourceProjectionDecision(
                            snapshot.server_id,
                            kind,
                            "resources/*",
                            spec.name,
                            True,
                            "projected",
                            lease,
                            stable_digest(spec.input_schema),
                        )
                    )
            for descriptor in snapshot.prompts:
                command = self._prompt_descriptor(snapshot, descriptor, lease)
                if command.name in occupied:
                    raise McpResourceProjectionCollision(
                        f"MCP prompt command collides with runtime surface: {command.name}"
                    )
                occupied.add(command.name)
                commands.append(command)
                decisions.append(
                    McpResourceProjectionDecision(
                        snapshot.server_id,
                        "prompt_command",
                        descriptor.remote_name,
                        command.name,
                        True,
                        "projected",
                        lease,
                        stable_digest(descriptor.to_dict()),
                    )
                )
        bundle = McpResourceProjectionBundle(
            new_id("mcpresourceprojection"),
            tuple(tools),
            handlers,
            tuple(commands),
            tuple(decisions),
            leases,
            session_id,
            worker_request_id,
        )
        self._emit_projection(bundle, run_id=run_id, task_id=task_id, node_id=node_id)
        return bundle

    def invoke_prompt(
        self,
        descriptor: McpPromptCommandDescriptor,
        arguments: Mapping[str, Any],
        *,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
    ) -> McpPromptCommandInvocation:
        self._assert_enabled()
        snapshot = self._require_lease(descriptor.lease)
        current = next(
            (
                item
                for item in snapshot.prompts
                if item.remote_name == descriptor.remote_name
            ),
            None,
        )
        if current is None:
            raise McpResourceProjectionStale("MCP prompt was removed by refresh")
        if current.projection_revision != descriptor.lease.capability_generation:
            raise McpResourceProjectionStale("MCP prompt projection revision changed")
        validated = current.validate_arguments(arguments)
        raw = self.connection_runtime.get_prompt(
            descriptor.server_id,
            descriptor.remote_name,
            validated,
        )
        result = to_json_value(raw)
        if not isinstance(result, Mapping):
            raise McpResourceProjectionError("MCP prompt result must be an object")
        invocation = McpPromptCommandInvocation(
            descriptor.name,
            descriptor.server_id,
            descriptor.remote_name,
            validated,
            descriptor.lease,
            result,
        )
        self._emit_prompt_invocation(
            invocation,
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
        )
        return invocation

    def validate_bundle(self, bundle: McpResourceProjectionBundle) -> None:
        self._assert_enabled()
        for lease in bundle.leases.values():
            self._require_lease(lease)
        handler_names = set(bundle.handlers)
        tool_names = set(bundle.tool_names)
        if handler_names != tool_names:
            raise McpResourceProjectionError("bundle handler/tool names diverged")
        if len(bundle.command_names) != len(set(bundle.command_names)):
            raise McpResourceProjectionError("bundle prompt command names collide")

    def _list_spec(
        self,
        snapshot: McpCapabilitySnapshot,
        lease: McpGenerationLease,
    ) -> ToolSpec:
        name = f"mcp__{_slug(snapshot.server_id)}__list_resources"
        return ToolSpec(
            name=name,
            purpose=f"List active resources exposed by MCP server {snapshot.server_id}.",
            source=f"mcp:{snapshot.server_id}:resources",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "maxLength": self.policy.max_filter_chars},
                    "mime_type": {"type": "string", "maxLength": 256},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.policy.max_resources_per_list,
                    },
                    "include_templates": {"type": "boolean"},
                },
            },
            output_schema={
                "type": "object",
                "required": ["resources", "generation"],
                "properties": {
                    "resources": {"type": "array"},
                    "resource_templates": {"type": "array"},
                    "generation": {"type": "object"},
                },
            },
            metadata=self._metadata(snapshot, lease, "resource_list"),
            execution_provenance=DynamicToolProvenance(
                tool_name=name,
                namespace="mcp",
                server_id=snapshot.server_id,
                version=str(lease.capability_generation),
                handler_kind="mcp_resource_list",
                external_boundary=True,
                requires_exact_grant=True,
                source="zyra_integrations.mcp.resource_projection",
            ),
        )

    def _read_spec(
        self,
        snapshot: McpCapabilitySnapshot,
        lease: McpGenerationLease,
    ) -> ToolSpec:
        name = f"mcp__{_slug(snapshot.server_id)}__read_resource"
        return ToolSpec(
            name=name,
            purpose=f"Read one active resource from MCP server {snapshot.server_id}.",
            source=f"mcp:{snapshot.server_id}:resources",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["uri"],
                "properties": {
                    "uri": {"type": "string", "maxLength": self.policy.max_uri_chars},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "content": {},
                    "artifacts": {"type": "array"},
                    "generation": {"type": "object"},
                },
            },
            metadata=self._metadata(snapshot, lease, "resource_read"),
            execution_provenance=DynamicToolProvenance(
                tool_name=name,
                namespace="mcp",
                server_id=snapshot.server_id,
                version=str(lease.capability_generation),
                handler_kind="mcp_resource_read",
                external_boundary=True,
                requires_exact_grant=True,
                source="zyra_integrations.mcp.resource_projection",
            ),
        )

    def _metadata(
        self,
        snapshot: McpCapabilitySnapshot,
        lease: McpGenerationLease,
        surface: str,
    ) -> dict[str, str]:
        return {
            "access_mode": "read_only",
            "read_only": "true",
            "concurrency_safe": "true",
            "mutates_workspace": "false",
            "destructive": "false",
            "open_world": "true",
            "idempotent": "true",
            "tool_namespace": "mcp",
            "namespace": "mcp",
            "server_id": snapshot.server_id,
            "mcp_surface": surface,
            "mcp_connection_generation": str(lease.connection_generation),
            "mcp_capability_generation": str(lease.capability_generation),
            "mcp_snapshot_digest": lease.snapshot_digest,
            "capabilities": "network,mcp,read_only",
            "risk_tags": "mcp,network,external_untrusted_content",
            "source_path": "claude-code-best/src/tools/ReadMcpResourceTool",
            "projection_source": "zyra_integrations.mcp.resource_projection",
        }

    def _list_handler(
        self,
        captured: McpCapabilitySnapshot,
        lease: McpGenerationLease,
    ) -> Callable[[ToolCall], ToolResult]:
        def execute(call: ToolCall) -> ToolResult:
            try:
                snapshot = self._require_lease(lease)
            except McpResourceProjectionStale as error:
                return _stale_result(call, str(error), lease)
            query = str(call.arguments.get("query") or "").strip().casefold()
            mime_type = str(call.arguments.get("mime_type") or "").strip().casefold()
            try:
                limit = int(call.arguments.get("limit") or self.policy.max_resources_per_list)
            except (TypeError, ValueError):
                return _invalid_result(call, "limit must be an integer", lease)
            limit = max(1, min(limit, self.policy.max_resources_per_list))
            resources: list[dict[str, JsonValue]] = []
            for item in snapshot.resources:
                if query and query not in " ".join((item.name, item.description, item.uri)).casefold():
                    continue
                if mime_type and item.mime_type.casefold() != mime_type:
                    continue
                resources.append(_resource_safe_dict(item, self.policy.max_description_chars))
                if len(resources) >= limit:
                    break
            include_templates = bool(call.arguments.get("include_templates", True))
            templates = (
                [redact_value(item) for item in snapshot.resource_templates[:limit]]
                if include_templates and self.policy.expose_resource_templates
                else []
            )
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=True,
                summary=f"Listed {len(resources)} MCP resources from {captured.server_id}",
                output={
                    "server_id": captured.server_id,
                    "resources": resources,
                    "resource_templates": templates,
                    "generation": lease.safe_dict(),
                    "untrusted_external_content": True,
                    "filtered": bool(query or mime_type),
                },
                metadata={
                    "tool_namespace": "mcp",
                    "mcp_surface": "resource_list",
                    "server_id": captured.server_id,
                    "mcp_invoke_started": "false",
                    "mcp_generation_validated": "true",
                },
            )

        return execute

    def _read_handler(
        self,
        captured: McpCapabilitySnapshot,
        lease: McpGenerationLease,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
    ) -> Callable[[ToolCall], ToolResult]:
        def execute(call: ToolCall) -> ToolResult:
            uri = str(call.arguments.get("uri") or "").strip()
            if not uri or len(uri) > self.policy.max_uri_chars:
                return _invalid_result(call, "uri is required or exceeds policy", lease)
            try:
                snapshot = self._require_lease(lease)
            except McpResourceProjectionStale as error:
                return _stale_result(call, str(error), lease)
            descriptor = next((item for item in snapshot.resources if item.uri == uri), None)
            if descriptor is None:
                return _stale_result(call, "resource is absent from active snapshot", lease)
            try:
                receipt = self.connection_runtime.read_resource(
                    captured.server_id,
                    uri,
                    run_id=run_id or call.run_id,
                    task_id=task_id or call.task_id,
                    node_id=node_id or call.node_id,
                    session_id=session_id,
                    worker_request_id=worker_request_id,
                    tool_call_id=call.tool_call_id,
                )
            except Exception as error:  # noqa: BLE001 - external failure becomes ToolResult.
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    ok=False,
                    summary=f"MCP resource read failed for {captured.server_id}",
                    error="mcp_resource_read_failed",
                    metadata={
                        "tool_namespace": "mcp",
                        "mcp_surface": "resource_read",
                        "server_id": captured.server_id,
                        "exception_type": type(error).__name__,
                        "message": str(error)[:1000],
                        "mcp_invoke_started": "true",
                    },
                )
            result = getattr(receipt, "result", None)
            if isinstance(result, ToolResult):
                selected = replace(
                    result,
                    tool_call_id=call.tool_call_id,
                    metadata={
                        **result.metadata,
                        "tool_namespace": "mcp",
                        "mcp_surface": "resource_read",
                        "server_id": captured.server_id,
                        "mcp_generation_validated": "true",
                        "mcp_invoke_started": "true",
                    },
                )
            else:
                safe = _receipt_safe_dict(receipt)
                selected = ToolResult(
                    tool_call_id=call.tool_call_id,
                    ok=True,
                    summary=f"Read MCP resource {descriptor.name}",
                    output={
                        "server_id": captured.server_id,
                        "uri": uri,
                        "receipt": safe,
                        "generation": lease.safe_dict(),
                        "untrusted_external_content": True,
                    },
                    metadata={
                        "tool_namespace": "mcp",
                        "mcp_surface": "resource_read",
                        "server_id": captured.server_id,
                        "mcp_generation_validated": "true",
                        "mcp_invoke_started": "true",
                    },
                )
            event_factory = getattr(self.output_runtime, "event_for_receipt", None)
            if callable(event_factory) and self.event_sink is not None:
                try:
                    event = event_factory(
                        receipt,
                        run_id=run_id or call.run_id,
                        task_id=task_id or call.task_id,
                        node_id=node_id or call.node_id,
                        connection_generation=lease.connection_generation,
                        capability_generation=lease.capability_generation,
                    )
                except Exception:
                    event = None
                if isinstance(event, EventRecord):
                    self.event_sink(event)
            return selected

        return execute

    def _prompt_descriptor(
        self,
        snapshot: McpCapabilitySnapshot,
        descriptor: McpPromptDescriptor,
        lease: McpGenerationLease,
    ) -> McpPromptCommandDescriptor:
        name = f"mcp__{_slug(snapshot.server_id)}__{_slug(descriptor.remote_name)}"
        arguments = tuple(
            McpPromptCommandArgument(
                item.name,
                item.description[: self.policy.max_description_chars],
                item.required,
            )
            for item in descriptor.arguments
        )
        return McpPromptCommandDescriptor(
            name=name,
            server_id=snapshot.server_id,
            remote_name=descriptor.remote_name,
            description=(
                descriptor.description[: self.policy.max_description_chars]
                or f"MCP prompt {descriptor.remote_name} from {snapshot.server_id}"
            ),
            arguments=arguments,
            lease=lease,
        )

    def _lease(self, snapshot: McpCapabilitySnapshot) -> McpGenerationLease:
        return McpGenerationLease(
            snapshot.server_id,
            snapshot.connection_generation,
            snapshot.generation,
            snapshot.snapshot_digest,
        )

    def _require_lease(self, lease: McpGenerationLease) -> McpCapabilitySnapshot:
        with self._lock:
            snapshot = self.catalog.get(lease.server_id)
            if snapshot is None:
                raise McpResourceProjectionStale("MCP capability snapshot is unavailable")
            connection = self.connection_runtime.snapshot(lease.server_id)
            if not bool(getattr(connection, "healthy", False)):
                raise McpResourceProjectionStale("MCP connection is not healthy")
            current_connection_generation = int(getattr(connection, "generation", -1))
            if current_connection_generation != lease.connection_generation:
                raise McpResourceProjectionStale("MCP connection generation changed")
            if snapshot.connection_generation != lease.connection_generation:
                raise McpResourceProjectionStale("MCP catalog connection generation changed")
            if snapshot.generation != lease.capability_generation:
                raise McpResourceProjectionStale("MCP capability generation changed")
            if snapshot.snapshot_digest != lease.snapshot_digest:
                raise McpResourceProjectionStale("MCP capability digest changed")
            return snapshot

    def _emit_projection(
        self,
        bundle: McpResourceProjectionBundle,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> None:
        if (
            self.event_sink is None
            or not self.policy.emit_projection_events
            or not run_id
            or not task_id
        ):
            return
        event_type = getattr(EventType, "MCP_CAPABILITIES_CHANGED", EventType.SYSTEM_NOTICE)
        self.event_sink(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=event_type,
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-resource-projection-event.v1",
                        "runtime_id": "McpResourceProjectionRuntime",
                        "bundle": bundle.safe_dict(),
                        "state_mutation": "resource_prompt_projection_built",
                    }
                },
            )
        )

    def _emit_prompt_invocation(
        self,
        invocation: McpPromptCommandInvocation,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
    ) -> None:
        if self.event_sink is None or not run_id or not task_id:
            return
        self.event_sink(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.CONTROL_COMMAND,
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-prompt-command-event.v1",
                        "runtime_id": "McpResourceProjectionRuntime",
                        "invocation": invocation.safe_dict(),
                        "session_id": session_id,
                        "worker_request_id": worker_request_id,
                        "state_mutation": "mcp_prompt_invoked",
                    }
                },
            )
        )

    def _assert_enabled(self) -> None:
        if self.disabled:
            raise McpResourceProjectionDisabled("McpResourceProjectionRuntime is disabled")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "unnamed"


def _resource_safe_dict(
    descriptor: McpResourceDescriptor,
    description_limit: int,
) -> dict[str, JsonValue]:
    return {
        "server_id": descriptor.server_id,
        "uri": descriptor.uri,
        "name": descriptor.name,
        "description": descriptor.description[:description_limit],
        "mime_type": descriptor.mime_type,
        "size": descriptor.size,
        "annotations": redact_value(descriptor.annotations),
        "untrusted_external_content": True,
    }


def _receipt_safe_dict(value: Any) -> dict[str, Any]:
    safe = getattr(value, "safe_dict", None)
    if callable(safe):
        selected = safe()
    else:
        selected = redact_value(value)
    if isinstance(selected, Mapping):
        return dict(selected)
    return {"value": selected}


def _stale_result(
    call: ToolCall,
    reason: str,
    lease: McpGenerationLease,
) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        ok=False,
        summary=reason,
        error="mcp_projection_stale",
        output={"generation": lease.safe_dict()},
        metadata={
            "tool_namespace": "mcp",
            "mcp_projection_stale": "true",
            "mcp_invoke_started": "false",
            "server_id": lease.server_id,
        },
    )


def _invalid_result(
    call: ToolCall,
    reason: str,
    lease: McpGenerationLease,
) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        ok=False,
        summary=reason,
        error="mcp_resource_input_invalid",
        output={"generation": lease.safe_dict()},
        metadata={
            "tool_namespace": "mcp",
            "mcp_projection_stale": "false",
            "mcp_invoke_started": "false",
            "server_id": lease.server_id,
        },
    )


__all__ = [
    "McpGenerationLease",
    "McpPromptCommandArgument",
    "McpPromptCommandDescriptor",
    "McpPromptCommandInvocation",
    "McpResourceConnectionPort",
    "McpResourceOutputPort",
    "McpResourceProjectionBundle",
    "McpResourceProjectionCollision",
    "McpResourceProjectionDecision",
    "McpResourceProjectionDisabled",
    "McpResourceProjectionError",
    "McpResourceProjectionPolicy",
    "McpResourceProjectionRuntime",
    "McpResourceProjectionStale",
]
