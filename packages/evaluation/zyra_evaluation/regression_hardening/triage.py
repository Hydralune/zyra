from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AssertionSeverity,
    CaseReceipt,
    CaseStatus,
    FailureKind,
    SuiteReceipt,
    bounded_text,
    stable_digest,
)


_PATH = re.compile(
    r"(?:(?:[A-Za-z]:)?[\\/](?:[^ \t\r\n:]+[\\/])*[^ \t\r\n:]+)"
)
_HEX = re.compile(r"\b[0-9a-f]{12,64}\b", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FailureSignature:
    signature: str
    failure_kind: FailureKind
    primary_code: str
    normalized_message: str
    case_ids: tuple[str, ...]
    count: int
    flaky: bool
    blocking: bool
    first_receipt_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "failure_kind": self.failure_kind.value,
            "primary_code": self.primary_code,
            "normalized_message": self.normalized_message,
            "case_ids": list(self.case_ids),
            "count": self.count,
            "flaky": self.flaky,
            "blocking": self.blocking,
            "first_receipt_digest": self.first_receipt_digest,
        }


@dataclass(frozen=True, slots=True)
class CaseTriage:
    case_id: str
    status: CaseStatus
    failure_kind: FailureKind | None
    failed_assertions: tuple[str, ...]
    blocker_assertions: tuple[str, ...]
    attempts: int
    flaky: bool
    duration_seconds: float
    signature: str
    remediation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "failure_kind": self.failure_kind.value if self.failure_kind else "",
            "failed_assertions": list(self.failed_assertions),
            "blocker_assertions": list(self.blocker_assertions),
            "attempts": self.attempts,
            "flaky": self.flaky,
            "duration_seconds": round(self.duration_seconds, 6),
            "signature": self.signature,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class TriageReport:
    suite_digest: str
    cases: tuple[CaseTriage, ...]
    signatures: tuple[FailureSignature, ...]
    status_counts: Mapping[str, int]
    failure_kind_counts: Mapping[str, int]
    blocking_cases: tuple[str, ...]
    flaky_cases: tuple[str, ...]
    slow_cases: tuple[str, ...]
    digest: str

    @property
    def valid(self) -> bool:
        return not self.blocking_cases and not any(
            item.failure_kind is FailureKind.INTERNAL
            for item in self.cases
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-regression-triage/v1",
            "valid": self.valid,
            "suite_digest": self.suite_digest,
            "cases": [item.to_dict() for item in self.cases],
            "signatures": [item.to_dict() for item in self.signatures],
            "status_counts": dict(sorted(self.status_counts.items())),
            "failure_kind_counts": dict(sorted(self.failure_kind_counts.items())),
            "blocking_cases": list(self.blocking_cases),
            "flaky_cases": list(self.flaky_cases),
            "slow_cases": list(self.slow_cases),
            "digest": self.digest,
        }


class FailureTriage:
    def __init__(
        self,
        *,
        slow_case_seconds: float = 30.0,
        message_limit: int = 500,
    ) -> None:
        self.slow_case_seconds = float(slow_case_seconds)
        self.message_limit = int(message_limit)

    def evaluate(self, suite: SuiteReceipt) -> TriageReport:
        cases = tuple(self._case(item) for item in suite.cases)
        grouped: dict[str, list[tuple[CaseReceipt, CaseTriage]]] = defaultdict(list)
        for receipt, triage in zip(suite.cases, cases, strict=True):
            if triage.signature:
                grouped[triage.signature].append((receipt, triage))
        signatures = tuple(
            self._signature(signature, values)
            for signature, values in sorted(grouped.items())
        )
        status_counts = Counter(item.status.value for item in cases)
        failure_counts = Counter(
            item.failure_kind.value
            for item in cases
            if item.failure_kind is not None
        )
        blocking = tuple(
            item.case_id
            for item in cases
            if item.blocker_assertions
            or item.status in {
                CaseStatus.BLOCKED,
                CaseStatus.TIMED_OUT,
                CaseStatus.CANCELLED,
            }
            or item.failure_kind in {
                FailureKind.SECURITY,
                FailureKind.POLLUTION,
                FailureKind.CONTRACT,
                FailureKind.INTERNAL,
            }
        )
        flaky = tuple(item.case_id for item in cases if item.flaky)
        slow = tuple(
            item.case_id
            for item in cases
            if item.duration_seconds >= self.slow_case_seconds
        )
        material = {
            "suite_digest": suite.digest,
            "cases": [item.to_dict() for item in cases],
            "signatures": [item.to_dict() for item in signatures],
            "status_counts": status_counts,
            "failure_kind_counts": failure_counts,
            "blocking_cases": blocking,
            "flaky_cases": flaky,
            "slow_cases": slow,
        }
        return TriageReport(
            suite_digest=suite.digest,
            cases=cases,
            signatures=signatures,
            status_counts=dict(status_counts),
            failure_kind_counts=dict(failure_counts),
            blocking_cases=blocking,
            flaky_cases=flaky,
            slow_cases=slow,
            digest=stable_digest(material),
        )

    def compare(
        self,
        baseline: TriageReport,
        candidate: TriageReport,
    ) -> dict[str, Any]:
        baseline_by_case = {item.case_id: item for item in baseline.cases}
        candidate_by_case = {item.case_id: item for item in candidate.cases}
        new_failures: list[str] = []
        resolved: list[str] = []
        status_changes: dict[str, dict[str, str]] = {}
        for case_id in sorted(set(baseline_by_case) | set(candidate_by_case)):
            before = baseline_by_case.get(case_id)
            after = candidate_by_case.get(case_id)
            if before is None:
                if after and after.status is not CaseStatus.PASSED:
                    new_failures.append(case_id)
                continue
            if after is None:
                new_failures.append(f"missing:{case_id}")
                continue
            if before.status != after.status:
                status_changes[case_id] = {
                    "before": before.status.value,
                    "after": after.status.value,
                }
            if before.status is CaseStatus.PASSED and after.status is not CaseStatus.PASSED:
                new_failures.append(case_id)
            if before.status is not CaseStatus.PASSED and after.status is CaseStatus.PASSED:
                resolved.append(case_id)
        baseline_signatures = {item.signature for item in baseline.signatures}
        candidate_signatures = {item.signature for item in candidate.signatures}
        payload = {
            "schema": "zyra.m3-regression-triage-comparison/v1",
            "baseline_digest": baseline.digest,
            "candidate_digest": candidate.digest,
            "new_failures": new_failures,
            "resolved_failures": resolved,
            "new_signatures": sorted(candidate_signatures - baseline_signatures),
            "resolved_signatures": sorted(baseline_signatures - candidate_signatures),
            "status_changes": status_changes,
            "accepted": not new_failures,
        }
        payload["digest"] = stable_digest(payload)
        return payload

    def _case(self, receipt: CaseReceipt) -> CaseTriage:
        final = receipt.final_attempt
        failed = tuple(
            item.assertion_id
            for item in final.assertions
            if not item.passed
        )
        blockers = tuple(
            item.assertion_id
            for item in final.assertions
            if not item.passed and item.severity is AssertionSeverity.BLOCKER
        )
        flaky = len(receipt.attempts) > 1 and receipt.passed
        primary_code = failed[0] if failed else final.status.value
        message = final.failure_message or next(
            (
                item.summary
                for item in final.assertions
                if not item.passed
            ),
            "",
        )
        normalized = self.normalize_message(message)
        signature = ""
        if not receipt.passed:
            signature = stable_digest(
                final.failure_kind.value if final.failure_kind else "unknown",
                primary_code,
                normalized,
            )
        return CaseTriage(
            case_id=receipt.spec.case_id,
            status=receipt.status,
            failure_kind=final.failure_kind,
            failed_assertions=failed,
            blocker_assertions=blockers,
            attempts=len(receipt.attempts),
            flaky=flaky,
            duration_seconds=sum(item.duration_seconds for item in receipt.attempts),
            signature=signature,
            remediation=self.remediation(
                final.failure_kind,
                primary_code,
                receipt.status,
            ),
        )

    def _signature(
        self,
        signature: str,
        values: Sequence[tuple[CaseReceipt, CaseTriage]],
    ) -> FailureSignature:
        first_receipt, first = values[0]
        final = first_receipt.final_attempt
        primary_code = (
            first.failed_assertions[0]
            if first.failed_assertions
            else first.status.value
        )
        message = final.failure_message or next(
            (
                item.summary
                for item in final.assertions
                if not item.passed
            ),
            "",
        )
        return FailureSignature(
            signature=signature,
            failure_kind=first.failure_kind or FailureKind.INTERNAL,
            primary_code=primary_code,
            normalized_message=self.normalize_message(message),
            case_ids=tuple(sorted(item.case_id for _, item in values)),
            count=len(values),
            flaky=any(item.flaky for _, item in values),
            blocking=any(bool(item.blocker_assertions) for _, item in values),
            first_receipt_digest=first_receipt.digest,
        )

    def normalize_message(self, value: str) -> str:
        message = bounded_text(value, maximum=self.message_limit).casefold()
        message = _PATH.sub("<path>", message)
        message = _HEX.sub("<hex>", message)
        message = _NUMBER.sub("<number>", message)
        message = _WHITESPACE.sub(" ", message).strip()
        return message

    @staticmethod
    def remediation(
        failure_kind: FailureKind | None,
        code: str,
        status: CaseStatus,
    ) -> str:
        if status is CaseStatus.BLOCKED:
            return "Restore the declared dependency/capability; do not enable a fallback owner."
        if status is CaseStatus.TIMED_OUT:
            return "Inspect the bounded process/case trace, terminate leaked work, and remove the hang."
        if status is CaseStatus.CANCELLED:
            return "Resolve the upstream blocker and rerun the cancelled dependency layer."
        if failure_kind is FailureKind.SECURITY:
            return "Treat as release-blocking; preserve the negative input and fix the enforcing owner."
        if failure_kind is FailureKind.POLLUTION:
            return "Confine writes to declared state roots and repeat from a fresh directory."
        if failure_kind is FailureKind.CONTRACT:
            return "Repair the evaluator/subject receipt contract before accepting behavior evidence."
        if failure_kind is FailureKind.PROCESS:
            return "Inspect exit code and bounded output; verify executable, cleanup and environment custody."
        if failure_kind is FailureKind.TIMEOUT:
            return "Add a deterministic terminal outcome and verify timeout cleanup."
        if failure_kind is FailureKind.INTERNAL:
            return "Fix the evaluation runtime; internal failures cannot be waived as product behavior."
        return f"Repair the failed assertion {code} and rerun its generated-input case."


__all__ = [
    "CaseTriage",
    "FailureSignature",
    "FailureTriage",
    "TriageReport",
]
