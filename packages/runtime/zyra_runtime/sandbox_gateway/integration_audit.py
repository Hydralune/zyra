from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .integration_models import (
    GatewayAuditFinding,
    GatewayAuditReport,
    content_digest,
    stable_identifier,
)


_PYTHON_BANNED_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.popen",
        "Path.write_text",
        "Path.write_bytes",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copytree",
        "urllib.request.urlopen",
        "requests.get",
        "requests.post",
        "httpx.get",
        "httpx.post",
    }
)
_TYPESCRIPT_BANNED = (
    (re.compile(r"\bexecSync\s*\("), "typescript_exec_sync"),
    (re.compile(r"\bspawnSync\s*\("), "typescript_spawn_sync"),
    (re.compile(r"\bexec\s*\("), "typescript_exec"),
    (re.compile(r"\bwriteFileSync\s*\("), "typescript_direct_write"),
    (re.compile(r"\bappendFileSync\s*\("), "typescript_direct_append"),
    (re.compile(r"\bfetch\s*\("), "typescript_direct_network"),
    (re.compile(r"\bcreateRequire\s*\("), "typescript_dynamic_require"),
    (re.compile(r"\bimport\s*\("), "typescript_dynamic_import"),
)
_PATH_ESCAPE = re.compile(r"(?:\.\.[/\\]|^[A-Za-z]:[/\\]|^\\\\|^//)")
_EXTERNAL_SOURCE = re.compile(
    r"(?:\.\.[/\\](?:claude-code-best|OpenHands|browser-use|opencode|OpenClaw|oh-my-pi)|"
    r"G:[/\\]agent-zoo[/\\](?:claude-code-best|OpenHands|browser-use|opencode|OpenClaw|oh-my-pi))",
    re.IGNORECASE,
)
_VENDOR_RUNTIME = re.compile(r"(?:vendor-runtimes|runtime-sources|source-pool)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class GatewayAuditPolicy:
    allowed_process_modules: frozenset[str] = frozenset(
        {
            "sandbox_gateway/backends.py",
            "sandbox_gateway/process_tree.py",
            "sandbox_gateway/integration_host.py",
        }
    )
    allowed_write_modules: frozenset[str] = frozenset(
        {
            "sandbox_gateway/state_store.py",
            "sandbox_gateway/quarantine.py",
            "sandbox_gateway/integration_events.py",
            "sandbox_gateway/integration_remote.py",
        }
    )
    allowed_network_modules: frozenset[str] = frozenset(
        {
            "sandbox_gateway/integration_browser.py",
        }
    )
    maximum_file_bytes: int = 2 * 1024 * 1024


class _PythonCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.imports: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name:
            self.calls.append((node.lineno, name))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append((node.lineno, alias.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.append((node.lineno, node.module))
        self.generic_visit(node)


class GatewayIntegrationAuditor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        policy: GatewayAuditPolicy | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.policy = policy or GatewayAuditPolicy()

    def audit(self, paths: Sequence[str | Path]) -> GatewayAuditReport:
        findings: list[GatewayAuditFinding] = []
        scanned_files = 0
        scanned_lines = 0
        external_dependencies = 0
        scopes: list[str] = []
        for selected in paths:
            path = Path(selected)
            if not path.is_absolute():
                path = self.project_root / path
            path = path.resolve()
            path.relative_to(self.project_root)
            scopes.append(path.relative_to(self.project_root).as_posix())
            files = tuple(_source_files(path))
            for file_path in files:
                scanned_files += 1
                if file_path.stat().st_size > self.policy.maximum_file_bytes:
                    findings.append(
                        self._finding(
                            "audit_file_too_large",
                            "error",
                            file_path,
                            0,
                            "module",
                            "source file exceeds static audit budget",
                        )
                    )
                    continue
                text = file_path.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
                scanned_lines += len(lines)
                relative = file_path.relative_to(self.project_root).as_posix()
                if _EXTERNAL_SOURCE.search(text):
                    external_dependencies += 1
                    findings.append(
                        self._finding(
                            "external_source_runtime_dependency",
                            "critical",
                            file_path,
                            _first_match_line(lines, _EXTERNAL_SOURCE),
                            "module",
                            "runtime source references a root-level source repository",
                        )
                    )
                audit_rule_module = relative.endswith(
                    ("sandbox_gateway/integration_audit.py", "sandbox-gateway-control/src/integration-audit.ts")
                )
                if not audit_rule_module and _VENDOR_RUNTIME.search(text):
                    findings.append(
                        self._finding(
                            "vendor_runtime_dependency",
                            "critical",
                            file_path,
                            _first_match_line(lines, _VENDOR_RUNTIME),
                            "module",
                            "runtime source depends on a vendor/source-pool boundary",
                        )
                    )
                if file_path.suffix == ".py":
                    findings.extend(self._audit_python(file_path, relative, text, lines))
                elif file_path.suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"}:
                    findings.extend(self._audit_typescript(file_path, relative, lines))
        disabled_probe_passed = self._disabled_probe(paths)
        if not disabled_probe_passed:
            findings.append(
                self._finding(
                    "gateway_disable_probe_failed",
                    "critical",
                    self.project_root,
                    0,
                    "disable_probe",
                    "production entry does not fail closed when the sandbox gateway is disconnected",
                )
            )
        return GatewayAuditReport(
            report_id=stable_identifier(
                "gateway-audit",
                sorted(scopes),
                [item.safe_dict() for item in findings],
            ),
            scope=tuple(scopes),
            findings=tuple(findings),
            scanned_files=scanned_files,
            scanned_lines=scanned_lines,
            disabled_probe_passed=disabled_probe_passed,
            external_dependency_count=external_dependencies,
            metadata={
                "project_root_digest": content_digest(str(self.project_root)),
                "policy": {
                    "allowed_process_modules": sorted(self.policy.allowed_process_modules),
                    "allowed_write_modules": sorted(self.policy.allowed_write_modules),
                    "allowed_network_modules": sorted(self.policy.allowed_network_modules),
                },
            },
        )

    def _audit_python(
        self,
        path: Path,
        relative: str,
        text: str,
        lines: Sequence[str],
    ) -> list[GatewayAuditFinding]:
        findings: list[GatewayAuditFinding] = []
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as error:
            return [
                self._finding(
                    "python_parse_failed",
                    "critical",
                    path,
                    int(error.lineno or 0),
                    "module",
                    f"Python source cannot be audited: {error.msg}",
                )
            ]
        visitor = _PythonCallVisitor()
        visitor.visit(tree)
        normalized = _gateway_relative(relative)
        for line, name in visitor.calls:
            matched = next((item for item in _PYTHON_BANNED_CALLS if name.endswith(item)), None)
            if matched is None:
                continue
            process = matched.startswith("subprocess") or matched.startswith("os.")
            write = "write" in matched or matched.startswith("shutil")
            network = matched.endswith("urlopen") or matched.startswith("requests") or matched.startswith("httpx")
            if process and normalized in self.policy.allowed_process_modules:
                continue
            if write and normalized in self.policy.allowed_write_modules:
                continue
            if network and normalized in self.policy.allowed_network_modules:
                continue
            findings.append(
                self._finding(
                    "gateway_primitive_bypass",
                    "critical",
                    path,
                    line,
                    name,
                    f"{name} bypasses the canonical sandbox gateway boundary",
                )
            )
        for line, module in visitor.imports:
            if module.startswith(("vendor", "runtime_sources", "source_pool")):
                findings.append(
                    self._finding(
                        "gateway_vendor_import",
                        "critical",
                        path,
                        line,
                        module,
                        "production gateway imports a vendor/source-pool module",
                    )
                )
        for line_number, line in enumerate(lines, 1):
            if _PATH_ESCAPE.search(line) and "test" not in relative.casefold():
                if "relative_to" in line or "traversal" in line.casefold() or "_PATH_ESCAPE" in line:
                    continue
                findings.append(
                    self._finding(
                        "gateway_path_escape_literal",
                        "warning",
                        path,
                        line_number,
                        "path_literal",
                        "source contains an escape-shaped path and requires review",
                        blocking=False,
                    )
                )
        return findings

    def _audit_typescript(
        self,
        path: Path,
        relative: str,
        lines: Sequence[str],
    ) -> list[GatewayAuditFinding]:
        findings: list[GatewayAuditFinding] = []
        normalized = _gateway_relative(relative)
        for line_number, line in enumerate(lines, 1):
            for pattern, code in _TYPESCRIPT_BANNED:
                if not pattern.search(line):
                    continue
                if code in {"typescript_exec_sync", "typescript_spawn_sync", "typescript_exec"} and normalized in self.policy.allowed_process_modules:
                    continue
                if code in {"typescript_direct_write", "typescript_direct_append"} and normalized in self.policy.allowed_write_modules:
                    continue
                if code == "typescript_direct_network" and normalized in self.policy.allowed_network_modules:
                    continue
                findings.append(
                    self._finding(
                        code,
                        "critical",
                        path,
                        line_number,
                        "typescript_call",
                        "TypeScript production path bypasses the fixed gateway RPC/control boundary",
                    )
                )
        return findings

    def _disabled_probe(self, paths: Sequence[str | Path]) -> bool:
        required_markers = {
            "sandbox_gateway_required",
            "sandbox_gateway_router",
            "sandbox_gateway_unavailable",
        }
        observed: set[str] = set()
        for selected in paths:
            path = Path(selected)
            if not path.is_absolute():
                path = self.project_root / path
            for file_path in _source_files(path.resolve()):
                if file_path.suffix not in {".py", ".ts", ".tsx"}:
                    continue
                text = file_path.read_text(encoding="utf-8", errors="replace")
                observed.update(marker for marker in required_markers if marker in text)
        return observed == required_markers

    def _finding(
        self,
        code: str,
        severity: str,
        path: Path,
        line: int,
        symbol: str,
        reason: str,
        *,
        blocking: bool = True,
    ) -> GatewayAuditFinding:
        try:
            relative = path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            relative = "<outside-project>"
        evidence = {
            "code": code,
            "path": relative,
            "line": line,
            "symbol": symbol,
            "reason": reason,
        }
        return GatewayAuditFinding(
            finding_id=stable_identifier("gateway-audit-finding", evidence),
            code=code,
            severity=severity,
            path=relative,
            line=line,
            symbol=symbol,
            reason=reason,
            evidence_digest=content_digest(evidence),
            blocking=blocking,
        )


def _source_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.suffix in {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs"}:
            yield path
        return
    if not path.is_dir():
        return
    for candidate in sorted(path.rglob("*")):
        if not candidate.is_file():
            continue
        if any(part in {"node_modules", "__pycache__", ".git", "dist", "build"} for part in candidate.parts):
            continue
        if candidate.suffix in {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs"}:
            yield candidate


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def _first_match_line(lines: Sequence[str], pattern: re.Pattern[str]) -> int:
    for index, line in enumerate(lines, 1):
        if pattern.search(line):
            return index
    return 0


def _gateway_relative(relative: str) -> str:
    marker = "zyra_runtime/"
    normalized = relative.replace("\\", "/")
    return normalized.split(marker, 1)[-1] if marker in normalized else normalized


__all__ = [
    "GatewayAuditPolicy",
    "GatewayIntegrationAuditor",
]
