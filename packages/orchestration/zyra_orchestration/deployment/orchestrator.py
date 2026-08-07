from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .clean_state import CleanStateManager
from .dispatch import DeploymentDispatchRuntime
from .doctor import DeploymentDoctor
from .errors import (
    DeploymentError,
    DoctorBlocked,
    ProcessUnavailable,
)
from .handoff import CheckpointHandoffRuntime
from .http_client import ProductHttpClient
from .models import (
    DeploymentProfile,
    DispatchReceipt,
    DispatchStatus,
    LifecycleStatus,
    NodeObservation,
    Sensitivity,
    Workload,
    digest,
    new_id,
    now_iso,
)
from .node_client import DeploymentNodeClient
from .placement import PlacementContext, PlacementPolicyRuntime
from .process_manager import DeploymentProcessManager, ProcessSpec
from .profiles import ProfileCatalog
from .recovery import DeploymentRecoveryRuntime
from .semantic_health import SemanticHealthRuntime
from .short_task import ShortTaskVerifier
from .state_store import DeploymentStateStore


class DeploymentOrchestrator:
    def __init__(
        self,
        project_root: Path | str,
        *,
        state_root: Path | str | None = None,
        environment: Mapping[str, str] | None = None,
        host: str = "127.0.0.1",
        profile_base_port: int = 8310,
        api_port: int = 8000,
        web_port: int = 5173,
        fence_nodes_to_supervisor: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.environment = dict(os.environ if environment is None else environment)
        shared_state_root = str(
            self.environment.get("ZYRA_STATE_ROOT") or ""
        ).strip()
        explicit_deployment_state_root = (
            state_root
            or self.environment.get("ZYRA_DEPLOYMENT_STATE_ROOT")
        )
        configured_state_root = (
            explicit_deployment_state_root
            or (
                Path(shared_state_root) / "deployment"
                if shared_state_root
                else self.project_root / "tmp" / "deployment"
            )
        )
        self.state_root = Path(configured_state_root).resolve()
        shared_root = (
            Path(shared_state_root).resolve() if shared_state_root else None
        )
        shared_root_is_safe = bool(
            shared_root is not None
            and shared_root != Path(shared_root.anchor)
            and shared_root.name
        )
        shared_child_exact = bool(
            explicit_deployment_state_root is None
            and shared_root_is_safe
            and self.state_root.parent == shared_root
            and self.state_root.name == "deployment"
        )
        if (
            self.project_root not in self.state_root.parents
            and not shared_child_exact
        ):
            raise ValueError("deployment state root must remain inside the project")
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.product_state_root = self.state_root / "product-state"
        self.product_state_root.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.api_port = int(self.environment.get("ZYRA_API_PORT") or api_port)
        self.web_port = int(self.environment.get("ZYRA_WEB_PORT") or web_port)
        selected_base_port = int(
            self.environment.get("ZYRA_DEPLOYMENT_PROFILE_BASE_PORT")
            or profile_base_port
        )
        self.catalog = ProfileCatalog.defaults(
            self.project_root,
            host=host,
            base_port=selected_base_port,
            environment=self.environment,
        )
        self.store = DeploymentStateStore(self.state_root / "deployment.sqlite3")
        self.clean_state = CleanStateManager(
            project_root=self.project_root,
            deployment_root=self.state_root,
            allow_external_deployment_root=shared_child_exact,
        )
        self.processes = DeploymentProcessManager(
            project_root=self.project_root,
            state_root=self.state_root,
            store=self.store,
            environment=self.environment,
            fence_nodes_to_supervisor=fence_nodes_to_supervisor,
        )
        self.placement = PlacementPolicyRuntime(
            self.catalog,
            self.store,
            environment=self.environment,
        )
        self.dispatch = DeploymentDispatchRuntime(self.store)
        self.handoff = CheckpointHandoffRuntime(
            self.store,
            canonical_verifier=self._canonical_checkpoint_ready,
        )
        self.recovery = DeploymentRecoveryRuntime(
            store=self.store,
            placement=self.placement,
            dispatch=self.dispatch,
            handoff=self.handoff,
        )
        self.api = ProductHttpClient(self.api_url)
        self._lock = threading.RLock()
        self._clients: dict[DeploymentProfile, DeploymentNodeClient] = {}

    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.api_port}"

    @property
    def web_url(self) -> str:
        return f"http://{self.host}:{self.web_port}"

    def target_commit(self) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return self._release_manifest_commit(git_error=error)
        commit = result.stdout.strip()
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            return self._release_manifest_commit(
                git_error=ValueError(f"invalid revision: {commit}")
            )
        return commit

    def _release_manifest_commit(self, *, git_error: BaseException) -> str:
        manifest_path = self.project_root / "release" / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProcessUnavailable(
                "deployment_revision_unavailable",
                "deployment runtime cannot resolve a Git or release revision",
                operation="target_commit",
                details={
                    "git_error": f"{type(git_error).__name__}: {git_error}",
                    "manifest": str(manifest_path),
                    "manifest_error": f"{type(error).__name__}: {error}",
                },
            ) from error
        if not isinstance(manifest, Mapping):
            raise ProcessUnavailable(
                "deployment_release_manifest_invalid",
                "deployment release manifest must be an object",
                operation="target_commit",
                details={"manifest": str(manifest_path)},
            )
        commit = str(manifest.get("source_commit") or "")
        if len(commit) != 40 or any(
            character not in "0123456789abcdef" for character in commit
        ):
            raise ProcessUnavailable(
                "deployment_release_revision_invalid",
                "deployment release manifest has an invalid source revision",
                operation="target_commit",
                details={
                    "manifest": str(manifest_path),
                    "revision": commit,
                },
            )
        return commit

    def product_environment(self) -> dict[str, str]:
        root = self.product_state_root
        artifacts = root / "artifacts"
        environment = {
            "ZYRA_PROFILE": "deployment",
            "ZYRA_STATE_ROOT": str(root),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_SQLITE_PATH": str(root / "zyra.sqlite3"),
            "ZYRA_WORKER_POOL_STORE": str(root / "worker-pool.sqlite3"),
            "ZYRA_GRAPH_STATE_STORE": str(root / "graph-state.sqlite3"),
            "ZYRA_FAULT_RUNTIME_STORE": str(root / "fault-runtime.sqlite3"),
            "ZYRA_RECOVERY_RUNTIME_STORE": str(root / "recovery-runtime.sqlite3"),
            "ZYRA_MEMORY_INDEX_PATH": str(root / "memory-index.sqlite3"),
            "ZYRA_CODE_INDEX_ROOT": str(root / "code-index"),
            "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
            "ZYRA_ARTIFACT_ROOT": str(artifacts),
            "ZYRA_PERMISSION_STORE": str(root / "permissions.json"),
            "ZYRA_PERMISSION_STATE": str(artifacts / ".permission" / "state.json"),
            "ZYRA_MCP_STATE": str(artifacts / ".mcp" / "state.json"),
            "ZYRA_TERMINAL_STATE": str(artifacts / ".terminal" / "sessions.json"),
            "ZYRA_CONTROL_STATE": str(artifacts / ".control"),
            "ZYRA_SUBAGENT_STATE": str(artifacts / ".subagents"),
            "ZYRA_SANDBOX_GATEWAY_STATE": str(artifacts / ".sandbox-gateway"),
            "ZYRA_PROVIDER_STATE": str(
                artifacts / ".provider-control-plane" / "provider.sqlite3"
            ),
            "ZYRA_MIGRATION_JOURNAL": str(root / ".productization" / "migrations.sqlite3"),
            "ZYRA_LIFECYCLE_LOG": str(root / ".productization" / "lifecycle.jsonl"),
            "ZYRA_API_HOST": self.host,
            "ZYRA_API_PORT": str(self.api_port),
            "ZYRA_WEB_HOST": self.host,
            "ZYRA_WEB_PORT": str(self.web_port),
            "ZYRA_CORS_ORIGINS": self.web_url,
            "ZYRA_DEPLOYMENT_STATE_ROOT": str(self.state_root),
            "ZYRA_DEPLOYMENT_PROFILE_BASE_PORT": str(
                self.catalog.policy(DeploymentProfile.DEVICE).port
            ),
        }
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ZAI_API_KEY",
            "KIMI_API_KEY",
            "DEEPSEEK_API_KEY",
            "ZYRA_PERMISSION_SERVICE_TOKEN",
            "ZYRA_WORKER_POOL_SECRET",
        ):
            value = str(self.environment.get(name) or "")
            if value:
                environment[name] = value
        return environment

    def build_web(self, *, force: bool = False) -> dict[str, Any]:
        index = self.project_root / "apps" / "web" / "dist" / "index.html"
        if index.is_file() and not force:
            return {
                "schema": "zyra.deployment-web-build/v1",
                "built": False,
                "ready": True,
                "reason": "existing-production-bundle",
                "index": str(index),
                "index_digest": self._file_digest(index),
            }
        local_bun = self.project_root / "node_modules" / ".bin" / (
            "bun.exe" if os.name == "nt" else "bun"
        )
        bun = shutil.which("bun") or (
            str(local_bun) if local_bun.is_file() else ""
        )
        if not bun:
            raise ProcessUnavailable(
                "deployment_web_build_tool_missing",
                "Bun is not installed and no workspace-local launcher exists",
                operation="build_web",
            )
        command = [bun, "run", "build:web"]
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=self.project_root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProcessUnavailable(
                "deployment_web_build_failed",
                "Web production bundle build could not start",
                operation="build_web",
                details={"error": f"{type(error).__name__}: {error}"},
            ) from error
        if result.returncode != 0 or not index.is_file():
            raise ProcessUnavailable(
                "deployment_web_build_failed",
                "Web production bundle build failed",
                operation="build_web",
                details={
                    "exit_code": result.returncode,
                    "output_tail": result.stdout[-4000:],
                },
            )
        return {
            "schema": "zyra.deployment-web-build/v1",
            "built": True,
            "ready": True,
            "command": command,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "index": str(index),
            "index_digest": self._file_digest(index),
        }

    def _api_spec(self) -> ProcessSpec:
        environment = self.product_environment()
        return ProcessSpec(
            component_id="api",
            command=(sys.executable, str(self.project_root / "scripts" / "dev_api.py")),
            cwd=self.project_root,
            endpoint=self.api_url,
            host=self.host,
            port=self.api_port,
            environment=environment,
            configuration_digest=digest(
                {
                    "kind": "api",
                    "environment": self._public_environment(environment),
                    "target_commit": self.target_commit(),
                }
            ),
            readiness=lambda: self.api.get("/runtime/readiness"),
            readiness_timeout_seconds=90.0,
            shutdown=lambda: self.api.post(
                "/deployment/shutdown",
                {},
                accepted_statuses=(202,),
                timeout_seconds=10.0,
            ),
            required=True,
        )

    def _web_spec(self) -> ProcessSpec:
        environment = self.product_environment()
        return ProcessSpec(
            component_id="web",
            command=(sys.executable, str(self.project_root / "scripts" / "dev_web.py")),
            cwd=self.project_root,
            endpoint=self.web_url,
            host=self.host,
            port=self.web_port,
            environment=environment,
            configuration_digest=digest(
                {
                    "kind": "web",
                    "environment": self._public_environment(environment),
                    "index": self._file_digest(
                        self.project_root / "apps" / "web" / "dist" / "index.html"
                    ),
                    "target_commit": self.target_commit(),
                }
            ),
            readiness=self._web_readiness,
            readiness_timeout_seconds=30.0,
            required=True,
        )

    def start(
        self,
        *,
        build_web: bool = True,
        restart: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            started_at = now_iso()
            if build_web:
                web_build = self.build_web()
            else:
                web_build = {
                    "ready": (
                        self.project_root / "apps" / "web" / "dist" / "index.html"
                    ).is_file(),
                    "built": False,
                }
            preflight = self.doctor().run(
                selected=(
                    "runtime",
                    "dependencies",
                    "configuration",
                    "state-store",
                    "credentials",
                    "source-boundary",
                    "native-artifacts",
                )
            )
            if preflight.get("ready") is not True:
                raise DoctorBlocked(
                    "deployment_start_preflight_blocked",
                    "deployment preflight doctor is blocked",
                    operation="start",
                    details={"preflight": preflight},
                )
            if restart:
                self.stop(tolerate_missing=True)
            launched: list[str] = []
            clients: dict[DeploymentProfile, DeploymentNodeClient] = {}
            observations: dict[str, Any] = {}
            try:
                for policy in self.catalog.policies():
                    record, client, health = self.processes.start_node(policy)
                    launched.append(record.component_id)
                    clients[policy.profile] = client
                    observations[record.component_id] = health
                api_spec = self._api_spec()
                api_record = self.processes.start(api_spec)
                launched.append(api_record.component_id)
                observations["api"] = self.processes.wait_process_ready("api")
                web_spec = self._web_spec()
                web_record = self.processes.start(web_spec)
                launched.append(web_record.component_id)
                observations["web"] = self.processes.wait_process_ready("web")
            except BaseException as error:
                stop_errors: list[str] = []
                for component_id in reversed(launched):
                    try:
                        self.processes.stop(component_id, tolerate_missing=True)
                    except BaseException as stop_error:
                        stop_errors.append(
                            f"{component_id}:{type(stop_error).__name__}:{stop_error}"
                        )
                self.store.append_event(
                    "deployment.start_failed",
                    {
                        "error": f"{type(error).__name__}: {error}",
                        "launched": launched,
                        "stop_errors": stop_errors,
                        "fallback": False,
                    },
                )
                raise
            self._clients = clients
            process_report = self.processes.process_report()
            ready = (
                process_report.get("ready") is True
                and set(clients) == set(DeploymentProfile)
            )
            receipt = {
                "schema": "zyra.deployment-start-receipt/v1",
                "ready": ready,
                "started_at": started_at,
                "completed_at": now_iso(),
                "target_commit": self.target_commit(),
                "profile_digest": self.catalog.profile_digest,
                "web_build": web_build,
                "processes": process_report,
                "observations": observations,
                "default_configuration": True,
                "demo_flag": False,
                "test_flag": False,
                "root_source_runtime_dependency": False,
                "fallback": False,
            }
            receipt["receipt_digest"] = digest(receipt)
            self.store.write_meta(
                "last_start_receipt",
                receipt,
            )
            self.store.append_event(
                "deployment.started",
                {
                    "ready": ready,
                    "target_commit": receipt["target_commit"],
                    "profile_digest": receipt["profile_digest"],
                    "receipt_digest": receipt["receipt_digest"],
                    "components": launched,
                },
            )
            return receipt

    def stop(
        self,
        *,
        tolerate_missing: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            started_at = now_iso()
            stopped = self.processes.stop_all()
            self._clients.clear()
            receipt = {
                "schema": "zyra.deployment-stop-receipt/v1",
                "stopped": True,
                "components": [item.to_dict() for item in stopped],
                "started_at": started_at,
                "completed_at": now_iso(),
                "remaining_active": [
                    item.to_dict()
                    for item in self.processes.statuses()
                    if item.status
                    not in {LifecycleStatus.STOPPED, LifecycleStatus.CRASHED}
                ],
            }
            receipt["ready"] = not receipt["remaining_active"]
            receipt["receipt_digest"] = digest(receipt)
            self.store.append_event(
                "deployment.stopped",
                {
                    "ready": receipt["ready"],
                    "component_count": len(stopped),
                    "receipt_digest": receipt["receipt_digest"],
                },
            )
            return receipt

    def restart(self) -> dict[str, Any]:
        stop = self.stop(tolerate_missing=True)
        start = self.start(build_web=True)
        return {
            "schema": "zyra.deployment-restart-receipt/v1",
            "ready": start.get("ready") is True,
            "stop": stop,
            "start": start,
            "restarted_at": now_iso(),
        }

    def status(self) -> dict[str, Any]:
        process_report = self.processes.process_report()
        profiles: dict[str, Any] = {}
        for profile in DeploymentProfile:
            try:
                client = self.client(profile)
                profiles[profile.value] = {
                    "reachable": True,
                    "health": client.health(),
                }
            except BaseException as error:
                profiles[profile.value] = {
                    "reachable": False,
                    "error": f"{type(error).__name__}: {error}",
                }
        api: dict[str, Any]
        try:
            api = {"reachable": True, "health": self.api.get("/health")}
        except BaseException as error:
            api = {
                "reachable": False,
                "error": f"{type(error).__name__}: {error}",
            }
        try:
            web = {"reachable": True, **self._web_readiness()}
        except BaseException as error:
            web = {
                "reachable": False,
                "error": f"{type(error).__name__}: {error}",
            }
        expected = {
            "api",
            "web",
            "profile:device",
            "profile:edge",
            "profile:cloud",
        }
        active = {
            item.component_id
            for item in self.processes.statuses()
            if item.status in {LifecycleStatus.READY, LifecycleStatus.DEGRADED}
        }
        ready = expected <= active and all(
            item.get("reachable") is True
            for item in [api, web, *profiles.values()]
        )
        return {
            "schema": "zyra.deployment-status/v1",
            "ready": ready,
            "status": "ready" if ready else "partial" if active else "stopped",
            "target_commit": self.target_commit(),
            "profile_digest": self.catalog.profile_digest,
            "processes": process_report,
            "profiles": profiles,
            "api": api,
            "web": web,
            "missing_components": sorted(expected - active),
            "store": self.store.integrity_report(),
            "observed_at": now_iso(),
            "fallback": False,
        }

    def doctor(self) -> DeploymentDoctor:
        return DeploymentDoctor(
            project_root=self.project_root,
            catalog=self.catalog,
            store=self.store,
            process_manager=self.processes,
            clean_state=self.clean_state,
            environment={
                **self.environment,
                **self.product_environment(),
            },
        )

    def clients(self) -> dict[DeploymentProfile, DeploymentNodeClient]:
        result: dict[DeploymentProfile, DeploymentNodeClient] = {}
        for profile in DeploymentProfile:
            result[profile] = self.client(profile)
        return result

    def client(self, profile: DeploymentProfile) -> DeploymentNodeClient:
        existing = self._clients.get(profile)
        if existing is not None:
            return existing
        recovered = self.processes.recovered_node_client(profile)
        self._clients[profile] = recovered
        return recovered

    def observations(self) -> dict[DeploymentProfile, NodeObservation]:
        return {
            profile: client.observation()
            for profile, client in self.clients().items()
        }

    def dispatch_workload(
        self,
        workload: Workload,
        *,
        unavailable_profiles: frozenset[DeploymentProfile] = frozenset(),
        excluded_profiles: frozenset[DeploymentProfile] = frozenset(),
        allow_degraded: bool = False,
    ) -> dict[str, Any]:
        observations = self.observations()
        context = PlacementContext(
            observations=observations,
            unavailable_profiles=unavailable_profiles,
            excluded_profiles=excluded_profiles,
            allow_degraded=allow_degraded,
        )
        decision = self.placement.decide(workload, context)
        receipt = self.dispatch.dispatch(
            workload,
            decision,
            self.client(decision.selected_profile),
            timeout_seconds=max(10.0, workload.latency_sla_ms / 1000 + 10.0),
        )
        return {
            "decision": decision.to_dict(),
            "receipt": receipt.to_dict(),
            "observations": {
                profile.value: observation.to_dict()
                for profile, observation in observations.items()
            },
        }

    def exercise_profiles(
        self,
        *,
        task_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        clients = self.clients()
        observations = self.observations()
        workloads = self._exercise_workloads(task_id=task_id, run_id=run_id)
        routes: list[dict[str, Any]] = []
        receipts: dict[DeploymentProfile, DispatchReceipt] = {}
        for expected_profile, workload in workloads.items():
            decision = self.placement.decide(
                workload,
                PlacementContext(observations=observations),
            )
            if decision.selected_profile is not expected_profile:
                raise ProcessUnavailable(
                    "deployment_exercise_route_unexpected",
                    "deployment exercise selected an unexpected profile",
                    operation="exercise_profiles",
                    profile=decision.selected_profile.value,
                    details={
                        "expected_profile": expected_profile.value,
                        "decision": decision.to_dict(),
                    },
                )
            receipt = self.dispatch.dispatch(
                workload,
                decision,
                clients[expected_profile],
            )
            if receipt.status is not DispatchStatus.SUCCEEDED:
                raise ProcessUnavailable(
                    "deployment_exercise_dispatch_failed",
                    "deployment exercise dispatch did not succeed",
                    operation="exercise_profiles",
                    profile=expected_profile.value,
                    details={"receipt": receipt.to_dict()},
                )
            receipts[expected_profile] = receipt
            routes.append(
                {
                    "workload_id": workload.workload_id,
                    "profile": expected_profile.value,
                    "decision_id": decision.decision_id,
                    "attempt_id": receipt.attempt_id,
                    "node_id": receipt.node_id,
                    "artifact_refs": list(receipt.artifact_refs),
                    "checkpoint_ref": receipt.checkpoint_ref,
                    "result_digest": receipt.result_digest,
                }
            )
        edge_workload = workloads[DeploymentProfile.EDGE]
        edge_receipt = receipts[DeploymentProfile.EDGE]
        clients[DeploymentProfile.EDGE].inject_fault(
            {"kind": "network-loss", "enabled": True}
        )
        self.store.append_event(
            "deployment.failure_observed",
            {
                "kind": "edge-network-loss",
                "profile": "edge",
                "attempt_id": edge_receipt.attempt_id,
                "checkpoint_ref": edge_receipt.checkpoint_ref,
            },
            component_id=edge_receipt.node_id,
            profile="edge",
            task_id=task_id,
            run_id=run_id,
            causation_id=edge_receipt.attempt_id,
        )
        failed_edge = replace(
            edge_receipt,
            status=DispatchStatus.FAILED,
            failure_code="node_network_unavailable",
            completed_at=now_iso(),
        )
        recovery = self.recovery.recover(
            workload=edge_workload,
            failed_receipt=failed_edge,
            observations=observations,
            clients=clients,
            source_client=clients[DeploymentProfile.EDGE],
            unavailable_profiles=frozenset({DeploymentProfile.EDGE}),
        )
        clients[DeploymentProfile.EDGE].inject_fault({"kind": "clear"})
        pids = [item.pid for item in observations.values()]
        placement_binding = (
            {item["profile"] for item in routes}
            == {"device", "edge", "cloud"}
            and all(item["artifact_refs"] for item in routes)
        )
        handoff_verified = (
            recovery.handoff is not None
            and recovery.handoff.verified
            and recovery.receipt.predecessor_attempt_id
            == edge_receipt.attempt_id
            and bool(recovery.receipt.checkpoint_ref)
        )
        result = {
            "schema": "zyra.deployment-profile-exercise/v1",
            "ready": (
                placement_binding
                and recovery.receipt.status is DispatchStatus.SUCCEEDED
                and handoff_verified
            ),
            "task_id": task_id,
            "run_id": run_id,
            "routes": routes,
            "placement_binding_verified": placement_binding,
            "independent_processes": len(pids) == len(set(pids)) == 3,
            "process_ids": pids,
            "failure_recovery": {
                "migrated": recovery.target_profile
                is not DeploymentProfile.EDGE,
                "source_profile": recovery.source_profile.value,
                "target_profile": recovery.target_profile.value,
                "checkpoint_handoff_verified": handoff_verified,
                "handoff_failure": recovery.handoff_failure,
                "handoff": recovery.handoff.to_dict()
                if recovery.handoff
                else None,
                "receipt": recovery.receipt.to_dict(),
                "degraded": recovery.degraded,
            },
            "observations": {
                profile.value: observation.to_dict()
                for profile, observation in observations.items()
            },
            "credential_values_exposed": False,
            "completed_at": now_iso(),
            "fallback": False,
        }
        result["receipt_digest"] = digest(result)
        self.store.append_event(
            "deployment.profile_exercise_completed",
            {
                "ready": result["ready"],
                "task_id": task_id,
                "run_id": run_id,
                "profiles": [item["profile"] for item in routes],
                "recovery_target": recovery.target_profile.value,
                "receipt_digest": result["receipt_digest"],
            },
            task_id=task_id,
            run_id=run_id,
        )
        return result

    def semantic_health(
        self,
        *,
        include_short_task: bool = True,
        fresh_state: bool = True,
        selected: Sequence[str] = (),
    ) -> dict[str, Any]:
        clients = self.clients()
        clean_state_observation: dict[str, Any] = {
            "schema": "zyra.deployment-clean-state-observation/v1",
            "requested": fresh_state,
            "verified": not fresh_state,
        }
        if fresh_state:
            run_root = self.clean_state.create_run_root(
                prefix="semantic-health-preflight"
            )
            reset = self.clean_state.reset_run_root(run_root)
            removal = self.clean_state.remove_run_root(run_root)
            clean_state_observation.update(
                {
                    "verified": (
                        reset.get("fresh") is True
                        and removal.get("removed") is True
                    ),
                    "reset": reset,
                    "removal": removal,
                }
            )
        short_task = ShortTaskVerifier(
            api=self.api,
            store=self.store,
            deployment_exercise=self.exercise_profiles,
        )
        runtime = SemanticHealthRuntime(
            project_root=self.project_root,
            target_commit=self.target_commit(),
            catalog=self.catalog,
            store=self.store,
            api=self.api,
            web_url=self.web_url,
            node_clients=clients,
            doctor=self.doctor(),
            short_task=short_task,
            environment=self.environment,
        )
        return runtime.run(
            include_short_task=include_short_task,
            fresh_state=fresh_state,
            selected=selected,
            clean_state_observation=clean_state_observation,
        )

    def inject_fault(
        self,
        profile: DeploymentProfile,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = self.client(profile).inject_fault(payload)
        self.store.append_event(
            "deployment.fault_injected",
            result,
            component_id=str(result.get("node_id") or ""),
            profile=profile.value,
        )
        return result

    def _canonical_checkpoint_ready(
        self,
        *,
        task_id: str,
        run_id: str,
        checkpoint_ref: str,
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        ready = False
        for attempt in range(1, 6):
            try:
                task = self.api.get(f"/tasks/{task_id}")
            except DeploymentError as error:
                attempts.append(
                    {
                        "attempt": attempt,
                        "ready": False,
                        "error": error.code,
                    }
                )
            else:
                value = task.get("task")
                observed_task_id = (
                    str(value.get("task_id") or "")
                    if isinstance(value, Mapping)
                    else ""
                )
                observed_run_id = (
                    str(value.get("run_id") or "")
                    if isinstance(value, Mapping)
                    else ""
                )
                ready = (
                    observed_task_id == task_id
                    and observed_run_id == run_id
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "ready": ready,
                        "observed_task_id": observed_task_id,
                        "observed_run_id": observed_run_id,
                    }
                )
                if ready:
                    break
            if attempt < 5:
                time.sleep(0.05 * attempt)
        return {
            "ready": ready,
            "task_id": task_id,
            "run_id": run_id,
            "checkpoint_ref": checkpoint_ref,
            "owner": "SQLiteStore/CheckpointRecoveryRuntime",
            "deployment_is_canonical_owner": False,
            "attempts": attempts,
        }

    def _exercise_workloads(
        self,
        *,
        task_id: str,
        run_id: str,
    ) -> dict[DeploymentProfile, Workload]:
        return {
            DeploymentProfile.DEVICE: Workload(
                workload_id=new_id("workload"),
                task_id=task_id,
                run_id=run_id,
                operation="hash-manifest",
                payload={
                    "entries": {
                        "task_id": task_id,
                        "scope": "restricted-device",
                    }
                },
                sensitivity=Sensitivity.RESTRICTED,
                complexity=2,
                latency_sla_ms=60,
                cpu_units=1,
                memory_mb=64,
                required_capabilities=("local-compute",),
                idempotency_key=new_id("idempotency"),
            ),
            DeploymentProfile.EDGE: Workload(
                workload_id=new_id("workload"),
                task_id=task_id,
                run_id=run_id,
                operation="analyze-text",
                payload={
                    "text": (
                        "Fresh latency-bound edge analysis for canonical task "
                        f"{task_id}."
                    )
                },
                sensitivity=Sensitivity.CONFIDENTIAL,
                complexity=5,
                latency_sla_ms=120,
                cpu_units=2,
                memory_mb=128,
                required_capabilities=(
                    "edge-compute",
                    "deterministic-transform",
                ),
                idempotency_key=new_id("idempotency"),
            ),
            DeploymentProfile.CLOUD: Workload(
                workload_id=new_id("workload"),
                task_id=task_id,
                run_id=run_id,
                operation="deterministic-transform",
                payload={
                    "items": [
                        {"task_id": task_id, "segment": index}
                        for index in range(24)
                    ]
                },
                sensitivity=Sensitivity.PUBLIC,
                complexity=10,
                latency_sla_ms=1500,
                cpu_units=5,
                memory_mb=512,
                required_capabilities=(
                    "cloud-compute",
                    "deterministic-transform",
                ),
                provider_required=False,
                idempotency_key=new_id("idempotency"),
            ),
        }

    def _web_readiness(self) -> dict[str, Any]:
        request = Request(
            self.web_url + "/",
            headers={"Accept": "text/html", "User-Agent": "zyra-deployment/1"},
        )
        with urlopen(request, timeout=5.0) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            content_type = str(response.headers.get("Content-Type") or "")
            status = int(response.status)
        text = body.decode("utf-8", errors="replace")
        ready = (
            status == 200
            and len(body) <= 2 * 1024 * 1024
            and "text/html" in content_type
            and "<html" in text.casefold()
        )
        return {
            "ready": ready,
            "status": "ready" if ready else "blocked",
            "service": "zyra-web",
            "bytes": len(body),
            "body_digest": digest(body),
        }

    @staticmethod
    def _public_environment(environment: Mapping[str, str]) -> dict[str, Any]:
        secret_markers = ("secret", "token", "password", "api_key")
        return {
            name: (
                {"present": bool(value), "value_exposed": False}
                if any(marker in name.casefold() for marker in secret_markers)
                or name.endswith("_KEY")
                else value
            )
            for name, value in sorted(environment.items())
        }

    @staticmethod
    def _file_digest(path: Path) -> str:
        import hashlib

        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        return "sha256:" + hasher.hexdigest()


__all__ = ["DeploymentOrchestrator"]
