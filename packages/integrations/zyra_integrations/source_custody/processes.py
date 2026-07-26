from __future__ import annotations

import fnmatch
import json
import re
import shlex
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .dependencies import DependencyGraph
from .javascript_analyzer import (
    INSTALLERS,
    PORT_CALLEES,
    JavaScriptFileAnalysis,
    is_process_call,
)
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
    identity,
    normalize_paths,
    relative_path,
    section,
    stable_digest,
    text,
)
from .python_analyzer import PORT_CALLS, PROCESS_CALLS, PythonFileAnalysis
from .repository import RepositoryInventory


PROCESS_CATALOG_SCHEMA = "zyra.process-custody-catalog/v1"
PROCESS_KINDS = frozenset(
    {
        "runtime",
        "sidecar",
        "worker",
        "tool",
        "build",
        "test",
        "healthcheck",
        "mcp_server",
        "plugin_host",
        "deployment",
    }
)
TRANSPORTS = frozenset(
    {
        "stdio",
        "http",
        "https",
        "sse",
        "websocket",
        "ipc",
        "filesystem",
        "none",
    }
)
PROFILE_MODES = frozenset({"required", "optional", "development", "test"})
PORT_PATTERN = re.compile(r"(?<!\d)([1-9][0-9]{1,4})(?!\d)")
PARENT_SOURCE = re.compile(
    r"(?i)(?:^|[/\\])\.\.[/\\](claude-code-best|browser-use|OpenHands|opencode|"
    r"agentscope|agent-framework|hermes-agent|langgraph|oh-my-pi|openclaw)(?:[/\\]|$)"
)
DOCKER_FROM = re.compile(r"^\s*FROM\s+([^\s]+)", re.IGNORECASE | re.MULTILINE)
DOCKER_CONTEXT = re.compile(
    r"(?im)^\s*(?:context|dockerfile)\s*:\s*['\"]?([^'\"#\r\n]+)"
)
DOCKER_RUN_DOWNLOAD = re.compile(
    r"(?im)^\s*RUN\s+.*(?:curl|wget|Invoke-WebRequest|pip\s+install|npm\s+install|"
    r"bun\s+install|cargo\s+install).*$"
)


@dataclass(frozen=True, slots=True)
class ProcessProfile:
    profile_id: str
    kind: str
    owner: str
    executable: str
    command_prefixes: tuple[tuple[str, ...], ...]
    callsite_patterns: tuple[str, ...]
    modes: tuple[str, ...]
    transport: str
    ports: tuple[int, ...]
    environment: tuple[str, ...]
    package: str
    entrypoint: str
    healthcheck: str
    source_role_entry_ids: tuple[str, ...]
    default_reachable: bool
    external: bool
    reason: str

    @classmethod
    def parse(cls, raw: Any) -> "ProcessProfile":
        if not isinstance(raw, Mapping):
            raise ValueError("process profile must be an object")
        profile_id = identity(raw.get("profile_id"), field_name="profile_id")
        kind = identity(raw.get("kind"), field_name="process kind")
        if kind not in PROCESS_KINDS:
            raise ValueError(f"unsupported process kind: {kind}")
        executable = text(raw.get("executable"), field_name="executable")
        if PARENT_SOURCE.search(executable):
            raise ValueError("process executable cannot point to a parent source repository")
        prefixes_raw = raw.get("command_prefixes") or []
        if not isinstance(prefixes_raw, list):
            raise ValueError("command_prefixes must be an array")
        prefixes: list[tuple[str, ...]] = []
        for prefix in prefixes_raw:
            if not isinstance(prefix, list) or not prefix:
                raise ValueError("each command prefix must be a non-empty argv array")
            values = tuple(text(item, field_name="command prefix") for item in prefix)
            prefixes.append(values)
        patterns = normalize_paths(
            raw.get("callsite_patterns"),
            field_name="callsite_patterns",
            allow_empty=False,
        )
        modes = tuple(
            identity(item, field_name="process mode")
            for item in (raw.get("modes") or [])
        )
        if not modes or any(mode not in PROFILE_MODES for mode in modes):
            raise ValueError("modes must contain supported process modes")
        transport = identity(raw.get("transport"), field_name="transport")
        if transport not in TRANSPORTS:
            raise ValueError(f"unsupported process transport: {transport}")
        raw_ports = raw.get("ports") or []
        if not isinstance(raw_ports, list):
            raise ValueError("ports must be an array")
        ports = tuple(sorted({_port(item) for item in raw_ports}))
        environment = tuple(
            sorted(
                {
                    identity(item, field_name="environment variable")
                    for item in (raw.get("environment") or [])
                }
            )
        )
        source_ids = tuple(
            sorted(
                {
                    identity(item, field_name="source role entry id")
                    for item in (raw.get("source_role_entry_ids") or [])
                }
            )
        )
        reason = text(raw.get("reason"), field_name="process reason")
        if len(reason) < 24:
            raise ValueError("process reason is too short")
        return cls(
            profile_id=profile_id,
            kind=kind,
            owner=text(raw.get("owner"), field_name="process owner"),
            executable=executable.replace("\\", "/"),
            command_prefixes=tuple(prefixes),
            callsite_patterns=patterns,
            modes=modes,
            transport=transport,
            ports=ports,
            environment=environment,
            package=text(raw.get("package"), field_name="package", allow_empty=True),
            entrypoint=text(
                raw.get("entrypoint"), field_name="entrypoint", allow_empty=True
            ),
            healthcheck=text(
                raw.get("healthcheck"), field_name="healthcheck", allow_empty=True
            ),
            source_role_entry_ids=source_ids,
            default_reachable=bool(raw.get("default_reachable")),
            external=bool(raw.get("external")),
            reason=reason,
        )

    def matches(self, use: "ProcessUse") -> bool:
        if not any(
            fnmatch.fnmatch(use.path, pattern)
            or fnmatch.fnmatch(use.path, f"{pattern}/**")
            for pattern in self.callsite_patterns
        ):
            return False
        executable = PurePosixPath(use.executable.replace("\\", "/")).name.casefold()
        expected = PurePosixPath(self.executable).name.casefold()
        if executable and expected not in {"", "*"} and executable != expected:
            return False
        if self.command_prefixes and use.argv:
            return any(
                tuple(item.casefold() for item in use.argv[: len(prefix)])
                == tuple(item.casefold() for item in prefix)
                for prefix in self.command_prefixes
            )
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "kind": self.kind,
            "owner": self.owner,
            "executable": self.executable,
            "command_prefixes": [list(item) for item in self.command_prefixes],
            "callsite_patterns": list(self.callsite_patterns),
            "modes": list(self.modes),
            "transport": self.transport,
            "ports": list(self.ports),
            "environment": list(self.environment),
            "package": self.package,
            "entrypoint": self.entrypoint,
            "healthcheck": self.healthcheck,
            "source_role_entry_ids": list(self.source_role_entry_ids),
            "default_reachable": self.default_reachable,
            "external": self.external,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProcessUse:
    path: str
    line: int
    language: str
    callee: str
    executable: str
    argv: tuple[str, ...]
    shell: bool
    literal: bool
    port: int
    source: str

    @property
    def identity(self) -> str:
        return stable_digest(
            {
                "path": self.path,
                "line": self.line,
                "callee": self.callee,
                "executable": self.executable,
                "argv": self.argv,
                "port": self.port,
            }
        ).split(":", 1)[1][:20]

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "path": self.path,
            "line": self.line,
            "language": self.language,
            "callee": self.callee,
            "executable": self.executable,
            "argv": list(self.argv),
            "shell": self.shell,
            "literal": self.literal,
            "port": self.port,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ProcessCatalog:
    profiles: tuple[ProcessProfile, ...]
    source_path: str
    digest: str

    def match(self, use: ProcessUse) -> tuple[ProcessProfile, ...]:
        return tuple(profile for profile in self.profiles if profile.matches(use))

    def profile(self, profile_id: str) -> ProcessProfile | None:
        return next(
            (item for item in self.profiles if item.profile_id == profile_id),
            None,
        )


class ProcessAuditor:
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
        dependencies: DependencyGraph,
        python: Sequence[PythonFileAnalysis],
        javascript: Sequence[JavaScriptFileAnalysis],
        catalog_path: str | Path,
    ) -> tuple[ProcessCatalog | None, tuple[ProcessUse, ...], AuditSection]:
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        catalog, load_findings = self._load_catalog(catalog_path)
        findings.extend(load_findings)
        uses: list[ProcessUse] = []
        uses.extend(self._python_uses(python))
        uses.extend(self._javascript_uses(javascript))
        uses.extend(self._script_uses(dependencies))
        uses.extend(self._config_uses(inventory))
        if self.switches.processes and catalog is not None:
            findings.extend(self._profile_findings(catalog))
            findings.extend(self._use_findings(catalog, uses))
            findings.extend(self._port_findings(catalog, uses))
            findings.extend(self._docker_findings(inventory))
            findings.extend(self._mcp_plugin_findings(inventory, catalog))
        if catalog is not None:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    path=catalog.source_path,
                    excerpt_digest=catalog.digest,
                    attributes={"profiles": len(catalog.profiles)},
                )
            )
        for use in uses:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.PROCESS,
                    path=use.path,
                    line=use.line,
                    attributes={
                        "identity": use.identity,
                        "executable": use.executable,
                        "callee": use.callee,
                        "port": use.port,
                    },
                )
            )
        matched = (
            sum(bool(catalog.match(use)) for use in uses) if catalog is not None else 0
        )
        return catalog, tuple(uses), section(
            "processes",
            metrics={
                "catalog_loaded": catalog is not None,
                "profiles": len(catalog.profiles) if catalog else 0,
                "uses": len(uses),
                "matched_uses": matched,
                "unmatched_uses": len(uses) - matched,
                "literal_uses": sum(use.literal for use in uses),
                "shell_uses": sum(use.shell for use in uses),
                "listener_uses": sum(bool(use.port) for use in uses),
                "use_sources": dict(Counter(use.source for use in uses)),
            },
            findings=findings,
            evidence=evidence,
        )

    def _load_catalog(
        self, path: str | Path
    ) -> tuple[ProcessCatalog | None, list[Finding]]:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        candidate = candidate.resolve(strict=False)
        display = relative_path(self.project_root, candidate)
        if not candidate.exists():
            return None, [
                finding(
                    "process_catalog_missing",
                    "Process custody catalog is missing.",
                    "processes",
                    severity=Severity.BLOCKER,
                    path=display,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Declare every release/build/test/sidecar process profile.",
                    default_path_impact="Release process graph cannot be validated.",
                )
            ]
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, [
                finding(
                    "process_catalog_unreadable",
                    f"Process catalog cannot be decoded: {exc}",
                    "processes",
                    severity=Severity.BLOCKER,
                    path=display,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Repair the UTF-8 JSON process catalog.",
                    default_path_impact="Release process graph cannot be validated.",
                )
            ]
        findings: list[Finding] = []
        if not isinstance(payload, Mapping) or payload.get("schema") != PROCESS_CATALOG_SCHEMA:
            findings.append(
                finding(
                    "process_catalog_schema_invalid",
                    f"Process catalog schema must be {PROCESS_CATALOG_SCHEMA}.",
                    "processes",
                    severity=Severity.BLOCKER,
                    path=display,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Use the supported process-custody schema.",
                    default_path_impact="Process declarations cannot be normalized.",
                )
            )
            payload = payload if isinstance(payload, Mapping) else {}
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            findings.append(
                finding(
                    "process_profiles_invalid",
                    "Process profiles must be an array.",
                    "processes",
                    severity=Severity.BLOCKER,
                    path=display,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Add a profiles array.",
                    default_path_impact="No process use can be matched.",
                )
            )
            raw_profiles = []
        profiles: list[ProcessProfile] = []
        for index, raw in enumerate(raw_profiles):
            try:
                profiles.append(ProcessProfile.parse(raw))
            except (TypeError, ValueError) as exc:
                findings.append(
                    finding(
                        "process_profile_invalid",
                        f"Process profile {index} is invalid: {exc}",
                        "processes",
                        severity=Severity.BLOCKER,
                        path=display,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Correct the process declaration.",
                        default_path_impact="A runtime/build process remains undeclared.",
                        attributes={"index": index},
                    )
                )
        normalized = {
            "schema": PROCESS_CATALOG_SCHEMA,
            "profiles": [profile.to_dict() for profile in profiles],
        }
        return (
            ProcessCatalog(
                profiles=tuple(profiles),
                source_path=display,
                digest=stable_digest(normalized),
            ),
            findings,
        )

    def _python_uses(
        self, analyses: Sequence[PythonFileAnalysis]
    ) -> list[ProcessUse]:
        uses: list[ProcessUse] = []
        for analysis in analyses:
            for call in analysis.calls:
                if call.qualified_name in PROCESS_CALLS:
                    argv = tuple(_flatten_python_literals(call.literal_arguments))
                    executable = argv[0] if argv else ""
                    shell = (
                        call.keyword_arguments.get("shell", "") in {"True", "true", "1"}
                        or call.qualified_name
                        in {
                            "os.system",
                            "os.popen",
                            "asyncio.create_subprocess_shell",
                        }
                    )
                    uses.append(
                        ProcessUse(
                            path=analysis.path,
                            line=call.line,
                            language="python",
                            callee=call.qualified_name,
                            executable=executable,
                            argv=argv,
                            shell=shell,
                            literal=bool(argv),
                            port=0,
                            source="python_call",
                        )
                    )
                if call.qualified_name in PORT_CALLS:
                    port = _first_port(
                        str(value)
                        for value in (
                            *call.literal_arguments,
                            *call.keyword_arguments.values(),
                        )
                    )
                    uses.append(
                        ProcessUse(
                            path=analysis.path,
                            line=call.line,
                            language="python",
                            callee=call.qualified_name,
                            executable="",
                            argv=(),
                            shell=False,
                            literal=bool(port),
                            port=port,
                            source="python_listener",
                        )
                    )
        return uses

    def _javascript_uses(
        self, analyses: Sequence[JavaScriptFileAnalysis]
    ) -> list[ProcessUse]:
        uses: list[ProcessUse] = []
        for analysis in analyses:
            for call in analysis.calls:
                tail = call.callee.rsplit(".", 1)[-1]
                if is_process_call(analysis, call):
                    argv = _javascript_argv(call.literal_arguments)
                    executable = argv[0] if argv else ""
                    shell = tail in {"exec", "execSync", "execaCommand", "$"} or (
                        "shell" in call.object_keys
                    )
                    uses.append(
                        ProcessUse(
                            path=analysis.path,
                            line=call.line,
                            language="javascript",
                            callee=call.callee,
                            executable=executable,
                            argv=argv,
                            shell=shell,
                            literal=bool(argv),
                            port=0,
                            source="javascript_call",
                        )
                    )
                if call.callee in PORT_CALLEES or tail in PORT_CALLEES:
                    port = _first_port(call.literal_arguments)
                    uses.append(
                        ProcessUse(
                            path=analysis.path,
                            line=call.line,
                            language="javascript",
                            callee=call.callee,
                            executable="",
                            argv=(),
                            shell=False,
                            literal=bool(port),
                            port=port,
                            source="javascript_listener",
                        )
                    )
        return uses

    def _script_uses(self, dependencies: DependencyGraph) -> list[ProcessUse]:
        uses: list[ProcessUse] = []
        for manifest in dependencies.manifests:
            for script_name, command in manifest.scripts.items():
                argv = _split_command(command)
                executable = argv[0] if argv else ""
                uses.append(
                    ProcessUse(
                        path=manifest.path,
                        line=0,
                        language=manifest.ecosystem,
                        callee=f"script:{script_name}",
                        executable=executable,
                        argv=argv,
                        shell=True,
                        literal=bool(argv),
                        port=_first_port(argv),
                        source="manifest_script",
                    )
                )
        return uses

    def _config_uses(self, inventory: RepositoryInventory) -> list[ProcessUse]:
        uses: list[ProcessUse] = []
        for record in inventory.files:
            name = PurePosixPath(record.path).name.casefold()
            if name not in {
                ".mcp.json",
                "mcp.json",
                "mcp.yaml",
                "mcp.yml",
                "procfile",
            } and not name.endswith((".mcp.json", ".plugin.json")):
                continue
            try:
                text_value = (self.project_root / record.path).read_text(
                    encoding="utf-8"
                )
            except (OSError, UnicodeDecodeError):
                continue
            if name.endswith(".json"):
                try:
                    payload = json.loads(text_value)
                except json.JSONDecodeError:
                    continue
                for command, arguments in _commands_from_json(payload):
                    argv = (command, *arguments) if command else arguments
                    uses.append(
                        ProcessUse(
                            path=record.path,
                            line=0,
                            language="config",
                            callee="config.command",
                            executable=command,
                            argv=argv,
                            shell=False,
                            literal=bool(command),
                            port=_first_port(argv),
                            source="mcp_plugin_config",
                        )
                    )
            elif name == "procfile":
                for line_number, line in enumerate(text_value.splitlines(), start=1):
                    _, separator, command = line.partition(":")
                    if not separator or not command.strip():
                        continue
                    argv = _split_command(command.strip())
                    uses.append(
                        ProcessUse(
                            path=record.path,
                            line=line_number,
                            language="config",
                            callee="procfile",
                            executable=argv[0] if argv else "",
                            argv=argv,
                            shell=True,
                            literal=bool(argv),
                            port=_first_port(argv),
                            source="process_config",
                        )
                    )
        return uses

    def _profile_findings(self, catalog: ProcessCatalog) -> list[Finding]:
        findings: list[Finding] = []
        ids = Counter(profile.profile_id for profile in catalog.profiles)
        for profile_id, count in ids.items():
            if count > 1:
                findings.append(
                    finding(
                        "process_profile_duplicate",
                        f"Process profile {profile_id!r} appears {count} times.",
                        "processes",
                        severity=Severity.BLOCKER,
                        disposition=Disposition.DECLARE,
                        remediation="Keep one authoritative process declaration.",
                        default_path_impact="Process-use matching is ambiguous.",
                    )
                )
        port_owners: dict[int, list[ProcessProfile]] = defaultdict(list)
        for profile in catalog.profiles:
            for port in profile.ports:
                port_owners[port].append(profile)
            if profile.default_reachable and "required" not in profile.modes:
                findings.append(
                    finding(
                        "default_process_not_required",
                        "Default-reachable process is not marked required.",
                        "processes",
                        severity=Severity.ERROR,
                        path=catalog.source_path,
                        disposition=Disposition.DECLARE,
                        remediation="Mark it required or remove default reachability.",
                        default_path_impact="Default release process availability is ambiguous.",
                        attributes={"profile_id": profile.profile_id},
                    )
                )
            if profile.external and not profile.healthcheck:
                findings.append(
                    finding(
                        "external_process_healthcheck_missing",
                        "External process profile has no semantic healthcheck.",
                        "processes",
                        severity=Severity.BLOCKER,
                        path=catalog.source_path,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Declare a fail-closed semantic healthcheck.",
                        default_path_impact="Release cannot distinguish process presence from readiness.",
                        attributes={"profile_id": profile.profile_id},
                    )
                )
        for port, profiles in port_owners.items():
            defaults = [profile for profile in profiles if profile.default_reachable]
            if len(defaults) > 1:
                findings.append(
                    finding(
                        "default_port_collision",
                        f"Multiple default processes claim port {port}.",
                        "processes",
                        severity=Severity.BLOCKER,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Assign distinct ports or mutually exclusive profiles.",
                        default_path_impact="Default startup has a deterministic port conflict.",
                        attributes={
                            "port": port,
                            "profiles": [item.profile_id for item in defaults],
                        },
                    )
                )
        return findings

    def _use_findings(
        self, catalog: ProcessCatalog, uses: Sequence[ProcessUse]
    ) -> list[Finding]:
        findings: list[Finding] = []
        for use in uses:
            matches = catalog.match(use)
            runtime_path = use.path.startswith(("apps/", "packages/", "scripts/"))
            if not matches and runtime_path:
                findings.append(
                    finding(
                        "process_use_undeclared",
                        "Process/listener use has no matching custody profile.",
                        "processes",
                        severity=Severity.BLOCKER,
                        path=use.path,
                        line=use.line,
                        disposition=Disposition.DECLARE,
                        remediation="Declare owner, executable, mode, transport, ports and health.",
                        default_path_impact="Release process graph is incomplete.",
                        attributes=use.to_dict(),
                    )
                )
            if len(matches) > 1:
                findings.append(
                    finding(
                        "process_use_ambiguous",
                        "Process use matches multiple custody profiles.",
                        "processes",
                        severity=Severity.ERROR,
                        path=use.path,
                        line=use.line,
                        disposition=Disposition.DECLARE,
                        remediation="Narrow callsite patterns and command prefixes.",
                        default_path_impact="Runtime process ownership is ambiguous.",
                        attributes={
                            "use": use.identity,
                            "profiles": [item.profile_id for item in matches],
                        },
                    )
                )
            executable = PurePosixPath(use.executable.replace("\\", "/")).name.casefold()
            if runtime_path and _is_dynamic_installer(use):
                findings.append(
                    finding(
                        "process_dynamic_installer",
                        f"Process graph invokes dynamic installer {executable!r}.",
                        "processes",
                        severity=Severity.BLOCKER,
                        path=use.path,
                        line=use.line,
                        disposition=Disposition.REMOVE,
                        remediation="Move dependency acquisition to frozen install/build.",
                        default_path_impact="Runtime may acquire undeclared mutable code.",
                    )
                )
            parent = PARENT_SOURCE.search(" ".join((use.executable, *use.argv)))
            if parent:
                findings.append(
                    finding(
                        "process_parent_source_executable",
                        "Process command refers to a sibling source repository.",
                        "processes",
                        severity=Severity.BLOCKER,
                        source_repo=parent.group(1),
                        path=use.path,
                        line=use.line,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Use Zyra-owned source/package entrypoints.",
                        default_path_impact="Clean release depends on root workspace repositories.",
                    )
                )
        return findings

    def _port_findings(
        self, catalog: ProcessCatalog, uses: Sequence[ProcessUse]
    ) -> list[Finding]:
        findings: list[Finding] = []
        declared = {
            port: profile.profile_id
            for profile in catalog.profiles
            for port in profile.ports
        }
        for use in uses:
            if use.port and use.port not in declared:
                findings.append(
                    finding(
                        "listener_port_undeclared",
                        f"Listener uses undeclared port {use.port}.",
                        "processes",
                        severity=Severity.BLOCKER,
                        path=use.path,
                        line=use.line,
                        disposition=Disposition.DECLARE,
                        remediation="Declare the port in its process/deployment profile.",
                        default_path_impact="Clean startup and isolation policy cannot reserve the port.",
                        attributes={"port": use.port},
                    )
                )
        return findings

    def _docker_findings(
        self, inventory: RepositoryInventory
    ) -> list[Finding]:
        findings: list[Finding] = []
        for record in inventory.files:
            name = PurePosixPath(record.path).name.casefold()
            if "docker" not in name and name not in {
                "compose.yaml",
                "compose.yml",
            }:
                continue
            try:
                source = (self.project_root / record.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in DOCKER_CONTEXT.finditer(source):
                context = match.group(1).strip().replace("\\", "/")
                if context.startswith("../") or PARENT_SOURCE.search(context):
                    findings.append(
                        finding(
                            "docker_external_build_context",
                            "Docker/Compose uses a build context outside Zyra.",
                            "processes",
                            severity=Severity.BLOCKER,
                            path=record.path,
                            line=source[: match.start()].count("\n") + 1,
                            disposition=Disposition.BLOCK_RELEASE,
                            remediation="Use a Zyra-contained build context.",
                            default_path_impact="Release image depends on sibling repository content.",
                        )
                    )
            for match in DOCKER_RUN_DOWNLOAD.finditer(source):
                findings.append(
                    finding(
                        "docker_undeclared_download",
                        "Docker build performs dependency/download acquisition in RUN.",
                        "processes",
                        severity=Severity.ERROR,
                        path=record.path,
                        line=source[: match.start()].count("\n") + 1,
                        disposition=Disposition.DECLARE,
                        remediation="Pin versions/checksums and record the build dependency.",
                        default_path_impact="Image inputs are not fully represented by manifests.",
                        attributes={
                            "instruction_digest": content_digest(match.group(0).encode())
                        },
                    )
                )
            for match in DOCKER_FROM.finditer(source):
                image = match.group(1)
                if "@sha256:" not in image and ":" not in image:
                    findings.append(
                        finding(
                            "docker_base_image_unpinned",
                            "Docker base image lacks a tag or digest.",
                            "processes",
                            severity=Severity.ERROR,
                            path=record.path,
                            line=source[: match.start()].count("\n") + 1,
                            disposition=Disposition.DECLARE,
                            remediation="Pin the base image by digest for release builds.",
                            default_path_impact="Clean image build is not reproducible.",
                            attributes={"image": image},
                        )
                    )
        return findings

    def _mcp_plugin_findings(
        self, inventory: RepositoryInventory, catalog: ProcessCatalog
    ) -> list[Finding]:
        findings: list[Finding] = []
        known_paths = {
            path
            for profile in catalog.profiles
            for path in profile.callsite_patterns
            if profile.kind in {"mcp_server", "plugin_host"}
        }
        for record in inventory.files:
            lower = record.path.casefold()
            if not (
                lower.endswith((".mcp.json", ".plugin.json"))
                or PurePosixPath(lower).name in {"mcp.json", ".mcp.json"}
            ):
                continue
            if not any(
                fnmatch.fnmatch(record.path, pattern)
                or fnmatch.fnmatch(record.path, f"{pattern}/**")
                for pattern in known_paths
            ):
                findings.append(
                    finding(
                        "mcp_plugin_config_undeclared",
                        "MCP/plugin configuration is not owned by a process profile.",
                        "processes",
                        severity=Severity.BLOCKER,
                        path=record.path,
                        disposition=Disposition.DECLARE,
                        remediation="Bind the config to an MCP/plugin process owner and healthcheck.",
                        default_path_impact="Release can start an undeclared extension process.",
                    )
                )
        return findings


def _is_dynamic_installer(use: ProcessUse) -> bool:
    """Return true only when a package manager performs dependency acquisition.

    Package managers are also test runners, build launchers, and workspace
    dispatchers.  Treating ``bun test`` or ``npm --workspace ... test`` as an
    installer hid the meaningful distinction between an audited locked
    command and runtime code acquisition.
    """

    executable = PurePosixPath(use.executable.replace("\\", "/")).name.casefold()
    if executable not in INSTALLERS:
        return False
    arguments = [
        PurePosixPath(str(item).replace("\\", "/")).name.casefold()
        for item in use.argv[1:]
        if str(item).strip() and str(item) not in {"&&", "||", ";", "|"}
    ]
    if executable in {"pip", "pip3"}:
        return any(item in {"install", "download", "wheel"} for item in arguments)
    if executable == "uv":
        return any(
            item in {"add", "install", "sync", "pip"}
            for item in arguments[:2]
        )
    if executable == "poetry":
        return any(item in {"add", "install", "update"} for item in arguments)
    if executable == "cargo":
        return "install" in arguments
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        return any(
            item in {"install", "add", "update", "upgrade"}
            for item in arguments
        )
    if executable in {"npx", "bunx"}:
        # These executors are acquisition-capable by default.  A checked-in
        # local binary can instead be invoked through a package script.
        return "--no-install" not in arguments
    return False


def _port(value: Any) -> int:
    try:
        selected = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"port is not an integer: {value!r}") from exc
    if not 1 <= selected <= 65535:
        raise ValueError(f"port is outside 1..65535: {selected}")
    return selected


def _first_port(values: Iterable[str]) -> int:
    for value in values:
        for match in PORT_PATTERN.finditer(str(value)):
            selected = int(match.group(1))
            if 1 <= selected <= 65535:
                return selected
    return 0


def _flatten_python_literals(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            if not result:
                result.extend(_split_command(value))
            else:
                result.append(value)
        elif isinstance(value, (list, tuple)):
            result.extend(_flatten_python_literals(value))
    return result


def _javascript_argv(literals: Sequence[str]) -> tuple[str, ...]:
    if not literals:
        return ()
    if len(literals) == 1:
        return _split_command(literals[0])
    return tuple(literals)


def _split_command(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command, posix=True))
    except ValueError:
        return tuple(part for part in re.split(r"\s+", command.strip()) if part)


def _commands_from_json(value: Any) -> Iterable[tuple[str, tuple[str, ...]]]:
    if isinstance(value, Mapping):
        command = value.get("command")
        arguments = value.get("args") or value.get("arguments") or []
        if isinstance(command, str):
            if not isinstance(arguments, list):
                arguments = []
            yield command, tuple(str(item) for item in arguments)
        for nested in value.values():
            yield from _commands_from_json(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _commands_from_json(nested)
