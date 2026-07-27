from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .bundle import (
    BenchmarkEvidenceLinker,
    ReleaseBundleBuilder,
    assert_clean_git,
    source_revision,
)
from .ci import (
    GateContext,
    GateExecutor,
    PythonTestPolicy,
    ReleaseAdmission,
    standard_gate_registry,
)
from .cleanroom import CleanInstallRunner, PlatformPlanner
from .errors import GateFailure, ReleaseError
from .integrity import (
    BoundaryScanner,
    BunLock,
    PythonLock,
    sha256_file,
    stable_digest,
)
from .inventory import (
    ConfigurationProvisioner,
    JavaScriptComponentInventory,
    NoticeBuilder,
    PythonComponentInventory,
    RuntimeInventory,
    SbomBuilder,
)
from .policy import DEFAULT_RELEASE_POLICY, ReleasePolicy
from .submission import CleanInstallReceiptVerifier
from .transactions import (
    InstallReceiptStore,
    MigrationExecutor,
    ReleaseInstaller,
    default_migration_registry,
)


class ReleaseDoctor:
    def __init__(
        self,
        project_root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.project_root = project_root.resolve()
        self.policy = policy

    def run(
        self,
        *,
        require_clean_git: bool = False,
        require_hashes: bool = True,
        require_tools: bool = True,
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        checks.append(self._check_project_root())
        checks.append(self._check_python(require_hashes=require_hashes))
        checks.append(self._check_javascript())
        checks.append(self._check_boundary())
        checks.append(self._check_runtime_inventory())
        checks.append(self._check_configuration())
        checks.append(self._check_platform_plans())
        if require_tools:
            checks.append(self._check_tools())
        if require_clean_git:
            checks.append(self._check_clean_git())
        blockers = [
            {
                "check": item["check"],
                "code": item.get("code", ""),
                "details": item.get("details", {}),
            }
            for item in checks
            if item.get("ready") is not True
        ]
        report = {
            "schema": "zyra.release-doctor/v1",
            "ready": not blockers,
            "project_root": str(self.project_root),
            "source_commit": self._safe_revision(),
            "platform": platform.system().lower(),
            "architecture": platform.machine().lower(),
            "python": platform.python_version(),
            "checks": checks,
            "blockers": blockers,
            "blocker_count": len(blockers),
        }
        report["digest"] = stable_digest(report)
        return report

    def enforce(
        self,
        *,
        require_clean_git: bool = False,
        require_hashes: bool = True,
        require_tools: bool = True,
    ) -> dict[str, Any]:
        report = self.run(
            require_clean_git=require_clean_git,
            require_hashes=require_hashes,
            require_tools=require_tools,
        )
        if not report["ready"]:
            raise ReleaseError(
                "Release doctor found blockers.",
                code="release_doctor_blocked",
                details={
                    "blocker_count": report["blocker_count"],
                    "blockers": report["blockers"],
                },
            )
        return report

    def _check_project_root(self) -> dict[str, Any]:
        required = (
            "pyproject.toml",
            "requirements.txt",
            "package.json",
            "bun.lock",
            "README.md",
            "apps",
            "packages",
            "scripts",
            "tests",
        )
        missing = [
            item
            for item in required
            if not (self.project_root / item).exists()
        ]
        return {
            "check": "project-root",
            "ready": not missing,
            "code": "" if not missing else "release_project_incomplete",
            "details": {"missing": missing},
        }

    def _check_python(self, *, require_hashes: bool) -> dict[str, Any]:
        try:
            lock = PythonLock.load(
                self.project_root / "requirements.txt",
                require_hashes=require_hashes,
            )
            with (self.project_root / "pyproject.toml").open("rb") as stream:
                pyproject = tomllib.load(stream)
            receipt = lock.verify_project_requirements(pyproject)
            return {
                "check": "python-lock",
                "ready": True,
                "details": receipt,
            }
        except BaseException as error:
            return self._failed("python-lock", error)

    def _check_javascript(self) -> dict[str, Any]:
        try:
            receipt = BunLock.load(
                self.project_root / "bun.lock"
            ).verify_workspace(self.project_root)
            return {
                "check": "javascript-lock",
                "ready": True,
                "details": receipt,
            }
        except BaseException as error:
            return self._failed("javascript-lock", error)

    def _check_boundary(self) -> dict[str, Any]:
        try:
            receipt = BoundaryScanner(
                self.project_root,
                policy=self.policy,
            ).enforce()
            return {
                "check": "submission-boundary",
                "ready": True,
                "details": receipt,
            }
        except BaseException as error:
            return self._failed("submission-boundary", error)

    def _check_runtime_inventory(self) -> dict[str, Any]:
        try:
            receipt = RuntimeInventory(
                self.project_root,
                policy=self.policy,
            ).enforce()
            return {
                "check": "runtime-inventory",
                "ready": True,
                "details": receipt,
            }
        except BaseException as error:
            return self._failed("runtime-inventory", error)

    def _check_configuration(self) -> dict[str, Any]:
        path = self.project_root / "config" / "release.example.json"
        if not path.is_file():
            return {
                "check": "release-configuration",
                "ready": False,
                "code": "release_configuration_missing",
                "details": {"path": str(path)},
            }
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise TypeError("configuration root must be an object")
            receipt = ConfigurationProvisioner(
                policy=self.policy,
            ).validate_template(value)
            return {
                "check": "release-configuration",
                "ready": True,
                "details": receipt,
            }
        except BaseException as error:
            return self._failed("release-configuration", error)

    def _check_platform_plans(self) -> dict[str, Any]:
        try:
            planner = PlatformPlanner(self.project_root)
            plans = {
                target: planner.plan(target).to_dict()
                for target in ("windows", "linux")
            }
            return {
                "check": "platform-plans",
                "ready": True,
                "details": {"plans": plans},
            }
        except BaseException as error:
            return self._failed("platform-plans", error)

    def _check_tools(self) -> dict[str, Any]:
        required = {
            "git": shutil.which("git"),
            "bun": self._resolve_bun(),
            "python": sys.executable,
        }
        missing = sorted(name for name, path in required.items() if not path)
        return {
            "check": "build-tools",
            "ready": not missing,
            "code": "" if not missing else "release_tool_missing",
            "details": {
                "tools": {
                    name: str(Path(path).resolve()) if path else ""
                    for name, path in required.items()
                },
                "missing": missing,
            },
        }

    def _resolve_bun(self) -> str | None:
        global_bun = shutil.which("bun")
        if global_bun:
            return global_bun
        local = self.project_root / "node_modules" / ".bin" / (
            "bun.exe" if os.name == "nt" else "bun"
        )
        if local.is_file():
            return str(local)
        return None

    def _check_clean_git(self) -> dict[str, Any]:
        try:
            receipt = assert_clean_git(self.project_root)
            return {
                "check": "clean-git",
                "ready": True,
                "details": receipt,
            }
        except BaseException as error:
            return self._failed("clean-git", error)

    def _safe_revision(self) -> str:
        try:
            return source_revision(self.project_root)
        except BaseException:
            return ""

    @staticmethod
    def _failed(check: str, error: BaseException) -> dict[str, Any]:
        if isinstance(error, ReleaseError):
            code = error.code
            details = error.to_dict()
        else:
            code = type(error).__name__
            details = {
                "type": type(error).__name__,
                "message": str(error),
            }
        return {
            "check": check,
            "ready": False,
            "code": code,
            "details": details,
        }


class ReleaseRuntime:
    def __init__(
        self,
        project_root: Path,
        *,
        work_root: Path | None = None,
        output_root: Path | None = None,
        state_root: Path | None = None,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.project_root = project_root.resolve()
        self.work_root = (
            work_root.resolve()
            if work_root is not None
            else self.project_root / ".tmp" / "release-work"
        )
        self.output_root = (
            output_root.resolve()
            if output_root is not None
            else self.project_root / "dist" / "release"
        )
        self.state_root = (
            state_root.resolve()
            if state_root is not None
            else self.project_root / "tmp" / "release-state"
        )
        self.policy = policy

    def doctor(
        self,
        *,
        require_clean_git: bool = False,
        require_hashes: bool = True,
        require_tools: bool = True,
    ) -> dict[str, Any]:
        return ReleaseDoctor(
            self.project_root,
            policy=self.policy,
        ).run(
            require_clean_git=require_clean_git,
            require_hashes=require_hashes,
            require_tools=require_tools,
        )

    def build(
        self,
        *,
        release_id: str,
        expected_commit: str | None = None,
        archive_format: str = "tar.gz",
        require_python_hashes: bool = True,
        benchmark_expected_commit: str | None = None,
    ) -> dict[str, Any]:
        ReleaseDoctor(
            self.project_root,
            policy=self.policy,
        ).enforce(
            require_clean_git=False,
            require_hashes=require_python_hashes,
            require_tools=True,
        )
        self.output_root.mkdir(parents=True, exist_ok=True)
        return ReleaseBundleBuilder(
            self.project_root,
            self.output_root,
            policy=self.policy,
        ).build(
            release_id=release_id,
            expected_commit=expected_commit,
            archive_format=archive_format,
            require_python_hashes=require_python_hashes,
            benchmark_expected_commit=benchmark_expected_commit,
        )

    def verify(self, archive: Path) -> dict[str, Any]:
        return ReleaseBundleBuilder(
            self.project_root,
            self.output_root,
            policy=self.policy,
        ).verify(archive)

    def install(
        self,
        archive: Path,
        *,
        install_root: Path,
        idempotency_key: str,
        release_id: str,
        manifest_digest: str,
        migration_version: int = 1,
    ) -> dict[str, Any]:
        self.state_root.mkdir(parents=True, exist_ok=True)
        with InstallReceiptStore(
            self.state_root / "install.sqlite3"
        ) as store:
            installer = ReleaseInstaller(
                store,
                migrations=MigrationExecutor(
                    default_migration_registry(),
                    store,
                ),
            )
            receipt = installer.install(
                archive,
                install_root=install_root,
                idempotency_key=idempotency_key,
                release_id=release_id,
                manifest_digest=manifest_digest,
                target_migration_version=migration_version,
            )
            return {
                "ready": receipt.state.value == "committed",
                "receipt": receipt.to_dict(),
                "events": store.events(receipt.transaction_id),
                "migration_journal": store.migration_journal(
                    receipt.transaction_id
                ),
            }

    def uninstall(
        self,
        transaction_id: str,
        *,
        purge_state: bool = False,
    ) -> dict[str, Any]:
        with InstallReceiptStore(
            self.state_root / "install.sqlite3"
        ) as store:
            installer = ReleaseInstaller(store)
            receipt = installer.uninstall(
                transaction_id,
                purge_state=purge_state,
            )
            return {
                "ready": receipt.state.value == "uninstalled",
                "receipt": receipt.to_dict(),
                "events": store.events(transaction_id),
            }

    def migrate(
        self,
        transaction_id: str,
        *,
        target_version: int,
    ) -> dict[str, Any]:
        with InstallReceiptStore(
            self.state_root / "install.sqlite3"
        ) as store:
            installer = ReleaseInstaller(
                store,
                migrations=MigrationExecutor(
                    default_migration_registry(),
                    store,
                ),
            )
            receipt = installer.migrate_committed(
                transaction_id,
                target_migration_version=target_version,
            )
            return {
                "ready": receipt.state.value == "committed"
                and receipt.migration_version == target_version,
                "receipt": receipt.to_dict(),
                "events": store.events(transaction_id),
                "migration_journal": store.migration_journal(transaction_id),
            }

    def rollback(
        self,
        transaction_id: str,
        *,
        target_version: int = 0,
    ) -> dict[str, Any]:
        with InstallReceiptStore(
            self.state_root / "install.sqlite3"
        ) as store:
            installer = ReleaseInstaller(
                store,
                migrations=MigrationExecutor(
                    default_migration_registry(),
                    store,
                ),
            )
            receipt = installer.rollback_committed(
                transaction_id,
                target_migration_version=target_version,
            )
            return {
                "ready": receipt.state.value == "rolled_back",
                "receipt": receipt.to_dict(),
                "events": store.events(transaction_id),
                "migration_journal": store.migration_journal(transaction_id),
            }

    def lifecycle(
        self,
        action: str,
        *,
        build_web: bool = True,
        include_short_task: bool = True,
    ) -> dict[str, Any]:
        normalized = action.casefold()
        if normalized not in {
            "start",
            "stop",
            "restart",
            "status",
            "doctor",
            "health",
        }:
            raise ReleaseError(
                "Release lifecycle action is unsupported.",
                code="release_lifecycle_action_unsupported",
                details={"action": action},
            )
        self.state_root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "zyra_orchestration.deployment.cli",
            "--project-root",
            str(self.project_root),
            "--state-root",
            str(self.state_root / "deployment"),
            normalized,
        ]
        if normalized == "start" and not build_web:
            command.append("--no-build-web")
        if normalized == "health" and not include_short_task:
            command.append("--no-short-task")
        completed = subprocess.run(
            command,
            cwd=self.project_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        try:
            deployment = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseError(
                "Deployment lifecycle returned invalid JSON.",
                code="release_lifecycle_output_invalid",
                details={
                    "action": normalized,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-8192:],
                    "stderr": completed.stderr[-8192:],
                },
            ) from error
        if not isinstance(deployment, Mapping):
            raise ReleaseError(
                "Deployment lifecycle response is not an object.",
                code="release_lifecycle_output_type",
                details={"action": normalized},
            )
        ready = completed.returncode == 0 and bool(
            deployment.get(
                "ready",
                deployment.get("stopped", deployment.get("healthy", True)),
            )
        )
        result = {
            "schema": "zyra.release-lifecycle-receipt/v1",
            "ready": ready,
            "action": normalized,
            "source_commit": self._release_revision(),
            "command": command,
            "returncode": completed.returncode,
            "deployment": dict(deployment),
            "stdout_digest": stable_digest(completed.stdout),
            "stderr_digest": stable_digest(completed.stderr),
        }
        if not ready:
            raise ReleaseError(
                "Deployment lifecycle action failed.",
                code="release_lifecycle_failed",
                details=result,
            )
        return result

    def _release_revision(self) -> str:
        try:
            return source_revision(self.project_root)
        except ReleaseError:
            manifest_path = self.project_root / "release" / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ReleaseError(
                    "Release revision is unavailable from Git and manifest.",
                    code="release_revision_unavailable",
                    details={
                        "project_root": str(self.project_root),
                        "manifest": str(manifest_path),
                        "error": str(error),
                    },
                ) from error
            if not isinstance(manifest, Mapping):
                raise ReleaseError(
                    "Installed release manifest is not an object.",
                    code="release_revision_manifest_invalid",
                )
            revision = str(manifest.get("source_commit") or "")
            if len(revision) != 40 or any(
                character not in "0123456789abcdef" for character in revision
            ):
                raise ReleaseError(
                    "Installed release manifest has no valid source commit.",
                    code="release_revision_manifest_commit_invalid",
                    details={"source_commit": revision},
                )
            return revision

    def clean_install(
        self,
        archive: Path,
        *,
        expected_commit: str,
        output: Path,
        offline: bool = False,
        run_product_lifecycle: bool = True,
    ) -> dict[str, Any]:
        return CleanInstallRunner(
            self.project_root,
            policy=self.policy,
        ).run(
            archive,
            expected_commit=expected_commit,
            output=output,
            offline=offline,
            run_product_lifecycle=run_product_lifecycle,
        )

    def ci(
        self,
        *,
        archive: Path,
        expected_commit: str,
        python: str | None = None,
        bun: str | None = None,
        maximum_parallel: int = 2,
    ) -> dict[str, Any]:
        python = python or sys.executable
        bun = bun or ReleaseDoctor(
            self.project_root,
            policy=self.policy,
        )._resolve_bun() or "bun"
        evidence_root = self.output_root / "ci"
        evidence_root.mkdir(parents=True, exist_ok=True)
        callables = self._ci_callables(
            archive=archive,
            expected_commit=expected_commit,
            evidence_root=evidence_root,
        )
        with tempfile.TemporaryDirectory(
            prefix="zyra-release-pytest-",
            ignore_cleanup_errors=True,
        ) as python_basetemp:
            registry = standard_gate_registry(
                python=python,
                bun=bun,
                output_root=evidence_root,
                python_basetemp=Path(python_basetemp),
                callable_gates=callables,
                python_test_arguments=PythonTestPolicy.load(
                    self.project_root,
                    self.project_root / "config" / "release-python-tests.json",
                ).pytest_arguments(),
            )
            executor = GateExecutor(
                registry,
                project_root=self.project_root,
                output_root=evidence_root,
                source_commit=expected_commit,
                environment=self._ci_environment(),
                maximum_parallel=maximum_parallel,
            )
            report = executor.execute()
        report_path = evidence_root / "ci-report.json"
        self._write_json(report_path, report)
        admission = ReleaseAdmission(policy=self.policy).verify(
            report,
            expected_commit=expected_commit,
        )
        admission_path = evidence_root / "release-admission.json"
        self._write_json(admission_path, admission)
        return {
            "schema": "zyra.release-ci-verdict/v1",
            "ready": True,
            "source_commit": expected_commit,
            "report": str(report_path),
            "report_sha256": sha256_file(report_path),
            "admission": admission,
            "admission_path": str(admission_path),
        }

    def _ci_callables(
        self,
        *,
        archive: Path,
        expected_commit: str,
        evidence_root: Path,
    ) -> dict[str, Any]:
        def python_lock(_: GateContext) -> Mapping[str, Any]:
            with (self.project_root / "pyproject.toml").open("rb") as stream:
                pyproject = tomllib.load(stream)
            return PythonLock.load(
                self.project_root / "requirements.txt",
                require_hashes=True,
            ).verify_project_requirements(pyproject)

        def javascript_lock(_: GateContext) -> Mapping[str, Any]:
            return BunLock.load(
                self.project_root / "bun.lock"
            ).verify_workspace(self.project_root)

        def bundle_boundary(_: GateContext) -> Mapping[str, Any]:
            return BoundaryScanner(
                self.project_root,
                policy=self.policy,
            ).enforce()

        def checksums(_: GateContext) -> Mapping[str, Any]:
            value = ReleaseBundleBuilder(
                self.project_root,
                self.output_root,
                policy=self.policy,
            ).verify(archive)
            self._write_json(
                evidence_root / "bundle-verification.json",
                value,
            )
            return value

        def sbom_notice(_: GateContext) -> Mapping[str, Any]:
            verification = ReleaseBundleBuilder(
                self.project_root,
                self.output_root,
                policy=self.policy,
            ).verify(archive)
            value = {
                "ready": all(
                    name in verification["verified_artifacts"]
                    for name in ("sbom", "notice", "runtime_inventory")
                ),
                "artifacts": {
                    name: verification["verified_artifacts"][name]
                    for name in ("sbom", "notice", "runtime_inventory")
                },
            }
            self._write_json(
                evidence_root / "sbom-verification.json",
                value,
            )
            return value

        def source_custody(context: GateContext) -> Mapping[str, Any]:
            receipt_path = evidence_root / "source-custody-receipt.json"
            queue_path = evidence_root / "source-custody-work-queue.json"
            command = [
                sys.executable,
                "scripts/audit_source_custody.py",
                "--project-root",
                str(self.project_root),
                "--mode",
                "candidate",
                "--revision",
                expected_commit,
                "--receipt",
                str(receipt_path),
                "--work-queue",
                str(queue_path),
                "--json",
            ]
            completed = subprocess.run(
                command,
                cwd=self.project_root,
                env=dict(context.environment),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            try:
                summary = json.loads(completed.stdout)
            except json.JSONDecodeError:
                summary = {
                    "release_ready": False,
                    "stdout_digest": stable_digest(completed.stdout),
                }
            ready = (
                completed.returncode == 0
                and isinstance(summary, Mapping)
                and summary.get("release_ready") is True
                and receipt_path.is_file()
                and queue_path.is_file()
            )
            return {
                "ready": ready,
                "command": command,
                "returncode": completed.returncode,
                "summary": summary,
                "stderr": completed.stderr[-16_384:],
            }

        def clean_install(_: GateContext) -> Mapping[str, Any]:
            return CleanInstallRunner(
                self.project_root,
                policy=self.policy,
            ).run(
                archive,
                    expected_commit=expected_commit,
                    output=evidence_root / "clean-install.json",
                    offline=False,
                    run_product_lifecycle=True,
                )

        def semantic_health(_: GateContext) -> Mapping[str, Any]:
            try:
                clean_receipt = json.loads(
                    (evidence_root / "clean-install.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, json.JSONDecodeError) as error:
                raise GateFailure(
                    "Clean-install semantic evidence is unavailable.",
                    code="release_semantic_evidence_invalid",
                    details={"error": str(error)},
                ) from error
            if not isinstance(clean_receipt, Mapping):
                raise GateFailure(
                    "Clean-install semantic evidence is not an object.",
                    code="release_semantic_evidence_invalid",
                )
            clean_admission = CleanInstallReceiptVerifier().verify(
                clean_receipt,
                expected_commit=expected_commit,
            )
            nested = clean_receipt.get("receipts")
            lifecycle = (
                nested.get("lifecycle")
                if isinstance(nested, Mapping)
                else None
            )
            commands = (
                lifecycle.get("commands")
                if isinstance(lifecycle, Mapping)
                else None
            )
            semantic_receipts = [
                item
                for item in commands or ()
                if isinstance(item, Mapping)
                and item.get("name") == "semantic-health"
            ]
            semantic = semantic_receipts[0] if len(semantic_receipts) == 1 else {}
            failures: list[str] = []
            if len(semantic_receipts) != 1:
                failures.append("semantic_receipt_count")
            if semantic.get("ready") is not True:
                failures.append("semantic_health_not_ready")
            if semantic.get("returncode") != 0:
                failures.append("semantic_health_returncode")
            command = semantic.get("command")
            if (
                not isinstance(command, list)
                or command[-2:] != ["lifecycle", "health"]
            ):
                failures.append("semantic_health_command")
            value = {
                "schema": "zyra.release-semantic-health-verification/v1",
                "ready": not failures,
                "source_commit": expected_commit,
                "clean_install_admission": clean_admission,
                "clean_install_digest": stable_digest(clean_receipt),
                "semantic_health": {
                    "command": command if isinstance(command, list) else [],
                    "duration_ms": semantic.get("duration_ms"),
                    "returncode": semantic.get("returncode"),
                    "stdout_digest": semantic.get("stdout_digest"),
                    "stderr_digest": semantic.get("stderr_digest"),
                    "timed_out": semantic.get("timed_out"),
                },
                "failures": failures,
            }
            value["digest"] = stable_digest(value)
            self._write_json(
                evidence_root / "semantic-health.json",
                value,
            )
            return value

        def benchmark_link(_: GateContext) -> Mapping[str, Any]:
            value = BenchmarkEvidenceLinker(self.project_root).link(
                expected_commit=None,
            )
            value["release_source_commit"] = expected_commit
            value["release_binding_digest"] = stable_digest(
                {
                    "release_source_commit": expected_commit,
                    "benchmark_digest": value["digest"],
                }
            )
            self._write_json(
                evidence_root / "benchmark-link.json",
                value,
            )
            return value

        return {
            "python-lock": python_lock,
            "javascript-lock": javascript_lock,
            "bundle-boundary": bundle_boundary,
            "checksums": checksums,
            "sbom-notice": sbom_notice,
            "source-custody": source_custody,
            "clean-install": clean_install,
            "semantic-health": semantic_health,
            "benchmark-link": benchmark_link,
        }

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def _ci_environment(self) -> dict[str, str]:
        allowed = {
            name: value
            for name, value in os.environ.items()
            if self.policy.environment_allowed(name)
        }
        allowed.update(
            {
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
                "CI": "1",
                "ZYRA_RELEASE_CI": "1",
            }
        )
        return allowed


__all__ = ["ReleaseDoctor", "ReleaseRuntime"]
