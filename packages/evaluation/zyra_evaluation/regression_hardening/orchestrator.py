from __future__ import annotations

import concurrent.futures
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import ArtifactManifest, SecureArtifactStore
from .clean_state import IsolatedStateFactory
from .contracts import (
    ArtifactDeclaration,
    AssertionSeverity,
    AttemptReceipt,
    CaseExecutionBuffer,
    CaseReceipt,
    CaseSpec,
    CaseStatus,
    ContractError,
    FailureKind,
    ObservationKind,
    SuiteReceipt,
    bounded_text,
    stable_digest,
    utc_now,
)
from .registry import CaseRegistry, RegisteredCase, RegistrySelection


class OrchestrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    request_id: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    input_text: str = ""
    maximum_output_bytes: int = 4 * 1024 * 1024
    accepted_exit_codes: tuple[int, ...] = (0,)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ContractError("process request_id is required")
        if not self.argv or not self.argv[0].strip():
            raise ContractError(f"{self.request_id} argv is empty")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 3_600:
            raise ContractError(f"{self.request_id} timeout is out of range")
        if self.maximum_output_bytes < 1:
            raise ContractError(f"{self.request_id} output budget must be positive")
        if not self.accepted_exit_codes:
            raise ContractError(f"{self.request_id} accepted_exit_codes is empty")


@dataclass(frozen=True, slots=True)
class ProcessReceipt:
    request_id: str
    argv_digest: str
    cwd: str
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    started_at: str
    completed_at: str
    duration_seconds: float
    stdout: str
    stderr: str
    stdout_digest: str
    stderr_digest: str
    output_truncated: bool

    @property
    def passed(self) -> bool:
        return not self.timed_out and not self.cancelled and self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "argv_digest": self.argv_digest,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 6),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "output_truncated": self.output_truncated,
        }


class BoundedProcessRunner:
    """Subprocess runner with explicit environment, kill-tree and output caps."""

    _INHERITED_ENVIRONMENT = (
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "LANG",
        "LC_ALL",
        "PYTHONUTF8",
    )

    def __init__(self, cancellation: threading.Event | None = None) -> None:
        self.cancellation = cancellation or threading.Event()

    def run(self, request: ProcessRequest) -> ProcessReceipt:
        cwd = request.cwd.resolve(strict=False)
        if not cwd.is_dir():
            raise OrchestrationError(f"process cwd is not a directory: {cwd}")
        executable = self._resolve_executable(request.argv[0], cwd)
        argv = (executable, *request.argv[1:])
        environment = self._environment(request.environment)
        started_at = utc_now()
        started = time.monotonic()
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE if request.input_text else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_overflow = threading.Event()
        stderr_overflow = threading.Event()
        readers = (
            threading.Thread(
                target=self._drain,
                args=(
                    process.stdout,
                    stdout_buffer,
                    request.maximum_output_bytes,
                    stdout_overflow,
                ),
                daemon=True,
                name=f"{request.request_id}-stdout",
            ),
            threading.Thread(
                target=self._drain,
                args=(
                    process.stderr,
                    stderr_buffer,
                    request.maximum_output_bytes,
                    stderr_overflow,
                ),
                daemon=True,
                name=f"{request.request_id}-stderr",
            ),
        )
        for reader in readers:
            reader.start()
        if request.input_text and process.stdin:
            try:
                process.stdin.write(request.input_text.encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        timed_out = False
        cancelled = False
        deadline = started + request.timeout_seconds
        while process.poll() is None:
            if self.cancellation.is_set():
                cancelled = True
                self._terminate(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._terminate(process)
                break
            time.sleep(0.02)
        try:
            exit_code = process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._kill(process)
            exit_code = process.wait(timeout=5.0)
        for reader in readers:
            reader.join(timeout=5.0)
        completed = time.monotonic()
        stdout_raw = bytes(stdout_buffer)
        stderr_raw = bytes(stderr_buffer)
        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")
        if exit_code not in request.accepted_exit_codes and not (timed_out or cancelled):
            stderr = bounded_text(
                stderr
                + (
                    "\n"
                    if stderr
                    else ""
                )
                + f"unexpected_exit_code:{exit_code};accepted={request.accepted_exit_codes}",
                maximum=request.maximum_output_bytes,
            )
        return ProcessReceipt(
            request_id=request.request_id,
            argv_digest=stable_digest(request.argv),
            cwd=cwd.as_posix(),
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            started_at=started_at,
            completed_at=utc_now(),
            duration_seconds=completed - started,
            stdout=bounded_text(stdout, maximum=request.maximum_output_bytes),
            stderr=bounded_text(stderr, maximum=request.maximum_output_bytes),
            stdout_digest=stable_digest(stdout_raw.hex()),
            stderr_digest=stable_digest(stderr_raw.hex()),
            output_truncated=stdout_overflow.is_set() or stderr_overflow.is_set(),
        )

    @staticmethod
    def _drain(
        stream: Any,
        buffer: bytearray,
        maximum: int,
        overflow: threading.Event,
    ) -> None:
        if stream is None:
            return
        try:
            while chunk := stream.read(64 * 1024):
                remaining = maximum - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    overflow.set()
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _resolve_executable(value: str, cwd: Path) -> str:
        candidate = Path(value)
        if candidate.is_absolute():
            if not candidate.is_file():
                raise OrchestrationError(f"executable does not exist: {candidate}")
            return str(candidate)
        if len(candidate.parts) > 1:
            resolved = (cwd / candidate).resolve(strict=False)
            if not resolved.is_file():
                raise OrchestrationError(f"relative executable does not exist: {resolved}")
            return str(resolved)
        resolved = shutil.which(value)
        if not resolved:
            raise OrchestrationError(f"executable is not on PATH: {value}")
        return resolved

    def _environment(self, overrides: Mapping[str, str]) -> dict[str, str]:
        environment = {
            name: os.environ[name]
            for name in self._INHERITED_ENVIRONMENT
            if name in os.environ
        }
        for key, value in overrides.items():
            name = str(key)
            if "\x00" in name or "=" in name or not name:
                raise OrchestrationError(f"invalid environment name: {name!r}")
            text = str(value)
            if "\x00" in text:
                raise OrchestrationError(f"environment value contains NUL: {name}")
            environment[name] = text
        environment.setdefault("PYTHONUTF8", "1")
        return environment

    @staticmethod
    def _terminate(process: subprocess.Popen[Any]) -> None:
        BoundedProcessRunner._signal_tree(process, force=False)
        try:
            process.wait(timeout=2.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            BoundedProcessRunner._kill(process)

    @staticmethod
    def _kill(process: subprocess.Popen[Any]) -> None:
        BoundedProcessRunner._signal_tree(process, force=True)
        try:
            if process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def _signal_tree(
        process: subprocess.Popen[Any],
        *,
        force: bool,
    ) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            command = [
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
            ]
            if force:
                command.append("/F")
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            return
        try:
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        except ProcessLookupError:
            return


@dataclass(slots=True)
class CaseContext:
    case_id: str
    spec: CaseSpec
    project_root: Path
    isolation_root: Path
    workspace_root: Path
    state_root: Path
    artifact_store: SecureArtifactStore
    generated_input: Mapping[str, Any]
    dependencies: Mapping[str, CaseReceipt]
    cancellation: threading.Event
    environment: Mapping[str, str]
    attempt: int
    process_runner: BoundedProcessRunner

    def require_active(self) -> None:
        if self.cancellation.is_set():
            raise concurrent.futures.CancelledError(
                f"{self.case_id} cancellation was requested"
            )

    def process(
        self,
        request_id: str,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        input_text: str = "",
        accepted_exit_codes: Sequence[int] = (0,),
    ) -> ProcessReceipt:
        self.require_active()
        return self.process_runner.run(
            ProcessRequest(
                request_id=request_id,
                argv=tuple(str(item) for item in argv),
                cwd=Path(cwd).resolve() if cwd else self.project_root,
                environment={
                    **self.environment,
                    **dict(environment or {}),
                },
                timeout_seconds=timeout_seconds or self.spec.timeout_seconds,
                input_text=input_text,
                accepted_exit_codes=tuple(accepted_exit_codes),
            )
        )


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    project_root: Path
    artifact_root: Path
    temporary_parent: Path | None = None
    retain_failed_isolation: bool = False
    secret_canaries: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        project_root: str | Path,
        artifact_root: str | Path,
        *,
        temporary_parent: str | Path | None = None,
        retain_failed_isolation: bool = False,
        secret_canaries: Iterable[str] = (),
    ) -> "OrchestratorConfig":
        root = Path(project_root).resolve()
        if not (root / ".git").exists():
            raise OrchestrationError(f"project root is not a Git repository: {root}")
        artifacts = Path(artifact_root).resolve(strict=False)
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = (
            Path(temporary_parent).resolve(strict=False)
            if temporary_parent is not None
            else None
        )
        if temporary:
            temporary.mkdir(parents=True, exist_ok=True)
        return cls(
            project_root=root,
            artifact_root=artifacts,
            temporary_parent=temporary,
            retain_failed_isolation=retain_failed_isolation,
            secret_canaries=tuple(secret_canaries),
        )


class RegressionOrchestrator:
    def __init__(
        self,
        registry: CaseRegistry,
        config: OrchestratorConfig,
    ) -> None:
        if not registry.sealed:
            raise OrchestrationError("registry must be sealed")
        self.registry = registry
        self.config = config
        self._cancel = threading.Event()
        self._active_contexts: dict[str, CaseContext] = {}
        self._active_lock = threading.RLock()

    def cancel(self) -> None:
        self._cancel.set()
        with self._active_lock:
            for context in self._active_contexts.values():
                context.cancellation.set()

    def execute(
        self,
        selection: RegistrySelection,
        *,
        suite_id: str = "m3-default-security-regression",
        suite_version: str = "1",
        generated_inputs: Mapping[str, Mapping[str, Any]] | None = None,
        suite_metadata: Mapping[str, Any] | None = None,
    ) -> SuiteReceipt:
        if selection.registry_digest == "":
            raise OrchestrationError("selection has no registry digest")
        revision = self._revision()
        started_at = utc_now()
        started = time.monotonic()
        results: dict[str, CaseReceipt] = {}
        inputs = dict(generated_inputs or {})
        layers = self.registry.dependency_layers(selection)
        for layer in layers:
            if self._cancel.is_set():
                break
            runnable: list[RegisteredCase] = []
            for item in layer:
                if item.spec.case_id not in selection.ordered_case_ids:
                    continue
                blockers = [
                    dependency
                    for dependency in item.spec.dependencies
                    if dependency not in results or not results[dependency].passed
                ]
                missing = selection.missing_capabilities.get(item.spec.case_id, ())
                if blockers or missing:
                    results[item.spec.case_id] = self._blocked_receipt(
                        item,
                        selection,
                        blockers=blockers,
                        missing_capabilities=missing,
                        generated_input=inputs.get(item.spec.case_id, {}),
                    )
                else:
                    runnable.append(item)
            if not runnable:
                continue
            layer_results = self._run_layer(
                runnable,
                selection,
                results,
                inputs,
            )
            results.update(layer_results)
            if (
                selection.profile.fail_fast
                and any(not item.passed for item in layer_results.values())
            ):
                self._cancel.set()
                break
        for case_id in selection.ordered_case_ids:
            if case_id in results:
                continue
            item = selection.case(case_id)
            results[case_id] = self._cancelled_receipt(
                item,
                selection,
                generated_input=inputs.get(case_id, {}),
            )
        ordered_results = tuple(
            results[case_id]
            for case_id in selection.ordered_case_ids
        )
        completed = time.monotonic()
        metadata = {
            "shard_ids": list(selection.shard_ids),
            "maximum_workers": selection.profile.maximum_workers,
            "case_count": len(ordered_results),
            "dependency_order": list(selection.ordered_case_ids),
        }
        protected_metadata = set(metadata)
        supplied_metadata = dict(suite_metadata or {})
        overlap = protected_metadata.intersection(supplied_metadata)
        if overlap:
            raise OrchestrationError(
                f"suite metadata cannot replace orchestrator fields: {sorted(overlap)}"
            )
        metadata.update(supplied_metadata)
        receipt = SuiteReceipt(
            suite_id=suite_id,
            suite_version=suite_version,
            profile=selection.profile,
            revision=revision,
            started_at=started_at,
            completed_at=utc_now(),
            duration_seconds=completed - started,
            cases=ordered_results,
            registry_digest=selection.registry_digest,
            artifact_root=self.config.artifact_root.as_posix(),
            cancelled=self._cancel.is_set() and any(
                item.status is CaseStatus.CANCELLED
                for item in ordered_results
            ),
            metadata=metadata,
        )
        self._write_suite_receipt(receipt)
        return receipt

    def _run_layer(
        self,
        cases: Sequence[RegisteredCase],
        selection: RegistrySelection,
        completed: Mapping[str, CaseReceipt],
        generated_inputs: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, CaseReceipt]:
        workers = min(selection.profile.maximum_workers, len(cases))
        resources: set[str] = set()
        parallel: list[RegisteredCase] = []
        serial: list[RegisteredCase] = []
        for item in cases:
            claimed = set(item.spec.exclusive_resources)
            if claimed.intersection(resources):
                serial.append(item)
            else:
                resources.update(claimed)
                parallel.append(item)
        results: dict[str, CaseReceipt] = {}
        if parallel:
            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, min(workers, len(parallel))),
                thread_name_prefix="zyra-m3-regression",
            )
            futures = {
                pool.submit(
                    self._run_case,
                    item,
                    selection,
                    completed,
                    generated_inputs.get(item.spec.case_id, {}),
                ): item
                for item in parallel
            }
            try:
                for future in concurrent.futures.as_completed(futures):
                    item = futures[future]
                    try:
                        results[item.spec.case_id] = future.result()
                    except BaseException as error:
                        results[item.spec.case_id] = self._internal_failure_receipt(
                            item,
                            selection,
                            generated_inputs.get(item.spec.case_id, {}),
                            error,
                        )
                    if selection.profile.fail_fast and not results[item.spec.case_id].passed:
                        self._cancel.set()
                        for pending in futures:
                            pending.cancel()
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        for item in serial:
            if self._cancel.is_set():
                results[item.spec.case_id] = self._cancelled_receipt(
                    item,
                    selection,
                    generated_input=generated_inputs.get(item.spec.case_id, {}),
                )
                continue
            results[item.spec.case_id] = self._run_case(
                item,
                selection,
                {**completed, **results},
                generated_inputs.get(item.spec.case_id, {}),
            )
        return results

    def _run_case(
        self,
        registered: RegisteredCase,
        selection: RegistrySelection,
        completed: Mapping[str, CaseReceipt],
        generated_input: Mapping[str, Any],
    ) -> CaseReceipt:
        spec = registered.spec
        shard_id = selection.shard_assignments[spec.case_id]
        dependency_receipts = {
            dependency: completed[dependency].digest
            for dependency in spec.dependencies
            if dependency in completed
        }
        attempts: list[AttemptReceipt] = []
        for attempt in range(1, spec.maximum_attempts + 1):
            if self._cancel.is_set():
                attempts.append(
                    self._status_attempt(
                        attempt,
                        CaseStatus.CANCELLED,
                        "suite cancellation requested",
                        FailureKind.DEPENDENCY,
                    )
                )
                break
            isolation = IsolatedStateFactory(self.config.temporary_parent)
            root = isolation.create(prefix=f"zyra-m3-regression-{spec.case_id}-")
            artifact_store = SecureArtifactStore(
                self.config.artifact_root,
                spec.case_id,
                spec.artifacts,
                secret_canaries=self.config.secret_canaries,
            )
            case_cancellation = threading.Event()
            if self._cancel.is_set():
                case_cancellation.set()
            context = CaseContext(
                case_id=spec.case_id,
                spec=spec,
                project_root=self.config.project_root,
                isolation_root=root,
                workspace_root=root / "workspace",
                state_root=root,
                artifact_store=artifact_store,
                generated_input=dict(generated_input),
                dependencies={
                    dependency: completed[dependency]
                    for dependency in spec.dependencies
                    if dependency in completed
                },
                cancellation=case_cancellation,
                environment=isolation.environment(),
                attempt=attempt,
                process_runner=BoundedProcessRunner(case_cancellation),
            )
            with self._active_lock:
                self._active_contexts[spec.case_id] = context
            receipt = self._execute_attempt(registered, context)
            attempts.append(receipt)
            with self._active_lock:
                self._active_contexts.pop(spec.case_id, None)
            keep = self.config.retain_failed_isolation and not receipt.passed
            if not keep:
                isolation.cleanup()
            if receipt.passed:
                break
            if not self._retryable(receipt, selection):
                break
        return CaseReceipt(
            spec=spec,
            shard_id=shard_id,
            input_digest=stable_digest(generated_input),
            isolation_id=stable_digest(spec.case_id, attempts[0].started_at),
            attempts=tuple(attempts),
            dependency_receipts=dependency_receipts,
        )

    def _execute_attempt(
        self,
        registered: RegisteredCase,
        context: CaseContext,
    ) -> AttemptReceipt:
        started_at = utc_now()
        started = time.monotonic()
        failure_message = ""
        failure_kind: FailureKind | None = None
        buffer = CaseExecutionBuffer(started_monotonic=started)
        manifest: ArtifactManifest | None = None
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"case-{context.case_id}",
        )
        future = pool.submit(registered.executor, context)
        try:
            value = future.result(timeout=context.spec.timeout_seconds)
            if isinstance(value, CaseExecutionBuffer):
                buffer = value
            elif isinstance(value, Mapping):
                self._merge_mapping_result(buffer, value)
            elif value is not None:
                raise ContractError(
                    f"{context.case_id} executor returned unsupported "
                    f"{type(value).__name__}"
                )
            manifest = context.artifact_store.finalize()
            manifest.require_valid()
            self._assert_required_semantics(buffer, context.spec)
            status = (
                CaseStatus.PASSED
                if buffer.assertions and all(item.passed for item in buffer.assertions)
                else CaseStatus.FAILED
            )
            if status is CaseStatus.FAILED:
                failure_kind = self._assertion_failure_kind(buffer)
                failure_message = "one or more case assertions failed"
        except concurrent.futures.TimeoutError:
            context.cancellation.set()
            future.cancel()
            status = CaseStatus.TIMED_OUT
            failure_kind = FailureKind.TIMEOUT
            failure_message = (
                f"case exceeded timeout of {context.spec.timeout_seconds} seconds"
            )
            buffer.assert_that(
                "orchestrator.timeout",
                False,
                failure_message,
                failure_kind=FailureKind.TIMEOUT,
            )
        except concurrent.futures.CancelledError as error:
            status = CaseStatus.CANCELLED
            failure_kind = FailureKind.DEPENDENCY
            failure_message = str(error)
        except BaseException as error:
            status = CaseStatus.FAILED
            failure_kind = self._classify_exception(error)
            failure_message = bounded_text(
                f"{type(error).__name__}: {error}\n"
                f"{''.join(traceback.format_exception(error))}",
                maximum=8_000,
            )
            buffer.assert_that(
                "orchestrator.exception",
                False,
                f"case raised {type(error).__name__}",
                failure_kind=failure_kind,
                details={"error": str(error)},
            )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        completed = time.monotonic()
        artifacts = manifest.receipts if manifest else ()
        return AttemptReceipt(
            attempt=context.attempt,
            status=status,
            started_at=started_at,
            completed_at=utc_now(),
            duration_seconds=completed - started,
            observations=tuple(buffer.observations),
            assertions=tuple(buffer.assertions),
            artifacts=tuple(artifacts),
            failure_message=failure_message,
            failure_kind=failure_kind,
        )

    @staticmethod
    def _merge_mapping_result(
        buffer: CaseExecutionBuffer,
        value: Mapping[str, Any],
    ) -> None:
        for index, raw in enumerate(value.get("observations") or ()):
            if not isinstance(raw, Mapping):
                raise ContractError("mapping observation must be an object")
            buffer.observe(
                str(raw.get("observation_id") or f"mapped.observation-{index + 1}"),
                ObservationKind(str(raw.get("kind") or "event")),
                str(raw.get("subject") or "mapped"),
                str(raw.get("status") or "observed"),
                attributes=raw.get("attributes") if isinstance(raw.get("attributes"), Mapping) else {},
                correlation_id=str(raw.get("correlation_id") or ""),
                causation_id=str(raw.get("causation_id") or ""),
                run_id=str(raw.get("run_id") or ""),
            )
        for index, raw in enumerate(value.get("assertions") or ()):
            if not isinstance(raw, Mapping):
                raise ContractError("mapping assertion must be an object")
            buffer.assert_that(
                str(raw.get("assertion_id") or f"mapped.assertion-{index + 1}"),
                bool(raw.get("passed")),
                str(raw.get("summary") or "mapped assertion"),
                severity=AssertionSeverity(str(raw.get("severity") or "blocker")),
                evidence=tuple(str(item) for item in raw.get("evidence_ids") or ()),
                failure_kind=FailureKind(str(raw.get("failure_kind") or "assertion")),
                details=raw.get("details") if isinstance(raw.get("details"), Mapping) else {},
            )

    @staticmethod
    def _assert_required_semantics(
        buffer: CaseExecutionBuffer,
        spec: CaseSpec,
    ) -> None:
        kinds = {item.kind for item in buffer.observations}
        statuses = {item.status.casefold() for item in buffer.observations}
        if spec.mutation_required and ObservationKind.MUTATION not in kinds:
            buffer.assert_that(
                "orchestrator.mutation-observation",
                False,
                "case declared mutation_required but produced no mutation observation",
                failure_kind=FailureKind.CONTRACT,
            )
        if spec.disable_required and not any("disable" in status for status in statuses):
            buffer.assert_that(
                "orchestrator.disable-observation",
                False,
                "case declared disable_required but produced no disable outcome",
                failure_kind=FailureKind.CONTRACT,
            )
        if spec.clean_state_required and ObservationKind.CLEAN_STATE not in kinds:
            buffer.assert_that(
                "orchestrator.clean-state-observation",
                False,
                "case declared clean_state_required but produced no clean-state observation",
                failure_kind=FailureKind.CONTRACT,
            )
        if spec.security_required and ObservationKind.SECURITY not in kinds:
            buffer.assert_that(
                "orchestrator.security-observation",
                False,
                "case declared security_required but produced no security observation",
                failure_kind=FailureKind.CONTRACT,
            )

    @staticmethod
    def _assertion_failure_kind(buffer: CaseExecutionBuffer) -> FailureKind:
        priority = (
            FailureKind.SECURITY,
            FailureKind.POLLUTION,
            FailureKind.PROCESS,
            FailureKind.CONTRACT,
            FailureKind.ASSERTION,
        )
        failures = {
            item.failure_kind
            for item in buffer.assertions
            if not item.passed
        }
        return next((item for item in priority if item in failures), FailureKind.ASSERTION)

    @staticmethod
    def _classify_exception(error: BaseException) -> FailureKind:
        name = type(error).__name__.casefold()
        text = str(error).casefold()
        if "security" in name or "escape" in text or "secret" in text:
            return FailureKind.SECURITY
        if "pollution" in name or "clean" in name:
            return FailureKind.POLLUTION
        if isinstance(error, (subprocess.SubprocessError, OSError)):
            return FailureKind.PROCESS
        if isinstance(error, (ContractError, ValueError, TypeError)):
            return FailureKind.CONTRACT
        return FailureKind.INTERNAL

    @staticmethod
    def _retryable(
        receipt: AttemptReceipt,
        selection: RegistrySelection,
    ) -> bool:
        if not selection.profile.retry_transient:
            return False
        return receipt.failure_kind in {
            FailureKind.TIMEOUT,
            FailureKind.PROCESS,
            FailureKind.INTERNAL,
        }

    def _blocked_receipt(
        self,
        registered: RegisteredCase,
        selection: RegistrySelection,
        *,
        blockers: Sequence[str],
        missing_capabilities: Sequence[str],
        generated_input: Mapping[str, Any],
    ) -> CaseReceipt:
        buffer = CaseExecutionBuffer()
        buffer.assert_that(
            "orchestrator.dependencies",
            False,
            "case dependencies or capabilities are unavailable",
            failure_kind=FailureKind.DEPENDENCY,
            details={
                "failed_dependencies": list(blockers),
                "missing_capabilities": list(missing_capabilities),
            },
        )
        attempt = AttemptReceipt(
            attempt=1,
            status=CaseStatus.BLOCKED,
            started_at=utc_now(),
            completed_at=utc_now(),
            duration_seconds=0,
            observations=(),
            assertions=tuple(buffer.assertions),
            failure_message="dependency/capability admission failed",
            failure_kind=FailureKind.DEPENDENCY,
        )
        return CaseReceipt(
            spec=registered.spec,
            shard_id=selection.shard_assignments[registered.spec.case_id],
            input_digest=stable_digest(generated_input),
            isolation_id=stable_digest("blocked", registered.spec.case_id),
            attempts=(attempt,),
            dependency_receipts={},
        )

    def _cancelled_receipt(
        self,
        registered: RegisteredCase,
        selection: RegistrySelection,
        *,
        generated_input: Mapping[str, Any],
    ) -> CaseReceipt:
        attempt = self._status_attempt(
            1,
            CaseStatus.CANCELLED,
            "suite cancelled before case execution",
            FailureKind.DEPENDENCY,
        )
        return CaseReceipt(
            spec=registered.spec,
            shard_id=selection.shard_assignments[registered.spec.case_id],
            input_digest=stable_digest(generated_input),
            isolation_id=stable_digest("cancelled", registered.spec.case_id),
            attempts=(attempt,),
            dependency_receipts={},
        )

    def _internal_failure_receipt(
        self,
        registered: RegisteredCase,
        selection: RegistrySelection,
        generated_input: Mapping[str, Any],
        error: BaseException,
    ) -> CaseReceipt:
        attempt = self._status_attempt(
            1,
            CaseStatus.FAILED,
            f"orchestrator failure: {type(error).__name__}: {error}",
            FailureKind.INTERNAL,
        )
        return CaseReceipt(
            spec=registered.spec,
            shard_id=selection.shard_assignments[registered.spec.case_id],
            input_digest=stable_digest(generated_input),
            isolation_id=stable_digest("internal", registered.spec.case_id),
            attempts=(attempt,),
            dependency_receipts={},
        )

    @staticmethod
    def _status_attempt(
        attempt: int,
        status: CaseStatus,
        message: str,
        failure_kind: FailureKind,
    ) -> AttemptReceipt:
        buffer = CaseExecutionBuffer()
        buffer.assert_that(
            "orchestrator.status",
            False,
            message,
            failure_kind=failure_kind,
        )
        return AttemptReceipt(
            attempt=attempt,
            status=status,
            started_at=utc_now(),
            completed_at=utc_now(),
            duration_seconds=0,
            observations=(),
            assertions=tuple(buffer.assertions),
            failure_message=message,
            failure_kind=failure_kind,
        )

    def _revision(self) -> str:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={self.config.project_root.as_posix()}",
                "rev-parse",
                "HEAD",
            ],
            cwd=self.config.project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        revision = completed.stdout.strip().casefold()
        if completed.returncode or len(revision) != 40:
            raise OrchestrationError(
                f"cannot resolve exact Git revision: {completed.stderr.strip()}"
            )
        return revision

    def _write_suite_receipt(self, receipt: SuiteReceipt) -> None:
        store = SecureArtifactStore(
            self.config.artifact_root,
            "suite",
            (
                # The store is used here to inherit atomic write and canary scan.
                # The declaration remains behavior-bearing because admission
                # verifies it before the freeze gate consumes the file.
                ArtifactDeclaration(
                    name="suite-receipt",
                    relative_path="suite-receipt.json",
                    media_type="application/json",
                    maximum_bytes=64 * 1024 * 1024,
                ),
            ),
            secret_canaries=self.config.secret_canaries,
        )
        # Suite fields are already public, bounded projections.  Rewriting the
        # serialized receipt after its digest is computed would invalidate the
        # freeze proof (and entropy heuristics can mistake long evidence paths
        # for credentials).  The store still performs exact canary rejection.
        store.write_json(
            "suite-receipt",
            receipt.to_dict(),
            redact=False,
        )
        store.finalize().require_valid()


__all__ = [
    "BoundedProcessRunner",
    "CaseContext",
    "OrchestrationError",
    "OrchestratorConfig",
    "ProcessReceipt",
    "ProcessRequest",
    "RegressionOrchestrator",
]
