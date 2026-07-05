from __future__ import annotations

from copy import deepcopy
from typing import Any


M5_SOURCE_TO_TARGET_LEDGER: list[dict[str, Any]] = [
    {
        "source_repo": "OpenHands",
        "source_modules": [
            "openhands/runtime",
            "openhands/controller/agent_controller.py",
            "openhands/events",
            "openhands/storage",
        ],
        "target_paths": [
            "packages/scheduler/zyra_scheduler/backends.py",
            "packages/scheduler/zyra_scheduler/pool.py",
            "apps/api/zyra_api/main.py",
        ],
        "internalized_capability": "workspace gateway, backend envelope, event-first runtime state.",
        "main_path": ["worker dispatch metadata", "API scheduler health", "event log"],
    },
    {
        "source_repo": "openclaw",
        "source_modules": [
            "docs/concepts/active-memory.md",
            "docs/concepts/compaction.md",
            "docs/tools/trajectory.md",
            "runtime fault/recovery concepts",
        ],
        "target_paths": [
            "packages/scheduler/zyra_scheduler/scheduler.py",
            "packages/scheduler/zyra_scheduler/recovery.py",
            "packages/symbolic/zyra_symbolic/control.py",
        ],
        "internalized_capability": "failure-aware active memory, trajectory-preserving recovery route.",
        "main_path": ["MemoryFabric signals", "failure injection", "requirement change replan"],
    },
    {
        "source_repo": "agentscope",
        "source_modules": [
            "src/agentscope/manager",
            "src/agentscope/service",
            "src/agentscope/pipelines",
        ],
        "target_paths": [
            "packages/scheduler/zyra_scheduler/pool.py",
            "packages/scheduler/zyra_scheduler/scheduler.py",
        ],
        "internalized_capability": "worker manifest registry, capability matching, resource profile scoring.",
        "main_path": ["TopologyRouter", "WorkerManifest", "ResourceScheduler"],
    },
    {
        "source_repo": "browser-use",
        "source_modules": [
            "browser_use/browser/session.py",
            "browser_use/agent/service.py",
            "browser_use/controller/service.py",
        ],
        "target_paths": [
            "packages/scheduler/zyra_scheduler/watchdog.py",
            "packages/scheduler/zyra_scheduler/pool.py",
            "packages/workers/zyra_workers/browser_worker.py",
        ],
        "internalized_capability": "browser worker manifest, simulated edge backend, browser failure classification.",
        "main_path": ["BrowserWorker route", "watchdog", "API worker health"],
    },
    {
        "source_repo": "agent-framework",
        "source_modules": [
            "docs/decisions/0019-python-context-compaction-strategy.md",
            "middleware / workflow recovery patterns",
        ],
        "target_paths": [
            "packages/scheduler/zyra_scheduler/recovery.py",
            "packages/scheduler/zyra_scheduler/scheduler.py",
            "packages/memory/zyra_memory/fabric.py",
        ],
        "internalized_capability": "middleware-like routing signals, compact-aware recovery context.",
        "main_path": ["ResourceScheduler signals", "MemoryFabric", "RecoveryPlanner"],
    },
    {
        "source_repo": "claude-code-best",
        "source_modules": [
            "QueryEngine / query loop",
            "tool permission runtime",
            "context compact",
            "AgentTool / background task",
            "slash commands",
        ],
        "target_paths": [
            "packages/runtime/zyra_runtime/permissions.py",
            "packages/runtime/zyra_runtime/workers.py",
            "packages/scheduler/zyra_scheduler/pool.py",
            "packages/scheduler/zyra_scheduler/watchdog.py",
            "packages/commands/zyra_commands/registry.py",
        ],
        "internalized_capability": "permission-governed code worker, command controlled runtime, watchdog-friendly tool loop metadata.",
        "main_path": ["CodeWorkerRuntime", "permission store", "control commands", "scheduler decisions"],
    },
]


def source_to_target_ledger() -> list[dict[str, Any]]:
    return deepcopy(M5_SOURCE_TO_TARGET_LEDGER)
