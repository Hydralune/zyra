from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .canonical import digest, executable_name
from .constants import NETWORK_EXECUTABLES
from .models import CommandEffect, CommandEvidence, CommandRisk, GatewayCommandEnvelope

_URL = re.compile(r"(?i)\b(?:https?|ftp|ssh|git)://[^\s\"']+")
_HOST_TOKEN = re.compile(r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}$")


@dataclass(frozen=True, slots=True)
class NetworkTarget:
    scheme: str
    host: str
    port: int | None
    source: str
    private: bool
    loopback: bool
    link_local: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "source": self.source,
            "private": self.private,
            "loopback": self.loopback,
            "link_local": self.link_local,
        }


@dataclass(frozen=True, slots=True)
class NetworkPolicyResult:
    effect: CommandEffect
    profile: str
    targets: tuple[NetworkTarget, ...]
    evidence: tuple[CommandEvidence, ...]
    policy_digest: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect.value,
            "profile": self.profile,
            "targets": [item.to_dict() for item in self.targets],
            "evidence": [item.to_dict() for item in self.evidence],
            "policy_digest": self.policy_digest,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class NetworkProfile:
    profile_id: str
    allowed_schemes: frozenset[str] = frozenset({"https"})
    allowed_hosts: frozenset[str] = frozenset()
    denied_hosts: frozenset[str] = frozenset()
    allow_subdomains: bool = False
    allow_private: bool = False
    allow_loopback: bool = False
    allow_link_local: bool = False
    require_approval: bool = True
    maximum_targets: int = 8

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "allowed_schemes": sorted(self.allowed_schemes),
            "allowed_hosts": sorted(self.allowed_hosts),
            "denied_hosts": sorted(self.denied_hosts),
            "allow_subdomains": self.allow_subdomains,
            "allow_private": self.allow_private,
            "allow_loopback": self.allow_loopback,
            "allow_link_local": self.allow_link_local,
            "require_approval": self.require_approval,
            "maximum_targets": self.maximum_targets,
        }


class NetworkPolicy:
    def __init__(self, profiles: Iterable[NetworkProfile] = ()) -> None:
        offline = NetworkProfile(
            profile_id="offline",
            allowed_schemes=frozenset(),
            allowed_hosts=frozenset(),
            require_approval=False,
            maximum_targets=0,
        )
        self._profiles = {"offline": offline}
        for profile in profiles:
            self._profiles[profile.profile_id] = profile
        self.policy_digest = digest(
            {
                "profiles": [
                    self._profiles[key].to_dict()
                    for key in sorted(self._profiles)
                ]
            }
        )

    def register(self, profile: NetworkProfile) -> None:
        if profile.profile_id in self._profiles:
            raise ValueError(f"network profile already exists: {profile.profile_id}")
        self._profiles[profile.profile_id] = profile
        self.policy_digest = digest(
            {"profiles": [self._profiles[key].to_dict() for key in sorted(self._profiles)]}
        )

    def evaluate(self, envelope: GatewayCommandEnvelope) -> NetworkPolicyResult:
        targets = self.extract_targets(envelope)
        executable = executable_name(envelope.executable)
        network_capable = executable in {item.casefold() for item in NETWORK_EXECUTABLES}
        if executable in {"git", "git.exe"}:
            subcommand = next(
                (item.casefold() for item in envelope.argv if not item.startswith("-")),
                "",
            )
            network_capable = subcommand in {
                "clone",
                "fetch",
                "ls-remote",
                "pull",
                "push",
                "submodule",
            }
        if not targets and not network_capable:
            return NetworkPolicyResult(
                effect=CommandEffect.ALLOW,
                profile=envelope.network_profile,
                targets=(),
                evidence=(),
                policy_digest=self.policy_digest,
                metadata={"network_capable": False},
            )
        profile = self._profiles.get(envelope.network_profile)
        if profile is None:
            evidence = (
                CommandEvidence(
                    code="network.profile_unknown",
                    effect=CommandEffect.DENY,
                    reason="unknown network profile fails closed",
                    risk=CommandRisk.HIGH,
                    source="network_policy",
                ),
            )
            return NetworkPolicyResult(
                effect=CommandEffect.DENY,
                profile=envelope.network_profile,
                targets=targets,
                evidence=evidence,
                policy_digest=self.policy_digest,
            )
        evidence: list[CommandEvidence] = []
        if profile.profile_id == "offline":
            evidence.append(
                CommandEvidence(
                    code="network.offline",
                    effect=CommandEffect.DENY,
                    reason="network-capable command is blocked by the offline profile",
                    risk=CommandRisk.HIGH,
                    source="network_policy",
                )
            )
        if len(targets) > profile.maximum_targets:
            evidence.append(
                CommandEvidence(
                    code="network.target_limit",
                    effect=CommandEffect.DENY,
                    reason="command exceeds network target budget",
                    risk=CommandRisk.HIGH,
                    source="network_policy",
                )
            )
        for target in targets:
            effect, code, reason = self._target_effect(profile, target)
            evidence.append(
                CommandEvidence(
                    code=code,
                    effect=effect,
                    reason=reason,
                    risk=CommandRisk.HIGH if effect is not CommandEffect.ALLOW else CommandRisk.MEDIUM,
                    source="network_policy",
                    metadata={"host_digest": digest({"host": target.host}), "scheme": target.scheme},
                )
            )
        if network_capable and not targets and profile.profile_id != "offline":
            evidence.append(
                CommandEvidence(
                    code="network.target_unresolved",
                    effect=CommandEffect.ASK,
                    reason="network-capable command has no statically resolved target",
                    risk=CommandRisk.HIGH,
                    source="network_policy",
                )
            )
        aggregate = self._aggregate(item.effect for item in evidence)
        if aggregate is CommandEffect.ALLOW and profile.require_approval and targets:
            aggregate = CommandEffect.ASK
            evidence.append(
                CommandEvidence(
                    code="network.profile_approval",
                    effect=CommandEffect.ASK,
                    reason="network profile requires exact per-command approval",
                    risk=CommandRisk.MEDIUM,
                    source="network_policy",
                )
            )
        return NetworkPolicyResult(
            effect=aggregate,
            profile=profile.profile_id,
            targets=targets,
            evidence=tuple(evidence),
            policy_digest=self.policy_digest,
            metadata={"network_capable": network_capable},
        )

    def extract_targets(self, envelope: GatewayCommandEnvelope) -> tuple[NetworkTarget, ...]:
        values = [envelope.executable, *envelope.argv]
        targets: dict[tuple[str, str, int | None], NetworkTarget] = {}
        for value in values:
            text = str(value)
            for match in _URL.finditer(text):
                target = self._from_url(match.group(0), source="url")
                targets[(target.scheme, target.host, target.port)] = target
            if _HOST_TOKEN.match(text):
                target = self._target("", text, None, source="argv")
                targets[(target.scheme, target.host, target.port)] = target
        return tuple(
            sorted(targets.values(), key=lambda item: (item.host, item.scheme, item.port or 0))
        )

    def _target_effect(
        self,
        profile: NetworkProfile,
        target: NetworkTarget,
    ) -> tuple[CommandEffect, str, str]:
        if target.host.casefold() in {item.casefold() for item in profile.denied_hosts}:
            return CommandEffect.DENY, "network.host_denied", "network host is explicitly denied"
        if target.scheme and target.scheme.casefold() not in {
            item.casefold() for item in profile.allowed_schemes
        }:
            return CommandEffect.DENY, "network.scheme_denied", "network scheme is not allowlisted"
        if target.loopback and not profile.allow_loopback:
            return CommandEffect.DENY, "network.loopback_denied", "loopback network access is denied"
        if target.link_local and not profile.allow_link_local:
            return CommandEffect.DENY, "network.link_local_denied", "link-local network access is denied"
        if target.private and not profile.allow_private:
            return CommandEffect.DENY, "network.private_denied", "private network access is denied"
        if profile.allowed_hosts and not self._host_allowed(profile, target.host):
            return CommandEffect.DENY, "network.host_not_allowed", "network host is not allowlisted"
        return CommandEffect.ALLOW, "network.target_allowed", "network target matches the selected profile"

    @staticmethod
    def _host_allowed(profile: NetworkProfile, host: str) -> bool:
        normalized = host.rstrip(".").casefold()
        for allowed in profile.allowed_hosts:
            candidate = allowed.rstrip(".").casefold()
            if normalized == candidate:
                return True
            if profile.allow_subdomains and normalized.endswith("." + candidate):
                return True
        return False

    def _from_url(self, value: str, *, source: str) -> NetworkTarget:
        parsed = urlsplit(value)
        return self._target(parsed.scheme, parsed.hostname or "", parsed.port, source=source)

    @staticmethod
    def _target(scheme: str, host: str, port: int | None, *, source: str) -> NetworkTarget:
        normalized = host.rstrip(".").casefold()
        private = False
        loopback = normalized in {"localhost", "localhost.localdomain"}
        link_local = False
        try:
            address = ipaddress.ip_address(normalized.strip("[]"))
        except ValueError:
            pass
        else:
            private = address.is_private
            loopback = address.is_loopback
            link_local = address.is_link_local
        return NetworkTarget(
            scheme=scheme.casefold(),
            host=normalized,
            port=port,
            source=source,
            private=private,
            loopback=loopback,
            link_local=link_local,
        )

    @staticmethod
    def _aggregate(effects: Iterable[CommandEffect]) -> CommandEffect:
        values = set(effects)
        if CommandEffect.DENY in values:
            return CommandEffect.DENY
        if CommandEffect.ASK in values:
            return CommandEffect.ASK
        return CommandEffect.ALLOW
