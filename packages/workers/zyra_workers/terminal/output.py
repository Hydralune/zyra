from __future__ import annotations

import hashlib
import re
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .models import TerminalError, TerminalSpill


_CREDENTIAL_PATTERNS = (
    re.compile(
        rb"\b(?:sk|pk|rk|ghp|github_pat|glpat|xox[baprs])[-_A-Za-z0-9]{12,}\b",
        re.IGNORECASE,
    ),
    re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(
        rb"\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|credential)"
        rb"\s*[:=]\s*[\"']?[^\s\"',;]{4,}",
        re.IGNORECASE,
    ),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
)
_OPEN_CREDENTIAL_PATTERNS = (
    re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/=-]*\Z", re.IGNORECASE),
    re.compile(
        rb"\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|credential)"
        rb"\s*[:=]\s*[\"']?[^\s\"',;]*\Z",
        re.IGNORECASE,
    ),
    re.compile(
        rb"\b(?:sk|pk|rk|ghp|github_pat|glpat|xox[baprs])[-_A-Za-z0-9]*\Z",
        re.IGNORECASE,
    ),
    re.compile(rb"\bAKIA[A-Z0-9]*\Z"),
)
_CREDENTIAL_PREFIXES = (
    b"bearer ",
    b"api_key",
    b"api-key",
    b"apikey",
    b"access_token",
    b"access-token",
    b"secret",
    b"password",
    b"passwd",
    b"credential",
    b"github_pat",
    b"glpat",
    b"ghp",
    b"xoxb",
    b"xoxa",
    b"xoxp",
    b"xoxr",
    b"xoxs",
    b"akia",
)


@dataclass(frozen=True, slots=True)
class OutputChunk:
    sequence: int
    first_cursor: int
    next_cursor: int
    text: str
    byte_length: int
    sha256: str
    redacted: bool

    def to_frame(self, binding: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "output",
            "protocol": "zyra.terminal.v1",
            "binding": binding,
            "first_cursor": self.first_cursor,
            "next_cursor": self.next_cursor,
            "text": self.text,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "redacted": self.redacted,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class ReplayResult:
    requested_cursor: int
    accepted_cursor: int
    earliest_cursor: int
    current_cursor: int
    reason: str
    chunks: tuple[OutputChunk, ...]
    spills: tuple[TerminalSpill, ...]

    @property
    def resync(self) -> bool:
        return bool(self.reason)


SpillSink = Callable[[bytes, int, int, bool, bool], TerminalSpill]


class SecretRedactor:
    _MARKER = b"[REDACTED]"

    def __init__(self, secrets: Iterable[str | bytes] = ()) -> None:
        values = {
            item.encode("utf-8") if isinstance(item, str) else bytes(item)
            for item in secrets
            if len(item) >= 4
        }
        self._secrets = tuple(sorted(values, key=len, reverse=True))

    def redact(self, data: bytes) -> tuple[bytes, bool]:
        selected = data
        redacted = False
        for secret in self._secrets:
            if secret not in selected:
                continue
            selected = selected.replace(secret, self._mask(len(secret)))
            redacted = True
        for pattern in _CREDENTIAL_PATTERNS:
            replaced, count = pattern.subn(
                lambda match: self._mask(len(match.group(0))),
                selected,
            )
            if count:
                selected = replaced
                redacted = True
        if len(selected) != len(data):
            raise AssertionError("terminal redaction must preserve cursor byte length")
        return selected, redacted

    @classmethod
    def _mask(cls, length: int) -> bytes:
        if length <= 0:
            return b""
        if length < len(cls._MARKER):
            return b"*" * length
        return cls._MARKER + (b"*" * (length - len(cls._MARKER)))

    def release_prefix(self, data: bytes) -> int:
        """Return the prefix that is safe to sanitize and publish now.

        PTY reads can split a configured secret or credential assignment at
        any byte boundary.  A small suspicious suffix is retained until the
        next read (or journal close), preventing an already-published prefix
        from escaping before the redactor has enough context.
        """

        if not data:
            return 0
        keep_from = len(data)
        folded = data.lower()
        for secret in self._secrets:
            maximum = min(len(data), max(0, len(secret) - 1))
            for length in range(maximum, 0, -1):
                if data.endswith(secret[:length]):
                    keep_from = min(keep_from, len(data) - length)
                    break
        for pattern in _OPEN_CREDENTIAL_PATTERNS:
            match = pattern.search(data)
            if match is not None:
                keep_from = min(keep_from, match.start())
        for prefix in _CREDENTIAL_PREFIXES:
            maximum = min(len(folded), len(prefix) - 1)
            for length in range(maximum, 0, -1):
                if folded.endswith(prefix[:length]):
                    keep_from = min(keep_from, len(data) - length)
                    break
        return keep_from


def binary_score(data: bytes) -> float:
    if not data:
        return 0.0
    if b"\x00" in data:
        return 1.0
    suspicious = 0
    for value in data:
        if value in {7, 8, 9, 10, 12, 13, 27}:
            continue
        if value < 32 or value == 127:
            suspicious += 1
    return suspicious / len(data)


def _utf8_release_prefix(data: bytes, maximum: int) -> int:
    """Keep only a trailing, otherwise-valid incomplete UTF-8 code point."""

    selected = data[:maximum]
    try:
        selected.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        if (
            error.reason == "unexpected end of data"
            and error.end == len(selected)
        ):
            return error.start
    return maximum


def _utf8_chunk_end(data: bytes, start: int, maximum_bytes: int) -> int:
    end = min(len(data), start + maximum_bytes)
    if end >= len(data):
        return end
    selected = data[start:end]
    try:
        selected.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        if (
            error.reason == "unexpected end of data"
            and error.end == len(selected)
            and error.start > 0
        ):
            return start + error.start
    return end


def _bounded_binary_placeholder(spill: TerminalSpill, maximum_bytes: int) -> str:
    marker = (
        f"<binary:{spill.artifact_id}:"
        f"{spill.sha256[:12]}>"
    ).encode("ascii", errors="replace")
    return marker[:maximum_bytes].decode("ascii")


class TerminalOutputJournal:
    def __init__(
        self,
        *,
        maximum_memory_bytes: int = 4 * 1_024 * 1_024,
        maximum_chunk_bytes: int = 64 * 1_024,
        spill_sink: SpillSink | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        if maximum_memory_bytes < 1_024:
            raise ValueError("terminal output memory budget is too small")
        if maximum_chunk_bytes < 256 or maximum_chunk_bytes > maximum_memory_bytes:
            raise ValueError("terminal output chunk budget is invalid")
        self._maximum_memory_bytes = maximum_memory_bytes
        self._maximum_chunk_bytes = maximum_chunk_bytes
        self._spill_sink = spill_sink
        self._redactor = redactor or SecretRedactor()
        self._chunks: deque[OutputChunk] = deque()
        self._spills: list[TerminalSpill] = []
        self._memory_bytes = 0
        self._cursor = 0
        self._earliest_cursor = 0
        self._sequence = 0
        self._spilled_bytes = 0
        self._binary_bytes = 0
        self._closed = False
        self._pending = bytearray()
        self._condition = threading.Condition(threading.RLock())

    @property
    def cursor(self) -> int:
        with self._condition:
            return self._cursor

    @property
    def earliest_cursor(self) -> int:
        with self._condition:
            return self._earliest_cursor

    @property
    def spilled_bytes(self) -> int:
        with self._condition:
            return self._spilled_bytes

    @property
    def binary_bytes(self) -> int:
        with self._condition:
            return self._binary_bytes

    def append(self, data: bytes) -> tuple[OutputChunk, ...]:
        if not data:
            return ()
        added: list[OutputChunk] = []
        offset = 0
        with self._condition:
            if self._closed:
                raise TerminalError(
                    "terminal_output_closed",
                    "Terminal output journal is closed.",
                    status=410,
                )
            self._pending.extend(data)
            release = self._redactor.release_prefix(bytes(self._pending))
            release = _utf8_release_prefix(bytes(self._pending), release)
            material = bytes(self._pending[:release])
            del self._pending[:release]
            if len(self._pending) > self._maximum_memory_bytes:
                self._pending.clear()
                self._closed = True
                self._condition.notify_all()
                raise TerminalError(
                    "terminal_output_redaction_budget_exceeded",
                    "Terminal output retained an unterminated credential beyond its budget.",
                    status=413,
                )
            safe_material, _ = self._redactor.redact(material)
            while offset < len(material):
                end = _utf8_chunk_end(
                    material,
                    offset,
                    self._maximum_chunk_bytes,
                )
                raw = material[offset:end]
                safe = safe_material[offset : offset + len(raw)]
                offset = end
                added.append(
                    self._append_chunk(
                        raw,
                        redacted_bytes=safe,
                        redacted=safe != raw,
                    )
                )
            self._trim()
            self._condition.notify_all()
        return tuple(added)

    def replay(
        self,
        cursor: int,
        *,
        maximum_chunks: int = 256,
        maximum_bytes: int = 2 * 1_024 * 1_024,
    ) -> ReplayResult:
        if cursor < 0:
            raise TerminalError(
                "terminal_cursor_invalid",
                "Terminal cursor must be non-negative.",
                status=400,
            )
        with self._condition:
            reason = ""
            accepted = cursor
            if cursor < self._earliest_cursor:
                reason = "stale_cursor"
                accepted = self._earliest_cursor
            elif cursor > self._cursor:
                reason = "future_cursor"
                accepted = self._cursor
            chunks: list[OutputChunk] = []
            used = 0
            for chunk in self._chunks:
                if chunk.next_cursor <= accepted:
                    continue
                if chunk.first_cursor < accepted < chunk.next_cursor:
                    reason = reason or "spill_boundary"
                    accepted = chunk.first_cursor
                if len(chunks) >= maximum_chunks:
                    break
                if chunks and used + chunk.byte_length > maximum_bytes:
                    break
                chunks.append(chunk)
                used += chunk.byte_length
            spills = tuple(
                spill
                for spill in self._spills
                if spill.next_cursor > cursor
            )
            return ReplayResult(
                requested_cursor=cursor,
                accepted_cursor=accepted,
                earliest_cursor=self._earliest_cursor,
                current_cursor=self._cursor,
                reason=reason,
                chunks=tuple(chunks),
                spills=spills,
            )

    def wait_for_cursor(
        self,
        cursor: int,
        *,
        timeout: float,
    ) -> bool:
        with self._condition:
            if self._cursor > cursor or self._closed:
                return True
            self._condition.wait_for(
                lambda: self._cursor > cursor or self._closed,
                timeout=max(0.0, timeout),
            )
            return self._cursor > cursor or self._closed

    def close(self) -> tuple[OutputChunk, ...]:
        with self._condition:
            if self._closed:
                return ()
            added: list[OutputChunk] = []
            material = bytes(self._pending)
            self._pending.clear()
            safe_material, _ = self._redactor.redact(material)
            offset = 0
            while offset < len(material):
                end = _utf8_chunk_end(
                    material,
                    offset,
                    self._maximum_chunk_bytes,
                )
                raw = material[offset:end]
                safe = safe_material[offset : offset + len(raw)]
                offset = end
                added.append(
                    self._append_chunk(
                        raw,
                        redacted_bytes=safe,
                        redacted=safe != raw,
                    )
                )
            self._trim()
            self._closed = True
            self._condition.notify_all()
            return tuple(added)

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "cursor": self._cursor,
                "earliest_cursor": self._earliest_cursor,
                "memory_bytes": self._memory_bytes,
                "maximum_memory_bytes": self._maximum_memory_bytes,
                "chunks": len(self._chunks),
                "spills": [spill.to_json() for spill in self._spills],
                "spilled_bytes": self._spilled_bytes,
                "binary_bytes": self._binary_bytes,
                "pending_redaction_bytes": len(self._pending),
                "closed": self._closed,
            }

    def _append_chunk(
        self,
        raw: bytes,
        *,
        redacted_bytes: bytes,
        redacted: bool,
    ) -> OutputChunk:
        first = self._cursor
        next_cursor = first + len(raw)
        if len(redacted_bytes) != len(raw):
            raise AssertionError("terminal output chunk must preserve cursor byte length")
        score = binary_score(raw)
        try:
            decoded = redacted_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            decoded = ""
            binary = True
        else:
            binary = score > 0.05
        if binary:
            self._binary_bytes += len(raw)
            # A binary classification must not bypass the same credential
            # redaction boundary applied to text.  Cursor accounting remains
            # based on the original PTY bytes, while the persisted artifact
            # receives only the sanitized material.
            spill = self._spill(
                redacted_bytes,
                first,
                next_cursor,
                True,
                redacted,
            )
            text = _bounded_binary_placeholder(spill, len(raw))
        else:
            text = decoded
        if len(text.encode("utf-8")) > len(raw):
            raise AssertionError("terminal output text exceeds its cursor byte length")
        self._sequence += 1
        chunk = OutputChunk(
            sequence=self._sequence,
            first_cursor=first,
            next_cursor=next_cursor,
            text=text,
            byte_length=len(raw),
            sha256=hashlib.sha256(redacted_bytes).hexdigest(),
            redacted=redacted,
        )
        self._chunks.append(chunk)
        self._memory_bytes += len(raw)
        self._cursor = next_cursor
        return chunk

    def _trim(self) -> None:
        pending: list[OutputChunk] = []
        total = 0
        while self._memory_bytes > self._maximum_memory_bytes and self._chunks:
            chunk = self._chunks.popleft()
            pending.append(chunk)
            total += chunk.byte_length
            self._memory_bytes -= chunk.byte_length
            self._earliest_cursor = chunk.next_cursor
            if total >= self._maximum_chunk_bytes * 4:
                self._spill_chunks(pending)
                pending = []
                total = 0
        if pending:
            self._spill_chunks(pending)

    def _spill_chunks(self, chunks: list[OutputChunk]) -> None:
        if not chunks:
            return
        data = "".join(chunk.text for chunk in chunks).encode("utf-8")
        redacted = any(chunk.redacted for chunk in chunks)
        self._spill(
            data,
            chunks[0].first_cursor,
            chunks[-1].next_cursor,
            False,
            redacted,
        )

    def _spill(
        self,
        data: bytes,
        first_cursor: int,
        next_cursor: int,
        binary: bool,
        redacted: bool,
    ) -> TerminalSpill:
        if self._spill_sink is None:
            digest = hashlib.sha256(data).hexdigest()
            spill = TerminalSpill(
                artifact_id=f"terminal-spill-{digest[:24]}",
                revision=digest,
                media_type=(
                    "application/octet-stream"
                    if binary
                    else "text/plain"
                ),
                sha256=digest,
                byte_length=next_cursor - first_cursor,
                first_cursor=first_cursor,
                next_cursor=next_cursor,
                binary=binary,
                redacted=redacted,
            )
        else:
            spill = self._spill_sink(
                data,
                first_cursor,
                next_cursor,
                binary,
                redacted,
            )
        for previous in self._spills:
            if (
                previous.artifact_id == spill.artifact_id
                and previous.revision != spill.revision
            ):
                raise TerminalError(
                    "terminal_spill_identity_conflict",
                    "Terminal spill identity was reused with another revision.",
                    status=500,
                )
        self._spills.append(spill)
        self._spilled_bytes += next_cursor - first_cursor
        return spill
