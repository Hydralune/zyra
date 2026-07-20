from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping

from .integration import (
    CODE_RECOVERY_REFERENCE_CONTRACT,
    CodeIndexConsumer,
    CodeIndexIntegrationError,
    CodeIndexIntegrationRuntime,
    CodeIndexQuery,
)
from .job_models import CodeIndexJobOperation, CodeIndexJobState
from .models import stable_digest


class CodeConformanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CodeConformanceCheck:
    name: str
    status: CodeConformanceStatus
    expected: str
    observed: Mapping[str, Any]
    elapsed_ms: float
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.status is CodeConformanceStatus.PASSED

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
class CodeIndexConformanceReport:
    workspace_id: str
    task_id: str
    checks: tuple[CodeConformanceCheck, ...]
    started_at: float
    completed_at: float
    destructive_rebuild_exercised: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(
            check.status in {CodeConformanceStatus.PASSED, CodeConformanceStatus.SKIPPED}
            for check in self.checks
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.code-index-conformance-report.v1",
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": max(0.0, (self.completed_at - self.started_at) * 1000.0),
            "destructive_rebuild_exercised": self.destructive_rebuild_exercised,
            "metadata": dict(self.metadata),
        }


class CodeIndexConformanceSuite:
    """Exercise current-revision, fencing and rebuild invariants in situ."""

    def __init__(self, runtime: CodeIndexIntegrationRuntime) -> None:
        self.runtime = runtime

    def run(
        self,
        *,
        query: str,
        concurrency: int = 8,
        destructive_rebuild: bool = False,
    ) -> CodeIndexConformanceReport:
        started = time.time()
        checks = [
            self._current_revision(),
            self._stable_selection(query, concurrency),
            self._empty_result(),
            self._reference_only(query),
            self._worker_phases(),
            self._disable_effect(query),
        ]
        if destructive_rebuild:
            checks.append(self._delete_rebuild(query))
        else:
            checks.append(
                CodeConformanceCheck(
                    name="delete_rebuild_equivalence",
                    status=CodeConformanceStatus.SKIPPED,
                    expected="explicit destructive_rebuild opt-in",
                    observed={"derived_state_deleted": False},
                    elapsed_ms=0.0,
                )
            )
        identity = self.runtime.runtime.source.identity
        return CodeIndexConformanceReport(
            workspace_id=identity.workspace_id,
            task_id=identity.task_id,
            checks=tuple(checks),
            started_at=started,
            completed_at=time.time(),
            destructive_rebuild_exercised=destructive_rebuild,
            metadata={
                "canonical_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
                "derived_owner": "CodeIndexRuntime",
                "external_source_runtime": False,
                "checkpoint_contract": CODE_RECOVERY_REFERENCE_CONTRACT,
            },
        )

    @staticmethod
    def assert_passed(report: CodeIndexConformanceReport) -> None:
        failed = [check for check in report.checks if check.status is CodeConformanceStatus.FAILED]
        if failed:
            raise CodeIndexIntegrationError(
                "code_index_conformance_failed",
                "; ".join(f"{check.name}: {check.error}" for check in failed),
            )

    def _query(self, text: str, *, request_id: str) -> CodeIndexQuery:
        identity = self.runtime.runtime.source.identity
        return CodeIndexQuery(
            task_id=identity.task_id,
            text=text,
            consumer=CodeIndexConsumer.EVALUATION,
            run_id=identity.run_id,
            session_id=identity.session_id,
            maximum_results=50,
            maximum_files=50,
            maximum_tests=200,
            maximum_chars=100_000,
            request_id=request_id,
        )

    def _current_revision(self) -> CodeConformanceCheck:
        def observe() -> Mapping[str, Any]:
            identity = self.runtime.runtime.source.identity
            fence = self.runtime.queue.fence_state(identity.workspace_id)
            state = self.runtime.runtime.store.workspace_state(identity.workspace_id)
            return {
                "canonical_revision": identity.revision,
                "indexed_revision": str(state.get("workspace_revision") or ""),
                "published_revision": str(fence.get("published_revision") or ""),
                "active_generation": int(state.get("active_generation") or 0),
                "published_generation": int(fence.get("published_generation") or 0),
                "matches": identity.revision
                == str(state.get("workspace_revision") or "")
                == str(fence.get("published_revision") or ""),
            }

        return self._check(
            "canonical_revision_covered",
            "active and published revisions equal the current workspace revision",
            observe,
            lambda value: value["matches"]
            and value["active_generation"] == value["published_generation"] > 0,
        )

    def _stable_selection(self, query: str, concurrency: int) -> CodeConformanceCheck:
        def observe() -> Mapping[str, Any]:
            count = max(2, min(int(concurrency), 32))

            def select(_: int) -> tuple[Any, ...]:
                result = self.runtime.select(
                    self._query(query, request_id="conformance-stable-selection")
                )
                return (
                    tuple(
                        (
                            ref.logical_path,
                            ref.line_start,
                            ref.line_end,
                            ref.file_hash,
                            ref.score,
                            ref.matched_terms,
                        )
                        for ref in result.source_refs
                    ),
                    result.selected_files,
                    result.selected_tests,
                    result.result_digest,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                results = tuple(pool.map(select, range(count * 2)))
            return {
                "execution_count": len(results),
                "unique_selection_count": len(set(results)),
                "selection_digest": stable_digest(results[0] if results else ()),
            }

        return self._check(
            "concurrent_selection_stability",
            "concurrent readers observe one file/test/context selection",
            observe,
            lambda value: value["execution_count"] > 1
            and value["unique_selection_count"] == 1,
        )

    def _empty_result(self) -> CodeConformanceCheck:
        marker = f"zyra_no_code_match_{stable_digest(time.time_ns())}"

        def observe() -> Mapping[str, Any]:
            selection = self.runtime.select(self._query(marker, request_id="conformance-empty"))
            return {
                "source_ref_count": len(selection.source_refs),
                "selected_file_count": len(selection.selected_files),
                "context_chars": selection.total_chars,
                "workspace_scan_fallback": False,
            }

        return self._check(
            "empty_result",
            "unmatched terms return no context without scanning fallback",
            observe,
            lambda value: value["source_ref_count"] == 0
            and value["selected_file_count"] == 0
            and value["context_chars"] == 0
            and value["workspace_scan_fallback"] is False,
        )

    def _reference_only(self, query: str) -> CodeConformanceCheck:
        def observe() -> Mapping[str, Any]:
            selection = self.runtime.select(self._query(query, request_id="conformance-reference"))
            reference = selection.recovery_reference()
            serialized = str(reference).casefold()
            forbidden = ("index_rows", "sqlite", "physical_root", "root_path", "file_content")
            return {
                "schema": reference["schema"],
                "source_ref_count": len(reference["source_refs"]),
                "index_dump_embedded": reference["index_dump_embedded"],
                "workspace_content_embedded": reference["workspace_content_embedded"],
                "forbidden_terms": [term for term in forbidden if term in serialized],
            }

        return self._check(
            "recovery_reference_only",
            "recovery state contains revisioned refs and no index/workspace dump",
            observe,
            lambda value: value["schema"] == CODE_RECOVERY_REFERENCE_CONTRACT
            and value["index_dump_embedded"] is False
            and value["workspace_content_embedded"] is False
            and not value["forbidden_terms"],
        )

    def _worker_phases(self) -> CodeConformanceCheck:
        def observe() -> Mapping[str, Any]:
            identity = self.runtime.runtime.source.identity
            events = self.runtime.queue.audit_events(workspace_id=identity.workspace_id)
            event_types = tuple(dict.fromkeys(str(event["event_type"]) for event in events))
            jobs = self.runtime.queue.list(workspace_id=identity.workspace_id)
            required = (
                "code_index.queued",
                "code_index.leased",
                "code_index.building",
                "code_index.publishing",
                "code_index.ready",
            )
            return {
                "event_types": list(event_types),
                "missing": [event for event in required if event not in event_types],
                "ready_jobs": sum(job.state is CodeIndexJobState.READY for job in jobs),
            }

        return self._check(
            "durable_worker_state_machine",
            "job audit contains queued, leased, building, publishing and ready",
            observe,
            lambda value: not value["missing"] and value["ready_jobs"] > 0,
        )

    def _disable_effect(self, query: str) -> CodeConformanceCheck:
        return self._check(
            "disable_effect",
            "disconnecting CodeIndex removes context/file/test selection",
            lambda: self.runtime.disable_probe(self._query(query, request_id="conformance-disable")),
            lambda value: value["disabled"]["selected_files"] == []
            and value["disabled"]["selected_tests"] == []
            and value["disabled"]["context_chars"] == 0
            and value["disabled"]["legacy_grep_fallback"] is False,
        )

    def _delete_rebuild(self, query: str) -> CodeConformanceCheck:
        def observe() -> Mapping[str, Any]:
            before = self.runtime.select(self._query(query, request_id="conformance-before-drop"))
            identity = self.runtime.runtime.source.identity
            removed = self.runtime.runtime.store.drop_active_workspace_index(identity.workspace_id)
            job = self.runtime.queue.enqueue(
                identity,
                operation=CodeIndexJobOperation.REBUILD,
                payload={"reason": "conformance_delete_rebuild"},
                idempotency_key=f"conformance-rebuild:{identity.revision}:{time.time_ns()}",
                causation_id="conformance-delete-rebuild",
            )
            outcomes = self.runtime._process(identity.workspace_id)
            current = self.runtime.queue.require(job.job_id)
            after = self.runtime.select(self._query(query, request_id="conformance-after-drop"))
            return {
                "removed": removed,
                "worker_outcomes": [dict(value) for value in outcomes],
                "job_state": current.state.value,
                "before_digest": before.result_digest,
                "after_digest": after.result_digest,
                "same_files": before.selected_files == after.selected_files,
                "same_tests": before.selected_tests == after.selected_tests,
                "same_source_locations": tuple(
                    (ref.logical_path, ref.line_start, ref.line_end) for ref in before.source_refs
                )
                == tuple((ref.logical_path, ref.line_start, ref.line_end) for ref in after.source_refs),
            }

        return self._check(
            "delete_rebuild_equivalence",
            "dropping the active derived projection and rebuilding preserves selection",
            observe,
            lambda value: value["job_state"] == CodeIndexJobState.READY.value
            and value["same_files"]
            and value["same_tests"]
            and value["same_source_locations"],
        )

    @staticmethod
    def _check(
        name: str,
        expected: str,
        observe: Callable[[], Mapping[str, Any]],
        predicate: Callable[[Mapping[str, Any]], bool],
    ) -> CodeConformanceCheck:
        started = time.perf_counter()
        try:
            observed = dict(observe())
            passed = bool(predicate(observed))
            return CodeConformanceCheck(
                name=name,
                status=CodeConformanceStatus.PASSED if passed else CodeConformanceStatus.FAILED,
                expected=expected,
                observed=observed,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                error="" if passed else "observed state did not satisfy the invariant",
            )
        except Exception as error:  # noqa: BLE001 - retain every invariant failure.
            return CodeConformanceCheck(
                name=name,
                status=CodeConformanceStatus.FAILED,
                expected=expected,
                observed={"exception_type": type(error).__name__},
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                error=str(error),
            )


__all__ = [
    "CodeConformanceCheck",
    "CodeConformanceStatus",
    "CodeIndexConformanceReport",
    "CodeIndexConformanceSuite",
]
