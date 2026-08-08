from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, ProbeStatus, Severity, utc_now


class DisableProbeError(RuntimeError):
    pass


class DisableProbeBlocked(DisableProbeError):
    pass


class ProbeHandle(Protocol):
    probe_id: str
    capability: str
    dependencies: tuple[str, ...]
    timeout_seconds: float
    expected_error_codes: tuple[str, ...]
    allow_success_with_difference: bool

    def capture(self) -> Mapping[str, Any]: ...

    def exercise(self) -> Mapping[str, Any]: ...

    def disable(self) -> Mapping[str, Any] | None: ...

    def restore(self, captured: Mapping[str, Any]) -> Mapping[str, Any] | None: ...


@dataclass(slots=True)
class FunctionDisableProbe:
    probe_id: str
    capability: str
    capture_fn: Callable[[], Mapping[str, Any]]
    exercise_fn: Callable[[], Mapping[str, Any]]
    disable_fn: Callable[[], Mapping[str, Any] | None]
    restore_fn: Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
    dependencies: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    expected_error_codes: tuple[str, ...] = ()
    allow_success_with_difference: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def capture(self) -> Mapping[str, Any]:
        return dict(self.capture_fn())

    def exercise(self) -> Mapping[str, Any]:
        return dict(self.exercise_fn())

    def disable(self) -> Mapping[str, Any] | None:
        value = self.disable_fn()
        return dict(value) if isinstance(value, Mapping) else value

    def restore(self, captured: Mapping[str, Any]) -> Mapping[str, Any] | None:
        value = self.restore_fn(captured)
        return dict(value) if isinstance(value, Mapping) else value


@dataclass(slots=True)
class ProbeExecution:
    probe_id: str
    capability: str
    status: ProbeStatus
    started_at: str
    completed_at: str
    duration_ms: int
    baseline: Mapping[str, Any] = field(default_factory=dict)
    disabled: Mapping[str, Any] = field(default_factory=dict)
    disable_receipt: Mapping[str, Any] = field(default_factory=dict)
    restore_receipt: Mapping[str, Any] = field(default_factory=dict)
    difference: Mapping[str, Any] = field(default_factory=dict)
    expected_failure_observed: bool = False
    fallback_masked: bool = False
    error_code: str = ""
    error_message: str = ""
    traceback_text: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "capability": self.capability,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "baseline": dict(self.baseline),
            "disabled": dict(self.disabled),
            "disable_receipt": dict(self.disable_receipt),
            "restore_receipt": dict(self.restore_receipt),
            "difference": dict(self.difference),
            "expected_failure_observed": self.expected_failure_observed,
            "fallback_masked": self.fallback_masked,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "traceback": self.traceback_text,
            "metadata": dict(self.metadata),
        }


class ProbeResultComparator:
    _VOLATILE_KEYS = {
        "timestamp",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "duration_ms",
        "request_id",
        "event_id",
        "trace_id",
        "span_id",
        # Kept in receipts for forensic identity, but the digest represents the
        # full response and therefore changes on fresh task/session IDs.  It
        # must not by itself satisfy a material semantic-difference contract.
        "response_digest",
    }

    def compare(
        self,
        baseline: Mapping[str, Any],
        disabled: Mapping[str, Any],
    ) -> dict[str, Any]:
        left = self._stable(baseline)
        right = self._stable(disabled)
        changed = self._diff(left, right)
        baseline_ok = self._ok(baseline)
        disabled_ok = self._ok(disabled)
        baseline_owner = self._owner(baseline)
        disabled_owner = self._owner(disabled)
        error_code = self._error_code(disabled)
        semantic_change = bool(changed) and bool(
            baseline_ok != disabled_ok
            or baseline_owner != disabled_owner
            or error_code
            or self._semantic_digest(baseline) != self._semantic_digest(disabled)
        )
        return {
            "baseline_ok": baseline_ok,
            "disabled_ok": disabled_ok,
            "baseline_owner": baseline_owner,
            "disabled_owner": disabled_owner,
            "error_code": error_code,
            "changed_paths": sorted(changed),
            "semantic_change": semantic_change,
            "baseline_digest": self._semantic_digest(baseline),
            "disabled_digest": self._semantic_digest(disabled),
        }

    def fallback_masked(
        self,
        baseline: Mapping[str, Any],
        disabled: Mapping[str, Any],
        difference: Mapping[str, Any],
    ) -> bool:
        if not self._ok(disabled):
            return False
        if not difference.get("semantic_change"):
            return True
        owner = self._owner(disabled).lower()
        baseline_owner = self._owner(baseline).lower()
        fallback = str(disabled.get("fallback") or disabled.get("fallback_owner") or "").lower()
        if fallback and fallback not in {"false", "none", "disabled"}:
            return True
        if baseline_owner and owner and baseline_owner != owner:
            return True
        metadata = disabled.get("metadata") if isinstance(disabled.get("metadata"), Mapping) else {}
        return bool(metadata.get("legacy_fallback") or metadata.get("vendor_fallback") or metadata.get("mock_fallback"))

    def restored_equivalent(
        self,
        baseline: Mapping[str, Any],
        restored: Mapping[str, Any],
    ) -> bool:
        """Compare capability recovery without requiring mutation outputs to rewind.

        Real owner probes may replay a state-changing request.  Its revision and
        response digest are expected to advance after restore; recovery means the
        owner is healthy again, its identity is unchanged, fallback remains off,
        and the disable error is gone.
        """

        return (
            self._ok(baseline) == self._ok(restored)
            and self._owner(baseline) == self._owner(restored)
            and self._error_code(baseline) == self._error_code(restored)
            and not bool(restored.get("fallback") or restored.get("fallback_owner"))
        )

    def expected_failure(
        self,
        disabled: Mapping[str, Any],
        expected_error_codes: Sequence[str],
    ) -> bool:
        if self._ok(disabled):
            return False
        if not expected_error_codes:
            return True
        code = self._error_code(disabled)
        return code in set(expected_error_codes)

    def _stable(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): self._stable(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(key).lower() not in self._VOLATILE_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [self._stable(item) for item in value]
        if isinstance(value, set):
            return sorted(self._stable(item) for item in value)
        return value

    def _diff(self, left: Any, right: Any, prefix: str = "$") -> set[str]:
        if type(left) is not type(right):
            return {prefix}
        if isinstance(left, Mapping):
            changed: set[str] = set()
            keys = set(left) | set(right)
            for key in keys:
                path = f"{prefix}.{key}"
                if key not in left or key not in right:
                    changed.add(path)
                else:
                    changed.update(self._diff(left[key], right[key], path))
            return changed
        if isinstance(left, list):
            changed: set[str] = set()
            if len(left) != len(right):
                changed.add(f"{prefix}.length")
            for index, (l_value, r_value) in enumerate(zip(left, right)):
                changed.update(self._diff(l_value, r_value, f"{prefix}[{index}]"))
            return changed
        return set() if left == right else {prefix}

    def _semantic_digest(self, value: Mapping[str, Any]) -> str:
        stable = self._stable(value)
        encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _ok(value: Mapping[str, Any]) -> bool:
        if "ok" in value:
            return value.get("ok") is True
        status = str(value.get("status") or value.get("state") or "").lower()
        return status in {"ok", "passed", "success", "succeeded", "completed", "active"}

    @staticmethod
    def _error_code(value: Mapping[str, Any]) -> str:
        error = value.get("error")
        if isinstance(error, Mapping):
            return str(error.get("code") or error.get("kind") or "")
        return str(value.get("error_code") or error or value.get("code") or "")

    @staticmethod
    def _owner(value: Mapping[str, Any]) -> str:
        metadata = value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}
        return str(
            value.get("canonical_owner")
            or value.get("state_owner")
            or value.get("runtime_owner")
            or metadata.get("canonical_owner")
            or metadata.get("state_owner")
            or ""
        )


class DisableProbeRunner:
    def __init__(self, *, max_workers: int = 1) -> None:
        self.max_workers = max(1, int(max_workers))
        self.comparator = ProbeResultComparator()
        self._locks: dict[str, threading.RLock] = {}
        self._lock_guard = threading.RLock()

    def run(self, probe: ProbeHandle) -> ProbeExecution:
        started = utc_now()
        start_clock = time.monotonic()
        execution = ProbeExecution(
            probe_id=probe.probe_id,
            capability=probe.capability,
            status=ProbeStatus.FAILED,
            started_at=started,
            completed_at="",
            duration_ms=0,
        )
        lock = self._capability_lock(probe.capability)
        with lock:
            captured: Mapping[str, Any] = {}
            disabled_applied = False
            try:
                captured = self._call(probe.capture, probe.timeout_seconds, "capture")
                baseline = self._call(probe.exercise, probe.timeout_seconds, "baseline exercise")
                # Preserve the exact failed baseline for diagnostics.  The
                # prior assignment happened only after the disabled exercise,
                # so a baseline_blocked receipt discarded the response that
                # explained why the real capability was unavailable.
                execution.baseline = baseline
                if not self.comparator._ok(baseline):
                    raise DisableProbeBlocked(
                        f"baseline behavior failed before disabling {probe.probe_id}: "
                        f"{self.comparator._error_code(baseline) or 'unknown error'}"
                    )
                receipt = self._call(probe.disable, probe.timeout_seconds, "disable")
                disabled_applied = True
                disabled = self._call(probe.exercise, probe.timeout_seconds, "disabled exercise")
                difference = self.comparator.compare(baseline, disabled)
                expected = self.comparator.expected_failure(disabled, probe.expected_error_codes)
                masked = self.comparator.fallback_masked(baseline, disabled, difference)
                allowed_difference = probe.allow_success_with_difference and bool(difference.get("semantic_change")) and not masked
                execution.disabled = disabled
                execution.disable_receipt = receipt
                execution.difference = difference
                execution.expected_failure_observed = expected or allowed_difference
                execution.fallback_masked = masked
                execution.error_code = self.comparator._error_code(disabled)
                execution.status = ProbeStatus.PASSED if execution.expected_failure_observed and not masked else ProbeStatus.FAILED
            except TimeoutError as error:
                execution.status = ProbeStatus.TIMED_OUT
                execution.error_code = "probe_timeout"
                execution.error_message = str(error)
            except DisableProbeBlocked as error:
                execution.status = ProbeStatus.BLOCKED
                execution.error_code = "baseline_blocked"
                execution.error_message = str(error)
            except Exception as error:  # probe boundaries must report restoration evidence
                execution.status = ProbeStatus.FAILED
                execution.error_code = type(error).__name__
                execution.error_message = str(error)
                execution.traceback_text = traceback.format_exc()
            finally:
                if disabled_applied or captured:
                    try:
                        restored = self._call(
                            lambda: probe.restore(captured),
                            probe.timeout_seconds,
                            "restore",
                        )
                        execution.restore_receipt = restored
                        post_restore = self._call(probe.exercise, probe.timeout_seconds, "post-restore exercise")
                        execution.metadata = {
                            **dict(execution.metadata),
                            "post_restore": post_restore,
                        }
                        if execution.baseline:
                            restore_difference = self.comparator.compare(execution.baseline, post_restore)
                            if not self.comparator.restored_equivalent(
                                execution.baseline,
                                post_restore,
                            ):
                                execution.status = ProbeStatus.FAILED
                                execution.error_code = "restore_semantic_mismatch"
                                execution.error_message = "Capability behavior did not return to its captured baseline."
                                execution.metadata = {**dict(execution.metadata), "restore_difference": restore_difference}
                    except Exception as restore_error:
                        execution.status = ProbeStatus.FAILED
                        execution.error_code = "restore_failed"
                        execution.error_message = str(restore_error)
                        execution.traceback_text = traceback.format_exc()
        execution.completed_at = utc_now()
        execution.duration_ms = max(0, int((time.monotonic() - start_clock) * 1000))
        return execution

    @staticmethod
    def _call(function: Callable[[], Any], timeout: float, stage: str) -> Mapping[str, Any]:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="zyra-disable-probe") as pool:
            future: Future[Any] = pool.submit(function)
            try:
                value = future.result(timeout=max(0.1, float(timeout)))
            except TimeoutError:
                future.cancel()
                raise TimeoutError(f"{stage} exceeded {timeout:.3f}s")
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise DisableProbeError(f"{stage} returned {type(value).__name__}, expected a mapping")
        return dict(value)

    def _capability_lock(self, capability: str) -> threading.RLock:
        with self._lock_guard:
            return self._locks.setdefault(capability, threading.RLock())


class DisableModuleProbe:
    def __init__(self, *, runner: DisableProbeRunner | None = None) -> None:
        self.runner = runner or DisableProbeRunner()
        self._probes: dict[str, ProbeHandle] = {}

    def register(self, probe: ProbeHandle) -> None:
        probe_id = str(probe.probe_id).strip()
        capability = str(probe.capability).strip()
        if not probe_id or not capability:
            raise ValueError("probe_id and capability are required")
        if probe_id in self._probes:
            raise ValueError(f"duplicate disable probe: {probe_id}")
        if probe_id in set(probe.dependencies):
            raise ValueError(f"disable probe cannot depend on itself: {probe_id}")
        self._probes[probe_id] = probe

    def registered_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._probes))

    def registered_capabilities(self) -> tuple[str, ...]:
        """Return the canonical capabilities backed by executable probes."""

        return tuple(sorted({probe.capability for probe in self._probes.values()}))

    def evaluate(
        self,
        *,
        required_capabilities: Iterable[str] = (),
        selected_probe_ids: Iterable[str] = (),
        execute: bool = True,
    ) -> GateResult:
        result = GateResult(
            gate_id="disable-module-probe",
            status=GateStatus.NOT_RUN,
            summary="Fail-closed M1 module-disable matrix with fallback masking detection.",
        )
        result.findings.extend(self._validate_registry(required_capabilities))
        selected = set(selected_probe_ids) or set(self._probes)
        unknown = selected - set(self._probes)
        for probe_id in sorted(unknown):
            result.add(
                Finding(
                    code="disable.probe_unknown",
                    severity=Severity.ERROR,
                    summary="Requested disable probe is not registered.",
                    detail=probe_id,
                )
            )
        if not execute:
            result.limitations.append("Disable probes were validated but not executed.")
            result.metrics["registered_probes"] = list(self.registered_ids())
            return result.finish(default_partial=True)

        ordered, cycle = self._order(selected & set(self._probes))
        if cycle:
            result.add(
                Finding(
                    code="disable.dependency_cycle",
                    severity=Severity.BLOCKER,
                    summary="Disable probe dependency graph contains a cycle.",
                    detail=" -> ".join(cycle),
                )
            )
            return result.finish()

        executions: list[ProbeExecution] = []
        status_by_probe: dict[str, ProbeStatus] = {}
        for probe_id in ordered:
            probe = self._probes[probe_id]
            blocked_dependencies = [
                dependency
                for dependency in probe.dependencies
                if dependency in status_by_probe and status_by_probe[dependency] is not ProbeStatus.PASSED
            ]
            if blocked_dependencies:
                execution = ProbeExecution(
                    probe_id=probe_id,
                    capability=probe.capability,
                    status=ProbeStatus.SKIPPED,
                    started_at=utc_now(),
                    completed_at=utc_now(),
                    duration_ms=0,
                    error_code="dependency_failed",
                    error_message=", ".join(blocked_dependencies),
                )
            else:
                execution = self.runner.run(probe)
            executions.append(execution)
            status_by_probe[probe_id] = execution.status
            result.findings.extend(self._findings_for(execution))
            result.evidence.append(
                EvidencePointer(
                    kind="disable_probe",
                    location=probe_id,
                    summary=f"{probe.capability}: {execution.status.value}",
                    metadata={
                        "duration_ms": execution.duration_ms,
                        "expected_failure_observed": execution.expected_failure_observed,
                        "fallback_masked": execution.fallback_masked,
                        "error_code": execution.error_code,
                    },
                )
            )
        counts: dict[str, int] = {}
        for status in ProbeStatus:
            counts[status.value] = sum(1 for execution in executions if execution.status is status)
        result.metrics.update(
            {
                "registered_count": len(self._probes),
                "executed_count": len(executions),
                "status_counts": counts,
                "executions": [execution.to_dict() for execution in executions],
            }
        )
        return result.finish()

    def _validate_registry(self, required_capabilities: Iterable[str]) -> list[Finding]:
        findings: list[Finding] = []
        registered_capabilities = {probe.capability for probe in self._probes.values()}
        for capability in sorted(set(required_capabilities) - registered_capabilities):
            findings.append(
                Finding(
                    code="disable.required_capability_missing",
                    severity=Severity.BLOCKER,
                    summary="Required M1 capability has no disable probe.",
                    capability=capability,
                )
            )
        for probe in self._probes.values():
            for dependency in probe.dependencies:
                if dependency not in self._probes:
                    findings.append(
                        Finding(
                            code="disable.dependency_missing",
                            severity=Severity.ERROR,
                            summary="Disable probe depends on an unregistered probe.",
                            capability=probe.capability,
                            detail=f"{probe.probe_id} -> {dependency}",
                        )
                    )
            if probe.timeout_seconds <= 0:
                findings.append(
                    Finding(
                        code="disable.timeout_invalid",
                        severity=Severity.ERROR,
                        summary="Disable probe timeout must be positive.",
                        detail=probe.probe_id,
                    )
                )
            if not probe.expected_error_codes and not probe.allow_success_with_difference:
                findings.append(
                    Finding(
                        code="disable.expected_effect_implicit",
                        severity=Severity.WARNING,
                        summary="Probe accepts any failed result; add a stable error code when possible.",
                        detail=probe.probe_id,
                    )
                )
        return findings

    @staticmethod
    def _findings_for(execution: ProbeExecution) -> list[Finding]:
        if execution.status is ProbeStatus.PASSED:
            return []
        if execution.status is ProbeStatus.BLOCKED:
            severity = Severity.BLOCKER
            code = "disable.baseline_blocked"
            summary = "Disable probe baseline could not exercise the real capability."
        elif execution.status is ProbeStatus.TIMED_OUT:
            severity = Severity.ERROR
            code = "disable.probe_timed_out"
            summary = "Disable probe exceeded its bounded execution time."
        elif execution.status is ProbeStatus.SKIPPED:
            severity = Severity.ERROR
            code = "disable.probe_skipped"
            summary = "Disable probe was skipped because a dependency failed."
        elif execution.fallback_masked:
            severity = Severity.BLOCKER
            code = "disable.fallback_masked"
            summary = "Disabled owner was masked by legacy/vendor/mock fallback."
        elif not execution.expected_failure_observed:
            severity = Severity.BLOCKER
            code = "disable.no_semantic_effect"
            summary = "Disabling the claimed owner did not cause the expected failure or behavior change."
        else:
            severity = Severity.ERROR
            code = "disable.probe_failed"
            summary = "Disable probe failed."
        return [
            Finding(
                code=code,
                severity=severity,
                summary=summary,
                capability=execution.capability,
                detail=execution.error_message or execution.error_code,
                location=execution.probe_id,
                metadata={"difference": dict(execution.difference)},
            )
        ]

    def _order(self, selected: set[str]) -> tuple[list[str], tuple[str, ...]]:
        temporary: set[str] = set()
        permanent: set[str] = set()
        order: list[str] = []
        stack: list[str] = []
        cycle: tuple[str, ...] = ()

        def visit(probe_id: str) -> bool:
            nonlocal cycle
            if probe_id in permanent:
                return True
            if probe_id in temporary:
                pivot = stack.index(probe_id) if probe_id in stack else 0
                cycle = tuple(stack[pivot:] + [probe_id])
                return False
            temporary.add(probe_id)
            stack.append(probe_id)
            probe = self._probes[probe_id]
            for dependency in probe.dependencies:
                if dependency in selected and not visit(dependency):
                    return False
            stack.pop()
            temporary.remove(probe_id)
            permanent.add(probe_id)
            order.append(probe_id)
            return True

        for probe_id in sorted(selected):
            if not visit(probe_id):
                return [], cycle
        return order, ()


class FileFlagDisableProbe:
    """A real process-shared disable switch for components that already consume a flag file."""

    def __init__(
        self,
        *,
        probe_id: str,
        capability: str,
        flag_path: str | Path,
        exercise: Callable[[], Mapping[str, Any]],
        expected_error_codes: Sequence[str],
        disabled_payload: Mapping[str, Any] | None = None,
        dependencies: Sequence[str] = (),
        timeout_seconds: float = 30.0,
    ) -> None:
        self.probe_id = probe_id
        self.capability = capability
        self.flag_path = Path(flag_path).resolve()
        self.exercise_fn = exercise
        self.expected_error_codes = tuple(expected_error_codes)
        self.dependencies = tuple(dependencies)
        self.timeout_seconds = timeout_seconds
        self.allow_success_with_difference = False
        self.disabled_payload = dict(disabled_payload or {"enabled": False, "reason": "m1-disable-probe"})

    def capture(self) -> Mapping[str, Any]:
        existed = self.flag_path.exists()
        content = self.flag_path.read_text(encoding="utf-8") if existed else ""
        return {"existed": existed, "content": content, "path": str(self.flag_path)}

    def exercise(self) -> Mapping[str, Any]:
        return dict(self.exercise_fn())

    def disable(self) -> Mapping[str, Any]:
        self.flag_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.flag_path.with_name(f".{self.flag_path.name}.{time.time_ns()}.tmp")
        temporary.write_text(json.dumps(self.disabled_payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self.flag_path)
        return {"ok": True, "path": str(self.flag_path), "payload": self.disabled_payload}

    def restore(self, captured: Mapping[str, Any]) -> Mapping[str, Any]:
        if captured.get("existed"):
            self.flag_path.write_text(str(captured.get("content") or ""), encoding="utf-8")
        elif self.flag_path.exists():
            self.flag_path.unlink()
        return {"ok": True, "restored": True, "existed": bool(captured.get("existed"))}
