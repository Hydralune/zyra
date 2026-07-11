from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .errors import (
    SkillContainmentError,
    SkillPathError,
    SkillReadRace,
    SkillResourceNotFound,
    SkillSymlinkError,
)


WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(
            device=int(getattr(value, "st_dev", 0)),
            inode=int(getattr(value, "st_ino", 0)),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            mode=int(value.st_mode),
        )

    def same_object(self, other: "FileIdentity") -> bool:
        if self.device and other.device and self.inode and other.inode:
            return self.device == other.device and self.inode == other.inode
        return self.size == other.size and self.mtime_ns == other.mtime_ns


def normalize_relative_path(value: str, *, allow_directory: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise SkillPathError("empty relative path")
    if "\x00" in raw:
        raise SkillPathError("NUL byte is forbidden in skill paths")
    if raw.startswith(("/", "\\", "//", "\\\\")) or DRIVE_PATTERN.match(raw):
        raise SkillContainmentError("absolute paths are forbidden", detail={"path": raw})
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise SkillContainmentError("absolute paths are forbidden", detail={"path": raw})
    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SkillContainmentError("skill path contains a traversal component", detail={"path": raw})
    for part in parts:
        stem = part.split(".", 1)[0].upper().rstrip(" .")
        if stem in WINDOWS_DEVICE_NAMES:
            raise SkillPathError("reserved device name is forbidden", detail={"path": raw})
        if part.endswith((" ", ".")):
            raise SkillPathError("trailing spaces or dots are forbidden", detail={"path": raw})
        if any(ord(char) < 32 for char in part):
            raise SkillPathError("control character is forbidden", detail={"path": raw})
    if not allow_directory and normalized.endswith("/"):
        raise SkillPathError("file path may not end with a separator", detail={"path": raw})
    return str(path)


def canonical_root(path: str | Path, *, require_exists: bool = True) -> Path:
    candidate = Path(path)
    if require_exists and not candidate.exists():
        raise SkillResourceNotFound("skill source root does not exist", detail={"path": str(path)})
    try:
        resolved = candidate.resolve(strict=require_exists)
    except OSError as error:
        raise SkillPathError("unable to resolve skill source root", detail={"path": str(path)}) from error
    if require_exists and not resolved.is_dir():
        raise SkillPathError("skill source root must be a directory", detail={"path": str(path)})
    return resolved


def is_within(path: str | Path, root: str | Path) -> bool:
    candidate = Path(path)
    boundary = Path(root)
    try:
        common = os.path.commonpath((os.path.normcase(str(candidate)), os.path.normcase(str(boundary))))
    except ValueError:
        return False
    return common == os.path.normcase(str(boundary))


def assert_within(path: str | Path, root: str | Path, *, label: str = "skill path") -> None:
    candidate = Path(path)
    boundary = Path(root)
    if not is_within(candidate, boundary):
        raise SkillContainmentError(
            f"{label} escapes its declared root",
            detail={"path": str(candidate), "root": str(boundary)},
        )


def assert_no_symlink_components(path: str | Path, root: str | Path, *, include_leaf: bool = True) -> None:
    candidate = Path(path)
    boundary = Path(root)
    assert_within(candidate, boundary)
    relative = candidate.relative_to(boundary)
    current = boundary
    components = relative.parts if include_leaf else relative.parts[:-1]
    for part in components:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise SkillResourceNotFound("skill path component does not exist", detail={"path": str(current)}) from error
        if stat.S_ISLNK(info.st_mode):
            raise SkillSymlinkError("symlinks are forbidden in skill packages", detail={"path": str(current)})
        if stat.S_ISDIR(info.st_mode) or current == candidate:
            continue
        if current != candidate:
            raise SkillPathError("non-directory component in skill path", detail={"path": str(current)})


def resolve_member(
    root: str | Path,
    relative_path: str,
    *,
    require_file: bool = True,
    reject_symlinks: bool = True,
) -> Path:
    boundary = canonical_root(root)
    normalized = normalize_relative_path(relative_path)
    lexical = boundary.joinpath(*PurePosixPath(normalized).parts)
    assert_within(lexical, boundary, label="lexical skill path")
    if reject_symlinks:
        assert_no_symlink_components(lexical, boundary)
    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError as error:
        raise SkillResourceNotFound("skill resource does not exist", detail={"path": normalized}) from error
    except OSError as error:
        raise SkillPathError("skill resource cannot be resolved", detail={"path": normalized}) from error
    assert_within(resolved, boundary, label="resolved skill path")
    if reject_symlinks and resolved != lexical:
        raise SkillSymlinkError("resolved skill path changed through a link", detail={"path": normalized})
    if require_file and not resolved.is_file():
        raise SkillPathError("skill resource must be a regular file", detail={"path": normalized})
    return resolved


def open_verified_binary(
    path: str | Path,
    *,
    root: str | Path,
    max_bytes: int,
    reject_symlinks: bool = True,
) -> tuple[BinaryIO, FileIdentity]:
    candidate = Path(path)
    boundary = canonical_root(root)
    assert_within(candidate, boundary)
    if reject_symlinks:
        assert_no_symlink_components(candidate, boundary)
    before = FileIdentity.from_stat(candidate.stat(follow_symlinks=False))
    if not stat.S_ISREG(before.mode):
        raise SkillPathError("skill resource is not a regular file", detail={"path": str(candidate)})
    if before.size > max_bytes:
        raise SkillPathError(
            "skill resource exceeds byte limit",
            detail={"path": str(candidate), "size": before.size, "limit": max_bytes},
        )
    handle = candidate.open("rb")
    opened = FileIdentity.from_stat(os.fstat(handle.fileno()))
    if not before.same_object(opened):
        handle.close()
        raise SkillReadRace("skill resource changed between validation and open", detail={"path": str(candidate)})
    return handle, before


def read_verified_bytes(
    path: str | Path,
    *,
    root: str | Path,
    max_bytes: int,
    reject_symlinks: bool = True,
) -> bytes:
    handle, before = open_verified_binary(
        path,
        root=root,
        max_bytes=max_bytes,
        reject_symlinks=reject_symlinks,
    )
    try:
        payload = handle.read(max_bytes + 1)
        after_open = FileIdentity.from_stat(os.fstat(handle.fileno()))
    finally:
        handle.close()
    if len(payload) > max_bytes:
        raise SkillPathError("skill resource exceeds byte limit", detail={"path": str(path), "limit": max_bytes})
    try:
        after_path = FileIdentity.from_stat(Path(path).stat(follow_symlinks=False))
    except OSError as error:
        raise SkillReadRace("skill resource disappeared during read", detail={"path": str(path)}) from error
    if not before.same_object(after_open) or not before.same_object(after_path):
        raise SkillReadRace("skill resource changed during read", detail={"path": str(path)})
    if len(payload) != before.size:
        raise SkillReadRace("skill resource size changed during read", detail={"path": str(path)})
    return payload


def read_verified_text(
    path: str | Path,
    *,
    root: str | Path,
    max_bytes: int,
    encoding: str = "utf-8",
) -> str:
    payload = read_verified_bytes(path, root=root, max_bytes=max_bytes)
    if b"\x00" in payload:
        raise SkillPathError("binary data is forbidden in text skill resources", detail={"path": str(path)})
    try:
        return payload.decode(encoding)
    except UnicodeDecodeError as error:
        raise SkillPathError("skill resource is not valid UTF-8", detail={"path": str(path)}) from error


def iter_skill_directories(root: str | Path, *, max_depth: int = 8) -> list[Path]:
    boundary = canonical_root(root)
    discovered: list[Path] = []
    stack: list[tuple[Path, int]] = [(boundary, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        entries = sorted(current.iterdir(), key=lambda item: item.name.casefold(), reverse=True)
        if (current / "SKILL.md").exists():
            resolve_member(current, "SKILL.md")
            discovered.append(current)
            continue
        for entry in entries:
            try:
                info = entry.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise SkillSymlinkError("symlink discovered under skill source root", detail={"path": str(entry)})
            if stat.S_ISDIR(info.st_mode):
                stack.append((entry, depth + 1))
    return sorted(discovered, key=lambda item: item.as_posix().casefold())
