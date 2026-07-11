from __future__ import annotations

"""Dynamic MCP prompt commands backed by the shared MCP control runtime."""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from zyra_integrations.mcp.control import (
    AuthorizationCallback,
    McpControlContext,
    McpControlRequest,
    McpControlResult,
    McpControlRuntime,
)

from .registry import CommandSpec, SlashCommandRegistry


@dataclass(frozen=True, slots=True)
class McpPromptCommandDescriptor:
    name: str
    server_id: str
    prompt_name: str
    description: str
    arguments: tuple[Mapping[str, Any], ...] = ()
    capability_generation: int = 0

    @property
    def command_name(self) -> str:
        selected = self.name.strip()
        return selected if selected.startswith("/") else f"/{selected}"

    def command_spec(self) -> CommandSpec:
        required = [
            str(item.get("name") or "")
            for item in self.arguments
            if bool(item.get("required")) and str(item.get("name") or "")
        ]
        return CommandSpec(
            name=self.command_name,
            purpose=self.description
            or f"MCP prompt {self.prompt_name} from {self.server_id}",
            source=f"mcp:{self.server_id}",
            event_hint="control_command",
            metadata={
                "category": "extension_team",
                "runtime_status": "stateful",
                "command_kind": "mcp_prompt",
                "mcp_server_id": self.server_id,
                "mcp_prompt_name": self.prompt_name,
                "mcp_capability_generation": str(self.capability_generation),
                "required_arguments": ",".join(required),
            },
        )


class McpPromptCommandSource(Protocol):
    def prompt_commands(self) -> Sequence[Any]: ...


class McpCommandAdapter:
    def __init__(
        self,
        control_runtime: McpControlRuntime,
        *,
        prompt_source: McpPromptCommandSource | Any | None = None,
        disabled: bool = False,
    ) -> None:
        self.control_runtime = control_runtime
        self.prompt_source = prompt_source
        self.disabled = disabled

    def prompt_descriptors(self) -> tuple[McpPromptCommandDescriptor, ...]:
        if self.disabled or self.prompt_source is None:
            return ()
        descriptors: list[McpPromptCommandDescriptor] = []
        seen: set[str] = set()
        for value in self.source_values():
            descriptor = coerce_descriptor(value)
            if descriptor.command_name in seen:
                continue
            seen.add(descriptor.command_name)
            descriptors.append(descriptor)
        return tuple(sorted(descriptors, key=lambda item: item.command_name))

    def command_specs(self) -> tuple[CommandSpec, ...]:
        return tuple(item.command_spec() for item in self.prompt_descriptors())

    def registry(self, base: SlashCommandRegistry) -> SlashCommandRegistry:
        return base.merged(self.command_specs())

    def execute(
        self,
        command_name: str,
        argument_text: str,
        *,
        context: McpControlContext,
        authorizer: AuthorizationCallback | None = None,
    ) -> McpControlResult:
        normalized = command_name if command_name.startswith("/") else f"/{command_name}"
        if normalized == "/mcp":
            return self.control_runtime.execute_text(
                argument_text,
                context=context,
                authorizer=authorizer,
            )
        descriptor = next(
            (
                item
                for item in self.prompt_descriptors()
                if item.command_name == normalized
            ),
            None,
        )
        if descriptor is None:
            return McpControlResult(
                request_id="mcp-command-not-found",
                action=self.control_runtime.parser.ALIASES["status"],
                ok=False,
                summary=f"Unknown MCP prompt command: {normalized}",
                error="mcp_prompt_command_not_found",
                operation_id=context.operation_id,
            )
        request = McpControlRequest(
            action="prompt_get",
            target=descriptor.server_id,
            arguments={
                "name": descriptor.prompt_name,
                "arguments": parse_prompt_arguments(descriptor, argument_text),
                "expected_capability_generation": descriptor.capability_generation,
            },
            context=context,
        )
        return self.control_runtime.execute(request, authorizer=authorizer)

    def diagnostics(self) -> dict[str, Any]:
        descriptors = self.prompt_descriptors()
        return {
            "schema": "zyra.mcp-command-adapter.v1",
            "runtime_id": "McpCommandAdapter",
            "owner_unit": "M1-S03B-02",
            "enabled": not self.disabled,
            "prompt_command_count": len(descriptors),
            "prompt_commands": [
                {
                    "name": item.command_name,
                    "server_id": item.server_id,
                    "prompt_name": item.prompt_name,
                    "capability_generation": item.capability_generation,
                }
                for item in descriptors
            ],
            "mcp_state_owner": "McpClientRuntime",
            "global_command_owner": "M1-03D ControlCommandRegistry",
            "creates_parallel_registry": False,
        }

    def source_values(self) -> Sequence[Any]:
        method = getattr(self.prompt_source, "prompt_commands", None)
        if callable(method):
            return tuple(method())
        method = getattr(self.prompt_source, "build_prompt_commands", None)
        if callable(method):
            return tuple(method())
        catalog = getattr(self.prompt_source, "catalog", self.prompt_source)
        list_method = getattr(catalog, "list", None)
        if not callable(list_method):
            return ()
        values: list[dict[str, Any]] = []
        for snapshot in list_method():
            generation = int(getattr(snapshot, "generation", 0))
            for prompt in getattr(snapshot, "prompts", ()):
                raw = safe_mapping(prompt)
                server_id = str(
                    raw.get("server_id") or getattr(snapshot, "server_id", "")
                )
                prompt_name = str(raw.get("name") or "")
                if not server_id or not prompt_name:
                    continue
                values.append(
                    {
                        "name": (
                            f"/mcp__{command_segment(server_id)}"
                            f"__{command_segment(prompt_name)}"
                        ),
                        "server_id": server_id,
                        "prompt_name": prompt_name,
                        "description": str(raw.get("description") or ""),
                        "arguments": raw.get("arguments") or (),
                        "capability_generation": generation,
                    }
                )
        return tuple(values)


def merge_mcp_commands(
    registry: SlashCommandRegistry,
    prompt_commands: Iterable[McpPromptCommandDescriptor | Mapping[str, Any] | Any],
) -> SlashCommandRegistry:
    return registry.merged(
        [coerce_descriptor(value).command_spec() for value in prompt_commands]
    )


def coerce_descriptor(value: Any) -> McpPromptCommandDescriptor:
    if isinstance(value, McpPromptCommandDescriptor):
        return value
    raw = safe_mapping(value)
    name = str(
        raw.get("command_name")
        or raw.get("local_name")
        or raw.get("name")
        or ""
    )
    server_id = str(raw.get("server_id") or "")
    prompt_name = str(
        raw.get("prompt_name")
        or raw.get("remote_name")
        or raw.get("name")
        or ""
    )
    if not name and server_id and prompt_name:
        name = (
            f"/mcp__{command_segment(server_id)}"
            f"__{command_segment(prompt_name)}"
        )
    if not name or not server_id or not prompt_name:
        raise ValueError("invalid MCP prompt command descriptor")
    raw_arguments = raw.get("arguments")
    arguments = (
        tuple(dict(item) for item in raw_arguments if isinstance(item, Mapping))
        if isinstance(raw_arguments, Sequence)
        and not isinstance(raw_arguments, (str, bytes))
        else ()
    )
    return McpPromptCommandDescriptor(
        name=name,
        server_id=server_id,
        prompt_name=prompt_name,
        description=str(raw.get("description") or ""),
        arguments=arguments,
        capability_generation=int(
            raw.get("capability_generation") or raw.get("generation") or 0
        ),
    )


def parse_prompt_arguments(
    descriptor: McpPromptCommandDescriptor,
    text: str,
) -> dict[str, Any]:
    selected = str(text or "").strip()
    if not selected:
        values: dict[str, Any] = {}
    elif selected.startswith("{"):
        decoded = json.loads(selected)
        if not isinstance(decoded, dict):
            raise ValueError("MCP prompt arguments must be a JSON object")
        values = decoded
    else:
        positional = selected.split()
        names = [str(item.get("name") or "") for item in descriptor.arguments]
        values = {
            name: positional[index]
            for index, name in enumerate(names)
            if name and index < len(positional)
        }
    missing = [
        str(item.get("name") or "")
        for item in descriptor.arguments
        if item.get("required")
        and str(item.get("name") or "")
        and not str(values.get(str(item.get("name"))) or "")
    ]
    if missing:
        raise ValueError("missing MCP prompt arguments: " + ", ".join(missing))
    return values


def safe_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for name in ("safe_dict", "to_dict"):
        method = getattr(value, name, None)
        if callable(method):
            selected = method()
            if isinstance(selected, Mapping):
                return dict(selected)
    return {}


def command_segment(value: str) -> str:
    selected: list[str] = []
    previous_separator = False
    for character in str(value).casefold():
        if character.isalnum() or character == "_":
            selected.append(character)
            previous_separator = False
        elif not previous_separator:
            selected.append("_")
            previous_separator = True
    return "".join(selected).strip("_") or "unnamed"


__all__ = [
    "McpCommandAdapter",
    "McpPromptCommandDescriptor",
    "McpPromptCommandSource",
    "merge_mcp_commands",
]
