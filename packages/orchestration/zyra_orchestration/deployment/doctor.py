from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from .clean_state import CleanStateManager
from .errors import DeploymentError, DoctorBlocked, redact
from .models import ProbeResult, ProbeStatus, digest, now_iso
from .ports import PortInspector
from .process_manager import DeploymentProcessManager
from .profiles import ProfileCatalog
from .state_store import DeploymentStateStore


_SOURCE_PATH_PATTERN = re.compile(
    r"(?i)(?:\.\.[\\/])+(?:opencode|openhands|browser-use|claude-code-best|"
    r"oh-my-pi|langgraph|agentscope|agent-framework|hermes-agent|openclaw)"
)
_EDITABLE_PATTERN = re.compile(r"(?i)(?:file:|path\s*=|editable).*(?:\.\.[\\/])")
_DYNAMIC_IMPORT_PATTERN = re.compile(
    r"\b(?:importlib\.import_module|__import__|require\s*\(|Bun\.plugin)\b"
)
_SUBPROCESS_PATTERN = re.compile(
    r"\b(?:subprocess\.(?:Popen|run|call)|child_process|Bun\.spawn)\b"
)
_PORT_PATTERN = re.compile(
    r"\b(?:listen|serve_forever|ThreadingHTTPServer|HTTPServer|createServer)\b"
)
_NATIVE_SUFFIXES = {".dll", ".exe", ".node", ".so", ".dylib", ".pyd"}


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    check_id: str
    required: bool
    run: Callable[[], Mapping[str, Any]]


class DeploymentDoctor:
    def __init__(
        self,
        *,
        project_root: Path | str,
        catalog: ProfileCatalog,
        store: DeploymentStateStore,
        process_manager: DeploymentProcessManager,
        clean_state: CleanStateManager,
        port_inspector: PortInspector | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.catalog = catalog
        self.store = store
        self.process_manager = process_manager
        self.clean_state = clean_state
        self.ports = port_inspector or PortInspector()
        self.environment = dict(os.environ if environment is None else environment)

    def checks(self) -> tuple[DoctorCheck, ...]:
        return (
            DoctorCheck("runtime", True, self.check_runtime),
            DoctorCheck("dependencies", True, self.check_dependencies),
            DoctorCheck("configuration", True, self.check_configuration),
            DoctorCheck("state-store", True, self.check_store),
            DoctorCheck("ports", True, self.check_ports),
            DoctorCheck("credentials", True, self.check_credentials),
            DoctorCheck("web-bundle", True, self.check_web_bundle),
            DoctorCheck("source-boundary", True, self.check_source_boundary),
            DoctorCheck("process-boundary", True, self.check_process_boundary),
            DoctorCheck("native-artifacts", True, self.check_native_artifacts),
            DoctorCheck("clean-state", True, self.check_clean_state),
            DoctorCheck("partial-startup", True, self.check_partial_startup),
        )

    def run(
        self,
        *,
        selected: Sequence[str] = (),
        fail_fast: bool = False,
    ) -> dict[str, Any]:
        selected_ids = set(selected)
        known = {check.check_id for check in self.checks()}
        unknown = selected_ids - known
        if unknown:
            raise DoctorBlocked(
                "deployment_doctor_check_unknown",
                "unknown deployment doctor check requested",
                operation="doctor",
                details={"unknown": sorted(unknown)},
            )
        results: list[ProbeResult] = []
        for check in self.checks():
            if selected_ids and check.check_id not in selected_ids:
                continue
            result = self._run_check(check)
            results.append(result)
            if fail_fast and result.status is ProbeStatus.BLOCKED:
                break
        blockers = [
            f"{item.probe_id}:{blocker}"
            for item in results
            for blocker in item.blockers
        ]
        warnings = [
            f"{item.probe_id}:{warning}"
            for item in results
            for warning in item.warnings
        ]
        ready = not blockers and all(
            item.status in {ProbeStatus.READY, ProbeStatus.DEGRADED}
            for item in results
        )
        status = (
            ProbeStatus.READY
            if ready and not warnings
            else ProbeStatus.DEGRADED
            if ready
            else ProbeStatus.BLOCKED
        )
        report = {
            "schema": "zyra.deployment-doctor-report/v1",
            "ready": ready,
            "status": status.value,
            "checks": [item.to_dict() for item in results],
            "blockers": blockers,
            "warnings": warnings,
            "project_root": str(self.project_root),
            "profile_digest": self.catalog.profile_digest,
            "created_at": now_iso(),
            "fallback": False,
        }
        report["report_digest"] = digest(report)
        self.store.append_event(
            "deployment.doctor_completed",
            {
                "ready": ready,
                "status": status.value,
                "blockers": blockers,
                "warnings": warnings,
                "report_digest": report["report_digest"],
            },
        )
        return report

    def _run_check(self, check: DoctorCheck) -> ProbeResult:
        started_at = now_iso()
        started = time.monotonic()
        try:
            observation = dict(check.run())
            ready = observation.get("ready") is True
            blockers = tuple(str(item) for item in observation.get("blockers") or ())
            warnings = tuple(str(item) for item in observation.get("warnings") or ())
            status = (
                ProbeStatus.READY
                if ready and not warnings
                else ProbeStatus.DEGRADED
                if ready
                else ProbeStatus.BLOCKED
            )
            summary = str(
                observation.get("summary")
                or ("ready" if ready else "blocked")
            )
        except DeploymentError as error:
            observation = error.to_dict()
            blockers = (error.code,) if check.required else ()
            warnings = () if check.required else (error.code,)
            status = ProbeStatus.BLOCKED if check.required else ProbeStatus.DEGRADED
            summary = str(error)
        except BaseException as error:
            observation = {
                "error": type(error).__name__,
                "message": str(error),
                "fallback": False,
            }
            blockers = (
                f"{check.check_id}_exception",
            ) if check.required else ()
            warnings = () if check.required else (
                f"{check.check_id}_exception",
            )
            status = ProbeStatus.BLOCKED if check.required else ProbeStatus.DEGRADED
            summary = f"{type(error).__name__}: {error}"
        return ProbeResult(
            probe_id=f"doctor:{check.check_id}",
            status=status,
            summary=summary,
            started_at=started_at,
            completed_at=now_iso(),
            latency_ms=round((time.monotonic() - started) * 1000),
            blockers=blockers,
            warnings=warnings,
            observations=redact(observation),
        )

    def check_runtime(self) -> dict[str, Any]:
        python_ok = sys.version_info >= (3, 12)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(self.project_root))
        blockers: list[str] = []
        warnings: list[str] = []
        if not python_ok:
            blockers.append("python_version_unsupported")
        if memory.available < 512 * 1024 * 1024:
            blockers.append("system_memory_too_low")
        elif memory.available < 1024 * 1024 * 1024:
            warnings.append("system_memory_constrained")
        if disk.free < 1024 * 1024 * 1024:
            blockers.append("project_disk_space_low")
        return {
            "ready": not blockers,
            "summary": "host runtime and resource capacity",
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": psutil.cpu_count(logical=True),
            "available_memory_mb": round(memory.available / 1024 / 1024, 3),
            "disk_free_mb": round(disk.free / 1024 / 1024, 3),
            "blockers": blockers,
            "warnings": warnings,
        }

    def check_dependencies(self) -> dict[str, Any]:
        python_modules = {
            "psutil": "required",
            "zyra_core": "required",
            "zyra_runtime": "required",
            "zyra_scheduler": "required",
            "zyra_workers": "required",
            "zyra_workspace": "required",
            "zyra_memory": "required",
            "zyra_skills": "required",
            "zyra_orchestration": "required",
        }
        modules = {
            name: importlib.util.find_spec(name) is not None
            for name in python_modules
        }
        local_bun = self.project_root / "node_modules" / ".bin" / (
            "bun.exe" if os.name == "nt" else "bun"
        )
        executables = {
            "git": shutil.which("git"),
            "bun": shutil.which("bun") or (
                str(local_bun) if local_bun.is_file() else None
            ),
            "node": shutil.which("node"),
        }
        blockers = [
            f"python_module_missing:{name}"
            for name, ready in modules.items()
            if not ready
        ]
        blockers.extend(
            f"executable_missing:{name}"
            for name, path in executables.items()
            if not path
        )
        lockfiles = {
            "bun.lock": (self.project_root / "bun.lock").is_file(),
            "python_lock": any(
                (self.project_root / name).is_file()
                for name in ("requirements.lock", "uv.lock", "poetry.lock")
            ),
        }
        warnings = [] if lockfiles["python_lock"] else ["python_lockfile_missing"]
        return {
            "ready": not blockers,
            "summary": "declared multi-language dependencies",
            "python_modules": modules,
            "executables": {
                name: bool(path) for name, path in executables.items()
            },
            "lockfiles": lockfiles,
            "blockers": blockers,
            "warnings": warnings,
        }

    def check_configuration(self) -> dict[str, Any]:
        projection = self.catalog.public_projection()
        profiles = projection["profiles"]
        ports = [int(item["port"]) for item in profiles]
        hosts = [str(item["host"]) for item in profiles]
        blockers: list[str] = []
        if len(set(ports)) != len(ports):
            blockers.append("profile_ports_not_unique")
        if len(profiles) != 3:
            blockers.append("required_profile_count_invalid")
        if any(not host for host in hosts):
            blockers.append("profile_host_missing")
        return {
            "ready": not blockers,
            "summary": "deployment profile configuration",
            "projection": projection,
            "blockers": blockers,
            "warnings": [],
        }

    def check_store(self) -> dict[str, Any]:
        integrity = self.store.integrity_report()
        return {
            "ready": integrity.get("ready") is True,
            "summary": "deployment state store integrity and schema migration",
            "integrity": integrity,
            "blockers": (
                []
                if integrity.get("ready") is True
                else ["deployment_store_integrity_failed"]
            ),
            "warnings": [],
        }

    def check_ports(self) -> dict[str, Any]:
        observations: list[dict[str, Any]] = []
        blockers: list[str] = []
        active = {
            (record.endpoint, record.pid)
            for record in self.process_manager.statuses()
            if record.status.value in {"starting", "ready", "degraded"}
        }
        active_endpoints = {endpoint for endpoint, _pid in active}
        endpoints = [
            (
                policy.profile.value,
                policy.host,
                policy.port,
                f"http://{policy.host}:{policy.port}",
            )
            for policy in self.catalog.policies()
        ]
        endpoints.append(
            (
                "api",
                str(self.environment.get("ZYRA_API_HOST") or "127.0.0.1"),
                int(self.environment.get("ZYRA_API_PORT") or 8000),
                f"http://{self.environment.get('ZYRA_API_HOST') or '127.0.0.1'}:"
                f"{int(self.environment.get('ZYRA_API_PORT') or 8000)}",
            ),
        )
        endpoints.append(
            (
                "web",
                str(self.environment.get("ZYRA_WEB_HOST") or "127.0.0.1"),
                int(self.environment.get("ZYRA_WEB_PORT") or 5173),
                f"http://{self.environment.get('ZYRA_WEB_HOST') or '127.0.0.1'}:"
                f"{int(self.environment.get('ZYRA_WEB_PORT') or 5173)}",
            ),
        )
        seen: set[tuple[str, int]] = set()
        for component, host, port, endpoint in endpoints:
            key = (host, port)
            if key in seen:
                blockers.append(f"configured_port_reused:{component}")
                continue
            seen.add(key)
            observed = self.ports.observe(host, port)
            owned = endpoint in active_endpoints
            observations.append(
                {
                    "component": component,
                    **observed.to_dict(),
                    "owned_by_supervisor": owned,
                }
            )
            if not observed.available and not owned:
                blockers.append(f"port_conflict:{component}")
        return {
            "ready": not blockers,
            "summary": "configured ports are available or supervisor-owned",
            "observations": observations,
            "blockers": blockers,
            "warnings": [],
        }

    def check_credentials(self) -> dict[str, Any]:
        profiles: dict[str, Any] = {}
        blockers: list[str] = []
        warnings: list[str] = []
        for policy in self.catalog.policies():
            presence = self.catalog.credential_presence(policy.profile)
            ready = (
                any(presence.values())
                if policy.credential_environment
                else True
            )
            profiles[policy.profile.value] = {
                "declared": list(policy.credential_environment),
                "presence": presence,
                "values_exposed": False,
                "ready": ready,
            }
            if policy.profile.value == "cloud" and not ready:
                warnings.append("cloud_provider_credential_missing")
        # Missing cloud credentials produce a degraded deployment and reject
        # provider-required cloud placement, but do not prevent device/edge
        # product startup.
        return {
            "ready": not blockers,
            "summary": "credential handles and fail-closed provider admission",
            "profiles": profiles,
            "blockers": blockers,
            "warnings": warnings,
        }

    def check_web_bundle(self) -> dict[str, Any]:
        index = self.project_root / "apps" / "web" / "dist" / "index.html"
        package = self.project_root / "apps" / "web" / "package.json"
        ready = index.is_file() and package.is_file()
        return {
            "ready": ready,
            "summary": "Web production bundle availability",
            "index": str(index),
            "index_exists": index.is_file(),
            "package_exists": package.is_file(),
            "blockers": [] if ready else ["web_production_bundle_missing"],
            "warnings": [],
        }

    def check_source_boundary(self) -> dict[str, Any]:
        candidates = [
            self.project_root / "pyproject.toml",
            self.project_root / "package.json",
            self.project_root / "bun.lock",
            *(
                path
                for path in self.project_root.rglob("package.json")
                if not any(
                    part in {"node_modules", "vendor", "vendor-runtimes", "third_party"}
                    for part in path.parts
                )
            ),
        ]
        findings: list[dict[str, Any]] = []
        for path in dict.fromkeys(candidates):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if _SOURCE_PATH_PATTERN.search(line) or _EDITABLE_PATTERN.search(line):
                    findings.append(
                        {
                            "path": path.relative_to(self.project_root).as_posix(),
                            "line": line_number,
                            "digest": digest(line),
                            "text_exposed": False,
                        }
                    )
        ready = not findings
        return {
            "ready": ready,
            "summary": "root source repositories are absent from product dependencies",
            "findings": findings,
            "scanned_files": len(candidates),
            "blockers": [] if ready else ["root_source_dependency_detected"],
            "warnings": [],
        }

    def check_process_boundary(self) -> dict[str, Any]:
        report = self.process_manager.process_report()
        source_processes: list[dict[str, Any]] = []
        dynamic_imports: list[dict[str, Any]] = []
        subprocess_sites: list[dict[str, Any]] = []
        port_sites: list[dict[str, Any]] = []
        source_names = {
            "opencode",
            "openhands",
            "browser-use",
            "claude-code-best",
            "oh-my-pi",
            "openclaw",
        }
        for process in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
            try:
                command = " ".join(process.info.get("cmdline") or ())
                cwd = str(process.info.get("cwd") or "")
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            lowered = f"{command} {cwd}".casefold()
            if any(
                re.search(rf"(?:^|[\\/]){re.escape(name)}(?:[\\/]|$)", lowered)
                for name in source_names
            ):
                source_processes.append(
                    {
                        "pid": int(process.info["pid"]),
                        "name": str(process.info.get("name") or ""),
                        "command_digest": digest(command),
                        "cwd_digest": digest(cwd),
                        "raw_values_exposed": False,
                    }
                )
        scan_roots = [
            self.project_root / "apps",
            self.project_root / "packages",
            self.project_root / "scripts",
        ]
        for root in scan_roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.casefold() not in {
                    ".py",
                    ".ts",
                    ".tsx",
                    ".js",
                    ".mjs",
                }:
                    continue
                if any(part in {"node_modules", "vendor", "vendor-runtimes"} for part in path.parts):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                relative = path.relative_to(self.project_root).as_posix()
                for pattern, target in (
                    (_DYNAMIC_IMPORT_PATTERN, dynamic_imports),
                    (_SUBPROCESS_PATTERN, subprocess_sites),
                    (_PORT_PATTERN, port_sites),
                ):
                    for match in pattern.finditer(text):
                        target.append(
                            {
                                "path": relative,
                                "line": text.count("\n", 0, match.start()) + 1,
                                "token": match.group(0),
                            }
                        )
        blockers = (
            ["root_source_process_detected"]
            if source_processes
            else []
        )
        return {
            "ready": not blockers,
            "summary": "runtime process, dynamic import, subprocess and port boundary",
            "managed": report,
            "root_source_processes": source_processes,
            "dynamic_import_sites": dynamic_imports,
            "subprocess_sites": subprocess_sites,
            "port_service_sites": port_sites,
            "blockers": blockers,
            "warnings": [],
        }

    def check_native_artifacts(self) -> dict[str, Any]:
        declared_roots = {
            (self.project_root / "node_modules").resolve(),
            (self.project_root / ".venv").resolve(),
        }
        findings: list[dict[str, Any]] = []
        for root in (
            self.project_root / "apps",
            self.project_root / "packages",
            self.project_root / "scripts",
            self.project_root / "dist",
        ):
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.casefold() not in _NATIVE_SUFFIXES:
                    continue
                resolved = path.resolve()
                declared = any(
                    resolved == declared_root or declared_root in resolved.parents
                    for declared_root in declared_roots
                )
                findings.append(
                    {
                        "path": path.relative_to(self.project_root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": self._sha256(path),
                        "declared_dependency_root": declared,
                    }
                )
        blockers = [
            f"undeclared_native_artifact:{item['path']}"
            for item in findings
            if not item["declared_dependency_root"]
        ]
        return {
            "ready": not blockers,
            "summary": "native and binary artifact declaration",
            "artifacts": findings,
            "blockers": blockers,
            "warnings": [],
        }

    def check_clean_state(self) -> dict[str, Any]:
        run_root = self.clean_state.create_run_root(prefix="doctor")
        try:
            reset = self.clean_state.reset_run_root(run_root)
            fingerprint = self.clean_state.fingerprint(run_root)
            ready = reset.get("fresh") is True and fingerprint.symlink_count == 0
            return {
                "ready": ready,
                "summary": "fresh deployment state can be created without cache residue",
                "reset": reset,
                "fingerprint": fingerprint.to_dict(),
                "blockers": [] if ready else ["clean_state_not_fresh"],
                "warnings": [],
            }
        finally:
            self.clean_state.remove_run_root(run_root)

    def check_partial_startup(self) -> dict[str, Any]:
        statuses = self.process_manager.statuses()
        active = {
            record.component_id: record.status.value
            for record in statuses
            if record.status.value not in {"stopped", "crashed"}
        }
        expected = {
            "api",
            "web",
            "profile:device",
            "profile:edge",
            "profile:cloud",
        }
        missing = sorted(expected - set(active))
        partial = bool(active) and bool(missing)
        blockers = ["deployment_partial_startup"] if partial else []
        return {
            "ready": not partial,
            "summary": "deployment process set is fully stopped or complete",
            "active": active,
            "expected": sorted(expected),
            "missing": missing,
            "partial": partial,
            "blockers": blockers,
            "warnings": [],
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        import hashlib

        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()


__all__ = [
    "DeploymentDoctor",
    "DoctorCheck",
]
