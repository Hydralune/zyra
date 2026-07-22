from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    EvidencePointer,
    Finding,
    GateResult,
    GateStatus,
    Maturity,
    Severity,
    SourceCoverageItem,
    SourceRole,
)


_ROLE_ALIASES = {
    "primary": SourceRole.PRIMARY,
    "primary_implementation": SourceRole.PRIMARY,
    "primary implementation": SourceRole.PRIMARY,
    "supplementary": SourceRole.SUPPLEMENTARY,
    "supplementary_implementation": SourceRole.SUPPLEMENTARY,
    "supplementary implementation": SourceRole.SUPPLEMENTARY,
    "conformance": SourceRole.CONFORMANCE,
    "conformance_only": SourceRole.CONFORMANCE,
    "conformance only": SourceRole.CONFORMANCE,
    "reference": SourceRole.REFERENCE,
    "reference_only": SourceRole.REFERENCE,
    "reference only": SourceRole.REFERENCE,
    "experimental": SourceRole.EXPERIMENTAL,
    "deferred": SourceRole.DEFERRED,
    "rejected": SourceRole.REJECTED,
}

_MATURITY_ALIASES = {
    "active": Maturity.ACTIVE_REAL,
    "active_real": Maturity.ACTIVE_REAL,
    "active real": Maturity.ACTIVE_REAL,
    "conformance": Maturity.CONFORMANCE_VERIFIED,
    "conformance_verified": Maturity.CONFORMANCE_VERIFIED,
    "conformance verified": Maturity.CONFORMANCE_VERIFIED,
    "source_inactive": Maturity.SOURCE_INACTIVE,
    "source inactive": Maturity.SOURCE_INACTIVE,
    "inactive": Maturity.SOURCE_INACTIVE,
    "experimental": Maturity.EXPERIMENTAL,
    "deferred": Maturity.DEFERRED,
    "debt": Maturity.DEBT,
    "blocked": Maturity.BLOCKED,
}

_CODE_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go"}
_ENTRY_PATTERNS = (
    re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
    re.compile(r"\bexport\s+(?:default\s+)?(?:class|function|const)\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
)


@dataclass(frozen=True, slots=True)
class MarkdownTable:
    source: Path
    heading: str
    headers: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]


class SourceGraphTableReader:
    """Read role/maturity decisions from source-graph Markdown without treating it as runtime data."""

    def read(self, path: Path) -> tuple[MarkdownTable, ...]:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        heading = ""
        tables: list[MarkdownTable] = []
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if line.startswith("#"):
                heading = line.lstrip("#").strip()
                index += 1
                continue
            if not self._looks_like_header(lines, index):
                index += 1
                continue
            headers = self._cells(lines[index])
            index += 2
            rows: list[Mapping[str, str]] = []
            while index < len(lines) and self._is_table_row(lines[index]):
                cells = self._cells(lines[index])
                padded = [*cells, *("" for _ in range(max(0, len(headers) - len(cells))))]
                rows.append({headers[pos]: padded[pos] for pos in range(len(headers))})
                index += 1
            tables.append(
                MarkdownTable(
                    source=path,
                    heading=heading,
                    headers=tuple(headers),
                    rows=tuple(rows),
                )
            )
        return tuple(tables)

    def decisions(self, path: Path) -> tuple[Mapping[str, str], ...]:
        decisions: list[Mapping[str, str]] = []
        for table in self.read(path):
            role_header = self._header(table.headers, "role", "角色", "前向角色", "source role")
            if not role_header:
                continue
            for row in table.rows:
                role = parse_source_role(row.get(role_header, ""))
                if role is None:
                    continue
                decisions.append(
                    {
                        **dict(row),
                        "_role": role.value,
                        "_heading": table.heading,
                        "_source": str(path),
                    }
                )
        return tuple(decisions)

    @staticmethod
    def _header(headers: Sequence[str], *needles: str) -> str:
        normalized = {re.sub(r"\s+", " ", item.strip().lower()): item for item in headers}
        for needle in needles:
            candidate = re.sub(r"\s+", " ", needle.strip().lower())
            if candidate in normalized:
                return normalized[candidate]
        for candidate, original in normalized.items():
            if any(needle.lower() in candidate for needle in needles):
                return original
        return ""

    @staticmethod
    def _looks_like_header(lines: Sequence[str], index: int) -> bool:
        if index + 1 >= len(lines):
            return False
        return SourceGraphTableReader._is_table_row(lines[index]) and bool(
            re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1])
        )

    @staticmethod
    def _is_table_row(line: str) -> bool:
        stripped = line.strip()
        return "|" in stripped and bool(stripped)

    @staticmethod
    def _cells(line: str) -> list[str]:
        stripped = line.strip().strip("|")
        cells: list[str] = []
        current: list[str] = []
        escaped = False
        code = False
        for char in stripped:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\":
                current.append(char)
                escaped = True
            elif char == "`":
                current.append(char)
                code = not code
            elif char == "|" and not code:
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        cells.append("".join(current).strip())
        return cells


class ProductionEntryInspector:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()
        self._symbol_cache: dict[Path, set[str]] = {}

    def inspect(self, item: SourceCoverageItem) -> tuple[list[EvidencePointer], list[Finding]]:
        evidence: list[EvidencePointer] = []
        findings: list[Finding] = []
        target_files = self._target_files(item.target_paths)
        if not target_files:
            findings.append(
                Finding(
                    code="coverage.target_missing",
                    severity=Severity.BLOCKER if item.role.production_bearing else Severity.WARNING,
                    summary="No Zyra target source exists for the coverage item.",
                    capability=item.capability_id,
                    location=", ".join(item.target_paths),
                    remediation="Restore the adjudicated target owner or lower maturity with an explicit decision.",
                )
            )
            return evidence, findings

        for path in target_files:
            evidence.append(
                EvidencePointer(
                    kind="target_source",
                    location=str(path.relative_to(self.root)),
                    summary=f"Zyra target source for {item.capability_id}",
                    metadata={"suffix": path.suffix, "bytes": path.stat().st_size},
                )
            )

        missing_entries: list[str] = []
        for entry in item.production_entries:
            path_part, symbol = self._split_entry(entry)
            candidates = target_files
            if path_part:
                normalized = path_part.replace("\\", "/")
                candidates = [
                    path
                    for path in candidates
                    if path.relative_to(self.root).as_posix() == normalized
                    or path.relative_to(self.root).as_posix().endswith(normalized)
                ]
            if not candidates or not any(self._has_symbol(path, symbol) for path in candidates):
                missing_entries.append(entry)
            else:
                evidence.append(
                    EvidencePointer(
                        kind="production_entry",
                        location=entry,
                        summary="Production entry exists in Zyra-owned source.",
                    )
                )
        if missing_entries:
            findings.append(
                Finding(
                    code="coverage.production_entry_missing",
                    severity=Severity.BLOCKER if item.maturity is Maturity.ACTIVE_REAL else Severity.ERROR,
                    summary="Declared production entries were not found.",
                    detail=", ".join(missing_entries),
                    capability=item.capability_id,
                    remediation="Correct stale evidence or connect the owner through a real production entry.",
                )
            )
        return evidence, findings

    def _target_files(self, target_paths: Sequence[str]) -> list[Path]:
        files: list[Path] = []
        seen: set[Path] = set()
        for raw in target_paths:
            target = (self.root / raw).resolve()
            if not target.is_relative_to(self.root):
                continue
            candidates = [target] if target.is_file() else (
                list(target.rglob("*")) if target.is_dir() else []
            )
            for candidate in candidates:
                if candidate.is_file() and candidate.suffix.lower() in _CODE_SUFFIXES and candidate not in seen:
                    seen.add(candidate)
                    files.append(candidate)
        return sorted(files)

    @staticmethod
    def _split_entry(entry: str) -> tuple[str, str]:
        if "::" in entry:
            path, symbol = entry.rsplit("::", 1)
            return path.strip(), symbol.strip()
        return "", entry.strip()

    def _has_symbol(self, path: Path, symbol: str) -> bool:
        if not symbol:
            return True
        symbols = self._symbol_cache.get(path)
        if symbols is None:
            symbols = self._symbols(path)
            self._symbol_cache[path] = symbols
        leaf = symbol.rsplit(".", 1)[-1]
        return symbol in symbols or leaf in symbols

    def _symbols(self, path: Path) -> set[str]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return set()
        if path.suffix in {".py", ".pyi"}:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                return set()
            found: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(node.name)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for parent in ast.walk(node):
                        if isinstance(parent, ast.Name):
                            found.add(parent.id)
            return found
        found = set()
        for pattern in _ENTRY_PATTERNS:
            found.update(pattern.findall(text))
        return found


class SourceToTargetCoverageReport:
    def __init__(
        self,
        project_root: str | Path,
        *,
        source_workspace: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.source_workspace = Path(source_workspace).resolve() if source_workspace else self.project_root.parent
        self.entry_inspector = ProductionEntryInspector(self.project_root)
        self.table_reader = SourceGraphTableReader()

    def evaluate(
        self,
        items: Sequence[SourceCoverageItem],
        *,
        required_capabilities: Iterable[str] = (),
        known_scenarios: Iterable[str] = (),
        known_disable_probes: Iterable[str] = (),
    ) -> GateResult:
        result = GateResult(
            gate_id="source-to-target-coverage",
            status=GateStatus.NOT_RUN,
            summary="Role-aware M1 source-to-target completion and maturity audit.",
        )
        scenarios = set(known_scenarios)
        probes = set(known_disable_probes)
        required = set(required_capabilities)
        by_capability: dict[str, list[SourceCoverageItem]] = defaultdict(list)
        for item in items:
            by_capability[item.capability_id].append(item)
            result.findings.extend(self._validate_item(item, scenarios=scenarios, probes=probes))
            evidence, findings = self.entry_inspector.inspect(item)
            result.evidence.extend(evidence)
            result.findings.extend(findings)

        result.findings.extend(self._validate_required(required, by_capability))
        result.findings.extend(self._validate_role_limits(by_capability))
        result.findings.extend(self._validate_unique_owners(by_capability))
        result.findings.extend(self._validate_source_graph_alignment(items))
        result.findings.extend(self._validate_omp_decisions(items))

        role_counts = Counter(item.role.value for item in items)
        maturity_counts = Counter(item.maturity.value for item in items)
        production = [item for item in items if item.role.production_bearing]
        active = [item for item in production if item.maturity is Maturity.ACTIVE_REAL]
        score = len(active) / len(production) if production else 0.0
        result.metrics.update(
            {
                "item_count": len(items),
                "capability_count": len(by_capability),
                "role_counts": dict(sorted(role_counts.items())),
                "maturity_counts": dict(sorted(maturity_counts.items())),
                "production_bearing_count": len(production),
                "active_real_count": len(active),
                "completion_score": round(score, 6),
                "items": [item.to_dict() for item in items],
            }
        )
        if production and not active:
            result.add(
                Finding(
                    code="coverage.no_active_production",
                    severity=Severity.BLOCKER,
                    summary="No primary or supplementary coverage item is active_real.",
                )
            )
        return result.finish()

    def discover_source_graph_decisions(self) -> tuple[Mapping[str, str], ...]:
        candidates = [
            self.source_workspace / "source-graphs" / "claude-code-best" / "source-graph.md",
            self.source_workspace / "source-graphs" / "langgraph" / "source-graph.md",
            self.source_workspace / "source-graphs" / "oh-my-pi" / "source-to-target.md",
            self.source_workspace / "docs" / "milestones" / "source-graph-realignment-2026-07-08.md",
        ]
        decisions: list[Mapping[str, str]] = []
        for path in candidates:
            if path.is_file():
                decisions.extend(self.table_reader.decisions(path))
        return tuple(decisions)

    def _validate_item(
        self,
        item: SourceCoverageItem,
        *,
        scenarios: set[str],
        probes: set[str],
    ) -> list[Finding]:
        findings: list[Finding] = []
        if not item.source_language or item.source_language.lower() in {"mixed", "unknown"}:
            findings.append(
                Finding(
                    code="coverage.source_language_invalid",
                    severity=Severity.BLOCKER,
                    summary="Source language must be explicit per adjudicated mechanism.",
                    capability=item.capability_id,
                    detail=item.source_language,
                )
            )
        if not item.target_language or item.target_language.lower() in {"mixed", "unknown"}:
            findings.append(
                Finding(
                    code="coverage.target_language_invalid",
                    severity=Severity.ERROR,
                    summary="Target owner language must be explicit.",
                    capability=item.capability_id,
                    detail=item.target_language,
                )
            )
        if item.role.production_bearing:
            findings.extend(self._validate_production_item(item, scenarios=scenarios, probes=probes))
        else:
            findings.extend(self._validate_inactive_item(item))
        if item.maturity is Maturity.ACTIVE_REAL and not item.role.production_bearing:
            findings.append(
                Finding(
                    code="coverage.inactive_role_promoted",
                    severity=Severity.BLOCKER,
                    summary="An inactive source role cannot be promoted to active_real.",
                    capability=item.capability_id,
                    detail=f"role={item.role.value}",
                )
            )
        return findings

    def _validate_production_item(
        self,
        item: SourceCoverageItem,
        *,
        scenarios: set[str],
        probes: set[str],
    ) -> list[Finding]:
        findings: list[Finding] = []
        required_fields = {
            "target_paths": item.target_paths,
            "canonical_owner": (item.canonical_owner,),
            "production_entries": item.production_entries,
            "state_owners": item.state_owners,
            "scenario_ids": item.scenario_ids,
            "disable_probe_ids": item.disable_probe_ids,
            "cleanroom_decision": (item.cleanroom_decision,),
        }
        for name, values in required_fields.items():
            if not any(str(value).strip() for value in values):
                findings.append(
                    Finding(
                        code=f"coverage.production_{name}_missing",
                        severity=Severity.BLOCKER if item.maturity is Maturity.ACTIVE_REAL else Severity.ERROR,
                        summary=f"Production-bearing source item has no {name.replace('_', ' ')} evidence.",
                        capability=item.capability_id,
                    )
                )
        missing_scenarios = sorted(set(item.scenario_ids) - scenarios)
        if missing_scenarios:
            findings.append(
                Finding(
                    code="coverage.scenario_unregistered",
                    severity=Severity.ERROR,
                    summary="Coverage item references unknown scenario evidence.",
                    detail=", ".join(missing_scenarios),
                    capability=item.capability_id,
                )
            )
        missing_probes = sorted(set(item.disable_probe_ids) - probes)
        if missing_probes:
            findings.append(
                Finding(
                    code="coverage.disable_probe_unregistered",
                    severity=Severity.ERROR,
                    summary="Coverage item references unknown disable probes.",
                    detail=", ".join(missing_probes),
                    capability=item.capability_id,
                )
            )
        if item.maturity is Maturity.ACTIVE_REAL and item.limitations:
            severe = [
                text
                for text in item.limitations
                if any(word in text.lower() for word in ("simulated", "loopback only", "not attached", "mock", "source dependency"))
            ]
            if severe:
                findings.append(
                    Finding(
                        code="coverage.active_real_contradicted",
                        severity=Severity.BLOCKER,
                        summary="active_real maturity conflicts with declared limitations.",
                        detail="; ".join(severe),
                        capability=item.capability_id,
                    )
                )
        return findings

    @staticmethod
    def _validate_inactive_item(item: SourceCoverageItem) -> list[Finding]:
        findings: list[Finding] = []
        if not item.limitations and not item.next_owner:
            findings.append(
                Finding(
                    code="coverage.inactive_reason_missing",
                    severity=Severity.WARNING,
                    summary="Inactive source item has no decision reason or next owner.",
                    capability=item.capability_id,
                )
            )
        if item.role is SourceRole.EXPERIMENTAL and item.maturity not in {
            Maturity.EXPERIMENTAL,
            Maturity.SOURCE_INACTIVE,
            Maturity.DEFERRED,
        }:
            findings.append(
                Finding(
                    code="coverage.experimental_maturity_invalid",
                    severity=Severity.ERROR,
                    summary="Experimental sources must remain inactive or explicitly experimental.",
                    capability=item.capability_id,
                )
            )
        if item.role in {SourceRole.DEFERRED, SourceRole.REJECTED} and item.production_entries:
            findings.append(
                Finding(
                    code="coverage.inactive_source_claims_entry",
                    severity=Severity.ERROR,
                    summary="Deferred/rejected source claims a production entry.",
                    capability=item.capability_id,
                )
            )
        return findings

    @staticmethod
    def _validate_required(
        required: set[str],
        by_capability: Mapping[str, Sequence[SourceCoverageItem]],
    ) -> list[Finding]:
        return [
            Finding(
                code="coverage.required_capability_missing",
                severity=Severity.BLOCKER,
                summary="Required M1 capability is absent from source coverage.",
                capability=capability,
            )
            for capability in sorted(required - set(by_capability))
        ]

    @staticmethod
    def _validate_role_limits(
        by_capability: Mapping[str, Sequence[SourceCoverageItem]],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for capability, items in by_capability.items():
            primary = [item for item in items if item.role is SourceRole.PRIMARY]
            supplementary = [item for item in items if item.role is SourceRole.SUPPLEMENTARY]
            if len(primary) > 1:
                findings.append(
                    Finding(
                        code="coverage.multiple_primary_sources",
                        severity=Severity.BLOCKER,
                        summary="A capability has more than one primary implementation source.",
                        capability=capability,
                        detail=", ".join(item.source_repository for item in primary),
                    )
                )
            if len(supplementary) > 2:
                findings.append(
                    Finding(
                        code="coverage.supplementary_limit_exceeded",
                        severity=Severity.BLOCKER,
                        summary="A capability has more than two supplementary implementation sources.",
                        capability=capability,
                        detail=", ".join(item.source_repository for item in supplementary),
                    )
                )
        return findings

    @staticmethod
    def _validate_unique_owners(
        by_capability: Mapping[str, Sequence[SourceCoverageItem]],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for capability, items in by_capability.items():
            active = [item for item in items if item.role.production_bearing and item.maturity is Maturity.ACTIVE_REAL]
            owners = {item.canonical_owner for item in active if item.canonical_owner}
            if len(owners) > 1:
                findings.append(
                    Finding(
                        code="coverage.duplicate_canonical_owner",
                        severity=Severity.BLOCKER,
                        summary="Active source items disagree on the canonical owner.",
                        capability=capability,
                        detail=", ".join(sorted(owners)),
                    )
                )
        return findings

    def _validate_source_graph_alignment(self, items: Sequence[SourceCoverageItem]) -> list[Finding]:
        decisions = self.discover_source_graph_decisions()
        if not decisions:
            return [
                Finding(
                    code="coverage.source_graph_decisions_unavailable",
                    severity=Severity.WARNING,
                    summary="No role-aware source-graph tables were discovered in the workspace.",
                )
            ]
        findings: list[Finding] = []
        for item in items:
            repository = item.source_repository.lower()
            path_relevant = [
                row
                for row in decisions
                if any(
                    self._source_path_matches_decision(path, str(row))
                    for path in item.source_paths
                )
            ]
            repository_relevant = [row for row in decisions if repository in str(row).lower()]
            relevant = path_relevant or repository_relevant
            if not relevant:
                findings.append(
                    Finding(
                        code="coverage.source_graph_row_unmatched",
                        severity=Severity.WARNING,
                        summary="Coverage item could not be matched to a role-aware source-graph row.",
                        capability=item.capability_id,
                        detail=item.source_repository,
                    )
                )
                continue
            roles = {row.get("_role", "") for row in path_relevant}
            if path_relevant and roles and item.role.value not in roles:
                findings.append(
                    Finding(
                        code="coverage.source_graph_role_conflict",
                        severity=Severity.ERROR,
                        summary="Coverage role conflicts with the discovered source-graph decision.",
                        capability=item.capability_id,
                        detail=f"declared={item.role.value}; discovered={','.join(sorted(roles))}",
                    )
                )
        return findings

    @staticmethod
    def _source_path_matches_decision(source_path: str, decision_text: str) -> bool:
        normalized_path = re.sub(r"[`\\]+", "/", source_path.lower()).strip("/")
        normalized_row = re.sub(r"[`\\]+", "/", decision_text.lower())
        if len(normalized_path) >= 8 and normalized_path in normalized_row:
            return True
        return False

    def _validate_omp_decisions(self, items: Sequence[SourceCoverageItem]) -> list[Finding]:
        omp_graph = self.source_workspace / "source-graphs" / "oh-my-pi" / "source-to-target.md"
        if not omp_graph.is_file():
            return [
                Finding(
                    code="coverage.omp_graph_missing",
                    severity=Severity.WARNING,
                    summary="Oh My Pi source-to-target graph is not available for the foundation audit.",
                    location=str(omp_graph),
                )
            ]
        decisions = self.table_reader.decisions(omp_graph)
        declared = [item for item in items if item.source_repository.lower() in {"oh-my-pi", "oh my pi", "omp"}]
        if decisions and not declared:
            return [
                Finding(
                    code="coverage.omp_items_unregistered",
                    severity=Severity.BLOCKER,
                    summary="OMP role decisions exist but no OMP coverage items were registered.",
                )
            ]
        return []


def parse_source_role(value: Any) -> SourceRole | None:
    normalized = _normalize_decision(value)
    if normalized in _ROLE_ALIASES:
        return _ROLE_ALIASES[normalized]
    for key, role in _ROLE_ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", normalized):
            return role
    return None


def parse_maturity(value: Any) -> Maturity | None:
    normalized = _normalize_decision(value)
    if normalized in _MATURITY_ALIASES:
        return _MATURITY_ALIASES[normalized]
    for key, maturity in _MATURITY_ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", normalized):
            return maturity
    return None


def _normalize_decision(value: Any) -> str:
    text = str(value or "").strip().lower().replace("`", "")
    text = re.sub(r"[*_]", "_", text)
    text = re.sub(r"\s+", " ", text)
    return text
