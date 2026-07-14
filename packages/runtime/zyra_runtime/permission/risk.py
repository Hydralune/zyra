from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePath
import re
from typing import Any


class RiskBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SafetyClass(StrEnum):
    LOW_RISK_READ = "low-risk-read"
    FAST_EDIT = "fast-edit"
    WORKSPACE_MUTATION = "workspace-mutation"
    EXTERNAL_SIDE_EFFECT = "external-side-effect"
    INTERACTIVE = "interactive"
    HARD_GUARD = "hard-guard"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    tool_name: str
    namespace: str | None = None
    server_name: str | None = None
    capabilities: frozenset[str] = frozenset()

    @property
    def normalized_name(self) -> str:
        return _normalize_name(self.tool_name)

    @property
    def is_remote(self) -> bool:
        return bool(self.server_name or self.namespace in {"mcp", "remote", "cloud"})

    @classmethod
    def coerce(cls, value: Any) -> "ToolIdentity":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(value)
        capabilities = _get(value, "capabilities", ()) or ()
        if isinstance(capabilities, str):
            capabilities = (capabilities,)
        name = _get(value, "tool_name", _get(value, "name", _get(value, "tool", "")))
        if not name:
            raise ValueError("tool identity requires a non-empty tool name")
        return cls(
            tool_name=str(name),
            namespace=_optional_text(_get(value, "namespace", None)),
            server_name=_optional_text(
                _get(value, "server_name", _get(value, "server", _get(value, "server_label", None)))
            ),
            capabilities=frozenset(_normalize_name(item) for item in capabilities),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceSafetyState:
    workspace_root: Path | None = None
    workspace_scoped: bool = False
    path_validated: bool = False
    read_before_write: bool = False
    baseline_current: bool = False
    bounded_change: bool = False
    trusted_remote: bool = False
    production: bool = False
    contains_secrets: bool = False
    external_egress: bool = False
    cross_repository: bool = False
    explicit_path_unsafe: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def coerce(cls, value: Any) -> "WorkspaceSafetyState":
        if isinstance(value, cls):
            return value
        value = value or {}
        root = _get(value, "workspace_root", _get(value, "root", None))
        root_path = Path(str(root)).resolve(strict=False) if root else None
        inside = _first_bool(
            value,
            "workspace_scoped",
            "inside_workspace",
            "path_within_workspace",
            "within_workspace",
        )
        path_validated = _first_bool(
            value,
            "path_validated",
            "path_safe",
            "safe_path",
            "resolved_path_safe",
        )
        explicit_unsafe = _any_explicit_false(
            value,
            "inside_workspace",
            "path_within_workspace",
            "within_workspace",
            "path_safe",
            "safe_path",
        ) or _first_bool(value, "outside_workspace", "path_escape", "path_traversal")
        return cls(
            workspace_root=root_path,
            workspace_scoped=inside,
            path_validated=path_validated,
            read_before_write=_first_bool(
                value,
                "read_before_write",
                "target_read",
                "read_observed",
                "read_version_known",
            ),
            baseline_current=_first_bool(
                value,
                "baseline_current",
                "stale_check_passed",
                "version_current",
                "etag_current",
            ),
            bounded_change=_first_bool(
                value,
                "bounded_change",
                "bounded_diff",
                "patch_bounded",
                "single_workspace_change",
            ),
            trusted_remote=_first_bool(value, "trusted_remote", "trusted_server", "preauthorized_remote"),
            production=_first_bool(value, "production", "is_production", "targets_production"),
            contains_secrets=_first_bool(value, "contains_secrets", "secret_material", "sensitive_payload"),
            external_egress=_first_bool(value, "external_egress", "egress", "external_destination"),
            cross_repository=_first_bool(value, "cross_repository", "cross_repo", "different_repository"),
            explicit_path_unsafe=explicit_unsafe,
            metadata={"provided": bool(value)},
        )


@dataclass(frozen=True, slots=True)
class ToolSpecificPermissionCheck:
    effect: str
    reason: str
    risk: RiskBand
    safety: SafetyClass
    interactive_required: bool = False
    bypass_immune: bool = False
    classifier_eligible: bool = False
    fast_edit_missing_prereqs: tuple[str, ...] = ()
    recovery_alternatives: tuple[str, ...] = ()
    rule_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def safety_critical(self) -> bool:
        return self.safety is SafetyClass.HARD_GUARD

    def as_base_decision(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "reason": self.reason,
            "risk": self.risk.value,
            "safety": self.safety.value,
            "interactive_required": self.interactive_required,
            "bypass_immune": self.bypass_immune,
            "classifier_eligible": self.classifier_eligible,
            "recovery_alternatives": self.recovery_alternatives,
            "rule_code": self.rule_code,
            "metadata": dict(self.metadata),
        }


_LOW_RISK_READ_NAMES = frozenset(
    {
        "read",
        "read_file",
        "file_read",
        "grep",
        "search",
        "text_search",
        "glob",
        "list_files",
        "directory_list",
        "lsp",
        "lsp_query",
        "symbol",
        "symbol_query",
        "artifact_read",
        "get_artifact",
        "event_query",
        "list_events",
        "task_metadata",
        "get_task_metadata",
    }
)

_EDIT_NAMES = frozenset(
    {
        "edit",
        "file_edit",
        "write",
        "write_file",
        "file_write",
        "apply_patch",
        "patch",
        "replace",
        "notebook_edit",
    }
)

_SHELL_NAMES = frozenset(
    {
        "bash",
        "shell",
        "shell_command",
        "terminal",
        "exec",
        "execute",
        "powershell",
        "pwsh",
        "command",
    }
)

_EXTERNAL_NAMES = frozenset(
    {
        "http",
        "http_request",
        "network",
        "web_request",
        "browser",
        "browser_action",
        "send_email",
        "send_message",
        "publish",
        "upload",
        "download",
        "mcp",
        "mcp_tool",
        "install",
        "package_install",
        "start_service",
        "stop_service",
        "memory_write",
        "shared_memory_write",
        "dispatch_worker",
    }
)

_INTERACTIVE_NAMES = frozenset(
    {
        "ask_user",
        "request_user_input",
        "prompt_user",
        "credential_prompt",
        "authenticate",
        "oauth",
    }
)

_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "python",
        "python3",
        "node",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
        "pwsh",
        "powershell",
        "cmd",
    }
)

_PATH_KEYS = frozenset(
    {
        "path",
        "paths",
        "file",
        "files",
        "file_path",
        "filepath",
        "target",
        "targets",
        "directory",
        "cwd",
        "destination_path",
    }
)


class ToolRiskPolicy:
    """Zyra-owned deterministic risk layer before rules, hooks, or classifiers.

    The policy consumes structured tool identity and workspace safety evidence.
    Text checks cover only narrow, unambiguously dangerous commands; an unknown
    shell expression remains an ask and is expected to receive parser/sandbox
    evidence in the execution integration slice.
    """

    FAST_EDIT_PREREQUISITES = (
        "workspace_scoped",
        "path_validated",
        "read_before_write",
        "baseline_current",
        "bounded_change",
    )

    def classify(
        self,
        identity: Any,
        args: Any,
        workspace_state: Any,
    ) -> ToolSpecificPermissionCheck:
        tool = ToolIdentity.coerce(identity)
        arguments = _mapping(args)
        state = WorkspaceSafetyState.coerce(workspace_state)

        hard_guard = self._hard_guard(tool, arguments, state)
        if hard_guard is not None:
            return hard_guard

        name = tool.normalized_name
        capabilities = tool.capabilities

        if name in _INTERACTIVE_NAMES or "interactive" in capabilities:
            return self._check(
                "ask",
                "tool requires live user interaction",
                RiskBand.HIGH,
                SafetyClass.INTERACTIVE,
                interactive_required=True,
                bypass_immune=True,
                classifier_eligible=False,
                recovery=("choose a non-interactive credential source", "defer the action to an interactive run"),
                code="interactive.required",
                tool=tool,
            )

        if tool.is_remote:
            return self._external_check(tool, state, "remote/MCP tool invocation")

        if name == "artifact_write" and tool.namespace in {"builtin", "zyra"}:
            return self._check(
                "allow",
                "Zyra-owned task artifact output stays inside the local artifact store",
                RiskBand.LOW,
                SafetyClass.LOW_RISK_READ,
                code="artifact.local_output",
                tool=tool,
            )

        # External/shared-state capabilities outrank the generic read-only
        # label.  A browser GET is still network egress and must never inherit
        # the local file-read fast path merely because the response is read.
        if name in _EXTERNAL_NAMES or capabilities.intersection(
            {"network", "external_side_effect", "package_install", "service_control", "shared_state"}
        ):
            return self._external_check(tool, state, "external or shared-state side effect")

        if name in _LOW_RISK_READ_NAMES or "read_only" in capabilities:
            return self._check(
                "allow",
                "explicit local low-risk read operation",
                RiskBand.LOW,
                SafetyClass.LOW_RISK_READ,
                code="read.low_risk",
                tool=tool,
            )

        if name in _EDIT_NAMES or "workspace_edit" in capabilities:
            return self._edit_check(tool, state)

        if name in _SHELL_NAMES or "shell" in capabilities:
            return self._check(
                "ask",
                "shell execution requires structured parser and sandbox evidence",
                RiskBand.HIGH,
                SafetyClass.WORKSPACE_MUTATION,
                bypass_immune=True,
                classifier_eligible=False,
                recovery=("use a typed read/edit tool", "narrow the command to a preauthorized operation"),
                code="shell.review",
                tool=tool,
            )

        if capabilities.intersection({"write", "mutation", "database_write", "worker_control"}):
            return self._check(
                "ask",
                "state-changing tool requires explicit policy review",
                RiskBand.HIGH,
                SafetyClass.WORKSPACE_MUTATION,
                classifier_eligible=True,
                recovery=("use a read-only inspection first", "narrow the mutation scope"),
                code="mutation.review",
                tool=tool,
            )

        return self._check(
            "ask",
            "unknown tool identity is not low-risk allowlisted",
            RiskBand.HIGH,
            SafetyClass.UNKNOWN,
            bypass_immune=True,
            classifier_eligible=False,
            recovery=("register the tool with typed capabilities", "use an explicitly allowlisted read operation"),
            code="tool.unknown",
            tool=tool,
        )

    def _edit_check(
        self,
        tool: ToolIdentity,
        state: WorkspaceSafetyState,
    ) -> ToolSpecificPermissionCheck:
        evidence = {
            "workspace_scoped": state.workspace_scoped,
            "path_validated": state.path_validated,
            "read_before_write": state.read_before_write,
            "baseline_current": state.baseline_current,
            "bounded_change": state.bounded_change,
        }
        missing = tuple(name for name in self.FAST_EDIT_PREREQUISITES if not evidence[name])
        if missing:
            return self._check(
                "ask",
                "workspace edit is missing fast-edit safety prerequisites",
                RiskBand.MEDIUM,
                SafetyClass.FAST_EDIT,
                bypass_immune=True,
                classifier_eligible=False,
                missing=missing,
                recovery=("read the target and refresh its version", "validate and bound the workspace path"),
                code="edit.prerequisites_missing",
                tool=tool,
            )
        return self._check(
            "allow",
            "all fast-edit safety prerequisites passed",
            RiskBand.MEDIUM,
            SafetyClass.FAST_EDIT,
            code="edit.fast_path",
            tool=tool,
        )

    def _external_check(
        self,
        tool: ToolIdentity,
        state: WorkspaceSafetyState,
        reason: str,
    ) -> ToolSpecificPermissionCheck:
        trusted_read = state.trusted_remote and "read_only" in tool.capabilities
        if trusted_read:
            return self._check(
                "allow",
                "preauthorized remote read-only capability",
                RiskBand.MEDIUM,
                SafetyClass.LOW_RISK_READ,
                code="remote.preauthorized_read",
                tool=tool,
            )
        return self._check(
            "ask",
            f"{reason} requires approval",
            RiskBand.HIGH,
            SafetyClass.EXTERNAL_SIDE_EFFECT,
            classifier_eligible=True,
            recovery=("use a local read-only alternative", "preauthorize the exact server and operation scope"),
            code="external.review",
            tool=tool,
        )

    def _hard_guard(
        self,
        tool: ToolIdentity,
        args: Mapping[str, Any],
        state: WorkspaceSafetyState,
    ) -> ToolSpecificPermissionCheck | None:
        name = tool.normalized_name
        operation = _normalize_name(_first_text(args, "operation", "action", "subcommand", "method"))
        command = _first_text(args, "command", "cmd", "script", "code")
        normalized_command = " ".join(command.lower().split())

        if state.explicit_path_unsafe or _paths_escape_workspace(args, state.workspace_root):
            return self._deny(
                tool,
                "resolved tool path escapes the authorized workspace",
                "hard.path_escape",
                ("choose a path inside the workspace", "request a separately scoped workspace capability"),
            )

        if _first_bool(args, "outside_workspace", "path_escape", "path_traversal"):
            return self._deny(
                tool,
                "tool arguments explicitly target outside the workspace",
                "hard.path_escape",
                ("choose a path inside the workspace",),
            )

        production = state.production or _is_production(args)
        if production and (name in {"deploy", "release", "publish"} or operation in {"deploy", "release"}):
            return self._deny(
                tool,
                "production deployment is outside autonomous permission scope",
                "hard.production_deploy",
                ("deploy to a staging target", "produce a reviewed deployment plan artifact"),
            )

        if production and _is_destructive_database(args, normalized_command):
            return self._deny(
                tool,
                "destructive production database operation is prohibited",
                "hard.production_database",
                ("run a read-only query", "prepare a reversible migration for review"),
            )

        secret_egress = (
            (state.contains_secrets or _first_bool(args, "contains_secrets", "secret_material", "credential_payload"))
            and (
                state.external_egress
                or tool.is_remote
                or name in _EXTERNAL_NAMES
                or bool(tool.capabilities.intersection({"network", "external_side_effect", "upload", "publish"}))
                or _first_bool(args, "external_egress", "external_destination")
            )
        )
        if secret_egress:
            return self._deny(
                tool,
                "secret-bearing payload cannot cross an external boundary",
                "hard.secret_exfiltration",
                ("redact or replace secrets with references", "use an approved secret broker"),
            )

        if _subagent_bypass_attempt(tool, args):
            return self._deny(
                tool,
                "subagent permission bypass cannot be delegated",
                "hard.subagent_bypass",
                ("inherit the parent permission policy", "delegate a narrower typed capability"),
            )

        if _broad_interpreter_allow(args):
            return self._deny(
                tool,
                "broad interpreter allow rule would suppress per-call safety checks",
                "hard.broad_interpreter_allow",
                ("authorize an exact command and argument digest", "use a typed tool capability"),
            )

        if _destructive_system_command(normalized_command):
            return self._deny(
                tool,
                "irreversible system command is prohibited",
                "hard.irreversible_system_command",
                ("target a disposable sandbox", "use a reversible workspace-scoped operation"),
            )

        if _destructive_git(normalized_command, state.cross_repository, args):
            return self._deny(
                tool,
                "destructive or cross-repository git operation is prohibited",
                "hard.destructive_git",
                ("create a new commit without rewriting history", "use a workspace-local branch"),
            )
        return None

    @staticmethod
    def _deny(
        tool: ToolIdentity,
        reason: str,
        code: str,
        recovery: tuple[str, ...],
    ) -> ToolSpecificPermissionCheck:
        return ToolRiskPolicy._check(
            "deny",
            reason,
            RiskBand.CRITICAL,
            SafetyClass.HARD_GUARD,
            bypass_immune=True,
            classifier_eligible=False,
            recovery=recovery,
            code=code,
            tool=tool,
        )

    @staticmethod
    def _check(
        effect: str,
        reason: str,
        risk: RiskBand,
        safety: SafetyClass,
        *,
        interactive_required: bool = False,
        bypass_immune: bool = False,
        classifier_eligible: bool = False,
        missing: tuple[str, ...] = (),
        recovery: tuple[str, ...] = (),
        code: str,
        tool: ToolIdentity,
    ) -> ToolSpecificPermissionCheck:
        return ToolSpecificPermissionCheck(
            effect=effect,
            reason=reason,
            risk=risk,
            safety=safety,
            interactive_required=interactive_required,
            bypass_immune=bypass_immune,
            classifier_eligible=classifier_eligible,
            fast_edit_missing_prereqs=missing,
            recovery_alternatives=recovery,
            rule_code=code,
            metadata={
                "tool_name": tool.tool_name,
                "namespace": tool.namespace,
                "server_name": tool.server_name,
            },
        )


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    exported = getattr(value, "model_dump", None)
    if callable(exported):
        result = exported()
        if isinstance(result, Mapping):
            return result
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        return attributes
    raise TypeError("tool arguments must be a mapping or structured object")


def _normalize_name(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _first_text(value: Any, *names: str) -> str:
    for name in names:
        found = _get(value, name, None)
        if found is not None and not isinstance(found, (Mapping, Sequence)):
            return str(found)
        if isinstance(found, str):
            return found
    return ""


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "safe", "passed"}
    return bool(value)


def _first_bool(value: Any, *names: str) -> bool:
    return any(_coerce_bool(_get(value, name, False)) for name in names)


def _any_explicit_false(value: Any, *names: str) -> bool:
    for name in names:
        found = _get(value, name, None)
        if found is not None and not _coerce_bool(found):
            return True
    return False


def _is_production(args: Mapping[str, Any]) -> bool:
    if _first_bool(args, "production", "is_production", "targets_production"):
        return True
    environment = _normalize_name(_first_text(args, "environment", "stage", "target_environment"))
    return environment in {"prod", "production", "live"}


def _is_destructive_database(args: Mapping[str, Any], command: str) -> bool:
    operation = _normalize_name(_first_text(args, "operation", "action", "statement_type"))
    if operation in {"drop", "truncate", "delete_all", "destroy_database"}:
        return True
    return bool(re.search(r"\b(drop\s+(database|schema|table)|truncate\s+table|delete\s+from\s+\S+\s*;?$)", command))


def _subagent_bypass_attempt(tool: ToolIdentity, args: Mapping[str, Any]) -> bool:
    subagent = tool.normalized_name in {"agent", "agent_tool", "task", "subagent", "dispatch_worker"} or bool(
        tool.capabilities.intersection({"subagent", "delegation", "worker_control"})
    )
    if not subagent:
        return False
    mode = _normalize_name(_first_text(args, "permission_mode", "mode", "permissions"))
    return mode in {"bypass", "bypass_permissions", "skip_permissions"} or _first_bool(
        args,
        "dangerously_skip_permissions",
        "bypass_permissions",
        "disable_permissions",
    )


def _broad_interpreter_allow(args: Mapping[str, Any]) -> bool:
    effect = _normalize_name(_first_text(args, "effect", "behavior", "rule_effect"))
    action = _normalize_name(_first_text(args, "operation", "action", "kind"))
    if effect != "allow" or action not in {
        "add_rule",
        "create_rule",
        "permission_rule",
        "permission_update",
        "update_permission",
    }:
        return False
    pattern = _first_text(args, "pattern", "rule", "rule_content").strip().lower()
    if pattern in {"*", "bash:*", "shell:*", "powershell:*"}:
        return True
    normalized = pattern.removesuffix(":*").removesuffix(" *").removesuffix("*").strip()
    return normalized.split(maxsplit=1)[0] in _INTERPRETERS if normalized else False


def _destructive_system_command(command: str) -> bool:
    if not command:
        return False
    patterns = (
        r"(^|[;&|]\s*)rm\s+-[^\n;&|]*r[^\n;&|]*f[^\n;&|]*\s+/(?:\s|$)",
        r"\bmkfs(?:\.[a-z0-9]+)?\s+/(?:dev|disk)/",
        r"\bdd\s+[^\n;&|]*\bof=/(?:dev|disk)/",
        r"\b(?:format|diskpart)\b[^\n;&|]*(?:/y|clean\s+all)",
        r"\bremove-item\b[^\n;&|]*-recurse[^\n;&|]*(?:[a-z]:\\|/)(?:\s|$)",
    )
    return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in patterns)


def _destructive_git(command: str, cross_repository: bool, args: Mapping[str, Any]) -> bool:
    if not command:
        operation = _normalize_name(_first_text(args, "operation", "action"))
        force = _first_bool(args, "force", "force_with_lease", "rewrite_history")
        return operation in {"reset_hard", "clean_force", "checkout_discard"} or (
            cross_repository and operation in {"push", "force_push"}
        ) or operation == "force_push" or (operation == "push" and force)
    destructive = (
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\s+-(?:[a-z]*f[a-z]*d|[a-z]*d[a-z]*f)[a-z]*\b",
        r"\bgit\s+checkout\s+--\s+",
        r"\bgit\s+restore\s+[^\n;&|]*--worktree\b",
        r"\bgit\s+push\b[^\n;&|]*(?:--force(?:-with-lease)?|-f)(?:\s|$)",
    )
    if any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in destructive):
        return True
    return cross_repository and bool(re.search(r"\bgit\s+push\b", command, flags=re.IGNORECASE))


def _paths_escape_workspace(args: Mapping[str, Any], workspace_root: Path | None) -> bool:
    if workspace_root is None:
        return False
    for path_text in _iter_path_values(args):
        if not path_text or _looks_like_url(path_text):
            continue
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = workspace_root / candidate
        try:
            candidate.resolve(strict=False).relative_to(workspace_root)
        except ValueError:
            return True
    return False


def _iter_path_values(value: Any, *, key: str | None = None) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            found.extend(_iter_path_values(child, key=_normalize_name(child_key)))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            found.extend(_iter_path_values(child, key=key))
    elif key in _PATH_KEYS and isinstance(value, (str, PurePath)):
        found.append(str(value))
    return found


def _looks_like_url(value: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", value, flags=re.IGNORECASE))


__all__ = [
    "RiskBand",
    "SafetyClass",
    "ToolIdentity",
    "ToolRiskPolicy",
    "ToolSpecificPermissionCheck",
    "WorkspaceSafetyState",
]
