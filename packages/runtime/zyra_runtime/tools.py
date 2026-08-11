from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from zyra_core import ArtifactRef, new_id, now_iso


BROWSER_TOOL_ACTIONS = (
    "open_url",
    "navigate",
    "extract_text",
    "extract",
    "snapshot_state",
    "find_elements",
)


class FrozenDict(dict):
    """Recursively immutable ``dict`` compatible with legacy schema code."""

    def __init__(self, value: Mapping[Any, Any] | None = None, **kwargs: Any) -> None:
        dict.__init__(self)
        selected = dict(value or {})
        selected.update(kwargs)
        for key, item in selected.items():
            dict.__setitem__(self, key, _deep_freeze(item))

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen mapping cannot be modified")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> "FrozenDict":
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> "FrozenDict":
        return self


class FrozenList(list):
    """List-compatible recursively immutable sequence used by JSON schemas."""

    def __init__(self, value: Any = ()) -> None:
        list.__init__(self, (_deep_freeze(item) for item in value))

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen sequence cannot be modified")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable

    def __copy__(self) -> "FrozenList":
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> "FrozenList":
        return self


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict | FrozenList):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, list):
        return FrozenList(value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_deep_freeze(item) for item in value)
    return copy.deepcopy(value)


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {copy.deepcopy(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, set | frozenset):
        return {_deep_thaw(item) for item in value}
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class DynamicToolProvenance:
    """Registry-owned identity for an executable dynamic handler.

    Permission identity cannot be inferred from display metadata: those values
    are serialized to models, APIs and logs.  This record is copied into the
    registry snapshot and compared with both the handler binding and one-use
    grant at the final side-effect boundary.
    """

    tool_name: str
    namespace: str
    server_id: str = ""
    version: str = ""
    handler_kind: str = "dynamic"
    external_boundary: bool = False
    requires_exact_grant: bool = True
    source: str = "zyra_runtime"

    def __post_init__(self) -> None:
        name = str(self.tool_name).strip()
        namespace = str(self.namespace).strip() or "dynamic"
        server_id = str(self.server_id).strip()
        if not name:
            raise ValueError("dynamic tool provenance requires tool_name")
        if namespace == "mcp" and not server_id:
            raise ValueError("MCP dynamic tool provenance requires exact server_id")
        object.__setattr__(self, "tool_name", name)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "server_id", server_id)
        object.__setattr__(self, "version", str(self.version).strip())
        object.__setattr__(self, "handler_kind", str(self.handler_kind).strip() or "dynamic")
        object.__setattr__(self, "source", str(self.source).strip() or "zyra_runtime")
        if namespace == "mcp":
            object.__setattr__(self, "external_boundary", True)
            object.__setattr__(self, "requires_exact_grant", True)


@dataclass(frozen=True, slots=True)
class ProvenancedDynamicHandler:
    """Callable paired with the immutable identity used by ``ToolExecutor``."""

    provenance: DynamicToolProvenance
    _handler: Callable[["ToolCall"], "ToolResult"] = field(repr=False, compare=False)

    def __call__(self, call: "ToolCall") -> "ToolResult":
        return self._handler(call)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    purpose: str
    source: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)
    execution_provenance: DynamicToolProvenance | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", FrozenDict(_deep_thaw(self.input_schema)))
        object.__setattr__(self, "output_schema", FrozenDict(_deep_thaw(self.output_schema)))
        object.__setattr__(self, "metadata", FrozenDict(_deep_thaw(self.metadata)))
        provenance = self.execution_provenance
        if provenance is not None:
            if not isinstance(provenance, DynamicToolProvenance):
                raise TypeError("execution_provenance must be DynamicToolProvenance")
            if provenance.tool_name != self.name:
                raise ValueError("dynamic tool provenance name must match ToolSpec.name")


@dataclass(frozen=True, slots=True)
class ToolCall:
    run_id: str
    task_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = field(default_factory=lambda: new_id("toolcall"))
    node_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    ok: bool
    summary: str
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    error: str | None = None
    completed_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec]) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools:
            if not tool.name:
                raise ValueError("tool name cannot be empty")
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool registration: {tool.name}")
            self._tools[tool.name] = _snapshot_tool_spec(tool)

    def get(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        return _snapshot_tool_spec(tool) if tool is not None else None

    def list(self) -> list[ToolSpec]:
        return [_snapshot_tool_spec(tool) for tool in self._tools.values()]

    def execution_provenance(self, name: str) -> DynamicToolProvenance | None:
        """Return the immutable handler identity from the private snapshot."""

        tool = self._tools.get(name)
        return tool.execution_provenance if tool is not None else None

    def merged(
        self,
        tools: list[ToolSpec],
        *,
        keep_existing: bool = True,
    ) -> "ToolRegistry":
        """Return a new immutable registry view with dynamic tools applied.

        Built-ins win by default, matching Claude Code's stable tool-pool
        assembly.  Callers may request strict replacement only when they own
        both declarations; MCP projections should never shadow a built-in.
        """

        merged = self.list()
        by_name = {tool.name: index for index, tool in enumerate(merged)}
        for tool in tools:
            existing = by_name.get(tool.name)
            if existing is None:
                by_name[tool.name] = len(merged)
                merged.append(tool)
            elif not keep_existing:
                merged[existing] = tool
        return ToolRegistry(merged)


def _snapshot_tool_spec(tool: ToolSpec) -> ToolSpec:
    """Detach every exposed container before it enters or leaves a registry."""

    return ToolSpec(
        name=tool.name,
        purpose=tool.purpose,
        source=tool.source,
        input_schema=_deep_thaw(tool.input_schema),
        output_schema=_deep_thaw(tool.output_schema),
        metadata=_deep_thaw(tool.metadata),
        execution_provenance=tool.execution_provenance,
    )


def default_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                "__zyra_invalid_tool_arguments__",
                "Internal no-effect error sink for an invalid provider tool call; never select directly.",
                "zyra compatible-provider safety boundary",
                input_schema={
                    "type": "object",
                    "required": ["original_tool_name", "raw_arguments_digest"],
                    "properties": {
                        "original_tool_name": {"type": "string"},
                        "parse_error": {"type": "string"},
                        "raw_arguments_digest": {"type": "string"},
                        "side_effect_executed": {"type": "boolean"},
                    },
                },
                metadata={
                    "access_mode": "read_only",
                    "read_only": "true",
                    "concurrency_safe": "true",
                    "mutates_workspace": "false",
                    "internal_error_sink": "true",
                },
            ),
            ToolSpec(
                "file_read",
                "Read files inside the permitted workspace.",
                "claude-code-best FileReadTool",
                input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
                metadata={
                    "access_mode": "read_only",
                    "read_only": "true",
                    "concurrency_safe": "true",
                    "source_path": "src/tools/FileReadTool",
                    "budget_source_path": "src/utils/toolResultStorage.ts",
                },
            ),
            ToolSpec(
                "file_write",
                "Write files inside the permitted workspace.",
                "claude-code-best FileWriteTool",
                input_schema={
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                },
                metadata={
                    "access_mode": "workspace_write",
                    "read_only": "false",
                    "concurrency_safe": "false",
                    "mutates_workspace": "true",
                    "conflict_argument": "path",
                    "source_path": "src/tools/FileWriteTool",
                },
            ),
            ToolSpec(
                "file_edit",
                "Patch files inside the permitted workspace using exact replacement.",
                "claude-code-best FileEditTool",
                input_schema={
                    "type": "object",
                    "required": ["path", "old", "new"],
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                },
                metadata={
                    "access_mode": "workspace_write",
                    "read_only": "false",
                    "concurrency_safe": "false",
                    "mutates_workspace": "true",
                    "conflict_argument": "path",
                    "source_path": "src/tools/FileEditTool",
                    "stale_write_guard_source_path": "src/utils/fileStateCache.ts",
                },
            ),
            ToolSpec(
                "shell",
                (
                    "Run one classified executable with permission policy. Prefer "
                    "executable/argv/cwd/environment for a working directory or environment; "
                    "the legacy command string cannot contain shell composition or redirects."
                ),
                "claude-code-best BashTool/PowerShellTool",
                input_schema={
                    "type": "object",
                    "anyOf": [
                        {"required": ["command"]},
                        {"required": ["executable"]},
                    ],
                    "properties": {
                        "command": {"type": "string"},
                        "executable": {"type": "string"},
                        "argv": {"type": "array", "items": {"type": "string"}},
                        "cwd": {"type": "string"},
                        "environment": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "approved": {"type": "boolean"},
                        "timeout_seconds": {"type": "integer"},
                        "foreground_wait_seconds": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 60,
                        },
                        "background": {"type": "boolean"},
                    },
                },
                metadata={
                    "access_mode": "shell",
                    "read_only": "false",
                    "concurrency_safe": "false",
                    "mutates_workspace": "true",
                    "source_path": "src/tools/BashTool",
                    "shell_lifecycle_source_path": "src/utils/ShellCommand.ts",
                    "sandbox_source_path": "src/utils/sandbox/sandbox-adapter.ts",
                },
            ),
            ToolSpec(
                "shell_wait",
                (
                    "Poll a background shell job by the stable job_id returned from shell. "
                    "Returns heartbeat and new output chunks while running, or the real "
                    "terminal result when complete."
                ),
                "zyra unified command lifecycle",
                input_schema={
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {
                        "job_id": {"type": "string"},
                        "timeout_seconds": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 60,
                        },
                        "after_sequence": {"type": "integer", "minimum": 0},
                    },
                },
                metadata={
                    "access_mode": "read_only",
                    "read_only": "true",
                    "concurrency_safe": "true",
                    "mutates_workspace": "false",
                    "source_path": "zyra_runtime/sandbox_gateway/integration_tools.py",
                },
            ),
            ToolSpec(
                "browser",
                (
                    "Read inline HTML or an allowed HTTP(S) URL. This tool does "
                    "not inspect local image or video files; use shell-based "
                    "image/OCR utilities for local media."
                ),
                "browser-use",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": list(BROWSER_TOOL_ACTIONS),
                            "description": (
                                "Browser operation. Use only one of the enumerated "
                                "canonical action names."
                            ),
                        },
                        "url": {"type": "string"},
                        "html": {"type": "string"},
                        "allow_network": {"type": "boolean"},
                        "allowed_domains": {"type": "array", "items": {"type": "string"}},
                        "capture_html": {"type": "boolean"},
                    },
                },
                metadata={
                    "access_mode": "read_only",
                    "read_only": "true",
                    "concurrency_safe": "true",
                    "source_path": "browser_use/tools/registry",
                },
            ),
            ToolSpec(
                "web_search",
                "Search local research material or explicitly allowed URLs and return evidence refs.",
                "claude-code-best WebSearchTool/browser-use",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "url": {"type": "string"},
                        "allow_network": {"type": "boolean"},
                        "allowed_domains": {"type": "array", "items": {"type": "string"}},
                        "max_results": {"type": "integer"},
                    },
                },
                metadata={
                    "access_mode": "read_only",
                    "read_only": "true",
                    "concurrency_safe": "true",
                    "source_path": "src/tools/WebSearchTool",
                },
            ),
            ToolSpec(
                "artifact_write",
                "Persist large outputs as artifacts.",
                "zyra",
                input_schema={
                    "type": "object",
                    "required": ["content"],
                    "properties": {
                        "content": {"type": "string"},
                        "title": {"type": "string"},
                        "kind": {"type": "string"},
                        "extension": {"type": "string"},
                    },
                },
                metadata={
                    "access_mode": "artifact_write",
                    "read_only": "false",
                    "concurrency_safe": "false",
                    "source_path": "zyra_runtime.artifacts",
                },
            ),
            ToolSpec(
                "checkpoint",
                "Read task checkpoint summaries or persist checkpoint artifacts.",
                "zyra",
                input_schema={
                    "type": "object",
                    "properties": {
                        "include_state": {"type": "boolean"},
                        "write_artifact": {"type": "boolean"},
                    },
                },
                metadata={
                    "access_mode": "read_only",
                    "read_only": "true",
                    "concurrency_safe": "true",
                    "source_path": "zyra_runtime.session",
                },
            ),
            ToolSpec(
                "trace",
                "Read event traces and runtime observations.",
                "zyra",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"},
                        "event_type": {"type": "string"},
                        "write_artifact": {"type": "boolean"},
                    },
                },
                metadata={
                    "access_mode": "read_only",
                    "read_only": "true",
                    "concurrency_safe": "true",
                    "source_path": "zyra_core.event_log",
                },
            ),
        ]
    )
