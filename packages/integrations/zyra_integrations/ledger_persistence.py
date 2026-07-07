from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .ledger_audit import AuditFindingCode, AuditSeverity, InternalizationLedgerAuditor, LedgerAuditFinding
from .ledger_models import InternalizationLedgerEntry, now_iso, to_jsonable
from .ledger_policy import LedgerPolicySeverity, validate_entry_policy
from .ledger_store import InternalizationLedger, LedgerMutation, project_ledger_path


class PersistenceSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class PersistenceCode(StrEnum):
    VALID = "VALID"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    POLICY_INVALID = "POLICY_INVALID"
    AUDIT_SCOPE_INVALID = "AUDIT_SCOPE_INVALID"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    JOURNAL_APPEND_FAILED = "JOURNAL_APPEND_FAILED"
    ATOMIC_WRITE_FAILED = "ATOMIC_WRITE_FAILED"
    LEDGER_NOT_FOUND = "LEDGER_NOT_FOUND"
    BOOTSTRAP_DISABLED = "BOOTSTRAP_DISABLED"


@dataclass(slots=True)
class PersistenceFinding:
    code: PersistenceCode
    severity: PersistenceSeverity
    message: str
    ledger_id: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerRevision:
    revision: int
    content_hash: str
    previous_hash: str = ""
    updated_at: str = field(default_factory=now_iso)
    updated_by: str = "system"
    run_id: str = ""
    mutation_count: int = 0
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class PersistenceValidationResult:
    ok: bool
    ledger_id: str
    findings: list[PersistenceFinding] = field(default_factory=list)
    audit_findings: list[dict[str, Any]] = field(default_factory=list)
    policy_findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == PersistenceSeverity.ERROR)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == PersistenceSeverity.BLOCKER)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["error_count"] = self.error_count
        payload["blocker_count"] = self.blocker_count
        return payload


@dataclass(slots=True)
class PersistedMutation:
    ok: bool
    mutation: LedgerMutation | None = None
    revision: LedgerRevision | None = None
    validation: PersistenceValidationResult | None = None
    ledger_path: str = ""
    journal_path: str = ""
    event_payload: dict[str, Any] | None = None
    findings: list[PersistenceFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class AtomicLedgerStore:
    def __init__(
        self,
        project_root: Path,
        *,
        ledger_path: Path | None = None,
        journal_path: Path | None = None,
        bootstrap: bool = True,
    ) -> None:
        self.project_root = Path(project_root)
        self.ledger_path = ledger_path or project_ledger_path(self.project_root)
        self.journal_path = journal_path or self.project_root / "tmp" / "internalization_ledger_mutations.jsonl"
        self.bootstrap = bootstrap

    def load(self) -> InternalizationLedger:
        if self.ledger_path.exists():
            return InternalizationLedger.load(self.ledger_path)
        if not self.bootstrap:
            raise FileNotFoundError(str(self.ledger_path))
        from .ledger_store import load_seed_ledger

        ledger = load_seed_ledger()
        self.save_atomic(ledger, actor="bootstrap", run_id="bootstrap")
        return ledger

    def current_revision(self) -> LedgerRevision:
        if not self.ledger_path.exists():
            return LedgerRevision(revision=0, content_hash="", path=str(self.ledger_path))
        payload = _read_json(self.ledger_path)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        revision = int(metadata.get("revision") or 0)
        previous_hash = str(metadata.get("previous_hash") or "")
        content_hash = stable_content_hash(_without_volatile_metadata(payload))
        updated_at = str(metadata.get("updated_at") or "")
        updated_by = str(metadata.get("updated_by") or "")
        run_id = str(metadata.get("run_id") or "")
        mutation_count = int(metadata.get("mutation_count") or 0)
        return LedgerRevision(
            revision=revision,
            content_hash=content_hash,
            previous_hash=previous_hash,
            updated_at=updated_at,
            updated_by=updated_by,
            run_id=run_id,
            mutation_count=mutation_count,
            path=str(self.ledger_path),
        )

    def save_atomic(
        self,
        ledger: InternalizationLedger,
        *,
        actor: str,
        run_id: str = "",
        expected_revision: int | None = None,
        mutations: list[LedgerMutation] | None = None,
    ) -> LedgerRevision:
        before = self.current_revision()
        if expected_revision is not None and before.revision != expected_revision:
            raise ValueError(f"Ledger revision mismatch: expected {expected_revision}, got {before.revision}")
        mutation_count = before.mutation_count + len(mutations or [])
        payload = ledger.to_dict()
        payload["metadata"] = {
            "revision": before.revision + 1,
            "previous_hash": before.content_hash,
            "updated_at": now_iso(),
            "updated_by": actor,
            "run_id": run_id,
            "mutation_count": mutation_count,
            "journal_path": str(self.journal_path),
        }
        content_hash = stable_content_hash(_without_volatile_metadata(payload))
        payload["metadata"]["content_hash"] = content_hash
        self._atomic_write(payload)
        revision = LedgerRevision(
            revision=before.revision + 1,
            content_hash=content_hash,
            previous_hash=before.content_hash,
            updated_at=payload["metadata"]["updated_at"],
            updated_by=actor,
            run_id=run_id,
            mutation_count=mutation_count,
            path=str(self.ledger_path),
        )
        if mutations:
            self.append_journal(revision, mutations, actor=actor, run_id=run_id)
        return revision

    def upsert_validated(
        self,
        entry: InternalizationLedgerEntry,
        *,
        actor: str,
        run_id: str = "",
        expected_revision: int | None = None,
        strict: bool = True,
    ) -> PersistedMutation:
        ledger = self.load()
        validation = validate_entry_for_persistence(self.project_root, ledger, entry, strict=strict)
        if not validation.ok:
            return PersistedMutation(
                ok=False,
                validation=validation,
                ledger_path=str(self.ledger_path),
                journal_path=str(self.journal_path),
                findings=validation.findings,
            )
        mutation = ledger.upsert(entry)
        revision = self.save_atomic(
            ledger,
            actor=actor,
            run_id=run_id,
            expected_revision=expected_revision,
            mutations=[mutation],
        )
        return PersistedMutation(
            ok=True,
            mutation=mutation,
            revision=revision,
            validation=validation,
            ledger_path=str(self.ledger_path),
            journal_path=str(self.journal_path),
        )

    def append_journal(self, revision: LedgerRevision, mutations: list[LedgerMutation], *, actor: str, run_id: str = "") -> Path:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as file:
            for mutation in mutations:
                file.write(
                    json.dumps(
                        {
                            "revision": revision.to_dict(),
                            "actor": actor,
                            "run_id": run_id,
                            "created_at": now_iso(),
                            "mutation": mutation.to_dict() if hasattr(mutation, "to_dict") else to_jsonable(mutation),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                file.write("\n")
        return self.journal_path

    def read_journal(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        lines = self.journal_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        records: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.ledger_path.name}.", suffix=".tmp", dir=str(self.ledger_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_name, self.ledger_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def validate_entry_for_persistence(
    project_root: Path,
    ledger: InternalizationLedger,
    entry: InternalizationLedgerEntry,
    *,
    strict: bool = True,
) -> PersistenceValidationResult:
    findings: list[PersistenceFinding] = []
    schema_errors = entry.validate()
    for error in schema_errors:
        findings.append(
            PersistenceFinding(
                code=PersistenceCode.SCHEMA_INVALID,
                severity=PersistenceSeverity.ERROR,
                ledger_id=entry.ledger_id,
                message=error,
                remediation="Fix the entry schema before it can be persisted.",
            )
        )
    policy_findings = validate_entry_policy(entry)
    for finding in policy_findings:
        if finding.severity in {LedgerPolicySeverity.ERROR, LedgerPolicySeverity.BLOCKER}:
            findings.append(
                PersistenceFinding(
                    code=PersistenceCode.POLICY_INVALID,
                    severity=PersistenceSeverity.BLOCKER
                    if finding.severity == LedgerPolicySeverity.BLOCKER
                    else PersistenceSeverity.ERROR,
                    ledger_id=entry.ledger_id,
                    message=f"{finding.code}: {finding.message}",
                    remediation=finding.remediation,
                    metadata=finding.to_dict(),
                )
            )
    if strict:
        scoped = InternalizationLedger(ledger.entries())
        scoped.upsert(entry)
        audit = InternalizationLedgerAuditor(project_root, strict=True).audit(scoped)
        for finding in audit.findings:
            if finding.ledger_id != entry.ledger_id:
                continue
            if finding.severity in {AuditSeverity.ERROR, AuditSeverity.BLOCKER}:
                findings.append(
                    PersistenceFinding(
                        code=PersistenceCode.AUDIT_SCOPE_INVALID,
                        severity=PersistenceSeverity.BLOCKER
                        if finding.severity == AuditSeverity.BLOCKER
                        else PersistenceSeverity.ERROR,
                        ledger_id=entry.ledger_id,
                        message=f"{finding.code}: {finding.message}",
                        remediation=finding.remediation,
                        metadata=finding.to_dict(),
                    )
                )
    ok = not any(finding.severity in {PersistenceSeverity.ERROR, PersistenceSeverity.BLOCKER} for finding in findings)
    return PersistenceValidationResult(
        ok=ok,
        ledger_id=entry.ledger_id,
        findings=findings,
        audit_findings=[
            finding.metadata
            for finding in findings
            if finding.code == PersistenceCode.AUDIT_SCOPE_INVALID
        ],
        policy_findings=[
            finding.metadata
            for finding in findings
            if finding.code == PersistenceCode.POLICY_INVALID
        ],
    )


def stable_content_hash(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _without_volatile_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    clone = json.loads(json.dumps(payload, ensure_ascii=False))
    metadata = clone.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("content_hash", None)
        metadata.pop("updated_at", None)
    return clone


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def mutation_to_dict(mutation: LedgerMutation) -> dict[str, Any]:
    return {
        "action": mutation.action,
        "ledger_id": mutation.ledger_id,
        "before": mutation.before,
        "after": mutation.after,
    }


def journal_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_actor: dict[str, int] = {}
    by_action: dict[str, int] = {}
    latest_revision = 0
    for record in records:
        actor = str(record.get("actor") or "unknown")
        by_actor[actor] = by_actor.get(actor, 0) + 1
        mutation = record.get("mutation") if isinstance(record.get("mutation"), dict) else {}
        action = str(mutation.get("action") or "unknown")
        by_action[action] = by_action.get(action, 0) + 1
        revision = record.get("revision") if isinstance(record.get("revision"), dict) else {}
        try:
            latest_revision = max(latest_revision, int(revision.get("revision") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "record_count": len(records),
        "latest_revision": latest_revision,
        "by_actor": dict(sorted(by_actor.items())),
        "by_action": dict(sorted(by_action.items())),
    }


def persistence_payload(store: AtomicLedgerStore) -> dict[str, Any]:
    revision = store.current_revision()
    journal = store.read_journal(limit=200)
    return {
        "ledger_path": str(store.ledger_path),
        "journal_path": str(store.journal_path),
        "revision": revision.to_dict(),
        "journal": journal,
        "journal_summary": journal_summary(journal),
    }
