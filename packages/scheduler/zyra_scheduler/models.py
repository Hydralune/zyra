from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import new_id, now_iso


class ResourceLocation(StrEnum):
    LOCAL = "local"
    EDGE = "edge"
    CLOUD = "cloud"


class WorkerBackendKind(StrEnum):
    LOCAL_PROCESS = "local_process"
    ISOLATED_PROCESS = "isolated_process"
    SIMULATED_EDGE = "simulated_edge"
    DOCKER_SANDBOX = "docker_sandbox"
    CLOUD_MODEL = "cloud_model"


class WorkerHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class FailureKind(StrEnum):
    TOOL_TIMEOUT = "tool_timeout"
    WORKER_UNAVAILABLE = "worker_unavailable"
    BROWSER_CRASH = "browser_crash"
    PERMISSION_DENIED = "permission_denied"
    MODEL_ERROR = "model_error"
    SCHEMA_ERROR = "schema_error"
    VALIDATION_FAILED = "validation_failed"
    NODE_FAILED = "node_failed"
    REQUIREMENT_DRIFT = "requirement_drift"
    UNKNOWN = "unknown"


class RecoveryAction(StrEnum):
    RETRY_SAME_WORKER = "retry_same_worker"
    REROUTE_WORKER = "reroute_worker"
    FALLBACK_MODEL = "fallback_model"
    CHECKPOINT_RESUME = "checkpoint_resume"
    LOCAL_REPLAN = "local_replan"
    DEGRADE_OUTPUT = "degrade_output"
    EXPLAIN_FAILURE = "explain_failure"


@dataclass(slots=True)
class WorkerManifest:
    worker_id: str
    display_name: str
    runtime_worker: str
    location: ResourceLocation
    backend: WorkerBackendKind
    capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    sandbox: str = "workspace"
    gateway: str = ""
    workspace_scope: str = "task"
    privacy_level: str = "project"
    max_concurrency: int = 1
    current_load: int = 0
    latency_ms: int = 50
    cost_per_1k_tokens: float = 0.0
    enabled: bool = True
    source_modules: dict[str, list[str]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(self, value: str) -> bool:
        normalized = value.strip().lower()
        if not normalized:
            return False
        names = {
            self.worker_id.lower(),
            self.display_name.lower(),
            self.runtime_worker.lower(),
        }
        return normalized in names


@dataclass(slots=True)
class WorkerHealth:
    worker_id: str
    status: WorkerHealthStatus = WorkerHealthStatus.HEALTHY
    recent_failures: int = 0
    recent_successes: int = 0
    current_load: int = 0
    reason: str = ""
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SchedulerSignals:
    required_tools: list[str] = field(default_factory=list)
    preferred_worker: str = ""
    avoided_workers: list[str] = field(default_factory=list)
    privacy_mode: str = "project"
    contains_url: bool = False
    stage: str = ""
    task_profile: str = "general"
    failure_workers: dict[str, int] = field(default_factory=dict)
    requirement_change_count: int = 0
    failure_count: int = 0
    compact_count: int = 0
    trajectory_count: int = 0
    memory_record_count: int = 0
    model_pressure: str = "balanced"
    source_event_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResourceDecision:
    run_id: str
    task_id: str
    node_id: str | None
    selected_manifest_id: str
    selected_worker: str
    selected_backend: WorkerBackendKind
    selected_location: ResourceLocation
    score: float
    reasons: list[str]
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    model_split: dict[str, Any] = field(default_factory=dict)
    signals: SchedulerSignals = field(default_factory=SchedulerSignals)
    decision_id: str = field(default_factory=lambda: new_id("resdec"))
    created_at: str = field(default_factory=now_iso)
    source_modules: dict[str, list[str]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FailureSignal:
    run_id: str
    task_id: str
    kind: FailureKind
    summary: str
    signal_id: str = field(default_factory=lambda: new_id("failure"))
    node_id: str | None = None
    failed_worker: str = ""
    severity: str = "medium"
    retryable: bool = True
    raw: str = ""
    evidence_event_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    source_modules: dict[str, list[str]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RecoveryPlan:
    run_id: str
    task_id: str
    node_id: str | None
    failure_signal_id: str
    actions: list[RecoveryAction]
    summary: str
    selected_worker: str
    selected_manifest_id: str
    decision: ResourceDecision | None = None
    can_continue: bool = True
    affected_node_ids: list[str] = field(default_factory=list)
    plan_id: str = field(default_factory=lambda: new_id("recovery"))
    created_at: str = field(default_factory=now_iso)
    source_modules: dict[str, list[str]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
