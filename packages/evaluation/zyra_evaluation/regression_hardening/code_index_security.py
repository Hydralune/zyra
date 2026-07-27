from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .contracts import (
    AssertionSeverity,
    CaseExecutionBuffer,
    FailureKind,
    ObservationKind,
    stable_digest,
)


class IndexPhase(StrEnum):
    ADMISSION = "admission"
    BUILD = "build"
    QUERY = "query"
    INVALIDATE = "invalidate"
    REBUILD = "rebuild"
    SELECT_CONTEXT = "select_context"
    SELECT_TESTS = "select_tests"
    PATCH_REFRESH = "patch_refresh"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class IndexBudget:
    maximum_files: int
    maximum_bytes: int
    maximum_results: int
    maximum_output_chars: int

    def __post_init__(self) -> None:
        if min(
            self.maximum_files,
            self.maximum_bytes,
            self.maximum_results,
            self.maximum_output_chars,
        ) < 0:
            raise ValueError("code-index budgets cannot be negative")

    def to_dict(self) -> dict[str, int]:
        return {
            "maximum_files": self.maximum_files,
            "maximum_bytes": self.maximum_bytes,
            "maximum_results": self.maximum_results,
            "maximum_output_chars": self.maximum_output_chars,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexObservation:
    observation_id: str
    request_id: str
    run_id: str
    workspace_id: str
    phase: IndexPhase
    source_revision: str
    generation_before: int
    generation_after: int
    permission_receipt_id: str
    permission_effect: str
    budget: IndexBudget
    files_scanned: int = 0
    bytes_scanned: int = 0
    result_count: int = 0
    output_chars: int = 0
    changed_paths: tuple[str, ...] = ()
    selected_context: tuple[str, ...] = ()
    selected_tests: tuple[str, ...] = ()
    transaction_id: str = ""
    causation_id: str = ""
    content_digest: str = ""
    status: str = ""
    error_code: str = ""
    truncated: bool = False
    fallback_kind: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def budget_valid(self) -> bool:
        return (
            self.files_scanned <= self.budget.maximum_files
            and self.bytes_scanned <= self.budget.maximum_bytes
            and self.result_count <= self.budget.maximum_results
            and self.output_chars <= self.budget.maximum_output_chars
        )

    @property
    def generation_advanced(self) -> bool:
        return self.generation_after > self.generation_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "phase": self.phase.value,
            "source_revision": self.source_revision,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "generation_advanced": self.generation_advanced,
            "permission_receipt_id": self.permission_receipt_id,
            "permission_effect": self.permission_effect,
            "budget": self.budget.to_dict(),
            "files_scanned": self.files_scanned,
            "bytes_scanned": self.bytes_scanned,
            "result_count": self.result_count,
            "output_chars": self.output_chars,
            "budget_valid": self.budget_valid,
            "changed_paths": list(self.changed_paths),
            "selected_context": list(self.selected_context),
            "selected_tests": list(self.selected_tests),
            "transaction_id": self.transaction_id,
            "causation_id": self.causation_id,
            "content_digest": self.content_digest,
            "status": self.status,
            "error_code": self.error_code,
            "truncated": self.truncated,
            "fallback_kind": self.fallback_kind,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class IndexFinding:
    code: str
    observation_id: str
    phase: IndexPhase
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "observation_id": self.observation_id,
            "phase": self.phase.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexSecurityReport:
    observations: tuple[CodeIndexObservation, ...]
    findings: tuple[IndexFinding, ...]
    phase_counts: Mapping[str, int]
    generations: tuple[int, ...]
    context_changed: bool
    test_selection_changed: bool
    patch_refresh_observed: bool
    digest: str

    @property
    def valid(self) -> bool:
        required = {
            IndexPhase.ADMISSION,
            IndexPhase.BUILD,
            IndexPhase.QUERY,
            IndexPhase.INVALIDATE,
            IndexPhase.REBUILD,
            IndexPhase.SELECT_CONTEXT,
            IndexPhase.SELECT_TESTS,
            IndexPhase.PATCH_REFRESH,
            IndexPhase.REJECTED,
        }
        observed = {item.phase for item in self.observations}
        return (
            not self.findings
            and required.issubset(observed)
            and self.context_changed
            and self.test_selection_changed
            and self.patch_refresh_observed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-code-index-security/v1",
            "valid": self.valid,
            "observations": [item.to_dict() for item in self.observations],
            "findings": [item.to_dict() for item in self.findings],
            "phase_counts": dict(sorted(self.phase_counts.items())),
            "generations": list(self.generations),
            "context_changed": self.context_changed,
            "test_selection_changed": self.test_selection_changed,
            "patch_refresh_observed": self.patch_refresh_observed,
            "digest": self.digest,
        }


class CodeIndexSecurityAuditor:
    def audit(
        self,
        observations: Sequence[CodeIndexObservation],
    ) -> CodeIndexSecurityReport:
        findings: list[IndexFinding] = []
        seen_ids: set[str] = set()
        by_run_workspace: dict[tuple[str, str], list[CodeIndexObservation]] = defaultdict(list)
        for item in observations:
            if item.observation_id in seen_ids:
                findings.append(
                    self._finding(
                        "duplicate_observation",
                        item,
                        "code-index observation identity is duplicated",
                    )
                )
            seen_ids.add(item.observation_id)
            by_run_workspace[(item.run_id, item.workspace_id)].append(item)
            findings.extend(self._observation_findings(item))
        findings.extend(self._sequence_findings(by_run_workspace))
        contexts = [
            item
            for item in observations
            if item.phase is IndexPhase.SELECT_CONTEXT
        ]
        tests = [
            item
            for item in observations
            if item.phase is IndexPhase.SELECT_TESTS
        ]
        context_changed = len(
            {
                item.selected_context
                for item in contexts
            }
        ) >= 2
        test_selection_changed = len(
            {
                item.selected_tests
                for item in tests
            }
        ) >= 2
        patch_refresh = any(
            item.phase is IndexPhase.PATCH_REFRESH
            and item.transaction_id
            and item.changed_paths
            and item.generation_advanced
            and item.causation_id
            for item in observations
        )
        phases = Counter(item.phase.value for item in observations)
        material = {
            "observations": [item.to_dict() for item in observations],
            "findings": [item.to_dict() for item in findings],
            "context_changed": context_changed,
            "test_selection_changed": test_selection_changed,
            "patch_refresh": patch_refresh,
        }
        return CodeIndexSecurityReport(
            observations=tuple(observations),
            findings=tuple(findings),
            phase_counts=dict(phases),
            generations=tuple(
                sorted({item.generation_after for item in observations})
            ),
            context_changed=context_changed,
            test_selection_changed=test_selection_changed,
            patch_refresh_observed=patch_refresh,
            digest=stable_digest(material),
        )

    def mutation_campaign(
        self,
        observations: Sequence[CodeIndexObservation],
    ) -> Mapping[str, CodeIndexSecurityReport]:
        baseline = self.audit(observations)
        query = next(item for item in observations if item.phase is IndexPhase.QUERY)
        rebuild = next(item for item in observations if item.phase is IndexPhase.REBUILD)
        refresh = next(
            item for item in observations if item.phase is IndexPhase.PATCH_REFRESH
        )
        selection = next(
            item for item in observations if item.phase is IndexPhase.SELECT_CONTEXT
        )
        over_budget = replace(
            query,
            observation_id=f"{query.observation_id}-budget",
            files_scanned=query.budget.maximum_files + 1,
            truncated=False,
            status="completed",
        )
        no_permission = replace(
            query,
            observation_id=f"{query.observation_id}-permission",
            permission_receipt_id="",
            permission_effect="allow",
        )
        denied_success = replace(
            query,
            observation_id=f"{query.observation_id}-denied",
            permission_effect="deny",
            status="completed",
            result_count=1,
        )
        stale_rebuild = replace(
            rebuild,
            observation_id=f"{rebuild.observation_id}-stale",
            source_revision=f"{rebuild.source_revision}-stale",
            status="completed",
        )
        no_refresh_cause = replace(
            refresh,
            observation_id=f"{refresh.observation_id}-cause",
            causation_id="",
        )
        fallback = replace(
            query,
            observation_id=f"{query.observation_id}-fallback",
            fallback_kind="mock_fallback",
        )
        no_selection_effect = tuple(
            replace(
                item,
                selected_context=selection.selected_context,
            )
            if item.phase is IndexPhase.SELECT_CONTEXT
            else item
            for item in observations
        )
        return {
            "baseline": baseline,
            "over_budget": self.audit((*observations, over_budget)),
            "no_permission": self.audit((*observations, no_permission)),
            "denied_success": self.audit((*observations, denied_success)),
            "stale_rebuild": self.audit((*observations, stale_rebuild)),
            "patch_refresh_without_cause": self.audit((*observations, no_refresh_cause)),
            "mock_fallback": self.audit((*observations, fallback)),
            "no_selection_effect": self.audit(no_selection_effect),
        }

    @staticmethod
    def _observation_findings(
        item: CodeIndexObservation,
    ) -> list[IndexFinding]:
        findings: list[IndexFinding] = []

        def add(code: str, reason: str) -> None:
            findings.append(CodeIndexSecurityAuditor._finding(code, item, reason))

        if not item.request_id or not item.run_id or not item.workspace_id:
            add("identity_missing", "request/run/workspace identity is required")
        if item.phase is not IndexPhase.REJECTED:
            if not item.permission_receipt_id:
                add("permission_receipt_missing", "index action lacks permission receipt")
            if item.permission_effect.casefold() != "allow":
                add("permission_not_allowed", "non-rejected index action lacks allow effect")
        if item.phase is IndexPhase.REJECTED:
            if not item.error_code:
                add("rejection_error_missing", "rejected index action lacks stable error code")
            if item.result_count or item.output_chars or item.generation_advanced:
                add("rejected_action_changed_state", "rejected index action produced output/state")
        if not item.budget_valid:
            add(
                "budget_exceeded",
                "index observation exceeds declared file/byte/result/output budget",
            )
        if (
            item.files_scanned == item.budget.maximum_files
            or item.bytes_scanned == item.budget.maximum_bytes
            or item.result_count == item.budget.maximum_results
            or item.output_chars == item.budget.maximum_output_chars
        ) and not item.truncated:
            add("budget_boundary_unmarked", "budget boundary reached without truncation marker")
        if item.fallback_kind in {
            "mock_fallback",
            "fixture_fallback",
            "legacy_fallback",
            "source_repository_fallback",
        }:
            add("fallback_masked", f"index failure used {item.fallback_kind}")
        if item.phase in {
            IndexPhase.BUILD,
            IndexPhase.REBUILD,
            IndexPhase.PATCH_REFRESH,
        }:
            if not item.content_digest:
                add("content_digest_missing", "index publication lacks content digest")
            if not item.generation_advanced:
                add("generation_not_advanced", "index publication did not advance generation")
        if item.phase is IndexPhase.INVALIDATE:
            if not item.changed_paths or not item.transaction_id:
                add("invalidation_unbound", "invalidation lacks changed paths/transaction")
        if item.phase is IndexPhase.PATCH_REFRESH:
            if not item.changed_paths or not item.transaction_id or not item.causation_id:
                add("patch_refresh_unbound", "patch refresh lacks patch causation")
        if item.phase is IndexPhase.SELECT_CONTEXT and not item.selected_context:
            add("context_selection_empty", "index had no effect on context selection")
        if item.phase is IndexPhase.SELECT_TESTS and not item.selected_tests:
            add("test_selection_empty", "index had no effect on test selection")
        return findings

    @staticmethod
    def _sequence_findings(
        groups: Mapping[tuple[str, str], Sequence[CodeIndexObservation]],
    ) -> list[IndexFinding]:
        findings: list[IndexFinding] = []
        for _, values in groups.items():
            published_revision = ""
            generation = 0
            for item in values:
                if item.generation_before < generation:
                    findings.append(
                        CodeIndexSecurityAuditor._finding(
                            "generation_regression",
                            item,
                            f"generation regressed from {generation} to {item.generation_before}",
                        )
                    )
                generation = max(generation, item.generation_after)
                if item.phase in {
                    IndexPhase.BUILD,
                    IndexPhase.REBUILD,
                    IndexPhase.PATCH_REFRESH,
                }:
                    if published_revision and item.source_revision == published_revision:
                        if not item.changed_paths:
                            findings.append(
                                CodeIndexSecurityAuditor._finding(
                                    "duplicate_publication",
                                    item,
                                    "generation advanced without revision/change input",
                                )
                            )
                    published_revision = item.source_revision
                elif published_revision and item.source_revision != published_revision:
                    findings.append(
                        CodeIndexSecurityAuditor._finding(
                            "stale_source_revision",
                            item,
                            "consumer observation uses a stale source revision",
                        )
                    )
        return findings

    @staticmethod
    def _finding(
        code: str,
        item: CodeIndexObservation,
        reason: str,
    ) -> IndexFinding:
        return IndexFinding(
            code=code,
            observation_id=item.observation_id,
            phase=item.phase,
            reason=reason,
        )


def code_index_observations_from_mappings(
    values: Iterable[Mapping[str, Any]],
) -> tuple[CodeIndexObservation, ...]:
    observations: list[CodeIndexObservation] = []
    for value in values:
        raw_budget = value.get("budget")
        budget = raw_budget if isinstance(raw_budget, Mapping) else {}
        observations.append(
            CodeIndexObservation(
                observation_id=str(value.get("observation_id") or ""),
                request_id=str(value.get("request_id") or ""),
                run_id=str(value.get("run_id") or ""),
                workspace_id=str(value.get("workspace_id") or ""),
                phase=IndexPhase(str(value.get("phase") or "")),
                source_revision=str(value.get("source_revision") or ""),
                generation_before=int(value.get("generation_before") or 0),
                generation_after=int(value.get("generation_after") or 0),
                permission_receipt_id=str(value.get("permission_receipt_id") or ""),
                permission_effect=str(value.get("permission_effect") or ""),
                budget=IndexBudget(
                    maximum_files=int(budget.get("maximum_files") or 0),
                    maximum_bytes=int(budget.get("maximum_bytes") or 0),
                    maximum_results=int(budget.get("maximum_results") or 0),
                    maximum_output_chars=int(budget.get("maximum_output_chars") or 0),
                ),
                files_scanned=int(value.get("files_scanned") or 0),
                bytes_scanned=int(value.get("bytes_scanned") or 0),
                result_count=int(value.get("result_count") or 0),
                output_chars=int(value.get("output_chars") or 0),
                changed_paths=tuple(str(item) for item in value.get("changed_paths") or ()),
                selected_context=tuple(str(item) for item in value.get("selected_context") or ()),
                selected_tests=tuple(str(item) for item in value.get("selected_tests") or ()),
                transaction_id=str(value.get("transaction_id") or ""),
                causation_id=str(value.get("causation_id") or ""),
                content_digest=str(value.get("content_digest") or ""),
                status=str(value.get("status") or ""),
                error_code=str(value.get("error_code") or ""),
                truncated=bool(value.get("truncated")),
                fallback_kind=str(value.get("fallback_kind") or ""),
                attributes=(
                    dict(value.get("attributes"))
                    if isinstance(value.get("attributes"), Mapping)
                    else {}
                ),
            )
        )
    return tuple(observations)


def evaluate_code_index_security(
    observations: Sequence[CodeIndexObservation],
) -> CaseExecutionBuffer:
    reports = CodeIndexSecurityAuditor().mutation_campaign(observations)
    buffer = CaseExecutionBuffer()
    baseline = reports["baseline"]
    base_observation = buffer.observe(
        "code-index-security.baseline",
        ObservationKind.INDEX,
        "budget-permission-incremental-selection",
        "verified" if baseline.valid else "invalid",
        attributes={
            "report_digest": baseline.digest,
            "phase_counts": baseline.phase_counts,
            "generations": list(baseline.generations),
            "context_changed": baseline.context_changed,
            "test_selection_changed": baseline.test_selection_changed,
        },
    )
    buffer.assert_that(
        "code-index-security.baseline-valid",
        baseline.valid,
        "code-index baseline must enforce budgets/permission and change selections",
        evidence=(base_observation.observation_id,),
    )
    for name, report in reports.items():
        if name == "baseline":
            continue
        observation = buffer.observe(
            f"code-index-security.{name}",
            ObservationKind.MUTATION,
            f"code-index-negative:{name}",
            "mutation-rejected" if not report.valid else "mutation-accepted",
            attributes={
                "report_digest": report.digest,
                "finding_codes": [item.code for item in report.findings],
            },
        )
        buffer.assert_that(
            f"code-index-security.reject-{name}",
            not report.valid,
            f"{name} code-index mutation must be rejected",
            severity=AssertionSeverity.BLOCKER,
            evidence=(observation.observation_id,),
            failure_kind=FailureKind.SECURITY,
        )
    return buffer


__all__ = [
    "CodeIndexObservation",
    "CodeIndexSecurityAuditor",
    "CodeIndexSecurityReport",
    "IndexBudget",
    "IndexFinding",
    "IndexPhase",
    "code_index_observations_from_mappings",
    "evaluate_code_index_security",
]
