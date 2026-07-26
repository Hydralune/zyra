from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .contracts import (
    M3WorkItem,
    ProductizationContractError,
    ResolutionStatus,
    canonicalize,
    digest_payload,
    normalize_repo_path,
)


PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
SCRIPT_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
SOURCE_SUFFIXES = PYTHON_SUFFIXES | SCRIPT_SUFFIXES
ARCHIVE_SUFFIXES = frozenset({".zip", ".tar", ".tgz", ".7z", ".whl"})
NATIVE_SUFFIXES = frozenset({".exe", ".dll", ".so", ".dylib", ".node", ".wasm", ".bin"})

INSTALLER_EXECUTABLES = frozenset({"npm", "npx", "pnpm", "yarn", "bun", "pip", "pip3", "uv"})
INSTALL_ACTIONS = frozenset(
    {
        "install",
        "add",
        "update",
        "upgrade",
        "remove",
        "uninstall",
        "link",
        "exec",
        "dlx",
        "x",
    }
)
COMMON_INTERPRETERS = frozenset(
    {
        "python",
        "python3",
        "python.exe",
        "node",
        "node.exe",
        "bun",
        "bun.exe",
        "pwsh",
        "pwsh.exe",
        "powershell",
        "powershell.exe",
    }
)
PROCESS_CALL_NAMES = frozenset(
    {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
    }
)
DYNAMIC_IMPORT_NAMES = frozenset(
    {
        "__import__",
        "importlib.import_module",
        "importlib.util.find_spec",
        "importlib.util.module_from_spec",
    }
)
SCRIPT_PROCESS_CALL = re.compile(
    r"\b(?:Bun\.spawn|Bun\.spawnSync|Deno\.Command|"
    r"child_process\.(?:spawn|spawnSync|exec|execFile|fork)|"
    r"(?:spawn|spawnSync|exec|execFile|fork))\s*\(",
)
SCRIPT_DYNAMIC_IMPORT = re.compile(r"\bimport\s*\((?P<argument>[^)]*)\)")
PARENT_SOURCE_PATTERN = re.compile(
    r"(?i)(?:^|[\\/])(?:\.\.[\\/])+(?P<repo>"
    r"claude-code-best|browser-use|OpenHands|opencode|oh-my-pi|"
    r"hermes-agent|langgraph|agentscope|agent-framework|openclaw"
    r")(?:[\\/]|$)"
)
OPENCLAW_RUNTIME_PATTERN = re.compile(
    r"(?i)(?:from|import|require\s*\(|import\s*\(|spawn|exec|Popen|"
    r"workspace:|file:|link:|path:).{0,160}\bopenclaw\b"
)
LANGGRAPH_IMPORT_PATTERN = re.compile(
    r"(?i)(?:from\s+langgraph|import\s+langgraph|"
    r"require\s*\(\s*[\"']langgraph|import\s*\(\s*[\"']langgraph)"
)
LANGGRAPH_BROAD_SYMBOLS = frozenset(
    {
        "StateGraph",
        "MessageGraph",
        "Pregel",
        "PregelRunner",
        "BinaryOperatorAggregate",
        "LastValue",
        "ToolNode",
        "ToolRuntime",
        "create_react_agent",
        "InMemoryStore",
        "RemoteGraph",
        "StreamController",
    }
)
PROVENANCE_TERMS = frozenset(
    {
        "source_repo",
        "source_repository",
        "source_commit",
        "migration_mode",
        "same-language",
        "cropped",
        "productized",
        "zyra-owned",
        "conformance",
        "reference_only",
        "provenance",
    }
)


class PathScope(StrEnum):
    PRODUCTION = "production"
    TEST = "test"
    EVIDENCE = "evidence"
    DOCUMENTATION = "documentation"
    AUDIT_TOOL = "audit_tool"
    BUILD_TOOL = "build_tool"
    VENDOR = "vendor"
    GENERATED = "generated"
    UNKNOWN = "unknown"

    @property
    def runtime_capable(self) -> bool:
        return self in {
            PathScope.PRODUCTION,
            PathScope.BUILD_TOOL,
        }

    @property
    def non_runtime(self) -> bool:
        return self in {
            PathScope.TEST,
            PathScope.EVIDENCE,
            PathScope.DOCUMENTATION,
            PathScope.AUDIT_TOOL,
            PathScope.VENDOR,
            PathScope.GENERATED,
        }


class BoundaryDecision(StrEnum):
    SAFE_RUNTIME = "safe_runtime"
    NON_RUNTIME = "non_runtime"
    EXTERNALIZED_EVIDENCE = "externalized_evidence"
    DECLARED_TOOLCHAIN = "declared_toolchain"
    PROVENANCE_ONLY = "provenance_only"
    REPRODUCIBLE_SOURCE = "reproducible_source"
    RETIRED = "retired"
    BLOCKED_DYNAMIC_IMPORT = "blocked_dynamic_import"
    BLOCKED_DYNAMIC_INSTALL = "blocked_dynamic_install"
    BLOCKED_PARENT_SOURCE = "blocked_parent_source"
    BLOCKED_EXTERNAL_PROCESS = "blocked_external_process"
    BLOCKED_OPAQUE_BINARY = "blocked_opaque_binary"
    BLOCKED_LANGGRAPH_RUNTIME = "blocked_langgraph_runtime"
    BLOCKED_OPENCLAW_RUNTIME = "blocked_openclaw_runtime"
    MISSING_PATH = "missing_path"
    UNSUPPORTED = "unsupported"

    @property
    def resolved(self) -> bool:
        return self in {
            BoundaryDecision.SAFE_RUNTIME,
            BoundaryDecision.NON_RUNTIME,
            BoundaryDecision.EXTERNALIZED_EVIDENCE,
            BoundaryDecision.DECLARED_TOOLCHAIN,
            BoundaryDecision.PROVENANCE_ONLY,
            BoundaryDecision.REPRODUCIBLE_SOURCE,
            BoundaryDecision.RETIRED,
        }


@dataclass(frozen=True, slots=True)
class ProcessInvocation:
    path: str
    line: int
    executable: str
    arguments: tuple[str, ...]
    shell: bool
    literal: bool
    source_kind: str

    @property
    def executable_name(self) -> str:
        normalized = self.executable.strip("\"'").replace("\\", "/")
        return PurePosixPath(normalized).name.casefold()

    @property
    def command_text(self) -> str:
        return " ".join((self.executable, *self.arguments)).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "executable": self.executable,
            "executable_name": self.executable_name,
            "arguments": list(self.arguments),
            "shell": self.shell,
            "literal": self.literal,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class DynamicImportUse:
    path: str
    line: int
    module: str
    literal: bool
    registry_bound: bool
    source_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "module": self.module,
            "literal": self.literal,
            "registry_bound": self.registry_bound,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class ParentSourceUse:
    path: str
    line: int
    source_repository: str
    literal_digest: str
    effect: str
    production_effect: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "source_repository": self.source_repository,
            "literal_digest": self.literal_digest,
            "effect": self.effect,
            "production_effect": self.production_effect,
        }


@dataclass(frozen=True, slots=True)
class PathBoundaryEvidence:
    path: str
    scope: PathScope
    decision: BoundaryDecision
    reason: str
    content_digest: str
    process_invocations: tuple[ProcessInvocation, ...] = ()
    dynamic_imports: tuple[DynamicImportUse, ...] = ()
    parent_source_uses: tuple[ParentSourceUse, ...] = ()
    findings: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.decision.resolved and not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "scope": self.scope.value,
            "decision": self.decision.value,
            "reason": self.reason,
            "content_digest": self.content_digest,
            "process_invocations": [item.to_dict() for item in self.process_invocations],
            "dynamic_imports": [item.to_dict() for item in self.dynamic_imports],
            "parent_source_uses": [item.to_dict() for item in self.parent_source_uses],
            "findings": list(self.findings),
            "attributes": canonicalize(self.attributes),
            "resolved": self.resolved,
        }


@dataclass(frozen=True, slots=True)
class SourceRiskResolution:
    work_id: str
    status: ResolutionStatus
    code: str
    path_evidence: tuple[PathBoundaryEvidence, ...]
    reason: str
    digest: str

    @property
    def resolved(self) -> bool:
        return self.status.closes_blocker and all(item.resolved for item in self.path_evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "status": self.status.value,
            "code": self.code,
            "path_evidence": [item.to_dict() for item in self.path_evidence],
            "reason": self.reason,
            "resolved": self.resolved,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class RepositoryBoundaryReport:
    revision: str
    item_resolutions: tuple[SourceRiskResolution, ...]
    openclaw_runtime_hits: tuple[str, ...]
    langgraph_runtime_hits: tuple[str, ...]
    parent_runtime_hits: tuple[str, ...]
    dynamic_install_hits: tuple[str, ...]
    opaque_runtime_hits: tuple[str, ...]
    digest: str

    @property
    def ready(self) -> bool:
        return (
            all(item.resolved for item in self.item_resolutions)
            and not self.openclaw_runtime_hits
            and not self.langgraph_runtime_hits
            and not self.parent_runtime_hits
            and not self.dynamic_install_hits
            and not self.opaque_runtime_hits
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "item_resolutions": [item.to_dict() for item in self.item_resolutions],
            "openclaw_runtime_hits": list(self.openclaw_runtime_hits),
            "langgraph_runtime_hits": list(self.langgraph_runtime_hits),
            "parent_runtime_hits": list(self.parent_runtime_hits),
            "dynamic_install_hits": list(self.dynamic_install_hits),
            "opaque_runtime_hits": list(self.opaque_runtime_hits),
            "ready": self.ready,
            "digest": self.digest,
        }


class _PythonBoundaryVisitor(ast.NodeVisitor):
    def __init__(self, *, path: str) -> None:
        self.path = path
        self.processes: list[ProcessInvocation] = []
        self.dynamic_imports: list[DynamicImportUse] = []
        self.parent_uses: list[ParentSourceUse] = []
        self.closed_registries: set[str] = set()
        self.assignment_literals: dict[str, Any] = {}
        self._parents: list[ast.AST] = []

    def visit(self, node: ast.AST) -> Any:
        self._parents.append(node)
        try:
            return super().visit(node)
        finally:
            self._parents.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._literal(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name) and value is not None:
                self.assignment_literals[target.id] = value
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
                if all(self._literal(key) is not None for key in node.value.keys if key is not None):
                    self.closed_registries.add(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            value = self._literal(node.value)
            if value is not None:
                self.assignment_literals[node.target.id] = value
            if isinstance(node.value, ast.Dict) and all(
                self._literal(key) is not None for key in node.value.keys if key is not None
            ):
                self.closed_registries.add(node.target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        qualified = self._qualified(node.func)
        if qualified in PROCESS_CALL_NAMES:
            self.processes.append(self._process(node, qualified))
        if qualified in DYNAMIC_IMPORT_NAMES:
            argument = node.args[0] if node.args else None
            module = self._literal(argument) if argument is not None else None
            registry_bound = self._registry_bound(argument)
            self.dynamic_imports.append(
                DynamicImportUse(
                    path=self.path,
                    line=int(getattr(node, "lineno", 0)),
                    module=str(module or self._expression(argument)),
                    literal=isinstance(module, str),
                    registry_bound=registry_bound,
                    source_kind="python_ast",
                )
            )
        for argument in (*node.args, *(item.value for item in node.keywords)):
            literal = self._literal(argument)
            for literal_text in self._literal_strings(literal):
                self._record_parent_literal(
                    literal_text,
                    line=int(getattr(argument, "lineno", getattr(node, "lineno", 0))),
                    effect=qualified or "call",
                    production_effect=qualified
                    in PROCESS_CALL_NAMES | DYNAMIC_IMPORT_NAMES
                    or qualified
                    in {
                        "open",
                        "Path",
                        "Path.open",
                        "Path.read_text",
                        "Path.read_bytes",
                        "Path.resolve",
                    },
                )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._record_parent_literal(
                node.value,
                line=int(getattr(node, "lineno", 0)),
                effect="literal",
                production_effect=False,
            )

    def _process(self, node: ast.Call, qualified: str) -> ProcessInvocation:
        shell = any(
            item.arg == "shell" and self._literal(item.value) is True
            for item in node.keywords
        )
        command = node.args[0] if node.args else None
        resolved = self._literal(command)
        literal = resolved is not None
        tokens: tuple[str, ...]
        if isinstance(resolved, str):
            tokens = tuple(split_command(resolved))
        elif isinstance(resolved, (list, tuple)):
            tokens = tuple(str(item) for item in resolved)
        else:
            tokens = (self._expression(command),) if command is not None else ()
        executable = tokens[0] if tokens else ""
        return ProcessInvocation(
            path=self.path,
            line=int(getattr(node, "lineno", 0)),
            executable=executable,
            arguments=tokens[1:],
            shell=shell,
            literal=literal,
            source_kind=qualified,
        )

    def _registry_bound(self, argument: ast.AST | None) -> bool:
        if not isinstance(argument, ast.Subscript):
            return False
        root = argument.value
        return isinstance(root, ast.Name) and root.id in self.closed_registries

    def _record_parent_literal(
        self,
        literal: str,
        *,
        line: int,
        effect: str,
        production_effect: bool,
    ) -> None:
        match = PARENT_SOURCE_PATTERN.search(literal.replace("\\", "/"))
        if not match:
            return
        digest = hashlib.sha256(literal.encode("utf-8")).hexdigest()
        candidate = ParentSourceUse(
            path=self.path,
            line=line,
            source_repository=match.group("repo"),
            literal_digest=f"sha256:{digest}",
            effect=effect,
            production_effect=production_effect,
        )
        identity = candidate.to_dict()
        if not any(item.to_dict() == identity for item in self.parent_uses):
            self.parent_uses.append(candidate)

    def _literal(self, node: ast.AST | None) -> Any:
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return self.assignment_literals.get(node.id)
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError):
            return None

    def _literal_strings(self, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(
                nested
                for item in value
                for nested in self._literal_strings(item)
            )
        if isinstance(value, Mapping):
            return tuple(
                nested
                for item in (*value.keys(), *value.values())
                for nested in self._literal_strings(item)
            )
        return ()

    @staticmethod
    def _qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _PythonBoundaryVisitor._qualified(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    @staticmethod
    def _expression(node: ast.AST | None) -> str:
        if node is None:
            return ""
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001 - evidence only.
            return type(node).__name__


class RepositoryBoundaryInspector:
    def __init__(
        self,
        project_root: Path,
        *,
        revision: str,
        runtime_roots: Sequence[str] = ("apps", "packages", "scripts"),
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        if not self.project_root.is_dir():
            raise ProductizationContractError(
                "project_root_missing",
                f"Zyra project root does not exist: {self.project_root}",
            )
        self.revision = revision
        self.runtime_roots = tuple(normalize_repo_path(item) for item in runtime_roots)
        self._path_cache: dict[str, PathBoundaryEvidence] = {}

    def inspect_work_item(self, item: M3WorkItem) -> SourceRiskResolution:
        if item.action.value != "dispose_source_risk":
            raise ProductizationContractError(
                "source_inspector_action_invalid",
                f"{item.work_id} is not a source-risk disposition",
            )
        code = item.finding_codes[0]
        evidence = tuple(self.inspect_path(path, finding_code=code) for path in item.paths)
        if not evidence:
            evidence = (
                PathBoundaryEvidence(
                    path="packages",
                    scope=PathScope.UNKNOWN,
                    decision=BoundaryDecision.UNSUPPORTED,
                    reason="source-risk work item has no path",
                    content_digest=digest_payload(""),
                    findings=("source_risk_path_missing",),
                ),
            )
        if all(record.resolved for record in evidence):
            if all(record.scope.non_runtime for record in evidence):
                status = (
                    ResolutionStatus.EXTERNALIZED
                    if any(record.scope is PathScope.EVIDENCE for record in evidence)
                    else ResolutionStatus.NON_RUNTIME
                )
            else:
                status = ResolutionStatus.RETAINED_PROVEN
            reason = "structural repository boundary proves no opaque runtime custody"
        else:
            status = ResolutionStatus.OPEN
            reason = "one or more paths retain an unresolved runtime/source boundary"
        payload = {
            "work_id": item.work_id,
            "status": status.value,
            "code": code,
            "path_evidence": [record.to_dict() for record in evidence],
            "reason": reason,
        }
        return SourceRiskResolution(
            work_id=item.work_id,
            status=status,
            code=code,
            path_evidence=evidence,
            reason=reason,
            digest=digest_payload(payload),
        )

    def inspect_path(self, path: str, *, finding_code: str = "") -> PathBoundaryEvidence:
        normalized = normalize_repo_path(path)
        cache_key = f"{finding_code}:{normalized}"
        cached = self._path_cache.get(cache_key)
        if cached is not None:
            return cached
        absolute = (self.project_root / normalized).resolve()
        if not absolute.is_relative_to(self.project_root):
            evidence = PathBoundaryEvidence(
                path=normalized,
                scope=PathScope.UNKNOWN,
                decision=BoundaryDecision.MISSING_PATH,
                reason="queued path resolves outside the Zyra repository",
                content_digest=digest_payload("missing"),
                findings=("queued_path_outside_project",),
            )
            self._path_cache[cache_key] = evidence
            return evidence
        if not absolute.exists():
            evidence = PathBoundaryEvidence(
                path=normalized,
                scope=classify_scope(normalized),
                decision=BoundaryDecision.RETIRED,
                reason=(
                    "queued source-risk path is retired from the current "
                    "repository and cannot reach a runtime effect"
                ),
                content_digest=digest_payload("retired"),
                attributes={"current_path_absent": True},
            )
            self._path_cache[cache_key] = evidence
            return evidence
        scope = classify_scope(normalized)
        content_digest = file_digest(absolute)
        suffix = absolute.suffix.casefold()
        if suffix in ARCHIVE_SUFFIXES:
            evidence = self._inspect_archive(
                normalized,
                absolute,
                scope=scope,
                content_digest=content_digest,
            )
        elif absolute.name.casefold() == "package.json":
            evidence = self._inspect_package_manifest(
                normalized,
                absolute,
                scope=scope,
                content_digest=content_digest,
                finding_code=finding_code,
            )
        elif suffix in PYTHON_SUFFIXES:
            evidence = self._inspect_python(
                normalized,
                absolute,
                scope=scope,
                content_digest=content_digest,
                finding_code=finding_code,
            )
        elif suffix in SCRIPT_SUFFIXES:
            evidence = self._inspect_script(
                normalized,
                absolute,
                scope=scope,
                content_digest=content_digest,
                finding_code=finding_code,
            )
        elif suffix in NATIVE_SUFFIXES:
            evidence = self._inspect_native(
                normalized,
                scope=scope,
                content_digest=content_digest,
            )
        else:
            evidence = PathBoundaryEvidence(
                path=normalized,
                scope=scope,
                decision=(
                    BoundaryDecision.NON_RUNTIME
                    if scope.non_runtime
                    else BoundaryDecision.SAFE_RUNTIME
                ),
                reason="path contains no executable source or opaque runtime artifact",
                content_digest=content_digest,
            )
        self._path_cache[cache_key] = evidence
        return evidence

    def audit(
        self,
        items: Iterable[M3WorkItem],
    ) -> RepositoryBoundaryReport:
        resolutions = tuple(
            self.inspect_work_item(item)
            for item in items
            if item.action.value == "dispose_source_risk"
        )
        openclaw_hits: list[str] = []
        langgraph_hits: list[str] = []
        parent_hits: list[str] = []
        dynamic_installs: list[str] = []
        opaque_hits: list[str] = []
        for path in self._iter_runtime_sources():
            relative = path.relative_to(self.project_root).as_posix()
            scope = classify_scope(relative)
            if not scope.runtime_capable:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                if path.suffix.casefold() in NATIVE_SUFFIXES | ARCHIVE_SUFFIXES:
                    opaque_hits.append(relative)
                continue
            if OPENCLAW_RUNTIME_PATTERN.search(source):
                openclaw_hits.append(relative)
            if LANGGRAPH_IMPORT_PATTERN.search(source) and any(
                symbol in source for symbol in LANGGRAPH_BROAD_SYMBOLS
            ):
                langgraph_hits.append(relative)
            evidence = self.inspect_path(relative)
            for use in evidence.parent_source_uses:
                if use.production_effect:
                    parent_hits.append(f"{relative}:{use.line}:{use.source_repository}")
            for invocation in evidence.process_invocations:
                if is_dynamic_install(invocation):
                    dynamic_installs.append(
                        f"{relative}:{invocation.line}:{invocation.command_text}"
                    )
                if is_opaque_process(invocation):
                    opaque_hits.append(
                        f"{relative}:{invocation.line}:{invocation.executable_name}"
                    )
        payload = {
            "revision": self.revision,
            "item_resolutions": [item.to_dict() for item in resolutions],
            "openclaw_runtime_hits": sorted(set(openclaw_hits)),
            "langgraph_runtime_hits": sorted(set(langgraph_hits)),
            "parent_runtime_hits": sorted(set(parent_hits)),
            "dynamic_install_hits": sorted(set(dynamic_installs)),
            "opaque_runtime_hits": sorted(set(opaque_hits)),
        }
        return RepositoryBoundaryReport(
            revision=self.revision,
            item_resolutions=resolutions,
            openclaw_runtime_hits=tuple(payload["openclaw_runtime_hits"]),
            langgraph_runtime_hits=tuple(payload["langgraph_runtime_hits"]),
            parent_runtime_hits=tuple(payload["parent_runtime_hits"]),
            dynamic_install_hits=tuple(payload["dynamic_install_hits"]),
            opaque_runtime_hits=tuple(payload["opaque_runtime_hits"]),
            digest=digest_payload(payload),
        )

    def _inspect_python(
        self,
        normalized: str,
        absolute: Path,
        *,
        scope: PathScope,
        content_digest: str,
        finding_code: str,
    ) -> PathBoundaryEvidence:
        try:
            source = absolute.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=normalized, type_comments=True)
        except (OSError, UnicodeDecodeError, SyntaxError) as error:
            return PathBoundaryEvidence(
                path=normalized,
                scope=scope,
                decision=BoundaryDecision.UNSUPPORTED,
                reason=f"Python source cannot be structurally inspected: {error}",
                content_digest=content_digest,
                findings=("python_parse_failed",),
            )
        visitor = _PythonBoundaryVisitor(path=normalized)
        visitor.visit(tree)
        findings: list[str] = []
        decision = BoundaryDecision.NON_RUNTIME if scope.non_runtime else BoundaryDecision.SAFE_RUNTIME
        reason = "Python source has no unresolved runtime boundary"
        if finding_code == "python_dynamic_import_nonliteral":
            unsafe_imports = [
                item
                for item in visitor.dynamic_imports
                if not item.literal and not item.registry_bound
            ]
            if unsafe_imports and scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_DYNAMIC_IMPORT
                findings.append("production_dynamic_import_not_registry_bound")
                reason = "production dynamic import is not bound to a closed registry"
            elif unsafe_imports:
                decision = BoundaryDecision.NON_RUNTIME
                reason = "non-literal import exists only in a non-runtime test/audit surface"
            else:
                decision = (
                    BoundaryDecision.SAFE_RUNTIME
                    if scope.runtime_capable
                    else BoundaryDecision.NON_RUNTIME
                )
                reason = "dynamic import is literal or closed-registry bound"
        if finding_code == "python_parent_source_path":
            effect_uses = [item for item in visitor.parent_uses if item.production_effect]
            if effect_uses and scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_PARENT_SOURCE
                findings.append("parent_source_path_reaches_production_effect")
                reason = "parent source path reaches a file/process/import effect"
            elif visitor.parent_uses:
                decision = (
                    BoundaryDecision.PROVENANCE_ONLY
                    if scope.runtime_capable
                    else BoundaryDecision.NON_RUNTIME
                )
                reason = "parent source name is deny-list/provenance data, not an executable path"
        for invocation in visitor.processes:
            if is_dynamic_install(invocation) and scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_DYNAMIC_INSTALL
                findings.append("runtime_dynamic_install")
                reason = "runtime process can acquire mutable code"
            elif is_opaque_process(invocation) and scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_OPAQUE_BINARY
                findings.append("runtime_opaque_process")
                reason = "runtime invokes an opaque, undeclared binary"
        if OPENCLAW_RUNTIME_PATTERN.search(source) and scope.runtime_capable:
            decision = BoundaryDecision.BLOCKED_OPENCLAW_RUNTIME
            findings.append("openclaw_runtime_dependency")
            reason = "forward-excluded OpenClaw is reachable from production source"
        return PathBoundaryEvidence(
            path=normalized,
            scope=scope,
            decision=decision,
            reason=reason,
            content_digest=content_digest,
            process_invocations=tuple(visitor.processes),
            dynamic_imports=tuple(visitor.dynamic_imports),
            parent_source_uses=tuple(visitor.parent_uses),
            findings=tuple(dict.fromkeys(findings)),
            attributes={
                "python_ast": True,
                "closed_registry_count": len(visitor.closed_registries),
            },
        )

    def _inspect_script(
        self,
        normalized: str,
        absolute: Path,
        *,
        scope: PathScope,
        content_digest: str,
        finding_code: str,
    ) -> PathBoundaryEvidence:
        try:
            source = absolute.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            return PathBoundaryEvidence(
                path=normalized,
                scope=scope,
                decision=BoundaryDecision.UNSUPPORTED,
                reason=f"script source cannot be read: {error}",
                content_digest=content_digest,
                findings=("script_read_failed",),
            )
        invocations = tuple(self._script_processes(normalized, source))
        dynamic_imports = tuple(self._script_imports(normalized, source))
        parent_uses = tuple(self._script_parent_uses(normalized, source, invocations))
        decision = BoundaryDecision.NON_RUNTIME if scope.non_runtime else BoundaryDecision.SAFE_RUNTIME
        reason = "script source has no unresolved runtime boundary"
        findings: list[str] = []
        if finding_code == "omp_external_process":
            omp_processes = [
                item
                for item in invocations
                if re.search(r"(?i)(?:oh-my-pi|\bomp\b|mnemopi)", item.command_text)
            ]
            if omp_processes and scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_EXTERNAL_PROCESS
                findings.append("oh_my_pi_process_launch")
                reason = "production code launches an Oh My Pi/OMP process"
            else:
                decision = (
                    BoundaryDecision.PROVENANCE_ONLY
                    if scope.runtime_capable
                    else BoundaryDecision.NON_RUNTIME
                )
                reason = "OMP text describes in-repository schema/provenance; no process call exists"
        for invocation in invocations:
            if is_dynamic_install(invocation) and scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_DYNAMIC_INSTALL
                findings.append("runtime_dynamic_install")
                reason = "runtime script can acquire mutable code"
            elif is_opaque_process(invocation) and scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_OPAQUE_BINARY
                findings.append("runtime_opaque_process")
                reason = "runtime script invokes an opaque binary"
        if any(not item.literal and not item.registry_bound for item in dynamic_imports):
            if scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_DYNAMIC_IMPORT
                findings.append("script_dynamic_import_not_registry_bound")
                reason = "production script uses an unbounded dynamic import"
        if any(item.production_effect for item in parent_uses) and scope.runtime_capable:
            decision = BoundaryDecision.BLOCKED_PARENT_SOURCE
            findings.append("parent_source_path_reaches_production_effect")
            reason = "script process/import reaches a sibling source repository"
        if OPENCLAW_RUNTIME_PATTERN.search(source) and scope.runtime_capable:
            decision = BoundaryDecision.BLOCKED_OPENCLAW_RUNTIME
            findings.append("openclaw_runtime_dependency")
            reason = "forward-excluded OpenClaw is reachable from production script"
        if LANGGRAPH_IMPORT_PATTERN.search(source) and any(
            symbol in source for symbol in LANGGRAPH_BROAD_SYMBOLS
        ):
            if scope.runtime_capable:
                decision = BoundaryDecision.BLOCKED_LANGGRAPH_RUNTIME
                findings.append("langgraph_broad_runtime_dependency")
                reason = "broad LangGraph runtime is imported by production code"
        if finding_code == "source_similarity_vendor_like" and not findings:
            provenance = {
                term for term in PROVENANCE_TERMS if term in source.casefold()
            }
            if provenance:
                decision = BoundaryDecision.REPRODUCIBLE_SOURCE
                reason = "cropped same-language source retains explicit Zyra provenance/custody"
            else:
                decision = BoundaryDecision.UNSUPPORTED
                findings.append("similarity_provenance_missing")
                reason = "similar source lacks explicit provenance/custody markers"
        return PathBoundaryEvidence(
            path=normalized,
            scope=scope,
            decision=decision,
            reason=reason,
            content_digest=content_digest,
            process_invocations=invocations,
            dynamic_imports=dynamic_imports,
            parent_source_uses=parent_uses,
            findings=tuple(dict.fromkeys(findings)),
            attributes={"script_lexical_structure": True},
        )

    def _inspect_package_manifest(
        self,
        normalized: str,
        absolute: Path,
        *,
        scope: PathScope,
        content_digest: str,
        finding_code: str,
    ) -> PathBoundaryEvidence:
        try:
            payload = json.loads(absolute.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return PathBoundaryEvidence(
                path=normalized,
                scope=scope,
                decision=BoundaryDecision.UNSUPPORTED,
                reason=f"package manifest cannot be parsed: {error}",
                content_digest=content_digest,
                findings=("package_manifest_invalid",),
            )
        scripts = payload.get("scripts", {})
        if not isinstance(scripts, Mapping):
            scripts = {}
        invocations: list[ProcessInvocation] = []
        for script_name, script_value in scripts.items():
            if not isinstance(script_value, str):
                continue
            for command in split_command_chain(script_value):
                tokens = split_command(command)
                if not tokens:
                    continue
                invocations.append(
                    ProcessInvocation(
                        path=normalized,
                        line=0,
                        executable=tokens[0],
                        arguments=tuple(tokens[1:]),
                        shell=True,
                        literal=True,
                        source_kind=f"package_script:{script_name}",
                    )
                )
        dynamic = [item for item in invocations if is_dynamic_install(item)]
        opaque = [item for item in invocations if is_opaque_process(item)]
        findings: list[str] = []
        decision = BoundaryDecision.NON_RUNTIME if scope.non_runtime else BoundaryDecision.SAFE_RUNTIME
        reason = "package scripts use only frozen build/test/runtime commands"
        if dynamic and scope.runtime_capable:
            decision = BoundaryDecision.BLOCKED_DYNAMIC_INSTALL
            findings.append("package_script_dynamic_install")
            reason = "package script acquires or mutates dependencies"
        elif opaque and scope.runtime_capable:
            decision = BoundaryDecision.BLOCKED_OPAQUE_BINARY
            findings.append("package_script_opaque_binary")
            reason = "package script invokes an undeclared opaque binary"
        elif finding_code == "opaque_binary_process":
            declared = all(
                item.executable_name in COMMON_INTERPRETERS for item in invocations
            )
            if declared:
                decision = BoundaryDecision.DECLARED_TOOLCHAIN
                reason = "package invokes only declared source interpreters/toolchain"
            elif opaque:
                decision = BoundaryDecision.BLOCKED_OPAQUE_BINARY
                findings.append("package_script_opaque_binary")
        package_manager = str(payload.get("packageManager", "")).strip()
        dependencies = {
            **(
                payload.get("dependencies", {})
                if isinstance(payload.get("dependencies"), Mapping)
                else {}
            ),
            **(
                payload.get("devDependencies", {})
                if isinstance(payload.get("devDependencies"), Mapping)
                else {}
            ),
        }
        return PathBoundaryEvidence(
            path=normalized,
            scope=scope,
            decision=decision,
            reason=reason,
            content_digest=content_digest,
            process_invocations=tuple(invocations),
            findings=tuple(findings),
            attributes={
                "package_name": str(payload.get("name", "")),
                "package_manager": package_manager,
                "dependency_count": len(dependencies),
                "workspace_dependency_count": sum(
                    str(value).startswith("workspace:")
                    for value in dependencies.values()
                ),
            },
        )

    def _inspect_archive(
        self,
        normalized: str,
        absolute: Path,
        *,
        scope: PathScope,
        content_digest: str,
    ) -> PathBoundaryEvidence:
        if scope is PathScope.EVIDENCE:
            return PathBoundaryEvidence(
                path=normalized,
                scope=scope,
                decision=BoundaryDecision.EXTERNALIZED_EVIDENCE,
                reason="checksum-bound archive is isolated under review evidence",
                content_digest=content_digest,
                attributes={"bytes": absolute.stat().st_size},
            )
        if scope in {PathScope.VENDOR, PathScope.GENERATED}:
            return PathBoundaryEvidence(
                path=normalized,
                scope=scope,
                decision=BoundaryDecision.NON_RUNTIME,
                reason="archive is vendor/generated material and receives no runtime credit",
                content_digest=content_digest,
                attributes={"bytes": absolute.stat().st_size},
            )
        return PathBoundaryEvidence(
            path=normalized,
            scope=scope,
            decision=BoundaryDecision.BLOCKED_OPAQUE_BINARY,
            reason="archive is inside a runtime-capable package boundary",
            content_digest=content_digest,
            findings=("runtime_archive_present",),
            attributes={"bytes": absolute.stat().st_size},
        )

    @staticmethod
    def _inspect_native(
        normalized: str,
        *,
        scope: PathScope,
        content_digest: str,
    ) -> PathBoundaryEvidence:
        if scope.non_runtime:
            return PathBoundaryEvidence(
                path=normalized,
                scope=scope,
                decision=BoundaryDecision.NON_RUNTIME,
                reason="native artifact is outside the runtime-capable product source",
                content_digest=content_digest,
            )
        return PathBoundaryEvidence(
            path=normalized,
            scope=scope,
            decision=BoundaryDecision.BLOCKED_OPAQUE_BINARY,
            reason="native artifact lacks an in-repository source/build custody proof",
            content_digest=content_digest,
            findings=("opaque_native_artifact",),
        )

    @staticmethod
    def _script_processes(path: str, source: str) -> Iterator[ProcessInvocation]:
        for match in SCRIPT_PROCESS_CALL.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            opening = source.find("(", match.start(), match.end() + 1)
            argument = first_call_argument(source, opening)
            literal = strip_string_literal(argument)
            tokens: list[str] = []
            if literal is not None:
                tokens = split_command(literal)
            elif argument.lstrip().startswith("["):
                tokens = re.findall(r"[\"']([^\"']+)[\"']", argument)
            executable = tokens[0] if tokens else argument.strip()[:256]
            yield ProcessInvocation(
                path=path,
                line=line,
                executable=executable,
                arguments=tuple(tokens[1:]),
                shell=False,
                literal=bool(tokens),
                source_kind=match.group(0).split("(", 1)[0].strip(),
            )

    @staticmethod
    def _script_imports(path: str, source: str) -> Iterator[DynamicImportUse]:
        registry_names = set(
            re.findall(
                r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:Object\.freeze\s*\()?\s*\{",
                source,
            )
        )
        for match in SCRIPT_DYNAMIC_IMPORT.finditer(source):
            argument = match.group("argument").strip()
            literal = strip_string_literal(argument)
            registry_bound = any(
                re.fullmatch(rf"{re.escape(name)}\s*\[[^\]]+\]", argument)
                for name in registry_names
            )
            yield DynamicImportUse(
                path=path,
                line=source.count("\n", 0, match.start()) + 1,
                module=literal if literal is not None else argument[:256],
                literal=literal is not None,
                registry_bound=registry_bound,
                source_kind="javascript_dynamic_import",
            )

    @staticmethod
    def _script_parent_uses(
        path: str,
        source: str,
        invocations: Sequence[ProcessInvocation],
    ) -> Iterator[ParentSourceUse]:
        process_lines = {item.line for item in invocations}
        for line_number, line in enumerate(source.splitlines(), start=1):
            match = PARENT_SOURCE_PATTERN.search(line.replace("\\", "/"))
            if not match:
                continue
            digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
            yield ParentSourceUse(
                path=path,
                line=line_number,
                source_repository=match.group("repo"),
                literal_digest=f"sha256:{digest}",
                effect="process" if line_number in process_lines else "literal",
                production_effect=line_number in process_lines,
            )

    def _iter_runtime_sources(self) -> Iterator[Path]:
        ignored = {
            ".git",
            ".tmp",
            ".pytest_cache",
            "node_modules",
            "dist",
            "build",
            "__pycache__",
        }
        for root_name in self.runtime_roots:
            root = self.project_root / root_name
            if not root.exists():
                continue
            for current_root, directories, files in os.walk(root):
                directories[:] = [
                    name for name in directories if name not in ignored
                ]
                current = Path(current_root)
                for filename in files:
                    path = current / filename
                    if (
                        path.suffix.casefold() in SOURCE_SUFFIXES | ARCHIVE_SUFFIXES | NATIVE_SUFFIXES
                        or filename.casefold() == "package.json"
                    ):
                        yield path


def classify_scope(path: str) -> PathScope:
    normalized = normalize_repo_path(path)
    parts = PurePosixPath(normalized).parts
    lower = tuple(item.casefold() for item in parts)
    filename = lower[-1]
    if lower[0] in {"vendor", "vendor-runtimes", "third_party", "runtime-sources"}:
        return PathScope.VENDOR
    if "generated" in lower or filename.endswith((".generated.ts", ".generated.py")):
        return PathScope.GENERATED
    if lower[0] == "docs":
        if len(lower) >= 3 and lower[1:3] == ("reviews", "evidence"):
            return PathScope.EVIDENCE
        return PathScope.DOCUMENTATION
    if lower[0] == "tests" or any(
        item in {"test", "tests", "__tests__", "fixtures"} for item in lower
    ) or filename.startswith("test_") or ".test." in filename or ".spec." in filename:
        return PathScope.TEST
    if lower[0] == "scripts":
        if filename.startswith(
            (
                "audit_",
                "verify_",
                "sync_",
                "probe_",
                "run_m1_",
                "m1_r01_",
                "cleanroom_",
            )
        ) or "remediation" in lower:
            return PathScope.AUDIT_TOOL
        return PathScope.BUILD_TOOL
    if lower[0] in {"apps", "packages", "skills"}:
        if (
            lower[:2] == ("packages", "evaluation")
            or any(
                item in {"audit", "audits", "freeze_audit", "source_custody"}
                for item in lower
            )
            or filename.endswith(("_audit.py", "_audit.ts"))
            or (
            len(lower) >= 4
            and lower[:3] == (
                "packages",
                "integrations",
                "zyra_integrations",
            )
            and filename.startswith("ledger_")
            )
        ):
            return PathScope.AUDIT_TOOL
        return PathScope.PRODUCTION
    return PathScope.UNKNOWN


def split_command(command: str) -> list[str]:
    value = str(command).strip()
    if not value:
        return []
    try:
        return shlex.split(value, posix=os.name != "nt")
    except ValueError:
        return [
            token.strip("\"'")
            for token in re.findall(r"\"[^\"]*\"|'[^']*'|[^\s]+", value)
        ]


def split_command_chain(command: str) -> tuple[str, ...]:
    pieces = re.split(r"\s*(?:&&|\|\||;)\s*", str(command))
    return tuple(piece.strip() for piece in pieces if piece.strip())


def is_dynamic_install(invocation: ProcessInvocation) -> bool:
    executable = invocation.executable_name
    arguments = [item.casefold().strip("\"'") for item in invocation.arguments]
    if executable == "npx":
        return True
    if executable not in INSTALLER_EXECUTABLES:
        return False
    if executable.startswith("pip") or executable == "uv":
        return bool(arguments) and arguments[0] in INSTALL_ACTIONS | {"pip"}
    if executable == "yarn" and not arguments:
        return True
    if not arguments:
        return False
    first = arguments[0]
    if first.startswith("-"):
        first_non_option = next(
            (item for item in arguments if item and not item.startswith("-")),
            "",
        )
        first = first_non_option
    return first in INSTALL_ACTIONS


def is_opaque_process(invocation: ProcessInvocation) -> bool:
    executable = invocation.executable_name
    if executable in COMMON_INTERPRETERS:
        return False
    return any(executable.endswith(suffix) for suffix in NATIVE_SUFFIXES)


def first_call_argument(source: str, opening_index: int) -> str:
    if opening_index < 0:
        return ""
    depth = 0
    quote = ""
    escaped = False
    start = opening_index + 1
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char in "([{":
            depth += 1
            continue
        if char in ")]}":
            if char == ")" and depth == 0:
                return source[start:index].strip()
            depth = max(0, depth - 1)
            continue
        if char == "," and depth == 0:
            return source[start:index].strip()
    return source[start:].strip()


def strip_string_literal(value: str) -> str | None:
    candidate = str(value).strip()
    if len(candidate) < 2 or candidate[0] not in {"'", '"', "`"}:
        return None
    if candidate[-1] != candidate[0]:
        return None
    body = candidate[1:-1]
    if candidate[0] == "`" and "${" in body:
        return None
    try:
        if candidate[0] in {"'", '"'}:
            parsed = ast.literal_eval(candidate)
            return parsed if isinstance(parsed, str) else None
    except (SyntaxError, ValueError):
        return body
    return body


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"
