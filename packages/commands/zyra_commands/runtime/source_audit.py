from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CommandSourceDecision:
    source_repository: str
    source_paths: tuple[str, ...]
    mechanism: str
    target_paths: tuple[str, ...]
    disposition: str
    adaptation: str
    main_path_entry: str
    downstream_owner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repository": self.source_repository,
            "source_paths": list(self.source_paths),
            "mechanism": self.mechanism,
            "target_paths": list(self.target_paths),
            "disposition": self.disposition,
            "adaptation": self.adaptation,
            "main_path_entry": self.main_path_entry,
            "downstream_owner": self.downstream_owner,
        }


COMMAND_SOURCE_DECISIONS = (
    CommandSourceDecision(
        "claude-code-best",
        ("src/commands.ts", "src/entrypoints/sdk/controlSchemas.ts", "src/cli/structuredIO.ts"),
        "command discovery, strict control frames and structured IO",
        ("packages/commands/zyra_commands/runtime/registry.py", "packages/commands/zyra_commands/runtime/dispatcher.py", "packages/commands/zyra_commands/runtime/structured_io.py"),
        "zyra_module_migrated",
        "Replaced static command tables and event-only ACKs with source-aware registry generations, durable lifecycle state, owner-bound dispatch and strict request/response frames.",
        "POST /tasks/{task_id}/commands and RuntimeControlDispatcher.submit",
    ),
    CommandSourceDecision(
        "claude-code-best",
        ("src/entrypoints/cli.tsx", "src/screens/REPL.tsx", "src/components/PromptQueue.tsx"),
        "now/next/later prompt and control queue",
        ("packages/commands/zyra_commands/runtime/prompt_queue.py",),
        "zyra_module_migrated",
        "Made queue state durable, idempotent and explicitly isolated by canonical session and logical subagent target.",
        "RuntimeControlDispatcher.submit/drain_one",
    ),
    CommandSourceDecision(
        "claude-code-best",
        ("src/screens/Btw.tsx", "src/screens/REPL.tsx"),
        "one-turn side questions",
        ("packages/commands/zyra_commands/runtime/side_question.py",),
        "zyra_module_migrated",
        "Added immutable parent snapshot, zero-tool enforcement, independent transcript/usage and proof that main messages and replanning are unchanged.",
        "/btw through side_question.ask",
    ),
    CommandSourceDecision(
        "opencode",
        ("packages/opencode/src/command/index.ts", "packages/opencode/src/session/prompt.ts"),
        "typed command metadata and session-scoped dispatch",
        ("packages/commands/zyra_commands/runtime/schemas.py", "packages/commands/zyra_commands/runtime/registry.py"),
        "zyra_module_migrated",
        "Normalized provider-specific command metadata into Zyra command sources, exposure, concurrency and mutation ownership.",
        "ControlCommandRegistry",
    ),
    CommandSourceDecision(
        "zyra",
        ("packages/commands/zyra_commands/registry.py", "apps/api/zyra_api/main.py"),
        "legacy slash-command table and event-only API path",
        ("packages/commands/zyra_commands/runtime/", "apps/api/zyra_api/main.py"),
        "active_adapter_port",
        "Legacy parsing remains compatible, but stateful handlers must bind a canonical owner and cannot fall back to metadata-only acknowledgements.",
        "POST /tasks/{task_id}/commands",
        "M1-03D-02 adds further interactive surfaces; M2 owns web console UX.",
    ),
)


def command_source_audit() -> dict[str, Any]:
    rows = [item.to_dict() for item in COMMAND_SOURCE_DECISIONS]
    return {
        "schema": "zyra.command-source-audit/v1",
        "decisions": rows,
        "counts": {
            disposition: sum(1 for row in rows if row["disposition"] == disposition)
            for disposition in sorted({row["disposition"] for row in rows})
        },
        "event_only_stateful_fallback_allowed": False,
        "physical_worker_state_owned": False,
    }
