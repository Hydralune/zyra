from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import Maturity, SourceCoverageItem, SourceRole, StateCustodyEntry
from .coverage import SourceGraphTableReader


FOUNDATION_SCENARIO = "m1-foundation-query-permission-control"


@dataclass(frozen=True, slots=True)
class CapabilityBlueprint:
    capability_id: str
    source_repository: str
    source_paths: tuple[str, ...]
    source_language: str
    role: SourceRole
    target_paths: tuple[str, ...]
    target_language: str
    canonical_owner: str
    production_entries: tuple[str, ...]
    state_owners: tuple[str, ...]
    scenario_ids: tuple[str, ...]
    disable_probe_ids: tuple[str, ...]
    maturity: Maturity = Maturity.ACTIVE_REAL
    cleanroom_decision: str = "source-custodied in Zyra; no runtime path to the source workspace"
    limitations: tuple[str, ...] = ()
    next_owner: str = ""

    def materialize(self) -> SourceCoverageItem:
        return SourceCoverageItem(
            capability_id=self.capability_id,
            source_repository=self.source_repository,
            source_paths=self.source_paths,
            source_language=self.source_language,
            role=self.role,
            maturity=self.maturity,
            target_paths=self.target_paths,
            target_language=self.target_language,
            canonical_owner=self.canonical_owner,
            production_entries=self.production_entries,
            state_owners=self.state_owners,
            scenario_ids=self.scenario_ids,
            disable_probe_ids=self.disable_probe_ids,
            cleanroom_decision=self.cleanroom_decision,
            limitations=self.limitations,
            next_owner=self.next_owner,
        )


class CapabilityCatalogError(ValueError):
    pass


class M1CapabilityCatalog:
    def __init__(self, project_root: str | Path, *, source_workspace: str | Path | None = None) -> None:
        self.root = Path(project_root).resolve()
        self.source_workspace = Path(source_workspace).resolve() if source_workspace else self.root.parent
        self.table_reader = SourceGraphTableReader()

    def items(self) -> tuple[SourceCoverageItem, ...]:
        blueprints = list(self._core_blueprints())
        items = [item.materialize() for item in blueprints]
        items.extend(self._omp_decision_items(items))
        self._validate(items)
        return tuple(sorted(items, key=lambda item: (item.capability_id, item.source_repository, item.role.value)))

    def required_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted({item.capability_id for item in self._core_blueprints()}))

    def known_disable_probe_ids(self) -> tuple[str, ...]:
        return tuple(sorted({probe for item in self.items() for probe in item.disable_probe_ids}))

    def source_summary(self) -> dict[str, Any]:
        items = self.items()
        by_repository = Counter(item.source_repository for item in items)
        by_role = Counter(item.role.value for item in items)
        by_maturity = Counter(item.maturity.value for item in items)
        capabilities: dict[str, list[dict[str, str]]] = defaultdict(list)
        for item in items:
            capabilities[item.capability_id].append(
                {
                    "source_repository": item.source_repository,
                    "role": item.role.value,
                    "maturity": item.maturity.value,
                    "canonical_owner": item.canonical_owner,
                }
            )
        return {
            "item_count": len(items),
            "capability_count": len(capabilities),
            "by_repository": dict(sorted(by_repository.items())),
            "by_role": dict(sorted(by_role.items())),
            "by_maturity": dict(sorted(by_maturity.items())),
            "capabilities": dict(sorted(capabilities.items())),
        }

    def _validate(self, items: Sequence[SourceCoverageItem]) -> None:
        duplicates = Counter(
            (item.capability_id, item.source_repository, item.role.value, item.source_paths)
            for item in items
        )
        repeated = [key for key, count in duplicates.items() if count > 1]
        if repeated:
            raise CapabilityCatalogError(f"duplicate capability coverage rows: {repeated}")
        by_capability: dict[str, list[SourceCoverageItem]] = defaultdict(list)
        for item in items:
            by_capability[item.capability_id].append(item)
            if item.role.production_bearing and not item.canonical_owner:
                raise CapabilityCatalogError(f"production capability lacks owner: {item.capability_id}")
            if item.role.production_bearing and not item.production_entries:
                raise CapabilityCatalogError(f"production capability lacks entry: {item.capability_id}")
            if item.maturity is Maturity.ACTIVE_REAL and not item.role.production_bearing:
                raise CapabilityCatalogError(f"inactive source role promoted to active: {item.capability_id}")
        for capability_id, values in by_capability.items():
            primary = [item for item in values if item.role is SourceRole.PRIMARY]
            supplementary = [item for item in values if item.role is SourceRole.SUPPLEMENTARY]
            if len(primary) > 1:
                raise CapabilityCatalogError(f"multiple primary sources for {capability_id}")
            if len(supplementary) > 2:
                raise CapabilityCatalogError(f"supplementary source limit exceeded for {capability_id}")

    def _core_blueprints(self) -> tuple[CapabilityBlueprint, ...]:
        foundation = (FOUNDATION_SCENARIO,)
        return (
            CapabilityBlueprint(
                "query-engine",
                "claude-code-best",
                ("src/query.ts", "src/queryEngine.ts", "src/session.ts"),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/runtime/claude-runtime/src/query-engine.ts", "packages/runtime/claude-runtime/src/query"),
                "typescript",
                "ClaudeRuntimeCore",
                (
                    "packages/runtime/claude-runtime/src/query-engine.ts::ClaudeRuntimeCore",
                    "packages/runtime/claude-runtime/src/query/lifecycle-runtime.ts::QueryLifecycleRuntime",
                ),
                ("task_session", "runtime_event"),
                (FOUNDATION_SCENARIO, "m1-query-mcp"),
                ("disable-codeworker",),
            ),
            CapabilityBlueprint(
                "tool-loop-budget",
                "claude-code-best",
                ("src/query.ts", "src/tools.ts", "src/toolUseContext.ts"),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/runtime/claude-runtime/src/loop", "packages/runtime/claude-runtime/src/tools"),
                "typescript",
                "ModelIterationRuntime",
                (
                    "packages/runtime/claude-runtime/src/loop/model-iteration-runtime.ts::ModelIterationRuntime",
                    "packages/runtime/claude-runtime/src/loop/tool-observation-budget-runtime.ts::ToolObservationBudgetRuntime",
                ),
                ("task_session", "runtime_event"),
                foundation,
                ("disable-codeworker",),
            ),
            CapabilityBlueprint(
                "permission-runtime",
                "claude-code-best",
                ("src/permissions", "src/hooks", "src/classifier"),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/runtime/claude-runtime/src/permission",),
                "typescript",
                "PermissionCoordinator",
                (
                    "packages/runtime/claude-runtime/src/permission/coordinator.ts::PermissionCoordinator",
                    "packages/runtime/claude-runtime/src/permission/evaluator.ts::PermissionEvaluator",
                ),
                ("permission",),
                foundation,
                ("disable-permission-runtime",),
            ),
            CapabilityBlueprint(
                "mcp-runtime",
                "claude-code-best",
                ("src/services/mcp",),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/integrations/claude-mcp/src",),
                "typescript",
                "McpClientRuntime",
                (
                    "packages/integrations/claude-mcp/src/runtime/client-runtime.ts::McpClientRuntime",
                    "packages/integrations/claude-mcp/src/runtime/coordinator.ts::McpRuntimeCoordinator",
                ),
                ("mcp_session", "runtime_event"),
                ("m1-query-mcp",),
                ("disable-mcp-runtime",),
            ),
            CapabilityBlueprint(
                "skill-runtime",
                "claude-code-best",
                ("src/skills", "src/SkillTool.ts"),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/runtime/claude-runtime/src/skills", "packages/skills/zyra_skills"),
                "typescript",
                "TypeScriptSkillRuntime",
                (
                    "packages/runtime/claude-runtime/src/skills/runtime.ts::TypeScriptSkillRuntime",
                    "packages/skills/zyra_skills/task_integration.py::SkillTaskIntegrationRuntime",
                ),
                ("skill_revision", "skill_invocation"),
                ("m1-skill-memory-compact",),
                ("disable-skill-runtime",),
            ),
            CapabilityBlueprint(
                "subagent-runtime",
                "claude-code-best",
                ("src/AgentTool.ts", "src/agents", "src/tasks"),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/runtime/claude-runtime/src/agents", "packages/runtime/claude-runtime/src/tasks"),
                "typescript",
                "AgentExecutionRuntime",
                (
                    "packages/runtime/claude-runtime/src/agents/execution-runtime.ts::AgentExecutionRuntime",
                    "packages/runtime/claude-runtime/src/tasks/registry.ts::DurableTaskRegistry",
                ),
                ("subagent_task", "worker_lease"),
                ("m1-subagent-worker-recovery",),
                ("disable-subagent-runtime",),
            ),
            CapabilityBlueprint(
                "subagent-runtime",
                "oh-my-pi",
                ("task/index.ts", "executor.ts", "AgentRegistry", "AsyncJobManager"),
                "typescript",
                SourceRole.SUPPLEMENTARY,
                ("packages/runtime/claude-runtime/src/omp-worker-control",),
                "typescript",
                "AgentExecutionRuntime",
                (
                    "packages/runtime/claude-runtime/src/omp-worker-control/dispatch-runtime.ts::OmpWorkerDispatchRuntime",
                ),
                ("subagent_task", "worker_lease"),
                ("m1-subagent-worker-recovery",),
                ("disable-subagent-runtime",),
            ),
            CapabilityBlueprint(
                "compact-restore",
                "claude-code-best",
                ("src/context", "src/compact", "src/session"),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/runtime/claude-runtime/src/compact", "packages/memory/skill-memory-runtime/src"),
                "typescript",
                "CompactionCustodyRuntime",
                (
                    "packages/runtime/claude-runtime/src/compact/compaction-custody-runtime.ts::CompactionSourceCustodyRuntime",
                    "packages/runtime/claude-runtime/src/compact/restore-runtime.ts::CompactRestoreRuntime",
                ),
                ("task_session", "memory"),
                ("m1-skill-memory-compact",),
                ("disable-compact-runtime",),
            ),
            CapabilityBlueprint(
                "browser-context",
                "browser-use",
                ("browser_use/agent/message_manager", "browser_use/browser"),
                "python",
                SourceRole.PRIMARY,
                ("packages/workers/zyra_workers/browser_context",),
                "python",
                "BrowserMessageManagerRuntime",
                (
                    "packages/workers/zyra_workers/browser_context/message_manager.py::BrowserMessageManagerRuntime",
                    "packages/workers/zyra_workers/browser_context/task_integration.py::BrowserContextTaskIntegrationRuntime",
                ),
                ("browser_turn", "artifact"),
                foundation,
                ("disable-browser-context",),
            ),
            CapabilityBlueprint(
                "browser-watchdog",
                "browser-use",
                ("browser_use/observability.py", "browser_use/browser/watchdogs"),
                "python",
                SourceRole.PRIMARY,
                ("packages/workers/zyra_workers/browser_observability",),
                "python",
                "BrowserWatchdogRegistry",
                (
                    "packages/workers/zyra_workers/browser_observability/watchdogs.py::BrowserWatchdogRegistry",
                    "packages/workers/zyra_workers/browser_observability/crash_detector.py::BrowserCrashDetector",
                ),
                ("fault_signal", "browser_session"),
                ("m1-subagent-worker-recovery",),
                ("disable-browser-watchdog",),
            ),
            CapabilityBlueprint(
                "workspace-gateway",
                "OpenHands",
                ("openhands/runtime", "openhands/events/action/files"),
                "python",
                SourceRole.PRIMARY,
                ("packages/workspace/zyra_workspace",),
                "python",
                "WorkspaceManagerRuntime",
                (
                    "packages/workspace/zyra_workspace/manager.py::WorkspaceManagerRuntime",
                    "packages/workspace/zyra_workspace/transactions.py::WorkspacePatchTransactionRuntime",
                ),
                ("workspace", "artifact"),
                foundation,
                ("disable-workspace-gateway",),
            ),
            CapabilityBlueprint(
                "workspace-gateway",
                "oh-my-pi",
                ("task/worktree.ts", "isolation-runner.ts", "pi-iso"),
                "typescript",
                SourceRole.SUPPLEMENTARY,
                ("packages/runtime/claude-runtime/src/isolation",),
                "typescript",
                "WorkspaceManagerRuntime",
                ("packages/runtime/claude-runtime/src/isolation/merge-runtime.ts::IsolationMergeRuntime",),
                ("workspace", "artifact"),
                foundation,
                ("disable-workspace-gateway",),
            ),
            CapabilityBlueprint(
                "runtime-events",
                "opencode",
                ("packages/opencode/src/session", "packages/opencode/src/event"),
                "typescript",
                SourceRole.SUPPLEMENTARY,
                ("packages/runtime/claude-runtime/src/protocol", "packages/core/zyra_core"),
                "typescript",
                "SQLiteStore",
                (
                    "packages/runtime/claude-runtime/src/protocol/effect-runtime.ts::EffectProtocolRuntime",
                    "packages/core/zyra_core/event_log.py::append_event",
                ),
                ("runtime_event",),
                (FOUNDATION_SCENARIO, "m1-api-stream-fallback"),
                ("disable-runtime-events",),
            ),
            CapabilityBlueprint(
                "provider-control",
                "opencode",
                ("packages/opencode/src/provider",),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/runtime/claude-runtime/src/provider", "packages/scheduler/zyra_scheduler/backend_registry"),
                "typescript",
                "ProviderRequestRuntime",
                (
                    "packages/runtime/claude-runtime/src/provider/request-runtime.ts::ProviderRequestRuntime",
                    "packages/runtime/claude-runtime/src/provider/routing-runtime.ts::ProviderRoutingRuntime",
                ),
                ("provider_backend",),
                ("m1-api-stream-fallback",),
                ("disable-provider-control",),
            ),
            CapabilityBlueprint(
                "provider-control",
                "oh-my-pi",
                ("packages/ai/providers", "catalog", "model registry", "retry"),
                "typescript",
                SourceRole.SUPPLEMENTARY,
                ("packages/runtime/claude-runtime/src/provider",),
                "typescript",
                "ProviderRequestRuntime",
                ("packages/runtime/claude-runtime/src/provider/transport-runtime.ts::ProviderTransportRuntime",),
                ("provider_backend",),
                ("m1-api-stream-fallback",),
                ("disable-provider-control",),
            ),
            CapabilityBlueprint(
                "memory-retrieval",
                "oh-my-pi",
                ("mnemopi/store", "mnemopi/recall", "mnemopi/vector", "mnemopi/mmr"),
                "typescript",
                SourceRole.PRIMARY,
                (
                    "packages/memory/retrieval-algorithms/src",
                    "packages/memory/zyra_memory",
                    "packages/workers/zyra_workers/retrieval_context_runtime.py",
                ),
                "typescript",
                "WorkerRetrievalContextRuntime",
                (
                    "packages/memory/retrieval-algorithms/src/ranking.ts::rankRetrievalCandidates",
                    "packages/workers/zyra_workers/retrieval_context_runtime.py::WorkerRetrievalContextRuntime",
                ),
                ("memory", "code_index"),
                ("m1-skill-memory-compact",),
                ("disable-memory-retrieval",),
            ),
            CapabilityBlueprint(
                "memory-curator",
                "oh-my-pi",
                ("coding memories job queue", "mnemopi consolidation"),
                "typescript",
                SourceRole.PRIMARY,
                (
                    "packages/memory/curator-state-machine/src",
                    "packages/memory/zyra_memory",
                    "packages/workers/zyra_workers/memory_curator.py",
                ),
                "typescript",
                "MemoryCuratorWorkerRuntime",
                (
                    "packages/memory/curator-state-machine/src/job-state-machine.ts::transitionJob",
                    "packages/workers/zyra_workers/memory_curator.py::MemoryCuratorWorkerRuntime",
                ),
                ("memory", "curator_job"),
                ("m1-skill-memory-compact",),
                ("disable-memory-curator",),
            ),
            CapabilityBlueprint(
                "worker-pool",
                "AgentScope",
                ("src/agentscope/runtime", "src/agentscope/message"),
                "python",
                SourceRole.PRIMARY,
                ("packages/scheduler/zyra_scheduler/worker_pool",),
                "python",
                "WorkerPoolIntegrationRuntime",
                (
                    "packages/scheduler/zyra_scheduler/worker_pool/integration.py::WorkerPoolIntegrationRuntime",
                    "packages/scheduler/zyra_scheduler/worker_pool/leases.py::WorkerLeaseManager",
                ),
                ("worker_lease", "worker_inbox"),
                ("m1-subagent-worker-recovery",),
                ("disable-worker-pool",),
            ),
            CapabilityBlueprint(
                "worker-pool",
                "oh-my-pi",
                ("AsyncJob", "AgentRegistry", "PAL lifecycle", "roboomp WorkerPool"),
                "typescript",
                SourceRole.SUPPLEMENTARY,
                ("packages/runtime/claude-runtime/src/omp-worker-control",),
                "typescript",
                "WorkerPoolIntegrationRuntime",
                ("packages/runtime/claude-runtime/src/omp-worker-control/job-manager.ts::OmpAsyncJobProjectionManager",),
                ("worker_lease", "worker_inbox"),
                ("m1-subagent-worker-recovery",),
                ("disable-worker-pool",),
            ),
            CapabilityBlueprint(
                "fault-observation",
                "browser-use",
                ("browser_use/agent/watchdog", "browser_use/browser/session"),
                "python",
                SourceRole.PRIMARY,
                ("packages/scheduler/zyra_scheduler/fault_runtime",),
                "python",
                "RuntimeFaultObservationPort",
                (
                    "packages/scheduler/zyra_scheduler/fault_runtime/observation_port.py::RuntimeFaultObservationPort",
                    "packages/scheduler/zyra_scheduler/fault_runtime/runtime.py::FaultRuntimeApplication",
                ),
                ("fault_signal",),
                ("m1-subagent-worker-recovery",),
                ("disable-fault-observation",),
            ),
            CapabilityBlueprint(
                "recovery-planner",
                "OpenHands",
                ("openhands/runtime/recovery", "openhands/controller/state"),
                "python",
                SourceRole.PRIMARY,
                ("packages/scheduler/zyra_scheduler/recovery_runtime",),
                "python",
                "RecoveryRuntimeApplication",
                (
                    "packages/scheduler/zyra_scheduler/recovery_runtime/application.py::RecoveryApplication",
                    "packages/scheduler/zyra_scheduler/recovery_runtime/exact_recovery_runtime.py::ExactRecoveryRuntime",
                ),
                ("recovery", "graph_topology"),
                ("m1-subagent-worker-recovery",),
                ("disable-recovery-planner",),
            ),
            CapabilityBlueprint(
                "graph-custody",
                "LangGraph",
                ("libs/langgraph/checkpoint", "pregel/write.py"),
                "python",
                SourceRole.CONFORMANCE,
                (),
                "python",
                "GraphStateCustody",
                (),
                (),
                (),
                (),
                maturity=Maturity.CONFORMANCE_VERIFIED,
                cleanroom_decision="semantic conformance only; no LangGraph runtime dependency",
                limitations=("StateGraph, Pregel, reducers, ToolNode, SDK and deploy remain non-owners",),
            ),
            CapabilityBlueprint(
                "graph-custody",
                "Zyra-owned",
                ("dynamic topology and immutable commit requirements",),
                "python",
                SourceRole.PRIMARY,
                ("packages/orchestration/zyra_orchestration/graph_custody",),
                "python",
                "GraphStateCustody",
                (
                    "packages/orchestration/zyra_orchestration/graph_custody/runtime.py::GraphStateCustody",
                    "packages/orchestration/zyra_orchestration/graph_custody/store.py::GraphStateStore",
                ),
                ("graph_topology",),
                ("m1-subagent-worker-recovery",),
                ("disable-graph-custody",),
            ),
            CapabilityBlueprint(
                "code-index",
                "OpenHands",
                ("openhands/runtime/plugins/agent_skills", "openhands/microagent"),
                "python",
                SourceRole.SUPPLEMENTARY,
                ("packages/code_index/zyra_code_index",),
                "python",
                "CodeIndexService",
                (
                    "packages/code_index/zyra_code_index/service.py::CodeIndexApiService",
                    "packages/code_index/zyra_code_index/runtime.py::CodeIndexRuntime",
                ),
                ("code_index",),
                foundation,
                ("disable-code-index",),
            ),
            CapabilityBlueprint(
                "control-commands",
                "claude-code-best",
                ("src/commands", "src/cli"),
                "typescript",
                SourceRole.PRIMARY,
                ("packages/commands/zyra_commands/runtime",),
                "python",
                "RuntimeControlDispatcher",
                (
                    "packages/commands/zyra_commands/runtime/dispatcher.py::RuntimeControlDispatcher",
                    "packages/commands/zyra_commands/runtime/control_hub.py::StructuredControlHub",
                ),
                ("task_session", "runtime_event"),
                (FOUNDATION_SCENARIO, "m1-control-session-mutation"),
                ("disable-control-command",),
            ),
        )

    def _omp_decision_items(self, existing: Sequence[SourceCoverageItem]) -> list[SourceCoverageItem]:
        path = self.source_workspace / "source-graphs" / "oh-my-pi" / "source-to-target.md"
        if not path.is_file():
            return []
        active_keys = {
            self._omp_key(item.source_paths): item
            for item in existing
            if item.source_repository.lower() == "oh-my-pi"
        }
        items: list[SourceCoverageItem] = []
        for table in self.table_reader.read(path):
            if "最早owner unit" not in table.headers or "OMP来源" not in table.headers:
                continue
            for row in table.rows:
                unit = self._plain(row.get("最早owner unit", ""))
                sources = self._split_sources(row.get("OMP来源", ""))
                if not unit or not sources:
                    continue
                key = self._omp_key(sources)
                if key in active_keys:
                    continue
                status = self._plain(row.get("建议状态", "")).lower()
                target = self._plain(row.get("Zyra目标", ""))
                evidence = self._plain(row.get("主要行为证据", ""))
                future = unit.startswith("M2") or unit.startswith("M3")
                role, maturity = self._omp_role(status, future=future)
                capability_id = "omp-decision-" + self._slug(unit)
                limitations = (
                    f"OMP decision retained independently: {status or 'unspecified'}; target={target}; evidence={evidence}",
                )
                items.append(
                    SourceCoverageItem(
                        capability_id=capability_id,
                        source_repository="oh-my-pi",
                        source_paths=sources,
                        source_language="typescript",
                        role=role,
                        maturity=maturity,
                        target_paths=(),
                        target_language="typescript",
                        canonical_owner="",
                        cleanroom_decision="decision audited; no source-workspace runtime dependency",
                        limitations=limitations,
                        next_owner=unit if future else "M1-S08-02",
                    )
                )
        return items

    @staticmethod
    def _omp_role(status: str, *, future: bool) -> tuple[SourceRole, Maturity]:
        if future:
            return SourceRole.DEFERRED, Maturity.DEFERRED
        if "experimental" in status:
            return SourceRole.EXPERIMENTAL, Maturity.EXPERIMENTAL
        if "reference" in status or "audit" in status:
            return SourceRole.REFERENCE, Maturity.CONFORMANCE_VERIFIED
        if "contract" in status:
            return SourceRole.CONFORMANCE, Maturity.CONFORMANCE_VERIFIED
        return SourceRole.REFERENCE, Maturity.CONFORMANCE_VERIFIED

    @staticmethod
    def _omp_key(paths: Sequence[str]) -> str:
        tokens = sorted(
            token
            for path in paths
            for token in re.findall(r"[a-z0-9]+", path.lower())
            if len(token) > 2
        )
        return "|".join(tokens)

    @staticmethod
    def _split_sources(value: str) -> tuple[str, ...]:
        plain = value.replace("`", "")
        return tuple(
            part.strip()
            for part in re.split(r"[、,，]", plain)
            if part.strip()
        )

    @staticmethod
    def _plain(value: str) -> str:
        return re.sub(r"[`*_]", "", str(value or "")).strip()

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


class M1CustodyCatalog:
    def entries(self) -> tuple[StateCustodyEntry, ...]:
        return (
            StateCustodyEntry(
                "task_session",
                "SQLiteStore",
                ("packages/core/zyra_core/models.py",),
                ("packages/memory/zyra_memory/sqlite_store.py",),
                ("packages/memory/zyra_memory/sqlite_store.py::SQLiteStore.save_checkpoint",),
                ("packages/memory/zyra_memory/sqlite_store.py::SQLiteStore.load_task",),
                ("packages/memory/zyra_memory/sqlite_store.py::SQLiteStore.load_task",),
                ("task_created", "task_updated", "requirement_change"),
                ("revision",),
                ("task_id", "event_id"),
                ("run_id", "task_id", "causation_id"),
                derivative_consumers=("ClaudeRuntimeCore", "RuntimeControlDispatcher"),
            ),
            StateCustodyEntry(
                "runtime_event",
                "SQLiteStore",
                ("packages/core/zyra_core/models.py",),
                ("packages/memory/zyra_memory/sqlite_store.py", "packages/core/zyra_core/event_log.py"),
                ("packages/memory/zyra_memory/sqlite_store.py::SQLiteStore.append_event",),
                ("packages/memory/zyra_memory/sqlite_store.py::SQLiteStore.task_events",),
                ("packages/memory/zyra_memory/sqlite_store.py::SQLiteStore.task_events",),
                ("tool_started", "tool_completed", "artifact_written", "permission_decision"),
                ("sequence", "revision"),
                ("event_id",),
                ("run_id", "task_id", "causation_id", "correlation_id"),
                derivative_consumers=("timeline", "trace", "memory_curator"),
            ),
            StateCustodyEntry(
                "permission",
                "PermissionJournal",
                ("packages/runtime/claude-runtime/src/permission/model.ts",),
                ("packages/runtime/claude-runtime/src/permission",),
                ("packages/runtime/claude-runtime/src/permission/permission-journal.ts::PermissionJournal",),
                ("packages/runtime/claude-runtime/src/permission/coordinator.ts::PermissionCoordinator",),
                ("packages/runtime/claude-runtime/src/permission/settings-runtime.ts::PermissionSettingsRuntime",),
                ("permission_requested", "permission_decision", "permission_denied"),
                ("revision",),
                ("requestId", "decisionId"),
                ("runId", "taskId", "causationId"),
                derivative_consumers=("CodeWorker", "BrowserActionRegistry", "SkillRuntime"),
                forbidden_owners=("model", "hook", "classifier"),
            ),
            StateCustodyEntry(
                "memory",
                "SQLiteStore",
                ("packages/memory/zyra_memory/models.py", "packages/memory/zyra_memory/retrieval_models.py"),
                ("packages/memory/zyra_memory/sqlite_store.py", "packages/memory/zyra_memory/curator_store.py"),
                ("packages/memory/zyra_memory/sqlite_store.py::SQLiteStore.save_memory_records",),
                ("packages/memory/zyra_memory/sqlite_store.py::SQLiteStore.task_memory_records",),
                ("packages/memory/zyra_memory/fabric.py::MemoryFabric.replay_trajectory",),
                ("memory_written", "memory_retrieved", "compact_completed"),
                ("revision", "sequence"),
                ("record_id", "candidate_id"),
                ("run_id", "task_id", "causation_id"),
                derivative_consumers=("context_projection", "recovery_planner"),
                forbidden_owners=("Mnemopi DB",),
            ),
            StateCustodyEntry(
                "worker_lease",
                "WorkerPoolStore",
                ("packages/scheduler/zyra_scheduler/worker_pool/models.py",),
                ("packages/scheduler/zyra_scheduler/worker_pool/store.py",),
                ("packages/scheduler/zyra_scheduler/worker_pool/store.py::WorkerPoolStore.save_heartbeat",),
                ("packages/scheduler/zyra_scheduler/worker_pool/store.py::WorkerPoolStore",),
                ("packages/scheduler/zyra_scheduler/worker_pool/checkpoint.py::WorkerPoolCheckpointRuntime.restore",),
                ("worker_admitted", "lease_granted", "lease_renewed", "worker_failed"),
                ("revision", "lease_epoch"),
                ("lease_id", "attempt_id", "fence_token"),
                ("run_id", "task_id", "worker_id", "causation_id"),
                derivative_consumers=("scheduler", "recovery_planner"),
                forbidden_owners=("AgentRegistry", "roboomp SQLite"),
            ),
            StateCustodyEntry(
                "provider_backend",
                "BackendRegistryStore",
                ("packages/scheduler/zyra_scheduler/backend_registry/models.py",),
                ("packages/scheduler/zyra_scheduler/backend_registry/store.py", "packages/scheduler/zyra_scheduler/backend_registry/journal.py"),
                ("packages/scheduler/zyra_scheduler/backend_registry/store.py::BackendRegistryStore.append_event",),
                ("packages/scheduler/zyra_scheduler/backend_registry/store.py::BackendRegistryStore",),
                ("packages/scheduler/zyra_scheduler/backend_registry/journal.py::BackendDispatchJournal.replay_plan",),
                ("backend_registered", "route_selected", "provider_request", "provider_response"),
                ("revision", "route_epoch"),
                ("request_id", "lease_id", "dispatch_id"),
                ("run_id", "task_id", "causation_id"),
                derivative_consumers=("ProviderRequestRuntime", "scheduler"),
                forbidden_owners=("provider SDK cache", "OMP catalog"),
            ),
            StateCustodyEntry(
                "graph_topology",
                "GraphStateStore",
                ("packages/orchestration/zyra_orchestration/graph_custody/models.py",),
                ("packages/orchestration/zyra_orchestration/graph_custody/store.py",),
                ("packages/orchestration/zyra_orchestration/graph_custody/store.py::GraphStateStore.commit_for_delta",),
                ("packages/orchestration/zyra_orchestration/graph_custody/store.py::GraphStateStore.current",),
                ("packages/orchestration/zyra_orchestration/graph_custody/store.py::GraphStateStore.journal",),
                ("topology_mutated", "graph_delta_committed", "graph_conflict"),
                ("revision", "base_revision"),
                ("delta_id", "commit_id", "side_effect_id"),
                ("run_id", "task_id", "causation_id", "branch_id"),
                derivative_consumers=("scheduler", "recovery_planner"),
                forbidden_owners=("LangGraph StateGraph", "Pregel"),
            ),
            StateCustodyEntry(
                "recovery",
                "RecoveryStore",
                ("packages/scheduler/zyra_scheduler/recovery_runtime/contracts.py",),
                ("packages/scheduler/zyra_scheduler/recovery_runtime/store.py",),
                ("packages/scheduler/zyra_scheduler/recovery_runtime/store.py::RecoveryStore.append_action_receipt",),
                ("packages/scheduler/zyra_scheduler/recovery_runtime/store.py::RecoveryPlanStore",),
                ("packages/scheduler/zyra_scheduler/recovery_runtime/checkpoint_runtime.py::CheckpointRuntime.resume",),
                ("recovery_planned", "recovery_action", "recovery_completed", "recovery_failed"),
                ("revision", "attempt"),
                ("plan_id", "action_id", "checkpoint_id"),
                ("run_id", "task_id", "causation_id", "fault_id"),
                derivative_consumers=("task_owner", "memory_feedback"),
                forbidden_owners=("LLM", "watchdog"),
            ),
            StateCustodyEntry(
                "workspace",
                "WorkspaceBindingStore",
                ("packages/workspace/zyra_workspace/models.py",),
                ("packages/workspace/zyra_workspace/store.py",),
                ("packages/workspace/zyra_workspace/store.py::WorkspaceBindingStore.append_receipt",),
                ("packages/workspace/zyra_workspace/store.py::WorkspaceBindingStore",),
                ("packages/workspace/zyra_workspace/manager.py::WorkspaceManagerRuntime.restore",),
                ("workspace_bound", "workspace_mutated", "workspace_restored", "artifact_written"),
                ("revision", "generation"),
                ("operation_id", "receipt_id", "snapshot_id"),
                ("run_id", "task_id", "workspace_id", "causation_id"),
                derivative_consumers=("CodeWorker", "PatchEngine"),
                forbidden_owners=("OMP worktree", "external sandbox"),
            ),
            StateCustodyEntry(
                "skill_invocation",
                "SkillInvocationStateStore",
                ("packages/skills/zyra_skills/models.py",),
                ("packages/skills/zyra_skills/state.py", "packages/skills/zyra_skills/revision_store.py"),
                ("packages/skills/zyra_skills/state.py::SkillInvocationStateStore",),
                ("packages/skills/zyra_skills/state.py::SkillInvocationStateStore",),
                ("packages/skills/zyra_skills/session_integration.py::SkillSessionIntegrationRuntime",),
                ("skill_invoked", "skill_completed", "skill_failed", "skill_restored"),
                ("revision",),
                ("invocation_id", "skill_revision"),
                ("run_id", "task_id", "causation_id"),
                derivative_consumers=("SkillRuntime", "compact_runtime"),
                forbidden_owners=("plugin cache",),
            ),
            StateCustodyEntry(
                "browser_session",
                "BrowserSessionStore",
                ("packages/workers/zyra_workers/browser_session/models.py",),
                ("packages/workers/zyra_workers/browser_session/store.py",),
                ("packages/workers/zyra_workers/browser_session/store.py::JsonBrowserStateStore",),
                ("packages/workers/zyra_workers/browser_session/store.py::JsonBrowserStateStore",),
                ("packages/workers/zyra_workers/browser_session/resume_runtime.py::BrowserSessionResumeRuntime",),
                ("browser_started", "browser_action", "browser_crashed", "browser_resumed"),
                ("revision", "generation"),
                ("session_id", "lease_id", "action_id"),
                ("run_id", "task_id", "causation_id"),
                derivative_consumers=("browser_context", "browser_observability"),
                forbidden_owners=("browser-use session",),
            ),
            StateCustodyEntry(
                "code_index",
                "CodeIndexStore",
                ("packages/code_index/zyra_code_index/models.py",),
                ("packages/code_index/zyra_code_index/store.py",),
                ("packages/code_index/zyra_code_index/store.py::CodeIndexStore",),
                ("packages/code_index/zyra_code_index/store.py::CodeIndexStore",),
                ("packages/code_index/zyra_code_index/service.py::CodeIndexApiService",),
                ("code_index_started", "code_index_updated", "code_index_invalidated"),
                ("revision", "generation"),
                ("job_id", "workspace_digest"),
                ("run_id", "task_id", "workspace_id", "causation_id"),
                derivative_consumers=("query_context", "recovery_planner"),
                forbidden_owners=("LSP process",),
            ),
        )

    def required_families(self) -> tuple[str, ...]:
        return tuple(entry.state_family for entry in self.entries())
