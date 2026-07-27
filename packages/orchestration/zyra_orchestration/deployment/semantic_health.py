from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .doctor import DeploymentDoctor
from .errors import (
    SemanticHealthDisabled,
    SemanticProbeFailed,
    redact,
)
from .http_client import ProductHttpClient
from .models import (
    DeploymentProfile,
    ProbeResult,
    ProbeStatus,
    SemanticHealthReport,
    digest,
    new_id,
    now_iso,
)
from .node_client import DeploymentNodeClient
from .profiles import ProfileCatalog
from .short_task import ShortTaskVerifier
from .state_store import DeploymentStateStore


@dataclass(frozen=True, slots=True)
class SemanticProbe:
    probe_id: str
    run: Callable[[], Mapping[str, Any]]
    dependencies: tuple[str, ...] = ()
    required: bool = True
    timeout_seconds: float = 30.0


class SemanticProbeRegistry:
    def __init__(self) -> None:
        self._probes: dict[str, SemanticProbe] = {}
        self._lock = threading.RLock()

    def register(self, probe: SemanticProbe) -> None:
        with self._lock:
            if not probe.probe_id or ":" in probe.probe_id:
                raise ValueError("semantic probe id must be non-empty and cannot contain ':'")
            if probe.probe_id in self._probes:
                raise ValueError(f"duplicate semantic probe: {probe.probe_id}")
            if probe.timeout_seconds <= 0 or probe.timeout_seconds > 600:
                raise ValueError("semantic probe timeout is outside the supported range")
            self._probes[probe.probe_id] = probe

    def probes(self) -> tuple[SemanticProbe, ...]:
        with self._lock:
            return tuple(self._probes.values())

    def validate(self) -> None:
        probes = {probe.probe_id: probe for probe in self.probes()}
        for probe in probes.values():
            missing = set(probe.dependencies) - set(probes)
            if missing:
                raise ValueError(
                    f"semantic probe {probe.probe_id} has missing dependencies: "
                    + ", ".join(sorted(missing))
                )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(probe_id: str) -> None:
            if probe_id in visited:
                return
            if probe_id in visiting:
                raise ValueError(f"semantic probe dependency cycle at {probe_id}")
            visiting.add(probe_id)
            for dependency in probes[probe_id].dependencies:
                visit(dependency)
            visiting.remove(probe_id)
            visited.add(probe_id)

        for probe_id in probes:
            visit(probe_id)

    def layers(self) -> tuple[tuple[SemanticProbe, ...], ...]:
        self.validate()
        probes = {probe.probe_id: probe for probe in self.probes()}
        remaining = set(probes)
        completed: set[str] = set()
        layers: list[tuple[SemanticProbe, ...]] = []
        while remaining:
            ready = sorted(
                (
                    probes[probe_id]
                    for probe_id in remaining
                    if set(probes[probe_id].dependencies) <= completed
                ),
                key=lambda item: item.probe_id,
            )
            if not ready:
                raise ValueError("semantic probe dependency graph cannot progress")
            layers.append(tuple(ready))
            completed.update(item.probe_id for item in ready)
            remaining -= {item.probe_id for item in ready}
        return tuple(layers)


class SemanticHealthRuntime:
    def __init__(
        self,
        *,
        project_root: Path | str,
        target_commit: str,
        catalog: ProfileCatalog,
        store: DeploymentStateStore,
        api: ProductHttpClient,
        web_url: str,
        node_clients: Mapping[DeploymentProfile, DeploymentNodeClient],
        doctor: DeploymentDoctor,
        short_task: ShortTaskVerifier,
        environment: Mapping[str, str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.target_commit = target_commit
        self.catalog = catalog
        self.store = store
        self.api = api
        self.web_url = web_url.rstrip("/")
        self.node_clients = dict(node_clients)
        self.doctor = doctor
        self.short_task = short_task
        self.environment = dict(os.environ if environment is None else environment)
        self.enabled = enabled
        self._runtime_readiness_lock = threading.RLock()
        self._runtime_readiness_cache: dict[str, Any] | None = None

    def registry(
        self,
        *,
        include_short_task: bool = True,
    ) -> SemanticProbeRegistry:
        registry = SemanticProbeRegistry()
        registry.register(SemanticProbe("api", self._probe_api))
        registry.register(SemanticProbe("web", self._probe_web))
        registry.register(
            SemanticProbe(
                "runtime-owners",
                self._probe_runtime_owners,
                dependencies=("api",),
                timeout_seconds=90,
            )
        )
        registry.register(
            SemanticProbe(
                "event-reducer",
                self._probe_event_reducer,
                dependencies=("api", "runtime-owners"),
            )
        )
        registry.register(
            SemanticProbe(
                "provider-failover",
                self._probe_provider,
                dependencies=("runtime-owners",),
            )
        )
        registry.register(
            SemanticProbe(
                "mcp-skills",
                self._probe_mcp_skills,
                dependencies=("runtime-owners",),
            )
        )
        registry.register(
            SemanticProbe(
                "workspace-artifact",
                self._probe_workspace_artifact,
                dependencies=("api", "runtime-owners"),
            )
        )
        registry.register(
            SemanticProbe(
                "scheduler-recovery",
                self._probe_scheduler_recovery,
                dependencies=("runtime-owners",),
            )
        )
        registry.register(
            SemanticProbe(
                "memory-checkpoint",
                self._probe_memory_checkpoint,
                dependencies=("runtime-owners",),
            )
        )
        registry.register(
            SemanticProbe(
                "sealed-permission",
                self._probe_sealed_permission,
                dependencies=("runtime-owners",),
            )
        )
        for profile in DeploymentProfile:
            registry.register(
                SemanticProbe(
                    f"node-{profile.value}",
                    lambda selected=profile: self._probe_node(selected),
                )
            )
        registry.register(
            SemanticProbe(
                "profile-isolation",
                self._probe_profile_isolation,
                dependencies=tuple(
                    f"node-{profile.value}" for profile in DeploymentProfile
                ),
            )
        )
        registry.register(
            SemanticProbe(
                "doctor",
                lambda: self.doctor.run(
                    selected=(
                        "runtime",
                        "dependencies",
                        "configuration",
                        "state-store",
                        "credentials",
                        "source-boundary",
                        "process-boundary",
                        "native-artifacts",
                    )
                ),
                dependencies=("profile-isolation",),
                timeout_seconds=120,
            )
        )
        if include_short_task:
            registry.register(
                SemanticProbe(
                    "short-task",
                    self.short_task.run,
                    dependencies=(
                        "web",
                        "event-reducer",
                        "provider-failover",
                        "mcp-skills",
                        "workspace-artifact",
                        "scheduler-recovery",
                        "memory-checkpoint",
                        "sealed-permission",
                        "profile-isolation",
                        "doctor",
                    ),
                    timeout_seconds=180,
                )
            )
        return registry

    def run(
        self,
        *,
        include_short_task: bool = True,
        fresh_state: bool = True,
        selected: Sequence[str] = (),
        clean_state_observation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled or self._truthy(
            self.environment.get("ZYRA_SEMANTIC_HEALTH_DISABLED")
        ):
            raise SemanticHealthDisabled(
                "semantic_health_disabled",
                "deployment semantic health runtime is disabled",
                operation="semantic_health",
            )
        started_at = now_iso()
        registry = self.registry(include_short_task=include_short_task)
        selected_ids = set(selected)
        if selected_ids:
            known = {probe.probe_id for probe in registry.probes()}
            unknown = selected_ids - known
            if unknown:
                raise SemanticProbeFailed(
                    "semantic_probe_unknown",
                    "unknown semantic probe requested",
                    operation="semantic_health",
                    details={"unknown": sorted(unknown)},
                )
        results: dict[str, ProbeResult] = {}
        for layer in registry.layers():
            runnable = [
                probe
                for probe in layer
                if not selected_ids or probe.probe_id in selected_ids
                or any(
                    selected_id in self._downstream(registry, probe.probe_id)
                    for selected_id in selected_ids
                )
            ]
            if not runnable:
                continue
            blocked_dependencies: dict[str, tuple[str, ...]] = {}
            ready: list[SemanticProbe] = []
            for probe in runnable:
                blocked = tuple(
                    dependency
                    for dependency in probe.dependencies
                    if dependency in results
                    and results[dependency].status is ProbeStatus.BLOCKED
                )
                if blocked:
                    blocked_dependencies[probe.probe_id] = blocked
                else:
                    ready.append(probe)
            for probe_id, blocked in blocked_dependencies.items():
                probe = next(item for item in runnable if item.probe_id == probe_id)
                results[probe_id] = ProbeResult(
                    probe_id=probe_id,
                    status=ProbeStatus.BLOCKED if probe.required else ProbeStatus.SKIPPED,
                    summary="semantic probe dependency is blocked",
                    started_at=now_iso(),
                    completed_at=now_iso(),
                    latency_ms=0,
                    blockers=tuple(f"dependency_blocked:{item}" for item in blocked),
                    dependencies=probe.dependencies,
                )
            results.update(self._run_layer(ready))
        ordered = tuple(
            results[probe.probe_id]
            for probe in registry.probes()
            if probe.probe_id in results
        )
        blockers = tuple(
            f"{item.probe_id}:{blocker}"
            for item in ordered
            for blocker in item.blockers
        )
        warnings = tuple(
            f"{item.probe_id}:{warning}"
            for item in ordered
            for warning in item.warnings
        )
        ready = not blockers and all(
            item.status in {ProbeStatus.READY, ProbeStatus.DEGRADED}
            for item in ordered
        )
        status = (
            ProbeStatus.READY
            if ready and not warnings
            else ProbeStatus.DEGRADED
            if ready
            else ProbeStatus.BLOCKED
        )
        short_task = next(
            (
                item
                for item in ordered
                if item.probe_id == "short-task"
            ),
            None,
        )
        report = SemanticHealthReport(
            report_id=new_id("semantic_health"),
            ready=ready,
            status=status,
            profile_digest=self.catalog.profile_digest,
            target_commit=self.target_commit,
            probes=ordered,
            blockers=blockers,
            warnings=warnings,
            started_at=started_at,
            completed_at=now_iso(),
            fresh_state=fresh_state,
            short_task_id=(
                str(short_task.observations.get("task_id") or "")
                if short_task
                else ""
            ),
        )
        body = report.to_dict()
        body["short_task_included"] = include_short_task
        body["clean_state"] = dict(clean_state_observation or {
            "requested": fresh_state,
            "verified": not fresh_state,
        })
        body["report_digest"] = digest(
            {key: value for key, value in body.items() if key != "report_digest"}
        )
        self.store.save_probe_report(body, target_commit=self.target_commit)
        return body

    def _run_layer(
        self,
        probes: Sequence[SemanticProbe],
    ) -> dict[str, ProbeResult]:
        if not probes:
            return {}
        result: dict[str, ProbeResult] = {}
        with ThreadPoolExecutor(
            max_workers=min(8, len(probes)),
            thread_name_prefix="zyra-semantic-health",
        ) as executor:
            futures: dict[Future[ProbeResult], SemanticProbe] = {
                executor.submit(self._run_probe, probe): probe
                for probe in probes
            }
            for future in as_completed(futures):
                probe = futures[future]
                try:
                    result[probe.probe_id] = future.result(
                        timeout=probe.timeout_seconds
                    )
                except BaseException as error:
                    result[probe.probe_id] = ProbeResult(
                        probe_id=probe.probe_id,
                        status=(
                            ProbeStatus.BLOCKED
                            if probe.required
                            else ProbeStatus.DEGRADED
                        ),
                        summary=f"{type(error).__name__}: {error}",
                        started_at=now_iso(),
                        completed_at=now_iso(),
                        latency_ms=0,
                        blockers=(
                            (f"{probe.probe_id}_execution_failed",)
                            if probe.required
                            else ()
                        ),
                        warnings=(
                            ()
                            if probe.required
                            else (f"{probe.probe_id}_execution_failed",)
                        ),
                        observations={
                            "error": type(error).__name__,
                            "message": str(error),
                            "fallback": False,
                        },
                        dependencies=probe.dependencies,
                    )
        return result

    @staticmethod
    def _run_probe(probe: SemanticProbe) -> ProbeResult:
        started_at = now_iso()
        started = time.monotonic()
        try:
            observation = dict(probe.run())
            explicit_ready = observation.get("ready")
            explicit_accepted = observation.get("accepted")
            ready = explicit_ready is True or (
                explicit_ready is None and explicit_accepted is True
            )
            blockers = tuple(
                str(item) for item in observation.get("blockers") or ()
            )
            warnings = tuple(
                str(item) for item in observation.get("warnings") or ()
            )
            if not ready and not blockers:
                blockers = (f"{probe.probe_id}_not_ready",)
            status = (
                ProbeStatus.READY
                if ready and not warnings
                else ProbeStatus.DEGRADED
                if ready
                else ProbeStatus.BLOCKED
            )
            summary = str(
                observation.get("summary")
                or ("semantic behavior ready" if ready else "semantic behavior blocked")
            )
        except BaseException as error:
            ready = False
            blockers = (
                (getattr(error, "code", f"{probe.probe_id}_failed"),)
                if probe.required
                else ()
            )
            warnings = (
                ()
                if probe.required
                else (getattr(error, "code", f"{probe.probe_id}_failed"),)
            )
            status = (
                ProbeStatus.BLOCKED
                if probe.required
                else ProbeStatus.DEGRADED
            )
            observation = (
                error.to_dict()
                if hasattr(error, "to_dict")
                else {
                    "error": type(error).__name__,
                    "message": str(error),
                    "fallback": False,
                }
            )
            summary = str(error)
        return ProbeResult(
            probe_id=probe.probe_id,
            status=status,
            summary=summary,
            started_at=started_at,
            completed_at=now_iso(),
            latency_ms=round((time.monotonic() - started) * 1000),
            blockers=blockers,
            warnings=warnings,
            observations=redact(observation),
            dependencies=probe.dependencies,
        )

    def _probe_api(self) -> dict[str, Any]:
        value = self.api.get("/health")
        workspace = value.get("workspace")
        ready = (
            value.get("ok") is True
            and value.get("service") == "zyra-api"
            and isinstance(workspace, Mapping)
        )
        return {
            "ready": ready,
            "summary": "API liveness plus workspace behavior",
            "service": value.get("service"),
            "api_version": value.get("api_version"),
            "workspace": dict(workspace) if isinstance(workspace, Mapping) else {},
            "capabilities": list(value.get("capabilities") or ()),
            "blockers": [] if ready else ["api_health_contract_invalid"],
        }

    def _probe_web(self) -> dict[str, Any]:
        request = Request(
            self.web_url + "/",
            headers={"Accept": "text/html", "User-Agent": "zyra-semantic-health/1"},
        )
        try:
            with urlopen(request, timeout=10.0) as response:
                body = response.read(2 * 1024 * 1024 + 1)
                status = int(response.status)
                content_type = str(response.headers.get("Content-Type") or "")
        except (URLError, OSError, TimeoutError) as error:
            raise SemanticProbeFailed(
                "web_endpoint_unavailable",
                "Web endpoint is unavailable",
                operation="probe_web",
                details={"error": f"{type(error).__name__}: {error}"},
            ) from error
        text = body.decode("utf-8", errors="replace")
        ready = (
            status == 200
            and len(body) <= 2 * 1024 * 1024
            and "text/html" in content_type
            and "<html" in text.casefold()
            and ("zyra" in text.casefold() or "root" in text.casefold())
        )
        return {
            "ready": ready,
            "summary": "Web production bundle is served",
            "status": status,
            "content_type": content_type,
            "bytes": len(body),
            "body_digest": digest(body),
            "blockers": [] if ready else ["web_semantic_response_invalid"],
        }

    def _runtime_readiness(self) -> dict[str, Any]:
        with self._runtime_readiness_lock:
            if self._runtime_readiness_cache is None:
                self._runtime_readiness_cache = self.api.get(
                    "/runtime/readiness"
                )
            return dict(self._runtime_readiness_cache)

    def _probe_runtime_owners(self) -> dict[str, Any]:
        value = self._runtime_readiness()
        canonical = value.get("canonical_owners")
        domains = (
            canonical.get("domains")
            if isinstance(canonical, Mapping)
            else None
        )
        ready_domains = [
            str(item.get("domain") or "")
            for item in domains or ()
            if isinstance(item, Mapping) and item.get("ready") is True
        ]
        fallback_domains = [
            str(item.get("domain") or "")
            for item in domains or ()
            if isinstance(item, Mapping) and item.get("fallback_active") is True
        ]
        ready = (
            value.get("ready") is True
            and isinstance(domains, list)
            and len(domains) == 11
            and len(ready_domains) == 11
            and not fallback_domains
        )
        return {
            "ready": ready,
            "summary": "eleven canonical runtime owners are semantically ready",
            "domain_count": len(domains or ()),
            "ready_domains": ready_domains,
            "fallback_domains": fallback_domains,
            "readiness_digest": digest(value),
            "blockers": (
                []
                if ready
                else list(value.get("blockers") or ())
                or ["canonical_runtime_owner_readiness_failed"]
            ),
        }

    def _domain(self, name: str) -> dict[str, Any]:
        value = self._runtime_readiness()
        canonical = value.get("canonical_owners")
        domains = canonical.get("domains") if isinstance(canonical, Mapping) else ()
        for item in domains or ():
            if isinstance(item, Mapping) and str(item.get("domain") or "") == name:
                return dict(item)
        return {}

    def _probe_event_reducer(self) -> dict[str, Any]:
        domain = self._domain("session_event_projection")
        ready = (
            domain.get("ready") is True
            and domain.get("fallback_active") is not True
            and bool(domain.get("owner"))
        )
        return {
            "ready": ready,
            "summary": "canonical event/session projection owner",
            "domain": domain,
            "blockers": [] if ready else ["event_projection_owner_not_ready"],
        }

    def _probe_provider(self) -> dict[str, Any]:
        domain = self._domain("provider_credential_failover")
        cloud_presence = self.catalog.credential_presence(DeploymentProfile.CLOUD)
        ready = (
            domain.get("ready") is True
            and domain.get("fallback_active") is not True
        )
        warnings = [] if any(cloud_presence.values()) else [
            "cloud_provider_credential_missing_fail_closed"
        ]
        return {
            "ready": ready,
            "summary": "provider catalog, credential presence and failover owner",
            "domain": domain,
            "cloud_credential_presence": cloud_presence,
            "credential_values_exposed": False,
            "warnings": warnings,
            "blockers": [] if ready else ["provider_owner_not_ready"],
        }

    def _probe_mcp_skills(self) -> dict[str, Any]:
        domain = self._domain("mcp_plugin_registry")
        ready = (
            domain.get("ready") is True
            and domain.get("fallback_active") is not True
        )
        return {
            "ready": ready,
            "summary": "MCP, skills and plugin registry canonical owner",
            "domain": domain,
            "blockers": [] if ready else ["mcp_skill_owner_not_ready"],
        }

    def _probe_workspace_artifact(self) -> dict[str, Any]:
        artifact = self._domain("artifact")
        gateway = self._domain("gateway_lease_busy")
        ready = (
            artifact.get("ready") is True
            and gateway.get("ready") is True
            and artifact.get("fallback_active") is not True
            and gateway.get("fallback_active") is not True
        )
        return {
            "ready": ready,
            "summary": "workspace/gateway and artifact stores",
            "artifact": artifact,
            "gateway": gateway,
            "blockers": [] if ready else ["workspace_artifact_owner_not_ready"],
        }

    def _probe_scheduler_recovery(self) -> dict[str, Any]:
        scheduler = self._domain("scheduler_recovery")
        worker = self._domain("worker_route")
        graph = self._domain("graph_checkpoint")
        ready = all(
            item.get("ready") is True and item.get("fallback_active") is not True
            for item in (scheduler, worker, graph)
        )
        return {
            "ready": ready,
            "summary": "scheduler placement, worker lease and recovery owners",
            "scheduler": scheduler,
            "worker": worker,
            "graph": graph,
            "blockers": [] if ready else ["scheduler_recovery_owner_not_ready"],
        }

    def _probe_memory_checkpoint(self) -> dict[str, Any]:
        memory = self._domain("memory_compact")
        graph = self._domain("graph_checkpoint")
        ready = all(
            item.get("ready") is True and item.get("fallback_active") is not True
            for item in (memory, graph)
        )
        return {
            "ready": ready,
            "summary": "memory/compact and exact checkpoint owners",
            "memory": memory,
            "graph_checkpoint": graph,
            "langgraph_default_runtime": False,
            "blockers": [] if ready else ["memory_checkpoint_owner_not_ready"],
        }

    def _probe_sealed_permission(self) -> dict[str, Any]:
        permission = self._domain("permission")
        ready = (
            permission.get("ready") is True
            and permission.get("fallback_active") is not True
        )
        return {
            "ready": ready,
            "summary": "sealed deterministic permission owner",
            "permission": permission,
            "model_authority": False,
            "unknown_action_policy": "deny-and-recover",
            "blockers": [] if ready else ["sealed_permission_owner_not_ready"],
        }

    def _probe_node(self, profile: DeploymentProfile) -> dict[str, Any]:
        client = self.node_clients.get(profile)
        if client is None:
            return {
                "ready": False,
                "summary": f"{profile.value} node client is missing",
                "blockers": [f"{profile.value}_node_client_missing"],
            }
        health = client.health()
        semantic = client.semantic_readiness()
        ready = (
            semantic.get("ready") is True
            and health.get("profile") == profile.value
            and health.get("node_id") == client.expected_node_id
            and health.get("generation_id") == client.expected_generation_id
            and int(health.get("pid") or 0) > 0
            and health.get("network", {}).get("in_process") is False
        )
        warnings = list(semantic.get("warnings") or ())
        if (
            profile is DeploymentProfile.CLOUD
            and not any(
                bool(value)
                for value in (health.get("credential_presence") or {}).values()
            )
        ):
            warnings.append("cloud_provider_credential_missing_fail_closed")
        return {
            "ready": ready,
            "summary": f"{profile.value} independent node semantic readiness",
            "health": health,
            "semantic": semantic,
            "warnings": warnings,
            "blockers": (
                []
                if ready
                else list(semantic.get("blockers") or ())
                or [f"{profile.value}_node_semantic_readiness_failed"]
            ),
        }

    def _probe_profile_isolation(self) -> dict[str, Any]:
        observations = [
            client.observation()
            for profile, client in sorted(
                self.node_clients.items(),
                key=lambda item: item[0].value,
            )
        ]
        pids = [item.pid for item in observations]
        node_ids = [item.node_id for item in observations]
        endpoints = [item.endpoint for item in observations]
        profiles = {item.profile for item in observations}
        ready = (
            profiles == set(DeploymentProfile)
            and len(pids) == len(set(pids)) == 3
            and len(node_ids) == len(set(node_ids)) == 3
            and len(endpoints) == len(set(endpoints)) == 3
            and all(
                item.network.get("in_process") is False
                for item in observations
            )
        )
        return {
            "ready": ready,
            "summary": "device, edge and cloud have independent process/node identity",
            "observations": [item.to_dict() for item in observations],
            "process_ids": pids,
            "node_ids": node_ids,
            "endpoints": endpoints,
            "blockers": [] if ready else ["profile_process_isolation_failed"],
        }

    @staticmethod
    def _downstream(
        registry: SemanticProbeRegistry,
        probe_id: str,
    ) -> set[str]:
        downstream: set[str] = set()
        changed = True
        while changed:
            changed = False
            for probe in registry.probes():
                if probe.probe_id in downstream:
                    continue
                if probe_id in probe.dependencies or any(
                    dependency in downstream for dependency in probe.dependencies
                ):
                    downstream.add(probe.probe_id)
                    changed = True
        return downstream

    @staticmethod
    def _truthy(value: Any) -> bool:
        return value is True or str(value or "").casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }


__all__ = [
    "SemanticHealthRuntime",
    "SemanticProbe",
    "SemanticProbeRegistry",
]
