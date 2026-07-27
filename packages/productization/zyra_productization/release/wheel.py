from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import tomllib
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import IntegrityViolation, InventoryViolation
from .integrity import normalize_relative_path, sha256_file, stable_digest
from .policy import DEFAULT_RELEASE_POLICY, ReleasePolicy


WHEEL_RECEIPT_SCHEMA = "zyra.python-wheel-receipt/v1"
_NORMALIZE_DISTRIBUTION = re.compile(r"[-_.]+")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]*$")


@dataclass(frozen=True, slots=True)
class WheelEntry:
    archive_path: str
    content: bytes
    executable: bool = False

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def record_digest(self) -> str:
        value = base64.urlsafe_b64encode(
            hashlib.sha256(self.content).digest()
        ).decode("ascii")
        return "sha256=" + value.rstrip("=")


class DeterministicWheelBuilder:
    """Build a pure-Python wheel without an ambient build backend.

    Release assembly already owns the exact source tree and does not need a
    mutable setuptools installation merely to copy Python packages into a
    wheel.  This builder deliberately implements the small, auditable subset
    of PEP 427 needed by Zyra: pure-Python packages, package data, console
    scripts and deterministic RECORD metadata.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.project_root = project_root.resolve()
        self.policy = policy

    def build(self, output_directory: Path) -> dict[str, Any]:
        pyproject_path = self.project_root / "pyproject.toml"
        if not pyproject_path.is_file():
            raise InventoryViolation(
                "Python wheel source has no pyproject.toml.",
                code="wheel_pyproject_missing",
                details={"path": str(pyproject_path)},
            )
        with pyproject_path.open("rb") as stream:
            pyproject = tomllib.load(stream)
        project = pyproject.get("project")
        if not isinstance(project, Mapping):
            raise InventoryViolation(
                "Python wheel project metadata is invalid.",
                code="wheel_project_metadata_invalid",
            )
        name = str(project.get("name") or "").strip()
        version = str(project.get("version") or "").strip()
        if not name or not version or not _SAFE_VERSION.fullmatch(version):
            raise InventoryViolation(
                "Python wheel name or version is invalid.",
                code="wheel_identity_invalid",
                details={"name": name, "version": version},
            )
        distribution = _NORMALIZE_DISTRIBUTION.sub("_", name)
        dist_info = f"{distribution}-{version}.dist-info"
        package_entries = self._package_entries(pyproject)
        metadata_entries = self._metadata_entries(
            project,
            distribution=distribution,
            version=version,
            dist_info=dist_info,
        )
        entries = self._unique_entries((*package_entries, *metadata_entries))
        record_path = f"{dist_info}/RECORD"
        record = self._record(entries, record_path=record_path)
        entries = (*entries, WheelEntry(record_path, record))
        output_directory.mkdir(parents=True, exist_ok=True)
        wheel_name = f"{distribution}-{version}-py3-none-any.whl"
        output = output_directory / wheel_name
        self._write(output, entries)
        receipt = {
            "schema": WHEEL_RECEIPT_SCHEMA,
            "ready": True,
            "distribution": name,
            "normalized_distribution": distribution,
            "version": version,
            "tag": "py3-none-any",
            "path": output.name,
            "sha256": sha256_file(output),
            "size": output.stat().st_size,
            "entry_count": len(entries),
            "package_entry_count": len(package_entries),
            "metadata_entry_count": len(entries) - len(package_entries),
            "source_digest": stable_digest(
                [
                    {
                        "path": item.archive_path,
                        "sha256": item.digest,
                        "size": len(item.content),
                    }
                    for item in package_entries
                ]
            ),
        }
        return receipt

    def verify(self, wheel: Path) -> dict[str, Any]:
        wheel = wheel.resolve()
        try:
            with zipfile.ZipFile(wheel) as archive:
                infos = archive.infolist()
                names = [normalize_relative_path(item.filename) for item in infos]
                if len(names) != len(set(names)):
                    raise IntegrityViolation(
                        "Python wheel contains duplicate paths.",
                        code="wheel_duplicate_path",
                    )
                if any(item.is_dir() for item in infos):
                    raise IntegrityViolation(
                        "Python wheel contains explicit directory entries.",
                        code="wheel_directory_entry",
                    )
                record_names = [
                    name for name in names if name.endswith(".dist-info/RECORD")
                ]
                wheel_names = [
                    name for name in names if name.endswith(".dist-info/WHEEL")
                ]
                metadata_names = [
                    name for name in names if name.endswith(".dist-info/METADATA")
                ]
                if not (
                    len(record_names) == len(wheel_names) == len(metadata_names) == 1
                ):
                    raise IntegrityViolation(
                        "Python wheel metadata set is incomplete.",
                        code="wheel_metadata_incomplete",
                        details={
                            "record": record_names,
                            "wheel": wheel_names,
                            "metadata": metadata_names,
                        },
                    )
                record_rows = self._parse_record(
                    archive.read(record_names[0]).decode("utf-8")
                )
                self._verify_record(
                    archive,
                    names=names,
                    record_rows=record_rows,
                    record_path=record_names[0],
                )
                wheel_metadata = archive.read(wheel_names[0]).decode("utf-8")
                if "Root-Is-Purelib: true" not in wheel_metadata:
                    raise IntegrityViolation(
                        "Python wheel is not declared pure-Python.",
                        code="wheel_not_purelib",
                    )
                if "Tag: py3-none-any" not in wheel_metadata:
                    raise IntegrityViolation(
                        "Python wheel has an unexpected compatibility tag.",
                        code="wheel_tag_invalid",
                    )
        except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as error:
            raise IntegrityViolation(
                "Python wheel cannot be verified.",
                code="wheel_unreadable",
                details={"path": str(wheel), "error": str(error)},
            ) from error
        return {
            "schema": "zyra.python-wheel-verification/v1",
            "ready": True,
            "path": str(wheel),
            "sha256": sha256_file(wheel),
            "size": wheel.stat().st_size,
            "entry_count": len(names),
            "record_entry_count": len(record_rows),
        }

    def _package_entries(
        self,
        pyproject: Mapping[str, Any],
    ) -> tuple[WheelEntry, ...]:
        tool = pyproject.get("tool")
        setuptools = tool.get("setuptools") if isinstance(tool, Mapping) else None
        find = setuptools.get("packages", {}).get("find") if isinstance(
            setuptools, Mapping
        ) else None
        if not isinstance(find, Mapping):
            raise InventoryViolation(
                "Python package discovery configuration is missing.",
                code="wheel_package_discovery_missing",
            )
        roots = find.get("where")
        includes = find.get("include")
        if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes)):
            raise InventoryViolation(
                "Python package roots are invalid.",
                code="wheel_package_roots_invalid",
            )
        patterns = (
            tuple(str(item) for item in includes)
            if isinstance(includes, Sequence)
            and not isinstance(includes, (str, bytes))
            else ("*",)
        )
        entries: list[WheelEntry] = []
        packages: set[str] = set()
        for raw_root in roots:
            relative_root = normalize_relative_path(str(raw_root))
            root = self.project_root.joinpath(*PurePosixPath(relative_root).parts)
            if not root.is_dir():
                raise InventoryViolation(
                    "Configured Python package root is missing.",
                    code="wheel_package_root_missing",
                    details={"root": relative_root},
                )
            for initializer in sorted(
                root.rglob("__init__.py"),
                key=lambda item: item.relative_to(root).as_posix().encode("utf-8"),
            ):
                package_root = initializer.parent
                package_name = package_root.relative_to(root).as_posix().replace(
                    "/",
                    ".",
                )
                top_level = package_name.split(".", 1)[0]
                if not self._matches(top_level, patterns):
                    continue
                packages.add(package_name)
                for source in self._package_files(package_root):
                    archive_path = source.relative_to(root).as_posix()
                    entries.append(
                        WheelEntry(
                            normalize_relative_path(archive_path),
                            source.read_bytes(),
                            executable=self._is_executable(source),
                        )
                    )
        if not packages:
            raise InventoryViolation(
                "Python package discovery produced no packages.",
                code="wheel_package_set_empty",
            )
        return self._unique_entries(entries)

    def _metadata_entries(
        self,
        project: Mapping[str, Any],
        *,
        distribution: str,
        version: str,
        dist_info: str,
    ) -> tuple[WheelEntry, ...]:
        metadata_lines = [
            "Metadata-Version: 2.3",
            f"Name: {str(project['name'])}",
            f"Version: {version}",
        ]
        summary = str(project.get("description") or "").strip()
        if summary:
            metadata_lines.append(f"Summary: {self._header_value(summary)}")
        requires_python = str(project.get("requires-python") or "").strip()
        if requires_python:
            metadata_lines.append(f"Requires-Python: {requires_python}")
        dependencies = project.get("dependencies")
        if isinstance(dependencies, Sequence) and not isinstance(
            dependencies, (str, bytes)
        ):
            for dependency in dependencies:
                metadata_lines.append(f"Requires-Dist: {str(dependency)}")
        optional = project.get("optional-dependencies")
        if isinstance(optional, Mapping):
            for extra in sorted(str(item) for item in optional):
                metadata_lines.append(f"Provides-Extra: {extra}")
                values = optional[extra]
                if isinstance(values, Sequence) and not isinstance(
                    values, (str, bytes)
                ):
                    for dependency in values:
                        metadata_lines.append(
                            f'Requires-Dist: {str(dependency)}; extra == "{extra}"'
                        )
        metadata = ("\n".join(metadata_lines) + "\n\n").encode("utf-8")
        wheel = (
            "Wheel-Version: 1.0\n"
            "Generator: zyra-productization\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n\n"
        ).encode("utf-8")
        entries = [
            WheelEntry(f"{dist_info}/METADATA", metadata),
            WheelEntry(f"{dist_info}/WHEEL", wheel),
        ]
        scripts = project.get("scripts")
        if isinstance(scripts, Mapping) and scripts:
            lines = ["[console_scripts]"]
            for name in sorted(str(item) for item in scripts):
                value = str(scripts[name]).strip()
                if not value or "\n" in value or "\r" in value:
                    raise InventoryViolation(
                        "Python console script entry is invalid.",
                        code="wheel_console_script_invalid",
                        details={"name": name},
                    )
                lines.append(f"{name} = {value}")
            entries.append(
                WheelEntry(
                    f"{dist_info}/entry_points.txt",
                    ("\n".join(lines) + "\n").encode("utf-8"),
                )
            )
        direct_url = {
            "archive_info": {"hash": f"sha256={stable_digest(project)}"},
            "url": f"zyra-release://{distribution}/{version}",
        }
        entries.append(
            WheelEntry(
                f"{dist_info}/direct_url.json",
                (
                    json.dumps(
                        direct_url,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        )
        return tuple(entries)

    def _write(self, output: Path, entries: Iterable[WheelEntry]) -> None:
        if output.exists():
            output.unlink()
        timestamp = datetime.fromtimestamp(
            max(self.policy.source_date_epoch, 315532800),
            UTC,
        )
        date_time = (
            timestamp.year,
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
        )
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for entry in sorted(
                entries,
                key=lambda item: item.archive_path.encode("utf-8"),
            ):
                info = zipfile.ZipInfo(entry.archive_path, date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                mode = 0o755 if entry.executable else 0o644
                info.external_attr = (mode | 0o100000) << 16
                archive.writestr(info, entry.content, compress_type=zipfile.ZIP_DEFLATED)

    @staticmethod
    def _package_files(package_root: Path) -> tuple[Path, ...]:
        blocked_names = {"__pycache__", ".pytest_cache", ".mypy_cache"}
        files: list[Path] = []
        for path in package_root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(package_root)
            if any(part in blocked_names for part in relative.parts):
                continue
            if path.suffix.casefold() in {".pyc", ".pyo"}:
                continue
            files.append(path)
        return tuple(
            sorted(
                files,
                key=lambda item: item.relative_to(package_root).as_posix().encode(
                    "utf-8"
                ),
            )
        )

    @staticmethod
    def _matches(name: str, patterns: Sequence[str]) -> bool:
        for pattern in patterns:
            expression = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
            if re.fullmatch(expression, name):
                return True
        return False

    @staticmethod
    def _unique_entries(
        entries: Iterable[WheelEntry],
    ) -> tuple[WheelEntry, ...]:
        unique: dict[str, WheelEntry] = {}
        for entry in entries:
            path = normalize_relative_path(entry.archive_path)
            prior = unique.get(path)
            if prior is not None:
                if prior.content != entry.content:
                    raise InventoryViolation(
                        "Python package roots produce conflicting wheel paths.",
                        code="wheel_package_path_conflict",
                        details={"path": path},
                    )
                continue
            unique[path] = WheelEntry(path, entry.content, entry.executable)
        return tuple(
            unique[path]
            for path in sorted(unique, key=lambda item: item.encode("utf-8"))
        )

    @staticmethod
    def _record(entries: Iterable[WheelEntry], *, record_path: str) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        for entry in sorted(
            entries,
            key=lambda item: item.archive_path.encode("utf-8"),
        ):
            writer.writerow(
                [entry.archive_path, entry.record_digest, len(entry.content)]
            )
        writer.writerow([record_path, "", ""])
        return stream.getvalue().encode("utf-8")

    @staticmethod
    def _parse_record(value: str) -> tuple[tuple[str, str, str], ...]:
        rows: list[tuple[str, str, str]] = []
        for raw in csv.reader(io.StringIO(value)):
            if len(raw) != 3:
                raise IntegrityViolation(
                    "Python wheel RECORD row is malformed.",
                    code="wheel_record_row_invalid",
                    details={"row": raw},
                )
            rows.append((normalize_relative_path(raw[0]), raw[1], raw[2]))
        return tuple(rows)

    @staticmethod
    def _verify_record(
        archive: zipfile.ZipFile,
        *,
        names: Sequence[str],
        record_rows: Sequence[tuple[str, str, str]],
        record_path: str,
    ) -> None:
        indexed = {path: (digest, size) for path, digest, size in record_rows}
        if set(indexed) != set(names):
            raise IntegrityViolation(
                "Python wheel RECORD does not cover the exact archive.",
                code="wheel_record_coverage",
                details={
                    "missing": sorted(set(names) - set(indexed)),
                    "extra": sorted(set(indexed) - set(names)),
                },
            )
        for name in names:
            digest, size = indexed[name]
            content = archive.read(name)
            if name == record_path:
                if digest or size:
                    raise IntegrityViolation(
                        "Python wheel RECORD self-entry must be unhashed.",
                        code="wheel_record_self_hash",
                    )
                continue
            expected = WheelEntry(name, content).record_digest
            if digest != expected or size != str(len(content)):
                raise IntegrityViolation(
                    "Python wheel RECORD digest does not match.",
                    code="wheel_record_digest_mismatch",
                    details={
                        "path": name,
                        "expected": expected,
                        "actual": digest,
                        "expected_size": len(content),
                        "actual_size": size,
                    },
                )

    @staticmethod
    def _is_executable(path: Path) -> bool:
        return bool(path.stat().st_mode & 0o111)

    @staticmethod
    def _header_value(value: str) -> str:
        return " ".join(value.replace("\r", " ").replace("\n", " ").split())


__all__ = [
    "DeterministicWheelBuilder",
    "WHEEL_RECEIPT_SCHEMA",
    "WheelEntry",
]
