from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AgentRole(StrEnum):
    USER = "user"
    SYSTEM = "system"
    SUPERVISOR = "supervisor"
    PLANNER = "planner"
    ROUTER = "router"
    WORKER = "worker"
    EVALUATOR = "evaluator"
    MEMORY = "memory"
    SYMBOLIC = "symbolic"


class PlanNodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REPLANNED = "replanned"
    SUPERSEDED = "superseded"
    NEEDS_REVISION = "needs_revision"


class MessageIntent(StrEnum):
    REQUEST = "request"
    PLAN = "plan"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    OBSERVATION = "observation"
    CRITIQUE = "critique"
    DECISION = "decision"
    REQUIREMENT_CHANGE = "requirement_change"
    FAILURE_INJECTION = "failure_injection"
    NODE_FAILURE = "node_failure"
    STATUS = "status"


class ArtifactKind(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    CODE = "code"
    FILE = "file"
    REPORT = "report"
    TRACE = "trace"
    DATASET = "dataset"
    SCREENSHOT = "screenshot"
    STRUCTURED_DATA = "structured_data"


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    NODE_CREATED = "node_created"
    NODE_UPDATED = "node_updated"
    AGENT_MESSAGE = "agent_message"
    ARTIFACT_WRITTEN = "artifact_written"
    SKILL_INVOKED = "skill_invoked"
    CONTROL_COMMAND = "control_command"
    REQUIREMENT_CHANGE = "requirement_change"
    FAILURE_INJECTED = "failure_injected"
    NODE_FAILED = "node_failed"
    CONSTRAINT_CHECK = "constraint_check"
    TOPOLOGY_ROUTE = "topology_route"
    RESOURCE_DECISION = "resource_decision"
    RECOVERY_PLANNED = "recovery_planned"
    WORKER_HEALTH = "worker_health"
    EVALUATION = "evaluation"
    BUDGET_UPDATED = "budget_updated"
    MCP_CONFIG_CHANGED = "mcp_config_changed"
    MCP_CONNECTION_CHANGED = "mcp_connection_changed"
    MCP_CAPABILITIES_CHANGED = "mcp_capabilities_changed"
    MCP_AUTH_CHANGED = "mcp_auth_changed"
    MCP_ELICITATION = "mcp_elicitation"
    MCP_TASK_UPDATED = "mcp_task_updated"
    MCP_INSTRUCTIONS_CHANGED = "mcp_instructions_changed"
    MCP_TOOL_RESULT = "mcp_tool_result"
    SUBAGENT_TASK_CREATED = "subagent_task_created"
    SUBAGENT_TASK_UPDATED = "subagent_task_updated"
    SUBAGENT_DISPATCHED = "subagent_dispatched"
    SUBAGENT_PROGRESS = "subagent_progress"
    SUBAGENT_COMPLETED = "subagent_completed"
    SUBAGENT_FAILED = "subagent_failed"
    SUBAGENT_CANCELLED = "subagent_cancelled"
    SUBAGENT_RESUMED = "subagent_resumed"
    SUBAGENT_MESSAGE = "subagent_message"
    SUBAGENT_ISOLATION = "subagent_isolation"
    COMMAND_REQUESTED = "command_requested"
    COMMAND_VALIDATED = "command_validated"
    COMMAND_QUEUED = "command_queued"
    COMMAND_STARTED = "command_started"
    COMMAND_SUCCEEDED = "command_succeeded"
    COMMAND_FAILED = "command_failed"
    COMMAND_CANCELLED = "command_cancelled"
    COMMAND_REGISTRY_REFRESHED = "command_registry_refreshed"
    PROMPT_QUEUE_UPDATED = "prompt_queue_updated"
    SIDE_QUESTION = "side_question"
    SYSTEM_NOTICE = "system_notice"
    BROWSER_SESSION_LIFECYCLE = "browser_session_lifecycle"
    BROWSER_TARGET_LIFECYCLE = "browser_target_lifecycle"
    BROWSER_CDP_REQUEST = "browser_cdp_request"
    BROWSER_RUNTIME_DIAGNOSTIC = "browser_runtime_diagnostic"
    TERMINAL_SESSION_LIFECYCLE = "terminal_session_lifecycle"
    TERMINAL_OUTPUT = "terminal_output"
    TERMINAL_CONTROL = "terminal_control"
    MEMORY_CURATOR_SCHEDULED = "memory_curator_scheduled"
    MEMORY_CURATOR_CANDIDATE = "memory_curator_candidate"
    MEMORY_CURATOR_ACCEPTED = "memory_curator_accepted"
    MEMORY_CURATOR_COMMITTED = "memory_curator_committed"
    MEMORY_CURATOR_INDEX_PUBLISHED = "memory_curator_index_published"
    MEMORY_CURATOR_REJECTED = "memory_curator_rejected"
    MEMORY_CURATOR_RECOVERED = "memory_curator_recovered"


@dataclass(slots=True)
class ArtifactRef:
    artifact_id: str = field(default_factory=lambda: new_id("artifact"))
    kind: ArtifactKind = ArtifactKind.FILE
    uri: str = ""
    title: str = ""
    producer_node_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EvidenceRef:
    evidence_id: str = field(default_factory=lambda: new_id("evidence"))
    source: str = ""
    summary: str = ""
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConstraintSet:
    objectives: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    allowed_workers: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    resource_preferences: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] = field(default_factory=dict)
    state_transition_rules: dict[str, Any] = field(default_factory=dict)
    max_tool_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    time_limit_ms: int | None = None
    message_budget_chars: int = 2000


@dataclass(slots=True)
class PlanNode:
    node_id: str = field(default_factory=lambda: new_id("node"))
    title: str = ""
    description: str = ""
    status: PlanNodeStatus = PlanNodeStatus.PENDING
    intent: MessageIntent = MessageIntent.PLAN
    summary: str = ""
    parent_node_id: str | None = None
    assigned_worker_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    constraints: ConstraintSet = field(default_factory=ConstraintSet)
    expected_output_schema: dict[str, Any] = field(default_factory=dict)
    completion_criteria: list[str] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    state_delta: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentMessage:
    run_id: str
    task_id: str
    sender_role: AgentRole
    receiver_role: AgentRole
    intent: MessageIntent
    content: str
    message_id: str = field(default_factory=lambda: new_id("msg"))
    node_id: str | None = None
    summary: str = ""
    constraints: ConstraintSet = field(default_factory=ConstraintSet)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    expected_output_schema: dict[str, Any] = field(default_factory=dict)
    state_delta: dict[str, Any] = field(default_factory=dict)
    message_budget_chars: int = 2000
    created_at: str = field(default_factory=now_iso)
    evidence: list[EvidenceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ControlCommand:
    run_id: str
    task_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: new_id("cmd"))
    issued_by: AgentRole = AgentRole.USER
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DecisionRecord:
    run_id: str
    task_id: str
    summary: str
    decision_id: str = field(default_factory=lambda: new_id("decision"))
    node_id: str | None = None
    decision_type: str = "runtime_decision"
    selected: str = ""
    rationale: str = ""
    alternatives: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    route_candidates: list[dict[str, Any]] = field(default_factory=list)
    affected_node_ids: list[str] = field(default_factory=list)
    state_delta: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceRef] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EventRecord:
    run_id: str
    task_id: str
    event_type: EventType
    event_id: str = field(default_factory=lambda: new_id("event"))
    node_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BudgetState:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    wall_time_ms: int = 0
    cost_usd_estimate: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResourceProfile:
    cpu_weight: float = 1.0
    memory_mb: int | None = None
    network: bool = False
    browser: bool = False
    sandbox: str = "workspace"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkerProfile:
    worker_id: str = field(default_factory=lambda: new_id("worker"))
    name: str = ""
    role: AgentRole = AgentRole.WORKER
    capabilities: list[str] = field(default_factory=list)
    resource_profile: ResourceProfile = field(default_factory=ResourceProfile)
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskState:
    run_id: str
    task_id: str
    user_goal: str
    root_node_id: str
    status: PlanNodeStatus = PlanNodeStatus.PENDING
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    constraints: ConstraintSet = field(default_factory=ConstraintSet)
    plan_nodes: dict[str, PlanNode] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    budget: BudgetState = field(default_factory=BudgetState)
    metadata: dict[str, Any] = field(default_factory=dict)


def create_task_state(user_goal: str) -> TaskState:
    run_id = new_id("run")
    task_id = new_id("task")
    root_node = PlanNode(
        title="Root task",
        description=user_goal,
        metadata={"source": "initial_user_goal"},
    )
    return TaskState(
        run_id=run_id,
        task_id=task_id,
        user_goal=user_goal,
        root_node_id=root_node.node_id,
        constraints=ConstraintSet(
            objectives=[user_goal],
            success_criteria=["Produce a traceable result that satisfies the user goal."],
        ),
        plan_nodes={root_node.node_id: root_node},
    )


EnumT = TypeVar("EnumT", bound=StrEnum)


def task_state_from_json(data: Mapping[str, Any]) -> TaskState:
    constraints = _constraint_set_from_json(_as_mapping(data.get("constraints")))
    plan_nodes = {
        str(node_id): _plan_node_from_json(_as_mapping(node_data))
        for node_id, node_data in _as_mapping(data.get("plan_nodes")).items()
    }
    artifacts = [
        _artifact_ref_from_json(_as_mapping(item))
        for item in _as_list(data.get("artifacts"))
    ]
    decisions = [
        _decision_record_from_json(_as_mapping(item))
        for item in _as_list(data.get("decisions"))
    ]
    budget = _budget_state_from_json(_as_mapping(data.get("budget")))

    return TaskState(
        run_id=str(data["run_id"]),
        task_id=str(data["task_id"]),
        user_goal=str(data.get("user_goal", "")),
        root_node_id=str(data["root_node_id"]),
        status=_enum_or_default(
            PlanNodeStatus,
            data.get("status"),
            PlanNodeStatus.PENDING,
        ),
        created_at=str(data.get("created_at") or now_iso()),
        updated_at=str(data.get("updated_at") or now_iso()),
        constraints=constraints,
        plan_nodes=plan_nodes,
        artifacts=artifacts,
        decisions=decisions,
        budget=budget,
        metadata=dict(_as_mapping(data.get("metadata"))),
    )


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: to_jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [to_jsonable(item) for item in value]
    return value


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _enum_or_default(enum_type: type[EnumT], value: Any, default: EnumT) -> EnumT:
    try:
        return enum_type(str(value))
    except ValueError:
        return default


def _artifact_ref_from_json(data: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(data.get("artifact_id") or new_id("artifact")),
        kind=_enum_or_default(ArtifactKind, data.get("kind"), ArtifactKind.FILE),
        uri=str(data.get("uri") or ""),
        title=str(data.get("title") or ""),
        producer_node_id=_optional_str(data.get("producer_node_id")),
        created_at=str(data.get("created_at") or now_iso()),
        metadata=dict(_as_mapping(data.get("metadata"))),
    )


def _evidence_ref_from_json(data: Mapping[str, Any]) -> EvidenceRef:
    confidence = data.get("confidence")
    return EvidenceRef(
        evidence_id=str(data.get("evidence_id") or new_id("evidence")),
        source=str(data.get("source") or ""),
        summary=str(data.get("summary") or ""),
        confidence=float(confidence) if isinstance(confidence, int | float) else None,
        metadata=dict(_as_mapping(data.get("metadata"))),
    )


def _constraint_set_from_json(data: Mapping[str, Any]) -> ConstraintSet:
    return ConstraintSet(
        objectives=[str(item) for item in _as_list(data.get("objectives"))],
        requirements=[str(item) for item in _as_list(data.get("requirements"))],
        forbidden=[str(item) for item in _as_list(data.get("forbidden"))],
        assumptions=[str(item) for item in _as_list(data.get("assumptions"))],
        success_criteria=[str(item) for item in _as_list(data.get("success_criteria"))],
        allowed_tools=[str(item) for item in _as_list(data.get("allowed_tools"))],
        required_tools=[str(item) for item in _as_list(data.get("required_tools"))],
        allowed_workers=[str(item) for item in _as_list(data.get("allowed_workers"))],
        required_artifacts=[str(item) for item in _as_list(data.get("required_artifacts"))],
        resource_preferences=[str(item) for item in _as_list(data.get("resource_preferences"))],
        output_schema=dict(_as_mapping(data.get("output_schema"))),
        state_transition_rules=dict(_as_mapping(data.get("state_transition_rules"))),
        max_tool_calls=_optional_int(data.get("max_tool_calls")),
        max_input_tokens=_optional_int(data.get("max_input_tokens")),
        max_output_tokens=_optional_int(data.get("max_output_tokens")),
        time_limit_ms=_optional_int(data.get("time_limit_ms")),
        message_budget_chars=_positive_int(data.get("message_budget_chars"), default=2000),
    )


def _plan_node_from_json(data: Mapping[str, Any]) -> PlanNode:
    return PlanNode(
        node_id=str(data.get("node_id") or new_id("node")),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        status=_enum_or_default(
            PlanNodeStatus,
            data.get("status"),
            PlanNodeStatus.PENDING,
        ),
        intent=_enum_or_default(MessageIntent, data.get("intent"), MessageIntent.PLAN),
        summary=str(data.get("summary") or ""),
        parent_node_id=_optional_str(data.get("parent_node_id")),
        assigned_worker_id=_optional_str(data.get("assigned_worker_id")),
        depends_on=[str(item) for item in _as_list(data.get("depends_on"))],
        constraints=_constraint_set_from_json(_as_mapping(data.get("constraints"))),
        expected_output_schema=dict(_as_mapping(data.get("expected_output_schema"))),
        completion_criteria=[str(item) for item in _as_list(data.get("completion_criteria"))],
        evidence_refs=[
            _evidence_ref_from_json(_as_mapping(item))
            for item in _as_list(data.get("evidence_refs"))
        ],
        artifact_refs=[
            _artifact_ref_from_json(_as_mapping(item))
            for item in _as_list(data.get("artifact_refs"))
        ],
        state_delta=dict(_as_mapping(data.get("state_delta"))),
        created_at=str(data.get("created_at") or now_iso()),
        updated_at=str(data.get("updated_at") or now_iso()),
        metadata=dict(_as_mapping(data.get("metadata"))),
    )


def _decision_record_from_json(data: Mapping[str, Any]) -> DecisionRecord:
    return DecisionRecord(
        run_id=str(data.get("run_id") or ""),
        task_id=str(data.get("task_id") or ""),
        summary=str(data.get("summary") or ""),
        decision_id=str(data.get("decision_id") or new_id("decision")),
        node_id=_optional_str(data.get("node_id")),
        decision_type=str(data.get("decision_type") or "runtime_decision"),
        selected=str(data.get("selected") or ""),
        rationale=str(data.get("rationale") or ""),
        alternatives=[str(item) for item in _as_list(data.get("alternatives"))],
        checks=[dict(_as_mapping(item)) for item in _as_list(data.get("checks"))],
        route_candidates=[dict(_as_mapping(item)) for item in _as_list(data.get("route_candidates"))],
        affected_node_ids=[str(item) for item in _as_list(data.get("affected_node_ids"))],
        state_delta=dict(_as_mapping(data.get("state_delta"))),
        evidence=[
            _evidence_ref_from_json(_as_mapping(item))
            for item in _as_list(data.get("evidence"))
        ],
        created_at=str(data.get("created_at") or now_iso()),
        metadata=dict(_as_mapping(data.get("metadata"))),
    )


def _budget_state_from_json(data: Mapping[str, Any]) -> BudgetState:
    return BudgetState(
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        tool_calls=int(data.get("tool_calls") or 0),
        wall_time_ms=int(data.get("wall_time_ms") or 0),
        cost_usd_estimate=float(data.get("cost_usd_estimate") or 0.0),
        metadata=dict(_as_mapping(data.get("metadata"))),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any, default: int) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else default
