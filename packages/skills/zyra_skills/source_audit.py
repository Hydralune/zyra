from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import SkillRegistrySnapshot, utc_now
from .runtime import SkillRuntime


class SkillSourceDisposition(StrEnum):
    ACTIVE = "active"
    ADAPTER = "adapter"
    CONTRACT_ONLY = "contract_only"
    REFERENCE_ONLY = "reference_only"
    DEFERRED = "deferred"


class SkillAuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class SkillSourceDecision:
    source_repo: str
    source_path: str
    disposition: SkillSourceDisposition
    target_paths: tuple[str, ...]
    mechanisms: tuple[str, ...]
    runtime_entries: tuple[str, ...]
    test_surfaces: tuple[str, ...]
    reason: str
    owner_slice: str = "M1-S03C-01"
    downstream_owner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "disposition": str(self.disposition),
            "target_paths": list(self.target_paths),
            "mechanisms": list(self.mechanisms),
            "runtime_entries": list(self.runtime_entries),
            "test_surfaces": list(self.test_surfaces),
            "reason": self.reason,
            "owner_slice": self.owner_slice,
            "downstream_owner": self.downstream_owner,
        }


@dataclass(frozen=True, slots=True)
class SkillAuditFinding:
    code: str
    severity: SkillAuditSeverity
    message: str
    path: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "path": self.path,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class SkillRuntimeAuditReport:
    project_root: str
    registry_generation: int
    registry_snapshot_id: str
    source_decisions: tuple[SkillSourceDecision, ...]
    findings: tuple[SkillAuditFinding, ...]
    module_reachability: dict[str, tuple[str, ...]]
    state_custody: dict[str, str]
    created_at: str = field(default_factory=utc_now)

    @property
    def error_count(self) -> int:
        return sum(finding.severity is SkillAuditSeverity.ERROR for finding in self.findings)

    @property
    def blocker_count(self) -> int:
        return sum(finding.severity is SkillAuditSeverity.BLOCKER for finding in self.findings)

    @property
    def ok(self) -> bool:
        return self.error_count == 0 and self.blocker_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "registry_generation": self.registry_generation,
            "registry_snapshot_id": self.registry_snapshot_id,
            "source_decisions": [item.to_dict() for item in self.source_decisions],
            "findings": [item.to_dict() for item in self.findings],
            "module_reachability": {key: list(value) for key, value in self.module_reachability.items()},
            "state_custody": dict(self.state_custody),
            "error_count": self.error_count,
            "blocker_count": self.blocker_count,
            "ok": self.ok,
            "created_at": self.created_at,
        }


class SkillRuntimeAuditor:
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def audit(self, runtime: SkillRuntime) -> SkillRuntimeAuditReport:
        runtime.bootstrap()
        snapshot = runtime.registry.snapshot()
        findings: list[SkillAuditFinding] = []
        findings.extend(self._audit_registry(snapshot))
        findings.extend(self._audit_paths(snapshot))
        findings.extend(self._audit_runtime_wiring())
        findings.extend(self._audit_dependencies())
        findings.extend(self._audit_assets(snapshot))
        findings.extend(self._audit_source_decisions())
        reachability = self._module_reachability()
        for required in (
            "SkillRuntime",
            "SkillRegistry",
            "SkillBodyResourceLoader",
            "SkillAllowedToolsPolicy",
            "SkillInvocationRuntime",
            "InvokedSkillState",
            "SkillCompactBridge",
        ):
            if not reachability.get(required):
                findings.append(
                    SkillAuditFinding(
                        code="SKILL_MODULE_UNREACHABLE",
                        severity=SkillAuditSeverity.BLOCKER,
                        message=f"{required} has no production import/call surface",
                    )
                )
        return SkillRuntimeAuditReport(
            project_root=str(self.project_root),
            registry_generation=snapshot.generation,
            registry_snapshot_id=snapshot.snapshot_id,
            source_decisions=default_skill_source_decisions(),
            findings=tuple(findings),
            module_reachability=reachability,
            state_custody={
                "discovery/body/resource/policy/invocation": "M1-03C SkillRuntime",
                "run/task/session identity": "02B/02D runtime checkpoint aggregate",
                "permission decisions/grants": "M1-03A ToolPermissionRuntime",
                "forked child execution": "M1-03D SubagentRuntime handoff",
                "skill outcome memory": "M1-06C read-only SkillOutcomeProjection",
                "canonical runtime events": "existing Zyra EventRecord/EventStore",
            },
        )

    def _audit_registry(self, snapshot: SkillRegistrySnapshot) -> list[SkillAuditFinding]:
        findings: list[SkillAuditFinding] = []
        if snapshot.generation < 1:
            findings.append(
                SkillAuditFinding(
                    code="SKILL_REGISTRY_NOT_BOOTSTRAPPED",
                    severity=SkillAuditSeverity.BLOCKER,
                    message="skill registry has not published an active generation",
                )
            )
        if len(snapshot.active_by_qualified_name) < 10:
            findings.append(
                SkillAuditFinding(
                    code="BUILTIN_SKILL_SET_INCOMPLETE",
                    severity=SkillAuditSeverity.ERROR,
                    message="fewer than ten product-owned builtin skills are active",
                    evidence={"active_count": len(snapshot.active_by_qualified_name)},
                )
            )
        for qualified, ref in snapshot.active_by_qualified_name.items():
            revision = snapshot.revisions_by_ref.get(ref)
            if revision is None:
                findings.append(
                    SkillAuditFinding(
                        code="ACTIVE_SKILL_REVISION_MISSING",
                        severity=SkillAuditSeverity.BLOCKER,
                        message="active registry entry references a missing revision",
                        evidence={"qualified_name": qualified, "ref": ref},
                    )
                )
                continue
            if ref != revision.version_ref.immutable_ref:
                findings.append(
                    SkillAuditFinding(
                        code="SKILL_REF_IDENTITY_MISMATCH",
                        severity=SkillAuditSeverity.BLOCKER,
                        message="registry key does not match immutable revision ref",
                        evidence={"key": ref, "revision_ref": revision.version_ref.immutable_ref},
                    )
                )
            if not revision.version_ref.policy_digest or not revision.version_ref.provenance_digest:
                findings.append(
                    SkillAuditFinding(
                        code="SKILL_REVISION_DIGEST_INCOMPLETE",
                        severity=SkillAuditSeverity.ERROR,
                        message="skill revision is missing policy or provenance digest",
                        evidence={"ref": ref},
                    )
                )
        return findings

    def _audit_source_decisions(self) -> list[SkillAuditFinding]:
        findings: list[SkillAuditFinding] = []
        decisions = default_skill_source_decisions()
        mandatory = (
            "SkillTool",
            "loadSkillsDir",
            "attachments.ts",
            "processSlashCommand",
            "compact.ts",
            "registerSkillHooks",
            "sessionHooks",
            "bundledSkills",
            "mcpSkillBuilders",
            "skillChangeDetector",
            "pluginLoader",
            "loadPluginCommands",
            "loadPluginHooks",
            "loadPluginAgents",
            "schemas",
            "walkPluginMarkdown",
            "pluginIdentifier",
            "pluginOptionsStorage",
            "mcpPluginIntegration",
            "agent_framework/_skills.py",
            "packages/core/src/skill.ts",
            "packages/opencode/src/tool/skill.ts",
            "packages/opencode/src/command/index.ts",
            "agent/skill_commands.py",
            "tools/skills_tool.py",
            "tools/skill_manager_tool.py",
            "tools/skills_hub.py",
        )
        source_text = "\n".join(decision.source_path for decision in decisions)
        for marker in mandatory:
            if marker not in source_text:
                findings.append(
                    SkillAuditFinding(
                        code="SKILL_SOURCE_DECISION_MISSING",
                        severity=SkillAuditSeverity.BLOCKER,
                        message=f"mandatory source path has no disposition: {marker}",
                    )
                )
        for decision in decisions:
            for test_surface in decision.test_surfaces:
                if not (self.project_root / test_surface).is_file():
                    findings.append(
                        SkillAuditFinding(
                            code="SKILL_SOURCE_TEST_SURFACE_MISSING",
                            severity=SkillAuditSeverity.BLOCKER,
                            message="source decision references a missing test surface",
                            path=test_surface,
                            evidence={"source_path": decision.source_path},
                        )
                    )
            if decision.disposition not in {SkillSourceDisposition.ACTIVE, SkillSourceDisposition.ADAPTER}:
                continue
            for target in decision.target_paths:
                matches = tuple(self.project_root.glob(target)) if "*" in target else ()
                exists = bool(matches) if "*" in target else (self.project_root / target).exists()
                if not exists:
                    findings.append(
                        SkillAuditFinding(
                            code="SKILL_SOURCE_ACTIVE_TARGET_MISSING",
                            severity=SkillAuditSeverity.BLOCKER,
                            message="active source decision target does not exist",
                            path=target,
                            evidence={"source_path": decision.source_path},
                        )
                    )
        compact_text = (
            self.project_root / "packages" / "runtime" / "zyra_runtime" / "compact_restore_runtime.py"
        ).read_text(encoding="utf-8")
        if "default_skill_runtime" in compact_text:
            findings.append(
                SkillAuditFinding(
                    code="SKILL_COMPACT_GLOBAL_FALLBACK",
                    severity=SkillAuditSeverity.BLOCKER,
                    message="02D structured skill restore must use the current session resolver",
                )
            )
        return findings

    def _audit_paths(self, snapshot: SkillRegistrySnapshot) -> list[SkillAuditFinding]:
        findings: list[SkillAuditFinding] = []
        for ref, revision in snapshot.revisions_by_ref.items():
            root = Path(revision.skill_root)
            file = Path(revision.skill_file)
            try:
                root.relative_to(self.project_root)
            except ValueError:
                if str(revision.provenance.source_kind) == "builtin":
                    findings.append(
                        SkillAuditFinding(
                            code="BUILTIN_SKILL_OUTSIDE_PROJECT",
                            severity=SkillAuditSeverity.BLOCKER,
                            message="builtin skill depends on a path outside Zyra",
                            path=str(root),
                        )
                    )
            if file.is_symlink() or root.is_symlink():
                findings.append(
                    SkillAuditFinding(
                        code="SKILL_SYMLINK_PRESENT",
                        severity=SkillAuditSeverity.BLOCKER,
                        message="active skill revision contains a symlink boundary",
                        path=str(file),
                    )
                )
            if "vendor" in {part.lower() for part in root.parts}:
                findings.append(
                    SkillAuditFinding(
                        code="SKILL_RUNTIME_VENDOR_DEPENDENCY",
                        severity=SkillAuditSeverity.BLOCKER,
                        message="active skill resolves from a vendor-like directory",
                        path=str(root),
                        evidence={"ref": ref},
                    )
                )
        return findings

    def _audit_runtime_wiring(self) -> list[SkillAuditFinding]:
        api = self.project_root / "apps" / "api" / "zyra_api" / "main.py"
        compact = self.project_root / "packages" / "runtime" / "zyra_runtime" / "compact_restore_runtime.py"
        findings: list[SkillAuditFinding] = []
        checks = {
            api: (
                "_execute_skill_runtime_post",
                "SkillInvocationRequest",
                "ToolPermissionRuntimeSkillGateway",
                "skill_runtime_state",
            ),
            compact: (
                "invoked_skill_refs",
                "skill_restore_resolver",
                "allowed_tools_restore_grant",
            ),
        }
        for path, needles in checks.items():
            if not path.exists():
                findings.append(
                    SkillAuditFinding(
                        code="SKILL_MAIN_PATH_FILE_MISSING",
                        severity=SkillAuditSeverity.BLOCKER,
                        message="required production main-path file is missing",
                        path=str(path),
                    )
                )
                continue
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle not in text:
                    findings.append(
                        SkillAuditFinding(
                            code="SKILL_MAIN_PATH_WIRING_MISSING",
                            severity=SkillAuditSeverity.BLOCKER,
                            message=f"required production wiring marker is absent: {needle}",
                            path=str(path),
                        )
                    )
        return findings

    def _audit_dependencies(self) -> list[SkillAuditFinding]:
        findings: list[SkillAuditFinding] = []
        package_root = self.project_root / "packages" / "skills" / "zyra_skills"
        forbidden_tokens = (
            "../claude-code-best",
            "../opencode",
            "../hermes-agent",
            "../agent-framework",
            "subprocess.run",
            "subprocess.Popen",
            "npm install",
            "pip install",
        )
        for path in package_root.rglob("*.py"):
            if path.name == "source_audit.py":
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in text:
                    findings.append(
                        SkillAuditFinding(
                            code="SKILL_EXTERNAL_RUNTIME_DEPENDENCY",
                            severity=SkillAuditSeverity.BLOCKER,
                            message=f"skill runtime contains forbidden external dependency token: {token}",
                            path=str(path),
                        )
                    )
            try:
                tree = ast.parse(text)
            except SyntaxError as error:
                findings.append(
                    SkillAuditFinding(
                        code="SKILL_MODULE_SYNTAX_ERROR",
                        severity=SkillAuditSeverity.BLOCKER,
                        message=str(error),
                        path=str(path),
                    )
                )
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif node.module:
                        names = [node.module]
                    for name in names:
                        if name.startswith(("claude_code", "opencode", "hermes", "agent_framework")):
                            findings.append(
                                SkillAuditFinding(
                                    code="SKILL_UPSTREAM_IMPORT",
                                    severity=SkillAuditSeverity.BLOCKER,
                                    message=f"skill runtime imports an upstream package: {name}",
                                    path=str(path),
                                )
                            )
        return findings

    def _audit_assets(self, snapshot: SkillRegistrySnapshot) -> list[SkillAuditFinding]:
        expected = {
            "codebase-analysis",
            "code-change",
            "verification",
            "web-research",
            "pdf-analysis",
            "report-writing",
            "trace-summary",
            "failure-recovery",
            "requirement-change",
            "competition-demo",
        }
        actual = {
            revision.metadata.name
            for revision in snapshot.revisions_by_ref.values()
            if str(revision.provenance.source_kind) == "builtin"
        }
        missing = sorted(expected - actual)
        return (
            [
                SkillAuditFinding(
                    code="BUILTIN_SKILL_ASSET_MISSING",
                    severity=SkillAuditSeverity.ERROR,
                    message="required builtin skill asset is missing",
                    evidence={"missing": missing},
                )
            ]
            if missing
            else []
        )

    def _module_reachability(self) -> dict[str, tuple[str, ...]]:
        roots = [self.project_root / "apps", self.project_root / "packages"]
        symbols = (
            "SkillRuntime",
            "SkillRegistry",
            "SkillBodyResourceLoader",
            "SkillAllowedToolsPolicy",
            "SkillInvocationRuntime",
            "InvokedSkillState",
            "SkillCompactBridge",
        )
        hits: dict[str, list[str]] = {symbol: [] for symbol in symbols}
        for root in roots:
            for path in root.rglob("*.py"):
                relative = path.relative_to(self.project_root).as_posix()
                if relative.startswith("packages/skills/zyra_skills/source_audit.py"):
                    continue
                text = path.read_text(encoding="utf-8")
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    continue
                called: set[str] = set()
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    function = node.func
                    if isinstance(function, ast.Name):
                        called.add(function.id)
                    elif isinstance(function, ast.Attribute):
                        called.add(function.attr)
                for symbol in symbols:
                    if symbol in called:
                        hits[symbol].append(relative)
        return {key: tuple(sorted(set(value))) for key, value in hits.items()}


def default_skill_source_decisions() -> tuple[SkillSourceDecision, ...]:
    core = ("packages/skills/zyra_skills", "apps/api/zyra_api/main.py")
    tests = (
        "tests/unit/test_skill_runtime_foundation.py",
        "tests/integration/test_api_control_commands.py",
    )
    return (
        SkillSourceDecision(
            "claude-code-best",
            "src/tools/SkillTool/{SkillTool,types}.ts",
            SkillSourceDisposition.ACTIVE,
            core,
            ("validate/permission/inline-fork dispatch", "message and context deltas"),
            ("POST /tasks/{id}/skills", "SkillInvocationRuntime.invoke"),
            tests,
            "Adapted into the Zyra session and permission state model; raw AppState union was removed.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/tools/SkillTool/prompt.ts",
            SkillSourceDisposition.REFERENCE_ONLY,
            ("skills/builtin/**/SKILL.md",),
            ("invocation guidance only",),
            ("GET /skills",),
            tests,
            "Prompt concepts informed Zyra-owned runtime assets; prompt text is not production source LOC.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/tools/SkillTool/UI.tsx",
            SkillSourceDisposition.DEFERRED,
            ("apps/web",),
            ("interactive skill invocation presentation",),
            ("M2 skill console",),
            tests,
            "The foundation API is active; interactive UI productization belongs to M2.",
            downstream_owner="M2-01A",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/skills/loadSkillsDir.ts",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/sources/filesystem.py", "packages/skills/zyra_skills/registry.py"),
            ("source discovery", "SKILL.md directory contract", "conditional paths"),
            ("SkillRuntime.bootstrap", "SkillSearchIndex"),
            tests,
            "Reworked into metadata/body separation with strict containment and immutable revisions.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/skills/bundledSkills.ts",
            SkillSourceDisposition.ACTIVE,
            ("skills/builtin/**/SKILL.md", "packages/skills/zyra_skills/runtime.py"),
            ("product-owned builtin discovery", "immutable product provenance", "bootstrap admission"),
            ("GET /skills", "POST /tasks/{id}/skills"),
            tests,
            "The upstream bundle list became Zyra runtime assets discovered by the same versioned loader; asset text is excluded from production LOC.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/utils/attachments.ts",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/attachments.py",),
            ("budgeted listing", "agent/session sent set", "resume suppression", "invoked projection"),
            ("SkillRuntime.listing_projection", "SkillInvocationRuntime.invoke"),
            tests,
            "Attachments are typed deltas and never carry permission authority.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/utils/processUserInput/processSlashCommand.tsx",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/invocation.py", "packages/skills/zyra_skills/session_bridge.py"),
            ("atomic body/policy/hook/state/message mutation",),
            ("POST /tasks/{id}/skills",),
            tests,
            "Product-specific UI behavior was removed; session mutation semantics were preserved.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/services/compact/compact.ts",
            SkillSourceDisposition.ADAPTER,
            ("packages/skills/zyra_skills/compact_bridge.py", "packages/runtime/zyra_runtime/compact_restore_runtime.py"),
            ("agent-scoped invoked restore", "per-skill and total budgets"),
            ("CompactRestoreRuntime.build_report",),
            tests,
            "Raw cached skill content was replaced by exact immutable revision references.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/utils/hooks/{registerSkillHooks,sessionHooks}.ts",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/hooks.py",),
            ("session leases", "once-success cleanup", "terminal/reload/revoke cleanup"),
            ("SkillInvocationRuntime.invoke/complete/cancel",),
            tests,
            "Only declarative product-owned hook actions are accepted; arbitrary commands are excluded.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/utils/skills/skillChangeDetector.ts",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/change_detector.py", "packages/skills/zyra_skills/reload.py"),
            ("stable snapshot debounce", "explicit polling", "validated atomic reload", "lifecycle cleanup"),
            ("SkillRuntime.reload_if_changed",),
            tests,
            "Background watcher ownership was removed; session-boundary polling drives a complete validated registry transaction.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/utils/plugins/{pluginLoader,schemas,walkPluginMarkdown,pluginIdentifier,pluginOptionsStorage}.ts",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/plugin_runtime.py", "packages/skills/zyra_skills/sources/plugin.py"),
            ("cache-only capabilities", "namespaced provenance", "atomic refresh", "secret sink split"),
            ("PluginRuntime.refresh", "SkillRuntime.bootstrap"),
            tests,
            "Marketplace/network install is deliberately excluded from the foundation runtime.",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/utils/plugins/{loadPluginCommands,loadPluginHooks}.ts",
            SkillSourceDisposition.CONTRACT_ONLY,
            ("packages/skills/zyra_skills/plugin_runtime.py", "packages/skills/zyra_skills/hooks.py"),
            ("validated plugin capability boundary", "no command or hook activation in foundation"),
            ("PluginRuntime.capabilities",),
            tests,
            "Plugin skill discovery is active; plugin command and hook activation remain an explicit 03C-02 integration contract.",
            downstream_owner="M1-03C-02",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/utils/plugins/loadPluginAgents.ts",
            SkillSourceDisposition.CONTRACT_ONLY,
            ("packages/skills/zyra_skills/subagent_contract.py",),
            ("agent type provenance handoff",),
            ("SkillForkRequest",),
            tests,
            "Agent definition and child execution are owned by M1-03D.",
            downstream_owner="M1-03D",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/utils/plugins/mcpPluginIntegration.ts",
            SkillSourceDisposition.CONTRACT_ONLY,
            ("packages/skills/zyra_skills/sources/mcp.py", "packages/skills/zyra_skills/plugin_runtime.py"),
            ("plugin provenance handoff", "03B typed server projection boundary"),
            ("McpProjectedSkillSource", "PluginRuntime.snapshot"),
            tests,
            "MCP server lifecycle and authentication remain 03B-owned; 03C accepts only an already validated typed projection.",
            downstream_owner="M1-03C-02",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/skills/mcpSkills.ts",
            SkillSourceDisposition.DEFERRED,
            ("packages/skills/zyra_skills/sources/mcp.py",),
            ("03B typed projection extension point",),
            ("McpProjectedSkillSource",),
            tests,
            "Claude mcpSkills.ts is a no-op stub; only a real 03B projection may activate MCP skills.",
            downstream_owner="M1-03C-02",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/skills/mcpSkillBuilders.ts",
            SkillSourceDisposition.CONTRACT_ONLY,
            ("packages/skills/zyra_skills/sources/mcp.py",),
            ("cycle-breaking typed MCP skill builder", "immutable projection validation"),
            ("McpProjectedSkillSource",),
            tests,
            "The builder shape is retained as a typed 03B projection contract; activation waits for 03C-02.",
            downstream_owner="M1-03C-02",
        ),
        SkillSourceDecision(
            "claude-code-best",
            "src/services/skillSearch/* and src/tools/DiscoverSkillsTool/prompt.ts",
            SkillSourceDisposition.DEFERRED,
            ("packages/skills/zyra_skills/search.py",),
            ("local metadata search only",),
            ("SkillSearchIndex.search",),
            tests,
            "Remote search is generated/no-op in the source snapshot and remains explicitly deferred.",
        ),
        SkillSourceDecision(
            "agent-framework",
            "python/packages/core/agent_framework/_skills.py::{SkillsProvider,FileSkillsSource,Skill,source combinators}",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/frontmatter.py", "packages/skills/zyra_skills/path_security.py", "packages/skills/zyra_skills/resource_loader.py"),
            ("progressive disclosure", "strict resource validation", "source composition"),
            ("SkillRegistry", "SkillBodyResourceLoader", "SkillResourceLoader"),
            tests,
            "Approval defaults were replaced by the existing 03A permission owner.",
        ),
        SkillSourceDecision(
            "agent-framework",
            "python/packages/core/agent_framework/_skills.py::MCPSkillsSource",
            SkillSourceDisposition.CONTRACT_ONLY,
            ("packages/skills/zyra_skills/sources/mcp.py",),
            ("03B typed MCP projection", "digest/provenance validation"),
            ("McpProjectedSkillSource",),
            tests,
            "MCP transport and server lifecycle remain 03B-owned; 03C-02 will activate projected skills.",
            downstream_owner="M1-03C-02",
        ),
        SkillSourceDecision(
            "opencode",
            "packages/core/src/skill.ts and packages/opencode/src/skill/**",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/registry.py", "packages/skills/zyra_skills/sources/filesystem.py"),
            ("typed source composition", "cache-free listing", "permission-filtered discovery"),
            ("GET /skills", "SkillRuntime.bootstrap"),
            tests,
            "Reworked into immutable Zyra registry generations and metadata-only listing.",
        ),
        SkillSourceDecision(
            "opencode",
            "packages/opencode/src/tool/skill.ts",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/invocation.py", "packages/skills/zyra_skills/invocation_permission.py"),
            ("permission-before-load", "lazy body disclosure", "typed invocation result"),
            ("POST /tasks/{id}/skills",),
            tests,
            "AISDK-specific tool wrapping was removed; 03A remains the only permission owner.",
        ),
        SkillSourceDecision(
            "opencode",
            "packages/opencode/src/command/index.ts",
            SkillSourceDisposition.ADAPTER,
            ("packages/skills/zyra_skills/session_bridge.py", "apps/api/zyra_api/main.py"),
            ("command-to-skill synthesis", "message/session delta", "terminal lifecycle"),
            ("POST /tasks/{id}/skills", "POST /tasks/{id}/skills/{invocation}/{complete|cancel}"),
            tests,
            "Command synthesis is an API/session mutation rather than an independent prompt library.",
        ),
        SkillSourceDecision(
            "opencode",
            "packages/opencode/src/plugin/{index,loader}.ts and packages/core/src/plugin/{host,skill}.ts",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/plugin_runtime.py", "packages/skills/zyra_skills/sources/plugin.py"),
            ("typed cached plugin skill source", "namespace/provenance", "atomic capability snapshot"),
            ("PluginRuntime.refresh", "SkillRuntime.bootstrap"),
            tests,
            "Dynamic module execution was removed; only validated cached skill capabilities enter 03C.",
        ),
        SkillSourceDecision(
            "opencode",
            "packages/opencode/src/plugin/install.ts",
            SkillSourceDisposition.DEFERRED,
            ("packages/skills/zyra_skills/atomic_update.py",),
            ("local staged update contract only",),
            ("AtomicSkillPackageUpdater",),
            tests,
            "Network/package installation is out of the foundation main path; local transaction support is retained for 03C-02.",
            downstream_owner="M1-03C-02",
        ),
        SkillSourceDecision(
            "hermes-agent",
            "agent/skill_utils.py, tools/skills_{sync,guard}.py, tools/skill_usage.py",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/revision_store.py", "packages/skills/zyra_skills/path_security.py", "packages/skills/zyra_skills/change_detector.py"),
            ("origin/version lifecycle", "strict validation", "revocation", "stable change detection"),
            ("SkillRuntime.bootstrap", "SkillRuntime.reload_if_changed"),
            tests,
            "Hermes home-sidecar ownership and warning-only injection handling were not retained.",
        ),
        SkillSourceDecision(
            "hermes-agent",
            "agent/skill_commands.py and tools/skills_tool.py",
            SkillSourceDisposition.ACTIVE,
            ("packages/skills/zyra_skills/search.py", "packages/skills/zyra_skills/invocation.py", "apps/api/zyra_api/main.py"),
            ("metadata listing/search", "body/resource disclosure", "slash/API invocation"),
            ("GET /skills", "POST /tasks/{id}/skills"),
            tests,
            "Hermes command and progressive-disclosure behavior was adapted to Zyra session and permission state.",
        ),
        SkillSourceDecision(
            "hermes-agent",
            "tools/skill_manager_tool.py",
            SkillSourceDisposition.CONTRACT_ONLY,
            ("packages/skills/zyra_skills/atomic_update.py",),
            ("read-before-write", "staging/backup/rollback", "approval boundary"),
            ("AtomicSkillPackageUpdater",),
            tests,
            "The local transaction exists, but mutation approval/API productization belongs to 03C-02.",
            downstream_owner="M1-03C-02",
        ),
        SkillSourceDecision(
            "hermes-agent",
            "tools/skills_hub.py and hermes_cli/skills_hub.py",
            SkillSourceDisposition.DEFERRED,
            ("packages/skills/zyra_skills/search.py",),
            ("remote marketplace search/install",),
            ("SkillSearchIndex.search",),
            tests,
            "Remote marketplace and network install are explicitly outside the foundation main path.",
            downstream_owner="M1-03C-02",
        ),
    )
