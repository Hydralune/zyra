from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .claude_context_assembly_foundation import ContextAssemblySnapshot
from .claude_input_processor import QueryInputKind, QueryInputRecord
from .claude_session_replay_runtime import SessionReplayPlan


class TurnLifecycleProjectionStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    EMPTY = "empty"


class TurnLifecycleProjectionSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class TurnLifecycleProjectionSurface(StrEnum):
    INPUT_BINDING = "input_binding"
    TOOL_PLAN_BINDING = "tool_plan_binding"
    CONTEXT_BINDING = "context_binding"
    REPLAY_BINDING = "replay_binding"
    TURN_SEQUENCE = "turn_sequence"
    QUERY_ENGINE_HANDOFF = "query_engine_handoff"


class TurnSeedKind(StrEnum):
    USER_TEXT = "user_text"
    SLASH_COMMAND = "slash_command"
    BASH_COMMAND = "bash_command"
    STRUCTURED_TOOL_PLAN = "structured_tool_plan"
    REPLAYED_CONTEXT = "replayed_context"
    SYNTHETIC_EMPTY = "synthetic_empty"


class TurnSeedRoute(StrEnum):
    QUERY_ENGINE = "query_engine"
    CONTROL_COMMAND = "control_command"
    SHELL_TOOL = "shell_tool"
    STRUCTURED_TOOL_LOOP = "structured_tool_loop"
    REPLAY_RESTORE = "replay_restore"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TurnLifecycleFinding:
    code: str
    severity: TurnLifecycleProjectionSeverity
    surface: TurnLifecycleProjectionSurface
    message: str
    turn_index: int = 0
    input_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == TurnLifecycleProjectionSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "turn_index": self.turn_index,
            "input_id": self.input_id,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TurnToolPlanBinding:
    turn_index: int
    step_count: int
    tool_names: tuple[str, ...]
    read_only_count: int = 0
    mutating_count: int = 0
    invalid_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return self.step_count <= 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "step_count": self.step_count,
            "tool_names": list(self.tool_names),
            "read_only_count": self.read_only_count,
            "mutating_count": self.mutating_count,
            "invalid_count": self.invalid_count,
            "empty": self.empty,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TurnInputBinding:
    input_id: str
    kind: str
    route: TurnSeedRoute
    text: str
    command_name: str = ""
    accepted: bool = True
    risk: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_id": self.input_id,
            "kind": self.kind,
            "route": str(self.route),
            "text": self.text,
            "command_name": self.command_name,
            "accepted": self.accepted,
            "risk": self.risk,
            "chars": self.chars,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TurnContextBinding:
    snapshot_id: str
    fingerprint: str
    status: str
    selected_block_count: int
    active_chars: int
    has_system_prompt: bool
    has_tool_inventory: bool
    has_user_input: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.snapshot_id) and self.status != "blocked" and self.has_system_prompt and self.has_user_input

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "selected_block_count": self.selected_block_count,
            "active_chars": self.active_chars,
            "has_system_prompt": self.has_system_prompt,
            "has_tool_inventory": self.has_tool_inventory,
            "has_user_input": self.has_user_input,
            "ok": self.ok,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TurnReplayBinding:
    replay_plan_id: str
    status: str
    action_count: int
    message_count: int
    context_ready: bool
    attach_ready: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.replay_plan_id or self.status in {"ready", "degraded"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "replay_plan_id": self.replay_plan_id,
            "status": self.status,
            "action_count": self.action_count,
            "message_count": self.message_count,
            "context_ready": self.context_ready,
            "attach_ready": self.attach_ready,
            "ok": self.ok,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TurnLifecycleSeed:
    seed_id: str
    turn_index: int
    kind: TurnSeedKind
    route: TurnSeedRoute
    input_binding: TurnInputBinding | None
    tool_plan_binding: TurnToolPlanBinding | None
    context_binding: TurnContextBinding | None
    replay_binding: TurnReplayBinding | None
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.route == TurnSeedRoute.BLOCKED

    @property
    def prompt_text(self) -> str:
        if self.input_binding is not None:
            return self.input_binding.text
        if self.replay_binding is not None:
            return f"Replay session from {self.replay_binding.replay_plan_id}"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_id": self.seed_id,
            "turn_index": self.turn_index,
            "kind": str(self.kind),
            "route": str(self.route),
            "input_binding": self.input_binding.to_dict() if self.input_binding else None,
            "tool_plan_binding": self.tool_plan_binding.to_dict() if self.tool_plan_binding else None,
            "context_binding": self.context_binding.to_dict() if self.context_binding else None,
            "replay_binding": self.replay_binding.to_dict() if self.replay_binding else None,
            "blocked": self.blocked,
            "prompt_text": self.prompt_text,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TurnLifecycleProjection:
    projection_id: str
    status: TurnLifecycleProjectionStatus
    session_id: str
    worker_request_id: str
    seeds: tuple[TurnLifecycleSeed, ...]
    findings: tuple[TurnLifecycleFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {TurnLifecycleProjectionStatus.READY, TurnLifecycleProjectionStatus.DEGRADED} and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == TurnLifecycleProjectionSeverity.WARNING)

    @property
    def route_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for seed in self.seeds:
            counts[str(seed.route)] = counts.get(str(seed.route), 0) + 1
        return counts

    def metadata_values(self) -> dict[str, str]:
        return {
            "turn_lifecycle_projection_id": self.projection_id,
            "turn_lifecycle_ok": str(self.ok).lower(),
            "turn_lifecycle_status": str(self.status),
            "turn_lifecycle_seed_count": str(len(self.seeds)),
            "turn_lifecycle_blockers": str(self.blocker_count),
            "turn_lifecycle_warnings": str(self.warning_count),
            "turn_lifecycle_query_engine_routes": str(self.route_counts.get(str(TurnSeedRoute.QUERY_ENGINE), 0)),
            "turn_lifecycle_tool_loop_routes": str(self.route_counts.get(str(TurnSeedRoute.STRUCTURED_TOOL_LOOP), 0)),
            "turn_lifecycle_control_routes": str(self.route_counts.get(str(TurnSeedRoute.CONTROL_COMMAND), 0)),
            "turn_lifecycle_shell_routes": str(self.route_counts.get(str(TurnSeedRoute.SHELL_TOOL), 0)),
            "turn_lifecycle_replay_routes": str(self.route_counts.get(str(TurnSeedRoute.REPLAY_RESTORE), 0)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "seeds": [seed.to_dict() for seed in self.seeds],
            "findings": [finding.to_dict() for finding in self.findings],
            "route_counts": self.route_counts,
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class TurnLifecycleRuntime:
    """Builds turn seeds that bind input, context, replay and tool plans."""

    def project(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        input_records: Sequence[QueryInputRecord],
        query_turns: Sequence[Sequence[Mapping[str, Any]]],
        context_snapshot: ContextAssemblySnapshot | None,
        replay_plan: SessionReplayPlan | None = None,
    ) -> TurnLifecycleProjection:
        findings: list[TurnLifecycleFinding] = []
        context_binding = context_binding_from_snapshot(context_snapshot)
        replay_binding = replay_binding_from_plan(replay_plan)
        tool_bindings = tool_plan_bindings(query_turns)
        seeds = build_turn_seeds(
            input_records=input_records,
            tool_bindings=tool_bindings,
            context_binding=context_binding,
            replay_binding=replay_binding,
        )
        findings.extend(validate_turn_seed_inputs(seeds))
        findings.extend(validate_turn_seed_tool_bindings(seeds, tool_bindings=tool_bindings))
        findings.extend(validate_context_binding(context_binding))
        findings.extend(validate_replay_binding(replay_binding))
        status = projection_status(seeds=seeds, findings=findings)
        return TurnLifecycleProjection(
            projection_id=new_id("turnlife"),
            status=status,
            session_id=session_id,
            worker_request_id=worker_request_id,
            seeds=tuple(seeds),
            findings=tuple(findings),
            metadata={
                "source_path": "src/QueryEngine.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_projection(
        self,
        projection: TurnLifecycleProjection,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": projection.session_id,
                    "worker_request_id": projection.worker_request_id,
                    "phase": "turn_lifecycle_projection",
                    "projection_id": projection.projection_id,
                    "ok": projection.ok,
                    "status": str(projection.status),
                    "seed_count": len(projection.seeds),
                    "route_counts": projection.route_counts,
                    "blocker_count": projection.blocker_count,
                    "warning_count": projection.warning_count,
                }
            },
        )


def build_turn_seeds(
    *,
    input_records: Sequence[QueryInputRecord],
    tool_bindings: Sequence[TurnToolPlanBinding],
    context_binding: TurnContextBinding | None,
    replay_binding: TurnReplayBinding | None,
) -> list[TurnLifecycleSeed]:
    seeds: list[TurnLifecycleSeed] = []
    accepted_inputs = [record for record in input_records if record.accepted]
    if not accepted_inputs and replay_binding is None:
        seeds.append(
            TurnLifecycleSeed(
                seed_id=new_id("turnseed"),
                turn_index=1,
                kind=TurnSeedKind.SYNTHETIC_EMPTY,
                route=TurnSeedRoute.BLOCKED,
                input_binding=None,
                tool_plan_binding=tool_bindings[0] if tool_bindings else None,
                context_binding=context_binding,
                replay_binding=replay_binding,
                metadata={"reason": "no_accepted_input"},
            )
        )
        return seeds
    if replay_binding is not None and replay_binding.replay_plan_id:
        seeds.append(
            TurnLifecycleSeed(
                seed_id=new_id("turnseed"),
                turn_index=1,
                kind=TurnSeedKind.REPLAYED_CONTEXT,
                route=TurnSeedRoute.REPLAY_RESTORE if replay_binding.ok else TurnSeedRoute.BLOCKED,
                input_binding=None,
                tool_plan_binding=tool_bindings[0] if tool_bindings else None,
                context_binding=context_binding,
                replay_binding=replay_binding,
                metadata={"source": "session_replay"},
            )
        )
    for index, record in enumerate(accepted_inputs, start=1):
        tool_binding = tool_bindings[min(index - 1, len(tool_bindings) - 1)] if tool_bindings else None
        input_binding = input_binding_from_record(record)
        route = route_for_input(record, tool_binding)
        seeds.append(
            TurnLifecycleSeed(
                seed_id=new_id("turnseed"),
                turn_index=index,
                kind=kind_for_input(record),
                route=route,
                input_binding=input_binding,
                tool_plan_binding=tool_binding,
                context_binding=context_binding,
                replay_binding=replay_binding,
                metadata={
                    "input_sequence": record.sequence,
                    "tool_binding_present": tool_binding is not None,
                },
            )
        )
    return seeds


def input_binding_from_record(record: QueryInputRecord) -> TurnInputBinding:
    return TurnInputBinding(
        input_id=record.input_id,
        kind=str(record.kind),
        route=route_for_input(record, None),
        text=record.normalized_text,
        command_name=record.command_name,
        accepted=record.accepted,
        risk=str(record.risk),
        metadata={
            "sequence": record.sequence,
            "disposition": str(record.disposition),
            "source_path": record.source.source_path,
            "target_path": record.source.target_path,
        },
    )


def context_binding_from_snapshot(snapshot: ContextAssemblySnapshot | None) -> TurnContextBinding | None:
    if snapshot is None:
        return None
    kinds = {str(block.kind) for block in snapshot.selected_blocks}
    return TurnContextBinding(
        snapshot_id=snapshot.snapshot_id,
        fingerprint=snapshot.fingerprint,
        status=str(snapshot.status),
        selected_block_count=len(snapshot.selected_blocks),
        active_chars=snapshot.active_chars,
        has_system_prompt="system_prompt" in kinds,
        has_tool_inventory="tool_inventory" in kinds,
        has_user_input="user_input" in kinds,
        metadata={
            "block_kinds": sorted(kinds),
            "source_path": snapshot.source.source_path,
            "target_path": snapshot.source.target_path,
        },
    )


def replay_binding_from_plan(plan: SessionReplayPlan | None) -> TurnReplayBinding | None:
    if plan is None:
        return None
    return TurnReplayBinding(
        replay_plan_id=plan.plan_id,
        status=str(plan.status),
        action_count=len(plan.actions),
        message_count=len(plan.replay_messages),
        context_ready=plan.context_state.ok if plan.context_state else False,
        attach_ready=plan.attach_state.ok if plan.attach_state else False,
        metadata={"blocker_count": plan.blocker_count, "warning_count": plan.warning_count},
    )


def tool_plan_bindings(query_turns: Sequence[Sequence[Mapping[str, Any]]]) -> list[TurnToolPlanBinding]:
    bindings: list[TurnToolPlanBinding] = []
    for index, turn in enumerate(query_turns, start=1):
        tool_names: list[str] = []
        read_only = 0
        mutating = 0
        invalid = 0
        for step in turn:
            if not isinstance(step, Mapping):
                invalid += 1
                continue
            tool_name = str(step.get("tool_name") or step.get("tool") or "")
            if not tool_name:
                invalid += 1
                tool_name = "unknown_tool"
            tool_names.append(tool_name)
            mode = str(step.get("access_mode") or step.get("mode") or "")
            if step.get("read_only") is True or mode == "read":
                read_only += 1
            else:
                mutating += 1
        bindings.append(
            TurnToolPlanBinding(
                turn_index=index,
                step_count=len(turn),
                tool_names=tuple(tool_names),
                read_only_count=read_only,
                mutating_count=mutating,
                invalid_count=invalid,
                metadata={"source": "query_turns"},
            )
        )
    return bindings


def kind_for_input(record: QueryInputRecord) -> TurnSeedKind:
    if record.kind == QueryInputKind.SLASH_COMMAND:
        return TurnSeedKind.SLASH_COMMAND
    if record.kind == QueryInputKind.BASH:
        return TurnSeedKind.BASH_COMMAND
    if record.kind == QueryInputKind.STRUCTURED_TURN:
        return TurnSeedKind.STRUCTURED_TOOL_PLAN
    return TurnSeedKind.USER_TEXT


def route_for_input(record: QueryInputRecord, tool_binding: TurnToolPlanBinding | None) -> TurnSeedRoute:
    disposition = str(record.disposition)
    if not record.accepted:
        return TurnSeedRoute.BLOCKED
    if disposition == "route_control_command":
        return TurnSeedRoute.CONTROL_COMMAND
    if disposition == "route_shell_tool":
        return TurnSeedRoute.SHELL_TOOL
    if disposition == "route_structured_plan" or tool_binding is not None:
        return TurnSeedRoute.STRUCTURED_TOOL_LOOP
    return TurnSeedRoute.QUERY_ENGINE


def validate_turn_seed_inputs(seeds: Sequence[TurnLifecycleSeed]) -> list[TurnLifecycleFinding]:
    findings: list[TurnLifecycleFinding] = []
    if not seeds:
        findings.append(
            TurnLifecycleFinding(
                code="turn_seed_empty",
                severity=TurnLifecycleProjectionSeverity.BLOCKER,
                surface=TurnLifecycleProjectionSurface.INPUT_BINDING,
                message="No turn lifecycle seeds were produced.",
            )
        )
    for seed in seeds:
        if seed.blocked:
            findings.append(
                TurnLifecycleFinding(
                    code="turn_seed_blocked",
                    severity=TurnLifecycleProjectionSeverity.BLOCKER,
                    surface=TurnLifecycleProjectionSurface.INPUT_BINDING,
                    message="Turn seed is blocked before QueryEngine handoff.",
                    turn_index=seed.turn_index,
                    input_id=seed.input_binding.input_id if seed.input_binding else "",
                    metadata=seed.to_dict(),
                )
            )
        if seed.input_binding is not None and not seed.input_binding.text:
            findings.append(
                TurnLifecycleFinding(
                    code="turn_input_empty",
                    severity=TurnLifecycleProjectionSeverity.WARNING,
                    surface=TurnLifecycleProjectionSurface.INPUT_BINDING,
                    message="Turn input binding has no text.",
                    turn_index=seed.turn_index,
                    input_id=seed.input_binding.input_id,
                )
            )
    return findings


def validate_turn_seed_tool_bindings(
    seeds: Sequence[TurnLifecycleSeed],
    *,
    tool_bindings: Sequence[TurnToolPlanBinding],
) -> list[TurnLifecycleFinding]:
    findings: list[TurnLifecycleFinding] = []
    if tool_bindings and not any(seed.tool_plan_binding for seed in seeds):
        findings.append(
            TurnLifecycleFinding(
                code="tool_plan_not_bound",
                severity=TurnLifecycleProjectionSeverity.BLOCKER,
                surface=TurnLifecycleProjectionSurface.TOOL_PLAN_BINDING,
                message="Tool plan exists but was not bound to any turn seed.",
            )
        )
    for seed in seeds:
        binding = seed.tool_plan_binding
        if binding is None:
            continue
        if binding.invalid_count:
            findings.append(
                TurnLifecycleFinding(
                    code="tool_plan_invalid_steps",
                    severity=TurnLifecycleProjectionSeverity.WARNING,
                    surface=TurnLifecycleProjectionSurface.TOOL_PLAN_BINDING,
                    message="Tool plan binding contains invalid steps.",
                    turn_index=seed.turn_index,
                    metadata=binding.to_dict(),
                )
            )
        if seed.route == TurnSeedRoute.STRUCTURED_TOOL_LOOP and binding.empty:
            findings.append(
                TurnLifecycleFinding(
                    code="structured_route_without_tools",
                    severity=TurnLifecycleProjectionSeverity.BLOCKER,
                    surface=TurnLifecycleProjectionSurface.TOOL_PLAN_BINDING,
                    message="Structured tool-loop route has an empty tool plan.",
                    turn_index=seed.turn_index,
                )
            )
    return findings


def validate_context_binding(context_binding: TurnContextBinding | None) -> list[TurnLifecycleFinding]:
    if context_binding is None:
        return [
            TurnLifecycleFinding(
                code="context_binding_missing",
                severity=TurnLifecycleProjectionSeverity.BLOCKER,
                surface=TurnLifecycleProjectionSurface.CONTEXT_BINDING,
                message="Turn lifecycle projection requires a context snapshot binding.",
            )
        ]
    if context_binding.ok:
        return []
    return [
        TurnLifecycleFinding(
            code="context_binding_incomplete",
            severity=TurnLifecycleProjectionSeverity.BLOCKER,
            surface=TurnLifecycleProjectionSurface.CONTEXT_BINDING,
            message="Context binding is missing required system/user blocks or is blocked.",
            metadata=context_binding.to_dict(),
        )
    ]


def validate_replay_binding(replay_binding: TurnReplayBinding | None) -> list[TurnLifecycleFinding]:
    if replay_binding is None:
        return []
    if replay_binding.ok:
        return []
    return [
        TurnLifecycleFinding(
            code="replay_binding_blocked",
            severity=TurnLifecycleProjectionSeverity.BLOCKER,
            surface=TurnLifecycleProjectionSurface.REPLAY_BINDING,
            message="Replay binding is present but blocked.",
            metadata=replay_binding.to_dict(),
        )
    ]


def projection_status(
    *,
    seeds: Sequence[TurnLifecycleSeed],
    findings: Sequence[TurnLifecycleFinding],
) -> TurnLifecycleProjectionStatus:
    if not seeds:
        return TurnLifecycleProjectionStatus.EMPTY
    if any(finding.blocking for finding in findings):
        return TurnLifecycleProjectionStatus.BLOCKED
    if any(finding.severity == TurnLifecycleProjectionSeverity.WARNING for finding in findings):
        return TurnLifecycleProjectionStatus.DEGRADED
    return TurnLifecycleProjectionStatus.READY


def turn_lifecycle_metadata(projection: TurnLifecycleProjection | None) -> dict[str, str]:
    if projection is None:
        return {
            "turn_lifecycle_projection_id": "",
            "turn_lifecycle_ok": "",
            "turn_lifecycle_status": "",
        }
    return projection.metadata_values()


def render_turn_lifecycle_markdown(projection: TurnLifecycleProjection) -> str:
    lines = [
        "# Turn Lifecycle Projection",
        "",
        f"- ok: `{str(projection.ok).lower()}`",
        f"- status: `{projection.status}`",
        f"- projection_id: `{projection.projection_id}`",
        f"- session_id: `{projection.session_id}`",
        f"- worker_request_id: `{projection.worker_request_id}`",
        f"- seed_count: `{len(projection.seeds)}`",
        f"- blockers: `{projection.blocker_count}`",
        f"- warnings: `{projection.warning_count}`",
        "",
        "## Routes",
        "",
    ]
    for route, count in sorted(projection.route_counts.items()):
        lines.append(f"- `{route}`: `{count}`")
    lines.extend(["", "## Seeds", ""])
    for seed in projection.seeds:
        lines.append(f"- turn `{seed.turn_index}` `{seed.kind}` `{seed.route}` input=`{seed.input_binding.input_id if seed.input_binding else ''}`")
    lines.extend(["", "## Findings", ""])
    if projection.findings:
        for finding in projection.findings:
            lines.append(f"- `{finding.severity}` `{finding.surface}` `{finding.code}` {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def projection_from_payload(payload: Mapping[str, Any]) -> TurnLifecycleProjection:
    findings = tuple(
        TurnLifecycleFinding(
            code=str(item.get("code") or ""),
            severity=_enum_or_default(
                TurnLifecycleProjectionSeverity,
                item.get("severity"),
                TurnLifecycleProjectionSeverity.INFO,
            ),
            surface=_enum_or_default(
                TurnLifecycleProjectionSurface,
                item.get("surface"),
                TurnLifecycleProjectionSurface.QUERY_ENGINE_HANDOFF,
            ),
            message=str(item.get("message") or ""),
            turn_index=_safe_int(item.get("turn_index"), default=0),
            input_id=str(item.get("input_id") or ""),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("findings", [])
        if isinstance(item, Mapping)
    )
    return TurnLifecycleProjection(
        projection_id=str(payload.get("projection_id") or new_id("turnlife")),
        status=_enum_or_default(
            TurnLifecycleProjectionStatus,
            payload.get("status"),
            TurnLifecycleProjectionStatus.BLOCKED,
        ),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        seeds=(),
        findings=findings,
        created_at=str(payload.get("created_at") or now_iso()),
        metadata=dict(payload.get("metadata") or {}),
    )


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default
