from __future__ import annotations

"""Unified permission barrier for BrowserWorker actions.

This module deliberately owns no browser implementation.  It translates one
Zyra BrowserWorker action into the same ``ToolPermissionRuntime`` contract used
by CodeWorker, then requires the returned one-use grant to be consumed against
the exact action identity immediately before the browser callback runs.

The current boundary guards explicit BrowserWorker plan actions and the
browser-use Agent task entry.  M1-04C may later move the same contract deeper
into browser-use's action registry without changing its state owner.
"""

import copy
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from zyra_core import EventRecord, EventType

from ..tools import ToolCall, ToolRegistry, ToolSpec
from .canonical import arguments_digest, build_tool_identity, canonicalize_arguments
from .custody import (
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyError,
    PermissionSessionCustodyReceipt,
    PermissionSessionCustodyStore,
)
from .models import PermissionEffect, PermissionEvaluationRequest
from .modes import ModeName
from .runtime import (
    PermissionGuardResult,
    PermissionRuntimeConfig,
    ToolPermissionRuntime,
    ToolPermissionRuntimeDisabledError,
)
from .store import PermissionStateStore


BROWSER_ACTION_GATE_RUNTIME_ID = "zyra-browser-action-permission-gate"
BROWSER_ACTION_GATE_OWNER_UNIT = "M1-S03A-02"
BROWSER_ACTION_IDENTITY_VERSION = "zyra-browser-action-v1"


class BrowserActionPermissionError(RuntimeError):
    code = "browser_action_permission_error"


class BrowserActionPermissionDisabled(BrowserActionPermissionError):
    code = "browser_action_permission_disabled"

    def __init__(self, component: str) -> None:
        super().__init__(f"browser action permission dependency is disabled: {component}")
        self.component = component


class BrowserActionPermissionCustodyError(BrowserActionPermissionError):
    code = "browser_action_permission_custody_error"

    def __init__(self, cause: PermissionSessionCustodyError) -> None:
        super().__init__(str(cause))
        self.component = type(cause).__name__
        self.cause_code = cause.code


class BrowserActionPermissionIdentityError(BrowserActionPermissionError):
    code = "browser_action_permission_identity_mismatch"


@dataclass(frozen=True, slots=True)
class BrowserActionPermissionInput:
    step_index: int
    action: str
    normalized_action: str
    backend: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    source_action: str = ""
    source_model: str = ""
    required_arguments: tuple[str, ...] = ()
    optional_arguments: tuple[str, ...] = ()
    current_url: str = ""
    target_url: str = ""
    explicit_tool_use_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.step_index < 1:
            raise ValueError("browser permission step_index must be positive")
        if not self.normalized_action.strip() or not self.backend.strip():
            raise ValueError("browser permission action and backend are required")
        canonical = canonicalize_arguments(dict(self.arguments))
        object.__setattr__(self, "arguments", canonical)
        object.__setattr__(self, "required_arguments", tuple(str(item) for item in self.required_arguments))
        object.__setattr__(self, "optional_arguments", tuple(str(item) for item in self.optional_arguments))
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))

    @property
    def permission_arguments(self) -> dict[str, Any]:
        # Keep action/backend inside the canonical digest.  A permission for an
        # ``input_text`` payload can never be replayed as ``evaluate_js`` even
        # if a transport accidentally reuses the tool_use id.
        return canonicalize_arguments(
            {
                **dict(self.arguments),
                "_zyra_browser_action": self.normalized_action,
                "_zyra_browser_backend": self.backend,
                "_zyra_browser_current_url": self.current_url,
                "_zyra_browser_target_url": self.target_url,
            }
        )

    @property
    def capabilities(self) -> tuple[str, ...]:
        return _browser_action_capabilities(self)

    @property
    def network(self) -> bool:
        return "network" in self.capabilities

    @property
    def read_only(self) -> bool:
        return "read_only" in self.capabilities and not bool(
            set(self.capabilities).intersection({"external_side_effect", "mutation", "upload", "download"})
        )

    def schema(self) -> dict[str, Any]:
        names = sorted(set((*self.required_arguments, *self.optional_arguments)))
        return {
            "type": "object",
            "required": list(self.required_arguments),
            "properties": {name: {} for name in names},
            "additionalProperties": True,
            "x-zyra-browser-action": self.normalized_action,
            "x-zyra-browser-backend": self.backend,
            "x-upstream-source-action": self.source_action,
            "x-upstream-source-model": self.source_model,
        }


@dataclass(frozen=True, slots=True)
class _BrowserPermissionExecutionView:
    workspace_root: Path
    registry: ToolRegistry


@dataclass(frozen=True, slots=True)
class _BrowserActionMaterial:
    action: BrowserActionPermissionInput
    evaluation_request: PermissionEvaluationRequest
    call: ToolCall
    execution_view: _BrowserPermissionExecutionView


@dataclass(frozen=True, slots=True)
class BrowserActionPermissionDecision:
    material: _BrowserActionMaterial
    guard: PermissionGuardResult
    events: tuple[EventRecord, ...]

    @property
    def allowed(self) -> bool:
        return self.guard.allowed

    @property
    def effect(self) -> PermissionEffect:
        return self.guard.effect

    @property
    def request_id(self) -> str:
        return self.guard.decision.request_id

    @property
    def decision_id(self) -> str:
        return self.guard.decision.decision_id

    @property
    def tool_use_id(self) -> str:
        return self.material.call.tool_call_id

    def metadata(self) -> dict[str, str]:
        return {
            "permission_runtime_id": BROWSER_ACTION_GATE_RUNTIME_ID,
            "permission_effect": str(self.effect),
            "permission_decision_id": self.decision_id,
            "permission_request_id": self.request_id,
            "permission_tool_use_id": self.tool_use_id,
            "permission_arguments_digest": self.guard.decision.arguments_digest,
            "permission_action_allowed": str(self.allowed).lower(),
        }


@dataclass(frozen=True, slots=True)
class BrowserActionPermissionConsumption:
    accepted: bool
    decision: BrowserActionPermissionDecision
    replay: BrowserActionPermissionInput
    events: tuple[EventRecord, ...]
    reason: str

    @property
    def authorizes_execution(self) -> bool:
        return self.accepted

    def metadata(self) -> dict[str, str]:
        return {
            **self.decision.metadata(),
            "permission_execution_grant_consumed": str(self.accepted).lower(),
            "permission_action_boundary_reason": self.reason,
        }


class BrowserActionPermissionGate:
    """Session-owned BrowserWorker facade over ``ToolPermissionRuntime``."""

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        tool_use_id_base: str,
        workspace_root: str | Path,
        state_store: PermissionStateStore,
        runtime: ToolPermissionRuntime,
        custody: PermissionSessionCustodyReceipt,
        requested_mode: str,
        persisted_mode: str,
        effective_mode: str,
        caller_bypass_ignored: bool,
        caller_auto_ignored: bool,
        redaction_secrets: tuple[str, ...] = (),
    ) -> None:
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.node_id = node_id
        self.worker_request_id = str(worker_request_id)
        self.session_id = str(session_id)
        self.tool_use_id_base = str(tool_use_id_base)
        self.workspace_root = Path(workspace_root).resolve()
        self.state_store = state_store
        self.runtime = runtime
        self.custody = custody
        self.requested_mode = requested_mode
        self.persisted_mode = persisted_mode
        self.effective_mode = effective_mode
        self.caller_bypass_ignored = bool(caller_bypass_ignored)
        self.caller_auto_ignored = bool(caller_auto_ignored)
        self.redaction_secrets = tuple(
            dict.fromkeys(str(item) for item in redaction_secrets if str(item))
        )

    @classmethod
    def for_worker_request(
        cls,
        request: Any,
        *,
        workspace_root: str | Path,
        state_path: str | Path,
    ) -> "BrowserActionPermissionGate":
        constraints = dict(getattr(request, "constraints", {}) or {})
        disabled_fields = (
            ("disable_browser_action_permission_gate", "BrowserActionPermissionGate"),
            ("disable_tool_permission_runtime", "ToolPermissionRuntime"),
            ("disable_permission_rule_store", "PermissionRuleStore"),
            ("disable_permission_request_queue", "PermissionRequestQueue"),
            ("disable_permission_decision_log", "PermissionDecisionLog"),
        )
        for field_name, component in disabled_fields:
            if constraints.get(field_name) is True:
                raise BrowserActionPermissionDisabled(component)

        run_id = str(getattr(request, "run_id", ""))
        task_id = str(getattr(request, "task_id", ""))
        worker_request_id = str(getattr(request, "request_id", ""))
        node_id = getattr(request, "node_id", None)
        workspace = Path(workspace_root).resolve()
        session_id = str(constraints.get("permission_session_id") or "").strip()
        if not session_id:
            session_id = _default_session_id(run_id=run_id, task_id=task_id, workspace_root=workspace)
        tool_use_id_base = str(constraints.get("permission_tool_use_id") or "").strip()

        state_store = PermissionStateStore(state_path)
        binding = PermissionSessionCustodyBinding(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            workspace_root=str(workspace),
        )
        presented_token = str(
            constraints.get("permission_session_custody_token")
            or constraints.get("session_custody_token")
            or ""
        )
        try:
            custody = PermissionSessionCustodyStore(state_store).claim(
                binding,
                presented_token=presented_token,
            )
        except PermissionSessionCustodyError as error:
            raise BrowserActionPermissionCustodyError(error) from error
        requested_mode = str(constraints.get("permission_mode") or "default")
        _, caller_bypass_ignored, caller_auto_ignored = _effective_mode(requested_mode, constraints)
        persisted_mode = _persisted_session_mode(state_store, session_id=session_id)
        selected_mode = persisted_mode or requested_mode
        effective_mode, selected_bypass_ignored, selected_auto_ignored = _effective_mode(selected_mode, {})
        bypass_ignored = caller_bypass_ignored or selected_bypass_ignored
        auto_ignored = caller_auto_ignored or selected_auto_ignored
        headless = constraints.get("permission_headless") is True or effective_mode in {
            str(ModeName.SEALED),
            str(ModeName.DONT_ASK),
        }
        interactive = (
            constraints.get("permission_interactive") is not False
            and not headless
            and effective_mode != str(ModeName.SEALED)
        )
        try:
            runtime = ToolPermissionRuntime.for_session(
                session_id=session_id,
                state_path=state_path,
                config=PermissionRuntimeConfig(
                    mode=effective_mode,
                    approval_ttl_seconds=_bounded_float(
                        constraints.get("permission_approval_ttl_seconds"),
                        default=300.0,
                        minimum=1.0,
                        maximum=300.0,
                    ),
                    execution_grant_ttl_seconds=_bounded_float(
                        constraints.get("permission_execution_grant_ttl_seconds"),
                        default=30.0,
                        minimum=1.0,
                        maximum=30.0,
                    ),
                    bypass_available=False,
                    auto_available=False,
                    interactive=interactive,
                    headless=headless,
                    metadata={
                        "source": "browser_worker",
                        "worker_request_id": worker_request_id,
                        "requested_mode": requested_mode,
                        "persisted_mode": persisted_mode,
                        "caller_bypass_ignored": bypass_ignored,
                        "caller_auto_ignored": auto_ignored,
                    },
                ),
                workspace_root=workspace,
                custody_fingerprint=custody.custody_fingerprint,
            )
        except ToolPermissionRuntimeDisabledError as error:
            raise BrowserActionPermissionDisabled(error.component) from error
        return cls(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_request_id=worker_request_id,
            session_id=session_id,
            tool_use_id_base=tool_use_id_base,
            workspace_root=workspace,
            state_store=state_store,
            runtime=runtime,
            custody=custody,
            requested_mode=requested_mode,
            persisted_mode=persisted_mode,
            effective_mode=effective_mode,
            caller_bypass_ignored=bypass_ignored,
            caller_auto_ignored=auto_ignored,
            redaction_secrets=tuple(
                item for item in (presented_token, custody.token) if item
            ),
        )

    @property
    def custody_token(self) -> str:
        return self.custody.token

    def metadata(self) -> dict[str, str]:
        return {
            **self.custody.metadata(),
            **self.runtime.metadata(),
            "browser_permission_gate_runtime_id": BROWSER_ACTION_GATE_RUNTIME_ID,
            "browser_permission_gate_owner_unit": BROWSER_ACTION_GATE_OWNER_UNIT,
            "browser_permission_requested_mode": self.requested_mode,
            "browser_permission_persisted_mode": self.persisted_mode,
            "browser_permission_mode_source": "state_owner" if self.persisted_mode else "request",
            "browser_permission_effective_mode": self.effective_mode,
            "browser_permission_caller_bypass_ignored": str(self.caller_bypass_ignored).lower(),
            "browser_permission_caller_auto_ignored": str(self.caller_auto_ignored).lower(),
        }

    def guard(self, action: BrowserActionPermissionInput) -> BrowserActionPermissionDecision:
        material = self._material(action)
        result = self.runtime.guard(
            material.evaluation_request,
            workspace_state=_workspace_state(action, self.workspace_root),
        )
        events = tuple(_sanitize_permission_event(event) for event in result.events)
        decision_event_id = _decision_event_id(events)
        if not result.allowed:
            blocked, recovery = _blocked_events(
                run_id=self.run_id,
                task_id=self.task_id,
                node_id=self.node_id,
                worker_request_id=self.worker_request_id,
                session_id=self.session_id,
                action=action,
                guard=result,
                cause_event_id=decision_event_id,
            )
            events = (*events, blocked, recovery)
        return BrowserActionPermissionDecision(material=material, guard=result, events=events)

    def consume(
        self,
        decision: BrowserActionPermissionDecision,
        replay: BrowserActionPermissionInput | None = None,
    ) -> BrowserActionPermissionConsumption:
        if not decision.allowed or decision.guard.execution_grant is None:
            return BrowserActionPermissionConsumption(
                accepted=False,
                decision=decision,
                replay=replay or decision.material.action,
                events=(),
                reason="permission decision did not issue an execution grant",
            )
        selected = replay or decision.material.action
        try:
            replay_material = self._material(selected)
            accepted = self.runtime.validate_and_consume(
                replay_material.call,
                decision.guard.execution_grant,
                replay_material.execution_view,
            )
        except Exception as error:  # noqa: BLE001 - action boundary must fail closed.
            accepted = False
            failure_reason = f"grant validation failed: {type(error).__name__}"
        else:
            failure_reason = "exact one-use browser action grant consumed" if accepted else "grant identity mismatch or replay"
        events = tuple(
            _sanitize_permission_event(event)
            for event in self.runtime.drain_execution_events(decision.tool_use_id)
        )
        if not accepted:
            rejected, recovery = _rejected_events(
                run_id=self.run_id,
                task_id=self.task_id,
                node_id=self.node_id,
                worker_request_id=self.worker_request_id,
                session_id=self.session_id,
                action=selected,
                decision=decision,
                reason=failure_reason,
                cause_event_id=_last_event_id(events) or _decision_event_id(decision.events),
            )
            events = (*events, rejected, recovery)
        return BrowserActionPermissionConsumption(
            accepted=accepted,
            decision=decision,
            replay=selected,
            events=events,
            reason=failure_reason,
        )

    def _material(self, action: BrowserActionPermissionInput) -> _BrowserActionMaterial:
        if PermissionSessionCustodyStore.contains_capability_echo(
            {"browser_action_arguments": dict(action.arguments)},
            self.redaction_secrets,
        ):
            raise BrowserActionPermissionIdentityError(
                "browser action arguments cannot contain the session custody capability"
            )
        permission_arguments = action.permission_arguments
        schema = action.schema()
        source = f"zyra-browser-worker:{action.backend}"
        # BrowserWorker is a Zyra-owned in-process executor, not a remote/MCP
        # server.  The backend remains part of both the canonical arguments and
        # schema identity; leaving server_id empty prevents local file reads
        # from being misclassified as remote merely because they have a backend.
        server_id = ""
        identity = build_tool_identity(
            action.normalized_action,
            namespace="browser",
            server_id=server_id,
            version=BROWSER_ACTION_IDENTITY_VERSION,
            schema=schema,
        )
        digest = arguments_digest(permission_arguments)
        tool_use_id = action.explicit_tool_use_id or _tool_use_id(
            base=self.tool_use_id_base,
            session_id=self.session_id,
            run_id=self.run_id,
            task_id=self.task_id,
            step_index=action.step_index,
            action=action.normalized_action,
            arguments_digest_value=digest,
        )
        spec = ToolSpec(
            name=action.normalized_action,
            purpose=f"BrowserWorker action {action.normalized_action}",
            source=source,
            input_schema=schema,
            metadata={
                "tool_namespace": identity.namespace,
                "server_id": identity.server_id,
                "tool_version": identity.version,
                "capabilities": ",".join(action.capabilities),
                "read_only": str(action.read_only).lower(),
                "source_path": "packages/workers/zyra_workers/browser_worker.py",
            },
        )
        registry = ToolRegistry([spec])
        metadata = {
            "tool_namespace": identity.namespace,
            "server_id": identity.server_id,
            "tool_version": identity.version,
            "registered_tool_source": source,
            "browser_backend": action.backend,
            "browser_step_index": str(action.step_index),
            "browser_source_action": action.source_action,
            "browser_source_model": action.source_model,
        }
        call = ToolCall(
            run_id=self.run_id,
            task_id=self.task_id,
            node_id=self.node_id,
            tool_name=action.normalized_action,
            arguments=copy.deepcopy(permission_arguments),
            tool_call_id=tool_use_id,
            metadata=metadata,
        )
        evaluation_request = PermissionEvaluationRequest(
            run_id=self.run_id,
            task_id=self.task_id,
            session_id=self.session_id,
            worker_request_id=self.worker_request_id,
            turn_id=f"browser-action:{action.step_index}",
            node_id=self.node_id,
            tool_use_id=tool_use_id,
            tool_identity=identity,
            arguments=copy.deepcopy(permission_arguments),
            workspace_root=str(self.workspace_root),
            interactive=self.runtime.config.interactive,
            headless=self.runtime.config.headless,
            safety_flags=tuple(str(item) for item in action.metadata.get("safety_flags", ()) if str(item)),
            risk_tags=tuple(str(item) for item in action.metadata.get("risk_tags", ()) if str(item)),
            attributes={
                "capabilities": list(action.capabilities),
                "read_only": action.read_only,
                "network": action.network,
                "browser_backend": action.backend,
                "browser_action": action.normalized_action,
                "current_url": action.current_url,
                "target_url": action.target_url,
                "path": str(action.arguments.get("path") or ""),
                "domain": _action_domain(action),
            },
            metadata={
                "owner_unit": BROWSER_ACTION_GATE_OWNER_UNIT,
                "runtime_id": BROWSER_ACTION_GATE_RUNTIME_ID,
                "registered_tool_source": source,
                "browser_step_index": action.step_index,
                "caller_bypass_ignored": self.caller_bypass_ignored,
                "caller_auto_ignored": self.caller_auto_ignored,
            },
        )
        return _BrowserActionMaterial(
            action=action,
            evaluation_request=evaluation_request,
            call=call,
            execution_view=_BrowserPermissionExecutionView(
                workspace_root=self.workspace_root,
                registry=registry,
            ),
        )


def browser_permission_setup_failure_events(
    request: Any,
    *,
    session_id: str,
    component: str,
    reason: str,
    code: str,
) -> tuple[EventRecord, EventRecord]:
    run_id = str(getattr(request, "run_id", ""))
    task_id = str(getattr(request, "task_id", ""))
    node_id = getattr(request, "node_id", None)
    worker_request_id = str(getattr(request, "request_id", ""))
    blocked = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="browser_action_permission_unavailable",
        effect="deny",
        reason=reason,
        payload={
            "component": component,
            "error_code": code,
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    recovery = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="recovery_input",
        effect="deny",
        reason="browser action permission authority must be restored before retry",
        cause_event_id=blocked.event_id,
        payload={
            "component": component,
            "retryable": True,
            "alternatives": [
                "restore the session-owned permission runtime",
                "use a local read-only diagnostic without a browser side effect",
            ],
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    return blocked, recovery


def _browser_action_capabilities(action: BrowserActionPermissionInput) -> tuple[str, ...]:
    normalized = action.normalized_action
    target_url = action.target_url or str(action.arguments.get("url") or action.current_url or "")
    scheme = urlparse(target_url).scheme.casefold() if target_url else ""
    local_resource = scheme in {"", "file"}
    local_read_actions = {
        "open_url",
        "navigate",
        "extract_text",
        "snapshot_state",
        "search_page",
        "wait",
        "take_screenshot",
        "save_as_pdf",
        "collect_downloads",
        "scroll_page",
        "scroll_to_text",
        "go_back",
        "list_targets",
        "focus_target",
        "take_screenshot",
        "capture_trace",
    }
    local_virtual_actions = {"click_element", "input_text", "send_keys"}
    # These actions only inspect the already-owned 04A browser session.  They
    # do not initiate network dispatch, even when the active page URL is
    # https://.  Content capture and topology mutation are intentionally absent
    # so their 04C risk classifications cannot inherit the low-risk read path.
    session_read_actions = {
        "snapshot_state",
        "search_page",
        "get_dropdown_options",
        "list_targets",
        "wait",
        "capture_trace",
    }
    capabilities: list[str] = ["browser_action"]
    if normalized in session_read_actions or (local_resource and normalized in local_read_actions) or (
        action.backend == "static" and local_resource and normalized in local_virtual_actions
    ):
        # The static backend cannot submit forms or execute page JavaScript;
        # file:// navigation and its virtual inputs are local simulation only.
        capabilities.extend(("read_only", "local_browser_state"))
    else:
        capabilities.append("network")
        if normalized in {
            "click_element",
            "input_text",
            "upload_file",
            "send_keys",
            "evaluate_js",
            "open_url",
            "go_back",
        } or action.backend == "browser-use-agent":
            capabilities.append("external_side_effect")
        else:
            capabilities.append("read_only")
    if normalized == "upload_file":
        capabilities.extend(("upload", "external_side_effect"))
    if normalized == "collect_downloads":
        capabilities.append("download")
    if normalized == "evaluate_js":
        capabilities.extend(("mutation", "external_side_effect"))
    return tuple(dict.fromkeys(capabilities))


def _workspace_state(action: BrowserActionPermissionInput, workspace_root: Path) -> dict[str, Any]:
    root = workspace_root.resolve()
    path_safe = True
    raw_path = str(action.arguments.get("path") or "").strip()
    target: Path | None = None
    if raw_path:
        try:
            candidate = Path(raw_path).expanduser()
            target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            path_safe = False
    for url in (action.target_url, str(action.arguments.get("url") or "")):
        parsed = urlparse(url)
        if parsed.scheme != "file":
            continue
        try:
            path_text = url2pathname(unquote(parsed.path))
            if parsed.netloc:
                path_text = f"//{parsed.netloc}{path_text}"
            candidate = Path(path_text).resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            path_safe = False
    return {
        "workspace_root": str(root),
        "workspace_scoped": path_safe,
        "path_validated": path_safe,
        "read_before_write": action.read_only,
        "baseline_current": action.read_only,
        "bounded_change": action.read_only,
        "trusted_remote": False,
        "production": bool(
            action.arguments.get("production")
            or str(action.arguments.get("environment") or "").casefold() == "production"
        ),
        "contains_secrets": bool(
            action.arguments.get("contains_secrets")
            or action.arguments.get("secret_material")
        ),
        "external_egress": action.network,
        "cross_repository": False,
        "outside_workspace": not path_safe,
        "workspace_precondition": _path_precondition(target) if target is not None and path_safe else {},
    }


def _path_precondition(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    except OSError as error:
        return {"exists": None, "error": type(error).__name__}
    return {
        "exists": True,
        "device": int(getattr(stat, "st_dev", 0)),
        "inode": int(getattr(stat, "st_ino", 0)),
        "mode": int(stat.st_mode),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _default_session_id(*, run_id: str, task_id: str, workspace_root: Path) -> str:
    encoded = json.dumps(
        {
            "schema": "zyra.browser-permission-session.v1",
            "run_id": run_id,
            "task_id": task_id,
            "workspace_root": str(workspace_root.resolve()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"browser-permission-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def _tool_use_id(
    *,
    base: str,
    session_id: str,
    run_id: str,
    task_id: str,
    step_index: int,
    action: str,
    arguments_digest_value: str,
) -> str:
    if base:
        return base if step_index == 1 else f"{base}:{step_index}"
    encoded = json.dumps(
        {
            "schema": "zyra.browser-permission-tool-use.v1",
            "session_id": session_id,
            "run_id": run_id,
            "task_id": task_id,
            "step_index": step_index,
            "action": action,
            "arguments_digest": arguments_digest_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"browser-tool-use-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _persisted_session_mode(state_store: PermissionStateStore, *, session_id: str) -> str:
    """Read the durable control-plane mode without creating another state owner."""

    state = state_store.read_state()
    metadata = state.get("metadata", {})
    integration = metadata.get("permission_integration", {}) if isinstance(metadata, Mapping) else {}
    modes = integration.get("session_modes", {}) if isinstance(integration, Mapping) else {}
    record = modes.get(session_id) if isinstance(modes, Mapping) else None
    if not isinstance(record, Mapping):
        return ""
    return str(record.get("mode") or "").strip()


def _effective_mode(requested: str, constraints: Mapping[str, Any]) -> tuple[str, bool, bool]:
    normalized = str(requested or "default").strip()
    aliases = {
        "workspace": str(ModeName.DEFAULT),
        "dont_ask": str(ModeName.DONT_ASK),
        "sealed": str(ModeName.SEALED),
        "default": str(ModeName.DEFAULT),
        "plan": str(ModeName.PLAN),
        "accept_edits": str(ModeName.ACCEPT_EDITS),
        "acceptedits": str(ModeName.ACCEPT_EDITS),
    }
    lowered = normalized.casefold().replace("-", "_")
    bypass_requested = lowered in {
        "bypass",
        "bypasspermissions",
        "dangerously_skip_permissions",
    } or constraints.get("permission_bypass_available") is True
    auto_requested = lowered == "auto" or constraints.get("permission_auto_available") is True
    if bypass_requested or auto_requested:
        return str(ModeName.DEFAULT), bypass_requested, auto_requested
    selected = aliases.get(lowered, normalized)
    try:
        mode = ModeName(selected)
    except ValueError:
        mode = ModeName.DEFAULT
    if mode in {ModeName.BYPASS, ModeName.AUTO}:
        return str(ModeName.DEFAULT), mode is ModeName.BYPASS, mode is ModeName.AUTO
    return str(mode), False, False


def _blocked_events(
    *,
    run_id: str,
    task_id: str,
    node_id: str | None,
    worker_request_id: str,
    session_id: str,
    action: BrowserActionPermissionInput,
    guard: PermissionGuardResult,
    cause_event_id: str,
) -> tuple[EventRecord, EventRecord]:
    blocked = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="browser_action_permission_blocked",
        effect=str(guard.effect),
        reason=guard.decision.reason,
        tool_use_id=guard.decision.tool_use_id,
        request_id=guard.decision.request_id,
        decision_id=guard.decision.decision_id,
        arguments_digest_value=guard.decision.arguments_digest,
        cause_event_id=cause_event_id,
        payload={
            "action": action.normalized_action,
            "backend": action.backend,
            "reason_code": guard.decision.reason_code,
            "ask_pending": guard.ask_pending,
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    recovery_payload = (
        guard.decision.recovery_input.to_dict()
        if guard.decision.recovery_input is not None
        else {
            "retryable": guard.effect is PermissionEffect.ASK,
            "alternatives": [
                {"kind": "permission_alternative", "instruction": str(item)}
                for item in guard.trace.recovery_alternatives
            ],
            "constraints": {
                "request_id": guard.decision.request_id,
                "exact_tool_use_id": guard.decision.tool_use_id,
                "arguments_digest": guard.decision.arguments_digest,
            },
        }
    )
    recovery = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="recovery_input",
        effect=str(guard.effect),
        reason="browser action was stopped before its side-effect boundary",
        tool_use_id=guard.decision.tool_use_id,
        request_id=guard.decision.request_id,
        decision_id=guard.decision.decision_id,
        arguments_digest_value=guard.decision.arguments_digest,
        cause_event_id=blocked.event_id,
        payload={
            **recovery_payload,
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    return blocked, recovery


def _rejected_events(
    *,
    run_id: str,
    task_id: str,
    node_id: str | None,
    worker_request_id: str,
    session_id: str,
    action: BrowserActionPermissionInput,
    decision: BrowserActionPermissionDecision,
    reason: str,
    cause_event_id: str,
) -> tuple[EventRecord, EventRecord]:
    rejected = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="browser_action_execution_grant_rejected",
        effect="deny",
        reason=reason,
        tool_use_id=decision.tool_use_id,
        request_id=decision.request_id,
        decision_id=decision.decision_id,
        arguments_digest_value=decision.guard.decision.arguments_digest,
        cause_event_id=cause_event_id,
        payload={
            "action": action.normalized_action,
            "backend": action.backend,
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    recovery = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="recovery_input",
        effect="deny",
        reason="re-evaluate the exact browser action; never replay this grant",
        tool_use_id=decision.tool_use_id,
        request_id=decision.request_id,
        decision_id=decision.decision_id,
        arguments_digest_value=decision.guard.decision.arguments_digest,
        cause_event_id=rejected.event_id,
        payload={
            "retryable": True,
            "alternatives": [
                "restore the exact session payload and request a fresh decision",
                "use a local read-only browser inspection",
            ],
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    return rejected, recovery


def _permission_event(
    *,
    run_id: str,
    task_id: str,
    node_id: str | None,
    session_id: str,
    worker_request_id: str,
    kind: str,
    effect: str,
    reason: str,
    tool_use_id: str = "",
    request_id: str = "",
    decision_id: str = "",
    arguments_digest_value: str = "",
    cause_event_id: str = "",
    payload: Mapping[str, Any] | None = None,
) -> EventRecord:
    return EventRecord(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "query_session": {
                "session_id": session_id,
                "worker_request_id": worker_request_id,
                "phase": kind,
                "permission_runtime": {
                    "schema": "zyra.browser-action-permission.event.v1",
                    "kind": kind,
                    "runtime_id": BROWSER_ACTION_GATE_RUNTIME_ID,
                    "owner_unit": BROWSER_ACTION_GATE_OWNER_UNIT,
                    "session_id": session_id,
                    "worker_request_id": worker_request_id,
                    "tool_call_id": tool_use_id,
                    "request_id": request_id,
                    "decision_id": decision_id,
                    "arguments_digest": arguments_digest_value,
                    "effect": effect,
                    "reason": reason,
                    "cause_event_id": cause_event_id,
                    "payload": _safe_event_payload(payload or {}),
                },
            }
        },
    )


def _sanitize_permission_event(event: EventRecord) -> EventRecord:
    return EventRecord(
        run_id=event.run_id,
        task_id=event.task_id,
        node_id=event.node_id,
        event_type=event.event_type,
        event_id=event.event_id,
        created_at=event.created_at,
        payload=_safe_event_payload(event.payload),
    )


_SENSITIVE_EVENT_KEYS = frozenset(
    {
        "arguments",
        "canonical_arguments",
        "raw_arguments",
        "authorization_token",
        "session_custody_token",
        "permission_session_custody_token",
        "token",
        "signature",
        "password",
        "credential",
        "cookie",
        "secret",
        "api_key",
    }
)


def _safe_event_payload(value: Any, *, key: str = "") -> Any:
    normalized = key.casefold().replace("-", "_")
    if normalized in _SENSITIVE_EVENT_KEYS or normalized.endswith("_custody_token"):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): _safe_event_payload(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_safe_event_payload(item, key=key) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _decision_event_id(events: Sequence[EventRecord]) -> str:
    for event in events:
        runtime = event.payload.get("query_session", {}).get("permission_runtime", {})
        if runtime.get("kind") == "permission_decision":
            return event.event_id
    return ""


def _last_event_id(events: Sequence[EventRecord]) -> str:
    return events[-1].event_id if events else ""


def _action_domain(action: BrowserActionPermissionInput) -> str:
    for value in (action.target_url, str(action.arguments.get("url") or ""), action.current_url):
        parsed = urlparse(value)
        if parsed.hostname:
            return parsed.hostname.casefold()
    return ""


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if name.casefold() in {"token", "session_custody_token", "permission_session_custody_token"}:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            output[name] = item
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray, memoryview)):
            output[name] = [str(child) for child in item]
    return output


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


__all__ = [
    "BROWSER_ACTION_GATE_OWNER_UNIT",
    "BROWSER_ACTION_GATE_RUNTIME_ID",
    "BrowserActionPermissionConsumption",
    "BrowserActionPermissionCustodyError",
    "BrowserActionPermissionDecision",
    "BrowserActionPermissionDisabled",
    "BrowserActionPermissionError",
    "BrowserActionPermissionGate",
    "BrowserActionPermissionIdentityError",
    "BrowserActionPermissionInput",
    "browser_permission_setup_failure_events",
]
