from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    AuditSection,
    Disposition,
    Evidence,
    EvidenceKind,
    Finding,
    RuleSwitches,
    Severity,
    content_digest,
    finding,
    section,
)
from .repository import RepositoryFile, RepositoryInventory


PARENT_SOURCE_NAMES = frozenset(
    {
        "claude-code-best",
        "browser-use",
        "openhands",
        "opencode",
        "agentscope",
        "agent-framework",
        "hermes-agent",
        "langgraph",
        "oh-my-pi",
        "openclaw",
    }
)
FORBIDDEN_LANGGRAPH_PREFIXES = (
    "langgraph.graph",
    "langgraph.pregel",
    "langgraph.channels",
    "langgraph.managed",
    "langgraph.prebuilt",
    "langgraph.store",
    "langgraph.stream",
    "langgraph_sdk",
    "langgraph_api",
    "langgraph_cli",
)
ALLOWED_NARROW_LANGGRAPH_TERMS = frozenset(
    {
        "checkpoint_id",
        "checkpoint_ns",
        "parent_checkpoint",
        "pending_writes",
        "committed_writes",
        "interrupt_id",
        "resume_id",
        "stable_task_id",
        "lineage",
        "atomic_commit",
        "exact_resume",
    }
)
DYNAMIC_IMPORT_CALLS = frozenset(
    {
        "__import__",
        "importlib.import_module",
        "importlib.util.spec_from_file_location",
        "importlib.machinery.SourceFileLoader",
        "pkgutil.resolve_name",
    }
)
PROCESS_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "os.system",
        "os.popen",
        "pexpect.spawn",
        "multiprocessing.Process",
    }
)
SHELL_PROCESS_CALLS = frozenset(
    {
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "asyncio.create_subprocess_shell",
        "os.system",
        "os.popen",
    }
)
NETWORK_CALLS = frozenset(
    {
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.patch",
        "requests.delete",
        "httpx.get",
        "httpx.post",
        "httpx.stream",
        "urllib.request.urlopen",
        "aiohttp.ClientSession",
        "socket.create_connection",
        "socket.socket.bind",
    }
)
PORT_CALLS = frozenset(
    {
        "socket.bind",
        "socket.socket.bind",
        "uvicorn.run",
        "app.run",
        "server.listen",
    }
)
HOME_CALLS = frozenset(
    {
        "Path.home",
        "os.path.expanduser",
        "os.environ.get",
        "os.getenv",
    }
)
SQLITE_CALLS = frozenset(
    {
        "sqlite3.connect",
        "aiosqlite.connect",
        "sqlalchemy.create_engine",
    }
)
INSTALL_TOKENS = frozenset(
    {
        "pip",
        "pip3",
        "uv",
        "poetry",
        "npm",
        "npx",
        "bun",
        "pnpm",
        "yarn",
        "cargo",
    }
)
ABSOLUTE_WINDOWS = re.compile(r"(?i)\b[A-Z]:[\\/]")
PARENT_REPO_PATH = re.compile(
    r"(?i)(?:^|[\\/])\.\.[\\/](claude-code-best|browser-use|OpenHands|opencode|"
    r"agentscope|agent-framework|hermes-agent|langgraph|oh-my-pi|openclaw)(?:[\\/]|$)"
)
URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PythonImport:
    module: str
    alias: str
    line: int
    dynamic: bool
    literal: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "alias": self.alias,
            "line": self.line,
            "dynamic": self.dynamic,
            "literal": self.literal,
        }


@dataclass(frozen=True, slots=True)
class PythonCall:
    qualified_name: str
    line: int
    arguments: tuple[str, ...]
    keyword_arguments: Mapping[str, str]
    literal_arguments: tuple[Any, ...]

    def has_token(self, token: str) -> bool:
        selected = token.casefold()
        return any(selected in argument.casefold() for argument in self.arguments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualified_name": self.qualified_name,
            "line": self.line,
            "arguments": list(self.arguments),
            "keyword_arguments": dict(self.keyword_arguments),
            "literal_arguments": list(self.literal_arguments),
        }


@dataclass(frozen=True, slots=True)
class PythonFileAnalysis:
    path: str
    imports: tuple[PythonImport, ...]
    calls: tuple[PythonCall, ...]
    string_literals: tuple[tuple[int, str], ...]
    assigned_names: Mapping[str, str]
    parse_error: str
    digest: str

    def imported_modules(self) -> tuple[str, ...]:
        return tuple(sorted({item.module for item in self.imports if item.module}))

    def calls_named(self, names: Iterable[str]) -> tuple[PythonCall, ...]:
        allowed = set(names)
        return tuple(call for call in self.calls if call.qualified_name in allowed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "imports": [item.to_dict() for item in self.imports],
            "calls": [item.to_dict() for item in self.calls],
            "string_literal_count": len(self.string_literals),
            "assigned_names": dict(self.assigned_names),
            "parse_error": self.parse_error,
            "digest": self.digest,
        }


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: list[PythonImport] = []
        self.calls: list[PythonCall] = []
        self.string_literals: list[tuple[int, str]] = []
        self.assigned_names: dict[str, str] = {}
        self.aliases: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            self.aliases[local] = alias.name
            self.imports.append(
                PythonImport(
                    module=alias.name,
                    alias=local,
                    line=node.lineno,
                    dynamic=False,
                    literal=True,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        dots = "." * node.level
        base = f"{dots}{node.module or ''}"
        for alias in node.names:
            local = alias.asname or alias.name
            qualified = f"{base}.{alias.name}" if base else alias.name
            self.aliases[local] = qualified
            self.imports.append(
                PythonImport(
                    module=base,
                    alias=local,
                    line=node.lineno,
                    dynamic=False,
                    literal=True,
                )
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        rendered = _safe_unparse(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name) and rendered:
                self.assigned_names[target.id] = rendered
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            rendered = _safe_unparse(node.value)
            if rendered:
                self.assigned_names[node.target.id] = rendered
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value:
            self.string_literals.append((node.lineno, node.value))

    def visit_Call(self, node: ast.Call) -> None:
        raw_name = _call_name(node.func)
        qualified = self._resolve_alias(raw_name)
        arguments = tuple(_safe_unparse(argument) for argument in node.args)
        keywords = {
            keyword.arg or "**": _safe_unparse(keyword.value)
            for keyword in node.keywords
        }
        literal_arguments: list[Any] = []
        for argument in node.args:
            try:
                literal_arguments.append(ast.literal_eval(argument))
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                continue
        self.calls.append(
            PythonCall(
                qualified_name=qualified,
                line=node.lineno,
                arguments=arguments,
                keyword_arguments=keywords,
                literal_arguments=tuple(literal_arguments),
            )
        )
        if qualified in DYNAMIC_IMPORT_CALLS or raw_name in DYNAMIC_IMPORT_CALLS:
            module = ""
            literal = False
            if node.args:
                try:
                    value = ast.literal_eval(node.args[0])
                except (ValueError, TypeError, SyntaxError):
                    value = None
                if isinstance(value, str):
                    module = value
                    literal = True
            self.imports.append(
                PythonImport(
                    module=module,
                    alias="",
                    line=node.lineno,
                    dynamic=True,
                    literal=literal,
                )
            )
        self.generic_visit(node)

    def _resolve_alias(self, name: str) -> str:
        if not name:
            return ""
        head, separator, tail = name.partition(".")
        replacement = self.aliases.get(head)
        if replacement:
            return f"{replacement}{separator}{tail}" if separator else replacement
        return name


class PythonAnalyzer:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def analyze(
        self, inventory: RepositoryInventory
    ) -> tuple[tuple[PythonFileAnalysis, ...], AuditSection]:
        analyses: list[PythonFileAnalysis] = []
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        candidates = [
            item
            for item in inventory.files
            if item.kind == "source"
            and item.suffix in {".py", ".pyi"}
            and not item.path.startswith(("vendor/", "vendor-runtimes/"))
        ]
        for record in candidates:
            analysis = self._analyze_file(record)
            analyses.append(analysis)
            if analysis.parse_error:
                findings.append(
                    finding(
                        "python_source_parse_failed",
                        f"Python source cannot be parsed: {analysis.parse_error}",
                        "dependencies",
                        severity=Severity.ERROR,
                        path=analysis.path,
                        disposition=Disposition.DECLARE,
                        remediation="Repair syntax or explicitly exclude non-runtime source.",
                        default_path_impact="Dependency and process calls in this file are unaudited.",
                    )
                )
                continue
            if self.switches.dependencies:
                findings.extend(self._dependency_findings(analysis))
            if self.switches.processes:
                findings.extend(self._process_findings(analysis))
            if self.switches.langgraph:
                findings.extend(self._langgraph_findings(analysis))
            if self.switches.source_specific:
                findings.extend(self._source_specific_findings(analysis))
            evidence.append(
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    path=analysis.path,
                    excerpt_digest=analysis.digest,
                    attributes={
                        "imports": len(analysis.imports),
                        "calls": len(analysis.calls),
                    },
                )
            )
        imported = Counter(
            item.module.split(".", 1)[0]
            for analysis in analyses
            for item in analysis.imports
            if item.module and not item.module.startswith(".")
        )
        call_counts = Counter(
            call.qualified_name
            for analysis in analyses
            for call in analysis.calls
            if call.qualified_name
        )
        return tuple(analyses), section(
            "python_source",
            metrics={
                "files": len(analyses),
                "parsed": sum(not item.parse_error for item in analyses),
                "parse_errors": sum(bool(item.parse_error) for item in analyses),
                "imports": sum(len(item.imports) for item in analyses),
                "calls": sum(len(item.calls) for item in analyses),
                "top_import_roots": dict(imported.most_common(30)),
                "process_call_count": sum(
                    count
                    for name, count in call_counts.items()
                    if name in PROCESS_CALLS
                ),
            },
            findings=findings,
            evidence=evidence,
        )

    def _analyze_file(self, record: RepositoryFile) -> PythonFileAnalysis:
        path = self.project_root / record.path
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=record.path, type_comments=True)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            return PythonFileAnalysis(
                path=record.path,
                imports=(),
                calls=(),
                string_literals=(),
                assigned_names={},
                parse_error=str(exc),
                digest=record.digest,
            )
        visitor = _PythonVisitor()
        visitor.visit(tree)
        return PythonFileAnalysis(
            path=record.path,
            imports=tuple(visitor.imports),
            calls=tuple(visitor.calls),
            string_literals=tuple(visitor.string_literals),
            assigned_names=dict(sorted(visitor.assigned_names.items())),
            parse_error="",
            digest=record.digest,
        )

    def _dependency_findings(self, analysis: PythonFileAnalysis) -> list[Finding]:
        findings: list[Finding] = []
        for imported in analysis.imports:
            if imported.dynamic and not imported.literal:
                findings.append(
                    finding(
                        "python_dynamic_import_nonliteral",
                        "Dynamic Python import uses a non-literal module name.",
                        "dependencies",
                        severity=Severity.BLOCKER,
                        path=analysis.path,
                        line=imported.line,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Replace with a declared import registry and allowlist.",
                        default_path_impact="Runtime dependency cannot be resolved at freeze time.",
                    )
                )
            module = imported.module.casefold()
            if module == "openclaw" or module.startswith("openclaw."):
                findings.append(
                    finding(
                        "openclaw_python_import",
                        "Python runtime imports the forward-excluded OpenClaw package.",
                        "dependencies",
                        severity=Severity.BLOCKER,
                        source_repo="openclaw",
                        path=analysis.path,
                        line=imported.line,
                        disposition=Disposition.REMOVE,
                        remediation="Remove the import and use the selected Zyra owner.",
                        default_path_impact="Release would reintroduce an excluded runtime source.",
                    )
                )
        for line, literal in analysis.string_literals:
            normalized = literal.replace("\\", "/")
            match = PARENT_REPO_PATH.search(normalized)
            if match:
                findings.append(
                    finding(
                        "python_parent_source_path",
                        "Python source embeds a parent source-repository path.",
                        "dependencies",
                        severity=Severity.BLOCKER,
                        source_repo=match.group(1),
                        path=analysis.path,
                        line=line,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Use project-local source or a declared package boundary.",
                        default_path_impact="Clean submission would depend on a sibling repository.",
                        attributes={"literal_digest": content_digest(literal.encode())},
                    )
                )
            if ABSOLUTE_WINDOWS.search(literal) and any(
                name in normalized.casefold() for name in PARENT_SOURCE_NAMES
            ):
                findings.append(
                    finding(
                        "python_absolute_source_path",
                        "Python source embeds an absolute source-repository path.",
                        "dependencies",
                        severity=Severity.BLOCKER,
                        path=analysis.path,
                        line=line,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Remove workspace-specific source paths.",
                        default_path_impact="Clean-machine behavior is not reproducible.",
                        attributes={"literal_digest": content_digest(literal.encode())},
                    )
                )
        return findings

    def _process_findings(self, analysis: PythonFileAnalysis) -> list[Finding]:
        findings: list[Finding] = []
        for call in analysis.calls:
            if call.qualified_name not in PROCESS_CALLS | SHELL_PROCESS_CALLS:
                continue
            shell_value = call.keyword_arguments.get("shell", "")
            shell_enabled = shell_value in {"True", "true", "1"}
            nonliteral = not call.literal_arguments and bool(call.arguments)
            command_tokens = tuple(_literal_tokens(call.literal_arguments))
            installer = next(
                (
                    token
                    for token in command_tokens
                    if Path(token).name.casefold() in INSTALL_TOKENS
                ),
                "",
            )
            if call.qualified_name in SHELL_PROCESS_CALLS or shell_enabled:
                findings.append(
                    finding(
                        "python_shell_process_call",
                        "Python runtime starts a shell-mediated process.",
                        "processes",
                        severity=Severity.BLOCKER,
                        path=analysis.path,
                        line=call.line,
                        disposition=Disposition.EXTERNALIZE,
                        remediation="Use an argv-based declared process profile and policy.",
                        default_path_impact="Shell expansion can bypass dependency/process custody.",
                        attributes={"call": call.qualified_name},
                    )
                )
            elif nonliteral:
                findings.append(
                    finding(
                        "python_process_command_nonliteral",
                        "Python process command cannot be resolved statically.",
                        "processes",
                        severity=Severity.ERROR,
                        path=analysis.path,
                        line=call.line,
                        disposition=Disposition.DECLARE,
                        remediation="Bind command identity to a declared process profile.",
                        default_path_impact="Release process graph is incomplete.",
                        attributes={"call": call.qualified_name},
                    )
                )
            if installer:
                findings.append(
                    finding(
                        "python_runtime_installer_call",
                        f"Python runtime invokes package installer {installer!r}.",
                        "processes",
                        severity=Severity.BLOCKER,
                        path=analysis.path,
                        line=call.line,
                        disposition=Disposition.REMOVE,
                        remediation="Lock dependencies at build/install time; do not install dynamically.",
                        default_path_impact="Runtime can download undeclared executable code.",
                        attributes={"installer": installer},
                    )
                )
        for call in analysis.calls:
            if call.qualified_name in NETWORK_CALLS:
                url = next(
                    (
                        literal
                        for literal in call.literal_arguments
                        if isinstance(literal, str) and URL_PATTERN.match(literal)
                    ),
                    "",
                )
                if url and _download_like(url):
                    findings.append(
                        finding(
                            "python_undeclared_download",
                            "Python runtime contains a direct code/archive download call.",
                            "processes",
                            severity=Severity.BLOCKER,
                            path=analysis.path,
                            line=call.line,
                            disposition=Disposition.EXTERNALIZE,
                            remediation="Declare checksum, provenance, cache, and build-time acquisition.",
                            default_path_impact="Release may fetch mutable undeclared code.",
                            attributes={"url_digest": content_digest(url.encode())},
                        )
                    )
        return findings

    def _langgraph_findings(self, analysis: PythonFileAnalysis) -> list[Finding]:
        findings: list[Finding] = []
        for imported in analysis.imports:
            module = imported.module.casefold()
            if any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in FORBIDDEN_LANGGRAPH_PREFIXES
            ):
                findings.append(
                    finding(
                        "langgraph_broad_runtime_import",
                        f"Default source imports forbidden broad LangGraph module {imported.module!r}.",
                        "langgraph",
                        severity=Severity.BLOCKER,
                        source_repo="langgraph",
                        path=analysis.path,
                        line=imported.line,
                        disposition=Disposition.REMOVE,
                        remediation="Use Zyra dynamic graph/runtime owners; retain exact-resume semantics only.",
                        default_path_impact="Broad LangGraph runtime could take canonical custody.",
                        attributes={"module": imported.module},
                    )
                )
        forbidden_symbols = {
            "StateGraph",
            "MessageGraph",
            "Pregel",
            "ToolNode",
            "create_react_agent",
            "InMemoryStore",
            "RemoteGraph",
            "GraphRunStream",
            "StreamController",
        }
        for call in analysis.calls:
            tail = call.qualified_name.rsplit(".", 1)[-1]
            if tail in forbidden_symbols:
                findings.append(
                    finding(
                        "langgraph_broad_runtime_call",
                        f"Source calls forbidden broad LangGraph symbol {tail!r}.",
                        "langgraph",
                        severity=Severity.BLOCKER,
                        source_repo="langgraph",
                        path=analysis.path,
                        line=call.line,
                        disposition=Disposition.REMOVE,
                        remediation="Keep CodeWorker/dynamic-topology/canonical stores Zyra-owned.",
                        default_path_impact="Broad framework call can replace the selected main path.",
                        attributes={"symbol": tail},
                    )
                )
        return findings

    def _source_specific_findings(
        self, analysis: PythonFileAnalysis
    ) -> list[Finding]:
        findings: list[Finding] = []
        path_lower = analysis.path.casefold()
        for call in analysis.calls:
            if call.qualified_name in SQLITE_CALLS:
                severity = (
                    Severity.BLOCKER
                    if any(name in path_lower for name in ("hermes", "oh_my_pi", "omp"))
                    else Severity.WARNING
                )
                findings.append(
                    finding(
                        "runtime_sqlite_custody",
                        "Runtime opens SQLite state and must declare owner, path, restore, and packaging.",
                        "source_specific",
                        severity=severity,
                        path=analysis.path,
                        line=call.line,
                        disposition=Disposition.DECLARE,
                        remediation="Bind the database to a Zyra state owner and clean profile.",
                        default_path_impact="Hidden local state can invalidate clean-run evidence.",
                        attributes={"call": call.qualified_name},
                    )
                )
            if call.qualified_name in HOME_CALLS:
                rendered = " ".join(call.arguments)
                if "~" in rendered or "HOME" in rendered or "USERPROFILE" in rendered:
                    findings.append(
                        finding(
                            "runtime_home_state_access",
                            "Runtime derives persistent state from the user home directory.",
                            "source_specific",
                            severity=Severity.ERROR,
                            path=analysis.path,
                            line=call.line,
                            disposition=Disposition.EXTERNALIZE,
                            remediation="Route state through an explicit Zyra workspace/profile path.",
                            default_path_impact="Clean-machine and privacy boundaries are ambiguous.",
                        )
                    )
        return findings


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _call_name(node.value)
        return f"{head}.{node.attr}" if head else node.attr
    if isinstance(node, ast.Subscript):
        return _safe_unparse(node)
    return ""


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (ValueError, TypeError, RecursionError):
        return f"<{type(node).__name__}>"


def _literal_tokens(values: Iterable[Any]) -> Iterator[str]:
    for value in values:
        if isinstance(value, str):
            for token in re.split(r"\s+", value):
                if token:
                    yield token
        elif isinstance(value, (list, tuple)):
            for nested in _literal_tokens(value):
                yield nested


def _download_like(url: str) -> bool:
    lowered = url.casefold().split("?", 1)[0]
    return any(
        lowered.endswith(suffix)
        for suffix in (
            ".zip",
            ".tar",
            ".tar.gz",
            ".tgz",
            ".whl",
            ".exe",
            ".dll",
            ".so",
            ".dylib",
            ".node",
            ".wasm",
            ".jsonl",
        )
    ) or any(
        fragment in lowered
        for fragment in (
            "github.com/releases/download",
            "raw.githubusercontent.com",
            "/install.sh",
            "/install.ps1",
        )
    )
