from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .canonical import digest, executable_name
from .constants import (
    DESTRUCTIVE_GIT_SUBCOMMANDS,
    MUTATING_GIT_SUBCOMMANDS,
    READ_ONLY_GIT_SUBCOMMANDS,
)
from .models import CommandEffect, CommandEvidence, CommandRisk, GatewayCommandEnvelope


@dataclass(frozen=True, slots=True)
class GitPolicyResult:
    applicable: bool
    subcommand: str
    effect: CommandEffect
    evidence: tuple[CommandEvidence, ...] = ()
    options: tuple[str, ...] = ()
    operands: tuple[str, ...] = ()
    policy_digest: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "subcommand": self.subcommand,
            "effect": self.effect.value,
            "evidence": [item.to_dict() for item in self.evidence],
            "options": list(self.options),
            "operands": list(self.operands),
            "policy_digest": self.policy_digest,
            "metadata": dict(self.metadata),
        }


class GitCommandPolicy:
    _GLOBAL_OPTIONS_WITH_VALUE = frozenset(
        {
            "-C",
            "-c",
            "--exec-path",
            "--git-dir",
            "--namespace",
            "--super-prefix",
            "--work-tree",
        }
    )
    _DANGEROUS_OPTIONS = frozenset(
        {
            "--delete",
            "--force",
            "-f",
            "--hard",
            "--mirror",
            "--prune",
            "--recurse-submodules",
        }
    )
    _NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "ls-remote", "pull", "push", "submodule"})

    def __init__(
        self,
        *,
        allow_read_only: bool = True,
        allow_local_mutation_with_approval: bool = True,
        deny_destructive: bool = True,
        deny_network: bool = True,
    ) -> None:
        self.allow_read_only = bool(allow_read_only)
        self.allow_local_mutation_with_approval = bool(allow_local_mutation_with_approval)
        self.deny_destructive = bool(deny_destructive)
        self.deny_network = bool(deny_network)
        self.policy_digest = digest(
            {
                "allow_read_only": self.allow_read_only,
                "allow_local_mutation_with_approval": self.allow_local_mutation_with_approval,
                "deny_destructive": self.deny_destructive,
                "deny_network": self.deny_network,
                "read_only": sorted(READ_ONLY_GIT_SUBCOMMANDS),
                "mutating": sorted(MUTATING_GIT_SUBCOMMANDS),
                "destructive": sorted(DESTRUCTIVE_GIT_SUBCOMMANDS),
            }
        )

    def evaluate(self, envelope: GatewayCommandEnvelope) -> GitPolicyResult:
        if executable_name(envelope.executable) not in {"git", "git.exe"}:
            return GitPolicyResult(
                applicable=False,
                subcommand="",
                effect=CommandEffect.ALLOW,
                policy_digest=self.policy_digest,
            )
        subcommand, options, operands = self._parse(envelope.argv)
        evidence: list[CommandEvidence] = []
        if not subcommand:
            evidence.append(
                CommandEvidence(
                    code="git.subcommand_missing",
                    effect=CommandEffect.ASK,
                    reason="git invocation has no explicit subcommand",
                    risk=CommandRisk.MEDIUM,
                    source="git_policy",
                )
            )
        elif subcommand in READ_ONLY_GIT_SUBCOMMANDS:
            effect = CommandEffect.ALLOW if self.allow_read_only else CommandEffect.ASK
            evidence.append(
                CommandEvidence(
                    code="git.read_only",
                    effect=effect,
                    reason=f"git {subcommand} is classified as read-only",
                    risk=CommandRisk.LOW,
                    source="git_policy",
                )
            )
        elif subcommand in MUTATING_GIT_SUBCOMMANDS:
            destructive = subcommand in DESTRUCTIVE_GIT_SUBCOMMANDS or any(
                option in self._DANGEROUS_OPTIONS for option in options
            )
            network = subcommand in self._NETWORK_SUBCOMMANDS
            if destructive and self.deny_destructive:
                effect = CommandEffect.DENY
                code = "git.destructive"
                reason = f"git {subcommand} can irreversibly alter repository state"
                risk = CommandRisk.CRITICAL
            elif network and self.deny_network:
                effect = CommandEffect.DENY
                code = "git.network"
                reason = f"git {subcommand} requires a separately authorized network profile"
                risk = CommandRisk.HIGH
            elif self.allow_local_mutation_with_approval:
                effect = CommandEffect.ASK
                code = "git.local_mutation"
                reason = f"git {subcommand} mutates repository state and requires exact approval"
                risk = CommandRisk.HIGH
            else:
                effect = CommandEffect.DENY
                code = "git.mutation_denied"
                reason = f"git {subcommand} is not enabled by deployment policy"
                risk = CommandRisk.HIGH
            evidence.append(
                CommandEvidence(
                    code=code,
                    effect=effect,
                    reason=reason,
                    risk=risk,
                    source="git_policy",
                    metadata={"subcommand": subcommand},
                )
            )
        else:
            evidence.append(
                CommandEvidence(
                    code="git.unknown_subcommand",
                    effect=CommandEffect.DENY,
                    reason=f"unknown git subcommand fails closed: {subcommand}",
                    risk=CommandRisk.HIGH,
                    source="git_policy",
                )
            )
        aggregate = self._aggregate(item.effect for item in evidence)
        return GitPolicyResult(
            applicable=True,
            subcommand=subcommand,
            effect=aggregate,
            evidence=tuple(evidence),
            options=tuple(options),
            operands=tuple(operands),
            policy_digest=self.policy_digest,
            metadata={
                "read_only": subcommand in READ_ONLY_GIT_SUBCOMMANDS,
                "mutating": subcommand in MUTATING_GIT_SUBCOMMANDS,
                "network": subcommand in self._NETWORK_SUBCOMMANDS,
            },
        )

    def _parse(self, argv: Sequence[str]) -> tuple[str, list[str], list[str]]:
        options: list[str] = []
        operands: list[str] = []
        subcommand = ""
        index = 0
        while index < len(argv):
            token = str(argv[index])
            if not subcommand and token.startswith("-"):
                options.append(token)
                if token in self._GLOBAL_OPTIONS_WITH_VALUE and index + 1 < len(argv):
                    options.append(str(argv[index + 1]))
                    index += 2
                    continue
                index += 1
                continue
            if not subcommand:
                subcommand = token.casefold()
            elif token.startswith("-"):
                options.append(token)
            else:
                operands.append(token)
            index += 1
        return subcommand, options, operands

    @staticmethod
    def _aggregate(effects: Sequence[CommandEffect] | Any) -> CommandEffect:
        values = set(effects)
        if CommandEffect.DENY in values:
            return CommandEffect.DENY
        if CommandEffect.ASK in values:
            return CommandEffect.ASK
        return CommandEffect.ALLOW
