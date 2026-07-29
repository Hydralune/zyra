from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .errors import LoopXInstallError
from .installer import InstallReceipt, LoopXInstaller
from .manifest import (
    LOOPX_SOURCE_COMMIT,
    LOOPX_VERSION,
    LoopXPackageLock,
    stable_digest,
)
from .profiles import InstallProfile, ProfileResolver


class LoopXDoctor:
    """Verify packaged and installed LoopX identity beyond import-only health."""

    def __init__(self, package_root: Path) -> None:
        self.package_root = package_root.resolve()

    def run(
        self,
        *,
        deep: bool = False,
        workspace_root: Path | None = None,
        install_root: Path | None = None,
        profile: str | InstallProfile | None = None,
        python_executable: Path | None = None,
    ) -> dict[str, Any]:
        lock_box: dict[str, LoopXPackageLock] = {}

        def load_lock() -> Mapping[str, Any]:
            lock = LoopXPackageLock.load(self.package_root)
            lock_box["value"] = lock
            return {
                "version": str(lock.package["version"]),
                "source_commit": str(lock.package["source_commit"]),
                "source_digest": lock.source_digest,
                "lock_digest": lock.lock_digest,
                "source_file_count": len(lock.manifest_files),
            }

        checks = [self._check("package-identity", load_lock)]
        lock = lock_box.get("value")
        if lock is not None:
            checks.append(
                self._check(
                    "offline-artifacts",
                    lambda: lock.verify_artifacts(deep=deep),
                )
            )
            checks.append(
                self._check(
                    "profile-boundary",
                    lambda: self._profile_boundary(lock),
                )
            )
            checks.append(
                self._check(
                    "release-independence",
                    lambda: self._release_independence(lock),
                )
            )
            if deep:
                checks.append(
                    self._check(
                        "import-and-cli-entry",
                        lambda: self._deep_canary(
                            lock,
                            workspace_root=workspace_root,
                            install_root=install_root,
                            profile=profile,
                            python_executable=python_executable,
                        ),
                    )
                )
            elif install_root is not None:
                checks.append(
                    self._check(
                        "installed-integrity",
                        lambda: self._installed_integrity(
                            install_root,
                            python_executable=python_executable,
                        ),
                    )
                )
        blockers = [
            {
                "check": str(item["check"]),
                "code": str(item.get("code") or ""),
                "details": item.get("details", {}),
                "recovery": str(item.get("recovery") or ""),
            }
            for item in checks
            if item.get("ready") is not True
        ]
        report: dict[str, Any] = {
            "schema": "zyra.loopx-doctor/v1",
            "ready": not blockers,
            "mode": "deep" if deep else "package",
            "package_root": str(self.package_root),
            "version": LOOPX_VERSION,
            "source_commit": LOOPX_SOURCE_COMMIT,
            "source_digest": lock.source_digest if lock is not None else "",
            "checks": checks,
            "blockers": blockers,
            "blocker_count": len(blockers),
        }
        report["digest"] = stable_digest(report)
        return report

    def enforce(self, **options: Any) -> dict[str, Any]:
        report = self.run(**options)
        if not report["ready"]:
            first = report["blockers"][0]
            raise LoopXInstallError(
                "LoopX doctor found a release or installation blocker.",
                code=str(first.get("code") or "loopx_artifact_tampered"),
                details={
                    "blocker_count": report["blocker_count"],
                    "blockers": report["blockers"],
                },
            )
        return report

    def _profile_boundary(self, lock: LoopXPackageLock) -> dict[str, Any]:
        resolver = ProfileResolver(lock)
        sentinel = (self.package_root / ".loopx-profile-contract").resolve()
        plans = {
            profile.value: resolver.resolve(
                sentinel,
                profile=profile,
            ).to_dict()
            for profile in InstallProfile
        }
        source_digests = {
            str(item["source_digest"]) for item in plans.values()
        }
        if source_digests != {lock.source_digest}:
            raise LoopXInstallError(
                "LoopX profiles do not report one pinned source digest.",
                code="loopx_package_lock_invalid",
                details={"source_digests": sorted(source_digests)},
            )
        for name, plan in plans.items():
            if (
                plan["network_access"] is not False
                or plan["user_home_write"] is not False
                or plan["system_path_write"] is not False
            ):
                raise LoopXInstallError(
                    "LoopX profile permits a forbidden user or network write.",
                    code="loopx_user_write_forbidden",
                    details={"profile": name, "plan": plan},
                )
            if {
                "user_shell_profile",
                "user_codex_skill",
                "user_claude_adapter",
                "global_slash_command",
                "system_path",
            } - set(plan["disabled_user_surfaces"]):
                raise LoopXInstallError(
                    "LoopX profile does not disable every user-level surface.",
                    code="loopx_user_write_forbidden",
                    details={"profile": name},
                )
        return {
            "source_digest": lock.source_digest,
            "profiles": plans,
            "same_source_digest": True,
            "user_level_writes_disabled": True,
        }

    def _release_independence(self, lock: LoopXPackageLock) -> dict[str, Any]:
        serialized = json.dumps(lock.value, ensure_ascii=False).casefold()
        forbidden = (
            "../long-horizon-systems",
            "..\\long-horizon-systems",
            "g:\\agent-zoo\\long-horizon-systems",
            "g:/agent-zoo/long-horizon-systems",
        )
        matched = [item for item in forbidden if item in serialized]
        if matched:
            raise LoopXInstallError(
                "LoopX package lock depends on the workspace source repository.",
                code="loopx_package_lock_invalid",
                details={"matched": matched},
            )
        for artifact in lock.artifacts.values():
            resolved = lock.resolve_artifact(artifact)
            try:
                resolved.relative_to(self.package_root)
            except ValueError as error:
                raise LoopXInstallError(
                    "LoopX artifact is outside the Zyra release.",
                    code="loopx_package_lock_invalid",
                    details={"artifact": artifact.name, "path": str(resolved)},
                ) from error
        return {
            "ready": True,
            "network_required": False,
            "external_source_required": False,
            "root_source_reference_count": 0,
            "package_root": str(self.package_root),
        }

    def _deep_canary(
        self,
        lock: LoopXPackageLock,
        *,
        workspace_root: Path | None,
        install_root: Path | None,
        profile: str | InstallProfile | None,
        python_executable: Path | None,
    ) -> dict[str, Any]:
        installer = LoopXInstaller(self.package_root)
        if install_root is not None:
            receipt = installer.validate_installed(install_root)
            return self._probe_import_cli(
                receipt,
                python_executable=python_executable,
            )
        if workspace_root is not None:
            install_value = installer.install(
                workspace_root,
                profile=profile,
                python_executable=python_executable,
            )
            receipt = InstallReceipt(install_value)
            return self._probe_import_cli(
                receipt,
                python_executable=python_executable,
            )
        with tempfile.TemporaryDirectory(prefix="zyra-loopx-doctor-") as raw:
            canary_workspace = Path(raw) / "workspace"
            install_value = installer.install(
                canary_workspace,
                profile=profile,
                python_executable=python_executable,
            )
            receipt = InstallReceipt(install_value)
            result = self._probe_import_cli(
                receipt,
                python_executable=python_executable,
            )
            result["temporary_canary"] = True
            result["package_lock_digest"] = lock.lock_digest
            return result

    def _installed_integrity(
        self,
        install_root: Path,
        *,
        python_executable: Path | None,
    ) -> dict[str, Any]:
        receipt = LoopXInstaller(self.package_root).validate_installed(
            install_root
        )
        return self._probe_import_cli(
            receipt,
            python_executable=python_executable,
        )

    @staticmethod
    def _probe_import_cli(
        receipt: InstallReceipt,
        *,
        python_executable: Path | None,
    ) -> dict[str, Any]:
        python = (
            python_executable.resolve()
            if python_executable is not None
            else Path(str(receipt.value["python"]["python"])).resolve()
        )
        module_root = receipt.module_root.resolve()
        state_root = receipt.state_root.resolve()
        workspace = Path(str(receipt.value["workspace_root"])).resolve()
        try:
            state_root.relative_to(workspace)
            receipt.install_root.resolve().relative_to(workspace)
        except ValueError as error:
            raise LoopXInstallError(
                "Installed LoopX state or package root escapes the workspace.",
                code="loopx_user_write_forbidden",
                details={
                    "workspace": str(workspace),
                    "install_root": str(receipt.install_root),
                    "state_root": str(state_root),
                },
            ) from error
        state_root.mkdir(parents=True, exist_ok=True)
        forbidden_home = receipt.install_root / ".doctor-forbidden-home"
        forbidden_home.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        prior_pythonpath = environment.get("PYTHONPATH", "")
        environment.update(
            {
                "PYTHONPATH": (
                    str(module_root)
                    + (os.pathsep + prior_pythonpath if prior_pythonpath else "")
                ),
                "LOOPX_RUNTIME_ROOT": str(state_root),
                "HOME": str(forbidden_home),
                "USERPROFILE": str(forbidden_home),
                "XDG_CONFIG_HOME": str(forbidden_home / "config"),
                "XDG_DATA_HOME": str(forbidden_home / "data"),
            }
        )
        import_code = (
            "import json,loopx;"
            "print(json.dumps({'version':loopx.__version__,"
            "'path':loopx.__file__}))"
        )
        import_result = LoopXDoctor._run(
            [str(python), "-c", import_code],
            environment=environment,
        )
        if import_result["returncode"] != 0:
            raise LoopXInstallError(
                "Installed LoopX package cannot be imported.",
                code="loopx_import_failed",
                details=import_result,
            )
        try:
            imported = json.loads(str(import_result["stdout"]))
        except json.JSONDecodeError as error:
            raise LoopXInstallError(
                "Installed LoopX import probe returned invalid output.",
                code="loopx_import_failed",
                details=import_result,
            ) from error
        if (
            imported.get("version") != LOOPX_VERSION
            or not Path(str(imported.get("path") or "")).resolve().is_relative_to(
                module_root
            )
        ):
            raise LoopXInstallError(
                "Imported LoopX does not match the project-local pinned package.",
                code="loopx_import_failed",
                details={"imported": imported, "module_root": str(module_root)},
            )
        cli_result = LoopXDoctor._run(
            [str(python), "-m", "loopx.cli", "--version"],
            environment=environment,
        )
        if (
            cli_result["returncode"] != 0
            or f"loopx {LOOPX_VERSION}" not in str(cli_result["stdout"])
        ):
            raise LoopXInstallError(
                "Installed LoopX CLI entry failed.",
                code="loopx_cli_entry_failed",
                details=cli_result,
            )
        user_writes = [
            path.relative_to(forbidden_home).as_posix()
            for path in forbidden_home.rglob("*")
            if path.is_file()
        ]
        if user_writes:
            raise LoopXInstallError(
                "LoopX import or CLI probe wrote user-level state.",
                code="loopx_user_write_forbidden",
                details={"paths": user_writes},
            )
        return {
            "ready": True,
            "version": imported["version"],
            "source_commit": str(receipt.value["source_commit"]),
            "source_digest": str(receipt.value["source_digest"]),
            "profile": str(receipt.value["profile"]),
            "install_root": str(receipt.install_root),
            "module_root": str(module_root),
            "state_root": str(state_root),
            "state_workspace_local": True,
            "import": import_result,
            "cli": cli_result,
            "cli_entry": "loopx = loopx.cli:main",
            "user_level_write_count": 0,
            "network_access": False,
        }

    @staticmethod
    def _run(
        command: list[str],
        *,
        environment: Mapping[str, str],
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
                env=dict(environment),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "command": command,
                "returncode": -1,
                "stdout": "",
                "stderr": str(error),
            }
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }

    @staticmethod
    def _check(
        name: str,
        operation: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        try:
            details = dict(operation())
            return {
                "check": name,
                "ready": True,
                "details": details,
            }
        except LoopXInstallError as error:
            return {
                "check": name,
                "ready": False,
                "code": error.code,
                "recovery": error.recovery,
                "details": error.to_dict(),
            }
        except BaseException as error:
            return {
                "check": name,
                "ready": False,
                "code": type(error).__name__,
                "recovery": "Inspect the doctor receipt and restore the pinned release.",
                "details": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }


__all__ = ["LoopXDoctor"]
