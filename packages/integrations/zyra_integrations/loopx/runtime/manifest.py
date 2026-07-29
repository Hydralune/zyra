from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import LoopXRuntimeError


LOOPX_VERSION = "0.2.13"
LOOPX_SOURCE_REF = "v0.2.13"
# The slice freezes the annotated tag object as the public source identity.
LOOPX_SOURCE_COMMIT = "a2c072d412d90839132e1cf39c23dd431c394175"
# Git peels the annotated tag above to this actual source commit.
LOOPX_SOURCE_TREE_COMMIT = "7232dca45ec2ca996edc43b2d3558edc802c844e"
PACKAGE_LOCK_SCHEMA = "zyra.loopx-package-lock/v2"
SOURCE_MANIFEST_SCHEMA = "zyra.loopx-embedded-source-manifest/v1"
PACKAGE_LOCK_RELATIVE_PATH = Path("config") / "loopx" / "package-lock.json"
EMBEDDED_ROOT_RELATIVE_PATH = (
    Path("packages") / "integrations" / "loopx_runtime"
)
SOURCE_MANIFEST_NAME = "SOURCE-MANIFEST.json"
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_IGNORED_CACHE_PARTS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


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
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def source_manifest_digest(files: Iterable[Mapping[str, Any]]) -> str:
    normalized = sorted(
        (
            {
                "path": str(item["path"]).replace("\\", "/"),
                "sha256": str(item["sha256"]),
                "size": int(item["size"]),
                "executable": bool(item.get("executable", False)),
            }
            for item in files
        ),
        key=lambda item: item["path"].encode("utf-8"),
    )
    return stable_digest(normalized)


def _safe_relative(value: str) -> str:
    raw = value.replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise LoopXRuntimeError(
            "LoopX manifest contains an unsafe relative path.",
            code="loopx_package_lock_invalid",
            details={"path": value},
        )
    return path.as_posix()


@dataclass(frozen=True, slots=True)
class LoopXPackageLock:
    package_root: Path
    value: Mapping[str, Any]
    path: Path

    @classmethod
    def load(
        cls,
        package_root: Path,
        *,
        lock_path: Path | None = None,
    ) -> "LoopXPackageLock":
        root = package_root.resolve()
        selected = (
            lock_path.resolve()
            if lock_path is not None
            else (root / PACKAGE_LOCK_RELATIVE_PATH).resolve()
        )
        if not selected.is_file():
            packaged = Path(__file__).with_name("package-lock.json").resolve()
            if packaged.is_file():
                selected = packaged
            else:
                raise LoopXRuntimeError(
                    "Pinned LoopX package lock is missing.",
                    code="loopx_package_lock_missing",
                    details={"path": str(selected)},
                )
        try:
            value = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LoopXRuntimeError(
                "Pinned LoopX package lock is unreadable.",
                code="loopx_package_lock_invalid",
                details={"path": str(selected), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise LoopXRuntimeError(
                "Pinned LoopX package lock root must be an object.",
                code="loopx_package_lock_invalid",
            )
        instance = cls(package_root=root, value=dict(value), path=selected)
        instance.validate()
        return instance

    @classmethod
    def discover(cls, package_root: Path) -> "LoopXPackageLock | None":
        root = package_root.resolve()
        if (root / PACKAGE_LOCK_RELATIVE_PATH).is_file():
            return cls.load(root)
        packaged = Path(__file__).with_name("package-lock.json")
        return cls.load(root, lock_path=packaged) if packaged.is_file() else None

    @property
    def package(self) -> Mapping[str, Any]:
        value = self.value.get("package")
        assert isinstance(value, Mapping)
        return value

    @property
    def source_record(self) -> Mapping[str, Any]:
        value = self.value.get("source_manifest")
        assert isinstance(value, Mapping)
        return value

    @property
    def embedded_root(self) -> Path | None:
        source_root = self.package_root / EMBEDDED_ROOT_RELATIVE_PATH
        if source_root.is_dir():
            return source_root.resolve()
        return None

    @property
    def manifest_path(self) -> Path:
        embedded = self.embedded_root
        if embedded is not None:
            return embedded / SOURCE_MANIFEST_NAME
        packaged = Path(__file__).with_name(SOURCE_MANIFEST_NAME)
        return packaged.resolve()

    @property
    def source_manifest(self) -> Mapping[str, Any]:
        path = self.manifest_path
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LoopXRuntimeError(
                "Embedded LoopX source manifest is unreadable.",
                code="loopx_source_manifest_mismatch",
                details={"path": str(path), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise LoopXRuntimeError(
                "Embedded LoopX source manifest root must be an object.",
                code="loopx_source_manifest_mismatch",
                details={"path": str(path)},
            )
        return value

    @property
    def source_digest(self) -> str:
        return str(self.source_record["tree_digest"])

    @property
    def lock_digest(self) -> str:
        return str(self.value["lock_digest"])

    @property
    def artifacts(self) -> dict[str, Any]:
        # Compatibility surface: archive artifacts were intentionally retired.
        return {}

    @property
    def profiles(self) -> Mapping[str, Any]:
        value = self.value.get("profiles")
        assert isinstance(value, Mapping)
        return value

    @property
    def manifest_files(self) -> tuple[Mapping[str, Any], ...]:
        raw = self.source_manifest.get("files")
        assert isinstance(raw, Sequence)
        return tuple(item for item in raw if isinstance(item, Mapping))

    def validate(self) -> None:
        if self.value.get("schema") != PACKAGE_LOCK_SCHEMA:
            self._invalid("unsupported package lock schema")
        package = self.value.get("package")
        source = self.value.get("source_manifest")
        profiles = self.value.get("profiles")
        if not all(isinstance(item, Mapping) for item in (package, source, profiles)):
            self._invalid("package lock sections are incomplete")
        assert isinstance(package, Mapping)
        assert isinstance(source, Mapping)
        if (
            package.get("name") != "loopx"
            or package.get("version") != LOOPX_VERSION
            or package.get("source_ref") != LOOPX_SOURCE_REF
            or package.get("source_commit") != LOOPX_SOURCE_COMMIT
            or package.get("source_tree_commit") != LOOPX_SOURCE_TREE_COMMIT
            or package.get("source_role") != "supplementary_implementation"
            or package.get("migration_mode")
            != "pinned_embedded_source_integration"
        ):
            self._invalid("package identity does not match the frozen decision")
        if (
            _HEX_40.fullmatch(str(package.get("source_commit") or "")) is None
            or _HEX_40.fullmatch(str(package.get("source_tree_commit") or ""))
            is None
        ):
            self._invalid("source identities must be full Git object ids")
        lock_digest = str(self.value.get("lock_digest") or "")
        unsigned = dict(self.value)
        unsigned.pop("lock_digest", None)
        if _HEX_64.fullmatch(lock_digest) is None or stable_digest(unsigned) != lock_digest:
            self._invalid("package lock self-digest does not match")
        if source.get("schema") != SOURCE_MANIFEST_SCHEMA:
            self._invalid("source manifest schema is invalid")
        manifest_path = self.manifest_path
        if not manifest_path.is_file():
            raise LoopXRuntimeError(
                "Embedded LoopX source manifest is missing.",
                code="loopx_embedded_source_missing",
                details={"path": str(manifest_path)},
            )
        expected_manifest_digest = str(source.get("sha256") or "")
        if (
            _HEX_64.fullmatch(expected_manifest_digest) is None
            or sha256_file(manifest_path) != expected_manifest_digest
        ):
            raise LoopXRuntimeError(
                "Embedded LoopX source manifest does not match the package lock.",
                code="loopx_source_manifest_mismatch",
                details={"path": str(manifest_path)},
            )
        manifest = self.source_manifest
        if (
            manifest.get("schema") != SOURCE_MANIFEST_SCHEMA
            or manifest.get("version") != LOOPX_VERSION
            or manifest.get("source_ref") != LOOPX_SOURCE_REF
            or manifest.get("source_commit") != LOOPX_SOURCE_COMMIT
            or manifest.get("source_tree_commit") != LOOPX_SOURCE_TREE_COMMIT
            or manifest.get("tree_digest") != source.get("tree_digest")
            or manifest.get("file_count") != source.get("file_count")
        ):
            raise LoopXRuntimeError(
                "Embedded LoopX source manifest identity does not match the lock.",
                code="loopx_source_manifest_mismatch",
            )
        files = manifest.get("files")
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            self._invalid("source file manifest is invalid")
        normalized: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, Mapping):
                self._invalid("source file record is not an object")
            path = _safe_relative(str(item.get("path") or ""))
            digest = str(item.get("sha256") or "")
            size = int(item.get("size", -1))
            if path in seen or _HEX_64.fullmatch(digest) is None or size < 0:
                self._invalid("source file record is invalid")
            seen.add(path)
            normalized.append(item)
        if source_manifest_digest(normalized) != str(manifest.get("tree_digest") or ""):
            raise LoopXRuntimeError(
                "Embedded LoopX tree digest does not match its file records.",
                code="loopx_source_manifest_mismatch",
            )

    def verify_artifacts(self, *, deep: bool = False) -> dict[str, Any]:
        return self.verify_embedded_source(deep=deep)

    def verify_embedded_source(self, *, deep: bool = True) -> dict[str, Any]:
        root = self.embedded_root
        if root is None:
            # Installed-wheel mode is verified by import provenance in doctor.
            return {
                "schema": "zyra.loopx-embedded-source-verification/v1",
                "ready": True,
                "mode": "installed_distribution",
                "version": LOOPX_VERSION,
                "source_commit": LOOPX_SOURCE_COMMIT,
                "source_tree_commit": LOOPX_SOURCE_TREE_COMMIT,
                "tree_digest": self.source_digest,
                "file_count": len(self.manifest_files),
            }
        changed: list[dict[str, Any]] = []
        expected_paths: set[str] = set()
        for item in self.manifest_files:
            relative = _safe_relative(str(item["path"]))
            expected_paths.add(relative)
            path = root.joinpath(*PurePosixPath(relative).parts)
            if not path.is_file():
                changed.append({"path": relative, "reason": "missing"})
                continue
            size = path.stat().st_size
            digest = sha256_file(path)
            if size != int(item["size"]) or digest != str(item["sha256"]):
                changed.append(
                    {
                        "path": relative,
                        "reason": "content_mismatch",
                        "expected_size": int(item["size"]),
                        "actual_size": size,
                        "expected_sha256": str(item["sha256"]),
                        "actual_sha256": digest,
                    }
                )
        actual_paths = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and path.name != SOURCE_MANIFEST_NAME
            and not _IGNORED_CACHE_PARTS.intersection(path.parts)
            and path.suffix.casefold() not in {".pyc", ".pyo"}
        }
        changed.extend(
            {"path": relative, "reason": "unexpected"}
            for relative in sorted(actual_paths - expected_paths)
        )
        if changed:
            raise LoopXRuntimeError(
                "Embedded LoopX source tree failed integrity verification.",
                code="loopx_embedded_source_tampered",
                details={"root": str(root), "changed": changed[:20]},
            )
        return {
            "schema": "zyra.loopx-embedded-source-verification/v1",
            "ready": True,
            "mode": "embedded_source",
            "root": str(root),
            "version": LOOPX_VERSION,
            "source_commit": LOOPX_SOURCE_COMMIT,
            "source_tree_commit": LOOPX_SOURCE_TREE_COMMIT,
            "tree_digest": self.source_digest,
            "file_count": len(self.manifest_files),
            "deep": deep,
            "archive_artifact_count": 0,
        }

    def resolve_artifact(self, artifact: Any) -> Path:
        raise LoopXRuntimeError(
            "LoopX archive artifacts are retired and have no runtime locator.",
            code="loopx_runtime_unavailable",
        )

    def component(self) -> dict[str, Any]:
        package = self.package
        return {
            "name": "loopx",
            "version": LOOPX_VERSION,
            "purl": f"pkg:pypi/loopx@{LOOPX_VERSION}",
            "licenses": ("MIT",),
            "hashes": (
                {"alg": "SHA-256", "content": self.source_digest},
            ),
            "properties": (
                {
                    "name": "zyra:source-role",
                    "value": str(package["source_role"]),
                },
                {
                    "name": "zyra:migration-mode",
                    "value": str(package["migration_mode"]),
                },
                {
                    "name": "zyra:source-ref-object",
                    "value": LOOPX_SOURCE_COMMIT,
                },
                {
                    "name": "zyra:source-tree-commit",
                    "value": LOOPX_SOURCE_TREE_COMMIT,
                },
                {
                    "name": "zyra:line-bucket",
                    "value": "runtime-assets/vendor-like",
                },
            ),
        }

    def _invalid(self, reason: str) -> None:
        raise LoopXRuntimeError(
            f"Pinned LoopX package lock is invalid: {reason}.",
            code="loopx_package_lock_invalid",
            details={"path": str(self.path), "reason": reason},
        )


__all__ = [
    "EMBEDDED_ROOT_RELATIVE_PATH",
    "LOOPX_SOURCE_COMMIT",
    "LOOPX_SOURCE_REF",
    "LOOPX_SOURCE_TREE_COMMIT",
    "LOOPX_VERSION",
    "PACKAGE_LOCK_RELATIVE_PATH",
    "PACKAGE_LOCK_SCHEMA",
    "SOURCE_MANIFEST_NAME",
    "SOURCE_MANIFEST_SCHEMA",
    "LoopXPackageLock",
    "sha256_file",
    "source_manifest_digest",
    "stable_digest",
    "stable_json",
]
