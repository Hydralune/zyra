from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, new_id, now_iso, to_jsonable

from zyra_runtime.artifacts import LocalArtifactStore
from zyra_runtime.tool_loop import ToolLoopRequest
from zyra_runtime.tools import ToolResult


class ClaudeContextBlockRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SUMMARY = "summary"
    ARTIFACT = "artifact"
    CONTROL = "control"
    MEMORY = "memory"
    ERROR = "error"


class ClaudeContextBlockState(StrEnum):
    ACTIVE = "active"
    PINNED = "pinned"
    COMPACTED = "compacted"
    DROPPED = "dropped"
    RESTORED = "restored"


class ClaudeContextCompactionReason(StrEnum):
    BUDGET_EXCEEDED = "budget_exceeded"
    MANUAL_COMPACT = "manual_compact"
    TURN_BOUNDARY = "turn_boundary"
    RESTORE_WINDOW = "restore_window"
    TOOL_RESULT_OVERFLOW = "tool_result_overflow"


@dataclass(frozen=True, slots=True)
class ClaudeContextBudget:
    max_chars: int
    reserve_chars: int = 1024
    min_recent_blocks: int = 4
    max_compaction_blocks: int = 64
    artifact_preview_chars: int = 4096
    compact_ratio_target: float = 0.45

    @property
    def active_limit(self) -> int:
        return max(1, self.max_chars - max(0, self.reserve_chars))

    def normalize(self) -> "ClaudeContextBudget":
        return ClaudeContextBudget(
            max_chars=max(1, int(self.max_chars)),
            reserve_chars=max(0, int(self.reserve_chars)),
            min_recent_blocks=max(0, int(self.min_recent_blocks)),
            max_compaction_blocks=max(1, int(self.max_compaction_blocks)),
            artifact_preview_chars=max(256, int(self.artifact_preview_chars)),
            compact_ratio_target=max(0.05, min(0.95, float(self.compact_ratio_target))),
        )

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeContextSource:
    source_id: str
    source_kind: str
    source_path: str = ""
    upstream_source_path: str = ""
    runtime_owner: str = "zyra"
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ClaudeContextBlock:
    role: ClaudeContextBlockRole
    text: str
    priority: int = 0
    source: ClaudeContextSource | None = None
    block_id: str = field(default_factory=lambda: new_id("ctx"))
    state: ClaudeContextBlockState = ClaudeContextBlockState.ACTIVE
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    turn_index: int | None = None
    batch_index: int | None = None
    step_index: int | None = None
    tool_call_id: str = ""
    tool_name: str = ""
    artifact_ids: list[str] = field(default_factory=list)
    parent_block_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def compactable(self) -> bool:
        if self.state != ClaudeContextBlockState.ACTIVE:
            return False
        if self.priority >= 900:
            return False
        if self.role in {ClaudeContextBlockRole.SYSTEM, ClaudeContextBlockRole.CONTROL}:
            return False
        return True

    def mark_compacted(self, artifact_id: str) -> None:
        self.state = ClaudeContextBlockState.COMPACTED
        self.updated_at = now_iso()
        if artifact_id and artifact_id not in self.artifact_ids:
            self.artifact_ids.append(artifact_id)

    def mark_restored(self) -> None:
        self.state = ClaudeContextBlockState.RESTORED
        self.updated_at = now_iso()

    def compact_preview(self, max_chars: int) -> str:
        normalized = " ".join(self.text.split())
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max(0, max_chars - 3)] + "..."

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "role": str(self.role),
            "state": str(self.state),
            "text": self.text,
            "chars": self.chars,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_index": self.turn_index,
            "batch_index": self.batch_index,
            "step_index": self.step_index,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "artifact_ids": list(self.artifact_ids),
            "parent_block_ids": list(self.parent_block_ids),
            "source": self.source.to_dict() if self.source else None,
            "metadata": to_jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClaudeContextBlock":
        source_data = data.get("source")
        source = None
        if isinstance(source_data, Mapping):
            source = ClaudeContextSource(
                source_id=str(source_data.get("source_id") or ""),
                source_kind=str(source_data.get("source_kind") or ""),
                source_path=str(source_data.get("source_path") or ""),
                upstream_source_path=str(source_data.get("upstream_source_path") or ""),
                runtime_owner=str(source_data.get("runtime_owner") or "zyra"),
                metadata={str(k): str(v) for k, v in dict(source_data.get("metadata") or {}).items()},
            )
        return cls(
            role=_enum_or_default(ClaudeContextBlockRole, data.get("role"), ClaudeContextBlockRole.USER),
            text=str(data.get("text") or ""),
            priority=_safe_int(data.get("priority"), default=0),
            source=source,
            block_id=str(data.get("block_id") or new_id("ctx")),
            state=_enum_or_default(ClaudeContextBlockState, data.get("state"), ClaudeContextBlockState.ACTIVE),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            turn_index=_optional_int(data.get("turn_index")),
            batch_index=_optional_int(data.get("batch_index")),
            step_index=_optional_int(data.get("step_index")),
            tool_call_id=str(data.get("tool_call_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            artifact_ids=[str(item) for item in _as_list(data.get("artifact_ids"))],
            parent_block_ids=[str(item) for item in _as_list(data.get("parent_block_ids"))],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ClaudeContextWindowStats:
    total_blocks: int
    active_blocks: int
    compacted_blocks: int
    dropped_blocks: int
    restored_blocks: int
    active_chars: int
    total_chars: int
    pinned_chars: int
    tool_chars: int
    user_chars: int
    assistant_chars: int
    artifact_refs: int
    oldest_active_block_id: str = ""
    newest_active_block_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeContextSelection:
    selected_blocks: list[ClaudeContextBlock]
    compacted_blocks: list[ClaudeContextBlock]
    active_chars: int
    overflow_chars: int
    reason: ClaudeContextCompactionReason | None = None

    @property
    def needs_compaction(self) -> bool:
        return self.overflow_chars > 0 and bool(self.compacted_blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_block_ids": [block.block_id for block in self.selected_blocks],
            "compacted_block_ids": [block.block_id for block in self.compacted_blocks],
            "active_chars": self.active_chars,
            "overflow_chars": self.overflow_chars,
            "reason": str(self.reason) if self.reason else "",
        }


@dataclass(frozen=True, slots=True)
class ClaudeContextCompaction:
    applied: bool
    reason: ClaudeContextCompactionReason
    before_chars: int
    after_chars: int
    compacted_chars: int
    compacted_block_ids: list[str]
    summary_block: ClaudeContextBlock | None = None
    artifact: ArtifactRef | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def artifact_id(self) -> str:
        return self.artifact.artifact_id if self.artifact else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "reason": str(self.reason),
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "compacted_chars": self.compacted_chars,
            "compacted_block_ids": list(self.compacted_block_ids),
            "summary_block": self.summary_block.to_dict() if self.summary_block else None,
            "artifact": to_jsonable(self.artifact) if self.artifact else None,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClaudeContextRestorePlan:
    ok: bool
    restored_blocks: list[ClaudeContextBlock]
    missing_artifact_ids: list[str]
    invalid_artifact_ids: list[str]
    source_artifact_ids: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def restored_chars(self) -> int:
        return sum(block.chars for block in self.restored_blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "restored_block_ids": [block.block_id for block in self.restored_blocks],
            "restored_chars": self.restored_chars,
            "missing_artifact_ids": list(self.missing_artifact_ids),
            "invalid_artifact_ids": list(self.invalid_artifact_ids),
            "source_artifact_ids": list(self.source_artifact_ids),
            "metadata": to_jsonable(self.metadata),
        }


class ClaudeContextWindowManager:
    """Owns the QueryEngine context window for the productized CodeWorker path."""

    def __init__(
        self,
        *,
        budget: ClaudeContextBudget,
        runtime_source: str,
        runtime_id: str,
        session_id: str = "",
        request_id: str = "",
    ) -> None:
        self.budget = budget.normalize()
        self.runtime_source = runtime_source
        self.runtime_id = runtime_id
        self.session_id = session_id
        self.request_id = request_id
        self._blocks: list[ClaudeContextBlock] = []
        self._compactions: list[ClaudeContextCompaction] = []
        self._restores: list[ClaudeContextRestorePlan] = []

    @property
    def blocks(self) -> list[ClaudeContextBlock]:
        return list(self._blocks)

    @property
    def compactions(self) -> list[ClaudeContextCompaction]:
        return list(self._compactions)

    @property
    def restores(self) -> list[ClaudeContextRestorePlan]:
        return list(self._restores)

    @property
    def active_blocks(self) -> list[ClaudeContextBlock]:
        return [block for block in self._blocks if block.state in {ClaudeContextBlockState.ACTIVE, ClaudeContextBlockState.PINNED, ClaudeContextBlockState.RESTORED}]

    @property
    def active_chars(self) -> int:
        return sum(block.chars for block in self.active_blocks)

    def add_block(self, block: ClaudeContextBlock) -> ClaudeContextBlock:
        self._blocks.append(block)
        return block

    def seed_request_messages(self, messages: Sequence[Any]) -> list[ClaudeContextBlock]:
        blocks: list[ClaudeContextBlock] = []
        for index, message in enumerate(messages):
            block = self._block_from_request_message(index, message)
            self.add_block(block)
            blocks.append(block)
        return blocks

    def record_turn_prompt(self, *, turn_index: int, text: str, metadata: Mapping[str, Any] | None = None) -> ClaudeContextBlock:
        block = ClaudeContextBlock(
            role=ClaudeContextBlockRole.USER,
            text=text,
            priority=700,
            turn_index=turn_index,
            source=ClaudeContextSource(
                source_id=f"turn:{turn_index}",
                source_kind="query_turn",
                source_path="packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                upstream_source_path="src/query.ts",
                runtime_owner=self.runtime_source,
            ),
            metadata={
                "session_id": self.session_id,
                "worker_request_id": self.request_id,
                **dict(metadata or {}),
            },
        )
        return self.add_block(block)

    def record_assistant_delta(
        self,
        *,
        turn_index: int,
        text: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ClaudeContextBlock:
        block = ClaudeContextBlock(
            role=ClaudeContextBlockRole.ASSISTANT,
            text=text,
            priority=620,
            turn_index=turn_index,
            source=ClaudeContextSource(
                source_id=f"assistant:{turn_index}:{len(self._blocks)}",
                source_kind="assistant_delta",
                source_path="packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                upstream_source_path="src/QueryEngine.ts",
                runtime_owner=self.runtime_source,
            ),
            metadata={"session_id": self.session_id, **dict(metadata or {})},
        )
        return self.add_block(block)

    def record_tool_result(
        self,
        *,
        planned: ToolLoopRequest,
        result: ToolResult,
        result_chars: int,
        turn_index: int,
        batch_index: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> ClaudeContextBlock:
        role = ClaudeContextBlockRole.TOOL if result.ok else ClaudeContextBlockRole.ERROR
        text = _tool_result_context_text(result)
        block = ClaudeContextBlock(
            role=role,
            text=text,
            priority=540 if result.ok else 760,
            source=ClaudeContextSource(
                source_id=planned.call.tool_call_id,
                source_kind="tool_result",
                source_path=planned.source_path,
                upstream_source_path=str(planned.metadata.get("upstream_source_path") or ""),
                runtime_owner=self.runtime_source,
                metadata={
                    "tool_name": planned.call.tool_name,
                    "access_mode": str(planned.access_mode),
                },
            ),
            turn_index=turn_index,
            batch_index=batch_index,
            step_index=planned.step_index,
            tool_call_id=planned.call.tool_call_id,
            tool_name=planned.call.tool_name,
            artifact_ids=[artifact.artifact_id for artifact in result.artifacts],
            metadata={
                "ok": result.ok,
                "error": result.error,
                "summary": result.summary,
                "result_chars": result_chars,
                "permission_effect": str(result.metadata.get("permission_effect") or ""),
                **dict(metadata or {}),
            },
        )
        return self.add_block(block)

    def record_control_result(
        self,
        *,
        command_name: str,
        text: str,
        ok: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> ClaudeContextBlock:
        block = ClaudeContextBlock(
            role=ClaudeContextBlockRole.CONTROL if ok else ClaudeContextBlockRole.ERROR,
            text=text,
            priority=880 if ok else 920,
            source=ClaudeContextSource(
                source_id=f"control:{command_name}:{len(self._blocks)}",
                source_kind="control_command",
                source_path="packages/runtime/zyra_runtime/claude_control_commands.py",
                upstream_source_path="src/commands",
                runtime_owner=self.runtime_source,
            ),
            metadata={
                "command_name": command_name,
                "ok": ok,
                "session_id": self.session_id,
                **dict(metadata or {}),
            },
        )
        return self.add_block(block)

    def select_for_budget(
        self,
        *,
        reason: ClaudeContextCompactionReason = ClaudeContextCompactionReason.BUDGET_EXCEEDED,
    ) -> ClaudeContextSelection:
        active = self.active_blocks
        active_chars = sum(block.chars for block in active)
        overflow = max(0, active_chars - self.budget.active_limit)
        if overflow <= 0:
            return ClaudeContextSelection(
                selected_blocks=active,
                compacted_blocks=[],
                active_chars=active_chars,
                overflow_chars=0,
                reason=None,
            )

        protected_ids = set()
        if self.budget.min_recent_blocks:
            protected_ids.update(block.block_id for block in active[-self.budget.min_recent_blocks :])
        candidates = [
            block
            for block in active
            if block.compactable and block.block_id not in protected_ids
        ]
        candidates.sort(key=lambda block: (block.priority, block.created_at, block.block_id))
        selected: list[ClaudeContextBlock] = []
        selected_chars = 0
        target_chars = max(overflow, int(active_chars * (1.0 - self.budget.compact_ratio_target)))
        for block in candidates[: self.budget.max_compaction_blocks]:
            selected.append(block)
            selected_chars += block.chars
            if selected_chars >= target_chars:
                break
        return ClaudeContextSelection(
            selected_blocks=[block for block in active if block not in selected],
            compacted_blocks=selected,
            active_chars=active_chars,
            overflow_chars=overflow,
            reason=reason,
        )

    def maybe_compact(
        self,
        *,
        artifact_store: LocalArtifactStore,
        run_id: str,
        task_id: str,
        producer_node_id: str | None,
        reason: ClaudeContextCompactionReason = ClaudeContextCompactionReason.BUDGET_EXCEEDED,
        force: bool = False,
    ) -> ClaudeContextCompaction:
        before = self.active_chars
        selection = self.select_for_budget(reason=reason)
        if not force and not selection.needs_compaction:
            decision = ClaudeContextCompaction(
                applied=False,
                reason=reason,
                before_chars=before,
                after_chars=before,
                compacted_chars=0,
                compacted_block_ids=[],
                metadata={"budget": self.budget.to_dict(), "selection": selection.to_dict()},
            )
            return decision

        compacted = selection.compacted_blocks if selection.compacted_blocks else [
            block for block in self.active_blocks if block.compactable
        ][: self.budget.max_compaction_blocks]
        if not compacted:
            decision = ClaudeContextCompaction(
                applied=False,
                reason=reason,
                before_chars=before,
                after_chars=before,
                compacted_chars=0,
                compacted_block_ids=[],
                metadata={"budget": self.budget.to_dict(), "selection": selection.to_dict(), "blocked": "no_compactable_blocks"},
            )
            return decision

        summary_text = self._summarize_blocks(compacted)
        payload = self._compaction_payload(compacted, reason=reason, summary_text=summary_text, before_chars=before)
        artifact = artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            title=f"Claude context compaction {self.session_id or self.request_id or run_id}",
            kind=ArtifactKind.TRACE,
            extension=".json",
            producer_node_id=producer_node_id,
        )
        for block in compacted:
            block.mark_compacted(artifact.artifact_id)
        summary_block = ClaudeContextBlock(
            role=ClaudeContextBlockRole.SUMMARY,
            text=summary_text,
            priority=780,
            state=ClaudeContextBlockState.ACTIVE,
            source=ClaudeContextSource(
                source_id=artifact.artifact_id,
                source_kind="context_compaction",
                source_path="packages/runtime/zyra_runtime/claude_context_window.py",
                upstream_source_path="src/services/compact/compact.ts",
                runtime_owner=self.runtime_source,
            ),
            artifact_ids=[artifact.artifact_id],
            parent_block_ids=[block.block_id for block in compacted],
            metadata={
                "reason": str(reason),
                "compacted_chars": sum(block.chars for block in compacted),
                "before_chars": before,
                "runtime_id": self.runtime_id,
            },
        )
        self.add_block(summary_block)
        after = self.active_chars
        decision = ClaudeContextCompaction(
            applied=True,
            reason=reason,
            before_chars=before,
            after_chars=after,
            compacted_chars=sum(block.chars for block in compacted),
            compacted_block_ids=[block.block_id for block in compacted],
            summary_block=summary_block,
            artifact=artifact,
            metadata={"budget": self.budget.to_dict(), "selection": selection.to_dict()},
        )
        self._compactions.append(decision)
        return decision

    def restore_from_artifacts(
        self,
        artifacts: Iterable[ArtifactRef],
        *,
        max_blocks: int | None = None,
    ) -> ClaudeContextRestorePlan:
        restored: list[ClaudeContextBlock] = []
        missing: list[str] = []
        invalid: list[str] = []
        source_ids: list[str] = []
        for artifact in artifacts:
            source_ids.append(artifact.artifact_id)
            path = _artifact_path(artifact)
            if path is None or not path.exists():
                missing.append(artifact.artifact_id)
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                invalid.append(artifact.artifact_id)
                continue
            blocks = payload.get("blocks")
            if not isinstance(blocks, list):
                invalid.append(artifact.artifact_id)
                continue
            for item in blocks:
                if not isinstance(item, Mapping):
                    continue
                block = ClaudeContextBlock.from_dict(item)
                block.mark_restored()
                restored.append(block)
                self.add_block(block)
                if max_blocks is not None and len(restored) >= max_blocks:
                    break
            if max_blocks is not None and len(restored) >= max_blocks:
                break
        plan = ClaudeContextRestorePlan(
            ok=not missing and not invalid,
            restored_blocks=restored,
            missing_artifact_ids=missing,
            invalid_artifact_ids=invalid,
            source_artifact_ids=source_ids,
            metadata={"runtime_id": self.runtime_id, "session_id": self.session_id},
        )
        self._restores.append(plan)
        return plan

    def stats(self) -> ClaudeContextWindowStats:
        active = self.active_blocks
        return ClaudeContextWindowStats(
            total_blocks=len(self._blocks),
            active_blocks=len(active),
            compacted_blocks=sum(1 for block in self._blocks if block.state == ClaudeContextBlockState.COMPACTED),
            dropped_blocks=sum(1 for block in self._blocks if block.state == ClaudeContextBlockState.DROPPED),
            restored_blocks=sum(1 for block in self._blocks if block.state == ClaudeContextBlockState.RESTORED),
            active_chars=sum(block.chars for block in active),
            total_chars=sum(block.chars for block in self._blocks),
            pinned_chars=sum(block.chars for block in self._blocks if block.state == ClaudeContextBlockState.PINNED),
            tool_chars=sum(block.chars for block in self._blocks if block.role == ClaudeContextBlockRole.TOOL),
            user_chars=sum(block.chars for block in self._blocks if block.role == ClaudeContextBlockRole.USER),
            assistant_chars=sum(block.chars for block in self._blocks if block.role == ClaudeContextBlockRole.ASSISTANT),
            artifact_refs=sum(len(block.artifact_ids) for block in self._blocks),
            oldest_active_block_id=active[0].block_id if active else "",
            newest_active_block_id=active[-1].block_id if active else "",
        )

    def metadata(self) -> dict[str, str]:
        stats = self.stats()
        return {
            "context_window_blocks": str(stats.total_blocks),
            "context_window_active_blocks": str(stats.active_blocks),
            "context_window_compacted_blocks": str(stats.compacted_blocks),
            "context_window_restored_blocks": str(stats.restored_blocks),
            "context_window_active_chars": str(stats.active_chars),
            "context_window_total_chars": str(stats.total_chars),
            "context_window_artifact_refs": str(stats.artifact_refs),
            "context_window_budget_chars": str(self.budget.max_chars),
            "context_window_active_limit_chars": str(self.budget.active_limit),
        }

    def model_messages(self, *, max_chars: int | None = None) -> list[dict[str, Any]]:
        """Build the provider prompt from the current active context custody.

        Provenance is retained on every message. External or otherwise untrusted
        blocks are always data in the user channel, even when their original
        context role was system/control/summary.
        """

        remaining = self.budget.active_limit if max_chars is None else max(0, int(max_chars))
        messages: list[dict[str, Any]] = []
        for block in self.active_blocks:
            if remaining <= 0:
                break
            source = block.source
            block_metadata = dict(block.metadata or {})
            source_metadata = dict(getattr(source, "metadata", {}) or {})
            provenance = str(
                block_metadata.get("source_provenance")
                or source_metadata.get("source_provenance")
                or getattr(source, "source_kind", "")
                or "unknown"
            )
            trust_level = str(
                block_metadata.get("trust_level")
                or source_metadata.get("trust_level")
                or "unknown"
            ).strip().lower()
            redaction_state = str(
                block_metadata.get("secret_redaction_state")
                or source_metadata.get("secret_redaction_state")
                or "unknown"
            ).strip().lower()
            external_marker = str(
                block_metadata.get("external")
                or source_metadata.get("external")
                or ""
            ).strip().lower()
            provenance_lower = provenance.lower()
            untrusted = (
                trust_level in {"untrusted", "external_untrusted", "unknown"}
                or external_marker in {"1", "true", "yes", "on"}
                or any(marker in provenance_lower for marker in ("external", "mcp", "browser", "web"))
            )

            original_role = str(block.role).split(".")[-1].lower()
            if untrusted:
                role = "user"
            elif original_role == "tool":
                role = "tool"
            elif original_role == "assistant":
                role = "assistant"
            elif original_role in {"system", "control", "summary"}:
                role = "system"
            else:
                role = "user"

            content = str(block.text or "")
            if untrusted and not content.startswith("[UNTRUSTED_CONTEXT"):
                source_ref = getattr(source, "source_id", "") or block.block_id
                content = f"[UNTRUSTED_CONTEXT source={source_ref}]\n{content}"
            if len(content) > remaining:
                content = content[:remaining]
            if not content:
                continue

            metadata = {
                "context_block_id": block.block_id,
                "context_block_state": str(block.state),
                "context_original_role": original_role,
                "source_id": getattr(source, "source_id", ""),
                "source_kind": getattr(source, "source_kind", ""),
                "source_path": getattr(source, "source_path", ""),
                "upstream_source_path": getattr(source, "upstream_source_path", ""),
                "runtime_owner": getattr(source, "runtime_owner", ""),
                "source_provenance": provenance,
                "trust_level": "external_untrusted" if untrusted else trust_level,
                "secret_redaction_state": redaction_state,
                "artifact_ids": list(block.artifact_ids),
                **source_metadata,
                **block_metadata,
            }
            message: dict[str, Any] = {
                "role": role,
                "content": content,
                "metadata": metadata,
            }
            if role == "tool" and block.tool_call_id:
                message["tool_call_id"] = block.tool_call_id
            messages.append(message)
            remaining -= len(content)
        return messages

    def snapshot(self, *, include_text: bool = True) -> dict[str, Any]:
        blocks = []
        for block in self._blocks:
            item = block.to_dict()
            if not include_text:
                item["text"] = block.compact_preview(240)
            blocks.append(item)
        return {
            "runtime_source": self.runtime_source,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.request_id,
            "budget": self.budget.to_dict(),
            "stats": self.stats().to_dict(),
            "blocks": blocks,
            "compactions": [item.to_dict() for item in self._compactions],
            "restores": [item.to_dict() for item in self._restores],
        }

    def _block_from_request_message(self, index: int, message: Any) -> ClaudeContextBlock:
        if isinstance(message, Mapping):
            role = _role_from_value(message.get("role"))
            text = _message_text(message)
            metadata = {str(k): to_jsonable(v) for k, v in message.items() if k not in {"content", "text", "role"}}
        else:
            role = ClaudeContextBlockRole.USER
            text = str(message)
            metadata = {}
        priority = 820 if role == ClaudeContextBlockRole.SYSTEM else 690
        return ClaudeContextBlock(
            role=role,
            text=text,
            priority=priority,
            source=ClaudeContextSource(
                source_id=f"request_message:{index}",
                source_kind="request_message",
                source_path="packages/workers/zyra_workers/code_worker_runtime.py",
                upstream_source_path="src/query.ts",
                runtime_owner=self.runtime_source,
            ),
            metadata={
                "message_index": index,
                "session_id": self.session_id,
                "worker_request_id": self.request_id,
                **metadata,
            },
        )

    def _summarize_blocks(self, blocks: Sequence[ClaudeContextBlock]) -> str:
        lines = [
            f"Compacted {len(blocks)} context block(s) for {self.runtime_source} session {self.session_id or 'unknown'}."
        ]
        by_role: dict[str, int] = {}
        for block in blocks:
            by_role[str(block.role)] = by_role.get(str(block.role), 0) + 1
        if by_role:
            lines.append("Roles: " + ", ".join(f"{role}={count}" for role, count in sorted(by_role.items())))
        for block in blocks[:12]:
            prefix = block.tool_name or str(block.role)
            lines.append(f"- {prefix}: {block.compact_preview(320)}")
        if len(blocks) > 12:
            lines.append(f"- ... {len(blocks) - 12} more block(s) stored in artifact.")
        return "\n".join(lines)

    def _compaction_payload(
        self,
        blocks: Sequence[ClaudeContextBlock],
        *,
        reason: ClaudeContextCompactionReason,
        summary_text: str,
        before_chars: int,
    ) -> dict[str, Any]:
        return {
            "schema": "zyra.claude.context_compaction.v1",
            "runtime_source": self.runtime_source,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.request_id,
            "created_at": now_iso(),
            "reason": str(reason),
            "before_chars": before_chars,
            "compacted_chars": sum(block.chars for block in blocks),
            "budget": self.budget.to_dict(),
            "summary": summary_text,
            "blocks": [block.to_dict() for block in blocks],
        }


def context_blocks_from_snapshot(snapshot: Mapping[str, Any]) -> list[ClaudeContextBlock]:
    messages = snapshot.get("messages")
    if not isinstance(messages, list):
        return []
    blocks: list[ClaudeContextBlock] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        role = _role_from_value(message.get("role"))
        text = str(message.get("content") or "")
        if not text:
            continue
        blocks.append(
            ClaudeContextBlock(
                role=role,
                text=text,
                priority=650,
                source=ClaudeContextSource(
                    source_id=str(message.get("message_id") or f"snapshot:{index}"),
                    source_kind="query_session_snapshot",
                    source_path="packages/runtime/zyra_runtime/query_session.py",
                    upstream_source_path="src/utils/sessionRestore.ts",
                    runtime_owner=str(snapshot.get("metadata", {}).get("contract_source") or "zyra-claude-productized"),
                ),
                metadata={"snapshot_index": index, "phase": str(message.get("phase") or "")},
            )
        )
    return blocks


def compact_context_text(blocks: Sequence[ClaudeContextBlock], *, max_chars: int = 4000) -> str:
    parts: list[str] = []
    remaining = max(1, max_chars)
    for block in blocks:
        label = block.tool_name or str(block.role)
        line = f"[{label}] {block.compact_preview(max(80, min(600, remaining)))}"
        if len(line) > remaining:
            line = line[: max(0, remaining - 3)] + "..."
        parts.append(line)
        remaining -= len(line) + 1
        if remaining <= 0:
            break
    return "\n".join(parts)


def context_window_from_payload(payload: Mapping[str, Any]) -> ClaudeContextWindowManager:
    budget_data = payload.get("budget") if isinstance(payload.get("budget"), Mapping) else {}
    manager = ClaudeContextWindowManager(
        budget=ClaudeContextBudget(
            max_chars=_safe_int(budget_data.get("max_chars"), default=32000),
            reserve_chars=_safe_int(budget_data.get("reserve_chars"), default=1024),
            min_recent_blocks=_safe_int(budget_data.get("min_recent_blocks"), default=4),
            max_compaction_blocks=_safe_int(budget_data.get("max_compaction_blocks"), default=64),
            artifact_preview_chars=_safe_int(budget_data.get("artifact_preview_chars"), default=4096),
            compact_ratio_target=float(budget_data.get("compact_ratio_target") or 0.45),
        ),
        runtime_source=str(payload.get("runtime_source") or "zyra-claude-productized"),
        runtime_id=str(payload.get("runtime_id") or "zyra-claude-code-productized-runtime"),
        session_id=str(payload.get("session_id") or ""),
        request_id=str(payload.get("worker_request_id") or ""),
    )
    for item in _as_list(payload.get("blocks")):
        if isinstance(item, Mapping):
            manager.add_block(ClaudeContextBlock.from_dict(item))
    return manager


def _tool_result_context_text(result: ToolResult) -> str:
    payload = {
        "ok": result.ok,
        "summary": result.summary,
        "error": result.error,
        "output": result.output,
        "artifacts": [to_jsonable(artifact) for artifact in result.artifacts],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                if isinstance(item.get("text"), str):
                    parts.append(str(item.get("text")))
                elif isinstance(item.get("content"), str):
                    parts.append(str(item.get("content")))
        return "\n".join(parts)
    if isinstance(message.get("text"), str):
        return str(message.get("text"))
    return json.dumps(to_jsonable(message), ensure_ascii=False, sort_keys=True)


def _role_from_value(value: Any) -> ClaudeContextBlockRole:
    lowered = str(value or "user").lower()
    mapping = {
        "system": ClaudeContextBlockRole.SYSTEM,
        "user": ClaudeContextBlockRole.USER,
        "assistant": ClaudeContextBlockRole.ASSISTANT,
        "tool": ClaudeContextBlockRole.TOOL,
        "summary": ClaudeContextBlockRole.SUMMARY,
        "artifact": ClaudeContextBlockRole.ARTIFACT,
        "control": ClaudeContextBlockRole.CONTROL,
        "memory": ClaudeContextBlockRole.MEMORY,
        "error": ClaudeContextBlockRole.ERROR,
    }
    return mapping.get(lowered, ClaudeContextBlockRole.USER)


def _artifact_path(artifact: ArtifactRef) -> Path | None:
    relative = artifact.metadata.get("relative_path")
    if isinstance(relative, str) and artifact.uri:
        return Path(artifact.uri)
    if artifact.uri:
        return Path(artifact.uri)
    return None


def _enum_or_default(enum_type: type[Any], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
