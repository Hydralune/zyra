from __future__ import annotations

import hashlib
import json
import math
import os
import time
from uuid import uuid4
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
from zyra_runtime.executor import ToolExecutionContext
from zyra_runtime.permissions import JsonPermissionStore
from zyra_runtime.permission.custody import (
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyError,
    PermissionSessionCustodyReceipt,
    PermissionSessionCustodyStore,
)
from zyra_runtime.permission.store import PermissionStateStore
from zyra_runtime.typescript_runtime_host import ClaudeQueryEngineConfig
from zyra_runtime.workers import WorkerRequest, WorkerResult

from zyra_runtime.sandbox_gateway.integration_factory import (
    install_gateway_runtime_services,
)
from zyra_runtime.sandbox_gateway.integration_host import GatewayHostProcessRuntime

from .code_worker_bridge import CodeWorkerSidecarClient
from .typescript_claude_runtime import TypeScriptClaudeQueryEngine
from .retrieval_context_runtime import WorkerRetrievalContext, WorkerRetrievalContextRuntime


@dataclass(frozen=True, slots=True)
class CodeWorkerRun:
    """Public worker result plus noncanonical Python event projection."""

    worker_result: WorkerResult
    event_records: list[EventRecord]
    session_custody_token: str = field(default="", repr=False)
    session_id: str = ""
    session_custody_id: str = ""
    session_custody_fingerprint: str = ""
    session_custody_created: bool = False
    execution_evidence: dict[str, Any] = field(default_factory=dict)

    def safe_session_metadata(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "permission_session_custody_id": self.session_custody_id,
            "permission_session_custody_fingerprint": self.session_custody_fingerprint,
            "permission_session_custody_created": self.session_custody_created,
            "session_custody_token_included": False,
        }

    def private_api_session_envelope(self) -> dict[str, Any]:
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
    """Narrow Python host for the canonical TypeScript CodeWorker runtime.

    Python owns process launch, durable byte storage, physical tools, artifacts,
    and EventRecord projection. Query, context, session, model, compact,
    permission, MCP, skill/plugin/command and tool-loop state transitions are
    produced only by TypeScript.
    """

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
        permission_extension_registry: Any | None = None,
        permission_state_path: str | Path | None = None,
        tool_registry: Any | None = None,
        dynamic_handlers: Mapping[str, Any] | None = None,
        runtime_services: Mapping[str, Any] | None = None,
        event_reader: Callable[[str], list[dict[str, Any]]] | None = None,
        checkpoint_reader: Callable[[str], dict[str, Any] | None] | None = None,
        skill_fork_port: Any | None = None,
        retrieval_context_runtime: WorkerRetrievalContextRuntime | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.sidecar_client = sidecar_client or CodeWorkerSidecarClient(self.project_root)
        self.query_engine_factory = query_engine_factory
        self.permission_bypass_available = bool(permission_bypass_available)
        self.permission_auto_available = bool(permission_auto_available)
        self.permission_accept_edits_available = bool(permission_accept_edits_available)
        self.permission_extension_registry = permission_extension_registry
        self.skill_fork_port = skill_fork_port
        self.retrieval_context_runtime = retrieval_context_runtime
        supplied_services = dict(runtime_services or {})
        self.managed_workspace_custody = bool(
            supplied_services.get(
                "sandbox_gateway_required",
                supplied_services.get("workspace_gateway_required", False),
            )
        )
        services = install_gateway_runtime_services(
            runtime_services,
            workspace_root=workspace_root,
            artifact_root=artifact_root,
            worker_id="CodeWorkerRuntime",
            workspace_edit_port=dict(runtime_services or {}).get("workspace_edit_port"),
        )
        bundle = services.get("sandbox_gateway_bundle")
        if bundle is not None:
            services.setdefault(
                "sandbox_gateway_host_runtime",
                GatewayHostProcessRuntime(
                    bundle.policy_runtime,
                    redactor=bundle.backend.redactor,
                    allowed_roots=(self.project_root,),
                ),
            )
        self.execution_context = ToolExecutionContext.for_workspace(
            workspace_root=workspace_root,
            artifact_root=artifact_root,
            permission_store=permission_store,
            registry=tool_registry,
            event_reader=event_reader,
            checkpoint_reader=checkpoint_reader,
            dynamic_handlers=dynamic_handlers,
            runtime_services=services,
        )
        self.permission_state_path = (
            Path(permission_state_path).resolve()
            if permission_state_path is not None
            else self.execution_context.artifact_store.root / ".permission" / "state.json"
        )
        self.runtime_state_root = (
            self.execution_context.artifact_store.root / ".e01-runtime-state"
        )

    def run(self, request: WorkerRequest) -> CodeWorkerRun:
        constraints = dict(request.constraints)
        if constraints.get("disable_typescript_runtime") is True:
            return self._failure(
                request,
                error="typescript_runtime_disabled",
                summary="CodeWorkerRuntime canonical TypeScript runtime is disabled.",
                session_id=self._session_id(request),
            )
        if (
            constraints.get("disable_productized_runtime") is True
            or self.query_engine_factory is None
        ):
            return self._failure(
                request,
                error="productized_query_engine_runtime_disabled",
                summary="CodeWorkerRuntime canonical TypeScript runtime is disconnected.",
                session_id=self._session_id(request),
            )

        session_id = self._session_id(request)
        custody: PermissionSessionCustodyReceipt | None = None
        if self.managed_workspace_custody:
            presented_token = str(
                constraints.get("session_custody_token")
                or constraints.get("permission_session_custody_token")
                or ""
            )
            try:
                custody = PermissionSessionCustodyStore(
                    PermissionStateStore(self.permission_state_path)
                ).claim(
                    PermissionSessionCustodyBinding(
                        session_id=session_id,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        workspace_root=str(self.workspace_root),
                    ),
                    presented_token=presented_token,
                )
            except PermissionSessionCustodyError as error:
                return self._failure(
                    request,
                    error=error.code,
                    summary=f"CodeWorkerRuntime rejected permission-session custody: {error}",
                    session_id=session_id,
                )
            constraints = {
                key: value
                for key, value in constraints.items()
                if "custody_token" not in str(key).casefold()
            }
            constraints.setdefault("permission_transport_queue_enabled", True)
        turns = self._query_turns(constraints)
        restored_state = self._restored_state(session_id, constraints)
        request_messages: Sequence[Any] = tuple(request.messages)
        request_metadata = {
            **request.metadata,
            "session_id": session_id,
            "worker_id": str(request.metadata.get("worker_id") or request.worker_name),
            "canonical_runtime_owner": "typescript",
            "python_runtime_role": "process-durability-side-effect-host",
            # The TypeScript E03 capability lattice must derive child roots
            # from the manager-owned workspace actually granted to this
            # worker, not from the Bun process cwd.
            "workspace_roots": [str(self.workspace_root)],
        }
        retrieval_context: WorkerRetrievalContext | None = None
        if self.retrieval_context_runtime is not None and constraints.get("disable_retrieval_context") is not True:
            try:
                retrieval_context = self.retrieval_context_runtime.prepare(
                    request,
                    session_id=session_id,
                )
            except Exception as error:  # noqa: BLE001 - current-context retrieval fails closed.
                return self._failure(
                    request,
                    error="retrieval_context_prepare_failed",
                    summary=f"CodeWorkerRuntime could not prove current retrieval context: {type(error).__name__}: {error}",
                    session_id=session_id,
                )
            request_messages = (*request_messages, *retrieval_context.messages)
            constraints.update(retrieval_context.constraint_delta)
            request_metadata.update(retrieval_context.metadata)
        logical_request_digest = hashlib.sha256(
            json.dumps(
                to_jsonable(
                    {
                        "turns": turns,
                        "messages": list(request_messages),
                    }
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        logical_worker_request_id = self._logical_worker_request_id(
            restored_state,
            request_id=request.request_id,
            request_digest=logical_request_digest,
        )
        try:
            engine = self.query_engine_factory(
                self.execution_context,
                ClaudeQueryEngineConfig(
                    max_turns=_optional_int(constraints.get("max_turns")),
                    max_tool_result_chars=_positive_int(
                        constraints.get("tool_result_budget_chars"), 8000
                    ),
                    max_query_context_chars=_positive_int(
                        constraints.get("query_context_budget_chars"), 32000
                    ),
                    continue_on_error=constraints.get("continue_on_error") is True,
                    max_read_only_concurrency=_positive_int(
                        constraints.get("max_read_only_concurrency"), 10
                    ),
                    emit_tool_use_summaries=(
                        constraints.get("emit_tool_use_summaries") is not False
                    ),
                    allow_empty_turns=not turns,
                    control_commands=tuple(constraints.get("control_commands") or ()),
                    project_root=self.project_root,
                    session_seed={
                        "session_id": session_id,
                        "run_id": request.run_id,
                        "task_id": request.task_id,
                        "worker_request_id": logical_worker_request_id,
                    },
                    context_snapshot=_mapping(constraints.get("context_snapshot")),
                    preprocessed_messages=request_messages,
                    session_foundation_metadata={
                        "canonical_runtime_owner": "typescript",
                        "runtime_protocol": "zyra.claude-runtime.v1",
                    },
                    max_turn_tool_result_chars=_optional_int(
                        constraints.get("turn_tool_result_budget_chars")
                    ),
                    disable_tool_registry_runtime=(
                        constraints.get("disable_tool_registry_runtime") is True
                    ),
                    disable_tool_execution_runtime=(
                        constraints.get("disable_tool_execution_runtime") is True
                    ),
                    disable_tool_result_budget_runtime=(
                        constraints.get("disable_tool_result_budget_runtime") is True
                    ),
                    disable_tool_permission_handoff_runtime=(
                        constraints.get("disable_tool_permission_handoff_runtime") is True
                    ),
                    disable_tool_permission_runtime=(
                        constraints.get("disable_tool_permission_runtime") is True
                    ),
                    disable_permission_rule_store=(
                        constraints.get("disable_permission_rule_store") is True
                    ),
                    disable_permission_request_queue=(
                        constraints.get("disable_permission_request_queue") is True
                    ),
                    disable_permission_decision_log=(
                        constraints.get("disable_permission_decision_log") is True
                    ),
                    disable_permission_continuation_runtime=(
                        constraints.get("disable_permission_continuation_runtime") is True
                    ),
                    permission_mode=str(constraints.get("permission_mode") or "default"),
                    permission_approval_ttl_seconds=_bounded_positive_float(
                        constraints.get("permission_approval_ttl_seconds"),
                        default=300.0,
                        minimum=1.0,
                        maximum=86_400.0,
                    ),
                    permission_execution_grant_ttl_seconds=_bounded_positive_float(
                        constraints.get("permission_execution_grant_ttl_seconds"),
                        default=30.0,
                        minimum=1.0,
                        maximum=3_600.0,
                    ),
                    permission_interactive=(
                        constraints.get("permission_interactive") is not False
                    ),
                    permission_headless=(
                        constraints.get("permission_headless") is True
                        or constraints.get("permission_mode") == "sealed"
                    ),
                    permission_bypass_available=self.permission_bypass_available,
                    permission_auto_available=self.permission_auto_available,
                    permission_state_path=self.permission_state_path,
                    permission_extension_registry=self.permission_extension_registry,
                    disable_runtime_budget_state=(
                        constraints.get("disable_runtime_budget_state") is True
                    ),
                    disable_compact_restore_runtime=(
                        constraints.get("disable_compact_restore_runtime") is True
                    ),
                    disable_model_stream_runtime=(
                        constraints.get("disable_model_stream_runtime") is True
                    ),
                    disable_api_retry_runtime=(
                        constraints.get("disable_api_retry_runtime") is True
                    ),
                    disable_context_security_runtime=(
                        constraints.get("disable_context_security_runtime") is True
                    ),
                    disable_restore_integration_runtime=(
                        constraints.get("disable_restore_integration_runtime") is True
                    ),
                    model_name=str(
                        constraints.get("model_name") or "zyra-local-code-model"
                    ),
                    model_input_token_limit=_positive_int(
                        constraints.get("model_input_token_limit"), 200000
                    ),
                    model_output_token_limit=_positive_int(
                        constraints.get("model_output_token_limit"), 8192
                    ),
                    api_retry_max_attempts=_positive_int(
                        constraints.get("api_retry_max_attempts"), 3
                    ),
                    api_retry_fallback_models=_string_tuple(
                        constraints.get("api_retry_fallback_models"),
                        ("zyra-local-fallback",),
                    ),
                    runtime_constraints=constraints,
                    restored_runtime_state=restored_state,
                ),
            )
            loop_result = engine.run(
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
                worker_request_id=logical_worker_request_id,
                turns=turns,
                request_messages=request_messages,
                request_metadata=request_metadata,
            )
        except Exception as error:  # noqa: BLE001 - process boundary fails closed.
            settlement_error = self._settle_retrieval_context(
                retrieval_context,
                committed=False,
                terminal_event_ids=(),
                reason=f"typescript_runtime_host_failed:{type(error).__name__}",
            )
            return self._failure(
                request,
                error=(
                    "retrieval_delivery_finalize_failed"
                    if settlement_error
                    else "typescript_runtime_host_failed"
                ),
                summary=(
                    f"TypeScript runtime host failed closed: {type(error).__name__}: {error}"
                    + (f"; retrieval settlement also failed: {settlement_error}" if settlement_error else "")
                ),
                session_id=session_id,
            )

        try:
            checkpoint_path = self._persist_runtime_state(
                session_id,
                {
                    "schema_version": 1,
                    "canonical_owner": "typescript",
                    "session_id": session_id,
                    "worker_request_id": logical_worker_request_id,
                    "logical_request_digest": logical_request_digest,
                    "session_snapshot": to_jsonable(loop_result.session_snapshot),
                    "metadata": to_jsonable(loop_result.metadata),
                },
            )
            artifacts = list(loop_result.artifacts)
            trace = self.execution_context.artifact_store.write_text(
                run_id=request.run_id,
                task_id=request.task_id,
                title=f"CodeWorker E01 trace {request.request_id}",
                kind=ArtifactKind.TRACE,
                extension=".md",
                producer_node_id=request.node_id,
                content=_trace_markdown(request, loop_result, checkpoint_path),
            )
            artifacts.append(trace)
        except Exception as error:  # noqa: BLE001 - release claimed context on host persistence failure.
            settlement_error = self._settle_retrieval_context(
                retrieval_context,
                committed=False,
                terminal_event_ids=tuple(
                    event.event_id for event in loop_result.event_records
                ),
                reason=f"code_worker_result_persistence_failed:{type(error).__name__}",
            )
            return self._failure(
                request,
                error=(
                    "retrieval_delivery_finalize_failed"
                    if settlement_error
                    else "code_worker_result_persistence_failed"
                ),
                summary=(
                    "CodeWorkerRuntime could not persist its canonical host result: "
                    f"{type(error).__name__}: {error}"
                    + (f"; retrieval settlement also failed: {settlement_error}" if settlement_error else "")
                ),
                session_id=session_id,
            )
        metadata = {
            "canonical_runtime_owner": "typescript",
            "python_runtime_role": "process-durability-side-effect-host",
            "python_policy_fallback": "false",
            "logical_worker_request_id": logical_worker_request_id,
            "runtime_protocol": "zyra.claude-runtime.v1",
            "query_session_id": str(
                loop_result.metadata.get("query_session_id") or session_id
            ),
            "query_turns": str(loop_result.turn_count),
            "tool_steps": str(loop_result.tool_call_count),
            "context_compactions": str(loop_result.context_compaction_count),
            "runtime_state_checkpoint_path": str(checkpoint_path),
            **_string_metadata(loop_result.metadata),
            **(custody.metadata() if custody is not None else {}),
        }
        error = None
        if not loop_result.ok:
            error = str(
                loop_result.metadata.get("error")
                or loop_result.stopped_reason
                or "typescript_query_runtime_failed"
            )
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=loop_result.ok,
            summary=(
                "CodeWorkerRuntime completed through the canonical TypeScript runtime."
                if loop_result.ok
                else "CodeWorkerRuntime stopped in the canonical TypeScript runtime."
            ),
            artifacts=artifacts,
            events=[to_jsonable(event) for event in loop_result.event_records],
            error=error,
            metadata=metadata,
        )
        retrieval_events = list(retrieval_context.events) if retrieval_context is not None else []
        settlement_error = self._settle_retrieval_context(
            retrieval_context,
            committed=bool(loop_result.ok),
            terminal_event_ids=tuple(
                event.event_id for event in loop_result.event_records
            ),
            reason=(
                "provider_runtime_completed"
                if loop_result.ok
                else (error or "provider_runtime_failed")
            ),
        )
        if retrieval_context is not None:
            metadata.update(retrieval_context.metadata)
        if settlement_error:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary=(
                    "CodeWorkerRuntime reached a provider outcome but could not durably "
                    f"settle retrieval delivery: {settlement_error}"
                ),
                artifacts=artifacts,
                events=[to_jsonable(event) for event in loop_result.event_records],
                error="retrieval_delivery_finalize_failed",
                metadata={
                    **metadata,
                    "provider_runtime_ok": str(bool(loop_result.ok)).lower(),
                    "retrieval_delivery_repair_required": "true",
                },
            )
        result_event = _worker_result_event(request, worker_result)
        return CodeWorkerRun(
            worker_result=worker_result,
            event_records=[*retrieval_events, *loop_result.event_records, result_event],
            session_custody_token=custody.token if custody is not None else "",
            session_id=metadata["query_session_id"],
            session_custody_id=custody.custody_id if custody is not None else "",
            session_custody_fingerprint=(
                custody.custody_fingerprint if custody is not None else ""
            ),
            session_custody_created=custody.created if custody is not None else False,
            execution_evidence=_execution_evidence(loop_result),
        )

    def _failure(
        self,
        request: WorkerRequest,
        *,
        error: str,
        summary: str,
        session_id: str,
    ) -> CodeWorkerRun:
        result = WorkerResult(
            request_id=request.request_id,
            ok=False,
            summary=summary,
            error=error,
            metadata={
                "canonical_runtime_owner": "typescript",
                "python_runtime_role": "process-durability-side-effect-host",
                "python_query_engine_fallback": "false",
                "python_policy_fallback": "false",
                "query_session_id": session_id,
            },
        )
        return CodeWorkerRun(
            worker_result=result,
            event_records=[_worker_result_event(request, result)],
            session_id=session_id,
        )

    def _settle_retrieval_context(
        self,
        context: WorkerRetrievalContext | None,
        *,
        committed: bool,
        terminal_event_ids: Sequence[str],
        reason: str,
    ) -> str:
        if context is None:
            return ""
        if self.retrieval_context_runtime is None:
            return "retrieval context exists without its delivery runtime"
        try:
            self.retrieval_context_runtime.finish(
                context,
                committed=committed,
                terminal_event_ids=terminal_event_ids,
                reason=reason,
            )
        except Exception as error:  # noqa: BLE001 - convert ambiguous settlement into a fail-closed result.
            return f"{type(error).__name__}: {error}"
        return ""

    @staticmethod
    def _session_id(request: WorkerRequest) -> str:
        return str(
            request.constraints.get("session_id")
            or request.metadata.get("session_id")
            or f"query:{request.run_id}:{request.task_id}"
        )

    @staticmethod
    def _query_turns(constraints: Mapping[str, Any]) -> list[Any]:
        raw = constraints.get("query_turns")
        if isinstance(raw, list) and raw:
            return list(raw)
        plan = constraints.get("tool_plan")
        if isinstance(plan, list) and plan:
            return [list(plan)]
        return list(raw) if isinstance(raw, list) else []

    def _state_path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        return self.runtime_state_root / f"{digest}.json"

    def _restored_state(
        self,
        session_id: str,
        constraints: Mapping[str, Any],
    ) -> dict[str, Any]:
        explicit = constraints.get("restored_runtime_state")
        if isinstance(explicit, Mapping):
            return dict(explicit)
        path = self._state_path(session_id)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    @staticmethod
    def _logical_worker_request_id(
        restored_state: Mapping[str, Any],
        *,
        request_id: str,
        request_digest: str,
    ) -> str:
        """Resume only an unfinished logical request in a durable session.

        Permission ASK deliberately has no terminal receipt so the approved
        retry keeps the exact worker binding.  Once a terminal receipt exists,
        a later API call is a new logical request; reusing the old identifier
        would recover its completed result and silently skip the new tool
        arguments.
        """

        restored_request_id = str(restored_state.get("worker_request_id") or "")
        if not restored_request_id:
            return request_id
        session_snapshot = restored_state.get("session_snapshot")
        if not isinstance(session_snapshot, Mapping):
            return restored_request_id
        terminal_receipts = session_snapshot.get("terminal_result_receipts")
        if (
            isinstance(terminal_receipts, Mapping)
            and restored_request_id in terminal_receipts
        ):
            if str(restored_state.get("logical_request_digest") or "") != request_digest:
                return request_id
        return restored_request_id

    def _persist_runtime_state(
        self,
        session_id: str,
        payload: Mapping[str, Any],
    ) -> Path:
        path = self._state_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(to_jsonable(payload), ensure_ascii=True, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            # Windows can transiently deny replacement while an antivirus or
            # checkpoint reader still owns a handle.  A request retry must not
            # fail solely because its previous suspended checkpoint is being
            # released, and concurrent sessions must never share a temp file.
            for retry in range(6):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if retry == 5:
                        raise
                    time.sleep(0.01 * (retry + 1))
        finally:
            temporary.unlink(missing_ok=True)
        return path


def _worker_result_event(request: WorkerRequest, result: WorkerResult) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE if result.ok else EventType.NODE_FAILED,
        payload={
            "worker_result": to_jsonable(result),
            "canonical_runtime_owner": "typescript",
        },
    )


def _trace_markdown(request: WorkerRequest, result: Any, checkpoint_path: Path) -> str:
    return "\n".join(
        (
            "# CodeWorker E01 TypeScript Runtime Trace",
            "",
            f"- request_id: `{request.request_id}`",
            f"- run_id: `{request.run_id}`",
            f"- task_id: `{request.task_id}`",
            "- canonical_runtime_owner: `typescript`",
            "- python_runtime_role: `process-durability-side-effect-host`",
            "- python_policy_fallback: `false`",
            f"- ok: `{str(bool(result.ok)).lower()}`",
            f"- turns: `{result.turn_count}`",
            f"- tool_calls: `{result.tool_call_count}`",
            f"- compactions: `{result.context_compaction_count}`",
            f"- checkpoint: `{checkpoint_path}`",
            "",
            "## Step summaries",
            "",
            *(f"- {item}" for item in result.step_summaries),
        )
    )


def _execution_evidence(result: Any) -> dict[str, Any]:
    """Project truthful provider/tool facts from the canonical TS result.

    The projection deliberately contains no estimated token counts.  Usage is
    summed only from successful ``model_stream_report`` frames emitted after a
    provider response.  The physical dispatch layer may enrich the request ids
    from the provider-control-plane journal, but must not invent missing data.
    """

    snapshot = _mapping(getattr(result, "session_snapshot", {}))
    typescript_snapshot = _mapping(snapshot.get("typescript_runtime_snapshot"))
    model_iteration = _mapping(typescript_snapshot.get("modelIteration"))
    final_text = str(model_iteration.get("finalText") or "").strip()
    provider_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "server_tool_use_tokens": 0,
        "total_tokens": 0,
    }
    for event in list(getattr(result, "event_records", ()) or ()):
        event_payload = getattr(event, "payload", {})
        if not isinstance(event_payload, Mapping):
            continue
        query = event_payload.get("query_session")
        if not isinstance(query, Mapping):
            continue
        phase = str(query.get("phase") or "")
        if phase == "model_stream_report":
            report = query.get("model_stream")
            if not isinstance(report, Mapping):
                continue
            raw_usage = report.get("usage")
            raw_observed_usage = (
                dict(raw_usage) if isinstance(raw_usage, Mapping) else {}
            )
            observed_usage = {
                **raw_observed_usage,
                "input_tokens": int(
                    raw_observed_usage.get("input_tokens")
                    or raw_observed_usage.get("prompt_tokens")
                    or 0
                ),
                "output_tokens": int(
                    raw_observed_usage.get("output_tokens")
                    or raw_observed_usage.get("completion_tokens")
                    or 0
                ),
                "cache_read_input_tokens": int(
                    raw_observed_usage.get("cache_read_input_tokens")
                    or raw_observed_usage.get("cached_prompt_tokens")
                    or 0
                ),
            }
            observed_usage["total_tokens"] = max(
                int(observed_usage.get("total_tokens") or 0),
                int(observed_usage["input_tokens"])
                + int(observed_usage["output_tokens"]),
            )
            call = {
                "request_id": str(
                    report.get("provider_request_id")
                    or report.get("request_id")
                    or ""
                ),
                "provider_id": str(report.get("provider") or ""),
                "model_id": str(report.get("model") or ""),
                "route_id": str(report.get("route_id") or ""),
                "transport": str(report.get("transport") or ""),
                "http_status": int(report.get("status") or 0),
                "ok": report.get("ok") is True,
                "usage": observed_usage,
                "stop_reason": str(report.get("stop_reason") or ""),
                "attempt_count": int(report.get("attempt_count") or 0),
                "frame_count": int(report.get("frame_count") or 0),
                "tool_call_count": int(report.get("tool_call_count") or 0),
            }
            provider_calls.append(call)
            if call["ok"] and call["request_id"]:
                for name in tuple(usage):
                    if name == "total_tokens":
                        continue
                    try:
                        usage[name] += max(0, int(observed_usage.get(name) or 0))
                    except (TypeError, ValueError):
                        continue
        if phase == "tool_result":
            raw_result = query.get("tool_result")
            if isinstance(raw_result, Mapping):
                tool_results.append(
                    {
                        "tool_call_id": str(raw_result.get("tool_call_id") or ""),
                        "tool_name": str(raw_result.get("tool_name") or ""),
                        "ok": raw_result.get("ok") is True,
                        "error": str(raw_result.get("error") or ""),
                        "summary": str(raw_result.get("summary") or "")[:500],
                        "output_digest": hashlib.sha256(
                            json.dumps(
                                to_jsonable(raw_result.get("output") or {}),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                    }
                )
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    actual_calls = [
        item
        for item in provider_calls
        if item["ok"]
        and item["request_id"]
        and item["transport"] in {"provider_control_plane", "http_sse"}
        and item["provider_id"] not in {"", "local", "zyra-sim"}
    ]
    return {
        "schema": "zyra.code-worker-execution-evidence/v1",
        "canonical_runtime_owner": "typescript",
        "model_reasoning_loop": True,
        "provider_called": bool(actual_calls),
        "provider_calls": provider_calls,
        "usage": usage,
        "final_text": final_text,
        "tool_results": tool_results,
        "tool_call_count": int(getattr(result, "tool_call_count", 0) or 0),
        "turn_count": int(getattr(result, "turn_count", 0) or 0),
        "artifact_ids": [
            str(getattr(item, "artifact_id", "") or "")
            for item in list(getattr(result, "artifacts", ()) or ())
            if str(getattr(item, "artifact_id", "") or "")
        ],
        "synthetic_usage": False,
    }


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any, default: int) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else default


def _bounded_positive_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        return default
    return parsed


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        result = tuple(item.strip() for item in value.split(",") if item.strip())
        return result or default
    if isinstance(value, Sequence):
        result = tuple(str(item).strip() for item in value if str(item).strip())
        return result or default
    return default


def _string_metadata(value: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, str):
            result[str(key)] = item
        elif isinstance(item, (bool, int, float)) or item is None:
            result[str(key)] = str(item).lower() if isinstance(item, bool) else str(item or "")
        else:
            result[str(key)] = json.dumps(to_jsonable(item), ensure_ascii=True, sort_keys=True)
    return result
