from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import now_iso

from .digests import digest_object
from .errors import TranscriptIntegrityError
from .models import TranscriptEntry, TranscriptEntryKind


@dataclass(frozen=True, slots=True)
class TranscriptReplay:
    task_id: str
    entries: tuple[TranscriptEntry, ...]
    leaf_entry_id: str
    consistent: bool
    findings: tuple[dict[str, Any], ...]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "entries": [item.to_dict() for item in self.entries],
            "leaf_entry_id": self.leaf_entry_id,
            "consistent": self.consistent,
            "findings": [dict(item) for item in self.findings],
            "digest": self.digest,
        }


class SubagentTranscriptStore:
    """Append-only child sidechain with parent-link integrity checks."""

    def __init__(self, root: str | Path, *, disabled: bool = False) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.disabled = disabled
        self._lock = RLock()

    def initialize(
        self,
        task_id: str,
        *,
        context_payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> TranscriptEntry:
        if self.path(task_id).exists():
            replay = self.replay(task_id)
            if replay.entries:
                return replay.entries[0]
        return self.append(
            task_id,
            TranscriptEntryKind.TASK_CREATED,
            {
                "context": dict(context_payload),
                "metadata": dict(metadata),
                "sidechain": True,
                "parent_transcript_mutated": False,
            },
        )

    def append(
        self,
        task_id: str,
        kind: TranscriptEntryKind,
        payload: Mapping[str, Any],
        *,
        expected_sequence: int | None = None,
    ) -> TranscriptEntry:
        self._require_enabled()
        with self._lock:
            entries = self._read(task_id)
            sequence = len(entries) + 1
            if expected_sequence is not None and expected_sequence != sequence:
                raise TranscriptIntegrityError(
                    "transcript sequence conflict",
                    task_id=task_id,
                    expected=expected_sequence,
                    actual=sequence,
                )
            parent_entry_id = entries[-1].entry_id if entries else ""
            entry = TranscriptEntry(
                task_id=task_id,
                kind=kind,
                payload=dict(payload),
                sequence=sequence,
                parent_entry_id=parent_entry_id,
            )
            path = self.path(task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            return entry

    def append_many(
        self,
        task_id: str,
        records: Iterable[tuple[TranscriptEntryKind, Mapping[str, Any]]],
    ) -> tuple[TranscriptEntry, ...]:
        result = []
        for kind, payload in records:
            result.append(self.append(task_id, kind, payload))
        return tuple(result)

    def replay(self, task_id: str) -> TranscriptReplay:
        self._require_enabled()
        with self._lock:
            entries = self._read(task_id)
        findings: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous = ""
        for index, entry in enumerate(entries, start=1):
            if entry.sequence != index:
                findings.append({
                    "code": "transcript_sequence_gap",
                    "expected": index,
                    "actual": entry.sequence,
                    "entry_id": entry.entry_id,
                })
            if entry.entry_id in seen_ids:
                findings.append({"code": "transcript_duplicate_entry", "entry_id": entry.entry_id})
            seen_ids.add(entry.entry_id)
            if entry.parent_entry_id != previous:
                findings.append({
                    "code": "transcript_parent_mismatch",
                    "entry_id": entry.entry_id,
                    "expected_parent": previous,
                    "actual_parent": entry.parent_entry_id,
                })
            expected_digest = TranscriptEntry(
                task_id=entry.task_id,
                kind=entry.kind,
                payload=entry.payload,
                sequence=entry.sequence,
                entry_id=entry.entry_id,
                parent_entry_id=entry.parent_entry_id,
                created_at=entry.created_at,
            ).digest
            if entry.digest != expected_digest:
                findings.append({"code": "transcript_digest_mismatch", "entry_id": entry.entry_id})
            previous = entry.entry_id
        consistent = not findings
        return TranscriptReplay(
            task_id=task_id,
            entries=tuple(entries),
            leaf_entry_id=entries[-1].entry_id if entries else "",
            consistent=consistent,
            findings=tuple(findings),
            digest=digest_object([item.to_dict() for item in entries]),
        )

    def require_consistent(self, task_id: str) -> TranscriptReplay:
        replay = self.replay(task_id)
        if not replay.consistent:
            raise TranscriptIntegrityError(
                "subagent sidechain transcript is corrupt",
                task_id=task_id,
                findings=[dict(item) for item in replay.findings],
            )
        return replay

    def sanitize_for_resume(self, task_id: str) -> tuple[dict[str, Any], ...]:
        replay = self.require_consistent(task_id)
        sanitized: list[dict[str, Any]] = []
        unresolved_tools: set[str] = set()
        for entry in replay.entries:
            payload = dict(entry.payload)
            if entry.kind == TranscriptEntryKind.TOOL_USE:
                tool_use_id = str(payload.get("tool_use_id") or "")
                if tool_use_id:
                    unresolved_tools.add(tool_use_id)
            elif entry.kind == TranscriptEntryKind.TOOL_RESULT:
                unresolved_tools.discard(str(payload.get("tool_use_id") or ""))
            if entry.kind == TranscriptEntryKind.ASSISTANT and not str(payload.get("content") or "").strip():
                continue
            sanitized.append({
                "entry_id": entry.entry_id,
                "parent_entry_id": entry.parent_entry_id,
                "kind": entry.kind.value,
                "payload": payload,
                "created_at": entry.created_at,
            })
        if unresolved_tools:
            raise TranscriptIntegrityError(
                "subagent transcript has unresolved tool calls",
                task_id=task_id,
                unresolved_tool_use_ids=sorted(unresolved_tools),
            )
        return tuple(sanitized)

    def path(self, task_id: str) -> Path:
        safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in task_id)
        if not safe:
            raise TranscriptIntegrityError("invalid subagent task id", task_id=task_id)
        return self.root / safe / "sidechain.jsonl"

    def _read(self, task_id: str) -> list[TranscriptEntry]:
        path = self.path(task_id)
        if not path.exists():
            return []
        result: list[TranscriptEntry] = []
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as error:
                    raise TranscriptIntegrityError(
                        "subagent transcript contains invalid JSON",
                        task_id=task_id,
                        line_number=line_number,
                    ) from error
                result.append(TranscriptEntry.from_dict(raw))
        except OSError as error:
            raise TranscriptIntegrityError(
                "subagent transcript could not be read",
                task_id=task_id,
                error_type=type(error).__name__,
            ) from error
        return result

    def _require_enabled(self) -> None:
        if self.disabled:
            raise RuntimeError("SubagentTranscriptStore is disabled")

