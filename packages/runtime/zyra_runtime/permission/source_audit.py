from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import importlib
from pathlib import Path


class SourceDisposition(StrEnum):
    """How an upstream source participates in the Zyra-owned runtime."""

    ACTIVE = "active"
    ADAPTER = "adapter"
    CONTRACT_ONLY = "contract-only"
    REFERENCE_ONLY = "reference-only"
    DEFERRED = "deferred"


class SourceAuthority(StrEnum):
    PRIMARY = "primary"
    SUPPLEMENTAL = "supplemental"


@dataclass(frozen=True, slots=True)
class PermissionSourceDecision:
    repository: str
    source_path: str
    disposition: SourceDisposition
    authority: SourceAuthority
    mechanisms: tuple[str, ...]
    target_symbols: tuple[str, ...] = ()
    runtime_entry: str | None = None
    test_target: str | None = None
    limitations: tuple[str, ...] = ()
    replacement: str | None = None
    next_owner: str | None = None
    rationale: str = ""

    @property
    def key(self) -> str:
        return f"{self.repository}:{self.source_path}"

    @property
    def claims_runtime_ownership(self) -> bool:
        return self.disposition in {SourceDisposition.ACTIVE, SourceDisposition.ADAPTER}


@dataclass(frozen=True, slots=True)
class SourceAuditIssue:
    source_key: str
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class SourceAuditReport:
    decisions: tuple[PermissionSourceDecision, ...]
    issues: tuple[SourceAuditIssue, ...]
    counts: Mapping[SourceDisposition, int] = field(default_factory=dict)

    @property
    def blocking_issues(self) -> tuple[SourceAuditIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def complete(self) -> bool:
        return not self.blocking_issues

    def require_complete(self) -> "SourceAuditReport":
        if self.blocking_issues:
            details = "; ".join(
                f"{issue.source_key} [{issue.code}]: {issue.message}"
                for issue in self.blocking_issues
            )
            raise SourceAuditError(details)
        return self


class SourceAuditError(RuntimeError):
    pass


CLAUDE_REQUIRED_PATHS = frozenset(
    {
        "src/utils/permissions/permissions.ts",
        "src/utils/permissions/permissionsLoader.ts",
        "src/utils/permissions/permissionRuleParser.ts",
        "src/utils/permissions/PermissionRule.ts",
        "src/utils/permissions/PermissionMode.ts",
        "src/utils/permissions/denialTracking.ts",
        "src/utils/permissions/yoloClassifier.ts",
        "src/utils/permissions/classifierDecision.ts",
        "src/utils/permissions/bashClassifier.ts",
        "src/hooks/useCanUseTool.tsx",
        "src/bridge/bridgePermissionCallbacks.ts",
        "src/services/tools/toolExecution.ts",
        "src/types/permissions.ts",
        "src/utils/permissions/PermissionResult.ts",
        "src/services/tools/toolHooks.ts",
        "src/hooks/toolPermission/PermissionContext.ts",
        "src/hooks/toolPermission/handlers/{coordinatorHandler,swarmWorkerHandler,interactiveHandler}.ts",
        "src/hooks/toolPermission/permissionLogging.ts",
        "src/utils/permissions/PermissionUpdate.ts",
        "src/utils/permissions/PermissionUpdateSchema.ts",
        "src/utils/permissions/permissionSetup.ts",
    }
)


def _decision(
    repository: str,
    source_path: str,
    disposition: SourceDisposition,
    mechanisms: tuple[str, ...],
    *,
    authority: SourceAuthority = SourceAuthority.PRIMARY,
    targets: tuple[str, ...] = (),
    entry: str | None = None,
    test: str | None = None,
    limitations: tuple[str, ...] = (),
    replacement: str | None = None,
    next_owner: str | None = None,
    rationale: str = "",
) -> PermissionSourceDecision:
    return PermissionSourceDecision(
        repository=repository,
        source_path=source_path,
        disposition=disposition,
        authority=authority,
        mechanisms=mechanisms,
        target_symbols=targets,
        runtime_entry=entry,
        test_target=test,
        limitations=limitations,
        replacement=replacement,
        next_owner=next_owner,
        rationale=rationale,
    )


_EVALUATOR = "zyra_runtime.permission.evaluator.PermissionPolicyEvaluator"
_RULE_STORE = "zyra_runtime.permission.store.PermissionRuleStore"
_PENDING_STORE = "zyra_runtime.permission.request_queue.PermissionRequestQueue"
_MODE_RUNTIME = "zyra_runtime.permission.modes.PermissionModeRuntime"
_RISK_POLICY = "zyra_runtime.permission.risk.ToolRiskPolicy"
_HOOK_PIPELINE = "zyra_runtime.permission.hooks.PermissionHookAdapter"
_PERMISSION_RUNTIME = "zyra_runtime.permission.runtime.ToolPermissionRuntime"
_EXECUTION_GATE = "zyra_runtime.tool_runtime_foundation.ToolExecutionRuntime"
_QUERY_ENGINE_ENTRY = "zyra_runtime.claude_query_engine_runtime.ZyraClaudeQueryEngine"
_QUEUE_RESOLUTION = _PENDING_STORE
_EVENT_PROJECTOR = "zyra_runtime.permission.events.PermissionEventProjector"
_UNIT_TEST = "tests/unit/test_permission_runtime_foundation.py"
_INTEGRATION_TEST = "tests/integration/test_code_worker_permission_runtime_foundation.py"
_ALLOWED_TEST_TARGETS = frozenset({_UNIT_TEST, _INTEGRATION_TEST})


PERMISSION_SOURCE_DECISIONS: tuple[PermissionSourceDecision, ...] = (
    _decision(
        "claude-code-best",
        "src/utils/permissions/permissions.ts",
        SourceDisposition.ACTIVE,
        ("deny/ask/allow precedence", "tool-specific checks", "mode overlay"),
        targets=(_EVALUATOR,),
        entry=_EXECUTION_GATE,
        test=_INTEGRATION_TEST,
        limitations=("PreToolUse ask may force a decision", "headless flow is not durable"),
        rationale="Rewrite the deterministic ordering; never let hook or user output bypass a hard deny.",
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/permissionsLoader.ts",
        SourceDisposition.ACTIVE,
        ("multi-source rule loading", "source attribution"),
        targets=(_RULE_STORE,),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
        limitations=("upstream settings overlays are not a durable transactional store",),
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/permissionRuleParser.ts",
        SourceDisposition.ACTIVE,
        ("tool and argument rule parsing", "invalid rule rejection"),
        targets=("zyra_runtime.permission.rules.RuleMatcher",),
        entry=_EVALUATOR,
        test=_UNIT_TEST,
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/PermissionRule.ts",
        SourceDisposition.ACTIVE,
        ("rule identity", "rule behavior", "rule destination"),
        targets=("zyra_runtime.permission.models.PermissionRuleRecord",),
        entry=_RULE_STORE,
        test=_UNIT_TEST,
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/PermissionMode.ts",
        SourceDisposition.ACTIVE,
        ("mode state", "plan return mode", "auto and bypass availability"),
        targets=(_MODE_RUNTIME,),
        entry=_EVALUATOR,
        test=_UNIT_TEST,
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/denialTracking.ts",
        SourceDisposition.ACTIVE,
        ("consecutive denial circuit breaker", "total denial circuit breaker"),
        targets=(_MODE_RUNTIME,),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
        limitations=("upstream counters are process-local and UI-oriented",),
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/yoloClassifier.ts",
        SourceDisposition.ADAPTER,
        ("per-tool prompt projection", "classifier suggestion"),
        targets=("zyra_runtime.permission.classifier.PermissionClassifierAdapter",),
        entry=_EVALUATOR,
        test=_UNIT_TEST,
        limitations=(
            "empty projection auto-allows upstream",
            "prompt rules are attacker-influenceable",
            "model output must remain advisory",
        ),
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/classifierDecision.ts",
        SourceDisposition.ACTIVE,
        ("classifier eligibility", "proposal-to-decision boundary"),
        targets=("zyra_runtime.permission.classifier.PermissionClassifierProposal", _EVALUATOR),
        entry=_EVALUATOR,
        test=_UNIT_TEST,
        limitations=("classifier must not approve bypass-immune or interactive-required checks",),
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/bashClassifier.ts",
        SourceDisposition.DEFERRED,
        ("placeholder Bash classifier surface",),
        replacement=_RISK_POLICY,
        next_owner="M1-03A-02",
        rationale="The upstream module is a stub; structured risk checks replace it now, AST enrichment is deferred.",
    ),
    _decision(
        "claude-code-best",
        "src/hooks/useCanUseTool.tsx",
        SourceDisposition.ADAPTER,
        ("interactive approval queue", "decision callback"),
        targets=(
            _PENDING_STORE,
            "zyra_runtime.permission.models.PermissionResolutionResponse",
        ),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
        limitations=("upstream UI path can skip deterministic evaluation",),
    ),
    _decision(
        "claude-code-best",
        "src/bridge/bridgePermissionCallbacks.ts",
        SourceDisposition.ADAPTER,
        ("bridge request/reply", "abort propagation"),
        targets=(
            "zyra_runtime.permission.models.PermissionResolutionResponse",
            _PENDING_STORE,
        ),
        entry=_QUEUE_RESOLUTION,
        test=_UNIT_TEST,
        limitations=("upstream request id is not bound to session, identity, digest, scope, or expiry",),
    ),
    _decision(
        "claude-code-best",
        "src/services/tools/toolExecution.ts",
        SourceDisposition.ACTIVE,
        ("pre-call execution guard", "updated input handoff", "abort"),
        targets=(_EXECUTION_GATE,),
        entry=_QUERY_ENGINE_ENTRY,
        test=_INTEGRATION_TEST,
        limitations=("updated input is not re-schema-checked or re-digested upstream",),
    ),
    _decision(
        "claude-code-best",
        "src/types/permissions.ts",
        SourceDisposition.CONTRACT_ONLY,
        ("permission request and result type vocabulary",),
        targets=("zyra_runtime.permission.models.PermissionDecisionRecord",),
        replacement="Zyra-owned immutable models and canonical argument digest",
        rationale="Type vocabulary is retained; state ownership and identity semantics are replaced.",
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/PermissionResult.ts",
        SourceDisposition.CONTRACT_ONLY,
        ("allow/deny/ask result union",),
        targets=("zyra_runtime.permission.models.PermissionDecisionRecord",),
        replacement="Zyra decision provenance, scope, expiry, and request identity",
    ),
    _decision(
        "claude-code-best",
        "src/services/tools/toolHooks.ts",
        SourceDisposition.ACTIVE,
        ("PreToolUse aggregation", "deny precedence", "updated input proposal"),
        targets=(_HOOK_PIPELINE,),
        entry=_EVALUATOR,
        test=_UNIT_TEST,
        limitations=("PermissionRequest hooks use first completion instead of deny-precedence aggregation",),
    ),
    _decision(
        "claude-code-best",
        "src/hooks/toolPermission/PermissionContext.ts",
        SourceDisposition.ACTIVE,
        ("pending request context", "resolve and abort"),
        targets=(_PENDING_STORE,),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
        limitations=("upstream resolve-once guarantee is only in memory",),
    ),
    _decision(
        "claude-code-best",
        "src/hooks/toolPermission/handlers/{coordinatorHandler,swarmWorkerHandler,interactiveHandler}.ts",
        SourceDisposition.ADAPTER,
        ("coordinator", "worker", "interactive transport adapters"),
        targets=(
            "zyra_runtime.permission.models.PermissionResolutionResponse",
            _PENDING_STORE,
        ),
        entry=_QUEUE_RESOLUTION,
        test=_UNIT_TEST,
        limitations=("transport responses must not become authoritative decisions",),
    ),
    _decision(
        "claude-code-best",
        "src/hooks/toolPermission/permissionLogging.ts",
        SourceDisposition.ACTIVE,
        ("decision provenance", "approval latency", "redacted audit fields"),
        targets=(
            _EVENT_PROJECTOR,
            "zyra_runtime.permission.events.PermissionEventEnvelope",
        ),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
        limitations=("upstream source/reason can reflect only the last or fastest hook",),
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/PermissionUpdate.ts",
        SourceDisposition.ACTIVE,
        ("session rule update", "mode update", "rule destination"),
        targets=(_RULE_STORE, _MODE_RUNTIME),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/PermissionUpdateSchema.ts",
        SourceDisposition.ACTIVE,
        ("validated permission mutation", "destination restrictions"),
        targets=("zyra_runtime.permission.models.PermissionRuleRecord",),
        entry=_RULE_STORE,
        test=_UNIT_TEST,
    ),
    _decision(
        "claude-code-best",
        "src/utils/permissions/permissionSetup.ts",
        SourceDisposition.ACTIVE,
        ("auto-mode dangerous allow stripping", "reversible mode setup"),
        targets=(_MODE_RUNTIME,),
        entry=_EVALUATOR,
        test=_UNIT_TEST,
        limitations=("upstream cannot strip some non-updateable policy and command sources",),
    ),
    _decision(
        "claude-code-best",
        "src/utils/autoModeDenials.ts",
        SourceDisposition.REFERENCE_ONLY,
        ("bounded denial presentation",),
        targets=(_MODE_RUNTIME,),
        replacement="durable denial counters and recovery input",
        rationale="A process-local 20-item UI list is insufficient as runtime state custody.",
    ),
    _decision(
        "claude-code-best",
        "src/tools/BashTool/bashPermissions.ts",
        SourceDisposition.ACTIVE,
        ("shell rule ordering", "path checks", "read-only recognition"),
        targets=(_RISK_POLICY, _EVALUATOR),
        entry=_EXECUTION_GATE,
        test=_UNIT_TEST,
        limitations=("exact allow may override upstream parser uncertainty",),
    ),
    _decision(
        "claude-code-best",
        "src/tools/BashTool/bashSecurity.ts",
        SourceDisposition.ACTIVE,
        ("shell AST validation", "legacy semantic validation", "dangerous path checks"),
        targets=(_RISK_POLICY,),
        entry=_EXECUTION_GATE,
        test=_UNIT_TEST,
        limitations=("many upstream security failures are asks and can be classifier-approved",),
    ),
    _decision(
        "agent-framework",
        "python/packages/core/agent_framework/_harness/_tool_approval.py",
        SourceDisposition.ACTIVE,
        ("session-backed standing rules", "exact canonical arguments", "server identity"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=(_PENDING_STORE, _RULE_STORE),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
    ),
    _decision(
        "agent-framework",
        "python/packages/ag-ui/agent_framework_ag_ui/_agent_run.py",
        SourceDisposition.ADAPTER,
        ("pending registry", "thread/request/function/arguments validation", "consume once"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=(_PENDING_STORE,),
        entry=_QUEUE_RESOLUTION,
        test=_UNIT_TEST,
    ),
    _decision(
        "agent-framework",
        "python/packages/ag-ui/agent_framework_ag_ui/_message_adapters.py",
        SourceDisposition.ADAPTER,
        ("approval message projection", "tool identity projection"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=("zyra_runtime.permission.models.PermissionResolutionResponse",),
        entry=_QUEUE_RESOLUTION,
        test=_UNIT_TEST,
    ),
    _decision(
        "opencode",
        "packages/opencode/src/permission/index.ts",
        SourceDisposition.ADAPTER,
        ("last-match rules", "pending event", "once/always/reject replies"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=(_RULE_STORE, _PENDING_STORE),
        entry=_EVALUATOR,
        test=_UNIT_TEST,
        limitations=("approved rules can override configured denies", "pending identity lacks digest and expiry"),
    ),
    _decision(
        "opencode",
        "packages/opencode/src/permission/arity.ts",
        SourceDisposition.REFERENCE_ONLY,
        ("generated command arity hints",),
        authority=SourceAuthority.SUPPLEMENTAL,
        replacement="structured tool schema and shell parser evidence",
        rationale="Generated/data-like arity tables cannot own a security boundary.",
    ),
    _decision(
        "opencode",
        "packages/opencode/src/acp/permission.ts",
        SourceDisposition.ADAPTER,
        ("ACP approval transport", "permission option projection"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=("zyra_runtime.permission.models.PermissionResolutionResponse",),
        entry=_QUEUE_RESOLUTION,
        test=_UNIT_TEST,
    ),
    _decision(
        "agentscope",
        "src/agentscope/permission/_engine.py",
        SourceDisposition.ADAPTER,
        ("permission mode", "tool-specific precheck", "rule passthrough"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=(_RISK_POLICY, _EVALUATOR),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
        limitations=("security asks marked bypass-immune must become Zyra hard guards where irreversible",),
    ),
    _decision(
        "openclaw",
        "src/agents/tool-policy-pipeline.ts",
        SourceDisposition.ADAPTER,
        ("layered tool policy", "plugin groups", "unknown-tool diagnostics"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=(_RULE_STORE,),
        entry=_EVALUATOR,
        test=_UNIT_TEST,
    ),
    _decision(
        "openclaw",
        "src/agents/agent-tools.before-tool-call.ts",
        SourceDisposition.ADAPTER,
        ("before-call block", "deferred approval", "parameter adjustment audit"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=(_HOOK_PIPELINE, _EXECUTION_GATE),
        entry=_EXECUTION_GATE,
        test=_INTEGRATION_TEST,
    ),
    _decision(
        "hermes-agent",
        "tools/approval.py",
        SourceDisposition.ADAPTER,
        ("staged approval", "session allow", "gateway notification"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=(_PENDING_STORE,),
        entry=_PERMISSION_RUNTIME,
        test=_UNIT_TEST,
        limitations=("smart approval and yolo switches cannot be authoritative",),
    ),
    _decision(
        "hermes-agent",
        "tools/write_approval.py",
        SourceDisposition.REFERENCE_ONLY,
        ("write boundary presentation", "session write approval"),
        authority=SourceAuthority.SUPPLEMENTAL,
        targets=(_RISK_POLICY,),
        replacement="fast-edit prerequisites and canonical pending request identity",
    ),
)


def audit_permission_sources(
    decisions: Iterable[PermissionSourceDecision] = PERMISSION_SOURCE_DECISIONS,
    *,
    required_claude_paths: Iterable[str] = CLAUDE_REQUIRED_PATHS,
) -> SourceAuditReport:
    materialized = tuple(decisions)
    issues: list[SourceAuditIssue] = []
    keys: set[str] = set()

    for decision in materialized:
        if decision.key in keys:
            issues.append(SourceAuditIssue(decision.key, "duplicate", "source was decided more than once"))
        keys.add(decision.key)

        if not decision.mechanisms:
            issues.append(SourceAuditIssue(decision.key, "missing-mechanism", "no source mechanism was identified"))
        if decision.claims_runtime_ownership:
            if not decision.target_symbols:
                issues.append(SourceAuditIssue(decision.key, "missing-target", "active source has no Zyra target"))
            if not decision.runtime_entry:
                issues.append(SourceAuditIssue(decision.key, "missing-entry", "active source has no runtime entry"))
            if not decision.test_target:
                issues.append(SourceAuditIssue(decision.key, "missing-test", "active source has no behavior test"))
        elif decision.disposition in {
            SourceDisposition.CONTRACT_ONLY,
            SourceDisposition.REFERENCE_ONLY,
            SourceDisposition.DEFERRED,
        }:
            if not (decision.replacement or decision.next_owner or decision.rationale):
                issues.append(
                    SourceAuditIssue(
                        decision.key,
                        "unexplained-downgrade",
                        "non-runtime disposition lacks replacement, owner, or rationale",
                    )
                )

    present_claude = {
        decision.source_path
        for decision in materialized
        if decision.repository == "claude-code-best"
    }
    for path in sorted(set(required_claude_paths) - present_claude):
        issues.append(
            SourceAuditIssue(
                f"claude-code-best:{path}",
                "required-source-missing",
                "slice-03a-01 requires an explicit source disposition",
            )
        )

    counts = Counter(decision.disposition for decision in materialized)
    return SourceAuditReport(materialized, tuple(issues), dict(counts))


def source_decision(repository: str, source_path: str) -> PermissionSourceDecision:
    key = f"{repository}:{source_path}"
    for decision in PERMISSION_SOURCE_DECISIONS:
        if decision.key == key:
            return decision
    raise KeyError(key)


def assert_permission_source_coverage() -> SourceAuditReport:
    return audit_permission_sources().require_complete()


__all__ = [
    "CLAUDE_REQUIRED_PATHS",
    "PERMISSION_SOURCE_DECISIONS",
    "PermissionSourceDecision",
    "SourceAuditError",
    "SourceAuditIssue",
    "SourceAuditReport",
    "SourceAuthority",
    "SourceDisposition",
    "assert_permission_source_coverage",
    "audit_permission_sources",
    "source_decision",
]
