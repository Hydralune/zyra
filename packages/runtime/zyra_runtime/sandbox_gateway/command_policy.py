from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .canonical import (
    canonical_logical_path,
    digest,
    executable_name,
    is_path_like_argument,
)
from .constants import DEFAULT_ALLOWED_ENVIRONMENT_KEYS, SHELL_EXECUTABLES
from .git_policy import GitCommandPolicy, GitPolicyResult
from .models import (
    CommandEffect,
    CommandEvidence,
    CommandPolicyDecision,
    CommandRisk,
    GatewayCommandEnvelope,
)
from .network_policy import NetworkPolicy, NetworkPolicyResult

_SHELL_CONTROL = re.compile(r"(?:&&|\|\||[|;&<>\x60]|\$\(|\$\{|%\w+%|>\s*\S)")
_POWERSHELL_CONTROL = re.compile(
    r"(?i)(?:invoke-expression|iex\b|-encodedcommand|-enc\b|start-process|"
    r"new-object\s+net\.webclient|downloadstring|frombase64string)"
)
_COMMAND_SUBSTITUTION = re.compile(r"(?:\$\([^)]*\)|\x60[^\x60]*\x60)")
_ENV_REFERENCE = re.compile(r"(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%)")
_REDIRECTION = re.compile(r"(?:^|\s)(?:\d?>|>>|<|2>&1|\*>)")


class GatewayCommandRule(Protocol):
    rule_id: str

    def evaluate(self, envelope: GatewayCommandEnvelope) -> tuple[CommandEvidence, ...]:
        ...

    def descriptor(self) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class CommandPolicyConfig:
    allowed_executables: frozenset[str] = frozenset(
        {
            "cat",
            "find",
            "findstr",
            "get-childitem",
            "get-content",
            "git",
            "head",
            "node",
            "python",
            "python3",
            "rg",
            "sed",
            "sort",
            "tail",
            "type",
            "wc",
            "where",
            "which",
        }
    )
    denied_executables: frozenset[str] = frozenset(
        {
            "cipher",
            "diskpart",
            "fdisk",
            "format",
            "mkfs",
            "reboot",
            "reg",
            "regedit",
            "shutdown",
        }
    )
    destructive_executables: frozenset[str] = frozenset(
        {
            "del",
            "erase",
            "rmdir",
            "rm",
            "shred",
        }
    )
    interpreter_executables: frozenset[str] = frozenset(
        {
            "bash",
            "cmd",
            "cmd.exe",
            "node",
            "perl",
            "powershell",
            "powershell.exe",
            "pwsh",
            "python",
            "python3",
            "ruby",
            "sh",
        }
    )
    package_managers: frozenset[str] = frozenset(
        {
            "bun",
            "cargo",
            "composer",
            "gem",
            "go",
            "npm",
            "npx",
            "pip",
            "pip3",
            "pnpm",
            "poetry",
            "uv",
            "yarn",
        }
    )
    allowed_environment_keys: frozenset[str] = DEFAULT_ALLOWED_ENVIRONMENT_KEYS
    allow_unknown_with_approval: bool = True
    allow_read_only_without_human: bool = True
    sealed_mode_denies_ask: bool = True
    deny_direct_shell_strings: bool = False
    deny_destructive: bool = True
    require_permission_for_all_commands: bool = True
    maximum_argument_bytes: int = 64 * 1024
    maximum_arguments: int = 512

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_executables": sorted(self.allowed_executables),
            "denied_executables": sorted(self.denied_executables),
            "destructive_executables": sorted(self.destructive_executables),
            "interpreter_executables": sorted(self.interpreter_executables),
            "package_managers": sorted(self.package_managers),
            "allowed_environment_keys": sorted(self.allowed_environment_keys),
            "allow_unknown_with_approval": self.allow_unknown_with_approval,
            "allow_read_only_without_human": self.allow_read_only_without_human,
            "sealed_mode_denies_ask": self.sealed_mode_denies_ask,
            "deny_direct_shell_strings": self.deny_direct_shell_strings,
            "deny_destructive": self.deny_destructive,
            "require_permission_for_all_commands": self.require_permission_for_all_commands,
            "maximum_argument_bytes": self.maximum_argument_bytes,
            "maximum_arguments": self.maximum_arguments,
        }


class EnvelopeIntegrityRule:
    rule_id = "gateway.envelope_integrity"

    def __init__(self, config: CommandPolicyConfig) -> None:
        self.config = config

    def descriptor(self) -> Mapping[str, Any]:
        return {"rule_id": self.rule_id, "config_digest": digest(self.config.to_dict())}

    def evaluate(self, envelope: GatewayCommandEnvelope) -> tuple[CommandEvidence, ...]:
        evidence: list[CommandEvidence] = []
        if len(envelope.argv) > self.config.maximum_arguments:
            evidence.append(
                self._deny(
                    "command.argument_count",
                    "command exceeds the structured argument count limit",
                )
            )
        argument_bytes = sum(len(item.encode("utf-8")) for item in envelope.argv)
        if argument_bytes > self.config.maximum_argument_bytes:
            evidence.append(
                self._deny(
                    "command.argument_bytes",
                    "command exceeds the structured argument byte limit",
                )
            )
        if envelope.metadata.get("permission_bypass") or envelope.metadata.get("auto_approve"):
            evidence.append(
                CommandEvidence(
                    code="command.untrusted_override_ignored",
                    effect=CommandEffect.ASK,
                    reason="caller-supplied bypass and auto-approval metadata has no authority",
                    risk=CommandRisk.HIGH,
                    source=self.rule_id,
                )
            )
        if not envelope.tool_use_id:
            evidence.append(
                CommandEvidence(
                    code="command.tool_use_identity_missing",
                    effect=CommandEffect.DENY,
                    reason="command must bind to a stable tool_use_id",
                    risk=CommandRisk.HIGH,
                    source=self.rule_id,
                )
            )
        if envelope.workspace_id and envelope.owner_epoch <= 0:
            evidence.append(
                self._deny(
                    "command.workspace_epoch_missing",
                    "workspace-bound command requires a positive owner epoch",
                )
            )
        if envelope.workspace_id and not envelope.fence_digest:
            evidence.append(
                self._deny(
                    "command.workspace_fence_missing",
                    "workspace-bound command requires a non-secret fence digest",
                )
            )
        try:
            canonical_logical_path(envelope.cwd, allow_root=True)
        except Exception:
            evidence.append(
                self._deny(
                    "command.cwd_invalid",
                    "command cwd is not a canonical workspace-relative path",
                )
            )
        return tuple(evidence)

    def _deny(self, code: str, reason: str) -> CommandEvidence:
        return CommandEvidence(
            code=code,
            effect=CommandEffect.DENY,
            reason=reason,
            risk=CommandRisk.HIGH,
            source=self.rule_id,
        )


class ExecutableRule:
    rule_id = "gateway.executable"

    def __init__(self, config: CommandPolicyConfig) -> None:
        self.config = config

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "rule_id": self.rule_id,
            "allowed": sorted(self.config.allowed_executables),
            "denied": sorted(self.config.denied_executables),
            "destructive": sorted(self.config.destructive_executables),
        }

    def evaluate(self, envelope: GatewayCommandEnvelope) -> tuple[CommandEvidence, ...]:
        name = executable_name(envelope.executable)
        if name in self.config.denied_executables:
            return (
                CommandEvidence(
                    code="executable.hard_denied",
                    effect=CommandEffect.DENY,
                    reason=f"executable is denied by deployment policy: {name}",
                    risk=CommandRisk.CRITICAL,
                    source=self.rule_id,
                ),
            )
        if name in self.config.destructive_executables:
            effect = CommandEffect.DENY if self.config.deny_destructive else CommandEffect.ASK
            return (
                CommandEvidence(
                    code="executable.destructive",
                    effect=effect,
                    reason=f"destructive executable cannot run without an elevated policy: {name}",
                    risk=CommandRisk.CRITICAL,
                    source=self.rule_id,
                ),
            )
        if name in self.config.allowed_executables:
            return (
                CommandEvidence(
                    code="executable.registered",
                    effect=CommandEffect.ALLOW,
                    reason=f"executable is registered by deployment policy: {name}",
                    risk=CommandRisk.LOW,
                    source=self.rule_id,
                ),
            )
        effect = (
            CommandEffect.ASK
            if self.config.allow_unknown_with_approval
            else CommandEffect.DENY
        )
        return (
            CommandEvidence(
                code="executable.unknown",
                effect=effect,
                reason=f"unregistered executable fails to explicit approval: {name}",
                risk=CommandRisk.HIGH,
                source=self.rule_id,
            ),
        )


class EnvironmentRule:
    rule_id = "gateway.environment"

    def __init__(self, config: CommandPolicyConfig) -> None:
        self.config = config
        self._allowed = {item.casefold() for item in config.allowed_environment_keys}

    def descriptor(self) -> Mapping[str, Any]:
        return {"rule_id": self.rule_id, "allowed_keys": sorted(self._allowed)}

    def evaluate(self, envelope: GatewayCommandEnvelope) -> tuple[CommandEvidence, ...]:
        evidence: list[CommandEvidence] = []
        for key, value in envelope.environment.items():
            lowered = key.casefold()
            if lowered not in self._allowed:
                evidence.append(
                    CommandEvidence(
                        code="environment.key_not_allowed",
                        effect=CommandEffect.DENY,
                        reason=f"environment key must be supplied by a gateway profile: {key}",
                        risk=CommandRisk.HIGH,
                        source=self.rule_id,
                        metadata={"key": key},
                    )
                )
            if any(fragment in lowered for fragment in ("token", "secret", "password", "credential", "key")):
                evidence.append(
                    CommandEvidence(
                        code="environment.inline_credential",
                        effect=CommandEffect.DENY,
                        reason="credentials must use a single-use credential envelope",
                        risk=CommandRisk.CRITICAL,
                        source=self.rule_id,
                        metadata={"key": key},
                    )
                )
            if "\x00" in value:
                evidence.append(
                    CommandEvidence(
                        code="environment.nul",
                        effect=CommandEffect.DENY,
                        reason="environment value contains a NUL byte",
                        risk=CommandRisk.HIGH,
                        source=self.rule_id,
                    )
                )
        return tuple(evidence)


class ShellBoundaryRule:
    rule_id = "gateway.shell_boundary"

    def __init__(self, config: CommandPolicyConfig) -> None:
        self.config = config
        self._shells = {item.casefold() for item in SHELL_EXECUTABLES}

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "rule_id": self.rule_id,
            "shells": sorted(self._shells),
            "uses_python_permission_shell_analyzer": False,
            "requires_typescript_permission_receipt": True,
        }

    def evaluate(self, envelope: GatewayCommandEnvelope) -> tuple[CommandEvidence, ...]:
        name = executable_name(envelope.executable)
        if name not in self._shells:
            return self._inspect_non_shell_arguments(envelope.argv)
        evidence: list[CommandEvidence] = [
            CommandEvidence(
                code="shell.interpreter",
                effect=CommandEffect.ASK,
                reason="shell interpreter execution requires exact command approval",
                risk=CommandRisk.HIGH,
                source=self.rule_id,
            )
        ]
        command = self._extract_shell_command(name, envelope.argv)
        if not command:
            evidence.append(
                CommandEvidence(
                    code="shell.command_missing",
                    effect=CommandEffect.DENY,
                    reason="shell invocation must expose a command for analysis",
                    risk=CommandRisk.HIGH,
                    source=self.rule_id,
                )
            )
            return tuple(evidence)
        if self.config.deny_direct_shell_strings:
            evidence.append(
                CommandEvidence(
                    code="shell.string_disabled",
                    effect=CommandEffect.DENY,
                    reason="deployment policy disables direct shell command strings",
                    risk=CommandRisk.HIGH,
                    source=self.rule_id,
                )
            )
        if _COMMAND_SUBSTITUTION.search(command):
            evidence.append(
                CommandEvidence(
                    code="shell.command_substitution",
                    effect=CommandEffect.DENY,
                    reason="command substitution can mutate the approved execution graph",
                    risk=CommandRisk.CRITICAL,
                    source=self.rule_id,
                )
            )
        if _POWERSHELL_CONTROL.search(command):
            evidence.append(
                CommandEvidence(
                    code="shell.powershell_dynamic",
                    effect=CommandEffect.DENY,
                    reason="dynamic or encoded PowerShell execution is denied",
                    risk=CommandRisk.CRITICAL,
                    source=self.rule_id,
                )
            )
        if _REDIRECTION.search(command):
            evidence.append(
                CommandEvidence(
                    code="shell.redirection",
                    effect=CommandEffect.DENY,
                    reason="shell redirection bypasses WorkspaceEditPort transactions",
                    risk=CommandRisk.CRITICAL,
                    source=self.rule_id,
                )
            )
        if _ENV_REFERENCE.search(command):
            evidence.append(
                CommandEvidence(
                    code="shell.environment_expansion",
                    effect=CommandEffect.ASK,
                    reason="environment expansion changes execution material at runtime",
                    risk=CommandRisk.HIGH,
                    source=self.rule_id,
                )
            )
        evidence.extend(self._permission_shell_evidence(command, name))
        return tuple(evidence)

    def _permission_shell_evidence(
        self,
        command: str,
        name: str,
    ) -> tuple[CommandEvidence, ...]:
        # Python only supplies immutable gateway evidence.  Shell parsing and
        # the allow/deny/ask decision were cut over to PermissionCoordinator;
        # without its bound receipt the gateway remains approval-required.
        dialect = "powershell" if name in {"powershell", "powershell.exe", "pwsh"} else (
            "cmd" if name in {"cmd", "cmd.exe"} else "bash"
        )
        return (
            CommandEvidence(
                code="shell.typescript_permission_required",
                effect=CommandEffect.ASK,
                reason="shell execution requires an exact TypeScript permission receipt",
                risk=CommandRisk.HIGH,
                source=self.rule_id,
                metadata={
                    "command_digest": digest({"command": command, "dialect": dialect}),
                    "dialect": dialect,
                    "canonical_permission_owner": "typescript.PermissionCoordinator",
                    "python_decision_fallback": False,
                },
            ),
        )

    def _inspect_non_shell_arguments(
        self,
        argv: Sequence[str],
    ) -> tuple[CommandEvidence, ...]:
        evidence: list[CommandEvidence] = []
        for argument in argv:
            if "\x00" in argument:
                evidence.append(
                    CommandEvidence(
                        code="argument.nul",
                        effect=CommandEffect.DENY,
                        reason="structured argument contains a NUL byte",
                        risk=CommandRisk.HIGH,
                        source=self.rule_id,
                    )
                )
            if _SHELL_CONTROL.search(argument) and not argument.lstrip().startswith(("-", "/")):
                evidence.append(
                    CommandEvidence(
                        code="argument.shell_like_literal",
                        effect=CommandEffect.ASK,
                        reason="shell-like syntax in a structured argument requires explicit review",
                        risk=CommandRisk.MEDIUM,
                        source=self.rule_id,
                    )
                )
        return tuple(evidence)

    @staticmethod
    def _extract_shell_command(name: str, argv: Sequence[str]) -> str:
        if name in {"cmd", "cmd.exe"}:
            for index, value in enumerate(argv):
                if value.casefold() in {"/c", "/k"} and index + 1 < len(argv):
                    return " ".join(argv[index + 1 :])
            return ""
        for index, value in enumerate(argv):
            if value in {"-c", "-Command", "-command"} and index + 1 < len(argv):
                return " ".join(argv[index + 1 :])
            if value.casefold() in {"-encodedcommand", "-enc"}:
                return " ".join(argv[index:])
        return ""


class InterpreterRule:
    rule_id = "gateway.interpreter"

    def __init__(self, config: CommandPolicyConfig) -> None:
        self.config = config

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "rule_id": self.rule_id,
            "interpreters": sorted(self.config.interpreter_executables),
        }

    def evaluate(self, envelope: GatewayCommandEnvelope) -> tuple[CommandEvidence, ...]:
        name = executable_name(envelope.executable)
        if name not in self.config.interpreter_executables:
            return ()
        inline_flags = {"-c", "-e", "--eval", "-Command", "-command", "/c"}
        if any(argument in inline_flags for argument in envelope.argv):
            return (
                CommandEvidence(
                    code="interpreter.inline_program",
                    effect=CommandEffect.ASK,
                    reason="inline interpreter program is command material and requires exact approval",
                    risk=CommandRisk.HIGH,
                    source=self.rule_id,
                ),
            )
        script = next((item for item in envelope.argv if not item.startswith("-")), "")
        if script and is_path_like_argument(script):
            try:
                canonical_logical_path(script)
            except Exception:
                return (
                    CommandEvidence(
                        code="interpreter.script_path_escape",
                        effect=CommandEffect.DENY,
                        reason="interpreter script path escapes the workspace namespace",
                        risk=CommandRisk.CRITICAL,
                        source=self.rule_id,
                    ),
                )
        return ()


class PackageManagerRule:
    rule_id = "gateway.package_manager"

    _READ_ONLY = frozenset({"list", "ls", "outdated", "show", "tree", "why"})
    _EXECUTE = frozenset({"exec", "run", "run-script", "x", "dlx"})
    _INSTALL = frozenset({"add", "install", "i", "remove", "rm", "uninstall", "update", "upgrade"})
    _PUBLISH = frozenset({"login", "logout", "pack", "publish", "token"})

    def __init__(self, config: CommandPolicyConfig) -> None:
        self.config = config

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "rule_id": self.rule_id,
            "package_managers": sorted(self.config.package_managers),
            "read_only": sorted(self._READ_ONLY),
            "execute": sorted(self._EXECUTE),
            "install": sorted(self._INSTALL),
            "publish": sorted(self._PUBLISH),
        }

    def evaluate(self, envelope: GatewayCommandEnvelope) -> tuple[CommandEvidence, ...]:
        name = executable_name(envelope.executable)
        if name not in self.config.package_managers:
            return ()
        subcommand = next((item.casefold() for item in envelope.argv if not item.startswith("-")), "")
        if subcommand in self._PUBLISH:
            return (
                CommandEvidence(
                    code="package_manager.publish_or_auth",
                    effect=CommandEffect.DENY,
                    reason="package publication and authentication are outside the sandbox profile",
                    risk=CommandRisk.CRITICAL,
                    source=self.rule_id,
                ),
            )
        if subcommand in self._INSTALL:
            return (
                CommandEvidence(
                    code="package_manager.dependency_mutation",
                    effect=CommandEffect.DENY,
                    reason="dependency installation requires an explicit dependency-change workflow",
                    risk=CommandRisk.HIGH,
                    source=self.rule_id,
                ),
            )
        if subcommand in self._EXECUTE or name in {"npx"}:
            return (
                CommandEvidence(
                    code="package_manager.script_execution",
                    effect=CommandEffect.ASK,
                    reason="package-manager script can execute project-controlled code",
                    risk=CommandRisk.HIGH,
                    source=self.rule_id,
                ),
            )
        if subcommand in self._READ_ONLY:
            return (
                CommandEvidence(
                    code="package_manager.read_only",
                    effect=CommandEffect.ALLOW,
                    reason="package-manager query is classified as read-only",
                    risk=CommandRisk.LOW,
                    source=self.rule_id,
                ),
            )
        return (
            CommandEvidence(
                code="package_manager.unknown_subcommand",
                effect=CommandEffect.ASK,
                reason="unknown package-manager subcommand requires explicit approval",
                risk=CommandRisk.HIGH,
                source=self.rule_id,
            ),
        )


class StructuredCommandPolicy:
    """Gateway evidence aggregator; TypeScript remains permission authority."""

    def __init__(
        self,
        config: CommandPolicyConfig | None = None,
        *,
        network_policy: NetworkPolicy | None = None,
        git_policy: GitCommandPolicy | None = None,
        additional_rules: Iterable[GatewayCommandRule] = (),
    ) -> None:
        self.config = config or CommandPolicyConfig()
        self.network_policy = network_policy or NetworkPolicy()
        self.git_policy = git_policy or GitCommandPolicy()
        self.rules: tuple[GatewayCommandRule, ...] = (
            EnvelopeIntegrityRule(self.config),
            ExecutableRule(self.config),
            EnvironmentRule(self.config),
            ShellBoundaryRule(self.config),
            InterpreterRule(self.config),
            PackageManagerRule(self.config),
            *tuple(additional_rules),
        )
        self.policy_digest = digest(self.descriptor())

    def descriptor(self) -> dict[str, Any]:
        return {
            "policy_id": "zyra.structured-command-policy.v1",
            "final_authority": "typescript.PermissionCoordinator",
            "config": self.config.to_dict(),
            "rules": [dict(item.descriptor()) for item in self.rules],
            "network_policy_digest": self.network_policy.policy_digest,
            "git_policy_digest": self.git_policy.policy_digest,
            "caller_overrides": False,
        }

    def evaluate(
        self,
        envelope: GatewayCommandEnvelope,
        *,
        sealed: bool = False,
    ) -> CommandPolicyDecision:
        evidence: list[CommandEvidence] = []
        for rule in self.rules:
            try:
                evidence.extend(rule.evaluate(envelope))
            except Exception as error:
                evidence.append(
                    CommandEvidence(
                        code=f"{rule.rule_id}.failed",
                        effect=CommandEffect.DENY,
                        reason=f"command policy rule failed closed: {type(error).__name__}",
                        risk=CommandRisk.CRITICAL,
                        source=rule.rule_id,
                    )
                )
        git_result = self.git_policy.evaluate(envelope)
        network_result = self.network_policy.evaluate(envelope)
        evidence.extend(git_result.evidence)
        evidence.extend(network_result.evidence)
        effect = self._aggregate(item.effect for item in evidence)
        if sealed and effect is CommandEffect.ASK and self.config.sealed_mode_denies_ask:
            evidence.append(
                CommandEvidence(
                    code="sealed.ask_denied",
                    effect=CommandEffect.DENY,
                    reason="sealed autonomous mode deterministically denies approval-required commands",
                    risk=CommandRisk.HIGH,
                    source="gateway.sealed_policy",
                )
            )
            effect = CommandEffect.DENY
        if effect is CommandEffect.DENY:
            reason = "command policy denied one or more high-risk mechanisms"
            recovery = (
                "replace the command with a structured read-only operation",
                "route workspace mutations through WorkspaceEditPort",
                "select a deployment-authorized network or dependency workflow",
            )
        elif effect is CommandEffect.ASK:
            reason = "command requires an exact one-use permission decision"
            recovery = ("obtain an exact permission grant for the immutable envelope",)
        else:
            reason = "command satisfies the deterministic gateway evidence policy"
            recovery = ()
        read_only = self._read_only(evidence, git_result, network_result)
        eligible = (
            effect is CommandEffect.ALLOW
            and read_only
            and self.config.allow_read_only_without_human
        )
        return CommandPolicyDecision(
            effect=effect,
            reason=reason,
            evidence=tuple(evidence),
            command_digest=envelope.identity_digest,
            policy_digest=self.policy_digest,
            requires_permission_runtime=self.config.require_permission_for_all_commands,
            eligible_for_sealed_auto_allow=eligible,
            recovery=recovery,
            metadata={
                "sealed": sealed,
                "read_only": read_only,
                "git": git_result.to_dict(),
                "network": network_result.to_dict(),
                "final_authority": "typescript.PermissionCoordinator",
                "python_decision_fallback": False,
            },
        )

    def assert_unchanged(
        self,
        approved: GatewayCommandEnvelope,
        replay: GatewayCommandEnvelope,
    ) -> None:
        if approved.command_id != replay.command_id:
            raise ValueError("command_id changed after policy evaluation")
        if approved.identity_digest != replay.identity_digest:
            raise ValueError("command executable, argv, cwd, or environment changed after approval")
        if approved.permission_material() != replay.permission_material():
            raise ValueError("permission-bound command material changed after approval")

    @staticmethod
    def _aggregate(effects: Iterable[CommandEffect]) -> CommandEffect:
        values = set(effects)
        if CommandEffect.DENY in values:
            return CommandEffect.DENY
        if CommandEffect.ASK in values:
            return CommandEffect.ASK
        return CommandEffect.ALLOW

    @staticmethod
    def _read_only(
        evidence: Sequence[CommandEvidence],
        git_result: GitPolicyResult,
        network_result: NetworkPolicyResult,
    ) -> bool:
        if network_result.targets:
            return False
        if git_result.applicable and not bool(git_result.metadata.get("read_only")):
            return False
        disqualifying = {
            "shell.redirection",
            "executable.destructive",
            "package_manager.dependency_mutation",
            "package_manager.script_execution",
            "git.local_mutation",
            "git.destructive",
        }
        return not any(item.code in disqualifying for item in evidence)
