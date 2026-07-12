from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Mapping

from zyra_core import new_id, now_iso

from .digests import digest_object
from .errors import SubagentBudgetExceeded, SubagentRevisionConflict
from .models import UsageBudget, UsageLedger


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    parent_task_id: str
    child_task_id: str
    requested: UsageBudget
    settled: UsageLedger = field(default_factory=UsageLedger)
    active: bool = True
    revision: int = 0
    created_at: str = field(default_factory=now_iso)
    settled_at: str = ""
    idempotency_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "parent_task_id": self.parent_task_id,
            "child_task_id": self.child_task_id,
            "requested": self.requested.to_dict(),
            "settled": self.settled.to_dict(),
            "active": self.active,
            "revision": self.revision,
            "created_at": self.created_at,
            "settled_at": self.settled_at,
            "idempotency_key": self.idempotency_key,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BudgetReservation":
        return cls(
            reservation_id=str(raw.get("reservation_id") or new_id("subbudget")),
            parent_task_id=str(raw.get("parent_task_id") or ""),
            child_task_id=str(raw.get("child_task_id") or ""),
            requested=UsageBudget.from_dict(raw.get("requested") if isinstance(raw.get("requested"), Mapping) else {}),
            settled=UsageLedger.from_dict(raw.get("settled") if isinstance(raw.get("settled"), Mapping) else {}),
            active=bool(raw.get("active", True)),
            revision=int(raw.get("revision") or 0),
            created_at=str(raw.get("created_at") or now_iso()),
            settled_at=str(raw.get("settled_at") or ""),
            idempotency_key=str(raw.get("idempotency_key") or ""),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ParentBudgetAccount:
    parent_task_id: str
    limit: UsageBudget
    committed: UsageLedger
    active_reservations: tuple[str, ...]
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_task_id": self.parent_task_id,
            "limit": self.limit.to_dict(),
            "committed": self.committed.to_dict(),
            "active_reservations": list(self.active_reservations),
            "revision": self.revision,
        }


class SubagentBudgetReservationStore:
    """Durable shared budget reservations preventing concurrent oversell."""

    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = disabled
        self._lock = RLock()
        self._reservations: dict[str, BudgetReservation] = {}
        self._limits: dict[str, UsageBudget] = {}
        self._committed: dict[str, UsageLedger] = {}
        self._revisions: dict[str, int] = {}
        self._idempotency: dict[str, str] = {}
        self._load()

    def set_parent_limit(
        self,
        parent_task_id: str,
        limit: UsageBudget,
        *,
        expected_revision: int | None = None,
    ) -> ParentBudgetAccount:
        self._require_enabled()
        with self._lock:
            actual = self._revisions.get(parent_task_id, 0)
            if expected_revision is not None and expected_revision != actual:
                raise SubagentRevisionConflict(parent_task_id, expected_revision, actual)
            current = self._limits.get(parent_task_id)
            if current is not None:
                # Parent limits may only shrink during a run.
                limit = current.narrowed_by(limit)
            self._limits[parent_task_id] = limit
            self._committed.setdefault(parent_task_id, UsageLedger())
            self._revisions[parent_task_id] = actual + 1
            self._persist()
            return self.account(parent_task_id)

    def reserve(
        self,
        *,
        parent_task_id: str,
        child_task_id: str,
        requested: UsageBudget,
        idempotency_key: str,
    ) -> BudgetReservation:
        self._require_enabled()
        with self._lock:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id:
                return copy.deepcopy(self._reservations[existing_id])
            limit = self._limits.get(parent_task_id)
            if limit is None:
                limit = requested
                self._limits[parent_task_id] = limit
                self._committed[parent_task_id] = UsageLedger()
                self._revisions[parent_task_id] = 1
            available = self._available(parent_task_id)
            for dimension, requested_value in requested.to_dict().items():
                if requested_value > available[dimension]:
                    raise SubagentBudgetExceeded(dimension, requested_value, available[dimension])
            reservation = BudgetReservation(
                reservation_id=new_id("subbudget"),
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                requested=requested,
                idempotency_key=idempotency_key,
                metadata={"limit_revision": self._revisions.get(parent_task_id, 0)},
            )
            self._reservations[reservation.reservation_id] = reservation
            self._idempotency[idempotency_key] = reservation.reservation_id
            self._revisions[parent_task_id] = self._revisions.get(parent_task_id, 0) + 1
            self._persist()
            return copy.deepcopy(reservation)

    def settle(
        self,
        reservation_id: str,
        usage: UsageLedger,
        *,
        release_unused: bool = True,
    ) -> BudgetReservation:
        self._require_enabled()
        with self._lock:
            current = self._reservations.get(reservation_id)
            if current is None:
                raise KeyError(reservation_id)
            if not current.active:
                if current.settled == usage:
                    return copy.deepcopy(current)
                raise SubagentRevisionConflict(current.child_task_id, current.revision, current.revision + 1)
            self._assert_usage_within(current.requested, usage)
            committed = self._committed.get(current.parent_task_id, UsageLedger())
            self._committed[current.parent_task_id] = committed.add(**usage.to_dict())
            settled = BudgetReservation(
                reservation_id=current.reservation_id,
                parent_task_id=current.parent_task_id,
                child_task_id=current.child_task_id,
                requested=current.requested,
                settled=usage,
                active=False,
                revision=current.revision + 1,
                created_at=current.created_at,
                settled_at=now_iso(),
                idempotency_key=current.idempotency_key,
                metadata={**current.metadata, "release_unused": release_unused},
            )
            self._reservations[reservation_id] = settled
            self._revisions[current.parent_task_id] = self._revisions.get(current.parent_task_id, 0) + 1
            self._persist()
            return copy.deepcopy(settled)

    def release(self, reservation_id: str, *, reason: str) -> BudgetReservation:
        self._require_enabled()
        with self._lock:
            current = self._reservations.get(reservation_id)
            if current is None:
                raise KeyError(reservation_id)
            if not current.active:
                return copy.deepcopy(current)
            released = BudgetReservation(
                reservation_id=current.reservation_id,
                parent_task_id=current.parent_task_id,
                child_task_id=current.child_task_id,
                requested=current.requested,
                settled=UsageLedger(),
                active=False,
                revision=current.revision + 1,
                created_at=current.created_at,
                settled_at=now_iso(),
                idempotency_key=current.idempotency_key,
                metadata={**current.metadata, "released": True, "reason": reason},
            )
            self._reservations[reservation_id] = released
            self._revisions[current.parent_task_id] = self._revisions.get(current.parent_task_id, 0) + 1
            self._persist()
            return copy.deepcopy(released)

    def account(self, parent_task_id: str) -> ParentBudgetAccount:
        with self._lock:
            limit = self._limits.get(parent_task_id, UsageBudget())
            committed = self._committed.get(parent_task_id, UsageLedger())
            active = tuple(sorted(
                item.reservation_id
                for item in self._reservations.values()
                if item.parent_task_id == parent_task_id and item.active
            ))
            return ParentBudgetAccount(
                parent_task_id=parent_task_id,
                limit=limit,
                committed=committed,
                active_reservations=active,
                revision=self._revisions.get(parent_task_id, 0),
            )

    def get(self, reservation_id: str) -> BudgetReservation:
        with self._lock:
            return copy.deepcopy(self._reservations[reservation_id])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = {
                "schema": "zyra.subagent-budget/v1",
                "limits": {key: value.to_dict() for key, value in self._limits.items()},
                "committed": {key: value.to_dict() for key, value in self._committed.items()},
                "revisions": dict(self._revisions),
                "reservations": {key: value.to_dict() for key, value in self._reservations.items()},
            }
            return {**payload, "digest": digest_object(payload)}

    def _available(self, parent_task_id: str) -> dict[str, int]:
        limit = self._limits[parent_task_id].to_dict()
        committed = self._committed.get(parent_task_id, UsageLedger()).to_dict()
        reserved = {key: 0 for key in limit}
        mapping = {
            "max_turns": "turns",
            "max_tool_calls": "tool_calls",
            "max_input_tokens": "input_tokens",
            "max_output_tokens": "output_tokens",
            "max_result_chars": "result_chars",
            "max_wall_time_ms": "wall_time_ms",
            "max_children": "child_count",
            "max_depth": None,
        }
        for reservation in self._reservations.values():
            if reservation.parent_task_id != parent_task_id or not reservation.active:
                continue
            for key, value in reservation.requested.to_dict().items():
                reserved[key] += value
        available: dict[str, int] = {}
        for key, maximum in limit.items():
            usage_key = mapping[key]
            used = committed.get(usage_key, 0) if usage_key else 0
            available[key] = max(0, maximum - used - reserved[key])
        return available

    def _assert_usage_within(self, budget: UsageBudget, usage: UsageLedger) -> None:
        mapping = {
            "turns": budget.max_turns,
            "tool_calls": budget.max_tool_calls,
            "input_tokens": budget.max_input_tokens,
            "output_tokens": budget.max_output_tokens,
            "result_chars": budget.max_result_chars,
            "wall_time_ms": budget.max_wall_time_ms,
            "child_count": budget.max_children,
        }
        for dimension, maximum in mapping.items():
            used = int(getattr(usage, dimension))
            if used > maximum:
                raise SubagentBudgetExceeded(dimension, used, maximum)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._limits = {
            str(key): UsageBudget.from_dict(value)
            for key, value in (raw.get("limits") or {}).items()
            if isinstance(value, Mapping)
        }
        self._committed = {
            str(key): UsageLedger.from_dict(value)
            for key, value in (raw.get("committed") or {}).items()
            if isinstance(value, Mapping)
        }
        self._revisions = {str(key): int(value) for key, value in (raw.get("revisions") or {}).items()}
        self._reservations = {
            str(key): BudgetReservation.from_dict(value)
            for key, value in (raw.get("reservations") or {}).items()
            if isinstance(value, Mapping)
        }
        self._idempotency = {
            item.idempotency_key: item.reservation_id
            for item in self._reservations.values()
            if item.idempotency_key
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.snapshot()
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise RuntimeError("SubagentBudgetReservationStore is disabled")
