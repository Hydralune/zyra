from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..runtime.errors import LoopXRuntimeError
from ..runtime.manifest import LOOPX_VERSION, LoopXPackageLock
from ..runtime.resolver import LoopXRuntimeResolver
from .profiles import InstallProfile, ProfilePlan, ProfileResolver


class LoopXInstaller:
    """Retired installer facade that performs resolution only.

    No archive is opened, no package is staged, and no
    ``.zyra/loopx/install`` directory is created. Existing callers receive the
    embedded runtime receipt used by the bridge.
    """

    def __init__(self, package_root: Path) -> None:
        self.package_root = package_root.resolve()
        self.package_lock = LoopXPackageLock.load(self.package_root)
        self.resolver = LoopXRuntimeResolver(self.package_root)

    def plan(
        self,
        workspace_root: Path,
        *,
        profile: str | InstallProfile | None = None,
        python_executable: Path | None = None,
    ) -> ProfilePlan:
        return ProfileResolver(self.package_lock).resolve(
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
        del profile
        self.check_interpreter(python_executable or Path(sys.executable))
        receipt = self.resolver.receipt(workspace_root, verify=True)
        return {
            **receipt,
            "schema": "zyra.loopx-install-compatibility-receipt/v2",
            "compatibility_facade": True,
            "installer_retired": True,
            "idempotent": True,
            "file_count": len(self.package_lock.manifest_files),
            "installed_files_digest": self.package_lock.source_digest,
            "source_manifest_digest": str(
                self.package_lock.source_record["sha256"]
            ),
        }

    def check_interpreter(self, python_executable: Path) -> dict[str, Any]:
        python = python_executable.resolve()
        if python == Path(sys.executable).resolve():
            version = sys.version_info
        else:
            # The compatibility facade never executes a second interpreter.
            version = sys.version_info
        if version < (3, 11):
            raise LoopXRuntimeError(
                "LoopX v0.2.13 requires CPython 3.11 or newer.",
                code="loopx_python_incompatible",
                details={"python": str(python)},
            )
        return {
            "python": str(python),
            "version": f"{version.major}.{version.minor}.{version.micro}",
            "compatible": True,
        }

    def validate_installed(
        self,
        install_root: Path,
        *,
        expected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolution = self.resolver.resolve(verify=True)
        selected = install_root.resolve()
        if selected != resolution.runtime_root:
            raise LoopXRuntimeError(
                "Historical project-local LoopX installs are retired.",
                code="loopx_runtime_origin_mismatch",
                details={
                    "expected_embedded_root": str(resolution.runtime_root),
                    "selected": str(selected),
                    "receipt": dict(expected or {}),
                },
            )
        return {
            "schema": "zyra.loopx-embedded-runtime-integrity/v1",
            "ready": True,
            "version": LOOPX_VERSION,
            "install_root": str(resolution.runtime_root),
            "module_root": str(
                resolution.module_root.relative_to(resolution.runtime_root)
            ),
            "source_digest": resolution.source_digest,
            "file_count": len(self.package_lock.manifest_files),
            "archive_extraction": False,
        }


__all__ = ["LoopXInstaller"]
