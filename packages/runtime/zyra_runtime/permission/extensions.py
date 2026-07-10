from __future__ import annotations

"""Deployment-owned permission hook and classifier extensions.

This registry is the production composition boundary for deterministic,
in-process permission extensions.  It intentionally accepts only an immutable
deployment configuration.  Request metadata, model output, plugin manifests,
dynamic imports, and tool arguments cannot register callbacks or turn an
extension into an allow authority.

The callbacks created here have two legal effects:

* hooks may pass through, ask, or deny;
* classifiers may provide advisory evidence, but every allow is marked
  ineligible for automatic authorization.

The existing :mod:`zyra_runtime.permission.evaluator` remains the sole owner of
rule, safety, mode, and final-decision precedence.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any

from zyra_core import to_jsonable

from .classifier import (
    PermissionClassifierAdapter,
    PermissionClassifierFailurePolicy,
    PermissionClassifierInput,
    PermissionClassifierProposal,
    RegisteredPermissionClassifier,
)
from .hooks import (
    PermissionHookAdapter,
    PermissionHookFailureMode,
    PermissionHookInput,
    PermissionHookProposal,
    PermissionHookStage,
    RegisteredPermissionHook,
)
from .shell_analysis import (
    SHELL_ANALYZER_ID,
    SHELL_ANALYZER_VERSION,
    ShellAnalysisResult,
    ShellAnalyzerConfig,
    ShellCommandAnalyzer,
    inspect_secret_egress,
    is_shell_tool,
)


PERMISSION_EXTENSION_REGISTRY_ID = "zyra-deployment-permission-extensions"
PERMISSION_EXTENSION_REGISTRY_VERSION = "1"
_TRUSTED_SOURCE = "zyra_deployment"


class PermissionExtensionConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DeploymentPermissionExtensionConfig:
    """Immutable server/deployment configuration, never a request DTO."""

    shell_hook_enabled: bool = True
    secret_egress_hook_enabled: bool = True
    evidence_classifier_enabled: bool = True
    hook_timeout_seconds: float = 2.0
    classifier_timeout_seconds: float = 2.0
    shell_analyzer: ShellAnalyzerConfig = field(default_factory=ShellAnalyzerConfig)
    source: str = _TRUSTED_SOURCE
    request_configurable: bool = False
    dynamic_imports_enabled: bool = False

    def __post_init__(self) -> None:
        if self.source != _TRUSTED_SOURCE:
            raise PermissionExtensionConfigurationError(
                "permission extension source is fixed to the trusted deployment registry"
            )
        if self.request_configurable:
            raise PermissionExtensionConfigurationError(
                "permission extensions cannot be configured by a request"
            )
        if self.dynamic_imports_enabled:
            raise PermissionExtensionConfigurationError(
                "dynamic permission extension imports are prohibited"
            )
        if self.hook_timeout_seconds <= 0 or self.classifier_timeout_seconds <= 0:
            raise PermissionExtensionConfigurationError("extension timeouts must be positive")

    @property
    def digest(self) -> str:
        return _stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "shell_hook_enabled": self.shell_hook_enabled,
            "secret_egress_hook_enabled": self.secret_egress_hook_enabled,
            "evidence_classifier_enabled": self.evidence_classifier_enabled,
            "hook_timeout_seconds": self.hook_timeout_seconds,
            "classifier_timeout_seconds": self.classifier_timeout_seconds,
            "shell_analyzer": self.shell_analyzer.to_dict(),
            "source": self.source,
            "request_configurable": self.request_configurable,
            "dynamic_imports_enabled": self.dynamic_imports_enabled,
        }


@dataclass(frozen=True, slots=True)
class PermissionExtensionRegistry:
    hook_adapter: PermissionHookAdapter
    classifier_adapter: PermissionClassifierAdapter
    analyzer: ShellCommandAnalyzer
    config: DeploymentPermissionExtensionConfig

    @property
    def registry_digest(self) -> str:
        return _stable_digest(self.descriptor())

    def descriptor(self) -> dict[str, Any]:
        return {
            "registry_id": PERMISSION_EXTENSION_REGISTRY_ID,
            "version": PERMISSION_EXTENSION_REGISTRY_VERSION,
            "source": _TRUSTED_SOURCE,
            "configuration_digest": self.config.digest,
            "request_configurable": False,
            "dynamic_imports": False,
            "authoritative_allow": False,
            "final_authority": "PermissionPolicyEvaluator",
            "analyzer": self.analyzer.descriptor(),
            "hooks": self.hook_adapter.descriptors(),
            "classifiers": self.classifier_adapter.descriptors(),
        }

    def metadata(self) -> dict[str, str]:
        return {
            "permission_extension_registry_id": PERMISSION_EXTENSION_REGISTRY_ID,
            "permission_extension_registry_version": PERMISSION_EXTENSION_REGISTRY_VERSION,
            "permission_extension_registry_digest": self.registry_digest,
            "permission_extension_configuration_digest": self.config.digest,
            "permission_extension_hook_count": str(len(self.hook_adapter.list_hooks())),
            "permission_extension_classifier_count": str(
                len(self.classifier_adapter.list_classifiers())
            ),
            "permission_extension_request_configurable": "false",
            "permission_extension_dynamic_imports": "false",
            "permission_extension_authoritative_allow": "false",
        }

    def reject_request_overrides(self, overrides: Mapping[str, Any] | None) -> None:
        if overrides:
            keys = sorted(str(key) for key in overrides)
            raise PermissionExtensionConfigurationError(
                "request cannot configure permission extensions: " + ", ".join(keys)
            )


def build_deployment_permission_extensions(
    config: DeploymentPermissionExtensionConfig | None = None,
    *,
    request_overrides: Mapping[str, Any] | None = None,
) -> PermissionExtensionRegistry:
    """Build the trusted, static extension registry.

    ``request_overrides`` exists only so callers have an explicit fail-closed
    handoff for untrusted configuration.  Non-empty values are always rejected;
    they are never merged with the deployment policy.
    """

    selected = config or DeploymentPermissionExtensionConfig()
    if request_overrides:
        keys = sorted(str(key) for key in request_overrides)
        raise PermissionExtensionConfigurationError(
            "request cannot enable or replace permission extensions: " + ", ".join(keys)
        )
    analyzer = ShellCommandAnalyzer(selected.shell_analyzer)
    hook_adapter = PermissionHookAdapter(
        (
            RegisteredPermissionHook(
                hook_id="zyra.extension.secret-egress.pre-tool.v1",
                name="zyra-secret-egress-guard",
                callback=_secret_egress_hook,
                stage=PermissionHookStage.PRE_TOOL_USE,
                source=_TRUSTED_SOURCE,
                priority=10,
                timeout_seconds=selected.hook_timeout_seconds,
                failure_mode=PermissionHookFailureMode.FAIL_CLOSED,
                enabled=selected.secret_egress_hook_enabled,
                metadata={
                    "extension_kind": "deterministic_secret_egress_guard",
                    "authoritative_allow": "false",
                    "request_configurable": "false",
                },
            ),
            RegisteredPermissionHook(
                hook_id="zyra.extension.shell-structure.pre-tool.v1",
                name="zyra-shell-structure-guard",
                callback=_ShellStructureHook(analyzer),
                stage=PermissionHookStage.PRE_TOOL_USE,
                source=_TRUSTED_SOURCE,
                priority=20,
                timeout_seconds=selected.hook_timeout_seconds,
                failure_mode=PermissionHookFailureMode.FAIL_CLOSED,
                enabled=selected.shell_hook_enabled,
                metadata={
                    "extension_kind": "structured_shell_guard",
                    "analyzer_id": SHELL_ANALYZER_ID,
                    "analyzer_version": SHELL_ANALYZER_VERSION,
                    "analyzer_config_digest": analyzer.config_digest,
                    "authoritative_allow": "false",
                    "request_configurable": "false",
                },
            ),
        )
    )
    classifier_adapter = PermissionClassifierAdapter(
        (
            RegisteredPermissionClassifier(
                classifier_id="zyra.extension.deterministic-evidence.classifier.v1",
                name="zyra-deterministic-permission-evidence",
                callback=_DeterministicEvidenceClassifier(analyzer),
                source=_TRUSTED_SOURCE,
                model="",
                version=PERMISSION_EXTENSION_REGISTRY_VERSION,
                priority=10,
                timeout_seconds=selected.classifier_timeout_seconds,
                failure_policy=PermissionClassifierFailurePolicy.FAIL_CLOSED,
                enabled=selected.evidence_classifier_enabled,
                config={
                    "analyzer_id": SHELL_ANALYZER_ID,
                    "analyzer_version": SHELL_ANALYZER_VERSION,
                    "analyzer_config_digest": analyzer.config_digest,
                    "eligible_for_auto_allow": False,
                    "request_configurable": False,
                },
            ),
        )
    )
    return PermissionExtensionRegistry(
        hook_adapter=hook_adapter,
        classifier_adapter=classifier_adapter,
        analyzer=analyzer,
        config=selected,
    )


@dataclass(frozen=True, slots=True)
class _ShellStructureHook:
    analyzer: ShellCommandAnalyzer

    def __call__(self, hook_input: PermissionHookInput) -> PermissionHookProposal:
        if not is_shell_tool(hook_input.tool_name, hook_input.arguments):
            return PermissionHookProposal.passthrough(
                "structured shell hook does not apply to this tool"
            )
        analysis = self.analyzer.analyze_arguments(
            hook_input.arguments,
            tool_name=hook_input.tool_name,
            workspace_root=hook_input.workspace_root,
        )
        metadata = _shell_metadata(analysis)
        if analysis.hard_denied:
            return PermissionHookProposal.deny(
                "structured shell evidence contains a non-overridable boundary violation",
                metadata=metadata,
            )
        if analysis.requires_approval:
            return PermissionHookProposal.ask(
                "structured shell evidence requires deterministic policy review",
                metadata=metadata,
            )
        return PermissionHookProposal.passthrough(
            "structured shell evidence found no additional tightening condition"
        )


@dataclass(frozen=True, slots=True)
class _DeterministicEvidenceClassifier:
    analyzer: ShellCommandAnalyzer

    def __call__(
        self,
        classifier_input: PermissionClassifierInput,
    ) -> PermissionClassifierProposal:
        secret = inspect_secret_egress(
            classifier_input.arguments,
            tool_name=classifier_input.tool_name,
            server_name=classifier_input.server_name,
            command=_command_from_arguments(classifier_input.arguments),
        )
        if secret.hard_denied:
            return PermissionClassifierProposal.deny(
                "deterministic classifier found secret-bearing external egress",
                confidence="high",
                metadata={
                    "advisory_only": True,
                    "secret_egress": secret.to_dict(),
                    "authoritative_allow": False,
                },
            )
        if is_shell_tool(classifier_input.tool_name, classifier_input.arguments):
            analysis = self.analyzer.analyze_arguments(
                classifier_input.arguments,
                tool_name=classifier_input.tool_name,
            )
            metadata = {
                "advisory_only": True,
                "authoritative_allow": False,
                "shell_analysis": _shell_metadata(analysis),
            }
            if analysis.hard_denied:
                return PermissionClassifierProposal.deny(
                    "deterministic classifier found hard shell risk evidence",
                    confidence="high",
                    metadata=metadata,
                )
            if analysis.requires_approval:
                return PermissionClassifierProposal.ask(
                    "deterministic classifier requires review of shell structure",
                    metadata=metadata,
                )
        # A clean deterministic scan is useful evidence but never an automatic
        # authorization capability.  The adapter exposes can_auto_allow=False.
        return PermissionClassifierProposal.allow(
            "deterministic extension found no additional tightening evidence",
            confidence="medium",
            eligible_for_auto_allow=False,
            metadata={
                "advisory_only": True,
                "authoritative_allow": False,
                "secret_egress": secret.to_dict(),
            },
        )


def _secret_egress_hook(hook_input: PermissionHookInput) -> PermissionHookProposal:
    evidence = inspect_secret_egress(
        hook_input.arguments,
        tool_name=hook_input.tool_name,
        server_name=hook_input.server_name,
        command=_command_from_arguments(hook_input.arguments),
    )
    if evidence.hard_denied:
        return PermissionHookProposal.deny(
            "secret-bearing payload cannot cross an external permission boundary",
            metadata={
                "evidence_code": "secret.external_egress",
                "secret_egress": evidence.to_dict(),
                "authoritative_allow": False,
            },
        )
    return PermissionHookProposal.passthrough(
        "secret egress guard found no secret-bearing external boundary"
    )


def _shell_metadata(analysis: ShellAnalysisResult) -> dict[str, Any]:
    return {
        **analysis.policy_metadata(),
        "authoritative_allow": False,
        "advisory_or_tightening_only": True,
    }


def _command_from_arguments(arguments: Mapping[str, Any]) -> str:
    for key in ("command", "cmd", "script", "code", "shell_command"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return ""


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "DeploymentPermissionExtensionConfig",
    "PERMISSION_EXTENSION_REGISTRY_ID",
    "PERMISSION_EXTENSION_REGISTRY_VERSION",
    "PermissionExtensionConfigurationError",
    "PermissionExtensionRegistry",
    "build_deployment_permission_extensions",
]
