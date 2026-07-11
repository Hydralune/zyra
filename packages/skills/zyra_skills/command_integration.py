from __future__ import annotations

import copy
import json
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .digests import digest_object
from .integration_errors import (
    SkillCommandArgumentError,
    SkillCommandNotFound,
    SkillCommandNotInvocable,
    SkillCommandParseError,
)
from .models import SkillInvocationRequest, new_id, utc_now
from .plugin_integration import PluginCapabilityIntegrationRuntime, PluginCommandCapability
from .session_integration import SkillSessionIntegrationRuntime


class SkillCommandKind(StrEnum):
    INVOKE = "invoke"
    LIST = "list"
    INSPECT = "inspect"
    COMPLETE = "complete"
    CANCEL = "cancel"
    PLUGIN = "plugin"


@dataclass(frozen=True, slots=True)
class ParsedSkillCommand:
    raw: str
    name: str
    kind: SkillCommandKind
    skill_name: str = ""
    invocation_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    resources: tuple[str, ...] = ()
    plugin_command: PluginCommandCapability | None = None
    parse_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "name": self.name,
            "kind": str(self.kind),
            "skill_name": self.skill_name,
            "invocation_id": self.invocation_id,
            "arguments": copy.deepcopy(self.arguments),
            "resources": list(self.resources),
            "plugin_command": self.plugin_command.to_dict() if self.plugin_command else None,
            "parse_digest": self.parse_digest,
        }


@dataclass(frozen=True, slots=True)
class SkillCommandExecution:
    command: ParsedSkillCommand
    ok: bool
    summary: str
    data: dict[str, Any]
    session_checkpoint: dict[str, Any]
    event_refs: tuple[str, ...]
    executed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command.to_dict(),
            "ok": self.ok,
            "summary": self.summary,
            "data": copy.deepcopy(self.data),
            "session_checkpoint": copy.deepcopy(self.session_checkpoint),
            "event_refs": list(self.event_refs),
            "executed_at": self.executed_at,
        }


class SkillCommandParser:
    """Strict slash parser shared by API command and prompt command paths."""

    BUILTINS = {
        "/skill": SkillCommandKind.INVOKE,
        "/skills": SkillCommandKind.LIST,
        "/skill-inspect": SkillCommandKind.INSPECT,
        "/skill-complete": SkillCommandKind.COMPLETE,
        "/skill-cancel": SkillCommandKind.CANCEL,
    }

    def __init__(self, plugin_runtime: PluginCapabilityIntegrationRuntime | None = None) -> None:
        self.plugin_runtime = plugin_runtime

    def parse(self, text: str) -> ParsedSkillCommand | None:
        raw = str(text).strip()
        if not raw.startswith("/"):
            return None
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError as error:
            raise SkillCommandParseError("skill command quoting is invalid") from error
        if not tokens:
            return None
        command_name = tokens[0]
        kind = self.BUILTINS.get(command_name)
        plugin_command = None
        if kind is None and self.plugin_runtime is not None:
            try:
                plugin_command = self.plugin_runtime.resolve_command(command_name)
            except Exception:
                plugin_command = None
            if plugin_command is not None:
                kind = SkillCommandKind.PLUGIN
        if kind is None:
            return None
        arguments, resources, positionals = self._parse_options(tokens[1:])
        skill_name = ""
        invocation_id = ""
        if kind is SkillCommandKind.INVOKE:
            if not positionals:
                raise SkillCommandArgumentError("/skill requires a skill name")
            skill_name = positionals[0].removeprefix("/")
            if len(positionals) > 1:
                arguments.setdefault("raw", " ".join(positionals[1:]))
        elif kind is SkillCommandKind.PLUGIN:
            if plugin_command is None:
                raise SkillCommandNotFound("plugin skill command disappeared during parsing")
            skill_name = plugin_command.target_skill
            merged = copy.deepcopy(plugin_command.default_arguments)
            merged.update(arguments)
            arguments = merged
            if positionals:
                arguments.setdefault("raw", " ".join(positionals))
        elif kind in {SkillCommandKind.COMPLETE, SkillCommandKind.CANCEL}:
            if not positionals:
                raise SkillCommandArgumentError(f"{command_name} requires invocation_id")
            invocation_id = positionals[0]
        elif kind is SkillCommandKind.INSPECT:
            if positionals:
                skill_name = positionals[0].removeprefix("/")
        elif kind is SkillCommandKind.LIST and positionals:
            arguments.setdefault("query", " ".join(positionals))
        payload = {
            "raw": raw,
            "name": command_name,
            "kind": str(kind),
            "skill_name": skill_name,
            "invocation_id": invocation_id,
            "arguments": arguments,
            "resources": resources,
            "plugin_ref": plugin_command.identity.immutable_ref if plugin_command else "",
        }
        return ParsedSkillCommand(
            raw=raw,
            name=command_name,
            kind=kind,
            skill_name=skill_name,
            invocation_id=invocation_id,
            arguments=arguments,
            resources=resources,
            plugin_command=plugin_command,
            parse_digest=digest_object(payload),
        )

    def _parse_options(
        self,
        tokens: Sequence[str],
    ) -> tuple[dict[str, Any], tuple[str, ...], list[str]]:
        arguments: dict[str, Any] = {}
        resources: list[str] = []
        positionals: list[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                positionals.extend(tokens[index + 1 :])
                break
            if token == "--resource":
                index += 1
                if index >= len(tokens):
                    raise SkillCommandArgumentError("--resource requires a path")
                resources.append(tokens[index])
            elif token == "--args-json":
                index += 1
                if index >= len(tokens):
                    raise SkillCommandArgumentError("--args-json requires an object")
                try:
                    value = json.loads(tokens[index])
                except json.JSONDecodeError as error:
                    raise SkillCommandArgumentError("--args-json is not valid JSON") from error
                if not isinstance(value, dict):
                    raise SkillCommandArgumentError("--args-json must decode to an object")
                arguments.update(value)
            elif token.startswith("--set="):
                self._apply_set(arguments, token.removeprefix("--set="))
            elif token == "--set":
                index += 1
                if index >= len(tokens):
                    raise SkillCommandArgumentError("--set requires KEY=VALUE")
                self._apply_set(arguments, tokens[index])
            elif token.startswith("--"):
                raise SkillCommandArgumentError(
                    "unknown skill command option",
                    detail={"option": token},
                )
            else:
                positionals.append(token)
            index += 1
        if len(resources) != len(set(resources)):
            raise SkillCommandArgumentError("skill command repeats a resource path")
        return arguments, tuple(resources), positionals

    def _apply_set(self, arguments: dict[str, Any], value: str) -> None:
        key, separator, raw = value.partition("=")
        if not separator or not key.strip():
            raise SkillCommandArgumentError("--set requires KEY=VALUE")
        key = key.strip()
        if key in arguments:
            raise SkillCommandArgumentError(
                "skill command argument is duplicated",
                detail={"key": key},
            )
        arguments[key] = raw


class SkillCommandIntegrationRuntime:
    def __init__(
        self,
        *,
        session_runtime: SkillSessionIntegrationRuntime,
        plugin_runtime: PluginCapabilityIntegrationRuntime | None = None,
    ) -> None:
        self.session_runtime = session_runtime
        self.plugin_runtime = plugin_runtime
        self.parser = SkillCommandParser(plugin_runtime)

    def execute(self, text: str, *, parent_tool_use_id: str = "") -> SkillCommandExecution | None:
        command = self.parser.parse(text)
        if command is None:
            return None
        if command.kind in {SkillCommandKind.INVOKE, SkillCommandKind.PLUGIN}:
            return self._invoke(command, parent_tool_use_id=parent_tool_use_id)
        if command.kind is SkillCommandKind.LIST:
            return self._list(command)
        if command.kind is SkillCommandKind.INSPECT:
            return self._inspect(command)
        if command.kind is SkillCommandKind.COMPLETE:
            return self._complete(command)
        if command.kind is SkillCommandKind.CANCEL:
            return self._cancel(command)
        raise SkillCommandNotInvocable("unsupported skill command kind")

    def command_specs(self) -> tuple[Any, ...]:
        try:
            from zyra_commands import CommandSpec
        except ImportError as error:  # pragma: no cover
            raise SkillCommandNotInvocable("zyra_commands is unavailable") from error
        values = [
            CommandSpec(
                name=name,
                purpose={
                    SkillCommandKind.INVOKE: "Invoke a versioned skill in the current session.",
                    SkillCommandKind.LIST: "List available skills without loading bodies.",
                    SkillCommandKind.INSPECT: "Inspect immutable skill metadata.",
                    SkillCommandKind.COMPLETE: "Commit a skill invocation outcome.",
                    SkillCommandKind.CANCEL: "Cancel an active skill invocation.",
                }[kind],
                source="M1-03C SkillCommandIntegrationRuntime",
                event_hint="skill_invoked",
                requires_task=True,
                metadata={"category": "skill_runtime", "runtime_status": "stateful"},
            )
            for name, kind in self.parser.BUILTINS.items()
        ]
        if self.plugin_runtime is not None:
            values.extend(self.plugin_runtime.command_specs())
        return tuple(values)

    def _invoke(
        self,
        command: ParsedSkillCommand,
        *,
        parent_tool_use_id: str,
    ) -> SkillCommandExecution:
        invocation_id = new_id("skillinv")
        request = SkillInvocationRequest(
            run_id=self.session_runtime.run_id,
            task_id=self.session_runtime.task_id,
            session_id=self.session_runtime.session_id,
            agent_id=self.session_runtime.agent_id,
            skill_name=command.skill_name,
            arguments=copy.deepcopy(command.arguments),
            parent_tool_use_id=parent_tool_use_id or invocation_id,
            idempotency_key=digest_object(
                {
                    "parse_digest": command.parse_digest,
                    "session_id": self.session_runtime.session_id,
                    "invocation_id": invocation_id,
                }
            ),
            interactive=True,
            headless=False,
            invocation_id=invocation_id,
        )
        plan = self.session_runtime.invoke(
            request,
            load_resources=command.resources,
            require_model_invocable=False,
            require_user_invocable=True,
        )
        if command.plugin_command and self.plugin_runtime:
            self.plugin_runtime.register_plugin_hooks(
                runtime=self.session_runtime.runtime,
                invocation_id=plan.state.invocation_id,
                session_id=plan.state.session_id,
                version_ref=plan.state.version_ref,
                skill_name=plan.revision.qualified_name,
            )
        aggregate = self.session_runtime.aggregate()
        return SkillCommandExecution(
            command=command,
            ok=True,
            summary=f"Invoked {plan.revision.qualified_name} as {plan.state.status}.",
            data={
                "invocation": plan.to_dict(include_body=False),
                "disclosure_pending": str(plan.revision.metadata.invocation.mode) == "inline",
                "body_in_checkpoint": False,
            },
            session_checkpoint=aggregate.to_dict(),
            event_refs=(aggregate.last_event_id,),
        )

    def _list(self, command: ParsedSkillCommand) -> SkillCommandExecution:
        query = str(command.arguments.get("query") or "")
        if query:
            from .search import SkillSearchIndex

            entries = [item.to_dict() for item in SkillSearchIndex(self.session_runtime.runtime.registry).search(query).hits]
        else:
            entries = [item.to_dict() for item in self.session_runtime.runtime.list(for_user=True)]
        aggregate = self.session_runtime.aggregate()
        return SkillCommandExecution(
            command=command,
            ok=True,
            summary=f"Found {len(entries)} skills.",
            data={"skills": entries, "body_disclosed": False},
            session_checkpoint=aggregate.to_dict(),
            event_refs=(),
        )

    def _inspect(self, command: ParsedSkillCommand) -> SkillCommandExecution:
        if not command.skill_name:
            raise SkillCommandArgumentError("/skill-inspect requires a skill name")
        revision = self.session_runtime.runtime.registry.resolve(command.skill_name)
        aggregate = self.session_runtime.aggregate()
        return SkillCommandExecution(
            command=command,
            ok=True,
            summary=f"Immutable metadata for {revision.qualified_name}.",
            data={"skill": revision.to_dict(include_paths=False), "body_disclosed": False},
            session_checkpoint=aggregate.to_dict(),
            event_refs=(),
        )

    def _complete(self, command: ParsedSkillCommand) -> SkillCommandExecution:
        state = self.session_runtime.complete(
            command.invocation_id,
            outcome_refs=tuple(str(item) for item in command.arguments.get("outcome_refs") or ()),
            evidence_refs=tuple(str(item) for item in command.arguments.get("evidence_refs") or ()),
            artifact_refs=tuple(str(item) for item in command.arguments.get("artifact_refs") or ()),
        )
        aggregate = self.session_runtime.aggregate()
        return SkillCommandExecution(
            command=command,
            ok=True,
            summary=f"Completed skill invocation {state.invocation_id}.",
            data={"state": state.to_dict(), "outcome_only_restore": True},
            session_checkpoint=aggregate.to_dict(),
            event_refs=(aggregate.last_event_id,),
        )

    def _cancel(self, command: ParsedSkillCommand) -> SkillCommandExecution:
        reason = str(command.arguments.get("reason") or "cancelled by skill command")
        state = self.session_runtime.cancel(command.invocation_id, reason=reason)
        aggregate = self.session_runtime.aggregate()
        return SkillCommandExecution(
            command=command,
            ok=True,
            summary=f"Cancelled skill invocation {state.invocation_id}.",
            data={"state": state.to_dict()},
            session_checkpoint=aggregate.to_dict(),
            event_refs=(aggregate.last_event_id,),
        )
