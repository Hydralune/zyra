from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import ArtifactRef, AgentMessage, new_id, now_iso


class WorkerRuntimeKind(StrEnum):
    PYTHON = "python"
    TYPESCRIPT_SIDECAR = "typescript_sidecar"
    VENDORED_ADAPTER = "vendored_adapter"


@dataclass(frozen=True, slots=True)
class WorkerRuntimeDescriptor:
    name: str
    kind: WorkerRuntimeKind
    source: str
    capabilities: tuple[str, ...]
    entrypoint: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    run_id: str
    task_id: str
    worker_name: str
    messages: list[AgentMessage] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: new_id("workerreq"))
    node_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    constraints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerResult:
    request_id: str
    ok: bool
    summary: str
    artifacts: list[ArtifactRef] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    completed_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)


def default_worker_descriptors() -> list[WorkerRuntimeDescriptor]:
    return [
        WorkerRuntimeDescriptor(
            name="CodeWorkerRuntime",
            kind=WorkerRuntimeKind.PYTHON,
            source="zyra-claude-productized",
            capabilities=("codebase-analysis", "code-change", "verification", "tool-permission", "compact", "mcp", "subagent"),
            entrypoint="packages/workers/zyra_workers/code_worker_runtime.py",
            metadata={
                "upstream_source": "claude-code-best",
                "runtime_id": "zyra-claude-code-productized-runtime",
                "sidecar_required_for_default_path": "false",
            },
        ),
        WorkerRuntimeDescriptor(
            name="BrowserWorker",
            kind=WorkerRuntimeKind.VENDORED_ADAPTER,
            source="vendor/browser-use",
            capabilities=("web-research", "browser-action", "browser-agent", "screenshot", "dom-state", "browser-trace"),
            entrypoint="packages/workers/zyra_workers/browser_worker.py",
        ),
    ]
