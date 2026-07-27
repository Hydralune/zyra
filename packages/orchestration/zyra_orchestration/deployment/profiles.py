from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .errors import ConfigurationRejected
from .models import (
    DeploymentProfile,
    NetworkMode,
    ProfilePolicy,
    ResourceEnvelope,
    Sensitivity,
    digest,
)


PROFILE_POLICY_VERSION = "2026-07-27.1"
_LOOPBACKS = {"127.0.0.1", "::1", "localhost"}
_CAPABILITY_ALIASES = {
    "code": "code-execution",
    "browser": "browser-automation",
    "memory": "memory-retrieval",
    "checkpoint": "checkpoint-handoff",
    "provider": "provider-dispatch",
    "tool": "tool-execution",
}


def default_profile_policies(
    *,
    host: str = "127.0.0.1",
    base_port: int = 8310,
) -> dict[DeploymentProfile, ProfilePolicy]:
    return {
        DeploymentProfile.DEVICE: ProfilePolicy(
            profile=DeploymentProfile.DEVICE,
            host=host,
            port=base_port,
            network_mode=NetworkMode.OFFLINE,
            latency_budget_ms=25,
            resource=ResourceEnvelope(
                cpu_percent=35,
                memory_mb=384,
                max_concurrency=2,
                disk_mb=512,
            ),
            allowed_sensitivity=(
                Sensitivity.PUBLIC,
                Sensitivity.INTERNAL,
                Sensitivity.CONFIDENTIAL,
                Sensitivity.RESTRICTED,
            ),
            capabilities=(
                "local-compute",
                "code-execution",
                "checkpoint-handoff",
                "artifact-projection",
                "deterministic-transform",
            ),
            required=True,
        ),
        DeploymentProfile.EDGE: ProfilePolicy(
            profile=DeploymentProfile.EDGE,
            host=host,
            port=base_port + 1,
            network_mode=NetworkMode.PRIVATE,
            latency_budget_ms=90,
            resource=ResourceEnvelope(
                cpu_percent=55,
                memory_mb=768,
                max_concurrency=4,
                disk_mb=1024,
            ),
            allowed_sensitivity=(
                Sensitivity.PUBLIC,
                Sensitivity.INTERNAL,
                Sensitivity.CONFIDENTIAL,
            ),
            capabilities=(
                "edge-compute",
                "code-execution",
                "browser-automation",
                "checkpoint-handoff",
                "artifact-projection",
                "memory-retrieval",
                "deterministic-transform",
            ),
            required=True,
        ),
        DeploymentProfile.CLOUD: ProfilePolicy(
            profile=DeploymentProfile.CLOUD,
            host=host,
            port=base_port + 2,
            network_mode=NetworkMode.PUBLIC,
            latency_budget_ms=800,
            resource=ResourceEnvelope(
                cpu_percent=85,
                memory_mb=2048,
                max_concurrency=8,
                disk_mb=4096,
            ),
            allowed_sensitivity=(Sensitivity.PUBLIC, Sensitivity.INTERNAL),
            capabilities=(
                "cloud-compute",
                "code-execution",
                "provider-dispatch",
                "model-reasoning",
                "checkpoint-handoff",
                "artifact-projection",
                "memory-retrieval",
                "deterministic-transform",
            ),
            providers=("openai", "anthropic", "deepseek"),
            models=("reasoning", "general", "fast"),
            credential_environment=(
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "DEEPSEEK_API_KEY",
            ),
            required=True,
        ),
    }


class ProfileCatalog:
    def __init__(
        self,
        policies: Mapping[DeploymentProfile | str, ProfilePolicy],
        *,
        project_root: Path | str,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.environment = dict(os.environ if environment is None else environment)
        self._policies: dict[DeploymentProfile, ProfilePolicy] = {}
        for key, policy in policies.items():
            profile = key if isinstance(key, DeploymentProfile) else DeploymentProfile(str(key))
            if policy.profile is not profile:
                raise ConfigurationRejected(
                    "profile_identity_mismatch",
                    "profile map key does not match policy identity",
                    profile=profile.value,
                )
            self._policies[profile] = self._normalize(policy)
        self._validate_catalog()

    @classmethod
    def defaults(
        cls,
        project_root: Path | str,
        *,
        host: str = "127.0.0.1",
        base_port: int = 8310,
        environment: Mapping[str, str] | None = None,
    ) -> "ProfileCatalog":
        policies = default_profile_policies(host=host, base_port=base_port)
        return cls(policies, project_root=project_root, environment=environment)

    @classmethod
    def from_mapping(
        cls,
        document: Mapping[str, Any],
        *,
        project_root: Path | str,
        environment: Mapping[str, str] | None = None,
    ) -> "ProfileCatalog":
        unknown = set(document) - {"schema", "version", "profiles"}
        if unknown:
            raise ConfigurationRejected(
                "profile_document_unknown_key",
                "deployment profile document contains unknown keys",
                details={"keys": sorted(unknown)},
            )
        raw_profiles = document.get("profiles")
        if not isinstance(raw_profiles, Mapping):
            raise ConfigurationRejected(
                "profile_document_invalid",
                "deployment profile document requires a profiles mapping",
            )
        policies: dict[DeploymentProfile, ProfilePolicy] = {}
        for raw_name, raw in raw_profiles.items():
            if not isinstance(raw, Mapping):
                raise ConfigurationRejected(
                    "profile_entry_invalid",
                    "profile entry must be an object",
                    profile=str(raw_name),
                )
            profile = DeploymentProfile(str(raw_name))
            policies[profile] = cls._policy_from_mapping(profile, raw)
        return cls(policies, project_root=project_root, environment=environment)

    @staticmethod
    def _policy_from_mapping(
        profile: DeploymentProfile,
        raw: Mapping[str, Any],
    ) -> ProfilePolicy:
        allowed = {
            "host",
            "port",
            "network_mode",
            "latency_budget_ms",
            "resource",
            "allowed_sensitivity",
            "capabilities",
            "providers",
            "models",
            "credential_environment",
            "allow_checkpoint_export",
            "allow_checkpoint_import",
            "required",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ConfigurationRejected(
                "profile_unknown_key",
                "profile contains unknown keys",
                profile=profile.value,
                details={"keys": sorted(unknown)},
            )
        resource = raw.get("resource")
        if not isinstance(resource, Mapping):
            raise ConfigurationRejected(
                "profile_resource_invalid",
                "profile resource must be an object",
                profile=profile.value,
            )
        try:
            envelope = ResourceEnvelope(
                cpu_percent=int(resource.get("cpu_percent") or 0),
                memory_mb=int(resource.get("memory_mb") or 0),
                max_concurrency=int(resource.get("max_concurrency") or 0),
                disk_mb=int(resource.get("disk_mb") or 0),
            )
            return ProfilePolicy(
                profile=profile,
                host=str(raw.get("host") or ""),
                port=int(raw.get("port") or 0),
                network_mode=NetworkMode(str(raw.get("network_mode") or "")),
                latency_budget_ms=int(raw.get("latency_budget_ms") or 0),
                resource=envelope,
                allowed_sensitivity=tuple(
                    Sensitivity(str(item))
                    for item in raw.get("allowed_sensitivity") or ()
                ),
                capabilities=tuple(str(item) for item in raw.get("capabilities") or ()),
                providers=tuple(str(item) for item in raw.get("providers") or ()),
                models=tuple(str(item) for item in raw.get("models") or ()),
                credential_environment=tuple(
                    str(item) for item in raw.get("credential_environment") or ()
                ),
                allow_checkpoint_export=raw.get("allow_checkpoint_export", True) is True,
                allow_checkpoint_import=raw.get("allow_checkpoint_import", True) is True,
                required=raw.get("required", True) is True,
            )
        except (TypeError, ValueError) as error:
            raise ConfigurationRejected(
                "profile_value_invalid",
                "profile contains an invalid typed value",
                profile=profile.value,
                details={"error": str(error)},
            ) from error

    def _normalize(self, policy: ProfilePolicy) -> ProfilePolicy:
        capabilities: list[str] = []
        for raw in policy.capabilities:
            normalized = str(raw).strip().casefold().replace("_", "-")
            normalized = _CAPABILITY_ALIASES.get(normalized, normalized)
            if normalized and normalized not in capabilities:
                capabilities.append(normalized)
        allowed = tuple(dict.fromkeys(policy.allowed_sensitivity))
        providers = tuple(
            dict.fromkeys(str(item).strip().casefold() for item in policy.providers if str(item).strip())
        )
        models = tuple(
            dict.fromkeys(str(item).strip().casefold() for item in policy.models if str(item).strip())
        )
        credentials = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in policy.credential_environment
                if str(item).strip()
            )
        )
        return replace(
            policy,
            host=policy.host.strip(),
            capabilities=tuple(capabilities),
            allowed_sensitivity=allowed,
            providers=providers,
            models=models,
            credential_environment=credentials,
        )

    def _validate_catalog(self) -> None:
        required = set(DeploymentProfile)
        missing = required - set(self._policies)
        if missing:
            raise ConfigurationRejected(
                "required_profiles_missing",
                "device, edge and cloud profiles are all required",
                details={"missing": sorted(item.value for item in missing)},
            )
        endpoints: set[tuple[str, int]] = set()
        for profile, policy in self._policies.items():
            self._validate_policy(profile, policy)
            endpoint = (policy.host.casefold(), policy.port)
            if endpoint in endpoints:
                raise ConfigurationRejected(
                    "profile_endpoint_reused",
                    "profile endpoints must be independently addressable",
                    profile=profile.value,
                    details={"host": policy.host, "port": policy.port},
                )
            endpoints.add(endpoint)
        self._validate_behavioral_differences()

    def _validate_policy(
        self,
        profile: DeploymentProfile,
        policy: ProfilePolicy,
    ) -> None:
        if not policy.host or any(character.isspace() for character in policy.host):
            raise ConfigurationRejected(
                "profile_host_invalid",
                "profile host is empty or contains whitespace",
                profile=profile.value,
            )
        if not 1024 <= policy.port <= 65535:
            raise ConfigurationRejected(
                "profile_port_invalid",
                "profile port must be between 1024 and 65535",
                profile=profile.value,
                details={"port": policy.port},
            )
        if policy.latency_budget_ms < 1 or policy.latency_budget_ms > 120_000:
            raise ConfigurationRejected(
                "profile_latency_invalid",
                "profile latency budget is outside the supported range",
                profile=profile.value,
            )
        resource = policy.resource
        if not 1 <= resource.cpu_percent <= 100:
            raise ConfigurationRejected(
                "profile_cpu_invalid",
                "profile CPU percentage must be between 1 and 100",
                profile=profile.value,
            )
        if not 64 <= resource.memory_mb <= 262_144:
            raise ConfigurationRejected(
                "profile_memory_invalid",
                "profile memory limit must be between 64 MiB and 256 GiB",
                profile=profile.value,
            )
        if not 1 <= resource.max_concurrency <= 256:
            raise ConfigurationRejected(
                "profile_concurrency_invalid",
                "profile concurrency must be between 1 and 256",
                profile=profile.value,
            )
        if resource.disk_mb < 64:
            raise ConfigurationRejected(
                "profile_disk_invalid",
                "profile disk allowance must be at least 64 MiB",
                profile=profile.value,
            )
        if not policy.allowed_sensitivity:
            raise ConfigurationRejected(
                "profile_sensitivity_empty",
                "profile allows no sensitivity class",
                profile=profile.value,
            )
        if not policy.capabilities:
            raise ConfigurationRejected(
                "profile_capabilities_empty",
                "profile exposes no executable capability",
                profile=profile.value,
            )
        if profile is DeploymentProfile.CLOUD:
            if policy.network_mode is not NetworkMode.PUBLIC:
                raise ConfigurationRejected(
                    "cloud_network_invalid",
                    "cloud profile must use explicit public network mode",
                    profile=profile.value,
                )
            if Sensitivity.RESTRICTED in policy.allowed_sensitivity:
                raise ConfigurationRejected(
                    "cloud_restricted_data_invalid",
                    "cloud profile cannot admit restricted data",
                    profile=profile.value,
                )
            if "provider-dispatch" not in policy.capabilities:
                raise ConfigurationRejected(
                    "cloud_provider_capability_missing",
                    "cloud profile must expose provider dispatch",
                    profile=profile.value,
                )
            if not policy.credential_environment:
                raise ConfigurationRejected(
                    "cloud_credential_contract_missing",
                    "cloud profile must declare credential environment handles",
                    profile=profile.value,
                )
        if profile is DeploymentProfile.DEVICE and policy.network_mode is NetworkMode.PUBLIC:
            raise ConfigurationRejected(
                "device_public_network_invalid",
                "device profile cannot default to public network mode",
                profile=profile.value,
            )

    def _validate_behavioral_differences(self) -> None:
        device = self._policies[DeploymentProfile.DEVICE]
        edge = self._policies[DeploymentProfile.EDGE]
        cloud = self._policies[DeploymentProfile.CLOUD]
        signatures = {
            (
                policy.network_mode,
                policy.latency_budget_ms,
                policy.resource,
                policy.allowed_sensitivity,
                policy.capabilities,
                policy.providers,
            )
            for policy in (device, edge, cloud)
        }
        if len(signatures) != 3:
            raise ConfigurationRejected(
                "profile_behavior_not_isolated",
                "device, edge and cloud must have distinct behavioral policies",
            )
        if device.resource.memory_mb >= cloud.resource.memory_mb:
            raise ConfigurationRejected(
                "profile_resource_order_invalid",
                "cloud memory envelope must exceed device memory envelope",
            )
        if device.latency_budget_ms >= cloud.latency_budget_ms:
            raise ConfigurationRejected(
                "profile_latency_order_invalid",
                "device latency budget must be tighter than cloud latency budget",
            )
        if Sensitivity.RESTRICTED not in device.allowed_sensitivity:
            raise ConfigurationRejected(
                "device_restricted_data_missing",
                "device must remain capable of restricted-data execution",
            )
        if Sensitivity.RESTRICTED in edge.allowed_sensitivity:
            raise ConfigurationRejected(
                "edge_restricted_data_invalid",
                "restricted data must remain on device",
            )

    def policy(self, profile: DeploymentProfile | str) -> ProfilePolicy:
        key = profile if isinstance(profile, DeploymentProfile) else DeploymentProfile(str(profile))
        return self._policies[key]

    def profiles(self) -> tuple[DeploymentProfile, ...]:
        return tuple(profile for profile in DeploymentProfile if profile in self._policies)

    def policies(self) -> tuple[ProfilePolicy, ...]:
        return tuple(self._policies[profile] for profile in self.profiles())

    def credential_presence(
        self,
        profile: DeploymentProfile | str,
    ) -> dict[str, bool]:
        policy = self.policy(profile)
        return {
            name: bool(str(self.environment.get(name) or "").strip())
            for name in policy.credential_environment
        }

    def credential_ready(
        self,
        profile: DeploymentProfile | str,
        *,
        provider_required: bool = False,
        preferred_provider: str = "",
    ) -> bool:
        policy = self.policy(profile)
        if not policy.credential_environment:
            return not provider_required
        presence = self.credential_presence(profile)
        if not provider_required:
            return True
        provider = preferred_provider.strip().casefold()
        if provider:
            candidates = [
                name
                for name in policy.credential_environment
                if name.casefold().startswith(provider)
            ]
            return any(presence.get(name, False) for name in candidates)
        return any(presence.values())

    def public_projection(self) -> dict[str, Any]:
        profiles = [
            policy.public_dict(
                credential_presence=self.credential_presence(policy.profile)
            )
            for policy in self.policies()
        ]
        return {
            "schema": "zyra.deployment-profile-catalog/v1",
            "policy_version": PROFILE_POLICY_VERSION,
            "project_root": str(self.project_root),
            "profiles": profiles,
            "profile_digest": digest(profiles),
            "independent_process_required": True,
            "same_process_labels_accepted": False,
        }

    @property
    def profile_digest(self) -> str:
        return str(self.public_projection()["profile_digest"])

    def with_network_mode(
        self,
        profile: DeploymentProfile,
        mode: NetworkMode,
    ) -> "ProfileCatalog":
        policies = dict(self._policies)
        policies[profile] = replace(policies[profile], network_mode=mode)
        return ProfileCatalog(
            policies,
            project_root=self.project_root,
            environment=self.environment,
        )

    def endpoint_is_loopback(self, profile: DeploymentProfile) -> bool:
        return self.policy(profile).host.casefold() in _LOOPBACKS

    def source_environment(self, names: Sequence[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name in names:
            if name in self.environment:
                result[name] = self.environment[name]
        return result


__all__ = [
    "PROFILE_POLICY_VERSION",
    "ProfileCatalog",
    "default_profile_policies",
]
