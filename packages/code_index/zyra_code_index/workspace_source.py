from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import stat
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    CodeFile,
    DiscoveryPage,
    FileDiscoveryQuery,
    FileDisposition,
    PathPolicyError,
    StaleWorkspaceError,
    WorkspaceIdentity,
    stable_digest,
)


DEFAULT_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "bower_components",
        "vendor",
        "vendor-runtimes",
        "runtime-sources",
        "source-pool",
        "third_party",
        "dist",
        "build",
        "coverage",
        ".next",
        ".nuxt",
        ".turbo",
        "target",
        "out",
        "bin",
        "obj",
    }
)


TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".json",
        ".kt",
        ".kts",
        ".lua",
        ".md",
        ".mjs",
        ".php",
        ".ps1",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".scala",
        ".scss",
        ".sh",
        ".sql",
        ".svelte",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)


LANGUAGE_BY_SUFFIX: Mapping[str, str] = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".json": "json",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".lua": "lua",
    ".md": "markdown",
    ".mjs": "javascript",
    ".php": "php",
    ".ps1": "powershell",
    ".py": "python",
    ".pyi": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scala": "scala",
    ".scss": "scss",
    ".sh": "shell",
    ".sql": "sql",
    ".svelte": "svelte",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".vue": "vue",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


GENERATED_PATTERNS = (
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "*.generated.*",
    "*_generated.*",
    "*.g.cs",
    "*.pb.go",
    "*.pb.cc",
    "*.designer.cs",
)


def _normalize_case(value: str) -> str:
    return os.path.normcase(value).casefold()


class WorkspacePathPolicy:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise PathPolicyError("workspace root is not a directory")
        self.root_case = _normalize_case(str(self.root))

    def normalize_logical(self, logical_path: str) -> str:
        raw = str(logical_path)
        if not raw or "\x00" in raw:
            raise PathPolicyError("logical path is empty or contains NUL")
        if len(raw) > 32_768:
            raise PathPolicyError("logical path exceeds platform-safe length")
        if raw.startswith(("\\\\", "//", "\\?\\", "\\.\\")):
            raise PathPolicyError("UNC and device paths are not workspace logical paths")
        if re.match(r"^[A-Za-z]:", raw):
            raise PathPolicyError("drive-qualified paths are forbidden")
        if ":" in raw:
            raise PathPolicyError("colon and NTFS alternate data stream paths are forbidden")
        normalized = raw.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute():
            raise PathPolicyError("absolute paths are forbidden")
        parts = pure.parts
        if any(part in {"", ".", ".."} for part in parts):
            raise PathPolicyError("path traversal and ambiguous segments are forbidden")
        if any(part.endswith((" ", ".")) for part in parts):
            raise PathPolicyError("Windows-trimmed path segments are forbidden")
        if any(self._reserved_windows_name(part) for part in parts):
            raise PathPolicyError("reserved Windows device name is forbidden")
        return "/".join(parts)

    def resolve(self, logical_path: str, *, require_exists: bool = True) -> Path:
        normalized = self.normalize_logical(logical_path)
        candidate = self.root.joinpath(*normalized.split("/"))
        self._reject_symlink_components(candidate, allow_missing=not require_exists)
        try:
            resolved = candidate.resolve(strict=require_exists)
        except FileNotFoundError:
            if require_exists:
                raise
            resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise PathPolicyError("path escapes workspace root") from error
        if not _normalize_case(str(resolved)).startswith(self.root_case):
            raise PathPolicyError("case-normalized path escapes workspace root")
        return resolved

    def logical_from_physical(self, path: str | Path) -> str:
        candidate = Path(path)
        self._reject_symlink_components(candidate, allow_missing=False)
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as error:
            raise PathPolicyError("physical path is outside workspace root") from error
        return self.normalize_logical(relative.as_posix())

    def _reject_symlink_components(self, candidate: Path, *, allow_missing: bool) -> None:
        current = self.root
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise PathPolicyError("candidate is outside workspace root") from error
        for part in relative.parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                if allow_missing:
                    return
                raise
            if stat.S_ISLNK(mode):
                raise PathPolicyError("symlink traversal is forbidden")
            if hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT"):
                attributes = getattr(current.lstat(), "st_file_attributes", 0)
                if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise PathPolicyError("Windows reparse point traversal is forbidden")

    @staticmethod
    def _reserved_windows_name(part: str) -> bool:
        stem = part.split(".", 1)[0].casefold()
        return stem in {
            "con",
            "prn",
            "aux",
            "nul",
            "com1",
            "com2",
            "com3",
            "com4",
            "com5",
            "com6",
            "com7",
            "com8",
            "com9",
            "lpt1",
            "lpt2",
            "lpt3",
            "lpt4",
            "lpt5",
            "lpt6",
            "lpt7",
            "lpt8",
            "lpt9",
        }


@dataclass(frozen=True, slots=True)
class BoundWorkspaceSource:
    identity: WorkspaceIdentity
    policy: WorkspacePathPolicy

    @classmethod
    def from_manager(cls, manager: Any, handle: Any) -> "BoundWorkspaceSource":
        root = manager.internal_task_root(handle)
        workspace_id = str(getattr(handle, "workspace_id", ""))
        if not workspace_id:
            raise StaleWorkspaceError("workspace access handle has no workspace_id")
        projection = manager.project(workspace_id)
        if int(getattr(handle, "owner_epoch", 0)) != int(projection.owner_epoch):
            raise StaleWorkspaceError("workspace access owner epoch is stale")
        identity = WorkspaceIdentity(
            workspace_id=workspace_id,
            task_id=str(projection.task_id),
            run_id=str(projection.run_id),
            session_id=str(projection.session_id),
            root=str(root),
            owner_epoch=int(projection.owner_epoch),
            binding_revision=int(projection.binding_revision),
            lease_id=str(getattr(handle, "lease_id", "")),
            backend_id=str(projection.backend_id),
        )
        return cls(identity=identity, policy=WorkspacePathPolicy(root))

    @classmethod
    def from_manager_snapshot(
        cls,
        manager: Any,
        workspace_id: str,
        *,
        expected_revision: str = "",
    ) -> "BoundWorkspaceSource":
        """Open a fenced, read-only view without acquiring or rotating a lease.

        Code indexing is derived work admitted from an already-authorized 05B
        workspace revision.  A worker process must therefore validate that
        revision, but it must not become the canonical workspace owner merely
        because it needs to read files.  The physical root is reconstructed
        from the manager backend and is never written to a code-index job or
        checkpoint reference.
        """

        workspace = workspace_id.strip()
        if not workspace:
            raise StaleWorkspaceError("workspace_id is required for an index snapshot")
        binding = manager.store.require_binding(workspace)
        if str(getattr(binding.lifecycle_state, "value", binding.lifecycle_state)) in {
            "deleted",
            "cleanup_pending",
            "recovery_required",
        }:
            raise StaleWorkspaceError("workspace is not readable for code indexing")
        root = manager.backend.mount_root(binding, getattr(binding, "workspace_kind"))
        identity = WorkspaceIdentity(
            workspace_id=binding.workspace_id,
            task_id=binding.task_id,
            run_id=binding.run_id,
            session_id=binding.session_id,
            root=str(root),
            owner_epoch=int(binding.owner_epoch),
            binding_revision=int(binding.binding_revision),
            lease_id=str(binding.lease_id),
            backend_id=str(binding.backend_id),
        )
        if expected_revision and identity.revision != expected_revision:
            raise StaleWorkspaceError(
                "canonical workspace revision changed before the index snapshot opened"
            )
        return cls(identity=identity, policy=WorkspacePathPolicy(root))

    @classmethod
    def for_test(
        cls,
        root: str | Path,
        *,
        workspace_id: str = "test-workspace",
        task_id: str = "test-task",
        run_id: str = "test-run",
        session_id: str = "test-session",
        owner_epoch: int = 1,
        binding_revision: int = 1,
    ) -> "BoundWorkspaceSource":
        resolved = Path(root).resolve(strict=True)
        return cls(
            identity=WorkspaceIdentity(
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                session_id=session_id,
                root=str(resolved),
                owner_epoch=owner_epoch,
                binding_revision=binding_revision,
                lease_id="test-lease",
                backend_id="test-local",
            ),
            policy=WorkspacePathPolicy(resolved),
        )

    def read_bytes(self, logical_path: str, *, maximum_bytes: int) -> bytes:
        path = self.policy.resolve(logical_path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PathPolicyError("code index reads regular files only")
        if info.st_size > maximum_bytes:
            raise ValueError("file exceeds read budget")
        before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        with path.open("rb") as handle:
            content = handle.read(maximum_bytes + 1)
        if len(content) > maximum_bytes:
            raise ValueError("file grew beyond read budget")
        after_info = path.lstat()
        after = (after_info.st_dev, after_info.st_ino, after_info.st_size, after_info.st_mtime_ns)
        if before != after:
            raise StaleWorkspaceError("workspace file changed while indexing")
        return content

    def read_text(self, logical_path: str, *, maximum_bytes: int) -> str:
        content = self.read_bytes(logical_path, maximum_bytes=maximum_bytes)
        if is_binary(content):
            raise UnicodeError("binary file is not code-indexable")
        return content.decode("utf-8", errors="replace")


class IgnoreMatcher:
    def __init__(
        self,
        *,
        root: Path,
        extra_patterns: Sequence[str] = (),
        include_generated: bool = False,
        include_vendor: bool = False,
    ) -> None:
        self.root = root
        self.include_generated = include_generated
        self.include_vendor = include_vendor
        self.patterns: list[tuple[str, bool, bool]] = []
        for filename in (".gitignore", ".ignore", ".zyraignore"):
            path = root / filename
            if path.is_file() and not path.is_symlink():
                self._load(path.read_text(encoding="utf-8", errors="replace").splitlines())
        self._load(extra_patterns)

    def _load(self, lines: Iterable[str]) -> None:
        for raw in lines:
            value = str(raw).strip()
            if not value or value.startswith("#"):
                continue
            negated = value.startswith("!")
            if negated:
                value = value[1:]
            directory_only = value.endswith("/")
            value = value.strip("/")
            if value:
                self.patterns.append((value, negated, directory_only))

    def disposition(self, logical_path: str, *, is_directory: bool) -> FileDisposition | None:
        pure = PurePosixPath(logical_path)
        parts = pure.parts
        if not self.include_vendor and any(part.casefold() in DEFAULT_IGNORED_DIRECTORIES for part in parts):
            return FileDisposition.VENDOR if any("vendor" in part.casefold() for part in parts) else FileDisposition.IGNORED
        name = pure.name
        if not self.include_generated and any(fnmatch.fnmatchcase(name.casefold(), pattern.casefold()) for pattern in GENERATED_PATTERNS):
            return FileDisposition.GENERATED
        ignored = False
        for pattern, negated, directory_only in self.patterns:
            if directory_only and not is_directory:
                continue
            if self._matches(logical_path, pattern):
                ignored = not negated
        return FileDisposition.IGNORED if ignored else None

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        if "/" not in pattern:
            return any(fnmatch.fnmatchcase(part, pattern) for part in PurePosixPath(path).parts)
        return fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern)


class FileDiscoveryRuntime:
    def __init__(self, source: BoundWorkspaceSource) -> None:
        self.source = source

    def discover(self, query: FileDiscoveryQuery, *, generation: int) -> DiscoveryPage:
        request = query.validated()
        started = time.monotonic()
        matcher = IgnoreMatcher(
            root=self.source.policy.root,
            extra_patterns=request.exclude_globs,
            include_generated=request.include_generated,
            include_vendor=request.include_vendor,
        )
        cursor_after = self._decode_cursor(request.cursor, request) if request.cursor else ""
        pending: deque[tuple[Path, int]] = deque([(self.source.policy.root, 0)])
        discovered: list[CodeFile] = []
        scanned_files = 0
        scanned_directories = 0
        scanned_bytes = 0
        ignored = 0
        warnings: list[str] = []
        truncated = False

        while pending:
            if (time.monotonic() - started) * 1000.0 > request.budget.deadline_ms:
                warnings.append("discovery_deadline_reached")
                truncated = True
                break
            directory, depth = pending.popleft()
            if depth > request.budget.maximum_depth:
                ignored += 1
                continue
            scanned_directories += 1
            if scanned_directories > request.budget.maximum_directories:
                warnings.append("directory_budget_reached")
                truncated = True
                break
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
            except OSError:
                warnings.append(f"unreadable_directory:{self._logical_safe(directory)}")
                continue
            for entry in entries:
                try:
                    logical = self.source.policy.logical_from_physical(entry.path)
                except (PathPolicyError, FileNotFoundError):
                    ignored += 1
                    continue
                if len(logical) > request.budget.maximum_path_chars:
                    ignored += 1
                    continue
                if not request.include_hidden and any(part.startswith(".") for part in PurePosixPath(logical).parts):
                    ignored += 1
                    continue
                try:
                    if entry.is_symlink():
                        ignored += 1
                        continue
                    is_directory = entry.is_dir(follow_symlinks=False)
                    disposition = matcher.disposition(logical, is_directory=is_directory)
                    if disposition is not None:
                        ignored += 1
                        continue
                    if is_directory:
                        pending.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        ignored += 1
                        continue
                    scanned_files += 1
                    if scanned_files > request.budget.maximum_files:
                        warnings.append("file_budget_reached")
                        truncated = True
                        pending.clear()
                        break
                    info = entry.stat(follow_symlinks=False)
                    if info.st_size > request.budget.maximum_file_bytes:
                        ignored += 1
                        continue
                    scanned_bytes += int(info.st_size)
                    if scanned_bytes > request.budget.maximum_total_bytes:
                        warnings.append("byte_budget_reached")
                        truncated = True
                        pending.clear()
                        break
                    suffix = Path(entry.name).suffix.casefold()
                    if request.include_suffixes and suffix not in {item.casefold() for item in request.include_suffixes}:
                        continue
                    if suffix not in TEXT_SUFFIXES and entry.name not in {"Dockerfile", "Makefile", "Justfile"}:
                        ignored += 1
                        continue
                    if not self._glob_match(logical, request.globs):
                        continue
                    if cursor_after and logical <= cursor_after:
                        continue
                    content = self.source.read_bytes(logical, maximum_bytes=request.budget.maximum_file_bytes)
                    if is_binary(content):
                        ignored += 1
                        continue
                    text = content.decode("utf-8", errors="replace")
                    digest = hashlib.sha256(content).hexdigest()
                    discovered.append(
                        CodeFile(
                            workspace_id=self.source.identity.workspace_id,
                            logical_path=logical,
                            language=LANGUAGE_BY_SUFFIX.get(suffix, "text"),
                            suffix=suffix,
                            size_bytes=len(content),
                            mtime_ns=int(info.st_mtime_ns),
                            content_hash=digest,
                            source_revision=f"file:{logical}:{digest}",
                            generation=generation,
                            line_count=text.count("\n") + (1 if text else 0),
                            metadata={
                                "workspace_revision": self.source.identity.revision,
                                "physical_path_public": False,
                            },
                        )
                    )
                except (OSError, UnicodeError, ValueError):
                    ignored += 1
                    continue

        discovered.sort(key=lambda item: item.logical_path)
        page_items = discovered[: request.page_size]
        has_more = len(discovered) > len(page_items) or truncated or bool(pending)
        next_cursor = self._encode_cursor(page_items[-1].logical_path, request) if page_items and has_more else ""
        return DiscoveryPage(
            files=tuple(page_items),
            next_cursor=next_cursor,
            scanned_files=scanned_files,
            scanned_directories=scanned_directories,
            scanned_bytes=scanned_bytes,
            ignored_count=ignored,
            truncated=has_more,
            warnings=tuple(warnings),
        )

    def _encode_cursor(self, logical_path: str, query: FileDiscoveryQuery) -> str:
        body = {
            "after": logical_path,
            "workspace_revision": self.source.identity.revision,
            "query": self._query_digest(query),
        }
        body["checksum"] = stable_digest(body)
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: str, query: FileDiscoveryQuery) -> str:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            body = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except Exception as error:  # noqa: BLE001
            raise PathPolicyError("invalid discovery cursor") from error
        checksum = str(body.pop("checksum", ""))
        if checksum != stable_digest(body):
            raise PathPolicyError("discovery cursor checksum mismatch")
        if body.get("workspace_revision") != self.source.identity.revision:
            raise StaleWorkspaceError("discovery cursor belongs to a stale workspace revision")
        if body.get("query") != self._query_digest(query):
            raise PathPolicyError("discovery cursor belongs to another query")
        return self.source.policy.normalize_logical(str(body.get("after") or ""))

    @staticmethod
    def _query_digest(query: FileDiscoveryQuery) -> str:
        return stable_digest(
            query.globs,
            query.include_suffixes,
            query.exclude_globs,
            query.include_hidden,
            query.include_generated,
            query.include_vendor,
            query.page_size,
            query.budget,
        )

    @staticmethod
    def _glob_match(logical: str, patterns: Sequence[str]) -> bool:
        pure = PurePosixPath(logical)
        for pattern in patterns:
            if pattern in {"*", "**", "**/*"}:
                return True
            if pure.match(pattern) or fnmatch.fnmatchcase(logical, pattern):
                return True
        return False

    def _logical_safe(self, path: Path) -> str:
        try:
            return self.source.policy.logical_from_physical(path)
        except Exception:  # noqa: BLE001
            return "<redacted>"


def is_binary(content: bytes) -> bool:
    if not content:
        return False
    sample = content[:8192]
    if b"\x00" in sample:
        return True
    control = sum(1 for value in sample if value < 9 or 13 < value < 32)
    return control / len(sample) > 0.10
