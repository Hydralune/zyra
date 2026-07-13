from __future__ import annotations

import dataclasses

import base64
import hashlib
import json
import mimetypes
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType
from zyra_runtime.permission import BrowserActionPermissionGate, BrowserActionPermissionInput

from .errors import (
    BrowserArtifactError,
    BrowserConnectionLost,
    BrowserPermissionDenied,
    BrowserRuntimeDisabled,
    BrowserSessionBusy,
    BrowserSessionNotFound,
    BrowserTargetError,
    classify_browser_error,
)
from .action_policy import BrowserActionAdmissionPolicy, BrowserActionPolicyConfig
from .artifact_pipeline import (
    BrowserArtifactPayload,
    BrowserArtifactPayloadKind,
    BrowserArtifactPipeline,
    BrowserArtifactPipelinePolicy,
)
from .integration_models import (
    BrowserActionExecution,
    BrowserActionName,
    BrowserActionReceipt,
    BrowserActionRequest,
    BrowserActionStatus,
    BrowserApplicationResult,
    BrowserArtifactHandoff,
    normalize_action_name,
    public_mapping,
    validate_plan,
)
from .models import BrowserArtifactKind, BrowserSessionRef, BrowserSessionStartResult, browser_now
from .screenshot_capture import HighlightFreeScreenshotCapture
from .lifecycle_transactions import BrowserLifecycleTransactionRuntime
from .runtime import BrowserRuntime
from .session_lease import BrowserSessionLeaseStore


class BrowserCanonicalIntegrationPorts(Protocol):
    def append_event(self, event: EventRecord) -> None: ...
    def append_artifact(self, artifact: ArtifactRef) -> None: ...


class BrowserApplicationArtifactStore(Protocol):
    def write_text(
        self,
        *,
        run_id: str,
        task_id: str,
        content: str,
        title: str,
        kind: ArtifactKind = ArtifactKind.TEXT,
        extension: str = ".txt",
        producer_node_id: str | None = None,
    ) -> ArtifactRef: ...

    def write_bytes(
        self,
        *,
        run_id: str,
        task_id: str,
        content: bytes,
        title: str,
        kind: ArtifactKind = ArtifactKind.FILE,
        extension: str = ".bin",
        producer_node_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...


class _SessionComponents:
    def __init__(self, browser_runtime: BrowserRuntime, session: BrowserSessionRef) -> None:
        runtime = getattr(browser_runtime, "_runtime", None)
        if runtime is None:
            raise BrowserRuntimeDisabled("BrowserRuntime does not expose its session runtime")
        persisted = browser_runtime.get_session(session.session_id)
        if persisted.run_id != session.run_id or persisted.task_id != session.task_id:
            raise BrowserSessionBusy("browser session identity changed before action execution", session_id=session.session_id)
        if persisted.status != "running":
            raise BrowserSessionBusy(
                f"browser session is not running: {persisted.status}",
                session_id=session.session_id,
            )
        self.session_runtime = runtime
        self.session = persisted
        self.cdp = getattr(runtime, "_cdp", {}).get(session.session_id)
        self.target = getattr(runtime, "_targets", {}).get(session.session_id)
        self.event_bus = getattr(runtime, "_event_buses", {}).get(session.session_id)
        self.profile_store = getattr(runtime, "profile_store", None)
        if self.cdp is None:
            raise BrowserConnectionLost("browser session has no active CDP runtime", session_id=session.session_id)
        if self.target is None:
            raise BrowserTargetError("browser session has no active target runtime", session_id=session.session_id)

    def active_target(self) -> Any:
        target = self.target.ensure_valid_focus()
        if not getattr(target, "target_id", ""):
            raise BrowserTargetError("browser session has no focused page target", session_id=self.session.session_id)
        return target

    def active_cdp_session_id(self, target: Any | None = None) -> str:
        selected = target or self.active_target()
        target_id = str(getattr(selected, "target_id", "") or "")
        for owner in (
            self.session_runtime,
            getattr(self.session_runtime, "source_mapper", None),
            getattr(self.session_runtime, "cdp_source_mapper", None),
            self.target,
        ):
            if owner is None:
                continue
            active = getattr(owner, "active_cdp_session", None)
            if callable(active):
                try:
                    value = active(timeout=min(5.0, self.session_runtime.config.connect_timeout_seconds))
                except Exception as error:
                    raise BrowserTargetError(
                        f"active target CDP attach timed out: {type(error).__name__}: {error}",
                        session_id=self.session.session_id,
                        code="browser_target_attach_timeout",
                    ) from error
                session_id = str(getattr(value, "cdp_session_id", "") or getattr(value, "session_id", "") or "")
                if session_id:
                    return session_id
            for name in (
                "active_cdp_session_id",
                "active_target_cdp_session_id",
                "cdp_session_id",
                "cdp_session_id_for_target",
                "cdp_session",
            ):
                method = getattr(owner, name, None)
                if not callable(method):
                    continue
                try:
                    try:
                        value = method(target_id)
                    except TypeError:
                        value = method()
                except Exception as error:
                    if "timeout" in str(error).casefold():
                        raise BrowserTargetError(
                            f"target CDP attach timed out: {error}",
                            session_id=self.session.session_id,
                            code="browser_target_attach_timeout",
                        ) from error
                    continue
                session_id = str(
                    getattr(value, "cdp_session_id", "")
                    or getattr(value, "session_id", "")
                    or (value if isinstance(value, str) else "")
                )
                if session_id:
                    return session_id
        session_ids = tuple(getattr(selected, "session_ids", ()) or ())
        if session_ids:
            return str(session_ids[0])
        raise BrowserTargetError(
            "active browser target has no attached CDP session route",
            session_id=self.session.session_id,
        )

    def profile(self) -> Any | None:
        if self.profile_store is None or not self.session.profile_id:
            return None
        return self.profile_store.get(self.session.profile_id)


@dataclass(frozen=True, slots=True)
class BrowserActionPlanExecution:
    ok: bool
    action_results: tuple[BrowserActionExecution, ...] = ()
    events: tuple[EventRecord, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    action_receipts: tuple[BrowserActionReceipt, ...] = ()
    outputs: tuple[Mapping[str, Any], ...] = ()
    error: str = ""
    summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SessionBoundActionRuntime:
    """Executes actions against the CDP/target/profile objects of one started session.

    It never constructs BrowserRuntime, BrowserSessionRuntime, CdpRequestRuntime,
    BrowserTargetRuntime or a transport. Absence of a live session component is
    a hard failure, preventing fallback to static/browser-use side paths.
    """

    def __init__(
        self,
        browser_runtime: BrowserRuntime,
        artifact_store: BrowserApplicationArtifactStore | None = None,
        *,
        receipt_store: BrowserSessionLeaseStore | None = None,
        canonical_ports: BrowserCanonicalIntegrationPorts | None = None,
        disabled: bool = False,
        max_screenshot_bytes: int = 64 * 1024 * 1024,
        max_download_bytes: int = 256 * 1024 * 1024,
        max_evaluate_code_chars: int = 256_000,
        artifact_pipeline: BrowserArtifactPipeline | None = None,
        action_policy: BrowserActionAdmissionPolicy | None = None,
    ) -> None:
        self.browser_runtime = browser_runtime
        self.artifact_store = artifact_store
        self.receipt_store = receipt_store
        self.canonical_ports = canonical_ports
        self.disabled = disabled
        self.max_screenshot_bytes = max(1024, int(max_screenshot_bytes))
        self.max_download_bytes = max(1024, int(max_download_bytes))
        self.max_evaluate_code_chars = max(1024, int(max_evaluate_code_chars))
        session_runtime = getattr(browser_runtime, "_runtime", None)
        artifact_bridge = getattr(session_runtime, "artifact_bridge", None)
        self.artifact_pipeline = artifact_pipeline or BrowserArtifactPipeline(
            artifact_store,
            artifact_bridge=artifact_bridge,
            handoff_store=receipt_store,
            policy=BrowserArtifactPipelinePolicy(
                max_screenshot_bytes=self.max_screenshot_bytes,
                max_download_bytes=self.max_download_bytes,
            ),
            disabled=disabled,
        )
        self.action_policy = action_policy or BrowserActionAdmissionPolicy(
            BrowserActionPolicyConfig(max_script_chars=self.max_evaluate_code_chars),
            disabled=disabled,
        )
        self._lock = threading.RLock()
        self._executions = 0
        self._failures = 0
        self._artifacts = 0
        self._last_error = ""

    def _execute_plan_compat(
        self,
        *,
        request: Any,
        session_start: BrowserSessionStartResult,
        plan: Sequence[Mapping[str, Any]],
        permission_gate: BrowserActionPermissionGate,
        **_ignored: Any,
    ) -> BrowserActionPlanExecution:
        issues = validate_plan(plan)
        if issues:
            return BrowserActionPlanExecution(
                ok=False,
                error="invalid_browser_plan",
                summary="Productized browser plan validation failed.",
                metadata={"validation_issues": len(issues)},
            )
        session = session_start.session
        if not session_start.ok or session.status != "running":
            return BrowserActionPlanExecution(
                ok=False,
                error="browser_session_not_running",
                summary="Productized browser actions require a running session.",
            )
        if session.run_id != str(getattr(request, "run_id", "")) or session.task_id != str(
            getattr(request, "task_id", "")
        ):
            return BrowserActionPlanExecution(
                ok=False,
                error="browser_session_identity_mismatch",
                summary="Browser action request does not own the started session.",
            )
        constraints = dict(getattr(request, "constraints", {}) or {})
        default_continue = bool(constraints.get("continue_on_error"))
        executions: list[BrowserActionExecution] = []
        events: list[EventRecord] = []
        artifacts: list[ArtifactRef] = []
        receipts: list[BrowserActionReceipt] = []
        outputs: list[Mapping[str, Any]] = []
        error = ""
        for index, item in enumerate(plan, start=1):
            try:
                action_request = BrowserActionRequest.from_plan_item(
                    item,
                    run_id=session.run_id,
                    task_id=session.task_id,
                    worker_request_id=str(getattr(request, "request_id", "")),
                    browser_session_id=session.session_id,
                    step_index=index,
                    node_id=str(getattr(request, "node_id", "") or ""),
                    default_continue_on_error=default_continue,
                )
                execution = self.execute(action_request, permission_gate=permission_gate)
            except Exception as exc:  # noqa: BLE001 - plan boundary returns typed failure.
                error = f"{type(exc).__name__}: {exc}"
                break
            executions.append(execution)
            events.extend(execution.events)
            artifacts.extend(execution.artifacts)
            receipts.append(execution.receipt)
            outputs.append(dict(execution.receipt.output))
            if not execution.ok:
                error = execution.receipt.error_code or "browser_action_failed"
                if not action_request.continue_on_error:
                    break
        ok = bool(executions) and len(executions) == len(plan) and all(item.ok for item in executions)
        return BrowserActionPlanExecution(
            ok=ok,
            action_results=tuple(executions),
            events=tuple(events),
            artifacts=tuple(artifacts),
            action_receipts=tuple(receipts),
            outputs=tuple(outputs),
            error="" if ok else error or "browser_action_failed",
            summary=(
                f"Executed {len(executions)} productized browser action(s)."
                if ok
                else "Productized browser action plan stopped on failure."
            ),
            metadata={
                "browser_backend": "zyra-browser-productized",
                "browser_session_id": session.session_id,
                "browser_canonical_session_id": session.canonical_session_id,
                "browser_action_execution_count": len(executions),
                "browser_action_artifact_count": len(artifacts),
            },
        )

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserRuntimeDisabled("session-bound browser action runtime is disabled")

    def execute(
        self,
        request: BrowserActionRequest,
        *,
        permission_gate: BrowserActionPermissionGate,
        lease: Any | None = None,
        lease_runtime: BrowserLifecycleTransactionRuntime | None = None,
    ) -> BrowserActionExecution:
        self._ensure_available()
        if lease is not None:
            if lease_runtime is None:
                raise BrowserSessionBusy("browser action lease runtime is required", session_id=request.browser_session_id)
            lease_runtime.assert_active(lease)
        session = self.browser_runtime.get_session(request.browser_session_id)
        session_runtime = getattr(self.browser_runtime, "_runtime", None)
        command = getattr(session_runtime, "_commands", {}).get(request.browser_session_id) if session_runtime else None
        profile_store = getattr(session_runtime, "profile_store", None) if session_runtime else None
        profile = profile_store.get(session.profile_id) if profile_store is not None and session.profile_id else None
        admission = self.action_policy.admit(
            request,
            session,
            workspace_root=getattr(command, "workspace_root", None),
            downloads_root=getattr(profile, "downloads_dir", None),
        )
        if admission.allowed:
            request = admission.apply(request)
        started_at = browser_now()
        if not admission.allowed:
            receipt = BrowserActionReceipt(
                request_id=request.request_id,
                request_fingerprint=request.fingerprint,
                browser_session_id=request.browser_session_id,
                action=request.action,
                step_index=request.step_index,
                status=BrowserActionStatus.FAILED,
                started_at=started_at,
                completed_at=browser_now(),
                error_code=admission.code or "browser_action_policy_denied",
                error_message=admission.reason,
                metadata={
                    "runtime_id": "zyra-session-bound-browser-actions",
                    "browser_action_admission": admission.to_dict(),
                    "permission_side_effect_count": 0,
                    "cdp_side_effect_count": 0,
                },
            )
            receipt = self._commit_receipt(request, receipt)
            event = self._action_event(request, receipt)
            self._commit_events((event,))
            return BrowserActionExecution(receipt=receipt, events=(event,))
        permission_input = self._permission_input(request)
        permission_decision = permission_gate.guard(permission_input)
        events: list[EventRecord] = list(permission_decision.events)
        if not permission_decision.allowed:
            permission_effect = str(permission_decision.effect).lower()
            pending_approval = permission_effect.endswith("ask")
            pending_request = permission_decision.guard.pending_request
            receipt = BrowserActionReceipt(
                request_id=request.request_id,
                request_fingerprint=request.fingerprint,
                browser_session_id=request.browser_session_id,
                action=request.action,
                step_index=request.step_index,
                status=BrowserActionStatus.PERMISSION_BLOCKED,
                started_at=started_at,
                completed_at=browser_now(),
                permission_metadata=permission_decision.metadata(),
                output={
                    "permission_decision": permission_decision.guard.decision.to_dict(),
                    "pending_request": pending_request.to_dict() if pending_request is not None else None,
                    "action_execution_count": 0,
                },
                error_code=(
                    "browser_action_permission_pending"
                    if pending_approval
                    else "browser_action_permission_denied"
                ),
                error_message=(
                    "browser action is pending exact approval"
                    if pending_approval
                    else "browser action was blocked before execution"
                ),
                metadata={"runtime_id": "zyra-session-bound-browser-actions"},
            )
            receipt = self._commit_receipt(request, receipt)
            action_event = self._action_event(request, receipt)
            events.append(action_event)
            self._commit_events(events)
            return BrowserActionExecution(receipt=receipt, events=tuple(events))
        consumption = permission_gate.consume(permission_decision, permission_input)
        events.extend(consumption.events)
        if not consumption.authorizes_execution:
            receipt = BrowserActionReceipt(
                request_id=request.request_id,
                request_fingerprint=request.fingerprint,
                browser_session_id=request.browser_session_id,
                action=request.action,
                step_index=request.step_index,
                status=BrowserActionStatus.PERMISSION_BLOCKED,
                started_at=started_at,
                completed_at=browser_now(),
                permission_metadata=consumption.metadata(),
                error_code="browser_action_execution_grant_rejected",
                error_message=consumption.reason,
                metadata={"runtime_id": "zyra-session-bound-browser-actions"},
            )
            receipt = self._commit_receipt(request, receipt)
            events.append(self._action_event(request, receipt))
            self._commit_events(events)
            return BrowserActionExecution(receipt=receipt, events=tuple(events))

        # A durable success may be replayed only after this request's permission
        # runtime has guarded and consumed its exact execution grant. This keeps
        # shared application state from bypassing current permission custody or
        # suppressing a first ASK/pending transition.
        if self.receipt_store is not None:
            cached = self.receipt_store.action_for_request(request.request_id, request.fingerprint)
            if cached is not None and cached.status is BrowserActionStatus.SUCCEEDED:
                cached_execution = self._cached_execution(request, cached)
                current_permission_events = tuple(events)
                self._commit_events(current_permission_events)
                return dataclasses.replace(
                    cached_execution,
                    events=current_permission_events + tuple(cached_execution.events),
                )

        artifacts: tuple[ArtifactRef, ...] = ()
        handoffs: tuple[BrowserArtifactHandoff, ...] = ()
        try:
            components = _SessionComponents(self.browser_runtime, self.browser_runtime.get_session(request.browser_session_id))
            output, artifacts, handoffs = self._dispatch(request, components)
            if lease is not None and lease_runtime is not None:
                lease_runtime.assert_active(lease)
            receipt = BrowserActionReceipt(
                request_id=request.request_id,
                request_fingerprint=request.fingerprint,
                browser_session_id=request.browser_session_id,
                action=request.action,
                step_index=request.step_index,
                status=BrowserActionStatus.SUCCEEDED,
                started_at=started_at,
                completed_at=browser_now(),
                output=output,
                artifact_handoffs=handoffs,
                permission_metadata=consumption.metadata(),
                metadata={
                    "runtime_id": "zyra-session-bound-browser-actions",
                    "browser_action_admission": admission.to_dict(),
                    "session_revision": components.session.revision,
                    "active_target_id": components.session.active_target_id,
                },
            )
        except Exception as error:
            failure = classify_browser_error(error, operation=str(request.action), session_id=request.browser_session_id)
            receipt = BrowserActionReceipt(
                request_id=request.request_id,
                request_fingerprint=request.fingerprint,
                browser_session_id=request.browser_session_id,
                action=request.action,
                step_index=request.step_index,
                status=BrowserActionStatus.FAILED,
                started_at=started_at,
                completed_at=browser_now(),
                permission_metadata=consumption.metadata(),
                error_code=failure.code,
                error_message=failure.message,
                metadata={
                    "runtime_id": "zyra-session-bound-browser-actions",
                    "retryable": failure.retryable,
                    "failure_kind": str(failure.kind),
                },
            )
            with self._lock:
                self._failures += 1
                self._last_error = f"{type(error).__name__}: {error}"
        receipt = self._commit_receipt(request, receipt)
        events.append(self._action_event(request, receipt))
        for artifact in artifacts:
            events.append(self._artifact_event(request, artifact, handoffs))
            self._commit_artifact(artifact)
        self._commit_events(events)
        with self._lock:
            self._executions += 1
            self._artifacts += len(artifacts)
            if receipt.ok:
                self._last_error = ""
        return BrowserActionExecution(receipt=receipt, events=tuple(events), artifacts=artifacts)

    def _cached_execution(
        self,
        request: BrowserActionRequest,
        receipt: BrowserActionReceipt,
    ) -> BrowserActionExecution:
        artifacts: list[ArtifactRef] = []
        for handoff in receipt.artifact_handoffs:
            try:
                kind = ArtifactKind(str(handoff.kind))
            except ValueError:
                kind = ArtifactKind.FILE
            artifacts.append(ArtifactRef(
                artifact_id=handoff.artifact_id,
                kind=kind,
                uri=handoff.uri,
                title=f"Recovered browser artifact {handoff.artifact_id}",
                producer_node_id=request.node_id or None,
                created_at=handoff.created_at,
                metadata={
                    "browser_session_id": handoff.browser_session_id,
                    "browser_action_request_id": handoff.action_request_id,
                    "sha256": handoff.sha256,
                    "size_bytes": handoff.size_bytes,
                    "media_type": handoff.media_type,
                    "idempotent_replay": True,
                },
            ))
        event = self._action_event(request, receipt)
        artifact_events = tuple(self._artifact_event(request, artifact, receipt.artifact_handoffs) for artifact in artifacts)
        return BrowserActionExecution(
            receipt=receipt,
            events=(event, *artifact_events),
            artifacts=tuple(artifacts),
        )

    def _permission_input(self, request: BrowserActionRequest) -> BrowserActionPermissionInput:
        target_url = str(request.arguments.get("url") or "")
        current_url = ""
        try:
            session = self.browser_runtime.get_session(request.browser_session_id)
            runtime = getattr(self.browser_runtime, "_runtime", None)
            target_runtime = getattr(runtime, "_targets", {}).get(request.browser_session_id) if runtime else None
            if target_runtime is not None:
                focused = target_runtime.ensure_valid_focus()
                current_url = str(getattr(focused, "url", "") or "")
        except Exception:
            current_url = ""
        required = {
            BrowserActionName.NAVIGATE: ("url",),
            BrowserActionName.EVALUATE_JS: ("code",),
            BrowserActionName.FOCUS_TARGET: ("target_id",),
        }.get(request.action, ())
        return BrowserActionPermissionInput(
            step_index=request.step_index,
            action=str(request.action),
            normalized_action=str(request.action),
            backend="zyra-browser-productized",
            arguments=request.arguments,
            source_action=str(request.action),
            source_model="M1-S04A-02-session-bound",
            required_arguments=required,
            current_url=current_url,
            target_url=target_url,
            explicit_tool_use_id=request.request_id,
            metadata={
                "browser_session_id": request.browser_session_id,
                "browser_action_fingerprint": request.fingerprint,
            },
        )

    def _dispatch(
        self,
        request: BrowserActionRequest,
        components: _SessionComponents,
    ) -> tuple[dict[str, Any], tuple[ArtifactRef, ...], tuple[BrowserArtifactHandoff, ...]]:
        handlers = {
            BrowserActionName.NAVIGATE: self._navigate,
            BrowserActionName.SCREENSHOT: self._screenshot,
            BrowserActionName.EVALUATE_JS: self._evaluate_js,
            BrowserActionName.COLLECT_DOWNLOADS: self._collect_downloads,
            BrowserActionName.CAPTURE_TRACE: self._capture_trace,
            BrowserActionName.LIST_TARGETS: self._list_targets,
            BrowserActionName.FOCUS_TARGET: self._focus_target,
        }
        return handlers[request.action](request, components)

    def _navigate(self, request: BrowserActionRequest, components: _SessionComponents):
        url = str(request.arguments.get("url") or "").strip()
        parsed = urlparse(url)
        remote = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        local_file = parsed.scheme == "file" and bool(parsed.path)
        if not remote and not local_file:
            raise ValueError("productized browser navigation requires an absolute http(s) or file URL")
        target = components.active_target()
        cdp_session_id = components.active_cdp_session_id(target)
        response = components.cdp.send(
            "Page.navigate",
            {"url": url},
            cdp_session_id=cdp_session_id,
            timeout_seconds=self._timeout(request.arguments, default=30.0),
        )
        wait_seconds = self._bounded_float(request.arguments.get("settle_seconds"), default=0.0, minimum=0.0, maximum=10.0)
        if wait_seconds:
            time.sleep(wait_seconds)
        return ({
            "url": public_mapping({"url": url})["url"],
            "target_id": target.target_id,
            "cdp_session_id": cdp_session_id,
            "frame_id": str(response.get("frameId") or ""),
            "loader_id": str(response.get("loaderId") or ""),
            "error_text": str(response.get("errorText") or ""),
        }, (), ())

    def _screenshot(self, request: BrowserActionRequest, components: _SessionComponents):
        target = components.active_target()
        cdp_session_id = components.active_cdp_session_id(target)
        image_format = str(request.arguments.get("format") or "png").casefold()
        if image_format not in {"png", "jpeg", "webp"}:
            raise ValueError("screenshot format must be png, jpeg, or webp")
        timeout = self._timeout(request.arguments, default=30.0)
        capture = HighlightFreeScreenshotCapture().capture(
            lambda method, params: components.cdp.send(
                method,
                params,
                cdp_session_id=cdp_session_id,
                timeout_seconds=timeout,
            ),
            image_format=image_format,
            capture_beyond_viewport=bool(request.arguments.get("full_page", False)),
            from_surface=True,
            quality=(
                self._bounded_int(request.arguments.get("quality"), default=90, minimum=1, maximum=100)
                if image_format in {"jpeg", "webp"}
                else None
            ),
            target_id=target.target_id,
            cdp_session_id=cdp_session_id,
        )
        content = capture.content
        if not content or len(content) > self.max_screenshot_bytes:
            raise BrowserArtifactError("CDP screenshot size is empty or exceeds the productized limit", session_id=request.browser_session_id)
        extension = ".jpg" if image_format == "jpeg" else f".{image_format}"
        media_type = "image/jpeg" if image_format == "jpeg" else f"image/{image_format}"
        artifact, handoff = self._write_artifact(
            request,
            content,
            title=f"Browser screenshot step {request.step_index}",
            core_kind=ArtifactKind.SCREENSHOT,
            browser_kind=BrowserArtifactKind.SCREENSHOT,
            extension=extension,
            media_type=media_type,
            metadata={
                "target_id": target.target_id,
                "format": image_format,
                "highlight_removed": capture.highlight_removed,
                "highlight_restored": capture.highlight_restored,
                "screenshot_capture_id": capture.capture_id,
                "screenshot_sha256": capture.sha256,
            },
        )
        return ({
            "artifact_id": artifact.artifact_id,
            "target_id": target.target_id,
            "size_bytes": len(content),
            "format": image_format,
            "highlight_removed": capture.highlight_removed,
            "highlight_restored": capture.highlight_restored,
            "capture_id": capture.capture_id,
        }, (artifact,), (handoff,))

    def _evaluate_js(self, request: BrowserActionRequest, components: _SessionComponents):
        code = str(request.arguments.get("code") or "")
        if len(code) > self.max_evaluate_code_chars:
            raise ValueError("evaluate_js code exceeds the productized size limit")
        components.active_target()
        cdp_session_id = components.active_cdp_session_id()
        response = components.cdp.send(
            "Runtime.evaluate",
            {
                "expression": code,
                "awaitPromise": bool(request.arguments.get("await_promise", True)),
                "returnByValue": bool(request.arguments.get("return_by_value", True)),
                "userGesture": bool(request.arguments.get("user_gesture", False)),
            },
            cdp_session_id=cdp_session_id,
            timeout_seconds=self._timeout(request.arguments, default=30.0),
        )
        exception = response.get("exceptionDetails")
        if isinstance(exception, Mapping):
            text = str(exception.get("text") or "JavaScript evaluation failed")
            raise RuntimeError(text)
        result = response.get("result")
        return ({"result": public_mapping(result if isinstance(result, Mapping) else {"value": result})}, (), ())

    def _collect_downloads(self, request: BrowserActionRequest, components: _SessionComponents):
        profile = components.profile()
        if profile is None:
            raise BrowserArtifactError("browser profile is unavailable for download collection", session_id=request.browser_session_id)
        downloads_root = Path(profile.downloads_dir).resolve()
        if not downloads_root.exists():
            raise BrowserArtifactError("browser downloads directory does not exist", session_id=request.browser_session_id)
        settle = self._bounded_float(request.arguments.get("settle_seconds"), default=0.0, minimum=0.0, maximum=10.0)
        if settle:
            time.sleep(settle)
        max_files = self._bounded_int(request.arguments.get("max_files"), default=32, minimum=1, maximum=256)
        artifacts: list[ArtifactRef] = []
        handoffs: list[BrowserArtifactHandoff] = []
        skipped: list[str] = []
        total = 0
        candidates = sorted((item for item in downloads_root.iterdir() if item.is_file()), key=lambda item: (item.stat().st_mtime_ns, item.name))
        for path in candidates:
            resolved = path.resolve()
            resolved.relative_to(downloads_root)
            if path.suffix.casefold() in {".crdownload", ".part", ".tmp"}:
                skipped.append(path.name)
                continue
            content = path.read_bytes()
            if not content:
                skipped.append(path.name)
                continue
            total += len(content)
            if total > self.max_download_bytes:
                raise BrowserArtifactError("download collection exceeds the productized byte limit", session_id=request.browser_session_id)
            pipeline_result = self.artifact_pipeline.ingest_download(
                source_path=path,
                downloads_root=downloads_root,
                run_id=request.run_id,
                task_id=request.task_id,
                browser_session_id=request.browser_session_id,
                action_request_id=request.request_id,
                node_id=request.node_id,
                title=f"Browser download: {path.name}",
                metadata={"source_name": path.name, "profile_id": profile.profile_id},
            )
            artifacts.append(pipeline_result.artifact)
            handoffs.append(pipeline_result.handoff)
            if len(artifacts) >= max_files:
                break
        return ({
            "download_count": len(artifacts),
            "download_bytes": total,
            "artifact_ids": [item.artifact_id for item in artifacts],
            "skipped_incomplete": skipped,
        }, tuple(artifacts), tuple(handoffs))

    def _capture_trace(self, request: BrowserActionRequest, components: _SessionComponents):
        target_snapshot = components.target.snapshot()
        cdp_snapshot = components.cdp.snapshot()
        event_snapshot = components.event_bus.snapshot() if components.event_bus is not None else None
        profile = components.profile()
        trace = {
            "schema": "zyra.browser-session-trace.v1",
            "captured_at": browser_now(),
            "request": request.to_dict(),
            "session": components.session.to_dict(),
            "target_runtime": target_snapshot.to_dict(),
            "cdp_runtime": cdp_snapshot.to_dict(),
            "event_bus": event_snapshot.to_dict() if event_snapshot is not None and hasattr(event_snapshot, "to_dict") else {},
            "profile": profile.to_dict() if profile is not None else {},
            "runtime_metadata": self.browser_runtime.metadata(),
        }
        pipeline_result = self.artifact_pipeline.commit_trace(
            run_id=request.run_id,
            task_id=request.task_id,
            browser_session_id=request.browser_session_id,
            action_request_id=request.request_id,
            title=f"Browser session trace step {request.step_index}",
            trace=trace,
            node_id=request.node_id,
            metadata={"trace_schema": trace["schema"]},
        )
        artifact, handoff = pipeline_result.artifact, pipeline_result.handoff
        return ({"artifact_id": artifact.artifact_id, "trace_schema": trace["schema"]}, (artifact,), (handoff,))

    def _list_targets(self, request: BrowserActionRequest, components: _SessionComponents):
        components.target.reconcile()
        snapshot = components.target.snapshot()
        return ({
            "active_target_id": snapshot.active_target_id,
            "targets": [item.to_dict() for item in snapshot.targets],
            "cdp_sessions": [item.to_dict() for item in snapshot.cdp_sessions],
            "generation": snapshot.generation,
        }, (), ())

    def _focus_target(self, request: BrowserActionRequest, components: _SessionComponents):
        target_id = str(request.arguments.get("target_id") or "")
        target = components.target.focus(target_id, activate=bool(request.arguments.get("activate", True)))
        return ({"target": target.to_dict(), "active_target_id": target.target_id}, (), ())

    def _write_artifact(
        self,
        request: BrowserActionRequest,
        content: bytes,
        *,
        title: str,
        core_kind: ArtifactKind,
        browser_kind: BrowserArtifactKind,
        extension: str,
        media_type: str,
        metadata: Mapping[str, Any],
    ) -> tuple[ArtifactRef, BrowserArtifactHandoff]:
        payload_kind = {
            BrowserArtifactKind.SCREENSHOT: BrowserArtifactPayloadKind.SCREENSHOT,
            BrowserArtifactKind.DOWNLOAD: BrowserArtifactPayloadKind.DOWNLOAD,
            BrowserArtifactKind.TRACE: BrowserArtifactPayloadKind.TRACE,
        }[browser_kind]
        result = self.artifact_pipeline.commit_bytes(BrowserArtifactPayload(
            kind=payload_kind,
            run_id=request.run_id,
            task_id=request.task_id,
            browser_session_id=request.browser_session_id,
            action_request_id=request.request_id,
            content=content,
            title=title,
            extension=extension,
            media_type=media_type,
            core_kind=core_kind,
            browser_kind=browser_kind,
            node_id=request.node_id,
            metadata={**public_mapping(metadata), "browser_action": str(request.action)},
        ))
        return result.artifact, result.handoff

    def _commit_receipt(self, request: BrowserActionRequest, receipt: BrowserActionReceipt) -> BrowserActionReceipt:
        if receipt.request_id != request.request_id or receipt.request_fingerprint != request.fingerprint:
            raise BrowserSessionBusy("browser action receipt identity mismatch", session_id=request.browser_session_id)
        if self.receipt_store is not None:
            return self.receipt_store.record_action(receipt)
        return receipt

    def _commit_events(self, events: Sequence[EventRecord]) -> None:
        if self.canonical_ports is None:
            return
        for event in events:
            method = getattr(self.canonical_ports, "append_event", None) or getattr(self.canonical_ports, "project_event", None)
            if not callable(method):
                raise TypeError("browser canonical ports cannot project EventRecord")
            method(event)

    def _commit_artifact(self, artifact: ArtifactRef) -> None:
        if self.canonical_ports is not None:
            method = getattr(self.canonical_ports, "append_artifact", None) or getattr(self.canonical_ports, "project_artifact", None)
            if not callable(method):
                raise TypeError("browser canonical ports cannot project ArtifactRef")
            method(artifact)

    @staticmethod
    def _action_event(request: BrowserActionRequest, receipt: BrowserActionReceipt) -> EventRecord:
        return EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id or None,
            event_type=EventType.BROWSER_CDP_REQUEST,
            payload={
                "browser_session_id": request.browser_session_id,
                "worker_request_id": request.worker_request_id,
                "browser_action": receipt.to_dict(),
            },
        )

    @staticmethod
    def _artifact_event(
        request: BrowserActionRequest,
        artifact: ArtifactRef,
        handoffs: Sequence[BrowserArtifactHandoff],
    ) -> EventRecord:
        handoff = next((item for item in handoffs if item.artifact_id == artifact.artifact_id), None)
        return EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id or None,
            event_type=EventType.ARTIFACT_WRITTEN,
            payload={
                "artifact_id": artifact.artifact_id,
                "kind": str(artifact.kind),
                "uri": artifact.uri,
                "browser_session_id": request.browser_session_id,
                "browser_action_request_id": request.request_id,
                "browser_artifact_handoff": handoff.to_dict() if handoff else {},
            },
        )

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _timeout(self, arguments: Mapping[str, Any], *, default: float) -> float:
        return self._bounded_float(arguments.get("timeout_seconds"), default=default, minimum=0.1, maximum=120.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runtime_id": "zyra-session-bound-browser-actions",
                "owner_unit": "M1-S04A-02",
                "disabled": self.disabled,
                "executions": self._executions,
                "failures": self._failures,
                "artifacts": self._artifacts,
                "last_error": self._last_error,
                "receipt_store": self.receipt_store.snapshot() if self.receipt_store is not None else {},
            }


class LeaseBoundBrowserSessionApplication:
    """Plan-level integration facade used by BrowserWorker after ensure_started."""

    def __init__(
        self,
        browser_runtime: BrowserRuntime,
        artifact_store: BrowserApplicationArtifactStore | None = None,
        *,
        receipt_store: BrowserSessionLeaseStore | None = None,
        canonical_ports: BrowserCanonicalIntegrationPorts | None = None,
        disabled: bool = False,
        lifecycle_transactions: BrowserLifecycleTransactionRuntime | None = None,
    ) -> None:
        self.browser_runtime = browser_runtime
        self.artifact_store = artifact_store
        self.receipt_store = receipt_store or BrowserSessionLeaseStore(
            Path(browser_runtime.config.state_root) / "integration"
        )
        self.canonical_ports = canonical_ports
        self.disabled = disabled
        self.lifecycle_transactions = lifecycle_transactions or BrowserLifecycleTransactionRuntime(
            browser_runtime,
            self.receipt_store,
            canonical_ports=canonical_ports,
            disabled=disabled,
        )
        self.actions = SessionBoundActionRuntime(
            browser_runtime,
            artifact_store,
            receipt_store=self.receipt_store,
            canonical_ports=canonical_ports,
            disabled=disabled,
        )

    def execute_plan(
        self,
        request: Any,
        session_start: BrowserSessionStartResult,
        plan: Sequence[Mapping[str, Any]],
        permission_gate: BrowserActionPermissionGate,
    ) -> BrowserApplicationResult:
        if self.disabled:
            raise BrowserRuntimeDisabled("browser session application is disabled")
        issues = validate_plan(plan)
        if issues:
            return BrowserApplicationResult(
                ok=False,
                events=(),
                artifacts=(),
                action_receipts=(),
                error="; ".join(issues),
                metadata={"browser_backend": "zyra-browser-productized", "plan_validation_failed": True},
            )
        session = session_start.session
        persisted = self.browser_runtime.get_session(session.session_id)
        if persisted.status != "running":
            raise BrowserSessionBusy("browser session application requires a running session", session_id=session.session_id)
        if persisted.run_id != str(getattr(request, "run_id", "")) or persisted.task_id != str(getattr(request, "task_id", "")):
            raise BrowserSessionBusy("BrowserWorker request does not own the started browser session", session_id=session.session_id)
        constraints = dict(getattr(request, "constraints", {}) or {})
        continue_default = constraints.get("continue_on_error") is True
        owner_id = f"browser-application:{getattr(request, 'request_id', '')}"
        lease = self.lifecycle_transactions.acquire_action_lease(
            browser_session_id=session.session_id,
            run_id=session.run_id,
            task_id=session.task_id,
            worker_request_id=str(getattr(request, "request_id", "")),
            owner_id=owner_id,
            ttl_seconds=self._plan_ttl(constraints, len(plan)),
            metadata={"node_id": str(getattr(request, "node_id", "") or ""), "plan_steps": len(plan)},
        )
        capsule = None
        resume_runtime = getattr(self.browser_runtime, "_browser_resume_runtime", None)
        if resume_runtime is not None:
            try:
                capsule = resume_runtime.capture(session.session_id)
            except Exception:
                self.lifecycle_transactions.release_action_lease(lease, reason="resume_capsule_capture_failed")
                raise
        events: list[EventRecord] = []
        artifacts: list[ArtifactRef] = []
        receipts: list[BrowserActionReceipt] = []
        error = ""
        release_reason = "completed"
        try:
            for index, item in enumerate(plan, start=1):
                lease = self.lifecycle_transactions.renew_action_lease(
                    lease,
                    ttl_seconds=self._plan_ttl(constraints, len(plan)),
                )
                self.lifecycle_transactions.assert_active(lease)
                action_request = BrowserActionRequest.from_plan_item(
                    item,
                    run_id=session.run_id,
                    task_id=session.task_id,
                    worker_request_id=str(getattr(request, "request_id", "")),
                    browser_session_id=session.session_id,
                    step_index=index,
                    node_id=str(getattr(request, "node_id", "") or ""),
                    default_continue_on_error=continue_default,
                )
                execution = self.actions.execute(
                    action_request,
                    permission_gate=permission_gate,
                    lease=lease,
                    lease_runtime=self.lifecycle_transactions,
                )
                self.lifecycle_transactions.assert_active(lease)
                events.extend(execution.events)
                artifacts.extend(execution.artifacts)
                receipts.append(execution.receipt)
                if not execution.ok and not action_request.continue_on_error:
                    error = execution.receipt.error_code or "browser_action_failed"
                    release_reason = "action_failed"
                    break
            ok = bool(receipts or not plan) and all(item.ok for item in receipts)
            stop_result: Any | None = None
            if not session.keep_alive:
                self.lifecycle_transactions.assert_active(lease)
                stop_result = self.browser_runtime.stop(
                    session.session_id,
                    reason="browser_application_plan_completed",
                )
                self.lifecycle_transactions.assert_active(lease)
            return BrowserApplicationResult(
                ok=ok,
                events=tuple(events),
                artifacts=tuple(artifacts),
                action_receipts=tuple(receipts),
                error=error,
                metadata={
                    "browser_backend": "zyra-browser-productized",
                    "browser_session_id": session.session_id,
                    "browser_session_revision": persisted.revision,
                    "browser_action_count": len(receipts),
                    "browser_artifact_count": len(artifacts),
                    "browser_logical_lease_id": lease.lease_id,
                    "browser_logical_lease_generation": lease.generation,
                    "browser_runtime_vendor_required": False,
                    "browser_session_stopped_in_lease": stop_result is not None,
                    "browser_resume_capsule_id": getattr(capsule, "capsule_id", ""),
                    "browser_resume_capsule_fingerprint": getattr(capsule, "fingerprint", ""),
                    **permission_gate.metadata(),
                },
            )
        except Exception:
            release_reason = "application_exception"
            raise
        finally:
            try:
                self.lifecycle_transactions.release_action_lease(lease, reason=release_reason)
            except Exception:
                self.receipt_store.revoke(session.session_id, reason="release_failed")

    @staticmethod
    def _plan_ttl(constraints: Mapping[str, Any], plan_steps: int) -> float:
        configured = constraints.get("browser_session_lease_ttl_seconds")
        try:
            value = float(configured)
        except (TypeError, ValueError):
            value = max(180.0, min(900.0, 60.0 + plan_steps * 60.0))
        return max(180.0, min(900.0, value))

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-session-application",
            "owner_unit": "M1-S04A-02",
            "disabled": self.disabled,
            "browser_runtime": self.browser_runtime.metadata(),
            "actions": self.actions.snapshot(),
            "integration_state": self.receipt_store.snapshot(),
            "lifecycle_transactions": self.lifecycle_transactions.snapshot(),
        }
