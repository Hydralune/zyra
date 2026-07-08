from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolOutputStoreEntryKind(StrEnum):
    INLINE = "inline"
    EXTERNALIZED = "externalized"
    ERROR = "error"
    PERMISSION = "permission"


class ToolOutputStoreStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class ToolOutputStoreEntry:
    entry_id: str
    tool_call_id: str
    tool_name: str
    kind: ToolOutputStoreEntryKind
    ok: bool
    error: str
    summary: str
    inline_preview: str
    output_chars: int
    artifact_ids: tuple[str, ...]
    externalized_artifact_id: str
    budget_applied: bool
    turn_index: int
    step_index: int
    source_path: str = "packages/runtime/zyra_runtime/tool_runtime_output_store.py"
    upstream_signal: str = "opencode ToolOutputStore / Claude persisted tool result"
    created_at: str = field(default_factory=now_iso)

    @property
    def externalized(self) -> bool:
        return self.kind == ToolOutputStoreEntryKind.EXTERNALIZED

    @property
    def retrievable(self) -> bool:
        return bool(self.inline_preview or self.artifact_ids or self.externalized_artifact_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "kind": str(self.kind),
            "ok": self.ok,
            "error": self.error,
            "summary": self.summary,
            "inline_preview": self.inline_preview,
            "output_chars": self.output_chars,
            "artifact_ids": list(self.artifact_ids),
            "externalized_artifact_id": self.externalized_artifact_id,
            "budget_applied": self.budget_applied,
            "turn_index": self.turn_index,
            "step_index": self.step_index,
            "externalized": self.externalized,
            "retrievable": self.retrievable,
            "source_path": self.source_path,
            "upstream_signal": self.upstream_signal,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolOutputStoreSnapshot:
    snapshot_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    entries: tuple[ToolOutputStoreEntry, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def status(self) -> ToolOutputStoreStatus:
        if not self.entries:
            return ToolOutputStoreStatus.EMPTY
        if all(entry.retrievable for entry in self.entries):
            return ToolOutputStoreStatus.READY
        return ToolOutputStoreStatus.DEGRADED

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def externalized_count(self) -> int:
        return sum(1 for entry in self.entries if entry.externalized)

    @property
    def error_count(self) -> int:
        return sum(1 for entry in self.entries if not entry.ok)

    @property
    def artifact_ref_count(self) -> int:
        return sum(len(entry.artifact_ids) + (1 if entry.externalized_artifact_id else 0) for entry in self.entries)

    @property
    def total_output_chars(self) -> int:
        return sum(entry.output_chars for entry in self.entries)

    @property
    def inline_entries(self) -> tuple[ToolOutputStoreEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == ToolOutputStoreEntryKind.INLINE)

    @property
    def externalized_entries(self) -> tuple[ToolOutputStoreEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == ToolOutputStoreEntryKind.EXTERNALIZED)

    @property
    def error_entries(self) -> tuple[ToolOutputStoreEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == ToolOutputStoreEntryKind.ERROR)

    @property
    def permission_entries(self) -> tuple[ToolOutputStoreEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == ToolOutputStoreEntryKind.PERMISSION)

    def entries_by_kind(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {str(kind): [] for kind in ToolOutputStoreEntryKind}
        for entry in self.entries:
            grouped.setdefault(str(entry.kind), []).append(entry.tool_call_id)
        return grouped

    def entry_for(self, tool_call_id: str) -> ToolOutputStoreEntry | None:
        for entry in self.entries:
            if entry.tool_call_id == tool_call_id:
                return entry
        return None

    def require_ready(self) -> None:
        if self.status == ToolOutputStoreStatus.READY:
            return
        missing = [
            entry.tool_call_id or entry.entry_id
            for entry in self.entries
            if not entry.retrievable
        ]
        detail = ", ".join(missing) if missing else str(self.status)
        raise AssertionError(f"tool output store is not ready: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "status": str(self.status),
            "entry_count": self.entry_count,
            "externalized_count": self.externalized_count,
            "error_count": self.error_count,
            "artifact_ref_count": self.artifact_ref_count,
            "total_output_chars": self.total_output_chars,
            "inline_count": len(self.inline_entries),
            "permission_count": len(self.permission_entries),
            "entries_by_kind": self.entries_by_kind(),
            "entries": [entry.to_dict() for entry in self.entries],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_output_store_snapshot_id": self.snapshot_id,
            "tool_output_store_owner_unit": self.owner_unit,
            "tool_output_store_status": str(self.status),
            "tool_output_store_entries": str(self.entry_count),
            "tool_output_store_externalized": str(self.externalized_count),
            "tool_output_store_errors": str(self.error_count),
            "tool_output_store_artifact_refs": str(self.artifact_ref_count),
            "tool_output_store_total_chars": str(self.total_output_chars),
            "tool_output_store_inline": str(len(self.inline_entries)),
            "tool_output_store_permission": str(len(self.permission_entries)),
        }


@dataclass(frozen=True, slots=True)
class ToolOutputStoreArtifact:
    snapshot: ToolOutputStoreSnapshot
    artifact: ArtifactRef | None

    @property
    def written(self) -> bool:
        return self.artifact is not None

    def metadata(self) -> dict[str, str]:
        metadata = self.snapshot.metadata()
        metadata.update(
            {
                "tool_output_store_artifact_written": str(self.written).lower(),
                "tool_output_store_artifact_id": self.artifact.artifact_id if self.artifact else "",
            }
        )
        return metadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "artifact": to_jsonable(self.artifact) if self.artifact else None,
            "written": self.written,
        }


class ToolOutputStoreRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
        inline_preview_chars: int = 240,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.inline_preview_chars = max(1, int(inline_preview_chars))

    def build_snapshot(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        receipts: Sequence[Mapping[str, Any]],
    ) -> ToolOutputStoreSnapshot:
        entries = tuple(self._entry(receipt) for receipt in receipts)
        return ToolOutputStoreSnapshot(
            snapshot_id=new_id("tooloutputstore"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            entries=entries,
        )

    def write_snapshot(
        self,
        artifact_store: Any,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        snapshot: ToolOutputStoreSnapshot,
    ) -> ToolOutputStoreArtifact:
        artifact = artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            producer_node_id=node_id,
            title="Tool output store snapshot",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            content=json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        )
        return ToolOutputStoreArtifact(snapshot=snapshot, artifact=artifact)

    def event_for_artifact(
        self,
        stored: ToolOutputStoreArtifact,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": stored.snapshot.session_id,
                    "worker_request_id": stored.snapshot.worker_request_id,
                    "phase": "tool_output_store_persisted",
                    "tool_output_store": stored.to_dict(),
                }
            },
        )

    def resolve_entry(self, snapshot: ToolOutputStoreSnapshot, tool_call_id: str) -> ToolOutputStoreEntry | None:
        return snapshot.entry_for(tool_call_id)

    def _entry(self, receipt: Mapping[str, Any]) -> ToolOutputStoreEntry:
        request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
        result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
        decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
        output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
        error = str(result.get("error") or "")
        ok = result.get("ok") is True
        budget_applied = decision.get("applied") is True
        externalized_artifact_id = str(decision.get("artifact_id") or output.get("full_output_artifact_id") or "")
        kind = _entry_kind(ok=ok, error=error, externalized=budget_applied or bool(externalized_artifact_id))
        output_text = json.dumps(to_jsonable(output), ensure_ascii=False, sort_keys=True)
        artifact_ids = _artifact_ids(result)
        return ToolOutputStoreEntry(
            entry_id=new_id("tooloutput"),
            tool_call_id=str(request.get("tool_call_id") or result.get("tool_call_id") or ""),
            tool_name=str(request.get("tool_name") or ""),
            kind=kind,
            ok=ok,
            error=error,
            summary=str(result.get("summary") or ""),
            inline_preview=output_text[: self.inline_preview_chars],
            output_chars=len(output_text),
            artifact_ids=artifact_ids,
            externalized_artifact_id=externalized_artifact_id,
            budget_applied=budget_applied,
            turn_index=_safe_int(request.get("turn_index")),
            step_index=_safe_int(request.get("step_index")),
        )


def tool_output_store_metadata(stored: ToolOutputStoreArtifact | ToolOutputStoreSnapshot | None) -> dict[str, str]:
    if stored is None:
        return {
            "tool_output_store_entries": "0",
            "tool_output_store_artifact_written": "false",
        }
    if isinstance(stored, ToolOutputStoreArtifact):
        return stored.metadata()
    return stored.metadata()


def assert_tool_output_store_ready(snapshot: ToolOutputStoreSnapshot) -> None:
    snapshot.require_ready()


def render_tool_output_store_markdown(snapshot: ToolOutputStoreSnapshot) -> str:
    lines = [
        "## Tool Output Store",
        "",
        f"- owner_unit: `{snapshot.owner_unit}`",
        f"- status: `{snapshot.status}`",
        f"- entries: `{snapshot.entry_count}`",
        f"- externalized: `{snapshot.externalized_count}`",
        f"- errors: `{snapshot.error_count}`",
        f"- artifact_refs: `{snapshot.artifact_ref_count}`",
        "",
        "### Entries",
        "",
    ]
    if snapshot.entries:
        lines.extend(
            f"- `{entry.tool_call_id}` `{entry.tool_name}`: `{entry.kind}`, artifacts `{len(entry.artifact_ids)}`"
            for entry in snapshot.entries
        )
    else:
        lines.append("- no tool outputs")
    return "\n".join(lines)


def _entry_kind(*, ok: bool, error: str, externalized: bool) -> ToolOutputStoreEntryKind:
    if error in {"permission_required", "permission_denied"}:
        return ToolOutputStoreEntryKind.PERMISSION
    if externalized:
        return ToolOutputStoreEntryKind.EXTERNALIZED
    if not ok:
        return ToolOutputStoreEntryKind.ERROR
    return ToolOutputStoreEntryKind.INLINE


def _artifact_ids(result: Mapping[str, Any]) -> tuple[str, ...]:
    artifact_ids: list[str] = []
    for artifact in result.get("artifacts") or []:
        if isinstance(artifact, Mapping):
            artifact_id = str(artifact.get("artifact_id") or "")
            if artifact_id:
                artifact_ids.append(artifact_id)
    output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
    output_artifact_id = str(output.get("full_output_artifact_id") or "")
    if output_artifact_id:
        artifact_ids.append(output_artifact_id)
    return tuple(dict.fromkeys(artifact_ids))


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
