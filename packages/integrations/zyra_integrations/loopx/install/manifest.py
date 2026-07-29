from __future__ import annotations

import hashlib
import json
import re
import tarfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import LoopXInstallError


LOOPX_VERSION = "0.2.4"
LOOPX_SOURCE_COMMIT = "8e79843704a40d8069a9cab4ede6edc6d29f671b"
PACKAGE_LOCK_SCHEMA = "zyra.loopx-package-lock/v1"
SOURCE_MANIFEST_SCHEMA = "zyra.loopx-source-manifest/v1"
PACKAGE_LOCK_RELATIVE_PATH = Path("config") / "loopx" / "package-lock.json"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ARCHIVE_PART = re.compile(r"^[^\\:\x00]+$")


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
    normalized = [
        {
            "path": str(item["path"]),
            "sha256": str(item["sha256"]),
            "size": int(item["size"]),
            "executable": bool(item.get("executable", False)),
        }
        for item in files
    ]
    return stable_digest(normalized)


def _safe_relative(value: str) -> str:
    raw = value.replace("\\", "/").strip("/")
    if not raw:
        raise LoopXInstallError(
            "LoopX package path is empty.",
            code="loopx_package_lock_invalid",
            details={"path": value},
        )
    parts = PurePosixPath(raw).parts
    if (
        PurePosixPath(raw).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(_SAFE_ARCHIVE_PART.fullmatch(part) is None for part in parts)
    ):
        raise LoopXInstallError(
            "LoopX package path is not a safe relative path.",
            code="loopx_package_lock_invalid",
            details={"path": value},
        )
    return PurePosixPath(*parts).as_posix()


@dataclass(frozen=True, slots=True)
class LockedArtifact:
    name: str
    path: str
    sha256: str
    size: int
    kind: str

    @classmethod
    def parse(cls, name: str, value: Mapping[str, Any]) -> "LockedArtifact":
        path = _safe_relative(str(value.get("path") or ""))
        digest = str(value.get("sha256") or "")
        size = int(value.get("size", -1))
        kind = str(value.get("kind") or "")
        if _HEX_64.fullmatch(digest) is None or size < 0 or not kind:
            raise LoopXInstallError(
                "LoopX artifact metadata is invalid.",
                code="loopx_package_lock_invalid",
                details={"artifact": name},
            )
        return cls(name=name, path=path, sha256=digest, size=size, kind=kind)


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
            raise LoopXInstallError(
                "Pinned LoopX package lock is missing.",
                code="loopx_package_lock_missing",
                details={"path": str(selected)},
            )
        try:
            value = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LoopXInstallError(
                "Pinned LoopX package lock is unreadable.",
                code="loopx_package_lock_invalid",
                details={"path": str(selected), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise LoopXInstallError(
                "Pinned LoopX package lock root must be an object.",
                code="loopx_package_lock_invalid",
            )
        instance = cls(package_root=root, value=dict(value), path=selected)
        instance.validate()
        return instance

    @classmethod
    def discover(cls, package_root: Path) -> "LoopXPackageLock | None":
        path = package_root.resolve() / PACKAGE_LOCK_RELATIVE_PATH
        return cls.load(package_root) if path.is_file() else None

    @property
    def package(self) -> Mapping[str, Any]:
        value = self.value.get("package")
        assert isinstance(value, Mapping)
        return value

    @property
    def source_manifest(self) -> Mapping[str, Any]:
        value = self.value.get("source_manifest")
        assert isinstance(value, Mapping)
        return value

    @property
    def source_digest(self) -> str:
        return str(self.source_manifest["source_digest"])

    @property
    def lock_digest(self) -> str:
        return str(self.value["lock_digest"])

    @property
    def artifacts(self) -> dict[str, LockedArtifact]:
        raw = self.value.get("artifacts")
        assert isinstance(raw, Mapping)
        return {
            str(name): LockedArtifact.parse(str(name), value)
            for name, value in raw.items()
            if isinstance(value, Mapping)
        }

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
        artifacts = self.value.get("artifacts")
        profiles = self.value.get("profiles")
        if not all(
            isinstance(item, Mapping)
            for item in (package, source, artifacts, profiles)
        ):
            self._invalid("package lock sections are incomplete")
        assert isinstance(package, Mapping)
        assert isinstance(source, Mapping)
        assert isinstance(artifacts, Mapping)
        assert isinstance(profiles, Mapping)
        if (
            package.get("name") != "loopx"
            or package.get("version") != LOOPX_VERSION
            or package.get("source_commit") != LOOPX_SOURCE_COMMIT
            or package.get("source_role") != "supplementary_implementation"
            or package.get("migration_mode") != "pinned_package_integration"
        ):
            self._invalid("package identity does not match the frozen decision")
        if source.get("schema") != SOURCE_MANIFEST_SCHEMA:
            self._invalid("source manifest schema is invalid")
        files = source.get("files")
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            self._invalid("source file manifest is invalid")
        normalized_files: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, Mapping):
                self._invalid("source file record is not an object")
            path = _safe_relative(str(item.get("path") or ""))
            digest = str(item.get("sha256") or "")
            size = int(item.get("size", -1))
            if path in seen or _HEX_64.fullmatch(digest) is None or size < 0:
                self._invalid("source file record is invalid", path=path)
            seen.add(path)
            normalized_files.append(
                {
                    "path": path,
                    "sha256": digest,
                    "size": size,
                    "executable": bool(item.get("executable", False)),
                }
            )
        if int(source.get("file_count") or -1) != len(normalized_files):
            self._invalid("source file count does not match")
        expected_source_digest = source_manifest_digest(normalized_files)
        if source.get("source_digest") != expected_source_digest:
            self._invalid("source manifest digest does not match")
        if source.get("source_commit") != LOOPX_SOURCE_COMMIT:
            self._invalid("source manifest commit does not match")
        parsed_artifacts = {
            str(name): LockedArtifact.parse(str(name), item)
            for name, item in artifacts.items()
            if isinstance(item, Mapping)
        }
        if set(parsed_artifacts) != {"wheel", "source_bundle"}:
            self._invalid("wheel and source bundle must both be locked")
        required_profiles = {
            "linux_wsl_upstream_semantics",
            "windows_release_offline_wheel",
        }
        if set(str(item) for item in profiles) != required_profiles:
            self._invalid("install profile set does not match the ADR")
        profile_digests: set[str] = set()
        for name in sorted(required_profiles):
            item = profiles[name]
            if not isinstance(item, Mapping):
                self._invalid("install profile is invalid", profile=name)
            profile_digests.add(str(item.get("source_digest") or ""))
            if item.get("network_access") is not False:
                self._invalid("install profile must be offline", profile=name)
            if item.get("user_home_write") is not False:
                self._invalid(
                    "install profile permits user-home writes",
                    profile=name,
                )
        if profile_digests != {expected_source_digest}:
            self._invalid("install profiles do not share one source digest")
        declared_lock_digest = str(self.value.get("lock_digest") or "")
        unlocked = dict(self.value)
        unlocked.pop("lock_digest", None)
        if (
            _HEX_64.fullmatch(declared_lock_digest) is None
            or stable_digest(unlocked) != declared_lock_digest
        ):
            self._invalid("package lock digest does not match")

    def verify_artifacts(self, *, deep: bool = False) -> dict[str, Any]:
        verified: dict[str, Any] = {}
        for name, artifact in self.artifacts.items():
            path = self.resolve_artifact(artifact)
            if not path.is_file():
                raise LoopXInstallError(
                    "Pinned LoopX artifact is missing.",
                    code="loopx_artifact_missing",
                    details={"artifact": name, "path": str(path)},
                )
            actual_size = path.stat().st_size
            actual_digest = sha256_file(path)
            if actual_size != artifact.size or actual_digest != artifact.sha256:
                raise LoopXInstallError(
                    "Pinned LoopX artifact failed its checksum.",
                    code="loopx_artifact_tampered",
                    details={
                        "artifact": name,
                        "path": str(path),
                        "expected_sha256": artifact.sha256,
                        "actual_sha256": actual_digest,
                        "expected_size": artifact.size,
                        "actual_size": actual_size,
                    },
                )
            verified[name] = {
                "path": str(path),
                "sha256": actual_digest,
                "size": actual_size,
            }
        wheel = self.resolve_artifact(self.artifacts["wheel"])
        verified["wheel"].update(self._verify_wheel(wheel))
        if deep:
            bundle = self.resolve_artifact(self.artifacts["source_bundle"])
            verified["source_bundle"].update(self._verify_source_bundle(bundle))
        return {
            "schema": "zyra.loopx-artifact-verification/v1",
            "ready": True,
            "version": LOOPX_VERSION,
            "source_commit": LOOPX_SOURCE_COMMIT,
            "source_digest": self.source_digest,
            "lock_digest": self.lock_digest,
            "artifacts": verified,
            "deep": deep,
        }

    def resolve_artifact(self, artifact: LockedArtifact) -> Path:
        path = (self.package_root / Path(*PurePosixPath(artifact.path).parts)).resolve()
        try:
            path.relative_to(self.package_root)
        except ValueError as error:
            raise LoopXInstallError(
                "LoopX artifact escapes the package root.",
                code="loopx_package_lock_invalid",
                details={"path": artifact.path},
            ) from error
        return path

    def component(self) -> dict[str, Any]:
        wheel = self.artifacts["wheel"]
        return {
            "name": "loopx",
            "version": LOOPX_VERSION,
            "purl": f"pkg:pypi/loopx@{LOOPX_VERSION}",
            "licenses": ("MIT",),
            "hashes": (
                {"alg": "SHA-256", "content": wheel.sha256},
                {"alg": "SHA-256", "content": self.source_digest},
            ),
            "properties": (
                {
                    "name": "zyra:source-role",
                    "value": "supplementary_implementation",
                },
                {
                    "name": "zyra:migration-mode",
                    "value": "pinned_package_integration",
                },
                {
                    "name": "zyra:source-commit",
                    "value": LOOPX_SOURCE_COMMIT,
                },
                {
                    "name": "zyra:source-digest",
                    "value": self.source_digest,
                },
                {"name": "zyra:line-bucket", "value": "runtime-assets/vendor-like"},
            ),
        }

    def _verify_wheel(self, path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(path) as archive:
                names = tuple(info.filename for info in archive.infolist())
                normalized = tuple(_safe_relative(name) for name in names)
                if len(normalized) != len(set(normalized)):
                    raise ValueError("duplicate wheel entries")
                required = {
                    "loopx/__init__.py",
                    f"loopx-{LOOPX_VERSION}.dist-info/METADATA",
                    f"loopx-{LOOPX_VERSION}.dist-info/WHEEL",
                    f"loopx-{LOOPX_VERSION}.dist-info/RECORD",
                    f"loopx-{LOOPX_VERSION}.dist-info/entry_points.txt",
                }
                if not required.issubset(normalized):
                    raise ValueError(
                        f"missing wheel entries: {sorted(required - set(normalized))}"
                    )
                init_text = archive.read("loopx/__init__.py").decode("utf-8")
                entry_text = archive.read(
                    f"loopx-{LOOPX_VERSION}.dist-info/entry_points.txt"
                ).decode("utf-8")
                metadata = archive.read(
                    f"loopx-{LOOPX_VERSION}.dist-info/METADATA"
                ).decode("utf-8")
        except (OSError, ValueError, zipfile.BadZipFile, UnicodeDecodeError) as error:
            raise LoopXInstallError(
                "Pinned LoopX wheel structure is invalid.",
                code="loopx_artifact_tampered",
                details={"artifact": "wheel", "error": str(error)},
            ) from error
        if (
            f'__version__ = "{LOOPX_VERSION}"' not in init_text
            or f"Version: {LOOPX_VERSION}" not in metadata
            or "loopx = loopx.cli:main" not in entry_text
        ):
            raise LoopXInstallError(
                "Pinned LoopX wheel identity or CLI entry is invalid.",
                code="loopx_artifact_tampered",
                details={"artifact": "wheel"},
            )
        return {
            "entry_count": len(normalized),
            "version": LOOPX_VERSION,
            "cli_entry": "loopx = loopx.cli:main",
        }

    def _verify_source_bundle(self, path: Path) -> dict[str, Any]:
        prefix = str(self.source_manifest.get("archive_prefix") or "")
        prefix = _safe_relative(prefix)
        expected = {
            str(item["path"]): {
                "sha256": str(item["sha256"]),
                "size": int(item["size"]),
            }
            for item in self.manifest_files
        }
        actual: dict[str, dict[str, Any]] = {}
        try:
            with tarfile.open(path, mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.issym() or member.islnk():
                        raise ValueError(f"link member is forbidden: {member.name}")
                    if not member.isfile():
                        continue
                    normalized = _safe_relative(member.name)
                    expected_prefix = prefix + "/"
                    if not normalized.startswith(expected_prefix):
                        raise ValueError(
                            f"source member is outside prefix: {normalized}"
                        )
                    relative = normalized[len(expected_prefix) :]
                    if relative in actual:
                        raise ValueError(f"duplicate source member: {relative}")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError(f"source member is unreadable: {relative}")
                    content = stream.read()
                    actual[relative] = {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
        except (OSError, ValueError, tarfile.TarError) as error:
            raise LoopXInstallError(
                "Pinned LoopX source bundle is invalid.",
                code="loopx_source_manifest_mismatch",
                details={"error": str(error)},
            ) from error
        if actual != expected:
            raise LoopXInstallError(
                "Pinned LoopX source bundle does not match its file manifest.",
                code="loopx_source_manifest_mismatch",
                details={
                    "missing": sorted(set(expected) - set(actual))[:25],
                    "extra": sorted(set(actual) - set(expected))[:25],
                    "changed": sorted(
                        name
                        for name in set(expected) & set(actual)
                        if expected[name] != actual[name]
                    )[:25],
                },
            )
        return {
            "file_count": len(actual),
            "source_digest": self.source_digest,
        }

    def _invalid(self, reason: str, **details: Any) -> None:
        raise LoopXInstallError(
            "Pinned LoopX package lock is invalid.",
            code="loopx_package_lock_invalid",
            details={"reason": reason, **details, "path": str(self.path)},
        )


__all__ = [
    "LOOPX_SOURCE_COMMIT",
    "LOOPX_VERSION",
    "PACKAGE_LOCK_RELATIVE_PATH",
    "PACKAGE_LOCK_SCHEMA",
    "SOURCE_MANIFEST_SCHEMA",
    "LockedArtifact",
    "LoopXPackageLock",
    "sha256_file",
    "source_manifest_digest",
    "stable_digest",
    "stable_json",
]
