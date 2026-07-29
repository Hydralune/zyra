from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tarfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import BoundaryViolation, IntegrityViolation, LockViolation
from .models import (
    BoundaryFinding,
    ChecksumManifest,
    FileKind,
    FileRecord,
    RequirementRecord,
)
from .policy import DEFAULT_RELEASE_POLICY, ReleasePolicy


_DRIVE = re.compile(r"^[a-zA-Z]:")
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[A-Za-z0-9_,.-]+\])?"
    r"==(?P<version>[^;\s\\]+)"
    r"(?:\s*;\s*(?P<marker>.*?))?$"
)
_HASH = re.compile(r"^--hash=(?P<algorithm>[a-z0-9]+):(?P<digest>[a-fA-F0-9]+)$")
_PACKAGE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_digest(value: Any) -> str:
    return sha256_text(stable_json(value))


def normalize_relative_path(value: str | Path) -> str:
    raw = str(value).replace("\\", "/")
    if not raw or raw in {".", "./"}:
        return "."
    if "\x00" in raw:
        raise BoundaryViolation(
            "Release path contains a NUL byte.",
            code="path_nul_byte",
            details={"path": raw},
        )
    if raw.startswith("/") or raw.startswith("//") or _DRIVE.match(raw):
        raise BoundaryViolation(
            "Release path must be relative.",
            code="path_absolute",
            details={"path": raw},
        )
    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise BoundaryViolation(
                    "Release path escapes its root.",
                    code="path_traversal",
                    details={"path": raw},
                )
            parts.pop()
            continue
        if part.endswith(" ") or part.endswith("."):
            raise BoundaryViolation(
                "Release path has a platform-ambiguous suffix.",
                code="path_ambiguous_suffix",
                details={"path": raw, "component": part},
            )
        if ":" in part:
            raise BoundaryViolation(
                "Release path contains a device or alternate-stream separator.",
                code="path_device_or_ads",
                details={"path": raw, "component": part},
            )
        if any(ord(character) < 32 for character in part):
            raise BoundaryViolation(
                "Release path contains a control character.",
                code="path_control_character",
                details={"path": raw, "component": part},
            )
        folded = part.casefold()
        stem = folded.split(".", 1)[0]
        if stem in {
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
        }:
            raise BoundaryViolation(
                "Release path uses a reserved device name.",
                code="path_reserved_device",
                details={"path": raw, "component": part},
            )
        parts.append(part)
    return "/".join(parts) or "."


def resolve_below(root: Path, relative: str | Path) -> Path:
    normalized = normalize_relative_path(relative)
    root_resolved = root.resolve()
    candidate = root_resolved if normalized == "." else root_resolved.joinpath(*PurePosixPath(normalized).parts)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as error:
        raise BoundaryViolation(
            "Resolved release path escapes its root.",
            code="resolved_path_escape",
            details={
                "root": str(root_resolved),
                "path": normalized,
                "resolved": str(candidate_resolved),
            },
        ) from error
    return candidate_resolved


def classify_file(path: str, *, executable: bool) -> FileKind:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    suffix = pure.suffix.casefold()
    if name in {
        "pyproject.toml",
        "package.json",
        "tsconfig.json",
        "cargo.toml",
        "manifest.json",
    }:
        return FileKind.MANIFEST
    if name in {
        "requirements.txt",
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
    }:
        return FileKind.LOCK
    if suffix in {".dll", ".exe", ".pyd", ".so", ".dylib", ".node", ".wasm"}:
        return FileKind.NATIVE
    if path.startswith("docs/") or suffix in {".md", ".rst"}:
        return FileKind.DOCUMENTATION
    if path.startswith("skills/") and suffix in {".md", ".json", ".yaml", ".yml"}:
        return FileKind.RUNTIME_ASSET
    if path.startswith("dist/") or executable:
        return FileKind.BUILD
    return FileKind.SOURCE


@dataclass(frozen=True, slots=True)
class TreeEntry:
    relative_path: str
    path: Path
    stat_result: os.stat_result
    symlink_target: str = ""

    @property
    def is_symlink(self) -> bool:
        return bool(self.symlink_target)

    @property
    def executable(self) -> bool:
        return bool(self.stat_result.st_mode & stat.S_IXUSR)


class CanonicalTreeWalker:
    def __init__(
        self,
        root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
        include_excluded: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.policy = policy
        self.include_excluded = include_excluded

    def walk(self) -> Iterator[TreeEntry]:
        if not self.root.is_dir():
            raise BoundaryViolation(
                "Release source root does not exist.",
                code="source_root_missing",
                details={"root": str(self.root)},
            )
        seen_casefold: dict[str, str] = {}
        count = 0
        total_size = 0
        stack = [self.root]
        while stack:
            directory = stack.pop()
            try:
                children = sorted(
                    directory.iterdir(),
                    key=lambda item: item.name.encode("utf-8"),
                    reverse=True,
                )
            except OSError as error:
                raise BoundaryViolation(
                    "Release source directory cannot be enumerated.",
                    code="source_directory_unreadable",
                    details={"path": str(directory), "error": str(error)},
                ) from error
            for child in children:
                relative = child.relative_to(self.root).as_posix()
                normalized = normalize_relative_path(relative)
                folded = normalized.casefold()
                prior = seen_casefold.get(folded)
                if prior is not None and prior != normalized:
                    raise BoundaryViolation(
                        "Release tree has case-colliding paths.",
                        code="path_case_collision",
                        details={"first": prior, "second": normalized},
                    )
                seen_casefold[folded] = normalized
                excluded, _ = self.policy.excluded(normalized)
                try:
                    item_stat = child.lstat()
                except OSError as error:
                    raise BoundaryViolation(
                        "Release source entry cannot be inspected.",
                        code="source_entry_unreadable",
                        details={"path": normalized, "error": str(error)},
                    ) from error
                if stat.S_ISLNK(item_stat.st_mode):
                    target = os.readlink(child)
                    yield TreeEntry(normalized, child, item_stat, target)
                    continue
                if stat.S_ISDIR(item_stat.st_mode):
                    if not excluded:
                        stack.append(child)
                    continue
                if excluded and not self.include_excluded:
                    continue
                if not stat.S_ISREG(item_stat.st_mode):
                    raise BoundaryViolation(
                        "Release tree contains a special filesystem entry.",
                        code="special_file_rejected",
                        details={"path": normalized, "mode": item_stat.st_mode},
                    )
                count += 1
                total_size += item_stat.st_size
                if count > self.policy.maximum_file_count:
                    raise BoundaryViolation(
                        "Release tree exceeds the file-count policy.",
                        code="file_count_limit",
                        details={"count": count, "limit": self.policy.maximum_file_count},
                    )
                if item_stat.st_size > self.policy.maximum_file_size:
                    raise BoundaryViolation(
                        "Release file exceeds the size policy.",
                        code="file_size_limit",
                        details={
                            "path": normalized,
                            "size": item_stat.st_size,
                            "limit": self.policy.maximum_file_size,
                        },
                    )
                if total_size > self.policy.maximum_bundle_size:
                    raise BoundaryViolation(
                        "Release tree exceeds the total-size policy.",
                        code="bundle_size_limit",
                        details={
                            "size": total_size,
                            "limit": self.policy.maximum_bundle_size,
                        },
                    )
                yield TreeEntry(normalized, child, item_stat)


class BoundaryScanner:
    def __init__(
        self,
        root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.root = root.resolve()
        self.policy = policy

    def scan(self) -> dict[str, Any]:
        findings: list[BoundaryFinding] = []
        included: list[str] = []
        excluded: list[dict[str, str]] = []
        native: list[str] = []
        executable: list[str] = []
        secrets: list[str] = []
        walker = CanonicalTreeWalker(
            self.root,
            policy=self.policy,
            include_excluded=True,
        )
        for entry in walker.walk():
            is_excluded, reason = self.policy.excluded(entry.relative_path)
            if is_excluded:
                excluded.append({"path": entry.relative_path, "reason": reason})
                continue
            if entry.is_symlink:
                findings.extend(self._inspect_symlink(entry))
                continue
            included.append(entry.relative_path)
            suffix = entry.path.suffix.casefold()
            if suffix in self.policy.allow_native_extensions:
                native.append(entry.relative_path)
                if entry.relative_path not in self.policy.tracked_native_paths:
                    findings.append(
                        BoundaryFinding(
                            code="undeclared_native_binary",
                            path=entry.relative_path,
                            message="Native artifact is not declared by release policy.",
                            attributes={"suffix": suffix},
                        )
                    )
            if entry.executable:
                executable.append(entry.relative_path)
                if suffix not in {
                    ".py",
                    ".pyw",
                    ".sh",
                    ".ps1",
                    ".cmd",
                    ".bat",
                    ".js",
                    ".mjs",
                    ".cjs",
                    ".ts",
                } and suffix not in self.policy.allow_native_extensions:
                    findings.append(
                        BoundaryFinding(
                            code="undeclared_executable",
                            path=entry.relative_path,
                            message="Executable release file has no recognized owner.",
                        )
                    )
            if self._looks_like_secret_file(entry.relative_path):
                secrets.append(entry.relative_path)
                findings.append(
                    BoundaryFinding(
                        code="secret_file_in_release",
                        path=entry.relative_path,
                        message="Secret-like file must not enter a release bundle.",
                    )
                )
            findings.extend(self._inspect_text_reference(entry))
        present = {PurePosixPath(path).as_posix() for path in included}
        for required in sorted(self.policy.required_root_files):
            if required not in present:
                findings.append(
                    BoundaryFinding(
                        code="required_release_file_missing",
                        path=required,
                        message="Required release input is missing.",
                    )
                )
        blocker_count = sum(item.blocker for item in findings)
        report = {
            "schema": "zyra.release-boundary-report/v1",
            "root": str(self.root),
            "ready": blocker_count == 0,
            "included_count": len(included),
            "excluded_count": len(excluded),
            "native_count": len(native),
            "executable_count": len(executable),
            "secret_file_count": len(secrets),
            "blocker_count": blocker_count,
            "findings": [item.to_dict() for item in findings],
            "included_digest": stable_digest(included),
            "excluded_digest": stable_digest(excluded),
        }
        return report

    def enforce(self) -> dict[str, Any]:
        report = self.scan()
        if not report["ready"]:
            raise BoundaryViolation(
                "Release source boundary contains blockers.",
                code="release_boundary_blocked",
                details={
                    "blocker_count": report["blocker_count"],
                    "findings": report["findings"][:50],
                },
            )
        return report

    def _inspect_symlink(self, entry: TreeEntry) -> list[BoundaryFinding]:
        findings: list[BoundaryFinding] = []
        target = entry.symlink_target
        try:
            normalized_target = normalize_relative_path(target)
        except BoundaryViolation:
            findings.append(
                BoundaryFinding(
                    code="symlink_target_unsafe",
                    path=entry.relative_path,
                    message="Symlink target is absolute or escapes release root.",
                    attributes={"target": target},
                )
            )
            return findings
        parent = PurePosixPath(entry.relative_path).parent
        combined = normalize_relative_path((parent / normalized_target).as_posix())
        resolved = resolve_below(self.root, combined)
        if not resolved.exists():
            findings.append(
                BoundaryFinding(
                    code="symlink_target_missing",
                    path=entry.relative_path,
                    message="Symlink target does not exist in the release tree.",
                    attributes={"target": target},
                )
            )
        else:
            findings.append(
                BoundaryFinding(
                    code="symlink_not_portable",
                    path=entry.relative_path,
                    message="Release source symlinks are rejected for portable installs.",
                    attributes={"target": target},
                )
            )
        return findings

    def _looks_like_secret_file(self, path: str) -> bool:
        pure = PurePosixPath(path)
        name = pure.name.casefold()
        if name == ".env.example":
            return False
        if name.startswith(".env"):
            return True
        if name.endswith((".pem", ".key", ".p12", ".pfx", ".jks")):
            return True
        return any(
            fragment in name for fragment in self.policy.secret_name_fragments
        ) and pure.suffix.casefold() not in {".py", ".ts", ".js", ".md"}

    def _inspect_text_reference(self, entry: TreeEntry) -> list[BoundaryFinding]:
        if entry.stat_result.st_size > 2 * 1024 * 1024:
            return []
        name = entry.path.name.casefold()
        manifest_names = {
            "pyproject.toml",
            "requirements.txt",
            "package.json",
            "bun.lock",
            "cargo.toml",
            "cargo.lock",
            "dockerfile",
            "docker-compose.yml",
            "docker-compose.yaml",
        }
        if name not in manifest_names and not name.endswith(
            (".requirements.txt", ".lock")
        ):
            return []
        try:
            content = entry.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        folded = content.casefold().replace("\\", "/")
        findings: list[BoundaryFinding] = []
        for name in sorted(self.policy.source_repository_names):
            patterns = (
                f"../{name}/",
                f"../{name}\"",
                f"../{name}'",
                f"g:/agent-zoo/{name}/",
                f"g:\\agent-zoo\\{name}\\",
            )
            if any(pattern in folded for pattern in patterns):
                findings.append(
                    BoundaryFinding(
                        code="root_source_runtime_reference",
                        path=entry.relative_path,
                        message="Release file references a parent source repository.",
                        attributes={"source_repository": name},
                    )
                )
        if "file:../" in folded or "link:../" in folded:
            findings.append(
                BoundaryFinding(
                    code="external_package_link",
                    path=entry.relative_path,
                    message="Release manifest contains an external file/link dependency.",
                )
            )
        if "--editable ../" in folded or "-e ../" in folded:
            findings.append(
                BoundaryFinding(
                    code="external_editable_dependency",
                    path=entry.relative_path,
                    message="Release input contains an external editable dependency.",
                )
            )
        return findings


class PythonLock:
    def __init__(self, records: Sequence[RequirementRecord], *, source: Path) -> None:
        self.records = tuple(records)
        self.source = source
        self.by_name = {item.canonical_name: item for item in self.records}

    @classmethod
    def load(cls, path: Path, *, require_hashes: bool = True) -> "PythonLock":
        if not path.is_file():
            raise LockViolation(
                "Python release lock is missing.",
                code="python_lock_missing",
                details={"path": str(path)},
            )
        logical_lines = cls._logical_lines(path.read_text(encoding="utf-8"))
        records: list[RequirementRecord] = []
        seen: dict[str, RequirementRecord] = {}
        for line_number, line in logical_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("-r ", "--requirement ", "-c ", "--constraint ")):
                raise LockViolation(
                    "Nested requirement files are not allowed in the release lock.",
                    code="python_lock_nested_file",
                    details={"line": line_number, "value": stripped},
                )
            if stripped.startswith(("-e ", "--editable ")):
                raise LockViolation(
                    "Editable requirements are not allowed in the release lock.",
                    code="python_lock_editable",
                    details={"line": line_number, "value": stripped},
                )
            hash_tokens = re.findall(
                r"--hash=[a-z0-9]+:[a-fA-F0-9]+",
                stripped,
            )
            requirement_token = re.sub(
                r"\s*--hash=[a-z0-9]+:[a-fA-F0-9]+",
                "",
                stripped,
            ).strip()
            if requirement_token.startswith((".", "/", "\\", "file:", "git+", "hg+", "svn+")):
                raise LockViolation(
                    "Local, VCS and path requirements are not allowed.",
                    code="python_lock_external_source",
                    details={"line": line_number, "value": requirement_token},
                )
            match = _REQUIREMENT.fullmatch(requirement_token)
            if match is None:
                raise LockViolation(
                    "Python requirement is not pinned with ==.",
                    code="python_lock_unpinned",
                    details={"line": line_number, "value": requirement_token},
                )
            raw_name = match.group("name")
            if not _PACKAGE_NAME.fullmatch(raw_name.casefold()):
                raise LockViolation(
                    "Python requirement name is invalid.",
                    code="python_lock_invalid_name",
                    details={"line": line_number, "name": raw_name},
                )
            version = match.group("version")
            if any(character in version for character in "*<>=!~"):
                raise LockViolation(
                    "Python requirement version is not exact.",
                    code="python_lock_nonexact_version",
                    details={"line": line_number, "version": version},
                )
            hashes: list[str] = []
            for token in hash_tokens:
                hash_match = _HASH.fullmatch(token)
                if hash_match is None:
                    raise LockViolation(
                        "Python lock contains an unsupported option.",
                        code="python_lock_unsupported_option",
                        details={"line": line_number, "option": token},
                    )
                if hash_match.group("algorithm") != "sha256":
                    raise LockViolation(
                        "Python lock uses a non-SHA256 digest.",
                        code="python_lock_hash_algorithm",
                        details={"line": line_number, "option": token},
                    )
                digest = hash_match.group("digest").lower()
                if len(digest) != 64:
                    raise LockViolation(
                        "Python lock hash has an invalid length.",
                        code="python_lock_hash_length",
                        details={"line": line_number, "digest": digest},
                    )
                hashes.append(digest)
            if require_hashes and not hashes:
                raise LockViolation(
                    "Python release lock entry has no artifact hash.",
                    code="python_lock_hash_missing",
                    details={"line": line_number, "name": raw_name},
                )
            extras_raw = match.group("extras") or ""
            extras = (
                tuple(sorted(item.strip() for item in extras_raw[1:-1].split(",")))
                if extras_raw
                else ()
            )
            record = RequirementRecord(
                name=raw_name,
                version=version,
                hashes=tuple(sorted(set(hashes))),
                marker=(match.group("marker") or "").strip(),
                extras=extras,
                source_line=line_number,
            )
            key = record.canonical_name
            prior = seen.get(key)
            if prior is not None and prior != record:
                raise LockViolation(
                    "Python release lock contains conflicting duplicate requirements.",
                    code="python_lock_conflict",
                    details={
                        "name": key,
                        "first_line": prior.source_line,
                        "second_line": line_number,
                    },
                )
            if prior is None:
                seen[key] = record
                records.append(record)
        if not records:
            raise LockViolation(
                "Python release lock is empty.",
                code="python_lock_empty",
                details={"path": str(path)},
            )
        return cls(records, source=path)

    @staticmethod
    def _logical_lines(content: str) -> list[tuple[int, str]]:
        output: list[tuple[int, str]] = []
        buffer = ""
        start = 0
        for line_number, raw in enumerate(content.splitlines(), 1):
            line = raw.rstrip()
            if not buffer:
                start = line_number
            if line.endswith("\\"):
                buffer += line[:-1].strip() + " "
                continue
            buffer += line
            output.append((start, buffer))
            buffer = ""
        if buffer:
            raise LockViolation(
                "Python release lock ends with an incomplete continuation.",
                code="python_lock_incomplete_continuation",
                details={"line": start},
            )
        return output

    def verify_project_requirements(self, pyproject: Mapping[str, Any]) -> dict[str, Any]:
        project = pyproject.get("project")
        if not isinstance(project, Mapping):
            raise LockViolation(
                "pyproject.toml has no project table.",
                code="python_project_table_missing",
            )
        raw_dependencies = project.get("dependencies", ())
        if not isinstance(raw_dependencies, Sequence) or isinstance(
            raw_dependencies, (str, bytes)
        ):
            raise LockViolation(
                "pyproject dependencies must be an array.",
                code="python_project_dependencies_invalid",
            )
        missing: list[str] = []
        incompatible: list[dict[str, str]] = []
        for raw in raw_dependencies:
            name, operator, version = self._parse_project_dependency(str(raw))
            record = self.by_name.get(name)
            if record is None:
                missing.append(name)
                continue
            if not self._version_satisfies(record.version, operator, version):
                incompatible.append(
                    {
                        "name": name,
                        "constraint": f"{operator}{version}",
                        "locked": record.version,
                    }
                )
        if missing or incompatible:
            raise LockViolation(
                "Python release lock does not satisfy project dependencies.",
                code="python_lock_project_mismatch",
                details={"missing": missing, "incompatible": incompatible},
            )
        return {
            "schema": "zyra.python-lock-receipt/v1",
            "ready": True,
            "path": self.source.as_posix(),
            "requirement_count": len(self.records),
            "hashed_requirement_count": sum(bool(item.hashes) for item in self.records),
            "digest": stable_digest([item.to_dict() for item in self.records]),
            "project_dependencies": len(raw_dependencies),
        }

    @staticmethod
    def _parse_project_dependency(value: str) -> tuple[str, str, str]:
        raw = value.strip()
        name_match = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]+\])?", raw)
        if name_match is None:
            raise LockViolation(
                "Project dependency name is invalid.",
                code="python_project_dependency_invalid",
                details={"dependency": value},
            )
        name = name_match.group(1).lower().replace("_", "-").replace(".", "-")
        remainder = raw[name_match.end() :].split(";", 1)[0].strip()
        constraint = re.search(r"(==|>=|<=|~=|>|<)\s*([A-Za-z0-9.+!_-]+)", remainder)
        if constraint is None:
            return name, ">=", "0"
        return name, constraint.group(1), constraint.group(2)

    @staticmethod
    def _version_satisfies(actual: str, operator: str, expected: str) -> bool:
        def key(value: str) -> tuple[Any, ...]:
            parts = re.split(r"([0-9]+)", value)
            return tuple(int(part) if part.isdigit() else part.casefold() for part in parts if part)

        actual_key = key(actual)
        expected_key = key(expected)
        if operator == "==":
            return actual_key == expected_key
        if operator == ">=":
            return actual_key >= expected_key
        if operator == "<=":
            return actual_key <= expected_key
        if operator == ">":
            return actual_key > expected_key
        if operator == "<":
            return actual_key < expected_key
        if operator == "~=":
            actual_parts = actual.split(".")
            expected_parts = expected.split(".")
            prefix = expected_parts[:-1] if len(expected_parts) > 1 else expected_parts
            return actual_key >= expected_key and actual_parts[: len(prefix)] == prefix
        return False


class BunLock:
    def __init__(self, value: Mapping[str, Any], *, source: Path) -> None:
        self.value = dict(value)
        self.source = source

    @classmethod
    def load(cls, path: Path) -> "BunLock":
        if not path.is_file():
            raise LockViolation(
                "Bun lockfile is missing.",
                code="bun_lock_missing",
                details={"path": str(path)},
            )
        try:
            content = path.read_text(encoding="utf-8")
            content = re.sub(r",(\s*[}\]])", r"\1", content)
            value = json.loads(content)
        except (OSError, json.JSONDecodeError) as error:
            raise LockViolation(
                "Bun lockfile is not valid JSON.",
                code="bun_lock_invalid",
                details={"path": str(path), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise LockViolation(
                "Bun lockfile root must be an object.",
                code="bun_lock_root_invalid",
            )
        if int(value.get("lockfileVersion") or 0) < 1:
            raise LockViolation(
                "Bun lockfile version is unsupported.",
                code="bun_lock_version_invalid",
                details={"version": value.get("lockfileVersion")},
            )
        return cls(value, source=path)

    def verify_workspace(self, root: Path) -> dict[str, Any]:
        root_manifest_path = root / "package.json"
        if not root_manifest_path.is_file():
            raise LockViolation(
                "Root package.json is missing.",
                code="javascript_manifest_missing",
            )
        manifest = json.loads(root_manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise LockViolation(
                "Root package.json is invalid.",
                code="javascript_manifest_invalid",
            )
        package_manager = str(manifest.get("packageManager") or "")
        if not re.fullmatch(r"bun@[0-9]+\.[0-9]+\.[0-9]+", package_manager):
            raise LockViolation(
                "Root package manager must pin an exact Bun version.",
                code="bun_version_unpinned",
                details={"packageManager": package_manager},
            )
        workspaces = manifest.get("workspaces", ())
        if not isinstance(workspaces, Sequence) or isinstance(workspaces, (str, bytes)):
            raise LockViolation(
                "Root workspaces field must be an array.",
                code="javascript_workspaces_invalid",
            )
        manifests: list[dict[str, Any]] = []
        missing: list[str] = []
        external: list[dict[str, str]] = []
        floating: list[dict[str, str]] = []
        for workspace in sorted(str(item) for item in workspaces):
            normalized = normalize_relative_path(workspace)
            path = resolve_below(root, normalized) / "package.json"
            if not path.is_file():
                missing.append(normalized)
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                missing.append(normalized)
                continue
            dependencies: dict[str, str] = {}
            for field in (
                "dependencies",
                "devDependencies",
                "optionalDependencies",
                "peerDependencies",
            ):
                raw = value.get(field, {})
                if not isinstance(raw, Mapping):
                    raise LockViolation(
                        "Workspace dependency table must be an object.",
                        code="javascript_dependency_table_invalid",
                        details={"workspace": normalized, "field": field},
                    )
                for name, version in raw.items():
                    text = str(version)
                    dependencies[str(name)] = text
                    if text.startswith(("file:", "link:", "git+", "github:", "../", "/")):
                        external.append(
                            {
                                "workspace": normalized,
                                "package": str(name),
                                "version": text,
                            }
                        )
                    if text in {"*", "latest", "next"}:
                        floating.append(
                            {
                                "workspace": normalized,
                                "package": str(name),
                                "version": text,
                            }
                        )
            manifests.append(
                {
                    "workspace": normalized,
                    "name": str(value.get("name") or ""),
                    "version": str(value.get("version") or ""),
                    "dependencies": dependencies,
                }
            )
        root_dependencies: dict[str, str] = {}
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            raw = manifest.get(field, {})
            if isinstance(raw, Mapping):
                for name, version in raw.items():
                    root_dependencies[str(name)] = str(version)
        for name, version in root_dependencies.items():
            if version.startswith(("file:", "link:", "git+", "github:", "../", "/")):
                external.append(
                    {"workspace": ".", "package": name, "version": version}
                )
            if version in {"*", "latest", "next"}:
                floating.append(
                    {"workspace": ".", "package": name, "version": version}
                )
        if missing or external or floating:
            raise LockViolation(
                "JavaScript workspace is not release-frozen.",
                code="javascript_workspace_unfrozen",
                details={
                    "missing_workspaces": missing,
                    "external_dependencies": external,
                    "floating_dependencies": floating,
                },
            )
        return {
            "schema": "zyra.bun-lock-receipt/v1",
            "ready": True,
            "path": self.source.as_posix(),
            "package_manager": package_manager,
            "workspace_count": len(manifests),
            "manifest_digest": stable_digest(
                {
                    "root_dependencies": root_dependencies,
                    "workspaces": manifests,
                }
            ),
            "lock_digest": stable_digest(self.value),
        }


class ChecksumBuilder:
    def __init__(
        self,
        root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.root = root.resolve()
        self.policy = policy

    def build(
        self,
        *,
        generated_at: str,
        include: Iterable[str] | None = None,
    ) -> ChecksumManifest:
        include_set = (
            {normalize_relative_path(item) for item in include}
            if include is not None
            else None
        )
        files: list[FileRecord] = []
        walker = CanonicalTreeWalker(self.root, policy=self.policy)
        for entry in walker.walk():
            if entry.is_symlink:
                raise IntegrityViolation(
                    "Checksum payload contains a symlink.",
                    code="checksum_symlink",
                    details={"path": entry.relative_path},
                )
            if include_set is not None and entry.relative_path not in include_set:
                continue
            digest = sha256_file(entry.path)
            mode = stat.S_IMODE(entry.stat_result.st_mode)
            executable = entry.executable
            files.append(
                FileRecord(
                    path=entry.relative_path,
                    size=entry.stat_result.st_size,
                    sha256=digest,
                    mode=mode,
                    kind=classify_file(entry.relative_path, executable=executable),
                    executable=executable,
                )
            )
        files.sort(key=lambda item: item.path.encode("utf-8"))
        if include_set is not None:
            found = {item.path for item in files}
            missing = sorted(include_set - found)
            if missing:
                raise IntegrityViolation(
                    "Checksum input contains missing paths.",
                    code="checksum_input_missing",
                    details={"missing": missing},
                )
        root_digest = self._root_digest(files)
        return ChecksumManifest(
            algorithm="sha256",
            root_digest=root_digest,
            files=tuple(files),
            generated_at=generated_at,
        )

    @staticmethod
    def _root_digest(files: Sequence[FileRecord]) -> str:
        digest = hashlib.sha256()
        for record in files:
            digest.update(record.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(record.size).encode("ascii"))
            digest.update(b"\0")
            digest.update(record.sha256.encode("ascii"))
            digest.update(b"\0")
            digest.update(format(record.mode, "o").encode("ascii"))
            digest.update(b"\0")
            digest.update(record.kind.value.encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()


class ChecksumVerifier:
    def __init__(
        self,
        root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.root = root.resolve()
        self.policy = policy

    def verify(
        self,
        manifest: ChecksumManifest | Mapping[str, Any],
        *,
        reject_extra: bool = True,
    ) -> dict[str, Any]:
        parsed = (
            manifest
            if isinstance(manifest, ChecksumManifest)
            else self._parse_manifest(manifest)
        )
        expected = {item.path: item for item in parsed.files}
        actual_paths: set[str] = set()
        missing: list[str] = []
        changed: list[dict[str, Any]] = []
        mode_changed: list[dict[str, Any]] = []
        for path, record in expected.items():
            target = resolve_below(self.root, path)
            if not target.is_file():
                missing.append(path)
                continue
            actual_paths.add(path)
            file_stat = target.stat()
            actual_digest = sha256_file(target)
            if file_stat.st_size != record.size or actual_digest != record.sha256:
                changed.append(
                    {
                        "path": path,
                        "expected_size": record.size,
                        "actual_size": file_stat.st_size,
                        "expected_sha256": record.sha256,
                        "actual_sha256": actual_digest,
                    }
                )
            actual_mode = stat.S_IMODE(file_stat.st_mode)
            if os.name != "nt" and actual_mode != record.mode:
                mode_changed.append(
                    {
                        "path": path,
                        "expected_mode": record.mode,
                        "actual_mode": actual_mode,
                    }
                )
        extra: list[str] = []
        if reject_extra:
            for entry in CanonicalTreeWalker(self.root, policy=self.policy).walk():
                if entry.is_symlink:
                    extra.append(entry.relative_path)
                elif entry.relative_path not in expected:
                    extra.append(entry.relative_path)
        calculated_root = ChecksumBuilder._root_digest(parsed.files)
        root_mismatch = calculated_root != parsed.root_digest
        ready = not missing and not changed and not mode_changed and not extra and not root_mismatch
        report = {
            "schema": "zyra.checksum-verification/v1",
            "ready": ready,
            "expected_files": len(expected),
            "verified_files": len(actual_paths),
            "missing": missing,
            "changed": changed,
            "mode_changed": mode_changed,
            "extra": sorted(extra),
            "expected_root_digest": parsed.root_digest,
            "calculated_root_digest": calculated_root,
            "root_mismatch": root_mismatch,
        }
        if not ready:
            raise IntegrityViolation(
                "Release checksum verification failed.",
                code="checksum_verification_failed",
                details=report,
            )
        return report

    @staticmethod
    def _parse_manifest(value: Mapping[str, Any]) -> ChecksumManifest:
        if value.get("schema") != "zyra.checksum-manifest/v1":
            raise IntegrityViolation(
                "Checksum manifest schema is unsupported.",
                code="checksum_schema_invalid",
                details={"schema": value.get("schema")},
            )
        if value.get("algorithm") != "sha256":
            raise IntegrityViolation(
                "Checksum algorithm is unsupported.",
                code="checksum_algorithm_invalid",
            )
        raw_files = value.get("files")
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
            raise IntegrityViolation(
                "Checksum file list is invalid.",
                code="checksum_files_invalid",
            )
        records: list[FileRecord] = []
        seen: set[str] = set()
        for raw in raw_files:
            if not isinstance(raw, Mapping):
                raise IntegrityViolation(
                    "Checksum file record is invalid.",
                    code="checksum_record_invalid",
                )
            path = normalize_relative_path(str(raw.get("path") or ""))
            if path in seen:
                raise IntegrityViolation(
                    "Checksum manifest contains duplicate paths.",
                    code="checksum_duplicate_path",
                    details={"path": path},
                )
            seen.add(path)
            digest = str(raw.get("sha256") or "").lower()
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise IntegrityViolation(
                    "Checksum record has an invalid SHA256.",
                    code="checksum_digest_invalid",
                    details={"path": path},
                )
            try:
                kind = FileKind(str(raw.get("kind") or ""))
            except ValueError as error:
                raise IntegrityViolation(
                    "Checksum record has an invalid kind.",
                    code="checksum_kind_invalid",
                    details={"path": path, "kind": raw.get("kind")},
                ) from error
            records.append(
                FileRecord(
                    path=path,
                    size=int(raw.get("size") or 0),
                    sha256=digest,
                    mode=int(raw.get("mode") or 0),
                    kind=kind,
                    executable=bool(raw.get("executable")),
                )
            )
        return ChecksumManifest(
            algorithm="sha256",
            root_digest=str(value.get("root_digest") or ""),
            files=tuple(records),
            generated_at=str(value.get("generated_at") or ""),
        )


class ArchiveInspector:
    def __init__(
        self,
        archive: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.archive = archive.resolve()
        self.policy = policy

    def inspect(self) -> dict[str, Any]:
        if not self.archive.is_file():
            raise IntegrityViolation(
                "Release archive is missing.",
                code="archive_missing",
                details={"path": str(self.archive)},
            )
        if self.archive.name.endswith((".tar.gz", ".tgz")):
            entries = self._inspect_tar()
            archive_type = "tar.gz"
        elif self.archive.suffix.casefold() == ".zip":
            entries = self._inspect_zip()
            archive_type = "zip"
        else:
            raise IntegrityViolation(
                "Release archive format is unsupported.",
                code="archive_format_unsupported",
                details={"path": str(self.archive)},
            )
        roots = {PurePosixPath(item["path"]).parts[0] for item in entries if item["path"] != "."}
        if len(roots) != 1:
            raise IntegrityViolation(
                "Release archive must contain exactly one top-level root.",
                code="archive_root_count",
                details={"roots": sorted(roots)},
            )
        folded: dict[str, str] = {}
        for item in entries:
            path = item["path"]
            key = path.casefold()
            prior = folded.get(key)
            if prior is not None:
                if prior == path:
                    raise IntegrityViolation(
                        "Archive contains duplicate entries.",
                        code="archive_duplicate_entry",
                        details={"path": path},
                    )
                raise IntegrityViolation(
                    "Archive contains case-colliding entries.",
                    code="archive_case_collision",
                    details={"first": prior, "second": path},
                )
            folded[key] = path
        return {
            "schema": "zyra.archive-inspection/v1",
            "ready": True,
            "archive": str(self.archive),
            "archive_type": archive_type,
            "archive_sha256": sha256_file(self.archive),
            "archive_size": self.archive.stat().st_size,
            "root": next(iter(roots)),
            "entry_count": len(entries),
            "entries_digest": stable_digest(entries),
        }

    def safe_extract(self, destination: Path) -> dict[str, Any]:
        report = self.inspect()
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise IntegrityViolation(
                "Archive destination must be empty.",
                code="archive_destination_not_empty",
                details={"destination": str(destination)},
            )
        if report["archive_type"] == "tar.gz":
            with tarfile.open(self.archive, "r:gz") as stream:
                for member in stream.getmembers():
                    normalized = normalize_relative_path(member.name)
                    target = resolve_below(destination, normalized)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if member.issym() or member.islnk():
                        raise IntegrityViolation(
                            "Archive links are rejected.",
                            code="archive_link_rejected",
                            details={"path": normalized},
                        )
                    if not member.isfile():
                        raise IntegrityViolation(
                            "Archive contains a special entry.",
                            code="archive_special_entry",
                            details={"path": normalized},
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = stream.extractfile(member)
                    if source is None:
                        raise IntegrityViolation(
                            "Archive member cannot be read.",
                            code="archive_member_unreadable",
                            details={"path": normalized},
                        )
                    with source, target.open("wb") as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
                    if os.name != "nt":
                        target.chmod(stat.S_IMODE(member.mode))
        else:
            with zipfile.ZipFile(self.archive, "r") as stream:
                for info in stream.infolist():
                    normalized = normalize_relative_path(info.filename)
                    target = resolve_below(destination, normalized)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    unix_mode = info.external_attr >> 16
                    if stat.S_ISLNK(unix_mode):
                        raise IntegrityViolation(
                            "Archive links are rejected.",
                            code="archive_link_rejected",
                            details={"path": normalized},
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with stream.open(info, "r") as source, target.open("wb") as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
                    if os.name != "nt" and unix_mode:
                        target.chmod(stat.S_IMODE(unix_mode))
        report["destination"] = str(destination.resolve())
        report["extracted"] = True
        return report

    def _inspect_tar(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        total_size = 0
        with tarfile.open(self.archive, "r:gz") as stream:
            for member in stream.getmembers():
                path = normalize_relative_path(member.name)
                if member.issym() or member.islnk():
                    raise IntegrityViolation(
                        "Archive links are rejected.",
                        code="archive_link_rejected",
                        details={"path": path, "target": member.linkname},
                    )
                if not (member.isfile() or member.isdir()):
                    raise IntegrityViolation(
                        "Archive special entries are rejected.",
                        code="archive_special_entry",
                        details={"path": path},
                    )
                total_size += member.size
                self._enforce_archive_limits(len(output) + 1, total_size, path, member.size)
                output.append(
                    {
                        "path": path,
                        "size": member.size,
                        "mode": stat.S_IMODE(member.mode),
                        "directory": member.isdir(),
                    }
                )
        return output

    def _inspect_zip(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        total_size = 0
        with zipfile.ZipFile(self.archive, "r") as stream:
            for info in stream.infolist():
                path = normalize_relative_path(info.filename)
                unix_mode = info.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise IntegrityViolation(
                        "Archive links are rejected.",
                        code="archive_link_rejected",
                        details={"path": path},
                    )
                total_size += info.file_size
                self._enforce_archive_limits(
                    len(output) + 1,
                    total_size,
                    path,
                    info.file_size,
                )
                output.append(
                    {
                        "path": path,
                        "size": info.file_size,
                        "mode": stat.S_IMODE(unix_mode),
                        "directory": info.is_dir(),
                    }
                )
        return output

    def _enforce_archive_limits(
        self,
        count: int,
        total_size: int,
        path: str,
        size: int,
    ) -> None:
        if count > self.policy.maximum_file_count:
            raise IntegrityViolation(
                "Archive exceeds the file-count policy.",
                code="archive_file_count_limit",
                details={"count": count, "limit": self.policy.maximum_file_count},
            )
        if size > self.policy.maximum_file_size:
            raise IntegrityViolation(
                "Archive member exceeds the size policy.",
                code="archive_file_size_limit",
                details={
                    "path": path,
                    "size": size,
                    "limit": self.policy.maximum_file_size,
                },
            )
        if total_size > self.policy.maximum_bundle_size:
            raise IntegrityViolation(
                "Archive expands beyond the size policy.",
                code="archive_expanded_size_limit",
                details={
                    "size": total_size,
                    "limit": self.policy.maximum_bundle_size,
                },
            )


def encode_digest(value: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value).digest()).decode("ascii").rstrip("=")


__all__ = [
    "ArchiveInspector",
    "BoundaryScanner",
    "BunLock",
    "CanonicalTreeWalker",
    "ChecksumBuilder",
    "ChecksumVerifier",
    "PythonLock",
    "TreeEntry",
    "classify_file",
    "encode_digest",
    "normalize_relative_path",
    "resolve_below",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "stable_digest",
    "stable_json",
]
