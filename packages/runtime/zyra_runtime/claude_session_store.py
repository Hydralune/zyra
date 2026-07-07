from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .claude_context_assembly_foundation import (
    ContextAssemblySnapshot,
    context_snapshot_metadata,
    context_snapshot_to_messages,
    render_context_snapshot_markdown,
)
from .claude_input_processor import (
    QueryInputProcessingReport,
    QueryInputRecord,
    QuerySourceKind,
    QuerySourceMetadata,
    input_records_to_metadata,
    render_query_input_report_markdown,
)


class CodeWorkerSessionStoreRecordType(StrEnum):
    SESSION_SEED = "session_seed"
    INPUT_ACCEPTED = "input_accepted"
    CONTEXT_SNAPSHOT = "context_snapshot"
    QUERY_ENGINE_ATTACHED = "query_engine_attached"
    SNAPSHOT_MATERIALIZED = "snapshot_materialized"
    FAILURE = "failure"


class CodeWorkerSessionSeedStatus(StrEnum):
    CREATED = "created"
    READY = "ready"
    BLOCKED = "blocked"
    ATTACHED = "attached"


class CodeWorkerSessionStoreStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CodeWorkerSessionStoreRecord:
    record_id: str
    record_type: CodeWorkerSessionStoreRecordType
    session_id: str
    worker_request_id: str
    run_id: str
    task_id: str
    created_at: str
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_type": str(self.record_type),
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "sequence": self.sequence,
            "payload": to_jsonable(self.payload),
            "metadata": to_jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CodeWorkerSessionStoreRecord":
        return cls(
            record_id=str(data.get("record_id") or new_id("cwstore")),
            record_type=_enum_or_default(
                CodeWorkerSessionStoreRecordType,
                data.get("record_type"),
                CodeWorkerSessionStoreRecordType.FAILURE,
            ),
            session_id=str(data.get("session_id") or ""),
            worker_request_id=str(data.get("worker_request_id") or ""),
            run_id=str(data.get("run_id") or ""),
            task_id=str(data.get("task_id") or ""),
            created_at=str(data.get("created_at") or now_iso()),
            sequence=int(data.get("sequence") or 0),
            payload=dict(_as_mapping(data.get("payload"))),
            metadata=dict(_as_mapping(data.get("metadata"))),
        )


@dataclass(frozen=True, slots=True)
class CodeWorkerSessionStoreReceipt:
    ok: bool
    session_id: str
    worker_request_id: str
    path: str
    appended_records: tuple[CodeWorkerSessionStoreRecord, ...]
    created_at: str = field(default_factory=now_iso)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def record_count(self) -> int:
        return len(self.appended_records)

    @property
    def last_sequence(self) -> int:
        return max((record.sequence for record in self.appended_records), default=0)

    def metadata_values(self) -> dict[str, str]:
        return {
            "code_worker_session_store_ok": str(self.ok).lower(),
            "code_worker_session_store_path": self.path,
            "code_worker_session_store_record_count": str(self.record_count),
            "code_worker_session_store_last_sequence": str(self.last_sequence),
            "code_worker_session_store_error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "path": self.path,
            "appended_records": [record.to_dict() for record in self.appended_records],
            "created_at": self.created_at,
            "error": self.error,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerSessionSeed:
    session_id: str
    worker_request_id: str
    run_id: str
    task_id: str
    node_id: str
    status: CodeWorkerSessionSeedStatus
    created_at: str
    input_report: QueryInputProcessingReport
    context_snapshot: ContextAssemblySnapshot | None
    store_receipt: CodeWorkerSessionStoreReceipt | None
    source: QuerySourceMetadata
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            self.status != CodeWorkerSessionSeedStatus.BLOCKED
            and self.input_report.ok
            and self.context_snapshot is not None
            and self.context_snapshot.ok
            and self.store_receipt is not None
            and self.store_receipt.ok
        )

    @property
    def accepted_inputs(self) -> tuple[QueryInputRecord, ...]:
        return self.input_report.accepted_records

    def request_messages(self) -> list[dict[str, Any]]:
        if self.context_snapshot is None:
            return [record.to_context_message() for record in self.accepted_inputs]
        return context_snapshot_to_messages(self.context_snapshot, self.accepted_inputs)

    def metadata_values(self) -> dict[str, str]:
        values = {
            "code_worker_session_seed_ok": str(self.ok).lower(),
            "code_worker_session_seed_status": str(self.status),
            "code_worker_session_seed_session_id": self.session_id,
            "code_worker_session_seed_worker_request_id": self.worker_request_id,
            "code_worker_session_seed_source_path": self.source.source_path,
            "code_worker_session_seed_target_path": self.source.target_path,
        }
        values.update(self.input_report.metadata_values())
        values.update(context_snapshot_metadata(self.context_snapshot))
        if self.store_receipt is not None:
            values.update(self.store_receipt.metadata_values())
        return values

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "status": str(self.status),
            "created_at": self.created_at,
            "ok": self.ok,
            "input_report": self.input_report.to_dict(),
            "context_snapshot": self.context_snapshot.to_dict(include_text=include_text) if self.context_snapshot else None,
            "store_receipt": self.store_receipt.to_dict() if self.store_receipt else None,
            "source": self.source.to_dict(),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerSessionStoreReplay:
    ok: bool
    session_id: str
    worker_request_id: str
    path: str
    records: tuple[CodeWorkerSessionStoreRecord, ...]
    error: str = ""

    @property
    def last_sequence(self) -> int:
        return max((record.sequence for record in self.records), default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "path": self.path,
            "records": [record.to_dict() for record in self.records],
            "last_sequence": self.last_sequence,
            "error": self.error,
        }


class CodeWorkerSessionStore:
    """Append-only store for the pre-query CodeWorker session seed.

    This store is not a Claude project JSONL clone. It persists Zyra-owned seed
    records before QueryEngine dispatch so disabling the store changes runtime
    behavior and blocks the default path.
    """

    def __init__(self, root: str | Path, *, source: QuerySourceMetadata | None = None) -> None:
        self.root = Path(root).resolve()
        self.source = source or default_session_store_source()

    def create_seed_records(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        run_id: str,
        task_id: str,
        node_id: str,
        input_report: QueryInputProcessingReport,
        context_snapshot: ContextAssemblySnapshot | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[CodeWorkerSessionStoreRecord, ...]:
        sequence = 0
        records: list[CodeWorkerSessionStoreRecord] = []
        sequence += 1
        records.append(
            self._record(
                record_type=CodeWorkerSessionStoreRecordType.SESSION_SEED,
                session_id=session_id,
                worker_request_id=worker_request_id,
                run_id=run_id,
                task_id=task_id,
                sequence=sequence,
                payload={
                    "node_id": node_id,
                    "status": str(CodeWorkerSessionSeedStatus.CREATED),
                    "source": self.source.to_dict(),
                    "metadata": dict(metadata or {}),
                },
            )
        )
        for input_record in input_report.records:
            sequence += 1
            records.append(
                self._record(
                    record_type=CodeWorkerSessionStoreRecordType.INPUT_ACCEPTED,
                    session_id=session_id,
                    worker_request_id=worker_request_id,
                    run_id=run_id,
                    task_id=task_id,
                    sequence=sequence,
                    payload=input_record.to_dict(),
                    metadata={"accepted": input_record.accepted, "kind": str(input_record.kind)},
                )
            )
        if context_snapshot is not None:
            sequence += 1
            records.append(
                self._record(
                    record_type=CodeWorkerSessionStoreRecordType.CONTEXT_SNAPSHOT,
                    session_id=session_id,
                    worker_request_id=worker_request_id,
                    run_id=run_id,
                    task_id=task_id,
                    sequence=sequence,
                    payload=context_snapshot.to_dict(include_text=True),
                    metadata={
                        "ok": context_snapshot.ok,
                        "fingerprint": context_snapshot.fingerprint,
                        "selected_block_count": len(context_snapshot.selected_blocks),
                    },
                )
            )
        return tuple(records)

    def append_records(
        self,
        records: Sequence[CodeWorkerSessionStoreRecord],
        *,
        disabled: bool = False,
    ) -> CodeWorkerSessionStoreReceipt:
        session_id = records[0].session_id if records else ""
        worker_request_id = records[0].worker_request_id if records else ""
        path = self.session_path(session_id) if session_id else self.root / "missing-session.jsonl"
        if disabled:
            return CodeWorkerSessionStoreReceipt(
                ok=False,
                session_id=session_id,
                worker_request_id=worker_request_id,
                path=str(path),
                appended_records=(),
                error="code_worker_session_store_disabled",
            )
        if not records:
            return CodeWorkerSessionStoreReceipt(
                ok=False,
                session_id=session_id,
                worker_request_id=worker_request_id,
                path=str(path),
                appended_records=(),
                error="no_session_records_to_append",
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))
                    handle.write("\n")
            self._write_request_index(records)
        except OSError as error:
            return CodeWorkerSessionStoreReceipt(
                ok=False,
                session_id=session_id,
                worker_request_id=worker_request_id,
                path=str(path),
                appended_records=(),
                error=type(error).__name__,
                metadata={"message": str(error)},
            )
        return CodeWorkerSessionStoreReceipt(
            ok=True,
            session_id=session_id,
            worker_request_id=worker_request_id,
            path=str(path),
            appended_records=tuple(records),
            metadata={"source": self.source.to_dict()},
        )

    def mark_query_engine_attached(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        run_id: str,
        task_id: str,
        query_session_id: str,
        resume_token: str,
    ) -> CodeWorkerSessionStoreReceipt:
        replay = self.replay_session(session_id)
        sequence = replay.last_sequence + 1
        record = self._record(
            record_type=CodeWorkerSessionStoreRecordType.QUERY_ENGINE_ATTACHED,
            session_id=session_id,
            worker_request_id=worker_request_id,
            run_id=run_id,
            task_id=task_id,
            sequence=sequence,
            payload={
                "query_session_id": query_session_id,
                "resume_token": resume_token,
                "attached_at": now_iso(),
            },
        )
        return self.append_records((record,))

    def replay_session(self, session_id: str) -> CodeWorkerSessionStoreReplay:
        path = self.session_path(session_id)
        if not path.exists():
            return CodeWorkerSessionStoreReplay(
                ok=False,
                session_id=session_id,
                worker_request_id="",
                path=str(path),
                records=(),
                error="session_store_file_missing",
            )
        try:
            records = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(CodeWorkerSessionStoreRecord.from_dict(json.loads(line)))
        except (OSError, json.JSONDecodeError) as error:
            return CodeWorkerSessionStoreReplay(
                ok=False,
                session_id=session_id,
                worker_request_id="",
                path=str(path),
                records=(),
                error=type(error).__name__,
            )
        worker_request_id = records[0].worker_request_id if records else ""
        return CodeWorkerSessionStoreReplay(
            ok=True,
            session_id=session_id,
            worker_request_id=worker_request_id,
            path=str(path),
            records=tuple(records),
        )

    def replay_request(self, worker_request_id: str) -> CodeWorkerSessionStoreReplay:
        index_path = self.request_index_path(worker_request_id)
        if not index_path.exists():
            return CodeWorkerSessionStoreReplay(
                ok=False,
                session_id="",
                worker_request_id=worker_request_id,
                path=str(index_path),
                records=(),
                error="request_index_missing",
            )
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return CodeWorkerSessionStoreReplay(
                ok=False,
                session_id="",
                worker_request_id=worker_request_id,
                path=str(index_path),
                records=(),
                error=type(error).__name__,
            )
        session_id = str(data.get("session_id") or "")
        if not session_id:
            return CodeWorkerSessionStoreReplay(
                ok=False,
                session_id="",
                worker_request_id=worker_request_id,
                path=str(index_path),
                records=(),
                error="request_index_missing_session_id",
            )
        return self.replay_session(session_id)

    def session_path(self, session_id: str) -> Path:
        safe = _safe_name(session_id or "missing-session")
        return self.root / "code-worker-sessions" / f"{safe}.jsonl"

    def request_index_path(self, worker_request_id: str) -> Path:
        safe = _safe_name(worker_request_id or "missing-request")
        return self.root / "code-worker-sessions" / "requests" / f"{safe}.json"

    def _write_request_index(self, records: Sequence[CodeWorkerSessionStoreRecord]) -> None:
        first = records[0]
        index_path = self.request_index_path(first.worker_request_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": first.session_id,
            "worker_request_id": first.worker_request_id,
            "run_id": first.run_id,
            "task_id": first.task_id,
            "path": str(self.session_path(first.session_id)),
            "updated_at": now_iso(),
            "last_sequence": max(record.sequence for record in records),
        }
        index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _record(
        self,
        *,
        record_type: CodeWorkerSessionStoreRecordType,
        session_id: str,
        worker_request_id: str,
        run_id: str,
        task_id: str,
        sequence: int,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> CodeWorkerSessionStoreRecord:
        return CodeWorkerSessionStoreRecord(
            record_id=new_id("cwstore"),
            record_type=record_type,
            session_id=session_id,
            worker_request_id=worker_request_id,
            run_id=run_id,
            task_id=task_id,
            created_at=now_iso(),
            sequence=sequence,
            payload=dict(payload),
            metadata={
                "source": self.source.to_dict(),
                **dict(metadata or {}),
            },
        )


class CodeWorkerSessionFoundationRuntime:
    """Prepares CodeWorker session seed before QueryEngine dispatch."""

    def __init__(self, *, store: CodeWorkerSessionStore, source: QuerySourceMetadata | None = None) -> None:
        self.store = store
        self.source = source or default_session_foundation_source()

    def build_seed(
        self,
        *,
        request: Any,
        input_report: QueryInputProcessingReport,
        context_snapshot: ContextAssemblySnapshot | None,
        session_id: str | None = None,
        disabled_store: bool = False,
    ) -> CodeWorkerSessionSeed:
        session_id = str(session_id or _as_mapping(getattr(request, "constraints", {})).get("session_id") or new_id("codesession"))
        run_id = str(getattr(request, "run_id", ""))
        task_id = str(getattr(request, "task_id", ""))
        worker_request_id = str(getattr(request, "request_id", ""))
        node_id = str(getattr(request, "node_id", "") or "")
        status = CodeWorkerSessionSeedStatus.READY
        if not input_report.ok or context_snapshot is None or not context_snapshot.ok:
            status = CodeWorkerSessionSeedStatus.BLOCKED
        records = self.store.create_seed_records(
            session_id=session_id,
            worker_request_id=worker_request_id,
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            input_report=input_report,
            context_snapshot=context_snapshot,
            metadata={"foundation_source": self.source.to_dict()},
        )
        receipt = self.store.append_records(records, disabled=disabled_store)
        if not receipt.ok:
            status = CodeWorkerSessionSeedStatus.BLOCKED
        return CodeWorkerSessionSeed(
            session_id=session_id,
            worker_request_id=worker_request_id,
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            status=status,
            created_at=now_iso(),
            input_report=input_report,
            context_snapshot=context_snapshot,
            store_receipt=receipt,
            source=self.source,
            metadata={
                "store_root": str(self.store.root),
                "store_source": self.store.source.to_dict(),
            },
        )

    def seed_events(self, seed: CodeWorkerSessionSeed) -> list[EventRecord]:
        phases = [
            (
                "query_session_seed_created",
                {
                    "status": str(seed.status),
                    "ok": seed.ok,
                    "source": seed.source.to_dict(),
                },
            ),
            (
                "query_input_processed",
                {
                    "ok": seed.input_report.ok,
                    "input_count": len(seed.input_report.records),
                    "accepted_count": len(seed.input_report.accepted_records),
                    "kind_counts": seed.input_report.kind_counts,
                },
            ),
        ]
        if seed.context_snapshot is not None:
            phases.append(
                (
                    "context_snapshot_ready",
                    {
                        "ok": seed.context_snapshot.ok,
                        "snapshot_id": seed.context_snapshot.snapshot_id,
                        "fingerprint": seed.context_snapshot.fingerprint,
                        "selected_block_count": len(seed.context_snapshot.selected_blocks),
                        "active_chars": seed.context_snapshot.active_chars,
                    },
                )
            )
        if seed.store_receipt is not None:
            phases.append(
                (
                    "session_store_append",
                    {
                        "ok": seed.store_receipt.ok,
                        "path": seed.store_receipt.path,
                        "record_count": seed.store_receipt.record_count,
                        "error": seed.store_receipt.error,
                    },
                )
            )
        events = []
        for phase, payload in phases:
            events.append(
                EventRecord(
                    run_id=seed.run_id,
                    task_id=seed.task_id,
                    node_id=seed.node_id or None,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={
                        "query_session": {
                            "session_id": seed.session_id,
                            "worker_request_id": seed.worker_request_id,
                            "phase": phase,
                            **payload,
                        }
                    },
                )
            )
        return events


def default_session_store_source() -> QuerySourceMetadata:
    return QuerySourceMetadata(
        source_repo="claude-code-best",
        source_path="src/utils/sessionStorage.ts",
        target_path="packages/runtime/zyra_runtime/claude_session_store.py",
        source_kind=QuerySourceKind.CLAUDE_SESSION_STORAGE,
        upstream_signals=(
            "append-only transcript",
            "sessionId",
            "parentUuid",
            "readLiteMetadata",
            "loadTranscriptFile",
        ),
        notes=(
            "The store persists Zyra pre-query seed records.",
            "QuerySession still owns in-turn message/turn lifecycle after engine dispatch.",
        ),
    )


def default_session_foundation_source() -> QuerySourceMetadata:
    return QuerySourceMetadata(
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        target_path="packages/runtime/zyra_runtime/claude_session_store.py",
        source_kind=QuerySourceKind.CLAUDE_QUERY_ENGINE,
        upstream_signals=(
            "submitMessage",
            "ProcessUserInputContext",
            "context assembly before query",
            "transcript prewrite",
        ),
        notes=(
            "Foundation runtime creates the session seed before tool loop execution.",
            "Disabling this runtime blocks CodeWorkerRuntime before QueryEngine dispatch.",
        ),
    )


def session_seed_metadata(seed: CodeWorkerSessionSeed | None) -> dict[str, str]:
    if seed is None:
        return {
            "code_worker_session_seed_ok": "false",
            "code_worker_session_seed_status": "",
            "code_worker_session_seed_session_id": "",
        }
    return seed.metadata_values()


def render_session_seed_markdown(seed: CodeWorkerSessionSeed) -> str:
    lines = [
        "# CodeWorker Session Seed",
        "",
        f"- ok: `{str(seed.ok).lower()}`",
        f"- status: `{seed.status}`",
        f"- session_id: `{seed.session_id}`",
        f"- worker_request_id: `{seed.worker_request_id}`",
        f"- run_id: `{seed.run_id}`",
        f"- task_id: `{seed.task_id}`",
        f"- source: `{seed.source.source_path}`",
        f"- target: `{seed.source.target_path}`",
        "",
        "## Store",
        "",
    ]
    if seed.store_receipt is None:
        lines.append("- missing")
    else:
        lines.extend(
            [
                f"- ok: `{str(seed.store_receipt.ok).lower()}`",
                f"- path: `{seed.store_receipt.path}`",
                f"- record_count: `{seed.store_receipt.record_count}`",
                f"- error: `{seed.store_receipt.error}`",
            ]
        )
    lines.extend(["", render_query_input_report_markdown(seed.input_report).rstrip(), ""])
    if seed.context_snapshot is not None:
        lines.extend([render_context_snapshot_markdown(seed.context_snapshot).rstrip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def seed_failure_result_metadata(seed: CodeWorkerSessionSeed | None, *, error: str) -> dict[str, str]:
    values = session_seed_metadata(seed)
    values["code_worker_session_seed_error"] = error
    values["query_turns"] = "0"
    values["tool_steps"] = "0"
    values["context_compactions"] = "0"
    return values


def _safe_name(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe)[:180] or "missing"


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
