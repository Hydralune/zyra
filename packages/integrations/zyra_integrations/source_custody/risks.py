from __future__ import annotations

import io
import re
import tokenize
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .catalog import SourceCatalog
from .custody import CustodyIndex
from .dependencies import DependencyGraph
from .javascript_analyzer import JavaScriptFileAnalysis, Token, JavaScriptLexer
from .model import (
    AuditSection,
    Disposition,
    Evidence,
    EvidenceKind,
    Finding,
    RuleSwitches,
    Severity,
    SourceEntry,
    content_digest,
    finding,
    section,
    stable_digest,
)
from .processes import ProcessCatalog, ProcessUse
from .python_analyzer import PythonFileAnalysis
from .repository import RepositoryFile, RepositoryInventory


STRUCTURAL_REQUIREMENTS: Mapping[str, Mapping[str, Any]] = {
    "dynamic_topology": {
        "source_terms": {
            "add_node",
            "remove_node",
            "add_edge",
            "remove_edge",
            "topology_revision",
        },
        "test_terms": {"add_node", "remove_node", "add_edge", "remove_edge"},
        "minimum_source_terms": 5,
        "minimum_test_terms": 4,
        "impact": "Runtime topology evolution is not evidenced.",
    },
    "immutable_graph_state": {
        "source_terms": {
            "read_set",
            "write_set",
            "branch_local_delta",
            "conflict",
            "commit",
        },
        "test_terms": {"branch_local_delta", "write_conflict", "deterministic"},
        "minimum_source_terms": 5,
        "minimum_test_terms": 2,
        "impact": "Branch isolation and deterministic commit are not evidenced.",
    },
    "checkpoint_exact_resume": {
        "source_terms": {
            "pending_writes",
            "committed_writes",
            "checkpoint",
            "lineage",
            "idempotency",
            "resume",
        },
        "test_terms": {"pending", "committed", "resume", "crash"},
        "minimum_source_terms": 5,
        "minimum_test_terms": 3,
        "impact": "Narrow exact-resume semantics are not evidenced.",
    },
    "code_worker_loop": {
        "source_terms": {
            "query",
            "tool",
            "observe",
            "revise",
            "permission",
            "budget",
            "compact",
            "session",
        },
        "test_terms": {"tool_loop", "permission", "compact", "session"},
        "minimum_source_terms": 7,
        "minimum_test_terms": 3,
        "impact": "Cohesive reason/tool/observe/revise custody is not evidenced.",
    },
}
SOURCE_SPECIFIC_PATTERNS: Mapping[str, tuple[tuple[str, str, Severity], ...]] = {
    "OpenHands": (
        (
            r"(?i)(?:from|import)\s+openhands(?:_sdk|\.sdk)|@openhands/sdk",
            "openhands_external_sdk_runtime",
            Severity.BLOCKER,
        ),
        (
            r"(?i)(?:docker|sandbox|runtime)\s*(?:image|service)\s*[:=].*openhands",
            "openhands_external_runtime",
            Severity.BLOCKER,
        ),
    ),
    "hermes-agent": (
        (
            r"(?i)(?:HOME|USERPROFILE|expanduser|Path\.home).*hermes",
            "hermes_home_state",
            Severity.ERROR,
        ),
        (
            r"(?i)(?:sqlite|\.db\b).*hermes|hermes.*(?:sqlite|\.db\b)",
            "hermes_sqlite_state",
            Severity.ERROR,
        ),
        (
            r"(?i)(?:spawn|Popen|exec).*hermes|hermes.*(?:cli|command)",
            "hermes_cli_runtime",
            Severity.BLOCKER,
        ),
    ),
    "opencode": (
        (
            r"(?i)opencode.*(?:v1|v2)|(?:v1|v2).*opencode",
            "opencode_v1_v2_dual_runtime",
            Severity.ERROR,
        ),
        (
            r"(?i)(?:npm|bun|pnpm|yarn).*(?:install|add).*(?:opencode)",
            "opencode_dynamic_install",
            Severity.BLOCKER,
        ),
        (
            r"(?i)opencode.*(?:job|worker|server|process)",
            "opencode_job_process",
            Severity.ERROR,
        ),
    ),
    "oh-my-pi": (
        (
            r"(?i)(?:oh-my-pi|\bomp\b|mnemopi).*(?:jsonl|sqlite|\.db\b)",
            "omp_local_state",
            Severity.ERROR,
        ),
        (
            (
                r"(?i)(?:(?:subprocess\.(?:run|call|check_call|check_output|Popen)"
                r"|asyncio\.create_subprocess_(?:exec|shell)|Bun\.spawn|spawnSync?"
                r"|execFileSync?|child_process\.(?:spawn|execFile))\s*\("
                r"[^\r\n]{0,320}(?:oh-my-pi|\bomp\b)"
                r"|(?:bun|npm|pnpm|yarn)\s+run\s+(?:oh-my-pi|omp)\b)"
            ),
            "omp_external_process",
            Severity.BLOCKER,
        ),
        (
            r"(?i)(?:oh-my-pi|\bomp\b).*(?:\.node\b|\.wasm\b|native|rust)",
            "omp_native_boundary",
            Severity.ERROR,
        ),
    ),
}
LANGGRAPH_FORBIDDEN_NAMES = frozenset(
    {
        "StateGraph",
        "MessageGraph",
        "Pregel",
        "PregelRunner",
        "BinaryOperatorAggregate",
        "LastValue",
        "DeltaChannel",
        "ToolNode",
        "ToolRuntime",
        "create_react_agent",
        "createReactAgent",
        "InMemoryStore",
        "RemoteGraph",
        "GraphRunStream",
        "StreamController",
    }
)
REASONING_MICRO_NODE_PATTERN = re.compile(
    r"(?i)(?:add_node|node\s*[:=])\s*\(?\s*[\"']"
    r"(reason|think|tool|observe|revise)[\"']"
)
OPAQUE_EXTENSIONS = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".node",
        ".wasm",
        ".bin",
        ".jar",
        ".whl",
        ".zip",
        ".tar",
        ".tgz",
        ".7z",
    }
)
SOURCE_AUDITED_INTERPRETERS = frozenset(
    {
        "python.exe",
        "python3.exe",
        "node.exe",
        "bun.exe",
        "deno.exe",
        "python",
        "python3",
        "node",
        "bun",
        "deno",
    }
)
VIRTUAL_ENV_INTERPRETER = re.compile(
    r"^(?:\.?venv)?scripts(?:python(?:3)?|node|bun|deno)(?:\.exe)?$",
    re.IGNORECASE,
)


def source_audited_interpreter(executable: str) -> bool:
    """Recognize declared interpreters even after package-script normalization.

    The package scanner intentionally removes shell separators while parsing
    JSON command strings.  On Windows that turns
    ``.venv/Scripts/python.exe`` into ``.venvscriptspython.exe``.  This
    structural check accepts only the known interpreter leaf or that exact
    virtual-environment shape; arbitrary native executables remain opaque.
    """

    normalized = str(executable or "").strip().replace("\\", "/")
    leaf = PurePosixPath(normalized).name.casefold()
    if leaf in SOURCE_AUDITED_INTERPRETERS:
        return True
    compact = re.sub(r"[/\s:_-]+", "", normalized).casefold()
    return VIRTUAL_ENV_INTERPRETER.fullmatch(compact) is not None


@dataclass(frozen=True, slots=True)
class StructuralEvidence:
    capability: str
    entry_ids: tuple[str, ...]
    source_terms: Mapping[str, tuple[str, ...]]
    test_terms: Mapping[str, tuple[str, ...]]
    source_complete: bool
    test_complete: bool
    digest: str

    @property
    def valid(self) -> bool:
        return self.source_complete and self.test_complete

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "entry_ids": list(self.entry_ids),
            "source_terms": {
                path: list(terms) for path, terms in self.source_terms.items()
            },
            "test_terms": {
                path: list(terms) for path, terms in self.test_terms.items()
            },
            "source_complete": self.source_complete,
            "test_complete": self.test_complete,
            "valid": self.valid,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class SimilarityPair:
    production_path: str
    source_pool_path: str
    score: float
    shared_fingerprints: int
    production_fingerprints: int
    source_fingerprints: int
    exact: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "production_path": self.production_path,
            "source_pool_path": self.source_pool_path,
            "score": round(self.score, 6),
            "shared_fingerprints": self.shared_fingerprints,
            "production_fingerprints": self.production_fingerprints,
            "source_fingerprints": self.source_fingerprints,
            "exact": self.exact,
        }


@dataclass(frozen=True, slots=True)
class RiskIndex:
    structural_evidence: tuple[StructuralEvidence, ...]
    similarity_pairs: tuple[SimilarityPair, ...]
    source_specific_hits: Mapping[str, int]
    langgraph_forbidden_hits: int
    opaque_runtime_hits: int
    digest: str

    def to_summary(self) -> dict[str, Any]:
        return {
            "structural_capabilities": len(self.structural_evidence),
            "structural_valid": sum(item.valid for item in self.structural_evidence),
            "similarity_pairs": len(self.similarity_pairs),
            "source_specific_hits": dict(self.source_specific_hits),
            "langgraph_forbidden_hits": self.langgraph_forbidden_hits,
            "opaque_runtime_hits": self.opaque_runtime_hits,
            "digest": self.digest,
        }


class RiskAuditor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
        similarity_threshold: float = 0.86,
        maximum_similarity_files: int = 1800,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()
        self.similarity_threshold = similarity_threshold
        self.maximum_similarity_files = maximum_similarity_files

    def audit(
        self,
        inventory: RepositoryInventory,
        catalog: SourceCatalog | None,
        dependencies: DependencyGraph,
        processes: ProcessCatalog | None,
        process_uses: Sequence[ProcessUse],
        custody: CustodyIndex,
        python: Sequence[PythonFileAnalysis],
        javascript: Sequence[JavaScriptFileAnalysis],
    ) -> tuple[RiskIndex, AuditSection]:
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        structural: list[StructuralEvidence] = []
        if catalog is not None:
            structural, structural_findings = self._structural_evidence(catalog)
            findings.extend(structural_findings)
        source_hits: Counter[str] = Counter()
        if self.switches.source_specific:
            hits, source_findings = self._source_specific(inventory)
            source_hits.update(hits)
            findings.extend(source_findings)
        langgraph_findings: list[Finding] = []
        if self.switches.langgraph:
            langgraph_findings.extend(self._reasoning_cohesion_scan(inventory))
            findings.extend(langgraph_findings)
        similarity_pairs: list[SimilarityPair] = []
        if self.switches.opaque:
            similarity_pairs, similarity_findings = self._similarity_scan(
                inventory, custody
            )
            findings.extend(similarity_findings)
        opaque_findings = self._opaque_runtime_custody(
            inventory, dependencies, processes, process_uses
        )
        if self.switches.opaque:
            findings.extend(opaque_findings)
        index_payload = {
            "structural": [item.to_dict() for item in structural],
            "similarity": [item.to_dict() for item in similarity_pairs],
            "source_specific_hits": dict(source_hits),
            "langgraph_forbidden_hits": len(langgraph_findings),
            "opaque_runtime_hits": len(opaque_findings),
        }
        index = RiskIndex(
            structural_evidence=tuple(structural),
            similarity_pairs=tuple(similarity_pairs),
            source_specific_hits=dict(sorted(source_hits.items())),
            langgraph_forbidden_hits=len(langgraph_findings),
            opaque_runtime_hits=len(opaque_findings),
            digest=stable_digest(index_payload),
        )
        for item in structural:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.TEST,
                    path="source-custody://structural",
                    excerpt_digest=item.digest,
                    attributes={
                        "capability": item.capability,
                        "valid": item.valid,
                    },
                )
            )
        return index, section(
            "risks",
            metrics=index.to_summary(),
            findings=findings,
            evidence=evidence,
        )

    def _structural_evidence(
        self, catalog: SourceCatalog
    ) -> tuple[list[StructuralEvidence], list[Finding]]:
        results: list[StructuralEvidence] = []
        findings: list[Finding] = []
        by_capability = catalog.by_capability()
        for capability, requirement in STRUCTURAL_REQUIREMENTS.items():
            entries = tuple(
                entry for entry in by_capability.get(capability, ()) if entry.active
            )
            if not entries:
                findings.append(
                    finding(
                        "structural_capability_entry_missing",
                        f"Required active structural capability {capability!r} is absent.",
                        "source_specific",
                        severity=Severity.BLOCKER,
                        capability=capability,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Bind the existing Zyra owner and evidence paths in the catalog.",
                        default_path_impact=requirement["impact"],
                    )
                )
                continue
            source_terms = self._terms_in_paths(
                (path for entry in entries for path in entry.target_paths),
                requirement["source_terms"],
            )
            test_terms = self._terms_in_paths(
                (path for entry in entries for path in entry.test_paths),
                requirement["test_terms"],
            )
            source_found = {
                term for terms in source_terms.values() for term in terms
            }
            test_found = {term for terms in test_terms.values() for term in terms}
            source_complete = (
                len(source_found) >= int(requirement["minimum_source_terms"])
            )
            test_complete = len(test_found) >= int(requirement["minimum_test_terms"])
            payload = {
                "capability": capability,
                "entries": [entry.entry_id for entry in entries],
                "source_terms": source_terms,
                "test_terms": test_terms,
                "source_complete": source_complete,
                "test_complete": test_complete,
            }
            result = StructuralEvidence(
                capability=capability,
                entry_ids=tuple(entry.entry_id for entry in entries),
                source_terms=source_terms,
                test_terms=test_terms,
                source_complete=source_complete,
                test_complete=test_complete,
                digest=stable_digest(payload),
            )
            results.append(result)
            if not source_complete:
                findings.append(
                    finding(
                        "structural_source_evidence_incomplete",
                        f"Source evidence for {capability!r} lacks required semantics.",
                        "source_specific",
                        severity=Severity.BLOCKER,
                        capability=capability,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Point the catalog at executable owner paths with the required behavior.",
                        default_path_impact=requirement["impact"],
                        attributes={
                            "found_terms": sorted(source_found),
                            "required_terms": sorted(requirement["source_terms"]),
                        },
                    )
                )
            if not test_complete:
                findings.append(
                    finding(
                        "structural_test_evidence_incomplete",
                        f"Behavior tests for {capability!r} lack required adversarial semantics.",
                        "source_specific",
                        severity=Severity.BLOCKER,
                        capability=capability,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Add or bind real behavior/disable/crash/conflict tests.",
                        default_path_impact=requirement["impact"],
                        attributes={
                            "found_terms": sorted(test_found),
                            "required_terms": sorted(requirement["test_terms"]),
                        },
                    )
                )
        return results, findings

    def _terms_in_paths(
        self, paths: Iterable[str], terms: Iterable[str]
    ) -> dict[str, tuple[str, ...]]:
        expected = {term.casefold(): term for term in terms}
        result: dict[str, tuple[str, ...]] = {}
        visited: set[str] = set()
        for raw_path in paths:
            normalized = raw_path.replace("\\", "/")
            if normalized in visited:
                continue
            visited.add(normalized)
            candidate = self.project_root / normalized
            selected_files: Iterable[Path]
            if candidate.is_dir():
                selected_files = (
                    path
                    for path in candidate.rglob("*")
                    if path.is_file()
                    and path.suffix.casefold()
                    in {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
                )
            elif candidate.is_file():
                selected_files = (candidate,)
            else:
                continue
            for path in selected_files:
                try:
                    source = path.read_text(encoding="utf-8").casefold()
                except (OSError, UnicodeDecodeError):
                    continue
                found = tuple(
                    sorted(original for lowered, original in expected.items() if lowered in source)
                )
                if found:
                    display = path.relative_to(self.project_root).as_posix()
                    result[display] = found
        return dict(sorted(result.items()))

    def _source_specific(
        self, inventory: RepositoryInventory
    ) -> tuple[Counter[str], list[Finding]]:
        hits: Counter[str] = Counter()
        findings: list[Finding] = []
        for record in inventory.files:
            if record.kind != "source":
                continue
            if record.path.startswith(
                (
                    "vendor/",
                    "vendor-runtimes/",
                    "docs/",
                    "tests/",
                    "packages/integrations/zyra_integrations/source_custody/",
                )
            ):
                continue
            if record.size > 2 * 1024 * 1024 or record.binary:
                continue
            try:
                source = (self.project_root / record.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for repository, rules in SOURCE_SPECIFIC_PATTERNS.items():
                for pattern, code, severity in rules:
                    for match in re.finditer(pattern, source):
                        line = source[: match.start()].count("\n") + 1
                        runtime_scope = _runtime_source_scope(record.path)
                        effective_severity = (
                            severity
                            if runtime_scope or severity is not Severity.BLOCKER
                            else Severity.WARNING
                        )
                        hits[code] += 1
                        findings.append(
                            finding(
                                code,
                                f"Source-specific custody risk for {repository}.",
                                "source_specific",
                                severity=effective_severity,
                                source_repo=repository,
                                path=record.path,
                                line=line,
                                disposition=(
                                    Disposition.BLOCK_RELEASE
                                    if effective_severity is Severity.BLOCKER
                                    else Disposition.DECLARE
                                ),
                                remediation=source_specific_remediation(code),
                                default_path_impact=source_specific_impact(code),
                                attributes={
                                    "match_digest": content_digest(
                                        match.group(0).encode()
                                    ),
                                    "runtime_scope": runtime_scope,
                                },
                            )
                        )
        return hits, findings

    def _langgraph_symbol_scan(
        self, inventory: RepositoryInventory
    ) -> list[Finding]:
        findings: list[Finding] = []
        symbol_pattern = re.compile(
            r"\b(" + "|".join(re.escape(item) for item in LANGGRAPH_FORBIDDEN_NAMES) + r")\b"
        )
        for record in inventory.files:
            if record.kind != "source" or record.path.startswith(
                (
                    "vendor/",
                    "vendor-runtimes/",
                    "tests/",
                    "packages/integrations/zyra_integrations/source_custody/",
                )
            ):
                continue
            if record.size > 2 * 1024 * 1024:
                continue
            try:
                source = (self.project_root / record.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "langgraph" not in source.casefold():
                continue
            for match in symbol_pattern.finditer(source):
                line = source[: match.start()].count("\n") + 1
                context = source[max(0, match.start() - 160) : match.end() + 160]
                if any(
                    qualifier in context.casefold()
                    for qualifier in (
                        "forbidden",
                        "reject",
                        "reference",
                        "conformance",
                        "negative",
                        "not import",
                    )
                ):
                    continue
                findings.append(
                    finding(
                        "langgraph_forbidden_symbol_reachable",
                        f"Production source refers to forbidden broad LangGraph symbol {match.group(1)!r}.",
                        "langgraph",
                        severity=Severity.BLOCKER,
                        source_repo="langgraph",
                        path=record.path,
                        line=line,
                        disposition=Disposition.REMOVE,
                        remediation="Keep only narrow exact-resume semantics in Zyra-owned code.",
                        default_path_impact="Broad LangGraph runtime may enter the default release path.",
                        attributes={"symbol": match.group(1)},
                    )
                )
        return findings

    def _reasoning_cohesion_scan(
        self, inventory: RepositoryInventory
    ) -> list[Finding]:
        findings: list[Finding] = []
        grouped: dict[str, set[str]] = defaultdict(set)
        locations: dict[str, tuple[str, int]] = {}
        for record in inventory.files:
            if record.kind != "source" or record.path.startswith(
                ("vendor/", "vendor-runtimes/", "tests/")
            ):
                continue
            if record.size > 2 * 1024 * 1024:
                continue
            try:
                source = (self.project_root / record.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in REASONING_MICRO_NODE_PATTERN.finditer(source):
                node = match.group(1).casefold()
                directory = PurePosixPath(record.path).parent.as_posix()
                grouped[directory].add(node)
                locations[f"{directory}:{node}"] = (
                    record.path,
                    source[: match.start()].count("\n") + 1,
                )
        for directory, nodes in grouped.items():
            if len(nodes) >= 3:
                path, line = locations[f"{directory}:{sorted(nodes)[0]}"]
                findings.append(
                    finding(
                        "code_worker_reasoning_micro_nodes",
                        "Reason/tool/observe/revise phases appear split into graph micro-nodes.",
                        "langgraph",
                        severity=Severity.BLOCKER,
                        source_repo="langgraph",
                        path=path,
                        line=line,
                        disposition=Disposition.ABSORB,
                        remediation="Keep the cohesive CodeWorker loop inside its runtime; graph only durable handoffs.",
                        default_path_impact="Shared-state micro-nodes can break reasoning cohesion.",
                        attributes={"directory": directory, "nodes": sorted(nodes)},
                    )
                )
        return findings

    def _similarity_scan(
        self,
        inventory: RepositoryInventory,
        custody: CustodyIndex,
    ) -> tuple[list[SimilarityPair], list[Finding]]:
        production = [
            item
            for item in inventory.files
            if item.kind == "source"
            and item.path.startswith(("apps/", "packages/"))
            and not item.vendor_like
            and 200 <= item.size <= 500_000
        ]
        source_pool = [
            item
            for item in inventory.files
            if item.kind == "source"
            and item.path.startswith(("vendor/", "vendor-runtimes/"))
            and 200 <= item.size <= 500_000
        ]
        production = production[: self.maximum_similarity_files]
        source_pool = source_pool[: self.maximum_similarity_files]
        by_digest: dict[str, list[RepositoryFile]] = defaultdict(list)
        by_name: dict[tuple[str, str], list[RepositoryFile]] = defaultdict(list)
        for item in source_pool:
            by_digest[item.digest].append(item)
            by_name[(PurePosixPath(item.path).name.casefold(), item.suffix)].append(item)
        pairs: list[SimilarityPair] = []
        findings: list[Finding] = []
        annotation_by_package = {
            annotation.package_path: annotation for annotation in custody.annotations
        }
        fingerprint_cache: dict[str, frozenset[int]] = {}
        for target in production:
            exact = by_digest.get(target.digest, [])
            candidates = exact or by_name.get(
                (PurePosixPath(target.path).name.casefold(), target.suffix), []
            )
            if not candidates:
                continue
            target_fingerprints = self._fingerprints(target, fingerprint_cache)
            for candidate in candidates[:12]:
                source_fingerprints = self._fingerprints(candidate, fingerprint_cache)
                if exact:
                    score = 1.0
                    shared = len(target_fingerprints)
                else:
                    shared_set = target_fingerprints & source_fingerprints
                    union = target_fingerprints | source_fingerprints
                    shared = len(shared_set)
                    score = len(shared_set) / len(union) if union else 0.0
                if score < self.similarity_threshold:
                    continue
                pair = SimilarityPair(
                    production_path=target.path,
                    source_pool_path=candidate.path,
                    score=score,
                    shared_fingerprints=shared,
                    production_fingerprints=len(target_fingerprints),
                    source_fingerprints=len(source_fingerprints),
                    exact=bool(exact),
                )
                pairs.append(pair)
                package = _package_root(target.path)
                annotation = annotation_by_package.get(package)
                severity = Severity.WARNING if annotation else Severity.BLOCKER
                findings.append(
                    finding(
                        "source_similarity_vendor_like",
                        "Formal production source is highly similar to source-pool/vendor content.",
                        "opaque",
                        severity=severity,
                        path=target.path,
                        disposition=(
                            Disposition.TRACK
                            if annotation
                            else Disposition.BLOCK_RELEASE
                        ),
                        remediation=(
                            "Retain cropped-control-flow provenance and Zyra custody evidence."
                            if annotation
                            else "Declare package custody or move unowned source out of formal modules."
                        ),
                        default_path_impact=(
                            "Similarity is reviewed provenance, not an automatic failure."
                            if annotation
                            else "Unannotated copy may be pseudo-internalization."
                        ),
                        evidence=(
                            Evidence(
                                kind=EvidenceKind.SOURCE,
                                path=candidate.path,
                                excerpt_digest=candidate.digest,
                            ),
                        ),
                        attributes=pair.to_dict(),
                    )
                )
        return (
            sorted(pairs, key=lambda item: (-item.score, item.production_path)),
            findings,
        )

    def _fingerprints(
        self,
        record: RepositoryFile,
        cache: dict[str, frozenset[int]],
    ) -> frozenset[int]:
        cached = cache.get(record.path)
        if cached is not None:
            return cached
        try:
            source = (self.project_root / record.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            cache[record.path] = frozenset()
            return frozenset()
        tokens = normalize_source_tokens(source, record.suffix)
        fingerprints = winnow(tokens, shingle_size=7, window_size=5)
        cache[record.path] = fingerprints
        return fingerprints

    def _opaque_runtime_custody(
        self,
        inventory: RepositoryInventory,
        dependencies: DependencyGraph,
        processes: ProcessCatalog | None,
        process_uses: Sequence[ProcessUse],
    ) -> list[Finding]:
        findings: list[Finding] = []
        external_profiles = (
            [profile for profile in processes.profiles if profile.external]
            if processes
            else []
        )
        for profile in external_profiles:
            source_bound = bool(profile.source_role_entry_ids)
            semantic_health = bool(profile.healthcheck)
            if profile.default_reachable and (not source_bound or not semantic_health):
                findings.append(
                    finding(
                        "opaque_default_process_owner",
                        "Default external process lacks source-role or semantic-health custody.",
                        "opaque",
                        severity=Severity.BLOCKER,
                        path=processes.source_path if processes else "",
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Internalize, externalize behind a full contract, or remove from default path.",
                        default_path_impact="Opaque process can own core release decisions.",
                        attributes={
                            "profile_id": profile.profile_id,
                            "source_bound": source_bound,
                            "semantic_health": semantic_health,
                        },
                    )
                )
        for use in process_uses:
            executable = PurePosixPath(use.executable.replace("\\", "/")).name.casefold()
            if executable.endswith(
                (".exe", ".dll", ".so", ".dylib", ".node", ".wasm")
            ) and not source_audited_interpreter(use.executable):
                findings.append(
                    finding(
                        "opaque_binary_process",
                        "Runtime starts an opaque binary/native process.",
                        "opaque",
                        severity=Severity.BLOCKER,
                        path=use.path,
                        line=use.line,
                        disposition=Disposition.EXTERNALIZE,
                        remediation="Bind source/build checksum/package owner and semantic health.",
                        default_path_impact="Core behavior may be unavailable for source audit.",
                        attributes={"executable": executable},
                    )
                )
        for item in inventory.files:
            if item.suffix not in OPAQUE_EXTENSIONS:
                continue
            if item.path.startswith(("apps/", "packages/")):
                findings.append(
                    finding(
                        "opaque_artifact_formal_package",
                        "Opaque binary/archive artifact is inside a formal package.",
                        "opaque",
                        severity=Severity.BLOCKER,
                        path=item.path,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Prove reproducible source build or remove/externalize the artifact.",
                        default_path_impact="Package behavior is not fully represented by source.",
                        evidence=(
                            Evidence(
                                kind=(
                                    EvidenceKind.ARCHIVE
                                    if item.kind == "archive"
                                    else EvidenceKind.BINARY
                                ),
                                path=item.path,
                                excerpt_digest=item.digest,
                            ),
                        ),
                    )
                )
        return findings


def normalize_source_tokens(source: str, suffix: str) -> tuple[str, ...]:
    if suffix in {".py", ".pyi"}:
        return _python_tokens(source)
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        return _javascript_tokens(source)
    return tuple(
        token.casefold()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\s]", source)
        if token
    )


def _python_tokens(source: str) -> tuple[str, ...]:
    result: list[str] = []
    try:
        stream = io.BytesIO(source.encode("utf-8")).readline
        for item in tokenize.tokenize(stream):
            if item.type in {
                tokenize.ENCODING,
                tokenize.ENDMARKER,
                tokenize.NEWLINE,
                tokenize.NL,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.COMMENT,
            }:
                continue
            if item.type == tokenize.STRING:
                result.append("<string>")
            elif item.type == tokenize.NUMBER:
                result.append("<number>")
            else:
                result.append(item.string.casefold())
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return tuple(
            token.casefold()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\s]", source)
        )
    return tuple(result)


def _javascript_tokens(source: str) -> tuple[str, ...]:
    result: list[str] = []
    for item in JavaScriptLexer(source).tokens():
        if item.kind == "error":
            continue
        if item.kind in {"string", "template"}:
            result.append("<string>")
        elif item.kind == "number":
            result.append("<number>")
        else:
            result.append(item.value.casefold())
    return tuple(result)


def winnow(
    tokens: Sequence[str],
    *,
    shingle_size: int,
    window_size: int,
) -> frozenset[int]:
    if len(tokens) < shingle_size:
        return frozenset()
    hashes = [
        _stable_token_hash(tokens[index : index + shingle_size])
        for index in range(len(tokens) - shingle_size + 1)
    ]
    if len(hashes) <= window_size:
        return frozenset(hashes)
    selected: set[int] = set()
    previous_index = -1
    for start in range(len(hashes) - window_size + 1):
        window = hashes[start : start + window_size]
        minimum = min(window)
        rightmost = max(
            index for index, value in enumerate(window) if value == minimum
        )
        absolute = start + rightmost
        if absolute != previous_index:
            selected.add(minimum)
            previous_index = absolute
    return frozenset(selected)


def _stable_token_hash(tokens: Sequence[str]) -> int:
    digest = content_digest("\x1f".join(tokens).encode()).split(":", 1)[1]
    return int(digest[:16], 16)


def _runtime_source_scope(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    if normalized.startswith(("tests/", "docs/", "scripts/")):
        return False
    if "/test/" in normalized or "/tests/" in normalized:
        return False
    if normalized.startswith(
        (
            "packages/evaluation/",
            "packages/skills/zyra_skills/source_audit.py",
            "packages/integrations/zyra_integrations/source_custody/",
            "packages/integrations/zyra_integrations/ledger_",
        )
    ):
        return False
    return normalized.startswith(("apps/", "packages/"))


def _package_root(path: str) -> str:
    parts = PurePosixPath(path).parts
    if len(parts) >= 2 and parts[0] == "apps":
        return "/".join(parts[:2])
    if len(parts) >= 3 and parts[0] == "packages":
        return "/".join(parts[:3])
    return ""


def source_specific_remediation(code: str) -> str:
    if "sdk" in code or "runtime" in code or "process" in code or "cli" in code:
        return "Remove black-box runtime custody or bind it behind an explicit Zyra contract."
    if "state" in code or "sqlite" in code or "home" in code:
        return "Move state into a declared Zyra profile/store with restore and clean-run policy."
    if "install" in code:
        return "Move acquisition to a locked checksum-bound build step."
    if "native" in code:
        return "Declare source/build/package/checksum and reproducible native health."
    return "Declare the bounded source-specific custody and owner."


def source_specific_impact(code: str) -> str:
    if "install" in code:
        return "Runtime can acquire mutable code outside the release lock."
    if "state" in code or "sqlite" in code or "home" in code:
        return "Ambient local state can invalidate clean-run and restore evidence."
    if "native" in code:
        return "Opaque native behavior can escape source and package audit."
    return "An upstream runtime/process may retain core decision custody."
