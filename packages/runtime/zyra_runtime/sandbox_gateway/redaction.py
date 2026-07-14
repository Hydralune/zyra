from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .canonical import content_digest, token_digest
from .constants import REDACTED, SENSITIVE_KEY_FRAGMENTS

_AUTHORIZATION = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*"
    r"(bearer|basic|token)?\s*([^\s,;]+)"
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|passwd|secret|session[_-]?token)\b(\s*[:=]\s*)"
    r"([\"']?)([^\"'\s,;]+)\3"
)
_URL_CREDENTIAL = re.compile(r"(?i)(https?://)([^/@:\s]+):([^/@\s]+)@")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_TOKEN = re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{16,}\b")


@dataclass(frozen=True, slots=True)
class RedactionFinding:
    code: str
    value_digest: str
    start: int
    end: int
    key: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "value_digest": self.value_digest,
            "start": self.start,
            "end": self.end,
            "key": self.key,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class RedactionReport:
    value: Any
    findings: tuple[RedactionFinding, ...] = ()
    input_digest: str = ""
    output_digest: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "findings": [item.to_dict() for item in self.findings],
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "changed": self.changed,
            "metadata": dict(self.metadata),
        }


class SecretRedactor:
    """Redacts secrets before events, receipts, terminal output, or artifacts escape."""

    def __init__(
        self,
        *,
        known_secrets: Iterable[str] = (),
        replacement: str = REDACTED,
        entropy_threshold: float = 4.1,
        minimum_entropy_token_length: int = 28,
        maximum_depth: int = 20,
    ) -> None:
        self.replacement = replacement
        self.entropy_threshold = float(entropy_threshold)
        self.minimum_entropy_token_length = int(minimum_entropy_token_length)
        self.maximum_depth = int(maximum_depth)
        self._known = tuple(
            sorted(
                {str(item) for item in known_secrets if len(str(item)) >= 4},
                key=len,
                reverse=True,
            )
        )

    def with_secrets(self, secrets: Iterable[str]) -> "SecretRedactor":
        return SecretRedactor(
            known_secrets=(*self._known, *tuple(str(item) for item in secrets)),
            replacement=self.replacement,
            entropy_threshold=self.entropy_threshold,
            minimum_entropy_token_length=self.minimum_entropy_token_length,
            maximum_depth=self.maximum_depth,
        )

    def redact_text(self, value: str, *, source: str = "") -> RedactionReport:
        original = str(value)
        findings: list[RedactionFinding] = []
        replacements: list[tuple[int, int, str, str, str]] = []

        def capture(code: str, start: int, end: int, secret: str, key: str = "") -> None:
            if not secret:
                return
            replacements.append((start, end, code, secret, key))

        for match in _AUTHORIZATION.finditer(original):
            capture("authorization", match.start(3), match.end(3), match.group(3), match.group(1))
        for match in _ASSIGNMENT.finditer(original):
            capture("secret_assignment", match.start(4), match.end(4), match.group(4), match.group(1))
        for match in _URL_CREDENTIAL.finditer(original):
            capture("url_username", match.start(2), match.end(2), match.group(2), "username")
            capture("url_password", match.start(3), match.end(3), match.group(3), "password")
        for pattern, code in (
            (_PRIVATE_KEY, "private_key"),
            (_JWT, "jwt"),
            (_LONG_TOKEN, "known_token_shape"),
        ):
            for match in pattern.finditer(original):
                capture(code, match.start(), match.end(), match.group(0))
        for secret in self._known:
            start = 0
            while True:
                index = original.find(secret, start)
                if index < 0:
                    break
                capture("known_secret", index, index + len(secret), secret)
                start = index + len(secret)
        for start, end, token in self._entropy_candidates(original):
            capture("high_entropy_token", start, end, token)

        intervals = self._merge_intervals(replacements)
        if not intervals:
            return RedactionReport(
                value=original,
                findings=(),
                input_digest=content_digest(original),
                output_digest=content_digest(original),
                metadata={"source": source},
            )
        chunks: list[str] = []
        cursor = 0
        for start, end, codes, secret, key in intervals:
            chunks.append(original[cursor:start])
            chunks.append(self.replacement)
            findings.append(
                RedactionFinding(
                    code="+".join(sorted(codes)),
                    value_digest=token_digest(secret),
                    start=start,
                    end=end,
                    key=key,
                    source=source,
                )
            )
            cursor = end
        chunks.append(original[cursor:])
        redacted = "".join(chunks)
        return RedactionReport(
            value=redacted,
            findings=tuple(findings),
            input_digest=content_digest(original),
            output_digest=content_digest(redacted),
            metadata={"source": source},
        )

    def redact_bytes(
        self,
        value: bytes,
        *,
        source: str = "",
        encoding: str = "utf-8",
    ) -> RedactionReport:
        try:
            decoded = bytes(value).decode(encoding)
        except UnicodeDecodeError:
            return RedactionReport(
                value=bytes(value),
                findings=(),
                input_digest=content_digest(bytes(value)),
                output_digest=content_digest(bytes(value)),
                metadata={"source": source, "binary": True},
            )
        report = self.redact_text(decoded, source=source)
        encoded = str(report.value).encode(encoding)
        return RedactionReport(
            value=encoded,
            findings=report.findings,
            input_digest=content_digest(bytes(value)),
            output_digest=content_digest(encoded),
            metadata={"source": source, "binary": False},
        )

    def redact_value(self, value: Any, *, source: str = "") -> RedactionReport:
        findings: list[RedactionFinding] = []

        def walk(item: Any, path: str, depth: int, parent_key: str = "") -> Any:
            if depth > self.maximum_depth:
                report = self.redact_text(str(item), source=path)
                findings.extend(report.findings)
                return report.value
            if self._sensitive_key(parent_key):
                if item is None:
                    return None
                text = str(item)
                findings.append(
                    RedactionFinding(
                        code="sensitive_key",
                        value_digest=token_digest(text),
                        start=0,
                        end=len(text),
                        key=parent_key,
                        source=path,
                    )
                )
                return self.replacement
            if isinstance(item, str):
                report = self.redact_text(item, source=path)
                findings.extend(report.findings)
                return report.value
            if isinstance(item, bytes):
                report = self.redact_bytes(item, source=path)
                findings.extend(report.findings)
                return report.value
            if isinstance(item, Mapping):
                return {
                    str(key): walk(child, f"{path}.{key}", depth + 1, str(key))
                    for key, child in item.items()
                }
            if isinstance(item, tuple):
                return tuple(
                    walk(child, f"{path}[{index}]", depth + 1)
                    for index, child in enumerate(item)
                )
            if isinstance(item, list):
                return [
                    walk(child, f"{path}[{index}]", depth + 1)
                    for index, child in enumerate(item)
                ]
            if isinstance(item, (set, frozenset)):
                return [
                    walk(child, f"{path}[]", depth + 1)
                    for child in sorted(item, key=str)
                ]
            return item

        projected = walk(value, source or "$", 0)
        return RedactionReport(
            value=projected,
            findings=tuple(findings),
            input_digest=content_digest(repr(value)),
            output_digest=content_digest(repr(projected)),
            metadata={"source": source},
        )

    def assert_absent(self, value: Any, *, secrets: Sequence[str] | None = None) -> None:
        serialized = repr(value)
        candidates = tuple(secrets or ()) + self._known
        leaked = [secret for secret in candidates if secret and secret in serialized]
        if leaked:
            raise ValueError(
                "redaction boundary leaked known secret digests: "
                + ", ".join(token_digest(item) for item in leaked)
            )

    def _sensitive_key(self, key: str) -> bool:
        normalized = str(key).casefold().replace("-", "_")
        return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)

    def _entropy_candidates(self, value: str) -> Iterable[tuple[int, int, str]]:
        for match in re.finditer(r"[A-Za-z0-9_+/=-]{16,}", value):
            token = match.group(0)
            if len(token) < self.minimum_entropy_token_length:
                continue
            if token.isdigit() or token.isalpha() and token.islower():
                continue
            if self._entropy(token) >= self.entropy_threshold:
                yield match.start(), match.end(), token

    @staticmethod
    def _entropy(value: str) -> float:
        if not value:
            return 0.0
        counts: dict[str, int] = {}
        for character in value:
            counts[character] = counts.get(character, 0) + 1
        length = len(value)
        return -sum(
            (count / length) * math.log2(count / length)
            for count in counts.values()
        )

    @staticmethod
    def _merge_intervals(
        values: Sequence[tuple[int, int, str, str, str]],
    ) -> list[tuple[int, int, set[str], str, str]]:
        if not values:
            return []
        ordered = sorted(values, key=lambda item: (item[0], -item[1]))
        merged: list[tuple[int, int, set[str], str, str]] = []
        for start, end, code, secret, key in ordered:
            if merged and start < merged[-1][1]:
                old_start, old_end, codes, old_secret, old_key = merged[-1]
                merged[-1] = (
                    old_start,
                    max(old_end, end),
                    {*codes, code},
                    old_secret if len(old_secret) >= len(secret) else secret,
                    old_key or key,
                )
            else:
                merged.append((start, end, {code}, secret, key))
        return merged


def redact_for_event(value: Any, *, known_secrets: Iterable[str] = ()) -> Any:
    return SecretRedactor(known_secrets=known_secrets).redact_value(
        value,
        source="gateway_event",
    ).value


def redact_terminal_output(
    stdout: bytes,
    stderr: bytes,
    *,
    known_secrets: Iterable[str] = (),
) -> tuple[bytes, bytes, tuple[RedactionFinding, ...]]:
    redactor = SecretRedactor(known_secrets=known_secrets)
    out = redactor.redact_bytes(stdout, source="stdout")
    err = redactor.redact_bytes(stderr, source="stderr")
    return bytes(out.value), bytes(err.value), (*out.findings, *err.findings)
