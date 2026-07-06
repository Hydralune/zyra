from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zyra_core import ArtifactRef, new_id, now_iso


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    purpose: str
    source: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


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
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list(self) -> list[ToolSpec]:
        return list(self._tools.values())


def default_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
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
                "Run classified shell commands with permission policy.",
                "claude-code-best BashTool/PowerShellTool",
                input_schema={
                    "type": "object",
                    "required": ["command"],
                    "properties": {
                        "command": {"type": "string"},
                        "approved": {"type": "boolean"},
                        "timeout_seconds": {"type": "integer"},
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
                "browser",
                "Capture a controlled browser-like state snapshot from inline HTML or an allowed URL.",
                "browser-use",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
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
