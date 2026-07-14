from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from zyra_core import ArtifactKind, EventRecord, EventType, new_id, to_jsonable
from zyra_runtime import (
    CodeWorkerSessionFoundationRuntime,
    CodeWorkerSessionStore,
    ContextAssemblyBudget,
    ContextAssemblyRuntime,
    ClaudeQueryEngineConfig,
    CodeWorkerSessionReplayRuntime,
    SessionFoundationAuditor,
    SessionAcceptanceRuntime,
    SessionLifecycleRuntime,
    QuerySessionIntegrationRuntime,
    QuerySessionEventFlowRuntime,
    QuerySessionHandoffContractRuntime,
    QuerySessionResumeCustodyRuntime,
    QuerySessionStateGraphRuntime,
    QuerySessionDisconnectAuditRuntime,
    TranscriptEventMapper,
    TurnLifecycleRuntime,
    JsonPermissionStore,
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyError,
    PermissionSessionCustodyReceipt,
    PermissionSessionCustodyStore,
    PermissionStateStore,
    QueryInputProcessor,
    ToolExecutionContext,
    ToolSessionBridgeRuntime,
    WorkerRequest,
    WorkerResult,
    assemble_claude_runtime_context,
    build_worker_execution_gate_inputs,
    build_worker_execution_gate_report,
    build_claude_productization_integration_report,
    build_claude_source_graph_audit,
    build_productized_claude_runtime_contracts,
    claude_productization_integration_events,
    claude_runtime_context_assembly_events,
    claude_source_graph_audit_event,
    query_plan_metadata_from_constraints,
    query_turns_from_constraints as productized_query_turns_from_constraints,
    foundation_audit_event,
    foundation_audit_metadata,
    render_foundation_audit_markdown,
    session_acceptance_metadata,
    session_lifecycle_metadata,
    query_session_integration_metadata,
    render_query_session_integration_markdown,
    query_event_flow_metadata,
    query_handoff_metadata,
    query_resume_custody_metadata,
    query_state_graph_metadata,
    query_disconnect_metadata,
    render_query_event_flow_markdown,
    render_query_disconnect_markdown,
    render_query_handoff_markdown,
    render_query_resume_custody_markdown,
    render_query_state_graph_markdown,
    session_replay_metadata,
    seed_failure_result_metadata,
    session_seed_metadata,
    tool_session_bridge_metadata,
    transcript_mapping_metadata,
    turn_lifecycle_metadata,
    runtime_context_assembly_markdown,
    source_graph_audit_markdown,
    source_graph_crosswalk_markdown,
    worker_execution_gate_event,
    worker_execution_gate_markdown,
)
from .typescript_claude_runtime import TypeScriptClaudeQueryEngine
from zyra_skills import (
    SkillToolProjectionRuntime,
    default_mcp_skill_discovery_runtime,
)
from zyra_runtime.permission.extensions import (
    PermissionExtensionRegistry,
    build_deployment_permission_extensions,
)

from .code_worker_bridge import CodeWorkerSidecarClient
from zyra_runtime.sandbox_gateway.integration_factory import (
    install_gateway_runtime_services,
)


@dataclass(frozen=True, slots=True)
class CodeWorkerRun:
    worker_result: WorkerResult
    event_records: list[EventRecord]
    session_custody_token: str = field(default="", repr=False)
    session_id: str = ""
    session_custody_id: str = ""
    session_custody_fingerprint: str = ""
    session_custody_created: bool = False

    def safe_session_metadata(self) -> dict[str, Any]:
        """Public session metadata with no bearer material."""

        return {
            "session_id": self.session_id,
            "permission_session_custody_id": self.session_custody_id,
            "permission_session_custody_fingerprint": self.session_custody_fingerprint,
            "permission_session_custody_created": self.session_custody_created,
            "session_custody_token_included": False,
        }

    def private_api_session_envelope(self) -> dict[str, Any]:
        """Explicit API handoff; callers must not persist this dictionary."""

        return {
            **self.safe_session_metadata(),
            "session_custody_token": (
                self.session_custody_token if self.session_custody_created else ""
            ),
            "session_custody_token_included": bool(
                self.session_custody_created and self.session_custody_token
            ),
        }


class CodeWorkerRuntime:
    """Code worker loop boundary backed by Zyra-owned Claude Code runtime ports."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        sidecar_client: CodeWorkerSidecarClient | None = None,
        permission_store: JsonPermissionStore | None = None,
        query_engine_factory: Any | None = TypeScriptClaudeQueryEngine,
        permission_bypass_available: bool = False,
        permission_auto_available: bool = False,
        permission_accept_edits_available: bool = False,
        permission_extension_registry: PermissionExtensionRegistry | None = None,
        permission_state_path: str | Path | None = None,
        mcp_runtime: Any | None = None,
        tool_registry: Any | None = None,
        dynamic_handlers: Mapping[str, Any] | None = None,
        runtime_services: Mapping[str, Any] | None = None,
        skill_fork_port: Any | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.sidecar_client = sidecar_client or CodeWorkerSidecarClient(self.project_root)
        self.runtime_contracts = build_productized_claude_runtime_contracts(project_root=self.project_root)
        self.query_engine_factory = query_engine_factory
        self.permission_bypass_available = bool(permission_bypass_available)
        self.permission_auto_available = bool(permission_auto_available)
        self.permission_accept_edits_available = bool(permission_accept_edits_available)
        self.permission_extension_registry = (
            permission_extension_registry or build_deployment_permission_extensions()
        )
        self.mcp_runtime = mcp_runtime
        self.skill_fork_port = skill_fork_port
        self.runtime_services = install_gateway_runtime_services(
            runtime_services,
            workspace_root=workspace_root,
            artifact_root=artifact_root,
            worker_id="CodeWorkerRuntime",
            workspace_edit_port=dict(runtime_services or {}).get("workspace_edit_port"),
        )
        self.execution_context = ToolExecutionContext.for_workspace(
            workspace_root=workspace_root,
            artifact_root=artifact_root,
            permission_store=permission_store,
            registry=tool_registry,
            dynamic_handlers=dynamic_handlers,
            runtime_services=self.runtime_services,
        )
        self.permission_state_path = (
            Path(permission_state_path).resolve()
            if permission_state_path is not None
            else self.execution_context.artifact_store.root / ".permission" / "state.json"
        )

    def run(self, request: WorkerRequest) -> CodeWorkerRun:
        session_custody_token = str(request.constraints.get("session_custody_token") or "")
        resume_session_custody_token = str(
            request.constraints.get("resume_session_custody_token") or ""
        )
        permission_capability_echo_detected = (
            PermissionSessionCustodyStore.contains_capability_echo(
                request.constraints,
                [session_custody_token, resume_session_custody_token],
            )
        )
        explicit_permission_resume = request.constraints.get("permission_continuation_resume")
        # Bearer material is consumed only by the custody gate.  Replace it
        # before any integration report, event, artifact, context snapshot or
        # model-facing projection can observe the WorkerRequest.
        redacted_constraints = PermissionSessionCustodyStore.redact_constraints(
            request.constraints
        )
        if explicit_permission_resume is not None:
            redacted_constraints["permission_continuation_resume"] = "<redacted>"
        request = replace(
            request,
            constraints=redacted_constraints,
        )
        if request.constraints.get("disable_productized_runtime") is True or self.query_engine_factory is None:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime productized QueryEngine runtime is disconnected.",
                error="productized_query_engine_runtime_disabled",
                metadata={
                    **self.runtime_contracts.metadata(),
                    "sidecar_contracts_used": "false",
                    "query_turns": "0",
                    "tool_steps": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
            )

        use_sidecar_contracts = request.constraints.get("use_sidecar_contracts") is True
        integration_report = build_claude_productization_integration_report(
            project_root=self.project_root,
            runtime_contracts=self.runtime_contracts,
            request_constraints=request.constraints,
            sidecar_contracts_used=use_sidecar_contracts,
        )
        integration_events = claude_productization_integration_events(request, integration_report)
        if not integration_report.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before tool execution because Claude source graph integration is blocked.",
                error=integration_report.blocking_error or "claude_productization_integration_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[*integration_events, _worker_result_event(request, worker_result)],
            )
        execution_context = self.execution_context
        typescript_capability_owner = (
            self.query_engine_factory is TypeScriptClaudeQueryEngine
        )
        mcp_projection = None
        skill_projection = None
        mcp_skill_sources: tuple[Any, ...] = ()
        mcp_skill_discovery_receipt = None
        mcp_skill_discovery_events: list[EventRecord] = []
        if self.mcp_runtime is not None and not typescript_capability_owner:
            included_mcp_servers = tuple(
                str(item) for item in request.constraints.get("mcp_include_servers") or () if str(item)
            )
            if included_mcp_servers and request.constraints.get("mcp_network_allowed") is not True:
                raise RuntimeError(
                    "child MCP projection denied because the signed child scope does not permit network access"
                )
            mcp_projection_session_id = str(
                request.constraints.get("session_id")
                or f"mcp-projection:{request.task_id}:{request.request_id}"
            )
            mcp_open = self.mcp_runtime.main_path_runtime.open_for_worker(
                execution_context,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
                session_id=mcp_projection_session_id,
                worker_request_id=request.request_id,
                constraints=request.constraints,
                include_servers=included_mcp_servers or None,
                bootstrap_connections=False,
                checkpoint_session=False,
            )
            mcp_projection = mcp_open.projection
            execution_context = mcp_projection.context
            try:
                discovery = default_mcp_skill_discovery_runtime(
                    self.mcp_runtime,
                    cache_root=self.execution_context.artifact_store.root / ".skill-mcp-cache",
                ).discover(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    worker_request_id=request.request_id,
                    node_id=request.node_id,
                )
                mcp_skill_sources = discovery.sources
                mcp_skill_discovery_receipt = discovery.receipt
                mcp_skill_discovery_events.append(
                    EventRecord(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        node_id=request.node_id,
                        event_type=EventType.SKILL_INVOKED,
                        payload={
                            "phase": "mcp_skill_discovery",
                            "owner_unit": "M1-03C",
                            "mcp_skill_discovery": discovery.receipt.to_dict(),
                            "body_persisted": False,
                        },
                    )
                )
            except Exception as error:  # noqa: BLE001 - one optional source must not hide builtin skills.
                mcp_skill_discovery_events.append(
                    EventRecord(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        node_id=request.node_id,
                        event_type=EventType.SKILL_INVOKED,
                        payload={
                            "phase": "mcp_skill_discovery_failed",
                            "owner_unit": "M1-03C",
                            "error_code": str(getattr(error, "code", "mcp_skill_discovery_failed")),
                            "error_type": type(error).__name__,
                            "body_persisted": False,
                        },
                    )
                )
        skill_projection_session_id = str(
            request.constraints.get("skill_session_id")
            or request.constraints.get("session_id")
            or f"skill:{request.task_id}:{request.request_id}"
        )
        if (
            not typescript_capability_owner
            and request.constraints.get("disable_skill_tool_projection") is not True
        ):
            skill_open = SkillToolProjectionRuntime.open_for_worker(
                execution_context,
                project_root=self.project_root,
                request=request,
                session_id=skill_projection_session_id,
                disabled=False,
                external_sources=mcp_skill_sources,
                fork_port=self.skill_fork_port,
            )
            skill_projection = skill_open.projection
            execution_context = skill_open.context
        child_scope_registry_assert = self.runtime_services.get("child_scope_registry_assert")
        if callable(child_scope_registry_assert):
            # MCP and SkillTool both project tools after construction.  Re-check
            # the final executable registry so neither dynamic source can widen
            # the server-signed parent ceiling.
            child_scope_registry_assert(execution_context.registry)
        tool_specs = execution_context.registry.list()
        tool_names = tuple(tool.name for tool in tool_specs)
        read_only_tool_names = tuple(tool.name for tool in tool_specs if tool.metadata.get("read_only") == "true")
        mutating_tool_names = tuple(tool.name for tool in tool_specs if tool.metadata.get("read_only") != "true")
        runtime_context_report = assemble_claude_runtime_context(
            request=request,
            integration_report=integration_report,
            runtime_contracts=self.runtime_contracts,
            project_root=self.project_root,
            workspace_root=execution_context.workspace_root,
            artifact_root=execution_context.artifact_store.root,
            tool_names=tool_names,
            read_only_tool_names=read_only_tool_names,
            mutating_tool_names=mutating_tool_names,
            permission_mode=str(request.constraints.get("permission_mode") or "workspace"),
        )
        runtime_context_events = claude_runtime_context_assembly_events(request, runtime_context_report)
        if not runtime_context_report.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before tool execution because RuntimeContext assembly is blocked.",
                error=runtime_context_report.first_blocker_code or "claude_runtime_context_assembly_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    _worker_result_event(request, worker_result),
                ],
            )
        source_graph_audit = build_claude_source_graph_audit(
            project_root=self.project_root,
            integration_report=integration_report,
            runtime_contracts=self.runtime_contracts,
            runtime_context_report=runtime_context_report,
        )
        source_graph_audit_events = [claude_source_graph_audit_event(request, source_graph_audit)]
        if not source_graph_audit.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before tool execution because source graph audit is blocked.",
                error=source_graph_audit.first_blocker_code or "claude_source_graph_audit_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    _worker_result_event(request, worker_result),
                ],
            )

        worker_gate = build_worker_execution_gate_report(
            build_worker_execution_gate_inputs(
                request=request,
                project_root=self.project_root,
                workspace_root=execution_context.workspace_root,
                artifact_root=execution_context.artifact_store.root,
                runtime_contracts=self.runtime_contracts,
                integration_report=integration_report,
                runtime_context_report=runtime_context_report,
                source_graph_audit=source_graph_audit,
                tool_specs=tool_specs,
                query_engine_available=self.query_engine_factory is not None,
                sidecar_contracts_used=use_sidecar_contracts,
            )
        )
        worker_gate_event = worker_execution_gate_event(request, worker_gate)
        if not worker_gate.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before QueryEngine because worker execution gate is blocked.",
                error=worker_gate.first_blocker_code or "claude_worker_execution_gate_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    _worker_result_event(request, worker_result),
                ],
            )

        if use_sidecar_contracts:
            runtime_health = self.sidecar_client.health()
            runtime_inventory = self.sidecar_client.runtime_inventory()
            query_contract = self.sidecar_client.query_contract()
            session_contract = self.sidecar_client.session_contract()
            tool_loop_contract = self.sidecar_client.tool_loop_contract()
        else:
            runtime_health = self.runtime_contracts.health
            runtime_inventory = self.runtime_contracts.inventory
            query_contract = self.runtime_contracts.query_contract
            session_contract = self.runtime_contracts.session_contract
            tool_loop_contract = self.runtime_contracts.tool_loop_contract

        query_turns = _bind_permission_tool_use_identity(
            productized_query_turns_from_constraints(request.constraints),
            request.constraints,
        )
        query_plan_metadata = query_plan_metadata_from_constraints(request.constraints)
        session_store = CodeWorkerSessionStore(execution_context.artifact_store.root)
        input_processor = QueryInputProcessor(
            max_input_chars=_positive_int(
                request.constraints.get("max_query_input_chars"),
                default=64000,
            )
        )
        input_report = input_processor.process_worker_request(
            request,
            disabled=request.constraints.get("disable_query_input_processor") is True,
        )
        context_runtime = ContextAssemblyRuntime(
            budget=ContextAssemblyBudget(
                max_chars=_positive_int(
                    request.constraints.get("query_context_budget_chars"),
                    default=32000,
                ),
                reserve_chars=_positive_int(
                    request.constraints.get("query_context_reserve_chars"),
                    default=4000,
                ),
            )
        )
        seed_session_id = str(request.constraints.get("session_id") or new_id("codesession"))
        resume_source_session_id = str(
            request.constraints.get("resume_session_id")
            or request.constraints.get("resume_code_worker_session_id")
            or ""
        )
        runtime_state_source_session_id = resume_source_session_id or seed_session_id
        permission_custody_store = PermissionSessionCustodyStore(
            PermissionStateStore(self.permission_state_path)
        )
        target_binding = PermissionSessionCustodyBinding(
            session_id=seed_session_id,
            run_id=request.run_id,
            task_id=request.task_id,
            workspace_root=str(execution_context.workspace_root),
        )
        target_session_exists = bool(session_store.replay_session(seed_session_id).records)
        try:
            if (
                resume_source_session_id
                and resume_source_session_id != seed_session_id
                and resume_session_custody_token
            ):
                permission_custody_store.verify(
                    PermissionSessionCustodyBinding(
                        session_id=resume_source_session_id,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        workspace_root=str(execution_context.workspace_root),
                    ),
                    presented_token=resume_session_custody_token,
                )
            custody_receipt = permission_custody_store.claim(
                target_binding,
                presented_token=session_custody_token,
                external_session_exists=target_session_exists,
            )
        except PermissionSessionCustodyError as error:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime refused an unverified permission session selector.",
                error=error.code,
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    "permission_session_custody_verified": "false",
                    "permission_session_custody_error": error.code,
                    "permission_session_id": seed_session_id,
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            custody_event = EventRecord(
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "phase": "permission_session_custody_rejected",
                        "session_id": seed_session_id,
                        "worker_request_id": request.request_id,
                        "error": error.code,
                        "token_projected": False,
                    }
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    custody_event,
                    _worker_result_event(request, worker_result),
                ],
            )
        if permission_capability_echo_detected:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime rejected a custody capability copied into task data.",
                error="permission_custody_capability_echo",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    **custody_receipt.metadata(),
                    "permission_capability_echo_rejected": "true",
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return _custodied_code_worker_run(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    _worker_result_event(request, worker_result),
                ],
                custody_receipt=custody_receipt,
            )
        if explicit_permission_resume is not None:
            # Continuation identity must be prepared by the permission API/control
            # plane.  A raw WorkerRequest field is not an execution capability and
            # is deliberately rejected before the QueryEngine can see it.
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime refused a direct permission continuation resume.",
                error="permission_continuation_api_resolver_required",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    **custody_receipt.metadata(),
                    "permission_continuation_resume_accepted": "false",
                    "permission_continuation_resume_owner": "PermissionApiFacade",
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            rejected_event = EventRecord(
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "phase": "permission_continuation_resume_rejected",
                        "session_id": seed_session_id,
                        "worker_request_id": request.request_id,
                        "reason": "api_resolver_required",
                        "resume_payload_projected": False,
                    }
                },
            )
            return _custodied_code_worker_run(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    rejected_event,
                    _worker_result_event(request, worker_result),
                ],
                custody_receipt=custody_receipt,
            )
        runtime_state_load = session_store.load_runtime_state(
            session_id=runtime_state_source_session_id,
            task_id=request.task_id,
            run_id=request.run_id,
        )
        if not runtime_state_load.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime refused a runtime-state checkpoint outside the current task/run scope.",
                error=runtime_state_load.error or "runtime_state_scope_mismatch",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    **runtime_state_load.metadata_values(),
                    "runtime_state_source_session_id": runtime_state_source_session_id,
                    "runtime_state_target_session_id": seed_session_id,
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return _custodied_code_worker_run(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    _worker_result_event(request, worker_result),
                ],
                custody_receipt=custody_receipt,
            )
        if self.mcp_runtime is not None:
            self.mcp_runtime.restore_session_from_store(
                session_store,
                session_id=seed_session_id,
                run_id=request.run_id,
                task_id=request.task_id,
                worker_request_id=request.request_id,
                node_id=str(request.node_id or ""),
                require_causality=False,
            )
            request = replace(
                request,
                constraints=self.mcp_runtime.prepare_worker_constraints(
                    request.constraints,
                    session_id=seed_session_id,
                ),
            )
        effective_permission_mode = _permission_mode_from_state_owner(
            permission_custody_store.state_store,
            session_id=seed_session_id,
            restored_runtime_state=(
                runtime_state_load.runtime_state
                if runtime_state_load.found
                and runtime_state_source_session_id == seed_session_id
                else {}
            ),
            fallback=request.constraints.get("permission_mode"),
            bypass_available=self.permission_bypass_available,
            auto_available=self.permission_auto_available,
            accept_edits_available=self.permission_accept_edits_available,
        )
        context_snapshot = context_runtime.assemble(
            request=request,
            session_id=seed_session_id,
            input_records=input_report.records,
            tool_specs=tool_specs,
            project_root=self.project_root,
            workspace_root=execution_context.workspace_root,
            artifact_root=execution_context.artifact_store.root,
            runtime_contracts=self.runtime_contracts,
            integration_report=integration_report,
            runtime_context_report=runtime_context_report,
            source_graph_audit=source_graph_audit,
            permission_mode=effective_permission_mode,
            disabled=request.constraints.get("disable_context_assembly") is True,
        )
        session_foundation = CodeWorkerSessionFoundationRuntime(store=session_store)
        session_seed = session_foundation.build_seed(
            request=request,
            input_report=input_report,
            context_snapshot=context_snapshot,
            session_id=seed_session_id,
            disabled_store=request.constraints.get("disable_code_worker_session_store") is True,
        )
        session_seed_events = session_foundation.seed_events(session_seed)
        tool_session_bridge_runtime = ToolSessionBridgeRuntime()
        tool_session_bridge = tool_session_bridge_runtime.build_report(
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            request_messages=[to_jsonable(message) for message in request.messages],
            constraints=request.constraints,
            fallback_turns=query_turns,
        )
        bridged_query_turns = tool_session_bridge.to_tool_turns()
        if bridged_query_turns:
            query_turns = bridged_query_turns
            query_plan_metadata = {
                **query_plan_metadata,
                **tool_session_bridge_metadata(tool_session_bridge),
                "query_plan_source": str(tool_session_bridge.origin),
                "query_plan_tool_steps": str(tool_session_bridge.valid_tool_use_count),
                "query_plan_turns": str(tool_session_bridge.generated_turn_count),
                "query_plan_valid_turns": str(tool_session_bridge.generated_turn_count),
            }
        else:
            query_plan_metadata = {**query_plan_metadata, **tool_session_bridge_metadata(tool_session_bridge)}
        tool_session_bridge_events = tool_session_bridge_runtime.events_for_report(
            tool_session_bridge,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        session_seed_events = [*session_seed_events, *tool_session_bridge_events]
        replay_runtime = CodeWorkerSessionReplayRuntime(session_store)
        replay_plan = replay_runtime.build_plan_from_constraints(
            request.constraints,
            expected_task_id=request.task_id,
            expected_run_id=request.run_id,
            target_session_id=session_seed.session_id,
            target_worker_request_id=request.request_id,
        )
        if replay_plan is not None and (
            replay_plan.session_id != session_seed.session_id
            or replay_plan.worker_request_id != request.request_id
        ):
            source_session_id = str(replay_plan.metadata.get("source_session_id") or replay_plan.session_id)
            source_worker_request_id = str(
                replay_plan.metadata.get("source_worker_request_id") or replay_plan.worker_request_id
            )
            replay_plan = replace(
                replay_plan,
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                metadata={
                    **replay_plan.metadata,
                    "replay_source_session_id": source_session_id,
                    "replay_source_worker_request_id": source_worker_request_id,
                    "replay_current_session_id": session_seed.session_id,
                    "replay_current_worker_request_id": request.request_id,
                    "replay_identity_rebound": True,
                },
            )
        replay_events = []
        if replay_plan is not None:
            replay_events.append(
                replay_runtime.event_for_plan(
                    replay_plan,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                )
            )
        turn_lifecycle_runtime = TurnLifecycleRuntime()
        turn_lifecycle_projection = turn_lifecycle_runtime.project(
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            input_records=input_report.records,
            query_turns=query_turns,
            context_snapshot=context_snapshot,
            replay_plan=replay_plan,
        )
        turn_lifecycle_event = turn_lifecycle_runtime.event_for_projection(
            turn_lifecycle_projection,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        foundation_auditor = SessionFoundationAuditor()
        foundation_audit = foundation_auditor.audit_seed(
            session_seed,
            store=session_store,
            events=[*session_seed_events, *replay_events, turn_lifecycle_event],
        )
        foundation_audit_record = foundation_audit_event(foundation_audit)
        acceptance_runtime = SessionAcceptanceRuntime()
        pre_query_acceptance = acceptance_runtime.evaluate(
            seed=session_seed,
            foundation_audit=foundation_audit,
            turn_lifecycle=turn_lifecycle_projection,
            replay_plan=replay_plan,
            transcript_mapping=None,
            require_transcript=False,
        )
        pre_query_acceptance_event = acceptance_runtime.event_for_report(
            pre_query_acceptance,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        lifecycle_runtime = SessionLifecycleRuntime()
        pre_query_lifecycle_report = lifecycle_runtime.build_report(
            [*session_seed_events, *replay_events, turn_lifecycle_event, foundation_audit_record, pre_query_acceptance_event],
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            require_query_engine=False,
            require_transcript=False,
        )
        pre_query_lifecycle_event = lifecycle_runtime.event_for_report(
            pre_query_lifecycle_report,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        if not session_seed.ok or not foundation_audit.ok or not turn_lifecycle_projection.ok or not pre_query_acceptance.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before QueryEngine because query session foundation is blocked.",
                error="code_worker_session_foundation_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    **query_plan_metadata,
                    **seed_failure_result_metadata(session_seed, error="code_worker_session_foundation_failed"),
                    **foundation_audit_metadata(foundation_audit),
                    **session_replay_metadata(replay_plan),
                    **turn_lifecycle_metadata(turn_lifecycle_projection),
                    **session_acceptance_metadata(pre_query_acceptance),
                    **session_lifecycle_metadata(pre_query_lifecycle_report),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                },
            )
            return _custodied_code_worker_run(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    *session_seed_events,
                    *replay_events,
                    turn_lifecycle_event,
                    foundation_audit_record,
                    pre_query_acceptance_event,
                    pre_query_lifecycle_event,
                    _worker_result_event(request, worker_result),
                ],
                custody_receipt=custody_receipt,
            )
        query_session_runtime = QuerySessionIntegrationRuntime(
            store=session_store,
            artifact_store=execution_context.artifact_store,
        )
        query_session_integration = query_session_runtime.prepare(
            request=request,
            seed=session_seed,
            input_report=input_report,
            context_snapshot=context_snapshot,
            tool_specs=tool_specs,
            query_turns=query_turns,
            turn_lifecycle=turn_lifecycle_projection,
            foundation_audit=foundation_audit,
            acceptance_report=pre_query_acceptance,
            lifecycle_report=pre_query_lifecycle_report,
            replay_plan=replay_plan,
        )
        query_session_integration_events = query_session_runtime.events_for_report(
            query_session_integration,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        handoff_runtime = QuerySessionHandoffContractRuntime()
        query_handoff = handoff_runtime.build_report(query_session_integration.packet.to_dict(include_text=True))
        query_handoff_event = handoff_runtime.event_for_report(
            query_handoff,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        custody_runtime = QuerySessionResumeCustodyRuntime()
        def bind_custody_to_current_request(report: Any) -> Any:
            source_worker_request_ids = sorted(
                {
                    record.worker_request_id
                    for record in report.records
                    if record.worker_request_id and record.worker_request_id != request.request_id
                }
            )
            rebound_records = tuple(
                replace(
                    record,
                    session_id=session_seed.session_id,
                    worker_request_id=request.request_id,
                    metadata={
                        **record.metadata,
                        "source_session_id": record.session_id,
                        "source_worker_request_id": record.worker_request_id,
                        "current_session_id": session_seed.session_id,
                        "current_worker_request_id": request.request_id,
                    },
                )
                for record in report.records
            )
            return replace(
                report,
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                records=rebound_records,
                metadata={
                    **report.metadata,
                    "source_session_id": report.session_id,
                    "source_worker_request_id": report.worker_request_id,
                    "source_worker_request_ids": source_worker_request_ids,
                    "current_session_id": session_seed.session_id,
                    "current_worker_request_id": request.request_id,
                    "identity_rebound": True,
                },
            )

        pre_engine_custody = bind_custody_to_current_request(
            custody_runtime.build_report(
                session_replay=session_store.replay_session(session_seed.session_id),
                packet=query_session_integration.packet.to_dict(include_text=False),
                integration_report=query_session_integration.to_dict(include_text=False),
                after_engine=False,
            )
        )
        pre_engine_custody_event = custody_runtime.event_for_report(
            pre_engine_custody,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        current_request_store_records = [
            record.to_dict()
            for record in session_store.replay_session(session_seed.session_id).records
            if record.worker_request_id == request.request_id
        ]
        current_request_observation_events = [
            *session_seed_events,
            turn_lifecycle_event,
            foundation_audit_record,
            pre_query_acceptance_event,
            pre_query_lifecycle_event,
            *query_session_integration_events,
            query_handoff_event,
            pre_engine_custody_event,
        ]
        event_flow_runtime = QuerySessionEventFlowRuntime()
        pre_engine_event_flow = event_flow_runtime.build_report(
            current_request_observation_events,
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            expected_engine_stream=False,
            blocked_before_engine=not query_session_integration.ok or not query_handoff.ok or not pre_engine_custody.ok,
        )
        pre_engine_event_flow_event = event_flow_runtime.event_for_report(
            pre_engine_event_flow,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        state_graph_runtime = QuerySessionStateGraphRuntime()
        pre_engine_state_graph = state_graph_runtime.build_report(
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            store_records=current_request_store_records,
            packet=query_session_integration.packet.to_dict(include_text=False),
            integration_report=query_session_integration.to_dict(include_text=False),
            handoff_report=query_handoff.to_dict(),
            custody_report=pre_engine_custody.to_dict(),
            event_flow_report=pre_engine_event_flow.to_dict(),
            events=[
                *current_request_observation_events,
                pre_engine_event_flow_event,
            ],
            blocked_before_engine=not query_session_integration.ok or not query_handoff.ok or not pre_engine_custody.ok,
            engine_stream_expected=False,
        )
        pre_engine_state_graph_event = state_graph_runtime.event_for_report(
            pre_engine_state_graph,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        if (
            not query_session_integration.ok
            or not query_handoff.ok
            or not pre_engine_custody.ok
            or not pre_engine_state_graph.ok
        ):
            disconnect_runtime = QuerySessionDisconnectAuditRuntime()
            pre_engine_disconnect = disconnect_runtime.build_report(
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                constraints=request.constraints,
                packet=query_session_integration.packet.to_dict(include_text=False),
                integration_report=query_session_integration.to_dict(include_text=False),
                handoff_report=query_handoff.to_dict(),
                custody_report=pre_engine_custody.to_dict(),
                event_flow_report=pre_engine_event_flow.to_dict(),
                state_graph_report=pre_engine_state_graph.to_dict(),
                events=[
                    *current_request_observation_events,
                    pre_engine_event_flow_event,
                    pre_engine_state_graph_event,
                ],
            )
            pre_engine_disconnect_event = disconnect_runtime.event_for_report(
                pre_engine_disconnect,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before QueryEngine because query session integration is blocked.",
                error=query_session_integration.first_blocker_code
                or query_handoff.first_blocker_code
                or pre_engine_custody.first_blocker_code
                or pre_engine_state_graph.first_blocker_code
                or pre_engine_disconnect.first_blocker_code
                or "code_worker_query_session_integration_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    **query_plan_metadata,
                    **session_seed_metadata(session_seed),
                    **foundation_audit_metadata(foundation_audit),
                    **session_replay_metadata(replay_plan),
                    **turn_lifecycle_metadata(turn_lifecycle_projection),
                    **session_acceptance_metadata(pre_query_acceptance),
                    **session_lifecycle_metadata(pre_query_lifecycle_report),
                    **query_session_integration_metadata(query_session_integration),
                    **query_handoff_metadata(query_handoff),
                    **query_resume_custody_metadata(pre_engine_custody),
                    **query_event_flow_metadata(pre_engine_event_flow),
                    **query_state_graph_metadata(pre_engine_state_graph),
                    **query_disconnect_metadata(pre_engine_disconnect),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return _custodied_code_worker_run(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    *session_seed_events,
                    *replay_events,
                    turn_lifecycle_event,
                    foundation_audit_record,
                    pre_query_acceptance_event,
                    pre_query_lifecycle_event,
                    *query_session_integration_events,
                    query_handoff_event,
                    pre_engine_custody_event,
                    pre_engine_event_flow_event,
                    pre_engine_state_graph_event,
                    pre_engine_disconnect_event,
                    _worker_result_event(request, worker_result),
                ],
                custody_receipt=custody_receipt,
            )
        query_entry_messages = query_session_integration.packet.request_messages()
        # Browser disclosures are TaskState-owned, read-once external context.
        # They must cross the final QueryEngine boundary in addition to the
        # 02B query-entry packet; ToolSessionBridge consumes WorkerRequest
        # messages for tool turns and is not a model-context handoff.  Restrict
        # this extension to the typed browser marker so existing arbitrary
        # WorkerRequest message behavior and tool pairing stay unchanged.
        browser_external_messages = []
        for request_message in request.messages:
            projected_message = to_jsonable(request_message)
            if not isinstance(projected_message, Mapping):
                continue
            projected_metadata = projected_message.get("metadata")
            projected_metadata = projected_metadata if isinstance(projected_metadata, Mapping) else {}
            source_id = str(
                projected_message.get("browser_context_source_id")
                or projected_metadata.get("browser_context_source_id")
                or ""
            )
            if not source_id.startswith("browser-disclosure:"):
                continue
            browser_external_messages.append(dict(projected_message))
        if browser_external_messages:
            query_entry_messages = [*query_entry_messages, *browser_external_messages]
        query_entry_metadata = query_session_integration.metadata_values()
        query_entry_metadata = {
            **query_entry_metadata,
            "browser_external_context_message_count": str(len(browser_external_messages)),
            "browser_external_context_owner": "M1-S04B-02.BrowserContextTaskIntegrationRuntime",
        }
        restored_runtime_state = None
        if runtime_state_load.found:
            restored_runtime_state = {
                **runtime_state_load.runtime_state,
                "restore_provenance": {
                    "source_session_id": runtime_state_source_session_id,
                    "source_worker_request_id": runtime_state_load.worker_request_id,
                    "target_session_id": session_seed.session_id,
                    "target_worker_request_id": request.request_id,
                    "branch_resume": runtime_state_source_session_id != session_seed.session_id,
                },
            }
            if runtime_state_source_session_id != session_seed.session_id:
                # Branch replay may recover context, but permission authority
                # is never copied to a fresh target session by selector alone.
                restored_runtime_state.pop("permission_runtime", None)
                restored_runtime_state.pop("permission_continuation", None)
                restored_runtime_state.pop("permission_continuation_payloads", None)
                restored_runtime_state["permission_branch_policy"] = {
                    "source_session_id": runtime_state_source_session_id,
                    "target_session_id": session_seed.session_id,
                    "permission_overlay_inherited": False,
                    "permission_continuation_inherited": False,
                    "source_custody_token_present": bool(resume_session_custody_token),
                }
        incoming_skill_state = request.constraints.get("skill_runtime_state")
        if isinstance(incoming_skill_state, Mapping):
            restored_runtime_state = dict(restored_runtime_state or {})
            restored_runtime_state["skill_runtime_state"] = dict(incoming_skill_state)
            restored_runtime_state["invoked_skill_refs"] = [
                dict(item)
                for item in request.constraints.get("invoked_skill_refs") or ()
                if isinstance(item, Mapping)
            ]
        permission_payload_sequence = [
            session_store.replay_session(session_seed.session_id).last_sequence
        ]

        def write_permission_continuation_payload(
            request_id: str,
            payload_locator: str,
            replay_payload: Mapping[str, Any],
        ) -> None:
            receipt = session_store.append_permission_continuation_payload(
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                run_id=request.run_id,
                task_id=request.task_id,
                request_id=request_id,
                payload_locator=payload_locator,
                replay_payload=replay_payload,
                expected_previous_sequence=permission_payload_sequence[0],
            )
            if not receipt.ok:
                raise RuntimeError(
                    receipt.error or "permission_continuation_payload_write_failed"
                )
            permission_payload_sequence[0] = receipt.last_sequence

        def tombstone_permission_continuation_payload(
            request_id: str,
            payload_locator: str,
            reason: str,
        ) -> None:
            receipt = session_store.append_permission_continuation_tombstone(
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                run_id=request.run_id,
                task_id=request.task_id,
                request_id=request_id,
                payload_locator=payload_locator,
                reason=reason,
                expected_previous_sequence=permission_payload_sequence[0],
            )
            if not receipt.ok:
                raise RuntimeError(
                    receipt.error or "permission_continuation_tombstone_write_failed"
                )
            permission_payload_sequence[0] = receipt.last_sequence

        engine = self.query_engine_factory(
            execution_context,
            ClaudeQueryEngineConfig(
                max_turns=_optional_int(request.constraints.get("max_turns")),
                max_tool_result_chars=_positive_int(
                    request.constraints.get("tool_result_budget_chars"),
                    default=8000,
                ),
                max_query_context_chars=_positive_int(
                    request.constraints.get("query_context_budget_chars"),
                    default=32000,
                ),
                continue_on_error=request.constraints.get("continue_on_error") is True,
                runtime_contracts=self.runtime_contracts,
                max_read_only_concurrency=_positive_int(
                    request.constraints.get("max_read_only_concurrency"),
                    default=_contract_default_concurrency(query_contract),
                ),
                emit_tool_use_summaries=request.constraints.get("emit_tool_use_summaries") is not False,
                control_commands=request.constraints.get("control_commands") or (),
                project_root=self.project_root,
                session_seed=session_seed.to_dict(include_text=False),
                context_snapshot=context_snapshot.to_dict(include_text=True),
                preprocessed_messages=query_entry_messages,
                session_foundation_metadata={
                    **session_seed.metadata_values(),
                    **query_entry_metadata,
                    **custody_receipt.metadata(),
                },
                max_turn_tool_result_chars=_optional_int(request.constraints.get("turn_tool_result_budget_chars")),
                disable_tool_registry_runtime=request.constraints.get("disable_tool_registry_runtime") is True,
                disable_tool_execution_runtime=request.constraints.get("disable_tool_execution_runtime") is True,
                disable_tool_result_budget_runtime=request.constraints.get("disable_tool_result_budget_runtime") is True,
                disable_tool_permission_handoff_runtime=request.constraints.get("disable_tool_permission_handoff_runtime") is True,
                disable_tool_permission_runtime=request.constraints.get("disable_tool_permission_runtime") is True,
                disable_permission_rule_store=request.constraints.get("disable_permission_rule_store") is True,
                disable_permission_request_queue=request.constraints.get("disable_permission_request_queue") is True,
                disable_permission_decision_log=request.constraints.get("disable_permission_decision_log") is True,
                permission_mode=effective_permission_mode,
                permission_approval_ttl_seconds=min(
                    300.0,
                    _positive_float(request.constraints.get("permission_approval_ttl_seconds"), default=300.0),
                ),
                permission_execution_grant_ttl_seconds=min(
                    30.0,
                    _positive_float(request.constraints.get("permission_execution_grant_ttl_seconds"), default=30.0),
                ),
                permission_headless=(
                    request.constraints.get("permission_headless") is True
                    or effective_permission_mode == "sealed"
                ),
                permission_interactive=(
                    request.constraints.get("permission_interactive") is not False
                    and request.constraints.get("permission_headless") is not True
                    and effective_permission_mode != "sealed"
                ),
                permission_bypass_available=self.permission_bypass_available,
                permission_auto_available=self.permission_auto_available,
                permission_state_path=self.permission_state_path,
                permission_extension_registry=self.permission_extension_registry,
                permission_continuation_payload_writer=write_permission_continuation_payload,
                permission_continuation_payload_tombstoner=tombstone_permission_continuation_payload,
                disable_runtime_budget_state=request.constraints.get("disable_runtime_budget_state") is True,
                disable_compact_restore_runtime=request.constraints.get("disable_compact_restore_runtime") is True,
                disable_model_stream_runtime=request.constraints.get("disable_model_stream_runtime") is True,
                disable_api_retry_runtime=request.constraints.get("disable_api_retry_runtime") is True,
                disable_codeworker_api_foundation_runtime=request.constraints.get("disable_codeworker_api_foundation_runtime") is True,
                disable_context_security_runtime=request.constraints.get("disable_context_security_runtime") is True,
                disable_restore_integration_runtime=request.constraints.get("disable_restore_integration_runtime") is True,
                model_name=str(request.constraints.get("model_name") or "zyra-local-code-model"),
                model_input_token_limit=_positive_int(
                    request.constraints.get("model_input_token_limit"),
                    default=200000,
                ),
                model_output_token_limit=_positive_int(
                    request.constraints.get("model_output_token_limit"),
                    default=8192,
                ),
                api_retry_max_attempts=_positive_int(
                    request.constraints.get("api_retry_max_attempts"),
                    default=3,
                ),
                api_retry_fallback_models=tuple(
                    str(item).strip()
                    for item in (
                        request.constraints.get("api_retry_fallback_models")
                        if isinstance(request.constraints.get("api_retry_fallback_models"), list)
                        else str(request.constraints.get("api_retry_fallback_models") or "zyra-local-fallback").split(",")
                    )
                    if str(item).strip()
                ),
                runtime_constraints=request.constraints,
                session_bridge_report=tool_session_bridge,
                restored_runtime_state=restored_runtime_state,
            ),
        )
        loop_result = engine.run(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            worker_request_id=request.request_id,
            turns=query_turns,
            request_messages=query_entry_messages,
            request_metadata={**request.metadata, **session_seed.metadata_values(), **query_entry_metadata},
        )
        def state_payload(value: Any) -> dict[str, Any]:
            if isinstance(value, Mapping):
                return dict(value)
            to_dict = getattr(value, "to_dict", None)
            if callable(to_dict):
                projected = to_dict()
                return dict(projected) if isinstance(projected, Mapping) else {}
            return {}

        session_snapshot_payload = to_jsonable(loop_result.session_snapshot)
        snapshot_runtime_state = (
            session_snapshot_payload.get("runtime_state")
            if isinstance(session_snapshot_payload, Mapping)
            else None
        )
        if isinstance(snapshot_runtime_state, Mapping):
            runtime_state_checkpoint = dict(snapshot_runtime_state)
        else:
            raw_runtime_state = getattr(loop_result, "runtime_state_checkpoint", None)
            if not isinstance(raw_runtime_state, Mapping):
                metadata_runtime_state = loop_result.metadata.get("runtime_state_checkpoint")
                raw_runtime_state = metadata_runtime_state if isinstance(metadata_runtime_state, Mapping) else None
            if isinstance(raw_runtime_state, Mapping):
                runtime_state_checkpoint = dict(raw_runtime_state)
            else:
                runtime_budget_state = {}
                for candidate in (
                    getattr(loop_result, "runtime_budget_state", None),
                    getattr(loop_result, "runtime_budget_snapshot", None),
                    getattr(loop_result, "runtime_budget_report", None),
                ):
                    runtime_budget_state = state_payload(candidate)
                    if runtime_budget_state:
                        break
                context_state = {}
                for candidate in (
                    getattr(loop_result, "context_window_state", None),
                    getattr(loop_result, "context_window_snapshot", None),
                    getattr(loop_result, "context_report", None),
                ):
                    context_state = state_payload(candidate)
                    if context_state:
                        break
                pending_restore_state = {}
                for candidate in (
                    getattr(loop_result, "pending_restore_contract", None),
                    getattr(loop_result, "compact_restore_state", None),
                    getattr(loop_result, "compact_restore_report", None),
                ):
                    pending_restore_state = state_payload(candidate)
                    if pending_restore_state:
                        break
                runtime_state_checkpoint = {
                    "schema_version": 1,
                    "query_session_id": str(loop_result.metadata.get("query_session_id") or session_seed.session_id),
                    "query_session_resume_token": str(loop_result.metadata.get("query_session_resume_token") or ""),
                    "session_snapshot": session_snapshot_payload,
                }
                if runtime_budget_state:
                    runtime_state_checkpoint["runtime_budget_state"] = runtime_budget_state
                if context_state:
                    runtime_state_checkpoint["context_window_state"] = context_state
                if pending_restore_state:
                    runtime_state_checkpoint["pending_restore_contract"] = pending_restore_state
        if self.mcp_runtime is not None and not typescript_capability_owner:
            runtime_state_checkpoint["mcp_runtime"] = self.mcp_runtime.prepare_session_checkpoint(
                session_store,
                session_id=session_seed.session_id,
                run_id=request.run_id,
                task_id=request.task_id,
                worker_request_id=request.request_id,
                node_id=str(request.node_id or ""),
            )
        causal_event_ids = [
            str(getattr(event, "event_id", "") or getattr(event, "id", ""))
            for event in loop_result.event_records
            if getattr(event, "event_id", "") or getattr(event, "id", "")
        ]
        runtime_state_checkpoint_receipt = session_store.append_runtime_state(
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            runtime_state=runtime_state_checkpoint,
            causal_receipt={
                "worker_request_id": request.request_id,
                "query_session_id": str(loop_result.metadata.get("query_session_id") or session_seed.session_id),
                "query_session_resume_token": str(loop_result.metadata.get("query_session_resume_token") or ""),
                "event_ids": causal_event_ids,
                "event_count": len(loop_result.event_records),
                "session_snapshot_present": loop_result.session_snapshot is not None,
            },
            expected_previous_sequence=permission_payload_sequence[0],
        )
        runtime_state_checkpoint_metadata = {
            "runtime_state_checkpoint_ok": str(runtime_state_checkpoint_receipt.ok).lower(),
            "runtime_state_checkpoint_path": runtime_state_checkpoint_receipt.path,
            "runtime_state_checkpoint_sequence": str(runtime_state_checkpoint_receipt.last_sequence),
            "runtime_state_checkpoint_record_count": str(runtime_state_checkpoint_receipt.record_count),
            "runtime_state_checkpoint_error": runtime_state_checkpoint_receipt.error,
            "runtime_state_checkpoint_causal_event_count": str(len(causal_event_ids)),
        }
        session_store.mark_query_engine_attached(
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            query_session_id=str(loop_result.metadata.get("query_session_id") or session_seed.session_id),
            resume_token=str(loop_result.metadata.get("query_session_resume_token") or ""),
        )
        final_custody = bind_custody_to_current_request(
            custody_runtime.build_report(
                session_replay=session_store.replay_session(session_seed.session_id),
                packet=query_session_integration.packet.to_dict(include_text=False),
                integration_report=query_session_integration.to_dict(include_text=False),
                after_engine=True,
            )
        )
        final_custody_event = custody_runtime.event_for_report(
            final_custody,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        foundation_audit = foundation_auditor.audit_seed(
            session_seed,
            store=session_store,
            events=[
                *session_seed_events,
                *replay_events,
                turn_lifecycle_event,
                *query_session_integration_events,
                query_handoff_event,
                final_custody_event,
                *loop_result.event_records,
            ],
        )
        foundation_audit_record = foundation_audit_event(foundation_audit)
        transcript_mapping = TranscriptEventMapper().map_snapshot(loop_result.session_snapshot)
        transcript_mapping_event = TranscriptEventMapper().event_for_report(transcript_mapping)
        final_acceptance = acceptance_runtime.evaluate(
            seed=session_seed,
            foundation_audit=foundation_audit,
            turn_lifecycle=turn_lifecycle_projection,
            replay_plan=replay_plan,
            transcript_mapping=transcript_mapping,
            require_transcript=True,
        )
        final_acceptance_event = acceptance_runtime.event_for_report(
            final_acceptance,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        lifecycle_events = [
            *session_seed_events,
            *replay_events,
            turn_lifecycle_event,
            foundation_audit_record,
            *query_session_integration_events,
            query_handoff_event,
            final_custody_event,
            *loop_result.event_records,
            transcript_mapping_event,
            final_acceptance_event,
        ]
        lifecycle_report = lifecycle_runtime.build_report(
            lifecycle_events,
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            require_query_engine=True,
            require_transcript=True,
        )
        lifecycle_event = lifecycle_runtime.event_for_report(
            lifecycle_report,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        final_event_flow = event_flow_runtime.build_report(
            [*lifecycle_events, lifecycle_event],
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            expected_engine_stream=True,
            blocked_before_engine=False,
        )
        final_event_flow_event = event_flow_runtime.event_for_report(
            final_event_flow,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        final_state_graph = state_graph_runtime.build_report(
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            store_records=[record.to_dict() for record in session_store.replay_session(session_seed.session_id).records],
            packet=query_session_integration.packet.to_dict(include_text=False),
            integration_report=query_session_integration.to_dict(include_text=False),
            handoff_report=query_handoff.to_dict(),
            custody_report=final_custody.to_dict(),
            event_flow_report=final_event_flow.to_dict(),
            events=[*lifecycle_events, lifecycle_event, final_event_flow_event],
            blocked_before_engine=False,
            engine_stream_expected=True,
        )
        final_state_graph_event = state_graph_runtime.event_for_report(
            final_state_graph,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        disconnect_runtime = QuerySessionDisconnectAuditRuntime()
        final_disconnect = disconnect_runtime.build_report(
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            constraints=request.constraints,
            packet=query_session_integration.packet.to_dict(include_text=False),
            integration_report=query_session_integration.to_dict(include_text=False),
            handoff_report=query_handoff.to_dict(),
            custody_report=final_custody.to_dict(),
            event_flow_report=final_event_flow.to_dict(),
            state_graph_report=final_state_graph.to_dict(),
            events=[*lifecycle_events, lifecycle_event, final_event_flow_event, final_state_graph_event],
        )
        final_disconnect_event = disconnect_runtime.event_for_report(
            final_disconnect,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
        )
        artifacts = list(loop_result.artifacts)
        step_summaries = list(loop_result.step_summaries)

        trace_artifact = execution_context.artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=_trace_markdown(
                request,
                runtime_health,
                runtime_inventory,
                query_contract,
                session_contract,
                tool_loop_contract,
                integration_report,
                runtime_context_report,
                source_graph_audit,
                worker_gate,
                query_session_integration,
                query_handoff,
                final_custody,
                final_event_flow,
                final_state_graph,
                final_disconnect,
                step_summaries,
                loop_result,
                sidecar_contracts_used=use_sidecar_contracts,
            ),
            title=f"CodeWorker trace {request.request_id}",
            kind=ArtifactKind.TRACE,
            extension=".md",
            producer_node_id=request.node_id,
        )
        artifacts.append(trace_artifact)

        summary = "CodeWorkerRuntime completed structured tool plan."
        if not loop_result.ok:
            summary = "CodeWorkerRuntime stopped on a failed tool step."
        elif not runtime_state_checkpoint_receipt.ok:
            summary = "CodeWorkerRuntime completed the tool loop but failed to persist its runtime-state checkpoint."
        elif not final_acceptance.ok or not lifecycle_report.ok:
            summary = "CodeWorkerRuntime completed the tool loop but failed the session lifecycle gate."
        elif not final_event_flow.ok:
            summary = "CodeWorkerRuntime completed the tool loop but failed the query event-flow gate."
        elif not final_custody.ok:
            summary = "CodeWorkerRuntime completed the tool loop but failed the query custody gate."
        elif not final_state_graph.ok:
            summary = "CodeWorkerRuntime completed the tool loop but failed the query state graph gate."
        elif not final_disconnect.ok:
            summary = "CodeWorkerRuntime completed the tool loop but failed the query disconnect audit gate."

        if loop_result.ok and skill_projection is not None:
            skill_projection.finalize_successful_query(
                evidence_refs=tuple(
                    f"event://{event.event_id}" for event in loop_result.event_records
                ),
                artifact_refs=tuple(
                    f"artifact://{artifact.artifact_id}" for artifact in artifacts
                ),
            )
        if skill_projection is not None:
            skill_projection_snapshot = skill_projection.snapshot()
            skill_projection_events = [
                EventRecord(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    event_type=EventType.SKILL_INVOKED,
                    payload={
                        **event.to_dict(),
                        "phase": event.kind,
                        "owner_unit": "M1-03C",
                        "skill_session_checkpoint": skill_projection_snapshot.state_snapshot,
                        "skill_compact_references": list(skill_projection_snapshot.compact_references),
                        "skill_outcome_projections": list(skill_projection_snapshot.outcome_projections),
                    },
                )
                for event in skill_projection.events()
            ]
            skill_projection_metadata = {
                "skill_tool_projection": "active",
                "skill_tool_projection_id": skill_projection_snapshot.projection_id,
                "skill_registry_generation": str(skill_projection_snapshot.registry_generation),
                "skill_invocation_count": str(len(skill_projection_snapshot.invocation_ids)),
                "skill_projection_event_count": str(skill_projection_snapshot.event_count),
                "skill_projection_snapshot_digest": skill_projection_snapshot.digest,
            }
        elif typescript_capability_owner:
            skill_projection_events = []
            skill_projection_metadata = {
                "skill_tool_projection": "typescript_runtime_owner",
                "skill_tool_projection_id": "",
                "skill_registry_generation": "0",
                "skill_invocation_count": "0",
                "skill_projection_event_count": "0",
                "skill_projection_snapshot_digest": "",
                "canonical_skill_owner": "typescript",
                "python_skill_projection_used": "false",
            }
        else:
            # A server-signed child scope may deliberately disable SkillTool
            # projection.  This is a real isolation mode, not a failed open:
            # the child retains only its already-narrowed executable registry.
            skill_projection_events = []
            skill_projection_metadata = {
                "skill_tool_projection": "disabled_by_signed_child_scope",
                "skill_tool_projection_id": "",
                "skill_registry_generation": "0",
                "skill_invocation_count": "0",
                "skill_projection_event_count": "0",
                "skill_projection_snapshot_digest": "",
            }
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=loop_result.ok
            and runtime_state_checkpoint_receipt.ok
            and final_acceptance.ok
            and lifecycle_report.ok
            and final_event_flow.ok
            and final_custody.ok
            and final_state_graph.ok
            and final_disconnect.ok,
            summary=summary,
            artifacts=artifacts,
            events=[
                to_jsonable(event)
                for event in [
                    *session_seed_events,
                    *replay_events,
                    turn_lifecycle_event,
                    foundation_audit_record,
                    *query_session_integration_events,
                    query_handoff_event,
                    final_custody_event,
                    *loop_result.event_records,
                    transcript_mapping_event,
                    final_acceptance_event,
                    lifecycle_event,
                    final_event_flow_event,
                    final_state_graph_event,
                    final_disconnect_event,
                    *skill_projection_events,
                    *mcp_skill_discovery_events,
                ]
            ],
            error=None
            if loop_result.ok
            and runtime_state_checkpoint_receipt.ok
            and final_acceptance.ok
            and lifecycle_report.ok
            and final_event_flow.ok
            and final_custody.ok
            and final_state_graph.ok
            and final_disconnect.ok
            else runtime_state_checkpoint_receipt.error
            or loop_result.stopped_reason
            or (final_event_flow.first_blocker_code if not final_event_flow.ok else "")
            or (final_custody.first_blocker_code if not final_custody.ok else "")
            or (final_state_graph.first_blocker_code if not final_state_graph.ok else "")
            or (final_disconnect.first_blocker_code if not final_disconnect.ok else "")
            or "code_worker_session_lifecycle_failed",
            metadata={
                **self.runtime_contracts.metadata(),
                **_sidecar_metadata(runtime_health, used=use_sidecar_contracts),
                **_inventory_metadata(runtime_inventory),
                **_contract_metadata(query_contract),
                **_session_contract_metadata(session_contract),
                **_tool_loop_contract_metadata(tool_loop_contract),
                **integration_report.metadata(),
                **runtime_context_report.metadata(),
                **source_graph_audit.metadata(),
                **worker_gate.metadata(),
                **query_plan_metadata,
                **session_seed_metadata(session_seed),
                **runtime_state_load.metadata_values(),
                **runtime_state_checkpoint_metadata,
                **foundation_audit_metadata(foundation_audit),
                **session_replay_metadata(replay_plan),
                **turn_lifecycle_metadata(turn_lifecycle_projection),
                **query_session_integration_metadata(query_session_integration),
                **query_handoff_metadata(query_handoff),
                **query_resume_custody_metadata(final_custody),
                **query_event_flow_metadata(final_event_flow),
                **query_state_graph_metadata(final_state_graph),
                **query_disconnect_metadata(final_disconnect),
                **transcript_mapping_metadata(transcript_mapping),
                **session_acceptance_metadata(final_acceptance),
                **session_lifecycle_metadata(lifecycle_report),
                **loop_result.metadata,
                **custody_receipt.metadata(),
                "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                "query_turns": str(loop_result.turn_count),
                "tool_steps": str(loop_result.tool_call_count),
                "context_compactions": str(loop_result.context_compaction_count),
                "trace_artifact_id": trace_artifact.artifact_id,
                "query_session_checkpoint_ready": str(bool(loop_result.session_snapshot)).lower(),
                **skill_projection_metadata,
                "mcp_skill_source_count": str(len(mcp_skill_sources)),
                "mcp_skill_discovery_id": (
                    mcp_skill_discovery_receipt.discovery_id
                    if mcp_skill_discovery_receipt is not None
                    else ""
                ),
            },
        )
        mcp_events = (
            list(self.mcp_runtime.drain_events(run_id=request.run_id, task_id=request.task_id))
            if self.mcp_runtime is not None and not typescript_capability_owner
            else []
        )
        return _custodied_code_worker_run(
            worker_result=worker_result,
            event_records=[
                *integration_events,
                *runtime_context_events,
                *source_graph_audit_events,
                worker_gate_event,
                *session_seed_events,
                *replay_events,
                turn_lifecycle_event,
                foundation_audit_record,
                *query_session_integration_events,
                query_handoff_event,
                final_custody_event,
                *loop_result.event_records,
                transcript_mapping_event,
                final_acceptance_event,
                lifecycle_event,
                final_event_flow_event,
                final_state_graph_event,
                final_disconnect_event,
                *skill_projection_events,
                *mcp_skill_discovery_events,
                *mcp_events,
                _worker_result_event(request, worker_result),
            ],
            custody_receipt=custody_receipt,
        )


def _custodied_code_worker_run(
    *,
    worker_result: WorkerResult,
    event_records: list[EventRecord],
    custody_receipt: PermissionSessionCustodyReceipt,
) -> CodeWorkerRun:
    """Build the private handoff without projecting bearer material elsewhere."""

    return CodeWorkerRun(
        worker_result=worker_result,
        event_records=event_records,
        session_custody_token=(
            custody_receipt.token if custody_receipt.created else ""
        ),
        session_id=custody_receipt.session_id,
        session_custody_id=custody_receipt.custody_id,
        session_custody_fingerprint=custody_receipt.custody_fingerprint,
        session_custody_created=custody_receipt.created,
    )


def _worker_result_event(request: WorkerRequest, result: WorkerResult) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "worker_request": to_jsonable(request),
            "worker_result": to_jsonable(result),
        },
    )


def _sidecar_metadata(health: dict[str, Any], *, used: bool) -> dict[str, str]:
    vendor = health.get("vendor") if isinstance(health.get("vendor"), dict) else {}
    return {
        "sidecar_runtime": str(health.get("runtime") or "") if used else "",
        "sidecar_worker": str(health.get("worker") or "") if used else "",
        "sidecar_contracts_used": str(used).lower(),
        "vendor_complete": str(vendor.get("complete") is True).lower(),
    }


def _inventory_metadata(inventory: dict[str, Any]) -> dict[str, str]:
    tool_runtime = inventory.get("toolRuntime") if isinstance(inventory.get("toolRuntime"), dict) else {}
    command_runtime = inventory.get("commandRuntime") if isinstance(inventory.get("commandRuntime"), dict) else {}
    return {
        "inventory_source": str(inventory.get("source") or ""),
        "inventory_base_tool_count": str(tool_runtime.get("baseToolCount") or 0),
        "inventory_command_count": str(command_runtime.get("commandCount") or 0),
    }


def _contract_metadata(contract: dict[str, Any]) -> dict[str, str]:
    orchestration = contract.get("toolOrchestration") if isinstance(contract.get("toolOrchestration"), dict) else {}
    budgets = contract.get("budgets") if isinstance(contract.get("budgets"), dict) else {}
    lifecycle_events = contract.get("lifecycleEvents") if isinstance(contract.get("lifecycleEvents"), list) else []
    return {
        "query_contract_source": str(contract.get("source") or ""),
        "query_contract_lifecycle_count": str(len(lifecycle_events)),
        "query_contract_read_only_concurrent": str(orchestration.get("readOnlyConcurrent") is True).lower(),
        "query_contract_write_serial": str(orchestration.get("writeSerial") is True).lower(),
        "query_contract_tool_result_budget": str(budgets.get("toolResultBudget") is True).lower(),
        "query_contract_reactive_compact": str(budgets.get("reactiveCompact") is True).lower(),
    }


def _session_contract_metadata(contract: dict[str, Any]) -> dict[str, str]:
    persistence = contract.get("transcriptPersistence") if isinstance(contract.get("transcriptPersistence"), dict) else {}
    recovery = contract.get("resumeRecovery") if isinstance(contract.get("resumeRecovery"), dict) else {}
    stream = contract.get("streamRuntime") if isinstance(contract.get("streamRuntime"), dict) else {}
    bridge = contract.get("bridgeSessionRuntime") if isinstance(contract.get("bridgeSessionRuntime"), dict) else {}
    foundation = contract.get("preQueryFoundation") if isinstance(contract.get("preQueryFoundation"), dict) else {}
    retry_matrix = stream.get("retryMatrix") if isinstance(stream.get("retryMatrix"), dict) else {}
    return {
        "session_contract_source": str(contract.get("source") or ""),
        "session_contract_owner_unit": str(contract.get("ownerUnit") or ""),
        "session_contract_inventory_exists": str(contract.get("inventoryExists") is True).lower(),
        "session_contract_foundation_owner_unit": str(foundation.get("ownerUnit") or ""),
        "session_contract_has_input_processor": str(foundation.get("hasInputProcessor") is True).lower(),
        "session_contract_has_context_assembly": str(foundation.get("hasContextAssemblyRuntime") is True).lower(),
        "session_contract_has_code_worker_session_store": str(foundation.get("hasCodeWorkerSessionStore") is True).lower(),
        "session_contract_seed_required": str(foundation.get("sessionSeedRequiredForDefaultPath") is True).lower(),
        "session_contract_append_only_jsonl": str(persistence.get("appendOnlyJsonl") is True).lower(),
        "session_contract_parent_uuid_chain": str(persistence.get("parentUuidChain") is True).lower(),
        "session_contract_lite_read_window": str(persistence.get("liteReadWindowBytes") or ""),
        "session_contract_resume_chain": str(recovery.get("hasChainTraversal") is True).lower(),
        "session_contract_interruption_detection": str(recovery.get("hasInterruptionDetection") is True).lower(),
        "session_contract_raw_sse": str(stream.get("rawSseStateMachine") is True).lower(),
        "session_contract_unattended_retry": str(retry_matrix.get("unattendedRetry") is True).lower(),
        "session_contract_bridge_runner": str(bridge.get("hasSessionRunner") is True).lower(),
    }


def _tool_loop_contract_metadata(contract: dict[str, Any]) -> dict[str, str]:
    interface = contract.get("toolInterface") if isinstance(contract.get("toolInterface"), dict) else {}
    execution = contract.get("executionPipeline") if isinstance(contract.get("executionPipeline"), dict) else {}
    scheduling = contract.get("scheduling") if isinstance(contract.get("scheduling"), dict) else {}
    budget = contract.get("resultBudget") if isinstance(contract.get("resultBudget"), dict) else {}
    shell = contract.get("shellRuntime") if isinstance(contract.get("shellRuntime"), dict) else {}
    sandbox = contract.get("sandboxRuntime") if isinstance(contract.get("sandboxRuntime"), dict) else {}
    failures = contract.get("failureSignals") if isinstance(contract.get("failureSignals"), dict) else {}
    source_files = contract.get("sourceFiles") if isinstance(contract.get("sourceFiles"), list) else []
    return {
        "tool_loop_contract_source": str(contract.get("source") or ""),
        "tool_loop_contract_owner_unit": str(contract.get("ownerUnit") or ""),
        "tool_loop_contract_inventory_exists": str(contract.get("inventoryExists") is True).lower(),
        "tool_loop_contract_source_count": str(len(source_files)),
        "tool_loop_contract_has_input_schema": str(interface.get("hasInputSchema") is True).lower(),
        "tool_loop_contract_permission_gate": str(execution.get("hasPermissionGate") is True).lower(),
        "tool_loop_contract_result_mapping": str(execution.get("hasToolResultBlockMapping") is True).lower(),
        "tool_loop_contract_read_only_concurrent": str(scheduling.get("readOnlyConcurrent") is True).lower(),
        "tool_loop_contract_write_serial": str(scheduling.get("writeSerial") is True).lower(),
        "tool_loop_contract_budget_externalization": str(budget.get("hasLargeResultExternalization") is True or budget.get("hasPersistedOutputTag") is True).lower(),
        "tool_loop_contract_shell_lifecycle": str(shell.get("hasProcessLifecycle") is True).lower(),
        "tool_loop_contract_sandbox_adapter": str(sandbox.get("hasSandboxAdapter") is True).lower(),
        "tool_loop_contract_denial_limits": str(failures.get("hasDenialLimits") is True).lower(),
    }


def _contract_default_concurrency(contract: dict[str, Any]) -> int:
    orchestration = contract.get("toolOrchestration") if isinstance(contract.get("toolOrchestration"), dict) else {}
    return _positive_int(orchestration.get("maxConcurrencyDefault"), default=10)


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _permission_mode(value: Any) -> str:
    text = str(value or "default").strip()
    aliases = {
        "workspace": "default",
        "accept_edits": "acceptEdits",
        "dont_ask": "dontAsk",
        "bypass": "bypassPermissions",
        "bypass_permissions": "bypassPermissions",
    }
    return aliases.get(text, text)


def _bounded_permission_mode(
    value: Any,
    *,
    bypass_available: bool,
    auto_available: bool,
    accept_edits_available: bool,
) -> str:
    requested = _permission_mode(value)
    if requested == "bypassPermissions" and not bypass_available:
        return "default"
    if requested == "auto" and not auto_available:
        return "default"
    if requested == "acceptEdits" and not accept_edits_available:
        return "default"
    allowed = {"default", "plan", "dontAsk", "sealed", "acceptEdits", "auto", "bypassPermissions"}
    return requested if requested in allowed else "default"


def _permission_mode_from_state_owner(
    state_store: PermissionStateStore,
    *,
    session_id: str,
    restored_runtime_state: Mapping[str, Any],
    fallback: Any,
    bypass_available: bool,
    auto_available: bool,
    accept_edits_available: bool,
) -> str:
    """Prefer durable session owners and only then use the request fallback."""

    selected: Any = fallback
    restored_permission = restored_runtime_state.get("permission_runtime")
    if isinstance(restored_permission, Mapping):
        restored_mode = restored_permission.get("mode")
        if isinstance(restored_mode, Mapping) and restored_mode.get("mode"):
            selected = restored_mode.get("mode")

    state = state_store.read_state()
    integration = state.get("metadata", {}).get("permission_integration", {})
    modes = integration.get("session_modes", {}) if isinstance(integration, Mapping) else {}
    persisted = modes.get(session_id) if isinstance(modes, Mapping) else None
    if isinstance(persisted, Mapping) and persisted.get("mode"):
        selected = persisted.get("mode")

    return _bounded_permission_mode(
        selected,
        bypass_available=bypass_available,
        auto_available=auto_available,
        accept_edits_available=accept_edits_available,
    )


def _bind_permission_tool_use_identity(
    query_turns: list[list[dict[str, Any]]],
    constraints: Mapping[str, Any],
) -> list[list[dict[str, Any]]]:
    """Retain caller-visible tool-use ids across an approved exact replay.

    The query planner owns generated step ids, while permission continuations
    bind the provider/tool-use id.  This overlay only preserves an explicitly
    supplied id; it does not grant permission and duplicate ids remain subject
    to the normal settlement/permission guards.
    """

    raw_turns: list[Any]
    raw_query_turns = constraints.get("query_turns")
    if isinstance(raw_query_turns, list):
        raw_turns = list(raw_query_turns)
    else:
        raw_tool_plan = constraints.get("tool_plan")
        raw_turns = [raw_tool_plan] if isinstance(raw_tool_plan, list) else []

    bound: list[list[dict[str, Any]]] = []
    for turn_index, generated_turn in enumerate(query_turns):
        raw_turn = raw_turns[turn_index] if turn_index < len(raw_turns) else []
        if isinstance(raw_turn, Mapping):
            raw_steps = (
                raw_turn.get("tool_calls")
                or raw_turn.get("steps")
                or raw_turn.get("tool_plan")
                or []
            )
        else:
            raw_steps = raw_turn
        if not isinstance(raw_steps, list):
            raw_steps = []

        output_turn: list[dict[str, Any]] = []
        for step_index, generated_step in enumerate(generated_turn):
            output_step = dict(generated_step)
            raw_step = raw_steps[step_index] if step_index < len(raw_steps) else None
            if isinstance(raw_step, Mapping):
                explicit_id = str(
                    raw_step.get("tool_call_id") or raw_step.get("id") or ""
                ).strip()
                if explicit_id:
                    output_step["tool_call_id"] = explicit_id
            output_turn.append(output_step)
        bound.append(output_turn)
    return bound


def _trace_markdown(
    request: WorkerRequest,
    runtime_health: dict[str, Any],
    runtime_inventory: dict[str, Any],
    query_contract: dict[str, Any],
    session_contract: dict[str, Any],
    tool_loop_contract: dict[str, Any],
    integration_report: Any,
    runtime_context_report: Any,
    source_graph_audit: Any,
    worker_gate: Any,
    query_session_integration: Any,
    query_handoff: Any,
    query_custody: Any,
    query_event_flow: Any,
    query_state_graph: Any,
    query_disconnect: Any,
    step_summaries: list[str],
    loop_result: Any,
    *,
    sidecar_contracts_used: bool,
) -> str:
    vendor = runtime_health.get("vendor") if isinstance(runtime_health.get("vendor"), dict) else {}
    modules = vendor.get("modules") if isinstance(vendor.get("modules"), list) else []
    tool_runtime = runtime_inventory.get("toolRuntime") if isinstance(runtime_inventory.get("toolRuntime"), dict) else {}
    command_runtime = (
        runtime_inventory.get("commandRuntime") if isinstance(runtime_inventory.get("commandRuntime"), dict) else {}
    )
    orchestration = (
        query_contract.get("toolOrchestration")
        if isinstance(query_contract.get("toolOrchestration"), dict)
        else {}
    )
    source_files = query_contract.get("sourceFiles")
    if not isinstance(source_files, list):
        source_files = []
    session_source_files = session_contract.get("sourceFiles")
    if not isinstance(session_source_files, list):
        session_source_files = []
    session_persistence = (
        session_contract.get("transcriptPersistence")
        if isinstance(session_contract.get("transcriptPersistence"), dict)
        else {}
    )
    session_recovery = (
        session_contract.get("resumeRecovery")
        if isinstance(session_contract.get("resumeRecovery"), dict)
        else {}
    )
    session_foundation = (
        session_contract.get("preQueryFoundation")
        if isinstance(session_contract.get("preQueryFoundation"), dict)
        else {}
    )
    tool_loop_scheduling = (
        tool_loop_contract.get("scheduling")
        if isinstance(tool_loop_contract.get("scheduling"), dict)
        else {}
    )
    tool_loop_budget = (
        tool_loop_contract.get("resultBudget")
        if isinstance(tool_loop_contract.get("resultBudget"), dict)
        else {}
    )
    tool_loop_shell = (
        tool_loop_contract.get("shellRuntime")
        if isinstance(tool_loop_contract.get("shellRuntime"), dict)
        else {}
    )
    tool_loop_sandbox = (
        tool_loop_contract.get("sandboxRuntime")
        if isinstance(tool_loop_contract.get("sandboxRuntime"), dict)
        else {}
    )
    tool_loop_source_files = tool_loop_contract.get("sourceFiles")
    if not isinstance(tool_loop_source_files, list):
        tool_loop_source_files = []
    high_value_commands = command_runtime.get("highValueCommandPaths")
    if not isinstance(high_value_commands, list):
        high_value_commands = []
    module_lines = [
        f"- {module.get('name')}: {'complete' if module.get('complete') else 'missing'}"
        for module in modules
        if isinstance(module, dict)
    ]
    return "\n".join(
        [
            "# CodeWorker Runtime Trace",
            "",
            f"- request_id: `{request.request_id}`",
            f"- worker_name: `{request.worker_name}`",
            f"- ok: `{str(loop_result.ok).lower()}`",
            f"- query_loop: `{loop_result.metadata.get('loop', '')}`",
            f"- runtime_source: `{runtime_health.get('source', '')}`",
            f"- sidecar_contracts_used: `{str(sidecar_contracts_used).lower()}`",
            f"- query_contract_source: `{query_contract.get('source', '')}`",
            f"- tool_orchestration_source: `{orchestration.get('sourcePath', '')}`",
            f"- read_only_concurrent: `{str(orchestration.get('readOnlyConcurrent') is True).lower()}`",
            f"- write_serial: `{str(orchestration.get('writeSerial') is True).lower()}`",
            f"- max_read_only_concurrency: `{loop_result.metadata.get('max_read_only_concurrency', '')}`",
            f"- query_turns: `{loop_result.turn_count}`",
            f"- tool_call_count: `{loop_result.tool_call_count}`",
            f"- context_compaction_count: `{loop_result.context_compaction_count}`",
            f"- stopped_reason: `{loop_result.stopped_reason or ''}`",
            f"- tool_result_budget_chars: `{loop_result.metadata.get('tool_result_budget_chars', '')}`",
            f"- query_context_budget_chars: `{loop_result.metadata.get('query_context_budget_chars', '')}`",
            f"- query_session_id: `{loop_result.metadata.get('query_session_id', '')}`",
            f"- query_session_resume_token: `{loop_result.metadata.get('query_session_resume_token', '')}`",
            f"- query_session_snapshot_artifact_id: `{loop_result.metadata.get('query_session_snapshot_artifact_id', '')}`",
            f"- query_session_transcript_artifact_id: `{loop_result.metadata.get('query_session_transcript_artifact_id', '')}`",
            f"- code_worker_session_seed_ok: `{loop_result.metadata.get('code_worker_session_seed_ok', '')}`",
            f"- query_session_integration_ok: `{loop_result.metadata.get('query_session_integration_ok', '')}`",
            f"- query_entry_packet_id: `{loop_result.metadata.get('query_entry_packet_id', '')}`",
            f"- query_entry_route: `{loop_result.metadata.get('query_entry_route', '')}`",
            f"- query_entry_block_reason: `{loop_result.metadata.get('query_entry_block_reason', '')}`",
            f"- query_entry_message_count: `{loop_result.metadata.get('query_entry_message_count', '')}`",
            f"- query_entry_handoff_ok: `{loop_result.metadata.get('query_entry_handoff_ok', '')}`",
            f"- query_handoff_ok: `{loop_result.metadata.get('query_handoff_ok', '')}`",
            f"- query_handoff_ready_port_count: `{loop_result.metadata.get('query_handoff_ready_port_count', '')}`",
            f"- query_custody_ok: `{loop_result.metadata.get('query_custody_ok', '')}`",
            f"- query_custody_engine_attached: `{loop_result.metadata.get('query_custody_engine_attached', '')}`",
            f"- query_event_flow_ok: `{loop_result.metadata.get('query_event_flow_ok', '')}`",
            f"- query_event_flow_has_query_started: `{loop_result.metadata.get('query_event_flow_has_query_started', '')}`",
            f"- query_event_flow_has_stream_request_start: `{loop_result.metadata.get('query_event_flow_has_stream_request_start', '')}`",
            f"- query_state_graph_ok: `{loop_result.metadata.get('query_state_graph_ok', '')}`",
            f"- query_state_graph_ready_edge_count: `{loop_result.metadata.get('query_state_graph_ready_edge_count', '')}`",
            f"- query_disconnect_ok: `{loop_result.metadata.get('query_disconnect_ok', '')}`",
            f"- query_disconnect_active_scenario_count: `{loop_result.metadata.get('query_disconnect_active_scenario_count', '')}`",
            f"- context_assembly_snapshot_id: `{loop_result.metadata.get('context_assembly_snapshot_id', '')}`",
            f"- query_input_count: `{loop_result.metadata.get('query_input_count', '')}`",
            f"- session_seed_required: `{str(session_foundation.get('sessionSeedRequiredForDefaultPath') is True).lower()}`",
            f"- session_append_only_jsonl: `{str(session_persistence.get('appendOnlyJsonl') is True).lower()}`",
            f"- session_parent_uuid_chain: `{str(session_persistence.get('parentUuidChain') is True).lower()}`",
            f"- session_resume_chain: `{str(session_recovery.get('hasChainTraversal') is True).lower()}`",
            f"- tool_loop_contract_source: `{tool_loop_contract.get('source', '')}`",
            f"- tool_loop_contract_owner_unit: `{tool_loop_contract.get('ownerUnit', '')}`",
            f"- tool_loop_inventory_exists: `{str(tool_loop_contract.get('inventoryExists') is True).lower()}`",
            f"- tool_loop_read_only_concurrent: `{str(tool_loop_scheduling.get('readOnlyConcurrent') is True).lower()}`",
            f"- tool_loop_write_serial: `{str(tool_loop_scheduling.get('writeSerial') is True).lower()}`",
            f"- tool_loop_budget_externalization: `{str(tool_loop_budget.get('hasPersistedOutputTag') is True or tool_loop_budget.get('hasMaxResultSizeChars') is True).lower()}`",
            f"- tool_loop_shell_lifecycle: `{str(tool_loop_shell.get('hasProcessLifecycle') is True).lower()}`",
            f"- tool_loop_sandbox_adapter: `{str(tool_loop_sandbox.get('hasSandboxAdapter') is True).lower()}`",
            f"- tool_failure_signal_count: `{loop_result.metadata.get('tool_failure_signals', '')}`",
            f"- vendored_runtime_complete: `{str(vendor.get('complete') is True).lower()}`",
            f"- inventory_base_tool_count: `{tool_runtime.get('baseToolCount', 0)}`",
            f"- inventory_command_count: `{command_runtime.get('commandCount', 0)}`",
            "",
            "## Tool Steps",
            "",
            *(step_summaries or ["- no tool steps executed"]),
            "",
            "## Claude Code Runtime Inventory",
            "",
            *(f"- command: `{command}`" for command in high_value_commands[:24]),
            "",
            "## QueryEngine Contract Sources",
            "",
            *(f"- `{source_file}`" for source_file in source_files[:24]),
            "",
            "## Query Session Contract Sources",
            "",
            *(f"- `{source_file}`" for source_file in session_source_files[:32]),
            "",
            "## Tool Loop Budget Contract Sources",
            "",
            *(f"- `{source_file}`" for source_file in tool_loop_source_files[:32]),
            "",
            source_graph_crosswalk_markdown(integration_report).rstrip(),
            "",
            runtime_context_assembly_markdown(runtime_context_report).rstrip(),
            "",
            source_graph_audit_markdown(source_graph_audit).rstrip(),
            "",
            worker_execution_gate_markdown(worker_gate).rstrip(),
            "",
            render_query_session_integration_markdown(query_session_integration).rstrip(),
            "",
            render_query_handoff_markdown(query_handoff).rstrip(),
            "",
            render_query_resume_custody_markdown(query_custody).rstrip(),
            "",
            render_query_event_flow_markdown(query_event_flow).rstrip(),
            "",
            render_query_state_graph_markdown(query_state_graph).rstrip(),
            "",
            render_query_disconnect_markdown(query_disconnect).rstrip(),
            "",
            "## Vendored Runtime Modules",
            "",
            *(module_lines or ["- no sidecar module snapshot available"]),
            "",
        ]
    )
