from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from zyra_memory import (
    MemoryIndexRuntime,
    MemoryLayer,
    MemoryRecord,
    PublicationFencedError,
    RetrievalBudget,
    RetrievalFilter,
    SQLiteStore,
    VectorAvailability,
)


class RetrievalIndexFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.canonical = SQLiteStore(self.root / "canonical.sqlite3")
        self.canonical.initialize()
        self.records = [
            _record(
                "memory_cat",
                "A cat recovery playbook",
                "cat retry lease fencing",
                layer=MemoryLayer.EPISODIC,
                source_type="failure_event",
                source_id="failure-1",
                artifact_ids=["artifact-cat"],
                score=0.9,
                metadata={"session_id": "session-a", "failure_kind": "worker_killed"},
            ),
            _record(
                "memory_category",
                "Category taxonomy",
                "category concatenation catalog",
                layer=MemoryLayer.SEMANTIC,
                source_type="checkpoint",
                source_id="taxonomy",
                score=0.6,
                metadata={"session_id": "session-b"},
            ),
            _record(
                "memory_skill",
                "Skill deploy procedure",
                "deploy release verification workflow",
                layer=MemoryLayer.SKILL,
                source_type="skill_invocation",
                source_id="deploy-skill-event",
                artifact_ids=["artifact-skill"],
                score=0.8,
                metadata={"session_id": "session-a", "skill_name": "deploy"},
            ),
        ]
        self.canonical.save_memory_records(self.records)
        self.runtime = MemoryIndexRuntime(
            canonical_store=self.canonical,
            index_path=self.root / "derived-memory-index.sqlite3",
            worker_id="unit-worker",
            lease_ttl_seconds=5.0,
            heartbeat_interval_seconds=0.2,
        )
        self.sync = self.runtime.synchronize_task("task-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_real_fts_tokenization_filters_and_explicit_vector_degradation(self) -> None:
        self.assertEqual(self.sync.outcome.status, "ready")
        self.assertTrue(self.runtime.index.fts_available())
        with self.runtime.index.connection() as connection:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='retrieval_fts'"
            ).fetchone()["sql"]
        self.assertIn("fts5", sql.casefold())

        cat = self.runtime.retrieve("task-1", "cat")
        self.assertEqual([item.memory_id for item in cat.records], ["memory_cat"])
        self.assertEqual(
            cat.retrieval.diagnostics.vector_status,
            VectorAvailability.UNAVAILABLE,
        )
        self.assertIn("no vector provider", cat.retrieval.diagnostics.vector_reason)

        filtered = self.runtime.retrieve(
            "task-1",
            "deploy verification",
            filters=RetrievalFilter(
                task_ids=("task-1",),
                session_ids=("session-a",),
                artifact_ids=("artifact-skill",),
                skill_names=("deploy",),
                layers=(MemoryLayer.SKILL,),
            ),
        )
        self.assertEqual([item.memory_id for item in filtered.records], ["memory_skill"])

        excluded = self.runtime.retrieve(
            "task-1",
            "deploy",
            filters=RetrievalFilter(task_ids=("task-1",), session_ids=("session-b",)),
        )
        self.assertEqual(excluded.records, ())

    def test_fts_parser_treats_user_operators_as_terms_and_order_is_concurrent_deterministic(self) -> None:
        hostile = self.runtime.retrieve("task-1", 'cat OR (deploy) -category: "unterminated')
        self.assertIsNotNone(hostile.retrieval.diagnostics)

        def recall(_: int) -> tuple[tuple[str, float], ...]:
            result = self.runtime.retrieve(
                "task-1",
                "recovery deploy verification",
                budget=RetrievalBudget(limit=10, candidate_limit=40),
            )
            return tuple((hit.document_id, hit.score) for hit in result.retrieval.hits)

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(recall, range(64)))
        self.assertTrue(results[0])
        self.assertTrue(all(item == results[0] for item in results))

    def test_clean_rebuild_from_canonical_restores_results_without_copying_derived_rows(self) -> None:
        before = self.runtime.retrieve("task-1", "lease fencing")
        before_ids = [item.memory_id for item in before.records]
        canonical_before = self.canonical.task_memory_records("task-1")

        self.runtime.index.drop_derived_state()
        self.assertEqual(self.runtime.retrieve("task-1", "lease fencing").records, ())
        rebuilt = self.runtime.rebuild_from_canonical("task-1")
        after = self.runtime.retrieve("task-1", "lease fencing")

        self.assertEqual(rebuilt.outcome.status, "ready")
        self.assertEqual([item.memory_id for item in after.records], before_ids)
        self.assertEqual(
            [item.memory_id for item in self.canonical.task_memory_records("task-1")],
            [item.memory_id for item in canonical_before],
        )
        self.assertTrue(self.runtime.index.database_identity()["rebuildable"])
        self.assertFalse(self.runtime.index.database_identity()["canonical"])

    def test_skill_failure_and_source_filters_are_intersections(self) -> None:
        failure = self.runtime.retrieve(
            "task-1",
            "retry fencing",
            filters=RetrievalFilter(
                task_ids=("task-1",),
                failure_kinds=("worker_killed",),
                source_ids=("failure-1",),
                artifact_ids=("artifact-cat",),
            ),
        )
        self.assertEqual([item.memory_id for item in failure.records], ["memory_cat"])
        wrong_source = self.runtime.retrieve(
            "task-1",
            "retry fencing",
            filters=RetrievalFilter(
                task_ids=("task-1",),
                failure_kinds=("worker_killed",),
                source_ids=("another-source",),
            ),
        )
        self.assertEqual(wrong_source.records, ())


def _record(
    memory_id: str,
    summary: str,
    text: str,
    *,
    layer: MemoryLayer,
    source_type: str,
    source_id: str,
    artifact_ids: list[str] | None = None,
    score: float = 0.5,
    metadata: dict | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        run_id="run-1",
        task_id="task-1",
        layer=layer,
        source_type=source_type,
        source_id=source_id,
        summary=summary,
        content={"text": text},
        keywords=text.split(),
        artifact_ids=artifact_ids or [],
        score=score,
        metadata=metadata or {},
    )
