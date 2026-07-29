from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import LoopXInstallError
from .manifest import (
    LOOPX_SOURCE_COMMIT,
    LOOPX_VERSION,
    LoopXPackageLock,
    sha256_file,
    stable_digest,
)
from .profiles import InstallProfile, ProfilePlan, ProfileResolver


INSTALL_RECEIPT_SCHEMA = "zyra.loopx-install-receipt/v1"
INSTALL_RECEIPT_FILENAME = "install-receipt.json"


def _safe_archive_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/").strip("/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
    ):
        raise LoopXInstallError(
            "LoopX artifact contains an unsafe path.",
            code="loopx_artifact_tampered",
            details={"path": value},
        )
    return pure


@dataclass(frozen=True, slots=True)
class InstallReceipt:
    value: Mapping[str, Any]

    @property
    def install_root(self) -> Path:
        return Path(str(self.value["install_root"]))

    @property
    def state_root(self) -> Path:
        return Path(str(self.value["state_root"]))

    @property
    def module_root(self) -> Path:
        return self.install_root / str(self.value["module_root"])

    @property
    def profile(self) -> InstallProfile:
        return InstallProfile(str(self.value["profile"]))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.value)


class LoopXInstaller:
    """Offline, project-local installer for the pinned LoopX package."""

    def __init__(self, package_root: Path) -> None:
        self.package_root = package_root.resolve()
        self.package_lock = LoopXPackageLock.load(self.package_root)
        self.profile_resolver = ProfileResolver(self.package_lock)

    def plan(
        self,
        workspace_root: Path,
        *,
        profile: str | InstallProfile | None = None,
        python_executable: Path | None = None,
    ) -> ProfilePlan:
        return self.profile_resolver.resolve(
            workspace_root,
            profile=profile,
            python_executable=python_executable,
        )

    def install(
        self,
        workspace_root: Path,
        *,
        profile: str | InstallProfile | None = None,
        python_executable: Path | None = None,
    ) -> dict[str, Any]:
        plan = self.plan(
            workspace_root,
            profile=profile,
            python_executable=python_executable,
        )
        interpreter = self.check_interpreter(plan.python_executable)
        artifact_verification = self.package_lock.verify_artifacts(
            deep=plan.profile is InstallProfile.LINUX_WSL
        )
        replay = self._preflight_existing(plan)
        if replay is not None:
            return {
                **replay.to_dict(),
                "idempotent_replay": True,
                "interpreter": interpreter,
                "artifact_verification": artifact_verification,
            }
        self._probe_writable(plan.workspace_root, plan.custody_root)
        plan.custody_root.mkdir(parents=True, exist_ok=True)
        plan.state_root.mkdir(parents=True, exist_ok=True)
        plan.runtime_root.mkdir(parents=True, exist_ok=True)
        stage = plan.install_root.parent / f".{plan.profile.value}.stage-{uuid.uuid4().hex}"
        if stage.exists():
            raise LoopXInstallError(
                "LoopX staging directory unexpectedly exists.",
                code="loopx_partial_install",
                details={"path": str(stage)},
            )
        stage.mkdir(parents=True, exist_ok=False)
        try:
            if plan.profile is InstallProfile.WINDOWS_RELEASE:
                module_relative = self._install_wheel(stage)
            else:
                module_relative = self._install_source_snapshot(stage)
            self._write_launchers(
                stage,
                plan=plan,
                module_relative=module_relative,
            )
            receipt = self._build_receipt(
                stage,
                plan=plan,
                module_relative=module_relative,
                interpreter=interpreter,
                artifact_verification=artifact_verification,
            )
            self._write_json(stage / INSTALL_RECEIPT_FILENAME, receipt)
            plan.install_root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, plan.install_root)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        installed = self.validate_installed(plan.install_root)
        return {
            **installed.to_dict(),
            "idempotent_replay": False,
            "interpreter": interpreter,
            "artifact_verification": artifact_verification,
        }

    def check_interpreter(self, python_executable: Path) -> dict[str, Any]:
        executable = python_executable.resolve()
        if not executable.is_file():
            raise LoopXInstallError(
                "Selected Python interpreter does not exist.",
                code="loopx_python_incompatible",
                details={"python": str(executable)},
            )
        probe = (
            "import importlib.util,json,sys;"
            "mods=json.loads(sys.argv[1]);"
            "print(json.dumps({'version':list(sys.version_info[:3]),"
            "'implementation':sys.implementation.name,"
            "'missing':[m for m in mods if importlib.util.find_spec(m) is None]}))"
        )
        raw_imports = self.package_lock.package.get(
            "runtime_dependency_imports",
            [],
        )
        imports = (
            [str(item) for item in raw_imports]
            if isinstance(raw_imports, Sequence)
            and not isinstance(raw_imports, (str, bytes))
            else []
        )
        try:
            result = subprocess.run(
                [str(executable), "-c", probe, json.dumps(imports)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            value = json.loads(result.stdout) if result.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            raise LoopXInstallError(
                "Selected Python interpreter could not be probed.",
                code="loopx_python_incompatible",
                details={"python": str(executable), "error": str(error)},
            ) from error
        version = value.get("version")
        if (
            not isinstance(version, Sequence)
            or isinstance(version, (str, bytes))
            or tuple(int(item) for item in version[:2]) < (3, 11)
        ):
            raise LoopXInstallError(
                "LoopX requires Python 3.11 or newer.",
                code="loopx_python_incompatible",
                details={"python": str(executable), "version": version},
            )
        missing = value.get("missing")
        if isinstance(missing, Sequence) and not isinstance(missing, (str, bytes)) and missing:
            raise LoopXInstallError(
                "A locked LoopX runtime dependency is missing.",
                code="loopx_dependency_missing",
                details={"python": str(executable), "missing": list(missing)},
            )
        return {
            "python": str(executable),
            "version": ".".join(str(item) for item in version),
            "implementation": str(value.get("implementation") or ""),
            "runtime_dependencies": imports,
            "missing_dependencies": [],
        }

    def validate_installed(self, install_root: Path) -> InstallReceipt:
        root = install_root.resolve()
        receipt_path = root / INSTALL_RECEIPT_FILENAME
        if not root.is_dir() or not receipt_path.is_file():
            raise LoopXInstallError(
                "LoopX installation is incomplete.",
                code="loopx_partial_install",
                details={"install_root": str(root), "receipt": str(receipt_path)},
            )
        try:
            value = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LoopXInstallError(
                "LoopX install receipt is unreadable.",
                code="loopx_partial_install",
                details={"path": str(receipt_path), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise LoopXInstallError(
                "LoopX install receipt root is invalid.",
                code="loopx_partial_install",
                details={"path": str(receipt_path)},
            )
        declared_digest = str(value.get("receipt_digest") or "")
        unsigned = dict(value)
        unsigned.pop("receipt_digest", None)
        if (
            value.get("schema") != INSTALL_RECEIPT_SCHEMA
            or declared_digest != stable_digest(unsigned)
        ):
            raise LoopXInstallError(
                "LoopX install receipt identity is invalid.",
                code="loopx_partial_install",
                details={"path": str(receipt_path)},
            )
        identity = {
            "version": value.get("version"),
            "source_commit": value.get("source_commit"),
            "source_digest": value.get("source_digest"),
            "package_lock_digest": value.get("package_lock_digest"),
        }
        expected_identity = {
            "version": LOOPX_VERSION,
            "source_commit": LOOPX_SOURCE_COMMIT,
            "source_digest": self.package_lock.source_digest,
            "package_lock_digest": self.package_lock.lock_digest,
        }
        if identity != expected_identity:
            raise LoopXInstallError(
                "Existing LoopX installation belongs to another pinned package.",
                code="loopx_upgrade_required",
                details={"installed": identity, "expected": expected_identity},
            )
        if Path(str(value.get("install_root") or "")).resolve() != root:
            raise LoopXInstallError(
                "LoopX install receipt points at another root.",
                code="loopx_partial_install",
                details={
                    "expected": str(root),
                    "actual": str(value.get("install_root") or ""),
                },
            )
        records = value.get("installed_files")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise LoopXInstallError(
                "LoopX installed file manifest is missing.",
                code="loopx_partial_install",
            )
        expected: dict[str, tuple[str, int]] = {}
        for item in records:
            if not isinstance(item, Mapping):
                raise LoopXInstallError(
                    "LoopX installed file manifest contains an invalid record.",
                    code="loopx_partial_install",
                )
            relative = _safe_archive_path(str(item.get("path") or "")).as_posix()
            expected[relative] = (
                str(item.get("sha256") or ""),
                int(item.get("size", -1)),
            )
        actual_paths = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path != receipt_path
        }
        if actual_paths != set(expected):
            raise LoopXInstallError(
                "LoopX installed file set has drifted.",
                code="loopx_installed_file_tampered",
                details={
                    "missing": sorted(set(expected) - actual_paths)[:25],
                    "extra": sorted(actual_paths - set(expected))[:25],
                },
            )
        changed: list[str] = []
        for relative, (expected_digest, expected_size) in expected.items():
            path = root.joinpath(*PurePosixPath(relative).parts)
            if (
                path.stat().st_size != expected_size
                or sha256_file(path) != expected_digest
            ):
                changed.append(relative)
        if changed:
            raise LoopXInstallError(
                "LoopX installed package file failed integrity verification.",
                code="loopx_installed_file_tampered",
                details={"changed": changed[:25]},
            )
        return InstallReceipt(dict(value))

    def _preflight_existing(self, plan: ProfilePlan) -> InstallReceipt | None:
        if not plan.install_root.exists():
            return None
        if not plan.install_root.is_dir():
            raise LoopXInstallError(
                "LoopX install target exists but is not a directory.",
                code="loopx_partial_install",
                details={"path": str(plan.install_root)},
            )
        return self.validate_installed(plan.install_root)

    @staticmethod
    def _probe_writable(workspace_root: Path, custody_root: Path) -> None:
        try:
            workspace_root.mkdir(parents=True, exist_ok=True)
            custody_root.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".loopx-write-probe-",
                dir=custody_root.parent,
                delete=True,
            ) as stream:
                stream.write(b"loopx")
                stream.flush()
        except OSError as error:
            raise LoopXInstallError(
                "LoopX project workspace is not writable.",
                code="loopx_workspace_unwritable",
                details={
                    "workspace_root": str(workspace_root),
                    "probe_root": str(custody_root.parent),
                    "error": str(error),
                },
            ) from error

    def _install_wheel(self, stage: Path) -> str:
        wheel = self.package_lock.resolve_artifact(
            self.package_lock.artifacts["wheel"]
        )
        destination = stage / "site-packages"
        destination.mkdir(parents=True, exist_ok=False)
        try:
            with zipfile.ZipFile(wheel) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    relative = _safe_archive_path(info.filename)
                    target = destination.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
                    target.chmod(0o644)
        except (OSError, zipfile.BadZipFile) as error:
            raise LoopXInstallError(
                "LoopX wheel could not be installed.",
                code="loopx_artifact_tampered",
                details={"path": str(wheel), "error": str(error)},
            ) from error
        return "site-packages"

    def _install_source_snapshot(self, stage: Path) -> str:
        bundle = self.package_lock.resolve_artifact(
            self.package_lock.artifacts["source_bundle"]
        )
        prefix = str(self.package_lock.source_manifest["archive_prefix"])
        prefix_path = _safe_archive_path(prefix)
        destination = stage / "release"
        destination.mkdir(parents=True, exist_ok=False)
        expected_modes = {
            str(item["path"]): bool(item.get("executable", False))
            for item in self.package_lock.manifest_files
        }
        try:
            with tarfile.open(bundle, mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.issym() or member.islnk():
                        raise LoopXInstallError(
                            "LoopX source bundle contains a link.",
                            code="loopx_artifact_tampered",
                            details={"path": member.name},
                        )
                    if not member.isfile():
                        continue
                    member_path = _safe_archive_path(member.name)
                    if member_path.parts[: len(prefix_path.parts)] != prefix_path.parts:
                        raise LoopXInstallError(
                            "LoopX source bundle member escapes its prefix.",
                            code="loopx_artifact_tampered",
                            details={"path": member.name},
                        )
                    relative = PurePosixPath(
                        *member_path.parts[len(prefix_path.parts) :]
                    )
                    if not relative.parts:
                        continue
                    relative_text = relative.as_posix()
                    if relative_text not in expected_modes:
                        raise LoopXInstallError(
                            "LoopX source bundle contains an unmanifested file.",
                            code="loopx_source_manifest_mismatch",
                            details={"path": relative_text},
                        )
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise OSError(f"cannot read {member.name}")
                    target = destination.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(stream.read())
                    target.chmod(0o755 if expected_modes[relative_text] else 0o644)
        except (OSError, tarfile.TarError) as error:
            raise LoopXInstallError(
                "LoopX source bundle could not be installed.",
                code="loopx_artifact_tampered",
                details={"path": str(bundle), "error": str(error)},
            ) from error
        return "release"

    @staticmethod
    def _write_launchers(
        stage: Path,
        *,
        plan: ProfilePlan,
        module_relative: str,
    ) -> None:
        bin_root = stage / "bin"
        bin_root.mkdir(parents=True, exist_ok=True)
        module_root = plan.install_root / module_relative
        python = plan.python_executable
        posix = bin_root / "loopx"
        posix.write_text(
            "#!/usr/bin/env sh\n"
            f'PYTHONPATH="{module_root}${{PYTHONPATH:+:${{PYTHONPATH}}}}" '
            f'exec "{python}" -m loopx.cli "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        posix.chmod(0o755)
        command = bin_root / "loopx.cmd"
        command.write_text(
            "@echo off\r\n"
            f'set "PYTHONPATH={module_root};%PYTHONPATH%"\r\n'
            f'"{python}" -m loopx.cli %*\r\n',
            encoding="utf-8",
            newline="",
        )
        command.chmod(0o644)

    def _build_receipt(
        self,
        stage: Path,
        *,
        plan: ProfilePlan,
        module_relative: str,
        interpreter: Mapping[str, Any],
        artifact_verification: Mapping[str, Any],
    ) -> dict[str, Any]:
        installed_files = [
            {
                "path": path.relative_to(stage).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & stat.S_IXUSR),
            }
            for path in sorted(
                (item for item in stage.rglob("*") if item.is_file()),
                key=lambda item: item.relative_to(stage).as_posix().encode("utf-8"),
            )
        ]
        artifact = self.package_lock.artifacts[plan.artifact_name]
        receipt: dict[str, Any] = {
            "schema": INSTALL_RECEIPT_SCHEMA,
            "ready": True,
            "profile": plan.profile.value,
            "version": LOOPX_VERSION,
            "source_commit": LOOPX_SOURCE_COMMIT,
            "source_digest": self.package_lock.source_digest,
            "package_lock_digest": self.package_lock.lock_digest,
            "artifact": {
                "name": plan.artifact_name,
                "sha256": artifact.sha256,
                "size": artifact.size,
            },
            "install_root": str(plan.install_root),
            "workspace_root": str(plan.workspace_root),
            "state_root": str(plan.state_root),
            "runtime_root": str(plan.runtime_root),
            "module_root": module_relative,
            "bin_root": str(plan.install_root / "bin"),
            "python": dict(interpreter),
            "installed_at": datetime.now(UTC).isoformat(),
            "installed_files": installed_files,
            "installed_file_digest": stable_digest(installed_files),
            "file_count": len(installed_files),
            "network_access": False,
            "user_home_write": False,
            "system_path_write": False,
            "disabled_user_surfaces": list(plan.disabled_user_surfaces),
            "profile_environment": dict(plan.environment),
            "artifact_verification_digest": stable_digest(artifact_verification),
        }
        receipt["receipt_digest"] = stable_digest(receipt)
        return receipt

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )


__all__ = [
    "INSTALL_RECEIPT_FILENAME",
    "INSTALL_RECEIPT_SCHEMA",
    "InstallReceipt",
    "LoopXInstaller",
]
