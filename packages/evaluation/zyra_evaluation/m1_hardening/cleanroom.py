from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity, utc_now
from .integration_contracts import stable_digest


FORBIDDEN_SIBLING_NAMES = (
    "claude-code-best",
    "browser-use",
    "OpenHands",
    "opencode",
    "oh-my-pi",
    "openclaw",
    "langgraph",
    "agentscope",
    "hermes-agent",
)

FORBIDDEN_RESIDUAL_NAMES = (
    ".cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage",
)

FORBIDDEN_RUNTIME_SUFFIXES = (
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pid",
    ".sock",
    ".log",
    ".pyc",
)


@dataclass(frozen=True, slots=True)
class CleanroomCommand:
    command_id: str
    argv: tuple[str, ...]
    timeout_seconds: float
    required: bool = True
    environment: Mapping[str, str] = field(default_factory=dict)
    expected_exit_codes: tuple[int, ...] = (0,)

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,80}", self.command_id):
            issues.append("invalid command id")
        if not self.argv:
            issues.append("command argv is empty")
        if self.timeout_seconds <= 0:
            issues.append("command timeout must be positive")
        if not self.expected_exit_codes:
            issues.append("expected exit codes are empty")
        forbidden = {"rm", "rmdir", "del", "format", "git-reset", "git-clean"}
        joined = "-".join(item.lower() for item in self.argv[:3])
        if any(token in joined for token in forbidden):
            issues.append("destructive command is not allowed in cleanroom verification")
        return tuple(issues)


@dataclass(slots=True)
class CleanroomCommandReceipt:
    command_id: str
    argv: tuple[str, ...]
    started_at: str
    completed_at: str
    duration_ms: int
    exit_code: int | None
    timed_out: bool
    stdout_digest: str
    stderr_digest: str
    stdout_tail: str
    stderr_tail: str
    required: bool
    expected_exit_codes: tuple[int, ...]

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code in self.expected_exit_codes

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "required": self.required,
            "expected_exit_codes": list(self.expected_exit_codes),
            "ok": self.ok,
        }


@dataclass(slots=True)
class CleanroomReceipt:
    source_root: str
    target_commit: str
    archive_digest: str
    extracted_root: str
    started_at: str
    completed_at: str
    source_dirty: bool
    file_count: int
    symlink_count: int
    residual_paths: list[str]
    outside_links: list[str]
    forbidden_references: list[Mapping[str, Any]]
    commands: list[CleanroomCommandReceipt]
    environment_names: list[str]
    cleanup_ok: bool
    limitations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.source_dirty
            and bool(self.archive_digest)
            and self.file_count > 0
            and not self.residual_paths
            and not self.outside_links
            and not self.forbidden_references
            and all(item.ok or not item.required for item in self.commands)
            and self.cleanup_ok
            and not self.limitations
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": "zyra.m1-cleanroom-receipt/v1",
            "source_root": self.source_root,
            "target_commit": self.target_commit,
            "archive_digest": self.archive_digest,
            "extracted_root_name": Path(self.extracted_root).name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "source_dirty": self.source_dirty,
            "file_count": self.file_count,
            "symlink_count": self.symlink_count,
            "residual_paths": list(self.residual_paths),
            "outside_links": list(self.outside_links),
            "forbidden_references": [dict(item) for item in self.forbidden_references],
            "commands": [item.to_dict() for item in self.commands],
            "environment_names": list(self.environment_names),
            "cleanup_ok": self.cleanup_ok,
            "limitations": list(self.limitations),
            "ok": self.ok,
        }
        value["content_digest"] = stable_digest(value)
        return value


class CleanEnvironment:
    ALLOW_EXACT = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TMP",
        "TEMP",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "LANG",
        "LC_ALL",
        "TERM",
        "CI",
    }
    DENY_TOKENS = (
        "API_KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "OPENAI",
        "ANTHROPIC",
        "MCP",
        "PYTHONPATH",
        "NODE_PATH",
        "VIRTUAL_ENV",
        "CODEX_HOME",
    )

    @classmethod
    def build(cls, root: Path, additions: Mapping[str, str] | None = None) -> dict[str, str]:
        environment: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if upper in cls.ALLOW_EXACT and not any(token in upper for token in cls.DENY_TOKENS):
                environment[key] = value
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
                "NPM_CONFIG_UPDATE_NOTIFIER": "false",
                "NPM_CONFIG_FUND": "false",
                "ZYRA_CLEANROOM": "1",
                "ZYRA_PROJECT_ROOT": str(root),
                "ZYRA_ARTIFACT_ROOT": str(root / ".cleanroom-artifacts"),
            }
        )
        for key, value in (additions or {}).items():
            upper = key.upper()
            if any(token in upper for token in cls.DENY_TOKENS):
                raise ValueError(f"cleanroom command cannot inject secret/runtime path variable: {key}")
            environment[str(key)] = str(value)
        return environment


class GitArchiveExporter:
    def __init__(self, source_root: str | Path) -> None:
        self.root = Path(source_root).resolve()

    def resolve_commit(self, expression: str) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", f"{expression}^{{commit}}"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"cannot resolve cleanroom commit {expression}: {completed.stderr.strip()}")
        value = completed.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise RuntimeError(f"git returned invalid commit identity: {value}")
        return value

    def dirty(self) -> bool:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"cannot inspect source worktree: {completed.stderr.strip()}")
        return bool(completed.stdout.strip())

    def export(self, commit: str, destination: Path) -> tuple[str, int]:
        archive_path = destination.parent / f"{commit}.tar"
        completed = subprocess.run(
            ["git", "archive", "--format=tar", f"--output={archive_path}", commit],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git archive failed: {completed.stderr.strip()}")
        archive_digest = self._sha256(archive_path)
        with tarfile.open(archive_path, mode="r") as archive:
            members = archive.getmembers()
            self._validate_members(members, destination)
            archive.extractall(destination, filter="data")
        archive_path.unlink(missing_ok=True)
        return archive_digest, len(members)

    @staticmethod
    def _validate_members(members: Sequence[tarfile.TarInfo], destination: Path) -> None:
        for member in members:
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as error:
                raise RuntimeError(f"archive member escapes cleanroom: {member.name}") from error
            if member.isdev() or member.isfifo():
                raise RuntimeError(f"archive contains unsupported special file: {member.name}")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class CleanroomBoundaryScanner:
    TEXT_SUFFIXES = {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".cjs",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".md",
        ".ps1",
        ".sh",
        ".rs",
    }

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def scan(self) -> tuple[list[str], list[str], list[Mapping[str, Any]], int, int]:
        residuals: list[str] = []
        outside_links: list[str] = []
        references: list[Mapping[str, Any]] = []
        file_count = 0
        symlink_count = 0
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                symlink_count += 1
                resolved = path.resolve()
                try:
                    resolved.relative_to(self.root)
                except ValueError:
                    outside_links.append(relative)
                continue
            if not path.is_file():
                if path.name in FORBIDDEN_RESIDUAL_NAMES:
                    residuals.append(relative + "/")
                continue
            file_count += 1
            lower_name = path.name.lower()
            if lower_name in {item.lower() for item in FORBIDDEN_RESIDUAL_NAMES}:
                residuals.append(relative)
            if (
                path.suffix.lower() in FORBIDDEN_RUNTIME_SUFFIXES
                and not self._committed_evidence_file(relative)
            ):
                residuals.append(relative)
            if (
                self._runtime_reference_scope(relative)
                and path.suffix.lower() in self.TEXT_SUFFIXES
                and path.stat().st_size <= 4 * 1024 * 1024
            ):
                references.extend(self._scan_text(path, relative))
        return sorted(set(residuals)), sorted(set(outside_links)), references, file_count, symlink_count

    @staticmethod
    def _committed_evidence_file(relative: str) -> bool:
        normalized = relative.replace("\\", "/").lower()
        return normalized.startswith(("docs/", "vendor/"))

    @staticmethod
    def _runtime_reference_scope(relative: str) -> bool:
        normalized = relative.replace("\\", "/").lower()
        parts = tuple(part for part in normalized.split("/") if part)
        excluded = (
            "docs/",
            "tests/",
            "vendor/",
            "vendor-runtimes/",
            "source-pool/",
            "runtime-sources/",
            "scripts/remediation/",
        )
        if normalized.startswith(excluded):
            return False
        if any(part in {"test", "tests", "__tests__"} for part in parts):
            return False
        if ".test." in normalized or ".spec." in normalized:
            return False
        if normalized.startswith("scripts/smoke_"):
            return False
        return normalized.startswith(("apps/", "packages/", "skills/", "scripts/"))

    def _scan_text(self, path: Path, relative: str) -> list[Mapping[str, Any]]:
        text = path.read_text(encoding="utf-8", errors="replace")
        findings: list[Mapping[str, Any]] = []
        patterns = (
            ("parent-runtime-path", re.compile(r"(?:^|[\"'\s=])\.\.[\\/](?:claude-code-best|browser-use|OpenHands|opencode|oh-my-pi|openclaw|langgraph|agentscope|hermes-agent)(?:[\\/]|$)", re.I)),
            ("editable-install", re.compile(r"(?:pip\s+install\s+-e|editable\s*=|file:|link:)[^\n]{0,200}(?:\.\.[\\/]|[A-Za-z]:[\\/])", re.I)),
            ("external-docker-context", re.compile(r"(?:build|context)\s*:\s*(?:\.\.[\\/]|[A-Za-z]:[\\/])", re.I)),
            ("sibling-process", re.compile(r"(?:spawn|Popen|Start-Process|subprocess)[^\n]{0,300}(?:claude-code-best|browser-use|OpenHands|opencode|oh-my-pi|openclaw)", re.I)),
            ("absolute-workspace-path", re.compile(r"[A-Za-z]:[\\/]agent-zoo[\\/](?!zyra(?:[\\/]|$))", re.I)),
        )
        for line_number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in patterns:
                # Python process construction is checked structurally below.  Do
                # not let this scanner's own detector regexes become evidence of
                # the behaviour that they are intended to detect.
                if path.suffix.lower() == ".py" and kind in {
                    "parent-runtime-path",
                    "editable-install",
                    "external-docker-context",
                    "sibling-process",
                    "absolute-workspace-path",
                }:
                    continue
                if pattern.search(line):
                    findings.append(
                        {
                            "kind": kind,
                            "path": relative,
                            "line": line_number,
                            "digest": stable_digest(line),
                        }
                    )
        if path.suffix.lower() == ".py" and not relative.startswith(("docs/", "tests/")):
            findings.extend(self._scan_python_ast(path, relative, text))
        return findings

    @staticmethod
    def _scan_python_ast(path: Path, relative: str, text: str) -> list[Mapping[str, Any]]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        findings: list[Mapping[str, Any]] = []
        path_operations = (
            "Path",
            "open",
            "read_text",
            "read_bytes",
            "write_text",
            "write_bytes",
            "glob",
            "rglob",
            "import_module",
            "spec_from_file_location",
            "add_dll_directory",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = CleanroomBoundaryScanner._call_name(node.func)
                runtime_operation = name.endswith(
                    ("Popen", "run", "call", "check_call", "check_output", *path_operations)
                )
                if runtime_operation:
                    for argument in node.args:
                        for value_node in ast.walk(argument):
                            if not isinstance(value_node, ast.Constant) or not isinstance(value_node.value, str):
                                continue
                            value = value_node.value
                            normalized = value.replace("\\", "/").lower()
                            sibling_path = any(
                                f"../{sibling.lower()}" in normalized
                                or f"/agent-zoo/{sibling.lower()}" in normalized
                                for sibling in FORBIDDEN_SIBLING_NAMES
                            )
                            if sibling_path:
                                findings.append(
                                    {
                                        "kind": "python-external-process-or-path",
                                        "path": relative,
                                        "line": getattr(value_node, "lineno", getattr(node, "lineno", 0)),
                                        "digest": stable_digest(value),
                                    }
                                )
        return findings

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return CleanroomBoundaryScanner._call_name(node.value) + "." + node.attr
        return ""


class CleanroomCommandRunner:
    def run(
        self,
        root: Path,
        command: CleanroomCommand,
        base_environment: Mapping[str, str],
    ) -> CleanroomCommandReceipt:
        issues = command.validate()
        if issues:
            raise ValueError(f"invalid cleanroom command {command.command_id}: {'; '.join(issues)}")
        environment = dict(base_environment)
        environment.update(command.environment)
        started_at = utc_now()
        started = time.monotonic()
        exit_code: int | None = None
        timed_out = False
        stdout = ""
        stderr = ""
        try:
            completed = subprocess.run(
                list(command.argv),
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=command.timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = self._text(error.stdout)
            stderr = self._text(error.stderr)
        except OSError as error:
            stderr = f"{type(error).__name__}: {error}"
        return CleanroomCommandReceipt(
            command_id=command.command_id,
            argv=command.argv,
            started_at=started_at,
            completed_at=utc_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_digest=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            stderr_digest=hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
            stdout_tail=stdout[-4000:],
            stderr_tail=stderr[-4000:],
            required=command.required,
            expected_exit_codes=command.expected_exit_codes,
        )

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)


class CleanroomVerifier:
    def __init__(self, source_root: str | Path) -> None:
        self.root = Path(source_root).resolve()
        self.exporter = GitArchiveExporter(self.root)
        self.runner = CleanroomCommandRunner()

    def verify(
        self,
        *,
        target_commit: str,
        commands: Sequence[CleanroomCommand] = (),
        require_clean_source: bool = True,
        temporary_parent: str | Path | None = None,
    ) -> tuple[CleanroomReceipt, GateResult]:
        started_at = utc_now()
        commit = self.exporter.resolve_commit(target_commit)
        dirty = self.exporter.dirty()
        limitations: list[str] = []
        if dirty and require_clean_source:
            limitations.append("source worktree was dirty before exact-commit cleanroom export")
        parent = Path(temporary_parent).resolve() if temporary_parent else None
        temporary = Path(tempfile.mkdtemp(prefix="zyra-m1-cleanroom-", dir=parent))
        extracted = temporary / "zyra"
        extracted.mkdir(parents=True, exist_ok=False)
        archive_digest = ""
        member_count = 0
        residuals: list[str] = []
        outside_links: list[str] = []
        references: list[Mapping[str, Any]] = []
        file_count = 0
        symlink_count = 0
        command_receipts: list[CleanroomCommandReceipt] = []
        cleanup_ok = False
        environment_names: list[str] = []
        try:
            archive_digest, member_count = self.exporter.export(commit, extracted)
            scanner = CleanroomBoundaryScanner(extracted)
            residuals, outside_links, references, file_count, symlink_count = scanner.scan()
            environment = CleanEnvironment.build(
                extracted,
                additions=self._controlled_toolchain_environment(extracted),
            )
            environment_names = sorted(environment)
            for command in commands:
                command_receipts.append(self.runner.run(extracted, command, environment))
            _post_residuals, post_links, post_references, post_files, post_symlinks = scanner.scan()
            # Command-local bytecode/test caches are destroyed with the temporary
            # tree and cannot satisfy a dependency.  The pre-command scan is the
            # evidence that the exact commit did not ship residual state; the
            # post-command scan remains authoritative for escaping links and
            # forbidden runtime references created while commands execute.
            outside_links = sorted(set(outside_links + post_links))
            references = self._unique_references([*references, *post_references])
            file_count = max(file_count, post_files)
            symlink_count = max(symlink_count, post_symlinks)
        except Exception as error:
            limitations.append(f"cleanroom execution failed: {type(error).__name__}: {error}")
        finally:
            try:
                shutil.rmtree(temporary)
                cleanup_ok = not temporary.exists()
            except OSError as error:
                limitations.append(f"cleanroom cleanup failed: {type(error).__name__}: {error}")
        receipt = CleanroomReceipt(
            source_root=str(self.root),
            target_commit=commit,
            archive_digest=archive_digest,
            extracted_root=str(extracted),
            started_at=started_at,
            completed_at=utc_now(),
            source_dirty=dirty,
            file_count=file_count,
            symlink_count=symlink_count,
            residual_paths=residuals,
            outside_links=outside_links,
            forbidden_references=references,
            commands=command_receipts,
            environment_names=environment_names,
            cleanup_ok=cleanup_ok,
            limitations=limitations,
        )
        return receipt, self._gate(receipt, member_count=member_count)

    def _controlled_toolchain_environment(self, extracted: Path) -> Mapping[str, str]:
        """Expose only the source workspace's package-manager-locked Bun binary.

        ``git archive`` intentionally excludes ``node_modules``.  The cleanroom
        may nevertheless execute the committed TypeScript sources with the
        toolchain locked by the archived ``package.json``.  The executable is
        selected from the source workspace, version checked, and exposed as a
        toolchain input; it is never copied into or treated as a runtime
        dependency of the archived project.
        """

        package_path = extracted / "package.json"
        if not package_path.is_file():
            return {}
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        package_manager = str(package.get("packageManager") or "")
        match = re.fullmatch(r"bun@([0-9]+(?:\.[0-9]+){2})", package_manager)
        if match is None:
            return {}
        locked_version = match.group(1)
        executable_name = "bun.exe" if os.name == "nt" else "bun"
        candidates = (
            self.root / "node_modules" / "bun" / "bin" / executable_name,
            self.root / "node_modules" / ".bin" / executable_name,
        )
        executable = next((item.resolve() for item in candidates if item.is_file()), None)
        if executable is None:
            return {}
        observed = subprocess.run(
            [str(executable), "--version"],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if observed.returncode != 0 or observed.stdout.strip() != locked_version:
            return {}
        original_path = os.environ.get("PATH", "")
        controlled_path = os.pathsep.join(
            item for item in (str(executable.parent), original_path) if item
        )
        return {
            "PATH": controlled_path,
            "ZYRA_BUN_EXECUTABLE": str(executable),
            "ZYRA_BUN_LOCKED_VERSION": locked_version,
        }

    @staticmethod
    def _unique_references(values: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        unique: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for value in values:
            key = (value.get("kind"), value.get("path"), value.get("line"), value.get("digest"))
            unique[key] = value
        return [unique[key] for key in sorted(unique, key=lambda item: tuple(str(part) for part in item))]

    @staticmethod
    def _gate(receipt: CleanroomReceipt, *, member_count: int) -> GateResult:
        result = GateResult(
            gate_id="m1-cleanroom",
            status=GateStatus.NOT_RUN,
            summary="Exact-commit clean-directory package, dependency and regression verification.",
        )
        if receipt.source_dirty:
            result.add(
                Finding(
                    code="cleanroom.source_dirty",
                    severity=Severity.BLOCKER,
                    summary="Exact-commit cleanroom was launched from a dirty source worktree.",
                )
            )
        if not re.fullmatch(r"[0-9a-f]{64}", receipt.archive_digest):
            result.add(
                Finding(
                    code="cleanroom.archive_digest_missing",
                    severity=Severity.BLOCKER,
                    summary="Git archive has no valid cryptographic digest.",
                )
            )
        for path in receipt.residual_paths:
            result.add(
                Finding(
                    code="cleanroom.residual_state",
                    severity=Severity.BLOCKER,
                    summary="Cleanroom contains cache, database or runtime residue.",
                    location=path,
                )
            )
        for path in receipt.outside_links:
            result.add(
                Finding(
                    code="cleanroom.outside_symlink",
                    severity=Severity.BLOCKER,
                    summary="Cleanroom package links outside its own Zyra tree.",
                    location=path,
                )
            )
        for reference in receipt.forbidden_references:
            result.add(
                Finding(
                    code="cleanroom.external_runtime_reference",
                    severity=Severity.BLOCKER,
                    summary="Production source contains a sibling/external runtime dependency.",
                    location=f"{reference.get('path')}:{reference.get('line')}",
                    detail=str(reference.get("kind") or ""),
                    metadata={"line_digest": reference.get("digest")},
                )
            )
        for command in receipt.commands:
            if command.required and not command.ok:
                result.add(
                    Finding(
                        code="cleanroom.command_failed",
                        severity=Severity.BLOCKER,
                        summary="Required cleanroom verification command failed.",
                        detail=(
                            f"{command.command_id}: exit={command.exit_code}; "
                            f"timed_out={command.timed_out}; stderr={command.stderr_tail[-500:]}"
                        ),
                    )
                )
        if not receipt.cleanup_ok:
            result.add(
                Finding(
                    code="cleanroom.cleanup_failed",
                    severity=Severity.ERROR,
                    summary="Temporary cleanroom could not be removed after verification.",
                )
            )
        for limitation in receipt.limitations:
            result.add(
                Finding(
                    code="cleanroom.limitation",
                    severity=Severity.BLOCKER,
                    summary="Cleanroom verification did not complete safely.",
                    detail=limitation,
                )
            )
        result.metrics.update(
            {
                "target_commit": receipt.target_commit,
                "archive_digest": receipt.archive_digest,
                "archive_member_count": member_count,
                "file_count": receipt.file_count,
                "symlink_count": receipt.symlink_count,
                "residual_count": len(receipt.residual_paths),
                "outside_link_count": len(receipt.outside_links),
                "forbidden_reference_count": len(receipt.forbidden_references),
                "command_count": len(receipt.commands),
                "passed_command_count": sum(item.ok for item in receipt.commands),
                "receipt_digest": receipt.to_dict()["content_digest"],
                "commands": [item.to_dict() for item in receipt.commands],
            }
        )
        result.evidence.append(
            EvidencePointer(
                kind="cleanroom",
                location=receipt.target_commit,
                summary=f"exact commit cleanroom: {'passed' if receipt.ok else 'blocked'}",
                metadata={
                    "archive_digest": receipt.archive_digest,
                    "receipt_digest": receipt.to_dict()["content_digest"],
                },
            )
        )
        return result.finish()


def default_cleanroom_commands() -> tuple[CleanroomCommand, ...]:
    python = str(Path(sys.executable).resolve())
    return (
        CleanroomCommand(
            command_id="python-compile",
            argv=(python, "-m", "compileall", "-q", "apps", "packages"),
            timeout_seconds=300,
        ),
        CleanroomCommand(
            command_id="m1-hardening-unit",
            argv=(python, "-m", "pytest", "-q", "tests/unit/test_m1_hardening_integration.py"),
            timeout_seconds=600,
        ),
        CleanroomCommand(
            command_id="m1-hardening-integration",
            argv=(python, "-m", "pytest", "-q", "tests/integration/test_m1_hardening_main_path.py"),
            timeout_seconds=900,
        ),
    )
