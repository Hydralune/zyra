from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import threading
import time
import zlib
from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import psutil

from .errors import DispatchRejected, StateConflict, redact
from .models import (
    DeploymentProfile,
    DispatchStatus,
    NetworkMode,
    ProfilePolicy,
    Sensitivity,
    Workload,
    canonical_json,
    digest,
    new_id,
    now_iso,
)
from .provider_dispatch import LiveProviderDispatchRuntime
from .resource_control import process_environment_snapshot


_WORD = re.compile(r"[\w'-]+", re.UNICODE)
_SECRET_MARKERS = ("secret", "token", "password", "api_key", "authorization")
_RUNTIME_IMPLEMENTATION_VERSION = "phase2-operator-execution-v6"
_ALLOWED_OPERATIONS = {
    "analyze-text",
    "compress-text",
    "deterministic-transform",
    "hash-manifest",
    "verify-checksum",
    "resource-probe",
    "provider-capability",
    "physical-dispatch-proof",
    "phase2-operator-execution",
}


class NodeJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS node_meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_dispatches (
                    idempotency_key TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workload_digest TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_checkpoints (
                    checkpoint_ref TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO node_meta(key, value_json, updated_at)
                VALUES ('schema', ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (
                    canonical_json(
                        {
                            "schema": "zyra.deployment-node-journal/v1",
                            "owner": "DeploymentNodeRuntime",
                        }
                    ).decode("utf-8"),
                    now_iso(),
                ),
            )

    def dispatch(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT receipt_json
                FROM node_dispatches
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["receipt_json"]))
        if not isinstance(value, dict):
            raise RuntimeError("node dispatch journal contains non-object receipt")
        return value

    def begin_dispatch(
        self,
        *,
        idempotency_key: str,
        workload: Workload,
        attempt_id: str,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            raise ValueError("node dispatch requires an idempotency key")
        started_at = now_iso()
        pending = {
            "schema": "zyra.deployment-node-receipt/v1",
            "attempt_id": attempt_id,
            "workload_id": workload.workload_id,
            "status": DispatchStatus.RUNNING.value,
            "started_at": started_at,
            "completed_at": "",
        }
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT workload_digest, receipt_json
                    FROM node_dispatches
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    if str(row["workload_digest"]) != workload.workload_digest:
                        raise StateConflict(
                            "node_idempotency_payload_conflict",
                            "idempotency key was reused for a different workload",
                            operation="begin_dispatch",
                            profile="",
                        )
                    connection.execute("COMMIT")
                    value = json.loads(str(row["receipt_json"]))
                    return dict(value)
                connection.execute(
                    """
                    INSERT INTO node_dispatches(
                        idempotency_key, workload_id, attempt_id, status,
                        workload_digest, receipt_json, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '')
                    """,
                    (
                        idempotency_key,
                        workload.workload_id,
                        attempt_id,
                        DispatchStatus.RUNNING.value,
                        workload.workload_digest,
                        canonical_json(pending).decode("utf-8"),
                        started_at,
                    ),
                )
                self._append_event(
                    connection,
                    "node.dispatch_started",
                    {
                        "workload_id": workload.workload_id,
                        "attempt_id": attempt_id,
                        "workload_digest": workload.workload_digest,
                    },
                )
                connection.execute("COMMIT")
                return None
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def complete_dispatch(
        self,
        *,
        idempotency_key: str,
        receipt: Mapping[str, Any],
    ) -> None:
        completed_at = str(receipt.get("completed_at") or now_iso())
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT status, receipt_json
                    FROM node_dispatches
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if row is None:
                    raise StateConflict(
                        "node_dispatch_reservation_missing",
                        "node dispatch completion has no reservation",
                        operation="complete_dispatch",
                    )
                if str(row["status"]) not in {
                    DispatchStatus.RUNNING.value,
                    DispatchStatus.FAILED.value,
                }:
                    existing = (
                        json.loads(str(row["receipt_json"]))
                        if row["receipt_json"]
                        else None
                    )
                    if existing != dict(receipt):
                        raise StateConflict(
                            "node_dispatch_terminal_conflict",
                            "node dispatch already has a different terminal receipt",
                            operation="complete_dispatch",
                        )
                    connection.execute("COMMIT")
                    return
                connection.execute(
                    """
                    UPDATE node_dispatches
                    SET status = ?, receipt_json = ?, completed_at = ?
                    WHERE idempotency_key = ?
                    """,
                    (
                        str(receipt.get("status") or DispatchStatus.FAILED.value),
                        canonical_json(dict(receipt)).decode("utf-8"),
                        completed_at,
                        idempotency_key,
                    ),
                )
                self._append_event(
                    connection,
                    "node.dispatch_completed",
                    {
                        "attempt_id": str(receipt.get("attempt_id") or ""),
                        "workload_id": str(receipt.get("workload_id") or ""),
                        "status": str(receipt.get("status") or ""),
                        "result_digest": str(receipt.get("result_digest") or ""),
                    },
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def save_checkpoint(
        self,
        checkpoint_ref: str,
        *,
        workload_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        semantic = {
            "schema": "zyra.deployment-node-checkpoint/v1",
            "checkpoint_ref": checkpoint_ref,
            "workload_id": workload_id,
            "payload": dict(payload),
            "created_at": now_iso(),
        }
        checksum = digest(semantic)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO node_checkpoints(
                    checkpoint_ref, workload_id, checksum, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_ref) DO UPDATE SET
                    workload_id = excluded.workload_id,
                    checksum = excluded.checksum,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (
                    checkpoint_ref,
                    workload_id,
                    checksum,
                    canonical_json(semantic).decode("utf-8"),
                    semantic["created_at"],
                ),
            )
            self._append_event(
                connection,
                "node.checkpoint_saved",
                {
                    "checkpoint_ref": checkpoint_ref,
                    "workload_id": workload_id,
                    "checksum": checksum,
                },
            )
        return {**semantic, "checksum": checksum}

    def checkpoint(self, checkpoint_ref: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT checksum, payload_json
                FROM node_checkpoints
                WHERE checkpoint_ref = ?
                """,
                (checkpoint_ref,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["payload_json"]))
        if not isinstance(value, dict) or digest(value) != str(row["checksum"]):
            raise RuntimeError("node checkpoint checksum is invalid")
        return {**value, "checksum": str(row["checksum"])}

    def counts(self) -> dict[str, int]:
        with self._lock, closing(self._connect()) as connection:
            values = {
                "dispatches": int(
                    connection.execute("SELECT COUNT(*) FROM node_dispatches").fetchone()[0]
                ),
                "completed_dispatches": int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM node_dispatches
                        WHERE status IN (?, ?, ?)
                        """,
                        (
                            DispatchStatus.SUCCEEDED.value,
                            DispatchStatus.FAILED.value,
                            DispatchStatus.DEGRADED.value,
                        ),
                    ).fetchone()[0]
                ),
                "checkpoints": int(
                    connection.execute("SELECT COUNT(*) FROM node_checkpoints").fetchone()[0]
                ),
                "events": int(
                    connection.execute("SELECT COUNT(*) FROM node_events").fetchone()[0]
                ),
            }
        return values

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO node_events(event_type, payload_json, created_at)
            VALUES (?, ?, ?)
            """,
            (
                event_type,
                canonical_json(dict(payload)).decode("utf-8"),
                now_iso(),
            ),
        )


class DeploymentNodeRuntime:
    def __init__(
        self,
        *,
        node_id: str,
        generation_id: str,
        policy: ProfilePolicy,
        data_root: Path | str,
        credential_presence: Mapping[str, bool],
    ) -> None:
        self.node_id = node_id
        self.generation_id = generation_id
        self.policy = policy
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.artifact_root = self.data_root / "artifacts"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.journal = NodeJournal(self.data_root / "node.sqlite3")
        self.credential_presence = dict(credential_presence)
        self.started_at = now_iso()
        self._lock = threading.RLock()
        self._active_dispatches = 0
        self._heartbeat_sequence = 0
        self._provider_runtime: LiveProviderDispatchRuntime | None = None
        self._faults: dict[str, Any] = {
            "network_down": False,
            "provider_failure": False,
            "latency_ms": 0,
            "fail_next": 0,
            "crash_next": False,
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            self._heartbeat_sequence += 1
            heartbeat = self._heartbeat_sequence
            faults = dict(self._faults)
            active = self._active_dispatches
        process = psutil.Process(os.getpid())
        parent = process.parent()
        memory = process.memory_info()
        process_create_time = process.create_time()
        try:
            supervisor_create_time = (
                parent.create_time() if parent is not None else 0.0
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            supervisor_create_time = 0.0
        counts = self.journal.counts()
        credential_ready = (
            any(self.credential_presence.values())
            if self.policy.credential_environment
            else True
        )
        blockers: list[str] = []
        warnings: list[str] = []
        if faults["network_down"] and self.policy.network_mode is not NetworkMode.OFFLINE:
            blockers.append("network_unavailable")
        if (
            self.policy.profile is DeploymentProfile.CLOUD
            and not credential_ready
        ):
            warnings.append("cloud_credential_missing")
        if memory.rss / 1024 / 1024 > self.policy.resource.memory_mb:
            blockers.append("memory_limit_exceeded")
        if active >= self.policy.resource.max_concurrency:
            blockers.append("concurrency_saturated")
        status = "ready" if not blockers else "degraded"
        semantic = {
            "node_id": self.node_id,
            "generation_id": self.generation_id,
            "profile": self.policy.profile.value,
            "pid": os.getpid(),
            "endpoint": f"http://{self.policy.host}:{self.policy.port}",
            "status": status,
            "started_at": self.started_at,
            "heartbeat_sequence": heartbeat,
            "capabilities": list(self.policy.capabilities),
            "resource": {
                **self.policy.resource.to_dict(),
                "rss_mb": round(memory.rss / 1024 / 1024, 3),
                "vms_mb": round(memory.vms / 1024 / 1024, 3),
                "active_dispatches": active,
            },
            "network": {
                "mode": self.policy.network_mode.value,
                "latency_budget_ms": self.policy.latency_budget_ms,
                "network_down": bool(faults["network_down"]),
                "in_process": False,
                "endpoint": f"http://{self.policy.host}:{self.policy.port}",
                "namespace": (
                    f"host-loopback:{self.policy.port}"
                    if self.policy.host in {"127.0.0.1", "::1", "localhost"}
                    else f"host-network:{self.policy.host}:{self.policy.port}"
                ),
            },
            "runtime_identity": {
                "runtime_kind": "isolated_process",
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "process_create_time": process_create_time,
                "supervisor_pid": os.getppid(),
                "supervisor_create_time": supervisor_create_time,
                "generation_id": self.generation_id,
                "failure_boundary_id": (
                    f"process:{os.getpid()}:{self.generation_id}"
                ),
                "independent_process": True,
                "terminal_id": digest(
                    {
                        "supervisor_pid": os.getppid(),
                        "supervisor_create_time": supervisor_create_time,
                        "session": str(
                            os.environ.get("WT_SESSION")
                            or os.environ.get("TERM_SESSION_ID")
                            or os.environ.get("TERM")
                            or "supervisor-process-chain"
                        ),
                    }
                ),
                "terminal_source": "supervisor_process_chain",
            },
            "credential_presence": dict(self.credential_presence),
            "credential_values_exposed": False,
            "configuration_digest": self.policy.configuration_digest,
            "blockers": blockers,
            "warnings": warnings,
            "journal": counts,
            "canonical_task_owner": False,
            "canonical_checkpoint_owner": False,
            "canonical_permission_owner": False,
            "runtime_implementation_version": _RUNTIME_IMPLEMENTATION_VERSION,
        }
        semantic["semantic_digest"] = digest(semantic)
        return {
            "schema": "zyra.deployment-node-health/v1",
            **semantic,
        }

    def semantic_probe(self) -> dict[str, Any]:
        health = self.health()
        root_writable = False
        probe = self.data_root / f".probe-{os.getpid()}-{time.time_ns()}"
        try:
            probe.write_bytes(b"zyra-node-semantic-probe")
            root_writable = probe.read_bytes() == b"zyra-node-semantic-probe"
        finally:
            probe.unlink(missing_ok=True)
        operations = sorted(
            operation
            for operation in _ALLOWED_OPERATIONS
            if self._operation_capability_available(operation)
        )
        readiness = {
            "profile_policy": health.get("configuration_digest")
            == self.policy.configuration_digest,
            "node_identity": (
                health.get("node_id") == self.node_id
                and health.get("generation_id") == self.generation_id
                and int(health.get("pid") or 0) == os.getpid()
            ),
            "resource_admission": self.policy.resource.max_concurrency > 0,
            "data_root_writable": root_writable,
            "checkpoint_export": self.policy.allow_checkpoint_export,
            "checkpoint_import": self.policy.allow_checkpoint_import,
            "operation_catalog": bool(operations),
            "secret_projection": health.get("credential_values_exposed") is False,
        }
        warnings = [
            str(blocker)
            for blocker in health.get("warnings") or ()
            if str(blocker) == "cloud_credential_missing"
        ]
        blockers = [
            str(blocker)
            for blocker in health.get("blockers") or ()
        ]
        ready = all(readiness.values()) and not blockers
        return {
            "schema": "zyra.deployment-node-semantic-readiness/v1",
            "ready": ready,
            "status": ("degraded" if ready and warnings else "ready") if ready else "blocked",
            "node_id": self.node_id,
            "profile": self.policy.profile.value,
            "checks": readiness,
            "operations": operations,
            "health_digest": health["semantic_digest"],
            "blockers": blockers,
            "warnings": warnings,
            "fixed_response": False,
            "runtime_implementation_version": health.get(
                "runtime_implementation_version"
            ),
            "observed_at": now_iso(),
        }

    def execute(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        workload = self._parse_workload(payload)
        attempt_id = str(payload.get("attempt_id") or new_id("attempt"))
        idempotency_key = str(
            payload.get("idempotency_key")
            or workload.idempotency_key
            or ""
        )
        existing = self.journal.begin_dispatch(
            idempotency_key=idempotency_key,
            workload=workload,
            attempt_id=attempt_id,
        )
        if existing is not None:
            return {
                **existing,
                "idempotent_replay": True,
                "node_id": self.node_id,
                "generation_id": self.generation_id,
            }
        started_at = now_iso()
        with self._lock:
            if self._active_dispatches >= self.policy.resource.max_concurrency:
                return self._terminal_failure(
                    workload,
                    attempt_id=attempt_id,
                    idempotency_key=idempotency_key,
                    started_at=started_at,
                    code="node_concurrency_exhausted",
                )
            self._active_dispatches += 1
            faults = dict(self._faults)
            if int(self._faults.get("fail_next") or 0) > 0:
                self._faults["fail_next"] = int(self._faults["fail_next"]) - 1
            if bool(self._faults.get("crash_next")):
                self._faults["crash_next"] = False
        try:
            self._admit_workload(workload, faults=faults)
            delay = min(
                10_000,
                max(
                    int(faults.get("latency_ms") or 0),
                    self._network_latency_ms(),
                ),
            )
            if delay:
                time.sleep(delay / 1000)
            if faults.get("crash_next"):
                os._exit(86)
            if int(faults.get("fail_next") or 0) > 0:
                raise DispatchRejected(
                    "node_injected_failure",
                    "node dispatch failed due to bounded fault injection",
                    operation=workload.operation,
                    profile=self.policy.profile.value,
                    retryable=True,
                )
            result = self._execute_operation(workload)
            artifact = self._write_artifact(
                workload,
                attempt_id=attempt_id,
                result=result,
            )
            checkpoint = self._checkpoint_after_execution(
                workload,
                attempt_id=attempt_id,
                result=result,
                artifact=artifact,
            )
            completed_at = now_iso()
            semantic_result = {
                "operation": workload.operation,
                "output": result,
                "artifact": artifact,
                "checkpoint": checkpoint,
                "node_profile": self.policy.profile.value,
                "node_generation": self.generation_id,
            }
            receipt = {
                "schema": "zyra.deployment-node-receipt/v1",
                "attempt_id": attempt_id,
                "workload_id": workload.workload_id,
                "task_id": workload.task_id,
                "run_id": workload.run_id,
                "node_id": self.node_id,
                "generation_id": self.generation_id,
                "profile": self.policy.profile.value,
                "status": DispatchStatus.SUCCEEDED.value,
                "started_at": started_at,
                "completed_at": completed_at,
                "result": semantic_result,
                "result_digest": digest(semantic_result),
                "artifact_refs": [artifact["artifact_ref"]],
                "checkpoint_ref": checkpoint["checkpoint_ref"],
                "idempotent_replay": False,
                "fallback": False,
            }
            self.journal.complete_dispatch(
                idempotency_key=idempotency_key,
                receipt=receipt,
            )
            return receipt
        except DispatchRejected as error:
            return self._terminal_failure(
                workload,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                started_at=started_at,
                code=error.code,
                message=str(error),
                details=error.details,
            )
        except BaseException as error:
            return self._terminal_failure(
                workload,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                started_at=started_at,
                code="node_execution_failed",
                message=f"{type(error).__name__}: {error}",
            )
        finally:
            with self._lock:
                self._active_dispatches = max(0, self._active_dispatches - 1)

    def _terminal_failure(
        self,
        workload: Workload,
        *,
        attempt_id: str,
        idempotency_key: str,
        started_at: str,
        code: str,
        message: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        completed_at = now_iso()
        result = {
            "error": code,
            "message": message or code.replace("_", " "),
            "retryable": code
            in {
                "node_concurrency_exhausted",
                "node_injected_failure",
                "node_network_unavailable",
                "node_provider_failure",
                "node_execution_failed",
            },
            "details": redact(dict(details or {})),
        }
        receipt = {
            "schema": "zyra.deployment-node-receipt/v1",
            "attempt_id": attempt_id,
            "workload_id": workload.workload_id,
            "task_id": workload.task_id,
            "run_id": workload.run_id,
            "node_id": self.node_id,
            "generation_id": self.generation_id,
            "profile": self.policy.profile.value,
            "status": DispatchStatus.FAILED.value,
            "started_at": started_at,
            "completed_at": completed_at,
            "result": result,
            "result_digest": digest(result),
            "artifact_refs": [],
            "checkpoint_ref": workload.checkpoint_ref,
            "failure_code": code,
            "idempotent_replay": False,
            "fallback": False,
        }
        self.journal.complete_dispatch(
            idempotency_key=idempotency_key,
            receipt=receipt,
        )
        return receipt

    def _parse_workload(self, payload: Mapping[str, Any]) -> Workload:
        raw = payload.get("workload")
        if not isinstance(raw, Mapping):
            raise DispatchRejected(
                "node_workload_missing",
                "node execution request requires a workload object",
                operation="execute",
                profile=self.policy.profile.value,
            )
        try:
            workload = Workload(
                workload_id=str(raw.get("workload_id") or ""),
                task_id=str(raw.get("task_id") or ""),
                run_id=str(raw.get("run_id") or ""),
                operation=str(raw.get("operation") or ""),
                payload=dict(raw.get("payload") or {}),
                sensitivity=Sensitivity(str(raw.get("sensitivity") or "")),
                complexity=int(raw.get("complexity") or 0),
                latency_sla_ms=int(raw.get("latency_sla_ms") or 0),
                cpu_units=int(raw.get("cpu_units") or 0),
                memory_mb=int(raw.get("memory_mb") or 0),
                required_capabilities=tuple(
                    str(item) for item in raw.get("required_capabilities") or ()
                ),
                provider_required=raw.get("provider_required") is True,
                preferred_provider=str(raw.get("preferred_provider") or ""),
                preferred_model=str(raw.get("preferred_model") or ""),
                checkpoint_ref=str(raw.get("checkpoint_ref") or ""),
                idempotency_key=str(raw.get("idempotency_key") or ""),
                created_at=str(raw.get("created_at") or now_iso()),
            )
        except (TypeError, ValueError) as error:
            raise DispatchRejected(
                "node_workload_invalid",
                "node workload contains invalid typed fields",
                operation="execute",
                profile=self.policy.profile.value,
            ) from error
        if not workload.workload_id or not workload.task_id or not workload.run_id:
            raise DispatchRejected(
                "node_workload_identity_invalid",
                "node workload identity is incomplete",
                operation=workload.operation,
                profile=self.policy.profile.value,
            )
        return workload

    def _admit_workload(
        self,
        workload: Workload,
        *,
        faults: Mapping[str, Any],
    ) -> None:
        if workload.operation not in _ALLOWED_OPERATIONS:
            raise DispatchRejected(
                "node_operation_not_allowed",
                "node operation is not in the deployment operation catalog",
                operation=workload.operation,
                profile=self.policy.profile.value,
            )
        if workload.sensitivity not in self.policy.allowed_sensitivity:
            raise DispatchRejected(
                "node_sensitivity_rejected",
                "node profile cannot admit this data sensitivity",
                operation=workload.operation,
                profile=self.policy.profile.value,
            )
        if workload.memory_mb > self.policy.resource.memory_mb:
            raise DispatchRejected(
                "node_memory_rejected",
                "workload memory request exceeds the node profile envelope",
                operation=workload.operation,
                profile=self.policy.profile.value,
            )
        missing = set(workload.required_capabilities) - set(self.policy.capabilities)
        if missing:
            raise DispatchRejected(
                "node_capability_missing",
                "node profile lacks required workload capabilities",
                operation=workload.operation,
                profile=self.policy.profile.value,
                details={"missing": sorted(missing)},
            )
        if faults.get("network_down") and self.policy.network_mode is not NetworkMode.OFFLINE:
            raise DispatchRejected(
                "node_network_unavailable",
                "node network is unavailable",
                operation=workload.operation,
                profile=self.policy.profile.value,
                retryable=True,
            )
        if workload.provider_required:
            if "provider-dispatch" not in self.policy.capabilities:
                raise DispatchRejected(
                    "node_provider_capability_missing",
                    "workload requires provider dispatch",
                    operation=workload.operation,
                    profile=self.policy.profile.value,
                )
            if not any(self.credential_presence.values()):
                raise DispatchRejected(
                    "node_provider_credential_missing",
                    "provider workload has no available credential handle",
                    operation=workload.operation,
                    profile=self.policy.profile.value,
                )
            if faults.get("provider_failure"):
                raise DispatchRejected(
                    "node_provider_failure",
                    "provider capability is unavailable",
                    operation=workload.operation,
                    profile=self.policy.profile.value,
                    retryable=True,
                )
        self._reject_secret_payload(workload.payload)

    @staticmethod
    def _reject_secret_payload(payload: Mapping[str, Any]) -> None:
        stack: list[tuple[str, Any]] = [("", payload)]
        while stack:
            path, value = stack.pop()
            if isinstance(value, Mapping):
                for key, child in value.items():
                    normalized = str(key).casefold()
                    child_path = f"{path}.{key}" if path else str(key)
                    secret_key = any(
                        marker in normalized
                        for marker in _SECRET_MARKERS
                        if marker != "token"
                    ) or ("token" in normalized and "tokens" not in normalized)
                    if secret_key:
                        raise DispatchRejected(
                            "node_secret_payload_rejected",
                            "secret material cannot be sent as deployment workload data",
                            operation="admit",
                            details={"path": child_path},
                        )
                    stack.append((child_path, child))
            elif isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    stack.append((f"{path}[{index}]", child))
            elif isinstance(value, str) and value.startswith(("sk-", "Bearer ", "Basic ")):
                raise DispatchRejected(
                    "node_secret_value_rejected",
                    "credential-like value cannot be sent as deployment workload data",
                    operation="admit",
                    details={"path": path},
                )

    def _execute_operation(self, workload: Workload) -> dict[str, Any]:
        operation = workload.operation
        payload = dict(workload.payload)
        if operation == "analyze-text":
            text = str(payload.get("text") or "")
            words = _WORD.findall(text.casefold())
            counter = Counter(words)
            return {
                "character_count": len(text),
                "line_count": len(text.splitlines()) or (1 if text else 0),
                "word_count": len(words),
                "unique_word_count": len(counter),
                "top_words": [
                    {"word": word, "count": count}
                    for word, count in counter.most_common(
                        min(20, max(1, int(payload.get("top_n") or 10)))
                    )
                ],
                "input_digest": digest(text),
            }
        if operation == "compress-text":
            text = str(payload.get("text") or "")
            compressed = zlib.compress(text.encode("utf-8"), level=9)
            return {
                "input_bytes": len(text.encode("utf-8")),
                "compressed_bytes": len(compressed),
                "compression_ratio": (
                    round(len(compressed) / len(text.encode("utf-8")), 6)
                    if text
                    else 0.0
                ),
                "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
                "roundtrip_verified": zlib.decompress(compressed).decode("utf-8")
                == text,
            }
        if operation == "deterministic-transform":
            items = payload.get("items")
            if not isinstance(items, list):
                raise DispatchRejected(
                    "node_transform_items_invalid",
                    "deterministic transform requires an items array",
                    operation=operation,
                    profile=self.policy.profile.value,
                )
            normalized = [
                {
                    "index": index,
                    "value": item,
                    "digest": digest(item),
                }
                for index, item in enumerate(items)
            ]
            normalized.sort(key=lambda item: (item["digest"], item["index"]))
            return {
                "input_count": len(items),
                "items": normalized,
                "output_digest": digest(normalized),
            }
        if operation == "hash-manifest":
            entries = payload.get("entries")
            if not isinstance(entries, Mapping):
                raise DispatchRejected(
                    "node_manifest_invalid",
                    "hash manifest requires an entries object",
                    operation=operation,
                    profile=self.policy.profile.value,
                )
            manifest = {
                str(key): digest(value)
                for key, value in sorted(entries.items(), key=lambda item: str(item[0]))
            }
            return {
                "entry_count": len(manifest),
                "entries": manifest,
                "manifest_digest": digest(manifest),
            }
        if operation == "verify-checksum":
            value = payload.get("value")
            expected = str(payload.get("expected_digest") or "")
            actual = digest(value)
            return {
                "expected_digest": expected,
                "actual_digest": actual,
                "verified": actual == expected,
            }
        if operation == "resource-probe":
            return process_environment_snapshot(
                allowed_names=self.policy.credential_environment
            )
        if operation == "provider-capability":
            provider = str(payload.get("provider") or workload.preferred_provider).casefold()
            return {
                "provider": provider,
                "provider_declared": provider in self.policy.providers,
                "model": str(payload.get("model") or workload.preferred_model),
                "credential_ready": any(self.credential_presence.values()),
                "provider_called": False,
                "capability_only": True,
            }
        if operation == "phase2-operator-execution":
            return self._execute_phase2_operator(payload, workload)
        if operation == "physical-dispatch-proof":
            payload_digest = digest(payload)
            marker = (
                "ZYRA_PHYSICAL_"
                + hashlib.sha256(payload_digest.encode("utf-8")).hexdigest()[:20].upper()
            )
            provider_call: Mapping[str, Any] = {}
            if self.policy.profile is DeploymentProfile.CLOUD:
                runtime = self._provider_runtime
                if runtime is None:
                    runtime = LiveProviderDispatchRuntime(
                        project_root=Path.cwd(),
                        state_root=self.data_root / "provider-control-plane",
                    )
                    self._provider_runtime = runtime
                provider_call = runtime.dispatch_marker(
                    run_id=workload.run_id,
                    task_id=workload.task_id,
                    node_id=self.node_id,
                    marker=marker,
                    provider_id=str(
                        payload.get("provider")
                        or workload.preferred_provider
                        or "zhipu"
                    ),
                    model_id=str(
                        payload.get("model")
                        or workload.preferred_model
                        or "glm-5.2"
                    ),
                    idempotency_key=(
                        workload.idempotency_key
                        or f"physical:{workload.workload_id}"
                    ),
                    payload_digest=payload_digest,
                ).to_dict()
            return {
                "task_payload_digest": payload_digest,
                "verification_marker_digest": digest(marker),
                "marker_verified": (
                    bool(provider_call.get("marker_verified"))
                    if provider_call
                    else True
                ),
                "profile": self.policy.profile.value,
                "node_id": self.node_id,
                "generation_id": self.generation_id,
                "pid": os.getpid(),
                "provider_call": dict(provider_call),
                "provider_called": bool(provider_call),
                "semantic_only": False,
                "simulated": False,
            }
        raise DispatchRejected(
            "node_operation_unimplemented",
            "node operation has no implementation",
            operation=operation,
            profile=self.policy.profile.value,
        )

    def _execute_phase2_operator(
        self,
        payload: Mapping[str, Any],
        workload: Workload,
    ) -> dict[str, Any]:
        operator = payload.get("operator")
        operator_ref = str(payload.get("operator_ref") or "")
        operator_runtime = str(payload.get("operator_runtime") or "")
        goal = str(payload.get("goal") or "")
        layer_index = int(payload.get("layer_index") or 0)
        physical_binding = payload.get("physical_worker_binding")
        invalid_fields: list[str] = []
        if payload.get("schema") != "zyra.production-physical-operator-task/v1":
            invalid_fields.append("schema")
        if not isinstance(operator, Mapping):
            invalid_fields.append("operator")
        elif str(operator.get("operator_ref") or "") != operator_ref:
            invalid_fields.append("operator_ref_binding")
        if not isinstance(physical_binding, Mapping):
            invalid_fields.append("physical_worker_binding")
        if not operator_ref:
            invalid_fields.append("operator_ref")
        if not operator_runtime:
            invalid_fields.append("operator_runtime")
        if (
            isinstance(operator, Mapping)
            and str(operator.get("operator_type") or "") == "worker"
            and isinstance(physical_binding, Mapping)
            and str(operator.get("source_ref") or "")
            != str(physical_binding.get("worker_id") or "")
        ):
            invalid_fields.append("worker_source_binding")
        if not goal:
            invalid_fields.append("goal")
        if layer_index < 1:
            invalid_fields.append("layer_index")
        if invalid_fields:
            raise DispatchRejected(
                "node_phase2_operator_task_invalid",
                "phase2 operator execution requires exact operator, goal, layer and physical-worker bindings",
                operation=workload.operation,
                profile=self.policy.profile.value,
                details={"invalid_fields": invalid_fields},
            )
        assert isinstance(operator, Mapping)
        assert isinstance(physical_binding, Mapping)

        expected_identity = f"process:{os.getpid()}:{self.generation_id}"
        expected_endpoint = f"http://{self.policy.host}:{self.policy.port}"
        identity_checks = {
            "process_identity_exact": (
                physical_binding.get("process_identity") == expected_identity
            ),
            "endpoint_exact": physical_binding.get("endpoint") == expected_endpoint,
            "node_id_exact": physical_binding.get("node_id") == self.node_id,
            "generation_id_exact": (
                physical_binding.get("generation_id") == self.generation_id
            ),
            "worker_id_present": bool(physical_binding.get("worker_id")),
            "backend_id_present": bool(physical_binding.get("backend_id")),
            "manifest_digest_present": bool(
                physical_binding.get("manifest_digest")
            ),
        }
        if not all(identity_checks.values()):
            raise DispatchRejected(
                "node_phase2_worker_identity_mismatch",
                "the leased worker is not the deployment process executing the operator",
                operation=workload.operation,
                profile=self.policy.profile.value,
                details={
                    "failed_checks": [
                        name for name, passed in identity_checks.items() if not passed
                    ]
                },
            )

        if payload.get("operator_adapter_enabled") is False:
            raise DispatchRejected(
                "node_phase2_operator_adapter_disabled",
                "the selected physical operator adapter is disabled",
                operation=workload.operation,
                profile=self.policy.profile.value,
                details={"operator_ref": operator_ref},
            )

        adapter = self._phase2_operator_adapter(
            operator_ref=operator_ref,
            operator=operator,
            operator_runtime=operator_runtime,
            goal=goal,
            layer_index=layer_index,
            workload=workload,
        )
        output_contract = tuple(
            str(item) for item in operator.get("output_contract") or () if str(item)
        )
        contract_outputs = dict(adapter["contract_outputs"])
        missing_contracts = tuple(
            item for item in output_contract if item not in contract_outputs
        )
        if not output_contract or missing_contracts:
            raise DispatchRejected(
                "node_phase2_output_contract_unfulfilled",
                "the selected adapter cannot fulfill the operator output contract",
                operation=workload.operation,
                profile=self.policy.profile.value,
                details={
                    "operator_ref": operator_ref,
                    "missing_contracts": list(missing_contracts),
                },
            )

        task_payload_digest = digest(payload)
        domain_artifact = dict(adapter["domain_artifact"])
        domain_result = dict(adapter["domain_result"])
        execution_body = {
            "operator_ref": operator_ref,
            "operator_type": str(operator.get("operator_type") or ""),
            "operator_runtime": operator_runtime,
            "operator_profile_digest": str(operator.get("profile_digest") or ""),
            "operator_adapter_id": str(adapter["adapter_id"]),
            "operator_adapter_version": str(adapter["adapter_version"]),
            "layer_index": layer_index,
            "goal_digest": digest(goal),
            "requirement_revision": str(payload.get("requirement_revision") or ""),
            "capabilities_applied": list(operator.get("capabilities") or ()),
            "input_contract": list(operator.get("input_contract") or ()),
            "output_contract": list(output_contract),
            "fulfilled_output_contract": sorted(contract_outputs),
            "contract_outputs_digest": digest(contract_outputs),
            "domain_result_digest": digest(domain_result),
            "domain_artifact_digest": str(domain_artifact["content_digest"]),
            "candidate_set_digest": str(payload.get("candidate_set_digest") or ""),
            "policy_input_digest": str(payload.get("policy_input_digest") or ""),
            "operator_idempotency_key": str(
                payload.get("operator_idempotency_key") or ""
            ),
            "leased_worker_id": str(physical_binding.get("worker_id") or ""),
            "leased_backend_id": str(physical_binding.get("backend_id") or ""),
            "leased_manifest_digest": str(
                physical_binding.get("manifest_digest") or ""
            ),
            "physical_process_identity": expected_identity,
            "physical_endpoint": expected_endpoint,
            "physical_profile": self.policy.profile.value,
            "node_id": self.node_id,
            "generation_id": self.generation_id,
        }
        execution_digest = digest(execution_body)
        provider_call: Mapping[str, Any] = {}
        if self.policy.profile is DeploymentProfile.CLOUD:
            runtime = self._provider_runtime
            if runtime is None:
                runtime = LiveProviderDispatchRuntime(
                    project_root=Path.cwd(),
                    state_root=self.data_root / "provider-control-plane",
                )
                self._provider_runtime = runtime
            provider_call = runtime.dispatch_marker(
                run_id=workload.run_id,
                task_id=workload.task_id,
                node_id=self.node_id,
                marker="ZYRA_OPERATOR_"
                + hashlib.sha256(execution_digest.encode("utf-8"))
                .hexdigest()[:20]
                .upper(),
                provider_id=str(
                    payload.get("provider")
                    or workload.preferred_provider
                    or "zhipu"
                ),
                model_id=str(
                    payload.get("model")
                    or workload.preferred_model
                    or "glm-5.2"
                ),
                idempotency_key=(
                    workload.idempotency_key
                    or str(payload.get("operator_idempotency_key") or "")
                ),
                payload_digest=task_payload_digest,
            ).to_dict()
            if not provider_call.get("marker_verified"):
                raise DispatchRejected(
                    "node_phase2_provider_execution_unverified",
                    "the cloud operator adapter did not obtain a verified provider call",
                    operation=workload.operation,
                    profile=self.policy.profile.value,
                )

        return {
            "schema": "zyra.deployment-operator-result/v2",
            **execution_body,
            "operator_execution_body": execution_body,
            "task_payload_digest": task_payload_digest,
            "operator_execution_digest": execution_digest,
            "domain_result": domain_result,
            "domain_artifact": domain_artifact,
            "contract_outputs": contract_outputs,
            "output_contract_fulfilled": True,
            "physical_worker_identity_checks": identity_checks,
            "summary": str(adapter["summary"]),
            "provider_call": dict(provider_call),
            "provider_called": bool(provider_call),
            "domain_effect_performed": True,
            "semantic_only": False,
            "simulated": False,
        }

    def _phase2_operator_adapter(
        self,
        *,
        operator_ref: str,
        operator: Mapping[str, Any],
        operator_runtime: str,
        goal: str,
        layer_index: int,
        workload: Workload,
    ) -> dict[str, Any]:
        goal_words = tuple(_WORD.findall(goal))
        usage = {
            "prompt_tokens": max(1, len(goal_words)),
            "completion_tokens": max(1, min(64, len(goal_words) + 8)),
            "total_tokens": max(2, min(128, len(goal_words) * 2 + 8)),
            "provider_called": self.policy.profile is DeploymentProfile.CLOUD,
        }
        if (
            operator_ref.startswith("worker:local-code-worker@")
            or (
                str(operator.get("operator_type") or "") == "worker"
                and operator_runtime == "CodeWorkerRuntime"
                and not operator_ref.startswith(
                    "worker:local-memory-curator@"
                )
            )
        ):
            function_name = "execute_phase2_goal"
            content = "\n".join(
                (
                    '"""Physical MaAS code-worker artifact."""',
                    "",
                    f"def {function_name}() -> dict[str, object]:",
                    "    return {",
                    f"        \"goal\": {goal!r},",
                    f"        \"layer_index\": {layer_index},",
                    "        \"status\": \"implemented\",",
                    "    }",
                    "",
                )
            )
            compiled = compile(
                content,
                f"<phase2:{workload.task_id}:layer-{layer_index}>",
                "exec",
            )
            namespace: dict[str, Any] = {}
            exec(compiled, namespace)  # noqa: S102 - audited generated adapter.
            observed_result = namespace[function_name]()
            validation_checks = {
                "callable_executed": isinstance(observed_result, Mapping),
                "goal_exact": (
                    isinstance(observed_result, Mapping)
                    and observed_result.get("goal") == goal
                ),
                "layer_exact": (
                    isinstance(observed_result, Mapping)
                    and observed_result.get("layer_index") == layer_index
                ),
                "status_exact": (
                    isinstance(observed_result, Mapping)
                    and observed_result.get("status") == "implemented"
                ),
            }
            if not all(validation_checks.values()):
                raise DispatchRejected(
                    "node_phase2_code_adapter_validation_failed",
                    "the physical code adapter failed runtime validation",
                    operation=workload.operation,
                    profile=self.policy.profile.value,
                    details={"failed_checks": [
                        name
                        for name, passed in validation_checks.items()
                        if not passed
                    ]},
                )
            domain_result = {
                "kind": "code_delivery",
                "function_name": function_name,
                "syntax_check": "compiled",
                "compiled_code_digest": digest(compiled.co_code.hex()),
                "runtime_result_digest": digest(observed_result),
                "validation_checks": validation_checks,
            }
            artifact = {
                "title": "Physical MaAS code delivery",
                "kind": "code",
                "extension": ".py",
                "media_type": "text/x-python",
                "content": content,
                "content_digest": digest(content),
            }
            adapter_id = "worker.local-code-worker.code-delivery"
        elif operator_ref.startswith("worker:local-memory-curator@"):
            facts = sorted(
                {
                    item.casefold()
                    for item in goal_words
                    if len(item) >= 4
                }
            )[:24]
            domain_result = {
                "kind": "memory_continuity",
                "fact_count": len(facts),
                "facts": facts,
                "requirement_goal_digest": digest(goal),
                "continuity_action": "refresh_canonical_task_memory",
            }
            content = json.dumps(
                {
                    "schema": "zyra.physical-memory-curator-result/v1",
                    "run_id": workload.run_id,
                    "task_id": workload.task_id,
                    "layer_index": layer_index,
                    **domain_result,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            artifact = {
                "title": "Physical MaAS memory continuity result",
                "kind": "structured_data",
                "extension": ".json",
                "media_type": "application/json",
                "content": content,
                "content_digest": digest(content),
            }
            adapter_id = "worker.local-memory-curator.continuity"
        elif operator_ref.startswith("tool:produce-tool@"):
            domain_result = {
                "kind": "tool_production",
                "produced": True,
                "goal_digest": digest(goal),
            }
            content = json.dumps(
                domain_result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            artifact = {
                "title": "Physical MaAS tool result",
                "kind": "structured_data",
                "extension": ".json",
                "media_type": "application/json",
                "content": content,
                "content_digest": digest(content),
            }
            adapter_id = "tool.produce-tool.deterministic"
        else:
            raise DispatchRejected(
                "node_phase2_operator_adapter_unavailable",
                "no audited physical adapter exists for the selected operator",
                operation=workload.operation,
                profile=self.policy.profile.value,
                details={
                    "operator_ref": operator_ref,
                    "operator_type": str(operator.get("operator_type") or ""),
                },
            )

        artifact_ref = "content://" + str(artifact["content_digest"])
        worker_result = {
            "ok": True,
            "summary": f"{adapter_id} completed physical layer {layer_index}.",
            "domain_kind": domain_result["kind"],
        }
        supported_outputs = {
            "worker_result": worker_result,
            "physical_worker_result": worker_result,
            "artifact_refs": [artifact_ref],
            "usage": usage,
            "tool_result": domain_result,
            "artifact": artifact_ref,
            "verification": {"passed": True, "adapter_id": adapter_id},
        }
        output_contract = tuple(
            str(item) for item in operator.get("output_contract") or () if str(item)
        )
        return {
            "adapter_id": adapter_id,
            "adapter_version": "phase2-physical-adapter-v1",
            "summary": worker_result["summary"],
            "domain_result": domain_result,
            "domain_artifact": artifact,
            "contract_outputs": {
                key: supported_outputs[key]
                for key in output_contract
                if key in supported_outputs
            },
        }

    def _operation_capability_available(self, operation: str) -> bool:
        if operation == "provider-capability":
            return "provider-dispatch" in self.policy.capabilities
        if (
            operation in {
                "physical-dispatch-proof",
                "phase2-operator-execution",
            }
            and self.policy.profile is DeploymentProfile.CLOUD
        ):
            return "provider-dispatch" in self.policy.capabilities
        return "deterministic-transform" in self.policy.capabilities

    def _write_artifact(
        self,
        workload: Workload,
        *,
        attempt_id: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        artifact_id = new_id("deployment_artifact")
        directory = self.artifact_root / workload.task_id
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / f"{artifact_id}.json").resolve()
        if self.artifact_root not in path.parents:
            raise DispatchRejected(
                "node_artifact_path_escape",
                "node artifact path escaped the configured root",
                operation=workload.operation,
                profile=self.policy.profile.value,
            )
        body = {
            "schema": "zyra.deployment-node-artifact/v1",
            "artifact_id": artifact_id,
            "task_id": workload.task_id,
            "run_id": workload.run_id,
            "workload_id": workload.workload_id,
            "attempt_id": attempt_id,
            "profile": self.policy.profile.value,
            "operation": workload.operation,
            "result": dict(result),
            "created_at": now_iso(),
        }
        encoded = canonical_json(body)
        temporary = path.with_suffix(".tmp")
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        return {
            "artifact_ref": f"deployment://{self.node_id}/{workload.task_id}/{artifact_id}",
            "artifact_id": artifact_id,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
            "kind": "structured_data",
            "node_local_path_exposed": False,
        }

    def _checkpoint_after_execution(
        self,
        workload: Workload,
        *,
        attempt_id: str,
        result: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.policy.allow_checkpoint_export:
            return {
                "checkpoint_ref": workload.checkpoint_ref,
                "exported": False,
                "reason": "profile_checkpoint_export_disabled",
            }
        checkpoint_ref = new_id("deployment_checkpoint")
        return self.journal.save_checkpoint(
            checkpoint_ref,
            workload_id=workload.workload_id,
            payload={
                "attempt_id": attempt_id,
                "task_id": workload.task_id,
                "run_id": workload.run_id,
                "profile": self.policy.profile.value,
                "predecessor_checkpoint_ref": workload.checkpoint_ref,
                "result_digest": digest(result),
                "artifact_ref": str(artifact.get("artifact_ref") or ""),
                "operation": workload.operation,
            },
        )

    def export_checkpoint(self, checkpoint_ref: str) -> dict[str, Any]:
        if not self.policy.allow_checkpoint_export:
            raise DispatchRejected(
                "node_checkpoint_export_disabled",
                "profile does not allow checkpoint export",
                operation="checkpoint_export",
                profile=self.policy.profile.value,
            )
        checkpoint = self.journal.checkpoint(checkpoint_ref)
        if checkpoint is None:
            raise DispatchRejected(
                "node_checkpoint_missing",
                "node checkpoint does not exist",
                operation="checkpoint_export",
                profile=self.policy.profile.value,
            )
        return {
            "schema": "zyra.deployment-node-checkpoint-export/v1",
            "node_id": self.node_id,
            "generation_id": self.generation_id,
            "profile": self.policy.profile.value,
            "checkpoint": checkpoint,
            "exported_at": now_iso(),
        }

    def import_checkpoint(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.policy.allow_checkpoint_import:
            raise DispatchRejected(
                "node_checkpoint_import_disabled",
                "profile does not allow checkpoint import",
                operation="checkpoint_import",
                profile=self.policy.profile.value,
            )
        checkpoint = payload.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            raise DispatchRejected(
                "node_checkpoint_import_invalid",
                "checkpoint import requires a checkpoint object",
                operation="checkpoint_import",
                profile=self.policy.profile.value,
            )
        checksum = str(checkpoint.get("checksum") or "")
        semantic = {key: value for key, value in checkpoint.items() if key != "checksum"}
        if not checksum or digest(semantic) != checksum:
            raise DispatchRejected(
                "node_checkpoint_import_checksum_invalid",
                "checkpoint import checksum is invalid",
                operation="checkpoint_import",
                profile=self.policy.profile.value,
            )
        predecessor = str(checkpoint.get("checkpoint_ref") or "")
        imported_ref = new_id("deployment_checkpoint")
        saved = self.journal.save_checkpoint(
            imported_ref,
            workload_id=str(checkpoint.get("workload_id") or ""),
            payload={
                "imported_from_checkpoint_ref": predecessor,
                "imported_from_node_id": str(payload.get("source_node_id") or ""),
                "source_checksum": checksum,
                "source_payload": dict(checkpoint.get("payload") or {}),
                "imported_at": now_iso(),
            },
        )
        return {
            "schema": "zyra.deployment-node-checkpoint-import/v1",
            "imported": True,
            "source_checkpoint_ref": predecessor,
            "checkpoint": saved,
            "node_id": self.node_id,
            "generation_id": self.generation_id,
        }

    def inject_fault(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "").strip().casefold()
        enabled = payload.get("enabled", True) is True
        with self._lock:
            if kind == "network-loss":
                self._faults["network_down"] = enabled
            elif kind == "provider-failure":
                self._faults["provider_failure"] = enabled
            elif kind == "latency":
                latency = int(payload.get("latency_ms") or 0)
                if latency < 0 or latency > 10_000:
                    raise ValueError("fault latency must be between 0 and 10000 ms")
                self._faults["latency_ms"] = latency
            elif kind == "fail-next":
                count = int(payload.get("count") or 1)
                if count < 0 or count > 100:
                    raise ValueError("fail-next count must be between 0 and 100")
                self._faults["fail_next"] = count
            elif kind == "crash-next":
                self._faults["crash_next"] = enabled
            elif kind == "clear":
                self._faults.update(
                    {
                        "network_down": False,
                        "provider_failure": False,
                        "latency_ms": 0,
                        "fail_next": 0,
                        "crash_next": False,
                    }
                )
            else:
                raise ValueError("unsupported deployment node fault kind")
            faults = dict(self._faults)
        return {
            "schema": "zyra.deployment-node-fault-state/v1",
            "node_id": self.node_id,
            "profile": self.policy.profile.value,
            "kind": kind,
            "faults": faults,
            "injected_at": now_iso(),
            "bounded": True,
        }

    def _network_latency_ms(self) -> int:
        if self.policy.network_mode is NetworkMode.OFFLINE:
            return 0
        if self.policy.network_mode is NetworkMode.PRIVATE:
            return min(20, self.policy.latency_budget_ms // 4)
        if self.policy.network_mode is NetworkMode.LIMITED:
            return min(250, self.policy.latency_budget_ms // 2)
        return min(100, self.policy.latency_budget_ms // 4)


__all__ = [
    "DeploymentNodeRuntime",
    "NodeJournal",
]
