from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, TaskState, task_state_from_json, to_jsonable


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
                SELECT task_id, run_id, status, user_goal, root_node_id, created_at, updated_at
                FROM tasks
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

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
