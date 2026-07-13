from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from zyra_integrations.browser_use import (
    BrowserEndpoint,
    BrowserEndpointDiscovery,
    BrowserEventBus,
    ChromeLaunchPlan,
    ChromeLaunchPolicy,
    ChromeProcessController,
    ChromeSandboxBypassRequired,
    WebSocketCdpTransport,
)

from .artifact_event_bridge import BrowserArtifactEventBridge, BrowserArtifactPort, BrowserCanonicalEventPort
from .cdp_runtime import CdpRequestRuntime, CdpTransportPort, MemoryCdpTransport
from .connection_policy import BrowserConnectionPolicy, BrowserConnectionPolicyRuntime
from .errors import (
    BrowserConnectionLost,
    BrowserLaunchFailed,
    BrowserRequestTimeout,
    BrowserRuntimeDisabled,
    BrowserSessionBusy,
    BrowserSessionNotFound,
    classify_browser_error,
)
from .health import BrowserHealthRuntime
from .models import (
    BrowserLifecycleEvent,
    BrowserPermissionRequest,
    BrowserRuntimeConfig,
    BrowserSessionCommand,
    BrowserSessionDiagnostic,
    BrowserSessionRef,
    BrowserSessionStartResult,
    BrowserSessionStatus,
    BrowserSessionStopResult,
    browser_id,
    browser_now,
)
from .permission_bridge import AllowLifecyclePermissionPort, BrowserPermissionPort
from .profile_store import BrowserProfilePolicy, BrowserProfileStore
from .recovery import BrowserRecoveryCoordinator, DisconnectKind, RecoveryAction
from .store import BrowserStatePort
from .task_supervisor import BrowserTaskSupervisor
from .target_runtime import BrowserTargetRuntime
from .worker_bridge import BrowserWorkerSessionBridge


class BrowserSessionRuntime:
    def __init__(
        self,
        config: BrowserRuntimeConfig,
        *,
        state_store: BrowserStatePort,
        permission_port: BrowserPermissionPort | None = None,
        artifact_port: BrowserArtifactPort | None = None,
        event_port: BrowserCanonicalEventPort | None = None,
        process_controller: ChromeProcessController | None = None,
        transport_factory: Callable[[str, Mapping[str, str]], CdpTransportPort] | None = None,
        disabled: bool = False,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.permission_port = permission_port or AllowLifecyclePermissionPort()
        self.process_controller = process_controller
        if self.process_controller is not None:
            configure_custody = getattr(self.process_controller, "configure_custody", None)
            if callable(configure_custody):
                configure_custody(config.state_root / "browser-process-custody")
        self.transport_factory = transport_factory
        self.disabled = disabled
        self.profile_store = BrowserProfileStore(
            config.runtime_root,
            config.state_root,
            state_store=state_store,
        )
        self.artifact_bridge = artifact_port if artifact_port is not None else BrowserArtifactEventBridge(
            config.artifact_root,
            state_store=state_store,
            canonical_event_port=event_port,
        )
        self.worker_bridge = BrowserWorkerSessionBridge()
        self.connection_policy = BrowserConnectionPolicyRuntime(config)
        self.task_supervisor = BrowserTaskSupervisor()
        self.recovery = BrowserRecoveryCoordinator(
            reconnect_delays=config.reconnect_delays,
            reconnect_timeout_seconds=config.connect_timeout_seconds,
            failure_threshold=max(1, config.max_reconnect_attempts),
        )
        self.health_runtime = BrowserHealthRuntime(
            state_root=config.state_root,
            runtime_root=config.runtime_root,
            artifact_root=config.artifact_root,
        )
        self._event_buses: dict[str, BrowserEventBus] = {}
        self._targets: dict[str, BrowserTargetRuntime] = {}
        self._cdp: dict[str, CdpRequestRuntime] = {}
        self._commands: dict[str, BrowserSessionCommand] = {}
        self._connection_policies: dict[str, BrowserConnectionPolicy] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._global_lock = threading.RLock()

    def _ensure_available(self) -> None:
        if self.disabled or self.config.disabled:
            raise BrowserRuntimeDisabled("browser session runtime is disabled")

    def _session_lock(self, session_id: str) -> threading.RLock:
        with self._global_lock:
            return self._locks.setdefault(session_id, threading.RLock())

    def start(self, command: BrowserSessionCommand) -> BrowserSessionStartResult:
        self._ensure_available()
        existing_result = self.state_store.request_result(command.worker_request_id, command.request_fingerprint)
        if existing_result:
            existing = self.state_store.find_session(
                run_id=command.run_id,
                task_id=command.task_id,
                canonical_session_id=command.canonical_session_id,
            )
            if existing is not None and existing.status == str(BrowserSessionStatus.RUNNING) and self._running_session_is_live(existing):
                return self._result(existing, created=False, reused=True, reconnected=False)
        existing = self.state_store.find_session(
            run_id=command.run_id,
            task_id=command.task_id,
            canonical_session_id=command.canonical_session_id,
        )
        if existing and existing.status == "running":
            if self._running_session_is_live(existing):
                self.worker_bridge.attach(command, existing)
                result = self._result(existing, created=False, reused=True, reconnected=False)
                self.state_store.record_request(command.worker_request_id, command.request_fingerprint, result.to_dict())
                return result
            stale = replace(
                existing,
                status=str(BrowserSessionStatus.FAILED),
                revision=existing.revision + 1,
                updated_at=browser_now(),
            )
            existing = self.state_store.update_session(stale, expected_revision=existing.revision)
        elif existing and existing.status in {"connecting", "starting", "preparing", "reconnecting", "stopping"}:
            raise BrowserSessionBusy(
                f"browser session is already in lifecycle transaction {existing.status}",
                session_id=existing.session_id,
            )
        if existing and existing.status in {"stopped", "failed"}:
            return self._start_existing(command, existing, reconnected=True)
        session_id = command.browser_session_id or browser_id("brsession")
        now = browser_now()
        session = BrowserSessionRef(
            session_id=session_id,
            run_id=command.run_id,
            task_id=command.task_id,
            canonical_session_id=command.canonical_session_id,
            worker_request_id=command.worker_request_id,
            status=str(BrowserSessionStatus.CREATED),
            revision=0,
            profile_id="",
            endpoint_url=command.endpoint_url,
            keep_alive=command.keep_alive,
            created_at=now,
            updated_at=now,
        )
        session = self.state_store.create_session(session, request_fingerprint=command.request_fingerprint)
        return self._start_existing(command, session, reconnected=False, created=True)

    def ensure_started(self, command: BrowserSessionCommand) -> BrowserSessionStartResult:
        existing = self.state_store.find_session(
            run_id=command.run_id,
            task_id=command.task_id,
            canonical_session_id=command.canonical_session_id,
        )
        if existing and existing.status == str(BrowserSessionStatus.RUNNING):
            if self._running_session_is_live(existing):
                self.worker_bridge.attach(command, existing)
                return self._result(existing, created=False, reused=True, reconnected=False)
            stale = replace(
                existing,
                status=str(BrowserSessionStatus.FAILED),
                revision=existing.revision + 1,
                updated_at=browser_now(),
            )
            self.state_store.update_session(stale, expected_revision=existing.revision)
        return self.start(command)

    def _running_session_is_live(self, session: BrowserSessionRef) -> bool:
        cdp = self._cdp.get(session.session_id)
        targets = self._targets.get(session.session_id)
        event_bus = self._event_buses.get(session.session_id)
        if cdp is None or targets is None or event_bus is None:
            return False
        cdp_snapshot = cdp.snapshot()
        target_snapshot = targets.snapshot()
        bus_snapshot = event_bus.snapshot()
        return (
            str(cdp_snapshot.status) == "open"
            and bool(target_snapshot.active_target_id)
            and str(bus_snapshot.state) == "running"
        )

    def _start_existing(
        self,
        command: BrowserSessionCommand,
        session: BrowserSessionRef,
        *,
        reconnected: bool,
        created: bool = False,
    ) -> BrowserSessionStartResult:
        lock = self._session_lock(session.session_id)
        if not lock.acquire(timeout=self.config.connect_timeout_seconds):
            raise BrowserSessionBusy("browser session start transaction is busy", session_id=session.session_id)
        events: list[Mapping[str, Any]] = []
        try:
            current = self.state_store.require_session(session.session_id) if hasattr(self.state_store, "require_session") else session
            if current.status == str(BrowserSessionStatus.RUNNING):
                return self._result(current, created=False, reused=True, reconnected=reconnected)
            preparing = self._transition(current, BrowserSessionStatus.PREPARING)
            self._authorize(command, preparing, "session_start")
            effective_command = self._effective_command_for_recovery(command, current)
            if not effective_command.browser_session_id:
                effective_command = replace(effective_command, browser_session_id=current.session_id)
            active_owned_profile = bool(
                current.process_id is not None
                and effective_command.endpoint_url == current.endpoint_url
                and self._process_owned(current.process_id)
            )
            profile_result = self.profile_store.prepare(
                effective_command,
                preparing.session_id,
                policy=BrowserProfilePolicy(),
                active_owned_profile=active_owned_profile,
            )
            connection_policy = self.connection_policy.normalize(
                effective_command,
                profile_root=profile_result.profile.root,
                downloads_dir=profile_result.profile.downloads_dir,
                base_arguments=profile_result.chrome_args,
                runtime_environment=self.profile_store.runtime_environment(profile_result.profile.profile_id),
            )
            self._connection_policies[preparing.session_id] = connection_policy
            task_generation = self.task_supervisor.begin_session(preparing.session_id)
            preparing = replace(
                preparing,
                profile_id=profile_result.profile.profile_id,
                revision=preparing.revision + 1,
                updated_at=browser_now(),
            )
            preparing = self.state_store.update_session(preparing, expected_revision=preparing.revision - 1)
            starting = self._transition(preparing, BrowserSessionStatus.STARTING)
            endpoint_url, process_id = self.task_supervisor.run_guarded(
                preparing.session_id,
                "launch_or_attach",
                self._launch_or_attach,
                effective_command,
                connection_policy,
                generation=task_generation,
                timeout=connection_policy.timeouts.connect_seconds,
                metadata={"connection_fingerprint": connection_policy.fingerprint},
            )
            starting = replace(
                starting,
                endpoint_url=endpoint_url,
                process_id=(
                    current.process_id
                    if process_id is None
                    and current.process_id is not None
                    and self._process_alive(current.process_id)
                    else process_id
                ),
                revision=starting.revision + 1,
                updated_at=browser_now(),
            )
            starting = self.state_store.update_session(starting, expected_revision=starting.revision - 1)
            connecting = self._transition(starting, BrowserSessionStatus.CONNECTING)
            self._release_runtime_resources(connecting.session_id, reason="session_rebuild")
            event_bus = self._event_buses.setdefault(connecting.session_id, BrowserEventBus())
            generation = event_bus.start()
            endpoint = BrowserEndpoint(
                http_url=endpoint_url,
                websocket_url=connection_policy.endpoint_websocket_url,
                headers=self._headers_from_policy(connection_policy),
                proxy_url=connection_policy.proxy.transport_url if connection_policy.proxy else "",
                verify_tls=connection_policy.verify_tls,
            )
            discovery_factory = lambda: BrowserEndpointDiscovery(endpoint, timeout_seconds=self.config.connect_timeout_seconds)
            discovery = discovery_factory().discover()
            target_runtime = BrowserTargetRuntime(connecting.session_id, event_bus, discovery_factory)
            self._targets[connecting.session_id] = target_runtime
            target_runtime.start()
            transport_factory = lambda: WebSocketCdpTransport(
                discovery.websocket_url,
                headers=connection_policy.headers.transport,
                proxy_url=connection_policy.proxy.transport_url if connection_policy.proxy else "",
                verify_tls=connection_policy.verify_tls,
                connect_timeout=connection_policy.timeouts.connect_seconds,
                read_timeout=max(connection_policy.timeouts.request_seconds, 0.25),
            )
            if str(command.constraints.get("browser_transport") or "").casefold() == "memory":
                transport_factory = MemoryCdpTransport
            elif self.transport_factory is not None:
                transport_factory = lambda: self.transport_factory(
                    discovery.websocket_url,
                    connection_policy.headers.transport,
                )
            cdp_runtime = CdpRequestRuntime(
                connecting.session_id,
                self.config.request_timeout_seconds,
                event_bus,
                transport_factory=transport_factory,
            )
            self._cdp[connecting.session_id] = cdp_runtime
            target_runtime.bind_cdp_events(cdp_runtime)
            cdp_runtime.connect()
            try:
                self._bootstrap_target_sessions(
                    cdp_runtime,
                    target_runtime,
                    memory_transport=str(effective_command.constraints.get("browser_transport") or "").casefold() == "memory",
                )
            except BrowserRequestTimeout as error:
                if (
                    os.name == "nt"
                    and connection_policy.launch.headless
                    and effective_command.constraints.get("browser_allow_unsafe_sandbox_bypass") is not True
                    and error.operation == "Page.enable"
                ):
                    raise BrowserLaunchFailed(
                        "Windows Chrome page sandbox did not initialize; explicitly set "
                        "browser_allow_unsafe_sandbox_bypass=true only for a trusted isolated lane",
                        session_id=connecting.session_id,
                        code="browser_unsafe_sandbox_bypass_required",
                        terminal=True,
                        details={
                            "required_constraint": "browser_allow_unsafe_sandbox_bypass=true",
                            "unsafe": True,
                            "platform": "windows",
                        },
                    ) from error
                raise
            active_target = target_runtime.ensure_valid_focus()
            active_cdp_session = target_runtime.active_cdp_session(timeout=self.config.connect_timeout_seconds)
            running = replace(
                connecting,
                status=str(BrowserSessionStatus.RUNNING),
                active_target_id=active_target.target_id,
                revision=connecting.revision + 1,
                updated_at=browser_now(),
            )
            running = self.state_store.update_session(running, expected_revision=connecting.revision)
            self._commands[running.session_id] = command
            self.worker_bridge.attach(command, running)
            events.append(self._emit(command, running, "browser.session.started", {
                "event_bus_generation": generation,
                "cdp_session_id": active_cdp_session.cdp_session_id,
            }))
            result = self._result(running, created=created, reused=False, reconnected=reconnected, events=events)
            self.state_store.record_request(command.worker_request_id, command.request_fingerprint, result.to_dict())
            return result
        except Exception as error:
            failure = classify_browser_error(error, operation="start", session_id=session.session_id)
            current = self.state_store.get_session(session.session_id)
            if current is not None and current.status != str(BrowserSessionStatus.FAILED):
                try:
                    failed = replace(current, status=str(BrowserSessionStatus.FAILED), revision=current.revision + 1, updated_at=browser_now())
                    self.state_store.update_session(failed, expected_revision=current.revision)
                    self._emit(command, failed, "browser.session.failed", {"failure": failure.to_dict()})
                except Exception:
                    pass
            self._release_runtime_resources(session.session_id, reason="session_start_failed")
            failed_session = self.state_store.get_session(session.session_id)
            if failed_session is not None and failed_session.process_id is not None and not failed_session.keep_alive:
                self._stop_process(failed_session.process_id, force=True)
            raise
        finally:
            lock.release()

    def stop(self, session_id: str, *, force: bool = False, reason: str = "requested") -> BrowserSessionStopResult:
        self._ensure_available()
        session = self.state_store.get_session(session_id)
        if session is None:
            raise BrowserSessionNotFound(f"browser session {session_id} does not exist", session_id=session_id)
        if session.status == str(BrowserSessionStatus.STOPPED):
            return BrowserSessionStopResult(ok=True, already_stopped=True, session=session)
        command = self._commands.get(session_id)
        if command is None:
            command = BrowserSessionCommand(
                run_id=session.run_id,
                task_id=session.task_id,
                worker_request_id=session.worker_request_id,
                canonical_session_id=session.canonical_session_id,
                browser_session_id=session.session_id,
                workspace_root=self.config.runtime_root,
                artifact_root=self.config.artifact_root,
                endpoint_url=session.endpoint_url,
                keep_alive=session.keep_alive,
            )
        self._authorize(command, session, "session_stop")
        stopping = self._transition(session, BrowserSessionStatus.STOPPING)
        events: list[Mapping[str, Any]] = []
        self.task_supervisor.cancel_session(
            session_id,
            reason=f"browser_session_stop:{reason}",
            advance_generation=True,
        )
        self.task_supervisor.drain(session_id, timeout=self.config.connect_timeout_seconds)
        event_bus = self._event_buses.get(session_id)
        generation = event_bus.snapshot().generation if event_bus is not None else 0
        self._release_runtime_resources(session_id, reason=reason)
        if stopping.process_id is not None and self.process_controller is not None and (force or not stopping.keep_alive):
            self._stop_process(stopping.process_id, force=force)
        stopped = replace(
            stopping,
            status=str(BrowserSessionStatus.STOPPED),
            active_target_id="",
            process_id=(stopping.process_id if stopping.keep_alive and not force else None),
            revision=stopping.revision + 1,
            updated_at=browser_now(),
        )
        stopped = self.state_store.update_session(stopped, expected_revision=stopping.revision)
        events.append(self._emit(command, stopped, "browser.session.stopped", {"reason": reason, "force": force, "event_bus_generation": generation}))
        self.worker_bridge.detach(command.worker_request_id)
        self._connection_policies.pop(session_id, None)
        self.recovery.reset(session_id)
        return BrowserSessionStopResult(ok=True, already_stopped=False, session=stopped, events=tuple(events))

    def reconnect(self, session_id: str) -> BrowserSessionStartResult:
        session = self.state_store.get_session(session_id)
        if session is None:
            raise BrowserSessionNotFound(f"browser session {session_id} does not exist", session_id=session_id)
        command = self._commands.get(session_id)
        if command is None:
            command = self._command_from_session(session)
        reconnecting = self._transition(session, BrowserSessionStatus.RECONNECTING)
        last_error: BaseException | None = None
        observation = self.recovery.classify(
            BrowserConnectionLost("browser session reconnect requested", session_id=session_id),
            session_id=session_id,
            operation="reconnect",
            target_id=session.active_target_id,
            generation=session.revision,
        )
        recovery_plan = self.recovery.plan(observation)
        if not self.recovery.allow(session_id):
            raise BrowserConnectionLost("browser reconnect circuit is open", session_id=session_id)
        reconnect_steps = tuple(
            step for step in recovery_plan.steps
            if step.action == RecoveryAction.RECONNECT_TRANSPORT
        )
        delays = tuple(step.delay_seconds for step in reconnect_steps)
        for attempt in range(1, self.config.max_reconnect_attempts + 1):
            self._emit(
                command,
                reconnecting,
                "browser.session.reconnecting",
                {"attempt": attempt, "recovery_plan_id": recovery_plan.plan_id},
            )
            try:
                cdp = self._cdp.get(session_id)
                target_runtime = self._targets.get(session_id)
                if cdp is None or target_runtime is None:
                    raise BrowserConnectionLost("browser session runtime custody must be rebuilt", session_id=session_id)
                target_runtime.begin_cdp_generation()
                cdp.reconnect()
                self._bootstrap_target_sessions(
                    cdp,
                    target_runtime,
                    memory_transport=str(command.constraints.get("browser_transport") or "").casefold() == "memory",
                )
                target = target_runtime.ensure_valid_focus()
                active_cdp_session = target_runtime.active_cdp_session(timeout=self.config.connect_timeout_seconds)
                running = replace(
                    reconnecting,
                    status=str(BrowserSessionStatus.RUNNING),
                    active_target_id=target.target_id if target else reconnecting.active_target_id,
                    revision=reconnecting.revision + 1,
                    updated_at=browser_now(),
                )
                running = self.state_store.update_session(running, expected_revision=reconnecting.revision)
                self.recovery.record_success(session_id)
                event = self._emit(
                    command,
                    running,
                    "browser.session.reconnected",
                    {
                        "attempt": attempt,
                        "recovery_plan_id": recovery_plan.plan_id,
                        "cdp_session_id": active_cdp_session.cdp_session_id,
                    },
                )
                return self._result(running, created=False, reused=False, reconnected=True, events=[event])
            except Exception as error:
                last_error = error
                self.recovery.record_failure(session_id, f"{type(error).__name__}: {error}")
                if attempt <= len(delays):
                    time.sleep(delays[attempt - 1])
        self._release_runtime_resources(session_id, reason="reconnect_rebuild")
        failed = self._transition(reconnecting, BrowserSessionStatus.FAILED)
        return self._start_existing(command, failed, reconnected=True)

    def target_runtime(self, session_id: str) -> BrowserTargetRuntime:
        target_runtime = self._targets.get(session_id)
        if target_runtime is None:
            raise BrowserConnectionLost("browser target runtime is unavailable", session_id=session_id)
        return target_runtime

    def cdp_runtime(self, session_id: str) -> CdpRequestRuntime:
        cdp_runtime = self._cdp.get(session_id)
        if cdp_runtime is None:
            raise BrowserConnectionLost("browser CDP runtime is unavailable", session_id=session_id)
        return cdp_runtime

    def active_cdp_session_id(self, session_id: str, *, timeout: float | None = None) -> str:
        return self.target_runtime(session_id).cdp_session_id(
            timeout=timeout or self.config.connect_timeout_seconds,
        )

    def target_cdp_session_id(
        self,
        session_id: str,
        target_id: str,
        *,
        timeout: float | None = None,
    ) -> str:
        return self.target_runtime(session_id).cdp_session_id(
            target_id,
            timeout=timeout or self.config.connect_timeout_seconds,
        )

    def diagnose(self, session_id: str) -> BrowserSessionDiagnostic:
        session = self.state_store.get_session(session_id)
        if session is None:
            raise BrowserSessionNotFound(f"browser session {session_id} does not exist", session_id=session_id)
        target = self._targets.get(session_id)
        cdp = self._cdp.get(session_id)
        event_bus = self._event_buses.get(session_id)
        profile = self.profile_store.get(session.profile_id) if session.profile_id else None
        profile_health = self.profile_store.health(profile) if profile is not None else None
        target_snapshot = target.snapshot() if target is not None else None
        cdp_snapshot = cdp.snapshot() if cdp is not None else None
        process_alive = self._process_alive(session.process_id)
        report = self.health_runtime.session(
            session,
            process_alive=process_alive,
            cdp_connected=bool(cdp_snapshot and str(cdp_snapshot.status) == "open"),
            target_count=len(target_snapshot.targets) if target_snapshot else 0,
            event_bus_generation=event_bus.snapshot().generation if event_bus else 0,
            profile_healthy=bool(profile_health and profile_health.healthy),
            metadata={
                "target_runtime": target_snapshot.to_dict() if target_snapshot else {},
                "cdp_runtime": cdp_snapshot.to_dict() if cdp_snapshot else {},
                "profile": profile_health.to_dict() if profile_health else {},
            },
        )
        assert report.diagnostic is not None
        return report.diagnostic

    def list_sessions(self, *, task_id: str = "") -> tuple[BrowserSessionRef, ...]:
        return self.state_store.list_sessions(task_id=task_id)

    def _transition(self, session: BrowserSessionRef, status: BrowserSessionStatus) -> BrowserSessionRef:
        allowed = {
            "created": {"preparing", "failed"},
            "preparing": {"starting", "failed"},
            "starting": {"connecting", "failed"},
            "connecting": {"running", "failed", "reconnecting"},
            "running": {"stopping", "reconnecting", "failed"},
            "reconnecting": {"running", "stopping", "failed"},
            "stopping": {"stopped", "failed"},
            "stopped": {"preparing", "stopped"},
            "failed": {"preparing", "stopping", "failed"},
        }
        target = str(status)
        if target not in allowed.get(session.status, set()):
            raise BrowserSessionBusy(f"invalid browser lifecycle transition {session.status} -> {target}", session_id=session.session_id)
        updated = replace(session, status=target, revision=session.revision + 1, updated_at=browser_now())
        return self.state_store.update_session(updated, expected_revision=session.revision)

    def _authorize(self, command: BrowserSessionCommand, session: BrowserSessionRef, action: str) -> None:
        request = BrowserPermissionRequest(
            session_id=session.session_id,
            action=action,
            arguments={"endpoint_url": bool(command.endpoint_url), "keep_alive": command.keep_alive},
            run_id=command.run_id,
            task_id=command.task_id,
            worker_request_id=command.worker_request_id,
            target_id=session.active_target_id,
            url=command.endpoint_url,
        )
        decision = self.permission_port.evaluate(request)
        if str(decision.effect) != "allow":
            raise BrowserLaunchFailed(f"browser lifecycle permission denied: {decision.reason}", session_id=session.session_id)

    def _launch_or_attach(
        self,
        command: BrowserSessionCommand,
        policy: BrowserConnectionPolicy,
    ) -> tuple[str, int | None]:
        if policy.endpoint_url:
            if self.process_controller is not None and hasattr(self.process_controller, "attach"):
                handle = self.process_controller.attach(
                    policy.endpoint_http_url,
                    headers=policy.headers.transport,
                    proxy_url=policy.proxy.transport_url if policy.proxy else "",
                    verify_tls=policy.verify_tls,
                    readiness_timeout_seconds=policy.timeouts.connect_seconds,
                )
                return handle.endpoint.base_url, None
            return policy.endpoint_http_url, None
        if self.process_controller is None:
            raise BrowserLaunchFailed("local browser process controller is not configured")
        executable = policy.launch.executable
        if executable is None:
            if hasattr(self.process_controller, "discover_executable"):
                executable = self.process_controller.discover_executable()
        if executable is None:
            raise BrowserLaunchFailed("browser executable could not be discovered")
        if not hasattr(self.process_controller, "launch"):
            raise BrowserLaunchFailed("browser process controller does not support launch")
        controller_owned_switches = (
            "--remote-debugging-address",
            "--remote-debugging-port",
            "--user-data-dir",
        )
        unsafe_sandbox_switches = ("--no-sandbox", "--disable-gpu-sandbox")
        allow_unsafe_sandbox_bypass = command.constraints.get("browser_allow_unsafe_sandbox_bypass") is True
        requested_unsafe_switches = tuple(
            argument
            for argument in policy.launch.arguments
            if any(argument == switch or argument.startswith(f"{switch}=") for switch in unsafe_sandbox_switches)
        )
        if requested_unsafe_switches and not allow_unsafe_sandbox_bypass:
            raise BrowserLaunchFailed(
                "unsafe Chrome sandbox switches require browser_allow_unsafe_sandbox_bypass=true",
                code="browser_unsafe_sandbox_bypass_not_authorized",
                terminal=True,
                details={"switches": list(requested_unsafe_switches)},
            )
        extra_switches = tuple(
            argument
            for argument in policy.launch.arguments
            if not any(argument == prefix or argument.startswith(f"{prefix}=") for prefix in controller_owned_switches)
            and not any(argument == switch or argument.startswith(f"{switch}=") for switch in unsafe_sandbox_switches)
        )
        launch_plan = ChromeLaunchPlan(
            executable=executable,
            user_data_dir=policy.launch.user_data_dir,
            downloads_dir=policy.launch.downloads_dir,
            runtime_dir=policy.launch.user_data_dir.parent / "process",
            policy=ChromeLaunchPolicy(
                headless=policy.launch.headless,
                keep_alive=command.keep_alive,
                startup_timeout_seconds=policy.timeouts.connect_seconds,
                shutdown_timeout_seconds=policy.timeouts.shutdown_seconds,
                allowed_extra_switches=(),
                allow_unsafe_sandbox_bypass=allow_unsafe_sandbox_bypass,
            ),
            extra_switches=extra_switches,
            environment=dict(policy.launch.environment),
            session_id=command.browser_session_id,
        )
        try:
            result = self.process_controller.launch(launch_plan)
        except ChromeSandboxBypassRequired as error:
            raise BrowserLaunchFailed(
                str(error),
                session_id=command.browser_session_id,
                code=error.code,
                terminal=True,
                details={
                    "required_constraint": "browser_allow_unsafe_sandbox_bypass=true",
                    "unsafe": True,
                    "platform": "windows",
                },
            ) from error
        endpoint = result.endpoint.base_url
        process_id = result.pid
        if not endpoint:
            raise BrowserLaunchFailed("browser process did not expose a CDP endpoint")
        return endpoint, int(process_id) if process_id is not None else None

    def _bootstrap_target_sessions(
        self,
        cdp_runtime: CdpRequestRuntime,
        target_runtime: BrowserTargetRuntime,
        *,
        memory_transport: bool,
    ) -> None:
        if memory_transport:
            target_runtime.reconcile()
            for target in target_runtime.page_targets():
                try:
                    target_runtime.cdp_session(target.target_id)
                except Exception:
                    target_runtime.attach_target(target.target_id, f"memory:{target.target_id}")
            return
        cdp_runtime.set_auto_attach(flatten=True, wait_for_debugger=False)
        discovered = cdp_runtime.discover_targets()
        for target_info in tuple(discovered.get("targetInfos") or ()):
            if isinstance(target_info, Mapping):
                target_runtime.upsert_target_info(target_info)
        target_runtime.reconcile()
        for target in target_runtime.page_targets():
            try:
                cdp_session = target_runtime.cdp_session(target.target_id)
            except Exception:
                attached = cdp_runtime.send(
                    "Target.attachToTarget",
                    {"targetId": target.target_id, "flatten": True},
                )
                cdp_session_id = str(attached.get("sessionId") or "")
                if not cdp_session_id:
                    raise BrowserConnectionLost(
                        f"CDP did not return a flattened session for target {target.target_id}",
                        session_id=target_runtime.session_id,
                    )
                target_runtime.attach_target(
                    target.target_id,
                    cdp_session_id,
                    target_type=target.target_type,
                    url=target.url,
                    title=target.title,
                )
                cdp_session = target_runtime.cdp_session(target.target_id)
            cdp_runtime.enable_page_runtime(cdp_session_id=cdp_session.cdp_session_id)

    def _effective_command_for_recovery(
        self,
        command: BrowserSessionCommand,
        session: BrowserSessionRef,
    ) -> BrowserSessionCommand:
        if command.endpoint_url or not session.endpoint_url or session.process_id is None:
            return command
        if self._endpoint_reachable(session.endpoint_url, command):
            self._adopt_persisted_process(command, session)
            return _endpoint_reattach_command(command, session.endpoint_url)
        if self._process_alive(session.process_id):
            self._stop_process(session.process_id, force=True)
        return command

    def _adopt_persisted_process(self, command: BrowserSessionCommand, session: BrowserSessionRef) -> None:
        if self.process_controller is None or session.process_id is None:
            raise BrowserLaunchFailed(
                "persisted local Chrome process cannot be adopted without process custody",
                session_id=session.session_id,
                code="browser_process_custody_unavailable",
                terminal=True,
            )
        adopter = getattr(self.process_controller, "adopt", None)
        if not callable(adopter):
            raise BrowserLaunchFailed(
                "browser process controller does not support durable adoption",
                session_id=session.session_id,
                code="browser_process_adoption_unsupported",
                terminal=True,
            )
        profile = self.profile_store.get(session.profile_id) if session.profile_id else None
        if profile is None:
            raise BrowserLaunchFailed(
                "persisted browser profile is unavailable for process adoption",
                session_id=session.session_id,
                code="browser_process_custody_profile_missing",
                terminal=True,
            )
        try:
            adopter(
                session_id=session.session_id,
                pid=session.process_id,
                endpoint_url=session.endpoint_url,
                runtime_dir=profile.root / "process",
                user_data_dir=profile.root / "user-data",
                executable_path=command.executable_path,
                readiness_timeout_seconds=self.config.connect_timeout_seconds,
            )
        except Exception as error:
            raise BrowserLaunchFailed(
                f"persisted local Chrome process failed custody validation: {error}",
                session_id=session.session_id,
                code="browser_process_custody_validation_failed",
                terminal=True,
                details={"pid": session.process_id, "endpoint_url": session.endpoint_url},
            ) from error

    def _endpoint_reachable(self, endpoint_url: str, command: BrowserSessionCommand) -> bool:
        try:
            endpoint = BrowserEndpoint(
                http_url=endpoint_url,
                headers=self._headers(command),
                proxy_url=command.proxy_url,
            )
            BrowserEndpointDiscovery(endpoint, timeout_seconds=self.config.connect_timeout_seconds).discover()
            return True
        except Exception:
            return False

    def _command_from_session(self, session: BrowserSessionRef) -> BrowserSessionCommand:
        executable_path = None
        if session.process_id is not None and self.process_controller is not None:
            resolver = getattr(self.process_controller, "executable_path", None)
            if callable(resolver):
                executable_path = resolver(session.process_id)
        return BrowserSessionCommand(
            run_id=session.run_id,
            task_id=session.task_id,
            worker_request_id=session.worker_request_id,
            canonical_session_id=session.canonical_session_id,
            browser_session_id=session.session_id,
            workspace_root=self.config.runtime_root,
            artifact_root=self.config.artifact_root,
            executable_path=executable_path,
            endpoint_url="" if session.process_id is not None else session.endpoint_url,
            keep_alive=session.keep_alive,
        )

    def _release_runtime_resources(self, session_id: str, *, reason: str) -> None:
        cdp = self._cdp.pop(session_id, None)
        if cdp is not None:
            cdp.close(reason=reason)
        target_runtime = self._targets.pop(session_id, None)
        if target_runtime is not None:
            target_runtime.clear()
        event_bus = self._event_buses.pop(session_id, None)
        if event_bus is not None:
            event_bus.stop(drain=True)

    def _stop_process(self, process_id: int, *, force: bool) -> None:
        if self.process_controller is None:
            return
        if hasattr(self.process_controller, "stop"):
            self.process_controller.stop(process_id, force=force)
        elif hasattr(self.process_controller, "terminate"):
            self.process_controller.terminate(process_id, force=force)

    def _process_alive(self, process_id: int | None) -> bool:
        if process_id is None:
            return False
        if self.process_controller is None:
            return False
        if hasattr(self.process_controller, "is_alive"):
            return bool(self.process_controller.is_alive(process_id))
        return True

    def _process_owned(self, process_id: int) -> bool:
        if self.process_controller is None:
            return False
        owns = getattr(self.process_controller, "owns", None)
        return bool(callable(owns) and owns(process_id))

    def _headers(self, command: BrowserSessionCommand):
        from zyra_integrations.browser_use.discovery import RedactedHeaders
        return RedactedHeaders.build(command.headers)

    def _headers_from_policy(self, policy: BrowserConnectionPolicy):
        from zyra_integrations.browser_use.discovery import RedactedHeaders
        return RedactedHeaders(
            transport=dict(policy.headers.transport),
            public=dict(policy.headers.public),
        )
    def _emit(
        self,
        command: BrowserSessionCommand,
        session: BrowserSessionRef,
        topic: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        event = BrowserLifecycleEvent(
            topic=topic,
            session_id=session.session_id,
            run_id=command.run_id,
            task_id=command.task_id,
            worker_request_id=command.worker_request_id,
            generation=session.revision,
            payload={"session": session.to_dict(), **dict(payload)},
        )
        if hasattr(self.artifact_bridge, "append"):
            self.artifact_bridge.append(event)
        return event.to_dict()

    def _result(
        self,
        session: BrowserSessionRef,
        *,
        created: bool,
        reused: bool,
        reconnected: bool,
        events: list[Mapping[str, Any]] | None = None,
    ) -> BrowserSessionStartResult:
        diagnostic = self.diagnose(session.session_id)
        return BrowserSessionStartResult(
            ok=diagnostic.ok or session.status == str(BrowserSessionStatus.RUNNING),
            created=created,
            reused=reused,
            reconnected=reconnected,
            session=session,
            diagnostic=diagnostic,
            events=tuple(events or ()),
            error="" if session.status == str(BrowserSessionStatus.RUNNING) else "browser_session_not_running",
        )


def _endpoint_reattach_command(command: BrowserSessionCommand, endpoint_url: str) -> BrowserSessionCommand:
    """Build an attach-only command after signed local-process adoption."""
    return replace(command, endpoint_url=endpoint_url, executable_path=None)
