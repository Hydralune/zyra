from __future__ import annotations

import ast
import json
import os
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity


_SOURCE_REPOSITORIES = {
    "claude-code-best",
    "browser-use",
    "openhands",
    "opencode",
    "oh-my-pi",
    "agentscope",
    "agent-framework",
    "langgraph",
    "hermes-agent",
    "openclaw",
}
_FORBIDDEN_POOL_SEGMENTS = {
    "vendor-runtimes",
    "runtime-sources",
    "source-pool",
    "runtime-sources",
    "productized-sources",
    "third_party_sources",
}
_OPAQUE_SUFFIXES = {
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".wasm",
    ".node",
    ".whl",
    ".jar",
    ".zip",
    ".tar",
    ".tgz",
    ".gz",
}
_CODE_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs"}
_DEFAULT_SCAN_ROOTS = ("apps", "packages", "skills", "scripts")
_IGNORED_SEGMENTS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tmp",
    "dist",
    "build",
    "target",
    "tmp",
    "test",
    "tests",
    "fixtures",
    "mocks",
    "remediation",
}


@dataclass(frozen=True, slots=True)
class DependencyReference:
    source_file: str
    line: int
    kind: str
    value: str
    resolved_path: str = ""
    dynamic: bool = False
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "line": self.line,
            "kind": self.kind,
            "value": self.value,
            "resolved_path": self.resolved_path,
            "dynamic": self.dynamic,
            "metadata": dict(self.metadata),
        }


class PythonDependencyScanner(ast.NodeVisitor):
    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root
        self.references: list[DependencyReference] = []
        self._aliases: dict[str, str] = {}

    def scan(self) -> list[DependencyReference]:
        try:
            text = self.path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(self.path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            return []
        self.visit(tree)
        return self.references

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            self._record(node, "python_import", alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = ("." * node.level) + (node.module or "")
        self._record(node, "python_import", module)
        for alias in node.names:
            self._aliases[alias.asname or alias.name] = f"{module}.{alias.name}".strip(".")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self._call_name(node.func)
        string_args = [self._literal(arg) for arg in node.args]
        if name in {"subprocess.run", "subprocess.Popen", "subprocess.call", "os.system", "os.popen"}:
            command = next((value for value in string_args if value is not None), "<dynamic>")
            self._record(node, "process", command, dynamic=command == "<dynamic>")
        elif name in {"Path", "pathlib.Path", "open", "io.open"}:
            value = next((value for value in string_args if value is not None), None)
            if value is not None:
                self._record(node, "path_literal", value, resolved=self._resolve_literal(value))
        elif name in {"importlib.import_module", "__import__"}:
            value = next((value for value in string_args if value is not None), "<dynamic>")
            self._record(node, "dynamic_import", value, dynamic=value == "<dynamic>")
        elif name.endswith("create_subprocess_exec") or name.endswith("create_subprocess_shell"):
            value = next((value for value in string_args if value is not None), "<dynamic>")
            self._record(node, "process", value, dynamic=value == "<dynamic>")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # Bare policy/example strings do not constitute runtime dependencies. Concrete
        # path/process/import calls are recorded by ``visit_Call`` instead.
        self.generic_visit(node)

    def _record(
        self,
        node: ast.AST,
        kind: str,
        value: str,
        *,
        resolved: str = "",
        dynamic: bool = False,
    ) -> None:
        self.references.append(
            DependencyReference(
                source_file=self.path.relative_to(self.root).as_posix(),
                line=int(getattr(node, "lineno", 0)),
                kind=kind,
                value=value,
                resolved_path=resolved,
                dynamic=dynamic,
            )
        )

    def _call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self._aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            prefix = self._call_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    @staticmethod
    def _literal(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
            return str(node.value)
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [PythonDependencyScanner._literal(item) for item in node.elts]
            if all(value is not None for value in values):
                return " ".join(value or "" for value in values)
        return None

    def _resolve_literal(self, value: str) -> str:
        if not value or "${" in value or "%" in value:
            return ""
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.path.parent / candidate
        try:
            return str(candidate.resolve(strict=False))
        except OSError:
            return ""


class ScriptDependencyScanner:
    _IMPORT = re.compile(
        r"(?:from\s+|import\s*(?:\(|)\s*|require\s*\(\s*|import\s*\(\s*)['\"]([^'\"]+)['\"]"
    )
    _PROCESS = re.compile(
        r"\b(?:spawn|spawnSync|exec|execFile|execFileSync|Bun\.spawn|Bun\.spawnSync)\s*\(\s*(?:\[\s*)?['\"]([^'\"]+)['\"]"
    )
    _PATH = re.compile(r"['\"]([^'\"\r\n]*(?:\.\./|\.cache|node_modules|vendor-runtimes|source-pool)[^'\"\r\n]*)['\"]")

    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root

    def scan(self) -> list[DependencyReference]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        references: list[DependencyReference] = []
        references.extend(self._matches(text, self._IMPORT, "script_import"))
        references.extend(self._matches(text, self._PROCESS, "process"))
        for reference in self._matches(text, self._PATH, "path_literal"):
            references.append(
                DependencyReference(
                    source_file=reference.source_file,
                    line=reference.line,
                    kind=reference.kind,
                    value=reference.value,
                    resolved_path=self._resolve(reference.value),
                )
            )
        return references

    def _matches(self, text: str, pattern: re.Pattern[str], kind: str) -> list[DependencyReference]:
        found: list[DependencyReference] = []
        for match in pattern.finditer(text):
            found.append(
                DependencyReference(
                    source_file=self.path.relative_to(self.root).as_posix(),
                    line=text.count("\n", 0, match.start()) + 1,
                    kind=kind,
                    value=match.group(1),
                )
            )
        return found

    def _resolve(self, value: str) -> str:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.path.parent / candidate
        try:
            return str(candidate.resolve(strict=False))
        except OSError:
            return ""


class PackageBoundaryInspector:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def inspect(self) -> tuple[list[DependencyReference], list[Finding]]:
        references: list[DependencyReference] = []
        findings: list[Finding] = []
        package_json = self.root / "package.json"
        if package_json.is_file():
            refs, issues = self._package_json(package_json)
            references.extend(refs)
            findings.extend(issues)
        pyproject = self.root / "pyproject.toml"
        if pyproject.is_file():
            refs, issues = self._pyproject(pyproject)
            references.extend(refs)
            findings.extend(issues)
        for path in self.root.rglob("*.pth"):
            if self._ignored(path):
                continue
            findings.append(
                Finding(
                    code="internalization.editable_path_file",
                    severity=Severity.BLOCKER,
                    summary="A .pth path injection exists inside the project boundary.",
                    location=path.relative_to(self.root).as_posix(),
                )
            )
        return references, findings

    def _package_json(self, path: Path) -> tuple[list[DependencyReference], list[Finding]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return [], [
                Finding(
                    code="internalization.package_json_invalid",
                    severity=Severity.ERROR,
                    summary="package.json could not be audited.",
                    detail=str(error),
                    location=str(path),
                )
            ]
        references: list[DependencyReference] = []
        findings: list[Finding] = []
        for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            values = payload.get(section)
            if not isinstance(values, Mapping):
                continue
            for name, spec in values.items():
                reference = DependencyReference(
                    source_file=path.relative_to(self.root).as_posix(),
                    line=0,
                    kind="npm_dependency",
                    value=f"{name}@{spec}",
                    metadata={"section": section, "name": name, "spec": spec},
                )
                references.append(reference)
                lowered = str(spec).lower()
                if lowered.startswith(("file:", "link:", "workspace:../")) or "../" in lowered:
                    findings.append(
                        Finding(
                            code="internalization.npm_external_link",
                            severity=Severity.BLOCKER,
                            summary="npm dependency uses a workspace-external file/link path.",
                            detail=reference.value,
                            location=reference.source_file,
                        )
                    )
        workspaces = payload.get("workspaces") or []
        if isinstance(workspaces, Mapping):
            workspaces = workspaces.get("packages") or []
        for value in workspaces if isinstance(workspaces, list) else []:
            normalized = str(value).replace("\\", "/")
            if normalized.startswith("../") or "/../" in normalized:
                findings.append(
                    Finding(
                        code="internalization.workspace_escape",
                        severity=Severity.BLOCKER,
                        summary="Bun/npm workspace escapes the Zyra repository.",
                        detail=normalized,
                        location=path.relative_to(self.root).as_posix(),
                    )
                )
        return references, findings

    def _pyproject(self, path: Path) -> tuple[list[DependencyReference], list[Finding]]:
        text = path.read_text(encoding="utf-8")
        references: list[DependencyReference] = []
        findings: list[Finding] = []
        for index, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower().replace("\\", "/")
            if any(marker in lowered for marker in ("path =", "develop = true", "editable")):
                references.append(
                    DependencyReference(
                        source_file=path.relative_to(self.root).as_posix(),
                        line=index,
                        kind="python_path_dependency",
                        value=line.strip(),
                    )
                )
                if "../" in lowered:
                    findings.append(
                        Finding(
                            code="internalization.python_external_path",
                            severity=Severity.BLOCKER,
                            summary="Python package configuration refers outside Zyra.",
                            detail=line.strip(),
                            location=f"{path.relative_to(self.root).as_posix()}:{index}",
                        )
                    )
        return references, findings

    @staticmethod
    def _ignored(path: Path) -> bool:
        return any(part in _IGNORED_SEGMENTS for part in path.parts)


class M1InternalizationGate:
    def __init__(
        self,
        project_root: str | Path,
        *,
        source_workspace: str | Path | None = None,
    ) -> None:
        self.root = Path(project_root).resolve()
        self.source_workspace = Path(source_workspace).resolve() if source_workspace else self.root.parent

    def evaluate(
        self,
        *,
        scan_roots: Sequence[str] = _DEFAULT_SCAN_ROOTS,
        allowed_external_processes: Iterable[str] = (),
        allowed_opaque_assets: Iterable[str] = (),
    ) -> GateResult:
        result = GateResult(
            gate_id="m1-internalization",
            status=GateStatus.NOT_RUN,
            summary="Default-path source, dependency, process and clean-room boundary audit.",
        )
        allowed_processes = {item.lower() for item in allowed_external_processes}
        allowed_opaque = {Path(item).as_posix().lower() for item in allowed_opaque_assets}
        files = list(self._source_files(scan_roots))
        references: list[DependencyReference] = []
        for path in files:
            if path.suffix in {".py", ".pyi"}:
                references.extend(PythonDependencyScanner(path, self.root).scan())
            else:
                references.extend(ScriptDependencyScanner(path, self.root).scan())
        package_references, package_findings = PackageBoundaryInspector(self.root).inspect()
        references.extend(package_references)
        result.findings.extend(package_findings)
        result.findings.extend(self._audit_references(references, allowed_processes))
        result.findings.extend(self._audit_symlinks())
        result.findings.extend(self._audit_opaque_assets(allowed_opaque))
        result.findings.extend(self._audit_source_pool_locations())
        result.findings.extend(self._audit_runtime_state_dependencies(references))
        result.findings.extend(self._audit_openclaw_forward_boundary(references))
        result.findings.extend(self._audit_import_cycles(files, references))

        kinds = Counter(reference.kind for reference in references)
        process_values = sorted({reference.value for reference in references if reference.kind == "process"})
        external_paths = sorted({
            reference.resolved_path
            for reference in references
            if reference.resolved_path and not self._inside_project(reference.resolved_path)
        })
        result.metrics.update(
            {
                "scanned_file_count": len(files),
                "reference_count": len(references),
                "reference_kinds": dict(sorted(kinds.items())),
                "process_values": process_values,
                "external_resolved_paths": external_paths,
                "source_workspace": str(self.source_workspace),
                "references": [reference.to_dict() for reference in references],
            }
        )
        result.evidence.extend(
            EvidencePointer(
                kind="dependency_scan",
                location=reference.source_file,
                summary=f"{reference.kind}: {reference.value}",
                metadata={"line": reference.line, "resolved_path": reference.resolved_path},
            )
            for reference in references
            if reference.kind in {"process", "dynamic_import", "source_path_literal", "npm_dependency"}
        )
        return result.finish()

    def _source_files(self, scan_roots: Sequence[str]) -> Iterator[Path]:
        for relative in scan_roots:
            root = (self.root / relative).resolve()
            if not root.is_relative_to(self.root) or not root.exists():
                continue
            candidates = [root] if root.is_file() else root.rglob("*")
            for path in candidates:
                if not path.is_file() or path.suffix.lower() not in _CODE_SUFFIXES:
                    continue
                if any(part in _IGNORED_SEGMENTS for part in path.relative_to(self.root).parts):
                    continue
                yield path

    def _audit_references(
        self,
        references: Sequence[DependencyReference],
        allowed_processes: set[str],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for reference in references:
            value = reference.value.lower().replace("\\", "/")
            location = f"{reference.source_file}:{reference.line}" if reference.line else reference.source_file
            if any(f"../{repo}" in value for repo in _SOURCE_REPOSITORIES):
                findings.append(
                    Finding(
                        code="internalization.source_repository_runtime_path",
                        severity=Severity.BLOCKER,
                        summary="Production source refers to a root-workspace source repository.",
                        detail=reference.value,
                        location=location,
                    )
                )
            if reference.resolved_path and not self._inside_project(reference.resolved_path):
                resolved = Path(reference.resolved_path)
                if self.source_workspace in resolved.parents or resolved == self.source_workspace:
                    findings.append(
                        Finding(
                            code="internalization.workspace_external_path",
                            severity=Severity.BLOCKER,
                            summary="A runtime path resolves outside Zyra into the source workspace.",
                            detail=reference.resolved_path,
                            location=location,
                        )
                    )
            if reference.kind == "process":
                executable = self._process_executable(reference.value)
                if executable and executable not in allowed_processes and executable in _SOURCE_REPOSITORIES:
                    findings.append(
                        Finding(
                            code="internalization.source_process",
                            severity=Severity.BLOCKER,
                            summary="A source-repository CLI/process is used as a runtime dependency.",
                            detail=reference.value,
                            location=location,
                        )
                    )
                if reference.dynamic:
                    findings.append(
                        Finding(
                            code="internalization.dynamic_process_unresolved",
                            severity=Severity.WARNING,
                            summary="A dynamically built process command requires explicit clean-room evidence.",
                            location=location,
                        )
                    )
            if reference.kind == "dynamic_import" and reference.dynamic:
                findings.append(
                    Finding(
                        code="internalization.dynamic_import_unresolved",
                        severity=Severity.WARNING,
                        summary="A dynamic import target could not be resolved statically.",
                        location=location,
                    )
                )
        return findings

    def _audit_symlinks(self) -> list[Finding]:
        findings: list[Finding] = []
        for path in self.root.rglob("*"):
            if any(part in _IGNORED_SEGMENTS for part in path.relative_to(self.root).parts):
                continue
            if not path.is_symlink():
                continue
            try:
                resolved = path.resolve(strict=False)
            except OSError as error:
                findings.append(
                    Finding(
                        code="internalization.symlink_unresolvable",
                        severity=Severity.ERROR,
                        summary="A project symlink cannot be resolved.",
                        detail=str(error),
                        location=path.relative_to(self.root).as_posix(),
                    )
                )
                continue
            if not resolved.is_relative_to(self.root):
                findings.append(
                    Finding(
                        code="internalization.symlink_escape",
                        severity=Severity.BLOCKER,
                        summary="A project symlink resolves outside the Zyra repository.",
                        detail=str(resolved),
                        location=path.relative_to(self.root).as_posix(),
                    )
                )
        return findings

    def _audit_opaque_assets(self, allowed: set[str]) -> list[Finding]:
        findings: list[Finding] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _OPAQUE_SUFFIXES:
                continue
            relative = path.relative_to(self.root).as_posix()
            if relative.lower() in allowed:
                continue
            if any(part in _IGNORED_SEGMENTS for part in path.relative_to(self.root).parts):
                continue
            findings.append(
                Finding(
                    code="internalization.opaque_asset",
                    severity=Severity.WARNING,
                    summary="Opaque binary or archive requires a locked build/provenance decision.",
                    detail=f"bytes={path.stat().st_size}",
                    location=relative,
                )
            )
        return findings

    def _audit_source_pool_locations(self) -> list[Finding]:
        findings: list[Finding] = []
        for segment in sorted(_FORBIDDEN_POOL_SEGMENTS):
            path = self.root / segment
            if not path.exists():
                continue
            source_files = [item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in _CODE_SUFFIXES]
            if source_files:
                findings.append(
                    Finding(
                        code="internalization.source_pool_present",
                        severity=Severity.WARNING,
                        summary="A source-pool-like directory remains and must not be a runtime dependency.",
                        detail=f"source_files={len(source_files)}",
                        location=segment,
                    )
                )
        return findings

    def _audit_runtime_state_dependencies(
        self,
        references: Sequence[DependencyReference],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for reference in references:
            if reference.kind != "state_path_literal":
                continue
            value = reference.value.lower().replace("\\", "/")
            if ".cache" in value or "node_modules" in value:
                severity = Severity.ERROR if any(word in reference.source_file for word in ("runtime", "api", "worker")) else Severity.WARNING
                findings.append(
                    Finding(
                        code="internalization.cache_state_literal",
                        severity=severity,
                        summary="Runtime source contains a cache/build-state path that needs a clean start fallback.",
                        detail=reference.value,
                        location=f"{reference.source_file}:{reference.line}",
                    )
                )
            if value.endswith((".sqlite", ".sqlite3", ".db")) and "artifact" not in reference.source_file:
                findings.append(
                    Finding(
                        code="internalization.database_path_literal",
                        severity=Severity.INFO,
                        summary="Persistent database path is included in the state-custody audit.",
                        detail=reference.value,
                        location=f"{reference.source_file}:{reference.line}",
                    )
                )
        return findings

    @staticmethod
    def _audit_openclaw_forward_boundary(
        references: Sequence[DependencyReference],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for reference in references:
            value = reference.value.lower().replace("\\", "/")
            if "openclaw" not in value:
                continue
            if reference.kind in {"python_import", "script_import", "process", "source_path_literal", "path_literal"}:
                findings.append(
                    Finding(
                        code="internalization.openclaw_forward_dependency",
                        severity=Severity.BLOCKER,
                        summary="OpenClaw is excluded from all forward M1 runtime and reference dependencies.",
                        detail=reference.value,
                        location=f"{reference.source_file}:{reference.line}",
                    )
                )
        return findings

    def _audit_import_cycles(
        self,
        files: Sequence[Path],
        references: Sequence[DependencyReference],
    ) -> list[Finding]:
        module_to_file: dict[str, Path] = {}
        for path in files:
            if path.suffix not in {".py", ".pyi"}:
                continue
            relative = path.relative_to(self.root).with_suffix("")
            parts = list(relative.parts)
            if "packages" in parts or "apps" in parts:
                module_to_file[".".join(parts)] = path
                if "zyra_" in path.as_posix():
                    index = next((pos for pos, part in enumerate(parts) if part.startswith("zyra_")), None)
                    if index is not None:
                        module_to_file[".".join(parts[index:])] = path
        graph: dict[str, set[str]] = defaultdict(set)
        path_to_module = {path.relative_to(self.root).as_posix(): module for module, path in module_to_file.items()}
        for reference in references:
            if reference.kind != "python_import":
                continue
            source = path_to_module.get(reference.source_file)
            if not source:
                continue
            target = next((module for module in module_to_file if reference.value == module or reference.value.startswith(module + ".")), "")
            if target and target != source:
                graph[source].add(target)
        cycles = self._find_cycles(graph)
        return [
            Finding(
                code="internalization.import_cycle",
                severity=Severity.WARNING,
                summary="A Python package import cycle can hide runtime initialization dependencies.",
                detail=" -> ".join(cycle),
            )
            for cycle in cycles[:25]
        ]

    @staticmethod
    def _find_cycles(graph: Mapping[str, set[str]]) -> list[tuple[str, ...]]:
        cycles: set[tuple[str, ...]] = set()
        for start in graph:
            queue: deque[tuple[str, tuple[str, ...]]] = deque([(start, (start,))])
            while queue:
                node, path = queue.popleft()
                if len(path) > 12:
                    continue
                for target in graph.get(node, set()):
                    if target == start and len(path) > 1:
                        cycle = path + (start,)
                        body = cycle[:-1]
                        pivot = min(range(len(body)), key=lambda index: body[index])
                        normalized = body[pivot:] + body[:pivot] + (body[pivot],)
                        cycles.add(normalized)
                    elif target not in path:
                        queue.append((target, path + (target,)))
        return sorted(cycles)

    def _inside_project(self, value: str) -> bool:
        try:
            return Path(value).resolve(strict=False).is_relative_to(self.root)
        except OSError:
            return False

    @staticmethod
    def _process_executable(command: str) -> str:
        stripped = command.strip().strip("[]")
        if not stripped or stripped == "<dynamic>":
            return ""
        first = re.split(r"[\s,]+", stripped, maxsplit=1)[0].strip("'\"")
        return Path(first).stem.lower()
