from __future__ import annotations

"""Bounded transcript eviction and exact logical subagent resume capsules."""

import copy
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import new_id, now_iso

from .transcript import SubagentTranscriptStore


class ResumeCapsuleError(RuntimeError):
    pass


class ResumeCapsuleDisabled(ResumeCapsuleError):
    pass


class ResumeCapsuleConflict(ResumeCapsuleError):
    pass


class ResumeCapsuleTampered(ResumeCapsuleError):
    pass


class ResumeCapsuleTooLarge(ResumeCapsuleError):
    pass


class CapsuleState(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    EVICTED = "evicted"
    RESUMED = "resumed"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class ResumeCapsuleBudget:
    maximum_entries: int = 256
    maximum_chars: int = 512_000
    maximum_summary_chars: int = 32_000
    maximum_pending_messages: int = 64
    maximum_artifact_refs: int = 256
    maximum_evidence_refs: int = 512
    maximum_replacement_refs: int = 512
    maximum_metadata_keys: int = 128

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")

    def to_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class CapsuleTranscriptEntry:
    sequence: int
    kind: str
    role: str
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    tool_use_id: str = ""
    digest: str = ""
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("capsule transcript sequence must be positive")
        object.__setattr__(self, "summary", str(self.summary))
        object.__setattr__(self, "data", _safe_mapping(self.data))
        object.__setattr__(self, "artifact_refs", _unique_strings(self.artifact_refs))
        object.__setattr__(self, "evidence_refs", _unique_strings(self.evidence_refs))

    @property
    def computed_digest(self) -> str:
        return _digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "sequence": self.sequence,
            "kind": self.kind,
            "role": self.role,
            "summary": self.summary,
            "data": _safe_mapping(self.data),
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "tool_use_id": self.tool_use_id,
            "created_at": self.created_at,
        }
        if include_digest:
            value["digest"] = self.digest or _digest(value)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapsuleTranscriptEntry":
        return cls(
            sequence=max(1, int(value.get("sequence") or 1)),
            kind=str(value.get("kind") or "metadata"),
            role=str(value.get("role") or "system"),
            summary=str(value.get("summary") or ""),
            data=_safe_mapping(value.get("data")),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
            tool_use_id=str(value.get("tool_use_id") or ""),
            digest=str(value.get("digest") or ""),
            created_at=str(value.get("created_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class ResumeCapsule:
    capsule_id: str
    task_id: str
    parent_task_id: str
    run_id: str
    parent_session_id: str
    child_session_id: str
    execution_ref: str
    attempt: int
    state: CapsuleState
    transcript_leaf_digest: str
    transcript_sequence: int
    entries: tuple[CapsuleTranscriptEntry, ...]
    summary: str
    context_epoch: int
    compact_boundary_id: str
    content_replacement_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    invoked_skill_refs: tuple[str, ...]
    pending_messages: tuple[Mapping[str, Any], ...]
    execution_receipt_id: str
    execution_receipt_digest: str
    yield_assembly_digest: str = ""
    previous_capsule_id: str = ""
    resume_count: int = 0
    revision: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    signature: str = ""

    def __post_init__(self) -> None:
        for name in (
            "capsule_id",
            "task_id",
            "parent_task_id",
            "run_id",
            "parent_session_id",
            "child_session_id",
            "execution_ref",
            "transcript_leaf_digest",
            "execution_receipt_id",
            "execution_receipt_digest",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.attempt <= 0 or self.transcript_sequence < 0 or self.context_epoch < 0:
            raise ValueError("capsule counters cannot be negative")
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "content_replacement_refs", _unique_strings(self.content_replacement_refs))
        object.__setattr__(self, "artifact_refs", _unique_strings(self.artifact_refs))
        object.__setattr__(self, "evidence_refs", _unique_strings(self.evidence_refs))
        object.__setattr__(self, "invoked_skill_refs", _unique_strings(self.invoked_skill_refs))
        object.__setattr__(self, "pending_messages", tuple(_safe_mapping(item) for item in self.pending_messages))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    @property
    def unsigned_digest(self) -> str:
        return _digest(self.to_dict(include_signature=False))

    def to_dict(self, *, include_signature: bool = True) -> dict[str, Any]:
        value = {
            "capsule_id": self.capsule_id,
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "run_id": self.run_id,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "execution_ref": self.execution_ref,
            "attempt": self.attempt,
            "state": self.state.value,
            "transcript_leaf_digest": self.transcript_leaf_digest,
            "transcript_sequence": self.transcript_sequence,
            "entries": [item.to_dict() for item in self.entries],
            "summary": self.summary,
            "context_epoch": self.context_epoch,
            "compact_boundary_id": self.compact_boundary_id,
            "content_replacement_refs": list(self.content_replacement_refs),
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "invoked_skill_refs": list(self.invoked_skill_refs),
            "pending_messages": [_safe_mapping(item) for item in self.pending_messages],
            "execution_receipt_id": self.execution_receipt_id,
            "execution_receipt_digest": self.execution_receipt_digest,
            "yield_assembly_digest": self.yield_assembly_digest,
            "previous_capsule_id": self.previous_capsule_id,
            "resume_count": self.resume_count,
            "revision": self.revision,
            "metadata": _safe_mapping(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_signature:
            value["signature"] = self.signature
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResumeCapsule":
        return cls(
            capsule_id=str(value.get("capsule_id") or ""),
            task_id=str(value.get("task_id") or ""),
            parent_task_id=str(value.get("parent_task_id") or ""),
            run_id=str(value.get("run_id") or ""),
            parent_session_id=str(value.get("parent_session_id") or ""),
            child_session_id=str(value.get("child_session_id") or ""),
            execution_ref=str(value.get("execution_ref") or ""),
            attempt=max(1, int(value.get("attempt") or 1)),
            state=CapsuleState(str(value.get("state") or CapsuleState.PREPARED.value)),
            transcript_leaf_digest=str(value.get("transcript_leaf_digest") or ""),
            transcript_sequence=max(0, int(value.get("transcript_sequence") or 0)),
            entries=tuple(
                CapsuleTranscriptEntry.from_dict(item)
                for item in value.get("entries") or ()
                if isinstance(item, Mapping)
            ),
            summary=str(value.get("summary") or ""),
            context_epoch=max(0, int(value.get("context_epoch") or 0)),
            compact_boundary_id=str(value.get("compact_boundary_id") or ""),
            content_replacement_refs=tuple(str(item) for item in value.get("content_replacement_refs") or ()),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
            invoked_skill_refs=tuple(str(item) for item in value.get("invoked_skill_refs") or ()),
            pending_messages=tuple(
                _safe_mapping(item) for item in value.get("pending_messages") or () if isinstance(item, Mapping)
            ),
            execution_receipt_id=str(value.get("execution_receipt_id") or ""),
            execution_receipt_digest=str(value.get("execution_receipt_digest") or ""),
            yield_assembly_digest=str(value.get("yield_assembly_digest") or ""),
            previous_capsule_id=str(value.get("previous_capsule_id") or ""),
            resume_count=max(0, int(value.get("resume_count") or 0)),
            revision=max(0, int(value.get("revision") or 0)),
            metadata=_safe_mapping(value.get("metadata")),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
            signature=str(value.get("signature") or ""),
        )


@dataclass(frozen=True, slots=True)
class ResumeMaterial:
    capsule: ResumeCapsule
    messages: tuple[Mapping[str, Any], ...]
    context: Mapping[str, Any]
    pending_messages: tuple[Mapping[str, Any], ...]
    resume_idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capsule": self.capsule.to_dict(),
            "messages": [_safe_mapping(item) for item in self.messages],
            "context": _safe_mapping(self.context),
            "pending_messages": [_safe_mapping(item) for item in self.pending_messages],
            "resume_idempotency_key": self.resume_idempotency_key,
        }


class TranscriptCapsuleBuilder:
    def __init__(self, budget: ResumeCapsuleBudget | None = None, *, disabled: bool = False) -> None:
        self.budget = budget or ResumeCapsuleBudget()
        self.disabled = bool(disabled)

    def build_entries(self, transcript: Sequence[Any]) -> tuple[CapsuleTranscriptEntry, ...]:
        if self.disabled:
            raise ResumeCapsuleDisabled("TranscriptCapsuleBuilder is disabled")
        selected: list[CapsuleTranscriptEntry] = []
        total_chars = 0
        tool_uses: dict[str, int] = {}
        for index, value in enumerate(transcript, start=1):
            raw = _object_mapping(value)
            kind = str(raw.get("kind") or raw.get("entry_kind") or "metadata")
            payload = _mapping(raw.get("payload")) or raw
            sequence = max(1, int(raw.get("sequence") or index))
            role = self._role(kind)
            tool_use_id = str(payload.get("tool_use_id") or payload.get("toolUseId") or "")
            if kind == "tool_use" and tool_use_id:
                tool_uses[tool_use_id] = sequence
            if kind == "tool_result" and tool_use_id and tool_use_id not in tool_uses:
                continue
            entry = CapsuleTranscriptEntry(
                sequence=sequence,
                kind=kind,
                role=role,
                summary=self._summary(kind, payload),
                data=self._bounded_data(kind, payload),
                artifact_refs=self._references(payload, "artifact"),
                evidence_refs=self._references(payload, "evidence"),
                tool_use_id=tool_use_id,
                created_at=str(raw.get("created_at") or now_iso()),
            )
            entry = replace(entry, digest=entry.computed_digest)
            size = _char_size(entry.to_dict())
            if size > self.budget.maximum_chars:
                continue
            selected.append(entry)
            total_chars += size
            while len(selected) > self.budget.maximum_entries or total_chars > self.budget.maximum_chars:
                removed = self._removable_index(selected)
                if removed is None:
                    raise ResumeCapsuleTooLarge("atomic transcript groups exceed capsule budget")
                total_chars -= _char_size(selected[removed].to_dict())
                selected.pop(removed)
        self._assert_tool_atomicity(selected)
        return tuple(selected)

    def summarize(self, entries: Sequence[CapsuleTranscriptEntry]) -> str:
        important = []
        for entry in entries:
            if entry.kind in {"user", "assistant", "failure", "handoff", "continuation", "cancel"}:
                important.append(f"[{entry.sequence}:{entry.kind}] {entry.summary}")
        summary = "\n".join(important)
        if len(summary) <= self.budget.maximum_summary_chars:
            return summary
        head = summary[: self.budget.maximum_summary_chars // 3]
        tail = summary[-(self.budget.maximum_summary_chars - len(head) - 32) :]
        return head + "\n… capsule summary truncated …\n" + tail

    def verify_leaf(self, entries: Sequence[CapsuleTranscriptEntry], expected: str) -> None:
        actual = self.leaf_digest(entries)
        if not hmac.compare_digest(actual, expected):
            raise ResumeCapsuleTampered("transcript leaf digest mismatch")

    @staticmethod
    def leaf_digest(entries: Sequence[CapsuleTranscriptEntry]) -> str:
        chain = "sha256:" + "0" * 64
        for entry in entries:
            chain = _digest({"previous": chain, "entry": entry.to_dict()})
        return chain

    @staticmethod
    def _role(kind: str) -> str:
        if kind == "user":
            return "user"
        if kind in {"assistant", "handoff", "progress"}:
            return "assistant"
        if kind in {"tool_use", "tool_result"}:
            return "tool"
        return "system"

    def _summary(self, kind: str, payload: Mapping[str, Any]) -> str:
        candidates = (
            payload.get("summary"),
            payload.get("content"),
            payload.get("reason"),
            payload.get("error_message"),
            payload.get("error"),
        )
        selected = next((str(item) for item in candidates if item not in (None, "")), kind)
        selected = selected.replace("\x1b", "").replace("\x00", "")
        maximum = min(4096, self.budget.maximum_summary_chars)
        return selected if len(selected) <= maximum else selected[: maximum - 1] + "…"

    def _bounded_data(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed_by_kind = {
            "tool_use": {"tool", "name", "tool_use_id", "input_digest", "arguments_preview"},
            "tool_result": {"tool", "name", "tool_use_id", "ok", "output_digest", "artifact_refs", "truncated"},
            "progress": {"current", "total", "activity", "usage"},
            "failure": {"error_code", "retryable", "recovery_signal"},
            "handoff": {"status", "artifact_refs", "evidence_refs", "next_actions", "usage"},
            "continuation": {"message_id", "intent", "summary", "sender_task_id"},
        }
        allowed = allowed_by_kind.get(kind, {"status", "summary", "digest", "objective_digest"})
        selected = {str(key): value for key, value in payload.items() if str(key) in allowed}
        return _safe_mapping(selected)

    @staticmethod
    def _references(payload: Mapping[str, Any], prefix: str) -> tuple[str, ...]:
        values = []
        for key, item in payload.items():
            if prefix not in str(key).casefold():
                continue
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                values.extend(str(value) for value in item)
            elif item:
                values.append(str(item))
        return _unique_strings(values)

    @staticmethod
    def _removable_index(entries: Sequence[CapsuleTranscriptEntry]) -> int | None:
        protected = {"user", "failure", "handoff", "cancel", "continuation"}
        for index, entry in enumerate(entries):
            if entry.kind not in protected and not entry.tool_use_id:
                return index
        for index, entry in enumerate(entries):
            if entry.kind == "progress":
                return index
        return None

    @staticmethod
    def _assert_tool_atomicity(entries: Sequence[CapsuleTranscriptEntry]) -> None:
        groups: dict[str, set[str]] = {}
        for entry in entries:
            if entry.tool_use_id:
                groups.setdefault(entry.tool_use_id, set()).add(entry.kind)
        broken = sorted(key for key, kinds in groups.items() if kinds != {"tool_use", "tool_result"})
        if broken:
            raise ResumeCapsuleConflict("capsule contains non-atomic tool groups: " + ", ".join(broken))


class ResumeCapsuleStore:
    schema = "zyra.subagent-resume-capsules/v1"

    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path = self.path.with_suffix(self.path.suffix + ".key")
        self.disabled = bool(disabled)
        self._lock = RLock()
        self._capsules: dict[str, ResumeCapsule] = {}
        self._latest_by_task: dict[str, str] = {}
        self._resume_idempotency: dict[str, str] = {}
        self._secret = self._load_or_create_secret()
        self._load()

    def prepare(self, capsule: ResumeCapsule) -> ResumeCapsule:
        self._require_enabled()
        with self._lock:
            if capsule.capsule_id in self._capsules:
                existing = self._capsules[capsule.capsule_id]
                if existing.unsigned_digest != capsule.unsigned_digest:
                    raise ResumeCapsuleConflict("capsule id already exists with different content")
                return copy.deepcopy(existing)
            latest_id = self._latest_by_task.get(capsule.task_id)
            changed = replace(
                capsule,
                state=CapsuleState.PREPARED,
                previous_capsule_id=capsule.previous_capsule_id or str(latest_id or ""),
                signature="",
            )
            changed = replace(changed, signature=self._sign(changed))
            self._capsules[changed.capsule_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def commit(self, capsule_id: str) -> ResumeCapsule:
        return self._transition(capsule_id, CapsuleState.COMMITTED)

    def mark_evicted(self, capsule_id: str) -> ResumeCapsule:
        return self._transition(capsule_id, CapsuleState.EVICTED)

    def resume(self, capsule_id: str, *, idempotency_key: str) -> ResumeCapsule:
        self._require_enabled()
        with self._lock:
            existing_id = self._resume_idempotency.get(idempotency_key)
            if existing_id:
                existing = self._capsules[existing_id]
                if existing.capsule_id != capsule_id:
                    raise ResumeCapsuleConflict("resume idempotency key points to another capsule")
                return copy.deepcopy(existing)
            current = self._verified(capsule_id)
            if current.state not in {CapsuleState.COMMITTED, CapsuleState.EVICTED, CapsuleState.RESUMED}:
                raise ResumeCapsuleConflict(f"capsule cannot resume from state {current.state.value}")
            changed = replace(
                current,
                state=CapsuleState.RESUMED,
                resume_count=current.resume_count + 1,
                revision=current.revision + 1,
                updated_at=now_iso(),
                signature="",
            )
            changed = replace(changed, signature=self._sign(changed))
            self._capsules[capsule_id] = changed
            self._resume_idempotency[idempotency_key] = capsule_id
            self._persist()
            return copy.deepcopy(changed)

    def revoke(self, capsule_id: str, *, reason: str) -> ResumeCapsule:
        return self._transition(capsule_id, CapsuleState.REVOKED, metadata={"revoke_reason": reason})

    def supersede(self, capsule_id: str, *, replacement_id: str) -> ResumeCapsule:
        return self._transition(capsule_id, CapsuleState.SUPERSEDED, metadata={"replacement_id": replacement_id})

    def get(self, capsule_id: str) -> ResumeCapsule:
        self._require_enabled()
        with self._lock:
            return copy.deepcopy(self._verified(capsule_id))

    def latest(self, task_id: str) -> ResumeCapsule:
        self._require_enabled()
        with self._lock:
            capsule_id = self._latest_by_task.get(task_id)
            if not capsule_id:
                raise ResumeCapsuleConflict(f"no resume capsule exists for {task_id}")
            return copy.deepcopy(self._verified(capsule_id))

    def list(self, *, task_id: str | None = None) -> tuple[ResumeCapsule, ...]:
        self._require_enabled()
        with self._lock:
            values = [
                copy.deepcopy(item)
                for item in self._capsules.values()
                if task_id is None or item.task_id == task_id
            ]
            return tuple(sorted(values, key=lambda item: (item.created_at, item.capsule_id)))

    def _transition(
        self,
        capsule_id: str,
        state: CapsuleState,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> ResumeCapsule:
        self._require_enabled()
        with self._lock:
            current = self._verified(capsule_id)
            allowed = {
                CapsuleState.PREPARED: {CapsuleState.COMMITTED, CapsuleState.REVOKED},
                CapsuleState.COMMITTED: {CapsuleState.EVICTED, CapsuleState.RESUMED, CapsuleState.SUPERSEDED, CapsuleState.REVOKED},
                CapsuleState.EVICTED: {CapsuleState.RESUMED, CapsuleState.SUPERSEDED, CapsuleState.REVOKED},
                CapsuleState.RESUMED: {CapsuleState.EVICTED, CapsuleState.SUPERSEDED, CapsuleState.REVOKED},
                CapsuleState.SUPERSEDED: set(),
                CapsuleState.REVOKED: set(),
            }
            if state is not current.state and state not in allowed[current.state]:
                raise ResumeCapsuleConflict(f"invalid capsule transition {current.state.value}->{state.value}")
            changed = replace(
                current,
                state=state,
                revision=current.revision + 1,
                metadata={**current.metadata, **_safe_mapping(metadata)},
                updated_at=now_iso(),
                signature="",
            )
            changed = replace(changed, signature=self._sign(changed))
            self._capsules[capsule_id] = changed
            if state not in {CapsuleState.SUPERSEDED, CapsuleState.REVOKED}:
                self._latest_by_task[changed.task_id] = capsule_id
            self._persist()
            return copy.deepcopy(changed)

    def _verified(self, capsule_id: str) -> ResumeCapsule:
        value = self._capsules.get(capsule_id)
        if value is None:
            raise ResumeCapsuleConflict(f"resume capsule not found: {capsule_id}")
        expected = self._sign(replace(value, signature=""))
        if not value.signature or not hmac.compare_digest(value.signature, expected):
            raise ResumeCapsuleTampered("resume capsule signature mismatch")
        for entry in value.entries:
            if entry.digest and not hmac.compare_digest(entry.digest, entry.computed_digest):
                raise ResumeCapsuleTampered(f"resume capsule entry {entry.sequence} digest mismatch")
        TranscriptCapsuleBuilder().verify_leaf(value.entries, value.transcript_leaf_digest)
        return value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            body = {
                "schema": self.schema,
                "owner": "M1-03D ResumeCapsuleStore",
                "capsules": [item.to_dict() for item in sorted(self._capsules.values(), key=lambda value: value.capsule_id)],
                "latest_by_task": dict(sorted(self._latest_by_task.items())),
                "resume_idempotency": dict(sorted(self._resume_idempotency.items())),
            }
            return {**body, "checksum": _digest(body)}

    def _sign(self, capsule: ResumeCapsule) -> str:
        payload = json.dumps(capsule.to_dict(include_signature=False), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "hmac-sha256:" + hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def _load_or_create_secret(self) -> bytes:
        if self.key_path.exists():
            value = self.key_path.read_bytes()
            if len(value) < 32:
                raise ResumeCapsuleError("resume capsule signing key is invalid")
            return value
        value = secrets.token_bytes(64)
        self.key_path.write_bytes(value)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return value

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResumeCapsuleError(f"cannot load resume capsule store: {error}") from error
        if not isinstance(value, Mapping):
            raise ResumeCapsuleError("resume capsule store root must be an object")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        expected = str(value.get("checksum") or "")
        if expected and not hmac.compare_digest(expected, _digest(body)):
            raise ResumeCapsuleTampered("resume capsule store checksum mismatch")
        for item in value.get("capsules") or ():
            if isinstance(item, Mapping):
                capsule = ResumeCapsule.from_dict(item)
                self._capsules[capsule.capsule_id] = capsule
        raw_latest = value.get("latest_by_task")
        if isinstance(raw_latest, Mapping):
            self._latest_by_task = {str(key): str(item) for key, item in raw_latest.items()}
        raw_idempotency = value.get("resume_idempotency")
        if isinstance(raw_idempotency, Mapping):
            self._resume_idempotency = {str(key): str(item) for key, item in raw_idempotency.items()}
        for capsule_id in self._capsules:
            self._verified(capsule_id)

    def _persist(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise ResumeCapsuleDisabled("ResumeCapsuleStore is disabled")


class ResumeCapsuleRuntime:
    def __init__(
        self,
        store: ResumeCapsuleStore,
        transcript_store: SubagentTranscriptStore,
        *,
        builder: TranscriptCapsuleBuilder | None = None,
        disabled: bool = False,
    ) -> None:
        self.store = store
        self.transcript_store = transcript_store
        self.builder = builder or TranscriptCapsuleBuilder()
        self.disabled = bool(disabled)

    def create(
        self,
        *,
        task_record: Any,
        execution_receipt: Mapping[str, Any],
        yield_assembly: Mapping[str, Any] | None = None,
        pending_messages: Sequence[Mapping[str, Any]] = (),
    ) -> ResumeCapsule:
        self._require_enabled()
        transcript = self.transcript_store.require_consistent(task_record.task_id).entries
        entries = self.builder.build_entries(transcript)
        leaf = self.builder.leaf_digest(entries)
        context = task_record.context_snapshot
        metadata = _safe_mapping(getattr(context, "metadata", {}))
        artifacts = tuple(getattr(context, "artifact_refs", ()) or metadata.get("artifact_refs") or ())
        evidence = tuple(getattr(context, "evidence_refs", ()) or metadata.get("evidence_refs") or ())
        receipt_id = str(execution_receipt.get("receipt_id") or "")
        receipt_digest = _digest(execution_receipt)
        assembly_digest = _digest(yield_assembly) if yield_assembly else ""
        capsule = ResumeCapsule(
            capsule_id=new_id("resumecapsule"),
            task_id=task_record.task_id,
            parent_task_id=task_record.parent_task_id,
            run_id=task_record.run_id,
            parent_session_id=task_record.parent_session_id,
            child_session_id=str(task_record.metadata.get("child_session_id") or ""),
            execution_ref=str(task_record.execution_ref or execution_receipt.get("execution_ref") or ""),
            attempt=max(1, int(task_record.attempt or execution_receipt.get("attempt") or 1)),
            state=CapsuleState.PREPARED,
            transcript_leaf_digest=leaf,
            transcript_sequence=max((item.sequence for item in entries), default=0),
            entries=entries,
            summary=self.builder.summarize(entries),
            context_epoch=max(0, int(getattr(context, "context_epoch", 0))),
            compact_boundary_id=str(getattr(context, "compact_boundary_id", "")),
            content_replacement_refs=tuple(getattr(context, "content_replacement_refs", ()) or ()),
            artifact_refs=artifacts,
            evidence_refs=evidence,
            invoked_skill_refs=tuple(getattr(context, "invoked_skill_refs", ()) or ()),
            pending_messages=tuple(pending_messages),
            execution_receipt_id=receipt_id,
            execution_receipt_digest=receipt_digest,
            yield_assembly_digest=assembly_digest,
            metadata={
                "agent_type": task_record.agent_type,
                "definition_id": task_record.definition_id,
                "tool_scope_digest": str(getattr(task_record.tool_scope, "digest", "")),
                "permission_digest": _digest(task_record.permission.to_dict()),
                "raw_transcript_injected": False,
            },
        )
        prepared = self.store.prepare(capsule)
        return self.store.commit(prepared.capsule_id)

    def evict(self, task_id: str, *, capsule_id: str | None = None) -> ResumeCapsule:
        self._require_enabled()
        capsule = self.store.get(capsule_id) if capsule_id else self.store.latest(task_id)
        if capsule.task_id != task_id:
            raise ResumeCapsuleConflict("capsule is bound to another task")
        if capsule.state not in {CapsuleState.COMMITTED, CapsuleState.RESUMED, CapsuleState.EVICTED}:
            raise ResumeCapsuleConflict("capsule must be committed before transcript eviction")
        path = self.transcript_store.path(task_id)
        if path.exists():
            tombstone = path.with_suffix(path.suffix + ".evicted")
            tombstone.write_text(json.dumps({
                "schema": "zyra.transcript-eviction/v1",
                "task_id": task_id,
                "capsule_id": capsule.capsule_id,
                "leaf_digest": capsule.transcript_leaf_digest,
                "sequence": capsule.transcript_sequence,
                "evicted_at": now_iso(),
            }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            path.unlink()
        return self.store.mark_evicted(capsule.capsule_id)

    def materialize(self, capsule_id: str, *, idempotency_key: str) -> ResumeMaterial:
        self._require_enabled()
        capsule = self.store.resume(capsule_id, idempotency_key=idempotency_key)
        self.builder.verify_leaf(capsule.entries, capsule.transcript_leaf_digest)
        messages = tuple(self._message(entry) for entry in capsule.entries)
        context = {
            "context_epoch": capsule.context_epoch,
            "compact_boundary_id": capsule.compact_boundary_id,
            "content_replacement_refs": list(capsule.content_replacement_refs),
            "artifact_refs": list(capsule.artifact_refs),
            "evidence_refs": list(capsule.evidence_refs),
            "invoked_skill_refs": list(capsule.invoked_skill_refs),
            "summary": capsule.summary,
            "transcript_leaf_digest": capsule.transcript_leaf_digest,
            "raw_transcript_injected": False,
        }
        return ResumeMaterial(
            capsule=capsule,
            messages=messages,
            context=context,
            pending_messages=capsule.pending_messages,
            resume_idempotency_key=idempotency_key,
        )

    @staticmethod
    def _message(entry: CapsuleTranscriptEntry) -> Mapping[str, Any]:
        return {
            "role": entry.role,
            "content": entry.summary,
            "metadata": {
                "capsule_sequence": entry.sequence,
                "entry_kind": entry.kind,
                "entry_digest": entry.digest,
                "tool_use_id": entry.tool_use_id,
                "artifact_refs": list(entry.artifact_refs),
                "evidence_refs": list(entry.evidence_refs),
                "bounded_resume": True,
            },
        }

    def _require_enabled(self) -> None:
        if self.disabled:
            raise ResumeCapsuleDisabled("ResumeCapsuleRuntime is disabled")


def _object_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for name in ("safe_dict", "to_dict"):
        method = getattr(value, name, None)
        if callable(method):
            selected = method()
            if isinstance(selected, Mapping):
                return dict(selected)
    return {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        lowered = name.casefold()
        if any(token in lowered for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        elif isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            result[name] = [_safe_value(entry) for entry in item]
        else:
            result[name] = _safe_value(item)
    if len(result) > ResumeCapsuleBudget().maximum_metadata_keys:
        result = dict(list(sorted(result.items()))[: ResumeCapsuleBudget().maximum_metadata_keys])
    return result


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    return str(value)


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result = []
    for value in values:
        selected = str(value).strip()
        if not selected or selected in seen:
            continue
        seen.add(selected)
        result.append(selected)
    return tuple(result)


def _char_size(value: Any) -> int:
    return len(json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True))


def _digest(value: Any) -> str:
    payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


__all__ = [
    "CapsuleState",
    "CapsuleTranscriptEntry",
    "ResumeCapsule",
    "ResumeCapsuleBudget",
    "ResumeCapsuleConflict",
    "ResumeCapsuleDisabled",
    "ResumeCapsuleError",
    "ResumeCapsuleRuntime",
    "ResumeCapsuleStore",
    "ResumeCapsuleTampered",
    "ResumeCapsuleTooLarge",
    "ResumeMaterial",
    "TranscriptCapsuleBuilder",
]
