from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ContractError, stable_digest, utc_now, validate_identifier
from .engine_io import atomic_json_write
from .orchestrator import BoundedProcessRunner, ProcessRequest
from zyra_runtime.sandbox_gateway.redaction import SecretRedactor


LIVE_MATRIX_SCHEMA = "zyra.m3-live-regression-matrix/v1"


class LiveMatrixError(ContractError):
    pass


@dataclass(frozen=True, slots=True)
class LiveScenarioSpec:
    scenario_id: str
    title: str
    argv: tuple[str, ...]
    source_languages: tuple[str, ...]
    entry_kinds: tuple[str, ...]
    semantic_domains: tuple[str, ...]
    required_paths: tuple[str, ...] = ()
    timeout_seconds: float = 180.0
    maximum_output_bytes: int = 8 * 1024 * 1024
    environment: Mapping[str, str] = field(default_factory=dict)
    expected_markers: tuple[str, ...] = ()
    exclusive_resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scenario_id",
            validate_identifier(self.scenario_id, field_name="scenario_id"),
        )
        if not self.title.strip():
            raise LiveMatrixError(f"{self.scenario_id} title is required")
        if not self.argv or not self.argv[0].strip():
            raise LiveMatrixError(f"{self.scenario_id} argv is empty")
        if not self.source_languages:
            raise LiveMatrixError(
                f"{self.scenario_id} must declare source languages"
            )
        if not self.entry_kinds:
            raise LiveMatrixError(f"{self.scenario_id} must declare entry kinds")
        if not self.semantic_domains:
            raise LiveMatrixError(
                f"{self.scenario_id} must declare semantic domains"
            )
        if self.timeout_seconds <= 0 or self.timeout_seconds > 3_600:
            raise LiveMatrixError(
                f"{self.scenario_id} timeout must be in (0, 3600]"
            )
        if self.maximum_output_bytes < 1:
            raise LiveMatrixError(
                f"{self.scenario_id} output budget must be positive"
            )
        object.__setattr__(
            self,
            "source_languages",
            tuple(sorted({str(item).casefold() for item in self.source_languages})),
        )
        object.__setattr__(
            self,
            "entry_kinds",
            tuple(sorted({str(item).casefold() for item in self.entry_kinds})),
        )
        object.__setattr__(
            self,
            "semantic_domains",
            tuple(sorted({str(item).casefold() for item in self.semantic_domains})),
        )
        object.__setattr__(
            self,
            "required_paths",
            tuple(sorted({Path(item).as_posix() for item in self.required_paths})),
        )
        object.__setattr__(
            self,
            "exclusive_resources",
            tuple(sorted({str(item) for item in self.exclusive_resources})),
        )
        object.__setattr__(
            self,
            "environment",
            {str(key): str(value) for key, value in self.environment.items()},
        )

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "argv_digest": stable_digest(self.argv),
            "source_languages": list(self.source_languages),
            "entry_kinds": list(self.entry_kinds),
            "semantic_domains": list(self.semantic_domains),
            "required_paths": list(self.required_paths),
            "timeout_seconds": self.timeout_seconds,
            "maximum_output_bytes": self.maximum_output_bytes,
            "environment_names": sorted(self.environment),
            "expected_marker_digests": [
                stable_digest("marker", item)
                for item in self.expected_markers
            ],
            "exclusive_resources": list(self.exclusive_resources),
        }


@dataclass(frozen=True, slots=True)
class LiveScenarioReceipt:
    scenario_id: str
    spec_digest: str
    revision: str
    started_at: str
    completed_at: str
    duration_seconds: float
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    output_truncated: bool
    stdout_digest: str
    stderr_digest: str
    marker_results: Mapping[str, bool]
    required_path_results: Mapping[str, bool]
    working_tree_before_digest: str
    working_tree_after_digest: str
    working_tree_mutated: bool
    state_root_digest: str
    failure_codes: tuple[str, ...]
    process_digest: str
    diagnostic_excerpt: str = ""

    @property
    def passed(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.cancelled
            and not self.output_truncated
            and not self.working_tree_mutated
            and all(self.marker_results.values())
            and all(self.required_path_results.values())
            and not self.failure_codes
        )

    @property
    def digest(self) -> str:
        return stable_digest(self.material())

    def material(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "spec_digest": self.spec_digest,
            "revision": self.revision,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 6),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "output_truncated": self.output_truncated,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "marker_results": dict(sorted(self.marker_results.items())),
            "required_path_results": dict(
                sorted(self.required_path_results.items())
            ),
            "working_tree_before_digest": self.working_tree_before_digest,
            "working_tree_after_digest": self.working_tree_after_digest,
            "working_tree_mutated": self.working_tree_mutated,
            "state_root_digest": self.state_root_digest,
            "failure_codes": list(self.failure_codes),
            "process_digest": self.process_digest,
            "diagnostic_excerpt": self.diagnostic_excerpt,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.material(),
            "passed": self.passed,
            "receipt_digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class LiveMatrixReceipt:
    matrix_id: str
    revision: str
    started_at: str
    completed_at: str
    duration_seconds: float
    shard_index: int
    shard_count: int
    maximum_workers: int
    registry_digest: str
    cases: tuple[LiveScenarioReceipt, ...]
    source_language_counts: Mapping[str, int]
    entry_kind_counts: Mapping[str, int]
    semantic_domain_counts: Mapping[str, int]
    cancelled: bool

    @property
    def passed(self) -> bool:
        return bool(self.cases) and not self.cancelled and all(
            item.passed for item in self.cases
        )

    @property
    def digest(self) -> str:
        return stable_digest(self.material())

    def material(self) -> dict[str, Any]:
        return {
            "matrix_id": self.matrix_id,
            "revision": self.revision,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 6),
            "shard_index": self.shard_index,
            "shard_count": self.shard_count,
            "maximum_workers": self.maximum_workers,
            "registry_digest": self.registry_digest,
            "cases": [item.to_dict() for item in self.cases],
            "source_language_counts": dict(
                sorted(self.source_language_counts.items())
            ),
            "entry_kind_counts": dict(sorted(self.entry_kind_counts.items())),
            "semantic_domain_counts": dict(
                sorted(self.semantic_domain_counts.items())
            ),
            "cancelled": self.cancelled,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": LIVE_MATRIX_SCHEMA,
            **self.material(),
            "passed": self.passed,
            "status_counts": dict(
                Counter("passed" if item.passed else "failed" for item in self.cases)
            ),
            "receipt_digest": self.digest,
        }


class LiveMatrixRegistry:
    def __init__(self, specs: Iterable[LiveScenarioSpec] = ()) -> None:
        self._specs: dict[str, LiveScenarioSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: LiveScenarioSpec) -> None:
        if spec.scenario_id in self._specs:
            raise LiveMatrixError(
                f"duplicate live scenario: {spec.scenario_id}"
            )
        self._specs[spec.scenario_id] = spec

    @property
    def digest(self) -> str:
        return stable_digest(
            [self._specs[key].to_dict() for key in sorted(self._specs)]
        )

    def select(
        self,
        *,
        shard_index: int = 0,
        shard_count: int = 1,
        scenario_ids: Sequence[str] = (),
    ) -> tuple[LiveScenarioSpec, ...]:
        if shard_count < 1 or shard_count > 128:
            raise LiveMatrixError("shard_count must be in [1, 128]")
        if shard_index < 0 or shard_index >= shard_count:
            raise LiveMatrixError("shard_index must address the selected shard")
        requested = set(scenario_ids)
        unknown = requested - set(self._specs)
        if unknown:
            raise LiveMatrixError(
                f"unknown live scenarios: {sorted(unknown)}"
            )
        selected: list[LiveScenarioSpec] = []
        for scenario_id in sorted(self._specs):
            if requested and scenario_id not in requested:
                continue
            bucket = int(
                stable_digest("live-shard", scenario_id).removeprefix("sha256:")[:16],
                16,
            ) % shard_count
            if bucket == shard_index:
                selected.append(self._specs[scenario_id])
        return tuple(selected)

    def describe(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-live-regression-registry/v1",
            "registry_digest": self.digest,
            "scenario_count": len(self._specs),
            "scenarios": [
                self._specs[key].to_dict()
                for key in sorted(self._specs)
            ],
        }


class LiveRegressionMatrixRunner:
    """Runs generated-input product scenarios without importing test owners."""

    def __init__(
        self,
        project_root: str | Path,
        registry: LiveMatrixRegistry,
        *,
        temporary_parent: str | Path | None = None,
        maximum_workers: int = 1,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        if not self.project_root.is_dir():
            raise LiveMatrixError(
                f"project root is not a directory: {self.project_root}"
            )
        if maximum_workers < 1 or maximum_workers > 16:
            raise LiveMatrixError("maximum_workers must be in [1, 16]")
        self.registry = registry
        self.temporary_parent = (
            Path(temporary_parent).resolve(strict=False)
            if temporary_parent is not None
            else None
        )
        self.maximum_workers = maximum_workers
        self.cancellation = threading.Event()

    def cancel(self) -> None:
        self.cancellation.set()

    def run(
        self,
        output_path: str | Path,
        *,
        shard_index: int = 0,
        shard_count: int = 1,
        scenario_ids: Sequence[str] = (),
    ) -> LiveMatrixReceipt:
        specs = self.registry.select(
            shard_index=shard_index,
            shard_count=shard_count,
            scenario_ids=scenario_ids,
        )
        if not specs:
            raise LiveMatrixError("live matrix selection is empty")
        revision = self._revision()
        started_at = utc_now()
        started = time.monotonic()
        receipts: dict[str, LiveScenarioReceipt] = {}
        resources: set[str] = set()
        parallel: list[LiveScenarioSpec] = []
        serial: list[LiveScenarioSpec] = []
        for spec in specs:
            claimed = set(spec.exclusive_resources)
            if claimed & resources:
                serial.append(spec)
            else:
                resources.update(claimed)
                parallel.append(spec)
        workers = min(self.maximum_workers, len(parallel))
        if parallel:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, workers),
                thread_name_prefix="zyra-live-regression",
            ) as pool:
                futures = {
                    pool.submit(self._run_one, spec, revision): spec
                    for spec in parallel
                }
                for future in concurrent.futures.as_completed(futures):
                    spec = futures[future]
                    receipts[spec.scenario_id] = future.result()
        for spec in serial:
            receipts[spec.scenario_id] = self._run_one(spec, revision)
        completed = time.monotonic()
        language_counts: Counter[str] = Counter()
        entry_counts: Counter[str] = Counter()
        domain_counts: Counter[str] = Counter()
        for spec in specs:
            language_counts.update(spec.source_languages)
            entry_counts.update(spec.entry_kinds)
            domain_counts.update(spec.semantic_domains)
        receipt = LiveMatrixReceipt(
            matrix_id="m3-s02a01-real-owner-generated-input",
            revision=revision,
            started_at=started_at,
            completed_at=utc_now(),
            duration_seconds=completed - started,
            shard_index=shard_index,
            shard_count=shard_count,
            maximum_workers=self.maximum_workers,
            registry_digest=self.registry.digest,
            cases=tuple(receipts[item.scenario_id] for item in specs),
            source_language_counts=dict(language_counts),
            entry_kind_counts=dict(entry_counts),
            semantic_domain_counts=dict(domain_counts),
            cancelled=self.cancellation.is_set(),
        )
        atomic_json_write(output_path, receipt.to_dict())
        return receipt

    def _run_one(
        self,
        spec: LiveScenarioSpec,
        revision: str,
    ) -> LiveScenarioReceipt:
        started_at = utc_now()
        started = time.monotonic()
        before = self._working_tree()
        required_paths = {
            relative: (self.project_root / relative).is_file()
            for relative in spec.required_paths
        }
        failure_codes: list[str] = []
        missing = [path for path, exists in required_paths.items() if not exists]
        if missing:
            failure_codes.append("required_path_missing")
        state_root_digest = ""
        process = None
        diagnostic_excerpt = ""
        if not failure_codes:
            with tempfile.TemporaryDirectory(
                prefix=f"zyra-live-{spec.scenario_id}-",
                dir=str(self.temporary_parent) if self.temporary_parent else None,
            ) as temporary:
                state_root = Path(temporary).resolve()
                state_root_digest = stable_digest(
                    "isolated-state-root",
                    spec.scenario_id,
                    state_root.name,
                )
                environment = {
                    **spec.environment,
                    "HOME": str(state_root / "home"),
                    "USERPROFILE": str(state_root / "home"),
                    "XDG_CONFIG_HOME": str(state_root / "config"),
                    "XDG_CACHE_HOME": str(state_root / "cache"),
                    "XDG_DATA_HOME": str(state_root / "data"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPYCACHEPREFIX": str(state_root / "pycache"),
                    "TMP": str(state_root / "tmp"),
                    "TEMP": str(state_root / "tmp"),
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": str(self.project_root),
                    "ZYRA_LIVE_REGRESSION_STATE_ROOT": str(state_root),
                }
                for relative in (
                    "home",
                    "config",
                    "cache",
                    "data",
                    "tmp",
                ):
                    (state_root / relative).mkdir(parents=True, exist_ok=True)
                process = BoundedProcessRunner(self.cancellation).run(
                    ProcessRequest(
                        request_id=spec.scenario_id,
                        argv=spec.argv,
                        cwd=self.project_root,
                        environment=environment,
                        timeout_seconds=spec.timeout_seconds,
                        maximum_output_bytes=spec.maximum_output_bytes,
                    )
                )
        after = self._working_tree()
        marker_results: dict[str, bool] = {}
        if process is not None:
            combined = f"{process.stdout}\n{process.stderr}"
            marker_results = {
                stable_digest("marker", marker): marker in combined
                for marker in spec.expected_markers
            }
            if process.exit_code != 0:
                failure_codes.append("process_exit_nonzero")
            if process.timed_out:
                failure_codes.append("process_timeout")
            if process.cancelled:
                failure_codes.append("process_cancelled")
            if process.output_truncated:
                failure_codes.append("process_output_truncated")
            if marker_results and not all(marker_results.values()):
                failure_codes.append("expected_marker_missing")
            if failure_codes:
                diagnostic_excerpt = self._diagnostic_excerpt(
                    process.stdout,
                    process.stderr,
                )
        else:
            marker_results = {
                stable_digest("marker", marker): False
                for marker in spec.expected_markers
            }
        mutated = before != after
        if mutated:
            failure_codes.append("working_tree_mutated")
        completed = time.monotonic()
        return LiveScenarioReceipt(
            scenario_id=spec.scenario_id,
            spec_digest=spec.digest,
            revision=revision,
            started_at=started_at,
            completed_at=utc_now(),
            duration_seconds=completed - started,
            exit_code=process.exit_code if process else None,
            timed_out=process.timed_out if process else False,
            cancelled=process.cancelled if process else False,
            output_truncated=process.output_truncated if process else False,
            stdout_digest=process.stdout_digest if process else stable_digest(""),
            stderr_digest=process.stderr_digest if process else stable_digest(""),
            marker_results=marker_results,
            required_path_results=required_paths,
            working_tree_before_digest=stable_digest(before),
            working_tree_after_digest=stable_digest(after),
            working_tree_mutated=mutated,
            state_root_digest=state_root_digest,
            failure_codes=tuple(dict.fromkeys(failure_codes)),
            process_digest=(
                stable_digest(process.to_dict())
                if process is not None
                else stable_digest("process-not-started")
            ),
            diagnostic_excerpt=diagnostic_excerpt,
        )

    def _working_tree(self) -> tuple[str, ...]:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={self.project_root.as_posix()}",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        if completed.returncode:
            raise LiveMatrixError(
                f"cannot inspect working tree: {completed.stderr.strip()}"
            )
        return tuple(
            line.rstrip()
            for line in completed.stdout.splitlines()
            if line.strip()
        )

    def _revision(self) -> str:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={self.project_root.as_posix()}",
                "rev-parse",
                "HEAD",
            ],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
        revision = completed.stdout.strip().casefold()
        if completed.returncode or len(revision) != 40:
            raise LiveMatrixError(
                f"cannot resolve project revision: {completed.stderr.strip()}"
            )
        return revision

    @staticmethod
    def _diagnostic_excerpt(stdout: str, stderr: str) -> str:
        combined = "\n".join(
            item
            for item in (stdout[-4_096:], stderr[-8_192:])
            if item
        )
        report = SecretRedactor().redact_text(
            combined,
            source="live_matrix_failure",
        )
        return str(report.value)[-8_192:]


def resolve_bun(project_root: str | Path) -> str:
    root = Path(project_root).resolve()
    candidates = (
        root / "node_modules" / ".bin" / "bun.exe",
        root / "node_modules" / ".bin" / "bun",
    )
    local = next((item for item in candidates if item.is_file()), None)
    if local is not None:
        return str(local)
    executable = shutil.which("bun")
    if executable:
        return executable
    raise LiveMatrixError("Bun is required for the TypeScript live matrix")


def default_live_registry(
    project_root: str | Path,
    *,
    python_executable: str | None = None,
    bun_executable: str | None = None,
) -> LiveMatrixRegistry:
    root = Path(project_root).resolve()
    python = str(Path(python_executable or sys.executable).resolve())
    bun = bun_executable or resolve_bun(root)
    pytest = (python, "-m", "pytest", "-q", "-p", "no:cacheprovider")
    specs = (
        LiveScenarioSpec(
            scenario_id="default-cli-worker-generated-task",
            title="Default CLI executes a generated task through CodeWorkerRuntime",
            argv=(python, "scripts/verify_m3.py", "--runtime-only"),
            source_languages=("python", "typescript"),
            entry_kinds=("cli", "worker"),
            semantic_domains=("default-path", "routing", "recovery", "trace"),
            required_paths=("scripts/verify_m3.py",),
            timeout_seconds=300,
            expected_markers=("M3 runtime verification passed",),
            exclusive_resources=("default-runtime",),
        ),
        LiveScenarioSpec(
            scenario_id="default-api-code-index-disable",
            title="API creates fresh task workspace and proves code-index disable effect",
            argv=(*pytest, "tests/integration/test_code_index_api_main_path.py"),
            source_languages=("python",),
            entry_kinds=("api",),
            semantic_domains=("code-index", "default-path", "disable", "permission"),
            required_paths=("tests/integration/test_code_index_api_main_path.py",),
            timeout_seconds=240,
            exclusive_resources=("api-environment",),
        ),
        LiveScenarioSpec(
            scenario_id="default-web-projection",
            title="Web canonical projection consumes generated canonical events",
            argv=(bun, "test", "apps/web/test/canonical-projection-store.test.ts"),
            source_languages=("typescript", "tsx"),
            entry_kinds=("web",),
            semantic_domains=("causality", "default-path", "projection", "restore"),
            required_paths=("apps/web/test/canonical-projection-store.test.ts",),
            timeout_seconds=300,
            exclusive_resources=("bun-test-runtime",),
        ),
        LiveScenarioSpec(
            scenario_id="patch-git-backend",
            title="Patch and Git backend enforces stale, atomic, rollback and history",
            argv=(*pytest, "tests/integration/test_diff_patch_review_integration.py"),
            source_languages=("python",),
            entry_kinds=("api", "worker"),
            semantic_domains=("git", "patch", "permission", "rollback"),
            required_paths=("tests/integration/test_diff_patch_review_integration.py",),
            timeout_seconds=240,
            exclusive_resources=("api-environment",),
        ),
        LiveScenarioSpec(
            scenario_id="patch-git-web",
            title="Web patch review preserves digest binding and progressive friction",
            argv=(bun, "test", "apps/web/test/diff-patch-review-integration.test.ts"),
            source_languages=("typescript", "tsx"),
            entry_kinds=("web",),
            semantic_domains=("git", "patch", "progressive-friction"),
            required_paths=("apps/web/test/diff-patch-review-integration.test.ts",),
            timeout_seconds=240,
            exclusive_resources=("bun-test-runtime",),
        ),
        LiveScenarioSpec(
            scenario_id="approval-ledger-race-restore",
            title="Approval ledger binds identity, nonce, race, timeout and restore",
            argv=(
                bun,
                "test",
                "packages/runtime/sandbox-gateway-control/test/approval.test.ts",
            ),
            source_languages=("typescript",),
            entry_kinds=("worker",),
            semantic_domains=("approval", "idempotency", "race", "restore", "security"),
            required_paths=(
                "packages/runtime/sandbox-gateway-control/test/approval.test.ts",
            ),
            timeout_seconds=240,
            exclusive_resources=("bun-test-runtime",),
        ),
        LiveScenarioSpec(
            scenario_id="sealed-permission-console",
            title="Sealed permission API and console terminate without human wait",
            argv=(*pytest, "tests/integration/test_permission_console_api.py"),
            source_languages=("python", "typescript"),
            entry_kinds=("api", "web"),
            semantic_domains=("approval", "permission", "sealed", "timeout"),
            required_paths=("tests/integration/test_permission_console_api.py",),
            timeout_seconds=240,
            exclusive_resources=("api-environment",),
        ),
        LiveScenarioSpec(
            scenario_id="code-index-patch-refresh",
            title="Patch events invalidate and refresh real code-index selection",
            argv=(*pytest, "tests/integration/test_code_index_patch_event_integration.py"),
            source_languages=("python",),
            entry_kinds=("api", "worker"),
            semantic_domains=("code-index", "incremental", "patch", "test-selection"),
            required_paths=(
                "tests/integration/test_code_index_patch_event_integration.py",
            ),
            timeout_seconds=240,
            exclusive_resources=("api-environment",),
        ),
        LiveScenarioSpec(
            scenario_id="browser-permission-injection",
            title="Browser worker rejects unauthorized and untrusted action input",
            argv=(*pytest, "tests/integration/test_browser_worker_permission_gate.py"),
            source_languages=("python",),
            entry_kinds=("worker",),
            semantic_domains=("browser", "permission", "prompt-injection", "security"),
            required_paths=(
                "tests/integration/test_browser_worker_permission_gate.py",
            ),
            timeout_seconds=240,
            exclusive_resources=("browser-runtime",),
        ),
        LiveScenarioSpec(
            scenario_id="regression-evaluator-mutations",
            title="Evaluator rejects generated fake causality, pollution and digest mutation",
            argv=(*pytest, "tests/unit/test_m3_regression_hardening.py"),
            source_languages=("python",),
            entry_kinds=("freeze-gate",),
            semantic_domains=("clean-state", "mutation", "security", "triage"),
            required_paths=("tests/unit/test_m3_regression_hardening.py",),
            timeout_seconds=180,
            exclusive_resources=("regression-artifacts",),
        ),
    )
    return LiveMatrixRegistry(specs)


def verify_live_matrix_payload(
    payload: Mapping[str, Any],
    *,
    expected_revision: str,
) -> tuple[bool, tuple[str, ...]]:
    failures: list[str] = []
    if payload.get("schema") != LIVE_MATRIX_SCHEMA:
        failures.append("live_matrix_schema_invalid")
    if payload.get("revision") != expected_revision:
        failures.append("live_matrix_revision_mismatch")
    if payload.get("passed") is not True:
        failures.append("live_matrix_not_passed")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        failures.append("live_matrix_cases_missing")
        cases = []
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            failures.append(f"live_matrix_case_invalid:{index}")
            continue
        claimed = str(case.get("receipt_digest") or "")
        material = {
            key: case.get(key)
            for key in (
                "scenario_id",
                "spec_digest",
                "revision",
                "started_at",
                "completed_at",
                "duration_seconds",
                "exit_code",
                "timed_out",
                "cancelled",
                "output_truncated",
                "stdout_digest",
                "stderr_digest",
                "marker_results",
                "required_path_results",
                "working_tree_before_digest",
                "working_tree_after_digest",
                "working_tree_mutated",
                "state_root_digest",
                "failure_codes",
                "process_digest",
                "diagnostic_excerpt",
            )
        }
        if claimed != stable_digest(material):
            failures.append(f"live_matrix_case_digest_mismatch:{index}")
        if case.get("passed") is not True:
            failures.append(f"live_matrix_case_failed:{index}")
    matrix_fields = (
        "matrix_id",
        "revision",
        "started_at",
        "completed_at",
        "duration_seconds",
        "shard_index",
        "shard_count",
        "maximum_workers",
        "registry_digest",
        "cases",
        "source_language_counts",
        "entry_kind_counts",
        "semantic_domain_counts",
        "cancelled",
    )
    claimed_matrix = str(payload.get("receipt_digest") or "")
    expected_matrix = stable_digest(
        {key: payload.get(key) for key in matrix_fields}
    )
    if claimed_matrix != expected_matrix:
        failures.append("live_matrix_digest_mismatch")
    language_counts = payload.get("source_language_counts")
    if not isinstance(language_counts, Mapping):
        failures.append("live_matrix_language_counts_missing")
    else:
        for language in ("python", "typescript"):
            if int(language_counts.get(language) or 0) < 1:
                failures.append(f"live_matrix_language_missing:{language}")
    entry_counts = payload.get("entry_kind_counts")
    if not isinstance(entry_counts, Mapping):
        failures.append("live_matrix_entry_counts_missing")
    else:
        for entry in ("cli", "api", "web", "worker"):
            if int(entry_counts.get(entry) or 0) < 1:
                failures.append(f"live_matrix_entry_missing:{entry}")
    return not failures, tuple(failures)


__all__ = [
    "LIVE_MATRIX_SCHEMA",
    "LiveMatrixError",
    "LiveMatrixReceipt",
    "LiveMatrixRegistry",
    "LiveRegressionMatrixRunner",
    "LiveScenarioReceipt",
    "LiveScenarioSpec",
    "default_live_registry",
    "resolve_bun",
    "verify_live_matrix_payload",
]
