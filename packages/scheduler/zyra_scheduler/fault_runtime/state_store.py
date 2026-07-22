from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import (
    FaultInjectionRequest,
    FaultSignal,
    InjectionPhase,
    InjectionTransition,
    ObserverDescriptor,
    ObserverLifecycle,
    ObserverState,
    ProjectionReceipt,
    RecoveryHandoff,
    StructuredObservation,
    canonical_json,
    runtime_id,
    stable_digest,
    utc_now,
)
from .errors import (
    FaultRuntimeError,
    FaultRuntimeErrorCode,
    InjectionStateConflict,
    ObserverStateConflict,
)
from .serialization import (
    handoff_from_dict,
    injection_request_from_dict,
    observation_from_dict,
    observer_state_from_dict,
    signal_from_dict,
    transition_from_dict,
)


class FaultStateStore:
    """Durable 07B state owner for observer and injection journals.

    This database does not mirror task, memory, backend-health, browser or
    recovery-planner state.  It stores only observation custody, classified
    signals, injection transitions and delivery receipts.  Domain projections
    keep their existing owners and are referenced by immutable ids.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        with self._guard:
            self._connection.close()

    def _initialize(self) -> None:
        with self._guard:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                PRAGMA foreign_keys=ON;
                PRAGMA busy_timeout=30000;

                CREATE TABLE IF NOT EXISTS observer_state (
                    observer_id TEXT PRIMARY KEY,
                    lifecycle TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    descriptor_revision INTEGER NOT NULL,
                    maturity TEXT NOT NULL,
                    observation_point TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observer_transitions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    transition_id TEXT NOT NULL UNIQUE,
                    observer_id TEXT NOT NULL,
                    from_lifecycle TEXT NOT NULL,
                    to_lifecycle TEXT NOT NULL,
                    from_revision INTEGER NOT NULL,
                    to_revision INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(observer_id) REFERENCES observer_state(observer_id)
                );

                CREATE TABLE IF NOT EXISTS observations (
                    observation_id TEXT PRIMARY KEY,
                    observer_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    code TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source_state_revision INTEGER NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(observer_id) REFERENCES observer_state(observer_id)
                );

                CREATE INDEX IF NOT EXISTS idx_fault_observations_task
                    ON observations(task_id, created_at, observation_id);
                CREATE INDEX IF NOT EXISTS idx_fault_observations_observer
                    ON observations(observer_id, source_state_revision, created_at);

                CREATE TABLE IF NOT EXISTS observation_source_cursors (
                    scope_key TEXT PRIMARY KEY,
                    observer_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    source_state_revision INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(observer_id) REFERENCES observer_state(observer_id)
                );

                CREATE INDEX IF NOT EXISTS idx_observation_source_cursors_observer
                    ON observation_source_cursors(observer_id, run_id, task_id);

                CREATE TABLE IF NOT EXISTS fault_signals (
                    signal_id TEXT PRIMARY KEY,
                    observation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    injection_id TEXT NOT NULL,
                    terminal INTEGER NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(observation_id) REFERENCES observations(observation_id)
                );

                CREATE INDEX IF NOT EXISTS idx_fault_signals_task
                    ON fault_signals(task_id, kind, created_at, signal_id);
                CREATE INDEX IF NOT EXISTS idx_fault_signals_injection
                    ON fault_signals(injection_id, created_at, signal_id);

                CREATE TABLE IF NOT EXISTS injections (
                    injection_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    terminal INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    latest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, task_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_fault_injections_task
                    ON injections(task_id, updated_at, injection_id);

                CREATE TABLE IF NOT EXISTS injection_transitions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    transition_id TEXT NOT NULL UNIQUE,
                    injection_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    signal_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    handoff_id TEXT NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(injection_id, revision),
                    FOREIGN KEY(injection_id) REFERENCES injections(injection_id)
                );

                CREATE TABLE IF NOT EXISTS recovery_handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE,
                    injection_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    fault_kind TEXT NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recovery_handoff_task
                    ON recovery_handoffs(task_id, created_at, handoff_id);

                CREATE TABLE IF NOT EXISTS recovery_handoff_delivery (
                    handoff_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    lease_token TEXT NOT NULL,
                    lease_expires_ms INTEGER,
                    attempts INTEGER NOT NULL,
                    available_at_ms INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    last_error TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(handoff_id) REFERENCES recovery_handoffs(handoff_id)
                );

                CREATE INDEX IF NOT EXISTS idx_recovery_handoff_delivery_due
                    ON recovery_handoff_delivery(status, available_at_ms, lease_expires_ms);

                CREATE TABLE IF NOT EXISTS projection_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL UNIQUE,
                    canonical_event_written INTEGER NOT NULL,
                    scheduler_changed INTEGER NOT NULL,
                    task_projection_changed INTEGER NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(signal_id) REFERENCES fault_signals(signal_id)
                );

                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    error TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(signal_id, destination, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS runtime_metadata (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._guard:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def register_observer(self, descriptor: ObserverDescriptor) -> ObserverState:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT json FROM observer_state WHERE observer_id = ?",
                (descriptor.observer_id,),
            ).fetchone()
            if row is not None:
                current = observer_state_from_dict(json.loads(str(row["json"])))
                if current.descriptor != descriptor:
                    raise FaultRuntimeError(
                        FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                        f"observer descriptor changed without an explicit revision: {descriptor.observer_id}",
                        details={
                            "current": current.descriptor.to_dict(),
                            "requested": descriptor.to_dict(),
                        },
                    )
                return current
            state = ObserverState(
                descriptor=descriptor,
                lifecycle=ObserverLifecycle.REGISTERED,
                revision=1,
            )
            self._write_observer(connection, state)
            self._insert_observer_transition(
                connection,
                state,
                from_lifecycle="",
                from_revision=0,
                reason="observer descriptor registered",
            )
            return state

    def observer(self, observer_id: str) -> ObserverState | None:
        with self._guard:
            row = self._connection.execute(
                "SELECT json FROM observer_state WHERE observer_id = ?",
                (observer_id,),
            ).fetchone()
        return None if row is None else observer_state_from_dict(json.loads(str(row["json"])))

    def require_observer(self, observer_id: str) -> ObserverState:
        state = self.observer(observer_id)
        if state is None:
            raise FaultRuntimeError(
                FaultRuntimeErrorCode.OBSERVER_NOT_REGISTERED,
                f"observer is not registered: {observer_id}",
                details={"observer_id": observer_id},
            )
        return state

    def observers(self) -> tuple[ObserverState, ...]:
        with self._guard:
            rows = self._connection.execute(
                "SELECT json FROM observer_state ORDER BY observer_id"
            ).fetchall()
        return tuple(observer_state_from_dict(json.loads(str(row["json"]))) for row in rows)

    def transition_observer(
        self,
        observer_id: str,
        lifecycle: ObserverLifecycle,
        *,
        expected_revision: int,
        reason: str,
        error: str = "",
    ) -> ObserverState:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT json FROM observer_state WHERE observer_id = ?",
                (observer_id,),
            ).fetchone()
            if row is None:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.OBSERVER_NOT_REGISTERED,
                    f"observer is not registered: {observer_id}",
                )
            current = observer_state_from_dict(json.loads(str(row["json"])))
            if current.revision != expected_revision:
                raise ObserverStateConflict(observer_id, expected_revision, current.revision)
            self._validate_observer_transition(current.lifecycle, lifecycle)
            state = ObserverState(
                descriptor=current.descriptor,
                lifecycle=lifecycle,
                revision=current.revision + 1,
                attach_count=current.attach_count + int(lifecycle is ObserverLifecycle.ATTACHED),
                start_count=current.start_count + int(lifecycle is ObserverLifecycle.RUNNING),
                stop_count=current.stop_count + int(lifecycle in {ObserverLifecycle.STOPPED, ObserverLifecycle.DISABLED}),
                observation_count=current.observation_count,
                emitted_count=current.emitted_count,
                last_observation_id=current.last_observation_id,
                last_error=error,
            )
            self._write_observer(connection, state)
            self._insert_observer_transition(
                connection,
                state,
                from_lifecycle=current.lifecycle.value,
                from_revision=current.revision,
                reason=reason,
            )
            return state

    def record_observation(
        self,
        observation: StructuredObservation,
        *,
        emitted: int = 0,
    ) -> tuple[StructuredObservation, bool, ObserverState]:
        observer_id = observation.provenance.observer_id
        with self.transaction() as connection:
            state_row = connection.execute(
                "SELECT json FROM observer_state WHERE observer_id = ?",
                (observer_id,),
            ).fetchone()
            if state_row is None:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.OBSERVER_NOT_REGISTERED,
                    f"observer is not registered: {observer_id}",
                )
            current = observer_state_from_dict(json.loads(str(state_row["json"])))
            if current.lifecycle is ObserverLifecycle.DISABLED:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.OBSERVER_DISABLED,
                    f"observer is disabled: {observer_id}",
                )
            if not current.lifecycle.accepts_observations:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.OBSERVER_NOT_RUNNING,
                    f"observer is not running: {observer_id}",
                    details={"lifecycle": current.lifecycle.value},
                )
            duplicate_row = connection.execute(
                "SELECT json FROM observations WHERE fingerprint = ?",
                (observation.fingerprint,),
            ).fetchone()
            if duplicate_row is not None:
                existing = observation_from_dict(json.loads(str(duplicate_row["json"])))
                return existing, True, current
            self._fence_observation_cursor(connection, observation)
            connection.execute(
                """
                INSERT INTO observations(
                    observation_id, observer_id, run_id, task_id, category, code,
                    fingerprint, source_state_revision, json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observer_id,
                    observation.refs.run_id,
                    observation.refs.task_id,
                    observation.category.value,
                    observation.code,
                    observation.fingerprint,
                    observation.refs.source_state_revision,
                    canonical_json(observation.to_dict()),
                    observation.observed_at,
                ),
            )
            updated = ObserverState(
                descriptor=current.descriptor,
                lifecycle=current.lifecycle,
                revision=current.revision + 1,
                attach_count=current.attach_count,
                start_count=current.start_count,
                stop_count=current.stop_count,
                observation_count=current.observation_count + 1,
                emitted_count=current.emitted_count + emitted,
                last_observation_id=observation.observation_id,
                last_error="",
            )
            self._write_observer(connection, updated)
            return observation, False, updated

    def observation(self, observation_id: str) -> StructuredObservation | None:
        with self._guard:
            row = self._connection.execute(
                "SELECT json FROM observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
        return None if row is None else observation_from_dict(json.loads(str(row["json"])))

    def observations(
        self,
        *,
        task_id: str = "",
        observer_id: str = "",
        limit: int = 200,
    ) -> tuple[StructuredObservation, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if observer_id:
            clauses.append("observer_id = ?")
            parameters.append(observer_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(max(1, min(int(limit), 5_000)))
        with self._guard:
            rows = self._connection.execute(
                f"SELECT json FROM observations{where} ORDER BY created_at DESC, observation_id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple(observation_from_dict(json.loads(str(row["json"]))) for row in rows)

    def observation_cursors(
        self,
        *,
        task_id: str = "",
        observer_id: str = "",
        limit: int = 500,
    ) -> tuple[dict[str, Any], ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if observer_id:
            clauses.append("observer_id = ?")
            parameters.append(observer_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(max(1, min(int(limit), 5_000)))
        with self._guard:
            rows = self._connection.execute(
                f"""
                SELECT * FROM observation_source_cursors{where}
                ORDER BY updated_at DESC, scope_key DESC LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(
            {
                "scope_key": str(row["scope_key"]),
                "observer_id": str(row["observer_id"]),
                "run_id": str(row["run_id"]),
                "task_id": str(row["task_id"]),
                "source_state_revision": int(row["source_state_revision"]),
                "fingerprint": str(row["fingerprint"]),
                "observation_id": str(row["observation_id"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        )

    def append_signal(self, signal: FaultSignal) -> tuple[FaultSignal, bool]:
        with self.transaction() as connection:
            observation_row = connection.execute(
                "SELECT observation_id FROM observations WHERE observation_id = ?",
                (signal.refs.observation_id,),
            ).fetchone()
            if observation_row is None:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                    "fault signal cannot be committed before its observation",
                    details={"observation_id": signal.refs.observation_id},
                )
            row = connection.execute(
                "SELECT json FROM fault_signals WHERE fingerprint = ?",
                (signal.fingerprint,),
            ).fetchone()
            if row is not None:
                return signal_from_dict(json.loads(str(row["json"]))), True
            connection.execute(
                """
                INSERT INTO fault_signals(
                    signal_id, observation_id, run_id, task_id, kind, origin,
                    fingerprint, injection_id, terminal, json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signal_id,
                    signal.refs.observation_id,
                    signal.refs.run_id,
                    signal.refs.task_id,
                    signal.kind.value,
                    signal.origin.value,
                    signal.fingerprint,
                    signal.provenance.injection_id,
                    int(signal.terminal),
                    canonical_json(signal.to_dict()),
                    signal.created_at,
                ),
            )
            return signal, False

    def signal(self, signal_id: str) -> FaultSignal | None:
        with self._guard:
            row = self._connection.execute(
                "SELECT json FROM fault_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return None if row is None else signal_from_dict(json.loads(str(row["json"])))

    def signals(
        self,
        *,
        task_id: str = "",
        injection_id: str = "",
        limit: int = 200,
    ) -> tuple[FaultSignal, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if injection_id:
            clauses.append("injection_id = ?")
            parameters.append(injection_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(max(1, min(int(limit), 5_000)))
        with self._guard:
            rows = self._connection.execute(
                f"SELECT json FROM fault_signals{where} ORDER BY created_at DESC, signal_id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple(signal_from_dict(json.loads(str(row["json"]))) for row in rows)

    def begin_injection(
        self,
        request: FaultInjectionRequest,
    ) -> tuple[FaultInjectionRequest, InjectionTransition, bool]:
        with self.transaction() as connection:
            duplicate = connection.execute(
                """
                SELECT request_json FROM injections
                WHERE run_id = ? AND task_id = ? AND idempotency_key = ?
                """,
                (request.run_id, request.task_id, request.idempotency_key),
            ).fetchone()
            if duplicate is not None:
                existing = injection_request_from_dict(json.loads(str(duplicate["request_json"])))
                transitions = self._injection_transitions(connection, existing.injection_id)
                if not transitions:
                    raise FaultRuntimeError(
                        FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                        "injection exists without a transition journal",
                    )
                return existing, transitions[-1], True
            transition = InjectionTransition(
                injection_id=request.injection_id,
                phase=InjectionPhase.REQUESTED,
                revision=1,
                reason="fault injection accepted and fenced",
                details={"idempotency_key": request.idempotency_key},
            )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO injections(
                    injection_id, run_id, task_id, kind, idempotency_key,
                    phase, revision, terminal, request_json, latest_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.injection_id,
                    request.run_id,
                    request.task_id,
                    request.kind.value,
                    request.idempotency_key,
                    transition.phase.value,
                    transition.revision,
                    int(transition.phase.terminal),
                    canonical_json(request.to_dict()),
                    canonical_json(transition.to_dict()),
                    now,
                    now,
                ),
            )
            self._insert_injection_transition(connection, transition)
            return request, transition, False

    def transition_injection(
        self,
        injection_id: str,
        phase: InjectionPhase,
        *,
        expected_revision: int,
        reason: str,
        signal_id: str = "",
        event_id: str = "",
        handoff_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> InjectionTransition:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT phase, revision, terminal FROM injections WHERE injection_id = ?",
                (injection_id,),
            ).fetchone()
            if row is None:
                raise KeyError(injection_id)
            current_revision = int(row["revision"])
            current_phase = InjectionPhase(str(row["phase"]))
            if current_revision != expected_revision:
                raise InjectionStateConflict(injection_id, expected_revision, current_revision)
            if bool(row["terminal"]):
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.INJECTION_ALREADY_TERMINAL,
                    f"injection is already terminal: {injection_id}",
                    details={"phase": current_phase.value},
                )
            self._validate_injection_transition(current_phase, phase)
            transition = InjectionTransition(
                injection_id=injection_id,
                phase=phase,
                revision=current_revision + 1,
                reason=reason,
                signal_id=signal_id,
                event_id=event_id,
                handoff_id=handoff_id,
                details=dict(details or {}),
            )
            connection.execute(
                """
                UPDATE injections
                SET phase = ?, revision = ?, terminal = ?, latest_json = ?, updated_at = ?
                WHERE injection_id = ? AND revision = ?
                """,
                (
                    phase.value,
                    transition.revision,
                    int(phase.terminal),
                    canonical_json(transition.to_dict()),
                    transition.created_at,
                    injection_id,
                    current_revision,
                ),
            )
            if connection.execute("SELECT changes() AS count").fetchone()["count"] != 1:
                raise InjectionStateConflict(injection_id, current_revision, current_revision + 1)
            self._insert_injection_transition(connection, transition)
            return transition

    def injection(self, injection_id: str) -> tuple[FaultInjectionRequest, tuple[InjectionTransition, ...]] | None:
        with self._guard:
            row = self._connection.execute(
                "SELECT request_json FROM injections WHERE injection_id = ?",
                (injection_id,),
            ).fetchone()
            if row is None:
                return None
            request = injection_request_from_dict(json.loads(str(row["request_json"])))
            transitions = self._injection_transitions(self._connection, injection_id)
        return request, transitions

    def injection_by_idempotency(
        self,
        run_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> tuple[FaultInjectionRequest, tuple[InjectionTransition, ...]] | None:
        with self._guard:
            row = self._connection.execute(
                """
                SELECT injection_id, request_json FROM injections
                WHERE run_id = ? AND task_id = ? AND idempotency_key = ?
                """,
                (run_id, task_id, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            request = injection_request_from_dict(json.loads(str(row["request_json"])))
            transitions = self._injection_transitions(self._connection, str(row["injection_id"]))
        return request, transitions

    def injections(
        self,
        *,
        task_id: str = "",
        limit: int = 200,
    ) -> tuple[tuple[FaultInjectionRequest, tuple[InjectionTransition, ...]], ...]:
        parameters: list[Any] = []
        where = ""
        if task_id:
            where = " WHERE task_id = ?"
            parameters.append(task_id)
        parameters.append(max(1, min(int(limit), 5_000)))
        with self._guard:
            rows = self._connection.execute(
                f"SELECT injection_id, request_json FROM injections{where} ORDER BY updated_at DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
            return tuple(
                (
                    injection_request_from_dict(json.loads(str(row["request_json"]))),
                    self._injection_transitions(self._connection, str(row["injection_id"])),
                )
                for row in rows
            )

    def append_handoff(self, handoff: RecoveryHandoff) -> tuple[RecoveryHandoff, bool]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT json FROM recovery_handoffs WHERE signal_id = ?",
                (handoff.signal_id,),
            ).fetchone()
            if row is not None:
                return handoff_from_dict(json.loads(str(row["json"]))), True
            connection.execute(
                """
                INSERT INTO recovery_handoffs(
                    handoff_id, signal_id, injection_id, run_id, task_id,
                    fault_kind, json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff.handoff_id,
                    handoff.signal_id,
                    handoff.injection_id,
                    handoff.run_id,
                    handoff.task_id,
                    handoff.fault_kind.value,
                    canonical_json(handoff.to_dict()),
                    handoff.created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO recovery_handoff_delivery(
                    handoff_id, status, consumer, lease_token,
                    lease_expires_ms, attempts, available_at_ms, revision,
                    last_error, receipt_json, updated_at
                ) VALUES (?, 'pending', '', '', NULL, 0, 0, 1, '', '{}', ?)
                ON CONFLICT(handoff_id) DO NOTHING
                """,
                (handoff.handoff_id, handoff.created_at),
            )
            return handoff, False

    def handoff(self, handoff_id: str) -> RecoveryHandoff | None:
        with self._guard:
            row = self._connection.execute(
                "SELECT json FROM recovery_handoffs WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
        return None if row is None else handoff_from_dict(json.loads(str(row["json"])))

    def handoffs(self, *, task_id: str = "", limit: int = 200) -> tuple[RecoveryHandoff, ...]:
        parameters: list[Any] = []
        where = ""
        if task_id:
            where = " WHERE task_id = ?"
            parameters.append(task_id)
        parameters.append(max(1, min(int(limit), 5_000)))
        with self._guard:
            rows = self._connection.execute(
                f"SELECT json FROM recovery_handoffs{where} ORDER BY created_at DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple(handoff_from_dict(json.loads(str(row["json"]))) for row in rows)

    def claim_handoffs(
        self,
        *,
        consumer: str,
        now_ms: int,
        lease_ms: int,
        task_id: str = "",
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        if not consumer.strip():
            raise ValueError("handoff claim requires a consumer identity")
        if now_ms < 0 or lease_ms < 1:
            raise ValueError("handoff claim requires non-negative time and positive lease")
        selected_limit = max(1, min(int(limit), 1_000))
        claimed: list[dict[str, Any]] = []
        with self.transaction() as connection:
            self._ensure_handoff_deliveries(connection)
            task_clause = " AND h.task_id = ?" if task_id else ""
            parameters: list[Any] = [now_ms, now_ms]
            if task_id:
                parameters.append(task_id)
            parameters.append(selected_limit)
            rows = connection.execute(
                f"""
                SELECT h.json AS handoff_json, d.*
                FROM recovery_handoff_delivery d
                JOIN recovery_handoffs h ON h.handoff_id = d.handoff_id
                WHERE (
                    (d.status = 'pending' AND d.available_at_ms <= ?)
                    OR (d.status = 'leased' AND d.lease_expires_ms <= ?)
                ){task_clause}
                ORDER BY d.available_at_ms, h.created_at, d.handoff_id
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            for row in rows:
                token = runtime_id("handoff-lease")
                revision = int(row["revision"])
                updated = connection.execute(
                    """
                    UPDATE recovery_handoff_delivery
                    SET status = 'leased', consumer = ?, lease_token = ?,
                        lease_expires_ms = ?, attempts = attempts + 1,
                        revision = revision + 1, last_error = '', updated_at = ?
                    WHERE handoff_id = ? AND revision = ?
                      AND (
                        (status = 'pending' AND available_at_ms <= ?)
                        OR (status = 'leased' AND lease_expires_ms <= ?)
                      )
                    """,
                    (
                        consumer,
                        token,
                        now_ms + lease_ms,
                        utc_now(),
                        str(row["handoff_id"]),
                        revision,
                        now_ms,
                        now_ms,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                claimed.append(
                    {
                        "schema": "zyra.recovery-handoff-lease/v1",
                        "handoff": json.loads(str(row["handoff_json"])),
                        "consumer": consumer,
                        "lease_token": token,
                        "lease_expires_ms": now_ms + lease_ms,
                        "attempt": int(row["attempts"]) + 1,
                        "revision": revision + 1,
                    }
                )
        return tuple(claimed)

    def acknowledge_handoff(
        self,
        handoff_id: str,
        *,
        consumer: str,
        lease_token: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not handoff_id or not consumer or not lease_token:
            raise ValueError("handoff acknowledgement requires handoff, consumer and lease token")
        with self.transaction() as connection:
            row = self._leased_handoff(connection, handoff_id, consumer, lease_token)
            revision = int(row["revision"])
            now = utc_now()
            connection.execute(
                """
                UPDATE recovery_handoff_delivery
                SET status = 'acknowledged', lease_token = '', lease_expires_ms = NULL,
                    revision = revision + 1, receipt_json = ?, updated_at = ?
                WHERE handoff_id = ? AND revision = ?
                """,
                (canonical_json(dict(receipt or {})), now, handoff_id, revision),
            )
            return {
                "handoff_id": handoff_id,
                "status": "acknowledged",
                "consumer": consumer,
                "attempts": int(row["attempts"]),
                "revision": revision + 1,
                "receipt": dict(receipt or {}),
                "updated_at": now,
            }

    def release_handoff(
        self,
        handoff_id: str,
        *,
        consumer: str,
        lease_token: str,
        now_ms: int,
        retry_after_ms: int,
        error: str,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        if now_ms < 0 or retry_after_ms < 0 or max_attempts < 1:
            raise ValueError("invalid handoff retry policy")
        with self.transaction() as connection:
            row = self._leased_handoff(connection, handoff_id, consumer, lease_token)
            attempts = int(row["attempts"])
            status = "dead" if attempts >= max_attempts else "pending"
            available_at_ms = now_ms if status == "dead" else now_ms + retry_after_ms
            revision = int(row["revision"])
            now = utc_now()
            connection.execute(
                """
                UPDATE recovery_handoff_delivery
                SET status = ?, consumer = '', lease_token = '', lease_expires_ms = NULL,
                    available_at_ms = ?, revision = revision + 1,
                    last_error = ?, updated_at = ?
                WHERE handoff_id = ? AND revision = ?
                """,
                (status, available_at_ms, error, now, handoff_id, revision),
            )
            return {
                "handoff_id": handoff_id,
                "status": status,
                "attempts": attempts,
                "available_at_ms": available_at_ms,
                "revision": revision + 1,
                "last_error": error,
                "updated_at": now,
            }

    def handoff_deliveries(
        self,
        *,
        task_id: str = "",
        statuses: tuple[str, ...] = (),
        limit: int = 200,
    ) -> tuple[dict[str, Any], ...]:
        allowed = {"pending", "leased", "acknowledged", "dead"}
        selected = tuple(dict.fromkeys(str(item) for item in statuses))
        if any(item not in allowed for item in selected):
            raise ValueError("unknown handoff delivery status")
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("h.task_id = ?")
            parameters.append(task_id)
        if selected:
            placeholders = ",".join("?" for _item in selected)
            clauses.append(f"d.status IN ({placeholders})")
            parameters.extend(selected)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(max(1, min(int(limit), 5_000)))
        with self.transaction() as connection:
            self._ensure_handoff_deliveries(connection)
            rows = connection.execute(
                f"""
                SELECT d.*, h.task_id, h.run_id, h.signal_id, h.fault_kind
                FROM recovery_handoff_delivery d
                JOIN recovery_handoffs h ON h.handoff_id = d.handoff_id
                {where}
                ORDER BY h.created_at DESC, d.handoff_id DESC
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(self._handoff_delivery_row(row) for row in rows)

    def append_projection_receipt(self, receipt: ProjectionReceipt) -> tuple[ProjectionReceipt, bool]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT json FROM projection_receipts WHERE signal_id = ?",
                (receipt.signal_id,),
            ).fetchone()
            if row is not None:
                return self._projection_from_dict(json.loads(str(row["json"]))), True
            connection.execute(
                """
                INSERT INTO projection_receipts(
                    receipt_id, signal_id, event_id, canonical_event_written,
                    scheduler_changed, task_projection_changed, json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.signal_id,
                    receipt.event_id,
                    int(receipt.canonical_event_written),
                    int(receipt.scheduler_changed),
                    int(receipt.task_projection_changed),
                    canonical_json(receipt.to_dict()),
                    receipt.created_at,
                ),
            )
            return receipt, False

    def projection_receipt(self, signal_id: str) -> ProjectionReceipt | None:
        with self._guard:
            row = self._connection.execute(
                "SELECT json FROM projection_receipts WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return None if row is None else self._projection_from_dict(json.loads(str(row["json"])))

    def record_delivery_attempt(
        self,
        *,
        signal_id: str,
        destination: str = "",
        status: str = "",
        attempt: int = 1,
        idempotency_key: str = "",
        error: str = "",
        details: Mapping[str, Any] | None = None,
        sink: str = "",
        ok: bool | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected_destination = destination or sink
        if not selected_destination:
            raise ValueError("delivery attempt requires a destination")
        selected_status = status or (
            "succeeded" if ok is True else ("failed" if ok is False else "unknown")
        )
        selected_idempotency = idempotency_key or (
            f"{signal_id}:{selected_destination}:{int(attempt)}"
        )
        value = {
            "attempt_id": runtime_id("faultdelivery"),
            "signal_id": signal_id,
            "destination": selected_destination,
            "status": selected_status,
            "attempt": int(attempt),
            "idempotency_key": selected_idempotency,
            "error": error,
            "details": {**dict(details or {}), **dict(receipt or {})},
            "created_at": utc_now(),
        }
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT attempt_id, signal_id, destination, status, attempt,
                       idempotency_key, error, details_json, created_at
                FROM delivery_attempts
                WHERE signal_id = ? AND destination = ? AND idempotency_key = ?
                """,
                (signal_id, selected_destination, selected_idempotency),
            ).fetchone()
            if row is not None:
                return self._delivery_row(row)
            connection.execute(
                """
                INSERT INTO delivery_attempts(
                    attempt_id, signal_id, destination, status, attempt,
                    idempotency_key, error, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value["attempt_id"],
                    signal_id,
                    selected_destination,
                    selected_status,
                    int(attempt),
                    selected_idempotency,
                    error,
                    canonical_json(value["details"]),
                    value["created_at"],
                ),
            )
        return value

    def delivery_attempts(self, signal_id: str) -> tuple[dict[str, Any], ...]:
        with self._guard:
            rows = self._connection.execute(
                "SELECT * FROM delivery_attempts WHERE signal_id = ? ORDER BY created_at, attempt_id",
                (signal_id,),
            ).fetchall()
        return tuple(self._delivery_row(row) for row in rows)

    def put_metadata(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> int:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT revision FROM runtime_metadata WHERE key = ?",
                (key,),
            ).fetchone()
            current = 0 if row is None else int(row["revision"])
            if expected_revision is not None and current != expected_revision:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                    f"metadata revision conflict for {key}: expected {expected_revision}, actual {current}",
                )
            revision = current + 1
            connection.execute(
                """
                INSERT INTO runtime_metadata(key, value_json, revision, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (key, canonical_json(dict(value)), revision, utc_now()),
            )
            return revision

    def metadata(self, key: str) -> tuple[dict[str, Any], int] | None:
        with self._guard:
            row = self._connection.execute(
                "SELECT value_json, revision FROM runtime_metadata WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["value_json"])), int(row["revision"])

    def snapshot(self, *, task_id: str = "") -> dict[str, Any]:
        observers = self.observers()
        signals = self.signals(task_id=task_id, limit=500)
        observations = self.observations(task_id=task_id, limit=500)
        injections = self.injections(task_id=task_id, limit=500)
        handoffs = self.handoffs(task_id=task_id, limit=500)
        handoff_deliveries = self.handoff_deliveries(task_id=task_id, limit=500)
        observation_cursors = self.observation_cursors(task_id=task_id, limit=500)
        return {
            "schema": "zyra.watchdog-fault-state/v1",
            "state_owner": "FaultStateStore",
            "path": str(self.path),
            "task_id": task_id,
            "observers": [item.to_dict() for item in observers],
            "observations": [item.to_dict() for item in observations],
            "observation_source_cursors": list(observation_cursors),
            "signals": [item.to_dict() for item in signals],
            "injections": [
                {
                    "request": request.to_dict(),
                    "transitions": [item.to_dict() for item in transitions],
                }
                for request, transitions in injections
            ],
            "recovery_handoffs": [item.to_dict() for item in handoffs],
            "recovery_handoff_deliveries": list(handoff_deliveries),
            "counts": {
                "observers": len(observers),
                "observations": len(observations),
                "observation_source_cursors": len(observation_cursors),
                "signals": len(signals),
                "injections": len(injections),
                "recovery_handoffs": len(handoffs),
                "recovery_handoff_deliveries": len(handoff_deliveries),
                "recovery_handoffs_pending": sum(
                    item["status"] in {"pending", "leased"}
                    for item in handoff_deliveries
                ),
                "recovery_handoffs_acknowledged": sum(
                    item["status"] == "acknowledged"
                    for item in handoff_deliveries
                ),
                "recovery_handoffs_dead": sum(
                    item["status"] == "dead"
                    for item in handoff_deliveries
                ),
            },
            "second_domain_store": False,
        }

    def _write_observer(self, connection: sqlite3.Connection, state: ObserverState) -> None:
        connection.execute(
            """
            INSERT INTO observer_state(
                observer_id, lifecycle, revision, descriptor_revision, maturity,
                observation_point, source_revision, json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(observer_id) DO UPDATE SET
                lifecycle = excluded.lifecycle,
                revision = excluded.revision,
                descriptor_revision = excluded.descriptor_revision,
                maturity = excluded.maturity,
                observation_point = excluded.observation_point,
                source_revision = excluded.source_revision,
                json = excluded.json,
                updated_at = excluded.updated_at
            """,
            (
                state.descriptor.observer_id,
                state.lifecycle.value,
                state.revision,
                state.descriptor.descriptor_revision,
                state.descriptor.maturity.value,
                state.descriptor.observation_point,
                state.descriptor.source_revision,
                canonical_json(state.to_dict()),
                state.updated_at,
            ),
        )

    @staticmethod
    def _fence_observation_cursor(
        connection: sqlite3.Connection,
        observation: StructuredObservation,
    ) -> None:
        refs = observation.refs.to_dict()
        refs.pop("observation_id", None)
        refs.pop("source_state_revision", None)
        scope_key = stable_digest(
            {
                "observer_id": observation.provenance.observer_id,
                "injection_id": observation.provenance.injection_id,
                "category": observation.category.value,
                "refs": refs,
            }
        )
        row = connection.execute(
            """
            SELECT source_state_revision, fingerprint, observation_id
            FROM observation_source_cursors WHERE scope_key = ?
            """,
            (scope_key,),
        ).fetchone()
        revision = observation.refs.source_state_revision
        if row is not None:
            current_revision = int(row["source_state_revision"])
            if revision < current_revision:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.INVALID_OBSERVATION,
                    "observation source revision regressed after runtime restore",
                    details={
                        "observer_id": observation.provenance.observer_id,
                        "scope_key": scope_key,
                        "current_revision": current_revision,
                        "received_revision": revision,
                        "current_observation_id": str(row["observation_id"]),
                    },
                )
            if revision == current_revision:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.INVALID_OBSERVATION,
                    "observation source revision was reused with different evidence",
                    details={
                        "observer_id": observation.provenance.observer_id,
                        "scope_key": scope_key,
                        "revision": revision,
                        "current_fingerprint": str(row["fingerprint"]),
                        "received_fingerprint": observation.fingerprint,
                    },
                )
            connection.execute(
                """
                UPDATE observation_source_cursors
                SET source_state_revision = ?, fingerprint = ?, observation_id = ?,
                    updated_at = ?
                WHERE scope_key = ? AND source_state_revision = ?
                """,
                (
                    revision,
                    observation.fingerprint,
                    observation.observation_id,
                    observation.observed_at,
                    scope_key,
                    current_revision,
                ),
            )
            if connection.execute("SELECT changes() AS count").fetchone()["count"] != 1:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                    "observation source cursor changed concurrently",
                    details={"scope_key": scope_key},
                )
            return
        connection.execute(
            """
            INSERT INTO observation_source_cursors(
                scope_key, observer_id, run_id, task_id, source_state_revision,
                fingerprint, observation_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope_key,
                observation.provenance.observer_id,
                observation.refs.run_id,
                observation.refs.task_id,
                revision,
                observation.fingerprint,
                observation.observation_id,
                observation.observed_at,
            ),
        )

    @staticmethod
    def _insert_observer_transition(
        connection: sqlite3.Connection,
        state: ObserverState,
        *,
        from_lifecycle: str,
        from_revision: int,
        reason: str,
    ) -> None:
        transition = {
            "transition_id": runtime_id("observerstep"),
            "observer_id": state.descriptor.observer_id,
            "from_lifecycle": from_lifecycle,
            "to_lifecycle": state.lifecycle.value,
            "from_revision": from_revision,
            "to_revision": state.revision,
            "reason": reason,
            "created_at": state.updated_at,
        }
        connection.execute(
            """
            INSERT INTO observer_transitions(
                transition_id, observer_id, from_lifecycle, to_lifecycle,
                from_revision, to_revision, reason, json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition["transition_id"],
                transition["observer_id"],
                transition["from_lifecycle"],
                transition["to_lifecycle"],
                transition["from_revision"],
                transition["to_revision"],
                transition["reason"],
                canonical_json(transition),
                transition["created_at"],
            ),
        )

    @staticmethod
    def _insert_injection_transition(
        connection: sqlite3.Connection,
        transition: InjectionTransition,
    ) -> None:
        connection.execute(
            """
            INSERT INTO injection_transitions(
                transition_id, injection_id, phase, revision, signal_id,
                event_id, handoff_id, json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition.transition_id,
                transition.injection_id,
                transition.phase.value,
                transition.revision,
                transition.signal_id,
                transition.event_id,
                transition.handoff_id,
                canonical_json(transition.to_dict()),
                transition.created_at,
            ),
        )

    @staticmethod
    def _injection_transitions(
        connection: sqlite3.Connection,
        injection_id: str,
    ) -> tuple[InjectionTransition, ...]:
        rows = connection.execute(
            """
            SELECT json FROM injection_transitions
            WHERE injection_id = ? ORDER BY revision
            """,
            (injection_id,),
        ).fetchall()
        return tuple(transition_from_dict(json.loads(str(row["json"]))) for row in rows)

    @staticmethod
    def _validate_observer_transition(
        current: ObserverLifecycle,
        target: ObserverLifecycle,
    ) -> None:
        allowed = {
            ObserverLifecycle.REGISTERED: {ObserverLifecycle.ATTACHED, ObserverLifecycle.DISABLED, ObserverLifecycle.FAILED},
            ObserverLifecycle.ATTACHED: {ObserverLifecycle.RUNNING, ObserverLifecycle.STOPPED, ObserverLifecycle.DISABLED, ObserverLifecycle.FAILED},
            ObserverLifecycle.RUNNING: {ObserverLifecycle.STOPPED, ObserverLifecycle.DISABLED, ObserverLifecycle.FAILED},
            ObserverLifecycle.STOPPED: {ObserverLifecycle.ATTACHED, ObserverLifecycle.RUNNING, ObserverLifecycle.DISABLED, ObserverLifecycle.FAILED},
            ObserverLifecycle.DISABLED: {ObserverLifecycle.ATTACHED},
            ObserverLifecycle.FAILED: {ObserverLifecycle.ATTACHED, ObserverLifecycle.DISABLED},
        }
        if target not in allowed[current]:
            raise FaultRuntimeError(
                FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                f"invalid observer lifecycle transition: {current.value} -> {target.value}",
            )

    @staticmethod
    def _validate_injection_transition(current: InjectionPhase, target: InjectionPhase) -> None:
        allowed = {
            InjectionPhase.REQUESTED: {InjectionPhase.ARMED, InjectionPhase.REJECTED, InjectionPhase.FAILED},
            InjectionPhase.ARMED: {InjectionPhase.TRIGGERED, InjectionPhase.REJECTED, InjectionPhase.FAILED},
            InjectionPhase.TRIGGERED: {InjectionPhase.OBSERVED, InjectionPhase.FAILED},
            InjectionPhase.OBSERVED: {InjectionPhase.PROJECTED, InjectionPhase.FAILED},
            InjectionPhase.PROJECTED: {InjectionPhase.CONTINUED, InjectionPhase.HANDED_OFF, InjectionPhase.FAILED},
        }
        if current.terminal or target not in allowed.get(current, set()):
            raise FaultRuntimeError(
                FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                f"invalid injection transition: {current.value} -> {target.value}",
            )

    @staticmethod
    def _projection_from_dict(value: Mapping[str, Any]) -> ProjectionReceipt:
        return ProjectionReceipt(
            receipt_id=str(value.get("receipt_id") or ""),
            signal_id=str(value.get("signal_id") or ""),
            event_id=str(value.get("event_id") or ""),
            canonical_event_written=bool(value.get("canonical_event_written")),
            memory_record_ids=tuple(str(item) for item in value.get("memory_record_ids") or ()),
            scheduler_changed=bool(value.get("scheduler_changed")),
            scheduler_receipt=(
                dict(value.get("scheduler_receipt") or {})
                if isinstance(value.get("scheduler_receipt"), Mapping)
                else {}
            ),
            task_projection_changed=bool(value.get("task_projection_changed")),
            created_at=str(value.get("created_at") or ""),
            errors=tuple(str(item) for item in value.get("errors") or ()),
        )

    @staticmethod
    def _delivery_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": str(row["attempt_id"]),
            "signal_id": str(row["signal_id"]),
            "destination": str(row["destination"]),
            "status": str(row["status"]),
            "attempt": int(row["attempt"]),
            "idempotency_key": str(row["idempotency_key"]),
            "error": str(row["error"]),
            "details": json.loads(str(row["details_json"])),
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _ensure_handoff_deliveries(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO recovery_handoff_delivery(
                handoff_id, status, consumer, lease_token, lease_expires_ms,
                attempts, available_at_ms, revision, last_error,
                receipt_json, updated_at
            )
            SELECT handoff_id, 'pending', '', '', NULL, 0, 0, 1, '', '{}', created_at
            FROM recovery_handoffs
            WHERE 1 = 1
            ON CONFLICT(handoff_id) DO NOTHING
            """
        )

    @staticmethod
    def _leased_handoff(
        connection: sqlite3.Connection,
        handoff_id: str,
        consumer: str,
        lease_token: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM recovery_handoff_delivery
            WHERE handoff_id = ?
            """,
            (handoff_id,),
        ).fetchone()
        if row is None:
            raise KeyError(handoff_id)
        if str(row["status"]) != "leased":
            raise FaultRuntimeError(
                FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                "recovery handoff does not hold an active delivery lease",
                details={"handoff_id": handoff_id, "status": str(row["status"])},
            )
        if str(row["consumer"]) != consumer or str(row["lease_token"]) != lease_token:
            raise FaultRuntimeError(
                FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                "recovery handoff lease ownership differs from acknowledgement",
                details={"handoff_id": handoff_id, "consumer": consumer},
            )
        return row

    @staticmethod
    def _handoff_delivery_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-handoff-delivery/v1",
            "handoff_id": str(row["handoff_id"]),
            "run_id": str(row["run_id"]),
            "task_id": str(row["task_id"]),
            "signal_id": str(row["signal_id"]),
            "fault_kind": str(row["fault_kind"]),
            "status": str(row["status"]),
            "consumer": str(row["consumer"]),
            "lease_token": str(row["lease_token"]),
            "lease_expires_ms": (
                None if row["lease_expires_ms"] is None else int(row["lease_expires_ms"])
            ),
            "attempts": int(row["attempts"]),
            "available_at_ms": int(row["available_at_ms"]),
            "revision": int(row["revision"]),
            "last_error": str(row["last_error"]),
            "receipt": json.loads(str(row["receipt_json"])),
            "updated_at": str(row["updated_at"]),
        }
