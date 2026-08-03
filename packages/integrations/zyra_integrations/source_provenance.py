from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


PROVENANCE_SCHEMA = "zyra.source-provenance/v1"
PROVENANCE_ROOT_NAME = "provenance"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class SourceProvenanceError(RuntimeError):
    pass


def provenance_root(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / PROVENANCE_ROOT_NAME


def source_workspace_root(project_root: str | Path) -> Path:
    return provenance_root(project_root)


@dataclass(frozen=True, slots=True)
class ProvenanceFile:
    path: str
    sha256: str
    size: int
    category: str
    source_repository: str = ""
    source_path: str = ""


class BundledSourceProvenance:
    """Verify and resolve the immutable source evidence shipped with Zyra."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = provenance_root(self.project_root)
        self.manifest_path = self.root / "manifest.json"
        self._manifest: Mapping[str, Any] | None = None
        self._records: tuple[ProvenanceFile, ...] | None = None
        self._source_identities: frozenset[tuple[str, str]] | None = None

    @property
    def manifest(self) -> Mapping[str, Any]:
        if self._manifest is None:
            try:
                value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise SourceProvenanceError(
                    f"source provenance manifest is unreadable: {self.manifest_path}: {error}"
                ) from error
            if not isinstance(value, Mapping):
                raise SourceProvenanceError("source provenance manifest root must be an object")
            if value.get("schema") != PROVENANCE_SCHEMA:
                raise SourceProvenanceError(
                    f"unsupported source provenance schema: {value.get('schema')!r}"
                )
            self._manifest = value
        return self._manifest

    @property
    def records(self) -> tuple[ProvenanceFile, ...]:
        if self._records is None:
            raw = self.manifest.get("files")
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise SourceProvenanceError("source provenance files must be an array")
            records: list[ProvenanceFile] = []
            seen: set[str] = set()
            for index, item in enumerate(raw):
                if not isinstance(item, Mapping):
                    raise SourceProvenanceError(
                        f"source provenance file record {index} must be an object"
                    )
                path = self._safe_relative(str(item.get("path") or ""))
                if path in seen:
                    raise SourceProvenanceError(f"duplicate source provenance path: {path}")
                seen.add(path)
                sha256 = str(item.get("sha256") or "")
                size = item.get("size")
                category = str(item.get("category") or "")
                if _SHA256.fullmatch(sha256) is None:
                    raise SourceProvenanceError(f"invalid source provenance digest: {path}")
                if not isinstance(size, int) or size < 0:
                    raise SourceProvenanceError(f"invalid source provenance size: {path}")
                if not category:
                    raise SourceProvenanceError(f"missing source provenance category: {path}")
                records.append(
                    ProvenanceFile(
                        path=path,
                        sha256=sha256,
                        size=size,
                        category=category,
                        source_repository=str(item.get("source_repository") or ""),
                        source_path=str(item.get("source_path") or ""),
                    )
                )
            self._records = tuple(records)
        return self._records

    def verify(self) -> dict[str, Any]:
        repositories = self.manifest.get("repositories")
        if not isinstance(repositories, Mapping) or not repositories:
            raise SourceProvenanceError("source provenance repository identities are missing")
        for name, item in repositories.items():
            if not isinstance(item, Mapping):
                raise SourceProvenanceError(f"invalid repository identity: {name}")
            commit = str(item.get("commit") or "")
            if _COMMIT.fullmatch(commit) is None:
                raise SourceProvenanceError(f"invalid repository commit: {name}")

        verified: list[str] = []
        expected = {record.path for record in self.records}
        for record in self.records:
            path = self.root.joinpath(*PurePosixPath(record.path).parts)
            if not path.is_file() or path.is_symlink():
                raise SourceProvenanceError(f"source provenance file is missing: {record.path}")
            payload = path.read_bytes()
            actual = hashlib.sha256(payload).hexdigest()
            if len(payload) != record.size or actual != record.sha256:
                raise SourceProvenanceError(
                    f"source provenance file changed: {record.path}"
                )
            verified.append(record.path)

        actual = {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and path != self.manifest_path
        }
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        if extra or missing:
            raise SourceProvenanceError(
                f"source provenance path set changed: extra={extra[:20]}, missing={missing[:20]}"
            )
        identity_indexes = self._verified_identity_indexes()
        return {
            "schema": "zyra.source-provenance-verification/v1",
            "ready": True,
            "manifest": self.manifest_path.relative_to(self.project_root).as_posix(),
            "file_count": len(verified),
            "repository_count": len(repositories),
            "source_file_count": sum(
                record.category == "repository_source" for record in self.records
            ),
            "identity_index_count": len(identity_indexes),
            "source_identity_count": len(self._load_source_identities()),
        }

    def source_identity(self, repository: str, source_path: str) -> dict[str, Any] | None:
        normalized = self._safe_relative(source_path)
        for record in self.records:
            if (
                record.category == "repository_source"
                and record.source_repository == repository
                and record.source_path == normalized
            ):
                return {
                    "verified": True,
                    "method": "bundled_source_file",
                    "repository": repository,
                    "source_path": normalized,
                    "provenance_path": record.path,
                }
        if (repository, normalized) in self._load_source_identities():
            return {
                "verified": True,
                "method": "frozen_ledger_identity",
                "repository": repository,
                "source_path": normalized,
                "provenance_path": self._ledger_identity_path(),
            }
        return None

    def source_file(self, repository: str, source_path: str) -> Path:
        normalized = self._safe_relative(source_path)
        matches = [
            record
            for record in self.records
            if record.category == "repository_source"
            and record.source_repository == repository
            and record.source_path == normalized
        ]
        if len(matches) != 1:
            raise SourceProvenanceError(
                f"source provenance identity is missing or ambiguous: {repository}:{normalized}"
            )
        record = matches[0]
        path = self.root.joinpath(*PurePosixPath(record.path).parts)
        payload = path.read_bytes()
        if len(payload) != record.size or hashlib.sha256(payload).hexdigest() != record.sha256:
            raise SourceProvenanceError(
                f"source provenance file changed: {repository}:{normalized}"
            )
        return path

    def _verified_identity_indexes(self) -> tuple[Mapping[str, Any], ...]:
        raw = self.manifest.get("identity_indexes")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise SourceProvenanceError("source provenance identity indexes must be an array")
        indexes: list[Mapping[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise SourceProvenanceError(
                    f"source provenance identity index {index} must be an object"
                )
            path_value = self._safe_relative(str(item.get("path") or ""))
            digest = str(item.get("sha256") or "")
            kind = str(item.get("kind") or "")
            normalization = str(item.get("normalization") or "binary")
            if (
                _SHA256.fullmatch(digest) is None
                or not kind
                or normalization not in {"binary", "utf8_lf"}
            ):
                raise SourceProvenanceError(
                    f"invalid source provenance identity index: {path_value}"
                )
            path = self.project_root.joinpath(*PurePosixPath(path_value).parts)
            try:
                path.relative_to(self.project_root)
            except ValueError as error:
                raise SourceProvenanceError(
                    f"source provenance identity index escapes project: {path_value}"
                ) from error
            if not path.is_file() or path.is_symlink():
                raise SourceProvenanceError(
                    f"source provenance identity index is missing: {path_value}"
                )
            payload = path.read_bytes()
            if normalization == "utf8_lf":
                try:
                    payload = (
                        payload.decode("utf-8")
                        .replace("\r\n", "\n")
                        .replace("\r", "\n")
                        .encode("utf-8")
                    )
                except UnicodeError as error:
                    raise SourceProvenanceError(
                        f"source provenance identity index is not UTF-8: {path_value}"
                    ) from error
            actual = hashlib.sha256(payload).hexdigest()
            if actual != digest:
                raise SourceProvenanceError(
                    f"source provenance identity index changed: {path_value}"
                )
            indexes.append(item)
        return tuple(indexes)

    def _ledger_identity_path(self) -> str:
        for item in self._verified_identity_indexes():
            if item.get("kind") == "internalization_ledger_source_evidence":
                return self._safe_relative(str(item.get("path") or ""))
        raise SourceProvenanceError(
            "internalization ledger source identity index is missing"
        )

    def _load_source_identities(self) -> frozenset[tuple[str, str]]:
        if self._source_identities is None:
            relative = self._ledger_identity_path()
            path = self.project_root.joinpath(*PurePosixPath(relative).parts)
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise SourceProvenanceError(
                    f"source identity index is unreadable: {relative}: {error}"
                ) from error
            entries = document.get("entries") if isinstance(document, Mapping) else None
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                raise SourceProvenanceError(
                    "internalization ledger source identity entries must be an array"
                )
            identities: set[tuple[str, str]] = set()
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                evidence = entry.get("source_evidence")
                if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
                    evidence = ()
                for item in evidence:
                    if not isinstance(item, Mapping) or item.get("exists_in_workspace") is not True:
                        continue
                    repository = str(item.get("source_repo") or entry.get("source_repo") or "")
                    source_path = str(item.get("source_path") or entry.get("source_path") or "")
                    if not repository or not source_path:
                        continue
                    identities.add((repository, self._safe_relative(source_path)))
            self._source_identities = frozenset(identities)
        return self._source_identities

    @staticmethod
    def _safe_relative(value: str) -> str:
        normalized = value.replace("\\", "/").strip("/")
        pure = PurePosixPath(normalized)
        if not normalized or pure.is_absolute() or ".." in pure.parts:
            raise SourceProvenanceError(f"unsafe source provenance path: {value!r}")
        return pure.as_posix()


__all__ = [
    "BundledSourceProvenance",
    "PROVENANCE_ROOT_NAME",
    "PROVENANCE_SCHEMA",
    "ProvenanceFile",
    "SourceProvenanceError",
    "provenance_root",
    "source_workspace_root",
]
