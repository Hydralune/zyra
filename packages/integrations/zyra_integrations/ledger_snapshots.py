from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger_audit import InternalizationLedgerAuditor, LedgerAuditFinding, LedgerAuditReport
from .ledger_linecount import EffectiveLineCountReport, build_line_count_report
from .ledger_models import now_iso, to_jsonable
from .ledger_reports import build_coverage_report, build_debt_report, build_unit_readiness_report
from .ledger_store import InternalizationLedger


SNAPSHOT_SCHEMA_VERSION = "1.0"
DEFAULT_SNAPSHOT_DIR = "tmp/internalization-ledger-snapshots"


@dataclass(slots=True)
class LedgerSnapshotIdentity:
    snapshot_id: str
    created_at: str
    label: str
    git_commit: str
    git_dirty: bool
    ledger_hash: str
    project_root: str
    query_scope: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerSnapshot:
    identity: LedgerSnapshotIdentity
    summary: dict[str, Any]
    audit: dict[str, Any]
    coverage: dict[str, Any]
    debt: dict[str, Any]
    readiness: dict[str, Any]
    line_count: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "kind": "zyra.internalization_ledger_snapshot",
            "identity": self.identity.to_dict(),
            "summary": self.summary,
            "audit": self.audit,
            "coverage": self.coverage,
            "debt": self.debt,
            "readiness": self.readiness,
            "line_count": self.line_count,
            "entries": self.entries,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerSnapshot":
        identity_data = dict(data.get("identity") or {})
        identity = LedgerSnapshotIdentity(
            snapshot_id=str(identity_data.get("snapshot_id") or ""),
            created_at=str(identity_data.get("created_at") or ""),
            label=str(identity_data.get("label") or ""),
            git_commit=str(identity_data.get("git_commit") or ""),
            git_dirty=bool(identity_data.get("git_dirty", False)),
            ledger_hash=str(identity_data.get("ledger_hash") or ""),
            project_root=str(identity_data.get("project_root") or ""),
            query_scope=dict(identity_data.get("query_scope") or {}),
        )
        return cls(
            identity=identity,
            summary=dict(data.get("summary") or {}),
            audit=dict(data.get("audit") or {}),
            coverage=dict(data.get("coverage") or {}),
            debt=dict(data.get("debt") or {}),
            readiness=dict(data.get("readiness") or {}),
            line_count=dict(data.get("line_count")) if isinstance(data.get("line_count"), dict) else None,
            entries=[dict(item) for item in data.get("entries", []) if isinstance(item, dict)],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class LedgerSnapshotDiff:
    base_snapshot_id: str
    head_snapshot_id: str
    added_entries: list[str] = field(default_factory=list)
    removed_entries: list[str] = field(default_factory=list)
    changed_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit_delta: dict[str, int] = field(default_factory=dict)
    line_count_delta: dict[str, int] = field(default_factory=dict)
    readiness_delta: dict[str, int] = field(default_factory=dict)
    summary_delta: dict[str, Any] = field(default_factory=dict)

    @property
    def changed_count(self) -> int:
        return len(self.added_entries) + len(self.removed_entries) + len(self.changed_entries)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["changed_count"] = self.changed_count
        return payload


def build_snapshot(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    label: str = "",
    owner_unit: str = "",
    base_commit: str = "",
    include_entries: bool = True,
    metadata: dict[str, Any] | None = None,
) -> LedgerSnapshot:
    audit = InternalizationLedgerAuditor(project_root, strict=True).audit(ledger)
    line_count = (
        build_line_count_report(
            project_root,
            base=base_commit,
            minimum_effective_lines=0,
        )
        if base_commit
        else None
    )
    coverage = build_coverage_report(ledger)
    debt = build_debt_report(audit, owner_unit=owner_unit)
    readiness = build_unit_readiness_report(
        project_root,
        ledger,
        owner_unit=owner_unit,
        audit_report=audit,
        line_count_report=line_count,
    )
    entries = [entry.to_dict() for entry in _entries_for_scope(ledger, owner_unit)] if include_entries else []
    identity = LedgerSnapshotIdentity(
        snapshot_id=_snapshot_id(label=label, ledger_hash=ledger_hash(ledger), owner_unit=owner_unit),
        created_at=now_iso(),
        label=label or owner_unit or "ledger-snapshot",
        git_commit=_git_commit(project_root),
        git_dirty=_git_dirty(project_root),
        ledger_hash=ledger_hash(ledger),
        project_root=str(project_root),
        query_scope={"owner_unit": owner_unit},
    )
    return LedgerSnapshot(
        identity=identity,
        summary=ledger.summary().to_dict(),
        audit=audit.to_dict(),
        coverage=coverage.to_dict(),
        debt=debt.to_dict(),
        readiness=readiness.to_dict(),
        line_count=line_count.to_dict() if line_count else None,
        entries=entries,
        metadata=metadata or {},
    )


def save_snapshot(snapshot: LedgerSnapshot, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def load_snapshot(path: str | Path) -> LedgerSnapshot:
    return LedgerSnapshot.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def snapshot_dir(project_root: Path) -> Path:
    return project_root / DEFAULT_SNAPSHOT_DIR


def save_snapshot_to_default_dir(project_root: Path, snapshot: LedgerSnapshot) -> Path:
    safe_id = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in snapshot.identity.snapshot_id)
    return save_snapshot(snapshot, snapshot_dir(project_root) / f"{safe_id}.json")


def list_snapshots(project_root: Path) -> list[dict[str, Any]]:
    directory = snapshot_dir(project_root)
    if not directory.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            snapshot = load_snapshot(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        payloads.append(
            {
                "path": str(path),
                "snapshot_id": snapshot.identity.snapshot_id,
                "label": snapshot.identity.label,
                "created_at": snapshot.identity.created_at,
                "git_commit": snapshot.identity.git_commit,
                "ledger_hash": snapshot.identity.ledger_hash,
                "audit": {
                    "ok": snapshot.audit.get("ok"),
                    "error_count": snapshot.audit.get("error_count"),
                    "blocker_count": snapshot.audit.get("blocker_count"),
                    "warning_count": snapshot.audit.get("warning_count"),
                },
                "readiness": {
                    "owner_unit": snapshot.readiness.get("owner_unit"),
                    "ok": snapshot.readiness.get("ok"),
                    "effective_added": snapshot.readiness.get("effective_added"),
                    "minimum_effective_lines": snapshot.readiness.get("minimum_effective_lines"),
                },
            }
        )
    return payloads


def diff_snapshots(base: LedgerSnapshot, head: LedgerSnapshot) -> LedgerSnapshotDiff:
    base_entries = {entry["ledger_id"]: entry for entry in base.entries if "ledger_id" in entry}
    head_entries = {entry["ledger_id"]: entry for entry in head.entries if "ledger_id" in entry}
    added = sorted(set(head_entries) - set(base_entries))
    removed = sorted(set(base_entries) - set(head_entries))
    changed: dict[str, dict[str, Any]] = {}
    for ledger_id in sorted(set(base_entries) & set(head_entries)):
        before = base_entries[ledger_id]
        after = head_entries[ledger_id]
        entry_delta = _entry_delta(before, after)
        if entry_delta:
            changed[ledger_id] = entry_delta
    return LedgerSnapshotDiff(
        base_snapshot_id=base.identity.snapshot_id,
        head_snapshot_id=head.identity.snapshot_id,
        added_entries=added,
        removed_entries=removed,
        changed_entries=changed,
        audit_delta=_numeric_delta(base.audit, head.audit, ["finding_count", "error_count", "blocker_count", "warning_count"]),
        line_count_delta=_numeric_delta(base.line_count or {}, head.line_count or {}, ["raw_added", "excluded_added", "effective_added", "review_added"]),
        readiness_delta=_numeric_delta(
            base.readiness,
            head.readiness,
            [
                "total_entries",
                "ready_for_advance",
                "ready_for_internalization",
                "ready_for_productization",
                "blocked_entries",
                "effective_added",
                "minimum_effective_lines",
            ],
        ),
        summary_delta=_summary_delta(base.summary, head.summary),
    )


def ledger_hash(ledger: InternalizationLedger) -> str:
    payload = json.dumps(ledger.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def audit_hash(report: LedgerAuditReport) -> str:
    payload = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def finding_fingerprint(finding: LedgerAuditFinding) -> str:
    payload = {
        "code": str(finding.code),
        "severity": str(finding.severity),
        "ledger_id": finding.ledger_id,
        "source_repo": finding.source_repo,
        "source_path": finding.source_path,
        "target_path": finding.target_path,
        "message": finding.message,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def compare_audit_findings(base: LedgerAuditReport, head: LedgerAuditReport) -> dict[str, Any]:
    base_map = {finding_fingerprint(finding): finding.to_dict() for finding in base.findings}
    head_map = {finding_fingerprint(finding): finding.to_dict() for finding in head.findings}
    return {
        "added": [head_map[key] for key in sorted(set(head_map) - set(base_map))],
        "removed": [base_map[key] for key in sorted(set(base_map) - set(head_map))],
        "unchanged_count": len(set(base_map) & set(head_map)),
    }


def _entries_for_scope(ledger: InternalizationLedger, owner_unit: str) -> list[Any]:
    return ledger.by_owner_unit(owner_unit) if owner_unit else ledger.entries()


def _snapshot_id(*, label: str, ledger_hash: str, owner_unit: str) -> str:
    basis = f"{label}:{owner_unit}:{ledger_hash}:{now_iso()}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
    prefix = label or owner_unit or "ledger"
    safe_prefix = "".join(char.lower() if char.isalnum() else "-" for char in prefix).strip("-") or "ledger"
    return f"{safe_prefix}-{digest}"


def _git_commit(project_root: Path) -> str:
    try:
        completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, text=True, capture_output=True, check=True)
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _git_dirty(project_root: Path) -> bool:
    try:
        completed = subprocess.run(["git", "status", "--short"], cwd=project_root, text=True, capture_output=True, check=True)
        return bool(completed.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return False


def _entry_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    keys = sorted(set(before) | set(after))
    for key in keys:
        if before.get(key) != after.get(key):
            delta[key] = {"before": before.get(key), "after": after.get(key)}
    return delta


def _numeric_delta(before: dict[str, Any], after: dict[str, Any], keys: list[str]) -> dict[str, int]:
    delta: dict[str, int] = {}
    for key in keys:
        before_value = _as_int(before.get(key))
        after_value = _as_int(after.get(key))
        if before_value != after_value:
            delta[key] = after_value - before_value
    return delta


def _summary_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        before_value = before.get(key)
        after_value = after.get(key)
        if isinstance(before_value, int) or isinstance(after_value, int):
            if _as_int(before_value) != _as_int(after_value):
                result[key] = {"before": before_value, "after": after_value, "delta": _as_int(after_value) - _as_int(before_value)}
        elif isinstance(before_value, dict) or isinstance(after_value, dict):
            nested = _dict_count_delta(dict(before_value or {}), dict(after_value or {}))
            if nested:
                result[key] = nested
        elif before_value != after_value:
            result[key] = {"before": before_value, "after": after_value}
    return result


def _dict_count_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        before_value = _as_int(before.get(key))
        after_value = _as_int(after.get(key))
        if before_value != after_value:
            result[key] = {"before": before.get(key), "after": after.get(key), "delta": after_value - before_value}
    return result


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
