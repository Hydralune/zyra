from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ledger_audit import LedgerAuditReport
from .ledger_linecount import EffectiveLineCountReport
from .ledger_models import to_jsonable


@dataclass(slots=True)
class LedgerHealthScore:
    score: int
    grade: str
    reasons: list[str] = field(default_factory=list)
    dimensions: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


def compute_health_score(
    *,
    audit: LedgerAuditReport,
    coverage: Any,
    debt: Any,
    readiness: Any,
    matrix: Any,
    line_count: EffectiveLineCountReport | None = None,
) -> LedgerHealthScore:
    dimensions: dict[str, int] = {
        "audit": _audit_score(audit),
        "coverage": _coverage_score(coverage),
        "debt": _debt_score(debt),
        "readiness": _readiness_score(readiness),
        "matrix": _matrix_score(matrix),
        "line_count": _line_count_score(line_count),
    }
    weights = {
        "audit": 25,
        "coverage": 15,
        "debt": 15,
        "readiness": 20,
        "matrix": 10,
        "line_count": 15,
    }
    weighted = sum(dimensions[name] * weight for name, weight in weights.items()) / sum(weights.values())
    score = max(0, min(100, round(weighted)))
    reasons = _health_reasons(audit, coverage, debt, readiness, matrix, line_count)
    return LedgerHealthScore(score=score, grade=_grade(score), reasons=reasons, dimensions=dimensions)


def health_payload(
    *,
    audit: LedgerAuditReport,
    coverage: Any,
    debt: Any,
    readiness: Any,
    matrix: Any,
    line_count: EffectiveLineCountReport | None = None,
) -> dict[str, Any]:
    score = compute_health_score(
        audit=audit,
        coverage=coverage,
        debt=debt,
        readiness=readiness,
        matrix=matrix,
        line_count=line_count,
    )
    return {
        "score": score.to_dict(),
        "audit": {
            "ok": audit.ok,
            "errors": audit.error_count,
            "blockers": audit.blocker_count,
            "warnings": audit.warning_count,
        },
        "coverage": {
            "total_entries": coverage.total_entries,
            "missing_required_repos": coverage.missing_required_repos,
            "effective_target_records": coverage.effective_target_records,
            "data_only_records": coverage.data_only_records,
        },
        "debt": {
            "total_debt_items": debt.total_debt_items,
            "blocking_debt_count": debt.blocking_debt_count,
        },
        "readiness": {
            "owner_unit": readiness.owner_unit,
            "ok": readiness.ok,
            "blocked_entries": readiness.blocked_entries,
            "ready_for_internalization": readiness.ready_for_internalization,
        },
        "matrix": {
            "unit_count": matrix.unit_count,
            "covered_units": matrix.covered_units,
            "uncovered_units": matrix.uncovered_units,
        },
        "line_count": {
            "ok": line_count.ok,
            "effective_added": line_count.effective_added,
            "minimum_effective_lines": line_count.minimum_effective_lines,
            "excluded_added": line_count.excluded_added,
        }
        if line_count
        else None,
    }


def _audit_score(audit: LedgerAuditReport) -> int:
    if audit.blocker_count:
        return 0
    if audit.error_count:
        return max(10, 70 - audit.error_count * 10)
    if audit.warning_count:
        return max(60, 95 - min(audit.warning_count, 50))
    return 100


def _coverage_score(coverage: Any) -> int:
    if coverage.missing_required_repos:
        return max(0, 70 - len(coverage.missing_required_repos) * 10)
    if coverage.total_entries <= 0:
        return 0
    effective_ratio = coverage.effective_target_records / max(coverage.total_entries, 1)
    return min(100, 70 + round(effective_ratio * 30))


def _debt_score(debt: Any) -> int:
    if debt.blocking_debt_count:
        return max(0, 70 - debt.blocking_debt_count * 5)
    if debt.total_debt_items:
        return max(50, 95 - min(debt.total_debt_items, 45))
    return 100


def _readiness_score(readiness: Any) -> int:
    if readiness.total_entries == 0:
        return 0
    if readiness.blocked_entries:
        return max(0, 75 - readiness.blocked_entries * 10)
    internalized_ratio = readiness.ready_for_internalization / max(readiness.total_entries, 1)
    return 70 + round(internalized_ratio * 30)


def _matrix_score(matrix: Any) -> int:
    if matrix.cyclic_or_unresolved_units:
        return 0
    if matrix.unit_count == 0:
        return 0
    coverage_ratio = matrix.covered_units / matrix.unit_count
    return round(coverage_ratio * 100)


def _line_count_score(line_count: EffectiveLineCountReport | None) -> int:
    if line_count is None:
        return 40
    if line_count.minimum_effective_lines <= 0:
        return 80
    ratio = line_count.effective_added / max(line_count.minimum_effective_lines, 1)
    if ratio >= 1:
        return 100
    return round(ratio * 80)


def _health_reasons(
    audit: LedgerAuditReport,
    coverage: Any,
    debt: Any,
    readiness: Any,
    matrix: Any,
    line_count: EffectiveLineCountReport | None,
) -> list[str]:
    reasons: list[str] = []
    if audit.blocker_count or audit.error_count:
        reasons.append("audit has blocking findings")
    if audit.warning_count:
        reasons.append("audit warnings remain and must be reviewed")
    if coverage.missing_required_repos:
        reasons.append("required source repository coverage is incomplete")
    if debt.blocking_debt_count:
        reasons.append("blocking remediation actions exist")
    if readiness.blocked_entries:
        reasons.append("unit readiness has blocked entries")
    if matrix.uncovered_units:
        reasons.append("some execution units have no ledger coverage")
    if line_count is not None and not line_count.ok:
        reasons.append("effective line-count gate has a shortfall")
    if not reasons:
        reasons.append("ledger health gate has no blocking issues")
    return reasons


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"
