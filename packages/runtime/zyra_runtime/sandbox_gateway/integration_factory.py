from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .artifact_port import GatewayFileArtifactPort
from .backends import LocalProcessSandboxBackend
from .command_policy import CommandPolicyConfig, StructuredCommandPolicy
from .event_port import GatewayEventPort, MemoryGatewayEventSink
from .file_policy import FilePolicyConfig, GatewayFilePolicy
from .git_policy import GitCommandPolicy
from .lifecycle import SandboxLifecycle
from .network_policy import NetworkPolicy, NetworkProfile
from .patch_port import GatewayPatchPort
from .permission_relay import GatewayPermissionRelay
from .provenance import ProvenancePolicy, ProvenanceRegistry
from .quarantine import QuarantineStore
from .receipts import ReceiptLedger
from .redaction import SecretRedactor
from .runtime import SandboxGatewayConfig, SandboxGatewayRuntime
from .session_queue import SessionActorQueue
from .state_store import GatewayStateStore
from .integration_events import (
    GatewayBackendSignalEmitter,
    GatewayEventProjector,
    GatewayReceiptJournal,
)
from .integration_models import content_digest
from .integration_permission import GatewayPermissionBridge
from .integration_policy import GatewayPolicyConfig, GatewayPolicyRuntime


@dataclass(slots=True)
class GatewayRuntimeBundle:
    runtime: SandboxGatewayRuntime
    state_store: GatewayStateStore
    lifecycle: SandboxLifecycle
    backend: LocalProcessSandboxBackend
    command_policy: StructuredCommandPolicy
    file_policy: GatewayFilePolicy
    network_policy: NetworkPolicy
    policy_runtime: GatewayPolicyRuntime
    permission_bridge: GatewayPermissionBridge
    permission_relay: GatewayPermissionRelay
    event_port: GatewayEventPort
    event_sink: MemoryGatewayEventSink
    event_projector: GatewayEventProjector
    signal_emitter: GatewayBackendSignalEmitter
    receipt_ledger: ReceiptLedger
    receipt_journal: GatewayReceiptJournal
    provenance_registry: ProvenanceRegistry
    quarantine_store: QuarantineStore
    artifact_port: GatewayFileArtifactPort | None
    patch_port: GatewayPatchPort | None
    workspace_edit_port: Any | None
    workspace_root: Path
    artifact_root: Path
    state_root: Path
    worker_id: str
    required: bool
    sealed: bool

    def descriptor(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime.descriptor(),
            "state_custody": self.state_store.custody_descriptor(),
            "backend": self.backend.descriptor(),
            "policy_digest": self.policy_runtime.policy_digest,
            "permission": self.permission_bridge.descriptor(),
            "receipt_ledger": self.receipt_ledger.descriptor(),
            "receipt_journal": self.receipt_journal.descriptor(),
            "artifact_port": self.artifact_port.descriptor() if self.artifact_port else None,
            "patch_port": self.patch_port.descriptor() if self.patch_port else None,
            "workspace_root_digest": content_digest(str(self.workspace_root)),
            "artifact_root_digest": content_digest(str(self.artifact_root)),
            "state_root_digest": content_digest(str(self.state_root)),
            "worker_id": self.worker_id,
            "required": self.required,
            "sealed": self.sealed,
            "canonical_gateway_owner": "SandboxGatewayRuntime",
            "permission_owner": "ToolPermissionRuntime",
            "workspace_owner": "WorkspaceManagerRuntime",
        }


class GatewayRuntimeBundleRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bundles: dict[tuple[str, str, str, int], GatewayRuntimeBundle] = {}

    def get_or_create(
        self,
        *,
        workspace_root: str | Path,
        artifact_root: str | Path,
        worker_id: str,
        workspace_edit_port: Any | None,
        runtime_services: Mapping[str, Any] | None = None,
    ) -> GatewayRuntimeBundle:
        workspace = Path(workspace_root).resolve()
        artifacts = Path(artifact_root).resolve()
        key = (str(workspace), str(artifacts), str(worker_id), id(workspace_edit_port))
        with self._lock:
            existing = self._bundles.get(key)
            if existing is not None:
                return existing
            bundle = build_gateway_runtime_bundle(
                workspace_root=workspace,
                artifact_root=artifacts,
                worker_id=worker_id,
                workspace_edit_port=workspace_edit_port,
                runtime_services=runtime_services,
            )
            self._bundles[key] = bundle
            return bundle

    def bundles(self) -> tuple[GatewayRuntimeBundle, ...]:
        with self._lock:
            return tuple(self._bundles.values())

    def clear_closed(self) -> int:
        removed = 0
        with self._lock:
            for key, bundle in list(self._bundles.items()):
                sessions = bundle.state_store.list_sessions()
                if sessions and all(item.state.value == "closed" for item in sessions):
                    del self._bundles[key]
                    removed += 1
        return removed


_DEFAULT_REGISTRY = GatewayRuntimeBundleRegistry()


def build_gateway_runtime_bundle(
    *,
    workspace_root: str | Path,
    artifact_root: str | Path,
    worker_id: str,
    workspace_edit_port: Any | None,
    runtime_services: Mapping[str, Any] | None = None,
) -> GatewayRuntimeBundle:
    services = dict(runtime_services or {})
    workspace = Path(workspace_root).resolve()
    artifacts = Path(artifact_root).resolve()
    required = bool(
        services.get("sandbox_gateway_required", services.get("workspace_gateway_required", True))
    )
    sealed = bool(services.get("sandbox_gateway_sealed", False))
    state_root = Path(
        services.get("sandbox_gateway_state_root")
        or artifacts / ".sandbox-gateway" / _safe_segment(worker_id)
    ).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    known_secrets = tuple(
        str(item)
        for item in services.get("sandbox_gateway_known_secrets", ())
        if str(item)
    )
    redactor = SecretRedactor(known_secrets=known_secrets)
    state_store = GatewayStateStore(state_root / "state")
    lifecycle = SandboxLifecycle(
        state_store,
        lease_seconds=float(services.get("sandbox_gateway_lease_seconds", 300.0)),
    )
    network_policy = NetworkPolicy(
        (
            NetworkProfile(profile_id="offline", require_approval=True),
            NetworkProfile(
                profile_id="public-https",
                allowed_schemes=frozenset({"https"}),
                allowed_hosts=frozenset(
                    str(item)
                    for item in services.get("sandbox_gateway_allowed_hosts", ())
                    if str(item)
                ),
                denied_hosts=frozenset(
                    str(item)
                    for item in services.get("sandbox_gateway_denied_hosts", ())
                    if str(item)
                ),
                allow_private=False,
                allow_loopback=False,
                allow_link_local=False,
                require_approval=True,
            ),
        )
    )
    command_policy = StructuredCommandPolicy(
        CommandPolicyConfig(
            allow_unknown_with_approval=True,
            allow_read_only_without_human=True,
            sealed_mode_denies_ask=True,
            deny_direct_shell_strings=False,
            deny_destructive=True,
            require_permission_for_all_commands=True,
        ),
        network_policy=network_policy,
        git_policy=GitCommandPolicy(
            allow_read_only=True,
            allow_local_mutation_with_approval=True,
            deny_destructive=True,
            deny_network=True,
        ),
    )
    provenance_policy = ProvenancePolicy(
        executable_from_untrusted=False,
        archive_from_untrusted=False,
    )
    file_policy = GatewayFilePolicy(
        FilePolicyConfig(
            maximum_bytes=int(services.get("sandbox_gateway_maximum_file_bytes", 128 * 1024 * 1024)),
            allow_binary=True,
            allow_executable=False,
            allow_archive=True,
            quarantine_content_type_mismatch=True,
            quarantine_unknown_binary=True,
        ),
        provenance_policy=provenance_policy,
    )
    policy_config = GatewayPolicyConfig(
        workspace_root=workspace,
        allow_public_https=bool(services.get("sandbox_gateway_allow_public_https", True)),
        allow_public_http=bool(services.get("sandbox_gateway_allow_public_http", False)),
        allow_private_network=bool(services.get("sandbox_gateway_allow_private_network", False)),
        allow_loopback_network=bool(services.get("sandbox_gateway_allow_loopback_network", False)),
        allow_file_urls=False,
        allowed_network_hosts=frozenset(
            str(item)
            for item in services.get("sandbox_gateway_allowed_hosts", ())
            if str(item)
        ),
        denied_network_hosts=frozenset(
            str(item)
            for item in services.get("sandbox_gateway_denied_hosts", ())
            if str(item)
        ),
        trusted_mcp_servers=frozenset(
            str(item)
            for item in services.get("sandbox_gateway_trusted_mcp_servers", ())
            if str(item)
        ),
        secret_environment_keys=frozenset(
            str(item)
            for item in services.get("sandbox_gateway_secret_environment_keys", ())
            if str(item)
        ),
        sealed=sealed,
    )
    policy_runtime = GatewayPolicyRuntime(
        policy_config,
        command_policy=command_policy,
        file_policy=file_policy,
        network_policy=network_policy,
    )
    permission_bridge = GatewayPermissionBridge(
        maximum_binding_seconds=float(
            services.get("sandbox_gateway_permission_binding_seconds", 30.0)
        )
    )
    permission_relay = GatewayPermissionRelay(
        state_store,
        permission_bridge,
        default_ttl_seconds=float(
            services.get("sandbox_gateway_permission_ticket_seconds", 30.0)
        ),
    )
    event_sink = MemoryGatewayEventSink()
    event_port = GatewayEventPort(
        state_store,
        sink=event_sink,
        redactor=redactor,
        known_secrets=known_secrets,
    )
    receipt_ledger = ReceiptLedger(state_store, redactor=redactor)
    provenance_registry = ProvenanceRegistry()
    quarantine_store = QuarantineStore(state_root / "quarantine", state_store)
    artifact_port: GatewayFileArtifactPort | None = None
    patch_port: GatewayPatchPort | None = None
    if workspace_edit_port is not None:
        artifact_port = GatewayFileArtifactPort(
            workspace_edit_port,
            file_policy=file_policy,
            provenance_registry=provenance_registry,
            quarantine_store=quarantine_store,
            redactor=redactor,
            enabled=True,
        )
        patch_port = GatewayPatchPort(
            workspace_edit_port,
            file_policy=file_policy,
            provenance_registry=provenance_registry,
            enabled=True,
        )
    backend = LocalProcessSandboxBackend(
        state_root / "backend",
        redactor=redactor,
        preserve_failed_roots=True,
    )
    runtime = SandboxGatewayRuntime(
        SandboxGatewayConfig(
            state_root=state_root,
            interactive=not sealed,
            sealed=sealed,
            enabled=True,
            actor_queue_timeout_seconds=float(
                services.get("sandbox_gateway_actor_timeout_seconds", 30.0)
            ),
            ready_timeout_seconds=float(
                services.get("sandbox_gateway_ready_timeout_seconds", 30.0)
            ),
            auto_prepare=True,
            commit_successful_outputs=True,
            preserve_failed_patch_root=state_root / "failed-patches",
        ),
        state_store=state_store,
        lifecycle=lifecycle,
        backend=backend,
        command_policy=command_policy,
        permission_relay=permission_relay,
        event_port=event_port,
        receipt_ledger=receipt_ledger,
        artifact_port=artifact_port,
        patch_port=patch_port,
        actor_queue=SessionActorQueue(
            maximum_depth=int(services.get("sandbox_gateway_queue_depth", 256))
        ),
    )
    signal_emitter = GatewayBackendSignalEmitter(state_store, event_port)
    receipt_journal = GatewayReceiptJournal(state_root / "receipts")
    return GatewayRuntimeBundle(
        runtime=runtime,
        state_store=state_store,
        lifecycle=lifecycle,
        backend=backend,
        command_policy=command_policy,
        file_policy=file_policy,
        network_policy=network_policy,
        policy_runtime=policy_runtime,
        permission_bridge=permission_bridge,
        permission_relay=permission_relay,
        event_port=event_port,
        event_sink=event_sink,
        event_projector=GatewayEventProjector(known_secrets=known_secrets),
        signal_emitter=signal_emitter,
        receipt_ledger=receipt_ledger,
        receipt_journal=receipt_journal,
        provenance_registry=provenance_registry,
        quarantine_store=quarantine_store,
        artifact_port=artifact_port,
        patch_port=patch_port,
        workspace_edit_port=workspace_edit_port,
        workspace_root=workspace,
        artifact_root=artifacts,
        state_root=state_root,
        worker_id=str(worker_id),
        required=required,
        sealed=sealed,
    )


def install_gateway_runtime_services(
    runtime_services: Mapping[str, Any] | None,
    *,
    workspace_root: str | Path,
    artifact_root: str | Path,
    worker_id: str,
    workspace_edit_port: Any | None = None,
) -> dict[str, Any]:
    services = dict(runtime_services or {})
    port = workspace_edit_port or services.get("workspace_edit_port")
    required = bool(
        services.get("sandbox_gateway_required", services.get("workspace_gateway_required", False))
    )
    services["sandbox_gateway_required"] = required
    if port is None:
        if required:
            services["sandbox_gateway_install_error"] = "workspace_gateway_unavailable"
        else:
            services["sandbox_gateway_installed"] = False
        return services
    bundle = _DEFAULT_REGISTRY.get_or_create(
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        worker_id=worker_id,
        workspace_edit_port=port,
        runtime_services=services,
    )
    from .integration_browser import BrowserGatewayBoundary
    from .integration_mcp import McpGatewayBoundary
    from .integration_tools import GatewayToolExecutionRouter

    router = GatewayToolExecutionRouter(bundle)
    services.update(
        {
            "sandbox_gateway_bundle": bundle,
            "sandbox_gateway_runtime": bundle.runtime,
            "sandbox_gateway_router": router,
            "sandbox_gateway_policy_runtime": bundle.policy_runtime,
            "sandbox_gateway_backend_signals": bundle.signal_emitter,
            "sandbox_gateway_mcp_boundary": McpGatewayBoundary(bundle),
            "sandbox_gateway_browser_boundary": BrowserGatewayBoundary(bundle),
            "sandbox_gateway_installed": True,
            "sandbox_gateway_required": required,
            "sandbox_gateway_descriptor": bundle.descriptor(),
        }
    )
    return services


def default_gateway_registry() -> GatewayRuntimeBundleRegistry:
    return _DEFAULT_REGISTRY


def _safe_segment(value: str) -> str:
    selected = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in str(value)
    ).strip("._")
    return selected[:80] or "worker"


__all__ = [
    "GatewayRuntimeBundle",
    "GatewayRuntimeBundleRegistry",
    "build_gateway_runtime_bundle",
    "default_gateway_registry",
    "install_gateway_runtime_services",
]
