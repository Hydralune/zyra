from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .contracts import (
    CorrelationRefs,
    FaultKind,
    ObservationCategory,
    ObservationProvenance,
    ObserverDescriptor,
    ObserverMaturity,
    StructuredObservation,
    runtime_id,
)


BROWSER_USE_REVISION = "18484f-source-audit"
OH_MY_PI_REVISION = "c6b83c-source-audit"
ZYRA_REVISION = "M1-S07B-01"


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...


def _descriptor(
    observer_id: str,
    display_name: str,
    category: ObservationCategory | tuple[ObservationCategory, ...],
    kinds: tuple[FaultKind, ...],
    *,
    observation_point: str,
    source_repo: str,
    source_revision: str,
    maturity: ObserverMaturity = ObserverMaturity.ACTIVE_REAL,
    enabled_by_default: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> ObserverDescriptor:
    return ObserverDescriptor(
        observer_id=observer_id,
        display_name=display_name,
        maturity=maturity,
        attach_owner="python.RuntimeWatchdog",
        lifecycle_owner="python.WatchdogObserverRegistry",
        observation_point=observation_point,
        source_repo=source_repo,
        source_revision=source_revision,
        emitted_kinds=kinds,
        categories=(category,) if isinstance(category, ObservationCategory) else category,
        enabled_by_default=enabled_by_default,
        metadata=dict(metadata or {}),
    )


class ManagedObserver:
    """Lifecycle-safe observation source.

    Attach and start are deliberately distinct.  A source can hold its native
    callback only after attach, and it cannot emit before the registry starts
    it.  This mirrors the browser-use watchdog lifecycle without inheriting the
    upstream disabled CrashWatchdog as an active source.
    """

    descriptor: ObserverDescriptor

    def __init__(self, descriptor: ObserverDescriptor) -> None:
        self.descriptor = descriptor
        self._emit: Callable[[StructuredObservation], None] | None = None
        self._attached = False
        self._running = False
        self._lock = threading.RLock()
        self._emitted = 0
        self._dropped = 0
        self._last_error = ""

    def attach(self, emit: Callable[[StructuredObservation], None]) -> None:
        if not callable(emit):
            raise TypeError("observer emit callback must be callable")
        with self._lock:
            if self._running:
                raise RuntimeError("cannot replace observer callback while running")
            self._emit = emit
            self._attached = True

    def start(self) -> None:
        with self._lock:
            if not self._attached or self._emit is None:
                raise RuntimeError("observer must be attached before start")
            self._running = True

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def submit(self, observation: StructuredObservation) -> bool:
        with self._lock:
            callback = self._emit
            if not self._running or callback is None:
                self._dropped += 1
                return False
        try:
            callback(observation)
        except Exception as error:
            with self._lock:
                self._last_error = f"{type(error).__name__}: {error}"
            raise
        with self._lock:
            self._emitted += 1
        return True

    def snapshot(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "observer_id": self.descriptor.observer_id,
                "attached": self._attached,
                "running": self._running,
                "emitted": self._emitted,
                "dropped_before_start_or_after_stop": self._dropped,
                "last_error": self._last_error,
            }

    def provenance(self, *, injection_id: str = "") -> ObservationProvenance:
        maturity = self.descriptor.maturity
        if injection_id:
            maturity = ObserverMaturity.INJECTION_ONLY
        return ObservationProvenance(
            observer_id=self.descriptor.observer_id,
            source_repo=self.descriptor.source_repo,
            source_revision=self.descriptor.source_revision,
            observation_point=self.descriptor.observation_point,
            maturity=maturity,
            injection_id=injection_id,
        )


@dataclass(slots=True)
class DeadlineRecord:
    refs: CorrelationRefs
    started_ms: int
    deadline_ms: int
    status: str = "pending"
    completed_ms: int | None = None
    timeout_observation_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "timed_out", "cancelled"}


class ToolDeadlineObserver(ManagedObserver):
    """Real deadline observer bound to explicit tool-call identities."""

    def __init__(self, *, monotonic_ms: Callable[[], int] | None = None) -> None:
        super().__init__(
            _descriptor(
                "tool-deadline",
                "Tool call deadline observer",
                ObservationCategory.TOOL,
                (FaultKind.TOOL_TIMEOUT,),
                observation_point="tool.dispatch.deadline",
                source_repo="oh-my-pi",
                source_revision=OH_MY_PI_REVISION,
                metadata={"mechanism": "timeout-race-and-terminal-result-guard"},
            )
        )
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._records: dict[str, DeadlineRecord] = {}
        self._revision = 0

    def begin(
        self,
        refs: CorrelationRefs,
        *,
        deadline_ms: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> DeadlineRecord:
        if not refs.tool_call_id or not refs.tool_name:
            raise ValueError("tool deadline requires explicit tool_call_id and tool_name")
        if deadline_ms <= 0:
            raise ValueError("tool deadline must be positive")
        with self._lock:
            existing = self._records.get(refs.tool_call_id)
            if existing is not None and not existing.terminal:
                raise RuntimeError(f"tool deadline already active: {refs.tool_call_id}")
            self._revision += 1
            record = DeadlineRecord(
                refs=refs,
                started_ms=self.monotonic_ms(),
                deadline_ms=int(deadline_ms),
                metadata=dict(metadata or {}),
            )
            self._records[refs.tool_call_id] = record
            return record

    def settle(
        self,
        tool_call_id: str,
        *,
        ok: bool,
        cancelled: bool = False,
    ) -> DeadlineRecord:
        with self._lock:
            record = self._records.get(tool_call_id)
            if record is None:
                raise KeyError(f"unknown tool deadline: {tool_call_id}")
            if record.status == "timed_out":
                return record
            record.completed_ms = self.monotonic_ms()
            record.status = "cancelled" if cancelled else ("succeeded" if ok else "failed")
            return record

    def poll(self, *, at_ms: int | None = None) -> tuple[StructuredObservation, ...]:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        observations: list[StructuredObservation] = []
        with self._lock:
            records = tuple(self._records.values())
        for record in records:
            if record.terminal:
                continue
            elapsed = max(0, now - record.started_ms)
            if elapsed < record.deadline_ms:
                continue
            observation_id = runtime_id("tool-timeout-observation")
            self._revision += 1
            refs = record.refs.with_observation(observation_id, revision=self._revision)
            observation = StructuredObservation(
                category=ObservationCategory.TOOL,
                code="tool_timeout",
                refs=refs,
                provenance=self.provenance(),
                summary=f"Tool {refs.tool_name} exceeded its configured deadline.",
                status="timed_out",
                error_type="ToolTimeoutError",
                retryable_hint=True,
                terminal_hint=False,
                elapsed_ms=elapsed,
                deadline_ms=record.deadline_ms,
                details={
                    **record.metadata,
                    "started_monotonic_ms": record.started_ms,
                    "observed_monotonic_ms": now,
                    "terminal_result_guarded": True,
                },
            )
            if self.submit(observation):
                with self._lock:
                    record.status = "timed_out"
                    record.completed_ms = now
                    record.timeout_observation_id = observation_id
                observations.append(observation)
        return tuple(observations)

    def snapshot(self) -> Mapping[str, Any]:
        base = dict(super().snapshot())
        with self._lock:
            base["deadlines"] = {
                key: {
                    "status": value.status,
                    "deadline_ms": value.deadline_ms,
                    "started_ms": value.started_ms,
                    "completed_ms": value.completed_ms,
                    "observation_id": value.timeout_observation_id,
                }
                for key, value in sorted(self._records.items())
            }
        return base


@dataclass(slots=True)
class ProcessRecord:
    refs: CorrelationRefs
    handle: ProcessLike
    generation: int
    expected_stop: bool = False
    last_poll_code: int | None = None
    emitted_generation: int = -1
    metadata: dict[str, Any] = field(default_factory=dict)


class ProcessLifecycleObserver(ManagedObserver):
    """Observes a real process handle and emits exactly once per generation."""

    def __init__(self) -> None:
        super().__init__(
            _descriptor(
                "process-lifecycle",
                "Worker and MCP process lifecycle observer",
                (
                    ObservationCategory.PROCESS,
                    ObservationCategory.WORKER,
                    ObservationCategory.MCP,
                ),
                (FaultKind.PROCESS_EXITED, FaultKind.WORKER_UNAVAILABLE, FaultKind.MCP_DISCONNECTED),
                observation_point="runtime.process.poll",
                source_repo="oh-my-pi",
                source_revision=OH_MY_PI_REVISION,
                metadata={"mechanism": "stdio-process-exit-and-reconnect-boundary"},
            )
        )
        self._processes: dict[str, ProcessRecord] = {}
        self._revision = 0

    def bind(
        self,
        process_key: str,
        refs: CorrelationRefs,
        handle: ProcessLike,
        *,
        generation: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProcessRecord:
        if not process_key.strip():
            raise ValueError("process_key must not be empty")
        if generation < 0 or int(handle.pid) <= 0:
            raise ValueError("process handle requires positive pid and non-negative generation")
        with self._lock:
            current = self._processes.get(process_key)
            if current is not None and generation < current.generation:
                raise RuntimeError("stale process generation cannot replace current handle")
            record = ProcessRecord(
                refs=refs,
                handle=handle,
                generation=generation,
                metadata={"process_key": process_key, **dict(metadata or {})},
            )
            self._processes[process_key] = record
            return record

    def expected_stop(self, process_key: str) -> None:
        with self._lock:
            self._processes[process_key].expected_stop = True

    def poll(self) -> tuple[StructuredObservation, ...]:
        observations: list[StructuredObservation] = []
        with self._lock:
            items = tuple(self._processes.items())
        for process_key, record in items:
            exit_code = record.handle.poll()
            record.last_poll_code = exit_code
            if exit_code is None or record.expected_stop or record.emitted_generation == record.generation:
                continue
            self._revision += 1
            observation_id = runtime_id("process-exit-observation")
            category = ObservationCategory.MCP if record.refs.mcp_server_id else ObservationCategory.WORKER
            code = "server_exited" if record.refs.mcp_server_id else "worker_lost"
            refs = record.refs.with_observation(observation_id, revision=self._revision)
            observation = StructuredObservation(
                category=category,
                code=code,
                refs=refs,
                provenance=self.provenance(),
                summary=f"Runtime process {process_key} exited unexpectedly.",
                status="exited",
                error_type="ProcessExit",
                retryable_hint=True,
                terminal_hint=False,
                details={
                    **record.metadata,
                    "pid": int(record.handle.pid),
                    "exit_code": int(exit_code),
                    "generation": record.generation,
                    "expected_stop": False,
                },
            )
            if self.submit(observation):
                record.emitted_generation = record.generation
                observations.append(observation)
        return tuple(observations)

    def snapshot(self) -> Mapping[str, Any]:
        value = dict(super().snapshot())
        with self._lock:
            value["processes"] = {
                key: {
                    "pid": int(record.handle.pid),
                    "generation": record.generation,
                    "expected_stop": record.expected_stop,
                    "last_poll_code": record.last_poll_code,
                    "emitted_generation": record.emitted_generation,
                }
                for key, record in sorted(self._processes.items())
            }
        return value


class PermissionReceiptObserver(ManagedObserver):
    """Adapts deterministic permission receipts; it never parses error prose."""

    def __init__(self) -> None:
        super().__init__(
            _descriptor(
                "permission-receipt",
                "Tool permission receipt observer",
                ObservationCategory.PERMISSION,
                (FaultKind.PERMISSION_DENIED,),
                observation_point="tool.permission.receipt",
                source_repo="zyra",
                source_revision=ZYRA_REVISION,
            )
        )
        self._revision = 0

    def observe_receipt(self, refs: CorrelationRefs, receipt: Mapping[str, Any]) -> StructuredObservation | None:
        decision = str(receipt.get("decision", "")).strip().lower()
        if decision not in {"deny", "denied", "block", "blocked"}:
            return None
        if not refs.tool_call_id or not refs.tool_name:
            raise ValueError("permission denial requires explicit tool call identities")
        self._revision += 1
        observation = StructuredObservation(
            category=ObservationCategory.PERMISSION,
            code="permission_denied",
            refs=refs.with_observation(runtime_id("permission-observation"), revision=self._revision),
            provenance=self.provenance(),
            summary=f"Permission policy denied tool {refs.tool_name}.",
            status="denied",
            error_type="PermissionDenied",
            retryable_hint=False,
            terminal_hint=True,
            details={
                "decision": decision,
                "rule_id": str(receipt.get("rule_id", "")),
                "policy_revision": int(receipt.get("policy_revision", 0) or 0),
                "interactive": bool(receipt.get("interactive", False)),
                "reason_code": str(receipt.get("reason_code", "policy_denied")),
            },
        )
        self.submit(observation)
        return observation


class ProviderFailureObserver(ManagedObserver):
    """Provider response observer using OMP-shaped structured failure fields."""

    def __init__(self) -> None:
        super().__init__(
            _descriptor(
                "provider-response",
                "Model provider response observer",
                ObservationCategory.PROVIDER,
                (
                    FaultKind.MODEL_FAILURE,
                    FaultKind.MODEL_RATE_LIMIT,
                    FaultKind.MODEL_QUOTA_EXHAUSTED,
                ),
                observation_point="provider.response.terminal",
                source_repo="oh-my-pi",
                source_revision=OH_MY_PI_REVISION,
                metadata={"mechanism": "structured-provider-error-and-retryability"},
            )
        )
        self._revision = 0

    def observe_response(
        self,
        refs: CorrelationRefs,
        *,
        ok: bool,
        status_code: int | None = None,
        error_type: str = "",
        error_code: str = "",
        retryable: bool | None = None,
        terminal: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> StructuredObservation | None:
        if ok:
            return None
        if not refs.provider_id:
            raise ValueError("provider failure requires explicit provider_id")
        normalized_code = error_code.strip().lower() or "provider_error"
        if status_code == 429 and normalized_code not in {
            "rate_limit",
            "rate_limited",
            "quota_exhausted",
            "usage_limit_reached",
            "insufficient_quota",
            "credits_exhausted",
        }:
            normalized_code = "rate_limited"
        elif status_code in {408, 500, 502, 503, 504, 529}:
            normalized_code = "provider_error"
        self._revision += 1
        observation = StructuredObservation(
            category=ObservationCategory.PROVIDER,
            code=normalized_code,
            refs=refs.with_observation(runtime_id("provider-observation"), revision=self._revision),
            provenance=self.provenance(),
            summary=f"Provider {refs.provider_id} returned a structured failure.",
            status="failed",
            status_code=status_code,
            error_type=error_type or "ProviderError",
            retryable_hint=retryable,
            terminal_hint=terminal,
            details={"provider_error_code": normalized_code, **dict(details or {})},
        )
        self.submit(observation)
        return observation


class SchemaValidationObserver(ManagedObserver):
    def __init__(self) -> None:
        super().__init__(
            _descriptor(
                "schema-validation",
                "Canonical schema validation observer",
                ObservationCategory.SCHEMA,
                (FaultKind.SCHEMA_FAILURE,),
                observation_point="runtime.schema.validation",
                source_repo="zyra",
                source_revision=ZYRA_REVISION,
            )
        )
        self._revision = 0

    def observe_validation(
        self,
        refs: CorrelationRefs,
        *,
        schema_id: str,
        valid: bool,
        violations: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
    ) -> StructuredObservation | None:
        if valid:
            return None
        normalized = [
            {"path": str(item.get("path", "")), "code": str(item.get("code", "invalid"))}
            for item in violations
        ]
        self._revision += 1
        observation = StructuredObservation(
            category=ObservationCategory.SCHEMA,
            code="validation_failed",
            refs=refs.with_observation(runtime_id("schema-observation"), revision=self._revision),
            provenance=self.provenance(),
            summary=f"Canonical schema {schema_id} rejected runtime state.",
            status="invalid",
            error_type="SchemaValidationError",
            retryable_hint=False,
            terminal_hint=True,
            details={"schema_id": schema_id, "violations": normalized, "violation_count": len(normalized)},
        )
        self.submit(observation)
        return observation


class SubagentLifecycleObserver(ManagedObserver):
    """Adapts durable AgentTool/background-task terminal receipts."""

    def __init__(self) -> None:
        super().__init__(
            _descriptor(
                "subagent-lifecycle",
                "Logical subagent terminal lifecycle observer",
                ObservationCategory.WORKER,
                (FaultKind.SUBAGENT_FAILED,),
                observation_point="subagent.task.terminal-receipt",
                source_repo="zyra",
                source_revision=ZYRA_REVISION,
                metadata={
                    "logical_state_owner": "typescript.DurableTaskRegistry",
                    "physical_worker_state_owner": "python.WorkerPoolStore",
                    "failure_text_identity_inference": False,
                },
            )
        )
        self._revision = 0

    def observe_terminal_receipt(
        self,
        refs: CorrelationRefs,
        receipt: Mapping[str, Any],
    ) -> StructuredObservation | None:
        status = str(receipt.get("status") or "").strip().lower()
        if status not in {"failed", "error"}:
            return None
        if not refs.subagent_task_id:
            raise ValueError("subagent failure requires explicit subagent_task_id")
        self._revision += 1
        observation = StructuredObservation(
            category=ObservationCategory.WORKER,
            code="subagent_failed",
            refs=refs.with_observation(runtime_id("subagent-observation"), revision=self._revision),
            provenance=self.provenance(),
            summary=f"Logical subagent task {refs.subagent_task_id} failed before completion.",
            status="failed",
            error_type=str(receipt.get("error_type") or "SubagentTaskFailed"),
            retryable_hint=bool(receipt.get("retryable", True)),
            terminal_hint=True,
            details={
                "terminal_revision": int(receipt.get("revision", 0) or 0),
                "attempt_number": int(receipt.get("attempt_number", 0) or 0),
                "failure_code": str(receipt.get("failure_code") or "subagent_failed"),
                "checkpoint_id": str(receipt.get("checkpoint_id") or ""),
                "logical_state_owner": "typescript.DurableTaskRegistry",
                "critical_ref_source": "structured_refs_only",
            },
        )
        self.submit(observation)
        return observation


class WorkspaceIntegrityObserver(ManagedObserver):
    """Validates explicit file attestations without owning workspace state."""

    def __init__(self, workspace_roots: Mapping[str, str | Path] | None = None) -> None:
        super().__init__(
            _descriptor(
                "workspace-integrity",
                "Workspace integrity observer",
                ObservationCategory.WORKSPACE,
                (FaultKind.WORKSPACE_CORRUPT,),
                observation_point="workspace.integrity.attestation",
                source_repo="zyra",
                source_revision=ZYRA_REVISION,
                enabled_by_default=False,
                maturity=ObserverMaturity.EXPERIMENTAL,
            )
        )
        self._roots = {key: Path(value).resolve() for key, value in dict(workspace_roots or {}).items()}
        self._revision = 0

    def register_workspace(self, workspace_id: str, root: str | Path) -> None:
        selected = Path(root).resolve()
        if not selected.exists() or not selected.is_dir():
            raise ValueError("workspace root must be an existing directory")
        self._roots[workspace_id] = selected

    @staticmethod
    def digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        return "sha256:" + hasher.hexdigest()

    def attest(
        self,
        refs: CorrelationRefs,
        *,
        relative_path: str,
        expected_digest: str,
    ) -> StructuredObservation | None:
        if not refs.workspace_id:
            raise ValueError("workspace attestation requires explicit workspace_id")
        root = self._roots.get(refs.workspace_id)
        if root is None:
            raise KeyError(f"workspace root is not registered: {refs.workspace_id}")
        target = (root / relative_path).resolve()
        if target != root and root not in target.parents:
            raise ValueError("workspace attestation path escapes registered root")
        exists = target.is_file()
        actual = self.digest(target) if exists else "missing"
        if actual == expected_digest:
            return None
        self._revision += 1
        observation = StructuredObservation(
            category=ObservationCategory.WORKSPACE,
            code="workspace_corrupt",
            refs=refs.with_observation(runtime_id("workspace-observation"), revision=self._revision),
            provenance=self.provenance(),
            summary=f"Workspace file {relative_path} failed integrity attestation.",
            status="corrupt",
            error_type="WorkspaceIntegrityError",
            retryable_hint=False,
            terminal_hint=True,
            details={
                "relative_path": relative_path.replace(os.sep, "/"),
                "expected_digest": expected_digest,
                "actual_digest": actual,
                "exists": exists,
                "root_digest": hashlib.sha256(str(root).encode("utf-8")).hexdigest(),
            },
        )
        self.submit(observation)
        return observation


class BrowserCrashObserver(ManagedObserver):
    """Bridge for the active Zyra 04D detector and browser-use lifecycle facts."""

    _CODE_MAP = {
        "browser_process_exited": "process_exited",
        "process_exited": "process_exited",
        "cdp_disconnected": "cdp_disconnected",
        "heartbeat_timeout": "heartbeat_late",
        "heartbeat_late": "heartbeat_late",
        "request_timeout": "request_timeout",
        "browser_crashed": "process_exited",
    }

    def __init__(self) -> None:
        super().__init__(
            _descriptor(
                "browser-crash",
                "Browser process and CDP crash observer",
                ObservationCategory.BROWSER,
                (FaultKind.BROWSER_CRASH, FaultKind.BROWSER_DISCONNECT),
                observation_point="browser.04d.watchdog-signal",
                source_repo="browser-use",
                source_revision=BROWSER_USE_REVISION,
                metadata={
                    "active_implementation": "zyra_workers.browser_observability.BrowserCrashDetector",
                    "upstream_crash_watchdog": "source_inactive",
                },
            )
        )
        self._sequences: dict[str, int] = {}

    def observe_04d_signal(self, signal: Mapping[str, Any]) -> StructuredObservation | None:
        scope = dict(signal.get("scope") or {})
        run_id = str(scope.get("run_id", ""))
        task_id = str(scope.get("task_id", ""))
        browser_session_id = str(scope.get("browser_session_id", ""))
        raw_kind = str(signal.get("kind", "")).split(".")[-1].lower()
        code = self._CODE_MAP.get(raw_kind)
        if code is None:
            return None
        if not run_id or not task_id or not browser_session_id:
            raise ValueError("04D browser signal lacks explicit run/task/browser identities")
        key = f"{run_id}:{task_id}:{browser_session_id}"
        sequence = int(signal.get("sequence", 0) or 0)
        prior = self._sequences.get(key, -1)
        if sequence <= prior:
            return None
        self._sequences[key] = sequence
        metadata = dict(signal.get("metadata") or {})
        refs = CorrelationRefs(
            run_id=run_id,
            task_id=task_id,
            observation_id=runtime_id("browser-observation"),
            session_id=str(scope.get("canonical_session_id", "")),
            node_id=str(scope.get("node_id", "")),
            attempt_id=str(scope.get("worker_request_id", "")),
            browser_session_id=browser_session_id,
            source_state_revision=sequence,
        )
        observation = StructuredObservation(
            category=ObservationCategory.BROWSER,
            code=code,
            refs=refs,
            provenance=self.provenance(),
            summary=str(signal.get("summary", "Browser watchdog observed a crash boundary.")),
            status=str(signal.get("status", "failed")).split(".")[-1].lower(),
            error_type="BrowserCrash" if code == "process_exited" else "BrowserDisconnect",
            retryable_hint=bool(signal.get("retryable", True)),
            terminal_hint=bool(signal.get("terminal", False)),
            elapsed_ms=int(metadata["elapsed_ms"]) if "elapsed_ms" in metadata else None,
            deadline_ms=int(metadata["deadline_ms"]) if "deadline_ms" in metadata else None,
            details={
                **metadata,
                "source_signal_id": str(signal.get("signal_id", "")),
                "source_watchdog": str(signal.get("watchdog", "")),
                "source_sequence": sequence,
            },
            evidence_event_ids=tuple(str(item) for item in signal.get("evidence_event_ids", ())),
        )
        self.submit(observation)
        return observation


def source_inactive_browser_descriptor() -> ObserverDescriptor:
    return _descriptor(
        "browser-use-crash-watchdog-upstream",
        "Browser Use upstream CrashWatchdog (not attached)",
        ObservationCategory.BROWSER,
        (FaultKind.BROWSER_CRASH,),
        observation_point="browser_use.watchdogs.crash_watchdog",
        source_repo="browser-use",
        source_revision=BROWSER_USE_REVISION,
        maturity=ObserverMaturity.SOURCE_INACTIVE,
        enabled_by_default=False,
        metadata={
            "reason": "upstream BrowserSession keeps CrashWatchdog disabled/commented",
            "replacement": "browser-crash",
            "runtime_callbacks_attached": False,
        },
    )


def advisor_candidate_descriptor() -> ObserverDescriptor:
    return _descriptor(
        "omp-advisor-candidate",
        "OMP advisor concern candidate (non-fault)",
        ObservationCategory.PROCESS,
        (),
        observation_point="omp.advisor.note",
        source_repo="oh-my-pi",
        source_revision=OH_MY_PI_REVISION,
        maturity=ObserverMaturity.EXPERIMENTAL,
        enabled_by_default=False,
        metadata={
            "candidate_only": True,
            "mutating_tools_allowed": False,
            "emits_faults": False,
            "reason": "advisor concern/blocker has no real process/tool/transport observation",
        },
    )


def injection_observer_descriptor() -> ObserverDescriptor:
    return ObserverDescriptor(
        observer_id="same-run-fault-injector",
        display_name="Same-run deterministic fault injector",
        maturity=ObserverMaturity.INJECTION_ONLY,
        attach_owner="python.FaultInjectionRuntime",
        lifecycle_owner="python.WatchdogObserverRegistry",
        observation_point="fault.inject.same-run",
        source_repo="zyra",
        source_revision=ZYRA_REVISION,
        emitted_kinds=tuple(FaultKind),
        categories=(
            ObservationCategory.WORKER,
            ObservationCategory.TOOL,
            ObservationCategory.BROWSER,
            ObservationCategory.PROVIDER,
            ObservationCategory.WORKSPACE,
        ),
        enabled_by_default=False,
        metadata={"real_source": False, "can_mask_disabled_observer": False},
    )


def observer_contract() -> dict[str, Any]:
    descriptors = (
        ToolDeadlineObserver().descriptor,
        ProcessLifecycleObserver().descriptor,
        PermissionReceiptObserver().descriptor,
        ProviderFailureObserver().descriptor,
        SchemaValidationObserver().descriptor,
        SubagentLifecycleObserver().descriptor,
        WorkspaceIntegrityObserver().descriptor,
        BrowserCrashObserver().descriptor,
        source_inactive_browser_descriptor(),
        advisor_candidate_descriptor(),
        injection_observer_descriptor(),
    )
    return {
        "schema": "zyra.watchdog-observer-contract/v1",
        "maturity_vocabulary": [item.value for item in ObserverMaturity],
        "observers": [descriptor.to_dict() for descriptor in descriptors],
        "requirement_changed_is_fault": False,
        "identity_inference_from_free_text": False,
        "state_owner": "python.FaultStateStore",
    }


__all__ = [
    "BrowserCrashObserver",
    "DeadlineRecord",
    "ManagedObserver",
    "PermissionReceiptObserver",
    "ProcessLifecycleObserver",
    "ProcessRecord",
    "ProviderFailureObserver",
    "SchemaValidationObserver",
    "SubagentLifecycleObserver",
    "ToolDeadlineObserver",
    "WorkspaceIntegrityObserver",
    "injection_observer_descriptor",
    "observer_contract",
    "source_inactive_browser_descriptor",
    "advisor_candidate_descriptor",
]
