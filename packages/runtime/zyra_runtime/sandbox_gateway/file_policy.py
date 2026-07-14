from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .canonical import canonical_logical_path, content_digest, digest
from .constants import (
    ARCHIVE_SUFFIXES,
    DEFAULT_MAX_ARTIFACT_BYTES,
    EXECUTABLE_SUFFIXES,
    POLICY_CONTROL_PATH_NAMES,
)
from .models import FileArtifactRequest
from .provenance import ProvenancePolicy, ProvenancePolicyDecision

_SCRIPT_SHEBANG = re.compile(br"^#!\s*(?:/usr/bin/env\s+)?(?:ba|z|fi|k)?sh\b", re.IGNORECASE)
_POWERSHELL = re.compile(br"(?i)\b(?:param\s*\(|invoke-expression|start-process)\b")
_BINARY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "application/vnd.microsoft.portable-executable"),
    (b"\x7fELF", "application/x-elf"),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-binary"),
    (b"\xfe\xed\xfa\xcf", "application/x-mach-binary"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


@dataclass(frozen=True, slots=True)
class FilePolicyFinding:
    code: str
    severity: str
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FilePolicyDecision:
    allowed: bool
    quarantine: bool
    logical_path: str
    content_digest: str
    detected_content_type: str
    executable: bool
    archive: bool
    control_target: bool
    findings: tuple[FilePolicyFinding, ...]
    provenance: ProvenancePolicyDecision
    policy_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "quarantine": self.quarantine,
            "logical_path": self.logical_path,
            "content_digest": self.content_digest,
            "detected_content_type": self.detected_content_type,
            "executable": self.executable,
            "archive": self.archive,
            "control_target": self.control_target,
            "findings": [item.to_dict() for item in self.findings],
            "provenance": self.provenance.to_dict(),
            "policy_digest": self.policy_digest,
        }


@dataclass(frozen=True, slots=True)
class FilePolicyConfig:
    maximum_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES
    allowed_content_types: frozenset[str] = frozenset()
    denied_content_types: frozenset[str] = frozenset(
        {
            "application/vnd.microsoft.portable-executable",
            "application/x-elf",
            "application/x-mach-binary",
        }
    )
    allow_binary: bool = True
    allow_executable: bool = False
    allow_archive: bool = True
    quarantine_content_type_mismatch: bool = True
    quarantine_unknown_binary: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "maximum_bytes": self.maximum_bytes,
            "allowed_content_types": sorted(self.allowed_content_types),
            "denied_content_types": sorted(self.denied_content_types),
            "allow_binary": self.allow_binary,
            "allow_executable": self.allow_executable,
            "allow_archive": self.allow_archive,
            "quarantine_content_type_mismatch": self.quarantine_content_type_mismatch,
            "quarantine_unknown_binary": self.quarantine_unknown_binary,
        }


class GatewayFilePolicy:
    def __init__(
        self,
        config: FilePolicyConfig | None = None,
        *,
        provenance_policy: ProvenancePolicy | None = None,
    ) -> None:
        self.config = config or FilePolicyConfig()
        self.provenance_policy = provenance_policy or ProvenancePolicy()
        self.policy_digest = digest(
            {
                "file": self.config.to_dict(),
                "provenance": self.provenance_policy.policy_digest,
            }
        )

    def inspect(self, request: FileArtifactRequest) -> FilePolicyDecision:
        path = canonical_logical_path(request.logical_path)
        suffix = PurePosixPath(path).suffix.casefold()
        detected = self.detect_content_type(request.content, path)
        binary = self.is_binary(request.content)
        executable = self.is_executable(request.content, path, detected)
        archive = suffix in ARCHIVE_SUFFIXES or detected in {
            "application/zip",
            "application/gzip",
            "application/x-tar",
            "application/x-7z-compressed",
        }
        control_target = self.is_control_target(path)
        findings: list[FilePolicyFinding] = []
        if len(request.content) > self.config.maximum_bytes:
            findings.append(
                FilePolicyFinding(
                    code="file.maximum_bytes",
                    severity="deny",
                    reason="artifact exceeds the file byte budget",
                    metadata={"bytes": len(request.content), "maximum": self.config.maximum_bytes},
                )
            )
        if detected in self.config.denied_content_types:
            findings.append(
                FilePolicyFinding(
                    code="file.content_type_denied",
                    severity="deny",
                    reason="detected content type is denied",
                    metadata={"detected_content_type": detected},
                )
            )
        if self.config.allowed_content_types and detected not in self.config.allowed_content_types:
            findings.append(
                FilePolicyFinding(
                    code="file.content_type_not_allowed",
                    severity="deny",
                    reason="detected content type is not allowlisted",
                    metadata={"detected_content_type": detected},
                )
            )
        declared = request.content_type.split(";", 1)[0].strip().casefold()
        if (
            declared
            and declared not in {"application/octet-stream", detected}
            and self.config.quarantine_content_type_mismatch
        ):
            findings.append(
                FilePolicyFinding(
                    code="file.content_type_mismatch",
                    severity="quarantine",
                    reason="declared and detected content types differ",
                    metadata={"declared": declared, "detected": detected},
                )
            )
        if binary and not self.config.allow_binary:
            findings.append(
                FilePolicyFinding(
                    code="file.binary_denied",
                    severity="deny",
                    reason="binary artifacts are disabled",
                )
            )
        if (
            binary
            and detected == "application/octet-stream"
            and self.config.quarantine_unknown_binary
        ):
            findings.append(
                FilePolicyFinding(
                    code="file.unknown_binary",
                    severity="quarantine",
                    reason="unknown binary content requires quarantine",
                )
            )
        if executable and not (self.config.allow_executable and request.executable_allowed):
            findings.append(
                FilePolicyFinding(
                    code="file.executable_denied",
                    severity="quarantine",
                    reason="active executable content requires a dedicated release workflow",
                )
            )
        if archive and not (self.config.allow_archive and request.archive_expansion_allowed):
            findings.append(
                FilePolicyFinding(
                    code="file.archive_not_released",
                    severity="quarantine",
                    reason="archive content requires explicit bounded expansion",
                )
            )
        provenance = self.provenance_policy.inspect_write(
            request.provenance,
            path,
            executable=executable,
            archive=archive,
            claims_policy_authority=bool(request.metadata.get("claims_policy_authority")),
        )
        if not provenance.allowed:
            findings.extend(
                FilePolicyFinding(
                    code=f"provenance.{code}",
                    severity="quarantine",
                    reason=provenance.reason,
                )
                for code in provenance.reason_codes
            )
        deny = any(item.severity == "deny" for item in findings)
        quarantine = provenance.quarantine or any(
            item.severity == "quarantine" for item in findings
        )
        return FilePolicyDecision(
            allowed=not deny and not quarantine,
            quarantine=quarantine,
            logical_path=path,
            content_digest=content_digest(request.content),
            detected_content_type=detected,
            executable=executable,
            archive=archive,
            control_target=control_target,
            findings=tuple(findings),
            provenance=provenance,
            policy_digest=self.policy_digest,
        )

    @staticmethod
    def detect_content_type(content: bytes, logical_path: str) -> str:
        for magic, content_type in _BINARY_MAGIC:
            if content.startswith(magic):
                return content_type
        guessed, _ = mimetypes.guess_type(logical_path)
        if guessed:
            return guessed.casefold()
        if not GatewayFilePolicy.is_binary(content):
            return "text/plain"
        return "application/octet-stream"

    @staticmethod
    def is_binary(content: bytes) -> bool:
        sample = content[:8192]
        if b"\x00" in sample:
            return True
        if not sample:
            return False
        controls = sum(
            1
            for value in sample
            if value < 9 or 13 < value < 32
        )
        return controls / len(sample) > 0.08

    @staticmethod
    def is_executable(content: bytes, logical_path: str, content_type: str) -> bool:
        suffix = PurePosixPath(logical_path).suffix.casefold()
        if suffix in EXECUTABLE_SUFFIXES:
            return True
        if content_type in {
            "application/vnd.microsoft.portable-executable",
            "application/x-elf",
            "application/x-mach-binary",
        }:
            return True
        sample = content[:4096]
        return bool(_SCRIPT_SHEBANG.search(sample) or _POWERSHELL.search(sample))

    @staticmethod
    def is_control_target(logical_path: str) -> bool:
        parts = {
            part.casefold()
            for part in logical_path.replace("\\", "/").split("/")
            if part
        }
        return bool(parts.intersection(POLICY_CONTROL_PATH_NAMES))

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "policy_id": "zyra.gateway-file-policy.v1",
            "policy_digest": self.policy_digest,
            "config": self.config.to_dict(),
            "provenance_policy_digest": self.provenance_policy.policy_digest,
        }
