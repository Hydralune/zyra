from __future__ import annotations

import base64
import hashlib
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO, Callable

import psutil

from .errors import PortConflict, ProcessUnavailable, StateConflict
from .models import (
    DeploymentProfile,
    LifecycleStatus,
    ProcessRecord,
    ProfilePolicy,
    canonical_json,
    digest,
    new_id,
    now_iso,
)
from .node_client import DeploymentNodeClient
from .ports import PortInspector
from .resource_control import ResourceController
from .security import derive_node_secret
from .state_store import DeploymentStateStore


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    component_id: str
    command: tuple[str, ...]
    cwd: Path
    endpoint: str
    host: str
    port: int
    environment: Mapping[str, str]
    configuration_digest: str
    profile: str = ""
    readiness: Callable[[], Mapping[str, Any]] | None = None
    readiness_timeout_seconds: float = 30.0
    shutdown: Callable[[], Mapping[str, Any]] | None = None
    required: bool = True
    resource_policy: ProfilePolicy | None = None

    @property
    def command_digest(self) -> str:
        return digest(
            {
                "command": list(self.command),
                "cwd": str(self.cwd.resolve()),
                "endpoint": self.endpoint,
                "environment_names": sorted(self.environment),
                "configuration_digest": self.configuration_digest,
            }
        )


@dataclass(slots=True)
class ManagedProcess:
    spec: ProcessSpec
    process: subprocess.Popen[bytes]
    record: ProcessRecord
    log_stream: BinaryIO


class SupervisorSecretStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def load_or_create(self) -> bytes:
        with self._lock:
            if self.path.is_file():
                value = self.path.read_bytes()
                if len(value) != 64:
                    raise StateConflict(
                        "deployment_supervisor_secret_invalid",
                        "deployment supervisor secret has an invalid length",
                        operation="load_supervisor_secret",
                    )
                return value
            value = secrets.token_bytes(64)
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("xb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            temporary.replace(self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            return value

    def permission_report(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "exists": False,
                "path": str(self.path),
                "ready": False,
                "reason": "supervisor-secret-missing",
            }
        stat = self.path.stat()
        mode = stat.st_mode & 0o777
        secure = os.name == "nt" or mode & 0o077 == 0
        return {
            "exists": True,
            "path": str(self.path),
            "ready": secure and stat.st_size == 64,
            "bytes": stat.st_size,
            "mode": oct(mode),
            "platform_acl_expected": os.name == "nt",
            "value_exposed": False,
        }


class DeploymentProcessManager:
    def __init__(
        self,
        *,
        project_root: Path | str,
        state_root: Path | str,
        store: DeploymentStateStore,
        port_inspector: PortInspector | None = None,
        resource_controller: ResourceController | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.log_root = self.state_root / "logs"
        self.log_root.mkdir(parents=True, exist_ok=True)
        self.node_root = self.state_root / "nodes"
        self.node_root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.environment = dict(os.environ)
        if environment is not None:
            self.environment.update(
                {str(key): str(value) for key, value in environment.items()}
            )
        self.ports = port_inspector or PortInspector()
        self.resources = resource_controller or ResourceController()
        self.secrets = SupervisorSecretStore(self.state_root / "supervisor.key")
        self._lock = threading.RLock()
        self._managed: dict[str, ManagedProcess] = {}
        self._node_secrets: dict[DeploymentProfile, bytes] = {}
        self.reconcile()

    def _creation_flags(self) -> int:
        if os.name == "nt":
            return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return 0

    def _base_environment(self) -> dict[str, str]:
        environment = dict(self.environment)
        environment.update(
            {
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "ZYRA_DEPLOYMENT_SUPERVISED": "1",
            }
        )
        return environment

    def node_spec(
        self,
        policy: ProfilePolicy,
    ) -> tuple[ProcessSpec, DeploymentNodeClient]:
        supervisor = self.secrets.load_or_create()
        generation_id = new_id("generation")
        node_id = f"zyra-{policy.profile.value}-{new_id('node').split('_', 1)[1]}"
        secret = derive_node_secret(
            supervisor,
            node_id=node_id,
            generation_id=generation_id,
        )
        self._node_secrets[policy.profile] = secret
        public_policy = policy.public_dict(
            credential_presence={
                name: bool(str(self.environment.get(name) or "").strip())
                for name in policy.credential_environment
            }
        )
        public_policy.pop("credential_presence", None)
        node_data_root = self.node_root / policy.profile.value
        command = (
            sys.executable,
            "-m",
            "zyra_orchestration.deployment.node_server",
            "--node-id",
            node_id,
            "--generation-id",
            generation_id,
            "--data-root",
            str(node_data_root),
            "--policy-json",
            canonical_json(public_policy).decode("utf-8"),
        )
        environment = {
            "ZYRA_DEPLOY_NODE_SECRET_B64": base64.b64encode(secret).decode("ascii"),
            **{
                name: str(self.environment.get(name) or "")
                for name in policy.credential_environment
                if str(self.environment.get(name) or "").strip()
            },
        }
        endpoint = f"http://{policy.host}:{policy.port}"
        client = DeploymentNodeClient(
            endpoint,
            secret,
            timeout_seconds=max(2.0, policy.latency_budget_ms / 1000 + 2.0),
            expected_node_id=node_id,
            expected_generation_id=generation_id,
            expected_profile=policy.profile,
        )
        spec = ProcessSpec(
            component_id=f"profile:{policy.profile.value}",
            command=command,
            cwd=self.project_root,
            endpoint=endpoint,
            host=policy.host,
            port=policy.port,
            environment=environment,
            configuration_digest=policy.configuration_digest,
            profile=policy.profile.value,
            readiness=client.health,
            readiness_timeout_seconds=30.0,
            shutdown=client.shutdown,
            required=policy.required,
            resource_policy=policy,
        )
        return spec, client

    def save_node_identity(
        self,
        profile: DeploymentProfile,
        *,
        node_id: str,
        generation_id: str,
    ) -> None:
        self.store.write_meta(
            f"node_identity:{profile.value}",
            {
                "node_id": node_id,
                "generation_id": generation_id,
                "profile": profile.value,
                "updated_at": now_iso(),
            },
        )

    def recovered_node_client(
        self,
        profile: DeploymentProfile,
    ) -> DeploymentNodeClient:
        process_item = self.store.process(f"profile:{profile.value}")
        identity = self.store.read_meta(f"node_identity:{profile.value}", {})
        if process_item is None or not isinstance(identity, Mapping):
            raise ProcessUnavailable(
                "deployment_node_state_missing",
                "deployment node process state is missing",
                operation="recover_node_client",
                profile=profile.value,
            )
        record, _revision = process_item
        node_id = str(identity.get("node_id") or "")
        generation_id = str(identity.get("generation_id") or "")
        if not node_id or generation_id != record.generation_id:
            raise StateConflict(
                "deployment_node_identity_state_mismatch",
                "deployment node identity does not match process generation",
                operation="recover_node_client",
                profile=profile.value,
            )
        secret = derive_node_secret(
            self.secrets.load_or_create(),
            node_id=node_id,
            generation_id=generation_id,
        )
        self._node_secrets[profile] = secret
        return DeploymentNodeClient(
            record.endpoint,
            secret,
            expected_node_id=node_id,
            expected_generation_id=generation_id,
            expected_profile=profile,
        )

    def start_node(
        self,
        policy: ProfilePolicy,
        *,
        restart: bool = False,
    ) -> tuple[ProcessRecord, DeploymentNodeClient, Mapping[str, Any]]:
        component_id = f"profile:{policy.profile.value}"
        with self._lock:
            existing = self.status(component_id)
            if existing is not None and existing.status in {
                LifecycleStatus.READY,
                LifecycleStatus.DEGRADED,
                LifecycleStatus.STARTING,
            }:
                if not restart:
                    client = self.recovered_node_client(policy.profile)
                    return existing, client, client.health()
                self.stop(component_id)
            elif existing is not None and existing.status is not LifecycleStatus.STOPPED:
                # Reap crashed or blocked generations before replacing their
                # component slot. This closes the inherited log handle and
                # prevents a stale managed object from being overwritten.
                self.stop(component_id, tolerate_missing=True)
            spec, client = self.node_spec(policy)
            node_id = client.expected_node_id
            generation_id = client.expected_generation_id
            record = self.start(spec, generation_id=generation_id)
            self.save_node_identity(
                policy.profile,
                node_id=node_id,
                generation_id=generation_id,
            )
            try:
                health = client.wait_ready(
                    timeout_seconds=spec.readiness_timeout_seconds
                )
            except BaseException:
                self._mark_failed(component_id)
                self.stop(component_id, tolerate_missing=True)
                raise
            updated = replace(
                record,
                status=(
                    LifecycleStatus.READY
                    if health.get("status") == "ready"
                    else LifecycleStatus.DEGRADED
                ),
                observed_at=now_iso(),
            )
            current = self.store.process(component_id)
            self.store.save_process(
                updated,
                expected_revision=current[1] if current else None,
            )
            managed = self._managed.get(component_id)
            if managed is not None:
                managed.record = updated
            return updated, client, health

    def start(
        self,
        spec: ProcessSpec,
        *,
        generation_id: str | None = None,
    ) -> ProcessRecord:
        if not spec.command:
            raise ValueError("managed process command cannot be empty")
        if not spec.cwd.is_dir():
            raise ProcessUnavailable(
                "deployment_process_cwd_missing",
                "managed process working directory does not exist",
                operation="start",
                details={"component_id": spec.component_id, "cwd": str(spec.cwd)},
            )
        with self._lock:
            existing = self.status(spec.component_id)
            if existing is not None and existing.status in {
                LifecycleStatus.STARTING,
                LifecycleStatus.READY,
                LifecycleStatus.DEGRADED,
            }:
                if (
                    existing.command_digest == spec.command_digest
                    and existing.configuration_digest == spec.configuration_digest
                ):
                    return existing
                raise StateConflict(
                    "deployment_process_configuration_drift",
                    "a running component has a different command or configuration",
                    operation="start",
                    profile=spec.profile,
                    details={"component_id": spec.component_id},
                )
            self.ports.require_available(spec.host, spec.port)
            log_path = self.log_root / f"{spec.component_id.replace(':', '-')}.log"
            log_stream = log_path.open("ab", buffering=0)
            environment = self._base_environment()
            environment.update({str(key): str(value) for key, value in spec.environment.items()})
            started_at = now_iso()
            try:
                process = subprocess.Popen(
                    list(spec.command),
                    cwd=spec.cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    creationflags=self._creation_flags(),
                )
            except BaseException:
                log_stream.close()
                raise
            try:
                create_time = psutil.Process(process.pid).create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                create_time = 0.0
            prior = self.store.process(spec.component_id)
            restart_count = (prior[0].restart_count + 1) if prior else 0
            record = ProcessRecord(
                component_id=spec.component_id,
                profile=spec.profile,
                pid=process.pid,
                generation_id=generation_id or new_id("generation"),
                command_digest=spec.command_digest,
                endpoint=spec.endpoint,
                status=LifecycleStatus.STARTING,
                started_at=started_at,
                observed_at=started_at,
                restart_count=restart_count,
                log_path=str(log_path),
                configuration_digest=spec.configuration_digest,
                process_create_time=create_time,
            )
            self.store.save_process(
                record,
                expected_revision=prior[1] if prior else None,
            )
            managed = ManagedProcess(
                spec=spec,
                process=process,
                record=record,
                log_stream=log_stream,
            )
            self._managed[spec.component_id] = managed
            if spec.resource_policy is not None:
                self.resources.apply(process.pid, spec.resource_policy.resource)
            return record

    def wait_process_ready(
        self,
        component_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        with self._lock:
            managed = self._managed.get(component_id)
        if managed is None:
            raise ProcessUnavailable(
                "deployment_process_not_managed",
                "process is not managed by the current supervisor generation",
                operation="wait_ready",
                details={"component_id": component_id},
            )
        if managed.spec.readiness is None:
            raise ProcessUnavailable(
                "deployment_readiness_probe_missing",
                "managed process has no readiness probe",
                operation="wait_ready",
                details={"component_id": component_id},
            )
        timeout = timeout_seconds or managed.spec.readiness_timeout_seconds
        deadline = time.monotonic() + timeout
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            exit_code = managed.process.poll()
            if exit_code is not None:
                self._mark_failed(component_id, exit_code=exit_code)
                raise ProcessUnavailable(
                    "deployment_process_exited_during_start",
                    "managed process exited before becoming ready",
                    operation="wait_ready",
                    profile=managed.spec.profile,
                    details={
                        "component_id": component_id,
                        "exit_code": exit_code,
                        "log_path": managed.record.log_path,
                    },
                )
            try:
                observation = dict(managed.spec.readiness())
                if observation.get("ready") is True or observation.get("status") in {
                    "ok",
                    "ready",
                    "degraded",
                }:
                    status = (
                        LifecycleStatus.DEGRADED
                        if observation.get("status") == "degraded"
                        else LifecycleStatus.READY
                    )
                    current = self.store.process(component_id)
                    updated = replace(
                        managed.record,
                        status=status,
                        observed_at=now_iso(),
                    )
                    self.store.save_process(
                        updated,
                        expected_revision=current[1] if current else None,
                    )
                    managed.record = updated
                    return observation
            except BaseException as error:
                last_error = error
            time.sleep(0.1)
        self._mark_failed(component_id)
        raise ProcessUnavailable(
            "deployment_process_readiness_timeout",
            "managed process did not become ready before the deadline",
            operation="wait_ready",
            profile=managed.spec.profile,
            details={
                "component_id": component_id,
                "timeout_seconds": timeout,
                "last_error": (
                    f"{type(last_error).__name__}: {last_error}"
                    if last_error is not None
                    else ""
                ),
                "log_path": managed.record.log_path,
            },
        )

    def stop(
        self,
        component_id: str,
        *,
        timeout_seconds: float = 10.0,
        tolerate_missing: bool = False,
    ) -> ProcessRecord | None:
        with self._lock:
            managed = self._managed.get(component_id)
            stored = self.store.process(component_id)
            if managed is None and stored is None:
                if tolerate_missing:
                    return None
                raise ProcessUnavailable(
                    "deployment_process_missing",
                    "managed process does not exist",
                    operation="stop",
                    details={"component_id": component_id},
                )
            record = managed.record if managed is not None else stored[0]
            revision = stored[1] if stored else None
            stopping = replace(
                record,
                status=LifecycleStatus.STOPPING,
                observed_at=now_iso(),
            )
            self.store.save_process(stopping, expected_revision=revision)
            if managed is not None:
                process = managed.process
                if process.poll() is None:
                    graceful = False
                    if managed.spec.shutdown is not None:
                        try:
                            receipt = dict(managed.spec.shutdown())
                            graceful = (
                                receipt.get("accepted") is True
                                or receipt.get("stopped") is True
                                or receipt.get("ready") is True
                            )
                        except BaseException:
                            graceful = False
                    if graceful:
                        try:
                            process.wait(timeout=timeout_seconds)
                        except subprocess.TimeoutExpired:
                            graceful = False
                    if not graceful and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=timeout_seconds)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=max(1.0, timeout_seconds / 2))
                exit_code = process.returncode
                managed.log_stream.close()
                self.resources.forget(process.pid)
                self._managed.pop(component_id, None)
            else:
                exit_code = self._terminate_recovered_process(record, timeout_seconds)
            current = self.store.process(component_id)
            stopped = replace(
                stopping,
                status=LifecycleStatus.STOPPED,
                observed_at=now_iso(),
                exit_code=exit_code,
            )
            self.store.save_process(
                stopped,
                expected_revision=current[1] if current else None,
            )
            return stopped

    def _terminate_recovered_process(
        self,
        record: ProcessRecord,
        timeout_seconds: float,
    ) -> int | None:
        try:
            process = psutil.Process(record.pid)
            if (
                record.process_create_time > 0
                and abs(process.create_time() - record.process_create_time) >= 0.01
            ):
                return record.exit_code
            process.terminate()
            try:
                return process.wait(timeout=timeout_seconds)
            except psutil.TimeoutExpired:
                process.kill()
                return process.wait(timeout=max(1.0, timeout_seconds / 2))
        except psutil.NoSuchProcess:
            return record.exit_code
        except psutil.AccessDenied as error:
            raise ProcessUnavailable(
                "deployment_process_termination_denied",
                "recovered process cannot be terminated",
                operation="stop",
                profile=record.profile,
                details={"pid": record.pid},
            ) from error

    def stop_all(self, *, timeout_seconds: float = 10.0) -> list[ProcessRecord]:
        records = [
            record
            for record, _revision in self.store.processes()
            if record.status
            not in {LifecycleStatus.STOPPED, LifecycleStatus.CRASHED}
        ]
        order = sorted(
            records,
            key=lambda item: (
                0 if item.component_id.startswith("profile:") else 1,
                item.component_id,
            ),
        )
        stopped: list[ProcessRecord] = []
        errors: list[BaseException] = []
        for record in order:
            try:
                value = self.stop(
                    record.component_id,
                    timeout_seconds=timeout_seconds,
                    tolerate_missing=True,
                )
                if value is not None:
                    stopped.append(value)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise ProcessUnavailable(
                "deployment_stop_partial",
                "one or more deployment processes could not be stopped",
                operation="stop_all",
                details={
                    "stopped": [item.component_id for item in stopped],
                    "errors": [
                        f"{type(error).__name__}: {error}" for error in errors
                    ],
                },
            )
        return stopped

    def status(self, component_id: str) -> ProcessRecord | None:
        stored = self.store.process(component_id)
        if stored is None:
            return None
        record, revision = stored
        current = self._observe_record(record)
        if current != record:
            self.store.save_process(current, expected_revision=revision)
            managed = self._managed.get(component_id)
            if managed is not None:
                managed.record = current
        return current

    def statuses(self) -> list[ProcessRecord]:
        result: list[ProcessRecord] = []
        for record, _revision in self.store.processes():
            observed = self.status(record.component_id)
            if observed is not None:
                result.append(observed)
        return result

    def _observe_record(self, record: ProcessRecord) -> ProcessRecord:
        if record.status in {
            LifecycleStatus.STOPPED,
            LifecycleStatus.CRASHED,
        }:
            return record
        try:
            process = psutil.Process(record.pid)
            same = (
                record.process_create_time <= 0
                or abs(process.create_time() - record.process_create_time) < 0.01
            )
            alive = (
                same
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            alive = False
        except psutil.AccessDenied:
            alive = True
        if alive:
            return replace(record, observed_at=now_iso())
        exit_code: int | None = record.exit_code
        managed = self._managed.get(record.component_id)
        if managed is not None:
            exit_code = managed.process.poll()
        return replace(
            record,
            status=LifecycleStatus.CRASHED,
            observed_at=now_iso(),
            exit_code=exit_code,
        )

    def _mark_failed(
        self,
        component_id: str,
        *,
        exit_code: int | None = None,
    ) -> None:
        current = self.store.process(component_id)
        if current is None:
            return
        record, revision = current
        updated = replace(
            record,
            status=LifecycleStatus.CRASHED,
            observed_at=now_iso(),
            exit_code=exit_code,
        )
        self.store.save_process(updated, expected_revision=revision)
        managed = self._managed.get(component_id)
        if managed is not None:
            managed.record = updated

    def reconcile(self) -> list[ProcessRecord]:
        alive: dict[int, float] = {}
        for process in psutil.process_iter(["pid", "create_time"]):
            try:
                alive[int(process.info["pid"])] = float(process.info["create_time"])
            except (TypeError, ValueError, psutil.Error):
                continue
        return self.store.reconcile_processes(alive)

    def process_report(self) -> dict[str, Any]:
        statuses = self.statuses()
        active = [
            item
            for item in statuses
            if item.status
            in {
                LifecycleStatus.STARTING,
                LifecycleStatus.READY,
                LifecycleStatus.DEGRADED,
            }
        ]
        return {
            "schema": "zyra.deployment-process-report/v1",
            "ready": bool(active)
            and all(
                item.status in {LifecycleStatus.READY, LifecycleStatus.DEGRADED}
                for item in active
            ),
            "processes": [item.to_dict() for item in statuses],
            "active_count": len(active),
            "crashed_count": sum(
                item.status is LifecycleStatus.CRASHED for item in statuses
            ),
            "independent_pids": len({item.pid for item in active}) == len(active),
            "secret_store": self.secrets.permission_report(),
            "resource_controls": self.resources.applied(),
        }


def command_environment_digest(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> str:
    return digest(
        {
            "command": list(command),
            "environment_names": sorted(environment),
            "environment_presence": {
                name: bool(str(value))
                for name, value in sorted(environment.items())
            },
        }
    )


__all__ = [
    "DeploymentProcessManager",
    "ManagedProcess",
    "ProcessSpec",
    "SupervisorSecretStore",
    "command_environment_digest",
]
