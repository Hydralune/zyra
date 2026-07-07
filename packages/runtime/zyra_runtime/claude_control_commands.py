from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, new_id, now_iso, to_jsonable

from .artifacts import LocalArtifactStore
from .claude_context_window import ClaudeContextWindowManager
from .claude_session_lifecycle import ClaudeSessionLifecycleRuntime, ClaudeSessionResumePlan
from .claude_tool_use_runtime import ClaudeToolUseRuntime
from .permissions import JsonPermissionStore, PermissionEffect, PermissionOperation, PermissionRule


class ClaudeControlCommandName(StrEnum):
    CONTEXT = "context"
    COMPACT = "compact"
    COST = "cost"
    DOCTOR = "doctor"
    DIFF = "diff"
    MEMORY = "memory"
    PERMISSIONS = "permissions"
    RESUME = "resume"
    SKILLS = "skills"
    TOOLS = "tools"


class ClaudeControlCommandStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"


class ClaudeControlArtifactPolicy(StrEnum):
    INLINE = "inline"
    ARTIFACT = "artifact"
    BOTH = "both"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ClaudeControlCommand:
    name: ClaudeControlCommandName
    arguments: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: new_id("ctrl"))
    requested_at: str = field(default_factory=now_iso)
    artifact_policy: ClaudeControlArtifactPolicy = ClaudeControlArtifactPolicy.INLINE
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_raw(cls, raw: Any) -> "ClaudeControlCommand | None":
        if isinstance(raw, str):
            return cls(name=_command_name(raw), arguments={})
        if not isinstance(raw, Mapping):
            return None
        name_value = raw.get("name") or raw.get("command") or raw.get("type")
        if not name_value:
            return None
        policy = _enum_or_default(
            ClaudeControlArtifactPolicy,
            raw.get("artifact_policy") or raw.get("artifactPolicy") or "inline",
            ClaudeControlArtifactPolicy.INLINE,
        )
        args = raw.get("arguments") if isinstance(raw.get("arguments"), Mapping) else raw.get("args")
        if not isinstance(args, Mapping):
            args = {key: value for key, value in raw.items() if key not in {"name", "command", "type", "artifact_policy", "artifactPolicy", "metadata"}}
        return cls(
            name=_command_name(name_value),
            arguments=dict(args),
            command_id=str(raw.get("command_id") or raw.get("id") or new_id("ctrl")),
            artifact_policy=policy,
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ClaudeControlCommandResult:
    command_id: str
    name: ClaudeControlCommandName
    status: ClaudeControlCommandStatus
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    artifact: ArtifactRef | None = None
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == ClaudeControlCommandStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "name": str(self.name),
            "status": str(self.status),
            "ok": self.ok,
            "summary": self.summary,
            "payload": to_jsonable(self.payload),
            "artifact": to_jsonable(self.artifact) if self.artifact else None,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClaudeControlRuntimeReport:
    results: list[ClaudeControlCommandResult]
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def artifacts(self) -> list[ArtifactRef]:
        return [result.artifact for result in self.results if result.artifact is not None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "created_at": self.created_at,
            "results": [result.to_dict() for result in self.results],
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(slots=True)
class ClaudeControlRuntimeState:
    project_root: Path
    workspace_root: Path
    artifact_store: LocalArtifactStore
    runtime_source: str
    runtime_id: str
    context_window: ClaudeContextWindowManager | None = None
    tool_runtime: ClaudeToolUseRuntime | None = None
    session_lifecycle: ClaudeSessionLifecycleRuntime | None = None
    permission_store: JsonPermissionStore | None = None
    event_reader: Callable[[str], list[dict[str, Any]]] | None = None
    checkpoint_reader: Callable[[str], dict[str, Any] | None] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ClaudeControlCommandRuntime:
    """Runtime implementation for Claude-Code-like control commands."""

    def __init__(self, state: ClaudeControlRuntimeState) -> None:
        self.state = state
        self._results: list[ClaudeControlCommandResult] = []

    @property
    def results(self) -> list[ClaudeControlCommandResult]:
        return list(self._results)

    def run_commands(
        self,
        raw_commands: Sequence[Any],
        *,
        run_id: str,
        task_id: str,
        producer_node_id: str | None = None,
    ) -> ClaudeControlRuntimeReport:
        commands = [command for command in (ClaudeControlCommand.from_raw(item) for item in raw_commands) if command is not None]
        results: list[ClaudeControlCommandResult] = []
        for command in commands:
            result = self.run_command(command, run_id=run_id, task_id=task_id, producer_node_id=producer_node_id)
            results.append(result)
            self._results.append(result)
        return ClaudeControlRuntimeReport(
            results=results,
            metadata={
                "runtime_source": self.state.runtime_source,
                "runtime_id": self.state.runtime_id,
                "command_count": len(results),
                "artifact_count": len([item for item in results if item.artifact is not None]),
            },
        )

    def run_command(
        self,
        command: ClaudeControlCommand,
        *,
        run_id: str,
        task_id: str,
        producer_node_id: str | None = None,
    ) -> ClaudeControlCommandResult:
        try:
            if command.name == ClaudeControlCommandName.CONTEXT:
                result = self._context(command)
            elif command.name == ClaudeControlCommandName.COMPACT:
                result = self._compact(command, run_id=run_id, task_id=task_id, producer_node_id=producer_node_id)
            elif command.name == ClaudeControlCommandName.COST:
                result = self._cost(command)
            elif command.name == ClaudeControlCommandName.DOCTOR:
                result = self._doctor(command)
            elif command.name == ClaudeControlCommandName.DIFF:
                result = self._diff(command)
            elif command.name == ClaudeControlCommandName.MEMORY:
                result = self._memory(command)
            elif command.name == ClaudeControlCommandName.PERMISSIONS:
                result = self._permissions(command)
            elif command.name == ClaudeControlCommandName.RESUME:
                result = self._resume(command)
            elif command.name == ClaudeControlCommandName.SKILLS:
                result = self._skills(command)
            elif command.name == ClaudeControlCommandName.TOOLS:
                result = self._tools(command)
            else:
                result = ClaudeControlCommandResult(
                    command_id=command.command_id,
                    name=command.name,
                    status=ClaudeControlCommandStatus.UNSUPPORTED,
                    summary=f"Unsupported control command: {command.name}",
                )
        except Exception as error:  # noqa: BLE001 - command failures must be observable.
            result = ClaudeControlCommandResult(
                command_id=command.command_id,
                name=command.name,
                status=ClaudeControlCommandStatus.FAILED,
                summary=f"{command.name} failed",
                payload={"error": type(error).__name__, "message": str(error)},
            )
        if command.artifact_policy in {ClaudeControlArtifactPolicy.ARTIFACT, ClaudeControlArtifactPolicy.BOTH}:
            artifact = self._write_command_artifact(command, result, run_id=run_id, task_id=task_id, producer_node_id=producer_node_id)
            result = ClaudeControlCommandResult(
                command_id=result.command_id,
                name=result.name,
                status=result.status,
                summary=result.summary,
                payload=result.payload,
                artifact=artifact,
                created_at=result.created_at,
                metadata={**result.metadata, "artifact_id": artifact.artifact_id},
            )
        return result

    def _context(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        if self.state.context_window is None:
            return self._skipped(command, "context window is not attached")
        snapshot = self.state.context_window.snapshot(include_text=bool(command.arguments.get("include_text", False)))
        stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), Mapping) else {}
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK,
            summary=f"Context window has {stats.get('active_blocks', 0)} active block(s).",
            payload=snapshot,
            metadata={"active_chars": str(stats.get("active_chars", 0)), "total_blocks": str(stats.get("total_blocks", 0))},
        )

    def _compact(
        self,
        command: ClaudeControlCommand,
        *,
        run_id: str,
        task_id: str,
        producer_node_id: str | None,
    ) -> ClaudeControlCommandResult:
        if self.state.context_window is None:
            return self._skipped(command, "context window is not attached")
        decision = self.state.context_window.maybe_compact(
            artifact_store=self.state.artifact_store,
            run_id=run_id,
            task_id=task_id,
            producer_node_id=producer_node_id,
            force=bool(command.arguments.get("force", True)),
        )
        status = ClaudeControlCommandStatus.OK if decision.applied else ClaudeControlCommandStatus.SKIPPED
        summary = "Context compacted." if decision.applied else "Context compaction was not needed."
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=status,
            summary=summary,
            payload=decision.to_dict(),
            artifact=decision.artifact,
            metadata={
                "applied": str(decision.applied).lower(),
                "artifact_id": decision.artifact_id,
                "before_chars": str(decision.before_chars),
                "after_chars": str(decision.after_chars),
            },
        )

    def _cost(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        context_stats = self.state.context_window.stats().to_dict() if self.state.context_window else {}
        tool_stats = self.state.tool_runtime.stats().to_dict() if self.state.tool_runtime else {}
        active_chars = int(context_stats.get("active_chars") or 0)
        result_chars = int(tool_stats.get("total_result_chars") or 0)
        estimated_tokens = max(1, (active_chars + result_chars) // 4)
        payload = {
            "estimated_tokens": estimated_tokens,
            "context": context_stats,
            "tools": tool_stats,
            "pricing": {
                "unit": "tokens",
                "source": "local_estimate",
                "note": "No external model pricing is used by the runtime.",
            },
        }
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK,
            summary=f"Estimated local context cost is {estimated_tokens} token(s).",
            payload=payload,
            metadata={"estimated_tokens": str(estimated_tokens)},
        )

    def _doctor(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        checks = [
            self._doctor_check("project_root_exists", self.state.project_root.exists(), str(self.state.project_root)),
            self._doctor_check("workspace_root_exists", self.state.workspace_root.exists(), str(self.state.workspace_root)),
            self._doctor_check("artifact_root_exists", self.state.artifact_store.root.exists(), str(self.state.artifact_store.root)),
            self._doctor_check("context_window_attached", self.state.context_window is not None, "claude_context_window"),
            self._doctor_check("tool_runtime_attached", self.state.tool_runtime is not None, "claude_tool_use_runtime"),
            self._doctor_check("session_lifecycle_attached", self.state.session_lifecycle is not None, "claude_session_lifecycle"),
        ]
        ok = all(item["ok"] for item in checks)
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK if ok else ClaudeControlCommandStatus.FAILED,
            summary="Runtime doctor passed." if ok else "Runtime doctor found blocking checks.",
            payload={"checks": checks},
            metadata={"check_count": str(len(checks)), "failed_count": str(sum(1 for item in checks if not item["ok"]))},
        )

    def _diff(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        root = self.state.workspace_root
        max_files = _safe_int(command.arguments.get("max_files"), default=80)
        files = []
        for path in sorted(root.rglob("*")):
            if len(files) >= max_files:
                break
            if path.is_file():
                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    rel = str(path)
                stat = path.stat()
                files.append({"path": rel, "size_bytes": stat.st_size, "modified_at": stat.st_mtime})
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK,
            summary=f"Workspace contains {len(files)} sampled file(s).",
            payload={"files": files, "workspace_root": str(root), "max_files": max_files},
            metadata={"file_count": str(len(files))},
        )

    def _memory(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        checkpoint = None
        if self.state.checkpoint_reader is not None:
            checkpoint = self.state.checkpoint_reader(str(command.arguments.get("checkpoint_id") or "latest"))
        context_snapshot = self.state.context_window.snapshot(include_text=False) if self.state.context_window else {}
        payload = {
            "checkpoint": checkpoint,
            "context_stats": context_snapshot.get("stats", {}),
            "runtime_memory": {
                "source": self.state.runtime_source,
                "runtime_id": self.state.runtime_id,
                "control_metadata": self.state.metadata,
            },
        }
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK,
            summary="Runtime memory state collected.",
            payload=payload,
            metadata={"checkpoint_present": str(checkpoint is not None).lower()},
        )

    def _permissions(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        store = self.state.permission_store
        if store is None:
            return self._skipped(command, "permission store is not attached")
        action = str(command.arguments.get("action") or "list")
        if action == "add_rule":
            operation = _enum_or_default(PermissionOperation, command.arguments.get("operation"), PermissionOperation.SHELL)
            effect = _enum_or_default(PermissionEffect, command.arguments.get("effect"), PermissionEffect.ASK)
            rule = PermissionRule(
                operation=operation,
                pattern=str(command.arguments.get("pattern") or ""),
                effect=effect,
                reason=str(command.arguments.get("reason") or "created by claude control command"),
                metadata={"command_id": command.command_id, "runtime_source": self.state.runtime_source},
            )
            store.add_rule(rule)
        rules = store.list_rules()
        requests = store.list_requests()
        payload = {
            "rules": [to_jsonable(rule) for rule in rules],
            "requests": [to_jsonable(request) for request in requests],
            "action": action,
        }
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK,
            summary=f"Permission store has {len(rules)} rule(s) and {len(requests)} request(s).",
            payload=payload,
            metadata={"rule_count": str(len(rules)), "request_count": str(len(requests)), "action": action},
        )

    def _resume(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        lifecycle = self.state.session_lifecycle
        if lifecycle is None:
            return self._skipped(command, "session lifecycle is not attached")
        plans = lifecycle.resume_plans
        latest = plans[-1] if plans else None
        if latest is None:
            payload = {"resume_plan": None, "checkpoints": [checkpoint.to_dict() for checkpoint in lifecycle.checkpoints]}
            return ClaudeControlCommandResult(
                command_id=command.command_id,
                name=command.name,
                status=ClaudeControlCommandStatus.SKIPPED,
                summary="No resume plan is available yet.",
                payload=payload,
            )
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK if latest.ok else ClaudeControlCommandStatus.FAILED,
            summary=f"Resume plan status is {latest.status}.",
            payload=latest.to_dict(),
            metadata={"resume_token": latest.resume_token, "status": str(latest.status)},
        )

    def _skills(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        skill_roots = [self.state.project_root / "skills", self.state.workspace_root / "skills"]
        skills = []
        for root in skill_roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("*/SKILL.md")):
                skills.append({"name": path.parent.name, "path": str(path), "root": str(root)})
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK,
            summary=f"Discovered {len(skills)} skill descriptor(s).",
            payload={"skills": skills},
            metadata={"skill_count": str(len(skills))},
        )

    def _tools(self, command: ClaudeControlCommand) -> ClaudeControlCommandResult:
        if self.state.tool_runtime is None:
            return self._skipped(command, "tool runtime is not attached")
        snapshot = self.state.tool_runtime.snapshot()
        stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), Mapping) else {}
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.OK,
            summary=f"Tool runtime has planned {stats.get('planned_count', 0)} tool use(s).",
            payload=snapshot,
            metadata={"planned_count": str(stats.get("planned_count", 0)), "failed_count": str(stats.get("failed_count", 0))},
        )

    def _write_command_artifact(
        self,
        command: ClaudeControlCommand,
        result: ClaudeControlCommandResult,
        *,
        run_id: str,
        task_id: str,
        producer_node_id: str | None,
    ) -> ArtifactRef:
        return self.state.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            title=f"Claude control command {command.name}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=producer_node_id,
        )

    def _doctor_check(self, name: str, ok: bool, subject: str) -> dict[str, Any]:
        return {"name": name, "ok": ok, "subject": subject, "checked_at": now_iso()}

    def _skipped(self, command: ClaudeControlCommand, summary: str) -> ClaudeControlCommandResult:
        return ClaudeControlCommandResult(
            command_id=command.command_id,
            name=command.name,
            status=ClaudeControlCommandStatus.SKIPPED,
            summary=summary,
            payload={},
        )


def control_commands_from_constraints(constraints: Mapping[str, Any]) -> list[ClaudeControlCommand]:
    raw = constraints.get("control_commands") or constraints.get("commands")
    if raw is None:
        return []
    if isinstance(raw, (str, Mapping)):
        raw_items: list[Any] = [raw]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        return []
    return [command for command in (ClaudeControlCommand.from_raw(item) for item in raw_items) if command is not None]


def control_metadata(report: ClaudeControlRuntimeReport | None) -> dict[str, str]:
    if report is None:
        return {
            "control_command_count": "0",
            "control_command_failed": "0",
            "control_command_artifacts": "0",
        }
    return {
        "control_command_count": str(len(report.results)),
        "control_command_failed": str(sum(1 for result in report.results if not result.ok)),
        "control_command_artifacts": str(len(report.artifacts)),
        "control_command_ok": str(report.ok).lower(),
    }


def control_report_markdown(report: ClaudeControlRuntimeReport) -> str:
    lines = ["# Claude Control Commands", ""]
    lines.append(f"- ok: `{str(report.ok).lower()}`")
    lines.append(f"- command_count: `{len(report.results)}`")
    lines.append("")
    for result in report.results:
        lines.append(f"## {result.name}")
        lines.append("")
        lines.append(f"- status: `{result.status}`")
        lines.append(f"- summary: {result.summary}")
        if result.artifact is not None:
            lines.append(f"- artifact_id: `{result.artifact.artifact_id}`")
        lines.append("")
    return "\n".join(lines)


def _command_name(value: Any) -> ClaudeControlCommandName:
    normalized = str(value or "").strip().lower().lstrip("/")
    aliases = {
        "ctx": "context",
        "compact-now": "compact",
        "usage": "cost",
        "health": "doctor",
        "permission": "permissions",
        "perm": "permissions",
        "resume-session": "resume",
        "tool": "tools",
    }
    normalized = aliases.get(normalized, normalized)
    return _enum_or_default(ClaudeControlCommandName, normalized, ClaudeControlCommandName.DOCTOR)


def _enum_or_default(enum_type: type[Any], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
