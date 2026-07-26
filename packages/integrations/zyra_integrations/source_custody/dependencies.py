from __future__ import annotations

import json
import re
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .javascript_analyzer import JavaScriptFileAnalysis
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
    relative_path,
    section,
)
from .python_analyzer import PythonFileAnalysis
from .repository import RepositoryInventory


PYTHON_MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "setup.cfg",
        "setup.py",
        "pdm.lock",
        "poetry.lock",
        "uv.lock",
        "pipfile",
        "pipfile.lock",
    }
)
JAVASCRIPT_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "bun.lock",
        "bun.lockb",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "npm-shrinkwrap.json",
    }
)
RUST_MANIFEST_NAMES = frozenset({"cargo.toml", "cargo.lock"})
DOCKER_MANIFEST_NAMES = frozenset(
    {
        "dockerfile",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }
)
DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
    "bundledDependencies",
)
PATH_PROTOCOLS = (
    "file:",
    "link:",
    "portal:",
    "path:",
)
REMOTE_PROTOCOLS = (
    "git:",
    "git+",
    "github:",
    "http:",
    "https:",
)
FLOATING_SPECIFIERS = frozenset({"*", "latest", "next", "canary", "nightly"})
PYTHON_STANDARD_LIBRARY = frozenset(sys.stdlib_module_names)
KNOWN_IMPORT_DISTRIBUTIONS: Mapping[str, str] = {
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "google": "google",
    "jwt": "pyjwt",
    "multipart": "python-multipart",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@/_.-]*$")
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
PARENT_PATH = re.compile(
    r"(?i)(?:^|[/\\])\.\.[/\\](claude-code-best|browser-use|OpenHands|opencode|"
    r"agentscope|agent-framework|hermes-agent|langgraph|oh-my-pi|openclaw)(?:[/\\]|$)"
)


@dataclass(frozen=True, slots=True)
class Dependency:
    ecosystem: str
    package: str
    specifier: str
    scope: str
    manifest_path: str
    optional: bool = False
    development: bool = False
    resolved: bool = False
    source: str = ""

    @property
    def normalized_package(self) -> str:
        return normalize_package(self.package, self.ecosystem)

    @property
    def path_based(self) -> bool:
        lowered = self.specifier.casefold().strip()
        return lowered.startswith(PATH_PROTOCOLS) or PARENT_PATH.search(lowered) is not None

    @property
    def remote_unpinned(self) -> bool:
        lowered = self.specifier.casefold().strip()
        if not lowered.startswith(REMOTE_PROTOCOLS):
            return False
        return not re.search(r"(?:#|@)[0-9a-f]{7,64}(?:$|[?&])", lowered)

    @property
    def floating(self) -> bool:
        lowered = self.specifier.casefold().strip()
        if lowered in FLOATING_SPECIFIERS:
            return True
        if self.ecosystem == "python":
            return not any(operator in lowered for operator in ("==", "===", "@"))
        if self.ecosystem == "javascript":
            return lowered in {"", "*", "latest"} or lowered.startswith(
                ("workspace:*", "workspace:^", "workspace:~")
            )
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "package": self.package,
            "specifier": self.specifier,
            "scope": self.scope,
            "manifest_path": self.manifest_path,
            "optional": self.optional,
            "development": self.development,
            "resolved": self.resolved,
            "source": self.source,
            "path_based": self.path_based,
            "remote_unpinned": self.remote_unpinned,
            "floating": self.floating,
        }


@dataclass(frozen=True, slots=True)
class Manifest:
    path: str
    ecosystem: str
    package_name: str
    package_version: str
    private: bool
    workspace_patterns: tuple[str, ...]
    scripts: Mapping[str, str]
    dependencies: tuple[Dependency, ...]
    digest: str
    parse_error: str = ""

    @property
    def directory(self) -> str:
        parent = PurePosixPath(self.path).parent.as_posix()
        return "" if parent == "." else parent

    def dependency_map(self) -> dict[str, Dependency]:
        result: dict[str, Dependency] = {}
        for dependency in self.dependencies:
            key = dependency.normalized_package
            existing = result.get(key)
            if existing is None or (existing.development and not dependency.development):
                result[key] = dependency
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ecosystem": self.ecosystem,
            "package_name": self.package_name,
            "package_version": self.package_version,
            "private": self.private,
            "workspace_patterns": list(self.workspace_patterns),
            "scripts": dict(self.scripts),
            "dependencies": [item.to_dict() for item in self.dependencies],
            "digest": self.digest,
            "parse_error": self.parse_error,
        }


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    manifests: tuple[Manifest, ...]
    lockfiles: tuple[str, ...]
    local_python_roots: frozenset[str]
    workspace_packages: Mapping[str, str]
    python_imports: Mapping[str, tuple[str, ...]]
    javascript_imports: Mapping[str, tuple[str, ...]]

    def nearest_manifest(self, path: str, ecosystem: str) -> Manifest | None:
        candidates = [
            manifest
            for manifest in self.manifests
            if manifest.ecosystem == ecosystem
            and (
                not manifest.directory
                or path == manifest.directory
                or path.startswith(f"{manifest.directory}/")
            )
        ]
        return max(candidates, key=lambda item: len(item.directory), default=None)

    def to_summary(self) -> dict[str, Any]:
        ecosystems = Counter(manifest.ecosystem for manifest in self.manifests)
        dependencies = Counter(
            dependency.ecosystem
            for manifest in self.manifests
            for dependency in manifest.dependencies
        )
        return {
            "manifest_count": len(self.manifests),
            "manifest_ecosystems": dict(sorted(ecosystems.items())),
            "dependency_count": sum(dependencies.values()),
            "dependency_ecosystems": dict(sorted(dependencies.items())),
            "lockfile_count": len(self.lockfiles),
            "workspace_package_count": len(self.workspace_packages),
            "local_python_root_count": len(self.local_python_roots),
            "python_import_root_count": len(self.python_imports),
            "javascript_import_root_count": len(self.javascript_imports),
        }


class DependencyAuditor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def audit(
        self,
        inventory: RepositoryInventory,
        python: Sequence[PythonFileAnalysis],
        javascript: Sequence[JavaScriptFileAnalysis],
    ) -> tuple[DependencyGraph, AuditSection]:
        manifests: list[Manifest] = []
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        lockfiles: list[str] = []
        for record in inventory.files:
            name = PurePosixPath(record.path).name.casefold()
            if name in PYTHON_MANIFEST_NAMES:
                if name in {"pyproject.toml", "setup.cfg", "setup.py"}:
                    manifest = self._parse_python_manifest(record.path, record.digest)
                    manifests.append(manifest)
                    if manifest.parse_error:
                        findings.append(self._parse_finding(manifest))
                else:
                    lockfiles.append(record.path)
                    if name.startswith("requirements"):
                        manifest = self._parse_requirements(record.path, record.digest)
                        manifests.append(manifest)
            elif name in JAVASCRIPT_MANIFEST_NAMES:
                if name == "package.json":
                    manifest = self._parse_package_json(record.path, record.digest)
                    manifests.append(manifest)
                    if manifest.parse_error:
                        findings.append(self._parse_finding(manifest))
                else:
                    lockfiles.append(record.path)
            elif name in RUST_MANIFEST_NAMES:
                if name == "cargo.toml":
                    manifest = self._parse_cargo_manifest(record.path, record.digest)
                    manifests.append(manifest)
                    if manifest.parse_error:
                        findings.append(self._parse_finding(manifest))
                else:
                    lockfiles.append(record.path)
        local_python = self._local_python_roots(inventory)
        workspace_packages = {
            manifest.package_name: manifest.directory
            for manifest in manifests
            if manifest.ecosystem == "javascript" and manifest.package_name
        }
        python_imports = self._python_import_index(python)
        javascript_imports = self._javascript_import_index(javascript)
        graph = DependencyGraph(
            manifests=tuple(sorted(manifests, key=lambda item: item.path)),
            lockfiles=tuple(sorted(lockfiles)),
            local_python_roots=frozenset(local_python),
            workspace_packages=dict(sorted(workspace_packages.items())),
            python_imports=python_imports,
            javascript_imports=javascript_imports,
        )
        if self.switches.dependencies:
            findings.extend(self._manifest_findings(graph))
            findings.extend(self._python_usage_findings(graph, python))
            findings.extend(self._javascript_usage_findings(graph, javascript))
            findings.extend(self._lockfile_findings(graph))
        for manifest in graph.manifests:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.MANIFEST,
                    path=manifest.path,
                    excerpt_digest=manifest.digest,
                    attributes={
                        "ecosystem": manifest.ecosystem,
                        "package": manifest.package_name,
                        "dependencies": len(manifest.dependencies),
                    },
                )
            )
        for path in graph.lockfiles:
            record = inventory.file(path)
            evidence.append(
                Evidence(
                    kind=EvidenceKind.LOCKFILE,
                    path=path,
                    excerpt_digest=record.digest if record else "",
                )
            )
        return graph, section(
            "dependencies",
            metrics=graph.to_summary(),
            findings=findings,
            evidence=evidence,
        )

    def _parse_python_manifest(self, path: str, digest: str) -> Manifest:
        if path.casefold().endswith("pyproject.toml"):
            try:
                payload = tomllib.loads(
                    (self.project_root / path).read_text(encoding="utf-8")
                )
                project = payload.get("project") or {}
                if not isinstance(project, Mapping):
                    raise ValueError("[project] must be a table")
                dependencies: list[Dependency] = []
                for item in project.get("dependencies") or []:
                    dependencies.append(
                        _python_dependency(item, "project.dependencies", path)
                    )
                optional = project.get("optional-dependencies") or {}
                if not isinstance(optional, Mapping):
                    raise ValueError("[project.optional-dependencies] must be a table")
                for group, requirements in optional.items():
                    if not isinstance(requirements, list):
                        raise ValueError(
                            f"optional dependency group {group!r} must be an array"
                        )
                    for item in requirements:
                        dependencies.append(
                            _python_dependency(
                                item,
                                f"project.optional-dependencies.{group}",
                                path,
                                optional=True,
                                development=str(group).casefold()
                                in {"dev", "test", "tests", "lint", "docs"},
                            )
                        )
                scripts = project.get("scripts") or {}
                if not isinstance(scripts, Mapping):
                    raise ValueError("[project.scripts] must be a table")
                return Manifest(
                    path=path,
                    ecosystem="python",
                    package_name=str(project.get("name") or ""),
                    package_version=str(project.get("version") or ""),
                    private=False,
                    workspace_patterns=(),
                    scripts={str(key): str(value) for key, value in scripts.items()},
                    dependencies=tuple(dependencies),
                    digest=digest,
                )
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
                return _invalid_manifest(path, "python", digest, exc)
        if path.casefold().endswith("setup.cfg"):
            return self._parse_setup_cfg(path, digest)
        return Manifest(
            path=path,
            ecosystem="python",
            package_name="",
            package_version="",
            private=False,
            workspace_patterns=(),
            scripts={},
            dependencies=(),
            digest=digest,
        )

    def _parse_setup_cfg(self, path: str, digest: str) -> Manifest:
        try:
            source = (self.project_root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _invalid_manifest(path, "python", digest, exc)
        dependencies: list[Dependency] = []
        active = False
        for raw_line in source.splitlines():
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                active = line.casefold() == "[options]"
                continue
            if active and line.casefold().startswith("install_requires"):
                _, _, initial = line.partition("=")
                if initial.strip():
                    dependencies.append(
                        _python_dependency(initial.strip(), "options.install_requires", path)
                    )
                continue
            if active and raw_line[:1].isspace() and line and not line.startswith(("#", ";")):
                try:
                    dependencies.append(
                        _python_dependency(line, "options.install_requires", path)
                    )
                except ValueError:
                    continue
        return Manifest(
            path=path,
            ecosystem="python",
            package_name="",
            package_version="",
            private=False,
            workspace_patterns=(),
            scripts={},
            dependencies=tuple(dependencies),
            digest=digest,
        )

    def _parse_requirements(self, path: str, digest: str) -> Manifest:
        dependencies: list[Dependency] = []
        try:
            source = (self.project_root / path).read_text(encoding="utf-8")
            for line_number, raw_line in enumerate(source.splitlines(), start=1):
                line = raw_line.split("#", 1)[0].strip()
                if not line:
                    continue
                if line.startswith(("-r", "--requirement", "-c", "--constraint")):
                    dependencies.append(
                        Dependency(
                            ecosystem="python",
                            package=f"include@{line_number}",
                            specifier=line,
                            scope="requirements-include",
                            manifest_path=path,
                            source="include",
                        )
                    )
                elif line.startswith(("-e", "--editable")):
                    dependencies.append(
                        Dependency(
                            ecosystem="python",
                            package=f"editable@{line_number}",
                            specifier=line,
                            scope="requirements",
                            manifest_path=path,
                            source="editable",
                        )
                    )
                else:
                    dependencies.append(
                        _python_dependency(line, "requirements", path)
                    )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return _invalid_manifest(path, "python", digest, exc)
        return Manifest(
            path=path,
            ecosystem="python",
            package_name="",
            package_version="",
            private=False,
            workspace_patterns=(),
            scripts={},
            dependencies=tuple(dependencies),
            digest=digest,
        )

    def _parse_package_json(self, path: str, digest: str) -> Manifest:
        try:
            payload = json.loads(
                (self.project_root / path).read_text(encoding="utf-8")
            )
            if not isinstance(payload, Mapping):
                raise ValueError("package.json root must be an object")
            dependencies: list[Dependency] = []
            for section_name in DEPENDENCY_SECTIONS:
                block = payload.get(section_name) or {}
                if isinstance(block, list) and section_name == "bundledDependencies":
                    block = {str(item): "bundled" for item in block}
                if not isinstance(block, Mapping):
                    raise ValueError(f"{section_name} must be an object")
                for package, specifier in block.items():
                    dependencies.append(
                        Dependency(
                            ecosystem="javascript",
                            package=str(package),
                            specifier=str(specifier),
                            scope=section_name,
                            manifest_path=path,
                            optional=section_name == "optionalDependencies",
                            development=section_name == "devDependencies",
                            source="package.json",
                        )
                    )
            workspaces = payload.get("workspaces") or []
            if isinstance(workspaces, Mapping):
                workspaces = workspaces.get("packages") or []
            if not isinstance(workspaces, list):
                raise ValueError("workspaces must be an array or packages object")
            scripts = payload.get("scripts") or {}
            if not isinstance(scripts, Mapping):
                raise ValueError("scripts must be an object")
            return Manifest(
                path=path,
                ecosystem="javascript",
                package_name=str(payload.get("name") or ""),
                package_version=str(payload.get("version") or ""),
                private=bool(payload.get("private")),
                workspace_patterns=tuple(str(item) for item in workspaces),
                scripts={str(key): str(value) for key, value in scripts.items()},
                dependencies=tuple(dependencies),
                digest=digest,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return _invalid_manifest(path, "javascript", digest, exc)

    def _parse_cargo_manifest(self, path: str, digest: str) -> Manifest:
        try:
            payload = tomllib.loads(
                (self.project_root / path).read_text(encoding="utf-8")
            )
            package = payload.get("package") or {}
            dependencies: list[Dependency] = []
            for section_name in (
                "dependencies",
                "dev-dependencies",
                "build-dependencies",
            ):
                block = payload.get(section_name) or {}
                if not isinstance(block, Mapping):
                    raise ValueError(f"[{section_name}] must be a table")
                for name, specifier in block.items():
                    if isinstance(specifier, Mapping):
                        rendered = json.dumps(
                            dict(specifier), sort_keys=True, separators=(",", ":")
                        )
                    else:
                        rendered = str(specifier)
                    dependencies.append(
                        Dependency(
                            ecosystem="rust",
                            package=str(name),
                            specifier=rendered,
                            scope=section_name,
                            manifest_path=path,
                            development=section_name == "dev-dependencies",
                            source="Cargo.toml",
                        )
                    )
            return Manifest(
                path=path,
                ecosystem="rust",
                package_name=str(package.get("name") or ""),
                package_version=str(package.get("version") or ""),
                private=bool(package.get("publish") is False),
                workspace_patterns=tuple(
                    str(item)
                    for item in (payload.get("workspace") or {}).get("members", [])
                ),
                scripts={},
                dependencies=tuple(dependencies),
                digest=digest,
            )
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
            return _invalid_manifest(path, "rust", digest, exc)

    def _local_python_roots(
        self, inventory: RepositoryInventory
    ) -> set[str]:
        roots: set[str] = set()
        for item in inventory.files:
            if item.suffix not in {".py", ".pyi"}:
                continue
            parts = PurePosixPath(item.path).parts
            for index, part in enumerate(parts):
                if part.startswith("zyra_"):
                    roots.add(part)
                    break
                if part == "apps" and index + 2 < len(parts):
                    candidate = parts[index + 2]
                    if candidate.startswith("zyra_"):
                        roots.add(candidate)
        return roots

    def _python_import_index(
        self, analyses: Sequence[PythonFileAnalysis]
    ) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, set[str]] = defaultdict(set)
        for analysis in analyses:
            for imported in analysis.imports:
                if not imported.module or imported.module.startswith("."):
                    continue
                root = imported.module.split(".", 1)[0]
                grouped[root].add(analysis.path)
        return {
            key: tuple(sorted(paths))
            for key, paths in sorted(grouped.items(), key=lambda item: item[0].casefold())
        }

    def _javascript_import_index(
        self, analyses: Sequence[JavaScriptFileAnalysis]
    ) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, set[str]] = defaultdict(set)
        for analysis in analyses:
            for imported in analysis.imports:
                specifier = imported.specifier
                if not specifier or specifier.startswith((".", "/", "#")):
                    continue
                root = _javascript_package_root(specifier)
                grouped[root].add(analysis.path)
        return {
            key: tuple(sorted(paths))
            for key, paths in sorted(grouped.items(), key=lambda item: item[0].casefold())
        }

    def _parse_finding(self, manifest: Manifest) -> Finding:
        return finding(
            "dependency_manifest_parse_failed",
            f"Dependency manifest cannot be parsed: {manifest.parse_error}",
            "dependencies",
            severity=Severity.BLOCKER,
            path=manifest.path,
            disposition=Disposition.BLOCK_RELEASE,
            remediation="Repair the manifest before freezing dependencies.",
            default_path_impact="Release dependency graph is incomplete.",
            attributes={"ecosystem": manifest.ecosystem},
        )

    def _manifest_findings(self, graph: DependencyGraph) -> list[Finding]:
        findings: list[Finding] = []
        for manifest in graph.manifests:
            for dependency in manifest.dependencies:
                if dependency.path_based:
                    parent = PARENT_PATH.search(dependency.specifier)
                    findings.append(
                        finding(
                            "path_dependency_declared",
                            "Manifest contains a filesystem path dependency.",
                            "dependencies",
                            severity=Severity.BLOCKER,
                            source_repo=parent.group(1) if parent else "",
                            path=manifest.path,
                            disposition=Disposition.BLOCK_RELEASE,
                            remediation="Internalize source or use a locked registry/workspace package.",
                            default_path_impact="Clean install depends on external filesystem state.",
                            attributes={
                                "package": dependency.package,
                                "specifier": dependency.specifier,
                            },
                        )
                    )
                if dependency.remote_unpinned:
                    findings.append(
                        finding(
                            "remote_dependency_unpinned",
                            "Remote Git/HTTP dependency is not pinned to an immutable revision.",
                            "dependencies",
                            severity=Severity.BLOCKER,
                            path=manifest.path,
                            disposition=Disposition.BLOCK_RELEASE,
                            remediation="Pin the dependency to an immutable commit and checksum.",
                            default_path_impact="Clean builds can change without a Zyra commit.",
                            attributes={"package": dependency.package},
                        )
                    )
                if dependency.floating and not dependency.development:
                    findings.append(
                        finding(
                            "production_dependency_floating",
                            "Production dependency has a floating or range-only specifier.",
                            "dependencies",
                            severity=Severity.ERROR,
                            path=manifest.path,
                            disposition=Disposition.DECLARE,
                            remediation="Ensure the committed lockfile resolves an immutable version.",
                            default_path_impact="Manifest alone does not reproduce the release.",
                            attributes={
                                "package": dependency.package,
                                "specifier": dependency.specifier,
                            },
                        )
                    )
                if dependency.normalized_package == "openclaw":
                    findings.append(
                        finding(
                            "openclaw_manifest_dependency",
                            "Manifest declares the forward-excluded OpenClaw package.",
                            "dependencies",
                            severity=Severity.BLOCKER,
                            source_repo="openclaw",
                            path=manifest.path,
                            disposition=Disposition.REMOVE,
                            remediation="Remove the dependency and use the selected Zyra owner.",
                            default_path_impact="Release would reintroduce excluded source.",
                        )
                    )
            for script_name, command in manifest.scripts.items():
                parent = PARENT_PATH.search(command.replace("\\", "/"))
                if parent:
                    findings.append(
                        finding(
                            "manifest_script_parent_source_path",
                            "Package script invokes a sibling source repository.",
                            "dependencies",
                            severity=Severity.BLOCKER,
                            source_repo=parent.group(1),
                            path=manifest.path,
                            disposition=Disposition.BLOCK_RELEASE,
                            remediation="Point scripts at Zyra-owned package paths.",
                            default_path_impact="Build/test/release depends on workspace layout.",
                            attributes={"script": script_name},
                        )
                    )
        return findings

    def _python_usage_findings(
        self,
        graph: DependencyGraph,
        analyses: Sequence[PythonFileAnalysis],
    ) -> list[Finding]:
        findings: list[Finding] = []
        root_manifest = next(
            (
                manifest
                for manifest in graph.manifests
                if manifest.ecosystem == "python" and manifest.path == "pyproject.toml"
            ),
            None,
        )
        declared = (
            set(root_manifest.dependency_map()) if root_manifest is not None else set()
        )
        for root, paths in graph.python_imports.items():
            normalized = normalize_package(
                KNOWN_IMPORT_DISTRIBUTIONS.get(root, root), "python"
            )
            if (
                root in PYTHON_STANDARD_LIBRARY
                or root in graph.local_python_roots
                or root == "__future__"
                or normalized in declared
            ):
                continue
            production_paths = [
                path
                for path in paths
                if path.startswith(("apps/", "packages/"))
                and "/test/" not in path.casefold()
                and "/tests/" not in path.casefold()
            ]
            if not production_paths:
                continue
            findings.append(
                finding(
                    "python_dependency_undeclared",
                    f"Python production source imports undeclared package root {root!r}.",
                    "dependencies",
                    severity=Severity.BLOCKER,
                    path=production_paths[0],
                    disposition=Disposition.DECLARE,
                    remediation="Declare and lock the direct runtime dependency.",
                    default_path_impact="Clean install may rely on transitive or ambient packages.",
                    attributes={
                        "import_root": root,
                        "expected_distribution": normalized,
                        "paths": production_paths[:20],
                        "path_count": len(production_paths),
                    },
                )
            )
        return findings

    def _javascript_usage_findings(
        self,
        graph: DependencyGraph,
        analyses: Sequence[JavaScriptFileAnalysis],
    ) -> list[Finding]:
        findings: list[Finding] = []
        node_builtins = {
            "assert",
            "buffer",
            "child_process",
            "crypto",
            "events",
            "fs",
            "http",
            "https",
            "module",
            "net",
            "os",
            "path",
            "perf_hooks",
            "process",
            "querystring",
            "readline",
            "stream",
            "string_decoder",
            "timers",
            "tls",
            "tty",
            "url",
            "util",
            "v8",
            "vm",
            "worker_threads",
            "zlib",
            "bun",
        }
        for package, paths in graph.javascript_imports.items():
            if (
                package.startswith(("node:", "bun:"))
                or package.removeprefix("node:") in node_builtins
            ):
                continue
            if package in graph.workspace_packages:
                continue
            undeclared_paths: list[str] = []
            for path in paths:
                lowered_path = path.casefold()
                if (
                    "/test/" in lowered_path
                    or "/tests/" in lowered_path
                    or lowered_path.endswith(
                        (".test.ts", ".test.tsx", ".test.js", ".test.jsx")
                    )
                ):
                    continue
                manifest = graph.nearest_manifest(path, "javascript")
                if manifest is None:
                    undeclared_paths.append(path)
                    continue
                declared = manifest.dependency_map()
                root_manifest = graph.nearest_manifest("", "javascript")
                root_declared = (
                    root_manifest.dependency_map() if root_manifest is not None else {}
                )
                if (
                    normalize_package(package, "javascript") not in declared
                    and normalize_package(package, "javascript") not in root_declared
                ):
                    undeclared_paths.append(path)
            if undeclared_paths:
                findings.append(
                    finding(
                        "javascript_dependency_undeclared",
                        f"JavaScript/TypeScript imports undeclared package {package!r}.",
                        "dependencies",
                        severity=Severity.BLOCKER,
                        path=undeclared_paths[0],
                        disposition=Disposition.DECLARE,
                        remediation="Declare the package in the owning workspace manifest and lock it.",
                        default_path_impact="Clean install can depend on hoisted ambient packages.",
                        attributes={
                            "package": package,
                            "paths": undeclared_paths[:20],
                            "path_count": len(undeclared_paths),
                        },
                    )
                )
        return findings

    def _lockfile_findings(self, graph: DependencyGraph) -> list[Finding]:
        findings: list[Finding] = []
        ecosystems = {manifest.ecosystem for manifest in graph.manifests}
        lock_names = {PurePosixPath(path).name.casefold() for path in graph.lockfiles}
        if "javascript" in ecosystems and not lock_names.intersection(
            {"bun.lock", "bun.lockb", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
        ):
            findings.append(
                finding(
                    "javascript_lockfile_missing",
                    "JavaScript workspace has no committed lockfile.",
                    "dependencies",
                    severity=Severity.BLOCKER,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Commit one package-manager lockfile.",
                    default_path_impact="Clean install is not reproducible.",
                )
            )
        root_python = next(
            (
                manifest
                for manifest in graph.manifests
                if manifest.ecosystem == "python" and manifest.path == "pyproject.toml"
            ),
            None,
        )
        if root_python and root_python.dependencies and not lock_names.intersection(
            {"uv.lock", "poetry.lock", "pdm.lock", "pipfile.lock", "requirements.txt"}
        ):
            findings.append(
                finding(
                    "python_lockfile_missing",
                    "Python project declares dependencies without a frozen lock/requirements file.",
                    "dependencies",
                    severity=Severity.ERROR,
                    path="pyproject.toml",
                    disposition=Disposition.DECLARE,
                    remediation="Generate a reproducible Python lock for release packaging.",
                    default_path_impact="Clean install can resolve different transitive versions.",
                )
            )
        if "rust" in ecosystems and "cargo.lock" not in lock_names:
            findings.append(
                finding(
                    "rust_lockfile_missing",
                    "Rust package has no committed Cargo.lock.",
                    "dependencies",
                    severity=Severity.ERROR,
                    disposition=Disposition.DECLARE,
                    remediation="Commit Cargo.lock for release binaries.",
                    default_path_impact="Native build is not reproducible.",
                )
            )
        return findings


def _invalid_manifest(
    path: str, ecosystem: str, digest: str, error: Exception
) -> Manifest:
    return Manifest(
        path=path,
        ecosystem=ecosystem,
        package_name="",
        package_version="",
        private=False,
        workspace_patterns=(),
        scripts={},
        dependencies=(),
        digest=digest,
        parse_error=str(error),
    )


def _python_dependency(
    raw: Any,
    scope: str,
    manifest_path: str,
    *,
    optional: bool = False,
    development: bool = False,
) -> Dependency:
    if not isinstance(raw, str):
        raise ValueError("Python dependency must be a PEP 508 string")
    match = REQUIREMENT_NAME.match(raw)
    if not match:
        raise ValueError(f"invalid Python dependency: {raw!r}")
    return Dependency(
        ecosystem="python",
        package=match.group(1),
        specifier=raw,
        scope=scope,
        manifest_path=manifest_path,
        optional=optional,
        development=development,
        source="PEP508",
    )


def normalize_package(package: str, ecosystem: str) -> str:
    candidate = package.strip().casefold()
    if ecosystem == "python":
        return re.sub(r"[-_.]+", "-", candidate)
    return candidate


def _javascript_package_root(specifier: str) -> str:
    normalized = specifier.replace("\\", "/")
    if normalized.startswith("@"):
        parts = normalized.split("/")
        return "/".join(parts[:2])
    return normalized.split("/", 1)[0]
