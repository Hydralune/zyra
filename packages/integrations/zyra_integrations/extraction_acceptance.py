from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .extraction_rules import ExtractionRuleAudit, audit_extraction_plan, build_rule_coverage_report
from .ledger_accounting import LedgerAccountingReport, build_accounting_report
from .ledger_audit import InternalizationLedgerAuditor
from .ledger_line_buckets import LineBucketReport, build_line_bucket_report
from .ledger_models import to_jsonable
from .ledger_store import InternalizationLedger, load_project_ledger
from .source_extraction import claude_code_m1_01b_plan


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    name: str
    ok: bool
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class M101BAcceptanceReport:
    project_root: Path
    source_workspace_root: Path
    rule_audit: ExtractionRuleAudit
    rule_coverage_payload: dict[str, Any]
    accounting: LedgerAccountingReport
    line_buckets: LineBucketReport | None
    runtime_payload: dict[str, Any]
    worker_payload: dict[str, Any]
    lineage_payload: dict[str, Any]
    controller_payload: dict[str, Any]
    checks: list[AcceptanceCheck]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_root": str(self.project_root),
            "source_workspace_root": str(self.source_workspace_root),
            "check_count": len(self.checks),
            "failed_checks": [check.name for check in self.checks if not check.ok],
            "rule_audit_ok": self.rule_audit.ok,
            "rule_coverage_ok": bool(self.rule_coverage_payload.get("ok")),
            "rule_included_files": self.rule_audit.summary()["included_files"],
            "accounting_ok": self.accounting.ok,
            "accounting_entries": self.accounting.total_entries,
            "runtime_ok": bool(self.runtime_payload.get("ok")),
            "worker_ok": bool(self.worker_payload.get("ok")),
            "lineage_ok": bool(self.lineage_payload.get("ok")),
            "controller_ok": bool(self.controller_payload.get("ok")),
            "bucket_effective_added": self.line_buckets.bucket_effective_added if self.line_buckets else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "checks": [check.to_dict() for check in self.checks],
            "rule_audit": self.rule_audit.to_dict(),
            "rule_coverage": self.rule_coverage_payload,
            "accounting": self.accounting.to_dict(),
            "line_buckets": self.line_buckets.to_dict() if self.line_buckets else None,
            "runtime": self.runtime_payload,
            "workers": self.worker_payload,
            "lineage": self.lineage_payload,
            "controller": self.controller_payload,
        }


def build_m1_01b_acceptance_report(
    project_root: str | Path,
    *,
    source_workspace_root: str | Path | None = None,
    base_commit: str = "",
    minimum_effective_lines: int = 10000,
    ledger: InternalizationLedger | None = None,
) -> M101BAcceptanceReport:
    root = Path(project_root).resolve()
    source_root = Path(
        source_workspace_root or root / "provenance"
    ).resolve()
    plan = claude_code_m1_01b_plan(project_root=root, source_workspace_root=source_root, dry_run=True)
    rule_audit = audit_extraction_plan(plan)
    rule_coverage = build_rule_coverage_report(rule_audit)
    active_ledger = ledger or load_project_ledger(root, bootstrap=True)
    accounting = build_accounting_report(root, active_ledger, owner_unit="M1-01B", include_entries=True)
    line_buckets = (
        build_line_bucket_report(root, base=base_commit, minimum_effective_lines=minimum_effective_lines)
        if base_commit
        else None
    )
    runtime_payload = _runtime_payload(root)
    worker_payload = _worker_payload(root)
    lineage_payload = _lineage_payload(root, source_root)
    controller_payload = _controller_payload(root, source_root)
    audit_report = InternalizationLedgerAuditor(root, strict=True).audit(active_ledger)
    checks = [
        AcceptanceCheck(
            "rule-audit",
            rule_audit.ok,
            "extraction rule audit has no blockers",
            rule_audit.summary(),
        ),
        AcceptanceCheck(
            "pilot-is-narrow",
            20 <= rule_audit.summary()["included_files"] <= 80,
            "M1-01B pilot stays narrow and does not claim full productization",
            {"included_files": rule_audit.summary()["included_files"]},
        ),
        AcceptanceCheck(
            "rule-coverage",
            rule_coverage.ok,
            "every extraction include capability materializes source without blocking findings",
            rule_coverage.summary,
        ),
        AcceptanceCheck(
            "ledger-accounting",
            accounting.ok and accounting.total_entries > 0,
            "M1-01B ledger accounting contains wired entries",
            accounting.summary(),
        ),
        AcceptanceCheck(
            "ledger-audit",
            audit_report.ok,
            "strict ledger audit passes",
            {"error_count": audit_report.error_count, "blocker_count": audit_report.blocker_count, "warning_count": audit_report.warning_count},
        ),
        AcceptanceCheck(
            "runtime-lifecycle",
            bool(runtime_payload.get("ok")),
            "runtime lifecycle surfaces are connected and disconnect probes fail as expected",
            {
                "event_count": runtime_payload.get("event_count"),
                "probe_count": len(runtime_payload.get("disconnect_probes", [])),
                "fault_matrix_ok": bool((runtime_payload.get("fault_matrix") or {}).get("ok")),
            },
        ),
        AcceptanceCheck(
            "worker-bridge",
            bool(worker_payload.get("ok")),
            "five worker bridge probes execute real behavior",
            {"worker_count": worker_payload.get("worker_count")},
        ),
        AcceptanceCheck(
            "source-lineage",
            bool(lineage_payload.get("ok")),
            "source-to-target lineage links extraction evidence to effective Zyra modules",
            lineage_payload.get("summary", {}),
        ),
        AcceptanceCheck(
            "runtime-controller",
            bool(controller_payload.get("ok")),
            "runtime extraction controller composes rules, ledger, lifecycle, worker contracts, and clean-boundary checks",
            controller_payload.get("summary", {}),
        ),
        AcceptanceCheck(
            "line-buckets",
            True if line_buckets is None else line_buckets.ok,
            "effective line bucket gate passes when a base commit is provided",
            {} if line_buckets is None else line_buckets.to_dict(),
        ),
    ]
    return M101BAcceptanceReport(
        project_root=root,
        source_workspace_root=source_root,
        rule_audit=rule_audit,
        rule_coverage_payload=rule_coverage.to_dict(),
        accounting=accounting,
        line_buckets=line_buckets,
        runtime_payload=runtime_payload,
        worker_payload=worker_payload,
        lineage_payload=lineage_payload,
        controller_payload=controller_payload,
        checks=checks,
    )


def m1_01b_acceptance_payload(report: M101BAcceptanceReport) -> dict[str, Any]:
    return report.to_dict()


def assert_m1_01b_acceptance(report: M101BAcceptanceReport) -> None:
    if report.ok:
        return
    failed = "\n".join(f"- {check.name}: {check.message}" for check in report.checks if not check.ok)
    raise AssertionError(f"M1-01B acceptance failed:\n{failed}")


def _runtime_payload(project_root: Path) -> dict[str, Any]:
    from zyra_runtime.scaffold_lifecycle import scaffold_lifecycle_payload

    return scaffold_lifecycle_payload(project_root)


def _worker_payload(project_root: Path) -> dict[str, Any]:
    from zyra_workers.scaffold_supervisor import run_supervised_scaffold_workers

    report = run_supervised_scaffold_workers(project_root, max_attempts=1)
    payload = report.to_dict()
    payload["worker_count"] = report.summary.get("worker_count", 0)
    return payload


def _lineage_payload(project_root: Path, source_workspace_root: Path) -> dict[str, Any]:
    from zyra_integrations.extraction_lineage import build_m1_01b_lineage_report

    return build_m1_01b_lineage_report(project_root, source_workspace_root=source_workspace_root).to_dict()


def _controller_payload(project_root: Path, source_workspace_root: Path) -> dict[str, Any]:
    from zyra_runtime.extraction_runtime import run_extraction_runtime_controller

    return run_extraction_runtime_controller(
        project_root,
        source_workspace_root=source_workspace_root,
        include_worker_probes=False,
    ).to_dict()
