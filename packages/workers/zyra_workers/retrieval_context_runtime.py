from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from zyra_code_index import (
    CodeIndexConsumer,
    CodeIndexIntegrationRuntime,
    CodeIndexQuery,
    CodeIndexSelection,
)
from zyra_core import AgentMessage, AgentRole, EventRecord, EventType, MessageIntent
from zyra_memory import (
    MemoryFilterQuery,
    RetrievalConsumer,
    RetrievalExecution,
    RetrievalIntegrationRuntime,
)
from zyra_runtime.workers import WorkerRequest


@dataclass(frozen=True, slots=True)
class WorkerRetrievalContext:
    worker_request_id: str
    messages: tuple[AgentMessage, ...]
    constraint_delta: Mapping[str, Any]
    metadata: Mapping[str, str]
    events: tuple[EventRecord, ...]
    memory_execution: RetrievalExecution | None = None
    code_selection: CodeIndexSelection | None = None
    delivery_claimed: bool = False
    recovery_reference: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerRetrievalRecoveryReference:
    """Reference-only checkpoint for exact retrieval re-execution."""

    task_id: str
    worker_request_id: str
    session_id: str
    memory: Mapping[str, Any]
    code: Mapping[str, Any]
    message_ids: tuple[str, ...]
    source_digest: str

    def validated(self) -> "WorkerRetrievalRecoveryReference":
        if not self.task_id or not self.worker_request_id or not self.session_id:
            raise ValueError("retrieval recovery reference requires task/request/session identity")
        if not self.source_digest:
            raise ValueError("retrieval recovery reference requires source_digest")
        body = json.dumps(
            {"memory": self.memory, "code": self.code},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).casefold()
        forbidden = (
            "retrieval_fts",
            "index_rows",
            "physical_root",
            "sqlite_path",
        )
        present = [value for value in forbidden if value in body]
        if present:
            raise ValueError(
                "retrieval recovery reference contains forbidden state: " + ", ".join(present)
            )
        if bool(self.memory.get("contains_index_dump")) or bool(
            self.memory.get("contains_canonical_records")
        ):
            raise ValueError("memory recovery reference contains embedded state")
        if bool(self.code.get("index_dump_embedded")) or bool(
            self.code.get("workspace_content_embedded")
        ):
            raise ValueError("code recovery reference contains embedded state")
        return self

    def to_dict(self) -> dict[str, Any]:
        value = self.validated()
        return {
            "schema": "zyra.worker-retrieval-recovery-reference.v1",
            "task_id": value.task_id,
            "worker_request_id": value.worker_request_id,
            "session_id": value.session_id,
            "memory": dict(value.memory),
            "code": dict(value.code),
            "message_ids": list(value.message_ids),
            "source_digest": value.source_digest,
            "contains_index_dump": False,
            "contains_canonical_records": False,
            "contains_workspace_content": False,
        }


class WorkerRetrievalContextRuntime:
    """Bind derived retrieval snapshots to one CodeWorker invocation.

    This runtime does not own Claude context/session state.  It produces
    ordinary AgentMessages plus bounded constraints for the canonical
    TypeScript QueryEngine, then commits or releases the delivery journal
    against the worker's terminal events.
    """

    def __init__(
        self,
        *,
        memory: RetrievalIntegrationRuntime,
        code: CodeIndexIntegrationRuntime,
        memory_maximum_results: int = 10,
        memory_maximum_chars: int = 16_000,
        code_maximum_files: int = 16,
        code_maximum_tests: int = 100,
        code_maximum_chars: int = 20_000,
    ) -> None:
        self.memory = memory
        self.code = code
        self.memory_maximum_results = max(0, memory_maximum_results)
        self.memory_maximum_chars = max(0, memory_maximum_chars)
        self.code_maximum_files = max(0, code_maximum_files)
        self.code_maximum_tests = max(0, code_maximum_tests)
        self.code_maximum_chars = max(0, code_maximum_chars)

    def prepare(self, request: WorkerRequest, *, session_id: str) -> WorkerRetrievalContext:
        query_text = self._query_text(request)
        if not query_text:
            raise ValueError("CodeWorker retrieval requires a non-empty request message")
        memory_execution, memory_block = self.memory.context(
            MemoryFilterQuery(
                task_id=request.task_id,
                text=query_text,
                consumer=RetrievalConsumer.CODE_WORKER_CONTEXT,
                run_id=request.run_id,
                # Query-session identity is delivery provenance, not an
                # implicit hard filter. Long-lived task memory commonly
                # predates the current Claude session; callers that require a
                # session intersection use MemoryFilterQuery directly.
                session_id="",
                maximum_results=self.memory_maximum_results,
                maximum_chars=self.memory_maximum_chars,
                request_id=request.request_id,
                causation_id=request.request_id,
                metadata={
                    "worker_name": request.worker_name,
                    "consumer_session_id": session_id,
                },
            )
        )
        changed_paths = self._changed_paths(request)
        code_selection = self.code.select(
            CodeIndexQuery(
                task_id=request.task_id,
                text=query_text,
                consumer=CodeIndexConsumer.CODE_WORKER_CONTEXT,
                run_id=request.run_id,
                session_id=session_id,
                worker_request_id=request.request_id,
                changed_paths=changed_paths,
                maximum_files=self.code_maximum_files,
                maximum_tests=self.code_maximum_tests,
                maximum_chars=self.code_maximum_chars,
                request_id=request.request_id,
                causation_id=request.request_id,
                metadata={"worker_name": request.worker_name},
            )
        )
        messages = self._messages(request, memory_block.to_dict(), code_selection)
        source_digest = _stable_json_digest(
            {
                "memory_query_id": memory_execution.snapshot.query_id,
                "memory_result": memory_execution.snapshot.result_digest,
                "code_query_id": code_selection.request.query_id,
                "code_result": code_selection.result_digest,
                "message_ids": [message.message_id for message in messages],
            }
        )
        code_message_id = next(
            (message.message_id for message in messages if message.metadata.get("retrieval_scope") == "task_workspace"),
            messages[-1].message_id,
        )
        try:
            self.memory.store.claim_delivery(
                worker_request_id=request.request_id,
                run_id=request.run_id,
                task_id=request.task_id,
                session_id=session_id,
                query_ids=(memory_execution.snapshot.query_id,),
                message_ids=tuple(message.message_id for message in messages),
                source_digest=source_digest,
                metadata={
                    "consumer": RetrievalConsumer.CODE_WORKER_CONTEXT.value,
                    "code_query_id": code_selection.request.query_id,
                    "code_result_digest": code_selection.result_digest,
                },
            )
            self.code.journal.claim_delivery(code_selection, message_id=code_message_id)
            recovery_reference = WorkerRetrievalRecoveryReference(
                task_id=request.task_id,
                worker_request_id=request.request_id,
                session_id=session_id,
                memory=self.memory.checkpoint_ref(request.task_id).to_dict(),
                code=code_selection.recovery_reference(),
                message_ids=tuple(message.message_id for message in messages),
                source_digest=source_digest,
            ).to_dict()
        except Exception as error:
            cleanup_errors = self._compensate_prepare(
                worker_request_id=request.request_id,
                source_digest=source_digest,
                code_query_id=code_selection.request.query_id,
                code_result_digest=code_selection.result_digest,
                reason=f"retrieval_context_prepare_failed:{type(error).__name__}",
            )
            if cleanup_errors:
                raise RuntimeError(
                    "retrieval context prepare failed and compensation was incomplete: "
                    + "; ".join(cleanup_errors)
                ) from error
            raise
        event = EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "retrieval_context": {
                    "schema": "zyra.worker-retrieval-context.v1",
                    "worker_request_id": request.request_id,
                    "session_id": session_id,
                    "message_ids": [message.message_id for message in messages],
                    "memory_query_id": memory_execution.snapshot.query_id,
                    "memory_generation": memory_execution.snapshot.index_generation,
                    "memory_source_refs": [ref.to_dict() for ref in memory_execution.snapshot.source_refs],
                    "code_query_id": code_selection.request.query_id,
                    "code_generation": code_selection.generation,
                    "code_source_refs": [ref.to_dict() for ref in code_selection.source_refs],
                    "selected_files": list(code_selection.selected_files),
                    "selected_tests": list(code_selection.selected_tests),
                    "canonical_context_owner": "typescript",
                    "derived_delivery_owner": "WorkerRetrievalContextRuntime",
                    "recovery_reference": recovery_reference,
                }
            },
        )
        return WorkerRetrievalContext(
            worker_request_id=request.request_id,
            messages=messages,
            constraint_delta={
                **memory_block.as_worker_constraints(),
                "retrieval_context_enabled": True,
                "retrieval_context_schema": "zyra.worker-retrieval-context.v1",
                "code_index_query_id": code_selection.request.query_id,
                "code_index_generation": code_selection.generation,
                "code_index_selected_files": list(code_selection.selected_files),
                "code_index_selected_tests": list(code_selection.selected_tests),
                "code_index_test_reasons": {
                    key: list(value) for key, value in code_selection.test_reasons.items()
                },
                "retrieval_recovery_reference": recovery_reference,
            },
            metadata={
                "memory_retrieval_query_id": memory_execution.snapshot.query_id,
                "memory_retrieval_generation": str(memory_execution.snapshot.index_generation),
                "code_index_query_id": code_selection.request.query_id,
                "code_index_generation": str(code_selection.generation),
                "retrieval_context_source_digest": source_digest,
            },
            events=(event,),
            memory_execution=memory_execution,
            code_selection=code_selection,
            delivery_claimed=True,
            recovery_reference=recovery_reference,
        )

    def finish(
        self,
        context: WorkerRetrievalContext,
        *,
        committed: bool,
        terminal_event_ids: Sequence[str],
        reason: str,
    ) -> None:
        if not context.delivery_claimed:
            return
        event_ids = tuple(dict.fromkeys(str(value) for value in terminal_event_ids if str(value)))
        final_reason = reason or (
            "provider_runtime_completed" if committed else "worker_execution_failed"
        )
        errors: list[str] = []
        try:
            if committed:
                self.memory.store.complete_delivery(
                    context.worker_request_id,
                    terminal_event_ids=event_ids,
                )
            else:
                self.memory.store.release_delivery(
                    context.worker_request_id,
                    reason=final_reason,
                    terminal_event_ids=event_ids,
                )
        except Exception as error:  # noqa: BLE001 - the peer journal must still settle.
            errors.append(f"memory:{type(error).__name__}:{error}")
        try:
            self.code.journal.finish_delivery(
                context.worker_request_id,
                committed=committed,
                terminal_event_ids=event_ids,
                reason=final_reason,
            )
        except Exception as error:  # noqa: BLE001 - report both durable journal failures.
            errors.append(f"code:{type(error).__name__}:{error}")
        if errors:
            raise RuntimeError(
                "retrieval delivery finalization incomplete; retry the same desired state: "
                + "; ".join(errors)
            )

    def _compensate_prepare(
        self,
        *,
        worker_request_id: str,
        source_digest: str,
        code_query_id: str,
        code_result_digest: str,
        reason: str,
    ) -> tuple[str, ...]:
        """Release only claims created/reclaimed for this exact snapshot."""

        errors: list[str] = []
        try:
            memory_delivery = self.memory.store.delivery(worker_request_id)
            if (
                memory_delivery is not None
                and memory_delivery.state.value == "claimed"
                and memory_delivery.source_digest == source_digest
            ):
                self.memory.store.release_delivery(
                    worker_request_id,
                    reason=reason,
                )
        except Exception as error:  # noqa: BLE001 - continue compensating the peer journal.
            errors.append(f"memory:{type(error).__name__}:{error}")
        try:
            code_delivery = self.code.journal.delivery(worker_request_id)
            if (
                code_delivery is not None
                and str(code_delivery.get("state") or "") == "claimed"
                and str(code_delivery.get("query_id") or "") == code_query_id
                and str(code_delivery.get("result_digest") or "") == code_result_digest
            ):
                self.code.journal.finish_delivery(
                    worker_request_id,
                    committed=False,
                    terminal_event_ids=(),
                    reason=reason,
                )
        except Exception as error:  # noqa: BLE001 - surface incomplete compensation to the caller.
            errors.append(f"code:{type(error).__name__}:{error}")
        return tuple(errors)

    @staticmethod
    def _query_text(request: WorkerRequest) -> str:
        parts = [message.content.strip() for message in request.messages if message.content.strip()]
        if parts:
            return "\n\n".join(parts[-8:])[-24_000:]
        # AgentTool/API requests encode the current instruction in the
        # canonical tool turn rather than a user AgentMessage.
        turns = request.constraints.get("query_turns") or request.constraints.get("tool_plan") or ()
        if turns:
            return json.dumps(turns, ensure_ascii=False, sort_keys=True, default=str)[-24_000:]
        return str(request.metadata.get("goal") or request.metadata.get("instruction") or "").strip()

    @staticmethod
    def _changed_paths(request: WorkerRequest) -> tuple[str, ...]:
        values = request.constraints.get("changed_paths") or request.metadata.get("changed_paths") or ()
        if isinstance(values, str):
            values = (values,)
        return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

    @staticmethod
    def _messages(
        request: WorkerRequest,
        memory_block: Mapping[str, Any],
        code_selection: CodeIndexSelection,
    ) -> tuple[AgentMessage, ...]:
        messages: list[AgentMessage] = []
        entries = list(memory_block.get("entries") or ())
        if entries:
            lines = ["Retrieved canonical task memory (derived index; verify source refs):"]
            for entry in entries:
                lines.append(
                    f"- [{entry['layer']}] {entry['title']}\n  {entry['content']}"
                )
            messages.append(
                AgentMessage(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    sender_role=AgentRole.MEMORY,
                    receiver_role=AgentRole.WORKER,
                    intent=MessageIntent.OBSERVATION,
                    message_id="msg_retrieval_memory_"
                    + _stable_json_digest(
                        {
                            "worker_request_id": request.request_id,
                            "query_id": memory_block.get("query_id"),
                        }
                    )[:24],
                    content="\n".join(lines),
                    summary="Retrieved task memory for the current CodeWorker request.",
                    message_budget_chars=max(1, int(memory_block.get("total_chars") or 0)),
                    metadata={
                        "schema": "zyra.retrieval-context-message.v1",
                        "retrieval_scope": "task_memory",
                        "query_id": str(memory_block.get("query_id") or ""),
                        "index_generation": int(memory_block.get("index_generation") or 0),
                        "canonical_owner": "MemoryRecordStore",
                    },
                )
            )
        if code_selection.source_refs or code_selection.selected_files or code_selection.selected_tests:
            lines = ["Retrieved code context from the current fenced workspace revision:"]
            for ref, excerpt in zip(code_selection.source_refs, code_selection.excerpts, strict=True):
                lines.append(
                    f"- {ref.logical_path}:{ref.line_start}-{ref.line_end}\n{excerpt}"
                )
            if code_selection.selected_tests:
                lines.append("Selected tests: " + ", ".join(code_selection.selected_tests))
            messages.append(
                AgentMessage(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    sender_role=AgentRole.MEMORY,
                    receiver_role=AgentRole.WORKER,
                    intent=MessageIntent.OBSERVATION,
                    message_id="msg_retrieval_code_"
                    + _stable_json_digest(
                        {
                            "worker_request_id": request.request_id,
                            "query_id": code_selection.request.query_id,
                        }
                    )[:24],
                    content="\n".join(lines),
                    summary="Retrieved current workspace code and test candidates.",
                    message_budget_chars=max(1, code_selection.total_chars),
                    metadata={
                        "schema": "zyra.code-context-message.v1",
                        "retrieval_scope": "task_workspace",
                        "query_id": code_selection.request.query_id,
                        "workspace_revision": code_selection.workspace_revision,
                        "generation": code_selection.generation,
                        "selected_files": list(code_selection.selected_files),
                        "selected_tests": list(code_selection.selected_tests),
                        "canonical_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
                    },
                )
            )
        if not messages:
            messages.append(
                AgentMessage(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    sender_role=AgentRole.MEMORY,
                    receiver_role=AgentRole.WORKER,
                    intent=MessageIntent.OBSERVATION,
                    message_id="msg_retrieval_empty_"
                    + _stable_json_digest(
                        {
                            "worker_request_id": request.request_id,
                            "memory_query_id": memory_block.get("query_id"),
                            "code_query_id": code_selection.request.query_id,
                        }
                    )[:24],
                    content=(
                        "Current retrieval snapshots contained no matching task memory "
                        "or code excerpts. Do not infer context from stale caches or scan "
                        "the workspace as an implicit fallback."
                    ),
                    summary="Current retrieval completed with no matching context.",
                    message_budget_chars=256,
                    metadata={
                        "schema": "zyra.retrieval-context-message.v1",
                        "retrieval_scope": "empty_current_snapshot",
                        "memory_query_id": str(memory_block.get("query_id") or ""),
                        "code_query_id": code_selection.request.query_id,
                        "code_generation": code_selection.generation,
                    },
                )
            )
        return tuple(messages)


def _stable_json_digest(value: Mapping[str, Any]) -> str:
    import hashlib

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "WorkerRetrievalContext",
    "WorkerRetrievalContextRuntime",
    "WorkerRetrievalRecoveryReference",
]
