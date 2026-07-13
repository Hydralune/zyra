from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    HistoryHead,
    HistoryKind,
    HistoryRecord,
    ObservationScope,
    ReplayIssue,
    canonical_json,
    digest_value,
    history_record_from_mapping,
    utc_now,
)


class BrowserHistoryStoreError(RuntimeError):
    code = "browser_history_store_error"


class BrowserHistoryConflict(BrowserHistoryStoreError):
    code = "browser_history_conflict"


class BrowserHistoryCorruption(BrowserHistoryStoreError):
    code = "browser_history_corruption"


class BrowserHistoryScopeMismatch(BrowserHistoryStoreError):
    code = "browser_history_scope_mismatch"


@dataclass(frozen=True, slots=True)
class HistoryStorePolicy:
    max_record_bytes: int = 2_000_000
    max_segment_bytes: int = 16_000_000
    max_records_per_scope: int = 100_000
    fsync: bool = True
    repair_partial_tail: bool = True
    verify_on_read: bool = True

    def __post_init__(self) -> None:
        if self.max_record_bytes < 1024:
            raise ValueError("history max_record_bytes must be at least 1024")
        if self.max_segment_bytes < self.max_record_bytes:
            raise ValueError("history segment must fit at least one record")
        if self.max_records_per_scope < 1:
            raise ValueError("history max_records_per_scope must be positive")


@dataclass(frozen=True, slots=True)
class HistoryAppendReceipt:
    scope_key: str
    record_id: str
    sequence: int
    content_digest: str
    segment: int
    offset: int
    length: int
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "record_id": self.record_id,
            "sequence": self.sequence,
            "content_digest": self.content_digest,
            "segment": self.segment,
            "offset": self.offset,
            "length": self.length,
            "idempotent": self.idempotent,
        }


@dataclass(frozen=True, slots=True)
class HistoryAudit:
    scope_key: str
    records: int
    segments: int
    head_digest: str
    issues: tuple[ReplayIssue, ...]
    repaired_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not any(item.fatal for item in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "records": self.records,
            "segments": self.segments,
            "head_digest": self.head_digest,
            "issues": [item.to_dict() for item in self.issues],
            "repaired_bytes": self.repaired_bytes,
            "ok": self.ok,
        }


class BrowserHistoryStore:
    """Durable append-only browser history with per-scope hash chains.

    This store owns browser observability history only. Canonical task/session
    state remains in the existing SQLite owner and canonical events remain in
    the event log. Every record carries those identities and causal event ids.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        policy: HistoryStorePolicy | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.policy = policy or HistoryStorePolicy()
        self._locks_guard = threading.RLock()
        self._locks: dict[str, threading.RLock] = {}

    def append(
        self,
        record: HistoryRecord,
        *,
        expected_head_digest: str | None = None,
    ) -> HistoryAppendReceipt:
        scope_key = record.scope.key
        with self._scope_lock(scope_key):
            self._ensure_scope_metadata(record.scope)
            head = self.head(record.scope)
            if expected_head_digest is not None:
                actual = head.content_digest if head else ""
                if actual != expected_head_digest:
                    raise BrowserHistoryConflict(
                        f"history head changed: expected {expected_head_digest!r}, got {actual!r}"
                    )
            existing = self._find_record(record.scope, record.record_id)
            if existing is not None:
                if existing.content_digest != record.content_digest:
                    raise BrowserHistoryConflict(
                        f"record id {record.record_id!r} was reused with different content"
                    )
                location = self._location_for_record(record.scope, record.record_id)
                return HistoryAppendReceipt(
                    scope_key=scope_key,
                    record_id=record.record_id,
                    sequence=record.sequence,
                    content_digest=record.content_digest,
                    segment=location.get("segment", 0),
                    offset=location.get("offset", 0),
                    length=location.get("length", 0),
                    idempotent=True,
                )
            expected_sequence = 1 if head is None else head.sequence + 1
            expected_previous = "" if head is None else head.content_digest
            if record.sequence != expected_sequence:
                raise BrowserHistoryConflict(
                    f"history sequence must be {expected_sequence}, got {record.sequence}"
                )
            if record.previous_digest != expected_previous:
                raise BrowserHistoryConflict(
                    "history previous_digest does not match the durable head"
                )
            if record.sequence > self.policy.max_records_per_scope:
                raise BrowserHistoryStoreError("history record limit exceeded")
            payload = canonical_json(record.to_dict()).encode("utf-8") + b"\n"
            if len(payload) > self.policy.max_record_bytes:
                raise BrowserHistoryStoreError(
                    f"history record exceeds {self.policy.max_record_bytes} bytes"
                )
            segment = self._select_segment(record.scope, len(payload))
            path = self._segment_path(record.scope, segment)
            path.parent.mkdir(parents=True, exist_ok=True)
            offset = path.stat().st_size if path.exists() else 0
            self._append_bytes(path, payload)
            receipt = HistoryAppendReceipt(
                scope_key=scope_key,
                record_id=record.record_id,
                sequence=record.sequence,
                content_digest=record.content_digest,
                segment=segment,
                offset=offset,
                length=len(payload),
            )
            self._write_index_entry(record.scope, receipt)
            self._write_head(
                record.scope,
                HistoryHead(
                    scope_key=scope_key,
                    sequence=record.sequence,
                    record_id=record.record_id,
                    content_digest=record.content_digest,
                    updated_at=record.created_at,
                    segment=segment,
                    offset=offset,
                ),
            )
            return receipt

    def append_many(
        self,
        scope: ObservationScope,
        records: Sequence[HistoryRecord],
        *,
        expected_head_digest: str | None = None,
    ) -> tuple[HistoryAppendReceipt, ...]:
        if not records:
            return ()
        if any(item.scope != scope for item in records):
            raise BrowserHistoryScopeMismatch("append_many records cross observation scopes")
        receipts: list[HistoryAppendReceipt] = []
        expected = expected_head_digest
        for record in records:
            receipt = self.append(record, expected_head_digest=expected)
            receipts.append(receipt)
            expected = receipt.content_digest
        return tuple(receipts)

    def next_record(
        self,
        scope: ObservationScope,
        kind: HistoryKind,
        payload: Mapping[str, Any],
        *,
        causal_event_ids: Sequence[str] = (),
        artifact_ids: Sequence[str] = (),
        tool_call_id: str = "",
        parent_record_id: str = "",
        branch_id: str = "main",
    ) -> HistoryRecord:
        head = self.head(scope)
        return HistoryRecord(
            scope=scope,
            kind=kind,
            sequence=1 if head is None else head.sequence + 1,
            previous_digest="" if head is None else head.content_digest,
            payload=dict(payload),
            causal_event_ids=tuple(causal_event_ids),
            artifact_ids=tuple(artifact_ids),
            tool_call_id=tool_call_id,
            parent_record_id=parent_record_id or (head.record_id if head else ""),
            branch_id=branch_id,
        )

    def record(
        self,
        scope: ObservationScope,
        kind: HistoryKind,
        payload: Mapping[str, Any],
        *,
        causal_event_ids: Sequence[str] = (),
        artifact_ids: Sequence[str] = (),
        tool_call_id: str = "",
        parent_record_id: str = "",
        branch_id: str = "main",
    ) -> tuple[HistoryRecord, HistoryAppendReceipt]:
        with self._scope_lock(scope.key):
            item = self.next_record(
                scope,
                kind,
                payload,
                causal_event_ids=causal_event_ids,
                artifact_ids=artifact_ids,
                tool_call_id=tool_call_id,
                parent_record_id=parent_record_id,
                branch_id=branch_id,
            )
            return item, self.append(item)

    def head(self, scope: ObservationScope) -> HistoryHead | None:
        path = self._head_path(scope)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return HistoryHead(
                scope_key=str(value["scope_key"]),
                sequence=int(value["sequence"]),
                record_id=str(value["record_id"]),
                content_digest=str(value["content_digest"]),
                updated_at=str(value["updated_at"]),
                segment=int(value.get("segment") or 0),
                offset=int(value.get("offset") or 0),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise BrowserHistoryCorruption(f"invalid history head: {error}") from error

    def records(
        self,
        scope: ObservationScope,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
        kinds: Iterable[HistoryKind] = (),
        branch_id: str = "",
    ) -> tuple[HistoryRecord, ...]:
        allowed = frozenset(kinds)
        output: list[HistoryRecord] = []
        previous = ""
        last_sequence = 0
        for item in self._iter_records(scope):
            if self.policy.verify_on_read:
                if item.sequence != last_sequence + 1:
                    raise BrowserHistoryCorruption(
                        f"history sequence gap before {item.record_id}"
                    )
                if item.previous_digest != previous:
                    raise BrowserHistoryCorruption(
                        f"history digest chain mismatch at {item.record_id}"
                    )
                previous = item.content_digest
                last_sequence = item.sequence
            if item.sequence <= after_sequence:
                continue
            if allowed and item.kind not in allowed:
                continue
            if branch_id and item.branch_id != branch_id:
                continue
            output.append(item)
            if limit is not None and len(output) >= max(0, limit):
                break
        return tuple(output)

    def tail(
        self,
        scope: ObservationScope,
        *,
        limit: int = 100,
    ) -> tuple[HistoryRecord, ...]:
        if limit <= 0:
            return ()
        values = self.records(scope)
        return values[-limit:]

    def find_by_tool_call(
        self,
        scope: ObservationScope,
        tool_call_id: str,
    ) -> tuple[HistoryRecord, ...]:
        return tuple(
            item
            for item in self.records(scope)
            if item.tool_call_id == tool_call_id
        )

    def find_by_artifact(
        self,
        scope: ObservationScope,
        artifact_id: str,
    ) -> tuple[HistoryRecord, ...]:
        return tuple(
            item
            for item in self.records(scope)
            if artifact_id in item.artifact_ids
        )

    def audit(
        self,
        scope: ObservationScope,
        *,
        repair: bool = False,
    ) -> HistoryAudit:
        issues: list[ReplayIssue] = []
        records = 0
        segments = 0
        previous = ""
        expected_sequence = 1
        repaired_bytes = 0
        for path in self._segment_paths(scope):
            segments += 1
            raw = path.read_bytes()
            cursor = 0
            for line in raw.splitlines(keepends=True):
                start = cursor
                cursor += len(line)
                if not line.endswith(b"\n"):
                    issue = ReplayIssue(
                        code="partial_history_tail",
                        summary="history segment ended with a partial record",
                        sequence=expected_sequence,
                        fatal=not repair,
                        details={"path": str(path), "offset": start},
                    )
                    issues.append(issue)
                    if repair and self.policy.repair_partial_tail:
                        with path.open("r+b") as handle:
                            handle.truncate(start)
                            if self.policy.fsync:
                                handle.flush()
                                os.fsync(handle.fileno())
                        repaired_bytes += len(raw) - start
                    break
                try:
                    value = json.loads(line)
                    record = history_record_from_mapping(value)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    issues.append(
                        ReplayIssue(
                            code="invalid_history_json",
                            summary=str(error),
                            sequence=expected_sequence,
                            fatal=True,
                            details={"path": str(path), "offset": start},
                        )
                    )
                    continue
                records += 1
                if record.scope != scope:
                    issues.append(
                        ReplayIssue(
                            code="history_scope_mismatch",
                            summary="record scope differs from segment scope",
                            sequence=record.sequence,
                            record_id=record.record_id,
                            fatal=True,
                        )
                    )
                if record.sequence != expected_sequence:
                    issues.append(
                        ReplayIssue(
                            code="history_sequence_gap",
                            summary=f"expected {expected_sequence}, got {record.sequence}",
                            sequence=record.sequence,
                            record_id=record.record_id,
                            fatal=True,
                        )
                    )
                if record.previous_digest != previous:
                    issues.append(
                        ReplayIssue(
                            code="history_digest_mismatch",
                            summary="record does not link to previous digest",
                            sequence=record.sequence,
                            record_id=record.record_id,
                            fatal=True,
                        )
                    )
                stored_digest = str(value.get("content_digest") or "")
                if stored_digest and stored_digest != record.content_digest:
                    issues.append(
                        ReplayIssue(
                            code="history_content_tampered",
                            summary="stored record digest does not match record content",
                            sequence=record.sequence,
                            record_id=record.record_id,
                            fatal=True,
                        )
                    )
                previous = record.content_digest
                expected_sequence = record.sequence + 1
        return HistoryAudit(
            scope_key=scope.key,
            records=records,
            segments=segments,
            head_digest=previous,
            issues=tuple(issues),
            repaired_bytes=repaired_bytes,
        )

    def list_scopes(
        self,
        *,
        task_id: str = "",
    ) -> tuple[dict[str, Any], ...]:
        output: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/*/scope.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            scope_value = value.get("scope")
            if not isinstance(scope_value, dict):
                continue
            if task_id and str(scope_value.get("task_id") or "") != task_id:
                continue
            head_path = path.parent / "head.json"
            head_value: dict[str, Any] = {}
            if head_path.exists():
                try:
                    head_value = json.loads(head_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    head_value = {"corrupt": True}
            output.append(
                {
                    "scope": dict(scope_value),
                    "scope_key": str(value.get("scope_key") or ""),
                    "created_at": str(value.get("created_at") or ""),
                    "head": head_value,
                }
            )
        return tuple(output)

    def delete_scope(self, scope: ObservationScope) -> None:
        raise BrowserHistoryStoreError(
            "browser history is append-only; retention must use a reviewed archive operation"
        )

    def _iter_records(self, scope: ObservationScope) -> Iterator[HistoryRecord]:
        for path in self._segment_paths(scope):
            with path.open("rb") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.endswith(b"\n"):
                        raise BrowserHistoryCorruption(
                            f"partial record in {path.name}:{line_number}"
                        )
                    try:
                        value = json.loads(line)
                        item = history_record_from_mapping(value)
                    except (TypeError, ValueError, json.JSONDecodeError) as error:
                        raise BrowserHistoryCorruption(
                            f"invalid record in {path.name}:{line_number}: {error}"
                        ) from error
                    if item.scope != scope:
                        raise BrowserHistoryScopeMismatch(
                            f"record {item.record_id} belongs to another scope"
                        )
                    yield item

    def _find_record(
        self,
        scope: ObservationScope,
        record_id: str,
    ) -> HistoryRecord | None:
        location = self._location_for_record(scope, record_id)
        if not location:
            return None
        path = self._segment_path(scope, int(location["segment"]))
        try:
            with path.open("rb") as handle:
                handle.seek(int(location["offset"]))
                line = handle.read(int(location["length"]))
            return history_record_from_mapping(json.loads(line))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise BrowserHistoryCorruption(
                f"history index points at an invalid record: {error}"
            ) from error

    def _location_for_record(
        self,
        scope: ObservationScope,
        record_id: str,
    ) -> dict[str, int]:
        path = self._index_path(scope)
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(value.get("record_id") or "") == record_id:
                    return {
                        "segment": int(value.get("segment") or 0),
                        "offset": int(value.get("offset") or 0),
                        "length": int(value.get("length") or 0),
                    }
        return {}

    def _ensure_scope_metadata(self, scope: ObservationScope) -> None:
        path = self._scope_path(scope)
        expected = {
            "schema": "zyra.browser-observability.history-scope.v1",
            "scope_key": scope.key,
            "scope": scope.to_dict(),
        }
        if path.exists():
            try:
                actual = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise BrowserHistoryCorruption(f"invalid scope metadata: {error}") from error
            if actual.get("scope") != expected["scope"]:
                raise BrowserHistoryScopeMismatch(
                    "history scope key collision or identity mismatch"
                )
            return
        expected["created_at"] = utc_now()
        self._atomic_json(path, expected)

    def _select_segment(
        self,
        scope: ObservationScope,
        incoming_bytes: int,
    ) -> int:
        paths = self._segment_paths(scope)
        if not paths:
            return 0
        current = paths[-1]
        segment = int(current.stem.split("-")[-1])
        if current.stat().st_size + incoming_bytes > self.policy.max_segment_bytes:
            return segment + 1
        return segment

    def _write_index_entry(
        self,
        scope: ObservationScope,
        receipt: HistoryAppendReceipt,
    ) -> None:
        payload = canonical_json(receipt.to_dict()).encode("utf-8") + b"\n"
        self._append_bytes(self._index_path(scope), payload)

    def _write_head(
        self,
        scope: ObservationScope,
        head: HistoryHead,
    ) -> None:
        self._atomic_json(
            self._head_path(scope),
            {
                "schema": "zyra.browser-observability.history-head.v1",
                **head.to_dict(),
            },
        )

    def _append_bytes(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            if self.policy.fsync:
                os.fsync(handle.fileno())

    def _atomic_json(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = (canonical_json(dict(value)) + "\n").encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            if self.policy.fsync:
                os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _segment_paths(self, scope: ObservationScope) -> tuple[Path, ...]:
        return tuple(sorted(self._scope_dir(scope).glob("segment-*.jsonl")))

    def _segment_path(self, scope: ObservationScope, segment: int) -> Path:
        return self._scope_dir(scope) / f"segment-{segment:06d}.jsonl"

    def _scope_path(self, scope: ObservationScope) -> Path:
        return self._scope_dir(scope) / "scope.json"

    def _head_path(self, scope: ObservationScope) -> Path:
        return self._scope_dir(scope) / "head.json"

    def _index_path(self, scope: ObservationScope) -> Path:
        return self._scope_dir(scope) / "index.jsonl"

    def _scope_dir(self, scope: ObservationScope) -> Path:
        return self.root / scope.task_id / scope.key

    def _lock_for(self, key: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def _scope_lock(self, key: str) -> Iterator[None]:
        lock = self._lock_for(key)
        with lock:
            yield
