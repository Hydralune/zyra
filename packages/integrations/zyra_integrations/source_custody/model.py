from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Self


class SourceRole(StrEnum):
    PRIMARY = "primary_implementation"
    SUPPLEMENTARY = "supplementary_implementation"
    CONFORMANCE = "conformance_only"
    REFERENCE = "reference_only"
    EXPERIMENTAL = "experimental"
    DEFERRED = "deferred"
    REJECTED = "rejected"

    @property
    def active_capable(self) -> bool:
        return self in {self.PRIMARY, self.SUPPLEMENTARY}

    @property
    def inactive_only(self) -> bool:
        return not self.active_capable


class LandingStatus(StrEnum):
    INTERNALIZED = "internalized"
    ADAPTER = "adapter"
    EXTERNALIZED = "externalized"
    DEBT = "debt"
    CONFORMANCE = "conformance"
    REFERENCE = "reference"
    EXPERIMENTAL = "experimental"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    NOT_APPLICABLE = "not_applicable"

    @property
    def production_reachable(self) -> bool:
        return self in {self.INTERNALIZED, self.ADAPTER, self.EXTERNALIZED, self.DEBT}


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"

    @property
    def rank(self) -> int:
        return {
            self.INFO: 0,
            self.WARNING: 1,
            self.ERROR: 2,
            self.BLOCKER: 3,
        }[self]


class Disposition(StrEnum):
    ACCEPT = "accept"
    TRACK = "track"
    REMOVE = "remove"
    ABSORB = "absorb"
    EXTERNALIZE = "externalize"
    DECLARE = "declare"
    BLOCK_RELEASE = "block_release"


class EvidenceKind(StrEnum):
    SOURCE = "source"
    MANIFEST = "manifest"
    LOCKFILE = "lockfile"
    CONFIG = "config"
    PROCESS = "process"
    PACKAGE_ANNOTATION = "package_annotation"
    NOTICE = "notice"
    VENDOR_MAP = "vendor_map"
    LEDGER = "ledger"
    BINARY = "binary"
    ARCHIVE = "archive"
    GENERATED = "generated"
    SYMLINK = "symlink"
    TEST = "test"


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_LANGUAGE = re.compile(r"^[a-z][a-z0-9+#.-]{0,31}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/+()#-]{1,255}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:/")
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def stable_digest(value: Any, *, prefix: str = "sha256") -> str:
    encoded = stable_json(value).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def content_digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _json_default(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def text(value: Any, *, field_name: str, allow_empty: bool = False) -> str:
    if value is None:
        candidate = ""
    elif isinstance(value, str):
        candidate = value.strip()
    else:
        raise ValueError(f"{field_name} must be a string")
    if _CONTROL.search(candidate):
        raise ValueError(f"{field_name} contains a control character")
    if not candidate and not allow_empty:
        raise ValueError(f"{field_name} is required")
    return candidate


def identity(value: Any, *, field_name: str) -> str:
    candidate = text(value, field_name=field_name)
    if not _IDENTITY.fullmatch(candidate):
        raise ValueError(f"{field_name} is not a stable identity: {candidate!r}")
    return candidate


def owner(value: Any) -> str:
    candidate = text(value, field_name="owner")
    if not _OWNER.fullmatch(candidate):
        raise ValueError(f"owner has unsupported characters: {candidate!r}")
    return candidate


def commit(value: Any, *, allow_none: bool = False) -> str:
    candidate = text(value, field_name="source_commit", allow_empty=allow_none)
    if not candidate and allow_none:
        return ""
    if not _COMMIT.fullmatch(candidate.casefold()):
        raise ValueError("source_commit must be a 7-64 character hexadecimal revision")
    return candidate.casefold()


def language_set(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw = list(value)
    else:
        raise ValueError("source_languages must be an array of language names")
    result: list[str] = []
    for item in raw:
        candidate = text(item, field_name="source_language").casefold()
        if candidate in {"mixed", "unknown", "none", "n/a"}:
            raise ValueError(f"source language {candidate!r} is forbidden")
        if not _LANGUAGE.fullmatch(candidate):
            raise ValueError(f"source language is invalid: {candidate!r}")
        if candidate not in result:
            result.append(candidate)
    if not result:
        raise ValueError("source_languages cannot be empty")
    return tuple(sorted(result))


def normalize_repo_path(value: Any, *, allow_empty: bool = False) -> str:
    candidate = text(value, field_name="repository path", allow_empty=allow_empty)
    if not candidate and allow_empty:
        return ""
    normalized = candidate.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if _DRIVE.match(normalized) or normalized.startswith("/") or _URL.match(normalized):
        raise ValueError(f"repository path must be relative: {candidate!r}")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"repository path is not normalized: {candidate!r}")
    return path.as_posix()


def normalize_paths(value: Any, *, field_name: str, allow_empty: bool) -> tuple[str, ...]:
    if value is None and allow_empty:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field_name} must be an array")
    result: list[str] = []
    for item in value:
        normalized = normalize_repo_path(item)
        if normalized not in result:
            result.append(normalized)
    if not result and not allow_empty:
        raise ValueError(f"{field_name} cannot be empty")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class RuleSwitches:
    catalog: bool = True
    roles: bool = True
    dependencies: bool = True
    processes: bool = True
    custody: bool = True
    langgraph: bool = True
    opaque: bool = True
    source_specific: bool = True

    def disabled(self, name: str) -> Self:
        if name not in self.__dataclass_fields__:
            raise ValueError(f"unknown rule group: {name}")
        return replace(self, **{name: False})

    def enabled_names(self) -> tuple[str, ...]:
        return tuple(
            name for name in self.__dataclass_fields__ if getattr(self, name)
        )


@dataclass(frozen=True, slots=True)
class RuntimeEntry:
    module: str
    symbol: str
    command: str
    protocol: str

    @classmethod
    def parse(cls, raw: Any, *, active: bool) -> Self:
        if not isinstance(raw, Mapping):
            if active:
                raise ValueError("active source entry requires runtime_entry")
            raw = {}
        module = text(raw.get("module"), field_name="runtime_entry.module", allow_empty=True)
        symbol = text(raw.get("symbol"), field_name="runtime_entry.symbol", allow_empty=True)
        command_value = text(
            raw.get("command"), field_name="runtime_entry.command", allow_empty=True
        )
        protocol = text(
            raw.get("protocol"), field_name="runtime_entry.protocol", allow_empty=True
        )
        if active and not (module or command_value):
            raise ValueError("active source entry needs a runtime module or command")
        if module.startswith("../") or "\\..\\" in module:
            raise ValueError("runtime module cannot reference a parent repository")
        return cls(module=module, symbol=symbol, command=command_value, protocol=protocol)

    def to_dict(self) -> dict[str, str]:
        return {
            "module": self.module,
            "symbol": self.symbol,
            "command": self.command,
            "protocol": self.protocol,
        }


@dataclass(frozen=True, slots=True)
class SourceEntry:
    entry_id: str
    source_repo: str
    source_commit: str
    capability: str
    role: SourceRole
    status: str
    source_languages: tuple[str, ...]
    target_languages: tuple[str, ...]
    migration_mode: str
    landing_status: LandingStatus
    target_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    owner: str
    runtime_entry: RuntimeEntry
    reason: str
    license_id: str
    license_status: str
    source_paths: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def primary(self) -> bool:
        return self.role is SourceRole.PRIMARY

    @property
    def supplementary(self) -> bool:
        return self.role is SourceRole.SUPPLEMENTARY

    @property
    def source_key(self) -> str:
        return self.source_repo.casefold()

    @classmethod
    def parse(cls, raw: Any) -> Self:
        if not isinstance(raw, Mapping):
            raise ValueError("source entry must be an object")
        role = _enum(SourceRole, raw.get("role"), "role")
        status = text(raw.get("status"), field_name="status").casefold()
        if status not in {"active", "inactive"}:
            raise ValueError("status must be active or inactive")
        active = status == "active"
        landing = _enum(LandingStatus, raw.get("landing_status"), "landing_status")
        source_repo = identity(raw.get("source_repo"), field_name="source_repo")
        if source_repo.casefold() == "openclaw":
            raise ValueError("OpenClaw cannot receive a forward source-role entry")
        target_paths = normalize_paths(
            raw.get("target_paths"), field_name="target_paths", allow_empty=not active
        )
        test_paths = normalize_paths(
            raw.get("test_paths"), field_name="test_paths", allow_empty=not active
        )
        source_paths = normalize_paths(
            raw.get("source_paths"), field_name="source_paths", allow_empty=True
        )
        evidence_paths = normalize_paths(
            raw.get("evidence_paths"), field_name="evidence_paths", allow_empty=True
        )
        target_languages = language_set(raw.get("target_languages"))
        if not active and target_languages != ("audit-only",):
            if landing in {
                LandingStatus.REFERENCE,
                LandingStatus.CONFORMANCE,
                LandingStatus.DEFERRED,
                LandingStatus.REJECTED,
                LandingStatus.NOT_APPLICABLE,
            }:
                target_languages = tuple(target_languages)
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata must be an object")
        result = cls(
            entry_id=identity(raw.get("entry_id"), field_name="entry_id"),
            source_repo=source_repo,
            source_commit=commit(raw.get("source_commit")),
            capability=identity(raw.get("capability"), field_name="capability"),
            role=role,
            status=status,
            source_languages=language_set(raw.get("source_languages")),
            target_languages=target_languages,
            migration_mode=identity(raw.get("migration_mode"), field_name="migration_mode"),
            landing_status=landing,
            target_paths=target_paths,
            test_paths=test_paths,
            owner=owner(raw.get("owner")),
            runtime_entry=RuntimeEntry.parse(raw.get("runtime_entry"), active=active),
            reason=text(raw.get("reason"), field_name="reason"),
            license_id=text(raw.get("license_id"), field_name="license_id"),
            license_status=identity(
                raw.get("license_status"), field_name="license_status"
            ),
            source_paths=source_paths,
            evidence_paths=evidence_paths,
            metadata=dict(metadata),
        )
        result.validate_semantics()
        return result

    def validate_semantics(self) -> None:
        if self.active and not self.role.active_capable:
            raise ValueError("inactive-only role cannot have active status")
        if not self.active and self.role.active_capable:
            raise ValueError("implementation roles must have active status")
        if self.active and not self.landing_status.production_reachable:
            raise ValueError("active role must have a production landing status")
        if not self.active and self.landing_status.production_reachable:
            raise ValueError("inactive role cannot claim a production landing")
        if self.role is SourceRole.EXPERIMENTAL and self.landing_status is not LandingStatus.EXPERIMENTAL:
            raise ValueError("experimental role requires experimental landing")
        if self.role is SourceRole.REJECTED and self.landing_status is not LandingStatus.REJECTED:
            raise ValueError("rejected role requires rejected landing")
        if len(self.reason) < 24:
            raise ValueError("reason must describe the bounded source decision")
        if self.active and self.migration_mode in {
            "reference_only",
            "conformance_only",
            "audit_only_no_runtime_migration",
        }:
            raise ValueError("active entry uses an inactive migration mode")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "source_repo": self.source_repo,
            "source_commit": self.source_commit,
            "capability": self.capability,
            "role": self.role.value,
            "status": self.status,
            "source_languages": list(self.source_languages),
            "target_languages": list(self.target_languages),
            "migration_mode": self.migration_mode,
            "landing_status": self.landing_status.value,
            "source_paths": list(self.source_paths),
            "target_paths": list(self.target_paths),
            "test_paths": list(self.test_paths),
            "evidence_paths": list(self.evidence_paths),
            "owner": self.owner,
            "runtime_entry": self.runtime_entry.to_dict(),
            "reason": self.reason,
            "license_id": self.license_id,
            "license_status": self.license_status,
            "metadata": dict(self.metadata),
        }


def _enum(enum: type[StrEnum], value: Any, field_name: str) -> Any:
    candidate = text(value, field_name=field_name).casefold()
    try:
        return enum(candidate)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum)
        raise ValueError(f"{field_name} must be one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    path: str
    line: int = 0
    symbol: str = ""
    excerpt_digest: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind.value, "path": self.path}
        if self.line:
            result["line"] = self.line
        if self.symbol:
            result["symbol"] = self.symbol
        if self.excerpt_digest:
            result["excerpt_digest"] = self.excerpt_digest
        if self.attributes:
            result["attributes"] = dict(self.attributes)
        return result


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    message: str
    rule_group: str
    severity: Severity
    capability: str = ""
    source_repo: str = ""
    path: str = ""
    line: int = 0
    owner_unit: str = "M3-01B"
    disposition: Disposition = Disposition.TRACK
    default_path_impact: str = ""
    remediation: str = ""
    evidence: tuple[Evidence, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        material = {
            "code": self.code,
            "capability": self.capability,
            "source_repo": self.source_repo.casefold(),
            "path": self.path,
            "line": self.line,
            "attributes": dict(self.attributes),
        }
        return stable_digest(material).split(":", 1)[1][:24]

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCKER or self.disposition is Disposition.BLOCK_RELEASE

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "code": self.code,
            "message": self.message,
            "rule_group": self.rule_group,
            "severity": self.severity.value,
            "blocking": self.blocking,
            "capability": self.capability,
            "source_repo": self.source_repo,
            "path": self.path,
            "line": self.line,
            "owner_unit": self.owner_unit,
            "disposition": self.disposition.value,
            "default_path_impact": self.default_path_impact,
            "remediation": self.remediation,
            "evidence": [item.to_dict() for item in self.evidence],
            "attributes": dict(self.attributes),
        }


def finding(
    code: str,
    message: str,
    rule_group: str,
    *,
    severity: Severity = Severity.ERROR,
    **kwargs: Any,
) -> Finding:
    return Finding(
        code=identity(code, field_name="finding code"),
        message=text(message, field_name="finding message"),
        rule_group=identity(rule_group, field_name="rule group"),
        severity=severity,
        **kwargs,
    )


def deduplicate_findings(items: Iterable[Finding]) -> tuple[Finding, ...]:
    selected: dict[str, Finding] = {}
    for item in items:
        prior = selected.get(item.fingerprint)
        if prior is None or item.severity.rank > prior.severity.rank:
            selected[item.fingerprint] = item
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (
                -item.severity.rank,
                item.code,
                item.path,
                item.line,
                item.fingerprint,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class AuditSection:
    name: str
    metrics: Mapping[str, Any]
    findings: tuple[Finding, ...]
    evidence: tuple[Evidence, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(item.blocking for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "valid": self.valid,
            "metrics": dict(self.metrics),
            "findings": [item.to_dict() for item in self.findings],
            "evidence": [item.to_dict() for item in self.evidence],
        }


def section(
    name: str,
    *,
    metrics: Mapping[str, Any] | None = None,
    findings: Iterable[Finding] = (),
    evidence: Iterable[Evidence] = (),
) -> AuditSection:
    return AuditSection(
        name=identity(name, field_name="section name"),
        metrics=dict(metrics or {}),
        findings=deduplicate_findings(findings),
        evidence=tuple(evidence),
    )


def resolve_within(root: Path, relative: str) -> Path:
    base = root.resolve(strict=False)
    candidate = (base / normalize_repo_path(relative)).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path escapes project root: {relative}") from exc
    return candidate


def relative_path(root: Path, path: Path) -> str:
    base = root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return resolved.as_posix()
