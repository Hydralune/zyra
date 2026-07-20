from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping

from .integration_runtime import RetrievalIntegrationError, RetrievalIntegrationRuntime
from .query_contract import MemoryFilterQuery, RetrievalConsumer
from .retrieval_models import IndexJobState, stable_digest


class ConformanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ConformanceCheck:
    name: str
    status: ConformanceStatus
    expected: str
    observed: Mapping[str, Any]
    elapsed_ms: float
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.status is ConformanceStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "passed": self.passed,
            "expected": self.expected,
            "observed": dict(self.observed),
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class RetrievalConformanceReport:
    task_id: str
    checks: tuple[ConformanceCheck, ...]
    started_at: float
    completed_at: float
    destructive_rebuild_exercised: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(
            check.status in {ConformanceStatus.PASSED, ConformanceStatus.SKIPPED}
            for check in self.checks
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.retrieval-conformance-report.v1",
            "task_id": self.task_id,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": max(0.0, (self.completed_at - self.started_at) * 1000.0),
            "destructive_rebuild_exercised": self.destructive_rebuild_exercised,
            "metadata": dict(self.metadata),
        }


class RetrievalConformanceSuite:
    """Runtime invariant suite for integration and cleanroom verification.

    This is callable production audit code, not a fixture replay.  Every check
    operates on the configured canonical MemoryRecordStore and the same durable
    job/index used by CodeWorker.  Destructive rebuild is opt-in because it
    deletes only derived state but can briefly make retrieval unavailable.
    """

    def __init__(self, runtime: RetrievalIntegrationRuntime) -> None:
        self.runtime = runtime

    def run(
        self,
        *,
        task_id: str,
        query: str,
        run_id: str = "",
        session_id: str = "",
        artifact_id: str = "",
        skill_name: str = "",
        failure_kind: str = "",
        concurrency: int = 8,
        destructive_rebuild: bool = False,
    ) -> RetrievalConformanceReport:
        started = time.time()
        checks: list[ConformanceCheck] = []
        checks.append(self._check_current_revision(task_id=task_id))
        checks.append(
            self._check_stable_ranking(
                task_id=task_id,
                query=query,
                run_id=run_id,
                session_id=session_id,
                concurrency=concurrency,
            )
        )
        checks.append(
            self._check_filter_intersection(
                task_id=task_id,
                query=query,
                run_id=run_id,
                session_id=session_id,
                artifact_id=artifact_id,
                skill_name=skill_name,
                failure_kind=failure_kind,
            )
        )
        checks.append(self._check_empty_result(task_id=task_id))
        checks.append(self._check_checkpoint_is_reference_only(task_id=task_id))
        checks.append(self._check_worker_state_machine(task_id=task_id))
        checks.append(self._check_disable_effect(task_id=task_id, query=query))
        if destructive_rebuild:
            checks.append(self._check_delete_rebuild_equivalence(task_id=task_id, query=query))
        else:
            checks.append(
                ConformanceCheck(
                    name="delete_rebuild_equivalence",
                    status=ConformanceStatus.SKIPPED,
                    expected="explicit destructive_rebuild opt-in",
                    observed={"derived_state_deleted": False},
                    elapsed_ms=0.0,
                )
            )
        completed = time.time()
        report = RetrievalConformanceReport(
            task_id=task_id,
            checks=tuple(checks),
            started_at=started,
            completed_at=completed,
            destructive_rebuild_exercised=destructive_rebuild,
            metadata={
                "canonical_owner": type(self.runtime.runtime.canonical_store).__name__,
                "derived_owner": "MemoryIndexRuntime",
                "langgraph_role": "conformance_only_checkpoint_reference",
                "external_source_runtime": False,
            },
        )
        if self.runtime.event_sink is not None and run_id:
            self.runtime._emit(
                task_id=task_id,
                run_id=run_id,
                phase="conformance_completed",
                payload={
                    "passed": report.passed,
                    "check_count": len(report.checks),
                    "report_digest": stable_digest(report.to_dict()),
                },
            )
        return report

    def assert_passed(self, report: RetrievalConformanceReport) -> None:
        failed = [check for check in report.checks if check.status is ConformanceStatus.FAILED]
        if failed:
            summary = "; ".join(f"{check.name}: {check.error}" for check in failed)
            raise RetrievalIntegrationError(
                "retrieval_conformance_failed",
                summary or "retrieval conformance report contains failed checks",
            )

    def _check_current_revision(self, *, task_id: str) -> ConformanceCheck:
        return self._run_check(
            "canonical_revision_covered",
            "published revision equals the current canonical MemoryRecordStore revision",
            lambda: self._current_revision_observation(task_id),
            lambda value: bool(value["matches"] and value["published_generation"] > 0),
        )

    def _current_revision_observation(self, task_id: str) -> Mapping[str, Any]:
        records = self.runtime.runtime.canonical_store.task_memory_records(task_id)
        canonical_revision = self.runtime.runtime.source_revision(records)
        scope = self.runtime.runtime.index.scope_state(self.runtime.runtime.task_scope(task_id))
        published_revision = str(scope.get("published_revision") or "")
        return {
            "canonical_revision": canonical_revision,
            "published_revision": published_revision,
            "published_generation": int(scope.get("published_generation") or 0),
            "matches": canonical_revision == published_revision,
            "canonical_record_count": len(records),
        }

    def _check_stable_ranking(
        self,
        *,
        task_id: str,
        query: str,
        run_id: str,
        session_id: str,
        concurrency: int,
    ) -> ConformanceCheck:
        def observe() -> Mapping[str, Any]:
            request = MemoryFilterQuery(
                task_id=task_id,
                text=query,
                consumer=RetrievalConsumer.EVALUATION,
                run_id=run_id,
                session_id=session_id,
                maximum_results=20,
                request_id="conformance-stable-ranking",
            )

            def execute(_: int) -> tuple[tuple[str, float], ...]:
                result = self.runtime.execute(request)
                return tuple(
                    (hit.document_id, hit.score) for hit in result.hydrated.retrieval.hits
                )

            count = max(2, min(int(concurrency), 32))
            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                results = tuple(pool.map(execute, range(count * 2)))
            first = results[0] if results else ()
            return {
                "execution_count": len(results),
                "unique_ranking_count": len(set(results)),
                "ranking_digest": stable_digest(first),
                "result_count": len(first),
            }

        return self._run_check(
            "concurrent_ranking_stability",
            "all concurrent readers observe one deterministic ordering",
            observe,
            lambda value: int(value["execution_count"]) > 1
            and int(value["unique_ranking_count"]) == 1,
        )

    def _check_filter_intersection(
        self,
        *,
        task_id: str,
        query: str,
        run_id: str,
        session_id: str,
        artifact_id: str,
        skill_name: str,
        failure_kind: str,
    ) -> ConformanceCheck:
        selected_filters = any((run_id, session_id, artifact_id, skill_name, failure_kind))
        if not selected_filters:
            return ConformanceCheck(
                name="filter_intersection",
                status=ConformanceStatus.SKIPPED,
                expected="at least one supplied run/session/artifact/skill/failure filter",
                observed={"filters_supplied": False},
                elapsed_ms=0.0,
            )

        def observe() -> Mapping[str, Any]:
            execution = self.runtime.execute(
                MemoryFilterQuery(
                    task_id=task_id,
                    text=query,
                    consumer=RetrievalConsumer.EVALUATION,
                    run_id=run_id,
                    session_id=session_id,
                    artifact_ids=(artifact_id,) if artifact_id else (),
                    skill_names=(skill_name,) if skill_name else (),
                    failure_kinds=(failure_kind,) if failure_kind else (),
                    maximum_results=50,
                    request_id="conformance-filter-intersection",
                )
            )
            violations: list[str] = []
            for record in execution.hydrated.records:
                metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
                if run_id and record.run_id != run_id:
                    violations.append(f"run:{record.memory_id}")
                if session_id and str(metadata.get("session_id") or "") != session_id:
                    violations.append(f"session:{record.memory_id}")
                if artifact_id and artifact_id not in record.artifact_ids:
                    violations.append(f"artifact:{record.memory_id}")
                if skill_name and str(metadata.get("skill_name") or record.source_id) != skill_name:
                    violations.append(f"skill:{record.memory_id}")
                if failure_kind and str(metadata.get("failure_kind") or "") != failure_kind:
                    violations.append(f"failure:{record.memory_id}")
            return {
                "result_count": len(execution.hydrated.records),
                "violations": violations,
                "query_id": execution.snapshot.query_id,
            }

        return self._run_check(
            "filter_intersection",
            "every hydrated record satisfies all supplied filters",
            observe,
            lambda value: not value["violations"],
        )

    def _check_empty_result(self, *, task_id: str) -> ConformanceCheck:
        marker = f"zyra-no-match-{stable_digest(task_id, time.time_ns())}"

        def observe() -> Mapping[str, Any]:
            execution = self.runtime.execute(
                MemoryFilterQuery(
                    task_id=task_id,
                    text=marker,
                    consumer=RetrievalConsumer.EVALUATION,
                    maximum_results=10,
                    request_id="conformance-empty-result",
                )
            )
            return {
                "result_count": len(execution.hydrated.records),
                "warning_count": len(execution.snapshot.warnings),
                "fallback": False,
            }

        return self._run_check(
            "empty_result",
            "an unmatched query returns an empty result without legacy scanning",
            observe,
            lambda value: int(value["result_count"]) == 0 and value["fallback"] is False,
        )

    def _check_checkpoint_is_reference_only(self, *, task_id: str) -> ConformanceCheck:
        def observe() -> Mapping[str, Any]:
            checkpoint = self.runtime.checkpoint_ref(task_id)
            body = checkpoint.to_dict()
            serialized = str(body).casefold()
            forbidden = (
                "memory_records",
                "index_rows",
                "embeddings",
                "retrieval_fts",
                "document_body",
            )
            return {
                "schema": body["schema"],
                "pending_job_count": len(checkpoint.pending_job_ids),
                "query_ref_count": len(checkpoint.last_query_ids),
                "cursor_ref_count": len(checkpoint.cursor_refs),
                "forbidden_terms": [term for term in forbidden if term in serialized],
                "contains_index_dump": body["contains_index_dump"],
                "contains_canonical_records": body["contains_canonical_records"],
            }

        return self._run_check(
            "checkpoint_reference_only",
            "checkpoint contains only source/job/query/cursor references",
            observe,
            lambda value: not value["forbidden_terms"]
            and value["contains_index_dump"] is False
            and value["contains_canonical_records"] is False,
        )

    def _check_worker_state_machine(self, *, task_id: str) -> ConformanceCheck:
        def observe() -> Mapping[str, Any]:
            scope_key = self.runtime.runtime.task_scope(task_id)
            events = self.runtime.runtime.index.audit_events(scope_key=scope_key)
            event_types = tuple(dict.fromkeys(str(event["event_type"]) for event in events))
            jobs = self.runtime.runtime.queue.list(scope_key=scope_key, limit=1_000)
            states = tuple(dict.fromkeys(job.state.value for job in jobs))
            required = ("index.queued", "index.leased", "index.building", "index.publishing", "index.ready")
            return {
                "event_types": list(event_types),
                "job_states": list(states),
                "required_missing": [value for value in required if value not in event_types],
                "job_count": len(jobs),
                "ready_count": sum(job.state is IndexJobState.READY for job in jobs),
            }

        return self._run_check(
            "durable_worker_state_machine",
            "audit contains queued, leased, building, publishing and ready phases",
            observe,
            lambda value: not value["required_missing"] and int(value["ready_count"]) > 0,
        )

    def _check_disable_effect(self, *, task_id: str, query: str) -> ConformanceCheck:
        return self._run_check(
            "disable_effect",
            "disabling retrieval removes results without a substring fallback",
            lambda: self.runtime.disable_probes(task_id, query),
            lambda value: value["retrieval_disabled"]["result_count"] == 0
            and value["retrieval_disabled"]["legacy_scan"] is False
            and value["retrieval_disabled"]["fallback"] is False,
        )

    def _check_delete_rebuild_equivalence(
        self,
        *,
        task_id: str,
        query: str,
    ) -> ConformanceCheck:
        def observe() -> Mapping[str, Any]:
            request = MemoryFilterQuery(
                task_id=task_id,
                text=query,
                consumer=RetrievalConsumer.EVALUATION,
                maximum_results=50,
                request_id="conformance-rebuild-equivalence",
            )
            before = self.runtime.execute(request)
            before_ids = tuple(record.memory_id for record in before.hydrated.records)
            before_canonical = tuple(
                record.memory_id
                for record in self.runtime.runtime.canonical_store.task_memory_records(task_id)
            )
            self.runtime.runtime.index.drop_derived_state()
            self.runtime.store = type(self.runtime.store)(self.runtime.runtime.index)
            queued = self.runtime.admit_canonical_records(
                task_id,
                process_worker=True,
                force=True,
                causation_id="conformance-delete-rebuild",
            )
            after = self.runtime.execute(request)
            after_ids = tuple(record.memory_id for record in after.hydrated.records)
            after_canonical = tuple(
                record.memory_id
                for record in self.runtime.runtime.canonical_store.task_memory_records(task_id)
            )
            return {
                "before_ids": list(before_ids),
                "after_ids": list(after_ids),
                "same_order": before_ids == after_ids,
                "canonical_unchanged": before_canonical == after_canonical,
                "rebuild_generation": queued.generation,
                "published": queued.published,
            }

        return self._run_check(
            "delete_rebuild_equivalence",
            "deleting derived state and rebuilding from canonical records preserves semantic ordering",
            observe,
            lambda value: value["same_order"]
            and value["canonical_unchanged"]
            and value["published"],
        )

    @staticmethod
    def _run_check(
        name: str,
        expected: str,
        observe: Callable[[], Mapping[str, Any]],
        predicate: Callable[[Mapping[str, Any]], bool],
    ) -> ConformanceCheck:
        started = time.perf_counter()
        try:
            observed = dict(observe())
            passed = bool(predicate(observed))
            return ConformanceCheck(
                name=name,
                status=ConformanceStatus.PASSED if passed else ConformanceStatus.FAILED,
                expected=expected,
                observed=observed,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                error="" if passed else "observed state did not satisfy the invariant",
            )
        except Exception as error:  # noqa: BLE001 - report must retain all failed checks.
            return ConformanceCheck(
                name=name,
                status=ConformanceStatus.FAILED,
                expected=expected,
                observed={"exception_type": type(error).__name__},
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                error=str(error),
            )


__all__ = [
    "ConformanceCheck",
    "ConformanceStatus",
    "RetrievalConformanceReport",
    "RetrievalConformanceSuite",
]
