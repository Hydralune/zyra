from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, Sequence

from .digests import arguments_digest, digest_object
from .composition import compose_skill_runtime
from .errors import SkillResourceNotFound
from .integration_errors import (
    SkillCommandArgumentError,
    SkillCommandNotInvocable,
    SkillForkToolDenied,
    SkillIntegrationDisabled,
    SkillOutcomeConflict,
)
from .invocation_permission import (
    SkillInvocationPermissionEffect,
    SkillInvocationPermissionResult,
)
from .models import (
    SkillInvocationMode,
    SkillInvocationRequest,
    SkillInvocationStatus,
    SkillRevision,
    new_id,
    utc_now,
)
from .runtime import SkillRuntime


class RuntimeToolTypes(Protocol):
    DynamicToolProvenance: Any
    ProvenancedDynamicHandler: Any
    ToolCall: Any
    ToolResult: Any
    ToolSpec: Any


@dataclass(frozen=True, slots=True)
class SkillToolProjectionConfig:
    project_root: str
    workspace_root: str
    session_id: str
    run_id: str
    task_id: str
    node_id: str = ""
    worker_request_id: str = ""
    agent_id: str = "CodeWorkerRuntime"
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    invoked_skill_refs: tuple[dict[str, Any], ...] = ()
    external_sources: tuple[Any, ...] = ()
    include_user_skills: bool = False
    disabled: bool = False
    max_resources_per_call: int = 16
    max_resource_tokens_per_call: int = 8_000
    fork_port: Any | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not str(self.project_root).strip():
            raise ValueError("skill tool projection requires project_root")
        if not str(self.workspace_root).strip():
            raise ValueError("skill tool projection requires workspace_root")
        if not str(self.session_id).strip():
            raise ValueError("skill tool projection requires session_id")
        if not str(self.run_id).strip() or not str(self.task_id).strip():
            raise ValueError("skill tool projection requires run_id and task_id")
        if self.max_resources_per_call <= 0:
            raise ValueError("max_resources_per_call must be positive")
        if self.max_resource_tokens_per_call <= 0:
            raise ValueError("max_resource_tokens_per_call must be positive")


@dataclass(frozen=True, slots=True)
class SkillToolProjectionEvent:
    kind: str
    run_id: str
    task_id: str
    session_id: str
    invocation_id: str = ""
    skill_ref: str = ""
    tool_call_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "skill_ref": self.skill_ref,
            "tool_call_id": self.tool_call_id,
            "payload": copy.deepcopy(self.payload),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SkillToolProjectionSnapshot:
    projection_id: str
    session_id: str
    registry_generation: int
    tool_names: tuple[str, ...]
    invocation_ids: tuple[str, ...]
    event_count: int
    state_snapshot: dict[str, Any]
    compact_references: tuple[dict[str, Any], ...]
    outcome_projections: tuple[dict[str, Any], ...]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "session_id": self.session_id,
            "registry_generation": self.registry_generation,
            "tool_names": list(self.tool_names),
            "invocation_ids": list(self.invocation_ids),
            "event_count": self.event_count,
            "state_snapshot": copy.deepcopy(self.state_snapshot),
            "compact_references": [copy.deepcopy(item) for item in self.compact_references],
            "outcome_projections": [copy.deepcopy(item) for item in self.outcome_projections],
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class SkillToolProjectionOpen:
    context: Any
    projection: "SkillToolProjectionRuntime"
    snapshot: SkillToolProjectionSnapshot


class ProjectedSkillInvocationPermission:
    """Admission port for an invocation already granted by ToolPermissionRuntime.

    The dynamic ToolExecutor verifies and consumes the exact one-use grant
    before calling the handler.  This port merely records that fact for the
    inner SkillInvocationRuntime.  It never creates a downstream tool grant.
    """

    def __init__(self) -> None:
        self._local = RLock()
        self._active_calls: dict[str, dict[str, Any]] = {}

    def enter(self, *, invocation_id: str, tool_call_id: str, request_digest: str) -> None:
        with self._local:
            if invocation_id in self._active_calls:
                raise SkillOutcomeConflict(
                    "projected skill invocation is already active",
                    detail={"invocation_id": invocation_id},
                )
            self._active_calls[invocation_id] = {
                "tool_call_id": tool_call_id,
                "request_digest": request_digest,
            }

    def exit(self, invocation_id: str) -> None:
        with self._local:
            self._active_calls.pop(invocation_id, None)

    def guard(
        self,
        request: SkillInvocationRequest,
        revision: SkillRevision,
    ) -> SkillInvocationPermissionResult:
        with self._local:
            active = self._active_calls.get(request.invocation_id)
        if active is None:
            return SkillInvocationPermissionResult(
                effect=SkillInvocationPermissionEffect.DENY,
                reason="skill invocation did not enter through the granted Skill tool handler",
                decision_id=f"projected-skill-deny:{request.invocation_id}",
                request_id=request.invocation_id,
                events=(),
            )
        expected = arguments_digest(
            {
                "skill": request.skill_name,
                "arguments": request.arguments,
                "requested_ref": request.requested_version_ref.to_dict()
                if request.requested_version_ref
                else None,
            }
        )
        if active["request_digest"] != expected:
            return SkillInvocationPermissionResult(
                effect=SkillInvocationPermissionEffect.DENY,
                reason="skill invocation arguments changed after the outer permission grant",
                decision_id=f"projected-skill-digest-deny:{request.invocation_id}",
                request_id=request.invocation_id,
                events=(),
            )
        return SkillInvocationPermissionResult(
            effect=SkillInvocationPermissionEffect.ALLOW,
            reason="exact Skill tool call was granted and consumed by M1-03A",
            decision_id=f"projected-skill-allow:{request.invocation_id}",
            request_id=request.invocation_id,
            events=(),
        )


class SkillToolProjectionRuntime:
    """Projects SkillRuntime into the real QueryEngine tool registry.

    The projection is request-scoped.  It merges immutable ToolSpecs and
    provenance-bound handlers into a copied ToolExecutionContext.  QueryEngine
    therefore discovers and invokes skills through the same registry,
    permission runtime, result budget, transcript, and event flow as every
    other CodeWorker tool.
    """

    TOOL_NAME = "skill"
    RESOURCE_TOOL_NAME = "read_skill_resource"
    LIST_TOOL_NAME = "list_skills"

    def __init__(self, config: SkillToolProjectionConfig) -> None:
        self.config = config
        self._permission = ProjectedSkillInvocationPermission()
        self.runtime, self.composition_snapshot = compose_skill_runtime(
            product_root=config.project_root,
            workspace_root=config.workspace_root,
            permission_port=self._permission,
            fork_port=config.fork_port,
            state_snapshot=config.state_snapshot or None,
            disabled=config.disabled,
            external_sources=config.external_sources,
        )
        self.runtime.bootstrap()
        self._lock = RLock()
        self._events: list[SkillToolProjectionEvent] = []
        self._tool_call_to_invocation: dict[str, str] = {}
        self._projection_id = digest_object(
            {
                "run_id": config.run_id,
                "task_id": config.task_id,
                "session_id": config.session_id,
                "worker_request_id": config.worker_request_id,
                "registry_generation": self.runtime.registry.generation,
            }
        )[:32]

    @classmethod
    def open_for_worker(
        cls,
        context: Any,
        *,
        project_root: str | Path,
        request: Any,
        session_id: str,
        disabled: bool = False,
        external_sources: Sequence[Any] = (),
        fork_port: Any | None = None,
    ) -> SkillToolProjectionOpen:
        constraints = request.constraints if isinstance(request.constraints, Mapping) else {}
        raw_state = constraints.get("skill_runtime_state")
        state_snapshot = dict(raw_state) if isinstance(raw_state, Mapping) else {}
        raw_refs = constraints.get("invoked_skill_refs")
        refs = tuple(dict(item) for item in raw_refs or () if isinstance(item, Mapping))
        runtime = cls(
            SkillToolProjectionConfig(
                project_root=str(Path(project_root).resolve()),
                workspace_root=str(Path(context.workspace_root).resolve()),
                session_id=session_id,
                run_id=str(request.run_id),
                task_id=str(request.task_id),
                node_id=str(request.node_id or ""),
                worker_request_id=str(request.request_id),
                agent_id=str(getattr(request, "worker_name", "") or "CodeWorkerRuntime"),
                state_snapshot=state_snapshot,
                invoked_skill_refs=refs,
                external_sources=tuple(external_sources),
                include_user_skills=False,
                disabled=disabled or constraints.get("disable_skill_runtime") is True,
                fork_port=fork_port,
            )
        )
        projected = runtime.project_context(context)
        return SkillToolProjectionOpen(
            context=projected,
            projection=runtime,
            snapshot=runtime.snapshot(),
        )

    def project_context(self, context: Any) -> Any:
        if self.config.disabled:
            raise SkillIntegrationDisabled("SkillTool projection is disabled")
        types = _runtime_tool_types()
        provenances = self._provenances(types)
        specs = self._tool_specs(types, provenances)
        handlers = {
            self.TOOL_NAME: types.ProvenancedDynamicHandler(
                provenances[self.TOOL_NAME],
                self._invoke_handler,
            ),
            self.RESOURCE_TOOL_NAME: types.ProvenancedDynamicHandler(
                provenances[self.RESOURCE_TOOL_NAME],
                self._resource_handler,
            ),
            self.LIST_TOOL_NAME: types.ProvenancedDynamicHandler(
                provenances[self.LIST_TOOL_NAME],
                self._listing_handler,
            ),
        }
        merged_registry = context.registry.merged(list(specs), keep_existing=True)
        existing_handlers = dict(context.dynamic_handlers)
        collisions = set(existing_handlers).intersection(handlers)
        if collisions:
            raise SkillCommandNotInvocable(
                "SkillTool projection collides with an existing executable handler",
                detail={"tools": sorted(collisions)},
            )
        existing_handlers.update(handlers)
        runtime_services = dict(getattr(context, "runtime_services", {}) or {})
        existing_skill_runtime = runtime_services.get("skill_runtime")
        if existing_skill_runtime is not None and existing_skill_runtime is not self.runtime:
            raise SkillCommandNotInvocable(
                "ToolExecutionContext already owns another SkillRuntime instance"
            )
        runtime_services.update(
            {
                "skill_runtime": self.runtime,
                "skill_tool_projection": self,
            }
        )
        return replace(
            context,
            registry=merged_registry,
            dynamic_handlers=existing_handlers,
            runtime_services=runtime_services,
        )

    def _provenances(self, types: RuntimeToolTypes) -> dict[str, Any]:
        return {
            name: types.DynamicToolProvenance(
                tool_name=name,
                namespace="skill",
                server_id="zyra-skill-runtime",
                version="M1-S03C-02",
                handler_kind="skill_runtime",
                external_boundary=False,
                requires_exact_grant=True,
                source="zyra_skills.SkillToolProjectionRuntime",
            )
            for name in (self.TOOL_NAME, self.RESOURCE_TOOL_NAME, self.LIST_TOOL_NAME)
        }

    def _tool_specs(self, types: RuntimeToolTypes, provenances: Mapping[str, Any]) -> tuple[Any, ...]:
        common = {
            "source_path": "src/tools/SkillTool/SkillTool.ts",
            "owner_unit": "M1-03C",
            "permission_owner": "M1-03A",
            "session_owner": "M1-02B/02D",
            "read_only": "false",
            "concurrency_safe": "false",
        }
        return (
            types.ToolSpec(
                name=self.TOOL_NAME,
                purpose="Invoke an available versioned Zyra skill in the current CodeWorker session.",
                source="claude-code-best SkillTool, productized by M1-03C",
                input_schema={
                    "type": "object",
                    "required": ["skill"],
                    "properties": {
                        "skill": {"type": "string"},
                        "arguments": {"type": "object"},
                        "resources": {"type": "array", "items": {"type": "string"}},
                        "invocation_id": {"type": "string"},
                        "idempotency_key": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "required": ["status", "invocation_id", "skill_ref"],
                    "properties": {
                        "status": {"type": "string"},
                        "invocation_id": {"type": "string"},
                        "skill_ref": {"type": "string"},
                        "body": {"type": "string"},
                        "fork_request": {"type": ["object", "null"]},
                    },
                },
                metadata={**common, "operation": "skill_context_expansion"},
                execution_provenance=provenances[self.TOOL_NAME],
            ),
            types.ToolSpec(
                name=self.RESOURCE_TOOL_NAME,
                purpose="Read one declared resource from an already invoked skill revision.",
                source="Agent Framework SkillResource + Claude SkillTool",
                input_schema={
                    "type": "object",
                    "required": ["invocation_id", "path"],
                    "properties": {
                        "invocation_id": {"type": "string"},
                        "path": {"type": "string"},
                        "token_budget": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "required": ["resource_ref", "content", "truncated"],
                },
                metadata={**common, "operation": "skill_resource_read", "read_only": "true"},
                execution_provenance=provenances[self.RESOURCE_TOOL_NAME],
            ),
            types.ToolSpec(
                name=self.LIST_TOOL_NAME,
                purpose="List skill descriptions without loading bodies or resources.",
                source="Claude skill_listing + OpenCode Skill.available",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "workspace_paths": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
                output_schema={"type": "object", "required": ["skills", "generation"]},
                metadata={**common, "operation": "skill_listing", "read_only": "true"},
                execution_provenance=provenances[self.LIST_TOOL_NAME],
            ),
        )

    def _invoke_handler(self, call: Any) -> Any:
        types = _runtime_tool_types()
        arguments = dict(call.arguments)
        skill_name = str(arguments.get("skill") or "").strip().removeprefix("/")
        if not skill_name:
            return self._error_result(types, call, "skill_name_required", "Skill tool requires a skill name.")
        skill_args = arguments.get("arguments")
        if skill_args is None:
            skill_args = {}
        if not isinstance(skill_args, Mapping):
            return self._error_result(types, call, "skill_arguments_invalid", "Skill arguments must be an object.")
        resources = arguments.get("resources") or ()
        if not isinstance(resources, Sequence) or isinstance(resources, str | bytes):
            return self._error_result(types, call, "skill_resources_invalid", "Skill resources must be an array.")
        if len(resources) > self.config.max_resources_per_call:
            return self._error_result(types, call, "skill_resource_limit", "Too many resources requested.")
        if not all(isinstance(item, str) for item in resources):
            return self._error_result(types, call, "skill_resources_invalid", "Skill resource paths must be strings.")
        invocation_id = str(arguments.get("invocation_id") or "").strip() or new_id("skillinv")
        request = SkillInvocationRequest(
            run_id=self.config.run_id,
            task_id=self.config.task_id,
            session_id=self.config.session_id,
            agent_id=self.config.agent_id,
            skill_name=skill_name,
            arguments=dict(skill_args),
            node_id=self.config.node_id,
            worker_request_id=self.config.worker_request_id,
            parent_tool_use_id=str(call.tool_call_id),
            idempotency_key=str(arguments.get("idempotency_key") or call.tool_call_id),
            interactive=False,
            headless=True,
            invocation_id=invocation_id,
        )
        request_digest = arguments_digest(
            {"skill": request.skill_name, "arguments": request.arguments, "requested_ref": None}
        )
        self._permission.enter(
            invocation_id=invocation_id,
            tool_call_id=str(call.tool_call_id),
            request_digest=request_digest,
        )
        try:
            plan = self.runtime.invoke(
                request,
                load_resources=tuple(resources),
                require_model_invocable=True,
                require_user_invocable=False,
            )
        except Exception as error:  # noqa: BLE001 - converted to a typed tool result.
            return self._error_result(
                types,
                call,
                str(getattr(error, "code", "skill_invocation_failed")),
                str(error),
                detail=dict(getattr(error, "detail", {}) or {}),
            )
        finally:
            self._permission.exit(invocation_id)
        with self._lock:
            self._tool_call_to_invocation[str(call.tool_call_id)] = invocation_id
        is_fork = plan.revision.metadata.invocation.mode is SkillInvocationMode.FORK
        output: dict[str, Any] = {
            "status": str(plan.state.status),
            "invocation_id": invocation_id,
            "skill_ref": plan.revision.version_ref.immutable_ref,
            "qualified_name": plan.revision.qualified_name,
            "policy_snapshot": plan.policy_snapshot.to_dict(),
            "attachment_refs": [item.immutable_ref for item in plan.attachments],
            "resource_refs": [item.descriptor.immutable_ref for item in plan.resources],
            "body": "" if is_fork else plan.body.text,
            "body_disclosed": not is_fork,
            "fork_request": plan.fork_request.to_dict() if plan.fork_request else None,
            "downstream_tools_authorized": False,
            "state_checkpoint": self.runtime.state_snapshot(),
        }
        event = SkillToolProjectionEvent(
            kind="skill_tool_invoked",
            run_id=self.config.run_id,
            task_id=self.config.task_id,
            session_id=self.config.session_id,
            invocation_id=invocation_id,
            skill_ref=plan.revision.version_ref.immutable_ref,
            tool_call_id=str(call.tool_call_id),
            payload={
                "mode": str(plan.revision.metadata.invocation.mode),
                "status": str(plan.state.status),
                "body_disclosed": not is_fork,
                "fork_handoff": is_fork,
                "policy_digest": plan.policy_snapshot.policy_digest,
            },
        )
        self._append_event(event)
        return types.ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=(
                f"Skill {plan.revision.qualified_name} handed off for isolated execution."
                if is_fork
                else f"Skill {plan.revision.qualified_name} loaded into the current query session."
            ),
            output=output,
            metadata={
                "tool_namespace": "skill",
                "skill_invocation_id": invocation_id,
                "skill_ref": plan.revision.version_ref.immutable_ref,
                "skill_mode": str(plan.revision.metadata.invocation.mode),
                "permission_authority": "M1-03A",
            },
        )

    def _resource_handler(self, call: Any) -> Any:
        types = _runtime_tool_types()
        invocation_id = str(call.arguments.get("invocation_id") or "").strip()
        relative_path = str(call.arguments.get("path") or "").strip()
        if not invocation_id or not relative_path:
            return self._error_result(
                types,
                call,
                "skill_resource_arguments_required",
                "read_skill_resource requires invocation_id and path.",
            )
        try:
            state = self.runtime.state_store.get(invocation_id)
            if state.session_id != self.config.session_id:
                raise SkillForkToolDenied(
                    "skill resource invocation belongs to another session",
                    detail={"invocation_id": invocation_id},
                )
            if state.status.terminal:
                raise SkillForkToolDenied(
                    "terminal skill invocation cannot disclose new resources",
                    detail={"status": str(state.status)},
                )
            revision = self.runtime.registry.resolve(
                state.version_ref.qualified_name,
                requested_ref=state.version_ref,
            )
            descriptor = next(
                (item for item in revision.resources if item.relative_path == relative_path),
                None,
            )
            if descriptor is None:
                raise SkillResourceNotFound(
                    "skill resource is not declared by the exact revision",
                    detail={"path": relative_path},
                )
            requested_budget = int(
                call.arguments.get("token_budget")
                or min(
                    revision.metadata.context_budget.resource_read_tokens,
                    self.config.max_resource_tokens_per_call,
                )
            )
            token_budget = max(1, min(requested_budget, self.config.max_resource_tokens_per_call))
            loaded = self.runtime.resource_loader.load(
                revision,
                descriptor.relative_path,
                token_budget=token_budget,
            )
        except Exception as error:  # noqa: BLE001 - converted to a typed tool result.
            return self._error_result(
                types,
                call,
                str(getattr(error, "code", "skill_resource_read_failed")),
                str(error),
                detail=dict(getattr(error, "detail", {}) or {}),
            )
        self._append_event(
            SkillToolProjectionEvent(
                kind="skill_resource_disclosed",
                run_id=self.config.run_id,
                task_id=self.config.task_id,
                session_id=self.config.session_id,
                invocation_id=invocation_id,
                skill_ref=state.version_ref.immutable_ref,
                tool_call_id=str(call.tool_call_id),
                payload={
                    "resource_ref": descriptor.immutable_ref,
                    "relative_path": descriptor.relative_path,
                    "token_estimate": loaded.token_estimate,
                    "truncated": loaded.truncated,
                },
            )
        )
        return types.ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Loaded declared resource {descriptor.relative_path}.",
            output={
                "invocation_id": invocation_id,
                "skill_ref": state.version_ref.immutable_ref,
                "resource_ref": descriptor.immutable_ref,
                "relative_path": descriptor.relative_path,
                "media_type": descriptor.media_type,
                "content": loaded.content,
                "truncated": loaded.truncated,
                "token_estimate": loaded.token_estimate,
            },
            metadata={
                "tool_namespace": "skill",
                "skill_invocation_id": invocation_id,
                "skill_ref": state.version_ref.immutable_ref,
                "resource_ref": descriptor.immutable_ref,
            },
        )

    def _listing_handler(self, call: Any) -> Any:
        types = _runtime_tool_types()
        query = str(call.arguments.get("query") or "").strip()
        paths = call.arguments.get("workspace_paths") or ()
        if not isinstance(paths, Sequence) or isinstance(paths, str | bytes):
            return self._error_result(
                types,
                call,
                "skill_workspace_paths_invalid",
                "workspace_paths must be an array of strings.",
            )
        if not all(isinstance(item, str) for item in paths):
            return self._error_result(
                types,
                call,
                "skill_workspace_paths_invalid",
                "workspace_paths must contain only strings.",
            )
        if query:
            from .search import SkillSearchIndex

            hits = SkillSearchIndex(self.runtime.registry).search(query).hits
            entries = [hit.entry.to_dict() for hit in hits]
        else:
            entries = [
                item.to_dict()
                for item in self.runtime.list(
                    workspace_paths=tuple(paths),
                    for_model=True,
                )
            ]
        self._append_event(
            SkillToolProjectionEvent(
                kind="skill_listing_disclosed",
                run_id=self.config.run_id,
                task_id=self.config.task_id,
                session_id=self.config.session_id,
                tool_call_id=str(call.tool_call_id),
                payload={"query": query, "count": len(entries), "body_disclosed": False},
            )
        )
        return types.ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Listed {len(entries)} available skills without loading their bodies.",
            output={
                "skills": entries,
                "generation": self.runtime.registry.generation,
                "body_disclosed": False,
            },
            metadata={"tool_namespace": "skill", "body_disclosed": "false"},
        )

    def complete_from_tool_result(
        self,
        tool_call_id: str,
        *,
        outcome_refs: Sequence[str] = (),
        evidence_refs: Sequence[str] = (),
        artifact_refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        with self._lock:
            invocation_id = self._tool_call_to_invocation.get(str(tool_call_id), "")
        if not invocation_id:
            raise SkillCommandArgumentError(
                "tool call does not identify a projected skill invocation",
                detail={"tool_call_id": tool_call_id},
            )
        state = self.runtime.invocation_runtime.complete(
            invocation_id,
            outcome_refs=outcome_refs,
            evidence_refs=evidence_refs,
            artifact_refs=artifact_refs,
        )
        projection = self.runtime.compact_bridge.memory_projection(state).to_dict()
        self._append_event(
            SkillToolProjectionEvent(
                kind="skill_tool_completed",
                run_id=self.config.run_id,
                task_id=self.config.task_id,
                session_id=self.config.session_id,
                invocation_id=invocation_id,
                skill_ref=state.version_ref.immutable_ref,
                tool_call_id=str(tool_call_id),
                payload={
                    "status": str(state.status),
                    "outcome_projection": projection,
                    "body_persisted": False,
                },
            )
        )
        return projection

    def finalize_successful_query(
        self,
        *,
        evidence_refs: Sequence[str] = (),
        artifact_refs: Sequence[str] = (),
    ) -> tuple[dict[str, Any], ...]:
        """Commit every inline invocation when its owning query finishes.

        A projected inline skill remains active for the whole query so its
        allowed-tools and hook policy govern all downstream calls.  The worker
        invokes this method only after a successful, non-suspended query.  Fork
        handoffs deliberately remain pending for M1-03D to execute.
        """

        projections: list[dict[str, Any]] = []
        for current in self.runtime.state_store.all_states():
            if current.session_id != self.config.session_id:
                continue
            if current.status is not SkillInvocationStatus.INLINE_ACTIVE:
                continue
            state = self.runtime.invocation_runtime.complete(
                current.invocation_id,
                outcome_refs=evidence_refs,
                evidence_refs=evidence_refs,
                artifact_refs=artifact_refs,
            )
            projection = self.runtime.compact_bridge.memory_projection(state).to_dict()
            projections.append(projection)
            self._append_event(
                SkillToolProjectionEvent(
                    kind="skill_query_completed",
                    run_id=self.config.run_id,
                    task_id=self.config.task_id,
                    session_id=self.config.session_id,
                    invocation_id=current.invocation_id,
                    skill_ref=current.version_ref.immutable_ref,
                    payload={
                        "status": str(state.status),
                        "outcome_projection": projection,
                        "body_persisted": False,
                        "terminal_owner": "CodeWorkerRuntime.query_success",
                    },
                )
            )
        return tuple(projections)

    def snapshot(self) -> SkillToolProjectionSnapshot:
        checkpoint = self.runtime.session_bridge.checkpoint(
            session_id=self.config.session_id,
            agent_id=self.config.agent_id,
            runtime_state_snapshot=self.runtime.state_snapshot(),
        )
        outcomes: list[dict[str, Any]] = []
        invocation_ids: list[str] = []
        for state in self.runtime.state_store.all_states():
            if state.session_id != self.config.session_id:
                continue
            invocation_ids.append(state.invocation_id)
            if state.status.terminal:
                outcomes.append(self.runtime.compact_bridge.memory_projection(state).to_dict())
        with self._lock:
            event_count = len(self._events)
        payload = {
            "projection_id": self._projection_id,
            "session_id": self.config.session_id,
            "registry_generation": self.runtime.registry.generation,
            "tools": [self.TOOL_NAME, self.RESOURCE_TOOL_NAME, self.LIST_TOOL_NAME],
            "invocation_ids": sorted(invocation_ids),
            "event_count": event_count,
            "checkpoint": checkpoint.to_dict(),
            "outcomes": outcomes,
        }
        return SkillToolProjectionSnapshot(
            projection_id=self._projection_id,
            session_id=self.config.session_id,
            registry_generation=self.runtime.registry.generation,
            tool_names=(self.TOOL_NAME, self.RESOURCE_TOOL_NAME, self.LIST_TOOL_NAME),
            invocation_ids=tuple(sorted(invocation_ids)),
            event_count=event_count,
            state_snapshot=checkpoint.to_dict(),
            compact_references=tuple(
                reference.to_dict() for reference in checkpoint.compact_references
            ),
            outcome_projections=tuple(outcomes),
            digest=digest_object(payload),
        )

    def events(self) -> tuple[SkillToolProjectionEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def _append_event(self, event: SkillToolProjectionEvent) -> None:
        with self._lock:
            self._events.append(event)

    def _error_result(
        self,
        types: RuntimeToolTypes,
        call: Any,
        code: str,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> Any:
        self._append_event(
            SkillToolProjectionEvent(
                kind="skill_tool_failed",
                run_id=self.config.run_id,
                task_id=self.config.task_id,
                session_id=self.config.session_id,
                tool_call_id=str(call.tool_call_id),
                payload={"error": code, "message": message, "detail": dict(detail or {})},
            )
        )
        return types.ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary=message,
            output={"detail": dict(detail or {})},
            error=code,
            metadata={"tool_namespace": "skill"},
        )


def _runtime_tool_types() -> RuntimeToolTypes:
    try:
        from zyra_runtime import (
            DynamicToolProvenance,
            ProvenancedDynamicHandler,
            ToolCall,
            ToolResult,
            ToolSpec,
        )
    except ImportError as error:  # pragma: no cover - packaging gate verifies dependency.
        raise SkillIntegrationDisabled("zyra_runtime tool contracts are unavailable") from error

    class _Types:
        pass

    value = _Types()
    value.DynamicToolProvenance = DynamicToolProvenance
    value.ProvenancedDynamicHandler = ProvenancedDynamicHandler
    value.ToolCall = ToolCall
    value.ToolResult = ToolResult
    value.ToolSpec = ToolSpec
    return value
