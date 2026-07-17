from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_runtime.permission import BrowserActionPermissionGate

from .cdp_probe import CdpElementProbePort
from .clipboard_guard import BrowserClipboardGuard, ClipboardPort
from .event_port import ArtifactPort, BrowserActionEventPort, BrowserActionResultProjector
from .executor import BrowserSideEffectFence, CdpTransport, NetworkDispatchScopeFactory
from .file_policy import BrowserFilePolicy, FilePolicyConfig
from .form_policy import BrowserFormPolicy
from .gateway import BrowserActionGateway, BrowserActionGatewayConfig
from .geometry_guard import BrowserGeometryGuard
from .hook_preflight import default_preflight_hooks
from .network_policy import (
    BrowserNetworkPolicy,
    HostResolver,
    NetworkPolicyConfig,
    SystemHostResolver,
    compile_patterns,
)
from .permission_bridge import BrowserActionPermissionBridge
from .registry import default_browser_action_registry
from .secret_policy import BrowserSecretPolicy, SecretProvider
from .selector_guard import BrowserSelectorGuard, ElementSemanticProbe, SelectorStore
from .sensitive_policy import BrowserSensitiveActionClassifier, ExecutionMode, SensitivePolicyConfig


@dataclass(frozen=True, slots=True)
class BrowserActionFoundationOptions:
    workspace_root: str | Path
    artifact_root: str | Path
    downloads_root: str | Path
    allowed_domains: tuple[str, ...] = ()
    denied_domains: tuple[str, ...] = ()
    require_domain_allowlist: bool = False
    allow_loopback: bool = False
    allow_private: bool = False
    allow_literal_ip: bool = False
    execution_mode: ExecutionMode = ExecutionMode.INTERACTIVE
    receipt_ttl_seconds: float = 30.0
    maximum_scroll_attempts: int = 2
    browser_context_id: str = "default"
    clipboard_maximum_chars: int = 100_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", str(Path(self.workspace_root).resolve(strict=False)))
        object.__setattr__(self, "artifact_root", str(Path(self.artifact_root).resolve(strict=False)))
        object.__setattr__(self, "downloads_root", str(Path(self.downloads_root).resolve(strict=False)))
        object.__setattr__(self, "allowed_domains", tuple(self.allowed_domains))
        object.__setattr__(self, "denied_domains", tuple(self.denied_domains))
        if not self.browser_context_id:
            raise ValueError("browser action foundation requires browser_context_id")
        if self.clipboard_maximum_chars < 1:
            raise ValueError("browser clipboard maximum characters must be positive")


@dataclass(frozen=True, slots=True)
class BrowserActionFoundation:
    gateway: BrowserActionGateway
    event_port: BrowserActionEventPort
    artifact_port: ArtifactPort
    file_policy: BrowserFilePolicy
    network_policy: BrowserNetworkPolicy
    selector_guard: BrowserSelectorGuard
    geometry_guard: BrowserGeometryGuard
    clipboard_guard: BrowserClipboardGuard | None
    semantic_probe: ElementSemanticProbe | None


class BrowserActionFoundationFactory:
    """Wire 04A/04B/03A owners into the default 04C main path."""

    @staticmethod
    def create(
        *,
        options: BrowserActionFoundationOptions,
        permission_gate: BrowserActionPermissionGate,
        selector_store: SelectorStore,
        cdp_transport: CdpTransport,
        artifact_port: ArtifactPort,
        resolver: HostResolver | None = None,
        secret_provider: SecretProvider | None = None,
        clipboard_port: ClipboardPort | None = None,
        semantic_probe: ElementSemanticProbe | None = None,
        network_scope_factory: NetworkDispatchScopeFactory | None = None,
        require_network_scope: bool = False,
        event_sink: Any = None,
    ) -> BrowserActionFoundation:
        workspace = Path(options.workspace_root)
        artifacts = Path(options.artifact_root)
        downloads = Path(options.downloads_root)
        workspace.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        downloads.mkdir(parents=True, exist_ok=True)
        registry = default_browser_action_registry()
        file_policy = BrowserFilePolicy(
            FilePolicyConfig(
                upload_roots=(str(workspace), str(artifacts)),
                download_root=str(downloads),
                artifact_root=str(artifacts),
            )
        )
        network_policy = BrowserNetworkPolicy(
            NetworkPolicyConfig(
                allowed_patterns=compile_patterns(options.allowed_domains),
                denied_patterns=compile_patterns(options.denied_domains),
                allow_loopback=options.allow_loopback,
                allow_private=options.allow_private,
                allow_literal_ip=options.allow_literal_ip,
                require_allowlist=options.require_domain_allowlist,
            ),
            resolver or SystemHostResolver(),
        )
        selector_guard = BrowserSelectorGuard(selector_store, semantic_probe=semantic_probe)
        geometry_guard = BrowserGeometryGuard(
            CdpElementProbePort(cdp_transport),
            maximum_scroll_attempts=options.maximum_scroll_attempts,
        )
        secret_policy = BrowserSecretPolicy(secret_provider) if secret_provider is not None else None
        clipboard_guard = (
            BrowserClipboardGuard(
                clipboard_port,
                maximum_chars=options.clipboard_maximum_chars,
            )
            if clipboard_port is not None
            else None
        )
        form_policy = BrowserFormPolicy()
        event_port = BrowserActionEventPort(event_sink)
        projector = BrowserActionResultProjector(artifact_port)
        executor = BrowserSideEffectFence(
            cdp_transport,
            file_policy=file_policy,
            secret_policy=secret_policy,
            clipboard_guard=clipboard_guard,
            artifact_port=artifact_port,
            network_scope_factory=network_scope_factory,
            require_network_scope=require_network_scope,
        )
        gateway = BrowserActionGateway(
            registry=registry,
            classifier=BrowserSensitiveActionClassifier(SensitivePolicyConfig()),
            hooks=default_preflight_hooks(),
            permission=BrowserActionPermissionBridge(permission_gate),
            executor=executor,
            event_port=event_port,
            result_projector=projector,
            selector_guard=selector_guard,
            network_policy=network_policy,
            file_policy=file_policy,
            secret_policy=secret_policy,
            geometry_guard=geometry_guard,
            clipboard_guard=clipboard_guard,
            form_policy=form_policy,
            config=BrowserActionGatewayConfig(
                execution_mode=options.execution_mode,
                receipt_ttl_seconds=options.receipt_ttl_seconds,
            ),
        )
        return BrowserActionFoundation(
            gateway=gateway,
            event_port=event_port,
            artifact_port=artifact_port,
            file_policy=file_policy,
            network_policy=network_policy,
            selector_guard=selector_guard,
            geometry_guard=geometry_guard,
            clipboard_guard=clipboard_guard,
            semantic_probe=semantic_probe,
        )
