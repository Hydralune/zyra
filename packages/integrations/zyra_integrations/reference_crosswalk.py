from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .ledger_models import to_jsonable
from .source_extraction import normalize_repo_path


CLAUDE_CODE_PRIMARY_REPO = "claude-code-best"

REFERENCE_ONLY_REPOSITORIES = {
    "claude-reviews-claude": {
        "root": "claudecode-related/claude-reviews-claude",
        "role": "architecture-chapter-crosscheck",
        "policy": "reference_only",
    },
    "Dive-into-Claude-Code": {
        "root": "claudecode-related/Dive-into-Claude-Code",
        "role": "design-space-crosscheck",
        "policy": "reference_only",
    },
}

CLAUDE_CODE_RUNTIME_CROSSWALK_SPECS = [
    {
        "crosswalk_id": "query-engine-session-loop",
        "capability": "QueryEngine session lifecycle and query loop",
        "owner_unit": "M1-02A",
        "downstream_units": ["M1-02B", "M1-02C", "M1-02D"],
        "source_paths": [
            "src/QueryEngine.ts",
            "src/query.ts",
            "src/services/tools/toolOrchestration.ts",
            "src/utils/toolResultStorage.ts",
        ],
        "reference_docs": [
            ("claude-reviews-claude", "architecture/zh-CN/01-query-engine.md"),
            ("claude-reviews-claude", "architecture/zh-CN/02-tool-system.md"),
            ("Dive-into-Claude-Code", "docs/architecture_zh.md"),
        ],
        "signals": [
            "QueryEngine wraps the session-level contract while query.ts owns turn-level iteration.",
            "Tool batches must preserve read-only concurrency and serial write semantics.",
            "Tool result budget and reactive compact are part of the runtime loop boundary.",
        ],
    },
    {
        "crosswalk_id": "tool-registry-and-tool-use-context",
        "capability": "Tool registry, ToolUseContext, base tools, and deferred tool discovery",
        "owner_unit": "M1-02A",
        "downstream_units": ["M1-02C", "M1-03A", "M1-03B"],
        "source_paths": [
            "src/Tool.ts",
            "src/tools.ts",
            "src/tools/FileReadTool",
            "src/tools/FileEditTool",
            "src/tools/FileWriteTool",
            "src/tools/GlobTool",
            "src/tools/GrepTool",
            "src/tools/TodoWriteTool",
            "src/tools/ToolSearchTool",
        ],
        "reference_docs": [
            ("claude-reviews-claude", "architecture/zh-CN/02-tool-system.md"),
            ("Dive-into-Claude-Code", "docs/build-your-own-agent_zh.md"),
        ],
        "signals": [
            "Built-in tools should remain a stable prompt-cache prefix before MCP tools.",
            "ToolSearch and shouldDefer keep large tool catalogs out of the initial context.",
            "ToolUseContext mutation is a first-class execution result, not ad hoc process state.",
        ],
    },
    {
        "crosswalk_id": "context-assembly-and-session-state",
        "capability": "Context assembly, session storage, and restore-safe runtime state",
        "owner_unit": "M1-02A",
        "downstream_units": ["M1-02B", "M1-02D", "M1-06C"],
        "source_paths": [
            "src/context.ts",
            "src/utils/queryContext.ts",
            "src/utils/sessionState.ts",
            "src/utils/sessionStorage.ts",
            "src/utils/messagePredicates.ts",
            "src/utils/messageQueueManager.ts",
            "src/utils/messages.ts",
        ],
        "reference_docs": [
            ("claude-reviews-claude", "architecture/zh-CN/09-session-persistence.md"),
            ("claude-reviews-claude", "architecture/zh-CN/10-context-assembly.md"),
            ("Dive-into-Claude-Code", "docs/architecture_zh.md"),
        ],
        "signals": [
            "Append-only session transcript and restore boundaries must be audit-friendly.",
            "Permissions are not silently restored across session boundaries.",
            "Context assembly is layered before model/tool execution.",
        ],
    },
    {
        "crosswalk_id": "compact-and-post-compact-restore",
        "capability": "Auto compact, reactive compact, micro compact, and post-compact restore",
        "owner_unit": "M1-02A",
        "downstream_units": ["M1-02D", "M1-06C"],
        "source_paths": [
            "src/services/compact",
        ],
        "reference_docs": [
            ("claude-reviews-claude", "architecture/zh-CN/11-compact-system.md"),
            ("Dive-into-Claude-Code", "docs/architecture_zh.md"),
            ("Dive-into-Claude-Code", "docs/build-your-own-agent_zh.md"),
        ],
        "signals": [
            "Compaction should progress from lowest-loss shaping to full summary.",
            "Reactive compaction is bounded and should not loop without a circuit breaker.",
            "Post-compact cleanup and restore hooks are runtime behavior, not documentation debt.",
        ],
    },
    {
        "crosswalk_id": "permission-mcp-skill-subagent-handoff",
        "capability": "Permission, MCP, SkillTool, and AgentTool productization handoff",
        "owner_unit": "M1-02A",
        "downstream_units": ["M1-03A", "M1-03B", "M1-03C", "M1-03D"],
        "source_paths": [
            "src/hooks/toolPermission",
            "src/services/mcp",
            "src/tools/SkillTool",
            "src/tools/AgentTool",
            "src/tools/MCPTool",
            "src/tools/McpAuthTool",
            "src/tools/ListMcpResourcesTool",
            "src/tools/ReadMcpResourceTool",
        ],
        "reference_docs": [
            ("claude-reviews-claude", "architecture/zh-CN/07-permission-pipeline.md"),
            ("claude-reviews-claude", "architecture/zh-CN/08-agent-swarms.md"),
            ("claude-reviews-claude", "architecture/zh-CN/15-services-api-layer.md"),
            ("Dive-into-Claude-Code", "docs/architecture_zh.md"),
        ],
        "signals": [
            "Permission checks need deny-first ordering and hook/user/classifier handoff.",
            "MCP is a high-context extension path and should stay separate from low-cost skills.",
            "AgentTool should isolate subagent context and return summaries/artifacts to the parent.",
        ],
    },
    {
        "crosswalk_id": "bash-and-workspace-action-safety",
        "capability": "Bash, filesystem, and workspace action safety boundary",
        "owner_unit": "M1-02A",
        "downstream_units": ["M1-03A", "M1-05A", "M1-05B"],
        "source_paths": [
            "src/tools/BashTool",
            "src/tools/WebFetchTool",
            "src/tools/WebSearchTool",
        ],
        "reference_docs": [
            ("claude-reviews-claude", "architecture/zh-CN/06-bash-engine.md"),
            ("claude-reviews-claude", "architecture/zh-CN/07-permission-pipeline.md"),
            ("Dive-into-Claude-Code", "docs/build-your-own-agent_zh.md"),
        ],
        "signals": [
            "Command execution safety combines parser checks, sandbox policy, and permissions.",
            "Approval fatigue should be handled by better boundaries, not by weakening checks.",
            "Workspace and network actions need separate runtime evidence paths.",
        ],
    },
]


@dataclass(frozen=True, slots=True)
class ReferenceOnlyRepository:
    name: str
    workspace_root: str
    role: str
    policy: str = "reference_only"
    exists: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ReferenceDoc:
    repository: str
    path: str
    role: str
    exists: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class CrosswalkSource:
    repository: str
    path: str
    role: str
    exists: bool = False
    is_primary_source: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class CrosswalkTarget:
    path: str
    exists: bool = False
    required_for_runtime: bool = True

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ReferenceCrosswalkEntry:
    crosswalk_id: str
    capability: str
    owner_unit: str
    downstream_units: list[str]
    primary_sources: list[CrosswalkSource]
    reference_docs: list[ReferenceDoc]
    targets: list[CrosswalkTarget]
    signals: list[str] = field(default_factory=list)
    reference_only_policy: str = "reference_only_docs_do_not_count_as_runtime_code"

    @property
    def missing_source_count(self) -> int:
        return sum(1 for source in self.primary_sources if not source.exists)

    @property
    def missing_reference_count(self) -> int:
        return sum(1 for doc in self.reference_docs if not doc.exists)

    @property
    def missing_target_count(self) -> int:
        return sum(1 for target in self.targets if target.required_for_runtime and not target.exists)

    @property
    def ok(self) -> bool:
        return self.missing_source_count == 0 and self.missing_reference_count == 0 and self.missing_target_count == 0

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["ok"] = self.ok
        payload["missing_source_count"] = self.missing_source_count
        payload["missing_reference_count"] = self.missing_reference_count
        payload["missing_target_count"] = self.missing_target_count
        return payload


@dataclass(frozen=True, slots=True)
class ReferenceCrosswalkReport:
    primary_repo: str
    reference_repositories: list[ReferenceOnlyRepository]
    entries: list[ReferenceCrosswalkEntry]
    target_mount: str

    @property
    def missing_source_count(self) -> int:
        return sum(entry.missing_source_count for entry in self.entries)

    @property
    def missing_reference_count(self) -> int:
        return sum(entry.missing_reference_count for entry in self.entries)

    @property
    def missing_target_count(self) -> int:
        return sum(entry.missing_target_count for entry in self.entries)

    @property
    def ok(self) -> bool:
        return (
            all(repo.exists for repo in self.reference_repositories)
            and self.missing_source_count == 0
            and self.missing_reference_count == 0
            and self.missing_target_count == 0
        )

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "primary_repo": self.primary_repo,
            "reference_only_repos": [repo.name for repo in self.reference_repositories],
            "entry_count": len(self.entries),
            "target_mount": self.target_mount,
            "missing_source_count": self.missing_source_count,
            "missing_reference_count": self.missing_reference_count,
            "missing_target_count": self.missing_target_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "reference_repositories": [repo.to_dict() for repo in self.reference_repositories],
            "entries": [entry.to_dict() for entry in self.entries],
        }


class ReferenceCrosswalkError(RuntimeError):
    pass


def build_claude_code_reference_crosswalk(
    *,
    project_root: Path,
    source_workspace_root: Path,
    target_mount: str = "productized/claude-code-best",
) -> ReferenceCrosswalkReport:
    raise ReferenceCrosswalkError(
        "Legacy source/reference crosswalk generation is retired. The first-stage "
        "crosswalk is frozen by the P2-S02A-01 Git-object retirement manifest."
    )
    workspace_root = source_workspace_root.resolve()
    project = project_root.resolve()
    reference_repositories = [
        _reference_repo(workspace_root, name, data)
        for name, data in sorted(REFERENCE_ONLY_REPOSITORIES.items())
    ]
    entries = [
        _crosswalk_entry(
            project_root=project,
            source_workspace_root=workspace_root,
            target_mount=target_mount,
            spec=spec,
        )
        for spec in CLAUDE_CODE_RUNTIME_CROSSWALK_SPECS
    ]
    return ReferenceCrosswalkReport(
        primary_repo=CLAUDE_CODE_PRIMARY_REPO,
        reference_repositories=reference_repositories,
        entries=entries,
        target_mount=target_mount,
    )


def write_claude_code_reference_crosswalk(
    *,
    project_root: Path,
    source_workspace_root: Path,
    output_path: Path | None = None,
    target_mount: str = "productized/claude-code-best",
) -> Path:
    raise ReferenceCrosswalkError(
        "Legacy source/reference crosswalk writing is retired; no current runtime "
        "or evidence path may be regenerated from an external source workspace."
    )
    project = project_root.resolve()
    report = build_claude_code_reference_crosswalk(
        project_root=project,
        source_workspace_root=source_workspace_root,
        target_mount=target_mount,
    )
    assert_reference_crosswalk(report)
    target = output_path or project / "docs" / "reviews" / "evidence" / "retired-crosswalk.json"
    target = target if target.is_absolute() else project / target
    try:
        target.resolve().relative_to(project)
    except ValueError as error:
        raise ReferenceCrosswalkError(f"crosswalk output escapes project root: {target}") from error
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def assert_reference_crosswalk(report: ReferenceCrosswalkReport) -> None:
    if report.ok:
        return
    problems: list[str] = []
    for repo in report.reference_repositories:
        if not repo.exists:
            problems.append(f"missing reference-only repo: {repo.name}")
    for entry in report.entries:
        for source in entry.primary_sources:
            if not source.exists:
                problems.append(f"{entry.crosswalk_id}: missing primary source {source.path}")
        for doc in entry.reference_docs:
            if not doc.exists:
                problems.append(f"{entry.crosswalk_id}: missing reference doc {doc.repository}/{doc.path}")
        for target in entry.targets:
            if target.required_for_runtime and not target.exists:
                problems.append(f"{entry.crosswalk_id}: missing productized target {target.path}")
    raise ReferenceCrosswalkError("\n".join(problems))


def crosswalk_entries_for_source(report: ReferenceCrosswalkReport, source_path: str) -> list[ReferenceCrosswalkEntry]:
    normalized = normalize_repo_path(source_path)
    return [
        entry
        for entry in report.entries
        if any(_source_path_matches(normalized, source.path) for source in entry.primary_sources)
    ]


def reference_metadata_for_source(report: ReferenceCrosswalkReport, source_path: str) -> dict[str, Any]:
    entries = crosswalk_entries_for_source(report, source_path)
    return {
        "reference_only": True,
        "primary_repo": report.primary_repo,
        "source_path": normalize_repo_path(source_path),
        "crosswalk_ids": [entry.crosswalk_id for entry in entries],
        "reference_docs": sorted(
            {
                f"{doc.repository}/{doc.path}"
                for entry in entries
                for doc in entry.reference_docs
            }
        ),
    }


def iter_crosswalk_source_paths() -> Iterable[str]:
    seen: set[str] = set()
    for spec in CLAUDE_CODE_RUNTIME_CROSSWALK_SPECS:
        for source_path in spec["source_paths"]:
            normalized = normalize_repo_path(str(source_path))
            if normalized in seen:
                continue
            seen.add(normalized)
            yield normalized


def _reference_repo(workspace_root: Path, name: str, data: dict[str, str]) -> ReferenceOnlyRepository:
    relative_root = normalize_repo_path(data["root"])
    return ReferenceOnlyRepository(
        name=name,
        workspace_root=relative_root,
        role=data["role"],
        policy=data.get("policy", "reference_only"),
        exists=(workspace_root / relative_root).exists(),
    )


def _crosswalk_entry(
    *,
    project_root: Path,
    source_workspace_root: Path,
    target_mount: str,
    spec: dict[str, Any],
) -> ReferenceCrosswalkEntry:
    source_paths = [normalize_repo_path(str(path)) for path in spec["source_paths"]]
    primary_sources = [
        CrosswalkSource(
            repository=CLAUDE_CODE_PRIMARY_REPO,
            path=source_path,
            role="canonical_runtime_source",
            exists=_source_exists(source_workspace_root, source_path),
            is_primary_source=True,
        )
        for source_path in source_paths
    ]
    reference_docs = [
        ReferenceDoc(
            repository=str(repo),
            path=normalize_repo_path(str(doc_path)),
            role="reference_only_crosscheck",
            exists=_reference_doc_exists(source_workspace_root, str(repo), str(doc_path)),
        )
        for repo, doc_path in spec["reference_docs"]
    ]
    targets = [
        CrosswalkTarget(
            path=_target_path(target_mount, source_path),
            exists=(project_root / _target_path(target_mount, source_path)).exists(),
        )
        for source_path in source_paths
    ]
    return ReferenceCrosswalkEntry(
        crosswalk_id=str(spec["crosswalk_id"]),
        capability=str(spec["capability"]),
        owner_unit=str(spec["owner_unit"]),
        downstream_units=[str(item) for item in spec["downstream_units"]],
        primary_sources=primary_sources,
        reference_docs=reference_docs,
        targets=targets,
        signals=[str(item) for item in spec["signals"]],
    )


def _source_exists(source_workspace_root: Path, source_path: str) -> bool:
    return (source_workspace_root / CLAUDE_CODE_PRIMARY_REPO / normalize_repo_path(source_path)).exists()


def _reference_doc_exists(source_workspace_root: Path, repo: str, doc_path: str) -> bool:
    repo_data = REFERENCE_ONLY_REPOSITORIES.get(repo)
    if not repo_data:
        return False
    return (source_workspace_root / normalize_repo_path(repo_data["root"]) / normalize_repo_path(doc_path)).exists()


def _target_path(target_mount: str, source_path: str) -> str:
    del target_mount, source_path
    raise ReferenceCrosswalkError(
        "Legacy crosswalk target projection is retired."
    )


def _source_path_matches(requested_path: str, crosswalk_path: str) -> bool:
    normalized_crosswalk = normalize_repo_path(crosswalk_path)
    return requested_path == normalized_crosswalk or requested_path.startswith(normalized_crosswalk.rstrip("/") + "/")
