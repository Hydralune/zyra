from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import (
    ArtifactDeclaration,
    ArtifactReceipt,
    ContractError,
    canonical_json,
    stable_digest,
    validate_relative_path,
)


class ArtifactSecurityError(ContractError):
    pass


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    relative_path: str
    size_bytes: int
    digest: str
    mode: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    case_id: str
    receipts: tuple[ArtifactReceipt, ...]
    undeclared_paths: tuple[str, ...]
    missing_required: tuple[str, ...]
    symlink_paths: tuple[str, ...]
    canary_hits: tuple[str, ...]
    digest: str

    @property
    def valid(self) -> bool:
        return not (
            self.undeclared_paths
            or self.missing_required
            or self.symlink_paths
            or self.canary_hits
        )

    def require_valid(self) -> None:
        if not self.valid:
            raise ArtifactSecurityError(
                "artifact manifest is invalid: "
                f"undeclared={list(self.undeclared_paths)}, "
                f"missing={list(self.missing_required)}, "
                f"symlinks={list(self.symlink_paths)}, "
                f"canary_hits={list(self.canary_hits)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "valid": self.valid,
            "receipts": [item.to_dict() for item in self.receipts],
            "undeclared_paths": list(self.undeclared_paths),
            "missing_required": list(self.missing_required),
            "symlink_paths": list(self.symlink_paths),
            "canary_hits": list(self.canary_hits),
            "digest": self.digest,
        }


class SecureArtifactStore:
    """Case-confined, atomic and canary-scanned artifact writer."""

    def __init__(
        self,
        root: str | Path,
        case_id: str,
        declarations: Iterable[ArtifactDeclaration],
        *,
        secret_canaries: Iterable[str] = (),
    ) -> None:
        self.root = Path(root).resolve(strict=False)
        self.case_id = str(case_id)
        self.case_root = (self.root / self.case_id).resolve(strict=False)
        if self.case_root == self.root or not self.case_root.is_relative_to(self.root):
            raise ArtifactSecurityError("invalid case artifact root")
        self.case_root.mkdir(parents=True, exist_ok=True)
        self._declarations = {item.name: item for item in declarations}
        self._paths = {
            item.relative_path: item
            for item in self._declarations.values()
        }
        if len(self._paths) != len(self._declarations):
            raise ArtifactSecurityError("artifact declarations contain duplicate paths")
        self._canaries = tuple(
            sorted(
                {str(value) for value in secret_canaries if len(str(value)) >= 4},
                key=len,
                reverse=True,
            )
        )
        self._written: set[str] = set()

    def write_json(
        self,
        name: str,
        value: Any,
        *,
        redact: bool = True,
    ) -> ArtifactReceipt:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n"
        ).encode("utf-8")
        return self.write_bytes(name, encoded, redact=redact)

    def write_text(
        self,
        name: str,
        value: str,
        *,
        redact: bool = True,
    ) -> ArtifactReceipt:
        return self.write_bytes(name, str(value).encode("utf-8"), redact=redact)

    def write_bytes(
        self,
        name: str,
        value: bytes,
        *,
        redact: bool = True,
    ) -> ArtifactReceipt:
        declaration = self._require_declaration(name)
        if name in self._written:
            raise ArtifactSecurityError(f"artifact {name} was already written")
        encoded = bytes(value)
        if len(encoded) > declaration.maximum_bytes:
            raise ArtifactSecurityError(
                f"artifact {name} exceeds {declaration.maximum_bytes} bytes"
            )
        was_redacted = False
        if redact:
            encoded, was_redacted = self._redact(encoded, source=name)
        hits = self._canary_hits(encoded)
        if hits:
            raise ArtifactSecurityError(
                f"artifact {name} contains secret canaries: {list(hits)}"
            )
        destination = self._destination(declaration.relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if destination.exists() and destination.is_symlink():
                raise ArtifactSecurityError(
                    f"artifact destination became a symlink: {destination}"
                )
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self._written.add(name)
        return self._receipt(declaration, destination, was_redacted)

    def admit_existing(self, name: str) -> ArtifactReceipt:
        declaration = self._require_declaration(name)
        destination = self._destination(declaration.relative_path)
        if not destination.is_file() or destination.is_symlink():
            raise ArtifactSecurityError(f"existing artifact is missing or unsafe: {name}")
        if destination.stat().st_size > declaration.maximum_bytes:
            raise ArtifactSecurityError(f"existing artifact exceeds size budget: {name}")
        content = destination.read_bytes()
        hits = self._canary_hits(content)
        if hits:
            raise ArtifactSecurityError(
                f"existing artifact {name} contains secret canaries: {list(hits)}"
            )
        self._written.add(name)
        return self._receipt(declaration, destination, False)

    def finalize(self) -> ArtifactManifest:
        fingerprints, symlinks = self._scan_files()
        actual_paths = {item.relative_path for item in fingerprints}
        undeclared = tuple(sorted(actual_paths - set(self._paths)))
        missing = tuple(
            sorted(
                item.name
                for item in self._declarations.values()
                if item.required and item.relative_path not in actual_paths
            )
        )
        canary_hits: set[str] = set()
        receipts: list[ArtifactReceipt] = []
        for declaration in sorted(
            self._declarations.values(),
            key=lambda item: item.name,
        ):
            path = self._destination(declaration.relative_path)
            if not path.is_file() or path.is_symlink():
                continue
            content = path.read_bytes()
            for canary in self._canary_hits(content):
                canary_hits.add(stable_digest("canary", canary))
            receipts.append(
                self._receipt(
                    declaration,
                    path,
                    b"<redacted>" in content,
                )
            )
        material = {
            "case_id": self.case_id,
            "receipts": [item.to_dict() for item in receipts],
            "undeclared_paths": undeclared,
            "missing_required": missing,
            "symlink_paths": symlinks,
            "canary_hits": sorted(canary_hits),
        }
        return ArtifactManifest(
            case_id=self.case_id,
            receipts=tuple(receipts),
            undeclared_paths=undeclared,
            missing_required=missing,
            symlink_paths=symlinks,
            canary_hits=tuple(sorted(canary_hits)),
            digest=stable_digest(material),
        )

    def fingerprint(self) -> tuple[FileFingerprint, ...]:
        return self._scan_files()[0]

    def _require_declaration(self, name: str) -> ArtifactDeclaration:
        try:
            return self._declarations[name]
        except KeyError as error:
            raise ArtifactSecurityError(f"undeclared artifact name: {name}") from error

    def _destination(self, relative_path: str) -> Path:
        normalized = validate_relative_path(
            relative_path,
            field_name="artifact relative path",
        )
        candidate = self.case_root.joinpath(*Path(normalized).parts)
        resolved_parent = candidate.parent.resolve(strict=False)
        if not resolved_parent.is_relative_to(self.case_root):
            raise ArtifactSecurityError(
                f"artifact path escapes case root: {relative_path}"
            )
        cursor = self.case_root
        for part in Path(normalized).parts[:-1]:
            cursor = cursor / part
            if cursor.exists() and cursor.is_symlink():
                raise ArtifactSecurityError(
                    f"artifact path traverses a symlink: {relative_path}"
                )
        return candidate

    def _scan_files(self) -> tuple[tuple[FileFingerprint, ...], tuple[str, ...]]:
        fingerprints: list[FileFingerprint] = []
        symlinks: list[str] = []
        if not self.case_root.exists():
            return (), ()
        for path in sorted(self.case_root.rglob("*")):
            relative = path.relative_to(self.case_root).as_posix()
            if path.is_symlink():
                symlinks.append(relative)
                continue
            if not path.is_file():
                continue
            stat = path.stat()
            fingerprints.append(
                FileFingerprint(
                    relative_path=relative,
                    size_bytes=stat.st_size,
                    digest=self._file_digest(path),
                    mode=stat.st_mode,
                )
            )
        return tuple(fingerprints), tuple(symlinks)

    def _receipt(
        self,
        declaration: ArtifactDeclaration,
        path: Path,
        redacted: bool,
    ) -> ArtifactReceipt:
        stat = path.stat()
        return ArtifactReceipt(
            name=declaration.name,
            relative_path=declaration.relative_path,
            media_type=declaration.media_type,
            size_bytes=stat.st_size,
            digest=self._file_digest(path),
            redacted=redacted,
        )

    def _redact(self, value: bytes, *, source: str) -> tuple[bytes, bool]:
        try:
            from zyra_runtime.sandbox_gateway.redaction import SecretRedactor
        except ImportError as error:
            if self._canary_hits(value):
                raise ArtifactSecurityError(
                    "SecretRedactor is unavailable while a canary is present"
                ) from error
            return value, False
        report = SecretRedactor(known_secrets=self._canaries).redact_bytes(
            value,
            source=f"m3-regression:{source}",
        )
        redacted = report.value
        if not isinstance(redacted, bytes):
            raise ArtifactSecurityError("SecretRedactor returned non-byte output")
        return redacted, bool(report.changed)

    def _canary_hits(self, value: bytes) -> tuple[str, ...]:
        hits: list[str] = []
        for canary in self._canaries:
            if canary.encode("utf-8") in value:
                hits.append(canary)
        return tuple(hits)

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()


def verify_manifest_bytes(
    payload: bytes,
    *,
    expected_digest: str,
    secret_canaries: Iterable[str] = (),
) -> Mapping[str, Any]:
    actual_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual_digest != expected_digest:
        raise ArtifactSecurityError(
            f"manifest byte digest mismatch: expected {expected_digest}, got {actual_digest}"
        )
    for canary in secret_canaries:
        if str(canary).encode("utf-8") in payload:
            raise ArtifactSecurityError(
                f"manifest contains secret canary {stable_digest('canary', canary)}"
            )
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise ArtifactSecurityError("manifest must contain a JSON object")
    recoded = canonical_json(decoded).encode("utf-8")
    if not recoded:
        raise ArtifactSecurityError("manifest canonical encoding is empty")
    return decoded


__all__ = [
    "ArtifactManifest",
    "ArtifactSecurityError",
    "FileFingerprint",
    "SecureArtifactStore",
    "verify_manifest_bytes",
]
