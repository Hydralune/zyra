from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class SourceRole(StrEnum):
    PRIMARY = "primary_implementation"
    SUPPLEMENTARY = "supplementary_implementation"
    CONFORMANCE = "conformance_only"
    REFERENCE = "reference_only"
    REJECTED = "rejected"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class SourceDecision:
    repository: str
    source_paths: tuple[str, ...]
    role: SourceRole
    target_paths: tuple[str, ...]
    capability: str
    rationale: str
    runtime_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "source_paths": list(self.source_paths),
            "role": str(self.role),
            "target_paths": list(self.target_paths),
            "capability": self.capability,
            "rationale": self.rationale,
            "runtime_required": self.runtime_required,
        }


SOURCE_DECISIONS: tuple[SourceDecision, ...] = (
    SourceDecision(
        "browser-use",
        (
            "browser_use/browser/session.py",
            "browser_use/browser/watchdogs/local_browser_watchdog.py",
            "browser_use/browser/watchdogs/security_watchdog.py",
            "browser_use/browser/watchdogs/downloads_watchdog.py",
            "browser_use/browser/watchdogs/storage_state_watchdog.py",
            "browser_use/browser/watchdogs/permissions_watchdog.py",
            "browser_use/browser/watchdogs/screenshot_watchdog.py",
            "browser_use/browser/watchdogs/popups_watchdog.py",
            "browser_use/browser/watchdogs/aboutblank_watchdog.py",
            "browser_use/agent/views.py",
        ),
        SourceRole.PRIMARY,
        (
            "packages/workers/zyra_workers/browser_observability/watchdogs.py",
            "packages/workers/zyra_workers/browser_observability/history_runtime.py",
            "packages/workers/zyra_workers/browser_observability/history_store.py",
            "packages/workers/zyra_workers/browser_observability/crash_detector.py",
        ),
        "attached watchdog lifecycle and durable browser action history",
        "Browser Use lifecycle semantics were decomposed into Zyra scope, event, "
        "artifact and recovery-input contracts. Upstream CrashWatchdog remains unattached.",
        True,
    ),
    SourceDecision(
        "OpenHands",
        (
            "openhands/events/**",
            "openhands/storage/**",
        ),
        SourceRole.SUPPLEMENTARY,
        (
            "packages/workers/zyra_workers/browser_observability/artifact_publisher.py",
            "packages/workers/zyra_workers/browser_observability/api_projection.py",
        ),
        "artifact/event projection and queryable observability views",
        "Only projection behavior is retained; canonical Zyra EventLog and "
        "LocalArtifactStore remain the sole owners.",
        True,
    ),
    SourceDecision(
        "oh-my-pi",
        (
            "packages/coding-agent/src/core/agent-loop.ts",
            "packages/coding-agent/src/tools/edit/hashline.ts",
        ),
        SourceRole.SUPPLEMENTARY,
        (
            "packages/workers/zyra_workers/browser_observability/trace_runtime.py",
            "packages/workers/zyra_workers/browser_observability/models.py",
            "packages/workers/zyra_workers/browser_observability/replay.py",
        ),
        "tool call/result pairing and digest-linked artifact receipts",
        "OMP process, session and tool owners are not copied; only pairing and "
        "receipt semantics are mapped into Zyra-owned records.",
        True,
    ),
    SourceDecision(
        "browser-use",
        ("browser_use/browser/watchdogs/crash_watchdog.py",),
        SourceRole.REJECTED,
        (
            "packages/workers/zyra_workers/browser_observability/crash_detector.py",
        ),
        "browser crash detection",
        "The upstream CrashWatchdog is commented out in attach_all_watchdogs. "
        "Zyra implements process exit, CDP disconnect and silent timeout directly.",
        False,
    ),
    SourceDecision(
        "claude-code-best",
        ("src/QueryEngine.ts", "src/tools/**"),
        SourceRole.CONFORMANCE,
        (
            "packages/workers/zyra_workers/browser_observability/trace_runtime.py",
        ),
        "tool-result pairing comparison",
        "No second session, history, trace or tool owner is migrated.",
        False,
    ),
    SourceDecision(
        "opencode",
        ("packages/opencode/src/session/**",),
        SourceRole.CONFORMANCE,
        (
            "packages/workers/zyra_workers/browser_observability/replay.py",
        ),
        "durable session event comparison",
        "Conformance only; it cannot become a parallel browser history owner.",
        False,
    ),
    SourceDecision(
        "Hermes-Agent",
        ("hermes-agent/**",),
        SourceRole.REFERENCE,
        (
            "packages/workers/zyra_workers/browser_observability/judge.py",
        ),
        "trajectory evaluation reference",
        "No runtime dependency and no authoritative LLM control path.",
        False,
    ),
)


@dataclass(frozen=True, slots=True)
class SourceAuditFinding:
    code: str
    path: str
    line: int
    summary: str
    fatal: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "summary": self.summary,
            "fatal": self.fatal,
        }


@dataclass(frozen=True, slots=True)
class SourceAuditReport:
    package_root: str
    production_files: tuple[str, ...]
    decisions: tuple[SourceDecision, ...]
    findings: tuple[SourceAuditFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(item.fatal for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_root": self.package_root,
            "production_files": list(self.production_files),
            "decisions": [item.to_dict() for item in self.decisions],
            "findings": [item.to_dict() for item in self.findings],
            "ok": self.ok,
        }


class BrowserObservabilitySourceAuditor:
    FORBIDDEN_IMPORTS = frozenset(
        {
            "browser_use",
            "openhands",
            "opencode",
            "hermes",
            "claude_code",
        }
    )
    FORBIDDEN_PROCESS_CALLS = frozenset(
        {
            "subprocess.Popen",
            "subprocess.run",
            "subprocess.call",
            "os.system",
            "os.popen",
        }
    )

    def __init__(
        self,
        package_root: str | Path,
    ) -> None:
        self.package_root = Path(package_root).resolve()

    def audit(self) -> SourceAuditReport:
        findings: list[SourceAuditFinding] = []
        files = tuple(sorted(self.package_root.rglob("*.py")))
        for path in files:
            findings.extend(self._audit_file(path))
        primary = {
            item.repository
            for item in SOURCE_DECISIONS
            if item.role == SourceRole.PRIMARY
        }
        supplementary = {
            item.repository
            for item in SOURCE_DECISIONS
            if item.role == SourceRole.SUPPLEMENTARY
        }
        if len(primary) != 1:
            findings.append(
                SourceAuditFinding(
                    "primary_source_count",
                    "",
                    0,
                    "observability domain must have exactly one primary source",
                    True,
                )
            )
        if len(supplementary) > 2:
            findings.append(
                SourceAuditFinding(
                    "supplementary_source_limit",
                    "",
                    0,
                    "observability domain has more than two supplementary sources",
                    True,
                )
            )
        return SourceAuditReport(
            package_root=str(self.package_root),
            production_files=tuple(
                str(path.relative_to(self.package_root)).replace("\\", "/")
                for path in files
            ),
            decisions=SOURCE_DECISIONS,
            findings=tuple(findings),
        )

    def _audit_file(
        self,
        path: Path,
    ) -> tuple[SourceAuditFinding, ...]:
        relative = str(path.relative_to(self.package_root)).replace("\\", "/")
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError) as error:
            return (
                SourceAuditFinding(
                    "python_source_invalid",
                    relative,
                    int(getattr(error, "lineno", 0) or 0),
                    str(error),
                    True,
                ),
            )
        findings: list[SourceAuditFinding] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [str(node.module or "")]
            else:
                names = []
            for name in names:
                if name.split(".", 1)[0] in self.FORBIDDEN_IMPORTS:
                    findings.append(
                        SourceAuditFinding(
                            "root_source_runtime_import",
                            relative,
                            int(getattr(node, "lineno", 0) or 0),
                            f"runtime imports forbidden source package {name!r}",
                            True,
                        )
                    )
            if isinstance(node, ast.Call):
                name = _dotted_name(node.func)
                if name in self.FORBIDDEN_PROCESS_CALLS:
                    findings.append(
                        SourceAuditFinding(
                            "unowned_subprocess_boundary",
                            relative,
                            int(getattr(node, "lineno", 0) or 0),
                            f"observability package calls {name!r}",
                            True,
                        )
                    )
        return tuple(findings)


def _dotted_name(
    node: ast.AST,
) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""
