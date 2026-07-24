from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .drivers import PtyProcess, PtySpawnOptions, spawn_pty
from .models import (
    TerminalBinding,
    TerminalControlRequest,
    TerminalCreateRequest,
    TerminalError,
    TerminalPermission,
    TerminalPhase,
    TerminalProjection,
    TerminalSpill,
    TerminalStatus,
    now_iso,
)
from .output import OutputChunk, SecretRedactor, TerminalOutputJournal
from .tickets import TerminalTicket, TerminalTicketAuthority


PermissionPort = Callable[
    [str, TerminalCreateRequest | TerminalControlRequest],
    TerminalPermission,
]
WorkspaceResolver = Callable[[TerminalCreateRequest], tuple[str, int, Path]]
EventSink = Callable[[Mapping[str, Any]], str]
SpillSinkFactory = Callable[
    [TerminalBinding],
    Callable[[bytes, int, int, bool, bool], TerminalSpill],
]
PtySpawner = Callable[[PtySpawnOptions], PtyProcess]


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


class TerminalStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            if not isinstance(value, dict) or value.get("schema") != "zyra.terminal-state.v1":
                return {}
            sessions = value.get("sessions")
            if not isinstance(sessions, dict):
                return {}
            return {
                str(key): dict(item)
                for key, item in sessions.items()
                if isinstance(item, dict)
            }

    def save(self, sessions: Mapping[str, Mapping[str, Any]]) -> None:
        payload = {
            "schema": "zyra.terminal-state.v1",
            "updated_at": now_iso(),
            "sessions": {
                key: dict(value)
                for key, value in sorted(sessions.items())
            },
        }
        material = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(
                f"{self.path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(material, encoding="utf-8")
            os.replace(temporary, self.path)


class TerminalSession:
    def __init__(
        self,
        *,
        binding: TerminalBinding,
        status: TerminalStatus,
        permission: TerminalPermission,
        correlation_id: str,
        causation_id: str,
        process: PtyProcess,
        journal: TerminalOutputJournal,
        event_sink: EventSink,
        mutation: Callable[[TerminalSession], None],
    ) -> None:
        self.binding = binding
        self.permission = permission
        self.correlation_id = correlation_id
        self.causation_id = causation_id
        self.process = process
        self.journal = journal
        self._event_sink = event_sink
        self._mutation = mutation
        self._status = status
        self._lock = threading.RLock()
        self._connections: set[str] = set()
        self._closed = False
        self._kill_requested = False
        self._input_rate_bytes_per_second = 128 * 1_024
        self._input_burst_bytes = 256 * 1_024
        self._input_tokens = float(self._input_burst_bytes)
        self._input_token_updated_at = time.monotonic()
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"zyra-terminal-reader-{binding.terminal_id}",
            daemon=True,
        )
        self._reader.start()

    @property
    def status(self) -> TerminalStatus:
        with self._lock:
            return self._synchronized_status()

    def projection(
        self,
        *,
        ticket: TerminalTicket | None = None,
        event_id: str = "",
    ) -> TerminalProjection:
        status = self.status
        return TerminalProjection(
            binding=self.binding,
            status=status,
            permission=self.permission,
            socket_path=(
                f"/tasks/{self.binding.task_id}/terminals/"
                f"{self.binding.terminal_id}/connect"
            ),
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            event_id=event_id,
            ticket=ticket.token if ticket else "",
            ticket_expires_at=ticket.expires_at_iso if ticket else "",
        )

    def attach(self) -> str:
        with self._lock:
            self._require_running_or_terminal()
            connection_id = _id("terminal-connection")
            self._connections.add(connection_id)
            self._status = replace(
                self._synchronized_status(),
                viewers=len(self._connections),
                updated_at=now_iso(),
                state_mutation_id=_id("terminal-viewer-attached"),
            )
            self._mutation(self)
            return connection_id

    def detach(self, connection_id: str) -> None:
        with self._lock:
            if connection_id not in self._connections:
                return
            self._connections.remove(connection_id)
            self._status = replace(
                self._synchronized_status(),
                viewers=len(self._connections),
                updated_at=now_iso(),
                state_mutation_id=_id("terminal-viewer-detached"),
            )
            self._mutation(self)

    def input(
        self,
        request: TerminalControlRequest,
        permission: TerminalPermission,
    ) -> int:
        data = request.data.encode("utf-8")
        if not data or len(data) > 256 * 1_024:
            raise TerminalError(
                "terminal_input_size_invalid",
                "Terminal input must contain 1..262144 UTF-8 bytes.",
                status=400,
            )
        with self._lock:
            status = self._synchronized_status()
            if status.phase != TerminalPhase.RUNNING:
                raise TerminalError(
                    "terminal_not_running",
                    "Terminal input requires a running PTY.",
                    status=410,
                )
            if request.sequence <= status.input_sequence:
                raise TerminalError(
                    "terminal_input_sequence_stale",
                    "Terminal input sequence is stale or duplicated.",
                    status=409,
                )
            if permission.effect != "allow":
                raise TerminalError(
                    "terminal_input_permission_denied",
                    (
                        "Terminal input requires approval"
                        + (
                            f" request {permission.request_id}."
                            if permission.request_id
                            else "."
                        )
                        if permission.effect == "ask"
                        else "Terminal input was denied."
                    ),
                    status=202 if permission.effect == "ask" else 403,
                    retryable=permission.effect == "ask",
                    details={"permission": permission.to_json()},
                )
            now = time.monotonic()
            elapsed = max(0.0, now - self._input_token_updated_at)
            self._input_tokens = min(
                float(self._input_burst_bytes),
                self._input_tokens
                + elapsed * self._input_rate_bytes_per_second,
            )
            self._input_token_updated_at = now
            if len(data) > self._input_tokens:
                missing = len(data) - self._input_tokens
                retry_after_ms = max(
                    1,
                    int(
                        missing
                        / self._input_rate_bytes_per_second
                        * 1_000
                    ),
                )
                raise TerminalError(
                    "terminal_input_rate_limited",
                    "Terminal input exceeded the session byte-rate budget.",
                    status=429,
                    retryable=True,
                    details={
                        "retry_after_ms": retry_after_ms,
                        "burst_bytes": self._input_burst_bytes,
                        "bytes_per_second": (
                            self._input_rate_bytes_per_second
                        ),
                    },
                )
            self._input_tokens -= len(data)
            written = self.process.write(data)
            self.permission = permission
            self._status = replace(
                status,
                input_sequence=request.sequence,
                updated_at=now_iso(),
                state_mutation_id=_id("terminal-input"),
            )
            self._mutation(self)
        self._emit(
            "terminal.input",
            {
                "sequence": request.sequence,
                "byte_length": len(data),
                "written": written,
                "permission": permission.to_json(),
            },
        )
        return written

    def resize(self, request: TerminalControlRequest) -> None:
        if not 2 <= request.rows <= 500 or not 2 <= request.cols <= 1_000:
            raise TerminalError(
                "terminal_resize_invalid",
                "Terminal resize is outside the supported range.",
                status=400,
            )
        with self._lock:
            status = self._synchronized_status()
            if status.phase != TerminalPhase.RUNNING:
                raise TerminalError(
                    "terminal_not_running",
                    "Terminal resize requires a running PTY.",
                    status=410,
                )
            if request.sequence <= status.resize_sequence:
                raise TerminalError(
                    "terminal_resize_sequence_stale",
                    "Terminal resize sequence is stale or duplicated.",
                    status=409,
                )
            self.process.resize(request.rows, request.cols)
            self._status = replace(
                status,
                rows=request.rows,
                cols=request.cols,
                resize_sequence=request.sequence,
                updated_at=now_iso(),
                state_mutation_id=_id("terminal-resize"),
            )
            self._mutation(self)
        self._emit(
            "terminal.resize",
            {
                "sequence": request.sequence,
                "rows": request.rows,
                "cols": request.cols,
            },
        )

    def kill(
        self,
        request: TerminalControlRequest,
        permission: TerminalPermission,
    ) -> TerminalStatus:
        with self._lock:
            status = self._synchronized_status()
            if status.phase != TerminalPhase.RUNNING:
                return status
            if permission.effect != "allow":
                raise TerminalError(
                    "terminal_kill_permission_denied",
                    (
                        "Terminal kill requires approval"
                        + (
                            f" request {permission.request_id}."
                            if permission.request_id
                            else "."
                        )
                        if permission.effect == "ask"
                        else "Terminal kill was denied."
                    ),
                    status=202 if permission.effect == "ask" else 403,
                    retryable=permission.effect == "ask",
                    details={"permission": permission.to_json()},
                )
            self._kill_requested = True
            self.permission = permission
        self.process.terminate_tree()
        with self._lock:
            exit_code = self.process.poll()
            status = self._synchronized_status()
            if status.phase == TerminalPhase.RUNNING:
                self._status = status.transition(
                    TerminalPhase.KILLED,
                    state_mutation_id=_id("terminal-killed"),
                    exit_code=exit_code,
                    signal="kill",
                )
                self._mutation(self)
            selected = self._status
        self._emit(
            "terminal.killed",
            {
                "reason": request.reason,
                "exit_code": selected.exit_code,
                "permission": permission.to_json(),
            },
        )
        return selected

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            running = self._synchronized_status().phase == TerminalPhase.RUNNING
            if running:
                self._kill_requested = True
        if running:
            self.process.terminate_tree(grace_seconds=0.5)
        self.process.close()
        if self._reader.is_alive() and self._reader is not threading.current_thread():
            self._reader.join(timeout=2.0)
        self.journal.close()

    def persisted(self) -> dict[str, Any]:
        status = self.status
        return {
            "binding": self.binding.to_json(),
            "status": status.to_json(),
            "permission": self.permission.to_json(),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "output": self.journal.snapshot(),
            "canonical_owner": "zyra_workers.terminal.TerminalSessionRegistry",
        }

    def _read_loop(self) -> None:
        error: BaseException | None = None
        try:
            while True:
                data = self.process.read()
                if not data:
                    break
                chunks = self.journal.append(data)
                self._record_output(chunks)
        except BaseException as caught:  # noqa: BLE001 - converted into terminal state.
            error = caught
        finally:
            try:
                self._record_output(self.journal.close())
            except BaseException as caught:  # noqa: BLE001
                if error is None:
                    error = caught
            if error is not None and self.process.poll() is None:
                try:
                    self.process.terminate_tree(grace_seconds=0.2)
                except BaseException:  # noqa: BLE001 - retain original failure.
                    pass
            try:
                exit_code = self.process.wait(timeout=2.0)
            except (TimeoutError, OSError):
                exit_code = self.process.poll()
            with self._lock:
                status = self._synchronized_status()
                if status.phase == TerminalPhase.RUNNING:
                    phase = (
                        TerminalPhase.KILLED
                        if self._kill_requested
                        else TerminalPhase.CRASHED
                        if error is not None
                        else TerminalPhase.EXITED
                    )
                    self._status = status.transition(
                        phase,
                        state_mutation_id=_id(f"terminal-{phase}"),
                        exit_code=exit_code,
                        signal=type(error).__name__ if error else "",
                    )
                    self._mutation(self)
                selected = self._status
            self._emit(
                "terminal.exited",
                {
                    "phase": str(selected.phase),
                    "exit_code": selected.exit_code,
                    "error_class": type(error).__name__ if error else "",
                },
            )

    def _record_output(self, chunks: tuple[OutputChunk, ...]) -> None:
        if not chunks:
            return
        with self._lock:
            status = self._synchronized_status()
            self._status = replace(
                status,
                cursor=self.journal.cursor,
                earliest_cursor=self.journal.earliest_cursor,
                spilled_bytes=self.journal.spilled_bytes,
                binary_bytes=self.journal.binary_bytes,
                updated_at=now_iso(),
                state_mutation_id=_id("terminal-output"),
            )
            self._mutation(self)
        self._emit(
            "terminal.output",
            {
                "first_cursor": chunks[0].first_cursor,
                "next_cursor": chunks[-1].next_cursor,
                "byte_length": sum(item.byte_length for item in chunks),
                "chunks": len(chunks),
                "redacted": any(item.redacted for item in chunks),
            },
        )

    def _synchronized_status(self) -> TerminalStatus:
        return replace(
            self._status,
            cursor=self.journal.cursor,
            earliest_cursor=self.journal.earliest_cursor,
            spilled_bytes=self.journal.spilled_bytes,
            binary_bytes=self.journal.binary_bytes,
            viewers=len(self._connections),
        )

    def _require_running_or_terminal(self) -> None:
        if self._closed:
            raise TerminalError(
                "terminal_session_closed",
                "Terminal session is closed.",
                status=410,
            )

    def _emit(self, event_type: str, detail: Mapping[str, Any]) -> str:
        return self._event_sink(
            {
                "schema": "zyra.terminal-event.v1",
                "event_type": event_type,
                "binding": self.binding.to_json(),
                "correlation_id": self.correlation_id,
                "causation_id": self.causation_id,
                "state_mutation_id": self.status.state_mutation_id,
                "terminal": dict(detail),
            }
        )


class TerminalSessionRegistry:
    def __init__(
        self,
        *,
        state_store: TerminalStateStore,
        workspace_resolver: WorkspaceResolver,
        permission_port: PermissionPort,
        event_sink: EventSink,
        spill_sink_factory: SpillSinkFactory,
        ticket_authority: TerminalTicketAuthority,
        pty_spawner: PtySpawner = spawn_pty,
        maximum_sessions: int = 128,
        enabled: bool = True,
    ) -> None:
        self.state_store = state_store
        self.workspace_resolver = workspace_resolver
        self.permission_port = permission_port
        self.event_sink = event_sink
        self.spill_sink_factory = spill_sink_factory
        self.ticket_authority = ticket_authority
        self.pty_spawner = pty_spawner
        self.maximum_sessions = maximum_sessions
        self.enabled = enabled
        self._lock = threading.RLock()
        self._sessions: dict[str, TerminalSession] = {}
        self._persisted_records: dict[str, dict[str, Any]] = {}
        self._recovered = self._recover_orphans()

    def create(self, request: TerminalCreateRequest) -> TerminalProjection:
        self._require_enabled()
        if request.sealed:
            permission = TerminalPermission(
                effect="deny",
                decision_id=_id("terminal-sealed-denial"),
                reason_code="terminal.sealed_human_input_denied",
                reason="Sealed autonomous runs reject human terminal creation.",
            )
        else:
            permission = self._permission("create", request)
        if permission.effect != "allow":
            phase = (
                TerminalPhase.PERMISSION_PENDING
                if permission.effect == "ask" and not request.sealed
                else TerminalPhase.REJECTED
            )
            return self._rejected_projection(request, permission, phase)
        workspace_id, workspace_revision, workspace_root = self.workspace_resolver(
            request
        )
        terminal_id = _id("terminal")
        binding = TerminalBinding(
            task_id=request.task_id,
            run_id=request.run_id,
            terminal_id=terminal_id,
            session_id=request.session_id,
            workspace_id=workspace_id,
            workspace_revision=workspace_revision,
            worker_id=request.worker_id,
            command_id=request.command_id,
            tool_call_id=request.tool_call_id,
            span_id=request.span_id,
        )
        environment = self._environment(request.environment)
        process = self.pty_spawner(
            PtySpawnOptions(
                command=request.command,
                cwd=workspace_root,
                shell=request.shell,
                rows=request.rows,
                cols=request.cols,
                environment=environment,
            )
        )
        timestamp = now_iso()
        status = TerminalStatus(
            phase=TerminalPhase.RUNNING,
            pid=process.pid,
            cwd=request.cwd or ".",
            title=request.title,
            rows=request.rows,
            cols=request.cols,
            cursor=0,
            earliest_cursor=0,
            started_at=timestamp,
            updated_at=timestamp,
            state_mutation_id=_id("terminal-created"),
        )
        journal = TerminalOutputJournal(
            spill_sink=self.spill_sink_factory(binding),
            redactor=SecretRedactor(request.environment.values()),
        )
        session = TerminalSession(
            binding=binding,
            status=status,
            permission=permission,
            correlation_id=request.correlation_id or _id("terminal-correlation"),
            causation_id=request.causation_id or request.tool_call_id,
            process=process,
            journal=journal,
            event_sink=self.event_sink,
            mutation=self._persist_session,
        )
        initial_record = session.persisted()
        self._prune()
        with self._lock:
            capacity_exhausted = len(self._sessions) >= self.maximum_sessions
            if not capacity_exhausted:
                self._sessions[terminal_id] = session
                self._persisted_records.setdefault(terminal_id, initial_record)
                self._persist()
        if capacity_exhausted:
            session.shutdown()
            with self._lock:
                self._persisted_records.pop(terminal_id, None)
                self._persist()
            raise TerminalError(
                "terminal_capacity_exhausted",
                "Terminal session capacity is exhausted.",
                status=503,
                retryable=True,
            )
        try:
            event_id = self.event_sink(
                {
                    "schema": "zyra.terminal-event.v1",
                    "event_type": "terminal.created",
                    "binding": binding.to_json(),
                    "correlation_id": session.correlation_id,
                    "causation_id": session.causation_id,
                    "state_mutation_id": status.state_mutation_id,
                    "terminal": {
                        "phase": "running",
                        "driver": "zyra-platform-pty",
                        "permission": permission.to_json(),
                    },
                }
            )
        except BaseException:
            session.shutdown()
            with self._lock:
                self._sessions.pop(terminal_id, None)
                self._persisted_records.pop(terminal_id, None)
                self._persist()
            raise
        return session.projection(event_id=event_id)

    def get(self, task_id: str, terminal_id: str) -> TerminalSession:
        with self._lock:
            session = self._sessions.get(terminal_id)
        if session is None or session.binding.task_id != task_id:
            raise TerminalError(
                "terminal_not_found",
                "Terminal session was not found.",
                status=404,
            )
        return session

    def list(
        self,
        task_id: str,
        *,
        include_closed: bool = False,
    ) -> tuple[TerminalProjection, ...]:
        with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if session.binding.task_id == task_id
            ]
        projections = []
        for session in sessions:
            projection = session.projection()
            if (
                not include_closed
                and projection.status.phase
                not in {TerminalPhase.OPENING, TerminalPhase.RUNNING}
            ):
                continue
            projections.append(projection)
        return tuple(
            sorted(
                projections,
                key=lambda item: (
                    item.status.started_at,
                    item.binding.terminal_id,
                ),
            )
        )

    def ticket(
        self,
        *,
        task_id: str,
        terminal_id: str,
        run_id: str,
        session_id: str,
        origin: str,
        cursor: int,
        protocol: str,
    ) -> TerminalProjection:
        session = self.get(task_id, terminal_id)
        binding = session.binding
        if binding.run_id != run_id or binding.session_id != session_id:
            raise TerminalError(
                "terminal_ticket_binding_mismatch",
                "Terminal ticket request belongs to another run or session.",
                status=403,
            )
        status = session.status
        if status.phase not in {
            TerminalPhase.RUNNING,
            TerminalPhase.EXITED,
            TerminalPhase.KILLED,
            TerminalPhase.CRASHED,
        }:
            raise TerminalError(
                "terminal_ticket_unavailable",
                "Terminal is not connectable in its current phase.",
                status=409,
            )
        ticket = self.ticket_authority.issue(
            binding=binding,
            origin=origin,
            cursor=cursor,
            protocol=protocol,
        )
        return session.projection(ticket=ticket)

    def consume_ticket(
        self,
        token: str,
        *,
        origin: str,
        task_id: str,
        terminal_id: str,
        protocol: str,
    ) -> tuple[TerminalSession, TerminalTicket, str]:
        ticket = self.ticket_authority.consume(
            token,
            expected_origin=origin,
            expected_task_id=task_id,
            expected_terminal_id=terminal_id,
            expected_protocol=protocol,
        )
        session = self.get(task_id, terminal_id)
        if session.binding != ticket.binding:
            raise TerminalError(
                "terminal_ticket_owner_changed",
                "Terminal binding changed after ticket issue.",
                status=403,
            )
        connection_id = session.attach()
        return session, ticket, connection_id

    def control(self, request: TerminalControlRequest) -> dict[str, Any]:
        session = self.get(request.task_id, request.terminal_id)
        self._validate_control_binding(session, request)
        if request.action == "resize":
            session.resize(request)
            return {"accepted": True, "status": session.status.to_json()}
        if request.sealed:
            permission = TerminalPermission(
                effect="deny",
                decision_id=_id("terminal-sealed-denial"),
                reason_code="terminal.sealed_human_input_denied",
                reason=f"Sealed autonomous runs reject human terminal {request.action}.",
            )
        else:
            permission = self._permission(request.action, request)
        if request.action == "input":
            written = session.input(request, permission)
            return {
                "accepted": True,
                "written": written,
                "permission": permission.to_json(),
                "status": session.status.to_json(),
            }
        if request.action == "kill":
            status = session.kill(request, permission)
            return {
                "accepted": permission.effect == "allow",
                "permission": permission.to_json(),
                "status": status.to_json(),
            }
        raise TerminalError(
            "terminal_control_action_invalid",
            "Terminal control action is invalid.",
            status=400,
        )

    def shutdown(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
        for session in sessions:
            session.shutdown()
        with self._lock:
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sessions = dict(self._sessions)
            recovered = list(self._recovered)
            enabled = self.enabled
        return {
            "enabled": enabled,
            "sessions": {
                terminal_id: session.persisted()
                for terminal_id, session in sessions.items()
            },
            "recovered_orphans": recovered,
            "tickets": self.ticket_authority.snapshot(),
            "canonical_owner": (
                "zyra_workers.terminal.TerminalSessionRegistry"
            ),
        }

    def _permission(
        self,
        action: str,
        request: TerminalCreateRequest | TerminalControlRequest,
    ) -> TerminalPermission:
        permission = self.permission_port(action, request)
        if permission.canonical_owner != "typescript.PermissionCoordinator":
            raise TerminalError(
                "terminal_permission_owner_invalid",
                "Terminal permission response is not TypeScript-owned.",
                status=503,
            )
        if permission.effect not in {"allow", "ask", "deny"}:
            raise TerminalError(
                "terminal_permission_effect_invalid",
                "Terminal permission effect is invalid.",
                status=503,
            )
        return permission

    def _persist_session(self, session: TerminalSession) -> None:
        record = session.persisted()
        with self._lock:
            self._persisted_records[session.binding.terminal_id] = record
            self._persist()

    def _persist(self) -> None:
        self.state_store.save(self._persisted_records)

    def _recover_orphans(self) -> tuple[dict[str, Any], ...]:
        recovered = []
        for terminal_id, record in self.state_store.load().items():
            status = record.get("status")
            if not isinstance(status, dict):
                continue
            if status.get("phase") not in {
                "opening",
                "running",
                "permission_pending",
            }:
                continue
            recovered.append(
                {
                    "terminal_id": terminal_id,
                    "previous_phase": status.get("phase"),
                    "recovered_phase": "crashed",
                    "reason": "api_process_restarted_without_live_pty_owner",
                }
            )
        if recovered:
            self.state_store.save({})
        return tuple(recovered)

    def _rejected_projection(
        self,
        request: TerminalCreateRequest,
        permission: TerminalPermission,
        phase: TerminalPhase,
    ) -> TerminalProjection:
        terminal_id = _id("terminal-rejected")
        binding = TerminalBinding(
            task_id=request.task_id,
            run_id=request.run_id,
            terminal_id=terminal_id,
            session_id=request.session_id,
            workspace_id="workspace-unallocated",
            workspace_revision=0,
            worker_id=request.worker_id,
            command_id=request.command_id,
            tool_call_id=request.tool_call_id,
            span_id=request.span_id,
        )
        timestamp = now_iso()
        status = TerminalStatus(
            phase=phase,
            cwd=".",
            title=request.title,
            rows=request.rows,
            cols=request.cols,
            cursor=0,
            earliest_cursor=0,
            started_at=timestamp,
            updated_at=timestamp,
            exited_at=timestamp if phase == TerminalPhase.REJECTED else "",
            state_mutation_id=_id(f"terminal-{phase}"),
        )
        event_id = self.event_sink(
            {
                "schema": "zyra.terminal-event.v1",
                "event_type": f"terminal.{phase}",
                "binding": binding.to_json(),
                "correlation_id": request.correlation_id or _id("terminal-correlation"),
                "causation_id": request.causation_id or request.tool_call_id,
                "state_mutation_id": status.state_mutation_id,
                "terminal": {
                    "phase": str(phase),
                    "permission": permission.to_json(),
                    "human_intervention_count": 0,
                },
            }
        )
        return TerminalProjection(
            binding=binding,
            status=status,
            permission=permission,
            socket_path=(
                f"/tasks/{request.task_id}/terminals/{terminal_id}/connect"
            ),
            correlation_id=request.correlation_id or _id("terminal-correlation"),
            causation_id=request.causation_id or request.tool_call_id,
            event_id=event_id,
            human_intervention_count=0,
        )

    def _validate_control_binding(
        self,
        session: TerminalSession,
        request: TerminalControlRequest,
    ) -> None:
        binding = session.binding
        if (
            request.run_id != binding.run_id
            or request.session_id != binding.session_id
            or request.worker_id != binding.worker_id
            or request.tool_call_id != binding.tool_call_id
            or request.span_id != binding.span_id
        ):
            raise TerminalError(
                "terminal_control_binding_mismatch",
                "Terminal control request belongs to another custody binding.",
                status=403,
            )

    def _prune(self) -> None:
        with self._lock:
            sessions = dict(self._sessions)
        terminal = [
            (terminal_id, session)
            for terminal_id, session in sessions.items()
            if session.status.phase
            not in {TerminalPhase.OPENING, TerminalPhase.RUNNING}
        ]
        terminal.sort(key=lambda item: item[1].status.updated_at)
        remove_count = max(
            0,
            len(sessions) - self.maximum_sessions + 1,
        )
        removed: list[TerminalSession] = []
        while remove_count and terminal:
            terminal_id, session = terminal.pop(0)
            with self._lock:
                if self._sessions.get(terminal_id) is not session:
                    continue
                self._sessions.pop(terminal_id, None)
                self._persisted_records.pop(terminal_id, None)
                self._persist()
            removed.append(session)
            remove_count -= 1
        for session in removed:
            session.shutdown()

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise TerminalError(
                "terminal_runtime_disabled",
                "Terminal runtime is disabled.",
                status=503,
            )

    @staticmethod
    def _environment(values: Mapping[str, str]) -> dict[str, str]:
        allowed = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "HOME",
            "USERPROFILE",
            "SHELL",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in allowed
        }
        forbidden = re.compile(
            r"(?:secret|token|password|passwd|credential|api[_-]?key)",
            re.IGNORECASE,
        )
        for key, value in values.items():
            if (
                not key
                or len(key) > 128
                or not key.replace("_", "A").isalnum()
                or forbidden.search(key)
                or "\x00" in value
                or len(value.encode("utf-8")) > 32 * 1_024
            ):
                raise TerminalError(
                    "terminal_environment_rejected",
                    f"Terminal environment key {key!r} is not allowed.",
                    status=400,
                )
            environment[key] = value
        return environment
