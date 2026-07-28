from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cleanroom import CommandRunner
from .errors import GateFailure, ReleaseError
from .integrity import sha256_file, stable_digest
from .models import GateReceipt, GateState
from .policy import DEFAULT_RELEASE_POLICY, ReleasePolicy


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class PythonTestPolicy:
    SCHEMA = "zyra.release-python-test-policy/v1"

    roots: tuple[str, ...]
    ignore_files: tuple[str, ...]
    deselect_nodeids: tuple[str, ...]
    debt_owner: str
    reason: str
    digest: str

    @classmethod
    def load(
        cls,
        project_root: Path,
        path: Path,
    ) -> "PythonTestPolicy":
        project_root = project_root.resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise GateFailure(
                "Release Python test policy is missing or invalid.",
                code="python_test_policy_invalid",
                details={"path": str(path), "error": str(error)},
            ) from error
        if not isinstance(payload, Mapping):
            raise GateFailure(
                "Release Python test policy must be an object.",
                code="python_test_policy_invalid",
            )
        schema = str(payload.get("schema") or "").strip()
        if schema != cls.SCHEMA:
            raise GateFailure(
                "Release Python test policy schema is unsupported.",
                code="python_test_policy_schema_unsupported",
                details={"expected": cls.SCHEMA, "actual": schema},
            )
        roots = cls._paths(payload.get("roots"), field_name="roots")
        ignored = cls._paths(
            payload.get("ignore_files"),
            field_name="ignore_files",
        )
        nodeids = cls._strings(
            payload.get("deselect_nodeids"),
            field_name="deselect_nodeids",
        )
        debt_owner = str(payload.get("debt_owner") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if not roots or not debt_owner or len(reason) < 24:
            raise GateFailure(
                "Release Python test policy lacks roots or debt ownership.",
                code="python_test_policy_incomplete",
            )
        for relative in (*roots, *ignored):
            candidate = project_root.joinpath(
                *relative.replace("\\", "/").split("/")
            ).resolve()
            try:
                candidate.relative_to(project_root)
            except ValueError as error:
                raise GateFailure(
                    "Release Python test path escapes the project.",
                    code="python_test_policy_path_escape",
                    details={"path": relative},
                ) from error
            if not candidate.exists():
                raise GateFailure(
                    "Release Python test policy references a missing path.",
                    code="python_test_policy_path_missing",
                    details={"path": relative},
                )
        for nodeid in nodeids:
            file_part = nodeid.split("::", 1)[0]
            if "::" not in nodeid or not (project_root / file_part).is_file():
                raise GateFailure(
                    "Release Python test policy has an invalid node id.",
                    code="python_test_policy_nodeid_invalid",
                    details={"nodeid": nodeid},
                )
        normalized = {
            "schema": schema,
            "roots": list(roots),
            "ignore_files": list(ignored),
            "deselect_nodeids": list(nodeids),
            "debt_owner": debt_owner,
            "reason": reason,
        }
        return cls(
            roots=roots,
            ignore_files=ignored,
            deselect_nodeids=nodeids,
            debt_owner=debt_owner,
            reason=reason,
            digest=stable_digest(normalized),
        )

    @staticmethod
    def _strings(value: Any, *, field_name: str) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise GateFailure(
                "Release Python test policy field must be an array.",
                code="python_test_policy_invalid",
                details={"field": field_name},
            )
        output = tuple(str(item).strip() for item in value)
        if any(not item for item in output) or len(set(output)) != len(output):
            raise GateFailure(
                "Release Python test policy contains empty or duplicate values.",
                code="python_test_policy_invalid",
                details={"field": field_name},
            )
        return output

    @classmethod
    def _paths(cls, value: Any, *, field_name: str) -> tuple[str, ...]:
        output = cls._strings(value, field_name=field_name)
        for item in output:
            normalized = item.replace("\\", "/")
            if (
                normalized.startswith("/")
                or ":" in normalized.split("/", 1)[0]
                or ".." in normalized.split("/")
            ):
                raise GateFailure(
                    "Release Python test policy path is unsafe.",
                    code="python_test_policy_path_escape",
                    details={"field": field_name, "path": item},
                )
        return output

    def pytest_arguments(self) -> tuple[str, ...]:
        arguments: list[str] = []
        arguments.extend(f"--ignore={item}" for item in self.ignore_files)
        arguments.extend(
            f"--deselect={item}" for item in self.deselect_nodeids
        )
        arguments.extend(self.roots)
        return tuple(arguments)


@dataclass(frozen=True, slots=True)
class GateSpec:
    gate_id: str
    command: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    timeout_seconds: float = 600.0
    required: bool = True
    allow_parallel: bool = True
    environment: dict[str, str] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    callable: Callable[["GateContext"], Mapping[str, Any]] | None = None

    def validate(self) -> None:
        if not self.gate_id or any(character.isspace() for character in self.gate_id):
            raise GateFailure(
                "CI gate id is invalid.",
                code="ci_gate_id_invalid",
                details={"gate_id": self.gate_id},
            )
        if bool(self.command) == bool(self.callable):
            raise GateFailure(
                "CI gate must define exactly one command or callable.",
                code="ci_gate_action_invalid",
                details={"gate_id": self.gate_id},
            )
        if self.timeout_seconds <= 0:
            raise GateFailure(
                "CI gate timeout must be positive.",
                code="ci_gate_timeout_invalid",
                details={"gate_id": self.gate_id},
            )
        if self.gate_id in self.dependencies:
            raise GateFailure(
                "CI gate cannot depend on itself.",
                code="ci_gate_self_dependency",
                details={"gate_id": self.gate_id},
            )


@dataclass(frozen=True, slots=True)
class GateContext:
    gate_id: str
    project_root: Path
    output_root: Path
    source_commit: str
    environment: Mapping[str, str]
    dependency_receipts: Mapping[str, GateReceipt]

    def artifact_path(self, relative: str) -> Path:
        normalized = relative.replace("\\", "/").strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise GateFailure(
                "Gate artifact path is unsafe.",
                code="ci_artifact_path_unsafe",
                details={"gate_id": self.gate_id, "path": relative},
            )
        path = self.output_root.joinpath(*normalized.split("/")).resolve()
        try:
            path.relative_to(self.output_root.resolve())
        except ValueError as error:
            raise GateFailure(
                "Gate artifact path escapes output root.",
                code="ci_artifact_path_escape",
                details={"gate_id": self.gate_id, "path": relative},
            ) from error
        return path


class GateRegistry:
    def __init__(self, specs: Iterable[GateSpec] = ()) -> None:
        self._specs: dict[str, GateSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: GateSpec) -> None:
        spec.validate()
        if spec.gate_id in self._specs:
            raise GateFailure(
                "CI gate is registered more than once.",
                code="ci_gate_duplicate",
                details={"gate_id": spec.gate_id},
            )
        self._specs[spec.gate_id] = spec

    def get(self, gate_id: str) -> GateSpec:
        try:
            return self._specs[gate_id]
        except KeyError as error:
            raise GateFailure(
                "CI gate is not registered.",
                code="ci_gate_missing",
                details={"gate_id": gate_id},
            ) from error

    def specs(self) -> tuple[GateSpec, ...]:
        return tuple(self._specs.values())

    def validate(self) -> tuple[str, ...]:
        for spec in self._specs.values():
            missing = [
                dependency
                for dependency in spec.dependencies
                if dependency not in self._specs
            ]
            if missing:
                raise GateFailure(
                    "CI gate dependency is not registered.",
                    code="ci_gate_dependency_missing",
                    details={"gate_id": spec.gate_id, "missing": missing},
                )
        indegree = {gate_id: 0 for gate_id in self._specs}
        outgoing: dict[str, set[str]] = defaultdict(set)
        for spec in self._specs.values():
            for dependency in spec.dependencies:
                if spec.gate_id not in outgoing[dependency]:
                    outgoing[dependency].add(spec.gate_id)
                    indegree[spec.gate_id] += 1
        queue = deque(sorted(gate_id for gate_id, degree in indegree.items() if degree == 0))
        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for target in sorted(outgoing[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(order) != len(self._specs):
            cycle = sorted(
                gate_id for gate_id, degree in indegree.items() if degree > 0
            )
            raise GateFailure(
                "CI gate dependency graph contains a cycle.",
                code="ci_gate_dependency_cycle",
                details={"gates": cycle},
            )
        return tuple(order)


class GateExecutor:
    def __init__(
        self,
        registry: GateRegistry,
        *,
        project_root: Path,
        output_root: Path,
        source_commit: str,
        environment: Mapping[str, str],
        maximum_parallel: int = 2,
    ) -> None:
        self.registry = registry
        self.project_root = project_root.resolve()
        self.output_root = output_root.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.source_commit = source_commit
        self.environment = dict(environment)
        self.maximum_parallel = max(1, maximum_parallel)
        self._receipts: dict[str, GateReceipt] = {}
        self._details: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def execute(self) -> dict[str, Any]:
        order = self.registry.validate()
        pending = set(order)
        running: dict[concurrent.futures.Future[tuple[GateReceipt, dict[str, Any]]], str] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.maximum_parallel,
            thread_name_prefix="zyra-release-gate",
        ) as pool:
            while pending or running:
                progress = False
                exclusive_running = any(
                    not self.registry.get(gate_id).allow_parallel
                    for gate_id in running.values()
                )
                for gate_id in order:
                    if gate_id not in pending:
                        continue
                    if exclusive_running:
                        break
                    spec = self.registry.get(gate_id)
                    dependency_states = {
                        dependency: self._receipts[dependency].state
                        for dependency in spec.dependencies
                        if dependency in self._receipts
                    }
                    if len(dependency_states) != len(spec.dependencies):
                        continue
                    failed_dependencies = [
                        dependency
                        for dependency, state in dependency_states.items()
                        if state is not GateState.PASSED
                    ]
                    if failed_dependencies:
                        receipt = self._blocked_receipt(
                            spec,
                            failed_dependencies,
                        )
                        self._receipts[gate_id] = receipt
                        self._details[gate_id] = {
                            "blocked_by": failed_dependencies,
                        }
                        pending.remove(gate_id)
                        progress = True
                        continue
                    if not spec.allow_parallel and running:
                        continue
                    if len(running) >= self.maximum_parallel:
                        break
                    future = pool.submit(self._execute_one, spec)
                    running[future] = gate_id
                    pending.remove(gate_id)
                    progress = True
                    if not spec.allow_parallel:
                        break
                if running:
                    done, _ = concurrent.futures.wait(
                        running,
                        timeout=0.1,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        gate_id = running.pop(future)
                        try:
                            receipt, details = future.result()
                        except BaseException as error:
                            spec = self.registry.get(gate_id)
                            receipt, details = self._exception_receipt(spec, error)
                        with self._lock:
                            self._receipts[gate_id] = receipt
                            self._details[gate_id] = details
                        progress = True
                if not progress and pending and not running:
                    raise GateFailure(
                        "CI gate scheduler reached an impossible state.",
                        code="ci_gate_scheduler_stalled",
                        details={"pending": sorted(pending)},
                    )
        receipts = [self._receipts[gate_id] for gate_id in order]
        required_failures = [
            receipt.gate_id
            for receipt in receipts
            if receipt.required and receipt.state is not GateState.PASSED
        ]
        report = {
            "schema": "zyra.release-ci-report/v1",
            "source_commit": self.source_commit,
            "ready": not required_failures,
            "gate_count": len(receipts),
            "passed_count": sum(
                receipt.state is GateState.PASSED for receipt in receipts
            ),
            "failed_count": sum(
                receipt.state is GateState.FAILED for receipt in receipts
            ),
            "blocked_count": sum(
                receipt.state is GateState.BLOCKED for receipt in receipts
            ),
            "required_failures": required_failures,
            "receipts": [receipt.to_dict() for receipt in receipts],
            "details": self._details,
        }
        report["digest"] = stable_digest(report)
        return report

    def _execute_one(
        self,
        spec: GateSpec,
    ) -> tuple[GateReceipt, dict[str, Any]]:
        started_at = utc_now()
        started = time.monotonic()
        environment = dict(self.environment)
        environment.update(spec.environment)
        context = GateContext(
            gate_id=spec.gate_id,
            project_root=self.project_root,
            output_root=self.output_root,
            source_commit=self.source_commit,
            environment=environment,
            dependency_receipts={
                dependency: self._receipts[dependency]
                for dependency in spec.dependencies
            },
        )
        if spec.callable is not None:
            try:
                details = dict(spec.callable(context))
                ready = details.get("ready") is True
                exit_code = 0 if ready else 1
                stdout = json.dumps(details, ensure_ascii=False, sort_keys=True)
                stderr = ""
            except BaseException as error:
                details = {
                    "ready": False,
                    "error": str(error),
                    "type": type(error).__name__,
                }
                if isinstance(error, ReleaseError):
                    details.update(
                        {
                            "code": error.code,
                            "retryable": error.retryable,
                            "error_details": error.details,
                        }
                    )
                ready = False
                exit_code = 1
                stdout = ""
                stderr = json.dumps(details, ensure_ascii=False, sort_keys=True)
        else:
            runner = CommandRunner(environment=environment)
            result = runner.run(
                spec.command,
                cwd=self.project_root,
                timeout=spec.timeout_seconds,
                check=False,
            )
            details = result.to_dict()
            ready = result.ready
            exit_code = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        # Preserve the originating gate failure when a callable aborts before
        # it can write its success artifact.  Admission still fails closed, but
        # the diagnostic remains actionable instead of being replaced by the
        # secondary "artifact missing" symptom.
        artifacts = self._collect_artifacts(spec) if ready else []
        state = GateState.PASSED if ready else GateState.FAILED
        receipt = GateReceipt(
            gate_id=spec.gate_id,
            state=state,
            required=spec.required,
            started_at=started_at,
            finished_at=utc_now(),
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            command=spec.command,
            exit_code=exit_code,
            stdout_digest=stable_digest(stdout),
            stderr_digest=stable_digest(stderr),
            artifacts=tuple(item["path"] for item in artifacts),
            reason="" if ready else str(details.get("error") or "gate_failed"),
        )
        details["artifacts"] = artifacts
        return receipt, details

    def _collect_artifacts(self, spec: GateSpec) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for relative in spec.artifacts:
            path = self.output_root.joinpath(*relative.replace("\\", "/").split("/")).resolve()
            try:
                path.relative_to(self.output_root)
            except ValueError as error:
                raise GateFailure(
                    "CI gate artifact escapes output root.",
                    code="ci_gate_artifact_escape",
                    details={"gate_id": spec.gate_id, "artifact": relative},
                ) from error
            if not path.is_file():
                raise GateFailure(
                    "CI gate did not produce a declared artifact.",
                    code="ci_gate_artifact_missing",
                    details={"gate_id": spec.gate_id, "artifact": relative},
                )
            output.append(
                {
                    "path": relative.replace("\\", "/"),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        return output

    @staticmethod
    def _blocked_receipt(
        spec: GateSpec,
        dependencies: Sequence[str],
    ) -> GateReceipt:
        now = utc_now()
        return GateReceipt(
            gate_id=spec.gate_id,
            state=GateState.BLOCKED,
            required=spec.required,
            started_at=now,
            finished_at=now,
            duration_ms=0,
            command=spec.command,
            exit_code=None,
            stdout_digest=stable_digest(""),
            stderr_digest=stable_digest(""),
            artifacts=(),
            reason="blocked_by:" + ",".join(dependencies),
        )

    @staticmethod
    def _exception_receipt(
        spec: GateSpec,
        error: BaseException,
    ) -> tuple[GateReceipt, dict[str, Any]]:
        now = utc_now()
        details = {
            "ready": False,
            "type": type(error).__name__,
            "error": str(error),
        }
        return (
            GateReceipt(
                gate_id=spec.gate_id,
                state=GateState.FAILED,
                required=spec.required,
                started_at=now,
                finished_at=now,
                duration_ms=0,
                command=spec.command,
                exit_code=1,
                stdout_digest=stable_digest(""),
                stderr_digest=stable_digest(json.dumps(details, sort_keys=True)),
                artifacts=(),
                reason=str(error),
            ),
            details,
        )


class ReleaseAdmission:
    def __init__(
        self,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.policy = policy

    def verify(
        self,
        report: Mapping[str, Any],
        *,
        expected_commit: str,
    ) -> dict[str, Any]:
        if report.get("schema") != "zyra.release-ci-report/v1":
            raise GateFailure(
                "Release CI report schema is unsupported.",
                code="release_admission_schema",
            )
        if report.get("source_commit") != expected_commit:
            raise GateFailure(
                "Release CI report targets a different commit.",
                code="release_admission_commit",
                details={
                    "expected": expected_commit,
                    "actual": report.get("source_commit"),
                },
            )
        raw_receipts = report.get("receipts")
        if not isinstance(raw_receipts, Sequence) or isinstance(
            raw_receipts, (str, bytes)
        ):
            raise GateFailure(
                "Release CI report receipts are invalid.",
                code="release_admission_receipts",
            )
        receipts: dict[str, Mapping[str, Any]] = {}
        duplicates: list[str] = []
        for raw in raw_receipts:
            if not isinstance(raw, Mapping):
                raise GateFailure(
                    "Release CI gate receipt is invalid.",
                    code="release_admission_receipt_invalid",
                )
            gate_id = str(raw.get("gate_id") or "")
            if gate_id in receipts:
                duplicates.append(gate_id)
            receipts[gate_id] = raw
        missing = sorted(self.policy.mandatory_gates - set(receipts))
        failed = sorted(
            gate_id
            for gate_id in self.policy.mandatory_gates
            if gate_id in receipts and receipts[gate_id].get("state") != "passed"
        )
        optional_required_failures = sorted(
            gate_id
            for gate_id, receipt in receipts.items()
            if bool(receipt.get("required"))
            and receipt.get("state") != "passed"
        )
        artifacts_missing = sorted(
            gate_id
            for gate_id in {"clean-install", "benchmark-link", "sbom-notice"}
            if gate_id in receipts and not receipts[gate_id].get("artifacts")
        )
        ready = (
            not duplicates
            and not missing
            and not failed
            and not optional_required_failures
            and not artifacts_missing
            and report.get("ready") is True
        )
        verdict = {
            "schema": "zyra.release-admission/v1",
            "ready": ready,
            "source_commit": expected_commit,
            "mandatory_gate_count": len(self.policy.mandatory_gates),
            "receipt_count": len(receipts),
            "duplicates": duplicates,
            "missing": missing,
            "failed": failed,
            "required_failures": optional_required_failures,
            "artifacts_missing": artifacts_missing,
            "ci_report_digest": stable_digest(report),
        }
        verdict["digest"] = stable_digest(verdict)
        if not ready:
            raise GateFailure(
                "Release admission failed closed.",
                code="release_admission_failed",
                details=verdict,
            )
        return verdict


def standard_gate_registry(
    *,
    python: str,
    bun: str,
    output_root: Path,
    python_basetemp: Path,
    callable_gates: Mapping[str, Callable[[GateContext], Mapping[str, Any]]],
    python_test_arguments: Sequence[str] = ("tests/unit", "tests/integration"),
) -> GateRegistry:
    specifications = [
        GateSpec(
            gate_id="python-lock",
            callable=callable_gates["python-lock"],
            timeout_seconds=60,
        ),
        GateSpec(
            gate_id="javascript-lock",
            callable=callable_gates["javascript-lock"],
            timeout_seconds=60,
        ),
        GateSpec(
            gate_id="bundle-boundary",
            callable=callable_gates["bundle-boundary"],
            dependencies=("python-lock", "javascript-lock"),
            timeout_seconds=120,
        ),
        GateSpec(
            gate_id="checksums",
            callable=callable_gates["checksums"],
            dependencies=("bundle-boundary",),
            timeout_seconds=120,
            artifacts=("bundle-verification.json",),
        ),
        GateSpec(
            gate_id="sbom-notice",
            callable=callable_gates["sbom-notice"],
            dependencies=("bundle-boundary",),
            timeout_seconds=120,
            artifacts=("sbom-verification.json",),
        ),
        GateSpec(
            gate_id="python-tests",
            command=(
                python,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(python_basetemp.resolve()),
                "-o",
                "faulthandler_timeout=300",
                "-o",
                "faulthandler_exit_on_timeout=true",
                "-q",
                *python_test_arguments,
            ),
            dependencies=("python-lock",),
            timeout_seconds=5400,
            allow_parallel=False,
        ),
        GateSpec(
            gate_id="typescript-typecheck",
            command=(bun, "run", "typecheck"),
            dependencies=("javascript-lock",),
            timeout_seconds=900,
        ),
        GateSpec(
            gate_id="web-build",
            command=(bun, "run", "build:web"),
            dependencies=("typescript-typecheck",),
            timeout_seconds=900,
        ),
        GateSpec(
            gate_id="source-custody",
            callable=callable_gates["source-custody"],
            dependencies=("python-lock", "javascript-lock"),
            timeout_seconds=900,
            artifacts=(
                "source-custody-receipt.json",
                "source-custody-work-queue.json",
            ),
        ),
        GateSpec(
            gate_id="submission-boundary",
            command=(python, "scripts/verify_submission_boundary.py"),
            dependencies=("bundle-boundary",),
            timeout_seconds=300,
        ),
        GateSpec(
            gate_id="clean-install",
            callable=callable_gates["clean-install"],
            dependencies=(
                "checksums",
                "sbom-notice",
                "web-build",
                "submission-boundary",
            ),
            timeout_seconds=1800,
            allow_parallel=False,
            artifacts=("clean-install.json",),
        ),
        GateSpec(
            gate_id="semantic-health",
            callable=callable_gates["semantic-health"],
            dependencies=("clean-install",),
            timeout_seconds=900,
            allow_parallel=False,
            artifacts=("semantic-health.json",),
        ),
        GateSpec(
            gate_id="benchmark-link",
            callable=callable_gates["benchmark-link"],
            dependencies=("source-custody",),
            timeout_seconds=120,
            artifacts=("benchmark-link.json",),
        ),
    ]
    return GateRegistry(specifications)


__all__ = [
    "GateContext",
    "GateExecutor",
    "GateRegistry",
    "GateSpec",
    "PythonTestPolicy",
    "ReleaseAdmission",
    "standard_gate_registry",
]
