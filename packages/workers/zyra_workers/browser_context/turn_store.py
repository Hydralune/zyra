from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from zyra_core import now_iso

from ..browser_state.contracts import digest_json
from ..browser_state.errors import BrowserMessageManagerDisabled
from ..browser_state.text import normalize_page_text, normalized_fact_key
from .models import BrowserMessageTurn


@dataclass(frozen=True, slots=True)
class BrowserTurnProjectionRecord:
    turn_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    canonical_session_id: str
    browser_session_id: str
    target_id: str
    target_generation: int
    cdp_session_id: str
    cdp_generation: int
    document_loader_id: str
    capture_id: str
    capture_digest: str
    selector_revision_id: str
    disclosure_id: str
    disclosure_fingerprint: str
    disclosure_source_id: str
    disclosure_tokens: int
    disclosure_bytes: int
    facts: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    memory_candidate_ids: tuple[str, ...]
    action_projection_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    next_context_receipt_id: str
    next_context_accepted: bool
    read_once_consumed: bool
    status: str
    findings: tuple[str, ...] = ()
    sequence: int = 0
    previous_turn_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def identity_key(self) -> str:
        return "::".join((self.canonical_session_id, self.browser_session_id, self.target_id))

    @property
    def generation_key(self) -> str:
        return "::".join((
            self.identity_key,
            str(self.target_generation),
            self.cdp_session_id,
            str(self.cdp_generation),
            self.document_loader_id,
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "canonical_session_id": self.canonical_session_id,
            "browser_session_id": self.browser_session_id,
            "target_id": self.target_id,
            "target_generation": self.target_generation,
            "cdp_session_id": self.cdp_session_id,
            "cdp_generation": self.cdp_generation,
            "document_loader_id": self.document_loader_id,
            "capture_id": self.capture_id,
            "capture_digest": self.capture_digest,
            "selector_revision_id": self.selector_revision_id,
            "disclosure_id": self.disclosure_id,
            "disclosure_fingerprint": self.disclosure_fingerprint,
            "disclosure_source_id": self.disclosure_source_id,
            "disclosure_tokens": self.disclosure_tokens,
            "disclosure_bytes": self.disclosure_bytes,
            "facts": list(self.facts),
            "artifact_ids": list(self.artifact_ids),
            "memory_candidate_ids": list(self.memory_candidate_ids),
            "action_projection_ids": list(self.action_projection_ids),
            "event_ids": list(self.event_ids),
            "next_context_receipt_id": self.next_context_receipt_id,
            "next_context_accepted": self.next_context_accepted,
            "read_once_consumed": self.read_once_consumed,
            "status": self.status,
            "findings": list(self.findings),
            "sequence": self.sequence,
            "previous_turn_id": self.previous_turn_id,
            "identity_key": self.identity_key,
            "generation_key": self.generation_key,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BrowserTurnProjectionRecord":
        return cls(
            turn_id=str(value.get("turn_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            worker_request_id=str(value.get("worker_request_id") or ""),
            canonical_session_id=str(value.get("canonical_session_id") or ""),
            browser_session_id=str(value.get("browser_session_id") or ""),
            target_id=str(value.get("target_id") or ""),
            target_generation=_integer(value.get("target_generation")),
            cdp_session_id=str(value.get("cdp_session_id") or ""),
            cdp_generation=_integer(value.get("cdp_generation")),
            document_loader_id=str(value.get("document_loader_id") or ""),
            capture_id=str(value.get("capture_id") or ""),
            capture_digest=str(value.get("capture_digest") or ""),
            selector_revision_id=str(value.get("selector_revision_id") or ""),
            disclosure_id=str(value.get("disclosure_id") or ""),
            disclosure_fingerprint=str(value.get("disclosure_fingerprint") or ""),
            disclosure_source_id=str(value.get("disclosure_source_id") or ""),
            disclosure_tokens=_integer(value.get("disclosure_tokens")),
            disclosure_bytes=_integer(value.get("disclosure_bytes")),
            facts=_strings(value.get("facts")),
            artifact_ids=_strings(value.get("artifact_ids")),
            memory_candidate_ids=_strings(value.get("memory_candidate_ids")),
            action_projection_ids=_strings(value.get("action_projection_ids")),
            event_ids=_strings(value.get("event_ids")),
            next_context_receipt_id=str(value.get("next_context_receipt_id") or ""),
            next_context_accepted=bool(value.get("next_context_accepted")),
            read_once_consumed=bool(value.get("read_once_consumed")),
            status=str(value.get("status") or "blocked"),
            findings=_strings(value.get("findings")),
            sequence=_integer(value.get("sequence")),
            previous_turn_id=str(value.get("previous_turn_id") or ""),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class BrowserTurnStoreSnapshot:
    records: int
    sessions: int
    disclosures: int
    consumed_disclosures: int
    writes: int
    loads: int
    conflicts: int
    corrupt_recoveries: int
    pruned: int
    path: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": "BrowserTurnProjectionStore",
            "owner_unit": "M1-S04B-01",
            "canonical_history_owner": False,
            "records": self.records,
            "sessions": self.sessions,
            "disclosures": self.disclosures,
            "consumed_disclosures": self.consumed_disclosures,
            "writes": self.writes,
            "loads": self.loads,
            "conflicts": self.conflicts,
            "corrupt_recoveries": self.corrupt_recoveries,
            "pruned": self.pruned,
            "path": self.path,
            "digest": self.digest,
        }


class BrowserTurnProjectionStore:
    """Durable idempotency/read-once index; event log remains canonical history."""

    SCHEMA = "zyra.browser-turn-projection-index.v1"

    def __init__(
        self,
        root: str | Path,
        *,
        max_records_per_session: int = 64,
        disabled: bool = False,
    ) -> None:
        if max_records_per_session <= 0:
            raise ValueError("max_records_per_session must be positive")
        self.root = Path(root).resolve()
        self.path = self.root / "browser-turn-projections.json"
        self.max_records_per_session = max_records_per_session
        self.disabled = disabled
        self._lock = threading.RLock()
        self._records: dict[str, BrowserTurnProjectionRecord] = {}
        self._latest: dict[str, str] = {}
        self._disclosures: dict[str, str] = {}
        self._request_index: dict[str, str] = {}
        self._writes = 0
        self._loads = 0
        self._conflicts = 0
        self._corrupt_recoveries = 0
        self._pruned = 0
        if not disabled:
            self.root.mkdir(parents=True, exist_ok=True)
            self._load()

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserMessageManagerDisabled("browser turn projection store is disabled")

    def record(
        self,
        turn: BrowserMessageTurn,
        *,
        canonical_session_id: str,
        target_id: str,
        target_generation: int,
        cdp_session_id: str,
        cdp_generation: int,
        document_loader_id: str,
        capture_digest: str,
        expected_previous_turn_id: str | None = None,
    ) -> BrowserTurnProjectionRecord:
        self._ensure_available()
        identity_key = "::".join((canonical_session_id, turn.browser_session_id, target_id))
        with self._lock:
            current_id = self._latest.get(identity_key, "")
            if expected_previous_turn_id is not None and current_id != expected_previous_turn_id:
                self._conflicts += 1
                raise ValueError(
                    "browser turn projection CAS conflict: "
                    f"expected={expected_previous_turn_id!r}, actual={current_id!r}"
                )
            duplicate_id = self._request_index.get(turn.worker_request_id)
            if duplicate_id:
                existing = self._records[duplicate_id]
                if existing.capture_digest != capture_digest:
                    self._conflicts += 1
                    raise ValueError("worker request id was reused for a different DOM capture")
                return existing
            disclosure_owner = self._disclosures.get(turn.disclosure.disclosure_id)
            if disclosure_owner and disclosure_owner != turn.turn_id:
                self._conflicts += 1
                raise ValueError("browser disclosure id already belongs to another turn")
            previous = self._records.get(current_id)
            sequence = previous.sequence + 1 if previous else 1
            fingerprint = hashlib.sha256(turn.disclosure.text.encode("utf-8")).hexdigest()
            record = BrowserTurnProjectionRecord(
                turn_id=turn.turn_id,
                run_id=turn.run_id,
                task_id=turn.task_id,
                worker_request_id=turn.worker_request_id,
                canonical_session_id=canonical_session_id,
                browser_session_id=turn.browser_session_id,
                target_id=target_id,
                target_generation=target_generation,
                cdp_session_id=cdp_session_id,
                cdp_generation=cdp_generation,
                document_loader_id=document_loader_id,
                capture_id=turn.capture_id,
                capture_digest=capture_digest,
                selector_revision_id=turn.selector_revision_id,
                disclosure_id=turn.disclosure.disclosure_id,
                disclosure_fingerprint=fingerprint,
                disclosure_source_id=turn.next_context.source_id,
                disclosure_tokens=turn.disclosure.tokens,
                disclosure_bytes=turn.disclosure.bytes,
                facts=_deduplicate_facts(turn.disclosure.facts),
                artifact_ids=turn.artifact_ids,
                memory_candidate_ids=tuple(item.candidate_id for item in turn.memory_candidates),
                action_projection_ids=tuple(item.projection_id for item in turn.action_results),
                event_ids=tuple(item.event_id for item in turn.events),
                next_context_receipt_id=turn.next_context.receipt_id,
                next_context_accepted=turn.next_context.accepted,
                # Browser capture only enqueues a read-once disclosure.  The
                # canonical task/context delivery bridge marks it consumed
                # after a subsequent 02D provider request returns.
                read_once_consumed=False,
                status=str(turn.status),
                findings=turn.findings,
                sequence=sequence,
                previous_turn_id=current_id,
            )
            self._records[record.turn_id] = record
            self._latest[identity_key] = record.turn_id
            self._disclosures[record.disclosure_id] = record.turn_id
            self._request_index[record.worker_request_id] = record.turn_id
            self._prune_locked(identity_key)
            self._persist_locked()
            self._writes += 1
            return record

    def latest(
        self,
        canonical_session_id: str,
        browser_session_id: str,
        target_id: str,
    ) -> BrowserTurnProjectionRecord | None:
        self._ensure_available()
        key = "::".join((canonical_session_id, browser_session_id, target_id))
        with self._lock:
            return self._records.get(self._latest.get(key, ""))

    def by_request(self, worker_request_id: str) -> BrowserTurnProjectionRecord | None:
        self._ensure_available()
        with self._lock:
            return self._records.get(self._request_index.get(worker_request_id, ""))

    def by_disclosure(self, disclosure_id: str) -> BrowserTurnProjectionRecord | None:
        self._ensure_available()
        with self._lock:
            return self._records.get(self._disclosures.get(disclosure_id, ""))

    def history(
        self,
        canonical_session_id: str,
        *,
        browser_session_id: str = "",
        limit: int = 16,
    ) -> tuple[BrowserTurnProjectionRecord, ...]:
        self._ensure_available()
        if limit <= 0:
            return ()
        with self._lock:
            values = [
                item for item in self._records.values()
                if item.canonical_session_id == canonical_session_id
                and (not browser_session_id or item.browser_session_id == browser_session_id)
            ]
        values.sort(key=lambda item: (item.sequence, item.created_at, item.turn_id), reverse=True)
        return tuple(values[:limit])

    def previous_facts(
        self,
        canonical_session_id: str,
        *,
        browser_session_id: str = "",
        limit: int = 128,
    ) -> tuple[str, ...]:
        values: list[str] = []
        seen: set[str] = set()
        for record in self.history(
            canonical_session_id,
            browser_session_id=browser_session_id,
            limit=self.max_records_per_session,
        ):
            for fact in record.facts:
                key = normalized_fact_key(fact)
                if key and key not in seen:
                    values.append(fact)
                    seen.add(key)
                if len(values) >= limit:
                    return tuple(values)
        return tuple(values)

    def consumed_source_ids(self, canonical_session_id: str) -> tuple[str, ...]:
        return tuple(
            record.disclosure_source_id
            for record in self.history(canonical_session_id, limit=self.max_records_per_session)
            if record.read_once_consumed and record.disclosure_source_id
        )

    def mark_consumed(
        self,
        disclosure_id: str,
        *,
        expected_turn_id: str = "",
    ) -> BrowserTurnProjectionRecord:
        self._ensure_available()
        with self._lock:
            turn_id = self._disclosures.get(disclosure_id, "")
            record = self._records.get(turn_id)
            if record is None:
                raise KeyError(f"browser disclosure {disclosure_id} is not indexed")
            if expected_turn_id and expected_turn_id != turn_id:
                self._conflicts += 1
                raise ValueError("browser disclosure consumption CAS conflict")
            if record.read_once_consumed:
                return record
            updated = replace(record, read_once_consumed=True, updated_at=now_iso())
            self._records[turn_id] = updated
            self._persist_locked()
            self._writes += 1
            return updated

    def snapshot(self) -> BrowserTurnStoreSnapshot:
        with self._lock:
            payload = self._payload_locked()
            return BrowserTurnStoreSnapshot(
                records=len(self._records),
                sessions=len(self._latest),
                disclosures=len(self._disclosures),
                consumed_disclosures=sum(item.read_once_consumed for item in self._records.values()),
                writes=self._writes,
                loads=self._loads,
                conflicts=self._conflicts,
                corrupt_recoveries=self._corrupt_recoveries,
                pruned=self._pruned,
                path=str(self.path),
                digest=digest_json(payload),
            )

    def _prune_locked(self, identity_key: str) -> None:
        values = sorted(
            (item for item in self._records.values() if item.identity_key == identity_key),
            key=lambda item: (item.sequence, item.created_at, item.turn_id),
            reverse=True,
        )
        for record in values[self.max_records_per_session :]:
            if self._latest.get(identity_key) == record.turn_id:
                continue
            self._records.pop(record.turn_id, None)
            self._disclosures.pop(record.disclosure_id, None)
            self._request_index.pop(record.worker_request_id, None)
            self._pruned += 1

    def _payload_locked(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "records": {
                key: value.to_dict()
                for key, value in sorted(self._records.items())
            },
            "latest": dict(sorted(self._latest.items())),
            "disclosures": dict(sorted(self._disclosures.items())),
            "request_index": dict(sorted(self._request_index.items())),
        }

    def _persist_locked(self) -> None:
        payload = self._payload_locked()
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        temp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(encoded, encoding="utf-8")
        os.replace(temp, self.path)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping) or raw.get("schema") != self.SCHEMA:
                raise ValueError("unsupported browser turn projection index schema")
            records_raw = raw.get("records") if isinstance(raw.get("records"), Mapping) else {}
            records = {
                str(key): BrowserTurnProjectionRecord.from_dict(value)
                for key, value in records_raw.items()
                if isinstance(value, Mapping)
            }
            latest = _string_mapping(raw.get("latest"))
            disclosures = _string_mapping(raw.get("disclosures"))
            request_index = _string_mapping(raw.get("request_index"))
            if any(turn_id not in records for turn_id in latest.values()):
                raise ValueError("latest browser turn index points to a missing record")
            if any(turn_id not in records for turn_id in disclosures.values()):
                raise ValueError("browser disclosure index points to a missing record")
            if any(turn_id not in records for turn_id in request_index.values()):
                raise ValueError("browser request index points to a missing record")
        except Exception:
            self._corrupt_recoveries += 1
            corrupt = self.path.with_suffix(f"{self.path.suffix}.corrupt")
            if corrupt.exists():
                corrupt = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.corrupt")
            os.replace(self.path, corrupt)
            return
        self._records = records
        self._latest = latest
        self._disclosures = disclosures
        self._request_index = request_index
        self._loads += 1


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray, Mapping)):
        return tuple(str(item) for item in value if str(item))
    return ()


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key) and str(item)}


def _deduplicate_facts(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_page_text(value, limit=600)
        key = normalized_fact_key(normalized)
        if key and key not in seen:
            result.append(normalized)
            seen.add(key)
    return tuple(result)
