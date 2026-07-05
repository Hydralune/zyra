from __future__ import annotations

from dataclasses import dataclass, field

from zyra_core import AgentRole, ControlCommand

COMMAND_RUNTIME_EVENT_ONLY = "event_only"
COMMAND_RUNTIME_STATEFUL = "stateful"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    purpose: str
    source: str
    event_hint: str
    aliases: tuple[str, ...] = ()
    requires_task: bool = True
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedSlashCommand:
    spec: CommandSpec
    argument_text: str
    control_command: ControlCommand


class SlashCommandRegistry:
    def __init__(self, commands: list[CommandSpec]) -> None:
        self._commands = {command.name: command for command in commands}
        self._aliases: dict[str, str] = {}
        for command in commands:
            for alias in command.aliases:
                self._aliases[alias] = command.name

    def get(self, name: str) -> CommandSpec | None:
        normalized = name if name.startswith("/") else f"/{name}"
        resolved = self._aliases.get(normalized, normalized)
        return self._commands.get(resolved)

    def list(self) -> list[CommandSpec]:
        return list(self._commands.values())


def default_command_registry() -> SlashCommandRegistry:
    return SlashCommandRegistry(
        [
            _command("/status", "Inspect current run state.", "zyra", "control_command", "runtime_observation"),
            _command("/graph", "Inspect task graph and dependencies.", "zyra", "control_command", "runtime_observation"),
            _command("/trace", "Inspect event trace.", "zyra", "control_command", "runtime_observation"),
            _command("/agents", "Inspect active worker and subagent state.", "claude-code-best AgentTool", "control_command", "runtime_observation"),
            _command("/artifacts", "Inspect artifact refs and outputs.", "zyra", "control_command", "runtime_observation"),
            _command("/tools", "Inspect registered tools and MCP/worker capabilities.", "claude-code-best tool registry", "control_command", "runtime_observation"),
            _command("/permissions", "Inspect or modify tool permission policy.", "claude-code-best permission runtime", "control_command", "runtime_observation"),
            _command("/help", "List available control commands and their purposes.", "claude-code-best help command", "control_command", "runtime_observation"),
            _command("/bashes", "List and manage background shell tasks.", "claude-code-best background bash/session tasks", "control_command", "runtime_observation"),
            _command("/clear", "Request a new visible conversation context for the current task.", "claude-code-best clear command", "control_command", "context_session", runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/compact", "Trigger context or trace compaction, optionally focused by argument text.", "claude-code-best compact service", "control_command", "context_session", runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/context", "Inspect context window composition and token pressure.", "claude-code-best context command", "control_command", "context_session"),
            _command("/rewind", "Request recovery of a previous context point after clear or rollback.", "claude-code-best rewind command", "control_command", "context_session", runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/resume", "Resume a prior task or session by id/name.", "claude-code-best resume/session command", "control_command", "context_session", runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/export", "Export the current conversation, event trace, and artifact references.", "claude-code-best export command", "control_command", "context_session", runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/memory", "Inspect, refresh, or edit memory state.", "claude-code-best memory command", "control_command", "context_session", runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/init", "Initialize project memory and local agent instructions.", "claude-code-best init command", "control_command", "context_session"),
            _command("/model", "Inspect or switch model/provider selection.", "claude-code-best model command", "control_command", "model_resource"),
            _command("/doctor", "Run environment and toolchain health checks.", "claude-code-best doctor command", "control_command", "model_resource"),
            _command("/cost", "Inspect token, tool, time, and cost budgets.", "claude-code-best cost tracker", "budget_updated", "model_resource"),
            _command("/usage", "Inspect token and resource usage.", "claude-code-best usage command", "budget_updated", "model_resource"),
            _command("/mcp", "Manage MCP servers and external tool/data sources.", "claude-code-best MCP command", "control_command", "extension_team"),
            _command("/hooks", "Inspect or update runtime lifecycle hooks.", "claude-code-best hooks/plugin runtime", "control_command", "extension_team"),
            _command("/skills", "Inspect registered skills and recent skill invocations.", "claude-code-best SkillTool/skills command", "control_command", "extension_team"),
            _command("/plan", "Enter a plan-first control mode for high-risk tasks.", "claude-code-best plan mode command", "control_command", "extension_team"),
            _command("/goal", "Register or inspect a long-running task goal.", "claude-code-best goal command", "control_command", "extension_team"),
            _command("/team-onboarding", "Generate onboarding material from memory, skills, agents, hooks, and recent workflows.", "claude-code-best team onboarding workflow", "control_command", "extension_team"),
            _command("/inject", "Inject a failure or runtime perturbation.", "zyra harness", "failure_injected", "competition_harness", runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/change", "Inject a running requirement change.", "zyra harness", "requirement_change", "competition_harness", aliases=("/需求变更",), runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/verify", "Run verification or evaluator checks.", "claude-code-best verifier", "evaluation", "competition_harness", runtime_status=COMMAND_RUNTIME_STATEFUL),
            _command("/eval", "Run scenario or trace evaluation.", "zyra evaluation harness", "evaluation", "competition_harness", runtime_status=COMMAND_RUNTIME_STATEFUL),
        ]
    )


def _command(
    name: str,
    purpose: str,
    source: str,
    event_hint: str,
    category: str,
    *,
    aliases: tuple[str, ...] = (),
    requires_task: bool = True,
    runtime_status: str = COMMAND_RUNTIME_EVENT_ONLY,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        purpose=purpose,
        source=source,
        event_hint=event_hint,
        aliases=aliases,
        requires_task=requires_task,
        metadata={
            "category": category,
            "runtime_status": runtime_status,
        },
    )


def parse_slash_command(
    text: str,
    run_id: str,
    task_id: str,
    issued_by: AgentRole = AgentRole.USER,
    registry: SlashCommandRegistry | None = None,
) -> ParsedSlashCommand | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    command_name, _, argument_text = stripped.partition(" ")
    command_registry = registry or default_command_registry()
    spec = command_registry.get(command_name)
    if spec is None:
        return None

    return ParsedSlashCommand(
        spec=spec,
        argument_text=argument_text.strip(),
        control_command=ControlCommand(
            run_id=run_id,
            task_id=task_id,
            name=spec.name,
            arguments={"raw": argument_text.strip()},
            issued_by=issued_by,
            metadata={"source": spec.source, "event_hint": spec.event_hint, **spec.metadata},
        ),
    )
