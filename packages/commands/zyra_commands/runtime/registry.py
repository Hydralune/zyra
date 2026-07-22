from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from .schemas import (
    CommandAvailability,
    CommandConcurrency,
    CommandExposure,
    CommandKind,
    CommandMutationScope,
    CommandOrigin,
    CommandRegistryCollision,
    CommandRegistrySnapshot,
    CommandSource,
    CommandSourceKind,
    ControlCommandDescriptor,
)


SOURCE_PRECEDENCE: dict[CommandSourceKind, int] = {
    CommandSourceKind.MCP: 10,
    CommandSourceKind.SKILL: 20,
    CommandSourceKind.PLUGIN: 30,
    CommandSourceKind.OPENCODE_ADAPTED: 40,
    CommandSourceKind.PROJECT: 50,
    CommandSourceKind.CLAUDE_MIGRATED: 60,
    CommandSourceKind.ZYRA_BUILTIN: 70,
}


class ControlCommandRegistry:
    """Dynamic command registry with deterministic shadow auditing."""

    def __init__(self, descriptors: Iterable[ControlCommandDescriptor] = (), *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._lock = RLock()
        self._generation = 0
        self._sources: dict[str, tuple[ControlCommandDescriptor, ...]] = {}
        self._active: dict[str, ControlCommandDescriptor] = {}
        self._aliases: dict[str, str] = {}
        self._collisions: tuple[CommandRegistryCollision, ...] = ()
        if descriptors:
            self.replace_source("bootstrap", tuple(descriptors))

    @property
    def generation(self) -> int:
        self._require_enabled()
        with self._lock:
            return self._generation

    def replace_source(
        self,
        source_id: str,
        descriptors: Sequence[ControlCommandDescriptor],
    ) -> CommandRegistrySnapshot:
        self._require_enabled()
        with self._lock:
            previous = _digest_registry(self._active, self._aliases, self._collisions) if self._active else ""
            self._sources[str(source_id)] = tuple(copy.deepcopy(item) for item in descriptors)
            active, aliases, collisions = self._assemble(self._sources)
            next_digest = _digest_registry(active, aliases, collisions)
            self._active = active
            self._aliases = aliases
            self._collisions = collisions
            if next_digest != previous:
                self._generation += 1
            return self.snapshot()

    def remove_source(self, source_id: str) -> CommandRegistrySnapshot:
        self._require_enabled()
        with self._lock:
            self._sources.pop(source_id, None)
            active, aliases, collisions = self._assemble(self._sources)
            self._active = active
            self._aliases = aliases
            self._collisions = collisions
            self._generation += 1
            return self.snapshot()

    def get(self, name: str) -> ControlCommandDescriptor | None:
        self._require_enabled()
        normalized = _normalize_name(name)
        with self._lock:
            canonical = self._aliases.get(normalized, normalized)
            descriptor = self._active.get(canonical)
            return copy.deepcopy(descriptor) if descriptor else None

    def require(self, name: str) -> ControlCommandDescriptor:
        descriptor = self.get(name)
        if descriptor is None:
            raise KeyError(_normalize_name(name))
        return descriptor

    def list(
        self,
        *,
        origin: CommandOrigin | None = None,
        session_mode: str = "default",
        include_disabled: bool = False,
    ) -> list[ControlCommandDescriptor]:
        self._require_enabled()
        with self._lock:
            selected = sorted(self._active.values(), key=lambda item: item.canonical_name)
            result = []
            for descriptor in selected:
                if not include_disabled and not descriptor.availability.enabled:
                    continue
                if origin is not None:
                    if not descriptor.availability.allows(origin=origin, session_mode=session_mode):
                        continue
                    if not descriptor.exposure.allows(origin):
                        continue
                result.append(copy.deepcopy(descriptor))
            return result

    def snapshot(self) -> CommandRegistrySnapshot:
        with self._lock:
            descriptors = tuple(copy.deepcopy(item) for item in sorted(self._active.values(), key=lambda item: item.canonical_name))
            collisions = tuple(copy.deepcopy(item) for item in self._collisions)
            return CommandRegistrySnapshot(
                generation=self._generation,
                descriptors=descriptors,
                collisions=collisions,
                digest=_digest_registry(self._active, self._aliases, collisions, generation=self._generation),
            )

    def merged(self, commands: Sequence[Any]) -> "ControlCommandRegistry":
        """Compatibility bridge for the pre-03D SlashCommandRegistry API."""

        descriptors = list(self.list(include_disabled=True))
        for item in commands:
            if isinstance(item, ControlCommandDescriptor):
                descriptors.append(item)
                continue
            metadata = dict(getattr(item, "metadata", {}) or {})
            descriptors.append(
                ControlCommandDescriptor(
                    canonical_name=str(getattr(item, "name", "")),
                    description=str(getattr(item, "purpose", "")),
                    source=CommandSource(
                        kind=CommandSourceKind.MCP if "mcp" in str(getattr(item, "source", "")).lower() else CommandSourceKind.PROJECT,
                        source_id=str(getattr(item, "source", "dynamic")),
                    ),
                    handler_id=str(metadata.get("handler_id") or "dynamic.command"),
                    aliases=tuple(getattr(item, "aliases", ()) or ()),
                    mutation_scope=CommandMutationScope.MCP if metadata.get("command_kind") == "mcp_prompt" else CommandMutationScope.READ_ONLY,
                    concurrency=CommandConcurrency.SESSION_SERIAL if metadata.get("command_kind") == "mcp_prompt" else CommandConcurrency.READ_ONLY_PARALLEL,
                    metadata=metadata,
                )
            )
        registry = ControlCommandRegistry()
        registry.replace_source("merged", descriptors)
        return registry

    def _assemble(
        self,
        sources: Mapping[str, tuple[ControlCommandDescriptor, ...]],
    ) -> tuple[dict[str, ControlCommandDescriptor], dict[str, str], tuple[CommandRegistryCollision, ...]]:
        grouped: dict[str, list[ControlCommandDescriptor]] = {}
        for descriptors in sources.values():
            for descriptor in descriptors:
                grouped.setdefault(descriptor.canonical_name, []).append(descriptor)
        active: dict[str, ControlCommandDescriptor] = {}
        collisions: list[CommandRegistryCollision] = []
        aliases: dict[str, str] = {}
        for name, candidates in grouped.items():
            ordered = sorted(
                candidates,
                key=lambda item: (
                    SOURCE_PRECEDENCE[item.source.kind],
                    item.source.version,
                    item.handler_id,
                ),
                reverse=True,
            )
            winner = ordered[0]
            active[name] = winner
            for shadowed in ordered[1:]:
                collisions.append(CommandRegistryCollision(
                    name=name,
                    active_source=winner.source.source_id,
                    shadowed_source=shadowed.source.source_id,
                    active_handler_id=winner.handler_id,
                    shadowed_handler_id=shadowed.handler_id,
                    reason="deterministic source precedence",
                ))
        for descriptor in active.values():
            for alias in descriptor.aliases:
                existing = aliases.get(alias)
                if existing and existing != descriptor.canonical_name:
                    winner = active[existing]
                    candidate = descriptor
                    if SOURCE_PRECEDENCE[candidate.source.kind] > SOURCE_PRECEDENCE[winner.source.kind]:
                        aliases[alias] = candidate.canonical_name
                    collisions.append(CommandRegistryCollision(
                        name=alias,
                        active_source=active[aliases[alias]].source.source_id,
                        shadowed_source=(winner.source.source_id if aliases[alias] == candidate.canonical_name else candidate.source.source_id),
                        active_handler_id=active[aliases[alias]].handler_id,
                        shadowed_handler_id=(winner.handler_id if aliases[alias] == candidate.canonical_name else candidate.handler_id),
                        reason="alias collision",
                    ))
                else:
                    aliases[alias] = descriptor.canonical_name
        return active, aliases, tuple(collisions)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise RuntimeError("ControlCommandRegistry is disabled")


def built_in_control_descriptors() -> tuple[ControlCommandDescriptor, ...]:
    claude = CommandSource(
        CommandSourceKind.CLAUDE_MIGRATED,
        "claude-code-best",
        source_paths=("src/commands.ts", "src/entrypoints/sdk/controlSchemas.ts", "src/cli/structuredIO.ts"),
    )
    zyra = CommandSource(CommandSourceKind.ZYRA_BUILTIN, "zyra")

    def descriptor(
        name: str,
        description: str,
        handler: str,
        *,
        source: CommandSource = claude,
        scope: CommandMutationScope = CommandMutationScope.READ_ONLY,
        concurrency: CommandConcurrency = CommandConcurrency.READ_ONLY_PARALLEL,
        immediate: bool = True,
        aliases: tuple[str, ...] = (),
        category: str = "runtime",
        hint: str = "",
        remote: bool = True,
        agent: bool = False,
        permission_action: str = "inspect",
        event_hint: str = "control_command",
    ) -> ControlCommandDescriptor:
        return ControlCommandDescriptor(
            canonical_name=name,
            description=description,
            source=source,
            handler_id=handler,
            aliases=aliases,
            argument_hint=hint,
            exposure=CommandExposure(local=True, api=True, remote=remote, agent=agent),
            mutation_scope=scope,
            concurrency=concurrency,
            immediate=immediate,
            permission_action=permission_action,
            category=category,
            metadata={"event_hint": event_hint, "requires_task": True},
        )

    return (
        descriptor("/context", "Inspect live context usage and provenance.", "session.context", category="context"),
        descriptor("/compact", "Compact the canonical session context.", "session.compact", scope=CommandMutationScope.CONTEXT, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, hint="[instructions]", category="context", permission_action="context.compact"),
        descriptor("/btw", "Ask one tool-free side question without mutating main messages.", "side_question.ask", hint="<question>", category="context"),
        descriptor("/clear", "Checkpoint and reset the active session epoch.", "session.clear", scope=CommandMutationScope.SESSION, concurrency=CommandConcurrency.INTERRUPTING, immediate=False, category="session", permission_action="session.clear"),
        descriptor("/rewind", "Restore an exact session or file-history checkpoint.", "session.rewind", scope=CommandMutationScope.SESSION, concurrency=CommandConcurrency.INTERRUPTING, immediate=False, hint="<checkpoint> [--dry-run]", category="session", permission_action="session.rewind"),
        descriptor("/resume", "Resume an exact canonical session checkpoint.", "session.resume", scope=CommandMutationScope.SESSION, concurrency=CommandConcurrency.INTERRUPTING, immediate=False, hint="<session-id>", category="session", permission_action="session.resume"),
        descriptor("/export", "Export a redacted trace and artifact manifest.", "artifact.export", scope=CommandMutationScope.ARTIFACT, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, hint="[format]", category="session", permission_action="artifact.export"),
        descriptor("/cost", "Inspect measured and estimated costs with provenance.", "usage.cost", category="observability"),
        descriptor("/usage", "Inspect token, time and runtime budgets.", "usage.inspect", category="observability"),
        descriptor("/memory", "Inspect MemoryFabric retrieval, compact and replay state.", "memory.inspect", hint="[query]", category="memory"),
        descriptor("/doctor", "Run real runtime health checks.", "runtime.doctor", category="observability", hint="[--repair]"),
        descriptor("/mcp", "Inspect or mutate the M1-03B MCP runtime.", "mcp.control", scope=CommandMutationScope.MCP, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="extensions", permission_action="mcp.control"),
        descriptor("/permissions", "Inspect or mutate the M1-03A permission runtime.", "permission.control", scope=CommandMutationScope.PERMISSION, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="security", permission_action="permission.control"),
        descriptor("/agents", "Inspect agent definitions and logical subagent tasks.", "subagent.inspect", category="agents"),
        descriptor("/hooks", "Inspect or atomically reload the M1-03C hook/plugin projection.", "skill_plugin.hooks", scope=CommandMutationScope.SKILL_PLUGIN, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="extensions", permission_action="plugin.reload"),
        descriptor("/plan", "Transition the session into plan-first permission mode.", "permission.plan", scope=CommandMutationScope.PERMISSION, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="session", permission_action="permission.mode"),
        descriptor("/model", "Inspect or switch the effective model projection.", "provider.model", scope=CommandMutationScope.PROVIDER, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="provider", permission_action="model.set"),
        descriptor("/goal", "Inspect or update the root task objective.", "task.goal", source=zyra, scope=CommandMutationScope.TASK_GRAPH, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="task", permission_action="task.goal"),
        descriptor("/team-onboarding", "Generate a sourced onboarding artifact.", "task.onboarding", source=zyra, scope=CommandMutationScope.ARTIFACT, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="task", permission_action="artifact.write"),
        descriptor("/status", "Inspect current task status.", "task.status", source=zyra, category="observability"),
        descriptor("/graph", "Inspect current task graph.", "task.graph", source=zyra, category="observability"),
        descriptor("/trace", "Inspect canonical event trace.", "task.trace", source=zyra, category="observability"),
        descriptor("/scheduler", "Inspect resource scheduler, worker manifests, health and recovery routes.", "scheduler.inspect", source=zyra, category="observability"),
        descriptor("/artifacts", "Inspect task artifacts.", "artifact.list", source=zyra, category="observability"),
        descriptor("/skills", "Inspect M1-03C skill registry and invocation state.", "skill.list", category="extensions"),
        descriptor("/tools", "Inspect the executable tool registry.", "tool.list", category="runtime"),
        descriptor("/inject", "Inject a failure through the task graph owner.", "task.inject", source=zyra, scope=CommandMutationScope.TASK_GRAPH, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="competition", permission_action="task.inject", event_hint="failure_injected"),
        descriptor("/watchdog", "Inspect or control active watchdog sources and recovery handoffs.", "watchdog.control", source=zyra, scope=CommandMutationScope.TASK_GRAPH, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="competition", permission_action="task.inject", event_hint="watchdog_control"),
        descriptor("/change", "Inject a requirement change through the task graph owner.", "task.change", source=zyra, aliases=("/需求变更",), scope=CommandMutationScope.TASK_GRAPH, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="competition", permission_action="task.change", event_hint="requirement_change"),
        descriptor("/verify", "Run the task verifier.", "task.verify", source=zyra, scope=CommandMutationScope.TASK_GRAPH, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="competition", event_hint="evaluation"),
        descriptor("/eval", "Run trace evaluation.", "task.evaluate", source=zyra, scope=CommandMutationScope.TASK_GRAPH, concurrency=CommandConcurrency.SESSION_SERIAL, immediate=False, category="competition", permission_action="task.evaluate", event_hint="evaluation"),
        descriptor("/help", "List commands, sources and availability.", "registry.help", source=zyra, category="runtime"),
    )


def default_control_command_registry(*, disabled: bool = False) -> ControlCommandRegistry:
    registry = ControlCommandRegistry(disabled=disabled)
    registry.replace_source("builtin", built_in_control_descriptors())
    return registry


def _normalize_name(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _digest_registry(
    active: Mapping[str, ControlCommandDescriptor],
    aliases: Mapping[str, str],
    collisions: Sequence[CommandRegistryCollision],
    *,
    generation: int = 0,
) -> str:
    payload = json.dumps({
        "generation": generation,
        "active": {key: value.to_dict() for key, value in sorted(active.items())},
        "aliases": dict(sorted(aliases.items())),
        "collisions": [item.to_dict() for item in collisions],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
