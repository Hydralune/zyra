from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import WorkspaceError, WorkspaceErrorCode
from .paths import WorkspacePathSafetyPolicy


READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "diff",
        "log",
        "ls-files",
        "rev-parse",
        "show",
        "status",
        "symbolic-ref",
    }
)

DENIED_GIT_OPTIONS = frozenset(
    {
        "--exec-path",
        "--ext-diff",
        "--output",
        "--paginate",
        "--path-format=absolute",
        "--upload-pack",
        "-c",
        "-C",
        "-P",
        "-p",
    }
)

SAFE_ENVIRONMENT_KEYS = (
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)


@dataclass(frozen=True, slots=True)
class GitQuery:
    operation: str
    arguments: tuple[str, ...] = ()
    relative_repository: str = "."
    timeout_seconds: float = 10.0
    max_output_bytes: int = 4 * 1024 * 1024
    accepted_return_codes: tuple[int, ...] = (0,)

    def __post_init__(self) -> None:
        if self.operation not in READ_ONLY_GIT_SUBCOMMANDS:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_REJECTED,
                "The requested git operation is not in the read-only workspace allowlist.",
                operation="git_query_validate",
                actual=self.operation,
            )
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_REJECTED,
                "Workspace git queries must use a bounded timeout.",
                operation="git_query_validate",
                actual=self.timeout_seconds,
            )
        if self.max_output_bytes < 1 or self.max_output_bytes > 64 * 1024 * 1024:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_REJECTED,
                "Workspace git query output bounds are invalid.",
                operation="git_query_validate",
                actual=self.max_output_bytes,
            )


@dataclass(frozen=True, slots=True)
class GitQueryResult:
    operation: str
    arguments: tuple[str, ...]
    repository: str
    return_code: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def stdout_text(self, encoding: str = "utf-8") -> str:
        return self.stdout.decode(encoding, errors="replace")

    def stderr_text(self, encoding: str = "utf-8") -> str:
        return self.stderr.decode(encoding, errors="replace")

    def lines(self) -> tuple[str, ...]:
        return tuple(self.stdout_text().splitlines())

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "arguments": list(self.arguments),
            "repository": self.repository,
            "return_code": self.return_code,
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
            "truncated": self.truncated,
            "metadata": dict(self.metadata),
        }


class WorkspaceGitBoundary:
    """Runs a closed set of non-mutating git inspections inside one workspace root."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        git_executable: str = "git",
        path_policy: WorkspacePathSafetyPolicy | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.git_executable = git_executable
        self.path_policy = path_policy

    def available(self) -> bool:
        executable = self._resolved_executable(required=False)
        return bool(executable)

    def query(self, request: GitQuery) -> GitQueryResult:
        executable = self._resolved_executable(required=True)
        repository = self._resolve_repository(request.relative_repository)
        arguments = self._validate_arguments(request.operation, request.arguments)
        command = [executable, "--no-pager", request.operation, *arguments]
        environment = self._environment()
        try:
            completed = subprocess.run(
                command,
                cwd=repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=request.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_FAILED,
                "The bounded workspace git query timed out.",
                retryable=True,
                operation=request.operation,
                path=request.relative_repository,
                expected=request.timeout_seconds,
            ) from error
        except OSError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_NOT_AVAILABLE,
                "Git could not be started for workspace inspection.",
                retryable=True,
                operation=request.operation,
                path=request.relative_repository,
                actual=type(error).__name__,
            ) from error
        stdout, stdout_truncated = _bounded_bytes(completed.stdout, request.max_output_bytes)
        stderr, stderr_truncated = _bounded_bytes(completed.stderr, min(request.max_output_bytes, 1024 * 1024))
        result = GitQueryResult(
            operation=request.operation,
            arguments=arguments,
            repository=self._public_repository_name(repository),
            return_code=int(completed.returncode),
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
            metadata={"allowlisted": True, "shell": False},
        )
        if completed.returncode not in request.accepted_return_codes:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_FAILED,
                "The read-only workspace git query failed.",
                operation=request.operation,
                path=request.relative_repository,
                expected=list(request.accepted_return_codes),
                actual=completed.returncode,
                metadata={
                    "stderr": result.stderr_text()[:4096],
                    "stdout": result.stdout_text()[:4096],
                    "truncated": result.truncated,
                },
            )
        return result

    def is_repository(self, relative_repository: str = ".") -> bool:
        try:
            result = self.query(
                GitQuery(
                    operation="rev-parse",
                    arguments=("--is-inside-work-tree",),
                    relative_repository=relative_repository,
                    accepted_return_codes=(0, 128),
                    max_output_bytes=4096,
                )
            )
        except WorkspaceError:
            return False
        return result.return_code == 0 and result.stdout_text().strip() == "true"

    def repository_root(self, relative_repository: str = ".") -> str:
        result = self.query(
            GitQuery(
                operation="rev-parse",
                arguments=("--show-toplevel",),
                relative_repository=relative_repository,
                max_output_bytes=64 * 1024,
            )
        )
        root = Path(result.stdout_text().strip()).resolve()
        try:
            relative = root.relative_to(self.workspace_root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_REJECTED,
                "The git repository root is outside the workspace boundary.",
                operation="git_repository_root",
                path=relative_repository,
            ) from error
        return relative.as_posix() or "."

    def head_commit(self, relative_repository: str = ".") -> str:
        result = self.query(
            GitQuery(
                operation="rev-parse",
                arguments=("--verify", "HEAD"),
                relative_repository=relative_repository,
                accepted_return_codes=(0, 128),
                max_output_bytes=4096,
            )
        )
        return result.stdout_text().strip() if result.return_code == 0 else ""

    def head_ref(self, relative_repository: str = ".") -> str:
        result = self.query(
            GitQuery(
                operation="symbolic-ref",
                arguments=("--quiet", "--short", "HEAD"),
                relative_repository=relative_repository,
                accepted_return_codes=(0, 1),
                max_output_bytes=4096,
            )
        )
        return result.stdout_text().strip()

    def tree_hash(self, relative_repository: str = ".") -> str:
        result = self.query(
            GitQuery(
                operation="rev-parse",
                arguments=("--verify", "HEAD^{tree}"),
                relative_repository=relative_repository,
                accepted_return_codes=(0, 128),
                max_output_bytes=4096,
            )
        )
        return result.stdout_text().strip() if result.return_code == 0 else ""

    def status_porcelain(self, relative_repository: str = ".") -> bytes:
        result = self.query(
            GitQuery(
                operation="status",
                arguments=("--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"),
                relative_repository=relative_repository,
                max_output_bytes=32 * 1024 * 1024,
            )
        )
        return result.stdout

    def tracked_files(self, relative_repository: str = ".") -> tuple[str, ...]:
        result = self.query(
            GitQuery(
                operation="ls-files",
                arguments=("-z",),
                relative_repository=relative_repository,
                max_output_bytes=32 * 1024 * 1024,
            )
        )
        return tuple(_decode_nul_paths(result.stdout))

    def diff(
        self,
        relative_repository: str = ".",
        *,
        paths: Sequence[str] = (),
        cached: bool = False,
        max_output_bytes: int = 8 * 1024 * 1024,
    ) -> GitQueryResult:
        safe_paths = tuple(self._validate_pathspec(item) for item in paths)
        arguments: list[str] = ["--no-ext-diff", "--binary", "--full-index"]
        if cached:
            arguments.append("--cached")
        if safe_paths:
            arguments.extend(("--", *safe_paths))
        return self.query(
            GitQuery(
                operation="diff",
                arguments=tuple(arguments),
                relative_repository=relative_repository,
                accepted_return_codes=(0, 1),
                max_output_bytes=max_output_bytes,
            )
        )

    def show_blob(
        self,
        object_name: str,
        *,
        relative_repository: str = ".",
        max_output_bytes: int = 16 * 1024 * 1024,
    ) -> GitQueryResult:
        safe_object = _validate_object_name(object_name)
        return self.query(
            GitQuery(
                operation="show",
                arguments=("--no-ext-diff", "--format=", safe_object),
                relative_repository=relative_repository,
                max_output_bytes=max_output_bytes,
            )
        )

    def _resolve_repository(self, relative_repository: str) -> Path:
        raw = str(relative_repository or ".").replace("\\", "/")
        if raw.startswith("/") or ":" in raw or ".." in Path(raw).parts:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_REJECTED,
                "Git repository paths must remain workspace relative.",
                operation="git_query_validate",
                path=raw,
            )
        candidate = self.workspace_root.joinpath(*Path(raw).parts).resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_REJECTED,
                "Git repository paths cannot escape the workspace.",
                operation="git_query_validate",
                path=raw,
            ) from error
        if not candidate.is_dir():
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "The workspace git repository path does not exist.",
                operation="git_query_validate",
                path=raw,
            )
        return candidate

    def _validate_arguments(self, operation: str, arguments: Iterable[str]) -> tuple[str, ...]:
        validated: list[str] = []
        for argument in arguments:
            value = str(argument)
            if "\x00" in value or "\r" in value or "\n" in value:
                raise WorkspaceError(
                    WorkspaceErrorCode.GIT_QUERY_REJECTED,
                    "Workspace git query arguments cannot contain control separators.",
                    operation=operation,
                )
            option_name = value.split("=", 1)[0]
            if value in DENIED_GIT_OPTIONS or option_name in DENIED_GIT_OPTIONS:
                raise WorkspaceError(
                    WorkspaceErrorCode.GIT_QUERY_REJECTED,
                    "A git option that can mutate state or execute external helpers was rejected.",
                    operation=operation,
                    actual=value,
                )
            if value.startswith("--config-env") or value.startswith("--super-prefix"):
                raise WorkspaceError(
                    WorkspaceErrorCode.GIT_QUERY_REJECTED,
                    "Workspace git query configuration injection was rejected.",
                    operation=operation,
                    actual=value,
                )
            validated.append(value)
        return tuple(validated)

    def _validate_pathspec(self, value: str) -> str:
        path = str(value or "").replace("\\", "/")
        if not path or path.startswith(("/", ":(")) or ":" in path or ".." in Path(path).parts:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_REJECTED,
                "Git pathspecs must be literal workspace-relative paths.",
                operation="git_pathspec_validate",
                path=path,
            )
        return f":(literal){path}"

    def _environment(self) -> dict[str, str]:
        environment = {key: os.environ[key] for key in SAFE_ENVIRONMENT_KEYS if key in os.environ}
        environment.update(
            {
                "GIT_CONFIG_COUNT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        for key in tuple(environment):
            upper = key.upper()
            if upper.startswith("GIT_") and upper not in {
                "GIT_CONFIG_COUNT",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_OPTIONAL_LOCKS",
                "GIT_PAGER",
                "GIT_TERMINAL_PROMPT",
            }:
                environment.pop(key, None)
        return environment

    def _resolved_executable(self, *, required: bool) -> str:
        executable = shutil.which(self.git_executable)
        if not executable and required:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_NOT_AVAILABLE,
                "Git is not available for workspace inspection.",
                operation="git_query",
            )
        return executable or ""

    def _public_repository_name(self, repository: Path) -> str:
        relative = repository.relative_to(self.workspace_root)
        return relative.as_posix() or "."


def _bounded_bytes(value: bytes, maximum: int) -> tuple[bytes, bool]:
    if len(value) <= maximum:
        return value, False
    return value[:maximum], True


def _decode_nul_paths(value: bytes) -> tuple[str, ...]:
    return tuple(
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in value.split(b"\x00")
        if item
    )


def _validate_object_name(value: str) -> str:
    candidate = str(value or "")
    if not candidate or len(candidate) > 512:
        raise WorkspaceError(
            WorkspaceErrorCode.GIT_QUERY_REJECTED,
            "Git object names must be non-empty and bounded.",
            operation="git_object_validate",
        )
    if candidate.startswith("-") or any(character in candidate for character in ("\x00", "\r", "\n", " ")):
        raise WorkspaceError(
            WorkspaceErrorCode.GIT_QUERY_REJECTED,
            "The requested git object name is unsafe.",
            operation="git_object_validate",
            actual=candidate,
        )
    return candidate
