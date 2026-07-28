from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .canonical import (
    bytes_digest,
    canonical_bytes,
    digest,
    file_digest,
    now,
    require_commit,
    require_digest,
    require_mapping,
    require_sequence,
    safe_relative_path,
)
from .errors import blocker, fail, require_no_blockers


ZERO_DIGEST = "0" * 64
MANIFEST_PATH = "manifest.json"
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_MEMBER_COUNT = 100_000
MAX_COMPRESSION_RATIO = 200


def chain_digest(
    *,
    path: str,
    member_digest: str,
    member_bytes: int,
    kind: str,
    previous_digest: str,
) -> str:
    return digest(
        {
            "path": safe_relative_path(path, "archive chain path"),
            "sha256": require_digest(member_digest, "archive member digest"),
            "bytes": int(member_bytes),
            "kind": str(kind),
            "previous_digest": require_digest(
                previous_digest,
                "archive previous digest",
            ),
        }
    )


class EvidenceArchiveBuilder:
    """Builds a deterministic evidence ZIP whose members form a hash chain."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        target_commit: str,
    ) -> None:
        self.repository_root = Path(repository_root).resolve(strict=True)
        self.target_commit = require_commit(target_commit, "archive target commit")
        self._members: dict[str, dict[str, Any]] = {}

    def add_file(
        self,
        source: str | Path,
        *,
        archive_path: str,
        kind: str,
    ) -> None:
        selected = Path(source).resolve(strict=True)
        self._ensure_inside_repository(selected)
        if not selected.is_file():
            raise fail(
                "archive-source-not-file",
                "Evidence archive source must be a regular file.",
                phase="archive",
                detail={"path": str(selected)},
            )
        if selected.is_symlink():
            raise fail(
                "archive-source-symlink-forbidden",
                "Evidence archive cannot include symbolic links.",
                phase="archive",
                detail={"path": str(selected)},
            )
        relative = safe_relative_path(archive_path, "archive member path")
        self._reserve(relative)
        size = selected.stat().st_size
        if size > MAX_MEMBER_BYTES:
            raise fail(
                "archive-source-too-large",
                "Evidence archive source exceeds the member limit.",
                phase="archive",
                detail={"path": str(selected), "bytes": size},
            )
        self._members[relative] = {
            "path": relative,
            "kind": self._kind(kind),
            "source": selected,
            "bytes": size,
            "sha256": file_digest(selected),
        }

    def add_bytes(
        self,
        data: bytes,
        *,
        archive_path: str,
        kind: str,
    ) -> None:
        if not isinstance(data, bytes):
            raise TypeError("archive data must be bytes")
        relative = safe_relative_path(archive_path, "archive member path")
        self._reserve(relative)
        if len(data) > MAX_MEMBER_BYTES:
            raise fail(
                "archive-data-too-large",
                "Generated evidence member exceeds the member limit.",
                phase="archive",
                detail={"path": relative, "bytes": len(data)},
            )
        self._members[relative] = {
            "path": relative,
            "kind": self._kind(kind),
            "data": data,
            "bytes": len(data),
            "sha256": bytes_digest(data),
        }

    def add_json(
        self,
        value: Any,
        *,
        archive_path: str,
        kind: str,
    ) -> None:
        self.add_bytes(
            canonical_bytes(value),
            archive_path=archive_path,
            kind=kind,
        )

    def add_tree(
        self,
        source_root: str | Path,
        *,
        archive_prefix: str,
        kind: str,
        include_suffixes: Sequence[str] = (),
    ) -> None:
        root = Path(source_root).resolve(strict=True)
        self._ensure_inside_repository(root)
        if not root.is_dir():
            raise fail(
                "archive-tree-not-directory",
                "Evidence archive tree source must be a directory.",
                phase="archive",
                detail={"path": str(root)},
            )
        prefix = safe_relative_path(archive_prefix, "archive tree prefix")
        suffixes = {item.lower() for item in include_suffixes}
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if path.is_symlink():
                raise fail(
                    "archive-tree-symlink-forbidden",
                    "Evidence archive tree contains a symbolic link.",
                    phase="archive",
                    detail={"path": str(path)},
                )
            if suffixes and path.suffix.lower() not in suffixes:
                continue
            relative = path.relative_to(root).as_posix()
            self.add_file(
                path,
                archive_path=f"{prefix}/{relative}",
                kind=kind,
            )

    def manifest(self) -> dict[str, Any]:
        if not self._members:
            raise fail(
                "archive-empty",
                "Evidence archive must contain at least one member.",
                phase="archive",
            )
        if len(self._members) > MAX_MEMBER_COUNT:
            raise fail(
                "archive-member-count-exceeded",
                "Evidence archive member count exceeds its limit.",
                phase="archive",
                detail={"member_count": len(self._members)},
            )
        previous = ZERO_DIGEST
        entries = []
        for path in sorted(self._members):
            member = self._members[path]
            current = chain_digest(
                path=path,
                member_digest=member["sha256"],
                member_bytes=member["bytes"],
                kind=member["kind"],
                previous_digest=previous,
            )
            entries.append(
                {
                    "path": path,
                    "sha256": member["sha256"],
                    "bytes": member["bytes"],
                    "kind": member["kind"],
                    "previous_digest": previous,
                    "chain_digest": current,
                }
            )
            previous = current
        manifest = {
            "schema": "zyra.first-stage-evidence-archive/v1",
            "target_commit": self.target_commit,
            "member_count": len(entries),
            "total_member_bytes": sum(item["bytes"] for item in entries),
            "chain_seed": ZERO_DIGEST,
            "chain_root": previous,
            "entries": entries,
            "created_at": now(),
        }
        manifest["manifest_digest"] = digest(manifest)
        return manifest

    def build(self, output_path: str | Path) -> dict[str, Any]:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = self.manifest()
        descriptor = -1
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            os.close(descriptor)
            descriptor = -1
            with zipfile.ZipFile(
                temporary,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=True,
            ) as archive:
                for path in sorted(self._members):
                    member = self._members[path]
                    info = self._zip_info(path)
                    if "data" in member:
                        archive.writestr(info, member["data"])
                    else:
                        with member["source"].open("rb") as source:
                            with archive.open(info, mode="w", force_zip64=True) as sink:
                                self._copy(source, sink)
                archive.writestr(
                    self._zip_info(MANIFEST_PATH),
                    canonical_bytes(manifest),
                )
            built = Path(temporary)
            if built.stat().st_size > MAX_ARCHIVE_BYTES:
                raise fail(
                    "archive-output-too-large",
                    "Evidence archive exceeds the output size limit.",
                    phase="archive",
                    detail={"bytes": built.stat().st_size},
                )
            os.replace(temporary, target)
            temporary = None
        except (OSError, zipfile.BadZipFile) as error:
            raise fail(
                "archive-build-failed",
                "Evidence archive could not be built atomically.",
                phase="archive",
                detail={"path": str(target), "error": str(error)},
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        verification = EvidenceArchiveVerifier().verify(
            target,
            expected_commit=self.target_commit,
        )
        return {
            "schema": "zyra.first-stage-evidence-archive-build/v1",
            "path": str(target),
            "sha256": file_digest(target),
            "bytes": target.stat().st_size,
            "manifest_digest": manifest["manifest_digest"],
            "chain_root": manifest["chain_root"],
            "member_count": manifest["member_count"],
            "verification_receipt": verification,
        }

    def _reserve(self, relative: str) -> None:
        if relative == MANIFEST_PATH:
            raise fail(
                "archive-manifest-path-reserved",
                "Archive manifest path is reserved.",
                phase="archive",
            )
        if relative in self._members:
            raise fail(
                "archive-member-duplicate",
                "Evidence archive member path is duplicated.",
                phase="archive",
                detail={"path": relative},
            )

    def _ensure_inside_repository(self, path: Path) -> None:
        try:
            path.relative_to(self.repository_root)
        except ValueError as error:
            raise fail(
                "archive-source-outside-repository",
                "Evidence archive source is outside the admitted repository.",
                phase="archive",
                detail={"path": str(path)},
            ) from error

    @staticmethod
    def _kind(value: str) -> str:
        selected = str(value or "").strip().lower().replace("_", "-")
        if not selected or len(selected) > 64:
            raise fail(
                "archive-member-kind-invalid",
                "Evidence archive member kind is invalid.",
                phase="archive",
            )
        return selected

    @staticmethod
    def _zip_info(path: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(
            filename=safe_relative_path(path, "ZIP member path"),
            date_time=(1980, 1, 1, 0, 0, 0),
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        info.create_system = 3
        return info

    @staticmethod
    def _copy(source: BinaryIO, sink: BinaryIO) -> None:
        while chunk := source.read(1024 * 1024):
            sink.write(chunk)


class EvidenceArchiveVerifier:
    """Verifies member names, sizes, content digests, and the manifest chain."""

    def verify(
        self,
        archive_path: str | Path,
        *,
        expected_commit: str | None = None,
    ) -> dict[str, Any]:
        selected = Path(archive_path).resolve(strict=True)
        if selected.stat().st_size > MAX_ARCHIVE_BYTES:
            raise fail(
                "archive-too-large",
                "Evidence archive exceeds the verification limit.",
                phase="archive",
                detail={"bytes": selected.stat().st_size},
            )
        findings: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(selected, mode="r") as archive:
                infos = archive.infolist()
                self._verify_member_headers(infos, findings)
                manifest = self._read_manifest(archive)
                self._verify_manifest(
                    archive,
                    manifest,
                    expected_commit=expected_commit,
                    findings=findings,
                )
        except zipfile.BadZipFile as error:
            raise fail(
                "archive-format-invalid",
                "Evidence archive is not a valid ZIP.",
                phase="archive",
                detail={"path": str(selected)},
            ) from error
        require_no_blockers(
            findings,
            code="archive-verification-failed",
            message="Evidence archive failed tamper verification.",
            phase="archive",
        )
        receipt = {
            "schema": "zyra.first-stage-evidence-archive-verification/v1",
            "valid": True,
            "archive_sha256": file_digest(selected),
            "archive_bytes": selected.stat().st_size,
            "target_commit": manifest["target_commit"],
            "manifest_digest": manifest["manifest_digest"],
            "chain_root": manifest["chain_root"],
            "member_count": manifest["member_count"],
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _verify_member_headers(
        self,
        infos: Sequence[zipfile.ZipInfo],
        findings: list[dict[str, Any]],
    ) -> None:
        if len(infos) > MAX_MEMBER_COUNT + 1:
            findings.append(
                blocker(
                    "archive-member-count-exceeded",
                    "Evidence archive exceeds its member count limit.",
                    observed=len(infos),
                )
            )
        seen: set[str] = set()
        for info in infos:
            try:
                path = safe_relative_path(info.filename, "ZIP member path")
            except Exception as error:
                findings.append(
                    blocker(
                        "archive-member-path-invalid",
                        "Evidence archive contains an unsafe member path.",
                        path=info.filename,
                        error=str(error),
                    )
                )
                continue
            if path in seen:
                findings.append(
                    blocker(
                        "archive-member-path-duplicate",
                        "Evidence archive contains duplicate member paths.",
                        path=path,
                    )
                )
            seen.add(path)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                findings.append(
                    blocker(
                        "archive-member-symlink-forbidden",
                        "Evidence archive contains a symbolic link.",
                        path=path,
                    )
                )
            if info.is_dir():
                findings.append(
                    blocker(
                        "archive-directory-entry-forbidden",
                        "Evidence archive contains an unnecessary directory entry.",
                        path=path,
                    )
                )
            if info.file_size > MAX_MEMBER_BYTES:
                findings.append(
                    blocker(
                        "archive-member-too-large",
                        "Evidence archive member exceeds its size limit.",
                        path=path,
                        bytes=info.file_size,
                    )
                )
            if info.compress_size == 0 and info.file_size > 0:
                findings.append(
                    blocker(
                        "archive-member-compression-invalid",
                        "Evidence archive member has invalid compression metadata.",
                        path=path,
                    )
                )
            elif info.compress_size:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_COMPRESSION_RATIO:
                    findings.append(
                        blocker(
                            "archive-member-compression-ratio-exceeded",
                            "Evidence archive member exceeds compression ratio limit.",
                            path=path,
                            ratio=ratio,
                        )
                    )

    @staticmethod
    def _read_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
        try:
            payload = archive.read(MANIFEST_PATH)
        except KeyError as error:
            raise fail(
                "archive-manifest-missing",
                "Evidence archive manifest is missing.",
                phase="archive",
            ) from error
        if len(payload) > 64 * 1024 * 1024:
            raise fail(
                "archive-manifest-too-large",
                "Evidence archive manifest exceeds its limit.",
                phase="archive",
            )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise fail(
                "archive-manifest-invalid",
                "Evidence archive manifest is not valid UTF-8 JSON.",
                phase="archive",
            ) from error
        return require_mapping(value, "archive manifest")

    def _verify_manifest(
        self,
        archive: zipfile.ZipFile,
        manifest: Mapping[str, Any],
        *,
        expected_commit: str | None,
        findings: list[dict[str, Any]],
    ) -> None:
        if manifest.get("schema") != "zyra.first-stage-evidence-archive/v1":
            findings.append(
                blocker(
                    "archive-manifest-schema-mismatch",
                    "Evidence archive manifest schema is unsupported.",
                    schema=manifest.get("schema"),
                )
            )
        target_commit = require_commit(
            manifest.get("target_commit"),
            "archive target commit",
        )
        if expected_commit and target_commit != require_commit(
            expected_commit,
            "expected archive commit",
        ):
            findings.append(
                blocker(
                    "archive-target-commit-mismatch",
                    "Evidence archive target commit does not match.",
                    expected=expected_commit,
                    observed=target_commit,
                )
            )
        projection = dict(manifest)
        declared_manifest_digest = projection.pop("manifest_digest", "")
        observed_manifest_digest = digest(projection)
        if declared_manifest_digest != observed_manifest_digest:
            findings.append(
                blocker(
                    "archive-manifest-digest-mismatch",
                    "Evidence archive manifest digest is invalid.",
                    declared=declared_manifest_digest,
                    observed=observed_manifest_digest,
                )
            )
        entries = [
            require_mapping(item, "archive manifest entry")
            for item in require_sequence(
                manifest.get("entries"),
                "archive manifest entries",
            )
        ]
        if manifest.get("member_count") != len(entries):
            findings.append(
                blocker(
                    "archive-manifest-count-mismatch",
                    "Archive manifest member count is inconsistent.",
                    declared=manifest.get("member_count"),
                    observed=len(entries),
                )
            )
        expected_names = {MANIFEST_PATH}
        previous = ZERO_DIGEST
        total = 0
        for entry in entries:
            path = safe_relative_path(entry.get("path"), "archive entry path")
            expected_names.add(path)
            try:
                info = archive.getinfo(path)
                payload = archive.read(path)
            except KeyError:
                findings.append(
                    blocker(
                        "archive-member-missing",
                        "Manifest member is missing from the archive.",
                        path=path,
                    )
                )
                continue
            observed_digest = bytes_digest(payload)
            observed_size = len(payload)
            total += observed_size
            if observed_digest != entry.get("sha256"):
                findings.append(
                    blocker(
                        "archive-member-digest-mismatch",
                        "Archive member content digest is invalid.",
                        path=path,
                        declared=entry.get("sha256"),
                        observed=observed_digest,
                    )
                )
            if observed_size != entry.get("bytes") or info.file_size != observed_size:
                findings.append(
                    blocker(
                        "archive-member-size-mismatch",
                        "Archive member size is inconsistent.",
                        path=path,
                        declared=entry.get("bytes"),
                        observed=observed_size,
                    )
                )
            if entry.get("previous_digest") != previous:
                findings.append(
                    blocker(
                        "archive-chain-predecessor-mismatch",
                        "Archive hash chain predecessor is invalid.",
                        path=path,
                    )
                )
            current = chain_digest(
                path=path,
                member_digest=observed_digest,
                member_bytes=observed_size,
                kind=str(entry.get("kind") or ""),
                previous_digest=previous,
            )
            if entry.get("chain_digest") != current:
                findings.append(
                    blocker(
                        "archive-chain-digest-mismatch",
                        "Archive hash chain digest is invalid.",
                        path=path,
                    )
                )
            previous = current
        observed_names = {info.filename for info in archive.infolist()}
        if observed_names != expected_names:
            findings.append(
                blocker(
                    "archive-member-set-mismatch",
                    "Archive contains unmanifested or missing members.",
                    missing=sorted(expected_names - observed_names),
                    unexpected=sorted(observed_names - expected_names),
                )
            )
        if manifest.get("total_member_bytes") != total:
            findings.append(
                blocker(
                    "archive-total-bytes-mismatch",
                    "Archive total member size is inconsistent.",
                    declared=manifest.get("total_member_bytes"),
                    observed=total,
                )
            )
        if manifest.get("chain_root") != previous:
            findings.append(
                blocker(
                    "archive-chain-root-mismatch",
                    "Archive hash-chain root is invalid.",
                    declared=manifest.get("chain_root"),
                    observed=previous,
                )
            )


def verify_archive(
    archive_path: str | Path,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    return EvidenceArchiveVerifier().verify(
        archive_path,
        expected_commit=expected_commit,
    )
