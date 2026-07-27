from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    schema_version: int = 1
    source_date_epoch: int = 1_700_000_000
    maximum_file_size: int = 256 * 1024 * 1024
    maximum_bundle_size: int = 2 * 1024 * 1024 * 1024
    maximum_file_count: int = 100_000
    allow_native_extensions: frozenset[str] = frozenset(
        {".dll", ".exe", ".pyd", ".so", ".dylib", ".node", ".wasm"}
    )
    tracked_native_paths: frozenset[str] = frozenset()
    excluded_names: frozenset[str] = frozenset(
        {
            ".git",
            ".hg",
            ".svn",
            ".cache",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            ".tox",
            ".nox",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            "dist",
            "build",
            "tmp",
            ".tmp",
            "coverage",
            ".coverage",
        }
    )
    excluded_roots: frozenset[str] = frozenset(
        {
            "vendor",
            "vendor-runtimes",
            "third_party",
            "source-pool",
            "runtime-sources",
        }
    )
    excluded_suffixes: frozenset[str] = frozenset(
        {
            ".pyc",
            ".pyo",
            ".sqlite",
            ".sqlite3",
            ".db",
            ".log",
            ".tmp",
            ".bak",
            ".orig",
            ".rej",
        }
    )
    secret_name_fragments: frozenset[str] = frozenset(
        {
            "password",
            "passwd",
            "secret",
            "token",
            "api_key",
            "apikey",
            "private_key",
            "credential",
        }
    )
    required_root_files: frozenset[str] = frozenset(
        {
            "README.md",
            "pyproject.toml",
            "requirements.txt",
            "package.json",
            "bun.lock",
        }
    )
    allowed_archive_suffixes: frozenset[str] = frozenset(
        {".tar.gz", ".tgz", ".zip"}
    )
    mandatory_gates: frozenset[str] = frozenset(
        {
            "python-lock",
            "javascript-lock",
            "bundle-boundary",
            "checksums",
            "sbom-notice",
            "python-tests",
            "typescript-typecheck",
            "web-build",
            "source-custody",
            "submission-boundary",
            "clean-install",
            "semantic-health",
            "benchmark-link",
        }
    )
    preserved_state_paths: frozenset[str] = frozenset(
        {"state", "operator-data", "secrets"}
    )
    environment_allowlist: frozenset[str] = frozenset(
        {
            "PATH",
            "SystemRoot",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "TZ",
            "CI",
            "NO_COLOR",
        }
    )
    environment_prefix_allowlist: tuple[str, ...] = (
        "ZYRA_",
        "OPENAI_",
        "DEEPSEEK_",
    )
    source_repository_names: frozenset[str] = frozenset(
        {
            "claude-code-best",
            "browser-use",
            "openhands",
            "opencode",
            "langgraph",
            "agentscope",
            "agent-framework",
            "hermes-agent",
            "openclaw",
        }
    )
    extra_exclusions: frozenset[str] = field(default_factory=frozenset)

    def excluded(self, path: str) -> tuple[bool, str]:
        normalized = path.replace("\\", "/").strip("/")
        pure = PurePosixPath(normalized)
        parts = tuple(part.casefold() for part in pure.parts)
        if not parts:
            return False, ""
        if parts[0] in {item.casefold() for item in self.excluded_roots}:
            return True, "excluded_release_root"
        if parts[0] == "artifacts":
            return True, "generated_artifact_root"
        if any(part in {item.casefold() for item in self.excluded_names} for part in parts):
            return True, "cache_build_or_state_residue"
        if pure.suffix.casefold() in {
            item.casefold() for item in self.excluded_suffixes
        }:
            return True, "excluded_runtime_residue"
        folded = normalized.casefold()
        if pure.name.casefold().startswith(".env") and pure.name.casefold() != ".env.example":
            return True, "secret_environment_file_excluded"
        if folded in {item.casefold() for item in self.extra_exclusions}:
            return True, "explicit_release_exclusion"
        if folded.startswith("docs/reviews/evidence/"):
            return True, "generated_evidence_excluded"
        if folded.startswith("docs/reviews/") and folded.endswith(".json"):
            return True, "generated_review_excluded"
        return False, ""

    def environment_allowed(self, name: str) -> bool:
        if name in self.environment_allowlist:
            return True
        return any(name.startswith(prefix) for prefix in self.environment_prefix_allowlist)

    def secret_name(self, name: str) -> bool:
        folded = name.casefold()
        return any(fragment in folded for fragment in self.secret_name_fragments)


DEFAULT_RELEASE_POLICY = ReleasePolicy()


__all__ = ["DEFAULT_RELEASE_POLICY", "ReleasePolicy"]
