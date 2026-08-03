from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, TaskState, task_state_from_json, to_jsonable

from .models import CompactResult, MemoryLayer, MemoryRecord


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    user_goal TEXT NOT NULL,
                    root_node_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    node_id TEXT,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_task_created
                    ON events(task_id, created_at);

                CREATE TABLE IF NOT EXISTS checkpoints (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_records (
                    memory_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    node_id TEXT,
                    summary TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    score REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_task_layer
                    ON memory_records(task_id, layer, score);

                CREATE TABLE IF NOT EXISTS compact_records (
                    compact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    focus TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_compact_task_created
                    ON compact_records(task_id, created_at);
                """
            )

    def append_event(self, event: EventRecord) -> None:
        self.initialize()
        payload = to_jsonable(event)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO events (
                    event_id, run_id, task_id, node_id, event_type, created_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.run_id,
                    event.task_id,
                    event.node_id,
                    str(event.event_type),
                    event.created_at,
                    json.dumps(payload["payload"], ensure_ascii=False, sort_keys=True),
                ),
            )

    def append_events(self, events: list[EventRecord]) -> None:
        for event in events:
            self.append_event(event)

    def save_checkpoint(self, state: TaskState) -> None:
        self.initialize()
        payload = to_jsonable(state)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, run_id, status, user_goal, root_node_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    user_goal = excluded.user_goal,
                    root_node_id = excluded.root_node_id,
                    updated_at = excluded.updated_at
                """,
                (
                    state.task_id,
                    state.run_id,
                    str(state.status),
                    state.user_goal,
                    state.root_node_id,
                    state.created_at,
                    state.updated_at,
                ),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO checkpoints (
                    task_id, run_id, updated_at, checkpoint_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    state.task_id,
                    state.run_id,
                    state.updated_at,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )

    def load_task(self, task_id: str) -> TaskState | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT checkpoint_json FROM checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return task_state_from_json(json.loads(str(row["checkpoint_json"])))

    def list_tasks(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    tasks.task_id,
                    tasks.run_id,
                    tasks.status,
                    tasks.user_goal,
                    tasks.root_node_id,
                    tasks.created_at,
                    tasks.updated_at,
                    checkpoints.checkpoint_json
                FROM tasks
                LEFT JOIN checkpoints ON checkpoints.task_id = tasks.task_id
                ORDER BY tasks.updated_at DESC
                """
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            checkpoint_json = value.pop("checkpoint_json", None)
            if checkpoint_json:
                checkpoint = json.loads(str(checkpoint_json))
                metadata = checkpoint.get("metadata")
                if isinstance(metadata, dict):
                    session_id = str(metadata.get("query_session_id") or "").strip()
                    if session_id:
                        value["session_id"] = session_id
            values.append(value)
        return values

    def task_events(self, task_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, task_id, node_id, event_type, created_at, payload_json
                FROM events
                WHERE task_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (task_id,),
            ).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def all_events(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, task_id, node_id, event_type, created_at, payload_json
                FROM events
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_event_row_to_dict(row) for row in reversed(rows)]

    def save_memory_records(self, records: list[MemoryRecord]) -> None:
        if not records:
            return
        self.initialize()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO memory_records (
                    memory_id, run_id, task_id, layer, source_type, source_id, node_id,
                    summary, content_json, keywords_json, artifact_ids_json, evidence_ids_json,
                    score, created_at, updated_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    layer = excluded.layer,
                    source_type = excluded.source_type,
                    source_id = excluded.source_id,
                    node_id = excluded.node_id,
                    summary = excluded.summary,
                    content_json = excluded.content_json,
                    keywords_json = excluded.keywords_json,
                    artifact_ids_json = excluded.artifact_ids_json,
                    evidence_ids_json = excluded.evidence_ids_json,
                    score = excluded.score,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                [
                    (
                        record.memory_id,
                        record.run_id,
                        record.task_id,
                        str(record.layer),
                        record.source_type,
                        record.source_id,
                        record.node_id,
                        record.summary,
                        json.dumps(to_jsonable(record.content), ensure_ascii=False, sort_keys=True),
                        json.dumps(record.keywords, ensure_ascii=False, sort_keys=True),
                        json.dumps(record.artifact_ids, ensure_ascii=False, sort_keys=True),
                        json.dumps(record.evidence_ids, ensure_ascii=False, sort_keys=True),
                        record.score,
                        record.created_at,
                        record.updated_at,
                        json.dumps(to_jsonable(record.metadata), ensure_ascii=False, sort_keys=True),
                    )
                    for record in records
                ],
            )

    def task_memory_records(self, task_id: str, layer: str | MemoryLayer | None = None) -> list[MemoryRecord]:
        self.initialize()
        params: list[Any] = [task_id]
        condition = "WHERE task_id = ?"
        if layer is not None:
            condition += " AND layer = ?"
            params.append(str(layer))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM memory_records
                {condition}
                ORDER BY layer ASC, score DESC, updated_at DESC, rowid ASC
                """,
                params,
            ).fetchall()
        return [_memory_row_to_record(row) for row in rows]

    def save_compact_result(self, result: CompactResult) -> None:
        self.initialize()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO compact_records (
                    compact_id, run_id, task_id, created_at, focus, summary, result_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(compact_id) DO UPDATE SET
                    focus = excluded.focus,
                    summary = excluded.summary,
                    result_json = excluded.result_json
                """,
                (
                    result.compact_id,
                    result.run_id,
                    result.task_id,
                    result.created_at,
                    result.focus,
                    result.summary,
                    json.dumps(to_jsonable(result), ensure_ascii=False, sort_keys=True),
                ),
            )

    def task_compactions(self, task_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT compact_id, run_id, task_id, created_at, focus, summary, result_json
                FROM compact_records
                WHERE task_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (task_id,),
            ).fetchall()
        return [_compact_row_to_dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "node_id": row["node_id"],
        "event_type": row["event_type"],
        "created_at": row["created_at"],
        "payload": json.loads(row["payload_json"]),
    }


def _memory_row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        memory_id=row["memory_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        layer=MemoryLayer(str(row["layer"])),
        source_type=row["source_type"],
        source_id=row["source_id"],
        node_id=row["node_id"],
        summary=row["summary"],
        content=json.loads(row["content_json"]),
        keywords=[str(item) for item in json.loads(row["keywords_json"])],
        artifact_ids=[str(item) for item in json.loads(row["artifact_ids_json"])],
        evidence_ids=[str(item) for item in json.loads(row["evidence_ids_json"])],
        score=float(row["score"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        metadata=json.loads(row["metadata_json"]),
    )


def _compact_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"])
    return {
        "compact_id": row["compact_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "created_at": row["created_at"],
        "focus": row["focus"],
        "summary": row["summary"],
        "result": result,
    }
