from __future__ import annotations

"""03A permission and task-control integration for local skill updates."""

import copy
import json
import os
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, MutableMapping

from .atomic_update import AtomicSkillPackageUpdater
from .digests import digest_object
from .integration_errors import (
    SkillPluginSupplyChainRejected,
    SkillPluginUpdateDenied,
)
from .models import SkillInvocationStatus, SkillSourceKind, utc_now
from .path_security import canonical_root
from .plugin_integration import PluginCapabilityIntegrationRuntime
from .update_runtime import (
    SkillUpdateAction,
    SkillUpdatePermissionDecision,
    SkillUpdatePreflight,
    SkillUpdateRequest,
    SkillUpdateRuntime,
    SkillUpdateState,
)


class ToolPermissionRuntimeSkillUpdateGateway:
    """Ask M1-03A for one exact local package-control operation."""

    def __init__(self, permission_runtime: Any, *, workspace_root: str | Path) -> None:
        self.permission_runtime = permission_runtime
        self.workspace_root = str(Path(workspace_root).resolve())

    def guard_update(
        self,
        request: SkillUpdateRequest,
        preflight: SkillUpdatePreflight,
    ) -> SkillUpdatePermissionDecision:
        try:
            from zyra_runtime.permission.models import PermissionEvaluationRequest, ToolIdentity
        except ImportError as error:
            raise SkillPluginUpdateDenied("03A permission contracts are unavailable") from error
        arguments = {
            **request.permission_arguments(),
            "preflight_digest": preflight.request_digest,
            "source_digest": preflight.source_digest,
            "base_digest": preflight.base_digest,
            "skill_count": preflight.skill_count,
            "total_bytes": preflight.total_bytes,
        }
        evaluation = PermissionEvaluationRequest(
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=request.session_id,
            worker_request_id=f"skill-update:{request.update_id}",
            tool_use_id=request.update_id,
            node_id="",
            tool_identity=ToolIdentity(
                namespace="skill_control",
                name=str(request.action),
                server_id=request.plugin_id or request.source_id or "local",
                version="M1-S03C-02",
                schema_digest=digest_object(arguments),
            ),
            arguments=arguments,
            operation="skill_package_control",
            workspace_root=self.workspace_root,
            interactive=request.interactive,
            headless=request.headless,
            requires_interaction=True,
            risk_tags=(
                "local_package_mutation",
                f"skill_update_action:{request.action}",
                f"skill_source:{request.source_kind}",
            ),
            attributes={
                "update_id": request.update_id,
                "plugin_id": request.plugin_id,
                "source_digest": preflight.source_digest,
            },
            metadata={
                "owner_unit": "M1-03C",
                "permission_owner": "M1-03A",
                "network_fetch_allowed": False,
                "dynamic_import_allowed": False,
            },
        )
        result = self.permission_runtime.guard(evaluation)
        effect = str(result.decision.effect)
        if result.execution_grant is not None:
            self.permission_runtime.grant_store.invalidate(
                result.execution_grant,
                reason="skill package control decision committed inside SkillUpdateRuntime",
            )
        events = tuple(result.events or ())
        decision = "allow" if effect == "allow" else "ask" if effect == "ask" else "deny"
        return SkillUpdatePermissionDecision(
            decision=decision,
            request_id=str(result.decision.request_id or request.update_id),
            reason=str(result.decision.reason or decision),
            events=events,
        )


@dataclass(frozen=True, slots=True)
class SkillUpdateControlInput:
    action: SkillUpdateAction
    channel: str
    bundle_name: str = ""
    plugin_id: str = ""
    expected_base_digest: str = ""
    receipt_id: str = ""
    reason: str = ""
    update_id: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SkillUpdateControlInput":
        allowed = {
            "action",
            "channel",
            "bundle_name",
            "plugin_id",
            "expected_base_digest",
            "receipt_id",
            "reason",
            "update_id",
        }
        unknown = set(value) - allowed
        if unknown:
            raise SkillPluginSupplyChainRejected(
                "skill update control contains unknown fields",
                detail={"fields": sorted(unknown)},
            )
        try:
            action = SkillUpdateAction(str(value.get("action") or ""))
        except ValueError as error:
            raise SkillPluginSupplyChainRejected("skill update action is invalid") from error
        channel = str(value.get("channel") or "project").strip()
        if channel not in {"project", "plugin"}:
            raise SkillPluginSupplyChainRejected("skill update channel must be project or plugin")
        bundle_name = _safe_name(str(value.get("bundle_name") or ""), optional=action not in {SkillUpdateAction.INSTALL, SkillUpdateAction.UPDATE})
        plugin_id = _safe_name(str(value.get("plugin_id") or ""), optional=channel != "plugin")
        return cls(
            action=action,
            channel=channel,
            bundle_name=bundle_name,
            plugin_id=plugin_id,
            expected_base_digest=str(value.get("expected_base_digest") or ""),
            receipt_id=str(value.get("receipt_id") or ""),
            reason=str(value.get("reason") or "")[:1_000],
            update_id=str(value.get("update_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": str(self.action),
            "channel": self.channel,
            "bundle_name": self.bundle_name,
            "plugin_id": self.plugin_id,
            "expected_base_digest": self.expected_base_digest,
            "receipt_id": self.receipt_id,
            "reason": self.reason,
            "update_id": self.update_id,
        }


@dataclass(frozen=True, slots=True)
class SkillUpdateControlReceipt:
    control_id: str
    state: SkillUpdateState
    task_metadata: dict[str, Any]
    event_payloads: tuple[dict[str, Any], ...]
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.skill-update-control.v1",
            "control_id": self.control_id,
            "state": self.state.to_dict(),
            "task_metadata": copy.deepcopy(self.task_metadata),
            "event_payloads": [copy.deepcopy(item) for item in self.event_payloads],
            "created_at": self.created_at,
        }


class SkillUpdateControlRuntime:
    """Resolve trusted local roots and execute updates through 03A + 03C."""

    def __init__(
        self,
        *,
        product_root: str | Path,
        workspace_root: str | Path,
        permission_runtime: Any,
        skill_runtime: Any,
        plugin_runtime: PluginCapabilityIntegrationRuntime | None = None,
        state_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        self.product_root = canonical_root(product_root)
        self.workspace_root = canonical_root(workspace_root)
        self.import_root = self.workspace_root / ".zyra" / "skill-imports"
        self.project_target_root = self.workspace_root / ".zyra" / "skills"
        self.plugin_target_root = self.workspace_root / ".zyra" / "plugins"
        self.control_root = self.workspace_root / ".zyra" / "skill-control"
        self.import_root.mkdir(parents=True, exist_ok=True)
        self.project_target_root.mkdir(parents=True, exist_ok=True)
        self.plugin_target_root.mkdir(parents=True, exist_ok=True)
        self.control_root.mkdir(parents=True, exist_ok=True)
        self.plugin_runtime = plugin_runtime or PluginCapabilityIntegrationRuntime(
            tuple(path for path in self.plugin_target_root.iterdir() if path.is_dir())
        )
        self.plugin_state_path = self.control_root / "plugin-state.json"
        self._restore_plugin_controls()
        self.skill_runtime = skill_runtime
        updater = AtomicSkillPackageUpdater(
            staging_root=self.control_root / "staging",
            backup_root=self.control_root / "backups",
        )
        self.runtime = SkillUpdateRuntime(
            updater=updater,
            permission_port=ToolPermissionRuntimeSkillUpdateGateway(
                permission_runtime,
                workspace_root=self.workspace_root,
            ),
            skill_runtime=skill_runtime,
            plugin_runtime=self.plugin_runtime,
            runtime_rebuilder=self._rebuild_runtime,
        )
        self.runtime.restore_pending(state_snapshot)
        self._lock = RLock()

    def execute(
        self,
        control: SkillUpdateControlInput | Mapping[str, Any],
        *,
        run_id: str,
        task_id: str,
        session_id: str,
        task_metadata: MutableMapping[str, Any],
    ) -> SkillUpdateControlReceipt:
        selected = control if isinstance(control, SkillUpdateControlInput) else SkillUpdateControlInput.from_mapping(control)
        source_root, target_root, source_kind, source_id, namespace = self._roots(selected)
        update_id = selected.update_id or digest_object(
            {
                "run_id": run_id,
                "task_id": task_id,
                "session_id": session_id,
                "control": selected.to_dict(),
                "time": utc_now(),
            }
        )[:32]
        request = SkillUpdateRequest(
            update_id=update_id,
            action=selected.action,
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            source_root=str(source_root),
            target_root=str(target_root),
            source_kind=source_kind,
            source_id=source_id,
            namespace=namespace,
            expected_base_digest=selected.expected_base_digest,
            plugin_id=selected.plugin_id,
            receipt_id=selected.receipt_id,
            reason=selected.reason,
            interactive=True,
            headless=False,
        )
        try:
            existing = self.runtime.state(update_id)
        except Exception:
            existing = None
        try:
            state = (
                self.runtime.resume(update_id)
                if existing is not None
                else self.runtime.request(request)
            )
        except Exception:
            try:
                pending_state = self.runtime.state(update_id)
            except Exception:
                raise
            self._persist_task_state(task_metadata, pending_state)
            raise
        self._persist_task_state(task_metadata, state)
        if state.status.terminal:
            self._persist_plugin_controls()
        return self._receipt(state, task_metadata)

    def _persist_task_state(
        self,
        task_metadata: MutableMapping[str, Any],
        state: SkillUpdateState,
    ) -> None:
        with self._lock:
            projection = task_metadata.setdefault("skill_update_control", {})
            if not isinstance(projection, MutableMapping):
                raise SkillPluginSupplyChainRejected("task skill update metadata is invalid")
            update_id = state.request.update_id
            projection[update_id] = state.to_dict()
            projection["latest_update_id"] = update_id
            projection["updated_at"] = state.updated_at
            projection["state_owner"] = "SkillUpdateRuntime"
            projection["permission_owner"] = "M1-03A"
            task_metadata["skill_update_runtime_state"] = self.runtime.snapshot()
            if "updates" in projection and not isinstance(projection["updates"], Mapping):
                del projection["updates"]

    def _receipt(
        self,
        state: SkillUpdateState,
        task_metadata: Mapping[str, Any],
    ) -> SkillUpdateControlReceipt:
        update_id = state.request.update_id
        event_payloads = tuple(
            {
                "phase": "skill_update_permission",
                "owner_unit": "M1-03C",
                "update_id": update_id,
                "permission_owner": "M1-03A",
                "event": _jsonable_permission_event(event),
            }
            for event in (state.permission_decision.events if state.permission_decision else ())
        )
        control_id = digest_object(
            {
                "update_id": update_id,
                "state_revision": state.revision,
                "status": str(state.status),
            }
        )[:40]
        return SkillUpdateControlReceipt(
            control_id=control_id,
            state=state,
            task_metadata={
                "latest_update_id": update_id,
                "status": str(state.status),
                "revision": state.revision,
                "invalidated_invocations": list(state.invalidated_invocations),
            },
            event_payloads=event_payloads,
        )

    def _roots(
        self,
        control: SkillUpdateControlInput,
    ) -> tuple[Path, Path, SkillSourceKind, str, str]:
        if control.channel == "plugin":
            name = control.plugin_id or control.bundle_name
            source = self.import_root / control.bundle_name if control.bundle_name else self.import_root
            target = self.plugin_target_root / name
            kind = SkillSourceKind.PLUGIN
            source_id = f"plugin:{name}"
            namespace = f"plugin:{name}"
        else:
            source = self.import_root / control.bundle_name if control.bundle_name else self.import_root
            target = self.project_target_root
            kind = SkillSourceKind.PROJECT
            source_id = "project:update"
            namespace = "project"
        source = source.resolve()
        target = target.resolve()
        source.relative_to(self.import_root.resolve())
        target.relative_to(self.workspace_root.resolve())
        if control.action in {SkillUpdateAction.INSTALL, SkillUpdateAction.UPDATE} and not source.is_dir():
            raise SkillPluginSupplyChainRejected(
                "skill update bundle was not found in the trusted import root",
                detail={"bundle_name": control.bundle_name},
            )
        return source, target, kind, source_id, namespace

    def _rebuild_runtime(self, request: SkillUpdateRequest) -> Any:
        """Publish a composition containing a newly committed source root."""

        previous = self.skill_runtime
        config = previous.config
        target = str(Path(request.target_root).resolve())
        project_roots = tuple(config.project_roots)
        plugin_roots = tuple(config.plugin_roots)
        if request.source_kind is SkillSourceKind.PROJECT:
            if Path(target).is_dir() and target not in project_roots:
                project_roots = (*project_roots, target)
            elif not Path(target).is_dir():
                project_roots = tuple(
                    root
                    for root in project_roots
                    if str(Path(root).resolve()) != target
                )
        if request.source_kind is SkillSourceKind.PLUGIN:
            disabled_plugins = self.plugin_runtime.snapshot().disabled
            if (
                not Path(target).is_dir()
                or request.plugin_id and request.plugin_id in disabled_plugins
            ):
                plugin_roots = tuple(
                    root
                    for root in plugin_roots
                    if str(Path(root).resolve()) != target
                )
            elif target not in plugin_roots:
                plugin_roots = (*plugin_roots, target)
        rebuilt_config = replace(
            config,
            project_roots=project_roots,
            plugin_roots=plugin_roots,
            composition_id=digest_object(
                {
                    "previous": config.composition_id,
                    "project_roots": project_roots,
                    "plugin_roots": plugin_roots,
                    "update_id": request.update_id,
                }
            )[:32],
        )
        rebuilt = type(previous)(
            rebuilt_config,
            permission_port=previous.invocation_runtime.permission_port,
            fork_port=previous.fork_queue,
            state_snapshot=previous.state_snapshot(),
        )
        rebuilt.bootstrap()
        invalidated: list[str] = []
        for state in rebuilt.state_store.all_states():
            if state.status.terminal:
                continue
            try:
                active = rebuilt.registry.resolve(state.version_ref.qualified_name)
            except Exception:
                active = None
            if active is not None and active.version_ref.immutable_ref == state.version_ref.immutable_ref:
                continue
            rebuilt.state_store.transition(
                state.invocation_id,
                SkillInvocationStatus.REVOKED,
                expected_revision=state.revision,
                error_code="skill_revision_replaced_by_update",
                error_message="active skill revision changed during package publication",
            )
            rebuilt.allowed_tools_policy.unbind(state.invocation_id)
            rebuilt.hook_runtime.cleanup_invocation(
                state.invocation_id,
                reason="skill package update replaced active revision",
            )
            invalidated.append(state.invocation_id)
        setattr(rebuilt, "_skill_update_invalidated", tuple(sorted(invalidated)))
        self.skill_runtime = rebuilt
        if request.source_kind is SkillSourceKind.PLUGIN:
            self.plugin_runtime.configure_roots(
                Path(item) for item in plugin_roots if Path(item).exists()
            )
        return rebuilt

    def _restore_plugin_controls(self) -> None:
        if not self.plugin_state_path.is_file():
            return
        try:
            value = json.loads(self.plugin_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SkillPluginSupplyChainRejected(
                "persisted plugin control state is invalid"
            ) from error
        disabled = value.get("disabled") if isinstance(value, Mapping) else None
        if not isinstance(disabled, Mapping):
            raise SkillPluginSupplyChainRejected("persisted plugin disabled state is invalid")
        self.plugin_runtime.restore_disabled(
            {str(key): str(reason) for key, reason in disabled.items()}
        )

    def _persist_plugin_controls(self) -> None:
        disabled = self.plugin_runtime.snapshot().disabled
        payload = {
            "schema": "zyra.plugin-control-state.v1",
            "disabled": dict(disabled),
            "updated_at": utc_now(),
        }
        temporary = self.plugin_state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.plugin_state_path)


def _safe_name(value: str, *, optional: bool) -> str:
    selected = value.strip()
    if not selected:
        if optional:
            return ""
        raise SkillPluginSupplyChainRejected("skill update bundle/plugin name is required")
    if len(selected) > 120 or selected in {".", ".."}:
        raise SkillPluginSupplyChainRejected("skill update name is invalid")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in selected):
        raise SkillPluginSupplyChainRejected("skill update name contains unsupported characters")
    return selected


def _jsonable_permission_event(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return copy.deepcopy(to_dict())
    if is_dataclass(value):
        return copy.deepcopy(asdict(value))
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"type": type(value).__name__, "value": str(value)}


__all__ = [
    "SkillUpdateControlInput",
    "SkillUpdateControlReceipt",
    "SkillUpdateControlRuntime",
    "ToolPermissionRuntimeSkillUpdateGateway",
]
