"""Deterministic submission candidate construction and independent checks.

The submission builder owns the final artifact boundary.  It copies only
explicitly admitted repository files, records their content digests, creates a
deterministic ZIP, and binds two independent reviewer attestations to the same
manifest.  The verifier does not trust builder state: it re-reads every file
and archive member and recomputes all digests from bytes.
"""

from __future__ import annotations

import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable

from zyra_evaluation.freeze_reporting.canonical import (
    digest,
    file_digest,
    normalize,
)

from .common import (
    FindingLedger,
    FinalFreezeError,
    ensure_new_directory,
    fail,
    load_json,
    object_with_digest,
    require_boolean,
    require_choice,
    require_digest,
    require_email,
    require_identity,
    require_integer,
    require_mapping,
    require_sequence,
    require_text,
    safe_relative_path,
    stable_unique,
    utc_now,
    write_json,
)


SUBMISSION_NAME_PATTERN = re.compile(
    r"^zyra-first-stage-2026-[0-9a-f]{7,40}\.zip$"
)
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SubmissionMaterial:
    """One file admitted into the final submission candidate."""

    source_path: str
    archive_path: str
    role: str
    required: bool = True
    description: str = ""

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "SubmissionMaterial":
        item = require_mapping(value, label)
        return cls(
            source_path=safe_relative_path(
                item.get("source_path"),
                f"{label}.source_path",
            ),
            archive_path=safe_relative_path(
                item.get("archive_path"),
                f"{label}.archive_path",
            ),
            role=require_choice(
                item.get("role"),
                f"{label}.role",
                (
                    "source",
                    "configuration",
                    "documentation",
                    "evidence",
                    "runbook",
                    "license",
                    "metadata",
                ),
            ),
            required=require_boolean(
                item.get("required"),
                f"{label}.required",
            ),
            description=require_text(
                item.get("description", ""),
                f"{label}.description",
                allow_empty=True,
                maximum=2048,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "archive_path": self.archive_path,
            "role": self.role,
            "required": self.required,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ReviewerAttestation:
    """One human review statement bound to a manifest digest."""

    reviewer_id: str
    reviewer_email: str
    role: str
    reviewed_at: str
    manifest_digest: str
    decision: str
    checks: tuple[str, ...]
    note: str = ""

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "ReviewerAttestation":
        item = require_mapping(value, label)
        checks = tuple(
            require_identity(entry, f"{label}.checks[{index}]")
            for index, entry in enumerate(
                require_sequence(
                    item.get("checks"),
                    f"{label}.checks",
                    minimum=1,
                )
            )
        )
        return cls(
            reviewer_id=require_identity(
                item.get("reviewer_id"),
                f"{label}.reviewer_id",
            ),
            reviewer_email=require_email(
                item.get("reviewer_email"),
                f"{label}.reviewer_email",
            ),
            role=require_choice(
                item.get("role"),
                f"{label}.role",
                (
                    "technical",
                    "submission",
                    "security",
                    "product",
                    "release",
                ),
            ),
            reviewed_at=require_text(
                item.get("reviewed_at"),
                f"{label}.reviewed_at",
            ),
            manifest_digest=require_digest(
                item.get("manifest_digest"),
                f"{label}.manifest_digest",
            ),
            decision=require_choice(
                item.get("decision"),
                f"{label}.decision",
                ("approve", "reject"),
            ),
            checks=checks,
            note=require_text(
                item.get("note", ""),
                f"{label}.note",
                allow_empty=True,
                maximum=4096,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer_id": self.reviewer_id,
            "reviewer_email": self.reviewer_email,
            "role": self.role,
            "reviewed_at": self.reviewed_at,
            "manifest_digest": self.manifest_digest,
            "decision": self.decision,
            "checks": list(self.checks),
            "note": self.note,
        }


class SubmissionManifestBuilder:
    """Build an immutable manifest from an explicit material allowlist."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        target_commit: str,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.target_commit = require_text(
            target_commit,
            "target_commit",
            minimum=7,
            maximum=40,
        ).lower()

    def build(
        self,
        materials: Iterable[SubmissionMaterial | dict[str, Any]],
    ) -> dict[str, Any]:
        rows: list[SubmissionMaterial] = []
        for index, raw in enumerate(materials):
            rows.append(
                raw
                if isinstance(raw, SubmissionMaterial)
                else SubmissionMaterial.from_dict(
                    raw,
                    f"materials[{index}]",
                )
            )
        if not rows:
            fail(
                "empty-submission",
                "submission material allowlist cannot be empty",
                phase="submission-manifest",
            )
        ledger = FindingLedger()
        seen_sources: set[str] = set()
        seen_archives: set[str] = set()
        members: list[dict[str, Any]] = []
        role_counts: dict[str, int] = {}
        total_bytes = 0
        for material in sorted(rows, key=lambda item: item.archive_path):
            self._check_duplicate_paths(
                material,
                seen_sources,
                seen_archives,
                ledger,
            )
            source = self._resolve_source(material.source_path, ledger)
            member = self._inspect_source(material, source, ledger)
            if member is None:
                continue
            members.append(member)
            role_counts[material.role] = role_counts.get(material.role, 0) + 1
            total_bytes += member["size_bytes"]
        self._check_required_roles(role_counts, ledger)
        if total_bytes > MAX_ARCHIVE_BYTES:
            ledger.blocker(
                "submission-too-large",
                "submission material exceeds the maximum archive budget",
                category="submission",
                total_bytes=total_bytes,
                maximum=MAX_ARCHIVE_BYTES,
            )
        document = {
            "schema": "zyra.final-freeze.submission-manifest.v1",
            "valid": ledger.valid,
            "target_commit": self.target_commit,
            "archive_name": (
                f"zyra-first-stage-2026-{self.target_commit[:12]}.zip"
            ),
            "member_count": len(members),
            "total_bytes": total_bytes,
            "role_counts": normalize(role_counts),
            "members": members,
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)

    def require_valid(
        self,
        materials: Iterable[SubmissionMaterial | dict[str, Any]],
    ) -> dict[str, Any]:
        manifest = self.build(materials)
        findings = require_mapping(
            manifest.get("findings"),
            "manifest.findings",
        )
        if not manifest["valid"]:
            fail(
                "invalid-submission-manifest",
                "submission manifest contains blocking findings",
                phase="submission-manifest",
                findings=findings,
            )
        return manifest

    def _check_duplicate_paths(
        self,
        material: SubmissionMaterial,
        seen_sources: set[str],
        seen_archives: set[str],
        ledger: FindingLedger,
    ) -> None:
        if material.source_path in seen_sources:
            ledger.blocker(
                "duplicate-submission-source",
                "the same source file is admitted more than once",
                category="submission",
                source_path=material.source_path,
            )
        if material.archive_path in seen_archives:
            ledger.blocker(
                "duplicate-submission-member",
                "two materials map to the same archive path",
                category="submission",
                archive_path=material.archive_path,
            )
        seen_sources.add(material.source_path)
        seen_archives.add(material.archive_path)

    def _resolve_source(
        self,
        relative: str,
        ledger: FindingLedger,
    ) -> Path:
        candidate = (self.repository_root / relative).resolve()
        try:
            candidate.relative_to(self.repository_root)
        except ValueError:
            ledger.blocker(
                "submission-path-escape",
                "submission material resolves outside the repository",
                category="submission",
                path=relative,
            )
        return candidate

    def _inspect_source(
        self,
        material: SubmissionMaterial,
        source: Path,
        ledger: FindingLedger,
    ) -> dict[str, Any] | None:
        if not source.exists():
            method = ledger.blocker if material.required else ledger.warning
            method(
                "missing-submission-material",
                "submission material does not exist",
                category="submission",
                source_path=material.source_path,
                required=material.required,
            )
            return None
        if source.is_symlink():
            ledger.blocker(
                "submission-symlink-rejected",
                "submission material cannot be a symbolic link",
                category="submission",
                source_path=material.source_path,
            )
            return None
        if not source.is_file():
            ledger.blocker(
                "submission-material-not-file",
                "submission materials must name individual files",
                category="submission",
                source_path=material.source_path,
            )
            return None
        size = source.stat().st_size
        if size > MAX_MEMBER_BYTES:
            ledger.blocker(
                "submission-member-too-large",
                "submission member exceeds the per-file size budget",
                category="submission",
                source_path=material.source_path,
                size_bytes=size,
                maximum=MAX_MEMBER_BYTES,
            )
        return {
            **material.to_dict(),
            "size_bytes": size,
            "sha256": file_digest(source),
            "executable": bool(source.stat().st_mode & stat.S_IXUSR),
        }

    def _check_required_roles(
        self,
        role_counts: dict[str, int],
        ledger: FindingLedger,
    ) -> None:
        for role in ("source", "documentation", "evidence", "runbook"):
            if role_counts.get(role, 0) == 0:
                ledger.blocker(
                    "missing-submission-role",
                    "submission is missing a required material role",
                    category="submission",
                    role=role,
                )


class SubmissionCandidateBuilder:
    """Materialize the manifest, archive, checksum list, and email checklist."""

    def __init__(self, repository_root: str | Path) -> None:
        self.repository_root = Path(repository_root).resolve()

    def build(
        self,
        output_directory: str | Path,
        manifest: dict[str, Any],
        *,
        email_recipients: Iterable[str],
        email_subject: str,
        instructions: Iterable[str],
    ) -> dict[str, Any]:
        self._require_manifest(manifest)
        output = ensure_new_directory(output_directory)
        try:
            staging = output / "payload"
            staging.mkdir()
            self._copy_members(manifest, staging)
            manifest_path = output / "submission-manifest.json"
            write_json(manifest_path, manifest)
            archive_path = output / require_text(
                manifest.get("archive_name"),
                "manifest.archive_name",
            )
            self._write_archive(staging, archive_path)
            checksum_path = output / "SHA256SUMS"
            self._write_checksums(
                checksum_path,
                [
                    manifest_path,
                    archive_path,
                ],
            )
            email = self._build_email_checklist(
                manifest,
                archive_path,
                email_recipients,
                email_subject,
                instructions,
            )
            email_path = output / "submission-email-checklist.json"
            write_json(email_path, email)
            receipt = {
                "schema": "zyra.final-freeze.submission-candidate.v1",
                "valid": True,
                "built_at": utc_now(),
                "manifest_path": manifest_path.name,
                "manifest_digest": manifest["digest"],
                "archive_path": archive_path.name,
                "archive_sha256": file_digest(archive_path),
                "archive_size_bytes": archive_path.stat().st_size,
                "checksum_path": checksum_path.name,
                "checksum_sha256": file_digest(checksum_path),
                "email_checklist_path": email_path.name,
                "email_checklist_digest": email["digest"],
                "member_count": manifest["member_count"],
            }
            receipt = object_with_digest(receipt)
            write_json(output / "candidate-receipt.json", receipt)
            return receipt
        except Exception:
            if output.exists():
                shutil.rmtree(output)
            raise

    def _require_manifest(self, manifest: dict[str, Any]) -> None:
        require_mapping(manifest, "manifest")
        if manifest.get("schema") != (
            "zyra.final-freeze.submission-manifest.v1"
        ):
            fail(
                "manifest-schema-mismatch",
                "candidate builder received an unsupported manifest schema",
                phase="submission-build",
                schema=manifest.get("schema"),
            )
        if manifest.get("valid") is not True:
            fail(
                "manifest-not-valid",
                "candidate builder refuses an invalid manifest",
                phase="submission-build",
            )
        expected = manifest.get("digest")
        body = {key: value for key, value in manifest.items() if key != "digest"}
        if expected != digest(body):
            fail(
                "manifest-digest-mismatch",
                "manifest content does not match its embedded digest",
                phase="submission-build",
                expected=expected,
                actual=digest(body),
            )
        archive_name = require_text(
            manifest.get("archive_name"),
            "manifest.archive_name",
        )
        if not SUBMISSION_NAME_PATTERN.fullmatch(archive_name):
            fail(
                "invalid-submission-name",
                "submission archive name violates the frozen naming rule",
                phase="submission-build",
                archive_name=archive_name,
            )

    def _copy_members(
        self,
        manifest: dict[str, Any],
        staging: Path,
    ) -> None:
        for index, raw in enumerate(
            require_sequence(
                manifest.get("members"),
                "manifest.members",
                minimum=1,
            )
        ):
            member = require_mapping(raw, f"manifest.members[{index}]")
            source_relative = safe_relative_path(
                member.get("source_path"),
                f"manifest.members[{index}].source_path",
            )
            archive_relative = safe_relative_path(
                member.get("archive_path"),
                f"manifest.members[{index}].archive_path",
            )
            source = (self.repository_root / source_relative).resolve()
            destination = (staging / archive_relative).resolve()
            try:
                source.relative_to(self.repository_root)
                destination.relative_to(staging.resolve())
            except ValueError:
                fail(
                    "copy-path-escape",
                    "submission copy path escaped its allowed root",
                    phase="submission-build",
                    source=source_relative,
                    destination=archive_relative,
                )
            if not source.is_file() or source.is_symlink():
                fail(
                    "submission-source-changed",
                    "submission source is missing, non-file, or symlinked",
                    phase="submission-build",
                    source=source_relative,
                )
            expected = require_digest(
                member.get("sha256"),
                f"manifest.members[{index}].sha256",
            )
            actual = file_digest(source)
            if expected != actual:
                fail(
                    "submission-source-digest-changed",
                    "submission source changed after manifest generation",
                    phase="submission-build",
                    source=source_relative,
                    expected=expected,
                    actual=actual,
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    def _write_archive(self, staging: Path, archive_path: Path) -> None:
        entries = sorted(
            path
            for path in staging.rglob("*")
            if path.is_file()
        )
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in entries:
                relative = path.relative_to(staging).as_posix()
                info = zipfile.ZipInfo(relative, date_time=ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                info.create_system = 3
                archive.writestr(info, path.read_bytes())

    def _write_checksums(
        self,
        path: Path,
        files: list[Path],
    ) -> None:
        lines = [
            f"{file_digest(file)}  {file.name}"
            for file in sorted(files, key=lambda item: item.name)
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _build_email_checklist(
        self,
        manifest: dict[str, Any],
        archive_path: Path,
        recipients: Iterable[str],
        subject: str,
        instructions: Iterable[str],
    ) -> dict[str, Any]:
        addresses = stable_unique(
            require_email(value, "email_recipient")
            for value in recipients
        )
        if not addresses:
            fail(
                "missing-email-recipient",
                "submission email checklist requires a recipient",
                phase="submission-email",
            )
        steps = stable_unique(
            require_text(value, "email_instruction", maximum=4096)
            for value in instructions
        )
        if not steps:
            fail(
                "missing-email-instructions",
                "submission email checklist requires instructions",
                phase="submission-email",
            )
        document = {
            "schema": "zyra.final-freeze.submission-email.v1",
            "recipients": addresses,
            "subject": require_text(
                subject,
                "email_subject",
                maximum=255,
            ),
            "attachment": archive_path.name,
            "attachment_sha256": file_digest(archive_path),
            "manifest_digest": manifest["digest"],
            "instructions": steps,
            "required_confirmation": (
                "record the platform or email acceptance receipt and compare "
                "its attachment digest with this checklist"
            ),
        }
        return object_with_digest(document)


class DualReviewVerifier:
    """Require two distinct approvals for the exact manifest."""

    REQUIRED_CHECKS = {
        "manifest",
        "checksums",
        "clean-rehearsal",
        "email-checklist",
    }

    def verify(
        self,
        attestations: Iterable[ReviewerAttestation | dict[str, Any]],
        *,
        manifest_digest: str,
    ) -> dict[str, Any]:
        expected = require_digest(manifest_digest, "manifest_digest")
        ledger = FindingLedger()
        rows: list[ReviewerAttestation] = []
        for index, raw in enumerate(attestations):
            rows.append(
                raw
                if isinstance(raw, ReviewerAttestation)
                else ReviewerAttestation.from_dict(
                    raw,
                    f"attestations[{index}]",
                )
            )
        reviewers: set[str] = set()
        emails: set[str] = set()
        roles: set[str] = set()
        for row in rows:
            if row.reviewer_id in reviewers:
                ledger.blocker(
                    "duplicate-reviewer",
                    "dual review requires two distinct reviewer identities",
                    category="submission-review",
                    reviewer_id=row.reviewer_id,
                )
            if row.reviewer_email in emails:
                ledger.blocker(
                    "duplicate-reviewer-email",
                    "dual review requires distinct reviewer contacts",
                    category="submission-review",
                    reviewer_email=row.reviewer_email,
                )
            reviewers.add(row.reviewer_id)
            emails.add(row.reviewer_email)
            roles.add(row.role)
            if row.manifest_digest != expected:
                ledger.blocker(
                    "reviewed-manifest-mismatch",
                    "reviewer approved a different submission manifest",
                    category="submission-review",
                    reviewer_id=row.reviewer_id,
                    expected=expected,
                    actual=row.manifest_digest,
                )
            if row.decision != "approve":
                ledger.blocker(
                    "reviewer-rejected-candidate",
                    "a required reviewer rejected the submission candidate",
                    category="submission-review",
                    reviewer_id=row.reviewer_id,
                )
            missing = sorted(self.REQUIRED_CHECKS - set(row.checks))
            if missing:
                ledger.blocker(
                    "review-checks-incomplete",
                    "reviewer attestation omits required checks",
                    category="submission-review",
                    reviewer_id=row.reviewer_id,
                    missing=missing,
                )
        if len(rows) < 2 or len(reviewers) < 2:
            ledger.blocker(
                "insufficient-independent-reviews",
                "final submission requires at least two independent reviews",
                category="submission-review",
                review_count=len(rows),
                distinct_reviewers=len(reviewers),
            )
        if len(roles) < 2:
            ledger.blocker(
                "insufficient-review-role-separation",
                "dual review must include at least two review roles",
                category="submission-review",
                roles=sorted(roles),
            )
        document = {
            "schema": "zyra.final-freeze.dual-review.v1",
            "valid": ledger.valid,
            "manifest_digest": expected,
            "attestations": [
                row.to_dict()
                for row in sorted(rows, key=lambda item: item.reviewer_id)
            ],
            "reviewer_count": len(reviewers),
            "role_count": len(roles),
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)


class SubmissionCandidateVerifier:
    """Independently verify a built candidate from filesystem bytes."""

    def verify(self, candidate_directory: str | Path) -> dict[str, Any]:
        root = Path(candidate_directory).resolve()
        ledger = FindingLedger()
        manifest = self._load_required_json(
            root / "submission-manifest.json",
            "submission-manifest",
            ledger,
        )
        receipt = self._load_required_json(
            root / "candidate-receipt.json",
            "candidate-receipt",
            ledger,
        )
        email = self._load_required_json(
            root / "submission-email-checklist.json",
            "submission-email",
            ledger,
        )
        manifest_digest = self._verify_embedded_digest(
            manifest,
            "submission-manifest",
            ledger,
        )
        receipt_digest = self._verify_embedded_digest(
            receipt,
            "candidate-receipt",
            ledger,
        )
        email_digest = self._verify_embedded_digest(
            email,
            "submission-email",
            ledger,
        )
        archive_name = manifest.get("archive_name")
        if not isinstance(archive_name, str) or not (
            SUBMISSION_NAME_PATTERN.fullmatch(archive_name)
        ):
            ledger.blocker(
                "invalid-submission-name",
                "candidate archive does not follow the frozen naming rule",
                category="submission-verification",
                archive_name=archive_name,
            )
            archive_path = root / "invalid.zip"
        else:
            archive_path = root / archive_name
        archive_result = self._verify_archive(
            archive_path,
            manifest,
            ledger,
        )
        checksum_result = self._verify_checksums(
            root / "SHA256SUMS",
            root,
            ledger,
        )
        self._verify_receipt_bindings(
            receipt,
            manifest_digest,
            archive_path,
            email_digest,
            ledger,
        )
        self._verify_email(
            email,
            manifest_digest,
            archive_path,
            ledger,
        )
        document = {
            "schema": "zyra.final-freeze.submission-verification.v1",
            "valid": ledger.valid,
            "candidate_directory": root.name,
            "manifest_digest": manifest_digest,
            "receipt_digest": receipt_digest,
            "email_digest": email_digest,
            "archive": archive_result,
            "checksums": checksum_result,
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)

    def require_valid(self, candidate_directory: str | Path) -> dict[str, Any]:
        receipt = self.verify(candidate_directory)
        if receipt["valid"] is not True:
            raise FinalFreezeError(
                "invalid-submission-candidate",
                "independent verification rejected the submission candidate",
                phase="submission-verification",
                detail={"findings": receipt["findings"]},
            )
        return receipt

    def _load_required_json(
        self,
        path: Path,
        label: str,
        ledger: FindingLedger,
    ) -> dict[str, Any]:
        if not path.is_file():
            ledger.blocker(
                "missing-candidate-document",
                "submission candidate is missing a required document",
                category="submission-verification",
                document=label,
                path=path.name,
            )
            return {}
        try:
            return load_json(path, label=label)
        except FinalFreezeError as error:
            ledger.blocker(
                error.code,
                str(error),
                category="submission-verification",
                document=label,
                **error.detail,
            )
            return {}

    def _verify_embedded_digest(
        self,
        document: dict[str, Any],
        label: str,
        ledger: FindingLedger,
    ) -> str | None:
        actual = document.get("digest")
        if not isinstance(actual, str):
            ledger.blocker(
                "missing-document-digest",
                "candidate document has no embedded digest",
                category="submission-verification",
                document=label,
            )
            return None
        expected = digest(
            {key: value for key, value in document.items() if key != "digest"}
        )
        if actual != expected:
            ledger.blocker(
                "candidate-document-digest-mismatch",
                "candidate document content does not match its digest",
                category="submission-verification",
                document=label,
                expected=expected,
                actual=actual,
            )
        return actual

    def _verify_archive(
        self,
        archive_path: Path,
        manifest: dict[str, Any],
        ledger: FindingLedger,
    ) -> dict[str, Any]:
        if not archive_path.is_file():
            ledger.blocker(
                "missing-submission-archive",
                "submission candidate is missing its archive",
                category="submission-verification",
                archive_path=archive_path.name,
            )
            return {"valid": False, "member_count": 0}
        expected_rows: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(
            require_sequence(
                manifest.get("members", []),
                "manifest.members",
            )
        ):
            row = require_mapping(raw, f"manifest.members[{index}]")
            path = safe_relative_path(
                row.get("archive_path"),
                f"manifest.members[{index}].archive_path",
            )
            expected_rows[path] = row
        seen: set[str] = set()
        total = 0
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                for info in archive.infolist():
                    name = self._safe_zip_name(info.filename, ledger)
                    if name is None:
                        continue
                    if name in seen:
                        ledger.blocker(
                            "duplicate-archive-member",
                            "archive contains a duplicate member path",
                            category="submission-verification",
                            member=name,
                        )
                    seen.add(name)
                    if info.is_dir():
                        continue
                    total += info.file_size
                    if info.file_size > MAX_MEMBER_BYTES:
                        ledger.blocker(
                            "archive-member-too-large",
                            "archive member exceeds its size budget",
                            category="submission-verification",
                            member=name,
                            size_bytes=info.file_size,
                        )
                    if total > MAX_ARCHIVE_BYTES:
                        ledger.blocker(
                            "archive-expansion-too-large",
                            "expanded archive exceeds the size budget",
                            category="submission-verification",
                            total_bytes=total,
                        )
                    data_digest, count = self._digest_member(
                        archive.open(info, "r")
                    )
                    expected = expected_rows.get(name)
                    if expected is None:
                        ledger.blocker(
                            "unexpected-archive-member",
                            "archive contains a member absent from manifest",
                            category="submission-verification",
                            member=name,
                        )
                        continue
                    if data_digest != expected.get("sha256"):
                        ledger.blocker(
                            "archive-member-digest-mismatch",
                            "archive member content differs from manifest",
                            category="submission-verification",
                            member=name,
                            expected=expected.get("sha256"),
                            actual=data_digest,
                        )
                    if count != expected.get("size_bytes"):
                        ledger.blocker(
                            "archive-member-size-mismatch",
                            "archive member size differs from manifest",
                            category="submission-verification",
                            member=name,
                            expected=expected.get("size_bytes"),
                            actual=count,
                        )
        except (OSError, zipfile.BadZipFile) as error:
            ledger.blocker(
                "unreadable-submission-archive",
                "submission archive could not be independently read",
                category="submission-verification",
                error=str(error),
            )
        missing = sorted(set(expected_rows) - seen)
        if missing:
            ledger.blocker(
                "missing-archive-members",
                "archive omits files declared by the manifest",
                category="submission-verification",
                missing=missing,
            )
        return {
            "valid": not missing,
            "archive_path": archive_path.name,
            "archive_sha256": file_digest(archive_path),
            "archive_size_bytes": archive_path.stat().st_size,
            "member_count": len(seen),
            "expanded_bytes": total,
        }

    def _safe_zip_name(
        self,
        value: str,
        ledger: FindingLedger,
    ) -> str | None:
        name = str(value).replace("\\", "/")
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or ".." in path.parts
            or any(part in {"", "."} for part in path.parts)
            or re.match(r"^[A-Za-z]:", name)
        ):
            ledger.blocker(
                "unsafe-archive-member",
                "archive member path is absolute or traverses directories",
                category="submission-verification",
                member=name,
            )
            return None
        return path.as_posix()

    def _digest_member(self, stream: BinaryIO) -> tuple[str, int]:
        import hashlib

        hasher = hashlib.sha256()
        count = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            hasher.update(chunk)
        return hasher.hexdigest(), count

    def _verify_checksums(
        self,
        checksum_path: Path,
        root: Path,
        ledger: FindingLedger,
    ) -> dict[str, Any]:
        if not checksum_path.is_file():
            ledger.blocker(
                "missing-checksum-file",
                "submission candidate is missing SHA256SUMS",
                category="submission-verification",
            )
            return {"valid": False, "entries": []}
        entries: list[dict[str, str]] = []
        for line_number, line in enumerate(
            checksum_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
            if match is None:
                ledger.blocker(
                    "invalid-checksum-line",
                    "SHA256SUMS contains a malformed line",
                    category="submission-verification",
                    line_number=line_number,
                )
                continue
            expected, name = match.groups()
            target = root / name
            if not target.is_file():
                ledger.blocker(
                    "checksum-target-missing",
                    "SHA256SUMS names a missing file",
                    category="submission-verification",
                    file=name,
                )
                continue
            actual = file_digest(target)
            entries.append(
                {"file": name, "expected": expected, "actual": actual}
            )
            if expected != actual:
                ledger.blocker(
                    "checksum-mismatch",
                    "submission file does not match SHA256SUMS",
                    category="submission-verification",
                    file=name,
                    expected=expected,
                    actual=actual,
                )
        expected_names = {"submission-manifest.json"}
        archive_names = {
            entry["file"]
            for entry in entries
            if entry["file"].endswith(".zip")
        }
        if not expected_names.issubset({entry["file"] for entry in entries}):
            ledger.blocker(
                "checksum-manifest-entry-missing",
                "SHA256SUMS does not cover the submission manifest",
                category="submission-verification",
            )
        if len(archive_names) != 1:
            ledger.blocker(
                "checksum-archive-entry-invalid",
                "SHA256SUMS must cover exactly one submission archive",
                category="submission-verification",
                archives=sorted(archive_names),
            )
        return {
            "valid": all(
                entry["expected"] == entry["actual"]
                for entry in entries
            ),
            "entries": entries,
            "sha256": file_digest(checksum_path),
        }

    def _verify_receipt_bindings(
        self,
        receipt: dict[str, Any],
        manifest_digest: str | None,
        archive_path: Path,
        email_digest: str | None,
        ledger: FindingLedger,
    ) -> None:
        comparisons = {
            "manifest_digest": manifest_digest,
            "archive_path": archive_path.name,
            "archive_sha256": (
                file_digest(archive_path) if archive_path.is_file() else None
            ),
            "email_checklist_digest": email_digest,
        }
        for key, expected in comparisons.items():
            if receipt.get(key) != expected:
                ledger.blocker(
                    "candidate-receipt-binding-mismatch",
                    "candidate receipt does not bind the verified artifact",
                    category="submission-verification",
                    field=key,
                    expected=expected,
                    actual=receipt.get(key),
                )

    def _verify_email(
        self,
        email: dict[str, Any],
        manifest_digest: str | None,
        archive_path: Path,
        ledger: FindingLedger,
    ) -> None:
        recipients = email.get("recipients")
        if not isinstance(recipients, list) or not recipients:
            ledger.blocker(
                "submission-email-recipient-missing",
                "submission email checklist has no recipient",
                category="submission-verification",
            )
        else:
            for index, value in enumerate(recipients):
                try:
                    require_email(value, f"email.recipients[{index}]")
                except FinalFreezeError as error:
                    ledger.blocker(
                        error.code,
                        str(error),
                        category="submission-verification",
                        **error.detail,
                    )
        if email.get("manifest_digest") != manifest_digest:
            ledger.blocker(
                "email-manifest-binding-mismatch",
                "email checklist names a different manifest",
                category="submission-verification",
                expected=manifest_digest,
                actual=email.get("manifest_digest"),
            )
        if email.get("attachment") != archive_path.name:
            ledger.blocker(
                "email-attachment-name-mismatch",
                "email checklist names a different attachment",
                category="submission-verification",
                expected=archive_path.name,
                actual=email.get("attachment"),
            )
        expected_archive_digest = (
            file_digest(archive_path) if archive_path.is_file() else None
        )
        if email.get("attachment_sha256") != expected_archive_digest:
            ledger.blocker(
                "email-attachment-digest-mismatch",
                "email checklist digest does not match the archive",
                category="submission-verification",
                expected=expected_archive_digest,
                actual=email.get("attachment_sha256"),
            )
