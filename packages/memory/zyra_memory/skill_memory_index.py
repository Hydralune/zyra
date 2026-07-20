from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .memory_index import HydratedMemoryResult, MemoryIndexRuntime
from .models import MemoryLayer, MemoryRecord
from .retrieval_models import RetrievalBudget, RetrievalFilter


@dataclass(frozen=True, slots=True)
class SkillExperience:
    memory_id: str
    skill_name: str
    task_id: str
    run_id: str
    session_id: str
    status: str
    summary: str
    artifact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    failure_kind: str
    score: float
    source_id: str
    metadata: Mapping[str, Any]

    @classmethod
    def from_record(cls, record: MemoryRecord) -> "SkillExperience":
        if record.layer is not MemoryLayer.SKILL:
            raise ValueError("SkillExperience requires a skill-layer MemoryRecord")
        content = record.content if isinstance(record.content, Mapping) else {}
        metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
        return cls(
            memory_id=record.memory_id,
            skill_name=str(
                metadata.get("skill_name")
                or content.get("skill_name")
                or content.get("name")
                or record.source_id
            ),
            task_id=record.task_id,
            run_id=record.run_id,
            session_id=str(metadata.get("session_id") or content.get("session_id") or ""),
            status=str(content.get("status") or metadata.get("status") or "unknown"),
            summary=record.summary,
            artifact_ids=tuple(record.artifact_ids),
            evidence_ids=tuple(record.evidence_ids),
            failure_kind=str(content.get("failure_kind") or metadata.get("failure_kind") or ""),
            score=float(record.score),
            source_id=record.source_id,
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "skill_name": self.skill_name,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status,
            "summary": self.summary,
            "artifact_ids": list(self.artifact_ids),
            "evidence_ids": list(self.evidence_ids),
            "failure_kind": self.failure_kind,
            "score": self.score,
            "source_id": self.source_id,
            "metadata": dict(self.metadata),
        }


class SkillMemoryIndex:
    """Constraint facade over the one shared MemoryIndexRuntime.

    This class does not introduce another store or state owner.  Skill
    activation/revocation remains in SkillRegistry; invocation experience is
    still a canonical MemoryRecord and this facade only constrains retrieval.
    """

    def __init__(self, runtime: MemoryIndexRuntime) -> None:
        self.runtime = runtime

    def search(
        self,
        task_id: str,
        query: str,
        *,
        skill_names: Sequence[str] = (),
        session_ids: Sequence[str] = (),
        failure_kinds: Sequence[str] = (),
        limit: int = 10,
    ) -> HydratedMemoryResult:
        return self.runtime.retrieve(
            task_id,
            query,
            filters=RetrievalFilter(
                task_ids=(task_id,),
                layers=(MemoryLayer.SKILL,),
                skill_names=tuple(str(item) for item in skill_names),
                session_ids=tuple(str(item) for item in session_ids),
                failure_kinds=tuple(str(item) for item in failure_kinds),
            ),
            budget=RetrievalBudget(
                limit=max(0, int(limit)),
                candidate_limit=max(max(0, int(limit)) * 8, max(0, int(limit))),
            ),
        )

    def experiences(
        self,
        task_id: str,
        query: str,
        *,
        skill_names: Sequence[str] = (),
        limit: int = 10,
    ) -> tuple[SkillExperience, ...]:
        result = self.search(task_id, query, skill_names=skill_names, limit=limit)
        return tuple(SkillExperience.from_record(record) for record in result.records)

    def best_prior(
        self,
        task_id: str,
        query: str,
        *,
        skill_name: str,
    ) -> SkillExperience | None:
        results = self.experiences(task_id, query, skill_names=(skill_name,), limit=1)
        return results[0] if results else None

    def status(self, task_id: str = "") -> Mapping[str, Any]:
        return {
            "facade": "SkillMemoryIndex",
            "shared_runtime": "MemoryIndexRuntime",
            "canonical_owner": type(self.runtime.canonical_store).__name__,
            "layer_filter": MemoryLayer.SKILL.value,
            "runtime": self.runtime.status(task_id),
        }
