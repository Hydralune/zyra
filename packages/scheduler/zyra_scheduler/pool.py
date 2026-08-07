from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from zyra_core import TaskState

from .models import (
    ResourceLocation,
    WorkerBackendKind,
    WorkerHealth,
    WorkerHealthStatus,
    WorkerManifest,
)


def default_worker_manifests() -> list[WorkerManifest]:
    return [
        WorkerManifest(
            worker_id="provider-code-worker",
            display_name="Provider-backed Code Worker",
            runtime_worker="CodeWorkerRuntime",
            location=ResourceLocation.CLOUD,
            backend=WorkerBackendKind.CLOUD_MODEL,
            capabilities=[
                "agent_task",
                "provider-reasoning",
                "direct-response",
                "tool-use",
                "artifact-production",
                "coding",
                "codebase-analysis",
                "code-change",
                "verification",
                "tool-permission",
                "compact",
                "mcp",
                "subagent",
                "checkpoint",
                "artifact",
                "shell",
            ],
            tools=["file_read", "file_write", "shell", "trace", "checkpoint", "artifact_write", "web_search"],
            models=[
                "zhipu/glm-5.2",
                "deepseek/deepseek-v4-flash",
                "kimi-platform/kimi-k2.7-code",
            ],
            sandbox="deployment-node-plus-workspace-permission-gateway",
            gateway="zyra_orchestration.deployment.CodeWorkerAdapter",
            workspace_scope="project-workspace",
            privacy_level="internal_or_project",
            latency_ms=160,
            cost_per_1k_tokens=0.035,
            source_modules={
                "claude-code-best": ["QueryEngine", "tool permission runtime", "commands/bashes/doctor"],
                "OpenHands": ["workspace gateway", "event stream runtime"],
            },
            metadata={
                "dispatch": (
                    "cloud deployment-node process -> Python adapter -> "
                    "vendored TypeScript QueryEngine provider/tool loop"
                ),
                "legacy_worker_id": True,
                "provider_reasoning_required": True,
            },
        ),
        WorkerManifest(
            worker_id="edge-browser-worker",
            display_name="Simulated Edge Browser Worker",
            runtime_worker="BrowserWorker",
            location=ResourceLocation.EDGE,
            backend=WorkerBackendKind.SIMULATED_EDGE,
            capabilities=[
                "web-research",
                "browser-action",
                "browser-agent",
                "screenshot",
                "dom-state",
                "browser-trace",
                "watchdog",
            ],
            tools=["browser", "open_url", "extract_text", "screenshot", "dom_snapshot"],
            models=["edge-browser-policy"],
            sandbox="simulated-edge-browser-session",
            gateway="zyra_workers.BrowserWorkerRuntime",
            workspace_scope="artifact-sandbox",
            privacy_level="public_or_masked",
            latency_ms=90,
            cost_per_1k_tokens=0.002,
            source_modules={
                "browser-use": ["BrowserSession", "Agent", "Controller", "DOM/state extraction"],
                "OpenHands": ["event stream runtime"],
            },
            metadata={"dispatch": "vendored browser-use adapter"},
        ),
        WorkerManifest(
            worker_id="cloud-planner-verifier",
            display_name="Cloud Planner Verifier",
            runtime_worker="CodeWorkerRuntime",
            location=ResourceLocation.CLOUD,
            backend=WorkerBackendKind.CLOUD_MODEL,
            capabilities=[
                "provider-reasoning",
                "direct-response",
                "long-horizon-planning",
                "deep-reasoning",
                "verification",
                "critique",
                "fallback-model",
                "schema-check",
            ],
            tools=["trace", "checkpoint", "artifact_write"],
            models=["cloud-strong-reasoner", "cloud-verifier"],
            sandbox="no-secret-cloud-envelope",
            gateway="zyra_scheduler.WorkerBackendGateway",
            workspace_scope="artifact-only",
            privacy_level="public_only",
            latency_ms=160,
            cost_per_1k_tokens=0.035,
            source_modules={
                "agent-framework": ["workflow middleware", "context compaction decision"],
                "claude-code-best": ["model command", "cost/usage command"],
            },
            metadata={"dispatch": "cloud model route represented through CodeWorkerRuntime contract"},
        ),
        WorkerManifest(
            worker_id="local-memory-curator",
            display_name="Local Memory Curator",
            runtime_worker="MemoryContinuityRuntime",
            location=ResourceLocation.LOCAL,
            backend=WorkerBackendKind.LOCAL_PROCESS,
            capabilities=[
                "memory-refresh",
                "compact",
                "trajectory-replay",
                "checkpoint",
                "recovery-context",
                "skill-memory",
            ],
            tools=["trace", "checkpoint", "artifact_write"],
            models=["local-memory-curator"],
            sandbox="workspace-memory-store",
            gateway="zyra_memory.MemoryFabric",
            workspace_scope="task-memory",
            privacy_level="sensitive_ok",
            latency_ms=45,
            cost_per_1k_tokens=0.0,
            source_modules={
                "openclaw": ["active-memory", "trajectory"],
                "agent-framework": ["context compaction strategy"],
                "claude-code-best": ["auto compact", "reactive compact"],
            },
            metadata={
                "dispatch": (
                    "deterministic MemoryFabric continuity adapter in the "
                    "deployment-node runtime"
                )
            },
        ),
    ]


class WorkerPool:
    def __init__(self, manifests: Sequence[WorkerManifest] | None = None) -> None:
        self._manifests = list(manifests) if manifests is not None else default_worker_manifests()

    def manifests(self) -> list[WorkerManifest]:
        return list(self._manifests)

    def by_id(self, worker_id: str) -> WorkerManifest | None:
        for manifest in self._manifests:
            if manifest.matches(worker_id):
                return manifest
        return None

    def runtime_workers(self) -> list[str]:
        return sorted({manifest.runtime_worker for manifest in self._manifests if manifest.enabled})

    def health_snapshot(
        self,
        *,
        state: TaskState | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[WorkerHealth]:
        failures = _failure_counts(state, events or [])
        successes = _success_counts(events or [])
        health: list[WorkerHealth] = []
        for manifest in self._manifests:
            failure_count = failures.get(manifest.worker_id, 0) + failures.get(manifest.runtime_worker, 0)
            success_count = successes.get(manifest.worker_id, 0) + successes.get(manifest.runtime_worker, 0)
            status = WorkerHealthStatus.HEALTHY
            reason = "ready"
            if not manifest.enabled:
                status = WorkerHealthStatus.UNAVAILABLE
                reason = "manifest disabled"
            elif failure_count >= 3:
                status = WorkerHealthStatus.FAILED
                reason = "multiple recent failures"
            elif failure_count:
                status = WorkerHealthStatus.DEGRADED
                reason = "recent failure history"
            health.append(
                WorkerHealth(
                    worker_id=manifest.worker_id,
                    status=status,
                    recent_failures=failure_count,
                    recent_successes=success_count,
                    current_load=manifest.current_load,
                    reason=reason,
                    metadata={
                        "runtime_worker": manifest.runtime_worker,
                        "backend": str(manifest.backend),
                        "location": str(manifest.location),
                    },
                )
            )
        return health


def _failure_counts(state: TaskState | None, events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    if state is not None:
        for node in state.plan_nodes.values():
            if str(node.status) == "failed" and node.assigned_worker_id:
                _increment(counts, str(node.assigned_worker_id))
        for item in state.metadata.get("failure_injections", []):
            if isinstance(item, Mapping):
                raw = " ".join(str(value) for value in item.values()).lower()
                _count_worker_mentions(counts, raw)
    for event in events:
        payload = event.get("payload") if isinstance(event, Mapping) else {}
        raw = str(payload if payload is not None else "").lower()
        event_type = str(event.get("event_type") or "")
        if event_type in {"failure_injected", "node_failed", "recovery_planned"} or "failed" in raw:
            _count_worker_mentions(counts, raw)
    return counts


def _success_counts(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        payload = event.get("payload") if isinstance(event, Mapping) else {}
        if not isinstance(payload, Mapping):
            continue
        for key in ("worker_result", "browser_result", "tool_result"):
            result = payload.get(key)
            if not isinstance(result, Mapping) or result.get("ok") is not True:
                continue
            metadata = result.get("metadata") if isinstance(result.get("metadata"), Mapping) else {}
            worker = str(metadata.get("worker") or metadata.get("manifest_id") or "")
            if worker:
                _increment(counts, worker)
    return counts


def _count_worker_mentions(counts: dict[str, int], raw: str) -> None:
    if "browser" in raw:
        _increment(counts, "BrowserWorker")
        _increment(counts, "edge-browser-worker")
    if "code" in raw or "shell" in raw or "tool" in raw:
        _increment(counts, "CodeWorkerRuntime")
        _increment(counts, "provider-code-worker")
    if "cloud" in raw or "model" in raw:
        _increment(counts, "cloud-planner-verifier")
    if "memory" in raw or "compact" in raw or "trajectory" in raw:
        _increment(counts, "local-memory-curator")


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1
