from __future__ import annotations

"""Read-only diff projection and permission-gated patch transaction bridge.

The module deliberately owns no canonical workspace, artifact, permission, or
task state.  Its registry contains bounded, reconstructible read snapshots.
All mutations are delegated to ``WorkspaceEditPort`` and its existing
``WorkspacePatchTransactionRuntime`` owner.
"""

import hashlib
import json
import mimetypes
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from zyra_core import ArtifactRef, EventRecord, EventType, new_id
from zyra_workspace import (
    MutationKind,
    WorkspaceDirtyKind,
    WorkspaceDirtyStateRuntime,
    WorkspaceEditPort,
    WorkspaceError,
    WorkspaceKind,
    WorkspaceManagerRuntime,
    WorkspaceMutation,
    new_workspace_id,
)

from .artifact_api import ArtifactCatalogService, ArtifactReadQuery


DIFF_MANIFEST_SCHEMA = "zyra.diff-review-manifest.v1"
DIFF_PAGE_SCHEMA = "zyra.diff-review-page.v1"
DIFF_CONTENT_SCHEMA = "zyra.diff-review-file-content.v1"
REVIEW_RECEIPT_SCHEMA = "zyra.diff-review-receipt.v1"
PATCH_RECEIPT_SCHEMA = "zyra.patch-review-transaction-receipt.v1"
DEFAULT_MAXIMUM_PAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAXIMUM_PAGE_LINES = 100_000
MAXIMUM_PATCH_BYTES = 64 * 1024 * 1024
MAXIMUM_FILES = 10_000
MAXIMUM_HUNKS = 100_000
MAXIMUM_LINES = 2_000_000
MAXIMUM_COMMENTS = 100_000
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)$"
)
_DIFF_HEADER = re.compile(r"^diff --git a/(?P<old>.+) b/(?P<new>.+)$")
_MODE = re.compile(r"^(?:old|new|new file|deleted file) mode (?P<mode>[0-7]{3,6})$")


class PermissionPort(Protocol):
    def permission_claim(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def permission_enforce(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class DiffReviewApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})

    def response(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
            "fallback": False,
        }


@dataclass(frozen=True, slots=True)
class DiffApiResponse:
    status: int
    body: Mapping[str, Any]
    events: tuple[EventRecord, ...] = ()
    headers: Mapping[str, str] = field(
        default_factory=lambda: {
            "Cache-Control": "no-store, max-age=0",
            "X-Zyra-Canonical-Owner": "WorkspacePatchTransactionRuntime",
        }
    )


@dataclass(frozen=True, slots=True)
class DiffLine:
    line_id: str
    kind: str
    text: str
    old_line: int | None
    new_line: int | None
    patch_line: int
    byte_offset: int
    byte_length: int
    no_newline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "kind": self.kind,
            "text": self.text,
            "old_line": self.old_line,
            "new_line": self.new_line,
            "patch_line": self.patch_line,
            "byte_offset": self.byte_offset,
            "byte_length": self.byte_length,
            "no_newline": self.no_newline,
        }


@dataclass(frozen=True, slots=True)
class DiffHunk:
    hunk_id: str
    file_id: str
    index: int
    header: str
    section: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    patch_start: int
    patch_end: int
    lines: tuple[DiffLine, ...]

    @property
    def additions(self) -> int:
        return sum(item.kind == "added" for item in self.lines)

    @property
    def deletions(self) -> int:
        return sum(item.kind == "deleted" for item in self.lines)

    @property
    def contexts(self) -> int:
        return sum(item.kind == "context" for item in self.lines)

    @property
    def utf8_bytes(self) -> int:
        return sum(item.byte_length for item in self.lines) + len(self.header.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "hunk_id": self.hunk_id,
            "file_id": self.file_id,
            "index": self.index,
            "header": self.header,
            "section": self.section,
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "additions": self.additions,
            "deletions": self.deletions,
            "context_lines": self.contexts,
            "patch_start": self.patch_start,
            "patch_end": self.patch_end,
            "lines": [item.to_dict() for item in self.lines],
        }


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    text: str
    byte_size: int
    sha256: str
    mtime_ns: int
    mode: int
    exists: bool
    evidence: Any
    encoding: str
    line_ending: str


@dataclass(frozen=True, slots=True)
class DiffFile:
    file_id: str
    path: str
    previous_path: str
    kind: str
    binary: bool
    patch_offset: int
    patch_bytes: int
    hunks: tuple[DiffHunk, ...]
    pages: tuple[tuple[DiffHunk, ...], ...]
    base: FileSnapshot
    proposed_text: str
    proposed_sha256: str
    old_mode: int
    new_mode: int
    language: str
    mime_type: str
    risk: Mapping[str, Any]

    @property
    def additions(self) -> int:
        return sum(item.additions for item in self.hunks)

    @property
    def deletions(self) -> int:
        return sum(item.deletions for item in self.hunks)

    @property
    def line_count(self) -> int:
        return sum(len(item.lines) for item in self.hunks)

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "path": self.path,
            "previous_path": self.previous_path or None,
            "kind": "binary" if self.binary else self.kind,
            "binary": self.binary,
            "oversized": self.patch_bytes > DEFAULT_MAXIMUM_PAGE_BYTES
            or self.line_count > DEFAULT_MAXIMUM_PAGE_LINES,
            "truncated": False,
            "encoding": "binary" if self.binary else self.base.encoding,
            "line_ending": "binary" if self.binary else self.base.line_ending,
            "additions": self.additions,
            "deletions": self.deletions,
            "hunk_count": len(self.hunks),
            "page_count": len(self.pages),
            "patch_offset": self.patch_offset,
            "patch_bytes": self.patch_bytes,
            "old_size": self.base.byte_size if self.base.exists else 0,
            "new_size": (
                self.base.byte_size
                if self.binary and self.kind != "deleted"
                else len(self.proposed_text.encode(self.base.encoding))
                if self.kind != "deleted"
                else 0
            ),
            "old_sha256": self.base.sha256 if self.base.exists else "",
            "new_sha256": self.proposed_sha256 if self.kind != "deleted" else "",
            "current_sha256": self.base.sha256,
            "old_mtime_ns": self.base.mtime_ns,
            "current_mtime_ns": self.base.mtime_ns,
            "old_mode": self.old_mode,
            "current_mode": self.base.mode,
            "language": self.language,
            "mime_type": self.mime_type,
            "risk": dict(self.risk),
        }


@dataclass(slots=True)
class DiffReviewSession:
    task_id: str
    run_id: str
    artifact_id: str
    artifact_revision: str
    artifact_sha256: str
    diff_id: str
    workspace_id: str
    owner_epoch: int
    binding_revision: int
    lease_id: str
    generated_at: str
    files: tuple[DiffFile, ...]
    patch_bytes: int
    review_revision: int = 0
    comments: dict[str, dict[str, Any]] = field(default_factory=dict)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def file(self, file_id: str) -> DiffFile:
        selected = next((item for item in self.files if item.file_id == file_id), None)
        if selected is None:
            raise DiffReviewApiError(404, "diff_file_not_found", "Diff file identity was not found.")
        return selected


class DiffReviewRegistry:
    """Bounded cache of reconstructible artifact/workspace observations."""

    def __init__(self, *, maximum_sessions: int = 128) -> None:
        self.maximum_sessions = max(1, int(maximum_sessions))
        self._sessions: dict[tuple[str, str, str], DiffReviewSession] = {}
        self._order: list[tuple[str, str, str]] = []
        self._guard = threading.RLock()

    def get(self, task_id: str, artifact_id: str, revision: str) -> DiffReviewSession | None:
        key = (task_id, artifact_id, revision)
        with self._guard:
            selected = self._sessions.get(key)
            if selected is not None:
                if key in self._order:
                    self._order.remove(key)
                self._order.append(key)
            return selected

    def put(self, session: DiffReviewSession) -> DiffReviewSession:
        key = (session.task_id, session.artifact_id, session.artifact_revision)
        with self._guard:
            self._sessions[key] = session
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
            while len(self._order) > self.maximum_sessions:
                expired = self._order.pop(0)
                self._sessions.pop(expired, None)
        return session

    def find_diff(self, task_id: str, diff_id: str) -> DiffReviewSession | None:
        with self._guard:
            return next(
                (
                    item
                    for item in self._sessions.values()
                    if item.task_id == task_id and item.diff_id == diff_id
                ),
                None,
            )

    def clear(self) -> None:
        with self._guard:
            self._sessions.clear()
            self._order.clear()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(content: bytes, *, prefix: bool = True) -> str:
    value = hashlib.sha256(content).hexdigest()
    return f"sha256:{value}" if prefix else value


def _stable_id(kind: str, *values: object) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{kind}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _identity(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 512 or "\x00" in text or "\r" in text or "\n" in text:
        raise DiffReviewApiError(400, "diff_identity_invalid", f"{label} identity is invalid.")
    return text


def _logical_path(value: Any, label: str = "path") -> str:
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("a/") or text.startswith("b/"):
        text = text[2:]
    parts = text.split("/")
    if (
        not text
        or text.startswith("/")
        or re.match(r"^[A-Za-z]:", text)
        or any(not part or part in {".", ".."} for part in parts)
    ):
        raise DiffReviewApiError(400, "diff_path_invalid", f"{label} is not a logical path.")
    return text


def _line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    without_crlf = text.replace("\r\n", "")
    lf = without_crlf.count("\n")
    cr = without_crlf.count("\r")
    active = sum(value > 0 for value in (crlf, lf, cr))
    if active > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if lf:
        return "lf"
    if cr:
        return "cr"
    return "none"


def _wire_mtime(value: int) -> int:
    """Project host nanoseconds into an exact JavaScript-safe microsecond tick."""

    selected = max(0, int(value))
    return min(2**53 - 1, selected // 1_000)


def _language(path: str) -> str:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "python",
        "ts": "typescript",
        "tsx": "typescriptreact",
        "js": "javascript",
        "jsx": "javascriptreact",
        "json": "json",
        "md": "markdown",
        "rs": "rust",
        "go": "go",
        "java": "java",
        "css": "css",
        "html": "html",
        "yaml": "yaml",
        "yml": "yaml",
        "toml": "toml",
    }.get(suffix, "text")


def _text_lines(text: str) -> tuple[list[str], bool]:
    if not text:
        return [], False
    terminated = text.endswith(("\n", "\r"))
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if terminated:
        lines.pop()
    return lines, terminated


def _join_lines(lines: Sequence[str], *, ending: str, terminated: bool) -> str:
    delimiter = {"crlf": "\r\n", "cr": "\r"}.get(ending, "\n")
    return delimiter.join(lines) + (delimiter if terminated else "")


def _apply_hunks(base_text: str, hunks: Sequence[DiffHunk], *, ending: str) -> str:
    source, terminated = _text_lines(base_text)
    result: list[str] = []
    cursor = 0
    for hunk in hunks:
        start = max(0, hunk.old_start - 1 if hunk.old_start > 0 else 0)
        if start < cursor or start > len(source):
            raise DiffReviewApiError(
                409,
                "diff_hunk_position_invalid",
                "Patch hunk cannot be positioned in the reviewed base.",
                details={"hunk_id": hunk.hunk_id, "start": start, "cursor": cursor},
            )
        result.extend(source[cursor:start])
        cursor = start
        for line in hunk.lines:
            if line.kind in {"context", "deleted"}:
                if cursor >= len(source) or source[cursor] != line.text:
                    raise DiffReviewApiError(
                        409,
                        "diff_hunk_context_mismatch",
                        "Patch hunk context does not match the reviewed base.",
                        details={"hunk_id": hunk.hunk_id, "patch_line": line.patch_line},
                    )
                if line.kind == "context":
                    result.append(source[cursor])
                cursor += 1
            elif line.kind == "added":
                result.append(line.text)
        if hunk.lines and hunk.lines[-1].no_newline:
            terminated = False
    result.extend(source[cursor:])
    return _join_lines(result, ending=ending, terminated=terminated)


@dataclass(slots=True)
class _MutableFile:
    old_path: str
    path: str
    patch_offset: int
    patch_end: int = 0
    kind: str = "modified"
    binary: bool = False
    old_mode: int = 0
    new_mode: int = 0
    hunks: list[DiffHunk] = field(default_factory=list)


def _path_from_marker(value: str) -> str:
    selected = value.split("\t", 1)[0].strip()
    if selected == "/dev/null":
        return ""
    return _logical_path(selected, "diff marker path")


def _read_patch_bytes(
    service: ArtifactCatalogService,
    artifact: ArtifactRef,
    *,
    task_id: str,
    revision: str,
) -> bytes:
    offset = 0
    output = bytearray()
    while True:
        observed, selected, _receipt = service.download(
            artifact=artifact,
            query=ArtifactReadQuery(
                task_id=task_id,
                artifact_id=artifact.artifact_id,
                expected_revision=revision,
                offset=offset,
                length=min(DEFAULT_MAXIMUM_PAGE_BYTES, MAXIMUM_PATCH_BYTES - offset),
                purpose="download",
            ),
        )
        output.extend(selected.content)
        if len(output) > MAXIMUM_PATCH_BYTES:
            raise DiffReviewApiError(
                413,
                "diff_patch_budget_exceeded",
                "Patch artifact exceeds the 64 MiB review budget.",
            )
        if selected.complete or selected.end_exclusive >= selected.total_bytes:
            if _digest(bytes(output), prefix=False) != observed.sha256:
                raise DiffReviewApiError(
                    409,
                    "diff_artifact_digest_mismatch",
                    "Patch bytes do not match the canonical artifact digest.",
                )
            return bytes(output)
        offset = selected.end_exclusive
        if offset >= MAXIMUM_PATCH_BYTES:
            raise DiffReviewApiError(
                413,
                "diff_patch_budget_exceeded",
                "Patch artifact exceeds the 64 MiB review budget.",
            )


def _parse_patch(patch: str) -> list[_MutableFile]:
    raw_lines = patch.splitlines(keepends=True)
    files: list[_MutableFile] = []
    current: _MutableFile | None = None
    byte_offset = 0
    patch_line = 0
    hunk_total = 0
    line_total = 0
    index = 0
    while index < len(raw_lines):
        raw = raw_lines[index]
        patch_line += 1
        stripped = raw.rstrip("\r\n")
        header = _DIFF_HEADER.match(stripped)
        if header:
            if current is not None:
                current.patch_end = byte_offset
            current = _MutableFile(
                old_path=_logical_path(header.group("old"), "diff old path"),
                path=_logical_path(header.group("new"), "diff new path"),
                patch_offset=byte_offset,
            )
            files.append(current)
            if len(files) > MAXIMUM_FILES:
                raise DiffReviewApiError(413, "diff_file_budget_exceeded", "Patch has too many files.")
            byte_offset += len(raw.encode("utf-8"))
            index += 1
            continue
        if current is None:
            byte_offset += len(raw.encode("utf-8"))
            index += 1
            continue
        if stripped.startswith("rename from "):
            current.old_path = _logical_path(stripped[len("rename from ") :], "rename source")
            current.kind = "renamed"
        elif stripped.startswith("rename to "):
            current.path = _logical_path(stripped[len("rename to ") :], "rename destination")
            current.kind = "renamed"
        elif stripped.startswith("new file mode "):
            current.kind = "added"
            current.new_mode = int(stripped.rsplit(" ", 1)[-1], 8) & 0o7777
        elif stripped.startswith("deleted file mode "):
            current.kind = "deleted"
            current.old_mode = int(stripped.rsplit(" ", 1)[-1], 8) & 0o7777
        elif stripped.startswith("old mode "):
            current.old_mode = int(stripped.rsplit(" ", 1)[-1], 8) & 0o7777
        elif stripped.startswith("new mode "):
            current.new_mode = int(stripped.rsplit(" ", 1)[-1], 8) & 0o7777
        elif stripped.startswith("Binary files ") or stripped.startswith("GIT binary patch"):
            current.binary = True
        elif stripped.startswith("--- "):
            marker = _path_from_marker(stripped[4:])
            if not marker:
                current.kind = "added"
            else:
                current.old_path = marker
        elif stripped.startswith("+++ "):
            marker = _path_from_marker(stripped[4:])
            if not marker:
                current.kind = "deleted"
            else:
                current.path = marker
        else:
            hunk_header = _HUNK_HEADER.match(stripped)
            if hunk_header:
                hunk_total += 1
                if hunk_total > MAXIMUM_HUNKS:
                    raise DiffReviewApiError(
                        413,
                        "diff_hunk_budget_exceeded",
                        "Patch has too many hunks.",
                    )
                hunk_patch_start = byte_offset
                old_start = int(hunk_header.group("old_start"))
                new_start = int(hunk_header.group("new_start"))
                old_count = int(hunk_header.group("old_count") or 1)
                new_count = int(hunk_header.group("new_count") or 1)
                file_id = _stable_id("diff-file", current.old_path, current.path)
                hunk_index = len(current.hunks)
                hunk_id = _stable_id("diff-hunk", file_id, hunk_index, stripped)
                hunk_lines: list[DiffLine] = []
                old_line = old_start
                new_line = new_start
                byte_offset += len(raw.encode("utf-8"))
                index += 1
                while index < len(raw_lines):
                    candidate = raw_lines[index]
                    candidate_text = candidate.rstrip("\r\n")
                    if _DIFF_HEADER.match(candidate_text) or _HUNK_HEADER.match(candidate_text):
                        break
                    if not candidate_text:
                        prefix = " "
                        content = ""
                    else:
                        prefix = candidate_text[0]
                        content = candidate_text[1:]
                    if prefix not in {" ", "+", "-", "\\"}:
                        break
                    patch_line += 1
                    candidate_offset = byte_offset
                    candidate_bytes = len(candidate.encode("utf-8"))
                    byte_offset += candidate_bytes
                    index += 1
                    if prefix == "\\":
                        if hunk_lines:
                            previous = hunk_lines[-1]
                            hunk_lines[-1] = DiffLine(
                                line_id=previous.line_id,
                                kind=previous.kind,
                                text=previous.text,
                                old_line=previous.old_line,
                                new_line=previous.new_line,
                                patch_line=previous.patch_line,
                                byte_offset=previous.byte_offset,
                                byte_length=previous.byte_length,
                                no_newline=True,
                            )
                        continue
                    kind = {" ": "context", "+": "added", "-": "deleted"}[prefix]
                    old_coordinate = old_line if kind in {"context", "deleted"} else None
                    new_coordinate = new_line if kind in {"context", "added"} else None
                    line_id = _stable_id(
                        "diff-line",
                        hunk_id,
                        len(hunk_lines),
                        kind,
                        old_coordinate,
                        new_coordinate,
                    )
                    hunk_lines.append(
                        DiffLine(
                            line_id=line_id,
                            kind=kind,
                            text=content,
                            old_line=old_coordinate,
                            new_line=new_coordinate,
                            patch_line=patch_line,
                            byte_offset=candidate_offset,
                            byte_length=candidate_bytes,
                        )
                    )
                    line_total += 1
                    if line_total > MAXIMUM_LINES:
                        raise DiffReviewApiError(
                            413,
                            "diff_line_budget_exceeded",
                            "Patch exceeds the two-million-line review budget.",
                        )
                    if old_coordinate is not None:
                        old_line += 1
                    if new_coordinate is not None:
                        new_line += 1
                if old_line - old_start != old_count or new_line - new_start != new_count:
                    raise DiffReviewApiError(
                        400,
                        "diff_hunk_count_mismatch",
                        "Patch hunk line counts do not match its header.",
                        details={
                            "header": stripped,
                            "actual_old": old_line - old_start,
                            "actual_new": new_line - new_start,
                        },
                    )
                current.hunks.append(
                    DiffHunk(
                        hunk_id=hunk_id,
                        file_id=file_id,
                        index=hunk_index,
                        header=stripped,
                        section=hunk_header.group("section").strip(),
                        old_start=old_start,
                        old_count=old_count,
                        new_start=new_start,
                        new_count=new_count,
                        patch_start=hunk_patch_start,
                        patch_end=byte_offset,
                        lines=tuple(hunk_lines),
                    )
                )
                continue
        byte_offset += len(raw.encode("utf-8"))
        index += 1
    if current is not None:
        current.patch_end = byte_offset
    if not files:
        raise DiffReviewApiError(400, "diff_patch_empty", "Artifact is not a unified patch.")
    return files


def _paginate_hunks(hunks: Sequence[DiffHunk]) -> tuple[tuple[DiffHunk, ...], ...]:
    if not hunks:
        return ()
    pages: list[tuple[DiffHunk, ...]] = []
    current: list[DiffHunk] = []
    current_lines = 0
    current_bytes = 0
    for hunk in hunks:
        hunk_lines = len(hunk.lines)
        hunk_bytes = hunk.utf8_bytes
        if (
            current
            and (
                current_lines + hunk_lines > DEFAULT_MAXIMUM_PAGE_LINES
                or current_bytes + hunk_bytes > DEFAULT_MAXIMUM_PAGE_BYTES
            )
        ):
            pages.append(tuple(current))
            current = []
            current_lines = 0
            current_bytes = 0
        if hunk_lines > DEFAULT_MAXIMUM_PAGE_LINES or hunk_bytes > DEFAULT_MAXIMUM_PAGE_BYTES:
            raise DiffReviewApiError(
                413,
                "diff_hunk_page_budget_exceeded",
                "A single patch hunk exceeds the bounded page contract.",
                details={"hunk_id": hunk.hunk_id, "lines": hunk_lines, "bytes": hunk_bytes},
            )
        current.append(hunk)
        current_lines += hunk_lines
        current_bytes += hunk_bytes
    if current:
        pages.append(tuple(current))
    return tuple(pages)


def _page_canonical_payload(
    session: DiffReviewSession,
    file: DiffFile,
    page_index: int,
    hunks: Sequence[DiffHunk],
) -> dict[str, Any]:
    start = hunks[0].index if hunks else 0
    end = hunks[-1].index + 1 if hunks else 0
    return {
        "diff_id": session.diff_id,
        "file_id": file.file_id,
        "artifact_revision": session.artifact_revision,
        "page_index": page_index,
        "page_count": len(file.pages),
        "hunk_start": start,
        "hunk_end": end,
        "hunks": [
            {
                "hunk_id": hunk.hunk_id,
                "index": hunk.index,
                "header": hunk.header,
                "old_start": hunk.old_start,
                "old_count": hunk.old_count,
                "new_start": hunk.new_start,
                "new_count": hunk.new_count,
                "lines": [
                    {
                        "line_id": line.line_id,
                        "kind": line.kind,
                        "text": line.text,
                        "old_line": line.old_line,
                        "new_line": line.new_line,
                        "patch_line": line.patch_line,
                        "no_newline": line.no_newline,
                    }
                    for line in hunk.lines
                ],
            }
            for hunk in hunks
        ],
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


class DiffReviewApiService:
    def __init__(
        self,
        *,
        artifact_service: ArtifactCatalogService,
        workspace_manager: WorkspaceManagerRuntime,
        permission_port: PermissionPort,
        registry: DiffReviewRegistry | None = None,
        artifact_store: Any | None = None,
        enabled: bool = True,
    ) -> None:
        self.artifact_service = artifact_service
        self.workspace_manager = workspace_manager
        self.permission_port = permission_port
        self.registry = registry or DiffReviewRegistry()
        self.artifact_store = artifact_store
        self.enabled = bool(enabled)
        self._receipt_guard = threading.RLock()
        self._transaction_receipts: dict[str, dict[str, Any]] = {}

    def manifest(
        self,
        *,
        task_id: str,
        run_id: str,
        artifact: ArtifactRef,
        revision: str = "",
    ) -> DiffApiResponse:
        self._require_enabled()
        metadata = self.artifact_service.metadata(
            task_id=task_id,
            artifact=artifact,
            expected_revision=revision or None,
        )
        contract = dict(metadata.get("artifact") or {})
        canonical_revision = _identity(contract.get("revision"), "artifact revision")
        existing = self.registry.get(task_id, artifact.artifact_id, canonical_revision)
        if existing is None:
            existing = self._build_session(
                task_id=task_id,
                run_id=run_id,
                artifact=artifact,
                revision=canonical_revision,
                artifact_sha256=str(contract.get("sha256") or ""),
            )
            self.registry.put(existing)
        return DiffApiResponse(HTTPStatus.OK, self._manifest_payload(existing))

    def page(
        self,
        *,
        task_id: str,
        run_id: str,
        artifact: ArtifactRef,
        file_id: str,
        revision: str,
        page_index: int,
        maximum_bytes: int,
        maximum_lines: int,
    ) -> DiffApiResponse:
        session = self._require_session(
            task_id=task_id,
            run_id=run_id,
            artifact=artifact,
            revision=revision,
        )
        file = session.file(_identity(file_id, "file"))
        if page_index < 0 or page_index >= len(file.pages):
            raise DiffReviewApiError(
                416,
                "diff_page_out_of_range",
                "Requested diff page is outside the manifest.",
            )
        hunks = file.pages[page_index]
        line_count = sum(len(item.lines) for item in hunks)
        utf8_bytes = sum(item.utf8_bytes for item in hunks)
        if maximum_bytes < 1_024 or maximum_lines < 1:
            raise DiffReviewApiError(400, "diff_page_budget_invalid", "Diff page budget is invalid.")
        if utf8_bytes > min(DEFAULT_MAXIMUM_PAGE_BYTES, maximum_bytes):
            raise DiffReviewApiError(
                413,
                "diff_page_byte_budget_exceeded",
                "Requested byte budget cannot contain this immutable hunk page.",
                details={"required": utf8_bytes, "requested": maximum_bytes},
            )
        if line_count > min(DEFAULT_MAXIMUM_PAGE_LINES, maximum_lines):
            raise DiffReviewApiError(
                413,
                "diff_page_line_budget_exceeded",
                "Requested line budget cannot contain this immutable hunk page.",
                details={"required": line_count, "requested": maximum_lines},
            )
        canonical = _page_canonical_payload(session, file, page_index, hunks)
        content_digest = _digest(_canonical_json(canonical).encode("utf-8"))
        cursor = _stable_id("diff-cursor", session.diff_id, file.file_id, page_index)
        next_cursor = (
            _stable_id("diff-cursor", session.diff_id, file.file_id, page_index + 1)
            if page_index + 1 < len(file.pages)
            else None
        )
        receipt_id = _stable_id("diff-page-receipt", content_digest, cursor)
        return DiffApiResponse(
            HTTPStatus.OK,
            {
                "schema": DIFF_PAGE_SCHEMA,
                **canonical,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "complete": page_index + 1 == len(file.pages),
                "utf8_bytes": utf8_bytes,
                "content_digest": content_digest,
                "receipt_id": receipt_id,
                "generated_at": _now(),
                "physical_path_disclosed": False,
            },
        )

    def file_content(
        self,
        *,
        task_id: str,
        run_id: str,
        artifact: ArtifactRef,
        file_id: str,
        revision: str,
        version: str,
    ) -> DiffApiResponse:
        session = self._require_session(
            task_id=task_id,
            run_id=run_id,
            artifact=artifact,
            revision=revision,
        )
        file = session.file(_identity(file_id, "file"))
        if version not in {"base", "current"}:
            raise DiffReviewApiError(400, "diff_content_version_invalid", "File version is invalid.")
        if file.binary:
            raise DiffReviewApiError(
                415,
                "diff_binary_content_refused",
                "Binary diff content cannot enter the text preflight path.",
            )
        selected = file.base
        if version == "current":
            port = self._edit_port(session)
            logical_path = file.previous_path if file.kind == "renamed" else file.path
            current = port.read_bytes(logical_path)
            selected = self._snapshot(current)
        content = selected.text
        digest = selected.sha256
        return DiffApiResponse(
            HTTPStatus.OK,
            {
                "schema": DIFF_CONTENT_SCHEMA,
                "diff_id": session.diff_id,
                "file_id": file.file_id,
                "artifact_revision": session.artifact_revision,
                "version": version,
                "text": content,
                "sha256": digest,
                "mtime_ns": selected.mtime_ns,
                "mode": selected.mode,
                "encoding": selected.encoding,
                "line_ending": selected.line_ending,
                "complete": True,
                "utf8_bytes": len(content.encode("utf-8")),
                "receipt_id": _stable_id(
                    "diff-content-receipt",
                    session.diff_id,
                    file.file_id,
                    version,
                    digest,
                    selected.mtime_ns,
                ),
                "generated_at": _now(),
                "physical_path_disclosed": False,
            },
        )

    def review(
        self,
        *,
        task_id: str,
        run_id: str,
        artifact: ArtifactRef,
        payload: Mapping[str, Any],
    ) -> DiffApiResponse:
        self._require_schema(payload, "zyra.diff-review-comment-request.v1")
        revision = _identity(payload.get("artifact_revision"), "artifact revision")
        session = self._require_session(
            task_id=task_id,
            run_id=run_id,
            artifact=artifact,
            revision=revision,
        )
        if _identity(payload.get("diff_id"), "diff") != session.diff_id:
            raise DiffReviewApiError(409, "diff_review_binding_mismatch", "Review targets another diff.")
        expected_revision = _integer(payload.get("expected_review_revision"), "review revision")
        if expected_revision != session.review_revision:
            raise DiffReviewApiError(
                409,
                "diff_review_revision_conflict",
                "Review revision changed before this action.",
                details={"expected": expected_revision, "current": session.review_revision},
            )
        action = str(payload.get("action") or "")
        if action not in {"select", "comment", "comment_update", "comment_resolve"}:
            raise DiffReviewApiError(400, "diff_review_action_invalid", "Review action is invalid.")
        selection = self._validate_selection(session, payload.get("selection"))
        causation_id = _identity(payload.get("causation_id"), "causation")
        actor_id = _identity(payload.get("actor_id"), "actor")
        created_at = _now()
        sealed = bool(payload.get("sealed"))
        permission_material = self._permission_material(
            session=session,
            payload=payload,
            operation="write",
            arguments={
                "diff_id": session.diff_id,
                "artifact_id": session.artifact_id,
                "artifact_revision": session.artifact_revision,
                "action": action,
                "selection": selection,
                "comment_id": str(payload.get("comment_id") or ""),
                "review_revision": expected_revision,
            },
        )
        permission = self._permission_decision(
            permission_material,
            permit_id=str(payload.get("permission_permit_id") or ""),
        )
        if sealed:
            permission = {
                **permission,
                "effect": "deny",
                "pending": False,
                "reason_code": "sealed.manual_review_mutation_denied",
                "recovery_input": {
                    "action": "continue_sealed_without_manual_review_mutation",
                    "sealed": True,
                    "human_intervention_count": 0,
                },
            }
        if permission["effect"] != "allow":
            event = self._review_event(
                session=session,
                action=action,
                causation_id=causation_id,
                selection=selection,
                comment=None,
                accepted=False,
                permission=permission,
            )
            receipt = {
                "schema": REVIEW_RECEIPT_SCHEMA,
                "receipt_id": _stable_id(
                    "diff-review-receipt",
                    session.diff_id,
                    action,
                    causation_id,
                    session.review_revision,
                    permission["effect"],
                ),
                "diff_id": session.diff_id,
                "task_id": task_id,
                "action": action,
                "accepted": False,
                "causation_id": causation_id,
                "event_ids": [event.event_id],
                "comment": None,
                "selection": None,
                "revision": session.review_revision,
                "permission": permission,
                "human_intervention_count": 0,
                "denied_manual_mutation_count": 1,
                "created_at": created_at,
            }
            return DiffApiResponse(
                HTTPStatus.ACCEPTED
                if permission["effect"] == "ask" and not sealed
                else HTTPStatus.FORBIDDEN,
                receipt,
                events=(event,),
            )
        session.review_revision += 1
        comment: dict[str, Any] | None = None
        if action != "select":
            comment_id = str(payload.get("comment_id") or "").strip()
            if action == "comment":
                comment_id = comment_id or _stable_id(
                    "diff-comment", session.diff_id, causation_id, session.review_revision
                )
                if comment_id in session.comments:
                    raise DiffReviewApiError(
                        409,
                        "diff_comment_identity_conflict",
                        "Comment identity already exists.",
                    )
                if len(session.comments) >= MAXIMUM_COMMENTS:
                    raise DiffReviewApiError(413, "diff_comment_budget_exceeded", "Comment budget exhausted.")
                original_created_at = created_at
                original_revision = 1
            else:
                comment_id = _identity(comment_id, "comment")
                previous = session.comments.get(comment_id)
                if previous is None:
                    raise DiffReviewApiError(404, "diff_comment_not_found", "Comment was not found.")
                original_created_at = str(previous["created_at"])
                original_revision = int(previous["revision"]) + 1
            body = str(payload.get("body") or "").strip()
            if action != "comment_resolve" and not body:
                raise DiffReviewApiError(400, "diff_comment_body_empty", "Comment body is required.")
            if len(body.encode("utf-8")) > 64 * 1024:
                raise DiffReviewApiError(413, "diff_comment_body_budget", "Comment is too large.")
            comment = {
                "comment_id": comment_id,
                "diff_id": session.diff_id,
                "file_id": selection["file_id"],
                "hunk_id": selection["hunk_id"],
                "selection": selection,
                "body": body or str(session.comments[comment_id]["body"]),
                "author_id": actor_id,
                "created_at": original_created_at,
                "updated_at": created_at,
                "state": "resolved" if action == "comment_resolve" else "submitted",
                "causation_id": causation_id,
                "revision": original_revision,
            }
            session.comments[comment_id] = comment
        event = self._review_event(
            session=session,
            action=action,
            causation_id=causation_id,
            selection=selection,
            comment=comment,
            accepted=True,
            permission=permission,
        )
        receipt_id = _stable_id(
            "diff-review-receipt",
            session.diff_id,
            action,
            causation_id,
            session.review_revision,
        )
        receipt = {
            "schema": REVIEW_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "diff_id": session.diff_id,
            "task_id": task_id,
            "action": action,
            "accepted": True,
            "causation_id": causation_id,
            "event_ids": [event.event_id],
            "comment": comment,
            "selection": selection if action == "select" else None,
            "revision": session.review_revision,
            "permission": permission,
            "human_intervention_count": 0,
            "denied_manual_mutation_count": 0,
            "created_at": created_at,
        }
        session.receipts[receipt_id] = receipt
        return DiffApiResponse(HTTPStatus.CREATED, receipt, events=(event,))

    def apply(
        self,
        *,
        task_id: str,
        run_id: str,
        artifact: ArtifactRef,
        payload: Mapping[str, Any],
    ) -> DiffApiResponse:
        self._require_schema(payload, "zyra.patch-review-apply.v1")
        self._require_request_binding(payload, task_id=task_id, run_id=run_id)
        revision = _identity(payload.get("artifact_revision"), "artifact revision")
        session = self._require_session(
            task_id=task_id,
            run_id=run_id,
            artifact=artifact,
            revision=revision,
        )
        causation_id = _identity(payload.get("causation_id"), "causation")
        idempotency_key = _identity(payload.get("idempotency_key"), "idempotency")
        replay = self._transaction_receipts.get(idempotency_key)
        if replay is not None:
            if (
                replay.get("task_id") != task_id
                or replay.get("diff_id") != session.diff_id
                or replay.get("causation_id") != causation_id
            ):
                raise DiffReviewApiError(
                    409,
                    "diff_patch_idempotency_conflict",
                    "Patch idempotency key is bound to another exact request.",
                )
            return DiffApiResponse(
                HTTPStatus.OK,
                {**replay, "idempotent_replay": True},
            )
        self._validate_apply_identity(session, payload)
        selected_ids = self._selected_file_ids(session, payload.get("selected_file_ids"))
        preconditions = self._preconditions(session, selected_ids, payload.get("preconditions"))
        sealed = bool(payload.get("sealed"))
        permission_payload = self._permission_material(
            session=session,
            payload=payload,
            operation="write",
            arguments={
                "diff_id": session.diff_id,
                "artifact_id": session.artifact_id,
                "artifact_revision": session.artifact_revision,
                "selected_file_ids": selected_ids,
                "preconditions": preconditions,
                "idempotency_key": idempotency_key,
            },
        )
        permission = self._permission_decision(
            permission_payload,
            permit_id=str(payload.get("permission_permit_id") or ""),
        )
        if sealed:
            permission = {
                **permission,
                "effect": "deny",
                "pending": False,
                "reason_code": "sealed.manual_patch_mutation_denied",
                "recovery_input": {
                    "action": "replan_without_manual_file_mutation",
                    "sealed": True,
                    "human_intervention_count": 0,
                },
            }
        if permission["effect"] != "allow":
            phase = (
                "permission_pending"
                if permission["effect"] == "ask" and not sealed
                else "permission_denied"
            )
            receipt = self._patch_receipt(
                session=session,
                payload=payload,
                phase=phase,
                permission=permission,
                reason_code=str(permission["reason_code"]),
                message=(
                    "Patch apply is waiting for an exact-call TypeScript permit."
                    if phase == "permission_pending"
                    else "Patch apply was deterministically denied before mutation."
                ),
                denied_manual_mutation_count=1,
            )
            self._remember_transaction_receipt(idempotency_key, receipt)
            event = self._patch_event(session, receipt)
            receipt["event_ids"] = [event.event_id]
            self._remember_transaction_receipt(idempotency_key, receipt, replace=True)
            return DiffApiResponse(
                HTTPStatus.ACCEPTED if phase == "permission_pending" else HTTPStatus.FORBIDDEN,
                receipt,
                events=(event,),
            )
        port = self._edit_port(session)
        mutations: list[WorkspaceMutation] = []
        evidence: list[Any] = []
        proposed: dict[str, bytes] = {}
        current_reads: dict[str, Any] = {}
        try:
            for file_id in selected_ids:
                file = session.file(file_id)
                precondition = preconditions[file_id]
                read_path = file.previous_path if file.kind == "renamed" else file.path
                current = port.read_bytes(read_path)
                current_reads[file_id] = current
                evidence.append(current.evidence)
                actual_digest = f"sha256:{current.evidence.content_hash}"
                if actual_digest != precondition["current_sha256"]:
                    receipt = self._patch_receipt(
                        session=session,
                        payload=payload,
                        phase="stale",
                        permission=permission,
                        reason_code="hashline.current_snapshot_stale",
                        message="Workspace content changed after browser preflight.",
                        path_results=[
                            self._path_result(
                                file,
                                current,
                                after_sha256="",
                                bytes_after=0,
                                disposition="stale",
                                verified=False,
                            )
                        ],
                    )
                    self._remember_transaction_receipt(idempotency_key, receipt)
                    event = self._patch_event(session, receipt)
                    receipt["event_ids"] = [event.event_id]
                    self._remember_transaction_receipt(idempotency_key, receipt, replace=True)
                    return DiffApiResponse(HTTPStatus.CONFLICT, receipt, events=(event,))
                if _wire_mtime(current.evidence.mtime_ns) != precondition["current_mtime_ns"]:
                    receipt = self._patch_receipt(
                        session=session,
                        payload=payload,
                        phase="stale",
                        permission=permission,
                        reason_code="hashline.current_mtime_stale",
                        message="Workspace mtime changed after browser preflight.",
                    )
                    self._remember_transaction_receipt(idempotency_key, receipt)
                    event = self._patch_event(session, receipt)
                    receipt["event_ids"] = [event.event_id]
                    self._remember_transaction_receipt(idempotency_key, receipt, replace=True)
                    return DiffApiResponse(HTTPStatus.CONFLICT, receipt, events=(event,))
                ending = precondition["line_ending"]
                current_text = current.content.decode(precondition["encoding"])
                proposed_text = _apply_hunks(current_text, file.hunks, ending=ending)
                proposed_bytes = proposed_text.encode(precondition["encoding"])
                actual_proposed = _digest(proposed_bytes)
                if actual_proposed != precondition["proposed_sha256"]:
                    receipt = self._patch_receipt(
                        session=session,
                        payload=payload,
                        phase="conflicted",
                        permission=permission,
                        reason_code="hashline.proposed_digest_conflict",
                        message="Server proposal differs from the reviewed browser preflight.",
                    )
                    self._remember_transaction_receipt(idempotency_key, receipt)
                    event = self._patch_event(session, receipt)
                    receipt["event_ids"] = [event.event_id]
                    self._remember_transaction_receipt(idempotency_key, receipt, replace=True)
                    return DiffApiResponse(HTTPStatus.CONFLICT, receipt, events=(event,))
                proposed[file_id] = proposed_bytes
                if file.kind == "deleted":
                    mutations.append(
                        WorkspaceMutation(
                            mutation_id=new_workspace_id("mutation"),
                            kind=MutationKind.DELETE_FILE,
                            logical_path=read_path,
                            read_evidence_id=current.evidence.evidence_id,
                            metadata={"diff_file_id": file.file_id, "source": "diff-review"},
                        )
                    )
                elif file.kind == "renamed":
                    destination = port.read_bytes(file.path)
                    evidence.append(destination.evidence)
                    mutations.extend(
                        (
                            WorkspaceMutation(
                                mutation_id=new_workspace_id("mutation"),
                                kind=MutationKind.WRITE_BYTES,
                                logical_path=file.path,
                                content=proposed_bytes,
                                encoding=precondition["encoding"],
                                read_evidence_id=destination.evidence.evidence_id,
                                expected_absent=not destination.exists,
                                mode=file.new_mode or None,
                                metadata={"diff_file_id": file.file_id, "source": "diff-review"},
                            ),
                            WorkspaceMutation(
                                mutation_id=new_workspace_id("mutation"),
                                kind=MutationKind.DELETE_FILE,
                                logical_path=read_path,
                                read_evidence_id=current.evidence.evidence_id,
                                metadata={"diff_file_id": file.file_id, "source": "diff-review"},
                            ),
                        )
                    )
                else:
                    mutations.append(
                        WorkspaceMutation(
                            mutation_id=new_workspace_id("mutation"),
                            kind=MutationKind.WRITE_BYTES,
                            logical_path=file.path,
                            content=proposed_bytes,
                            encoding=precondition["encoding"],
                            read_evidence_id=current.evidence.evidence_id,
                            expected_absent=not current.exists,
                            mode=file.new_mode or None,
                            metadata={"diff_file_id": file.file_id, "source": "diff-review"},
                        )
                    )
            result = port.apply(
                tuple(mutations),
                evidence=tuple(evidence),
                publish_artifact=True,
                idempotency_key=idempotency_key,
                causation_id=causation_id,
            )
        except WorkspaceError as error:
            return self._workspace_failure_response(
                session=session,
                payload=payload,
                permission=permission,
                idempotency_key=idempotency_key,
                error=error,
            )
        transaction = result.transaction
        path_results: list[dict[str, Any]] = []
        for file_id in selected_ids:
            file = session.file(file_id)
            current = current_reads[file_id]
            target_path = file.path
            after = b"" if file.kind == "deleted" else proposed[file_id]
            path_results.append(
                self._path_result(
                    file,
                    current,
                    after_sha256=_digest(after) if after else "",
                    bytes_after=len(after),
                    disposition="deleted" if file.kind == "deleted" else "written",
                    verified=True,
                    path=target_path,
                )
            )
        receipt = self._patch_receipt(
            session=session,
            payload=payload,
            phase="committed",
            permission=permission,
            transaction_id=transaction.transaction_id,
            owner_epoch_before=transaction.owner_epoch_before,
            owner_epoch_after=transaction.owner_epoch_after,
            binding_revision_before=transaction.binding_revision_before,
            binding_revision_after=transaction.binding_revision_after,
            snapshot_id=transaction.snapshot_id,
            reason_code="patch.transaction_committed",
            message="Workspace patch transaction committed and verified.",
            path_results=path_results,
            artifact_refs=list(transaction.artifact_refs),
            verification_refs=[
                _stable_id("patch-verification", transaction.transaction_id, item["file_id"])
                for item in path_results
            ],
            terminal_refs=[transaction.transaction_id],
            timeline_refs=[transaction.transaction_id, causation_id],
            idempotent_replay=result.idempotent_replay,
        )
        event = self._patch_event(session, receipt)
        receipt["event_ids"] = [event.event_id]
        self._remember_transaction_receipt(idempotency_key, receipt)
        return DiffApiResponse(HTTPStatus.CREATED, receipt, events=(event,))

    def rollback(
        self,
        *,
        task_id: str,
        run_id: str,
        transaction_id: str,
        payload: Mapping[str, Any],
    ) -> DiffApiResponse:
        self._require_schema(payload, "zyra.patch-review-rollback.v1")
        self._require_request_binding(payload, task_id=task_id, run_id=run_id)
        requested_transaction = _identity(payload.get("transaction_id"), "transaction")
        if requested_transaction != _identity(transaction_id, "transaction"):
            raise DiffReviewApiError(
                409,
                "diff_rollback_transaction_mismatch",
                "Rollback route and payload target different transactions.",
            )
        source = next(
            (
                receipt
                for receipt in self._transaction_receipts.values()
                if receipt.get("transaction_id") == requested_transaction
                and receipt.get("committed") is True
            ),
            None,
        )
        if source is None:
            raise DiffReviewApiError(
                404,
                "diff_rollback_transaction_not_found",
                "Committed diff transaction receipt was not found.",
            )
        diff_id = _identity(payload.get("diff_id"), "diff")
        session = self.registry.find_diff(task_id, diff_id)
        if session is None:
            raise DiffReviewApiError(404, "diff_review_session_not_found", "Diff session expired.")
        causation_id = _identity(payload.get("causation_id"), "causation")
        idempotency_key = _identity(payload.get("idempotency_key"), "idempotency")
        replay = self._transaction_receipts.get(idempotency_key)
        if replay is not None:
            return DiffApiResponse(HTTPStatus.OK, {**replay, "idempotent_replay": True})
        sealed = bool(payload.get("sealed"))
        permission_payload = self._permission_material(
            session=session,
            payload=payload,
            operation="write",
            arguments={
                "transaction_id": requested_transaction,
                "snapshot_id": str(payload.get("snapshot_id") or ""),
                "operation": "rollback",
            },
        )
        permission = self._permission_decision(
            permission_payload,
            permit_id=str(payload.get("permission_permit_id") or ""),
        )
        if sealed:
            permission = {
                **permission,
                "effect": "deny",
                "pending": False,
                "reason_code": "sealed.manual_rollback_mutation_denied",
                "recovery_input": {
                    "action": "continue_sealed_without_manual_rollback",
                    "sealed": True,
                    "human_intervention_count": 0,
                },
            }
        if permission["effect"] != "allow":
            phase = "permission_pending" if permission["effect"] == "ask" else "permission_denied"
            receipt = self._patch_receipt(
                session=session,
                payload=payload,
                phase=phase,
                permission=permission,
                transaction_id=requested_transaction,
                snapshot_id=str(payload.get("snapshot_id") or ""),
                reason_code=str(permission["reason_code"]),
                message="Rollback did not mutate the workspace because permission was not allowed.",
                denied_manual_mutation_count=1,
            )
            event = self._patch_event(session, receipt)
            receipt["event_ids"] = [event.event_id]
            self._remember_transaction_receipt(idempotency_key, receipt)
            return DiffApiResponse(
                HTTPStatus.ACCEPTED if phase == "permission_pending" else HTTPStatus.FORBIDDEN,
                receipt,
                events=(event,),
            )
        snapshot_id = _identity(payload.get("snapshot_id"), "snapshot")
        if snapshot_id != str(source.get("snapshot_id") or ""):
            raise DiffReviewApiError(
                409,
                "diff_rollback_snapshot_mismatch",
                "Rollback snapshot does not match the committed transaction.",
            )
        try:
            before = self.workspace_manager.store.require_binding(session.workspace_id)
            restored = self.workspace_manager.restore(
                session.workspace_id,
                snapshot_id,
                causation_id=causation_id,
            )
            after = self.workspace_manager.store.require_binding(session.workspace_id)
        except WorkspaceError as error:
            receipt = self._patch_receipt(
                session=session,
                payload=payload,
                phase="rollback_failed",
                permission=permission,
                transaction_id=requested_transaction,
                snapshot_id=snapshot_id,
                reason_code=f"workspace.{getattr(error, 'code', 'rollback_failed')}",
                message="Workspace snapshot restore failed; recovery is required.",
                rollback_failed=True,
            )
            event = self._patch_event(session, receipt)
            receipt["event_ids"] = [event.event_id]
            self._remember_transaction_receipt(idempotency_key, receipt)
            return DiffApiResponse(HTTPStatus.CONFLICT, receipt, events=(event,))
        receipt = self._patch_receipt(
            session=session,
            payload=payload,
            phase="rolled_back",
            permission=permission,
            transaction_id=requested_transaction,
            owner_epoch_before=before.owner_epoch,
            owner_epoch_after=after.owner_epoch,
            binding_revision_before=before.binding_revision,
            binding_revision_after=after.binding_revision,
            snapshot_id=snapshot_id,
            reason_code="patch.transaction_rolled_back",
            message="Committed patch snapshot was restored by WorkspaceManagerRuntime.",
            path_results=list(source.get("path_results") or []),
            verification_refs=[_stable_id("rollback-verification", requested_transaction, snapshot_id)],
            terminal_refs=[requested_transaction, snapshot_id],
            timeline_refs=[requested_transaction, causation_id],
        )
        event = self._patch_event(session, receipt)
        receipt["event_ids"] = [event.event_id]
        self._remember_transaction_receipt(idempotency_key, receipt)
        return DiffApiResponse(HTTPStatus.CREATED, receipt, events=(event,))

    def _build_session(
        self,
        *,
        task_id: str,
        run_id: str,
        artifact: ArtifactRef,
        revision: str,
        artifact_sha256: str,
    ) -> DiffReviewSession:
        patch_bytes = _read_patch_bytes(
            self.artifact_service,
            artifact,
            task_id=task_id,
            revision=revision,
        )
        try:
            patch = patch_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise DiffReviewApiError(
                415,
                "diff_patch_encoding_unsupported",
                "Patch artifact must be UTF-8 text.",
            ) from error
        mutable_files = _parse_patch(patch)
        access = self.workspace_manager.acquire_for_worker(
            task_id=task_id,
            session_id="",
            worker_id="console-diff-review",
        )
        binding = self.workspace_manager.store.require_binding(access.workspace_id)
        try:
            task_root = self.workspace_manager.backend.mount_root(
                binding,
                WorkspaceKind.TASK,
            )
            dirty_state = WorkspaceDirtyStateRuntime(
                workspace_id=binding.workspace_id,
                workspace_root=task_root,
                ownership_store=self.workspace_manager.ownership_store,
                path_policy=self.workspace_manager.backend.path_policy,
            ).scan()
            dirty_state_error = ""
        except WorkspaceError as error:
            dirty_state = None
            dirty_state_error = str(getattr(error, "code", "dirty_state_unavailable"))
        port = WorkspaceEditPort(
            self.workspace_manager,
            access,
            worker_id="console-diff-review",
            run_id=run_id,
            task_id=task_id,
            node_id="console-diff-review",
            artifact_store=self.artifact_store,
            disabled=not self.enabled,
        )
        files: list[DiffFile] = []
        for parsed in mutable_files:
            read_path = parsed.old_path if parsed.kind in {"renamed", "deleted"} else parsed.path
            read = port.read_bytes(read_path)
            base = self._snapshot(read, binary=parsed.binary)
            if parsed.kind == "added" and base.exists:
                raise DiffReviewApiError(
                    409,
                    "diff_add_target_exists",
                    "Patch declares a new file that already exists.",
                    details={"path": parsed.path},
                )
            if parsed.kind != "added" and not base.exists:
                raise DiffReviewApiError(
                    409,
                    "diff_base_file_missing",
                    "Patch base file does not exist in the task workspace.",
                    details={"path": read_path},
                )
            if parsed.binary:
                proposed_text = ""
                proposed_sha256 = ""
            else:
                proposed_text = _apply_hunks(
                    base.text,
                    parsed.hunks,
                    ending=base.line_ending,
                )
                proposed_sha256 = _digest(proposed_text.encode(base.encoding))
            file_id = _stable_id("diff-file", parsed.old_path, parsed.path)
            normalized_hunks = tuple(
                DiffHunk(
                    hunk_id=item.hunk_id,
                    file_id=file_id,
                    index=item.index,
                    header=item.header,
                    section=item.section,
                    old_start=item.old_start,
                    old_count=item.old_count,
                    new_start=item.new_start,
                    new_count=item.new_count,
                    patch_start=item.patch_start,
                    patch_end=item.patch_end,
                    lines=item.lines,
                )
                for item in parsed.hunks
            )
            files.append(
                DiffFile(
                    file_id=file_id,
                    path=parsed.path,
                    previous_path=(
                        parsed.old_path if parsed.kind == "renamed" else ""
                    ),
                    kind=parsed.kind,
                    binary=parsed.binary,
                    patch_offset=parsed.patch_offset,
                    patch_bytes=max(0, parsed.patch_end - parsed.patch_offset),
                    hunks=normalized_hunks,
                    pages=_paginate_hunks(normalized_hunks),
                    base=base,
                    proposed_text=proposed_text,
                    proposed_sha256=proposed_sha256,
                    old_mode=parsed.old_mode or base.mode,
                    new_mode=parsed.new_mode or parsed.old_mode or base.mode,
                    language=_language(parsed.path),
                    mime_type=mimetypes.guess_type(parsed.path)[0] or "text/plain",
                    risk=self._risk_projection(
                        dirty_state,
                        parsed.path,
                        unavailable_reason=dirty_state_error,
                    ),
                )
            )
        diff_id = _stable_id(
            "diff",
            task_id,
            artifact.artifact_id,
            revision,
            binding.workspace_id,
            binding.owner_epoch,
            binding.binding_revision,
        )
        return DiffReviewSession(
            task_id=task_id,
            run_id=run_id,
            artifact_id=artifact.artifact_id,
            artifact_revision=revision,
            artifact_sha256=(
                artifact_sha256
                if str(artifact_sha256).startswith("sha256:")
                else f"sha256:{artifact_sha256}"
            ),
            diff_id=diff_id,
            workspace_id=binding.workspace_id,
            owner_epoch=binding.owner_epoch,
            binding_revision=binding.binding_revision,
            lease_id=binding.lease_id,
            generated_at=_now(),
            files=tuple(files),
            patch_bytes=len(patch_bytes),
        )

    @staticmethod
    def _risk_projection(
        dirty_state: Any,
        logical_path: str,
        *,
        unavailable_reason: str = "",
    ) -> dict[str, Any]:
        if dirty_state is None:
            return {
                "dirty": False,
                "nested_repository": False,
                "untracked_paths": 0,
                "modified_paths": 0,
                "staged_paths": 0,
                "conflicted_paths": 0,
                "repository_root": "",
                "reason_codes": [
                    f"git_boundary.{unavailable_reason or 'dirty_state_unavailable'}"
                ],
            }
        path = _logical_path(logical_path)
        repositories = tuple(dirty_state.repository_refs)
        repository = None
        for candidate in sorted(
            repositories,
            key=lambda item: len(str(item.relative_root)),
            reverse=True,
        ):
            root = str(candidate.relative_root)
            if root == "." or path == root or path.startswith(root + "/"):
                repository = candidate
                break
        repository_id = str(getattr(repository, "repository_id", ""))
        scoped = [
            item
            for item in dirty_state.paths
            if not repository_id or str(item.nested_repository_id) == repository_id
        ]
        untracked = sum(item.kind is WorkspaceDirtyKind.UNTRACKED for item in scoped)
        conflicted = sum(item.kind is WorkspaceDirtyKind.CONFLICTED for item in scoped)
        modified = sum(
            item.kind
            in {
                WorkspaceDirtyKind.MODIFIED,
                WorkspaceDirtyKind.ADDED,
                WorkspaceDirtyKind.DELETED,
                WorkspaceDirtyKind.RENAMED,
                WorkspaceDirtyKind.SUBMODULE,
            }
            for item in scoped
        )
        staged = sum(
            bool(str(item.metadata.get("status") or "").strip())
            and str(item.metadata.get("status") or "")[:1] not in {" ", "?", "!"}
            for item in scoped
        )
        nested = bool(repository and str(repository.relative_root) != ".")
        reason_codes: list[str] = []
        if scoped:
            reason_codes.append("git_boundary.dirty_worktree")
        if nested:
            reason_codes.append("git_boundary.nested_repository")
        if conflicted:
            reason_codes.append("git_boundary.conflicted_paths")
        return {
            "dirty": bool(scoped or getattr(repository, "dirty", False)),
            "nested_repository": nested,
            "untracked_paths": untracked,
            "modified_paths": modified,
            "staged_paths": staged,
            "conflicted_paths": conflicted,
            "repository_root": str(getattr(repository, "relative_root", "")),
            "reason_codes": reason_codes,
        }

    def _manifest_payload(self, session: DiffReviewSession) -> dict[str, Any]:
        files = [item.to_manifest_dict() for item in session.files]
        totals = {
            "files": len(files),
            "hunks": sum(item["hunk_count"] for item in files),
            "lines": sum(file.line_count for file in session.files),
            "additions": sum(item["additions"] for item in files),
            "deletions": sum(item["deletions"] for item in files),
            "binary_files": sum(bool(item["binary"]) for item in files),
            "renamed_files": sum(item["kind"] == "renamed" for item in files),
            "oversized_files": sum(bool(item["oversized"]) for item in files),
            "patch_bytes": session.patch_bytes,
        }
        return {
            "schema": DIFF_MANIFEST_SCHEMA,
            "diff_id": session.diff_id,
            "generated_at": session.generated_at,
            "source": {
                "task_id": session.task_id,
                "artifact_id": session.artifact_id,
                "artifact_revision": session.artifact_revision,
                "artifact_sha256": session.artifact_sha256,
                "run_id": session.run_id,
                "workspace_id": session.workspace_id,
                "owner_epoch": session.owner_epoch,
                "binding_revision": session.binding_revision,
                "lease_id": session.lease_id,
            },
            "files": files,
            "totals": totals,
            "maximum_page_bytes": DEFAULT_MAXIMUM_PAGE_BYTES,
            "maximum_page_lines": DEFAULT_MAXIMUM_PAGE_LINES,
            "physical_path_disclosed": False,
            "read_only": True,
        }

    def _require_session(
        self,
        *,
        task_id: str,
        run_id: str,
        artifact: ArtifactRef,
        revision: str,
    ) -> DiffReviewSession:
        self._require_enabled()
        selected_revision = _identity(revision, "artifact revision")
        session = self.registry.get(task_id, artifact.artifact_id, selected_revision)
        if session is None:
            response = self.manifest(
                task_id=task_id,
                run_id=run_id,
                artifact=artifact,
                revision=selected_revision,
            )
            diff_id = str(response.body.get("diff_id") or "")
            session = self.registry.find_diff(task_id, diff_id)
        if session is None:
            raise DiffReviewApiError(503, "diff_session_unavailable", "Diff session was not built.")
        if session.run_id != run_id:
            raise DiffReviewApiError(409, "diff_run_binding_mismatch", "Diff belongs to another run.")
        return session

    def _edit_port(self, session: DiffReviewSession) -> WorkspaceEditPort:
        access = self.workspace_manager.acquire_for_worker(
            task_id=session.task_id,
            session_id="",
            worker_id="console-diff-review",
        )
        return WorkspaceEditPort(
            self.workspace_manager,
            access,
            worker_id="console-diff-review",
            run_id=session.run_id,
            task_id=session.task_id,
            node_id="console-diff-review",
            artifact_store=self.artifact_store,
            disabled=not self.enabled,
        )

    @staticmethod
    def _snapshot(read: Any, *, binary: bool = False) -> FileSnapshot:
        if binary:
            return FileSnapshot(
                text="",
                byte_size=len(read.content),
                sha256=f"sha256:{read.evidence.content_hash}",
                mtime_ns=_wire_mtime(read.evidence.mtime_ns),
                mode=0,
                exists=read.exists,
                evidence=read.evidence,
                encoding="binary",
                line_ending="binary",
            )
        raw = bytes(read.content)
        evidence_encoding = str(read.evidence.encoding or "").strip().casefold()
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            candidates = ("utf-16",)
        elif raw.startswith(b"\xef\xbb\xbf"):
            candidates = ("utf-8-sig",)
        elif evidence_encoding in {
            "utf-8",
            "utf-8-sig",
            "utf-16",
            "utf-16-le",
            "utf-16-be",
            "cp1252",
            "windows-1252",
            "latin-1",
        }:
            candidates = (evidence_encoding, "utf-8", "cp1252")
        else:
            candidates = ("utf-8", "cp1252")
        text = ""
        encoding = ""
        for candidate in dict.fromkeys(candidates):
            try:
                text = raw.decode(candidate)
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if not encoding or "\x00" in text:
            raise DiffReviewApiError(
                415,
                "diff_workspace_encoding_unsupported",
                "Workspace file is binary or uses an unsupported text encoding.",
                details={"logical_path": read.logical_path},
            )
        return FileSnapshot(
            text=text,
            byte_size=len(read.content),
            sha256=f"sha256:{read.evidence.content_hash}",
            mtime_ns=_wire_mtime(read.evidence.mtime_ns),
            mode=0,
            exists=read.exists,
            evidence=read.evidence,
            encoding=encoding,
            line_ending=_line_ending(text),
        )

    def _validate_selection(
        self,
        session: DiffReviewSession,
        value: Any,
    ) -> dict[str, Any]:
        source = _mapping(value, "selection")
        file = session.file(_identity(source.get("file_id"), "selection file"))
        hunk_id = _identity(source.get("hunk_id"), "selection hunk")
        hunk = next((item for item in file.hunks if item.hunk_id == hunk_id), None)
        if hunk is None:
            raise DiffReviewApiError(404, "diff_selection_hunk_not_found", "Selection hunk was not found.")
        side = str(source.get("side") or "")
        if side not in {"old", "new"}:
            raise DiffReviewApiError(400, "diff_selection_side_invalid", "Selection side is invalid.")
        start = _integer(source.get("start_line"), "selection start", minimum=1)
        end = _integer(source.get("end_line"), "selection end", minimum=start)
        anchors = [_identity(item, "selection anchor") for item in _sequence(source.get("anchor_line_ids"))]
        if len(anchors) != end - start + 1 or len(set(anchors)) != len(anchors):
            raise DiffReviewApiError(
                400,
                "diff_selection_anchor_mismatch",
                "Selection anchors do not match the line range.",
            )
        visible = {
            item.line_id
            for item in hunk.lines
            if (
                item.old_line is not None
                if side == "old"
                else item.new_line is not None
            )
        }
        if any(item not in visible for item in anchors):
            raise DiffReviewApiError(
                409,
                "diff_selection_anchor_stale",
                "Selection includes an anchor outside its reviewed hunk.",
            )
        return {
            "file_id": file.file_id,
            "hunk_id": hunk.hunk_id,
            "side": side,
            "start_line": start,
            "end_line": end,
            "anchor_line_ids": anchors,
        }

    def _validate_apply_identity(
        self,
        session: DiffReviewSession,
        payload: Mapping[str, Any],
    ) -> None:
        expected = {
            "diff_id": session.diff_id,
            "artifact_id": session.artifact_id,
            "workspace_id": session.workspace_id,
            "expected_owner_epoch": session.owner_epoch,
            "expected_binding_revision": session.binding_revision,
            "expected_lease_id": session.lease_id,
        }
        actual = {
            "diff_id": payload.get("diff_id"),
            "artifact_id": payload.get("artifact_id"),
            "workspace_id": payload.get("workspace_id"),
            "expected_owner_epoch": payload.get("expected_owner_epoch"),
            "expected_binding_revision": payload.get("expected_binding_revision"),
            "expected_lease_id": payload.get("expected_lease_id"),
        }
        if actual != expected:
            raise DiffReviewApiError(
                409,
                "diff_apply_binding_stale",
                "Patch request no longer matches the reviewed workspace binding.",
                details={"expected": expected, "actual": actual},
            )
        review_revision = _integer(payload.get("review_revision"), "review revision")
        if review_revision != session.review_revision:
            raise DiffReviewApiError(
                409,
                "diff_apply_review_revision_stale",
                "Review changed after patch preflight.",
                details={"expected": session.review_revision, "actual": review_revision},
            )

    @staticmethod
    def _selected_file_ids(session: DiffReviewSession, value: Any) -> list[str]:
        selected = [_identity(item, "selected file") for item in _sequence(value)]
        if not selected or len(selected) != len(set(selected)):
            raise DiffReviewApiError(
                400,
                "diff_apply_selection_invalid",
                "Patch apply requires a non-empty unique file selection.",
            )
        for file_id in selected:
            file = session.file(file_id)
            if file.binary:
                raise DiffReviewApiError(
                    415,
                    "diff_apply_binary_rejected",
                    "Binary files cannot use the text patch transaction.",
                )
        return selected

    @staticmethod
    def _preconditions(
        session: DiffReviewSession,
        selected_ids: Sequence[str],
        value: Any,
    ) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        for raw in _sequence(value):
            source = _mapping(raw, "patch precondition")
            file_id = _identity(source.get("file_id"), "precondition file")
            if file_id in entries:
                raise DiffReviewApiError(
                    400,
                    "diff_apply_precondition_duplicate",
                    "Patch precondition repeats a file.",
                )
            file = session.file(file_id)
            path = _logical_path(source.get("path"), "precondition path")
            if path != file.path:
                raise DiffReviewApiError(
                    409,
                    "diff_apply_path_mismatch",
                    "Patch precondition path differs from the manifest.",
                )
            base_sha256 = _sha256(source.get("base_sha256"), "base sha256")
            current_sha256 = _sha256(source.get("current_sha256"), "current sha256")
            proposed_sha256 = _sha256(source.get("proposed_sha256"), "proposed sha256")
            if base_sha256 != file.base.sha256:
                raise DiffReviewApiError(
                    409,
                    "diff_apply_base_digest_mismatch",
                    "Patch base digest differs from the reviewed snapshot.",
                )
            encoding = str(source.get("encoding") or "")
            if encoding not in {"utf-8", "utf-8-sig"}:
                raise DiffReviewApiError(
                    415,
                    "diff_apply_encoding_unsupported",
                    "Patch transaction only supports verified UTF-8 content.",
                )
            line_ending = str(source.get("line_ending") or "")
            if line_ending not in {"lf", "crlf", "cr", "mixed", "none"}:
                raise DiffReviewApiError(
                    400,
                    "diff_apply_line_ending_invalid",
                    "Patch precondition line ending is invalid.",
                )
            if bool(source.get("binary")):
                raise DiffReviewApiError(
                    415,
                    "diff_apply_binary_rejected",
                    "Binary precondition cannot enter text patch apply.",
                )
            entries[file_id] = {
                "file_id": file_id,
                "path": path,
                "previous_path": str(source.get("previous_path") or ""),
                "kind": str(source.get("kind") or ""),
                "base_sha256": base_sha256,
                "current_sha256": current_sha256,
                "proposed_sha256": proposed_sha256,
                "base_mtime_ns": _integer(source.get("base_mtime_ns"), "base mtime"),
                "current_mtime_ns": _integer(source.get("current_mtime_ns"), "current mtime"),
                "base_mode": _integer(source.get("base_mode"), "base mode", maximum=0o7777),
                "current_mode": _integer(
                    source.get("current_mode"), "current mode", maximum=0o7777
                ),
                "encoding": encoding,
                "line_ending": line_ending,
                "binary": False,
            }
        if set(entries) != set(selected_ids):
            raise DiffReviewApiError(
                400,
                "diff_apply_precondition_set_mismatch",
                "Selected files and precondition files differ.",
            )
        return entries

    @staticmethod
    def _permission_material(
        *,
        session: DiffReviewSession,
        payload: Mapping[str, Any],
        operation: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "run_id": session.run_id,
            "task_id": session.task_id,
            "session_id": _identity(payload.get("session_id"), "permission session"),
            "session_revision": _integer(
                payload.get("session_revision"), "permission session revision"
            ),
            "worker_request_id": _identity(
                payload.get("worker_request_id"), "permission worker request"
            ),
            "tool_call_id": _identity(payload.get("tool_call_id"), "permission tool call"),
            "tool_name": "workspace.patch-review",
            "namespace": "builtin",
            "server_id": "",
            "operation": operation,
            "workspace_root": ".",
            "arguments": dict(arguments),
            "metadata": {
                "canonical_workspace_owner": "WorkspacePatchTransactionRuntime",
                "canonical_permission_owner": "typescript.PermissionCoordinator",
                "python_decision_fallback": False,
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "openWorldHint": False,
                    "idempotentHint": True,
                },
            },
            "await_approval_delivery": True,
        }

    def _permission_decision(
        self,
        material: Mapping[str, Any],
        *,
        permit_id: str,
    ) -> dict[str, Any]:
        request = dict(material)
        if permit_id:
            request["permit_id"] = _identity(permit_id, "permission permit")
        claimed = dict(self.permission_port.permission_claim(request))
        if claimed.get("claimed") is True:
            response = claimed
        else:
            response = dict(self.permission_port.permission_enforce(request))
        canonical = str(
            response.get("canonical_owner")
            or response.get("canonicalOwner")
            or ""
        )
        decision = dict(response.get("decision") or {})
        decision_owner = str(
            decision.get("canonical_owner")
            or decision.get("canonicalOwner")
            or ""
        )
        if "typescript" not in (canonical or decision_owner).lower():
            raise DiffReviewApiError(
                503,
                "diff_permission_owner_invalid",
                "Patch permission response is not TypeScript-owned.",
            )
        effect = str(decision.get("effect") or response.get("effect") or "").lower()
        if effect not in {"allow", "ask", "deny"}:
            raise DiffReviewApiError(
                503,
                "diff_permission_effect_invalid",
                "Patch permission owner returned an invalid effect.",
            )
        fingerprint = str(
            response.get("request_fingerprint")
            or decision.get("request_fingerprint")
            or _stable_id("permission-fingerprint", material)
        )
        return {
            "canonical_owner": "typescript.PermissionCoordinator",
            "effect": effect,
            "decision_id": str(
                decision.get("decision_id")
                or decision.get("decisionId")
                or _stable_id("permission-decision", fingerprint, effect)
            ),
            "request_id": str(
                decision.get("request_id")
                or decision.get("requestId")
                or response.get("request_id")
                or ""
            )
            or None,
            "permit_id": str(
                response.get("permit_id")
                or decision.get("permit_id")
                or permit_id
                or ""
            )
            or None,
            "reason_code": str(
                decision.get("reason_code")
                or decision.get("reasonCode")
                or f"permission.{effect}"
            ),
            "policy_revision": int(
                decision.get("policy_revision")
                or decision.get("policyRevision")
                or 0
            ),
            "mode_revision": int(
                decision.get("mode_revision")
                or decision.get("modeRevision")
                or 0
            ),
            "request_fingerprint": fingerprint,
            "pending": effect == "ask",
            "recovery_input": (
                dict(response.get("recovery_input") or {})
                if isinstance(response.get("recovery_input"), Mapping)
                else None
            ),
        }

    @staticmethod
    def _patch_receipt(
        *,
        session: DiffReviewSession,
        payload: Mapping[str, Any],
        phase: str,
        permission: Mapping[str, Any],
        reason_code: str,
        message: str,
        transaction_id: str = "",
        owner_epoch_before: int | None = None,
        owner_epoch_after: int | None = None,
        binding_revision_before: int | None = None,
        binding_revision_after: int | None = None,
        snapshot_id: str = "",
        recovery_input_id: str = "",
        path_results: Sequence[Mapping[str, Any]] = (),
        artifact_refs: Sequence[str] = (),
        verification_refs: Sequence[str] = (),
        terminal_refs: Sequence[str] = (),
        timeline_refs: Sequence[str] = (),
        idempotent_replay: bool = False,
        denied_manual_mutation_count: int = 0,
        rollback_failed: bool | None = None,
    ) -> dict[str, Any]:
        causation_id = _identity(payload.get("causation_id"), "causation")
        selected_transaction = transaction_id or _stable_id(
            "workspace-txn-pending",
            session.diff_id,
            causation_id,
            payload.get("idempotency_key"),
        )
        failed_rollback = (
            phase in {"rollback_failed", "quarantined"}
            if rollback_failed is None
            else bool(rollback_failed)
        )
        created_at = _now()
        return {
            "schema": PATCH_RECEIPT_SCHEMA,
            "receipt_id": _stable_id(
                "patch-receipt",
                session.diff_id,
                selected_transaction,
                phase,
                causation_id,
            ),
            "task_id": session.task_id,
            "run_id": session.run_id,
            "diff_id": session.diff_id,
            "transaction_id": selected_transaction,
            "phase": phase,
            "accepted": phase in {"preflight", "permission_pending", "committed", "rolled_back"},
            "committed": phase == "committed",
            "idempotent_replay": bool(idempotent_replay),
            "stale": phase == "stale",
            "conflict": phase == "conflicted",
            "rolled_back": phase == "rolled_back",
            "rollback_failed": failed_rollback,
            "sealed": bool(payload.get("sealed")),
            "human_intervention_count": 0,
            "denied_manual_mutation_count": max(0, int(denied_manual_mutation_count)),
            "workspace_id": session.workspace_id,
            "owner_epoch_before": (
                session.owner_epoch if owner_epoch_before is None else owner_epoch_before
            ),
            "owner_epoch_after": (
                session.owner_epoch if owner_epoch_after is None else owner_epoch_after
            ),
            "binding_revision_before": (
                session.binding_revision
                if binding_revision_before is None
                else binding_revision_before
            ),
            "binding_revision_after": (
                session.binding_revision
                if binding_revision_after is None
                else binding_revision_after
            ),
            "snapshot_id": snapshot_id,
            "recovery_input_id": recovery_input_id,
            "reason_code": _identity(reason_code.replace(" ", "_"), "reason code"),
            "message": str(message),
            "path_results": [dict(item) for item in path_results],
            "artifact_refs": [_identity(item, "artifact reference") for item in artifact_refs],
            "event_ids": [],
            "verification_refs": [
                _identity(item, "verification reference") for item in verification_refs
            ],
            "terminal_refs": [_identity(item, "terminal reference") for item in terminal_refs],
            "timeline_refs": [_identity(item, "timeline reference") for item in timeline_refs],
            "permission": dict(permission),
            "causation_id": causation_id,
            "created_at": created_at,
        }

    @staticmethod
    def _path_result(
        file: DiffFile,
        read: Any,
        *,
        after_sha256: str,
        bytes_after: int,
        disposition: str,
        verified: bool,
        path: str | None = None,
    ) -> dict[str, Any]:
        return {
            "file_id": file.file_id,
            "path": path or file.path,
            "disposition": _identity(disposition, "path disposition"),
            "before_sha256": f"sha256:{read.evidence.content_hash}" if read.exists else "",
            "after_sha256": after_sha256,
            "bytes_before": len(read.content),
            "bytes_after": max(0, int(bytes_after)),
            "verified": bool(verified),
        }

    def _workspace_failure_response(
        self,
        *,
        session: DiffReviewSession,
        payload: Mapping[str, Any],
        permission: Mapping[str, Any],
        idempotency_key: str,
        error: WorkspaceError,
    ) -> DiffApiResponse:
        code = str(getattr(error, "code", "workspace_patch_failed"))
        lowered = code.lower()
        if "stale" in lowered or "evidence" in lowered or "binding" in lowered:
            phase = "stale"
        elif "conflict" in lowered:
            phase = "conflicted"
        elif "quarantine" in lowered or "recovery" in lowered:
            phase = "quarantined"
        else:
            phase = "failed"
        detail = getattr(error, "detail", None)
        detail_map = dict(detail) if isinstance(detail, Mapping) else {}
        receipt = self._patch_receipt(
            session=session,
            payload=payload,
            phase=phase,
            permission=permission,
            transaction_id=str(
                detail_map.get("transaction_id")
                or _stable_id("workspace-txn-failed", session.diff_id, idempotency_key)
            ),
            snapshot_id=str(detail_map.get("snapshot_id") or ""),
            recovery_input_id=str(detail_map.get("recovery_input_id") or ""),
            reason_code=f"workspace.{code}",
            message=str(error),
            rollback_failed=phase == "quarantined",
        )
        event = self._patch_event(session, receipt)
        receipt["event_ids"] = [event.event_id]
        self._remember_transaction_receipt(idempotency_key, receipt)
        return DiffApiResponse(HTTPStatus.CONFLICT, receipt, events=(event,))

    def _remember_transaction_receipt(
        self,
        idempotency_key: str,
        receipt: Mapping[str, Any],
        *,
        replace: bool = False,
    ) -> None:
        with self._receipt_guard:
            existing = self._transaction_receipts.get(idempotency_key)
            if existing is not None and not replace:
                if (
                    existing.get("diff_id") != receipt.get("diff_id")
                    or existing.get("causation_id") != receipt.get("causation_id")
                ):
                    raise DiffReviewApiError(
                        409,
                        "diff_patch_idempotency_conflict",
                        "Patch idempotency key changed exact-call identity.",
                    )
                return
            self._transaction_receipts[idempotency_key] = dict(receipt)

    @staticmethod
    def _review_event(
        *,
        session: DiffReviewSession,
        action: str,
        causation_id: str,
        selection: Mapping[str, Any],
        comment: Mapping[str, Any] | None,
        accepted: bool,
        permission: Mapping[str, Any],
    ) -> EventRecord:
        return EventRecord(
            event_id=new_id("event"),
            event_type=EventType.SYSTEM_NOTICE,
            run_id=session.run_id,
            task_id=session.task_id,
            payload={
                "diff_review": {
                    "schema": "zyra.diff-review-event.v1",
                    "action": action,
                    "diff_id": session.diff_id,
                    "artifact_id": session.artifact_id,
                    "artifact_revision": session.artifact_revision,
                    "review_revision": session.review_revision,
                    "selection": dict(selection),
                    "comment": dict(comment) if comment else None,
                    "accepted": bool(accepted),
                    "permission": dict(permission),
                    "human_intervention_count": 0,
                    "state_owner": "canonical event log",
                    "physical_path_disclosed": False,
                },
                "correlation_id": session.diff_id,
                "causation_id": causation_id,
            },
        )

    @staticmethod
    def _patch_event(
        session: DiffReviewSession,
        receipt: Mapping[str, Any],
    ) -> EventRecord:
        return EventRecord(
            event_id=new_id("event"),
            event_type=EventType.SYSTEM_NOTICE,
            run_id=session.run_id,
            task_id=session.task_id,
            payload={
                "diff_patch_transaction": {
                    "schema": "zyra.diff-patch-event.v1",
                    "diff_id": session.diff_id,
                    "artifact_id": session.artifact_id,
                    "artifact_revision": session.artifact_revision,
                    "transaction_id": receipt.get("transaction_id"),
                    "phase": receipt.get("phase"),
                    "reason_code": receipt.get("reason_code"),
                    "snapshot_id": receipt.get("snapshot_id"),
                    "verification_refs": list(receipt.get("verification_refs") or []),
                    "terminal_refs": list(receipt.get("terminal_refs") or []),
                    "permission": dict(receipt.get("permission") or {}),
                    "human_intervention_count": 0,
                    "state_owner": (
                        "WorkspacePatchTransactionRuntime + "
                        "typescript.PermissionCoordinator + canonical event log"
                    ),
                },
                "correlation_id": str(
                    receipt.get("transaction_id") or session.diff_id
                ),
                "causation_id": str(receipt.get("causation_id") or ""),
            },
        )

    @staticmethod
    def _require_schema(payload: Mapping[str, Any], expected: str) -> None:
        if payload.get("schema") != expected:
            raise DiffReviewApiError(400, "diff_request_schema_invalid", "Patch request schema is invalid.")

    @staticmethod
    def _require_request_binding(
        payload: Mapping[str, Any],
        *,
        task_id: str,
        run_id: str,
    ) -> None:
        if payload.get("task_id") != task_id or payload.get("run_id") != run_id:
            raise DiffReviewApiError(
                409,
                "diff_request_binding_mismatch",
                "Patch request task/run identity differs from the route.",
            )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise DiffReviewApiError(
                503,
                "diff_review_disabled",
                "Diff/patch integration is disabled; no fallback owner is allowed.",
            )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DiffReviewApiError(400, "diff_object_required", f"{label} must be an object.")
    return dict(value)


def _sequence(value: Any) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise DiffReviewApiError(400, "diff_array_required", "Expected an array.")
    return list(value)


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 2**53 - 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise DiffReviewApiError(
            400,
            "diff_integer_invalid",
            f"{label} must be an integer from {minimum} through {maximum}.",
        )
    return int(value)


def _sha256(value: Any, label: str) -> str:
    text = str(value or "").lower()
    if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", text):
        raise DiffReviewApiError(400, "diff_sha256_invalid", f"{label} is invalid.")
    return text if text.startswith("sha256:") else f"sha256:{text}"
