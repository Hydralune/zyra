from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import new_id, now_iso, to_jsonable


class ClaudeQueryPlanSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ClaudeQueryTurnKind(StrEnum):
    STRUCTURED_TOOL_TURN = "structured_tool_turn"
    CONTROL_TURN = "control_turn"
    EMPTY_TURN = "empty_turn"


class ClaudeQueryStepState(StrEnum):
    READY = "ready"
    INVALID = "invalid"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ClaudeQueryPlanDiagnostic:
    severity: ClaudeQueryPlanSeverity
    code: str
    message: str
    turn_index: int = 0
    step_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ClaudeQueryPlanSeverity.ERROR

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeQueryToolStep:
    tool_name: str
    arguments: dict[str, Any]
    turn_index: int
    step_index: int
    step_id: str = field(default_factory=lambda: new_id("qstep"))
    state: ClaudeQueryStepState = ClaudeQueryStepState.READY
    prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.state == ClaudeQueryStepState.READY and bool(self.tool_name)

    def to_tool_call_dict(self) -> dict[str, Any]:
        payload = {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "step_id": self.step_id,
            "metadata": {
                "query_plan_step_id": self.step_id,
                "query_plan_turn_index": self.turn_index,
                "query_plan_step_index": self.step_index,
                **dict(self.metadata),
            },
        }
        if self.prompt:
            payload["prompt"] = self.prompt
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "arguments": to_jsonable(self.arguments),
            "turn_index": self.turn_index,
            "step_index": self.step_index,
            "state": str(self.state),
            "valid": self.valid,
            "prompt": self.prompt,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClaudeQueryTurn:
    turn_index: int
    steps: list[ClaudeQueryToolStep]
    kind: ClaudeQueryTurnKind = ClaudeQueryTurnKind.STRUCTURED_TOOL_TURN
    prompt: str = ""
    turn_id: str = field(default_factory=lambda: new_id("qturn"))
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def valid_steps(self) -> list[ClaudeQueryToolStep]:
        return [step for step in self.steps if step.valid]

    @property
    def valid(self) -> bool:
        return bool(self.valid_steps) or self.kind == ClaudeQueryTurnKind.CONTROL_TURN

    def to_tool_turn(self) -> list[dict[str, Any]]:
        return [step.to_tool_call_dict() for step in self.valid_steps]

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "kind": str(self.kind),
            "prompt": self.prompt,
            "valid": self.valid,
            "step_count": len(self.steps),
            "valid_step_count": len(self.valid_steps),
            "steps": [step.to_dict() for step in self.steps],
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClaudeQueryPlan:
    plan_id: str
    turns: list[ClaudeQueryTurn]
    diagnostics: list[ClaudeQueryPlanDiagnostic]
    source: str
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(item.blocking for item in self.diagnostics)

    @property
    def valid_turns(self) -> list[ClaudeQueryTurn]:
        return [turn for turn in self.turns if turn.valid]

    @property
    def tool_step_count(self) -> int:
        return sum(len(turn.valid_steps) for turn in self.turns)

    @property
    def invalid_step_count(self) -> int:
        return sum(len([step for step in turn.steps if not step.valid]) for turn in self.turns)

    def to_tool_turns(self) -> list[list[dict[str, Any]]]:
        return [turn.to_tool_turn() for turn in self.valid_turns if turn.to_tool_turn()]

    def metadata_strings(self) -> dict[str, str]:
        return {
            "query_plan_id": self.plan_id,
            "query_plan_source": self.source,
            "query_plan_ok": str(self.ok).lower(),
            "query_plan_turns": str(len(self.turns)),
            "query_plan_valid_turns": str(len(self.valid_turns)),
            "query_plan_tool_steps": str(self.tool_step_count),
            "query_plan_invalid_steps": str(self.invalid_step_count),
            "query_plan_diagnostics": str(len(self.diagnostics)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.claude.query_plan.v1",
            "plan_id": self.plan_id,
            "source": self.source,
            "created_at": self.created_at,
            "ok": self.ok,
            "turn_count": len(self.turns),
            "valid_turn_count": len(self.valid_turns),
            "tool_step_count": self.tool_step_count,
            "invalid_step_count": self.invalid_step_count,
            "turns": [turn.to_dict() for turn in self.turns],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClaudeQueryPlanAudit:
    plan_id: str
    ok: bool
    turn_count: int
    valid_turn_count: int
    tool_step_count: int
    invalid_step_count: int
    blocking_diagnostic_count: int
    warning_count: int
    read_only_step_count: int
    mutating_step_count: int
    shell_step_count: int
    unknown_tool_count: int
    tool_names: list[str]
    diagnostics: list[ClaudeQueryPlanDiagnostic] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def metadata_strings(self) -> dict[str, str]:
        return {
            "query_plan_id": self.plan_id,
            "query_plan_ok": str(self.ok).lower(),
            "query_plan_turns": str(self.turn_count),
            "query_plan_valid_turns": str(self.valid_turn_count),
            "query_plan_tool_steps": str(self.tool_step_count),
            "query_plan_invalid_steps": str(self.invalid_step_count),
            "query_plan_blocking_diagnostics": str(self.blocking_diagnostic_count),
            "query_plan_warnings": str(self.warning_count),
            "query_plan_read_only_steps": str(self.read_only_step_count),
            "query_plan_mutating_steps": str(self.mutating_step_count),
            "query_plan_shell_steps": str(self.shell_step_count),
            "query_plan_unknown_tools": str(self.unknown_tool_count),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.claude.query_plan_audit.v1",
            "plan_id": self.plan_id,
            "ok": self.ok,
            "turn_count": self.turn_count,
            "valid_turn_count": self.valid_turn_count,
            "tool_step_count": self.tool_step_count,
            "invalid_step_count": self.invalid_step_count,
            "blocking_diagnostic_count": self.blocking_diagnostic_count,
            "warning_count": self.warning_count,
            "read_only_step_count": self.read_only_step_count,
            "mutating_step_count": self.mutating_step_count,
            "shell_step_count": self.shell_step_count,
            "unknown_tool_count": self.unknown_tool_count,
            "tool_names": list(self.tool_names),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "metadata": to_jsonable(self.metadata),
        }


class ClaudeQueryPlanner:
    def __init__(self, *, source: str = "worker_constraints") -> None:
        self.source = source

    def from_constraints(self, constraints: Mapping[str, Any]) -> ClaudeQueryPlan:
        diagnostics: list[ClaudeQueryPlanDiagnostic] = []
        turns: list[ClaudeQueryTurn] = []
        raw_query_turns = constraints.get("query_turns")
        if isinstance(raw_query_turns, list):
            for turn_index, raw_turn in enumerate(raw_query_turns, start=1):
                turn = self._turn_from_raw(raw_turn, turn_index=turn_index, diagnostics=diagnostics)
                turns.append(turn)
        else:
            raw_tool_plan = constraints.get("tool_plan")
            if isinstance(raw_tool_plan, list):
                turns.append(self._turn_from_steps(raw_tool_plan, turn_index=1, diagnostics=diagnostics))
            elif raw_tool_plan is not None:
                diagnostics.append(
                    ClaudeQueryPlanDiagnostic(
                        severity=ClaudeQueryPlanSeverity.ERROR,
                        code="INVALID_TOOL_PLAN",
                        message="tool_plan must be a list of tool steps",
                    )
                )
        if not turns:
            diagnostics.append(
                ClaudeQueryPlanDiagnostic(
                    severity=ClaudeQueryPlanSeverity.WARNING,
                    code="EMPTY_QUERY_PLAN",
                    message="request has no query_turns or tool_plan",
                )
            )
        return ClaudeQueryPlan(
            plan_id=new_id("qplan"),
            turns=turns,
            diagnostics=diagnostics,
            source=self.source,
            metadata={
                "constraint_keys": sorted(str(key) for key in constraints.keys()),
                "max_turns": constraints.get("max_turns"),
                "continue_on_error": constraints.get("continue_on_error"),
            },
        )

    def _turn_from_raw(
        self,
        raw_turn: Any,
        *,
        turn_index: int,
        diagnostics: list[ClaudeQueryPlanDiagnostic],
    ) -> ClaudeQueryTurn:
        if isinstance(raw_turn, Mapping):
            raw_steps = raw_turn.get("tool_calls") or raw_turn.get("steps") or raw_turn.get("tool_plan") or []
            prompt = str(raw_turn.get("prompt") or raw_turn.get("user_message") or "")
            metadata = {str(k): v for k, v in raw_turn.items() if k not in {"tool_calls", "steps", "tool_plan", "prompt", "user_message"}}
        else:
            raw_steps = raw_turn
            prompt = ""
            metadata = {}
        if not isinstance(raw_steps, list):
            diagnostics.append(
                ClaudeQueryPlanDiagnostic(
                    severity=ClaudeQueryPlanSeverity.ERROR,
                    code="INVALID_QUERY_TURN",
                    message="query turn steps must be a list",
                    turn_index=turn_index,
                )
            )
            raw_steps = []
        turn = self._turn_from_steps(raw_steps, turn_index=turn_index, diagnostics=diagnostics)
        return ClaudeQueryTurn(
            turn_index=turn.turn_index,
            steps=turn.steps,
            kind=turn.kind,
            prompt=prompt,
            metadata={**turn.metadata, **metadata},
        )

    def _turn_from_steps(
        self,
        raw_steps: Sequence[Any],
        *,
        turn_index: int,
        diagnostics: list[ClaudeQueryPlanDiagnostic],
    ) -> ClaudeQueryTurn:
        steps: list[ClaudeQueryToolStep] = []
        for step_index, raw_step in enumerate(raw_steps, start=1):
            step = self._step_from_raw(raw_step, turn_index=turn_index, step_index=step_index, diagnostics=diagnostics)
            steps.append(step)
        kind = ClaudeQueryTurnKind.STRUCTURED_TOOL_TURN if steps else ClaudeQueryTurnKind.EMPTY_TURN
        return ClaudeQueryTurn(
            turn_index=turn_index,
            steps=steps,
            kind=kind,
            metadata={"raw_step_count": len(raw_steps)},
        )

    def _step_from_raw(
        self,
        raw_step: Any,
        *,
        turn_index: int,
        step_index: int,
        diagnostics: list[ClaudeQueryPlanDiagnostic],
    ) -> ClaudeQueryToolStep:
        if not isinstance(raw_step, Mapping):
            diagnostics.append(
                ClaudeQueryPlanDiagnostic(
                    severity=ClaudeQueryPlanSeverity.ERROR,
                    code="INVALID_TOOL_STEP",
                    message="tool step must be an object",
                    turn_index=turn_index,
                    step_index=step_index,
                )
            )
            return ClaudeQueryToolStep(
                tool_name="",
                arguments={},
                turn_index=turn_index,
                step_index=step_index,
                state=ClaudeQueryStepState.INVALID,
                metadata={"raw_type": type(raw_step).__name__},
            )
        tool_name = str(raw_step.get("tool_name") or raw_step.get("tool") or raw_step.get("name") or "")
        arguments = raw_step.get("arguments") if isinstance(raw_step.get("arguments"), Mapping) else raw_step.get("args")
        if not isinstance(arguments, Mapping):
            arguments = {}
        prompt = str(raw_step.get("prompt") or raw_step.get("user_message") or "")
        metadata = raw_step.get("metadata") if isinstance(raw_step.get("metadata"), Mapping) else {}
        state = ClaudeQueryStepState.READY
        if not tool_name:
            state = ClaudeQueryStepState.INVALID
            diagnostics.append(
                ClaudeQueryPlanDiagnostic(
                    severity=ClaudeQueryPlanSeverity.ERROR,
                    code="MISSING_TOOL_NAME",
                    message="tool step is missing tool_name",
                    turn_index=turn_index,
                    step_index=step_index,
                )
            )
        return ClaudeQueryToolStep(
            tool_name=tool_name,
            arguments=dict(arguments),
            turn_index=turn_index,
            step_index=step_index,
            state=state,
            prompt=prompt,
            metadata={
                "source": self.source,
                **dict(metadata),
            },
        )


def plan_from_constraints(constraints: Mapping[str, Any], *, source: str = "worker_constraints") -> ClaudeQueryPlan:
    return ClaudeQueryPlanner(source=source).from_constraints(constraints)


def query_turns_from_constraints(constraints: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
    return plan_from_constraints(constraints).to_tool_turns()


def audit_query_plan(plan: ClaudeQueryPlan, *, known_tools: set[str] | None = None) -> ClaudeQueryPlanAudit:
    known_tools = known_tools or {
        "file_read",
        "file_write",
        "file_edit",
        "shell",
        "browser",
        "web_search",
        "artifact_write",
        "checkpoint",
        "trace",
    }
    steps = [step for turn in plan.turns for step in turn.steps]
    valid_steps = [step for step in steps if step.valid]
    read_only_tools = {"file_read", "browser", "web_search", "checkpoint", "trace"}
    mutating_tools = {"file_write", "file_edit", "shell", "artifact_write"}
    tool_names = [step.tool_name for step in valid_steps if step.tool_name]
    unknown = [name for name in tool_names if name not in known_tools]
    diagnostics = list(plan.diagnostics)
    for step in valid_steps:
        if step.tool_name not in known_tools:
            diagnostics.append(
                ClaudeQueryPlanDiagnostic(
                    severity=ClaudeQueryPlanSeverity.ERROR,
                    code="UNKNOWN_TOOL_IN_PLAN",
                    message=f"tool {step.tool_name} is not registered in the productized planner inventory",
                    turn_index=step.turn_index,
                    step_index=step.step_index,
                    metadata={"tool_name": step.tool_name},
                )
            )
    return ClaudeQueryPlanAudit(
        plan_id=plan.plan_id,
        ok=plan.ok and not unknown,
        turn_count=len(plan.turns),
        valid_turn_count=len(plan.valid_turns),
        tool_step_count=len(valid_steps),
        invalid_step_count=len([step for step in steps if not step.valid]),
        blocking_diagnostic_count=sum(1 for diagnostic in diagnostics if diagnostic.blocking),
        warning_count=sum(1 for diagnostic in diagnostics if diagnostic.severity == ClaudeQueryPlanSeverity.WARNING),
        read_only_step_count=sum(1 for step in valid_steps if step.tool_name in read_only_tools),
        mutating_step_count=sum(1 for step in valid_steps if step.tool_name in mutating_tools),
        shell_step_count=sum(1 for step in valid_steps if step.tool_name == "shell"),
        unknown_tool_count=len(unknown),
        tool_names=sorted(set(tool_names)),
        diagnostics=diagnostics,
        metadata={"source": plan.source, "created_at": plan.created_at},
    )


def query_plan_metadata_from_constraints(constraints: Mapping[str, Any]) -> dict[str, str]:
    plan = plan_from_constraints(constraints)
    audit = audit_query_plan(plan)
    return {**plan.metadata_strings(), **audit.metadata_strings()}


def query_plan_summary_lines(plan: ClaudeQueryPlan) -> list[str]:
    audit = audit_query_plan(plan)
    lines = [
        f"- plan_id: `{plan.plan_id}`",
        f"- ok: `{str(audit.ok).lower()}`",
        f"- turns: `{audit.valid_turn_count}/{audit.turn_count}`",
        f"- tool_steps: `{audit.tool_step_count}`",
        f"- mutating_steps: `{audit.mutating_step_count}`",
        f"- read_only_steps: `{audit.read_only_step_count}`",
    ]
    for diagnostic in audit.diagnostics:
        lines.append(f"- {diagnostic.severity} {diagnostic.code}: {diagnostic.message}")
    return lines


def merge_query_plans(plans: Sequence[ClaudeQueryPlan], *, source: str = "merged_query_plan") -> ClaudeQueryPlan:
    turns: list[ClaudeQueryTurn] = []
    diagnostics: list[ClaudeQueryPlanDiagnostic] = []
    turn_offset = 0
    for plan in plans:
        diagnostics.extend(plan.diagnostics)
        for turn in plan.turns:
            turn_offset += 1
            remapped_steps = [
                ClaudeQueryToolStep(
                    tool_name=step.tool_name,
                    arguments=dict(step.arguments),
                    turn_index=turn_offset,
                    step_index=step.step_index,
                    step_id=step.step_id,
                    state=step.state,
                    prompt=step.prompt,
                    metadata={**step.metadata, "merged_from_plan": plan.plan_id},
                )
                for step in turn.steps
            ]
            turns.append(
                ClaudeQueryTurn(
                    turn_index=turn_offset,
                    steps=remapped_steps,
                    kind=turn.kind,
                    prompt=turn.prompt,
                    metadata={**turn.metadata, "merged_from_plan": plan.plan_id},
                )
            )
    return ClaudeQueryPlan(
        plan_id=new_id("qplan"),
        turns=turns,
        diagnostics=diagnostics,
        source=source,
        metadata={"merged_plan_count": len(plans)},
    )


def query_plan_tool_matrix(plan: ClaudeQueryPlan) -> list[dict[str, Any]]:
    matrix: list[dict[str, Any]] = []
    read_only_tools = {"file_read", "browser", "web_search", "checkpoint", "trace"}
    mutating_tools = {"file_write", "file_edit", "shell", "artifact_write"}
    for turn in plan.turns:
        for step in turn.steps:
            matrix.append(
                {
                    "plan_id": plan.plan_id,
                    "turn_id": turn.turn_id,
                    "turn_index": turn.turn_index,
                    "step_id": step.step_id,
                    "step_index": step.step_index,
                    "tool_name": step.tool_name,
                    "valid": step.valid,
                    "read_only": step.tool_name in read_only_tools,
                    "mutating": step.tool_name in mutating_tools,
                    "argument_keys": sorted(str(key) for key in step.arguments.keys()),
                    "prompt_present": bool(step.prompt),
                    "state": str(step.state),
                }
            )
    return matrix


def render_query_plan_markdown(plan: ClaudeQueryPlan) -> str:
    audit = audit_query_plan(plan)
    lines = [
        "# Claude Query Plan",
        "",
        f"- plan_id: `{plan.plan_id}`",
        f"- source: `{plan.source}`",
        f"- ok: `{str(audit.ok).lower()}`",
        f"- turns: `{audit.valid_turn_count}/{audit.turn_count}`",
        f"- tool_steps: `{audit.tool_step_count}`",
        f"- mutating_steps: `{audit.mutating_step_count}`",
        f"- read_only_steps: `{audit.read_only_step_count}`",
        "",
        "## Tool Matrix",
        "",
    ]
    for row in query_plan_tool_matrix(plan):
        flags = []
        if row["read_only"]:
            flags.append("read_only")
        if row["mutating"]:
            flags.append("mutating")
        if not row["valid"]:
            flags.append("invalid")
        flag_text = ",".join(flags) if flags else "neutral"
        lines.append(
            f"- turn {row['turn_index']} step {row['step_index']} `{row['tool_name'] or 'missing_tool'}` "
            f"[{flag_text}] args={','.join(row['argument_keys']) or '-'}"
        )
    if audit.diagnostics:
        lines.extend(["", "## Diagnostics", ""])
        for diagnostic in audit.diagnostics:
            lines.append(f"- `{diagnostic.severity}` `{diagnostic.code}`: {diagnostic.message}")
    return "\n".join(lines)


def query_plan_review_payload(plan: ClaudeQueryPlan) -> dict[str, Any]:
    audit = audit_query_plan(plan)
    matrix = query_plan_tool_matrix(plan)
    mutating_steps = [row for row in matrix if row["mutating"]]
    read_only_steps = [row for row in matrix if row["read_only"]]
    return {
        "schema": "zyra.claude.query_plan_review.v1",
        "plan": plan.to_dict(),
        "audit": audit.to_dict(),
        "tool_matrix": matrix,
        "summary": {
            "ok": audit.ok,
            "turn_count": audit.turn_count,
            "tool_step_count": audit.tool_step_count,
            "mutating_step_count": len(mutating_steps),
            "read_only_step_count": len(read_only_steps),
            "diagnostic_count": len(audit.diagnostics),
        },
        "review_flags": {
            "has_shell": audit.shell_step_count > 0,
            "has_unknown_tool": audit.unknown_tool_count > 0,
            "has_blocking_diagnostics": audit.blocking_diagnostic_count > 0,
            "mixes_read_and_write": bool(mutating_steps and read_only_steps),
        },
    }
