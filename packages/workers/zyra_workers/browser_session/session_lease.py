from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from .errors import BrowserSessionBusy, BrowserStateConflict, BrowserStateCorrupt, BrowserStateError
from .integration_models import (
    BrowserActionReceipt,
    BrowserActionStatus,
    BrowserArtifactHandoff,
    BrowserLeaseStatus,
    BrowserSessionLease,
    integration_digest,
)
from .models import browser_now


T = TypeVar("T")
LEASE_SCHEMA = "zyra.browser-session-integration.v1"
_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _shared_process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _PROCESS_LOCK_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


class BrowserLeaseLost(BrowserSessionBusy):
    code = "browser_session_logical_lease_lost"


class BrowserReceiptConflict(BrowserStateConflict):
    code = "browser_action_receipt_conflict"


class BrowserSessionLeaseStore:
    """Durable logical action fencing for one BrowserSession.

    This is deliberately not a physical worker/resource lease. It prevents two
    BrowserWorker requests in one API process (or two recovered processes using
    the same state root) from mutating the same session concurrently. Physical
    worker placement remains owned by M1-07A.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        default_ttl_seconds: float = 60.0,
        receipt_limit: int = 8192,
        handoff_limit: int = 8192,
        disabled: bool = False,
    ) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("browser logical lease ttl must be positive")
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "browser-session-integration.json"
        self.backup_path = self.root / "browser-session-integration.backup.json"
        self.journal_path = self.root / "browser-session-integration.journal.jsonl"
        self.default_ttl_seconds = float(default_ttl_seconds)
        self.receipt_limit = max(128, int(receipt_limit))
        self.handoff_limit = max(128, int(handoff_limit))
        self.disabled = disabled
        self._lock = _shared_process_lock(self.path)
        self.lock_path = self.root / "browser-session-integration.lock"
        self._state: dict[str, Any] | None = None
        if not disabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserStateError("browser session integration store is disabled", code="browser_integration_store_disabled")

    def _empty(self) -> dict[str, Any]:
        now = browser_now()
        return {
            "schema": LEASE_SCHEMA,
            "revision": 0,
            "created_at": now,
            "updated_at": now,
            "leases": {},
            "session_generations": {},
            "action_receipts": {},
            "request_index": {},
            "session_receipts": {},
            "artifact_handoffs": {},
            "session_handoffs": {},
            "control_receipts": {},
            "control_request_index": {},
            "session_controls": {},
            "mutations": [],
            "checksum": "",
        }

    def _validate(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != LEASE_SCHEMA:
            raise BrowserStateCorrupt("browser session integration schema mismatch")
        for name in (
            "leases",
            "session_generations",
            "action_receipts",
            "request_index",
            "session_receipts",
            "artifact_handoffs",
            "session_handoffs",
            "control_receipts",
            "control_request_index",
            "session_controls",
        ):
            if not isinstance(state.get(name), Mapping):
                raise BrowserStateCorrupt(f"browser session integration field {name} is invalid")
        if int(state.get("revision") or 0) < 0:
            raise BrowserStateCorrupt("browser session integration revision is invalid")

    def _load(self) -> dict[str, Any]:
        self._ensure_available()
        if self._state is not None:
            return self._state
        if not self.path.exists():
            state = self._empty()
            self._persist(state, mutation=None)
            self._state = state
            return state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            self._migrate(state)
            self._validate(state)
        except (OSError, json.JSONDecodeError, BrowserStateCorrupt) as original:
            state = self._recover(original)
        self._state = state
        return state

    def _recover(self, original: BaseException) -> dict[str, Any]:
        if not self.backup_path.exists():
            raise BrowserStateCorrupt(f"browser session integration state cannot be recovered: {original}") from original
        try:
            state = json.loads(self.backup_path.read_text(encoding="utf-8"))
            self._migrate(state)
            self._validate(state)
        except (OSError, json.JSONDecodeError, BrowserStateCorrupt) as backup:
            raise BrowserStateCorrupt(f"browser integration primary and backup are corrupt: {original}; {backup}") from backup
        self._write_atomic(self.path, state)
        return state

    @staticmethod
    def _migrate(state: dict[str, Any]) -> None:
        # Additive v1 migrations preserve existing action/lease receipts.
        changed = False
        for key, default in (
            ("control_receipts", {}),
            ("control_request_index", {}),
            ("session_controls", {}),
            ("mutations", []),
        ):
            if key not in state:
                state[key] = default
                changed = True
        if changed:
            state["checksum"] = integration_digest({key: value for key, value in state.items() if key != "checksum"})

    @staticmethod
    def _checksum(state: Mapping[str, Any]) -> str:
        value = {key: item for key, item in state.items() if key != "checksum"}
        return integration_digest(value)

    def _write_atomic(self, path: Path, value: Mapping[str, Any]) -> None:
        # Explicit store operations may legitimately race directory setup (for
        # example a newly provisioned runtime root). Atomic persistence owns
        # creation of its immediate parent; registry health probes separately
        # reject removed/stale runtime owners before reaching this boundary.
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        with temp.open("w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    def _persist(self, state: dict[str, Any], mutation: Mapping[str, Any] | None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = browser_now()
        state["checksum"] = self._checksum(state)
        if self.path.exists():
            try:
                previous = json.loads(self.path.read_text(encoding="utf-8"))
                self._migrate(previous)
                self._validate(previous)
            except (OSError, json.JSONDecodeError, BrowserStateCorrupt):
                previous = None
            if previous is not None:
                self._write_atomic(self.backup_path, previous)
        self._write_atomic(self.path, state)
        if mutation is not None:
            with self.journal_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(mutation), sort_keys=True, default=str) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def _mutate(self, name: str, operation: Callable[[dict[str, Any]], T]) -> T:
        self._ensure_available()
        with self._lock:
            with self._exclusive_file_lock():
                # Never mutate a cached revision after another application or
                # process may have committed the same JSON store.
                self._state = None
                before = self._load()
                candidate = deepcopy(before)
                result = operation(candidate)
                before_revision = int(before["revision"])
                candidate["revision"] = before_revision + 1
                mutation = {
                    "operation": name,
                    "revision_before": before_revision,
                    "revision_after": candidate["revision"],
                    "checksum_before": self._checksum(before),
                    "committed_at": browser_now(),
                }
                mutations = candidate.setdefault("mutations", [])
                mutations.append(mutation)
                del mutations[:-2048]
                self._persist(candidate, mutation=mutation)
                self._state = candidate
                return result

    @contextmanager
    def _exclusive_file_lock(self, *, timeout_seconds: float = 30.0):
        deadline = time.monotonic() + timeout_seconds
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, f"{os.getpid()} {time.time()}".encode("ascii"))
                os.fsync(descriptor)
            except FileExistsError:
                try:
                    stale = time.time() - self.lock_path.stat().st_mtime > min(10.0, timeout_seconds)
                except OSError:
                    stale = False
                if stale:
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise BrowserSessionBusy("browser integration state file lock is busy")
                time.sleep(0.01)
        try:
            yield
        finally:
            try:
                os.close(descriptor)
            finally:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass

    def acquire(
        self,
        *,
        browser_session_id: str,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        owner_id: str,
        ttl_seconds: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserSessionLease:
        ttl = self.default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            raise ValueError("browser logical lease ttl must be positive")
        now_epoch = time.time()

        def mutate(state: dict[str, Any]) -> BrowserSessionLease:
            raw = state["leases"].get(browser_session_id)
            existing = BrowserSessionLease.from_dict(raw) if isinstance(raw, Mapping) else None
            if existing is not None and existing.status == BrowserLeaseStatus.ACTIVE and existing.expires_at_epoch > now_epoch:
                raise BrowserSessionBusy(
                    f"browser session {browser_session_id} is logically leased by {existing.owner_id}",
                    session_id=browser_session_id,
                )
            generation = int(state["session_generations"].get(browser_session_id) or 0) + 1
            lease = BrowserSessionLease(
                browser_session_id=browser_session_id,
                run_id=run_id,
                task_id=task_id,
                worker_request_id=worker_request_id,
                owner_id=owner_id,
                generation=generation,
                acquired_at=browser_now(),
                expires_at_epoch=now_epoch + ttl,
                metadata=dict(metadata or {}),
            )
            state["leases"][browser_session_id] = lease.to_dict()
            state["session_generations"][browser_session_id] = generation
            return lease

        return self._mutate("lease.acquire", mutate)

    def renew(self, lease: BrowserSessionLease, *, ttl_seconds: float | None = None) -> BrowserSessionLease:
        ttl = self.default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            raise ValueError("browser logical lease ttl must be positive")

        def mutate(state: dict[str, Any]) -> BrowserSessionLease:
            current = self._require_exact(state, lease)
            if current.expires_at_epoch <= time.time():
                expired = replace(current, status=BrowserLeaseStatus.EXPIRED, released_at=browser_now(), reason="lease_expired")
                state["leases"][lease.browser_session_id] = expired.to_dict()
                raise BrowserLeaseLost("browser session logical lease expired", session_id=lease.browser_session_id)
            renewed = replace(current, expires_at_epoch=time.time() + ttl)
            state["leases"][lease.browser_session_id] = renewed.to_dict()
            return renewed

        return self._mutate("lease.renew", mutate)

    def assert_fence(self, lease: BrowserSessionLease) -> BrowserSessionLease:
        with self._lock:
            self._state = None
            current = self._require_exact(self._load(), lease)
            if current.status != BrowserLeaseStatus.ACTIVE or current.expires_at_epoch <= time.time():
                raise BrowserLeaseLost("browser session logical lease is no longer active", session_id=lease.browser_session_id)
            return current

    def release(self, lease: BrowserSessionLease, *, reason: str = "completed") -> BrowserSessionLease:
        def mutate(state: dict[str, Any]) -> BrowserSessionLease:
            current = self._require_exact(state, lease)
            released = replace(current, status=BrowserLeaseStatus.RELEASED, released_at=browser_now(), reason=reason)
            state["leases"][lease.browser_session_id] = released.to_dict()
            return released

        return self._mutate("lease.release", mutate)

    def revoke(self, browser_session_id: str, *, reason: str) -> BrowserSessionLease | None:
        def mutate(state: dict[str, Any]) -> BrowserSessionLease | None:
            raw = state["leases"].get(browser_session_id)
            if not isinstance(raw, Mapping):
                return None
            current = BrowserSessionLease.from_dict(raw)
            revoked = replace(current, status=BrowserLeaseStatus.REVOKED, released_at=browser_now(), reason=reason)
            state["leases"][browser_session_id] = revoked.to_dict()
            return revoked

        return self._mutate("lease.revoke", mutate)

    @staticmethod
    def _require_exact(state: Mapping[str, Any], lease: BrowserSessionLease) -> BrowserSessionLease:
        raw = state["leases"].get(lease.browser_session_id)
        if not isinstance(raw, Mapping):
            raise BrowserLeaseLost("browser session logical lease does not exist", session_id=lease.browser_session_id)
        current = BrowserSessionLease.from_dict(raw)
        if current.lease_id != lease.lease_id or current.owner_id != lease.owner_id or current.generation != lease.generation:
            raise BrowserLeaseLost("browser session logical lease fence changed", session_id=lease.browser_session_id)
        return current

    def get_lease(self, browser_session_id: str, *, include_inactive: bool = True) -> BrowserSessionLease | None:
        with self._lock:
            self._state = None
            raw = self._load()["leases"].get(browser_session_id)
            lease = BrowserSessionLease.from_dict(raw) if isinstance(raw, Mapping) else None
        if lease is None:
            return None
        if not include_inactive and (lease.status != BrowserLeaseStatus.ACTIVE or lease.expires_at_epoch <= time.time()):
            return None
        return lease

    def record_action(self, receipt: BrowserActionReceipt) -> BrowserActionReceipt:
        def mutate(state: dict[str, Any]) -> BrowserActionReceipt:
            indexed_id = state["request_index"].get(receipt.request_id)
            if indexed_id:
                existing_raw = state["action_receipts"].get(indexed_id)
                if not isinstance(existing_raw, Mapping):
                    raise BrowserStateCorrupt("browser action receipt index is dangling")
                existing = BrowserActionReceipt.from_dict(existing_raw)
                if existing.request_fingerprint != receipt.request_fingerprint:
                    raise BrowserReceiptConflict("browser action request already has a different receipt", session_id=receipt.browser_session_id)
                if existing.to_dict() == receipt.to_dict():
                    return existing
                approved_transition = (
                    existing.status is BrowserActionStatus.PERMISSION_BLOCKED
                    and existing.error_code == "browser_action_permission_pending"
                    and receipt.status is BrowserActionStatus.SUCCEEDED
                )
                if not approved_transition:
                    raise BrowserReceiptConflict("browser action request already has a different receipt", session_id=receipt.browser_session_id)
                # Preserve the pending receipt in the session audit trail while
                # advancing the request index to its exact approved execution.
                del state["request_index"][receipt.request_id]
            state["action_receipts"][receipt.receipt_id] = receipt.to_dict()
            state["request_index"][receipt.request_id] = receipt.receipt_id
            session_receipts = state["session_receipts"].setdefault(receipt.browser_session_id, [])
            session_receipts.append(receipt.receipt_id)
            del session_receipts[:-self.receipt_limit]
            live_ids = {item for values in state["session_receipts"].values() for item in values}
            state["action_receipts"] = {key: value for key, value in state["action_receipts"].items() if key in live_ids}
            state["request_index"] = {
                key: value for key, value in state["request_index"].items() if value in state["action_receipts"]
            }
            return receipt

        return self._mutate("action.record", mutate)

    def action_for_request(self, request_id: str, request_fingerprint: str = "") -> BrowserActionReceipt | None:
        with self._lock:
            self._state = None
            state = self._load()
            receipt_id = state["request_index"].get(request_id)
            raw = state["action_receipts"].get(receipt_id) if receipt_id else None
            receipt = BrowserActionReceipt.from_dict(raw) if isinstance(raw, Mapping) else None
        if receipt and request_fingerprint and receipt.request_fingerprint != request_fingerprint:
            raise BrowserReceiptConflict("browser action retry fingerprint changed", session_id=receipt.browser_session_id)
        return receipt

    def list_actions(self, browser_session_id: str, *, limit: int = 200) -> tuple[BrowserActionReceipt, ...]:
        with self._lock:
            self._state = None
            state = self._load()
            ids = tuple(state["session_receipts"].get(browser_session_id, ()))
            values = [state["action_receipts"].get(item) for item in ids[-max(1, limit):]]
        return tuple(BrowserActionReceipt.from_dict(item) for item in values if isinstance(item, Mapping))

    def record_handoff(self, handoff: BrowserArtifactHandoff) -> BrowserArtifactHandoff:
        def mutate(state: dict[str, Any]) -> BrowserArtifactHandoff:
            existing = state["artifact_handoffs"].get(handoff.handoff_id)
            if isinstance(existing, Mapping):
                if dict(existing) != handoff.to_dict():
                    raise BrowserStateConflict("browser artifact handoff identifier conflict", session_id=handoff.browser_session_id)
                return handoff
            state["artifact_handoffs"][handoff.handoff_id] = handoff.to_dict()
            ids = state["session_handoffs"].setdefault(handoff.browser_session_id, [])
            ids.append(handoff.handoff_id)
            del ids[:-self.handoff_limit]
            live_ids = {item for values in state["session_handoffs"].values() for item in values}
            state["artifact_handoffs"] = {key: value for key, value in state["artifact_handoffs"].items() if key in live_ids}
            return handoff

        return self._mutate("artifact.handoff", mutate)

    def list_handoffs(self, browser_session_id: str, *, limit: int = 200) -> tuple[dict[str, Any], ...]:
        with self._lock:
            self._state = None
            state = self._load()
            ids = tuple(state["session_handoffs"].get(browser_session_id, ()))
            return tuple(deepcopy(state["artifact_handoffs"][item]) for item in ids[-max(1, limit):] if item in state["artifact_handoffs"])

    def expire_stale(self, *, now_epoch: float | None = None) -> tuple[BrowserSessionLease, ...]:
        selected_now = time.time() if now_epoch is None else float(now_epoch)

        def mutate(state: dict[str, Any]) -> tuple[BrowserSessionLease, ...]:
            expired: list[BrowserSessionLease] = []
            for session_id, raw in tuple(state["leases"].items()):
                lease = BrowserSessionLease.from_dict(raw)
                if lease.status == BrowserLeaseStatus.ACTIVE and lease.expires_at_epoch <= selected_now:
                    lease = replace(lease, status=BrowserLeaseStatus.EXPIRED, released_at=browser_now(), reason="ttl_elapsed")
                    state["leases"][session_id] = lease.to_dict()
                    expired.append(lease)
            return tuple(expired)

        return self._mutate("lease.expire_stale", mutate)

    def record_control(self, value: Mapping[str, Any]) -> dict[str, Any]:
        receipt = deepcopy(dict(value))
        request_id = str(receipt.get("request_id") or "")
        receipt_id = str(receipt.get("receipt_id") or "")
        session_id = str(receipt.get("browser_session_id") or "")
        fingerprint = str(receipt.get("request_fingerprint") or "")
        if not request_id or not receipt_id or not fingerprint:
            raise BrowserStateError("durable browser control receipt identity is incomplete")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            existing_id = state["control_request_index"].get(request_id)
            if existing_id:
                existing = state["control_receipts"].get(existing_id)
                if not isinstance(existing, Mapping) or str(existing.get("request_fingerprint")) != fingerprint:
                    raise BrowserReceiptConflict("browser control request fingerprint conflict", session_id=session_id)
                return deepcopy(dict(existing))
            state["control_receipts"][receipt_id] = receipt
            state["control_request_index"][request_id] = receipt_id
            values = state["session_controls"].setdefault(session_id, [])
            values.append(receipt_id)
            del values[:-self.receipt_limit]
            return receipt

        return self._mutate("control.record", mutate)

    def control_for_request(self, request_id: str, fingerprint: str = "") -> dict[str, Any] | None:
        with self._lock:
            self._state = None
            state = self._load()
            receipt_id = state["control_request_index"].get(request_id)
            value = state["control_receipts"].get(receipt_id) if receipt_id else None
            result = deepcopy(dict(value)) if isinstance(value, Mapping) else None
        if result is not None and fingerprint and str(result.get("request_fingerprint")) != fingerprint:
            raise BrowserReceiptConflict("browser control retry fingerprint changed", session_id=str(result.get("browser_session_id") or ""))
        return result

    def list_controls(self, browser_session_id: str, *, limit: int = 200) -> tuple[dict[str, Any], ...]:
        with self._lock:
            self._state = None
            state = self._load()
            ids = tuple(state["session_controls"].get(browser_session_id, ()))
            return tuple(
                deepcopy(state["control_receipts"][item])
                for item in ids[-max(1, limit):]
                if item in state["control_receipts"]
            )

    def audit(self) -> tuple[str, ...]:
        issues: list[str] = []
        with self._lock:
            self._state = None
            state = self._load()
            if state.get("checksum") != self._checksum(state):
                issues.append("integration state checksum mismatch")
            for request_id, receipt_id in state["request_index"].items():
                if receipt_id not in state["action_receipts"]:
                    issues.append(f"dangling action request index: {request_id}")
            for request_id, receipt_id in state["control_request_index"].items():
                if receipt_id not in state["control_receipts"]:
                    issues.append(f"dangling control request index: {request_id}")
            for session_id, ids in state["session_receipts"].items():
                for receipt_id in ids:
                    raw = state["action_receipts"].get(receipt_id)
                    if not isinstance(raw, Mapping):
                        issues.append(f"missing action receipt {receipt_id} for session {session_id}")
                    elif str(raw.get("browser_session_id")) != session_id:
                        issues.append(f"action receipt session mismatch: {receipt_id}")
            for session_id, raw in state["leases"].items():
                try:
                    lease = BrowserSessionLease.from_dict(raw)
                except (TypeError, ValueError) as error:
                    issues.append(f"invalid lease {session_id}: {error}")
                    continue
                generation = int(state["session_generations"].get(session_id) or 0)
                if lease.generation > generation:
                    issues.append(f"lease generation exceeds fence for session {session_id}")
            for session_id, ids in state["session_controls"].items():
                for receipt_id in ids:
                    value = state["control_receipts"].get(receipt_id)
                    if not isinstance(value, Mapping):
                        issues.append(f"missing control receipt {receipt_id} for session {session_id}")
                    elif str(value.get("browser_session_id") or "") != session_id:
                        issues.append(f"control receipt session mismatch: {receipt_id}")
        return tuple(issues)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            leases = tuple(BrowserSessionLease.from_dict(item) for item in state["leases"].values())
            active = sum(1 for item in leases if item.status == BrowserLeaseStatus.ACTIVE and item.expires_at_epoch > time.time())
            return {
                "runtime_id": "zyra-browser-session-integration-store",
                "schema": LEASE_SCHEMA,
                "revision": int(state["revision"]),
                "checksum": str(state["checksum"]),
                "leases": len(leases),
                "active_leases": active,
                "action_receipts": len(state["action_receipts"]),
                "artifact_handoffs": len(state["artifact_handoffs"]),
                "control_receipts": len(state["control_receipts"]),
                "updated_at": str(state["updated_at"]),
                "issues": list(self.audit()),
            }

    def reload(self) -> dict[str, Any]:
        with self._lock:
            self._state = None
            self._load()
        return self.snapshot()
