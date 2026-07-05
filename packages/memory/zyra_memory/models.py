from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import ArtifactRef, new_id, now_iso


class MemoryLayer(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    SKILL = "skill"


@dataclass(slots=True)
class MemoryRecord:
    run_id: str
    task_id: str
    layer: MemoryLayer
    source_type: str
    source_id: str
    summary: str
    content: dict[str, Any] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    node_id: str | None = None
    score: float = 0.0
    memory_id: str = field(default_factory=lambda: new_id("memory"))
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemorySnapshot:
    run_id: str
    task_id: str
    records: list[MemoryRecord]
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def layer_counts(self) -> dict[str, int]:
        counts = {str(layer): 0 for layer in MemoryLayer}
        for record in self.records:
            counts[str(record.layer)] = counts.get(str(record.layer), 0) + 1
        return counts


@dataclass(slots=True)
class CompactPolicy:
    max_active_tokens: int = 8000
    tail_groups: int = 8
    max_inline_chars: int = 3000
    preserve_initial_goal: bool = True
    preserve_constraints: bool = True
    preserve_requirement_changes: bool = True
    preserve_failures: bool = True
    preserve_decisions: bool = True
    preserve_tool_groups: bool = True
    source: str = "claude-code-best/hermes-agent/agent-framework/openclaw"


@dataclass(slots=True)
class CompactResult:
    run_id: str
    task_id: str
    summary: str
    compact_id: str = field(default_factory=lambda: new_id("compact"))
    created_at: str = field(default_factory=now_iso)
    focus: str = ""
    preserved_event_ids: list[str] = field(default_factory=list)
    summarized_event_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    memory_ids: list[str] = field(default_factory=list)
    original_approx_tokens: int = 0
    compacted_approx_tokens: int = 0
    compression_ratio: float = 1.0
    preserved_group_count: int = 0
    summarized_group_count: int = 0
    policy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TrajectoryFrame:
    run_id: str
    task_id: str
    event_id: str
    event_type: str
    index: int
    created_at: str
    title: str
    summary: str
    node_id: str | None = None
    frame_id: str = field(default_factory=lambda: new_id("frame"))
    worker: str | None = None
    route: str | None = None
    status: str | None = None
    requirement_change: bool = False
    failure_injection: bool = False
    verification: bool = False
    artifact_ids: list[str] = field(default_factory=list)
    state_delta: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
