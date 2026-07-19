from __future__ import annotations

"""Stable identifiers and conservative defaults for the sandbox gateway."""

GATEWAY_SCHEMA_VERSION = "zyra.sandbox-gateway.v1"
COMMAND_ENVELOPE_SCHEMA = "zyra.gateway-command-envelope.v1"
COMMAND_RECEIPT_SCHEMA = "zyra.gateway-command-receipt.v1"
FILE_ARTIFACT_SCHEMA = "zyra.gateway-file-artifact.v1"
PERMISSION_BINDING_SCHEMA = "zyra.gateway-permission-binding.v1"
CREDENTIAL_ENVELOPE_SCHEMA = "zyra.gateway-credential-envelope.v1"
PROVENANCE_SCHEMA = "zyra.gateway-provenance.v1"
PATCH_SET_SCHEMA = "zyra.gateway-patch-set.v1"
EVENT_SCHEMA = "zyra.gateway-event.v1"
STATE_SCHEMA = "zyra.gateway-state.v1"
SOURCE_CUSTODY_SCHEMA = "zyra.gateway-source-custody.v1"

GATEWAY_RUNTIME_ID = "SandboxGatewayRuntime"
GATEWAY_OWNER_UNIT = "M1-S05B"
GATEWAY_FOUNDATION_SLICE = "M1-S05B-01"
PERMISSION_OWNER = "typescript.PermissionCoordinator"
WORKSPACE_OWNER = "WorkspaceManagerRuntime"
WORKSPACE_EDIT_OWNER = "WorkspaceEditPort"
ARTIFACT_OWNER = "GatewayFileArtifactPort"

DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0
DEFAULT_CANCEL_GRACE_SECONDS = 3.0
DEFAULT_STDOUT_LIMIT_BYTES = 2 * 1024 * 1024
DEFAULT_STDERR_LIMIT_BYTES = 2 * 1024 * 1024
DEFAULT_COMBINED_OUTPUT_LIMIT_BYTES = 3 * 1024 * 1024
DEFAULT_MAX_PROCESSES = 32
DEFAULT_MAX_ENVIRONMENT_ENTRIES = 128
DEFAULT_MAX_ENVIRONMENT_VALUE_BYTES = 16 * 1024
DEFAULT_MAX_ARGUMENTS = 512
DEFAULT_MAX_ARGUMENT_BYTES = 64 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_ENTRIES = 10_000
DEFAULT_MAX_ARCHIVE_EXPANDED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_RATIO = 100.0
DEFAULT_MAX_PATCH_FILES = 2_000
DEFAULT_MAX_PATCH_BYTES = 128 * 1024 * 1024
DEFAULT_SESSION_LEASE_SECONDS = 300.0
DEFAULT_EVENT_HISTORY_LIMIT = 10_000
DEFAULT_QUEUE_DEPTH = 256

REDACTED = "[REDACTED]"
DIGEST_PREFIX = "sha256:"
TOKEN_DIGEST_PREFIX = "sha256:zyra-token:"
CONTENT_DIGEST_PREFIX = "sha256:zyra-content:"
COMMAND_DIGEST_PREFIX = "sha256:zyra-command:"
ENVIRONMENT_DIGEST_PREFIX = "sha256:zyra-environment:"
PROVENANCE_DIGEST_PREFIX = "sha256:zyra-provenance:"
APPROVAL_DIGEST_PREFIX = "sha256:zyra-gateway-approval:"
RECEIPT_DIGEST_PREFIX = "sha256:zyra-gateway-receipt:"

SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "private_key",
    "refresh_token",
    "secret",
    "session_token",
    "token",
)

UNTRUSTED_PROVENANCE_KINDS = frozenset(
    {
        "browser",
        "download",
        "mcp",
        "network",
        "remote_tool",
        "user_upload",
        "web",
    }
)

POLICY_CONTROL_PATH_NAMES = frozenset(
    {
        ".agents",
        ".codex",
        ".env",
        ".gitconfig",
        ".npmrc",
        ".pypirc",
        "agents.md",
        "permission.json",
        "permissions.json",
        "policy.json",
        "profiles.json",
        "settings.json",
        "tool-policy.json",
    }
)

ARCHIVE_SUFFIXES = frozenset(
    {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z"}
)
EXECUTABLE_SUFFIXES = frozenset(
    {".bat", ".cmd", ".com", ".dll", ".exe", ".msi", ".ps1", ".scr", ".sh"}
)

READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "blame",
        "branch",
        "diff",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "rev-list",
        "rev-parse",
        "show",
        "status",
        "tag",
    }
)

MUTATING_GIT_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "bisect",
        "checkout",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "fetch",
        "init",
        "merge",
        "mv",
        "pull",
        "push",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "submodule",
        "switch",
        "worktree",
    }
)

DESTRUCTIVE_GIT_SUBCOMMANDS = frozenset(
    {"clean", "push", "reset", "restore", "rm"}
)

NETWORK_EXECUTABLES = frozenset(
    {
        "curl",
        "ftp",
        "git",
        "Invoke-RestMethod",
        "Invoke-WebRequest",
        "nc",
        "netcat",
        "scp",
        "sftp",
        "ssh",
        "wget",
    }
)

SHELL_EXECUTABLES = frozenset(
    {
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "sh",
        "zsh",
    }
)

DEFAULT_ALLOWED_ENVIRONMENT_KEYS = frozenset(
    {
        "CI",
        "COLORTERM",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "TERM",
        "TMP",
        "TEMP",
        "TZ",
    }
)
