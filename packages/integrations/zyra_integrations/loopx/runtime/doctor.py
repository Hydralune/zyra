from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import LoopXRuntimeError
from .interpreter import probe_python_interpreter
from .manifest import (
    LOOPX_SOURCE_COMMIT,
    LOOPX_SOURCE_TREE_COMMIT,
    LOOPX_VERSION,
    LoopXPackageLock,
)
from .resolver import LoopXRuntimeResolver


class LoopXDoctor:
    """Validate the embedded source, direct import, resources and state boundary."""

    def __init__(self, package_root: Path) -> None:
        self.package_root = package_root.resolve()

    def run(
        self,
        *,
        deep: bool = False,
        workspace_root: Path | None = None,
        install_root: Path | None = None,
        profile: str | None = None,
        python_executable: Path | None = None,
    ) -> dict[str, Any]:
        del install_root, profile
        checks: list[dict[str, Any]] = []
        lock: LoopXPackageLock | None = None

        def load_lock() -> Mapping[str, Any]:
            nonlocal lock
            lock = LoopXPackageLock.load(self.package_root)
            return {
                "version": str(lock.package["version"]),
                "source_ref": str(lock.package["source_ref"]),
                "source_commit": str(lock.package["source_commit"]),
                "source_tree_commit": str(lock.package["source_tree_commit"]),
                "source_digest": lock.source_digest,
                "package_lock_digest": lock.lock_digest,
                "migration_mode": str(lock.package["migration_mode"]),
            }

        checks.append(self._check("package-identity", load_lock))
        if lock is not None:
            checks.append(
                self._check(
                    "embedded-source-integrity",
                    lambda: lock.verify_embedded_source(deep=deep),
                )
            )
            checks.append(
                self._check(
                    "embedded-runtime-layout",
                    lambda: self._layout(lock),
                )
            )
            checks.append(
                self._check(
                    "archive-entry-retired",
                    lambda: self._archive_retirement(lock),
                )
            )
            if deep:
                python = (python_executable or Path(sys.executable)).resolve()
                checks.append(
                    self._check(
                        "import-and-cli-entry",
                        lambda: self._import_and_cli(lock, python),
                    )
                )
                checks.append(
                    self._check(
                        "extension-skill-resource-discovery",
                        lambda: self._resources(lock),
                    )
                )
        if workspace_root is not None:
            checks.append(
                self._check(
                    "workspace-private-state",
                    lambda: self._workspace_state(workspace_root),
                )
            )
        blockers = [
            {
                "check": item["check"],
                "code": item.get("code", ""),
                "details": item.get("details", {}),
            }
            for item in checks
            if item.get("ready") is not True
        ]
        return {
            "schema": "zyra.loopx-embedded-doctor/v1",
            "mode": "deep" if deep else "standard",
            "ready": not blockers,
            "version": LOOPX_VERSION,
            "source_commit": LOOPX_SOURCE_COMMIT,
            "source_tree_commit": LOOPX_SOURCE_TREE_COMMIT,
            "runtime_source": "pinned_embedded_source",
            "archive_fallback": False,
            "user_level_fallback": False,
            "root_source_fallback": False,
            "checks": checks,
            "blockers": blockers,
        }

    def enforce(self, **kwargs: Any) -> dict[str, Any]:
        report = self.run(**kwargs)
        if report["ready"] is not True:
            raise LoopXRuntimeError(
                "Embedded LoopX doctor found blocking failures.",
                code="loopx_runtime_unavailable",
                details={"blockers": report["blockers"]},
            )
        return report

    def _layout(self, lock: LoopXPackageLock) -> Mapping[str, Any]:
        resolution = LoopXRuntimeResolver(self.package_root).resolve(verify=False)
        embedded = resolution.runtime_root
        pyproject_path = embedded / "pyproject.toml"
        init_path = resolution.package_init
        if not init_path.is_file():
            raise LoopXRuntimeError(
                "Embedded LoopX source layout is incomplete.",
                code="loopx_embedded_source_missing",
                details={
                    "pyproject": str(pyproject_path),
                    "package_init": str(init_path),
                },
            )
        init_text = init_path.read_text(encoding="utf-8")
        if f'__version__ = "{LOOPX_VERSION}"' not in init_text:
            raise LoopXRuntimeError(
                "Embedded LoopX runtime version declaration is inconsistent.",
                code="loopx_version_mismatch",
                details={"path": str(init_path)},
            )
        expected_scripts = dict(lock.source_manifest.get("entry_points") or {})
        if resolution.mode == "embedded_source":
            if not pyproject_path.is_file():
                raise LoopXRuntimeError(
                    "Embedded LoopX pyproject is missing.",
                    code="loopx_embedded_source_missing",
                    details={"path": str(pyproject_path)},
                )
            with pyproject_path.open("rb") as stream:
                pyproject = tomllib.load(stream)
            project = pyproject.get("project")
            if (
                not isinstance(project, Mapping)
                or project.get("version") != LOOPX_VERSION
            ):
                raise LoopXRuntimeError(
                    "Embedded LoopX pyproject version is not the pinned version.",
                    code="loopx_version_mismatch",
                    details={"path": str(pyproject_path)},
                )
            scripts = project.get("scripts")
            if (
                not isinstance(scripts, Mapping)
                or dict(scripts) != expected_scripts
            ):
                raise LoopXRuntimeError(
                    "Embedded LoopX entry points do not match the source manifest.",
                    code="loopx_source_manifest_mismatch",
                )
        return {
            "root": str(embedded),
            "package_init": str(init_path),
            "pyproject": (
                str(pyproject_path)
                if resolution.mode == "embedded_source"
                else "package-lock-bound"
            ),
            "version": LOOPX_VERSION,
            "entry_points": expected_scripts,
            "mode": resolution.mode,
        }

    def _archive_retirement(self, lock: LoopXPackageLock) -> Mapping[str, Any]:
        legacy_assets = (
            self.package_root
            / "packages"
            / "integrations"
            / "zyra_integrations"
            / "loopx"
            / "install"
            / "assets"
        )
        archives = []
        if legacy_assets.is_dir():
            archives = [
                path.name
                for path in legacy_assets.iterdir()
                if path.is_file()
                and (
                    path.suffix.casefold() == ".whl"
                    or path.name.casefold().endswith(".tar.gz")
                )
            ]
        if archives or lock.artifacts:
            raise LoopXRuntimeError(
                "Retired LoopX archive artifacts remain reachable.",
                code="loopx_package_lock_invalid",
                details={"archives": sorted(archives)},
            )
        return {
            "ready": True,
            "archive_artifact_count": 0,
            "archive_extraction": False,
            "archive_fallback": False,
            "legacy_assets_path": str(legacy_assets),
        }

    def _import_and_cli(
        self,
        lock: LoopXPackageLock,
        python: Path,
    ) -> Mapping[str, Any]:
        interpreter = probe_python_interpreter(
            python,
            minimum=(3, 12),
            requirement="Zyra",
        )
        resolution = LoopXRuntimeResolver(self.package_root).resolve(verify=False)
        root = resolution.runtime_root
        probe = (
            "import json,loopx;"
            "from importlib import resources;"
            "from loopx.extensions import bundled;"
            "skill=resources.files('loopx.capabilities.auto_research')"
            ".joinpath('worker_skill/SKILL.md');"
            "print(json.dumps({"
            "'version':loopx.__version__,"
            "'origin':loopx.__file__,"
            "'skill':str(skill),"
            "'skill_exists':skill.is_file(),"
            "'bundled_origin':bundled.__file__"
            "},sort_keys=True))"
        )
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.pop("PYTHONPATH", None)
        imported = self._run(
            [str(python), "-c", probe],
            cwd=root,
            environment=environment,
        )
        if imported["returncode"] != 0:
            raise LoopXRuntimeError(
                "Embedded LoopX import probe failed.",
                code="loopx_runtime_unavailable",
                details=imported,
            )
        try:
            payload = json.loads(str(imported["stdout"]).splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as error:
            raise LoopXRuntimeError(
                "Embedded LoopX import probe returned invalid output.",
                code="loopx_runtime_unavailable",
                details=imported,
            ) from error
        origin = Path(str(payload.get("origin") or "")).resolve()
        bundled_origin = Path(str(payload.get("bundled_origin") or "")).resolve()
        if (
            payload.get("version") != LOOPX_VERSION
            or not origin.is_relative_to(root)
            or not bundled_origin.is_relative_to(root)
            or payload.get("skill_exists") is not True
        ):
            raise LoopXRuntimeError(
                "LoopX import or resource resolved outside the embedded runtime.",
                code="loopx_runtime_origin_mismatch",
                details={"probe": payload, "root": str(root)},
            )
        cli = self._run(
            [str(python), "-m", "loopx.cli", "--version"],
            cwd=root,
            environment=environment,
        )
        if cli["returncode"] != 0 or cli["stdout"].strip() != f"loopx {LOOPX_VERSION}":
            raise LoopXRuntimeError(
                "Embedded LoopX CLI version probe failed.",
                code="loopx_cli_entry_failed",
                details=cli,
            )
        return {
            "version": payload["version"],
            "module_origin": str(origin),
            "extension_origin": str(bundled_origin),
            "package_resource": str(payload["skill"]),
            "cli": cli["stdout"].strip(),
            "entry_points": dict(lock.source_manifest.get("entry_points") or {}),
            "python": str(python),
            "python_version": interpreter["version"],
            "python_implementation": interpreter["implementation"],
            "probed_selected_executable": True,
            "cwd_import": True,
            "pythonpath_override": False,
        }

    def _resources(self, lock: LoopXPackageLock) -> Mapping[str, Any]:
        resolution = LoopXRuntimeResolver(self.package_root).resolve(verify=False)
        root = resolution.runtime_root
        manifest = lock.source_manifest
        categories = {
            "extensions": tuple(manifest.get("extensions") or ()),
            "skills": tuple(manifest.get("skills") or ()),
            "templates": tuple(manifest.get("templates") or ()),
            "package_data": tuple(manifest.get("package_data") or ()),
            "upstream_tests": tuple(manifest.get("upstream_tests") or ()),
        }
        missing: list[str] = []
        selected_categories = categories
        if resolution.mode == "installed_distribution":
            selected_categories = {
                "extensions": tuple(
                    path
                    for path in categories["extensions"]
                    if str(path).startswith("loopx/")
                ),
                "package_data": categories["package_data"],
            }
        for paths in selected_categories.values():
            for relative in paths:
                if not root.joinpath(*Path(str(relative)).parts).is_file():
                    missing.append(str(relative))
        installed_skill_count = 0
        installed_template_count = 0
        installed_extension_count = 0
        if resolution.mode == "installed_distribution":
            share_root = Path(sys.prefix) / "share" / "zyra" / "loopx"
            installed_skills = share_root / "skills"
            external_skills = tuple(
                str(path)
                for path in categories["skills"]
                if not str(path).startswith("loopx/")
            )
            for relative in external_skills:
                installed = share_root.joinpath(*Path(relative).parts)
                if installed.is_file():
                    installed_skill_count += 1
                else:
                    missing.append(str(installed))
            for relative in categories["templates"]:
                installed = share_root / "templates" / Path(str(relative)).name
                if installed.is_file():
                    installed_template_count += 1
                else:
                    missing.append(str(installed))
            for relative in categories["extensions"]:
                normalized = str(relative).replace("\\", "/")
                if not normalized.startswith("packages/"):
                    continue
                extension_id = PurePosixPath(normalized).parts[1]
                installed = (
                    share_root
                    / "extensions"
                    / extension_id
                    / "extension.toml"
                )
                if installed.is_file():
                    installed_extension_count += 1
                else:
                    missing.append(str(installed))
        if missing or not categories["extensions"] or not categories["skills"]:
            raise LoopXRuntimeError(
                "Embedded LoopX runtime resources are incomplete.",
                code="loopx_resource_missing",
                details={"missing": missing[:50], "counts": {k: len(v) for k, v in categories.items()}},
            )
        return {
            "root": str(root),
            "counts": {key: len(value) for key, value in categories.items()},
            "installed_skill_count": installed_skill_count,
            "installed_template_count": installed_template_count,
            "installed_external_extension_count": installed_extension_count,
            "mode": resolution.mode,
            "extensions": list(categories["extensions"]),
            "skills": list(categories["skills"]),
            "templates": list(categories["templates"]),
        }

    @staticmethod
    def _workspace_state(workspace_root: Path) -> Mapping[str, Any]:
        workspace = workspace_root.resolve()
        state_root = (workspace / ".zyra" / "loopx" / "state").resolve()
        retired = (workspace / ".zyra" / "loopx" / "install").resolve()
        if not state_root.is_relative_to(workspace):
            raise LoopXRuntimeError(
                "LoopX private state root escapes the workspace.",
                code="loopx_runtime_unavailable",
                details={"workspace": str(workspace), "state_root": str(state_root)},
            )
        return {
            "workspace": str(workspace),
            "state_root": str(state_root),
            "state_owner": "loopx_private_control",
            "retired_install_root": str(retired),
            "retired_install_root_exists": retired.exists(),
            "retired_install_root_used": False,
            "retired_path_diagnostic": (
                "historical_install_path_ignored" if retired.exists() else ""
            ),
            "user_home_write": False,
        }

    @staticmethod
    def _run(
        command: list[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
                cwd=cwd,
                env=dict(environment),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "command": command,
                "cwd": str(cwd),
                "returncode": -1,
                "stdout": "",
                "stderr": str(error),
            }
        return {
            "command": command,
            "cwd": str(cwd),
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
            return {"check": name, "ready": True, "details": dict(operation())}
        except LoopXRuntimeError as error:
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
                "recovery": "Restore the matching checksum-verified Zyra release.",
                "details": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }


__all__ = ["LoopXDoctor"]
