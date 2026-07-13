from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import ActionDefinition, ActionSource, digest_value
from .registry import BrowserActionRegistry


class SourceAuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SourceAuditKind(StrEnum):
    RUNTIME_DEPENDENCY = "runtime_dependency"
    DYNAMIC_IMPORT = "dynamic_import"
    SUBPROCESS_BOUNDARY = "subprocess_boundary"
    NETWORK_INSTALL = "network_install"
    SOURCE_ROLE = "source_role"
    OWNER_CONFLICT = "owner_conflict"
    PROVENANCE_GAP = "provenance_gap"
    SCHEMA_GAP = "schema_gap"


@dataclass(frozen=True, slots=True)
class SourceAuditFinding:
    kind: SourceAuditKind
    severity: SourceAuditSeverity
    code: str
    message: str
    path: str = ""
    line: int = 0
    action: str = ""
    source_repository: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", dict(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "action": self.action,
            "source_repository": self.source_repository,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class SourceAuditReport:
    registry_digest: str
    package_root: str
    production_files: tuple[str, ...]
    findings: tuple[SourceAuditFinding, ...]
    source_roles: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "production_files", tuple(self.production_files))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "source_roles", {key: dict(value) for key, value in self.source_roles.items()})

    @property
    def ok(self) -> bool:
        return not any(item.severity == SourceAuditSeverity.ERROR for item in self.findings)

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_digest": self.registry_digest,
            "package_root": self.package_root,
            "production_files": list(self.production_files),
            "findings": [item.to_dict() for item in self.findings],
            "source_roles": {key: dict(value) for key, value in self.source_roles.items()},
            "ok": self.ok,
        }

    def require(self) -> "SourceAuditReport":
        if not self.ok:
            codes = ", ".join(item.code for item in self.findings if item.severity == SourceAuditSeverity.ERROR)
            raise RuntimeError(f"browser action source audit failed: {codes}")
        return self


class BrowserActionSourceAuditor:
    """Productized clean-path and source-role audit for the 04C package."""

    ROOT_SOURCE_NAMES = frozenset(
        {
            "browser-use",
            "claude-code-best",
            "oh-my-pi",
            "openhands",
            "opencode",
            "agentscope",
            "openclaw",
        }
    )
    FORBIDDEN_IMPORT_ROOTS = frozenset({"browser_use", "openhands", "opencode", "agentscope", "openclaw"})
    DYNAMIC_IMPORT_CALLS = frozenset({"importlib.import_module", "__import__", "pkgutil.resolve_name"})
    PROCESS_CALLS = frozenset(
        {
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.call",
            "os.system",
            "os.popen",
        }
    )

    def __init__(self, registry: BrowserActionRegistry, package_root: str | Path) -> None:
        self.registry = registry
        self.package_root = Path(package_root).resolve()

    def audit(self) -> SourceAuditReport:
        findings: list[SourceAuditFinding] = []
        files = tuple(sorted(self.package_root.rglob("*.py")))
        for path in files:
            findings.extend(self._audit_file(path))
        roles, role_findings = self._audit_roles()
        findings.extend(role_findings)
        findings.extend(self._audit_registry_contract())
        return SourceAuditReport(
            registry_digest=self.registry.digest,
            package_root=str(self.package_root),
            production_files=tuple(str(path.relative_to(self.package_root)) for path in files),
            findings=tuple(findings),
            source_roles=roles,
        )

    def _audit_file(self, path: Path) -> list[SourceAuditFinding]:
        findings: list[SourceAuditFinding] = []
        relative = str(path.relative_to(self.package_root)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            return [
                SourceAuditFinding(
                    SourceAuditKind.RUNTIME_DEPENDENCY,
                    SourceAuditSeverity.ERROR,
                    "python_syntax_error",
                    str(exc),
                    relative,
                    exc.lineno or 0,
                )
            ]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in self.FORBIDDEN_IMPORT_ROOTS:
                        findings.append(self._finding_import(relative, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                root = str(node.module or "").split(".", 1)[0]
                if root in self.FORBIDDEN_IMPORT_ROOTS:
                    findings.append(self._finding_import(relative, node.lineno, str(node.module)))
            elif isinstance(node, ast.Call):
                name = dotted_name(node.func)
                if name in self.DYNAMIC_IMPORT_CALLS:
                    findings.append(
                        SourceAuditFinding(
                            SourceAuditKind.DYNAMIC_IMPORT,
                            SourceAuditSeverity.ERROR,
                            "dynamic_import_in_main_path",
                            f"dynamic import call {name!r} is prohibited in browser action runtime",
                            relative,
                            node.lineno,
                        )
                    )
                if name in self.PROCESS_CALLS:
                    findings.append(
                        SourceAuditFinding(
                            SourceAuditKind.SUBPROCESS_BOUNDARY,
                            SourceAuditSeverity.ERROR,
                            "subprocess_in_main_path",
                            f"subprocess call {name!r} is prohibited in browser action runtime",
                            relative,
                            node.lineno,
                        )
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.replace("\\", "/").casefold()
                if any(f"../{name}" in value or f"g:/agent-zoo/{name}" in value for name in self.ROOT_SOURCE_NAMES):
                    findings.append(
                        SourceAuditFinding(
                            SourceAuditKind.RUNTIME_DEPENDENCY,
                            SourceAuditSeverity.ERROR,
                            "root_source_path_literal",
                            "runtime package contains a path to a root source repository",
                            relative,
                            getattr(node, "lineno", 0),
                        )
                    )
        return findings

    def _audit_roles(self) -> tuple[dict[str, dict[str, Any]], list[SourceAuditFinding]]:
        roles: dict[str, dict[str, Any]] = {}
        findings: list[SourceAuditFinding] = []
        for name in self.registry.names():
            definition = self.registry.require(name)
            primary = [source for source in definition.sources if source.role == "primary_implementation"]
            supplementary = [source for source in definition.sources if source.role == "supplementary_implementation"]
            primary_repositories = {source.repository for source in primary}
            supplementary_repositories = {source.repository for source in supplementary}
            roles[name] = {
                "primary": [source.to_dict() for source in primary],
                "supplementary": [source.to_dict() for source in supplementary],
                "all_sources": [source.to_dict() for source in definition.sources],
            }
            if len(primary_repositories) != 1:
                findings.append(
                    SourceAuditFinding(
                        SourceAuditKind.SOURCE_ROLE,
                        SourceAuditSeverity.ERROR,
                        "primary_source_count",
                        "browser action must have exactly one primary implementation source",
                        action=name,
                        details={"repositories": sorted(primary_repositories)},
                    )
                )
            if len(supplementary_repositories) > 2:
                findings.append(
                    SourceAuditFinding(
                        SourceAuditKind.SOURCE_ROLE,
                        SourceAuditSeverity.ERROR,
                        "supplementary_source_limit",
                        "browser action has more than two supplementary implementation sources",
                        action=name,
                        details={"repositories": sorted(supplementary_repositories)},
                    )
                )
        return roles, findings

    def _audit_registry_contract(self) -> list[SourceAuditFinding]:
        findings: list[SourceAuditFinding] = []
        snapshot = self.registry.snapshot()
        if len(snapshot.actions) != len(set(snapshot.actions)):
            findings.append(
                SourceAuditFinding(
                    SourceAuditKind.SCHEMA_GAP,
                    SourceAuditSeverity.ERROR,
                    "duplicate_action",
                    "browser action registry contains duplicate names",
                )
            )
        for alias, target in snapshot.aliases.items():
            if target not in snapshot.actions or alias in snapshot.actions:
                findings.append(
                    SourceAuditFinding(
                        SourceAuditKind.SCHEMA_GAP,
                        SourceAuditSeverity.ERROR,
                        "alias_collision",
                        "browser action alias is invalid or shadows a canonical action",
                        action=alias,
                        details={"target": target},
                    )
                )
        for name in snapshot.actions:
            definition = self.registry.require(name)
            if not definition.description or not definition.arguments and definition.selector_required:
                findings.append(
                    SourceAuditFinding(
                        SourceAuditKind.SCHEMA_GAP,
                        SourceAuditSeverity.ERROR,
                        "incomplete_action_contract",
                        "browser action definition lacks description or selector contract",
                        action=name,
                    )
                )
        return findings

    @staticmethod
    def _finding_import(path: str, line: int, imported: str) -> SourceAuditFinding:
        return SourceAuditFinding(
            SourceAuditKind.RUNTIME_DEPENDENCY,
            SourceAuditSeverity.ERROR,
            "upstream_runtime_import",
            f"production browser action package imports upstream runtime {imported!r}",
            path,
            line,
        )


def dotted_name(value: ast.expr) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        parent = dotted_name(value.value)
        return f"{parent}.{value.attr}" if parent else value.attr
    return ""
