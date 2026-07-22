from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity


REQUIRED_DISABLE_CAPABILITIES = (
    "query-session",
    "tool-loop",
    "mcp-runtime",
    "permission-runtime",
    "workspace-runtime",
    "sandbox-gateway",
    "runtime-event-spine",
    "provider-control-plane",
    "memory-retrieval",
    "code-index",
    "memory-curator",
    "skill-memory-restore",
    "physical-worker",
    "edge-worker",
    "watchdog",
    "checkpoint-recovery",
    "layered-route",
    "graph-custody",
)


@dataclass(frozen=True, slots=True)
class EntrypointRequirement:
    path: str
    symbols: tuple[str, ...] = ()
    route_patterns: tuple[str, ...] = ()
    event_patterns: tuple[str, ...] = ()
    language: str = "Python"


@dataclass(frozen=True, slots=True)
class OwnerRequirement:
    capability: str
    canonical_owner: str
    state_family: str
    source_role: str
    source_repository: str
    source_language: str
    target_language: str
    entrypoints: tuple[EntrypointRequirement, ...]
    disable_probe_ids: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    required_for_scenarios: tuple[str, ...] = ()
    restore_semantics: tuple[str, ...] = ()
    forbidden_alternate_owners: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntrypointObservation:
    path: str
    exists: bool
    language: str
    symbols_found: tuple[str, ...]
    symbols_missing: tuple[str, ...]
    route_patterns_found: tuple[str, ...]
    route_patterns_missing: tuple[str, ...]
    event_patterns_found: tuple[str, ...]
    event_patterns_missing: tuple[str, ...]
    digest: str
    executable_lines: int

    @property
    def reachable(self) -> bool:
        return (
            self.exists
            and not self.symbols_missing
            and not self.route_patterns_missing
            and not self.event_patterns_missing
            and self.executable_lines > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "language": self.language,
            "symbols_found": list(self.symbols_found),
            "symbols_missing": list(self.symbols_missing),
            "route_patterns_found": list(self.route_patterns_found),
            "route_patterns_missing": list(self.route_patterns_missing),
            "event_patterns_found": list(self.event_patterns_found),
            "event_patterns_missing": list(self.event_patterns_missing),
            "digest": self.digest,
            "executable_lines": self.executable_lines,
            "reachable": self.reachable,
        }


@dataclass(frozen=True, slots=True)
class OwnerObservation:
    requirement: OwnerRequirement
    entrypoints: tuple[EntrypointObservation, ...]
    registered_probe_ids: tuple[str, ...]
    scenario_ids: tuple[str, ...]

    @property
    def reachable(self) -> bool:
        return bool(self.entrypoints) and all(item.reachable for item in self.entrypoints)

    @property
    def disable_covered(self) -> bool:
        return bool(set(self.requirement.disable_probe_ids) & set(self.registered_probe_ids))

    @property
    def scenario_covered(self) -> bool:
        return set(self.requirement.required_for_scenarios).issubset(set(self.scenario_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.requirement.capability,
            "canonical_owner": self.requirement.canonical_owner,
            "state_family": self.requirement.state_family,
            "source_role": self.requirement.source_role,
            "source_repository": self.requirement.source_repository,
            "source_language": self.requirement.source_language,
            "target_language": self.requirement.target_language,
            "dependencies": list(self.requirement.dependencies),
            "restore_semantics": list(self.requirement.restore_semantics),
            "entrypoints": [item.to_dict() for item in self.entrypoints],
            "required_probe_ids": list(self.requirement.disable_probe_ids),
            "registered_probe_ids": list(self.registered_probe_ids),
            "required_scenarios": list(self.requirement.required_for_scenarios),
            "observed_scenarios": list(self.scenario_ids),
            "reachable": self.reachable,
            "disable_covered": self.disable_covered,
            "scenario_covered": self.scenario_covered,
        }


class SourceInspector:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._text_cache: dict[Path, str] = {}

    def inspect(self, requirement: EntrypointRequirement) -> EntrypointObservation:
        path = self._resolve(requirement.path)
        if not path.is_file():
            return EntrypointObservation(
                path=requirement.path,
                exists=False,
                language=requirement.language,
                symbols_found=(),
                symbols_missing=requirement.symbols,
                route_patterns_found=(),
                route_patterns_missing=requirement.route_patterns,
                event_patterns_found=(),
                event_patterns_missing=requirement.event_patterns,
                digest="",
                executable_lines=0,
            )
        text = self._read(path)
        symbols = self._symbols(path, text, requirement.language)
        symbols_found = tuple(symbol for symbol in requirement.symbols if symbol in symbols)
        symbols_missing = tuple(symbol for symbol in requirement.symbols if symbol not in symbols)
        routes_found, routes_missing = self._patterns(text, requirement.route_patterns)
        events_found, events_missing = self._patterns(text, requirement.event_patterns)
        return EntrypointObservation(
            path=requirement.path,
            exists=True,
            language=requirement.language,
            symbols_found=symbols_found,
            symbols_missing=symbols_missing,
            route_patterns_found=routes_found,
            route_patterns_missing=routes_missing,
            event_patterns_found=events_found,
            event_patterns_missing=events_missing,
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            executable_lines=self._executable_lines(text, requirement.language),
        )

    def _resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return self.root / "__outside_workspace__"
        return path

    def _read(self, path: Path) -> str:
        if path not in self._text_cache:
            self._text_cache[path] = path.read_text(encoding="utf-8", errors="replace")
        return self._text_cache[path]

    @staticmethod
    def _patterns(text: str, patterns: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        found: list[str] = []
        missing: list[str] = []
        for pattern in patterns:
            try:
                matched = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None
            except re.error:
                matched = False
            (found if matched else missing).append(pattern)
        return tuple(found), tuple(missing)

    @staticmethod
    def _symbols(path: Path, text: str, language: str) -> set[str]:
        if language.lower() == "python" or path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                return set()
            symbols: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            symbols.add(target.id)
            return symbols
        patterns = (
            r"\b(?:export\s+)?(?:class|function|interface|type|const|let|var|enum)\s+([A-Za-z_$][\w$]*)",
            r"\b(?:pub\s+)?(?:struct|enum|trait|fn|type|const|static)\s+([A-Za-z_][\w]*)",
        )
        symbols: set[str] = set()
        for pattern in patterns:
            symbols.update(re.findall(pattern, text))
        return symbols

    @staticmethod
    def _executable_lines(text: str, language: str) -> int:
        count = 0
        in_block_comment = False
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if in_block_comment:
                if "*/" in line:
                    in_block_comment = False
                continue
            if line.startswith("/*"):
                if "*/" not in line[2:]:
                    in_block_comment = True
                continue
            if line.startswith(("#", "//", "*")):
                continue
            if language.lower() == "python" and line.startswith(('"""', "'''")):
                continue
            count += 1
        return count


class OwnerDependencyGraph:
    def __init__(self, requirements: Sequence[OwnerRequirement]) -> None:
        self.requirements = {item.capability: item for item in requirements}

    def validate(self) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        for capability, requirement in self.requirements.items():
            for dependency in requirement.dependencies:
                if dependency not in self.requirements:
                    findings.append(
                        Finding(
                            code="owner.dependency_missing",
                            severity=Severity.BLOCKER,
                            summary="Owner matrix refers to an unknown hard dependency.",
                            capability=capability,
                            detail=dependency,
                        )
                    )
            if capability in requirement.dependencies:
                findings.append(
                    Finding(
                        code="owner.self_dependency",
                        severity=Severity.BLOCKER,
                        summary="Owner capability cannot depend on itself.",
                        capability=capability,
                    )
                )
        _, cycle = self.order()
        if cycle:
            findings.append(
                Finding(
                    code="owner.dependency_cycle",
                    severity=Severity.BLOCKER,
                    summary="Owner dependency graph contains a cycle.",
                    detail=" -> ".join(cycle),
                )
            )
        return tuple(findings)

    def order(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        indegree = {capability: 0 for capability in self.requirements}
        dependants: dict[str, list[str]] = defaultdict(list)
        for capability, requirement in self.requirements.items():
            for dependency in requirement.dependencies:
                if dependency not in self.requirements:
                    continue
                indegree[capability] += 1
                dependants[dependency].append(capability)
        queue = deque(sorted(capability for capability, degree in indegree.items() if degree == 0))
        order: list[str] = []
        while queue:
            capability = queue.popleft()
            order.append(capability)
            for dependant in sorted(dependants.get(capability, ())):
                indegree[dependant] -= 1
                if indegree[dependant] == 0:
                    queue.append(dependant)
        cycle = tuple(sorted(capability for capability, degree in indegree.items() if degree > 0))
        return tuple(order), cycle


class OwnerMatrix:
    def __init__(self, root: str | Path, requirements: Sequence[OwnerRequirement] | None = None) -> None:
        self.root = Path(root).resolve()
        self.requirements = tuple(requirements or default_owner_requirements())
        self.inspector = SourceInspector(self.root)

    def evaluate(
        self,
        *,
        registered_probe_ids: Iterable[str],
        scenario_ids: Iterable[str],
        executed_probe_ids: Iterable[str] = (),
        final_completion: bool = False,
    ) -> GateResult:
        result = GateResult(
            gate_id="m1-owner-matrix",
            status=GateStatus.NOT_RUN,
            summary="Canonical owner reachability, disable coverage and scenario responsibility matrix.",
        )
        requirements_by_capability = {item.capability: item for item in self.requirements}
        if len(requirements_by_capability) != len(self.requirements):
            duplicates = Counter(item.capability for item in self.requirements)
            for capability, count in duplicates.items():
                if count > 1:
                    result.add(
                        Finding(
                            code="owner.duplicate_capability",
                            severity=Severity.BLOCKER,
                            summary="Owner matrix grants duplicate capability responsibility.",
                            capability=capability,
                            detail=f"count={count}",
                        )
                    )
        result.findings.extend(OwnerDependencyGraph(self.requirements).validate())
        self._validate_unique_state_owners(result)
        registered = tuple(sorted(set(registered_probe_ids)))
        scenarios = tuple(sorted(set(scenario_ids)))
        executed = set(executed_probe_ids)
        observations: list[OwnerObservation] = []
        for requirement in self.requirements:
            observation = OwnerObservation(
                requirement=requirement,
                entrypoints=tuple(self.inspector.inspect(entry) for entry in requirement.entrypoints),
                registered_probe_ids=tuple(probe for probe in registered if probe in requirement.disable_probe_ids),
                scenario_ids=tuple(scenario for scenario in scenarios if scenario in requirement.required_for_scenarios),
            )
            observations.append(observation)
            self._findings_for(observation, result)
            if final_completion:
                if not set(requirement.disable_probe_ids) & executed:
                    result.add(
                        Finding(
                            code="owner.disable_not_executed",
                            severity=Severity.BLOCKER,
                            summary="Canonical owner disable probe was registered but not executed for final completion.",
                            capability=requirement.capability,
                            detail=", ".join(requirement.disable_probe_ids),
                        )
                    )
            result.evidence.extend(
                EvidencePointer(
                    kind="owner_entrypoint",
                    location=entry.path,
                    summary=f"{requirement.capability}: {'reachable' if entry.reachable else 'incomplete'}",
                    metadata={"canonical_owner": requirement.canonical_owner, "digest": entry.digest},
                )
                for entry in observation.entrypoints
            )
        missing_capabilities = set(REQUIRED_DISABLE_CAPABILITIES) - set(requirements_by_capability)
        for capability in sorted(missing_capabilities):
            result.add(
                Finding(
                    code="owner.required_capability_missing",
                    severity=Severity.BLOCKER,
                    summary="M1 final disable matrix omits a required capability.",
                    capability=capability,
                )
            )
        role_counts = Counter(item.requirement.source_role for item in observations)
        language_pairs = Counter(
            f"{item.requirement.source_language}->{item.requirement.target_language}" for item in observations
        )
        result.metrics.update(
            {
                "required_capability_count": len(self.requirements),
                "reachable_capability_count": sum(item.reachable for item in observations),
                "disable_covered_count": sum(item.disable_covered for item in observations),
                "scenario_covered_count": sum(item.scenario_covered for item in observations),
                "role_counts": dict(sorted(role_counts.items())),
                "language_pairs": dict(sorted(language_pairs.items())),
                "registered_probe_count": len(registered),
                "executed_probe_count": len(executed),
                "observations": [item.to_dict() for item in observations],
            }
        )
        return result.finish()

    def _validate_unique_state_owners(self, result: GateResult) -> None:
        by_state: dict[str, set[str]] = defaultdict(set)
        for requirement in self.requirements:
            by_state[requirement.state_family].add(requirement.canonical_owner)
        allowed_composites = {
            "memory": {"MemoryIndex/CuratorStateMachine", "SkillMemoryRuntime"},
            "worker_route": {"WorkerPoolStore", "LayeredSchedulerRuntime"},
            "workspace": {"WorkspaceManager", "SandboxGateway"},
        }
        for family, owners in sorted(by_state.items()):
            if len(owners) <= 1:
                continue
            if owners == allowed_composites.get(family, set()):
                continue
            result.add(
                Finding(
                    code="owner.state_family_ambiguous",
                    severity=Severity.BLOCKER,
                    summary="Owner matrix gives one canonical state family multiple writers.",
                    detail=f"{family}: {', '.join(sorted(owners))}",
                )
            )

    @staticmethod
    def _findings_for(observation: OwnerObservation, result: GateResult) -> None:
        requirement = observation.requirement
        if requirement.source_role not in {"primary", "supplementary", "zyra_primary"}:
            result.add(
                Finding(
                    code="owner.production_role_invalid",
                    severity=Severity.BLOCKER,
                    summary="Executable owner matrix entry has a non-production source role.",
                    capability=requirement.capability,
                    detail=requirement.source_role,
                )
            )
        for entry in observation.entrypoints:
            if not entry.exists:
                result.add(
                    Finding(
                        code="owner.entrypoint_missing",
                        severity=Severity.BLOCKER,
                        summary="Canonical owner production entrypoint is missing.",
                        capability=requirement.capability,
                        location=entry.path,
                    )
                )
            if entry.symbols_missing:
                result.add(
                    Finding(
                        code="owner.symbol_missing",
                        severity=Severity.BLOCKER,
                        summary="Canonical owner entrypoint lacks required executable symbols.",
                        capability=requirement.capability,
                        location=entry.path,
                        detail=", ".join(entry.symbols_missing),
                    )
                )
            if entry.route_patterns_missing:
                result.add(
                    Finding(
                        code="owner.route_missing",
                        severity=Severity.BLOCKER,
                        summary="Canonical owner is not wired to its required product route.",
                        capability=requirement.capability,
                        location=entry.path,
                        detail=", ".join(entry.route_patterns_missing),
                    )
                )
            if entry.event_patterns_missing:
                result.add(
                    Finding(
                        code="owner.event_missing",
                        severity=Severity.BLOCKER,
                        summary="Canonical owner lacks required event evidence wiring.",
                        capability=requirement.capability,
                        location=entry.path,
                        detail=", ".join(entry.event_patterns_missing),
                    )
                )
        if not observation.disable_covered:
            result.add(
                Finding(
                    code="owner.disable_probe_missing",
                    severity=Severity.BLOCKER,
                    summary="Canonical owner has no registered reversible disconnect probe.",
                    capability=requirement.capability,
                    detail=", ".join(requirement.disable_probe_ids),
                )
            )
        if not observation.scenario_covered:
            missing = set(requirement.required_for_scenarios) - set(observation.scenario_ids)
            result.add(
                Finding(
                    code="owner.scenario_missing",
                    severity=Severity.BLOCKER,
                    summary="Canonical owner is not covered by all required integration scenarios.",
                    capability=requirement.capability,
                    detail=", ".join(sorted(missing)),
                )
            )
        if not requirement.restore_semantics:
            result.add(
                Finding(
                    code="owner.restore_contract_missing",
                    severity=Severity.ERROR,
                    summary="Canonical state owner has no documented restore invariant in the executable matrix.",
                    capability=requirement.capability,
                )
            )


def _entry(
    path: str,
    *symbols: str,
    language: str = "Python",
    routes: Sequence[str] = (),
    events: Sequence[str] = (),
) -> EntrypointRequirement:
    return EntrypointRequirement(
        path=path,
        symbols=tuple(symbols),
        route_patterns=tuple(routes),
        event_patterns=tuple(events),
        language=language,
    )


def default_owner_requirements() -> tuple[OwnerRequirement, ...]:
    query = "m1-integration-query-session-context-tool"
    permission = "m1-integration-dangerous-tool-permission"
    mcp = "m1-integration-mcp-auth-elicitation"
    memory = "m1-integration-skill-memory-compact-restore"
    recovery = "m1-integration-subagent-worker-recovery"
    failover = "m1-integration-api-stream-provider-failover"
    return (
        OwnerRequirement(
            capability="query-session",
            canonical_owner="QueryEngine/CodeWorkerSessionFoundationRuntime",
            state_family="task_session",
            source_role="primary",
            source_repository="claude-code-best",
            source_language="TypeScript",
            target_language="TypeScript",
            entrypoints=(
                _entry("packages/runtime/claude-runtime/src/query-engine.ts", "ClaudeRuntimeCore", language="TypeScript"),
                _entry("packages/runtime/zyra_runtime/claude_session_store.py", "CodeWorkerSessionFoundationRuntime"),
            ),
            disable_probe_ids=("disable-query-session",),
            required_for_scenarios=(query, permission, mcp, memory),
            restore_semantics=("session revision and transcript digest survive exact resume",),
        ),
        OwnerRequirement(
            capability="tool-loop",
            canonical_owner="ClaudeToolLoopRuntime",
            state_family="tool_execution",
            source_role="primary",
            source_repository="claude-code-best",
            source_language="TypeScript",
            target_language="TypeScript",
            entrypoints=(
                _entry(
                    "packages/runtime/claude-runtime/src/query-engine.ts",
                    "ClaudeRuntimeCore",
                    language="TypeScript",
                    events=(r"tool", r"permission"),
                ),
            ),
            disable_probe_ids=("disable-tool-loop",),
            dependencies=("query-session",),
            required_for_scenarios=(query, permission, mcp),
            restore_semantics=("pending tool call settles once with stable tool-use identity",),
        ),
        OwnerRequirement(
            capability="mcp-runtime",
            canonical_owner="McpRuntimeCoordinator/McpConnectionRuntime",
            state_family="mcp_runtime",
            source_role="primary",
            source_repository="claude-code-best",
            source_language="TypeScript",
            target_language="TypeScript",
            entrypoints=(
                _entry(
                    "packages/integrations/claude-mcp/src/runtime/coordinator.ts",
                    "McpRuntimeCoordinator",
                    language="TypeScript",
                    events=(r"mcp", r"elicitation|auth"),
                ),
                _entry(
                    "packages/integrations/claude-mcp/src/connection/connection-runtime.ts",
                    "McpConnectionRuntime",
                    language="TypeScript",
                ),
            ),
            disable_probe_ids=("disable-mcp-runtime",),
            dependencies=("permission-runtime",),
            required_for_scenarios=(mcp,),
            restore_semantics=("connection generation, auth and elicitation custody survive restart",),
        ),
        OwnerRequirement(
            capability="permission-runtime",
            canonical_owner="PermissionJournal/PermissionApprovalRuntime",
            state_family="permission",
            source_role="primary",
            source_repository="claude-code-best",
            source_language="TypeScript",
            target_language="TypeScript",
            entrypoints=(
                _entry(
                    "packages/runtime/claude-runtime/src/permission/approval-runtime.ts",
                    "PermissionApprovalRuntime",
                    language="TypeScript",
                    events=(r"approval", r"decision"),
                ),
            ),
            disable_probe_ids=("disable-permission-runtime",),
            dependencies=("query-session", "tool-loop"),
            required_for_scenarios=(permission, mcp),
            restore_semantics=("approval binds one decision, policy revision and physical call",),
        ),
        OwnerRequirement(
            capability="workspace-runtime",
            canonical_owner="WorkspaceManager",
            state_family="workspace",
            source_role="primary",
            source_repository="agentscope",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/workspace/zyra_workspace/manager.py", "WorkspaceManagerRuntime"),
            ),
            disable_probe_ids=("disable-workspace-runtime",),
            dependencies=("permission-runtime",),
            required_for_scenarios=(query, permission),
            restore_semantics=("workspace revision and dirty paths are recovered without destructive reset",),
        ),
        OwnerRequirement(
            capability="sandbox-gateway",
            canonical_owner="SandboxGateway",
            state_family="workspace",
            source_role="zyra_primary",
            source_repository="zyra",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/runtime/zyra_runtime/sandbox_gateway/runtime.py", "SandboxGatewayRuntime"),
            ),
            disable_probe_ids=("disable-sandbox-gateway",),
            dependencies=("permission-runtime", "workspace-runtime"),
            required_for_scenarios=(query, permission),
            restore_semantics=("gateway grant and side-effect fence retain their idempotency binding",),
        ),
        OwnerRequirement(
            capability="runtime-event-spine",
            canonical_owner="RuntimeEventSpine",
            state_family="runtime_event",
            source_role="primary",
            source_repository="opencode",
            source_language="TypeScript",
            target_language="TypeScript",
            entrypoints=(
                _entry(
                    "packages/runtime/runtime-event-spine/src/runtime.ts",
                    "RuntimeEventSpine",
                    language="TypeScript",
                ),
            ),
            disable_probe_ids=("disable-runtime-event-spine",),
            required_for_scenarios=(query, permission, mcp, memory, recovery, failover),
            restore_semantics=("event sequence and digest chain resume monotonically",),
        ),
        OwnerRequirement(
            capability="provider-control-plane",
            canonical_owner="ProviderControlPlane",
            state_family="provider_backend",
            source_role="primary",
            source_repository="opencode",
            source_language="TypeScript",
            target_language="TypeScript",
            entrypoints=(
                _entry(
                    "packages/runtime/provider-control-plane/src/control-plane.ts",
                    "ProviderControlPlane",
                    language="TypeScript",
                ),
            ),
            disable_probe_ids=("disable-provider-control-plane",),
            dependencies=("runtime-event-spine",),
            required_for_scenarios=(query, failover),
            restore_semantics=("provider attempt and fallback chain remain correlation stable",),
        ),
        OwnerRequirement(
            capability="memory-retrieval",
            canonical_owner="MemoryIndex/CuratorStateMachine",
            state_family="memory",
            source_role="primary",
            source_repository="agentscope",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/memory/zyra_memory/integration_runtime.py", "RetrievalIntegrationRuntime"),
            ),
            disable_probe_ids=("disable-memory-retrieval",),
            dependencies=("runtime-event-spine",),
            required_for_scenarios=(memory,),
            restore_semantics=("retrieval snapshot and source revision restore together",),
        ),
        OwnerRequirement(
            capability="code-index",
            canonical_owner="CodeIndexStore",
            state_family="code_index",
            source_role="supplementary",
            source_repository="oh-my-pi",
            source_language="TypeScript/Rust",
            target_language="Python/TypeScript",
            entrypoints=(
                _entry("packages/code_index/zyra_code_index/store.py", "CodeIndexStore"),
            ),
            disable_probe_ids=("disable-code-index",),
            dependencies=("workspace-runtime",),
            required_for_scenarios=(query, recovery),
            restore_semantics=("index generation is bound to workspace content revision",),
        ),
        OwnerRequirement(
            capability="memory-curator",
            canonical_owner="MemoryIndex/CuratorStateMachine",
            state_family="memory",
            source_role="primary",
            source_repository="hermes-agent",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/memory/zyra_memory/curator_runtime.py", "MemoryCuratorWorker"),
            ),
            disable_probe_ids=("disable-memory-curator",),
            dependencies=("memory-retrieval",),
            required_for_scenarios=(memory,),
            restore_semantics=("validator decision and commit revision cannot be separated",),
        ),
        OwnerRequirement(
            capability="skill-memory-restore",
            canonical_owner="SkillMemoryRuntime",
            state_family="memory",
            source_role="primary",
            source_repository="claude-code-best",
            source_language="TypeScript",
            target_language="TypeScript",
            entrypoints=(
                _entry(
                    "packages/memory/skill-memory-runtime/src/restore-bridge.ts",
                    "CompactRestoreMemoryBridge",
                    language="TypeScript",
                ),
            ),
            disable_probe_ids=("disable-skill-memory-restore",),
            dependencies=("query-session", "memory-curator"),
            required_for_scenarios=(memory,),
            restore_semantics=("compact restore imports only committed procedure outcomes",),
        ),
        OwnerRequirement(
            capability="physical-worker",
            canonical_owner="WorkerPoolStore",
            state_family="worker_route",
            source_role="primary",
            source_repository="agentscope",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/scheduler/zyra_scheduler/worker_pool/store.py", "WorkerPoolStore"),
            ),
            disable_probe_ids=("disable-physical-worker",),
            dependencies=("runtime-event-spine",),
            required_for_scenarios=(recovery,),
            restore_semantics=("worker lease epoch and fencing token remain monotonic",),
        ),
        OwnerRequirement(
            capability="edge-worker",
            canonical_owner="WorkerPoolStore",
            state_family="worker_route",
            source_role="primary",
            source_repository="agentscope",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/workers/zyra_workers/edge_pool/runtime.py", "EdgeWorkerGatewayRuntime"),
            ),
            disable_probe_ids=("disable-edge-worker",),
            dependencies=("physical-worker",),
            required_for_scenarios=(recovery, failover),
            restore_semantics=("edge endpoint lease is independent and reconnect increments epoch",),
        ),
        OwnerRequirement(
            capability="watchdog",
            canonical_owner="WatchdogRuntime",
            state_family="fault",
            source_role="primary",
            source_repository="browser-use",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/scheduler/zyra_scheduler/fault_runtime/runtime.py", "RuntimeWatchdog"),
            ),
            disable_probe_ids=("disable-watchdog",),
            dependencies=("physical-worker", "runtime-event-spine"),
            required_for_scenarios=(recovery, failover),
            restore_semantics=("watchdog deadline and observed heartbeat revision restore exactly",),
        ),
        OwnerRequirement(
            capability="checkpoint-recovery",
            canonical_owner="RecoveryApplication/RecoveryPlanStore",
            state_family="recovery",
            source_role="zyra_primary",
            source_repository="zyra/langgraph-narrow-semantics",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/scheduler/zyra_scheduler/recovery_runtime/application.py", "RecoveryApplication"),
            ),
            disable_probe_ids=("disable-checkpoint-recovery",),
            dependencies=("runtime-event-spine", "watchdog", "graph-custody"),
            required_for_scenarios=(recovery, failover),
            restore_semantics=("checkpoint lineage and pending/committed writes resume exactly once",),
        ),
        OwnerRequirement(
            capability="layered-route",
            canonical_owner="LayeredSchedulerRuntime",
            state_family="worker_route",
            source_role="zyra_primary",
            source_repository="zyra",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/scheduler/zyra_scheduler/recovery_runtime/route_runtime.py", "LayeredRouteRuntime"),
            ),
            disable_probe_ids=("disable-layered-route",),
            dependencies=("physical-worker", "provider-control-plane"),
            required_for_scenarios=(recovery, failover),
            restore_semantics=("route revision and placement decision share a causation id",),
        ),
        OwnerRequirement(
            capability="graph-custody",
            canonical_owner="GraphStateStore",
            state_family="graph_topology",
            source_role="zyra_primary",
            source_repository="zyra/langgraph-narrow-semantics",
            source_language="Python",
            target_language="Python",
            entrypoints=(
                _entry("packages/orchestration/zyra_orchestration/graph_custody/store.py", "GraphStateStore"),
            ),
            disable_probe_ids=("disable-graph-state-store",),
            dependencies=("runtime-event-spine",),
            required_for_scenarios=(recovery,),
            restore_semantics=("immutable topology snapshot and branch delta commit atomically",),
        ),
    )
