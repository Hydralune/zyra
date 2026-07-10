from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from .canonical import canonical_arguments_json
from .models import (
    PermissionDecisionRecord,
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionRequestPhase,
    PermissionRequestRecord,
    PermissionRequestStatus,
    PermissionResolutionResponse,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity,
)
from .rules import RuleEvaluation, RuleMatch, RuleMatcher, evaluate_rules


PERMISSION_STATE_SCHEMA = "zyra.permission-state"
PERMISSION_STATE_VERSION = 1
PERMISSION_SNAPSHOT_SCHEMA = "zyra.permission-state-snapshot"
PERMISSION_SNAPSHOT_VERSION = 1

StateMutator = Callable[[dict[str, Any]], Mapping[str, Any] | None]
T = TypeVar("T")


class PermissionStoreError(RuntimeError):
    pass


class PermissionStateConflict(PermissionStoreError):
    pass


class PermissionIdentityMismatch(PermissionStoreError):
    pass


class PermissionRequestTerminal(PermissionStoreError):
    pass


class PermissionRequestExpired(PermissionStoreError):
    pass


class PermissionStateDisabled(PermissionStoreError):
    pass


class PermissionStateCorrupt(PermissionStoreError):
    pass


_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


class PermissionStateStore:
    """Atomic, source-aware permission state repository.

    This is the single JSON state container used by the permission foundation:
    global rule sources, frozen per-session base snapshots, session overlays,
    request transitions and append-only decisions live under one revision. The
    logical RuleStore and RequestQueue are views over this state, not separate
    files or competing owners.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        disabled: bool = False,
        lock_timeout: float = 5.0,
        stale_lock_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path).resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.disabled = disabled
        self.lock_timeout = max(0.1, float(lock_timeout))
        self.stale_lock_seconds = max(self.lock_timeout, float(stale_lock_seconds))
        self._lock = _process_lock(self.path)

    def read_state(self) -> dict[str, Any]:
        self._assert_enabled()
        with self._guard():
            state = self._load_unlocked()
            self._validate_state(state)
            return copy.deepcopy(state)

    def mutate(
        self,
        mutator: StateMutator,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._assert_enabled()
        if not callable(mutator):
            raise TypeError("mutator must be callable")
        with self._guard():
            current = self._load_unlocked()
            self._validate_state(current)
            actual_revision = int(current.get("revision") or 0)
            if expected_revision is not None and actual_revision != expected_revision:
                raise PermissionStateConflict(
                    f"permission state revision conflict: expected {expected_revision}, actual {actual_revision}"
                )
            working = copy.deepcopy(current)
            replacement = mutator(working)
            if replacement is not None:
                if not isinstance(replacement, Mapping):
                    raise TypeError("permission state mutator must return a mapping or None")
                working = copy.deepcopy(dict(replacement))
            working["schema"] = PERMISSION_STATE_SCHEMA
            working["schema_version"] = PERMISSION_STATE_VERSION
            working["revision"] = actual_revision + 1
            working["updated_at"] = self._now_iso()
            self._normalize_state(working)
            self._validate_state(working)
            self._write_unlocked(working)
            return copy.deepcopy(working)

    def snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        state = self.read_state()
        if session_id:
            overlay = copy.deepcopy(state["session_overlays"].get(session_id))
            if overlay is None:
                overlay = {
                    "session_id": session_id,
                    "revision": 0,
                    "base_revision": int(state.get("revision") or 0),
                    "base_rules": copy.deepcopy(state["global_rules"]),
                    "rules": [],
                    "created_at": str(state.get("updated_at") or self._now_iso()),
                    "updated_at": str(state.get("updated_at") or self._now_iso()),
                    "metadata": {"synthesized_for_snapshot": True},
                }
            requests = {
                key: copy.deepcopy(value)
                for key, value in state["requests"].items()
                if str(value.get("session_id") or "") == session_id
            }
            decisions = [
                copy.deepcopy(value)
                for value in state["decisions"]
                if str(value.get("session_id") or "") == session_id
            ]
            payload = {
                "global_rules": copy.deepcopy(state["global_rules"]),
                "session_overlay": overlay,
                "requests": requests,
                "decisions": decisions,
                "session_metrics": copy.deepcopy(
                    _as_mapping(
                        _as_mapping(state.get("metadata")).get("session_metrics")
                    ).get(session_id, {})
                ),
            }
        else:
            payload = {
                key: copy.deepcopy(value)
                for key, value in state.items()
                if key not in {"snapshots"}
            }
        snapshot = {
            "schema": PERMISSION_SNAPSHOT_SCHEMA,
            "schema_version": PERMISSION_SNAPSHOT_VERSION,
            "snapshot_id": f"permsnapshot_{uuid4().hex}",
            "store_revision": int(state["revision"]),
            "session_id": session_id or "",
            "captured_at": self._now_iso(),
            "payload": payload,
        }
        snapshot["checksum"] = _snapshot_checksum(snapshot)
        return snapshot

    def restore_snapshot(
        self,
        snapshot: Mapping[str, Any],
        session_id: str | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        raw = copy.deepcopy(dict(snapshot))
        if raw.get("schema") != PERMISSION_SNAPSHOT_SCHEMA or int(raw.get("schema_version") or 0) != PERMISSION_SNAPSHOT_VERSION:
            raise PermissionStateCorrupt("unsupported permission snapshot schema")
        checksum = str(raw.get("checksum") or "")
        if not checksum or not hmac.compare_digest(checksum, _snapshot_checksum(raw)):
            raise PermissionStateCorrupt("permission snapshot checksum mismatch")
        captured_session = str(raw.get("session_id") or "")
        target_session = session_id or captured_session
        if captured_session and target_session != captured_session:
            raise PermissionIdentityMismatch("snapshot session identity does not match restore target")
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            raise PermissionStateCorrupt("permission snapshot payload is not an object")

        def restore(state: dict[str, Any]) -> None:
            if target_session:
                overlay = payload.get("session_overlay")
                current_overlay = state["session_overlays"].get(target_session)
                if overlay is None:
                    # Absence in an older checkpoint is never authority to
                    # delete newer live session policy.
                    pass
                elif isinstance(overlay, Mapping):
                    normalized = copy.deepcopy(dict(overlay))
                    normalized["session_id"] = target_session
                    snapshot_overlay_revision = int(normalized.get("revision") or 0)
                    current_overlay_revision = (
                        int(current_overlay.get("revision") or 0)
                        if isinstance(current_overlay, Mapping)
                        else -1
                    )
                    if current_overlay_revision < snapshot_overlay_revision:
                        state["session_overlays"][target_session] = normalized
                    elif current_overlay_revision == snapshot_overlay_revision:
                        if isinstance(current_overlay, Mapping) and dict(current_overlay) != normalized:
                            raise PermissionStateConflict(
                                "permission snapshot overlay conflicts at the same revision"
                            )
                else:
                    raise PermissionStateCorrupt("session overlay snapshot is not an object")
                for key, value in _as_mapping(payload.get("requests")).items():
                    record = PermissionRequestRecord.from_dict(_as_mapping(value))
                    if record.session_id != target_session or key != record.request_id:
                        raise PermissionIdentityMismatch("snapshot request identity does not match target session")
                    current_value = state["requests"].get(key)
                    if current_value is None:
                        state["requests"][key] = record.to_dict()
                        continue
                    current_record = PermissionRequestRecord.from_dict(_as_mapping(current_value))
                    _assert_same_request_identity(current_record, record)
                    if record.revision > current_record.revision:
                        if _request_phase_rank(record.phase) < _request_phase_rank(current_record.phase):
                            raise PermissionStateConflict(
                                "permission snapshot request phase would move backwards"
                            )
                        state["requests"][key] = record.to_dict()
                    elif record.revision == current_record.revision and record.to_dict() != current_record.to_dict():
                        raise PermissionStateConflict(
                            "permission snapshot request conflicts at the same revision"
                        )
                decisions_by_id = {
                    str(value.get("decision_id") or ""): value
                    for value in state["decisions"]
                    if isinstance(value, Mapping)
                }
                for value in payload.get("decisions", []):
                    record = PermissionDecisionRecord.from_dict(_as_mapping(value))
                    if record.session_id != target_session:
                        raise PermissionIdentityMismatch("snapshot decision identity does not match target session")
                    current_value = decisions_by_id.get(record.decision_id)
                    if current_value is None:
                        serialized = record.to_dict()
                        state["decisions"].append(serialized)
                        decisions_by_id[record.decision_id] = serialized
                    elif PermissionDecisionRecord.from_dict(_as_mapping(current_value)).to_dict() != record.to_dict():
                        raise PermissionStateConflict(
                            "permission snapshot decision conflicts with append-only state"
                        )
                snapshot_metrics = payload.get("session_metrics")
                if isinstance(snapshot_metrics, Mapping):
                    metrics = state["metadata"].setdefault("session_metrics", {}).setdefault(
                        target_session,
                        {},
                    )
                    for name, value in snapshot_metrics.items():
                        if name.endswith("_count"):
                            metrics[name] = max(int(metrics.get(name) or 0), int(value or 0))
            else:
                restored = self._migrate(dict(payload))
                state.clear()
                state.update(restored)
            state["metadata"]["last_restored_snapshot_id"] = str(raw.get("snapshot_id") or "")
            state["metadata"]["last_restore_at"] = self._now_iso()

        return self.mutate(restore, expected_revision=expected_revision)

    def add_global_rule(
        self,
        rule: PermissionRuleRecord,
        *,
        expected_revision: int | None = None,
    ) -> PermissionRuleRecord:
        _validate_rule_for_storage(rule, session_id=None)

        def add(state: dict[str, Any]) -> None:
            if any(item.get("rule_id") == rule.rule_id for item in state["global_rules"]):
                raise PermissionStateConflict(f"permission rule already exists: {rule.rule_id}")
            state["global_rules"].append(rule.to_dict())

        self.mutate(add, expected_revision=expected_revision)
        return rule

    def add_session_rule(
        self,
        session_id: str,
        rule: PermissionRuleRecord,
        *,
        expected_revision: int | None = None,
    ) -> PermissionRuleRecord:
        if not session_id:
            raise ValueError("session_id is required for a session overlay rule")
        _validate_rule_for_storage(rule, session_id=session_id)

        def add(state: dict[str, Any]) -> None:
            overlay = _ensure_session_overlay(state, session_id, self._now_iso())
            if any(item.get("rule_id") == rule.rule_id for item in overlay["rules"]):
                raise PermissionStateConflict(f"permission rule already exists: {rule.rule_id}")
            overlay["rules"].append(rule.to_dict())
            overlay["revision"] = int(overlay.get("revision") or 0) + 1
            overlay["updated_at"] = self._now_iso()

        self.mutate(add, expected_revision=expected_revision)
        return rule

    def remove_rule(
        self,
        rule_id: str,
        *,
        session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> PermissionRuleRecord:
        removed: list[PermissionRuleRecord] = []

        def remove(state: dict[str, Any]) -> None:
            if session_id:
                overlay = _ensure_session_overlay(state, session_id, self._now_iso())
                records = overlay["rules"]
            else:
                overlay = None
                records = state["global_rules"]
            next_records = []
            for item in records:
                if str(item.get("rule_id") or "") == rule_id:
                    removed.append(PermissionRuleRecord.from_dict(item))
                else:
                    next_records.append(item)
            if not removed:
                raise KeyError(rule_id)
            if overlay is None:
                state["global_rules"] = next_records
            else:
                overlay["rules"] = next_records
                overlay["revision"] = int(overlay.get("revision") or 0) + 1
                overlay["updated_at"] = self._now_iso()

        self.mutate(remove, expected_revision=expected_revision)
        return removed[0]

    def freeze_session_rules(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        holder: list[dict[str, Any]] = []

        def freeze(state: dict[str, Any]) -> None:
            overlay = _ensure_session_overlay(state, session_id, self._now_iso())
            holder.append(copy.deepcopy(overlay))

        self.mutate(freeze, expected_revision=expected_revision)
        return holder[0]

    def list_rules(
        self,
        *,
        session_id: str | None = None,
        effective: bool = True,
        include_inactive: bool = True,
    ) -> list[PermissionRuleRecord]:
        state = self.read_state()
        if session_id and effective:
            overlay = state["session_overlays"].get(session_id)
            base = overlay.get("base_rules", []) if isinstance(overlay, Mapping) else state["global_rules"]
            additions = overlay.get("rules", []) if isinstance(overlay, Mapping) else []
            values = [*base, *additions]
        elif session_id:
            overlay = state["session_overlays"].get(session_id)
            values = overlay.get("rules", []) if isinstance(overlay, Mapping) else []
        else:
            values = state["global_rules"]
        records = [PermissionRuleRecord.from_dict(item) for item in values]
        if include_inactive:
            return records
        now = self._now()
        return [record for record in records if record.is_active(now)]

    def effective_rules(self, session_id: str) -> list[PermissionRuleRecord]:
        if not session_id:
            raise ValueError("session_id is required for effective rules")
        state = self.read_state()
        if session_id not in state["session_overlays"]:
            self.freeze_session_rules(session_id)
        return self.list_rules(session_id=session_id, effective=True, include_inactive=False)

    def create_request(
        self,
        record: PermissionRequestRecord,
        *,
        expected_revision: int | None = None,
    ) -> PermissionRequestRecord:
        if record.phase != PermissionRequestPhase.CREATED or record.status != PermissionRequestStatus.PENDING:
            raise ValueError("new permission requests must be pending in created phase")
        if not record.expires_at:
            raise ValueError("permission request expiry is required")
        selected: list[PermissionRequestRecord] = []

        def create(state: dict[str, Any]) -> None:
            existing = next(
                (
                    PermissionRequestRecord.from_dict(item)
                    for item in state["requests"].values()
                    if str(item.get("session_id") or "") == record.session_id
                    and str(item.get("request_fingerprint") or "") == record.request_fingerprint
                    and str(item.get("status") or "") == str(PermissionRequestStatus.PENDING)
                ),
                None,
            )
            if existing is not None:
                selected.append(existing)
                return
            if record.request_id in state["requests"]:
                raise PermissionStateConflict(f"permission request already exists: {record.request_id}")
            state["requests"][record.request_id] = record.to_dict()
            selected.append(record)

        self.mutate(create, expected_revision=expected_revision)
        return selected[0]

    def get_request(self, request_id: str) -> PermissionRequestRecord | None:
        item = self.read_state()["requests"].get(request_id)
        return PermissionRequestRecord.from_dict(item) if isinstance(item, Mapping) else None

    def list_requests(
        self,
        *,
        session_id: str | None = None,
        status: PermissionRequestStatus | None = None,
        phase: PermissionRequestPhase | None = None,
    ) -> list[PermissionRequestRecord]:
        records = [PermissionRequestRecord.from_dict(item) for item in self.read_state()["requests"].values()]
        if session_id is not None:
            records = [record for record in records if record.session_id == session_id]
        if status is not None:
            records = [record for record in records if record.status == status]
        if phase is not None:
            records = [record for record in records if record.phase == phase]
        return sorted(records, key=lambda record: (record.created_at, record.request_id))

    def mark_delivered(
        self,
        request_id: str,
        *,
        expected_request_revision: int,
        channel: str = "",
        expected_revision: int | None = None,
    ) -> PermissionRequestRecord:
        updated: list[PermissionRequestRecord] = []

        def deliver(state: dict[str, Any]) -> None:
            record = _request_from_state(state, request_id)
            if record.revision != expected_request_revision:
                raise PermissionStateConflict("permission request revision conflict")
            if record.terminal:
                raise PermissionRequestTerminal(f"permission request is terminal: {record.phase}")
            if record.phase not in {PermissionRequestPhase.CREATED, PermissionRequestPhase.DELIVERED}:
                raise PermissionStateConflict(f"permission request cannot be delivered from phase {record.phase}")
            next_record = PermissionRequestRecord.from_dict(
                {
                    **record.to_dict(),
                    "phase": str(PermissionRequestPhase.DELIVERED),
                    "revision": record.revision + 1,
                    "delivered_at": record.delivered_at or self._now_iso(),
                    "metadata": {**record.metadata, "delivery_channel": channel},
                }
            )
            state["requests"][request_id] = next_record.to_dict()
            updated.append(next_record)

        self.mutate(deliver, expected_revision=expected_revision)
        return updated[0]

    def resolve_request(
        self,
        response: PermissionResolutionResponse,
        *,
        expected_revision: int | None = None,
    ) -> PermissionRequestRecord:
        resolved: list[PermissionRequestRecord] = []
        expired: list[PermissionRequestRecord] = []

        def resolve(state: dict[str, Any]) -> None:
            record = _request_from_state(state, response.request_id)
            if record.revision != response.expected_revision:
                raise PermissionStateConflict("permission request revision conflict")
            if record.terminal:
                raise PermissionRequestTerminal(f"permission request is already terminal: {record.phase}")
            _verify_response_identity(record, response)
            now = self._now()
            if _parse_time(record.expires_at) <= now:
                next_record = PermissionRequestRecord.from_dict(
                    {
                        **record.to_dict(),
                        "status": str(PermissionRequestStatus.EXPIRED),
                        "phase": str(PermissionRequestPhase.EXPIRED),
                        "revision": record.revision + 1,
                        "resolved_at": self._now_iso(),
                        "metadata": {**record.metadata, "expiry_reason": "response_after_expiry"},
                    }
                )
                state["requests"][record.request_id] = next_record.to_dict()
                expired.append(next_record)
                return
            status = PermissionRequestStatus.APPROVED if response.effect == PermissionEffect.ALLOW else PermissionRequestStatus.DENIED
            next_record = PermissionRequestRecord.from_dict(
                {
                    **record.to_dict(),
                    "status": str(status),
                    "phase": str(PermissionRequestPhase.RESOLVED),
                    "revision": record.revision + 1,
                    "resolved_at": response.responded_at or self._now_iso(),
                    "resolved_by": response.actor_id,
                    "resolution_channel": response.channel,
                    "resolution_effect": str(response.effect),
                    "metadata": {
                        **record.metadata,
                        **response.metadata,
                        "resolution_idempotency_key": response.idempotency_key,
                        "resolution_reason": response.reason,
                    },
                }
            )
            state["requests"][record.request_id] = next_record.to_dict()
            if response.create_rule:
                rule_scope = response.rule_scope or record.scope
                rule = PermissionRuleRecord(
                    effect=response.effect,
                    source=PermissionRuleSource.SESSION,
                    scope=rule_scope,
                    tool_pattern=record.tool_identity.name,
                    namespace_pattern=record.tool_identity.namespace,
                    server_pattern=record.tool_identity.server_id or "*",
                    argument_pattern="",
                    reason=f"created from resolved request {record.request_id}",
                    metadata={"permission_request_id": record.request_id, "actor_id": response.actor_id},
                )
                _validate_rule_for_storage(rule, session_id=record.session_id)
                overlay = _ensure_session_overlay(state, record.session_id, self._now_iso())
                overlay["rules"].append(rule.to_dict())
                overlay["revision"] = int(overlay.get("revision") or 0) + 1
                overlay["updated_at"] = self._now_iso()
            resolved.append(next_record)

        self.mutate(resolve, expected_revision=expected_revision)
        if expired:
            raise PermissionRequestExpired(f"permission request expired: {expired[0].request_id}")
        return resolved[0]

    def transition_request(
        self,
        request_id: str,
        phase: PermissionRequestPhase,
        *,
        expected_request_revision: int,
        reason: str = "",
        expected_revision: int | None = None,
    ) -> PermissionRequestRecord:
        if phase not in {PermissionRequestPhase.EXPIRED, PermissionRequestPhase.CANCELLED, PermissionRequestPhase.ABORTED}:
            raise ValueError("transition_request only accepts terminal non-resolution phases")
        status_by_phase = {
            PermissionRequestPhase.EXPIRED: PermissionRequestStatus.EXPIRED,
            PermissionRequestPhase.CANCELLED: PermissionRequestStatus.CANCELLED,
            PermissionRequestPhase.ABORTED: PermissionRequestStatus.ABORTED,
        }
        updated: list[PermissionRequestRecord] = []

        def transition(state: dict[str, Any]) -> None:
            record = _request_from_state(state, request_id)
            if record.revision != expected_request_revision:
                raise PermissionStateConflict("permission request revision conflict")
            if record.terminal:
                raise PermissionRequestTerminal(f"permission request is already terminal: {record.phase}")
            next_record = PermissionRequestRecord.from_dict(
                {
                    **record.to_dict(),
                    "status": str(status_by_phase[phase]),
                    "phase": str(phase),
                    "revision": record.revision + 1,
                    "resolved_at": self._now_iso(),
                    "metadata": {**record.metadata, "terminal_reason": reason},
                }
            )
            state["requests"][request_id] = next_record.to_dict()
            updated.append(next_record)

        self.mutate(transition, expected_revision=expected_revision)
        return updated[0]

    def expire_requests(
        self,
        *,
        session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> list[PermissionRequestRecord]:
        expired: list[PermissionRequestRecord] = []
        now = self._now()

        def expire(state: dict[str, Any]) -> None:
            for request_id, item in list(state["requests"].items()):
                record = PermissionRequestRecord.from_dict(item)
                if record.terminal or (session_id is not None and record.session_id != session_id):
                    continue
                if _parse_time(record.expires_at) > now:
                    continue
                next_record = PermissionRequestRecord.from_dict(
                    {
                        **record.to_dict(),
                        "status": str(PermissionRequestStatus.EXPIRED),
                        "phase": str(PermissionRequestPhase.EXPIRED),
                        "revision": record.revision + 1,
                        "resolved_at": self._now_iso(),
                        "metadata": {**record.metadata, "expiry_reason": "ttl_elapsed"},
                    }
                )
                state["requests"][request_id] = next_record.to_dict()
                expired.append(next_record)

        self.mutate(expire, expected_revision=expected_revision)
        return expired

    def append_decision(
        self,
        record: PermissionDecisionRecord,
        *,
        expected_revision: int | None = None,
    ) -> PermissionDecisionRecord:
        return self.commit_decision(record, expected_revision=expected_revision)

    def commit_decision(
        self,
        record: PermissionDecisionRecord,
        *,
        consume_rule_id: str = "",
        consume_approval_request_id: str = "",
        expected_revision: int | None = None,
    ) -> PermissionDecisionRecord:
        """Atomically persist a decision and consume security capabilities.

        Finite rule use and an approved request's one execution claim share
        the same file-locked state transition as the append-only decision.
        No runtime-local set is authoritative for replay prevention.
        """

        committed: list[PermissionDecisionRecord] = []

        def append(state: dict[str, Any]) -> None:
            if any(item.get("decision_id") == record.decision_id for item in state["decisions"]):
                raise PermissionStateConflict(f"permission decision already exists: {record.decision_id}")
            human_delta = 0
            human_count = int(
                state["metadata"]
                .setdefault("session_metrics", {})
                .setdefault(record.session_id, {})
                .get("human_intervention_count")
                or 0
            )
            if consume_approval_request_id:
                request = _request_from_state(state, consume_approval_request_id)
                if request.session_id != record.session_id or request.request_id != record.request_id:
                    raise PermissionIdentityMismatch(
                        "approval claim does not match permission decision session/request"
                    )
                if (
                    request.status is not PermissionRequestStatus.APPROVED
                    or request.phase is not PermissionRequestPhase.RESOLVED
                    or request.resolution_effect is not PermissionEffect.ALLOW
                ):
                    raise PermissionStateConflict("permission request is not an approved allow")
                if _parse_time(request.expires_at) <= self._now():
                    raise PermissionRequestExpired(
                        f"permission approval expired before grant issuance: {request.request_id}"
                    )
                request_metadata = dict(request.metadata)
                if request_metadata.get("execution_claim_decision_id"):
                    raise PermissionStateConflict(
                        f"permission approval was already claimed: {request.request_id}"
                    )
                human_delta = int(_resolution_requires_human_intervention(request))
                human_count += human_delta
                session_metrics = state["metadata"]["session_metrics"][record.session_id]
                session_metrics["human_intervention_count"] = human_count
                session_metrics["approval_execution_claim_count"] = int(
                    session_metrics.get("approval_execution_claim_count") or 0
                ) + 1
                request_metadata.update(
                    {
                        "execution_claim_decision_id": record.decision_id,
                        "execution_claimed_at": self._now_iso(),
                        "execution_claim_human_intervention": bool(human_delta),
                    }
                )
                claimed_request = replace(
                    request,
                    revision=request.revision + 1,
                    metadata=request_metadata,
                )
                state["requests"][request.request_id] = claimed_request.to_dict()
            if consume_rule_id:
                overlay = state["session_overlays"].get(record.session_id)
                _consume_winning_rule(
                    state,
                    overlay=overlay if isinstance(overlay, dict) else None,
                    rule_id=consume_rule_id,
                    now=self._now(),
                    now_iso=self._now_iso(),
                )
            committed_record = replace(
                record,
                metadata={
                    **record.metadata,
                    "human_intervention_count": human_count,
                    "human_intervention_delta": human_delta,
                    "approval_execution_claimed": bool(consume_approval_request_id),
                },
            )
            state["decisions"].append(committed_record.to_dict())
            committed.append(committed_record)

        self.mutate(append, expected_revision=expected_revision)
        return committed[0]

    def list_decisions(
        self,
        *,
        session_id: str | None = None,
        tool_use_id: str | None = None,
    ) -> list[PermissionDecisionRecord]:
        records = [PermissionDecisionRecord.from_dict(item) for item in self.read_state()["decisions"]]
        if session_id is not None:
            records = [record for record in records if record.session_id == session_id]
        if tool_use_id is not None:
            records = [record for record in records if record.tool_use_id == tool_use_id]
        return records

    def _assert_enabled(self) -> None:
        if self.disabled:
            raise PermissionStateDisabled("PermissionStateStore is disabled")

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise TypeError("permission store clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("permission store clock must return a timezone-aware datetime")
        return value

    def _now_iso(self) -> str:
        return self._now().isoformat()

    @contextmanager
    def _guard(self):
        with self._lock:
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            started = time.monotonic()
            descriptor: int | None = None
            while descriptor is None:
                try:
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(descriptor, f"{os.getpid()}:{threading.get_ident()}:{time.time()}".encode("ascii"))
                    os.fsync(descriptor)
                except FileExistsError:
                    if _lock_is_stale(lock_path, self.stale_lock_seconds):
                        try:
                            lock_path.unlink()
                        except FileNotFoundError:
                            pass
                        continue
                    if time.monotonic() - started >= self.lock_timeout:
                        raise PermissionStoreError(f"timed out acquiring permission state lock: {lock_path}")
                    time.sleep(0.01)
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
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PermissionStateCorrupt(f"cannot read permission state: {error}") from error
        if not isinstance(value, dict):
            raise PermissionStateCorrupt("permission state root must be an object")
        return self._migrate(value)

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp")
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _empty_state(self) -> dict[str, Any]:
        now = self._now_iso()
        return {
            "schema": PERMISSION_STATE_SCHEMA,
            "schema_version": PERMISSION_STATE_VERSION,
            "revision": 0,
            "created_at": now,
            "updated_at": now,
            "global_rules": [],
            "session_overlays": {},
            "requests": {},
            "decisions": [],
            "snapshots": {},
            "metadata": {},
        }

    def _migrate(self, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("schema") == PERMISSION_STATE_SCHEMA:
            version = int(value.get("schema_version") or 0)
            if version > PERMISSION_STATE_VERSION:
                raise PermissionStateCorrupt(f"permission state version {version} is newer than supported")
            state = copy.deepcopy(value)
        elif "rules" in value or "requests" in value:
            state = self._empty_state()
            state["metadata"]["migrated_from"] = "legacy_json_permission_store"
            state["metadata"]["legacy_payload_digest"] = hashlib.sha256(
                json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            for legacy in value.get("rules", []):
                if not isinstance(legacy, Mapping):
                    continue
                effect_text = str(legacy.get("effect") or "ask")
                try:
                    effect = PermissionEffect(effect_text)
                except ValueError:
                    effect = PermissionEffect.ASK
                operation = str(legacy.get("operation") or "*")
                legacy_metadata = {"legacy": True, **_as_mapping(legacy.get("metadata"))}
                if effect == PermissionEffect.ALLOW and str(legacy.get("pattern") or "") in {"", "*"}:
                    effect = PermissionEffect.ASK
                    legacy_metadata["migration_downgrade"] = "broad legacy allow converted to ask"
                rule = PermissionRuleRecord(
                    rule_id=str(legacy.get("rule_id") or f"legacy_rule_{uuid4().hex}"),
                    effect=effect,
                    source=PermissionRuleSource.LOCAL,
                    scope=PermissionScope(PermissionScopeKind.GLOBAL),
                    tool_pattern="*",
                    operation_pattern=operation,
                    argument_pattern=str(legacy.get("pattern") or ""),
                    reason=str(legacy.get("reason") or "migrated legacy rule"),
                    created_at=str(legacy.get("created_at") or self._now_iso()),
                    metadata=legacy_metadata,
                )
                state["global_rules"].append(rule.to_dict())
            legacy_requests = [item for item in value.get("requests", []) if isinstance(item, Mapping)]
            if legacy_requests:
                state["metadata"]["legacy_requests_quarantined"] = copy.deepcopy(legacy_requests)
                state["metadata"]["legacy_requests_active"] = False
        else:
            raise PermissionStateCorrupt("unrecognized permission state schema")
        self._normalize_state(state)
        return state

    def _normalize_state(self, state: MutableMapping[str, Any]) -> None:
        state.setdefault("schema", PERMISSION_STATE_SCHEMA)
        state.setdefault("schema_version", PERMISSION_STATE_VERSION)
        state.setdefault("revision", 0)
        state.setdefault("created_at", self._now_iso())
        state.setdefault("updated_at", state["created_at"])
        state.setdefault("global_rules", [])
        state.setdefault("session_overlays", {})
        state.setdefault("requests", {})
        state.setdefault("decisions", [])
        state.setdefault("snapshots", {})
        state.setdefault("metadata", {})

    def _validate_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != PERMISSION_STATE_SCHEMA:
            raise PermissionStateCorrupt("permission state schema mismatch")
        if int(state.get("schema_version") or 0) != PERMISSION_STATE_VERSION:
            raise PermissionStateCorrupt("permission state version mismatch")
        if int(state.get("revision") or 0) < 0:
            raise PermissionStateCorrupt("permission state revision cannot be negative")
        for name, expected in (
            ("global_rules", list), ("session_overlays", dict), ("requests", dict),
            ("decisions", list), ("snapshots", dict), ("metadata", dict),
        ):
            if not isinstance(state.get(name), expected):
                raise PermissionStateCorrupt(f"permission state field {name} has invalid type")
        rule_ids: set[str] = set()
        for item in state["global_rules"]:
            rule = PermissionRuleRecord.from_dict(_as_mapping(item))
            _validate_rule_for_storage(rule, session_id=None)
            if rule.rule_id in rule_ids:
                raise PermissionStateCorrupt(f"duplicate permission rule id: {rule.rule_id}")
            rule_ids.add(rule.rule_id)
        for session_id, overlay in state["session_overlays"].items():
            if not isinstance(overlay, Mapping) or str(overlay.get("session_id") or "") != str(session_id):
                raise PermissionStateCorrupt("session overlay identity mismatch")
            for item in overlay.get("base_rules", []):
                _validate_rule_for_storage(PermissionRuleRecord.from_dict(_as_mapping(item)), session_id=None)
            for item in overlay.get("rules", []):
                _validate_rule_for_storage(PermissionRuleRecord.from_dict(_as_mapping(item)), session_id=str(session_id))
        for request_id, item in state["requests"].items():
            request = PermissionRequestRecord.from_dict(_as_mapping(item))
            if request.request_id != request_id:
                raise PermissionStateCorrupt("permission request key/id mismatch")
        decision_ids: set[str] = set()
        for item in state["decisions"]:
            decision = PermissionDecisionRecord.from_dict(_as_mapping(item))
            if decision.decision_id in decision_ids:
                raise PermissionStateCorrupt(f"duplicate permission decision id: {decision.decision_id}")
            decision_ids.add(decision.decision_id)


class PermissionRuleStore:
    """Logical rule facade over ``PermissionStateStore`` session state."""

    def __init__(
        self,
        state_store: PermissionStateStore,
        session_id: str = "",
        *,
        matcher: RuleMatcher | None = None,
    ) -> None:
        self.state_store = state_store
        self.session_id = session_id
        self.matcher = matcher or RuleMatcher()

    def add(
        self,
        rule: PermissionRuleRecord,
        *,
        session_overlay: bool | None = None,
        expected_revision: int | None = None,
    ) -> PermissionRuleRecord:
        use_session = bool(self.session_id) if session_overlay is None else session_overlay
        if use_session:
            if not self.session_id:
                raise ValueError("PermissionRuleStore has no session_id")
            return self.state_store.add_session_rule(self.session_id, rule, expected_revision=expected_revision)
        return self.state_store.add_global_rule(rule, expected_revision=expected_revision)

    add_rule = add

    def remove(
        self,
        rule_id: str,
        *,
        session_overlay: bool | None = None,
        expected_revision: int | None = None,
    ) -> PermissionRuleRecord:
        use_session = bool(self.session_id) if session_overlay is None else session_overlay
        return self.state_store.remove_rule(
            rule_id,
            session_id=self.session_id if use_session else None,
            expected_revision=expected_revision,
        )

    remove_rule = remove

    def list(
        self,
        *,
        effective: bool = True,
        include_inactive: bool = True,
    ) -> list[PermissionRuleRecord]:
        return self.state_store.list_rules(
            session_id=self.session_id or None,
            effective=effective,
            include_inactive=include_inactive,
        )

    list_rules = list

    def matches(
        self,
        request: PermissionEvaluationRequest,
        *,
        at: datetime | None = None,
    ) -> tuple[RuleMatch, ...]:
        session_id = self.session_id or request.session_id
        rules = self.state_store.effective_rules(session_id)
        return self.matcher.matches(rules, request, at=at)

    def evaluate(
        self,
        request: PermissionEvaluationRequest,
        *,
        at: datetime | None = None,
    ) -> RuleEvaluation:
        session_id = self.session_id or request.session_id
        rules = self.state_store.effective_rules(session_id)
        return evaluate_rules(rules, request, matcher=self.matcher, at=at)

    def snapshot(self) -> dict[str, Any]:
        return self.state_store.snapshot(self.session_id or None)

    def restore(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self.state_store.restore_snapshot(
            snapshot,
            self.session_id or None,
            expected_revision=expected_revision,
        )


def _request_phase_rank(phase: PermissionRequestPhase) -> int:
    return {
        PermissionRequestPhase.CREATED: 0,
        PermissionRequestPhase.DELIVERED: 1,
        PermissionRequestPhase.RESOLVED: 2,
        PermissionRequestPhase.EXPIRED: 2,
        PermissionRequestPhase.CANCELLED: 2,
        PermissionRequestPhase.ABORTED: 2,
    }[phase]


def _assert_same_request_identity(
    current: PermissionRequestRecord,
    candidate: PermissionRequestRecord,
) -> None:
    fields = (
        "request_id",
        "session_id",
        "task_id",
        "run_id",
        "worker_request_id",
        "tool_use_id",
        "tool_identity",
        "arguments_digest",
        "request_fingerprint",
        "scope",
    )
    for name in fields:
        if getattr(current, name) != getattr(candidate, name):
            raise PermissionIdentityMismatch(
                f"permission snapshot request {name} does not match live state"
            )


def _resolution_requires_human_intervention(record: PermissionRequestRecord) -> bool:
    channel = record.resolution_channel.strip().casefold()
    actor = record.resolved_by.strip().casefold()
    non_human_channels = {
        "policy",
        "sealed",
        "classifier",
        "hook",
        "system",
        "autonomous",
        "benchmark_policy",
    }
    non_human_actors = {"policy", "system", "sealed-policy", "autonomous-policy"}
    return channel not in non_human_channels and actor not in non_human_actors


def _consume_winning_rule(
    state: dict[str, Any],
    *,
    overlay: dict[str, Any] | None,
    rule_id: str,
    now: datetime,
    now_iso: str,
) -> None:
    """Consume a finite rule using the correct ownership layer.

    Session overlay rules are private counters.  Frozen ``base_rules`` are
    policy definitions, but their use counter is global across every session
    that inherited that rule.  Updating all live copies under one state lock
    prevents ``max_uses=1`` from becoming once-per-session.
    """

    if overlay is not None:
        for index, item in enumerate(overlay.get("rules", [])):
            if str(item.get("rule_id") or "") != rule_id:
                continue
            rule = PermissionRuleRecord.from_dict(item)
            if not rule.is_active(now):
                raise PermissionStateConflict(
                    f"permission rule exhausted before decision commit: {rule_id}"
                )
            overlay["rules"][index] = rule.with_use().to_dict()
            overlay["revision"] = int(overlay.get("revision") or 0) + 1
            overlay["updated_at"] = now_iso
            return

        inherited = any(
            str(item.get("rule_id") or "") == rule_id
            for item in overlay.get("base_rules", [])
        )
        if inherited:
            locations: list[tuple[list[dict[str, Any]], dict[str, Any] | None]] = [
                (state["global_rules"], None)
            ]
            for candidate in state["session_overlays"].values():
                if isinstance(candidate, dict):
                    locations.append((candidate.get("base_rules", []), candidate))
            matches: list[tuple[list[dict[str, Any]], int, dict[str, Any] | None, PermissionRuleRecord]] = []
            for values, owner in locations:
                for index, item in enumerate(values):
                    if str(item.get("rule_id") or "") == rule_id:
                        matches.append((values, index, owner, PermissionRuleRecord.from_dict(item)))
            if not matches:
                raise PermissionStateConflict(
                    f"permission inherited rule disappeared before decision commit: {rule_id}"
                )
            shared_use_count = max(match[3].use_count for match in matches)
            reference = replace(matches[0][3], use_count=shared_use_count)
            if not reference.is_active(now):
                raise PermissionStateConflict(
                    f"permission rule exhausted before decision commit: {rule_id}"
                )
            next_use_count = shared_use_count + 1
            touched_overlays: set[str] = set()
            for values, index, owner, rule in matches:
                values[index] = replace(rule, use_count=next_use_count).to_dict()
                if owner is not None:
                    session_key = str(owner.get("session_id") or "")
                    if session_key and session_key not in touched_overlays:
                        owner["revision"] = int(owner.get("revision") or 0) + 1
                        owner["updated_at"] = now_iso
                        touched_overlays.add(session_key)
            return

    for index, item in enumerate(state["global_rules"]):
        if str(item.get("rule_id") or "") != rule_id:
            continue
        rule = PermissionRuleRecord.from_dict(item)
        if not rule.is_active(now):
            raise PermissionStateConflict(
                f"permission rule exhausted before decision commit: {rule_id}"
            )
        next_use_count = rule.use_count + 1
        state["global_rules"][index] = replace(rule, use_count=next_use_count).to_dict()
        for candidate in state["session_overlays"].values():
            if not isinstance(candidate, dict):
                continue
            changed = False
            for base_index, base_item in enumerate(candidate.get("base_rules", [])):
                if str(base_item.get("rule_id") or "") == rule_id:
                    base_rule = PermissionRuleRecord.from_dict(base_item)
                    candidate["base_rules"][base_index] = replace(
                        base_rule,
                        use_count=next_use_count,
                    ).to_dict()
                    changed = True
            if changed:
                candidate["revision"] = int(candidate.get("revision") or 0) + 1
                candidate["updated_at"] = now_iso
        return
    raise PermissionStateConflict(
        f"permission winning rule disappeared before decision commit: {rule_id}"
    )


def _ensure_session_overlay(state: dict[str, Any], session_id: str, now: str) -> dict[str, Any]:
    overlay = state["session_overlays"].get(session_id)
    if overlay is None:
        overlay = {
            "session_id": session_id,
            "revision": 0,
            "base_revision": int(state.get("revision") or 0),
            "base_rules": copy.deepcopy(state["global_rules"]),
            "rules": [],
            "created_at": now,
            "updated_at": now,
            "metadata": {},
        }
        state["session_overlays"][session_id] = overlay
    return overlay


def _validate_rule_for_storage(rule: PermissionRuleRecord, *, session_id: str | None) -> None:
    if session_id:
        if rule.scope.session_id and rule.scope.session_id != session_id:
            raise PermissionIdentityMismatch("session overlay rule scope belongs to another session")
        if rule.source not in {PermissionRuleSource.SESSION, PermissionRuleSource.COMMAND}:
            raise ValueError("session overlay rules must use session or command source")
    elif rule.source in {PermissionRuleSource.SESSION, PermissionRuleSource.COMMAND}:
        raise ValueError("session/command rules cannot be written to the global rule layer")
    if rule.effect == PermissionEffect.ALLOW:
        tool = rule.tool_pattern.casefold()
        dangerous_tools = {"shell", "bash", "powershell", "agent", "python", "node", "interpreter"}
        broad_argument = rule.argument_pattern in {"", "*"} and not rule.scope.argument_digest and not rule.scope.request_fingerprint
        if (tool in dangerous_tools or tool == "*") and broad_argument:
            raise ValueError("broad shell/interpreter/agent allow rules are forbidden")
    if rule.expires_at:
        _parse_time(rule.expires_at)


def _request_from_state(state: Mapping[str, Any], request_id: str) -> PermissionRequestRecord:
    item = state["requests"].get(request_id)
    if not isinstance(item, Mapping):
        raise KeyError(request_id)
    return PermissionRequestRecord.from_dict(item)


def _verify_response_identity(record: PermissionRequestRecord, response: PermissionResolutionResponse) -> None:
    plain_checks = (
        (record.request_id, response.request_id, "request_id"),
        (record.session_id, response.session_id, "session_id"),
        (record.tool_use_id, response.tool_use_id, "tool_use_id"),
    )
    for expected, actual, name in plain_checks:
        if expected != actual:
            raise PermissionIdentityMismatch(f"permission response {name} mismatch")
    digest_checks = (
        (record.arguments_digest, response.arguments_digest, "arguments_digest"),
        (record.request_fingerprint, response.request_fingerprint, "request_fingerprint"),
    )
    for expected, actual, name in digest_checks:
        if not hmac.compare_digest(expected, actual):
            raise PermissionIdentityMismatch(f"permission response {name} mismatch")
    if record.tool_identity != response.tool_identity:
        raise PermissionIdentityMismatch("permission response tool/server identity mismatch")
    if canonical_arguments_json(record.scope.to_dict()) != canonical_arguments_json(response.scope.to_dict()):
        raise PermissionIdentityMismatch("permission response scope mismatch")


def _snapshot_checksum(snapshot: Mapping[str, Any]) -> str:
    payload = {key: copy.deepcopy(value) for key, value in snapshot.items() if key != "checksum"}
    encoded = canonical_arguments_json(payload)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _parse_time(value: str) -> datetime:
    if not value:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _lock_is_stale(path: Path, stale_seconds: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime > stale_seconds
    except FileNotFoundError:
        return False


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PermissionStateCorrupt("expected an object in permission state")
    return {str(key): item for key, item in value.items()}
