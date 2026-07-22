from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .contracts import HardeningReport
from .reporting import HardeningMarkdownRenderer, ReportDisposition, ReportNormalizer


class HardeningStoreError(RuntimeError):
    pass


class ReportNotFound(HardeningStoreError):
    pass


class ReportIntegrityError(HardeningStoreError):
    pass


class ConcurrentAuditError(HardeningStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ReportRecord:
    report_id: str
    generated_at: str
    scenario_id: str
    task_id: str
    run_id: str
    baseline_commit: str
    accepted: bool
    blockers: int
    failures: int
    json_path: str
    markdown_path: str
    content_digest: str
    previous_digest: str
    sequence: int
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "scenario_id": self.scenario_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "baseline_commit": self.baseline_commit,
            "accepted": self.accepted,
            "blockers": self.blockers,
            "failures": self.failures,
            "json_path": self.json_path,
            "markdown_path": self.markdown_path,
            "content_digest": self.content_digest,
            "previous_digest": self.previous_digest,
            "sequence": self.sequence,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReportRecord:
        return cls(
            report_id=str(value.get("report_id") or ""),
            generated_at=str(value.get("generated_at") or ""),
            scenario_id=str(value.get("scenario_id") or ""),
            task_id=str(value.get("task_id") or ""),
            run_id=str(value.get("run_id") or ""),
            baseline_commit=str(value.get("baseline_commit") or ""),
            accepted=bool(value.get("accepted")),
            blockers=int(value.get("blockers") or 0),
            failures=int(value.get("failures") or 0),
            json_path=str(value.get("json_path") or ""),
            markdown_path=str(value.get("markdown_path") or ""),
            content_digest=str(value.get("content_digest") or ""),
            previous_digest=str(value.get("previous_digest") or ""),
            sequence=int(value.get("sequence") or 0),
            metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
        )


@dataclass(frozen=True, slots=True)
class AuditLease:
    lease_id: str
    owner: str
    acquired_at: str
    expires_at_epoch: float
    scope: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "owner": self.owner,
            "acquired_at": self.acquired_at,
            "expires_at_epoch": self.expires_at_epoch,
            "scope": self.scope,
        }


class AtomicFileWriter:
    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def write_json(self, path: Path, value: Any) -> None:
        encoded = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        self.write_text(path, encoded)


class SafeReportPath:
    _ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def report_json(self, report_id: str) -> Path:
        return self._under_root("reports", self._identifier(report_id) + ".json")

    def report_markdown(self, report_id: str) -> Path:
        return self._under_root("reports", self._identifier(report_id) + ".md")

    def task_index(self, task_id: str) -> Path:
        return self._under_root("tasks", self._identifier(task_id) + ".json")

    def run_index(self, run_id: str) -> Path:
        return self._under_root("runs", self._identifier(run_id) + ".json")

    def lease(self, scope: str) -> Path:
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
        return self._under_root("leases", digest + ".json")

    def _under_root(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise HardeningStoreError(f"report path escapes root: {path}") from error
        return path

    def _identifier(self, value: str) -> str:
        candidate = value.strip()
        if not self._ID.fullmatch(candidate):
            raise HardeningStoreError(f"invalid report identifier: {value!r}")
        return candidate


class HardeningReportStore:
    _INDEX_SCHEMA = "zyra.m1-hardening-index/v1"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.paths = SafeReportPath(self.root)
        self.writer = AtomicFileWriter()
        self.normalizer = ReportNormalizer()
        self.renderer = HardeningMarkdownRenderer()
        self._mutex = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def persist(
        self,
        report: HardeningReport,
        *,
        disposition: ReportDisposition,
        evidence_audit: Mapping[str, Any],
    ) -> ReportRecord:
        with self._mutex:
            report_path = self.paths.report_json(report.report_id)
            markdown_path = self.paths.report_markdown(report.report_id)
            if report_path.exists() or markdown_path.exists():
                existing = self.load(report.report_id, verify=True)
                candidate = self._payload(report, disposition, evidence_audit)
                existing_content = dict(existing)
                existing_content.pop("storage", None)
                if self.normalizer.digest(existing_content) == self.normalizer.digest(candidate):
                    record = self.record(report.report_id)
                    if record is None:
                        raise ReportIntegrityError("idempotent report exists without index record")
                    return record
                raise ReportIntegrityError(f"report id already contains different evidence: {report.report_id}")
            index = self._load_index()
            records = [ReportRecord.from_dict(item) for item in index.get("reports", []) if isinstance(item, Mapping)]
            previous_digest = records[-1].content_digest if records else ""
            payload = self._payload(report, disposition, evidence_audit)
            digest = self.normalizer.digest(payload, retain_timestamps=True)
            sequence = records[-1].sequence + 1 if records else 1
            record = ReportRecord(
                report_id=report.report_id,
                generated_at=report.generated_at,
                scenario_id=report.scenario_id,
                task_id=report.task_id,
                run_id=report.run_id,
                baseline_commit=report.baseline_commit,
                accepted=disposition.accepted,
                blockers=report.blockers,
                failures=report.failures,
                json_path=report_path.relative_to(self.root).as_posix(),
                markdown_path=markdown_path.relative_to(self.root).as_posix(),
                content_digest=digest,
                previous_digest=previous_digest,
                sequence=sequence,
                metadata={
                    "final_completion": disposition.final_completion,
                    "gate_count": len(report.gates),
                    "evidence_complete": bool(evidence_audit.get("complete")),
                },
            )
            payload["storage"] = {
                "sequence": sequence,
                "content_digest": digest,
                "previous_digest": previous_digest,
            }
            markdown = self.renderer.render(report, disposition=disposition, evidence_audit=evidence_audit)
            self.writer.write_json(report_path, payload)
            self.writer.write_text(markdown_path, markdown)
            records.append(record)
            self._write_index(records)
            self._append_secondary_index(self.paths.task_index(report.task_id), record) if report.task_id else None
            self._append_secondary_index(self.paths.run_index(report.run_id), record) if report.run_id else None
            return record

    def load(self, report_id: str, *, verify: bool = True) -> dict[str, Any]:
        path = self.paths.report_json(report_id)
        if not path.is_file():
            raise ReportNotFound(report_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReportIntegrityError(f"cannot decode report {report_id}: {error}") from error
        if not isinstance(value, dict):
            raise ReportIntegrityError(f"report {report_id} is not a JSON object")
        if verify:
            self.verify_report(value)
        return value

    def latest(
        self,
        *,
        task_id: str = "",
        run_id: str = "",
        accepted_only: bool = False,
    ) -> dict[str, Any] | None:
        records = self.list_records(task_id=task_id, run_id=run_id, accepted_only=accepted_only, limit=1)
        return self.load(records[0].report_id) if records else None

    def list_records(
        self,
        *,
        task_id: str = "",
        run_id: str = "",
        scenario_id: str = "",
        accepted_only: bool = False,
        baseline_commit: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReportRecord]:
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        records = [
            ReportRecord.from_dict(value)
            for value in self._load_index().get("reports", [])
            if isinstance(value, Mapping)
        ]
        selected = [
            record
            for record in records
            if (not task_id or record.task_id == task_id)
            and (not run_id or record.run_id == run_id)
            and (not scenario_id or record.scenario_id == scenario_id)
            and (not accepted_only or record.accepted)
            and (not baseline_commit or record.baseline_commit == baseline_commit)
        ]
        selected.sort(key=lambda item: (item.sequence, item.generated_at), reverse=True)
        if limit == 0:
            return selected[offset:]
        return selected[offset : offset + limit]

    def record(self, report_id: str) -> ReportRecord | None:
        for record in self.list_records(limit=0):
            if record.report_id == report_id:
                return record
        return None

    def verify_report(self, payload: Mapping[str, Any]) -> None:
        report_id = str(payload.get("report_id") or "")
        storage = payload.get("storage") if isinstance(payload.get("storage"), Mapping) else {}
        expected = str(storage.get("content_digest") or "")
        candidate = dict(payload)
        candidate.pop("storage", None)
        actual = self.normalizer.digest(candidate, retain_timestamps=True)
        if not expected:
            raise ReportIntegrityError(f"report {report_id} has no storage digest")
        if expected != actual:
            raise ReportIntegrityError(f"report digest mismatch: {report_id}")
        record = self.record(report_id)
        if record is None:
            raise ReportIntegrityError(f"report is not indexed: {report_id}")
        if record.content_digest != expected:
            raise ReportIntegrityError(f"index digest mismatch: {report_id}")

    def verify_chain(self) -> dict[str, Any]:
        records = list(reversed(self.list_records(limit=0)))
        errors: list[dict[str, Any]] = []
        expected_sequence = 1
        previous_digest = ""
        verified = 0
        for record in records:
            if record.sequence != expected_sequence:
                errors.append(
                    {
                        "report_id": record.report_id,
                        "error": "sequence_gap",
                        "expected": expected_sequence,
                        "actual": record.sequence,
                    }
                )
            if record.previous_digest != previous_digest:
                errors.append(
                    {
                        "report_id": record.report_id,
                        "error": "previous_digest_mismatch",
                        "expected": previous_digest,
                        "actual": record.previous_digest,
                    }
                )
            try:
                self.load(record.report_id, verify=True)
                verified += 1
            except ReportIntegrityError as error:
                errors.append({"report_id": record.report_id, "error": str(error)})
            previous_digest = record.content_digest
            expected_sequence = record.sequence + 1
        return {
            "record_count": len(records),
            "verified_count": verified,
            "error_count": len(errors),
            "errors": errors,
            "head_digest": previous_digest,
            "valid": not errors,
        }

    def status(self) -> dict[str, Any]:
        records = self.list_records(limit=0)
        accepted = sum(1 for item in records if item.accepted)
        tasks = {item.task_id for item in records if item.task_id}
        runs = {item.run_id for item in records if item.run_id}
        baselines = Counter(item.baseline_commit for item in records)
        latest = records[0] if records else None
        chain = self.verify_chain()
        return {
            "schema": "zyra.m1-hardening-store-status/v1",
            "root": str(self.root),
            "report_count": len(records),
            "accepted_count": accepted,
            "rejected_count": len(records) - accepted,
            "task_count": len(tasks),
            "run_count": len(runs),
            "baselines": dict(sorted(baselines.items())),
            "latest": latest.to_dict() if latest else None,
            "chain": chain,
        }

    @contextmanager
    def audit_lease(
        self,
        scope: str,
        *,
        owner: str,
        ttl_seconds: float = 1800.0,
    ) -> Iterator[AuditLease]:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        path = self.paths.lease(scope)
        lease = AuditLease(
            lease_id=uuid.uuid4().hex,
            owner=owner,
            acquired_at=datetime.now(timezone.utc).isoformat(),
            expires_at_epoch=time.time() + ttl_seconds,
            scope=scope,
        )
        with self._mutex:
            current = self._read_json(path, default={})
            if current and float(current.get("expires_at_epoch") or 0) > time.time():
                raise ConcurrentAuditError(
                    f"audit scope {scope!r} is held by {current.get('owner')} ({current.get('lease_id')})"
                )
            self.writer.write_json(path, lease.to_dict())
        try:
            yield lease
        finally:
            with self._mutex:
                current = self._read_json(path, default={})
                if str(current.get("lease_id") or "") == lease.lease_id and path.exists():
                    path.unlink()

    def prune_unindexed_temporary_files(self) -> dict[str, Any]:
        removed: list[str] = []
        reports = self.root / "reports"
        if not reports.is_dir():
            return {"removed": removed, "count": 0}
        for path in reports.iterdir():
            if not path.is_file() or not path.name.endswith(".tmp"):
                continue
            try:
                path.relative_to(self.root)
            except ValueError:
                continue
            path.unlink()
            removed.append(path.relative_to(self.root).as_posix())
        return {"removed": sorted(removed), "count": len(removed)}

    def _payload(
        self,
        report: HardeningReport,
        disposition: ReportDisposition,
        evidence_audit: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = report.to_dict()
        payload["disposition"] = disposition.to_dict()
        payload["evidence_link_audit"] = dict(evidence_audit)
        return payload

    def _load_index(self) -> dict[str, Any]:
        value = self._read_json(self.root / "index.json", default={})
        if not value:
            return {"schema": self._INDEX_SCHEMA, "revision": 0, "reports": []}
        if value.get("schema") != self._INDEX_SCHEMA:
            raise ReportIntegrityError("unsupported hardening index schema")
        if not isinstance(value.get("reports"), Sequence):
            raise ReportIntegrityError("hardening index reports field is invalid")
        return value

    def _write_index(self, records: Sequence[ReportRecord]) -> None:
        revision = int(self._load_index().get("revision") or 0) + 1
        payload = {
            "schema": self._INDEX_SCHEMA,
            "revision": revision,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reports": [item.to_dict() for item in records],
        }
        self.writer.write_json(self.root / "index.json", payload)

    def _append_secondary_index(self, path: Path, record: ReportRecord) -> None:
        current = self._read_json(path, default={})
        values = current.get("reports") if isinstance(current.get("reports"), Sequence) else []
        report_ids = [str(item) for item in values if str(item)]
        if record.report_id not in report_ids:
            report_ids.append(record.report_id)
        self.writer.write_json(
            path,
            {
                "schema": "zyra.m1-hardening-secondary-index/v1",
                "revision": int(current.get("revision") or 0) + 1,
                "reports": report_ids,
                "latest_report_id": record.report_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def _read_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
        if not path.is_file():
            return dict(default)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReportIntegrityError(f"cannot decode {path}: {error}") from error
        if not isinstance(value, dict):
            raise ReportIntegrityError(f"expected JSON object: {path}")
        return value
