from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .digests import digest_object


class SourceDisposition(StrEnum):
    ACTIVE = "active"
    ADAPTER = "adapter"
    CONTRACT_ONLY = "contract_only"
    REFERENCE_ONLY = "reference_only"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class SubagentSourceDecision:
    source_repository: str
    source_path: str
    disposition: SourceDisposition
    target_module: str
    runtime_entry: str
    behavior_test: str
    rationale: str
    downstream_owner: str = ""
    maturity: str = "mature"
    main_path: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repository": self.source_repository,
            "source_path": self.source_path,
            "disposition": self.disposition.value,
            "target_module": self.target_module,
            "runtime_entry": self.runtime_entry,
            "behavior_test": self.behavior_test,
            "rationale": self.rationale,
            "downstream_owner": self.downstream_owner,
            "maturity": self.maturity,
            "main_path": self.main_path,
        }


@dataclass(frozen=True, slots=True)
class SubagentSourceAudit:
    decisions: tuple[SubagentSourceDecision, ...]
    findings: tuple[dict[str, Any], ...]
    digest: str

    @property
    def ok(self) -> bool:
        return not any(item.get("blocking") for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for decision in self.decisions:
            counts[decision.disposition.value] = counts.get(decision.disposition.value, 0) + 1
        return {
            "ok": self.ok,
            "decisions": [item.to_dict() for item in self.decisions],
            "findings": [dict(item) for item in self.findings],
            "counts": counts,
            "digest": self.digest,
        }


def subagent_source_decisions() -> tuple[SubagentSourceDecision, ...]:
    active = {
        "src/tools/AgentTool/AgentTool.tsx": "zyra_workers.subagents.runtime",
        "src/tools/AgentTool/runAgent.ts": "zyra_workers.subagents.dispatch",
        "src/tools/AgentTool/loadAgentsDir.ts": "zyra_workers.subagents.definitions",
        "src/tools/AgentTool/agentToolUtils.ts": "zyra_workers.subagents.tool_scope",
        "src/tools/AgentTool/forkSubagent.ts": "zyra_workers.subagents.context",
        "src/tools/AgentTool/resumeAgent.ts": "zyra_workers.subagents.continuation",
        "src/utils/agentContext.ts": "zyra_workers.subagents.context",
        "src/tools/AgentTool/builtInAgents.ts": "zyra_workers.subagents.definitions",
        "src/tasks/LocalAgentTask/*": "zyra_workers.subagents.lifecycle",
        "src/tools/SendMessageTool/*": "zyra_workers.subagents.continuation",
        "src/utils/task/framework.ts": "zyra_workers.subagents.task_store",
        "src/utils/task/diskOutput.ts": "zyra_workers.subagents.transcript",
        "src/utils/sessionStorage.ts": "zyra_workers.subagents.transcript",
    }
    decisions = [
        SubagentSourceDecision(
            source_repository="claude-code-best",
            source_path=path,
            disposition=SourceDisposition.ACTIVE,
            target_module=target,
            runtime_entry="SubagentRuntime.spawn",
            behavior_test="tests/unit/test_subagent_commands_foundation.py",
            rationale="Mechanism is rewritten into the durable Zyra logical task/session/permission model.",
        )
        for path, target in active.items()
    ]
    decisions.extend(
        [
            SubagentSourceDecision(
                "claude-code-best", "src/tools/AgentTool/agentMemory.ts", SourceDisposition.ADAPTER,
                "zyra_workers.subagents.context", "SubagentContextFactory.create",
                "tests/unit/test_subagent_commands_foundation.py",
                "03D carries memory refs only; 06C owns memory outcome and restore.", downstream_owner="M1-06C",
            ),
            SubagentSourceDecision(
                "claude-code-best", "src/tools/AgentTool/agentMemorySnapshot.ts", SourceDisposition.DEFERRED,
                "packages/memory", "none in 03D",
                "tests/unit/test_subagent_commands_foundation.py",
                "Agent memory snapshot mutation belongs to 06C.", downstream_owner="M1-06C", main_path=False,
            ),
            SubagentSourceDecision(
                "claude-code-best", "src/tasks/RemoteAgentTask/*", SourceDisposition.DEFERRED,
                "packages/workers", "SubagentExecutionPort",
                "tests/unit/test_subagent_commands_foundation.py",
                "CCR/ant-only remote task is product-specific; 07A/05D own remote worker and lease.", downstream_owner="M1-07A/M1-05D", main_path=False,
            ),
            SubagentSourceDecision(
                "claude-code-best", "src/utils/worktree.ts", SourceDisposition.CONTRACT_ONLY,
                "zyra_workers.subagents.isolation", "SubagentIsolationRequestPort",
                "tests/unit/test_subagent_commands_foundation.py",
                "03D defines fail-closed request/manifest/cleanup contract; physical worktree owner is 05A.", downstream_owner="M1-05A",
            ),
            SubagentSourceDecision(
                "opencode", "packages/opencode/src/tool/task.ts", SourceDisposition.ADAPTER,
                "zyra_workers.subagents.runtime", "SubagentRuntime.spawn",
                "tests/integration/test_subagent_commands_foundation.py",
                "Child session, permission derivation and foreground/background semantics supplement Claude AgentTool.",
            ),
            SubagentSourceDecision(
                "opencode", "packages/core/src/background-job.ts", SourceDisposition.REFERENCE_ONLY,
                "zyra_workers.subagents.lifecycle", "AgentTaskLifecycleRuntime",
                "tests/unit/test_subagent_commands_foundation.py",
                "Upstream job is explicitly process-local and cannot own durable task state.", maturity="process-local", main_path=False,
            ),
            SubagentSourceDecision(
                "hermes-agent", "tools/delegate_tool.py", SourceDisposition.ADAPTER,
                "zyra_workers.subagents.tool_scope", "ChildToolScopeRuntime.resolve",
                "tests/unit/test_subagent_commands_foundation.py",
                "Tool intersection, depth cap and summary rollup are adapted without subprocess delegation.",
            ),
            SubagentSourceDecision(
                "hermes-agent", "tools/async_delegation.py", SourceDisposition.ADAPTER,
                "zyra_workers.subagents.lifecycle", "AgentTaskLifecycleRuntime.execute",
                "tests/integration/test_subagent_commands_foundation.py",
                "Completion/re-entry semantics are moved to the durable task store; daemon registry is not retained.",
            ),
            SubagentSourceDecision(
                "agentscope", "src/agentscope/app/_tool/_agent_create.py", SourceDisposition.ADAPTER,
                "zyra_workers.subagents.definitions", "AgentDefinitionRegistry",
                "tests/unit/test_subagent_commands_foundation.py",
                "Agent template and child session provenance supplement AgentTool definitions.",
            ),
            SubagentSourceDecision(
                "agentscope", "background task manager / wakeup dispatcher", SourceDisposition.DEFERRED,
                "packages/workers", "none in 03D",
                "tests/unit/test_subagent_commands_foundation.py",
                "Resident inbox/wakeup and physical background registry belong to 07A/05C.", downstream_owner="M1-07A/M1-05C", maturity="process-local", main_path=False,
            ),
        ]
    )
    return tuple(decisions)


def audit_subagent_sources(project_root: str | Path) -> SubagentSourceAudit:
    root = Path(project_root).resolve()
    decisions = subagent_source_decisions()
    findings: list[dict[str, Any]] = []
    for decision in decisions:
        target = root / decision.target_module.replace(".", "/")
        if decision.disposition in {SourceDisposition.ACTIVE, SourceDisposition.ADAPTER}:
            module_name = decision.target_module.rsplit(".", 1)[-1] + ".py"
            candidates = list((root / "packages").rglob(module_name))
            if not candidates:
                findings.append({
                    "code": "source_target_missing",
                    "blocking": True,
                    "source_path": decision.source_path,
                    "target_module": decision.target_module,
                })
        if decision.main_path and not decision.runtime_entry:
            findings.append({
                "code": "source_runtime_entry_missing",
                "blocking": True,
                "source_path": decision.source_path,
            })
        if decision.disposition == SourceDisposition.REFERENCE_ONLY and decision.main_path:
            findings.append({
                "code": "reference_source_marked_main_path",
                "blocking": True,
                "source_path": decision.source_path,
            })
    payload = [item.to_dict() for item in decisions]
    return SubagentSourceAudit(tuple(decisions), tuple(findings), digest_object(payload))

