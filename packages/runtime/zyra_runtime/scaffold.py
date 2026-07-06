from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType, new_id, to_jsonable


class RuntimeScaffoldStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class RuntimeOperation(StrEnum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"
    NETWORK = "network"
    BROWSER = "browser"
    MCP = "mcp"
    SKILL = "skill"
    SUBAGENT = "subagent"
    SANDBOX = "sandbox"


class RuntimeSurface(StrEnum):
    SESSION = "session"
    TOOL_LOOP = "tool_loop"
    PERMISSION = "permission"
    MCP = "mcp"
    SKILL = "skill"
    SUBAGENT = "subagent"
    WORKER_BRIDGE = "worker_bridge"


@dataclass(frozen=True, slots=True)
class BudgetEnvelope:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_result_chars: int = 8000
    wall_time_seconds: int = 0
    cost_usd: float = 0.0
    max_turns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ContextWindow:
    max_context_tokens: int = 0
    current_context_tokens: int = 0
    compact_threshold: float = 0.75
    compact_state: str = "not_started"
    restore_points: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class RuntimeSessionContract:
    run_id: str
    task_id: str
    node_id: str
    session_id: str
    worker_id: str
    worker_kind: str
    runtime_kind: str
    workspace_root: str
    artifact_root: str
    event_log_ref: str = ""
    checkpoint_ref: str = ""
    status: str = "created"
    lifecycle_phase: str = "session_started"
    turn_count: int = 0
    budget: BudgetEnvelope = field(default_factory=BudgetEnvelope)
    context_window: ContextWindow = field(default_factory=ContextWindow)
    permission_profile_id: str = ""
    mcp_registry_id: str = ""
    skill_scope: list[str] = field(default_factory=list)
    subagent_scope: list[str] = field(default_factory=list)
    memory_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    dispatch_envelope_id: str = ""
    source_ledger_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    def event(self, phase: str, payload: dict[str, Any] | None = None) -> EventRecord:
        return EventRecord(
            run_id=self.run_id,
            task_id=self.task_id,
            node_id=self.node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "runtime_session": {
                    "session_id": self.session_id,
                    "worker_id": self.worker_id,
                    "worker_kind": self.worker_kind,
                    "runtime_kind": self.runtime_kind,
                    "phase": phase,
                    **(payload or {}),
                }
            },
        )


@dataclass(frozen=True, slots=True)
class ToolLoopBatchPolicy:
    read_only_concurrency: int = 10
    write_serial: bool = True
    allow_mixed_batches: bool = False
    result_budget_chars: int = 8000
    query_context_budget_chars: int = 32000

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ToolLoopStateContract:
    loop_id: str
    session_id: str
    turn_index: int = 0
    phase: str = "created"
    planned_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_calls: list[str] = field(default_factory=list)
    running_tool_calls: list[str] = field(default_factory=list)
    completed_tool_results: list[str] = field(default_factory=list)
    batch_policy: ToolLoopBatchPolicy = field(default_factory=ToolLoopBatchPolicy)
    auto_compact_tracking: dict[str, Any] = field(default_factory=dict)
    has_attempted_reactive_compact: bool = False
    permission_denials: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    event_cursor: str = ""
    artifact_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @property
    def has_live_tools(self) -> bool:
        return bool(self.pending_tool_calls or self.running_tool_calls)


@dataclass(frozen=True, slots=True)
class PermissionDecisionContract:
    request_id: str
    run_id: str
    task_id: str
    node_id: str
    session_id: str
    tool_call_id: str
    operation: RuntimeOperation
    subject: str
    subject_kind: str
    arguments_preview: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    workspace_scope: str = ""
    resource_location: str = "local"
    source: str = "rule"
    effect: str = "ask"
    reason: str = ""
    rule_id: str = ""
    expires_at: str = ""
    persistent: bool = False
    approval_event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class McpServerContract:
    server_id: str
    name: str
    transport: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    auth_profile: str = ""
    enabled: bool = True
    trust_level: str = "workspace"
    permission_scope: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    elicitation_queue: list[str] = field(default_factory=list)
    health: str = "unknown"
    last_refresh_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class SkillContract:
    skill_id: str
    name: str
    version: str
    source: str
    description: str
    body_path: str = ""
    resources: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    default_worker: str = ""
    load_policy: str = "metadata"
    permission_profile: str = ""
    source_ledger_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class SubagentExecutionContract:
    subagent_id: str
    agent_type: str
    parent_session_id: str
    task: str
    model_profile: str = ""
    tool_scope: list[str] = field(default_factory=list)
    mcp_scope: list[str] = field(default_factory=list)
    skill_scope: list[str] = field(default_factory=list)
    workspace_policy: str = "shared_readonly"
    isolation: str = "in_process"
    background: bool = False
    max_turns: int = 0
    output_contract: dict[str, Any] = field(default_factory=dict)
    permission_inheritance: str = "ask"
    result_artifacts: list[str] = field(default_factory=list)
    transcript_ref: str = ""
    memory_delta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class WorkerBridgeContract:
    worker_id: str
    worker_kind: str
    runtime_kind: str
    capabilities: list[str]
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    entrypoint: str = ""
    health_profile: dict[str, Any] = field(default_factory=dict)
    smoke_profile: dict[str, Any] = field(default_factory=dict)
    resource_profile: dict[str, Any] = field(default_factory=dict)
    source_ledger_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class RuntimeScaffoldComponent:
    surface: RuntimeSurface
    status: RuntimeScaffoldStatus
    contract_path: str
    owner_unit: str
    downstream_units: list[str]
    source_references: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class RuntimeScaffold:
    scaffold_id: str
    owner_unit: str
    components: list[RuntimeScaffoldComponent]
    session_contract: RuntimeSessionContract
    tool_loop_contract: ToolLoopStateContract
    permission_contract: PermissionDecisionContract
    mcp_servers: list[McpServerContract]
    skills: list[SkillContract]
    subagent_contract: SubagentExecutionContract
    worker_bridges: list[WorkerBridgeContract]

    def health(self) -> dict[str, Any]:
        blocked = [component for component in self.components if component.status == RuntimeScaffoldStatus.BLOCKED]
        degraded = [component for component in self.components if component.status == RuntimeScaffoldStatus.DEGRADED]
        return {
            "ok": not blocked,
            "scaffold_id": self.scaffold_id,
            "owner_unit": self.owner_unit,
            "component_count": len(self.components),
            "blocked_count": len(blocked),
            "degraded_count": len(degraded),
            "session_id": self.session_contract.session_id,
            "worker_bridge_count": len(self.worker_bridges),
            "mcp_server_count": len(self.mcp_servers),
            "skill_count": len(self.skills),
        }

    def event(self, run_id: str = "m1-01b", task_id: str = "runtime-scaffold") -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id="runtime-scaffold",
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "runtime_scaffold_health": self.health(),
                "components": [component.to_dict() for component in self.components],
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


def default_m1_01b_runtime_scaffold(project_root: str | Path | None = None) -> RuntimeScaffold:
    root = Path(project_root or ".").resolve()
    session_id = new_id("m1_01b_session")
    components = [
        RuntimeScaffoldComponent(
            surface=RuntimeSurface.SESSION,
            status=RuntimeScaffoldStatus.READY,
            contract_path="packages/runtime/zyra_runtime/scaffold.py:RuntimeSessionContract",
            owner_unit="M1-01B",
            downstream_units=["M1-02B", "M1-02D"],
            source_references=["claude-code-best/src/QueryEngine.ts", "agent-framework/_sessions.py"],
        ),
        RuntimeScaffoldComponent(
            surface=RuntimeSurface.TOOL_LOOP,
            status=RuntimeScaffoldStatus.READY,
            contract_path="packages/runtime/zyra_runtime/scaffold.py:ToolLoopStateContract",
            owner_unit="M1-01B",
            downstream_units=["M1-02C", "M1-03A"],
            source_references=["claude-code-best/src/query.ts", "claude-code-best/src/tools.ts"],
        ),
        RuntimeScaffoldComponent(
            surface=RuntimeSurface.PERMISSION,
            status=RuntimeScaffoldStatus.READY,
            contract_path="packages/runtime/zyra_runtime/scaffold.py:PermissionDecisionContract",
            owner_unit="M1-01B",
            downstream_units=["M1-03A", "M1-04C"],
            source_references=["claude-code-best/src/hooks/toolPermission"],
        ),
        RuntimeScaffoldComponent(
            surface=RuntimeSurface.MCP,
            status=RuntimeScaffoldStatus.READY,
            contract_path="packages/runtime/zyra_runtime/scaffold.py:McpServerContract",
            owner_unit="M1-01B",
            downstream_units=["M1-03B"],
            source_references=["claude-code-best/src/services/mcp"],
        ),
        RuntimeScaffoldComponent(
            surface=RuntimeSurface.SKILL,
            status=RuntimeScaffoldStatus.READY,
            contract_path="packages/runtime/zyra_runtime/scaffold.py:SkillContract",
            owner_unit="M1-01B",
            downstream_units=["M1-03C", "M1-06C"],
            source_references=["claude-code-best/src/tools/SkillTool"],
        ),
        RuntimeScaffoldComponent(
            surface=RuntimeSurface.SUBAGENT,
            status=RuntimeScaffoldStatus.READY,
            contract_path="packages/runtime/zyra_runtime/scaffold.py:SubagentExecutionContract",
            owner_unit="M1-01B",
            downstream_units=["M1-03D", "M1-07A"],
            source_references=["claude-code-best/src/tools/AgentTool"],
        ),
        RuntimeScaffoldComponent(
            surface=RuntimeSurface.WORKER_BRIDGE,
            status=RuntimeScaffoldStatus.READY,
            contract_path="packages/runtime/zyra_runtime/scaffold.py:WorkerBridgeContract",
            owner_unit="M1-01B",
            downstream_units=["M1-05C", "M1-07A", "M1-08"],
            source_references=["OpenHands app_server event", "agentscope workspace manager"],
        ),
    ]
    session = RuntimeSessionContract(
        run_id="m1-01b",
        task_id="runtime-scaffold",
        node_id="runtime-contract",
        session_id=session_id,
        worker_id="code-worker-pilot",
        worker_kind="code",
        runtime_kind="claude-code-runtime-pilot",
        workspace_root=str(root),
        artifact_root=str(root / "tmp" / "artifacts"),
        event_log_ref="tmp/events.jsonl",
        checkpoint_ref="tmp/checkpoints",
        status="ready",
        lifecycle_phase="session_contract_ready",
        budget=BudgetEnvelope(max_turns=12, tool_result_chars=8000, wall_time_seconds=900),
        context_window=ContextWindow(max_context_tokens=200000, compact_threshold=0.72),
        permission_profile_id="m1-01b-default-permission",
        mcp_registry_id="m1-01b-mcp-registry",
        skill_scope=["verification", "code-change", "failure-recovery"],
        subagent_scope=["planner", "verifier", "code-worker"],
        source_ledger_ids=[],
    )
    tool_loop = ToolLoopStateContract(
        loop_id=new_id("m1_01b_loop"),
        session_id=session_id,
        phase="scaffold_ready",
        batch_policy=ToolLoopBatchPolicy(read_only_concurrency=10, write_serial=True),
    )
    permission = PermissionDecisionContract(
        request_id=new_id("m1_01b_perm"),
        run_id="m1-01b",
        task_id="runtime-scaffold",
        node_id="permission-contract",
        session_id=session_id,
        tool_call_id="tool-call-placeholder",
        operation=RuntimeOperation.SHELL,
        subject="pilot extraction smoke command",
        subject_kind="command",
        risk_level="low",
        workspace_scope="project",
        effect="ask",
        reason="M1-01B only defines the permission decision envelope; M1-03A implements enforcement.",
    )
    mcp_servers = [
        McpServerContract(
            server_id="m1-01b-local-tools",
            name="local-tool-registry",
            transport="stdio",
            command="python",
            args=["scripts/zyra_source_extract.py", "smoke"],
            trust_level="workspace",
            permission_scope=["read", "tool"],
            tools=["source-scan", "ledger-gate", "runtime-health"],
            health="ready",
        )
    ]
    skills = [
        SkillContract(
            skill_id="m1-01b-runtime-scaffold-review",
            name="runtime-scaffold-review",
            version="0.1.0",
            source="builtin",
            description="Review runtime scaffold, extraction report, ledger entry, and downstream M1-02A handoff.",
            allowed_tools=["file_read", "shell", "ledger"],
            default_worker="CodeWorker",
            load_policy="metadata",
        )
    ]
    subagent = SubagentExecutionContract(
        subagent_id="m1-01b-verifier",
        agent_type="verifier",
        parent_session_id=session_id,
        task="Verify source extraction report, runtime scaffold smoke, and ledger gate.",
        model_profile="gpt-5.5 xhigh",
        tool_scope=["file_read", "shell", "ledger"],
        skill_scope=["verification"],
        max_turns=8,
        permission_inheritance="ask",
    )
    worker_bridges = [
        WorkerBridgeContract(
            worker_id="code-worker-pilot",
            worker_kind="code",
            runtime_kind="claude-code-runtime-pilot",
            capabilities=["tool-loop-contract", "permission-envelope", "artifact-trace"],
            tools=["file_read", "file_write", "shell"],
            skills=["code-change", "verification"],
            entrypoint="packages/workers/zyra_workers/code_worker_runtime.py",
            health_profile={"command": "python scripts/verify_code_worker_sidecar.py"},
            smoke_profile={"tool_plan": ["file_write", "file_read"]},
            resource_profile={"location": "local", "privacy": "workspace"},
        ),
        WorkerBridgeContract(
            worker_id="browser-worker-pilot",
            worker_kind="browser",
            runtime_kind="browser-use-runtime",
            capabilities=["browser-action-contract", "artifact-screenshot"],
            tools=["browser"],
            entrypoint="packages/workers/zyra_workers/browser_worker.py",
            health_profile={"command": "python scripts/smoke_browser_use_runtime.py"},
            smoke_profile={"scenario": "local-html-open-extract"},
            resource_profile={"location": "local", "privacy": "workspace"},
        ),
        WorkerBridgeContract(
            worker_id="sandbox-worker-pilot",
            worker_kind="sandbox",
            runtime_kind="sandbox-gateway-scaffold",
            capabilities=["workspace-boundary", "artifact-io"],
            tools=["shell", "file_read", "file_write"],
            entrypoint="packages/workers/zyra_workers/runtime_scaffold.py:SandboxWorkerScaffold",
            health_profile={"check": "workspace_root and artifact_root are project-bound"},
            smoke_profile={"scenario": "path-boundary-artifact-write"},
            resource_profile={"location": "local", "privacy": "workspace"},
        ),
        WorkerBridgeContract(
            worker_id="memory-worker-pilot",
            worker_kind="memory",
            runtime_kind="memory-fabric-scaffold",
            capabilities=["memory-refresh", "compact-trace"],
            tools=["memory"],
            entrypoint="packages/memory/zyra_memory/fabric.py",
            health_profile={"check": "MemoryFabric import and sqlite store boundary"},
            smoke_profile={"scenario": "refresh-and-compact"},
            resource_profile={"location": "local", "privacy": "workspace"},
        ),
        WorkerBridgeContract(
            worker_id="scheduler-worker-pilot",
            worker_kind="scheduler",
            runtime_kind="scheduler-recovery-scaffold",
            capabilities=["resource-decision", "recovery-plan"],
            tools=["scheduler", "failure_injection"],
            entrypoint="packages/scheduler/zyra_scheduler/scheduler.py",
            health_profile={"check": "ResourceScheduler and RecoveryPlanner import"},
            smoke_profile={"scenario": "select-worker-and-plan-recovery"},
            resource_profile={"location": "local", "privacy": "workspace"},
        ),
    ]
    return RuntimeScaffold(
        scaffold_id="m1-01b-runtime-scaffold",
        owner_unit="M1-01B",
        components=components,
        session_contract=session,
        tool_loop_contract=tool_loop,
        permission_contract=permission,
        mcp_servers=mcp_servers,
        skills=skills,
        subagent_contract=subagent,
        worker_bridges=worker_bridges,
    )


def runtime_scaffold_event_payload(scaffold: RuntimeScaffold) -> dict[str, Any]:
    return {
        "runtime_scaffold": scaffold.to_dict(),
        "runtime_scaffold_health": scaffold.health(),
    }


__all__ = [
    "BudgetEnvelope",
    "ContextWindow",
    "McpServerContract",
    "PermissionDecisionContract",
    "RuntimeOperation",
    "RuntimeScaffold",
    "RuntimeScaffoldComponent",
    "RuntimeScaffoldStatus",
    "RuntimeSessionContract",
    "RuntimeSurface",
    "SkillContract",
    "SubagentExecutionContract",
    "ToolLoopBatchPolicy",
    "ToolLoopStateContract",
    "WorkerBridgeContract",
    "default_m1_01b_runtime_scaffold",
    "runtime_scaffold_event_payload",
]
