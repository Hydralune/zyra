from __future__ import annotations

"""Productized facade for the Zyra-owned MCP client runtime.

This is the composition root used by workers and the HTTP control plane.  It
keeps config, auth, transports, catalogs, tool projection, artifacts,
elicitations, sampling and instructions under one state owner rather than
exposing a group of unrelated helper clients.
"""

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, new_id
from zyra_runtime import LocalArtifactStore, ToolExecutionContext

from .auth import (
    AuthMode,
    AuthRuntimeConfig,
    McpAuthRuntime,
    OAuthTokenResponse,
)
from .capabilities import McpCapabilityCatalog, McpPaginationPolicy, McpResourcePromptRuntime
from .bootstrap import McpBootstrapRuntime
from .config import McpConfigNotFound, McpConfigStore
from .connection import McpConnectReceipt, McpConnectionRuntime, McpRefreshReceipt
from .credentials import FileCredentialVault
from .elicitation import McpElicitationQueue, McpElicitationQueueReceipt
from .instructions import McpInstructionsRuntime
from .models import (
    JsonValue,
    McpApprovalState,
    McpConfigScope,
    McpElicitationAction,
    McpElicitationResolution,
    McpServerConfig,
    McpTransportKind,
    redact_value,
    to_json_value,
)
from .output import McpOutputBudgetRuntime, McpOutputPolicy
from .projection import McpProjectionBundle, McpToolProjectionRuntime
from .resource_projection import (
    McpResourceProjectionBundle,
    McpResourceProjectionRuntime,
)
from .main_path import McpMainPathRuntime
from .control import McpControlRuntime
from .sampling import McpSamplingRuntime, SamplingCallback, SamplingPolicy
from .session_bridge import McpSessionBridge, McpSessionIdentity
from .recovery import (
    McpRecoveryIdentity,
    McpRecoveryPlanner,
    McpRecoverySignal,
)
from .store import McpRuntimeStateStore


# Machine-readable product boundary used by submission/cleanroom audits.  It
# names the composition root, the production consumers that make it reachable,
# and the behavior suites whose failure proves the boundary is not optional.
MCP_RUNTIME_BOUNDARY: Mapping[str, Any] = {
    "runtime_entry": "zyra_integrations.mcp.runtime.McpClientRuntime",
    "main_path": (
        "apps/api/zyra_api/main.py:McpApiFacade",
        "packages/workers/zyra_workers/code_worker_runtime.py:CodeWorkerRuntime",
    ),
    "test_binding": (
        "tests/unit/test_mcp_adversarial_lifecycle.py",
        "tests/integration/test_mcp_codeworker_permission_integration.py",
        "tests/integration/test_mcp_api_control_restore.py",
    ),
}


class McpClientRuntimeError(RuntimeError):
    pass


class McpClientRuntimeDisabled(McpClientRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class McpWorkerProjection:
    context: ToolExecutionContext
    bundle: McpProjectionBundle
    omitted_by_policy: tuple[str, ...]
    resource_bundle: McpResourceProjectionBundle | None = None

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "bundle": self.bundle.safe_dict(),
            "omitted_by_policy": list(self.omitted_by_policy),
            "registry_tool_count": len(self.context.registry.list()),
            "dynamic_handler_count": len(self.context.dynamic_handlers),
            "resource_projection": (
                self.resource_bundle.safe_dict()
                if self.resource_bundle is not None
                else None
            ),
        }


class McpRuntimeEventBuffer:
    """Small process-local handoff for canonical events.

    Events remain canonical ``EventRecord`` values.  Workers drain only their
    own run/task partition and persist them with the rest of the trace; API
    control operations can return the emitted records immediately.
    """

    def __init__(self, *, capacity: int = 4096) -> None:
        if capacity <= 0:
            raise ValueError("event buffer capacity must be positive")
        self.capacity = capacity
        self._events: list[EventRecord] = []
        self._lock = threading.RLock()

    def append(self, event: EventRecord) -> None:
        with self._lock:
            self._events.append(event)
            overflow = len(self._events) - self.capacity
            if overflow > 0:
                del self._events[:overflow]

    def snapshot(self, *, run_id: str = "", task_id: str = "") -> tuple[EventRecord, ...]:
        with self._lock:
            return tuple(
                event
                for event in self._events
                if (not run_id or event.run_id == run_id)
                and (not task_id or event.task_id == task_id)
            )

    def drain(self, *, run_id: str = "", task_id: str = "") -> tuple[EventRecord, ...]:
        with self._lock:
            selected: list[EventRecord] = []
            retained: list[EventRecord] = []
            for event in self._events:
                matches = (not run_id or event.run_id == run_id) and (not task_id or event.task_id == task_id)
                (selected if matches else retained).append(event)
            self._events = retained
            return tuple(selected)


class McpClientRuntime:
    """One cohesive MCP runtime consumed by CodeWorker and the API."""

    def __init__(
        self,
        *,
        state_store: McpRuntimeStateStore,
        artifact_store: LocalArtifactStore,
        credential_vault: FileCredentialVault,
        config_store: McpConfigStore | None = None,
        output_policy: McpOutputPolicy | None = None,
        pagination_policy: McpPaginationPolicy | None = None,
        sampling_policy: SamplingPolicy | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.state_store = state_store
        self.artifact_store = artifact_store
        self.credential_vault = credential_vault
        self.config_store = config_store or McpConfigStore(state_store)
        self.disabled = disabled
        self.event_buffer = McpRuntimeEventBuffer()
        self.external_event_sink = event_sink
        self.catalog = McpCapabilityCatalog()
        self.output_runtime = McpOutputBudgetRuntime(
            artifact_store,
            policy=output_policy,
            disabled=disabled,
        )
        self.resource_runtime = McpResourcePromptRuntime(
            self.catalog,
            self.output_runtime,
            pagination_policy=pagination_policy,
            disabled=disabled,
        )
        self.instructions_runtime = McpInstructionsRuntime(
            artifact_store,
            state_store=state_store,
            event_sink=self._emit,
            disabled=disabled,
        )
        self.elicitation_queue = McpElicitationQueue(
            state_store=state_store,
            event_sink=self._emit,
            disabled=disabled,
        )
        self.sampling_runtime = McpSamplingRuntime(
            sampling_policy or SamplingPolicy(enabled=False),
            event_sink=self._sampling_event,
        )
        self._auth_runtimes: dict[tuple[str, str], McpAuthRuntime] = {}
        self._auth_lock = threading.RLock()
        self.connection_runtime = McpConnectionRuntime(
            catalog=self.catalog,
            resource_runtime=self.resource_runtime,
            state_store=state_store,
            instructions_runtime=self.instructions_runtime,
            elicitation_queue=self.elicitation_queue,
            sampling_runtime=self.sampling_runtime,
            auth_factory=self._build_auth_runtime,
            event_sink=self._emit,
            disabled=disabled,
        )
        self.projection_runtime = McpToolProjectionRuntime(
            self.catalog,
            self.connection_runtime,
            self.output_runtime,
            event_sink=self._emit,
            disabled=disabled,
        )
        self.resource_projection_runtime = McpResourceProjectionRuntime(
            self.catalog,
            self.connection_runtime,
            self.output_runtime,
            event_sink=self._emit,
            disabled=disabled,
        )
        self.bootstrap_runtime = McpBootstrapRuntime(
            self.config_store,
            self.connection_runtime,
            self.state_store,
            event_sink=self._emit,
            disabled=disabled,
        )
        self.control_runtime = McpControlRuntime(
            self,
            bootstrap_runtime=self.bootstrap_runtime,
            task_runtime=getattr(self.connection_runtime, "task_runtime", None),
            disabled=disabled,
        )
        self.main_path_runtime = McpMainPathRuntime(
            self,
            self.resource_projection_runtime,
            self.bootstrap_runtime,
            disabled=disabled,
        )
        self.recovery_planner = McpRecoveryPlanner(disabled=disabled)

    @classmethod
    def from_paths(
        cls,
        *,
        state_path: str | Path,
        artifact_root: str | Path,
        credential_root: str | Path | None = None,
        disabled: bool = False,
        event_sink: Callable[[EventRecord], None] | None = None,
    ) -> "McpClientRuntime":
        artifacts = LocalArtifactStore(artifact_root)
        credentials = Path(credential_root).resolve() if credential_root else Path(artifact_root).resolve() / ".mcp" / "credentials"
        return cls(
            state_store=McpRuntimeStateStore(state_path, disabled=disabled),
            artifact_store=artifacts,
            credential_vault=FileCredentialVault(credentials),
            event_sink=event_sink,
            disabled=disabled,
        )

    def register_in_process(self, server_id: str, program: Callable[..., Any], *, replace_existing: bool = False) -> None:
        self._assert_enabled()
        self.connection_runtime.register_in_process(server_id, program, replace_existing=replace_existing)

    def add_server(
        self,
        name: str,
        config: Mapping[str, Any] | McpServerConfig,
        *,
        source_id: str = "manual:dynamic",
        scope: McpConfigScope | str = McpConfigScope.DYNAMIC,
        expected_revision: int | None = None,
    ) -> McpServerConfig:
        self._assert_enabled()
        raw = config.to_dict(include_secrets=True) if isinstance(config, McpServerConfig) else dict(config)
        return self.config_store.upsert_server(
            name,
            raw,
            source_id=source_id,
            scope=scope,
            expected_revision=expected_revision,
        )

    def connect_server(self, name: str, **context: Any) -> McpConnectReceipt:
        self._assert_enabled()
        materialized = self.config_store.materialize_server(name)
        return self.connection_runtime.connect(materialized.config, **context)

    def connect_all(self, **context: Any) -> tuple[McpConnectReceipt, ...]:
        self._assert_enabled()
        receipts: list[McpConnectReceipt] = []
        for name in sorted(self.config_store.effective_servers()):
            receipts.append(self.connect_server(name, **context))
        return tuple(receipts)

    def disconnect_server(self, name: str, **context: Any) -> Any:
        self._assert_enabled()
        return self.connection_runtime.disconnect(name, **context)

    def reconnect_server(self, name: str, **context: Any) -> McpConnectReceipt:
        self._assert_enabled()
        return self.connection_runtime.reconnect(name, **context)

    def refresh_server(
        self,
        name: str,
        *,
        refresh_kinds: Sequence[str] = ("tools", "resources", "resource_templates", "prompts"),
        **context: Any,
    ) -> McpRefreshReceipt:
        self._assert_enabled()
        return self.connection_runtime.refresh(name, refresh_kinds=refresh_kinds, **context)

    def set_server_disabled(self, name: str, disabled: bool, **context: Any) -> dict[str, Any]:
        self._assert_enabled()
        record = self.config_store.get_server(name, include_inactive=True)
        server_id = record.config.server_id if record is not None else name
        state = self.config_store.set_disabled(name, disabled)
        if disabled:
            with _suppress_runtime_error():
                self.connection_runtime.disable(server_id, **context)
        return state

    def approve_server(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self._assert_enabled()
        return self.config_store.approve_project_server(name, **kwargs)

    def reject_server(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self._assert_enabled()
        record = self.config_store.get_server(name, include_inactive=True)
        server_id = record.config.server_id if record is not None else name
        result = self.config_store.reject_project_server(name, **kwargs)
        with _suppress_runtime_error():
            self.connection_runtime.disable(server_id)
        return result

    def install_auth_tokens(
        self,
        server_id: str,
        response: OAuthTokenResponse | Mapping[str, Any],
    ) -> dict[str, Any]:
        self._assert_enabled()
        runtime = self.auth_runtime(server_id)
        outcome = runtime.install_tokens(response)
        return outcome.safe_dict()

    def refresh_auth(self, server_id: str) -> dict[str, Any]:
        self._assert_enabled()
        return self.auth_runtime(server_id).refresh().safe_dict()

    def revoke_auth(self, server_id: str) -> dict[str, Any]:
        self._assert_enabled()
        return self.auth_runtime(server_id).revoke().safe_dict()

    def auth_runtime(self, server_id: str) -> McpAuthRuntime:
        record = self.config_store.get_server(server_id, include_inactive=True)
        if record is None:
            raise McpConfigNotFound(server_id)
        return self._build_auth_runtime(record.config) or self._build_auth_runtime(
            replace(record.config, metadata={**record.config.metadata, "auth_type": "oauth"})
        )  # type: ignore[return-value]

    def register_sampling_callback(self, server_id: str, callback: SamplingCallback) -> None:
        self._assert_enabled()
        # Registration is the explicit operator opt-in.  Until it happens the
        # default policy is disabled and sampling is neither advertised nor
        # executable.  Existing connections must still reconnect before the
        # newly truthful client capability is visible to their server.
        if not self.sampling_runtime.policy.enabled:
            self.sampling_runtime.policy = replace(self.sampling_runtime.policy, enabled=True)
        self.sampling_runtime.register_callback(server_id, callback)

    def unregister_sampling_callback(self, server_id: str) -> bool:
        return self.sampling_runtime.unregister_callback(server_id)

    def resolve_elicitation(
        self,
        *,
        server_id: str,
        session_id: str,
        request_id: str,
        expected_revision: int,
        action: McpElicitationAction | str,
        content: Mapping[str, Any] | None,
        actor_id: str,
        idempotency_key: str,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
    ) -> McpElicitationQueueReceipt:
        self._assert_enabled()
        return self.elicitation_queue.resolve(
            McpElicitationResolution(
                request_id=request_id,
                session_id=session_id,
                server_id=server_id,
                expected_revision=expected_revision,
                action=McpElicitationAction(str(action)),
                content=to_json_value(content or {}),
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
        )

    def read_resource(self, server_id: str, uri: str, **context: Any) -> dict[str, JsonValue]:
        self._assert_enabled()
        return self.connection_runtime.read_resource(server_id, uri, **context).safe_dict()

    def get_prompt(self, server_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, JsonValue]:
        self._assert_enabled()
        return self.connection_runtime.get_prompt(server_id, name, arguments)

    def worker_projection(
        self,
        base_context: ToolExecutionContext,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str = "",
        worker_request_id: str = "",
        include_servers: Sequence[str] | None = None,
        include_resource_surfaces: bool = True,
    ) -> McpWorkerProjection:
        self._assert_enabled()
        reserved = tuple(tool.name for tool in base_context.registry.list())
        built = self.projection_runtime.build_bundle(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            reserved_names=reserved,
            include_servers=include_servers,
        )
        accepted_specs = []
        accepted_handlers: dict[str, Any] = {}
        omitted: list[str] = []
        for spec in built.tool_specs:
            server_id = str(spec.metadata.get("server_id") or "")
            remote_name = str(spec.metadata.get("mcp_tool_name") or spec.name)
            decision = self.config_store.tool_allowed(server_id, remote_name)
            if not decision.allowed:
                omitted.append(spec.name)
                continue
            accepted_specs.append(spec)
            accepted_handlers[spec.name] = built.handlers[spec.name]
        bundle = McpProjectionBundle(
            bundle_id=built.bundle_id,
            tool_specs=tuple(accepted_specs),
            handlers=accepted_handlers,
            decisions=built.decisions,
            server_generations=built.server_generations,
            created_at=built.created_at,
        )
        resource_bundle = None
        resource_specs: tuple[Any, ...] = ()
        resource_handlers: Mapping[str, Any] = {}
        if include_resource_surfaces:
            resource_bundle = self.resource_projection_runtime.build_bundle(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                worker_request_id=worker_request_id,
                reserved_names=(
                    *reserved,
                    *(spec.name for spec in bundle.tool_specs),
                ),
                include_servers=include_servers,
            )
            resource_specs = resource_bundle.tool_specs
            resource_handlers = resource_bundle.handlers
        merged_registry = base_context.registry.merged(
            [*bundle.tool_specs, *resource_specs]
        )
        context = replace(
            base_context,
            registry=merged_registry,
            dynamic_handlers={
                **dict(base_context.dynamic_handlers),
                **dict(bundle.handlers),
                **dict(resource_handlers),
            },
        )
        return McpWorkerProjection(
            context,
            bundle,
            tuple(sorted(omitted)),
            resource_bundle,
        )

    def prompt_commands(self) -> tuple[Any, ...]:
        bundle = self.resource_projection_runtime.build_bundle(
            run_id="mcp-command-catalog",
            task_id="mcp-command-catalog",
            node_id=None,
            session_id="mcp-command-catalog",
            worker_request_id="mcp-command-catalog",
            reserved_names=(),
        )
        return bundle.prompt_commands

    def prepare_worker_constraints(
        self,
        constraints: Mapping[str, Any],
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self._assert_enabled()
        selected = dict(constraints)
        # Only runtime-owned deltas reach 02D.  Request fields with the same
        # name are overwritten so a caller cannot inject trusted restore text.
        selected["mcp_instruction_deltas"] = self.instructions_runtime.restore_constraints(session_id)
        selected["mcp_runtime_projection"] = {
            "schema": "zyra.mcp-worker-projection.v1",
            "session_id": session_id,
            "connected_servers": [
                snapshot.server_id
                for snapshot in self.connection_runtime.snapshots()
                if snapshot.healthy
            ],
            "catalog_digest": self.catalog.safe_dict(),
            "instruction_count": len(selected["mcp_instruction_deltas"]),
            "runtime_owned": True,
        }
        return selected

    def session_snapshot(self, session_id: str) -> dict[str, JsonValue]:
        state = self.state_store.read_state()
        return {
            "schema": "zyra.mcp-session-state.v1",
            "session_id": session_id,
            "state_revision": int(state["revision"]),
            "connections": [snapshot.to_dict() for snapshot in self.connection_runtime.snapshots()],
            "catalog": self.catalog.safe_dict(),
            "instructions": self.instructions_runtime.snapshot(session_id),
            "pending_elicitations": [
                item.safe_dict()
                for item in self.elicitation_queue.list(session_id=session_id, include_terminal=False)
            ],
            "auth": {
                server_id: runtime.snapshot.safe_dict()
                for (server_id, _), runtime in sorted(self._auth_runtimes.items())
            },
            "credentials_included": False,
            "live_transports_included": False,
        }

    def restore_session_snapshot(self, snapshot: Mapping[str, Any] | None) -> int:
        if not isinstance(snapshot, Mapping):
            return 0
        instructions = snapshot.get("instructions")
        return self.instructions_runtime.restore_snapshot(instructions) if isinstance(instructions, Mapping) else 0

    def restore_session_from_store(
        self,
        store: Any,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        node_id: str = "",
        require_causality: bool = False,
    ) -> dict[str, JsonValue]:
        bridge = McpSessionBridge(store=store)
        identity = McpSessionIdentity(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            worker_request_id=worker_request_id,
            node_id=node_id,
        )
        loaded = bridge.load(identity)
        if not bool(getattr(loaded, "found", False)):
            return {
                "schema": "zyra.mcp-session-restore-receipt.v1",
                "restored": False,
                "found": False,
                "session_id": session_id,
            }
        receipt = bridge.restore(
            self,
            identity,
            require_causality=require_causality,
        )
        return receipt.to_dict()

    def prepare_session_checkpoint(
        self,
        store: Any,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        node_id: str = "",
    ) -> dict[str, JsonValue]:
        bridge = McpSessionBridge(store=store)
        identity = McpSessionIdentity(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            worker_request_id=worker_request_id,
            node_id=node_id,
        )
        snapshot = self.session_snapshot(session_id)
        validation = bridge.validator.validate(
            snapshot,
            expected_session_id=session_id,
        )
        if str(snapshot.get("session_id") or "") != session_id:
            raise McpClientRuntimeError("MCP session checkpoint identity mismatch")
        if bool(snapshot.get("credentials_included")) or bool(
            snapshot.get("live_transports_included")
        ):
            raise McpClientRuntimeError("MCP session checkpoint contains forbidden custody")
        bridge.diff_checkpoint(identity, snapshot)
        return snapshot

    def plan_recovery(
        self,
        *,
        run_id: str,
        task_id: str,
        signals: Sequence[McpRecoverySignal],
        node_id: str = "",
        session_id: str = "",
        worker_request_id: str = "",
        cause_event_id: str = "",
    ) -> Any:
        return self.recovery_planner.plan(
            McpRecoveryIdentity(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                worker_request_id=worker_request_id,
                cause_event_id=cause_event_id,
            ),
            signals,
        )

    def diagnostics(self) -> dict[str, JsonValue]:
        auth = {
            server_id: runtime.safe_diagnostics()
            for (server_id, _), runtime in sorted(self._auth_runtimes.items())
        }
        health = self.connection_runtime.health()
        config = self.config_store.public_snapshot()
        return {
            "schema": "zyra.mcp-client-runtime.v1",
            "runtime_id": "McpClientRuntime",
            "owner_unit": "M1-S03B-02",
            "enabled": not self.disabled,
            "ok": bool(health["ok"]) and not self.disabled,
            "config": to_json_value(config),
            "health": health,
            "catalog": self.catalog.safe_dict(),
            "auth": to_json_value(auth),
            "sampling": to_json_value(self.sampling_runtime.safe_diagnostics()),
            "elicitation": {
                "pending": [item.safe_dict() for item in self.elicitation_queue.list(include_terminal=False)],
                "pending_count": len(self.elicitation_queue.list(include_terminal=False)),
            },
            "state": to_json_value(self.state_store.verify_integrity()),
            "requires_node_sidecar": False,
            "root_source_repository_dependency": False,
            "fixed_ok_health": False,
            "control": self.control_runtime.diagnostics(),
            "resource_projection": {
                "runtime_id": "McpResourceProjectionRuntime",
                "enabled": not self.resource_projection_runtime.disabled,
                "state_owner": "McpCapabilityCatalog",
            },
            "bootstrap": (
                self.bootstrap_runtime.last_report.safe_dict()
                if self.bootstrap_runtime.last_report is not None
                else {
                    "runtime_id": "McpBootstrapRuntime",
                    "enabled": not self.bootstrap_runtime.disabled,
                    "executed": False,
                }
            ),
            "main_path": {
                "runtime_id": "McpMainPathRuntime",
                "enabled": not self.main_path_runtime.disabled,
                "creates_parallel_store": False,
            },
            "recovery": {
                "runtime_id": "McpRecoveryPlanner",
                "enabled": not self.recovery_planner.disabled,
                "state_owner": "McpRuntimeStateStore",
                "creates_parallel_store": False,
            },
        }

    def drain_events(self, *, run_id: str = "", task_id: str = "") -> tuple[EventRecord, ...]:
        return self.event_buffer.drain(run_id=run_id, task_id=task_id)

    def _build_auth_runtime(self, config: McpServerConfig) -> McpAuthRuntime | None:
        auth_type = str(config.metadata.get("auth_type") or config.metadata.get("auth") or "").casefold()
        if not auth_type:
            # Explicit static Authorization headers are transport-owned.  Other
            # servers default to no auth until config declares oauth/xaa.
            auth_type = "none"
        if auth_type in {"none", "static", "header"}:
            return None
        raw_config: dict[str, Any] = config.to_dict(include_secrets=False)
        raw_config["auth_type"] = auth_type
        raw_config["auth"] = {
            "type": auth_type,
            "scopes": str(config.metadata.get("auth_scopes") or "").split(),
            "resource": str(config.metadata.get("auth_resource") or config.url),
        }
        auth_config = AuthRuntimeConfig.from_server_config(config.server_id, raw_config)
        key = (config.server_id, auth_config.config_fingerprint)
        with self._auth_lock:
            runtime = self._auth_runtimes.get(key)
            if runtime is None:
                cache_root = self.credential_vault.root.parent / "auth-cache"
                cache_root.mkdir(parents=True, exist_ok=True)
                cache_key = hashlib.sha256(config.server_id.encode("utf-8")).hexdigest()
                runtime = McpAuthRuntime(
                    auth_config,
                    credential_vault=self.credential_vault,
                    needs_auth_cache_path=cache_root / f"{cache_key}.needs-auth.json",
                    metadata_cache_path=cache_root / f"{cache_key}.metadata.json",
                    event_sink=self._auth_event,
                    state_store=self.state_store,
                )
                self._auth_runtimes[key] = runtime
            return runtime

    def _emit(self, event: EventRecord) -> None:
        if self.external_event_sink is not None:
            try:
                self.external_event_sink(event)
                return
            except ValueError:
                # Events without a canonical run/task partition remain in the
                # transient outbox until a caller supplies that identity.
                pass
        self.event_buffer.append(event)

    def _auth_event(self, value: Mapping[str, Any]) -> None:
        # Auth runtime already persists a redacted event in McpRuntimeStateStore.
        # Canonical task events are emitted when connect maps the resulting
        # auth state onto a run/task identity.
        return

    def _sampling_event(self, value: Mapping[str, Any]) -> None:
        # Sampling audit has no task identity at this callback boundary.  It is
        # retained in sampling diagnostics and never promoted with a fake id.
        return

    def _assert_enabled(self) -> None:
        if self.disabled:
            raise McpClientRuntimeDisabled("McpClientRuntime is disabled")


class _suppress_runtime_error:
    def __enter__(self) -> "_suppress_runtime_error":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: Any,
    ) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


def in_process_server_config(
    server_id: str,
    *,
    name: str | None = None,
    scope: McpConfigScope | str = McpConfigScope.DYNAMIC,
    approval: McpApprovalState | str = McpApprovalState.NOT_REQUIRED,
    metadata: Mapping[str, str] | None = None,
) -> McpServerConfig:
    return McpServerConfig(
        server_id=server_id,
        name=name or server_id,
        transport=McpTransportKind.IN_PROCESS,
        scope=McpConfigScope(str(scope)),
        approval=McpApprovalState(str(approval)),
        metadata=dict(metadata or {}),
    )


__all__ = [
    "McpClientRuntime",
    "McpClientRuntimeDisabled",
    "McpClientRuntimeError",
    "McpRuntimeEventBuffer",
    "McpWorkerProjection",
    "in_process_server_config",
]
