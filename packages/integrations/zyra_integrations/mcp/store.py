from __future__ import annotations

"""Atomic state custody for the Zyra MCP client runtime.

``McpRuntimeStateStore`` is the single durable owner for MCP configuration
sources, approvals, policy, connection snapshots, capability catalogs, auth
references, lifecycle queues and their bounded transition journal.  Live
transports and credentials are intentionally not owned by this store.

The store uses a process-wide re-entrant lock plus an exclusive lock file.  A
mutation always reloads the latest state while holding both locks, validates an
optional compare-and-swap revision, writes a fully fsynced temporary file and
atomically replaces the destination.  Consequently separate store instances
and separate processes cannot silently lose each other's updates.
"""

import copy
import dataclasses
import hashlib
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, overload
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from .events import (
    DEFAULT_REDACTION_POLICY,
    McpRuntimeEventKind,
    RedactionPolicy,
    canonical_event_digest,
    is_sensitive_field_name,
    make_journal_entry,
    sanitize_mcp_payload,
    sanitize_mcp_value,
)


MCP_STATE_SCHEMA = "zyra.mcp-runtime-state"
MCP_STATE_VERSION = 1
MCP_SNAPSHOT_SCHEMA = "zyra.mcp-runtime-state-snapshot"
MCP_SNAPSHOT_VERSION = 1
DEFAULT_JOURNAL_LIMIT = 2_000

StateMutator = Callable[[dict[str, Any]], Mapping[str, Any] | None]
T = TypeVar("T")


class McpStateStoreError(RuntimeError):
    pass


class McpStateConflict(McpStateStoreError):
    pass


class McpStateCorrupt(McpStateStoreError):
    pass


class McpStateSerializationError(McpStateStoreError):
    pass


class McpStateDisabled(McpStateStoreError):
    pass


class McpStateLockTimeout(McpStateStoreError):
    pass


McpRuntimeStateConflict = McpStateConflict
McpRuntimeStateCorrupt = McpStateCorrupt


@dataclass(frozen=True, slots=True)
class McpStateSnapshot:
    snapshot_id: str
    store_revision: int
    captured_at: str
    payload: Mapping[str, Any]
    checksum: str
    schema: str = MCP_SNAPSHOT_SCHEMA
    schema_version: int = MCP_SNAPSHOT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "store_revision": self.store_revision,
            "captured_at": self.captured_at,
            "payload": copy.deepcopy(dict(self.payload)),
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "McpStateSnapshot":
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise McpStateCorrupt("MCP snapshot payload must be an object")
        return cls(
            schema=str(value.get("schema") or ""),
            schema_version=int(value.get("schema_version") or 0),
            snapshot_id=str(value.get("snapshot_id") or ""),
            store_revision=int(value.get("store_revision") or 0),
            captured_at=str(value.get("captured_at") or ""),
            payload=copy.deepcopy(dict(payload)),
            checksum=str(value.get("checksum") or ""),
        )


@dataclass(frozen=True, slots=True)
class McpStateMutation:
    revision: int
    previous_revision: int
    changed_sections: tuple[str, ...]
    state_digest: str
    journal_id: str = ""
    state: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class McpJournalQuery:
    after_revision: int = -1
    before_revision: int | None = None
    kinds: tuple[str, ...] = ()
    server_id: str = ""
    source_id: str = ""
    limit: int | None = None
    newest_first: bool = False

    def __post_init__(self) -> None:
        if self.after_revision < -1:
            raise ValueError("after_revision must be -1 or greater")
        if self.before_revision is not None and self.before_revision < 0:
            raise ValueError("before_revision cannot be negative")
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit must be positive")


_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


class McpRuntimeStateStore:
    """Atomic and secret-rejecting MCP state repository."""

    _SECTION_TYPES: Mapping[str, type] = {
        "config_sources": dict,
        "approvals": dict,
        "policy": dict,
        "connections": dict,
        "catalogs": dict,
        "auth": dict,
        "elicitations": dict,
        "tasks": dict,
        "runtime_values": dict,
        "journal": list,
        "metadata": dict,
    }

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        disabled: bool = False,
        lock_timeout: float = 10.0,
        stale_lock_seconds: float = 60.0,
        journal_limit: int = DEFAULT_JOURNAL_LIMIT,
        redaction_policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
        instance_id: str | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.disabled = bool(disabled)
        self.lock_timeout = max(0.1, float(lock_timeout))
        self.stale_lock_seconds = max(self.lock_timeout, float(stale_lock_seconds))
        self.journal_limit = max(10, int(journal_limit))
        self.redaction_policy = redaction_policy
        self.instance_id = instance_id or f"mcpstore_{os.getpid()}_{uuid4().hex}"
        self._lock = _process_lock(self.path)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    @property
    def revision(self) -> int:
        return int(self.read_state()["revision"])

    def exists(self) -> bool:
        return self.path.is_file()

    def read_state(self) -> dict[str, Any]:
        self._assert_enabled()
        with self._guard():
            raw = self._load_unlocked()
            state = self._runtime_view(raw)
            self._validate_state(state, verify_checksum=False)
            return copy.deepcopy(state)

    def read(self) -> dict[str, Any]:
        return self.read_state()

    def load(
        self,
        key: str | None = None,
        default: Any = None,
        *,
        section: str | None = None,
    ) -> Any:
        if key is None:
            return self.read_state()
        return self.get(key, default, section=section)

    def raw_persisted_state(self) -> dict[str, Any]:
        """Read validated disk state without live-state restoration.

        This is primarily useful for diagnostics and integrity tests.  Runtime
        owners should use :meth:`read_state`, which applies fail-closed restore
        semantics to connection snapshots from another instance.
        """

        self._assert_enabled()
        with self._guard():
            return copy.deepcopy(self._load_unlocked())

    def mutate(
        self,
        mutator: StateMutator,
        expected_revision: int | None = None,
        *,
        actor: str = "runtime",
        reason: str = "state_mutation",
        event_kind: McpRuntimeEventKind | str = McpRuntimeEventKind.CONFIG_CHANGED,
        server_id: str = "",
        source_id: str = "",
        cause_event_id: str = "",
        journal_payload: Mapping[str, Any] | None = None,
        journal: bool = True,
    ) -> dict[str, Any]:
        self._assert_enabled()
        if not callable(mutator):
            raise TypeError("mutator must be callable")
        with self._guard():
            persisted = self._load_unlocked()
            current = self._runtime_view(persisted)
            actual_revision = int(current.get("revision") or 0)
            if expected_revision is not None and actual_revision != expected_revision:
                raise McpStateConflict(
                    f"MCP state revision conflict: expected {expected_revision}, actual {actual_revision}"
                )

            working = copy.deepcopy(current)
            before_sections = {
                key: _section_digest(working.get(key))
                for key in self._SECTION_TYPES
                if key != "journal"
            }
            replacement = mutator(working)
            if replacement is not None:
                if not isinstance(replacement, Mapping):
                    raise TypeError("MCP state mutator must return a mapping or None")
                working = copy.deepcopy(dict(replacement))

            next_revision = actual_revision + 1
            now = self._now_iso()
            working["schema"] = MCP_STATE_SCHEMA
            working["schema_version"] = MCP_STATE_VERSION
            working["revision"] = next_revision
            working.setdefault("state_id", str(current.get("state_id") or f"mcpstate_{uuid4().hex}"))
            working.setdefault("created_at", str(current.get("created_at") or now))
            working["updated_at"] = now
            working["writer_instance_id"] = self.instance_id
            self._normalize_state(working)

            changed_sections = tuple(
                key
                for key in self._SECTION_TYPES
                if key != "journal" and _section_digest(working.get(key)) != before_sections.get(key)
            )
            if journal:
                payload = {
                    "changed_sections": list(changed_sections),
                    "previous_revision": actual_revision,
                    **dict(journal_payload or {}),
                }
                entry = make_journal_entry(
                    kind=event_kind,
                    revision=next_revision,
                    actor=actor,
                    reason=reason,
                    server_id=server_id,
                    source_id=source_id,
                    cause_event_id=cause_event_id,
                    payload=payload,
                    policy=self.redaction_policy,
                )
                working["journal"].append(entry)
                if len(working["journal"]) > self.journal_limit:
                    removed = len(working["journal"]) - self.journal_limit
                    del working["journal"][:removed]
                    working["metadata"]["journal_compacted_count"] = int(
                        working["metadata"].get("journal_compacted_count") or 0
                    ) + removed

            prepared = _prepare_state_value(working, path=("state",))
            if not isinstance(prepared, dict):
                raise McpStateSerializationError("MCP state root did not serialize as an object")
            prepared["checksum"] = _state_checksum(prepared)
            self._validate_state(prepared)
            self._write_unlocked(prepared)
            return copy.deepcopy(prepared)

    def mutate_with_result(
        self,
        mutator: StateMutator,
        expected_revision: int | None = None,
        **kwargs: Any,
    ) -> McpStateMutation:
        before = self.read_state()
        state = self.mutate(mutator, expected_revision=expected_revision, **kwargs)
        changed = tuple(
            key
            for key in self._SECTION_TYPES
            if _section_digest(before.get(key)) != _section_digest(state.get(key))
        )
        journal = state.get("journal") or []
        journal_id = str(journal[-1].get("journal_id") or "") if journal else ""
        return McpStateMutation(
            revision=int(state["revision"]),
            previous_revision=int(before["revision"]),
            changed_sections=changed,
            state_digest=canonical_event_digest({key: value for key, value in state.items() if key != "checksum"}),
            journal_id=journal_id,
            state=state,
        )

    def compare_and_swap(
        self,
        expected_revision: int,
        mutator: StateMutator,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.mutate(mutator, expected_revision=expected_revision, **kwargs)

    @contextmanager
    def transaction(
        self,
        expected_revision: int | None = None,
        *,
        actor: str = "runtime",
        reason: str = "transaction",
        event_kind: McpRuntimeEventKind | str = McpRuntimeEventKind.CONFIG_CHANGED,
    ) -> Iterator[dict[str, Any]]:
        """Yield a detached transaction draft and commit it on clean exit."""

        state = self.read_state()
        revision = int(state["revision"])
        if expected_revision is not None and expected_revision != revision:
            raise McpStateConflict(
                f"MCP state revision conflict: expected {expected_revision}, actual {revision}"
            )
        draft = copy.deepcopy(state)
        yield draft

        def replace_state(current: dict[str, Any]) -> Mapping[str, Any]:
            return draft

        self.mutate(
            replace_state,
            expected_revision=revision,
            actor=actor,
            reason=reason,
            event_kind=event_kind,
        )

    def get_section(self, section: str) -> Any:
        self._assert_section(section)
        return copy.deepcopy(self.read_state()[section])

    def update_section(
        self,
        section: str,
        values: Mapping[str, Any],
        *,
        replace: bool = False,
        expected_revision: int | None = None,
        actor: str = "runtime",
        reason: str = "section_update",
        event_kind: McpRuntimeEventKind | str = McpRuntimeEventKind.CONFIG_CHANGED,
        server_id: str = "",
        source_id: str = "",
    ) -> dict[str, Any]:
        self._assert_mapping_section(section)
        if not isinstance(values, Mapping):
            raise TypeError("section values must be a mapping")

        def update(state: dict[str, Any]) -> None:
            if replace:
                state[section] = copy.deepcopy(dict(values))
            else:
                state[section].update(copy.deepcopy(dict(values)))

        return self.mutate(
            update,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
            event_kind=event_kind,
            server_id=server_id,
            source_id=source_id,
            journal_payload={"section": section, "keys": sorted(str(key) for key in values)},
        )

    def replace_section(
        self,
        section: str,
        values: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.update_section(section, values, replace=True, **kwargs)

    def get(self, key: str, default: T | None = None, *, section: str | None = None) -> Any | T | None:
        selected = section or "runtime_values"
        self._assert_mapping_section(selected)
        return copy.deepcopy(self.read_state()[selected].get(key, default))

    def set(
        self,
        key: str,
        value: Any,
        *,
        section: str | None = None,
        expected_revision: int | None = None,
        actor: str = "runtime",
        reason: str = "value_set",
    ) -> dict[str, Any]:
        if not key:
            raise ValueError("state key is required")
        selected = section or "runtime_values"
        return self.update_section(
            selected,
            {key: value},
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
        )

    def delete(
        self,
        key: str,
        *,
        section: str | None = None,
        expected_revision: int | None = None,
        actor: str = "runtime",
        reason: str = "value_deleted",
        missing_ok: bool = True,
    ) -> dict[str, Any]:
        selected = section or "runtime_values"
        self._assert_mapping_section(selected)

        def remove(state: dict[str, Any]) -> None:
            if key not in state[selected] and not missing_ok:
                raise KeyError(key)
            state[selected].pop(key, None)

        return self.mutate(
            remove,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
            journal_payload={"section": selected, "key": key},
        )

    def save(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Compatibility facade for state-aware runtime components.

        ``save(mapping)`` replaces the whole state through CAS.  ``save(key,
        value)`` writes a namespaced runtime value.  Explicit section owners
        should prefer :meth:`update_section`.
        """

        expected_revision = kwargs.pop("expected_revision", None)
        if len(args) == 1 and isinstance(args[0], Mapping):
            payload = copy.deepcopy(dict(args[0]))
            return self.mutate(
                lambda _state: payload,
                expected_revision=expected_revision,
                actor=str(kwargs.pop("actor", "runtime")),
                reason=str(kwargs.pop("reason", "state_save")),
            )
        if len(args) == 2:
            return self.set(
                str(args[0]),
                args[1],
                section=kwargs.pop("section", None),
                expected_revision=expected_revision,
                actor=str(kwargs.pop("actor", "runtime")),
                reason=str(kwargs.pop("reason", "value_save")),
            )
        raise TypeError("save expects a state mapping or key/value pair")

    def get_auth_record(self, server_id: str) -> dict[str, Any] | None:
        value = self.get(server_id, section="auth")
        return value if isinstance(value, dict) else None

    def put_auth_record(
        self,
        server_id: str,
        record: Any,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self.update_section(
            "auth",
            {server_id: record},
            expected_revision=expected_revision,
            actor="auth_runtime",
            reason="auth_reference_changed",
            event_kind=McpRuntimeEventKind.AUTH_REFRESHED,
            server_id=server_id,
        )

    def delete_auth_record(
        self,
        server_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self.delete(
            server_id,
            section="auth",
            expected_revision=expected_revision,
            actor="auth_runtime",
            reason="auth_reference_deleted",
        )

    def set_connection(
        self,
        server_id: str,
        snapshot: Any,
        *,
        expected_revision: int | None = None,
        cause_event_id: str = "",
    ) -> dict[str, Any]:
        value = _model_mapping(snapshot)
        value.setdefault("server_id", server_id)
        state_text = str(getattr(value.get("state"), "value", value.get("state", "")))
        kind = {
            "connected": McpRuntimeEventKind.CONNECTION_CONNECTED,
            "failed": McpRuntimeEventKind.CONNECTION_FAILED,
            "needs_auth": McpRuntimeEventKind.CONNECTION_NEEDS_AUTH,
            "disabled": McpRuntimeEventKind.CONNECTION_DISABLED,
            "reconnecting": McpRuntimeEventKind.CONNECTION_RECONNECTING,
            "closed": McpRuntimeEventKind.CONNECTION_DISCONNECTED,
        }.get(state_text, McpRuntimeEventKind.CONNECTION_PENDING)
        return self.update_section(
            "connections",
            {server_id: value},
            expected_revision=expected_revision,
            actor="connection_runtime",
            reason=f"connection_{state_text or 'changed'}",
            event_kind=kind,
            server_id=server_id,
        )

    def get_connection(self, server_id: str) -> dict[str, Any] | None:
        value = self.get(server_id, section="connections")
        return value if isinstance(value, dict) else None

    def remove_connection(
        self,
        server_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self.delete(
            server_id,
            section="connections",
            expected_revision=expected_revision,
            actor="connection_runtime",
            reason="connection_removed",
        )

    def append_journal(
        self,
        kind: McpRuntimeEventKind | str,
        payload: Mapping[str, Any] | None = None,
        *,
        expected_revision: int | None = None,
        actor: str = "runtime",
        reason: str = "event",
        server_id: str = "",
        source_id: str = "",
        cause_event_id: str = "",
    ) -> dict[str, Any]:
        return self.mutate(
            lambda _state: None,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
            event_kind=kind,
            server_id=server_id,
            source_id=source_id,
            cause_event_id=cause_event_id,
            journal_payload=payload,
        )

    def append_event(
        self,
        event: Any,
        *,
        expected_revision: int | None = None,
        actor: str = "runtime",
    ) -> dict[str, Any]:
        value = _model_mapping(event)
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            payload = {key: item for key, item in value.items() if key != "payload"}
        event_type = str(getattr(value.get("event_type"), "value", value.get("event_type", "mcp_event")))
        event_id = str(value.get("event_id") or "")
        return self.append_journal(
            event_type,
            {
                "event_id": event_id,
                "run_id": str(value.get("run_id") or ""),
                "task_id": str(value.get("task_id") or ""),
                "node_id": str(value.get("node_id") or ""),
                "event_payload": sanitize_mcp_payload(payload, policy=self.redaction_policy),
            },
            expected_revision=expected_revision,
            actor=actor,
            reason="projected_event",
            cause_event_id=event_id,
        )

    publish = append_event

    def query_journal(self, query: McpJournalQuery | None = None) -> list[dict[str, Any]]:
        selected = query or McpJournalQuery()
        values = self.read_state()["journal"]
        if selected.newest_first:
            values = list(reversed(values))
        output: list[dict[str, Any]] = []
        allowed_kinds = set(selected.kinds)
        for item in values:
            revision = int(item.get("revision") or 0)
            if revision <= selected.after_revision:
                continue
            if selected.before_revision is not None and revision >= selected.before_revision:
                continue
            if allowed_kinds and str(item.get("kind") or "") not in allowed_kinds:
                continue
            if selected.server_id and str(item.get("server_id") or "") != selected.server_id:
                continue
            if selected.source_id and str(item.get("source_id") or "") != selected.source_id:
                continue
            output.append(copy.deepcopy(item))
            if selected.limit is not None and len(output) >= selected.limit:
                break
        return output

    def iter_journal(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        yield from self.query_journal(McpJournalQuery(**kwargs))

    def compact_journal(
        self,
        *,
        keep_last: int = 500,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if keep_last < 0:
            raise ValueError("keep_last cannot be negative")

        def compact(state: dict[str, Any]) -> None:
            removed = max(0, len(state["journal"]) - keep_last)
            if keep_last:
                state["journal"] = state["journal"][-keep_last:]
            else:
                state["journal"] = []
            state["metadata"]["journal_compacted_count"] = int(
                state["metadata"].get("journal_compacted_count") or 0
            ) + removed

        return self.mutate(
            compact,
            expected_revision=expected_revision,
            actor="state_store",
            reason="journal_compaction",
            event_kind=McpRuntimeEventKind.STATE_COMPACTED,
            journal_payload={"keep_last": keep_last},
        )

    def snapshot(self) -> dict[str, Any]:
        state = self.read_state()
        payload = copy.deepcopy(state)
        checksum = _snapshot_checksum_payload(
            {
                "schema": MCP_SNAPSHOT_SCHEMA,
                "schema_version": MCP_SNAPSHOT_VERSION,
                "snapshot_id": "pending",
                "store_revision": int(state["revision"]),
                "captured_at": "pending",
                "payload": payload,
            },
            identity_agnostic=True,
        )
        snapshot = McpStateSnapshot(
            snapshot_id=f"mcpsnapshot_{uuid4().hex}",
            store_revision=int(state["revision"]),
            captured_at=self._now_iso(),
            payload=payload,
            checksum=checksum,
        )
        # The digest is identity-agnostic so equivalent snapshots can be
        # compared across sessions without trusting caller-selected IDs.
        return snapshot.to_dict()

    def restore_snapshot(
        self,
        snapshot: Mapping[str, Any] | McpStateSnapshot,
        *,
        expected_revision: int | None = None,
        preserve_newer_journal: bool = True,
    ) -> dict[str, Any]:
        value = snapshot if isinstance(snapshot, McpStateSnapshot) else McpStateSnapshot.from_dict(snapshot)
        if value.schema != MCP_SNAPSHOT_SCHEMA or value.schema_version != MCP_SNAPSHOT_VERSION:
            raise McpStateCorrupt("unsupported MCP state snapshot schema")
        expected_checksum = _snapshot_checksum_payload(
            {
                "schema": value.schema,
                "schema_version": value.schema_version,
                "snapshot_id": "pending",
                "store_revision": value.store_revision,
                "captured_at": "pending",
                "payload": value.payload,
            },
            identity_agnostic=True,
        )
        if not _constant_time_equal(value.checksum, expected_checksum):
            raise McpStateCorrupt("MCP state snapshot checksum mismatch")
        restored = self._runtime_view(copy.deepcopy(dict(value.payload)), force_restore=True)
        _prepare_state_value(restored, path=("snapshot",))

        def apply(state: dict[str, Any]) -> Mapping[str, Any]:
            journal = copy.deepcopy(state["journal"]) if preserve_newer_journal else []
            next_state = copy.deepcopy(restored)
            if preserve_newer_journal:
                known = {str(item.get("journal_id") or "") for item in next_state.get("journal", [])}
                next_state["journal"] = [
                    *next_state.get("journal", []),
                    *(item for item in journal if str(item.get("journal_id") or "") not in known),
                ]
            next_state.setdefault("metadata", {})["restored_snapshot_id"] = value.snapshot_id
            next_state["metadata"]["restored_snapshot_revision"] = value.store_revision
            return next_state

        return self.mutate(
            apply,
            expected_revision=expected_revision,
            actor="state_store",
            reason="snapshot_restore",
            event_kind=McpRuntimeEventKind.STATE_RESTORED,
            journal_payload={
                "snapshot_id": value.snapshot_id,
                "snapshot_revision": value.store_revision,
            },
        )

    def verify_integrity(self) -> dict[str, Any]:
        with self._guard():
            state = self._load_unlocked()
            self._validate_state(state)
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "revision": int(state["revision"]),
            "checksum": str(state.get("checksum") or ""),
            "journal_entries": len(state["journal"]),
            "valid": True,
        }

    def _assert_enabled(self) -> None:
        if self.disabled:
            raise McpStateDisabled("MCP runtime state store is disabled")

    def _assert_section(self, section: str) -> None:
        if section not in self._SECTION_TYPES:
            raise KeyError(f"unknown MCP state section: {section}")

    def _assert_mapping_section(self, section: str) -> None:
        self._assert_section(section)
        if self._SECTION_TYPES[section] is not dict:
            raise TypeError(f"MCP state section {section} is not a mapping")

    @contextmanager
    def _guard(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.lock_path
            started = time.monotonic()
            descriptor: int | None = None
            while descriptor is None:
                try:
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    metadata = {
                        "pid": os.getpid(),
                        "thread": threading.get_ident(),
                        "created_at_epoch": time.time(),
                        "instance_id": self.instance_id,
                    }
                    os.write(descriptor, json.dumps(metadata, sort_keys=True).encode("ascii"))
                    os.fsync(descriptor)
                except FileExistsError:
                    if _lock_is_stale(lock_path, self.stale_lock_seconds):
                        try:
                            lock_path.unlink()
                        except FileNotFoundError:
                            pass
                        continue
                    if time.monotonic() - started >= self.lock_timeout:
                        raise McpStateLockTimeout(f"timed out acquiring MCP state lock: {lock_path}")
                    time.sleep(0.01)
                except OSError as error:
                    raise McpStateStoreError(f"cannot acquire MCP state lock {lock_path}: {error}") from error
            try:
                yield
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_state()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise McpStateCorrupt(f"cannot read MCP runtime state: {error}") from error
        if not isinstance(value, dict):
            raise McpStateCorrupt("MCP runtime state root must be an object")
        state = self._migrate(value)
        self._validate_state(state)
        return state

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
        )
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    state,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            _fsync_directory(self.path.parent)
        except (OSError, TypeError, ValueError) as error:
            raise McpStateStoreError(f"cannot atomically write MCP runtime state: {error}") from error
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _empty_state(self) -> dict[str, Any]:
        now = self._now_iso()
        state = {
            "schema": MCP_STATE_SCHEMA,
            "schema_version": MCP_STATE_VERSION,
            "state_id": f"mcpstate_{uuid4().hex}",
            "revision": 0,
            "created_at": now,
            "updated_at": now,
            "writer_instance_id": self.instance_id,
            "config_sources": {},
            "approvals": {},
            "policy": {
                "enterprise_exclusive": True,
                "disabled_servers": [],
                "allowed_mcp_servers": None,
                "denied_mcp_servers": [],
                "allowed_tools": {},
                "denied_tools": {},
            },
            "connections": {},
            "catalogs": {},
            "auth": {},
            "elicitations": {},
            "tasks": {},
            "runtime_values": {},
            "journal": [],
            "metadata": {},
        }
        state["checksum"] = _state_checksum(state)
        return state

    def _migrate(self, value: dict[str, Any]) -> dict[str, Any]:
        schema = str(value.get("schema") or "")
        version = int(value.get("schema_version") or 0)
        if schema == MCP_STATE_SCHEMA:
            if version > MCP_STATE_VERSION:
                raise McpStateCorrupt(
                    f"MCP runtime state version {version} is newer than supported {MCP_STATE_VERSION}"
                )
            state = copy.deepcopy(value)
        elif not schema and any(key in value for key in self._SECTION_TYPES):
            state = self._empty_state()
            for key in self._SECTION_TYPES:
                if key in value:
                    state[key] = copy.deepcopy(value[key])
            state["revision"] = max(0, int(value.get("revision") or 0))
            state["metadata"]["migrated_from"] = "unversioned_mcp_state"
            state["metadata"]["migrated_payload_digest"] = canonical_event_digest(value)
        else:
            raise McpStateCorrupt("unsupported MCP runtime state schema")
        self._normalize_state(state)
        if version < MCP_STATE_VERSION:
            state["schema_version"] = MCP_STATE_VERSION
        return state

    def _normalize_state(self, state: MutableMapping[str, Any]) -> None:
        now = self._now_iso()
        state.setdefault("schema", MCP_STATE_SCHEMA)
        state.setdefault("schema_version", MCP_STATE_VERSION)
        state.setdefault("state_id", f"mcpstate_{uuid4().hex}")
        state.setdefault("revision", 0)
        state.setdefault("created_at", now)
        state.setdefault("updated_at", state["created_at"])
        state.setdefault("writer_instance_id", "")
        for section, expected_type in self._SECTION_TYPES.items():
            state.setdefault(section, expected_type())
        policy = state["policy"]
        if isinstance(policy, MutableMapping):
            policy.setdefault("enterprise_exclusive", True)
            policy.setdefault("disabled_servers", [])
            policy.setdefault("allowed_mcp_servers", None)
            policy.setdefault("denied_mcp_servers", [])
            policy.setdefault("allowed_tools", {})
            policy.setdefault("denied_tools", {})

    def _runtime_view(self, state: dict[str, Any], *, force_restore: bool = False) -> dict[str, Any]:
        view = copy.deepcopy(state)
        writer = str(view.get("writer_instance_id") or "")
        if force_restore or (writer and writer != self.instance_id):
            transitioned: list[str] = []
            for key, value in view.get("connections", {}).items():
                if not isinstance(value, MutableMapping):
                    continue
                state_text = str(getattr(value.get("state"), "value", value.get("state", "")))
                if state_text == "connected":
                    value["state"] = "reconnecting"
                    value["healthy"] = False
                    value["restore_reason"] = "live_transport_not_restorable"
                    value["restored_from_writer_instance_id"] = writer
                    value["last_transition_at"] = self._now_iso()
                    transitioned.append(str(key))
            if transitioned:
                view.setdefault("metadata", {})["restore_reconnecting_servers"] = sorted(transitioned)
                view["metadata"]["restored_by_instance_id"] = self.instance_id
        return view

    def _validate_state(self, state: Mapping[str, Any], *, verify_checksum: bool = True) -> None:
        if state.get("schema") != MCP_STATE_SCHEMA:
            raise McpStateCorrupt("MCP runtime state schema mismatch")
        if int(state.get("schema_version") or 0) != MCP_STATE_VERSION:
            raise McpStateCorrupt("MCP runtime state version mismatch")
        if not str(state.get("state_id") or ""):
            raise McpStateCorrupt("MCP runtime state id is required")
        if int(state.get("revision") or 0) < 0:
            raise McpStateCorrupt("MCP runtime state revision cannot be negative")
        for section, expected_type in self._SECTION_TYPES.items():
            if not isinstance(state.get(section), expected_type):
                raise McpStateCorrupt(f"MCP runtime state section {section} has invalid type")
        if verify_checksum:
            checksum = str(state.get("checksum") or "")
            if not checksum or not _constant_time_equal(checksum, _state_checksum(state)):
                raise McpStateCorrupt("MCP runtime state checksum mismatch")
        previous_revision = -1
        journal_ids: set[str] = set()
        for item in state["journal"]:
            if not isinstance(item, Mapping):
                raise McpStateCorrupt("MCP journal entry must be an object")
            revision = int(item.get("revision") or 0)
            if revision < previous_revision:
                raise McpStateCorrupt("MCP journal revisions are not monotonic")
            previous_revision = revision
            journal_id = str(item.get("journal_id") or "")
            if not journal_id or journal_id in journal_ids:
                raise McpStateCorrupt("MCP journal entry id is missing or duplicated")
            journal_ids.add(journal_id)
        try:
            _prepare_state_value(state, path=("state",))
        except McpStateSerializationError as error:
            raise McpStateCorrupt(f"MCP state contains forbidden secret material: {error}") from error

    def _now_iso(self) -> str:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MCP state clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _prepare_state_value(value: Any, *, path: tuple[str, ...]) -> Any:
    """Convert state to JSON while rejecting secret-bearing material."""

    if value is None or isinstance(value, bool | int | str):
        if isinstance(value, str):
            return _safe_state_string(value, path=path)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise McpStateSerializationError(f"non-finite number at {_path(path)}")
        return value
    if isinstance(value, Enum):
        return _prepare_state_value(value.value, path=path)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        selected = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return selected.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, bytes | bytearray | memoryview):
        raise McpStateSerializationError(
            f"binary data at {_path(path)} must be externalized to an artifact before persistence"
        )
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        safe_dict = getattr(value, "safe_dict", None)
        if callable(safe_dict):
            return _prepare_state_value(safe_dict(), path=path)
        value = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    elif not isinstance(value, Mapping | Sequence):
        safe_dict = getattr(value, "safe_dict", None)
        to_dict = getattr(value, "to_dict", None)
        if callable(safe_dict):
            value = safe_dict()
        elif callable(to_dict):
            value = to_dict()
        else:
            raise McpStateSerializationError(
                f"unsupported state value {type(value).__name__} at {_path(path)}"
            )
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise McpStateSerializationError(f"non-string object key at {_path(path)}")
            key = raw_key
            child = (*path, key)
            if is_sensitive_field_name(key) and not _safe_reference_field(key):
                if _is_config_template_location(path) and isinstance(item, str) and _is_secret_template(item):
                    output[key] = item
                    continue
                if _is_redacted_value(item):
                    output[key] = copy.deepcopy(item)
                    continue
                raise McpStateSerializationError(
                    f"raw secret field {_path(child)} is forbidden; persist an opaque credential reference"
                )
            if _is_environment_key(key) and isinstance(item, Mapping):
                output[key] = _prepare_environment(item, path=child)
                continue
            if _is_headers_key(key) and isinstance(item, Mapping):
                output[key] = _prepare_headers(item, path=child)
                continue
            output[key] = _prepare_state_value(item, path=child)
        return output
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray | memoryview):
        return [
            _prepare_state_value(item, path=(*path, str(index)))
            for index, item in enumerate(value)
        ]
    raise McpStateSerializationError(f"unsupported state value at {_path(path)}")


def _prepare_environment(value: Mapping[Any, Any], *, path: tuple[str, ...]) -> dict[str, str]:
    output: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise McpStateSerializationError(f"environment entries at {_path(path)} must be strings")
        if is_sensitive_field_name(raw_key) and not _is_secret_template(raw_value):
            raise McpStateSerializationError(
                f"sensitive environment value {_path((*path, raw_key))} must use an environment template"
            )
        output[raw_key] = _safe_state_string(raw_value, path=(*path, raw_key))
    return output


def _prepare_headers(value: Mapping[Any, Any], *, path: tuple[str, ...]) -> dict[str, str]:
    output: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise McpStateSerializationError(f"header entries at {_path(path)} must be strings")
        if is_sensitive_field_name(raw_key) and not _is_secret_template(raw_value):
            raise McpStateSerializationError(
                f"sensitive header {_path((*path, raw_key))} must use an environment template"
            )
        output[raw_key] = _safe_state_string(raw_value, path=(*path, raw_key))
    return output


_SECRET_CLI = re.compile(
    r"(?i)(?:--?(?:access[-_]?token|refresh[-_]?token|password|passwd|secret|api[-_]?key)|"
    r"(?:authorization|cookie))\s*(?:=|:)\s*([^\s]+)"
)
_ENV_TEMPLATE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")
_PEM_MARKER = re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----")


def _safe_state_string(value: str, *, path: tuple[str, ...]) -> str:
    if _PEM_MARKER.search(value):
        raise McpStateSerializationError(f"PEM secret material is forbidden at {_path(path)}")
    match = _SECRET_CLI.search(value)
    if match and not _is_secret_template(match.group(1)):
        raise McpStateSerializationError(f"inline secret argument is forbidden at {_path(path)}")
    if value.startswith(("http://", "https://", "ws://", "wss://")):
        _validate_secret_free_url(value, path=path)
    # Error messages can contain an auth scheme even when their key is benign.
    # The event sanitizer removes those values without changing config templates.
    if not _is_config_template_location(path):
        projected = sanitize_mcp_value({"value": value})
        if isinstance(projected, Mapping) and isinstance(projected.get("value"), str):
            return str(projected["value"])
    return value


def _validate_secret_free_url(value: str, *, path: tuple[str, ...]) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise McpStateSerializationError(f"invalid URL at {_path(path)}: {error}") from error
    if parsed.username is not None or parsed.password is not None:
        raise McpStateSerializationError(f"URL userinfo is forbidden at {_path(path)}")
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if (
            is_sensitive_field_name(key)
            and item
            and not _is_secret_template(item)
            and not _is_redacted_value(item)
        ):
            raise McpStateSerializationError(
                f"sensitive URL query value {key!r} at {_path(path)} must use an environment template"
            )


def _safe_reference_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized.endswith(
        (
            "_ref",
            "_reference",
            "_digest",
            "_hash",
            "_id",
            "_present",
            "_expires_at",
            "_issued_at",
            "_status",
            "_state",
            "_scope",
            "_type",
        )
    )


def _is_secret_template(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    # A literal prefix such as "Bearer " is safe only when all variable data is
    # supplied by one or more unresolved environment references.
    without_templates = _ENV_TEMPLATE.sub("", stripped)
    return bool(_ENV_TEMPLATE.search(stripped)) and without_templates.casefold().strip() in {
        "",
        "bearer",
        "basic",
        "token",
    }


def _is_redacted_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.casefold() in {"[redacted]", "<redacted>", "[omitted]", "<omitted>"}
    if isinstance(value, Mapping):
        return value.get("redacted") is True and set(value).issubset({"redacted", "sha256"})
    return False


def _is_config_template_location(path: tuple[str, ...]) -> bool:
    return "config_sources" in path or "config" in path


def _is_environment_key(key: str) -> bool:
    return key.casefold().replace("-", "_") in {"env", "environment", "environment_variables"}


def _is_headers_key(key: str) -> bool:
    return key.casefold().replace("-", "_") in {"headers", "http_headers", "request_headers"}


def _state_checksum(state: Mapping[str, Any]) -> str:
    payload = {key: copy.deepcopy(value) for key, value in state.items() if key != "checksum"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _snapshot_checksum_payload(value: Mapping[str, Any], *, identity_agnostic: bool) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop("checksum", None)
    if identity_agnostic:
        payload["snapshot_id"] = "pending"
        payload["captured_at"] = "pending"
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _section_digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()


def _model_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    safe_dict = getattr(value, "safe_dict", None)
    if callable(safe_dict):
        candidate = safe_dict()
        if isinstance(candidate, Mapping):
            return copy.deepcopy(dict(candidate))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        candidate = to_dict()
        if isinstance(candidate, Mapping):
            return copy.deepcopy(dict(candidate))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: copy.deepcopy(getattr(value, field.name)) for field in dataclasses.fields(value)}
    raise TypeError(f"expected mapping-like MCP record, got {type(value).__name__}")


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _lock_is_stale(path: Path, stale_seconds: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime > stale_seconds
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    descriptor: int | None = None
    try:
        descriptor = os.open(str(path), flags)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _path(parts: tuple[str, ...]) -> str:
    return ".".join(parts) if parts else "$"


__all__ = [
    "DEFAULT_JOURNAL_LIMIT",
    "MCP_SNAPSHOT_SCHEMA",
    "MCP_SNAPSHOT_VERSION",
    "MCP_STATE_SCHEMA",
    "MCP_STATE_VERSION",
    "McpJournalQuery",
    "McpRuntimeStateConflict",
    "McpRuntimeStateCorrupt",
    "McpRuntimeStateStore",
    "McpStateConflict",
    "McpStateCorrupt",
    "McpStateDisabled",
    "McpStateLockTimeout",
    "McpStateMutation",
    "McpStateSerializationError",
    "McpStateSnapshot",
    "McpStateStoreError",
]
