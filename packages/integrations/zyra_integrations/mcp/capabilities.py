from __future__ import annotations

"""MCP capability pagination, snapshot replacement, resources and prompts.

Claude Code supplies the capability projection model, while opencode and Agent
Framework supply the pagination/cursor guard missing from the Claude snapshot.
The catalog is deliberately snapshot-replace rather than append-only: after a
``list_changed`` notification, a removed remote tool must become unreachable.
"""

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from zyra_core import ArtifactRef, EventRecord, EventType, new_id, now_iso

from .models import (
    JsonValue,
    McpContent,
    McpModelError,
    McpPage,
    McpPromptDescriptor,
    McpResourceDescriptor,
    McpServerCapabilities,
    McpToolDescriptor,
    canonical_json,
    redact_value,
    stable_digest,
    to_json_value,
)
from .output import McpOutputBudgetRuntime, McpOutputReceipt


class McpCapabilityError(RuntimeError):
    pass


class McpCapabilityRuntimeDisabled(McpCapabilityError):
    pass


class McpRequestPort(Protocol):
    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]: ...


RequestCallable = Callable[[str, Mapping[str, Any] | None], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class McpPaginationPolicy:
    max_pages: int = 1_000
    max_items: int = 100_000
    allow_empty_cursor: bool = True

    def __post_init__(self) -> None:
        if self.max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if self.max_items <= 0:
            raise ValueError("max_items must be positive")


@dataclass(frozen=True, slots=True)
class McpPaginationTrace:
    method: str
    item_key: str
    page_count: int
    item_count: int
    cursors: tuple[str, ...]
    terminal_cursor: str
    snapshot_digest: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "method": self.method,
            "item_key": self.item_key,
            "page_count": self.page_count,
            "item_count": self.item_count,
            "cursors": list(self.cursors),
            "terminal_cursor": self.terminal_cursor,
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class McpCollectedPages:
    items: tuple[Mapping[str, JsonValue], ...]
    trace: McpPaginationTrace


class McpPageCollector:
    def __init__(self, *, policy: McpPaginationPolicy | None = None) -> None:
        self.policy = policy or McpPaginationPolicy()

    def collect(
        self,
        request: McpRequestPort | RequestCallable,
        *,
        method: str,
        item_key: str,
        params: Mapping[str, Any] | None = None,
    ) -> McpCollectedPages:
        base = dict(params or {})
        items: list[Mapping[str, JsonValue]] = []
        seen_request_cursors: set[str] = set()
        requested_cursors: list[str] = []
        cursor: str | None = None

        for page_index in range(self.policy.max_pages):
            request_cursor = "" if cursor is None else cursor
            if request_cursor in seen_request_cursors:
                raise McpCapabilityError(
                    f"{method} repeated cursor {request_cursor!r} at page {page_index}"
                )
            seen_request_cursors.add(request_cursor)
            requested_cursors.append(request_cursor)
            page_params = dict(base)
            if cursor is not None or self.policy.allow_empty_cursor:
                page_params["cursor"] = request_cursor
            result = _request(request, method, page_params)
            try:
                page = McpPage.from_result(
                    result,
                    item_key=item_key,
                    request_cursor=request_cursor,
                    page_index=page_index,
                )
            except (McpModelError, TypeError, ValueError) as error:
                raise McpCapabilityError(f"invalid {method} page {page_index}: {error}") from error
            items.extend(page.items)
            if len(items) > self.policy.max_items:
                raise McpCapabilityError(
                    f"{method} returned more than {self.policy.max_items} items"
                )
            if page.terminal:
                trace = McpPaginationTrace(
                    method=method,
                    item_key=item_key,
                    page_count=page_index + 1,
                    item_count=len(items),
                    cursors=tuple(requested_cursors),
                    terminal_cursor="",
                    snapshot_digest=stable_digest(items),
                )
                return McpCollectedPages(tuple(items), trace)
            cursor = page.next_cursor

        raise McpCapabilityError(
            f"{method} exceeded pagination limit {self.policy.max_pages}"
        )


@dataclass(frozen=True, slots=True)
class McpCapabilityDiff:
    server_id: str
    previous_generation: int
    generation: int
    added_tools: tuple[str, ...] = ()
    removed_tools: tuple[str, ...] = ()
    changed_tools: tuple[str, ...] = ()
    added_resources: tuple[str, ...] = ()
    removed_resources: tuple[str, ...] = ()
    changed_resources: tuple[str, ...] = ()
    added_prompts: tuple[str, ...] = ()
    removed_prompts: tuple[str, ...] = ()
    changed_prompts: tuple[str, ...] = ()
    instructions_changed: bool = False

    @property
    def changed(self) -> bool:
        return any(
            (
                self.added_tools,
                self.removed_tools,
                self.changed_tools,
                self.added_resources,
                self.removed_resources,
                self.changed_resources,
                self.added_prompts,
                self.removed_prompts,
                self.changed_prompts,
            )
        ) or self.instructions_changed

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "previous_generation": self.previous_generation,
            "generation": self.generation,
            "added_tools": list(self.added_tools),
            "removed_tools": list(self.removed_tools),
            "changed_tools": list(self.changed_tools),
            "added_resources": list(self.added_resources),
            "removed_resources": list(self.removed_resources),
            "changed_resources": list(self.changed_resources),
            "added_prompts": list(self.added_prompts),
            "removed_prompts": list(self.removed_prompts),
            "changed_prompts": list(self.changed_prompts),
            "instructions_changed": self.instructions_changed,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class McpCapabilitySnapshot:
    server_id: str
    connection_generation: int
    generation: int
    capabilities: McpServerCapabilities
    tools: tuple[McpToolDescriptor, ...]
    resources: tuple[McpResourceDescriptor, ...]
    resource_templates: tuple[Mapping[str, JsonValue], ...]
    prompts: tuple[McpPromptDescriptor, ...]
    instructions: str = ""
    instructions_hash: str = ""
    pagination: Mapping[str, McpPaginationTrace] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.server_id:
            raise ValueError("server_id is required")
        if self.connection_generation < 0 or self.generation < 0:
            raise ValueError("capability generations cannot be negative")
        tool_names = [item.local_name for item in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise McpCapabilityError("MCP projected tool names collide within one snapshot")
        resource_uris = [item.uri for item in self.resources]
        if len(resource_uris) != len(set(resource_uris)):
            raise McpCapabilityError("MCP resource URIs collide within one snapshot")
        prompt_names = [item.remote_name for item in self.prompts]
        if len(prompt_names) != len(set(prompt_names)):
            raise McpCapabilityError("MCP prompt names collide within one snapshot")

    @property
    def snapshot_digest(self) -> str:
        return stable_digest(
            {
                "server_id": self.server_id,
                "connection_generation": self.connection_generation,
                "generation": self.generation,
                "tools": [item.to_dict() for item in self.tools],
                "resources": [item.to_dict() for item in self.resources],
                "resource_templates": list(self.resource_templates),
                "prompts": [item.to_dict() for item in self.prompts],
                "instructions_hash": self.instructions_hash,
            }
        )

    def safe_dict(self, *, include_instructions: bool = False) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "connection_generation": self.connection_generation,
            "generation": self.generation,
            "capabilities": self.capabilities.to_dict(),
            "tools": [item.to_dict() for item in self.tools],
            "resources": [item.to_dict() for item in self.resources],
            "resource_templates": [redact_value(item) for item in self.resource_templates],
            "prompts": [item.to_dict() for item in self.prompts],
            "instructions": self.instructions if include_instructions else "<present>" if self.instructions else "",
            "instructions_hash": self.instructions_hash,
            "pagination": {key: value.to_dict() for key, value in self.pagination.items()},
            "snapshot_digest": self.snapshot_digest,
            "created_at": self.created_at,
        }


class McpCapabilityCatalog:
    """Thread-safe read model for capability snapshots.

    Mutation responsibility stays with ``McpConnectionRuntime``.  This catalog
    only atomically replaces immutable snapshots and makes stale entries
    unreachable to registry and API consumers.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, McpCapabilitySnapshot] = {}
        self._lock = threading.RLock()

    def get(self, server_id: str) -> McpCapabilitySnapshot | None:
        with self._lock:
            return self._snapshots.get(server_id)

    def list(self) -> tuple[McpCapabilitySnapshot, ...]:
        with self._lock:
            return tuple(self._snapshots[key] for key in sorted(self._snapshots))

    def replace(self, snapshot: McpCapabilitySnapshot) -> McpCapabilityDiff:
        with self._lock:
            current = self._snapshots.get(snapshot.server_id)
            if current is not None:
                if snapshot.connection_generation < current.connection_generation:
                    raise McpCapabilityError("stale connection generation cannot replace MCP catalog")
                if (
                    snapshot.connection_generation == current.connection_generation
                    and snapshot.generation <= current.generation
                ):
                    raise McpCapabilityError("stale capability generation cannot replace MCP catalog")
            self._assert_global_tool_collisions(snapshot)
            diff = _diff_snapshots(current, snapshot)
            self._snapshots[snapshot.server_id] = snapshot
            return diff

    def remove(self, server_id: str) -> McpCapabilitySnapshot | None:
        with self._lock:
            return self._snapshots.pop(server_id, None)

    def clear(self) -> None:
        with self._lock:
            self._snapshots.clear()

    def tool(self, local_name: str) -> McpToolDescriptor | None:
        with self._lock:
            for snapshot in self._snapshots.values():
                for tool in snapshot.tools:
                    if tool.local_name == local_name:
                        return tool
        return None

    def resource(self, server_id: str, uri: str) -> McpResourceDescriptor | None:
        snapshot = self.get(server_id)
        if snapshot is None:
            return None
        return next((item for item in snapshot.resources if item.uri == uri), None)

    def prompt(self, server_id: str, name: str) -> McpPromptDescriptor | None:
        snapshot = self.get(server_id)
        if snapshot is None:
            return None
        return next((item for item in snapshot.prompts if item.remote_name == name), None)

    def safe_dict(self, *, include_instructions: bool = False) -> dict[str, JsonValue]:
        return {
            "servers": [item.safe_dict(include_instructions=include_instructions) for item in self.list()],
            "server_count": len(self.list()),
            "tool_count": sum(len(item.tools) for item in self.list()),
            "resource_count": sum(len(item.resources) for item in self.list()),
            "prompt_count": sum(len(item.prompts) for item in self.list()),
        }

    def _assert_global_tool_collisions(self, incoming: McpCapabilitySnapshot) -> None:
        occupied = {
            tool.local_name: (server_id, tool.remote_name)
            for server_id, snapshot in self._snapshots.items()
            if server_id != incoming.server_id
            for tool in snapshot.tools
        }
        for tool in incoming.tools:
            if tool.local_name in occupied:
                other = occupied[tool.local_name]
                raise McpCapabilityError(
                    f"projected MCP tool collision {tool.local_name!r}: "
                    f"{other[0]}/{other[1]} vs {incoming.server_id}/{tool.remote_name}"
                )


class McpResourcePromptRuntime:
    def __init__(
        self,
        catalog: McpCapabilityCatalog,
        output_runtime: McpOutputBudgetRuntime,
        *,
        pagination_policy: McpPaginationPolicy | None = None,
        disabled: bool = False,
    ) -> None:
        self.catalog = catalog
        self.output_runtime = output_runtime
        self.collector = McpPageCollector(policy=pagination_policy)
        self.disabled = disabled

    def discover(
        self,
        *,
        server_id: str,
        request: McpRequestPort | RequestCallable,
        initialize_result: Mapping[str, Any],
        connection_generation: int,
        generation: int,
        current: McpCapabilitySnapshot | None = None,
        refresh_kinds: Iterable[str] = ("tools", "resources", "resource_templates", "prompts"),
    ) -> tuple[McpCapabilitySnapshot, McpCapabilityDiff]:
        if self.disabled:
            raise McpCapabilityRuntimeDisabled("McpResourcePromptRuntime is disabled")
        capabilities = McpServerCapabilities.from_initialize_result(initialize_result)
        wanted = set(refresh_kinds)
        pagination: dict[str, McpPaginationTrace] = dict(current.pagination) if current else {}

        tools = current.tools if current else ()
        resources = current.resources if current else ()
        resource_templates = current.resource_templates if current else ()
        prompts = current.prompts if current else ()

        if "tools" in wanted and capabilities.supports("tools"):
            pages = self.collector.collect(request, method="tools/list", item_key="tools")
            tools = tuple(
                McpToolDescriptor.from_wire(
                    server_id,
                    item,
                    projection_revision=generation,
                )
                for item in pages.items
            )
            pagination["tools"] = pages.trace
        if "resources" in wanted and capabilities.supports("resources"):
            pages = self.collector.collect(request, method="resources/list", item_key="resources")
            resources = tuple(McpResourceDescriptor.from_wire(server_id, item) for item in pages.items)
            pagination["resources"] = pages.trace
        if "resource_templates" in wanted and capabilities.supports("resources"):
            pages = self.collector.collect(
                request,
                method="resources/templates/list",
                item_key="resourceTemplates",
            )
            resource_templates = tuple(to_json_value(item) for item in pages.items)
            pagination["resource_templates"] = pages.trace
        if "prompts" in wanted and capabilities.supports("prompts"):
            pages = self.collector.collect(request, method="prompts/list", item_key="prompts")
            prompts = tuple(
                McpPromptDescriptor.from_wire(server_id, item, projection_revision=generation)
                for item in pages.items
            )
            pagination["prompts"] = pages.trace

        instructions = str(initialize_result.get("instructions") or "")
        instructions_hash = stable_digest(instructions) if instructions else ""
        snapshot = McpCapabilitySnapshot(
            server_id=server_id,
            connection_generation=connection_generation,
            generation=generation,
            capabilities=capabilities,
            tools=tuple(tools),
            resources=tuple(resources),
            resource_templates=tuple(resource_templates),
            prompts=tuple(prompts),
            instructions=instructions,
            instructions_hash=instructions_hash,
            pagination=pagination,
        )
        return snapshot, self.catalog.replace(snapshot)

    def read_resource(
        self,
        *,
        server_id: str,
        uri: str,
        request: McpRequestPort | RequestCallable,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> McpOutputReceipt:
        if self.disabled:
            raise McpCapabilityRuntimeDisabled("McpResourcePromptRuntime is disabled")
        if self.catalog.resource(server_id, uri) is None:
            raise McpCapabilityError("resource is not present in the active MCP snapshot")
        result = _request(request, "resources/read", {"uri": uri})
        return self.output_runtime.normalize_resource_result(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            server_id=server_id,
            uri=uri,
            raw_result=result,
        )

    def get_prompt(
        self,
        *,
        server_id: str,
        name: str,
        arguments: Mapping[str, Any],
        request: McpRequestPort | RequestCallable,
    ) -> dict[str, JsonValue]:
        if self.disabled:
            raise McpCapabilityRuntimeDisabled("McpResourcePromptRuntime is disabled")
        descriptor = self.catalog.prompt(server_id, name)
        if descriptor is None:
            raise McpCapabilityError("prompt is not present in the active MCP snapshot")
        validated = descriptor.validate_arguments(arguments)
        result = _request(request, "prompts/get", {"name": name, "arguments": validated})
        raw_messages = result.get("messages")
        if not isinstance(raw_messages, list):
            raise McpCapabilityError("prompts/get result requires messages array")
        messages: list[dict[str, JsonValue]] = []
        for index, raw in enumerate(raw_messages):
            if not isinstance(raw, Mapping):
                raise McpCapabilityError(f"prompt message {index} is not an object")
            role = str(raw.get("role") or "")
            if role not in {"user", "assistant"}:
                raise McpCapabilityError(f"prompt message {index} has invalid role {role!r}")
            raw_content = raw.get("content")
            values = raw_content if isinstance(raw_content, list) else [raw_content]
            content: list[dict[str, JsonValue]] = []
            for block in values:
                if not isinstance(block, Mapping):
                    raise McpCapabilityError(f"prompt content {index} is not an object")
                parsed = McpContent.from_wire(block)
                content.append(parsed.safe_dict())
            messages.append({"role": role, "content": content})
        return {
            "server_id": server_id,
            "name": name,
            "description": str(result.get("description") or descriptor.description),
            "arguments": validated,
            "messages": messages,
            "projection_revision": descriptor.projection_revision,
            "untrusted_external_content": True,
        }

    def notification_refresh_kinds(self, method: str) -> tuple[str, ...]:
        normalized = method.strip().lower()
        mapping = {
            "notifications/tools/list_changed": ("tools",),
            "notifications/resources/list_changed": ("resources", "resource_templates"),
            "notifications/prompts/list_changed": ("prompts",),
        }
        return mapping.get(normalized, ())

    def event_for_diff(
        self,
        diff: McpCapabilityDiff,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        cause_event_id: str = "",
    ) -> EventRecord:
        event_type = getattr(EventType, "MCP_CAPABILITIES_CHANGED", EventType.SYSTEM_NOTICE)
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload={
                "mcp_runtime": {
                    "schema": "zyra.mcp-capability-event.v1",
                    "runtime_id": "McpResourcePromptRuntime",
                    "server_id": diff.server_id,
                    "cause_event_id": cause_event_id,
                    "diff": diff.to_dict(),
                }
            },
        )


def _request(
    port: McpRequestPort | RequestCallable,
    method: str,
    params: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if hasattr(port, "request"):
        result = port.request(method, params)  # type: ignore[union-attr]
    else:
        result = port(method, params)  # type: ignore[operator]
    if not isinstance(result, Mapping):
        raise McpCapabilityError(f"{method} returned a non-object result")
    return result


def _diff_snapshots(
    current: McpCapabilitySnapshot | None,
    incoming: McpCapabilitySnapshot,
) -> McpCapabilityDiff:
    def changes(
        before: Mapping[str, str],
        after: Mapping[str, str],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        added = tuple(sorted(set(after) - set(before)))
        removed = tuple(sorted(set(before) - set(after)))
        changed = tuple(sorted(key for key in set(before) & set(after) if before[key] != after[key]))
        return added, removed, changed

    previous_tools = {
        item.local_name: item.schema_fingerprint for item in (current.tools if current else ())
    }
    next_tools = {item.local_name: item.schema_fingerprint for item in incoming.tools}
    previous_resources = {
        item.uri: stable_digest(item.to_dict()) for item in (current.resources if current else ())
    }
    next_resources = {item.uri: stable_digest(item.to_dict()) for item in incoming.resources}
    previous_prompts = {
        item.remote_name: stable_digest(item.to_dict()) for item in (current.prompts if current else ())
    }
    next_prompts = {item.remote_name: stable_digest(item.to_dict()) for item in incoming.prompts}
    added_tools, removed_tools, changed_tools = changes(previous_tools, next_tools)
    added_resources, removed_resources, changed_resources = changes(previous_resources, next_resources)
    added_prompts, removed_prompts, changed_prompts = changes(previous_prompts, next_prompts)
    return McpCapabilityDiff(
        server_id=incoming.server_id,
        previous_generation=current.generation if current else 0,
        generation=incoming.generation,
        added_tools=added_tools,
        removed_tools=removed_tools,
        changed_tools=changed_tools,
        added_resources=added_resources,
        removed_resources=removed_resources,
        changed_resources=changed_resources,
        added_prompts=added_prompts,
        removed_prompts=removed_prompts,
        changed_prompts=changed_prompts,
        instructions_changed=(current.instructions_hash if current else "") != incoming.instructions_hash,
    )


__all__ = [
    "McpCapabilityCatalog",
    "McpCapabilityDiff",
    "McpCapabilityError",
    "McpCapabilityRuntimeDisabled",
    "McpCapabilitySnapshot",
    "McpCollectedPages",
    "McpPageCollector",
    "McpPaginationPolicy",
    "McpPaginationTrace",
    "McpRequestPort",
    "McpResourcePromptRuntime",
]
