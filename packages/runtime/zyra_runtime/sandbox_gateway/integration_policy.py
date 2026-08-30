from __future__ import annotations

import ipaddress
import math
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

from .canonical import digest
from .command_policy import StructuredCommandPolicy
from .file_policy import FileArtifactRequest, FilePolicyDecision, GatewayFilePolicy
from .models import (
    CommandEffect,
    CommandPolicyDecision,
    GatewayCommandEnvelope,
)
from .network_policy import NetworkPolicy
from .provenance import ArtifactProvenance, TrustLevel
from .integration_models import (
    GatewayAction,
    GatewayOutcome,
    GatewaySurface,
    TrustDisposition,
    canonical_value,
    content_digest,
    freeze_mapping,
)


_SHELL_OPERATORS = re.compile(r"(?:&&|\|\||[|;`]|\$\(|\r|\n|\x00)")
_REDIRECT_OPERATOR = re.compile(r"(?:^|\s)(?:>{1,2}|<{1,2}|2>|2>>|&>)(?:\s|$)")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_SECRET_KEY = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|credential|private[_-]?key|auth|cookie|session)",
    re.IGNORECASE,
)
_CONTROL_BASENAMES = frozenset(
    {
        "agents.md",
        "settings.json",
        "permissions.json",
        "permission.json",
        "policy.json",
        "tool-policy.json",
        ".gitconfig",
        ".npmrc",
        ".pypirc",
        ".env",
        "mcp.json",
        "mcp-config.json",
        "profile.ps1",
        ".bashrc",
        ".zshrc",
    }
)
_DESTRUCTIVE_GIT = frozenset(
    {
        "clean",
        "reset",
        "checkout",
        "restore",
        "rebase",
        "filter-branch",
        "gc",
        "prune",
        "push",
        "tag",
        "branch",
    }
)
_READ_ONLY_GIT = frozenset(
    {
        "status",
        "diff",
        "show",
        "log",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "cat-file",
        "grep",
        "remote",
        "config",
    }
)
_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "fish",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
    }
)
_SHELL_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "fish",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
    }
)
_NETWORK_EXECUTABLES = frozenset(
    {
        "curl",
        "wget",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "nc",
        "ncat",
        "telnet",
    }
)


def _contains_unquoted_shell_syntax(value: str) -> bool:
    quote = ""
    escaped = False
    index = 0
    while index < len(value):
        character = value[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        pair = value[index : index + 2]
        if pair in {"&&", "||", "$(", ">>", "<<", "2>", "&>"}:
            return True
        if character in {"|", ";", "`", ">", "<", "\r", "\n", "\x00"}:
            return True
        index += 1
    return False


@dataclass(frozen=True, slots=True)
class GatewayPolicyConfig:
    workspace_root: Path
    require_structured_argv: bool = True
    allow_legacy_command_strings: bool = True
    allow_shell_composition: bool = False
    default_command_network_profile: str = "offline"
    default_command_timeout_seconds: float = 120.0
    maximum_command_timeout_seconds: float = 43_200.0
    allow_read_only_git_without_approval: bool = True
    allow_public_https: bool = True
    allow_public_http: bool = False
    allow_private_network: bool = False
    allow_loopback_network: bool = False
    allow_file_urls: bool = False
    allowed_network_hosts: frozenset[str] = frozenset()
    denied_network_hosts: frozenset[str] = frozenset()
    maximum_url_chars: int = 8192
    maximum_argument_chars: int = 65536
    maximum_environment_keys: int = 128
    maximum_download_bytes: int = 128 * 1024 * 1024
    maximum_mcp_result_bytes: int = 16 * 1024 * 1024
    maximum_browser_result_bytes: int = 32 * 1024 * 1024
    trusted_mcp_servers: frozenset[str] = frozenset()
    denied_control_targets: frozenset[str] = _CONTROL_BASENAMES
    secret_environment_keys: frozenset[str] = frozenset()
    sealed: bool = False

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).resolve()
        object.__setattr__(self, "workspace_root", root)
        selected_network_profile = str(self.default_command_network_profile).strip()
        if not selected_network_profile:
            raise ValueError("default_command_network_profile is required")
        object.__setattr__(
            self,
            "default_command_network_profile",
            selected_network_profile,
        )
        for field_name in (
            "default_command_timeout_seconds",
            "maximum_command_timeout_seconds",
        ):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be a finite positive number")
            object.__setattr__(self, field_name, value)
        if self.maximum_command_timeout_seconds < self.default_command_timeout_seconds:
            raise ValueError(
                "maximum_command_timeout_seconds must be at least "
                "default_command_timeout_seconds"
            )
        for field_name in (
            "maximum_url_chars",
            "maximum_argument_chars",
            "maximum_environment_keys",
            "maximum_download_bytes",
            "maximum_mcp_result_bytes",
            "maximum_browser_result_bytes",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        object.__setattr__(
            self,
            "allowed_network_hosts",
            frozenset(_canonical_host(item) for item in self.allowed_network_hosts),
        )
        object.__setattr__(
            self,
            "denied_network_hosts",
            frozenset(_canonical_host(item) for item in self.denied_network_hosts),
        )
        object.__setattr__(
            self,
            "trusted_mcp_servers",
            frozenset(str(item).strip() for item in self.trusted_mcp_servers if str(item).strip()),
        )
        object.__setattr__(
            self,
            "denied_control_targets",
            frozenset(str(item).casefold() for item in self.denied_control_targets),
        )
        object.__setattr__(
            self,
            "secret_environment_keys",
            frozenset(str(item).casefold() for item in self.secret_environment_keys),
        )

    @property
    def policy_digest(self) -> str:
        return content_digest(self.safe_dict())

    def safe_dict(self) -> dict[str, Any]:
        return {
            "workspace_root_digest": content_digest(str(self.workspace_root)),
            "require_structured_argv": self.require_structured_argv,
            "allow_legacy_command_strings": self.allow_legacy_command_strings,
            "allow_shell_composition": self.allow_shell_composition,
            "default_command_network_profile": self.default_command_network_profile,
            "default_command_timeout_seconds": self.default_command_timeout_seconds,
            "maximum_command_timeout_seconds": self.maximum_command_timeout_seconds,
            "allow_read_only_git_without_approval": self.allow_read_only_git_without_approval,
            "allow_public_https": self.allow_public_https,
            "allow_public_http": self.allow_public_http,
            "allow_private_network": self.allow_private_network,
            "allow_loopback_network": self.allow_loopback_network,
            "allow_file_urls": self.allow_file_urls,
            "allowed_network_hosts": sorted(self.allowed_network_hosts),
            "denied_network_hosts": sorted(self.denied_network_hosts),
            "maximum_url_chars": self.maximum_url_chars,
            "maximum_argument_chars": self.maximum_argument_chars,
            "maximum_environment_keys": self.maximum_environment_keys,
            "maximum_download_bytes": self.maximum_download_bytes,
            "maximum_mcp_result_bytes": self.maximum_mcp_result_bytes,
            "maximum_browser_result_bytes": self.maximum_browser_result_bytes,
            "trusted_mcp_servers": sorted(self.trusted_mcp_servers),
            "denied_control_targets": sorted(self.denied_control_targets),
            "secret_environment_keys": sorted(self.secret_environment_keys),
            "sealed": self.sealed,
        }


@dataclass(frozen=True, slots=True)
class GatewayPolicyFinding:
    code: str
    severity: str
    reason: str
    hard_deny: bool = False
    requires_approval: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code or not self.reason:
            raise ValueError("policy finding code and reason are required")
        if self.severity not in {"info", "warning", "error", "critical"}:
            raise ValueError("unsupported policy finding severity")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "reason": self.reason,
            "hard_deny": self.hard_deny,
            "requires_approval": self.requires_approval,
            "metadata": canonical_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GatewaySurfacePolicyDecision:
    surface: GatewaySurface
    action: GatewayAction
    outcome: GatewayOutcome
    reason: str
    findings: tuple[GatewayPolicyFinding, ...]
    subject_digest: str
    policy_digest: str
    requires_permission: bool
    quarantine: bool = False
    normalized: Mapping[str, Any] = field(default_factory=dict)
    recovery: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface", GatewaySurface(self.surface))
        object.__setattr__(self, "action", GatewayAction(self.action))
        object.__setattr__(self, "outcome", GatewayOutcome(self.outcome))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "normalized", freeze_mapping(self.normalized))
        object.__setattr__(self, "recovery", tuple(str(item) for item in self.recovery))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def allowed(self) -> bool:
        return self.outcome in {GatewayOutcome.ALLOWED, GatewayOutcome.COMMITTED}

    @property
    def hard_denied(self) -> bool:
        return self.outcome == GatewayOutcome.DENIED and any(item.hard_deny for item in self.findings)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface.value,
            "action": self.action.value,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "findings": [item.safe_dict() for item in self.findings],
            "subject_digest": self.subject_digest,
            "policy_digest": self.policy_digest,
            "requires_permission": self.requires_permission,
            "quarantine": self.quarantine,
            "normalized": canonical_value(self.normalized),
            "recovery": list(self.recovery),
            "metadata": canonical_value(self.metadata),
        }


class GatewayPolicyRuntime:
    """Composes the 05B-01 policies without becoming a second permission owner."""

    def __init__(
        self,
        config: GatewayPolicyConfig,
        *,
        command_policy: StructuredCommandPolicy,
        file_policy: GatewayFilePolicy,
        network_policy: NetworkPolicy | None = None,
    ) -> None:
        self.config = config
        self.command_policy = command_policy
        self.file_policy = file_policy
        self.network_policy = network_policy

    @property
    def policy_digest(self) -> str:
        return content_digest(
            {
                "integration": self.config.safe_dict(),
                "command": self.command_policy.descriptor(),
                "file": self.file_policy.descriptor(),
            }
        )

    def evaluate_command(self, envelope: GatewayCommandEnvelope) -> GatewaySurfacePolicyDecision:
        findings = list(self._command_findings(envelope))
        command = self.command_policy.evaluate(envelope, sealed=self.config.sealed)
        findings.extend(_command_decision_findings(command))
        hard = [item for item in findings if item.hard_deny]
        asks = [item for item in findings if item.requires_approval]
        if hard or command.effect == CommandEffect.DENY:
            outcome = GatewayOutcome.DENIED
            reason = hard[0].reason if hard else command.reason
        elif command.effect == CommandEffect.ASK or asks:
            outcome = GatewayOutcome.PENDING
            reason = asks[0].reason if asks else command.reason
        else:
            outcome = GatewayOutcome.ALLOWED
            reason = command.reason
        return GatewaySurfacePolicyDecision(
            surface=GatewaySurface.CODE_WORKER,
            action=GatewayAction.COMMAND,
            outcome=outcome,
            reason=reason,
            findings=tuple(findings),
            subject_digest=content_digest(envelope.to_dict(include_environment=False)),
            policy_digest=self.policy_digest,
            requires_permission=command.requires_permission_runtime or bool(asks),
            normalized={
                "command_digest": command.command_digest,
                "executable": _executable_name(envelope.executable),
                "argv_digest": content_digest(envelope.argv),
                "cwd": envelope.cwd,
                "network_profile": envelope.network_profile,
                "base_policy_digest": command.policy_digest,
            },
            recovery=command.recovery,
            metadata={"base_effect": command.effect.value},
        )

    def revalidate_command(
        self,
        approved: GatewayCommandEnvelope,
        replay: GatewayCommandEnvelope,
        approved_decision: GatewaySurfacePolicyDecision,
    ) -> GatewaySurfacePolicyDecision:
        self.command_policy.assert_unchanged(approved, replay)
        replay_decision = self.evaluate_command(replay)
        if replay_decision.policy_digest != approved_decision.policy_digest:
            raise ValueError("gateway policy changed after approval")
        if replay_decision.subject_digest != approved_decision.subject_digest:
            raise ValueError("gateway command changed after approval")
        if replay_decision.outcome != approved_decision.outcome:
            raise ValueError("gateway command effect changed after approval")
        return replay_decision

    def evaluate_file(self, request: FileArtifactRequest) -> GatewaySurfacePolicyDecision:
        decision = self.file_policy.inspect(request)
        findings = [
            GatewayPolicyFinding(
                code=item.code,
                severity={
                    "deny": "critical",
                    "quarantine": "warning",
                }.get(item.severity, "info"),
                reason=item.reason,
                hard_deny=not decision.allowed and not decision.quarantine,
                requires_approval=decision.quarantine,
                metadata=item.metadata,
            )
            for item in decision.findings
        ]
        control_target = _is_control_target(request.logical_path, self.config.denied_control_targets)
        untrusted = request.provenance.trust in {TrustLevel.UNTRUSTED, TrustLevel.QUARANTINED}
        if control_target and untrusted:
            findings.append(
                GatewayPolicyFinding(
                    code="untrusted_control_target",
                    severity="critical",
                    reason="untrusted content cannot modify runtime, permission, profile or MCP configuration",
                    hard_deny=True,
                    metadata={"logical_path": request.logical_path},
                )
            )
        hard = any(item.hard_deny for item in findings)
        quarantine = decision.quarantine and not hard
        if hard or not decision.allowed and not quarantine:
            outcome = GatewayOutcome.DENIED
        elif quarantine:
            outcome = GatewayOutcome.QUARANTINED
        else:
            outcome = GatewayOutcome.ALLOWED
        action = {
            "file_write": GatewayAction.FILE_WRITE,
            "file_edit": GatewayAction.FILE_EDIT,
            "file_delete": GatewayAction.FILE_DELETE,
            "artifact_publish": GatewayAction.ARTIFACT_PUBLISH,
        }.get(str(request.operation.value), GatewayAction.FILE_WRITE)
        return GatewaySurfacePolicyDecision(
            surface=GatewaySurface.ARTIFACT_PIPELINE,
            action=action,
            outcome=outcome,
            reason=_file_decision_reason(decision, findings),
            findings=tuple(findings),
            subject_digest=content_digest(
                {
                    "path": request.logical_path,
                    "content_digest": decision.content_digest,
                    "provenance": request.provenance.provenance_id,
                    "operation": request.operation.value,
                }
            ),
            policy_digest=self.policy_digest,
            requires_permission=action in {
                GatewayAction.FILE_WRITE,
                GatewayAction.FILE_EDIT,
                GatewayAction.FILE_DELETE,
            },
            quarantine=quarantine,
            normalized={
                "logical_path": decision.logical_path,
                "content_digest": decision.content_digest,
                "content_type": decision.detected_content_type,
                "executable": decision.executable,
                "archive": decision.archive,
                "control_target": decision.control_target,
                "base_policy_digest": decision.policy_digest,
            },
            recovery=("discard_output", "quarantine") if quarantine else (),
        )

    def evaluate_url(
        self,
        url: str,
        *,
        surface: GatewaySurface = GatewaySurface.BROWSER_WORKER,
        allowed_schemes: Iterable[str] = (),
        allowed_hosts: Iterable[str] = (),
        trusted: bool = False,
    ) -> GatewaySurfacePolicyDecision:
        findings: list[GatewayPolicyFinding] = []
        normalized_url = str(url or "").strip()
        candidate_url = normalized_url
        if not normalized_url:
            findings.append(_deny("url_missing", "URL is required"))
            candidate_url = "invalid://missing"
        elif len(normalized_url) > self.config.maximum_url_chars:
            findings.append(_deny("url_too_long", "URL exceeds the gateway budget"))
            candidate_url = normalized_url[: self.config.maximum_url_chars]
        try:
            parsed = urlparse(candidate_url)
            scheme = parsed.scheme.casefold()
            host = _canonical_host(parsed.hostname or "")
            port = parsed.port
        except ValueError:
            findings.append(_deny("url_invalid", "URL could not be parsed"))
            parsed = urlparse("invalid://missing")
            scheme = ""
            host = ""
            port = None
        configured_schemes = {str(item).casefold() for item in allowed_schemes}
        if configured_schemes and scheme not in configured_schemes:
            findings.append(_deny("scheme_not_allowed", f"URL scheme {scheme!r} is not allowed"))
        if scheme == "https" and not self.config.allow_public_https:
            findings.append(_deny("https_disabled", "HTTPS access is disabled by gateway policy"))
        elif scheme == "http" and not self.config.allow_public_http:
            findings.append(_deny("plaintext_http_denied", "plaintext HTTP is denied by default"))
        elif scheme == "file" and not self.config.allow_file_urls:
            findings.append(_deny("file_url_denied", "file URLs cannot bypass the workspace gateway"))
        elif scheme not in {"https", "http", "workspace", "file"}:
            findings.append(_deny("unsupported_url_scheme", f"unsupported URL scheme {scheme!r}"))
        requested_hosts = {_canonical_host(item) for item in allowed_hosts if str(item).strip()}
        if scheme in {"http", "https"}:
            if not host:
                findings.append(_deny("network_host_missing", "network URL must include a host"))
            if host in self.config.denied_network_hosts:
                findings.append(_deny("network_host_denied", "network host is explicitly denied"))
            if self.config.allowed_network_hosts and host not in self.config.allowed_network_hosts:
                findings.append(_deny("network_host_not_configured", "network host is not in the deployment allowlist"))
            if requested_hosts and host not in requested_hosts:
                findings.append(_deny("network_host_not_requested", "network host is outside the task allowlist"))
            address = _literal_address(host)
            if address is not None:
                # ``ipaddress`` classifies loopback as private too.  Preserve
                # independent, least-authority grants instead of making a
                # narrow loopback grant depend on broad private-network access.
                if address.is_loopback:
                    if not self.config.allow_loopback_network:
                        findings.append(_deny("loopback_network_denied", "loopback targets are denied"))
                elif (address.is_private or address.is_link_local) and not self.config.allow_private_network:
                    findings.append(_deny("private_network_denied", "private and link-local targets are denied"))
            if parsed.username or parsed.password:
                findings.append(_deny("url_credentials_denied", "credentials cannot be embedded in URLs"))
        outcome = GatewayOutcome.DENIED if any(item.hard_deny for item in findings) else GatewayOutcome.ALLOWED
        if outcome == GatewayOutcome.ALLOWED and not trusted and scheme in {"http", "https"}:
            findings.append(
                GatewayPolicyFinding(
                    code="external_content_untrusted",
                    severity="info",
                    reason="external content remains untrusted after transport validation",
                )
            )
        return GatewaySurfacePolicyDecision(
            surface=surface,
            action=GatewayAction.BROWSER_FETCH,
            outcome=outcome,
            reason=findings[0].reason if outcome == GatewayOutcome.DENIED else "URL passed gateway policy",
            findings=tuple(findings),
            subject_digest=content_digest(
                {
                    "scheme": scheme,
                    "host": host,
                    "port": port,
                    "path_digest": content_digest(unquote(parsed.path)),
                }
            ),
            policy_digest=self.policy_digest,
            requires_permission=scheme in {"http", "https"},
            normalized={
                "scheme": scheme,
                "host": host,
                "port": port,
                "url_digest": content_digest(normalized_url),
            },
            recovery=("reduce_scope", "replan") if outcome == GatewayOutcome.DENIED else (),
        )

    def evaluate_mcp_call(
        self,
        *,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        external_boundary: bool,
    ) -> GatewaySurfacePolicyDecision:
        findings: list[GatewayPolicyFinding] = []
        canonical_server = str(server_id or "").strip()
        canonical_tool = str(tool_name or "").strip()
        if not canonical_server:
            findings.append(_deny("mcp_server_missing", "MCP server identity is required"))
        if not canonical_tool:
            findings.append(_deny("mcp_tool_missing", "MCP tool identity is required"))
        if not external_boundary:
            findings.append(_deny("mcp_boundary_confused", "MCP execution must retain external-boundary provenance"))
        if self.config.trusted_mcp_servers and canonical_server not in self.config.trusted_mcp_servers:
            findings.append(
                GatewayPolicyFinding(
                    code="mcp_server_untrusted",
                    severity="warning",
                    reason="MCP server is not deployment-trusted and requires exact approval",
                    requires_approval=True,
                )
            )
        dangerous = tuple(_find_control_mutations(arguments))
        findings.extend(dangerous)
        serialized = canonical_value(arguments)
        size = len(str(serialized).encode("utf-8"))
        if size > self.config.maximum_argument_chars:
            findings.append(_deny("mcp_arguments_too_large", "MCP arguments exceed the gateway budget"))
        hard = any(item.hard_deny for item in findings)
        asks = any(item.requires_approval for item in findings)
        outcome = GatewayOutcome.DENIED if hard else GatewayOutcome.PENDING if asks else GatewayOutcome.ALLOWED
        return GatewaySurfacePolicyDecision(
            surface=GatewaySurface.MCP_TOOL,
            action=GatewayAction.MCP_CALL,
            outcome=outcome,
            reason=(
                findings[0].reason
                if findings
                else "MCP call retains exact server, tool and argument provenance"
            ),
            findings=tuple(findings),
            subject_digest=content_digest(
                {
                    "server_id": canonical_server,
                    "tool_name": canonical_tool,
                    "arguments": arguments,
                }
            ),
            policy_digest=self.policy_digest,
            requires_permission=True,
            normalized={
                "server_id": canonical_server,
                "tool_name": canonical_tool,
                "arguments_digest": content_digest(arguments),
                "external_boundary": external_boundary,
            },
            recovery=("request_permission", "reduce_scope") if asks else (),
        )

    def evaluate_dispatch(self, envelope: Mapping[str, Any]) -> GatewaySurfacePolicyDecision:
        findings: list[GatewayPolicyFinding] = []
        backend = str(envelope.get("backend") or "").strip()
        location = str(envelope.get("location") or "").strip()
        sandbox = str(envelope.get("sandbox") or "").strip()
        gateway = str(envelope.get("gateway") or "").strip()
        workspace_root = str(envelope.get("workspace_root") or "").strip()
        if not backend:
            findings.append(_deny("backend_missing", "dispatch backend is required"))
        if not location:
            findings.append(_deny("location_missing", "dispatch location is required"))
        if sandbox.casefold() in {"", "none", "disabled", "yolo", "host-unrestricted"}:
            findings.append(_deny("sandbox_unenforced", "dispatch must select an enforced sandbox"))
        if gateway.casefold() in {"", "none", "disabled", "direct", "bypass"}:
            findings.append(_deny("gateway_unenforced", "dispatch cannot bypass the Zyra gateway"))
        if not workspace_root:
            findings.append(_deny("workspace_missing", "dispatch workspace is required"))
        else:
            candidate = Path(workspace_root).resolve()
            try:
                candidate.relative_to(self.config.workspace_root)
            except ValueError:
                findings.append(_deny("workspace_outside_custody", "dispatch workspace is outside gateway custody"))
        outcome = GatewayOutcome.DENIED if any(item.hard_deny for item in findings) else GatewayOutcome.ALLOWED
        return GatewaySurfacePolicyDecision(
            surface=GatewaySurface.REMOTE_WORKER,
            action=GatewayAction.BACKEND_DISPATCH,
            outcome=outcome,
            reason=findings[0].reason if findings else "dispatch is bound to an enforced gateway",
            findings=tuple(findings),
            subject_digest=content_digest(envelope),
            policy_digest=self.policy_digest,
            requires_permission=location.casefold() in {"edge", "cloud", "remote"},
            normalized={
                "backend": backend,
                "location": location,
                "sandbox": sandbox,
                "gateway": gateway,
                "workspace_digest": content_digest(workspace_root),
            },
            recovery=("replace_backend", "rebind_workspace") if findings else (),
        )

    def assert_path(self, value: str, *, allow_root: bool = False) -> str:
        raw = str(value or "").replace("\\", "/").strip()
        if not raw:
            if allow_root:
                return "."
            raise ValueError("logical path is required")
        if raw.startswith("/") or raw.startswith("//") or _WINDOWS_DRIVE.match(raw):
            raise ValueError("absolute, drive and UNC paths are denied")
        path = PurePosixPath(raw)
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("logical path must be normalized and traversal-free")
        if any("\x00" in part or ":" in part for part in path.parts):
            raise ValueError("logical path contains a reserved component")
        return path.as_posix()

    def assert_environment(self, environment: Mapping[str, str]) -> Mapping[str, str]:
        if len(environment) > self.config.maximum_environment_keys:
            raise ValueError("environment exceeds the gateway key budget")
        normalized: dict[str, str] = {}
        for key, value in environment.items():
            name = str(key).strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid environment key: {name!r}")
            if _SECRET_KEY.search(name) or name.casefold() in self.config.secret_environment_keys:
                raise ValueError(f"secret environment key must use CredentialRelay: {name}")
            text = str(value)
            if "\x00" in text:
                raise ValueError(f"environment value for {name} contains NUL")
            normalized[name] = text
        return MappingProxyType(normalized)

    def command_from_arguments(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[str, tuple[str, ...], Mapping[str, str], str]:
        executable = str(arguments.get("executable") or "").strip()
        raw_argv = arguments.get("argv")
        legacy = str(arguments.get("command") or "").strip()
        if executable:
            if raw_argv is None:
                argv: tuple[str, ...] = ()
            elif isinstance(raw_argv, Sequence) and not isinstance(raw_argv, (str, bytes, bytearray)):
                argv = tuple(str(item) for item in raw_argv)
            else:
                raise ValueError("argv must be a sequence of strings")
        elif legacy and self.config.allow_legacy_command_strings:
            executable, argv = self._parse_legacy_command(legacy)
        else:
            raise ValueError("structured executable and argv are required")
        if not executable or "\x00" in executable:
            raise ValueError("executable is invalid")
        if argv and _executable_name(argv[0]) == _executable_name(executable):
            raise ValueError(
                "argv contains arguments only and must not repeat executable"
            )
        if len(argv) > 512:
            raise ValueError("argv exceeds the gateway item budget")
        if sum(len(item) for item in argv) > self.config.maximum_argument_chars:
            raise ValueError("argv exceeds the gateway character budget")
        if any("\x00" in item for item in argv):
            raise ValueError("argv contains NUL")
        environment = self.assert_environment(
            {
                str(key): str(value)
                for key, value in dict(arguments.get("environment") or {}).items()
            }
        )
        cwd = self.assert_path(str(arguments.get("cwd") or "."), allow_root=True)
        return executable, argv, environment, cwd

    def _parse_legacy_command(self, command: str) -> tuple[str, tuple[str, ...]]:
        if _contains_unquoted_shell_syntax(command):
            if not self.config.allow_shell_composition:
                raise ValueError(
                    "shell operators and redirects require an explicit reviewed script artifact"
                )
            if len(command) > self.config.maximum_argument_chars:
                raise ValueError("command exceeds the gateway character budget")
            return "sh", ("-c", command)
        if len(command) > self.config.maximum_argument_chars:
            raise ValueError("command exceeds the gateway character budget")
        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError as error:
            raise ValueError(f"legacy command cannot be tokenized: {error}") from error
        cleaned = tuple(_strip_balanced_quotes(item) for item in tokens)
        if not cleaned:
            raise ValueError("command is empty")
        if _ENV_ASSIGNMENT.match(cleaned[0]):
            raise ValueError("inline environment assignment is denied")
        return cleaned[0], cleaned[1:]

    def _command_findings(self, envelope: GatewayCommandEnvelope) -> Iterable[GatewayPolicyFinding]:
        executable = _executable_name(envelope.executable)
        argv = tuple(str(item) for item in envelope.argv)
        if executable in _INTERPRETERS:
            nested = _interpreter_payload(executable, argv)
            if nested is not None:
                if executable in _SHELL_INTERPRETERS and (
                    _SHELL_OPERATORS.search(nested) or _REDIRECT_OPERATOR.search(nested)
                ):
                    if self.config.allow_shell_composition:
                        yield GatewayPolicyFinding(
                            code="shell_composition_requires_exact_approval",
                            severity="warning",
                            reason=(
                                "shell composition in the disposable execution profile "
                                "requires exact one-use approval"
                            ),
                            requires_approval=True,
                        )
                    else:
                        yield _deny(
                            "broad_interpreter_payload",
                            "interpreter payload contains shell composition or redirect operators",
                        )
                else:
                    yield GatewayPolicyFinding(
                        code="interpreter_requires_exact_approval",
                        severity="warning",
                        reason="interpreter execution requires exact one-use approval",
                        requires_approval=True,
                    )
        if executable == "git" and argv:
            subcommand = next((item.casefold() for item in argv if not item.startswith("-")), "")
            if subcommand in _DESTRUCTIVE_GIT:
                yield GatewayPolicyFinding(
                    code="destructive_git_requires_strong_approval",
                    severity="critical" if "--force" in argv or "-f" in argv else "warning",
                    reason=f"git {subcommand} can destroy, rewrite or publish repository state",
                    hard_deny="--force" in argv or "-f" in argv,
                    requires_approval=True,
                )
            elif subcommand not in _READ_ONLY_GIT:
                yield GatewayPolicyFinding(
                    code="git_mutation_requires_approval",
                    severity="warning",
                    reason=f"git {subcommand or '<unknown>'} is not classified read-only",
                    requires_approval=True,
                )
        if executable in {item.casefold() for item in _NETWORK_EXECUTABLES}:
            yield GatewayPolicyFinding(
                code="network_cli_requires_approval",
                severity="warning",
                reason="network-capable CLI requires network policy and exact approval",
                requires_approval=True,
            )
        if any(_SECRET_KEY.search(str(key)) for key in envelope.environment):
            yield _deny(
                "secret_environment_bypass",
                "credentials must be relayed by CredentialRelay rather than command environment",
            )


def _deny(code: str, reason: str, *, metadata: Mapping[str, Any] | None = None) -> GatewayPolicyFinding:
    return GatewayPolicyFinding(
        code=code,
        severity="critical",
        reason=reason,
        hard_deny=True,
        metadata=metadata or {},
    )


def _canonical_host(value: str) -> str:
    return str(value or "").strip().rstrip(".").casefold()


def _literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _executable_name(value: str) -> str:
    return Path(str(value).replace("\\", "/")).name.casefold()


def _strip_balanced_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _interpreter_payload(executable: str, argv: Sequence[str]) -> str | None:
    lowered = executable.casefold()
    flags = {"-c", "/c", "-command", "--eval", "-e"}
    for index, value in enumerate(argv):
        if value.casefold() in flags and index + 1 < len(argv):
            return str(argv[index + 1])
    if lowered in {"powershell", "powershell.exe", "pwsh", "cmd", "cmd.exe"} and argv:
        return " ".join(argv)
    return None


def _is_control_target(path: str, denied: frozenset[str]) -> bool:
    parts = [item.casefold() for item in PurePosixPath(str(path).replace("\\", "/")).parts]
    if not parts:
        return False
    if parts[-1] in denied:
        return True
    return any(item in {".git", ".agents", ".codex", ".claude", ".ssh"} for item in parts)


def _command_decision_findings(decision: CommandPolicyDecision) -> Iterable[GatewayPolicyFinding]:
    for evidence in decision.evidence:
        yield GatewayPolicyFinding(
            code=evidence.code,
            severity="critical" if evidence.effect == CommandEffect.DENY else "warning",
            reason=evidence.reason,
            hard_deny=evidence.effect == CommandEffect.DENY,
            requires_approval=evidence.effect == CommandEffect.ASK,
            metadata=evidence.metadata,
        )


def _file_decision_reason(
    decision: FilePolicyDecision,
    findings: Sequence[GatewayPolicyFinding],
) -> str:
    if findings:
        return findings[0].reason
    if decision.quarantine:
        return "file requires quarantine"
    if decision.allowed:
        return "file passed gateway policy"
    return "file was rejected by gateway policy"


def _find_control_mutations(value: Any, path: tuple[str, ...] = ()) -> Iterable[GatewayPolicyFinding]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).casefold()
            current = (*path, name)
            if name in {
                "permission_mode",
                "permission_rules",
                "project_rules",
                "mcp_config",
                "mcp_servers",
                "shell_profile",
                "allowed_commands",
                "bypass_permissions",
            }:
                yield _deny(
                    "mcp_control_plane_mutation",
                    "MCP tool arguments cannot modify permission, project, MCP or shell policy",
                    metadata={"argument_path": ".".join(current)},
                )
            yield from _find_control_mutations(item, current)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _find_control_mutations(item, (*path, str(index)))


__all__ = [
    "GatewayPolicyConfig",
    "GatewayPolicyFinding",
    "GatewayPolicyRuntime",
    "GatewaySurfacePolicyDecision",
]
