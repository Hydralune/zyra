from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable

from .ledger_models import (
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    SourceEvidence,
    TargetBinding,
    TestEntry,
)


OWNER_UNIT = "M1-02A"
PRIMARY_SOURCE_REPO = "claude-code-best"
SOURCE_POOL_PREFIX = "vendor-runtimes/claude-code-runtime/productized/claude-code-best/"
RETIREMENT_MANIFEST_PATH = (
    "docs/reviews/evidence/P2-S02A-01/"
    "legacy-source-pool-retirement-manifest.json"
)
RETIREMENT_OVERLAY_PATH = (
    "packages/integrations/zyra_integrations/data/"
    "legacy_source_retirement_overlay.json"
)
FOUNDATION_TEST_COMMAND = "python scripts/verify_claude_productization_foundation.py"
RETIRED_TARGET_PATHS = {
    "apps/code-worker/src/main.mjs": "apps/code-worker/src/main.ts",
    "tests/unit/test_tool_loop_budget_runtime.py": "packages/runtime/claude-runtime/test/runtime.test.ts",
    "packages/workers/zyra_workers/code_query_loop.py": "packages/runtime/claude-runtime/src/query-engine.ts",
    "packages/runtime/zyra_runtime/tool_runtime_foundation.py": "packages/runtime/claude-runtime/src/tools/execution-runtime.ts",
    "packages/runtime/zyra_runtime/permission/action_gate.py": "packages/runtime/claude-runtime/src/permission/coordinator.ts",
    "packages/runtime/zyra_runtime/permission/classifier.py": "packages/runtime/claude-runtime/src/permission/command-risk-runtime.ts",
    "packages/runtime/zyra_runtime/permission/evaluator.py": "packages/runtime/claude-runtime/src/permission/evaluator.ts",
    "packages/runtime/zyra_runtime/permission/extensions.py": "packages/runtime/claude-runtime/src/permission/settings-runtime.ts",
    "packages/runtime/zyra_runtime/permission/hooks.py": "packages/runtime/claude-runtime/src/permission/hook-runtime.ts",
    "packages/runtime/zyra_runtime/permission/modes.py": "packages/runtime/claude-runtime/src/permission/mode-runtime.ts",
    "packages/runtime/zyra_runtime/permission/risk.py": "packages/runtime/claude-runtime/src/permission/risk-runtime.ts",
    "packages/runtime/zyra_runtime/permission/rules.py": "packages/runtime/claude-runtime/src/permission/rule-index.ts",
    "packages/runtime/zyra_runtime/permission/shell_analysis.py": "packages/runtime/claude-runtime/src/permission/command-risk-runtime.ts",
    "packages/integrations/zyra_integrations/mcp/auth.py": "packages/integrations/claude-mcp/src/auth/oauth-runtime.ts",
    "packages/integrations/zyra_integrations/mcp/capabilities.py": "packages/integrations/claude-mcp/src/catalog/capability-catalog.ts",
    "packages/integrations/zyra_integrations/mcp/config.py": "packages/integrations/claude-mcp/src/config/config-store.ts",
    "packages/integrations/zyra_integrations/mcp/connection.py": "packages/integrations/claude-mcp/src/connection/connection-runtime.ts",
    "packages/integrations/zyra_integrations/mcp/elicitation.py": "packages/integrations/claude-mcp/src/runtime/elicitation-runtime.ts",
    "packages/integrations/zyra_integrations/mcp/instructions.py": "packages/integrations/claude-mcp/src/projection/instruction-runtime.ts",
    "packages/integrations/zyra_integrations/mcp/projection.py": "packages/integrations/claude-mcp/src/projection/tool-projection.ts",
    "packages/integrations/zyra_integrations/mcp/protocol.py": "packages/integrations/claude-mcp/src/core/protocol.ts",
    "packages/integrations/zyra_integrations/mcp/sampling.py": "packages/integrations/claude-mcp/src/runtime/sampling-runtime.ts",
    "packages/integrations/zyra_integrations/mcp/tasks.py": "packages/integrations/claude-mcp/src/runtime/task-runtime.ts",
    "packages/integrations/zyra_integrations/mcp/transport.py": "packages/integrations/claude-mcp/src/transport.ts",
}


def normalize_ledger_for_current_policy(entries: Iterable[InternalizationLedgerEntry]) -> list[InternalizationLedgerEntry]:
    normalized = [_normalize_entry(entry) for entry in entries]
    by_id = {entry.ledger_id: entry for entry in normalized}
    for entry in _foundation_entries():
        _normalize_entry(entry)
        by_id[entry.ledger_id] = entry
    current = list(by_id.values())
    if current_legacy_target_paths(current):
        raise ValueError(
            "current ledger normalization retained a retired legacy target"
        )
    return current


def _normalize_entry(entry: InternalizationLedgerEntry) -> InternalizationLedgerEntry:
    if _is_legacy_m1_02a_source_pool(entry):
        _downgrade_legacy_source_pool(entry)
    elif _is_legacy_m1_02a_connected_vendor(entry):
        _downgrade_legacy_connected_vendor(entry)
    _remap_retired_targets(entry)
    _strip_current_legacy_targets(entry)
    return entry


def _strip_current_legacy_targets(entry: InternalizationLedgerEntry) -> None:
    historical = [
        binding.target_path
        for binding in entry.target_bindings
        if _is_legacy_target(binding.target_path)
    ]
    if historical:
        entry.metadata["historical_legacy_target_count"] = len(historical)
        entry.metadata["historical_target_provenance"] = RETIREMENT_MANIFEST_PATH
        entry.metadata.pop("source_pool_target", None)
    entry.target_bindings = [
        binding
        for binding in entry.target_bindings
        if not _is_legacy_target(binding.target_path)
    ]
    if historical and not entry.target_bindings:
        entry.target_bindings = [
            TargetBinding(
                RETIREMENT_OVERLAY_PATH,
                role="historical_evidence",
                required_for_main_path=False,
                must_exist_for_statuses=[],
            )
        ]
    entry.main_path.surfaces = [
        path for path in entry.main_path.surfaces if not _is_legacy_target(path)
    ]
    entry.runtime_entry.config_refs = [
        path for path in entry.runtime_entry.config_refs if not _is_legacy_target(path)
    ]
    if _is_legacy_target(entry.runtime_entry.command):
        entry.runtime_entry.command = "python scripts/zyra_source_extract.py status"
    if _is_legacy_target(entry.runtime_entry.health_check):
        entry.runtime_entry.health_check = "python scripts/zyra_source_extract.py status"


def _is_legacy_target(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized.startswith(("vendor/", "vendor-runtimes/"))


def current_legacy_target_paths(
    entries: Iterable[InternalizationLedgerEntry],
) -> list[str]:
    paths: list[str] = []
    for entry in entries:
        paths.extend(
            binding.target_path
            for binding in entry.target_bindings
            if _is_legacy_target(binding.target_path)
        )
        paths.extend(
            path
            for path in entry.main_path.surfaces
            if _is_legacy_target(path)
        )
        paths.extend(
            path
            for path in entry.runtime_entry.config_refs
            if _is_legacy_target(path)
        )
    return sorted(set(paths))


def _remap_retired_targets(entry: InternalizationLedgerEntry) -> None:
    """Point historical rows at the current Zyra-owned runtime owners."""

    def remap(value: str) -> str:
        result = value
        for retired, current in RETIRED_TARGET_PATHS.items():
            result = result.replace(retired, current)
        return result

    for binding in entry.target_bindings:
        binding.target_path = remap(binding.target_path)
    entry.runtime_entry.command = remap(entry.runtime_entry.command)
    entry.runtime_entry.health_check = remap(entry.runtime_entry.health_check)
    entry.runtime_entry.config_refs = [remap(value) for value in entry.runtime_entry.config_refs]
    entry.main_path.surfaces = [remap(value) for value in entry.main_path.surfaces]


def _is_legacy_m1_02a_source_pool(entry: InternalizationLedgerEntry) -> bool:
    if entry.owner_unit != OWNER_UNIT:
        return False
    paths = [
        str(entry.metadata.get("source_pool_target") or ""),
        *entry.target_paths,
        entry.primary_target_path,
    ]
    if any(_is_productized_source_pool_path(path) for path in paths):
        return True
    if any("claude-code-runtime-productized" in tag or "source-pool" in tag for tag in entry.tags):
        return True
    return False


def _is_legacy_m1_02a_connected_vendor(entry: InternalizationLedgerEntry) -> bool:
    if entry.owner_unit != OWNER_UNIT:
        return False
    if entry.migration_strategy != MigrationStrategy.VENDORED_RUNTIME:
        return False
    if entry.main_path_status not in {
        MainPathStatus.WORKER_RUNTIME_CONNECTED,
        MainPathStatus.API_CONNECTED,
        MainPathStatus.EVENT_LOG_CONNECTED,
        MainPathStatus.CONTROL_COMMAND_CONNECTED,
        MainPathStatus.TESTED_MAIN_PATH,
    }:
        return False
    return bool(entry.metadata.get("legacy")) or any(tag.startswith("legacy-") for tag in entry.tags)


def _is_productized_source_pool_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith(SOURCE_POOL_PREFIX) or "/productized/claude-code-best/" in normalized


def _downgrade_legacy_source_pool(entry: InternalizationLedgerEntry) -> None:
    entry.lifecycle = LedgerLifecycle.CANDIDATE
    entry.main_path_status = MainPathStatus.INVENTORIED
    entry.migration_strategy = MigrationStrategy.CANDIDATE_REVIEW
    entry.line_count_policy = LineCountPolicy.EXCLUDED_INVENTORY_ONLY
    entry.metadata["effective_line_count"] = 0
    entry.metadata["effective_target_count"] = 0
    entry.main_path = MainPathBinding()
    entry.runtime_entry = RuntimeEntry(
        command="python scripts/zyra_source_extract.py status",
        module="zyra_integrations.source_extraction",
        function="LegacySourcePoolRetiredError",
        protocol="historical-source-retirement-v1",
        health_check="python scripts/zyra_source_extract.py status",
        config_refs=[
            "packages/integrations/zyra_integrations/source_extraction.py",
            "packages/runtime/zyra_runtime/claude_productization_foundation.py",
        ],
    )
    historical_targets = _source_pool_targets(entry)
    entry.metadata["historical_legacy_target_count"] = len(
        historical_targets or [entry.source_path]
    )
    entry.metadata["historical_target_provenance"] = RETIREMENT_MANIFEST_PATH
    entry.metadata.pop("source_pool_target", None)
    entry.target_bindings = [
        TargetBinding(
            RETIREMENT_OVERLAY_PATH,
            role="historical_evidence",
            required_for_main_path=False,
            must_exist_for_statuses=[],
        )
    ]
    entry.test_entries = [
        TestEntry(
            path="tests/integration/test_claude_code_productized_runtime.py",
            command="python -m unittest tests.integration.test_claude_code_productized_runtime",
            kind="integration",
            expected_signal="legacy productized source-pool inventory remains readable but does not count as deep internalization",
        ),
        TestEntry(
            path="tests/unit/test_claude_productization_foundation.py",
            command="python -m unittest tests.unit.test_claude_productization_foundation",
            kind="unit",
            expected_signal="Zyra-owned foundation surfaces carry connected main-path evidence",
        ),
    ]
    if entry.license_notice is None:
        entry.license_notice = LicenseNotice(
            source_repo=entry.source_repo,
            status=NoticeStatus.RECORDED,
            license_hint="Historical source evidence only; runtime ownership is carried by Zyra modules.",
            notice_path=RETIREMENT_MANIFEST_PATH,
            notes="Legacy M1-02A productized copies are frozen at the retirement base commit.",
        )
    if "source-pool-evidence" not in entry.tags:
        entry.tags.append("source-pool-evidence")
    if "legacy-m1-02a-downgraded" not in entry.tags:
        entry.tags.append("legacy-m1-02a-downgraded")
    note = (
        "Legacy M1-02A productized upstream file is retained as source-pool evidence only; "
        "connected runtime claims must point at packages/apps/scripts Zyra-owned foundation targets."
    )
    if note not in entry.risk_notes:
        entry.risk_notes.append(note)
    entry.replacement_plan = (
        "M1-02A source-pool files remain evidence for source custody. The effective foundation is "
        "implemented by zyra_runtime.claude_productization_foundation, zyra_workers.claude_foundation_worker, "
        "apps/code-worker protocol surfaces, and behavior tests."
    )


def _downgrade_legacy_connected_vendor(entry: InternalizationLedgerEntry) -> None:
    entry.lifecycle = LedgerLifecycle.CANDIDATE
    entry.main_path_status = MainPathStatus.INVENTORIED
    entry.migration_strategy = MigrationStrategy.CANDIDATE_REVIEW
    entry.line_count_policy = LineCountPolicy.EXCLUDED_INVENTORY_ONLY
    entry.metadata["effective_line_count"] = 0
    entry.metadata["effective_target_count"] = 0
    entry.main_path = MainPathBinding()
    entry.runtime_entry = RuntimeEntry(
        command="python scripts/verify_code_worker_sidecar.py",
        module="zyra_workers.code_worker_bridge",
        function="CodeWorkerSidecarClient.runtime_inventory",
        protocol="legacy-code-worker-inventory-evidence-v1",
        health_check="python scripts/verify_code_worker_sidecar.py",
        config_refs=[
            "apps/code-worker/src/main.mjs",
            "packages/workers/zyra_workers/code_worker_bridge.py",
            "packages/runtime/zyra_runtime/claude_productization_foundation.py",
        ],
    )
    entry.target_bindings = [
        TargetBinding(
            path,
            role="legacy_zyra_surface",
            required_for_main_path=False,
            must_exist_for_statuses=[],
        )
        for path in entry.target_paths
    ]
    if "legacy-m1-02a-downgraded" not in entry.tags:
        entry.tags.append("legacy-m1-02a-downgraded")
    note = (
        "Legacy M0 CodeWorker sidecar inventory is retained as historical evidence only; "
        "M1-02A connected runtime evidence is carried by Zyra-owned foundation ledger rows."
    )
    if note not in entry.risk_notes:
        entry.risk_notes.append(note)
    entry.replacement_plan = (
        "Use the M1-02A claude-productization-foundation entries for connected runtime accounting; "
        "this legacy row remains an inventoried bridge record and is excluded from effective source counts."
    )


def _source_pool_targets(entry: InternalizationLedgerEntry) -> list[str]:
    targets: list[str] = []
    for path in [*entry.target_paths, str(entry.metadata.get("source_pool_target") or "")]:
        if path and _is_productized_source_pool_path(path) and path not in targets:
            targets.append(path.replace("\\", "/"))
    return targets


def _foundation_entries() -> list[InternalizationLedgerEntry]:
    return [
        _foundation_entry(
            source_path="src/QueryEngine.ts",
            capability_name="query-engine-foundation-port",
            capability_summary="QueryEngine source is mapped to Zyra QuerySession and CodeQueryLoop state ports.",
            target_paths=[
                "packages/runtime/zyra_runtime/claude_productization_foundation.py",
                "packages/runtime/zyra_runtime/query_session.py",
                "packages/workers/zyra_workers/code_query_loop.py",
                "apps/code-worker/src/main.mjs",
            ],
            event_types=["query_session", "agent_message"],
            downstream_units=["M1-02B", "M1-02D", "M1-03A"],
            source_symbols=["QueryEngine", "QueryEngineConfig"],
        ),
        _foundation_entry(
            source_path="src/Tool.ts",
            capability_name="tool-loop-foundation-port",
            capability_summary="Claude Code Tool interface is mapped to Zyra ToolRegistry and ToolLoopScheduler.",
            target_paths=[
                "packages/runtime/zyra_runtime/tools.py",
                "packages/runtime/zyra_runtime/tool_loop.py",
                "packages/workers/zyra_workers/code_query_loop.py",
            ],
            event_types=["tool_batch_started", "tool_call_completed", "tool_result_budget_exceeded"],
            downstream_units=["M1-02C", "M1-03A", "M1-08"],
            source_symbols=["Tool", "ToolUse"],
        ),
        _foundation_entry(
            source_path="src/hooks/toolPermission",
            capability_name="permission-foundation-port",
            capability_summary="Claude Code permission pipeline is mapped to Zyra ToolPermissionPolicy and executor gate.",
            target_paths=[
                "packages/runtime/zyra_runtime/permissions.py",
                "packages/runtime/zyra_runtime/executor.py",
                "packages/workers/zyra_workers/code_query_loop.py",
            ],
            event_types=["tool_permission_requested", "tool_permission_decision", "tool_call_denied"],
            downstream_units=["M1-03A", "M1-04C"],
            source_symbols=["canUseTool", "permission"],
            control_commands=["permission.allow", "permission.deny"],
        ),
        _foundation_entry(
            source_path="src/services/mcp",
            capability_name="mcp-foundation-boundary",
            capability_summary="Claude Code MCP service source is mapped to Zyra integration and runtime scaffold contracts.",
            target_paths=[
                "packages/integrations/zyra_integrations/source_extraction.py",
                "packages/runtime/zyra_runtime/scaffold.py",
                "scripts/zyra_source_extract.py",
            ],
            event_types=["mcp_server_loaded", "mcp_tool_discovered"],
            downstream_units=["M1-03B", "M1-08"],
            source_symbols=["MCP", "resources", "prompts"],
            control_commands=["mcp.list", "mcp.disable"],
        ),
        _foundation_entry(
            source_path="src/commands.ts",
            capability_name="command-control-foundation",
            capability_summary="Claude Code command organization is mapped to Zyra ControlCommand and code-worker protocol surfaces.",
            target_paths=[
                "packages/runtime/zyra_runtime/control.py",
                "apps/code-worker/src/main.mjs",
                "packages/workers/zyra_workers/code_worker_bridge.py",
            ],
            event_types=["control_command", "agent_message"],
            downstream_units=["M1-02D", "M1-03D", "M2-04A"],
            source_symbols=["Command", "commands"],
            control_commands=["context.show", "compact.run", "session.resume"],
        ),
        _foundation_entry(
            source_path="src/services/compact",
            capability_name="context-compact-foundation",
            capability_summary="Claude Code compact/context source is mapped to Zyra session snapshot and compaction event ports.",
            target_paths=[
                "packages/runtime/zyra_runtime/session.py",
                "packages/runtime/zyra_runtime/query_session.py",
                "packages/workers/zyra_workers/code_query_loop.py",
            ],
            event_types=["context_compacted", "query_session_snapshot"],
            downstream_units=["M1-02D", "M1-06C", "M2-04B"],
            source_symbols=["compact", "autoCompact", "sessionRestore"],
        ),
        _foundation_entry(
            source_path="src/tools/SkillTool",
            capability_name="skill-subagent-foundation",
            capability_summary="Claude Code SkillTool/AgentTool source is reserved as Zyra skill and subagent runtime boundary.",
            target_paths=[
                "packages/runtime/zyra_runtime/scaffold.py",
                "packages/workers/zyra_workers/runtime_scaffold.py",
                "packages/workers/zyra_workers/code_worker_runtime.py",
            ],
            event_types=["skill_loaded", "subagent_invoked"],
            downstream_units=["M1-03C", "M1-03D", "M1-07A"],
            source_symbols=["SkillTool", "AgentTool", "forkedAgent"],
        ),
        _foundation_entry(
            source_path="src/cli.ts",
            capability_name="code-worker-ingress-foundation",
            capability_summary="Claude Code CLI/session ingress is mapped to Zyra code-worker line protocol and sidecar client.",
            target_paths=[
                "apps/code-worker/src/main.mjs",
                "packages/workers/zyra_workers/code_worker_bridge.py",
                "packages/workers/zyra_workers/claude_foundation_worker.py",
                "scripts/verify_claude_productization_foundation.py",
            ],
            event_types=["code_worker_health", "agent_message"],
            downstream_units=["M1-02B", "M1-02C", "M1-08"],
            source_symbols=["cli", "doctor", "session"],
            control_commands=["code_worker.health"],
        ),
    ]


def _foundation_entry(
    *,
    source_path: str,
    capability_name: str,
    capability_summary: str,
    target_paths: list[str],
    event_types: list[str],
    downstream_units: list[str],
    source_symbols: list[str],
    control_commands: list[str] | None = None,
) -> InternalizationLedgerEntry:
    entry = InternalizationLedgerEntry.new(
        source_repo=PRIMARY_SOURCE_REPO,
        source_path=source_path,
        capability_name=capability_name,
        capability_summary=capability_summary,
        target_paths=target_paths,
        migration_strategy=MigrationStrategy.REIMPLEMENTED_PATTERN,
        main_path_status=MainPathStatus.WORKER_RUNTIME_CONNECTED,
        lifecycle=LedgerLifecycle.INTERNALIZED,
        owner_unit=OWNER_UNIT,
        milestone="M1",
        runtime_entry=RuntimeEntry(
            command=FOUNDATION_TEST_COMMAND,
            module="zyra_runtime.claude_productization_foundation",
            function="ClaudeProductizationFoundation.inspect",
            protocol="zyra-claude-productization-foundation-v1",
            health_check=FOUNDATION_TEST_COMMAND,
            config_refs=[
                "packages/runtime/zyra_runtime/claude_productization_foundation.py",
                "packages/workers/zyra_workers/claude_foundation_worker.py",
                "apps/code-worker/src/main.mjs",
            ],
        ),
        test_entries=[
            TestEntry(
                path="tests/unit/test_claude_productization_foundation.py",
                command="python -m unittest tests.unit.test_claude_productization_foundation",
                kind="unit",
                expected_signal="foundation runtime reports active Zyra-owned boundaries and rejects source-pool main paths",
            ),
            TestEntry(
                path="tests/integration/test_claude_productization_foundation_cli.py",
                command="python -m unittest tests.integration.test_claude_productization_foundation_cli",
                kind="integration",
                expected_signal="CLI and worker probe exercise the same foundation runtime",
            ),
        ],
        main_path=MainPathBinding(
            surfaces=[
                "packages/runtime/zyra_runtime/claude_productization_foundation.py",
                "packages/workers/zyra_workers/claude_foundation_worker.py",
                "apps/code-worker/src/main.mjs",
            ],
            event_types=event_types,
            control_commands=list(control_commands or []),
            artifact_kinds=["trace", "structured_data"],
            worker_runtime="CodeWorkerRuntime:claude-productization-foundation",
        ),
        line_count_policy=LineCountPolicy.COUNTS_AS_RUNTIME,
        license_notice=LicenseNotice(
            source_repo=PRIMARY_SOURCE_REPO,
            status=NoticeStatus.RECORDED,
            license_hint="Primary source repository is claude-code-best; this entry records Zyra-owned semantic port code.",
            notice_path=RETIREMENT_MANIFEST_PATH,
            notes="No historical source repository is loaded or counted as current runtime source.",
        ),
        source_owner_unit=OWNER_UNIT,
    )
    entry.downstream_units = list(downstream_units)
    entry.source_evidence = [
        SourceEvidence(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path=source_path,
            exists_in_workspace=False,
            source_kind="frozen_git_object",
            reason="Historical primary source frozen by the P2-S02A-01 retirement manifest.",
            symbols=list(source_symbols),
            tags=["m1-02a", "claude-code-best", "primary-source"],
        ),
        SourceEvidence(
            source_repo="claudecode-related/claude-reviews-claude",
            source_path=_reference_doc_for_source(source_path),
            exists_in_workspace=False,
            source_kind="reference_doc",
            reason="Reference-only checklist used to avoid missing Claude Code module boundaries.",
            symbols=[],
            tags=["reference-only", "not-counted"],
        ),
    ]
    entry.target_bindings = [
        TargetBinding(path, role="primary", required_for_main_path=True)
        for path in target_paths
        if not _is_productized_source_pool_path(path)
    ]
    entry.tags = [
        "m1-02a",
        "slice-02a-01",
        "claude-productization-foundation",
        "zyra-owned-runtime",
    ]
    entry.risk_notes = [
        "This entry carries connected main-path evidence for Zyra-owned modules only; productized/vendor source-pool rows are downgraded separately.",
    ]
    entry.replacement_plan = (
        "M1-02A foundation maps claude-code-best source concepts into Zyra runtime, worker, integration, script, "
        "event, artifact, and control surfaces. M1-02A integration and M1-02B/02C deepen behavior."
    )
    entry.metadata["reference_only_repos"] = ["claudecode-related/claude-reviews-claude", "claudecode-related/Dive-into-Claude-Code"]
    return entry


def _reference_doc_for_source(source_path: str) -> str:
    normalized = PurePosixPath(source_path).as_posix()
    if "Tool" in normalized or "tools" in normalized:
        return "architecture/zh-CN/02-tool-system.md"
    if "permission" in normalized.lower():
        return "architecture/zh-CN/07-permission-pipeline.md"
    if "compact" in normalized.lower() or "session" in normalized.lower():
        return "architecture/zh-CN/10-context-assembly.md"
    if "mcp" in normalized.lower() or "services" in normalized.lower():
        return "architecture/zh-CN/15-services-api-layer.md"
    return "architecture/zh-CN/01-query-engine.md"
