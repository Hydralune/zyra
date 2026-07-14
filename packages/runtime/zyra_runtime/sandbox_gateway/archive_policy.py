from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .canonical import canonical_logical_path, content_digest, digest
from .constants import (
    DEFAULT_MAX_ARCHIVE_ENTRIES,
    DEFAULT_MAX_ARCHIVE_EXPANDED_BYTES,
    DEFAULT_MAX_ARCHIVE_RATIO,
)
from .errors import GatewayErrorCode, SandboxGatewayError


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    logical_path: str
    content: bytes
    compressed_bytes: int
    expanded_bytes: int
    mode: int
    content_digest: str
    source_index: int
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = {
            "logical_path": self.logical_path,
            "compressed_bytes": self.compressed_bytes,
            "expanded_bytes": self.expanded_bytes,
            "mode": self.mode,
            "content_digest": self.content_digest,
            "source_index": self.source_index,
            "metadata": dict(self.metadata),
        }
        if include_content:
            value["content_hex"] = self.content.hex()
        return value


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    format: str
    archive_digest: str
    entries: tuple[ArchiveEntry, ...]
    compressed_bytes: int
    expanded_bytes: int
    rejected: tuple[str, ...]
    policy_digest: str

    @property
    def ok(self) -> bool:
        return not self.rejected

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "archive_digest": self.archive_digest,
            "entries": [item.to_dict() for item in self.entries],
            "compressed_bytes": self.compressed_bytes,
            "expanded_bytes": self.expanded_bytes,
            "rejected": list(self.rejected),
            "policy_digest": self.policy_digest,
            "ok": self.ok,
        }


class ArchivePolicy:
    def __init__(
        self,
        *,
        maximum_entries: int = DEFAULT_MAX_ARCHIVE_ENTRIES,
        maximum_expanded_bytes: int = DEFAULT_MAX_ARCHIVE_EXPANDED_BYTES,
        maximum_ratio: float = DEFAULT_MAX_ARCHIVE_RATIO,
        allow_links: bool = False,
        allow_devices: bool = False,
        allow_encrypted: bool = False,
    ) -> None:
        self.maximum_entries = int(maximum_entries)
        self.maximum_expanded_bytes = int(maximum_expanded_bytes)
        self.maximum_ratio = float(maximum_ratio)
        self.allow_links = bool(allow_links)
        self.allow_devices = bool(allow_devices)
        self.allow_encrypted = bool(allow_encrypted)
        self.policy_digest = digest(self.descriptor(include_digest=False))

    def inspect(self, content: bytes, *, filename: str = "") -> ArchiveInspection:
        if zipfile.is_zipfile(io.BytesIO(content)):
            inspection = self._inspect_zip(content)
        else:
            try:
                inspection = self._inspect_tar(content)
            except tarfile.TarError as error:
                raise SandboxGatewayError(
                    GatewayErrorCode.ARCHIVE_INVALID,
                    f"unsupported or corrupt archive: {type(error).__name__}",
                    operation="inspect_archive",
                ) from error
        self.require_safe(inspection)
        return inspection

    def require_safe(self, inspection: ArchiveInspection) -> ArchiveInspection:
        if inspection.rejected:
            code = (
                GatewayErrorCode.ARCHIVE_TRAVERSAL
                if any("path" in item or "link" in item for item in inspection.rejected)
                else GatewayErrorCode.ARCHIVE_BOMB
                if any("limit" in item or "ratio" in item for item in inspection.rejected)
                else GatewayErrorCode.ARCHIVE_INVALID
            )
            raise SandboxGatewayError(
                code,
                "archive failed bounded expansion policy",
                operation="inspect_archive",
                metadata=inspection.to_dict(),
            )
        return inspection

    def _inspect_zip(self, content: bytes) -> ArchiveInspection:
        entries: list[ArchiveEntry] = []
        rejected: list[str] = []
        expanded = 0
        with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
            members = archive.infolist()
            if len(members) > self.maximum_entries:
                rejected.append("entry_limit")
            for index, member in enumerate(members[: self.maximum_entries + 1]):
                if member.is_dir():
                    continue
                path = self._safe_path(member.filename, rejected)
                unix_mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(unix_mode) and not self.allow_links:
                    rejected.append(f"link_rejected:{index}")
                    continue
                if member.flag_bits & 0x1 and not self.allow_encrypted:
                    rejected.append(f"encrypted_rejected:{index}")
                    continue
                expanded += member.file_size
                if expanded > self.maximum_expanded_bytes:
                    rejected.append("expanded_byte_limit")
                    break
                ratio = member.file_size / max(1, member.compress_size)
                if ratio > self.maximum_ratio:
                    rejected.append(f"compression_ratio:{index}")
                    continue
                data = archive.read(member)
                if len(data) != member.file_size:
                    rejected.append(f"size_mismatch:{index}")
                    continue
                entries.append(
                    ArchiveEntry(
                        logical_path=path,
                        content=data,
                        compressed_bytes=member.compress_size,
                        expanded_bytes=len(data),
                        mode=unix_mode,
                        content_digest=content_digest(data),
                        source_index=index,
                        metadata={"crc": member.CRC},
                    )
                )
        return ArchiveInspection(
            format="zip",
            archive_digest=content_digest(content),
            entries=tuple(entries),
            compressed_bytes=len(content),
            expanded_bytes=expanded,
            rejected=tuple(sorted(set(rejected))),
            policy_digest=self.policy_digest,
        )

    def _inspect_tar(self, content: bytes) -> ArchiveInspection:
        entries: list[ArchiveEntry] = []
        rejected: list[str] = []
        expanded = 0
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > self.maximum_entries:
                rejected.append("entry_limit")
            for index, member in enumerate(members[: self.maximum_entries + 1]):
                if member.isdir():
                    continue
                path = self._safe_path(member.name, rejected)
                if (member.issym() or member.islnk()) and not self.allow_links:
                    rejected.append(f"link_rejected:{index}")
                    continue
                if (member.isdev() or member.isfifo()) and not self.allow_devices:
                    rejected.append(f"device_rejected:{index}")
                    continue
                if not member.isfile():
                    rejected.append(f"unsupported_entry:{index}")
                    continue
                expanded += member.size
                if expanded > self.maximum_expanded_bytes:
                    rejected.append("expanded_byte_limit")
                    break
                extracted = archive.extractfile(member)
                if extracted is None:
                    rejected.append(f"missing_content:{index}")
                    continue
                data = extracted.read(self.maximum_expanded_bytes + 1)
                if len(data) != member.size:
                    rejected.append(f"size_mismatch:{index}")
                    continue
                entries.append(
                    ArchiveEntry(
                        logical_path=path,
                        content=data,
                        compressed_bytes=0,
                        expanded_bytes=len(data),
                        mode=member.mode,
                        content_digest=content_digest(data),
                        source_index=index,
                        metadata={"mtime": member.mtime},
                    )
                )
        ratio = expanded / max(1, len(content))
        if ratio > self.maximum_ratio:
            rejected.append("archive_ratio_limit")
        return ArchiveInspection(
            format="tar",
            archive_digest=content_digest(content),
            entries=tuple(entries),
            compressed_bytes=len(content),
            expanded_bytes=expanded,
            rejected=tuple(sorted(set(rejected))),
            policy_digest=self.policy_digest,
        )

    @staticmethod
    def _safe_path(value: str, rejected: list[str]) -> str:
        try:
            path = canonical_logical_path(value)
        except SandboxGatewayError:
            rejected.append(f"path_traversal:{content_digest(value)}")
            return f"rejected/{len(rejected)}"
        if PurePosixPath(path).is_absolute():
            rejected.append(f"absolute_path:{content_digest(value)}")
        return path

    def descriptor(self, *, include_digest: bool = True) -> Mapping[str, Any]:
        value: dict[str, Any] = {
            "maximum_entries": self.maximum_entries,
            "maximum_expanded_bytes": self.maximum_expanded_bytes,
            "maximum_ratio": self.maximum_ratio,
            "allow_links": self.allow_links,
            "allow_devices": self.allow_devices,
            "allow_encrypted": self.allow_encrypted,
            "writes_directly": False,
        }
        if include_digest:
            value["policy_digest"] = self.policy_digest
        return value
