from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import venv
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zyra_integrations.loopx.runtime import LoopXDoctor, LoopXRuntimeResolver

from .bundle import ReleaseBundleBuilder, source_revision
from .errors import CleanroomFailure, IntegrityViolation, LockViolation, ReleaseError
from .integrity import (
    ArchiveInspector,
    PythonLock,
    normalize_relative_path,
    resolve_below,
    sha256_file,
    stable_digest,
)
from .models import PlatformPlan
from .policy import DEFAULT_RELEASE_POLICY, ReleasePolicy
from .transactions import (
    InstallReceiptStore,
    MigrationExecutor,
    ReleaseInstaller,
    default_migration_registry,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    cwd: str
    returncode: int
    started_at: str
    finished_at: str
    duration_ms: float
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ready(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_digest": stable_digest(self.stdout),
            "stderr_digest": stable_digest(self.stderr),
            "timed_out": self.timed_out,
            "ready": self.ready,
        }


class CommandRunner:
    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        maximum_output: int = 128 * 1024,
    ) -> None:
        self.environment = dict(environment or {})
        self.maximum_output = maximum_output

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        check: bool = True,
        extra_environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        if not command:
            raise CleanroomFailure(
                "Cleanroom command is empty.",
                code="cleanroom_command_empty",
            )
        executable = str(command[0])
        if any(character in executable for character in "\r\n\x00"):
            raise CleanroomFailure(
                "Cleanroom executable contains an invalid character.",
                code="cleanroom_executable_invalid",
                details={"executable": executable},
            )
        environment = dict(self.environment)
        environment.update(extra_environment or {})
        started_at = utc_now()
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                [str(item) for item in command],
                cwd=cwd,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            returncode = completed.returncode
            stdout = completed.stdout[-self.maximum_output :]
            stderr = completed.stderr[-self.maximum_output :]
        except subprocess.TimeoutExpired as error:
            timed_out = True
            returncode = -1
            stdout = self._decode_timeout_output(error.stdout)
            stderr = self._decode_timeout_output(error.stderr)
        finished = time.monotonic()
        result = CommandResult(
            command=tuple(str(item) for item in command),
            cwd=str(cwd.resolve()),
            returncode=returncode,
            started_at=started_at,
            finished_at=utc_now(),
            duration_ms=round((finished - started) * 1000, 3),
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )
        if check and not result.ready:
            raise CleanroomFailure(
                "Cleanroom command failed.",
                code="cleanroom_command_failed",
                details=result.to_dict(),
            )
        return result

    def which(self, executable: str) -> str:
        path = shutil.which(executable, path=self.environment.get("PATH"))
        if path is None:
            raise CleanroomFailure(
                "Required cleanroom executable is unavailable.",
                code="cleanroom_dependency_missing",
                details={"executable": executable},
            )
        return str(Path(path).resolve())

    @staticmethod
    def _decode_timeout_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value


class PlatformPlanner:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def plan(
        self,
        target: str,
        *,
        architecture: str | None = None,
        python: str | None = None,
        offline: bool = False,
        wheelhouse: str = "release/wheelhouse",
    ) -> PlatformPlan:
        normalized = target.casefold()
        if normalized not in {"windows", "linux"}:
            raise CleanroomFailure(
                "Release target platform is unsupported.",
                code="platform_plan_unsupported",
                details={"platform": target},
            )
        architecture = architecture or self._default_architecture(normalized)
        if architecture not in {"amd64", "arm64", "x86_64", "aarch64"}:
            raise CleanroomFailure(
                "Release target architecture is unsupported.",
                code="platform_architecture_unsupported",
                details={"architecture": architecture},
            )
        if normalized == "windows":
            python = python or "python.exe"
            venv_python = r".venv\Scripts\python.exe"
            venv_pip = (venv_python, "-m", "pip")
            launcher = (venv_python, "-m", "zyra_productization.release.cli")
        else:
            python = python or "python3"
            venv_python = ".venv/bin/python"
            venv_pip = (venv_python, "-m", "pip")
            launcher = (venv_python, "-m", "zyra_productization.release.cli")
        pip_install = [
            *venv_pip,
            "install",
            "--require-hashes",
            "-r",
            "requirements.txt",
        ]
        if offline:
            pip_install.extend(["--no-index", "--find-links", wheelhouse])
        bundled_wheels = sorted(
            (self.project_root / "release" / "wheels").glob("zyra-*.whl")
        )
        wheel_source = (
            bundled_wheels[0].relative_to(self.project_root).as_posix()
            if len(bundled_wheels) == 1
            else "release/wheels/zyra-<version>-py3-none-any.whl"
        )
        commands = (
            (python, "-m", "venv", ".venv"),
            tuple(pip_install),
            (
                *venv_pip,
                "install",
                "--no-deps",
                wheel_source,
            ),
            ("bun", "install", "--frozen-lockfile", "--ignore-scripts"),
            ("bun", "run", "typecheck"),
            (
                "bun",
                "build",
                "apps/code-worker/src/main.ts",
                "--outdir",
                "dist/code-worker",
                "--target",
                "bun",
            ),
            (
                "bun",
                "build",
                "apps/code-worker/src/main.ts",
                "--outdir",
                "dist/code-worker-node",
                "--target",
                "node",
            ),
            (
                "bun",
                "build",
                "apps/web/index.html",
                "--outdir",
                "apps/web/dist",
                "--target",
                "browser",
                "--sourcemap=linked",
            ),
        )
        lifecycle = {
            "start": (*launcher, "lifecycle", "start"),
            "stop": (*launcher, "lifecycle", "stop"),
            "doctor": (*launcher, "doctor"),
            "health": (*launcher, "health"),
            "migrate": (
                *launcher,
                "migrate",
                "<transaction-id>",
                "--target-version",
                "<version>",
            ),
            "rollback": (
                *launcher,
                "rollback",
                "<transaction-id>",
                "--target-version",
                "<version>",
            ),
            "uninstall": (*launcher, "uninstall", "<transaction-id>"),
        }
        environment = {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "BUN_INSTALL_CACHE_DIR": ".release-cache/bun",
            "SOURCE_DATE_EPOCH": str(DEFAULT_RELEASE_POLICY.source_date_epoch),
        }
        if offline:
            environment["PIP_NO_INDEX"] = "1"
            environment["ZYRA_NETWORK_MODE"] = "offline"
        return PlatformPlan(
            platform=normalized,
            architecture=architecture,
            python=python,
            install_commands=commands,
            lifecycle_commands=lifecycle,
            environment=environment,
        )

    def current(
        self,
        *,
        offline: bool = False,
    ) -> PlatformPlan:
        system = platform.system().casefold()
        target = "windows" if system == "windows" else "linux"
        return self.plan(
            target,
            architecture=self._normalize_architecture(platform.machine()),
            python=sys.executable,
            offline=offline,
        )

    @staticmethod
    def _default_architecture(target: str) -> str:
        return "amd64" if target == "windows" else "x86_64"

    @staticmethod
    def _normalize_architecture(value: str) -> str:
        normalized = value.casefold()
        if normalized in {"amd64", "x86_64", "x64"}:
            return "amd64" if os.name == "nt" else "x86_64"
        if normalized in {"arm64", "aarch64"}:
            return "arm64" if os.name == "nt" else "aarch64"
        return normalized


class OfflineArtifactResolver:
    _WHEEL = re.compile(
        r"^(?P<name>.+?)-(?P<version>[0-9][^-]*)-(?P<python>[^-]+)-"
        r"(?P<abi>[^-]+)-(?P<platform>[^.]+)\.whl$",
        re.IGNORECASE,
    )

    def __init__(self, wheelhouse: Path) -> None:
        self.wheelhouse = wheelhouse.resolve()

    def inventory(self) -> dict[str, list[dict[str, Any]]]:
        if not self.wheelhouse.is_dir():
            raise LockViolation(
                "Offline wheelhouse is missing.",
                code="offline_wheelhouse_missing",
                details={"path": str(self.wheelhouse)},
            )
        output: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(self.wheelhouse.glob("*.whl")):
            match = self._WHEEL.fullmatch(path.name)
            if match is None:
                continue
            name = self._canonical_name(match.group("name"))
            output.setdefault(name, []).append(
                {
                    "path": path.name,
                    "version": match.group("version"),
                    "python": match.group("python"),
                    "abi": match.group("abi"),
                    "platform": match.group("platform"),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
        return output

    def resolve(
        self,
        lock: PythonLock,
        *,
        python_tag: str | None = None,
        platform_tags: Iterable[str] = (),
    ) -> dict[str, Any]:
        inventory = self.inventory()
        tags = {item.casefold() for item in platform_tags}
        selected: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        hash_mismatch: list[dict[str, Any]] = []
        for requirement in lock.records:
            candidates = [
                item
                for item in inventory.get(requirement.canonical_name, ())
                if item["version"] == requirement.version
            ]
            compatible = [
                item
                for item in candidates
                if self._compatible(item, python_tag=python_tag, platform_tags=tags)
            ]
            if not compatible:
                missing.append(
                    {
                        "name": requirement.canonical_name,
                        "version": requirement.version,
                    }
                )
                continue
            hashed = [
                item
                for item in compatible
                if not requirement.hashes or item["sha256"] in requirement.hashes
            ]
            if not hashed:
                hash_mismatch.append(
                    {
                        "name": requirement.canonical_name,
                        "version": requirement.version,
                        "expected_hashes": list(requirement.hashes),
                        "actual_hashes": [item["sha256"] for item in compatible],
                    }
                )
                continue
            selected.append(
                sorted(
                    hashed,
                    key=lambda item: (
                        item["platform"] != "any",
                        item["path"],
                    ),
                )[0]
            )
        ready = not missing and not hash_mismatch
        report = {
            "schema": "zyra.offline-artifact-receipt/v1",
            "ready": ready,
            "wheelhouse": str(self.wheelhouse),
            "requirement_count": len(lock.records),
            "selected": selected,
            "missing": missing,
            "hash_mismatch": hash_mismatch,
            "inventory_digest": stable_digest(inventory),
            "selection_digest": stable_digest(selected),
        }
        if not ready:
            raise LockViolation(
                "Offline dependency closure is incomplete.",
                code="offline_dependency_incomplete",
                details=report,
            )
        return report

    @staticmethod
    def _compatible(
        item: Mapping[str, Any],
        *,
        python_tag: str | None,
        platform_tags: set[str],
    ) -> bool:
        wheel_python = str(item["python"]).casefold()
        wheel_platform = str(item["platform"]).casefold()
        if python_tag and wheel_python not in {
            "py3",
            "py2.py3",
            python_tag.casefold(),
        }:
            return False
        if platform_tags and wheel_platform != "any":
            parts = set(wheel_platform.split("."))
            if not parts.intersection(platform_tags):
                return False
        return True

    @staticmethod
    def _canonical_name(value: str) -> str:
        return value.lower().replace("_", "-").replace(".", "-")


class PortAvailabilityProbe:
    def probe(
        self,
        host: str,
        ports: Iterable[int],
    ) -> dict[str, Any]:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise CleanroomFailure(
                "Release port probe is restricted to loopback.",
                code="cleanroom_port_host_unsafe",
                details={"host": host},
            )
        available: list[int] = []
        occupied: list[int] = []
        invalid: list[int] = []
        for port in sorted(set(int(item) for item in ports)):
            if not 1 <= port <= 65535:
                invalid.append(port)
                continue
            family = socket.AF_INET6 if host == "::1" else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                try:
                    probe.bind((host, port))
                except OSError:
                    occupied.append(port)
                else:
                    available.append(port)
        ready = not occupied and not invalid
        report = {
            "schema": "zyra.port-availability/v1",
            "ready": ready,
            "host": host,
            "available": available,
            "occupied": occupied,
            "invalid": invalid,
        }
        if not ready:
            raise CleanroomFailure(
                "Required release ports are unavailable.",
                code="cleanroom_port_unavailable",
                details=report,
            )
        return report


class ProcessLifecycle:
    def __init__(
        self,
        state_root: Path,
        *,
        environment: Mapping[str, str],
    ) -> None:
        self.state_root = state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.environment = dict(environment)
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.logs: dict[str, tuple[Path, Path]] = {}

    def start(
        self,
        name: str,
        command: Sequence[str],
        *,
        cwd: Path,
        readiness: Callable[[], bool],
        timeout: float,
    ) -> dict[str, Any]:
        if name in self.processes and self.processes[name].poll() is None:
            raise CleanroomFailure(
                "Cleanroom process is already running.",
                code="cleanroom_process_duplicate",
                details={"name": name},
            )
        stdout_path = self.state_root / f"{name}.stdout.log"
        stderr_path = self.state_root / f"{name}.stderr.log"
        stdout_stream = stdout_path.open("w", encoding="utf-8")
        stderr_stream = stderr_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [str(item) for item in command],
                cwd=cwd,
                env=self.environment,
                stdout=stdout_stream,
                stderr=stderr_stream,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0
                ),
                start_new_session=os.name != "nt",
            )
        except BaseException:
            stdout_stream.close()
            stderr_stream.close()
            raise
        stdout_stream.close()
        stderr_stream.close()
        self.processes[name] = process
        self.logs[name] = (stdout_path, stderr_path)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            returncode = process.poll()
            if returncode is not None:
                raise CleanroomFailure(
                    "Cleanroom process exited before readiness.",
                    code="cleanroom_process_early_exit",
                    details={
                        "name": name,
                        "returncode": returncode,
                        "stdout": self._tail(stdout_path),
                        "stderr": self._tail(stderr_path),
                    },
                )
            try:
                if readiness():
                    return {
                        "name": name,
                        "pid": process.pid,
                        "command": list(command),
                        "ready": True,
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                    }
            except OSError:
                pass
            time.sleep(0.1)
        self.stop(name)
        raise CleanroomFailure(
            "Cleanroom process readiness timed out.",
            code="cleanroom_process_readiness_timeout",
            details={"name": name, "timeout": timeout},
        )

    def stop(self, name: str, *, timeout: float = 15.0) -> dict[str, Any]:
        process = self.processes.get(name)
        if process is None:
            return {"name": name, "stopped": True, "already_stopped": True}
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout_path, stderr_path = self.logs[name]
        result = {
            "name": name,
            "stopped": True,
            "already_stopped": False,
            "returncode": process.returncode,
            "stdout": self._tail(stdout_path),
            "stderr": self._tail(stderr_path),
        }
        self.processes.pop(name, None)
        return result

    def stop_all(self) -> list[dict[str, Any]]:
        return [
            self.stop(name)
            for name in reversed(tuple(self.processes))
        ]

    def assert_stopped(self) -> None:
        alive = [
            {"name": name, "pid": process.pid}
            for name, process in self.processes.items()
            if process.poll() is None
        ]
        if alive:
            raise CleanroomFailure(
                "Cleanroom left supervised processes running.",
                code="cleanroom_process_leak",
                details={"processes": alive},
            )

    @staticmethod
    def _tail(path: Path, maximum: int = 16 * 1024) -> str:
        if not path.is_file():
            return ""
        data = path.read_bytes()
        return data[-maximum:].decode("utf-8", errors="replace")


class CleanInstallRunner:
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
        archive: Path,
        *,
        expected_commit: str,
        output: Path,
        offline: bool = False,
        run_product_lifecycle: bool = True,
        command_timeout: float = 600.0,
    ) -> dict[str, Any]:
        started_at = utc_now()
        started = time.monotonic()
        revision = source_revision(self.project_root)
        if revision != expected_commit:
            raise CleanroomFailure(
                "Cleanroom project revision does not match frozen target.",
                code="cleanroom_revision_mismatch",
                details={"expected": expected_commit, "actual": revision},
            )
        workspace = Path(
            tempfile.mkdtemp(prefix="zyra-release-cleanroom-")
        ).resolve()
        receipts: dict[str, Any] = {}
        commands: list[dict[str, Any]] = []
        install_store: InstallReceiptStore | None = None
        lifecycle: ProcessLifecycle | None = None
        try:
            extracted = workspace / "extracted"
            inspection = ArchiveInspector(
                archive,
                policy=self.policy,
            ).safe_extract(extracted)
            receipts["archive"] = inspection
            bundle_root = extracted / str(inspection["root"])
            payload_roots = [item for item in bundle_root.iterdir() if item.is_dir()]
            if len(payload_roots) != 1:
                raise CleanroomFailure(
                    "Cleanroom archive payload is ambiguous.",
                    code="cleanroom_payload_ambiguous",
                )
            payload = payload_roots[0]
            bundle_verification = ReleaseBundleBuilder(
                self.project_root,
                workspace / "unused",
                policy=self.policy,
            ).verify(archive)
            receipts["bundle"] = bundle_verification
            if bundle_verification["source_commit"] != expected_commit:
                raise CleanroomFailure(
                    "Release bundle targets the wrong commit.",
                    code="cleanroom_bundle_revision_mismatch",
                    details={
                        "expected": expected_commit,
                        "actual": bundle_verification["source_commit"],
                    },
                )
            plan = PlatformPlanner(payload).current(offline=offline)
            receipts["platform_plan"] = plan.to_dict()
            if offline:
                lock = PythonLock.load(
                    payload / "requirements.txt",
                    require_hashes=True,
                )
                receipts["offline"] = OfflineArtifactResolver(
                    payload / "release" / "wheelhouse"
                ).resolve(
                    lock,
                    python_tag=f"cp{sys.version_info.major}{sys.version_info.minor}",
                    platform_tags=self._platform_tags(),
                )
            ports = self._allocate_ports(5)
            receipts["ports"] = PortAvailabilityProbe().probe(
                "127.0.0.1",
                ports,
            )
            environment = self._environment(
                workspace=workspace,
                plan=plan,
                ports=ports,
            )
            if run_product_lifecycle:
                environment["ZYRA_BUN_EXECUTABLE"] = self._resolve_bun()
            runner = CommandRunner(environment=environment)
            venv_root = workspace / "venv"
            venv.EnvBuilder(
                with_pip=True,
                clear=True,
                symlinks=False,
            ).create(venv_root)
            python = self._venv_python(venv_root)
            uv = shutil.which("uv")
            if uv:
                dependency_command = [
                    str(Path(uv).resolve()),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--require-hashes",
                    "-r",
                    str(payload / "requirements.txt"),
                ]
                if offline:
                    dependency_command.append("--offline")
            else:
                dependency_command = [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    str(payload / "requirements.txt"),
                ]
                if offline:
                    dependency_command.extend(
                        [
                            "--no-index",
                            "--find-links",
                            str(payload / "release" / "wheelhouse"),
                        ]
                    )
            dependencies = runner.run(
                dependency_command,
                cwd=payload,
                timeout=command_timeout,
            )
            commands.append(dependencies.to_dict())
            wheel_reference = bundle_verification["verified_artifacts"].get(
                "python_wheel"
            )
            if not isinstance(wheel_reference, Mapping):
                raise CleanroomFailure(
                    "Release bundle does not identify its Python wheel.",
                    code="cleanroom_wheel_reference_missing",
                )
            wheel = payload / normalize_relative_path(
                str(wheel_reference["path"])
            )
            if not wheel.is_file():
                raise CleanroomFailure(
                    "Release bundle Python wheel is missing after extraction.",
                    code="cleanroom_wheel_missing",
                    details={"path": str(wheel)},
                )
            install = runner.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    str(wheel),
                ],
                cwd=workspace,
                timeout=command_timeout,
            )
            commands.append(install.to_dict())
            import_probe = runner.run(
                [
                    str(python),
                    "-c",
                    (
                        "from zyra_productization.release import ReleaseRuntime;"
                        "print(ReleaseRuntime.__name__)"
                    ),
                ],
                cwd=workspace,
                timeout=60,
            )
            commands.append(import_probe.to_dict())
            if bundle_verification.get("loopx") is not None:
                loopx_workspace = workspace / "loopx-workspace"
                loopx_runtime = LoopXRuntimeResolver(payload).receipt(
                    loopx_workspace
                )
                loopx_doctor = LoopXDoctor(payload).run(
                    deep=True,
                    workspace_root=loopx_workspace,
                    python_executable=python,
                )
                if loopx_doctor["ready"] is not True:
                    raise CleanroomFailure(
                        "Cleanroom LoopX deep doctor did not pass.",
                        code="cleanroom_loopx_doctor_failed",
                        details={"doctor": loopx_doctor},
                    )
                receipts["loopx"] = {
                    "runtime": loopx_runtime,
                    "doctor": loopx_doctor,
                    "offline": True,
                    "external_source_required": False,
                    "archive_install_required": False,
                }
                release_python_paths = [
                    payload / "apps" / "api",
                    *sorted(
                        path
                        for path in (payload / "packages").iterdir()
                        if path.is_dir()
                    ),
                ]
                first_task = runner.run(
                    [
                        str(python),
                        "-m",
                        "zyra_integrations.loopx.runtime.first_task",
                        "--workspace",
                        str(workspace / "loopx-first-task"),
                    ],
                    cwd=workspace,
                    timeout=command_timeout,
                    extra_environment={
                        "PYTHONPATH": os.pathsep.join(
                            str(path) for path in release_python_paths
                        ),
                    },
                )
                commands.append(first_task.to_dict())
                try:
                    first_task_receipt = json.loads(first_task.stdout)
                except json.JSONDecodeError as error:
                    raise CleanroomFailure(
                        "Cleanroom LoopX first-task receipt is invalid.",
                        code="cleanroom_loopx_first_task_invalid",
                        details={"stdout": first_task.stdout},
                    ) from error
                if first_task_receipt.get("ready") is not True:
                    raise CleanroomFailure(
                        "Cleanroom LoopX first task did not pass.",
                        code="cleanroom_loopx_first_task_failed",
                        details={"receipt": first_task_receipt},
                    )
                release_origins = (
                    first_task_receipt.get("probe_origin"),
                    first_task_receipt.get("api_origin"),
                    (
                        first_task_receipt.get("runtime", {})
                        if isinstance(first_task_receipt.get("runtime"), Mapping)
                        else {}
                    ).get("install_root"),
                )
                try:
                    for origin in release_origins:
                        Path(str(origin)).resolve().relative_to(payload)
                except ValueError as error:
                    raise CleanroomFailure(
                        "Cleanroom LoopX first task escaped the release payload.",
                        code="cleanroom_loopx_first_task_origin_invalid",
                        details={
                            "payload": str(payload),
                            "origins": list(release_origins),
                        },
                    ) from error
                first_task_receipt["release_pythonpath"] = [
                    str(path.relative_to(payload))
                    for path in release_python_paths
                ]
                receipts["loopx"]["first_task"] = first_task_receipt
            if run_product_lifecycle:
                bun = environment["ZYRA_BUN_EXECUTABLE"]
                javascript_commands = (
                    (
                        [
                            bun,
                            "install",
                            "--frozen-lockfile",
                            "--ignore-scripts",
                            *(["--offline"] if offline else []),
                        ],
                        payload,
                    ),
                    ([bun, "run", "typecheck"], payload),
                    (
                        [
                            bun,
                            "build",
                            "apps/code-worker/src/main.ts",
                            "--outdir",
                            "dist/code-worker",
                            "--target",
                            "bun",
                        ],
                        payload,
                    ),
                    (
                        [
                            bun,
                            "build",
                            "apps/code-worker/src/main.ts",
                            "--outdir",
                            "dist/code-worker-node",
                            "--target",
                            "node",
                        ],
                        payload,
                    ),
                    (
                        [
                            bun,
                            "build",
                            "index.html",
                            "--outdir",
                            "dist",
                            "--target",
                            "browser",
                            "--sourcemap=linked",
                        ],
                        payload / "apps" / "web",
                    ),
                )
                for command, command_root in javascript_commands:
                    receipt = runner.run(
                        command,
                        cwd=command_root,
                        timeout=command_timeout,
                    )
                    commands.append(receipt.to_dict())
            install_root = workspace / "installed"
            install_store = InstallReceiptStore(
                workspace / "state" / "install.sqlite3"
            )
            installer = ReleaseInstaller(
                install_store,
                migrations=MigrationExecutor(
                    default_migration_registry(),
                    install_store,
                ),
            )
            install_receipt = installer.install(
                archive,
                install_root=install_root,
                idempotency_key=f"cleanroom-{expected_commit}",
                release_id=bundle_verification["release_id"],
                manifest_digest=bundle_verification["manifest_digest"],
                target_migration_version=1,
            )
            receipts["install"] = install_receipt.to_dict()
            if run_product_lifecycle:
                deployment_root = payload
                lifecycle = ProcessLifecycle(
                    workspace / "processes",
                    environment=environment,
                )
                lifecycle_receipts = self._exercise_product_lifecycle(
                    runner,
                    payload=deployment_root,
                    python=python,
                    timeout=command_timeout,
                )
                receipts["lifecycle"] = lifecycle_receipts
            uninstall_receipt = installer.uninstall(
                install_receipt.transaction_id,
                purge_state=False,
            )
            receipts["uninstall"] = uninstall_receipt.to_dict()
            if install_receipt.state.value != "committed":
                raise CleanroomFailure(
                    "Cleanroom installation did not commit.",
                    code="cleanroom_install_not_committed",
                )
            if uninstall_receipt.state.value != "uninstalled":
                raise CleanroomFailure(
                    "Cleanroom uninstall did not complete.",
                    code="cleanroom_uninstall_incomplete",
                )
            result = {
                "schema": "zyra.clean-install-receipt/v1",
                "ready": True,
                "source_commit": expected_commit,
                "archive": str(archive.resolve()),
                "archive_sha256": sha256_file(archive),
                "platform": platform.system().lower(),
                "architecture": platform.machine().lower(),
                "offline": offline,
                "product_lifecycle_exercised": run_product_lifecycle,
                "workspace_isolated": True,
                "parent_source_repositories_present": False,
                "commands": commands,
                "receipts": receipts,
                "started_at": started_at,
                "finished_at": utc_now(),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
            result["digest"] = stable_digest(result)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            return result
        except BaseException as error:
            if isinstance(error, ReleaseError):
                error.details.setdefault(
                    "cleanroom_log_tails",
                    self._diagnostic_log_tails(workspace),
                )
            raise
        finally:
            if lifecycle is not None:
                lifecycle.stop_all()
                lifecycle.assert_stopped()
            if install_store is not None:
                install_store.close()
            shutil.rmtree(workspace, ignore_errors=True)

    def _diagnostic_log_tails(self, workspace: Path) -> dict[str, str]:
        secrets = [
            value
            for name, value in os.environ.items()
            if value and self.policy.secret_name(name)
        ]
        logs: dict[str, str] = {}
        candidates = {
            *workspace.rglob("*.log"),
            *workspace.rglob("*.jsonl"),
        }
        for path in sorted(candidates)[:48]:
            try:
                content = path.read_bytes()[-16 * 1024 :].decode(
                    "utf-8",
                    errors="replace",
                )
            except OSError:
                continue
            for secret in secrets:
                content = content.replace(secret, "<redacted>")
            logs[path.relative_to(workspace).as_posix()] = content
        return logs

    @staticmethod
    def _environment(
        *,
        workspace: Path,
        plan: PlatformPlan,
        ports: Sequence[int],
    ) -> dict[str, str]:
        allowed = {
            name: value
            for name, value in os.environ.items()
            if name
            in {
                "PATH",
                "SystemRoot",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "TEMP",
                "TMP",
                "LANG",
                "LC_ALL",
                "TZ",
            }
        }
        allowed.update(plan.environment)
        bun_cache = (workspace / "cache" / "bun").resolve()
        pip_cache = (workspace / "cache" / "pip").resolve()
        uv_cache = (workspace / "cache" / "uv").resolve()
        # Bun may materialize platform executables in its cache.  Keep that
        # cache outside the extracted release payload so lifecycle doctor and
        # submission-boundary scans observe only deliverable files.
        allowed["BUN_INSTALL_CACHE_DIR"] = str(bun_cache)
        allowed["PIP_CACHE_DIR"] = str(pip_cache)
        allowed["UV_CACHE_DIR"] = str(uv_cache)
        if os.name == "nt":
            system_root = (
                os.environ.get("SystemRoot")
                or os.environ.get("SYSTEMROOT")
                or os.environ.get("windir")
                or os.environ.get("WINDIR")
            )
            if system_root:
                allowed["SystemRoot"] = system_root
                allowed["WINDIR"] = system_root
        allowed.update(
            {
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
                "ZYRA_RELEASE_CLEANROOM": "1",
                "ZYRA_STATE_ROOT": str(workspace / "product-state"),
                "ZYRA_API_PORT": str(ports[0]),
                "ZYRA_WEB_PORT": str(ports[1]),
                "ZYRA_DEVICE_PORT": str(ports[2]),
                "ZYRA_EDGE_PORT": str(ports[3]),
                "ZYRA_CLOUD_PORT": str(ports[4]),
            }
        )
        return allowed

    @staticmethod
    def _allocate_ports(count: int) -> list[int]:
        sockets: list[socket.socket] = []
        ports: list[int] = []
        try:
            for _ in range(count):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.bind(("127.0.0.1", 0))
                sockets.append(probe)
                ports.append(int(probe.getsockname()[1]))
        finally:
            for probe in sockets:
                probe.close()
        return ports

    @staticmethod
    def _venv_python(root: Path) -> Path:
        return (
            root / "Scripts" / "python.exe"
            if os.name == "nt"
            else root / "bin" / "python"
        )

    @staticmethod
    def _platform_tags() -> tuple[str, ...]:
        system = platform.system().casefold()
        machine = platform.machine().casefold()
        tags = {"any"}
        if system == "windows":
            tags.add("win_amd64" if machine in {"amd64", "x86_64"} else "win_arm64")
        elif system == "linux":
            if machine in {"amd64", "x86_64"}:
                tags.update({"manylinux_2_17_x86_64", "manylinux2014_x86_64", "linux_x86_64"})
            elif machine in {"arm64", "aarch64"}:
                tags.update({"manylinux_2_17_aarch64", "manylinux2014_aarch64", "linux_aarch64"})
        return tuple(sorted(tags))

    def _resolve_bun(self) -> str:
        global_bun = shutil.which("bun")
        if global_bun:
            return str(Path(global_bun).resolve())
        binary_name = "bun.exe" if os.name == "nt" else "bun"
        candidates = sorted(
            self.project_root.glob(
                f"node_modules/@oven/bun-*/bin/{binary_name}"
            )
        )
        if candidates:
            return str(candidates[0].resolve())
        launcher = self.project_root / "node_modules" / ".bin" / binary_name
        if launcher.is_file():
            return str(launcher.resolve())
        raise CleanroomFailure(
            "Bun is required to install and build the locked web workspace.",
            code="cleanroom_bun_missing",
        )

    @staticmethod
    def _exercise_product_lifecycle(
        runner: CommandRunner,
        *,
        payload: Path,
        python: Path,
        timeout: float,
    ) -> dict[str, Any]:
        state_root = payload / ".release-cleanroom-state"
        launcher = [
            str(python),
            "-m",
            "zyra_productization.release.cli",
            "--project-root",
            str(payload),
            "--state-root",
            str(state_root),
        ]
        commands = [
            (
                "submission-boundary",
                [str(python), "scripts/verify_submission_boundary.py"],
            ),
            ("release-doctor", [*launcher, "doctor"]),
            (
                "product-start",
                [*launcher, "lifecycle", "start", "--no-build-web"],
            ),
            (
                "semantic-health",
                [*launcher, "lifecycle", "health"],
            ),
            ("product-stop", [*launcher, "lifecycle", "stop"]),
        ]
        receipts: list[dict[str, Any]] = []
        try:
            for name, command in commands:
                result = runner.run(
                    command,
                    cwd=payload,
                    timeout=timeout,
                )
                value = result.to_dict()
                value["name"] = name
                receipts.append(value)
        finally:
            if not receipts or receipts[-1].get("name") != "product-stop":
                emergency = runner.run(
                    [*launcher, "lifecycle", "stop"],
                    cwd=payload,
                    timeout=min(timeout, 120),
                    check=False,
                ).to_dict()
                emergency["name"] = "product-stop-emergency"
                receipts.append(emergency)
        return {
            "ready": all(item["ready"] for item in receipts),
            "commands": receipts,
        }


__all__ = [
    "CleanInstallRunner",
    "CommandResult",
    "CommandRunner",
    "OfflineArtifactResolver",
    "PlatformPlanner",
    "PortAvailabilityProbe",
    "ProcessLifecycle",
]
