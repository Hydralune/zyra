from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
from zyra_runtime import QuerySession, QueryStreamEventType, StopReason
from zyra_runtime.claude_query_engine_runtime import (
    ClaudeQueryEngineConfig,
    ClaudeQueryEngineResult,
)
from zyra_runtime.executor import ToolExecutionContext, ToolExecutor
from zyra_runtime.permission import (
    PermissionEvaluationRequest,
    PermissionMode,
    ToolIdentity,
)
from zyra_runtime.permission.runtime import (
    PermissionRuntimeConfig,
    ToolPermissionRuntime,
)
from zyra_runtime.tools import ToolCall, ToolResult

from .code_worker_bridge import code_worker_entrypoint


RUNTIME_PROTOCOL_VERSION = "zyra.claude-runtime.v1"
TYPESCRIPT_RUNTIME_ID = "zyra-typescript-claude-runtime"


class TypeScriptRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ProcessLineReader:
    def __init__(self, stream: Any) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def get(self, timeout: float) -> str | None:
        try:
            return self._lines.get(timeout=max(0.01, timeout))
        except queue.Empty as error:
            raise TypeScriptRuntimeError(
                "typescript_runtime_timeout",
                "TypeScript runtime did not produce a protocol frame before the deadline.",
            ) from error

    def _read(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._lines.put(line)
        finally:
            self._lines.put(None)


class TypeScriptClaudeQueryEngine:
    """Canonical TypeScript query runtime with a narrow Python side-effect host.

    This preserves the QueryEngine factory protocol expected by CodeWorker while
    refusing to invoke the historical Python query loop as a fallback.
    """

    def __init__(
        self,
        context: ToolExecutionContext,
        config: ClaudeQueryEngineConfig | None = None,
    ) -> None:
        self.context = context
        self.config = config or ClaudeQueryEngineConfig()
        self.project_root = Path(self.config.project_root or Path.cwd()).resolve()
        self.entrypoint = code_worker_entrypoint(self.project_root)
        self._host_events: list[EventRecord] = []
        self._host_artifacts: list[Any] = []

    def run(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        turns: Sequence[Any],
        request_messages: Sequence[Any] = (),
        request_metadata: Mapping[str, Any] | None = None,
    ) -> ClaudeQueryEngineResult:
        session_id = self._session_id(run_id, request_metadata)
        projection = QuerySession(
            run_id=run_id,
            task_id=task_id,
            worker_request_id=worker_request_id,
            node_id=node_id,
            session_id=session_id,
            source_contract={
                "source": "zyra-typescript-runtime",
                "runtime_id": TYPESCRIPT_RUNTIME_ID,
                "canonical_owner": "typescript",
                "projection_owner": "python-noncanonical",
            },
            metadata={
                "canonical_runtime_owner": "typescript",
                "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
            },
        )
        try:
            return self._run_process(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                session_id=session_id,
                turns=turns,
                request_messages=request_messages,
                request_metadata=request_metadata or {},
                projection=projection,
            )
        except TypeScriptRuntimeError as error:
            return self._failed_result(
                error=error,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                projection=projection,
            )
        except Exception as error:  # noqa: BLE001 - process boundary fails closed.
            return self._failed_result(
                error=TypeScriptRuntimeError(
                    "typescript_runtime_process_failed",
                    f"{type(error).__name__}: {error}",
                ),
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                projection=projection,
            )

    def _run_process(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        turns: Sequence[Any],
        request_messages: Sequence[Any],
        request_metadata: Mapping[str, Any],
        projection: QuerySession,
    ) -> ClaudeQueryEngineResult:
        constraints = dict(self.config.runtime_constraints)
        if (
            os.environ.get("ZYRA_DISABLE_TYPESCRIPT_RUNTIME") == "1"
            or constraints.get("disable_typescript_runtime") is True
        ):
            raise TypeScriptRuntimeError(
                "typescript_runtime_disabled",
                "The canonical TypeScript query runtime is disabled.",
            )
        command, transport = self._runtime_command()
        timeout_seconds = min(
            600.0,
            max(1.0, float(constraints.get("typescript_runtime_timeout_seconds") or 120.0)),
        )
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=self._runtime_environment(),
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                "TypeScript runtime process pipes were not created.",
            )
        reader = _ProcessLineReader(process.stdout)
        deadline = time.monotonic() + timeout_seconds
        outbound_sequence = 1
        inbound_sequence = 1
        permission_runtime = self._permission_runtime(
            session_id=session_id,
            restored_state=self.config.restored_runtime_state or {},
        )
        executor = ToolExecutor(
            self.context,
            permission_authority=permission_runtime,
        )
        self._write_frame(
            process,
            run_id=run_id,
            sequence=outbound_sequence,
            kind="run.start",
            payload={
                "task_id": task_id,
                "node_id": node_id,
                "worker_request_id": worker_request_id,
                "session_id": session_id,
                "messages": to_jsonable(list(request_messages)),
                "turns": to_jsonable(list(turns)),
                "tools": [to_jsonable(spec) for spec in self.context.registry.list()],
                "config": self._typescript_config(),
                "session_seed": to_jsonable(self.config.session_seed or {}),
                "context_snapshot": to_jsonable(self.config.context_snapshot or {}),
                "restored_state": to_jsonable(self.config.restored_runtime_state or {}),
                "metadata": to_jsonable(dict(request_metadata)),
            },
        )
        outbound_sequence += 1
        if constraints.get("kill_typescript_runtime_after_start") is True:
            process.kill()

        accepted = self._read_frame(
            reader,
            process,
            run_id=run_id,
            expected_sequence=inbound_sequence,
            deadline=deadline,
        )
        inbound_sequence += 1
        if accepted["kind"] != "run.accepted":
            self._terminate(process)
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime did not acknowledge run.start.",
            )

        result_payload: dict[str, Any] | None = None
        projection_error = ""
        while result_payload is None:
            frame = self._read_frame(
                reader,
                process,
                run_id=run_id,
                expected_sequence=inbound_sequence,
                deadline=deadline,
            )
            inbound_sequence += 1
            kind = str(frame["kind"])
            correlation_id = str(frame.get("correlation_id") or "")
            payload = dict(frame.get("payload") or {})
            if kind == "runtime.event":
                self._host_events.append(
                    self._event_record(
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        payload=payload,
                    )
                )
                if not projection_error:
                    try:
                        self._project_event(projection, payload)
                    except Exception as error:  # noqa: BLE001 - noncanonical compatibility projection.
                        projection_error = f"{type(error).__name__}: {error}"
                continue
            if kind == "tool.request":
                tool_result = self._execute_tool_request(
                    payload=payload,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    worker_request_id=worker_request_id,
                    session_id=session_id,
                    executor=executor,
                    permission_runtime=permission_runtime,
                )
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="tool.result",
                    payload=tool_result,
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                continue
            if kind == "artifact.request":
                artifact = self._write_artifact(
                    payload=payload,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                )
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="artifact.result",
                    payload={"artifact": to_jsonable(artifact)},
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                continue
            if kind == "runtime.error":
                self._terminate(process)
                raise TypeScriptRuntimeError(
                    str(payload.get("code") or "typescript_runtime_process_failed"),
                    str(payload.get("message") or "TypeScript runtime reported an error."),
                )
            if kind == "run.result":
                result_payload = dict(payload.get("result") or {})
                break
            self._terminate(process)
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                f"Unexpected TypeScript runtime frame: {kind}",
            )

        process.stdin.close()
        try:
            exit_code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            self._terminate(process)
            raise TypeScriptRuntimeError(
                "typescript_runtime_timeout",
                "TypeScript runtime did not exit after run.result.",
            ) from error
        stderr = process.stderr.read().strip()
        if exit_code != 0:
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                stderr or f"TypeScript runtime exited with status {exit_code}.",
            )

        ok = result_payload.get("ok") is True
        stopped_reason = str(result_payload.get("stoppedReason") or "") or None
        if projection.active_turn is not None:
            projection.end_turn(
                ok=ok,
                stop_reason=self._stop_reason(stopped_reason),
                error=None if ok else stopped_reason,
                metadata={"canonical_owner": "typescript"},
            )
        projection.complete_session(
            ok=ok,
            stop_reason=self._stop_reason(stopped_reason, complete=ok),
            metadata={
                "canonical_owner": "typescript",
                "typescript_runtime_id": TYPESCRIPT_RUNTIME_ID,
                "typescript_runtime_snapshot": dict(result_payload.get("sessionSnapshot") or {}),
                "projection_error": projection_error,
            },
        )
        session_snapshot = to_jsonable(projection.snapshot())
        snapshot_artifact = self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(
                session_snapshot,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            title="CodeWorker QuerySession Snapshot",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=node_id,
        )
        self._host_artifacts.append(snapshot_artifact)
        transcript_lines = [
            json.dumps(
                {
                    "type": "session_metadata",
                    "session_id": session_id,
                    "canonical_runtime_owner": "typescript",
                    "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        ]
        for event in self._host_events:
            event_payload = getattr(event, "payload", {})
            query_event = (
                event_payload.get("query_session")
                if isinstance(event_payload, Mapping)
                else None
            )
            if not isinstance(query_event, Mapping):
                continue
            transcript_lines.append(
                json.dumps(
                    {
                        "type": "runtime_event",
                        "event_type": str(query_event.get("phase") or "runtime_event"),
                        "canonical_runtime_owner": "typescript",
                        **to_jsonable(dict(query_event)),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        transcript_artifact = self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content="\n".join(transcript_lines) + "\n",
            title="CodeWorker TypeScript Runtime Transcript",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".jsonl",
            producer_node_id=node_id,
        )
        self._host_artifacts.append(transcript_artifact)
        snapshot_stats = dict(session_snapshot.get("stats") or {})
        snapshot_consistency = dict(session_snapshot.get("consistency") or {})
        metadata = {
            str(key): str(value)
            for key, value in dict(result_payload.get("metadata") or {}).items()
        }
        metadata.update(
            {
                "loop": "zyra_typescript_query_engine_runtime",
                "canonical_runtime_owner": "typescript",
                "typescript_runtime_id": TYPESCRIPT_RUNTIME_ID,
                "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
                "runtime_transport": transport,
                "python_query_engine_fallback": "false",
                "python_session_projection_canonical": "false",
                "query_session_id": session_id,
                "projection_error": projection_error,
                "query_session_snapshot_artifact_id": snapshot_artifact.artifact_id,
                "query_session_transcript_artifact_id": transcript_artifact.artifact_id,
                "query_session_checkpoint_ready": str(
                    snapshot_consistency.get("ok") is True
                ).lower(),
                "query_session_turns": str(
                    snapshot_stats.get("turn_count")
                    or result_payload.get("turnCount")
                    or 0
                ),
                "query_session_consistent": str(
                    snapshot_consistency.get("ok") is True
                ).lower(),
                "query_session_resume_token": str(
                    session_snapshot.get("resume_token") or session_id
                ),
            }
        )
        result_artifacts = [
            self._artifact_from_mapping(item)
            for item in list(result_payload.get("artifacts") or [])
            if isinstance(item, Mapping)
        ]
        return ClaudeQueryEngineResult(
            ok=ok,
            event_records=list(self._host_events),
            artifacts=self._dedupe_artifacts([*self._host_artifacts, *result_artifacts]),
            step_summaries=[
                str(item) for item in list(result_payload.get("stepSummaries") or [])
            ],
            turn_count=int(result_payload.get("turnCount") or 0),
            tool_call_count=int(result_payload.get("toolCallCount") or 0),
            context_compaction_count=int(
                result_payload.get("contextCompactionCount") or 0
            ),
            stopped_reason=stopped_reason,
            session_snapshot=session_snapshot,
            metadata=metadata,
        )

    def _runtime_command(self) -> tuple[list[str], str]:
        if not self.entrypoint.exists():
            raise TypeScriptRuntimeError(
                "typescript_runtime_unavailable",
                f"TypeScript runtime entrypoint is missing: {self.entrypoint}",
            )
        bun = shutil.which("bun")
        if bun:
            return [bun, str(self.entrypoint), "--stdio"], "bun"
        node = shutil.which("node")
        if node:
            return [
                node,
                "--experimental-strip-types",
                str(self.entrypoint),
                "--stdio",
            ], "node-strip-types"
        raise TypeScriptRuntimeError(
            "typescript_runtime_unavailable",
            "Neither Bun nor a TypeScript-capable Node runtime is available.",
        )

    def _runtime_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("NODE_PATH", None)
        environment["ZYRA_TYPESCRIPT_RUNTIME_OWNER"] = "canonical"
        environment["ZYRA_TYPESCRIPT_RUNTIME_PROTOCOL"] = RUNTIME_PROTOCOL_VERSION
        return environment

    def _typescript_config(self) -> dict[str, Any]:
        return {
            "maxTurns": self.config.max_turns,
            "maxToolResultChars": self.config.max_tool_result_chars,
            "maxTurnToolResultChars": self.config.max_turn_tool_result_chars,
            "maxQueryContextChars": self.config.max_query_context_chars,
            "continueOnError": self.config.continue_on_error,
            "maxReadOnlyConcurrency": self.config.max_read_only_concurrency,
            "emitToolUseSummaries": self.config.emit_tool_use_summaries,
            "allowEmptyTurns": self.config.allow_empty_turns,
            "modelName": self.config.model_name,
            "runtimeConstraints": to_jsonable(dict(self.config.runtime_constraints)),
            "controlCommands": to_jsonable(list(self.config.control_commands)),
        }

    def _permission_runtime(
        self,
        *,
        session_id: str,
        restored_state: Mapping[str, Any],
    ) -> ToolPermissionRuntime:
        state_path = Path(
            self.config.permission_state_path
            or self.project_root / ".zyra-runtime" / "permission-state.json"
        )
        state_path.parent.mkdir(parents=True, exist_ok=True)
        restored_permission = restored_state.get("permission_runtime")
        restored_snapshot = (
            dict(restored_permission)
            if isinstance(restored_permission, Mapping)
            else None
        )
        seed = dict(self.config.session_seed or {})
        foundation_metadata = dict(self.config.session_foundation_metadata or {})
        custody_fingerprint = str(
            seed.get("custody_fingerprint")
            or foundation_metadata.get("custody_fingerprint")
            or foundation_metadata.get("permission_session_custody_fingerprint")
            or ""
        )
        return ToolPermissionRuntime.for_session(
            session_id=session_id,
            state_path=state_path,
            config=PermissionRuntimeConfig(
                mode=self.config.permission_mode,
                approval_ttl_seconds=self.config.permission_approval_ttl_seconds,
                execution_grant_ttl_seconds=self.config.permission_execution_grant_ttl_seconds,
                bypass_available=self.config.permission_bypass_available,
                auto_available=self.config.permission_auto_available,
                interactive=self.config.permission_interactive,
                headless=self.config.permission_headless,
                disabled=self.config.disable_tool_permission_runtime,
                disable_rule_store=self.config.disable_permission_rule_store,
                disable_request_queue=self.config.disable_permission_request_queue,
                disable_decision_log=self.config.disable_permission_decision_log,
            ),
            restored_snapshot=restored_snapshot,
            workspace_root=self.context.workspace_root,
            custody_fingerprint=custody_fingerprint,
        )

    def _execute_tool_request(
        self,
        *,
        payload: Mapping[str, Any],
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        executor: ToolExecutor,
        permission_runtime: ToolPermissionRuntime,
    ) -> dict[str, Any]:
        tool_name = str(payload.get("tool_name") or "")
        tool_call_id = str(payload.get("tool_call_id") or "")
        arguments = dict(payload.get("arguments") or {})
        metadata = {
            str(key): str(value)
            for key, value in dict(payload.get("metadata") or {}).items()
        }
        spec = self.context.registry.get(tool_name)
        if spec is None:
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    summary=f"Unknown tool: {tool_name}",
                    error="unknown_tool",
                    metadata={"canonical_request_owner": "typescript"},
                )
            )
        provenance = spec.execution_provenance
        namespace = provenance.namespace if provenance is not None else "builtin"
        server_id = provenance.server_id if provenance is not None else ""
        version = provenance.version if provenance is not None else ""
        metadata.update(
            {
                "tool_namespace": namespace,
                "namespace": namespace,
                "server_id": server_id,
                "server_name": server_id,
                "tool_version": version,
                "canonical_request_owner": "typescript",
            }
        )
        call = ToolCall(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            metadata=metadata,
        )
        permission_request = PermissionEvaluationRequest(
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            turn_id=str(payload.get("turn_index") or ""),
            node_id=node_id,
            tool_use_id=tool_call_id,
            tool_identity=ToolIdentity(
                namespace=namespace,
                name=tool_name,
                server_id=server_id,
                version=version,
            ),
            arguments=arguments,
            operation=str(spec.metadata.get("access_mode") or "execute"),
            mode=self._permission_mode(self.config.permission_mode),
            workspace_root=str(self.context.workspace_root),
            interactive=self.config.permission_interactive,
            headless=self.config.permission_headless,
            requires_interaction=tool_name in {"shell", "browser"},
            attributes={
                "tool_source": spec.source,
                "tool_metadata": dict(spec.metadata),
                "canonical_request_owner": "typescript",
            },
            metadata={
                "batch_id": str(payload.get("batch_id") or ""),
                "execution_mode": str(payload.get("execution_mode") or ""),
            },
        )
        guard = permission_runtime.guard(
            permission_request,
            workspace_state=self._workspace_safety_state(tool_name, arguments),
        )
        self._host_events.extend(guard.events)
        if guard.execution_grant is None:
            effect = str(guard.decision.effect)
            result = ToolResult(
                tool_call_id=tool_call_id,
                ok=False,
                summary=str(guard.decision.reason),
                error=(
                    "permission_approval_required"
                    if effect == "ask"
                    else "permission_denied"
                ),
                metadata={
                    "permission_effect": effect,
                    "permission_decision_id": guard.decision.decision_id,
                    "canonical_request_owner": "typescript",
                },
            )
            return to_jsonable(result)
        result = executor.execute(call, permission_grant=guard.execution_grant)
        self._host_events.extend(permission_runtime.drain_execution_events(tool_call_id))
        self._host_artifacts.extend(result.artifacts)
        return to_jsonable(result)

    def _workspace_safety_state(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw_path = arguments.get("path") or arguments.get("file_path")
        if raw_path is None:
            local = tool_name not in {"shell", "browser", "web_search"}
            return {
                "workspace_root": str(self.context.workspace_root),
                "workspace_scoped": local,
                "path_validated": local,
                "read_before_write": True,
                "baseline_current": True,
                "bounded_change": True,
                "external_egress": not local,
            }
        root = Path(self.context.workspace_root).resolve()
        candidate = Path(str(raw_path))
        target = (
            (root / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
        try:
            target.relative_to(root)
            inside = True
        except ValueError:
            inside = False
        try:
            target.stat()
            baseline_current = True
        except FileNotFoundError:
            baseline_current = True
        except OSError:
            baseline_current = False
        return {
            "workspace_root": str(root),
            "workspace_scoped": inside,
            "path_validated": inside,
            "read_before_write": inside,
            "baseline_current": baseline_current,
            "bounded_change": inside,
            "cross_repository": not inside,
            "explicit_path_unsafe": not inside,
        }

    def _write_artifact(
        self,
        *,
        payload: Mapping[str, Any],
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> Any:
        kind_value = str(payload.get("kind") or "structured_data")
        try:
            kind = ArtifactKind(kind_value)
        except ValueError:
            kind = ArtifactKind.STRUCTURED_DATA
        artifact = self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=str(payload.get("content") or ""),
            title=str(payload.get("title") or "TypeScript runtime artifact"),
            kind=kind,
            extension=str(payload.get("extension") or ".txt"),
            producer_node_id=node_id,
        )
        self._host_artifacts.append(artifact)
        return artifact

    def _event_record(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        payload: Mapping[str, Any],
    ) -> EventRecord:
        phase = str(payload.get("phase") or "runtime_event")
        event_payload: dict[str, Any] = {
            "query_session": dict(payload),
            "typescript_runtime": {
                "runtime_id": TYPESCRIPT_RUNTIME_ID,
                "protocol": RUNTIME_PROTOCOL_VERSION,
                "canonical_owner": "typescript",
                "phase": phase,
            },
        }
        tool_result = payload.get("tool_result")
        if isinstance(tool_result, Mapping):
            event_payload["tool_result"] = dict(tool_result)
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=(
                EventType.BUDGET_UPDATED
                if phase in {"tool_result_budget_exceeded", "context_compacted"}
                else EventType.AGENT_MESSAGE
            ),
            payload=event_payload,
        )

    def _project_event(
        self,
        projection: QuerySession,
        payload: Mapping[str, Any],
    ) -> None:
        phase = str(payload.get("phase") or "")
        metadata = {
            **dict(payload),
            "canonical_owner": "typescript",
            "python_projection_canonical": False,
        }
        if phase == "turn_started":
            if projection.active_turn is None:
                projection.start_turn(
                    int(payload.get("turn_index") or 0),
                    user_content=str(payload.get("user_content") or ""),
                    metadata=metadata,
                )
            return
        if phase == "stream_request_start":
            projection.start_stream_request(metadata=metadata)
            return
        if phase in {
            "tool_batch_started",
            "tool_batch_completed",
            "tool_use_summary",
        }:
            projection.record_batch_event(QueryStreamEventType(phase), metadata=metadata)
            return
        if phase == "tool_call_started":
            projection.record_tool_call(
                tool_call_id=str(payload.get("tool_call_id") or ""),
                tool_name=str(payload.get("tool_name") or ""),
                metadata=metadata,
            )
            return
        if phase == "tool_call_completed":
            result = dict(payload.get("tool_result") or {})
            projection.record_tool_result(
                tool_call_id=str(result.get("tool_call_id") or payload.get("tool_call_id") or ""),
                tool_name=str(payload.get("tool_name") or ""),
                summary=str(result.get("summary") or ""),
                ok=result.get("ok") is True,
                error=str(result.get("error") or "") or None,
                artifacts=[
                    str(item.get("artifact_id") or "")
                    for item in list(result.get("artifacts") or [])
                    if isinstance(item, Mapping) and item.get("artifact_id")
                ],
                metadata=metadata,
            )
            return
        if phase == "context_compacted":
            projection.record_context_compaction(
                artifact_id=str(payload.get("artifact_id") or ""),
                metadata=metadata,
            )
            return
        if phase == "turn_completed" and projection.active_turn is not None:
            ok = payload.get("ok") is True
            projection.end_turn(
                ok=ok,
                stop_reason=StopReason.END_TURN if ok else StopReason.TOOL_ERROR,
                error=str(payload.get("error") or "") or None,
                metadata=metadata,
            )

    def _failed_result(
        self,
        *,
        error: TypeScriptRuntimeError,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        projection: QuerySession,
    ) -> ClaudeQueryEngineResult:
        if projection.active_turn is not None:
            projection.end_turn(
                ok=False,
                stop_reason=StopReason.STREAM_ERROR,
                error=error.code,
                metadata={"canonical_owner": "typescript"},
            )
        projection.complete_session(
            ok=False,
            stop_reason=StopReason.STREAM_ERROR,
            metadata={
                "canonical_owner": "typescript",
                "typescript_runtime_error": error.code,
            },
        )
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.NODE_FAILED,
            payload={
                "query_session": {
                    "phase": "session_failed",
                    "session_id": session_id,
                    "canonical_owner": "typescript",
                    "runtime_id": TYPESCRIPT_RUNTIME_ID,
                    "error": error.code,
                    "message": str(error),
                },
                "typescript_runtime": {
                    "runtime_id": TYPESCRIPT_RUNTIME_ID,
                    "protocol": RUNTIME_PROTOCOL_VERSION,
                    "canonical_owner": "typescript",
                    "fallback_used": False,
                },
            },
        )
        return ClaudeQueryEngineResult(
            ok=False,
            event_records=[*self._host_events, event],
            artifacts=self._dedupe_artifacts(self._host_artifacts),
            step_summaries=[],
            turn_count=0,
            tool_call_count=0,
            context_compaction_count=0,
            stopped_reason=error.code,
            session_snapshot=to_jsonable(projection.snapshot()),
            metadata={
                "loop": "zyra_typescript_query_engine_runtime",
                "canonical_runtime_owner": "typescript",
                "typescript_runtime_id": TYPESCRIPT_RUNTIME_ID,
                "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
                "python_query_engine_fallback": "false",
                "typescript_runtime_error": error.code,
                "query_session_id": session_id,
            },
        )

    def _read_frame(
        self,
        reader: _ProcessLineReader,
        process: subprocess.Popen[str],
        *,
        run_id: str,
        expected_sequence: int,
        deadline: float,
    ) -> dict[str, Any]:
        line = reader.get(deadline - time.monotonic())
        if line is None:
            stderr = process.stderr.read().strip() if process.stderr is not None else ""
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                stderr or "TypeScript runtime closed stdout before completing the run.",
            )
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as error:
            self._terminate(process)
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime emitted non-JSON output.",
            ) from error
        if not isinstance(frame, dict):
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime frame must be an object.",
            )
        if frame.get("protocol") != RUNTIME_PROTOCOL_VERSION:
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime protocol version mismatch.",
            )
        if frame.get("run_id") != run_id:
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime frame run_id mismatch.",
            )
        sequence = frame.get("sequence")
        if sequence != expected_sequence:
            reason = (
                "duplicate"
                if isinstance(sequence, int) and sequence < expected_sequence
                else "out_of_order"
            )
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                f"TypeScript runtime {reason} frame: expected {expected_sequence}, got {sequence}.",
            )
        if not isinstance(frame.get("payload"), dict):
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime frame payload must be an object.",
            )
        return frame

    @staticmethod
    def _write_frame(
        process: subprocess.Popen[str],
        *,
        run_id: str,
        sequence: int,
        kind: str,
        payload: Mapping[str, Any],
        correlation_id: str = "",
    ) -> None:
        if process.stdin is None or process.poll() is not None:
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                "TypeScript runtime process is not writable.",
            )
        frame = {
            "protocol": RUNTIME_PROTOCOL_VERSION,
            "run_id": run_id,
            "sequence": sequence,
            "kind": kind,
            "correlation_id": correlation_id,
            "payload": to_jsonable(dict(payload)),
        }
        try:
            process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                "TypeScript runtime process disconnected while receiving a frame.",
            ) from error

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _session_id(
        self,
        run_id: str,
        request_metadata: Mapping[str, Any] | None,
    ) -> str:
        seed = dict(self.config.session_seed or {})
        metadata = dict(request_metadata or {})
        return str(
            seed.get("session_id")
            or metadata.get("query_session_id")
            or f"codesession_{run_id}"
        )

    @staticmethod
    def _permission_mode(value: str) -> PermissionMode:
        aliases = {
            "acceptEdits": PermissionMode.ACCEPT_EDITS,
            "accept_edits": PermissionMode.ACCEPT_EDITS,
            "dontAsk": PermissionMode.DONT_ASK,
            "dont_ask": PermissionMode.DONT_ASK,
            "bypassPermissions": PermissionMode.BYPASS,
            "bypass": PermissionMode.BYPASS,
        }
        if value in aliases:
            return aliases[value]
        try:
            return PermissionMode(value)
        except ValueError:
            return PermissionMode.DEFAULT

    @staticmethod
    def _stop_reason(value: str | None, *, complete: bool = False) -> StopReason:
        if complete:
            return StopReason.SESSION_COMPLETED
        return {
            "max_turns_exceeded": StopReason.MAX_TURNS_EXCEEDED,
            "user_cancelled": StopReason.USER_CANCELLED,
            "model_error": StopReason.STREAM_ERROR,
            "typescript_runtime_timeout": StopReason.STREAM_ERROR,
            "typescript_runtime_process_failed": StopReason.STREAM_ERROR,
        }.get(str(value or ""), StopReason.TOOL_ERROR)

    @staticmethod
    def _artifact_from_mapping(value: Mapping[str, Any]) -> Any:
        from zyra_core import ArtifactRef

        try:
            kind = ArtifactKind(str(value.get("kind") or "file"))
        except ValueError:
            kind = ArtifactKind.FILE
        return ArtifactRef(
            artifact_id=str(value.get("artifact_id") or ""),
            kind=kind,
            uri=str(value.get("uri") or ""),
            title=str(value.get("title") or ""),
            producer_node_id=value.get("producer_node_id"),
            created_at=str(value.get("created_at") or ""),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _dedupe_artifacts(values: Sequence[Any]) -> list[Any]:
        selected: list[Any] = []
        seen: set[str] = set()
        for value in values:
            artifact_id = str(getattr(value, "artifact_id", "") or "")
            if not artifact_id or artifact_id in seen:
                continue
            seen.add(artifact_id)
            selected.append(value)
        return selected
