from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType, TaskState, now_iso, task_state_from_json, to_jsonable

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

                CREATE TABLE IF NOT EXISTS deleted_conversations (
                    session_id TEXT PRIMARY KEY,
                    deleted_at TEXT NOT NULL,
                    task_ids_json TEXT NOT NULL
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

                CREATE TABLE IF NOT EXISTS user_input_requests (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    node_id TEXT,
                    tool_call_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    request_digest TEXT NOT NULL,
                    questions_json TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    answer_id TEXT,
                    answer_digest TEXT,
                    responder TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    answered_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_user_input_task_status_created
                    ON user_input_requests(task_id, status, created_at);
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
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT checkpoint_json FROM checkpoints WHERE task_id = ?", (state.task_id,),
            ).fetchone()
            if previous is not None:
                saved_metadata = json.loads(str(previous["checkpoint_json"])).get("metadata", {})
                # A long-running executor owns graph state, while a rename can
                # commit concurrently. Preserve the newer title atomically.
                title_revision = int(saved_metadata.get("session_title_revision") or 0)
                if title_revision > int(state.metadata.get("session_title_revision") or 0):
                    for key in ("session_title", "session_title_revision"):
                        state.metadata[key] = saved_metadata[key]
                        payload["metadata"][key] = saved_metadata[key]
            if connection.execute(
                "SELECT 1 FROM deleted_conversations WHERE session_id = ?",
                (_conversation_key(payload),),
            ).fetchone():
                raise ValueError("This conversation has been deleted.")
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
            payload = json.loads(str(row["checkpoint_json"]))
            if connection.execute(
                "SELECT 1 FROM deleted_conversations WHERE session_id = ?",
                (_conversation_key(payload),),
            ).fetchone():
                return None
        return task_state_from_json(payload)

    def list_tasks(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            deleted = {row["session_id"] for row in connection.execute("SELECT session_id FROM deleted_conversations")}
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
                if _conversation_key(checkpoint) in deleted:
                    continue
                metadata = checkpoint.get("metadata")
                if isinstance(metadata, dict):
                    session_id = str(metadata.get("query_session_id") or "").strip()
                    if session_id:
                        value["session_id"] = session_id
                    session_title = str(metadata.get("session_title") or "").strip()
                    if session_title:
                        value["session_title"] = session_title
            values.append(value)
        return values

    def delete_conversation(self, session_id: str) -> dict[str, Any]:
        """Atomically remove a settled conversation from product access.

        Execution evidence is retained for audits; a durable tombstone prevents
        later checkpoint writes or a stale client from resurrecting the chat.
        """
        self.initialize()
        normalized = _canonical_conversation_key(session_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM deleted_conversations WHERE session_id = ?", (normalized,)
            ).fetchone()
            if existing:
                task_ids = json.loads(existing["task_ids_json"])
                deleted_at = existing["deleted_at"]
            else:
                selected = [
                    json.loads(row["checkpoint_json"])
                    for row in connection.execute("SELECT checkpoint_json FROM checkpoints")
                ]
                selected = [task for task in selected if _conversation_key(task) == normalized]
                if not selected:
                    raise KeyError(normalized)
                terminal = {"completed", "failed", "cancelled", "rejected", "timed_out"}
                if any(str(task.get("status")) not in terminal for task in selected):
                    raise ValueError("会话中仍有未结束的任务，请先停止任务再删除。")
                task_ids = sorted(task["task_id"] for task in selected)
                deleted_at = now_iso()
                connection.execute(
                    "INSERT INTO deleted_conversations (session_id, deleted_at, task_ids_json) VALUES (?, ?, ?)",
                    (normalized, deleted_at, json.dumps(task_ids)),
                )
        return {"schema": "zyra.session-deletion.v1", "session_id": normalized,
                "task_ids": task_ids, "deleted_at": deleted_at, "state_owner": "task_store_projection"}

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

    def create_user_input_request(
        self,
        *,
        request_id: str,
        run_id: str,
        task_id: str,
        node_id: str | None,
        tool_call_id: str,
        request_digest: str,
        questions: list[dict[str, Any]],
        created_at: str,
    ) -> dict[str, Any]:
        """Create or replay one canonical model-to-user input request.

        The provider tool may be replayed after a transport interruption.  A
        stable request identity is therefore idempotent only when every task,
        tool-call and payload binding still matches exactly.
        """

        self.initialize()
        serialized_questions = json.dumps(
            questions,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM user_input_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                existing = _user_input_row_to_dict(row)
                if (
                    existing["run_id"] != run_id
                    or existing["task_id"] != task_id
                    or existing["tool_call_id"] != tool_call_id
                    or existing["request_digest"] != request_digest
                ):
                    raise ValueError("user input request identity conflict")
                return existing
            connection.execute(
                """
                INSERT INTO user_input_requests (
                    request_id, run_id, task_id, node_id, tool_call_id,
                    status, revision, request_digest, questions_json,
                    answers_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, '{}', ?, ?)
                """,
                (
                    request_id,
                    run_id,
                    task_id,
                    node_id,
                    tool_call_id,
                    request_digest,
                    serialized_questions,
                    created_at,
                    created_at,
                ),
            )
            _insert_user_input_event(
                connection,
                event_id=f"event_{request_id}_requested",
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                created_at=created_at,
                payload={
                    "schema": "zyra.user-input-event/v1",
                    "phase": "requested",
                    "request_id": request_id,
                    "tool_call_id": tool_call_id,
                    "request_digest": request_digest,
                    "canonical_owner": "SQLiteStore.user_input_requests",
                },
            )
            row = connection.execute(
                "SELECT * FROM user_input_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        assert row is not None
        return _user_input_row_to_dict(row)

    def user_input_request(
        self,
        task_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_input_requests
                WHERE task_id = ? AND request_id = ?
                """,
                (task_id, request_id),
            ).fetchone()
        return _user_input_row_to_dict(row) if row is not None else None

    def user_input_requests(
        self,
        task_id: str,
        *,
        include_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        self.initialize()
        where = "task_id = ?" if include_terminal else "task_id = ? AND status = 'pending'"
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM user_input_requests
                WHERE {where}
                ORDER BY created_at ASC, rowid ASC
                """,
                (task_id,),
            ).fetchall()
        return [_user_input_row_to_dict(row) for row in rows]

    def answer_user_input_request(
        self,
        *,
        task_id: str,
        request_id: str,
        expected_revision: int,
        answer_id: str,
        answer_digest: str,
        answers: dict[str, Any],
        responder: str,
        answered_at: str,
    ) -> dict[str, Any]:
        """Commit one exact answer with optimistic concurrency and replay safety."""

        self.initialize()
        serialized_answers = json.dumps(
            answers,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM user_input_requests
                WHERE task_id = ? AND request_id = ?
                """,
                (task_id, request_id),
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            existing = _user_input_row_to_dict(row)
            if existing["status"] == "answered":
                if (
                    existing.get("answer_id") == answer_id
                    and existing.get("answer_digest") == answer_digest
                ):
                    return existing
                raise ValueError("user input request was already answered")
            if existing["status"] != "pending":
                raise ValueError("user input request is no longer pending")
            if existing["revision"] != expected_revision:
                raise ValueError("user input request revision conflict")
            cursor = connection.execute(
                """
                UPDATE user_input_requests
                SET status = 'answered', revision = revision + 1,
                    answers_json = ?, answer_id = ?, answer_digest = ?,
                    responder = ?, answered_at = ?, updated_at = ?
                WHERE task_id = ? AND request_id = ? AND revision = ?
                """,
                (
                    serialized_answers,
                    answer_id,
                    answer_digest,
                    responder,
                    answered_at,
                    answered_at,
                    task_id,
                    request_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("user input request revision conflict")
            _insert_user_input_event(
                connection,
                event_id=f"event_{request_id}_{answer_id}",
                run_id=str(existing["run_id"]),
                task_id=task_id,
                node_id=existing.get("node_id"),
                created_at=answered_at,
                payload={
                    "schema": "zyra.user-input-event/v1",
                    "phase": "answered",
                    "request_id": request_id,
                    "answer_digest": answer_digest,
                    "responder": responder,
                    "canonical_owner": "SQLiteStore.user_input_requests",
                },
            )
            row = connection.execute(
                "SELECT * FROM user_input_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        assert row is not None
        return _user_input_row_to_dict(row)

    def close_user_input_request(
        self,
        *,
        task_id: str,
        request_id: str,
        expected_revision: int,
        status: str,
        closed_at: str,
    ) -> dict[str, Any]:
        """Close an unanswered request without leaving a pending zombie."""

        if status not in {"cancelled", "expired"}:
            raise ValueError("user input close status must be cancelled or expired")
        self.initialize()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM user_input_requests
                WHERE task_id = ? AND request_id = ?
                """,
                (task_id, request_id),
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            existing = _user_input_row_to_dict(row)
            if existing["status"] != "pending":
                return existing
            if existing["revision"] != expected_revision:
                raise ValueError("user input request revision conflict")
            cursor = connection.execute(
                """
                UPDATE user_input_requests
                SET status = ?, revision = revision + 1, updated_at = ?
                WHERE task_id = ? AND request_id = ? AND revision = ?
                """,
                (status, closed_at, task_id, request_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("user input request revision conflict")
            _insert_user_input_event(
                connection,
                event_id=f"event_{request_id}_{status}",
                run_id=str(existing["run_id"]),
                task_id=task_id,
                node_id=existing.get("node_id"),
                created_at=closed_at,
                payload={
                    "schema": "zyra.user-input-event/v1",
                    "phase": status,
                    "request_id": request_id,
                    "canonical_owner": "SQLiteStore.user_input_requests",
                },
            )
            row = connection.execute(
                "SELECT * FROM user_input_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        assert row is not None
        return _user_input_row_to_dict(row)

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


def _canonical_conversation_key(value: str) -> str:
    return f"session_{value[5:]}" if value.startswith("task:task_") else value


def _conversation_key(task: dict[str, Any]) -> str:
    metadata = task.get("metadata") or {}
    return _canonical_conversation_key(str(metadata.get("query_session_id") or metadata.get("session_id")
                                           or task.get("session_id") or f"task:{task['task_id']}"))


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


def _user_input_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": "zyra.user-input-request/v1",
        "request_id": str(row["request_id"]),
        "run_id": str(row["run_id"]),
        "task_id": str(row["task_id"]),
        "node_id": str(row["node_id"] or "") or None,
        "tool_call_id": str(row["tool_call_id"]),
        "status": str(row["status"]),
        "revision": int(row["revision"]),
        "request_digest": str(row["request_digest"]),
        "questions": json.loads(str(row["questions_json"])),
        "answers": json.loads(str(row["answers_json"])),
        "answer_id": str(row["answer_id"] or "") or None,
        "answer_digest": str(row["answer_digest"] or "") or None,
        "responder": str(row["responder"] or "") or None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "answered_at": str(row["answered_at"] or "") or None,
        "canonical_owner": "SQLiteStore.user_input_requests",
    }


def _insert_user_input_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    run_id: str,
    task_id: str,
    node_id: str | None,
    created_at: str,
    payload: dict[str, Any],
) -> None:
    """Insert the audit fact inside its owning request-state transaction."""

    connection.execute(
        """
        INSERT INTO events (
            event_id, run_id, task_id, node_id, event_type, created_at, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            run_id,
            task_id,
            node_id,
            str(EventType.USER_INPUT),
            created_at,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
