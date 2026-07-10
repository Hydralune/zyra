from __future__ import annotations

"""Tamper-evident permission decision journal.

The journal is deliberately separate from the pending queue: the queue owns
mutable lifecycle state while this component owns the append-only explanation
of what the guard observed and why execution was allowed or blocked.  Entries
are hash chained per session and can be restored into a resumed CodeWorker
session without granting authority by themselves.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from zyra_core import new_id, now_iso, to_jsonable


PERMISSION_DECISION_LOG_SCHEMA_VERSION = 1
_GENESIS_HASH = "sha256:" + "0" * 64
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "secret_token",
        "signature",
        "token",
    }
)


class PermissionDecisionEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionDecisionStage(StrEnum):
    PRE_TOOL_HOOK = "pre_tool_hook"
    DENY_RULE = "deny_rule"
    ASK_RULE = "ask_rule"
    TOOL_RISK = "tool_risk"
    TOOL_DENY = "tool_deny"
    INTERACTIVE = "interactive"
    CONTENT = "content"
    SAFETY = "safety"
    MODE = "mode"
    CLASSIFIER = "classifier"
    USER_RESOLUTION = "user_resolution"
    EXECUTION_GRANT = "execution_grant"
    FINAL = "final"
    RECOVERY = "recovery"


class PermissionDecisionLogDisabledError(RuntimeError):
    pass


class PermissionDecisionLogCorruptError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PermissionDecisionEvidence:
    stage: PermissionDecisionStage
    effect: str
    reason: str
    source: str
    source_id: str = ""
    bypass_immune: bool = False
    advisory_only: bool = False
    matched: bool = True
    priority: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage),
            "effect": self.effect,
            "reason": self.reason,
            "source": self.source,
            "source_id": self.source_id,
            "bypass_immune": self.bypass_immune,
            "advisory_only": self.advisory_only,
            "matched": self.matched,
            "priority": self.priority,
            "metadata": _redact(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PermissionDecisionEvidence":
        return cls(
            stage=PermissionDecisionStage(str(value.get("stage") or PermissionDecisionStage.FINAL)),
            effect=str(value.get("effect") or "ask"),
            reason=str(value.get("reason") or ""),
            source=str(value.get("source") or "runtime"),
            source_id=str(value.get("source_id") or ""),
            bypass_immune=bool(value.get("bypass_immune", False)),
            advisory_only=bool(value.get("advisory_only", False)),
            matched=bool(value.get("matched", True)),
            priority=int(value.get("priority") or 0),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class PermissionDecisionEntry:
    decision_id: str
    session_id: str
    run_id: str
    task_id: str
    node_id: str | None
    worker_request_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    namespace: str
    server_name: str
    arguments_digest: str
    scope_digest: str
    effect: PermissionDecisionEffect
    reason: str
    mode: str
    risk: str
    request_id: str = ""
    rule_id: str = ""
    rule_source: str = ""
    expiry: float | None = None
    human_intervention_count: int = 0
    recovery_alternatives: tuple[str, ...] = ()
    evidence: tuple[PermissionDecisionEvidence, ...] = ()
    parent_decision_id: str = ""
    previous_hash: str = _GENESIS_HASH
    entry_hash: str = ""
    sequence: int = 0
    created_at: str = field(default_factory=now_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def exact_tool_key(self) -> str:
        return "|".join((self.namespace, self.server_name, self.tool_name))

    def hash_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "worker_request_id": self.worker_request_id,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "namespace": self.namespace,
            "server_name": self.server_name,
            "arguments_digest": self.arguments_digest,
            "scope_digest": self.scope_digest,
            "effect": str(self.effect),
            "reason": self.reason,
            "mode": self.mode,
            "risk": self.risk,
            "request_id": self.request_id,
            "rule_id": self.rule_id,
            "rule_source": self.rule_source,
            "expiry": self.expiry,
            "human_intervention_count": self.human_intervention_count,
            "recovery_alternatives": list(self.recovery_alternatives),
            "evidence": [item.to_dict() for item in self.evidence],
            "parent_decision_id": self.parent_decision_id,
            "previous_hash": self.previous_hash,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "metadata": _redact(self.metadata),
        }

    def calculate_hash(self) -> str:
        raw = json.dumps(self.hash_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.hash_payload(), "entry_hash": self.entry_hash or self.calculate_hash()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PermissionDecisionEntry":
        return cls(
            decision_id=str(value.get("decision_id") or ""),
            session_id=str(value.get("session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            node_id=str(value["node_id"]) if value.get("node_id") is not None else None,
            worker_request_id=str(value.get("worker_request_id") or ""),
            turn_id=str(value.get("turn_id") or ""),
            tool_call_id=str(value.get("tool_call_id") or value.get("tool_use_id") or ""),
            tool_name=str(value.get("tool_name") or ""),
            namespace=str(value.get("namespace") or ""),
            server_name=str(value.get("server_name") or ""),
            arguments_digest=str(value.get("arguments_digest") or ""),
            scope_digest=str(value.get("scope_digest") or ""),
            effect=PermissionDecisionEffect(str(value.get("effect") or PermissionDecisionEffect.ASK)),
            reason=str(value.get("reason") or ""),
            mode=str(value.get("mode") or "default"),
            risk=str(value.get("risk") or "unknown"),
            request_id=str(value.get("request_id") or ""),
            rule_id=str(value.get("rule_id") or ""),
            rule_source=str(value.get("rule_source") or ""),
            expiry=float(value["expiry"]) if value.get("expiry") is not None else None,
            human_intervention_count=max(0, int(value.get("human_intervention_count") or 0)),
            recovery_alternatives=tuple(str(item) for item in value.get("recovery_alternatives") or ()),
            evidence=tuple(
                PermissionDecisionEvidence.from_mapping(item)
                for item in value.get("evidence") or ()
                if isinstance(item, Mapping)
            ),
            parent_decision_id=str(value.get("parent_decision_id") or ""),
            previous_hash=str(value.get("previous_hash") or _GENESIS_HASH),
            entry_hash=str(value.get("entry_hash") or ""),
            sequence=max(0, int(value.get("sequence") or 0)),
            created_at=str(value.get("created_at") or now_iso()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class PermissionDecisionChainVerification:
    valid: bool
    session_id: str
    entry_count: int
    checked_hashes: int
    first_invalid_sequence: int | None = None
    reason: str = ""
    head_hash: str = _GENESIS_HASH

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class PermissionDecisionLog:
    """Thread-safe append-only permission decision owner."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        disabled: bool = False,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self.path = Path(path).resolve() if path is not None else None
        self.disabled = bool(disabled)
        self.clock = clock
        self._lock = RLock()
        self._entries: list[PermissionDecisionEntry] = []
        self._by_id: dict[str, PermissionDecisionEntry] = {}
        self._heads: dict[str, str] = {}
        self._sequences: dict[str, int] = {}
        self._generation = 0
        if self.path is not None and self.path.exists():
            self._load()

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise PermissionDecisionLogDisabledError("PermissionDecisionLog is disabled")

    def append(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments_digest: str,
        scope_digest: str,
        effect: PermissionDecisionEffect | str,
        reason: str,
        mode: str,
        risk: str,
        node_id: str | None = None,
        turn_id: str = "",
        namespace: str = "",
        server_name: str = "",
        request_id: str = "",
        rule_id: str = "",
        rule_source: str = "",
        expiry: float | None = None,
        human_intervention_count: int = 0,
        recovery_alternatives: Iterable[str] = (),
        evidence: Iterable[PermissionDecisionEvidence | Mapping[str, Any]] = (),
        parent_decision_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        decision_id: str | None = None,
    ) -> PermissionDecisionEntry:
        self._ensure_enabled()
        required = {
            "session_id": session_id,
            "run_id": run_id,
            "task_id": task_id,
            "worker_request_id": worker_request_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments_digest": arguments_digest,
            "scope_digest": scope_digest,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"permission decision missing: {', '.join(missing)}")
        normalized_evidence = tuple(
            item if isinstance(item, PermissionDecisionEvidence) else PermissionDecisionEvidence.from_mapping(item)
            for item in evidence
        )
        normalized_effect = PermissionDecisionEffect(str(effect))
        with self._lock:
            identifier = decision_id or new_id("permdecision")
            existing = self._by_id.get(identifier)
            if existing is not None:
                candidate = {
                    "session_id": session_id,
                    "tool_call_id": tool_call_id,
                    "arguments_digest": arguments_digest,
                    "effect": normalized_effect,
                }
                if any(getattr(existing, key) != value for key, value in candidate.items()):
                    raise ValueError(f"decision id collision: {identifier}")
                return existing
            sequence = self._sequences.get(session_id, 0) + 1
            previous_hash = self._heads.get(session_id, _GENESIS_HASH)
            entry = PermissionDecisionEntry(
                decision_id=identifier,
                session_id=session_id,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                namespace=namespace,
                server_name=server_name,
                arguments_digest=arguments_digest,
                scope_digest=scope_digest,
                effect=normalized_effect,
                reason=reason,
                mode=mode,
                risk=risk,
                request_id=request_id,
                rule_id=rule_id,
                rule_source=rule_source,
                expiry=expiry,
                human_intervention_count=human_intervention_count,
                recovery_alternatives=tuple(str(item) for item in recovery_alternatives),
                evidence=normalized_evidence,
                parent_decision_id=parent_decision_id,
                previous_hash=previous_hash,
                sequence=sequence,
                created_at=self.clock(),
                metadata=_redact(dict(metadata or {})),
            )
            hashed = PermissionDecisionEntry.from_mapping(entry.to_dict())
            self._entries.append(hashed)
            self._by_id[hashed.decision_id] = hashed
            self._heads[session_id] = hashed.entry_hash
            self._sequences[session_id] = sequence
            self._commit_locked()
            return hashed

    def append_from_mapping(self, value: Mapping[str, Any]) -> PermissionDecisionEntry:
        return self.append(
            session_id=str(value.get("session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            node_id=str(value["node_id"]) if value.get("node_id") is not None else None,
            worker_request_id=str(value.get("worker_request_id") or ""),
            turn_id=str(value.get("turn_id") or ""),
            tool_call_id=str(value.get("tool_call_id") or value.get("tool_use_id") or ""),
            tool_name=str(value.get("tool_name") or ""),
            namespace=str(value.get("namespace") or ""),
            server_name=str(value.get("server_name") or ""),
            arguments_digest=str(value.get("arguments_digest") or ""),
            scope_digest=str(value.get("scope_digest") or ""),
            effect=str(value.get("effect") or "ask"),
            reason=str(value.get("reason") or ""),
            mode=str(value.get("mode") or "default"),
            risk=str(value.get("risk") or "unknown"),
            request_id=str(value.get("request_id") or ""),
            rule_id=str(value.get("rule_id") or ""),
            rule_source=str(value.get("rule_source") or ""),
            expiry=float(value["expiry"]) if value.get("expiry") is not None else None,
            human_intervention_count=int(value.get("human_intervention_count") or 0),
            recovery_alternatives=value.get("recovery_alternatives") or (),
            evidence=value.get("evidence") or (),
            parent_decision_id=str(value.get("parent_decision_id") or ""),
            metadata=dict(value.get("metadata") or {}),
            decision_id=str(value.get("decision_id") or "") or None,
        )

    def get(self, decision_id: str) -> PermissionDecisionEntry | None:
        self._ensure_enabled()
        with self._lock:
            return self._by_id.get(decision_id)

    def list(
        self,
        *,
        session_id: str | None = None,
        tool_call_id: str | None = None,
        effects: Iterable[PermissionDecisionEffect | str] | None = None,
        request_id: str | None = None,
    ) -> tuple[PermissionDecisionEntry, ...]:
        self._ensure_enabled()
        effect_set = {PermissionDecisionEffect(str(item)) for item in effects} if effects is not None else None
        with self._lock:
            return tuple(
                entry
                for entry in self._entries
                if (session_id is None or entry.session_id == session_id)
                and (tool_call_id is None or entry.tool_call_id == tool_call_id)
                and (effect_set is None or entry.effect in effect_set)
                and (request_id is None or entry.request_id == request_id)
            )

    def latest_for_tool_call(
        self,
        session_id: str,
        tool_call_id: str,
    ) -> PermissionDecisionEntry | None:
        entries = self.list(session_id=session_id, tool_call_id=tool_call_id)
        return entries[-1] if entries else None

    def verify(self, session_id: str) -> PermissionDecisionChainVerification:
        self._ensure_enabled()
        with self._lock:
            entries = [item for item in self._entries if item.session_id == session_id]
        previous = _GENESIS_HASH
        checked = 0
        for expected_sequence, entry in enumerate(entries, start=1):
            if entry.sequence != expected_sequence:
                return PermissionDecisionChainVerification(
                    False,
                    session_id,
                    len(entries),
                    checked,
                    entry.sequence,
                    "non-contiguous permission decision sequence",
                    previous,
                )
            if entry.previous_hash != previous:
                return PermissionDecisionChainVerification(
                    False,
                    session_id,
                    len(entries),
                    checked,
                    entry.sequence,
                    "permission decision previous hash mismatch",
                    previous,
                )
            calculated = entry.calculate_hash()
            if calculated != entry.entry_hash:
                return PermissionDecisionChainVerification(
                    False,
                    session_id,
                    len(entries),
                    checked,
                    entry.sequence,
                    "permission decision entry hash mismatch",
                    previous,
                )
            previous = calculated
            checked += 1
        return PermissionDecisionChainVerification(
            True,
            session_id,
            len(entries),
            checked,
            head_hash=previous,
        )

    def snapshot(self, *, session_id: str | None = None) -> dict[str, Any]:
        self._ensure_enabled()
        with self._lock:
            entries = [
                entry.to_dict()
                for entry in self._entries
                if session_id is None or entry.session_id == session_id
            ]
            return {
                "schema_version": PERMISSION_DECISION_LOG_SCHEMA_VERSION,
                "generation": self._generation,
                "session_id": session_id,
                "entries": entries,
                "heads": dict(self._heads),
                "sequences": dict(self._sequences),
            }

    def restore(
        self,
        snapshot: Mapping[str, Any],
        *,
        session_id: str | None = None,
    ) -> int:
        self._ensure_enabled()
        if int(snapshot.get("schema_version") or 0) != PERMISSION_DECISION_LOG_SCHEMA_VERSION:
            raise ValueError("unsupported permission decision log snapshot schema")
        incoming = tuple(
            PermissionDecisionEntry.from_mapping(item)
            for item in snapshot.get("entries") or ()
            if isinstance(item, Mapping)
        )
        if session_id and any(item.session_id != session_id for item in incoming):
            raise ValueError("permission decision snapshot contains a different session")
        with self._lock:
            added = 0
            for entry in incoming:
                existing = self._by_id.get(entry.decision_id)
                if existing:
                    if existing.entry_hash != entry.entry_hash:
                        raise ValueError(f"permission decision restore collision: {entry.decision_id}")
                    continue
                expected_sequence = self._sequences.get(entry.session_id, 0) + 1
                expected_previous = self._heads.get(entry.session_id, _GENESIS_HASH)
                if entry.sequence != expected_sequence or entry.previous_hash != expected_previous:
                    raise ValueError("permission decision restore is not a contiguous hash-chain suffix")
                if entry.calculate_hash() != entry.entry_hash:
                    raise ValueError(f"permission decision restore hash mismatch: {entry.decision_id}")
                self._entries.append(entry)
                self._by_id[entry.decision_id] = entry
                self._heads[entry.session_id] = entry.entry_hash
                self._sequences[entry.session_id] = entry.sequence
                added += 1
            if added:
                self._commit_locked()
            return added

    def metrics(self, *, session_id: str | None = None) -> dict[str, int]:
        entries = self.list(session_id=session_id)
        effects = {str(effect): 0 for effect in PermissionDecisionEffect}
        for entry in entries:
            effects[str(entry.effect)] += 1
        return {
            **effects,
            "total": len(entries),
            "sessions": len({entry.session_id for entry in entries}),
            "with_request": sum(1 for entry in entries if entry.request_id),
            "with_recovery": sum(1 for entry in entries if entry.recovery_alternatives),
        }

    def _document_locked(self) -> dict[str, Any]:
        return {
            "schema_version": PERMISSION_DECISION_LOG_SCHEMA_VERSION,
            "generation": self._generation,
            "entries": [entry.to_dict() for entry in self._entries],
            "heads": dict(self._heads),
            "sequences": dict(self._sequences),
        }

    def _commit_locked(self) -> None:
        self._generation += 1
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{self._generation}.tmp")
        raw = json.dumps(self._document_locked(), ensure_ascii=False, indent=2, sort_keys=True)
        try:
            temporary.write_text(raw, encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> None:
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermissionDecisionLogCorruptError(
                f"cannot load permission decision log: {type(exc).__name__}"
            ) from exc
        if int(raw.get("schema_version") or 0) != PERMISSION_DECISION_LOG_SCHEMA_VERSION:
            raise PermissionDecisionLogCorruptError("unsupported permission decision log schema")
        values = raw.get("entries") or ()
        if not isinstance(values, Sequence):
            raise PermissionDecisionLogCorruptError("permission decision entries must be an array")
        try:
            entries = [
                PermissionDecisionEntry.from_mapping(item)
                for item in values
                if isinstance(item, Mapping)
            ]
        except (TypeError, ValueError, KeyError) as exc:
            raise PermissionDecisionLogCorruptError(f"invalid permission decision entry: {exc}") from exc
        self._entries = entries
        self._by_id = {entry.decision_id: entry for entry in entries}
        self._generation = max(0, int(raw.get("generation") or 0))
        self._heads = {}
        self._sequences = {}
        for entry in entries:
            self._heads[entry.session_id] = entry.entry_hash
            self._sequences[entry.session_id] = entry.sequence
        for session_id in self._sequences:
            verification = self.verify(session_id)
            if not verification.valid:
                raise PermissionDecisionLogCorruptError(
                    f"permission decision chain invalid at {verification.first_invalid_sequence}: {verification.reason}"
                )


def _redact(value: Any, *, key: str = "") -> Any:
    normalized = key.lower().replace("-", "_")
    if normalized in _SECRET_KEYS or any(token in normalized for token in ("password", "secret", "token", "credential")):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item) for item in value]
    return to_jsonable(value)
