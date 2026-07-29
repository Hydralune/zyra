from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .contracts import (
    BRIDGE_COMMAND_SCHEMA,
    BridgeCommand,
    OutboxConflictError,
    OutboxLeaseError,
    OutboxRecord,
    OutboxState,
)
from .single_writer import WriterFence, workspace_identity


OUTBOX_SCHEMA = "zyra.loopx-outbox/v1"
OUTBOX_DB_VERSION = 1


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LoopXOutbox:
    """Workspace-local durable outbox owned by the Zyra LoopX bridge."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        state_root: Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        expected_state = (self.workspace_root / ".zyra" / "loopx" / "state").resolve()
        self.state_root = (state_root or expected_state).resolve()
        if self.state_root != expected_state:
            raise OutboxConflictError(
                "LoopX outbox state root violates the workspace-local contract.",
                code="loopx_workspace_state_root_mismatch",
                details={
                    "expected": str(expected_state),
                    "actual": str(self.state_root),
                },
            )
        self.workspace_id = workspace_identity(self.workspace_root)
        self.path = self.state_root / "bridge" / "outbox.sqlite3"
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._initialize()

    def enqueue_after_commit(
        self,
        *,
        run_id: str,
        task_id: str,
        canonical_commit: Any,
        update: Mapping[str, Any] | Any,
        created_at: str | None = None,
        causation_id: str = "",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> OutboxRecord:
        command = BridgeCommand.create(
            workspace_id=self.workspace_id,
            run_id=run_id,
            task_id=task_id,
            canonical_commit=canonical_commit,
            update=update,
            created_at=created_at or "",
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return self.enqueue(command)

    def enqueue(self, command: BridgeCommand) -> OutboxRecord:
        if command.workspace_id != self.workspace_id:
            raise OutboxConflictError(
                "Bridge command belongs to another workspace.",
                code="loopx_workspace_mismatch",
                details={
                    "expected": self.workspace_id,
                    "actual": command.workspace_id,
                },
            )
        payload = command.to_dict()
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._transaction() as connection:
            prior = connection.execute(
                """
                SELECT * FROM loopx_outbox
                WHERE workspace_id = ? AND idempotency_key = ?
                """,
                (self.workspace_id, command.idempotency_key),
            ).fetchone()
            if prior is not None:
                if str(prior["content_digest"]) != command.content_digest:
                    raise OutboxConflictError(
                        "LoopX idempotency key was reused for different content.",
                        code="loopx_outbox_idempotency_conflict",
                        details={
                            "idempotency_key": command.idempotency_key,
                            "existing_digest": prior["content_digest"],
                            "incoming_digest": command.content_digest,
                        },
                    )
                return self._row(prior)
            cursor = connection.execute(
                """
                INSERT INTO loopx_outbox (
                    workspace_id,
                    schema,
                    mapping_version,
                    idempotency_key,
                    content_digest,
                    causation_id,
                    correlation_id,
                    run_id,
                    task_id,
                    commit_id,
                    command_json,
                    state,
                    attempts,
                    available_at,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    self.workspace_id,
                    BRIDGE_COMMAND_SCHEMA,
                    command.mapping_version,
                    command.idempotency_key,
                    command.content_digest,
                    command.causation_id,
                    command.correlation_id,
                    command.run_id,
                    command.task_id,
                    command.canonical_commit.commit_id,
                    payload_json,
                    OutboxState.PENDING.value,
                    command.created_at,
                    command.created_at,
                ),
            )
            sequence = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM loopx_outbox WHERE sequence = ?",
                (sequence,),
            ).fetchone()
            if row is None:
                raise RuntimeError("LoopX outbox insert disappeared")
            return self._row(row)

    def recover_inflight(self, fence: WriterFence) -> int:
        self._assert_workspace_fence(fence)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE loopx_outbox
                SET state = ?,
                    available_at = 0,
                    lease_token = '',
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE workspace_id = ? AND state = ?
                """,
                (
                    OutboxState.PENDING.value,
                    _now_iso(),
                    self.workspace_id,
                    OutboxState.INFLIGHT.value,
                ),
            )
            recovered = int(cursor.rowcount)
        self._assert_workspace_fence(fence)
        return recovered

    def claim_next(
        self,
        fence: WriterFence,
        *,
        lease_seconds: float = 30.0,
        now_epoch: float | None = None,
    ) -> OutboxRecord | None:
        self._assert_workspace_fence(fence)
        now = time.time() if now_epoch is None else float(now_epoch)
        lease_token = f"outbox-lease:{fence.fencing_epoch}:{uuid4().hex}"
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM loopx_outbox
                WHERE workspace_id = ?
                  AND (
                    (state = ? AND available_at <= ?)
                    OR (state = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                  )
                ORDER BY sequence
                LIMIT 1
                """,
                (
                    self.workspace_id,
                    OutboxState.PENDING.value,
                    now,
                    OutboxState.INFLIGHT.value,
                    now,
                ),
            ).fetchone()
            if row is None:
                return None
            sequence = int(row["sequence"])
            connection.execute(
                """
                UPDATE loopx_outbox
                SET state = ?,
                    attempts = attempts + 1,
                    lease_token = ?,
                    lease_expires_at = ?,
                    writer_epoch = ?,
                    writer_token = ?,
                    updated_at = ?
                WHERE sequence = ?
                """,
                (
                    OutboxState.INFLIGHT.value,
                    lease_token,
                    now + max(0.1, float(lease_seconds)),
                    fence.fencing_epoch,
                    fence.fencing_token,
                    _now_iso(),
                    sequence,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM loopx_outbox WHERE sequence = ?",
                (sequence,),
            ).fetchone()
            if claimed is None:
                raise RuntimeError("LoopX outbox claim disappeared")
            record = self._row(claimed)
        self._assert_workspace_fence(fence)
        return record

    def record_apply_receipt(
        self,
        record: OutboxRecord,
        fence: WriterFence,
        receipt: Mapping[str, Any],
    ) -> OutboxRecord:
        return self._update_leased(
            record,
            fence,
            apply_receipt_json=json.dumps(
                dict(receipt),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def acknowledge(
        self,
        record: OutboxRecord,
        fence: WriterFence,
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> OutboxRecord:
        values: dict[str, Any] = {
            "state": OutboxState.ACKED.value,
            "acked_at": _now_iso(),
            "lease_token": "",
            "lease_expires_at": None,
            "last_error_code": "",
            "last_error_message": "",
        }
        if receipt is not None:
            values["apply_receipt_json"] = json.dumps(
                dict(receipt),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return self._update_leased(record, fence, **values)

    def retry(
        self,
        record: OutboxRecord,
        fence: WriterFence,
        *,
        error_code: str,
        error_message: str,
        delay_seconds: float,
    ) -> OutboxRecord:
        return self._update_leased(
            record,
            fence,
            state=OutboxState.PENDING.value,
            available_at=time.time() + max(0.0, float(delay_seconds)),
            lease_token="",
            lease_expires_at=None,
            last_error_code=error_code,
            last_error_message=error_message[:2000],
        )

    def dead_letter(
        self,
        record: OutboxRecord,
        fence: WriterFence,
        *,
        error_code: str,
        error_message: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> OutboxRecord:
        values: dict[str, Any] = {
            "state": OutboxState.DEAD_LETTER.value,
            "dead_lettered_at": _now_iso(),
            "lease_token": "",
            "lease_expires_at": None,
            "last_error_code": error_code,
            "last_error_message": error_message[:2000],
        }
        if receipt is not None:
            values["apply_receipt_json"] = json.dumps(
                dict(receipt),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return self._update_leased(record, fence, **values)

    def replay_dead_letter(
        self,
        sequence: int,
        fence: WriterFence,
    ) -> OutboxRecord:
        self._assert_workspace_fence(fence)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM loopx_outbox WHERE sequence = ? AND workspace_id = ?",
                (int(sequence), self.workspace_id),
            ).fetchone()
            if row is None:
                raise KeyError(sequence)
            if row["state"] != OutboxState.DEAD_LETTER.value:
                raise OutboxConflictError(
                    "Only dead-letter records can be replayed.",
                    code="loopx_outbox_replay_invalid",
                    details={"sequence": sequence, "state": row["state"]},
                )
            connection.execute(
                """
                UPDATE loopx_outbox
                SET state = ?,
                    available_at = 0,
                    lease_token = '',
                    lease_expires_at = NULL,
                    last_error_code = '',
                    last_error_message = '',
                    dead_lettered_at = NULL,
                    updated_at = ?
                WHERE sequence = ?
                """,
                (OutboxState.PENDING.value, _now_iso(), int(sequence)),
            )
            updated = connection.execute(
                "SELECT * FROM loopx_outbox WHERE sequence = ?",
                (int(sequence),),
            ).fetchone()
            if updated is None:
                raise RuntimeError("LoopX replay record disappeared")
            result = self._row(updated)
        self._assert_workspace_fence(fence)
        return result

    def get(self, sequence: int) -> OutboxRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM loopx_outbox WHERE sequence = ? AND workspace_id = ?",
                (int(sequence), self.workspace_id),
            ).fetchone()
        return None if row is None else self._row(row)

    def get_by_idempotency_key(self, key: str) -> OutboxRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM loopx_outbox
                WHERE workspace_id = ? AND idempotency_key = ?
                """,
                (self.workspace_id, key),
            ).fetchone()
        return None if row is None else self._row(row)

    def list_records(
        self,
        *,
        state: OutboxState | None = None,
        limit: int = 1000,
    ) -> tuple[OutboxRecord, ...]:
        parameters: tuple[Any, ...]
        query = "SELECT * FROM loopx_outbox WHERE workspace_id = ?"
        parameters = (self.workspace_id,)
        if state is not None:
            query += " AND state = ?"
            parameters += (state.value,)
        query += " ORDER BY sequence LIMIT ?"
        parameters += (max(1, min(10000, int(limit))),)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._row(row) for row in rows)

    def checkpoint(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state, COUNT(*) AS count, MAX(sequence) AS max_sequence
                FROM loopx_outbox
                WHERE workspace_id = ?
                GROUP BY state
                """,
                (self.workspace_id,),
            ).fetchall()
            maximum = connection.execute(
                "SELECT MAX(sequence) AS value FROM loopx_outbox WHERE workspace_id = ?",
                (self.workspace_id,),
            ).fetchone()
        counts = {state.value: 0 for state in OutboxState}
        for row in rows:
            counts[str(row["state"])] = int(row["count"])
        return {
            "schema": OUTBOX_SCHEMA,
            "workspace_id": self.workspace_id,
            "database": str(self.path),
            "database_version": OUTBOX_DB_VERSION,
            "max_sequence": int(maximum["value"] or 0) if maximum else 0,
            "counts": counts,
            "captured_at": _now_iso(),
        }

    def _update_leased(
        self,
        record: OutboxRecord,
        fence: WriterFence,
        **values: Any,
    ) -> OutboxRecord:
        self._assert_workspace_fence(fence)
        assignments = {**values, "updated_at": _now_iso()}
        columns = ", ".join(f"{key} = ?" for key in assignments)
        parameters = tuple(assignments.values()) + (
            record.sequence,
            self.workspace_id,
            OutboxState.INFLIGHT.value,
            record.lease_token,
            fence.fencing_epoch,
            fence.fencing_token,
        )
        with self._transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE loopx_outbox
                SET {columns}
                WHERE sequence = ?
                  AND workspace_id = ?
                  AND state = ?
                  AND lease_token = ?
                  AND writer_epoch = ?
                  AND writer_token = ?
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                raise OutboxLeaseError(
                    "LoopX outbox lease or writer fence no longer matches.",
                    code="loopx_outbox_lease_lost",
                    details={
                        "sequence": record.sequence,
                        "writer_epoch": fence.fencing_epoch,
                    },
                )
            row = connection.execute(
                "SELECT * FROM loopx_outbox WHERE sequence = ?",
                (record.sequence,),
            ).fetchone()
            if row is None:
                raise RuntimeError("LoopX outbox update disappeared")
            updated = self._row(row)
        self._assert_workspace_fence(fence)
        return updated

    def _assert_workspace_fence(self, fence: WriterFence) -> None:
        fence.assert_owned()
        if fence.owner.workspace_id != self.workspace_id:
            raise OutboxLeaseError(
                "LoopX outbox rejected a writer fence from another workspace.",
                code="loopx_writer_workspace_mismatch",
                details={
                    "outbox_workspace_id": self.workspace_id,
                    "fence_workspace_id": fence.owner.workspace_id,
                },
            )

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS loopx_outbox_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS loopx_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    schema TEXT NOT NULL,
                    mapping_version TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    causation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL,
                    writer_epoch INTEGER NOT NULL DEFAULT 0,
                    writer_token TEXT NOT NULL DEFAULT '',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error_message TEXT NOT NULL DEFAULT '',
                    apply_receipt_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    acked_at TEXT,
                    dead_lettered_at TEXT,
                    UNIQUE(workspace_id, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS loopx_outbox_ready
                ON loopx_outbox(workspace_id, state, available_at, sequence)
                """
            )
            connection.execute(
                """
                INSERT INTO loopx_outbox_meta(key, value)
                VALUES ('schema', ?), ('version', ?), ('workspace_id', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (OUTBOX_SCHEMA, str(OUTBOX_DB_VERSION), self.workspace_id),
            )
            meta = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM loopx_outbox_meta"
                ).fetchall()
            }
            expected = {
                "schema": OUTBOX_SCHEMA,
                "version": str(OUTBOX_DB_VERSION),
                "workspace_id": self.workspace_id,
            }
            if any(meta.get(key) != value for key, value in expected.items()):
                raise OutboxConflictError(
                    "LoopX outbox metadata belongs to another schema or workspace.",
                    code="loopx_outbox_schema_mismatch",
                    details={"expected": expected, "actual": meta},
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _row(row: sqlite3.Row) -> OutboxRecord:
        command = BridgeCommand.from_dict(json.loads(str(row["command_json"])))
        if command.content_digest != str(row["content_digest"]):
            raise OutboxConflictError(
                "LoopX outbox command content digest does not match durable data.",
                code="loopx_outbox_content_tampered",
                details={"sequence": int(row["sequence"])},
            )
        raw_receipt = row["apply_receipt_json"]
        receipt = json.loads(str(raw_receipt)) if raw_receipt else None
        return OutboxRecord(
            sequence=int(row["sequence"]),
            command=command,
            state=OutboxState(str(row["state"])),
            attempts=int(row["attempts"]),
            lease_token=str(row["lease_token"] or ""),
            lease_expires_at=(
                float(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            ),
            available_at=float(row["available_at"]),
            last_error_code=str(row["last_error_code"] or ""),
            last_error_message=str(row["last_error_message"] or ""),
            apply_receipt=receipt,
            writer_epoch=int(row["writer_epoch"] or 0),
            writer_token=str(row["writer_token"] or ""),
        )


__all__ = [
    "OUTBOX_DB_VERSION",
    "OUTBOX_SCHEMA",
    "LoopXOutbox",
]
