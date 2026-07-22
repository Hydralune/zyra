from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zyra_memory import (
    RETRIEVAL_ALGORITHM_PROTOCOL,
    IndexSourceKind,
    MemoryIndexRuntime,
    QueryIntentCategory,
    RetrievalBudget,
    RetrievalHit,
    RetrievalQuery,
    TypeScriptRetrievalAlgorithmError,
    TypeScriptRetrievalAlgorithmsPort,
    VectorAvailability,
)
from zyra_memory.retrieval_models import (
    QueryIntent,
    RetrievalFilter,
    TemporalConstraint,
)


ROOT = Path(__file__).resolve().parents[2]


def _hit(
    identity: str,
    score: float,
    content: str,
    *,
    importance: float = 0.0,
    event_at: str = "",
) -> RetrievalHit:
    return RetrievalHit(
        document_id=identity,
        source_kind=IndexSourceKind.MEMORY,
        source_id=identity,
        title=identity,
        content=content,
        score=score,
        task_id="task-ts-retrieval",
        importance=importance,
        event_at=event_at,
        generation=1,
        content_digest=identity.ljust(64, "0")[:64],
    )


class TypeScriptRetrievalAlgorithmsPortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = TypeScriptRetrievalAlgorithmsPort(project_root=ROOT)
        self.assertTrue(self.port.available, self.port.health())

    def test_real_typescript_analysis_and_ranking(self) -> None:
        query = self.port.enrich_query(
            RetrievalQuery(
                text="how to fix the provider failure yesterday",
                budget=RetrievalBudget(limit=2, candidate_limit=4, mmr_lambda=0.35),
                request_time="2026-07-22T12:00:00+00:00",
            )
        )
        self.assertEqual(query.intent.category, QueryIntentCategory.TEMPORAL)
        self.assertEqual(query.filters.temporal.start_at, "2026-07-21T00:00:00+00:00")
        result = self.port.rank(
            query,
            fts_hits=(
                _hit("a", 1.0, "provider failure retry route", importance=0.9),
                _hit("b", 0.99, "provider failure retry route duplicate", importance=0.8),
                _hit("c", 0.7, "credential refresh rotation", importance=0.7),
            ),
            vector_hits=(
                _hit("a", 0.8, "provider failure retry route", importance=0.9),
            ),
            vector_status=VectorAvailability.AVAILABLE,
            vector_reason="test vector",
            index_scope="memory:task-ts-retrieval",
            index_generation=1,
            elapsed_ms=1.0,
        )
        self.assertEqual([item.document_id for item in result.hits], ["a", "c"])
        self.assertTrue(
            any(
                warning == "typescript_retrieval_algorithms_active"
                for warning in result.diagnostics.warnings
            )
        )
        self.assertTrue(
            any(
                voice.detail.get("algorithm_owner") == "typescript"
                for voice in result.hits[0].voices
            )
        )

    def test_missing_runtime_fails_closed(self) -> None:
        unavailable = TypeScriptRetrievalAlgorithmsPort(
            project_root=ROOT,
            bun_executable=str(ROOT / "missing-bun"),
        )
        with self.assertRaises(TypeScriptRetrievalAlgorithmError) as failure:
            unavailable.enrich_query(RetrievalQuery(text="provider failure"))
        self.assertEqual(failure.exception.code, "typescript_retrieval_spawn_failed")

    def test_disabling_supplement_changes_real_retrieve_path(self) -> None:
        class CanonicalStore:
            def task_memory_records(self, _task_id: str):
                return []

        with tempfile.TemporaryDirectory() as directory:
            runtime = MemoryIndexRuntime(
                canonical_store=CanonicalStore(),
                index_path=Path(directory) / "retrieval.sqlite3",
                enable_typescript_supplement=False,
            )
            with self.assertRaisesRegex(RuntimeError, "no Python ranking fallback"):
                runtime.retrieve("task-disabled", "provider state")

    def test_explicit_intent_and_temporal_filter_remain_caller_owned(self) -> None:
        intent = QueryIntent(
            category=QueryIntentCategory.ENTITY,
            confidence=0.95,
            signals=(QueryIntentCategory.ENTITY,),
            vector_bias=1.4,
            fts_bias=0.7,
            importance_bias=1.2,
        )
        temporal = TemporalConstraint(
            start_at="2026-01-01T00:00:00+00:00",
            end_at="2026-02-01T00:00:00+00:00",
            tags=("explicit",),
            precision="month",
            source_text="caller supplied",
        )
        enriched = self.port.enrich_query(
            RetrievalQuery(
                text="what happened yesterday",
                intent=intent,
                filters=RetrievalFilter(temporal=temporal),
                request_time="2026-07-22T12:00:00+00:00",
            )
        )
        self.assertEqual(enriched.intent, intent)
        self.assertEqual(enriched.filters.temporal, temporal)

    def test_untrusted_typescript_output_cannot_invent_a_candidate(self) -> None:
        class MutatingPort(TypeScriptRetrievalAlgorithmsPort):
            def _invoke(self, request, *, timeout_seconds=None):
                payload = request["payload"]
                return {
                    "protocol": RETRIEVAL_ALGORITHM_PROTOCOL,
                    "requestId": request["requestId"],
                    "ok": True,
                    "result": {
                        "intent": payload["intent"],
                        "temporal": payload["temporal"],
                        "hits": [
                            {
                                "documentId": "invented",
                                "score": 1.0,
                                "rank": 1,
                                "content": "not submitted",
                                "voices": [],
                            }
                        ],
                        "candidateCount": 1,
                        "fusedCount": 1,
                        "truncated": False,
                        "evidenceDigest": "a" * 64,
                    },
                }

        query = RetrievalQuery(
            text="provider",
            intent=QueryIntent(
                category=QueryIntentCategory.GENERAL,
                confidence=0.0,
                signals=(),
            ),
        )
        with self.assertRaises(TypeScriptRetrievalAlgorithmError) as failure:
            MutatingPort(project_root=ROOT).rank(
                query,
                fts_hits=(_hit("real", 1.0, "provider"),),
                vector_hits=(),
                vector_status=VectorAvailability.DISABLED,
                vector_reason="disabled",
                index_scope="memory:test",
                index_generation=1,
                elapsed_ms=0.0,
            )
        self.assertEqual(
            failure.exception.code,
            "typescript_retrieval_partition_invalid",
        )


if __name__ == "__main__":
    unittest.main()
