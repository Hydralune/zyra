from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .errors import BrowserSessionNotFound, BrowserStateConflict, BrowserStateCorrupt, BrowserStateError
from .models import BrowserSessionRef, browser_now, stable_digest


STATE_SCHEMA = "zyra.browser-session-state.v1"
T = TypeVar("T")


class BrowserStatePort(Protocol):
    def get_session(self, session_id: str) -> BrowserSessionRef | None: ...
    def find_session(self, *, run_id: str, task_id: str, canonical_session_id: str) -> BrowserSessionRef | None: ...
    def list_sessions(self, *, task_id: str = "") -> tuple[BrowserSessionRef, ...]: ...
    def create_session(self, session: BrowserSessionRef, *, request_fingerprint: str) -> BrowserSessionRef: ...
    def update_session(self, session: BrowserSessionRef, *, expected_revision: int) -> BrowserSessionRef: ...
    def append_event(self, session_id: str, event: Mapping[str, Any]) -> None: ...
    def list_events(self, session_id: str, *, limit: int = 100) -> tuple[dict[str, Any], ...]: ...
    def record_request(self, request_id: str, fingerprint: str, result: Mapping[str, Any]) -> None: ...
    def request_result(self, request_id: str, fingerprint: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class StateMutation:
    revision_before: int
    revision_after: int
    checksum_before: str
    checksum_after: str
    changed_paths: tuple[str, ...]
    committed_at: str = field(default_factory=browser_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "checksum_before": self.checksum_before,
            "checksum_after": self.checksum_after,
            "changed_paths": list(self.changed_paths),
            "committed_at": self.committed_at,
        }


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    revision: int
    checksum: str
    sessions: int
    events: int
    requests: int
    leases: int
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "checksum": self.checksum,
            "sessions": self.sessions,
            "events": self.events,
            "requests": self.requests,
            "leases": self.leases,
            "updated_at": self.updated_at,
        }


def _empty_state() -> dict[str, Any]:
    now = browser_now()
    return {
        "schema": STATE_SCHEMA,
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "sessions": {},
        "identity_index": {},
        "events": {},
        "requests": {},
        "leases": {},
        "profiles": {},
        "artifacts": {},
        "metadata": {},
    }


def _session_from_dict(value: Mapping[str, Any]) -> BrowserSessionRef:
    return BrowserSessionRef(
        session_id=str(value.get("session_id") or ""),
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        canonical_session_id=str(value.get("canonical_session_id") or ""),
        worker_request_id=str(value.get("worker_request_id") or ""),
        status=str(value.get("status") or "created"),
        revision=int(value.get("revision") or 0),
        profile_id=str(value.get("profile_id") or ""),
        active_target_id=str(value.get("active_target_id") or ""),
        process_id=int(value["process_id"]) if value.get("process_id") is not None else None,
        endpoint_url=str(value.get("endpoint_url") or ""),
        keep_alive=bool(value.get("keep_alive")),
        created_at=str(value.get("created_at") or ""),
        updated_at=str(value.get("updated_at") or ""),
    )


def _identity_key(run_id: str, task_id: str, canonical_session_id: str) -> str:
    return stable_digest({"run_id": run_id, "task_id": task_id, "canonical_session_id": canonical_session_id})


def _checksum(state: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in state.items() if key != "checksum"}
    return stable_digest(payload)


def _validate_state(state: Mapping[str, Any]) -> None:
    if state.get("schema") != STATE_SCHEMA:
        raise BrowserStateCorrupt("browser state schema mismatch")
    if not isinstance(state.get("revision"), int) or int(state["revision"]) < 0:
        raise BrowserStateCorrupt("browser state revision is invalid")
    for key in ("sessions", "identity_index", "events", "requests", "leases", "profiles", "artifacts", "metadata"):
        if not isinstance(state.get(key), dict):
            raise BrowserStateCorrupt(f"browser state section {key} is invalid")
    sessions = state["sessions"]
    identity_index = state["identity_index"]
    for session_id, value in sessions.items():
        if not isinstance(value, Mapping):
            raise BrowserStateCorrupt(f"session {session_id} is invalid")
        session = _session_from_dict(value)
        if session.session_id != session_id:
            raise BrowserStateCorrupt(f"session key mismatch for {session_id}")
        key = _identity_key(session.run_id, session.task_id, session.canonical_session_id)
        if identity_index.get(key) != session_id:
            raise BrowserStateCorrupt(f"identity index mismatch for {session_id}")
    for key, session_id in identity_index.items():
        if session_id not in sessions:
            raise BrowserStateCorrupt(f"identity index {key} points to a missing session")


class JsonBrowserStateStore:
    def __init__(self, root: str | Path, *, disabled: bool = False, event_limit: int = 2048) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "browser-sessions.json"
        self.backup_path = self.root / "browser-sessions.backup.json"
        self.journal_path = self.root / "browser-sessions.journal.jsonl"
        self.disabled = disabled
        self.event_limit = max(32, event_limit)
        self._lock = threading.RLock()
        self._state: dict[str, Any] | None = None
        if not disabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserStateError("browser state store is disabled", code="browser_state_store_disabled")

    def _load(self) -> dict[str, Any]:
        self._ensure_available()
        if self._state is not None:
            return self._state
        if not self.path.exists():
            self._state = _empty_state()
            self._persist(self._state)
            return self._state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            _validate_state(state)
        except (OSError, json.JSONDecodeError, BrowserStateCorrupt) as error:
            state = self._recover_from_backup(error)
        self._state = state
        return state

    def _recover_from_backup(self, original_error: BaseException) -> dict[str, Any]:
        if not self.backup_path.exists():
            raise BrowserStateCorrupt(f"browser state cannot be loaded: {original_error}") from original_error
        try:
            state = json.loads(self.backup_path.read_text(encoding="utf-8"))
            _validate_state(state)
        except (OSError, json.JSONDecodeError, BrowserStateCorrupt) as backup_error:
            raise BrowserStateCorrupt(
                f"browser state and backup are corrupt: {original_error}; {backup_error}"
            ) from backup_error
        self._write_atomic(self.path, state)
        return state

    def _write_atomic(self, path: Path, state: Mapping[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        with temp.open("w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    def _persist(self, state: dict[str, Any], mutation: StateMutation | None = None) -> None:
        state["updated_at"] = browser_now()
        state["checksum"] = _checksum(state)
        if self.path.exists():
            try:
                previous = json.loads(self.path.read_text(encoding="utf-8"))
                _validate_state(previous)
                self._write_atomic(self.backup_path, previous)
            except (OSError, json.JSONDecodeError, BrowserStateCorrupt):
                pass
        self._write_atomic(self.path, state)
        if mutation is not None:
            with self.journal_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(mutation.to_dict(), sort_keys=True))
                stream.write("\n")

    def _mutate(self, mutator: Callable[[dict[str, Any]], T], *, changed_paths: tuple[str, ...]) -> T:
        with self._lock:
            current = self._load()
            before = deepcopy(current)
            revision_before = int(before["revision"])
            checksum_before = _checksum(before)
            candidate = deepcopy(current)
            result = mutator(candidate)
            candidate["revision"] = revision_before + 1
            _validate_state(candidate)
            mutation = StateMutation(
                revision_before=revision_before,
                revision_after=revision_before + 1,
                checksum_before=checksum_before,
                checksum_after=_checksum(candidate),
                changed_paths=changed_paths,
            )
            self._persist(candidate, mutation)
            self._state = candidate
            return result

    @contextmanager
    def transaction(self):
        self._ensure_available()
        with self._lock:
            yield self

    def get_session(self, session_id: str) -> BrowserSessionRef | None:
        with self._lock:
            value = self._load()["sessions"].get(session_id)
            return _session_from_dict(value) if isinstance(value, Mapping) else None

    def require_session(self, session_id: str) -> BrowserSessionRef:
        session = self.get_session(session_id)
        if session is None:
            raise BrowserSessionNotFound(f"browser session {session_id} does not exist", session_id=session_id)
        return session

    def find_session(self, *, run_id: str, task_id: str, canonical_session_id: str) -> BrowserSessionRef | None:
        key = _identity_key(run_id, task_id, canonical_session_id)
        with self._lock:
            state = self._load()
            session_id = state["identity_index"].get(key)
            value = state["sessions"].get(session_id) if session_id else None
            return _session_from_dict(value) if isinstance(value, Mapping) else None

    def list_sessions(self, *, task_id: str = "") -> tuple[BrowserSessionRef, ...]:
        with self._lock:
            values = tuple(_session_from_dict(item) for item in self._load()["sessions"].values())
        if task_id:
            values = tuple(item for item in values if item.task_id == task_id)
        return tuple(sorted(values, key=lambda item: (item.created_at, item.session_id)))

    def create_session(self, session: BrowserSessionRef, *, request_fingerprint: str) -> BrowserSessionRef:
        def mutate(state: dict[str, Any]) -> BrowserSessionRef:
            if session.session_id in state["sessions"]:
                existing = _session_from_dict(state["sessions"][session.session_id])
                if existing.to_dict() != session.to_dict():
                    raise BrowserStateConflict("session identifier already exists", session_id=session.session_id)
                return existing
            key = _identity_key(session.run_id, session.task_id, session.canonical_session_id)
            existing_id = state["identity_index"].get(key)
            if existing_id:
                return _session_from_dict(state["sessions"][existing_id])
            state["sessions"][session.session_id] = session.to_dict()
            state["identity_index"][key] = session.session_id
            state["events"][session.session_id] = []
            state["metadata"].setdefault("request_fingerprints", {})[session.session_id] = request_fingerprint
            return session
        return self._mutate(mutate, changed_paths=(f"sessions.{session.session_id}", "identity_index"))

    def update_session(self, session: BrowserSessionRef, *, expected_revision: int) -> BrowserSessionRef:
        def mutate(state: dict[str, Any]) -> BrowserSessionRef:
            value = state["sessions"].get(session.session_id)
            if not isinstance(value, Mapping):
                raise BrowserSessionNotFound("browser session does not exist", session_id=session.session_id)
            current = _session_from_dict(value)
            if current.revision != expected_revision:
                raise BrowserStateConflict(
                    f"session revision conflict: expected {expected_revision}, found {current.revision}",
                    session_id=session.session_id,
                )
            if session.revision <= current.revision:
                raise BrowserStateConflict("session revision must advance", session_id=session.session_id)
            state["sessions"][session.session_id] = session.to_dict()
            return session
        return self._mutate(mutate, changed_paths=(f"sessions.{session.session_id}",))

    def append_event(self, session_id: str, event: Mapping[str, Any]) -> None:
        event_value = dict(event)
        if not event_value.get("event_id"):
            raise BrowserStateError("browser event id is required")
        def mutate(state: dict[str, Any]) -> None:
            if session_id not in state["sessions"]:
                raise BrowserSessionNotFound("browser session does not exist", session_id=session_id)
            events = state["events"].setdefault(session_id, [])
            if any(item.get("event_id") == event_value["event_id"] for item in events):
                return
            events.append(event_value)
            del events[:-self.event_limit]
        self._mutate(mutate, changed_paths=(f"events.{session_id}",))

    def list_events(self, session_id: str, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        with self._lock:
            events = self._load()["events"].get(session_id, [])
            return tuple(deepcopy(events[-max(0, limit):]))

    def record_request(self, request_id: str, fingerprint: str, result: Mapping[str, Any]) -> None:
        if not request_id or not fingerprint:
            raise BrowserStateError("request id and fingerprint are required")
        def mutate(state: dict[str, Any]) -> None:
            current = state["requests"].get(request_id)
            if current and current.get("fingerprint") != fingerprint:
                raise BrowserStateConflict("request identifier reused with another fingerprint")
            state["requests"][request_id] = {
                "fingerprint": fingerprint,
                "result": dict(result),
                "recorded_at": browser_now(),
            }
            if len(state["requests"]) > 4096:
                oldest = sorted(state["requests"], key=lambda key: state["requests"][key]["recorded_at"])[:512]
                for key in oldest:
                    state["requests"].pop(key, None)
        self._mutate(mutate, changed_paths=(f"requests.{request_id}",))

    def request_result(self, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._load()["requests"].get(request_id)
            if not isinstance(value, Mapping):
                return None
            if value.get("fingerprint") != fingerprint:
                raise BrowserStateConflict("request fingerprint does not match committed result")
            return deepcopy(dict(value.get("result") or {}))

    def acquire_lease(self, session_id: str, owner: str, *, generation: int) -> bool:
        def mutate(state: dict[str, Any]) -> bool:
            current = state["leases"].get(session_id)
            if current and current.get("owner") != owner and int(current.get("generation") or 0) >= generation:
                return False
            state["leases"][session_id] = {
                "owner": owner,
                "generation": generation,
                "acquired_at": browser_now(),
            }
            return True
        return self._mutate(mutate, changed_paths=(f"leases.{session_id}",))

    def release_lease(self, session_id: str, owner: str) -> bool:
        def mutate(state: dict[str, Any]) -> bool:
            current = state["leases"].get(session_id)
            if not current or current.get("owner") != owner:
                return False
            state["leases"].pop(session_id, None)
            return True
        return self._mutate(mutate, changed_paths=(f"leases.{session_id}",))

    def put_profile(self, profile_id: str, value: Mapping[str, Any]) -> None:
        self._mutate(
            lambda state: state["profiles"].__setitem__(profile_id, dict(value)),
            changed_paths=(f"profiles.{profile_id}",),
        )

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._load()["profiles"].get(profile_id)
            return deepcopy(dict(value)) if isinstance(value, Mapping) else None

    def put_artifact(self, artifact_id: str, value: Mapping[str, Any]) -> None:
        self._mutate(
            lambda state: state["artifacts"].__setitem__(artifact_id, dict(value)),
            changed_paths=(f"artifacts.{artifact_id}",),
        )

    def snapshot(self) -> StateSnapshot:
        with self._lock:
            state = self._load()
            return StateSnapshot(
                revision=int(state["revision"]),
                checksum=_checksum(state),
                sessions=len(state["sessions"]),
                events=sum(len(items) for items in state["events"].values()),
                requests=len(state["requests"]),
                leases=len(state["leases"]),
                updated_at=str(state["updated_at"]),
            )

    def reload(self) -> StateSnapshot:
        with self._lock:
            self._state = None
            self._load()
            return self.snapshot()

    def compact(self) -> StateSnapshot:
        def mutate(state: dict[str, Any]) -> None:
            active = set(state["sessions"])
            state["events"] = {key: value[-self.event_limit:] for key, value in state["events"].items() if key in active}
            state["leases"] = {key: value for key, value in state["leases"].items() if key in active}
            state["profiles"] = {
                key: value for key, value in state["profiles"].items()
                if not value.get("session_id") or value.get("session_id") in active
            }
        self._mutate(mutate, changed_paths=("events", "leases", "profiles"))
        return self.snapshot()

    def audit(self) -> tuple[str, ...]:
        findings: list[str] = []
        with self._lock:
            state = deepcopy(self._load())
        try:
            _validate_state(state)
        except BrowserStateCorrupt as error:
            findings.append(str(error))
        expected = _checksum(state)
        recorded = str(state.get("checksum") or "")
        if recorded and recorded != expected:
            findings.append("state checksum does not match content")
        for session_id, events in state["events"].items():
            seen: set[str] = set()
            previous_generation = -1
            for event in events:
                event_id = str(event.get("event_id") or "")
                if not event_id:
                    findings.append(f"session {session_id} contains event without id")
                elif event_id in seen:
                    findings.append(f"session {session_id} contains duplicate event {event_id}")
                seen.add(event_id)
                generation = int(event.get("generation") or 0)
                if generation < previous_generation:
                    findings.append(f"session {session_id} event generation regressed")
                previous_generation = max(previous_generation, generation)
        for request_id, value in state["requests"].items():
            if not value.get("fingerprint"):
                findings.append(f"request {request_id} has no fingerprint")
            if not isinstance(value.get("result"), Mapping):
                findings.append(f"request {request_id} has invalid result")
        for session_id, lease in state["leases"].items():
            if session_id not in state["sessions"]:
                findings.append(f"lease points to missing session {session_id}")
            if not lease.get("owner"):
                findings.append(f"lease for {session_id} has no owner")
        return tuple(findings)

    def export_state(self) -> dict[str, Any]:
        with self._lock:
            state = deepcopy(self._load())
        state["exported_at"] = browser_now()
        state["export_checksum"] = _checksum(state)
        return state

    def restore_state(self, value: Mapping[str, Any], *, expected_revision: int | None = None) -> StateSnapshot:
        self._ensure_available()
        candidate = deepcopy(dict(value))
        candidate.pop("exported_at", None)
        candidate.pop("export_checksum", None)
        _validate_state(candidate)
        with self._lock:
            current = self._load()
            if expected_revision is not None and int(current["revision"]) != expected_revision:
                raise BrowserStateConflict("restore revision precondition failed")
            if int(candidate["revision"]) < int(current["revision"]):
                raise BrowserStateConflict("restore would regress state revision")
            candidate["revision"] = int(current["revision"]) + 1
            candidate["updated_at"] = browser_now()
            self._persist(candidate)
            self._state = candidate
        return self.snapshot()

    def session_event_chain_valid(self, session_id: str) -> bool:
        events = self.list_events(session_id, limit=self.event_limit)
        known: set[str] = set()
        for event in events:
            event_id = str(event.get("event_id") or "")
            cause = str(event.get("cause_event_id") or "")
            if not event_id or event_id in known:
                return False
            if cause and cause not in known:
                return False
            known.add(event_id)
        return True

    def prune_requests(self, *, keep: int = 2048) -> int:
        keep = max(0, keep)
        removed = 0
        def mutate(state: dict[str, Any]) -> None:
            nonlocal removed
            ordered = sorted(
                state["requests"].items(),
                key=lambda item: str(item[1].get("recorded_at") or ""),
                reverse=True,
            )
            retained = dict(ordered[:keep])
            removed = len(state["requests"]) - len(retained)
            state["requests"] = retained
        self._mutate(mutate, changed_paths=("requests",))
        return removed

    def clear_stale_leases(self, *, active_owners: set[str]) -> tuple[str, ...]:
        removed: list[str] = []
        def mutate(state: dict[str, Any]) -> None:
            for session_id, lease in tuple(state["leases"].items()):
                if str(lease.get("owner") or "") not in active_owners:
                    state["leases"].pop(session_id, None)
                    removed.append(session_id)
        self._mutate(mutate, changed_paths=("leases",))
        return tuple(removed)

    def mark_interrupted_sessions(self) -> tuple[BrowserSessionRef, ...]:
        changed: list[BrowserSessionRef] = []
        def mutate(state: dict[str, Any]) -> None:
            for session_id, value in tuple(state["sessions"].items()):
                session = _session_from_dict(value)
                if session.status not in {"preparing", "starting", "connecting", "reconnecting", "stopping"}:
                    continue
                updated = BrowserSessionRef(
                    **{
                        **session.to_dict(),
                        "status": "failed",
                        "revision": session.revision + 1,
                        "updated_at": browser_now(),
                    }
                )
                state["sessions"][session_id] = updated.to_dict()
                changed.append(updated)
        self._mutate(mutate, changed_paths=("sessions",))
        return tuple(changed)
