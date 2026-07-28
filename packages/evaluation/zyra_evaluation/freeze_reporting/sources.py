from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import require_mapping, require_sequence, safe_relative_path


CAPABILITY_TARGET_HINTS: dict[str, tuple[str, ...]] = {
    "browser_runtime": (
        "packages/workers/zyra_workers/browser_worker.py",
        "packages/workers/zyra_workers/browser_runtime",
    ),
    "code_worker_loop": (
        "packages/runtime/claude-runtime/src",
        "packages/workers/zyra_workers",
    ),
    "control_console": (
        "apps/web/src",
        "packages/commands",
    ),
    "dynamic_topology": (
        "packages/orchestration/zyra_orchestration",
        "packages/symbolic",
    ),
    "event_projection": (
        "packages/runtime/zyra_runtime",
        "apps/web/src",
    ),
    "exact_resume": (
        "packages/orchestration/zyra_orchestration",
    ),
    "fault_recovery": (
        "packages/scheduler",
        "packages/orchestration/zyra_orchestration",
    ),
    "hashline": (
        "packages/runtime/sandbox-gateway-control/src/hashline.ts",
        "apps/web/src/features/diff-review/hashline.ts",
    ),
    "mcp_runtime": (
        "packages/integrations/zyra_integrations/mcp",
        "packages/runtime/claude-runtime/src",
    ),
    "memory_retrieval": (
        "packages/memory/zyra_memory",
    ),
    "permission_runtime": (
        "packages/runtime/zyra_runtime/permission",
        "packages/runtime/claude-runtime/src",
    ),
    "provider_control": (
        "packages/runtime/zyra_runtime",
        "packages/integrations",
    ),
    "skill_subagent_runtime": (
        "packages/skills",
        "packages/runtime/claude-runtime/src",
    ),
    "workspace_sandbox": (
        "packages/workspace/zyra_workspace",
        "packages/orchestration/zyra_orchestration/deployment",
    ),
}

CAPABILITY_TEST_TERMS: dict[str, tuple[str, ...]] = {
    "browser_runtime": ("browser", "watchdog"),
    "code_worker_loop": ("code_worker", "typescript_runtime"),
    "control_console": ("command", "control", "web"),
    "dynamic_topology": ("dynamic_graph", "topology"),
    "event_projection": ("event", "projection"),
    "exact_resume": ("exact_resume", "checkpoint", "recovery"),
    "fault_recovery": ("fault", "recovery"),
    "hashline": ("hashline",),
    "mcp_runtime": ("mcp",),
    "memory_retrieval": ("memory", "retrieval"),
    "permission_runtime": ("permission",),
    "provider_control": ("provider",),
    "skill_subagent_runtime": ("skill", "subagent"),
    "workspace_sandbox": ("workspace", "sandbox"),
}

OPENCODE_REQUIRED_CAPABILITIES = (
    "v2-durable-session-event",
    "v1-provider-product-loop",
    "typed-protocol",
    "app-session-ui",
    "tui-control",
    "web-desktop",
    "permission-question",
    "terminal-review-diff",
    "mcp-skill-command-plugin",
    "task-background-location-worktree",
)

OMP_REQUIRED_CAPABILITIES = (
    "agentloop-session",
    "permission-mcp-skills",
    "tasktool-pal",
    "mnemopi-compact",
    "provider-rpc-acp",
    "hashline",
    "tui-native",
    "roboomp",
    "snapcompact",
)

LANGGRAPH_REQUIRED_CAPABILITIES = (
    "open-world-topology",
    "immutable-branch-delta",
    "deterministic-commit",
    "codeworker-reasoning-cohesion",
    "checkpoint-identity-lineage",
    "pending-committed-writes",
    "side-effect-fence",
    "exact-resume",
    "stategraph-pregel-inactive",
    "generic-channel-reducer-inactive",
    "toolnode-stream-store-inactive",
    "sdk-server-inactive",
)


def existing_targets(
    repository_root: str | Path,
    capability: str,
) -> list[str]:
    root = Path(repository_root).resolve(strict=True)
    values = []
    for relative in CAPABILITY_TARGET_HINTS.get(capability, ()):
        path = root / relative
        if path.exists():
            values.append(safe_relative_path(relative))
    return values


def behavior_tests(
    repository_root: str | Path,
    capability: str,
    *,
    limit: int = 8,
) -> list[str]:
    root = Path(repository_root).resolve(strict=True)
    terms = CAPABILITY_TEST_TERMS.get(capability, (capability.replace("-", "_"),))
    candidates: list[tuple[int, str]] = []
    for path in (root / "tests").rglob("test_*.py"):
        lowered = path.name.lower()
        score = sum(1 for term in terms if term in lowered)
        if score:
            candidates.append((score, path.relative_to(root).as_posix()))
    for path in (root / "apps" / "web" / "src").rglob("*.test.ts*"):
        lowered = path.name.lower()
        score = sum(1 for term in terms if term in lowered)
        if score:
            candidates.append((score, path.relative_to(root).as_posix()))
    for path in (root / "packages").rglob("*.test.ts*"):
        lowered = path.name.lower()
        score = sum(1 for term in terms if term in lowered)
        if score:
            candidates.append((score, path.relative_to(root).as_posix()))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [path for _score, path in candidates[:limit]]


def expand_source_capabilities(
    source_row: Mapping[str, Any],
) -> list[dict[str, Any]]:
    selected = require_mapping(source_row, "custody source row")
    source_id = str(selected.get("source_repo") or "")
    active = [
        str(item)
        for item in require_sequence(
            selected.get("active_capabilities") or [],
            "active capabilities",
        )
    ]
    inactive = [
        str(item)
        for item in require_sequence(
            selected.get("inactive_capabilities") or [],
            "inactive capabilities",
        )
    ]
    roles = [
        str(item)
        for item in require_sequence(selected.get("roles") or [], "source roles")
    ]
    default_active_role = next(
        (
            role
            for role in roles
            if role in {
                "primary_implementation",
                "supplementary_implementation",
            }
        ),
        "supplementary_implementation",
    )
    default_inactive_role = next(
        (
            role
            for role in roles
            if role
            not in {
                "primary_implementation",
                "supplementary_implementation",
            }
        ),
        "reference_only",
    )
    return [
        {
            "source_id": source_id,
            "capability": capability,
            "role": default_active_role,
            "active": True,
        }
        for capability in active
    ] + [
        {
            "source_id": source_id,
            "capability": capability,
            "role": default_inactive_role,
            "active": False,
        }
        for capability in inactive
    ]


def expected_specialized_rows(source_id: str) -> tuple[str, ...]:
    lowered = source_id.lower()
    if lowered == "opencode":
        return OPENCODE_REQUIRED_CAPABILITIES
    if lowered in {"oh-my-pi", "omp"}:
        return OMP_REQUIRED_CAPABILITIES
    if lowered == "langgraph":
        return LANGGRAPH_REQUIRED_CAPABILITIES
    return ()
