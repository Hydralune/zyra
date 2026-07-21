from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from zyra_core import AgentMessage, AgentRole, MessageIntent, now_iso

from .curator_integration_models import (
    ContextEffect,
    CuratorConsumer,
    CuratorContextProof,
    CuratorContextVerificationError,
    CuratorOutcome,
    CuratorOutcomeKind,
)
from .curator_integration_store import CuratorIntegrationStore
from .memory_index import HydratedMemoryResult, MemoryIndexRuntime
from .retrieval_context import MemoryContextBlock, MemoryContextBridge
from .retrieval_models import RetrievalBudget, RetrievalFilter


@dataclass(frozen=True, slots=True)
class CuratorRecallRequest:
    outcome_id: str
    query: str = ""
    maximum_results: int = 12
    maximum_chars: int = 24_000
    vector_enabled: bool = True
    force_synchronize: bool = False
    worker_request_id: str = ""
    session_id: str = ""
    require_present: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorRecallRequest:
        if not self.outcome_id.strip():
            raise ValueError("recall verification requires outcome_id")
        if not 1 <= self.maximum_results <= 1000:
            raise ValueError("maximum_results must be between 1 and 1000")
        if not 1 <= self.maximum_chars <= 1_000_000:
            raise ValueError("maximum_chars must be between 1 and 1000000")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "query": self.query,
            "maximum_results": self.maximum_results,
            "maximum_chars": self.maximum_chars,
            "vector_enabled": self.vector_enabled,
            "force_synchronize": self.force_synchronize,
            "worker_request_id": self.worker_request_id,
            "session_id": self.session_id,
            "require_present": self.require_present,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CuratorRecallResult:
    request: CuratorRecallRequest
    outcome: CuratorOutcome
    proof: CuratorContextProof
    context: MemoryContextBlock
    hydrated: HydratedMemoryResult
    worker_message: AgentMessage
    index_sync: Mapping[str, Any]
    stored: bool
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.proof.verified

    def to_dict(self, *, include_context: bool = True) -> dict[str, Any]:
        output = {
            "request": self.request.to_dict(),
            "outcome": self.outcome.to_dict(),
            "proof": self.proof.to_dict(),
            "hydrated": self.hydrated.to_dict(),
            "worker_message": {
                "content": self.worker_message.content,
                "metadata": dict(self.worker_message.metadata),
                "sender_role": str(self.worker_message.sender_role),
                "receiver_role": str(self.worker_message.receiver_role),
                "intent": str(self.worker_message.intent),
            },
            "index_sync": dict(self.index_sync),
            "stored": self.stored,
            "verified": self.verified,
            "diagnostics": dict(self.diagnostics),
        }
        if include_context:
            output["context"] = self.context.to_dict()
        return output


@dataclass(frozen=True, slots=True)
class CuratorRecallComparison:
    task_id: str
    query: str
    before_memory_ids: tuple[str, ...]
    after_memory_ids: tuple[str, ...]
    added_memory_ids: tuple[str, ...]
    removed_memory_ids: tuple[str, ...]
    before_context_digest: str
    after_context_digest: str
    changed: bool
    outcome_id: str
    proof_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "query": self.query,
            "before_memory_ids": list(self.before_memory_ids),
            "after_memory_ids": list(self.after_memory_ids),
            "added_memory_ids": list(self.added_memory_ids),
            "removed_memory_ids": list(self.removed_memory_ids),
            "before_context_digest": self.before_context_digest,
            "after_context_digest": self.after_context_digest,
            "changed": self.changed,
            "outcome_id": self.outcome_id,
            "proof_id": self.proof_id,
        }


class CuratorRecallVerifier:
    """Prove a validated commit changes 06A retrieval and worker context.

    Retrieval is performed against the derived ``MemoryIndexRuntime`` but each
    hit is hydrated from canonical ``MemoryRecord`` rows.  The proof stores IDs,
    revisions and digests only; it never persists an index dump or makes the
    index a fact owner.
    """

    def __init__(
        self,
        *,
        index_runtime: MemoryIndexRuntime,
        integration_store: CuratorIntegrationStore,
    ) -> None:
        self.index_runtime = index_runtime
        self.integration_store = integration_store
        self.context_bridge = MemoryContextBridge(index_runtime)

    def verify(self, request: CuratorRecallRequest) -> CuratorRecallResult:
        value = request.validated()
        outcome = self.integration_store.outcome(value.outcome_id)
        if outcome is None:
            raise KeyError(value.outcome_id)
        self._assert_recallable(outcome)
        sync = self.index_runtime.synchronize_task(
            outcome.task_id,
            process=True,
            causation_id=outcome.outcome_id,
            force=value.force_synchronize,
        )
        query = value.query.strip() or self._default_query(outcome)
        hydrated = self.index_runtime.retrieve(
            outcome.task_id,
            query,
            filters=RetrievalFilter(task_ids=(outcome.task_id,)),
            budget=RetrievalBudget(
                limit=value.maximum_results,
                candidate_limit=max(value.maximum_results * 8, value.maximum_results),
                max_output_chars=value.maximum_chars,
                max_document_chars=min(8_000, value.maximum_chars),
            ),
            vector_enabled=value.vector_enabled,
            synchronize=False,
            query_id=f"curator-recall:{outcome.outcome_id}",
        )
        context = self.context_bridge.build(
            outcome.task_id,
            query,
            filters=RetrievalFilter(task_ids=(outcome.task_id,)),
            maximum_entries=value.maximum_results,
            maximum_chars=value.maximum_chars,
            synchronize=False,
        )
        memory_ids = tuple(record.memory_id for record in hydrated.records)
        scope = self.index_runtime.index.scope_state(
            self.index_runtime.task_scope(outcome.task_id)
        )
        source_revision = str(scope.get("published_revision") or sync.source_revision)
        context_payload = context.to_dict()
        proof = CuratorContextProof.build(
            outcome=outcome,
            query_id=context.query_id or hydrated.retrieval.diagnostics.query_id,
            query_text=query,
            index_scope=context.index_scope,
            index_generation=context.index_generation,
            index_revision=source_revision,
            retrieved_memory_ids=memory_ids,
            context_payload=context_payload,
            consumer=CuratorConsumer.RETRIEVAL_CONTEXT,
            worker_request_id=value.worker_request_id,
            session_id=value.session_id,
            message_count=1,
            total_chars=context.total_chars,
            metadata={
                **dict(value.metadata),
                "outcome_kind": outcome.kind.value,
                "memory_source_revision": source_revision,
                "retrieval_result_digest": self._retrieval_digest(hydrated),
                "stale_document_ids": list(hydrated.stale_document_ids),
                "missing_document_ids": list(hydrated.missing_document_ids),
                "query_generated": not bool(value.query.strip()),
                "canonical_hydration": True,
                "index_is_canonical": False,
            },
        )
        proof, stored = self.integration_store.record_context_proof(proof)
        message = self._worker_message(
            outcome=outcome,
            context=context,
            proof=proof,
        )
        result = CuratorRecallResult(
            request=value,
            outcome=outcome,
            proof=proof,
            context=context,
            hydrated=hydrated,
            worker_message=message,
            index_sync=sync.to_dict(),
            stored=stored,
            diagnostics={
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "derived_index_owner": "MemoryIndexRuntime",
                "worker_context_owner": "WorkerRetrievalContextRuntime",
                "contains_index_dump": False,
                "stale_hit_count": len(hydrated.stale_document_ids),
                "missing_hit_count": len(hydrated.missing_document_ids),
                "retrieved_memory_count": len(memory_ids),
                "context_entry_count": len(context.entries),
            },
        )
        if value.require_present and not result.verified:
            raise CuratorContextVerificationError(
                f"committed memory {outcome.memory_id} was absent from retrieval context"
            )
        return result

    def verify_all(
        self,
        task_id: str,
        *,
        maximum_outcomes: int = 100,
        require_present: bool = True,
    ) -> tuple[CuratorRecallResult, ...]:
        outcomes = self.integration_store.outcomes(
            task_id=task_id,
            states=(),
            canonical_only=True,
            limit=maximum_outcomes,
        )
        selected = self._select_recall_outcomes(outcomes)
        return tuple(
            self.verify(
                CuratorRecallRequest(
                    outcome_id=outcome.outcome_id,
                    require_present=require_present,
                    metadata={"verification_mode": "verify_all"},
                )
            )
            for outcome in selected
        )

    def capture_baseline(
        self,
        *,
        task_id: str,
        query: str,
        maximum_results: int = 12,
        maximum_chars: int = 24_000,
    ) -> Mapping[str, Any]:
        self.index_runtime.synchronize_task(task_id, process=True)
        context = self.context_bridge.build(
            task_id,
            query,
            maximum_entries=maximum_results,
            maximum_chars=maximum_chars,
            synchronize=False,
        )
        return {
            "task_id": task_id,
            "query": query,
            "memory_ids": [entry.memory_id for entry in context.entries],
            "context_digest": self._context_digest(context),
            "index_scope": context.index_scope,
            "index_generation": context.index_generation,
            "captured_at": now_iso(),
            "canonical_memory_owner": "SQLiteStore.memory_records",
        }

    def compare(
        self,
        *,
        baseline: Mapping[str, Any],
        result: CuratorRecallResult,
    ) -> CuratorRecallComparison:
        task_id = str(baseline.get("task_id") or "")
        query = str(baseline.get("query") or "")
        if task_id != result.outcome.task_id:
            raise CuratorContextVerificationError(
                "recall comparison baseline belongs to another task"
            )
        before = tuple(str(item) for item in baseline.get("memory_ids") or ())
        after = tuple(entry.memory_id for entry in result.context.entries)
        before_set = set(before)
        after_set = set(after)
        before_digest = str(baseline.get("context_digest") or "")
        after_digest = self._context_digest(result.context)
        comparison = CuratorRecallComparison(
            task_id=task_id,
            query=query,
            before_memory_ids=before,
            after_memory_ids=after,
            added_memory_ids=tuple(item for item in after if item not in before_set),
            removed_memory_ids=tuple(item for item in before if item not in after_set),
            before_context_digest=before_digest,
            after_context_digest=after_digest,
            changed=before != after or before_digest != after_digest,
            outcome_id=result.outcome.outcome_id,
            proof_id=result.proof.proof_id,
        )
        if result.outcome.memory_id not in comparison.after_memory_ids:
            raise CuratorContextVerificationError(
                "recall comparison does not contain the committed memory"
            )
        return comparison

    @staticmethod
    def _select_recall_outcomes(
        outcomes: Sequence[CuratorOutcome],
    ) -> tuple[CuratorOutcome, ...]:
        by_memory: dict[str, CuratorOutcome] = {}
        for outcome in outcomes:
            if not outcome.canonical_memory_changed or not outcome.memory_id:
                continue
            existing = by_memory.get(outcome.memory_id)
            if existing is None:
                by_memory[outcome.memory_id] = outcome
                continue
            if outcome.kind is CuratorOutcomeKind.INDEX_PUBLISHED:
                by_memory[outcome.memory_id] = outcome
                continue
            if outcome.memory_revision > existing.memory_revision:
                by_memory[outcome.memory_id] = outcome
        return tuple(
            sorted(
                by_memory.values(),
                key=lambda item: (item.memory_id, item.memory_revision, item.outcome_id),
            )
        )

    @staticmethod
    def _assert_recallable(outcome: CuratorOutcome) -> None:
        if not outcome.canonical_memory_changed:
            raise CuratorContextVerificationError(
                "non-canonical curator outcome cannot be used as a recall proof"
            )
        if not outcome.memory_id or outcome.memory_revision <= 0:
            raise CuratorContextVerificationError(
                "curator outcome lacks committed memory identity"
            )
        if not outcome.deterministic_validation:
            raise CuratorContextVerificationError(
                "curator outcome did not pass deterministic validation"
            )

    @staticmethod
    def _default_query(outcome: CuratorOutcome) -> str:
        content = outcome.payload.get("receipt")
        parts = [outcome.subject, outcome.summary]
        if isinstance(content, Mapping):
            parts.append(str(content.get("reason") or ""))
        value = " ".join(part.strip() for part in parts if str(part).strip())
        if not value:
            raise CuratorContextVerificationError(
                "curator outcome cannot produce a recall query"
            )
        return value[:4000]

    @staticmethod
    def _worker_message(
        *,
        outcome: CuratorOutcome,
        context: MemoryContextBlock,
        proof: CuratorContextProof,
    ) -> AgentMessage:
        return AgentMessage(
            run_id=outcome.run_id,
            task_id=outcome.task_id,
            sender_role=AgentRole.SYSTEM,
            receiver_role=AgentRole.WORKER,
            intent=MessageIntent.OBSERVATION,
            content=CuratorRecallVerifier._context_text(context),
            metadata={
                "retrieval_scope": "task_memory",
                "curator_outcome_id": outcome.outcome_id,
                "curator_context_proof_id": proof.proof_id,
                "memory_id": outcome.memory_id,
                "memory_revision": outcome.memory_revision,
                "memory_retrieval_query_id": context.query_id,
                "memory_index_scope": context.index_scope,
                "memory_index_generation": context.index_generation,
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "derived_index_owner": "MemoryIndexRuntime",
                "contains_index_dump": False,
            },
        )

    @staticmethod
    def _context_text(context: MemoryContextBlock) -> str:
        lines = ["Curated task memory (validated and canonically hydrated):"]
        for entry in context.entries:
            lines.append(
                f"- [{entry.layer}] {entry.title}\n  {entry.content}"
            )
        return "\n".join(lines)

    @staticmethod
    def _context_digest(context: MemoryContextBlock) -> str:
        from .curator_models import stable_digest

        return stable_digest(context.to_dict())

    @staticmethod
    def _retrieval_digest(hydrated: HydratedMemoryResult) -> str:
        from .curator_models import stable_digest

        return stable_digest(hydrated.retrieval.to_dict())


__all__ = [
    "CuratorRecallComparison",
    "CuratorRecallRequest",
    "CuratorRecallResult",
    "CuratorRecallVerifier",
]
