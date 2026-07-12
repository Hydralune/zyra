from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from .digests import digest_object
from .errors import (
    AgentDefinitionConflict,
    AgentDefinitionError,
    AgentDefinitionNotFound,
    SubagentDisabled,
)
from .models import (
    AgentDefinition,
    AgentDefinitionSource,
    PermissionMode,
    SubagentIsolationKind,
    UsageBudget,
)


SOURCE_PRECEDENCE: dict[AgentDefinitionSource, int] = {
    AgentDefinitionSource.BUILTIN: 10,
    AgentDefinitionSource.USER: 20,
    AgentDefinitionSource.PLUGIN: 30,
    AgentDefinitionSource.PROJECT: 40,
    AgentDefinitionSource.MANAGED: 50,
    AgentDefinitionSource.REQUEST: 60,
}


@dataclass(frozen=True, slots=True)
class AgentDefinitionShadow:
    agent_type: str
    active_definition_id: str
    shadowed_definition_id: str
    active_source: str
    shadowed_source: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "active_definition_id": self.active_definition_id,
            "shadowed_definition_id": self.shadowed_definition_id,
            "active_source": self.active_source,
            "shadowed_source": self.shadowed_source,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AgentDefinitionSnapshot:
    generation: int
    active: tuple[AgentDefinition, ...]
    shadowed: tuple[AgentDefinitionShadow, ...]
    failed: tuple[dict[str, Any], ...]
    digest: str

    def to_dict(self, *, include_prompts: bool = False) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "active": [item.to_dict(include_prompt=include_prompts) for item in self.active],
            "shadowed": [item.to_dict() for item in self.shadowed],
            "failed": [copy.deepcopy(item) for item in self.failed],
            "digest": self.digest,
        }


class AgentDefinitionRegistry:
    """Deterministic, generation-tracked definition registry.

    Claude's loader merges built-in, plugin, user, project and policy agents.
    This implementation retains that mature precedence model while making
    every collision inspectable and preserving the active definition as an
    immutable value. It owns definitions only; child sessions and tasks stay
    in ``SubagentTaskStore``.
    """

    def __init__(self, definitions: Iterable[AgentDefinition] = (), *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._lock = RLock()
        self._generation = 0
        self._definitions: dict[str, list[AgentDefinition]] = {}
        self._active: dict[str, AgentDefinition] = {}
        self._shadowed: list[AgentDefinitionShadow] = []
        self._failed: list[dict[str, Any]] = []
        if definitions:
            self.replace(definitions)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def replace(self, definitions: Iterable[AgentDefinition]) -> AgentDefinitionSnapshot:
        self._require_enabled()
        grouped: dict[str, list[AgentDefinition]] = {}
        for definition in definitions:
            grouped.setdefault(definition.agent_type, []).append(definition)
        with self._lock:
            active, shadowed = self._resolve(grouped)
            new_digest = digest_object({
                "active": [item.to_dict(include_prompt=True) for item in active.values()],
                "shadowed": [item.to_dict() for item in shadowed],
            })
            old_digest = self.snapshot().digest if self._active else ""
            self._definitions = grouped
            self._active = active
            self._shadowed = shadowed
            if new_digest != old_digest:
                self._generation += 1
            return self.snapshot()

    def register(self, definition: AgentDefinition, *, replace_same_source: bool = True) -> AgentDefinitionSnapshot:
        self._require_enabled()
        with self._lock:
            grouped = {key: list(value) for key, value in self._definitions.items()}
            existing = grouped.setdefault(definition.agent_type, [])
            same = [item for item in existing if item.source == definition.source]
            if same and not replace_same_source:
                raise AgentDefinitionConflict(definition.agent_type, definition.source.value)
            existing[:] = [item for item in existing if item.source != definition.source]
            existing.append(definition)
        return self.replace(item for values in grouped.values() for item in values)

    def remove(self, agent_type: str, *, source: AgentDefinitionSource | None = None) -> AgentDefinitionSnapshot:
        self._require_enabled()
        with self._lock:
            grouped = {key: list(value) for key, value in self._definitions.items()}
            if agent_type not in grouped:
                raise AgentDefinitionNotFound(agent_type)
            if source is None:
                grouped.pop(agent_type, None)
            else:
                grouped[agent_type] = [item for item in grouped[agent_type] if item.source != source]
                if not grouped[agent_type]:
                    grouped.pop(agent_type, None)
        return self.replace(item for values in grouped.values() for item in values)

    def get(self, agent_type: str, *, required: bool = True) -> AgentDefinition | None:
        self._require_enabled()
        with self._lock:
            definition = self._active.get(str(agent_type))
            result = copy.deepcopy(definition) if definition else None
        if result is None and required:
            raise AgentDefinitionNotFound(str(agent_type))
        return result

    def list(self, *, include_disabled: bool = False) -> tuple[AgentDefinition, ...]:
        self._require_enabled()
        with self._lock:
            selected = sorted(self._active.values(), key=lambda item: item.agent_type)
            if not include_disabled:
                selected = [item for item in selected if item.enabled]
            return tuple(copy.deepcopy(item) for item in selected)

    def snapshot(self) -> AgentDefinitionSnapshot:
        with self._lock:
            active = tuple(copy.deepcopy(item) for item in sorted(self._active.values(), key=lambda item: item.agent_type))
            shadowed = tuple(copy.deepcopy(item) for item in self._shadowed)
            failed = tuple(copy.deepcopy(item) for item in self._failed)
            return AgentDefinitionSnapshot(
                generation=self._generation,
                active=active,
                shadowed=shadowed,
                failed=failed,
                digest=digest_object({
                    "generation": self._generation,
                    "active": [item.to_dict(include_prompt=True) for item in active],
                    "shadowed": [item.to_dict() for item in shadowed],
                    "failed": failed,
                }),
            )

    def load_json_file(
        self,
        path: str | Path,
        *,
        source: AgentDefinitionSource = AgentDefinitionSource.PROJECT,
    ) -> AgentDefinitionSnapshot:
        self._require_enabled()
        selected_path = Path(path).resolve()
        try:
            raw = json.loads(selected_path.read_text(encoding="utf-8"))
            records = raw if isinstance(raw, list) else raw.get("agents", [])
            if not isinstance(records, list):
                raise AgentDefinitionError("agent definition document must contain a list", path=str(selected_path))
            definitions = []
            for item in records:
                if not isinstance(item, Mapping):
                    raise AgentDefinitionError("agent definition entry must be an object", path=str(selected_path))
                data = dict(item)
                data["source"] = source.value
                definitions.append(AgentDefinition.from_dict(data))
        except Exception as error:
            with self._lock:
                self._failed.append({
                    "path": str(selected_path),
                    "source": source.value,
                    "error_type": type(error).__name__,
                    "message": str(error),
                })
            raise
        current = [item for item in self.list(include_disabled=True) if item.source != source]
        return self.replace([*current, *definitions])

    def available_for(
        self,
        *,
        available_tools: Sequence[str],
        available_mcp_servers: Sequence[str] = (),
        capabilities: Sequence[str] = (),
    ) -> tuple[AgentDefinition, ...]:
        available_tool_set = set(available_tools)
        available_mcp_set = set(available_mcp_servers)
        capability_set = set(capabilities)
        selected: list[AgentDefinition] = []
        for definition in self.list():
            if definition.tools and not set(definition.tools).issubset(available_tool_set):
                continue
            if definition.mcp_servers and not set(definition.mcp_servers).issubset(available_mcp_set):
                continue
            if capability_set and definition.capabilities and not capability_set.intersection(definition.capabilities):
                continue
            selected.append(definition)
        return tuple(selected)

    def _resolve(
        self,
        grouped: Mapping[str, list[AgentDefinition]],
    ) -> tuple[dict[str, AgentDefinition], list[AgentDefinitionShadow]]:
        active: dict[str, AgentDefinition] = {}
        shadowed: list[AgentDefinitionShadow] = []
        for agent_type, definitions in grouped.items():
            ordered = sorted(
                definitions,
                key=lambda item: (
                    SOURCE_PRECEDENCE[item.source],
                    item.version,
                    item.definition_id,
                ),
                reverse=True,
            )
            if not ordered:
                continue
            winner = ordered[0]
            active[agent_type] = winner
            for item in ordered[1:]:
                shadowed.append(
                    AgentDefinitionShadow(
                        agent_type=agent_type,
                        active_definition_id=winner.definition_id,
                        shadowed_definition_id=item.definition_id,
                        active_source=winner.source.value,
                        shadowed_source=item.source.value,
                        reason="higher source precedence or later version",
                    )
                )
        return active, shadowed

    def _require_enabled(self) -> None:
        if self.disabled:
            raise SubagentDisabled("AgentDefinitionRegistry")


def built_in_agent_definitions() -> tuple[AgentDefinition, ...]:
    """Zyra-owned, product-facing definitions derived from AgentTool roles."""

    read_tools = ("file_read", "trace", "checkpoint", "list_skills", "read_skill_resource", "SubagentYield")
    edit_tools = (*read_tools, "file_write", "file_edit", "artifact_write", "shell", "skill")
    return (
        AgentDefinition(
            agent_type="explore",
            description="Read-only codebase and evidence exploration with bounded structured output.",
            source=AgentDefinitionSource.BUILTIN,
            tools=read_tools,
            disallowed_tools=("file_write", "file_edit", "shell", "browser", "web_search"),
            permission_mode=PermissionMode.DEFAULT,
            capabilities=("codebase-analysis", "evidence-collection"),
            isolation=SubagentIsolationKind.WORKSPACE,
            system_prompt="Explore the assigned scope. Return conclusions, evidence refs, and open questions only.",
            budget=UsageBudget(max_turns=8, max_tool_calls=24, max_output_tokens=8_000, max_children=0, max_depth=2),
        ),
        AgentDefinition(
            agent_type="plan",
            description="Produce a bounded implementation plan without mutating the workspace.",
            source=AgentDefinitionSource.BUILTIN,
            tools=read_tools,
            disallowed_tools=("file_write", "file_edit", "shell", "browser"),
            permission_mode=PermissionMode.PLAN,
            capabilities=("planning", "dependency-analysis"),
            isolation=SubagentIsolationKind.WORKSPACE,
            system_prompt="Create a concrete plan and return it as a structured handoff. Do not modify files.",
            budget=UsageBudget(max_turns=6, max_tool_calls=18, max_output_tokens=8_000, max_children=0, max_depth=2),
        ),
        AgentDefinition(
            agent_type="code-worker",
            description="Implement an explicitly bounded code change and return artifacts and verification evidence.",
            source=AgentDefinitionSource.BUILTIN,
            tools=edit_tools,
            permission_mode=PermissionMode.DEFAULT,
            capabilities=("code-change", "verification"),
            isolation=SubagentIsolationKind.WORKSPACE,
            system_prompt="Implement only the assigned change. Use the derived child tool and permission scope.",
            budget=UsageBudget(max_turns=16, max_tool_calls=64, max_output_tokens=16_000, max_children=2, max_depth=3),
        ),
        AgentDefinition(
            agent_type="verify",
            description="Independently verify a result without inheriting the implementation scratchpad.",
            source=AgentDefinitionSource.BUILTIN,
            tools=("file_read", "shell", "trace", "artifact_write", "checkpoint"),
            disallowed_tools=("file_write", "file_edit"),
            permission_mode=PermissionMode.DEFAULT,
            capabilities=("verification", "failure-analysis"),
            isolation=SubagentIsolationKind.WORKSPACE,
            system_prompt="Verify the supplied result independently. Return checks, failures, and evidence refs.",
            budget=UsageBudget(max_turns=8, max_tool_calls=32, max_output_tokens=8_000, max_children=0, max_depth=2),
        ),
        AgentDefinition(
            agent_type="general-purpose",
            description="Execute a bounded child task using an explicitly derived tool and permission scope.",
            source=AgentDefinitionSource.BUILTIN,
            tools=edit_tools,
            permission_mode=PermissionMode.DEFAULT,
            capabilities=("analysis", "execution", "verification"),
            isolation=SubagentIsolationKind.WORKSPACE,
            system_prompt="Complete the assigned child task and return a structured handoff to the parent.",
            budget=UsageBudget(max_turns=12, max_tool_calls=48, max_output_tokens=12_000, max_children=2, max_depth=3),
        ),
    )


def default_agent_definition_registry(*, disabled: bool = False) -> AgentDefinitionRegistry:
    return AgentDefinitionRegistry(built_in_agent_definitions(), disabled=disabled)
