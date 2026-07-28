"""Fail-closed helpers shared by final-freeze product runtimes.

This module deliberately keeps final-freeze validation independent from the
builders that create submission artifacts.  A verifier can read bytes, apply
contracts, and recompute digests without trusting a generation receipt or a
builder's cached success flag.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from zyra_evaluation.freeze_reporting.canonical import (
    atomic_write,
    canonical_bytes,
    canonical_text,
    digest,
    file_digest,
    normalize,
    resolve_inside,
    safe_relative_path,
)


SCHEMA_TOKEN = re.compile(r"^[a-z][a-z0-9._/-]{2,127}$")
IDENTITY_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
COMMIT_TOKEN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_TOKEN = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
EMAIL_TOKEN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
SECRET_KEY_TOKEN = re.compile(
    r"(?:secret|password|passwd|token|api[_-]?key|credential|authorization)",
    re.IGNORECASE,
)
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


class FinalFreezeError(ValueError):
    """Typed final-freeze failure with safe structured detail."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "validation",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = _safe_code(code)
        self.message = str(message)
        self.phase = _safe_code(phase)
        self.detail = redact(detail or {})
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "phase": self.phase,
            "detail": normalize(self.detail),
        }


def fail(
    code: str,
    message: str,
    *,
    phase: str = "validation",
    **detail: Any,
) -> NoReturn:
    raise FinalFreezeError(code, message, phase=phase, detail=detail)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any, label: str) -> datetime:
    text = require_text(value, label)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        fail(
            "invalid-timestamp",
            f"{label} is not an ISO-8601 timestamp",
            label=label,
            value=text,
            error=str(error),
        )
    if parsed.tzinfo is None:
        fail(
            "naive-timestamp",
            f"{label} must include a timezone",
            label=label,
            value=text,
        )
    return parsed.astimezone(UTC)


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        fail(
            "mapping-required",
            f"{label} must be an object",
            label=label,
            actual_type=type(value).__name__,
        )
    return {str(key): item for key, item in value.items()}


def require_sequence(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value,
        Sequence,
    ):
        fail(
            "sequence-required",
            f"{label} must be an array",
            label=label,
            actual_type=type(value).__name__,
        )
    items = list(value)
    required_minimum = 0 if allow_empty else 1
    if minimum is not None:
        required_minimum = minimum
    if len(items) < required_minimum:
        fail(
            "empty-sequence",
            f"{label} contains too few items",
            label=label,
            minimum=required_minimum,
            actual=len(items),
        )
    if maximum is not None and len(items) > maximum:
        fail(
            "sequence-too-long",
            f"{label} contains too many items",
            label=label,
            maximum=maximum,
            actual=len(items),
        )
    return items


def require_text(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum: int = 4096,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        fail(
            "text-required",
            f"{label} must be text",
            label=label,
            actual_type=type(value).__name__,
        )
    text = value.strip()
    if allow_empty:
        minimum = 0
    if len(text) < minimum:
        fail(
            "text-too-short",
            f"{label} is too short",
            label=label,
            minimum=minimum,
            actual=len(text),
        )
    if len(text) > maximum:
        fail(
            "text-too-long",
            f"{label} is too long",
            label=label,
            maximum=maximum,
            actual=len(text),
        )
    if "\x00" in text:
        fail(
            "text-contains-nul",
            f"{label} contains a NUL character",
            label=label,
        )
    return text


def require_identity(value: Any, label: str) -> str:
    text = require_text(value, label, maximum=256)
    if not IDENTITY_TOKEN.fullmatch(text):
        fail(
            "invalid-identity",
            f"{label} has an invalid identity token",
            label=label,
            value=text,
        )
    return text


def require_schema(
    value: Any,
    label: str,
    *,
    expected: str | None = None,
) -> str:
    text = require_text(value, label, maximum=128)
    if not SCHEMA_TOKEN.fullmatch(text):
        fail(
            "invalid-schema",
            f"{label} has an invalid schema identifier",
            label=label,
            value=text,
        )
    if expected is not None and text != expected:
        fail(
            "schema-mismatch",
            f"{label} does not match the required schema",
            label=label,
            expected=expected,
            actual=text,
        )
    return text


def require_commit(value: Any, label: str = "commit") -> str:
    text = require_text(value, label, maximum=40).lower()
    if not COMMIT_TOKEN.fullmatch(text):
        fail(
            "invalid-commit",
            f"{label} must be a full lowercase Git commit",
            label=label,
            value=text,
        )
    return text


def require_digest(value: Any, label: str = "digest") -> str:
    text = require_text(value, label, maximum=71).lower()
    if not DIGEST_TOKEN.fullmatch(text):
        fail(
            "invalid-digest",
            f"{label} must be a SHA-256 digest",
            label=label,
            value=text,
        )
    return text.removeprefix("sha256:")


def require_integer(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            "integer-required",
            f"{label} must be an integer",
            label=label,
            actual_type=type(value).__name__,
        )
    if minimum is not None and value < minimum:
        fail(
            "integer-below-minimum",
            f"{label} is below its minimum",
            label=label,
            minimum=minimum,
            actual=value,
        )
    if maximum is not None and value > maximum:
        fail(
            "integer-above-maximum",
            f"{label} is above its maximum",
            label=label,
            maximum=maximum,
            actual=value,
        )
    return value


def require_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(
            "number-required",
            f"{label} must be numeric",
            label=label,
            actual_type=type(value).__name__,
        )
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        fail(
            "finite-number-required",
            f"{label} must be finite",
            label=label,
            actual=str(value),
        )
    if minimum is not None and number < minimum:
        fail(
            "number-below-minimum",
            f"{label} is below its minimum",
            label=label,
            minimum=minimum,
            actual=number,
        )
    if maximum is not None and number > maximum:
        fail(
            "number-above-maximum",
            f"{label} is above its maximum",
            label=label,
            maximum=maximum,
            actual=number,
        )
    return number


def require_boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        fail(
            "boolean-required",
            f"{label} must be a boolean",
            label=label,
            actual_type=type(value).__name__,
        )
    return value


def require_choice(
    value: Any,
    label: str,
    allowed: Iterable[str],
) -> str:
    text = require_text(value, label, maximum=128)
    choices = tuple(dict.fromkeys(str(item) for item in allowed))
    if text not in choices:
        fail(
            "invalid-choice",
            f"{label} is not an allowed value",
            label=label,
            value=text,
            allowed=list(choices),
        )
    return text


def require_email(value: Any, label: str = "email") -> str:
    text = require_text(value, label, maximum=320)
    if not EMAIL_TOKEN.fullmatch(text):
        fail(
            "invalid-email",
            f"{label} is not a valid delivery address",
            label=label,
        )
    return text


def load_json(
    path: str | Path,
    *,
    label: str | None = None,
    maximum_bytes: int = 256 * 1024 * 1024,
) -> dict[str, Any]:
    target = Path(path)
    try:
        stat = target.stat()
    except OSError as error:
        fail(
            "input-missing",
            "required JSON input is unavailable",
            phase="input",
            path=str(target),
            error=str(error),
        )
    if not target.is_file():
        fail(
            "input-not-file",
            "required JSON input is not a regular file",
            phase="input",
            path=str(target),
        )
    if stat.st_size > maximum_bytes:
        fail(
            "input-too-large",
            "JSON input exceeds the configured limit",
            phase="input",
            path=str(target),
            maximum_bytes=maximum_bytes,
            actual_bytes=stat.st_size,
        )
    try:
        with target.open("r", encoding="utf-8", newline="") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(
            "input-json-invalid",
            "required JSON input could not be decoded",
            phase="input",
            path=str(target),
            error=str(error),
        )
    return require_mapping(value, label or str(target))


def write_json(path: str | Path, value: Any) -> str:
    payload = canonical_text(value, pretty=True).encode("utf-8")
    atomic_write(path, payload)
    return hashlib.sha256(payload).hexdigest()


def verify_file(
    root: str | Path,
    relative: Any,
    expected_digest: Any,
    *,
    label: str,
    maximum_bytes: int | None = None,
) -> Path:
    path = resolve_inside(root, safe_relative_path(relative, label))
    if not path.is_file():
        fail(
            "member-not-file",
            f"{label} is not a regular file",
            path=str(path),
        )
    if maximum_bytes is not None:
        size = path.stat().st_size
        if size > maximum_bytes:
            fail(
                "member-too-large",
                f"{label} exceeds the configured limit",
                path=str(path),
                maximum_bytes=maximum_bytes,
                actual_bytes=size,
            )
    actual = file_digest(path)
    expected = require_digest(expected_digest, f"{label}.sha256")
    if not secrets.compare_digest(actual, expected):
        fail(
            "member-digest-mismatch",
            f"{label} does not match its checksum",
            path=str(path),
            expected=expected,
            actual=actual,
        )
    return path


def verify_object_digest(
    value: Mapping[str, Any],
    *,
    digest_key: str,
    label: str,
) -> str:
    document = dict(value)
    expected = require_digest(document.pop(digest_key, None), f"{label}.{digest_key}")
    actual = digest(document)
    if not secrets.compare_digest(expected, actual):
        fail(
            "object-digest-mismatch",
            f"{label} does not match its embedded digest",
            expected=expected,
            actual=actual,
        )
    return actual


def object_with_digest(
    value: Mapping[str, Any],
    *,
    digest_key: str = "digest",
) -> dict[str, Any]:
    document = normalize(dict(value))
    if digest_key in document:
        fail(
            "digest-key-present",
            "caller supplied the reserved digest field",
            digest_key=digest_key,
        )
    document[digest_key] = digest(document)
    return document


def stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def redact(value: Any, *, key: str = "") -> Any:
    if key and SECRET_KEY_TOKEN.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _redact_text(value: str) -> str:
    text = value
    patterns = (
        re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
        re.compile(r"(?i)((?:api[_-]?key|password|secret|token)\s*[=:]\s*)[^\s,;]+"),
        re.compile(r"(?i)(https?://[^:/\s]+:)[^@\s]+@"),
    )
    for pattern in patterns:
        text = pattern.sub(r"\1<redacted>", text)
    return text


def contains_absolute_path(value: str) -> bool:
    if WINDOWS_DRIVE.match(value):
        return True
    return value.startswith("/") or value.startswith("\\\\")


def temporary_directory(
    root: str | Path,
    *,
    prefix: str,
) -> tempfile.TemporaryDirectory[str]:
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=directory)


@dataclass(slots=True)
class Finding:
    code: str
    message: str
    severity: str = "blocker"
    category: str = "validation"
    evidence: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        severity = require_choice(
            self.severity,
            "finding.severity",
            ("blocker", "warning", "observation"),
        )
        return {
            "code": _safe_code(self.code),
            "message": require_text(
                self.message,
                "finding.message",
                maximum=4096,
            ),
            "severity": severity,
            "category": _safe_code(self.category),
            "evidence": stable_unique(
                safe_relative_path(path, "finding.evidence")
                for path in self.evidence
            ),
            "detail": normalize(redact(self.detail)),
        }


@dataclass(slots=True)
class FindingLedger:
    """Deterministic issue collector used by builders and verifiers."""

    findings: list[Finding] = field(default_factory=list)

    def blocker(
        self,
        code: str,
        message: str,
        *,
        category: str = "validation",
        evidence: Iterable[str] = (),
        **detail: Any,
    ) -> None:
        self.add(
            Finding(
                code=code,
                message=message,
                severity="blocker",
                category=category,
                evidence=list(evidence),
                detail=detail,
            )
        )

    def warning(
        self,
        code: str,
        message: str,
        *,
        category: str = "validation",
        evidence: Iterable[str] = (),
        **detail: Any,
    ) -> None:
        self.add(
            Finding(
                code=code,
                message=message,
                severity="warning",
                category=category,
                evidence=list(evidence),
                detail=detail,
            )
        )

    def observation(
        self,
        code: str,
        message: str,
        *,
        category: str = "validation",
        evidence: Iterable[str] = (),
        **detail: Any,
    ) -> None:
        self.add(
            Finding(
                code=code,
                message=message,
                severity="observation",
                category=category,
                evidence=list(evidence),
                detail=detail,
            )
        )

    def add(self, finding: Finding) -> None:
        item = finding.to_dict()
        identity = (
            item["severity"],
            item["category"],
            item["code"],
            item["message"],
            tuple(item["evidence"]),
            digest(item["detail"]),
        )
        for existing in self.findings:
            other = existing.to_dict()
            other_identity = (
                other["severity"],
                other["category"],
                other["code"],
                other["message"],
                tuple(other["evidence"]),
                digest(other["detail"]),
            )
            if other_identity == identity:
                return
        self.findings.append(finding)

    def extend(self, findings: Iterable[Finding]) -> None:
        for finding in findings:
            self.add(finding)

    @property
    def blockers(self) -> list[dict[str, Any]]:
        return [
            finding.to_dict()
            for finding in self.findings
            if finding.severity == "blocker"
        ]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [
            finding.to_dict()
            for finding in self.findings
            if finding.severity == "warning"
        ]

    @property
    def observations(self) -> list[dict[str, Any]]:
        return [
            finding.to_dict()
            for finding in self.findings
            if finding.severity == "observation"
        ]

    @property
    def valid(self) -> bool:
        return not self.blockers

    def require_valid(self, *, phase: str) -> None:
        if self.valid:
            return
        fail(
            "blocking-findings",
            "blocking findings prevent final freeze",
            phase=phase,
            blocker_count=len(self.blockers),
            blockers=self.blockers,
        )

    def to_dict(self) -> dict[str, Any]:
        items = [finding.to_dict() for finding in self.findings]
        severity_order = {"blocker": 0, "warning": 1, "observation": 2}
        items.sort(
            key=lambda item: (
                severity_order[item["severity"]],
                item["category"],
                item["code"],
                item["message"],
            )
        )
        return {
            "valid": self.valid,
            "counts": {
                "blocker": len(self.blockers),
                "warning": len(self.warnings),
                "observation": len(self.observations),
                "total": len(items),
            },
            "findings": items,
            "digest": digest(items),
        }


def _safe_code(value: str) -> str:
    text = str(value).strip().lower().replace("_", "-").replace(" ", "-")
    text = re.sub(r"[^a-z0-9.-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    return text or "unknown"


def ensure_new_directory(path: str | Path) -> Path:
    target = Path(path)
    if target.exists():
        fail(
            "output-exists",
            "refusing to overwrite an existing final-freeze output",
            phase="output",
            path=str(target),
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.mkdir()
    except OSError as error:
        fail(
            "output-create-failed",
            "could not create final-freeze output directory",
            phase="output",
            path=str(target),
            error=str(error),
        )
    return target


def environment_snapshot(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = dict(environment or os.environ)
    presence = {}
    for key in (
        "CI",
        "ZYRA_RELEASE_MODE",
        "ZYRA_SEALED_POLICY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
    ):
        presence[key] = bool(values.get(key))
    return {
        "presence": presence,
        "credential_values_recorded": False,
        "digest": digest(presence),
    }
