from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .model import (
    AuditSection,
    Disposition,
    Evidence,
    EvidenceKind,
    Finding,
    RuleSwitches,
    Severity,
    content_digest,
    finding,
    relative_path,
    section,
)


DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".coverage",
        ".next",
        ".turbo",
        ".cache",
        ".tmp",
        "tmp",
    }
)
SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".cs",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
        ".swift",
        ".sh",
        ".bash",
        ".ps1",
    }
)
CONFIG_SUFFIXES = frozenset(
    {
        ".json",
        ".jsonl",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".conf",
        ".lock",
        ".xml",
        ".env",
    }
)
DOCUMENT_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz2",
    ".7z",
    ".rar",
    ".whl",
    ".jar",
    ".crate",
)
BINARY_SUFFIXES = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".node",
        ".wasm",
        ".a",
        ".lib",
        ".pdb",
        ".bin",
        ".dat",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".duckdb",
        ".class",
        ".o",
        ".obj",
    }
)
GENERATED_MARKERS = (
    "generated file",
    "do not edit",
    "@generated",
    "code generated",
    "automatically generated",
    "this file is generated",
)
MINIFIED_NAMES = re.compile(r"(?:^|[._-])min(?:[._-]|$)", re.IGNORECASE)
HASHED_BUNDLE_NAME = re.compile(r"[.-][0-9a-f]{8,}[.-]", re.IGNORECASE)
VENDOR_PARTS = frozenset(
    {
        "vendor",
        "vendor-runtimes",
        "third_party",
        "third-party",
        "source-pool",
        "runtime-sources",
        "productized",
        "loopx_runtime",
    }
)


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    path: str
    size: int
    suffix: str
    kind: str
    digest: str
    line_count: int
    average_line_length: float
    maximum_line_length: int
    printable_ratio: float
    generated: bool
    minified: bool
    vendor_like: bool
    executable: bool
    binary: bool
    symlink: bool
    link_target: str

    @property
    def language(self) -> str:
        return {
            ".py": "python",
            ".pyi": "python",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".js": "javascript",
            ".jsx": "jsx",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".rs": "rust",
            ".go": "go",
            ".cs": "csharp",
            ".java": "java",
            ".kt": "kotlin",
            ".kts": "kotlin",
            ".c": "c",
            ".cc": "cpp",
            ".cpp": "cpp",
            ".cxx": "cpp",
            ".h": "c",
            ".hpp": "cpp",
            ".swift": "swift",
            ".sh": "shell",
            ".bash": "shell",
            ".ps1": "powershell",
        }.get(self.suffix, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "suffix": self.suffix,
            "kind": self.kind,
            "digest": self.digest,
            "line_count": self.line_count,
            "average_line_length": round(self.average_line_length, 2),
            "maximum_line_length": self.maximum_line_length,
            "printable_ratio": round(self.printable_ratio, 5),
            "generated": self.generated,
            "minified": self.minified,
            "vendor_like": self.vendor_like,
            "executable": self.executable,
            "binary": self.binary,
            "symlink": self.symlink,
            "link_target": self.link_target,
            "language": self.language,
        }


@dataclass(frozen=True, slots=True)
class RepositoryInventory:
    root: Path
    files: tuple[RepositoryFile, ...]
    skipped_directories: tuple[str, ...]
    errors: tuple[str, ...]

    def by_kind(self) -> dict[str, tuple[RepositoryFile, ...]]:
        grouped: dict[str, list[RepositoryFile]] = defaultdict(list)
        for item in self.files:
            grouped[item.kind].append(item)
        return {
            key: tuple(sorted(value, key=lambda item: item.path))
            for key, value in sorted(grouped.items())
        }

    def by_language(self) -> dict[str, tuple[RepositoryFile, ...]]:
        grouped: dict[str, list[RepositoryFile]] = defaultdict(list)
        for item in self.files:
            if item.language:
                grouped[item.language].append(item)
        return {
            key: tuple(sorted(value, key=lambda item: item.path))
            for key, value in sorted(grouped.items())
        }

    def select(
        self,
        *,
        kinds: Iterable[str] = (),
        suffixes: Iterable[str] = (),
        prefixes: Iterable[str] = (),
    ) -> tuple[RepositoryFile, ...]:
        kind_set = set(kinds)
        suffix_set = {suffix.casefold() for suffix in suffixes}
        prefix_set = tuple(prefix.rstrip("/") + "/" for prefix in prefixes)
        return tuple(
            item
            for item in self.files
            if (not kind_set or item.kind in kind_set)
            and (not suffix_set or item.suffix in suffix_set)
            and (not prefix_set or item.path.startswith(prefix_set))
        )

    def file(self, path: str) -> RepositoryFile | None:
        normalized = path.replace("\\", "/").lstrip("/")
        return next((item for item in self.files if item.path == normalized), None)

    def to_summary(self) -> dict[str, Any]:
        kinds = Counter(item.kind for item in self.files)
        languages = Counter(item.language for item in self.files if item.language)
        return {
            "file_count": len(self.files),
            "byte_count": sum(item.size for item in self.files),
            "line_count": sum(item.line_count for item in self.files),
            "kind_counts": dict(sorted(kinds.items())),
            "language_counts": dict(sorted(languages.items())),
            "generated_count": sum(item.generated for item in self.files),
            "minified_count": sum(item.minified for item in self.files),
            "vendor_like_count": sum(item.vendor_like for item in self.files),
            "binary_count": sum(item.binary for item in self.files),
            "archive_count": sum(item.kind == "archive" for item in self.files),
            "symlink_count": sum(item.symlink for item in self.files),
            "skipped_directory_count": len(self.skipped_directories),
            "error_count": len(self.errors),
        }


class RepositoryScanner:
    def __init__(
        self,
        project_root: str | Path,
        *,
        exclude_directories: Iterable[str] = DEFAULT_EXCLUDED_DIRECTORIES,
        include_vendor: bool = True,
        maximum_text_bytes: int = 4 * 1024 * 1024,
        hash_chunk_size: int = 128 * 1024,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.exclude_directories = frozenset(exclude_directories)
        self.include_vendor = include_vendor
        self.maximum_text_bytes = maximum_text_bytes
        self.hash_chunk_size = hash_chunk_size
        self.switches = switches or RuleSwitches()

    def scan(self) -> tuple[RepositoryInventory, AuditSection]:
        records: list[RepositoryFile] = []
        skipped: list[str] = []
        errors: list[str] = []
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        for path in self._walk(skipped, errors):
            try:
                record = self._inspect(path)
            except OSError as exc:
                display = relative_path(self.project_root, path)
                errors.append(f"{display}: {exc}")
                findings.append(
                    finding(
                        "repository_file_unreadable",
                        f"Repository file cannot be inspected: {exc}",
                        "opaque",
                        severity=Severity.ERROR,
                        path=display,
                        disposition=Disposition.DECLARE,
                        remediation="Restore readable repository contents or exclude generated output.",
                        default_path_impact="Audit coverage is incomplete.",
                    )
                )
                continue
            records.append(record)
            if self.switches.opaque:
                findings.extend(self._opaque_findings(record))
            findings.extend(self._symlink_findings(record))
        inventory = RepositoryInventory(
            root=self.project_root,
            files=tuple(sorted(records, key=lambda item: item.path)),
            skipped_directories=tuple(sorted(set(skipped))),
            errors=tuple(errors),
        )
        evidence.append(
            Evidence(
                kind=EvidenceKind.CONFIG,
                path=".",
                excerpt_digest=content_digest(
                    "\n".join(
                        f"{item.path}\0{item.digest}\0{item.size}" for item in inventory.files
                    ).encode("utf-8")
                ),
                attributes=inventory.to_summary(),
            )
        )
        return inventory, section(
            "repository",
            metrics=inventory.to_summary(),
            findings=findings,
            evidence=evidence,
        )

    def _walk(self, skipped: list[str], errors: list[str]) -> Iterator[Path]:
        pending = [self.project_root]
        visited: set[tuple[int, int]] = set()
        while pending:
            directory = pending.pop()
            try:
                directory_stat = directory.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append(f"{relative_path(self.project_root, directory)}: {exc}")
                continue
            key = (directory_stat.st_dev, directory_stat.st_ino)
            if directory_stat.st_ino and key in visited:
                continue
            visited.add(key)
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError as exc:
                errors.append(f"{relative_path(self.project_root, directory)}: {exc}")
                continue
            child_directories: list[Path] = []
            for entry in entries:
                path = Path(entry.path)
                display = relative_path(self.project_root, path)
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                    is_link = entry.is_symlink()
                except OSError as exc:
                    errors.append(f"{display}: {exc}")
                    continue
                if is_directory:
                    if self._excluded_directory(entry.name, display):
                        skipped.append(display)
                    else:
                        child_directories.append(path)
                elif is_file or is_link:
                    yield path
            pending.extend(reversed(child_directories))

    def _excluded_directory(self, name: str, display: str) -> bool:
        if name in self.exclude_directories:
            return True
        if not self.include_vendor and name.casefold() in VENDOR_PARTS:
            return True
        normalized = display.replace("\\", "/")
        return any(
            fnmatch.fnmatch(normalized, pattern)
            for pattern in (
                "**/.git/**",
                "**/.cache/**",
                "**/__pycache__/**",
                "**/node_modules/**",
            )
        )

    def _inspect(self, path: Path) -> RepositoryFile:
        display = relative_path(self.project_root, path)
        metadata = path.lstat()
        link = stat.S_ISLNK(metadata.st_mode)
        link_target = os.readlink(path) if link else ""
        suffix = _compound_suffix(path.name)
        vendor_like = any(part.casefold() in VENDOR_PARTS for part in path.parts)
        executable = bool(metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        if link:
            return RepositoryFile(
                path=display,
                size=metadata.st_size,
                suffix=suffix,
                kind="symlink",
                digest=content_digest(link_target.encode("utf-8", errors="replace")),
                line_count=0,
                average_line_length=0,
                maximum_line_length=0,
                printable_ratio=1,
                generated=False,
                minified=False,
                vendor_like=vendor_like,
                executable=executable,
                binary=False,
                symlink=True,
                link_target=link_target,
            )
        size = metadata.st_size
        kind = _file_kind(path.name, suffix)
        binary_hint = suffix in BINARY_SUFFIXES or kind in {"binary", "archive"}
        digest = self._hash(path)
        line_count = 0
        average_line = 0.0
        maximum_line = 0
        printable = 1.0
        generated = False
        minified = False
        binary = binary_hint
        if size <= self.maximum_text_bytes and not binary_hint:
            sample = path.read_bytes()
            binary = _looks_binary(sample)
            if not binary:
                decoded = sample.decode("utf-8", errors="replace")
                lines = decoded.splitlines()
                line_count = len(lines)
                maximum_line = max((len(line) for line in lines), default=0)
                average_line = (
                    sum(len(line) for line in lines) / len(lines) if lines else 0.0
                )
                printable = _printable_ratio(sample)
                header = "\n".join(lines[:8]).casefold()
                generated = any(marker in header for marker in GENERATED_MARKERS)
                minified = _is_minified(path.name, lines, average_line, maximum_line)
            else:
                kind = "binary"
        elif size > self.maximum_text_bytes and kind in {"source", "config", "document"}:
            with path.open("rb") as stream:
                sample = stream.read(64 * 1024)
            binary = _looks_binary(sample)
            printable = _printable_ratio(sample)
            if binary:
                kind = "binary"
        return RepositoryFile(
            path=display,
            size=size,
            suffix=suffix,
            kind=kind,
            digest=digest,
            line_count=line_count,
            average_line_length=average_line,
            maximum_line_length=maximum_line,
            printable_ratio=printable,
            generated=generated,
            minified=minified,
            vendor_like=vendor_like,
            executable=executable,
            binary=binary,
            symlink=False,
            link_target="",
        )

    def _hash(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            _update_hash(stream, hasher, self.hash_chunk_size)
        return f"sha256:{hasher.hexdigest()}"

    def _opaque_findings(self, record: RepositoryFile) -> list[Finding]:
        findings: list[Finding] = []
        production_path = record.path.startswith(("apps/", "packages/", "scripts/"))
        if record.kind == "archive":
            findings.append(
                finding(
                    "archive_artifact_present",
                    "Archive artifact is present inside the repository.",
                    "opaque",
                    severity=Severity.ERROR if production_path else Severity.WARNING,
                    path=record.path,
                    disposition=Disposition.REMOVE,
                    remediation="Remove it from runtime paths or declare it as checksum-bound evidence.",
                    default_path_impact=(
                        "Archive can conceal undeclared runtime source or binaries."
                        if production_path
                        else "Evidence archive must remain non-runtime."
                    ),
                    evidence=(
                        Evidence(
                            kind=EvidenceKind.ARCHIVE,
                            path=record.path,
                            excerpt_digest=record.digest,
                            attributes={"size": record.size},
                        ),
                    ),
                )
            )
        if record.binary and production_path:
            allowed_evidence = record.path.startswith(
                ("docs/reviews/evidence/", "tests/fixtures/")
            )
            findings.append(
                finding(
                    "opaque_binary_in_production_tree",
                    "Opaque binary appears in a production-scanned tree.",
                    "opaque",
                    severity=Severity.BLOCKER if not allowed_evidence else Severity.WARNING,
                    path=record.path,
                    disposition=(
                        Disposition.BLOCK_RELEASE
                        if not allowed_evidence
                        else Disposition.TRACK
                    ),
                    remediation="Build from declared source, externalize, or remove the binary.",
                    default_path_impact="Binary runtime behavior cannot be source audited.",
                    evidence=(
                        Evidence(
                            kind=EvidenceKind.BINARY,
                            path=record.path,
                            excerpt_digest=record.digest,
                            attributes={"size": record.size},
                        ),
                    ),
                )
            )
        if record.minified and production_path and record.kind == "source":
            findings.append(
                finding(
                    "minified_source_in_production_tree",
                    "Minified or bundled source appears in the production tree.",
                    "opaque",
                    severity=Severity.BLOCKER,
                    path=record.path,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Retain auditable source and reproducible build inputs instead.",
                    default_path_impact="Core behavior is not reviewable at source level.",
                )
            )
        if record.generated and production_path:
            findings.append(
                finding(
                    "generated_runtime_source",
                    "Generated source is present in a production tree.",
                    "opaque",
                    severity=Severity.WARNING,
                    path=record.path,
                    disposition=Disposition.DECLARE,
                    remediation="Declare generator, inputs, output checksum, and non-counting status.",
                    default_path_impact="Generated code is excluded from effective production lines.",
                )
            )
        if record.vendor_like and record.path.startswith(("packages/", "apps/")):
            findings.append(
                finding(
                    "vendor_like_shape_in_formal_module",
                    "A formal package path contains a vendor-like directory boundary.",
                    "opaque",
                    severity=Severity.ERROR,
                    path=record.path,
                    disposition=Disposition.ABSORB,
                    remediation="Prove cropped Zyra custody or move source-pool material out of the formal package.",
                    default_path_impact="Directory placement alone cannot establish internalization.",
                )
            )
        return findings

    def _symlink_findings(self, record: RepositoryFile) -> list[Finding]:
        if not record.symlink:
            return []
        target = record.link_target.replace("\\", "/")
        target_path = Path(record.link_target)
        outside = target_path.is_absolute()
        if not outside:
            source = self.project_root / record.path
            resolved = (source.parent / target_path).resolve(strict=False)
            try:
                resolved.relative_to(self.project_root)
            except ValueError:
                outside = True
        return [
            finding(
                "symlink_external_target" if outside else "repository_symlink",
                (
                    "Repository symlink resolves outside the Zyra project."
                    if outside
                    else "Repository contains a symlink that must survive clean packaging."
                ),
                "dependencies",
                severity=Severity.BLOCKER if outside else Severity.WARNING,
                path=record.path,
                disposition=(
                    Disposition.BLOCK_RELEASE if outside else Disposition.TRACK
                ),
                remediation=(
                    "Replace external link with owned source or a declared package dependency."
                    if outside
                    else "Verify archive and package tooling preserves the link safely."
                ),
                default_path_impact=(
                    "Clean checkout would depend on external filesystem state."
                    if outside
                    else "Clean package behavior may differ by archive format."
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.SYMLINK,
                        path=record.path,
                        attributes={"target": target},
                    ),
                ),
            )
        ]


def _update_hash(stream: BinaryIO, hasher: Any, chunk_size: int) -> None:
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return
        hasher.update(chunk)


def _compound_suffix(name: str) -> str:
    lower = name.casefold()
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            return suffix
    return Path(lower).suffix


def _file_kind(name: str, suffix: str) -> str:
    lower = name.casefold()
    if any(lower.endswith(item) for item in ARCHIVE_SUFFIXES):
        return "archive"
    if suffix in BINARY_SUFFIXES:
        return "binary"
    if suffix in SOURCE_SUFFIXES:
        return "source"
    if suffix in CONFIG_SUFFIXES or lower in {
        "dockerfile",
        "makefile",
        "justfile",
        "procfile",
        "license",
        "notice",
    }:
        return "config"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if lower.startswith(".env"):
        return "config"
    return "asset"


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:8192]
    suspicious = sum(
        byte < 9 or (13 < byte < 32) or byte == 127 for byte in sample
    )
    return suspicious / len(sample) > 0.05


def _printable_ratio(data: bytes) -> float:
    if not data:
        return 1.0
    sample = data[:65536]
    printable = sum(
        byte in {9, 10, 13} or 32 <= byte <= 126 or byte >= 128 for byte in sample
    )
    return printable / len(sample)


def _is_minified(
    name: str,
    lines: Sequence[str],
    average_line: float,
    maximum_line: int,
) -> bool:
    if MINIFIED_NAMES.search(name) or HASHED_BUNDLE_NAME.search(name):
        return True
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return False
    very_long = sum(len(line) >= 500 for line in meaningful)
    punctuation = sum(
        line.count(";") + line.count("{") + line.count("}") for line in meaningful
    )
    return (
        maximum_line >= 2000
        or (average_line >= 300 and very_long / len(meaningful) >= 0.25)
        or (
            len(meaningful) <= 8
            and maximum_line >= 800
            and punctuation >= len(meaningful) * 20
        )
    )


def inventory_manifest(inventory: RepositoryInventory) -> list[dict[str, Any]]:
    return [
        item.to_dict()
        for item in inventory.files
        if item.kind in {"config", "archive", "binary", "symlink"}
        or item.generated
        or item.minified
        or item.vendor_like
    ]


def source_digest_index(
    inventory: RepositoryInventory,
    *,
    include_vendor: bool,
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in inventory.files:
        if item.kind != "source":
            continue
        if item.vendor_like and not include_vendor:
            continue
        grouped[item.digest].append(item.path)
    return {
        digest: tuple(sorted(paths))
        for digest, paths in sorted(grouped.items())
        if len(paths) > 1
    }
